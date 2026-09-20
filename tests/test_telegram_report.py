"""scripts/telegram_report.py: kapılar, mesaj içeriği ve "koşuyu asla düşürme" sözü.

Bu script'in en önemli özelliği ne yolladığı değil, HİÇBİR KOŞULDA sıfırdan farklı
dönmemesidir: özet bir bildirimdir, ölçümün parçası değil.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "telegram_report", Path(__file__).resolve().parent.parent / "scripts" / "telegram_report.py"
)
assert _SPEC and _SPEC.loader
telegram_report = importlib.util.module_from_spec(_SPEC)
sys.modules["telegram_report"] = telegram_report
_SPEC.loader.exec_module(telegram_report)


def _direction(**overrides: Any) -> dict[str, Any]:
    payload = {
        "direction": "long", "trades": 40, "avg_r": 0.2, "win_rate": 0.55,
        "funding": -12.5, "profit_factor": 1.4, "total_r": 8.0, "pnl": 400.0,
        "avg_stop_distance_pct": 3.0, "cost_per_r": 0.09,
    }
    payload.update(overrides)
    return payload


def _model(model: str, *, avg_r: float | None = 0.2, trades: int = 40,
           total_return: float = 0.05, benchmark: bool = False,
           replica: bool = False) -> dict[str, Any]:
    return {
        "model": model, "name": model, "is_benchmark": benchmark, "is_replica": replica,
        "total": _direction(avg_r=avg_r, trades=trades),
        "long": _direction(), "short": _direction(direction="short"),
        "account": {"total_return": total_return, "max_drawdown": -0.03,
                    "final_equity": 10_500.0, "initial_capital": 10_000.0,
                    "sharpe": 1.1, "bars": 100},
    }


def _payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "as_of": "2026-03-10T20:00:00+00:00",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "dry_run": False,
        "benchmarks": ["buyhold"],
        "models": [
            _model("trend", avg_r=0.4, total_return=0.11),
            _model("meanrev", avg_r=-0.2, total_return=-0.04),
            _model("random_ctrl", avg_r=0.0, total_return=0.0),
            _model("buyhold", avg_r=None, total_return=0.06, benchmark=True),
        ],
        "pooled": {"models": ["trend", "meanrev"], "directions": {
            "long": _direction(avg_r=0.10, trades=120),
            "short": _direction(direction="short", avg_r=0.25, trades=90, funding=31.0),
            "total": _direction(direction="total"),
        }},
        "acceptance": {
            "control_model": "random_ctrl", "min_trades": 30, "stop_band_ratio": 2.5,
            "edge_margin_r": 0.15,
            "models": [
                {"model": "trend", "sample": True, "band": True, "edge": True, "passed": True,
                 "measured_trades": 40, "min_trades": 30, "avg_stop_distance_pct": 3.0,
                 "band_low": 2.0, "band_high": 5.0, "avg_r": 0.4, "control_avg_r": 0.0,
                 "edge_margin_r": 0.15, "total_return": 0.11, "benchmark_return": 0.06},
            ],
        },
        "activity": {"since": "2026-03-09T20:00:00+00:00", "hours": 24, "opened": 4,
                     "still_open": 2, "closed": 3, "closed_pnl": 55.0, "closed_avg_r": 0.3,
                     "closed_wins": 2, "trades": [
                         {"model": "trend", "symbol": "BTC-USDT-SWAP", "direction": "short",
                          "r": 1.2, "exit_reason": "take_profit", "pnl": 36.0},
                     ]},
    }
    payload.update(overrides)
    return payload


def _write(tmp_path: Path, payload: dict[str, Any]) -> Path:
    path = tmp_path / "metrics.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _run(tmp_path: Path, payload: dict[str, Any], *args: str) -> int:
    return telegram_report.main([*args, "--metrics", str(_write(tmp_path, payload))])


# --------------------------------------------------------------------------- #
# Mesaj içeriği
# --------------------------------------------------------------------------- #
def test_message_leads_with_the_long_short_question() -> None:
    message = telegram_report.build_message(_payload())
    assert message.index("LONG vs SHORT") < message.index("İlk")
    assert "+0.10R" in message and "+0.25R" in message
    assert "Short" in message.split("İlk")[0]  # havuz kazananı söylenir


def test_message_lists_top_and_bottom_models() -> None:
    message = telegram_report.build_message(_payload())
    assert "trend" in message and "meanrev" in message


def test_message_reports_the_daily_activity() -> None:
    message = telegram_report.build_message(_payload())
    assert "4 açılan" in message and "3 kapanan" in message
    assert "2/3 kazanç" in message


def test_message_says_who_cleared_the_acceptance_bar() -> None:
    message = telegram_report.build_message(_payload())
    assert "iki kapıyı da geçen" in message and "trend" in message


def test_message_says_when_nobody_cleared_the_bar() -> None:
    """Geçilememesi de bir sonuçtur ve atlanmaz (kural 15)."""
    payload = _payload()
    payload["acceptance"]["models"][0].update(passed=False, edge=False)
    message = telegram_report.build_message(payload)
    assert "geçen model yok" in message
    assert "E✗" in message
    assert "gereken marj 0.15R" in message


def test_band_never_appears_as_a_failed_gate() -> None:
    """Band bir kapı değil: "B✗" yazmak doğrulanmış bir modeli reddedilmiş gösterirdi."""
    payload = _payload()
    payload["acceptance"]["models"][0].update(passed=False, edge=False, band=False)
    message = telegram_report.build_message(payload)
    assert "B✗" not in message


def test_band_warning_rides_along_with_a_passing_model() -> None:
    """Geçen model bandın dışındaysa uyarı anılır — ama "geçti" bozulmaz."""
    payload = _payload()
    payload["acceptance"]["models"][0].update(band=False)
    message = telegram_report.build_message(payload)
    assert "iki kapıyı da geçen" in message
    assert "⚠" in message and "cost_per_r" in message


def test_message_marks_the_control_group_in_the_ranking() -> None:
    """Bilgisiz çekilişin sıralamada nerede olduğu özetin taşıması gereken bilgidir."""
    message = telegram_report.build_message(_payload())
    assert "random_ctrl" in message and "kontrol grubu" in message


def test_message_reports_the_benchmark_as_the_floor() -> None:
    message = telegram_report.build_message(_payload())
    assert "buyhold" in message and "+6.00%" in message


def test_model_names_with_underscores_survive_intact() -> None:
    """HTML seçilmesinin sebebi: Markdown'da `failed_breakout` italik açar, ad bozulurdu."""
    payload = _payload()
    payload["models"].append(_model("failed_breakout", avg_r=0.9, total_return=0.2))
    message = telegram_report.build_message(payload)
    assert "failed_breakout" in message
    assert "failed<i>breakout" not in message


def test_dangerous_characters_in_a_name_are_escaped() -> None:
    """Kaçırılmayan bir `<`, Telegram'dan 400 döndürüp özeti tamamen susturur."""
    payload = _payload()
    payload["models"].append(_model("a<b&c", avg_r=0.9))
    message = telegram_report.build_message(payload)
    assert "a&lt;b&amp;c" in message


def test_undefined_metrics_render_as_a_dash_not_zero() -> None:
    payload = _payload()
    payload["pooled"]["directions"]["short"]["avg_r"] = None
    message = telegram_report.build_message(payload)
    assert "—" in message


def test_message_stays_within_telegram_limits() -> None:
    payload = _payload()
    payload["models"] = [
        _model(f"model_{index}", avg_r=index / 10.0) for index in range(40)
    ]
    assert len(telegram_report.build_message(payload)) <= telegram_report.MAX_MESSAGE_CHARS + 2


def test_models_without_closed_trades_stay_out_of_the_ranking() -> None:
    payload = _payload(models=[
        _model("ölçülen", avg_r=0.3),
        _model("bos", avg_r=None),
    ], acceptance={"control_model": "random_ctrl", "min_trades": 20,
                   "stop_band_ratio": 2.5, "models": []})
    message = telegram_report.build_message(payload)
    assert "1 model henüz kapanmış işlem üretmedi" in message


# --------------------------------------------------------------------------- #
# Kapılar
# --------------------------------------------------------------------------- #
def test_only_the_configured_hour_sends(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    payload = _payload(as_of="2026-03-10T04:00:00+00:00")
    with caplog.at_level("INFO"):
        assert _run(tmp_path, payload, "--dry-run") == 0
    assert "özet turu değil" in caplog.text


def test_force_skips_the_hour_gate(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert _run(tmp_path, _payload(as_of="2026-03-10T04:00:00+00:00"), "--dry-run", "--force") == 0
    assert "LONG vs SHORT" in capsys.readouterr().out


def test_stale_report_is_not_resent(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Tur düşerse dosya bir önceki turdan kalır; saat kapısı tek başına yetmez."""
    payload = _payload(generated_at="2020-01-01T20:05:00+00:00")  # dünkü 20:00 raporu
    with caplog.at_level("WARNING"):
        assert _run(tmp_path, payload, "--dry-run") == 0
    assert "bayat" in caplog.text or "saatlik" in caplog.text


def test_dry_run_report_is_not_announced(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert _run(tmp_path, _payload(dry_run=True), "--dry-run") == 0
    assert capsys.readouterr().out == ""


# --------------------------------------------------------------------------- #
# "Koşuyu asla düşürme"
# --------------------------------------------------------------------------- #
def test_missing_metrics_file_is_not_an_error(tmp_path: Path) -> None:
    assert telegram_report.main(["--metrics", str(tmp_path / "yok.json")]) == 0


def test_broken_json_is_not_an_error(tmp_path: Path) -> None:
    path = tmp_path / "metrics.json"
    path.write_text("{bozuk", encoding="utf-8")
    assert telegram_report.main(["--metrics", str(path), "--force"]) == 0


def test_missing_secrets_skip_quietly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    assert _run(tmp_path, _payload()) == 0


def test_network_failure_is_logged_and_swallowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "chat")

    def _boom(**_: Any) -> None:
        raise RuntimeError("ağ yok")

    monkeypatch.setattr(telegram_report, "_send", _boom)
    with caplog.at_level("WARNING"):
        assert _run(tmp_path, _payload()) == 0
    assert "ağ yok" in caplog.text


def test_telegram_4xx_is_logged_and_swallowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "chat")
    sent: dict[str, Any] = {}

    class _Response:
        status_code = 400
        text = "Bad Request: chat not found"

    def _post(url: str, **kwargs: Any) -> _Response:
        sent.update(url=url, **kwargs)
        return _Response()

    monkeypatch.setitem(sys.modules, "requests", type("M", (), {"post": staticmethod(_post)}))
    with caplog.at_level("WARNING"):
        assert _run(tmp_path, _payload()) == 0
    assert "chat not found" in caplog.text
    assert "token" not in caplog.text  # gizli anahtar loga yazılmaz


def test_successful_send_posts_html_to_the_chat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "gizli-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-100123")
    sent: dict[str, Any] = {}

    class _Response:
        status_code = 200
        text = "{}"

    def _post(url: str, **kwargs: Any) -> _Response:
        sent.update(url=url, **kwargs)
        return _Response()

    monkeypatch.setitem(sys.modules, "requests", type("M", (), {"post": staticmethod(_post)}))
    assert _run(tmp_path, _payload()) == 0
    assert sent["url"].endswith("/botgizli-token/sendMessage")
    assert sent["json"]["chat_id"] == "-100123"
    assert sent["json"]["parse_mode"] == "HTML"
    assert "LONG vs SHORT" in sent["json"]["text"]


def test_replicas_are_not_ranked_as_competitors() -> None:
    """Kural 15b: kopyanın 1R'si başka bir birimdedir, sıralamaya giremez.

    Özet `core/metrics.py`nin dışarıda bıraktığı kıyası geri getiremez: kopya en yüksek
    ortalama R'ye sahip olsa bile listenin başına oturmamalıdır.
    """
    payload = _payload(
        replicas=["vwap_clone"],
        models=[
            _model("trend", avg_r=0.4, total_return=0.11),
            _model("random_ctrl", avg_r=0.0, total_return=0.0),
            _model("buyhold", avg_r=None, total_return=0.06, benchmark=True),
            _model("vwap_clone", avg_r=9.0, total_return=0.90, replica=True),
        ],
    )

    competitors = telegram_report._competitors(payload)

    assert {row["model"] for row in competitors} == {"trend", "random_ctrl"}
