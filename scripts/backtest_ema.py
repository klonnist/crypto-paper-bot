#!/usr/bin/env python3
"""`ema_trend` ön-kayıtlı koşusu: iki dönem × (portföy + coin başına), tek çıktı.

**Bu araç ikinci bir backtest DEĞİLDİR.** Tek yaptığı `scripts/backtest.py::run_backtest`i
ön-kayıtta (docs/backtest.md > 6d) sabitlenen sırayla çağırmak ve sonuçları tek bir
versiyonlanabilir yüke indirmektir. Hiçbir metrik burada hesaplanmaz: her sayı
`core/metrics.py`den gelir (kural 7). Burada bir ortalama, bir kâr faktörü ya da bir
drawdown hesaplansaydı, aynı defterin iki farklı cevabı olurdu.

## Neden İKİ koşu sınıfı

- **Portföy koşusu** (13 sembol birlikte): canlının gerçekte yapacağı şey. Portföy kotası
  (`max_positions: 5`) burada BAĞLAR ve sinyal reddeder — kabul çıtası (C-1..C-4) bu
  koşudan okunur, çünkü çıta canlıya alma kararını verir.
- **Coin başına koşu** (her sembol tek başına): dış bir referansla (TradingView) kıyasın
  tek geçerli biçimi ve model sahibinin K-1 kapısının birimi. Kota burada bağlamaz, yani
  "bu coin ne yaptı" sorusunun cevabı kotanın değil sinyalin ölçüsüdür.

İki koşunun sayıları TOPLANMAZ ve karıştırılmaz; ayrı bölümlerde durur.

## Dönem ataması ve embargo

Dönem A'nın sinyal kesimi ön-kayıtta sabittir; kesimden sonraki barlar pozisyon yönetimi
için işlenmeye devam eder, yani A'da açılan bir işlem sınırı aşsa bile kapanışına kadar
A'ya sayılır (`--signal-cutoff`).

Dönem B'nin embargosu VARSAYILMAZ, A'dan ÖLÇÜLÜR: model zaman stop'u taşımadığı için
`docs/backtest.md > 6.1`in dayandığı üst sınır (azami tutuş süresi) tanım gereği yoktur.
Bu yüzden B, A'nın koşusu bitmeden başlatılamaz — sıra bir tercih değil, zorunluluktur.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd  # noqa: E402

from core.config import get_setting, load_config  # noqa: E402
from core.layers import resolve_layer  # noqa: E402
from core.metrics import format_report  # noqa: E402
from scripts.backtest import BacktestResult, format_fill_ambiguity, run_backtest, results_payload  # noqa: E402
from main import jsonable  # noqa: E402

logger = logging.getLogger("backtest-ema")

MODEL = "ema_trend"
LAYER = "ema"

# Ön-kayıtlı pencereler (docs/backtest.md > 6d). Sonuca göre KAYDIRILMAZ (§7.3); CLI
# bayrakları yalnızca yeniden üretim ve hata ayıklama içindir.
PERIOD_A_START = "2022-01-01T00:00:00+00:00"
PERIOD_A_CUTOFF = "2024-06-30T00:00:00+00:00"
PERIOD_A_TAIL_END = "2024-12-31T00:00:00+00:00"
PERIOD_B_END = None  # varsayılan: koşu anı

# Ön-kayıtlı maliyet varsayımı: model sahibinin TradingView koşusununki. Canlı config
# DEĞİŞMEZ (kural 6); sapma burada, açıkça ve manifest'e yazılarak uygulanır.
FEE_RATE = 0.00075
SLIPPAGE_BASE = 0.0001

# Kaynak sistemin BTCUSDT referansı (P1 kapısı). Bu sayılar model sahibinden geldi ve
# koşudan ÖNCE yazıldı; sapma toleransı da öyle.
TV_REFERENCE = {"A": 1.551, "B": 1.299}
TV_TOLERANCE = 0.2

# Model sahibinin EK kapıları (K-1..K-3). Repo kapılarının (C-1..C-5) yerine geçmez.
K1_MIN_COINS = 6
K1_MIN_PROFIT_FACTOR = 1.1
K2_MIN_TRADES = 300
K3_MAX_DRAWDOWN_PCT = 25.0


# --------------------------------------------------------------------------- #
# Koşular
# --------------------------------------------------------------------------- #
def run_period(
    *,
    name: str,
    start: pd.Timestamp,
    end: pd.Timestamp,
    out_root: Path,
    symbols: Sequence[str],
    models: Sequence[str],
    history_bars: int,
    funding_periods: int,
    signal_cutoff: pd.Timestamp | None = None,
    embargo_bars: int = 0,
    singles: bool = True,
    config_path: str | None = None,
) -> dict[str, Any]:
    """Bir dönemin portföy koşusu + (istenirse) coin başına koşuları."""
    shared = dict(
        layer_name=LAYER,
        start=start,
        end=end,
        history_bars=history_bars,
        funding_periods=funding_periods,
        fee_rate=FEE_RATE,
        slippage_base=SLIPPAGE_BASE,
        signal_cutoff=signal_cutoff,
        embargo_bars=embargo_bars or None,
        config_path=config_path,
    )

    logger.info("[%s] portföy koşusu: %d sembol, %d model", name, len(symbols), len(models))
    portfolio = run_backtest(
        out_dir=out_root / f"{name}-portfolio", models=list(models), symbols=list(symbols), **shared
    )

    per_coin: dict[str, BacktestResult] = {}
    if singles:
        for symbol in symbols:
            logger.info("[%s] tek sembol: %s", name, symbol)
            try:
                per_coin[symbol] = run_backtest(
                    out_dir=out_root / f"{name}-{symbol}",
                    models=[MODEL, "trend"],
                    symbols=[symbol],
                    **shared,
                )
            except Exception as exc:  # noqa: BLE001
                # Bir sembolün düşmesi (ör. o pencerede hiç barı yok) diğerlerini
                # düşürmez; ama SESSİZ de geçmez — yük onu `failed` olarak taşır.
                logger.error("[%s] %s koşulamadı: %s", name, symbol, exc)
                per_coin[symbol] = exc  # type: ignore[assignment]

    return {"portfolio": portfolio, "per_coin": per_coin}


def measured_embargo_bars(result: BacktestResult, *, model: str = MODEL) -> int:
    """Dönem A'da gözlenen AZAMİ tutuş süresi, bar cinsinden yukarı yuvarlanmış.

    Ön-kayıt (docs/backtest.md > 6d) bunu böyle sabitledi: model zaman stop'u taşımadığı
    için embargo varsayılamaz. Ölçüm yoksa 0 döner ve bu SÖYLENİR — sessiz bir sıfır,
    "örtüşme yok" ile "ölçemedik"i aynı hücreye yazardı.
    """
    stats = result.holding.get(model) or {}
    value = stats.get("max_bars")
    if value is None or not isinstance(value, (int, float)) or value != value:
        logger.warning(
            "%s: dönem A'da kapanmış pozisyon yok, embargo ÖLÇÜLEMEDİ -> 0 bar. "
            "Dönem B, A'nın kurulumlarıyla örtüşebilir.", model,
        )
        return 0
    return int(math.ceil(float(value)))


# --------------------------------------------------------------------------- #
# Yük: coin başına tablo
# --------------------------------------------------------------------------- #
def coin_rows(
    period: str, runs: Mapping[str, Any], *, initial_capital: float
) -> list[dict[str, Any]]:
    """Coin başına satırlar. Hiçbir sayı burada HESAPLANMAZ, yalnızca seçilir.

    `max_drawdown_pct` ön-kayıtlı tanımdır (K-3): kümülatif PnL eğrisinin en büyük
    düşüşünün BAŞLANGIÇ SERMAYESİNE oranı. Hesap düzeyi `max_drawdown` (özsermaye
    eğrisinden) ayrıca ve AYRI bir kolonda taşınır — tek sembollü bir koşuda ikisi de
    tanımlıdır ve farkları bilgidir, ama kapı ön-kayıtlı olanla ölçülür.
    """
    from core.metrics import pnl_drawdown_pct  # gecikmeli: tek tanım core'da

    rows: list[dict[str, Any]] = []
    for symbol, result in runs["per_coin"].items():
        if isinstance(result, Exception):
            rows.append({"period": period, "symbol": symbol, "failed": str(result)})
            continue
        buy_hold = result.buy_hold.get(symbol, float("nan"))
        # Kapsam kolonları: hangi sonucun kaç yıllık veriye dayandığı satırın KENDİSİNDE
        # görünmeli. Sabit evren listesi, sembolün o pencerede var olduğu anlamına gelmez.
        coverage = result.coverage.get(symbol) or {}
        bars = coverage.get("bars") or 0
        for metrics in result.metrics:
            total = metrics.total
            # `DirectionStats`i sözlüğe indirgemek yerine alan alan seçiyoruz: yükün
            # kolonları ön-kayıtta sayılıdır ve tam olarak onlar okunmalı.
            rows.append(
                {
                    "period": period,
                    "symbol": symbol,
                    "model": metrics.model,
                    "first_bar": coverage.get("first_bar"),
                    "last_bar": coverage.get("last_bar"),
                    "bars": bars,
                    # Bar süresi katmanın ayarıdır (4H); yıl karşılığı okuyucunun
                    # "bu sayı kaç yıllık veriye dayanıyor" sorusunun doğrudan cevabıdır.
                    "years": round(bars * 4.0 / 24.0 / 365.0, 2) if bars else 0.0,
                    "trades": total.trades,
                    "win_rate_pct": _pct(total.win_rate),
                    "profit_factor": total.profit_factor,
                    "avg_r": total.avg_r,          # = expectancy (R biriminde özdeşlik)
                    "avg_win_pct": total.avg_win_pct,
                    "avg_loss_pct": total.avg_loss_pct,
                    "payoff": total.payoff,
                    "max_drawdown_pct": pnl_drawdown_pct(total, initial_capital=initial_capital),
                    "account_max_drawdown_pct": _pct(metrics.account.max_drawdown),
                    "total_return_pct": _pct(metrics.account.total_return),
                    "buy_hold_pct": buy_hold,
                    "cost_pct": total.cost_pct,
                    "cost_per_r": total.cost_per_r,
                    "fees": total.fees,
                    "slippage_cost": total.slippage_cost,
                    "funding": total.funding,
                    "avg_stop_distance_pct": total.avg_stop_distance_pct,
                    "median_hold_bars": (result.holding.get(metrics.model) or {}).get("median_bars"),
                    "max_hold_bars": (result.holding.get(metrics.model) or {}).get("max_bars"),
                }
            )
    return rows


def _pct(value: float | None) -> float:
    """Oranı yüzdeye çevirir; `nan` korunur (0.0'a düşürülmez)."""
    if value is None:
        return float("nan")
    return float(value) * 100.0


# --------------------------------------------------------------------------- #
# Kapılar
# --------------------------------------------------------------------------- #
def evaluate_gates(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Ön-kayıtlı kapıları MEKANİK olarak uygular (docs/backtest.md > 6d).

    Burada hiçbir eşik seçilmez; hepsi bu modülün başındaki ön-kayıtlı sabitlerdir.
    Repo kapıları (C-1..C-4) `core/metrics.py::acceptance_flags`ten okunur — ikinci bir
    uygulama, canlı tablo ile backtest tablosunun farklı çıta göstermesi demekti.
    """
    coins = [
        row for row in payload["coins"]
        if row.get("model") == MODEL and not row.get("failed")
    ]
    period_b = [row for row in coins if row["period"] == "B"]

    k1_pass = [
        row["symbol"] for row in period_b
        if _finite(row["profit_factor"]) and row["profit_factor"] > K1_MIN_PROFIT_FACTOR
    ]
    total_trades = sum(row["trades"] for row in coins)
    k3_breach = [
        {"symbol": row["symbol"], "period": row["period"], "drawdown_pct": row["max_drawdown_pct"]}
        for row in coins
        if _finite(row["max_drawdown_pct"]) and abs(row["max_drawdown_pct"]) > K3_MAX_DRAWDOWN_PCT
    ]

    btc = {
        row["period"]: row["profit_factor"]
        for row in coins
        if row["symbol"].startswith("BTC-")
    }
    p1 = {
        period: {
            "reference": reference,
            "measured": btc.get(period),
            "deviation": (
                None if not _finite(btc.get(period)) else btc[period] - reference
            ),
            "passed": (
                _finite(btc.get(period)) and abs(btc[period] - reference) <= TV_TOLERANCE
            ),
        }
        for period, reference in TV_REFERENCE.items()
    }

    repo = {
        period: next(
            (flag for flag in data.get("acceptance", ()) if flag["model"] == MODEL), None
        )
        for period, data in payload["periods"].items()
    }

    gates: dict[str, Any] = {
        "P1_tradingview_parity": {
            "tolerance": TV_TOLERANCE,
            "periods": p1,
            "passed": all(item["passed"] for item in p1.values()),
        },
        "K1_period_b_profit_factor": {
            "threshold": K1_MIN_PROFIT_FACTOR,
            "required_coins": K1_MIN_COINS,
            "coins": k1_pass,
            "passed": len(k1_pass) >= K1_MIN_COINS,
        },
        "K2_total_trades": {
            "threshold": K2_MIN_TRADES,
            "measured": total_trades,
            "passed": total_trades > K2_MIN_TRADES,
        },
        "K3_max_drawdown": {
            "threshold_pct": K3_MAX_DRAWDOWN_PCT,
            "breaches": k3_breach,
            "passed": not k3_breach,
        },
        "repo_acceptance": repo,
    }
    # Karar METNİ de yükün içindedir: sayfanın (docs/backtest.html) kapı sonuçlarına
    # bakıp kendi kararını türetmesi, kural 7'nin ölçüm için koyduğu sınırın aynısını
    # karar kuralı için delerdi — iki yol bugün hizalansa bile yarın ayrışır ve aynı
    # koşu iki yerde iki farklı sonuç gösterirdi.
    gates["verdict"] = _verdict(gates)
    return gates


def _finite(value: Any) -> bool:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return number == number and not math.isinf(number)


def format_gates(gates: Mapping[str, Any]) -> str:
    """Kapıları insan için basar. İSTİSNA kuralı burada da yazılıdır (ön-kayıt).

    Model YALNIZCA çıpa koşulundan (C-3) kalıyorsa karar otomatik değildir ve bu satır
    bunu söyler; başka herhangi bir kapıdan kalırsa sonuç BLOKE'dur.
    """
    out = ["", "=" * 78, "KAPILAR (docs/backtest.md > 6d — ön-kayıtlı)", "=" * 78]

    p1 = gates["P1_tradingview_parity"]
    out.append(f"\nP1 — TradingView paritesi (BTC tek sembollü, tolerans ±{p1['tolerance']}):")
    for period, item in sorted(p1["periods"].items()):
        measured = item["measured"]
        deviation = item["deviation"]
        measured_text = "—" if measured is None else f"{measured:.3f}"
        deviation_text = "—" if deviation is None else f"{deviation:+.3f}"
        out.append(
            f"  Dönem {period}: referans {item['reference']:.3f}  "
            f"ölçülen {measured_text}  sapma {deviation_text}  "
            f"-> {'GEÇTİ' if item['passed'] else 'DÜŞTÜ'}"
        )
    if not p1["passed"]:
        out.append(
            "  !! P1 bir TAHMİN DEĞİL, KAPIDIR: düştüğünde hiçbir sayı yorumlanmaz ve\n"
            "     paper trading'e geçilmez. Önce harness ↔ kaynak sistem farkı açıklanır."
        )

    k1 = gates["K1_period_b_profit_factor"]
    out.append(
        f"\nK-1 — Dönem B'de kâr faktörü > {k1['threshold']}: "
        f"{len(k1['coins'])}/{k1['required_coins']} coin -> {'GEÇTİ' if k1['passed'] else 'DÜŞTÜ'}"
    )
    if k1["coins"]:
        out.append("     " + ", ".join(k1["coins"]))

    k2 = gates["K2_total_trades"]
    out.append(
        f"K-2 — toplam işlem > {k2['threshold']}: {k2['measured']} "
        f"-> {'GEÇTİ' if k2['passed'] else 'DÜŞTÜ'}"
    )

    k3 = gates["K3_max_drawdown"]
    out.append(
        f"K-3 — coin başına drawdown <= %{k3['threshold_pct']:.0f}: "
        f"{'GEÇTİ' if k3['passed'] else 'DÜŞTÜ'}"
    )
    for breach in k3["breaches"]:
        out.append(f"     AŞIM {breach['symbol']} ({breach['period']}): %{breach['drawdown_pct']:.2f}")

    out.append("\nREPO KAPILARI (core/metrics.py::acceptance_flags — canlıyla aynı hesap):")
    for period, flag in sorted(gates["repo_acceptance"].items()):
        if flag is None:
            out.append(f"  Dönem {period}: model tabloda yok")
            continue
        out.append(
            f"  Dönem {period}: örneklem {'✓' if flag['sample'] else '✗'} "
            f"(n={flag['measured_trades']}/{flag['min_trades']})  "
            f"edge {'✓' if flag['edge'] else '✗'} "
            f"(ort.R {flag['avg_r']:+.3f} ↔ kontrol {flag['control_avg_r']:+.3f})  "
            f"band {'✓' if flag['band'] else '⚠'}  "
            f"getiri {flag['total_return'] * 100:+.2f}% ↔ çıpa {flag['benchmark_return'] * 100:+.2f}%  "
            f"-> {'GEÇTİ' if flag['passed'] else 'DÜŞTÜ'}"
        )

    out.append("")
    out.append(_verdict(gates))
    return "\n".join(out) + "\n"


def _verdict(gates: Mapping[str, Any]) -> str:
    """Ön-kayıtlı karar kuralı. Tek istisna, sonuçtan ÖNCE yazıldı (docs/backtest.md > 6d)."""
    if not gates["P1_tradingview_parity"]["passed"]:
        return (
            "SONUÇ: P1 DÜŞTÜ -> hiçbir sayı yorumlanmaz, paper trading'e GEÇİLMEZ.\n"
            "Sıradaki iş kaynak sistemle farkın açıklanmasıdır (ATR tanımı, veri kaynağı,\n"
            "dolum, maliyet), ayar araması DEĞİL (docs/backtest.md > 7.1)."
        )

    owner_gates = ["K1_period_b_profit_factor", "K2_total_trades", "K3_max_drawdown"]
    owner_failed = [key for key in owner_gates if not gates[key]["passed"]]
    repo_flags = [flag for flag in gates["repo_acceptance"].values() if flag]
    repo_failed = [flag for flag in repo_flags if not flag["passed"]]

    if owner_failed:
        return f"SONUÇ: BLOKE — model sahibinin kapıları düştü: {', '.join(owner_failed)}"

    if not repo_failed:
        return "SONUÇ: TÜM KAPILAR GEÇİLDİ -> paper trading'e alınabilir (C-5 dâhil)."

    # Yalnızca çıpa koşulundan (C-3) kalma durumu: karar otomatik değildir.
    only_benchmark = all(
        flag["sample"] and flag["avg_r"] > 0.0
        and flag["avg_r"] - flag["control_avg_r"] >= flag["edge_margin_r"]
        and flag["total_return"] <= flag["benchmark_return"]
        for flag in repo_failed
    )
    if only_benchmark:
        return (
            "SONUÇ: DUR — model yalnızca ÇIPA koşulundan (C-3) kalıyor; diğer kapıları\n"
            "geçiyor. Ön-kayıt bu durumda otomatik geçiş YASAKLAR: karar model sahibinindir."
        )
    return "SONUÇ: BLOKE — repo kabul kapıları düştü (çıpa dışındaki koşullar)."


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper()),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    config = load_config(args.config)
    layer = resolve_layer(config, LAYER)
    symbols = list(args.symbols.split(",")) if args.symbols else list(layer.symbols or ())
    initial_capital = float(get_setting(layer.config, "initial_capital"))
    out_root = Path(args.out)

    a_start = pd.Timestamp(args.a_start)
    a_cutoff = pd.Timestamp(args.a_cutoff)
    a_end = pd.Timestamp(args.a_tail_end)
    b_end = pd.Timestamp(args.b_end) if args.b_end else pd.Timestamp.now(tz="UTC")

    logger.warning(
        "MALİYET: bu koşu fee_rate=%.5f slippage_base=%.5f ile koşuyor; canlı config "
        "%.5f/%.5f. Sapma ön-kayıtlıdır (docs/backtest.md > 6d) ve canlı config'e "
        "DOKUNULMAZ.", FEE_RATE, SLIPPAGE_BASE,
        float(get_setting(layer.config, "fee_rate")),
        float(get_setting(layer.config, "slippage_base")),
    )

    # Koşu tek işe sığmazsa ikiye bölünebilir (--only). Bölünme ölçümü DEĞİŞTİRMEZ ama
    # bir şartı vardır: dönem B'nin embargosu A'da ÖLÇÜLÜR ve uydurulamaz. Bu yüzden
    # `--only B` embargoyu dışarıdan İSTER; varsayılan bir değere düşmek, A'nın ölçtüğü
    # sayıyı sessizce bir tahminle değiştirmek olurdu.
    if args.only == "B" and args.embargo_bars is None:
        logger.error(
            "--only B için --embargo-bars zorunlu: embargo dönem A'da ÖLÇÜLÜR "
            "(docs/backtest.md > 6d) ve varsayılamaz."
        )
        return 2

    period_a: dict[str, Any] | None = None
    if args.only != "B":
        period_a = run_period(
            name="A", start=a_start, end=a_end, out_root=out_root, symbols=symbols,
            models=list(layer.models), history_bars=args.history_bars,
            funding_periods=args.funding_periods, signal_cutoff=a_cutoff,
            singles=not args.skip_singles, config_path=args.config,
        )
        embargo = measured_embargo_bars(period_a["portfolio"])
        logger.info(
            "embargo ÖLÇÜLDÜ: dönem A'da azami tutuş %d bar -> dönem B o kadar ileriden "
            "başlar. İkinci iş `--only B --embargo-bars %d` ile koşulur.", embargo, embargo,
        )
    else:
        embargo = int(args.embargo_bars or 0)
        logger.info("embargo DIŞARIDAN verildi (dönem A koşusundan): %d bar", embargo)

    period_b: dict[str, Any] | None = None
    if args.only != "A":
        period_b = run_period(
            name="B", start=a_cutoff, end=b_end, out_root=out_root, symbols=symbols,
            models=list(layer.models), history_bars=args.history_bars,
            funding_periods=args.funding_periods, embargo_bars=embargo,
            singles=not args.skip_singles, config_path=args.config,
        )

    payload: dict[str, Any] = {
        "model": MODEL,
        "layer": LAYER,
        "preregistration": "docs/backtest.md > 6d",
        "costs": {"fee_rate": FEE_RATE, "slippage_base": SLIPPAGE_BASE},
        "embargo_bars": embargo,
        "symbols": symbols,
        "periods": {
            name: results_payload(runs["portfolio"])
            for name, runs in (("A", period_a), ("B", period_b))
            if runs is not None
        },
        "coins": [
            row
            for name, runs in (("A", period_a), ("B", period_b))
            if runs is not None
            for row in coin_rows(name, runs, initial_capital=initial_capital)
        ],
    }
    # Kapılar yalnızca İKİ dönem de elde olduğunda değerlendirilir: K-1 dönem B'nin,
    # K-2 ikisinin toplamının kapısıdır. Yarım bir yükten "geçti" çıkarmak, kapının
    # kendisini yarıya indirmek olurdu.
    if period_a is not None and period_b is not None:
        payload["gates"] = evaluate_gates(payload)
    else:
        payload["gates"] = None
        logger.warning(
            "koşu bölündü (--only %s): KAPILAR DEĞERLENDİRİLMEDİ. İki dönemin yükü "
            "birleştirilmeden kapı okunamaz.", args.only,
        )

    results = Path(args.results)
    results.parent.mkdir(parents=True, exist_ok=True)
    results.write_text(
        json.dumps(jsonable(payload), ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    _write_csv(Path(args.csv), payload["coins"])
    logger.info("yük: %s ve %s", results, args.csv)

    if args.site_json:
        site = Path(args.site_json)
        site.parent.mkdir(parents=True, exist_ok=True)
        site.write_text(
            json.dumps(jsonable(site_payload(payload)), ensure_ascii=False, indent=1, default=str),
            encoding="utf-8",
        )
        logger.info("site yükü: %s", site)

    for name, runs in (("A", period_a), ("B", period_b)):
        if runs is None:
            continue
        print(f"\n{'=' * 78}\nDÖNEM {name} — PORTFÖY KOŞUSU\n{'=' * 78}")
        print(format_report(list(runs["portfolio"].metrics), min_trades=runs["portfolio"].min_trades))
        print(format_fill_ambiguity(runs["portfolio"].report))
    if payload["gates"] is not None:
        print(format_gates(payload["gates"]))

    return 0


def site_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Tam yükün SAYFANIN OKUDUĞU kısmı — kırılımlar çıkarılmış hâli.

    **Neden ayrı bir dosya.** İş salt okunur (`permissions: contents: read`) ve sonuçlar
    runner'dan yalnızca artifact ile ya da LOG ile çıkabilir. Artifact her ortamdan
    indirilemiyor (blob deposuna erişim ağ politikasına bağlı), yani sonucun depoya
    girmesi pratikte log'dan okunmasına bağlı — ve o zaman da yükün okunabilir bir boyda
    olması gerekir.

    Çıkarılan tek şey `breakdowns`tır ve gerekçesi şudur: `docs/backtest.html` onu HİÇ
    okumaz (sembol kırılımının sayıları zaten `coins` satırlarında, tek sembollü
    koşulardan gelir), ama yükün en büyük parçasıdır. Kırılımlar TAM yükte ve
    artifact'te durmaya devam eder — bu bir silme değil, sayfanın okumadığı bir bölümün
    sayfanın dosyasına konmaması.

    Sayı KOPYALANMAZ, seçilir: aynı `payload` sözlüğünün alt kümesidir.
    """
    return {
        key: value for key, value in payload.items() if key != "periods"
    } | {
        "periods": {
            name: {k: v for k, v in period.items() if k != "breakdowns"}
            for name, period in (payload.get("periods") or {}).items()
        },
    }


def _write_csv(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    """Coin tablosunu CSV'ye yazar. Kolon kümesi satırlardan türetilir, elle yazılmaz."""
    if not rows:
        logger.warning("coin satırı yok, CSV yazılmadı")
        return
    columns: list[str] = []
    for row in rows:
        for key in row:
            if key not in columns:
                columns.append(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="ema_trend ön-kayıtlı backtest koşusu (docs/backtest.md > 6d)."
    )
    parser.add_argument("--a-start", default=PERIOD_A_START)
    parser.add_argument("--a-cutoff", default=PERIOD_A_CUTOFF, help="dönem A sinyal kesimi")
    parser.add_argument(
        "--a-tail-end", default=PERIOD_A_TAIL_END,
        help="A'nın kuyruğu: kesimden sonra pozisyonların kapanması için işlenen son bar",
    )
    parser.add_argument("--b-end", default=PERIOD_B_END, help="varsayılan: koşu anı")
    parser.add_argument("--symbols", default=None, help="virgülle; boş = katmanın evreni")
    parser.add_argument("--history-bars", type=int, default=12000)
    parser.add_argument("--funding-periods", type=int, default=6000)
    parser.add_argument("--out", default="backtests/ema", help="ham defterlerin kökü")
    parser.add_argument("--results", default="docs/data/backtest_ema_trend.json")
    parser.add_argument("--csv", default="docs/data/backtest_ema_trend.csv")
    parser.add_argument(
        "--site-json", default=None, metavar="PATH",
        help=(
            "sayfanın okuduğu yük (kırılımlar hariç). Sonuç runner'dan yalnızca artifact "
            "ya da log ile çıkar; artifact her ortamdan indirilemediği için yükün log'a "
            "sığacak boyda bir sürümü gerekir."
        ),
    )
    parser.add_argument("--config", default=None)
    parser.add_argument(
        "--only", choices=["A", "B"], default=None,
        help=(
            "yalnızca bu dönemi koş. B'yi tek başına koşmak --embargo-bars GEREKTİRİR: "
            "embargo A'dan ölçülür ve uydurulamaz (docs/backtest.md > 6d)."
        ),
    )
    parser.add_argument(
        "--embargo-bars", type=int, default=None, metavar="N",
        help=(
            "A'da ÖLÇÜLMÜŞ embargoyu dışarıdan verir; yalnızca koşu iki işe bölündüğünde "
            "(--only B) kullanılır. Değer A'nın çıktısından gelir, seçilmez."
        ),
    )
    parser.add_argument(
        "--skip-singles", action="store_true",
        help="yalnızca portföy koşusu (hata ayıklama; coin tablosu ÜRETİLMEZ)",
    )
    parser.add_argument(
        "--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"]
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    sys.exit(main())
