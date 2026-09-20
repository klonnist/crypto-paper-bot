"""strategies/failed_breakout.py: 20 bar zirvesini süpürüp altına kapanan tuzak (short).

Modelin ölçüm değeri dört kapının GERÇEKTEN tutmasına bağlı: süpürme fitille tanımlanmazsa
model tuzağı değil sıradan kırılımı sayar; hacim kapısı delinirse "katılımsız kırılım" tezi
hiç test edilmemiş olur; uyumsuzluk aranmazsa her geri çekilme sinyal olur; stop tavanı
(kural 14) uygulanmazsa uzun fitiller modeli bandın dışına taşır ve R başına maliyeti
kıyaslanamaz hâle getirir. Testler bunları, teyidin BİRİNCİ kapanışta aranmasını (aynı
tuzağın iki tur üst üste sinyal üretmemesi) ve funding önceliğinin boyuta değil SIRAYA
dokunmasını sabitler.
"""

from __future__ import annotations

import logging

import pandas as pd
import pytest

from core.indicators import average_true_range
from core.validate import validate_signal
from strategies.base import MarketData, Signal
from strategies.failed_breakout import FailedBreakout
from tests.helpers_market import frame, funding_series, market

SYMBOL = "BTC-USDT-SWAP"
OTHER = "ETH-USDT-SWAP"
SPREAD = 0.5
LEVEL = 112.5  # 20 barlık zirve: P1 barının fitil tepesi
SWEEP_HIGH = 113.0
TRAP_CLOSE = 112.0
WEAK_VOLUME = 0.5  # 20 barlık ortalama 1.0 olduğu için doğrudan "×" demek

# Taban (60 bar salınım) -> hızlı ralli (P1, yüksek RSI) -> düzeltme -> yavaş yaklaşma.
# Yavaş yaklaşma uyumsuzluğun kaynağıdır: fiyat P1'in üstüne çıkarken RSI çıkmaz.
_BASE = [100.0 + (0.4 if index % 2 == 0 else -0.4) for index in range(60)]
_RALLY = [102.0, 104.0, 106.0, 108.0, 110.0, 112.0]
_PULLBACK = [111.0, 110.0, 109.0, 109.5, 110.0]
_DRIFT = [110.2, 110.5, 110.8, 111.0, 111.4, 111.8]
_SWEEP_CLOSE = 112.6


def _candles(
    *,
    sweep_high: float = SWEEP_HIGH,
    sweep_close: float = _SWEEP_CLOSE,
    trap_close: float = TRAP_CLOSE,
    volume: float = WEAK_VOLUME,
    middle: list[float] | None = None,
    middle_highs: list[float] | None = None,
) -> pd.DataFrame:
    """Süpürme barı + (isteğe bağlı ara barlar) + tuzak teyidi barı.

    `middle`, teyidin kırılımdan 2 bar sonra geldiği senaryolar içindir; `middle_highs`
    o barların fitillerini açıkça sabitler ki ara bar kendisi bir süpürmeye dönüşmesin.
    """
    body = list(_BASE + _RALLY + _PULLBACK + _DRIFT)
    closes = body + [sweep_close] + list(middle or []) + [trap_close]
    volumes = [1.0] * len(body) + [volume] + [1.0] * (len(middle or []) + 1)
    candles = frame(closes, spread=SPREAD, volumes=volumes)
    sweep_position = len(body)
    candles.loc[candles.index[sweep_position], "high"] = sweep_high
    for offset, high in enumerate(middle_highs or []):
        candles.loc[candles.index[sweep_position + 1 + offset], "high"] = high
    return candles


def _signals(candles: pd.DataFrame | None = None) -> list[Signal]:
    return FailedBreakout().generate_signals(
        market({SYMBOL: candles if candles is not None else _candles()})
    )


# --------------------------------------------------------------------------- #
# Sözleşme
# --------------------------------------------------------------------------- #
def test_is_a_short_only_competitor() -> None:
    """Yön sözleşmesi: long üretmek core/validate.py'de ValueError'dır (kural 8)."""
    strategy = FailedBreakout()
    assert strategy.name == "failed_breakout"
    assert strategy.allowed_directions == ["short"]
    assert strategy.is_benchmark is False
    assert strategy.is_meta is False


def test_registry_resolves_the_model_name() -> None:
    from strategies.registry import build

    assert isinstance(build("failed_breakout"), FailedBreakout)


def test_model_is_retired_but_still_buildable() -> None:
    """Karar 33: canlı listeden ÇIKARILDI, koddan çıkarılmadı.

    Emeklilik ölçütü performans değil ÖLÇÜLEBİLİRLİKTİR: 25 barda HİÇ sinyal üretmedi,
    yani ne kadar iyi olduğu asla öğrenilemezdi ve tabloda yalnızca gürültü üretiyordu.

    Test iki şeyi birden çiviler: modelin canlı listede OLMADIĞINI (kazara geri dönmesi
    sessiz kalmasın) ve hâlâ KURULABİLDİĞİNİ — defteri ve kodu duruyor (kural 1), listeye
    geri eklemek bir commit. "Emekli" ile "silinmiş" aynı şey değildir.
    """
    from core.config import get_setting, load_config
    from strategies.registry import REGISTRY

    assert "failed_breakout" not in get_setting(load_config(), "models")
    assert "failed_breakout" in REGISTRY


def test_signal_uses_the_shared_risk_sizing() -> None:
    signal = _signals()[0]
    assert signal.sizing == "risk"
    assert signal.notional_fraction is None


def test_every_signal_is_a_short() -> None:
    assert all(signal.direction == "short" for signal in _signals())


# --------------------------------------------------------------------------- #
# Sinyal üretilen senaryolar
# --------------------------------------------------------------------------- #
def test_a_swept_high_that_closes_back_below_opens_a_short() -> None:
    signals = _signals()
    assert len(signals) == 1
    assert signals[0].direction == "short"


def test_confirmation_two_bars_after_the_sweep_still_counts() -> None:
    """Teyit penceresi 1-2 bardır; ara bar seviyenin ÜSTÜNDE kapandığı sürece kurulum yaşar."""
    candles = _candles(middle=[112.7], middle_highs=[112.9])
    assert len(_signals(candles)) == 1


def test_stop_falls_back_to_one_atr_when_the_wick_is_narrow() -> None:
    """ATR bir TABAN: fitilin hemen üstüne konan stop R'yi gürültü ölçeğine indirirdi."""
    candles = _candles()
    atr = average_true_range(candles, 14)
    assert atr is not None
    expected = TRAP_CLOSE + atr
    assert expected > SWEEP_HIGH, "test verisi ATR tabanının devrede olduğu hâli kurmalı"
    assert _signals(candles)[0].stop_price == pytest.approx(expected)


def test_a_long_wick_widens_the_stop_beyond_one_atr() -> None:
    """"Hangisi genişse": uzun fitilli süpürmede stop fitilin tepesidir."""
    candles = _candles(sweep_high=114.5)
    atr = average_true_range(candles, 14)
    assert atr is not None
    assert 114.5 > TRAP_CLOSE + atr, "test verisi fitilin geniş olduğu hâli kurmalı"
    assert _signals(candles)[0].stop_price == pytest.approx(114.5)


def test_target_is_the_most_recent_swing_low_below_the_entry() -> None:
    """Tez tuzağın çözüldüğü yerde biter: süpürmeden önceki dip."""
    candles = _candles()
    signal = _signals(candles)[0]
    assert len(signal.take_profits) == 1
    assert signal.take_profits[0].fraction == 1.0
    assert signal.take_profits[0].price == pytest.approx(109.0 - SPREAD)


def test_target_falls_back_to_two_r_when_no_swing_low_sits_below_the_entry() -> None:
    """Sabit R hedefi dipten ÖNCE gelseydi model "tuzak" değil "sabit R hasat eden" olurdu."""
    closes = [100.0 + index for index in range(40)]  # kesin monoton: fraktal üretmez
    closes += [138.0, 137.0, 137.5, 138.5]  # tek düzeltme: bir fraktal zirve + bir fraktal dip
    closes += [138.6, 138.7, 138.8, 138.9, 139.7, 136.0]  # yaklaşma, süpürme, tuzak
    candles = frame(
        closes, spread=SPREAD, volumes=[1.0] * (len(closes) - 2) + [WEAK_VOLUME, 1.0]
    )
    candles.loc[candles.index[-2], "high"] = 139.9

    signal = FailedBreakout().generate_signals(market({SYMBOL: candles}))[0]
    risk = signal.stop_price - 136.0
    assert signal.stop_price == pytest.approx(139.9)
    assert signal.take_profits[0].price == pytest.approx(136.0 - 2.0 * risk)
    assert "girişin altında swing dip yok" in signal.reason


def test_signal_passes_the_validation_gate() -> None:
    candles = _candles()
    validate_signal(
        _signals(candles)[0],
        entry_price=float(candles["close"].iloc[-1]),
        allowed_directions=FailedBreakout().allowed_directions,
        symbol_universe=[SYMBOL],
        is_benchmark=False,
    )


def test_reason_carries_every_gate_next_to_its_threshold() -> None:
    reason = _signals()[0].reason
    assert "20 bar zirvesi 112.5 fitille süpürüldü" in reason
    assert "1 bar sonra altına kapanış" in reason
    assert "kırılım hacmi 0.50× (20 bar ortalaması, eşik <1.00×)" in reason
    assert "RSI(14) ayı uyumsuzluğu" in reason
    assert "tavan 3×" in reason
    assert "hedef son swing dip" in reason


# --------------------------------------------------------------------------- #
# Funding önceliği: boyut değil SIRA
# --------------------------------------------------------------------------- #
def _two_symbol_market(rates: list[float]) -> MarketData:
    """İki sembol de aynı kurulumu taşır; tek fark birinin funding geçmişidir."""
    candles = _candles()
    as_of = candles.index[-1]
    return market(
        {SYMBOL: candles, OTHER: candles.copy()},
        funding={OTHER: funding_series(rates, end=as_of)},
    )


def test_rising_positive_funding_moves_the_signal_to_the_front_of_the_queue() -> None:
    """Motor max_positions dolana kadar SIRAYLA doldurur; öncelik yalnızca burada yaşar."""
    signals = FailedBreakout().generate_signals(_two_symbol_market([0.0001, 0.0002, 0.0003]))
    assert [signal.symbol for signal in signals] == [OTHER, SYMBOL]
    assert "sinyal listenin başına alındı" in signals[0].reason


def test_funding_priority_never_touches_sizing_or_geometry() -> None:
    """Öncelik boyutu büyütemez (kural 3/11): iki sinyal geometrik olarak birebir aynıdır."""
    prioritized, plain = FailedBreakout().generate_signals(
        _two_symbol_market([0.0001, 0.0002, 0.0003])
    )
    assert prioritized.stop_price == pytest.approx(plain.stop_price)
    assert prioritized.take_profits == plain.take_profits
    assert prioritized.sizing == plain.sizing == "risk"


def test_positive_but_falling_funding_gets_no_priority() -> None:
    """Kalabalığın yönü değil, pencerenin iki ucu arasındaki FARK okunur."""
    signals = FailedBreakout().generate_signals(_two_symbol_market([0.0003, 0.0002, 0.0001]))
    assert [signal.symbol for signal in signals] == [SYMBOL, OTHER]
    assert "öncelik yok" in signals[1].reason


def test_rising_but_negative_funding_gets_no_priority() -> None:
    signals = FailedBreakout().generate_signals(_two_symbol_market([-0.0004, -0.0003, -0.0002]))
    assert [signal.symbol for signal in signals] == [SYMBOL, OTHER]


def test_a_short_funding_history_is_not_completed_with_guesses() -> None:
    """Eksik periyodu doldurmak, veri boşluğunu sentetik bir sıraya çevirirdi."""
    signals = FailedBreakout().generate_signals(_two_symbol_market([0.0001, 0.0002]))
    assert [signal.symbol for signal in signals] == [SYMBOL, OTHER]
    assert "tamamlanmadı" in signals[1].reason


# --------------------------------------------------------------------------- #
# Sinyal ÜRETİLMEYEN senaryolar — modelin ölçüm değeri bunlarda
# --------------------------------------------------------------------------- #
def test_a_sweep_on_average_volume_is_not_a_trap(caplog: pytest.LogCaptureFixture) -> None:
    """Hacimle gelen kırılımın geri gelmesi tuzak değil, sıradan bir geri çekilmedir."""
    with caplog.at_level(logging.INFO, logger="strategies.failed_breakout"):
        signals = _signals(_candles(volume=1.0))
    assert signals == []
    assert any("zayıf kırılım değil" in record.getMessage() for record in caplog.records)


def test_volume_just_below_the_average_still_counts_as_weak() -> None:
    """Sınır davranışı yazılı olmalı: eşik "ortalamanın ALTINDA"dır."""
    assert _signals(_candles(volume=1.0)) == []
    assert len(_signals(_candles(volume=0.99))) == 1


def test_a_sweep_without_bearish_divergence_is_not_traded() -> None:
    """Uyumsuzluk aranmazsa her zirve süpürmesi sinyal olur; tezin momentum yarısı kaybolur.

    Veri tek yerde değişir: P1 rallisi yavaşlatılır, böylece süpürmenin RSI'ı P1'inkinin
    ALTINDA değil ÜSTÜNDE kalır.
    """
    slow_rally = [100.4, 100.8, 101.2, 101.6, 102.0, 102.4]
    closes = _BASE + slow_rally + [101.0, 100.0, 99.0, 100.5, 102.0]
    closes += [103.0, 104.5, 106.0, 107.5, 109.0, 110.5, 112.6, TRAP_CLOSE]
    candles = frame(
        closes, spread=SPREAD, volumes=[1.0] * (len(closes) - 2) + [WEAK_VOLUME, 1.0]
    )
    candles.loc[candles.index[-2], "high"] = SWEEP_HIGH
    assert FailedBreakout().generate_signals(market({SYMBOL: candles})) == []


def test_a_sweep_that_holds_above_the_level_is_not_traded() -> None:
    """Teyit yoksa kırılım hâlâ başarılı olabilir; model onu ölçmez."""
    assert _signals(_candles(trap_close=112.8)) == []


def test_a_bar_that_sweeps_and_closes_below_on_its_own_is_a_different_setup() -> None:
    """Aynı bar içinde reddedilen kırılım, "takip eden barda teyit" değildir."""
    assert _signals(_candles(sweep_close=112.2)) == []


def test_the_same_trap_is_not_signalled_again_on_the_next_round() -> None:
    """Teyit BİRİNCİ kapanıştadır: aksi hâlde model aynı tuzağı iki tur üst üste sinyal eder."""
    confirmed = _candles()
    assert len(_signals(confirmed)) == 1
    # Bir bar daha: teyit artık iki bar geride, kurulum orada üretilmişti.
    next_round = _candles(middle=[TRAP_CLOSE], middle_highs=[TRAP_CLOSE + SPREAD])
    assert _signals(next_round) == []


def test_confirmation_three_bars_after_the_sweep_is_out_of_the_window() -> None:
    assert _signals(_candles(middle=[112.7, 112.8], middle_highs=[112.9, 112.9])) == []


def test_a_wick_beyond_the_atr_ceiling_skips_the_trade(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Stop tavana ÇEKİLMEZ (kural 14): uzun fitilli süpürmelerde bu sık olacak."""
    with caplog.at_level(logging.INFO, logger="strategies.failed_breakout"):
        signals = _signals(_candles(sweep_high=118.0))
    assert signals == []
    assert any("tavanı" in record.getMessage() for record in caplog.records)


def test_not_enough_history_produces_nothing() -> None:
    assert _signals(_candles().iloc[-15:]) == []


def test_a_symbol_that_is_missing_the_as_of_bar_is_skipped() -> None:
    candles = _candles()
    snapshot = MarketData(
        ohlcv={SYMBOL: candles.iloc[:-1]},
        btc=candles,
        funding={},
        as_of=candles.index[-1],
    )
    assert FailedBreakout().generate_signals(snapshot) == []
