"""`core/metrics.py::ExposureStats` ve `ModelReport.rejections_by_open` (karar 55): ÖLÇÜM kolonları.

İki şey ölçülür: (a) sayım tanımları — ters yönlü çakışmanın GİRİŞTE ve ARALIK olarak ayrılması,
açık pozisyonların dâhil edilmesi, `leverage > 1` ve nakit baskısı; (b) bu kolonların bir KURAL
olmadığı: eklenmeleri hiçbir mevcut metriği, sinyali ya da dolumu değiştirmez ve ret sayımı yalnızca
tur raporuna bir döküm ekler.
"""

from __future__ import annotations

import dataclasses
import math
from pathlib import Path
from typing import Any

import pytest

from core import metrics
from core.ledger import TRADE_COLUMNS, Ledger, new_state
from tests.test_engine_per_bar import OTHER, SYMBOL, _config, _EveryBar, _engine, _market

BTC, ETH = "BTC-USDT-SWAP", "ETH-USDT-SWAP"


def _t(hour: int) -> str:
    return f"2026-10-01T{hour:02d}:00:00+00:00"


def _trade(symbol: str, direction: str, opened: str, closed: str, *, leverage: float = 1.0) -> dict[str, Any]:
    row: dict[str, Any] = {column: "" for column in TRADE_COLUMNS}
    row.update(
        strategy="m", symbol=symbol, direction=direction, opened_at=opened, closed_at=closed,
        entry_price=100.0, exit_price=101.0, qty=1.0, notional=100.0, stop_price=97.0,
        risk_amount=100.0, leverage=leverage, margin=100.0, fee=0.0, slippage_cost=0.0,
        funding=0.0, pnl=1.0, exit_reason="tp",
    )
    return row


def _equity(*pairs: tuple[float, float]) -> list[dict[str, Any]]:
    return [{"ts": _t(i), "cash": cash, "equity": equity, "open_positions": 0} for i, (cash, equity) in enumerate(pairs)]


def _metrics(trades: list[dict[str, Any]], equity: list[dict[str, Any]], open_positions: list[dict[str, Any]] = ()) -> Any:  # type: ignore[assignment]
    return metrics.model_metrics(
        "m", trades=trades, equity_rows=equity, initial_capital=10_000.0,
        periods_per_year=365.0 * 96, open_positions=open_positions,
    )


def test_exposure_counts_opposite_overlaps_and_includes_open_positions() -> None:
    trades = [_trade(BTC, "long", _t(1), _t(6), leverage=1.0), _trade(ETH, "short", _t(2), _t(3), leverage=1.4)]
    open_positions = [{"symbol": BTC, "direction": "short", "opened_at": _t(4)}]

    result = _metrics(trades, _equity((9_000.0, 10_000.0)), open_positions).exposure

    assert result.positions == 3 and result.closed_positions == 2
    assert (result.opposite_at_entry, result.opposite_overlap) == (1, 2)    # BTC long ↔ BTC short (açık)
    assert result.leveraged_positions == 1


def test_exposure_cash_columns() -> None:
    equity = _equity((5_000.0, 10_000.0), (100.0, 10_000.0), (-0.8, 10_000.0))

    result = _metrics([], equity).exposure

    assert (result.bars, result.cash_tight_bars, result.cash_negative_bars) == (3, 2, 1)
    assert result.min_cash_ratio == pytest.approx(-0.00008)


def test_exposure_is_defined_for_an_empty_ledger() -> None:
    result = _metrics([], []).exposure

    assert dataclasses.astuple(result) == (0, 0, 0, 0, 0, 0, 0, 0, 0.0)


def test_adding_the_columns_changes_no_other_metric() -> None:
    """Ölçüm kolonu bir kural değildir: açık pozisyon bilgisi yalnızca `exposure`ı etkiler."""
    trades = [_trade(BTC, "long", _t(1), _t(6)), _trade(ETH, "short", _t(2), _t(3))]
    equity = _equity((9_000.0, 10_000.0), (9_500.0, 10_100.0))

    plain = _metrics(trades, equity)
    with_open = _metrics(trades, equity, [{"symbol": BTC, "direction": "short", "opened_at": _t(4)}])

    for field in dataclasses.fields(plain):
        if field.name == "exposure":
            continue
        left, right = getattr(plain, field.name), getattr(with_open, field.name)
        assert repr(left) == repr(right), field.name
    assert plain.exposure != with_open.exposure


def test_the_columns_reach_the_json_payload() -> None:
    payload = dataclasses.asdict(_metrics([_trade(BTC, "long", _t(1), _t(2))], _equity((9_000.0, 10_000.0))))

    assert set(payload["exposure"]) == {
        "positions", "opposite_at_entry", "opposite_overlap", "closed_positions",
        "leveraged_positions", "bars", "cash_tight_bars", "cash_negative_bars", "min_cash_ratio",
    }


def test_compare_reads_open_positions_from_the_ledger_state(tmp_path: Path) -> None:
    from core.config import load_config

    ledger = Ledger(tmp_path)
    ledger.initialize_model("m", initial_capital=10_000.0)
    ledger.append_trades("m", [_trade(BTC, "long", _t(1), _t(6))])
    state = new_state("m", initial_capital=10_000.0)
    state["positions"] = [{"symbol": BTC, "direction": "short", "opened_at": _t(4)}]
    ledger.write_state("m", state)

    (item,) = metrics.compare(["m"], ledger=ledger, config=load_config())

    assert item.exposure is not None
    assert (item.exposure.positions, item.exposure.opposite_at_entry) == (2, 1)


def test_the_script_and_the_metrics_module_share_one_definition() -> None:
    """İkinci bir tanım yok (kural 7): script, `core/metrics.py`nin fonksiyonlarını kullanır."""
    import importlib.util
    import sys

    spec = importlib.util.spec_from_file_location(
        "measure_engine_rules_shared",
        Path(__file__).resolve().parent.parent / "scripts" / "measure_engine_rules.py",
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["measure_engine_rules_shared"] = module
    spec.loader.exec_module(module)

    assert module.opposite_stats is metrics.opposite_stats
    assert module.leverage_stats is metrics.leverage_stats
    assert module.cash_stats is metrics.cash_stats


# --------------------------------------------------------------------------- #
# Tur raporu: `rejections_by_open`
# --------------------------------------------------------------------------- #
def test_rejections_are_broken_down_by_the_open_position_count(tmp_path: Path) -> None:
    """Kota doluyken gelen ret `max_positions`tır ve KAÇ açık pozisyonla geldiği raporda durur."""
    symbols = [SYMBOL, OTHER, "SOL-USDT-SWAP", "XRP-USDT-SWAP", "DOGE-USDT-SWAP", "ADA-USDT-SWAP"]
    ledger = Ledger(tmp_path)
    engine = _engine(_EveryBar(symbols=symbols), ledger, _config(max_positions=2, max_short_positions=1))

    engine.run_round(_market(1, symbols=symbols))
    report = engine.run_round(_market(7, symbols=symbols))

    model = report.by_model("her_bar")
    assert model is not None
    assert model.rejections == {"max_positions": 4}
    assert model.rejections_by_open == {"max_positions": {"2": 4}}


def test_the_breakdown_never_exceeds_the_rejection_counts(tmp_path: Path) -> None:
    symbols = [SYMBOL, OTHER, "SOL-USDT-SWAP", "XRP-USDT-SWAP"]
    ledger = Ledger(tmp_path)
    engine = _engine(_EveryBar(symbols=symbols), ledger, _config(max_positions=3, max_short_positions=1))

    engine.run_round(_market(1, symbols=symbols))
    model = engine.run_round(_market(7, symbols=symbols)).by_model("her_bar")

    assert model is not None
    for code, buckets in model.rejections_by_open.items():
        assert sum(buckets.values()) <= model.rejections[code]
        assert all(key.isdigit() for key in buckets)


def test_the_audit_field_does_not_change_fills_or_the_ledger(tmp_path: Path) -> None:
    """Denetim izi: aynı turun doldurduğu pozisyonlar, ret sayıları ve defter durumu birebir aynı."""
    symbols = [SYMBOL, OTHER, "SOL-USDT-SWAP", "XRP-USDT-SWAP", "DOGE-USDT-SWAP", "ADA-USDT-SWAP"]
    ledger = Ledger(tmp_path)
    engine = _engine(_EveryBar(symbols=symbols), ledger, _config(max_positions=2, max_short_positions=1))

    engine.run_round(_market(1, symbols=symbols))
    model = engine.run_round(_market(7, symbols=symbols)).by_model("her_bar")
    state = ledger.load_state("her_bar")

    assert model is not None and state is not None
    assert model.filled == 2 and len(state["positions"]) == 2
    assert sorted(position["symbol"] for position in state["positions"]) == sorted(symbols[:2])
    assert math.isclose(sum(model.rejections.values()), 4)
