from typing import Mapping

import pandas as pd
import pytest

from strategies.base import (
    ExitInstruction,
    MarketData,
    Position,
    Signal,
    Strategy,
    TakeProfit,
)


def _market() -> MarketData:
    index = pd.DatetimeIndex([pd.Timestamp("2024-01-01", tz="UTC")], name="ts")
    df = pd.DataFrame(
        {"open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0], "volume": [1.0]},
        index=index,
    )
    funding = pd.Series([0.0], index=index, name="funding_rate")
    return MarketData(
        ohlcv={"BTC-USDT-SWAP": df},
        btc=df,
        funding={"BTC-USDT-SWAP": funding},
        as_of=index[-1],
    )


def test_strategy_cannot_be_instantiated_without_generate_signals() -> None:
    with pytest.raises(TypeError):
        Strategy()  # type: ignore[abstract]


def test_manage_positions_default_returns_empty_list() -> None:
    class OnlyOpens(Strategy):
        name = "only_opens"
        allowed_directions = ["long"]

        def generate_signals(
            self,
            market: MarketData,
            peer_signals: Mapping[str, tuple[Signal, ...]] | None = None,
        ) -> list[Signal]:
            return [Signal(symbol="BTC-USDT-SWAP", direction="long", stop_price=90.0)]

    strategy = OnlyOpens()
    market = _market()

    signals = strategy.generate_signals(market)
    assert signals[0].reason == ""

    position = Position(
        symbol="BTC-USDT-SWAP",
        direction="long",
        entry_price=100.0,
        stop_price=90.0,
        take_profits=(TakeProfit(price=110.0, fraction=1.0),),
        trailing_atr=None,
        opened_at=pd.Timestamp("2024-01-01"),
    )
    assert strategy.manage_positions(market, [position]) == []


def test_manage_positions_can_be_overridden() -> None:
    class ClosesEverything(Strategy):
        name = "closes_everything"
        allowed_directions = ["long", "short"]

        def generate_signals(
            self,
            market: MarketData,
            peer_signals: Mapping[str, tuple[Signal, ...]] | None = None,
        ) -> list[Signal]:
            return []

        def manage_positions(
            self, market: MarketData, positions: list[Position]
        ) -> list[ExitInstruction]:
            return [ExitInstruction(symbol=p.symbol, action="close") for p in positions]

    position = Position(
        symbol="ETH-USDT-SWAP",
        direction="short",
        entry_price=100.0,
        stop_price=110.0,
        take_profits=(),
        trailing_atr=1.5,
        opened_at=pd.Timestamp("2024-01-01"),
    )
    exits = ClosesEverything().manage_positions(_market(), [position])
    assert exits == [ExitInstruction(symbol="ETH-USDT-SWAP", action="close")]


def test_signal_take_profits_default_is_an_immutable_tuple() -> None:
    """frozen dataclass içinde mutable liste taşımak dondurmayı yarım bırakır."""
    signal = Signal(symbol="BTC-USDT-SWAP", direction="long", stop_price=90.0)
    assert signal.take_profits == ()
    with pytest.raises(AttributeError):
        signal.take_profits.append(TakeProfit(price=110.0, fraction=1.0))  # type: ignore[attr-defined]


def test_meta_flag_defaults_to_false() -> None:
    class Plain(Strategy):
        name = "plain"
        allowed_directions = ["long"]

        def generate_signals(
            self,
            market: MarketData,
            peer_signals: Mapping[str, tuple[Signal, ...]] | None = None,
        ) -> list[Signal]:
            return []

    assert Plain().is_meta is False
