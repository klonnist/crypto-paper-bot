"""core/engine.py: dolum sırası, iki geçişli tur, izolasyon ve defter kalıcılığı.

Buradaki testler zamanlama kurallarını çiviler: sinyal ÜRETİLDİĞİ barda dolmaz (kural 13),
meta modeller yalnızca normal modellerin sinyallerini görür (kural 4), bir modelin hatası
turu düşürmez (kural 8).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Mapping, Sequence

import pandas as pd
import pytest

from core.config import load_config
from core.engine import Engine, PendingOrder, average_true_range
from core.ledger import Ledger
from strategies.base import (
    ExitInstruction,
    MarketData,
    Position,
    Signal,
    Strategy,
    TakeProfit,
)

SYMBOL = "BTC-USDT-SWAP"
START = pd.Timestamp("2026-01-01 00:00:00", tz="UTC")


# --------------------------------------------------------------------------- #
# Test yardımcıları
# --------------------------------------------------------------------------- #
def _frame(rows: Sequence[tuple[float, float, float, float]]) -> pd.DataFrame:
    index = pd.date_range(START, periods=len(rows), freq="4h", tz="UTC", name="ts")
    frame = pd.DataFrame(list(rows), index=index, columns=["open", "high", "low", "close"])
    frame["volume"] = 1.0
    return frame


def _market(
    rows: Sequence[tuple[float, float, float, float]],
    *,
    bars: int | None = None,
    funding: Mapping[str, pd.Series] | None = None,
) -> MarketData:
    frame = _frame(rows)
    if bars is not None:
        frame = frame.head(bars)
    return MarketData(
        ohlcv={SYMBOL: frame},
        btc=frame,
        funding=dict(funding or {}),
        as_of=frame.index[-1],
    )


def _config(**overrides: Any) -> dict[str, Any]:
    config = load_config()
    config.update(fee_rate=0.0, slippage_base=0.0, slippage_short_stop=0.0)
    config.update(overrides)
    return config


class _Scripted(Strategy):
    """Barına göre önceden yazılmış sinyal/çıkış üreten sahte model."""

    is_meta = False

    def __init__(
        self,
        name: str,
        *,
        allowed_directions: Sequence[str] = ("long", "short"),
        signals: Mapping[pd.Timestamp, Sequence[Signal]] | None = None,
        exits: Mapping[pd.Timestamp, Sequence[ExitInstruction]] | None = None,
    ) -> None:
        self.name = name
        self.allowed_directions = list(allowed_directions)  # type: ignore[assignment]
        self._signals = dict(signals or {})
        self._exits = dict(exits or {})
        self.seen_positions: list[list[Position]] = []

    def generate_signals(
        self, market: MarketData, peer_signals: Mapping[str, tuple[Signal, ...]] | None = None
    ) -> list[Signal]:
        assert peer_signals is None  # normal modeller akran sinyali görmez (kural 4)
        return list(self._signals.get(market.as_of, ()))

    def manage_positions(
        self, market: MarketData, positions: list[Position]
    ) -> list[ExitInstruction]:
        self.seen_positions.append(list(positions))
        return list(self._exits.get(market.as_of, ()))


class _Meta(Strategy):
    is_meta = True

    def __init__(self, name: str) -> None:
        self.name = name
        self.allowed_directions = ["long", "short"]
        self.peers: Mapping[str, tuple[Signal, ...]] | None = None

    def generate_signals(
        self, market: MarketData, peer_signals: Mapping[str, tuple[Signal, ...]] | None = None
    ) -> list[Signal]:
        self.peers = peer_signals
        return []


class _Broken(Strategy):
    name = "broken"
    allowed_directions = ["long", "short"]

    def generate_signals(
        self, market: MarketData, peer_signals: Mapping[str, tuple[Signal, ...]] | None = None
    ) -> list[Signal]:
        raise RuntimeError("model çöktü")


def _long_signal(stop: float = 95.0, **overrides: Any) -> Signal:
    return Signal(symbol=SYMBOL, direction="long", stop_price=stop, reason="test", **overrides)


ROWS = [
    (100.0, 101.0, 99.0, 100.0),   # bar0 00:00 — sinyal burada üretilir
    (102.0, 103.0, 101.0, 102.0),  # bar1 04:00 — dolum burada
    (102.0, 104.0, 101.5, 103.5),  # bar2 08:00
]

# --------------------------------------------------------------------------- #
# Dolum kuralı (kural 13)
# --------------------------------------------------------------------------- #
def test_signal_does_not_fill_on_the_bar_it_was_generated(tmp_path: Path) -> None:
    strategy = _Scripted("m", signals={START: [_long_signal()]})
    engine = Engine([strategy], config=_config(), ledger=Ledger(tmp_path))

    report = engine.run_round(_market(ROWS, bars=1))

    state = Ledger(tmp_path).load_state("m")
    assert state is not None
    assert state["positions"] == []
    assert len(state["pending_orders"]) == 1
    assert state["last_processed_bar"] == START.isoformat()
    assert report.by_model("m").signals == 1  # type: ignore[union-attr]


def test_pending_order_fills_at_the_next_bar_open(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path)
    strategy = _Scripted("m", signals={START: [_long_signal()]})
    engine = Engine([strategy], config=_config(), ledger=ledger)

    engine.run_round(_market(ROWS, bars=1))
    report = engine.run_round(_market(ROWS, bars=2))

    state = ledger.load_state("m")
    assert state is not None
    (position,) = state["positions"]
    assert position["entry_price"] == pytest.approx(102.0)  # bar1'in AÇILIŞI
    assert position["opened_at"] == pd.Timestamp("2026-01-01 04:00", tz="UTC").isoformat()
    assert state["pending_orders"] == []
    assert report.by_model("m").filled == 1  # type: ignore[union-attr]


def test_fill_price_carries_fee_and_slippage(tmp_path: Path) -> None:
    config = load_config()  # gerçek maliyetler
    strategy = _Scripted("m", signals={START: [_long_signal()]})
    engine = Engine([strategy], config=config, ledger=Ledger(tmp_path))

    engine.run_round(_market(ROWS, bars=1))
    engine.run_round(_market(ROWS, bars=2))

    state = Ledger(tmp_path).load_state("m")
    assert state is not None
    (position,) = state["positions"]
    entry = 102.0 * (1 + config["slippage_base"])
    assert position["entry_price"] == pytest.approx(entry)
    notional = position["qty"] * entry
    assert state["cash"] == pytest.approx(
        config["initial_capital"] - position["margin"] - notional * config["fee_rate"]
    )


def test_state_survives_a_fresh_engine_instance(tmp_path: Path) -> None:
    """Cron'la çalışan sistemde tur bir süreç, defter tek hafızadır."""
    signals = {START: [_long_signal()]}
    Engine([_Scripted("m", signals=signals)], config=_config(), ledger=Ledger(tmp_path)).run_round(
        _market(ROWS, bars=1)
    )
    Engine([_Scripted("m", signals=signals)], config=_config(), ledger=Ledger(tmp_path)).run_round(
        _market(ROWS, bars=2)
    )

    state = Ledger(tmp_path).load_state("m")
    assert state is not None and len(state["positions"]) == 1


def test_bars_are_processed_once(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path)
    engine = Engine([_Scripted("m")], config=_config(), ledger=ledger)
    engine.run_round(_market(ROWS, bars=2))
    engine.run_round(_market(ROWS, bars=2))  # aynı as_of ile tekrar
    engine.run_round(_market(ROWS, bars=3))

    timestamps = [row["ts"] for row in ledger.read_equity("m")]
    assert timestamps == sorted(set(timestamps))
    assert len(timestamps) == 2  # ilk tur yalnızca as_of'u, ikinci tur yeni barı işler


# --------------------------------------------------------------------------- #
# Kaçırılan turların telafisi (cron gecikmesi/atlaması)
# --------------------------------------------------------------------------- #
# GitHub cron'u garantili değildir: 15 dakikalık kadansta tetikleme gecikebilir ya da
# tamamen atlanabilir. Motor yalnızca son bara atlasaydı, atlanan barlardaki stop/TP hiç
# sorulmaz ve pozisyon ölçümde haksız yere hayatta kalırdı.
GAP_ROWS = [
    (100.0, 101.0, 99.0, 100.0),   # bar0 00:00 — sinyal burada üretilir
    (100.0, 101.0, 99.5, 100.5),   # bar1 04:00 — dolum burada
    (100.0, 101.0, 90.0, 100.0),   # bar2 08:00 — KAÇIRILAN tur; stop (95) burada vurulur
    (100.0, 101.0, 99.0, 100.0),   # bar3 12:00 — koşunun geri döndüğü bar
]
GAP_ROWS_INDEX = pd.date_range(START, periods=len(GAP_ROWS), freq="4h", tz="UTC")


def test_missed_round_backfills_every_bar_in_order(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path)
    engine = Engine(
        [_Scripted("m", signals={START: [_long_signal(stop=95.0)]})],
        config=_config(),
        ledger=ledger,
    )
    engine.run_round(_market(GAP_ROWS, bars=1))
    engine.run_round(_market(GAP_ROWS, bars=2))  # dolum @100
    report = engine.run_round(_market(GAP_ROWS, bars=4))  # bar2'nin turu ATLANDI

    assert report.by_model("m").bars_processed == 2  # type: ignore[union-attr]
    (trade,) = ledger.read_trades("m")
    # Stop, koşunun geri döndüğü barda değil, ATLANAN barda ve o barın fiyatından tetiklenir.
    assert trade["exit_reason"] == "stop"
    assert float(trade["exit_price"]) == pytest.approx(95.0)
    assert trade["closed_at"] == GAP_ROWS_INDEX[2].isoformat()
    assert [row["ts"] for row in ledger.read_equity("m")] == [
        ts.isoformat() for ts in GAP_ROWS_INDEX
    ]


def test_pending_order_survives_a_missed_round_and_fills_at_its_own_bar(tmp_path: Path) -> None:
    """Kuyruktaki emir atlanan turda kaybolmaz; kural 13'ün barında dolar, `as_of`ta değil."""
    ledger = Ledger(tmp_path)
    engine = Engine(
        [_Scripted("m", signals={START: [_long_signal(stop=95.0)]})],
        config=_config(),
        ledger=ledger,
    )
    engine.run_round(_market(GAP_ROWS, bars=1))  # emir kuyruğa girer
    engine.run_round(_market(GAP_ROWS, bars=4))  # bar1 ve bar2'nin turları ATLANDI

    (trade,) = ledger.read_trades("m")
    assert float(trade["entry_price"]) == pytest.approx(100.0)  # bar1'in açılışı
    assert trade["opened_at"] == GAP_ROWS_INDEX[1].isoformat()


def test_bar_missing_from_the_snapshot_is_counted_and_warned(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Telafi ancak bar elimizdeyse mümkündür; olmayan bar SESSİZ atlanamaz.

    Çıpanın penceresi son işlenmiş bara kadar geri gitmiyorsa (kesinti data.history_bars'ı
    aşmış ya da seride delik var) o barlar hiç işlenmez, ama `last_processed_bar` yine
    `as_of`a taşınır. Sayı tur raporunda durmalı, log da uyarmalı.
    """
    ledger = Ledger(tmp_path)
    engine = Engine(
        [_Scripted("m", signals={START: [_long_signal(stop=95.0)]})],
        config=_config(),
        ledger=ledger,
    )
    engine.run_round(_market(GAP_ROWS, bars=1))  # last_bar = 00:00, emir kuyrukta

    window = _frame(GAP_ROWS).tail(2)  # pencere 08:00'da başlıyor: 04:00 barı YOK
    truncated = MarketData(
        ohlcv={SYMBOL: window}, btc=window, funding={}, as_of=window.index[-1]
    )
    with caplog.at_level(logging.WARNING, logger="core.engine"):
        report = engine.run_round(truncated)

    assert report.by_model("m").missing_bars == 1  # type: ignore[union-attr]
    assert any("telafi edilemedi" in record.message for record in caplog.records)


def test_gap_without_exposure_is_reported_but_not_a_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Açık pozisyonsuz/emirsiz boşluk yalnızca eksik özsermaye satırıdır: INFO yeter.

    Seviyeyi ayırmak, gerçekten ölçümü etkileyen boşluğun (pozisyon taşınıyordu) gürültüde
    kaybolmasını engeller.
    """
    ledger = Ledger(tmp_path)
    engine = Engine([_Scripted("m")], config=_config(), ledger=ledger)
    engine.run_round(_market(GAP_ROWS, bars=1))

    window = _frame(GAP_ROWS).tail(2)
    truncated = MarketData(
        ohlcv={SYMBOL: window}, btc=window, funding={}, as_of=window.index[-1]
    )
    with caplog.at_level(logging.INFO, logger="core.engine"):
        report = engine.run_round(truncated)

    assert report.by_model("m").missing_bars == 1  # type: ignore[union-attr]
    assert not [record for record in caplog.records if record.levelno >= logging.WARNING]


def test_a_day_of_missed_scalp_rounds_is_caught_up_bar_by_bar(tmp_path: Path) -> None:
    """15m katmanı günde 96 tur koşar; bir günlük kesinti tek turda telafi edilmelidir.

    Stop yalnızca TEK bir barın içinde vurulur (bar 40). Motor yalnızca son bara atlasaydı
    o bar hiç sorulmaz ve pozisyon ölçümde haksız yere hayatta kalırdı.
    """
    rows = [(100.0, 101.0, 99.0, 100.0)] * 97
    rows[40] = (100.0, 101.0, 90.0, 100.0)  # stop (95) YALNIZCA bu barda
    index = pd.date_range(START, periods=len(rows), freq="15min", tz="UTC", name="ts")
    frame = pd.DataFrame(list(rows), index=index, columns=["open", "high", "low", "close"])
    frame["volume"] = 1.0

    def snapshot(bars: int) -> MarketData:
        window = frame.head(bars)
        return MarketData(
            ohlcv={SYMBOL: window}, btc=window, funding={}, as_of=window.index[-1]
        )

    ledger = Ledger(tmp_path)
    engine = Engine(
        [_Scripted("m", signals={START: [_long_signal(stop=95.0)]})],
        config=_config(timeframe="15m"),
        ledger=ledger,
    )
    engine.run_round(snapshot(1))
    engine.run_round(snapshot(2))  # dolum @100
    report = engine.run_round(snapshot(97))  # araya 95 tur ATLANDI

    model = report.by_model("m")
    assert model.bars_processed == 95  # type: ignore[union-attr]
    assert model.missing_bars == 0  # type: ignore[union-attr]
    (trade,) = ledger.read_trades("m")
    assert trade["exit_reason"] == "stop"
    assert trade["closed_at"] == index[40].isoformat()


def test_uninterrupted_round_reports_no_missing_bars(tmp_path: Path) -> None:
    """Olağan gecikme bir arıza değildir: telafi edilen barlar `missing_bars`a yazılmaz."""
    ledger = Ledger(tmp_path)
    engine = Engine([_Scripted("m")], config=_config(), ledger=ledger)
    engine.run_round(_market(GAP_ROWS, bars=1))
    report = engine.run_round(_market(GAP_ROWS, bars=4))

    assert report.by_model("m").missing_bars == 0  # type: ignore[union-attr]
    assert report.by_model("m").bars_processed == 3  # type: ignore[union-attr]


# --------------------------------------------------------------------------- #
# İşlem yaşam döngüsü ve defter
# --------------------------------------------------------------------------- #
def test_closed_trade_is_written_to_the_ledger(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path)
    rows = [
        (100.0, 101.0, 99.0, 100.0),
        (102.0, 103.0, 101.0, 102.0),
        (102.0, 102.5, 94.0, 95.0),  # stop (95) bu barda vurulur
    ]
    strategy = _Scripted("m", signals={START: [_long_signal(stop=95.0)]})
    engine = Engine([strategy], config=_config(), ledger=ledger)

    engine.run_round(_market(rows, bars=1))
    engine.run_round(_market(rows, bars=3))

    (trade,) = ledger.read_trades("m")
    assert trade["symbol"] == SYMBOL
    assert trade["direction"] == "long"
    assert trade["exit_reason"] == "stop"
    assert trade["signal_reason"] == "test"
    assert float(trade["exit_price"]) == pytest.approx(95.0)
    assert float(trade["pnl"]) < 0.0
    assert ledger.load_state("m")["positions"] == []  # type: ignore[index]


def test_exit_instruction_closes_at_the_next_bar_open(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path)
    bar1 = pd.Timestamp("2026-01-01 04:00", tz="UTC")
    strategy = _Scripted(
        "m",
        signals={START: [_long_signal()]},
        exits={bar1: [ExitInstruction(symbol=SYMBOL, action="close", reason="kapat")]},
    )
    engine = Engine([strategy], config=_config(), ledger=ledger)

    engine.run_round(_market(ROWS, bars=1))
    engine.run_round(_market(ROWS, bars=2))  # dolum + çıkış talimatı
    engine.run_round(_market(ROWS, bars=3))  # çıkış bar2'nin açılışından dolar

    (trade,) = ledger.read_trades("m")
    assert trade["exit_reason"] == "signal"
    assert float(trade["exit_price"]) == pytest.approx(102.0)  # bar2 açılışı
    assert ledger.load_state("m")["positions"] == []  # type: ignore[index]


def test_manage_positions_sees_a_read_only_view_without_size(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path)
    strategy = _Scripted("m", signals={START: [_long_signal()]})
    engine = Engine([strategy], config=_config(), ledger=ledger)
    engine.run_round(_market(ROWS, bars=1))
    engine.run_round(_market(ROWS, bars=2))

    (view,) = strategy.seen_positions[-1]
    assert isinstance(view, Position)
    assert not hasattr(view, "qty")  # kural 3: strateji boyutu görmez
    with pytest.raises(Exception):
        view.stop_price = 1.0  # type: ignore[misc]


def test_take_profit_partial_exit_is_recorded(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path)
    rows = [
        (100.0, 101.0, 99.0, 100.0),
        (102.0, 103.0, 101.0, 102.0),
        (102.0, 111.0, 101.5, 110.0),  # TP 110'a değer
    ]
    signal = _long_signal(stop=95.0, take_profits=(TakeProfit(price=110.0, fraction=0.5),))
    engine = Engine([_Scripted("m", signals={START: [signal]})], config=_config(), ledger=ledger)

    engine.run_round(_market(rows, bars=1))
    engine.run_round(_market(rows, bars=3))

    (trade,) = ledger.read_trades("m")
    assert trade["exit_reason"] == "tp"
    assert float(trade["pnl"]) > 0.0
    (position,) = ledger.load_state("m")["positions"]  # type: ignore[index]
    assert position["qty"] == pytest.approx(position["initial_qty"] / 2)


def test_funding_is_charged_while_the_position_is_open(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path)
    funding_ts = pd.Timestamp("2026-01-01 08:00", tz="UTC")
    series = pd.Series(
        [0.0002], index=pd.DatetimeIndex([funding_ts], name="ts"), dtype="float64"
    )
    strategy = _Scripted("m", signals={START: [_long_signal()]})
    engine = Engine([strategy], config=_config(), ledger=ledger)

    engine.run_round(_market(ROWS, bars=1, funding={SYMBOL: series}))
    engine.run_round(_market(ROWS, bars=3, funding={SYMBOL: series}))

    (position,) = ledger.load_state("m")["positions"]  # type: ignore[index]
    # 04:00'te açıldı, 08:00 periyodunu ÖDEDİ (long, pozitif oran)
    assert position["funding"] == pytest.approx(-position["qty"] * 102.0 * 0.0002)


# --------------------------------------------------------------------------- #
# İki geçişli tur ve izolasyon (kural 4)
# --------------------------------------------------------------------------- #
def test_meta_models_see_only_normal_model_signals(tmp_path: Path) -> None:
    normal_a = _Scripted("a", signals={START: [_long_signal()]})
    normal_b = _Scripted("b", signals={START: []})
    meta_one, meta_two = _Meta("meta1"), _Meta("meta2")
    engine = Engine(
        [normal_a, meta_one, normal_b, meta_two], config=_config(), ledger=Ledger(tmp_path)
    )

    engine.run_round(_market(ROWS, bars=1))

    for meta in (meta_one, meta_two):
        assert meta.peers is not None
        assert set(meta.peers) == {"a", "b"}  # meta'lar birbirini görmez
        assert len(meta.peers["a"]) == 1


def test_each_meta_model_gets_its_own_copy_of_peer_signals(tmp_path: Path) -> None:
    normal = _Scripted("a", signals={START: [_long_signal()]})
    meta_one, meta_two = _Meta("meta1"), _Meta("meta2")
    engine = Engine([normal, meta_one, meta_two], config=_config(), ledger=Ledger(tmp_path))

    engine.run_round(_market(ROWS, bars=1))

    assert meta_one.peers is not None and meta_two.peers is not None
    assert meta_one.peers["a"][0] is not meta_two.peers["a"][0]
    assert meta_one.peers["a"][0] == meta_two.peers["a"][0]


def test_duplicate_strategy_names_are_refused() -> None:
    with pytest.raises(ValueError, match="benzersiz"):
        Engine([_Scripted("m"), _Scripted("m")], config=_config(), ledger=Ledger())


# --------------------------------------------------------------------------- #
# Hata izolasyonu (kural 8)
# --------------------------------------------------------------------------- #
def test_a_crashing_model_does_not_stop_the_round(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path)
    healthy = _Scripted("healthy", signals={START: [_long_signal()]})
    engine = Engine([_Broken(), healthy], config=_config(), ledger=ledger)

    report = engine.run_round(_market(ROWS, bars=1))

    assert "model çöktü" in report.by_model("broken").skipped  # type: ignore[union-attr]
    assert report.by_model("healthy").signals == 1  # type: ignore[union-attr]
    assert len(ledger.load_state("healthy")["pending_orders"]) == 1  # type: ignore[index]
    assert ledger.load_state("broken")["pending_orders"] == []  # type: ignore[index]


def test_signal_violating_allowed_directions_skips_only_that_model(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path)
    offender = _Scripted(
        "short_only",
        allowed_directions=("short",),
        signals={START: [_long_signal()]},
    )
    healthy = _Scripted("healthy", signals={START: [_long_signal()]})
    engine = Engine([offender, healthy], config=_config(), ledger=ledger)

    report = engine.run_round(_market(ROWS, bars=1))

    assert "izinli değil" in report.by_model("short_only").skipped  # type: ignore[union-attr]
    assert ledger.load_state("short_only")["pending_orders"] == []  # type: ignore[index]
    assert len(ledger.load_state("healthy")["pending_orders"]) == 1  # type: ignore[index]


# --------------------------------------------------------------------------- #
# Trailing stop (kural 9)
# --------------------------------------------------------------------------- #
def test_trailing_stop_tightens_as_price_advances(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path)
    rows = [(100.0 + i, 101.0 + i, 99.0 + i, 100.0 + i) for i in range(20)]
    signal = _long_signal(stop=95.0, trailing_atr=1.0)
    engine = Engine(
        [_Scripted("m", signals={START: [signal]})],
        config=_config(**{"trailing": {"atr_period": 3}}),
        ledger=ledger,
    )

    engine.run_round(_market(rows, bars=1))
    engine.run_round(_market(rows, bars=len(rows)))

    (position,) = ledger.load_state("m")["positions"]  # type: ignore[index]
    assert position["stop_price"] > position["initial_stop_price"]
    assert position["stop_price"] < rows[-1][3]  # stop hâlâ fiyatın altında


def test_trailing_needs_enough_history() -> None:
    frame = _frame(ROWS)
    assert average_true_range(frame, 10) is None
    # TR(bar1)=max(2, |103-100|, |101-100|)=3, TR(bar2)=max(2.5, 2, 0.5)=2.5
    assert average_true_range(frame, 2) == pytest.approx(2.75)


def test_atr_period_must_be_positive() -> None:
    with pytest.raises(ValueError, match="atr_period"):
        average_true_range(_frame(ROWS), 0)


# --------------------------------------------------------------------------- #
# Kenar durumlar
# --------------------------------------------------------------------------- #
def test_round_without_strategies_is_a_no_op(tmp_path: Path) -> None:
    report = Engine([], config=_config(), ledger=Ledger(tmp_path)).run_round(_market(ROWS, bars=1))
    assert report.models == ()


def test_equity_row_is_written_for_every_processed_bar(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path)
    engine = Engine([_Scripted("m")], config=_config(), ledger=ledger)
    engine.run_round(_market(ROWS, bars=1))
    engine.run_round(_market(ROWS, bars=3))

    rows = ledger.read_equity("m")
    assert len(rows) == 3
    assert all(float(row["equity"]) == pytest.approx(10_000.0) for row in rows)


def test_rerunning_the_same_bar_does_not_queue_the_signal_twice(tmp_path: Path) -> None:
    """Cron retry ya da elle tekrar, aynı bar için ikinci bir pozisyon açtırmamalı."""
    ledger = Ledger(tmp_path)
    signals = {START: [_long_signal()], pd.Timestamp("2026-01-01 04:00", tz="UTC"): []}
    engine = Engine([_Scripted("m", signals=signals)], config=_config(), ledger=ledger)

    engine.run_round(_market(ROWS, bars=1))
    engine.run_round(_market(ROWS, bars=1))  # aynı as_of, ikinci kez

    assert len(ledger.load_state("m")["pending_orders"]) == 1  # type: ignore[index]

    engine.run_round(_market(ROWS, bars=2))
    assert len(ledger.load_state("m")["positions"]) == 1  # type: ignore[index]


def test_order_is_cancelled_when_the_symbol_leaves_the_universe(tmp_path: Path) -> None:
    """Sembol bir sonraki turda görülemiyorsa emir iptal edilir; gecikmeli dolum yok."""
    ledger = Ledger(tmp_path)
    engine = Engine(
        [_Scripted("m", signals={START: [_long_signal()]})], config=_config(), ledger=ledger
    )
    engine.run_round(_market(ROWS, bars=1))

    frame = _frame(ROWS).head(2)
    blind = MarketData(ohlcv={}, btc=frame, funding={}, as_of=frame.index[-1])
    engine.run_round(blind)

    state = ledger.load_state("m")
    assert state is not None
    assert state["positions"] == [] and state["pending_orders"] == []


# --------------------------------------------------------------------------- #
# Stop mesafesi bandı (kural 14)
# --------------------------------------------------------------------------- #
# Her barın gerçek aralığı 2.0 ve boşluk yok -> ATR tam olarak 2.0; tavan 3×ATR = 6 birim.
FLAT = [(100.0, 101.0, 99.0, 100.0)] * 6


def _band_engine(strategy: Strategy, ledger: Ledger) -> Engine:
    return Engine(
        [strategy],
        config=_config(**{"trailing": {"atr_period": 3}, "max_stop_atr_multiple": 3.0}),
        ledger=ledger,
    )


def test_signal_inside_the_stop_band_is_kept(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path)
    as_of = _frame(FLAT).index[4]
    strategy = _Scripted("m", signals={as_of: [_long_signal(stop=95.0)]})  # 5 birim = 2.5×ATR
    engine = _band_engine(strategy, ledger)

    report = engine.run_round(_market(FLAT, bars=5))

    assert report.by_model("m").skipped_signals == 0  # type: ignore[union-attr]
    assert len(ledger.load_state("m")["pending_orders"]) == 1  # type: ignore[index]


def test_signal_wider_than_the_cap_is_skipped_and_logged(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Stop tavana ÇEKİLMEZ, işlem atlanır — çekmek modelin tezini başka bir modele çevirirdi."""
    ledger = Ledger(tmp_path)
    as_of = _frame(FLAT).index[4]
    strategy = _Scripted("m", signals={as_of: [_long_signal(stop=93.0)]})  # 7 birim = 3.5×ATR
    engine = _band_engine(strategy, ledger)

    with caplog.at_level(logging.INFO, logger="core.engine"):
        report = engine.run_round(_market(FLAT, bars=5))

    assert report.by_model("m").skipped_signals == 1  # type: ignore[union-attr]
    assert ledger.load_state("m")["pending_orders"] == []  # type: ignore[index]
    assert "stop mesafesi" in caplog.text and "3.50" in caplog.text  # atlama sessiz değil


def test_wide_stop_is_a_market_condition_not_a_programming_error(tmp_path: Path) -> None:
    """Kural 14 elemesi kural 8 ile karışmaz: model atlanmaz, yalnızca o sinyal düşer."""
    ledger = Ledger(tmp_path)
    as_of = _frame(FLAT).index[4]
    strategy = _Scripted(
        "m", signals={as_of: [_long_signal(stop=93.0), _long_signal(stop=96.0)]}
    )
    report = _band_engine(strategy, ledger).run_round(_market(FLAT, bars=5))

    assert report.by_model("m").skipped == ""  # type: ignore[union-attr]
    assert report.by_model("m").signals == 1  # type: ignore[union-attr]
    assert len(ledger.load_state("m")["pending_orders"]) == 1  # type: ignore[index]


def test_band_is_measured_against_the_short_side_too(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path)
    as_of = _frame(FLAT).index[4]
    wide = Signal(symbol=SYMBOL, direction="short", stop_price=107.0, reason="geniş")
    strategy = _Scripted("m", allowed_directions=("short",), signals={as_of: [wide]})
    report = _band_engine(strategy, ledger).run_round(_market(FLAT, bars=5))

    assert report.by_model("m").skipped_signals == 1  # type: ignore[union-attr]


def test_unverifiable_band_keeps_the_signal_and_warns(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """ATR yoksa tavan doğrulanamaz; sinyali elemek işlem sayısını ölçülemeyen nedenle düşürürdü."""
    ledger = Ledger(tmp_path)
    strategy = _Scripted("m", signals={START: [_long_signal(stop=95.0)]})
    engine = Engine(
        [strategy],
        config=_config(**{"trailing": {"atr_period": 50}, "max_stop_atr_multiple": 3.0}),
        ledger=ledger,
    )
    with caplog.at_level(logging.WARNING, logger="core.engine"):
        report = engine.run_round(_market(ROWS, bars=1))

    assert report.by_model("m").skipped_signals == 0  # type: ignore[union-attr]
    assert len(ledger.load_state("m")["pending_orders"]) == 1  # type: ignore[index]
    assert "ATR hesaplanamadı" in caplog.text


# --------------------------------------------------------------------------- #
# Referans modeller (CLAUDE.md kural 15)
# --------------------------------------------------------------------------- #
class _Benchmark(_Scripted):
    is_benchmark = True


def test_benchmark_signal_passes_validation_and_fills(tmp_path: Path) -> None:
    """Stop'suz notional_fraction sinyali motordan geçip 1x pozisyona dönüşür."""
    market = _market([(100.0, 101.0, 99.0, 100.0)] * 3, bars=2)
    strategy = _Benchmark(
        "bench",
        allowed_directions=("long",),
        signals={
            market.as_of: [
                Signal(
                    symbol=SYMBOL,
                    direction="long",
                    sizing="notional_fraction",
                    notional_fraction=0.5,
                    reason="çıpa",
                )
            ]
        },
    )
    ledger = Ledger(tmp_path)
    engine = Engine([strategy], config=_config(), ledger=ledger)

    engine.run_round(market)
    assert ledger.load_state("bench")["positions"] == []  # kural 13: bir sonraki bar

    full = _market([(100.0, 101.0, 99.0, 100.0)] * 3)
    Engine([strategy], config=_config(), ledger=ledger).run_round(full)

    position = ledger.load_state("bench")["positions"][0]
    assert position["stop_price"] is None
    assert position["leverage"] == pytest.approx(1.0)
    assert position["qty"] == pytest.approx(50.0)  # 10000 × 0.5 / 100


def test_competitor_notional_fraction_signal_skips_only_that_model(
    tmp_path: Path, caplog: Any
) -> None:
    """is_benchmark=False bir model muafiyeti kullanamaz; koşu yine de durmaz (kural 8)."""
    market = _market([(100.0, 101.0, 99.0, 100.0)] * 3)
    cheater = _Scripted(
        "cheater",
        allowed_directions=("long",),
        signals={
            market.as_of: [
                Signal(
                    symbol=SYMBOL, direction="long",
                    sizing="notional_fraction", notional_fraction=0.9,
                )
            ]
        },
    )
    honest = _Scripted(
        "honest",
        allowed_directions=("long",),
        signals={market.as_of: [Signal(symbol=SYMBOL, direction="long", stop_price=95.0)]},
    )

    with caplog.at_level(logging.ERROR):
        report = Engine([cheater, honest], config=_config(), ledger=Ledger(tmp_path)).run_round(market)

    assert "is_benchmark=True" in (report.by_model("cheater").skipped or "")
    assert report.by_model("cheater").signals == 0
    assert report.by_model("honest").signals == 1  # diğer model etkilenmedi


def test_stopless_signal_bypasses_the_stop_band(tmp_path: Path) -> None:
    """Kural 14 bandı stop mesafesi üzerinden tanımlıdır; stop'suz sinyalde uygulanamaz."""
    market = _market([(100.0, 101.0, 99.0, 100.0)] * 20)
    strategy = _Benchmark(
        "bench",
        allowed_directions=("long",),
        signals={
            market.as_of: [
                Signal(
                    symbol=SYMBOL, direction="long",
                    sizing="notional_fraction", notional_fraction=0.5,
                )
            ]
        },
    )
    report = Engine([strategy], config=_config(), ledger=Ledger(tmp_path)).run_round(market)
    assert report.by_model("bench").signals == 1
    assert report.by_model("bench").skipped_signals == 0


def test_pending_order_round_trips_sizing_fields() -> None:
    order = PendingOrder(
        kind="open",
        symbol=SYMBOL,
        direction="long",
        created_at=START,
        sizing="notional_fraction",
        notional_fraction=0.25,
    )
    restored = PendingOrder.from_state(order.as_state())
    assert restored == order


def test_legacy_pending_order_without_sizing_defaults_to_risk() -> None:
    """Kural 15'ten önce yazılmış defterler okunabilir kalmalı."""
    restored = PendingOrder.from_state(
        {
            "kind": "open",
            "symbol": SYMBOL,
            "direction": "long",
            "created_at": START.isoformat(),
            "stop_price": 95.0,
        }
    )
    assert restored.sizing == "risk"
    assert restored.notional_fraction is None
    assert restored.stop_price == pytest.approx(95.0)


# --------------------------------------------------------------------------- #
# Çıkışın ALT sebebi: motorun yazdığı `exit_rule` etiketi
# --------------------------------------------------------------------------- #
def test_exit_instruction_tag_reaches_the_ledger(tmp_path: Path) -> None:
    """`exit_reason` strateji çıkışında her zaman "signal"dır; talimatın kendi etiketi
    (ör. scalp katmanının zaman stop'u) olmadan defterde zaman stop'u ile başka bir
    strateji çıkışı ayırt edilemezdi."""
    from core.tags import format_tags

    ledger = Ledger(tmp_path)
    exit_ts = pd.Timestamp("2026-01-01 04:00", tz="UTC")
    strategy = _Scripted(
        "m",
        signals={START: [_long_signal()]},
        exits={exit_ts: [ExitInstruction(
            symbol=SYMBOL, action="close",
            reason=format_tags("zaman stop'u: 16 bar", exit_rule="time_stop"),
        )]},
    )
    engine = Engine([strategy], config=_config(), ledger=ledger)

    engine.run_round(_market(ROWS, bars=1))
    engine.run_round(_market(ROWS, bars=2))
    engine.run_round(_market(ROWS, bars=3))

    (trade,) = ledger.read_trades("m")
    assert trade["exit_reason"] == "signal"
    assert "exit_rule=time_stop" in trade["notes"]


def test_trailing_stop_labels_the_rule_that_moved_it(tmp_path: Path) -> None:
    """Takip eden stop'un aldığı işlem, ilk stop'un aldığından ayırt edilebilmeli."""
    ledger = Ledger(tmp_path)
    # ATR(14) hesaplanabilsin diye önce 15 sakin bar; sonra zirve, sonra düşüş.
    rows = [(100.0, 101.0, 99.0, 100.0)] * 15
    rows = rows + [
        (100.0, 130.0, 99.0, 129.0),    # zirve: trailing stop yukarı taşınır
        (129.0, 129.5, 100.0, 101.0),   # taşınmış stop alır (ilk stop 95'e hiç inilmedi)
    ]
    signal = _long_signal(stop=95.0, trailing_atr=1.0)
    engine = Engine([_Scripted("m", signals={START: [signal]})], config=_config(), ledger=ledger)

    engine.run_round(_market(rows, bars=1))
    engine.run_round(_market(rows, bars=len(rows)))

    (trade,) = ledger.read_trades("m")
    assert trade["exit_reason"] == "stop"
    assert "exit_rule=trailing_atr" in trade["notes"]


# --------------------------------------------------------------------------- #
# Kuyruğa giren sinyalin denetim kaydı (EmittedSignal)
# --------------------------------------------------------------------------- #
def test_emitted_signal_records_the_bar_its_fill_and_the_geometry(tmp_path: Path) -> None:
    """Anlık bildirim bu kaydı okur: sayıdan (signals) hangi sinyal olduğu okunamaz."""
    signal = _long_signal(stop=95.0, take_profits=(TakeProfit(price=115.0, fraction=1.0),))
    engine = Engine([_Scripted("m", signals={START: [signal]})], config=_config(), ledger=Ledger(tmp_path))

    report = engine.run_round(_market(ROWS, bars=1))

    (record,) = report.by_model("m").emitted  # type: ignore[union-attr]
    assert (record.model, record.symbol, record.direction) == ("m", SYMBOL, "long")
    assert record.bar == START
    assert record.fills_at == START + pd.Timedelta("4h")  # kural 13: bir SONRAKİ bar
    assert record.close == 100.0                          # modelin gördüğü son fiyat
    assert (record.stop_price, record.target_price) == (95.0, 115.0)
    assert record.reward_risk == pytest.approx(3.0)       # 15 birim hedef / 5 birim stop


def test_emitted_signal_leaves_reward_risk_undefined_without_a_target(tmp_path: Path) -> None:
    """Ölçülemeyen oran `None`dır, 0.0 değil (bkz. core/metrics.py'nin nan kuralı)."""
    engine = Engine([_Scripted("m", signals={START: [_long_signal()]})], config=_config(), ledger=Ledger(tmp_path))

    report = engine.run_round(_market(ROWS, bars=1))

    (record,) = report.by_model("m").emitted  # type: ignore[union-attr]
    assert record.reward_risk is None


def test_band_skipped_signal_is_not_recorded_as_emitted(tmp_path: Path) -> None:
    """Kayıt, `signals` sayısıyla AYNI kümedir: elenen sinyal kuyruğa hiç girmedi."""
    as_of = _frame(FLAT).index[4]
    strategy = _Scripted("m", signals={as_of: [_long_signal(stop=93.0)]})  # 3.5×ATR
    report = _band_engine(strategy, Ledger(tmp_path)).run_round(_market(FLAT, bars=5))

    assert report.by_model("m").skipped_signals == 1  # type: ignore[union-attr]
    assert report.by_model("m").emitted == ()         # type: ignore[union-attr]


# --------------------------------------------------------------------------- #
# Tarama sayımı (Strategy.take_survey -> ModelReport.survey)
# --------------------------------------------------------------------------- #
# Neden burada: sayım bir DENETİM İZİDİR (kural 15, `rejections`/`emitted` ile aynı
# statü) ve motorun onu toplamaması hâlinde "bu barda hiç kurulum yoktu" ile "sinyal
# modülü sessizce bozuldu" ayırt edilemez. Koşu logları siliniyor; tur raporu kalıyor.
class _Surveying(_Scripted):
    """Her `generate_signals` çağrısında sabit bir sayım döndüren sahte model."""

    def __init__(self, name: str, counts: Mapping[str, int], **kwargs: Any) -> None:
        super().__init__(name, **kwargs)
        self._counts = dict(counts)
        self.survey_calls = 0

    def take_survey(self) -> Mapping[str, int] | None:
        self.survey_calls += 1
        return dict(self._counts)


def test_survey_reaches_the_round_report(tmp_path: Path) -> None:
    strategy = _Surveying("m", {"kurulum": 1, "bant_ici": 3})
    engine = Engine([strategy], config=_config(), ledger=Ledger(tmp_path))

    report = engine.run_round(_market(ROWS, bars=1))

    assert report.by_model("m").survey == {"bant_ici": 3, "kurulum": 1}  # type: ignore[union-attr]
    assert strategy.survey_calls == 1


def test_survey_is_summed_over_every_backfilled_bar(tmp_path: Path) -> None:
    """`signals_per_bar` açıkken her barın kendi taraması vardır; sayım TOPLANIR.

    Son barınkini saklamak, telafi edilen barlarda kolun ne gördüğünü kaydın dışında
    bırakırdı — oysa o barlar ölçüme tam olarak giriyor.
    """
    strategy = _Surveying("m", {"kurulum": 1})
    engine = Engine([strategy], config=_config(signals_per_bar=True), ledger=Ledger(tmp_path))
    engine.run_round(_market(ROWS, bars=1))

    report = engine.run_round(_market(ROWS, bars=3))  # iki bar birden işlenir

    assert strategy.survey_calls == 3  # 1 + 2
    assert report.by_model("m").survey == {"kurulum": 2}  # type: ignore[union-attr]


def test_a_model_without_a_survey_reports_nothing(tmp_path: Path) -> None:
    """Varsayılan kanca `None` döner: sayım tutmayan model rapora boş alanla girer."""
    engine = Engine([_Scripted("m")], config=_config(), ledger=Ledger(tmp_path))

    report = engine.run_round(_market(ROWS, bars=1))

    assert report.by_model("m").survey == {}  # type: ignore[union-attr]


def test_a_crashing_model_leaves_no_survey(tmp_path: Path) -> None:
    """Patlayan çağrının yarım sayımı kaydedilmez: "kaç sembol incelendi" yanlış olurdu."""
    engine = Engine([_Broken()], config=_config(), ledger=Ledger(tmp_path))

    report = engine.run_round(_market(ROWS, bars=1))

    assert report.by_model("broken").survey == {}  # type: ignore[union-attr]
    assert "generate_signals" in report.by_model("broken").skipped  # type: ignore[union-attr]
