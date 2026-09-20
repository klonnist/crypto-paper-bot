"""RSI + Bollinger ortalamaya dönüş (yarışmacı model).

Tez: fiyat kısa vadede bandın dışına taştığında (RSI aşırı bölgede VE kapanış Bollinger
bandının dışında), orta banda (20 SMA) doğru bir geri çekilme olasılığı rastgeleden
yüksektir. Hedef bu yüzden bir ATR katı değil, göstergenin kendi orta bandıdır: modelin
tezi "ne kadar kazanacağım" değil, "fiyat ortalamasına döner"dir; TP'yi tezden bağımsız bir
sayıya bağlamak, ölçülen şeyi ortalamaya dönüş olmaktan çıkarırdı. Tek TP, fraction 1.0 —
kısmi çıkış ayrı bir tezdir (kâr koruma) ve bu modele karıştırılmaz.

**Short'un BTC rejim kapısı.** Short sinyali yalnızca BTC 4h kapanışı kendi 200 EMA'sının
ALTINDAYKEN geçerlidir. Gerekçe ölçüm tarafındadır: kripto piyasasında altcoin'lerin çoğu
BTC ile yüksek korelasyonludur ve boğa rejiminde "aşırı alım" göstergesi sürekli tetiklenir
— filtresiz bir short kolu, sinyalin kalitesini değil yalnızca rejim yönünü ölçerdi. Projenin
ana sorusu "short işlemler long'lardan daha mı başarılı" olduğuna göre, short kolunu rejimden
arındırmak o sorunun cevabını baştan kirletir.

Kapı BTC'ye bağlıdır çünkü `as_of` çıpası da BTC'dir (bkz. CLAUDE.md, `core/data.py`): her
model aynı barı "şimdi" sayar, dolayısıyla bu filtre tüm semboller için aynı anda ve aynı
biçimde açılıp kapanır — sembol başına ayrı bir rejim tanımı, aynı turda birbiriyle çelişen
kapılar üretirdi.

Rollere dikkat: bu modül boyut/komisyon/bakiye hesaplamaz (kural 1/2/3/7) ve gösterge
matematiğini kendisi yazmaz — hepsi `core/indicators.py`'dedir.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

from core.config import get_setting, load_config
from core.indicators import average_true_range, bars_until, bollinger, ema, rsi
from strategies.base import Direction, MarketData, Signal, Strategy, TakeProfit

logger = logging.getLogger(__name__)

RSI_PERIOD = 14
RSI_OVERSOLD = 30.0
RSI_OVERBOUGHT = 70.0
BOLLINGER_PERIOD = 20
BOLLINGER_STD = 2.0
STOP_ATR_MULTIPLE = 2.0
BTC_REGIME_EMA_PERIOD = 200


class MeanReversion(Strategy):
    name = "meanrev"
    allowed_directions: list[Direction] = ["long", "short"]

    def __init__(
        self,
        *,
        config: Mapping[str, Any] | None = None,
        rsi_period: int = RSI_PERIOD,
        rsi_oversold: float = RSI_OVERSOLD,
        rsi_overbought: float = RSI_OVERBOUGHT,
        bollinger_period: int = BOLLINGER_PERIOD,
        bollinger_std: float = BOLLINGER_STD,
        stop_atr_multiple: float = STOP_ATR_MULTIPLE,
        btc_regime_ema_period: int = BTC_REGIME_EMA_PERIOD,
    ) -> None:
        # ATR periyodu parametre değil: tek ATR tanımı config'ten gelir (bkz. strategies/trend.py).
        self._atr_period = int(get_setting(dict(config) if config is not None else load_config(),
                                           "trailing.atr_period"))
        self._rsi_period = rsi_period
        self._rsi_oversold = rsi_oversold
        self._rsi_overbought = rsi_overbought
        self._bollinger_period = bollinger_period
        self._bollinger_std = bollinger_std
        self._stop_atr_multiple = stop_atr_multiple
        self._btc_regime_ema_period = btc_regime_ema_period

    def generate_signals(
        self,
        market: MarketData,
        peer_signals: Mapping[str, tuple[Signal, ...]] | None = None,
    ) -> list[Signal]:
        shorts_allowed, btc_note = self._btc_regime(market)
        signals: list[Signal] = []
        for symbol in sorted(market.ohlcv):
            signal = self._evaluate(symbol, market, shorts_allowed=shorts_allowed, btc_note=btc_note)
            if signal is not None:
                signals.append(signal)
        return signals

    def _btc_regime(self, market: MarketData) -> tuple[bool, str]:
        """Short kapısı: BTC kapanışı 200 EMA'sının altında mı?

        EMA hesaplanamıyorsa (yeterli bar yok) kapı KAPALI kalır. Açık varsaymak, filtrenin
        var olmadığı bir dönemde short açmak — yani modeli sessizce başka bir modele çevirmek
        olurdu; kapalı varsaymanın bedeli yalnızca eksik işlemdir ve o log'a yazılır.
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
        note = (
            f"BTC {btc_close:.6g}, {self._btc_regime_ema_period} EMA {btc_ema:.6g} "
            f"{'altında' if below else 'üstünde'}"
        )
        return below, note

    def _evaluate(
        self, symbol: str, market: MarketData, *, shorts_allowed: bool, btc_note: str
    ) -> Signal | None:
        frame = bars_until(market.ohlcv[symbol], market.as_of)
        if frame.empty or frame.index[-1] != market.as_of:
            return None

        bands = bollinger(frame["close"], self._bollinger_period, self._bollinger_std)
        strength = rsi(frame["close"], self._rsi_period)
        if bands is None or strength is None:
            return None

        close = float(frame["close"].iloc[-1])
        if strength < self._rsi_oversold and close < bands.lower:
            direction: Direction = "long"
            band, band_name = bands.lower, "alt"
        elif strength > self._rsi_overbought and close > bands.upper:
            if not shorts_allowed:
                logger.info(
                    "%s %s: short sinyali bastırıldı, BTC rejim kapısı kapalı (%s)",
                    self.name, symbol, btc_note,
                )
                return None
            direction = "short"
            band, band_name = bands.upper, "üst"
        else:
            return None

        atr = average_true_range(frame, self._atr_period)
        if atr is None or atr <= 0.0:
            logger.info(
                "%s %s: %s sinyali atlandı, ATR(%d) hesaplanamadı",
                self.name, symbol, direction, self._atr_period,
            )
            return None

        distance = atr * self._stop_atr_multiple
        stop_price = close - distance if direction == "long" else close + distance
        # Orta bant tezin hedefi, ama geometri girişe göre doğru tarafta olmak ZORUNDA
        # (core/validate.py). Bandın dışında kapanan bir bar için bu normalde sağlanır;
        # sağlanmadığı uç durumda (bant kapanışa eşit) hedefsiz gitmek, kural 8'e takılıp
        # tüm modeli düşürmekten iyidir.
        target_valid = (
            bands.middle > close if direction == "long" else bands.middle < close
        )
        if not target_valid:
            logger.info(
                "%s %s: orta bant (%.10g) girişin (%.10g) doğru tarafında değil, TP'siz açılıyor",
                self.name, symbol, bands.middle, close,
            )

        band_pct = abs(close - band) / band * 100.0
        return Signal(
            symbol=symbol,
            direction=direction,
            stop_price=stop_price,
            take_profits=(
                (TakeProfit(price=bands.middle, fraction=1.0),) if target_valid else ()
            ),
            reason=self._reason(
                direction=direction,
                strength=strength,
                band=band,
                band_name=band_name,
                band_pct=band_pct,
                close=close,
                distance=distance,
                stop_price=stop_price,
                middle=bands.middle if target_valid else None,
                btc_note=btc_note,
            ),
        )

    def _reason(
        self,
        *,
        direction: Direction,
        strength: float,
        band: float,
        band_name: str,
        band_pct: float,
        close: float,
        distance: float,
        stop_price: float,
        middle: float | None,
        btc_note: str,
    ) -> str:
        """Deftere ve dashboard'a giden gerekçe: "model neden bu işlemi yaptı" denetimi buradan.

        Eşik ve ölçülen değer YAN YANA yazılır (ör. "RSI 24 (<30)"): yalnızca ölçümü yazmak,
        sonradan eşiğin ne olduğunu koddan/commit geçmişinden aramayı gerektirirdi.
        """
        threshold = self._rsi_oversold if direction == "long" else self._rsi_overbought
        comparison = "<" if direction == "long" else ">"
        side = "altında" if direction == "long" else "üstünde"
        parts = [
            f"RSI({self._rsi_period}) {strength:.0f} ({comparison}{threshold:g})",
            f"{band_name} Bollinger({self._bollinger_period},{self._bollinger_std:g}) "
            f"{band:.6g} bandının %{band_pct:.2f} {side} kapanış ({close:.6g})",
            f"stop {self._stop_atr_multiple:g}×ATR({self._atr_period})={distance:.6g} "
            f"uzakta ({stop_price:.6g})",
            f"TP orta bant {middle:.6g}" if middle is not None else "TP yok (orta bant girişin yanlış tarafında)",
        ]
        if direction == "short":
            parts.append(f"short rejim kapısı açık: {btc_note}")
        return "; ".join(parts)
