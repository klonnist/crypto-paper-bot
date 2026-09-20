"""Dashboard yükü: `docs/data/metrics.json`'un tablo DIŞINDA kalan bölümleri.

`core/metrics.py` "model ne kadar iyi" sorusunu cevaplar ve sayı üretir. Burası o
sayıların yanına, tek bir sayfada okunabilmeleri için gereken BAĞLAMI koyar: hangi
pozisyonlar hâlâ açık, son işlemler hangi gerekçeyle kapandı, özsermaye eğrileri nereden
geçti, son 24 saatte ne oldu. İkisi bilinçli olarak ayrı modüldür — metrics salt
okunur bir ÖLÇÜM modülüdür ve bir sunum katmanının ihtiyaçları (kaç satır gösterilecek,
eğri kaç noktaya seyreltilecek) oraya sızarsa ölçümün tanımı sunum kararlarına bağlanır.

Bu modül de salt okunurdur: defteri ve anlık görüntüyü okur, hiçbir şey yazmaz ve hiçbir
şey hesaplamaz ki `core/portfolio.py` zaten hesaplamış olsun. Açık pozisyonun güncel
PnL'i tek istisnadır ve bilinçli olarak portfolio'nun kapanış formülüyle AYNI parçalardan
kurulur (brüt fiyat farkı − giriş komisyonu + funding); çıkış maliyeti dâhil değildir,
çünkü pozisyon henüz kapanmamıştır ve kapanış fiyatı bilinmez. Dashboard'da bu ayrım
etiketle görünür.

Yüke giren her şey `docs/data/metrics.json`'da durur: sayfa statiktir (GitHub Pages,
build adımı yok) ve defteri kendisi okuyamaz.
"""

from __future__ import annotations

import logging
import math
from dataclasses import asdict
from typing import Any, Mapping, Sequence

import pandas as pd

from core.config import get_setting
from core.ledger import Ledger
from core.metrics import (
    ModelMetrics,
    acceptance_flags,
    annotate_loss_streak,
    arm_of,
    breakdown,
    exit_rule_of,
    loss_streak_of,
    pooled_direction_stats,
    r_series,
    return_correlation,
    session_of,
    symbol_of,
)
# Yoğunlaşma hesabı burada YAZILMAZ, Portfolio'dan çağrılır (kural 7): bu modül çizer,
# hesaplamaz — ikinci bir maruziyet hesabı, dashboard'un motorun bilmediği bir sayı
# göstermesi demekti.
from core.portfolio import Portfolio
from core.tags import find_tag
from strategies.base import MarketData

logger = logging.getLogger(__name__)

# Son N işlem ve eğri seyreltmesi ÖLÇÜM sabiti değil sunum sabitidir, bu yüzden
# config.yaml'da değil burada durur: config.yaml "tüm modeller için birebir aynı
# ölçüm koşulu" sözleşmesidir (kural 6), kaç satır çizildiği o sözleşmenin parçası değil.
RECENT_TRADE_LIMIT = 20
# Model detay görünümü (dashboard'ın ikinci seviyesi) modelin KENDİ geçmişini sayfalı
# gösterir, bu yüzden 20 satırlık ortak akış yetmez. Tamamı da taşınmaz: defter
# append-only büyür ve JSON her turda baştan yazılır — denetim izi `ledgers/` altındadır,
# sayfa onun son penceresini çizer.
MODEL_TRADE_LIMIT = 100
EQUITY_MAX_POINTS = 500  # eğri başına; koşu aylarca sürdüğünde JSON sınırsız büyümesin
ACTIVITY_HOURS = 24


def build_dashboard(
    metrics: Sequence[ModelMetrics],
    *,
    ledger: Ledger,
    config: Mapping[str, Any],
    market: MarketData,
    model_trade_limit: int = MODEL_TRADE_LIMIT,
    breakdowns: Sequence[str] = (),
) -> dict[str, Any]:
    """`docs/data/metrics.json`'a eklenen dashboard bölümleri.

    Havuz (`pooled`) ve kabul bayrakları YALNIZCA yarışmacılardan hesaplanır (ölçüt tek
    yerde: `ModelMetrics.is_competitor`). Referans çıpasının R'si yoktur (kural 15) ve
    kopya modelin R'si başka bir birimdedir (sabit teminat); ikisini de havuza katmak,
    projenin ana sorusunu (short işlemler daha mı başarılı) farklı boyutlandırmalarla
    açılmış işlemlerin ortalamasına bağlamak olurdu. Korelasyon matrisi ise ikisini DE
    içerir — orada ölçülen R değil bar getirisidir ve "modeller piyasadan/dış sistemden
    ne kadar ayrışıyor" sorusunun cevabı tam olarak o karşılaştırmayı gerektirir.

    `model_trade_limit` ve `breakdowns` KATMAN ayarlarıdır (core/layers.py): 15 dakikalık
    katman günde 96 tur koşar ve JSON her turda commit edilir — sayfanın çizdiği pencere
    orada daha dar tutulur (son 50 işlem). Kırılımlar da katmana bağlıdır: kol kırılımının
    yalnızca scalp modellerinde bir karşılığı vardır, 4 saatlik modellerde kol yoktur.
    """
    config_dict = dict(config)
    competitors = [item.model for item in metrics if item.is_competitor]
    models = [item.model for item in metrics]

    trades = {model: ledger.read_trades(model) for model in models}
    equity = {model: ledger.read_equity(model) for model in models}
    marks = marks_from_market(market)

    control_model = str(get_setting(config_dict, "acceptance.control_model"))
    ci_alpha = float(get_setting(config_dict, "acceptance.edge_ci_alpha"))
    bootstrap_samples = int(get_setting(config_dict, "acceptance.bootstrap_samples"))
    # Havuzun aralığı projenin ANA sorusunu okunur kılar: "short'lar long'lardan iyi"
    # ancak iki aralık ayrıştığında söylenebilir. Tohum model adına değil havuza
    # bağlanır (havuz tek bir kümedir), alfa ise model tablosuyla aynı anahtardan gelir.
    pooled = pooled_direction_stats(
        {model: trades[model] for model in competitors},
        ci_alpha=ci_alpha,
        bootstrap_samples=bootstrap_samples,
        seed=int(get_setting(config_dict, "random_seed")),
        # Havuz, ana sorunun okunduğu yer: long ↔ short farkının ne kadarının
        # piyasadan geldiği tam burada görünmeli (bkz. core/metrics.py::_market_context).
        reference=market.btc.get("close"),
    )
    # R örneklemleri kabul çıtasının bootstrap'ı için: aynı `merge_fills` -> `r_multiple`
    # yolundan gelir (core/metrics.py::r_series), yani tablodaki ortalama R ile aralığın
    # altındaki sayılar AYNI kümedir. Kontrol yarışmacı listesinde olmasa bile eklenir:
    # farkın öteki tarafı odur.
    sample_models = {*competitors, control_model} & set(trades)
    r_samples = {model: r_series(trades[model]) for model in sample_models}
    flags = acceptance_flags(
        metrics,
        min_trades=int(get_setting(config_dict, "acceptance.min_trades")),
        stop_band_ratio=float(get_setting(config_dict, "acceptance.stop_band_ratio")),
        control_model=control_model,
        edge_margin_r=float(get_setting(config_dict, "acceptance.edge_margin_r")),
        control_min_trades=int(get_setting(config_dict, "acceptance.control_min_trades")),
        r_samples=r_samples,
        ci_alpha=ci_alpha,
        bootstrap_samples=bootstrap_samples,
        seed=int(get_setting(config_dict, "random_seed")),
    )
    positions = open_positions(models, ledger=ledger, marks=marks)

    return {
        "pooled": {
            "models": competitors,
            "directions": {
                direction: asdict(stats) for direction, stats in pooled.items()
            },
        },
        "acceptance": {
            "control_model": control_model,
            "min_trades": int(get_setting(config_dict, "acceptance.min_trades")),
            "control_min_trades": int(
                get_setting(config_dict, "acceptance.control_min_trades")
            ),
            "edge_margin_r": float(get_setting(config_dict, "acceptance.edge_margin_r")),
            "edge_ci_alpha": float(get_setting(config_dict, "acceptance.edge_ci_alpha")),
            "bootstrap_samples": int(
                get_setting(config_dict, "acceptance.bootstrap_samples")
            ),
            "stop_band_ratio": float(get_setting(config_dict, "acceptance.stop_band_ratio")),
            "models": [asdict(item) for item in flags],
        },
        "correlation": return_correlation(equity),
        "equity": {model: equity_series(rows) for model, rows in equity.items()},
        "open_positions": positions,
        # Portföy yoğunlaşması: ÖLÇÜM, kural değil (bkz. concentration).
        "concentration": concentration(
            models, ledger=ledger, config=config_dict, marks=marks
        ),
        "recent_trades": recent_trades(trades, limit=RECENT_TRADE_LIMIT),
        "model_trades": model_trades(trades, limit=model_trade_limit),
        # Gruplar da aralık alır: bir kırılım grubunun ortalamasına örneklemi ve
        # aralığı olmadan bakmak, karar 27/28'in iki kez düştüğü tuzaktır.
        "breakdowns": model_breakdowns(
            trades,
            kinds=breakdowns,
            ci_alpha=ci_alpha,
            bootstrap_samples=bootstrap_samples,
            seed=int(get_setting(config_dict, "random_seed")),
        ),
        "activity": activity(
            trades, positions=positions, as_of=market.as_of, hours=ACTIVITY_HOURS
        ),
    }


# --------------------------------------------------------------------------- #
# Kırılımlar (kol / sembol)
# --------------------------------------------------------------------------- #
_BREAKDOWN_KEYS: Mapping[str, Any] = {
    "arm": arm_of,
    "symbol": symbol_of,
    "exit_rule": exit_rule_of,
    "session": session_of,
    "loss_streak": loss_streak_of,
}

# Anahtarı tek satırdan okunamayan kırılımlar için ÖN HAZIRLIK. Kol, sembol, seans ve
# çıkış kuralı satırın kendi alanlarından türer; kayıp serisi ise satırın kendisinde
# DEĞİL, ondan önce kapanmış pozisyonların sırasında durur. Hazırlık adımı bu ölçütü
# türetilmiş bir alana yazar, anahtar da onu okur — `breakdown`ın tek satırlık `key`
# sözleşmesi bozulmadan kalır.
_BREAKDOWN_PREPARE: Mapping[str, Any] = {
    "loss_streak": annotate_loss_streak,
}


def model_breakdowns(
    trades: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    kinds: Sequence[str],
    ci_alpha: float = float("nan"),
    bootstrap_samples: int = 0,
    seed: int = 0,
) -> dict[str, dict[str, dict[str, Any]]]:
    """Model başına kol ve/veya sembol kırılımı. `kinds` boşsa bölüm de boştur.

    Sembol kırılımı scalp katmanında ölçümün bir parçasıdır, süs değil: evrendeki her
    sembol aynı likiditeye sahip değildir ve kayma varsayımı (`slippage_base`) tüm
    semboller için tek bir sayıdır. İnce kitapta işlem gören bir sembolün `cost_per_r`
    kolonu diğerlerinden belirgin biçimde ayrışıyorsa, o varsayımın orada tutmadığı
    buradan okunur — sonuç yorumlanmadan önce bilinmesi gereken şey budur.

    Çıkış kuralı kırılımı (`exit_rule`) aynı statüdedir: üç aşamalı çıkış yönetiminin
    katkısı (modeller 13/14/15), üç aşamanın kaç kez tetiklendiği bilinmeden okunamaz.
    Birimi DİLİMDİR, pozisyon değil — bkz. `core/metrics.py::exit_rule_of`.
    """
    result: dict[str, dict[str, dict[str, Any]]] = {}
    for kind in kinds:
        key = _BREAKDOWN_KEYS.get(kind)
        if key is None:
            raise ValueError(f"bilinmeyen kırılım: {kind!r} (geçerli: {sorted(_BREAKDOWN_KEYS)})")
        prepare = _BREAKDOWN_PREPARE.get(kind)
        result[kind] = {
            model: {
                group: asdict(stats)
                for group, stats in breakdown(
                    prepare(rows) if prepare is not None else rows,
                    key=key,
                    ci_alpha=ci_alpha,
                    bootstrap_samples=bootstrap_samples,
                    seed=seed,
                ).items()
            }
            for model, rows in trades.items()
        }
    return result


# --------------------------------------------------------------------------- #
# Fiyat çıpası
# --------------------------------------------------------------------------- #
def marks_from_market(market: MarketData) -> dict[str, float]:
    """Sembol -> `as_of` barının KAPANIŞI.

    Kural 12: yalnızca kapanmış barlar. Son satırı körlemesine almak yerine `as_of`
    satırı aranır — bir sembolün serisi daha ileri giderse (önbellek yenilendi, tur
    gecikti) o barın fiyatı turun "şimdi"sinden ileride olur ve açık pozisyonlar
    stratejilerin görmediği bir fiyattan işaretlenirdi.
    """
    marks: dict[str, float] = {}
    for symbol, frame in market.ohlcv.items():
        if frame is None or frame.empty or "close" not in frame.columns:
            continue
        if market.as_of in frame.index:
            marks[symbol] = float(frame.loc[market.as_of, "close"])
        else:
            logger.debug("%s serisinde as_of barı yok, işaretleme dışı", symbol)
    return marks


# --------------------------------------------------------------------------- #
# Özsermaye eğrileri
# --------------------------------------------------------------------------- #
def equity_series(
    equity_rows: Sequence[Mapping[str, Any]], *, max_points: int = EQUITY_MAX_POINTS
) -> list[list[Any]]:
    """`[[ts, equity], ...]`. Uzun eğriler EŞİT ARALIKLA seyreltilir.

    Seyreltme tepe/dip değil aralık üzerinden yapılır: "en uç noktaları koru" biçiminde
    bir seyreltme eğriyi olduğundan oynak gösterir. İlk ve son nokta her zaman korunur,
    böylece başlangıç sermayesi ve son özsermaye grafikten okunabilir kalır. Max drawdown
    gibi uç değerler zaten tablodan (hesap düzeyi metrikler) okunur, grafikten değil.
    """
    points: list[list[Any]] = []
    for row in equity_rows:
        ts = str(row.get("ts", ""))
        value = _to_float(row.get("equity"))
        if ts and value is not None:
            points.append([ts, value])
    if len(points) <= max_points or max_points < 2:
        return points
    step = (len(points) - 1) / (max_points - 1)
    indexes = sorted({int(round(index * step)) for index in range(max_points)} | {len(points) - 1})
    return [points[index] for index in indexes]


# --------------------------------------------------------------------------- #
# Açık pozisyonlar
# --------------------------------------------------------------------------- #
def open_positions(
    models: Sequence[str], *, ledger: Ledger, marks: Mapping[str, float]
) -> list[dict[str, Any]]:
    """Her modelin `positions.json`'daki açık pozisyonları, güncel fiyatla işaretlenmiş.

    `pnl` ÇIKIŞ MALİYETİ HARİÇTİR: pozisyon kapanmadığı için çıkış fiyatı da komisyonu da
    bilinmez. Uydurmak (örn. mevcut fiyattan kapanmış saymak) deftere hiç girmeyecek bir
    sayıyı kapanmış işlemlerin yanına koyardı; sayfa bunu etiketiyle söyler.

    `r` payda olarak İLK stop'tan gelen risk tutarını kullanır — kapanan işlemlerdeki
    `risk_amount` ile birebir aynı tanım (bkz. core/portfolio.py::_close), ki açık ve
    kapalı işlemler aynı birimde okunabilsin. Stop'suz referans pozisyonlarda `nan`.
    """
    rows: list[dict[str, Any]] = []
    for model in models:
        state = ledger.load_state(model)
        if not state:
            continue
        for payload in state.get("positions", ()) or ():
            rows.append(_position_row(model, payload, marks))
    rows.sort(key=lambda row: (row["model"], row["symbol"]))
    return rows


def concentration(
    models: Sequence[str],
    *,
    ledger: Ledger,
    config: Mapping[str, Any],
    marks: Mapping[str, float],
) -> dict[str, dict[str, float]]:
    """Model -> açık pozisyonların yoğunlaşması (net/brüt maruziyet, en büyük sembol payı).

    Hesap BURADA YAPILMAZ: `core/portfolio.py::Portfolio.concentration` çağrılır (kural 7
    ve bu modülün sözleşmesi — rapor çizer, hesaplamaz). Portföy defterin durumundan
    kurulur, yani sayı `positions.json`daki aynı pozisyonlardan gelir; ikinci bir yol,
    dashboard'un motorun bilmediği bir maruziyet göstermesi demekti.

    **Bu bir ÖLÇÜMDÜR, bir kural değil.** Hiçbir sinyal bu sayılara göre elenmez. Neden
    yine de raporlanıyor: bir korelasyon/net beta tavanı önerisi ancak modellerin
    gerçekten yoğunlaştığı GÖSTERİLİRSE tartışılabilir ve bugün o sayı hiçbir yerde yok
    (bkz. `Portfolio.concentration`).
    """
    portfolio = Portfolio(config)
    rows: dict[str, dict[str, float]] = {}
    for model in models:
        state = ledger.load_state(model)
        if not state:
            continue
        portfolio.load_state(model, state)
        rows[model] = portfolio.concentration(model, marks)
    return rows


def _position_row(
    model: str, payload: Mapping[str, Any], marks: Mapping[str, float]
) -> dict[str, Any]:
    symbol = str(payload.get("symbol", ""))
    direction = str(payload.get("direction", ""))
    qty = _to_float(payload.get("qty")) or 0.0
    entry = _to_float(payload.get("entry_price")) or 0.0
    stop = _to_float(payload.get("stop_price"))
    initial_stop = _to_float(payload.get("initial_stop_price"))
    funding = _to_float(payload.get("funding")) or 0.0
    entry_fee = _to_float(payload.get("entry_fee")) or 0.0
    partial_tp = payload.get("partial_tp")
    breakeven_at_r = _to_float(payload.get("breakeven_at_r"))
    trail_giveback = _to_float(payload.get("trail_giveback_pct"))
    trailing_atr = _to_float(payload.get("trailing_atr"))
    partial_done = bool(payload.get("partial_done", False))
    # Fiyatı olmayan sembolde giriş fiyatı kullanılır: bilgi yokken pozisyonu kâr ya da
    # zararda göstermek uydurmak olurdu (core/portfolio.py::_mark ile aynı kural).
    mark = float(marks.get(symbol, entry))
    sign = 1.0 if direction == "long" else -1.0
    gross = sign * qty * (mark - entry)
    pnl = gross - entry_fee + funding
    risk = None if initial_stop is None else qty * abs(entry - initial_stop)

    return {
        "model": model,
        "symbol": symbol,
        "direction": direction,
        "opened_at": str(payload.get("opened_at", "")),
        "qty": qty,
        "entry_price": entry,
        "mark_price": mark,
        "marked": symbol in marks,
        "stop_price": stop,
        "initial_stop_price": initial_stop,
        "liq_price": _to_float(payload.get("liq_price")),
        "leverage": _to_float(payload.get("leverage")),
        "notional": qty * entry,
        # Marj ve riske edilen tutar deftere yazılan iki AYRI büyüklüktür ve biri
        # diğerinden türetilemez: marj notional/kaldıraçtır, risk ise ilk stop'a olan
        # mesafedir (kural 11'in paydası). Sayfa ikisini yan yana gösterebilsin diye
        # ikisi de taşınır — "ne kadarı bağlı" ile "ne kadarı riskte" aynı soru değil.
        "margin": _to_float(payload.get("margin")),
        "risk_amount": risk,
        "initial_qty": _to_float(payload.get("initial_qty")),
        # Hedefler KALAN hedeflerdir: kısmi TP dolduğunda portfolio onu pozisyondan
        # düşer (core/portfolio.py), dolayısıyla sayfa "bundan sonra ne bekleniyor"u
        # gösterir — dolmuş bir hedefi hâlâ beklenen gibi çizmek yanlış olurdu.
        "take_profits": [
            {
                "price": _to_float(tp.get("price")),
                "fraction": _to_float(tp.get("fraction")),
            }
            for tp in (payload.get("take_profits", ()) or ())
            if isinstance(tp, Mapping)
        ],
        "gross_pnl": gross,
        "entry_fee": entry_fee,
        "funding": funding,
        "pnl": pnl,
        # Yüzde NET PnL'den türetilir, brütten değil: iki sayı yan yana duruyor ve
        # birinin artı diğerinin eksi görünmesi (brüt kârda ama komisyon sonrası
        # zararda bir pozisyon) okuyucuya çelişki gibi gelirdi.
        "pnl_pct": (pnl / (qty * entry) * 100.0) if qty * entry > 0.0 else float("nan"),
        "r": (pnl / risk) if risk else float("nan"),
        "trailing_atr": trailing_atr,
        # --- Üç aşamalı çıkış yönetiminin DURUMU (kural 13b) ---
        # İstek ile OLAY ayrı taşınır: `breakeven_at_r` modelin açılışta bildirdiği
        # isteği, `breakeven_done` stop'un gerçekten girişe çekilmiş olduğunu söyler.
        # İkisini tek alana indirmek, mekanizmayı bildiren ama henüz tetiklenmemiş bir
        # pozisyonu "yönetildi" gibi gösterirdi — oysa modeller 13/14/15'in ölçtüğü şey
        # yönetimin UYGULANMASI.
        "breakeven_at_r": breakeven_at_r,
        "breakeven_done": _breakeven_done(
            direction=direction, entry=entry, stop=stop, breakeven_at_r=breakeven_at_r
        ),
        "partial_tp": (
            {
                "r": _to_float(partial_tp.get("r")),
                "fraction": _to_float(partial_tp.get("fraction")),
            }
            if isinstance(partial_tp, Mapping)
            else None
        ),
        "partial_done": partial_done,
        "trail_giveback_pct": trail_giveback,
        # "Takip aktif" = stop'u HÂLÂ hareket ettirebilecek bir kural var. Geri verme
        # takibi kısmi çıkıştan önce devreye girmez (core/engine.py::_giveback_stop), bu
        # yüzden `partial_done` şartı burada da durur.
        "trailing_active": trailing_atr is not None or (trail_giveback is not None and partial_done),
        # Stop'u en son hangi kural taşıdı; boş = ilk stop yerinde duruyor.
        "stop_rule": str(payload.get("stop_rule", "") or ""),
        "stop_moved": stop is not None and initial_stop is not None and stop != initial_stop,
        "reason": str(payload.get("reason", "")),
    }


def _breakeven_done(
    *, direction: str, entry: float, stop: float | None, breakeven_at_r: float | None
) -> bool:
    """Stop GERÇEKTEN başabaşa (ya da ötesine) çekilmiş mi.

    Ölçüt stop'un kendisidir, "pozisyon o R'a ulaştı mı" hesabının burada tekrarlanması
    değil: bu modül salt okunurdur ve motorun kuralını ikinci kez yazmak, ikisinin bir
    gün sessizce ayrışması demekti (core/engine.py::_breakeven_stop tek uygulayıcıdır).
    Mekanizmayı hiç bildirmemiş bir model için False — stop'u girişe denk gelen bir
    pozisyonu "başabaş alındı" diye göstermek, olmayan bir yönetimi raporlamak olurdu.
    """
    if breakeven_at_r is None or stop is None:
        return False
    return stop >= entry if direction == "long" else stop <= entry


# --------------------------------------------------------------------------- #
# Son işlemler ve 24 saatlik hareket
# --------------------------------------------------------------------------- #
def recent_trades(
    trades_by_model: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    limit: int = RECENT_TRADE_LIMIT,
) -> list[dict[str, Any]]:
    """Tüm modellerin defterlerinden, kapanış zamanına göre en yeni `limit` işlem.

    `signal_reason` kırpılmadan taşınır: bir modelin neden o işlemi açtığı (ensemble'ın
    oy sayısı, confluence'ın güven kuyruğu) tablodaki sayıdan çok daha fazlasını söyler
    ve denetim izinin okunabilir yüzü tam olarak budur.
    """
    rows = [
        {**dict(row), "model": model}
        for model, trades in trades_by_model.items()
        for row in trades
    ]
    rows.sort(key=lambda row: str(row.get("closed_at", "")), reverse=True)
    return [_trade_row(row) for row in rows[: max(0, limit)]]


def _trade_row(row: Mapping[str, Any]) -> dict[str, Any]:
    risk = _to_float(row.get("risk_amount"))
    pnl = _to_float(row.get("pnl"))
    return {
        "model": str(row.get("model", row.get("strategy", ""))),
        "symbol": str(row.get("symbol", "")),
        "direction": str(row.get("direction", "")),
        "opened_at": str(row.get("opened_at", "")),
        "closed_at": str(row.get("closed_at", "")),
        "entry_price": _to_float(row.get("entry_price")),
        "exit_price": _to_float(row.get("exit_price")),
        "stop_price": _to_float(row.get("stop_price")),
        "qty": _to_float(row.get("qty")),
        "notional": _to_float(row.get("notional")),
        # Kaldıraç ve marj deftere yazılır ama tabloda türetilemez: kural 11 kaldıracı bir
        # sonuç olarak üretir (tavana takılan pozisyon küçülür), yani notional/sermaye
        # oranından geri hesaplanamaz — sermaye o işlemin açıldığı andaki sermayedir.
        "leverage": _to_float(row.get("leverage")),
        "margin": _to_float(row.get("margin")),
        # Deftere yazılan GERÇEKLEŞEN 1R. R kolonunun paydası olduğu için taşınır:
        # onsuz sayfa "bu işlemin R'si neye göre" sorusunu cevaplayamaz ve okuyucu
        # paydayı `risk_per_trade × sermaye` sanar (kural 11'in tavanı boyutu
        # küçülttüğünde ikisi ayrışır).
        "risk_amount": risk,
        "pnl": pnl,
        "fee": _to_float(row.get("fee")),
        "slippage_cost": _to_float(row.get("slippage_cost")),
        "funding": _to_float(row.get("funding")),
        "r": (pnl / risk) if (risk and pnl is not None) else float("nan"),
        "exit_reason": str(row.get("exit_reason", "")),
        # Çıkışın ALT sebebi (core/portfolio.py::_exit_notes): takip eden stop mu, başabaş
        # stop'u mu, zaman stop'u mu. `exit_reason` beş kaba koddur ve bu ayrımı taşımaz;
        # eski satırlarda etiket yoktur ve boş kalır — uydurulmaz.
        "exit_rule": find_tag(row.get("notes", ""), "exit_rule") or "",
        # Kısmi çıkış TAMAMLANMIŞ bir işlem değildir: aynı pozisyonun bir dilimidir ve
        # kalanı hâlâ açıktır. Sayfa onu ayrı satır olarak gösterir ama istatistiğe
        # katmaz; ayrımı satırın kendisi taşısın diye bayrak yükte durur.
        "is_partial": str(row.get("exit_reason", "")) == "partial",
        "notes": str(row.get("notes", "")),
        "reason": str(row.get("signal_reason", "")),
    }


def model_trades(
    trades_by_model: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    limit: int = MODEL_TRADE_LIMIT,
) -> dict[str, Any]:
    """Model başına kapanmış işlem geçmişi: en yeni `limit` satır, yeniden eskiye.

    `recent_trades` tüm modellerin TEK akışıdır ("şu anda ne oldu"); burası modelin
    kendi geçmişidir ("bu model nasıl işlem yapıyor"). İkisi ayrı durur çünkü tek bir
    akışı modele göre filtrelemek, çok işlem yapan bir modelin 20 satırı doldurup
    diğerlerini akıştan silmesi demekti.

    `total` kırpılmadan ÖNCEki satır sayısıdır: sayfa "son 100 gösteriliyor, defterde
    420 var" diyebilsin. Olmadığında kırpılmış bir liste, modelin tüm geçmişi gibi
    okunur.
    """
    out: dict[str, Any] = {"limit": int(max(0, limit)), "models": {}}
    for model, trades in trades_by_model.items():
        rows = sorted(
            (dict(row) for row in trades),
            key=lambda row: str(row.get("closed_at", "")),
            reverse=True,
        )
        out["models"][model] = {
            "total": len(rows),
            "trades": [_trade_row({**row, "model": model}) for row in rows[: max(0, limit)]],
        }
    return out


def activity(
    trades_by_model: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    positions: Sequence[Mapping[str, Any]],
    as_of: pd.Timestamp,
    hours: int = ACTIVITY_HOURS,
) -> dict[str, Any]:
    """Son `hours` saatte açılan ve kapanan işlemler — Telegram özetinin "o gün" bölümü.

    Pencere duvar saatinden değil `as_of`'tan geriye sayılır: koşu geciktiğinde ya da
    elle tekrarlandığında duvar saati penceresi turun gerçekten işlediği barlarla
    örtüşmez ve özet, olmayan bir sessizliği rapor ederdi.

    Açılan işlem sayılırken HÂLÂ AÇIK pozisyonlar da sayılır: yalnızca kapananlara
    bakmak, pencerede açılıp açık kalan bir pozisyonu hiç olmamış gibi gösterirdi.
    """
    since = pd.Timestamp(as_of) - pd.Timedelta(hours=int(hours))
    since_text = since.isoformat()

    closed = [
        _trade_row({**dict(row), "model": model})
        for model, trades in trades_by_model.items()
        for row in trades
        if str(row.get("closed_at", "")) >= since_text
    ]
    closed.sort(key=lambda row: str(row["closed_at"]), reverse=True)

    opened_closed = sum(
        1
        for model, trades in trades_by_model.items()
        for row in trades
        if str(row.get("opened_at", "")) >= since_text
    )
    opened_open = [row for row in positions if str(row.get("opened_at", "")) >= since_text]

    r_values = [row["r"] for row in closed if not math.isnan(row["r"])]
    pnl_values = [row["pnl"] for row in closed if row["pnl"] is not None]

    return {
        "since": since_text,
        "hours": int(hours),
        "opened": opened_closed + len(opened_open),
        "still_open": len(opened_open),
        "closed": len(closed),
        "closed_pnl": sum(pnl_values) if pnl_values else 0.0,
        "closed_avg_r": (sum(r_values) / len(r_values)) if r_values else float("nan"),
        "closed_wins": sum(1 for value in r_values if value > 0.0),
        "trades": closed,
    }


def _to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
