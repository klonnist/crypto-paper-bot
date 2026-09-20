"""Strateji arayüz sözleşmesi: Strategy, Signal, Position, ExitInstruction, MarketData.

Onaylanan tasarım (bkz. CLAUDE.md > "Strateji Arayüz Sözleşmesi"):

- generate_signals: yalnızca yeni pozisyon açılışı önerir.
- manage_positions: yalnızca mevcut pozisyonlarda kapanış/kısmi çıkış önerir.
  İkisi karıştırılmaz; engine'in "bu sinyal yeni mi, mevcut pozisyona müdahale mi"
  diye tahmin yürütmesi gerekmez.
- entry_type sözleşmede "limit" olarak da yazılabilir ama v1'de yalnızca "market"
  işlenir; core/validate.py "limit" gördüğünde NotImplementedError fırlatır.
- trailing_atr yalnızca stratejinin isteğini taşır; trailing'in uygulanması
  core/engine.py'nin işidir, strateji kendi trailing mantığını yazmaz.
- take_profits bir tuple'dır, liste değil: frozen dataclass içinde mutable liste
  taşımak dondurmayı yarım bırakır ve bir stratejinin (ya da meta modelin) kendi/
  başkasının sinyalini yerinde değiştirmesine izin verirdi.
- sizing/notional_fraction yalnızca REFERANS (`is_benchmark`, kural 15) ve KOPYA
  (`is_replica`) modeller içindir. Yarışmacı modeller `sizing="risk"` kullanır ve
  boyutlarını yine core/portfolio.py belirler (kural 3/11) — bu alanlar strateji başına
  boyutlandırma yapma kapısı değildir; core/validate.py ikisi de olmayan bir modelin
  notional_fraction kullanmasını ValueError ile reddeder.
- breakeven_at_r / partial_tp / trail_giveback_pct ÜÇ AŞAMALI ÇIKIŞ YÖNETİMİDİR ve
  trailing_atr ile aynı sözleşme kuralına tabidir (kural 9): strateji yalnızca İSTEĞİNİ
  bildirir, uygulaması core/engine.py ve core/portfolio.py'dedir. Üçü de OPSİYONELDİR ve
  varsayılanları None'dır — doldurmayan model (mevcut on üç modelin hepsi) bu
  eklemeden hiçbir biçimde etkilenmez.
- ModelLimits yalnızca KOPYA modellerin kendi kaldıraç/limit kurallarını bildirdiği
  dar bir kapıdır ve kapısı yine core/validate.py'dedir; yarışmacı bir model buraya
  değer yazamaz (kural 6: limitler herkes için aynıdır).
- MarketData.funding tek oran değil zaman indeksli seridir ve as_of sözleşmede yer
  alır: "şimdi"nin tek ve açık tanımı, look-ahead yasağını (kural 12) test edilebilir
  kılar.
- Bu dosya mantık içermez, yalnızca sözleşme. Doğrulama core/validate.py'dedir.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Literal, Mapping, Sequence

import pandas as pd

Direction = Literal["long", "short"]

# Boyutlandırma modu. "risk" tüm yarışmacı modellerin tek modudur: boyut
# risk_per_trade × sermaye / |giriş − stop| (kural 11). "notional_fraction" İKİ istisnaya
# açıktır (kapı: core/validate.py):
#
#   is_benchmark=True — kural 15: stop'u olmayan bir alım-tut çıpası risk formülüne
#     sokulamaz, çünkü paydası yoktur. Kaldıraç ZORLA 1x'tir.
#   is_replica=True   — model bir DIŞ SİSTEMİN kurallarını birebir yeniden üretir ve o
#     sistemin boyutlandırması sabit teminattır, %1 risk değil. Çıpadan farkı: kopyanın
#     stop'u VARDIR (stop yönetimi kopyalanan sistemin parçasıdır), bu yüzden stop_price
#     ZORUNLUDUR — ama tabloda yine yarışmacılarla aynı sütunda sıralanmaz.
SizingMode = Literal["risk", "notional_fraction"]


@dataclass(frozen=True, kw_only=True)
class TakeProfit:
    price: float
    fraction: float  # 0 < fraction <= 1.0; bir Signal içindeki toplam <= 1.0


@dataclass(frozen=True, kw_only=True)
class PartialTakeProfit:
    """Kısmi çıkış İSTEĞİ: R cinsinden bir seviye ve kapanacak oran.

    Neden `TakeProfit` değil: `TakeProfit` bir FİYATTIR, bu ise bir R SEVİYESİDİR — ve
    ikisi aynı şey değildir. R seviyesi giriş anındaki riski (|giriş − ilk stop|) birim
    alır, yani stratejinin "kurulumum 1.5 katını verdiğinde yarısını al" isteğini
    fiyattan bağımsız ifade eder. Fiyata çevirmek core/portfolio.py'nin işidir (kural 3):
    strateji fiyatı kendisi hesaplasaydı, stop'un dolumda kaydığı (ya da kaldıraç tavanı
    yüzünden boyutun küçüldüğü) durumlarda modelin "1.5R" dediği yer gerçek 1.5R
    olmazdı.

    `fraction` BAŞLANGIÇ miktarının oranıdır (core/portfolio.py::_close ile aynı taban)
    ve 1.0 OLAMAZ: tamamı kapanıyorsa ortada "kısmi"den sonra taşınacak bir bakiye ve
    çekilecek bir stop yoktur — o bir take-profit'tir ve `take_profits` ile ifade edilir.
    """

    r: float
    fraction: float  # 0 < fraction < 1.0


@dataclass(frozen=True, kw_only=True)
class ModelLimits:
    """Bir modelin KENDİ pozisyon/kaldıraç kuralları — yalnızca `is_replica` modellere açık.

    Neden var: kopya model bir dış sistemin kurallarını birebir yeniden üretir ve o
    sistemin kaldıracı, eşzamanlı pozisyon sayısı ve portföy riski tavanı kendi
    kurallarıdır. Bunları modelin içinde uygulamak imkânsızdır — model kendi açık
    pozisyonlarını `generate_signals`ta göremez (kural 4/16) ve boyut/kaldıraç hesabı
    zaten core/portfolio.py'nindir (kural 3). Bu yüzden limitler bir BİLDİRİMDİR ve
    uygulayan yine tek yetkili yerdir.

    Neden yarışmacılara kapalı: kural 6 "izin verilen evren, maliyet ve limitler tüm
    modeller için birebir aynıdır" der. Yarışmacı bir modelin kendi limitini yazması,
    tabloda yan yana duran iki satırın farklı kurallarla koşması demekti. Kapı
    core/validate.py::validate_model'dedir.

    Alanların hiçbiri zorunlu değildir; None = "kök ayar geçerli". `max_positions` ve
    `max_per_direction` kök ayarları yalnızca DARALTIR (genişletemez): kopya kendi
    kuralını bildirir, ölçümün ortak tavanını delemez.
    """

    max_positions: int | None = None
    max_per_direction: int | None = None
    # Açık pozisyonların toplam riski (Σ adet × |giriş − ilk stop|) sermayenin bu oranını
    # aşacaksa yeni pozisyon AÇILMAZ. Kural 11'in "küçült, atlama" ilkesi burada
    # geçerli değildir: bu bir ölçüm kuralı değil, kopyalanan sistemin kendi kuralıdır
    # ve o sistem işlemi almaz. Atlama sessiz olmaz — sebep koduyla sayılır.
    max_portfolio_risk: float | None = None
    # Yalnızca sizing="notional_fraction" ile anlamlı ve yalnızca is_replica modellerde
    # geçerli. Tavanı core/validate.py'deki REPLICA_LEVERAGE_CAP'tir.
    leverage: float | None = None


@dataclass(frozen=True, kw_only=True)
class Signal:
    symbol: str
    direction: Direction
    # sizing="risk" iken zorunlu, "notional_fraction" iken None OLMALI (core/validate.py).
    stop_price: float | None = None
    sizing: SizingMode = "risk"
    # Yalnızca sizing="notional_fraction" iken anlamlı: sermayenin bu oranı kadar notional.
    notional_fraction: float | None = None
    entry_type: Literal["market", "limit"] = "market"
    take_profits: tuple[TakeProfit, ...] = ()
    trailing_atr: float | None = None
    # --- Üç aşamalı çıkış yönetimi (hepsi OPSİYONEL, varsayılan KAPALI) ---
    # Üçü de yalnızca bir İSTEKTİR; uygulaması core/engine.py (stop hareketleri) ve
    # core/portfolio.py'dedir (kısmi dolum) — kural 9'un trailing_atr için koyduğu
    # sınırın aynısı. Doldurmayan model için hiçbir yol değişmez.
    #
    # breakeven_at_r: pozisyon bu R'a ulaştığında stop GİRİŞE çekilir.
    breakeven_at_r: float | None = None
    # partial_tp: bu R'da pozisyonun `fraction` kadarı kapanır VE stop aynı anda kısmi
    # çıkış seviyesine çekilir. İkisi tek alanda durur çünkü tek bir karardır: "kârın bir
    # kısmını al, kalanı risksiz taşı".
    partial_tp: PartialTakeProfit | None = None
    # trail_giveback_pct: KISMİ ÇIKIŞTAN SONRA stop, o ana kadarki en iyi kazancın en çok
    # bu oranını geri verecek şekilde takip eder ve orijinal hedefi ASLA aşmaz.
    # trailing_atr'dan AYRI bir mekanizmadır; ikisi aynı anda kullanılamaz (validate).
    trail_giveback_pct: float | None = None
    reason: str = ""  # deftere yazılacak serbest metin


@dataclass(frozen=True, kw_only=True)
class Position:
    """manage_positions'a verilen salt okunur pozisyon görünümü."""

    symbol: str
    direction: Direction
    entry_price: float
    stop_price: float | None  # referans modellerde stop yoktur (kural 15)
    take_profits: tuple[TakeProfit, ...] = ()
    trailing_atr: float | None = None
    # Modelin kendi çıkış yönetimi isteği, geri okunabilir hâliyle. `partial_done`
    # gerçekleşmiş bir OLAYDIR (kısmi çıkış doldu mu) — modelin kendi pozisyonudur,
    # kural 4'ün izolasyonuna girmez ve `manage_positions` kararını buna dayandırabilir.
    breakeven_at_r: float | None = None
    partial_tp: PartialTakeProfit | None = None
    trail_giveback_pct: float | None = None
    partial_done: bool = False
    opened_at: pd.Timestamp


@dataclass(frozen=True, kw_only=True)
class ClosedTrade:
    """`observe_closed_trades`a verilen, modelin KENDİ kapanmış işleminin salt okunur görünümü.

    Neden sözleşmede: uyarlanabilir (bandit) modeller kendi geçmiş sonuçlarından öğrenir ve
    bu bilginin tek meşru kaynağı defterin KAPANMIŞ satırlarıdır. Açık pozisyon buraya
    hiç girmez — girse, henüz gerçekleşmemiş bir sonucu öğrenmeye katmak, kâğıt üstündeki
    kârı ölçüme sokmak olurdu (ve hangi modelin ne zaman "şanslı durduğu" görülemezdi).

    Kural 4'ün izolasyonu korunur: model yalnızca KENDİ işlemlerini görür, başka modelin
    işlemini, pozisyonunu ya da bakiyesini değil. Kural 1/7 de korunur: bu bir OKUMA
    yüzeyidir, model deftere yazmaz ve bakiye/pozisyon durumu tutmaz.

    `r_multiple` `pnl / risk_amount`tır (core/metrics.py'deki tek tanım) ve risk bilinmiyorsa
    None'dır — 0.0 değil: "ölçülemedi" ile "ölçüldü, sıfır çıktı" aynı hücreye yazılamaz.
    """

    symbol: str
    direction: Direction
    opened_at: pd.Timestamp
    closed_at: pd.Timestamp
    r_multiple: float | None
    signal_reason: str
    exit_reason: str


@dataclass(frozen=True, kw_only=True)
class ExitInstruction:
    symbol: str
    action: Literal["close", "reduce"]
    fraction: float = 1.0  # yalnızca action="reduce" için anlamlı
    reason: str = ""


@dataclass(frozen=True, kw_only=True)
class MarketData:
    ohlcv: dict[str, pd.DataFrame]  # sembol -> timeframe OHLCV, yalnızca KAPANMIŞ barlar
    btc: pd.DataFrame  # BTC referans verisi (aynı kural)
    funding: dict[str, pd.Series] = field(default_factory=dict)  # sembol -> funding geçmişi
    as_of: pd.Timestamp  # değerlendirilen son KAPANMIŞ barın zamanı (tz-aware, UTC)


class Strategy(ABC):
    name: str
    allowed_directions: list[Direction]
    is_meta: bool = False  # True ise engine'in ikinci geçişinde çalışır (CLAUDE.md kural 4)
    # True ise model bir REFERANS çıpasıdır, yarışmacı değil (CLAUDE.md kural 15):
    # sizing="notional_fraction" kullanabilir ve metrics tablosunda ayrı bölümde,
    # R'ye dayalı kolonları nan olarak raporlanır.
    is_benchmark: bool = False
    # True ise model bir DIŞ SİSTEMİN KOPYASIDIR: yarışmacı değil, ayrı bir referanstır.
    # Çıpadan (is_benchmark) farkı, ölçtüğü sorudur — çıpa "piyasa ne yaptı" der, kopya
    # "dışarıdaki şu sistem bizim maliyet/likidasyon varsayımlarımız altında ne yapardı"
    # der. Ortak yanları: ikisi de kendi boyutlandırma kuralıyla koşar, bu yüzden
    # ikisi de ortalama R sıralamasına GİRMEZ ve maliyet ölçeği kolonlarında nan alır
    # (R başına maliyet, ancak ortak risk birimiyle koşan satırlar arasında kıyaslanır).
    is_replica: bool = False
    # Yalnızca is_replica modellerde dolu olabilir (kapı: core/validate.py).
    limits: "ModelLimits | None" = None

    @abstractmethod
    def generate_signals(
        self,
        market: MarketData,
        peer_signals: Mapping[str, tuple[Signal, ...]] | None = None,
    ) -> list[Signal]:
        """Yeni pozisyon açılışı önerir. Mevcut pozisyonlara dokunmaz.

        peer_signals yalnızca is_meta=True modellere doldurulur: strateji adı ->
        o turda üretilmiş sinyallerin salt okunur kopyası. Normal modellerde None.
        """

    def manage_positions(
        self, market: MarketData, positions: list[Position]
    ) -> list[ExitInstruction]:
        """Mevcut pozisyonlarda kapanış/kısmi çıkış önerir. Varsayılan: hiçbir şey yapma."""
        return []

    def observe_closed_trades(self, trades: Sequence[ClosedTrade]) -> None:
        """Modelin KENDİ kapanmış işlemleri; `generate_signals`tan ÖNCE, turda bir kez.

        Varsayılan: hiçbir şey yapma. Motor bu kancayı yalnızca gerçekten uygulayan
        modeller için doldurur (bkz. core/engine.py) — geri kalan modeller defteri hiç
        okumaz ve davranışları değişmez.

        Liste yalnızca KAPANMIŞ işlemleri taşır (açık pozisyon asla girmez) ve kapanış
        sırasındadır. Sözleşme salt okunur: veriyi değiştirmek değil, ondan öğrenmek için.
        """
        return None

    def take_survey(self) -> Mapping[str, int] | None:
        """Son `generate_signals` çağrısının ELEME SAYIMI: sebep kodu -> sembol sayısı.

        Varsayılan `None` = bu model sayım tutmaz. Motor her bardan sonra okur ve tur
        raporuna toplar (`core/engine.py::ModelReport.survey`); oradan
        `docs/data/metrics_*.json > round.models[].survey` altına düşer.

        **Bu bir DENETİM İZİDİR, ölçüm değil** (`rejections` ve `emitted` ile aynı statü,
        kural 15). Bir modelin `signals=0` ile geçtiği tur iki bambaşka şeyin aynı
        görünümüdür: "bugün hiç kurulum yoktu" ve "sinyal modülü sessizce bozuldu". Sayım
        olmadan ikisi ancak veriyi elle çekerek ayrılır — ve sebep yalnızca koşu logunda
        dursaydı, o loglar silindiğinde cevap tamamen kaybolurdu.

        Sözleşme gereği: sayım sinyalleri, sıralarını ya da seçimi HİÇBİR biçimde
        etkilemez ve motor onu okumasa da modelin davranışı aynı kalır.
        """
        return None
