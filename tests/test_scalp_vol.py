"""Model 17: `scalp_patient`in ikizi, TEK farkı kesitsel volatilite rejimi kapısı.

Çivilenen şey yine bir EŞİTLİK (bkz. `tests/test_scalp_managed.py`, `test_scalp_patient.py`):
kol seçimi, kapılar, geometri, zaman stop'u ve çekiliş birebir aynı olmalı. Ayrışan tek şey
rejim kapısıdır; bozulduğu an ortalama R farkı "kapının katkısı" olmaktan çıkar.
"""

from __future__ import annotations

from typing import Any

import pandas as pd
import pytest

from core.config import load_config
from core.layers import resolve_layer
from strategies.base import MarketData
from strategies.scalp.arms import ArmSetup
from strategies.scalp_patient import ScalpPatient
from strategies.scalp_vol import ScalpVol
from tests.helpers_market import frame, market

START = pd.Timestamp("2026-01-02 00:00:00", tz="UTC")


@pytest.fixture()
def config() -> dict[str, Any]:
    return resolve_layer(load_config(), "scalp").config


def _market(spreads: dict[str, float]) -> MarketData:
    """Sembol başına FARKLI oynaklık: `spread` doğrudan ATR'yi büyütür."""
    flat = [100.0 + (index % 3) * 0.05 for index in range(80)]
    closes = flat + [flat[-1] + 1.5 * (step + 1) for step in range(3)]
    volumes = [1.0] * 80 + [10.0] * 3
    return market({
        symbol: frame(closes, spread=spread, freq="15min", start=START, volumes=volumes)
        for symbol, spread in spreads.items()
    })


def _setup(symbol: str) -> ArmSetup:
    return ArmSetup(
        arm="momentum_burst", symbol=symbol, direction="long",
        entry_price=100.0, stop_price=95.0, target_price=110.0, detail="test",
    )


# --------------------------------------------------------------------------- #
# Ayrışan TEK şey
# --------------------------------------------------------------------------- #
def test_everything_except_the_regime_gate_is_inherited(config: dict[str, Any]) -> None:
    vol, patient = ScalpVol(config=config), ScalpPatient(config=config)
    assert ScalpVol.choose_arm is ScalpPatient.choose_arm
    assert ScalpVol.manage_positions is ScalpPatient.manage_positions
    assert vol._time_stop.bars == patient._time_stop.bars == 100
    assert vol.rng_identity == patient.rng_identity          # eşleştirilmiş deney
    assert vol.exit_management is patient.exit_management is None
    assert ScalpVol.regime_filter is not ScalpPatient.regime_filter  # ayrışan tek nokta


def test_the_twin_has_no_regime_gate(config: dict[str, Any]) -> None:
    """`scalp_patient` eksenin diğer ucu: kapısı OLMAMALI."""
    patient = ScalpPatient(config=config)
    data = _market({"BTC-USDT-SWAP": 0.1, "ETH-USDT-SWAP": 5.0})
    setups = [_setup("BTC-USDT-SWAP"), _setup("ETH-USDT-SWAP")]
    assert patient.regime_filter(setups, data) == setups


# --------------------------------------------------------------------------- #
# Kapının kendisi
# --------------------------------------------------------------------------- #
def test_below_median_volatility_is_dropped(config: dict[str, Any]) -> None:
    """Düşük ATR% elenir, yüksek kalır."""
    data = _market({
        "AAA-USDT-SWAP": 0.05, "BBB-USDT-SWAP": 0.10,
        "CCC-USDT-SWAP": 5.00, "DDD-USDT-SWAP": 8.00,
    })
    kept = ScalpVol(config=config).regime_filter(
        [_setup(s) for s in ("AAA-USDT-SWAP", "DDD-USDT-SWAP")], data
    )
    assert [s.symbol for s in kept] == ["DDD-USDT-SWAP"]


def test_the_threshold_is_the_universe_median_not_the_candidate_median(
    config: dict[str, Any]
) -> None:
    """Eşik ADAYLARDAN değil EVRENDEN gelmeli.

    Adaylara göre hesaplansaydı eşik "o barda kaç aday var"a bağlı olurdu — kesitsel bir
    rejim ölçüsü olmaktan çıkıp veriye göre kayan bir eşiğe dönüşürdü. Test bunu tek bir
    adayla gösteriyor: aday tek olduğu için aday-medyanı KENDİSİ olurdu ve hiç elenmezdi.
    """
    data = _market({
        "AAA-USDT-SWAP": 0.05, "BBB-USDT-SWAP": 5.00, "CCC-USDT-SWAP": 8.00,
    })
    # AAA evrenin en düşüğü; tek aday olsa bile evren medyanının altında kaldığı için elenir.
    assert ScalpVol(config=config).regime_filter([_setup("AAA-USDT-SWAP")], data) == []


def test_the_gate_is_not_applied_when_the_median_is_undefined(
    config: dict[str, Any]
) -> None:
    """Tek sembollük evrende medyan anlamsız: kapı UYGULANMAZ ve bu sayıma yazılır.

    Elemek, bir veri boşluğunu bir rejim kararıymış gibi gösterirdi.
    """
    model = ScalpVol(config=config)
    data = _market({"AAA-USDT-SWAP": 1.0})
    setups = [_setup("AAA-USDT-SWAP")]
    assert model.regime_filter(setups, data) == setups
    assert model.take_survey() == {"rejim_kapisi_yok": 1}


# --------------------------------------------------------------------------- #
# Denetim izi (karar 34'ün dersi)
# --------------------------------------------------------------------------- #
def test_survey_counts_what_the_gate_dropped(config: dict[str, Any]) -> None:
    """`momentum_burst`ün ölü olduğu iki backtest sonra öğrenildi çünkü sayım yoktu."""
    model = ScalpVol(config=config)
    data = _market({
        "AAA-USDT-SWAP": 0.05, "BBB-USDT-SWAP": 0.10,
        "CCC-USDT-SWAP": 5.00, "DDD-USDT-SWAP": 8.00,
    })
    model.regime_filter([_setup(s) for s in ("AAA-USDT-SWAP", "DDD-USDT-SWAP")], data)
    assert model.take_survey() == {"dusuk_vol": 1, "gecti": 1}
    assert model.take_survey() is None, "okunan sayım sıfırlanır, tur tur birikmez"


def test_the_candidate_is_not_in_the_live_layer_yet() -> None:
    """Önce taze bir OOS penceresinde ölçülür (docs/backtest.md > 4)."""
    from strategies.registry import REGISTRY

    assert ScalpVol.name in REGISTRY
    assert ScalpVol.name not in resolve_layer(load_config(), "scalp").models
