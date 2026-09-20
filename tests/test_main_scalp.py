"""main.py'nin scalp katmanını uçtan uca koşması ve iki katmanın birbirine karışmaması.

Ölçülen şey veri çekme değil (borsa sahte), turun SIRASI: katman çözüldü mü, sabit evren
istendi mi, defter ve rapor katmanın kendi yoluna mı yazıldı, kırılımlar ve saklama
penceresi katmandan mı geldi.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

import main as main_module
from core.config import load_config
from core.layers import resolve_layer
from core.ledger import Ledger

# Sıkıştırma modele bakmaz (her model için birebir aynı uygulanır); testin bir ADA
# ihtiyacı var, hangi ad olduğuna değil. Katmandan okumak, kadro değiştiğinde testin
# sessizce yanlış modeli aramasını engeller.
COMPACTION_MODEL = "scalp_fixed"

SYMBOLS = ("BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP")
START = pd.Timestamp("2026-03-02 00:00:00", tz="UTC")


def _market(bars: int = 60) -> Any:
    from strategies.base import MarketData

    index = pd.date_range(START, periods=bars, freq="15min", tz="UTC", name="ts")
    frame = pd.DataFrame(
        {"open": 100.0, "high": 100.6, "low": 99.4, "close": 100.0, "volume": 100.0},
        index=index,
    )
    return MarketData(
        ohlcv={symbol: frame for symbol in SYMBOLS},
        btc=frame,
        funding={},
        as_of=index[-1],
    )


class Sandbox:
    def __init__(self, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.root = root
        self.ledgers = root / "ledgers_scalp"
        self.metrics_path = root / "docs" / "data" / "metrics_scalp.json"
        self.requested: list[list[str] | None] = []
        real_resolve = main_module.resolve_layer

        def _resolve(config: Any, name: str = "base") -> Any:
            return replace(
                real_resolve(config, name),
                ledger_root=self.ledgers,
                metrics_path=self.metrics_path,
            )

        def _load(config: Any, symbols: Any = None) -> Any:
            self.requested.append(None if symbols is None else list(symbols))
            return _market()

        monkeypatch.setattr(main_module, "resolve_layer", _resolve)
        monkeypatch.setattr(main_module, "load_market_data", _load)

    def metrics(self) -> dict[str, Any]:
        return json.loads(self.metrics_path.read_text(encoding="utf-8"))


@pytest.fixture()
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Sandbox:
    return Sandbox(tmp_path, monkeypatch)


def test_scalp_layer_runs_every_model(sandbox: Sandbox) -> None:
    assert main_module.main(["--layer", "scalp"]) == 0

    payload = sandbox.metrics()
    assert payload["layer"] == "scalp"
    assert payload["settings"]["timeframe"] == "15m"
    assert [model["name"] for model in payload["models"]] == resolve_layer(
        load_config(), "scalp"
    ).models, "katmandaki her model koşmalı; sessizce düşen model ölçümü eksiltir"
    for name in resolve_layer(load_config(), "scalp").models:
        assert (sandbox.ledgers / name / "positions.json").is_file(), name


def test_scalp_payload_separates_the_replica(sandbox: Sandbox) -> None:
    """Kopya model (13) yarışmacı değildir: sayfa onu ayrı bölümde çizebilmeli.

    Yük hem `replicas` listesini hem satırın kendi `is_replica` bayrağını taşır; sayfanın
    "bu satır sıralamaya girer mi" sorusunu ada bakarak tahmin etmesi gerekmez.
    """
    main_module.main(["--layer", "scalp"])

    payload = sandbox.metrics()
    assert payload["replicas"] == ["vwap_clone"]
    by_name = {model["name"]: model for model in payload["models"]}
    assert by_name["vwap_clone"]["is_replica"] is True
    assert by_name["vwap_managed"]["is_replica"] is False
    # Maliyet ölçeği kolonları kopyada koşulsuz nan'dır (JSON'da null).
    assert by_name["vwap_clone"]["total"]["cost_per_r"] is None
    assert by_name["vwap_clone"]["total"]["avg_stop_distance_pct"] is None
    # Kabul çıtası yalnızca yarışmacılara uygulanır.
    flagged = {item["model"] for item in payload["acceptance"]["models"]}
    assert "vwap_clone" not in flagged


def test_scalp_round_report_always_has_the_emitted_slot(sandbox: Sandbox) -> None:
    """Alan sinyal üretilmeyen turda da durur: bildirim onu okuyamazsa (KeyError yerine)
    sessizce "hiç sinyal yok" derdi ve gerçek bir arıza sessiz kalırdı."""
    main_module.main(["--layer", "scalp"])

    models = sandbox.metrics()["round"]["models"]
    assert models and all(isinstance(model["emitted"], list) for model in models)


def test_scalp_layer_requests_the_fixed_universe(sandbox: Sandbox) -> None:
    """Sabit evren katmandan gelir: hacimden otomatik seçim scalp'te YOK."""
    main_module.main(["--layer", "scalp"])

    assert sandbox.requested and sandbox.requested[0] is not None
    assert len(sandbox.requested[0]) == 13
    assert "PENGU-USDT-SWAP" in sandbox.requested[0]


def test_base_layer_still_asks_for_the_computed_universe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """4 saatlik katman bu eklemeden etkilenmez: evreni yine hacimden hesaplanır."""
    box = Sandbox(tmp_path, monkeypatch)

    main_module.main([])

    assert box.requested == [None]


def test_scalp_payload_carries_the_layer_breakdowns(sandbox: Sandbox) -> None:
    """Kol ve sembol kırılımı katmanın rapor sözleşmesidir; sayfa onları buradan okur."""
    main_module.main(["--layer", "scalp"])

    breakdowns = sandbox.metrics()["breakdowns"]

    assert set(breakdowns) == {"arm", "symbol", "exit_rule", "session", "loss_streak"}
    assert set(breakdowns["arm"]) == set(resolve_layer(load_config(), "scalp").models)


def test_scalp_settings_report_the_layer_ceiling(sandbox: Sandbox) -> None:
    """Stop tavanı scalp'te 8×ATR'dir: yükün "hangi varsayımlarla ölçüldü" bölümü bunu söylemeli."""
    main_module.main(["--layer", "scalp"])

    assert sandbox.metrics()["settings"]["max_stop_atr_multiple"] == 8.0


def test_equity_compaction_runs_for_the_scalp_layer(sandbox: Sandbox) -> None:
    """30 günden eski özsermaye satırları günlük özete iner: depo geçmişi şişmesin."""
    ledger = Ledger(sandbox.ledgers)
    ledger.reset_model(COMPACTION_MODEL, initial_capital=10_000.0)
    old = [
        {
            "ts": (START - pd.Timedelta(days=60) + pd.Timedelta(minutes=15 * i)).isoformat(),
            "cash": 10_000,
            "margin_used": 0,
            "unrealized_pnl": 0,
            "equity": 10_000,
            "open_positions": 0,
        }
        for i in range(96)  # tek bir günün tüm barları
    ]
    ledger.append_equity(COMPACTION_MODEL, old)

    main_module.main(["--layer", "scalp"])

    stale = [
        row
        for row in ledger.read_equity(COMPACTION_MODEL)
        if row["ts"] < (START - pd.Timedelta(days=30)).isoformat()
    ]
    assert len(stale) == 1  # 96 bar -> 1 günlük özet


def test_base_layer_never_compacts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """4 saatlik katmanda sıkıştırma kapalıdır (`null`): eğri tam çözünürlükte kalır."""
    box = Sandbox(tmp_path, monkeypatch)
    ledger = Ledger(box.ledgers)
    ledger.reset_model("buyhold", initial_capital=10_000.0)
    rows = [
        {
            "ts": (START - pd.Timedelta(days=200) + pd.Timedelta(hours=4 * i)).isoformat(),
            "cash": 10_000,
            "margin_used": 0,
            "unrealized_pnl": 0,
            "equity": 10_000,
            "open_positions": 0,
        }
        for i in range(6)
    ]
    ledger.append_equity("buyhold", rows)

    main_module.main([])

    assert len([row for row in ledger.read_equity("buyhold") if row["ts"] < START.isoformat()]) == 6


def test_dry_run_writes_nothing_in_the_scalp_layer(sandbox: Sandbox) -> None:
    assert main_module.main(["--layer", "scalp", "--dry-run"]) == 0

    assert not sandbox.metrics_path.exists()
    assert not sandbox.ledgers.exists() or list(sandbox.ledgers.iterdir()) == []


def test_unknown_layer_fails_loudly(sandbox: Sandbox) -> None:
    """Sessizce base'e düşmek, scalp turunun 4 saatlik defteri ezmesi demekti."""
    from core.config import ConfigError

    with pytest.raises(ConfigError):
        main_module.main(["--layer", "yok"])
