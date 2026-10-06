"""Scalp kollarının KURULUM ve KAPI sayımını ölçer. SALT OKUNUR: ölçüm aracı, model değil.

Cevapladığı soru tek: *"yapısal engel girişin ÖNÜNDE olmak zorunda, yoksa kurulum yok"
kuralıyla kol bazında kaç kurulum kapıdan geçer ve bu, n=30'a ulaşılabilir bir sıklık mı?*
(karar 53; hipotezin kaynağı: karar 34'ün kapı aritmetiği ve karar 52 > S2.) Getiri/R'ye
BAKMAZ: yalnızca kurulum ve kapı sayıları (docs/backtest.md > 7: ön-kayıt commit'lenmeden
hiçbir getiri/R sayısı okunmaz; sıklık ölçümü serbesttir).

**Neden kendi kol/kapı mantığını yazmıyor.** Her barda GERÇEK `ScalpPatient.generate_signals`
çalıştırılır ve sayım modelin kendi `take_survey()`inden okunur (karar 52 > P1a): kollar
`strategies/scalp/arms.py::propose_all`dan, kapılar `ScalpModel._gate_reason`dan, `engel_onde`
↔ `engel_geride_veya_uzak` ayrımı `ScalpModel._target_kind`dan gelir. Bu araç yalnızca
barları gezer ve toplar; ikinci bir uygulama, ölçtüğü modelden sessizce ayrışabilecek bir sayı
üretirdi (`scripts/measure_vwap_signal.py` ile aynı gerekçe).

**Hızlandırma bir varsayımdır ve KANITLANIR.** Her bar için yalnızca son `--tail` bar verilir
(varsayılan 400; en uzun kol penceresi EMA50/Bollinger20/gün VWAP'i, yani ~100 bar). `--verify`
rastgele barlarda aynı sayımı TÜM geçmişle de üretip karşılaştırır; farklıysa script hata
koduyla biter. Bir ölçüm aracının kendi doğruluğu iddia edilmez, gösterilir.

**Funding verisi YOKTUR** (`market.funding = {}`): `funding_spike_fade` kolu bu ölçümde hiç
kurulum görmez. Bu bir sınırlama DEĞİL bir tutarlılıktır — o kol katmanın tüm canlı ömrü
boyunca da hiç sinyal üretmedi (karar 48) ve geçmiş pencerelerde funding derinliği zaten
yoktur (karar 50: OKX ~3 aylık kayan pencere tutuyor).

**Barda tek sinyal.** Model her barda yalnızca TEK sinyal oynar; bu yüzden "sinyal" sayısı
kapıdan geçen kurulumların değil, **en az bir kolun kapıdan geçen kurulum görmüş olduğu
barların** sayısıdır (TABLO 2). `scalp_thesis` adayı (karar 53) için eşdeğeri, en az bir
kolun `engel_onde` kurulumu görmüş olduğu barlardır.

Rollere dikkat (CLAUDE.md kural 1/2/3/7): deftere YAZMAZ, bakiye/pozisyon/komisyon hesaplamaz,
`config.yaml`ı değiştirmez ve hiçbir modelin davranışına dokunmaz.

Kullanım (depo kökünden):
    python scripts/measure_scalp_arms.py --days 60 --end 2026-09-19 --verify 100
    python scripts/measure_scalp_arms.py --days 60 --end 2026-09-19 --realization 0.185
"""

from __future__ import annotations

import argparse
import logging
import random
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.config import get_setting, load_config  # noqa: E402
from core.data import fetch_ohlcv  # noqa: E402
from core.layers import resolve_layer  # noqa: E402
from strategies.base import MarketData  # noqa: E402
from strategies.registry import build  # noqa: E402
from strategies.scalp.arms import ARM_NAMES  # noqa: E402
from strategies.scalp.model import SURVEY_REASONS, SURVEY_TARGETS, ScalpModel, survey_key  # noqa: E402

logger = logging.getLogger("measure_scalp_arms")

DEFAULT_TAIL = 400
FUNNEL_REASONS: tuple[str, ...] = ("stop_tabani", "rr_kapisi", "rejim", "gecti")


@dataclass(frozen=True, kw_only=True)
class BarCount:
    """Tek bar için modelin `take_survey()` çıktısı (35 anahtar) ve sinyal üretip üretmediği."""

    as_of: pd.Timestamp
    survey: Mapping[str, int]
    signalled: bool

    def arm(self, arm: str, reason: str) -> int:
        return self.survey[survey_key(arm, reason)]

    @property
    def available_arms(self) -> tuple[str, ...]:
        """Kapıdan geçen kurulumu olan kollar (modelin çekilişi bunlar arasından seçer)."""
        return tuple(arm for arm in ARM_NAMES if self.arm(arm, "gecti") > 0)

    @property
    def thesis_arms(self) -> tuple[str, ...]:
        """Kapıdan geçen VE hedefi kolun yapısal engeli olan (`engel_onde`) kurulumu olan kollar."""
        return tuple(arm for arm in ARM_NAMES if self.arm(arm, "engel_onde") > 0)


def build_market(
    frames: Mapping[str, pd.DataFrame], as_of: pd.Timestamp, *, tail: int | None
) -> MarketData:
    """`as_of`a kadar kesilmiş anlık görüntü (kural 12); `tail` verilirse son `tail` bar."""
    cut = {}
    for symbol, frame in frames.items():
        upto = frame.loc[:as_of]
        cut[symbol] = upto if tail is None else upto.iloc[-tail:]
    btc = cut.get("BTC-USDT-SWAP", next(iter(cut.values())))
    return MarketData(ohlcv=cut, btc=btc, funding={}, as_of=as_of)


def count_bar(model: ScalpModel, market: MarketData) -> BarCount:
    signals = model.generate_signals(market)
    survey = model.take_survey()
    if survey is None:
        raise RuntimeError(f"{model.name}: take_survey() None döndü; sayım okunamadı")
    return BarCount(as_of=market.as_of, survey=dict(survey), signalled=bool(signals))


def measure(
    frames: Mapping[str, pd.DataFrame],
    model: ScalpModel,
    *,
    start: pd.Timestamp,
    end: pd.Timestamp,
    tail: int | None,
) -> list[BarCount]:
    anchor = frames.get("BTC-USDT-SWAP", next(iter(frames.values())))
    stamps = [ts for ts in anchor.index if start <= ts <= end]
    return [count_bar(model, build_market(frames, ts, tail=tail)) for ts in stamps]


def verify(
    frames: Mapping[str, pd.DataFrame],
    model: ScalpModel,
    counted: Sequence[BarCount],
    *,
    tail: int,
    sample: int,
    seed: int,
) -> None:
    """Hızlı yolun (son `tail` bar) TÜM geçmişle AYNI sayımı verdiğini KANITLAR."""
    if not counted:
        raise SystemExit("doğrulanacak bar yok")
    chosen = random.Random(seed).sample(list(counted), min(sample, len(counted)))
    for item in chosen:
        full = count_bar(model, build_market(frames, item.as_of, tail=None))
        if dict(full.survey) != dict(item.survey) or full.signalled != item.signalled:
            diff = {
                key: (item.survey[key], full.survey[key])
                for key in item.survey
                if item.survey[key] != full.survey[key]
            }
            raise SystemExit(
                f"DOĞRULAMA BAŞARISIZ {item.as_of}: tail={tail} ↔ tam geçmiş farkı {diff}"
            )
    logger.info("doğrulama geçti: %d barda tail=%d ile tam geçmiş birebir aynı sayımı verdi",
                len(chosen), tail)


# --------------------------------------------------------------------------- #
# Tablolar
# --------------------------------------------------------------------------- #
def arm_funnel_table(counted: Sequence[BarCount], *, weeks: float) -> str:
    """TABLO 1 — kol bazında huni (birim: SEMBOL-BAR) ve `engel_onde` payı."""
    lines = [
        "TABLO 1 — KOL HUNİSİ  (birim: sembol-bar; `kurulum` = kolun ürettiği aday)",
        f"{'kol':<24} {'kurulum':>8} {'stop_tab':>9} {'rr_kapisi':>10} {'rejim':>6} {'GEÇEN':>7} "
        f"{'engel_önde':>11} {'geride/uzak':>12} {'önde%':>6} {'GEÇEN/hf':>9} {'önde/hf':>8}",
    ]
    for arm in ARM_NAMES:
        total = {reason: sum(c.arm(arm, reason) for c in counted) for reason in (*SURVEY_REASONS, *SURVEY_TARGETS)}
        setups = sum(total[reason] for reason in FUNNEL_REASONS)
        passed = total["gecti"]
        share = f"{total['engel_onde'] / passed * 100:>6.1f}" if passed else f"{'—':>6}"
        lines.append(
            f"{arm:<24} {setups:>8} {total['stop_tabani']:>9} {total['rr_kapisi']:>10} "
            f"{total['rejim']:>6} {passed:>7} {total['engel_onde']:>11} "
            f"{total['engel_geride_veya_uzak']:>12} {share} "
            f"{_per_week(passed, weeks):>9} {_per_week(total['engel_onde'], weeks):>8}"
        )
    return "\n".join(lines)


def model_table(counted: Sequence[BarCount], *, weeks: float) -> str:
    """TABLO 2 — MODEL düzeyi: barda TEK sinyal. Baz (`scalp_patient`) ↔ `engel_onde` adayı."""
    bars = len(counted)
    base = [c for c in counted if c.available_arms]
    thesis = [c for c in counted if c.thesis_arms]
    signalled = sum(1 for c in counted if c.signalled)
    lines = [
        "TABLO 2 — MODEL DÜZEYİ (barda TEK sinyal; birim: BAR)",
        f"  ölçülen bar                         {bars}",
        f"  sinyal üretilen bar (model)         {signalled}   ({_per_week(signalled, weeks)}/hf)",
        f"  ≥1 kol kapıdan geçti (baz)          {len(base)}   ({_per_week(len(base), weeks)}/hf)",
        f"  ≥1 kol `engel_onde` geçti (aday)    {len(thesis)}   ({_per_week(len(thesis), weeks)}/hf)",
    ]
    if base:
        lines.append(f"  aday / baz                          {len(thesis) / len(base):.3f}")
    if signalled != len(base):
        lines.append("  ⚠ model sinyal barı ≠ baz bar sayısı: sayım ile motor ayrışıyor")
    lines.append("  `engel_onde` barlarında kol payı (her barda seçilebilir kollar eşit ağırlık):")
    shares = {arm: 0.0 for arm in ARM_NAMES}
    for item in thesis:
        arms = item.thesis_arms
        for arm in arms:
            shares[arm] += 1.0 / len(arms)
    for arm in ARM_NAMES:
        bars_with = sum(1 for c in thesis if arm in c.thesis_arms)
        lines.append(
            f"    {arm:<24} {bars_with:>5} bar   beklenen pay {shares[arm] / len(thesis) * 100:5.1f}%"
            if thesis
            else f"    {arm:<24} {'—':>5}"
        )
    return "\n".join(lines)


def measurability_table(
    counted: Sequence[BarCount], *, weeks: float, realization: float | None
) -> str:
    """TABLO 3 — n=30 POZİSYONA ulaşma süresi (karar 33'ün ölçülebilirlik ölçütü).

    `tavan` her sinyalin pozisyona dönüştüğü varsayımıdır (üst sınır). Gerçekleşen oran
    reddedilen sinyaller (`duplicate_position`, kota, nakit) kadar daha düşüktür ve bu
    araçta ÖLÇÜLMEZ — `--realization` defterden okunan oranı (pozisyon/sinyal) verir.
    """
    base = sum(1 for c in counted if c.available_arms)
    thesis = sum(1 for c in counted if c.thesis_arms)
    header = f"{'küme':<10} {'sinyal/hf':>10} {'30→hafta (tavan)':>17} {'30→gün (tavan)':>15}"
    if realization:
        header += f" {'30→gün (oran=' + f'{realization:g}' + ')':>20}"
    lines = ["TABLO 3 — ÖLÇÜLEBİLİRLİK: n=30 pozisyon için süre", header]
    for label, count in (("baz", base), ("aday", thesis)):
        weekly = count / weeks if weeks else float("nan")
        to30_weeks = 30.0 / weekly if weekly > 0 else float("inf")
        row = (
            f"{label:<10} {weekly:>10.2f} {_inf(to30_weeks):>17} {_inf(to30_weeks * 7):>15}"
        )
        if realization:
            row += f" {_inf(to30_weeks * 7 / realization):>20}"
        lines.append(row)
    return "\n".join(lines)


def _per_week(count: float, weeks: float) -> str:
    return f"{count / weeks:.1f}" if weeks else "—"


def _inf(value: float) -> str:
    return f"{value:.1f}" if np.isfinite(value) else "∞"


def load_frames(config: dict, symbols: Sequence[str]) -> dict[str, pd.DataFrame]:
    frames: dict[str, pd.DataFrame] = {}
    for symbol in symbols:
        frame = fetch_ohlcv(config, symbol)
        if frame.empty:
            logger.warning("%s: veri yok, ölçüm dışı", symbol)
            continue
        frames[symbol] = frame
        logger.info("%s: %d bar (%s .. %s)", symbol, len(frame), frame.index[0], frame.index[-1])
    return frames


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=60, help="ölçüm penceresi (gün)")
    parser.add_argument(
        "--end", default=None,
        help="pencerenin SON günü (UTC, YYYY-MM-DD, dâhil); varsayılan: en yeni bar",
    )
    parser.add_argument("--layer", default="scalp")
    parser.add_argument("--model", default="scalp_patient", help="kapıları belirleyen ScalpModel")
    parser.add_argument("--tail", type=int, default=DEFAULT_TAIL, help="her bara verilen son bar sayısı")
    parser.add_argument("--verify", type=int, default=0, help="kaç barda tail ↔ tam geçmiş kıyaslansın")
    parser.add_argument(
        "--realization", type=float, default=None,
        help="defterden okunan pozisyon/sinyal oranı (TABLO 3'e ikinci sütun ekler)",
    )
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper()),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    # Her bar için modelin log'u (kurulum atlandı …) bu ölçümde gürültüdür.
    logging.getLogger("strategies").setLevel(logging.WARNING)

    layer = resolve_layer(load_config(), args.layer)
    config = dict(layer.config)
    bars_per_day = int(pd.Timedelta("1D") / pd.Timedelta(config["timeframe"]))
    end = None if args.end is None else pd.Timestamp(args.end, tz="UTC") + pd.Timedelta(days=1)
    lookback_days = args.days
    if end is not None:
        lookback_days = int((pd.Timestamp.now(tz="UTC") - (end - pd.Timedelta(days=args.days))).days) + 1
    config["data"] = dict(config["data"])
    config["data"]["history_bars"] = lookback_days * bars_per_day + bars_per_day * 2

    symbols = list(layer.symbols or [])
    if not symbols:
        raise SystemExit(f"{args.layer}: sabit evren yok, ölçüm tanımsız")

    frames = load_frames(config, symbols)
    if not frames:
        raise SystemExit("hiç sembol çekilemedi")
    if end is not None:
        frames = {s: f[f.index < end] for s, f in frames.items()}
        frames = {s: f for s, f in frames.items() if not f.empty}

    model = build(args.model, config=config)
    if not isinstance(model, ScalpModel):
        raise SystemExit(f"{args.model} bir ScalpModel değil: kol/kapı sayımı tanımsız")

    newest = max(frame.index[-1] for frame in frames.values())
    start = newest - pd.Timedelta(days=args.days)
    counted = measure(frames, model, start=start, end=newest, tail=args.tail)
    if not counted:
        raise SystemExit("ölçülecek bar yok")
    weeks = (counted[-1].as_of - counted[0].as_of) / pd.Timedelta(days=7)

    print()
    print("=" * 100)
    print(f"SCALP KOLLARI — KURULUM VE KAPI SAYIMI  katman={args.layer} bar={config['timeframe']} model={args.model}")
    print("=" * 100)
    print(f"pencere      : {counted[0].as_of}  ..  {counted[-1].as_of}")
    print(f"              {weeks:.2f} hafta, {len(counted)} bar, {len(frames)} sembol, tail={args.tail}")
    print(
        f"SABİTLER     : taban=%{float(get_setting(config, 'scalp.min_stop_pct')) * 100:g}  "
        f"çıta={float(get_setting(config, 'scalp.min_reward_risk')):g}R  "
        f"stop={float(get_setting(config, 'scalp.stop_atr_multiple')):g}×ATR  "
        f"hedef=projeksiyon {float(get_setting(config, 'scalp.target_reward_risk')):g}R ∧ kol engeli"
    )
    print("funding      : YOK (funding_spike_fade bu ölçümde kurulum görmez; bkz. modül başlığı)")
    print()
    print(arm_funnel_table(counted, weeks=weeks))
    print()
    print(model_table(counted, weeks=weeks))
    print()
    print(measurability_table(counted, weeks=weeks, realization=args.realization))
    print()

    if args.verify:
        verify(
            frames, model, counted,
            tail=args.tail, sample=args.verify, seed=int(get_setting(config, "random_seed")),
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
