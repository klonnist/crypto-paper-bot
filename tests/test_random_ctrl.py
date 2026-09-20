"""strategies/random_ctrl.py: kontrol grubu — bilgisiz sinyalin referansı.

Bu modelin testleri "iyi sinyal üretiyor mu"yu değil, TASARIMININ BOZULMADIĞINI ölçer:
çekiliş bilgisizse ve maliyet/stop ölçeği diğerleriyle aynıysa, bir modelin ortalama R'si
bu satırdan ayrışıyorsa ayrışma sinyalden gelir. Bozulmanın üç kapısı test edilir: (a) RNG
sabit tohumla süreç başına kurulursa kontrol "bilgisiz" değil SABİT olur; (b) RNG ağ
jitter'ıyla paylaşılırsa aynı tur iki koşuda farklı sembol seçer; (c) elemeye bir görüş
(rejim, hacim) sızarsa kontrol sessizce bir stratejiye döner.
"""

from __future__ import annotations

import logging
import random

import pytest

from core.config import get_setting, load_config
from core.indicators import average_true_range
from core.validate import validate_signal
from strategies.base import MarketData, Signal
from strategies.random_ctrl import RandomControl
from tests.helpers_market import falling, flat, frame, market, rising

SYMBOLS = [f"SYM{index}-USDT-SWAP" for index in range(8)]


def _universe(bars: int = 60) -> dict:
    """Aynı kurulumun sekiz sembolü: farklı fiyat yolları, aynı kurulabilirlik."""
    paths = {
        symbol: frame(
            [100.0 + index + (0.7 if bar % 2 == 0 else -0.7) + bar * 0.05 for bar in range(bars)],
            spread=0.4,
        )
        for index, symbol in enumerate(SYMBOLS)
    }
    return paths


def _signals(ohlcv: dict | None = None, *, as_of=None) -> list[Signal]:
    frames = ohlcv if ohlcv is not None else _universe()
    return RandomControl().generate_signals(market(frames, as_of=as_of))


# --------------------------------------------------------------------------- #
# Sözleşme
# --------------------------------------------------------------------------- #
def test_is_a_competitor_not_a_benchmark() -> None:
    """Kontrol bir REFERANS değil yarışmacıdır: aynı sütunda, aynı boyutlandırmayla durur."""
    strategy = RandomControl()
    assert strategy.name == "random_ctrl"
    assert strategy.allowed_directions == ["long", "short"]
    assert strategy.is_benchmark is False
    assert strategy.is_meta is False


def test_registry_resolves_the_model_name() -> None:
    from strategies.registry import build

    assert isinstance(build("random_ctrl"), RandomControl)


def test_model_is_listed_in_config() -> None:
    assert "random_ctrl" in get_setting(load_config(), "models")


def test_signal_uses_the_shared_risk_sizing() -> None:
    signal = _signals()[0]
    assert signal.sizing == "risk"
    assert signal.notional_fraction is None


def test_one_draw_per_round() -> None:
    assert len(_signals()) == 1


def test_signal_passes_the_validation_gate() -> None:
    frames = _universe()
    signal = _signals(frames)[0]
    validate_signal(
        signal,
        entry_price=float(frames[signal.symbol]["close"].iloc[-1]),
        allowed_directions=RandomControl().allowed_directions,
        symbol_universe=list(frames),
        is_benchmark=False,
    )


# --------------------------------------------------------------------------- #
# Maliyet ölçeği: diğer modellerle BİREBİR aynı
# --------------------------------------------------------------------------- #
def test_stop_is_two_atr_from_the_close() -> None:
    """Kontrolün R ölçeği bandın dışında kalsaydı cost_per_r kolonu onu haksız gösterirdi."""
    frames = _universe()
    signal = _signals(frames)[0]
    candles = frames[signal.symbol]
    atr = average_true_range(candles, int(get_setting(load_config(), "trailing.atr_period")))
    assert atr is not None
    close = float(candles["close"].iloc[-1])
    expected = close - 2.0 * atr if signal.direction == "long" else close + 2.0 * atr
    assert signal.stop_price == pytest.approx(expected)


def test_the_model_asks_for_no_targets_and_no_trailing() -> None:
    """TP ya da trailing bir TEZDİR; bilgisiz sinyalin tezi olamaz."""
    signal = _signals()[0]
    assert signal.take_profits == ()
    assert signal.trailing_atr is None


def test_the_atr_period_comes_from_config_not_from_the_model() -> None:
    """Kendi periyodunu seçen bir kontrol, "2×ATR"yi diğer modellerden farklı bir mesafe yapardı."""
    strategy = RandomControl(config={**load_config(), "trailing": {"atr_period": 5}})
    frames = _universe()
    signal = strategy.generate_signals(market(frames))[0]
    candles = frames[signal.symbol]
    atr = average_true_range(candles, 5)
    assert atr is not None
    close = float(candles["close"].iloc[-1])
    expected = close - 2.0 * atr if signal.direction == "long" else close + 2.0 * atr
    assert signal.stop_price == pytest.approx(expected)


# --------------------------------------------------------------------------- #
# Tekrarlanabilirlik ve bağımsızlık
# --------------------------------------------------------------------------- #
def test_the_same_round_draws_the_same_signal_twice() -> None:
    """Aynı `as_of` ile yeniden koşulan tur birebir aynı sinyali üretmeli (random_seed ilkesi)."""
    frames = _universe()
    first = RandomControl().generate_signals(market(frames))[0]
    second = RandomControl().generate_signals(market(frames))[0]
    assert (first.symbol, first.direction, first.stop_price) == (
        second.symbol,
        second.direction,
        second.stop_price,
    )


def test_different_rounds_draw_independently() -> None:
    """Tohum tur bazlı karışmasaydı her tur aynı çekilişi yapardı: kontrol bilgisiz değil SABİT olurdu."""
    frames = _universe(bars=80)
    draws = {
        (signal.symbol, signal.direction)
        for index in range(1, 40)
        for signal in RandomControl().generate_signals(
            market(frames, as_of=frames[SYMBOLS[0]].index[-index])
        )
    }
    assert len(draws) > 1


def test_a_different_seed_changes_the_draw() -> None:
    """Çekiliş gerçekten tohumdan besleniyor mu: aynı veride farklı tohum farklı sonuç vermeli."""
    frames = _universe()
    base = load_config()
    draws = {
        RandomControl(config={**base, "random_seed": seed})
        .generate_signals(market(frames))[0]
        .symbol
        for seed in range(1, 30)
    }
    assert len(draws) > 1


def test_the_draw_does_not_consume_the_global_random_stream() -> None:
    """Ağ jitter'ı ile RNG paylaşmak, çekilişi borsanın o günkü keyfine bağlardı."""
    frames = _universe()
    random.seed(1234)
    before = random.random()
    random.seed(1234)
    RandomControl().generate_signals(market(frames))
    assert random.random() == before


def test_both_directions_are_drawn_over_many_rounds() -> None:
    """Tek yöne kilitlenmiş bir kontrol, long/short ayrıştırmasının referansı olamaz."""
    frames = _universe(bars=120)
    directions = {
        signal.direction
        for index in range(1, 60)
        for signal in RandomControl().generate_signals(
            market(frames, as_of=frames[SYMBOLS[0]].index[-index])
        )
    }
    assert directions == {"long", "short"}


# --------------------------------------------------------------------------- #
# Elemede görüş yok: yalnızca "işlem kurulabilir mi"
# --------------------------------------------------------------------------- #
def test_the_draw_ignores_trend_direction() -> None:
    """Rejim, hacim ya da "kötü sembol" filtresi kontrolü sessizce bir stratejiye çevirirdi."""
    frames = {
        "UP-USDT-SWAP": frame(rising(60), spread=0.4),
        "DOWN-USDT-SWAP": frame(falling(60), spread=0.4),
    }
    drawn = {
        signal.symbol
        for index in range(1, 30)
        for signal in RandomControl().generate_signals(
            market(frames, as_of=frames["UP-USDT-SWAP"].index[-index])
        )
    }
    assert drawn == {"UP-USDT-SWAP", "DOWN-USDT-SWAP"}


def test_a_symbol_without_the_as_of_bar_is_not_drawable() -> None:
    """Kural 5/12: bir bar geriden sinyal üretmek look-ahead kadar sessiz bir ölçüm hatasıdır."""
    frames = _universe()
    snapshot = MarketData(
        ohlcv={SYMBOLS[0]: frames[SYMBOLS[0]].iloc[:-1], SYMBOLS[1]: frames[SYMBOLS[1]]},
        btc=frames[SYMBOLS[1]],
        funding={},
        as_of=frames[SYMBOLS[1]].index[-1],
    )
    drawn = {
        signal.symbol for signal in RandomControl().generate_signals(snapshot) for _ in range(1)
    }
    assert drawn == {SYMBOLS[1]}


def test_a_symbol_without_an_atr_is_not_drawable(caplog: pytest.LogCaptureFixture) -> None:
    """Stop mesafesi üretilemeyen sembolde işlem KURULAMAZ; bu bir görüş değil, kurulabilirliktir."""
    frames = {"FLAT-USDT-SWAP": frame(flat(60), spread=0.0)}
    with caplog.at_level(logging.INFO, logger="strategies.random_ctrl"):
        signals = RandomControl().generate_signals(market(frames))
    assert signals == []
    assert any("çekiliş yapılmadı" in record.getMessage() for record in caplog.records)


def test_reason_marks_the_row_as_the_control_group() -> None:
    """Defteri okuyan biri bu satırın bir strateji olmadığını gerekçeden görmeli."""
    reason = _signals()[0].reason
    assert "KONTROL GRUBU" in reason
    assert "bilgisiz çekiliş" in reason
    assert "tohum" in reason
    assert "2×ATR(14)" in reason
