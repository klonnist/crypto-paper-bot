"""MODEL 14'ÜN VWAP sapma-dönüş sinyali. Bu modülü YALNIZCA model 14 okur.

**Model 13 bu modülü OKUMAZ** (karar 23): kopyanın kuralları kaynak sistemin kurallarıdır
ve `strategies/vwap/clone_signal.py`'dedir — ayrı VWAP çapası (kayan kümülatif pencere),
ayrı σ tanımı (ağırlıksız örneklem sapması), ayrı dönüş şartı, ayrı stop/hedef geometrisi
ve ayrı kol etiketi. Ortak olan yalnızca `core/indicators.py` yardımcılarıdır. Tek modülde
birleştirilselerdi her kural bir dallanma olurdu ve "iki sistemin farkı" o dallanmaların
durumuna bağlı bir şeye dönüşürdü.

**Tez.** Gün-çapalı VWAP, günün o ana kadarki hacim ağırlıklı ortalama işlem maliyetidir.
Fiyat ondan kendi hacim ağırlıklı standart sapmasının `band_mult` katı kadar uzaklaştığında
"bugünkü katılımcıların çoğuna göre pahalı/ucuz" bölgeye girmiştir; oradan VWAP'e doğru
DÖNMEYE BAŞLADIĞINDA işlem VWAP yönünde açılır.

**Dönüş şartı sapmanın kendisinden ayrıdır ve zorunludur.** Yalnızca "banttan uzakta"
olmak bir sinyal değildir: güçlü bir trendde fiyat bant dışında saatlerce kalır ve her
barda aynı sinyali üretirdi. Bu yüzden iki bar karşılaştırılır — önceki bar bandın
DIŞINDA kapanmış, bu bar VWAP'e doğru bir adım atmış ve HÂLÂ aynı tarafta olmalıdır.
"Hâlâ aynı tarafta" şartı, VWAP'i çoktan geçmiş bir barı bir dönüş başlangıcı saymayı
engeller: o artık başka bir kurulumdur (VWAP kırılımı), bu kolun ölçtüğü şey değil.

**Kurulumun YERİ burada, stop ve hedef modelde.** Bu modül sembol, yön, giriş, ATR, VWAP
ve sapmayı verir; model 14 stop'u sabit ATR katı, hedefi ise projeksiyon ile VWAP'in YAKIN
olanı olarak kurar ve %1 stop tabanı ile 1.5R kapısını uygular
(`strategies/vwap_managed.py`).

Hedef sabit bir R katı olsaydı 1.5R kapısı ölü koda dönerdi (bkz.
`strategies/scalp/arms.py`'deki aynı gerekçe): sabit bir R katı kapıyı hiçbir zaman
tetiklemez. VWAP'i yapısal engel saymak kapıyı canlı tutar — VWAP 1.5R'den yakınsa
kurulum atlanır, çünkü tezin hedefi zaten VWAP'tir ve oraya kadar olan mesafe maliyeti
karşılamıyordur.

**z hesabı `as_of` VWAP'ine göredir, her bar için yeniden çapalanmış VWAP'e göre değil.**
Soru "önceki bar ŞU ANKİ ortalama maliyetin ne kadar uzağındaydı"dır; her bar için o barın
kendi VWAP'ini kullanmak iki farklı ölçeği karşılaştırmak olurdu. Ayrıca kural 12 ile de
tutarlıdır: kullanılan tüm barlar `as_of` ve öncesindedir.

**Aday BULUNAMAMASI da kaydedilir (`Survey`).** Kol, sinyal üretmediği barlarda hiçbir iz
bırakmasaydı "bugün kurulum yoktu" ile "sinyal modülü sessizce bozuldu" aynı görünürdü —
hiç işlem açmamış bir modelde defterdeki boşluk tek başına hangisinin doğru olduğunu
söylemez. Sayım iki parçalıdır: eleme SEBEPLERİ (ayrık) ve |z_prev| KOVALARI (kümülatif);
ikincisi olmadan "13 sembol de bandın içindeydi" satırı bandın kıl payı mı yoksa fersah
fersah mı kaçırdığını söylemez. Bu yüzden her tarama eleme SEBEPLERİYLE sayılır ve loglanır; kural
15'in "ret sebep koduyla kaydedilir" şartının bu koldaki karşılığıdır. Sayım yalnızca bir
denetim izidir: hangi adayın üretileceğini ve sıralarını hiçbir biçimde etkilemez.

Rollere dikkat: bu modül boyut/komisyon/bakiye hesaplamaz (kural 1/2/3/7), deftere yazmaz
ve kendi gösterge matematiğini yazmaz — VWAP ve ATR `core/indicators.py`'dedir, sembol
görünümleri `strategies/scalp/arms.py`'nin ortak yardımcısından gelir.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Collection, Mapping, Sequence

from core.indicators import anchored_vwap
from strategies.base import Direction, MarketData
from strategies.scalp.arms import SymbolView, symbol_views

logger = logging.getLogger(__name__)

# Kolun ADI — YALNIZCA model 14'ün etiketi. Model 13 kendi etiketini yazar
# (`vwap_revert_src`): tek bir ad, iki farklı kural kümesini aynı kolmuş gibi gösterir ve
# kol kırılımını (core/metrics.py::arm_of) anlamsız kılardı.
ARM_NAME = "vwap_revert"


@dataclass(frozen=True, kw_only=True)
class VwapCandidate:
    """Bir sembolde görülen sapma-dönüş kurulumunun YERİ. Stop/hedef henüz YOKTUR."""

    symbol: str
    direction: Direction
    entry_price: float  # `as_of` kapanışı — dolum bir sonraki barın açılışındadır (kural 13)
    atr: float
    vwap: float
    deviation: float
    z_prev: float  # önceki barın VWAP'ten uzaklığı (sapma cinsinden)
    z_now: float   # bu barın uzaklığı; |z_now| < |z_prev| olduğu için dönüş "başlamış"tır
    vwap_bars: int

    @property
    def extension(self) -> float:
        """Kurulumun GÜCÜ: sinyalin doğduğu barın bant dışındaki mesafesi."""
        return abs(self.z_prev)

    def detail(self) -> str:
        return (
            f"VWAP sapma-dönüş: gün-çapalı VWAP={self.vwap:.6g} ({self.vwap_bars} bar), "
            f"sapma={self.deviation:.6g}; önceki bar {self.z_prev:+.2f}σ ile bandın "
            f"dışında kapandı, bu bar {self.z_now:+.2f}σ ile VWAP'e döndü "
            f"(yön {self.direction}, ATR={self.atr:.6g})"
        )


# Bir sembolün o barda neden aday OLAMADIĞI. Sayım bir ölçüm değil, bir DENETİM İZİDİR:
# "bu barda hiç sinyal yok" satırı, sinyal modülünün sessizce bozulduğu bir bardan ayırt
# edilemezse kural 15'in "atlama sessiz olamaz" şartı bu kolda karşılanmamış olur. Model
# 13'ün deftere hiç satır yazmadığı bir gün ile kolun hiç kurulum görmediği bir gün
# birbirinin aynısı görünür ve hangisinin doğru olduğu ancak veriyi elle çekerek anlaşılır.
NO_VWAP = "vwap_yok"             # çapa/sapma yokluğu: "kaç σ uzakta" sorusunun cevabı yok
INSIDE_BAND = "bant_ici"         # önceki bar bandın İÇİNDE kapandı (sapma yetersiz)
STILL_EXTENDING = "donus_yok"    # bant dışı ama dönüş başlamamış (uzaklaşma sürüyor)
CROSSED = "vwap_gecildi"         # bant dışı, ama bu bar VWAP'i çoktan geçmiş
SETUP = "kurulum"                # aday

# |z_prev| HİSTOGRAMI (kümülatif): "kaç sembol en az bu kadar uzaktaydı".
#
# Eleme sayımı `bant_ici=13` der ama "0.4σ'da mı, 1.9σ'da mı" demez — oysa iki durum
# bambaşka şeyler söyler: birincisi piyasanın VWAP'e yapışık olduğunu, ikincisi bandın
# kıl payı kaçırdığını. `max_extension` bu bilgiyi yalnızca TEK sembol için ve yalnızca
# loga taşıyor; kovalar aynı bilgiyi tüm evren için ve TUR RAPORUNA taşır (sözleşme
# `Mapping[str, int]` olduğu için kova bir SAYIMDIR, `max_extension` gibi bir ölçü değil).
#
# Kümülatif ("≥") olmaları bilinçlidir: bandın eşiği bir kesme noktasıdır ve sorulan soru
# "eşik şu olsaydı kaç sembol geçerdi"dir. Ayrık kovalar bu soruyu her okumada toplama
# yaptırırdı. Kümülatif oldukları için ÜST ÜSTE BİNERLER ve toplamları `examined` DEĞİLDİR
# — bu yüzden eleme sayımıyla aynı sözlükte tutulmazlar (bkz. `Survey.counts`/`report`).
EXTENSION_BUCKETS: tuple[float, ...] = (1.0, 1.5, 2.0, 2.5)


@dataclass(frozen=True, kw_only=True)
class Survey:
    """Bir BARDA kolun ne gördüğü: sebep -> sembol sayısı, ve VWAP'ten en uzak sembol.

    `max_extension` bandın kendisiyle kıyaslanmak içindir: 0 aday üreten bir barda
    "en uzak sembol 1.9σ'daydı" ile "en uzak sembol 0.4σ'daydı" bambaşka iki durumdur —
    birincisi bandın dar kaldığını, ikincisi piyasanın gerçekten VWAP'e yapışık olduğunu
    söyler. `nan` ise hiçbir sembolde z hesaplanamadı demektir (bkz. NO_VWAP).
    """

    examined: int
    counts: Mapping[str, int]          # eleme sebebi -> sembol; Σ = examined (AYRIK)
    extensions: Mapping[str, int]      # z_ge_* -> sembol; kümülatif, ÜST ÜSTE BİNER
    furthest_symbol: str | None
    max_extension: float   # görülen en büyük |z_prev|; nan = hiç ölçülemedi

    @property
    def candidates(self) -> int:
        return self.counts.get(SETUP, 0)

    def report(self) -> dict[str, int]:
        """Tur raporuna düşen sayım: eleme sebepleri + |z_prev| kovaları.

        İkisi tek sözlükte BİRLEŞTİRİLİR ama ayrı alanlarda TUTULUR: `counts` ayrıktır ve
        `Σ counts == examined` değişmezi denetlenebilir olmalıdır, kovalar ise kümülatiftir
        ve üst üste biner. Aynı alanda saklamak o değişmezi sessizce yok ederdi; ayrı
        raporlamak ise okuyanı iki sözlüğü elle birleştirmeye zorlardı.
        """
        return {**self.counts, **self.extensions}

    def describe(self) -> str:
        reasons = " ".join(
            f"{reason}={self.counts[reason]}"
            for reason in (SETUP, INSIDE_BAND, STILL_EXTENDING, CROSSED, NO_VWAP)
            if self.counts.get(reason)
        )
        buckets = " ".join(
            f"{key}={self.extensions[key]}"
            for key in _bucket_keys()
            if self.extensions.get(key)
        )
        furthest = (
            "en uzak sapma: yok"
            if self.furthest_symbol is None or math.isnan(self.max_extension)
            else f"en uzak sapma: {self.furthest_symbol} {self.max_extension:.2f}σ"
        )
        return (
            f"{self.examined} sembol; {reasons or 'sayım yok'}; "
            f"|z| dağılımı: {buckets or 'yok'}; {furthest}"
        )


def bucket_key(threshold: float) -> str:
    """`1.5` -> `z_ge_1_5`. Ondalık basamak SABİTTİR (`:g` değil `:.1f`).

    `:g` biçimlendirmesi 1.0'ı `z_ge_1`, 1.5'i `z_ge_1_5` yapardı: aynı eksende iki farklı
    ad şeması, tur raporunu okuyan tarafta sessizce kaçırılan bir anahtar demektir.
    Nokta alt çizgiye çevrilir çünkü etiket/anahtar biçimi noktayı kabul etmez
    (core/tags.py).
    """
    return f"z_ge_{threshold:.1f}".replace(".", "_")


def _bucket_keys() -> tuple[str, ...]:
    return tuple(bucket_key(threshold) for threshold in EXTENSION_BUCKETS)


def propose(
    market: MarketData,
    *,
    atr_period: int,
    band_mult: float,
    min_vwap_bars: int,
    symbols: Collection[str] | None = None,
    model: str | None = None,
) -> list[VwapCandidate]:
    """O BARIN sapma-dönüş adayları; GÜÇ SIRASINA göre (eşitlikte sembol adına göre).

    Sıra sözlük sırasına bırakılmaz: iki model de aynı adaylardan tek bir kurulum seçer
    ve seçimin tekrarlanabilir olması sıranın belirli olmasına bağlıdır
    (`strategies/scalp/arms.py::symbol_views` ile aynı gerekçe).

    `symbols` modelin KENDİ evrenidir (None = katmanın tamamı). Kopya model kaynak
    sistemin evrenini taşır; katmanın evreni ondan geniştir ve fazladan bir sembolde
    işlem açmak kopyayı kopya olmaktan çıkarırdı.

    `model` yalnızca LOG ETİKETİDİR ve seçimi hiçbir biçimde etkilemez: aynı bar iki model
    için ayrı ayrı taranır (evrenleri farklı) ve iki sayım satırı birbirinden ayırt
    edilebilmelidir. Sonuç, `scan`in döndürdüğü listenin aynısıdır.
    """
    candidates, survey = scan(
        market,
        atr_period=atr_period,
        band_mult=band_mult,
        min_vwap_bars=min_vwap_bars,
        symbols=symbols,
    )
    logger.info(
        "%s%s %s bandı=%.2fσ -> %s",
        f"{model} " if model else "",
        ARM_NAME,
        market.as_of.isoformat(),
        band_mult,
        survey.describe(),
    )
    return candidates


def scan(
    market: MarketData,
    *,
    atr_period: int,
    band_mult: float,
    min_vwap_bars: int,
    symbols: Collection[str] | None = None,
) -> tuple[list[VwapCandidate], Survey]:
    """`propose`un sessiz hâli: adaylar VE eleme sayımı.

    Ayrı bir fonksiyon olmasının nedeni test edilebilirliktir: sayımın kendisi log
    metninden değil bir değerden okunabilmelidir, yoksa "denetim izi doğru mu" sorusu
    ancak log ayrıştırarak cevaplanabilirdi.
    """
    allowed = None if symbols is None else set(symbols)
    candidates: list[VwapCandidate] = []
    counts: dict[str, int] = {
        SETUP: 0, INSIDE_BAND: 0, STILL_EXTENDING: 0, CROSSED: 0, NO_VWAP: 0
    }
    extensions: dict[str, int] = {key: 0 for key in _bucket_keys()}
    examined = 0
    furthest_symbol: str | None = None
    max_extension = float("nan")

    for view in symbol_views(market, atr_period=atr_period):
        if allowed is not None and view.symbol not in allowed:
            continue
        examined += 1
        candidate, reason, extension = _evaluate(
            view, band_mult=band_mult, min_vwap_bars=min_vwap_bars
        )
        counts[reason] += 1
        if candidate is not None:
            candidates.append(candidate)
        if extension is not None:
            for threshold in EXTENSION_BUCKETS:
                if extension >= threshold:
                    extensions[bucket_key(threshold)] += 1
            if math.isnan(max_extension) or extension > max_extension:
                furthest_symbol, max_extension = view.symbol, extension

    candidates.sort(key=lambda item: (-item.extension, item.symbol))
    survey = Survey(
        examined=examined,
        counts=counts,
        extensions=extensions,
        furthest_symbol=furthest_symbol,
        max_extension=max_extension,
    )
    return candidates, survey


def _evaluate(
    view: SymbolView, *, band_mult: float, min_vwap_bars: int
) -> tuple[VwapCandidate | None, str, float | None]:
    """Tek sembolün değerlendirmesi: aday, ELEME SEBEBİ ve ölçülen |z_prev|.

    Sebep ile birlikte döner, çünkü `None` tek başına "neden olmadığını" söylemez ve tam
    da ayırt edilmek istenen şey odur (bkz. sebep sabitlerinin üstündeki gerekçe).
    """
    if len(view.frame) < 2:
        return None, NO_VWAP, None
    vwap = anchored_vwap(view.frame, anchor=view.as_of.normalize())
    if vwap is None or vwap.bars < min_vwap_bars or vwap.deviation <= 0.0:
        # Sapması sıfır olan (ya da gün başı henüz birkaç barlık) bir pencerede "kaç σ
        # uzakta" sorusunun cevabı yoktur; sıfıra bölmek yerine kurulum kurulmaz.
        return None, NO_VWAP, None

    previous = float(view.frame["close"].iloc[-2])
    z_prev = (previous - vwap.value) / vwap.deviation
    z_now = (view.close - vwap.value) / vwap.deviation
    extension = abs(z_prev)

    if -band_mult < z_prev < band_mult:
        return None, INSIDE_BAND, extension

    direction: Direction | None = None
    if z_prev <= -band_mult and z_prev < z_now < 0.0:
        direction = "long"
    elif z_prev >= band_mult and 0.0 < z_now < z_prev:
        direction = "short"
    if direction is None:
        # Bant dışında kalan bar ya hâlâ uzaklaşıyordur ya da VWAP'i geçmiştir; ikisi
        # farklı şeyler söyler (birincisi trend, ikincisi kaçırılmış dönüş) ve tek bir
        # "aday değil" sayısına indirgemek kolun neyi kaçırdığını gizlerdi.
        crossed = z_now >= 0.0 if z_prev < 0.0 else z_now <= 0.0
        return None, CROSSED if crossed else STILL_EXTENDING, extension

    return (
        VwapCandidate(
            symbol=view.symbol,
            direction=direction,
            entry_price=view.close,
            atr=view.atr,
            vwap=vwap.value,
            deviation=vwap.deviation,
            z_prev=z_prev,
            z_now=z_now,
            vwap_bars=vwap.bars,
        ),
        SETUP,
        extension,
    )


def stop_price(candidate: VwapCandidate, *, atr_multiple: float) -> float:
    """Stop: girişin ATR'nin `atr_multiple` katı kadar ALEYHTE tarafında.

    Mesafe her iki modelde de aynı formülle kurulur; farklı olan yalnızca çarpanın
    NEREDEN geldiğidir (model 13 öğrenir, model 14 config'ten okur). Formülü tek yerde
    tutmak, iki modelin stop mesafesini — yani maliyet ölçeğini, kural 14 — aynı tanıma
    bağlar.
    """
    sign = 1.0 if candidate.direction == "long" else -1.0
    return candidate.entry_price - sign * candidate.atr * atr_multiple


def projected_target(candidate: VwapCandidate, *, stop: float, reward_risk: float) -> float:
    """Hedef PROJEKSİYONU: stop mesafesinin `reward_risk` katı, girişin lehte tarafında."""
    sign = 1.0 if candidate.direction == "long" else -1.0
    return candidate.entry_price + sign * abs(candidate.entry_price - stop) * reward_risk


def nearest_target(candidate: VwapCandidate, *, projected: float) -> float:
    """Projeksiyon ile yapısal engelin (VWAP) YAKIN olanı.

    VWAP girişin gerisinde kaldıysa (fiyat dönüşü çoktan tamamlamış) yolda engel yok
    demektir ve hedef projeksiyondur. Önünde duruyorsa hedef odur: bilinen bir seviyenin
    ötesini hedeflemek, o seviyenin orada olmadığını varsaymak olurdu.
    """
    sign = 1.0 if candidate.direction == "long" else -1.0
    ahead = (candidate.vwap - candidate.entry_price) * sign > 0.0
    if not ahead:
        return projected
    return min(projected, candidate.vwap) if candidate.direction == "long" else max(
        projected, candidate.vwap
    )


def reward_risk_of(candidate: VwapCandidate, *, stop: float, target: float) -> float:
    distance = abs(candidate.entry_price - stop)
    if distance <= 0.0:
        raise ValueError(f"{candidate.symbol}: stop giriş fiyatına eşit, R tanımsız")
    return abs(target - candidate.entry_price) / distance


def stop_distance_pct(candidate: VwapCandidate, *, stop: float) -> float:
    return abs(candidate.entry_price - stop) / candidate.entry_price


def strongest(candidates: Sequence[VwapCandidate]) -> VwapCandidate | None:
    """Turda oynanacak tek aday: en uzağa sapmış olan (liste zaten o sırada)."""
    return candidates[0] if candidates else None
