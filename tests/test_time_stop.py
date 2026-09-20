"""Zaman stop'u TEK kopyadır (strategies/time_stop.py).

Testin ölçtüğü şey bir davranış değil, bir DEĞİŞMEZ: aynı kuralı okuyan dört model
(11, 12, 14, 15) aynı sayıyı görmek zorundadır. Model 14 `ScalpModel` gövdesinden
türemez, yani kural kopyalanmış olsaydı ikisi bir gün sessizce ayrışabilir ve
`model 14 ↔ scalp_fixed` kıyasına ölçülmeyen bir dördüncü değişken girerdi.
"""

from __future__ import annotations

from typing import Any

import pandas as pd
import pytest

from core.config import load_config
from core.layers import resolve_layer
from core.tags import parse_tag
from strategies.base import Position
from strategies.scalp_fixed import ScalpFixed
from strategies.time_stop import TimeStop
from strategies.vwap_clone import VwapClone
from strategies.vwap_managed import VwapManaged
from tests.helpers_market import frame, market

START = pd.Timestamp("2026-01-01 00:00:00", tz="UTC")
SYMBOL = "BTC-USDT-SWAP"


@pytest.fixture()
def config() -> dict[str, Any]:
    return resolve_layer(load_config(), "scalp").config


def _market(bars: int = 40):
    return market({SYMBOL: frame([100.0] * bars, freq="15min", start=START)})


def _position(*, age_bars: int, as_of: pd.Timestamp) -> Position:
    return Position(
        symbol=SYMBOL,
        direction="long",
        entry_price=100.0,
        stop_price=99.0,
        opened_at=as_of - pd.Timedelta(minutes=15) * age_bars,
    )


def test_the_deadline_is_counted_in_bars_not_in_hours(config: dict[str, Any]) -> None:
    """Aynı "16 bar" 15m katmanında 4 saat, 4H kökünde 64 saattir."""
    assert TimeStop.from_config(config).duration == pd.Timedelta(minutes=15)
    assert TimeStop.from_config(load_config()).duration == pd.Timedelta(hours=4)


def test_a_position_younger_than_the_deadline_is_left_alone(config: dict[str, Any]) -> None:
    data = _market()
    stop = TimeStop.from_config(config)

    assert stop.instructions(data, [_position(age_bars=15, as_of=data.as_of)]) == []


def test_the_deadline_bar_closes_the_position(config: dict[str, Any]) -> None:
    """"16 bar" kararın verildiği bardır; dolum bir SONRAKİ barın açılışındadır (kural 13)."""
    data = _market()
    stop = TimeStop.from_config(config)

    instructions = stop.instructions(data, [_position(age_bars=16, as_of=data.as_of)])

    assert [item.symbol for item in instructions] == [SYMBOL]
    assert instructions[0].action == "close"
    assert instructions[0].fraction == 1.0


def test_the_instruction_carries_the_exit_rule_tag(config: dict[str, Any]) -> None:
    """Kural 13c: `exit_reason="signal"` tek başına zaman stop'unu başka çıkışlardan ayırmaz."""
    data = _market()
    instruction = TimeStop.from_config(config).instructions(
        data, [_position(age_bars=20, as_of=data.as_of)]
    )[0]

    assert parse_tag(instruction.reason, "exit_rule") == "time_stop"


# --------------------------------------------------------------------------- #
# Tek kopya sözleşmesi
# --------------------------------------------------------------------------- #
def test_model_14_reads_the_same_rule_as_the_scalp_models(config: dict[str, Any]) -> None:
    """Kopyalanmış bir uygulama, scalp_fixed kıyasına ölçülmeyen bir değişken koyardı."""
    assert VwapManaged(config=config)._time_stop == ScalpFixed(config=config)._time_stop


def test_model_14_closes_a_stale_position(config: dict[str, Any]) -> None:
    data = _market()
    model = VwapManaged(config=config)

    assert model.manage_positions(data, [_position(age_bars=15, as_of=data.as_of)]) == []
    late = model.manage_positions(data, [_position(age_bars=16, as_of=data.as_of)])
    assert parse_tag(late[0].reason, "exit_rule") == "time_stop"


def test_the_replica_has_no_time_stop(config: dict[str, Any]) -> None:
    """Kaynak sistemde zaman stop'u YOKTUR; eklemek kopyayı model 14'e çevirirdi (kural 15b)."""
    data = _market()

    assert VwapClone(config=config).manage_positions(
        data, [_position(age_bars=500, as_of=data.as_of)]
    ) == []
