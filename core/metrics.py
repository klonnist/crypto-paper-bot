"""Performans metrikleri. Salt okunur: defteri okur, asla değiştirmez.

**Birinci sınıf metrik: işlem başına ortalama R.**

R = işlemin net PnL'i / o işlemde AÇILIŞTA riske edilen tutar. Payda `trades.csv`'nin
`risk_amount` kolonudur (`adet × |giriş − ilk stop|`), dolayısıyla R bileşiklenmeden
bağımsızdır: hesap büyüdükçe boyut da büyüdüğü için toplam getiri kısmen "model ne kadar
hızlı bileşiklendi"yi ölçer, sinyal kalitesini değil. Aynı sinyal kalitesine sahip iki
modelden açık kârını erken büyüteni toplam getiride öne geçer — oysa ölçmek istediğimiz
şey bu değil. Ortalama R bu etkiyi dışarıda bırakır ve "bu model iyi mi" sorusuna daha
temiz cevap verir. Toplam getiri (USDT/%) ikinci sırada raporlanır, atılmaz.

**Long/short ayrıştırması opsiyonel değildir** (CLAUDE.md): projenin ana sorusu short
işlemlerin görece başarısı olduğu için her işlem-tabanlı metrik long, short ve toplam için
ayrı hesaplanır. Özsermaye eğrisinden gelen metrikler (hesap getirisi, hesap max drawdown,
hesap Sharpe) yön bazında ayrıştırılamaz — tek bir bakiye vardır — bu yüzden onlar
`AccountStats` altında açıkça "hesap düzeyi" olarak raporlanır. Yönlerin kendi risk profili
R serisinden ölçülür: `r_sharpe` ve `max_drawdown_r`, o yönün kümülatif R eğrisi üzerinden.

**Maliyet ölçeği kolonları zorunludur** (CLAUDE.md > Rapor Kolonları): `avg_stop_distance_pct`
ve `cost_per_r`. Bunlar projenin ana sorusunu doğrudan kirleten etkiyi ölçer — "short modeller
daha iyi" sonucu sinyalden mi geliyor, yoksa short modellerin daha geniş stop kullanıp R başına
daha az maliyet ödemesinden mi? Boyut `risk / |giriş − stop|` olduğu için dar stop kuran model
aynı 1R'yi daha büyük notional ile taşır ve R başına daha çok komisyon+kayma öder. İki modelin
`avg_stop_distance_pct` değerleri banda göre ayrışıyor ve `cost_per_r` farkı performans farkını
tek başına açıklayabiliyorsa, kıyas geçersiz sayılır.

**Referans (benchmark) modeller yarışmacı değildir** (CLAUDE.md kural 15). Stop'u olmayan
bir alım-tut çıpasının 1R'si yoktur; `avg_stop_distance_pct` ve `cost_per_r` onun için
tanımsızdır ve `nan` raporlanır. Tabloda ortalama R sıralamasına da girmez, ayrı bir
"REFERANS" bölümünde durur: aynı sütunda sıralamak, farklı boyutlandırma kuralıyla
(fraction × sermaye, 1x) çalışan bir satırı risk-birimi yarışının parçasıymış gibi
gösterirdi. Referansın işi sıralamada yer almak değil, yarışmacıların hesap düzeyi
getirisine bir zemin vermek: "model piyasayı yendi mi?"

**Kopya (replica) modeller de yarışmacı değildir.** `is_replica=True` bir model bir DIŞ
SİSTEMİN kurallarını yeniden üretir: boyutlandırması sabit teminattır (%1 risk değil) ve
kaldıracı kendi sistemininkidir. Stop'u vardır — dolayısıyla defterde `risk_amount` da
vardır — ama o R, yarışmacıların R'siyle AYNI BİRİM DEĞİLDİR: 1R burada "sermayenin %1'i"
değil, "sabit teminatın stop mesafesi kadarı"dır. Bu yüzden kopya da ortalama R
sıralamasına girmez, ayrı bir "REFERANS (dış sistem)" bölümünde durur ve maliyet ölçeği
kolonlarında (`avg_stop_distance_pct`, `cost_per_r`) `nan` alır: "R başına maliyet" ancak
ortak risk birimiyle koşan satırlar arasında bir kıyas ölçüsüdür. Kopyanın taşıdığı bilgi
o kolonlarda değil, hesap düzeyi getirisinde ve kendi işlem geçmişindedir.

Çıpa ile kopya aynı kefeye konmaz: ikisi de yarışma dışıdır ama ölçtükleri soru farklıdır
("piyasa ne yaptı" ve "dış sistem bizim maliyet/likidasyon varsayımlarımızla ne yapardı"),
bu yüzden tabloda ayrı bölümlerde dururlar. Kabul çıtasına (iki kapı) ikisi de girmez.

Tanımsız bir metrik (işlem yok, varyans sıfır) `nan` döner; 0.0 döndürmek "ölçüldü ve
sıfır çıktı" ile "ölçülemedi"yi aynı sayıya indirger ve karşılaştırmayı sessizce bozar.
Hiç short açmamış bir modelin `cost_per_r`'si 0.0 olsaydı, "maliyetsiz short yapan model"
gibi görünür ve model ortalamalarını aşağı çekerdi.
"""

from __future__ import annotations

import logging
import math
import random
from bisect import bisect_right
from dataclasses import dataclass
from typing import Any, Callable, Collection, Iterable, Mapping, Sequence

import pandas as pd

from core.config import get_setting
from core.data import bar_duration
from core.ledger import Ledger
from core.tags import find_tag, parse_tag
from strategies.base import Direction

logger = logging.getLogger(__name__)

DIRECTIONS: tuple[Direction, ...] = ("long", "short")
TOTAL = "total"

_NAN = float("nan")
_DAYS_PER_YEAR = 365.0


@dataclass(frozen=True, kw_only=True)
class DirectionStats:
    """Tek bir yönün (ya da toplamın) işlem-tabanlı metrikleri. Başta R gelir."""

    direction: str
    trades: int  # POZİSYON sayısı (dolum değil); bkz. merge_fills
    avg_r: float
    median_r: float
    total_r: float
    r_sharpe: float
    max_drawdown_r: float
    win_rate: float
    avg_win_r: float
    avg_loss_r: float
    profit_factor: float
    avg_stop_distance_pct: float
    cost_per_r: float
    # YÜZDE biriminde ödeme profili. R biriminde karşılıkları (`avg_win_r`, `avg_loss_r`)
    # zaten var ve birincildir; bunlar onların YERİNE değil YANINA gelir, çünkü dışarıdan
    # gelen bir referansla (TradingView, bir başka backtest aracı) kıyas ancak yüzde
    # biriminde kurulabilir — R, boyutlandırma kuralımıza bağlıdır ve o kural dışarıda
    # başkadır. Payda pozisyonun GİRİŞ notional'ıdır (`merge_fills` onu da toplar), yani
    # ölçü kaldıraçtan bağımsızdır.
    #
    # `payoff` ikisinin oranıdır ve AYRI bir alan olmasının sebebi `nan` cebiridir: kayıp
    # işlemi olmayan bir satırda oran tanımsızdır ve okuyucunun bölme yapması, o satırda
    # sıfıra bölmesi demekti.
    avg_win_pct: float
    avg_loss_pct: float
    payoff: float
    # Kümülatif PnL eğrisinin (para birimi) en büyük tepe-dip düşüşü. `max_drawdown_r`ın
    # yanında durur ve ondan farkı PAYDADIR: R eğrisi risk birimini, bu eğri parayı
    # ölçer. Kırılım satırlarında (sembol, kol) hesap düzeyi `max_drawdown` yoktur —
    # hesap tektir ve sembole bölünemez (ortak nakit, ortak margin) — ama "bu sembol
    # hesabı ne kadar geriye çekti" sorusunun cevabı buradadır. Yüzdeye çevirmek için
    # `pnl_drawdown_pct` kullanılır; oran, paydayı (başlangıç sermayesi) bilen tek yerde
    # kurulur.
    max_drawdown_pnl: float
    # Ortalama R'nin yüzdelik bootstrap aralığı. `cost_per_r`'nin yanında durur çünkü
    # ikisi de aynı soruya bakar: "bu satır okunabilir mi". `nan` = hesaplanmadı
    # (bootstrap kapalı ya da örneklem boş) — 0.0 DEĞİL, çünkü sıfır bir aralık sınırı
    # olabilir ve "hesaplanmadı" ile "sıfırı içeriyor" aynı hücreye yazılamaz.
    avg_r_ci_low: float
    avg_r_ci_high: float
    # Friksiyon ölçeği. `notional` işlemlerin GİRİŞ notional'larının toplamıdır (dolum
    # değil pozisyon birimiyle; merge_fills nakit kolonlarını toplar), `cost_pct` ise
    # o notional'a düşen komisyon+kayma yüzdesi — yani karar 35'in "tur maliyeti%"
    # değişkeninin defterden okunan hâli: net R = (brüt sürüklenme% − maliyet%) / stop%.
    #
    # `cost_per_r`den farkı PAYDADIR ve fark önemlidir: `cost_per_r` maliyeti 1R'ye,
    # bu ise notional'a böler. Birincisi kopya ve çıpa satırlarında `nan`dır (1R'leri
    # başka bir birimden gelir); ikincisi HER satırda kıyaslanabilir, çünkü notional
    # tek ve ortak bir birimdir. Kopyanın friksiyonunu görebilmenin tek yolu budur.
    notional: float
    cost_pct: float
    # PİYASA KONTROLÜ (kural: bir ÖLÇÜ, bir düzeltme DEĞİL). Projenin ana sorusu
    # "short'lar long'lardan daha mı başarılı" ve bu soru, ölçüldüğü pencerede
    # piyasanın hangi yöne gittiğiyle TANIM GEREĞİ karışır: düşen bir pencerede her
    # short daha iyi görünür. `market_tailwind_pct` pozisyonun tutuş penceresinde
    # çıpanın (BTC) ne yaptığını POZİSYONUN YÖNÜNE çevirerek söyler (long: +hareket,
    # short: −hareket); `market_r` onu aynı pozisyonun stop ölçeğine bölerek R
    # birimine taşır ve `avg_r` ile YAN YANA okunur.
    #
    # `avg_r`den ÇIKARILMAZ: çıkarmak beta=1 varsayımını birincil metriğin içine
    # gömerdi (§7.4'ün yasakladığı metrik değiştirme). Varsayım açıkta durur ve
    # okuyucu farkı kendisi kurar.
    market_tailwind_pct: float
    market_r: float
    market_measured: int  # çıpa penceresinde fiyatlanabilen POZİSYON sayısı
    pnl: float
    fees: float
    slippage_cost: float
    funding: float
    liquidations: int
    unmeasured: int  # risk_amount'ı olmayan, R'ye giremeyen POZİSYON sayısı


@dataclass(frozen=True, kw_only=True)
class AccountStats:
    """Özsermaye eğrisinden gelen, yön bazında ayrıştırılamayan hesap düzeyi metrikler."""

    initial_capital: float
    final_equity: float
    total_return: float
    max_drawdown: float
    sharpe: float
    bars: int
    # Defterin kapsadığı takvim günü (ilk ve son özsermaye damgası arası). Bar SAYISI
    # değil: sıkıştırılmış eski satırlar (retention) bar başına bir satır taşımaz, ama
    # damgaları yerinde durur. Friksiyon HIZI (gün başına ciro/sürüklenme) bu paydayı
    # kullanır — "günde ne kadar yakıyor" sorusu bar sayısından okunamaz.
    days: float


@dataclass(frozen=True, kw_only=True)
class FrictionStats:
    """Modelin FRİKSİYON HIZI: günde ne kadar sermaye çeviriyor ve bu ne kadara mal oluyor.

    **Neden ayrı bir ölçü:** `cost_per_r` maliyeti İŞLEM BAŞINA ölçer ve işlem sıklığını
    görmez; iki model aynı `cost_per_r` ile koşup biri diğerinden on kat hızlı
    çevirebilir. Karar 35'in özdeşliği (net R = (brüt sürüklenme% − maliyet%) / stop%)
    tek bir pozisyonun içindedir; hesabın ne kadar hızlı eridiğini söyleyen şey ise o
    özdeşliğin gün başına kaç kez uygulandığıdır.

    **Birim kasten sermayedir, R değil.** `turnover_per_day` ve `cost_drag_pct_per_day`
    BAŞLANGIÇ sermayesine bölünür: güncel bakiyeye bölmek sayıyı modelin kendi
    performansına bağlar (kaybeden modelin cirosu yapay yükselir) ve modeller arası
    kıyası bozardı. Başlangıç sermayesi tüm modellerde aynıdır (kural 6), yani bu iki
    sayı çıpa, kopya ve yarışmacı satırlarında AYNI birimdedir — `cost_per_r`'nin
    aksine.

    **Bu bir ÖLÇÜMDÜR, bir kural DEĞİL** (seans ve yoğunlaşma ölçümleriyle aynı statü):
    hiçbir sinyal ciroya göre elenmez, hiçbir boyut ona göre değişmez. Bir işlem sıklığı
    tavanı karar 40'ta açıkça REDDEDİLDİ; burada ölçülen şey o tavanın yokluğunun bedeli.
    """

    days: float
    trades_per_day: float
    # Σnotional / başlangıç sermayesi / gün — "günde kaç kat sermaye çevrildi".
    turnover_per_day: float
    # Σ(komisyon + kayma) / başlangıç sermayesi / gün, yüzde. Funding DIŞARIDADIR:
    # funding bir carry'dir, işareti iki yönlü olabilir ve bir işlem maliyeti değildir;
    # `cost_per_r`nin payı da (komisyon + kayma) olduğu için iki sayı aynı şeyi sayar.
    cost_drag_pct_per_day: float


@dataclass(frozen=True, kw_only=True)
class ModelMetrics:
    model: str
    long: DirectionStats
    short: DirectionStats
    total: DirectionStats
    account: AccountStats
    friction: FrictionStats
    is_benchmark: bool = False  # kural 15: yarışmacı değil, referans çıpası
    is_replica: bool = False    # yarışmacı değil, dış sistem kopyası

    @property
    def is_competitor(self) -> bool:
        """Ortalama R sıralamasına ve kabul çıtasına giren satır mı?

        Tek yerde tanımlı olması şart: "yarışmacı" ölçütü tabloda, kabul kapılarında,
        havuzda ve stop bandı medyanında AYNI küme olmalıdır. Ayrışırlarsa bir modelin
        bandı hesaplanır ama sıralamada görünmez (ya da tersi) ve iki sayı birbirinin
        dilinden konuşmayı bırakır.
        """
        return not (self.is_benchmark or self.is_replica)

    def by_direction(self, direction: str) -> DirectionStats:
        return {"long": self.long, "short": self.short, TOTAL: self.total}[direction]


# --------------------------------------------------------------------------- #
# İşlem-tabanlı metrikler
# --------------------------------------------------------------------------- #
def r_multiple(row: Mapping[str, Any]) -> float | None:
    """İşlemin R katsayısı; riske edilen tutar bilinmiyorsa None (0.0 değil).

    Likidasyonda R -1'in altına iner (marjın tamamı gider) — bu bir hata değil, tam da
    ölçmek istediğimiz risk farkının görünür hâlidir.
    """
    risk = _to_float(row.get("risk_amount"))
    if risk is None or risk <= 0.0:
        return None
    pnl = _to_float(row.get("pnl"))
    return None if pnl is None else pnl / risk


def stop_distance_pct(row: Mapping[str, Any]) -> float | None:
    """`|giriş − ilk stop| / giriş` (yüzde). Modelin hangi R ölçeğinde işlem yaptığını gösterir."""
    entry = _to_float(row.get("entry_price"))
    stop = _to_float(row.get("stop_price"))
    if entry is None or stop is None or entry <= 0.0:
        return None
    return abs(entry - stop) / entry * 100.0


def cost_per_r(row: Mapping[str, Any]) -> float | None:
    """İşlemin tüm dolumlarında ödenen komisyon + kaymanın `risk_amount`'a oranı.

    "Tüm dolumlar" (giriş, kısmi TP'ler, çıkış) şartını sağlayan şey satırın kendisi
    değil, `merge_fills`tir: defterde her dilim ayrı satırdır ve tek bir dilimin
    maliyet/risk oranı pozisyonun maliyetini anlatmaz. Birleştirilmiş satırda hem pay
    hem payda pozisyonun tamamını taşır.

    Payda her zaman İLK stop'tan gelen `risk_amount`'tır, trailing ile güncellenen stop
    değil: R giriş anında üstlenilen risktir, trailing yalnızca kârı korur. Yürüyen stop'u
    kullanmak iyi giden işlemlerin paydasını sonradan küçültüp cost_per_r'yi şişirirdi —
    üstelik bu şişme trailing kullanan modellerde farklı olur, yani tam da kıyaslanmak
    istenen şeyi bozardı.
    """
    risk = _to_float(row.get("risk_amount"))
    if risk is None or risk <= 0.0:
        return None
    fee = _to_float(row.get("fee")) or 0.0
    slippage = _to_float(row.get("slippage_cost")) or 0.0
    return (fee + slippage) / risk


# --------------------------------------------------------------------------- #
# Pozisyon birleştirme: bir POZİSYON = bir ölçüm satırı
# --------------------------------------------------------------------------- #
# `trades.csv` bir DOLUM defteridir, işlem defteri değil: kısmi çıkış (`exit_reason`
# "partial") ve `fraction < 1.0` olan her take-profit aynı pozisyon için AYRI satır
# yazar (core/portfolio.py::_close, `fraction_of_initial`). Bugün `avwap` iki TP
# seviyesi, `downtrend_rally` yarım TP, modeller 13/14/15 ise üç aşamalı çıkış
# kullanıyor — hepsi pozisyon başına birden çok satır demektir.
#
# Ölçüm bu satırları TEK pozisyona indirger. İki alternatif de yanlıştı:
#   - Satırları olduğu gibi saymak, aynı pozisyonu iki kez ölçüme sokar ve kazanma
#     oranını yapay yükseltir (kısmi çıkış tanımı gereği kârda gerçekleşir).
#   - Kısmi satırı ATMAK ise ters yönde bozar: pozisyonun kilitlenmiş kârı ölçümden
#     düşer, kalan dilimin R'si tüm pozisyonun R'si sanılır ve yönetimli model
#     (15) yönetimsiz ikizine (12) karşı haksızca kötü görünür.
# Toplayarak ikisinden de kaçınılır: R = Σpnl / Σrisk, yani pozisyonun GERÇEK R'si.
_SUMMED_COLUMNS: tuple[str, ...] = (
    "qty", "notional", "risk_amount", "margin", "fee", "slippage_cost", "funding", "pnl",
)


def position_key(row: Mapping[str, Any]) -> tuple[str, str, str, str]:
    """Satırın ait olduğu pozisyonun kimliği.

    `strategy` anahtara dâhildir çünkü havuz (`pooled_direction_stats`) birden çok
    modelin satırlarını tek listede birleştirir; onsuz iki modelin aynı sembolde aynı
    barda açtığı pozisyonlar tek pozisyon sanılırdı — kural 4'ün izolasyonu ölçümde
    delinirdi.
    """
    return (
        str(row.get("strategy", "")),
        str(row.get("symbol", "")),
        str(row.get("direction", "")),
        str(row.get("opened_at", "")),
    )


def merge_fills(trades: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Dolum satırlarını pozisyon başına tek ölçüm satırına indirger.

    Nakit kolonları (`pnl`, `fee`, `slippage_cost`, `funding`) TOPLANIR, atılmaz:
    "Σpnl = bakiye değişimi" değişmezi bu toplamayla korunur. Açılış alanları
    (`entry_price`, `stop_price`, `signal_reason`) pozisyonun tamamı için aynıdır;
    kapanış alanları (`closed_at`, `exit_reason`, `notes`) pozisyonu KAPATAN son
    dolumdan gelir — ara dilimin çıkış sebebini pozisyonun sebebi saymak, kısmi
    çıkışla kapanmış gibi görünen bir işlem üretirdi.

    `opened_at` taşımayan satır (elle düzeltilmiş ya da şema öncesi bir defter) kendi
    başına bir pozisyon sayılır: bilinmeyen kimliği ortak kabul edip hepsini tek
    pozisyonda toplamak, sessiz bir veri kaybı olurdu.
    """
    grouped: dict[Any, list[Mapping[str, Any]]] = {}
    for index, row in enumerate(trades):
        key = position_key(row)
        grouped.setdefault(key if key[3] else (key, index), []).append(row)
    merged = [_merge_position(rows) for rows in grouped.values()]
    merged.sort(key=lambda row: str(row.get("closed_at", "")))
    return merged


def _merge_position(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    # Kararlı sıralama: aynı barda gerçekleşen kısmi ve TP dolumları eşit `closed_at`
    # taşır; defterdeki (append) sırası korunur, yani pozisyonu kapatan satır sonda kalır.
    ordered = sorted(rows, key=lambda row: str(row.get("closed_at", "")))
    final = ordered[-1]
    merged = dict(ordered[0])
    merged.update(
        {
            "closed_at": final.get("closed_at", ""),
            "exit_price": final.get("exit_price"),
            "exit_reason": final.get("exit_reason", ""),
            "notes": final.get("notes", ""),
            "fills": len(ordered),
        }
    )
    for column in _SUMMED_COLUMNS:
        values = _collect(ordered, lambda row, col=column: _to_float(row.get(col)))
        merged[column] = sum(values) if values else None
    return merged


def direction_stats(
    trades: Iterable[Mapping[str, Any]],
    *,
    direction: str = TOTAL,
    is_benchmark: bool = False,
    is_replica: bool = False,
    ci_alpha: float = _NAN,
    bootstrap_samples: int = 0,
    seed: int = 0,
    reference: Any = None,
) -> DirectionStats:
    """`direction` ("long" | "short" | "total") için işlem metrikleri.

    Birim POZİSYONDUR: `trades` defterdeki satır sayısı değil, `merge_fills`ten geçmiş
    pozisyon sayısıdır. Kısmi çıkış ve fraksiyonel TP kullanan modeller aynı pozisyon
    için birden çok satır yazar; onları ayrı işlem saymak kazanma oranını yapay
    yükseltir ve `acceptance.min_trades` örneklem kapısını iki kat hızlı geçirirdi.
    Nakit kolonları (`pnl`, `fees`, `slippage_cost`, `funding`) birleştirmede TOPLANIR,
    yani "Σpnl = bakiye değişimi" değişmezi korunur.

    Maliyet ölçeği kolonları `is_benchmark` ya da `is_replica` iken KOŞULSUZ `nan` olur.

    Çıpada (kural 15) stop yoktur, yani bu değerler defterden zaten hesaplanamaz — ama
    garantiyi defterin içeriğine bırakmak kırılgan olurdu: çıpanın defterine bir gün
    stop'lu bir satır girerse (elle düzeltme, şema göçü) tablo sessizce onu yarışmacı bir
    maliyet ölçeği gibi gösterirdi.

    Kopyada stop VARDIR ve sayı hesaplanabilir; yine de `nan` yazılır çünkü kolonun
    anlamı kıyastır: kopya 1R'yi sabit teminattan türetir, yarışmacılar sermayenin
    %1'inden. İkisini aynı sütunda göstermek, farklı paydaya sahip iki oranı
    karşılaştırılabilirmiş gibi sunardı.

    `notional` ve `cost_pct` bu muafiyetin DIŞINDADIR ve her satırda hesaplanır: paydası
    notional'dır, yani ortak bir birim (bkz. `DirectionStats`).

    `bootstrap_samples > 0` verilirse ortalama R'nin yüzdelik bootstrap aralığı da
    hesaplanır. Aralık kabul çıtasının aralığıyla AYNI alfayı ve AYNI tohum türetmesini
    kullanır (çağıran taşır): iki sayı aynı tabloda yan yana durduğu için ikinci bir
    alfa, aynı modelin iki farklı kesinlik ölçüsünü göstermek olurdu.

    `reference` verilirse (zaman indeksli çıpa kapanış serisi — canlıda `MarketData.btc`)
    piyasa kontrolü de hesaplanır. Modül veriyi kendisi ÇEKMEZ; seri dışarıdan enjekte
    edilir, çünkü `core/metrics.py` defteri okur ve borsaya hiç dokunmaz.
    """
    # Ölçümün birimi POZİSYONDUR, dolum değil: `merge_fills` aynı pozisyonun kısmi çıkış
    # ve fraksiyonel TP satırlarını tek satıra indirger (bkz. merge_fills). Kapanış sırası
    # da oradan gelir — yön bazlı R-Sharpe, o yöndeki işlemlerin kapanış sırasına göre
    # dizilmiş R dizisinden hesaplanır (CLAUDE.md > Rapor Kolonları).
    rows = [
        row
        for row in merge_fills(trades)
        if direction == TOTAL or str(row.get("direction", "")) == direction
    ]
    r_values = [r for r in (r_multiple(row) for row in rows) if r is not None]
    wins = [r for r in r_values if r > 0.0]
    losses = [r for r in r_values if r < 0.0]
    # Yüzde profili R'den BAĞIMSIZ toplanır: `r_multiple` `risk_amount`ı olmayan satırda
    # None döner (çıpa) ama yüzde getirisi orada da tanımlıdır. İki listeyi tek döngüde
    # türetmek, çıpanın ödeme profilini sessizce boşaltırdı.
    pnl_pcts = _collect(rows, _pnl_pct)
    win_pcts = [value for value in pnl_pcts if value > 0.0]
    loss_pcts = [value for value in pnl_pcts if value < 0.0]
    loss_total = abs(sum(losses))
    notional = _sum_column(rows, "notional")
    cost = _sum_column(rows, "fee") + _sum_column(rows, "slippage_cost")
    tailwind, market_r, market_measured = _market_context(rows, reference=reference)
    # Tohuma yön karıştırılır: long, short ve toplam aynı tohumla yeniden örneklenseydi
    # üç aralık aynı çekiliş desenini paylaşır, bağımsız birer ölçü olmaktan çıkardı.
    ci_low, ci_high = bootstrap_mean_ci(
        r_values,
        alpha=ci_alpha,
        iterations=bootstrap_samples,
        seed=int(seed) ^ hash_name(direction),
    )

    return DirectionStats(
        direction=direction,
        trades=len(rows),
        avg_r=_mean(r_values),
        median_r=_median(r_values),
        total_r=sum(r_values) if r_values else _NAN,
        r_sharpe=_ratio(_mean(r_values), _stdev(r_values)),
        max_drawdown_r=_max_drawdown_r(r_values),
        win_rate=len(wins) / len(r_values) if r_values else _NAN,
        avg_win_r=_mean(wins),
        avg_loss_r=_mean(losses),
        profit_factor=_ratio(sum(wins), loss_total) if r_values else _NAN,
        avg_stop_distance_pct=(
            _NAN
            if (is_benchmark or is_replica)
            else _mean(_collect(rows, stop_distance_pct))
        ),
        cost_per_r=(
            _NAN if (is_benchmark or is_replica) else _mean(_collect(rows, cost_per_r))
        ),
        avg_win_pct=_mean(win_pcts),
        avg_loss_pct=_mean(loss_pcts),
        payoff=_ratio(_mean(win_pcts), abs(_mean(loss_pcts))),
        max_drawdown_pnl=_max_drawdown_series(
            _collect(rows, lambda row: _to_float(row.get("pnl")))
        ),
        avg_r_ci_low=ci_low,
        avg_r_ci_high=ci_high,
        notional=notional,
        cost_pct=_pct(_ratio(cost, notional)),
        market_tailwind_pct=tailwind,
        market_r=market_r,
        market_measured=market_measured,
        pnl=_sum_column(rows, "pnl"),
        fees=_sum_column(rows, "fee"),
        slippage_cost=_sum_column(rows, "slippage_cost"),
        funding=_sum_column(rows, "funding"),
        liquidations=sum(1 for row in rows if row.get("exit_reason") == "liquidation"),
        unmeasured=len(rows) - len(r_values),
    )


def normalize_reference(reference: Any) -> "pd.Series | None":
    """Çıpa serisini UTC zaman indeksli, sıralı bir `pd.Series`e çevirir.

    Damgaların UTC'ye çekilmesi `_utc_stamp` ile aynı gerekçedir: defter UTC yazar ve
    zaman dilimsiz bir çıpa, ölçümü dosyayı okuyan makinenin ayarına bağlardı.
    """
    if reference is None:
        return None
    series = pd.Series(reference).dropna()
    if series.empty:
        return None
    index = pd.DatetimeIndex(series.index)
    index = index.tz_localize("UTC") if index.tz is None else index.tz_convert("UTC")
    series.index = index
    return series.sort_index()


def _price_at_or_before(series: "pd.Series", when: pd.Timestamp) -> float | None:
    """Çıpanın `when` anında ya da ÖNCESİNDE bilinen son kapanışı.

    "Sonrasına" bakmak look-ahead olurdu (kural 12); tam eşleşme aramak ise 15 dakikalık
    bir çıpa ile 4 saatlik bir defteri hiç eşleştiremezdi. Pozisyonun penceresi çıpa
    serisinin BAŞLANGICINDAN önce açılmışsa fiyat yoktur ve pozisyon ÖLÇÜLMEZ — uydurma
    bir başlangıç fiyatı, o pozisyonun piyasa katkısını sıfır göstermek olurdu.
    """
    position = series.index.searchsorted(when, side="right") - 1
    if position < 0:
        return None
    return _to_float(series.iloc[position])


def _market_context(
    rows: Sequence[Mapping[str, Any]], *, reference: Any
) -> tuple[float, float, int]:
    """Pozisyonların tutuş penceresinde çıpanın katkısı: (tailwind%, market_R, sayı).

    **Ortalama, ORANLARIN ortalamasıdır** — `cost_per_r` ile birebir aynı sözleşme:
    önce her pozisyon için `tailwind% / stop%`, sonra ortalama. Önce ortalamaları alıp
    bölmek dar stop'lu pozisyonların katkısını gizlerdi ve iki kolon birbirinin dilinden
    konuşmayı bırakırdı.

    **beta = 1 VARSAYIMI açıktır ve gizlenmez:** `market_r`, pozisyonun çıpayla birebir
    hareket ettiği durumda kazanacağı R'dir. Bir sembol bazlı beta tahmini serbest bir
    parametre açardı (pencere, yöntem, yeniden hesaplama sıklığı) ve ölçüyü o seçimlere
    bağlardı; varsayımı sabit ve görünür tutmak, tahmin etmekten daha denetlenebilirdir.
    """
    series = normalize_reference(reference)
    if series is None:
        return (_NAN, _NAN, 0)

    tailwinds: list[float] = []
    market_r: list[float] = []
    for row in rows:
        opened = _utc_stamp(row.get("opened_at"))
        closed = _utc_stamp(row.get("closed_at"))
        if opened is None or closed is None:
            continue
        start = _price_at_or_before(series, opened)
        end = _price_at_or_before(series, closed)
        if start is None or end is None or start <= 0.0:
            continue
        sign = 1.0 if str(row.get("direction", "")) == "long" else -1.0
        value = (end / start - 1.0) * 100.0 * sign
        tailwinds.append(value)
        stop = stop_distance_pct(row)
        if stop:
            market_r.append(value / stop)
    return (_mean(tailwinds), _mean(market_r), len(tailwinds))


def _pnl_pct(row: Mapping[str, Any]) -> float | None:
    """Pozisyonun GİRİŞ notional'ına oranla net sonucu (yüzde).

    Net: komisyon, kayma ve funding `pnl`in içindedir (defterin `pnl` kolonu kapanışta
    yazılan nakit değişimidir). Brüt bir yüzde, maliyeti ölçüme sokmayan bir sayı olurdu
    ve tam da kıyaslanmak istenen friksiyon görünmez kalırdı.
    """
    pnl = _to_float(row.get("pnl"))
    notional = _to_float(row.get("notional"))
    if pnl is None or notional is None or notional <= 0.0:
        return None
    return pnl / notional * 100.0


def _max_drawdown_series(values: Sequence[float]) -> float:
    """Sıralı bir nakit akışının kümülatif eğrisindeki en büyük tepe-dip düşüş (<= 0)."""
    if not values:
        return _NAN
    cumulative = peak = worst = 0.0
    for value in values:
        cumulative += value
        peak = max(peak, cumulative)
        worst = min(worst, cumulative - peak)
    return worst


@dataclass(frozen=True, kw_only=True)
class HoldingStats:
    """Pozisyonların TUTUŞ SÜRESİ dağılımı. Bir ÖLÇÜMDÜR, bir kural değil.

    Neden ayrı bir ölçü: zaman stop'u olmayan bir modelde (ör. `ema_trend`) pozisyon
    ömrü sınırsızdır ve OOS penceresinin embargosu (docs/backtest.md > 6.1) bu yüzden
    VARSAYILAMAZ — "doğru boşluk azami tutuş süresidir" kuralının dayandığı üst sınır
    o modelde tanım gereği yoktur. Sınır burada ÖLÇÜLÜR.

    Dağılımın kendisi de bir bulgudur: uzun bir kuyruk, kurulumların hedefe ya da stop'a
    varmadan beklediğini söyler — yani "hedefe ulaşma oranı düşük" iddiasının bağımsız
    kontrolüdür.

    `bars` ve `days` birlikte tutulur çünkü ikisi farklı soruya cevap verir: bar sayısı
    modelin geometrisiyle (zaman stop'u, hedef mesafesi) kıyaslanabilir, gün ise funding
    maliyetiyle ve embargo takvimiyle.
    """

    positions: int
    median_bars: float
    p90_bars: float
    max_bars: float
    median_days: float
    p90_days: float
    max_days: float


def holding_stats(
    trades: Iterable[Mapping[str, Any]], *, bar_duration: pd.Timedelta
) -> HoldingStats:
    """Kapanmış POZİSYONLARIN açılış-kapanış süresi dağılımı.

    Birim pozisyondur (`merge_fills`): kısmi çıkışlı bir pozisyonun her dilimini ayrı
    saymak, erken kapanan dilim yüzünden dağılımı kısa tarafa çekerdi. Süre, pozisyonu
    KAPATAN dilimin `closed_at`ine göre ölçülür — pozisyon o ana kadar taşınmıştır.

    Bar süresi dışarıdan gelir (`core/data.py::bar_duration`): bu modül katmanın zaman
    dilimini bilmez ve bilmemelidir.
    """
    if bar_duration <= pd.Timedelta(0):
        raise ValueError(f"bar süresi pozitif olmalı: {bar_duration}")

    spans: list[float] = []
    for row in merge_fills(trades):
        opened, closed = _utc_stamp(row.get("opened_at")), _utc_stamp(row.get("closed_at"))
        if opened is None or closed is None:
            continue
        seconds = (closed - opened).total_seconds()
        if seconds < 0.0:
            continue
        spans.append(seconds)

    # `_percentile` SIRALI dizi bekler (bootstrap aralıkları da onu öyle çağırır) ve bu
    # modülde TEK tanımdır. Buraya ikinci bir yüzdelik fonksiyonu yazmak, aynı defterin
    # iki farklı yüzdelik verebilmesi demekti — nitekim ilk hâlinde tam olarak bu oldu:
    # sıralanmamış girdiyle p90, medyandan KÜÇÜK çıktı.
    bars = sorted(span / bar_duration.total_seconds() for span in spans)
    days = sorted(span / 86400.0 for span in spans)
    return HoldingStats(
        positions=len(spans),
        median_bars=_median(bars),
        p90_bars=_percentile(bars, 0.90),
        max_bars=bars[-1] if bars else _NAN,
        median_days=_median(days),
        p90_days=_percentile(days, 0.90),
        max_days=days[-1] if days else _NAN,
    )


def buy_hold_return(series: "pd.Series", *, start: pd.Timestamp, end: pd.Timestamp) -> float:
    """Bir sembolün pencere boyunca al-tut getirisi (yüzde); fiyat yoksa `nan`.

    Neden burada: `buyhold` çıpası (kural 15) BTC %50 / ETH %50 taşır ve hesap düzeyinde
    tek bir sayı verir — "bu SEMBOL ne yaptı" sorusunun cevabı onda yoktur. Kırılım
    tablosunda bir sembolün ortalama R'sini okumak, o sembolün o pencerede ne yaptığını
    bilmeden yanıltıcıdır (çöken bir sembolde long-only bir modelin kaybetmesi bir sinyal
    kusuru değildir).

    Seri DIŞARIDAN enjekte edilir (`compare(reference=...)` ile aynı sözleşme): bu modül
    defteri okur, borsaya hiç dokunmaz. Fiyat çapaları `_price_at_or_before` ile
    bulunur, yani pencerenin ucunda barı olmayan sembol uydurma bir fiyat almaz.
    """
    first = _price_at_or_before(series, start)
    last = _price_at_or_before(series, end)
    if first is None or last is None or first <= 0.0:
        return _NAN
    return (last / first - 1.0) * 100.0


def pnl_drawdown_pct(stats: DirectionStats, *, initial_capital: float) -> float:
    """`max_drawdown_pnl`i başlangıç sermayesinin yüzdesine çevirir (<= 0).

    Payda GÜNCEL bakiye DEĞİLDİR ve sebebi `FrictionStats`inkiyle aynı: güncel bakiyeye
    bölmek ölçüyü modelin kendi performansına bağlar, yani kaybeden bir modelin
    drawdown'ı yapay büyür. Sabit payda, aynı sayıyı iki model arasında kıyaslanabilir
    kılar.

    Ayrı bir fonksiyon olmasının sebebi `friction_stats`inkiyle aynı: `direction_stats`
    defteri okur, config'i değil; başlangıç sermayesi bir AYAR'dır ve onu okuyan taraf
    çağırandır.
    """
    if initial_capital <= 0.0 or stats.max_drawdown_pnl != stats.max_drawdown_pnl:
        return _NAN
    return stats.max_drawdown_pnl / initial_capital * 100.0


def _max_drawdown_r(r_values: Sequence[float]) -> float:
    """Kümülatif R eğrisinin tepe-dip en büyük düşüşü (negatif ya da 0.0).

    Yön bazında risk profilini özsermaye eğrisine bakmadan ölçmenin yolu budur: hesapta
    tek bakiye vardır, longun drawdown'ı shortunkinden ayrıştırılamaz ama R eğrileri
    ayrıştırılabilir.
    """
    if not r_values:
        return _NAN
    cumulative = 0.0
    peak = 0.0
    worst = 0.0
    for value in r_values:
        cumulative += value
        peak = max(peak, cumulative)
        worst = min(worst, cumulative - peak)
    return worst


# --------------------------------------------------------------------------- #
# Hesap düzeyi metrikler (equity.csv)
# --------------------------------------------------------------------------- #
def account_stats(
    equity_rows: Sequence[Mapping[str, Any]],
    *,
    initial_capital: float,
    periods_per_year: float,
) -> AccountStats:
    """Özsermaye eğrisinden getiri, max drawdown ve Sharpe.

    Sharpe bar getirileri üzerinden hesaplanır ve risksiz getiri 0 alınır: modeller aynı
    anda, aynı para birimiyle ve aynı ufukta yarıştığı için ortak bir sabit çıkarmak
    sıralamayı değiştirmez, ama uydurulmuş bir oran karşılaştırmayı bozabilirdi.
    """
    equity = [value for value in (_to_float(row.get("equity")) for row in equity_rows) if value is not None]
    if not equity:
        return AccountStats(
            initial_capital=initial_capital,
            final_equity=initial_capital,
            total_return=_NAN,
            max_drawdown=_NAN,
            sharpe=_NAN,
            bars=0,
            days=_NAN,
        )

    returns = [
        equity[index] / equity[index - 1] - 1.0
        for index in range(1, len(equity))
        if equity[index - 1] > 0.0
    ]
    return AccountStats(
        initial_capital=initial_capital,
        final_equity=equity[-1],
        total_return=(equity[-1] / initial_capital - 1.0) if initial_capital > 0.0 else _NAN,
        max_drawdown=_max_drawdown_pct(equity),
        sharpe=_ratio(_mean(returns), _stdev(returns)) * math.sqrt(periods_per_year),
        bars=len(equity),
        days=_equity_span_days(equity_rows),
    )


def _equity_span_days(equity_rows: Sequence[Mapping[str, Any]]) -> float:
    """Defterin kapsadığı takvim günü: ilk ve son damga arası.

    Bar SAYISINDAN türetilmez, çünkü saklama penceresi eski satırları günlük özete
    indirir (CLAUDE.md > Saklama penceresi) ve bar sayısı o noktadan sonra geçen zamanı
    anlatmayı bırakır. Damga okunamıyorsa ya da tek satır varsa `nan`: sıfır gün bir
    payda değildir ve "bir günde şu kadar çevirdi" demek, henüz ölçülmemiş bir hızı
    ölçülmüş gibi gösterirdi.
    """
    stamps = [
        stamp
        for stamp in (_utc_stamp(row.get("ts")) for row in equity_rows)
        if stamp is not None
    ]
    if len(stamps) < 2:
        return _NAN
    span = (max(stamps) - min(stamps)).total_seconds() / 86400.0
    return span if span > 0.0 else _NAN


def _max_drawdown_pct(equity: Sequence[float]) -> float:
    peak = equity[0]
    worst = 0.0
    for value in equity:
        peak = max(peak, value)
        if peak > 0.0:
            worst = min(worst, value / peak - 1.0)
    return worst


def friction_stats(
    total: DirectionStats, *, initial_capital: float, days: float
) -> FrictionStats:
    """Friksiyon hızı. Girdi zaten hesaplanmış TOPLAM yön istatistiğidir.

    Defteri ikinci kez toplamaz: `Σnotional` ve `Σ(komisyon+kayma)` tek bir yerde
    (`direction_stats`) hesaplanır ve buraya taşınır. İkinci bir toplama yolu bugün
    hizalansa bile yarın ayrışır — aynı modelin tablodaki maliyeti ile friksiyon
    satırındaki maliyeti birbirini tutmazdı.
    """
    cost = total.fees + total.slippage_cost
    return FrictionStats(
        days=days,
        trades_per_day=_ratio(float(total.trades), days),
        turnover_per_day=_ratio(_ratio(total.notional, initial_capital), days),
        cost_drag_pct_per_day=_ratio(_pct(_ratio(cost, initial_capital)), days),
    )


def periods_per_year(config: Mapping[str, Any]) -> float:
    duration = bar_duration(str(get_setting(dict(config), "timeframe")))
    return pd.Timedelta(days=_DAYS_PER_YEAR) / duration


# --------------------------------------------------------------------------- #
# Model ve karşılaştırma
# --------------------------------------------------------------------------- #
def model_metrics(
    model: str,
    *,
    trades: Sequence[Mapping[str, Any]],
    equity_rows: Sequence[Mapping[str, Any]],
    initial_capital: float,
    periods_per_year: float,
    is_benchmark: bool = False,
    is_replica: bool = False,
    ci_alpha: float = _NAN,
    bootstrap_samples: int = 0,
    seed: int = 0,
    reference: Any = None,
) -> ModelMetrics:
    flags = {"is_benchmark": is_benchmark, "is_replica": is_replica}
    # Tohum model adına bağlanır — kabul çıtasının farkı için yapılanın aynısı
    # (bkz. `acceptance_flags`): her model kendi yeniden örneklemesini alır ama aynı
    # defter her zaman aynı aralığı verir.
    ci = {
        "ci_alpha": ci_alpha,
        "bootstrap_samples": bootstrap_samples,
        "seed": int(seed) ^ hash_name(model),
        # Çıpa BİR KEZ normalize edilir: her yön için yeniden kurmak aynı seriyi üç kez
        # sıralamak olurdu ve sonucu değiştirmezdi.
        "reference": normalize_reference(reference),
    }
    total = direction_stats(trades, direction=TOTAL, **flags, **ci)
    account = account_stats(
        equity_rows, initial_capital=initial_capital, periods_per_year=periods_per_year
    )
    return ModelMetrics(
        model=model,
        long=direction_stats(trades, direction="long", **flags, **ci),
        short=direction_stats(trades, direction="short", **flags, **ci),
        total=total,
        account=account,
        friction=friction_stats(
            total, initial_capital=initial_capital, days=account.days
        ),
        **flags,
    )


def compare(
    models: Sequence[str],
    *,
    ledger: Ledger | None = None,
    config: Mapping[str, Any],
    benchmarks: Collection[str] = (),
    replicas: Collection[str] = (),
    reference: Any = None,
) -> list[ModelMetrics]:
    """Defterleri okuyup her model için metrikleri üretir. Defter değiştirilmez.

    `benchmarks` referans çıpalarının (kural 15), `replicas` ise dış sistem kopyalarının
    adlarıdır. Bilgi stratejinin `is_benchmark` / `is_replica` alanından gelir ve buraya
    çağıran tarafından taşınır: metrics defteri okur, strateji sınıflarını değil —
    `strategies/` importu, salt okunur bir metrik modülünü tüm model koduna bağlardı.
    """
    active_ledger = ledger if ledger is not None else Ledger()
    config_dict = dict(config)
    initial_capital = float(get_setting(config_dict, "initial_capital"))
    per_year = periods_per_year(config_dict)
    benchmark_names = set(benchmarks)
    replica_names = set(replicas)
    # Aralığın alfası ve yeniden örnekleme sayısı kabul çıtasının anahtarlarından okunur,
    # yeni bir anahtar açılmaz: aynı tabloda iki farklı kesinlik ölçüsü göstermek,
    # `trailing.atr_period`in tek tutulmasıyla aynı gerekçeyle reddedilir.
    ci_alpha = float(get_setting(config_dict, "acceptance.edge_ci_alpha"))
    bootstrap_samples = int(get_setting(config_dict, "acceptance.bootstrap_samples"))
    seed = int(get_setting(config_dict, "random_seed"))
    normalized = normalize_reference(reference)
    return [
        model_metrics(
            model,
            trades=active_ledger.read_trades(model),
            equity_rows=active_ledger.read_equity(model),
            initial_capital=initial_capital,
            periods_per_year=per_year,
            is_benchmark=model in benchmark_names,
            is_replica=model in replica_names,
            ci_alpha=ci_alpha,
            bootstrap_samples=bootstrap_samples,
            seed=seed,
            reference=normalized,
        )
        for model in models
    ]


# --------------------------------------------------------------------------- #
# Kırılımlar (kol / sembol)
# --------------------------------------------------------------------------- #
def breakdown(
    trades: Iterable[Mapping[str, Any]],
    *,
    key: Callable[[Mapping[str, Any]], str],
    ci_alpha: float = _NAN,
    bootstrap_samples: int = 0,
    seed: int = 0,
) -> dict[str, DirectionStats]:
    """İşlemleri `key`e göre gruplayıp her grup için aynı metrikleri hesaplar.

    Grup ölçütü dışarıdan gelir: bu modül defteri okur, modelleri değil (bkz. `compare`).
    Kol kırılımı `arm_of`, sembol kırılımı `symbol_of` ile kurulur; ikisi de satırın
    kendi alanlarından türer.

    Her grup TOPLAM (yön ayrımsız) raporlanır. Grup × yön kırılımı JSON'u üç katına
    çıkarırdı ve yön sorusunun cevabı zaten model tablosunda ve havuz panelinde durur;
    kırılımın cevapladığı soru farklıdır: "bu kol/sembol ölçülebilir bir şey üretti mi".

    `key` bir satırda hata fırlatırsa hata YUTULMAZ (bkz. `arm_of`): eksik etiketi olan
    satırı gruptan düşürmek, kırılım toplamı ile model toplamını sessizce ayrıştırırdı.

    Gruplama DOLUM satırları üzerinde yapılır, birleştirme grubun içinde olur
    (`direction_stats` -> `merge_fills`). Sıra önemlidir ama sonucu değiştirmez: kol ve
    sembol pozisyonun özellikleridir, yani bir pozisyonun tüm dilimleri zaten aynı
    gruba düşer. Ters sırada kurmak (önce birleştir, sonra grupla) aynı sayıyı verirdi;
    böylesi `key`in defterin ham satırını görmesini korur.

    Gruplar da ortalama R'nin bootstrap aralığını alır (`bootstrap_samples > 0` iken).
    Gerekçe bu projenin kendi geçmişidir: karar 27 (saat hipotezi) ve karar 28 (kayıp
    serisi cooldown'u) tam olarak bir KIRILIM grubunun ortalamasına bakıp kural yazma
    denemeleriydi ve ikisi de daha uzun örneklemde çürüdü. Grup ortalamasını örneklemi
    ve aralığı olmadan göstermek, o hatayı arayüzün içine yerleştirmek olurdu.

    Tohum grup ADINA bağlanır (model adına bağlandığı gibi): her grup kendi yeniden
    örneklemesini alır, aynı defter her zaman aynı aralığı verir.
    """
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for row in trades:
        grouped.setdefault(key(row), []).append(row)
    return {
        group: direction_stats(
            rows,
            direction=TOTAL,
            ci_alpha=ci_alpha,
            bootstrap_samples=bootstrap_samples,
            seed=int(seed) ^ hash_name(group),
        )
        for group, rows in sorted(grouped.items())
    }


def arm_of(row: Mapping[str, Any]) -> str:
    """İşlemin kolu: `signal_reason` kuyruğundaki `arm=` etiketi.

    Etiket yoksa `TagError` (bkz. core/tags.py): kol kırılımının anlamı "her işlem bir
    kola aittir" varsayımına dayanır ve etiketsiz satırı sessizce atlamak o varsayımı
    denetlenemez kılardı.
    """
    return parse_tag(str(row.get("signal_reason", "")), "arm")


def symbol_of(row: Mapping[str, Any]) -> str:
    return str(row.get("symbol", ""))


# Seans sınırları UTC'dedir ve SABİTTİR: (başlangıç saati, ad). Son dilim gün sonuna sarar.
# Yerel saat kullanmak (ör. Europe/Berlin) yaz saati geçişlerinde sınırları yılda iki kez
# kaydırırdı ve aynı defter iki farklı kırılım üretirdi — tekrarlanabilirlik `random_seed`
# ile aynı statüdedir. Karşılığı parantezde yazılıdır ve YAZ saati içindir (CEST, UTC+2);
# kış saatinde yerel karşılık bir saat geriye kayar, UTC sınırları ise yerinde kalır.
_SESSIONS: tuple[tuple[int, str], ...] = (
    (0, "00-07_asya"),      # Berlin 02-09
    (7, "07-12_avrupa"),    # Berlin 09-14
    (12, "12-16_abd"),      # Berlin 14-18
    (16, "16-24_gece"),     # Berlin 18-02
)


def _utc_stamp(value: Any) -> pd.Timestamp | None:
    """Defter damgasını UTC'ye çevirir; okunamayan damga None (0 ya da "şimdi" DEĞİL).

    Zaman dilimsiz damga UTC sayılır: yerel saate çevirmek kırılımı defteri okuyan
    makinenin ayarına bağlardı.
    """
    raw = str(value or "")
    if not raw:
        return None
    try:
        stamp = pd.Timestamp(raw)
    except (ValueError, TypeError):
        return None
    if pd.isna(stamp):
        return None
    return stamp.tz_convert("UTC") if stamp.tzinfo is not None else stamp.tz_localize("UTC")


def session_of(row: Mapping[str, Any]) -> str:
    """İşlemin AÇILDIĞI seans (UTC sabit sınırlar; bkz. `_SESSIONS`).

    Ölçüt `opened_at`tır, `closed_at` değil: sorulan şey "bu kurulum hangi piyasa
    koşulunda ALINDI", "hangi koşulda kapandı" değil. Kapanışa göre gruplamak, gece açılıp
    sabah stoplanan bir pozisyonu sabahın hanesine yazardı.

    Kol ve sembol gibi bu da POZİSYONUN özelliğidir: bir pozisyonun tüm dilimleri aynı
    `opened_at`i taşır, yani aynı gruba düşer ve grupların `trades` toplamı model
    tablosuyla tutarlı kalır (`exit_rule` kırılımının aksine — bkz. `exit_rule_of`).

    **Bu kırılım bir ÖLÇÜMDÜR, bir kural değil.** Hiçbir model seansa bakmaz ve hiçbir
    sinyal bu etikete göre elenmez; kırılım yalnızca "seansın ölçülebilir bir etkisi var
    mı" sorusuna zamanla cevap biriktirir. Bugünkü cevap "ayırt edilemiyor"dur: kaynak
    sistemin 6 günlük 256 pozisyonluk defterinde seanslar arası fark permütasyon testinde
    p=0.575 çıktı (docs/decisions.md > 27). Karar için gereken şey daha çok GÜN, daha çok
    işlem değil — bir seansın tek bir gününü ölçmek, o günü ölçmektir.

    Okunamayan `opened_at` sessizce atlanmaz (`arm_of` ile aynı gerekçe): satırı gruptan
    düşürmek kırılım toplamı ile model toplamını ayrıştırırdı.
    """
    stamp = _utc_stamp(row.get("opened_at"))
    if stamp is None:
        raise ValueError(f"seans kırılımı: okunamayan opened_at {row.get('opened_at')!r}")
    hour = stamp.hour
    name = _SESSIONS[0][1]
    for start, label in _SESSIONS:
        if hour >= start:
            name = label
    return name


# Kayıp serisi kovaları: (asgari ardışık kayıp, ad). En üstteki eşleşen kazanır.
# Kovalar 0..4'ü TEK TEK tutar, 5 ve üstünü birleştirir. Gerekçe ölçülen şeyin kendisi:
# kaynak sistemin 256 pozisyonluk defterinde kayıp OLASILIĞI k arttıkça yükseliyor
# (0.598 -> 0.753) ama ortalama PnL k=4'te tabana DÖNÜYOR (8.89 vs 8.42). Yani karar
# açısından ilginç bölge 1-3 ile 4+ arasındaki sınırdır; kovaları daha kaba yapmak
# (ör. "1-2", "3+") tam o sınırı görünmez kılardı. 5+ birleştirilir çünkü orada örneklem
# hızla erir (41 -> 30 -> ...) ve ayrı kovalar birer gürültü satırına dönerdi.
_LOSS_STREAK_BUCKETS: tuple[tuple[int, str], ...] = (
    (0, "0"), (1, "1"), (2, "2"), (3, "3"), (4, "4"), (5, "5+"),
)
# Dolum satırına iliştirilen türetilmiş alan. Alt çizgi ile başlar: `trades.csv`in bir
# kolonu DEĞİLDİR ve deftere hiç yazılmaz (kural 13c'nin "yeni kolon açılmaz" gerekçesi —
# başlık değişirse eski satırlar okunamaz hâle gelir).
LOSS_STREAK_FIELD = "_loss_streak"


def annotate_loss_streak(trades: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Her dolum satırına, pozisyonun AÇILDIĞI andaki ardışık kayıp sayısını iliştirir.

    Diğer kırılımlardan farklı olarak bu ölçüt tek bir satırdan okunamaz: kayıp serisi
    satırın kendi alanlarında değil, ondan ÖNCE kapanmış pozisyonların SIRASINDA durur.
    Bu yüzden kırılım anahtarı (`loss_streak_of`) bir ön hazırlık ister ve yapan da bu
    fonksiyondur.

    **Kesim noktası `opened_at`tır, `closed_at` değil.** Sayılan şey "bu pozisyon
    açılırken modelin GÖREBİLDİĞİ kayıp serisi"dir; model yalnızca kapanmış işlemleri
    görebilir (kural 16). Kesimi kapanışa taşımak, pozisyonun kendi ömrü boyunca kapanan
    işlemleri de sayıya katardı — yani ölçüm, modelin karar anında sahip olmadığı bir
    bilgiyle kurulurdu ve bir cooldown kuralının ölçüsü olmaktan çıkardı.

    **Kayıp `pnl < 0` demektir; tam sıfır seriyi KIRAR.** Başabaş kapanan bir işlem
    (breakeven stop) bir kayıp değildir ve onu kayıp saymak serileri yapay olarak
    uzatırdı — üstelik tam da üç aşamalı çıkış yönetimini kullanan modellerde (13/14/15),
    yani kıyasın bir tarafında.

    Seri POZİSYON bazında sayılır ama alan DOLUM satırına yazılır: bir pozisyonun tüm
    dilimleri aynı `opened_at`i taşır, dolayısıyla aynı kovaya düşer ve grupların işlem
    sayısı toplamı model tablosuyla tutarlı kalır (`exit_rule`in aksine).
    """
    rows = [dict(row) for row in trades]
    positions = merge_fills(rows)  # `closed_at`e göre sıralı döner

    closes: list[pd.Timestamp] = []
    # trailing[i] = ilk i pozisyon kapandıktan SONRA geçerli ardışık kayıp sayısı.
    trailing: list[int] = [0]
    for position in positions:
        stamp = _utc_stamp(position.get("closed_at"))
        if stamp is None:
            # Kapanış zamanı okunamayan satır sıraya giremez; sessizce atmak yerine
            # seriyi kırar, çünkü "bu işlemin kayıp olup olmadığını bilmiyoruz" ile
            # "kayıp değildi" aynı şey değildir ve ikincisi daha temkinlidir.
            closes.append(pd.Timestamp.max.tz_localize("UTC"))
            trailing.append(0)
            continue
        closes.append(stamp)
        lost = _to_float(position.get("pnl"))
        trailing.append(trailing[-1] + 1 if lost is not None and lost < 0.0 else 0)

    buckets: dict[Any, str] = {}
    for position in positions:
        opened = _utc_stamp(position.get("opened_at"))
        # `closed_at <= opened_at` olan pozisyonlar bu pozisyon açılırken BİLİNİYORDU.
        # Pozisyonun kendisi doğal olarak dışarıda kalır: dolum bir sonraki bardadır
        # (kural 13), yani kapanışı açılışından kesin olarak sonradır.
        seen = 0 if opened is None else bisect_right(closes, opened)
        buckets[position_key(position)] = _loss_streak_label(trailing[seen])

    for row in rows:
        row[LOSS_STREAK_FIELD] = buckets.get(position_key(row), _LOSS_STREAK_BUCKETS[0][1])
    return rows


def _loss_streak_label(count: int) -> str:
    label = _LOSS_STREAK_BUCKETS[0][1]
    for threshold, name in _LOSS_STREAK_BUCKETS:
        if count >= threshold:
            label = name
    return label


def loss_streak_of(row: Mapping[str, Any]) -> str:
    """Pozisyon açılırken geçerli olan ardışık kayıp kovası (`annotate_loss_streak`).

    Alan yoksa hata fırlatılır, uydurma bir "0" üretilmez: alanın yokluğu "seri sıfırdı"
    değil "ön hazırlık hiç koşmadı" demektir ve ikisini aynı kovaya yazmak, bozuk bir
    kırılımı sağlam gibi gösterirdi.
    """
    if LOSS_STREAK_FIELD not in row:
        raise ValueError(
            f"kayıp serisi kırılımı: {LOSS_STREAK_FIELD} alanı yok "
            "(annotate_loss_streak çağrılmamış)"
        )
    return str(row[LOSS_STREAK_FIELD])


def exit_rule_of(row: Mapping[str, Any]) -> str:
    """Çıkışın TAM kimliği: `exit_reason` + varsa `notes` kuyruğundaki `exit_rule`.

    Neden gerekli: `exit_reason` beş kaba koddur ve ikisi birleşiktir (kural 13c) —
    `stop` hem ilk stop'u hem takip eden stop'u, `signal` hem zaman stop'unu hem başka
    bir strateji çıkışını anlatır. Oysa modeller 13/14/15'in ölçtüğü şey tam olarak
    yönetimin katkısıdır: "geri verme takibi bu katmanda hiç tetiklendi mi" sorusunun
    cevabı tek tek satırlara bakmadan okunamıyorsa, üç aşamalı yönetimin üçüncü aşaması
    ölçülmemiş demektir.

    İkisi birlikte etiketlenir (`stop:giveback`), çünkü ad uzayları ÇAKIŞIR: `partial`
    hem bir `exit_reason`dır (kısmi dolum satırı) hem bir stop kuralıdır (stop'un kısmi
    seviyeye çekilmesi). Tek başına "partial" grubu iki farklı olayı aynı satırda
    toplardı.

    **Etiketin YOKLUĞU bir bilgidir** (kural 13c): `stop` grubu "stop hiç hareket etmedi"
    demektir. Uydurma bir `initial` değeri ne deftere yazılır ne de burada üretilir.

    `parse_tag` DEĞİL `find_tag` kullanılır: `arm_of`un aksine etiketin yokluğu burada bir
    hata değil, anlamın kendisidir.

    **Dikkat — bu kırılımın birimi DİLİMDİR, pozisyon değil.** Kol ve sembol pozisyonun
    özellikleridir, yani bir pozisyonun tüm dilimleri aynı gruba düşer; çıkış kuralı ise
    dilimin özelliğidir. Kısmi çıkışlı bir pozisyon iki gruba birden düşer (`partial` ve
    kapatan dilimin grubu) ve iki ölçüm satırı üretir. Nakit toplamı korunur
    (Σpnl değişmez), işlem SAYISI korunmaz: grupların `trades` toplamı modelin pozisyon
    sayısından büyük olabilir. Kırılımın cevapladığı soru "hangi kural kaç kez tetikledi",
    "model kaç pozisyon açtı" değildir — ikincisi model tablosunda durur.
    """
    reason = str(row.get("exit_reason", ""))
    rule = find_tag(str(row.get("notes", "")), "exit_rule")
    return f"{reason}:{rule}" if rule else reason


# --------------------------------------------------------------------------- #
# Havuzlanmış yön karşılaştırması (projenin ana sorusu)
# --------------------------------------------------------------------------- #
def pooled_direction_stats(
    trades_by_model: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    ci_alpha: float = _NAN,
    bootstrap_samples: int = 0,
    seed: int = 0,
    reference: Any = None,
) -> dict[str, DirectionStats]:
    """Birden çok modelin işlemlerini TEK havuzda birleştirip yön bazında ölçer.

    Model tablosu "hangi model iyi" sorusunu cevaplar; bu havuz projenin asıl sorusunu:
    **short işlemler long işlemlerden daha mı başarılı?** Model başına ortalama R'lerin
    ortalamasını almak bu soruya yanlış cevap verirdi — 2 işlemlik bir model 200 işlemlik
    bir modelle eşit ağırlık alır ve sonuç, işlemlerin değil model sayısının ortalaması
    olurdu. Havuz her işleme bir oy verir.

    Havuza kimin gireceğine çağıran karar verir: referans çıpalarının (kural 15) R'si
    yoktur, havuzda işleri de yoktur. Burada filtre uygulanmaz ki modül defterin
    içeriğinden başka bir şey varsaymasın.

    Havuzun ortalama R'si projenin ANA sorusunun cevabıdır, bu yüzden güven aralığı
    burada opsiyonel bir süs değil: "short'lar long'lardan iyi" cümlesi ancak iki
    aralık ayrıştığında kurulabilir.
    """
    rows = [row for trades in trades_by_model.values() for row in trades]
    normalized = normalize_reference(reference)
    return {
        direction: direction_stats(
            rows,
            direction=direction,
            ci_alpha=ci_alpha,
            bootstrap_samples=bootstrap_samples,
            seed=seed,
            reference=normalized,
        )
        for direction in (*DIRECTIONS, TOTAL)
    }


# --------------------------------------------------------------------------- #
# Kabul çıtası (iki kapı + bir uyarı)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, kw_only=True)
class AcceptanceFlags:
    """Bir yarışmacının sonucu: İKİ KAPI ve BİR UYARI.

    **Kapılar** (`passed` = ikisi birden):

    - `sample` — **örneklem**: R'ye giren kapanmış işlem sayısı eşiğin altındaysa
      ortalama R bir ölçüm değil gürültüdür. Bu kapı olmadan iki işlemle +3R yapmış bir
      model tablonun başına oturur.
    - `edge` — **üstünlük**: ortalama R pozitif, bilgisiz kontrol grubunun ortalama
      R'sini **en az `edge_margin_r` kadar** aşıyor, farkın **bootstrap güven aralığının
      alt sınırı sıfırın üstünde** ve hesap getirisi referans çıpasını geçiyor. Dördü
      birlikte tek bir soruyu sorar: "bu sonuç sinyalden mi geliyor, yoksa piyasadan ve
      şanstan mı?" Marj olmadan kontrolü 0.01R ile geçen bir model de "geçti" sayılırdı;
      oysa çekilişin kendi gürültüsü o kadar farkı tek başına üretir.

      **Marj ve güven aralığı birbirinin yerine geçmez, ikisi de gerekir.** Marj bir
      ETKİ BÜYÜKLÜĞÜ eşiğidir ("fark yeterince büyük mü"), güven aralığı bir KESİNLİK
      eşiğidir ("fark örneklem gürültüsünden ayırt edilebiliyor mu"). 8 işlemle ölçülen
      0.40R'lik bir fark marjı rahatça geçer ama aralığı sıfırı fazlasıyla içerir;
      300 işlemle ölçülen 0.03R'lik bir fark ise aralığı sıfırın üstünde tutabilir ama
      karar verilecek bir büyüklük değildir. R dağılımı kalın kuyrukludur (stop'lu bir
      sistemde kayıplar −1R'de kümelenir, kazançlar uzun kuyruk yapar), bu yüzden
      aralık normal varsayımıyla değil YÜZDELİK BOOTSTRAP ile kurulur.

      **Kontrolün kendi örneklemi de bir kapıdır** (`control_min_trades`). Marj,
      kontrolün ortalamasına göre ölçülür; o ortalama dört işlemden geliyorsa kapı
      çalışıyormuş gibi görünürken aslında gürültüyü gürültüyle kıyaslar. Kontrol kendi
      kapısını geçmiyorsa `edge` DEĞERLENDİRİLEMEZ ve False kalır — eksik bir çıta,
      geçilmiş bir çıta gibi görünmemelidir (`_benchmark_return`'ün eksik çıpa için
      yaptığının aynısı).

    **Uyarı** (`band`, `passed`'ı ETKİLEMEZ):

    - `band` — **maliyet ölçeği** (kural 14): modelin `avg_stop_distance_pct` değeri
      yarışmacı medyanının etrafındaki bantta mı? Dışındaysa model aynı 1R'yi belirgin
      biçimde farklı notional ile taşımış, yani R başına farklı maliyet ödemiştir.
      Bu bir KUSUR DEĞİL, bir kıyas koşuludur: modelin kendi ölçümü geçerlidir, ama
      başka bir modelle yan yana konurken `cost_per_r` farkının sonucu tek başına
      açıklayıp açıklamadığı sorulmalıdır (CLAUDE.md > Rapor Kolonları). Kapı yapmak
      iki ayrı soruyu birbirine karıştırırdı: "bu model doğrulandı mı" ile "bu model
      şu modelle kıyaslanabilir mi". Bu yüzden raporda bir uyarı göstergesidir.

    Kapılar YALNIZCA yarışmacılara uygulanır; ne referans çıpası (kural 15) ne de dış
    sistem kopyası yarışmacıdır — onlara bir çıta koymak, ölçmedikleri bir yarışta not
    vermek olurdu.
    """

    model: str
    sample: bool
    edge: bool
    passed: bool
    band: bool  # UYARI göstergesi: False = bandın dışında. `passed`'a girmez.
    measured_trades: int
    min_trades: int
    avg_stop_distance_pct: float
    band_low: float
    band_high: float
    avg_r: float
    control_avg_r: float
    edge_margin_r: float
    total_return: float
    benchmark_return: float
    # Kontrolün KENDİ örneklemi: marj, kontrolün ortalamasına göre ölçülür ve o ortalama
    # da bir örneklemden gelir. Denetlenebilir olması için sayı bayrakla birlikte durur.
    control_trades: int = 0
    control_min_trades: int = 0
    # Farkın (model − kontrol) bootstrap güven aralığı. `nan` = hesaplanamadı (örneklem
    # verilmedi ya da bir taraf boş); o durumda `edge` yalnızca marja düşer ve bu
    # logger.warning ile söylenir — eksik bir çıta, geçilmiş bir çıta gibi görünmemelidir.
    edge_diff_ci_low: float = _NAN
    edge_diff_ci_high: float = _NAN
    ci_alpha: float = _NAN


def r_series(trades: Iterable[Mapping[str, Any]]) -> list[float]:
    """Bir modelin POZİSYON başına R dizisi, kapanış sırasına göre.

    `direction_stats` ile AYNI tanımdan gelir (`merge_fills` -> `r_multiple`) ve ayrı bir
    hesap yolu açmaz: kabul çıtasının bootstrap'ı ile tablodaki ortalama R aynı sayıların
    üstünde durmalıdır. İkinci bir yol bugün hizalansa bile yarın ayrışır ve aynı model
    için "ortalama R" ile "farkın güven aralığı" birbiriyle çelişen iki örneklemden
    hesaplanırdı.
    """
    return [
        r for r in (r_multiple(row) for row in merge_fills(trades)) if r is not None
    ]


def bootstrap_diff_ci(
    sample: Sequence[float],
    control: Sequence[float],
    *,
    alpha: float,
    iterations: int,
    seed: int,
) -> tuple[float, float]:
    """`ort(sample) − ort(control)` farkının yüzdelik bootstrap güven aralığı.

    **Neden bootstrap, neden t-testi değil:** R dağılımı stop'lu bir sistemde tanım
    gereği çarpıktır — kayıplar −1R civarında kümelenir, kazançlar hedef ve trailing
    yüzünden uzun kuyruk yapar. Normal varsayımına dayanan bir aralık, bu asimetride
    alt sınırı sistematik olarak yanlış yere koyar.

    **İki örneklem BAĞIMSIZ yeniden örneklenir** (eşleştirilmemiş): model ile kontrol
    aynı barlarda aynı sembollerde işlem açmaz, yani eşleştirilecek bir çift yoktur.
    Eşleştirme varsayımı, olmayan bir kovaryansı hesaba katıp aralığı yapay daraltırdı.

    **Deterministiktir:** tohum `config.yaml > random_seed`'den gelir ve çağıran taşır.
    Aynı defter aynı aralığı vermelidir; rozetin koşudan koşuya titremesi, çıtayı bir
    ölçü olmaktan çıkarıp bir çekilişe çevirirdi.

    Bir taraf boşsa ya da `iterations` sıfırsa `(nan, nan)` döner — çağıran bunu
    "değerlendirilemedi" olarak okur ve kapıyı geçmiş saymaz.
    """
    if not sample or not control or iterations <= 0:
        return (_NAN, _NAN)

    rng = random.Random(seed)
    sample_n = len(sample)
    control_n = len(control)
    diffs: list[float] = []
    for _ in range(int(iterations)):
        left = sum(sample[rng.randrange(sample_n)] for _ in range(sample_n)) / sample_n
        right = (
            sum(control[rng.randrange(control_n)] for _ in range(control_n)) / control_n
        )
        diffs.append(left - right)
    diffs.sort()
    return (_percentile(diffs, alpha / 2.0), _percentile(diffs, 1.0 - alpha / 2.0))


def bootstrap_mean_ci(
    sample: Sequence[float],
    *,
    alpha: float,
    iterations: int,
    seed: int,
) -> tuple[float, float]:
    """Tek bir örneklemin ORTALAMASININ yüzdelik bootstrap aralığı.

    `bootstrap_diff_ci` ile aynı gerekçeler geçerlidir (çarpık R dağılımı, normal
    varsayımı yok, deterministik tohum) ve aynı `_percentile` yardımcısını kullanır;
    farkı sorudur: o "fark sıfırdan ayırt edilebiliyor mu" der, bu "bu ortalamanın
    kendisi ne kadar belirsiz" der.

    **Neden gerekli:** tabloda `ort. R = −0.31` ile `ort. R = −0.01` yan yana durur ve
    n=37 ile n=230 aynı yazı tipiyle yazılır. Aralık, okuyucunun örneklem büyüklüğünü
    tabloda ARAMASINI gerektirmeden iki satırın ne kadar konuşabildiğini gösterir.

    **Bir kapı DEĞİLDİR** (kabul çıtası `passed`'ı bu sayıdan okumaz): bir okuma
    yardımıdır, tıpkı band uyarısı gibi. Kapı yapmak, `min_trades`in sorduğu soruyu
    ikinci bir eşikle tekrar sormak olurdu.

    **Tek gözlemde aralık YOKTUR** — `_stdev`in "iki işlemden azında tanımsızdır"
    sözleşmesinin aynısı. Tek bir R'nin yeniden örneklemesi her zaman kendisini verir ve
    `[+0.08, +0.08]` gibi DEJENERE bir aralık üretir: okuyucuya kıl payı bir kesinlik
    vaat eder, oysa ortada dağılım yoktur. Eşik serbest bir parametre değil, bootstrap'ın
    tanım sınırıdır.

    Örneklem iki gözlemden azsa, `iterations` sıfırsa ya da alfa tanımsızsa `(nan, nan)`.
    """
    if len(sample) < 2 or iterations <= 0 or math.isnan(alpha):
        return (_NAN, _NAN)

    rng = random.Random(seed)
    size = len(sample)
    means = sorted(
        sum(sample[rng.randrange(size)] for _ in range(size)) / size
        for _ in range(int(iterations))
    )
    return (_percentile(means, alpha / 2.0), _percentile(means, 1.0 - alpha / 2.0))


def _percentile(ordered: Sequence[float], fraction: float) -> float:
    """Sıralı dizinin `fraction` yüzdeliği (doğrusal ara değerleme)."""
    if not ordered:
        return _NAN
    if len(ordered) == 1:
        return ordered[0]
    position = min(max(fraction, 0.0), 1.0) * (len(ordered) - 1)
    lower = int(math.floor(position))
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def acceptance_flags(
    metrics: Sequence[ModelMetrics],
    *,
    min_trades: int,
    stop_band_ratio: float,
    control_model: str,
    edge_margin_r: float,
    control_min_trades: int | None = None,
    r_samples: Mapping[str, Sequence[float]] | None = None,
    ci_alpha: float = 0.05,
    bootstrap_samples: int = 0,
    seed: int = 0,
) -> list[AcceptanceFlags]:
    """Her yarışmacı için iki kabul kapısı ve bir band uyarısı. Çıpalar listeye girmez.

    Bandın çapası yarışmacıların `avg_stop_distance_pct` MEDYANIDIR, sabit bir yüzde
    değil: kural 14'ün bandı ATR katı cinsindendir, defterde ise ATR yoktur (işlem
    kapandıktan sonra "o anki ATR" geri hesaplanamaz, geriye dönük yeniden hesaplamak da
    look-ahead kapısı açardı). Medyan, aynı evrende aynı barlarda işlem yapan modellerin
    ortak volatilite ölçeğini taşır; band `medyan/√oran .. medyan×√oran` olarak kurulur,
    yani uçtan uca tam `stop_band_ratio` kadar geniştir.

    Kopya modeller (is_replica) bandın medyanına da girmez: 1R'lerini sabit teminattan
    türettikleri için stop mesafeleri yarışmacıların ortak volatilite ölçeğini taşımaz ve
    medyanı kendi ölçeklerine doğru kaydırırlardı.

    Kontrol ya da referans modeli kümede yoksa ilgili koşul değerlendirilemez ve `edge`
    geri kalan koşullara düşer — ama bu sessiz olmaz, `logger.warning` ile söylenir:
    eksik bir çıta, geçilmiş bir çıta gibi görünmemelidir.
    """
    competitors = [item for item in metrics if item.is_competitor]
    band_low, band_high = _stop_band(competitors, ratio=stop_band_ratio)
    control_avg_r = _control_avg_r(metrics, control_model)
    benchmark_return = _benchmark_return(metrics)

    control_gate = int(min_trades if control_min_trades is None else control_min_trades)
    measured_control = _control_trades(metrics, control_model)
    control_trades = measured_control or 0
    # Kümede hiç olmayan kontrol (None) koşulu DÜŞÜRÜR, kapıyı düşürmez — bkz.
    # `_control_trades`. `_control_avg_r` o durumu zaten ayrıca loglar.
    control_ready = measured_control is None or measured_control >= control_gate
    if not control_ready:
        logger.warning(
            "kontrol grubu %r kendi örneklem kapısını geçmedi (%d < %d): edge bayrağı "
            "DEĞERLENDİRİLEMEZ — %d işlemlik bir ortalamaya karşı marj ölçmek, gürültüyü "
            "gürültüyle kıyaslamaktır",
            control_model, control_trades, control_gate, control_trades,
        )

    samples = dict(r_samples) if r_samples is not None else {}
    control_sample = samples.get(control_model, ())
    if r_samples is None or bootstrap_samples <= 0:
        logger.warning(
            "R örneklemi verilmedi (ya da bootstrap_samples=0): edge bayrağı güven "
            "aralığı koşulunu değerlendiremiyor, yalnızca marja düşüyor",
        )

    return [
        _flags_for(
            item,
            min_trades=int(min_trades),
            band_low=band_low,
            band_high=band_high,
            control_avg_r=control_avg_r,
            edge_margin_r=float(edge_margin_r),
            benchmark_return=benchmark_return,
            control_trades=control_trades,
            control_gate=control_gate,
            control_ready=control_ready,
            diff_ci=bootstrap_diff_ci(
                samples.get(item.model, ()),
                control_sample,
                alpha=float(ci_alpha),
                iterations=int(bootstrap_samples),
                # Tohum model adına bağlanır: her model kendi yeniden örneklemesini alır
                # ama aynı defterde aynı aralığı üretir. Tek bir tohumu paylaşmak,
                # modellerin çekilişlerini birbirine kilitlerdi.
                seed=int(seed) ^ (hash_name(item.model) if item.model else 0),
            ),
            ci_alpha=float(ci_alpha),
        )
        for item in competitors
    ]


def hash_name(name: str) -> int:
    """Model adından deterministik tohum eki.

    `hash()` KULLANILMAZ: Python'un dize hash'i `PYTHONHASHSEED` ile koşudan koşuya
    değişir ve aynı defter iki farklı güven aralığı üretirdi (`random_seed`in sabit
    olmasının gerekçesiyle aynı).
    """
    value = 0
    for char in name:
        value = (value * 131 + ord(char)) & 0xFFFFFFFF
    return value


def _flags_for(
    item: ModelMetrics,
    *,
    min_trades: int,
    band_low: float,
    band_high: float,
    control_avg_r: float,
    edge_margin_r: float,
    benchmark_return: float,
    control_trades: int,
    control_gate: int,
    control_ready: bool,
    diff_ci: tuple[float, float],
    ci_alpha: float,
) -> AcceptanceFlags:
    measured = _measured(item)
    avg_r = item.total.avg_r
    stop_distance = item.total.avg_stop_distance_pct
    total_return = item.account.total_return

    sample = measured >= min_trades
    # Ölçülemeyen band (tek yarışmacı, hiç stop'lu işlem yok) uyarı üretmez: gösterge
    # ancak kıyaslanacak bir medyan varken anlamlıdır.
    band = (
        True
        if math.isnan(band_low) or math.isnan(stop_distance)
        else band_low <= stop_distance <= band_high
    )
    ci_low, ci_high = diff_ci
    # Kontrol kendi örneklem kapısını geçmediyse edge DEĞERLENDİRİLEMEZ. Marjı yine de
    # hesaplayıp "geçti" demek, kapıyı kontrolün gürültüsü kadar aşağı indirirdi; kontrol
    # kümede hiç yokken (control_avg_r = nan) koşulun düşmesiyle aynı statü değildir —
    # orada kıyaslanacak bir şey yoktur, burada kıyaslanacak şey henüz ölçülmemiştir.
    edge = (
        control_ready
        and not math.isnan(avg_r)
        and avg_r > 0.0
        # Marj, kontrolü ANLAMLI biçimde geçmiş modeli kıl payı önde olandan ayırır:
        # bilgisiz çekilişin kendi gürültüsü 0.01R'lik bir farkı tek başına üretebilir.
        and (math.isnan(control_avg_r) or avg_r - control_avg_r >= edge_margin_r)
        # Güven aralığı marjın YERİNE değil YANINA: marj etki büyüklüğünü, aralık
        # kesinliği sorar. Aralık hesaplanamadıysa (nan) koşul düşer ve bu çağıranda
        # logger.warning ile söylenmiştir.
        and (math.isnan(ci_low) or ci_low > 0.0)
        and (math.isnan(benchmark_return) or (
            not math.isnan(total_return) and total_return > benchmark_return
        ))
    )
    return AcceptanceFlags(
        model=item.model,
        sample=sample,
        edge=edge,
        # Band bilinçli olarak DIŞARIDA: kıyas koşuludur, kalite kapısı değil.
        passed=sample and edge,
        band=band,
        measured_trades=measured,
        min_trades=min_trades,
        avg_stop_distance_pct=stop_distance,
        band_low=band_low,
        band_high=band_high,
        avg_r=avg_r,
        control_avg_r=control_avg_r,
        edge_margin_r=edge_margin_r,
        total_return=total_return,
        benchmark_return=benchmark_return,
        control_trades=control_trades,
        control_min_trades=control_gate,
        edge_diff_ci_low=ci_low,
        edge_diff_ci_high=ci_high,
        ci_alpha=ci_alpha,
    )


def _stop_band(competitors: Sequence[ModelMetrics], *, ratio: float) -> tuple[float, float]:
    values = [
        item.total.avg_stop_distance_pct
        for item in competitors
        if not math.isnan(item.total.avg_stop_distance_pct)
    ]
    if not values or ratio <= 0.0:
        return (_NAN, _NAN)
    center = _median(values)
    half = math.sqrt(ratio)
    return (center / half, center * half)


def _control_avg_r(metrics: Sequence[ModelMetrics], control_model: str) -> float:
    for item in metrics:
        if item.model == control_model:
            return item.total.avg_r
    logger.warning(
        "kontrol grubu %r kümede yok: edge bayrağı 'kontrolü geçti mi' koşulunu "
        "değerlendiremiyor", control_model,
    )
    return _NAN


def _control_trades(metrics: Sequence[ModelMetrics], control_model: str) -> int | None:
    """Kontrol grubunun R'ye GİREN pozisyon sayısı; kümede YOKSA None.

    `_control_avg_r` ile aynı satırdan okunur ama ayrı bir soruyu cevaplar: ortalama
    "ne kadar", bu "kaç işlemden". İkincisi olmadan birincisi bir kapıya dayanak olamaz.

    **None ile 0 aynı şey DEĞİLDİR ve karışmamaları kuralın kendisidir.** Kontrol kümede
    hiç yoksa kıyaslanacak bir şey yoktur ve CLAUDE.md'nin yazdığı davranış geçerlidir:
    koşul düşer, `edge` geri kalanlara dayanır, durum `logger.warning` ile söylenir.
    Kontrol VARSA ama az işlemi varsa kıyaslanacak şey vardır, yalnızca henüz
    ölçülmemiştir — o zaman `edge` geçilmiş SAYILMAZ. İkisini tek sayıya indirmek,
    kontrolü listeden çıkarmayı kapıyı geçmenin bir yolu hâline getirirdi.
    """
    for item in metrics:
        if item.model == control_model:
            return item.total.trades - item.total.unmeasured
    return None


def _benchmark_return(metrics: Sequence[ModelMetrics]) -> float:
    """Çıpanın hesap getirisi. Birden fazla çıpa varsa EN YÜKSEĞİ alınır.

    Zemin en yüksek çıpadır: "piyasayı yendi mi" sorusuna, geçilmesi en kolay çıpayı
    seçerek cevap vermek çıtayı sessizce indirirdi.
    """
    returns = [
        item.account.total_return
        for item in metrics
        if item.is_benchmark and not math.isnan(item.account.total_return)
    ]
    if not returns:
        logger.warning(
            "kümede referans çıpası yok: edge bayrağı 'piyasayı geçti mi' koşulunu "
            "değerlendiremiyor (kural 15)",
        )
        return _NAN
    return max(returns)


# --------------------------------------------------------------------------- #
# Modeller arası getiri korelasyonu
# --------------------------------------------------------------------------- #
def return_correlation(
    equity_by_model: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    min_overlap: int = 3,
) -> dict[str, Any]:
    """Modellerin bar getirilerinin Pearson korelasyon matrisi.

    Neden ölçülüyor: on model aynı evreni aynı barlarda görüyor. Yüksek korelasyonla
    yarışan iki model bağımsız iki ölçüm değil, tek ölçümün iki kopyasıdır — tablonun
    ilk iki sırasını doldurmaları bir teyit değil, tekrardır. Matris, sıralamanın ne
    kadarının gerçekten farklı fikirlerden geldiğini gösterir.

    Kesişim PAR BAZINDA alınır, global değil: yarışmaya sonradan eklenen bir modelin kısa
    geçmişi, global kesişim kullanılsaydı TÜM çiftleri onun uzunluğuna kırpardı. Örtüşme
    `min_overlap`'in altındaysa hücre `nan`'dır (0.0 "ilişkisiz" demek olurdu); örneklem
    okunabilsin diye örtüşme sayıları da ayrıca döner.
    """
    models = sorted(equity_by_model)
    series = {model: _bar_returns(equity_by_model[model]) for model in models}

    matrix: list[list[float]] = []
    overlap: list[list[int]] = []
    for row_model in models:
        correlations: list[float] = []
        counts: list[int] = []
        for column_model in models:
            left, right = _align(series[row_model], series[column_model])
            counts.append(len(left))
            correlations.append(
                _pearson(left, right) if len(left) >= min_overlap else _NAN
            )
        matrix.append(correlations)
        overlap.append(counts)

    return {
        "models": models,
        "matrix": matrix,
        "overlap": overlap,
        "min_overlap": int(min_overlap),
    }


def _bar_returns(equity_rows: Sequence[Mapping[str, Any]]) -> dict[str, float]:
    """Zaman damgası -> o bardaki getiri. Damga anahtar, çünkü modeller farklı barda başlar."""
    points = [
        (str(row.get("ts", "")), _to_float(row.get("equity")))
        for row in equity_rows
    ]
    points = [(ts, value) for ts, value in points if ts and value is not None]
    points.sort(key=lambda item: item[0])
    return {
        ts: points[index][1] / points[index - 1][1] - 1.0
        for index, (ts, _) in enumerate(points)
        if index > 0 and points[index - 1][1] > 0.0
    }


def _align(
    left: Mapping[str, float], right: Mapping[str, float]
) -> tuple[list[float], list[float]]:
    shared = sorted(set(left) & set(right))
    return ([left[ts] for ts in shared], [right[ts] for ts in shared])


def _pearson(left: Sequence[float], right: Sequence[float]) -> float:
    """Sabit bir seri (varyans 0) için `nan`: sabitle korelasyon tanımsızdır, 0 değil."""
    mean_left = _mean(left)
    mean_right = _mean(right)
    covariance = sum(
        (a - mean_left) * (b - mean_right) for a, b in zip(left, right)
    )
    spread_left = math.sqrt(sum((a - mean_left) ** 2 for a in left))
    spread_right = math.sqrt(sum((b - mean_right) ** 2 for b in right))
    if spread_left == 0.0 or spread_right == 0.0:
        return _NAN
    return covariance / (spread_left * spread_right)


# --------------------------------------------------------------------------- #
# Rapor
# --------------------------------------------------------------------------- #
_HEADERS = ("model", "yön", "n", "ort.R", "medyan R", "topl.R", "R-Sharpe",
            "maxDD(R)", "kazanç%", "PF", "stopMes.%", "maliyet/R", "PnL(USDT)", "likid.")
_WIDTHS = (18, 6, 5, 8, 9, 8, 9, 9, 8, 7, 10, 10, 12, 7)
_TABLE_WIDTH = sum(_WIDTHS) + 2 * (len(_WIDTHS) - 1)


def format_report(metrics: Sequence[ModelMetrics], *, min_trades: int | None = None) -> str:
    """Karşılaştırma tablosu. Kolon sırası bilinçlidir: önce R, sonra USDT getirisi.

    Sıralama ortalama R'ye göredir — tabloyu toplam getiriye göre sıralamak, tam da
    ayıklamaya çalıştığımız bileşiklenme etkisini geri sokardı.

    **`min_trades` verilirse örneklem kapısını (Ö) geçemeyen yarışmacılar SIRALAMAYA
    GİRMEZ** ve ayrı bir bölümde, işlem sayısına göre dizilir. Gerekçe, rozetle tablonun
    aynı şeyi söylemesidir: kapının düştüğünü bir rozette yazıp satırı yine de "#1"
    olarak göstermek, okuyucunun ikincisini okuması demektir — ve 4 işlemlik bir ortalama
    ile 200 işlemlik bir ortalamayı aynı sütunda sıralamak zaten kapının reddettiği
    kıyastır. Satır GİZLENMEZ, çünkü base katmanının ölçütü (karar 33) tam olarak
    "model n=30'a ulaşabiliyor mu"dur; bu yüzden ayrı bölüm işlem sayısına göre sıralanır.

    Referans çıpası (kural 15) ve dış sistem kopyası sıralamaya girmez, tablonun altında
    AYRI İKİ bölümde durur: farklı boyutlandırma kuralıyla çalışan bir satırı
    yarışmacılarla aynı sütunda sıralamak, okuyucuya olmayan bir kıyas sunardı. İkisi
    de aynı bölümde toplanmaz, çünkü ölçtükleri soru farklıdır — çıpa "piyasa ne yaptı",
    kopya "dış sistem bizim varsayımlarımızla ne yapardı".
    """
    competitors = [item for item in metrics if item.is_competitor]
    benchmarks = [item for item in metrics if item.is_benchmark]
    replicas = [item for item in metrics if item.is_replica and not item.is_benchmark]

    if min_trades is None:
        ranked, unmeasured = competitors, []
    else:
        ranked = [item for item in competitors if _measured(item) >= min_trades]
        unmeasured = [item for item in competitors if _measured(item) < min_trades]

    lines = [
        "  ".join(header.rjust(width) if index else header.ljust(width)
                  for index, (header, width) in enumerate(zip(_HEADERS, _WIDTHS))),
        "-" * _TABLE_WIDTH,
    ]
    for item in sorted(ranked, key=_rank_key):
        lines.extend(_model_block(item))

    if unmeasured:
        lines.append(
            f"YETERSİZ ÖRNEKLEM (Ö kapısı: n < {min_trades}) — sıralamaya girmez; "
            "ortalama R henüz bir ölçüm değil gürültüdür"
        )
        lines.append("-" * _TABLE_WIDTH)
        # Sıralama ölçütü R DEĞİL, işlem sayısıdır: bu bölümün cevapladığı soru
        # "hangisi önde" değil, "hangisi kapıya ne kadar yakın".
        for item in sorted(unmeasured, key=lambda entry: (-_measured(entry), entry.model)):
            lines.extend(_model_block(item))

    for title, section in (
        (
            "REFERANS (yarışma dışı, kural 15) — kıyas zemini: model piyasayı yendi mi?",
            benchmarks,
        ),
        (
            "REFERANS (dış sistem) — yarışma dışı: sabit teminat/kendi kaldıracıyla koşar, "
            "1R'si yarışmacılarınkiyle aynı birim değildir",
            replicas,
        ),
    ):
        if not section:
            continue
        lines.append(title)
        lines.append("-" * _TABLE_WIDTH)
        for item in sorted(section, key=lambda entry: entry.model):
            lines.extend(_model_block(item))

    return "\n".join(lines).rstrip() + "\n"


def _measured(item: ModelMetrics) -> int:
    """R'ye GİREN pozisyon sayısı. Örneklem kapısının tek tanımı (bkz. `_flags_for`)."""
    return item.total.trades - item.total.unmeasured


def _rank_key(item: ModelMetrics) -> tuple[float, str]:
    """Ortalama R'si olmayan model sıralamanın sonuna düşer, başına değil."""
    avg_r = item.total.avg_r
    return (-avg_r if not math.isnan(avg_r) else math.inf, item.model)


def _model_block(item: ModelMetrics) -> list[str]:
    lines: list[str] = []
    for index, direction in enumerate((*DIRECTIONS, TOTAL)):
        stats = item.by_direction(direction)
        label = item.model if index == 0 else ""
        cells = (
            label.ljust(_WIDTHS[0]),
            ("TOPLAM" if direction == TOTAL else direction).rjust(_WIDTHS[1]),
            str(stats.trades).rjust(_WIDTHS[2]),
            _fmt(stats.avg_r).rjust(_WIDTHS[3]),
            _fmt(stats.median_r).rjust(_WIDTHS[4]),
            _fmt(stats.total_r).rjust(_WIDTHS[5]),
            _fmt(stats.r_sharpe).rjust(_WIDTHS[6]),
            _fmt(stats.max_drawdown_r).rjust(_WIDTHS[7]),
            _fmt(_pct(stats.win_rate), digits=1).rjust(_WIDTHS[8]),
            _fmt(stats.profit_factor).rjust(_WIDTHS[9]),
            _fmt(stats.avg_stop_distance_pct).rjust(_WIDTHS[10]),
            _fmt(stats.cost_per_r, digits=3).rjust(_WIDTHS[11]),
            _fmt(stats.pnl, digits=2).rjust(_WIDTHS[12]),
            str(stats.liquidations).rjust(_WIDTHS[13]),
        )
        lines.append("  ".join(cells))

    account = item.account
    lines.append(
        f"{'':<{_WIDTHS[0]}}  hesap: son özsermaye {_fmt(account.final_equity, digits=2)} | "
        f"getiri {_fmt(_pct(account.total_return), digits=2)}% | "
        f"maxDD {_fmt(_pct(account.max_drawdown), digits=2)}% | "
        f"Sharpe {_fmt(account.sharpe)} | {account.bars} bar"
    )
    lines.extend(_reading_lines(item))
    # Yarışma dışı satırda R'siz işlem beklenendir (çıpada stop yoktur, kopyanın kısmi
    # dolumları farklı bir birimdedir); yarışmacıda ise denetlenmesi gereken bir
    # anomalidir. Uyarıyı hepsine yazmak, gerçek uyarıyı gürültüye boğardı.
    unmeasured = item.total.unmeasured
    if unmeasured and item.is_competitor:
        lines.append(
            f"{'':<{_WIDTHS[0]}}  UYARI: {unmeasured} işlemde risk_amount yok, R'ye girmedi"
        )
    lines.append("")
    return lines


# --------------------------------------------------------------------------- #
# Yardımcılar
# --------------------------------------------------------------------------- #
def _reading_lines(item: ModelMetrics) -> list[str]:
    """Satırın OKUNMASINA yardım eden üç kuyruk: aralık, beklenti ayrışması, friksiyon.

    Üçü de tabloya KOLON olarak eklenmedi: tablo zaten on dört kolon geniştir ve
    okunabilirliği ölçümün parçasıdır (`docs/shared.css`'in tek kopya olmasıyla aynı
    gerekçe). Kuyruk satırları model bloğunun içinde durur, yani hangi modele ait
    oldukları tartışmasızdır.
    """
    total = item.total
    pad = f"{'':<{_WIDTHS[0]}}  "
    lines: list[str] = []

    if not math.isnan(total.avg_r_ci_low) or not math.isnan(total.avg_r_ci_high):
        lines.append(
            f"{pad}ort. R aralığı: [{_fmt(total.avg_r_ci_low)}, "
            f"{_fmt(total.avg_r_ci_high)}] (bootstrap; bir KAPI değil, okuma yardımı)"
        )

    # Beklenti bir ÖZDEŞLİKTİR, yeni bir metrik değil: WR×ort.kazanç + (1−WR)×ort.kayıp
    # R biriminde tam olarak ortalama R'ye eşittir (test: tests/test_metrics.py).
    # Değeri sayının kendisinde değil AYRIŞMASINDA: ortalama R'nin negatif olması
    # kazanma oranından mı, ödeme oranından mı geliyor?
    if not math.isnan(total.avg_r) and not math.isnan(total.win_rate):
        loss_rate = 1.0 - total.win_rate
        lines.append(
            f"{pad}beklenti: %{_fmt(_pct(total.win_rate), digits=1)} × "
            f"{_fmt(total.avg_win_r)}R + %{_fmt(_pct(loss_rate), digits=1)} × "
            f"{_fmt(total.avg_loss_r)}R = {_fmt(total.avg_r)}R"
        )

    # Piyasa kontrolü YÖN BAZINDA yazılır, toplamda değil: projenin ana sorusu
    # "short'lar long'lardan iyi mi" ve o soruyu kirleten şey tam olarak iki yönün
    # FARKLI piyasa penceresi görmesidir. Tek bir toplam satır bunu gizlerdi.
    for direction in (*DIRECTIONS, TOTAL):
        stats = item.by_direction(direction)
        if not stats.market_measured:
            continue
        label = ("TOPLAM" if direction == TOTAL else direction).ljust(6)
        adjusted = (
            stats.avg_r - stats.market_r
            if not (math.isnan(stats.avg_r) or math.isnan(stats.market_r))
            else _NAN
        )
        lines.append(
            f"{pad}piyasa (beta=1) {label} tailwind %{_fmt(stats.market_tailwind_pct, digits=3)} | "
            f"market_R {_fmt(stats.market_r)} | R−market_R {_fmt(adjusted)} | "
            f"{stats.market_measured}/{stats.trades} fiyatlandı"
        )

    friction = item.friction
    lines.append(
        f"{pad}friksiyon: ciro {_fmt(friction.turnover_per_day)}x/gün | "
        f"tur maliyeti %{_fmt(total.cost_pct, digits=3)} | "
        f"sürüklenme %{_fmt(friction.cost_drag_pct_per_day, digits=3)}/gün | "
        f"{_fmt(friction.trades_per_day, digits=1)} işlem/gün | "
        f"{_fmt(friction.days, digits=1)} gün"
    )
    return lines


def _to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _collect(
    rows: Iterable[Mapping[str, Any]], extract: Any
) -> list[float]:
    """Hesaplanabilen değerleri toplar; hesaplanamayanlar ortalamaya 0.0 olarak girmez."""
    return [value for value in (extract(row) for row in rows) if value is not None]


def _sum_column(rows: Iterable[Mapping[str, Any]], column: str) -> float:
    return sum(value for value in (_to_float(row.get(column)) for row in rows) if value is not None)


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else _NAN


def _median(values: Sequence[float]) -> float:
    if not values:
        return _NAN
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def _stdev(values: Sequence[float]) -> float:
    """Örneklem standart sapması; iki işlemden azında tanımsızdır."""
    if len(values) < 2:
        return _NAN
    mean = _mean(values)
    return math.sqrt(sum((value - mean) ** 2 for value in values) / (len(values) - 1))


def _ratio(numerator: float, denominator: float) -> float:
    if math.isnan(numerator) or math.isnan(denominator) or denominator == 0.0:
        return _NAN
    return numerator / denominator


def _pct(value: float) -> float:
    return value * 100.0 if not math.isnan(value) else _NAN


def _fmt(value: float, *, digits: int = 2) -> str:
    return "—" if value is None or math.isnan(value) else f"{value:.{digits}f}"
