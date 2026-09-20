"""MODEL 11 — Thompson sampling ile kollar arası tahsis (15 dakikalık scalp katmanı).

Ölçtüğü soru: **beş sabit kol arasında tahsisi ÖĞRENMEK, eşit dağıtmaktan daha iyi bir
sonuç üretir mi?** Cevabın okunabilmesi için model 12 (`scalp_fixed`) birebir aynı kolları
birebir aynı kapılarla, ama öğrenmeden oynar. İki modelin ortalama R farkı adaptasyonun
katkısıdır — başka hiçbir şey farklı olmadığı için.

**Posterior YALNIZCA kapanmış işlemlerin gerçekleşmiş R'sinden beslenir.** Açık pozisyon
hiçbir yoldan giremez: `core/engine.py` modele yalnızca KAPANMIŞ satırları verir
(`Strategy.observe_closed_trades`), açık pozisyonun kâğıt üstündeki kârı bu listede yoktur.
Girseydi model, henüz gerçekleşmemiş bir sonucu öğrenir ve iyi giden ama henüz kapanmamış
bir kolu kendi kendine ödüllendirirdi — üstelik aynı pozisyon kapandığında ikinci kez.

**Durum deftere yazılır ve tekrar üretilebilir.** Ayrı bir `bandit_state.json` YOKTUR ve
bilinçli olarak yoktur: posterior, `ledgers_scalp/scalp_bandit/trades.csv`'nin saf bir
fonksiyonudur (kol etiketi + gerçekleşen R + kayan pencere). Ayrı bir durum dosyası,
defterle senkron kalması ayrıca test edilmesi gereken İKİNCİ bir doğruluk kaynağı yaratır
ve ikisi ayrıştığında hangisinin doğru olduğu bilinemezdi. Denetim izi tek yerdedir: her
işlemin `signal_reason` kuyruğunda o an oynanan kol ve o andaki posterior ortalama durur
(`| arm=vwap_pullback | post_r=0.31`), yani geçmiş her karar defterden geri okunabilir.

**Kol etiketi bulunamazsa hata fırlatılır, satır atlanmaz.** Etiketsiz bir satırı sessizce
atlamak, o işlemin R'sini posterior'dan düşürüp bandit'i defterde görünmeyen bir geçmişle
öğrenir hâle getirirdi — ve tam da "tekrar üretilebilir" iddiası çökerdi.

**Öğrenme modeli: Normal-Normal.** Ödül (R) sürekli ve sınırsızdır (likidasyonda −1'in
altına iner), bu yüzden Beta/Bernoulli uygun değildir: kazanma oranını öğrenmek, 1R
kazanan kol ile 3R kazanan kolu aynı saymak demekti. Kolun ortalama R'si üzerine normal
posterior kullanılır; çekiliş `Normal(ortalama, σ/√n)`dan yapılır ve en yüksek çekilişi
alan kol oynanır. Gözlemsiz kolun σ'sı `scalp.bandit.prior_r_sigma` önselinden gelir.

Üç koruma, üçü de `config.yaml > scalp.bandit` altında:

- **Isınma (`warmup_trades`, 20):** kolların HEPSİ 20 işleme ulaşana kadar seçim eşit
  ağırlıklı çekiliştir. Erken öğrenme, 2-3 işlemlik gürültüyü kalıcı bir tercih hâline
  getirir ve az örneklemli kolu bir daha hiç denemez.
- **Taban tahsis (`min_allocation`, %5):** ısınmadan sonra da her turda %5×(uygun kol
  sayısı) olasılıkla eşit çekiliş yapılır. Böylece hiçbir kol tahsisi sıfıra düşmez —
  susturulan kol bir daha ÖLÇÜLEMEZ ve "kötüydü" iddiası sınanamaz hâle gelir.
- **Kayan pencere (`window_trades`, 100):** kol başına son 100 işlem. Piyasa rejimi
  değişir; iki yıl önceki bir kolun ortalaması bugünkü tahsisi belirlememelidir.

Rollere dikkat: bu modül boyut/komisyon/bakiye hesaplamaz (kural 1/2/3/7), deftere yazmaz
(kural 1) ve başka modelin verisini görmez (kural 4) — gördüğü tek geçmiş KENDİ kapanmış
işlemleridir. Kol mantığı ve kapılar `strategies/scalp/`dedir.
"""

from __future__ import annotations

import logging
import math
import random
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from core.config import get_setting, load_config
from core.tags import parse_tag
from strategies.base import ClosedTrade, Direction
from strategies.scalp.arms import ARM_NAMES
from strategies.scalp.model import ScalpModel

logger = logging.getLogger(__name__)

NAN = float("nan")


@dataclass(frozen=True, kw_only=True)
class ArmPosterior:
    """Bir kolun kayan penceredeki gözlem özeti. `nan` = ölçülmedi (0.0 değil)."""

    arm: str
    trades: int
    mean_r: float
    stdev_r: float

    @property
    def measured(self) -> bool:
        return self.trades > 0


class ScalpBandit(ScalpModel):
    name = "scalp_bandit"
    allowed_directions: list[Direction] = ["long", "short"]

    def __init__(self, *, config: Mapping[str, Any] | None = None) -> None:
        settings = dict(config) if config is not None else load_config()
        super().__init__(config=settings)
        self._warmup = int(get_setting(settings, "scalp.bandit.warmup_trades"))
        self._min_allocation = float(get_setting(settings, "scalp.bandit.min_allocation"))
        self._window = int(get_setting(settings, "scalp.bandit.window_trades"))
        self._prior_sigma = float(get_setting(settings, "scalp.bandit.prior_r_sigma"))
        if self._window <= 0:
            raise ValueError(f"scalp.bandit.window_trades pozitif olmalı: {self._window}")
        if not 0.0 <= self._min_allocation * len(ARM_NAMES) <= 1.0:
            raise ValueError(
                "scalp.bandit.min_allocation × kol sayısı 1.0'ı aşamaz: "
                f"{self._min_allocation} × {len(ARM_NAMES)}"
            )
        self._posteriors: dict[str, ArmPosterior] = {
            arm: ArmPosterior(arm=arm, trades=0, mean_r=NAN, stdev_r=NAN)
            for arm in ARM_NAMES
        }

    # ------------------------------------------------------------------ #
    # Öğrenme
    # ------------------------------------------------------------------ #
    def observe_closed_trades(self, trades: Sequence[ClosedTrade]) -> None:
        """Kendi KAPANMIŞ işlemlerinden posterior'ı yeniden kurar.

        Durum her turda sıfırdan kurulur, artımlı güncellenmez: artımlı sayaç, defterle
        modelin hafızasının ayrışabileceği bir kapı açardı (bir tur atlanır, bir satır
        elle düzeltilir). Sıfırdan kurmak, posterior'ı defterin saf bir fonksiyonu yapar —
        "tekrar üretilebilir" iddiasının tamamı budur.

        Kol etiketi olmayan satır bir veri durumu değil, etiketi yazması gereken modelin
        hatasıdır: `core.tags.parse_tag` hata fırlatır ve model o turu boş geçer (kural 8).
        """
        by_arm: dict[str, list[float]] = {arm: [] for arm in ARM_NAMES}
        unknown: set[str] = set()
        skipped = 0

        for trade in sorted(trades, key=lambda item: item.closed_at):
            arm = parse_tag(trade.signal_reason, "arm")
            if arm not in by_arm:
                # Kayıtsız kol adı: kol kümesi değişmiş demektir. Sessizce yutmak, eski
                # defterle yeni kol kümesini karıştırıp posterior'ı bozardı.
                unknown.add(arm)
                continue
            if trade.r_multiple is None:
                # R'si olmayan satır (risk_amount yok): metrics de bu satırı R'ye katmaz.
                skipped += 1
                continue
            by_arm[arm].append(float(trade.r_multiple))

        if unknown:
            logger.warning(
                "%s: defterde tanınmayan kol adı: %s — bu satırlar posterior'a girmedi",
                self.name, ", ".join(sorted(unknown)),
            )
        if skipped:
            logger.warning(
                "%s: %d kapanmış işlem risk_amount taşımadığı için posterior'a girmedi",
                self.name, skipped,
            )

        self._posteriors = {
            arm: _summarize(arm, values[-self._window:]) for arm, values in by_arm.items()
        }
        logger.info("%s posterior: %s", self.name, self.state_summary())

    def posteriors(self) -> dict[str, ArmPosterior]:
        """Durumun salt okunur kopyası (testler ve denetim için)."""
        return dict(self._posteriors)

    def state_summary(self) -> str:
        return " ".join(
            f"{arm}(n={item.trades},R={item.mean_r:.3f})"
            if item.measured
            else f"{arm}(n=0,R=nan)"
            for arm, item in self._posteriors.items()
        )

    # ------------------------------------------------------------------ #
    # Seçim
    # ------------------------------------------------------------------ #
    def choose_arm(
        self, available: Sequence[str], *, rng: random.Random
    ) -> tuple[str, float]:
        """Isınma → eşit çekiliş; sonra taban tahsis payı kadar eşit, kalanda Thompson.

        Taban tahsis UYGUN kollar üzerinden uygulanır: o turda kurulum üretmemiş bir kola
        pay ayırmak turu boşa harcamak olurdu, ama kurulum üreten hiçbir kol tabanın
        altına düşmez.
        """
        arms = list(available)
        if not arms:
            raise ValueError(f"{self.name}: seçilecek kol yok")

        warming = [arm for arm in arms if self._posteriors[arm].trades < self._warmup]
        if warming:
            choice = rng.choice(arms)
            logger.info(
                "%s: ısınma sürüyor (%s henüz %d işleme ulaşmadı), eşit ağırlıklı çekiliş -> %s",
                self.name, ", ".join(warming), self._warmup, choice,
            )
            return choice, self._posteriors[choice].mean_r

        floor_budget = self._min_allocation * len(arms)
        if rng.random() < floor_budget:
            choice = rng.choice(arms)
            logger.info(
                "%s: taban tahsis çekilişi (%.0f%%), kol -> %s",
                self.name, floor_budget * 100, choice,
            )
            return choice, self._posteriors[choice].mean_r

        samples = {arm: self._sample(self._posteriors[arm], rng) for arm in arms}
        choice = max(arms, key=lambda arm: samples[arm])
        logger.info(
            "%s: Thompson çekilişi %s -> %s",
            self.name,
            ", ".join(f"{arm}={samples[arm]:.3f}" for arm in arms),
            choice,
        )
        return choice, self._posteriors[choice].mean_r

    def _sample(self, posterior: ArmPosterior, rng: random.Random) -> float:
        """Ortalama R'nin posterior'ından çekiliş: `Normal(ortalama, σ/√n)`.

        σ kolun kendi örneklem sapmasıdır; tek gözlemli kolda sapma tanımsız olduğu için
        önsel (`prior_r_sigma`) devreye girer. Belirsizliği `√n` ile daraltmak Thompson
        sampling'in kendisidir: az ölçülmüş kol geniş çekiliş alır (keşif), çok ölçülmüş
        kol ortalamasına yakın çeker (kullanım).
        """
        if not posterior.measured:
            return rng.gauss(0.0, self._prior_sigma)
        sigma = posterior.stdev_r if posterior.trades > 1 else self._prior_sigma
        if not math.isfinite(sigma) or sigma <= 0.0:
            sigma = self._prior_sigma
        return rng.gauss(posterior.mean_r, sigma / math.sqrt(posterior.trades))


def _summarize(arm: str, values: Sequence[float]) -> ArmPosterior:
    count = len(values)
    if count == 0:
        return ArmPosterior(arm=arm, trades=0, mean_r=NAN, stdev_r=NAN)
    mean = sum(values) / count
    if count < 2:
        return ArmPosterior(arm=arm, trades=count, mean_r=mean, stdev_r=NAN)
    variance = sum((value - mean) ** 2 for value in values) / (count - 1)
    return ArmPosterior(arm=arm, trades=count, mean_r=mean, stdev_r=math.sqrt(variance))
