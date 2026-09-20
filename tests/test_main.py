"""main.py: turun uçtan uca akışı, hata izolasyonu ve --dry-run'ın yazmadığı.

Bu dosya canlı borsaya bağlanmaz: `load_market_data` sahte bir anlık görüntüyle
değiştirilir. Ölçülen şey veri çekme değil, main.py'nin SIRASI — tur koştu mu, metrikler
defterden mi üretildi, dry-run gerçekten hiçbir şey yazmadı mı.
"""

from __future__ import annotations

import json
import logging
from dataclasses import replace
from pathlib import Path
from typing import Any, Sequence

import pandas as pd
import pytest

import main as main_module
from core.config import load_config
from core.ledger import Ledger
from strategies.base import MarketData, Signal, Strategy

SYMBOLS = ("BTC-USDT-SWAP", "ETH-USDT-SWAP")
START = pd.Timestamp("2026-01-01 00:00:00", tz="UTC")

_real_build = main_module.build  # monkeypatch'lenmeden önceki hâli


def _market(bars: int = 30, *, last_close: float = 100.0) -> MarketData:
    index = pd.date_range(START, periods=bars, freq="4h", tz="UTC", name="ts")
    frame = pd.DataFrame(
        {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "volume": 1.0},
        index=index,
    )
    frame.iloc[-1, frame.columns.get_loc("close")] = last_close
    return MarketData(
        ohlcv={symbol: frame for symbol in SYMBOLS},
        btc=frame,
        funding={},
        as_of=index[-1],
    )


class Sandbox:
    """İzole bir koşu ortamı: tmp defter + sahte borsa. `bars` turun "şimdi"sini ilerletir.

    İzolasyon KATMANIN üzerinden kurulur (core/layers.py): gerçek katman çözülür, yalnızca
    defter kökü ve rapor dosyası tmp dizine çevrilir. Yolları main.py'nin içinden
    yamalamak, katmanın yolu nereden okuduğunu testin varsaymasını gerektirirdi.
    """

    def __init__(self, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.root = root
        self.ledgers = root / "ledgers"
        self.ledgers.mkdir()
        self.metrics_path = root / "docs" / "data" / "metrics.json"
        self._bars = 30

        real_resolve = main_module.resolve_layer

        def _resolve(config: Any, name: str = "base") -> Any:
            return replace(
                real_resolve(config, name),
                ledger_root=self.ledgers,
                metrics_path=self.metrics_path,
            )

        monkeypatch.setattr(main_module, "resolve_layer", _resolve)
        monkeypatch.setattr(
            main_module,
            "load_market_data",
            lambda config, symbols=None: _market(bars=self._bars),
        )

    def advance_to(self, bars: int) -> None:
        """Anlık görüntüyü `bars` barlık yap: bir sonraki tur yeni bir `as_of` görür."""
        self._bars = bars

    @property
    def ledger(self) -> Ledger:
        return Ledger(self.ledgers)

    def metrics(self) -> dict[str, Any]:
        return json.loads(self.metrics_path.read_text(encoding="utf-8"))


@pytest.fixture()
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Sandbox:
    return Sandbox(tmp_path, monkeypatch)


# --------------------------------------------------------------------------- #
# Normal koşu
# --------------------------------------------------------------------------- #
def test_run_writes_ledger_and_metrics(sandbox: Sandbox) -> None:
    assert main_module.main([]) == 0

    payload = sandbox.metrics()
    assert payload["as_of"] == _market().as_of.isoformat()
    assert payload["dry_run"] is False
    assert payload["benchmarks"] == ["buyhold"]
    # Literal bir liste yerine config: yarışan küme büyüdükçe (10 model hedefleniyor) bu
    # testin ölçtüğü şey "hangi modeller var" değil, "main config'in TAMAMINI koşturdu mu"dur.
    assert [model["name"] for model in payload["models"]] == load_config()["models"]
    assert payload["settings"]["fee_rate"] == load_config()["fee_rate"]
    assert (sandbox.ledgers / "buyhold" / "positions.json").is_file()


def test_metrics_json_is_valid_json_with_null_not_nan(sandbox: Sandbox) -> None:
    """`json.dumps` nan'ı `NaN` yazar ve dosya GEÇERSİZ JSON olur; sayfa onu okuyamaz."""
    main_module.main([])
    text = sandbox.metrics_path.read_text(encoding="utf-8")
    assert "NaN" not in text
    payload = json.loads(text)  # katı parser: nan görse patlardı
    total = payload["models"][0]["total"]
    assert total["cost_per_r"] is None
    assert total["avg_stop_distance_pct"] is None


def test_metrics_json_carries_the_emitted_signals(sandbox: Sandbox) -> None:
    """Anlık bildirim (scripts/telegram_signals.py) sinyalleri YALNIZCA buradan okur.

    Defterde cevabı yoktur: sinyal bir sonraki barın açılışında dolar (kural 13) ve ancak
    kapandığında `trades.csv`'ye yazılır — yani haber değeri olduğu an hiçbir satırı yok.
    """
    main_module.main([])

    payload = sandbox.metrics()
    rows = {model["model"]: model for model in payload["round"]["models"]}
    emitted = rows["buyhold"]["emitted"]
    assert {item["symbol"] for item in emitted} == set(SYMBOLS)
    assert all(item["bar"] == payload["as_of"] for item in emitted)
    assert all(item["fills_at"] > item["bar"] for item in emitted)  # kural 13
    # Stop'suz referans sinyali (kural 15): geometri ölçülemez, null olur — 0.0 DEĞİL.
    assert all(item["stop_price"] is None for item in emitted)
    assert all(item["reward_risk"] is None for item in emitted)


def test_metrics_json_carries_the_dashboard_sections(sandbox: Sandbox) -> None:
    """Sayfa statiktir ve defteri okuyamaz: ihtiyacı olan her şey bu dosyada olmalı."""
    main_module.main([])
    payload = sandbox.metrics()
    for section in ("pooled", "acceptance", "correlation", "equity",
                    "open_positions", "recent_trades", "model_trades", "activity"):
        assert section in payload, section
    assert payload["pooled"]["models"] == [
        name for name in load_config()["models"] if name != "buyhold"
    ]
    assert set(payload["equity"]) == set(load_config()["models"])


def test_dashboard_sections_are_also_nan_free(sandbox: Sandbox) -> None:
    """Tanımsız metrik yalnızca model tablosunda değil, her bölümde null olmalı."""
    main_module.main([])
    text = sandbox.metrics_path.read_text(encoding="utf-8")
    assert "NaN" not in text
    payload = json.loads(text)
    assert payload["pooled"]["directions"]["short"]["avg_r"] is None


def test_dry_run_does_not_write_the_dashboard_sections(sandbox: Sandbox) -> None:
    """Sayfa defterin türevidir: kalıcı olmayan bir turdan üretilmiş hâli ikisini ayrıştırır."""
    main_module.main(["--dry-run"])
    assert not sandbox.metrics_path.exists()


def test_benchmark_opens_once_then_holds(sandbox: Sandbox) -> None:
    """İlk tur sinyali kuyruğa alır, ikinci tur doldurur, sonraki turlar hiç işlem açmaz."""
    main_module.main([])
    # Kural 13: sinyal üretildiği barda dolmaz, bir sonraki barın açılışında dolar.
    assert sandbox.ledger.load_state("buyhold")["positions"] == []

    sandbox.advance_to(31)
    main_module.main([])
    held = sandbox.ledger.load_state("buyhold")["positions"]
    assert {position["symbol"] for position in held} == set(SYMBOLS)
    assert all(position["stop_price"] is None for position in held)

    sandbox.advance_to(32)
    main_module.main([])
    sandbox.advance_to(33)
    main_module.main([])

    still_held = sandbox.ledger.load_state("buyhold")["positions"]
    assert len(still_held) == 2
    # Alıp tutuyor: hiçbir işlem kapanmadı, çift pozisyon açılmadı.
    assert sandbox.ledger.read_trades("buyhold") == []


def test_benchmark_splits_capital_at_1x(sandbox: Sandbox) -> None:
    main_module.main([])
    sandbox.advance_to(31)
    main_module.main([])

    positions = sandbox.ledger.load_state("buyhold")["positions"]
    initial_capital = float(load_config()["initial_capital"])
    assert len(positions) == 2
    for position in positions:
        assert position["leverage"] == pytest.approx(1.0)
        # Kaldıraç 1x: marj notional'ın tamamıdır
        assert position["margin"] == pytest.approx(position["qty"] * position["entry_price"])
    # Çıpa kaldıraçsızdır: iki pozisyonun toplam marjı sermayeyi aşmaz.
    assert sum(position["margin"] for position in positions) <= initial_capital


def test_benchmark_r_columns_stay_undefined_after_filling(sandbox: Sandbox) -> None:
    """Pozisyon açıldıktan sonra bile R kolonları nan kalır: stop yok, payda yok."""
    main_module.main([])
    sandbox.advance_to(31)
    main_module.main([])

    total = sandbox.metrics()["models"][0]["total"]
    assert total["avg_r"] is None
    assert total["cost_per_r"] is None
    assert sandbox.metrics()["models"][0]["is_benchmark"] is True


# --------------------------------------------------------------------------- #
# --dry-run
# --------------------------------------------------------------------------- #
def test_dry_run_writes_nothing(sandbox: Sandbox, capsys: pytest.CaptureFixture[str]) -> None:
    assert main_module.main(["--dry-run"]) == 0

    assert not sandbox.metrics_path.exists()
    assert list(sandbox.ledgers.iterdir()) == []
    assert "buyhold" in capsys.readouterr().out  # rapor yine de basıldı


def test_dry_run_does_not_advance_the_real_ledger(sandbox: Sandbox) -> None:
    """Dry-run gerçek defteri okur (rapor birikimi göstersin) ama ilerletmez."""
    main_module.main([])
    state_path = sandbox.ledgers / "buyhold" / "positions.json"
    before = state_path.read_text(encoding="utf-8")

    sandbox.advance_to(31)
    assert main_module.main(["--dry-run"]) == 0

    assert state_path.read_text(encoding="utf-8") == before


# --------------------------------------------------------------------------- #
# Hata izolasyonu
# --------------------------------------------------------------------------- #
def test_unknown_model_is_skipped_but_run_fails_loudly(
    sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tanınmayan model sessizce düşmez: diğerleri koşar, çıkış kodu 1 olur (kural 6)."""
    config = load_config()
    config["models"] = ["buyhold", "yok_boyle_bir_model"]
    monkeypatch.setattr(main_module, "load_config", lambda path=None: config)

    assert main_module.main([]) == 1

    payload = sandbox.metrics()
    assert "yok_boyle_bir_model" in payload["build_failures"]
    assert [model["name"] for model in payload["models"]] == ["buyhold"]


def test_broken_model_does_not_stop_the_round(
    sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sinyal üretiminde patlayan model yalnızca kendi turunu kaybeder (kural 8)."""

    class _Broken(Strategy):
        name = "bozuk"
        allowed_directions = ["long"]

        def generate_signals(
            self, market: MarketData, peer_signals: Any = None
        ) -> list[Signal]:
            raise RuntimeError("model içi hata")

    config = load_config()
    config["models"] = ["buyhold", "bozuk"]
    monkeypatch.setattr(main_module, "load_config", lambda path=None: config)
    monkeypatch.setattr(
        main_module,
        "build",
        lambda name, config=None: _Broken() if name == "bozuk" else _real_build(name, config=config),
    )

    assert main_module.main([]) == 0  # kurulum başarılı, koşu sürdü

    reports = {item["model"]: item for item in sandbox.metrics()["round"]["models"]}
    assert "model içi hata" in reports["bozuk"]["skipped"]
    assert reports["buyhold"]["signals"] == 2  # diğer model etkilenmedi


def test_empty_model_list_fails(sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch) -> None:
    config = load_config()
    config["models"] = []
    monkeypatch.setattr(main_module, "load_config", lambda path=None: config)
    assert main_module.main([]) == 1


# --------------------------------------------------------------------------- #
# Ret dökümü: "sinyal var, işlem yok" tek bir görünüme çökmemeli
# --------------------------------------------------------------------------- #
def test_holding_rounds_report_duplicate_position_not_silence(sandbox: Sandbox) -> None:
    """Çıpanın her turu signals=2/filled=0'dır; sebebi raporda AÇIKÇA durmalı.

    Aksi hâlde aylar sonra gerçek bir boyutlandırma arızası da tam bu şekilde görünür.
    """
    main_module.main([])
    sandbox.advance_to(31)
    main_module.main([])  # dolum burada

    sandbox.advance_to(32)
    main_module.main([])

    report = {item["model"]: item for item in sandbox.metrics()["round"]["models"]}["buyhold"]
    assert report["signals"] == 2
    assert report["filled"] == 0
    # Sessizlik değil, kategorili gerekçe:
    assert report["rejections"] == {"duplicate_position": 2}


class _FixedStop(Strategy):
    """Stop'lu, sıradan bir yarışmacı: boyutu core/portfolio.py'nin kuralı belirler."""

    name = "kobay"
    allowed_directions = ["long"]

    def generate_signals(self, market: MarketData, peer_signals: Any = None) -> list[Signal]:
        return [
            Signal(symbol=SYMBOLS[0], direction="long", stop_price=95.0, reason="kobay")
        ]


def _run_with_broken_sizing(monkeypatch: pytest.MonkeyPatch) -> None:
    """risk_per_trade=0 -> boyut sıfır: tam da sessizce kaybolabilecek türden bir arıza."""
    config = load_config()
    config["models"] = ["kobay"]
    config["risk_per_trade"] = 0.0
    monkeypatch.setattr(main_module, "load_config", lambda path=None: config)
    monkeypatch.setattr(main_module, "build", lambda name, config=None: _FixedStop())


def test_sizing_failure_reports_a_different_code(
    sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gerçek arıza, beklenen tekrardan FARKLI bir kodla görünür — ayırt edilebilirlik testi."""
    _run_with_broken_sizing(monkeypatch)

    main_module.main([])
    sandbox.advance_to(31)
    main_module.main([])

    report = {item["model"]: item for item in sandbox.metrics()["round"]["models"]}["kobay"]
    assert report["signals"] == 1
    assert report["filled"] == 0
    # Çıpanın beklenen tekrarıyla aynı hücreye düşmüyor:
    assert report["rejections"] == {"zero_size": 1}
    assert "duplicate_position" not in report["rejections"]


def test_queued_first_round_has_no_rejections(sandbox: Sandbox) -> None:
    """İlk tur da signals=2/filled=0'dır ama sebebi ret değil, kural 13'ün kuyruğudur."""
    main_module.main([])
    report = {item["model"]: item for item in sandbox.metrics()["round"]["models"]}["buyhold"]
    assert report["signals"] == 2
    assert report["filled"] == 0
    assert report["rejections"] == {}


def test_expected_repeat_logs_at_info(
    sandbox: Sandbox, caplog: pytest.LogCaptureFixture
) -> None:
    """Çıpanın her turki tekrarı INFO: beklenen bir durum logu kırmızıya boğmamalı."""
    main_module.main([])
    sandbox.advance_to(31)
    main_module.main([])
    sandbox.advance_to(32)

    with caplog.at_level(logging.INFO, logger="core.engine"):
        main_module.main([])

    records = [r for r in caplog.records if "duplicate_position" in r.getMessage()]
    assert records, "beklenen tekrar hiç loglanmadı"
    assert all(record.levelno == logging.INFO for record in records)


def test_sizing_failure_logs_at_warning(
    sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Boyutlandırma arızası WARNING: logu gözle tarayan da ikisini ayırabilsin."""
    _run_with_broken_sizing(monkeypatch)
    main_module.main([])
    sandbox.advance_to(31)

    with caplog.at_level(logging.INFO, logger="core.engine"):
        main_module.main([])

    records = [r for r in caplog.records if "zero_size" in r.getMessage()]
    assert records, "boyutlandırma arızası hiç loglanmadı"
    assert all(record.levelno == logging.WARNING for record in records)
