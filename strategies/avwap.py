"""Çapalı VWAP (AVWAP) sapması — yarışmacı model, iki yönlü.

Tez: bir swing dönüş noktasından itibaren hacim ağırlıklı ortalama fiyat, o hareket boyunca
pozisyon alanların ORTAK MALİYETİDİR. Fiyatın bu maliyetten hacim ağırlıklı 2 standart sapma
uzaklaşması bir aşırılıktır ve çizginin kendisi bir mıknatıstır — hedef bu yüzden sabit bir R
katı değil, AVWAP çizgisinin kendisidir.

Çapa **en son kesinleşmiş zigzag pivotudur.** `core.indicators.zigzag_pivots` listesinin son
elemanı, reversal ile henüz onaylanmamış CANLI uçtur (bkz. o modülün docstring'i) ve bir
sonraki barda yer değiştirebilir; onu çapa yapmak, her barda başka bir yerden başlayan bir
AVWAP demekti — ölçülen şey artık "şu swing'in ortak maliyeti" olmazdı. Bu yüzden son eleman
her koşulda atılır. Kısa bacakların çift hâlinde elenmesi (bkz. `_merge_short_legs`) bazen
canlı ucu zaten silmiş olur; o durumda bu seçim çapayı bir pivot daha geriye alır — fazladan
eski bir çapa, oynak bir çapadan iyidir.

Kurulumlar:

- **Long** — çapa bir swing DİP, kapanış AVWAP−2σ'nın altında ve fiyat hâlâ çapa fiyatının
  ÜSTÜNDE. Son koşul tezin sınırıdır: fiyat çapanın altına düştüyse o swing'in "dip" olduğu
  iddiası ölmüştür, ortada aşırılık değil yeni bir düşüş bacağı vardır.
- **Short** — çapa bir swing TEPE, kapanış AVWAP+2σ'nın üstünde VE BTC kendi 200 EMA'sının
  altında. BTC kapısının gerekçesi `strategies/meanrev.py`'deki ile birebir aynıdır: boğa
  rejiminde altcoin'lerin "aşırı" sapmaları sürekli tetiklenir ve filtresiz bir short kolu
  sinyal kalitesini değil yalnızca rejim yönünü ölçer — projenin ana sorusu (short'lar daha
  mı başarılı) tam da orada kirlenirdi.

**Eğim filtresi.** AVWAP'ın kendisi işleme karşı hızlı hareket ediyorsa girilmez: çizgi bir
mıknatıs değil, kaçan bir hedeftir ve "ortalamaya dönüş" tezi orada geçerli değildir. "Sert"
ölçüsü projenin ortak birimindedir (ATR): son `SLOPE_LOOKBACK_BARS` barda AVWAP işlemin
aleyhine `SLOPE_LIMIT_ATR`×ATR'den fazla kaydıysa sinyal üretilmez. Yüzde yerine ATR
kullanılması, eşiğin sembolün oynaklığına göre aynı anlama gelmesi içindir.

**Stop çapanın ötesi veya 1.5×ATR — hangisi genişse**, ve mesafe `max_stop_atr_multiple`
tavanını aşarsa işlem ATLANIR (kural 14). Çapa uzaktaysa bu sık olacaktır; stop tavana
çekilmez, çünkü çekmek modelin "tez çapanın ötesinde ölür" iddiasını sessizce başka bir
iddiaya çevirirdi.

**Hedefler iki kademelidir:** ±1σ'da pozisyonun yarısı, AVWAP çizgisinde kalanı. Tek hedefle
(yalnızca AVWAP) model yolun yarısında dönen hareketleri hiç hasat edemez; yalnızca ±1σ ile
de tezin bittiği yeri (çizginin kendisi) hiç ölçemezdi.

Rollere dikkat: bu modül boyut/komisyon/bakiye hesaplamaz (kural 1/2/3/7) ve gösterge
matematiğini kendisi yazmaz — AVWAP, hacim ağırlıklı sapma, EMA, ATR ve zigzag pivotları
`core/indicators.py`'dedir.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Mapping

import pandas as pd

from core.config import get_setting, load_config
from core.indicators import (
    AnchoredVwap,
    Pivot,
    anchored_vwap,
    average_true_range,
    bars_until,
    ema,
    zigzag_pivots,
)
from strategies.base import Direction, MarketData, Signal, Strategy, TakeProfit

logger = logging.getLogger(__name__)

# Pivot tanımı `strategies/confluence.py` ve `strategies/downtrend_rally.py` ile aynı:
# aynı zigzag'ın iki farklı eşikle okunması, iki modelin "swing" derken farklı şeyler
# kastetmesi demekti.
ZIGZAG_PCT_THRESHOLD = 0.05
MIN_LEG_DURATION_BARS = 8
ENTRY_STD = 2.0
PARTIAL_STD = 1.0
PARTIAL_FRACTION = 0.5
STOP_ATR_MULTIPLE = 1.5
SLOPE_LOOKBACK_BARS = 5
SLOPE_LIMIT_ATR = 0.5
# Çapadan sonra bu kadar bar geçmeden σ bir "dağılım" değil, iki üç barın gürültüsüdür.
MIN_ANCHOR_BARS = 10
BTC_REGIME_EMA_PERIOD = 200


class Avwap(Strategy):
    name = "avwap"
    allowed_directions: list[Direction] = ["long", "short"]

    def __init__(
        self,
        *,
        config: Mapping[str, Any] | None = None,
        pct_threshold: float = ZIGZAG_PCT_THRESHOLD,
        min_leg_bars: int = MIN_LEG_DURATION_BARS,
        entry_std: float = ENTRY_STD,
        partial_std: float = PARTIAL_STD,
        stop_atr_multiple: float = STOP_ATR_MULTIPLE,
        slope_lookback_bars: int = SLOPE_LOOKBACK_BARS,
        slope_limit_atr: float = SLOPE_LIMIT_ATR,
        min_anchor_bars: int = MIN_ANCHOR_BARS,
        btc_regime_ema_period: int = BTC_REGIME_EMA_PERIOD,
    ) -> None:
        settings = dict(config) if config is not None else load_config()
        # ATR periyodu ve stop tavanı config'ten gelir: ikisi de tüm modeller için ortaktır.
        self._atr_period = int(get_setting(settings, "trailing.atr_period"))
        self._max_stop_atr_multiple = float(get_setting(settings, "max_stop_atr_multiple"))
        self._pct_threshold = pct_threshold
        self._min_leg_bars = min_leg_bars
        self._entry_std = entry_std
        self._partial_std = partial_std
        self._stop_atr_multiple = stop_atr_multiple
        self._slope_lookback_bars = slope_lookback_bars
        self._slope_limit_atr = slope_limit_atr
        self._min_anchor_bars = min_anchor_bars
        self._btc_regime_ema_period = btc_regime_ema_period

    def generate_signals(
        self,
        market: MarketData,
        peer_signals: Mapping[str, tuple[Signal, ...]] | None = None,
    ) -> list[Signal]:
        shorts_allowed, btc_note = self._btc_regime(market)
        signals: list[Signal] = []
        for symbol in sorted(market.ohlcv):
            signal = self._evaluate(
                symbol, market, shorts_allowed=shorts_allowed, btc_note=btc_note
            )
            if signal is not None:
                signals.append(signal)
        return signals

    def _btc_regime(self, market: MarketData) -> tuple[bool, str]:
        """Short kapısı: BTC kapanışı 200 EMA'sının altında mı? (bkz. strategies/meanrev.py)

        EMA hesaplanamıyorsa kapı KAPALI kalır: açık varsaymak, filtrenin var olmadığı bir
        dönemde short açmak — modeli sessizce başka bir modele çevirmek — olurdu.
        """
        btc = bars_until(market.btc, market.as_of)
        if btc.empty:
            logger.info("%s: BTC referans verisi boş, short kapısı kapalı", self.name)
            return False, "BTC verisi yok"
        btc_ema = ema(btc["close"], self._btc_regime_ema_period)
        if btc_ema is None:
            logger.info(
                "%s: BTC %d EMA'sı için yeterli bar yok, short kapısı kapalı",
                self.name, self._btc_regime_ema_period,
            )
            return False, f"BTC {self._btc_regime_ema_period} EMA hesaplanamadı"
        btc_close = float(btc["close"].iloc[-1])
        below = btc_close < btc_ema
        return below, (
            f"BTC {btc_close:.6g}, {self._btc_regime_ema_period} EMA {btc_ema:.6g} "
            f"{'altında' if below else 'üstünde'}"
        )

    def _evaluate(
        self, symbol: str, market: MarketData, *, shorts_allowed: bool, btc_note: str
    ) -> Signal | None:
        frame = bars_until(market.ohlcv[symbol], market.as_of)
        if frame.empty or frame.index[-1] != market.as_of:
            return None

        anchor = self._anchor(frame)
        if anchor is None:
            return None
        band = anchored_vwap(frame, anchor=anchor.time)
        if band is None or band.bars < self._min_anchor_bars or band.deviation <= 0.0:
            return None

        close = float(frame["close"].iloc[-1])
        upper = band.value + self._entry_std * band.deviation
        lower = band.value - self._entry_std * band.deviation

        if anchor.kind == "low" and close < lower:
            if close <= anchor.price:
                # Fiyat çapanın altında: o swing'in "dip" olduğu iddiası ölmüştür.
                logger.info(
                    "%s %s: long atlandı, kapanış (%.10g) çapa dibinin (%.10g) altında — "
                    "aşırılık değil yeni bir düşüş bacağı",
                    self.name, symbol, close, anchor.price,
                )
                return None
            direction: Direction = "long"
        elif anchor.kind == "high" and close > upper:
            if not shorts_allowed:
                logger.info(
                    "%s %s: short sinyali bastırıldı, BTC rejim kapısı kapalı (%s)",
                    self.name, symbol, btc_note,
                )
                return None
            direction = "short"
        else:
            return None

        atr = average_true_range(frame, self._atr_period)
        if atr is None or atr <= 0.0:
            logger.info(
                "%s %s: %s sinyali atlandı, ATR(%d) hesaplanamadı",
                self.name, symbol, direction, self._atr_period,
            )
            return None

        slope = self._slope(frame, anchor=anchor)
        if slope is None:
            return None
        against = -slope if direction == "long" else slope
        if against > self._slope_limit_atr * atr:
            logger.info(
                "%s %s: %s atlandı, AVWAP eğimi işleme sert ters — son %d barda %.6g "
                "(%.2f×ATR(%d), sınır %g×)",
                self.name, symbol, direction, self._slope_lookback_bars, slope,
                against / atr, self._atr_period, self._slope_limit_atr,
            )
            return None

        anchor_stop = anchor.price
        distance = atr * self._stop_atr_multiple
        stop_price = (
            min(anchor_stop, close - distance)
            if direction == "long"
            else max(anchor_stop, close + distance)
        )
        multiple = abs(close - stop_price) / atr
        if multiple > self._max_stop_atr_multiple:
            logger.info(
                "%s %s: %s atlandı, çapa (%.10g) %.2f×ATR(%d) uzakta ve tavanı (%.2f×) "
                "aşıyor — stop tavana ÇEKİLMEZ (kural 14)",
                self.name, symbol, direction, anchor_stop, multiple, self._atr_period,
                self._max_stop_atr_multiple,
            )
            return None

        partial = (
            band.value - self._partial_std * band.deviation
            if direction == "long"
            else band.value + self._partial_std * band.deviation
        )
        return Signal(
            symbol=symbol,
            direction=direction,
            stop_price=stop_price,
            take_profits=(
                TakeProfit(price=partial, fraction=PARTIAL_FRACTION),
                TakeProfit(price=band.value, fraction=1.0 - PARTIAL_FRACTION),
            ),
            reason=(
                f"çapa {'swing dip' if anchor.kind == 'low' else 'swing tepe'} "
                f"{anchor.price:.6g} ({anchor.time:%Y-%m-%d %H:%M} UTC, {band.bars} bar); "
                f"AVWAP {band.value:.6g}, hacim ağırlıklı σ {band.deviation:.6g}; "
                f"kapanış {close:.6g} "
                f"{'alt' if direction == 'long' else 'üst'} {self._entry_std:g}σ bandının "
                f"({lower if direction == 'long' else upper:.6g}) "
                f"{'altında' if direction == 'long' else 'üstünde'}; "
                f"eğim son {self._slope_lookback_bars} barda {slope:+.6g} "
                f"({against / atr:+.2f}×ATR aleyhte, sınır {self._slope_limit_atr:g}×); "
                f"stop çapanın ötesi veya {self._stop_atr_multiple:g}×ATR({self._atr_period}) "
                f"({stop_price:.6g}, {multiple:.2f}×ATR, tavan "
                f"{self._max_stop_atr_multiple:g}×); hedef {self._partial_std:g}σ "
                f"{partial:.6g} (%{PARTIAL_FRACTION * 100:.0f}) ve AVWAP {band.value:.6g}"
                + (f"; short rejim kapısı açık: {btc_note}" if direction == "short" else "")
            ),
        )

    def _anchor(self, frame: pd.DataFrame) -> Pivot | None:
        """Son KESİNLEŞMİŞ zigzag pivotu; teyit ölçüsü zigzag'ın kendi eşiğidir.

        Neden "listenin sonuncusunu at" değil: zigzag kısa bacakları ÇİFT hâlinde eler
        (bkz. `core.indicators._merge_short_legs`), yani canlı uç bazen zaten silinmiş
        olur ve son eleman teyitli bir pivottur. Onu da atmak çapayı bir swing geriye —
        bazen serinin başındaki sentetik ankraja — kaydırırdı. Teyit doğrudan sorulur:
        pivottan sonra fiyat `pct_threshold` kadar ters yöne dönmüş mü? Bu, pivot
        matematiğinin ikinci bir uygulaması değil, aynı eşikle yapılmış bir SÜZMEDİR.
        """
        pivots = zigzag_pivots(
            frame, pct_threshold=self._pct_threshold, min_leg_bars=self._min_leg_bars
        )
        for pivot in reversed(pivots):
            if self._is_confirmed(frame, pivot):
                return pivot
        return None

    def _is_confirmed(self, frame: pd.DataFrame, pivot: Pivot) -> bool:
        after = frame.loc[pivot.time :].iloc[1:]
        if after.empty:
            return False
        if pivot.kind == "high":
            return float(after["low"].min()) <= pivot.price * (1.0 - self._pct_threshold)
        return float(after["high"].max()) >= pivot.price * (1.0 + self._pct_threshold)

    def _slope(self, frame: pd.DataFrame, *, anchor: Pivot) -> float | None:
        """AVWAP'ın son `slope_lookback_bars` bardaki değişimi; ölçülemiyorsa None.

        Karşılaştırma AYNI çapadan hesaplanmış iki AVWAP arasındadır: çapayı da geriye
        kaydırmak iki farklı çizginin farkını "eğim" diye raporlamak olurdu.
        """
        if len(frame) <= self._slope_lookback_bars:
            return None
        past = frame.iloc[: -self._slope_lookback_bars]
        if past.empty or past.index[-1] <= anchor.time:
            return None
        earlier = anchored_vwap(past, anchor=anchor.time)
        current = anchored_vwap(frame, anchor=anchor.time)
        if earlier is None or current is None:
            return None
        return current.value - earlier.value
