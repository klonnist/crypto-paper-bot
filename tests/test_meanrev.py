"""strategies/meanrev.py: RSI + Bollinger ortalamaya dönüş ve short'un BTC rejim kapısı.

En kritik test BTC kapısıdır: kapı delinirse short kolu sinyal kalitesini değil rejim yönünü
ölçmeye başlar ve projenin ana sorusunun ("short işlemler long'lardan daha mı başarılı")
cevabı baştan kirlenir. Kalan testler iki eşiğin (RSI + bant) birlikte aranmasını, hedefin
orta bant olmasını ve sinyalin doğrulama kapısından geçmesini sabitler.
"""

from __future__ import annotations

import logging

import pytest

from core.indicators import average_true_range, bollinger, rsi
from core.validate import validate_signal
from strategies.meanrev import MeanReversion
from tests.helpers_market import falling, flat, frame, market, rising

SYMBOL = "ETH-USDT-SWAP"
SPREAD = 1.0


def _oversold() -> list[float]:
    """Düz geçmiş + sert bir düşüş barı: RSI dibe iner ve kapanış alt bandın altında kalır."""
    closes = flat()
    closes[-1] = 80.0
    return closes


def _overbought() -> list[float]:
    closes = flat()
    closes[-1] = 120.0
    return closes


def _signals(closes: list[float], btc_closes: list[float] | None = None):
    candles = frame(closes, spread=SPREAD)
    btc = frame(btc_closes if btc_closes is not None else falling())
    return MeanReversion().generate_signals(market({SYMBOL: candles}, btc=btc))


# --------------------------------------------------------------------------- #
# Sözleşme
# --------------------------------------------------------------------------- #
def test_is_a_competitor_not_a_benchmark() -> None:
    strategy = MeanReversion()
    assert strategy.name == "meanrev"
    assert strategy.is_benchmark is False
    assert strategy.is_meta is False
    assert strategy.allowed_directions == ["long", "short"]


def test_registry_resolves_the_model_name() -> None:
    from strategies.registry import build

    assert isinstance(build("meanrev"), MeanReversion)


def test_model_is_listed_in_config() -> None:
    from core.config import get_setting, load_config

    assert "meanrev" in get_setting(load_config(), "models")


# --------------------------------------------------------------------------- #
# Sinyal üretilen senaryolar
# --------------------------------------------------------------------------- #
def test_oversold_close_below_the_lower_band_opens_a_long() -> None:
    signals = _signals(_oversold())
    assert len(signals) == 1
    assert signals[0].direction == "long"
    assert signals[0].sizing == "risk"
    assert signals[0].notional_fraction is None


def test_overbought_close_above_the_upper_band_opens_a_short_in_a_btc_downtrend() -> None:
    signals = _signals(_overbought(), falling())
    assert len(signals) == 1
    assert signals[0].direction == "short"


def test_take_profit_is_the_middle_band_as_a_single_full_exit() -> None:
    """Hedef tezin kendisidir: fiyat ortalamasına döner. Kısmi çıkış ayrı bir tezdir."""
    closes = _oversold()
    bands = bollinger(frame(closes, spread=SPREAD)["close"], 20, 2.0)
    assert bands is not None
    signal = _signals(closes)[0]
    assert len(signal.take_profits) == 1
    assert signal.take_profits[0].price == pytest.approx(bands.middle)
    assert signal.take_profits[0].fraction == pytest.approx(1.0)


def test_long_stop_sits_two_atr_below_the_close() -> None:
    closes = _oversold()
    atr = average_true_range(frame(closes, spread=SPREAD), 14)
    assert atr is not None
    assert _signals(closes)[0].stop_price == pytest.approx(closes[-1] - 2.0 * atr)


def test_short_stop_sits_two_atr_above_the_close() -> None:
    closes = _overbought()
    atr = average_true_range(frame(closes, spread=SPREAD), 14)
    assert atr is not None
    assert _signals(closes, falling())[0].stop_price == pytest.approx(closes[-1] + 2.0 * atr)


def test_model_asks_for_no_trailing_stop() -> None:
    """Trailing kârı koruma tezidir; bu modelin tezi orta banda dönüştür — karıştırılmaz."""
    assert _signals(_oversold())[0].trailing_atr is None


@pytest.mark.parametrize("closes,btc", [(_oversold(), falling()), (_overbought(), falling())])
def test_signals_pass_the_validation_gate(closes: list[float], btc: list[float]) -> None:
    signal = _signals(closes, btc)[0]
    validate_signal(
        signal,
        entry_price=closes[-1],
        allowed_directions=MeanReversion().allowed_directions,
        symbol_universe=[SYMBOL],
        is_benchmark=False,
    )


def test_reason_carries_the_measurement_next_to_the_threshold() -> None:
    """Eşiksiz bir gerekçe, denetimi commit geçmişinde eşik aramaya zorlardı."""
    reason = _signals(_oversold())[0].reason
    assert "RSI(14)" in reason and "<30" in reason
    assert "Bollinger(20,2)" in reason
    assert "alt" in reason
    assert "TP orta bant" in reason
    assert "ATR(14)" in reason


def test_short_reason_records_the_open_btc_gate() -> None:
    reason = _signals(_overbought(), falling())[0].reason
    assert ">70" in reason
    assert "200 EMA" in reason and "altında" in reason


# --------------------------------------------------------------------------- #
# Sinyal ÜRETİLMEYEN senaryolar
# --------------------------------------------------------------------------- #
def test_no_signal_on_a_flat_market() -> None:
    assert _signals(flat()) == []


def test_rsi_alone_is_not_enough_without_a_band_break() -> None:
    """İki eşik BİRLİKTE aranır: tek başına RSI, bandın içinde kalan bir düşüşte tetiklenmemeli."""
    closes = flat()
    for offset, index in enumerate(range(-14, 0)):
        closes[index] = 100.0 - 0.2 * (offset + 1)
    candles = frame(closes, spread=SPREAD)
    bands = bollinger(candles["close"], 20, 2.0)
    assert bands is not None
    assert closes[-1] > bands.lower, "test verisi bandın İÇİNDE kalmalı"
    assert _signals(closes) == []


def test_band_break_alone_is_not_enough_without_an_rsi_extreme() -> None:
    """Bandın dışına taşan ama RSI eşiğine ulaşmayan bar: iki kapı BİRLİKTE aranmalı.

    Veri, 13 barlık bir düşüşün ardından gelen sert bir toparlanma barıdır: kapanış üst bandın
    üstündedir ama önceki kayıplar RSI'yi 70'in altında tutar. Tek kapıya indirgenmiş bir model
    burada short açardı — yani ortalamaya dönüş değil, salt oynaklık ölçerdi.
    """
    closes = flat()
    for offset in range(13):
        closes[-13 + offset] = 100.0 - (offset + 1)
    closes[-1] = closes[-2] + 20.0
    candles = frame(closes, spread=SPREAD)
    bands = bollinger(candles["close"], 20, 2.0)
    strength = rsi(candles["close"], 14)
    assert bands is not None and strength is not None
    assert closes[-1] > bands.upper, "test verisi bandı gerçekten kırmalı"
    assert strength < 70.0, "test verisi RSI eşiğinin ALTINDA kalmalı"
    assert _signals(closes, falling()) == []


# --------------------------------------------------------------------------- #
# BTC rejim kapısı — modelin ölçüm açısından en kritik kuralı
# --------------------------------------------------------------------------- #
def test_short_is_suppressed_while_btc_trades_above_its_200_ema(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Boğa rejiminde "aşırı alım" sürekli tetiklenir; filtresiz short kolu rejimi ölçerdi."""
    with caplog.at_level(logging.INFO, logger="strategies.meanrev"):
        signals = _signals(_overbought(), rising())
    assert signals == []
    assert any("bastırıldı" in record.getMessage() for record in caplog.records)


def test_the_same_setup_produces_a_short_once_btc_falls_below_its_200_ema() -> None:
    """Kapının tek değişkeni BTC olmalı: sembol verisi birebir aynı, sonuç farklı."""
    closes = _overbought()
    assert _signals(closes, rising()) == []
    assert [signal.direction for signal in _signals(closes, falling())] == ["short"]


def test_long_is_unaffected_by_the_btc_gate() -> None:
    """Kapı YALNIZCA short içindir; long'u da kesmek modeli tek yönlü bir rejim modeline çevirirdi."""
    assert [signal.direction for signal in _signals(_oversold(), rising())] == ["long"]


def test_short_gate_stays_closed_when_btcs_200_ema_cannot_be_computed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Açık varsaymak, filtrenin var olmadığı bir dönemde short açmak olurdu."""
    candles = frame(_overbought(), spread=SPREAD)
    short_btc = frame(falling(bars=199))
    with caplog.at_level(logging.INFO, logger="strategies.meanrev"):
        signals = MeanReversion().generate_signals(
            market({SYMBOL: candles}, btc=short_btc, as_of=candles.index[-1])
        )
    assert signals == []
    assert any("short kapısı kapalı" in record.getMessage() for record in caplog.records)


# --------------------------------------------------------------------------- #
# Veri hijyeni
# --------------------------------------------------------------------------- #
def test_symbol_without_the_as_of_bar_is_ignored() -> None:
    fresh = frame(_oversold(), spread=SPREAD)
    stale = frame(_oversold(), spread=SPREAD)[:-1]
    signals = MeanReversion().generate_signals(
        market({SYMBOL: fresh, "SOL-USDT-SWAP": stale}, btc=frame(falling()), as_of=fresh.index[-1])
    )
    assert [signal.symbol for signal in signals] == [SYMBOL]


def test_no_signal_before_the_bollinger_window_is_full() -> None:
    closes = flat(bars=19)
    closes[-1] = 80.0
    candles = frame(closes, spread=SPREAD)
    signals = MeanReversion().generate_signals(
        market({SYMBOL: candles}, btc=frame(falling()), as_of=candles.index[-1])
    )
    assert signals == []


def test_skipped_signal_is_logged_when_atr_is_unusable(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr("strategies.meanrev.average_true_range", lambda frame, period: None)
    with caplog.at_level(logging.INFO, logger="strategies.meanrev"):
        signals = _signals(_oversold())
    assert signals == []
    assert any("ATR" in record.getMessage() for record in caplog.records)
