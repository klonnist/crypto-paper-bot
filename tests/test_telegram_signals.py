"""scripts/telegram_signals.py: tetikleme, üç filtre ve "koşuyu asla düşürme" sözü.

Bu script'in en önemli iki özelliği: (a) YALNIZCA yeni sinyalde ve yalnızca SON barın
sinyalinde mesaj yollar, (b) hiçbir koşulda sıfırdan farklı dönmez — bildirim ölçümün
parçası değildir ve Telegram kesintisi turu kırmızıya çeviremez.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "telegram_signals", Path(__file__).resolve().parent.parent / "scripts" / "telegram_signals.py"
)
assert _SPEC and _SPEC.loader
telegram_signals = importlib.util.module_from_spec(_SPEC)
sys.modules["telegram_signals"] = telegram_signals
_SPEC.loader.exec_module(telegram_signals)

AS_OF = datetime(2026, 9, 14, 7, 45, tzinfo=timezone.utc)
BAR = timedelta(minutes=15)


# --------------------------------------------------------------------------- #
# Yardımcılar
# --------------------------------------------------------------------------- #
def _signal(
    *,
    model: str = "vwap_managed",
    symbol: str = "BTC-USDT-SWAP",
    direction: str = "long",
    bar: datetime = AS_OF,
    arm: str = "rsi2_reversal",
    **overrides: Any,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": model,
        "symbol": symbol,
        "direction": direction,
        "bar": bar.isoformat(),
        "fills_at": (bar + BAR).isoformat(),
        "close": 64123.5,
        "stop_price": 63482.3,
        "target_price": 65405.9,
        "reward_risk": 2.0,
        "reason": f"RSI(2) dönüşü; stop 1.00% (63482.3) | arm={arm} | post_r=0.31",
    }
    payload.update(overrides)
    return payload


def _payload(signals: Sequence[dict[str, Any]] = (), **overrides: Any) -> dict[str, Any]:
    by_model: dict[str, list[dict[str, Any]]] = {}
    for signal in signals:
        by_model.setdefault(str(signal["model"]), []).append(signal)
    payload: dict[str, Any] = {
        "layer": "scalp",
        "as_of": AS_OF.isoformat(),
        "dry_run": False,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "settings": {"timeframe": "15m"},
        "round": {
            "as_of": AS_OF.isoformat(),
            "models": [
                {"model": model, "signals": len(emitted), "emitted": emitted}
                for model, emitted in by_model.items()
            ],
        },
    }
    payload.update(overrides)
    return payload


class _Response:
    def __init__(self, status_code: int = 200, text: str = "{}") -> None:
        self.status_code = status_code
        self.text = text


class _FakeRequests:
    """`requests` yerine geçen sahte modül; gönderilen gövdeleri saklar."""

    def __init__(self, response: Any = None) -> None:
        self.response = response if response is not None else _Response()
        self.calls: list[dict[str, Any]] = []

    def post(self, url: str, *, json: dict[str, Any], timeout: float) -> Any:
        self.calls.append(json)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


@pytest.fixture
def telegram(monkeypatch: pytest.MonkeyPatch) -> _FakeRequests:
    fake = _FakeRequests()
    monkeypatch.setitem(sys.modules, "requests", fake)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "gizli-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "4242")
    return fake


def _run(
    tmp_path: Path,
    payload: dict[str, Any],
    *,
    state: dict[str, str] | None = None,
    args: Sequence[str] = (),
) -> tuple[int, Path]:
    metrics = tmp_path / "metrics_scalp.json"
    metrics.write_text(json.dumps(payload), encoding="utf-8")
    state_path = tmp_path / "state" / "telegram_scalp.json"
    if state is not None:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps({"sent": state}), encoding="utf-8")
    code = telegram_signals.main(
        ["--metrics", str(metrics), "--state", str(state_path), *args]
    )
    return code, state_path


def _messages(fake: _FakeRequests) -> list[str]:
    return [call["text"] for call in fake.calls]


# --------------------------------------------------------------------------- #
# Tetikleme: yalnızca YENİ SİNYAL
# --------------------------------------------------------------------------- #
def test_new_signal_on_the_last_bar_is_notified(tmp_path: Path, telegram: _FakeRequests) -> None:
    code, _ = _run(tmp_path, _payload([_signal()]))

    assert code == 0
    (message,) = _messages(telegram)
    assert "vwap_managed" in message and "rsi2_reversal" in message
    assert "BTC-USDT-SWAP" in message and "LONG" in message
    assert "2026-09-14 07:45" in message          # sinyalin üretildiği bar
    assert "64123.5" in message                   # o barın kapanışı
    assert "63482.3" in message and "65405.9" in message   # stop ve hedef
    assert "R:R 2.00" in message


def test_message_carries_the_mandatory_fill_warning(tmp_path: Path, telegram: _FakeRequests) -> None:
    """Uyarı opsiyonel DEĞİLDİR: emir bir sonraki barın açılışından dolar (kural 13),
    mesajı okuyan kişi araya girerse fiyatı botunkiyle aynı olmaz."""
    _run(tmp_path, _payload([_signal()]))

    (message,) = _messages(telegram)
    assert (
        "Bot bu emri bir sonraki bar açılışından dolduracak (2026-09-14 08:00 UTC). "
        "Senin girişin farklı bir fiyattan olacak." in message
    )


def test_a_round_without_signals_sends_nothing(tmp_path: Path, telegram: _FakeRequests) -> None:
    """Kapanış, funding ve bar ilerlemesi mesaj üretmez — tetikleyen tek olay yeni sinyaldir."""
    code, _ = _run(tmp_path, _payload([]))

    assert code == 0
    assert telegram.calls == []


# --------------------------------------------------------------------------- #
# Filtre 0: yalnızca NOTIFY_MODELS'teki modeller (kullanıcı tercihi)
# --------------------------------------------------------------------------- #
def test_only_notify_models_are_sent(tmp_path: Path, telegram: _FakeRequests) -> None:
    """Yalnızca `vwap_managed` ve `vwap_reentry` Telegram'a düşer (kullanıcı tercihi); diğer
    scalp modelleri (scalp_fixed, scalp_patient, vwap_clone) deftere yazılır ve ölçüme girer."""
    signals = [
        _signal(model="scalp_fixed", symbol="ETH-USDT-SWAP"),
        _signal(model="scalp_patient", symbol="SOL-USDT-SWAP"),
        _signal(model="vwap_clone", symbol="XRP-USDT-SWAP"),
        _signal(model="vwap_managed", symbol="BTC-USDT-SWAP"),
        _signal(model="vwap_reentry", symbol="ADA-USDT-SWAP"),
    ]
    _run(tmp_path, _payload(signals))

    text = " ".join(_messages(telegram))
    assert "BTC-USDT-SWAP" in text and "ADA-USDT-SWAP" in text
    for other in ("ETH-USDT-SWAP", "SOL-USDT-SWAP", "XRP-USDT-SWAP"):
        assert other not in text


def test_the_notify_set_is_exactly_the_two_vwap_models() -> None:
    from scripts import telegram_signals

    assert telegram_signals.NOTIFY_MODELS == frozenset({"vwap_managed", "vwap_reentry"})


def test_no_notify_model_signals_means_no_message(tmp_path: Path, telegram: _FakeRequests) -> None:
    code, _ = _run(tmp_path, _payload([_signal(model="scalp_patient")]))

    assert code == 0
    assert telegram.calls == []


# --------------------------------------------------------------------------- #
# Filtre 1: yalnızca SON barın sinyalleri
# --------------------------------------------------------------------------- #
def test_recovered_bar_signals_are_not_notified(tmp_path: Path, telegram: _FakeRequests) -> None:
    """Telafi barlarının sinyali deftere yazılır ama bildirilmez: emri çoktan dolmuştur."""
    old = _signal(symbol="ETH-USDT-SWAP", bar=AS_OF - 3 * BAR)
    _run(tmp_path, _payload([old, _signal()]))

    (message,) = _messages(telegram)
    assert "BTC-USDT-SWAP" in message
    assert "ETH-USDT-SWAP" not in message


def test_only_recovered_bars_means_no_message(tmp_path: Path, telegram: _FakeRequests) -> None:
    _run(tmp_path, _payload([_signal(bar=AS_OF - BAR), _signal(bar=AS_OF - 2 * BAR)]))

    assert telegram.calls == []


def test_unreadable_as_of_sends_nothing(tmp_path: Path, telegram: _FakeRequests) -> None:
    """Hangi barın "son" olduğu bilinmeden filtre uygulanamaz; filtresiz bildirim
    tam da engellenmek istenen gürültüdür."""
    code, _ = _run(tmp_path, _payload([_signal()], as_of=""))

    assert code == 0
    assert telegram.calls == []


# --------------------------------------------------------------------------- #
# Filtre 2: aynı (model, sembol, yön) için 4 bar susturma
# --------------------------------------------------------------------------- #
def test_same_setup_within_four_bars_is_suppressed(tmp_path: Path, telegram: _FakeRequests) -> None:
    state = {"vwap_managed|BTC-USDT-SWAP|long": (AS_OF - 2 * BAR).isoformat()}
    code, _ = _run(tmp_path, _payload([_signal()]), state=state)

    assert code == 0
    assert telegram.calls == []


def test_same_setup_after_four_bars_is_notified_again(tmp_path: Path, telegram: _FakeRequests) -> None:
    state = {"vwap_managed|BTC-USDT-SWAP|long": (AS_OF - 4 * BAR).isoformat()}
    _run(tmp_path, _payload([_signal()]), state=state)

    assert len(telegram.calls) == 1


def test_suppression_is_scoped_to_model_symbol_and_direction(
    tmp_path: Path, telegram: _FakeRequests
) -> None:
    """Susturulan yalnızca AYNI kurulumdur: başka sembol ya da ters yön geçer.

    "Başka model" artık NOTIFY_MODELS filtresine tabidir (bkz.
    test_only_notify_models_are_sent) — susturma penceresinin kapsamını burada
    ayrıca sınamaya gerek yok, o modelin sinyali zaten filtrede elenir.
    """
    state = {"vwap_managed|BTC-USDT-SWAP|long": (AS_OF - BAR).isoformat()}
    signals = [
        _signal(),                                    # susturulur
        _signal(direction="short"),                   # ters yön
        _signal(symbol="SOL-USDT-SWAP"),              # başka sembol
    ]
    _run(tmp_path, _payload(signals), state=state)

    messages = " ".join(_messages(telegram))
    assert len(telegram.calls) == 2
    assert "SHORT" in messages and "SOL-USDT-SWAP" in messages


def test_notified_signals_are_written_to_the_state_file(tmp_path: Path, telegram: _FakeRequests) -> None:
    _, state_path = _run(tmp_path, _payload([_signal()]))

    sent = json.loads(state_path.read_text(encoding="utf-8"))["sent"]
    assert sent["vwap_managed|BTC-USDT-SWAP|long"] == AS_OF.isoformat()


def test_state_is_not_written_when_nothing_was_sent(tmp_path: Path, telegram: _FakeRequests) -> None:
    """Yollanamamış bir mesajı "bildirildi" saymak, bildirimi sessizce kaybederdi."""
    telegram.response = _Response(status_code=400, text="chat not found")
    _, state_path = _run(tmp_path, _payload([_signal()]))

    assert not state_path.exists()


def test_dry_run_prints_and_leaves_the_state_alone(
    tmp_path: Path, telegram: _FakeRequests, capsys: pytest.CaptureFixture[str]
) -> None:
    code, state_path = _run(tmp_path, _payload([_signal()]), args=["--dry-run"])

    assert code == 0
    assert telegram.calls == []
    assert not state_path.exists()
    assert "SCALP SİNYALİ" in capsys.readouterr().out


def test_a_corrupt_state_file_does_not_silence_the_notification(
    tmp_path: Path, telegram: _FakeRequests
) -> None:
    """Bozuk durum dosyası yüzünden susmak, tekrar eden bir mesajdan daha büyük kayıptır."""
    state_path = tmp_path / "state" / "telegram_scalp.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text("{bozuk", encoding="utf-8")

    metrics = tmp_path / "metrics_scalp.json"
    metrics.write_text(json.dumps(_payload([_signal()])), encoding="utf-8")
    code = telegram_signals.main(["--metrics", str(metrics), "--state", str(state_path)])

    assert code == 0
    assert len(telegram.calls) == 1


# --------------------------------------------------------------------------- #
# Filtre 3: 5'ten fazla sinyalde tek toplu mesaj
# --------------------------------------------------------------------------- #
def test_five_signals_are_sent_one_by_one(tmp_path: Path, telegram: _FakeRequests) -> None:
    signals = [_signal(symbol=f"SYM{index}-USDT-SWAP") for index in range(5)]
    _run(tmp_path, _payload(signals))

    assert len(telegram.calls) == 5


def test_more_than_five_signals_collapse_into_one_batch(tmp_path: Path, telegram: _FakeRequests) -> None:
    signals = [_signal(symbol=f"SYM{index}-USDT-SWAP") for index in range(6)]
    _run(tmp_path, _payload(signals))

    (message,) = _messages(telegram)
    assert "6 yeni sinyal" in message
    for index in range(6):
        assert f"SYM{index}-USDT-SWAP" in message
    # Toplu mesajda da uyarı satırı durur.
    assert "Senin girişin farklı bir fiyattan olacak." in message


def test_batch_message_stays_within_the_telegram_limit(tmp_path: Path, telegram: _FakeRequests) -> None:
    signals = [_signal(symbol=f"SYM{index}-USDT-SWAP") for index in range(40)]
    _run(tmp_path, _payload(signals))

    (message,) = _messages(telegram)
    assert len(message) <= telegram_signals.MAX_MESSAGE_CHARS + 2


# --------------------------------------------------------------------------- #
# Mesaj içeriği
# --------------------------------------------------------------------------- #
def test_reason_is_clipped_to_two_hundred_characters(tmp_path: Path, telegram: _FakeRequests) -> None:
    long_reason = "x" * 400 + " | arm=vwap_pullback"
    _run(tmp_path, _payload([_signal(reason=long_reason)]))

    (message,) = _messages(telegram)
    assert "x" * telegram_signals.REASON_CHARS not in message
    assert "…" in message
    assert "vwap_pullback" in message  # kol etiketi kırpmadan bağımsız okunur


def test_missing_arm_tag_is_shown_as_undefined(tmp_path: Path, telegram: _FakeRequests) -> None:
    """Etiketsiz sinyalde TagError fırlatılmaz: bu bir mesaj satırı, bir kırılım değil."""
    _run(tmp_path, _payload([_signal(reason="etiketsiz gerekçe")]))

    (message,) = _messages(telegram)
    assert "—" in message


def test_undefined_geometry_is_a_dash_not_a_zero(tmp_path: Path, telegram: _FakeRequests) -> None:
    _run(tmp_path, _payload([_signal(target_price=None, reward_risk=None)]))

    (message,) = _messages(telegram)
    assert "R:R —" in message
    assert "R:R 0.00" not in message


def test_model_names_with_underscores_are_not_broken_by_markdown(
    tmp_path: Path, telegram: _FakeRequests
) -> None:
    """HTML parse_mode seçilmesinin gerekçesi: alt çizgi Markdown'da italik açardı."""
    _run(tmp_path, _payload([_signal()]))

    assert telegram.calls[0]["parse_mode"] == "HTML"


# --------------------------------------------------------------------------- #
# Kapılar
# --------------------------------------------------------------------------- #
def test_dry_run_report_is_never_notified(tmp_path: Path, telegram: _FakeRequests) -> None:
    _run(tmp_path, _payload([_signal()], dry_run=True))

    assert telegram.calls == []


def test_report_from_another_layer_is_refused(tmp_path: Path, telegram: _FakeRequests) -> None:
    """4 saatlik katmanın sinyalini scalp bildirimi diye yollamak, iki katmanı karıştırmaktır."""
    code, _ = _run(tmp_path, _payload([_signal()], layer="base"))

    assert code == 0
    assert telegram.calls == []


def test_a_stale_report_is_not_notified(tmp_path: Path, telegram: _FakeRequests) -> None:
    """Tur düşerse depodaki rapor bir önceki turdan kalır; o turun sinyali artık yeni değildir."""
    stale = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    code, _ = _run(tmp_path, _payload([_signal()], generated_at=stale))

    assert code == 0
    assert telegram.calls == []


def test_force_skips_the_freshness_gate(tmp_path: Path, telegram: _FakeRequests) -> None:
    stale = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    _run(tmp_path, _payload([_signal()], generated_at=stale), args=["--force"])

    assert len(telegram.calls) == 1


# --------------------------------------------------------------------------- #
# "Koşuyu asla düşürme" (her yol 0 döner)
# --------------------------------------------------------------------------- #
def test_missing_metrics_file_returns_zero(tmp_path: Path) -> None:
    assert telegram_signals.main(["--metrics", str(tmp_path / "yok.json")]) == 0


def test_corrupt_metrics_file_returns_zero(tmp_path: Path) -> None:
    metrics = tmp_path / "metrics_scalp.json"
    metrics.write_text("{bozuk", encoding="utf-8")

    assert telegram_signals.main(["--metrics", str(metrics)]) == 0


def test_missing_tokens_return_zero_without_sending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    fake = _FakeRequests()
    monkeypatch.setitem(sys.modules, "requests", fake)

    code, state_path = _run(tmp_path, _payload([_signal()]))

    assert code == 0
    assert fake.calls == [] and not state_path.exists()


def test_telegram_error_status_returns_zero(tmp_path: Path, telegram: _FakeRequests) -> None:
    telegram.response = _Response(status_code=429, text="Too Many Requests")

    code, _ = _run(tmp_path, _payload([_signal()]))

    assert code == 0


def test_network_failure_returns_zero(tmp_path: Path, telegram: _FakeRequests) -> None:
    telegram.response = RuntimeError("bağlantı koptu")

    code, _ = _run(tmp_path, _payload([_signal()]))

    assert code == 0


def test_partial_send_failure_still_returns_zero(tmp_path: Path, telegram: _FakeRequests) -> None:
    """Beş mesajın biri gitmese bile tur kırmızıya dönmez."""
    telegram.response = _Response(status_code=400, text="bad request")
    signals = [_signal(symbol=f"SYM{index}-USDT-SWAP") for index in range(3)]

    code, _ = _run(tmp_path, _payload(signals))

    assert code == 0
    assert len(telegram.calls) == 3  # her mesaj denenir, ilk hata kalanları susturmaz


def test_secrets_never_reach_the_logs(
    tmp_path: Path, telegram: _FakeRequests, caplog: pytest.LogCaptureFixture
) -> None:
    telegram.response = _Response(status_code=401, text="Unauthorized")

    with caplog.at_level("DEBUG", logger="telegram_signals"):
        _run(tmp_path, _payload([_signal()]))

    assert "gizli-token" not in caplog.text
    assert "4242" not in caplog.text


def test_only_the_signals_whose_message_was_sent_are_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Yollanamayan mesajın sinyali "bildirildi" sayılmaz: susturma penceresi hiç gitmemiş
    bir bildirimi susturmuş olurdu."""
    class _Flaky(_FakeRequests):
        def post(self, url: str, *, json: dict[str, Any], timeout: float) -> Any:
            self.calls.append(json)
            failing = "SYM1-USDT-SWAP" in json["text"]
            return _Response(status_code=400 if failing else 200)

    fake = _Flaky()
    monkeypatch.setitem(sys.modules, "requests", fake)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "gizli-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "4242")

    signals = [_signal(symbol=f"SYM{index}-USDT-SWAP") for index in range(3)]
    code, state_path = _run(tmp_path, _payload(signals))

    assert code == 0
    sent = json.loads(state_path.read_text(encoding="utf-8"))["sent"]
    assert "vwap_managed|SYM0-USDT-SWAP|long" in sent
    assert "vwap_managed|SYM2-USDT-SWAP|long" in sent
    assert "vwap_managed|SYM1-USDT-SWAP|long" not in sent


# --------------------------------------------------------------------------- #
# Katman ayrımı: scalp BİREBİR aynı, h1 (1 saatlik gözlem katmanı, karar 59) ayrı ayar
# --------------------------------------------------------------------------- #
H1_BAR = timedelta(hours=1)
H1_AS_OF = datetime(2026, 10, 6, 19, 0, tzinfo=timezone.utc)


def _h1_signal(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "bar": H1_AS_OF.isoformat(),
        "fills_at": (H1_AS_OF + H1_BAR).isoformat(),
        "arm": "vwap_revert",
    }
    base.update(overrides)
    arm = base.pop("arm")
    bar = base.pop("bar")
    fills_at = base.pop("fills_at")
    return _signal(
        bar=datetime.fromisoformat(bar), arm=arm, fills_at=fills_at, **base,
    )


def _h1_payload(signals: Sequence[dict[str, Any]] = ()) -> dict[str, Any]:
    return _payload(
        signals,
        layer="h1",
        as_of=H1_AS_OF.isoformat(),
        settings={"timeframe": "1H"},
    )


def _run_h1(
    tmp_path: Path, payload: dict[str, Any], *, args: Sequence[str] = ()
) -> tuple[int, Path]:
    metrics = tmp_path / "metrics_h1.json"
    metrics.write_text(json.dumps(payload), encoding="utf-8")
    state_path = tmp_path / "state" / "telegram_h1.json"
    code = telegram_signals.main(
        ["--layer", "h1", "--metrics", str(metrics), "--state", str(state_path), *args]
    )
    return code, state_path


def test_layer_defaults_pin_the_files_models_and_the_scalp_behaviour() -> None:
    """Katman başına AYRI kaynak, durum ve model kümesi; scalp'in değerleri değişmedi."""
    settings = telegram_signals.LAYER_SETTINGS

    assert settings["scalp"].metrics == Path("docs/data/metrics_scalp.json")
    assert settings["scalp"].state == Path("state/telegram_scalp.json")
    assert settings["scalp"].models == frozenset({"vwap_managed", "vwap_reentry"})
    assert settings["scalp"].merge_models is False
    assert settings["h1"].metrics == Path("docs/data/metrics_h1.json")
    assert settings["h1"].state == Path("state/telegram_h1.json")
    assert settings["h1"].models == frozenset({"vwap_managed", "vwap_reentry"})
    assert settings["h1"].state != settings["scalp"].state
    assert telegram_signals.DEFAULT_LAYER == "scalp"


def test_h1_messages_carry_the_1h_label_the_model_name_and_the_hour_warning(
    tmp_path: Path, telegram: _FakeRequests
) -> None:
    _run_h1(tmp_path, _h1_payload([_h1_signal(model="vwap_reentry", arm="vwap_revert_reentry")]))

    (message,) = _messages(telegram)
    assert "⏱ 1H" in message
    assert "vwap_reentry" in message and "vwap_revert_reentry" in message
    assert "bir sonraki bar açılışından" in message          # zorunlu uyarı aynen
    assert "bir saate kadar" in message                      # 1H'ye özgü ek
    assert "SCALP" not in message


def test_scalp_messages_keep_their_label_and_have_no_1h_text(
    tmp_path: Path, telegram: _FakeRequests
) -> None:
    _run(tmp_path, _payload([_signal(model="vwap_managed")]))

    (message,) = _messages(telegram)
    assert "SCALP SİNYALİ" in message
    assert "1H" not in message and "bir saate kadar" not in message


def test_h1_notifies_only_the_two_vwap_models(tmp_path: Path, telegram: _FakeRequests) -> None:
    signals = [
        _h1_signal(model="scalp_fixed", symbol="ETH-USDT-SWAP"),
        _h1_signal(model="vwap_managed", symbol="BTC-USDT-SWAP"),
        _h1_signal(model="vwap_reentry", symbol="SOL-USDT-SWAP"),
    ]
    _run_h1(tmp_path, _h1_payload(signals))

    text = " ".join(_messages(telegram))
    assert "BTC-USDT-SWAP" in text and "SOL-USDT-SWAP" in text
    assert "ETH-USDT-SWAP" not in text


def test_h1_merges_two_models_on_the_same_bar_and_symbol_into_one_message(
    tmp_path: Path, telegram: _FakeRequests
) -> None:
    signals = [
        _h1_signal(model="vwap_managed", arm="vwap_revert"),
        _h1_signal(model="vwap_reentry", arm="vwap_revert_reentry"),
    ]
    code, state_path = _run_h1(tmp_path, _h1_payload(signals))

    (message,) = _messages(telegram)                      # İKİ sinyal, TEK mesaj
    lines = [line for line in message.splitlines() if line.startswith("•")]
    assert code == 0 and len(lines) == 2
    assert "vwap_managed" in lines[0] and "vwap_reentry" in lines[1]
    # iki sinyal de bildirildi sayılır: susturma durumu İKİ anahtarı da taşır
    sent = json.loads(state_path.read_text(encoding="utf-8"))["sent"]
    assert set(sent) == {
        "vwap_managed|BTC-USDT-SWAP|long", "vwap_reentry|BTC-USDT-SWAP|long",
    }


def test_h1_does_not_merge_different_symbols(tmp_path: Path, telegram: _FakeRequests) -> None:
    signals = [
        _h1_signal(model="vwap_managed", symbol="BTC-USDT-SWAP"),
        _h1_signal(model="vwap_reentry", symbol="ETH-USDT-SWAP"),
    ]
    _run_h1(tmp_path, _h1_payload(signals))

    assert len(_messages(telegram)) == 2


def test_scalp_never_merges_models_on_the_same_symbol(
    tmp_path: Path, telegram: _FakeRequests
) -> None:
    """Scalp davranışı birebir aynı kalır: birleştirme yalnızca h1'e özgüdür."""
    signals = [_signal(model="vwap_managed"), _signal(model="vwap_reentry")]
    _run(tmp_path, _payload(signals))

    assert len(_messages(telegram)) == 2


def test_a_report_of_another_layer_is_never_sent_under_this_layer(
    tmp_path: Path, telegram: _FakeRequests
) -> None:
    """H1 raporunu `--layer scalp` ile okumak (ya da tersi) bildirim üretmez."""
    metrics = tmp_path / "metrics_h1.json"
    metrics.write_text(json.dumps(_h1_payload([_h1_signal()])), encoding="utf-8")

    code = telegram_signals.main(
        ["--layer", "scalp", "--metrics", str(metrics), "--state", str(tmp_path / "s.json")]
    )

    assert code == 0 and telegram.calls == []


def test_unknown_layer_is_a_quiet_zero(tmp_path: Path, telegram: _FakeRequests) -> None:
    assert telegram_signals.main(["--layer", "nope"]) == 0
    assert telegram.calls == []


def test_h1_dry_run_prints_the_message_and_makes_no_network_call(
    tmp_path: Path, telegram: _FakeRequests, capsys: pytest.CaptureFixture[str]
) -> None:
    signals = [
        _h1_signal(model="vwap_managed", arm="vwap_revert"),
        _h1_signal(model="vwap_reentry", arm="vwap_revert_reentry"),
    ]
    code, state_path = _run_h1(tmp_path, _h1_payload(signals), args=["--dry-run"])

    out = capsys.readouterr().out
    assert code == 0 and telegram.calls == []
    assert "⏱ 1H" in out and "vwap_managed" in out and "vwap_reentry" in out
    assert not state_path.exists()          # yollanmamış mesaj "bildirildi" sayılmaz
