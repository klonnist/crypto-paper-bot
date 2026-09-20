"""Scalp katmanının ORTAK kuralları: stop tabanı, 1.5R kapısı, zaman stop'u, kol etiketi.

Bu dosya iki modelin de uyduğu gövdeyi ölçer (`strategies/scalp/model.py`). Model 12'nin
model 11'in null hipotezi olabilmesi bu kuralların İKİSİNDE DE birebir aynı olmasına
bağlıdır; bu yüzden kapı testleri her iki modelde birden koşar.
"""

from __future__ import annotations

import random
from typing import Any, Sequence

import pandas as pd
import pytest

from core.config import load_config
from core.layers import resolve_layer
from core.tags import find_tag, parse_tag
from strategies.base import Direction, MarketData, Position
from strategies.scalp.arms import ArmParams, ArmSetup
from strategies.scalp.model import ScalpModel
from strategies.scalp_bandit import ScalpBandit
from strategies.scalp_fixed import ScalpFixed
from tests.helpers_market import frame, market

SYMBOL = "BTC-USDT-SWAP"
START = pd.Timestamp("2026-03-02 00:00:00", tz="UTC")


@pytest.fixture()
def config() -> dict[str, Any]:
    return resolve_layer(load_config(), "scalp").config


class _Stub(ScalpModel):
    """Kolları sabitlenmiş model: kapıların davranışı ölçülürken piyasa gürültüsü olmasın."""

    name = "stub_scalp"
    allowed_directions: list[Direction] = ["long", "short"]

    def __init__(self, *, config: Any, setups: Sequence[ArmSetup]) -> None:
        super().__init__(config=config)
        self._setups = list(setups)
        self.chosen: list[str] = []

    def generate_signals(self, market_data: MarketData, peer_signals: Any = None) -> Any:
        # propose_all yerine sabit kurulum listesi: kapı testinin ölçtüğü şey kolun
        # tetiklenmesi değil, kurulumun kapıdan geçip geçmediği.
        available = {
            arm: gated
            for arm, setups in self._by_arm().items()
            if (gated := self._gated(arm, setups))
        }
        if not available:
            return []
        rng = random.Random(0)
        arm, posterior = self.choose_arm(sorted(available), rng=rng)
        return [self._signal(available[arm][0], posterior=posterior)]

    def _by_arm(self) -> dict[str, list[ArmSetup]]:
        grouped: dict[str, list[ArmSetup]] = {}
        for setup in self._setups:
            grouped.setdefault(setup.arm, []).append(setup)
        return grouped

    def choose_arm(self, available: Sequence[str], *, rng: random.Random) -> tuple[str, float]:
        self.chosen.append(available[0])
        return available[0], 0.25


def _setup(
    *,
    arm: str = "vwap_pullback",
    entry: float = 100.0,
    stop: float = 99.0,
    target: float = 101.5,
    direction: Direction = "long",
) -> ArmSetup:
    return ArmSetup(
        arm=arm,
        symbol=SYMBOL,
        direction=direction,
        entry_price=entry,
        stop_price=stop,
        target_price=target,
        detail="test kurulumu",
    )


def _market_15m(bars: int = 60) -> MarketData:
    closes = [100.0 + 0.01 * i for i in range(bars)]
    return market({SYMBOL: frame(closes, spread=0.2, start=START, freq="15min")})


# --------------------------------------------------------------------------- #
# Kapı 1: stop tabanı
# --------------------------------------------------------------------------- #
def test_stop_below_floor_is_skipped(config: dict[str, Any]) -> None:
    """%1'in altındaki stop'ta tur maliyeti 0.25R'yi aşar: işlem ALINMAZ, stop genişletilmez."""
    narrow = _setup(entry=100.0, stop=99.5, target=102.0)  # %0.5 stop
    model = _Stub(config=config, setups=[narrow])

    assert model.generate_signals(_market_15m()) == []


def test_stop_at_the_floor_is_accepted(config: dict[str, Any]) -> None:
    """Taban bir eşiktir, bir tampon değil: tam %1 geçer (karşılaştırma `<`)."""
    model = _Stub(config=config, setups=[_setup(entry=100.0, stop=99.0, target=101.5)])

    signals = model.generate_signals(_market_15m())

    assert [signal.stop_price for signal in signals] == [99.0]


def test_narrow_stop_is_not_widened_to_the_floor(config: dict[str, Any]) -> None:
    """Atlamak ile tabana çekmek AYNI ŞEY DEĞİLDİR: ikincisi modelin tezini değiştirirdi."""
    model = _Stub(config=config, setups=[_setup(entry=100.0, stop=99.6, target=103.0)])

    assert model.generate_signals(_market_15m()) == []  # 99.0'a çekilmiş bir sinyal YOK


def test_short_stop_floor_uses_the_same_distance(config: dict[str, Any]) -> None:
    """Kapı yön bağımsızdır: short'ta da mesafe girişin %1'i ölçülür."""
    model = _Stub(
        config=config,
        setups=[_setup(direction="short", entry=100.0, stop=100.5, target=98.0)],
    )

    assert model.generate_signals(_market_15m()) == []


# --------------------------------------------------------------------------- #
# Kapı 2: hedef/stop oranı
# --------------------------------------------------------------------------- #
def test_reward_risk_below_the_bar_is_skipped(config: dict[str, Any]) -> None:
    """1.4R'lik kurulum atlanır: başabaş kazanma oranı çıtanın üstüne çıkardı."""
    model = _Stub(config=config, setups=[_setup(entry=100.0, stop=99.0, target=101.4)])

    assert model.generate_signals(_market_15m()) == []


def test_reward_risk_exactly_at_the_bar_is_accepted(config: dict[str, Any]) -> None:
    model = _Stub(config=config, setups=[_setup(entry=100.0, stop=99.0, target=101.5)])

    signals = model.generate_signals(_market_15m())

    assert [tp.price for tp in signals[0].take_profits] == [101.5]


def test_both_gates_are_reported_in_the_log(
    config: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    """Atlama SESSİZ OLAMAZ (kural 14): iki kapı da gerekçesini yazar."""
    model = _Stub(
        config=config,
        setups=[
            _setup(arm="rsi2_reversal", entry=100.0, stop=99.6, target=103.0),
            _setup(arm="momentum_burst", entry=100.0, stop=99.0, target=101.2),
        ],
    )

    with caplog.at_level("INFO", logger="strategies.scalp.model"):
        assert model.generate_signals(_market_15m()) == []

    messages = " ".join(record.getMessage() for record in caplog.records)
    assert "stop mesafesi" in messages
    assert "hedef/stop" in messages


# --------------------------------------------------------------------------- #
# Zaman stop'u
# --------------------------------------------------------------------------- #
def _position(opened_at: pd.Timestamp) -> Position:
    return Position(
        symbol=SYMBOL,
        direction="long",
        entry_price=100.0,
        stop_price=99.0,
        opened_at=opened_at,
    )


@pytest.mark.parametrize("model_class", [ScalpBandit, ScalpFixed])
def test_time_stop_closes_after_sixteen_bars(model_class: Any, config: dict[str, Any]) -> None:
    """16 bar (4 saat) dolduğunda pozisyon piyasa fiyatından kapatılır."""
    model = model_class(config=config)
    snapshot = _market_15m()
    opened = snapshot.as_of - pd.Timedelta(minutes=15) * 16

    instructions = model.manage_positions(snapshot, [_position(opened)])

    assert [item.action for item in instructions] == ["close"]
    assert parse_tag(instructions[0].reason, "exit_rule") == "time_stop"


@pytest.mark.parametrize("model_class", [ScalpBandit, ScalpFixed])
def test_time_stop_does_not_fire_one_bar_early(
    model_class: Any, config: dict[str, Any]
) -> None:
    """15 bar yetmez: sınır 16'dır ve erken kapatmak tezin ölçüldüğü pencereyi kısaltırdı."""
    model = model_class(config=config)
    snapshot = _market_15m()
    opened = snapshot.as_of - pd.Timedelta(minutes=15) * 15

    assert model.manage_positions(snapshot, [_position(opened)]) == []


@pytest.mark.parametrize("model_class", [ScalpBandit, ScalpFixed])
def test_time_stop_closes_every_overdue_position(
    model_class: Any, config: dict[str, Any]
) -> None:
    model = model_class(config=config)
    snapshot = _market_15m()
    old = snapshot.as_of - pd.Timedelta(hours=6)
    fresh = snapshot.as_of - pd.Timedelta(minutes=30)

    instructions = model.manage_positions(snapshot, [_position(old), _position(fresh)])

    assert len(instructions) == 1


# --------------------------------------------------------------------------- #
# reason kuyruğu: kol etiketi ayrıştırılabilir olmalı
# --------------------------------------------------------------------------- #
def test_signal_reason_carries_a_parsable_arm_tag(config: dict[str, Any]) -> None:
    """Kol bazlı kırılım bu etiketten okunur; ayrıştırılamazsa kırılım hiç üretilemez."""
    model = _Stub(config=config, setups=[_setup(arm="funding_spike_fade")])

    signal = model.generate_signals(_market_15m())[0]

    assert parse_tag(signal.reason, "arm") == "funding_spike_fade"
    assert find_tag(signal.reason, "post_r") == "0.25"
    assert "test kurulumu" in signal.reason  # serbest metin korunur


def test_models_do_not_request_trailing_stops(config: dict[str, Any]) -> None:
    """Trailing, işlemin gerçekleşen R'si ile kurulumun vaat ettiği R arasındaki bağı koparırdı."""
    model = _Stub(config=config, setups=[_setup()])

    assert model.generate_signals(_market_15m())[0].trailing_atr is None


# --------------------------------------------------------------------------- #
# İki modelin ortak gövdesi gerçekten ortak mı
# --------------------------------------------------------------------------- #
def test_both_models_read_the_same_gates(config: dict[str, Any]) -> None:
    """Model 12 ancak kapıları model 11 ile birebir aynıysa null hipotez olabilir."""
    bandit, fixed = ScalpBandit(config=config), ScalpFixed(config=config)

    for field in ("_min_stop_pct", "_min_reward_risk", "_time_stop"):
        assert getattr(bandit, field) == getattr(fixed, field), field
    assert bandit._params == fixed._params


def test_layer_config_drives_the_bar_duration() -> None:
    """Kök config 4H'tir: katman verilmezse model zaman stop'unu 64 saat sanırdı."""
    scalp = ScalpFixed(config=resolve_layer(load_config(), "scalp").config)

    assert scalp._time_stop.duration == pd.Timedelta(minutes=15)


def test_arm_params_come_from_the_single_atr_definition(config: dict[str, Any]) -> None:
    """ATR periyodu modelin değil projenin tanımıdır (config > trailing.atr_period)."""
    expected = int(load_config()["trailing"]["atr_period"])

    assert ScalpBandit(config=config)._params == ArmParams(
        atr_period=expected,
        stop_atr_multiple=float(config["scalp"]["stop_atr_multiple"]),
        target_reward_risk=float(config["scalp"]["target_reward_risk"]),
    )
