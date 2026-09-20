"""Kesitsel (cross-sectional) momentum: evrenin en güçlüsü long, en zayıfı short.

Tez: 7 günlük getiriye göre sıralanan evrende üst uçtaki semboller önümüzdeki hafta da
görece güçlü, alt uçtakiler görece zayıf kalır. Model mutlak bir yön iddiası taşımaz —
aynı anda hem long hem short açar; ölçtüğü şey **sıralamanın** kendisidir.

**Neden yalnızca Pazartesi 00:00 UTC.** Sıralama her turda (4 saatte bir) yeniden hesaplansaydı
model haftada 42 kez yeniden dengelerdi; sıra değiştiren her küçük oynama bir işlem açar ve
sonuç "momentum işe yarıyor mu" sorusuna değil "komisyon+kayma ne kadar yiyor" sorusuna cevap
verirdi. Sabit ve tek bir dengeleme barı, işlem sayısını tezin zaman ölçeğine (haftalık)
bağlar. Bar sabit olduğu için de denetlenebilir: hangi turda dengelendiği sonradan `as_of`'tan
okunur.

**Dengeleme barı dışındaki turlarda sinyal ÜRETİLMEZ** — açık pozisyonların stop/trailing
takibi motorun işidir (kural 9), modelin değil. Aynı sembol bir sonraki Pazartesi hâlâ ilk
5'teyse sinyal tekrar üretilir; tekrarı reddeden tek yetkili yer `core/portfolio.py`'dir
(kural 15'in `duplicate_position` sebep kodu), model kendi defterini göremez (kural 4/7).

Neden stop 2.5×ATR ve neden sabit: stop mesafesi aynı zamanda MALİYET ÖLÇEĞİDİR (kural 14).
2.5×ATR, kuralın 1×–2.5×ATR bandının üst ucudur — haftalık tutulan bir pozisyonun 4 saatlik
gürültüye takılmaması için bilinçli olarak geniş, ama `max_stop_atr_multiple` (3.0) tavanının
altında: model tavan yüzünden sessizce işlem kaybetmez.

Rollere dikkat: bu modül boyut/komisyon/bakiye hesaplamaz (kural 1/2/3/7). Gösterge matematiği
`core/indicators.py`'dedir; buradaki tek aritmetik iki kapanışın oranıdır (bkz. `_lookback_return`).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Mapping

import pandas as pd

from core.config import get_setting, load_config
from core.data import bar_duration
from core.indicators import average_true_range, bars_until
from strategies.base import Direction, MarketData, Signal, Strategy

logger = logging.getLogger(__name__)

LOOKBACK_DAYS = 7
SELECTION_COUNT = 5
STOP_ATR_MULTIPLE = 2.5
REBALANCE_WEEKDAY = 0  # pandas: Pazartesi
REBALANCE_HOUR = 0


@dataclass(frozen=True, kw_only=True)
class _Ranked:
    symbol: str
    lookback_return: float
    close: float
    atr: float


class Momentum(Strategy):
    name = "momentum"
    allowed_directions: list[Direction] = ["long", "short"]

    def __init__(
        self,
        *,
        config: Mapping[str, Any] | None = None,
        lookback_days: int = LOOKBACK_DAYS,
        selection_count: int = SELECTION_COUNT,
        stop_atr_multiple: float = STOP_ATR_MULTIPLE,
    ) -> None:
        settings = dict(config) if config is not None else load_config()
        # ATR periyodu parametre DEĞİL: projenin tek ATR tanımı config'in `trailing.atr_period`
        # değeridir (bkz. strategies/trend.py) — aynı "2.5×ATR" her modelde aynı mesafe demeli.
        self._atr_period = int(get_setting(settings, "trailing.atr_period"))
        # Geriye bakış BAR cinsinden değil GÜN cinsinden tanımlı: timeframe config'ten gelir,
        # 42 gibi bir sabit yazmak timeframe değiştiğinde tezi sessizce başka bir tez yapardı.
        self._lookback_bars = int(
            pd.Timedelta(days=lookback_days)
            / bar_duration(str(get_setting(settings, "timeframe")))
        )
        if self._lookback_bars <= 0:
            raise ValueError(
                f"{lookback_days} günlük geriye bakış bu timeframe'de tek bara sığmıyor"
            )
        self._lookback_days = lookback_days
        self._selection_count = selection_count
        self._stop_atr_multiple = stop_atr_multiple

    def generate_signals(
        self,
        market: MarketData,
        peer_signals: Mapping[str, tuple[Signal, ...]] | None = None,
    ) -> list[Signal]:
        if not self._is_rebalance_bar(market.as_of):
            logger.info(
                "%s: %s dengeleme barı değil (Pazartesi %02d:00 UTC bekleniyor), sinyal yok",
                self.name, market.as_of, REBALANCE_HOUR,
            )
            return []

        ranked = self._rank(market)
        if len(ranked) < 2 * self._selection_count:
            # Uçlar örtüşürse aynı sembol hem long hem short listesine girer; sıralamanın
            # taşıdığı bilgi de kalmaz. Listeyi küçültmek modeli sessizce "ilk 2 / son 2"
            # modeline çevirirdi — atlamak, ölçülemeyen turu ölçüyormuş gibi göstermez.
            logger.info(
                "%s: sıralanabilen sembol sayısı %d, uçların örtüşmemesi için en az %d gerekiyor; "
                "bu dengelemede sinyal üretilmedi",
                self.name, len(ranked), 2 * self._selection_count,
            )
            return []

        longs = ranked[: self._selection_count]
        shorts = ranked[-self._selection_count :]
        long_cutoff = longs[-1].lookback_return
        short_cutoff = shorts[0].lookback_return
        total = len(ranked)

        signals = [
            self._signal(entry, "long", rank=index + 1, total=total, cutoff=long_cutoff)
            for index, entry in enumerate(longs)
        ]
        signals.extend(
            self._signal(
                entry,
                "short",
                rank=total - self._selection_count + index + 1,
                total=total,
                cutoff=short_cutoff,
            )
            for index, entry in enumerate(shorts)
        )
        return signals

    def _is_rebalance_bar(self, as_of: pd.Timestamp) -> bool:
        """Dengeleme yalnızca Pazartesi 00:00 UTC barında.

        Karşılaştırma UTC'ye çevrilerek yapılır: `as_of` sözleşme gereği UTC'dir, ama yerel
        saatli bir zaman damgası sessizce farklı bir barı "Pazartesi" sayabilirdi.
        """
        stamp = as_of.tz_convert("UTC") if as_of.tzinfo is not None else as_of
        return (
            stamp.dayofweek == REBALANCE_WEEKDAY
            and stamp.hour == REBALANCE_HOUR
            and stamp.minute == 0
            and stamp.second == 0
        )

    def _rank(self, market: MarketData) -> list[_Ranked]:
        """Getiriye göre AZALAN sıralama; eşitlikte sembol adı — sıra koşudan koşuya sabit olmalı."""
        entries: list[_Ranked] = []
        for symbol in sorted(market.ohlcv):
            entry = self._measure(symbol, market)
            if entry is not None:
                entries.append(entry)
        return sorted(entries, key=lambda entry: (-entry.lookback_return, entry.symbol))

    def _measure(self, symbol: str, market: MarketData) -> _Ranked | None:
        frame = bars_until(market.ohlcv[symbol], market.as_of)
        if frame.empty or frame.index[-1] != market.as_of:
            # `as_of` barını taşımayan sembolü sıralamaya sokmak, farklı tarihli iki getiriyi
            # aynı kolonda yarıştırmak olurdu (kural 5/12).
            return None
        lookback_return = self._lookback_return(frame)
        if lookback_return is None:
            return None
        atr = average_true_range(frame, self._atr_period)
        if atr is None or atr <= 0.0:
            logger.info(
                "%s %s: sıralama dışı, ATR(%d) hesaplanamadı — stop mesafesi üretilemez",
                self.name, symbol, self._atr_period,
            )
            return None
        return _Ranked(
            symbol=symbol,
            lookback_return=lookback_return,
            close=float(frame["close"].iloc[-1]),
            atr=atr,
        )

    def _lookback_return(self, frame: pd.DataFrame) -> float | None:
        """`lookback_bars` bar önceki kapanışa göre oransal değişim; yeterli bar yoksa None.

        Bu bir gösterge değil iki kapanışın oranıdır: parametresi (pencere) dışında ayrışacak
        bir matematiği yok, dolayısıyla `core/indicators.py`'de ikinci bir uygulama riski de
        yok. Kısmi pencereyle hesaplamak (yeni listelenmiş sembolde 3 günlük getiriyi 7 günlük
        sanmak) sıralamayı sessizce bozardı — o yüzden None.
        """
        closes = frame["close"].to_numpy(dtype="float64")
        if len(closes) < self._lookback_bars + 1:
            return None
        past = float(closes[-(self._lookback_bars + 1)])
        if past <= 0.0:
            return None
        return float(closes[-1]) / past - 1.0

    def _signal(
        self, entry: _Ranked, direction: Direction, *, rank: int, total: int, cutoff: float
    ) -> Signal:
        distance = entry.atr * self._stop_atr_multiple
        stop_price = (
            entry.close - distance if direction == "long" else entry.close + distance
        )
        side = "ilk" if direction == "long" else "son"
        return Signal(
            symbol=entry.symbol,
            direction=direction,
            stop_price=stop_price,
            reason=(
                f"{self._lookback_days}g getiri %{entry.lookback_return * 100:+.2f} "
                f"({self._lookback_bars} bar), sıra {rank}/{total} "
                f"({side} {self._selection_count} eşiği %{cutoff * 100:+.2f}); "
                f"stop {self._stop_atr_multiple:g}×ATR({self._atr_period})={distance:.6g} "
                f"uzakta ({stop_price:.6g}); haftalık dengeleme (Pazartesi "
                f"{REBALANCE_HOUR:02d}:00 UTC)"
            ),
        )
