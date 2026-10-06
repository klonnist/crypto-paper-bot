"""scripts/paired_axis.py `--sets`: giriş FİLTRESİ olan ikizin küme farkı (karar 54).

Filtreli bir ikizde eşleşen pozisyonlar aynı sinyalin aynı çıkışla iki kopyasıdır (ΔR ≈ 0);
etki korunan ile elenen KÜMELER arasında yaşar. Burada ölçülen: kümelerin kimlikten doğru
türetilmesi, geçerlilik kapısının (eşleşenlerde ΔR) ayrı raporlanması, açık pozisyonların
sınıflanmaması ve hesabın `core/metrics.py` tanımlarından gelmesi.
"""

from __future__ import annotations

import math
from typing import Any

import pytest

from core.ledger import new_state
from core.metrics import bootstrap_diff_ci, hash_name
from tests.test_paired_axis import BTC, ETH, SOL, _fill, _t, paired_axis

TWIN, BASE = "twin", "base"


def _state(model: str, *opened: tuple[str, str]) -> dict[str, Any]:
    payload = new_state(model, initial_capital=10_000.0)
    payload["positions"] = [
        {"symbol": symbol, "direction": "long", "opened_at": opened_at} for symbol, opened_at in opened
    ]
    return payload


def _split(
    base_trades: list[dict[str, Any]],
    twin_trades: list[dict[str, Any]],
    *,
    base_open: tuple[tuple[str, str], ...] = (),
    twin_open: tuple[tuple[str, str], ...] = (),
) -> Any:
    return paired_axis.set_split(
        twin=TWIN, trades_twin=twin_trades, state_twin=_state(TWIN, *twin_open),
        base=BASE, trades_base=base_trades, state_base=_state(BASE, *base_open),
        alpha=0.05, iterations=300, seed=7,
    )


def test_kept_and_removed_are_the_base_positions_the_twin_did_and_did_not_take() -> None:
    base = [
        _fill(BASE, BTC, _t(1), pnl=+50.0),     # ikiz de aldı  -> korunan
        _fill(BASE, ETH, _t(2), pnl=-100.0),    # ikiz ALMADI   -> elenen
        _fill(BASE, SOL, _t(3), pnl=-100.0),    # ikiz ALMADI   -> elenen
    ]
    twin = [_fill(TWIN, BTC, _t(1), pnl=+50.0)]

    result = _split(base, twin)

    assert (result.kept, result.removed) == (1, 2)
    assert result.mean_kept == pytest.approx(0.5)
    assert result.mean_removed == pytest.approx(-1.0)
    assert result.diff == pytest.approx(1.5)


def test_the_validity_gate_is_the_matched_delta_and_is_reported_separately() -> None:
    """Saf filtre ikizinde eşleşenlerin R'si AYNIDIR: ΔR = 0. Fark varsa ikiz başka bir şeyde ayrışıyor."""
    base = [_fill(BASE, BTC, _t(1), pnl=50.0), _fill(BASE, ETH, _t(2), pnl=-100.0)]
    identical = [_fill(TWIN, BTC, _t(1), pnl=50.0)]
    drifting = [_fill(TWIN, BTC, _t(1), pnl=20.0)]

    assert _split(base, identical).matched_mean_delta == pytest.approx(0.0)
    assert _split(base, drifting).matched_mean_delta == pytest.approx(-0.3)


def test_a_base_position_still_open_in_the_twin_is_in_neither_set() -> None:
    """İkizde hâlâ AÇIK olan baz pozisyonu henüz sınıflanamaz: "elenen" saymak onu haksız ayıklardı."""
    base = [_fill(BASE, BTC, _t(1), pnl=-100.0)]

    result = _split(base, [], twin_open=((BTC, _t(1)),))

    assert (result.kept, result.removed) == (0, 0)


def test_replacements_are_counted_but_never_enter_either_set() -> None:
    base = [_fill(BASE, BTC, _t(1), pnl=10.0)]
    twin = [_fill(TWIN, BTC, _t(1), pnl=10.0), _fill(TWIN, ETH, _t(5), pnl=-100.0)]   # ETH yalnız ikizde

    result = _split(base, twin)

    assert result.replacement == 1 and (result.kept, result.removed) == (1, 0)


def test_the_interval_comes_from_the_metrics_module_definition() -> None:
    base = [_fill(BASE, BTC, _t(h), pnl=float(p)) for h, p in zip(range(1, 9), (50, 30, -100, 40, -100, -100, -100, 20))]
    twin = [_fill(TWIN, BTC, _t(h), pnl=float(p)) for h, p in zip(range(1, 5), (50, 30, -100, 40))]

    result = _split(base, twin)

    kept = [0.5, 0.3, -1.0, 0.4]
    removed = [-1.0, -1.0, -1.0, 0.2]
    low, high = bootstrap_diff_ci(
        kept, removed, alpha=0.05, iterations=300, seed=7 ^ hash_name(f"set_split:{TWIN}:{BASE}")
    )
    assert result.diff == pytest.approx(sum(kept) / 4 - sum(removed) / 4)
    assert (result.ci_low, result.ci_high) == (low, high)


def test_an_empty_set_gives_nan_not_zero() -> None:
    base = [_fill(BASE, BTC, _t(1), pnl=10.0)]

    result = _split(base, [_fill(TWIN, BTC, _t(1), pnl=10.0)])

    assert result.removed == 0
    assert math.isnan(result.diff) and math.isnan(result.mean_removed) and math.isnan(result.mde)


def test_the_report_names_the_gate_before_the_effect() -> None:
    base = [_fill(BASE, BTC, _t(1), pnl=50.0), _fill(BASE, ETH, _t(2), pnl=-100.0),
            _fill(BASE, SOL, _t(3), pnl=-100.0)]
    twin = [_fill(TWIN, BTC, _t(1), pnl=50.0)]

    text = paired_axis.format_split(_split(base, twin))

    assert text.index("geçerlilik") < text.index("fark (korunan − elenen)")
    assert "KÜME FARKI: twin (ikiz) ↔ base (baz)" in text
