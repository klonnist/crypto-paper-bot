#!/usr/bin/env python3
"""İki modelin defterindeki ORTAK pozisyonlar üzerinden eşleştirilmiş eksen ölçümü. SALT OKUNUR.

Cevapladığı soru tek: *iki ikiz model aynı kurulumu aldığında, ayrışan tek değişken
pozisyon başına kaç R fark yaratıyor?* (`ΔR = R_A − R_B`, docs/backtest.md > 6e: eksen
istatistiği marjinal ortalama R değil, eşleşen pozisyonlarda ΔR'nin ortalamasıdır.)

**Bu araç ölçümün parçası DEĞİLDİR** (`scripts/measure_vwap_signal.py` ile aynı statü):
deftere yazmaz, `config.yaml`a dokunmaz, hiçbir modelin davranışını değiştirmez ve hiçbir
kabul kapısı buradan okunmaz. Defterleri yalnızca OKUR.

**Yeni bir R tanımı YOKTUR** (kural 7). R, `core/metrics.py::merge_fills` + `r_multiple`ten,
bootstrap aralığı `core/metrics.py::bootstrap_mean_ci`den gelir. Eşleştirilmiş bootstrap
ayrı bir yordam değildir: pozisyon çiftleri BİRLİKTE yeniden örneklenir, bu da çiftlerin
FARKLARININ ortalamasını yeniden örneklemekle özdeştir (bağımsız yeniden örnekleme
eşleştirmenin kazancını geri verirdi). Alfa, yeniden örnekleme sayısı ve tohum
`config.yaml`dan gelir (`acceptance.edge_ci_alpha`, `acceptance.bootstrap_samples`,
`random_seed`): ikinci bir alfa anahtarı açılmaz.

**Eşleşme kimliği `(symbol, direction, opened_at)`tir** — `merge_fills`in pozisyon kimliğinin
`strategy` bileşeni olmadan hâli (iki model karşılaştırılıyor, ikisi de aynı sembolde aynı
barda açabilir). `n` EŞLEŞEN pozisyonların sayısıdır, iki defterin toplamı değil; dolumlar
ayrışır (çıkış kuralı kotayı ve nakdi farklı zamanlarda serbest bırakır) ve eşleşme
kesişimdir. Kesişimin DIŞINDA kalanlar ayrıca sayılır ve üç sınıfa ayrılır:

- `yalnız A` / `yalnız B`: öteki modelin defterinde (kapalı ya da açık) hiç karşılığı yok;
- `bekleyen`: bir tarafta KAPANMIŞ, öteki tarafta hâlâ AÇIK — henüz karşılaştırılamaz,
  zamanla eşleşmeye döner (kısa süreli modelin kapattığı pozisyonu uzun süreli model hâlâ
  taşıyor olabilir). Bunu "yalnız"a katmak ΔR ekseninin örnek seçimini sessizce kaydırırdı;
- `ölçülemeyen`: eşleşti ama taraflardan birinin R'si tanımsız (`risk_amount` yok).

**`ρ` ve gerçekleşen `sd(ΔR)` sonucun YANINA yazılır** (§6e, "Üç şart" 1): MDE beklenen bir
`ρ`den değil gerçekleşen `sd`den yeniden hesaplanır (`(z₁₋α/₂ + z_güç) · sd / √n`, α=0.05
iki yanlı, güç 0.80 — §6e'nin tablosundakiyle aynı formül). Beklentiyi tutmuş gibi
raporlamak gücü olduğundan iyi göstermek olurdu.

**C-1 yerine geçmez.** Bu araç "çıkış/giriş kuralı bir fark yaratıyor mu" sorusunu cevaplar;
"bu model para kazanıyor mu" ayrı bir sorudur ve kabul çıtasındadır (§6e, "Üç şart" 3).

Kullanım (depo kökünden):
    python scripts/paired_axis.py --a scalp_patient --b scalp_fixed
    python scripts/paired_axis.py --a scalp_patient --b scalp_fixed --json
    python scripts/paired_axis.py --a x --b y --ledger-root /yol/ledgers_scalp
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import statistics
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config import get_setting, load_config  # noqa: E402
from core.layers import DEFAULT_LAYER, resolve_layer  # noqa: E402
from core.ledger import Ledger, LedgerError  # noqa: E402
from core.metrics import (  # noqa: E402
    bootstrap_mean_ci,
    hash_name,
    merge_fills,
    r_multiple,
)

logger = logging.getLogger("paired_axis")

_NAN = float("nan")
# §6e'nin MDE tablosundaki çözünürlük: iki yanlı α=0.05, güç 0.80.
_POWER = 0.80
_ALPHA_FOR_MDE = 0.05

PositionId = tuple[str, str, str]


@dataclass(frozen=True, kw_only=True)
class PairedResult:
    """Eşleştirilmiş eksenin sonucu. Tanımsız sayı `nan`dır, 0.0 DEĞİL."""

    model_a: str
    model_b: str
    matched: int
    mean_a: float
    mean_b: float
    mean_delta: float
    ci_low: float
    ci_high: float
    sd_delta: float
    rho: float
    mde: float
    only_a: int
    only_b: int
    pending_a_closed_b_open: int
    pending_b_closed_a_open: int
    unmeasured: int
    alpha: float
    iterations: int


def position_id(row: Mapping[str, Any]) -> PositionId:
    """`merge_fills`in kimliği, `strategy` bileşeni olmadan: iki modelin satırı karşılaştırılır."""
    return (
        str(row.get("symbol", "")),
        str(row.get("direction", "")),
        str(row.get("opened_at", "")),
    )


def closed_by_id(trades: Sequence[Mapping[str, Any]]) -> dict[PositionId, Mapping[str, Any]]:
    """Kapanmış POZİSYONLAR (dolumlar `merge_fills` ile birleşmiş), kimliğe göre.

    `opened_at` taşımayan satır kimliksizdir ve eşleştirilemez: onu ortak bir kimliğe
    toplamak sessiz bir veri kaybı olurdu (`merge_fills`in aynı gerekçesi) — atlanır.
    """
    result: dict[PositionId, Mapping[str, Any]] = {}
    for row in merge_fills(trades):
        identity = position_id(row)
        if identity[2]:
            result[identity] = row
    return result


def open_ids(state: Mapping[str, Any] | None) -> set[PositionId]:
    """Hâlâ AÇIK pozisyonların kimlikleri (`positions.json > positions`)."""
    if not state:
        return set()
    return {
        position_id(position)
        for position in state.get("positions") or ()
        if position.get("opened_at")
    }


def paired_axis(
    *,
    model_a: str,
    trades_a: Sequence[Mapping[str, Any]],
    state_a: Mapping[str, Any] | None,
    model_b: str,
    trades_b: Sequence[Mapping[str, Any]],
    state_b: Mapping[str, Any] | None,
    alpha: float,
    iterations: int,
    seed: int,
) -> PairedResult:
    closed_a = closed_by_id(trades_a)
    closed_b = closed_by_id(trades_b)
    open_a, open_b = open_ids(state_a), open_ids(state_b)

    deltas: list[float] = []
    r_a: list[float] = []
    r_b: list[float] = []
    unmeasured = 0
    for identity in sorted(closed_a.keys() & closed_b.keys()):
        left, right = r_multiple(closed_a[identity]), r_multiple(closed_b[identity])
        if left is None or right is None:
            unmeasured += 1
            continue
        r_a.append(left)
        r_b.append(right)
        deltas.append(left - right)

    only_a = [i for i in closed_a if i not in closed_b and i not in open_b]
    only_b = [i for i in closed_b if i not in closed_a and i not in open_a]
    pending_a = [i for i in closed_a if i in open_b and i not in closed_b]
    pending_b = [i for i in closed_b if i in open_a and i not in closed_a]

    ci_low, ci_high = bootstrap_mean_ci(
        deltas,
        alpha=alpha,
        iterations=iterations,
        seed=int(seed) ^ hash_name(f"paired_axis:{model_a}:{model_b}"),
    )
    sd_delta = statistics.stdev(deltas) if len(deltas) >= 2 else _NAN
    return PairedResult(
        model_a=model_a,
        model_b=model_b,
        matched=len(deltas),
        mean_a=_mean(r_a),
        mean_b=_mean(r_b),
        mean_delta=_mean(deltas),
        ci_low=ci_low,
        ci_high=ci_high,
        sd_delta=sd_delta,
        rho=_correlation(r_a, r_b),
        mde=_mde(sd_delta, len(deltas)),
        only_a=len(only_a),
        only_b=len(only_b),
        pending_a_closed_b_open=len(pending_a),
        pending_b_closed_a_open=len(pending_b),
        unmeasured=unmeasured,
        alpha=alpha,
        iterations=iterations,
    )


def format_report(result: PairedResult) -> str:
    a, b = result.model_a, result.model_b
    rows = [
        ("eşleşen pozisyon (n)", str(result.matched)),
        (f"ort. R  {a}", _fmt(result.mean_a)),
        (f"ort. R  {b}", _fmt(result.mean_b)),
        (
            "ort. ΔR",
            f"{_fmt(result.mean_delta)}   [%{(1 - result.alpha) * 100:g} eşleştirilmiş bootstrap: "
            f"{_fmt(result.ci_low)} … {_fmt(result.ci_high)}, {result.iterations} örnek]",
        ),
        ("gerçekleşen sd(ΔR)", _fmt(result.sd_delta, signed=False)),
        (f"ρ (R_{a}, R_{b})", _fmt(result.rho)),
        (
            "MDE (gerçekleşen sd'den)",
            f"±{_fmt(result.mde, signed=False)}   (iki yanlı α=0.05, güç 0.80)",
        ),
    ]
    width = max(len(label) for label, _ in rows) + 2
    lines = [
        f"EŞLEŞTİRİLMİŞ EKSEN: {a} ↔ {b}   (ΔR = R_{a} − R_{b}, eşleşen pozisyon başına)",
        "",
        *(f"  {label.ljust(width)}{value}" for label, value in rows),
        "",
        "  eşleşmeyenler (n'ye GİRMEZ):",
        f"    yalnız {a}: {result.only_a}   yalnız {b}: {result.only_b}",
        f"    bekleyen ({a} kapalı, {b} açık): {result.pending_a_closed_b_open}   "
        f"({b} kapalı, {a} açık): {result.pending_b_closed_a_open}",
        f"    ölçülemeyen (R tanımsız): {result.unmeasured}",
        "",
        "  Okuma: aralık 0'ı içeriyorsa fark bu örneklemde AYIRT EDİLEMİYOR; ΔR'nin işareti ile",
        "  C-1 (modelin kendi ort. R'si > 0) ayrı sorulardır ve ikisi birlikte okunur (§6e).",
    ]
    return "\n".join(lines)


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else _NAN


def _correlation(left: Sequence[float], right: Sequence[float]) -> float:
    """Pearson ρ; ikiden az çift ya da sabit bir dizi için `nan` (0.0 "ilişkisiz" demek olurdu)."""
    if len(left) < 2 or len(set(left)) < 2 or len(set(right)) < 2:
        return _NAN
    return statistics.correlation(left, right)


def _mde(sd: float, n: int) -> float:
    if n < 1 or math.isnan(sd):
        return _NAN
    normal = statistics.NormalDist()
    z = normal.inv_cdf(1.0 - _ALPHA_FOR_MDE / 2.0) + normal.inv_cdf(_POWER)
    return z * sd / math.sqrt(n)


def _fmt(value: float, *, signed: bool = True) -> str:
    if math.isnan(value):
        return "—"
    return f"{value:+.3f}" if signed else f"{value:.3f}"


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="İki modelin ortak pozisyonları üzerinden eşleştirilmiş ΔR ekseni (salt okunur)."
    )
    parser.add_argument("--a", required=True, help="birinci model (ΔR = R_a − R_b)")
    parser.add_argument("--b", required=True, help="ikinci model")
    parser.add_argument("--layer", default="scalp", help="defter kökünü belirleyen katman (varsayılan scalp)")
    parser.add_argument("--ledger-root", default=None, help="defter kökünü ez (testler/yedek klon için)")
    parser.add_argument("--json", action="store_true", help="metin yerine JSON yazdır")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    config = load_config()
    layer = resolve_layer(config, args.layer if args.layer else DEFAULT_LAYER)
    ledger = Ledger(args.ledger_root if args.ledger_root else layer.ledger_root)

    for model in (args.a, args.b):
        if not ledger.model_dir(model).is_dir():
            print(f"defter yok: {ledger.model_dir(model)}", file=sys.stderr)
            return 2
    try:
        result = paired_axis(
            model_a=args.a,
            trades_a=ledger.read_trades(args.a),
            state_a=ledger.load_state(args.a),
            model_b=args.b,
            trades_b=ledger.read_trades(args.b),
            state_b=ledger.load_state(args.b),
            alpha=float(get_setting(layer.config, "acceptance.edge_ci_alpha")),
            iterations=int(get_setting(layer.config, "acceptance.bootstrap_samples")),
            seed=int(get_setting(layer.config, "random_seed")),
        )
    except LedgerError as exc:
        print(f"defter okunamadı: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(_json_safe(asdict(result)), indent=2, ensure_ascii=False))
    else:
        print(format_report(result))
    return 0


def _json_safe(payload: Mapping[str, Any]) -> dict[str, Any]:
    """`nan` JSON'da geçerli değildir: null yazılır (0.0 DEĞİL — "ölçülmedi" ≠ "sıfır")."""
    return {
        key: (None if isinstance(value, float) and math.isnan(value) else value)
        for key, value in payload.items()
    }


if __name__ == "__main__":
    sys.exit(main())
