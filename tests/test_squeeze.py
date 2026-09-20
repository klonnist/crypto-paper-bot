"""strategies/squeeze.py: Bollinger sıkışması + hacim teyitli kırılım.

Modelin ölçüm değeri üç kapının GERÇEKTEN tutmasına bağlı: sıkışma yoksa model salt
kırılım sayar; hacim teyidi delinirse tezin kendisi (hacmin katılımı) hiç test edilmemiş
olur; stop tavanı (kural 14) uygulanmazsa geniş sıkışmalar modeli bandın dışına taşır ve
R başına maliyeti diğer modellerle kıyaslanamaz hâle getirir. Testler bu üçünü, bantların
kırılım barını hariç tutmasını ve sinyalin doğrulama kapısından geçtiğini sabitler.
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd
import pytest

from core.indicators import average_true_range, bollinger
from core.validate import validate_signal
from strategies.base import MarketData, Signal
from strategies.squeeze import Squeeze
from tests.helpers_market import frame, market

SYMBOL = "BTC-USDT-SWAP"
BARS = 120
BASE = 100.0
TAIL_BARS = 40  # sıkışmaya giren kuyruk; son 50 pencerenin en az %20'si burada daralır
TIGHTENING = 0.5  # kuyruğun başındaki salınım
QUIET = 0.05  # kuyruğun sonundaki (sıkışmanın en dar noktasındaki) salınım
NOISY = 5.0  # sıkışma öncesi salınım
SPREAD = 0.5
BREAKOUT_UP = 102.0
BREAKOUT_DOWN = 98.0
CONFIRMED_VOLUME = 2.0  # 20 barlık ortalama 1.0 olduğu için doğrudan "×" demek


def _closes(
    *,
    breakout: float,
    prefix_swing: float = NOISY,
    tail_swing_start: float = TIGHTENING,
    tail_swing_end: float = QUIET,
) -> list[float]:
    """`prefix_swing` kadar salınan geçmiş + giderek daralan `TAIL_BARS` bar + kırılım barı.

    Kuyruk DARALARAK iner (0.5 -> 0.05): sabit genişlikte bir kuyruk, son 50 pencerenin
    onlarcasını birbirine eşit genişlikte bırakır ve "en dar %20" eşiği kayan nokta
    eşitliğine indirgenirdi. Daralan kuyrukta sıralama kesindir.
    """
    closes = [
        BASE + (prefix_swing if index % 2 == 0 else -prefix_swing)
        for index in range(BARS - 1 - TAIL_BARS)
    ]
    for index in range(TAIL_BARS):
        swing = tail_swing_start + (tail_swing_end - tail_swing_start) * index / (TAIL_BARS - 1)
        closes.append(BASE + (swing if index % 2 == 0 else -swing))
    return closes + [breakout]


def _volumes(last: float) -> list[float]:
    return [1.0] * (BARS - 1) + [last]


def _candles(
    *,
    breakout: float = BREAKOUT_UP,
    prefix_swing: float = NOISY,
    tail_swing_start: float = TIGHTENING,
    tail_swing_end: float = QUIET,
    volume: float = CONFIRMED_VOLUME,
) -> pd.DataFrame:
    return frame(
        _closes(
            breakout=breakout,
            prefix_swing=prefix_swing,
            tail_swing_start=tail_swing_start,
            tail_swing_end=tail_swing_end,
        ),
        spread=SPREAD,
        volumes=_volumes(volume),
    )


def _bandwidth_threshold(candles: pd.DataFrame) -> float:
    """Modelin eşiği: kırılım barı hariç son 50 pencerenin en dar %20'si."""
    closes = candles["close"].iloc[:-1]
    widths = []
    for offset in range(50):
        bands = bollinger(closes.iloc[: len(closes) - offset], 20, 2.0)
        assert bands is not None
        widths.append((bands.upper - bands.lower) / bands.middle)
    return float(np.quantile(widths, 0.20))


def _signals(candles: pd.DataFrame | None = None) -> list[Signal]:
    return Squeeze().generate_signals(market({SYMBOL: candles if candles is not None else _candles()}))


def _squeeze_bands(candles: pd.DataFrame):
    """Modelin gördüğü bantlar: kırılım barı HARİÇ (bkz. strategies/squeeze.py)."""
    bands = bollinger(candles["close"].iloc[:-1], 20, 2.0)
    assert bands is not None
    return bands


# --------------------------------------------------------------------------- #
# Sözleşme
# --------------------------------------------------------------------------- #
def test_is_a_competitor_not_a_benchmark() -> None:
    """Yarışmacı model kendi boyutunu belirleyemez (kural 3/11/15)."""
    strategy = Squeeze()
    assert strategy.name == "squeeze"
    assert strategy.is_benchmark is False
    assert strategy.is_meta is False
    assert strategy.allowed_directions == ["long", "short"]


def test_registry_resolves_the_model_name() -> None:
    from strategies.registry import build

    assert isinstance(build("squeeze"), Squeeze)


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

    assert "squeeze" not in get_setting(load_config(), "models")
    assert "squeeze" in REGISTRY


def test_signal_uses_the_shared_risk_sizing() -> None:
    signal = _signals()[0]
    assert signal.sizing == "risk"
    assert signal.notional_fraction is None


# --------------------------------------------------------------------------- #
# Sinyal üretilen senaryolar
# --------------------------------------------------------------------------- #
def test_upward_break_out_of_a_squeeze_with_volume_opens_a_long() -> None:
    signals = _signals()
    assert len(signals) == 1
    assert signals[0].direction == "long"


def test_downward_break_out_of_a_squeeze_with_volume_opens_a_short() -> None:
    signals = _signals(_candles(breakout=BREAKOUT_DOWN))
    assert len(signals) == 1
    assert signals[0].direction == "short"


def test_long_stop_is_the_far_end_of_the_squeeze_range() -> None:
    """Tez orada biter: kırılım geçersizse fiyat aralığın içine döner ve diğer uca ulaşır."""
    candles = _candles()
    expected = float(candles["low"].iloc[-21:-1].min())
    assert _signals(candles)[0].stop_price == pytest.approx(expected)


def test_short_stop_is_the_far_end_of_the_squeeze_range() -> None:
    candles = _candles(breakout=BREAKOUT_DOWN)
    expected = float(candles["high"].iloc[-21:-1].max())
    assert _signals(candles)[0].stop_price == pytest.approx(expected)


def test_volume_exactly_at_the_threshold_is_confirmation_enough() -> None:
    """Eşik "1.5 katı ÜSTÜNDE" değil "1.5 katı" olarak okunur; sınır davranışı yazılı olmalı."""
    assert len(_signals(_candles(volume=1.5))) == 1


def test_bandwidth_is_measured_on_the_history_not_on_the_breakout_bar() -> None:
    """Kırılım barının kendi hareketi aynı barın sapmasını şişirir; en güçlü kırılım kendini elerdi."""
    candles = _candles()
    inflated = bollinger(candles["close"], 20, 2.0)
    squeezed = _squeeze_bands(candles)
    assert inflated is not None
    threshold = _bandwidth_threshold(candles)
    inflated_width = (inflated.upper - inflated.lower) / inflated.middle
    squeezed_width = (squeezed.upper - squeezed.lower) / squeezed.middle

    assert inflated_width > threshold, "test verisi kendi kendini eleyen kurulumu kurmalı"
    assert squeezed_width <= threshold
    assert len(_signals(candles)) == 1


def test_volume_average_excludes_the_breakout_bar() -> None:
    """Ortalama kırılım barını içerseydi barın kendi hacmi eşiği yukarı çekerdi."""
    candles = _candles(volume=CONFIRMED_VOLUME)
    history_average = float(candles["volume"].iloc[-21:-1].mean())
    assert history_average == pytest.approx(1.0)
    assert f"hacim {CONFIRMED_VOLUME / history_average:.2f}×" in _signals(candles)[0].reason


def test_signals_pass_the_validation_gate() -> None:
    for candles in (_candles(), _candles(breakout=BREAKOUT_DOWN)):
        signal = _signals(candles)[0]
        validate_signal(
            signal,
            entry_price=float(candles["close"].iloc[-1]),
            allowed_directions=Squeeze().allowed_directions,
            symbol_universe=[SYMBOL],
            is_benchmark=False,
        )


def test_model_asks_for_no_trailing_stop_and_no_take_profit() -> None:
    """Trailing ve TP ayrı tezlerdir; bu modelin tezi sıkışmadan çıkıştır."""
    signal = _signals()[0]
    assert signal.trailing_atr is None
    assert signal.take_profits == ()


def test_reason_carries_the_measurement_next_to_the_threshold() -> None:
    """Eşiksiz bir gerekçe, denetimi commit geçmişinde eşik aramaya zorlardı."""
    reason = _signals()[0].reason
    assert "Bollinger(20,2) bant genişliği" in reason
    assert "son 50 barın en dar %20 eşiği" in reason
    assert "hacim 2.00× (20 bar ortalaması, eşik 1.5×)" in reason
    assert "yukarı kırılım" in reason
    assert "stop sıkışma aralığının karşı ucu" in reason
    assert "ATR(14), tavan 3×)" in reason


# --------------------------------------------------------------------------- #
# Sinyal ÜRETİLMEYEN senaryolar — modelin ölçüm değeri bunlarda
# --------------------------------------------------------------------------- #
def test_a_breakout_without_volume_confirmation_is_suppressed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Sahte kırılımların ana filtresi bu: teyit yoksa işlem YOK, ve bu sessiz olmaz."""
    with caplog.at_level(logging.INFO, logger="strategies.squeeze"):
        signals = _signals(_candles(volume=1.49))
    assert signals == []
    assert any("hacim teyidi yok" in record.getMessage() for record in caplog.records)


def test_the_same_breakout_trades_once_volume_confirms_it() -> None:
    """Tek değişken hacim olmalı: fiyat verisi birebir aynı, sonuç farklı."""
    assert _signals(_candles(volume=1.0)) == []
    assert len(_signals(_candles(volume=1.5))) == 1


def test_a_breakout_without_a_squeeze_is_not_traded() -> None:
    """Sıkışma kapısı delinirse model salt kırılım sayar; ölçtüğü tez kalmaz.

    Veri tersine çevrilmiştir: geçmiş sıkışık, son 20 bar geniş. Kırılım ve hacim teyidi
    yerindedir — tek eksik, bant genişliğinin en dar %20'de olması.
    """
    candles = _candles(
        breakout=103.0, prefix_swing=QUIET, tail_swing_start=1.0, tail_swing_end=1.0
    )
    bands = _squeeze_bands(candles)
    assert float(candles["close"].iloc[-1]) > bands.upper, "test verisi bandı gerçekten kırmalı"
    width = (bands.upper - bands.lower) / bands.middle
    assert width > _bandwidth_threshold(candles), "test verisinde sıkışma OLMAMALI"
    assert _signals(candles) == []


def test_a_squeeze_without_a_breakout_is_not_traded() -> None:
    candles = _candles(breakout=BASE)
    bands = _squeeze_bands(candles)
    close = float(candles["close"].iloc[-1])
    assert bands.lower <= close <= bands.upper, "test verisi bandın İÇİNDE kalmalı"
    assert _signals(candles) == []


def test_a_squeeze_range_wider_than_the_atr_ceiling_skips_the_trade(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Stop tavana ÇEKİLMEZ (kural 14): model "karşı uç" tezini korur, işlemi atlar."""
    candles = _candles()
    deep = candles.index[-21]  # sıkışma penceresinin İÇİNDE, ATR(14) penceresinin dışında
    candles.loc[deep, "low"] = BASE - 30.0
    with caplog.at_level(logging.INFO, logger="strategies.squeeze"):
        signals = Squeeze().generate_signals(market({SYMBOL: candles}))
    assert signals == []
    assert any("tavanı" in record.getMessage() for record in caplog.records)


def test_not_enough_history_for_the_bandwidth_window_produces_nothing() -> None:
    """Kısmi pencereyle "en dar %20" üretmek, eşiği sembolden sembole farklı bir ölçüye çevirirdi."""
    candles = _candles().iloc[-60:]
    assert _signals(candles) == []


def test_a_symbol_that_is_missing_the_as_of_bar_is_skipped() -> None:
    candles = _candles()
    snapshot = MarketData(
        ohlcv={SYMBOL: candles.iloc[:-1]},
        btc=candles,
        funding={},
        as_of=candles.index[-1],
    )
    assert Squeeze().generate_signals(snapshot) == []
