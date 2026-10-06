#!/usr/bin/env python3
"""Motor kurallarının DEFTERDEKİ izlerini ölçer. SALT OKUNUR: ölçüm aracı, kural değil.

Cevapladığı üç soru (karar taslağı P3, docs/taslak-p3-motor-kurallari.md):

(a) **Ters yönlü pozisyon.** Aynı model aynı sembolde ters yönde AÇIK bir pozisyon varken yeni
    bir pozisyon açıyor mu ve bu ne sıklıkta? (`Account.find` yalnızca `(sembol, yön)`e bakar:
    karşı yön engellenmez.) İki sayı: GİRİŞTE (açılış anında karşı yönde açık pozisyon var) ve
    ARALIK ÇAKIŞMASI (tutuş aralıkları herhangi bir anda kesişiyor).
(b) **Teminat kuralı.** `size_position`: notional serbest nakdi aşınca marj nakdin TAMAMIDIR.
    Kaç pozisyon bu yolla açıldı (`leverage > 1`), nakit ne kadar süre tükenmiş durumda ve —
    tur raporlarından — `zero_size` retleri kaç pozisyon AÇIKKEN yaşandı (kota dolu değilse
    ret nakitten gelmiştir)?
(c) **Ret sayımı.** Modelin tur raporlarındaki `rejections` toplamı (git geçmişinden).

**Getiri/R'ye BAKMAZ** (docs/backtest.md > 7): yalnızca sıklık ve sayı. Bu araç deftere
yazmaz, `config.yaml`a dokunmaz ve hiçbir modelin davranışını değiştirmez.

**Tanım kararları.** Pozisyon birimi `core/metrics.py::merge_fills` (kural 7: ikinci bir tanım
yok); açık pozisyonlar `positions.json`dan eklenir (kapanmış defter tek başına, kapanmamış
çakışmaları kaçırırdı — karar 33-DÜZELTME'nin aynı dersi). `zero_size` ile açık pozisyon
sayısı yalnızca TEK BARLIK turlarda eşleştirilir (çok barlık turda ret hangi bara ait
bilinemez) ve eşleştirilemeyenler ayrıca sayılır.

Kullanım (depo kökünden):
    python scripts/measure_engine_rules.py
    python scripts/measure_engine_rules.py --layer base
    python scripts/measure_engine_rules.py --layer scalp --history   # tur raporları (git)
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.config import load_config  # noqa: E402
from core.layers import resolve_layer  # noqa: E402
from core.metrics import (  # noqa: E402
    Interval,
    cash_stats,
    leverage_stats,
    opposite_stats,
    position_intervals,
)

logger = logging.getLogger("measure_engine_rules")


def intervals_of(
    trades: Sequence[Mapping[str, Any]], state: Mapping[str, Any] | None
) -> list[Interval]:
    """Kapanmış + AÇIK pozisyonların tutuş aralıkları (tanım `core/metrics.py::position_intervals`)."""
    return position_intervals(trades, (state or {}).get("positions") or ())


def zero_size_by_open_positions(
    rounds: Iterable[Mapping[str, Any]],
    equity_rows: Sequence[Mapping[str, Any]],
    model: str,
) -> dict[str, Any]:
    """`zero_size` retlerinin kaç AÇIK pozisyonla yaşandığı (tek barlık turlar)."""
    open_at = {str(row["ts"]): int(float(row["open_positions"])) for row in equity_rows}
    by_open: Counter[int] = Counter()
    unmatched = 0
    for report in rounds:
        if report.get("dry_run"):
            continue
        for entry in report.get("models") or ():
            if entry.get("model") != model:
                continue
            count = int((entry.get("rejections") or {}).get("zero_size", 0))
            if not count:
                continue
            count_open = open_at.get(str(report.get("as_of")))
            if int(entry.get("bars_processed") or 0) != 1 or count_open is None:
                unmatched += count
            else:
                by_open[count_open] += count
    return {"by_open": dict(sorted(by_open.items())), "unmatched": unmatched}


def rejection_totals(rounds: Iterable[Mapping[str, Any]], model: str) -> dict[str, int]:
    totals: Counter[str] = Counter()
    signals = 0
    for report in rounds:
        if report.get("dry_run"):
            continue
        for entry in report.get("models") or ():
            if entry.get("model") == model:
                signals += int(entry.get("signals") or 0)
                totals.update({k: int(v) for k, v in (entry.get("rejections") or {}).items()})
    return {"signals": signals, **totals}


def rounds_from_git(path: str, *, repo: Path = REPO_ROOT) -> list[dict[str, Any]]:
    """`path` dosyasının git geçmişindeki HER sürümü (eskiden yeniye); `round` bölümleriyle."""
    log = subprocess.run(
        ["git", "log", "--format=%H", "--reverse", "--", path],
        cwd=repo, capture_output=True, text=True, check=True,
    ).stdout.split()
    reports: list[dict[str, Any]] = []
    with subprocess.Popen(
        ["git", "cat-file", "--batch"], cwd=repo, stdin=subprocess.PIPE, stdout=subprocess.PIPE
    ) as proc:
        assert proc.stdin and proc.stdout
        for sha in log:
            proc.stdin.write(f"{sha}:{path}\n".encode())
            proc.stdin.flush()
            head = proc.stdout.readline().decode().split()
            if len(head) < 3:
                continue
            body = proc.stdout.read(int(head[2]))
            proc.stdout.read(1)
            try:
                payload = json.loads(body)
            except ValueError:
                continue
            reports.append(
                {
                    "as_of": payload.get("as_of"),
                    "dry_run": payload.get("dry_run"),
                    "models": (payload.get("round") or {}).get("models") or [],
                }
            )
    return reports


def _float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _read_state(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def measure_model(
    root: Path, model: str, *, rounds: Sequence[Mapping[str, Any]] | None
) -> dict[str, Any]:
    directory = root / model
    trades = _read_csv(directory / "trades.csv")
    equity = _read_csv(directory / "equity.csv")
    state = _read_state(directory / "positions.json")
    result: dict[str, Any] = {
        "model": model,
        **opposite_stats(intervals_of(trades, state)),
        **{f"lev_{k}": v for k, v in leverage_stats(trades).items()},
        **cash_stats(equity),
    }
    if rounds is not None:
        result["rejections"] = rejection_totals(rounds, model)
        result["zero_size"] = zero_size_by_open_positions(rounds, equity, model)
    return result


def format_report(results: Sequence[Mapping[str, Any]], *, layer: str) -> str:
    lines = [
        f"MOTOR KURALLARININ DEFTERDEKİ İZİ — katman={layer} (getiri/R'ye BAKILMAZ)",
        "",
        "(a) TERS YÖNLÜ POZİSYON  (birim: pozisyon; kapanmış + açık)",
        f"{'model':<18} {'pozisyon':>9} {'girişte':>8} {'aralık çakışması':>17}",
    ]
    for item in results:
        lines.append(
            f"{item['model']:<18} {item['positions']:>9} {item['opposite_at_entry']:>8} "
            f"{item['opposite_overlap']:>17}"
        )
    lines += [
        "",
        "(b) TEMİNAT  (leverage > 1 = notional nakdi aştı → marj nakdin TAMAMI)",
        f"{'model':<18} {'kapanmış':>9} {'kaldıraçlı':>11} {'bar':>7} {'nakit<%2':>9} "
        f"{'nakit<0':>8} {'min nakit/özsermaye':>20}",
    ]
    for item in results:
        lines.append(
            f"{item['model']:<18} {item['lev_closed']:>9} {item['lev_leveraged']:>11} "
            f"{int(item['bars']):>7} {int(item['cash_tight']):>9} {int(item['cash_negative']):>8} "
            f"{item['min_cash_ratio']:>19.3%}"
        )
    if any("rejections" in item for item in results):
        lines += ["", "(c) RETLER (tur raporlarının toplamı, git geçmişi)"]
        for item in results:
            rejections = item.get("rejections") or {}
            zero = item.get("zero_size") or {}
            if not rejections.get("signals"):
                continue
            detail = ", ".join(f"{k}={v}" for k, v in sorted(rejections.items()) if k != "signals")
            lines.append(f"  {item['model']:<18} sinyal={rejections['signals']}  {detail or 'ret yok'}")
            if zero.get("by_open") or zero.get("unmatched"):
                lines.append(
                    f"  {'':<18} zero_size ↔ açık pozisyon sayısı: {zero.get('by_open')}  "
                    f"(eşleştirilemeyen: {zero.get('unmatched')})"
                )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--layer", default="scalp")
    parser.add_argument("--models", default="", help="virgülle; boşsa defter klasöründeki tüm modeller")
    parser.add_argument("--history", action="store_true", help="tur raporlarını git geçmişinden oku (c)")
    parser.add_argument("--ledger-root", default=None, help="defter kökünü ez")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    layer = resolve_layer(load_config(), args.layer)
    root = Path(args.ledger_root) if args.ledger_root else layer.ledger_root
    models = [m for m in args.models.split(",") if m] or sorted(
        entry.name for entry in root.iterdir() if entry.is_dir()
    )
    rounds = None
    if args.history:
        metrics_path = str(layer.metrics_path.relative_to(REPO_ROOT)).replace("\\", "/")
        rounds = rounds_from_git(metrics_path)
        logger.info("%d tur raporu okundu (%s)", len(rounds), metrics_path)

    results = [measure_model(root, model, rounds=rounds) for model in models]
    print(format_report(results, layer=args.layer))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
