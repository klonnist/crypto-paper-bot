"""config.yaml'ın şemaya uyduğunu ve eksik anahtarın sessizce geçmediğini doğrular.

Bir sabitin config'ten düşmesi (ya da modülün kendi varsayılanını kullanması) modellerin
farklı maliyet/risk varsayımlarıyla yarışması demektir — ölçümün adilliği buna bağlı.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.config import REQUIRED_KEYS, ConfigError, get_setting, load_config


def test_repository_config_has_every_required_key() -> None:
    config = load_config()
    for key in REQUIRED_KEYS:
        get_setting(config, key)


def test_repository_config_matches_claude_md_values() -> None:
    config = load_config()
    assert config["initial_capital"] == 10000
    assert config["risk_per_trade"] == 0.01
    assert config["leverage_cap"] == 5
    assert config["max_positions"] == 5
    assert config["max_short_positions"] == 3
    assert config["fee_rate"] == 0.00055
    assert config["slippage_base"] == 0.0005
    assert config["slippage_short_stop"] == 0.0015
    assert config["maintenance_margin"] == 0.005
    assert config["max_stop_atr_multiple"] == 3.0
    assert config["timeframe"] == "4H"
    assert config["universe_size"] == 50
    assert config["universe_refresh_days"] == 30
    assert isinstance(config["random_seed"], int)
    assert isinstance(config["models"], list)


def test_legacy_key_names_are_gone() -> None:
    """slippage_long dâhil eski adlar config'te kalmamalı.

    slippage_long adı, kaymanın yalnızca long'lara uygulandığı izlenimini veriyordu; oran
    aslında short stop dışındaki her dolumda geçerli. Eski ad geri sızarsa iki anahtar bir
    süre birlikte yaşar ve modeller farklı maliyet varsayımlarıyla yarışır.
    """
    config = load_config()
    for legacy in ("commission_rate", "account", "universe", "schedule", "slippage_long"):
        assert legacy not in config, f"eski anahtar hâlâ duruyor: {legacy}"


def test_missing_key_raises_instead_of_defaulting(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("initial_capital: 10000\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="eksik anahtarlar"):
        load_config(path)


def test_unknown_dotted_key_raises() -> None:
    with pytest.raises(ConfigError, match="config anahtarı yok"):
        get_setting(load_config(), "exchange.nope")
