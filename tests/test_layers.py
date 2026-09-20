"""core/layers.py: katman çözümü, override sınırları ve katmanlar arası izolasyon.

Katmanın işi ölçümün KOŞULLARINI değiştirmek, kurallarını değil. Bu dosya tam olarak o
sınırı ölçer: bar/evren/defter katmana göre değişir, maliyet ve risk sabitleri değişmez.
"""

from __future__ import annotations

import pytest

from core.config import ConfigError, load_config
from core.layers import DEFAULT_LAYER, layer_names, resolve_layer

SHARED_KEYS = (
    "initial_capital",
    "risk_per_trade",
    "leverage_cap",
    "max_positions",
    "max_short_positions",
    "fee_rate",
    "slippage_base",
    "slippage_short_stop",
    "maintenance_margin",
    "random_seed",
)


def test_repository_defines_its_layers() -> None:
    assert layer_names(load_config()) == ["base", "ema", "scalp"]


def test_base_layer_matches_the_root_config() -> None:
    """Varsayılan katman kökün kendisidir: 4 saatlik boru hattı bu eklemeden etkilenmez."""
    config = load_config()
    layer = resolve_layer(config, DEFAULT_LAYER)

    assert layer.timeframe == config["timeframe"] == "4H"
    assert layer.models == config["models"]
    assert layer.symbols is None  # evren hacimden hesaplanır
    assert layer.ledger_root.name == "ledgers"
    assert layer.metrics_path.name == "metrics.json"
    assert layer.retention.equity_compaction_days is None
    assert layer.breakdowns == ()


def test_scalp_layer_overrides_only_the_conditions() -> None:
    config = load_config()
    scalp = resolve_layer(config, "scalp")

    assert scalp.timeframe == "15m"
    assert scalp.models == ["scalp_fixed", "scalp_patient", "vwap_clone", "vwap_managed"]
    assert scalp.ledger_root.name == "ledgers_scalp"
    assert scalp.metrics_path.name == "metrics_scalp.json"
    assert scalp.breakdowns == ("arm", "symbol", "exit_rule", "session", "loss_streak")


def test_cost_and_risk_constants_are_identical_in_every_layer() -> None:
    """Kural 6: HER katman BİREBİR aynı maliyet/risk varsayımlarıyla koşar.

    Bu test ayrı bir scalp_config.yaml yerine katman bloğu seçilmesinin gerekçesidir:
    sabitler tek kaynaktan geldiği sürece sessizce ayrışamazlar. Katman listesi config'ten
    okunur, burada elle tutulmaz — elle tutulan bir liste, üçüncü katman eklendiği gün
    kapıyı sessizce o katmanın dışında bırakırdı.
    """
    config = load_config()
    root = resolve_layer(config, "base").config

    for name in layer_names(config):
        layer = resolve_layer(config, name).config
        for key in SHARED_KEYS:
            assert layer[key] == root[key], f"{name}.{key}"


def test_layers_never_share_a_ledger() -> None:
    """Aynı defter, bir katmanın turunun diğerinin `last_processed_bar`ını ezmesi demekti."""
    config = load_config()
    names = layer_names(config)

    roots = [resolve_layer(config, name).ledger_root for name in names]
    reports = [resolve_layer(config, name).metrics_path for name in names]
    assert len(set(roots)) == len(names)
    assert len(set(reports)) == len(names)


def test_ema_layer_pins_its_universe_and_carries_its_own_comparison_rows() -> None:
    """`ema` katmanı: sabit evren + kıyas hedefi/kontrol/çıpa İÇERİDE.

    Evren sabitliği katmanın varlık sebebidir (backtest ile canlı aynı kümeyi görmeli);
    kıyas satırlarının içeride olması ise katmanlar arası kıyas yapılmadığı içindir —
    `trend` dışarıda kalsaydı "bu model trend'den iyi mi" sorusu hiçbir tabloda
    cevaplanamazdı (bkz. docs/decisions.md > 45).
    """
    config = load_config()
    ema = resolve_layer(config, "ema")

    assert ema.timeframe == "4H"
    assert ema.symbols == resolve_layer(config, "scalp").symbols
    assert ema.ledger_root.name == "ledgers_ema"
    assert ema.metrics_path.name == "metrics_ema.json"
    assert {"ema_trend", "trend", "random_ctrl", "buyhold"} == set(ema.models)
    assert ema.config["signals_per_bar"] is False


def test_scalp_universe_is_fixed_and_complete() -> None:
    """Sabit evren kıyasın koşuludur: evren kayarsa geçmiş performans başka bir kümeye ait olur."""
    scalp = resolve_layer(load_config(), "scalp")

    assert scalp.symbols is not None
    assert len(scalp.symbols) == 13
    # OKX'te mevcut olmayan sembol (51001) evrende duramaz: config'in yazdığı küme ile
    # turun gerçekte gördüğü küme ayrışırsa "evren kaç sembol" sorusunun iki cevabı olur
    # (bkz. docs/decisions.md > 24).
    assert "TON-USDT-SWAP" not in scalp.symbols
    assert all(symbol.endswith("-USDT-SWAP") for symbol in scalp.symbols)
    assert "PENGU-USDT-SWAP" in scalp.symbols
    assert len(set(scalp.symbols)) == len(scalp.symbols)


def test_resolved_config_drops_the_layers_block() -> None:
    """Çözülmüş config'te tek bir "şimdi geçerli" değer olmalı.

    `layers` kalsaydı bir modül yanlışlıkla diğer katmanın barını okuyabilirdi.
    """
    assert "layers" not in resolve_layer(load_config(), "scalp").config


def test_unknown_layer_is_an_error_not_a_fallback() -> None:
    """Yazım hatasıyla base'e düşmek, scalp turunun 4 saatlik defteri ezmesi demekti."""
    with pytest.raises(ConfigError, match="tanınmayan katman"):
        resolve_layer(load_config(), "scalpp")


# --------------------------------------------------------------------------- #
# Blok şeması: eksik anahtar sessiz varsayılana düşmez
# --------------------------------------------------------------------------- #
def _config(**layer: object) -> dict[str, object]:
    base = {"timeframe": "4H", "models": ["x"], "data": {"cache_dir": "c", "history_bars": 10}}
    block = {
        "ledger_dir": "ledgers_x",
        "metrics_file": "docs/data/x.json",
        "universe": None,
        "retention": {"equity_compaction_days": None, "model_trade_limit": 10},
        "breakdowns": [],
    }
    block.update(layer)
    return {**base, "layers": {"x": block}}


@pytest.mark.parametrize(
    "missing", ["ledger_dir", "metrics_file", "universe", "retention", "breakdowns"]
)
def test_missing_layer_key_raises(missing: str) -> None:
    config = _config()
    del config["layers"]["x"][missing]  # type: ignore[index]

    with pytest.raises(ConfigError):
        resolve_layer(config, "x")


def test_empty_breakdown_list_is_a_valid_choice() -> None:
    """Boş liste bir tercihtir (kırılım yok); anahtarın YOKLUĞU ise bir eksiktir."""
    assert resolve_layer(_config(breakdowns=[]), "x").breakdowns == ()


def test_unknown_breakdown_kind_raises() -> None:
    with pytest.raises(ConfigError, match="tanınmayan kırılım"):
        resolve_layer(_config(breakdowns=["kol"]), "x")


def test_duplicate_symbol_in_universe_raises() -> None:
    """Tekrar eden sembol, evren sayısını olduğundan büyük gösterirdi."""
    with pytest.raises(ConfigError, match="tekrar eden sembol"):
        resolve_layer(_config(universe=["A-USDT-SWAP", "A-USDT-SWAP"]), "x")


def test_negative_compaction_window_raises() -> None:
    with pytest.raises(ConfigError, match="equity_compaction_days"):
        resolve_layer(
            _config(retention={"equity_compaction_days": 0, "model_trade_limit": 10}), "x"
        )


def test_overrides_merge_deeply() -> None:
    """`data.history_bars` yazmak `data`nın geri kalanını silmemeli."""
    config = _config()
    config["layers"]["x"]["data"] = {"history_bars": 999}  # type: ignore[index]

    resolved = resolve_layer(config, "x").config

    assert resolved["data"] == {"cache_dir": "c", "history_bars": 999}
