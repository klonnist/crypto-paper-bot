#!/usr/bin/env python3
"""Adım 0b — `ema_trend`in dönem A YOL ölçümü: salt okunur teşhis aracı.

**Bu araç ölçümün parçası DEĞİLDİR** (`scripts/measure_vwap_signal.py` ile aynı statü):
canlı deftere yazmaz, `config.yaml`a dokunmaz, hiçbir modelin davranışını değiştirmez ve
hiçbir kabul kapısı buradan okunmaz. Cevapladığı tek soru şudur:

    Kesişim sinyalinin taşıdığı sapma (TP payı %35.2 ↔ sürüklenmesiz yürüyüşün %33.3'ü)
    fiyat YOLUNUN neresinde yoğunlaşıyor?

Gerekçe: `ema_trend` bloke edildi (docs/backtest.md > 6d > SONUÇ) ve "çıkış geometrisi
kenarı yiyor" tezi ancak yol istatistiğiyle sınanabilir. Sapmanın ŞEKLİNİ ölçmeden bir
çıkış varyantı tanımlamak tahmin olurdu; tahminle tanımlanan bir varyant ise ilk kapıda
"kurtarmak için ayar arama" hâline gelir (docs/backtest.md > 7.1).

## İkinci bir backtest DEĞİLDİR

Pencereyi `scripts/backtest.py::run_backtest` koşar ve dönem A'nın parametreleri
`scripts/backtest_ema.py`den İTHAL EDİLİR, burada yeniden yazılmaz — iki yerde ayrışan
bir pencere, teşhisin ölçtüğü koşunun karara giren koşu olmadığı anlamına gelirdi.
Performans metriği burada HİÇ hesaplanmaz (kural 7): ortalama R, kırılımlar ve tutuş
süresi `core/metrics.py`den gelir. Bu modülün kendi hesapladığı tek şey **yol
istatistiğidir** (MFE/MAE, çıkış sonrası devam) ve o, `core/metrics.py`de yoktur.

## Dönem B'ye DOKUNMAZ — ve bu yapısal olarak garanti edilir

Betikte dönem B parametresi YOKTUR; verilebilecek bir bayrak da yoktur. Gerekçe
kontaminasyondur (docs/backtest.md > 6): varyantların türetileceği her sayı A'dan
gelmek zorundadır, çünkü B onların OOS penceresi olacaktır. B'nin yol istatistiğini bir
kez görmek, ondan sonra seçilen her eşiği B'ye bakarak seçilmiş yapardı.

## Determinizm kapısı

Koşu, karara giren koşunun (`backtest-ema` #35391881083, commit `4c5bfac`) dönem A
sayılarını BİREBİR yeniden üretmelidir. Üretmezse harness sapmıştır ve **hiçbir yol
istatistiği okunmaz** — betik hata koduyla biter. Kapı, `docs/backtest.md > 1`in
(Kapı 0) aynı mantığıdır: ölçülen şeyin ölçülmek istenen şey olduğu önce kanıtlanır.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd  # noqa: E402

from core.config import get_setting, load_config  # noqa: E402
from core.data import _cache_path, bar_duration, okx_bar  # noqa: E402
from core.layers import resolve_layer  # noqa: E402
from core.ledger import Ledger  # noqa: E402

# `_median` / `_percentile` bilinçli olarak core'dan ithal edilir: yüzdelik tanımı bu
# projede TEK yerdedir ve ikinci bir uygulama, aynı defterin iki farklı p90'ı demekti
# (core/metrics.py::holding_stats'ın yorumunda anlatılan hata). Alt çizgili olmaları
# "kopyala" davetiyesi değil, "tek tanım" işaretidir.
from core.metrics import _median, _percentile, holding_stats, merge_fills  # noqa: E402
from scripts.backtest import format_breakdowns, run_backtest  # noqa: E402
from scripts.backtest_ema import (  # noqa: E402
    FEE_RATE,
    LAYER,
    MODEL,
    PERIOD_A_CUTOFF,
    PERIOD_A_START,
    PERIOD_A_TAIL_END,
    SLIPPAGE_BASE,
)

logger = logging.getLogger("diagnose-ema-exits")

# Karara giren koşunun dönem A sayıları (docs/backtest.md > 6d > SONUÇ ve
# docs/data/backtest_ema_trend.json). Teşhis koşusu bunları birebir üretmek ZORUNDADIR.
PREREGISTERED_A: Mapping[str, Any] = {
    "positions": 369,
    "tp_exits": 130,
    "stop_exits": 239,
    "avg_r": -0.0014889765123856217,
    "max_hold_bars": 129.0,
}
AVG_R_TOLERANCE = 1e-9

# Çıkış sonrası ufuklar (bar). Bir eşik DEĞİL, bir dağılım ekseni: "kuyruk kaç bar sonra
# ne kadar" sorusu tek bir ufukla sorulamaz ve tek ufuk seçmek, o ufku sonradan bir
# parametre gibi kullanmaya davet ederdi.
CONTINUATION_HORIZONS: tuple[int, ...] = (5, 10, 20, 40)

# --------------------------------------------------------------------------- #
# BİRİNCİL VARYANT SEÇİM KURALI — teşhis çıktısı GÖRÜLMEDEN sabitlendi
# --------------------------------------------------------------------------- #
# Kuralın ÇALIŞTIRILABİLİR kopyası burasıdır; docs/backtest.md > 6e onu alıntılar.
# İki yerde yazılı bir kural, bir gün birinin sessizce ayrışması demekti — ve kuralın
# tek işi "sonucu görüp seçmedik"i kanıtlamak olduğu için o ayrışma kuralı yok ederdi.
#
# Eşikler ve gerekçeleri (hiçbiri veriden türetilmedi):
# - M1 = 1.0R: `config.yaml > exit_management.breakeven_at_r` değerinin ta kendisi. Bir
#   breakeven kuralının tetiklenebilmesi için gereken hareket odur; keyfi değildir.
# - M2 = +0.25R: ölçülen friksiyonun (0.057R) kabaca dört katı. YUVARLAK bir sayıdır ve
#   öyle seçildiği burada yazılıdır — sonradan eşik tartışması açılmasın.
# - M2 ufku = 20 bar: dönem A'nın p90 tutuş süresi 21 bardır (docs/backtest.md > 6d'de
#   koşudan önce yayımlandı), yuvarlanmış hâli. Teşhis çıktısından GELMİYOR.
# - M4 = 2.0×: yuvarlak katsayı, açıkça yuvarlak seçildi.
M1_MIN_MEDIAN_MFE_R = 1.0
M2_HORIZON_BARS = 20
M2_MIN_MEDIAN_DRIFT_R = 0.25
M4_MIN_HOLD_RATIO = 2.0

# MFE/MAE kovaları (R). Sınırlar geometriden gelir, veriden değil: 1.0R breakeven'ın,
# 1.5R kısmi çıkışın, 2.0R hedefin yeridir (config > exit_management, ema_trend).
MFE_BUCKETS: tuple[float, ...] = (0.0, 0.5, 1.0, 1.5, 2.0)
MAE_BUCKETS: tuple[float, ...] = (-1.0, -0.75, -0.5, -0.25, 0.0)


# --------------------------------------------------------------------------- #
# Yol istatistiği
# --------------------------------------------------------------------------- #
def position_paths(
    trades: Sequence[Mapping[str, Any]],
    candles: Mapping[str, pd.DataFrame],
    *,
    horizons: Sequence[int] = CONTINUATION_HORIZONS,
) -> list[dict[str, Any]]:
    """Her POZİSYON için yol istatistiği (birim: `merge_fills`, yani dilim değil pozisyon).

    Tanımlar — hepsi R cinsindendir ve payda **ilk stop mesafesidir**
    (`|giriş − stop|`), `core/metrics.py`nin R tanımıyla aynı payda:

    - `mfe_r` / `mae_r`: açılış barından KAPANIŞ BARI DÂHİL en lehte / en aleyhte hareket.
    - `mfe_r_prior` / `mae_r_prior`: kapanış barı HARİÇ aynı ölçü. Karar için anlamlı olan
      budur: stop hareketleri bar KAPANDIKTAN sonra uygulanır (kural 13b), yani bir
      breakeven ya da kısmi çıkış kuralı ancak önceki barlarda görülen hareketi
      görebilirdi. Kapanış barını dâhil etmek, o kuralı pozisyonu öldüren barın içindeki
      bilgiyle donatmak — yani look-ahead — olurdu.
    - `continuation_r[H]`: çıkıştan SONRAKİ H bar içinde fiyatın çıkış fiyatının ne kadar
      üstüne çıktığı (azami YÜKSELİŞ). "Kesilen kuyruk" tam olarak budur.
    - `drift_r[H]`: çıkıştan H bar SONRAKİ barın KAPANIŞI ile çıkış fiyatı arasındaki
      İŞARETLİ fark. İkisi ayrı tutulur çünkü ayrı sorulara cevap verirler ve yalnızca
      ikincisi bir kuralın dayanağı olabilir: azami yükseliş tanım gereği ≥ 0'dır ve
      sürüklenmesiz bir yürüyüşte bile ufukla birlikte `√H` hızında büyür (20 barda
      ~2.4R), yani "kuyruk var" demeye her zaman izin verirdi. İşaretli kapanışın
      medyanı ise martingal altında SIFIRDIR — sıfırdan sapması gerçek bir sürüklenmedir.

    Mum içi sıralama bilinemez (kural 13): `mfe_r` ile `mae_r` aynı barda gerçekleşmiş
    olabilir ve hangisinin önce olduğu bu veriyle söylenemez. Bu yüzden buradaki hiçbir
    sayı bir kuralın "ne kazandıracağı" değildir — yalnızca yolun şeklidir.
    """
    paths: list[dict[str, Any]] = []
    for row in trades:
        symbol = str(row.get("symbol") or "")
        frame = candles.get(symbol)
        if frame is None or frame.empty:
            logger.warning("%s: mum verisi yok, pozisyon yol ölçümüne girmedi", symbol)
            continue

        opened, closed = _stamp(row.get("opened_at")), _stamp(row.get("closed_at"))
        entry, stop = _float(row.get("entry_price")), _float(row.get("stop_price"))
        exit_price = _float(row.get("exit_price"))
        if opened is None or closed is None or entry is None or stop is None:
            logger.warning("%s: eksik alan, pozisyon yol ölçümüne girmedi", symbol)
            continue
        unit = abs(entry - stop)
        if unit <= 0.0:
            logger.warning("%s: stop girişe eşit, R paydası yok", symbol)
            continue

        window = frame.loc[(frame.index >= opened) & (frame.index <= closed)]
        if window.empty:
            logger.warning("%s: %s..%s aralığında bar yok", symbol, opened, closed)
            continue
        prior = window.iloc[:-1]

        after = frame.loc[frame.index > closed]
        continuation = {
            str(horizon): (
                (float(after["high"].iloc[:horizon].max()) - exit_price) / unit
                if exit_price is not None and len(after) > 0
                else float("nan")
            )
            for horizon in horizons
        }
        # İşaretli sürüklenme: H barın MAKSİMUMU değil, H. barın KAPANIŞI. Pencere
        # dolmuyorsa `nan` — kısa bir pencereyi son bara kadar doldurmak, ufku pozisyondan
        # pozisyona değiştirip medyanı kısa kuyruğa doğru çekerdi.
        drift = {
            str(horizon): (
                (float(after["close"].iloc[horizon - 1]) - exit_price) / unit
                if exit_price is not None and len(after) >= horizon
                else float("nan")
            )
            for horizon in horizons
        }

        paths.append(
            {
                "symbol": symbol,
                "opened_at": opened.isoformat(),
                "closed_at": closed.isoformat(),
                "exit_reason": str(row.get("exit_reason") or ""),
                "bars_held": int(len(window) - 1),
                "r_multiple": _ratio(_float(row.get("pnl")), _float(row.get("risk_amount"))),
                "mfe_r": (float(window["high"].max()) - entry) / unit,
                "mae_r": (float(window["low"].min()) - entry) / unit,
                "mfe_r_prior": (
                    (float(prior["high"].max()) - entry) / unit if not prior.empty else float("nan")
                ),
                "mae_r_prior": (
                    (float(prior["low"].min()) - entry) / unit if not prior.empty else float("nan")
                ),
                "continuation_r": continuation,
                "drift_r": drift,
            }
        )
    return paths


def select_primary_family(
    *,
    median_mfe_r_stop: float,
    median_drift_r_tp: float,
    median_hold_stop: float,
    median_hold_tp: float,
) -> dict[str, Any]:
    """BİRİNCİL varyant ailesini teşhis çıktısından MEKANİK olarak seçer.

    Kural koşudan önce sabitlendi (docs/backtest.md > 6e) ve **bir kez çalışır**: çıkan
    aile beklenen olmasa bile tartışılmaz. İnsanın "baktım, en iyi görüneni seçtim"
    serbestliği tam olarak burada kapanır — bu yüzden seçim bir metin değil, bir
    fonksiyondur.

    Dallar ÖNCELİK SIRASIYLA denenir ve sıra da önceden yazılıdır:

    1. **M2 — kuyruk** (`tp` çıkışlarından 20 bar sonraki İŞARETLİ hareketin medyanı
       ≥ +0.25R): hedefte kesilen pozisyon yükselmeye devam ediyor -> *kazananı koşturan*
       aile (trailing / hedefin kaldırılması).
    2. **M1 — geri dönüş** (`stop` çıkışlarının medyan `mfe_r_prior`ı ≥ 1.0R): kaybedenler
       ölmeden önce en az 1R kâra gidiyor -> *breakeven + kısmi çıkış* ailesi.
    3. **M4 — oyalanma** (medyan `stop` tutuşu ≥ 2.0 × medyan `tp` tutuşu): kaybedenler
       uzun süre oyalanıp sonra ölüyor -> *zaman stop'u* ailesi.
    4. Hiçbiri: **tur KAPANIR.** Varyant kurulmaz. Bu bir başarısızlık değil bir
       sonuçtur: yolda çıkışın sömürebileceği bir yapı yoksa, açık kalan tek kaldıraç
       friksiyondur ve o, çıkış ekseninde değil stop mesafesi ekseninde durur
       (docs/backtest.md > 6e > KAYIT).

    M2'nin M1'den ÖNCE gelmesinin gerekçesi: ikisi de aynı pozisyonlar üzerinde ama ters
    yönde çalışır (biri kuyruğu uzatır, öteki keser). İkisi birden tetiklenirse hangisinin
    seçileceği önceden yazılmazsa, "hangisi daha mantıklı" tartışması kuralın kapatmak
    için var olduğu serbestliği geri açardı. M4 en sona konur çünkü ölçtüğü şey R değil
    sermaye hızıdır — C-1 bir R kapısıdır.
    """
    hold_ratio = (
        median_hold_stop / median_hold_tp
        if median_hold_tp and not math.isnan(median_hold_tp) and median_hold_tp > 0.0
        else float("nan")
    )
    branches = [
        ("M2", "kuyruk (trailing / hedefsiz)", median_drift_r_tp, M2_MIN_MEDIAN_DRIFT_R,
         _at_least(median_drift_r_tp, M2_MIN_MEDIAN_DRIFT_R)),
        ("M1", "geri dönüş (breakeven + kısmi çıkış)", median_mfe_r_stop, M1_MIN_MEDIAN_MFE_R,
         _at_least(median_mfe_r_stop, M1_MIN_MEDIAN_MFE_R)),
        ("M4", "oyalanma (zaman stop'u)", hold_ratio, M4_MIN_HOLD_RATIO,
         _at_least(hold_ratio, M4_MIN_HOLD_RATIO)),
    ]
    fired = next((branch for branch in branches if branch[4]), None)
    return {
        "measurements": [
            {"id": name, "family": family, "measured": value, "threshold": threshold, "fired": ok}
            for name, family, value, threshold, ok in branches
        ],
        "branch": fired[0] if fired else "yok",
        "family": fired[1] if fired else "tur kapanır (varyant kurulmaz)",
        "round_closes": fired is None,
    }


def _at_least(value: float, threshold: float) -> bool:
    """`nan` bir dalı TETİKLEMEZ: ölçülemeyen bir koşul sağlanmış sayılamaz."""
    return not math.isnan(value) and value >= threshold


def distribution(values: Sequence[float]) -> dict[str, float]:
    """Bir dağılımın özeti. Yüzdelikler `core/metrics.py::_percentile` ile hesaplanır."""
    clean = sorted(value for value in values if value is not None and not math.isnan(value))
    if not clean:
        return {"n": 0, "median": float("nan"), "p75": float("nan"),
                "p90": float("nan"), "min": float("nan"), "max": float("nan"),
                "mean": float("nan")}
    return {
        "n": len(clean),
        "median": _median(clean),
        "p75": _percentile(clean, 0.75),
        "p90": _percentile(clean, 0.90),
        "min": clean[0],
        "max": clean[-1],
        "mean": sum(clean) / len(clean),
    }


def buckets(values: Sequence[float], edges: Sequence[float]) -> list[dict[str, Any]]:
    """Kova sayımı. Kenarlar geometriden gelir (bkz. MFE_BUCKETS), veriden türetilmez."""
    clean = [value for value in values if value is not None and not math.isnan(value)]
    total = len(clean)
    rows: list[dict[str, Any]] = []
    bounds = [-math.inf, *edges, math.inf]
    for low, high in zip(bounds[:-1], bounds[1:]):
        count = sum(1 for value in clean if low <= value < high)
        rows.append(
            {
                "low": low,
                "high": high,
                "count": count,
                "pct": 100.0 * count / total if total else float("nan"),
            }
        )
    return rows


# --------------------------------------------------------------------------- #
# Determinizm kapısı
# --------------------------------------------------------------------------- #
def check_determinism(
    *, positions: int, tp_exits: int, stop_exits: int, avg_r: float, max_hold_bars: float
) -> dict[str, Any]:
    """Teşhis koşusu, karara giren koşunun dönem A sayılarını yeniden üretti mi?

    Üretmediyse yol istatistiği OKUNMAZ: ölçülen pencere, hakkında konuştuğumuz pencere
    değildir. Kapı sayıları eşitlik, ortalama R'yi ise dar bir toleransla (kayan nokta
    toplama sırası) karşılaştırır.
    """
    checks = {
        "positions": (positions, PREREGISTERED_A["positions"], positions == PREREGISTERED_A["positions"]),
        "tp_exits": (tp_exits, PREREGISTERED_A["tp_exits"], tp_exits == PREREGISTERED_A["tp_exits"]),
        "stop_exits": (stop_exits, PREREGISTERED_A["stop_exits"], stop_exits == PREREGISTERED_A["stop_exits"]),
        "avg_r": (avg_r, PREREGISTERED_A["avg_r"], abs(avg_r - float(PREREGISTERED_A["avg_r"])) <= AVG_R_TOLERANCE),
        "max_hold_bars": (
            max_hold_bars,
            PREREGISTERED_A["max_hold_bars"],
            max_hold_bars == PREREGISTERED_A["max_hold_bars"],
        ),
    }
    return {
        "passed": all(ok for _, _, ok in checks.values()),
        "checks": {name: {"measured": got, "expected": want, "ok": ok} for name, (got, want, ok) in checks.items()},
    }


# --------------------------------------------------------------------------- #
# Biçimlendirme
# --------------------------------------------------------------------------- #
def format_determinism(result: Mapping[str, Any]) -> str:
    lines = ["", "DETERMİNİZM KAPISI (karara giren koşu: backtest-ema #35391881083)", "-" * 72,
             f"{'ölçüt':18s}{'ölçülen':>22}{'beklenen':>22}{'':>6}"]
    for name, check in result["checks"].items():
        mark = "✅" if check["ok"] else "❌"
        lines.append(f"{name:18s}{_cell(check['measured']):>22}{_cell(check['expected']):>22}{mark:>6}")
    lines.append("")
    lines.append("GEÇTİ — yol istatistiği okunabilir." if result["passed"]
                 else "DÜŞTÜ — harness sapmış, hiçbir yol istatistiği okunmaz.")
    return "\n".join(lines) + "\n"


def format_distribution_table(title: str, rows: Mapping[str, Mapping[str, float]], *, unit: str) -> str:
    lines = ["", f"{title}  (birim: {unit})", "-" * 78,
             f"{'grup':22s}{'n':>6}{'medyan':>10}{'p75':>10}{'p90':>10}{'azami':>10}{'ort.':>10}"]
    for name, stats in rows.items():
        lines.append(
            f"{name:22s}{int(stats['n']):6d}{_cell(stats['median']):>10}{_cell(stats['p75']):>10}"
            f"{_cell(stats['p90']):>10}{_cell(stats['max']):>10}{_cell(stats['mean']):>10}"
        )
    return "\n".join(lines) + "\n"


def format_buckets(title: str, rows: Sequence[Mapping[str, Any]]) -> str:
    lines = ["", title, "-" * 52, f"{'aralık (R)':24s}{'n':>8}{'%':>10}"]
    for row in rows:
        low = "−∞" if row["low"] == -math.inf else f"{row['low']:+.2f}"
        high = "+∞" if row["high"] == math.inf else f"{row['high']:+.2f}"
        lines.append(f"{f'[{low}, {high})':24s}{row['count']:8d}{row['pct']:10.1f}")
    return "\n".join(lines) + "\n"


def format_continuation(rows: Mapping[str, Mapping[str, float]]) -> str:
    lines = ["", "TP SONRASI DEVAM — hedefte kapanan pozisyonun kesilen kuyruğu", "-" * 78,
             "Çıkış fiyatının üstüne çıkılan AZAMİ mesafe (R), çıkıştan sonraki H bar içinde.",
             "Bir kuralın kazancı DEĞİLDİR ve bir dalı TETİKLEMEZ: bu ölçü tanım gereği ≥ 0'dır",
             "ve sürüklenmesiz bir yürüyüşte bile √H hızında büyür. Kural işaretli ölçüye bakar.",
             "",
             f"{'ufuk (bar)':22s}{'n':>6}{'medyan':>10}{'p75':>10}{'p90':>10}{'azami':>10}{'ort.':>10}"]
    for horizon, stats in rows.items():
        lines.append(
            f"{horizon:22s}{int(stats['n']):6d}{_cell(stats['median']):>10}{_cell(stats['p75']):>10}"
            f"{_cell(stats['p90']):>10}{_cell(stats['max']):>10}{_cell(stats['mean']):>10}"
        )
    return "\n".join(lines) + "\n"


def format_drift(rows: Mapping[str, Mapping[str, float]]) -> str:
    lines = ["", "TP SONRASI İŞARETLİ HAREKET — kuralın (M2) baktığı ölçü", "-" * 78,
             "H. barın KAPANIŞI eksi çıkış fiyatı (R). Martingal altında medyanı SIFIRDIR;",
             "sıfırdan sapması gerçek bir sürüklenmedir. Pencere dolmayan pozisyon `—`.", "",
             f"{'ufuk (bar)':22s}{'n':>6}{'medyan':>10}{'p75':>10}{'p90':>10}{'azami':>10}{'ort.':>10}"]
    for horizon, stats in rows.items():
        lines.append(
            f"{horizon:22s}{int(stats['n']):6d}{_cell(stats['median']):>10}{_cell(stats['p75']):>10}"
            f"{_cell(stats['p90']):>10}{_cell(stats['max']):>10}{_cell(stats['mean']):>10}"
        )
    return "\n".join(lines) + "\n"


def format_primary_rule(rule: Mapping[str, Any]) -> str:
    """Ön-kayıtlı seçim kuralının uygulanması. Kural bir kez çalışır; sonucu tartışılmaz."""
    lines = ["", "BİRİNCİL VARYANT SEÇİMİ (kural koşudan ÖNCE sabitlendi — docs/backtest.md > 6e)",
             "-" * 86,
             f"{'dal':6s}{'aile':38s}{'ölçülen':>12}{'eşik':>10}{'':>8}"]
    for item in rule["measurements"]:
        mark = "TETİK" if item["fired"] else "—"
        lines.append(
            f"{item['id']:6s}{item['family']:38s}{_cell(item['measured']):>12}"
            f"{_cell(item['threshold']):>10}{mark:>8}"
        )
    lines.append("")
    lines.append(f"SEÇİLEN: {rule['family']}  (dal: {rule['branch']})")
    if rule["round_closes"]:
        lines.append(
            "Hiçbir dal tetiklenmedi: yolda çıkışın sömürebileceği bir yapı yok. Bu bir\n"
            "başarısızlık değil bir SONUÇTUR — varyant turu kurulmaz ve kayıt yazılır."
        )
    return "\n".join(lines) + "\n"


def _cell(value: Any) -> str:
    """Tanımsız değer `—` olur, `0` DEĞİL (docs/shared.js ile aynı söz)."""
    if value is None:
        return "—"
    if isinstance(value, float):
        return "—" if math.isnan(value) else f"{value:.4g}"
    return str(value)


# --------------------------------------------------------------------------- #
# Koşu
# --------------------------------------------------------------------------- #
def diagnose(
    *,
    out_dir: Path,
    history_bars: int,
    funding_periods: int,
    symbols: Sequence[str] | None = None,
    models: Sequence[str] | None = None,
    config_path: str | None = None,
) -> dict[str, Any]:
    """Dönem A'yı koşar, determinizmi sınar, yol istatistiğini üretir."""
    start = pd.Timestamp(PERIOD_A_START)
    end = pd.Timestamp(PERIOD_A_TAIL_END)
    cutoff = pd.Timestamp(PERIOD_A_CUTOFF)

    logger.info("dönem A koşusu: %s → %s (sinyal kesimi %s)", start, end, cutoff)
    result = run_backtest(
        layer_name=LAYER,
        start=start,
        end=end,
        out_dir=out_dir,
        models=list(models) if models else None,
        symbols=list(symbols) if symbols else None,
        history_bars=history_bars,
        funding_periods=funding_periods,
        fee_rate=FEE_RATE,
        slippage_base=SLIPPAGE_BASE,
        signal_cutoff=cutoff,
        config_path=config_path,
    )

    metrics = next((item for item in result.metrics if item.model == MODEL), None)
    if metrics is None:
        raise RuntimeError(f"{MODEL} metrikleri üretilmedi: koşu okunamaz")
    report = next((item for item in result.report.models if item.model == MODEL), None)
    if report is None:
        raise RuntimeError(f"{MODEL} tur raporu yok: koşu okunamaz")

    positions = int(metrics.total.trades)
    stop_exits = int(report.stop_exits)
    gate = check_determinism(
        positions=positions,
        tp_exits=positions - stop_exits,
        stop_exits=stop_exits,
        avg_r=float(metrics.total.avg_r),
        max_hold_bars=float((result.holding.get(MODEL) or {}).get("max_bars", float("nan"))),
    )

    payload: dict[str, Any] = {
        "model": MODEL,
        "layer": LAYER,
        "period": "A",
        "window": {"start": result.start.isoformat(), "end": result.end.isoformat(),
                   "signal_cutoff": cutoff.isoformat()},
        "costs": {"fee_rate": FEE_RATE, "slippage_base": SLIPPAGE_BASE},
        "deviations": dict(result.deviations),
        "determinism": gate,
        "validity": {
            "missing_bars": report.missing_bars,
            "unchecked_position_bars": report.unchecked_position_bars,
            "ambiguous_stop_exits": report.ambiguous_stop_exits,
            "stop_exits": stop_exits,
            "rejections": dict(report.rejections),
        },
    }
    if not gate["passed"]:
        return payload

    config = dict(resolve_layer(load_config(config_path), LAYER).config)
    rows = Ledger(out_dir / "ledger").read_trades(MODEL)
    trades = merge_fills(rows)
    payload["fills"] = {"rows": len(rows), "positions": len(trades)}

    candles = _load_candles(config, sorted({str(row.get("symbol") or "") for row in trades}))
    paths = position_paths(trades, candles)
    payload["paths_measured"] = len(paths)

    duration = bar_duration(str(get_setting(config, "timeframe")))
    by_reason = {"tp": [row for row in paths if row["exit_reason"] == "tp"],
                 "stop": [row for row in paths if row["exit_reason"] == "stop"]}
    other = [row for row in paths if row["exit_reason"] not in by_reason]
    if other:
        by_reason["diğer"] = other

    payload["holding_by_exit"] = {
        reason: distribution([row["bars_held"] for row in rows]) for reason, rows in by_reason.items()
    }
    payload["holding_core"] = {
        reason: _as_dict(
            holding_stats(
                [trade for trade in trades
                 if str(trade.get("exit_reason") or "") == reason],
                bar_duration=duration,
            )
        )
        for reason in ("tp", "stop")
    }
    payload["r_by_exit"] = {
        reason: distribution([row["r_multiple"] for row in rows if row["r_multiple"] is not None])
        for reason, rows in by_reason.items()
    }
    payload["mfe_prior_by_exit"] = {
        reason: distribution([row["mfe_r_prior"] for row in rows]) for reason, rows in by_reason.items()
    }
    payload["mae_prior_by_exit"] = {
        reason: distribution([row["mae_r_prior"] for row in rows]) for reason, rows in by_reason.items()
    }
    payload["mfe_prior_buckets_stop"] = buckets(
        [row["mfe_r_prior"] for row in by_reason.get("stop", [])], MFE_BUCKETS
    )
    payload["mae_prior_buckets_tp"] = buckets(
        [row["mae_r_prior"] for row in by_reason.get("tp", [])], MAE_BUCKETS
    )
    payload["continuation_after_tp"] = {
        str(horizon): distribution(
            [row["continuation_r"][str(horizon)] for row in by_reason.get("tp", [])]
        )
        for horizon in CONTINUATION_HORIZONS
    }
    payload["drift_after_tp"] = {
        str(horizon): distribution(
            [row["drift_r"][str(horizon)] for row in by_reason.get("tp", [])]
        )
        for horizon in CONTINUATION_HORIZONS
    }
    # Kural teşhisin İÇİNDE uygulanır: sonucu bir insanın okuyup dalı seçmesi, kuralın
    # kapatmak için var olduğu serbestliği geri açardı (docs/backtest.md > 6e).
    payload["primary_rule"] = select_primary_family(
        median_mfe_r_stop=payload["mfe_prior_by_exit"].get("stop", {}).get("median", float("nan")),
        median_drift_r_tp=payload["drift_after_tp"][str(M2_HORIZON_BARS)]["median"],
        median_hold_stop=payload["holding_by_exit"].get("stop", {}).get("median", float("nan")),
        median_hold_tp=payload["holding_by_exit"].get("tp", {}).get("median", float("nan")),
    )
    payload["breakdowns"] = {
        kind: {model: groups for model, groups in per_model.items() if model == MODEL}
        for kind, per_model in (result.breakdowns or {}).items()
    }
    payload["paths"] = paths
    return payload


def _load_candles(config: Mapping[str, Any], symbols: Sequence[str]) -> dict[str, pd.DataFrame]:
    """Mumları önbellekten OKUR — borsaya dokunmaz.

    Koşu zaten aynı işte indirdi; ikinci bir indirme aynı barları iki kez çekmek olurdu.
    Yol adı `core/data.py`nin kendi kuralından gelir (ikinci bir dosya adı şeması, bir
    gün sessizce ayrışan iki önbellek demekti).
    """
    bar = okx_bar(dict(config))
    frames: dict[str, pd.DataFrame] = {}
    for symbol in symbols:
        path = _cache_path(dict(config), symbol, bar)
        if not path.is_file():
            logger.warning("%s: önbellek dosyası yok (%s)", symbol, path)
            continue
        frame = pd.read_parquet(path)
        frames[symbol] = frame.sort_index()
    return frames


def _as_dict(stats: Any) -> dict[str, Any]:
    return {field: getattr(stats, field) for field in stats.__dataclass_fields__}


def _stamp(value: Any) -> pd.Timestamp | None:
    if value in (None, ""):
        return None
    stamp = pd.Timestamp(str(value))
    return stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")


def _float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(number) else number


def _ratio(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator in (None, 0.0):
        return None
    return numerator / float(denominator)


def format_report(payload: Mapping[str, Any]) -> str:
    """Log'a basılan rapor. Artifact her ortamdan indirilemiyor, tek çıkış yolu budur."""
    out = [format_determinism(payload["determinism"])]
    if not payload["determinism"]["passed"]:
        return "\n".join(out)

    validity = payload["validity"]
    out.append(
        f"\nVERİ BÜTÜNLÜĞÜ (B-2): missing_bars={validity['missing_bars']} "
        f"unchecked_position_bars={validity['unchecked_position_bars']} "
        f"belirsiz stop={validity['ambiguous_stop_exits']}\n"
        f"DEFTER: {payload['fills']['rows']} dolum satırı -> "
        f"{payload['fills']['positions']} pozisyon; yol ölçülen: {payload['paths_measured']}\n"
    )
    out.append(format_distribution_table("TUTUŞ SÜRESİ — çıkış sebebine göre",
                                         payload["holding_by_exit"], unit="bar"))
    out.append(format_distribution_table("GERÇEKLEŞEN R — çıkış sebebine göre",
                                         payload["r_by_exit"], unit="R"))
    out.append(format_distribution_table(
        "MFE (kapanış barı HARİÇ) — pozisyon ölmeden önce en lehte nereye gitti",
        payload["mfe_prior_by_exit"], unit="R"))
    out.append(format_distribution_table(
        "MAE (kapanış barı HARİÇ) — pozisyon en aleyhte nereye gitti",
        payload["mae_prior_by_exit"], unit="R"))
    out.append(format_buckets(
        "STOP'LA KAPANANLARIN MFE'si (kapanış barı hariç) — breakeven/kısmi çıkış kuralının görebileceği hareket",
        payload["mfe_prior_buckets_stop"]))
    out.append(format_buckets(
        "HEDEFE VARANLARIN MAE'si (kapanış barı hariç) — breakeven kuralının kaç kazananı keseceği",
        payload["mae_prior_buckets_tp"]))
    out.append(format_continuation(payload["continuation_after_tp"]))
    out.append(format_drift(payload["drift_after_tp"]))
    out.append(format_primary_rule(payload["primary_rule"]))
    out.append(format_breakdowns(payload.get("breakdowns") or {}))
    out.append(
        "\nOKUMA NOTLARI\n"
        "- Mum içi sıralama bilinemez (kural 13): MFE ile MAE aynı barda olmuş olabilir.\n"
        "- 'kapanış barı hariç' ölçüsü bilinçlidir: stop hareketleri bar KAPANDIKTAN sonra\n"
        "  uygulanır (kural 13b), yani bir kural ancak önceki barların hareketini görebilirdi.\n"
        "- Bu sayıların hiçbiri bir kuralın kazancı DEĞİLDİR; yalnızca yolun şeklidir.\n"
        "- Dönem B bu koşuda HİÇ okunmadı.\n"
    )
    return "\n".join(out)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(level=getattr(logging, args.log_level.upper()),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    payload = diagnose(
        out_dir=Path(args.out),
        history_bars=args.history_bars,
        funding_periods=args.funding_periods,
        symbols=[s.strip() for s in args.symbols.split(",") if s.strip()] if args.symbols else None,
        models=[m.strip() for m in args.models.split(",") if m.strip()] if args.models else None,
        config_path=args.config,
    )

    if args.results:
        path = Path(args.results)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=1, default=str), encoding="utf-8")
        logger.info("sonuç yazıldı: %s", path)

    if args.summary:
        # Pozisyon satırları çıkarılmış yük: artifact her ortamdan indirilemiyor, yani
        # sonucun depoya girmesi log'dan okunmasına bağlı — ve 369 satırlık yol dizisi
        # log'da okunabilir bir boyda değil. Sayı KOPYALANMAZ, seçilir (site_payload'ın
        # `breakdowns`ı çıkarmasıyla aynı gerekçe).
        path = Path(args.summary)
        path.parent.mkdir(parents=True, exist_ok=True)
        compact = {key: value for key, value in payload.items() if key != "paths"}
        path.write_text(json.dumps(compact, indent=1, default=str), encoding="utf-8")
        logger.info("özet yazıldı: %s", path)

    print(format_report(payload))
    return 0 if payload["determinism"]["passed"] else 1


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="ema_trend dönem A yol teşhisi (Adım 0b). Dönem B parametresi YOKTUR."
    )
    parser.add_argument("--out", default="backtests/ema-diag-a")
    parser.add_argument("--results", default=None, help="makine okunur TAM yükün yolu (JSON)")
    parser.add_argument("--summary", default=None, help="pozisyon satırları çıkarılmış yük (JSON)")
    parser.add_argument("--history-bars", type=int, default=12000)
    parser.add_argument("--funding-periods", type=int, default=6000)
    parser.add_argument("--symbols", default=None, help="virgülle; boş = katmanın 13 sembolü")
    parser.add_argument("--models", default=None, help="virgülle; boş = katmanın listesi")
    parser.add_argument("--config", default=None)
    parser.add_argument("--log-level", default="INFO")
    return parser.parse_args(argv)


if __name__ == "__main__":
    sys.exit(main())
