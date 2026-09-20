"""core/indicators.py: göstergelerin TEK uygulaması beklenen sayıyı veriyor mu.

Bu testlerin ölçüm açısından değeri, "doğru formül" doğrulamasından çok TANIMI SABİTLEMEKTİR:
bir gösterge sessizce değişirse (Wilder'a geçiş, Donchian'ın son barı içermeye başlaması,
ddof=1'e kayma) o barda üretilen tüm sinyaller değişir ve geçmiş turlarla kıyas geçersizleşir.
Elle hesaplanmış beklenen değerler bu kaymayı görünür kılar.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from core.indicators import (
    anchored_vwap,
    average_true_range,
    bars_until,
    bollinger,
    donchian,
    ema,
    fib_levels,
    rsi,
    sma,
    typical_price,
    zigzag_pivots,
)

START = pd.Timestamp("2026-01-01 00:00:00", tz="UTC")


def _index(count: int) -> pd.DatetimeIndex:
    return pd.date_range(START, periods=count, freq="4h", tz="UTC", name="ts")


def _series(values: list[float]) -> pd.Series:
    return pd.Series(values, index=_index(len(values)), dtype="float64")


def _frame(rows: list[tuple[float, float, float]]) -> pd.DataFrame:
    """(high, low, close) üçlüleri; open/volume göstergeler için kullanılmaz."""
    return pd.DataFrame(
        {
            "open": [close for _, _, close in rows],
            "high": [high for high, _, _ in rows],
            "low": [low for _, low, _ in rows],
            "close": [close for _, _, close in rows],
            "volume": [1.0] * len(rows),
        },
        index=_index(len(rows)),
    )


# --------------------------------------------------------------------------- #
# bars_until: look-ahead kesmesi (kural 12)
# --------------------------------------------------------------------------- #
def test_bars_until_drops_bars_after_as_of() -> None:
    frame = _frame([(1.0, 1.0, 1.0)] * 5)
    as_of = frame.index[2]
    sliced = bars_until(frame, as_of)
    assert list(sliced.index) == list(frame.index[:3])


def test_bars_until_keeps_the_as_of_bar_itself() -> None:
    """as_of barı KAPANMIŞTIR: onu da atmak modeli bir bar geriden karar verir hâle getirir."""
    frame = _frame([(1.0, 1.0, 1.0)] * 5)
    assert bars_until(frame, frame.index[-1]).index[-1] == frame.index[-1]


# --------------------------------------------------------------------------- #
# sma / ema
# --------------------------------------------------------------------------- #
def test_sma_uses_only_the_last_period_values() -> None:
    assert sma(_series([1.0, 2.0, 3.0, 100.0, 200.0]), 2) == pytest.approx(150.0)


def test_sma_returns_none_when_history_is_short() -> None:
    assert sma(_series([1.0, 2.0]), 3) is None


def test_ema_is_seeded_with_the_first_window_sma() -> None:
    """Tohum açıkça SMA'dır: ilk `period` barda EMA = SMA olmalı."""
    assert ema(_series([10.0, 20.0, 30.0]), 3) == pytest.approx(20.0)


def test_ema_recursion_matches_the_written_definition() -> None:
    values = [10.0, 20.0, 30.0, 40.0]
    alpha = 2.0 / 4.0
    expected = alpha * 40.0 + (1.0 - alpha) * 20.0
    assert ema(_series(values), 3) == pytest.approx(expected)


def test_ema_returns_none_below_its_period() -> None:
    assert ema(_series([1.0] * 199), 200) is None


# --------------------------------------------------------------------------- #
# rsi
# --------------------------------------------------------------------------- #
def test_rsi_is_100_when_every_change_is_a_gain() -> None:
    assert rsi(_series([1.0, 2.0, 3.0, 4.0]), 3) == pytest.approx(100.0)


def test_rsi_is_zero_when_every_change_is_a_loss() -> None:
    assert rsi(_series([4.0, 3.0, 2.0, 1.0]), 3) == pytest.approx(0.0)


def test_flat_series_is_neutral_not_overbought() -> None:
    """Hareketsiz seride 100 demek, hiç hareket etmemiş fiyatı 'azami aşırı alım' ilan etmektir."""
    assert rsi(_series([5.0] * 10), 3) == pytest.approx(50.0)


def test_rsi_matches_hand_computed_average_gain_loss() -> None:
    # Değişimler: +2, -1, +2, -1 -> ort. kazanç 1.0, ort. kayıp 0.5 -> RS=2 -> RSI=100-100/3
    strength = rsi(_series([10.0, 12.0, 11.0, 13.0, 12.0]), 4)
    assert strength == pytest.approx(100.0 - 100.0 / 3.0)


def test_rsi_needs_one_bar_more_than_its_period() -> None:
    assert rsi(_series([1.0] * 14), 14) is None
    assert rsi(_series([1.0] * 15), 14) is not None


# --------------------------------------------------------------------------- #
# bollinger
# --------------------------------------------------------------------------- #
def test_bollinger_bands_are_symmetric_around_the_sma() -> None:
    bands = bollinger(_series([1.0, 2.0, 3.0, 4.0, 5.0]), 5, 2.0)
    assert bands is not None
    assert bands.middle == pytest.approx(3.0)
    assert bands.upper - bands.middle == pytest.approx(bands.middle - bands.lower)


def test_bollinger_uses_population_standard_deviation() -> None:
    """ddof=1'e kayma 20 barlık pencerede bandı ~%2.6 genişletir: tanım sabitlensin."""
    values = [1.0, 2.0, 3.0, 4.0, 5.0]
    bands = bollinger(_series(values), 5, 2.0)
    assert bands is not None
    expected = 3.0 + 2.0 * float(np.std(values, ddof=0))
    assert bands.upper == pytest.approx(expected)


def test_bollinger_returns_none_when_history_is_short() -> None:
    assert bollinger(_series([1.0] * 19), 20, 2.0) is None


# --------------------------------------------------------------------------- #
# donchian
# --------------------------------------------------------------------------- #
def test_donchian_excludes_the_evaluated_bar() -> None:
    """Kanal son barı da içerseydi 'tepenin üstüne kapanış' pratikte hiç oluşmazdı."""
    rows = [(10.0, 8.0, 9.0)] * 3 + [(99.0, 1.0, 50.0)]
    channel = donchian(_frame(rows), 3)
    assert channel is not None
    assert channel.upper == pytest.approx(10.0)
    assert channel.lower == pytest.approx(8.0)


def test_donchian_needs_one_bar_more_than_its_period() -> None:
    rows = [(10.0, 8.0, 9.0)] * 20
    assert donchian(_frame(rows), 20) is None
    assert donchian(_frame(rows + [(11.0, 7.0, 10.0)]), 20) is not None


# --------------------------------------------------------------------------- #
# average_true_range: motordan taşındı, davranışı aynı kalmalı
# --------------------------------------------------------------------------- #
def test_atr_averages_the_true_ranges() -> None:
    rows = [(10.0, 8.0, 9.0), (12.0, 9.0, 11.0), (13.0, 10.0, 12.0)]
    # TR: max(12-9, |12-9|, |9-9|)=3 ve max(13-10, |13-11|, |10-11|)=3
    assert average_true_range(_frame(rows), 2) == pytest.approx(3.0)


def test_atr_returns_none_when_history_is_short() -> None:
    assert average_true_range(_frame([(10.0, 8.0, 9.0)] * 3), 3) is None


@pytest.mark.parametrize("period", [0, -1])
def test_indicators_reject_non_positive_periods(period: int) -> None:
    """Sıfır periyot sessizce nan/boş pencere üretirse, o sayı sinyale dönüşür."""
    with pytest.raises(ValueError):
        average_true_range(_frame([(10.0, 8.0, 9.0)] * 5), period)
    with pytest.raises(ValueError):
        rsi(_series([1.0] * 5), period)
    with pytest.raises(ValueError):
        ema(_series([1.0] * 5), period)
    with pytest.raises(ValueError):
        donchian(_frame([(10.0, 8.0, 9.0)] * 5), period)


def test_engine_reexports_the_same_atr_implementation() -> None:
    """İki ATR uygulaması, trailing mesafesi ile stratejinin stop'unun ayrışması demekti."""
    from core import engine

    assert engine.average_true_range is average_true_range


# --------------------------------------------------------------------------- #
# Zigzag pivotları — kaynak tarayıcıdan birebir taşınan tanım
#
# Bu testlerin işlevi doğruluk değil KİMLİK denetimidir: pivot tanımı sessizce
# kayarsa (eşik karşılaştırması, canlı ucun atılması, çift yerine tek pivot silme)
# `confluence` modelinin ölçtüğü şey kaynak tarayıcıyla aynı olmaktan çıkar ve
# bunu hiçbir metrik göstermez.
# --------------------------------------------------------------------------- #


def _swing(closes: list[float], *, spread: float = 0.0) -> pd.DataFrame:
    return _frame([(close + spread, close - spread, close) for close in closes])


def test_pivots_alternate_between_lows_and_highs() -> None:
    """Alternans bozulursa bir "bacak" iki dip arasına düşer ve Fibonacci yönü anlamsızlaşır."""
    closes = [100.0] + [100.0 + i for i in range(1, 21)] + [120.0 - i for i in range(1, 21)]
    pivots = zigzag_pivots(_swing(closes), pct_threshold=0.05, min_leg_bars=0)
    kinds = [pivot.kind for pivot in pivots]
    assert kinds == ["low", "high", "low"]
    assert pivots[1].price == pytest.approx(120.0)


def test_a_move_below_the_threshold_does_not_create_a_pivot() -> None:
    """Eşik düşerse gürültü pivot sanılır; her gürültü bacağı bir "bağımsız swing" olurdu."""
    closes = [100.0 + i for i in range(21)] + [120.0 - 0.2 * i for i in range(1, 11)]
    pivots = zigzag_pivots(_swing(closes), pct_threshold=0.05, min_leg_bars=0)
    assert [pivot.kind for pivot in pivots] == ["low", "high"]


def test_the_live_extreme_is_reported_as_the_last_pivot() -> None:
    """Canlı uç atılırsa swing'in güncel ucu kaybolur; kaynaktaki davranış korunmalı."""
    closes = [100.0 + i for i in range(21)]
    pivots = zigzag_pivots(_swing(closes), pct_threshold=0.05, min_leg_bars=0)
    assert pivots[-1].kind == "high"
    assert pivots[-1].price == pytest.approx(120.0)


def test_the_series_start_is_anchored_with_the_opposite_kind() -> None:
    """Ankraj olmadan ilk swing (başlangıç -> ilk pivot) hiç aday olamazdı."""
    closes = [100.0 + i for i in range(21)]
    pivots = zigzag_pivots(_swing(closes), pct_threshold=0.05, min_leg_bars=0)
    assert pivots[0].kind == "low"
    assert pivots[0].price == pytest.approx(100.0)
    assert pivots[0].time == _index(len(closes))[0]


def test_short_legs_are_dropped_in_pairs_so_alternation_survives() -> None:
    """Tek pivot silmek dip/zirve sırasını bozardı; kaynak bu yüzden çift siler."""
    closes = (
        [100.0 + i for i in range(21)]  # uzun yükseliş
        + [120.0 - 2.0 * i for i in range(1, 5)]  # 4 barlık sert düşüş (kısa bacak)
        + [112.0 + 2.0 * i for i in range(1, 5)]  # 4 barlık sert dönüş (kısa bacak)
        + [120.0 + i for i in range(1, 11)]
    )
    frame = _swing(closes)
    unmerged = zigzag_pivots(frame, pct_threshold=0.05, min_leg_bars=0)
    merged = zigzag_pivots(frame, pct_threshold=0.05, min_leg_bars=8)
    assert len(unmerged) - len(merged) == 2
    kinds = [pivot.kind for pivot in merged]
    assert all(first != second for first, second in zip(kinds, kinds[1:]))


def test_a_frame_shorter_than_three_bars_has_no_pivots() -> None:
    assert zigzag_pivots(_swing([100.0, 101.0]), pct_threshold=0.05, min_leg_bars=8) == []


def test_zigzag_rejects_a_non_positive_threshold() -> None:
    """Sıfır eşik her barı pivot yapar: "bağımsız swing" kavramı tümden kaybolurdu."""
    with pytest.raises(ValueError):
        zigzag_pivots(_swing([100.0] * 5), pct_threshold=0.0, min_leg_bars=8)


def test_fib_levels_project_downward_from_a_leg_that_started_at_a_low() -> None:
    """Yön A'nın tipinden okunur: dipten başlayan bacakta seviyeler B'den AŞAĞI iner."""
    levels = fib_levels(a_price=100.0, b_price=200.0, a_kind="low", ratios=(0.618, 1.272))
    assert levels[0.618] == pytest.approx(138.2)
    assert levels[1.272] == pytest.approx(72.8)


def test_fib_levels_project_upward_from_a_leg_that_started_at_a_high() -> None:
    levels = fib_levels(a_price=200.0, b_price=100.0, a_kind="high", ratios=(0.618, 1.272))
    assert levels[0.618] == pytest.approx(161.8)
    assert levels[1.272] == pytest.approx(227.2)


# --------------------------------------------------------------------------- #
# Çapalı VWAP
# --------------------------------------------------------------------------- #
def _volume_frame(
    rows: list[tuple[float, float, float]], volumes: list[float]
) -> pd.DataFrame:
    frame = _frame(rows)
    frame["volume"] = volumes
    return frame


def test_typical_price_is_the_middle_of_the_bar_not_the_close() -> None:
    """VWAP'ın iddiası "bu hacim hangi fiyattan el değiştirdi"; kapanış barın aralığını atar."""
    frame = _volume_frame([(12.0, 6.0, 9.0)], [1.0])
    assert float(typical_price(frame).iloc[0]) == pytest.approx(9.0)


def test_anchored_vwap_weights_by_volume_not_by_bar_count() -> None:
    """Ağırlık hacimdedir: iki katı hacimli bar ortalamayı iki kat çeker."""
    frame = _volume_frame([(10.0, 10.0, 10.0), (20.0, 20.0, 20.0)], [1.0, 3.0])
    result = anchored_vwap(frame, anchor=frame.index[0])
    assert result is not None
    assert result.value == pytest.approx(17.5)
    assert result.bars == 2


def test_anchored_vwap_starts_at_the_anchor_bar_inclusive() -> None:
    """Çapa barı DÂHİLDİR: pivotun kendi barını atmak çizgiyi çapadan koparırdı."""
    frame = _volume_frame(
        [(10.0, 10.0, 10.0), (20.0, 20.0, 20.0), (30.0, 30.0, 30.0)], [1.0, 1.0, 1.0]
    )
    anchored = anchored_vwap(frame, anchor=frame.index[1])
    assert anchored is not None
    assert anchored.value == pytest.approx(25.0)
    assert anchored.bars == 2


def test_anchored_deviation_is_volume_weighted_and_population() -> None:
    """ddof=0 ve aynı hacim ağırlıkları: "2σ" bu depoda tek bir şey ifade etmeli."""
    frame = _volume_frame([(10.0, 10.0, 10.0), (20.0, 20.0, 20.0)], [3.0, 1.0])
    result = anchored_vwap(frame, anchor=frame.index[0])
    assert result is not None
    assert result.value == pytest.approx(12.5)
    expected = np.sqrt((3.0 * 2.5**2 + 1.0 * 7.5**2) / 4.0)
    assert result.deviation == pytest.approx(float(expected))


def test_a_zero_volume_window_has_no_anchored_vwap() -> None:
    """Eşit ağırlığa düşmek, göstergeyi sessizce basit ortalamaya çevirirdi."""
    frame = _volume_frame([(10.0, 10.0, 10.0), (20.0, 20.0, 20.0)], [0.0, 0.0])
    assert anchored_vwap(frame, anchor=frame.index[0]) is None


def test_an_anchor_outside_the_frame_has_no_anchored_vwap() -> None:
    frame = _volume_frame([(10.0, 10.0, 10.0)], [1.0])
    assert anchored_vwap(frame, anchor=START - pd.Timedelta("4h")) is None
