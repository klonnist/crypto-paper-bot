"""strategies/buyhold.py: referans çıpası gerçekten alıp tutuyor mu (CLAUDE.md kural 15).

Çıpanın değeri onun DEĞİŞMEZLİĞİNDEDİR: stop takarsa trend modeline, kaldıraçlanırsa
başka bir riske, satarsa bir zamanlama modeline dönüşür ve "model piyasayı yendi mi"
sorusunun zemini kaybolur. Testler bu üç dönüşümü de engeller.
"""

from __future__ import annotations

from typing import Sequence

import pandas as pd
import pytest

from core.validate import validate_signal
from strategies.base import MarketData, Position
from strategies.buyhold import WEIGHTS, BuyHold

START = pd.Timestamp("2026-01-01 00:00:00", tz="UTC")


def _frame(bars: int = 5) -> pd.DataFrame:
    index = pd.date_range(START, periods=bars, freq="4h", tz="UTC", name="ts")
    frame = pd.DataFrame(
        {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "volume": 1.0},
        index=index,
    )
    return frame


def _market(symbols: Sequence[str] = tuple(WEIGHTS)) -> MarketData:
    frame = _frame()
    return MarketData(
        ohlcv={symbol: frame for symbol in symbols},
        btc=frame,
        funding={},
        as_of=frame.index[-1],
    )


def test_declares_itself_a_long_only_benchmark() -> None:
    strategy = BuyHold()
    assert strategy.is_benchmark is True
    assert strategy.allowed_directions == ["long"]
    assert strategy.is_meta is False


def test_signals_split_capital_fifty_fifty() -> None:
    signals = BuyHold().generate_signals(_market())
    assert {s.symbol: s.notional_fraction for s in signals} == {
        "BTC-USDT-SWAP": 0.5,
        "ETH-USDT-SWAP": 0.5,
    }
    assert sum(s.notional_fraction or 0.0 for s in signals) == pytest.approx(1.0)


def test_signals_are_stopless_notional_fraction_longs() -> None:
    for signal in BuyHold().generate_signals(_market()):
        assert signal.direction == "long"
        assert signal.sizing == "notional_fraction"
        assert signal.stop_price is None
        assert signal.trailing_atr is None
        assert signal.take_profits == ()
        assert signal.reason  # deftere gerekçe yazılır (kabul çıtası)


def test_signals_pass_the_validation_gate() -> None:
    strategy = BuyHold()
    market = _market()
    for signal in strategy.generate_signals(market):
        validate_signal(
            signal,
            entry_price=100.0,
            allowed_directions=list(strategy.allowed_directions),
            symbol_universe=list(market.ohlcv),
            is_benchmark=strategy.is_benchmark,
        )


def test_symbol_missing_from_snapshot_is_skipped_not_raised() -> None:
    """Dışlanmış sembol için sinyal üretmek doğrulamada patlar ve TÜM modeli düşürürdü."""
    signals = BuyHold().generate_signals(_market(["BTC-USDT-SWAP"]))
    assert [signal.symbol for signal in signals] == ["BTC-USDT-SWAP"]


def test_never_exits() -> None:
    positions = [
        Position(
            symbol=symbol,
            direction="long",
            entry_price=100.0,
            stop_price=None,
            opened_at=START,
        )
        for symbol in WEIGHTS
    ]
    assert BuyHold().manage_positions(_market(), positions) == []


def test_weights_above_one_are_rejected() -> None:
    """Toplam > 1.0 çıpayı sessizce kaldıraçlardı."""
    with pytest.raises(ValueError, match="1.0'ı aşamaz"):
        BuyHold({"BTC-USDT-SWAP": 0.7, "ETH-USDT-SWAP": 0.7})
