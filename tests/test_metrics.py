"""core/metrics.py: R birinci sınıf mı, long/short ayrışıyor mu, tanımsız metrik sessizce sıfır mı oluyor.

Projenin ana sorusu long/short karşılaştırması olduğu için ayrıştırmanın ve R'nin
bileşiklenmeden bağımsızlığının testi, "metrik hesaplandı mı" testinden daha önemlidir.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from core.config import load_config
from core.ledger import TRADE_COLUMNS, Ledger
from core.tags import TagError, format_tags
from core.metrics import (
    acceptance_flags,
    bootstrap_diff_ci,
    bootstrap_mean_ci,
    buy_hold_return,
    account_stats,
    annotate_loss_streak,
    arm_of,
    breakdown,
    compare,
    cost_per_r,
    direction_stats,
    exit_rule_of,
    loss_streak_of,
    session_of,
    format_report,
    holding_stats,
    merge_fills,
    model_metrics,
    periods_per_year,
    pnl_drawdown_pct,
    pooled_direction_stats,
    r_multiple,
    r_series,
    return_correlation,
    stop_distance_pct,
    symbol_of,
)


def _trade(
    *, direction: str = "long", pnl: float, risk: float | str = 100.0, **overrides: Any
) -> dict[str, Any]:
    row: dict[str, Any] = {column: "" for column in TRADE_COLUMNS}
    row.update(
        strategy="m",
        symbol="BTC-USDT-SWAP",
        direction=direction,
        qty=1.0,
        notional=100.0,
        stop_price=95.0,
        risk_amount=risk,
        leverage=1.0,
        margin=100.0,
        fee=0.2,
        funding=0.0,
        pnl=pnl,
        exit_reason="stop",
    )
    row.update(overrides)
    return row


def _equity(*values: float) -> list[dict[str, Any]]:
    return [
        {"ts": f"2026-01-0{i + 1}T00:00:00+00:00", "cash": value, "margin_used": 0.0,
         "unrealized_pnl": 0.0, "equity": value, "open_positions": 0}
        for i, value in enumerate(values)
    ]


# --------------------------------------------------------------------------- #
# R katsayısı
# --------------------------------------------------------------------------- #
def test_r_is_pnl_over_the_risk_taken_at_entry() -> None:
    assert r_multiple(_trade(pnl=250.0, risk=100.0)) == pytest.approx(2.5)
    assert r_multiple(_trade(pnl=-100.0, risk=100.0)) == pytest.approx(-1.0)


def test_liquidation_costs_more_than_one_r() -> None:
    """Likidasyon marjın tamamını götürür: R -1'in altına iner ve bu görünür olmalı."""
    trade = _trade(pnl=-320.0, risk=100.0, exit_reason="liquidation")
    assert r_multiple(trade) == pytest.approx(-3.2)
    stats = direction_stats([trade])
    assert stats.liquidations == 1
    assert stats.avg_r == pytest.approx(-3.2)


def test_trade_without_risk_amount_is_counted_not_silently_zeroed() -> None:
    stats = direction_stats([_trade(pnl=50.0, risk=""), _trade(pnl=50.0, risk=100.0)])
    assert stats.trades == 2
    assert stats.unmeasured == 1
    assert stats.avg_r == pytest.approx(0.5)  # ölçülemeyen satır ortalamayı aşağı çekmez
    assert r_multiple(_trade(pnl=50.0, risk=0.0)) is None


# --------------------------------------------------------------------------- #
# Bileşiklenmeden bağımsızlık — R'nin birinci sınıf olma gerekçesi
# --------------------------------------------------------------------------- #
def test_average_r_is_independent_of_compounding() -> None:
    """Aynı sinyal kalitesi, farklı bileşiklenme: ortalama R aynı, toplam PnL farklı.

    Bileşiklenen model (hesabı büyüdüğü için pozisyonu da büyüyen) toplam getiride öne
    geçer; ölçmek istediğimiz sinyal kalitesi ise ikisinde de birebir aynıdır.
    """
    compounding = [
        _trade(pnl=50.0, risk=100.0),
        _trade(pnl=105.0, risk=210.0),   # hesap büyüdü, riske edilen tutar da büyüdü
        _trade(pnl=-220.0, risk=220.0),
    ]
    flat = [
        _trade(pnl=50.0, risk=100.0),
        _trade(pnl=50.0, risk=100.0),
        _trade(pnl=-100.0, risk=100.0),
    ]

    compounding_stats = direction_stats(compounding)
    flat_stats = direction_stats(flat)

    assert compounding_stats.avg_r == pytest.approx(flat_stats.avg_r)
    assert compounding_stats.total_r == pytest.approx(flat_stats.total_r)
    assert compounding_stats.pnl != pytest.approx(flat_stats.pnl)  # getiri ayrışır, R ayrışmaz


# --------------------------------------------------------------------------- #
# Long/short ayrıştırması (CLAUDE.md: opsiyonel değil)
# --------------------------------------------------------------------------- #
def test_long_and_short_are_measured_separately() -> None:
    trades = [
        _trade(direction="long", pnl=-100.0, risk=100.0),
        _trade(direction="long", pnl=-100.0, risk=100.0),
        _trade(direction="short", pnl=300.0, risk=100.0),
    ]
    metrics = model_metrics(
        "m", trades=trades, equity_rows=_equity(10_000.0, 10_100.0),
        initial_capital=10_000.0, periods_per_year=2190.0,
    )

    assert metrics.long.trades == 2 and metrics.long.avg_r == pytest.approx(-1.0)
    assert metrics.short.trades == 1 and metrics.short.avg_r == pytest.approx(3.0)
    # Toplam, ayrışmanın yerine geçmez: birleşik değer shortun başarısını gizlerdi.
    assert metrics.total.trades == 3
    assert metrics.total.avg_r == pytest.approx(1.0 / 3.0)


def test_win_rate_and_payoff_are_split_per_direction() -> None:
    trades = [
        _trade(direction="short", pnl=200.0, risk=100.0),
        _trade(direction="short", pnl=-100.0, risk=100.0),
        _trade(direction="long", pnl=-100.0, risk=100.0),
    ]
    short = direction_stats(trades, direction="short")
    assert short.win_rate == pytest.approx(0.5)
    assert short.avg_win_r == pytest.approx(2.0)
    assert short.avg_loss_r == pytest.approx(-1.0)
    assert short.profit_factor == pytest.approx(2.0)

    long_stats = direction_stats(trades, direction="long")
    assert long_stats.win_rate == pytest.approx(0.0)
    assert math.isnan(long_stats.avg_win_r)  # kazanan işlem yok: uydurulmuş 0.0 değil


def test_max_drawdown_r_follows_the_cumulative_r_curve() -> None:
    stats = direction_stats([
        _trade(pnl=200.0, risk=100.0),   # +2R, zirve
        _trade(pnl=-100.0, risk=100.0),  # -1R
        _trade(pnl=-50.0, risk=100.0),   # -0.5R -> zirveden 1.5R düşüş
        _trade(pnl=100.0, risk=100.0),
    ])
    assert stats.max_drawdown_r == pytest.approx(-1.5)
    assert stats.total_r == pytest.approx(1.5)


# --------------------------------------------------------------------------- #
# Tanımsız metrikler
# --------------------------------------------------------------------------- #
def test_empty_history_reports_nan_not_zero() -> None:
    stats = direction_stats([])
    assert stats.trades == 0
    for value in (stats.avg_r, stats.total_r, stats.win_rate, stats.r_sharpe):
        assert math.isnan(value), "ölçülemedi ile sıfır çıktı aynı sayıya indirgenmemeli"


def test_single_trade_has_no_r_sharpe() -> None:
    assert math.isnan(direction_stats([_trade(pnl=100.0)]).r_sharpe)


# --------------------------------------------------------------------------- #
# Hesap düzeyi
# --------------------------------------------------------------------------- #
def test_account_stats_from_the_equity_curve() -> None:
    stats = account_stats(
        _equity(10_000.0, 11_000.0, 8_800.0, 9_900.0),
        initial_capital=10_000.0,
        periods_per_year=2190.0,
    )
    assert stats.final_equity == pytest.approx(9_900.0)
    assert stats.total_return == pytest.approx(-0.01)
    assert stats.max_drawdown == pytest.approx(-0.2)  # 11_000 -> 8_800
    assert stats.bars == 4


def test_account_stats_without_history_is_undefined_not_zero() -> None:
    stats = account_stats([], initial_capital=10_000.0, periods_per_year=2190.0)
    assert stats.bars == 0 and math.isnan(stats.sharpe) and math.isnan(stats.total_return)


def test_periods_per_year_follows_the_configured_timeframe() -> None:
    assert periods_per_year(load_config()) == pytest.approx(365 * 24 / 4)


# --------------------------------------------------------------------------- #
# Rapor
# --------------------------------------------------------------------------- #
def _metrics(model: str, trades: list[dict[str, Any]], final: float) -> Any:
    return model_metrics(
        model, trades=trades, equity_rows=_equity(10_000.0, final),
        initial_capital=10_000.0, periods_per_year=2190.0,
    )


def test_report_puts_r_before_total_return() -> None:
    report = format_report([_metrics("m", [_trade(pnl=100.0)], 10_100.0)])
    assert report.index("ort.R") < report.index("PnL(USDT)")
    assert "long" in report and "short" in report and "TOPLAM" in report
    assert "getiri" in report  # toplam getiri atılmaz, ikinci sırada durur


def test_report_ranks_by_average_r_not_by_total_return() -> None:
    """Toplam getiriye göre sıralamak, ayıklamaya çalıştığımız bileşiklenme etkisini geri sokardı."""
    sharp = _metrics("keskin", [_trade(pnl=300.0, risk=100.0)], 10_300.0)          # 3.0R
    lucky = _metrics("bileşiklenen", [_trade(pnl=1_000.0, risk=2_000.0)], 11_000.0)  # 0.5R
    report = format_report([lucky, sharp])
    assert report.index("keskin") < report.index("bileşiklenen")


def test_report_warns_about_unmeasurable_trades() -> None:
    report = format_report([_metrics("m", [_trade(pnl=100.0, risk="")], 10_100.0)])
    assert "risk_amount yok" in report


def test_report_renders_undefined_metrics_as_a_dash() -> None:
    assert "—" in format_report([_metrics("m", [], 10_000.0)])


# --------------------------------------------------------------------------- #
# Defterden okuma (salt okunur)
# --------------------------------------------------------------------------- #
def test_compare_reads_the_ledger_without_changing_it(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path)
    ledger.initialize_model("alpha", initial_capital=10_000.0)
    ledger.append_trades("alpha", [_trade(direction="short", pnl=150.0, risk=100.0)])
    ledger.append_equity("alpha", _equity(10_000.0, 10_150.0))
    before = (tmp_path / "alpha" / "trades.csv").read_text()

    (metrics,) = compare(["alpha"], ledger=ledger, config=load_config())

    assert metrics.model == "alpha"
    assert metrics.short.avg_r == pytest.approx(1.5)
    assert metrics.long.trades == 0
    assert metrics.account.final_equity == pytest.approx(10_150.0)
    assert (tmp_path / "alpha" / "trades.csv").read_text() == before


def test_compare_handles_a_model_that_never_traded(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path)
    ledger.initialize_model("sessiz", initial_capital=10_000.0)
    (metrics,) = compare(["sessiz"], ledger=ledger, config=load_config())
    assert metrics.total.trades == 0 and math.isnan(metrics.total.avg_r)


# --------------------------------------------------------------------------- #
# Maliyet ölçeği kolonları (CLAUDE.md > Rapor Kolonları)
# --------------------------------------------------------------------------- #
def test_stop_distance_and_cost_per_r_are_computed_per_trade() -> None:
    row = _trade(pnl=100.0, risk=100.0, entry_price=100.0, stop_price=95.0,
                 fee=2.0, slippage_cost=1.5)
    assert stop_distance_pct(row) == pytest.approx(5.0)
    assert cost_per_r(row) == pytest.approx(0.035)


def test_cost_per_r_exposes_the_tight_stop_penalty() -> None:
    """Dar stop kuran model aynı 1R'yi daha büyük notional ile taşır, R başına daha çok öder.

    Ana soruyu kirleten etki tam olarak budur: iki model eşit R üretse bile, stopu dar olan
    maliyet yüzünden geride kalır — bu bir sinyal farkı değildir ve tabloda görünmelidir.
    """
    wide = direction_stats([
        _trade(pnl=0.0, risk=100.0, entry_price=100.0, stop_price=95.0, fee=2.0, slippage_cost=1.0)
    ])
    tight = direction_stats([
        _trade(pnl=0.0, risk=100.0, entry_price=100.0, stop_price=99.0, fee=10.0, slippage_cost=5.0)
    ])
    assert wide.avg_stop_distance_pct == pytest.approx(5.0)
    assert tight.avg_stop_distance_pct == pytest.approx(1.0)
    assert tight.cost_per_r > wide.cost_per_r * 4


def test_cost_columns_are_split_per_direction() -> None:
    trades = [
        _trade(direction="long", pnl=0.0, risk=100.0, entry_price=100.0, stop_price=90.0,
               fee=1.0, slippage_cost=0.0),
        _trade(direction="short", pnl=0.0, risk=100.0, entry_price=100.0, stop_price=102.0,
               fee=5.0, slippage_cost=0.0),
    ]
    assert direction_stats(trades, direction="long").avg_stop_distance_pct == pytest.approx(10.0)
    assert direction_stats(trades, direction="short").avg_stop_distance_pct == pytest.approx(2.0)
    assert direction_stats(trades, direction="long").cost_per_r == pytest.approx(0.01)
    assert direction_stats(trades, direction="short").cost_per_r == pytest.approx(0.05)


def test_a_direction_with_no_trades_reports_nan_cost_not_zero() -> None:
    """0.0 yazmak, hiç short açmamış modeli 'maliyetsiz short yapan model' gibi gösterirdi."""
    stats = direction_stats([_trade(direction="long", pnl=50.0)], direction="short")
    assert stats.trades == 0
    assert math.isnan(stats.cost_per_r) and math.isnan(stats.avg_stop_distance_pct)


def test_report_shows_the_cost_scale_columns() -> None:
    report = format_report([_metrics("m", [
        _trade(pnl=100.0, risk=100.0, entry_price=100.0, stop_price=95.0,
               fee=2.0, slippage_cost=1.0)
    ], 10_100.0)])
    assert "stopMes.%" in report and "maliyet/R" in report
    assert report.index("maliyet/R") < report.index("PnL(USDT)")  # maliyet ölçeği getiriden önce


def test_direction_r_series_follows_close_order() -> None:
    """Yön bazlı R-Sharpe kapanış sırasına göre dizilmiş R dizisinden gelir."""
    late = _trade(pnl=100.0, risk=100.0, closed_at="2026-01-02T00:00:00+00:00")
    early = _trade(pnl=-50.0, risk=100.0, closed_at="2026-01-01T00:00:00+00:00")
    shuffled = direction_stats([late, early])
    ordered = direction_stats([early, late])
    assert shuffled.max_drawdown_r == pytest.approx(ordered.max_drawdown_r)
    assert ordered.max_drawdown_r == pytest.approx(-0.5)  # önce -0.5R, sonra toparlıyor


# --------------------------------------------------------------------------- #
# Referans (benchmark) satırı (CLAUDE.md kural 15)
# --------------------------------------------------------------------------- #
def _benchmark_metrics(**overrides: Any) -> Any:
    """Stop'suz, R'siz bir referans modelin metrikleri."""
    trades = [
        _trade(direction="long", pnl=500.0, risk="", stop_price="", entry_price=100.0),
    ]
    payload: dict[str, Any] = dict(
        trades=trades,
        equity_rows=[{"equity": 10000.0}, {"equity": 10500.0}],
        initial_capital=10000.0,
        periods_per_year=periods_per_year(load_config()),
        is_benchmark=True,
    )
    payload.update(overrides)
    return model_metrics("buyhold", **payload)


def test_benchmark_cost_columns_are_nan() -> None:
    stats = _benchmark_metrics().total
    assert math.isnan(stats.avg_stop_distance_pct)
    assert math.isnan(stats.cost_per_r)


def test_benchmark_cost_columns_stay_nan_even_with_a_stopped_row() -> None:
    """Garanti defterin içeriğine bırakılmaz: bayrak koşulsuz kazanır."""
    stats = _benchmark_metrics(
        trades=[_trade(direction="long", pnl=50.0, risk=100.0, entry_price=100.0)]
    ).total
    assert math.isnan(stats.avg_stop_distance_pct)
    assert math.isnan(stats.cost_per_r)


def test_competitor_cost_columns_are_still_measured() -> None:
    """Bayrak yalnızca referansı susturur, yarışmacıyı değil.

    Aynı stop'lu satır: is_benchmark=True iken nan, False iken ölçülür.
    """
    stopped = [_trade(direction="long", pnl=50.0, risk=100.0, entry_price=100.0)]
    competitor = _benchmark_metrics(trades=stopped, is_benchmark=False).total
    reference = _benchmark_metrics(trades=stopped, is_benchmark=True).total

    assert competitor.avg_stop_distance_pct == pytest.approx(5.0)  # |100-95|/100
    assert not math.isnan(competitor.cost_per_r)
    assert math.isnan(reference.avg_stop_distance_pct)


def test_benchmark_account_return_is_still_reported() -> None:
    """Çıpanın tek işi budur: hesap getirisi zemin olarak durmalı."""
    account = _benchmark_metrics().account
    assert account.total_return == pytest.approx(0.05)


def test_benchmark_is_reported_outside_the_ranking() -> None:
    competitor = model_metrics(
        "model_a",
        trades=[_trade(direction="long", pnl=50.0)],
        equity_rows=[{"equity": 10000.0}, {"equity": 10050.0}],
        initial_capital=10000.0,
        periods_per_year=periods_per_year(load_config()),
    )
    report = format_report([_benchmark_metrics(), competitor])

    assert "REFERANS" in report
    # Referans, ortalama R'si nan olmasına rağmen yarışmacının üstüne çıkmaz.
    assert report.index("model_a") < report.index("REFERANS") < report.index("buyhold")


def test_benchmark_has_no_unmeasured_warning() -> None:
    """Referansta R'siz satır beklenendir; uyarı gerçek anomaliler için saklanır."""
    assert "UYARI" not in format_report([_benchmark_metrics()])


def test_competitor_keeps_the_unmeasured_warning() -> None:
    report = format_report([_benchmark_metrics(is_benchmark=False)])
    assert "UYARI" in report


def test_compare_marks_named_benchmarks(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path)
    for model in ("buyhold", "model_a"):
        ledger.reset_model(model, initial_capital=10000.0)
        ledger.append_trades(model, [_trade(direction="long", pnl=50.0)])

    results = compare(
        ["buyhold", "model_a"], ledger=ledger, config=load_config(), benchmarks=["buyhold"]
    )
    marked = {item.model: item.is_benchmark for item in results}
    assert marked == {"buyhold": True, "model_a": False}
    assert math.isnan(results[0].total.cost_per_r)
    assert not math.isnan(results[1].total.cost_per_r)


# --------------------------------------------------------------------------- #
# Havuzlanmış yön karşılaştırması (dashboard'un üst paneli)
# --------------------------------------------------------------------------- #
def test_pooled_stats_weight_every_trade_not_every_model() -> None:
    """Havuz işleme oy verir: 1 işlemlik bir model 10 işlemlik bir modeli dengeleyemez."""
    pooled = pooled_direction_stats({
        "az": [_trade(direction="long", pnl=1000.0, risk=100.0)],           # +10R
        "cok": [_trade(direction="long", pnl=-100.0, risk=100.0) for _ in range(10)],  # 10 × -1R
    })
    # Model ortalamalarının ortalaması (+10 ile -1) +4.5 olurdu; havuz 11 işleme bakar.
    assert pooled["long"].trades == 11
    assert pooled["long"].avg_r == pytest.approx((10.0 - 10.0) / 11.0)


def test_pooled_stats_keep_the_directions_apart() -> None:
    pooled = pooled_direction_stats({
        "a": [_trade(direction="long", pnl=100.0, risk=100.0, funding=-3.0)],
        "b": [_trade(direction="short", pnl=-50.0, risk=100.0, funding=5.0)],
    })
    assert pooled["long"].avg_r == pytest.approx(1.0)
    assert pooled["short"].avg_r == pytest.approx(-0.5)
    assert pooled["long"].funding == pytest.approx(-3.0)
    assert pooled["short"].funding == pytest.approx(5.0)
    assert pooled["total"].trades == 2


def test_pooled_stats_with_no_trades_are_nan_not_zero() -> None:
    pooled = pooled_direction_stats({"a": []})
    assert math.isnan(pooled["short"].avg_r)
    assert pooled["short"].trades == 0


# --------------------------------------------------------------------------- #
# Kabul çıtası (üç bayrak)
# --------------------------------------------------------------------------- #
def _competitor(
    model: str, *, avg_r_trades: list[dict[str, Any]], final: float = 10_100.0
) -> Any:
    return model_metrics(
        model, trades=avg_r_trades, equity_rows=_equity(10_000.0, final),
        initial_capital=10_000.0, periods_per_year=2190.0,
    )


def _winner(model: str, *, n: int = 40, pnl: float = 50.0, final: float = 12_000.0) -> Any:
    return _competitor(
        model,
        avg_r_trades=[
            _trade(pnl=pnl, risk=100.0, entry_price=100.0, stop_price=97.0,
                   closed_at=f"2026-01-{index + 1:02d}T00:00:00+00:00")
            for index in range(n)
        ],
        final=final,
    )


def _flags(metrics: list[Any], **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = dict(
        min_trades=30, stop_band_ratio=2.5, control_model="ctrl", edge_margin_r=0.15
    )
    payload.update(overrides)
    return {item.model: item for item in acceptance_flags(metrics, **payload)}


def test_acceptance_needs_both_gates() -> None:
    flags = _flags([
        _winner("good"),
        _competitor("ctrl", avg_r_trades=[
            _trade(pnl=-10.0, risk=100.0, entry_price=100.0, stop_price=97.0) for _ in range(30)
        ], final=9_800.0),
        model_metrics("bench", trades=[], equity_rows=_equity(10_000.0, 10_500.0),
                      initial_capital=10_000.0, periods_per_year=2190.0, is_benchmark=True),
    ])
    assert flags["good"].sample and flags["good"].edge
    assert flags["good"].passed
    # Kontrol grubu kendini geçemez: kendisiyle arasındaki fark 0, gereken marj 0.15.
    assert not flags["ctrl"].edge


def test_small_sample_fails_even_with_a_great_average() -> None:
    flags = _flags([_winner("tiny", n=3, final=15_000.0)])
    assert not flags["tiny"].sample
    assert not flags["tiny"].passed
    assert flags["tiny"].measured_trades == 3


def test_sample_gate_uses_the_configured_threshold() -> None:
    """Eşik config'ten gelir; 29 işlem 30'luk çıtayı geçmez, 30 geçer."""
    assert not _flags([_winner("m", n=29)])["m"].sample
    assert _flags([_winner("m", n=30)])["m"].sample


def test_benchmarks_get_no_acceptance_row() -> None:
    """Çıpa yarışmacı değildir (kural 15): ölçmediği bir yarışta not almaz."""
    flags = _flags([
        _winner("good"),
        model_metrics("bench", trades=[], equity_rows=_equity(10_000.0, 10_500.0),
                      initial_capital=10_000.0, periods_per_year=2190.0, is_benchmark=True),
    ])
    assert "bench" not in flags


def test_band_warns_about_a_model_outside_the_stop_scale() -> None:
    """Bandın çapası yarışmacı medyanıdır; çok dar stop kuran model bandın dışına düşer."""
    wide = [
        _competitor(f"wide{index}", avg_r_trades=[
            _trade(pnl=10.0, risk=100.0, entry_price=100.0, stop_price=97.0)
        ]) for index in range(3)
    ]
    tight = _competitor("tight", avg_r_trades=[
        _trade(pnl=10.0, risk=100.0, entry_price=100.0, stop_price=99.9)  # %0.1 stop
    ])
    flags = _flags([*wide, tight])
    assert flags["wide0"].band
    assert not flags["tight"].band
    assert flags["tight"].band_low == pytest.approx(3.0 / math.sqrt(2.5))


def test_band_is_a_warning_not_a_gate() -> None:
    """Bandın dışında kalmak bir KUSUR değil kıyas koşuludur: doğrulamayı engellemez."""
    wide = [
        _competitor(f"wide{index}", avg_r_trades=[
            _trade(pnl=10.0, risk=100.0, entry_price=100.0, stop_price=97.0)
        ]) for index in range(3)
    ]
    # İki kapıyı da geçen ama stop'u bandın çok dışında (çok dar) bir model.
    tight = _competitor("tight", avg_r_trades=[
        _trade(pnl=50.0, risk=100.0, entry_price=100.0, stop_price=99.9,
               closed_at=f"2026-01-{index + 1:02d}T00:00:00+00:00")
        for index in range(40)
    ], final=12_000.0)
    flags = _flags([*wide, tight])
    assert flags["tight"].band is False
    assert flags["tight"].sample and flags["tight"].edge
    assert flags["tight"].passed  # band `passed`'a GİRMEZ


def test_edge_needs_the_configured_margin_over_the_control() -> None:
    """Kontrolü kıl payı geçmek yetmez: çekilişin kendi gürültüsü o farkı üretebilir."""
    control = _competitor("ctrl", avg_r_trades=[
        _trade(pnl=20.0, risk=100.0, entry_price=100.0, stop_price=97.0,
               closed_at=f"2026-01-{index + 1:02d}T00:00:00+00:00")
        for index in range(40)
    ], final=10_800.0)                      # kontrolün ort. R'si +0.20
    barely = _winner("barely", pnl=30.0)    # +0.30 → fark 0.10, marj 0.15
    clearly = _winner("clearly", pnl=40.0)  # +0.40 → fark 0.20

    flags = _flags([control, barely, clearly])
    assert flags["barely"].control_avg_r == pytest.approx(0.20)
    assert not flags["barely"].edge
    assert flags["clearly"].edge
    assert flags["barely"].edge_margin_r == pytest.approx(0.15)


def test_edge_margin_is_a_minimum_not_a_strict_excess() -> None:
    """Eşik "en az bu kadar"dır (`>=`), "bundan fazla" değil.

    Sınırın ULP düzeyinde test edilmesi anlamsız olurdu: karşılaştırma iki kayan noktalı
    ORTALAMANIN farkı üzerinden yapılır ve 0.15 ile 0.1499999999999999 arasındaki ayrım
    gürültünün altındadır. Test bu yüzden eşiğin iki yanını açıkça ayrı noktalardan
    yoklar; koda yapay bir tolerans eklemek, olmayan bir hassasiyeti iddia etmek olurdu.
    """
    control = _competitor("ctrl", avg_r_trades=[
        _trade(pnl=10.0, risk=100.0, entry_price=100.0, stop_price=97.0,
               closed_at=f"2026-01-{index + 1:02d}T00:00:00+00:00")
        for index in range(40)
    ], final=10_400.0)                                    # kontrol +0.10
    flags = _flags([control, _winner("uzak", pnl=26.0)])   # +0.26 → fark 0.16 >= 0.15
    assert flags["uzak"].edge
    flags = _flags([control, _winner("yakin", pnl=24.0)])  # +0.24 → fark 0.14 < 0.15
    assert not flags["yakin"].edge


def test_edge_requires_beating_the_benchmark_return() -> None:
    """Ortalama R pozitif ama piyasa daha çok kazandırdıysa edge yanmaz (kural 15)."""
    flags = _flags([
        _winner("beaten", final=10_100.0),
        model_metrics("bench", trades=[], equity_rows=_equity(10_000.0, 13_000.0),
                      initial_capital=10_000.0, periods_per_year=2190.0, is_benchmark=True),
    ])
    assert flags["beaten"].sample and flags["beaten"].band
    assert not flags["beaten"].edge
    assert flags["beaten"].benchmark_return == pytest.approx(0.30)


def test_missing_control_is_warned_not_silently_passed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level("WARNING"):
        flags = _flags([_winner("solo")], control_model="yok")
    assert math.isnan(flags["solo"].control_avg_r)
    assert "yok" in caplog.text


def test_control_with_no_trades_blocks_edge_and_says_so(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Kontrol kümede AMA henüz ölçülmemişse edge verilmez — ve bu sessiz olmaz.

    Bu, bilinçli bir davranış değişikliğidir. Eskiden kontrolün R'si yoksa marj koşulu
    düşüyor ve edge veriliyordu; n=0 ile n=4 aynı durumun iki hâli olduğu için bu şu
    tuhaflığı üretiyordu: bir model ilk günlerde (kontrolün hiç işlemi yokken) E rozetini
    alıyor, kontrol İLK işlemini kapattığı anda rozeti kaybediyordu. Rozetin kontrolün
    işlem yapmamasıyla kazanılması, kapının kendi amacının tersidir.

    Kontrolün kümede HİÇ OLMAMASI ayrı bir durumdur ve koşulu düşürmeye devam eder
    (bkz. `test_missing_control_is_not_the_same_as_an_unmeasured_one`).
    """
    with caplog.at_level("WARNING"):
        flags = _flags([
            _winner("m"),
            _competitor("ctrl", avg_r_trades=[]),
        ])
    assert math.isnan(flags["m"].control_avg_r)
    assert not flags["m"].edge
    assert flags["m"].control_trades == 0
    assert "örneklem kapısını geçmedi" in caplog.text


def test_highest_benchmark_sets_the_floor() -> None:
    """Birden çok çıpa varsa en yükseği zemindir: kolay olanı seçmek çıtayı indirirdi."""
    flags = _flags([
        _winner("m", final=11_000.0),
        model_metrics("low", trades=[], equity_rows=_equity(10_000.0, 10_050.0),
                      initial_capital=10_000.0, periods_per_year=2190.0, is_benchmark=True),
        model_metrics("high", trades=[], equity_rows=_equity(10_000.0, 12_000.0),
                      initial_capital=10_000.0, periods_per_year=2190.0, is_benchmark=True),
    ])
    assert flags["m"].benchmark_return == pytest.approx(0.20)
    assert not flags["m"].edge


# --------------------------------------------------------------------------- #
# Modeller arası getiri korelasyonu
# --------------------------------------------------------------------------- #
def test_identical_curves_correlate_perfectly() -> None:
    result = return_correlation({"a": _equity(100.0, 110.0, 99.0, 120.0),
                                 "b": _equity(100.0, 110.0, 99.0, 120.0)})
    assert result["models"] == ["a", "b"]
    assert result["matrix"][0][1] == pytest.approx(1.0)


def test_mirrored_curves_correlate_negatively() -> None:
    # Getiriler birebir zıt: +10/-10/+20% ile -10/+10/-20%.
    result = return_correlation({"up": _equity(100.0, 110.0, 99.0, 118.8),
                                 "down": _equity(100.0, 90.0, 99.0, 79.2)})
    assert result["matrix"][0][1] == pytest.approx(-1.0)


def test_short_overlap_is_nan_not_zero() -> None:
    """0.0 'ilişkisiz' demektir; ölçülemeyen bir ilişkiyi öyle göstermek yanıltır."""
    result = return_correlation({"a": _equity(100.0, 110.0), "b": _equity(100.0, 90.0)})
    index = result["models"].index("a")
    other = result["models"].index("b")
    assert math.isnan(result["matrix"][index][other])
    assert result["overlap"][index][other] == 1


def test_overlap_is_computed_pairwise_not_globally() -> None:
    """Yeni eklenen kısa geçmişli bir model, DİĞER çiftlerin örneklemini kırpmaz."""
    long_rows = _equity(100.0, 102.0, 104.0, 103.0, 106.0)
    result = return_correlation({
        "a": long_rows,
        "b": long_rows,
        "yeni": long_rows[-2:],
    })
    a, b, yeni = (result["models"].index(name) for name in ("a", "b", "yeni"))
    assert result["overlap"][a][b] == 4
    assert result["overlap"][a][yeni] < result["overlap"][a][b]


def test_flat_curve_has_no_correlation() -> None:
    result = return_correlation({"flat": _equity(100.0, 100.0, 100.0, 100.0),
                                 "moving": _equity(100.0, 110.0, 99.0, 120.0)})
    index = result["models"].index("flat")
    assert math.isnan(result["matrix"][index][result["models"].index("moving")])


# --------------------------------------------------------------------------- #
# Kırılımlar (kol / sembol) — scalp katmanının rapor kolonları
# --------------------------------------------------------------------------- #
def _tagged(arm: str, *, symbol: str = "BTC-USDT-SWAP", pnl: float, risk: float = 100.0,
            fee: float = 1.0, slippage: float = 0.5, closed_at: str = "2026-03-02T00:00:00+00:00",
            direction: str = "long") -> dict[str, Any]:
    return {
        "symbol": symbol,
        "direction": direction,
        "closed_at": closed_at,
        "entry_price": "100",
        "stop_price": "99",
        "risk_amount": str(risk),
        "pnl": str(pnl),
        "fee": str(fee),
        "slippage_cost": str(slippage),
        "signal_reason": format_tags("kurulum", arm=arm, post_r=0.1),
        "exit_reason": "target",
    }


def test_breakdown_groups_by_arm() -> None:
    """Kol kırılımı: her kolun kaç işlem yaptığı, ortalama R'si ve kazanma oranı."""
    trades = [
        _tagged("vwap_pullback", pnl=200.0),
        _tagged("vwap_pullback", pnl=-100.0),
        _tagged("momentum_burst", pnl=150.0),
    ]

    groups = breakdown(trades, key=arm_of)

    assert set(groups) == {"vwap_pullback", "momentum_burst"}
    assert groups["vwap_pullback"].trades == 2
    assert groups["vwap_pullback"].avg_r == pytest.approx(0.5)
    assert groups["vwap_pullback"].win_rate == pytest.approx(0.5)
    assert groups["momentum_burst"].avg_r == pytest.approx(1.5)


def test_breakdown_groups_by_symbol_with_cost_per_r() -> None:
    """Sembol kırılımı ince kitaplı sembollerde kayma varsayımını denetlemek içindir."""
    trades = [
        _tagged("vwap_pullback", symbol="PENGU-USDT-SWAP", pnl=100.0, fee=4.0, slippage=6.0),
        _tagged("vwap_pullback", symbol="BTC-USDT-SWAP", pnl=100.0, fee=1.0, slippage=0.5),
    ]

    groups = breakdown(trades, key=symbol_of)

    assert groups["PENGU-USDT-SWAP"].cost_per_r == pytest.approx(0.1)
    assert groups["BTC-USDT-SWAP"].cost_per_r == pytest.approx(0.015)


def test_breakdown_is_sorted_for_stable_output() -> None:
    """JSON her turda baştan yazılır: grup sırası kararlı olmalı ki diff anlamlı kalsın."""
    trades = [_tagged("momentum_burst", pnl=10.0), _tagged("funding_spike_fade", pnl=10.0)]

    assert list(breakdown(trades, key=arm_of)) == ["funding_spike_fade", "momentum_burst"]


def test_breakdown_totals_match_the_model_total() -> None:
    """Kırılım toplamı model toplamından AYRILAMAZ: ayrılırsa biri yanlış ölçüyor demektir."""
    trades = [
        _tagged("vwap_pullback", pnl=200.0),
        _tagged("rsi2_reversal", pnl=-100.0, direction="short"),
        _tagged("momentum_burst", pnl=50.0),
    ]

    groups = breakdown(trades, key=arm_of)
    total = direction_stats(trades, direction="total")

    assert sum(group.trades for group in groups.values()) == total.trades
    assert sum(group.total_r for group in groups.values()) == pytest.approx(total.total_r)


def test_breakdown_raises_when_the_arm_tag_is_missing() -> None:
    """Etiketsiz satırı atlamak, kırılım toplamını sessizce eksiltirdi."""
    orphan = _tagged("vwap_pullback", pnl=10.0)
    orphan["signal_reason"] = "etiketsiz eski satır"

    with pytest.raises(TagError):
        breakdown([orphan], key=arm_of)


# --------------------------------------------------------------------------- #
# Seans kırılımı (ölçüm, kural değil)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("opened_at", "expected"),
    [
        ("2026-09-15T00:00:00+00:00", "00-07_asya"),
        ("2026-09-15T06:59:59+00:00", "00-07_asya"),
        ("2026-09-15T07:00:00+00:00", "07-12_avrupa"),
        ("2026-09-15T11:59:59+00:00", "07-12_avrupa"),
        ("2026-09-15T12:00:00+00:00", "12-16_abd"),
        ("2026-09-15T15:59:59+00:00", "12-16_abd"),
        ("2026-09-15T16:00:00+00:00", "16-24_gece"),
        ("2026-09-15T23:59:59+00:00", "16-24_gece"),
    ],
)
def test_session_boundaries_are_fixed_in_utc(opened_at: str, expected: str) -> None:
    """Sınırlar UTC'de SABİT: yerel saat kullanmak yaz saatinde kırılımı kaydırırdı."""
    assert session_of(_trade(pnl=1.0, opened_at=opened_at)) == expected


def test_a_naive_timestamp_is_read_as_utc() -> None:
    """Zaman dilimsiz damga UTC sayılır; yerel saate çevirmek defteri okuyan makineye
    bağlı bir kırılım üretirdi."""
    assert session_of(_trade(pnl=1.0, opened_at="2026-09-15T14:00:00")) == "12-16_abd"


def test_session_uses_the_open_not_the_close() -> None:
    """Soru "bu kurulum hangi koşulda ALINDI"; kapanışa göre gruplamak gece açılıp sabah
    stoplanan pozisyonu sabahın hanesine yazardı."""
    overnight = _trade(
        pnl=-100.0, opened_at="2026-09-14T22:00:00+00:00", closed_at="2026-09-15T08:00:00+00:00"
    )
    assert session_of(overnight) == "16-24_gece"


def test_session_groups_keep_the_model_total() -> None:
    """Seans POZİSYONUN özelliğidir: bir pozisyonun tüm dilimleri aynı gruba düşer, yani
    grupların işlem sayısı toplamı model toplamından ayrılmaz (`exit_rule`in aksine)."""
    trades = [
        _trade(pnl=200.0, opened_at="2026-09-15T03:00:00+00:00"),
        _trade(pnl=-100.0, opened_at="2026-09-15T14:00:00+00:00", direction="short"),
        _trade(pnl=50.0, opened_at="2026-09-15T20:00:00+00:00"),
    ]

    groups = breakdown(trades, key=session_of)
    total = direction_stats(trades, direction="total")

    assert sum(group.trades for group in groups.values()) == total.trades
    assert sum(group.total_r for group in groups.values()) == pytest.approx(total.total_r)


def test_session_raises_when_the_open_time_is_unreadable() -> None:
    """Satırı gruptan düşürmek kırılım toplamını sessizce eksiltirdi (`arm_of` gerekçesi)."""
    with pytest.raises(ValueError):
        breakdown([_trade(pnl=10.0, opened_at="")], key=session_of)


# --------------------------------------------------------------------------- #
# Kayıp serisi kırılımı (ölçüm, kural değil)
# --------------------------------------------------------------------------- #
def _sequence(pnls: list[float]) -> list[dict[str, Any]]:
    """Ardışık, çakışmayan pozisyonlar: i. pozisyon i:00'da açılır, i:30'da kapanır."""
    return [
        _trade(
            pnl=pnl,
            opened_at=f"2026-09-15T{i:02d}:00:00+00:00",
            closed_at=f"2026-09-15T{i:02d}:30:00+00:00",
        )
        for i, pnl in enumerate(pnls)
    ]


def test_loss_streak_counts_consecutive_losses_before_the_open() -> None:
    rows = annotate_loss_streak(_sequence([-1.0] * 7))
    assert [loss_streak_of(row) for row in rows] == ["0", "1", "2", "3", "4", "5+", "5+"]


def test_a_win_resets_the_streak() -> None:
    rows = annotate_loss_streak(_sequence([-1.0, -1.0, -1.0, 5.0, -1.0, -1.0]))
    assert [loss_streak_of(row) for row in rows] == ["0", "1", "2", "3", "0", "1"]


def test_a_breakeven_exit_also_resets_the_streak() -> None:
    """Başabaş kapanan işlem bir KAYIP DEĞİLDİR.

    Kayıp saymak serileri yapay uzatırdı — üstelik tam da breakeven stop kullanan
    modellerde (13/14/15), yani kıyasın bir tarafında.
    """
    rows = annotate_loss_streak(_sequence([-1.0, -1.0, 0.0, -1.0]))
    assert [loss_streak_of(row) for row in rows] == ["0", "1", "2", "0"]


def test_only_trades_closed_before_the_open_are_counted() -> None:
    """Sayılan şey modelin KARAR ANINDA görebildiğidir (kural 16: yalnızca kapanmışlar).

    Burada ikinci pozisyon, birincisi hâlâ AÇIKKEN açılıyor; birincinin kaybı onun
    kovasına giremez. Kesimi `closed_at`e taşımak, modelin o an sahip olmadığı bir
    bilgiyle ölçüm kurmak olurdu.
    """
    rows = annotate_loss_streak([
        _trade(pnl=-1.0, opened_at="2026-09-15T00:00:00+00:00",
               closed_at="2026-09-15T05:00:00+00:00"),          # uzun süre açık kalıyor
        _trade(pnl=-1.0, opened_at="2026-09-15T01:00:00+00:00",
               closed_at="2026-09-15T06:00:00+00:00"),          # birincisi HÂLÂ açıkken açıldı
        _trade(pnl=-1.0, opened_at="2026-09-15T07:00:00+00:00",
               closed_at="2026-09-15T08:00:00+00:00"),          # ikisi de kapandıktan sonra
    ])
    assert [loss_streak_of(row) for row in rows] == ["0", "0", "2"]


def test_every_fill_of_a_position_lands_in_the_same_bucket() -> None:
    """Seri POZİSYONUN özelliğidir: dilimler bölünmez, grup toplamı model toplamıyla eşleşir."""
    opened = "2026-09-15T01:00:00+00:00"
    rows = annotate_loss_streak([
        _trade(pnl=-1.0, opened_at="2026-09-15T00:00:00+00:00",
               closed_at="2026-09-15T00:30:00+00:00"),
        # Tek pozisyonun iki dilimi: aynı opened_at, farklı closed_at.
        _trade(pnl=3.0, opened_at=opened, closed_at="2026-09-15T02:00:00+00:00",
               exit_reason="partial"),
        _trade(pnl=-4.0, opened_at=opened, closed_at="2026-09-15T03:00:00+00:00"),
    ])
    assert [loss_streak_of(row) for row in rows] == ["0", "1", "1"]

    groups = breakdown(rows, key=loss_streak_of)
    total = direction_stats(rows, direction="total")
    assert sum(group.trades for group in groups.values()) == total.trades == 2


def test_loss_streak_raises_when_the_rows_were_not_annotated() -> None:
    """Alanın yokluğu "seri sıfırdı" değil "ön hazırlık koşmadı" demektir."""
    with pytest.raises(ValueError):
        breakdown([_trade(pnl=-1.0)], key=loss_streak_of)


# --------------------------------------------------------------------------- #
# Çıkış kuralı kırılımı (kural 13c)
# --------------------------------------------------------------------------- #
def test_the_missing_exit_rule_tag_means_the_initial_stop() -> None:
    """Etiketin YOKLUĞU bir bilgidir: uydurma bir `initial` değeri üretilmez."""
    assert exit_rule_of(_trade(pnl=-100.0, exit_reason="stop", notes="")) == "stop"


def test_the_exit_rule_tag_is_appended_to_the_reason() -> None:
    trade = _trade(pnl=150.0, exit_reason="stop", notes="x | exit_rule=giveback")

    assert exit_rule_of(trade) == "stop:giveback"


def test_a_partial_slice_never_collides_with_a_partial_stop_rule() -> None:
    """Ad uzayları çakışır: `partial` hem bir exit_reason hem bir stop kuralıdır."""
    slice_row = _trade(pnl=75.0, exit_reason="partial", notes="")
    pulled = _trade(pnl=70.0, exit_reason="stop", notes="x | exit_rule=partial")

    assert exit_rule_of(slice_row) != exit_rule_of(pulled)
    assert (exit_rule_of(slice_row), exit_rule_of(pulled)) == ("partial", "stop:partial")


def test_the_time_stop_is_separable_from_other_strategy_exits() -> None:
    """`signal` hem zaman stop'unu hem başka bir strateji çıkışını anlatır (kural 13c)."""
    timed = _trade(pnl=-20.0, exit_reason="signal", notes="x | exit_rule=time_stop")
    other = _trade(pnl=-20.0, exit_reason="signal", notes="")

    assert exit_rule_of(timed) == "signal:time_stop"
    assert exit_rule_of(other) == "signal"


def test_breakdown_groups_by_exit_rule() -> None:
    rows = [
        _trade(pnl=100.0, exit_reason="stop", notes="x | exit_rule=breakeven"),
        _trade(pnl=-100.0, exit_reason="stop", notes=""),
        _trade(pnl=-100.0, exit_reason="stop", notes=""),
    ]

    groups = breakdown(rows, key=exit_rule_of)

    assert set(groups) == {"stop", "stop:breakeven"}
    assert groups["stop"].trades == 2
    assert groups["stop:breakeven"].trades == 1


def test_the_exit_rule_breakdown_measures_slices_not_positions() -> None:
    """Kol/sembolün aksine çıkış kuralı DİLİMİN özelliğidir: pozisyon iki gruba düşer.

    Nakit toplamı korunur, işlem SAYISI korunmaz — kırılımın cevapladığı soru "hangi kural
    kaç kez tetikledi", "model kaç pozisyon açtı" değildir.
    """
    rows = [
        _fill(closed_at="2026-01-01T04:00:00+00:00", pnl=75.0, risk=100.0,
              exit_reason="partial", notes=""),
        _fill(closed_at="2026-01-01T08:00:00+00:00", pnl=25.0, risk=100.0,
              exit_reason="stop", notes="x | exit_rule=giveback"),
    ]

    merged = direction_stats(rows, direction="total")
    groups = breakdown(rows, key=exit_rule_of)

    assert merged.trades == 1                                   # tek POZİSYON
    assert sum(stats.trades for stats in groups.values()) == 2  # iki DİLİM
    assert set(groups) == {"partial", "stop:giveback"}


# --------------------------------------------------------------------------- #
# Dolum -> pozisyon birleştirme (bir POZİSYON = bir ölçüm satırı)
# --------------------------------------------------------------------------- #
def _fill(
    *, closed_at: str, pnl: float, risk: float, exit_reason: str, **overrides: Any
) -> dict[str, Any]:
    """AYNI pozisyonun bir dilimi: kimlik alanları (strategy/symbol/direction/opened_at) ortak."""
    return _trade(
        pnl=pnl,
        risk=risk,
        opened_at="2026-01-01T00:00:00+00:00",
        closed_at=closed_at,
        entry_price=100.0,
        exit_reason=exit_reason,
        **overrides,
    )


def test_partial_exit_and_its_remainder_are_one_trade() -> None:
    """Kısmi çıkış tamamlanmış bir işlem değil, hâlâ açık bir pozisyonun dilimidir.

    İki satırı ayrı işlem saymak aynı pozisyonu iki kez ölçüme sokar ve
    `acceptance.min_trades` örneklem kapısını iki kat hızlı geçirirdi.
    """
    rows = [
        _fill(closed_at="2026-01-01T04:00:00+00:00", pnl=75.0, risk=50.0, exit_reason="partial"),
        _fill(closed_at="2026-01-01T08:00:00+00:00", pnl=150.0, risk=50.0, exit_reason="tp"),
    ]

    stats = direction_stats(rows, direction="total")

    assert stats.trades == 1
    # R pozisyonun tamamından: 225 / 100. Kalan dilimin kendi R'si (3.0) değil.
    assert stats.avg_r == pytest.approx(2.25)


def test_position_r_keeps_the_profit_locked_in_by_the_partial_exit() -> None:
    """Kısmi satırı ATMAK, ölçümü ters yönde bozardı.

    Kilitlenen kâr ölçümden düşer ve kalan dilimin R'si tüm pozisyonun R'si sanılırdı:
    yönetimli model (15), yönetimsiz ikizine (12) karşı haksızca kötü görünürdü.
    """
    rows = [
        _fill(closed_at="2026-01-01T04:00:00+00:00", pnl=75.0, risk=50.0, exit_reason="partial"),
        # Kalan dilim başabaşa çekilmiş stop'ta kapanır: kendi başına 0.0R.
        _fill(closed_at="2026-01-01T08:00:00+00:00", pnl=0.0, risk=50.0, exit_reason="stop"),
    ]

    stats = direction_stats(rows, direction="total")

    assert stats.trades == 1
    assert stats.avg_r == pytest.approx(0.75)  # kalan dilimin 0.0'ı değil
    assert stats.win_rate == pytest.approx(1.0)


def test_a_winning_partial_does_not_make_a_losing_position_a_win() -> None:
    """Kısmi çıkış tanımı gereği kârda gerçekleşir; ayrı sayılsaydı kazanma oranı şişerdi."""
    rows = [
        _fill(closed_at="2026-01-01T04:00:00+00:00", pnl=75.0, risk=50.0, exit_reason="partial"),
        _fill(closed_at="2026-01-01T08:00:00+00:00", pnl=-150.0, risk=50.0, exit_reason="stop"),
    ]

    stats = direction_stats(rows, direction="total")

    assert stats.trades == 1
    assert stats.win_rate == pytest.approx(0.0)
    assert stats.avg_r == pytest.approx(-0.75)


def test_fractional_take_profits_collapse_too() -> None:
    """Ayrım `exit_reason == "partial"` değil, POZİSYON kimliğidir.

    `avwap` iki TP seviyesi, `downtrend_rally` yarım TP kullanır: ikisi de aynı pozisyon
    için birden çok "tp" satırı yazar ve hiçbiri "partial" kodunu taşımaz. Koda
    `exit_reason` filtresi koymak bu modelleri çift saymaya devam ederdi.
    """
    rows = [
        _fill(closed_at="2026-01-01T04:00:00+00:00", pnl=50.0, risk=50.0, exit_reason="tp"),
        _fill(closed_at="2026-01-01T04:00:00+00:00", pnl=100.0, risk=50.0, exit_reason="tp"),
    ]

    assert direction_stats(rows, direction="total").trades == 1


def test_merge_keeps_the_closing_fill_as_the_positions_exit() -> None:
    """Ara dilimin çıkış sebebi pozisyonun sebebi değildir."""
    rows = [
        _fill(closed_at="2026-01-01T04:00:00+00:00", pnl=75.0, risk=50.0, exit_reason="partial"),
        _fill(closed_at="2026-01-01T08:00:00+00:00", pnl=-90.0, risk=50.0, exit_reason="liquidation"),
    ]

    merged = merge_fills(rows)

    assert len(merged) == 1
    assert merged[0]["exit_reason"] == "liquidation"
    assert merged[0]["fills"] == 2
    assert direction_stats(rows, direction="total").liquidations == 1


def test_merge_does_not_join_two_models_holding_the_same_symbol() -> None:
    """Havuz birden çok modelin satırlarını tek listede birleştirir (kural 4 ölçümde de geçerli)."""
    mine = _fill(closed_at="2026-01-01T04:00:00+00:00", pnl=75.0, risk=50.0, exit_reason="stop")
    theirs = {**mine, "strategy": "other"}

    assert len(merge_fills([mine, theirs])) == 2


def test_rows_without_an_open_timestamp_stay_separate() -> None:
    """Bilinmeyen kimliği ortak kabul edip hepsini tek pozisyonda toplamak veri kaybı olurdu."""
    rows = [_trade(pnl=10.0), _trade(pnl=-20.0), _trade(pnl=30.0)]
    assert all(row["opened_at"] == "" for row in rows)

    assert direction_stats(rows, direction="total").trades == 3


def test_cash_columns_are_summed_not_dropped_by_the_merge() -> None:
    """"Σpnl = bakiye değişimi" değişmezi: nakit kolonları birleştirmede TOPLANIR."""
    rows = [
        _fill(closed_at="2026-01-01T04:00:00+00:00", pnl=75.0, risk=50.0, exit_reason="partial",
              fee=0.5, slippage_cost=0.1, funding=-0.2),
        _fill(closed_at="2026-01-01T08:00:00+00:00", pnl=-150.0, risk=50.0, exit_reason="stop",
              fee=0.7, slippage_cost=0.3, funding=-0.4),
    ]

    stats = direction_stats(rows, direction="total")

    assert stats.pnl == pytest.approx(sum(row["pnl"] for row in rows))
    assert stats.fees == pytest.approx(sum(row["fee"] for row in rows))
    assert stats.slippage_cost == pytest.approx(sum(row["slippage_cost"] for row in rows))
    assert stats.funding == pytest.approx(sum(row["funding"] for row in rows))


def test_cost_per_r_covers_every_fill_of_the_position() -> None:
    """CLAUDE.md > Rapor Kolonları: pay TÜM dolumların (giriş, kısmi TP'ler, çıkış) maliyeti."""
    rows = [
        _fill(closed_at="2026-01-01T04:00:00+00:00", pnl=75.0, risk=50.0, exit_reason="partial",
              fee=1.0, slippage_cost=0.5),
        _fill(closed_at="2026-01-01T08:00:00+00:00", pnl=-150.0, risk=50.0, exit_reason="stop",
              fee=2.0, slippage_cost=0.5),
    ]

    # (1.0 + 0.5 + 2.0 + 0.5) / (50 + 50)
    assert direction_stats(rows, direction="total").cost_per_r == pytest.approx(0.04)


def test_the_pnl_total_still_equals_the_balance_change_with_a_partial_exit() -> None:
    """Uçtan uca: gerçek bir kısmi çıkış senaryosunda değişmez korunuyor mu.

    Birleştirme sayımı değiştirir, nakdi DEĞİŞTİRMEZ: metriklerin `pnl` toplamı hâlâ
    portföyün bakiye değişimine eşit olmalıdır, yoksa defter denetlenemez hâle gelir.
    """
    from core.portfolio import Bar, Portfolio
    from strategies.base import TakeProfit

    ts = pd.Timestamp("2026-01-01 00:00:00", tz="UTC")
    symbol = "BTC-USDT-SWAP"
    portfolio = Portfolio(load_config())
    start = portfolio.cash("m")
    portfolio.open_position(
        "m", symbol=symbol, direction="long", stop_price=95.0,
        reference_price=100.0, ts=ts, marks={symbol: 100.0},
        take_profits=(TakeProfit(price=105.0, fraction=0.5),),
    )
    fills = portfolio.process_bar(
        "m", ts=ts, bars={symbol: Bar(open=100.0, high=106.0, low=99.0, close=105.5)}
    )
    fills.append(
        portfolio.close_position(
            "m", symbol=symbol, direction="long", reference_price=104.0,
            ts=ts + pd.Timedelta(hours=4),
        )
    )
    assert len(fills) == 2  # tek pozisyon, iki dolum

    stats = direction_stats([fill.as_row() for fill in fills], direction="total")

    assert stats.trades == 1
    assert stats.pnl == pytest.approx(portfolio.cash("m") - start)
    assert portfolio.positions("m") == ()


# --------------------------------------------------------------------------- #
# Kabul çıtası: kontrolün KENDİ örneklemi ve farkın güven aralığı
# --------------------------------------------------------------------------- #
def _loser(model: str, *, n: int, pnl: float = -10.0, final: float = 9_800.0) -> Any:
    return _competitor(
        model,
        avg_r_trades=[
            _trade(pnl=pnl, risk=100.0, entry_price=100.0, stop_price=97.0,
                   closed_at=f"2026-02-{index % 28 + 1:02d}T00:00:00+00:00")
            for index in range(n)
        ],
        final=final,
    )


def test_edge_is_not_granted_against_an_unmeasured_control() -> None:
    """Kontrol kendi örneklem kapısını geçmediyse edge DEĞERLENDİRİLEMEZ.

    Canlı base katmanında gerçekten oluşan durum: random_ctrl n=4 iken bir model ona
    karşı 0.15R marjla "ölçülüyordu". Marj, kontrolün ORTALAMASINA göre tanımlı ve o
    ortalama da bir örneklemden geliyor; dört işlemlik bir ortalamaya karşı marj ölçmek
    gürültüyü gürültüyle kıyaslamaktır.
    """
    flags = _flags([_winner("good"), _loser("ctrl", n=4)])

    assert flags["good"].sample          # modelin kendi örneklemi yeterli
    assert not flags["good"].edge        # ama kontrolünki değil
    assert not flags["good"].passed
    assert flags["good"].control_trades == 4
    assert flags["good"].control_min_trades == 30


def test_a_measured_control_still_allows_edge() -> None:
    """Kapı kontrolü cezalandırmaz, yalnızca ölçülmüş olmasını ister."""
    flags = _flags([_winner("good"), _loser("ctrl", n=30)])
    assert flags["good"].edge and flags["good"].passed


def test_missing_control_is_not_the_same_as_an_unmeasured_one() -> None:
    """Kümede HİÇ kontrol yoksa koşul düşer (CLAUDE.md); az örneklemliyse düşmez.

    İkisini tek sayıya indirmek, kontrolü listeden çıkarmayı kapıyı geçmenin bir yolu
    hâline getirirdi — oysa amaç tam tersi.
    """
    assert _flags([_winner("good")])["good"].edge            # kontrol yok -> koşul düşer
    assert not _flags([_winner("good"), _loser("ctrl", n=1)])["good"].edge


def test_control_sample_gate_defaults_to_the_model_gate() -> None:
    """`control_min_trades` verilmezse kontrol de modellerle aynı çıtayı görür (kural 6)."""
    flags = _flags([_winner("good"), _loser("ctrl", n=29)])
    assert flags["good"].control_min_trades == 30
    assert not flags["good"].edge


def test_bootstrap_ci_blocks_an_edge_that_is_large_but_uncertain() -> None:
    """Marj ETKİ BÜYÜKLÜĞÜ, aralık KESİNLİK sorar: biri diğerinin yerine geçmez.

    Burada fark marjı rahatça geçiyor (model +0.5R, kontrol −0.1R) ama model yalnızca
    birkaç işlemden geliyor ve dağılımı çok saçılmış: farkın güven aralığı sıfırı içerir.
    """
    scattered = [3.0, -2.0, 2.5, -2.2, 3.1, -2.4]
    control = [-0.1] * 40
    low, high = bootstrap_diff_ci(
        scattered, control, alpha=0.05, iterations=1000, seed=11
    )
    assert low < 0.0 < high                       # aralık sıfırı içeriyor
    assert sum(scattered) / len(scattered) - sum(control) / len(control) > 0.15  # marj geçildi


def test_bootstrap_ci_is_deterministic_for_the_same_ledger() -> None:
    """Aynı defter HER ZAMAN aynı aralığı vermeli: rozet bir ölçüdür, bir çekiliş değil."""
    sample, control = [1.0, -1.0, 2.0, -1.0] * 10, [-1.0, 0.2] * 20
    first = bootstrap_diff_ci(sample, control, alpha=0.05, iterations=400, seed=5)
    second = bootstrap_diff_ci(sample, control, alpha=0.05, iterations=400, seed=5)
    assert first == second


def test_bootstrap_ci_is_nan_when_a_side_is_empty() -> None:
    """Hesaplanamayan aralık `nan`dır; 0.0 olsaydı "ölçüldü ve sıfır çıktı" derdi."""
    low, high = bootstrap_diff_ci([1.0], [], alpha=0.05, iterations=100, seed=1)
    assert math.isnan(low) and math.isnan(high)


def test_r_series_matches_the_table_average() -> None:
    """Bootstrap ile tablo AYNI sayıların üstünde durmalı: ikinci bir hesap yolu yok."""
    trades = [
        _trade(pnl=value * 100.0, risk=100.0, entry_price=100.0, stop_price=97.0)
        for value in (0.5, -1.0, 2.0)
    ]
    values = r_series(trades)
    assert values == pytest.approx([0.5, -1.0, 2.0])
    assert sum(values) / len(values) == pytest.approx(direction_stats(trades).avg_r)


def test_report_does_not_rank_models_below_the_sample_gate() -> None:
    """Kapıyı geçmeyen satır SIRALANMAZ ama GİZLENMEZ de (karar 33: ölçülebilirlik)."""
    report = format_report([_winner("measured", n=40), _winner("tiny", n=3)], min_trades=30)

    assert "YETERSİZ ÖRNEKLEM" in report
    assert "tiny" in report
    # Ayrı bölüm, sıralamanın ALTINDA: kapıyı geçen satır önce gelir.
    assert report.index("measured") < report.index("YETERSİZ ÖRNEKLEM") < report.index("tiny")


def test_report_without_a_gate_keeps_the_single_table() -> None:
    """`min_trades` verilmezse davranış değişmez: kapı bir sunum kararı değil, bir ayardır."""
    assert "YETERSİZ ÖRNEKLEM" not in format_report([_winner("tiny", n=3)])


# --------------------------------------------------------------------------- #
# Okuma yardımları: beklenti ayrışması, ortalama R aralığı, friksiyon hızı
# --------------------------------------------------------------------------- #
def test_expectancy_is_an_identity_not_a_second_metric() -> None:
    """`WR × ort.kazanç + (1−WR) × ort.kayıp` R biriminde ortalama R'nin TA KENDİSİDİR.

    Bu yüzden tabloya yeni bir sayı olarak değil, mevcut sayının AYRIŞMASI olarak girer:
    değeri "ortalama R negatif" bilgisinde değil, bunun kazanma oranından mı yoksa ödeme
    oranından mı geldiğinde. İkisini iki ayrı metrik gibi raporlamak, aynı sayıyı iki kez
    ölçüyormuş izlenimi verirdi.
    """
    trades = [
        _trade(pnl=200.0), _trade(pnl=-100.0), _trade(pnl=-50.0), _trade(pnl=300.0),
        _trade(direction="short", pnl=-100.0), _trade(direction="short", pnl=25.0),
    ]
    for stats in (
        direction_stats(trades, direction="long"),
        direction_stats(trades, direction="short"),
        direction_stats(trades),
    ):
        expectancy = (
            stats.win_rate * stats.avg_win_r + (1.0 - stats.win_rate) * stats.avg_loss_r
        )
        assert expectancy == pytest.approx(stats.avg_r)


def test_cost_pct_divides_cost_by_notional_so_the_replica_stays_comparable() -> None:
    """Friksiyon kolonları `cost_per_r` muafiyetinin DIŞINDADIR.

    `cost_per_r` kopyada ve çıpada `nan`dır çünkü paydası (1R) onlarda başka bir birimden
    gelir. `cost_pct`in paydası notional'dır — tek ve ortak bir birim — bu yüzden her
    satırda hesaplanır. Kopyanın friksiyonunu görebilmenin tek yolu budur ve tam da
    ölçülmek istenen şeydir: `vwap_clone` R'den önce cirodan ölüyor mu?
    """
    trades = [_trade(pnl=-100.0, notional=1000.0, fee=2.0, slippage_cost=1.0)]
    for flags in ({}, {"is_replica": True}, {"is_benchmark": True}):
        stats = direction_stats(trades, **flags)  # type: ignore[arg-type]
        assert stats.notional == pytest.approx(1000.0)
        assert stats.cost_pct == pytest.approx(0.3)

    assert math.isnan(direction_stats(trades, is_replica=True).cost_per_r)


def test_friction_rates_are_measured_against_the_starting_capital(tmp_path: Path) -> None:
    """Payda BAŞLANGIÇ sermayesi ve TAKVİM günüdür, güncel bakiye ve bar sayısı değil.

    Güncel bakiyeye bölmek ciroyu modelin kendi performansına bağlar: kaybeden modelin
    cirosu yapay yükselir ve iki model aynı birimden konuşmayı bırakır.
    """
    ledger = Ledger(tmp_path)
    ledger.initialize_model("alpha", initial_capital=10_000.0)
    ledger.append_trades("alpha", [
        _trade(pnl=-50.0, notional=5_000.0, fee=5.0, slippage_cost=5.0),
        _trade(pnl=-50.0, notional=5_000.0, fee=5.0, slippage_cost=5.0),
    ])
    # İki satır, bir gün arayla: takvim aralığı 1 gün.
    ledger.append_equity("alpha", _equity(10_000.0, 9_900.0))

    (metrics,) = compare(["alpha"], ledger=ledger, config=load_config())

    assert metrics.account.days == pytest.approx(1.0)
    assert metrics.friction.trades_per_day == pytest.approx(2.0)
    # Σnotional 10.000 / sermaye 10.000 / 1 gün
    assert metrics.friction.turnover_per_day == pytest.approx(1.0)
    # Σ(komisyon+kayma) 20 / sermaye 10.000 = %0.2, günde bir kez
    assert metrics.friction.cost_drag_pct_per_day == pytest.approx(0.2)


def test_span_days_comes_from_timestamps_so_compaction_cannot_shrink_it(
    tmp_path: Path,
) -> None:
    """Saklama penceresi eski satırları günlük özete indirir; geçen zaman değişmez.

    Günü bar SAYISINDAN türetseydik sıkıştırılmış bir defterde ciro yapay olarak
    yükselirdi — aynı işlem sayısı daha az "gün"e bölünürdü.
    """
    ledger = Ledger(tmp_path)
    ledger.initialize_model("alpha", initial_capital=10_000.0)
    ledger.append_equity("alpha", [
        {"ts": "2026-01-01T00:00:00+00:00", "cash": 10_000.0, "margin_used": 0.0,
         "unrealized_pnl": 0.0, "equity": 10_000.0, "open_positions": 0},
        {"ts": "2026-01-31T00:00:00+00:00", "cash": 10_000.0, "margin_used": 0.0,
         "unrealized_pnl": 0.0, "equity": 10_000.0, "open_positions": 0},
    ])

    (metrics,) = compare(["alpha"], ledger=ledger, config=load_config())

    assert metrics.account.bars == 2
    assert metrics.account.days == pytest.approx(30.0)


def test_average_r_interval_is_deterministic_and_brackets_the_average(
    tmp_path: Path,
) -> None:
    """Aynı defter HER ZAMAN aynı aralığı verir ve aralık ortalamayı içerir.

    Titreyen bir aralık, okuma yardımını bir çekilişe çevirirdi — `random_seed`in sabit
    olmasının gerekçesiyle aynı (bkz. `bootstrap_diff_ci`).
    """
    ledger = Ledger(tmp_path)
    ledger.initialize_model("alpha", initial_capital=10_000.0)
    ledger.append_trades("alpha", [
        _trade(pnl=150.0 if index % 3 else -100.0, risk=100.0)
        for index in range(40)
    ])
    ledger.append_equity("alpha", _equity(10_000.0, 10_200.0))

    (first,) = compare(["alpha"], ledger=ledger, config=load_config())
    (second,) = compare(["alpha"], ledger=ledger, config=load_config())

    assert first.total.avg_r_ci_low == second.total.avg_r_ci_low
    assert first.total.avg_r_ci_high == second.total.avg_r_ci_high
    assert first.total.avg_r_ci_low <= first.total.avg_r <= first.total.avg_r_ci_high


def test_average_r_interval_is_undefined_when_bootstrap_is_not_requested() -> None:
    """Varsayılan `nan`: aralık bir kapı değil, istendiğinde hesaplanan bir okuma yardımı."""
    stats = direction_stats([_trade(pnl=100.0)])
    assert math.isnan(stats.avg_r_ci_low) and math.isnan(stats.avg_r_ci_high)


# --------------------------------------------------------------------------- #
# Piyasa kontrolü (ana sorunun karıştırıcısı)
# --------------------------------------------------------------------------- #
def _reference(*prices: float) -> pd.Series:
    return pd.Series(
        list(prices),
        index=pd.to_datetime(
            [f"2026-01-0{i + 1}T00:00:00+00:00" for i in range(len(prices))]
        ),
    )


def _window_trade(*, direction: str, pnl: float, opened: str, closed: str) -> dict[str, Any]:
    return _trade(
        direction=direction, pnl=pnl, risk=100.0,
        entry_price=100.0, stop_price=99.0,  # stop mesafesi %1
        opened_at=f"2026-01-0{opened}T00:00:00+00:00",
        closed_at=f"2026-01-0{closed}T00:00:00+00:00",
    )


def test_market_tailwind_is_signed_by_the_position_direction() -> None:
    """Yükselen bir pencerede long'un rüzgârı ARKADAN, short'un KARŞIDAN eser.

    İşaret sözleşmesi projenin ana sorusunun kendisidir: "short'lar daha başarılı"
    cümlesi, düşen bir pencerede ölçüldüğünde tanım gereği doğru çıkar. Ölçü, o
    pencereyi görünür kılmak için vardır.
    """
    reference = _reference(100.0, 102.0)  # +%2
    rows = [
        _window_trade(direction="long", pnl=50.0, opened="1", closed="2"),
        _window_trade(direction="short", pnl=50.0, opened="1", closed="2"),
    ]

    long_stats = direction_stats(rows, direction="long", reference=reference)
    short_stats = direction_stats(rows, direction="short", reference=reference)

    assert long_stats.market_tailwind_pct == pytest.approx(2.0)
    assert short_stats.market_tailwind_pct == pytest.approx(-2.0)
    # stop mesafesi %1 olduğu için market_R, tailwind'in tam katı
    assert long_stats.market_r == pytest.approx(2.0)
    assert short_stats.market_r == pytest.approx(-2.0)


def test_market_control_never_touches_the_primary_metric() -> None:
    """Ortalama R bir ÖLÇÜM, piyasa katkısı bir KONTROLDÜR; ikincisi birincisini bozmaz.

    Düzeltilmiş bir "ort. R" üretmek beta=1 varsayımını birincil metriğin içine gömerdi
    (docs/backtest.md > 7.4: metrik değiştirmek yasaktır). Varsayım açıkta durur.
    """
    rows = [_window_trade(direction="long", pnl=50.0, opened="1", closed="2")]
    without = direction_stats(rows, direction="long")
    with_reference = direction_stats(rows, direction="long", reference=_reference(100.0, 110.0))

    assert without.avg_r == with_reference.avg_r
    assert math.isnan(without.market_r) and without.market_measured == 0
    assert with_reference.market_measured == 1


def test_positions_the_anchor_cannot_price_are_counted_not_guessed() -> None:
    """Çıpa serisinden ÖNCE açılmış pozisyon ölçülmez ve bu sayıyla söylenir.

    Uydurma bir başlangıç fiyatı (serinin ilk değeri) o pozisyonun piyasa katkısını
    sıfır gösterirdi — yani "ölçemedik" ile "piyasa hiç katkı vermedi" aynı hücreye
    yazılırdı. `market_measured` farkı denetlenebilir kılar.
    """
    reference = pd.Series(
        [100.0, 101.0],
        index=pd.to_datetime(["2026-01-03T00:00:00+00:00", "2026-01-04T00:00:00+00:00"]),
    )
    rows = [
        _window_trade(direction="long", pnl=10.0, opened="1", closed="2"),  # seriden ÖNCE
        _window_trade(direction="long", pnl=10.0, opened="3", closed="4"),  # seri içinde
    ]

    stats = direction_stats(rows, direction="long", reference=reference)

    assert stats.trades == 2
    assert stats.market_measured == 1
    assert stats.market_tailwind_pct == pytest.approx(1.0)


def test_market_r_averages_per_position_ratios_like_cost_per_r() -> None:
    """Önce her pozisyonun oranı, SONRA ortalama — `cost_per_r` ile aynı sözleşme.

    Önce ortalamaları alıp bölmek dar stop'lu pozisyonların piyasa katkısını gizlerdi
    ve iki kolon birbirinin dilinden konuşmayı bırakırdı.
    """
    reference = _reference(100.0, 101.0)  # +%1
    rows = [
        # stop %1 -> market_R = 1.0
        _window_trade(direction="long", pnl=10.0, opened="1", closed="2"),
        # stop %4 -> market_R = 0.25. Farklı SEMBOL: aynı sembol + aynı açılış damgası
        # tek bir pozisyona indirgenirdi (merge_fills), yani iki oran hiç oluşmazdı.
        _trade(direction="long", pnl=10.0, risk=100.0, symbol="ETH-USDT-SWAP",
               entry_price=100.0, stop_price=96.0,
               opened_at="2026-01-01T00:00:00+00:00", closed_at="2026-01-02T00:00:00+00:00"),
    ]

    stats = direction_stats(rows, direction="long", reference=reference)

    assert stats.market_r == pytest.approx((1.0 + 0.25) / 2.0)


def test_reference_timestamps_are_read_as_utc(tmp_path: Path) -> None:
    """Zaman dilimsiz bir çıpa serisi UTC sayılır — defter UTC yazar (kural 12).

    Yerel saate çevirmek, aynı defterin makineden makineye farklı piyasa kontrolü
    üretmesi demekti; `random_seed`in sabit olmasıyla aynı statü.
    """
    naive = pd.Series([100.0, 102.0], index=pd.to_datetime(
        ["2026-01-01T00:00:00", "2026-01-02T00:00:00"]
    ))
    rows = [_window_trade(direction="long", pnl=10.0, opened="1", closed="2")]

    stats = direction_stats(rows, direction="long", reference=naive)

    assert stats.market_measured == 1
    assert stats.market_tailwind_pct == pytest.approx(2.0)


def test_breakdown_groups_carry_their_own_interval() -> None:
    """Grup ortalaması da örneklemiyle ve aralığıyla birlikte raporlanır.

    Karar 27 (saat hipotezi) ve karar 28 (kayıp serisi cooldown'u) tam olarak bir
    KIRILIM grubunun ortalamasına bakıp kural yazma denemeleriydi; ikisi de daha uzun
    örneklemde çürüdü. Grup ortalamasını aralıksız göstermek, o hatayı ölçüm katmanının
    içine yerleştirmek olurdu.
    """
    trades = [
        _trade(pnl=100.0 if index % 2 else -100.0, risk=100.0,
               signal_reason=format_tags("kurulum", arm="a" if index < 6 else "b"))
        for index in range(12)
    ]

    groups = breakdown(trades, key=arm_of, ci_alpha=0.05, bootstrap_samples=200, seed=7)
    again = breakdown(trades, key=arm_of, ci_alpha=0.05, bootstrap_samples=200, seed=7)

    assert set(groups) == {"a", "b"}
    for name, stats in groups.items():
        assert not math.isnan(stats.avg_r_ci_low)
        assert stats.avg_r_ci_low <= stats.avg_r <= stats.avg_r_ci_high
        # Aynı defter, aynı aralık: tohum grup adına bağlıdır ve titremez.
        assert stats.avg_r_ci_low == again[name].avg_r_ci_low


def test_breakdown_reports_no_interval_when_bootstrap_is_off() -> None:
    trades = [_trade(pnl=100.0, signal_reason=format_tags("kurulum", arm="a"))]
    (stats,) = breakdown(trades, key=arm_of).values()
    assert math.isnan(stats.avg_r_ci_low)


def test_single_observation_has_no_interval_only_a_value() -> None:
    """Tek gözlemde bootstrap dejenere bir aralık üretir; `_stdev` ile aynı sınır.

    `[+0.08, +0.08]` okuyucuya kıl payı bir kesinlik vaat eder, oysa yeniden
    örneklenecek bir dağılım yoktur. Eşik serbest bir parametre değil, bootstrap'ın
    tanım sınırıdır.
    """
    assert bootstrap_mean_ci([0.08], alpha=0.05, iterations=500, seed=1) == (
        pytest.approx(float("nan"), nan_ok=True),
        pytest.approx(float("nan"), nan_ok=True),
    )
    low, high = bootstrap_mean_ci([0.08, -1.0], alpha=0.05, iterations=500, seed=1)
    assert not math.isnan(low) and low < high


# --------------------------------------------------------------------------- #
# Yüzde ödeme profili, PnL drawdown'ı, tutuş süresi ve sembol al-tut
#
# Dördü de dışarıdan gelen bir referansla (TradingView, başka bir backtest aracı) kıyas
# kurmak için var: R bizim boyutlandırma kuralımıza bağlıdır ve o kural dışarıda
# başkadır — ortak birim yüzde ve gündür.
# --------------------------------------------------------------------------- #
def test_percent_payoff_is_measured_on_entry_notional() -> None:
    """Ort. kazanç% / ort. kayıp% ve ödeme oranı, R'den BAĞIMSIZ hesaplanır."""
    trades = [
        _trade(pnl=6.0, notional=100.0),    # +%6
        _trade(pnl=4.0, notional=100.0),    # +%4
        _trade(pnl=-2.0, notional=100.0),   # −%2
        _trade(pnl=-4.0, notional=200.0),   # −%2
    ]
    stats = direction_stats(trades)

    assert stats.avg_win_pct == pytest.approx(5.0)
    assert stats.avg_loss_pct == pytest.approx(-2.0)
    assert stats.payoff == pytest.approx(2.5)


def test_payoff_is_nan_without_a_losing_trade_instead_of_dividing_by_zero() -> None:
    """Kayıp yoksa oran TANIMSIZDIR; 0.0 yazmak "ödeme oranı sıfır" demek olurdu."""
    stats = direction_stats([_trade(pnl=5.0)])
    assert math.isnan(stats.payoff)
    assert math.isnan(stats.avg_loss_pct)


def test_percent_profile_is_measured_even_when_r_cannot_be() -> None:
    """Çıpanın (kural 15) `risk_amount`ı yoktur: R yok, ama yüzde getirisi VAR.

    İkisini tek döngüde türetmek, çıpanın ödeme profilini sessizce boşaltırdı.
    """
    stats = direction_stats([_trade(pnl=10.0, risk="", notional=100.0)])
    assert math.isnan(stats.avg_r)
    assert stats.avg_win_pct == pytest.approx(10.0)


def test_pnl_drawdown_follows_the_order_of_closes_not_the_worst_trade() -> None:
    """Drawdown bir SIRA ölçüsüdür: tek bir kaybın büyüklüğü değil, tepe-dip mesafesi."""
    trades = [
        _trade(pnl=100.0, closed_at="2026-01-01T00:00:00+00:00", opened_at="2026-01-01T00:00:00+00:00"),
        _trade(pnl=-60.0, closed_at="2026-01-02T00:00:00+00:00", opened_at="2026-01-02T00:00:00+00:00"),
        _trade(pnl=-40.0, closed_at="2026-01-03T00:00:00+00:00", opened_at="2026-01-03T00:00:00+00:00"),
        _trade(pnl=30.0, closed_at="2026-01-04T00:00:00+00:00", opened_at="2026-01-04T00:00:00+00:00"),
    ]
    stats = direction_stats(trades)
    assert stats.max_drawdown_pnl == pytest.approx(-100.0)
    assert pnl_drawdown_pct(stats, initial_capital=10_000.0) == pytest.approx(-1.0)


def test_pnl_drawdown_pct_uses_initial_capital_not_the_current_balance() -> None:
    """Payda SABİTTİR: güncel bakiyeye bölmek, kaybeden modelin drawdown'ını şişirirdi."""
    stats = direction_stats([_trade(pnl=-500.0)])
    assert pnl_drawdown_pct(stats, initial_capital=10_000.0) == pytest.approx(-5.0)
    assert math.isnan(pnl_drawdown_pct(stats, initial_capital=0.0))


def test_holding_stats_measure_the_distribution_in_bars_and_days() -> None:
    """Zaman stop'u olmayan modelde embargo VARSAYILAMAZ, ölçülür (docs/backtest.md > 6.1)."""
    spans_hours = [4, 8, 40]  # 4H barda 1, 2, 10 bar
    trades = [
        _trade(
            pnl=1.0,
            opened_at="2026-01-01T00:00:00+00:00",
            closed_at=(pd.Timestamp("2026-01-01T00:00:00+00:00") + pd.Timedelta(hours=hours)).isoformat(),
            symbol=f"S{index}-USDT-SWAP",
        )
        for index, hours in enumerate(spans_hours)
    ]
    stats = holding_stats(trades, bar_duration=pd.Timedelta(hours=4))

    assert stats.positions == 3
    assert stats.median_bars == pytest.approx(2.0)
    assert stats.max_bars == pytest.approx(10.0)
    assert stats.max_days == pytest.approx(40.0 / 24.0)
    assert stats.p90_bars == pytest.approx(2.0 + 0.8 * 8.0)


def test_holding_percentiles_are_monotonic_whatever_the_input_order() -> None:
    """medyan ≤ p90 ≤ azami — SIRA bir varsayım değil, sağlanması gereken bir şey.

    İlk hâlinde değildi: modüle ikinci bir `_percentile` eklenmişti ve sonraki tanım
    (bootstrap'ınki, SIRALI girdi bekleyen) kazanıyordu. Sonuç, gerçek bir koşuda
    medyanı 9.00 iken p90'ı 8.40 raporlanan bir dağılımdı — yani hiç kimsenin
    inanmaması gereken bir sayı, sessizce basıldı.
    """
    stamp = pd.Timestamp("2026-01-01T00:00:00+00:00")
    spans = [40, 4, 184, 8, 12, 60, 16, 36, 20]  # bilinçli olarak SIRASIZ
    trades = [
        _trade(
            pnl=1.0,
            symbol=f"S{index}-USDT-SWAP",
            opened_at=stamp.isoformat(),
            closed_at=(stamp + pd.Timedelta(hours=hours)).isoformat(),
        )
        for index, hours in enumerate(spans)
    ]
    stats = holding_stats(trades, bar_duration=pd.Timedelta(hours=4))

    assert stats.median_bars <= stats.p90_bars <= stats.max_bars
    assert stats.median_days <= stats.p90_days <= stats.max_days
    assert stats.max_bars == pytest.approx(46.0)


def test_holding_stats_count_positions_not_fills() -> None:
    """Kısmi çıkışın her dilimini saymak, dağılımı kısa tarafa çekerdi."""
    common = dict(
        strategy="m", symbol="BTC-USDT-SWAP", direction="long",
        opened_at="2026-01-01T00:00:00+00:00",
    )
    trades = [
        _trade(pnl=5.0, exit_reason="partial", closed_at="2026-01-01T04:00:00+00:00", **common),
        _trade(pnl=5.0, exit_reason="tp", closed_at="2026-01-02T00:00:00+00:00", **common),
    ]
    stats = holding_stats(trades, bar_duration=pd.Timedelta(hours=4))

    assert stats.positions == 1
    assert stats.max_bars == pytest.approx(6.0)  # pozisyonu KAPATAN dilime kadar


def test_holding_stats_reject_a_non_positive_bar_duration() -> None:
    with pytest.raises(ValueError):
        holding_stats([], bar_duration=pd.Timedelta(0))


def test_buy_hold_return_reads_the_injected_series_only() -> None:
    index = pd.date_range("2026-01-01", periods=5, freq="4h", tz="UTC")
    series = pd.Series([100.0, 110.0, 120.0, 130.0, 140.0], index=index)

    assert buy_hold_return(series, start=index[0], end=index[-1]) == pytest.approx(40.0)
    # Pencerenin ucunda barı olmayan sembol uydurma fiyat almaz: çapa "o ana kadarki son
    # kapanış"tır, ondan öncesi yoksa `nan`.
    assert math.isnan(
        buy_hold_return(series, start=pd.Timestamp("2025-01-01", tz="UTC"), end=index[-1])
    )
