"""Beş ortak kol: tetikleme koşulları, hedef geometrisi ve kol izolasyonu.

Kollar iki modelin de gördüğü tek kopyadır; bu dosya onların NE ZAMAN kurulum ürettiğini
ölçer. Kapılar (stop tabanı, 1.5R) burada değil `tests/test_scalp_model.py`'dedir — kol
kurulum önerir, modelin kapısı onu eler.
"""

from __future__ import annotations

from typing import Sequence

import pandas as pd
import pytest

from strategies.base import MarketData
from strategies.scalp.arms import (
    ARM_NAMES,
    ArmParams,
    ArmSetup,
    funding_spike_fade,
    momentum_burst,
    opening_range_breakout,
    propose_all,
    rsi2_reversal,
    symbol_views,
    vwap_pullback,
)
from tests.helpers_market import frame, market

SYMBOL = "BTC-USDT-SWAP"
DAY = pd.Timestamp("2026-03-02 00:00:00", tz="UTC")
PARAMS = ArmParams(atr_period=14, stop_atr_multiple=5.0, target_reward_risk=2.0)


def _views(data: MarketData) -> Sequence[object]:
    return symbol_views(data, atr_period=PARAMS.atr_period)


def _market_from(
    closes: Sequence[float],
    *,
    highs: Sequence[float] | None = None,
    lows: Sequence[float] | None = None,
    volumes: Sequence[float] | None = None,
    funding: pd.Series | None = None,
) -> MarketData:
    data = frame(closes, spread=0.3, start=DAY, freq="15min", highs=highs, lows=lows, volumes=volumes)
    return market(
        {SYMBOL: data},
        funding={SYMBOL: funding} if funding is not None else None,
    )


# --------------------------------------------------------------------------- #
# Ortak geometri
# --------------------------------------------------------------------------- #
def test_every_arm_uses_the_same_stop_distance() -> None:
    """Kollar stop mesafesinde AYRIŞAMAZ: ayrışsalardı kol tablosu bir maliyet tablosu olurdu."""
    closes = [100.0 + (i % 7) * 0.3 for i in range(80)]
    data = _market_from(closes)
    views = _views(data)
    assert views, "sembol görünümü kurulamadı"
    atr = views[0].atr  # type: ignore[attr-defined]

    setups = [
        setup
        for arm in (vwap_pullback, opening_range_breakout, rsi2_reversal, momentum_burst)
        for setup in arm(views, PARAMS)  # type: ignore[arg-type]
    ]

    for setup in setups:
        assert setup.stop_distance == pytest.approx(atr * PARAMS.stop_atr_multiple)


def test_target_never_exceeds_the_projection() -> None:
    """Hedef, projeksiyon ile yapısal engelin YAKIN olanıdır: projeksiyonu aşamaz."""
    closes = [100.0 + (i % 5) * 0.4 for i in range(80)]
    data = _market_from(closes)
    views = _views(data)

    for arm_name in ARM_NAMES:
        for setup in propose_all(data, PARAMS)[arm_name]:
            cap = setup.stop_distance * PARAMS.target_reward_risk
            assert abs(setup.target_price - setup.entry_price) <= cap + 1e-9


def test_target_is_always_on_the_correct_side() -> None:
    """Yanlış taraftaki hedef bir piyasa durumu değil, core/validate.py'de programlama hatasıdır."""
    closes = [100.0 + (i % 11) * 0.25 for i in range(120)]
    data = _market_from(closes)

    for setups in propose_all(data, PARAMS).values():
        for setup in setups:
            if setup.direction == "long":
                assert setup.target_price > setup.entry_price > setup.stop_price
            else:
                assert setup.target_price < setup.entry_price < setup.stop_price


# --------------------------------------------------------------------------- #
# 1) VWAP geri çekilme
# --------------------------------------------------------------------------- #
def test_vwap_pullback_needs_a_touch() -> None:
    """VWAP'e dokunmayan bar bir geri çekilme değildir."""
    closes = [100.0 + 0.2 * i for i in range(80)]
    # Son bar VWAP'in çok yukarısında ve aralığı VWAP'e uzak: dokunuş yok.
    data = _market_from(closes)

    assert vwap_pullback(_views(data), PARAMS) == []  # type: ignore[arg-type]


def test_vwap_pullback_fires_in_trend_direction() -> None:
    """Yükseliş trendinde VWAP'e dokunup üstünde kapanan bar LONG kurulumdur."""
    closes = [100.0 + 0.25 * i for i in range(79)] + [118.0]
    lows = [close - 0.3 for close in closes[:-1]] + [105.0]  # son bar VWAP'e kadar iniyor
    highs = [close + 0.3 for close in closes]
    data = _market_from(closes, highs=highs, lows=lows)

    setups = vwap_pullback(_views(data), PARAMS)  # type: ignore[arg-type]

    assert [setup.direction for setup in setups] == ["long"]
    assert setups[0].arm == "vwap_pullback"


# --------------------------------------------------------------------------- #
# 2) Açılış aralığı kırılımı
# --------------------------------------------------------------------------- #
def test_opening_range_breakout_requires_volume() -> None:
    """Hacimsiz kırılım, aralığın içine geri düşen bir fitil olmaya en yatkın harekettir."""
    closes = [100.0] * 4 + [100.5] * 4
    flat_volume = [100.0] * 8
    data = _market_from(closes, volumes=flat_volume)

    assert opening_range_breakout(_views(data), PARAMS) == []  # type: ignore[arg-type]


def test_opening_range_breakout_fires_with_volume() -> None:
    # 20 bar: hacim SMA'sı (20 bar) hesaplanabilsin ve kırılım günün ilk 16 barında kalsın.
    closes = [100.0] * 4 + [100.2] * 15 + [103.0]
    volumes = [100.0] * 19 + [1000.0]
    data = _market_from(closes, volumes=volumes)

    setups = opening_range_breakout(_views(data), PARAMS)  # type: ignore[arg-type]

    assert [setup.direction for setup in setups] == ["long"]


def test_opening_range_breakout_ignores_late_day_moves() -> None:
    """Aralık gün sonuna kadar geçerli sayılsaydı kol, tezinin ölçemeyeceği bir harekete girerdi."""
    closes = [100.0] * 4 + [100.2] * 40 + [103.0]
    volumes = [100.0] * 44 + [1000.0]
    data = _market_from(closes, volumes=volumes)

    assert opening_range_breakout(_views(data), PARAMS) == []  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# 3) RSI(2) aşırılık dönüşü
# --------------------------------------------------------------------------- #
def test_rsi2_reversal_stays_out_of_trending_regimes() -> None:
    """Trend içinde aynı uç değer bir dönüş değil, hareketin GÜCÜDÜR."""
    closes = [100.0 - 0.5 * i for i in range(80)]  # güçlü düşüş trendi
    data = _market_from(closes)

    assert rsi2_reversal(_views(data), PARAMS) == []  # type: ignore[arg-type]


def test_rsi2_reversal_fires_in_a_range() -> None:
    closes = [100.0 + (0.05 if i % 2 else -0.05) for i in range(78)] + [99.0, 98.5]
    data = _market_from(closes)

    setups = rsi2_reversal(_views(data), PARAMS)  # type: ignore[arg-type]

    assert [setup.direction for setup in setups] == ["long"]


# --------------------------------------------------------------------------- #
# 4) Momentum patlaması
# --------------------------------------------------------------------------- #
def test_momentum_burst_needs_three_aligned_bars() -> None:
    closes = [100.0] * 77 + [101.0, 100.5, 101.5]  # yön tutarsız
    volumes = [100.0] * 79 + [1000.0]
    data = _market_from(closes, volumes=volumes)

    assert momentum_burst(_views(data), PARAMS) == []  # type: ignore[arg-type]


def test_momentum_burst_fires_on_three_bars_with_volume() -> None:
    closes = [100.0] * 77 + [100.5, 101.0, 101.5]
    volumes = [100.0] * 79 + [1000.0]
    data = _market_from(closes, volumes=volumes)

    setups = momentum_burst(_views(data), PARAMS)  # type: ignore[arg-type]

    assert [setup.direction for setup in setups] == ["long"]


# --------------------------------------------------------------------------- #
# 5) Funding sıçraması fade'i
# --------------------------------------------------------------------------- #
def _funding(rates: Sequence[float], *, end: pd.Timestamp) -> pd.Series:
    index = pd.date_range(end=end, periods=len(rates), freq="8h", tz="UTC", name="ts")
    return pd.Series([float(rate) for rate in rates], index=index, dtype="float64")


def test_funding_spike_fade_is_short_only() -> None:
    """Simetrik hâli ("negatif funding'de long") AYRI bir tezdir ve ayrı ölçülmelidir."""
    closes = [100.0 + (i % 3) * 0.2 for i in range(80)]
    end = DAY + pd.Timedelta(minutes=15 * 79)
    data = _market_from(closes, funding=_funding([0.0001] * 9 + [0.0012], end=end))

    setups = funding_spike_fade(_views(data), PARAMS)  # type: ignore[arg-type]

    assert all(setup.direction == "short" for setup in setups)


def test_funding_spike_fade_ignores_stale_spikes() -> None:
    """Funding 8 saatte bir yayınlanır, bar 15 dakikalıktır: taze olmayan sıçrama oynanmaz.

    Kapı olmasaydı aynı olay 32 tur boyunca "yeni" sayılır ve kol tek bir olayı onlarca
    kez oynardı.
    """
    closes = [100.0 + (i % 3) * 0.2 for i in range(80)]
    end = DAY + pd.Timedelta(minutes=15 * 79) - pd.Timedelta(hours=5)
    data = _market_from(closes, funding=_funding([0.0001] * 9 + [0.0012], end=end))

    assert funding_spike_fade(_views(data), PARAMS) == []  # type: ignore[arg-type]


def test_funding_spike_fade_ignores_small_rates() -> None:
    """Tabanın altındaki "sıçrama" gürültüdür: oran 2 katına çıksa bile."""
    closes = [100.0 + (i % 3) * 0.2 for i in range(80)]
    end = DAY + pd.Timedelta(minutes=15 * 79)
    data = _market_from(closes, funding=_funding([0.00001] * 9 + [0.0001], end=end))

    assert funding_spike_fade(_views(data), PARAMS) == []  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Kol izolasyonu
# --------------------------------------------------------------------------- #
def test_propose_all_returns_every_arm_even_when_empty() -> None:
    """Kol kümesi sabittir: boş kol da anahtarıyla durur, yoksa bandit onu göremezdi."""
    data = _market_from([100.0] * 80)

    assert set(propose_all(data, PARAMS)) == set(ARM_NAMES)


def test_a_broken_arm_does_not_silence_the_others(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tek bir kolun patlaması beş kolu birden susturamaz."""
    import strategies.scalp.arms as arms_module

    def _explode(views: object, params: ArmParams) -> list[ArmSetup]:
        raise RuntimeError("kol içi hata")

    monkeypatch.setitem(arms_module.ARMS, "rsi2_reversal", _explode)
    closes = [100.0] * 77 + [100.5, 101.0, 101.5]
    data = _market_from(closes, volumes=[100.0] * 79 + [1000.0])

    proposals = propose_all(data, PARAMS)

    assert proposals["rsi2_reversal"] == []
    assert proposals["momentum_burst"], "diğer kollar etkilenmemeliydi"


def test_symbol_views_are_sorted_and_respect_as_of() -> None:
    """Aday sırası çekilişin tekrarlanabilirliğinin parçasıdır (sözlük sırasına bırakılamaz)."""
    closes = [100.0 + 0.1 * i for i in range(80)]
    frames = {
        "ETH-USDT-SWAP": frame(closes, spread=0.3, start=DAY, freq="15min"),
        "BTC-USDT-SWAP": frame(closes, spread=0.3, start=DAY, freq="15min"),
    }
    data = market(frames)

    views = symbol_views(data, atr_period=14)

    assert [view.symbol for view in views] == ["BTC-USDT-SWAP", "ETH-USDT-SWAP"]
    assert all(view.frame.index[-1] == data.as_of for view in views)
