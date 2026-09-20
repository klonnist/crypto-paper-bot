"""Düşüş trendinde ralli satışı — yalnızca SHORT açan yarışmacı model.

Tez: düşüş trendindeki bir sembolde yukarı hareket bir dönüş değil, bir NEFES ARASIDIR.
Model bu nefesi, düşüş bacağının Fibonacci düzeltmesine ya da 20 EMA'ya yukarıdan yaklaşan
fiyatın zayıflık gösterdiği anda satar.

Beş kapı sırayla uygulanır:

1. **Rejim** — kapanış 200 EMA'nın altında VE 50 EMA < 200 EMA. Tek bir ortalama yetmez:
   fiyat 200 EMA'nın altına yeni sarkmışken ortalamalar hâlâ yükseliş dizilimindeyse
   ölçülen şey trend değil, bir sapmadır.
2. **Göreli zayıflık** — 7 günlük getiri EVRENİN alt %20'sinde. Bu kapı kesitseldir
   (momentum modelindeki gibi): "düşüyor" mutlak bir ifade değil, sıralamada bir yerdir.
   Piyasanın tamamı düşerken herkesi satmak, modeli bir piyasa yönü bahsine çevirirdi.
3. **Ralli** — son düşüş bacağının 0.382-0.618 düzeltme bandına ya da 20 EMA'ya AŞAĞIDAN
   dokunuş. "Aşağıdan" şart: seviyeye yukarıdan sarkmak düzeltme değil, kırılımdır.
4. **Tetik** — RSI(14) < 45 VE hacim 20 bar ortalamasının üstünde; ikisi birden. Zayıf
   momentumla gelen bir ralliyi satmak tezin kendisidir; hacim ise ralliye karşı gelen
   arzın teyididir. Tek başına biri, tezin yalnızca yarısını test ederdi.
5. **Funding kapısı** — son 3 periyodun funding ortalaması %-0.01'in altındaysa GİRİLMEZ.
   Negatif funding shortların ödediği anlamına gelir: kalabalık zaten short taraftadır ve
   bir short squeeze'in yakıtı oradadır. Bu kapı bir kâr filtresi değil, ölçüm filtresidir
   — squeeze'e yakalanan işlemler modelin sinyal kalitesini değil kalabalıklığı ölçer.

**Dokunuş İLK barda aranır.** Seviye bir önceki barda da dokunulmuşsa kurulum orada
oluşmuştur; aksi hâlde fiyat bantta oyalandığı sürece model her turda aynı sinyali üretir
ve `core/portfolio.py` bunları `duplicate_position` ile reddederken (kural 15) modelin
sinyal sayısı ölçülemeyen bir nedenle şişerdi.

**Funding verisi yoksa kapı KAPALI kalır.** Açık varsaymak, filtrenin var olmadığı bir
dönemde işlem açmak — yani modeli sessizce başka bir modele çevirmek olurdu (aynı gerekçe
`strategies/meanrev.py`'nin BTC rejim kapısında da geçerli). Eksik veriyi ortalamayla
doldurmak ise `core/funding.py`'nin açıkça reddettiği şeydir.

**Stop düzeltme tepesinin üstü veya 1.5×ATR — hangisi genişse**, ve mesafe
`max_stop_atr_multiple` tavanını aşarsa işlem ATLANIR (kural 14). Stop tavana çekilmez:
çekmek, modelin "ralli tepesi aşılırsa tez ölmüştür" iddiasını başka bir iddiaya çevirirdi.

**Hedef önceki dip, kalanı trailing.** Bacağın dibi tezin bittiği yerdir; pozisyonun yarısı
orada kapanır, kalanı 1×ATR trailing ile taşınır (uygulaması `core/engine.py`'de, kural 9).
Yarı yarıya bölünmesi bir tercih: tezin bittiği yerde kârın yarısını almak ile trendin
devamını ölçmeye devam etmek arasında, ikisini de ölçülebilir bırakan tek nokta odur.

Rollere dikkat: bu modül boyut/komisyon/bakiye hesaplamaz (kural 1/2/3/7), trailing'i kendisi
yürütmez (kural 9) ve gösterge matematiğini kendisi yazmaz — EMA, RSI, ATR, SMA, zigzag
pivotları ve Fibonacci seviyeleri `core/indicators.py`'dedir.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Mapping

import numpy as np
import pandas as pd

from core.config import get_setting, load_config
from core.data import bar_duration
from core.indicators import (
    average_true_range,
    bars_until,
    ema,
    fib_levels,
    rsi,
    sma,
    zigzag_pivots,
)
from strategies.base import Direction, MarketData, Signal, Strategy, TakeProfit

logger = logging.getLogger(__name__)

FAST_EMA_PERIOD = 50
SLOW_EMA_PERIOD = 200
RALLY_EMA_PERIOD = 20
LOOKBACK_DAYS = 7
WEAKNESS_QUANTILE = 0.20
# Alt %20'nin tek bir sembolü adlandırabilmesi için gereken en küçük evren. Daha küçük
# bir kümede "alt %20" ile "en zayıfı" aynı şeydir ve kapı kesitsel olmaktan çıkar.
MIN_RANKED_SYMBOLS = 5
RETRACEMENT_RATIOS: tuple[float, ...] = (0.382, 0.618)
# Pivot tanımı `strategies/confluence.py` ile aynı: aynı zigzag'ın iki farklı eşikle
# okunması, iki modelin "son düşüş bacağı" derken farklı bacakları kastetmesi demekti.
ZIGZAG_PCT_THRESHOLD = 0.05
MIN_LEG_DURATION_BARS = 8
RSI_PERIOD = 14
RSI_TRIGGER = 45.0
VOLUME_PERIOD = 20
FUNDING_PERIODS = 3
FUNDING_FLOOR = -0.0001  # %-0.01
STOP_ATR_MULTIPLE = 1.5
TRAILING_ATR_MULTIPLE = 1.0
TARGET_FRACTION = 0.5


@dataclass(frozen=True, kw_only=True)
class _Leg:
    """Son düşüş bacağı: teyitli bir zigzag zirvesinden, o zirveden sonraki EN DÜŞÜK dibe."""

    start_price: float
    start_time: pd.Timestamp
    low_price: float
    low_time: pd.Timestamp


@dataclass(frozen=True, kw_only=True)
class _Touch:
    """Rallinin dokunduğu seviye ve hangi tanımla dokunduğu."""

    level: float
    label: str


class DowntrendRally(Strategy):
    name = "downtrend_rally"
    allowed_directions: list[Direction] = ["short"]

    def __init__(
        self,
        *,
        config: Mapping[str, Any] | None = None,
        fast_ema_period: int = FAST_EMA_PERIOD,
        slow_ema_period: int = SLOW_EMA_PERIOD,
        rally_ema_period: int = RALLY_EMA_PERIOD,
        lookback_days: int = LOOKBACK_DAYS,
        weakness_quantile: float = WEAKNESS_QUANTILE,
        rsi_period: int = RSI_PERIOD,
        rsi_trigger: float = RSI_TRIGGER,
        volume_period: int = VOLUME_PERIOD,
        funding_floor: float = FUNDING_FLOOR,
        stop_atr_multiple: float = STOP_ATR_MULTIPLE,
        trailing_atr_multiple: float = TRAILING_ATR_MULTIPLE,
    ) -> None:
        settings = dict(config) if config is not None else load_config()
        # ATR periyodu ve stop tavanı config'ten gelir: ikisi de tüm modeller için ortaktır.
        self._atr_period = int(get_setting(settings, "trailing.atr_period"))
        self._max_stop_atr_multiple = float(get_setting(settings, "max_stop_atr_multiple"))
        # Geriye bakış GÜN cinsinden tanımlı (bkz. strategies/momentum.py): bar sayısı
        # timeframe'den türetilir, yoksa timeframe değiştiğinde tez sessizce değişirdi.
        self._lookback_bars = int(
            pd.Timedelta(days=lookback_days)
            / bar_duration(str(get_setting(settings, "timeframe")))
        )
        if self._lookback_bars <= 0:
            raise ValueError(
                f"{lookback_days} günlük geriye bakış bu timeframe'de tek bara sığmıyor"
            )
        self._lookback_days = lookback_days
        self._fast_ema_period = fast_ema_period
        self._slow_ema_period = slow_ema_period
        self._rally_ema_period = rally_ema_period
        self._weakness_quantile = weakness_quantile
        self._rsi_period = rsi_period
        self._rsi_trigger = rsi_trigger
        self._volume_period = volume_period
        self._funding_floor = funding_floor
        self._stop_atr_multiple = stop_atr_multiple
        self._trailing_atr_multiple = trailing_atr_multiple

    def generate_signals(
        self,
        market: MarketData,
        peer_signals: Mapping[str, tuple[Signal, ...]] | None = None,
    ) -> list[Signal]:
        returns = self._lookback_returns(market)
        if len(returns) < MIN_RANKED_SYMBOLS:
            logger.info(
                "%s: 7 günlük getirisi ölçülebilen sembol sayısı %d, kesitsel alt %%%.0f "
                "kapısı için en az %d gerekiyor; bu turda sinyal üretilmedi",
                self.name, len(returns), self._weakness_quantile * 100, MIN_RANKED_SYMBOLS,
            )
            return []

        threshold = float(
            np.quantile(np.array(list(returns.values()), dtype="float64"), self._weakness_quantile)
        )
        signals: list[Signal] = []
        for symbol in sorted(returns):
            if returns[symbol] > threshold:
                continue
            signal = self._evaluate(
                symbol, market, lookback_return=returns[symbol], threshold=threshold
            )
            if signal is not None:
                signals.append(signal)
        return signals

    # ------------------------------------------------------------------ #
    # Kesitsel zayıflık
    # ------------------------------------------------------------------ #
    def _lookback_returns(self, market: MarketData) -> dict[str, float]:
        """`as_of` barını taşıyan ve yeterli geçmişi olan sembollerin 7 günlük getirisi.

        Kısmi pencereyle hesaplanmış bir getiri (yeni listelenmiş sembolde 3 günlük getiriyi
        7 günlük sanmak) sıralamayı ve dolayısıyla eşiği sessizce bozardı — o sembol
        sıralamaya hiç girmez.
        """
        returns: dict[str, float] = {}
        for symbol in sorted(market.ohlcv):
            frame = bars_until(market.ohlcv[symbol], market.as_of)
            if frame.empty or frame.index[-1] != market.as_of:
                continue
            closes = frame["close"].to_numpy(dtype="float64")
            if len(closes) < self._lookback_bars + 1:
                continue
            past = float(closes[-(self._lookback_bars + 1)])
            if past <= 0.0:
                continue
            returns[symbol] = float(closes[-1]) / past - 1.0
        return returns

    # ------------------------------------------------------------------ #
    # Sembol bazlı kapılar
    # ------------------------------------------------------------------ #
    def _evaluate(
        self, symbol: str, market: MarketData, *, lookback_return: float, threshold: float
    ) -> Signal | None:
        frame = bars_until(market.ohlcv[symbol], market.as_of)
        close = float(frame["close"].iloc[-1])

        fast = ema(frame["close"], self._fast_ema_period)
        slow = ema(frame["close"], self._slow_ema_period)
        if fast is None or slow is None:
            return None
        if not (close < slow and fast < slow):
            return None

        leg = self._last_down_leg(frame)
        if leg is None:
            return None
        touch = self._rally_touch(frame, leg)
        if touch is None:
            return None

        strength = rsi(frame["close"], self._rsi_period)
        if strength is None:
            return None
        volume_ratio = self._volume_ratio(frame)
        if volume_ratio is None:
            return None
        if strength >= self._rsi_trigger or volume_ratio <= 1.0:
            logger.info(
                "%s %s: %s dokunuşu tetiklenmedi — RSI(%d) %.0f (eşik <%g), hacim %.2f× "
                "(%d bar ortalaması, eşik >1.00×)",
                self.name, symbol, touch.label, self._rsi_period, strength,
                self._rsi_trigger, volume_ratio, self._volume_period,
            )
            return None

        allowed, funding_note = self._funding_gate(symbol, market)
        if not allowed:
            logger.info(
                "%s %s: short bastırıldı, funding kapısı kapalı (%s)",
                self.name, symbol, funding_note,
            )
            return None

        atr = average_true_range(frame, self._atr_period)
        if atr is None or atr <= 0.0:
            logger.info(
                "%s %s: ralli satışı atlandı, ATR(%d) hesaplanamadı",
                self.name, symbol, self._atr_period,
            )
            return None

        rally_top = float(frame.loc[leg.low_time :, "high"].max())
        stop_price = max(rally_top, close + atr * self._stop_atr_multiple)
        multiple = (stop_price - close) / atr
        if multiple > self._max_stop_atr_multiple:
            logger.info(
                "%s %s: ralli satışı atlandı, düzeltme tepesi (%.10g) %.2f×ATR(%d) uzakta ve "
                "tavanı (%.2f×) aşıyor — stop tavana ÇEKİLMEZ (kural 14)",
                self.name, symbol, rally_top, multiple, self._atr_period,
                self._max_stop_atr_multiple,
            )
            return None

        target = leg.low_price
        target_valid = target < close
        if not target_valid:
            # Bacağın dibi girişin üstünde kaldı: hedefi yine de yazmak core/validate.py'de
            # ValueError'a (kural 8) takılıp TÜM modeli düşürürdü. Pozisyon hedefsiz açılır,
            # kalanı zaten trailing ile taşınıyordu.
            logger.info(
                "%s %s: önceki dip (%.10g) girişin (%.10g) üstünde, TP'siz açılıyor",
                self.name, symbol, target, close,
            )

        return Signal(
            symbol=symbol,
            direction="short",
            stop_price=stop_price,
            take_profits=(
                (TakeProfit(price=target, fraction=TARGET_FRACTION),) if target_valid else ()
            ),
            trailing_atr=self._trailing_atr_multiple,
            reason=(
                f"rejim: kapanış {close:.6g} < EMA{self._slow_ema_period} {slow:.6g} ve "
                f"EMA{self._fast_ema_period} {fast:.6g} < EMA{self._slow_ema_period}; "
                f"{self._lookback_days}g getiri %{lookback_return * 100:+.2f} ≤ evrenin alt "
                f"%{self._weakness_quantile * 100:.0f} eşiği %{threshold * 100:+.2f}; "
                f"ralli {touch.label} {touch.level:.6g} seviyesine aşağıdan dokundu "
                f"(bacak {leg.start_price:.6g}->{leg.low_price:.6g}); "
                f"RSI({self._rsi_period}) {strength:.0f} (<{self._rsi_trigger:g}) ve hacim "
                f"{volume_ratio:.2f}× ({self._volume_period} bar ortalaması, eşik >1.00×); "
                f"funding {funding_note}; stop düzeltme tepesi veya "
                f"{self._stop_atr_multiple:g}×ATR({self._atr_period}) ({stop_price:.6g}, "
                f"{multiple:.2f}×ATR, tavan {self._max_stop_atr_multiple:g}×); "
                + (
                    f"hedef önceki dip {target:.6g} (%{TARGET_FRACTION * 100:.0f}), "
                    if target_valid
                    else "hedef yok (önceki dip girişin üstünde), "
                )
                + f"kalanı {self._trailing_atr_multiple:g}×ATR trailing"
            ),
        )

    def _last_down_leg(self, frame: pd.DataFrame) -> _Leg | None:
        """En güncel teyitli zigzag zirvesi ve ondan SONRAKİ en düşük dip.

        Bacağın dibi neden pivot değil, barların kendi en düşüğü: zigzag'ın kısa bacakları
        ÇİFT hâlinde elediği (bkz. core/indicators._merge_short_legs) için taze bir dip,
        onu teyit eden ralli 8 bardan kısaysa pivot listesinde HİÇ görünmez. Dibi pivotlardan
        okumak, modelin "son düşüş bacağı" derken hep bir önceki bacağı kastetmesi ve
        düzeltme bandını fiyattan onlarca yüzde uzağa koyması demekti — yani kapının pratikte
        hiç açılmaması. Zirve tarafında aynı sorun yok: bacağın başı zaten teyit edilmiş
        olmalıdır, yoksa "düşüş bacağı" henüz oluşmamış demektir.

        Zirveden sonra bar kalmamışsa (zirve son bardır) ya da dip zirvenin altında değilse
        ortada bir düşüş bacağı yoktur.
        """
        pivots = zigzag_pivots(
            frame, pct_threshold=ZIGZAG_PCT_THRESHOLD, min_leg_bars=MIN_LEG_DURATION_BARS
        )
        highs = [pivot for pivot in pivots if pivot.kind == "high"]
        if not highs:
            return None
        start = highs[-1]
        after = frame.loc[start.time :].iloc[1:]
        if after.empty:
            return None
        low_time = after["low"].idxmin()
        low_price = float(after["low"].loc[low_time])
        if low_price >= start.price:
            return None
        return _Leg(
            start_price=start.price,
            start_time=start.time,
            low_price=low_price,
            low_time=low_time,
        )

    def _rally_touch(self, frame: pd.DataFrame, leg: _Leg) -> _Touch | None:
        """Düzeltme bandına ya da 20 EMA'ya AŞAĞIDAN ilk dokunuş.

        "İlk": bir önceki barın fitili seviyeye ulaşmamış olmalı. Bu hem "aşağıdan" şartını
        (seviye o barda henüz aşılmamıştı) hem de aynı ralliyi her barda tekrar sinyale
        çevirmemeyi sağlar.
        """
        if len(frame) < 2:
            return None

        high = float(frame["high"].iloc[-1])
        low = float(frame["low"].iloc[-1])
        previous_high = float(frame["high"].iloc[-2])

        levels = fib_levels(
            a_price=leg.start_price,
            b_price=leg.low_price,
            a_kind="high",
            ratios=RETRACEMENT_RATIOS,
        )
        zone_low, zone_high = levels[0.382], levels[0.618]
        if high >= zone_low and low <= zone_high and previous_high < zone_low:
            return _Touch(
                level=zone_low,
                label=f"{RETRACEMENT_RATIOS[0]:g}-{RETRACEMENT_RATIOS[1]:g} düzeltme bandı",
            )

        rally_ema = ema(frame["close"], self._rally_ema_period)
        if rally_ema is not None and high >= rally_ema and low <= rally_ema:
            if previous_high < rally_ema:
                return _Touch(level=rally_ema, label=f"{self._rally_ema_period} EMA")
        return None

    def _volume_ratio(self, frame: pd.DataFrame) -> float | None:
        """Tetik barının hacmi / kendinden ÖNCEKİ `volume_period` barın ortalaması.

        Ortalama tetik barını içerseydi barın kendi hacmi eşiği yukarı çeker ve "ortalamanın
        üstünde" ifadesi barın büyüklüğüne göre kayardı (bkz. strategies/squeeze.py).
        """
        average = sma(frame["volume"].iloc[:-1], self._volume_period)
        if average is None or average <= 0.0:
            return None
        return float(frame["volume"].iloc[-1]) / average

    def _funding_gate(self, symbol: str, market: MarketData) -> tuple[bool, str]:
        """Son 3 periyodun ortalaması `funding_floor`un altındaysa kapı KAPALI."""
        series = market.funding.get(symbol)
        if series is None or len(series) == 0:
            return False, "geçmişi yok, kapı kapalı"
        recent = series.loc[: market.as_of].tail(FUNDING_PERIODS)
        if len(recent) < FUNDING_PERIODS:
            return False, (
                f"son {FUNDING_PERIODS} periyot tamamlanmadı ({len(recent)}), kapı kapalı"
            )
        average = float(recent.to_numpy(dtype="float64").mean())
        note = (
            f"son {FUNDING_PERIODS} periyot ortalaması %{average * 100:+.4f} "
            f"(taban %{self._funding_floor * 100:+.4f})"
        )
        if average < self._funding_floor:
            return False, note + " — squeeze riski"
        return True, note
