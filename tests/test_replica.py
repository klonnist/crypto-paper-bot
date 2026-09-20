"""Kopya (`is_replica`) modellerin boyutlandırması, limitleri ve raporlanışı.

Kopya bir YARIŞMACI DEĞİLDİR: kendi kaldıracıyla ve sabit teminatla koşar, ortalama R
sıralamasına girmez ve maliyet ölçeği kolonlarında `nan` alır. Buradaki testler tam olarak
bu ayrımın kodda tuttuğunu çiviler — ayrım kayarsa, farklı boyutlandırmayla koşan bir satır
tabloda yarışmacıların arasına karışır ve sıralama ölçmediği bir şeyi ölçüyormuş gibi
görünür.
"""

from __future__ import annotations

import math
from typing import Any

import pandas as pd
import pytest

from core.config import load_config
from core.metrics import acceptance_flags, compare, format_report, model_metrics
from core.portfolio import Bar, Portfolio, size_notional_fraction
from strategies.base import ModelLimits

TS = pd.Timestamp("2026-01-01 00:00:00", tz="UTC")
SYMBOL = "BTC-USDT-SWAP"
OTHER = "ETH-USDT-SWAP"


def _frictionless(**overrides: Any) -> dict[str, Any]:
    config = load_config()
    config.update(fee_rate=0.0, slippage_base=0.0, slippage_short_stop=0.0, **overrides)
    return config


def _limits(**overrides: Any) -> ModelLimits:
    defaults: dict[str, Any] = dict(
        max_positions=5, max_per_direction=3, max_portfolio_risk=0.08, leverage=10.0
    )
    defaults.update(overrides)
    return ModelLimits(**defaults)


# --------------------------------------------------------------------------- #
# Boyutlandırma: sabit teminat × kaldıraç
# --------------------------------------------------------------------------- #
def test_notional_fraction_with_leverage_is_fixed_margin_times_leverage() -> None:
    """0.5 × 10.000 = 5.000 notional, 10x'te 500 USDT teminat — kaynak sistemin kuralı."""
    sizing = size_notional_fraction(
        equity=10_000.0, free_cash=10_000.0, entry_price=100.0, fraction=0.5, leverage=10.0
    )

    assert sizing.notional == pytest.approx(5_000.0)
    assert sizing.margin == pytest.approx(500.0)
    assert sizing.leverage == pytest.approx(10.0)
    assert sizing.qty == pytest.approx(50.0)


def test_the_anchor_default_is_still_one_x() -> None:
    """Kural 15 değişmedi: çıpa kaldıraç kullanmaz, marj notional'ın tamamıdır."""
    sizing = size_notional_fraction(
        equity=10_000.0, free_cash=10_000.0, entry_price=100.0, fraction=0.5
    )

    assert sizing.leverage == pytest.approx(1.0)
    assert sizing.margin == pytest.approx(sizing.notional)


def test_replica_leverage_does_not_leak_into_risk_sizing() -> None:
    """Kaldıraç bildirimi yalnızca notional_fraction modunda okunur; kök tavan yerinde."""
    portfolio = Portfolio(_frictionless())
    result = portfolio.open_position(
        "m", symbol=SYMBOL, direction="long", stop_price=99.9,
        reference_price=100.0, ts=TS, marks={SYMBOL: 100.0}, limits=_limits(),
    )

    assert result.position is not None
    assert result.position.leverage == pytest.approx(float(load_config()["leverage_cap"]))


def test_replica_liquidation_is_still_modelled() -> None:
    """10x'te likidasyon gerçek bir risktir; kapatmak kopyayı haksız biçimde iyi gösterirdi."""
    portfolio = Portfolio(_frictionless())
    result = portfolio.open_position(
        "m", symbol=SYMBOL, direction="long", stop_price=85.0, reference_price=100.0,
        ts=TS, marks={SYMBOL: 100.0}, sizing_mode="notional_fraction",
        notional_fraction=0.5, limits=_limits(),
    )
    position = result.position
    assert position is not None, result.rejected
    # 10x + %0.5 bakım marjı: likidasyon girişin ~%9.5 altında, yani stop'un (%15) ÜSTÜNDE.
    # Stop'a hiç ulaşılmaz; kopyanın gerçek riski tam olarak budur.
    assert 88.0 < position.liq_price < 92.0
    assert position.liq_price > 85.0

    trades = portfolio.process_bar(
        "m", ts=TS, bars={SYMBOL: Bar(open=100.0, high=100.0, low=84.0, close=86.0)}
    )
    assert [trade.exit_reason for trade in trades] == ["liquidation"]


# --------------------------------------------------------------------------- #
# ModelLimits: kök kotayı DARALTIR
# --------------------------------------------------------------------------- #
def _fill(portfolio: Portfolio, symbol: str, direction: str, **kwargs: Any) -> Any:
    return portfolio.open_position(
        "m", symbol=symbol, direction=direction,  # type: ignore[arg-type]
        stop_price=95.0 if direction == "long" else 105.0,
        reference_price=100.0, ts=TS, marks={symbol: 100.0}, **kwargs,
    )


def test_same_direction_quota_is_enforced_for_longs_too() -> None:
    """Kök `max_short_positions` yalnızca short'u kapsar; kopya her iki yönü de sınırlar."""
    portfolio = Portfolio(_frictionless())
    limits = _limits(max_per_direction=2, max_portfolio_risk=None)
    for symbol in ("A", "B"):
        assert _fill(portfolio, symbol, "long", limits=limits).position is not None

    rejected = _fill(portfolio, "C", "long", limits=limits)

    assert rejected.position is None
    assert rejected.reason_code == "max_direction_positions"


def test_portfolio_risk_cap_rejects_instead_of_shrinking() -> None:
    """Kural 11'in "küçült" ilkesi burada geçerli değil: kaynak sistem işlemi hiç almaz.

    Atlama yine de sessiz değildir — sebep kodu tur raporunda sayılır (kural 15).
    """
    portfolio = Portfolio(_frictionless())
    # %1 risk × 3 pozisyon = %3; tavan %2 olduğunda üçüncüsü girmez.
    limits = _limits(max_portfolio_risk=0.02, max_per_direction=None)
    assert _fill(portfolio, "A", "long", limits=limits).position is not None
    assert _fill(portfolio, "B", "long", limits=limits).position is not None

    rejected = _fill(portfolio, "C", "long", limits=limits)

    assert rejected.position is None
    assert rejected.reason_code == "portfolio_risk_cap"


def test_model_limits_can_only_narrow_the_root_quota() -> None:
    """Kopya kendi kuralını bildirir, ölçümün ortak tavanını delemez."""
    portfolio = Portfolio(_frictionless(max_positions=2))
    limits = _limits(max_positions=9, max_per_direction=None, max_portfolio_risk=None)
    assert _fill(portfolio, "A", "long", limits=limits).position is not None
    assert _fill(portfolio, "B", "long", limits=limits).position is not None

    rejected = _fill(portfolio, "C", "long", limits=limits)

    assert rejected.reason_code == "max_positions"


def test_positions_without_limits_are_untouched() -> None:
    """Yarışmacı modeller bu eklemeden etkilenmez: limits=None hiçbir kapı eklemez."""
    portfolio = Portfolio(_frictionless())
    for symbol in ("A", "B", "C"):
        assert _fill(portfolio, symbol, "long").position is not None


# --------------------------------------------------------------------------- #
# Raporlama: sıralamaya girmez, nan alır, çıtaya tabi değildir
# --------------------------------------------------------------------------- #
def _trade(pnl: float, *, risk: float = 100.0, direction: str = "long") -> dict[str, Any]:
    """Defter şemasına TAM uyan bir işlem satırı (core/ledger.py eksik kolonu reddeder)."""
    return {
        "strategy": "m",
        "symbol": SYMBOL,
        "direction": direction,
        "opened_at": "2026-01-01T00:00:00+00:00",
        "closed_at": "2026-01-01T00:00:00+00:00",
        "entry_price": 100.0,
        "exit_price": 105.0,
        "qty": 20.0,
        "notional": 2000.0,
        "stop_price": 95.0,
        "risk_amount": risk,
        "leverage": 1.0,
        "margin": 2000.0,
        "fee": 2.0,
        "slippage_cost": 1.0,
        "funding": 0.0,
        "pnl": pnl,
        "exit_reason": "tp",
        "signal_reason": "test",
        "notes": "",
    }


def _metrics(model: str, *, pnl: float, count: int = 40, **flags: Any):
    return model_metrics(
        model,
        trades=[_trade(pnl) for _ in range(count)],
        equity_rows=[{"ts": "2026-01-01T00:00:00+00:00", "equity": 10_000.0 + pnl * count}],
        initial_capital=10_000.0,
        periods_per_year=2190.0,
        **flags,
    )


def test_replica_cost_columns_are_nan_even_though_the_stop_exists() -> None:
    """Sayı hesaplanabilir ama KIYAS anlamsızdır: kopyanın 1R'si başka bir birimdedir."""
    replica = _metrics("vwap_clone", pnl=50.0, is_replica=True)
    competitor = _metrics("vwap_managed", pnl=50.0)

    assert math.isnan(replica.total.cost_per_r)
    assert math.isnan(replica.total.avg_stop_distance_pct)
    # R'nin kendisi ölçülmeye devam eder: kopyanın kendi geçmişi okunabilir kalmalı.
    assert replica.total.avg_r == pytest.approx(0.5)
    assert not math.isnan(competitor.total.cost_per_r)


def test_replica_is_not_a_competitor() -> None:
    assert _metrics("vwap_clone", pnl=50.0, is_replica=True).is_competitor is False
    assert _metrics("buyhold", pnl=50.0, is_benchmark=True).is_competitor is False
    assert _metrics("vwap_managed", pnl=50.0).is_competitor is True


def test_replica_is_excluded_from_the_average_r_ranking() -> None:
    """Kopya en yüksek ortalama R'ye sahip olsa bile sıralamanın başına oturmaz."""
    metrics = [
        _metrics("vwap_clone", pnl=900.0, is_replica=True),   # ort. R = 9.0
        _metrics("vwap_managed", pnl=50.0),
        _metrics("scalp_fixed", pnl=10.0),
    ]

    report = format_report(metrics)
    competitor_block, _, replica_block = report.partition("REFERANS (dış sistem)")

    assert "vwap_clone" not in competitor_block
    assert "vwap_clone" in replica_block
    # Yarışmacılar kendi aralarında ortalama R'ye göre sıralanır, kopya araya girmez.
    assert competitor_block.index("vwap_managed") < competitor_block.index("scalp_fixed")


def test_anchor_and_replica_get_separate_sections() -> None:
    """İkisi de yarışma dışıdır ama ölçtükleri soru farklıdır: aynı bölümde toplanmazlar."""
    report = format_report(
        [
            _metrics("vwap_managed", pnl=50.0),
            _metrics("buyhold", pnl=20.0, is_benchmark=True),
            _metrics("vwap_clone", pnl=30.0, is_replica=True),
        ]
    )

    assert "REFERANS (yarışma dışı, kural 15)" in report
    assert "REFERANS (dış sistem)" in report
    assert report.index("REFERANS (yarışma dışı") < report.index("REFERANS (dış sistem)")


def test_acceptance_gates_skip_replicas() -> None:
    """Kapılar yalnızca yarışmacılara uygulanır: ölçmediği bir yarışta not vermek olurdu."""
    flags = acceptance_flags(
        [
            _metrics("vwap_managed", pnl=50.0),
            _metrics("random_ctrl", pnl=5.0),
            _metrics("vwap_clone", pnl=900.0, is_replica=True),
            _metrics("buyhold", pnl=1.0, is_benchmark=True),
        ],
        min_trades=30,
        stop_band_ratio=2.5,
        control_model="random_ctrl",
        edge_margin_r=0.15,
    )

    assert {item.model for item in flags} == {"vwap_managed", "random_ctrl"}


def test_replica_does_not_shift_the_stop_band_median() -> None:
    """Band medyanı yarışmacıların ortak volatilite ölçeğidir; kopya onu kendine çekemez."""
    competitors = [_metrics("a", pnl=50.0), _metrics("b", pnl=40.0)]
    with_replica = [*competitors, _metrics("vwap_clone", pnl=900.0, is_replica=True)]

    def band(metrics: list[Any]) -> tuple[float, float]:
        item = acceptance_flags(
            metrics, min_trades=30, stop_band_ratio=2.5,
            control_model="random_ctrl", edge_margin_r=0.15,
        )[0]
        return (item.band_low, item.band_high)

    assert band(competitors) == pytest.approx(band(with_replica))


def test_compare_carries_the_replica_flag(tmp_path: Any) -> None:
    from core.ledger import Ledger

    ledger = Ledger(tmp_path / "ledgers")
    for model in ("vwap_clone", "vwap_managed"):
        ledger.initialize_model(model, initial_capital=10_000.0)
        ledger.append_trades(model, [_trade(50.0) | {"strategy": model}])

    metrics = compare(
        ["vwap_clone", "vwap_managed"],
        ledger=ledger,
        config=load_config(),
        replicas=["vwap_clone"],
    )

    by_name = {item.model: item for item in metrics}
    assert by_name["vwap_clone"].is_replica is True
    assert by_name["vwap_managed"].is_replica is False
    assert math.isnan(by_name["vwap_clone"].total.cost_per_r)
