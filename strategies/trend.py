"""Donchian kırılımı + EMA rejim filtresi (yarışmacı model).

Tez: fiyat son `donchian_period` barın aralığını kırdığında, kırılım yönünde bir trend
başlama olasılığı rastgeleden yüksektir. Model bunu iki kapıdan geçirir:

1. **Kırılım** — kapanış, son 20 barın (o barın KENDİSİ hariç, bkz. `core.indicators.donchian`)
   tepesinin üstünde ya da dibinin altında olmalı.
2. **Rejim** — 50 EMA / 200 EMA yönüne ters işlem açılmaz. Filtrenin işlevi kârı artırmak
   değil, modelin ölçtüğü şeyi daraltmaktır: filtresiz bir kırılım modeli yatay piyasada
   iki yönde de tetiklenir ve sonuç "kırılım işe yarıyor mu" sorusuna değil "hangi rejimde
   ne kadar testere yedik" sorusuna cevap verir.

Neden stop 2×ATR ve neden sabit: stop mesafesi aynı zamanda MALİYET ÖLÇEĞİDİR (CLAUDE.md
kural 14) — boyut `risk / |giriş − stop|` olduğu için dar stop kuran model aynı 1R'yi daha
büyük notional ile taşır ve R başına daha çok komisyon öder. 2×ATR, kuralın 1×–2.5×ATR
bandının ortasındadır; `max_stop_atr_multiple` (3.0) tavanına takılmaz, yani bu model
tavan yüzünden sessizce işlem kaybetmez. Mesafeyi veriye bağlamak (örn. "fitilin üstü")
modeli bandın dışına taşıyabilirdi.

Rollere dikkat: bu modül boyut, komisyon ya da bakiye HESAPLAMAZ (kural 1/2/3/7) ve kendi
trailing'ini yürütmez (kural 9) — `trailing_atr` yalnızca isteği bildirir, uygulaması
`core/engine.py`'dedir. Gösterge matematiği de burada değil `core/indicators.py`'dedir:
aynı göstergenin ikinci bir uygulaması iki modelin farklı sayı görmesi demek olurdu.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

from core.config import get_setting, load_config
from core.indicators import average_true_range, bars_until, donchian, ema
from strategies.base import Direction, MarketData, Signal, Strategy

logger = logging.getLogger(__name__)

DONCHIAN_PERIOD = 20
FAST_EMA_PERIOD = 50
SLOW_EMA_PERIOD = 200
STOP_ATR_MULTIPLE = 2.0
TRAILING_ATR_MULTIPLE = 1.0


class Trend(Strategy):
    name = "trend"
    allowed_directions: list[Direction] = ["long", "short"]

    def __init__(
        self,
        *,
        config: Mapping[str, Any] | None = None,
        donchian_period: int = DONCHIAN_PERIOD,
        fast_ema_period: int = FAST_EMA_PERIOD,
        slow_ema_period: int = SLOW_EMA_PERIOD,
        stop_atr_multiple: float = STOP_ATR_MULTIPLE,
        trailing_atr_multiple: float = TRAILING_ATR_MULTIPLE,
    ) -> None:
        # ATR periyodu parametre DEĞİL: projenin tek ATR tanımı config'in `trailing.atr_period`
        # değeridir (CLAUDE.md). Modelin kendi periyodunu seçmesi, aynı "2×ATR" ifadesinin
        # modelden modele farklı mesafe anlamına gelmesi demek olurdu.
        self._atr_period = int(get_setting(dict(config) if config is not None else load_config(),
                                           "trailing.atr_period"))
        self._donchian_period = donchian_period
        self._fast_ema_period = fast_ema_period
        self._slow_ema_period = slow_ema_period
        self._stop_atr_multiple = stop_atr_multiple
        self._trailing_atr_multiple = trailing_atr_multiple

    def generate_signals(
        self,
        market: MarketData,
        peer_signals: Mapping[str, tuple[Signal, ...]] | None = None,
    ) -> list[Signal]:
        signals: list[Signal] = []
        # Sıralı gezinti: sinyal sırası max_positions dolduğunda hangi sembolün girdiğini
        # belirler; sözlük sırasına bırakmak koşuyu veri katmanının sırasına bağlardı.
        for symbol in sorted(market.ohlcv):
            signal = self._evaluate(symbol, market)
            if signal is not None:
                signals.append(signal)
        return signals

    def _evaluate(self, symbol: str, market: MarketData) -> Signal | None:
        frame = bars_until(market.ohlcv[symbol], market.as_of)
        if frame.empty or frame.index[-1] != market.as_of:
            # Sembol `as_of` barını taşımıyor: core/data.py bunları zaten dışlar, ama bir
            # sinyali bir bar geriden üretmek look-ahead kadar sessiz bir ölçüm hatasıdır.
            return None

        channel = donchian(frame, self._donchian_period)
        fast = ema(frame["close"], self._fast_ema_period)
        slow = ema(frame["close"], self._slow_ema_period)
        if channel is None or fast is None or slow is None:
            return None

        close = float(frame["close"].iloc[-1])
        if close > channel.upper and fast > slow:
            direction: Direction = "long"
            level, regime = channel.upper, "yukarı"
        elif close < channel.lower and fast < slow:
            direction = "short"
            level, regime = channel.lower, "aşağı"
        else:
            return None

        atr = average_true_range(frame, self._atr_period)
        if atr is None or atr <= 0.0:
            # Stop mesafesi üretilemiyor. Sessiz atlamak, bu modelin işlem sayısını ölçülemeyen
            # bir nedenle düşürürdü — kural 14'ün "atlama sessiz olamaz" gerekçesi burada da geçerli.
            logger.info(
                "%s %s: %s kırılımı atlandı, ATR(%d) hesaplanamadı",
                self.name, symbol, direction, self._atr_period,
            )
            return None

        distance = atr * self._stop_atr_multiple
        stop_price = close - distance if direction == "long" else close + distance
        breakout_pct = abs(close - level) / level * 100.0

        return Signal(
            symbol=symbol,
            direction=direction,
            stop_price=stop_price,
            trailing_atr=self._trailing_atr_multiple,
            reason=(
                f"{self._donchian_period} bar Donchian {'tepesi' if direction == 'long' else 'dibi'} "
                f"{level:.6g}, kapanış {close:.6g} ile %{breakout_pct:.2f} "
                f"{'üstünde' if direction == 'long' else 'altında'}; "
                f"rejim {regime} (EMA{self._fast_ema_period} {fast:.6g} "
                f"{'>' if direction == 'long' else '<'} EMA{self._slow_ema_period} {slow:.6g}); "
                f"stop {self._stop_atr_multiple:g}×ATR({self._atr_period})={distance:.6g} "
                f"uzakta ({stop_price:.6g}), trailing {self._trailing_atr_multiple:g}×ATR"
            ),
        )
