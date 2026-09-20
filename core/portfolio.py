"""Pozisyon/bakiye yönetimi ve boyutlandırmanın tek yetkili kaynağı (CLAUDE.md kural 3/11).

Parayı hareket ettiren tek yer burasıdır: boyutlandırma, marj, komisyon, kayma,
likidasyon ve stop/TP tetikleme. Stratejiler yalnızca yön ve seviye önerir; buradaki
kurallar 10 modele de birebir aynı uygulanır (kural 2/6).

Mum içi kontrol sırası — CLAUDE.md kural 13 ve modül tablosu:

    1. likidasyon (bakım marjı, mum içi high/low ile)
    2. stop
    3. kısmi çıkış (partial_tp) — varsa; aynı anda stop'u kısmi seviyeye çeker
    4. take-profit

Bir mumda stop ile TP (ya da stop ile kısmi çıkış seviyesi) birlikte aralığa giriyorsa mum
içi sıralama bilinemez; bu yüzden KÖTÜ olan (stop) gerçekleşmiş varsayılır — iyimser
varsayım her modelin sonucunu, en çok da geniş hedefli olanlarınkini, sistematik biçimde
şişirirdi. Likidasyon hepsinden önce gelir: gerçek borsada bakım marjı ihlali stop emrini
beklemez, kontrolü sonraya almak yüksek kaldıraçlı modellere gerçekte var olmayan bir
kurtulma şansı verir.

Kısmi çıkışın ÇEKTİĞİ stop o mumda değil, BİR SONRAKİ mumdan itibaren geçerlidir: o stop,
kısmi dolum gerçekleştikten sonra verilmiş YENİ bir emirdir ve mumun daha önceki
hareketleri sırasında piyasada durduğu varsayılamaz (kural 13'ün "emir bir sonraki barda
geçerlidir" ilkesi). Tersini yapmak — yeni stop'u aynı mumda da kontrol etmek — kısmi
çıkışı neredeyse her mumda anında tam çıkışa çevirir ve mekanizmayı ölçülemez kılardı.

Nakit muhasebesi (deftere birebir yansır):

    açılışta   : nakit -= marj + giriş komisyonu
    açıkken    : nakit += funding (işaretli; + alınan, - ödenen)
    kapanışta  : nakit += marj + brüt PnL - çıkış komisyonu
    likidasyon : nakit += 0  (marjın tamamı gider)

`Trade.pnl` işlemin nakde net etkisidir (marj gidiş-dönüşü hariç, likidasyonda marj
kaybı dâhil): kapanan işlemlerin `pnl` toplamı, bakiyedeki toplam değişime eşittir.

Boyutlandırmanın iki modu vardır ve ikisi de BURADA uygulanır (kural 3 delinmez):

    "risk"             : boyut = risk_per_trade × sermaye / |giriş − stop|   (kural 11)
    "notional_fraction": boyut = fraction × sermaye / giriş; kaldıraç çıpada 1x,
                         kopya modelde modelin bildirdiği sabit kaldıraç

İkincisi yalnızca `is_benchmark=True` (referans çıpası, kural 15) ve `is_replica=True`
(dış sistem kopyası) modellere açıktır; kapısı core/validate.py'dedir. Çıpada kaldıraç
1x'e SABİTLENİR — çıpanın işi "piyasa ne yaptı"yı ölçmek, onu kaldıraçla büyütmek değil.
Kopyada ise kaldıraç kopyalanan sistemin kuralıdır ve `ModelLimits.leverage` ile bildirilir
(tavanı core/validate.py::REPLICA_LEVERAGE_CAP): kaldıracı 1x'e zorlamak, kopyanın
likidasyon riskini yok etmek ve onu haksız biçimde iyi göstermek olurdu.

Stop'suz pozisyonun `stop_price`/`initial_stop_price` alanı None kalır ve deftere BOŞ
yazılır — 0.0 yazmak "stop girişin %100 altındaydı" demek olurdu ve `risk_amount`
üzerinden R'yi, `avg_stop_distance_pct` üzerinden maliyet ölçeğini uydururdu.

`ModelLimits` (yalnızca kopya modeller) kök limitleri DARALTIR, genişletmez: modelin
kendi yön kotası ve portföy riski tavanı burada uygulanır, çünkü model kendi açık
pozisyonlarını göremez (kural 4/16) ve boyut/marj hesabı zaten burasının işidir.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Literal, Mapping, Sequence

import pandas as pd

from core.config import get_setting
from core.tags import format_tags
from strategies.base import (
    Direction,
    ModelLimits,
    PartialTakeProfit,
    Position,
    SizingMode,
    TakeProfit,
)

logger = logging.getLogger(__name__)

# "partial" ayrı bir sebeptir, "tp"nin bir türü değil: kısmi çıkış aynı anda stop'u da
# hareket ettirir (bkz. Portfolio._process_position) ve defterde ayırt edilemezse
# "modelin hedefi doldu" ile "yönetim kuralı devreye girdi" aynı satıra çöker — oysa
# 14 ve 15 numaralı modellerin ölçtüğü şey tam olarak ikincisinin katkısıdır.
ExitReason = Literal["liquidation", "stop", "partial", "tp", "signal"]

# Açılış reddinin SEBEP KODU. Serbest metin gerekçe (OpenResult.rejected) insan içindir ve
# sembol adı taşıdığı için toplanamaz; bu kod ise tur raporunda sayılabilir ve zaman içinde
# karşılaştırılabilir. Ayrım şart: "sinyal üretildi ama işlem açılmadı" iki bambaşka şeyin
# aynı görünümüdür — beklenen bir tekrar (referans modelin zaten taşıdığı pozisyon) ile
# gerçek bir boyutlandırma arızası (sıfır boyut, yetersiz nakit). Kod olmadan ikisi aylar
# sonra ayırt edilemez.
RejectReason = Literal[
    "duplicate_position",    # aynı sembol+yönde zaten açık pozisyon var (BEKLENEN olabilir)
    "max_positions",         # eşzamanlı pozisyon kotası dolu
    "max_short_positions",   # short kotası dolu
    "gap_past_stop",         # bar stop'un ötesinde açtı, emir doldurulmadı
    "zero_size",             # boyutlandırma sıfır adet üretti (ARIZA sinyali)
    "insufficient_cash",     # nakit marj + komisyonu karşılamıyor (ARIZA sinyali)
    "max_direction_positions",  # modelin KENDİ yön kotası dolu (ModelLimits, kopya modeller)
    "portfolio_risk_cap",       # açık toplam risk modelin KENDİ tavanını aşardı (ModelLimits)
]

# Boyutlandırmanın "çalıştı ama sıfır çıktı" hâlleri. Bunlar beklenen bir tekrar değildir:
# sermaye tükenmiş ya da formül beklenmedik bir sayı üretmiştir — log seviyesi de bunu
# yansıtır (INFO değil WARNING), çünkü bakılması gereken tek grup budur.
SIZING_FAILURES: frozenset[str] = frozenset({"zero_size", "insufficient_cash"})

# Kalan miktar başlangıcın bu oranının altına düşerse pozisyon kapanmış sayılır:
# kısmi çıkışların kayan nokta artığı "0.0000000001 adet" pozisyon bırakmasın.
_DUST_RATIO = 1e-9


@dataclass(frozen=True, kw_only=True)
class Bar:
    """Tek bir mumun motorun ihtiyaç duyduğu dar görünümü."""

    open: float
    high: float
    low: float
    close: float

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "Bar":
        return cls(
            open=float(row["open"]),
            high=float(row["high"]),
            low=float(row["low"]),
            close=float(row["close"]),
        )


@dataclass(frozen=True, kw_only=True)
class Sizing:
    qty: float
    notional: float
    margin: float
    leverage: float
    clipped: bool
    note: str


@dataclass(frozen=True, kw_only=True)
class Trade:
    """Kapanan işlem (kısmi çıkışta yalnızca kapanan dilim) — trades.csv'nin bir satırı."""

    strategy: str
    symbol: str
    direction: Direction
    opened_at: pd.Timestamp
    closed_at: pd.Timestamp
    entry_price: float
    exit_price: float
    qty: float
    notional: float
    # Referans modellerde (kural 15) stop yoktur: ikisi de None kalır ve deftere boş yazılır.
    stop_price: float | None
    risk_amount: float | None
    leverage: float
    margin: float
    fee: float
    slippage_cost: float
    funding: float
    pnl: float
    exit_reason: ExitReason
    signal_reason: str
    notes: str

    def as_row(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy,
            "symbol": self.symbol,
            "direction": self.direction,
            "opened_at": self.opened_at.isoformat(),
            "closed_at": self.closed_at.isoformat(),
            "entry_price": self.entry_price,
            "exit_price": self.exit_price,
            "qty": self.qty,
            "notional": self.notional,
            "stop_price": self.stop_price,
            "risk_amount": self.risk_amount,
            "leverage": self.leverage,
            "margin": self.margin,
            "fee": self.fee,
            "slippage_cost": self.slippage_cost,
            "funding": self.funding,
            "pnl": self.pnl,
            "exit_reason": self.exit_reason,
            "signal_reason": self.signal_reason,
            "notes": self.notes,
        }


@dataclass
class OpenPosition:
    """Açık pozisyonun tam (yazılabilir) hâli. `strategies/base.Position` bunun kırpılmış görünümüdür.

    `margin`, `entry_fee` ve `funding` kısmi çıkışlarda miktarla orantılı azalır; bu sayede
    `margin / qty` (dolayısıyla likidasyon fiyatı) kısmi çıkıştan etkilenmez.
    """

    symbol: str
    direction: Direction
    qty: float
    initial_qty: float
    entry_price: float
    stop_price: float | None
    initial_stop_price: float | None
    opened_at: pd.Timestamp
    margin: float
    leverage: float
    entry_fee: float
    # Kayma, dolum fiyatının içine gömülü olduğu için deftere ayrıca yazılmazsa görünmez
    # kalır; cost_per_r (CLAUDE.md > Rapor Kolonları) komisyon + KAYMA istediğinden giriş
    # kayması burada USDT olarak taşınır ve kısmi çıkışlarda orantılı azalır.
    entry_slippage: float
    liq_price: float
    high_water: float
    low_water: float
    take_profits: tuple[TakeProfit, ...] = ()
    trailing_atr: float | None = None
    # --- Üç aşamalı çıkış yönetimi (CLAUDE.md > Strateji Arayüz Sözleşmesi) ---
    # Modelin AÇILIŞTA bildirdiği istek; hiçbiri sonradan değişmez. Kısmi çıkış
    # gerçekleştiğinde yalnızca `partial_done` True olur — o bir olaydır, bir istek değil.
    breakeven_at_r: float | None = None
    partial_tp: PartialTakeProfit | None = None
    trail_giveback_pct: float | None = None
    partial_done: bool = False
    # Stop'u EN SON hangi kuralın hareket ettirdiği ("breakeven" | "giveback" |
    # "trailing_atr" | "partial"; boş = hiç hareket etmedi, ilk stop duruyor). Defterde
    # `exit_reason` yalnızca "stop" der; oysa takip eden stop'un aldığı bir işlem ile ilk
    # stop'un aldığı işlem iki ayrı sonuçtur ve modeller 13/14/15'in ölçtüğü şey tam olarak
    # bu yönetimin katkısıdır. Sonradan geri hesaplanamaz (defterde yalnızca İLK stop
    # yazılıdır), bu yüzden hareket anında saklanır ve kapanışta `notes` kuyruğuna
    # `exit_rule=` etiketiyle düşer.
    stop_rule: str = ""
    # Sinyalin EN UZAK hedefi. Takip eden stop bunu asla aşamaz (sözleşme): aşsaydı stop
    # hedefin ötesine geçer, hedef hiç dolmaz ve pozisyon her zaman stop'la kapanırdı —
    # yani model "hedefe ulaştım" diyemez hâle gelirdi. Hedef dolduğunda take_profits'ten
    # düşüldüğü için tavan ayrıca saklanır.
    final_target_price: float | None = None
    funding: float = 0.0
    reason: str = ""
    notes: str = ""

    def view(self) -> Position:
        """Stratejiye verilen salt okunur görünüm — miktar/marj bilinçli olarak yoktur (kural 3)."""
        return Position(
            symbol=self.symbol,
            direction=self.direction,
            entry_price=self.entry_price,
            stop_price=self.stop_price,
            take_profits=self.take_profits,
            trailing_atr=self.trailing_atr,
            breakeven_at_r=self.breakeven_at_r,
            partial_tp=self.partial_tp,
            trail_giveback_pct=self.trail_giveback_pct,
            partial_done=self.partial_done,
            opened_at=self.opened_at,
        )

    @property
    def r_distance(self) -> float | None:
        """1R'nin FİYAT karşılığı: |giriş − İLK stop|.

        Payda her zaman İLK stop'tur, yürüyen stop değil (CLAUDE.md > Rapor Kolonları):
        R giriş anında üstlenilen risktir. Takip eden stop paydayı da küçültseydi, iyi
        giden bir işlemin "2R" dediği yer her barda başka bir fiyat olurdu.
        """
        if self.initial_stop_price is None:
            return None
        distance = abs(self.entry_price - self.initial_stop_price)
        return distance if distance > 0.0 else None

    def price_at_r(self, r: float) -> float | None:
        """Pozisyonun LEHİNE `r` kadar R'lik seviyenin fiyatı; R tanımsızsa None."""
        distance = self.r_distance
        if distance is None:
            return None
        sign = 1.0 if self.direction == "long" else -1.0
        return self.entry_price + sign * r * distance

    def favorable_excursion_r(self) -> float | None:
        """Açılıştan bu yana görülen EN İYİ hareketin R cinsinden büyüklüğü.

        Ölçü `high_water`/`low_water`dur, kapanış değil: breakeven ve takip eden stop
        "pozisyon şu kadar kâra ULAŞTI mı" sorusuna cevap verir, "şu an kârda mı"ya değil.
        """
        distance = self.r_distance
        if distance is None:
            return None
        peak = (
            self.high_water - self.entry_price
            if self.direction == "long"
            else self.entry_price - self.low_water
        )
        return peak / distance

    def as_state(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "direction": self.direction,
            "qty": self.qty,
            "initial_qty": self.initial_qty,
            "entry_price": self.entry_price,
            "stop_price": self.stop_price,
            "initial_stop_price": self.initial_stop_price,
            "opened_at": self.opened_at.isoformat(),
            "margin": self.margin,
            "leverage": self.leverage,
            "entry_fee": self.entry_fee,
            "entry_slippage": self.entry_slippage,
            "liq_price": self.liq_price,
            "high_water": self.high_water,
            "low_water": self.low_water,
            "take_profits": [
                {"price": tp.price, "fraction": tp.fraction} for tp in self.take_profits
            ],
            "trailing_atr": self.trailing_atr,
            "breakeven_at_r": self.breakeven_at_r,
            "partial_tp": partial_tp_to_state(self.partial_tp),
            "trail_giveback_pct": self.trail_giveback_pct,
            "partial_done": self.partial_done,
            "stop_rule": self.stop_rule,
            "final_target_price": self.final_target_price,
            "funding": self.funding,
            "reason": self.reason,
            "notes": self.notes,
        }

    @classmethod
    def from_state(cls, payload: Mapping[str, Any]) -> "OpenPosition":
        return cls(
            symbol=str(payload["symbol"]),
            direction=str(payload["direction"]),  # type: ignore[arg-type]
            qty=float(payload["qty"]),
            initial_qty=float(payload["initial_qty"]),
            entry_price=float(payload["entry_price"]),
            stop_price=_opt_float(payload["stop_price"]),
            initial_stop_price=_opt_float(payload["initial_stop_price"]),
            opened_at=_to_utc(payload["opened_at"]),
            margin=float(payload["margin"]),
            leverage=float(payload["leverage"]),
            entry_fee=float(payload["entry_fee"]),
            entry_slippage=float(payload.get("entry_slippage", 0.0)),
            liq_price=float(payload["liq_price"]),
            high_water=float(payload["high_water"]),
            low_water=float(payload["low_water"]),
            take_profits=tuple(
                TakeProfit(price=float(tp["price"]), fraction=float(tp["fraction"]))
                for tp in payload.get("take_profits", ())
            ),
            trailing_atr=(
                None if payload.get("trailing_atr") is None else float(payload["trailing_atr"])
            ),
            # Eski defterlerde bu alanlar yoktu: yokluk "yönetim kapalı" demektir ve
            # o modellerin davranışı bu eklemeden etkilenmez.
            breakeven_at_r=_opt_float(payload.get("breakeven_at_r")),
            partial_tp=partial_tp_from_state(payload.get("partial_tp")),
            trail_giveback_pct=_opt_float(payload.get("trail_giveback_pct")),
            partial_done=bool(payload.get("partial_done", False)),
            # Alanı olmayan eski satır "stop hiç hareket etmedi" okunur: uydurmak yerine
            # boş bırakmak, etiketi olmayan bir işlemi etiketlenmiş gibi göstermez.
            stop_rule=str(payload.get("stop_rule", "") or ""),
            final_target_price=_opt_float(payload.get("final_target_price")),
            funding=float(payload.get("funding", 0.0)),
            reason=str(payload.get("reason", "")),
            notes=str(payload.get("notes", "")),
        )


@dataclass(frozen=True, kw_only=True)
class OpenResult:
    """Açılış denemesinin sonucu. `rejected` doluysa pozisyon açılmadı.

    `rejected` insan için serbest metindir (sembol/fiyat taşır, toplanamaz); `reason_code`
    ise tur raporunda sayılabilen sabit kategoridir. İkisi birlikte döner çünkü log satırı
    ayrıntı ister, denetim izi ise sayılabilirlik.
    """

    position: OpenPosition | None = None
    rejected: str = ""
    reason_code: RejectReason | None = None


@dataclass
class Account:
    """Bir stratejinin izole sanal hesabı (kural 4/6)."""

    model: str
    initial_capital: float
    cash: float
    positions: list[OpenPosition] = field(default_factory=list)

    def find(self, symbol: str, direction: Direction) -> OpenPosition | None:
        for position in self.positions:
            if position.symbol == symbol and position.direction == direction:
                return position
        return None


# --------------------------------------------------------------------------- #
# Saf kurallar: boyutlandırma ve likidasyon fiyatı
# --------------------------------------------------------------------------- #
def size_position(
    *,
    equity: float,
    free_cash: float,
    entry_price: float,
    stop_price: float,
    risk_per_trade: float,
    leverage_cap: float,
) -> Sizing:
    """CLAUDE.md kural 11: boyut = (risk_per_trade × sermaye) / |giriş − stop|.

    Sermaye tanımı: `equity` hesabın toplam değeridir (nakit + marj + gerçekleşmemiş PnL),
    yani riske atılan %1 her zaman hesabın %1'idir — açık pozisyon sayısına göre sessizce
    değişmez. Kaldıraç tavanı ise `free_cash` üzerinden uygulanır: pozisyonun marjı eldeki
    nakitten fazla olamaz, dolayısıyla hesabın toplam notional'ı da `leverage_cap × equity`
    sınırında kalır.

    Kaldıraç bir hedef değil sonuçtur: yalnızca gereken notional eldeki nakdi aşarsa
    devreye girer. `leverage_cap` aşılacaksa işlem ATLANMAZ, pozisyon tavana sığacak
    şekilde küçültülür (atlamak, geniş stop kullanan modellerin işlem sayısını sessizce
    düşürüp tam da ölçtüğümüz karşılaştırmayı bozardı).
    """
    distance = abs(entry_price - stop_price)
    if distance <= 0.0:
        raise ValueError("stop giriş fiyatına eşit: boyutlandırma sıfıra bölünürdü")
    if entry_price <= 0.0:
        raise ValueError(f"geçersiz giriş fiyatı: {entry_price}")
    if equity <= 0.0 or free_cash <= 0.0:
        return Sizing(qty=0.0, notional=0.0, margin=0.0, leverage=0.0, clipped=False,
                      note="sermaye/nakit kalmadı")

    qty = (risk_per_trade * equity) / distance
    notional = qty * entry_price
    leverage = 1.0 if notional <= free_cash else notional / free_cash

    if leverage <= leverage_cap:
        margin = min(notional / leverage, free_cash)
        return Sizing(qty=qty, notional=notional, margin=margin, leverage=leverage,
                      clipped=False, note="")

    capped_notional = leverage_cap * free_cash
    scale = capped_notional / notional
    note = (
        f"boyut leverage_cap={leverage_cap:g} nedeniyle {scale:.4f} oranında kırpıldı "
        f"(gereken kaldıraç {leverage:.2f})"
    )
    return Sizing(
        qty=qty * scale,
        notional=capped_notional,
        margin=free_cash,
        leverage=float(leverage_cap),
        clipped=True,
        note=note,
    )


def size_notional_fraction(
    *,
    equity: float,
    free_cash: float,
    entry_price: float,
    fraction: float,
    leverage: float = 1.0,
) -> Sizing:
    """CLAUDE.md kural 15: boyut = (fraction × sermaye) / giriş, marj = notional / kaldıraç.

    Bu mod yalnızca `is_benchmark=True` referans çıpalarına ve `is_replica=True` kopya
    modellere açıktır (kapı: core/validate.py).

    **Çıpada `leverage` 1.0'dır ve öyle kalmalıdır:** kaldıraç bir sonuç bile değil,
    sabittir — referans çıpasının işi "piyasa ne yaptı" sorusuna cevap vermek, kaldıraçla
    o cevabı büyütmek değil. 1x'te marj = notional olur ve `liquidation_price` pratikte
    ulaşılamaz bir seviye üretir; alım-tut çıpasının likide olması ölçtüğü şeyi yok ederdi.

    **Kopyada `leverage` kopyalanan sistemin kuralıdır** (`ModelLimits.leverage`, tavanı
    core/validate.py::REPLICA_LEVERAGE_CAP). Marj notional'ın kaldıraca bölümüdür, yani
    "sabit teminat × kaldıraç = notional" kuralı doğrudan bu iki alanla ifade edilir.
    Likidasyon modellemesi burada DEĞİŞMEZ: yüksek kaldıraçta likidasyon gerçek bir
    risktir ve onu kapatmak kopyayı haksız biçimde iyi gösterirdi (bkz. docs/decisions.md).

    `free_cash` yine tavandır: marj eldeki nakdi aşamaz.
    """
    if entry_price <= 0.0:
        raise ValueError(f"geçersiz giriş fiyatı: {entry_price}")
    if not 0.0 < fraction <= 1.0:
        raise ValueError(f"notional_fraction 0 ile 1.0 arasında olmalı: {fraction}")
    if leverage <= 0.0:
        raise ValueError(f"kaldıraç pozitif olmalı: {leverage}")
    if equity <= 0.0 or free_cash <= 0.0:
        return Sizing(qty=0.0, notional=0.0, margin=0.0, leverage=0.0, clipped=False,
                      note="sermaye/nakit kalmadı")

    wanted = fraction * equity
    notional = min(wanted, free_cash * leverage)
    clipped = notional < wanted
    note = (
        f"boyut nakde sığsın diye {notional / wanted:.4f} oranında kırpıldı "
        f"(istenen notional {wanted:.2f}, nakit {free_cash:.2f}, kaldıraç {leverage:g}x)"
        if clipped
        else ""
    )
    return Sizing(
        qty=notional / entry_price,
        notional=notional,
        margin=notional / leverage,
        leverage=float(leverage),
        clipped=clipped,
        note=note,
    )


def liquidation_price(
    *,
    direction: Direction,
    entry_price: float,
    qty: float,
    margin: float,
    maintenance_margin: float,
) -> float:
    """Pozisyon özsermayesinin (marj + gerçekleşmemiş PnL) bakım marjına indiği fiyat.

    Eşik giriş notional'ı üzerinden hesaplanır: mum içi mark fiyatına göre yeniden
    hesaplamak her barda farklı bir eşik üretir ve aynı senaryo iki koşuda farklı
    sonuç verebilir — tekrarlanabilirlik (random_seed ilkesi) bundan önce gelir.

    Kaldıraçsız (marj = notional) pozisyonda sonuç sıfırın yakınına düşer, yani pratikte
    likidasyon olmaz; kaldıraç arttıkça eşik giriş fiyatına yaklaşır.
    """
    if qty <= 0.0:
        raise ValueError("likidasyon fiyatı için miktar pozitif olmalı")
    buffer = margin / qty
    if direction == "long":
        return entry_price * (1.0 + maintenance_margin) - buffer
    return entry_price * (1.0 - maintenance_margin) + buffer


# --------------------------------------------------------------------------- #
# Portföy
# --------------------------------------------------------------------------- #
class Portfolio:
    """Her strateji için izole hesap durumu tutar ve tüm para kurallarını uygular."""

    def __init__(self, config: Mapping[str, Any]) -> None:
        config_dict = dict(config)
        self.initial_capital = float(get_setting(config_dict, "initial_capital"))
        self.risk_per_trade = float(get_setting(config_dict, "risk_per_trade"))
        self.leverage_cap = float(get_setting(config_dict, "leverage_cap"))
        self.max_positions = int(get_setting(config_dict, "max_positions"))
        self.max_short_positions = int(get_setting(config_dict, "max_short_positions"))
        self.fee_rate = float(get_setting(config_dict, "fee_rate"))
        self.slippage_base = float(get_setting(config_dict, "slippage_base"))
        self.slippage_short_stop = float(get_setting(config_dict, "slippage_short_stop"))
        self.maintenance_margin = float(get_setting(config_dict, "maintenance_margin"))
        self._accounts: dict[str, Account] = {}

    # ------------------------------------------------------------------ #
    # Hesap durumu
    # ------------------------------------------------------------------ #
    def account(self, model: str) -> Account:
        account = self._accounts.get(model)
        if account is None:
            account = Account(model=model, initial_capital=self.initial_capital,
                              cash=self.initial_capital)
            self._accounts[model] = account
        return account

    def cash(self, model: str) -> float:
        return self.account(model).cash

    def positions(self, model: str) -> tuple[OpenPosition, ...]:
        return tuple(self.account(model).positions)

    def position_views(self, model: str) -> list[Position]:
        """`Strategy.manage_positions`a verilecek salt okunur görünümler."""
        return [position.view() for position in self.account(model).positions]

    def margin_used(self, model: str) -> float:
        return sum(position.margin for position in self.account(model).positions)

    def open_risk(self, model: str) -> float:
        """Açık pozisyonların toplam AÇILIŞ riski: Σ kalan adet × |giriş − ilk stop|.

        Payda yine İLK stop'tur (kapanan işlemlerin `risk_amount`'ı ile aynı tanım), ki
        "portföy riski" ile deftere yazılan R aynı birimi konuşsun. Stop'suz pozisyon
        (referans çıpası, kural 15) sıfır katkı verir: ölçülemeyen bir riski uydurmak,
        tavanı sembolden sembole kayan bir sayıya bağlardı.
        """
        return sum(
            position.qty * abs(position.entry_price - position.initial_stop_price)
            for position in self.account(model).positions
            if position.initial_stop_price is not None
        )

    def concentration(self, model: str, marks: Mapping[str, float]) -> dict[str, float]:
        """Açık pozisyonların YOĞUNLAŞMASI: net/brüt maruziyet ve en büyük sembol payı.

        **Bu bir ÖLÇÜMDÜR, bir kural DEĞİL** — hiçbir sinyal bu sayılara göre elenmez ve
        hiçbir pozisyon boyutu onlara göre değişmez (seans ve kayıp serisi kırılımlarıyla
        aynı statü). Gerekçe: "sinyal ≠ emir; korelasyon ve net beta tavanı gerekir"
        önerisi makul görünüyor ama bugün ölçülen bir şeye dayanmıyor — modellerin
        gerçekten yoğunlaşıp yoğunlaşmadığını söyleyen bir sayı yok. Tavan koymak ise
        BEDAVA değil: 13 sembollük bir kripto evreninde pozisyonlar birbirine yüksek
        korelasyonludur, yani bir korelasyon tavanı `max_positions` kotasını (5) pratikte
        1-2'ye indirir ve `acceptance.min_trades` (30) kapısına zaten zor ulaşan bir
        katmanda ölçümü durdurur. Kural 11'in "atlamak işlem sayısını sessizce düşürür"
        itirazının aynısı, daha sert hâli.

        Kaba bir portföy katmanı zaten VAR (`max_positions`, `max_short_positions`,
        kopyanın `max_portfolio_risk`i); eksik olan, o katmanın yetip yetmediğini
        söyleyecek sayıydı.

        Döndürülen alanlar (hepsi özsermayenin katı; pozisyon yoksa hepsi 0.0):

        - `net_exposure`   — (long notional − short notional) / özsermaye. Yönlü
          maruziyet: kripto evreninde bu sayı kabaca BTC betasının vekilidir.
        - `gross_exposure` — (long + short) / özsermaye. Kaldıracın gerçekleşen hâli.
        - `top_symbol_share` — en büyük tek sembolün brüt içindeki payı. 1.0 "tüm risk
          tek sembolde", 1/n "eşit dağılmış" demektir.

        Notional GÜNCEL fiyattan ölçülür, girişten değil: sorulan şey "şu an ne kadar
        maruzuz", "ne kadar maruz kalmıştık" değil.
        """
        account = self.account(model)
        equity = self.equity(model, marks)
        if not account.positions or equity <= 0.0:
            return {"net_exposure": 0.0, "gross_exposure": 0.0, "top_symbol_share": 0.0}

        by_symbol: dict[str, float] = {}
        net = 0.0
        for position in account.positions:
            notional = position.qty * _mark(position, marks)
            by_symbol[position.symbol] = by_symbol.get(position.symbol, 0.0) + notional
            net += notional if position.direction == "long" else -notional

        gross = sum(by_symbol.values())
        return {
            "net_exposure": net / equity,
            "gross_exposure": gross / equity,
            "top_symbol_share": (max(by_symbol.values()) / gross) if gross > 0.0 else 0.0,
        }

    def unrealized_pnl(self, model: str, marks: Mapping[str, float]) -> float:
        return sum(
            _gross_pnl(position, _mark(position, marks), position.qty)
            for position in self.account(model).positions
        )

    def equity(self, model: str, marks: Mapping[str, float]) -> float:
        account = self.account(model)
        return account.cash + self.margin_used(model) + self.unrealized_pnl(model, marks)

    def load_state(self, model: str, state: Mapping[str, Any]) -> None:
        self._accounts[model] = Account(
            model=model,
            initial_capital=float(state.get("initial_capital", self.initial_capital)),
            cash=float(state["cash"]),
            positions=[OpenPosition.from_state(item) for item in state.get("positions", ())],
        )

    def to_state(self, model: str) -> dict[str, Any]:
        account = self.account(model)
        return {
            "initial_capital": account.initial_capital,
            "cash": account.cash,
            "positions": [position.as_state() for position in account.positions],
        }

    # ------------------------------------------------------------------ #
    # Dolum fiyatı: komisyon ve kayma
    # ------------------------------------------------------------------ #
    def fill_price(
        self,
        *,
        direction: Direction,
        reference_price: float,
        side: Literal["entry", "exit"],
        is_stop: bool = False,
    ) -> float:
        """Referans fiyata kaymayı DAİMA aleyhte uygular.

        `slippage_base` yönden bağımsız olarak her dolumda (long/short giriş, çıkış, TP)
        geçerlidir; `slippage_short_stop` yalnızca short stop dolumlarında onun yerine
        geçer. Short girişlere/TP'lere kayma uygulamamak, projenin ana sorusunu (short'lar
        daha mı başarılı) shortlar lehine bozardı; tek asimetri config'in açıkça istediğidir.
        """
        slippage = (
            self.slippage_short_stop
            if is_stop and direction == "short"
            else self.slippage_base
        )
        adverse = 1.0 if (direction == "long") == (side == "entry") else -1.0
        return reference_price * (1.0 + adverse * slippage)

    # ------------------------------------------------------------------ #
    # Açılış
    # ------------------------------------------------------------------ #
    def open_position(
        self,
        model: str,
        *,
        symbol: str,
        direction: Direction,
        stop_price: float | None,
        reference_price: float,
        ts: pd.Timestamp,
        marks: Mapping[str, float],
        sizing_mode: SizingMode = "risk",
        notional_fraction: float | None = None,
        take_profits: Sequence[TakeProfit] = (),
        trailing_atr: float | None = None,
        breakeven_at_r: float | None = None,
        partial_tp: PartialTakeProfit | None = None,
        trail_giveback_pct: float | None = None,
        limits: ModelLimits | None = None,
        reason: str = "",
    ) -> OpenResult:
        """Bir sonraki barın açılışından pozisyon açar (kural 13); reddedilirse gerekçe döner."""
        account = self.account(model)
        # `sizing_mode="notional_fraction"` + stop ARTIK geçerli bir şekildir: referans
        # çıpasında stop yoktur (kural 15) ama dış sistem kopyasında vardır ve stop
        # yönetimi kopyalanan sistemin parçasıdır. İkisini ayıran bilgi modelin
        # bayrağıdır (`is_benchmark` / `is_replica`) ve burada YOKTUR — bu modül defterle
        # parayı bilir, model sınıflarını değil. Kapı tek yerdedir: core/validate.py.
        if sizing_mode == "risk" and stop_price is None:
            raise ValueError('sizing_mode="risk" için stop_price zorunludur')

        if account.find(symbol, direction) is not None:
            return OpenResult(
                rejected=f"{symbol} üzerinde zaten açık {direction} pozisyon var",
                reason_code="duplicate_position",
            )
        # Modelin KENDİ kotası kök kotayı yalnızca daraltabilir (ModelLimits sözleşmesi).
        max_positions = self.max_positions
        if limits is not None and limits.max_positions is not None:
            max_positions = min(max_positions, int(limits.max_positions))
        if len(account.positions) >= max_positions:
            return OpenResult(
                rejected=f"max_positions={max_positions} dolu",
                reason_code="max_positions",
            )
        if direction == "short":
            open_shorts = sum(1 for p in account.positions if p.direction == "short")
            if open_shorts >= self.max_short_positions:
                return OpenResult(
                    rejected=f"max_short_positions={self.max_short_positions} dolu",
                    reason_code="max_short_positions",
                )
        if limits is not None and limits.max_per_direction is not None:
            same_side = sum(1 for p in account.positions if p.direction == direction)
            if same_side >= int(limits.max_per_direction):
                return OpenResult(
                    rejected=(
                        f"modelin {direction} kotası dolu "
                        f"(ModelLimits.max_per_direction={limits.max_per_direction})"
                    ),
                    reason_code="max_direction_positions",
                )

        entry_price = self.fill_price(
            direction=direction, reference_price=reference_price, side="entry"
        )
        # Sinyal önceki barın kapanışına göre üretildi; bir sonraki bar stop'un ötesinde
        # açtıysa pozisyon doğduğu anda stoplanmış olurdu — böyle bir emir doldurulmaz.
        # Stop'suz referans pozisyonda (kural 15) böyle bir boşluk tanımsızdır.
        if stop_price is not None:
            if direction == "long" and stop_price >= entry_price:
                return OpenResult(
                    rejected=f"boşluklu açılış: stop {stop_price} >= dolum {entry_price}",
                    reason_code="gap_past_stop",
                )
            if direction == "short" and stop_price <= entry_price:
                return OpenResult(
                    rejected=f"boşluklu açılış: stop {stop_price} <= dolum {entry_price}",
                    reason_code="gap_past_stop",
                )

        equity = self.equity(model, marks)
        if sizing_mode == "notional_fraction":
            if notional_fraction is None:
                raise ValueError('sizing_mode="notional_fraction" için notional_fraction zorunludur')
            sizing = size_notional_fraction(
                equity=equity,
                free_cash=account.cash,
                entry_price=entry_price,
                fraction=notional_fraction,
                # Kaldıraç yalnızca modelin bildirdiği kadardır; bildirilmemişse 1x
                # (çıpanın kuralı). Kök `leverage_cap` bu modda hiç devreye girmez:
                # o, risk boyutlandırmasının tavanıdır.
                leverage=1.0 if limits is None or limits.leverage is None else float(limits.leverage),
            )
        else:
            assert stop_price is not None  # yukarıdaki kapı garanti eder
            sizing = size_position(
                equity=equity,
                free_cash=account.cash,
                entry_price=entry_price,
                stop_price=stop_price,
                risk_per_trade=self.risk_per_trade,
                leverage_cap=self.leverage_cap,
            )
        if sizing.qty <= 0.0:
            return OpenResult(
                rejected=f"boyut sıfır ({sizing.note or 'yetersiz sermaye'})",
                reason_code="zero_size",
            )

        qty, notional, margin = sizing.qty, sizing.notional, sizing.margin
        fee = self.fee_rate * notional
        notes = sizing.note

        # Portföy riski tavanı (yalnızca kopya modeller, ModelLimits). Kural 11'in
        # "küçült, atlama" ilkesi burada GEÇERLİ DEĞİLDİR: bu bir ölçüm kuralı değil,
        # kopyalanan sistemin kendi kuralıdır ve o sistem işlemi hiç almaz. Atlama yine
        # sessiz olmaz — sebep koduyla sayılır ve tur raporunda durur (kural 15).
        if limits is not None and limits.max_portfolio_risk is not None and stop_price is not None:
            open_risk = self.open_risk(model)
            new_risk = qty * abs(entry_price - stop_price)
            allowed = float(limits.max_portfolio_risk) * equity
            if open_risk + new_risk > allowed:
                return OpenResult(
                    rejected=(
                        f"portföy riski tavanı aşılırdı: açık {open_risk:.2f} + yeni "
                        f"{new_risk:.2f} > %{float(limits.max_portfolio_risk) * 100:g} × "
                        f"{equity:.2f} = {allowed:.2f}"
                    ),
                    reason_code="portfolio_risk_cap",
                )

        # Marj + komisyon nakdi aşamaz. Her büyüklük miktarla doğrusal olduğu için
        # tek bir ölçekle tam olarak nakde sığdırılır (yine kırpma, atlama değil).
        cost = margin + fee
        if cost > account.cash:
            scale = account.cash / cost
            qty *= scale
            notional *= scale
            margin *= scale
            fee *= scale
            notes = _join_notes(notes, f"boyut komisyon+marj nakde sığsın diye {scale:.4f} oranında kırpıldı")
        if qty <= 0.0:
            return OpenResult(
                rejected="nakit marj ve komisyonu karşılamıyor",
                reason_code="insufficient_cash",
            )

        position = OpenPosition(
            symbol=symbol,
            direction=direction,
            qty=qty,
            initial_qty=qty,
            entry_price=entry_price,
            stop_price=stop_price,
            initial_stop_price=stop_price,
            opened_at=ts,
            margin=margin,
            leverage=sizing.leverage,
            entry_fee=fee,
            entry_slippage=abs(entry_price - reference_price) * qty,
            liq_price=liquidation_price(
                direction=direction,
                entry_price=entry_price,
                qty=qty,
                margin=margin,
                maintenance_margin=self.maintenance_margin,
            ),
            high_water=entry_price,
            low_water=entry_price,
            take_profits=_ordered_take_profits(direction, take_profits),
            trailing_atr=trailing_atr,
            breakeven_at_r=breakeven_at_r,
            partial_tp=partial_tp,
            trail_giveback_pct=trail_giveback_pct,
            final_target_price=_final_target(direction, take_profits),
            reason=reason,
            notes=notes,
        )
        account.cash -= margin + fee
        account.positions.append(position)
        if sizing.clipped:
            logger.info("%s %s %s: %s", model, symbol, direction, sizing.note)
        return OpenResult(position=position)

    # ------------------------------------------------------------------ #
    # Bar işleme: likidasyon -> stop -> TP
    # ------------------------------------------------------------------ #
    def process_bar(
        self,
        model: str,
        *,
        ts: pd.Timestamp,
        bars: Mapping[str, Bar],
        on_unchecked: Callable[[str], None] | None = None,
        on_stop_exit: Callable[[bool], None] | None = None,
    ) -> list[Trade]:
        """Açık pozisyonları tek bir mum boyunca ilerletir ve kapananların kaydını döndürür.

        `on_unchecked`, barı olmayan her AÇIK POZİSYON için bir kez çağrılır (sembol adıyla).
        Çağıran bunu tur raporuna sayar; bkz. aşağıdaki dal.

        `on_stop_exit`, stop'la kapanan her pozisyon için bir kez çağrılır ve argümanı
        şudur: **aynı mumun aralığı lehte bir seviyeye (hedef ya da kısmi çıkış) DE
        değiyor muydu?** Kural 13 böyle bir mumda kötü olanın (stop) gerçekleştiğini
        VARSAYAR — mum içi sıralama bilinemez — ve bu varsayım muhafazakârdır, yani
        sonuçları aşağı çeker. Varsayımın BEDELİ bugüne kadar hiç ölçülmedi: "modeller
        kaybediyor" sonucunun ne kadarı sinyalden, ne kadarı bu varsayımdan geliyor
        bilinmiyor. Sayaç tam olarak o payı ölçer.

        **Bu bir DENETİM İZİDİR, bir kural değil** (`rejections` / `survey` ile aynı
        statü): hiçbir dolumu, fiyatı ya da sırayı değiştirmez; yalnızca sayar. Varsayımın
        kendisini oynatmak ayrı bir karardır ve canlı deftere DEĞİL, backtest'in duyarlılık
        koşusuna aittir — defterin kuralı tek olmalıdır.
        """
        account = self.account(model)
        trades: list[Trade] = []

        for position in list(account.positions):
            bar = bars.get(position.symbol)
            if bar is None:
                # Sembol o tur evrende görünmüyorsa (gecikmiş/durdurulmuş) pozisyona
                # dokunulmaz: elimizde olmayan mumla stop tetiklemek uydurma olurdu.
                #
                # Ama bu bar bir daha GERİ GELMEZ: `core/engine.py` `last_processed_bar`ı
                # yine de ilerletir, yani o mumdaki stop/TP/likidasyon kontrolü kalıcı
                # olarak yapılmamış olur. Kayıp veriden doğuyor (mum elimizde yok) ve
                # telafi edilemez — telafi edilebilecek tek şey SESSİZLİĞİDİR. Uyarı logda
                # kalırsa yalnızca o koşunun logunu açan görür; sayı tur raporuna düşerse
                # `missing_bars` gibi denetlenebilir olur ve "hiç olmuyor" ile "sürekli
                # oluyor" ayırt edilebilir (kural 15'in sebep kodu mantığı).
                logger.warning(
                    "%s %s: %s barı yok, pozisyon bu barda kontrol edilmedi", model, position.symbol, ts
                )
                if on_unchecked is not None:
                    on_unchecked(position.symbol)
                continue
            trades.extend(self._process_position(
                account, position, bar=bar, ts=ts, on_stop_exit=on_stop_exit
            ))

        return trades

    def _process_position(
        self,
        account: Account,
        position: OpenPosition,
        *,
        bar: Bar,
        ts: pd.Timestamp,
        on_stop_exit: Callable[[bool], None] | None = None,
    ) -> list[Trade]:
        long = position.direction == "long"

        # 1) Likidasyon — stop'tan ÖNCE. Bakım marjı ihlali gerçek borsada stop emrini
        #    beklemez; likide olan pozisyon stop'a hiç ulaşmaz (CLAUDE.md kural 13).
        if (long and bar.low <= position.liq_price) or (not long and bar.high >= position.liq_price):
            # Likidasyonda dolum kaymasız varsayılır: pozisyonun değeri zaten sıfırlanmıştır,
            # üstüne kayma yazmak kaybı marjın ötesine taşırdı.
            return [self._close(account, position, exit_price=position.liq_price,
                                exit_reference=position.liq_price, ts=ts,
                                fraction_of_initial=1.0, exit_reason="liquidation")]

        # 2) Stop. Mum stop'un ötesinde AÇTIYSA dolum stop'ta değil açılışta gerçekleşir
        #    (boşluk); iki fiyattan aleyhte olanı seçilir, üstüne kayma uygulanır.
        #    Stop'suz referans pozisyonda (kural 15) bu adım tümden atlanır.
        stop = position.stop_price
        if stop is not None and ((long and bar.low <= stop) or (not long and bar.high >= stop)):
            # Sayım dolumdan ÖNCE yapılır: `_close` pozisyonun hedeflerini tüketir ve
            # sonrasında "bu mumda hedef de aralıktaydı" sorusu artık sorulamaz.
            if on_stop_exit is not None:
                on_stop_exit(_favourable_level_in_range(position, bar))
            reference = min(stop, bar.open) if long else max(stop, bar.open)
            exit_price = self.fill_price(
                direction=position.direction, reference_price=reference, side="exit", is_stop=True
            )
            return [self._close(account, position, exit_price=exit_price,
                                exit_reference=reference, ts=ts,
                                fraction_of_initial=1.0, exit_reason="stop")]

        trades: list[Trade] = []

        # 3) Kısmi çıkış (partial_tp). Stop'tan SONRA, TP'den ÖNCE: aynı mumda hem stop hem
        #    kısmi seviye aralığa giriyorsa mum içi sıralama bilinemez ve kötü olan (stop)
        #    gerçekleşmiş varsayılır — yukarıdaki dal zaten dönmüştür.
        #
        #    Kısmi dolumla birlikte stop, kısmi çıkış SEVİYESİNE (kaymasız referans fiyata)
        #    çekilir. Kayan dolum fiyatını kullanmak, stop'u modelin hiç istemediği bir
        #    yere koyup mekanizmayı kayma varsayımına bağlardı. Yeni stop bu mumda bir daha
        #    KONTROL EDİLMEZ (bkz. modül docstring'i): kısmi dolumdan sonra verilmiş bir
        #    emrin, mumun daha önceki hareketleri sırasında piyasada durduğu varsayılamaz.
        partial = position.partial_tp
        if partial is not None and not position.partial_done:
            trigger = position.price_at_r(partial.r)
            touched = (
                trigger is not None
                and (bar.high >= trigger if long else bar.low <= trigger)
            )
            if trigger is not None and touched:
                exit_price = self.fill_price(
                    direction=position.direction, reference_price=trigger, side="exit"
                )
                position.partial_done = True
                trades.append(
                    self._close(account, position, exit_price=exit_price,
                                exit_reference=trigger, ts=ts,
                                fraction_of_initial=partial.fraction, exit_reason="partial")
                )
                if position.qty > 0.0:
                    _tighten_stop(position, trigger, rule="partial")

        # 4) Take-profit. Buraya yalnızca stop AYNI mumda tetiklenmediyse gelinir: stop ve TP
        #    aynı mumun aralığındaysa mum içi sıralama bilinemeyeceği için kötü olan (stop)
        #    gerçekleşmiş varsayılır ve yukarıdaki dal döner.
        for take_profit in list(position.take_profits):
            touched = bar.high >= take_profit.price if long else bar.low <= take_profit.price
            if not touched:
                continue
            # Limit emri kendi fiyatından dolar; mum TP'nin ötesinde açtıysa oluşan lehte
            # boşluk kâr yazılmaz (iyimser varsayımdan kaçınma).
            exit_price = self.fill_price(
                direction=position.direction, reference_price=take_profit.price, side="exit"
            )
            position.take_profits = tuple(tp for tp in position.take_profits if tp is not take_profit)
            trades.append(
                self._close(account, position, exit_price=exit_price,
                            exit_reference=take_profit.price, ts=ts,
                            fraction_of_initial=take_profit.fraction, exit_reason="tp")
            )
            if position.qty <= 0.0:
                break

        if position.qty > 0.0:
            position.high_water = max(position.high_water, bar.high)
            position.low_water = min(position.low_water, bar.low)
        return trades

    def close_position(
        self,
        model: str,
        *,
        symbol: str,
        direction: Direction,
        reference_price: float,
        ts: pd.Timestamp,
        fraction: float = 1.0,
        exit_reason: ExitReason = "signal",
        exit_rule: str = "",
    ) -> Trade | None:
        """Strateji talimatıyla (manage_positions) kapatma/kısmi çıkış.

        `exit_rule` talimatın KENDİ etiketidir (ör. `time_stop`): `exit_reason` bu yolda
        her zaman "signal" olur ve zaman stop'u ile başka bir strateji çıkışı defterde
        ayırt edilemez kalırdı — oysa scalp katmanının zaman stop'u ölçülen bir kuraldır.
        """
        account = self.account(model)
        position = account.find(symbol, direction)
        if position is None:
            return None
        exit_price = self.fill_price(
            direction=direction, reference_price=reference_price, side="exit"
        )
        return self._close(account, position, exit_price=exit_price,
                           exit_reference=reference_price, ts=ts,
                           fraction_of_initial=fraction, exit_reason=exit_reason,
                           exit_rule=exit_rule)

    def _close(
        self,
        account: Account,
        position: OpenPosition,
        *,
        exit_price: float,
        exit_reference: float,
        ts: pd.Timestamp,
        fraction_of_initial: float,
        exit_reason: ExitReason,
        exit_rule: str = "",
    ) -> Trade:
        """Pozisyonun `fraction_of_initial` dilimini kapatır ve nakit etkisini işler.

        Oranlar BAŞLANGIÇ miktarı üzerindendir: `TakeProfit.fraction` toplamı bir sinyal
        içinde 1.0'ı aşamaz (sözleşme), yani hedefler baştaki boyutun dilimlerini ifade
        eder. `ExitInstruction.fraction` da aynı tabanı kullanır ki iki yol aynı anlama
        gelsin.
        """
        qty = min(position.initial_qty * fraction_of_initial, position.qty)
        if exit_reason == "liquidation":
            qty = position.qty
        share = qty / position.qty if position.qty > 0.0 else 0.0

        margin_part = position.margin * share
        entry_fee_part = position.entry_fee * share
        entry_slippage_part = position.entry_slippage * share
        funding_part = position.funding * share
        gross = _gross_pnl(position, exit_price, qty)

        if exit_reason == "liquidation":
            # Marjın tamamı gider: borsaya geri dönen nakit yoktur, ayrıca çıkış komisyonu
            # da yazılmaz (pozisyonun değeri zaten sıfırlanmıştır).
            exit_fee = 0.0
            exit_slippage = 0.0
            cash_back = 0.0
            pnl = -(margin_part + entry_fee_part) + funding_part
        else:
            exit_fee = self.fee_rate * qty * exit_price
            exit_slippage = abs(exit_price - exit_reference) * qty
            cash_back = margin_part + gross - exit_fee
            pnl = gross - entry_fee_part - exit_fee + funding_part

        account.cash += cash_back
        position.qty -= qty
        position.margin -= margin_part
        position.entry_fee -= entry_fee_part
        position.entry_slippage -= entry_slippage_part
        position.funding -= funding_part
        if position.qty <= position.initial_qty * _DUST_RATIO:
            account.positions.remove(position)

        return Trade(
            strategy=account.model,
            symbol=position.symbol,
            direction=position.direction,
            opened_at=position.opened_at,
            closed_at=ts,
            entry_price=position.entry_price,
            exit_price=exit_price,
            qty=qty,
            notional=qty * position.entry_price,
            stop_price=position.initial_stop_price,
            # R'nin paydası: bu dilim için AÇILIŞTA riske edilen tutar. İlk stop kullanılır —
            # trailing stop sonradan kısaldığında R'nin tabanı değişirse aynı işlem sonradan
            # daha başarılı görünürdü. Kaldıraç tavanına takılıp küçülen pozisyonda da payda
            # gerçekten riske edilen tutardır, modelin niyeti değil.
            risk_amount=(
                None
                if position.initial_stop_price is None
                else qty * abs(position.entry_price - position.initial_stop_price)
            ),
            leverage=position.leverage,
            margin=margin_part,
            fee=entry_fee_part + exit_fee,
            slippage_cost=entry_slippage_part + exit_slippage,
            funding=funding_part,
            pnl=pnl,
            exit_reason=exit_reason,
            signal_reason=position.reason,
            notes=_exit_notes(position, exit_reason=exit_reason, exit_rule=exit_rule),
        )

    # ------------------------------------------------------------------ #
    # Funding ve trailing stop
    # ------------------------------------------------------------------ #
    def apply_funding(self, model: str, charges: Iterable[Any]) -> float:
        """core/funding.py'nin ürettiği tahakkukları nakde ve pozisyona işler.

        Tutarın işareti funding modülünde belirlenir (+ alınan, - ödenen); burada yalnızca
        nakit hareketi yapılır — maliyet kuralının iki yerde durması, iki yerde bozulması
        demektir (kural 2).
        """
        account = self.account(model)
        total = 0.0
        for charge in charges:
            position = account.find(charge.symbol, charge.direction)
            if position is None:
                continue
            position.funding += charge.amount
            account.cash += charge.amount
            total += charge.amount
        return total

    def set_stop_price(
        self,
        model: str,
        *,
        symbol: str,
        direction: Direction,
        stop_price: float,
        rule: str = "",
    ) -> bool:
        """Stop'u yalnızca SIKILAŞTIRIR (trailing mantığı core/engine.py'de).

        Gevşeme yönünde hareket, zarardaki bir pozisyonun stop'unu kaçırıp riski sessizce
        büyütmek olurdu; hesabın değişmezi olarak burada engellenir.

        `rule` hareketi YAPAN kuralın adıdır (core/engine.py verir) ve pozisyonda saklanır;
        stop'la kapanan işlemin `notes` kuyruğuna `exit_rule=` etiketi olarak düşer.
        """
        position = self.account(model).find(symbol, direction)
        if position is None:
            return False
        # Stop'suz referans pozisyona (kural 15) stop takmak, modelin sözleşmesini
        # ("alıp tut") sessizce değiştirmek olurdu — `_tighten_stop` onu da reddeder.
        return _tighten_stop(position, stop_price, rule=rule)


# --------------------------------------------------------------------------- #
# Yardımcılar
# --------------------------------------------------------------------------- #
def _exit_notes(
    position: OpenPosition, *, exit_reason: ExitReason, exit_rule: str
) -> str:
    """Pozisyonun notlarına, varsa çıkışın ALT SEBEBİNİ `exit_rule=` etiketiyle ekler.

    `exit_reason` beş kaba koddur (kural 13); rapor ise "stop" ile "takip eden stop'un
    aldığı işlem"i ve "strateji çıkışı" ile "zaman stop'u"nu ayırmak zorundadır — modeller
    13/14/15'in ölçtüğü şey tam olarak yönetimin katkısıdır. Ayrım sonradan geri
    hesaplanamaz: defterde yalnızca İLK stop yazılıdır, stop'un sonradan nereye çekildiği
    yalnızca kapanış anında bilinir. Etiket `notes` kolonuna düşer, yeni bir kolon
    açılmaz — defter şeması değişirse eski satırlar okunamaz hâle gelirdi
    (core/ledger.py::_assert_header).
    """
    rule = exit_rule
    if not rule and exit_reason == "stop":
        # Stop hiç hareket etmediyse etiket YAZILMAZ: "ilk stop aldı" etiketin yokluğudur,
        # uydurma bir `exit_rule=initial` değil.
        rule = position.stop_rule
    if not rule:
        return position.notes
    return format_tags(position.notes, exit_rule=rule)


def _gross_pnl(position: OpenPosition, price: float, qty: float) -> float:
    sign = 1.0 if position.direction == "long" else -1.0
    return sign * qty * (price - position.entry_price)


def _mark(position: OpenPosition, marks: Mapping[str, float]) -> float:
    # Fiyatı olmayan sembol için giriş fiyatı kullanılır: bilgi yokken pozisyonu kâr ya da
    # zararda göstermek, boyutlandırmanın dayandığı sermayeyi uydurmak olurdu.
    return float(marks.get(position.symbol, position.entry_price))


def _favourable_level_in_range(position: OpenPosition, bar: Bar) -> bool:
    """Bu mumun aralığı pozisyonun LEHİNE bir seviyeye değiyor mu (hedef ya da kısmi)?

    Kural 13'ün "kötü olan gerçekleşmiş varsayılır" kuralının ne sıklıkta BAĞLADIĞINI
    ölçmek için: stop'la kapanan bir pozisyonun mumu hedefe de değmişse, o işlemin sonucu
    bir piyasa gerçeği değil bir SIRALAMA VARSAYIMIDIR.

    Kısmi çıkış seviyesi de sayılır: o da lehte bir seviyedir ve aynı mumda stop'a
    öncelik verilmesi onu da yutmuştur (bkz. `_process_position` 3. adım).

    Zaten dolmuş kısmi (`partial_done`) sayılmaz: o seviye bu mumda değil daha önce
    geçilmiştir ve burada ölçülen şey BU mumun belirsizliğidir.
    """
    long = position.direction == "long"
    levels = [take_profit.price for take_profit in position.take_profits]

    partial = position.partial_tp
    if partial is not None and not position.partial_done:
        trigger = position.price_at_r(partial.r)
        if trigger is not None:
            levels.append(trigger)

    return any(
        bar.high >= level if long else bar.low <= level
        for level in levels
    )


def _tighten_stop(position: OpenPosition, stop_price: float, *, rule: str = "") -> bool:
    """Stop'u YALNIZCA sıkıştırır (Portfolio.set_stop_price ile aynı değişmez).

    Ayrı bir fonksiyon, çünkü kısmi çıkış stop'u pozisyon nesnesi elde ikenken hareket
    ettirir; `set_stop_price` ise model adı + sembol ile arar. İkisi aynı kuralı
    uygulamalı: gevşeme yönünde hareket, zarardaki bir pozisyonun riskini sessizce
    büyütmek olurdu.

    `rule` yalnızca stop GERÇEKTEN hareket ettiğinde yazılır: hareket etmeyen bir
    denemenin kuralını saklamak, işlemi hiç uygulanmamış bir yönetimle etiketlerdi.
    """
    if position.stop_price is None:
        return False
    tightened = (
        max(position.stop_price, stop_price)
        if position.direction == "long"
        else min(position.stop_price, stop_price)
    )
    if tightened == position.stop_price:
        return False
    position.stop_price = tightened
    if rule:
        position.stop_rule = rule
    return True


def _final_target(direction: Direction, take_profits: Sequence[TakeProfit]) -> float | None:
    """Sinyalin EN UZAK hedefi — takip eden stop'un aşamayacağı tavan."""
    if not take_profits:
        return None
    prices = [tp.price for tp in take_profits]
    return min(prices) if direction == "short" else max(prices)


def partial_tp_to_state(partial: PartialTakeProfit | None) -> dict[str, float] | None:
    """Kısmi çıkış isteğinin defter gösterimi. TEK kopya: aynı istek hem açık pozisyonda
    (OpenPosition) hem bekleyen emirde (core/engine.PendingOrder) saklanır ve iki ayrı
    biçimlendirme, bir gün birinin diğerinin yazdığını okuyamaması demekti."""
    if partial is None:
        return None
    return {"r": partial.r, "fraction": partial.fraction}


def partial_tp_from_state(payload: Any) -> PartialTakeProfit | None:
    """`partial_tp_to_state`in tersi. Alanı olmayan eski satır "yönetim kapalı" okunur."""
    if not isinstance(payload, Mapping):
        return None
    return PartialTakeProfit(r=float(payload["r"]), fraction=float(payload["fraction"]))


def _ordered_take_profits(
    direction: Direction, take_profits: Sequence[TakeProfit]
) -> tuple[TakeProfit, ...]:
    """Hedefler girişe en yakından uzağa sıralanır: aynı mumda birden fazlası dolarsa sıra bellidir."""
    return tuple(sorted(take_profits, key=lambda tp: tp.price, reverse=direction == "short"))


def _join_notes(*notes: str) -> str:
    return "; ".join(note for note in notes if note)


def _opt_float(value: Any) -> float | None:
    """Defterde None ya da boş yazılmış stop alanlarını geri okur (kural 15)."""
    return None if value is None or value == "" else float(value)


def _to_utc(value: Any) -> pd.Timestamp:
    stamp = pd.Timestamp(value)
    return stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")
