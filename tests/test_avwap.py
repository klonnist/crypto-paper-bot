"""strategies/avwap.py: çapalı VWAP sapması (long + short).

Modelin ölçüm değeri üç tanımın sabit kalmasına bağlı: çapa KESİNLEŞMİŞ pivot olmazsa
AVWAP her barda başka bir yerden başlar ve ölçülen şey "şu swing'in ortak maliyeti" olmaktan
çıkar; short kolu BTC rejim kapısı olmadan sinyal kalitesini değil rejim yönünü ölçer; stop
tavanı (kural 14) uygulanmazsa uzak çapalar modeli maliyet bandının dışına taşır. Testler
bunları, iki kademeli hedefi ve eğim filtresinin YÖNE duyarlı olmasını sabitler.
"""

from __future__ import annotations

import logging

import pandas as pd
import pytest

from core.indicators import Pivot, anchored_vwap, average_true_range, zigzag_pivots
from core.validate import validate_signal
from strategies.avwap import Avwap
from strategies.base import MarketData, Signal
from tests.helpers_market import frame, market

SYMBOL = "AAA-USDT-SWAP"
SPREAD = 0.3
# Çapadan önce 200+ bar: BTC rejim kapısı BTC'nin 200 EMA'sını ister ve BTC serisi de
# `as_of`ta kesilir (kural 12) — kısa bir geçmiş kapıyı test etmez, susturur.
PREFIX_BARS = 200
FLAT_BARS = 14
FLAT_SWING = 1.0
HEAVY_VOLUME = 20.0  # ağırlık düz bölgede: AVWAP oraya oturur, σ dar kalır


def _long_closes(tail: tuple[float, ...] = (104.5, 103.0)) -> list[float]:
    """Düşüş -> teyitli swing dip (99.7) -> ralli -> hacimli düz bölge -> bandın altına sarkma."""
    closes = [130.0] * PREFIX_BARS
    closes += [130.0 - 30.0 * index / 29 for index in range(30)]
    closes += [100.0 + 6.5 * (index + 1) / 8 for index in range(8)]
    closes += [106.5 + (FLAT_SWING if index % 2 == 0 else -FLAT_SWING) for index in range(FLAT_BARS)]
    return closes + list(tail)


def _short_closes(tail: tuple[float, ...] = (125.0, 127.0)) -> list[float]:
    """Long kurulumunun aynası: teyitli swing tepe (130.3) ve bandın üstüne çıkış."""
    closes = [100.0] * PREFIX_BARS
    closes += [100.0 + 30.0 * index / 29 for index in range(30)]
    closes += [130.0 - 6.5 * (index + 1) / 8 for index in range(8)]
    closes += [123.5 + (FLAT_SWING if index % 2 == 0 else -FLAT_SWING) for index in range(FLAT_BARS)]
    return closes + list(tail)


def _candles(closes: list[float], *, tail: int = 2) -> pd.DataFrame:
    volumes = [1.0] * (len(closes) - FLAT_BARS - tail) + [HEAVY_VOLUME] * FLAT_BARS + [1.0] * tail
    return frame(closes, spread=SPREAD, volumes=volumes)


def _btc(bars: int, *, falling: bool = True) -> pd.DataFrame:
    if falling:
        return frame([300.0 - 0.6 * index for index in range(bars)], spread=0.5)
    return frame([100.0 * 1.004**index for index in range(bars)], spread=0.5)


def _snapshot(candles: pd.DataFrame, *, btc_falling: bool = True) -> MarketData:
    return market(
        {SYMBOL: candles},
        btc=_btc(len(candles), falling=btc_falling),
        as_of=candles.index[-1],
    )


def _signals(candles: pd.DataFrame, *, btc_falling: bool = True, **kwargs: float) -> list[Signal]:
    return Avwap(**kwargs).generate_signals(_snapshot(candles, btc_falling=btc_falling))


def _band(candles: pd.DataFrame):
    anchor = Avwap()._anchor(candles)
    assert anchor is not None
    band = anchored_vwap(candles, anchor=anchor.time)
    assert band is not None
    return anchor, band


# --------------------------------------------------------------------------- #
# Sözleşme
# --------------------------------------------------------------------------- #
def test_is_a_two_sided_competitor() -> None:
    strategy = Avwap()
    assert strategy.name == "avwap"
    assert strategy.allowed_directions == ["long", "short"]
    assert strategy.is_benchmark is False
    assert strategy.is_meta is False


def test_registry_resolves_the_model_name() -> None:
    from strategies.registry import build

    assert isinstance(build("avwap"), Avwap)


def test_model_is_retired_but_still_buildable() -> None:
    """Karar 33: canlı listeden ÇIKARILDI, koddan çıkarılmadı.

    Emeklilik ölçütü performans değil ÖLÇÜLEBİLİRLİKTİR: mevcut hızda n=30'a ~42 günde ulaşırdı,
    yani ne kadar iyi olduğu asla öğrenilemezdi ve tabloda yalnızca gürültü üretiyordu.

    Test iki şeyi birden çiviler: modelin canlı listede OLMADIĞINI (kazara geri dönmesi
    sessiz kalmasın) ve hâlâ KURULABİLDİĞİNİ — defteri ve kodu duruyor (kural 1), listeye
    geri eklemek bir commit. "Emekli" ile "silinmiş" aynı şey değildir.
    """
    from core.config import get_setting, load_config
    from strategies.registry import REGISTRY

    assert "avwap" not in get_setting(load_config(), "models")
    assert "avwap" in REGISTRY


def test_signal_uses_the_shared_risk_sizing() -> None:
    signal = _signals(_candles(_long_closes()))[0]
    assert signal.sizing == "risk"
    assert signal.notional_fraction is None


# --------------------------------------------------------------------------- #
# Çapa: kesinleşmiş pivot
# --------------------------------------------------------------------------- #
def test_the_anchor_is_the_last_confirmed_pivot_not_the_live_tip() -> None:
    """Canlı uç bir sonraki barda yer değiştirebilir; çapası kayan AVWAP başka bir gösterge olurdu."""
    candles = _candles(_long_closes())
    pivots = zigzag_pivots(candles, pct_threshold=0.05, min_leg_bars=8)
    live_tip = pivots[-1]
    anchor, _ = _band(candles)

    assert live_tip.kind == "high", "test verisi teyit edilmemiş bir uç taşımalı"
    assert anchor.time != live_tip.time
    assert anchor.kind == "low"
    assert f"{anchor.time:%Y-%m-%d %H:%M}" in _signals(candles)[0].reason


def test_a_frame_without_any_confirmed_pivot_produces_nothing() -> None:
    """Teyit ölçüsü zigzag'ın kendi eşiğidir: hiç dönüş yoksa çapa da yoktur."""
    flat = frame([100.0] * 260, spread=SPREAD)
    assert Avwap().generate_signals(_snapshot(flat)) == []


def test_too_few_bars_since_the_anchor_produce_nothing() -> None:
    """σ birkaç barın gürültüsüyse "2σ" bir aşırılık ölçüsü değildir."""
    assert _signals(_candles(_long_closes()), min_anchor_bars=50) == []


# --------------------------------------------------------------------------- #
# Long kurulumu
# --------------------------------------------------------------------------- #
def test_a_close_below_minus_two_sigma_above_the_anchor_opens_a_long() -> None:
    candles = _candles(_long_closes())
    anchor, band = _band(candles)
    close = float(candles["close"].iloc[-1])
    assert close < band.value - 2.0 * band.deviation, "test verisi bandı gerçekten kırmalı"
    assert close > anchor.price, "test verisi çapanın üstünde kalmalı"

    signals = _signals(candles)
    assert [signal.direction for signal in signals] == ["long"]


def test_long_stop_is_the_wider_of_the_anchor_and_one_and_a_half_atr() -> None:
    candles = _candles(_long_closes())
    anchor, _ = _band(candles)
    atr = average_true_range(candles, 14)
    assert atr is not None
    close = float(candles["close"].iloc[-1])
    assert _signals(candles)[0].stop_price == pytest.approx(min(anchor.price, close - 1.5 * atr))


def test_long_targets_are_minus_one_sigma_then_the_avwap_line() -> None:
    """Tek hedefle yolun yarısında dönen hareket hiç hasat edilemezdi; ±1σ tek başına da
    tezin bittiği yeri (çizginin kendisi) ölçmezdi."""
    candles = _candles(_long_closes())
    _, band = _band(candles)
    targets = _signals(candles)[0].take_profits
    assert [tp.fraction for tp in targets] == [0.5, 0.5]
    assert targets[0].price == pytest.approx(band.value - band.deviation)
    assert targets[1].price == pytest.approx(band.value)


def test_a_long_is_refused_when_price_has_fallen_below_the_anchor(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Çapanın altına düşen fiyatta "dip" iddiası ölmüştür.

    Çapa doğrudan verilir: zigzag geometrisinde fiyat teyitli bir dibin altına inerken
    araya giren zirve de teyitlenir ve çapa zirveye kayar — yani bu koşul veriden
    kurulamaz, ama kurulumun sınırı olarak kodda durması gerekir.
    """
    candles = _candles(_long_closes())
    anchor, _ = _band(candles)
    strategy = Avwap()
    monkeypatch.setattr(
        strategy,
        "_anchor",
        lambda frame: Pivot(time=anchor.time, price=200.0, kind="low"),
    )
    with caplog.at_level(logging.INFO, logger="strategies.avwap"):
        signals = strategy.generate_signals(_snapshot(candles))
    assert signals == []
    assert any("çapa dibinin" in record.getMessage() for record in caplog.records)


# --------------------------------------------------------------------------- #
# Short kurulumu
# --------------------------------------------------------------------------- #
def test_a_close_above_plus_two_sigma_with_btc_below_its_ema_opens_a_short() -> None:
    candles = _candles(_short_closes())
    _, band = _band(candles)
    close = float(candles["close"].iloc[-1])
    assert close > band.value + 2.0 * band.deviation, "test verisi bandı gerçekten kırmalı"

    signals = _signals(candles)
    assert [signal.direction for signal in signals] == ["short"]
    assert "short rejim kapısı açık" in signals[0].reason


def test_short_is_suppressed_while_btc_is_above_its_two_hundred_ema(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Filtresiz short kolu sinyal kalitesini değil rejim yönünü ölçerdi (bkz. meanrev)."""
    candles = _candles(_short_closes())
    with caplog.at_level(logging.INFO, logger="strategies.avwap"):
        signals = _signals(candles, btc_falling=False)
    assert signals == []
    assert any("BTC rejim kapısı kapalı" in record.getMessage() for record in caplog.records)


def test_short_stop_is_the_wider_of_the_anchor_and_one_and_a_half_atr() -> None:
    candles = _candles(_short_closes())
    anchor, _ = _band(candles)
    atr = average_true_range(candles, 14)
    assert atr is not None
    close = float(candles["close"].iloc[-1])
    assert _signals(candles)[0].stop_price == pytest.approx(max(anchor.price, close + 1.5 * atr))


def test_short_targets_are_plus_one_sigma_then_the_avwap_line() -> None:
    candles = _candles(_short_closes())
    _, band = _band(candles)
    targets = _signals(candles)[0].take_profits
    assert targets[0].price == pytest.approx(band.value + band.deviation)
    assert targets[1].price == pytest.approx(band.value)


# --------------------------------------------------------------------------- #
# Filtreler
# --------------------------------------------------------------------------- #
def test_the_slope_filter_only_blocks_the_side_it_is_against(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Aynı sıfır toleransta düşen AVWAP long'u eler, short'a dokunmaz: filtre YÖNE duyarlıdır."""
    with caplog.at_level(logging.INFO, logger="strategies.avwap"):
        longs = _signals(_candles(_long_closes()), slope_limit_atr=0.0)
    assert longs == []
    assert any("eğimi işleme sert ters" in record.getMessage() for record in caplog.records)
    assert len(_signals(_candles(_short_closes()), slope_limit_atr=0.0)) == 1


def test_a_mild_slope_does_not_block_the_trade() -> None:
    """Varsayılan sınır (0.5×ATR) altında kalan eğim tezin kendisidir, engel değil."""
    assert len(_signals(_candles(_long_closes()))) == 1


def test_an_anchor_beyond_the_atr_ceiling_skips_the_trade(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Stop tavana ÇEKİLMEZ (kural 14): uzak çapa işlemi eler, mesafeyi kısaltmaz."""
    candles = _candles(_long_closes())
    anchor, _ = _band(candles)
    candles.loc[anchor.time, "low"] = 90.0
    with caplog.at_level(logging.INFO, logger="strategies.avwap"):
        signals = _signals(candles)
    assert signals == []
    assert any("tavanı" in record.getMessage() for record in caplog.records)


def test_a_close_inside_the_bands_is_not_traded() -> None:
    assert _signals(_candles(_long_closes(tail=(106.0, 105.8)))) == []


# --------------------------------------------------------------------------- #
# Doğrulama kapısı ve gerekçe
# --------------------------------------------------------------------------- #
def test_signals_pass_the_validation_gate() -> None:
    for closes in (_long_closes(), _short_closes()):
        candles = _candles(closes)
        validate_signal(
            _signals(candles)[0],
            entry_price=float(candles["close"].iloc[-1]),
            allowed_directions=Avwap().allowed_directions,
            symbol_universe=[SYMBOL],
            is_benchmark=False,
        )


def test_reason_carries_every_measurement_next_to_its_threshold() -> None:
    reason = _signals(_candles(_long_closes()))[0].reason
    assert "çapa swing dip" in reason
    assert "hacim ağırlıklı σ" in reason
    assert "alt 2σ bandının" in reason
    assert "sınır 0.5×" in reason
    assert "tavan 3×" in reason
    assert "hedef 1σ" in reason


def test_a_symbol_that_is_missing_the_as_of_bar_is_skipped() -> None:
    candles = _candles(_long_closes())
    snapshot = MarketData(
        ohlcv={SYMBOL: candles.iloc[:-1]},
        btc=_btc(len(candles)),
        funding={},
        as_of=candles.index[-1],
    )
    assert Avwap().generate_signals(snapshot) == []
