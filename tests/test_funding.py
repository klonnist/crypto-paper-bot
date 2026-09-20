"""core/funding.py: funding'in YÖNÜ, periyodu ve veri boşluğunda davranışı.

Yön hatası tam da projenin ana sorusunu (short'lar daha mı başarılı) bozar: işareti ters
çevirmek, shortları sistematik olarak kârlı ya da zararlı gösterirdi.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import pandas as pd
import pytest

from core.funding import FundingCharge, accrue, funding_amount, funding_times, rate_at

SYMBOL = "BTC-USDT-SWAP"
BAR = pd.Timedelta(hours=4)


@dataclass
class _Position:
    symbol: str
    direction: str
    qty: float
    opened_at: pd.Timestamp


def _series(*pairs: tuple[str, float]) -> pd.Series:
    index = pd.DatetimeIndex([pd.Timestamp(ts, tz="UTC") for ts in (p[0] for p in pairs)], name="ts")
    return pd.Series([value for _, value in pairs], index=index, dtype="float64")


# --------------------------------------------------------------------------- #
# Yön kuralı
# --------------------------------------------------------------------------- #
def test_long_pays_and_short_receives_on_positive_funding() -> None:
    paid = funding_amount(direction="long", qty=2.0, price=100.0, rate=0.0001)
    received = funding_amount(direction="short", qty=2.0, price=100.0, rate=0.0001)
    assert paid == pytest.approx(-0.02)  # 200 notional × %0.01
    assert received == pytest.approx(0.02)
    assert paid == -received


def test_negative_funding_reverses_the_direction() -> None:
    assert funding_amount(direction="long", qty=2.0, price=100.0, rate=-0.0001) == pytest.approx(0.02)
    assert funding_amount(direction="short", qty=2.0, price=100.0, rate=-0.0001) == pytest.approx(-0.02)


def test_accrual_direction_matches_position_side() -> None:
    positions = [
        _Position(SYMBOL, "long", 2.0, pd.Timestamp("2026-01-01 00:00", tz="UTC")),
        _Position(SYMBOL, "short", 2.0, pd.Timestamp("2026-01-01 00:00", tz="UTC")),
    ]
    bar_open = pd.Timestamp("2026-01-01 08:00", tz="UTC")
    charges = accrue(
        positions,
        bar_open=bar_open,
        bar_close=bar_open + BAR,
        prices={SYMBOL: 100.0},
        funding={SYMBOL: _series(("2026-01-01 08:00", 0.0001))},
        interval_hours=8,
    )
    by_direction = {charge.direction: charge for charge in charges}
    assert by_direction["long"].amount == pytest.approx(-0.02)
    assert by_direction["short"].amount == pytest.approx(0.02)
    assert by_direction["long"].notional == pytest.approx(200.0)


def test_portfolio_moves_funding_into_cash_with_the_same_sign() -> None:
    """Kural 2: maliyet core'da tek yerde hesaplanır, nakde core/portfolio.py işler."""
    from core.config import load_config
    from core.portfolio import Portfolio

    portfolio = Portfolio(load_config())
    ts = pd.Timestamp("2026-01-01 00:00", tz="UTC")
    portfolio.open_position(
        "m", symbol=SYMBOL, direction="long", stop_price=95.0,
        reference_price=100.0, ts=ts, marks={SYMBOL: 100.0},
    )
    before = portfolio.cash("m")
    charge = FundingCharge(
        symbol=SYMBOL, direction="long", ts=ts, rate=0.0001, notional=2000.0, amount=-0.2
    )
    assert portfolio.apply_funding("m", [charge]) == pytest.approx(-0.2)
    assert portfolio.cash("m") == pytest.approx(before - 0.2)
    assert portfolio.positions("m")[0].funding == pytest.approx(-0.2)


# --------------------------------------------------------------------------- #
# Periyot
# --------------------------------------------------------------------------- #
def test_funding_times_are_anchored_to_utc_midnight() -> None:
    start = pd.Timestamp("2026-01-01 00:00", tz="UTC")
    assert funding_times(start=start, end=start + pd.Timedelta(days=1), interval_hours=8) == [
        pd.Timestamp("2026-01-01 00:00", tz="UTC"),
        pd.Timestamp("2026-01-01 08:00", tz="UTC"),
        pd.Timestamp("2026-01-01 16:00", tz="UTC"),
    ]


def test_bars_without_a_funding_boundary_accrue_nothing() -> None:
    bar_open = pd.Timestamp("2026-01-01 04:00", tz="UTC")
    assert funding_times(start=bar_open, end=bar_open + BAR, interval_hours=8) == []


def test_position_opened_at_the_funding_instant_does_not_pay() -> None:
    """Borsanın anlık görüntüsünde henüz yok olan pozisyon o periyodu ödemez."""
    bar_open = pd.Timestamp("2026-01-01 08:00", tz="UTC")
    charges = accrue(
        [_Position(SYMBOL, "long", 1.0, bar_open)],
        bar_open=bar_open,
        bar_close=bar_open + BAR,
        prices={SYMBOL: 100.0},
        funding={SYMBOL: _series(("2026-01-01 08:00", 0.0001))},
        interval_hours=8,
    )
    assert charges == []


def test_funding_disabled_accrues_nothing() -> None:
    bar_open = pd.Timestamp("2026-01-01 08:00", tz="UTC")
    assert accrue(
        [_Position(SYMBOL, "long", 1.0, pd.Timestamp("2026-01-01 00:00", tz="UTC"))],
        bar_open=bar_open,
        bar_close=bar_open + BAR,
        prices={SYMBOL: 100.0},
        funding={SYMBOL: _series(("2026-01-01 08:00", 0.0001))},
        interval_hours=8,
        enabled=False,
    ) == []


# --------------------------------------------------------------------------- #
# Veri boşluğu: atla ve logla, uydurma
# --------------------------------------------------------------------------- #
def test_missing_rate_is_skipped_and_logged(caplog: pytest.LogCaptureFixture) -> None:
    bar_open = pd.Timestamp("2026-01-01 08:00", tz="UTC")
    with caplog.at_level(logging.WARNING, logger="core.funding"):
        charges = accrue(
            [_Position(SYMBOL, "long", 1.0, pd.Timestamp("2026-01-01 00:00", tz="UTC"))],
            bar_open=bar_open,
            bar_close=bar_open + BAR,
            prices={SYMBOL: 100.0},
            funding={SYMBOL: _series(("2026-01-01 00:00", 0.0001))},  # 08:00 kaydı yok
            interval_hours=8,
        )
    assert charges == []
    assert "funding kaydı yok" in caplog.text


def test_rate_is_never_forward_filled() -> None:
    series = _series(("2026-01-01 00:00", 0.0003))
    assert rate_at(series, pd.Timestamp("2026-01-01 00:00", tz="UTC")) == pytest.approx(0.0003)
    assert rate_at(series, pd.Timestamp("2026-01-01 08:00", tz="UTC")) is None
    assert rate_at(None, pd.Timestamp("2026-01-01 00:00", tz="UTC")) is None


def test_missing_price_is_skipped_and_logged(caplog: pytest.LogCaptureFixture) -> None:
    bar_open = pd.Timestamp("2026-01-01 08:00", tz="UTC")
    with caplog.at_level(logging.WARNING, logger="core.funding"):
        charges = accrue(
            [_Position(SYMBOL, "long", 1.0, pd.Timestamp("2026-01-01 00:00", tz="UTC"))],
            bar_open=bar_open,
            bar_close=bar_open + BAR,
            prices={},
            funding={SYMBOL: _series(("2026-01-01 08:00", 0.0001))},
            interval_hours=8,
        )
    assert charges == []
    assert "fiyat yok" in caplog.text


def test_invalid_interval_is_a_programming_error() -> None:
    start = pd.Timestamp("2026-01-01 00:00", tz="UTC")
    with pytest.raises(ValueError, match="interval_hours"):
        funding_times(start=start, end=start + BAR, interval_hours=0)
