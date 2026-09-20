"""MODEL 13'ün KENDİ sinyali: kaynak sistemin kuralları, olduğu gibi.

Kaynak `vwap_detector.py::detect_vwap_signal` + `main.py::_find_vwap_setup`
(klonnist/Hasanwavebot). Bu modül o iki fonksiyonun kurallarını yeniden üretir; ev
kuralları burada YOKTUR.

**Neden `strategies/vwap/signal.py`'den AYRI bir dosya.** İki modül aynı fikri (VWAP'ten
sapıp dönen fiyat) anlatır ama BAŞKA kurallarla ölçer ve ayrışma noktaları tek tek
ölçümün konusudur:

    signal.py  (model 14)          clone_signal.py  (model 13)
    gün-çapalı VWAP                son `vwap_window` barın KÜMÜLATİF VWAP'i
    hacim ağırlıklı σ (ddof=0)     sapma serisinin rolling σ'sı (ddof=1, ağırlıksız)
    ÖNCEKİ bar bant dışında        MEVCUT bar bant dışında
    sapma daralmış olmalı          yalnızca kapanış dönüşü (close vs prev_close)
    VWAP geçilmişse ayrı eleme     geometri kontrolünden düşer
    gün çapasından ≥ min_vwap_bars gün sınırı yok, ≥ `min_bars` bar
    güce göre sıralı adaylar       sıralama yok: sembol listesi sırası

Tek dosyada dallanma olarak yazılsalardı model 13 ↔ 14 ekseninin ("ev kurallarının
katkısı") ölçüsü, o dallanmanın hangi dalının hangi model için açık olduğuna bağlı bir
şeye dönüşürdü — ve bir gün biri diğerinin dalını sessizce değiştirirdi. Ortak olan
yalnızca YARDIMCILARDIR (`core/indicators.py::typical_price`, `bars_until`): kural
mantığı paylaşılmaz, aritmetik paylaşılır.

**Bu modül kaynağın sınırlarını da kopyalar.** σ hacim ağırlıksızken VWAP hacim
ağırlıklıdır (kaynakta böyle), `z` typical price'tan okunurken dönüş kapanıştan okunur
(kaynakta böyle), `std_window` parametre değil sabittir (kaynakta böyle). Bunları
"düzeltmek" kopyayı kopya olmaktan çıkarırdı: model 13 iyi bir sinyal değil, DIŞ BİR
SİSTEM ölçmektedir.

Kopyalanmayan üç şey vardır ve üçü de kural 12/13'ün sonucudur (bkz. docs/decisions.md >
"Sadık kopyanın sınırları"): kaynak kapanmamış barı kullanır, girişi o barın anlık
fiyatından yapar ve her sembolü kendi fetch anında değerler. Burada tüm barlar
kapanmıştır, giriş `as_of` kapanışıdır ve dolum bir sonraki barın açılışındadır.

Rollere dikkat: bu modül boyut/komisyon/bakiye hesaplamaz (kural 1/2/3/7), deftere yazmaz
ve rastgelelik kullanmaz. Parametreleri (`CloneParams`) çağıran verir; kaynakta da öyledir
— `learner.select(symbol)` her sembol için `detect_vwap_signal`ten ÖNCE çağrılır.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Mapping

import pandas as pd

from core.indicators import bars_until, typical_price
from strategies.base import Direction

# Kolun ADI. Model 14'ün `vwap_revert`inden AYRIDIR ve bilinçli olarak ayrıdır: iki model
# artık aynı kurulumu aramıyor (yukarıdaki tabloya bkz.). Aynı etiketi yazsalardı kol
# kırılımı (core/metrics.py::breakdown) iki farklı kural kümesini tek bir ad altında
# gösterir ve okuyucu iki satırı "aynı kolun iki modeldeki hâli" sanırdı.
ARM_NAME = "vwap_revert_src"

# Bir sembolün o barda neden aday OLAMADIĞI. Sayım bir ölçüm değil, bir DENETİM İZİDİR
# (`strategies/vwap/signal.py::Survey` ile aynı gerekçe): model 13'ün hiçbir ev kapısı
# yoktur, dolayısıyla `signals=0` olan bir tur "hiç kurulum yoktu" ile "sinyal modülü
# sessizce bozuldu"yu aynı görünüme çökertir. Sebepler kaynağın ELEME NOKTALARIDIR, bizim
# seçtiğimiz kategoriler değil.
NO_BAR = "bar_yok"                  # sembol o barı taşımıyor (o tur evrende değil)
NO_BANDS = "band_yok"               # < min_bars, ya da σ NaN/≤0: "kaç σ uzakta" sorusu yok
INSIDE_BAND = "bant_ici"            # |z| < band_mult
NO_TURN = "donus_yok"               # bant dışı ama kapanış dönüşü yok
BAD_GEOMETRY = "gecersiz_geometri"  # sl < entry < tp sağlanmadı (çoğunlukla VWAP geçilmiş)
SETUP = "kurulum"                   # aday

REASONS: tuple[str, ...] = (SETUP, INSIDE_BAND, NO_TURN, BAD_GEOMETRY, NO_BANDS, NO_BAR)


@dataclass(frozen=True, kw_only=True)
class CloneParams:
    """Kaynağın `VwapParams`ı: bandit'in bir kolunun üç sayısı.

    Üçü birlikte gelir çünkü kaynakta da birlikte gelir — ve `band_mult` HEM giriş eşiği
    HEM stop mesafesidir (`sl = entry ∓ band_mult × sl_mult × σ`). İkisini ayırmak
    kaynağın öğrendiği ödül yüzeyini değiştirirdi: orada "dar bant" ile "dar stop" aynı
    kolun iki yüzüdür.
    """

    band_mult: float
    tp_mult: float
    sl_mult: float


@dataclass(frozen=True, kw_only=True)
class CloneCandidate:
    """Kaynağın `detect_vwap_signal` çıktısı: kurulumun YERİ ve SEVİYELERİ.

    `signal.py::VwapCandidate`ten farkı, stop ve hedefin BURADA kurulmuş olmasıdır: kaynak
    sistemde seviyeler sinyalin parçasıdır ve geometri kontrolü (`sl < entry < tp`) onlara
    bakarak yapılır, yani seviyeler olmadan "bu bir kurulum mu" sorusu cevaplanamaz.
    """

    symbol: str
    direction: Direction
    entry_price: float  # `as_of` kapanışı — dolum bir sonraki barın açılışındadır (kural 13)
    stop_price: float
    target_price: float
    vwap: float
    std: float
    z: float
    bars: int  # VWAP penceresinin gerçek uzunluğu (≤ vwap_window)

    def detail(self) -> str:
        return (
            f"VWAP sapma-dönüş (kaynak kuralları): kümülatif VWAP={self.vwap:.6g} "
            f"({self.bars} bar), σ={self.std:.6g}; bu bar {self.z:+.2f}σ ile bandın "
            f"dışında kapandı ve kapanış {self.direction} yönünde döndü"
        )


@dataclass(frozen=True, kw_only=True)
class Survey:
    """Bir BARDA kolun ne gördüğü: sebep -> sembol sayısı.

    Her incelenen sembol TAM OLARAK bir sebep alır, yani `examined == Σcounts`. Ayrı bir
    `examined` alanı tutulmaz: iki sayı bir gün ayrışırsa hangisinin doğru olduğu
    bilinemezdi.
    """

    counts: Mapping[str, int]

    @property
    def examined(self) -> int:
        return sum(self.counts.values())

    @property
    def candidates(self) -> int:
        return self.counts.get(SETUP, 0)

    def describe(self) -> str:
        reasons = " ".join(
            f"{reason}={self.counts[reason]}"
            for reason in REASONS
            if self.counts.get(reason)
        )
        return f"{self.examined} sembol; {reasons or 'sayım yok'}"


def detect(
    frame: pd.DataFrame | None,
    *,
    symbol: str,
    as_of: pd.Timestamp,
    params: CloneParams,
    vwap_window: int,
    std_window: int,
    min_bars: int,
) -> tuple[CloneCandidate | None, str]:
    """Kaynağın `detect_vwap_signal`i: aday VE eleme sebebi.

    Sebep ile birlikte döner çünkü `None` tek başına "neden olmadığını" söylemez ve tam da
    ayırt edilmek istenen şey odur (bkz. sebep sabitlerinin üstündeki gerekçe).

    Kaynakta pencere `fetch_ohlcv(..., limit=300)` ile gelir; burada aynı pencere
    `as_of`'a kadar kesilmiş çerçevenin son `vwap_window` barıdır (kural 12). Çapa
    sabit değildir: her bar pencereyi bir ileri kaydırır, yani aynı tarihsel barın VWAP'i
    bir sonraki turda farklı çıkar. Bu bir kusur değil, kaynağın kuralıdır — ve gün-çapalı
    VWAP'ten (model 14) ayrıldığı yer tam olarak burasıdır.
    """
    if frame is None or frame.empty:
        return None, NO_BAR
    window = bars_until(frame, as_of)
    if window.empty or window.index[-1] != as_of:
        # Sembol `as_of` barını taşımıyor: o tur evrenin dışındadır (core/data.py aynı
        # kuralı uygular). Kaynakta bu durumun karşılığı "fetch boş döndü"dür.
        return None, NO_BAR
    window = window.tail(int(vwap_window))
    if len(window) < int(min_bars):
        return None, NO_BANDS

    prices = typical_price(window)
    volumes = window["volume"].astype("float64")
    cumulative_volume = volumes.cumsum()
    # Kümülatif (expanding) VWAP: pencerenin İLK barından itibaren. Hacim ağırlıklıdır.
    vwap = (prices * volumes).cumsum() / cumulative_volume.where(cumulative_volume > 0.0)
    distance = prices - vwap
    # σ HACİM AĞIRLIKSIZ ve ddof=1 (pandas varsayılanı) — kaynakta `dist.rolling(20).std()`.
    # VWAP ağırlıklı, σ ağırlıksız: tutarsız görünür ve kaynakta gerçekten böyledir.
    deviation = distance.rolling(int(std_window)).std()

    last_std = float(deviation.iloc[-1])
    last_vwap = float(vwap.iloc[-1])
    if not math.isfinite(last_std) or last_std <= 0.0 or not math.isfinite(last_vwap):
        return None, NO_BANDS

    entry = float(window["close"].iloc[-1])
    previous = float(window["close"].iloc[-2])
    z = float(distance.iloc[-1]) / last_std

    # Bant dışı olma şartı MEVCUT bara bakar (kaynakta `z` son barın değeridir).
    if z <= -params.band_mult:
        direction: Direction = "long"
    elif z >= params.band_mult:
        direction = "short"
    else:
        return None, INSIDE_BAND

    # Dönüş şartının TAMAMI budur: kapanış bir önceki kapanışa göre VWAP yönünde.
    # "Sapma daraldı" (|z_now| < |z_prev|) şartı kaynakta YOKTUR ve eklenmez — eklemek
    # model 14'ün kuralını kopyaya taşımak olurdu.
    turned = entry > previous if direction == "long" else entry < previous
    if not turned:
        return None, NO_TURN

    sign = 1.0 if direction == "long" else -1.0
    band = last_std * params.band_mult
    stop = entry - sign * band * params.sl_mult
    # Hedef VWAP'e olan mesafenin KESRİDİR (tp_mult ≤ 1), yani VWAP'i asla aşmaz. VWAP
    # girişin gerisinde kaldıysa mesafe 0'a kırpılır ve hedef girişe eşit olur — geometri
    # kontrolü o kurulumu düşürür. Kaynakta "VWAP geçildi" için ayrı bir eleme yoktur;
    # eleme buradan gelir.
    gap = max(sign * (last_vwap - entry), 0.0) * params.tp_mult
    target = entry + sign * gap

    if not _ordered(direction, stop=stop, entry=entry, target=target):
        return None, BAD_GEOMETRY

    return (
        CloneCandidate(
            symbol=symbol,
            direction=direction,
            entry_price=entry,
            stop_price=stop,
            target_price=target,
            vwap=last_vwap,
            std=last_std,
            z=z,
            bars=len(window),
        ),
        SETUP,
    )


def empty_counts() -> dict[str, int]:
    """Sıfırlanmış sayaç. Sebep kümesi tek yerde durur ki sayım eksik başlamasın."""
    return {reason: 0 for reason in REASONS}


def _ordered(direction: Direction, *, stop: float, entry: float, target: float) -> bool:
    """Kaynağın geometri kontrolü: long'da `sl < entry < tp`, short'ta `tp < entry < sl`.

    Eşitlik GEÇMEZ (kaynakta da geçmez): hedefi girişe eşit bir kurulum, fiyat hiç
    hareket etmeden "hedefe ulaştı" diye kapanır ve deftere kârmış gibi yazılırdı.
    """
    if not all(math.isfinite(value) for value in (stop, entry, target)):
        return False
    if direction == "long":
        return stop < entry < target
    return target < entry < stop
