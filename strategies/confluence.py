"""Fibonacci confluence: büyük dalganın retracement'i ile küçük ABC bacağının extension'ı.

Model, crypto-scanner deposundaki ana gate'in (`find_confluence_candidates` +
`evaluate_confluence_entry`) bu projeye taşınmış hâlidir.

Tez: fiyatın kendi yapısından çıkan İKİ BAĞIMSIZ swing'in Fibonacci seviyeleri aynı fiyatta
çakışıyorsa (büyük dalganın 0.618/0.786 geri çekilmesi, küçük ABC bacağının 1.272/1.618
uzantısı) orası tek bir seviyeden daha güçlü bir dönüş bölgesidir. Yönü RSI belirler:
aşırı satımda long, aşırı alımda short, nötrde işlem yok.

Yapı (kaynaktaki sırayla):

1. `core.indicators.zigzag_pivots` ile pivotlar bulunur; ardışık her pivot çifti bir bacaktır.
2. **Büyük dalga** = |B−A| en büyük TEK bacak; yalnızca retracement rolünde kullanılır.
3. **Küçük ABC bacağı** = büyük dalganın bitişinden SONRA başlayan, onunla ortak nokta
   paylaşmayan bacaklardan kronolojik olarak EN GÜNCEL olanı; yalnızca extension rolünde.
4. Güncel fiyata toplam mesafesi en küçük (retracement, extension) çifti seçilir; iki mesafe
   de `FIB_CONFLUENCE_TOLERANCE`'ın altındaysa kapı açılır.
5. Yön RSI'dan: <35 long, >65 short, arası sinyal yok.

**Kaynaktan bilinçli sapmalar** (hepsi bu projenin değişmez kurallarından doğar; gerekçeler
`docs/decisions.md` karar 11'de):

- **Confidence kademesi hesaplanır ama HİÇBİR kararı etkilemez.** Kaynakta kademenin tek
  işlevi pozisyon boyutunu çarpmaktı (0.5R/1.0R/1.5R); bu projede boyutlandırma stratejinin
  işi değildir (kural 3/11) ve `core/portfolio.py` dışında kimse 1R'yi büyütemez. Kademe
  yalnızca `reason`ın sonuna `| confidence=low|medium|high` olarak yazılır — defterden
  (`trades.csv`) gruplanıp "teyit katmanı ayırt ediyor mu" sorusu, ölçüm tablosunu hiç
  kirletmeden cevaplanabilsin diye. Hesabı `strategies/confluence_confidence.py`'dedir ve
  oradan buraya yalnızca bir metin gelir; o katman patlarsa etiket `unknown` olur ve sinyal
  değişmeden üretilir.
- **RSI ve ATR `core/indicators.py`'den okunur.** Kaynak Wilder yumuşatması kullanıyor
  (`ewm(alpha=1/period)`); bu depo onu açıkça reddediyor (özyineleme sonucu kaç bar geçmiş
  verildiğine bağlı kılar). Sayılar kaynakla birebir aynı çıkmaz; eşikler (35/65) aynıdır.
- **Stop 2.5×ATR, kaynaktaki 3.0×ATR değil.** 3.0 bu projede `max_stop_atr_multiple`
  TAVANIDIR (kural 14): dolum bir sonraki barın açılışında olduğu için (kural 13) mesafe
  tavanın hemen üstüne çıkar ve motor işlemi eler — model sessizce işlem kaybederdi. 2.5,
  kuralın 1×–2.5×ATR bandının üst ucudur ve `momentum` ile aynı ölçektedir.
- Telegram/state/cooldown/CSV ve likidite-stablecoin filtreleri taşınmadı: ilki raporlama
  katmanı (burada defter var), ikincisi evren katmanının işi (`core/data.py`).

Pivot matematiği burada DEĞİL `core/indicators.py`'dedir ve kaynaktan birebir taşınmıştır:
aynı pivotun ikinci bir tanımı, modelin ölçtüğü şeyin kaynak tarayıcıyla aynı olduğu iddiasını
kanıtlanamaz kılardı.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Mapping

import pandas as pd

from core.config import get_setting, load_config
from core.indicators import PivotKind, average_true_range, bars_until, fib_levels, rsi, zigzag_pivots
from strategies import confluence_confidence
from strategies.base import Direction, MarketData, Signal, Strategy

logger = logging.getLogger(__name__)

ZIGZAG_PCT_THRESHOLD = 0.05
MIN_LEG_DURATION_BARS = 8
RETRACEMENT_RATIOS: tuple[float, ...] = (0.618, 0.786)
EXTENSION_RATIOS: tuple[float, ...] = (1.272, 1.618)
FIB_CONFLUENCE_TOLERANCE = 0.03
# Kaynaktaki `watch_tolerance`: gate değil, ön elemedir — bu mesafenin iki katından uzak
# seviyeler eşleşme aramasına hiç girmez.
WATCH_TOLERANCE = 0.08
RSI_PERIOD = 14
RSI_OVERSOLD = 35.0
RSI_OVERBOUGHT = 65.0
STOP_ATR_MULTIPLE = 2.5


@dataclass(frozen=True, kw_only=True)
class _Leg:
    leg_id: int
    a_kind: PivotKind
    a_price: float
    a_time: pd.Timestamp
    b_price: float
    b_time: pd.Timestamp
    duration_bars: int
    magnitude: float
    retracements: dict[float, float]
    extensions: dict[float, float]


@dataclass(frozen=True, kw_only=True)
class _Match:
    combined_distance: float
    big_ratio: float
    big_level: float
    big_distance: float
    small_ratio: float
    small_level: float
    small_distance: float


class Confluence(Strategy):
    name = "confluence"
    allowed_directions: list[Direction] = ["long", "short"]

    def __init__(
        self,
        *,
        config: Mapping[str, Any] | None = None,
        pct_threshold: float = ZIGZAG_PCT_THRESHOLD,
        min_leg_bars: int = MIN_LEG_DURATION_BARS,
        tolerance: float = FIB_CONFLUENCE_TOLERANCE,
        watch_tolerance: float = WATCH_TOLERANCE,
        rsi_period: int = RSI_PERIOD,
        rsi_oversold: float = RSI_OVERSOLD,
        rsi_overbought: float = RSI_OVERBOUGHT,
        stop_atr_multiple: float = STOP_ATR_MULTIPLE,
    ) -> None:
        # ATR periyodu parametre değil: tek ATR tanımı config'ten gelir (bkz. strategies/trend.py).
        self._atr_period = int(
            get_setting(dict(config) if config is not None else load_config(), "trailing.atr_period")
        )
        self._pct_threshold = pct_threshold
        self._min_leg_bars = min_leg_bars
        self._tolerance = tolerance
        self._watch_tolerance = watch_tolerance
        self._rsi_period = rsi_period
        self._rsi_oversold = rsi_oversold
        self._rsi_overbought = rsi_overbought
        self._stop_atr_multiple = stop_atr_multiple

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

        legs = self._legs(frame)
        if len(legs) < 2:
            return None

        big_wave = max(legs, key=lambda leg: leg.magnitude)
        small_leg = self._small_leg(legs, big_wave)
        if small_leg is None:
            return None

        close = float(frame["close"].iloc[-1])
        match = self._best_match(big_wave, small_leg, close)
        if match is None:
            return None
        if match.big_distance > self._tolerance or match.small_distance > self._tolerance:
            return None

        strength = rsi(frame["close"], self._rsi_period)
        if strength is None:
            return None
        if strength < self._rsi_oversold:
            direction: Direction = "long"
        elif strength > self._rsi_overbought:
            direction = "short"
        else:
            # Kurulum var ama yön yok. Sessiz geçmek, kapının kaç kez açılıp yönsüz
            # kaldığını ölçülemez kılardı — eşiklerin gerçekten tuttuğu buradan denetlenir.
            logger.info(
                "%s %s: confluence kapısı açık (toplam mesafe %%%.2f) ama RSI(%d) %.0f nötr "
                "bölgede (%g-%g), yön belirlenemedi",
                self.name, symbol, match.combined_distance * 100.0, self._rsi_period,
                strength, self._rsi_oversold, self._rsi_overbought,
            )
            return None

        atr = average_true_range(frame, self._atr_period)
        if atr is None or atr <= 0.0:
            logger.info(
                "%s %s: %s kurulumu atlandı, ATR(%d) hesaplanamadı",
                self.name, symbol, direction, self._atr_period,
            )
            return None

        distance = atr * self._stop_atr_multiple
        stop_price = close - distance if direction == "long" else close + distance

        return Signal(
            symbol=symbol,
            direction=direction,
            stop_price=stop_price,
            reason=(
                f"RSI({self._rsi_period}) {strength:.0f} "
                f"({'<' if direction == 'long' else '>'}"
                f"{self._rsi_oversold if direction == 'long' else self._rsi_overbought:g}); "
                f"büyük dalga {big_wave.a_price:.6g}->{big_wave.b_price:.6g} "
                f"({big_wave.duration_bars} bar) {match.big_ratio:g} retracement "
                f"{match.big_level:.6g}, mesafe %{match.big_distance * 100:.2f}; "
                f"küçük bacak {small_leg.a_price:.6g}->{small_leg.b_price:.6g} "
                f"({small_leg.duration_bars} bar) {match.small_ratio:g} extension "
                f"{match.small_level:.6g}, mesafe %{match.small_distance * 100:.2f} "
                f"(tolerans %{self._tolerance * 100:g}); kapanış {close:.6g}; "
                f"zigzag %{self._pct_threshold * 100:g} / {self._min_leg_bars} bar; "
                f"stop {self._stop_atr_multiple:g}×ATR({self._atr_period})={distance:.6g} "
                f"uzakta ({stop_price:.6g})"
                # Etiket serbest cümlenin İÇİNE gömülmez: defterden ayrıştırılabilir olması
                # (trades.csv'de gruplama) tek amacı.
                f" | confidence={self._confidence(frame, direction, symbol)}"
            ),
        )

    def _confidence(self, frame: pd.DataFrame, direction: Direction, symbol: str) -> str:
        """Kademe etiketi. Hesap patlarsa sinyal DEĞİŞMEZ, etiket "unknown" olur.

        Etiketin hiçbir kararı etkilememesi bir tasarım kısıtı: burada bir istisnanın sinyali
        düşürmesi, süs amaçlı bir katmanın ölçümü etkilemesi demek olurdu.
        """
        try:
            return confluence_confidence.assess(frame, direction).tier
        except Exception as exc:  # noqa: BLE001 - etiket katmanı ölçümü düşüremez
            logger.warning(
                "%s %s: confidence kademesi hesaplanamadı (%s); sinyal etkilenmedi",
                self.name, symbol, exc,
            )
            return "unknown"

    def _legs(self, frame: pd.DataFrame) -> list[_Leg]:
        """Ardışık pivot çiftlerinden bacaklar; sıfır genlikli ve negatif seviyeli olanlar elenir."""
        pivots = zigzag_pivots(
            frame, pct_threshold=self._pct_threshold, min_leg_bars=self._min_leg_bars
        )
        legs: list[_Leg] = []
        for leg_id, (start, end) in enumerate(zip(pivots, pivots[1:])):
            if abs(end.price - start.price) <= 0.0:
                continue
            retracements = fib_levels(
                a_price=start.price, b_price=end.price, a_kind=start.kind, ratios=RETRACEMENT_RATIOS
            )
            extensions = fib_levels(
                a_price=start.price, b_price=end.price, a_kind=start.kind, ratios=EXTENSION_RATIOS
            )
            if any(level <= 0.0 for level in (*retracements.values(), *extensions.values())):
                # Negatif/sıfır seviye, yüzdesel mesafeyi anlamsız kılar (paydada kullanılıyor).
                continue
            legs.append(
                _Leg(
                    leg_id=leg_id,
                    a_kind=start.kind,
                    a_price=start.price,
                    a_time=start.time,
                    b_price=end.price,
                    b_time=end.time,
                    duration_bars=int(
                        frame.index.get_loc(end.time) - frame.index.get_loc(start.time)
                    ),
                    magnitude=abs(end.price - start.price),
                    retracements=retracements,
                    extensions=extensions,
                )
            )
        return legs

    def _small_leg(self, legs: list[_Leg], big_wave: _Leg) -> _Leg | None:
        """Büyük dalgadan SONRA başlayan ve onunla ortak uç paylaşmayan en güncel bacak.

        "Bağımsızlık" tanımı kaynaktan birebir: yalnızca kronolojik sonralık yetmez, iki ucun
        da büyük dalganın uçlarından farklı olması gerekir — aynı B'yi paylaşan iki bacak
        aynı hareketin iki yüzüdür ve çakışmaları "iki bağımsız swing" sayılamaz.
        """
        candidates = [
            leg
            for leg in legs
            if leg.leg_id != big_wave.leg_id
            and leg.a_time > big_wave.b_time
            and leg.a_time not in (big_wave.a_time, big_wave.b_time)
            and leg.b_time not in (big_wave.a_time, big_wave.b_time)
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda leg: leg.leg_id)

    def _best_match(self, big_wave: _Leg, small_leg: _Leg, close: float) -> _Match | None:
        """Güncel fiyata TOPLAM mesafesi en küçük (retracement, extension) çifti."""
        best: _Match | None = None
        limit = self._watch_tolerance * 2
        for big_ratio, big_level in big_wave.retracements.items():
            big_distance = abs(close - big_level) / big_level
            if big_distance > limit:
                continue
            for small_ratio, small_level in small_leg.extensions.items():
                small_distance = abs(close - small_level) / small_level
                if small_distance > limit:
                    continue
                combined = big_distance + small_distance
                if best is None or combined < best.combined_distance:
                    best = _Match(
                        combined_distance=combined,
                        big_ratio=big_ratio,
                        big_level=big_level,
                        big_distance=big_distance,
                        small_ratio=small_ratio,
                        small_level=small_level,
                        small_distance=small_distance,
                    )
        return best
