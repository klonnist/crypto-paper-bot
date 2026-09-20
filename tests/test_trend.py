"""strategies/trend.py: Donchian kırılımı + EMA rejim filtresi.

Modelin ölçüm değeri iki kapının GERÇEKTEN tutmasına bağlı: kırılım olmadan sinyal üretirse
ölçtüğü şey kırılım olmaktan çıkar, rejim filtresi delinirse sonuç "kırılım işe yarıyor mu"
sorusuna değil "hangi rejimde ne kadar testere yedik" sorusuna cevap verir. Testler bu iki
kapıyı, stop mesafesinin maliyet ölçeğini (kural 14) ve sinyalin doğrulama kapısından
geçtiğini sabitler.
"""

from __future__ import annotations

import logging

import pytest

from core.indicators import average_true_range, donchian, ema
from core.validate import validate_signal
from strategies.trend import Trend
from tests.helpers_market import falling, flat, frame, market, rising

SYMBOL = "BTC-USDT-SWAP"


def _uptrend_breakout() -> list[float]:
    """Monoton yükseliş: rejim yukarı ve son bar 20 barlık tepenin üstünde kapanır."""
    return rising()


def _uptrend_inside_channel() -> list[float]:
    """Aynı rejim, ama son bar kanalın İÇİNDE kapanır (tepe = önceki kapanış + spread)."""
    closes = rising()
    closes[-1] = closes[-2] + 0.2
    return closes


def _downtrend_breakdown() -> list[float]:
    return falling()


def _downtrend_with_upward_breakout() -> list[float]:
    """Rejim aşağı, ama son bar yukarı kırıyor: filtre bu işlemi engellemeli."""
    closes = falling()
    closes[-1] = closes[-2] + 20.0
    return closes


def _signals(closes: list[float], *, symbol: str = SYMBOL, spread: float = 0.5):
    return Trend().generate_signals(market({symbol: frame(closes, spread=spread)}))


# --------------------------------------------------------------------------- #
# Sözleşme
# --------------------------------------------------------------------------- #
def test_is_a_competitor_not_a_benchmark() -> None:
    """Yarışmacı model kendi boyutunu belirleyemez (kural 3/11/15)."""
    strategy = Trend()
    assert strategy.name == "trend"
    assert strategy.is_benchmark is False
    assert strategy.is_meta is False
    assert strategy.allowed_directions == ["long", "short"]


def test_registry_resolves_the_model_name() -> None:
    from strategies.registry import build

    assert isinstance(build("trend"), Trend)


def test_model_is_listed_in_config() -> None:
    from core.config import get_setting, load_config

    assert "trend" in get_setting(load_config(), "models")


# --------------------------------------------------------------------------- #
# Sinyal üretilen senaryo
# --------------------------------------------------------------------------- #
def test_close_above_donchian_high_in_an_uptrend_opens_a_long() -> None:
    signals = _signals(_uptrend_breakout())
    assert len(signals) == 1
    assert signals[0].direction == "long"
    assert signals[0].symbol == SYMBOL


def test_close_below_donchian_low_in_a_downtrend_opens_a_short() -> None:
    signals = _signals(_downtrend_breakdown())
    assert len(signals) == 1
    assert signals[0].direction == "short"


def test_long_stop_sits_two_atr_below_the_close() -> None:
    """Stop mesafesi maliyet ölçeğidir (kural 14): 2×ATR bandın ortası, tavana takılmaz."""
    closes = _uptrend_breakout()
    candles = frame(closes)
    atr = average_true_range(candles, 14)
    assert atr is not None
    signal = _signals(closes)[0]
    assert signal.stop_price == pytest.approx(closes[-1] - 2.0 * atr)


def test_short_stop_sits_two_atr_above_the_close() -> None:
    closes = _downtrend_breakdown()
    atr = average_true_range(frame(closes), 14)
    assert atr is not None
    signal = _signals(closes)[0]
    assert signal.stop_price == pytest.approx(closes[-1] + 2.0 * atr)


def test_stop_distance_stays_under_the_configured_atr_ceiling() -> None:
    from core.config import get_setting, load_config

    ceiling = float(get_setting(load_config(), "max_stop_atr_multiple"))
    closes = _uptrend_breakout()
    atr = average_true_range(frame(closes), 14)
    assert atr is not None
    signal = _signals(closes)[0]
    assert abs(closes[-1] - signal.stop_price) / atr <= ceiling


def test_signal_requests_a_one_atr_trailing_stop_without_implementing_it() -> None:
    """Trailing'i core/engine.py yürütür (kural 9); strateji yalnızca isteği bildirir."""
    signal = _signals(_uptrend_breakout())[0]
    assert signal.trailing_atr == pytest.approx(1.0)


def test_signal_uses_common_risk_sizing() -> None:
    signal = _signals(_uptrend_breakout())[0]
    assert signal.sizing == "risk"
    assert signal.notional_fraction is None
    assert signal.take_profits == ()


def test_signal_passes_the_validation_gate() -> None:
    closes = _uptrend_breakout()
    signal = _signals(closes)[0]
    validate_signal(
        signal,
        entry_price=closes[-1],
        allowed_directions=Trend().allowed_directions,
        symbol_universe=[SYMBOL],
        is_benchmark=False,
    )


def test_reason_names_the_level_the_regime_and_the_stop() -> None:
    """Gerekçe dashboard'da "model neden bu işlemi yaptı" denetiminin tek kaynağıdır."""
    closes = _uptrend_breakout()
    channel = donchian(frame(closes), 20)
    assert channel is not None
    reason = _signals(closes)[0].reason
    assert "Donchian" in reason
    assert f"{channel.upper:.6g}" in reason
    assert "EMA50" in reason and "EMA200" in reason
    assert "rejim yukarı" in reason
    assert "ATR(14)" in reason


# --------------------------------------------------------------------------- #
# Sinyal ÜRETİLMEYEN senaryolar
# --------------------------------------------------------------------------- #
def test_no_signal_when_the_close_stays_inside_the_channel() -> None:
    assert _signals(_uptrend_inside_channel()) == []


def test_no_signal_on_a_flat_market() -> None:
    assert _signals(flat()) == []


def test_regime_filter_blocks_an_upward_breakout_in_a_downtrend() -> None:
    """50/200 EMA aşağıysa long açılmaz — filtrenin tek işi budur."""
    closes = _downtrend_with_upward_breakout()
    candles = frame(closes)
    channel = donchian(candles, 20)
    fast, slow = ema(candles["close"], 50), ema(candles["close"], 200)
    assert channel is not None and fast is not None and slow is not None
    assert closes[-1] > channel.upper, "test verisi kırılımı gerçekten içermeli"
    assert fast < slow, "test verisi rejimi gerçekten aşağıda tutmalı"
    assert _signals(closes) == []


def test_regime_filter_blocks_a_downward_breakdown_in_an_uptrend() -> None:
    closes = rising()
    closes[-1] = closes[-2] - 20.0
    candles = frame(closes)
    channel = donchian(candles, 20)
    assert channel is not None and closes[-1] < channel.lower
    assert _signals(closes) == []


def test_no_signal_before_the_slow_ema_has_enough_history() -> None:
    """Eksik geçmişte "200 EMA" üretmek rejim filtresini sessizce anlamsız kılardı."""
    assert _signals(rising(bars=199)) == []


def test_symbol_without_the_as_of_bar_is_ignored() -> None:
    """Bir bar geriden sinyal üretmek, look-ahead kadar sessiz bir ölçüm hatasıdır."""
    fresh = frame(rising())
    stale = frame(rising())[:-1]
    signals = Trend().generate_signals(
        market({"BTC-USDT-SWAP": fresh, "ETH-USDT-SWAP": stale}, as_of=fresh.index[-1])
    )
    assert [signal.symbol for signal in signals] == ["BTC-USDT-SWAP"]


def test_signals_are_ordered_by_symbol() -> None:
    """max_positions dolduğunda hangi sembolün girdiği veri katmanının sözlük sırasına kalmamalı."""
    candles = frame(rising())
    unsorted = {"SOL-USDT-SWAP": candles, "BTC-USDT-SWAP": candles, "ETH-USDT-SWAP": candles}
    signals = Trend().generate_signals(market(unsorted))
    assert [signal.symbol for signal in signals] == sorted(unsorted)


def test_skipped_signal_is_logged_when_atr_is_unusable(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Atlama sessiz olamaz (kural 14): kayıt olmadan işlem sayısı ölçülemeyen bir nedenle düşer.

    ATR bu veriyle hesaplanabildiği için dal doğrudan zorlanır: kullanılamaz bir ATR (0 ya da
    None) gerçek veride kırılımla bir arada görülmez — kanal son barın geçmişinden geldiği için
    ATR'yi sıfırlayacak düz bir kuyruk kırılımı da imkânsız kılar. Yine de dal savunma amaçlı
    duruyor; test onun SESSİZ olmadığını sabitler.
    """
    monkeypatch.setattr("strategies.trend.average_true_range", lambda frame, period: 0.0)
    with caplog.at_level(logging.INFO, logger="strategies.trend"):
        signals = _signals(_uptrend_breakout())
    assert signals == []
    assert any("ATR" in record.getMessage() for record in caplog.records)
