"""Kontrol grubu: bilgisiz sinyal — edge'in ölçüldüğü sıfır noktası.

Bu model bir strateji DEĞİLDİR; diğer dokuz modelin ürettiği sayıların referansıdır.
Sorduğu soru tek: *"bir modelin ortalama R'si, aynı maliyetler ve aynı limitler altında
rastgele açılmış işlemlerden ayırt edilebilir mi?"* Buy&hold çıpası (kural 15) "piyasa ne
yaptı"yı ölçer; bu satır ise "sinyalin kendisi bir şey söylüyor mu"yu ölçer. İkisi farklı
sorulardır ve bu yüzden bu model bir REFERANS değil, yarışmacıdır: boyutlandırması,
komisyonu, kayması, stop'u ve limitleri diğerleriyle BİREBİR aynıdır — tek farkı sinyalin
bilgisiz olmasıdır.

**Tasarımı bozulamaz.** Buraya eklenen her filtre (rejim, hacim, likidite, "kötü sembolleri
ele") kontrolü sessizce bir stratejiye çevirir ve o andan itibaren diğer modellerin farkı
neye karşı ölçtüğü bilinmez hâle gelir. Tek meşru eleme, işlemin KURULAMADIĞI durumlardır:
`as_of` barı olmayan ya da ATR'si hesaplanamayan sembolde stop mesafesi üretilemez.

**Stop 2×ATR ve sabit.** Stop mesafesi aynı zamanda maliyet ölçeğidir (kural 14): kontrolün
R ölçeği yarışmacılarınkiyle aynı bantta (1×–2.5×ATR) olmazsa `cost_per_r` kolonu kontrolü
haksız biçimde iyi ya da kötü gösterir. 2×, `trend` modeliyle aynı — bandın ortası.

**Tohum turdan tura karıştırılır.** RNG `config.yaml`'ın `random_seed` değerinden beslenir
ama her tur `as_of` ile birlikte tohumlanır: `random.Random(f"{seed}:{as_of}")`. Sabit bir
tohumla süreç başına tek bir RNG kurmak, her koşu ayrı bir süreç olduğu için HER TURDA aynı
çekilişi yapardı — kontrol grubu bilgisiz değil, sabit olurdu. Tur bazlı tohumlama ise iki
şeyi birden verir: aynı `as_of` ile yeniden koşulan bir tur birebir aynı sinyali üretir
(tekrarlanabilirlik), farklı turlar ise bağımsız çekilişler görür.

**RNG ağ katmanıyla PAYLAŞILMAZ.** `core/data.py` retry jitter'ı için kendi
`random.Random(random_seed)` örneğini tutar. Aynı örneği paylaşmak, çekilişi o turda kaç
kez yeniden denendiğine — yani borsanın o günkü keyfine — bağlardı: aynı `as_of` iki koşuda
farklı sembol seçebilirdi ve "tekrarlanabilir koşu" iddiası çökerdi. Modül düzeyinde
`random` fonksiyonlarının (global RNG) kullanılmaması da aynı gerekçeye dayanır.

Rollere dikkat: bu modül boyut/komisyon/bakiye hesaplamaz (kural 1/2/3/7); ATR
`core/indicators.py`'dedir. Pozisyon sayısı, short limiti ve tekrar reddi
`core/portfolio.py`'nin işidir — kontrol de o kapılardan diğerleriyle aynı şekilde geçer.
"""

from __future__ import annotations

import logging
import random
from typing import Any, Mapping

from core.config import get_setting, load_config
from core.indicators import average_true_range, bars_until
from strategies.base import Direction, MarketData, Signal, Strategy

logger = logging.getLogger(__name__)

STOP_ATR_MULTIPLE = 2.0
DIRECTIONS: tuple[Direction, ...] = ("long", "short")
SIGNALS_PER_ROUND = 1


class RandomControl(Strategy):
    name = "random_ctrl"
    allowed_directions: list[Direction] = ["long", "short"]

    def __init__(
        self,
        *,
        config: Mapping[str, Any] | None = None,
        stop_atr_multiple: float = STOP_ATR_MULTIPLE,
    ) -> None:
        settings = dict(config) if config is not None else load_config()
        self._atr_period = int(get_setting(settings, "trailing.atr_period"))
        self._seed = int(get_setting(settings, "random_seed"))
        self._stop_atr_multiple = stop_atr_multiple

    def generate_signals(
        self,
        market: MarketData,
        peer_signals: Mapping[str, tuple[Signal, ...]] | None = None,
    ) -> list[Signal]:
        eligible = self._eligible(market)
        if not eligible:
            logger.info(
                "%s: %s barında işlem kurulabilir sembol yok, çekiliş yapılmadı",
                self.name, market.as_of,
            )
            return []

        rng = self._round_rng(market)
        signals: list[Signal] = []
        for _ in range(SIGNALS_PER_ROUND):
            symbol, close, atr = rng.choice(eligible)
            direction = rng.choice(DIRECTIONS)
            distance = atr * self._stop_atr_multiple
            stop_price = close - distance if direction == "long" else close + distance
            signals.append(
                Signal(
                    symbol=symbol,
                    direction=direction,
                    stop_price=stop_price,
                    reason=(
                        f"KONTROL GRUBU: bilgisiz çekiliş — {len(eligible)} uygun sembol "
                        f"arasından {symbol}, yön {direction}; tohum {self._seed} + "
                        f"{market.as_of:%Y-%m-%d %H:%M} UTC; stop "
                        f"{self._stop_atr_multiple:g}×ATR({self._atr_period})={distance:.6g} "
                        f"uzakta ({stop_price:.6g})"
                    ),
                )
            )
        return signals

    def _round_rng(self, market: MarketData) -> random.Random:
        """Tur başına bağımsız RNG: tohum sabit, çekiliş `as_of` ile karışır.

        Modül düzeyinde `random` kullanılmaz ve `core/data.py`'nin jitter RNG'si ile örnek
        paylaşılmaz (bkz. modül docstring'i): ikisi de aynı `as_of` için aynı sinyalin
        üretilmesi garantisini kırardı.
        """
        return random.Random(f"{self._seed}:{market.as_of.isoformat()}")

    def _eligible(self, market: MarketData) -> list[tuple[str, float, float]]:
        """`as_of` barını taşıyan ve ATR'si hesaplanabilen semboller, ADI SIRALI.

        Sıra çekilişin tekrarlanabilirliğinin parçasıdır: sözlük sırasına bırakmak, aynı
        tohumla aynı turun veri katmanının sırasına göre farklı sembol seçmesi demekti.
        Eleme ölçütü bir görüş değil, işlemin kurulabilirliğidir — kontrolün tasarımı
        gereği burada filtre olamaz.
        """
        eligible: list[tuple[str, float, float]] = []
        for symbol in sorted(market.ohlcv):
            frame = bars_until(market.ohlcv[symbol], market.as_of)
            if frame.empty or frame.index[-1] != market.as_of:
                continue
            atr = average_true_range(frame, self._atr_period)
            if atr is None or atr <= 0.0:
                continue
            eligible.append((symbol, float(frame["close"].iloc[-1]), atr))
        return eligible
