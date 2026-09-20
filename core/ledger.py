"""Append-only işlem/bakiye kaydı. Sistemin denetim izi (CLAUDE.md kural 1).

Model başına `ledgers/<model>/` altında üç dosya tutulur:

- `positions.json` — koşular arası taşınan DURUM: nakit, açık pozisyonlar, bekleyen
  emirler ve işlenmiş son bar. Tek yazılabilir (üzerine yazılan) dosya budur.
- `trades.csv`    — kapanan her işlem (kısmi çıkışlar dâhil) bir satır. Append-only.
- `equity.csv`    — bar başına bakiye/özsermaye anlık görüntüsü. Append-only.

Neden atomik yazma: koşu bir cron adımının ortasında düşerse (runner öldürülür, disk
dolar) yarım yazılmış bir defter, sonraki koşunun yanlış bakiyeyle devam etmesi demektir
— ölçüm sessizce bozulur. Her yazma geçici dosyaya yapılır, `fsync` edilir ve `os.replace`
ile yerine taşınır; `os.replace` aynı dizinde atomiktir, yani dosya ya eski ya yeni hâliyle
görünür, arada bir hâli olmaz.

Append'ler de aynı yoldan gider: mevcut içerik bayt bayt korunarak yeni satırlar eklenir ve
dosya bütün olarak değiştirilir. Yazılmış bir satır asla değiştirilmez veya silinmez —
"append-only" burada bir kural değil, dosya sözleşmesidir.

Bu modül işin mantığını bilmez: ne PnL hesaplar ne pozisyon yorumlar. Satırları yazar,
durumu okur/yazar. Böylece diğer core modülleri mock'lanarak tek başına test edilebilir.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Mapping, Sequence

from core.config import project_path

logger = logging.getLogger(__name__)

LEDGER_DIRNAME = "ledgers"

STATE_FILENAME = "positions.json"
TRADES_FILENAME = "trades.csv"
EQUITY_FILENAME = "equity.csv"

TRADE_COLUMNS: tuple[str, ...] = (
    "strategy",
    "symbol",
    "direction",
    "opened_at",
    "closed_at",
    "entry_price",
    "exit_price",
    "qty",
    "notional",
    "stop_price",
    "risk_amount",
    "leverage",
    "margin",
    "fee",
    "slippage_cost",
    "funding",
    "pnl",
    "exit_reason",
    "signal_reason",
    "notes",
)

EQUITY_COLUMNS: tuple[str, ...] = (
    "ts",
    "cash",
    "margin_used",
    "unrealized_pnl",
    "equity",
    "open_positions",
)

STATE_VERSION = 1


class LedgerError(RuntimeError):
    """Defter okunamıyor, şeması tutmuyor ya da model adı dosya yolu olarak güvensiz."""


class Ledger:
    """`ledgers/` altındaki model defterlerine erişim.

    `root` testlerde tmp_path'e yönlendirilebilsin diye parametredir; üretimde proje
    kökündeki `ledgers/` kullanılır.
    """

    def __init__(self, root: str | Path | None = None) -> None:
        self._root = Path(root) if root is not None else project_path(LEDGER_DIRNAME)

    @property
    def root(self) -> Path:
        return self._root

    def model_dir(self, model: str) -> Path:
        return self._root / _safe_model_name(model)

    # ----------------------------------------------------------------- #
    # Durum (positions.json)
    # ----------------------------------------------------------------- #
    def initialize_model(self, model: str, *, initial_capital: float) -> dict[str, Any]:
        """Defteri yoksa sıfırdan kurar, varsa olduğu gibi döndürür.

        Yarışmaya yeni bir model eklendiğinde defterinin kendiliğinden ve boş başlaması
        gerekir; mevcut bir modelin defterine dokunmak ise geçmiş işlemleri yok etmek
        demek olurdu (bunun için açıkça `reset_model` çağrılır).
        """
        existing = self.load_state(model)
        if existing is not None:
            return existing
        return self.reset_model(model, initial_capital=initial_capital)

    def reset_model(self, model: str, *, initial_capital: float) -> dict[str, Any]:
        """Defteri sıfırlar: durum başlangıç bakiyesine döner, CSV'ler yalnızca başlıktan ibaret olur."""
        directory = self.model_dir(model)
        directory.mkdir(parents=True, exist_ok=True)
        state = new_state(model, initial_capital=initial_capital)
        _atomic_write_text(directory / TRADES_FILENAME, _header_line(TRADE_COLUMNS))
        _atomic_write_text(directory / EQUITY_FILENAME, _header_line(EQUITY_COLUMNS))
        self.write_state(model, state)
        logger.info("%s defteri sıfırlandı (initial_capital=%s)", model, initial_capital)
        return state

    def load_state(self, model: str) -> dict[str, Any] | None:
        path = self.model_dir(model) / STATE_FILENAME
        if not path.is_file():
            return None
        try:
            with path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            # Bozuk durumu sessizce sıfırlamak, bakiyeyi uydurmak olurdu: koşu durur.
            raise LedgerError(f"{path} okunamadı: {exc}") from exc
        if not isinstance(payload, dict):
            raise LedgerError(f"{path} sözlük değil: {type(payload).__name__}")
        return payload

    def write_state(self, model: str, state: Mapping[str, Any]) -> None:
        path = self.model_dir(model) / STATE_FILENAME
        text = json.dumps(dict(state), indent=2, ensure_ascii=False, sort_keys=True) + "\n"
        _atomic_write_text(path, text)

    # ----------------------------------------------------------------- #
    # Satır ekleme (trades.csv / equity.csv)
    # ----------------------------------------------------------------- #
    def append_trades(self, model: str, rows: Sequence[Mapping[str, Any]]) -> None:
        self._append_rows(self.model_dir(model) / TRADES_FILENAME, TRADE_COLUMNS, rows)

    def append_equity(self, model: str, rows: Sequence[Mapping[str, Any]]) -> None:
        self._append_rows(self.model_dir(model) / EQUITY_FILENAME, EQUITY_COLUMNS, rows)

    def compact_equity(self, model: str, *, older_than: str) -> int:
        """`older_than`dan (ISO zaman damgası) eski equity satırlarını GÜNLÜK özete indirir.

        Neden bu dosyada bir istisna var: bu modülün sözleşmesi append-only'dir ve
        `trades.csv` için istisnasızdır — denetim izi odur, bir işlem satırı hiçbir
        gerekçeyle değişmez veya silinmez. `equity.csv` ise bar başına bir ANLIK GÖRÜNTÜ
        serisidir: aynı bilginin türevi, defterin kanıtı değil. 15 dakikalık katman günde
        96 tur koşar ve her turu commit eder; sıkıştırma olmadan tek bir modelin özsermaye
        dosyası yılda ~35 bin satıra çıkar ve depo geçmişi ölçümle ilgisiz satırlarla şişer.

        Sıkıştırma ölçümü değiştirmemek için iki kurala bağlıdır:
        - **Taze pencereye dokunulmaz.** `older_than` ve sonrası bar bazında kalır; max
          drawdown ve Sharpe gibi eğri metrikleri son dönemde tam çözünürlükte ölçülür.
        - **Gün başına SON satır tutulur** (o günün kapanış özsermayesi), ortalama değil:
          bir gün içindeki en düşük noktayı kaybetmek eski dönemin drawdown'ını olduğundan
          iyi gösterirdi; kapanış serisi en azından tutarlı bir günlük seridir ve aynı
          kural her model için birebir uygulanır.

        Sıkıştırılan satır sayısını döndürür (0 = değişiklik yok, dosya hiç yazılmaz).
        """
        path = self.model_dir(model) / EQUITY_FILENAME
        rows = _read_rows(path)
        if not rows:
            return 0

        keep: list[Mapping[str, Any]] = []
        daily: dict[str, Mapping[str, Any]] = {}
        for row in rows:
            ts = str(row.get("ts", ""))
            if ts >= older_than:
                keep.append(row)
                continue
            daily[ts[:10]] = row  # aynı günün sonraki satırı öncekini geçer: gün sonu kalır

        compacted = [daily[day] for day in sorted(daily)] + keep
        if len(compacted) == len(rows):
            return 0

        removed = len(rows) - len(compacted)
        _atomic_write_text(path, _header_line(EQUITY_COLUMNS) + _rows_to_csv(EQUITY_COLUMNS, compacted))
        logger.info(
            "%s equity.csv sıkıştırıldı: %d satır -> %d (%s öncesi günlük özete indi)",
            model, len(rows), len(compacted), older_than,
        )
        return removed

    def read_trades(self, model: str) -> list[dict[str, str]]:
        return _read_rows(self.model_dir(model) / TRADES_FILENAME)

    def read_equity(self, model: str) -> list[dict[str, str]]:
        return _read_rows(self.model_dir(model) / EQUITY_FILENAME)

    def _append_rows(
        self, path: Path, columns: Sequence[str], rows: Sequence[Mapping[str, Any]]
    ) -> None:
        if not rows:
            return
        existing = _read_text(path)
        if existing:
            _assert_header(path, existing, columns)
        else:
            existing = _header_line(columns)
        _atomic_write_text(path, existing + _rows_to_csv(columns, rows))


def new_state(model: str, *, initial_capital: float) -> dict[str, Any]:
    """Boş bir model durumu. Alanların anlamı core/engine.py ve core/portfolio.py'de."""
    return {
        "version": STATE_VERSION,
        "model": model,
        "initial_capital": float(initial_capital),
        "cash": float(initial_capital),
        "positions": [],
        "pending_orders": [],
        "last_processed_bar": None,
    }


# --------------------------------------------------------------------------- #
# Dosya yardımcıları
# --------------------------------------------------------------------------- #
def _safe_model_name(model: str) -> str:
    """Model adı bir dizin adı olacağı için yol ayırıcı/üst dizin içeremez."""
    name = str(model).strip()
    if not name or name in {".", ".."} or "/" in name or "\\" in name or os.sep in name:
        raise LedgerError(f"defter için güvensiz model adı: {model!r}")
    return name


def _header_line(columns: Sequence[str]) -> str:
    return ",".join(columns) + "\n"


def _rows_to_csv(columns: Sequence[str], rows: Sequence[Mapping[str, Any]]) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(
        buffer, fieldnames=list(columns), lineterminator="\n", extrasaction="raise"
    )
    for row in rows:
        missing = [column for column in columns if column not in row]
        if missing:
            raise LedgerError(f"satırda eksik kolon: {missing}")
        writer.writerow({column: _format(row[column]) for column in columns})
    return buffer.getvalue()


def _format(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        # Sabit basamak, defterin diff'lenebilir ve tekrarlanabilir kalması için.
        return f"{value:.10g}"
    return str(value)


def _read_text(path: Path) -> str:
    if not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise LedgerError(f"{path} okunamadı: {exc}") from exc


def _assert_header(path: Path, text: str, columns: Sequence[str]) -> None:
    header = text.split("\n", 1)[0].strip()
    expected = ",".join(columns)
    if header != expected:
        # Kolon şeması değiştiyse eski satırlar yeni kolonlarla karışır ve denetim izi
        # sessizce anlamsızlaşır: defteri elle taşımak gerekir.
        raise LedgerError(f"{path} başlığı beklenenden farklı:\n  var: {header}\n  beklenen: {expected}")


def _read_rows(path: Path) -> list[dict[str, str]]:
    text = _read_text(path)
    if not text:
        return []
    return list(csv.DictReader(io.StringIO(text)))


def _atomic_write_text(path: Path, text: str) -> None:
    """Geçici dosya + fsync + rename. Yarım yazılmış defter bırakmaz."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle_fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(handle_fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
