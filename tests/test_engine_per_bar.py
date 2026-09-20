"""Telafi edilen barlarda sinyal üretimi (`signals_per_bar`, core/engine.py > _trade_step).

Neden ayrı dosya: buradaki testlerin ölçtüğü şey tek bir bayrak değil, bir EŞDEĞERLİKTİR
— "bir turda telafi edilen N bar" ile "N ayrı turda koşulan N bar" aynı defteri üretmeli.
Motorun geri kalanını ölçen tests/test_engine.py bayrağı KAPALI (kök config) koşar ve
öyle kalmalıdır: 4 saatlik katmanın davranışının değişmediği ancak orada görülebilir.

Bayrağın gerekçesi ölçümün kendisidir: GitHub cron'u 15 dakikalık kadansta tetiklemelerin
büyük kısmını düşürür. Sinyal yalnızca `as_of` barında üretilseydi, atlanan turların
sinyal FIRSATI da kaybolurdu ve tablo modelin değil cron'un kadansını ölçerdi.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Sequence

import pandas as pd
import pytest

from core.config import load_config
from core.engine import Engine
from core.ledger import Ledger
from core.portfolio import Portfolio
from strategies.base import (
    Direction,
    ExitInstruction,
    MarketData,
    Position,
    Signal,
    Strategy,
)

SYMBOL = "BTC-USDT-SWAP"
OTHER = "ETH-USDT-SWAP"
START = pd.Timestamp("2026-01-01 00:00:00", tz="UTC")

# Her barın açılışı farklı: dolumun HANGİ barda gerçekleştiği fiyattan okunabilsin.
# Aralık dar tutulur (stop 90'ın altında) ki sinyal barı ile dolum barı arasındaki fark
# bir stop tetiklemesiyle karışmasın.
ROWS = [
    (100.0, 100.6, 99.4, 100.0),
    (101.0, 101.6, 100.4, 101.0),
    (102.0, 102.6, 101.4, 102.0),
    (103.0, 103.6, 102.4, 103.0),
    (104.0, 104.6, 103.4, 104.0),
    (105.0, 105.6, 104.4, 105.0),
    (106.0, 106.6, 105.4, 106.0),
    (107.0, 107.6, 106.4, 107.0),
]
INDEX = pd.date_range(START, periods=len(ROWS), freq="15min", tz="UTC", name="ts")


def _frame(rows: Sequence[tuple[float, float, float, float]] = ROWS) -> pd.DataFrame:
    frame = pd.DataFrame(
        list(rows),
        index=INDEX[: len(rows)],
        columns=["open", "high", "low", "close"],
    )
    frame["volume"] = 1.0
    return frame


def _market(bars: int, *, symbols: Sequence[str] = (SYMBOL,)) -> MarketData:
    window = _frame().head(bars)
    return MarketData(
        ohlcv={symbol: window for symbol in symbols},
        btc=window,
        funding={},
        as_of=window.index[-1],
    )


def _config(*, per_bar: bool = True, **overrides: Any) -> dict[str, Any]:
    config = load_config()
    config.update(
        fee_rate=0.0,
        slippage_base=0.0,
        slippage_short_stop=0.0,
        timeframe="15m",
        signals_per_bar=per_bar,
    )
    config.update(overrides)
    return config


def _engine(strategy: Strategy, ledger: Ledger, config: dict[str, Any]) -> Engine:
    return Engine([strategy], config=config, ledger=ledger, portfolio=Portfolio(config))


class _EveryBar(Strategy):
    """Gördüğü HER barda tek bir long sinyali üreten model; gördüklerini kaydeder."""

    allowed_directions: list[Direction] = ["long"]

    def __init__(
        self,
        name: str = "her_bar",
        *,
        symbols: Sequence[str] = (SYMBOL,),
        only_at: Sequence[pd.Timestamp] | None = None,
    ) -> None:
        self.name = name
        self._symbols = list(symbols)
        self._only_at = None if only_at is None else set(only_at)
        self.signal_bars: list[pd.Timestamp] = []
        self.managed_bars: list[pd.Timestamp] = []
        self.seen_last_bar: list[pd.Timestamp] = []
        self.seen_universe: list[tuple[str, ...]] = []

    def generate_signals(
        self, market: MarketData, peer_signals: Mapping[str, tuple[Signal, ...]] | None = None
    ) -> list[Signal]:
        frame = market.ohlcv[self._symbols[0]]
        self.seen_last_bar.append(frame.index[-1])
        self.seen_universe.append(tuple(sorted(market.ohlcv)))
        if self._only_at is not None and market.as_of not in self._only_at:
            return []
        self.signal_bars.append(market.as_of)
        # Sembol, barın sırasına göre dönüşümlü: kota testinde her bar BAŞKA bir sembole
        # sinyal üretilsin (aynı sembol `duplicate_position` ile reddedilirdi).
        index = len(self.signal_bars) - 1
        symbol = self._symbols[index % len(self._symbols)]
        price = float(market.ohlcv[symbol]["close"].iloc[-1])
        return [
            Signal(
                symbol=symbol,
                direction="long",
                stop_price=price * 0.9,
                reason=f"{self.name} @ {market.as_of.isoformat()}",
            )
        ]

    def manage_positions(
        self, market: MarketData, positions: list[Position]
    ) -> list[ExitInstruction]:
        self.managed_bars.append(market.as_of)
        return []


# --------------------------------------------------------------------------- #
# 1) Boşluğun HER barı sinyal üretir ve yönetilir
# --------------------------------------------------------------------------- #
def test_six_bar_gap_generates_a_signal_on_every_bar(tmp_path: Path) -> None:
    """Altı barlık boşluk: altı barın da sinyali üretilir, altısı da yönetilir.

    Eski davranışta bu turdan TEK sinyal çıkardı (yalnızca `as_of`); atlanan beş barın
    fırsatı kaybolurdu. Ölçülen şey bu: kayıp yok.
    """
    ledger = Ledger(tmp_path)
    strategy = _EveryBar()
    engine = _engine(strategy, ledger, _config())

    engine.run_round(_market(1))            # bar0 işlenir, last_bar = bar0
    report = engine.run_round(_market(7))   # bar1..bar6: ALTI barlık boşluk

    model = report.by_model("her_bar")
    assert model is not None
    assert model.bars_processed == 6
    # İlk turun bar0 sinyali listenin başında durur; boşluğun altı barı ONUN ardından gelir.
    assert strategy.signal_bars == list(INDEX[0:7])
    assert strategy.managed_bars[-6:] == list(INDEX[1:7])
    assert model.signals == 6


def test_manage_positions_runs_on_every_backfilled_bar(tmp_path: Path) -> None:
    """Pozisyon yönetimi de bar bazındadır: zaman stop'u turun sonunu bekleyemez.

    `scalp` modelleri pozisyonu 16 bar sonra kapatır (manage_positions, kural 10). Yönetim
    yalnızca `as_of`ta sorulsaydı, boşlukta yaşı dolan pozisyon turun sonuna kadar açık
    kalır ve ömrü boşluğun uzunluğuna göre uzardı.
    """
    ledger = Ledger(tmp_path)

    class _CloseAfterTwoBars(_EveryBar):
        def manage_positions(
            self, market: MarketData, positions: list[Position]
        ) -> list[ExitInstruction]:
            self.managed_bars.append(market.as_of)
            return [
                ExitInstruction(symbol=position.symbol, action="close", reason="yas")
                for position in positions
                if market.as_of - pd.Timestamp(position.opened_at) >= pd.Timedelta(minutes=30)
            ]

    strategy = _CloseAfterTwoBars(only_at=[INDEX[0]])
    engine = _engine(strategy, ledger, _config())
    engine.run_round(_market(1))   # bar0: sinyal
    engine.run_round(_market(7))   # bar1 dolum, bar3'te yaş dolar, bar4'te kapanır

    (trade,) = ledger.read_trades("her_bar")
    assert trade["opened_at"] == INDEX[1].isoformat()
    assert trade["exit_reason"] == "signal"
    # Yaş bar3'te dolar, talimat bar3'te verilir, dolum bar4'ün AÇILIŞINDADIR (kural 13).
    assert trade["closed_at"] == INDEX[4].isoformat()
    assert float(trade["exit_price"]) == pytest.approx(ROWS[4][0])


# --------------------------------------------------------------------------- #
# 2) Sıra: sinyal -> BİR SONRAKİ barın açılışı -> mum içi kontrol
# --------------------------------------------------------------------------- #
def test_each_bar_order_fills_at_the_next_bar_open(tmp_path: Path) -> None:
    """Telafide de dolum sinyalin barında değil, BİR SONRAKİ barın açılışındadır (kural 13).

    Boşluğun ortasındaki tek bir barda sinyal üretilir; dolum fiyatı hangi barın açılışı
    olduğunu tek başına söyler (her barın açılışı farklı).
    """
    ledger = Ledger(tmp_path)
    strategy = _EveryBar(only_at=[INDEX[3]])
    engine = _engine(strategy, ledger, _config())

    engine.run_round(_market(1))
    engine.run_round(_market(7))

    state = ledger.load_state("her_bar")
    assert state is not None
    (position,) = state["positions"]
    assert position["opened_at"] == INDEX[4].isoformat()
    assert float(position["entry_price"]) == pytest.approx(ROWS[4][0])


def test_catch_up_matches_running_each_bar_in_its_own_round(tmp_path: Path) -> None:
    """EŞDEĞERLİK: bir turda telafi edilen altı bar, altı ayrı turla AYNI defteri üretir.

    Toplu (barları birleştiren) bir değerlendirme olmadığının tek gerçek kanıtı budur:
    sinyal, dolum, mum içi kontrol ve özsermaye satırları bar bazında aynı sırayla
    yürümezse iki defter ayrışır. Cron'un kaç turu düşürdüğü böylece ölçümün sonucunu
    değiştirmez — yalnızca sonucun ne zaman yazıldığını.
    """
    config = _config()

    catch_up_dir = tmp_path / "telafi"
    catch_up = Ledger(catch_up_dir)
    strategy_a = _EveryBar("m")
    engine_a = _engine(strategy_a, catch_up, config)
    engine_a.run_round(_market(1))
    engine_a.run_round(_market(7))   # bar1..bar6 tek turda

    per_round_dir = tmp_path / "tur_tur"
    per_round = Ledger(per_round_dir)
    strategy_b = _EveryBar("m")
    engine_b = _engine(strategy_b, per_round, config)
    for bars in range(1, 8):         # bar0..bar6, her biri kendi turunda
        engine_b.run_round(_market(bars))

    assert catch_up.read_trades("m") == per_round.read_trades("m")
    assert catch_up.read_equity("m") == per_round.read_equity("m")
    assert catch_up.load_state("m") == per_round.load_state("m")
    assert strategy_a.signal_bars == strategy_b.signal_bars


# --------------------------------------------------------------------------- #
# 3) Pozisyon limitleri BAR bazında
# --------------------------------------------------------------------------- #
def test_position_limits_are_enforced_per_bar(tmp_path: Path) -> None:
    """Kota her barın dolumunda yeniden sorulur; turun toplamına bakılmaz.

    Altı bar altı sinyal üretir, ama kota iki pozisyondur: ilk iki dolum geçer, geri
    kalan dördü `max_positions` koduyla reddedilir. Kota tur başına uygulansaydı altı
    sinyalin hepsi tek bir kotadan geçer ya da hepsi birden düşerdi — ikisi de gerçek
    borsanın davranışı değil.
    """
    symbols = [SYMBOL, OTHER, "SOL-USDT-SWAP", "XRP-USDT-SWAP", "DOGE-USDT-SWAP", "ADA-USDT-SWAP"]
    ledger = Ledger(tmp_path)
    strategy = _EveryBar(symbols=symbols)
    config = _config(max_positions=2, max_short_positions=1)
    engine = _engine(strategy, ledger, config)

    engine.run_round(_market(1, symbols=symbols))
    report = engine.run_round(_market(7, symbols=symbols))

    model = report.by_model("her_bar")
    assert model is not None
    assert model.signals == 6
    assert model.filled == 2
    assert model.rejections.get("max_positions") == 4

    state = ledger.load_state("her_bar")
    assert state is not None
    assert len(state["positions"]) == 2
    # Kota BAR bazında dolar: geçenler ilk iki sinyaldir, sonrakiler değil.
    assert sorted(position["symbol"] for position in state["positions"]) == sorted(symbols[:2])


# --------------------------------------------------------------------------- #
# 4) Look-ahead yasağı (kural 12)
# --------------------------------------------------------------------------- #
def test_model_only_sees_bars_up_to_the_signal_bar(tmp_path: Path) -> None:
    """Telafi barında model turun `as_of`'unu GÖRMEZ; çerçeve o barda biter.

    Bu kuralın ihlali tek bir modeli değil tüm sonuçları geçersiz kılar: model geleceği
    görerek geçmişte sinyal üretirdi.
    """
    ledger = Ledger(tmp_path)
    strategy = _EveryBar()
    engine = _engine(strategy, ledger, _config())

    engine.run_round(_market(1))
    engine.run_round(_market(7))

    # Model her çağrıda yalnızca O barda biten bir çerçeve görür: turun `as_of`'u (bar6)
    # boşluğun hiçbir barında görünmez.
    assert strategy.seen_last_bar == list(INDEX[0:7])
    assert strategy.signal_bars == strategy.seen_last_bar


def test_symbol_without_the_bar_leaves_that_bars_universe(tmp_path: Path) -> None:
    """O barı taşımayan sembol, o barın evreninden düşer (core/data.py'nin aynı kuralı).

    Düşmeseydi sembolün referans fiyatı bulunamaz ve doğrulama o barın TÜM sinyallerini
    düşürürdü (kural 8) — cron atlaması yüzünden gelen bir boşluk, gecikmeli tek bir
    sembol yüzünden bütün bir barı sessizce boşa çıkarırdı.
    """
    ledger = Ledger(tmp_path)
    strategy = _EveryBar()
    engine = _engine(strategy, ledger, _config())

    full = _frame()
    late = full.tail(2)  # bar6'dan itibaren var; önceki barları YOK
    market = MarketData(
        ohlcv={SYMBOL: full, OTHER: late},
        btc=full,
        funding={},
        as_of=full.index[-1],
    )
    engine.run_round(_market(1))
    engine.run_round(market)

    # bar1..bar6: OTHER yalnızca bar6 ve bar7'de var, `as_of` barı bar7.
    assert strategy.seen_universe[1:] == [(SYMBOL,)] * 5 + [(SYMBOL, OTHER)] * 2


def test_bar_after_as_of_is_never_processed(tmp_path: Path) -> None:
    """`as_of`tan SONRAKİ bar işlenmez: kapanmamış bar ne sinyal ne özsermaye üretir.

    core/data.py kapanmamış barı zaten atar (kural 12); motor ikinci kapıdır, çünkü
    `signals_per_bar` açıkken bir bar fazla işlemek doğrudan look-ahead ile sinyal
    üretmek demektir.
    """
    ledger = Ledger(tmp_path)
    strategy = _EveryBar()
    engine = _engine(strategy, ledger, _config())

    full = _frame()  # sekiz bar
    market = MarketData(
        ohlcv={SYMBOL: full},
        btc=full,
        funding={},
        as_of=INDEX[5],  # son İKİ bar henüz "kapanmamış" sayılıyor
    )
    report = engine.run_round(market)

    model = report.by_model("her_bar")
    assert model is not None
    assert model.bars_processed == 1          # defteri yeni model yalnızca `as_of`u işler
    assert strategy.signal_bars == [INDEX[5]]
    assert [row["ts"] for row in ledger.read_equity("her_bar")] == [INDEX[5].isoformat()]


def test_backfill_stops_at_as_of(tmp_path: Path) -> None:
    """Telafi `as_of`ta biter: boşluk kapatılırken kapanmamış barlara taşmaz."""
    ledger = Ledger(tmp_path)
    strategy = _EveryBar()
    engine = _engine(strategy, ledger, _config())

    engine.run_round(_market(1))
    full = _frame()
    market = MarketData(ohlcv={SYMBOL: full}, btc=full, funding={}, as_of=INDEX[4])
    report = engine.run_round(market)

    model = report.by_model("her_bar")
    assert model is not None
    assert model.bars_processed == 4                       # bar1..bar4
    assert strategy.signal_bars == list(INDEX[0:5])
    assert INDEX[5] not in strategy.seen_last_bar


# --------------------------------------------------------------------------- #
# 5) Bayrak kapalıyken davranış değişmez (4 saatlik katman)
# --------------------------------------------------------------------------- #
def test_flag_off_keeps_signals_at_as_of_only(tmp_path: Path) -> None:
    """Kapalıyken telafi barları YALNIZCA pozisyon yönetimi için ilerletilir.

    4 saatlik katmanın defterinde biriken geçmiş bu davranışla üretildi; bayrağın kökte
    kapalı olması o defterin tek bir kuralla yazılmaya devam etmesi demektir.
    """
    ledger = Ledger(tmp_path)
    strategy = _EveryBar()
    engine = _engine(strategy, ledger, _config(per_bar=False))

    engine.run_round(_market(1))
    report = engine.run_round(_market(7))

    model = report.by_model("her_bar")
    assert model is not None
    assert model.bars_processed == 6      # barlar yine sırayla ilerletilir
    assert model.signals == 1             # ama sinyal yalnızca `as_of` barında üretilir
    assert strategy.signal_bars == [INDEX[0], INDEX[6]]


def test_rerunning_the_same_bar_does_not_signal_twice(tmp_path: Path) -> None:
    """Aynı `as_of` ile ikinci koşu (cron retry) sinyali ikinci kez kuyruğa almaz."""
    ledger = Ledger(tmp_path)
    strategy = _EveryBar()
    engine = _engine(strategy, ledger, _config())

    engine.run_round(_market(4))
    report = engine.run_round(_market(4))

    model = report.by_model("her_bar")
    assert model is not None
    assert model.bars_processed == 0
    assert model.signals == 0
    assert strategy.signal_bars == [INDEX[3]]


def test_each_recovered_bar_records_its_own_emitted_signal(tmp_path: Path) -> None:
    """Kayıt BARA aittir, tura değil: bildirim "son bar" filtresini buradan kurar.

    Telafi edilen barların sinyalleri deftere yazılır ve ölçüme girer; anlık bildirim ise
    yalnızca `as_of` barındakini yollar (scripts/telegram_signals.py). Bar zamanı kayıtta
    durmasaydı o filtre sonradan hiçbir yerden kurulamazdı.
    """
    ledger = Ledger(tmp_path)
    strategy = _EveryBar()
    config = _config()

    _engine(strategy, ledger, config).run_round(_market(1))
    report = _engine(strategy, Ledger(tmp_path), config).run_round(_market(4))

    emitted = report.by_model("her_bar").emitted  # type: ignore[union-attr]
    assert [record.bar for record in emitted] == list(INDEX[1:4])
    assert [record.fills_at for record in emitted] == list(INDEX[2:5])
    # Her kayıt KENDİ barının kapanışını taşır, turun `as_of` kapanışını değil.
    assert [record.close for record in emitted] == [101.0, 102.0, 103.0]
