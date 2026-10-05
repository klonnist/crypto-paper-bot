"""scripts/paired_axis.py: eşleştirilmiş ΔR ekseni — salt okunur, YENİ bir R tanımı YOK.

Ölçülen şey iki yönlüdür: (a) eşleşme kimliği ve kesişimin DIŞINDA kalanların sınıflandırması
(yalnız/bekleyen/ölçülemeyen) ve (b) sayıların `core/metrics.py` ile AYNI tanımdan gelmesi —
araç kendi R'sini ya da bootstrap'ını yazsaydı aynı defter iki cevap verirdi (kural 7).
"""

from __future__ import annotations

import importlib.util
import json
import math
import sys
from pathlib import Path
from typing import Any

import pytest

from core.ledger import TRADE_COLUMNS, Ledger, new_state
from core.metrics import bootstrap_mean_ci, hash_name, merge_fills, r_multiple

_SPEC = importlib.util.spec_from_file_location(
    "paired_axis", Path(__file__).resolve().parent.parent / "scripts" / "paired_axis.py"
)
assert _SPEC and _SPEC.loader
paired_axis = importlib.util.module_from_spec(_SPEC)
sys.modules["paired_axis"] = paired_axis
_SPEC.loader.exec_module(paired_axis)

A, B = "model_a", "model_b"
BTC, ETH, SOL = "BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP"


def _fill(
    model: str,
    symbol: str,
    opened: str,
    *,
    pnl: float,
    risk: float = 100.0,
    direction: str = "long",
    closed: str = "2026-10-01T12:00:00+00:00",
) -> dict[str, Any]:
    row: dict[str, Any] = {column: "" for column in TRADE_COLUMNS}
    row.update(
        strategy=model,
        symbol=symbol,
        direction=direction,
        opened_at=opened,
        closed_at=closed,
        entry_price=100.0,
        exit_price=101.0,
        qty=1.0,
        notional=100.0,
        stop_price=97.0,
        risk_amount=risk,
        pnl=pnl,
        fee=0.0,
        slippage_cost=0.0,
        funding=0.0,
        exit_reason="tp",
    )
    return row


def _t(hour: int) -> str:
    return f"2026-10-01T{hour:02d}:00:00+00:00"


def _run(
    trades_a: list[dict[str, Any]],
    trades_b: list[dict[str, Any]],
    *,
    open_a: list[tuple[str, str]] = (),  # type: ignore[assignment]
    open_b: list[tuple[str, str]] = (),  # type: ignore[assignment]
    iterations: int = 500,
) -> Any:
    def state(model: str, opened: list[tuple[str, str]]) -> dict[str, Any]:
        payload = new_state(model, initial_capital=10_000.0)
        payload["positions"] = [
            {"symbol": symbol, "direction": "long", "opened_at": opened_at}
            for symbol, opened_at in opened
        ]
        return payload

    return paired_axis.paired_axis(
        model_a=A, trades_a=trades_a, state_a=state(A, list(open_a)),
        model_b=B, trades_b=trades_b, state_b=state(B, list(open_b)),
        alpha=0.05, iterations=iterations, seed=7,
    )


def test_matching_uses_symbol_direction_and_opened_at_and_pairs_the_deltas() -> None:
    trades_a = [_fill(A, BTC, _t(1), pnl=+50.0), _fill(A, ETH, _t(2), pnl=-100.0)]
    trades_b = [_fill(B, BTC, _t(1), pnl=-100.0), _fill(B, ETH, _t(2), pnl=-100.0)]

    result = _run(trades_a, trades_b)

    assert result.matched == 2
    assert result.mean_a == pytest.approx(-0.25)          # (+0.5 −1.0) / 2
    assert result.mean_b == pytest.approx(-1.0)
    assert result.mean_delta == pytest.approx(+0.75)      # ((+0.5+1.0) + 0) / 2


def test_a_different_opened_at_or_direction_is_not_the_same_position() -> None:
    trades_a = [_fill(A, BTC, _t(1), pnl=10.0), _fill(A, ETH, _t(2), pnl=10.0, direction="short")]
    trades_b = [_fill(B, BTC, _t(3), pnl=10.0), _fill(B, ETH, _t(2), pnl=10.0, direction="long")]

    result = _run(trades_a, trades_b)

    assert result.matched == 0
    assert (result.only_a, result.only_b) == (2, 2)
    assert math.isnan(result.mean_delta)        # 0.0 değil: ölçülmedi


def test_unmatched_positions_are_split_into_only_pending_and_open_both() -> None:
    trades_a = [
        _fill(A, BTC, _t(1), pnl=1.0),      # B'de hiç yok          -> yalnız A
        _fill(A, ETH, _t(2), pnl=1.0),      # B'de hâlâ AÇIK         -> bekleyen (A kapalı, B açık)
        _fill(A, SOL, _t(3), pnl=1.0),      # B'de kapalı           -> eşleşir
    ]
    trades_b = [
        _fill(B, SOL, _t(3), pnl=2.0),
        _fill(B, BTC, _t(4), pnl=1.0),      # A'da yok              -> yalnız B
        _fill(B, ETH, _t(5), pnl=1.0),      # A'da hâlâ açık        -> bekleyen (B kapalı, A açık)
    ]

    result = _run(trades_a, trades_b, open_a=[(ETH, _t(5))], open_b=[(ETH, _t(2))])

    assert result.matched == 1
    assert (result.only_a, result.only_b) == (1, 1)
    assert result.pending_a_closed_b_open == 1
    assert result.pending_b_closed_a_open == 1


def test_a_pending_position_never_counts_as_only() -> None:
    """Bekleyeni "yalnız"a katmak ΔR ekseninin örnek seçimini sessizce kaydırırdı."""
    result = _run([_fill(A, BTC, _t(1), pnl=1.0)], [], open_b=[(BTC, _t(1))])

    assert (result.only_a, result.pending_a_closed_b_open) == (0, 1)


def test_partial_fills_are_merged_into_one_position_per_side() -> None:
    """Birim POZİSYONDUR (kural 21): kısmi çıkış satırı ΔR'yi iki kez saymaz."""
    trades_a = [
        _fill(A, BTC, _t(1), pnl=30.0, risk=50.0, closed=_t(5)),
        _fill(A, BTC, _t(1), pnl=20.0, risk=50.0, closed=_t(6)),   # aynı pozisyonun 2. dilimi
    ]
    trades_b = [_fill(B, BTC, _t(1), pnl=40.0)]

    result = _run(trades_a, trades_b)

    assert result.matched == 1
    assert result.mean_a == pytest.approx(0.5)    # (30+20) / (50+50)
    assert result.mean_delta == pytest.approx(0.5 - 0.4)


def test_an_undefined_r_is_counted_as_unmeasured_not_zero() -> None:
    trades_a = [_fill(A, BTC, _t(1), pnl=5.0, risk=0.0)]      # risk bilinmiyor -> R yok
    trades_b = [_fill(B, BTC, _t(1), pnl=5.0)]

    result = _run(trades_a, trades_b)

    assert (result.matched, result.unmeasured) == (0, 1)


def test_the_numbers_come_from_the_metrics_module_definitions() -> None:
    """Kural 7: R `merge_fills` + `r_multiple`ten, aralık `bootstrap_mean_ci`dan — ikinci tanım yok."""
    trades_a = [_fill(A, BTC, _t(h), pnl=float(10 * h - 30)) for h in range(1, 9)]
    trades_b = [_fill(B, BTC, _t(h), pnl=float(5 * h - 10)) for h in range(1, 9)]

    result = _run(trades_a, trades_b, iterations=300)

    r_a = [r_multiple(row) for row in merge_fills(trades_a)]
    r_b = [r_multiple(row) for row in merge_fills(trades_b)]
    deltas = [x - y for x, y in zip(r_a, r_b)]  # type: ignore[operator]
    low, high = bootstrap_mean_ci(
        deltas, alpha=0.05, iterations=300, seed=7 ^ hash_name(f"paired_axis:{A}:{B}")
    )
    assert result.mean_delta == pytest.approx(sum(deltas) / len(deltas))
    assert (result.ci_low, result.ci_high) == (low, high)


def test_the_interval_is_deterministic() -> None:
    trades_a = [_fill(A, BTC, _t(h), pnl=float(h * 7 % 11) - 5) for h in range(1, 9)]
    trades_b = [_fill(B, BTC, _t(h), pnl=float(h * 5 % 13) - 6) for h in range(1, 9)]

    first = _run(trades_a, trades_b)
    second = _run(trades_a, trades_b)

    assert (first.ci_low, first.ci_high) == (second.ci_low, second.ci_high)


def test_rho_and_sd_are_reported_and_mde_follows_the_realised_sd() -> None:
    """§6e: ρ ve gerçekleşen sd(ΔR) sonucun yanına yazılır; MDE beklentiden değil sd'den gelir."""
    trades_a = [_fill(A, BTC, _t(h), pnl=float(p)) for h, p in zip(range(1, 7), (10, -40, 30, -80, 60, 5))]
    trades_b = [_fill(B, BTC, _t(h), pnl=float(p)) for h, p in zip(range(1, 7), (5, -30, 20, -50, 10, 15))]

    result = _run(trades_a, trades_b)

    assert -1.0 <= result.rho <= 1.0
    assert result.sd_delta > 0.0
    assert result.mde == pytest.approx(2.80158 * result.sd_delta / math.sqrt(result.matched), rel=1e-4)


def test_too_few_pairs_give_nan_not_zero() -> None:
    one = _run([_fill(A, BTC, _t(1), pnl=1.0)], [_fill(B, BTC, _t(1), pnl=2.0)])

    assert one.matched == 1
    assert math.isnan(one.sd_delta) and math.isnan(one.rho) and math.isnan(one.ci_low)
    assert one.mean_delta == pytest.approx(-0.01)


def test_the_script_never_writes_to_the_ledger(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ledger = Ledger(tmp_path)
    for model in (A, B):
        ledger.initialize_model(model, initial_capital=10_000.0)
    ledger.append_trades(A, [_fill(A, BTC, _t(1), pnl=10.0)])
    ledger.append_trades(B, [_fill(B, BTC, _t(1), pnl=20.0)])
    before = {path: path.read_bytes() for path in sorted(tmp_path.rglob("*")) if path.is_file()}

    code = paired_axis.main(["--a", A, "--b", B, "--ledger-root", str(tmp_path), "--json"])

    after = {path: path.read_bytes() for path in sorted(tmp_path.rglob("*")) if path.is_file()}
    payload = json.loads(capsys.readouterr().out)
    assert code == 0 and before == after
    assert payload["matched"] == 1
    assert payload["sd_delta"] is None          # nan JSON'da null: 0.0 DEĞİL


def test_a_missing_ledger_is_an_error_not_an_empty_result(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    Ledger(tmp_path).initialize_model(A, initial_capital=10_000.0)

    code = paired_axis.main(["--a", A, "--b", "yok", "--ledger-root", str(tmp_path)])

    assert code == 2
    assert "defter yok" in capsys.readouterr().err
