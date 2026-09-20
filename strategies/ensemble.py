"""Konsensüs meta-modeli: kendi sinyal mantığı yok, yalnızca akranların oylarını okur.

Tez tek cümledir: **bağımsız modellerin aynı sembolde aynı yönde üst üste binmesi, tek bir
modelin sinyalinden daha iyi bir giriş midir?** Bu yüzden model hiçbir gösterge hesaplamaz,
hiçbir fiyat kapısı kurmaz; tek girdisi `peer_signals`'tır (CLAUDE.md kural 4'ün dar
istisnası: o turda üretilmiş sinyallerin salt okunur kopyası). Kendi kapısını eklemek
ölçtüğü şeyi "üst üste binme + benim filtrem" yapardı ve tezi test edilemez hâle getirirdi.

Kurallar:

1. **Oy veren havuz sabittir** (`VOTER_POOL`) ve `random_ctrl` ile `buyhold` dışarıdadır.
   `random_ctrl` bilgisiz bir çekiliştir (kontrol grubu); oyu sayılırsa ensemble kısmen
   rastgele olur — üstelik iki modelin ölçümü birden bozulur: kontrolün "bilgisiz sinyal"
   tanımı ensemble üzerinden dolaylı olarak bir stratejiye bağlanır. `buyhold` bir referans
   çıpasıdır (kural 15), yönlü bir görüş bildirmez: her turda aynı iki sinyali üretir ve
   oyu sayılsaydı BTC/ETH'de kalıcı bir "long" tabanı yaratırdı.
2. **Eşik `MIN_VOTES` = 2 ayrı model.** Aynı model bir sembolde aynı yönde iki sinyal
   üretse bile TEK oy sayar: ölçülen şey modellerin üst üste binmesi, sinyal sayısı değil.
3. **Oylar eşit ağırlıklıdır.** Short-only modeller (`failed_breakout`, `downtrend_rally`)
   short tarafta diğerleriyle aynı ağırlığı taşır. Ağırlık kalibrasyonu ("hangi model daha
   iyi oy veriyor") ayrı bir tezdir ve bu modelin sorusunu — üst üste binme işe yarıyor mu —
   cevaplanamaz hâle getirirdi: iyi sonuç ağırlıktan mı, örtüşmeden mi geldiği ayrılamazdı.
4. **Zıt yön çakışması:** bir sembolde hem long hem short eşiği geçiyorsa işlem YOKTUR ve
   durum loglanır. Net olmayan bir tabloda bir tarafı seçmek (oy farkı, öncelik sırası)
   ensemble'ı gizli bir ağırlıklandırmaya çevirirdi. Eşiği geçmeyen TEK bir karşı oy ise
   bir çakışma değildir: iki long oyuna karşı bir short oyu, konsensüsün tanımı gereği
   konsensüsü bozmaz — yoksa tek bir muhalif her işlemi veto ederdi ve model "hiç kimse
   itiraz etmedi" tezini ölçmeye başlardı.

Boyut, maliyet ve defter kuralları bu modelde de birebir aynıdır (kural 1/2/3/7): ensemble
yalnızca yön ve seviye önerir.

Seviyeler katılımcılardan devralınır, yeniden hesaplanmaz:

- **Stop: katılan modellerin en GENİŞİ.** En muhafazakâr seçim budur — dar stop, aynı 1R'yi
  daha büyük notional ile taşımak ve R başına daha çok maliyet ödemek demektir (kural 14).
  Ortalama almak ya da kendi ATR katını kurmak, ensemble'ı katılımcıların hiçbirinin
  savunmadığı üçüncü bir modele çevirirdi. En geniş stop `max_stop_atr_multiple` tavanını
  aşarsa işlem `core/engine.py` tarafından atlanır ve loglanır; bu bilinçli bir sonuçtur:
  konsensüsün taşıdığı risk ölçülemiyorsa işlem de açılmaz.
- **Hedef: katılan modellerin en YAKINI, tek TP, fraction 1.0.** Kısmi çıkışları birleştirmek
  (fraction'ları toplamak, kademeleri sıraya dizmek) katılımcıların çıkış planlarından hiçbirine
  benzemeyen bir plan üretirdi. Hiçbir katılımcının hedefi yoksa sinyal hedefsiz gider.
- **`trailing_atr` istenmez.** Trailing bir çıkış tezidir ve katılımcılar arasında farklıdır;
  "en yakın hedef" ile "en geniş stop" dışında üçüncü bir birleştirme kuralı, ölçülen şeye
  bu modelin kendi katkısını eklerdi.

`reason` ayrıştırılabilir bir kuyrukla biter (`| voters=...`), böylece defterden hangi model
bileşiminin kazandığı sonradan gruplanabilir — tablo kolonlarını hiç kirletmeden.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Mapping, Sequence

from strategies.base import Direction, MarketData, Signal, Strategy, TakeProfit

logger = logging.getLogger(__name__)

# Oy veren modeller. random_ctrl (kontrol grubu) ve buyhold (referans çıpası) bilinçli
# olarak dışarıdadır — gerekçe modül docstring'inde.
VOTER_POOL: tuple[str, ...] = (
    "trend",
    "meanrev",
    "momentum",
    "squeeze",
    "confluence",
    "failed_breakout",
    "downtrend_rally",
    "avwap",
)
MIN_VOTES = 2
DIRECTIONS: tuple[Direction, ...] = ("long", "short")


class Ensemble(Strategy):
    name = "ensemble"
    allowed_directions: list[Direction] = ["long", "short"]
    is_meta = True

    def __init__(self, *, voter_pool: Sequence[str] = VOTER_POOL, min_votes: int = MIN_VOTES) -> None:
        self._voter_pool = tuple(voter_pool)
        self._min_votes = int(min_votes)

    def generate_signals(
        self,
        market: MarketData,
        peer_signals: Mapping[str, tuple[Signal, ...]] | None = None,
    ) -> list[Signal]:
        if peer_signals is None:
            # Meta yolu bağlanmamış: sessizce boş dönmek, modelin "o turda konsensüs yoktu"
            # görünmesi demekti — oysa hiç oy görmedi. İkisi ayırt edilebilir kalmalı.
            logger.warning(
                "%s: peer_signals None geldi (meta geçişi çalışmıyor), oy sayılamadı", self.name
            )
            return []

        ballots = self._ballots(peer_signals)
        if not ballots:
            logger.info(
                "%s: bu turda oy veren havuzdan hiç sinyal gelmedi (havuz=%s)",
                self.name, ",".join(self._voter_pool),
            )
            return []

        signals: list[Signal] = []
        # Sıralı gezinti: sinyal sırası max_positions dolduğunda hangi sembolün girdiğini
        # belirler; sözlük sırasına bırakmak sonucu akran modellerin çalışma sırasına bağlardı.
        for symbol in sorted({symbol for symbol, _ in ballots}):
            passing = [
                direction
                for direction in DIRECTIONS
                if len(ballots.get((symbol, direction), {})) >= self._min_votes
            ]
            if len(passing) > 1:
                logger.info(
                    "%s %s: işlem yok, iki yön de eşiği geçti (long=%s, short=%s)",
                    self.name, symbol,
                    ",".join(sorted(ballots[(symbol, "long")])),
                    ",".join(sorted(ballots[(symbol, "short")])),
                )
                continue
            if not passing:
                continue
            signal = self._build(symbol, passing[0], ballots[(symbol, passing[0])], market)
            if signal is not None:
                signals.append(signal)
        return signals

    def _ballots(
        self, peer_signals: Mapping[str, tuple[Signal, ...]]
    ) -> dict[tuple[str, Direction], dict[str, list[Signal]]]:
        """(sembol, yön) -> model adı -> o modelin sinyalleri; yalnızca havuzdaki modeller.

        Oy MODEL başınadır: aynı model aynı sembol/yön için birden çok sinyal üretirse hepsi
        tek oyun altında toplanır (seviyeler yine de en geniş stop / en yakın hedef seçiminde
        birlikte değerlendirilir).
        """
        ballots: dict[tuple[str, Direction], dict[str, list[Signal]]] = defaultdict(
            lambda: defaultdict(list)
        )
        for model in self._voter_pool:
            for signal in peer_signals.get(model, ()):
                ballots[(signal.symbol, signal.direction)][model].append(signal)
        return {key: dict(models) for key, models in ballots.items()}

    def _build(
        self,
        symbol: str,
        direction: Direction,
        voters: Mapping[str, Sequence[Signal]],
        market: MarketData,
    ) -> Signal | None:
        frame = market.ohlcv.get(symbol)
        if frame is None or market.as_of not in frame.index:
            # Akran sinyali `as_of` barı olmayan bir sembole işaret ediyor: seviyeleri
            # devralacak ortak bir referans yok. Sessiz atlamak ölçülemeyen bir eksilme olurdu.
            logger.info(
                "%s %s: konsensüs atlandı, sembol bu turun anlık görüntüsünde %s barını taşımıyor",
                self.name, symbol, market.as_of,
            )
            return None

        stop_owner, stop_price = self._widest_stop(direction, voters)
        if stop_owner is None or stop_price is None:
            logger.info(
                "%s %s: konsensüs atlandı, katılımcıların hiçbirinde stop yok (oy=%s)",
                self.name, symbol, ",".join(sorted(voters)),
            )
            return None

        tp_owner, tp_price = self._nearest_target(direction, voters)
        take_profits: tuple[TakeProfit, ...] = (
            () if tp_price is None else (TakeProfit(price=tp_price, fraction=1.0),)
        )

        names = sorted(voters)
        target_text = (
            "hedef yok (hiçbir katılımcı hedef vermedi)"
            if tp_price is None
            else f"hedef en yakın katılımcıdan {tp_price:.6g} ({tp_owner}), tamamı tek seferde"
        )
        return Signal(
            symbol=symbol,
            direction=direction,
            stop_price=stop_price,
            take_profits=take_profits,
            reason=(
                f"{len(names)}/{len(self._voter_pool)} model {direction} yönünde birleşti "
                f"(eşik {self._min_votes}); stop en geniş katılımcıdan {stop_price:.6g} "
                f"({stop_owner}); {target_text}"
                f" | voters={','.join(names)} | stop_from={stop_owner}"
                f" | tp_from={tp_owner if tp_owner is not None else 'none'}"
            ),
        )

    def _widest_stop(
        self, direction: Direction, voters: Mapping[str, Sequence[Signal]]
    ) -> tuple[str | None, float | None]:
        """En muhafazakâr stop: long'da en düşük, short'ta en yüksek stop fiyatı.

        Eşitlikte model adı alfabetik olarak seçilir — iki katılımcı aynı stop'u verdiğinde
        `stop_from` etiketinin akranların çalışma sırasına göre değişmemesi için.
        """
        best_owner: str | None = None
        best_price: float | None = None
        for model in sorted(voters):
            for signal in voters[model]:
                if signal.stop_price is None:
                    continue
                price = float(signal.stop_price)
                if best_price is None or (
                    price < best_price if direction == "long" else price > best_price
                ):
                    best_owner, best_price = model, price
        return best_owner, best_price

    def _nearest_target(
        self, direction: Direction, voters: Mapping[str, Sequence[Signal]]
    ) -> tuple[str | None, float | None]:
        """En yakın hedef: long'da en düşük, short'ta en yüksek TP fiyatı; kademeler birleşmez."""
        best_owner: str | None = None
        best_price: float | None = None
        for model in sorted(voters):
            for signal in voters[model]:
                for tp in signal.take_profits:
                    price = float(tp.price)
                    if best_price is None or (
                        price < best_price if direction == "long" else price > best_price
                    ):
                        best_owner, best_price = model, price
        return best_owner, best_price
