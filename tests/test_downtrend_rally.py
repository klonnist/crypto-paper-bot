"""strategies/downtrend_rally.py: düşüş trendinde ralli satışı (short).

Modelin ölçüm değeri kapıların GERÇEKTEN tutmasına bağlı: rejim kapısı delinirse model
yükselen piyasada da satar; kesitsel zayıflık kapısı delinirse "düşüyor" mutlak bir ifadeye
döner ve model bir piyasa yönü bahsine indirgenir; tetik (RSI + hacim) yarım uygulanırsa
tezin yalnızca yarısı test edilir; funding kapısı olmadan squeeze'e yakalanan işlemler
sinyal kalitesi diye raporlanır. Testler bunları, dokunuşun İLK barda aranmasını ve stop
tavanını (kural 14) sabitler.
"""

from __future__ import annotations

import logging

import pandas as pd
import pytest

from core.validate import validate_signal
from strategies.base import MarketData, Signal
from strategies.downtrend_rally import DowntrendRally
from tests.helpers_market import frame, funding_series, market

SYMBOL = "CAND-USDT-SWAP"
PEERS = [f"ALT{index}-USDT-SWAP" for index in range(4)]
NEUTRAL_FUNDING = [0.0001, 0.0001, 0.0001]
SQUEEZE_FUNDING = [-0.0002, -0.0003, -0.0004]
CONFIRMED_VOLUME = 2.0  # 20 barlık ortalama 1.0 olduğu için doğrudan "×" demek


def _staircase() -> list[float]:
    """Alçalan zirveler/dipler: zigzag'ın %5 eşiğini geçen bacaklar üretir."""
    closes = [400.0]
    for _ in range(12):
        for _ in range(12):
            closes.append(closes[-1] * 0.989)
        for _ in range(8):
            closes.append(closes[-1] * 1.0075)
    return closes


def _closes(
    *,
    final_bars: int = 12,
    final_pct: float = -0.011,
    bounce: tuple[float, ...] = (0.03, 0.03),
) -> list[float]:
    """Merdiven + son düşüş bacağı + ralli barları."""
    closes = _staircase()
    for _ in range(final_bars):
        closes.append(closes[-1] * (1 + final_pct))
    for step in bounce:
        closes.append(closes[-1] * (1 + step))
    return closes


def _candidate(
    *,
    volume: float = CONFIRMED_VOLUME,
    final_bars: int = 12,
    final_pct: float = -0.011,
    bounce: tuple[float, ...] = (0.03, 0.03),
) -> pd.DataFrame:
    closes = _closes(final_bars=final_bars, final_pct=final_pct, bounce=bounce)
    return frame(
        closes,
        spread=closes[-1] * 0.002,
        volumes=[1.0] * (len(closes) - 1) + [volume],
    )


def _peer_rising(bars: int = 255) -> pd.DataFrame:
    """Evrenin geri kalanı: aday sembolü alt %20'ye iten güçlü yükseliş.

    Bar sayısı adayınkiyle eşitlenir: `as_of` barını taşımayan sembol sıralamaya hiç
    girmez (kural 5/12), yani kısa bir eş sembol kesitsel kapıyı test etmez, susturur.
    """
    return frame([100.0 * 1.004**index for index in range(bars)], spread=0.2)


def _peer_falling(bars: int = 255) -> pd.DataFrame:
    """Adaydan DAHA zayıf, rallisiz sembol: kesitsel eşiği aşağı çeker, kendisi sinyal vermez."""
    return frame([400.0 * 0.99**index for index in range(bars)], spread=0.2)


def _snapshot(
    candidate: pd.DataFrame | None = None,
    *,
    funding: list[float] | None = NEUTRAL_FUNDING,
    peers: pd.DataFrame | None = None,
    peer_count: int = 4,
) -> MarketData:
    candles = candidate if candidate is not None else _candidate()
    as_of = candles.index[-1]
    peer = peers if peers is not None else _peer_rising(len(candles))
    ohlcv = {SYMBOL: candles}
    for name in PEERS[:peer_count]:
        ohlcv[name] = peer.copy()
    return market(
        ohlcv,
        as_of=as_of,
        funding=(
            {SYMBOL: funding_series(funding, end=as_of)} if funding is not None else None
        ),
    )


def _signals(snapshot: MarketData | None = None) -> list[Signal]:
    return DowntrendRally().generate_signals(snapshot if snapshot is not None else _snapshot())


# --------------------------------------------------------------------------- #
# Sözleşme
# --------------------------------------------------------------------------- #
def test_is_a_short_only_competitor() -> None:
    strategy = DowntrendRally()
    assert strategy.name == "downtrend_rally"
    assert strategy.allowed_directions == ["short"]
    assert strategy.is_benchmark is False
    assert strategy.is_meta is False


def test_registry_resolves_the_model_name() -> None:
    from strategies.registry import build

    assert isinstance(build("downtrend_rally"), DowntrendRally)


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

    assert "downtrend_rally" not in get_setting(load_config(), "models")
    assert "downtrend_rally" in REGISTRY


def test_signal_uses_the_shared_risk_sizing() -> None:
    signal = _signals()[0]
    assert signal.sizing == "risk"
    assert signal.notional_fraction is None
    assert signal.direction == "short"


# --------------------------------------------------------------------------- #
# Sinyal üretilen senaryolar
# --------------------------------------------------------------------------- #
def test_a_rally_into_the_retracement_zone_opens_a_short() -> None:
    signals = _signals()
    assert [signal.symbol for signal in signals] == [SYMBOL]
    assert "düzeltme bandı" in signals[0].reason


def test_a_rally_that_only_reaches_the_twenty_ema_also_opens_a_short() -> None:
    """İki dokunuş tanımı da geçerlidir; bu kurulumda düzeltme bandına ulaşılmaz."""
    candidate = _candidate(final_bars=28, final_pct=-0.006, bounce=(0.026, 0.026))
    signals = _signals(_snapshot(candidate))
    assert [signal.symbol for signal in signals] == [SYMBOL]
    assert "20 EMA" in signals[0].reason


def test_stop_is_the_wider_of_the_rally_top_and_one_and_a_half_atr() -> None:
    from core.indicators import average_true_range

    candidate = _candidate()
    atr = average_true_range(candidate, 14)
    assert atr is not None
    close = float(candidate["close"].iloc[-1])
    rally_top = float(candidate["high"].iloc[-3:].max())
    expected = max(rally_top, close + 1.5 * atr)
    assert _signals(_snapshot(candidate))[0].stop_price == pytest.approx(expected)


def test_target_is_the_previous_low_for_half_the_position_and_the_rest_trails() -> None:
    """Bacağın dibi tezin bittiği yer; kalanı trendin devamını ölçmeye devam eder (kural 9)."""
    candidate = _candidate()
    signal = _signals(_snapshot(candidate))[0]
    leg_low = float(candidate["low"].iloc[-15:-2].min())
    assert len(signal.take_profits) == 1
    assert signal.take_profits[0].price == pytest.approx(leg_low)
    assert signal.take_profits[0].fraction == 0.5
    assert signal.trailing_atr == 1.0


def test_signal_passes_the_validation_gate() -> None:
    candidate = _candidate()
    validate_signal(
        _signals(_snapshot(candidate))[0],
        entry_price=float(candidate["close"].iloc[-1]),
        allowed_directions=DowntrendRally().allowed_directions,
        symbol_universe=[SYMBOL, *PEERS],
        is_benchmark=False,
    )


def test_reason_carries_every_gate_next_to_its_threshold() -> None:
    reason = _signals()[0].reason
    assert "rejim: kapanış" in reason and "< EMA200" in reason
    assert "7g getiri" in reason and "evrenin alt %20 eşiği" in reason
    assert "aşağıdan dokundu" in reason
    assert "(<45) ve hacim 2.00× (20 bar ortalaması, eşik >1.00×)" in reason
    assert "funding son 3 periyot ortalaması" in reason
    assert "tavan 3×" in reason
    assert "kalanı 1×ATR trailing" in reason


# --------------------------------------------------------------------------- #
# Sinyal ÜRETİLMEYEN senaryolar
# --------------------------------------------------------------------------- #
def test_a_touch_without_the_volume_half_of_the_trigger_is_not_traded(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Tetiğin iki yarısı birden aranır; hacimsiz ralli tezin yarısını test etmiş olurdu."""
    with caplog.at_level(logging.INFO, logger="strategies.downtrend_rally"):
        signals = _signals(_snapshot(_candidate(volume=1.0)))
    assert signals == []
    assert any("tetiklenmedi" in record.getMessage() for record in caplog.records)


def test_a_touch_with_strong_momentum_is_not_traded(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """RSI eşiği delinirse model "ralli" ile "dönüş"ü ayırt edemez."""
    candidate = _candidate(final_bars=6, bounce=(0.025, 0.02))
    with caplog.at_level(logging.INFO, logger="strategies.downtrend_rally"):
        signals = _signals(_snapshot(candidate))
    assert signals == []
    assert any("tetiklenmedi" in record.getMessage() for record in caplog.records)


def test_deeply_negative_funding_closes_the_gate(caplog: pytest.LogCaptureFixture) -> None:
    """Kalabalık zaten short taraftaysa ölçülen şey sinyal kalitesi değil, squeeze riskidir."""
    with caplog.at_level(logging.INFO, logger="strategies.downtrend_rally"):
        signals = _signals(_snapshot(funding=SQUEEZE_FUNDING))
    assert signals == []
    assert any("squeeze riski" in record.getMessage() for record in caplog.records)


def test_funding_exactly_at_the_floor_still_trades() -> None:
    """Sınır davranışı yazılı olmalı: kapı "tabanın ALTINDA" kapanır, tabanda değil."""
    assert len(_signals(_snapshot(funding=[-0.0001, -0.0001, -0.0001]))) == 1


def test_missing_funding_history_keeps_the_gate_closed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Açık varsaymak, filtrenin var olmadığı bir dönemde işlem açmak olurdu."""
    with caplog.at_level(logging.INFO, logger="strategies.downtrend_rally"):
        signals = _signals(_snapshot(funding=None))
    assert signals == []
    assert any("kapı kapalı" in record.getMessage() for record in caplog.records)


def test_a_symbol_that_is_not_in_the_weakest_fifth_is_not_traded() -> None:
    """Kapı kesitseldir: evren daha da zayıfsa aday sembol "göreli zayıf" değildir."""
    assert _signals(_snapshot(peers=_peer_falling(len(_candidate())))) == []


def test_a_universe_too_small_for_a_bottom_fifth_produces_nothing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """3 sembollük bir kümede "alt %20" ile "en zayıfı" aynı şeydir; kapı kesitsel kalmaz."""
    with caplog.at_level(logging.INFO, logger="strategies.downtrend_rally"):
        signals = _signals(_snapshot(peer_count=2))
    assert signals == []
    assert any("en az 5 gerekiyor" in record.getMessage() for record in caplog.records)


def test_an_uptrend_regime_is_not_traded() -> None:
    """Rejim kapısı: 200 EMA'nın üstünde satmak modeli bir dönüş bahsine çevirirdi."""
    rising = _peer_rising()
    snapshot = market(
        {SYMBOL: rising.copy(), **{name: rising.copy() for name in PEERS}},
        as_of=rising.index[-1],
        funding={SYMBOL: funding_series(NEUTRAL_FUNDING, end=rising.index[-1])},
    )
    assert DowntrendRally().generate_signals(snapshot) == []


def test_a_level_already_touched_on_the_previous_bar_is_not_signalled_again() -> None:
    """Dokunuş İLK barda aranır: yoksa fiyat bantta oyalandıkça model her turda sinyal üretir."""
    assert len(_signals(_snapshot(_candidate(bounce=(0.03, 0.03))))) == 1
    assert _signals(_snapshot(_candidate(bounce=(0.03, 0.03, 0.002)))) == []


def test_a_rally_top_beyond_the_atr_ceiling_skips_the_trade(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Stop tavana ÇEKİLMEZ (kural 14): geniş düzeltme tepesi işlemi eler."""
    candidate = _candidate()
    candidate.loc[candidate.index[-1], "high"] = float(candidate["close"].iloc[-1]) * 1.10
    with caplog.at_level(logging.INFO, logger="strategies.downtrend_rally"):
        signals = _signals(_snapshot(candidate))
    assert signals == []
    assert any("tavanı" in record.getMessage() for record in caplog.records)


def test_not_enough_history_produces_nothing() -> None:
    """Kısmi pencereyle 7 günlük getiri üretmek kesitsel eşiği sessizce bozardı."""
    candidate = _candidate().iloc[-20:]
    assert _signals(_snapshot(candidate)) == []


def test_a_symbol_that_is_missing_the_as_of_bar_is_skipped() -> None:
    candidate = _candidate()
    snapshot = _snapshot(candidate)
    trimmed = MarketData(
        ohlcv={**snapshot.ohlcv, SYMBOL: candidate.iloc[:-1]},
        btc=snapshot.btc,
        funding=snapshot.funding,
        as_of=snapshot.as_of,
    )
    assert DowntrendRally().generate_signals(trimmed) == []
