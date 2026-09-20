"""strategies/confluence.py: büyük dalga retracement'i × küçük bacak extension'ı.

Modelin ölçüm değeri, kaynak tarayıcıdaki (crypto-scanner) kapının BURADA DA aynı kapı
olmasına bağlı: aday seçimi (büyük dalga / bağımsız küçük bacak), tolerans ve yönü RSI'ın
belirlemesi. Testler bunları, kaynaktan bilinçli sapmaları (boyutlandırma yok, stop 2.5×ATR)
ve sinyalin doğrulama kapısından geçtiğini sabitler.

Fiyat yolları parça parça doğrusal kurulur: pivotların nerede oluşacağı (ve dolayısıyla hangi
bacağın büyük dalga, hangisinin küçük bacak olduğu) okunabilir kalmalı — rastgele yürüyüşte
kapıyı geçen bir kurulum ancak arayarak bulunur ve test bozulduğunda neyin değiştiği anlaşılmaz.
"""

from __future__ import annotations

import logging

import pandas as pd
import pytest

from core.indicators import average_true_range, rsi
from core.validate import validate_signal
from strategies.base import MarketData, Signal
from strategies.confluence import Confluence, _Leg
from tests.helpers_market import frame, market

SYMBOL = "BTC-USDT-SWAP"
SPREAD = 0.2

# LONG kurulumu: büyük DÜŞÜŞ dalgası (200 -> 133.4) + sonrasında bağımsız bir yükseliş
# bacağı (173.8 -> 183.7). Kapanış 175.2, büyük dalganın 0.618 retracement'i ile küçük
# bacağın 1.272 extension'ının çakıştığı yerde; son 8 barlık geri çekilme RSI'ı 35'in
# altına indirir.
LONG_START = 200.0
LONG_PATH = [(133.4, 40), (185.0, 20), (174.0, 12), (183.5, 20), (175.2, 8)]
# Aynı kurulumun aynası: büyük YÜKSELİŞ dalgası + son bacaktan sonra yavaş toparlanma,
# RSI 65'in üstünde kalır.
SHORT_START = 133.6
SHORT_PATH = [(200.0, 40), (150.0, 20), (160.0, 12), (151.5, 20), (158.0, 8)]


def _closes(start: float, segments: list[tuple[float, int]]) -> list[float]:
    """Parça parça doğrusal fiyat yolu: her parça `bars` barda `target`e ulaşır."""
    closes = [start]
    for target, bars in segments:
        begin = closes[-1]
        closes.extend(begin + (target - begin) * (step + 1) / bars for step in range(bars))
    return closes


def _candles(start: float, segments: list[tuple[float, int]]) -> pd.DataFrame:
    return frame(_closes(start, segments), spread=SPREAD)


def _long_setup() -> pd.DataFrame:
    return _candles(LONG_START, LONG_PATH)


def _short_setup() -> pd.DataFrame:
    return _candles(SHORT_START, SHORT_PATH)


def _signals(candles: pd.DataFrame, strategy: Confluence | None = None) -> list[Signal]:
    model = strategy if strategy is not None else Confluence()
    return model.generate_signals(market({SYMBOL: candles}))


# --------------------------------------------------------------------------- #
# Sözleşme
# --------------------------------------------------------------------------- #
def test_is_a_competitor_not_a_benchmark() -> None:
    """Yarışmacı model kendi boyutunu belirleyemez (kural 3/11/15)."""
    strategy = Confluence()
    assert strategy.name == "confluence"
    assert strategy.is_benchmark is False
    assert strategy.is_meta is False
    assert strategy.allowed_directions == ["long", "short"]


def test_registry_resolves_the_model_name() -> None:
    from strategies.registry import build

    assert isinstance(build("confluence"), Confluence)


def test_model_is_retired_but_still_buildable() -> None:
    """Karar 33: canlı listeden ÇIKARILDI, koddan çıkarılmadı.

    Emeklilik ölçütü performans değil ÖLÇÜLEBİLİRLİKTİR: mevcut hızda n=30'a ~72 günde ulaşırdı,
    yani ne kadar iyi olduğu asla öğrenilemezdi ve tabloda yalnızca gürültü üretiyordu.

    Test iki şeyi birden çiviler: modelin canlı listede OLMADIĞINI (kazara geri dönmesi
    sessiz kalmasın) ve hâlâ KURULABİLDİĞİNİ — defteri ve kodu duruyor (kural 1), listeye
    geri eklemek bir commit. "Emekli" ile "silinmiş" aynı şey değildir.
    """
    from core.config import get_setting, load_config
    from strategies.registry import REGISTRY

    assert "confluence" not in get_setting(load_config(), "models")
    assert "confluence" in REGISTRY


def test_the_source_confidence_tiers_never_become_position_size() -> None:
    """Kaynakta confidence 0.5R/1.0R/1.5R çarpanıydı; burada boyut stratejinin işi değil.

    Bu test kuralın kendisini korur: bir gün "high confidence'ta biraz büyük açalım" diye
    eklenen bir alan, modelleri ortak risk biriminden (1R) çıkarır ve tabloyu kıyaslanamaz
    hâle getirir.
    """
    for signal in _signals(_long_setup()) + _signals(_short_setup()):
        assert signal.sizing == "risk"
        assert signal.notional_fraction is None


# --------------------------------------------------------------------------- #
# Kapı açık: sinyal üretilen kurulumlar
# --------------------------------------------------------------------------- #
def test_confluence_with_an_oversold_rsi_opens_a_long() -> None:
    signals = _signals(_long_setup())
    assert len(signals) == 1
    assert signals[0].direction == "long"


def test_confluence_with_an_overbought_rsi_opens_a_short() -> None:
    signals = _signals(_short_setup())
    assert len(signals) == 1
    assert signals[0].direction == "short"


def test_direction_comes_from_rsi_not_from_the_geometry() -> None:
    """Kaynaktaki kural: seviye matematiği yön bağımsız, yönü YALNIZCA RSI belirler."""
    candles = _long_setup()
    strength = rsi(candles["close"], 14)
    assert strength is not None and strength < 35.0
    # Aynı geometri, eşikler ters çevrildiğinde short üretir: değişen tek şey RSI kapısıdır.
    flipped = Confluence(rsi_oversold=-1.0, rsi_overbought=strength - 1.0)
    assert [signal.direction for signal in _signals(candles, flipped)] == ["short"]


def test_long_stop_sits_two_and_a_half_atr_below_the_close() -> None:
    candles = _long_setup()
    atr = average_true_range(candles, 14)
    assert atr is not None
    close = float(candles["close"].iloc[-1])
    assert _signals(candles)[0].stop_price == pytest.approx(close - 2.5 * atr)


def test_short_stop_sits_two_and_a_half_atr_above_the_close() -> None:
    candles = _short_setup()
    atr = average_true_range(candles, 14)
    assert atr is not None
    close = float(candles["close"].iloc[-1])
    assert _signals(candles)[0].stop_price == pytest.approx(close + 2.5 * atr)


def test_stop_distance_stays_under_the_shared_ceiling() -> None:
    """Kaynaktaki 3.0×ATR tam TAVANIN kendisiydi; dolum bir sonraki barda olduğu için
    (kural 13) motor işlemi elerdi. 2.5× bandın üst ucu, tavanın altı."""
    from core.config import get_setting, load_config

    ceiling = float(get_setting(load_config(), "max_stop_atr_multiple"))
    for candles in (_long_setup(), _short_setup()):
        atr = average_true_range(candles, 14)
        signal = _signals(candles)[0]
        assert atr is not None and signal.stop_price is not None
        distance = abs(float(candles["close"].iloc[-1]) - signal.stop_price)
        assert distance / atr < ceiling


def test_model_asks_for_no_trailing_stop_and_no_take_profit() -> None:
    """Kaynakta da çıkış yönetimi yok: kapı bir GİRİŞ kapısıdır."""
    signal = _signals(_long_setup())[0]
    assert signal.trailing_atr is None
    assert signal.take_profits == ()


def test_signals_pass_the_validation_gate() -> None:
    for candles in (_long_setup(), _short_setup()):
        signal = _signals(candles)[0]
        validate_signal(
            signal,
            entry_price=float(candles["close"].iloc[-1]),
            allowed_directions=Confluence().allowed_directions,
            symbol_universe=[SYMBOL],
            is_benchmark=False,
        )


def test_reason_carries_both_levels_next_to_the_tolerance() -> None:
    """Gerekçe olmadan "bu işlem hangi çakışmadan doğdu" sorusu deftere bakarak cevaplanamaz."""
    reason = _signals(_long_setup())[0].reason
    assert "RSI(14)" in reason and "<35" in reason
    assert "0.618 retracement" in reason
    assert "1.272 extension" in reason
    assert "tolerans %3" in reason
    assert "zigzag %5 / 8 bar" in reason
    assert "2.5×ATR(14)" in reason


# --------------------------------------------------------------------------- #
# Kapı kapalı: sinyal ÜRETİLMEYEN kurulumlar
# --------------------------------------------------------------------------- #
def test_a_level_outside_the_tolerance_closes_the_gate() -> None:
    """Tolerans gevşerse "çakışma" iki ayrı seviyeye dönüşür ve model tezini kaybeder."""
    drifted = _candles(LONG_START, [*LONG_PATH[:-1], (176.5, 8)])
    assert _signals(drifted) == []


def test_a_neutral_rsi_produces_no_signal_and_says_so(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Kurulum var ama yön yok: kapının kaç kez yönsüz açıldığı denetlenebilir olmalı."""
    neutral = Confluence(rsi_oversold=1.0, rsi_overbought=99.0)
    with caplog.at_level(logging.INFO, logger="strategies.confluence"):
        assert _signals(_long_setup(), neutral) == []
    assert any("nötr" in record.getMessage() for record in caplog.records)


def test_a_flat_market_has_no_legs_to_compare() -> None:
    assert _signals(frame([100.0] * 200, spread=SPREAD)) == []


def test_a_symbol_that_is_missing_the_as_of_bar_is_skipped() -> None:
    candles = _long_setup()
    snapshot = MarketData(
        ohlcv={SYMBOL: candles.iloc[:-1]},
        btc=candles,
        funding={},
        as_of=candles.index[-1],
    )
    assert Confluence().generate_signals(snapshot) == []


# --------------------------------------------------------------------------- #
# Bağımsızlık kuralı — kaynaktaki tanımın birebir karşılığı
# --------------------------------------------------------------------------- #
def _leg(leg_id: int, *, a_bar: int, b_bar: int) -> _Leg:
    index = pd.date_range("2026-01-01", periods=40, freq="4h", tz="UTC")
    return _Leg(
        leg_id=leg_id,
        a_kind="low",
        a_price=100.0,
        a_time=index[a_bar],
        b_price=110.0,
        b_time=index[b_bar],
        duration_bars=b_bar - a_bar,
        magnitude=10.0,
        retracements={},
        extensions={},
    )


def test_a_leg_sharing_an_endpoint_with_the_big_wave_is_not_independent() -> None:
    """Aynı B'yi paylaşan iki bacak aynı hareketin iki yüzüdür; çakışmaları "iki bağımsız
    swing" sayılamaz — kaynaktaki ek koşulun tam olarak engellediği şey."""
    big = _leg(0, a_bar=0, b_bar=10)
    adjacent = _leg(1, a_bar=10, b_bar=20)  # A'sı büyük dalganın B'si
    assert Confluence()._small_leg([big, adjacent], big) is None


def test_the_most_recent_independent_leg_wins() -> None:
    big = _leg(0, a_bar=0, b_bar=10)
    older = _leg(2, a_bar=12, b_bar=20)
    newest = _leg(3, a_bar=22, b_bar=30)
    assert Confluence()._small_leg([big, older, newest], big) is newest


def test_a_leg_that_starts_before_the_big_wave_ends_is_not_a_candidate() -> None:
    """Küçük bacak "büyük dalganın SONRASINDA" olmalı: iç içe geçmiş bacaklar bağımsız değil."""
    big = _leg(0, a_bar=0, b_bar=20)
    overlapping = _leg(1, a_bar=5, b_bar=25)
    assert Confluence()._small_leg([big, overlapping], big) is None
