"""Başarısız kırılım (tuzak) — yalnızca SHORT açan yarışmacı model.

Tez: 20 barın zirvesini süpüren ama arkasını getiremeyen bir kırılım, yön değiştiren bir
hareketin başlangıcı değil, likidite toplayan bir TUZAKTIR. Kırılımın üstünde alan ve
stop'unu zirvenin altına koyan katılımcılar, fiyat seviyenin altına kapandığında sıkışır;
modelin sattığı şey bu sıkışmadır.

Dört kapı birden aranır — hiçbiri opsiyonel değil, çünkü her biri tezin bir parçasıdır:

1. **Süpürme** — barın FİTİLİ (high) son 20 barın zirvesini aşmalı. Kapanışla tanımlamak
   tuzağın kendisini eleyerek "başarılı kırılım" arardı; süpürmenin tanımı fitildir.
2. **Tuzak teyidi** — kırılımı takip eden 1-2 bar içinde aynı seviyenin ALTINA kapanış.
   Teyit `as_of` barındadır: kırılım barı ya bir ya iki bar geride olabilir.
3. **Zayıf hacim** — kırılım barının hacmi kendi 20 barlık ortalamasının ALTINDA. Hacimle
   gelen bir kırılımın geri gelmesi tuzak değil, sıradan bir geri çekilmedir; bu kapı
   modelin ölçtüğü şeyi "katılımsız kırılım" ile sınırlar.
4. **RSI ayı uyumsuzluğu** — süpürme barının zirvesi bir önceki teyitli zirvenin ÜSTÜNDE,
   RSI'ı ise ALTINDA. Fiyatın yeni zirvesini momentumun teyit etmemesi, süpürmenin
   alıcıdan değil stop avından geldiği iddiasını taşır.

**Teyit BİRİNCİ kapanışta aranır.** Süpürme ile `as_of` arasındaki barlardan biri zaten
seviyenin altına kapanmışsa kurulum o barda oluşmuştur ve bu turda tekrar üretilmez:
aksi hâlde aynı tuzak iki tur üst üste sinyal üretir, `core/portfolio.py` ikincisini
`duplicate_position` ile reddeder (kural 15) ve modelin sinyal sayısı ölçülemeyen bir
nedenle şişerdi.

**Funding önceliği boyut değil SIRA demektir.** Son 3 periyodun funding ortalaması pozitif
ve yükseliyorsa kalabalık long tarafındadır — tuzak tezinin en güçlü olduğu yer. Model bunu
pozisyonu büyüterek kullanamaz (kural 3/11): boyutlandırmanın tek yetkili yeri
`core/portfolio.py`'dir. Kullanabileceği tek kanal SIRADIR — `max_positions` dolduğunda
motor sinyalleri geldikleri sırada doldurur, dolayısıyla öncelikli sinyalleri listenin
başına almak "öncelik ver" talimatının boyutlandırmaya dokunmayan tam karşılığıdır. Sıra
gerekçesi `reason`a da yazılır ki defterden denetlenebilsin.

**Stop fitilin üstüdür ve bu model tavana sık takılır.** Stop = süpürme fitilinin tepesi ya
da 1×ATR — hangisi GENİŞSE. Uzun fitiller bu mesafeyi `max_stop_atr_multiple` tavanının
üstüne çıkarır ve o işlem ATLANIR (kural 14). Stop tavana çekilmez: çekmek, modelin "stop
fitilin üstünde olmalı" tezini sessizce başka bir modele çevirirdi. Atlama sessiz de
değildir — her eleme gerekçesiyle loglanır, bandın gerçekten tuttuğu ise raporun
`avg_stop_distance_pct` kolonundan okunur.

**Hedef önce son swing dip, o yoksa 2R.** Tezin kendi bittiği yer, süpürmeden önceki dip
seviyesidir; girişin altında böyle bir dip yoksa (tuzak zaten en dipte oluştuysa) hedef
risk biriminden türer. Sabit bir R hedefini dipten öne almak, modeli "tuzak" modelinden
"sabit R hasat eden" bir modele çevirirdi.

Rollere dikkat: bu modül boyut/komisyon/bakiye hesaplamaz (kural 1/2/3/7) ve gösterge
matematiğini kendisi yazmaz — Donchian, RSI, ATR, SMA ve fraktal pivotlar
`core/indicators.py`'dedir.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Mapping

import pandas as pd

from core.config import get_setting, load_config
from core.indicators import (
    average_true_range,
    bars_until,
    donchian,
    local_highs,
    local_lows,
    rsi_series,
    sma,
)
from strategies.base import Direction, MarketData, Signal, Strategy, TakeProfit

logger = logging.getLogger(__name__)

BREAKOUT_LOOKBACK = 20
# Teyit penceresi: kırılım barı `as_of`'tan 1 ya da 2 bar geride olabilir.
CONFIRMATION_BARS = 2
VOLUME_PERIOD = 20
RSI_PERIOD = 14
PIVOT_ORDER = 2  # fraktal pivot: iki yanında 2'şer bar (core.indicators.local_highs)
STOP_ATR_MULTIPLE = 1.0  # fitil dar kaldığında devreye giren TABAN, tavan değil
TARGET_R_MULTIPLE = 2.0
FUNDING_PRIORITY_PERIODS = 3


@dataclass(frozen=True, kw_only=True)
class _Sweep:
    """Süpürme barı ve tuzağı tanımlayan ölçüler."""

    position: int  # kırılım barının konum indeksi
    level: float  # süpürülen 20 bar zirvesi
    wick_top: float  # süpürmeden `as_of`'a kadarki en yüksek fitil
    volume_ratio: float
    bars_to_confirmation: int


@dataclass(frozen=True, kw_only=True)
class _Divergence:
    previous_high: float
    previous_rsi: float
    breakout_high: float
    breakout_rsi: float


class FailedBreakout(Strategy):
    name = "failed_breakout"
    allowed_directions: list[Direction] = ["short"]

    def __init__(
        self,
        *,
        config: Mapping[str, Any] | None = None,
        breakout_lookback: int = BREAKOUT_LOOKBACK,
        confirmation_bars: int = CONFIRMATION_BARS,
        volume_period: int = VOLUME_PERIOD,
        rsi_period: int = RSI_PERIOD,
        pivot_order: int = PIVOT_ORDER,
        stop_atr_multiple: float = STOP_ATR_MULTIPLE,
        target_r_multiple: float = TARGET_R_MULTIPLE,
    ) -> None:
        settings = dict(config) if config is not None else load_config()
        # ATR periyodu ve stop tavanı parametre DEĞİL: ikisi de tüm modeller için ortaktır
        # (bkz. strategies/trend.py ve CLAUDE.md kural 14), modelin kendi kopyası olamaz.
        self._atr_period = int(get_setting(settings, "trailing.atr_period"))
        self._max_stop_atr_multiple = float(get_setting(settings, "max_stop_atr_multiple"))
        self._breakout_lookback = breakout_lookback
        self._confirmation_bars = confirmation_bars
        self._volume_period = volume_period
        self._rsi_period = rsi_period
        self._pivot_order = pivot_order
        self._stop_atr_multiple = stop_atr_multiple
        self._target_r_multiple = target_r_multiple

    def generate_signals(
        self,
        market: MarketData,
        peer_signals: Mapping[str, tuple[Signal, ...]] | None = None,
    ) -> list[Signal]:
        prioritized: list[Signal] = []
        rest: list[Signal] = []
        for symbol in sorted(market.ohlcv):
            result = self._evaluate(symbol, market)
            if result is None:
                continue
            signal, has_priority = result
            (prioritized if has_priority else rest).append(signal)
        # Öncelik yalnızca sıradadır (bkz. modül docstring'i): motor max_positions dolana
        # kadar sinyalleri geldikleri sırada doldurur.
        return prioritized + rest

    def _evaluate(self, symbol: str, market: MarketData) -> tuple[Signal, bool] | None:
        frame = bars_until(market.ohlcv[symbol], market.as_of)
        if frame.empty or frame.index[-1] != market.as_of:
            return None

        sweep = self._find_sweep(frame)
        if sweep is None:
            return None

        if sweep.volume_ratio >= 1.0:
            logger.info(
                "%s %s: süpürme (%.10g seviyesi) tuzak sayılmadı, kırılım barının hacmi "
                "%d bar ortalamasının %.2f katı — zayıf kırılım değil",
                self.name, symbol, sweep.level, self._volume_period, sweep.volume_ratio,
            )
            return None

        divergence = self._bearish_divergence(frame, sweep)
        if divergence is None:
            return None

        close = float(frame["close"].iloc[-1])
        atr = average_true_range(frame, self._atr_period)
        if atr is None or atr <= 0.0:
            logger.info(
                "%s %s: tuzak atlandı, ATR(%d) hesaplanamadı — stop mesafesi üretilemez",
                self.name, symbol, self._atr_period,
            )
            return None

        # "Fitilin üstü VEYA 1×ATR, hangisi genişse": ATR bir taban, tavan değil. Dar
        # fitilli bir süpürmede stop'u fitilin hemen üstüne koymak, R'yi gürültü ölçeğine
        # indirir ve R başına maliyeti diğer modellerle kıyaslanamaz hâle getirirdi.
        stop_price = max(sweep.wick_top, close + atr * self._stop_atr_multiple)
        distance = stop_price - close
        multiple = distance / atr
        if multiple > self._max_stop_atr_multiple:
            logger.info(
                "%s %s: tuzak atlandı, fitil tepesi (%.10g) %.2f×ATR(%d) uzakta ve tavanı "
                "(%.2f×) aşıyor — stop tavana ÇEKİLMEZ (kural 14), işlem atlanır",
                self.name, symbol, sweep.wick_top, multiple, self._atr_period,
                self._max_stop_atr_multiple,
            )
            return None

        target, target_note = self._target(frame, close=close, distance=distance)
        has_priority, funding_note = self._funding_priority(symbol, market)

        return (
            Signal(
                symbol=symbol,
                direction="short",
                stop_price=stop_price,
                take_profits=(TakeProfit(price=target, fraction=1.0),),
                reason=(
                    f"{self._breakout_lookback} bar zirvesi {sweep.level:.6g} fitille "
                    f"süpürüldü (tepe {sweep.wick_top:.6g}), {sweep.bars_to_confirmation} bar "
                    f"sonra altına kapanış {close:.6g}; kırılım hacmi {sweep.volume_ratio:.2f}× "
                    f"({self._volume_period} bar ortalaması, eşik <1.00×); RSI"
                    f"({self._rsi_period}) ayı uyumsuzluğu: fiyat {divergence.previous_high:.6g}"
                    f"->{divergence.breakout_high:.6g} yükselirken RSI "
                    f"{divergence.previous_rsi:.0f}->{divergence.breakout_rsi:.0f} düştü; "
                    f"stop fitil tepesi veya {self._stop_atr_multiple:g}×ATR"
                    f"({self._atr_period}) ({stop_price:.6g}, {multiple:.2f}×ATR, tavan "
                    f"{self._max_stop_atr_multiple:g}×); {target_note}; funding {funding_note}"
                ),
            ),
            has_priority,
        )

    # ------------------------------------------------------------------ #
    # Kapılar
    # ------------------------------------------------------------------ #
    def _find_sweep(self, frame: pd.DataFrame) -> _Sweep | None:
        """`as_of`'tan 1-2 bar geride, teyidi BU barda tamamlanan süpürme.

        En güncel aday önce denenir: iki ayrı süpürme de teyit edilebiliyorsa tuzağı
        kuran son hareket geçerlidir.
        """
        close = float(frame["close"].iloc[-1])
        for gap in range(1, self._confirmation_bars + 1):
            position = len(frame) - 1 - gap
            if position <= 0:
                continue
            history = frame.iloc[: position + 1]  # son barı süpürme barı olan dilim
            channel = donchian(history, self._breakout_lookback)
            if channel is None:
                continue
            level = channel.upper
            if float(frame["high"].iloc[position]) <= level:
                continue
            if float(frame["close"].iloc[position]) < level:
                # Süpürme barı zaten seviyenin altına kapanmış: bu aynı bar içinde
                # reddedilen bir kırılımdır, "takip eden barda teyit" değil. Ayrı bir
                # kurulumdur ve bu model onu ölçmez.
                continue
            if close >= level:
                continue
            between = frame["close"].iloc[position + 1 : -1]
            if (between < level).any():
                # Teyit daha önceki bir barda olmuş; kurulum orada üretilmişti.
                continue
            volume_ratio = self._volume_ratio(frame, position)
            if volume_ratio is None:
                continue
            return _Sweep(
                position=position,
                level=level,
                wick_top=float(frame["high"].iloc[position:].max()),
                volume_ratio=volume_ratio,
                bars_to_confirmation=gap,
            )
        return None

    def _volume_ratio(self, frame: pd.DataFrame, position: int) -> float | None:
        """Kırılım barının hacmi / kendinden ÖNCEKİ `volume_period` barın ortalaması.

        Ortalama kırılım barını içerseydi barın kendi hacmi eşiği yukarı çeker ve "zayıf
        kırılım" tanımı barın büyüklüğüne göre kayardı (bkz. strategies/squeeze.py).
        """
        average = sma(frame["volume"].iloc[:position], self._volume_period)
        if average is None or average <= 0.0:
            return None
        return float(frame["volume"].iloc[position]) / average

    def _bearish_divergence(self, frame: pd.DataFrame, sweep: _Sweep) -> _Divergence | None:
        """Süpürme zirvesi bir önceki TEYİTLİ zirvenin üstünde, RSI'ı altında mı?

        Karşılaştırma noktası fraktal pivottur (`local_highs`): süpürme barının kendisi
        henüz pivot olamaz (sağ yanı oluşmadı), ama tezin sorduğu şey zaten "bu yeni zirve
        bir öncekine göre momentumsuz mu" — iki zirve de aynı tanımla okunur.
        """
        strength = rsi_series(frame["close"], self._rsi_period)
        highs = local_highs(frame["high"].iloc[: sweep.position], order=self._pivot_order)
        if not highs:
            return None
        previous = highs[-1]

        breakout_high = float(frame["high"].iloc[sweep.position])
        previous_high = float(frame["high"].iloc[previous])
        breakout_rsi = float(strength.iloc[sweep.position])
        previous_rsi = float(strength.iloc[previous])
        if pd.isna(breakout_rsi) or pd.isna(previous_rsi):
            return None
        if breakout_high <= previous_high or breakout_rsi >= previous_rsi:
            return None
        return _Divergence(
            previous_high=previous_high,
            previous_rsi=previous_rsi,
            breakout_high=breakout_high,
            breakout_rsi=breakout_rsi,
        )

    def _target(
        self, frame: pd.DataFrame, *, close: float, distance: float
    ) -> tuple[float, str]:
        """Girişin altındaki en güncel swing dip; yoksa 2R (bkz. modül docstring'i)."""
        for position in reversed(local_lows(frame["low"], order=self._pivot_order)):
            low = float(frame["low"].iloc[position])
            if low < close:
                return low, f"hedef son swing dip {low:.6g}"
        target = close - distance * self._target_r_multiple
        return target, (
            f"hedef {self._target_r_multiple:g}R {target:.6g} "
            "(girişin altında swing dip yok)"
        )

    def _funding_priority(self, symbol: str, market: MarketData) -> tuple[bool, str]:
        """Son 3 periyodun ortalaması pozitif VE seri yükseliyorsa sinyal öne alınır.

        "Yükseliyor" iki uç arasında ölçülür (son > ilk): ara periyottaki tek bir sapma
        kalabalıklığın yönünü değiştirmez, ama pencerenin iki ucu arasındaki fark değiştirir.
        Funding verisi yoksa öncelik YOKTUR — eksik veriyi "öncelik yok" saymak, onu
        sentetik bir sıraya çevirmekten iyidir (bkz. core/funding.py'nin ffill reddi).
        """
        series = market.funding.get(symbol)
        if series is None or len(series) == 0:
            return False, "geçmişi yok, öncelik yok"
        recent = series.loc[: market.as_of].tail(FUNDING_PRIORITY_PERIODS)
        if len(recent) < FUNDING_PRIORITY_PERIODS:
            return False, (
                f"son {FUNDING_PRIORITY_PERIODS} periyot tamamlanmadı ({len(recent)}), öncelik yok"
            )
        values = recent.to_numpy(dtype="float64")
        average = float(values.mean())
        rising = float(values[-1]) > float(values[0])
        note = (
            f"son {FUNDING_PRIORITY_PERIODS} periyot ortalaması %{average * 100:+.4f}, "
            f"{'yükseliyor' if rising else 'yükselmiyor'}"
        )
        if average > 0.0 and rising:
            return True, note + " — sinyal listenin başına alındı"
        return False, note + " — öncelik yok"
