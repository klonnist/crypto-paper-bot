"""scripts/diagnose_ema_exits.py: yol istatistiğinin saf çekirdeği.

Teşhis aracı ölçümün parçası değildir ama ÜRETTİĞİ sayılar varyant tanımına girecek —
yani yanlış bir MFE tanımı, ön-kayda yanlış bir eşik yazdırırdı. Bu yüzden yolun
matematiği (payda, kapanış barının hariç tutulması, çıkış sonrası devam) elle kurulmuş
mumlarla sabitlenir; koşunun kendisi (ağ, motor) burada test edilmez.
"""

from __future__ import annotations

import math
from pathlib import Path
import sys

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.diagnose_ema_exits import (
    MAE_BUCKETS,
    MFE_BUCKETS,
    buckets,
    check_determinism,
    distribution,
    position_paths,
    select_primary_family,
)


def _frame(rows: list[tuple[str, float, float]]) -> pd.DataFrame:
    """(ts, high, low) -> OHLCV çerçevesi; yalnızca high/low okunur."""
    index = pd.DatetimeIndex([pd.Timestamp(ts, tz="UTC") for ts, _, _ in rows], name="ts")
    return pd.DataFrame(
        {
            "open": [low for _, _, low in rows],
            "high": [high for _, high, _ in rows],
            "low": [low for _, _, low in rows],
            "close": [high for _, high, _ in rows],
            "volume": [1.0] * len(rows),
        },
        index=index,
    )


CANDLES = {
    "BTC-USDT-SWAP": _frame(
        [
            ("2024-01-01T00:00:00", 102.0, 99.0),   # giriş barı
            ("2024-01-01T04:00:00", 110.0, 98.0),   # ara bar: +1.0R'a kadar gitti
            ("2024-01-01T08:00:00", 120.0, 90.0),   # kapanış barı: hedef de stop da aralıkta
            ("2024-01-01T12:00:00", 130.0, 118.0),  # çıkıştan sonra: devam
            ("2024-01-01T16:00:00", 125.0, 115.0),
        ]
    )
}

# giriş 100, stop 90 -> 1R = 10 birim fiyat.
TRADE = {
    "symbol": "BTC-USDT-SWAP",
    "direction": "long",
    "opened_at": "2024-01-01T00:00:00+00:00",
    "closed_at": "2024-01-01T08:00:00+00:00",
    "entry_price": "100",
    "stop_price": "90",
    "exit_price": "120",
    "pnl": "195",
    "risk_amount": "100",
    "exit_reason": "tp",
}


def test_mfe_prior_excludes_the_closing_bar() -> None:
    """Kapanış barı hariç MFE +1.0R: karar anında görülebilen tek hareket odur.

    Kapanış barı dâhil edilseydi +2.0R çıkardı ve bir breakeven kuralı, pozisyonu
    kapatan barın İÇİNDEKİ bilgiyle tetiklenmiş sayılırdı (kural 13b: stop hareketleri
    bar KAPANDIKTAN sonra uygulanır).
    """
    (path,) = position_paths([TRADE], CANDLES)
    assert path["mfe_r"] == pytest.approx(2.0)
    assert path["mfe_r_prior"] == pytest.approx(1.0)


def test_mae_prior_excludes_the_closing_bar() -> None:
    (path,) = position_paths([TRADE], CANDLES)
    assert path["mae_r"] == pytest.approx(-1.0)
    assert path["mae_r_prior"] == pytest.approx(-0.2)


def test_bars_held_counts_bar_steps_not_rows() -> None:
    """Açılış ve kapanış barı arasındaki ADIM sayısı: 3 bar 2 adımdır."""
    (path,) = position_paths([TRADE], CANDLES)
    assert path["bars_held"] == 2


def test_continuation_is_measured_from_the_exit_price() -> None:
    """Çıkıştan sonraki H bar içinde çıkış fiyatının ne kadar üstüne çıkıldı."""
    (path,) = position_paths([TRADE], CANDLES)
    # 120'den çıkıldı, sonraki barlarda azami 130 -> +1.0R.
    assert path["continuation_r"]["5"] == pytest.approx(1.0)


def test_r_multiple_comes_from_the_ledger_not_from_the_path() -> None:
    """R defterden okunur (pnl/risk_amount), yoldan yeniden hesaplanmaz (kural 7)."""
    (path,) = position_paths([TRADE], CANDLES)
    assert path["r_multiple"] == pytest.approx(1.95)


def test_position_without_candles_is_skipped_loudly(caplog: pytest.LogCaptureFixture) -> None:
    """Mum yoksa satır sessizce ölçülmüş sayılmaz: sayım eksilir ve loglanır."""
    with caplog.at_level("WARNING", logger="diagnose-ema-exits"):
        assert position_paths([{**TRADE, "symbol": "YOK-USDT-SWAP"}], CANDLES) == []
    assert "mum verisi yok" in caplog.text


def test_zero_stop_distance_is_skipped() -> None:
    """Stop girişe eşitse R paydası yoktur; sıfıra bölmek yerine satır düşer."""
    assert position_paths([{**TRADE, "stop_price": "100"}], CANDLES) == []


def test_distribution_reports_nan_for_empty_input() -> None:
    empty = distribution([])
    assert empty["n"] == 0
    assert math.isnan(empty["median"])


def test_distribution_percentiles_use_the_core_definition() -> None:
    stats = distribution([1.0, 2.0, 3.0, 4.0, 5.0])
    assert stats["median"] == pytest.approx(3.0)
    assert stats["p75"] == pytest.approx(4.0)
    assert stats["mean"] == pytest.approx(3.0)


def test_buckets_cover_the_whole_line_and_sum_to_n() -> None:
    values = [-0.3, 0.1, 0.7, 1.2, 1.7, 2.4]
    rows = buckets(values, MFE_BUCKETS)
    assert sum(row["count"] for row in rows) == len(values)
    assert rows[0]["low"] == -math.inf and rows[-1]["high"] == math.inf


def test_mae_buckets_also_cover_the_whole_line() -> None:
    rows = buckets([-1.4, -0.8, -0.1, 0.3], MAE_BUCKETS)
    assert sum(row["count"] for row in rows) == 4


def test_determinism_gate_passes_on_the_preregistered_numbers() -> None:
    gate = check_determinism(
        positions=369, tp_exits=130, stop_exits=239,
        avg_r=-0.0014889765123856217, max_hold_bars=129.0,
    )
    assert gate["passed"] is True


def test_determinism_gate_fails_when_a_single_count_drifts() -> None:
    """Tek bir pozisyonun kayması bile kapıyı düşürür: pencere aynı pencere değildir."""
    gate = check_determinism(
        positions=368, tp_exits=130, stop_exits=239,
        avg_r=-0.0014889765123856217, max_hold_bars=129.0,
    )
    assert gate["passed"] is False
    assert gate["checks"]["positions"]["ok"] is False


def test_determinism_gate_fails_on_a_drifting_average() -> None:
    gate = check_determinism(
        positions=369, tp_exits=130, stop_exits=239,
        avg_r=-0.002, max_hold_bars=129.0,
    )
    assert gate["passed"] is False
    assert gate["checks"]["avg_r"]["ok"] is False


# --------------------------------------------------------------------------- #
# Birincil varyant seçim kuralı (docs/backtest.md > 6e)
# --------------------------------------------------------------------------- #
def _select(**overrides: float) -> dict:
    """Hiçbir dalı tetiklemeyen taban; test yalnızca ilgilendiği ölçüyü oynatır."""
    base = dict(
        median_mfe_r_stop=0.3,      # M1 eşiği 1.0
        median_drift_r_tp=0.0,      # M2 eşiği +0.25
        median_hold_stop=6.0,       # M4 eşiği 2.0×
        median_hold_tp=8.0,
    )
    return select_primary_family(**(base | overrides))


def test_no_branch_fires_closes_the_round() -> None:
    """Yolda yapı yoksa tur kapanır — bu meşru bir sonuçtur, bir boşluk değil."""
    rule = _select()
    assert rule["round_closes"] is True
    assert rule["branch"] == "yok"


def test_m2_fires_on_signed_post_exit_drift() -> None:
    rule = _select(median_drift_r_tp=0.25)
    assert rule["branch"] == "M2"
    assert rule["round_closes"] is False


def test_m1_fires_when_losers_first_run_in_favour() -> None:
    rule = _select(median_mfe_r_stop=1.0)
    assert rule["branch"] == "M1"


def test_m4_fires_on_the_holding_time_ratio() -> None:
    rule = _select(median_hold_stop=16.0, median_hold_tp=8.0)
    assert rule["branch"] == "M4"


def test_priority_is_m2_then_m1_then_m4() -> None:
    """Üçü birden tetiklense bile sıra önceden yazılıdır; tartışma yok."""
    rule = _select(median_drift_r_tp=0.9, median_mfe_r_stop=1.4,
                   median_hold_stop=20.0, median_hold_tp=5.0)
    assert rule["branch"] == "M2"
    assert [item["fired"] for item in rule["measurements"]] == [True, True, True]

    without_m2 = _select(median_mfe_r_stop=1.4, median_hold_stop=20.0, median_hold_tp=5.0)
    assert without_m2["branch"] == "M1"


def test_thresholds_are_inclusive_at_the_boundary() -> None:
    """Sınır 'en az bu kadar'dır; kıl payı altı tetiklemez."""
    assert _select(median_mfe_r_stop=0.999)["round_closes"] is True
    assert _select(median_mfe_r_stop=1.0)["branch"] == "M1"


def test_nan_measurement_never_fires_a_branch() -> None:
    """Ölçülemeyen bir koşul sağlanmış sayılamaz (eksik çıta, geçilmiş çıta değildir)."""
    rule = _select(median_drift_r_tp=float("nan"), median_hold_tp=float("nan"))
    assert rule["round_closes"] is True
    assert all(not item["fired"] for item in rule["measurements"])


def test_signed_drift_is_close_based_and_nan_when_the_window_is_short() -> None:
    """İşaretli sürüklenme H. barın KAPANIŞIDIR; pencere dolmuyorsa `nan`."""
    (path,) = position_paths([TRADE], CANDLES)
    # Çıkıştan sonra iki bar var: kapanışları 130 ve 125 (_frame close=high).
    assert path["drift_r"]["5"] != path["drift_r"]["5"]  # nan: 5 bar yok
    assert position_paths([TRADE], CANDLES)[0]["continuation_r"]["5"] == pytest.approx(1.0)
