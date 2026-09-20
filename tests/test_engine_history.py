"""Motorun modele KENDİ kapanmış işlemlerini vermesi (`observe_closed_trades`).

Bu kancanın tek gerekçesi uyarlanabilir modellerdir (model 11) ve en kritik özelliği bir
YOKLUKTUR: açık pozisyon listeye giremez. Bu dosya onu yapısal olarak ölçer — modelin
"bakmamayı seçmesi" değil, görebileceği bir yüzeyin hiç olmaması.

İkinci ölçtüğü şey izolasyonun korunduğudur (kural 4): model başka modelin işlemini
görmez ve kancayı uygulamayan modeller için defter hiç okunmaz.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Sequence

import pandas as pd
import pytest

from core.config import load_config
from core.engine import Engine
from core.ledger import Ledger
from core.portfolio import Portfolio
from strategies.base import (
    ClosedTrade,
    Direction,
    MarketData,
    Position,
    Signal,
    Strategy,
)
from tests.helpers_market import frame, market

SYMBOL = "BTC-USDT-SWAP"
OTHER = "ETH-USDT-SWAP"


class _Recorder(Strategy):
    """Kancayı uygulayan model: gördüğü geçmişi kaydeder, tek bir sinyal üretir."""

    allowed_directions: list[Direction] = ["long"]

    def __init__(self, name: str = "kayitci", *, symbol: str = SYMBOL) -> None:
        self.name = name
        self._symbol = symbol
        self.seen: list[tuple[ClosedTrade, ...]] = []

    def observe_closed_trades(self, trades: Sequence[ClosedTrade]) -> None:
        self.seen.append(tuple(trades))

    def generate_signals(self, market_data: MarketData, peer_signals: Any = None) -> list[Signal]:
        price = float(market_data.ohlcv[self._symbol]["close"].iloc[-1])
        return [
            Signal(
                symbol=self._symbol,
                direction="long",
                stop_price=price * 0.97,
                reason="kayitci kurulumu",
            )
        ]

    def manage_positions(self, market_data: MarketData, positions: list[Position]) -> list[Any]:
        return []


class _Blind(Strategy):
    """Kancayı UYGULAMAYAN model: motorun defteri hiç okumadığı durum."""

    name = "kor"
    allowed_directions: list[Direction] = ["long"]

    def generate_signals(self, market_data: MarketData, peer_signals: Any = None) -> list[Signal]:
        return []


def _market(bars: int = 40, *, close: float = 100.0) -> MarketData:
    closes = [close] * bars
    frames = {
        SYMBOL: frame(closes, spread=1.0),
        OTHER: frame(closes, spread=1.0),
    }
    return market(frames)


def _run(
    strategies: Sequence[Strategy], ledger: Ledger, snapshot: MarketData
) -> Any:
    config = load_config()
    return Engine(
        list(strategies), config=config, ledger=ledger, portfolio=Portfolio(config)
    ).run_round(snapshot)


# --------------------------------------------------------------------------- #
# Açık pozisyon ASLA girmez
# --------------------------------------------------------------------------- #
def test_open_position_never_reaches_the_model(tmp_path: Path) -> None:
    """Pozisyon açıkken geçmiş BOŞTUR: kâğıt üstündeki kâr öğrenmeye giremez."""
    ledger = Ledger(tmp_path)
    model = _Recorder()

    _run([model], ledger, _market(bars=40))  # sinyal kuyruğa girer
    _run([model], ledger, _market(bars=41))  # dolum: pozisyon AÇIK

    assert ledger.load_state(model.name)["positions"], "pozisyon açılmadı, test anlamsız"
    assert model.seen[-1] == ()


def test_closed_trade_reaches_the_model_with_its_realized_r(tmp_path: Path) -> None:
    """Pozisyon kapandığında gerçekleşen R modele ulaşır — ve ancak o zaman."""
    ledger = Ledger(tmp_path)
    model = _Recorder()

    _run([model], ledger, _market(bars=40))
    _run([model], ledger, _market(bars=41))
    # Fiyat stop'un altına iner: pozisyon kapanır.
    _run([model], ledger, _market(bars=42, close=90.0))
    _run([model], ledger, _market(bars=43, close=90.0))

    history = model.seen[-1]
    assert len(history) == 1
    assert history[0].symbol == SYMBOL
    assert history[0].r_multiple is not None and history[0].r_multiple < 0.0
    assert history[0].signal_reason.startswith("kayitci kurulumu")


def test_trades_closed_this_round_are_included(tmp_path: Path) -> None:
    """Bu turda kapanan işlem deftere henüz yazılmamıştır ama KAPANMIŞTIR: beklenmez.

    15 dakikalık bir modelde pozisyon aynı turda açılıp kapanabilir; defteri beklemek,
    modelin en taze sonucu bir tur geç görmesi demekti.
    """
    ledger = Ledger(tmp_path)
    model = _Recorder()

    _run([model], ledger, _market(bars=40))
    _run([model], ledger, _market(bars=41))
    assert ledger.read_trades(model.name) == []  # üçüncü tura kadar defter boş

    # Aynı turda hem kapanış hem yeni sinyal: kapanan işlem BU turun geçmişinde görünmeli.
    _run([model], ledger, _market(bars=42, close=90.0))

    # Defterden gelemezdi (tur başladığında boştu): besleme turun kendi kapanışlarını da taşır.
    assert len(model.seen[2]) == 1


# --------------------------------------------------------------------------- #
# İzolasyon (kural 4)
# --------------------------------------------------------------------------- #
def test_model_only_sees_its_own_trades(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path)
    mine, other = _Recorder("benim"), _Recorder("oteki", symbol=OTHER)

    _run([mine, other], ledger, _market(bars=40))
    _run([mine, other], ledger, _market(bars=41))
    _run([mine, other], ledger, _market(bars=42, close=90.0))
    _run([mine, other], ledger, _market(bars=43, close=90.0))

    assert {trade.symbol for trade in mine.seen[-1]} == {SYMBOL}
    assert {trade.symbol for trade in other.seen[-1]} == {OTHER}


def test_models_without_the_hook_never_read_the_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """4 saatlik katmanın modelleri bu eklemeden etkilenmez: defteri hiç okumazlar."""
    ledger = Ledger(tmp_path)
    reads: list[str] = []
    original = Ledger.read_trades
    monkeypatch.setattr(
        Ledger,
        "read_trades",
        lambda self, model: (reads.append(model), original(self, model))[1],
    )

    _run([_Blind()], ledger, _market())

    assert reads == []


# --------------------------------------------------------------------------- #
# Hata izolasyonu (kural 8)
# --------------------------------------------------------------------------- #
def test_failing_hook_skips_only_that_model(tmp_path: Path) -> None:
    """Yarım öğrenilmiş posterior ile sinyal üretmek, modelin ne ölçtüğünü bilinmez kılardı."""

    class _Exploding(_Recorder):
        def observe_closed_trades(self, trades: Sequence[ClosedTrade]) -> None:
            raise ValueError("kol etiketi yok")

    ledger = Ledger(tmp_path)
    broken, healthy = _Exploding("patlayan"), _Recorder("saglam", symbol=OTHER)

    report = _run([broken, healthy], ledger, _market())

    assert report.by_model("patlayan").signals == 0
    assert "kol etiketi yok" in report.by_model("patlayan").skipped
    assert report.by_model("saglam").signals == 1  # koşu sürdü


def test_history_is_read_only_for_the_model(tmp_path: Path) -> None:
    """Sözleşme salt okunur: model geçmişi değiştirerek defteri etkileyemez."""
    ledger = Ledger(tmp_path)
    model = _Recorder()

    _run([model], ledger, _market(bars=40))
    _run([model], ledger, _market(bars=41))
    _run([model], ledger, _market(bars=42, close=90.0))
    _run([model], ledger, _market(bars=43, close=90.0))

    trade = model.seen[-1][0]
    with pytest.raises(Exception):
        trade.r_multiple = 99.0  # type: ignore[misc]


def test_history_is_ordered_by_close_time(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path)
    model = _Recorder()

    for bars, close in ((40, 100.0), (41, 100.0), (42, 90.0), (43, 90.0), (44, 80.0), (45, 80.0)):
        _run([model], ledger, _market(bars=bars, close=close))

    history = model.seen[-1]
    assert len(history) >= 2
    assert list(history) == sorted(history, key=lambda item: item.closed_at)
    assert all(isinstance(item.closed_at, pd.Timestamp) for item in history)
