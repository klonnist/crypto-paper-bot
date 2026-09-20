"""MODEL 14 — VWAP sapma-dönüş sinyali, EV KURALLARIYLA. Tam yarışmacı.

Bu model iki kıyasın ucudur:

    model 13 ↔ model 14   Ev kurallarının katkısı — AMA TEK DEĞİŞKENLİ DEĞİL. Karar 23'ten
                          sonra iki model AYRI sinyal modülü okur (`vwap/clone_signal.py`
                          ↔ `vwap/signal.py`), yani aradaki fark altı eksende birden
                          ayrışır: (1) sinyal kuralları — VWAP çapası, σ tanımı, bant
                          barı, dönüş şartı, minimum bar; (2) boyutlandırma (sabit
                          teminat × 10x ↔ risk %1); (3) ev kapıları (yok ↔ %1 taban +
                          1.5R + zaman stop'u); (4) seçim politikası (evren listesi sırası
                          ↔ en güçlü tek aday); (5) bar başına sinyal (kotaya kadar 5 ↔ 1);
                          (6) evren (12 ↔ katmanın tamamı). Fark "ev kurallarının katkısı"
                          olarak OKUNAMAZ; okunabilen şey "iki sistemin toplam farkı"dır.
                          Tek değişkenli bir eksen isteniyorsa yeni bir model gerekir.
    model 14 ↔ scalp_fixed  Sinyal + çıkış yönetimi birlikte, beş kollu eşit ağırlıklı
                          çekilişe karşı ne yapıyor? Bu kıyasın okunabilmesi için burada
                          DEĞİŞEBİLECEK tek şey sinyal ve çıkış yönetimidir; geri kalan
                          her şey (boyutlandırma, maliyet, limitler, stop tabanı,
                          hedef/stop kapısı, zaman stop'u, katmanın evreni) scalp_fixed
                          ile birebir aynıdır.

**Parametre öğrenimi YOKTUR ve bilinçli olarak yoktur.** ATR ve hedef çarpanları
config'ten okunan SABİT değerlerdir. Bandit eklemek, model 14 ile scalp_fixed arasındaki
farka üçüncü bir değişken (adaptasyon) katardı — oysa adaptasyonun katkısı zaten
model 11 ↔ model 12 ekseninde ölçülüyor. Bir eksende bir değişken: projenin tamamının
tasarım ilkesi budur.

**Uygulanan ev kapıları** (`strategies/scalp/model.py` ile aynı değerler, aynı config
anahtarları — iki model aynı çıtayı görmezse aralarındaki fark çıtanın farkı olurdu):

- *Stop tabanı %1* (`scalp.min_stop_pct`): tur maliyeti ~%0.25'tir; daha dar stop'ta
  maliyet 0.25R'yi aşar. Stop GENİŞLETİLMEZ, kurulum ATLANIR ve atlama loglanır
  (kural 14'ün "atlama sessiz olamaz" şartı).
- *Hedef/stop ≥ 1.5* (`scalp.min_reward_risk`): sağlamayan kurulum atlanır. Kapının canlı
  kalması için hedef, projeksiyon ile VWAP'in YAKIN olanıdır (bkz.
  `strategies/vwap/signal.py`): yalnızca projeksiyon kullanmak kapıyı ölü koda çevirirdi.
- *Zaman stop'u 16 bar* (`scalp.time_stop_bars`, uygulama `strategies/time_stop.py`):
  scalp modelleriyle TEK KOPYADAN okunur. Bu kapı olmadan model 14 takılan pozisyonu
  süresiz taşırdı ve `scalp_fixed` kıyasına ölçülmeyen dördüncü bir değişken girerdi —
  üstelik %1 stop tabanı ile kural 11 birlikte pratikte ~1 eşzamanlı pozisyon dayattığı
  için tek bir takılı pozisyon sinyal akışını süresiz bloklardı.

**Barda tek sinyal.** En uzağa sapmış aday oynanır. Gerekçe scalp modellerininkiyle aynı
değildir (burada kol tahsisi yok) ama sonucu aynı olmalıdır: kıyas hedefi scalp_fixed bar
başına tek pozisyon açar ve model 14 beşini birden açsaydı aradaki ortalama R farkı
kısmen "kaç işlem açıldığı"nın farkı olurdu.

Rollere dikkat: bu modül boyut/komisyon/bakiye hesaplamaz (kural 1/2/3/7) ve deftere
yazmaz (kural 1). Sinyali `strategies/vwap/signal.py`'dedir ve model 13 ile PAYLAŞILMAZ
(karar 23); çıkış yönetimi `strategies/exit_management.py`'de, zaman stop'u
`strategies/time_stop.py`'dedir — bu ikisi tek kopyadır.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

from core.config import get_setting, load_config
from core.tags import format_tags
from strategies.base import (
    Direction,
    ExitInstruction,
    MarketData,
    Position,
    Signal,
    Strategy,
    TakeProfit,
)
from strategies.exit_management import ExitManagement
from strategies.time_stop import TimeStop
from strategies.vwap import signal as vwap_signal

logger = logging.getLogger(__name__)


class VwapManaged(Strategy):
    name = "vwap_managed"
    allowed_directions: list[Direction] = ["long", "short"]

    def __init__(self, *, config: Mapping[str, Any] | None = None) -> None:
        settings = dict(config) if config is not None else load_config()
        self._band_mult = float(get_setting(settings, "vwap.band_mult"))
        self._min_vwap_bars = int(get_setting(settings, "vwap.min_vwap_bars"))
        self._atr_period = int(get_setting(settings, "trailing.atr_period"))
        self._atr_multiple = float(get_setting(settings, "vwap.managed.atr_multiple"))
        self._target_reward_risk = float(
            get_setting(settings, "vwap.managed.target_reward_risk")
        )
        # Ev kapıları scalp modelleriyle AYNI anahtarlardan okunur: iki ayrı anahtar,
        # iki ayrı çıta demekti ve model 14 ↔ scalp_fixed kıyası bozulurdu.
        self._min_stop_pct = float(get_setting(settings, "scalp.min_stop_pct"))
        self._min_reward_risk = float(get_setting(settings, "scalp.min_reward_risk"))
        # Zaman stop'u scalp modelleriyle TEK KOPYADAN okunur: iki uygulama, iki
        # ayrı kural demekti ve scalp_fixed kıyasına sessiz bir değişken girerdi.
        self._time_stop = TimeStop.from_config(settings)
        self._exit = ExitManagement.from_config(settings)
        # Son taramanın eleme sayımı (kural 15). Ölçüme girmez, sinyalleri etkilemez.
        self._survey: vwap_signal.Survey | None = None

    def generate_signals(
        self,
        market: MarketData,
        peer_signals: Mapping[str, tuple[Signal, ...]] | None = None,
    ) -> list[Signal]:
        """Kapılardan geçen en güçlü tek aday. Evren katmanın kendi evrenidir (filtre yok).

        `propose` yerine `scan` çağrılır ve log satırı burada yazılır: ikisinin döndürdüğü
        ADAY LİSTESİ birebir aynıdır (`propose` = `scan` + tek bir log satırı), fark
        yalnızca sayımın bir DEĞER olarak elde kalmasıdır — tur raporuna düşebilmesi için
        (bkz. `take_survey`). Modelin gördüğü adaylar, sıraları ve seçimi değişmez.
        """
        candidates, survey = vwap_signal.scan(
            market,
            atr_period=self._atr_period,
            band_mult=self._band_mult,
            min_vwap_bars=self._min_vwap_bars,
        )
        self._survey = survey
        logger.info(
            "%s %s %s bandı=%.2fσ -> %s",
            self.name,
            vwap_signal.ARM_NAME,
            market.as_of.isoformat(),
            self._band_mult,
            survey.describe(),
        )
        for candidate in candidates:
            stop = vwap_signal.stop_price(candidate, atr_multiple=self._atr_multiple)
            projected = vwap_signal.projected_target(
                candidate, stop=stop, reward_risk=self._target_reward_risk
            )
            target = vwap_signal.nearest_target(candidate, projected=projected)
            if not self._passes_gates(candidate, stop=stop, target=target):
                continue
            return [self._signal(candidate, stop=stop, target=target)]
        return []

    def manage_positions(
        self, market: MarketData, positions: list[Position]
    ) -> list[ExitInstruction]:
        """Zaman stop'u: 16 bardır açık pozisyon piyasa fiyatından kapatılır.

        Kural `strategies/time_stop.py`de tek kopyadır ve scalp modelleriyle aynı
        config anahtarını (`scalp.time_stop_bars`) okur. Kapanış `manage_positions`
        üzerinden istenir (kural 10), dolum bir SONRAKİ barın açılışındadır
        (kural 13), yani gerçek ömür 17 bardır.
        """
        return self._time_stop.instructions(market, positions)

    def take_survey(self) -> Mapping[str, int] | None:
        """Son taramanın sayımı (kural 15). Sinyalleri hiçbir biçimde etkilemez.

        İki sayım birlikte döner (`Survey.report`): eleme SEBEPLERİ ve |z_prev| KOVALARI.
        Kovalar olmadan `bant_ici=13` satırı "band kıl payı mı kaçırdı, piyasa VWAP'e
        yapışık mıydı" sorusunu cevaplayamaz — ve bu soru, bandın ölçeğinin doğru olup
        olmadığının tek canlı kanıtıdır.

        `Survey.max_extension` ("en uzak sembol kaç σ'daydı") rapora değil loga kalır:
        sözleşme `Mapping[str, int]`tir ve tek bir kayan noktayı sayaçların arasına
        sıkıştırmak, okuyanın onu da bir sayım sanmasına açık kapı bırakırdı. Kova bir
        sayımdır, bu ayrımı bozmaz.
        """
        return None if self._survey is None else self._survey.report()

    def _passes_gates(
        self, candidate: vwap_signal.VwapCandidate, *, stop: float, target: float
    ) -> bool:
        """Stop tabanı ve hedef/stop kapısı. Her eleme GEREKÇESİYLE loglanır (kural 14)."""
        stop_pct = vwap_signal.stop_distance_pct(candidate, stop=stop)
        if stop_pct < self._min_stop_pct:
            logger.info(
                "%s %s: kurulum atlandı, stop mesafesi %%%.3f < taban %%%.3f "
                "(tur maliyeti bu mesafede 0.25R'yi aşar)",
                self.name, candidate.symbol, stop_pct * 100, self._min_stop_pct * 100,
            )
            return False
        reward_risk = vwap_signal.reward_risk_of(candidate, stop=stop, target=target)
        if reward_risk < self._min_reward_risk:
            logger.info(
                "%s %s: kurulum atlandı, hedef/stop %.2f < çıta %.2f (VWAP %.6g çok yakın)",
                self.name, candidate.symbol, reward_risk, self._min_reward_risk, candidate.vwap,
            )
            return False
        return True

    def _signal(
        self, candidate: vwap_signal.VwapCandidate, *, stop: float, target: float
    ) -> Signal:
        stop_pct = vwap_signal.stop_distance_pct(candidate, stop=stop)
        reward_risk = vwap_signal.reward_risk_of(candidate, stop=stop, target=target)
        return Signal(
            symbol=candidate.symbol,
            direction=candidate.direction,
            # Ev kuralı: boyut core/portfolio.py'de, risk_per_trade × sermaye / stop
            # mesafesi (kural 11) ve katmanın leverage_cap'i geçerli.
            stop_price=stop,
            take_profits=(TakeProfit(price=target, fraction=1.0),),
            **self._exit.signal_fields(),
            reason=format_tags(
                f"{candidate.detail()}; stop {self._atr_multiple:g}×ATR = "
                f"%{stop_pct * 100:.2f} ({stop:.6g}), hedef {target:.6g} "
                f"({reward_risk:.2f}R), {self._time_stop.describe()}; "
                f"{self._exit.describe()}",
                arm=vwap_signal.ARM_NAME,
                rr=reward_risk,
            ),
        )
