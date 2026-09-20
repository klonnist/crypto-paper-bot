"""Üç aşamalı çıkış yönetimi: breakeven, kısmi çıkış + stop kaydırma, geri verme takibi.

Bu dosya MOTOR YETENEĞİNİ çiviler, bir modeli değil. Yetenek opsiyoneldir ve varsayılanı
kapalıdır; bu yüzden ilk sınanan şey "alan doldurulmadığında hiçbir şeyin değişmediği"dir
(mevcut on üç model bu eklemeden etkilenmemelidir).

Sayılar elle doğrulanabilir kalsın diye maliyetler sıfırlanır; maliyetin kendisi
tests/test_portfolio.py'de gerçek config değerleriyle sınanır.
"""

from __future__ import annotations

from typing import Any

import pandas as pd
import pytest

from core.config import load_config
from core.engine import _breakeven_stop, _giveback_stop
from core.portfolio import Bar, Portfolio
from strategies.base import PartialTakeProfit, TakeProfit

TS = pd.Timestamp("2026-01-01 00:00:00", tz="UTC")
SYMBOL = "BTC-USDT-SWAP"


def _frictionless(**overrides: Any) -> dict[str, Any]:
    config = load_config()
    config.update(
        fee_rate=0.0, slippage_base=0.0, slippage_short_stop=0.0, **overrides
    )
    return config


def _bar(open_: float, high: float, low: float, close: float) -> Bar:
    return Bar(open=open_, high=high, low=low, close=close)


def _open(
    portfolio: Portfolio,
    *,
    direction: str = "long",
    stop_price: float = 95.0,
    reference_price: float = 100.0,
    **kwargs: Any,
) -> Any:
    result = portfolio.open_position(
        "m",
        symbol=SYMBOL,
        direction=direction,  # type: ignore[arg-type]
        stop_price=stop_price,
        reference_price=reference_price,
        ts=TS,
        marks={SYMBOL: reference_price},
        **kwargs,
    )
    assert result.position is not None, result.rejected
    return result.position


# --------------------------------------------------------------------------- #
# Varsayılan KAPALI: doldurulmayan alan hiçbir şeyi değiştirmez
# --------------------------------------------------------------------------- #
def test_position_without_management_behaves_exactly_as_before() -> None:
    """Mevcut modellerin sözleşmesi: alan doldurulmazsa stop da akış da değişmez."""
    portfolio = Portfolio(_frictionless())
    position = _open(portfolio, take_profits=(TakeProfit(price=110.0, fraction=1.0),))

    assert position.breakeven_at_r is None
    assert position.partial_tp is None
    assert position.trail_giveback_pct is None
    assert _breakeven_stop(position) is None
    assert _giveback_stop(position) is None

    # 3R'lık lehte hareket: yönetim kapalı olduğu için stop yerinde kalır.
    portfolio.process_bar("m", ts=TS, bars={SYMBOL: _bar(100.0, 109.0, 99.0, 108.0)})
    assert position.stop_price == pytest.approx(95.0)


# --------------------------------------------------------------------------- #
# 1) Breakeven
# --------------------------------------------------------------------------- #
def test_breakeven_moves_the_stop_to_entry_once_the_level_is_reached() -> None:
    portfolio = Portfolio(_frictionless())
    position = _open(portfolio, breakeven_at_r=1.0)  # 1R = 5 birim -> 105

    # 0.8R: henüz ulaşılmadı.
    portfolio.process_bar("m", ts=TS, bars={SYMBOL: _bar(100.0, 104.0, 99.0, 103.0)})
    assert _breakeven_stop(position) is None

    # 1.2R: ulaşıldı; ölçü mumun ZİRVESİ (kapanış değil) — soru "ulaştı mı", "şu an
    # orada mı" değil.
    portfolio.process_bar("m", ts=TS, bars={SYMBOL: _bar(103.0, 106.0, 102.0, 102.5)})
    assert _breakeven_stop(position) == pytest.approx(100.0)
    assert portfolio.set_stop_price(
        "m", symbol=SYMBOL, direction="long", stop_price=_breakeven_stop(position)
    )
    assert position.stop_price == pytest.approx(100.0)


def test_breakeven_is_symmetric_for_shorts() -> None:
    portfolio = Portfolio(_frictionless())
    position = _open(portfolio, direction="short", stop_price=105.0, breakeven_at_r=1.0)

    portfolio.process_bar("m", ts=TS, bars={SYMBOL: _bar(100.0, 101.0, 94.0, 95.0)})
    assert _breakeven_stop(position) == pytest.approx(100.0)


def test_breakeven_never_loosens_an_already_tighter_stop() -> None:
    """Stop yalnızca sıkışır: breakeven, daha ileri çekilmiş bir stop'u geri almaz."""
    portfolio = Portfolio(_frictionless())
    position = _open(portfolio, breakeven_at_r=1.0)
    portfolio.process_bar("m", ts=TS, bars={SYMBOL: _bar(100.0, 108.0, 99.0, 107.0)})
    position.stop_price = 104.0

    assert not portfolio.set_stop_price(
        "m", symbol=SYMBOL, direction="long", stop_price=_breakeven_stop(position)
    )
    assert position.stop_price == pytest.approx(104.0)


# --------------------------------------------------------------------------- #
# 2) Kısmi çıkış: dolum + stop kaydırma
# --------------------------------------------------------------------------- #
def test_partial_exit_closes_the_fraction_and_pulls_the_stop_to_that_level() -> None:
    portfolio = Portfolio(_frictionless())
    position = _open(
        portfolio,
        partial_tp=PartialTakeProfit(r=1.5, fraction=0.5),
        take_profits=(TakeProfit(price=120.0, fraction=1.0),),
    )
    initial_qty = position.initial_qty

    # 1.5R = 100 + 1.5 × 5 = 107.5
    trades = portfolio.process_bar("m", ts=TS, bars={SYMBOL: _bar(100.0, 108.0, 99.5, 107.0)})

    assert [trade.exit_reason for trade in trades] == ["partial"]
    assert trades[0].qty == pytest.approx(initial_qty * 0.5)
    assert trades[0].exit_price == pytest.approx(107.5)
    assert position.qty == pytest.approx(initial_qty * 0.5)
    assert position.partial_done is True
    # Stop kısmi çıkış SEVİYESİNE çekildi (kayan dolum fiyatına değil).
    assert position.stop_price == pytest.approx(107.5)


def test_partial_exit_level_is_symmetric_for_shorts() -> None:
    portfolio = Portfolio(_frictionless())
    position = _open(
        portfolio,
        direction="short",
        stop_price=105.0,
        partial_tp=PartialTakeProfit(r=1.5, fraction=0.5),
    )

    trades = portfolio.process_bar("m", ts=TS, bars={SYMBOL: _bar(100.0, 100.5, 92.0, 93.0)})

    assert [trade.exit_reason for trade in trades] == ["partial"]
    assert trades[0].exit_price == pytest.approx(92.5)
    assert position.stop_price == pytest.approx(92.5)


def test_partial_exit_fires_only_once() -> None:
    portfolio = Portfolio(_frictionless())
    position = _open(portfolio, partial_tp=PartialTakeProfit(r=1.5, fraction=0.5))

    portfolio.process_bar("m", ts=TS, bars={SYMBOL: _bar(100.0, 108.0, 99.5, 107.0)})
    remaining = position.qty
    trades = portfolio.process_bar("m", ts=TS, bars={SYMBOL: _bar(107.0, 112.0, 107.6, 111.0)})

    assert trades == []
    assert position.qty == pytest.approx(remaining)


def test_the_moved_stop_is_not_rechecked_inside_the_same_bar() -> None:
    """Kısmi dolumdan SONRA verilen stop, o mumun daha önceki hareketinde piyasada değildi.

    Aynı mumda yeniden kontrol etmek, kısmi çıkışı her seferinde anında tam çıkışa çevirir
    ve mekanizmayı ölçülemez kılardı (bkz. core/portfolio.py modül docstring'i).
    """
    portfolio = Portfolio(_frictionless())
    position = _open(portfolio, partial_tp=PartialTakeProfit(r=1.5, fraction=0.5))

    # Mum 1.5R'ı geçiyor ama sonra 101'e kadar geri düşüyor: yeni stop (107.5) bu mumda
    # tetiklenmez, kalan pozisyon açık kalır.
    trades = portfolio.process_bar("m", ts=TS, bars={SYMBOL: _bar(100.0, 108.0, 101.0, 101.5)})

    assert [trade.exit_reason for trade in trades] == ["partial"]
    assert position.qty > 0.0

    # BİR SONRAKİ mumda aynı seviyeye dokunmak ise stop'u tetikler.
    trades = portfolio.process_bar("m", ts=TS, bars={SYMBOL: _bar(101.5, 102.0, 100.0, 100.5)})
    assert [trade.exit_reason for trade in trades] == ["stop"]


# --------------------------------------------------------------------------- #
# 3) Mum içi sıralama: likidasyon -> stop -> kısmi -> TP
# --------------------------------------------------------------------------- #
def test_stop_wins_over_partial_in_the_same_bar() -> None:
    """Kural 13: mum içi sıralama bilinemez, KÖTÜ olan gerçekleşmiş varsayılır."""
    portfolio = Portfolio(_frictionless())
    position = _open(portfolio, partial_tp=PartialTakeProfit(r=1.5, fraction=0.5))

    trades = portfolio.process_bar("m", ts=TS, bars={SYMBOL: _bar(100.0, 108.0, 94.0, 96.0)})

    assert [trade.exit_reason for trade in trades] == ["stop"]
    assert position.partial_done is False


def test_partial_comes_before_take_profit_in_the_same_bar() -> None:
    """Aynı mumda ikisi de dolduğunda önce kısmi dilim, sonra kalanın hedefi kapanır."""
    portfolio = Portfolio(_frictionless())
    _open(
        portfolio,
        partial_tp=PartialTakeProfit(r=1.5, fraction=0.5),
        take_profits=(TakeProfit(price=110.0, fraction=1.0),),
    )

    trades = portfolio.process_bar("m", ts=TS, bars={SYMBOL: _bar(100.0, 111.0, 99.5, 110.5)})

    assert [trade.exit_reason for trade in trades] == ["partial", "tp"]
    assert trades[0].exit_price == pytest.approx(107.5)
    assert trades[1].exit_price == pytest.approx(110.0)
    assert portfolio.positions("m") == ()


def test_liquidation_still_comes_first() -> None:
    """Likidasyon her şeyden önce gelir; yönetim alanları bu sırayı değiştirmez."""
    portfolio = Portfolio(_frictionless())
    position = _open(
        portfolio,
        stop_price=95.0,
        partial_tp=PartialTakeProfit(r=1.5, fraction=0.5),
        breakeven_at_r=1.0,
    )
    liq = position.liq_price

    trades = portfolio.process_bar(
        "m", ts=TS, bars={SYMBOL: _bar(100.0, 108.0, liq - 1.0, liq - 0.5)}
    )

    assert [trade.exit_reason for trade in trades] == ["liquidation"]


# --------------------------------------------------------------------------- #
# 4) Geri verme takibi
# --------------------------------------------------------------------------- #
def test_giveback_trail_is_inactive_before_the_partial_exit() -> None:
    portfolio = Portfolio(_frictionless())
    position = _open(
        portfolio,
        partial_tp=PartialTakeProfit(r=1.5, fraction=0.5),
        trail_giveback_pct=0.5,
        take_profits=(TakeProfit(price=130.0, fraction=1.0),),
    )
    # 1R ilerledi ama kısmi seviyeye (1.5R) değmedi: takip henüz devrede değil.
    portfolio.process_bar("m", ts=TS, bars={SYMBOL: _bar(100.0, 105.0, 99.5, 104.0)})

    assert position.partial_done is False
    assert _giveback_stop(position) is None


def test_giveback_trail_gives_back_at_most_the_configured_share() -> None:
    portfolio = Portfolio(_frictionless())
    position = _open(
        portfolio,
        partial_tp=PartialTakeProfit(r=1.5, fraction=0.5),
        trail_giveback_pct=0.5,
        take_profits=(TakeProfit(price=130.0, fraction=1.0),),
    )
    # Kısmi dolar (107.5) ve mum 4R'a (120) kadar yükselir.
    portfolio.process_bar("m", ts=TS, bars={SYMBOL: _bar(100.0, 120.0, 99.5, 119.0)})

    assert position.partial_done is True
    # En iyi hareket 4R; yarısı geri verilir -> stop 2R = 110.
    assert _giveback_stop(position) == pytest.approx(110.0)


def test_giveback_trail_never_passes_the_original_target() -> None:
    """Takip eden stop hedefi aşarsa hedef hiç dolmaz ve her işlem stop'la kapanırdı."""
    portfolio = Portfolio(_frictionless())
    position = _open(
        portfolio,
        partial_tp=PartialTakeProfit(r=1.5, fraction=0.5),
        trail_giveback_pct=0.1,  # kazancın yalnızca %10'u geri verilir
        take_profits=(TakeProfit(price=112.0, fraction=1.0),),
    )
    assert position.final_target_price == pytest.approx(112.0)

    # Mum 111.9'a kadar çıkar (hedefe değmeden): ham takip 111.71 ister ama hedefin
    # ötesine geçmesi engellenmelidir — burada henüz altında.
    portfolio.process_bar("m", ts=TS, bars={SYMBOL: _bar(100.0, 111.9, 99.5, 111.0)})
    raw = 100.0 + (111.9 - 100.0) * 0.9
    assert _giveback_stop(position) == pytest.approx(min(raw, 112.0))

    # Hedefin çok ötesine taşan bir zirvede tavan devreye girer.
    position.high_water = 200.0
    assert _giveback_stop(position) == pytest.approx(112.0)


def test_giveback_trail_is_symmetric_for_shorts() -> None:
    portfolio = Portfolio(_frictionless())
    position = _open(
        portfolio,
        direction="short",
        stop_price=105.0,
        partial_tp=PartialTakeProfit(r=1.5, fraction=0.5),
        trail_giveback_pct=0.5,
        take_profits=(TakeProfit(price=70.0, fraction=1.0),),
    )
    portfolio.process_bar("m", ts=TS, bars={SYMBOL: _bar(100.0, 100.5, 80.0, 81.0)})

    assert position.partial_done is True
    # En iyi hareket 4R (100 -> 80); yarısı geri verilir -> stop 2R = 90.
    assert _giveback_stop(position) == pytest.approx(90.0)


def test_giveback_trail_without_a_target_has_no_ceiling() -> None:
    """Hedefi olmayan bir sinyalde aşılacak bir seviye de yoktur."""
    portfolio = Portfolio(_frictionless())
    position = _open(
        portfolio,
        partial_tp=PartialTakeProfit(r=1.5, fraction=0.5),
        trail_giveback_pct=0.5,
    )
    portfolio.process_bar("m", ts=TS, bars={SYMBOL: _bar(100.0, 120.0, 99.5, 119.0)})

    assert position.final_target_price is None
    assert _giveback_stop(position) == pytest.approx(110.0)


# --------------------------------------------------------------------------- #
# Kalıcılık: istek koşular arası taşınır
# --------------------------------------------------------------------------- #
def test_management_fields_survive_a_state_round_trip() -> None:
    """Emir bir sonraki turda dolar (kural 13): yönetim isteği de defterde taşınmalı."""
    portfolio = Portfolio(_frictionless())
    _open(
        portfolio,
        breakeven_at_r=1.0,
        partial_tp=PartialTakeProfit(r=1.5, fraction=0.5),
        trail_giveback_pct=0.5,
        take_profits=(TakeProfit(price=130.0, fraction=1.0),),
    )
    state = portfolio.to_state("m")

    restored = Portfolio(_frictionless())
    restored.load_state("m", state)
    position = restored.positions("m")[0]

    assert position.breakeven_at_r == pytest.approx(1.0)
    assert position.partial_tp == PartialTakeProfit(r=1.5, fraction=0.5)
    assert position.trail_giveback_pct == pytest.approx(0.5)
    assert position.final_target_price == pytest.approx(130.0)
    assert position.partial_done is False


def test_old_ledgers_without_the_fields_load_as_management_off() -> None:
    """Şema göçü: alanı olmayan eski satır "yönetim kapalı" olarak okunur, patlamaz."""
    portfolio = Portfolio(_frictionless())
    _open(portfolio)
    state = portfolio.to_state("m")
    for key in ("breakeven_at_r", "partial_tp", "trail_giveback_pct", "partial_done",
                "final_target_price"):
        state["positions"][0].pop(key)

    restored = Portfolio(_frictionless())
    restored.load_state("m", state)
    position = restored.positions("m")[0]

    assert position.breakeven_at_r is None
    assert position.partial_tp is None
    assert position.partial_done is False


# --------------------------------------------------------------------------- #
# Kısmi çıkışın ÇEKTİĞİ stop'un kimliği
# --------------------------------------------------------------------------- #
def test_stop_pulled_by_the_partial_is_labelled_on_the_closing_trade() -> None:
    """Kısmi çıkış stop'u çeker; o stop'un aldığı işlem "ilk stop aldı" gibi okunmamalı.

    Modeller 13/14/15'in ölçtüğü şey yönetimin katkısıdır: kısmi çıkıştan sonra
    kapanan pozisyonun çıkış kuralı defterden okunabilmeli.
    """
    portfolio = Portfolio(_frictionless())
    position = _open(portfolio, partial_tp=PartialTakeProfit(r=1.0, fraction=0.5))

    partials = portfolio.process_bar("m", ts=TS, bars={SYMBOL: _bar(100.0, 106.0, 99.0, 105.0)})
    assert [trade.exit_reason for trade in partials] == ["partial"]
    assert position.stop_rule == "partial"
    assert position.stop_price == pytest.approx(105.0)

    closes = portfolio.process_bar("m", ts=TS, bars={SYMBOL: _bar(105.0, 106.0, 104.0, 104.5)})
    assert [trade.exit_reason for trade in closes] == ["stop"]
    assert "exit_rule=partial" in closes[0].notes
