"""scripts/measure_engine_rules.py: motor kurallarının defterdeki izi — salt okunur, getiri/R'ye bakmaz.

Ölçülen şey sayım tanımlarıdır: ters yönlü çakışmanın GİRİŞTE ve ARALIK olarak ayrılması, açık
pozisyonların (kapanmış defter tek başına görmez) sayıma dâhil edilmesi, `zero_size` retlerinin
yalnızca TEK barlık turlarda açık pozisyon sayısıyla eşleştirilmesi ve aracın deftere
yazmaması.
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from core.ledger import TRADE_COLUMNS, Ledger, new_state

_SPEC = importlib.util.spec_from_file_location(
    "measure_engine_rules",
    Path(__file__).resolve().parent.parent / "scripts" / "measure_engine_rules.py",
)
assert _SPEC and _SPEC.loader
mer = importlib.util.module_from_spec(_SPEC)
sys.modules["measure_engine_rules"] = mer
_SPEC.loader.exec_module(mer)

BTC, ETH = "BTC-USDT-SWAP", "ETH-USDT-SWAP"


def _t(hour: int, minute: int = 0) -> str:
    return f"2026-10-01T{hour:02d}:{minute:02d}:00+00:00"


def _trade(symbol: str, direction: str, opened: str, closed: str, *, leverage: float = 1.0) -> dict[str, Any]:
    row: dict[str, Any] = {column: "" for column in TRADE_COLUMNS}
    row.update(
        strategy="m", symbol=symbol, direction=direction, opened_at=opened, closed_at=closed,
        entry_price=100.0, exit_price=101.0, qty=1.0, notional=100.0, stop_price=97.0,
        risk_amount=100.0, leverage=leverage, margin=100.0, fee=0.0, slippage_cost=0.0,
        funding=0.0, pnl=1.0, exit_reason="tp",
    )
    return row


def _state(*opened: tuple[str, str, str]) -> dict[str, Any]:
    payload = new_state("m", initial_capital=10_000.0)
    payload["positions"] = [
        {"symbol": symbol, "direction": direction, "opened_at": opened_at}
        for symbol, direction, opened_at in opened
    ]
    return payload


def test_opposite_at_entry_requires_the_other_position_to_be_open_when_this_one_opens() -> None:
    trades = [
        _trade(BTC, "long", _t(1), _t(5)),
        _trade(BTC, "short", _t(3), _t(4)),      # long AÇIKKEN açıldı  -> girişte
    ]

    stats = mer.opposite_stats(mer.intervals_of(trades, None))

    assert stats == {"positions": 2, "opposite_at_entry": 1, "opposite_overlap": 2}


def test_overlap_without_an_entry_conflict_is_counted_separately() -> None:
    """Birinci pozisyon, ters yönlü olan AÇILMADAN önce açıldı: giriş çakışması yalnız ikincide."""
    trades = [_trade(BTC, "long", _t(1), _t(4)), _trade(BTC, "short", _t(2), _t(6))]

    stats = mer.opposite_stats(mer.intervals_of(trades, None))

    assert stats["opposite_at_entry"] == 1 and stats["opposite_overlap"] == 2


def test_different_symbols_or_the_same_direction_never_conflict() -> None:
    trades = [
        _trade(BTC, "long", _t(1), _t(5)),
        _trade(ETH, "short", _t(2), _t(4)),      # başka sembol
        _trade(BTC, "long", _t(6), _t(8)),       # aynı yön, çakışmıyor
    ]

    assert mer.opposite_stats(mer.intervals_of(trades, None))["opposite_overlap"] == 0


def test_touching_intervals_do_not_overlap() -> None:
    """Kapanış anı ile açılış anı eşitse çakışma YOK: dolum bir sonraki barın açılışındadır."""
    trades = [_trade(BTC, "long", _t(1), _t(3)), _trade(BTC, "short", _t(3), _t(5))]

    stats = mer.opposite_stats(mer.intervals_of(trades, None))

    assert (stats["opposite_at_entry"], stats["opposite_overlap"]) == (0, 0)


def test_open_positions_are_part_of_the_count() -> None:
    """Kapanmış defter tek başına, kapanmamış çakışmaları kaçırırdı (karar 33-DÜZELTME'nin dersi)."""
    trades = [_trade(BTC, "long", _t(1), _t(6))]

    stats = mer.opposite_stats(mer.intervals_of(trades, _state((BTC, "short", _t(4)))))

    assert stats == {"positions": 2, "opposite_at_entry": 1, "opposite_overlap": 2}


def test_partial_fills_count_as_one_position() -> None:
    """Birim POZİSYONDUR (`merge_fills`): kısmi çıkış satırı iki pozisyon sayılmaz."""
    trades = [
        _trade(BTC, "long", _t(1), _t(3)),
        _trade(BTC, "long", _t(1), _t(5)),      # aynı pozisyonun 2. dilimi
    ]

    assert mer.opposite_stats(mer.intervals_of(trades, None))["positions"] == 1


def test_leverage_counts_positions_opened_with_more_than_one_x() -> None:
    trades = [
        _trade(BTC, "long", _t(1), _t(2), leverage=1.0),
        _trade(ETH, "long", _t(1), _t(2), leverage=1.39),
        _trade(BTC, "short", _t(3), _t(4), leverage=5.0),
    ]

    assert mer.leverage_stats(trades) == {"closed": 3, "leveraged": 2}


def test_cash_stats_flag_a_tight_and_a_negative_cash_bar() -> None:
    rows = [
        {"cash": "5000", "equity": "10000"},
        {"cash": "100", "equity": "10000"},        # %1 < %2 -> tükenmiş
        {"cash": "-0.8", "equity": "10000"},       # negatif ve tükenmiş
    ]

    stats = mer.cash_stats(rows)

    assert (stats["bars"], stats["cash_tight"], stats["cash_negative"]) == (3, 2, 1)
    assert stats["min_cash_ratio"] == pytest.approx(-0.00008)


def _round(as_of: str, model: str, *, zero: int, bars: int = 1, **extra: int) -> dict[str, Any]:
    return {
        "as_of": as_of, "dry_run": False,
        "models": [{
            "model": model, "bars_processed": bars, "signals": 5,
            "rejections": {"zero_size": zero, **extra},
        }],
    }


def test_zero_size_is_matched_to_open_positions_only_in_single_bar_rounds() -> None:
    equity = [{"ts": _t(1), "open_positions": "3"}, {"ts": _t(2), "open_positions": "4"}]
    rounds = [
        _round(_t(1), "m", zero=2),
        _round(_t(2), "m", zero=1),
        _round(_t(2), "m", zero=7, bars=4),           # çok barlık tur: ret hangi bara ait bilinmez
        _round("2026-10-01T09:00:00+00:00", "m", zero=3),   # equity satırı yok
        _round(_t(1), "başka", zero=9),               # başka model
    ]

    result = mer.zero_size_by_open_positions(rounds, equity, "m")

    assert result == {"by_open": {3: 2, 4: 1}, "unmatched": 10}


def test_dry_run_rounds_are_ignored() -> None:
    dry = _round(_t(1), "m", zero=5)
    dry["dry_run"] = True

    assert mer.rejection_totals([dry], "m") == {"signals": 0}
    assert mer.zero_size_by_open_positions([dry], [{"ts": _t(1), "open_positions": "1"}], "m") == {
        "by_open": {}, "unmatched": 0,
    }


def test_rejection_totals_sum_every_reason_across_rounds() -> None:
    rounds = [
        _round(_t(1), "m", zero=1, duplicate_position=2),
        _round(_t(2), "m", zero=2, max_positions=4),
    ]

    assert mer.rejection_totals(rounds, "m") == {
        "signals": 10, "zero_size": 3, "duplicate_position": 2, "max_positions": 4,
    }


def test_measuring_a_ledger_never_writes_to_it(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ledger = Ledger(tmp_path)
    ledger.initialize_model("m", initial_capital=10_000.0)
    ledger.append_trades("m", [_trade(BTC, "long", _t(1), _t(5)), _trade(BTC, "short", _t(3), _t(4))])
    before = {path: path.read_bytes() for path in sorted(tmp_path.rglob("*")) if path.is_file()}

    code = mer.main(["--layer", "scalp", "--ledger-root", str(tmp_path), "--models", "m"])

    after = {path: path.read_bytes() for path in sorted(tmp_path.rglob("*")) if path.is_file()}
    out = capsys.readouterr().out
    assert code == 0 and before == after
    assert "(a) TERS YÖNLÜ POZİSYON" in out and "getiri/R'ye BAKILMAZ" in out
    row = next(line for line in out.splitlines() if line.startswith("m "))
    assert row.split()[1:] == ["2", "1", "2"]


def test_the_report_renders_the_rejection_section_only_with_history() -> None:
    base = {
        "model": "m", "positions": 1, "opposite_at_entry": 0, "opposite_overlap": 0,
        "lev_closed": 1, "lev_leveraged": 0, "bars": 1, "cash_tight": 0, "cash_negative": 0,
        "min_cash_ratio": 0.0,
    }

    assert "(c) RETLER" not in mer.format_report([base], layer="scalp")
    with_history = {**base, "rejections": {"signals": 4, "zero_size": 1}, "zero_size": {"by_open": {3: 1}, "unmatched": 0}}
    text = mer.format_report([with_history], layer="scalp")
    assert "(c) RETLER" in text and "{3: 1}" in text
