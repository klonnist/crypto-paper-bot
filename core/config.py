"""config.yaml okuyucu: sabitlerin tek kapısı.

Neden ayrı modül: config.yaml "tek kaynak" (CLAUDE.md kural 6/7). Loader'ı bir iş
modülünün (örn. core/data.py) içine koymak, ayara ihtiyaç duyan her modülü o iş
modülüne bağımlı kılardı.

Burada VARSAYILAN DEĞER YOKTUR. Eksik bir anahtar sessizce makul bir sayıya
düşerse modeller farklı maliyet/risk varsayımlarıyla yarışır ve ölçüm bozulur;
bu yüzden eksik anahtar ConfigError ile patlar.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.yaml"

REQUIRED_KEYS: tuple[str, ...] = (
    "initial_capital",
    "risk_per_trade",
    "leverage_cap",
    "max_positions",
    "max_short_positions",
    "fee_rate",
    "slippage_base",
    "slippage_short_stop",
    "maintenance_margin",
    "max_stop_atr_multiple",
    "signals_per_bar",
    "timeframe",
    "universe_size",
    "universe_refresh_days",
    "random_seed",
    "models",
    "acceptance.min_trades",
    "acceptance.control_model",
    "acceptance.control_min_trades",
    "acceptance.edge_margin_r",
    "acceptance.edge_ci_alpha",
    "acceptance.bootstrap_samples",
    "acceptance.stop_band_ratio",
    "trailing.atr_period",
    "funding.enabled",
    "funding.interval_hours",
    "exchange.rest_base",
    "exchange.inst_type",
    "exchange.quote_ccy",
    "exchange.btc_reference",
    "exchange.candles_limit",
    "exchange.history_candles_limit",
    "exchange.funding_limit",
    "exchange.request_timeout_sec",
    "exchange.min_request_interval_sec",
    "exchange.max_retries",
    "exchange.backoff_base_sec",
    "exchange.backoff_max_sec",
    "data.cache_dir",
    "data.universe_file",
    "data.history_bars",
    "data.funding_history_periods",
    "data.max_staleness_bars",
    # Katmanlar (core/layers.py): iki katman da AYNI çekirdeği koşar, farkları burada durur.
    "layers.base",
    "layers.scalp",
    # Scalp modellerinin ortak kısıtları. Yalnızca scalp modelleri okur, ama eksikliği
    # koşu ortasında değil config yüklenirken görülmelidir: 15 dakikalık katman günde 96
    # tur koşar, yarım saat sonra fark edilen bir eksik anahtar onlarca boş tur demektir.
    "scalp.min_stop_pct",
    "scalp.min_reward_risk",
    "scalp.time_stop_bars",
    "scalp.stop_atr_multiple",
    "scalp.target_reward_risk",
    "scalp.bandit.warmup_trades",
    "scalp.bandit.min_allocation",
    "scalp.bandit.window_trades",
    "scalp.bandit.prior_r_sigma",
)


class ConfigError(RuntimeError):
    """config.yaml eksik ya da beklenen şemaya uymuyor."""


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    config_path = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    if not config_path.is_file():
        raise ConfigError(f"config dosyası bulunamadı: {config_path}")

    with config_path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle)

    if not isinstance(raw, dict):
        raise ConfigError(f"config kök düzeyi sözlük olmalı, {type(raw).__name__} geldi")

    missing = [key for key in REQUIRED_KEYS if not _has(raw, key)]
    if missing:
        raise ConfigError("config.yaml'da eksik anahtarlar: " + ", ".join(missing))

    return raw


def get_setting(config: dict[str, Any], dotted_key: str) -> Any:
    """`"exchange.rest_base"` gibi noktalı yolu okur; yoksa ConfigError fırlatır."""
    node: Any = config
    for part in dotted_key.split("."):
        if not isinstance(node, dict) or part not in node:
            raise ConfigError(f"config anahtarı yok: {dotted_key}")
        node = node[part]
    return node


def project_path(relative: str | Path) -> Path:
    """config.yaml'daki göreli yolları proje köküne göre çözer.

    Koşunun hangi dizinden başlatıldığına bağlı olarak önbelleğin farklı yerlere
    yazılması, "eksik barları çek" mantığını sessizce bozar.
    """
    candidate = Path(relative)
    return candidate if candidate.is_absolute() else PROJECT_ROOT / candidate


def _has(config: dict[str, Any], dotted_key: str) -> bool:
    try:
        get_setting(config, dotted_key)
    except ConfigError:
        return False
    return True
