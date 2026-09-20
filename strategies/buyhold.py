"""Alım-tut referans çıpası: BTC %50 / ETH %50, bir kez alınır ve hiç satılmaz.

Bu bir yarışmacı değil, bir ZEMİNDİR (CLAUDE.md kural 15). Cevapladığı soru tek:
"model piyasayı yendi mi, yoksa yalnızca yükselen bir piyasada mı durdu?" On modelin
hepsi pozitif getiri üretse bile, hiçbiri bu satırı geçemiyorsa ölçümün sonucu
"stratejiler işe yarıyor" değildir.

Neden stop yok ve neden `sizing="notional_fraction"`: alım-tut'un tanımı stop'suz
olmasıdır. Ona yapay bir stop takmak onu bir trend modeline çevirirdi; risk formülüne
(`risk / |giriş − stop|`) sokmak ise imkânsızdır, paydası yoktur. Bu yüzden boyut
sermayenin sabit bir oranıdır ve kaldıraç 1x'e sabitlenir — uygulaması yine tek yetkili
yerde, `core/portfolio.py`'dedir (kural 3 delinmez).

Neden ağırlıklar burada ve %50/%50: çıpanın tek bir sembole (BTC) bağlanması, ölçümü
tek bir varlığın rejimine bağlardı; eşit ağırlık ise "hangi coin daha iyi" sorusunu
çıpanın dışında tutar — çıpa piyasayı temsil etmeli, bir tercihi değil.

**"Defter boşsa aç, sonra hiç işlem yapma"nın nasıl sağlandığı:** bu model defterini
göremez (kural 4/7), o yüzden her turda aynı iki sinyali üretir. Aynı sembol+yönde açık
pozisyon varsa emri reddeden tek yetkili yer `core/portfolio.py`'dir; davranış oradan
gelir. Bilgiyi buraya taşımak, stratejiye bakiye/pozisyon durumu sızdırmak (ve kuralı iki
yere bölmek) olurdu. Pratik sonuç aynıdır: ilk dolumdan sonra hiçbir işlem daha açılmaz,
`manage_positions` hiçbir zaman çıkış istemez, pozisyonlar süresiz taşınır.

Bunun görünür bedeli, her turun `signals=2 / filled=0` olmasıdır — gerçek bir boyutlandırma
arızasıyla AYNI görünüm. Bu yüzden ret sessiz değildir: `duplicate_position` sebep koduyla
tur raporuna sayılarak yazılır ve `zero_size` / `insufficient_cash` gibi arızalardan hem kodla
hem log seviyesiyle ayrılır (bkz. CLAUDE.md kural 15).
"""

from __future__ import annotations

import logging
from typing import Mapping

from strategies.base import (
    Direction,
    ExitInstruction,
    MarketData,
    Position,
    Signal,
    Strategy,
)

logger = logging.getLogger(__name__)

# Sembol -> sermaye payı. Toplam 1.0'ı aşmamalıdır: aşarsa çıpa kaldıraçlanır ve
# "1x alım-tut" olmaktan çıkar.
WEIGHTS: Mapping[str, float] = {
    "BTC-USDT-SWAP": 0.5,
    "ETH-USDT-SWAP": 0.5,
}


class BuyHold(Strategy):
    name = "buyhold"
    allowed_directions: list[Direction] = ["long"]
    is_benchmark = True

    def __init__(self, weights: Mapping[str, float] | None = None) -> None:
        self._weights = dict(weights if weights is not None else WEIGHTS)
        total = sum(self._weights.values())
        if total > 1.0:
            raise ValueError(f"ağırlık toplamı 1.0'ı aşamaz (kaldıraçsız çıpa): {total}")

    def generate_signals(
        self,
        market: MarketData,
        peer_signals: Mapping[str, tuple[Signal, ...]] | None = None,
    ) -> list[Signal]:
        signals: list[Signal] = []
        for symbol, fraction in self._weights.items():
            if symbol not in market.ohlcv:
                # Sembol o tur anlık görüntüde yoksa (gecikmiş/dışlanmış) sinyal üretmek
                # doğrulamada patlar ve tüm modeli düşürürdü (kural 8).
                logger.info(
                    "%s: %s bu turun anlık görüntüsünde yok, sinyal üretilmedi",
                    self.name, symbol,
                )
                continue
            signals.append(
                Signal(
                    symbol=symbol,
                    direction="long",
                    sizing="notional_fraction",
                    notional_fraction=fraction,
                    reason=f"alım-tut referansı: sermayenin %{fraction * 100:g}'i, 1x, stopsuz",
                )
            )
        return signals

    def manage_positions(
        self, market: MarketData, positions: list[Position]
    ) -> list[ExitInstruction]:
        """Asla çıkmaz. Çıpanın tüm anlamı, ölçüm penceresi boyunca pozisyonda kalmasıdır."""
        return []
