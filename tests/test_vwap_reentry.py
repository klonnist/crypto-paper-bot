"""Model 19 (`vwap_reentry`): `vwap_managed`in ikizi, TEK farkı giriş onayı.

Bu dosyanın ölçtüğü şey bir EŞİTLİK ve bir ALT KÜME: ikiz, ayrışan tek noktanın dışında
modelle birebir aynı olmalı (kapılar, stop, hedef, çıkış, zaman stop'u, seçim) ve aday kümesi
baz kümenin alt kümesi olmalı. Eşitlik bozulduğu an `vwap_managed ↔ vwap_reentry` farkı
"giriş onayının katkısı" olmaktan çıkar (strategies/scalp/model.py docstring'indeki aynı kural).
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest

from core.config import load_config
from core.layers import resolve_layer
from core.tags import parse_tag
from strategies import vwap_reentry as vwap_reentry_module
from strategies.base import MarketData
from strategies.registry import REGISTRY, build
from strategies.vwap import signal as vwap_signal
from strategies.vwap_managed import VwapManaged
from strategies.vwap_reentry import VwapReentry
from tests.helpers_market import frame, market
from tests.test_vwap_models import SPREAD, START, SYMBOL, _market, _reverting

OTHER = "ETH-USDT-SWAP"


@pytest.fixture()
def config() -> dict[str, Any]:
    return resolve_layer(load_config(), "scalp").config


def _two_symbol_market(strong: list[float], weak: list[float]) -> MarketData:
    frames = {
        SYMBOL: frame(strong, spread=SPREAD, freq="15min", start=START),
        OTHER: frame(weak, spread=SPREAD, freq="15min", start=START),
    }
    return market(frames)


# --------------------------------------------------------------------------- #
# Ayrışan TEK şey
# --------------------------------------------------------------------------- #
def test_only_the_entry_confirmation_and_the_label_are_overridden() -> None:
    """Sınıf gövdesi `name`, `_reentry` ve `_arm_name`den ibaret: başka hiçbir şey ezilmedi."""
    # `_abc_impl` ABCMeta'nın her alt sınıfa kendiliğinden eklediği iç alandır, bir ezme değil.
    own = {key for key in vars(VwapReentry) if not key.startswith("__") and key != "_abc_impl"}

    assert own == {"name", "_reentry", "_arm_name"}
    assert VwapReentry._reentry is True and VwapManaged._reentry is False
    assert VwapReentry._arm_name == vwap_signal.REENTRY_ARM_NAME != VwapManaged._arm_name


def test_every_inherited_setting_is_identical_to_the_base_model(config: dict[str, Any]) -> None:
    base, twin = VwapManaged(config=config), VwapReentry(config=config)

    for key, value in vars(base).items():
        if key == "_survey":
            continue
        assert vars(twin)[key] == value or repr(vars(twin)[key]) == repr(value), key
    assert twin._time_stop.bars == base._time_stop.bars
    assert twin._exit.describe() == base._exit.describe()


def test_the_twin_reads_no_config_key_of_its_own() -> None:
    """Yeni anahtar yok: ayrı bir anahtar, ayrı çıta ve ölçülmeyen ikinci bir değişken demekti."""
    source = inspect.getsource(vwap_reentry_module)

    assert "get_setting" not in source and "load_config" not in source


# --------------------------------------------------------------------------- #
# Giriş onayı: dönüş bandın İÇİNE kapanmalı
# --------------------------------------------------------------------------- #
def test_a_return_that_closes_inside_the_band_is_played_by_both(config: dict[str, Any]) -> None:
    data = _market(_reverting(rebound=1.0))         # z_prev −2.28 → z_now −1.85 (içeride)

    base = VwapManaged(config=config).generate_signals(data)
    twin = VwapReentry(config=config).generate_signals(data)

    assert len(base) == len(twin) == 1
    for field in ("symbol", "direction", "stop_price", "take_profits", "breakeven_at_r",
                  "partial_tp", "trail_giveback_pct"):
        assert getattr(twin[0], field) == getattr(base[0], field), field


def test_a_return_that_still_closes_outside_the_band_is_played_only_by_the_base(
    config: dict[str, Any],
) -> None:
    data = _market(_reverting())                     # z_prev −2.23 → z_now −2.12 (hâlâ dışarıda)

    assert len(VwapManaged(config=config).generate_signals(data)) == 1
    assert VwapReentry(config=config).generate_signals(data) == []


def test_the_arm_label_separates_the_two_models_in_the_ledger(config: dict[str, Any]) -> None:
    data = _market(_reverting(rebound=1.0))

    base = VwapManaged(config=config).generate_signals(data)[0]
    twin = VwapReentry(config=config).generate_signals(data)[0]

    assert parse_tag(base.reason, "arm") == vwap_signal.ARM_NAME
    assert parse_tag(twin.reason, "arm") == vwap_signal.REENTRY_ARM_NAME


def test_the_variant_picks_the_strongest_candidate_among_those_that_re_entered(
    config: dict[str, Any],
) -> None:
    """Baz, bandın dışına kapanan en güçlü adayı oynar; ikiz onu eler ve içeri dönen en güçlüyü."""
    strong_outside = _reverting(spike=2.0, rebound=0.1)     # |z_prev| 2.69 (daha güçlü), dışarıda
    weaker_inside = _reverting(rebound=1.0)                 # |z_prev| 2.28, içeride
    data = _two_symbol_market(strong_outside, weaker_inside)

    base = VwapManaged(config=config).generate_signals(data)
    twin = VwapReentry(config=config).generate_signals(data)

    assert [signal.symbol for signal in base] == [SYMBOL]
    assert [signal.symbol for signal in twin] == [OTHER]


# --------------------------------------------------------------------------- #
# Alt küme ve sayım
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("shape", [
    {}, {"rebound": 1.0}, {"rebound": 0.6}, {"spike": 2.0, "rebound": 0.1}, {"step": 0.3},
])
def test_the_variant_candidates_are_a_subset_of_the_base_candidates(shape: dict[str, float]) -> None:
    data = _market(_reverting(**shape))
    kwargs = dict(atr_period=14, band_mult=2.0, min_vwap_bars=8)

    base, _ = vwap_signal.scan(data, **kwargs)
    twin, _ = vwap_signal.scan(data, reentry=True, **kwargs)

    twin_symbols = {item.symbol for item in twin}
    assert twin_symbols <= {item.symbol for item in base}
    assert all(abs(item.z_now) < 2.0 for item in twin)
    assert all(abs(item.z_now) >= 2.0 for item in base if item.symbol not in twin_symbols)


def test_the_base_survey_never_carries_the_new_reason(config: dict[str, Any]) -> None:
    """Model 14'ün tur raporu bu eklemeden ETKİLENMEZ: yeni sebep anahtarı yalnızca ikizde yazılır."""
    data = _market(_reverting())
    base = VwapManaged(config=config)
    base.generate_signals(data)

    assert vwap_signal.OUTSIDE_AFTER_RETURN not in (base.take_survey() or {})
    _, survey = vwap_signal.scan(data, atr_period=14, band_mult=2.0, min_vwap_bars=8)
    assert vwap_signal.OUTSIDE_AFTER_RETURN not in survey.counts


def test_the_variant_survey_counts_what_the_filter_removed(config: dict[str, Any]) -> None:
    data = _market(_reverting())                     # baz için kurulum, ikiz için "bant dışı dönüş"
    twin = VwapReentry(config=config)
    twin.generate_signals(data)
    survey = twin.take_survey()

    assert survey is not None
    assert survey[vwap_signal.OUTSIDE_AFTER_RETURN] == 1 and survey[vwap_signal.SETUP] == 0
    reasons = (vwap_signal.SETUP, vwap_signal.INSIDE_BAND, vwap_signal.STILL_EXTENDING,
               vwap_signal.CROSSED, vwap_signal.NO_VWAP, vwap_signal.OUTSIDE_AFTER_RETURN)
    assert sum(survey[reason] for reason in reasons) == 1        # Σ counts = incelenen sembol
    assert [survey[key] for key in vwap_signal.GATE_KEYS] == [0, 0, 0]


def test_variant_plus_removed_equals_the_base_setups(config: dict[str, Any]) -> None:
    data = _two_symbol_market(_reverting(spike=2.0, rebound=0.1), _reverting(rebound=1.0))
    base, twin = VwapManaged(config=config), VwapReentry(config=config)
    base.generate_signals(data)
    twin.generate_signals(data)
    base_survey, twin_survey = base.take_survey() or {}, twin.take_survey() or {}

    assert base_survey[vwap_signal.SETUP] == (
        twin_survey[vwap_signal.SETUP] + twin_survey[vwap_signal.OUTSIDE_AFTER_RETURN]
    )


# --------------------------------------------------------------------------- #
# Kayıt ve canlı katman
# --------------------------------------------------------------------------- #
def test_the_candidate_is_registered_but_not_in_the_live_layer(config: dict[str, Any]) -> None:
    layer = resolve_layer(load_config(), "scalp")

    assert "vwap_reentry" in REGISTRY
    assert "vwap_reentry" not in layer.models
    assert build("vwap_reentry", config=config).name == "vwap_reentry"


def test_the_registered_name_matches_the_ledger_folder_name() -> None:
    """Ad, sınıfın `name` alanıyla birebir aynı olmalı: defter klasörü o addan türer."""
    assert REGISTRY["vwap_reentry"].name == "vwap_reentry"
