"""strategies/ensemble.py: konsensüs meta-modeli ve is_meta yolunun canlı doğrulaması.

Testler iki şeyi ayrı ayrı çiviler:

1. **Oylama sözleşmesi** — havuz (random_ctrl/buyhold oy VERMEZ), eşik, zıt yön çakışması,
   short-only modellerin eşit ağırlığı, en geniş stop ve en yakın hedef. Bunların her biri
   bozulduğunda model sessizce başka bir şey ölçmeye başlar: ağırlıklı bir ensemble,
   kısmen rastgele bir ensemble ya da "hiç kimse itiraz etmedi" modeli.
2. **İki geçişli turun gerçekten çalıştığı** — `is_meta` yolu bugüne kadar canlı bir modelle
   hiç kullanılmadı. Buradaki entegrasyon testleri sahte bir meta ile değil, GERÇEK Ensemble
   ile koşar: normal modeller bittikten sonra o turun sinyalleri metaya kopya olarak gidiyor
   mu, metalar birbirini görüyor mu, konsensüs gerçekten dolan bir emre dönüşüyor mu.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Mapping, Sequence

import pandas as pd
import pytest

from core.config import load_config
from core.engine import Engine
from core.ledger import Ledger
from core.validate import validate_signal
from strategies.base import Direction, MarketData, Signal, Strategy, TakeProfit
from strategies.ensemble import MIN_VOTES, VOTER_POOL, Ensemble
from tests.helpers_market import frame, market

SYMBOL = "BTC-USDT-SWAP"
OTHER = "ETH-USDT-SWAP"
START = pd.Timestamp("2026-01-01 00:00:00", tz="UTC")


def _market(symbols: Sequence[str] = (SYMBOL, OTHER)) -> MarketData:
    """Her sembolün `as_of` barını taşıdığı düz bir anlık görüntü.

    Ensemble fiyata bakmaz (seviyeleri akranlardan devralır); çerçeveler yalnızca "sembol bu
    turun anlık görüntüsünde var mı" kontrolü ve doğrulamanın referans fiyatı için gerekli.
    """
    return market({symbol: frame([100.0] * 30) for symbol in symbols})


def _signal(
    symbol: str = SYMBOL,
    direction: Direction = "long",
    *,
    stop: float = 95.0,
    take_profits: tuple[TakeProfit, ...] = (),
) -> Signal:
    return Signal(
        symbol=symbol,
        direction=direction,
        stop_price=stop,
        take_profits=take_profits,
        reason="akran",
    )


def _run(peers: Mapping[str, tuple[Signal, ...]], *, data: MarketData | None = None) -> list[Signal]:
    return Ensemble().generate_signals(data if data is not None else _market(), peers)


def _tail(reason: str, key: str) -> str:
    """`reason`ın ayrıştırılabilir kuyruğundan bir alanı okur (defterden gruplama biçimi)."""
    for part in reason.split("|"):
        name, _, value = part.strip().partition("=")
        if name == key:
            return value
    raise AssertionError(f"{key!r} alanı reason içinde yok: {reason!r}")


# --------------------------------------------------------------------------- #
# Eşik: iki AYRI model
# --------------------------------------------------------------------------- #
def test_two_models_in_the_same_direction_open_a_position() -> None:
    (signal,) = _run({"trend": (_signal(),), "squeeze": (_signal(),)})

    assert signal.symbol == SYMBOL
    assert signal.direction == "long"
    assert _tail(signal.reason, "voters") == "squeeze,trend"  # sıralı: koşu sırasından bağımsız


def test_a_single_vote_is_not_consensus() -> None:
    assert _run({"trend": (_signal(),)}) == []


def test_one_model_voting_twice_is_still_one_vote() -> None:
    """Ölçülen şey modellerin üst üste binmesi; aynı modelin sinyal sayısı değil."""
    assert _run({"trend": (_signal(stop=95.0), _signal(stop=94.0))}) == []


def test_ensemble_has_no_signal_logic_of_its_own() -> None:
    """Akranlar susunca model de susar: kendi kapısı yoktur."""
    assert _run({"trend": (), "squeeze": ()}) == []


def test_min_votes_is_two() -> None:
    assert MIN_VOTES == 2


# --------------------------------------------------------------------------- #
# Sınır durumu 1: random_ctrl ve buyhold oy VERMEZ
# --------------------------------------------------------------------------- #
def test_random_ctrl_and_buyhold_are_not_in_the_voter_pool() -> None:
    assert "random_ctrl" not in VOTER_POOL
    assert "buyhold" not in VOTER_POOL
    assert set(VOTER_POOL) == {
        "trend", "meanrev", "momentum", "squeeze", "confluence",
        "failed_breakout", "downtrend_rally", "avwap",
    }


def test_random_ctrl_vote_cannot_complete_a_consensus() -> None:
    """Kontrol grubunun bilgisiz sinyali sayılsaydı ensemble kısmen rastgele olurdu."""
    assert _run({"trend": (_signal(),), "random_ctrl": (_signal(),)}) == []


def test_benchmark_vote_cannot_complete_a_consensus() -> None:
    assert _run({"meanrev": (_signal(),), "buyhold": (_signal(),)}) == []


def test_two_excluded_models_alone_produce_nothing() -> None:
    assert _run({"random_ctrl": (_signal(),), "buyhold": (_signal(),)}) == []


# --------------------------------------------------------------------------- #
# Sınır durumu 2: zıt yönler
# --------------------------------------------------------------------------- #
def test_both_directions_reaching_the_threshold_means_no_trade(
    caplog: pytest.LogCaptureFixture,
) -> None:
    peers = {
        "trend": (_signal(direction="long"),),
        "squeeze": (_signal(direction="long"),),
        "failed_breakout": (_signal(direction="short", stop=105.0),),
        "downtrend_rally": (_signal(direction="short", stop=106.0),),
    }
    with caplog.at_level(logging.INFO, logger="strategies.ensemble"):
        assert _run(peers) == []

    # Atlama sessiz olamaz: hangi sembolde kimin ne oyladığı sonradan denetlenebilmeli.
    assert any(SYMBOL in record.getMessage() for record in caplog.records)


def test_a_lone_dissenter_does_not_veto_the_consensus() -> None:
    """Eşiği geçmeyen tek karşı oy bir çakışma değildir; aksi hâlde model "hiç kimse itiraz
    etmedi"yi ölçmeye başlardı."""
    (signal,) = _run(
        {
            "trend": (_signal(direction="long"),),
            "squeeze": (_signal(direction="long"),),
            "failed_breakout": (_signal(direction="short", stop=105.0),),
        }
    )

    assert signal.direction == "long"
    assert _tail(signal.reason, "voters") == "squeeze,trend"


def test_conflict_on_one_symbol_does_not_block_another() -> None:
    peers = {
        "trend": (_signal(direction="long"), _signal(OTHER, "long")),
        "squeeze": (_signal(direction="long"), _signal(OTHER, "long")),
        "failed_breakout": (_signal(direction="short", stop=105.0),),
        "downtrend_rally": (_signal(direction="short", stop=106.0),),
    }
    (signal,) = _run(peers)

    assert signal.symbol == OTHER


# --------------------------------------------------------------------------- #
# Sınır durumu 3: short-only modeller eşit ağırlıkta
# --------------------------------------------------------------------------- #
def test_two_short_only_models_are_a_full_consensus() -> None:
    (signal,) = _run(
        {
            "failed_breakout": (_signal(direction="short", stop=105.0),),
            "downtrend_rally": (_signal(direction="short", stop=106.0),),
        }
    )

    assert signal.direction == "short"
    assert _tail(signal.reason, "voters") == "downtrend_rally,failed_breakout"


def test_short_only_vote_weighs_the_same_as_a_two_sided_model() -> None:
    """Ağırlıklandırma yok: karışık bir çift de saf short-only bir çift kadar konsensüstür."""
    mixed = _run(
        {
            "failed_breakout": (_signal(direction="short", stop=105.0),),
            "trend": (_signal(direction="short", stop=105.0),),
        }
    )
    pure = _run(
        {
            "failed_breakout": (_signal(direction="short", stop=105.0),),
            "downtrend_rally": (_signal(direction="short", stop=105.0),),
        }
    )

    assert len(mixed) == len(pure) == 1
    assert mixed[0].stop_price == pure[0].stop_price


# --------------------------------------------------------------------------- #
# Sınır durumu 4: seviyeler — en geniş stop, en yakın hedef
# --------------------------------------------------------------------------- #
def test_long_takes_the_widest_stop() -> None:
    (signal,) = _run({"trend": (_signal(stop=97.0),), "squeeze": (_signal(stop=93.0),)})

    assert signal.stop_price == 93.0  # en muhafazakâr = en uzak
    assert _tail(signal.reason, "stop_from") == "squeeze"


def test_short_takes_the_widest_stop() -> None:
    (signal,) = _run(
        {
            "failed_breakout": (_signal(direction="short", stop=103.0),),
            "downtrend_rally": (_signal(direction="short", stop=108.0),),
        }
    )

    assert signal.stop_price == 108.0
    assert _tail(signal.reason, "stop_from") == "downtrend_rally"


def test_long_takes_the_nearest_target_as_a_single_full_exit() -> None:
    peers = {
        "trend": (_signal(take_profits=(TakeProfit(price=120.0, fraction=1.0),)),),
        "squeeze": (
            _signal(
                take_profits=(
                    TakeProfit(price=108.0, fraction=0.5),
                    TakeProfit(price=130.0, fraction=0.5),
                ),
            ),
        ),
    }
    (signal,) = _run(peers)

    # Kademeler BİRLEŞTİRİLMEZ: en yakın hedef, tek TP, tamamı.
    assert signal.take_profits == (TakeProfit(price=108.0, fraction=1.0),)
    assert _tail(signal.reason, "tp_from") == "squeeze"


def test_short_takes_the_nearest_target() -> None:
    peers = {
        "failed_breakout": (
            _signal(direction="short", stop=105.0, take_profits=(TakeProfit(price=80.0, fraction=1.0),)),
        ),
        "downtrend_rally": (
            _signal(direction="short", stop=106.0, take_profits=(TakeProfit(price=94.0, fraction=0.4),)),
        ),
    }
    (signal,) = _run(peers)

    assert signal.take_profits == (TakeProfit(price=94.0, fraction=1.0),)
    assert _tail(signal.reason, "tp_from") == "downtrend_rally"


def test_no_participant_target_means_no_take_profit() -> None:
    (signal,) = _run({"trend": (_signal(),), "squeeze": (_signal(),)})

    assert signal.take_profits == ()
    assert _tail(signal.reason, "tp_from") == "none"


def test_no_trailing_is_requested() -> None:
    """Trailing üçüncü bir birleştirme kuralı olurdu; model kendi çıkış tezini eklemez."""
    peers = {
        "trend": (Signal(symbol=SYMBOL, direction="long", stop_price=95.0, trailing_atr=1.0),),
        "squeeze": (Signal(symbol=SYMBOL, direction="long", stop_price=94.0, trailing_atr=2.0),),
    }
    (signal,) = _run(peers)

    assert signal.trailing_atr is None


# --------------------------------------------------------------------------- #
# Sözleşme: boyutlandırma, doğrulama kapısı, determinizm
# --------------------------------------------------------------------------- #
def test_signal_uses_the_shared_risk_sizing() -> None:
    """Meta model de kendi boyutunu belirlemez (kural 3/11/15)."""
    (signal,) = _run({"trend": (_signal(),), "squeeze": (_signal(),)})

    assert signal.sizing == "risk"
    assert signal.notional_fraction is None


def test_generated_signal_passes_the_validation_gate() -> None:
    strategy = Ensemble()
    data = _market()
    peers = {
        "trend": (_signal(take_profits=(TakeProfit(price=120.0, fraction=1.0),)),),
        "squeeze": (_signal(stop=93.0, take_profits=(TakeProfit(price=110.0, fraction=0.5),)),),
    }
    for signal in strategy.generate_signals(data, peers):
        validate_signal(
            signal,
            entry_price=float(data.ohlcv[signal.symbol].loc[data.as_of, "close"]),
            allowed_directions=list(strategy.allowed_directions),
            symbol_universe=list(data.ohlcv),
            is_benchmark=strategy.is_benchmark,
        )


def test_symbols_are_emitted_in_sorted_order() -> None:
    """Sinyal sırası max_positions dolduğunda kimin girdiğini belirler: akran sırasına bağlanamaz."""
    peers = {
        "trend": (_signal(OTHER, "long"), _signal(SYMBOL, "long")),
        "squeeze": (_signal(SYMBOL, "long"), _signal(OTHER, "long")),
    }
    assert [signal.symbol for signal in _run(peers)] == sorted([SYMBOL, OTHER])


def test_symbol_missing_from_the_snapshot_is_skipped_and_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    peers = {"trend": (_signal("SOL-USDT-SWAP"),), "squeeze": (_signal("SOL-USDT-SWAP"),)}
    with caplog.at_level(logging.INFO, logger="strategies.ensemble"):
        assert _run(peers, data=_market([SYMBOL])) == []

    assert any("SOL-USDT-SWAP" in record.getMessage() for record in caplog.records)


def test_missing_peer_signals_is_a_warning_not_a_quiet_empty_round(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """`peer_signals=None` "konsensüs yoktu" değil, "meta yolu bağlı değil" demektir."""
    with caplog.at_level(logging.WARNING, logger="strategies.ensemble"):
        assert Ensemble().generate_signals(_market(), None) == []

    assert any(record.levelno == logging.WARNING for record in caplog.records)


def test_empty_peer_round_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="strategies.ensemble"):
        assert _run({"random_ctrl": (_signal(),)}) == []

    assert caplog.records


# --------------------------------------------------------------------------- #
# İki geçişli turun canlı doğrulaması (kural 4)
# --------------------------------------------------------------------------- #
ROWS = [
    (100.0, 101.0, 99.0, 100.0),   # bar0 — sinyaller burada üretilir
    (102.0, 103.0, 101.0, 102.0),  # bar1 — dolum burada (kural 13)
]


class _Voter(Strategy):
    """Belirli bir barda önceden yazılmış sinyal üreten sahte NORMAL model."""

    is_meta = False

    def __init__(self, name: str, signals: Sequence[Signal]) -> None:
        self.name = name
        self.allowed_directions: list[Direction] = ["long", "short"]
        self._signals = list(signals)
        self.saw_peer_signals: list[Any] = []

    def generate_signals(
        self, market: MarketData, peer_signals: Mapping[str, tuple[Signal, ...]] | None = None
    ) -> list[Signal]:
        self.saw_peer_signals.append(peer_signals)
        return list(self._signals) if market.as_of == START else []


class _SpyMeta(Strategy):
    """Ensemble'ın yanında koşan ikinci bir meta: metalar birbirini görmemeli."""

    is_meta = True

    def __init__(self, name: str) -> None:
        self.name = name
        self.allowed_directions: list[Direction] = ["long", "short"]
        self.peers: Mapping[str, tuple[Signal, ...]] | None = None

    def generate_signals(
        self, market: MarketData, peer_signals: Mapping[str, tuple[Signal, ...]] | None = None
    ) -> list[Signal]:
        self.peers = peer_signals
        return [Signal(symbol=SYMBOL, direction="long", stop_price=90.0, reason="spy")]


def _engine_market(bars: int) -> MarketData:
    index = pd.date_range(START, periods=len(ROWS), freq="4h", tz="UTC", name="ts")
    data = pd.DataFrame(list(ROWS), index=index, columns=["open", "high", "low", "close"])
    data["volume"] = 1.0
    data = data.head(bars)
    return MarketData(ohlcv={SYMBOL: data}, btc=data, funding={}, as_of=data.index[-1])


def _config(**overrides: Any) -> dict[str, Any]:
    config = load_config()
    config.update(fee_rate=0.0, slippage_base=0.0, slippage_short_stop=0.0)
    config.update(overrides)
    return config


def test_ensemble_reads_this_round_signals_through_the_engine(tmp_path: Path) -> None:
    """is_meta yolunun uçtan uca canlı doğrulaması: oy -> konsensüs -> dolan emir."""
    voters = [
        _Voter("trend", [Signal(symbol=SYMBOL, direction="long", stop_price=96.0, reason="a")]),
        _Voter("squeeze", [Signal(symbol=SYMBOL, direction="long", stop_price=94.0, reason="b")]),
    ]
    ledger = Ledger(tmp_path)
    engine = Engine([*voters, Ensemble()], config=_config(), ledger=ledger)

    first = engine.run_round(_engine_market(bars=1))
    ensemble_report = next(item for item in first.models if item.model == "ensemble")
    assert ensemble_report.signals == 1  # iki oy, tek konsensüs sinyali

    engine.run_round(_engine_market(bars=2))
    (position,) = ledger.load_state("ensemble")["positions"]  # type: ignore[index]
    assert position["direction"] == "long"
    assert position["stop_price"] == 94.0  # en geniş katılımcı stopu devralındı


def test_normal_voters_never_receive_peer_signals(tmp_path: Path) -> None:
    voter = _Voter("trend", [Signal(symbol=SYMBOL, direction="long", stop_price=96.0, reason="a")])
    engine = Engine([voter, Ensemble()], config=_config(), ledger=Ledger(tmp_path))

    engine.run_round(_engine_market(bars=1))

    assert voter.saw_peer_signals == [None]


def test_meta_models_cannot_see_each_other(tmp_path: Path) -> None:
    """Metaların birbirini okuması sonucu çalışma sırasına bağlardı (kim önce koştuysa avantajlı)."""
    voters = [
        _Voter("trend", [Signal(symbol=SYMBOL, direction="long", stop_price=96.0, reason="a")]),
        _Voter("squeeze", [Signal(symbol=SYMBOL, direction="long", stop_price=94.0, reason="b")]),
    ]
    spy = _SpyMeta("spy")
    # spy listede Ensemble'dan ÖNCE: sırası ne olursa olsun meta geçişine giren küme aynı.
    engine = Engine([voters[0], spy, voters[1], Ensemble()], config=_config(), ledger=Ledger(tmp_path))

    engine.run_round(_engine_market(bars=1))

    assert spy.peers is not None
    assert set(spy.peers) == {"trend", "squeeze"}  # ne kendisi ne de ensemble görünür


def test_ensemble_does_not_count_a_meta_peer_as_a_vote(tmp_path: Path) -> None:
    """Tek normal oy + bir meta sinyali konsensüs DEĞİLDİR: meta'lar peer kümesine hiç girmez."""
    voter = _Voter("trend", [Signal(symbol=SYMBOL, direction="long", stop_price=96.0, reason="a")])
    engine = Engine([voter, _SpyMeta("spy"), Ensemble()], config=_config(), ledger=Ledger(tmp_path))

    report = engine.run_round(_engine_market(bars=1))

    assert next(item for item in report.models if item.model == "ensemble").signals == 0


def test_peer_signals_are_a_read_only_copy(tmp_path: Path) -> None:
    """Kopya olmasaydı bir meta, akranın sinyalini yerinde bozabilirdi (kural 4)."""
    voter = _Voter("trend", [Signal(symbol=SYMBOL, direction="long", stop_price=96.0, reason="a")])
    spy = _SpyMeta("spy")
    engine = Engine([voter, spy, Ensemble()], config=_config(), ledger=Ledger(tmp_path))

    engine.run_round(_engine_market(bars=1))

    assert spy.peers is not None
    assert spy.peers["trend"][0] is not voter._signals[0]
    with pytest.raises(TypeError):
        spy.peers["trend"] = ()  # type: ignore[index]
