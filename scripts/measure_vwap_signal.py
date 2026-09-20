"""Model 14'ün (vwap_managed) sinyal FREKANSINI ölçer. SALT OKUNUR: ölçüm aracı, model değil.

Cevapladığı soru tek: *model 14 neden hiç sinyal üretmiyor ve hangi parametre bunu
değiştirir?* Tablo, kurulumların hangi kapıda elendiğini sayar — yani "kaç aday vardı",
"kaçı %1 stop tabanına takıldı", "kaçı 1.5R kapısına takıldı", "kaç tanesi geçti".

**Ölçülen eksen `vwap.managed.atr_multiple`dir, `band_mult` DEĞİL.** Gerekçe ölçümün
kendi sonucudur: model 14'te hedef VWAP ile sınırlı, stop ise `atr_multiple × ATR`tır,
yani

    R:R = |z_now| × σ / (atr_multiple × ATR)

Aday olmanın şartı `|z_now| < |z_prev|` (dönüş başlamış olmalı) olduğu için `band_mult`i
DÜŞÜRMEK `|z_now|`ı da düşürür ve R:R'yi küçültür: yeni adaylar aynı kapıda ölür. Bandın
frekans düğmesi olmadığı buradan gelir. Payda ise doğrudan `atr_multiple`dır — eksen odur.
`--bands` ile eski (band) süpürmesi de koşulabilir; ikisi de aynı sınıflandırmayı kullanır.

**Neden kendi z hesabını yazmıyor.** `core.indicators` (anchored_vwap, average_true_range,
bars_until) doğrudan çağrılır ve bant/dönüş karşılaştırmaları `signal.py::_evaluate`in,
kapılar ise `vwap_managed.py::_passes_gates`in birebir aynısıdır. `--verify` bunu rastgele
barlarda `vwap_signal.scan()` ile karşılaştırıp KANITLAR: iki yol aynı adayları vermezse
script hata koduyla biter. Bir ölçüm aracının kendi doğruluğu iddia edilmez, gösterilir.

**Neden bar başına tek `anchored_vwap`.** Eşikler yalnızca KARŞILAŞTIRMADIR; z, ATR ve VWAP
eşiklerden bağımsızdır. Bir kez hesaplayıp N eşiği aynı sayıya uygulamak, N ayrı tarama
koşmakla birebir aynı sonucu verir ve 60 günlük pencereyi dakikalar içinde bitirir.

Rollere dikkat (CLAUDE.md kural 1/2/3/7): deftere YAZMAZ, bakiye/pozisyon/komisyon
hesaplamaz, `config.yaml`ı değiştirmez ve hiçbir modelin davranışına dokunmaz. Model 13'ün
`strategies/vwap/clone_signal.py`si import BİLE EDİLMEZ — ölçülen eksen model 14'ündür.

Kullanım (depo kökünden):
    python scripts/measure_vwap_signal.py --days 60 --verify 200
    python scripts/measure_vwap_signal.py --days 60 --csv /tmp/z.csv
    python scripts/measure_vwap_signal.py --days 60 --bands 1.0,1.5,2.0   # eski eksen
"""

from __future__ import annotations

import argparse
import logging
import random
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.config import get_setting, load_config  # noqa: E402
from core.data import fetch_ohlcv  # noqa: E402
from core.indicators import anchored_vwap, average_true_range, bars_until  # noqa: E402
from core.layers import resolve_layer  # noqa: E402
from strategies.base import Direction, MarketData  # noqa: E402
from strategies.vwap import signal as vwap_signal  # noqa: E402

logger = logging.getLogger("measure_vwap_signal")

# Eleme sebepleri. İlk üçü KOLUN (signal.py), son ikisi MODELİN (vwap_managed.py) kapısıdır.
INSIDE_BAND = "bant_ici"
STILL_EXTENDING = "donus_yok"
CROSSED = "vwap_gecildi"
STOP_FLOOR = "stop_tabani"
RR_GATE = "rr_kapisi"
PASSED = "gecti"

DEFAULT_STOP_SCALES = "1.5,2.0,2.5,3.0,4.0,5.0"


@dataclass(frozen=True, kw_only=True)
class BarPoint:
    """Tek (sembol, bar) için z ve geometri girdileri. HİÇBİR eşikten etkilenmez."""

    symbol: str
    as_of: pd.Timestamp
    close: float
    atr: float
    vwap: float
    deviation: float
    z_prev: float
    z_now: float

    @property
    def sigma_over_atr(self) -> float:
        return self.deviation / self.atr


@dataclass(frozen=True, kw_only=True)
class Candidate:
    """Bant ve dönüş şartını geçmiş kurulum. Kapılar HENÜZ uygulanmamıştır."""

    point: BarPoint
    direction: Direction

    @property
    def extension(self) -> float:
        """`signal.py::VwapCandidate.extension` ile aynı: kurulumun gücü."""
        return abs(self.point.z_prev)


@dataclass(frozen=True, kw_only=True)
class Geometry:
    """Bir adayın verilen stop ölçeğindeki geometrisi."""

    stop_pct: float
    reward_risk: float
    verdict: str


def measure(
    frames: dict[str, pd.DataFrame],
    *,
    atr_period: int,
    min_vwap_bars: int,
    start: pd.Timestamp,
) -> list[BarPoint]:
    """Her (sembol, bar) için z'yi BİR KEZ hesaplar; `signal.py::_evaluate` ile aynı formül.

    Eleme burada yapılmaz: `min_vwap_bars` ve sıfır sapma kontrolü kolun `NO_VWAP` sebebidir
    ve o noktalar hiç ölçülemez — geri kalan her nokta tabloya girer.
    """
    points: list[BarPoint] = []
    for symbol in sorted(frames):
        full = frames[symbol]
        for as_of in full.index:
            if as_of < start:
                continue
            frame = bars_until(full, as_of)
            if len(frame) < 2:
                continue
            atr = average_true_range(frame, atr_period)
            if atr is None or atr <= 0.0:
                continue
            close = float(frame["close"].iloc[-1])
            if close <= 0.0:
                continue
            vwap = anchored_vwap(frame, anchor=as_of.normalize())
            if vwap is None or vwap.bars < min_vwap_bars or vwap.deviation <= 0.0:
                continue
            previous = float(frame["close"].iloc[-2])
            points.append(
                BarPoint(
                    symbol=symbol,
                    as_of=as_of,
                    close=close,
                    atr=atr,
                    vwap=vwap.value,
                    deviation=vwap.deviation,
                    z_prev=(previous - vwap.value) / vwap.deviation,
                    z_now=(close - vwap.value) / vwap.deviation,
                )
            )
    return points


def arm_verdict(point: BarPoint, *, band_mult: float) -> tuple[Direction | None, str]:
    """KOLUN kararı (`signal.py::_evaluate`): yön ve — yoksa — eleme sebebi."""
    if -band_mult < point.z_prev < band_mult:
        return None, INSIDE_BAND
    if point.z_prev <= -band_mult and point.z_prev < point.z_now < 0.0:
        return "long", PASSED
    if point.z_prev >= band_mult and 0.0 < point.z_now < point.z_prev:
        return "short", PASSED
    crossed = point.z_now >= 0.0 if point.z_prev < 0.0 else point.z_now <= 0.0
    return None, CROSSED if crossed else STILL_EXTENDING


def candidates_of(points: Sequence[BarPoint], *, band_mult: float) -> list[Candidate]:
    """Bant + dönüş şartını geçen kurulumlar. `atr_multiple`dan BAĞIMSIZDIR."""
    found: list[Candidate] = []
    for point in points:
        direction, _ = arm_verdict(point, band_mult=band_mult)
        if direction is not None:
            found.append(Candidate(point=point, direction=direction))
    return found


def geometry_of(
    candidate: Candidate,
    *,
    atr_multiple: float,
    target_reward_risk: float,
    min_stop_pct: float,
    min_reward_risk: float,
) -> Geometry:
    """MODELİN kapıları (`vwap_managed.py::_passes_gates`), aynı sırayla.

    Sıra önemlidir: stop tabanı önce bakılır, R kapısı sonra. Tersine çevirmek "hangi kapı
    eledi" sorusunun cevabını değiştirirdi ve modelin log'larıyla ayrışırdı.
    """
    point = candidate.point
    sign = 1.0 if candidate.direction == "long" else -1.0
    stop = point.close - sign * point.atr * atr_multiple
    distance = abs(point.close - stop)
    stop_pct = distance / point.close

    projected = point.close + sign * distance * target_reward_risk
    ahead = (point.vwap - point.close) * sign > 0.0
    if ahead:
        target = min(projected, point.vwap) if candidate.direction == "long" else max(
            projected, point.vwap
        )
    else:
        target = projected
    reward_risk = abs(target - point.close) / distance

    if stop_pct < min_stop_pct:
        verdict = STOP_FLOOR
    elif reward_risk < min_reward_risk:
        verdict = RR_GATE
    else:
        verdict = PASSED
    return Geometry(stop_pct=stop_pct, reward_risk=reward_risk, verdict=verdict)


def played_signals(
    candidates: Sequence[Candidate],
    *,
    atr_multiple: float,
    target_reward_risk: float,
    min_stop_pct: float,
    min_reward_risk: float,
) -> list[tuple[Candidate, Geometry]]:
    """MODEL düzeyi: barda TEK sinyal — kapılardan geçen EN GÜÇLÜ aday oynanır.

    `vwap_managed.generate_signals` adayları güce göre sıralı gezer ve kapıları geçen İLKİNİ
    döndürür; defterle kıyaslanacak örneklem budur, "geçen tüm adaylar" değil.
    """
    by_bar: dict[pd.Timestamp, list[Candidate]] = {}
    for candidate in candidates:
        by_bar.setdefault(candidate.point.as_of, []).append(candidate)

    played: list[tuple[Candidate, Geometry]] = []
    for as_of in sorted(by_bar):
        bucket = sorted(by_bar[as_of], key=lambda c: (-c.extension, c.point.symbol))
        for candidate in bucket:
            geometry = geometry_of(
                candidate,
                atr_multiple=atr_multiple,
                target_reward_risk=target_reward_risk,
                min_stop_pct=min_stop_pct,
                min_reward_risk=min_reward_risk,
            )
            if geometry.verdict == PASSED:
                played.append((candidate, geometry))
                break
    return played


def _percentile(values: Sequence[float], fraction: float) -> float:
    return float(np.percentile(values, fraction * 100.0)) if values else float("nan")


def _fmt(value: float, width: int, digits: int = 2) -> str:
    return f"{'—':>{width}}" if np.isnan(value) else f"{value:>{width}.{digits}f}"


def funnel_table(
    points: Sequence[BarPoint],
    candidates: Sequence[Candidate],
    scales: Sequence[float],
    *,
    band_mult: float,
    target_reward_risk: float,
    min_stop_pct: float,
    min_reward_risk: float,
    weeks: float,
    symbols: int,
) -> str:
    """TABLO 1 — huni: aday -> kapılar -> geçen -> kadans."""
    lines = [
        f"TABLO 1 — ELEME HUNİSİ  (band_mult={band_mult:g} SABİT, çıta={min_reward_risk:g}R, "
        f"taban=%{min_stop_pct * 100:g})",
        f"{'atr_x':>6} {'aday':>7} {'stop_tab':>9} {'rr_kapisi':>10} {'GEÇEN':>7} "
        f"{'oynanan':>8} {'sinyal/hf':>10} {'/sem/hf':>9} {'30→hafta':>9}",
    ]
    for scale in scales:
        counts = {STOP_FLOOR: 0, RR_GATE: 0, PASSED: 0}
        for candidate in candidates:
            geometry = geometry_of(
                candidate,
                atr_multiple=scale,
                target_reward_risk=target_reward_risk,
                min_stop_pct=min_stop_pct,
                min_reward_risk=min_reward_risk,
            )
            counts[geometry.verdict] += 1
        played = played_signals(
            candidates,
            atr_multiple=scale,
            target_reward_risk=target_reward_risk,
            min_stop_pct=min_stop_pct,
            min_reward_risk=min_reward_risk,
        )
        weekly = len(played) / weeks if weeks else float("nan")
        per_symbol = counts[PASSED] / weeks / symbols if weeks and symbols else float("nan")
        to_thirty = 30.0 / weekly if weekly > 0 else float("inf")
        lines.append(
            f"{scale:>6.1f} {len(candidates):>7} {counts[STOP_FLOOR]:>9} {counts[RR_GATE]:>10} "
            f"{counts[PASSED]:>7} {len(played):>8} {_fmt(weekly, 10)} {_fmt(per_symbol, 9)} "
            + (f"{to_thirty:>9.1f}" if np.isfinite(to_thirty) else f"{'∞':>9}")
        )
    return "\n".join(lines)


def geometry_table(
    candidates: Sequence[Candidate],
    scales: Sequence[float],
    *,
    target_reward_risk: float,
    min_stop_pct: float,
    min_reward_risk: float,
) -> str:
    """TABLO 2 — OYNANAN sinyallerin geometrisi: stop ölçeği (kural 14) ve R:R dağılımı."""
    lines = [
        "TABLO 2 — OYNANAN sinyallerin geometrisi (deftere düşecek örneklem)",
        f"{'atr_x':>6} {'n':>5} {'stop% ort':>10} {'stop% med':>10} {'stop% p10':>10} "
        f"{'stop% p90':>10} {'R:R p50':>9} {'R:R p90':>9}",
    ]
    for scale in scales:
        played = played_signals(
            candidates,
            atr_multiple=scale,
            target_reward_risk=target_reward_risk,
            min_stop_pct=min_stop_pct,
            min_reward_risk=min_reward_risk,
        )
        stops = [g.stop_pct * 100.0 for _, g in played]
        ratios = [g.reward_risk for _, g in played]
        mean = float(np.mean(stops)) if stops else float("nan")
        lines.append(
            f"{scale:>6.1f} {len(played):>5} {_fmt(mean, 10)} "
            f"{_fmt(_percentile(stops, 0.50), 10)} {_fmt(_percentile(stops, 0.10), 10)} "
            f"{_fmt(_percentile(stops, 0.90), 10)} {_fmt(_percentile(ratios, 0.50), 9)} "
            f"{_fmt(_percentile(ratios, 0.90), 9)}"
        )
    return "\n".join(lines)


def scale_table(points: Sequence[BarPoint], *, min_reward_risk: float) -> str:
    """TABLO 3 — σ/ATR ölçeği: 1.5R kapısının her stop ölçeğinde gerektirdiği |z_now|.

    Kapı `|z_now| × σ >= min_reward_risk × atr_multiple × ATR` demektir; σ/ATR bilinirse
    gereken uzaklık doğrudan okunur ve tablonun neden öyle çıktığı görünür olur.
    """
    ratios = [p.sigma_over_atr for p in points]
    if not ratios:
        return "TABLO 3 — ölçülecek nokta yok"
    lines = [
        "TABLO 3 — σ/ATR ölçeği ve 1.5R kapısının gerektirdiği |z_now| (stop ölçeğine göre)",
        f"{'yüzdelik':>10} {'σ/ATR':>8} {'atr_x=1.5':>10} {'2.0':>7} {'2.5':>7} "
        f"{'3.0':>7} {'4.0':>7} {'5.0':>7}",
    ]
    for fraction, label in ((0.10, "p10"), (0.50, "medyan"), (0.90, "p90")):
        ratio = _percentile(ratios, fraction)
        needed = [min_reward_risk * scale / ratio for scale in (1.5, 2.0, 2.5, 3.0, 4.0, 5.0)]
        lines.append(
            f"{label:>10} {ratio:>8.2f} {needed[0]:>10.2f} " + " ".join(
                f"{value:>7.2f}" for value in needed[1:]
            )
        )
    return "\n".join(lines)


def band_table(
    points: Sequence[BarPoint],
    bands: Sequence[float],
    *,
    atr_multiple: float,
    target_reward_risk: float,
    min_stop_pct: float,
    min_reward_risk: float,
    weeks: float,
) -> str:
    """EK TABLO — eski eksen (`band_mult`), stop ölçeği sabitken. `--bands` ile istenir."""
    lines = [
        f"EK TABLO — band_mult süpürmesi (atr_multiple={atr_multiple:g} SABİT)",
        f"{'band':>6} {'aday':>7} {'stop_tab':>9} {'rr_kapisi':>10} {'GEÇEN':>7} {'sinyal/hf':>10}",
    ]
    for band in bands:
        candidates = candidates_of(points, band_mult=band)
        counts = {STOP_FLOOR: 0, RR_GATE: 0, PASSED: 0}
        for candidate in candidates:
            counts[
                geometry_of(
                    candidate,
                    atr_multiple=atr_multiple,
                    target_reward_risk=target_reward_risk,
                    min_stop_pct=min_stop_pct,
                    min_reward_risk=min_reward_risk,
                ).verdict
            ] += 1
        played = played_signals(
            candidates,
            atr_multiple=atr_multiple,
            target_reward_risk=target_reward_risk,
            min_stop_pct=min_stop_pct,
            min_reward_risk=min_reward_risk,
        )
        weekly = len(played) / weeks if weeks else float("nan")
        lines.append(
            f"{band:>6.1f} {len(candidates):>7} {counts[STOP_FLOOR]:>9} {counts[RR_GATE]:>10} "
            f"{counts[PASSED]:>7} {_fmt(weekly, 10)}"
        )
    return "\n".join(lines)


def verify(
    frames: dict[str, pd.DataFrame],
    points: Sequence[BarPoint],
    *,
    atr_period: int,
    min_vwap_bars: int,
    band_mult: float,
    sample: int,
    seed: int,
) -> None:
    """Hızlı yolun `vwap_signal.scan()` ile AYNI adayları verdiğini KANITLAR.

    Kanıtlanan şey aday kümesidir: kapılar `scan`de yoktur, onlar modelin tarafındadır ve
    `geometry_of` zaten `_passes_gates`in aynısıdır (aynı sıra, aynı eşikler).
    """
    stamps = sorted({p.as_of for p in points})
    if not stamps:
        raise SystemExit("doğrulanacak bar yok")
    chosen = random.Random(seed).sample(stamps, min(sample, len(stamps)))
    btc = frames.get("BTC-USDT-SWAP", next(iter(frames.values())))
    fast: dict[pd.Timestamp, set[str]] = {}
    for candidate in candidates_of(points, band_mult=band_mult):
        fast.setdefault(candidate.point.as_of, set()).add(candidate.point.symbol)

    for as_of in chosen:
        market = MarketData(
            ohlcv={symbol: bars_until(frame, as_of) for symbol, frame in frames.items()},
            btc=bars_until(btc, as_of),
            funding={},
            as_of=as_of,
        )
        found, _ = vwap_signal.scan(
            market, atr_period=atr_period, band_mult=band_mult, min_vwap_bars=min_vwap_bars
        )
        expected = {item.symbol for item in found}
        actual = fast.get(as_of, set())
        if expected != actual:
            raise SystemExit(
                f"DOĞRULAMA BAŞARISIZ {as_of}: scan={sorted(expected)} ölçüm={sorted(actual)}"
            )
    logger.info("doğrulama geçti: %d barda scan() ile birebir aynı adaylar", len(chosen))


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
    parser.add_argument("--layer", default="scalp")
    parser.add_argument(
        "--stop-scales", default=DEFAULT_STOP_SCALES,
        help="süpürülecek vwap.managed.atr_multiple değerleri (virgülle)",
    )
    parser.add_argument("--bands", default="", help="ek olarak band_mult süpürmesi (virgülle)")
    parser.add_argument("--verify", type=int, default=0, help="kaç barda scan() ile kıyaslansın")
    parser.add_argument("--csv", type=Path, default=None, help="ham z noktalarını buraya yaz")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper()),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    layer = resolve_layer(load_config(), args.layer)
    config = dict(layer.config)
    bars_per_day = int(pd.Timedelta("1D") / pd.Timedelta(config["timeframe"]))
    # Isınma: ATR(14) ve gün çapası için iki tam gün fazladan bar çekilir.
    config["data"] = dict(config["data"])
    config["data"]["history_bars"] = args.days * bars_per_day + bars_per_day * 2

    symbols = list(layer.symbols or [])
    if not symbols:
        raise SystemExit(f"{args.layer}: sabit evren yok, ölçüm tanımsız")

    band_mult = float(get_setting(config, "vwap.band_mult"))
    target_reward_risk = float(get_setting(config, "vwap.managed.target_reward_risk"))
    min_stop_pct = float(get_setting(config, "scalp.min_stop_pct"))
    min_reward_risk = float(get_setting(config, "scalp.min_reward_risk"))
    atr_period = int(get_setting(config, "trailing.atr_period"))
    min_vwap_bars = int(get_setting(config, "vwap.min_vwap_bars"))
    stop_cap = float(get_setting(config, "max_stop_atr_multiple"))
    scales = [float(value) for value in args.stop_scales.split(",")]

    frames = load_frames(config, symbols)
    if not frames:
        raise SystemExit("hiç sembol çekilemedi")

    newest = max(frame.index[-1] for frame in frames.values())
    start = newest - pd.Timedelta(days=args.days)
    points = measure(frames, atr_period=atr_period, min_vwap_bars=min_vwap_bars, start=start)
    if not points:
        raise SystemExit("ölçülecek nokta yok")
    candidates = candidates_of(points, band_mult=band_mult)

    stamps = {point.as_of for point in points}
    weeks = (max(stamps) - min(stamps)) / pd.Timedelta(days=7)

    print()
    print("=" * 100)
    print(f"MODEL 14 SİNYAL FREKANSI — katman={args.layer} bar={config['timeframe']}")
    print("=" * 100)
    print(f"pencere      : {min(stamps)}  ..  {max(stamps)}")
    print(f"              {weeks:.2f} hafta, {len(stamps)} ayrı bar, {len(frames)} sembol")
    print(f"semboller    : {', '.join(sorted(frames))}")
    print(f"ölçülen nokta: {len(points)} (sembol,bar); bant+dönüş şartını geçen aday: {len(candidates)}")
    print(
        f"SABİTLER     : band_mult={band_mult:g}  çıta={min_reward_risk:g}R  "
        f"taban=%{min_stop_pct * 100:g}  hedef={target_reward_risk:g}R projeksiyon ∧ VWAP  "
        f"ATR periyodu={atr_period}"
    )
    print(f"stop tavanı  : {stop_cap:g}×ATR (kural 14) — süpürülen değerlerin hepsi altında")
    print()
    print(
        funnel_table(
            points, candidates, scales,
            band_mult=band_mult, target_reward_risk=target_reward_risk,
            min_stop_pct=min_stop_pct, min_reward_risk=min_reward_risk,
            weeks=weeks, symbols=len(frames),
        )
    )
    print()
    print(
        geometry_table(
            candidates, scales,
            target_reward_risk=target_reward_risk,
            min_stop_pct=min_stop_pct, min_reward_risk=min_reward_risk,
        )
    )
    print()
    print(scale_table(points, min_reward_risk=min_reward_risk))
    print()
    if args.bands:
        print(
            band_table(
                points, [float(value) for value in args.bands.split(",")],
                atr_multiple=float(get_setting(config, "vwap.managed.atr_multiple")),
                target_reward_risk=target_reward_risk,
                min_stop_pct=min_stop_pct, min_reward_risk=min_reward_risk,
                weeks=weeks,
            )
        )
        print()

    if args.verify:
        verify(
            frames, points,
            atr_period=atr_period, min_vwap_bars=min_vwap_bars, band_mult=band_mult,
            sample=args.verify, seed=int(get_setting(config, "random_seed")),
        )
    if args.csv:
        args.csv.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame([vars(point) for point in points]).to_csv(args.csv, index=False)
        logger.info("ham noktalar yazıldı: %s", args.csv)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
