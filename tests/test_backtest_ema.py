"""scripts/backtest_ema.py: ön-kayıtlı kapıların MEKANİK uygulaması.

Bu dosyanın ölçtüğü şey bir metrik değil, bir KARAR KURALIDIR. Kapılar göz kararı
okunursa ön-kayıt bir metin olarak kalır: "K-1 galiba geçti", "çıpayı zaten geçemezdi"
türü okumalar tam olarak `docs/backtest.md > 7`nin yasakladığı esnekliktir. Testler
kuralın üç yerini sabitler:

1. **P1 bir KAPIDIR, bir tahmin değil.** Düştüğünde sonuç yorumlanmaz — diğer kapılar
   geçse bile.
2. **İki kapı kümesi de bağlayıcıdır.** Model sahibinin kapısı (K-1..K-3) repo kapısının
   (C-1..C-4) yerine geçmez; biri düşerse sonuç BLOKE'dur.
3. **Tek istisnanın KAPSAMI.** Yalnızca çıpa koşulundan (C-3) kalma durumu otomatik
   geçiş değil, DURMA üretir; başka bir koşuldan kalmak bloke eder. İstisnanın
   genişlemesi, sonucu gördükten sonra çıtayı yumuşatmanın kapısı olurdu.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import pytest

from main import jsonable
from scripts import backtest_ema


def _coin(
    *,
    period: str,
    symbol: str,
    model: str = backtest_ema.MODEL,
    profit_factor: float = 1.5,
    trades: int = 40,
    drawdown: float = -5.0,
) -> dict[str, Any]:
    return {
        "period": period,
        "symbol": symbol,
        "model": model,
        "trades": trades,
        "profit_factor": profit_factor,
        "max_drawdown_pct": drawdown,
    }


def _flag(
    *,
    passed: bool = True,
    sample: bool = True,
    edge: bool = True,
    avg_r: float = 0.30,
    control_avg_r: float = 0.05,
    total_return: float = 0.20,
    benchmark_return: float = 0.10,
) -> dict[str, Any]:
    return {
        "model": backtest_ema.MODEL,
        "sample": sample,
        "edge": edge,
        "passed": passed,
        "band": True,
        "measured_trades": 120,
        "min_trades": 30,
        "avg_r": avg_r,
        "control_avg_r": control_avg_r,
        "edge_margin_r": 0.15,
        "total_return": total_return,
        "benchmark_return": benchmark_return,
    }


def _payload(
    *,
    coins: list[dict[str, Any]] | None = None,
    flags: dict[str, Any] | None = None,
    btc_a: float = 1.55,
    btc_b: float = 1.30,
) -> dict[str, Any]:
    rows = coins if coins is not None else (
        [_coin(period="A", symbol="BTC-USDT-SWAP", profit_factor=btc_a)]
        + [_coin(period="B", symbol="BTC-USDT-SWAP", profit_factor=btc_b)]
        + [
            _coin(period="B", symbol=f"C{index}-USDT-SWAP", profit_factor=1.4)
            for index in range(5)
        ]
    )
    acceptance = flags if flags is not None else {"A": [_flag()], "B": [_flag()]}
    return {
        "coins": rows,
        "periods": {
            period: {"acceptance": items} for period, items in acceptance.items()
        },
    }


# --------------------------------------------------------------------------- #
# P1 — TradingView paritesi (KAPI)
# --------------------------------------------------------------------------- #
def test_p1_passes_inside_the_preregistered_tolerance() -> None:
    gates = backtest_ema.evaluate_gates(_payload(btc_a=1.55, btc_b=1.30))
    assert gates["P1_tradingview_parity"]["passed"] is True


def test_p1_fails_outside_the_tolerance_and_blocks_every_other_reading() -> None:
    """Sapma ±0.2'yi aşarsa sonuç YORUMLANMAZ: diğer kapılar geçse bile."""
    gates = backtest_ema.evaluate_gates(_payload(btc_a=1.10, btc_b=1.30))

    assert gates["P1_tradingview_parity"]["passed"] is False
    assert gates["K1_period_b_profit_factor"]["passed"] is True  # diğerleri geçiyor
    verdict = backtest_ema._verdict(gates)
    assert "P1 DÜŞTÜ" in verdict
    assert "GEÇİLMEZ" in verdict
    # Ayar araması ÖNERİLMEZ (docs/backtest.md > 7.1): sıradaki iş farkın açıklanmasıdır.
    assert "ayar araması DEĞİL" in verdict


def test_p1_deviation_is_reported_with_its_sign() -> None:
    """Sapmanın YÖNÜ bilgi: harness kaynaktan iyi mi çıkıyor, kötü mü?"""
    gates = backtest_ema.evaluate_gates(_payload(btc_a=1.751, btc_b=1.30))
    assert gates["P1_tradingview_parity"]["periods"]["A"]["deviation"] == pytest.approx(0.2)


def test_p1_cannot_pass_without_a_btc_measurement() -> None:
    """BTC koşusu düşmüşse kapı GEÇİLMİŞ sayılmaz; eksik ölçüm geçilmiş çıta değildir."""
    coins = [_coin(period="B", symbol="ETH-USDT-SWAP")]
    gates = backtest_ema.evaluate_gates(_payload(coins=coins))

    assert gates["P1_tradingview_parity"]["passed"] is False
    assert gates["P1_tradingview_parity"]["periods"]["A"]["measured"] is None


# --------------------------------------------------------------------------- #
# K-1..K-3 — model sahibinin ek kapıları
# --------------------------------------------------------------------------- #
def test_k1_counts_only_period_b_and_only_this_model() -> None:
    """Kapı OOS penceresinin kapısıdır; dönem A'nın coinleri sayıya girmez.

    `trend` satırları da girmez: kıyas hedefinin kâr faktörü bir bilgidir, bu modelin
    kapısı değil.
    """
    coins = (
        [_coin(period="A", symbol=f"A{i}-USDT-SWAP", profit_factor=9.0) for i in range(9)]
        + [_coin(period="B", symbol=f"T{i}-USDT-SWAP", profit_factor=9.0, model="trend")
           for i in range(9)]
        + [_coin(period="B", symbol=f"B{i}-USDT-SWAP", profit_factor=1.2) for i in range(3)]
        + [_coin(period="A", symbol="BTC-USDT-SWAP", profit_factor=1.55),
           _coin(period="B", symbol="BTC-USDT-SWAP", profit_factor=1.30)]
    )
    gates = backtest_ema.evaluate_gates(_payload(coins=coins))

    assert sorted(gates["K1_period_b_profit_factor"]["coins"]) == [
        "B0-USDT-SWAP", "B1-USDT-SWAP", "B2-USDT-SWAP", "BTC-USDT-SWAP"
    ]
    assert gates["K1_period_b_profit_factor"]["passed"] is False  # 4 < 6


def test_k1_threshold_is_strict() -> None:
    """Eşik "1.1'den BÜYÜK": tam 1.1 geçmez, aksi hâlde eşik sessizce gevşerdi."""
    coins = [_coin(period="B", symbol=f"C{i}-USDT-SWAP", profit_factor=1.1) for i in range(8)]
    gates = backtest_ema.evaluate_gates(_payload(coins=coins))
    assert gates["K1_period_b_profit_factor"]["coins"] == []


def test_k1_ignores_a_non_finite_profit_factor() -> None:
    """Kayıpsız bir coinde kâr faktörü sonsuzdur; onu "geçti" saymak gürültüyü ödüllendirirdi."""
    coins = [
        _coin(period="B", symbol=f"C{i}-USDT-SWAP", profit_factor=float("inf"))
        for i in range(8)
    ]
    coins += [_coin(period="B", symbol="N-USDT-SWAP", profit_factor=float("nan"))]
    gates = backtest_ema.evaluate_gates(_payload(coins=coins))
    assert gates["K1_period_b_profit_factor"]["coins"] == []


def test_k2_counts_both_periods_but_only_this_model() -> None:
    coins = [
        _coin(period="A", symbol="BTC-USDT-SWAP", trades=200, profit_factor=1.55),
        _coin(period="B", symbol="BTC-USDT-SWAP", trades=101, profit_factor=1.30),
        _coin(period="B", symbol="X-USDT-SWAP", trades=5000, model="trend"),
    ]
    gates = backtest_ema.evaluate_gates(_payload(coins=coins))

    assert gates["K2_total_trades"]["measured"] == 301
    assert gates["K2_total_trades"]["passed"] is True


def test_k3_flags_any_coin_in_any_period() -> None:
    """Kapı "HİÇBİR coinde" der: tek bir aşım yeter ve hangisi olduğu raporlanır."""
    coins = [
        _coin(period="A", symbol="BTC-USDT-SWAP", profit_factor=1.55, drawdown=-3.0),
        _coin(period="B", symbol="BTC-USDT-SWAP", profit_factor=1.30, drawdown=-25.01),
    ]
    gates = backtest_ema.evaluate_gates(_payload(coins=coins))

    assert gates["K3_max_drawdown"]["passed"] is False
    assert gates["K3_max_drawdown"]["breaches"][0]["symbol"] == "BTC-USDT-SWAP"
    assert gates["K3_max_drawdown"]["breaches"][0]["period"] == "B"


def test_failed_coin_runs_never_count_as_passing() -> None:
    """Koşulamayan sembol bir sayı üretmez; sessizce "geçti" tarafına düşemez."""
    coins = [
        {"period": "B", "symbol": "PENGU-USDT-SWAP", "failed": "veri yok"},
        _coin(period="A", symbol="BTC-USDT-SWAP", profit_factor=1.55),
        _coin(period="B", symbol="BTC-USDT-SWAP", profit_factor=1.30),
    ]
    gates = backtest_ema.evaluate_gates(_payload(coins=coins))
    assert "PENGU-USDT-SWAP" not in gates["K1_period_b_profit_factor"]["coins"]


# --------------------------------------------------------------------------- #
# Karar kuralı: iki kapı kümesi de bağlayıcı, tek istisnanın kapsamı dar
# --------------------------------------------------------------------------- #
def test_everything_green_allows_paper_trading() -> None:
    coins = [
        _coin(period="A", symbol="BTC-USDT-SWAP", profit_factor=1.55, trades=200),
        _coin(period="B", symbol="BTC-USDT-SWAP", profit_factor=1.30, trades=200),
    ] + [_coin(period="B", symbol=f"C{i}-USDT-SWAP", profit_factor=1.4) for i in range(5)]
    gates = backtest_ema.evaluate_gates(_payload(coins=coins))

    assert "TÜM KAPILAR GEÇİLDİ" in backtest_ema._verdict(gates)


def test_owner_gate_failure_blocks_even_when_repo_gates_pass() -> None:
    """Model sahibinin kapısı repo kapısının yerine geçmez; ikisi de bağlayıcı."""
    coins = [
        _coin(period="A", symbol="BTC-USDT-SWAP", profit_factor=1.55, trades=10),
        _coin(period="B", symbol="BTC-USDT-SWAP", profit_factor=1.30, trades=10),
    ]
    gates = backtest_ema.evaluate_gates(_payload(coins=coins))

    verdict = backtest_ema._verdict(gates)
    assert verdict.startswith("SONUÇ: BLOKE")
    assert "K1_period_b_profit_factor" in verdict
    assert "K2_total_trades" in verdict


def test_failing_only_the_benchmark_condition_stops_instead_of_blocking() -> None:
    """Ön-kayıtlı TEK istisna: karar otomatik değil, model sahibinindir."""
    coins = [
        _coin(period="A", symbol="BTC-USDT-SWAP", profit_factor=1.55, trades=200),
        _coin(period="B", symbol="BTC-USDT-SWAP", profit_factor=1.30, trades=200),
    ] + [_coin(period="B", symbol=f"C{i}-USDT-SWAP", profit_factor=1.4) for i in range(5)]
    # Çıpanın ALTINDA kalıyor (C-3 düşük), geri kalan her koşul sağlanıyor.
    only_benchmark = _flag(passed=False, total_return=0.05, benchmark_return=1.10)
    gates = backtest_ema.evaluate_gates(
        _payload(coins=coins, flags={"A": [only_benchmark], "B": [only_benchmark]})
    )

    verdict = backtest_ema._verdict(gates)
    assert verdict.startswith("SONUÇ: DUR")
    assert "ÇIPA" in verdict
    assert "karar model sahibinindir" in verdict


def test_failing_the_sample_gate_blocks_instead_of_stopping() -> None:
    """İstisna YALNIZCA çıpa içindir: örneklem kapısı düşerse sonuç BLOKE'dur."""
    coins = [
        _coin(period="A", symbol="BTC-USDT-SWAP", profit_factor=1.55, trades=200),
        _coin(period="B", symbol="BTC-USDT-SWAP", profit_factor=1.30, trades=200),
    ] + [_coin(period="B", symbol=f"C{i}-USDT-SWAP", profit_factor=1.4) for i in range(5)]
    thin = _flag(passed=False, sample=False, total_return=0.05, benchmark_return=1.10)
    gates = backtest_ema.evaluate_gates(
        _payload(coins=coins, flags={"A": [thin], "B": [thin]})
    )

    verdict = backtest_ema._verdict(gates)
    assert verdict.startswith("SONUÇ: BLOKE")
    assert "çıpa dışındaki" in verdict


def test_failing_the_edge_margin_blocks_even_if_the_benchmark_is_also_missed() -> None:
    """Kontrolü marjla geçemeyen model, "sadece çıpadan kaldı" sayılamaz."""
    coins = [
        _coin(period="A", symbol="BTC-USDT-SWAP", profit_factor=1.55, trades=200),
        _coin(period="B", symbol="BTC-USDT-SWAP", profit_factor=1.30, trades=200),
    ] + [_coin(period="B", symbol=f"C{i}-USDT-SWAP", profit_factor=1.4) for i in range(5)]
    weak = _flag(passed=False, avg_r=0.06, control_avg_r=0.05,
                 total_return=0.05, benchmark_return=1.10)
    gates = backtest_ema.evaluate_gates(
        _payload(coins=coins, flags={"A": [weak], "B": [weak]})
    )
    assert backtest_ema._verdict(gates).startswith("SONUÇ: BLOKE")


# --------------------------------------------------------------------------- #
# Embargo ölçümü ve CSV
# --------------------------------------------------------------------------- #
class _Result:
    def __init__(self, holding: dict[str, Any]) -> None:
        self.holding = holding


def test_embargo_is_measured_and_rounded_up() -> None:
    """Aşağı yuvarlamak, örtüşen son barları OOS'a bırakırdı."""
    assert backtest_ema.measured_embargo_bars(
        _Result({backtest_ema.MODEL: {"max_bars": 37.2}})  # type: ignore[arg-type]
    ) == 38


def test_embargo_falls_back_to_zero_but_says_so(caplog: pytest.LogCaptureFixture) -> None:
    """Sessiz bir sıfır, "örtüşme yok" ile "ölçemedik"i aynı hücreye yazardı."""
    import logging

    with caplog.at_level(logging.WARNING, logger="backtest-ema"):
        value = backtest_ema.measured_embargo_bars(
            _Result({backtest_ema.MODEL: {"max_bars": float("nan")}})  # type: ignore[arg-type]
        )
    assert value == 0
    assert any("ÖLÇÜLEMEDİ" in record.message for record in caplog.records)


def test_csv_keeps_every_column_even_when_a_row_is_a_failure(tmp_path: Path) -> None:
    """Düşen sembolün satırı da yazılır: eksik satır, eksik ölçümü görünmez kılardı."""
    rows = [
        {"period": "A", "symbol": "BTC-USDT-SWAP", "trades": 10},
        {"period": "A", "symbol": "PENGU-USDT-SWAP", "failed": "veri yok"},
    ]
    path = tmp_path / "out.csv"
    backtest_ema._write_csv(path, rows)

    text = path.read_text(encoding="utf-8")
    assert "failed" in text.splitlines()[0]
    assert "PENGU-USDT-SWAP" in text


def test_preregistered_constants_match_the_document() -> None:
    """Sabitler ön-kayıttan gelir; kodda kaymaları sessiz bir kural değişikliği olurdu."""
    document = (Path(__file__).resolve().parent.parent / "docs/backtest.md").read_text(
        encoding="utf-8"
    )
    assert "1.551" in document and "1.299" in document
    assert backtest_ema.TV_REFERENCE == {"A": 1.551, "B": 1.299}
    assert backtest_ema.TV_TOLERANCE == 0.2
    assert (backtest_ema.K1_MIN_COINS, backtest_ema.K1_MIN_PROFIT_FACTOR) == (6, 1.1)
    assert backtest_ema.K2_MIN_TRADES == 300
    assert backtest_ema.K3_MAX_DRAWDOWN_PCT == 25.0
    assert (backtest_ema.FEE_RATE, backtest_ema.SLIPPAGE_BASE) == (0.00075, 0.0001)
    assert math.isclose(backtest_ema.FEE_RATE + backtest_ema.SLIPPAGE_BASE, 0.00085)


def _strict_loads(text: str) -> Any:
    """`NaN`/`Infinity`ye izin VERMEYEN bir okuma — tarayıcının `JSON.parse`ı gibi.

    `json.loads` bu üç sabiti varsayılan olarak kabul eder, tarayıcı etmez; varsayılanla
    sınamak, sayfanın hiç ayrıştıramadığı bir dosyayı "geçerli" göstermek olurdu.
    """

    def reject(constant: str) -> Any:
        raise ValueError(f"geçersiz JSON sabiti: {constant}")

    return json.loads(text, parse_constant=reject)


def test_site_payload_is_valid_json_for_a_browser() -> None:
    """Tanımsız metrik `null` olarak yazılır, `NaN` olarak DEĞİL.

    Yükün her dalı tanımsız sayı taşır (hiç short açmamış bir modelin kolonları, listede
    olmayan bir sembolün al-tut getirisi — bkz. core/metrics.py "veri yoksa nan"). `NaN`
    geçerli JSON değildir: dosya `docs/backtest.html` tarafından hiç ayrıştırılamaz ve
    sayfa tek bir sayı çizemeden ölür. Bu yüzden yazma yolu `main.jsonable`dan geçer —
    ölçümün kendisi değişmez, yalnızca "ölçülemedi"nin JSON'daki karşılığı yazılır.
    """
    payload = {
        "model": backtest_ema.MODEL,
        "gates": {"P1": {"measured": float("nan"), "passed": False}},
        "periods": {
            "A": {
                "breakdowns": {"symbol": {"BTC": {"avg_r": float("nan")}}},
                "models": [{"short": {"avg_r": float("nan"), "payoff": math.inf}}],
                "buy_hold_pct": {"PENGU-USDT-SWAP": float("nan")},
            },
        },
    }

    text = json.dumps(jsonable(backtest_ema.site_payload(payload)), ensure_ascii=False)

    assert "NaN" not in text
    restored = _strict_loads(text)
    assert restored["gates"]["P1"]["measured"] is None
    assert restored["periods"]["A"]["models"][0]["short"]["payoff"] is None
    assert restored["periods"]["A"]["buy_hold_pct"]["PENGU-USDT-SWAP"] is None


def test_unsanitised_payload_would_not_parse() -> None:
    """Yukarıdaki testin neyi yakaladığını sabitler: çağrı kaldırılırsa dosya bozulur.

    Sanitasyon olmadan `json.dumps` `NaN` yazar ve tarayıcının okuması patlar. Bu test
    olmadan `jsonable(...)` çağrısı "gereksiz sarmalayıcı" diye kaldırılabilirdi.
    """
    text = json.dumps({"avg_r": float("nan")})

    with pytest.raises(ValueError):
        _strict_loads(text)
