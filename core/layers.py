"""Katman çözücü: `config.yaml > layers.<ad>` bloğunu kök ayarların üzerine bindirir.

**Neden katman var.** Proje iki zaman diliminde ölçüm yapar: 4 saatlik ana yarışma
(`base`) ve 15 dakikalık scalp katmanı (`scalp`). İkisi AYNI çekirdeği kullanır —
`core/engine.py`, `core/portfolio.py`, `core/ledger.py`, `core/funding.py`,
`core/metrics.py` — çünkü maliyet, likidasyon, dolum ve metrik kuralları katmana göre
değişemez: değişirse iki katmanın sayıları birbirinin diliyle konuşmayı bırakır.
Katmanın değiştirebildiği şey, ölçümün **koşullarıdır**: hangi bar, hangi semboller,
hangi modeller, hangi defter, hangi rapor dosyası.

**Neden ayrı bir config dosyası değil.** `risk_per_trade`, `fee_rate`, `slippage_*`,
`leverage_cap`, `initial_capital` iki katmanda da BİREBİR aynıdır (kural 6). İki dosyaya
bölmek, bir gün birinin sessizce ayrışması demekti — ve iki katman farklı maliyet
varsayımlarıyla koşmaya başladığında bunu hiçbir test yakalamazdı. Kök tek kaynaktır;
katman bloğu yalnızca **fark**ı yazar.

**Neden `layers` anahtarı çözülen config'ten DÜŞÜRÜLÜR.** Çekirdek modüller ayarı
`get_setting(config, "timeframe")` ile okur. Çözülmüş config'te `layers` kalsaydı, bir
modül yanlışlıkla `layers.scalp.timeframe`'i okuyabilir ve base katmanı koşarken scalp'in
barını görebilirdi. Çözücüden çıkan sözlükte tek bir "şimdi geçerli" değer vardır.

Katman bloğunun ZORUNLU anahtarları (eksiği `ConfigError`; varsayılan yoktur):

| Anahtar | Anlamı |
|---|---|
| `ledger_dir` | Katmanın defter kökü. İki katman asla aynı defteri paylaşmaz. |
| `metrics_file` | Katmanın dashboard yükü (`docs/data/…json`). |
| `universe` | Sabit sembol listesi, ya da `null` (hacimden otomatik seçim). |
| `retention.equity_compaction_days` | Bu günden eski `equity.csv` satırları günlük özete iner; `null` = sıkıştırma yok. |
| `retention.model_trade_limit` | JSON'a model başına kaç kapanmış işlem yazılır. |
| `breakdowns` | JSON'a hangi kırılımlar eklenir (`"arm"`, `"symbol"`); boş liste = yok. |

Bunların dışındaki her anahtar kök ayarların üzerine **derin birleştirme** ile biner:
`data.history_bars` yazmak `data`nın geri kalanını silmez.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from core.config import ConfigError, get_setting, project_path

LAYERS_KEY = "layers"
DEFAULT_LAYER = "base"

# Katman bloğunun kendi anahtarları: kök ayarların üzerine BİNMEZLER, katmanın kimliğini
# tanımlarlar. Geri kalan her anahtar override'dır.
_IDENTITY_KEYS: tuple[str, ...] = ("ledger_dir", "metrics_file", "universe", "retention", "breakdowns")

_REQUIRED_RETENTION: tuple[str, ...] = ("equity_compaction_days", "model_trade_limit")

# Tanınan kırılım adları. `core/report.py::_BREAKDOWN_KEYS` ile aynı kümedir; ikisi ayrı
# yerlerde durur çünkü biri KATMAN ayarını doğrular (config okunurken, tur başlamadan),
# diğeri kırılımı ÜRETİR (rapor yazılırken). Buradaki kapı olmadan yazım hatası bir tur
# koştuktan sonra rapor aşamasında patlardı.
VALID_BREAKDOWNS: tuple[str, ...] = ("arm", "symbol", "exit_rule", "session", "loss_streak")


@dataclass(frozen=True, kw_only=True)
class Retention:
    """Katmanın depolama/sunum sınırları — ÖLÇÜM sabitleri değil.

    Neden config'te: 15 dakikalık katman günde 96 tur koşar ve her tur defteri commit
    eder; `equity.csv` yılda on binlerce satıra çıkar ve depo geçmişi bu satırlarla şişer.
    Sınır katmana göre değiştiği için (`base` sıkıştırma istemez) `core/report.py`'deki
    sunum sabitlerinin yanına konamaz: orada tek bir değer durur, burada katman başına.
    Ölçümü etkilemezler — `trades.csv` (denetim izi) hiçbir koşulda dokunulmaz.
    """

    equity_compaction_days: int | None
    model_trade_limit: int


@dataclass(frozen=True, kw_only=True)
class Layer:
    """Bir turun koşacağı katman: çözülmüş config + katman kimliği."""

    name: str
    config: dict[str, Any]
    ledger_root: Path
    metrics_path: Path
    symbols: list[str] | None  # None => evren hacimden hesaplanır (core/data.py)
    retention: Retention
    breakdowns: tuple[str, ...]

    @property
    def timeframe(self) -> str:
        return str(get_setting(self.config, "timeframe"))

    @property
    def models(self) -> list[str]:
        return [str(name) for name in get_setting(self.config, "models")]


def layer_names(config: Mapping[str, Any]) -> list[str]:
    block = config.get(LAYERS_KEY)
    if not isinstance(block, dict):
        raise ConfigError("config.yaml'da `layers` bloğu yok ya da sözlük değil")
    return sorted(str(name) for name in block)


def resolve_layer(config: Mapping[str, Any], name: str = DEFAULT_LAYER) -> Layer:
    """Katmanı çözer: kök ∪ katman override'ı, artı katman kimliği.

    Tanınmayan katman adı sessizce `base`e düşmez — yazım hatası yüzünden scalp turunun
    4 saatlik defteri ezmesi, ölçümün geri alınamaz biçimde bozulması demekti.
    """
    block = config.get(LAYERS_KEY)
    if not isinstance(block, dict):
        raise ConfigError("config.yaml'da `layers` bloğu yok ya da sözlük değil")
    if name not in block:
        raise ConfigError(
            f"tanınmayan katman: {name!r} (tanımlı: {', '.join(layer_names(config))})"
        )
    layer_block = block[name]
    if not isinstance(layer_block, dict):
        raise ConfigError(f"layers.{name} sözlük olmalı, {type(layer_block).__name__} geldi")

    resolved = {key: value for key, value in config.items() if key != LAYERS_KEY}
    overrides = {key: value for key, value in layer_block.items() if key not in _IDENTITY_KEYS}
    resolved = _deep_merge(resolved, overrides)

    return Layer(
        name=name,
        config=resolved,
        ledger_root=project_path(_require(layer_block, name, "ledger_dir", str)),
        metrics_path=project_path(_require(layer_block, name, "metrics_file", str)),
        symbols=_symbols(layer_block, name),
        retention=_retention(layer_block, name),
        breakdowns=_breakdowns(layer_block, name),
    )


# --------------------------------------------------------------------------- #
# Katman kimliği alanları
# --------------------------------------------------------------------------- #
def _symbols(block: Mapping[str, Any], layer: str) -> list[str] | None:
    """Sabit evren ya da None.

    Sabit evrenin gerekçesi kural 6'nın kıyas koşuludur: evren zamanla kayarsa geçmiş
    performans başka bir sembol kümesine ait olur ve iki modelin sayıları aynı yarışın
    sayıları olmaktan çıkar. `null` yazmak "hacimden otomatik" demektir ve bu tercih
    açıkça yazılır — anahtarın hiç olmaması bir tercih değil, bir eksiktir.
    """
    if "universe" not in block:
        raise ConfigError(f"layers.{layer}.universe eksik (sabit liste ya da null olmalı)")
    value = block["universe"]
    if value is None:
        return None
    if not isinstance(value, list) or not value:
        raise ConfigError(f"layers.{layer}.universe boş olmayan bir liste ya da null olmalı")
    symbols = [str(symbol) for symbol in value]
    duplicates = sorted({symbol for symbol in symbols if symbols.count(symbol) > 1})
    if duplicates:
        # Tekrar eden sembol, aynı sembolün iki kez çekilmesi ve evren sayısının
        # olduğundan büyük görünmesi demektir.
        raise ConfigError(f"layers.{layer}.universe içinde tekrar eden sembol: {duplicates}")
    return symbols


def _retention(block: Mapping[str, Any], layer: str) -> Retention:
    raw = block.get("retention")
    if not isinstance(raw, dict):
        raise ConfigError(f"layers.{layer}.retention sözlük olmalı")
    missing = [key for key in _REQUIRED_RETENTION if key not in raw]
    if missing:
        raise ConfigError(f"layers.{layer}.retention eksik anahtarlar: {', '.join(missing)}")

    days = raw["equity_compaction_days"]
    if days is not None:
        days = int(days)
        if days <= 0:
            raise ConfigError(
                f"layers.{layer}.retention.equity_compaction_days pozitif olmalı ya da null: {days}"
            )
    limit = int(raw["model_trade_limit"])
    if limit <= 0:
        raise ConfigError(f"layers.{layer}.retention.model_trade_limit pozitif olmalı: {limit}")
    return Retention(equity_compaction_days=days, model_trade_limit=limit)


def _breakdowns(block: Mapping[str, Any], layer: str) -> tuple[str, ...]:
    if "breakdowns" not in block:
        raise ConfigError(f"layers.{layer}.breakdowns eksik (boş liste de bir tercihtir)")
    value = block["breakdowns"]
    if not isinstance(value, list):
        raise ConfigError(f"layers.{layer}.breakdowns liste olmalı")
    names = tuple(str(item) for item in value)
    unknown = [item for item in names if item not in VALID_BREAKDOWNS]
    if unknown:
        raise ConfigError(
            f"layers.{layer}.breakdowns tanınmayan kırılım: {unknown} "
            f"(geçerli: {list(VALID_BREAKDOWNS)})"
        )
    return names


def _require(block: Mapping[str, Any], layer: str, key: str, kind: type) -> Any:
    if key not in block:
        raise ConfigError(f"layers.{layer}.{key} eksik")
    value = block[key]
    if not isinstance(value, kind):
        raise ConfigError(
            f"layers.{layer}.{key} {kind.__name__} olmalı, {type(value).__name__} geldi"
        )
    return value


def _deep_merge(base: dict[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    """Sözlükleri derinlemesine birleştirir: `data.history_bars` yazmak `data`yı silmez.

    Sığ birleştirme, katmanda tek bir alt anahtar yazan kullanıcının o bloğun geri
    kalanını (cache_dir, universe_file, …) sessizce düşürmesi demekti — ve eksik anahtar
    ancak koşu sırasında ConfigError olarak görünürdü.
    """
    merged = dict(base)
    for key, value in override.items():
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            merged[key] = _deep_merge(current, value)
        else:
            merged[key] = value
    return merged
