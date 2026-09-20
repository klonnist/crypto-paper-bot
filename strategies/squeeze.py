"""Bollinger sıkışması + hacim teyitli kırılım (yarışmacı model).

Tez: bant genişliği son 50 barın en dar %20'sine indiğinde oynaklık birikmiştir; bu
sıkışmadan hacimle birlikte çıkan kapanış, çıkış yönünde bir hareketin başlangıcıdır.

**Hacim teyidi modelin ana filtresidir, opsiyonel bir ek değil.** Sıkışmadan çıkan
kapanışların çoğu sahte kırılımdır; hacimsiz bir kırılım koşulu, modeli "dar banttan
çıkışları say" ölçümüne indirger ve tezin kendisi (hacmin katılımı) hiç test edilmemiş olur.
Bu yüzden teyit yoksa **işlem yok** — bastırılan her kırılım loglanır ki filtre kaç işlemi
elediği sonradan denetlenebilsin.

**Bantlar ve hacim ortalaması kırılım barını HARİÇ tutar.** Gerekçe `core.indicators.donchian`
ile birebir aynıdır: kırılım barının kendi hareketi aynı barın standart sapmasını şişirir,
kendi hacmi de 20 barlık ortalamayı yukarı çeker. İki ölçüyü de kırılım barının GEÇMİŞİNDEN
almak, en güçlü kırılımların kendi kendini elemesini önler ve "sıkışma" ile "kırılım"ı iki
ayrı bara yerleştirerek tanımı denetlenebilir kılar.

**Stop sıkışma aralığının karşı ucudur** — tezle aynı yerde: kırılım geçersizse fiyat
sıkışma aralığının içine geri döner ve diğer uca ulaşır. Bu, mesafeyi veriye bağlar; dar
sıkışmalarda mesafe küçük, geniş sıkışmalarda büyüktür. `max_stop_atr_multiple` bu yüzden
burada bir TAVANDIR (kural 14): mesafe tavanı aşarsa **işlem atlanır**, stop tavana çekilmez
— çekmek modelin "stop aralığın karşı ucunda olmalı" tezini sessizce başka bir modele çevirirdi.
Motor aynı tavanı ayrıca uygular; modelin kendi kapısı, atlamanın gerekçesini (hangi sıkışma,
hangi mesafe) modelin diliyle loglamak içindir.

Rollere dikkat: bu modül boyut/komisyon/bakiye hesaplamaz (kural 1/2/3/7) ve gösterge
matematiğini kendisi yazmaz — Bollinger, SMA ve ATR `core/indicators.py`'dedir.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

import numpy as np
import pandas as pd

from core.config import get_setting, load_config
from core.indicators import average_true_range, bars_until, bollinger, sma
from strategies.base import Direction, MarketData, Signal, Strategy

logger = logging.getLogger(__name__)

BOLLINGER_PERIOD = 20
BOLLINGER_STD = 2.0
BANDWIDTH_LOOKBACK = 50
SQUEEZE_QUANTILE = 0.20
VOLUME_PERIOD = 20
VOLUME_MULTIPLE = 1.5


class Squeeze(Strategy):
    name = "squeeze"
    allowed_directions: list[Direction] = ["long", "short"]

    def __init__(
        self,
        *,
        config: Mapping[str, Any] | None = None,
        bollinger_period: int = BOLLINGER_PERIOD,
        bollinger_std: float = BOLLINGER_STD,
        bandwidth_lookback: int = BANDWIDTH_LOOKBACK,
        squeeze_quantile: float = SQUEEZE_QUANTILE,
        volume_period: int = VOLUME_PERIOD,
        volume_multiple: float = VOLUME_MULTIPLE,
    ) -> None:
        settings = dict(config) if config is not None else load_config()
        # ATR periyodu ve stop tavanı config'ten gelir: ikisi de tüm modeller için ortaktır
        # (bkz. CLAUDE.md kural 14 ve "config.yaml Değerleri"), modelin kendi kopyası olamaz.
        self._atr_period = int(get_setting(settings, "trailing.atr_period"))
        self._max_stop_atr_multiple = float(get_setting(settings, "max_stop_atr_multiple"))
        self._bollinger_period = bollinger_period
        self._bollinger_std = bollinger_std
        self._bandwidth_lookback = bandwidth_lookback
        self._squeeze_quantile = squeeze_quantile
        self._volume_period = volume_period
        self._volume_multiple = volume_multiple

    def generate_signals(
        self,
        market: MarketData,
        peer_signals: Mapping[str, tuple[Signal, ...]] | None = None,
    ) -> list[Signal]:
        signals: list[Signal] = []
        for symbol in sorted(market.ohlcv):
            signal = self._evaluate(symbol, market)
            if signal is not None:
                signals.append(signal)
        return signals

    def _evaluate(self, symbol: str, market: MarketData) -> Signal | None:
        frame = bars_until(market.ohlcv[symbol], market.as_of)
        if frame.empty or frame.index[-1] != market.as_of:
            return None

        history = frame.iloc[:-1]  # kırılım barının GEÇMİŞİ: bantlar ve hacim buradan
        bands = bollinger(history["close"], self._bollinger_period, self._bollinger_std)
        if bands is None or bands.middle <= 0.0:
            return None
        bandwidths = self._bandwidth_history(history)
        if bandwidths is None:
            return None

        bandwidth = (bands.upper - bands.lower) / bands.middle
        threshold = float(np.quantile(bandwidths, self._squeeze_quantile))
        if bandwidth > threshold:
            return None

        close = float(frame["close"].iloc[-1])
        if close > bands.upper:
            direction: Direction = "long"
            level = bands.upper
        elif close < bands.lower:
            direction = "short"
            level = bands.lower
        else:
            return None

        volume_ratio = self._volume_ratio(frame, history)
        if volume_ratio is None:
            return None
        if volume_ratio < self._volume_multiple:
            logger.info(
                "%s %s: %s kırılımı bastırıldı, hacim teyidi yok — hacim %.2f× "
                "(%d bar ortalaması, eşik %.2f×)",
                self.name, symbol, direction, volume_ratio,
                self._volume_period, self._volume_multiple,
            )
            return None

        stop_price = self._squeeze_edge(history, direction)
        if not self._stop_is_on_the_right_side(stop_price, close=close, direction=direction):
            # Sıkışma aralığı kapanışı içeriyorsa stop girişin yanlış tarafındadır. Bunu
            # sinyale çevirmek core/validate.py'de ValueError'a (kural 8) takılıp TÜM modeli
            # düşürürdü; oysa bu bir programlama hatası değil, veri durumudur.
            logger.info(
                "%s %s: %s kırılımı atlandı, sıkışma aralığının karşı ucu (%.10g) kapanışın "
                "(%.10g) doğru tarafında değil",
                self.name, symbol, direction, stop_price, close,
            )
            return None

        atr = average_true_range(frame, self._atr_period)
        if atr is None or atr <= 0.0:
            logger.info(
                "%s %s: %s kırılımı atlandı, ATR(%d) hesaplanamadı — stop tavanı doğrulanamaz",
                self.name, symbol, direction, self._atr_period,
            )
            return None

        distance = abs(close - stop_price)
        multiple = distance / atr
        if multiple > self._max_stop_atr_multiple:
            logger.info(
                "%s %s: %s kırılımı atlandı, sıkışma aralığının karşı ucu %.2f×ATR(%d) uzakta "
                "ve tavanı (%.2f×) aşıyor (stop=%.10g, kapanış=%.10g)",
                self.name, symbol, direction, multiple, self._atr_period,
                self._max_stop_atr_multiple, stop_price, close,
            )
            return None

        return Signal(
            symbol=symbol,
            direction=direction,
            stop_price=stop_price,
            reason=(
                f"Bollinger({self._bollinger_period},{self._bollinger_std:g}) bant genişliği "
                f"%{bandwidth * 100:.2f} ≤ son {self._bandwidth_lookback} barın en dar "
                f"%{self._squeeze_quantile * 100:.0f} eşiği %{threshold * 100:.2f}; "
                f"{'üst' if direction == 'long' else 'alt'} banda ({level:.6g}) "
                f"{'yukarı' if direction == 'long' else 'aşağı'} kırılım, kapanış {close:.6g}; "
                f"hacim {volume_ratio:.2f}× ({self._volume_period} bar ortalaması, "
                f"eşik {self._volume_multiple:g}×); "
                f"stop sıkışma aralığının karşı ucu {stop_price:.6g} "
                f"({multiple:.2f}×ATR({self._atr_period}), tavan "
                f"{self._max_stop_atr_multiple:g}×)"
            ),
        )

    def _bandwidth_history(self, history: pd.DataFrame) -> list[float] | None:
        """Son `bandwidth_lookback` barın bant genişliği dizisi; yeterli bar yoksa None.

        Genişlik orta banda bölünür (yüzdesel): mutlak genişlik fiyat seviyesiyle ölçeklenir
        ve aynı sembolün altı ay önceki bandıyla bugünküsü kıyaslanamaz hâle gelirdi.
        Kısmi bir pencereyle (ör. 12 barlık) yüzdelik eşik üretmek, "en dar %20" ifadesini
        sembolden sembole farklı bir ölçüye çevirirdi — o yüzden None.
        """
        closes = history["close"]
        values: list[float] = []
        for offset in range(self._bandwidth_lookback):
            window = closes.iloc[: len(closes) - offset]
            bands = bollinger(window, self._bollinger_period, self._bollinger_std)
            if bands is None or bands.middle <= 0.0:
                return None
            values.append((bands.upper - bands.lower) / bands.middle)
        return values

    def _volume_ratio(self, frame: pd.DataFrame, history: pd.DataFrame) -> float | None:
        average = sma(history["volume"], self._volume_period)
        if average is None or average <= 0.0:
            logger.info(
                "%s: %d barlık hacim ortalaması hesaplanamadı, kırılım teyit edilemez",
                self.name, self._volume_period,
            )
            return None
        return float(frame["volume"].iloc[-1]) / average

    def _squeeze_edge(self, history: pd.DataFrame, direction: Direction) -> float:
        """Sıkışma aralığının karşı ucu: bandın hesaplandığı barların dibi (long) / zirvesi (short)."""
        window = history.iloc[-self._bollinger_period :]
        column = "low" if direction == "long" else "high"
        values = window[column].to_numpy(dtype="float64")
        return float(values.min() if direction == "long" else values.max())

    @staticmethod
    def _stop_is_on_the_right_side(
        stop_price: float, *, close: float, direction: Direction
    ) -> bool:
        return stop_price < close if direction == "long" else stop_price > close
