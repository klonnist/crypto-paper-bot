"""MODEL 17 — `scalp_patient`in İKİZİ, tek farkı VOLATİLİTE REJİMİ kapısı.

**Tez.** Friksiyon notional'ın sabit bir YÜZDESİDİR; sinyalin ürettiği sürüklenme ise
volatiliteyle ÖLÇEKLENİR. O hâlde aynı kurulum, düşük ATR% barlarında yapısal olarak
kaybeder ve yüksek ATR% barlarında kazanabilir.

**Neden tam burada (karar 35).** Ölçülen özdeşlik:

    brüt sürüklenme% = (ort.R + cost_per_r) × avg_stop_distance_pct

`scalp_patient` için: `(−0.01 + 0.124) × %2.29 = %0.261`, tur maliyeti ise
`0.124 × %2.29 = %0.284`. **Ortalama R'nin −0.01 çıkması tesadüf değil, bu iki sayının
neredeyse eşit olmasıdır.** Yani başabaş noktası evrenin MEDYAN volatilitesindedir. Edge
σ ile orantılıysa, medyanın ÜSTÜNDEKİ barlarda net R pozitif olmak ZORUNDADIR.

Bu tahmin veriye bakılarak değil, bir özdeşlikten türetildi — karar 31'in ("100 teoriden
gelir") aynı deseni.

**Kapı KESİTSELDİR ve serbest parametre DEĞİLDİR.** Eşik, o bardaki evrenin ATR% MEDYANIDIR;
sabit bir sayı olsaydı süpürülebilir bir parametre olurdu ve `docs/backtest.md > 7.1`in
yasakladığı şey tam olarak budur. Medyan ayrıca rejimi kendiliğinden takip eder: sakin bir
haftada da, dalgalı bir haftada da evrenin yarısı geçer.

**Neden `stop_atr_multiple`i oynatmak DEĞİL (karar 35).** `cost_per_r = maliyet% / stop%`
özdeşliğinden: stop'u genişletmek maliyet/R'yi düşürür ama R cinsinden brüt edge'i AYNI
oranda düşürür. **Stop genişliği net R'nin İŞARETİNİ değiştiremez.** Ölçek tartışması
birinci mertebede boştur; kaldıraç volatilite rejimindedir.

**Neden `scalp_patient`in ikizi.** Süre ekseni zaten ölçüldü (karar 32: −0.15 → −0.01) ve
hareket eden tek eksen oydu. Rejim kapısını onun ÜSTÜNE koymak, ölçülen şeyi "kapının
sürenin üstüne ne kattığı" yapar. `scalp_fixed`in üstüne konsaydı fark iki değişkenli
(süre + rejim) olurdu ve okunamazdı.

**Çekiliş PAYLAŞILIR** (`rng_identity` mirasla `scalp_fixed`): üçü de her barda aynı kolu ve
aynı sembolü seçer. Defterlerin ayrışması yalnızca kapının ELEDİĞİ kurulumlardan gelir.

**`take_survey` UYGULANIR ve bu bilinçlidir.** `momentum_burst`ün hiç tetiklenmediği iki
backtest sonra öğrenildi çünkü `ScalpModel` sayım tutmuyordu (karar 34). Bu modelde kapının
kaç kurulumu elediği ilk turun yük dosyasında görünür.

Rollere dikkat: boyut/komisyon/bakiye hesaplanmaz (kural 1/2/3/7), deftere yazılmaz (kural 1).
Kol seçimi, kapılar, geometri ve zaman stop'u `ScalpPatient`ten MİRAS ALINIR.
"""

from __future__ import annotations

import logging
from statistics import median
from typing import Mapping, Sequence

from strategies.base import MarketData
from strategies.scalp.arms import ArmSetup, symbol_views
from strategies.scalp_patient import ScalpPatient

logger = logging.getLogger(__name__)


class ScalpVol(ScalpPatient):
    name = "scalp_vol"

    def __init__(self, *, config: Mapping[str, object] | None = None) -> None:
        super().__init__(config=config)  # type: ignore[arg-type]
        self._survey: dict[str, int] = {}

    def regime_filter(
        self, setups: Sequence[ArmSetup], market: MarketData
    ) -> list[ArmSetup]:
        """Sembolün ATR%'i o bardaki evrenin MEDYANININ altındaysa kurulumu ele.

        Medyan, kurulum üretenlerden değil **evrenin tamamından** hesaplanır: yalnızca
        adaylara bakmak, eşiği o barda kaç aday olduğuna bağlı hâle getirirdi — yani
        kesitsel bir rejim ölçüsü olmaktan çıkıp veriye bağlı kayan bir eşiğe dönüşürdü.

        ATR `symbol_views` üzerinden okunur; ikinci bir ATR hesabı, projenin tek ATR
        tanımından (`trailing.atr_period`) sessizce ayrışabilirdi.
        """
        if not setups:
            return []
        views = symbol_views(market, atr_period=self._params.atr_period)
        ratios = {v.symbol: v.atr / v.close for v in views if v.close > 0.0}
        if len(ratios) < 2:
            # Medyan tanımsız/anlamsız: kapı UYGULANMAZ ve bu sessiz olmaz. Elemek,
            # veri boşluğunu bir rejim kararıymış gibi gösterirdi.
            logger.info("%s: evren %d sembol, rejim kapısı uygulanmadı", self.name, len(ratios))
            self._survey["rejim_kapisi_yok"] = self._survey.get("rejim_kapisi_yok", 0) + 1
            return list(setups)

        threshold = median(ratios.values())
        kept: list[ArmSetup] = []
        for setup in setups:
            ratio = ratios.get(setup.symbol)
            if ratio is None or ratio < threshold:
                logger.info(
                    "%s %s/%s: kurulum atlandı, ATR %%%.3f < evren medyanı %%%.3f "
                    "(düşük volatilite rejiminde friksiyon sürüklenmeyi yer)",
                    self.name, setup.arm, setup.symbol,
                    (ratio or 0.0) * 100, threshold * 100,
                )
                self._survey["dusuk_vol"] = self._survey.get("dusuk_vol", 0) + 1
                continue
            self._survey["gecti"] = self._survey.get("gecti", 0) + 1
            kept.append(setup)
        return kept

    def take_survey(self) -> Mapping[str, int] | None:
        """Kapının kaç kurulumu elediği — denetim izi, ölçüme girmez (kural 15)."""
        survey, self._survey = dict(self._survey), {}
        return survey or None
