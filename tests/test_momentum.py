"""strategies/momentum.py: kesitsel momentum, haftalık dengeleme.

Modelin ölçüm değeri üç şeye bağlı: (a) sıralama gerçekten 7 günlük pencereden gelmeli —
pencere kayarsa model başka bir tezi ölçer; (b) dengeleme yalnızca Pazartesi 00:00 UTC
barında olmalı — her turda dengeleyen bir model momentumu değil komisyonu ölçer; (c) uçlar
(ilk 5 / son 5) örtüşmemeli. Testler bu üçünü, stop mesafesinin maliyet ölçeğini (kural 14)
ve sinyalin doğrulama kapısından geçtiğini sabitler.
"""

from __future__ import annotations

import logging

import pandas as pd
import pytest

from core.indicators import average_true_range
from core.validate import validate_signal
from strategies.base import MarketData, Signal
from strategies.momentum import Momentum
from tests.helpers_market import frame

BARS = 100
LOOKBACK_BARS = 42  # 7 gün / 4h
BASE = 100.0
MONDAY = pd.Timestamp("2026-03-02 00:00:00", tz="UTC")  # Pazartesi 00:00 UTC
FIRST_BAR = MONDAY - pd.Timedelta(hours=4) * (BARS - 1)

# SYM00 en güçlü, SYM11 en zayıf: ilk 5 long, son 5 short, ortadaki 2 sembol dışarıda kalmalı.
RETURNS = [0.30, 0.25, 0.20, 0.15, 0.10, 0.05, 0.02, -0.02, -0.05, -0.10, -0.20, -0.30]
SYMBOLS = [f"SYM{index:02d}-USDT-SWAP" for index in range(len(RETURNS))]


def _closes(lookback_return: float) -> list[float]:
    """Son 42 barda `lookback_return` kadar yol alan, öncesinde yatay seyreden kapanışlar."""
    closes = [BASE] * BARS
    target = BASE * (1.0 + lookback_return)
    for step in range(1, LOOKBACK_BARS + 1):
        closes[-LOOKBACK_BARS - 1 + step] = BASE + (target - BASE) * step / LOOKBACK_BARS
    return closes


def _universe(
    returns: list[float] | None = None, *, as_of: pd.Timestamp = MONDAY
) -> MarketData:
    values = RETURNS if returns is None else returns
    frames = {
        f"SYM{index:02d}-USDT-SWAP": frame(_closes(value), start=FIRST_BAR)
        for index, value in enumerate(values)
    }
    return MarketData(ohlcv=frames, btc=next(iter(frames.values())), funding={}, as_of=as_of)


def _signals(market: MarketData | None = None) -> list[Signal]:
    return Momentum().generate_signals(market if market is not None else _universe())


def _by_direction(signals: list[Signal], direction: str) -> list[str]:
    return [signal.symbol for signal in signals if signal.direction == direction]


# --------------------------------------------------------------------------- #
# Sözleşme
# --------------------------------------------------------------------------- #
def test_is_a_competitor_not_a_benchmark() -> None:
    """Yarışmacı model kendi boyutunu belirleyemez (kural 3/11/15)."""
    strategy = Momentum()
    assert strategy.name == "momentum"
    assert strategy.is_benchmark is False
    assert strategy.is_meta is False
    assert strategy.allowed_directions == ["long", "short"]


def test_registry_resolves_the_model_name() -> None:
    from strategies.registry import build

    assert isinstance(build("momentum"), Momentum)


def test_model_is_retired_but_still_buildable() -> None:
    """Karar 33: canlı listeden ÇIKARILDI, koddan çıkarılmadı.

    Emeklilik ölçütü performans değil ÖLÇÜLEBİLİRLİKTİR: 29 barda HİÇ sinyal üretmedi,
    yani ne kadar iyi olduğu asla öğrenilemezdi ve tabloda yalnızca gürültü üretiyordu.

    Test iki şeyi birden çiviler: modelin canlı listede OLMADIĞINI (kazara geri dönmesi
    sessiz kalmasın) ve hâlâ KURULABİLDİĞİNİ — defteri ve kodu duruyor (kural 1), listeye
    geri eklemek bir commit. "Emekli" ile "silinmiş" aynı şey değildir.
    """
    from core.config import get_setting, load_config
    from strategies.registry import REGISTRY

    assert "momentum" not in get_setting(load_config(), "models")
    assert "momentum" in REGISTRY


def test_every_signal_uses_the_shared_risk_sizing() -> None:
    for signal in _signals():
        assert signal.sizing == "risk"
        assert signal.notional_fraction is None
        assert signal.stop_price is not None


# --------------------------------------------------------------------------- #
# Dengeleme barı
# --------------------------------------------------------------------------- #
def test_monday_bar_rebalances_the_whole_book() -> None:
    signals = _signals()
    assert len(signals) == 10
    assert _by_direction(signals, "long") == SYMBOLS[:5]
    assert _by_direction(signals, "short") == SYMBOLS[-5:]


@pytest.mark.parametrize(
    "as_of",
    [
        MONDAY - pd.Timedelta(hours=4),  # Pazar 20:00
        MONDAY + pd.Timedelta(hours=4),  # Pazartesi 04:00
        MONDAY + pd.Timedelta(days=1),  # Salı 00:00
    ],
)
def test_no_signal_outside_the_monday_midnight_bar(
    as_of: pd.Timestamp, caplog: pytest.LogCaptureFixture
) -> None:
    """Her turda dengeleyen bir model momentumu değil komisyon+kaymayı ölçerdi."""
    market = _universe(as_of=as_of)
    # Sembollerin son barı `as_of` olmalı ki testin tek değişkeni TAKVİM olsun.
    trimmed = MarketData(
        ohlcv={symbol: candles.loc[:as_of] for symbol, candles in market.ohlcv.items()},
        btc=market.btc.loc[:as_of],
        funding={},
        as_of=as_of,
    )
    with caplog.at_level(logging.INFO, logger="strategies.momentum"):
        assert Momentum().generate_signals(trimmed) == []
    assert any("dengeleme barı değil" in record.getMessage() for record in caplog.records)


def test_the_same_universe_produces_signals_once_the_monday_bar_arrives() -> None:
    """Tek değişken takvim olmalı: veri birebir aynı, sonuç farklı."""
    sunday = MONDAY - pd.Timedelta(hours=4)
    market = _universe()
    earlier = MarketData(
        ohlcv={symbol: candles.loc[:sunday] for symbol, candles in market.ohlcv.items()},
        btc=market.btc.loc[:sunday],
        funding={},
        as_of=sunday,
    )
    assert Momentum().generate_signals(earlier) == []
    assert len(Momentum().generate_signals(market)) == 10


# --------------------------------------------------------------------------- #
# Sıralama
# --------------------------------------------------------------------------- #
def test_middle_of_the_ranking_is_left_alone() -> None:
    """Model mutlak yön iddiası taşımaz; ölçtüğü şey yalnızca sıralamanın UÇLARIDIR."""
    traded = {signal.symbol for signal in _signals()}
    assert SYMBOLS[5] not in traded and SYMBOLS[6] not in traded


def test_ranking_reads_the_return_from_the_bar_seven_days_back() -> None:
    """Pencere kayarsa model başka bir tezi ölçer: 42 barın DIŞINDAKİ hareket sıralamayı değiştirmemeli."""
    market = _universe()
    tweaked = dict(market.ohlcv)
    victim = SYMBOLS[8]  # short tarafında, ortalama bir sembol
    candles = tweaked[victim].copy()
    older = candles.index[-(LOOKBACK_BARS + 2)]
    candles.loc[older, ["open", "close"]] = BASE * 3.0
    candles.loc[older, "high"] = BASE * 3.0 + 0.5
    candles.loc[older, "low"] = BASE * 3.0 - 0.5
    tweaked[victim] = candles

    shifted = MarketData(
        ohlcv=tweaked, btc=market.btc, funding={}, as_of=market.as_of
    )
    assert _by_direction(Momentum().generate_signals(shifted), "short") == SYMBOLS[-5:]


def test_a_move_inside_the_window_moves_the_symbol_across_the_ranking() -> None:
    returns = list(RETURNS)
    returns[11] = 0.50  # en zayıf sembol en güçlüsü olur
    signals = _signals(_universe(returns))
    assert _by_direction(signals, "long")[0] == SYMBOLS[11]
    assert SYMBOLS[11] not in _by_direction(signals, "short")


def test_symbols_without_a_full_lookback_window_are_not_ranked() -> None:
    """Kısmi pencereyle hesaplanmış getiri, farklı uzunlukta iki getiriyi yarıştırmak olurdu."""
    market = _universe()
    short_history = {
        symbol: (candles.iloc[-(LOOKBACK_BARS - 5):] if symbol == SYMBOLS[0] else candles)
        for symbol, candles in market.ohlcv.items()
    }
    signals = Momentum().generate_signals(
        MarketData(ohlcv=short_history, btc=market.btc, funding={}, as_of=market.as_of)
    )
    assert SYMBOLS[0] not in {signal.symbol for signal in signals}
    assert _by_direction(signals, "long") == SYMBOLS[1:6]


def test_a_symbol_that_is_missing_the_as_of_bar_is_not_ranked() -> None:
    """Farklı tarihli iki getiriyi aynı kolonda yarıştırmak kural 5/12'yi sayısal düzeyde deler."""
    market = _universe()
    stale = dict(market.ohlcv)
    stale[SYMBOLS[0]] = stale[SYMBOLS[0]].iloc[:-1]
    signals = Momentum().generate_signals(
        MarketData(ohlcv=stale, btc=market.btc, funding={}, as_of=market.as_of)
    )
    assert SYMBOLS[0] not in {signal.symbol for signal in signals}


def test_a_universe_too_small_for_two_distinct_ends_produces_nothing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Uçlar örtüşürse aynı sembol hem long hem short listesine girerdi."""
    with caplog.at_level(logging.INFO, logger="strategies.momentum"):
        signals = _signals(_universe(RETURNS[:8]))
    assert signals == []
    assert any("örtüşmemesi" in record.getMessage() for record in caplog.records)


def test_ties_are_broken_by_symbol_name_so_runs_repeat() -> None:
    """Eşit getiride sıra sözlük/veri katmanı sırasına bırakılırsa koşu tekrarlanabilir olmaz."""
    returns = [0.10] * 12
    first = _signals(_universe(returns))
    second = _signals(_universe(returns))
    assert _by_direction(first, "long") == SYMBOLS[:5]
    assert [signal.symbol for signal in first] == [signal.symbol for signal in second]


# --------------------------------------------------------------------------- #
# Stop ve gerekçe
# --------------------------------------------------------------------------- #
def test_long_stop_sits_two_and_a_half_atr_below_the_close() -> None:
    market = _universe()
    signal = next(signal for signal in _signals(market) if signal.direction == "long")
    candles = market.ohlcv[signal.symbol]
    atr = average_true_range(candles, 14)
    assert atr is not None
    assert signal.stop_price == pytest.approx(float(candles["close"].iloc[-1]) - 2.5 * atr)


def test_short_stop_sits_two_and_a_half_atr_above_the_close() -> None:
    market = _universe()
    signal = next(signal for signal in _signals(market) if signal.direction == "short")
    candles = market.ohlcv[signal.symbol]
    atr = average_true_range(candles, 14)
    assert atr is not None
    assert signal.stop_price == pytest.approx(float(candles["close"].iloc[-1]) + 2.5 * atr)


def test_stop_distance_stays_under_the_shared_ceiling() -> None:
    """2.5×ATR bandın üst ucu ama tavanın (3.0×) altı: model tavan yüzünden işlem kaybetmemeli."""
    from core.config import get_setting, load_config

    ceiling = float(get_setting(load_config(), "max_stop_atr_multiple"))
    market = _universe()
    for signal in _signals(market):
        candles = market.ohlcv[signal.symbol]
        atr = average_true_range(candles, 14)
        assert atr is not None and signal.stop_price is not None
        distance = abs(float(candles["close"].iloc[-1]) - signal.stop_price)
        assert distance / atr <= ceiling


def test_model_asks_for_no_trailing_stop_and_no_take_profit() -> None:
    """Tez haftalık sıralamadır; trailing ve TP ayrı tezlerdir, bu modele karıştırılmaz."""
    for signal in _signals():
        assert signal.trailing_atr is None
        assert signal.take_profits == ()


def test_signals_pass_the_validation_gate() -> None:
    market = _universe()
    for signal in _signals(market):
        validate_signal(
            signal,
            entry_price=float(market.ohlcv[signal.symbol]["close"].iloc[-1]),
            allowed_directions=Momentum().allowed_directions,
            symbol_universe=list(market.ohlcv),
            is_benchmark=False,
        )


def test_reason_carries_the_measurement_next_to_the_threshold() -> None:
    """Eşiksiz bir gerekçe, denetimi commit geçmişinde eşik aramaya zorlardı."""
    signals = _signals()
    long_reason = next(signal.reason for signal in signals if signal.direction == "long")
    assert "7g getiri %+30.00" in long_reason
    assert f"({LOOKBACK_BARS} bar)" in long_reason
    assert "sıra 1/12" in long_reason
    assert "ilk 5 eşiği %+10.00" in long_reason
    assert "ATR(14)" in long_reason
    assert "Pazartesi 00:00 UTC" in long_reason

    short_reason = next(signal.reason for signal in signals if signal.direction == "short")
    assert "son 5 eşiği %-2.00" in short_reason
    assert "sıra 12/12" in signals[-1].reason
