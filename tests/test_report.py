"""core/report.py: dashboard yükünün defterden doğru okunduğu ve HİÇBİR ŞEY UYDURMADIĞI.

Buradaki testlerin ortak derdi tek bir şey: sayfa güzel görünsün diye bir sayının
uydurulmaması. Fiyatı olmayan sembol kâr/zararda gösterilmez, stop'suz pozisyonun R'si
"0" olmaz, pencere duvar saatinden değil `as_of`'tan sayılır.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from core.config import load_config
from core.ledger import EQUITY_COLUMNS, TRADE_COLUMNS, Ledger
from core.metrics import compare
from core.report import (
    activity,
    build_dashboard,
    concentration,
    equity_series,
    marks_from_market,
    model_trades,
    open_positions,
    recent_trades,
)
from strategies.base import MarketData

AS_OF = pd.Timestamp("2026-03-10 12:00:00", tz="UTC")


def _market(**closes: float) -> MarketData:
    frames = {}
    for symbol, close in closes.items():
        index = pd.DatetimeIndex([AS_OF - pd.Timedelta(hours=4), AS_OF], name="ts")
        frames[symbol.replace("_", "-")] = pd.DataFrame(
            {"open": [close, close], "high": [close, close], "low": [close, close],
             "close": [close * 0.5, close], "volume": [1.0, 1.0]},
            index=index,
        )
    btc = next(iter(frames.values()), pd.DataFrame())
    return MarketData(ohlcv=frames, btc=btc, funding={}, as_of=AS_OF)


def _trade(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {column: "" for column in TRADE_COLUMNS}
    row.update(
        strategy="m", symbol="BTC-USDT-SWAP", direction="long",
        opened_at="2026-03-10T00:00:00+00:00", closed_at="2026-03-10T08:00:00+00:00",
        entry_price=100.0, exit_price=105.0, qty=1.0, notional=100.0, stop_price=97.0,
        risk_amount=3.0, leverage=1.0, margin=100.0, fee=0.2, slippage_cost=0.1,
        funding=0.0, pnl=4.7, exit_reason="take_profit", signal_reason="gerekçe", notes="",
    )
    row.update(overrides)
    return row


def _position(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "symbol": "BTC-USDT-SWAP", "direction": "long", "qty": 2.0, "initial_qty": 2.0,
        "entry_price": 100.0, "stop_price": 97.0, "initial_stop_price": 97.0,
        "opened_at": "2026-03-10T08:00:00+00:00", "margin": 50.0, "leverage": 4.0,
        "entry_fee": 0.2, "entry_slippage": 0.1, "liq_price": 80.0,
        "high_water": 105.0, "low_water": 99.0, "take_profits": [], "trailing_atr": 1.0,
        "funding": -0.5, "reason": "kurulum gerekçesi", "notes": "",
    }
    payload.update(overrides)
    return payload


def _ledger(tmp_path: Path, model: str, *, trades: list[dict[str, Any]] | None = None,
            positions: list[dict[str, Any]] | None = None,
            equity: list[tuple[str, float]] | None = None) -> Ledger:
    ledger = Ledger(tmp_path)
    ledger.reset_model(model, initial_capital=10_000.0)
    if trades:
        ledger.append_trades(model, trades)
    if equity:
        ledger.append_equity(model, [
            {"ts": ts, "cash": value, "margin_used": 0.0, "unrealized_pnl": 0.0,
             "equity": value, "open_positions": 0}
            for ts, value in equity
        ])
    state = ledger.load_state(model) or {}
    state["positions"] = positions or []
    ledger.write_state(model, state)
    return ledger


# --------------------------------------------------------------------------- #
# Fiyat çıpası
# --------------------------------------------------------------------------- #
def test_marks_come_from_the_as_of_bar_not_the_last_row() -> None:
    """Seri as_of'tan ileri giderse (önbellek tazelendi) o bar KULLANILMAZ (kural 12)."""
    market = _market(BTC_USDT_SWAP=120.0)
    frame = market.ohlcv["BTC-USDT-SWAP"]
    extended = pd.concat([frame, frame.tail(1).rename(index={AS_OF: AS_OF + pd.Timedelta(hours=4)})])
    extended.iloc[-1, extended.columns.get_loc("close")] = 999.0
    market = MarketData(ohlcv={"BTC-USDT-SWAP": extended}, btc=extended, funding={}, as_of=AS_OF)
    assert marks_from_market(market)["BTC-USDT-SWAP"] == pytest.approx(120.0)


def test_symbol_without_the_as_of_bar_is_left_unmarked() -> None:
    market = _market(BTC_USDT_SWAP=120.0)
    frame = market.ohlcv["BTC-USDT-SWAP"].iloc[:1]
    market = MarketData(ohlcv={"BTC-USDT-SWAP": frame}, btc=frame, funding={}, as_of=AS_OF)
    assert marks_from_market(market) == {}


# --------------------------------------------------------------------------- #
# Açık pozisyonlar
# --------------------------------------------------------------------------- #
def test_open_position_pnl_matches_the_close_formula(tmp_path: Path) -> None:
    """Brüt fiyat farkı − giriş komisyonu + funding; çıkış maliyeti YOK (pozisyon açık)."""
    ledger = _ledger(tmp_path, "m", positions=[_position()])
    rows = open_positions(["m"], ledger=ledger, marks={"BTC-USDT-SWAP": 110.0})
    assert rows[0]["gross_pnl"] == pytest.approx(20.0)
    assert rows[0]["pnl"] == pytest.approx(20.0 - 0.2 - 0.5)
    assert rows[0]["r"] == pytest.approx((20.0 - 0.7) / (2.0 * 3.0))


def test_unmarked_position_is_not_shown_in_profit(tmp_path: Path) -> None:
    """Fiyatı olmayan sembolde giriş fiyatı kullanılır; kâr/zarar UYDURULMAZ."""
    ledger = _ledger(tmp_path, "m", positions=[_position()])
    rows = open_positions(["m"], ledger=ledger, marks={})
    assert rows[0]["marked"] is False
    assert rows[0]["mark_price"] == pytest.approx(100.0)
    assert rows[0]["gross_pnl"] == pytest.approx(0.0)


def test_short_position_gains_when_price_falls(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path, "m", positions=[_position(direction="short", initial_stop_price=103.0, stop_price=103.0)])
    rows = open_positions(["m"], ledger=ledger, marks={"BTC-USDT-SWAP": 90.0})
    assert rows[0]["gross_pnl"] == pytest.approx(20.0)


def test_stopless_reference_position_has_no_r(tmp_path: Path) -> None:
    """Stop'u olmayanın 1R'si yoktur (kural 15); 0.0 yazmak onu R yarışına sokardı."""
    ledger = _ledger(tmp_path, "buyhold", positions=[
        _position(stop_price=None, initial_stop_price=None)
    ])
    rows = open_positions(["buyhold"], ledger=ledger, marks={"BTC-USDT-SWAP": 110.0})
    assert math.isnan(rows[0]["r"])


def test_pnl_pct_agrees_with_pnl_sign(tmp_path: Path) -> None:
    """Brüt kârda ama komisyon sonrası zararda bir pozisyonda iki sayı çelişmemeli."""
    ledger = _ledger(tmp_path, "m", positions=[_position(entry_fee=25.0)])
    rows = open_positions(["m"], ledger=ledger, marks={"BTC-USDT-SWAP": 110.0})
    assert rows[0]["pnl"] < 0.0
    assert rows[0]["pnl_pct"] < 0.0


def test_open_position_carries_the_remaining_take_profits(tmp_path: Path) -> None:
    """Hedefler KALAN hedeflerdir: dolmuş bir TP'yi beklenen gibi çizmek yanlış olurdu."""
    ledger = _ledger(tmp_path, "m", positions=[
        _position(take_profits=[{"price": 120.0, "fraction": 0.5}])
    ])
    rows = open_positions(["m"], ledger=ledger, marks={"BTC-USDT-SWAP": 110.0})
    assert rows[0]["take_profits"] == [{"price": 120.0, "fraction": 0.5}]


def test_open_position_without_targets_has_an_empty_target_list(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path, "m", positions=[_position()])
    rows = open_positions(["m"], ledger=ledger, marks={"BTC-USDT-SWAP": 110.0})
    assert rows[0]["take_profits"] == []


# --------------------------------------------------------------------------- #
# Son işlemler
# --------------------------------------------------------------------------- #
def test_recent_trades_are_newest_first_across_models() -> None:
    rows = recent_trades({
        "a": [_trade(closed_at="2026-03-01T00:00:00+00:00")],
        "b": [_trade(closed_at="2026-03-05T00:00:00+00:00")],
    })
    assert [row["model"] for row in rows] == ["b", "a"]


def test_recent_trades_keep_the_signal_reason_untrimmed() -> None:
    reason = "konsensüs 3 oy (trend, squeeze, momentum); güven 0.82"
    rows = recent_trades({"m": [_trade(signal_reason=reason)]})
    assert rows[0]["reason"] == reason


def test_recent_trades_respect_the_limit() -> None:
    rows = recent_trades({"m": [_trade() for _ in range(50)]}, limit=20)
    assert len(rows) == 20


def test_trade_without_risk_amount_has_no_r() -> None:
    rows = recent_trades({"m": [_trade(risk_amount="")]})
    assert math.isnan(rows[0]["r"])


def test_trade_rows_carry_quantity_and_the_realised_risk() -> None:
    """Model detayı miktarı ve R'nin PAYDASINI gösterir; ikisi de deftere yazılan değerdir."""
    rows = recent_trades({"m": [_trade(qty=2.5, notional=250.0, risk_amount=3.0)]})
    assert rows[0]["qty"] == 2.5
    assert rows[0]["notional"] == 250.0
    assert rows[0]["risk_amount"] == 3.0


# --------------------------------------------------------------------------- #
# Model başına işlem geçmişi
# --------------------------------------------------------------------------- #
def test_model_trades_are_kept_per_model_and_newest_first() -> None:
    """Tek akışı modele göre filtrelemek, çok işlem yapan modelin diğerlerini silmesiydi."""
    result = model_trades({
        "a": [_trade(closed_at="2026-03-01T00:00:00+00:00"),
              _trade(closed_at="2026-03-05T00:00:00+00:00")],
        "b": [_trade(closed_at="2026-03-03T00:00:00+00:00")],
    }, limit=10)
    assert set(result["models"]) == {"a", "b"}
    assert [row["closed_at"] for row in result["models"]["a"]["trades"]] == [
        "2026-03-05T00:00:00+00:00", "2026-03-01T00:00:00+00:00",
    ]
    assert [row["model"] for row in result["models"]["b"]["trades"]] == ["b"]


def test_model_trades_report_the_untruncated_total() -> None:
    """Kırpılmış bir liste, `total` olmadan modelin TÜM geçmişi gibi okunur."""
    result = model_trades({"m": [_trade() for _ in range(7)]}, limit=3)
    assert result["limit"] == 3
    assert result["models"]["m"]["total"] == 7
    assert len(result["models"]["m"]["trades"]) == 3


# --------------------------------------------------------------------------- #
# 24 saatlik hareket
# --------------------------------------------------------------------------- #
def test_activity_window_is_counted_from_as_of_not_the_wall_clock() -> None:
    """Duvar saati kullanan bir pencere, gecikmiş bir turda olmayan bir sessizlik raporlardı."""
    result = activity(
        {"m": [
            _trade(closed_at="2026-03-10T08:00:00+00:00"),      # pencere içi
            _trade(closed_at="2026-03-08T08:00:00+00:00"),      # pencere dışı
        ]},
        positions=[], as_of=AS_OF, hours=24,
    )
    assert result["closed"] == 1
    assert result["since"].startswith("2026-03-09T12:00")


def test_activity_counts_positions_that_are_still_open() -> None:
    """Yalnızca kapananlara bakmak, açılıp açık kalan pozisyonu hiç olmamış gibi gösterirdi."""
    result = activity(
        {"m": [_trade(opened_at="2026-03-01T00:00:00+00:00", closed_at="2026-03-01T04:00:00+00:00")]},
        positions=[{"opened_at": "2026-03-10T04:00:00+00:00"}],
        as_of=AS_OF, hours=24,
    )
    assert result["opened"] == 1
    assert result["still_open"] == 1
    assert result["closed"] == 0


def test_activity_without_closed_trades_reports_nan_average() -> None:
    result = activity({"m": []}, positions=[], as_of=AS_OF, hours=24)
    assert result["closed"] == 0
    assert math.isnan(result["closed_avg_r"])


# --------------------------------------------------------------------------- #
# Özsermaye eğrisi
# --------------------------------------------------------------------------- #
def test_equity_series_keeps_the_first_and_last_point_when_thinning() -> None:
    rows = [{"ts": f"2026-01-01T{index:02d}:00:00+00:00", "equity": float(index)}
            for index in range(24)]
    thinned = equity_series(rows, max_points=6)
    assert len(thinned) <= 6
    assert thinned[0][0] == rows[0]["ts"] and thinned[0][1] == 0.0
    assert thinned[-1][1] == 23.0


def test_equity_series_skips_rows_without_a_value() -> None:
    rows = [{"ts": "2026-01-01T00:00:00+00:00", "equity": ""},
            {"ts": "2026-01-01T04:00:00+00:00", "equity": 10.0}]
    assert equity_series(rows) == [["2026-01-01T04:00:00+00:00", 10.0]]


# --------------------------------------------------------------------------- #
# Bütün yük
# --------------------------------------------------------------------------- #
def test_build_dashboard_excludes_benchmarks_from_the_pool(tmp_path: Path) -> None:
    """Çıpanın stop'suz işlemi havuzun ortalama R'sine giremez (kural 15)."""
    ledger = Ledger(tmp_path)
    for model in ("m", "buyhold"):
        ledger.reset_model(model, initial_capital=10_000.0)
    ledger.append_trades("m", [_trade(direction="short", pnl=30.0, risk_amount=10.0)])
    ledger.append_trades("buyhold", [_trade(direction="long", pnl=900.0, risk_amount="", stop_price="")])
    ledger.append_equity("m", [{"ts": AS_OF.isoformat(), "cash": 0.0, "margin_used": 0.0,
                                "unrealized_pnl": 0.0, "equity": 10_030.0, "open_positions": 0}])

    config = load_config()
    metrics = compare(["m", "buyhold"], ledger=ledger, config=config, benchmarks=["buyhold"])
    payload = build_dashboard(metrics, ledger=ledger, config=config, market=_market(BTC_USDT_SWAP=100.0))

    assert payload["pooled"]["models"] == ["m"]
    assert payload["pooled"]["directions"]["short"]["avg_r"] == pytest.approx(3.0)
    assert payload["pooled"]["directions"]["long"]["trades"] == 0
    assert [item["model"] for item in payload["acceptance"]["models"]] == ["m"]
    # Korelasyon çıpayı DA içerir: orada ölçülen R değil, bar getirisidir.
    assert "buyhold" in payload["correlation"]["models"]


def test_build_dashboard_sections_are_all_present(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path)
    ledger.reset_model("m", initial_capital=10_000.0)
    config = load_config()
    metrics = compare(["m"], ledger=ledger, config=config, benchmarks=[])
    payload = build_dashboard(metrics, ledger=ledger, config=config, market=_market())
    assert set(payload) == {
        "pooled", "acceptance", "correlation", "equity",
        "open_positions", "concentration", "recent_trades", "model_trades",
        "activity", "breakdowns",
    }
    # Kırılım bölümü katmana bağlıdır: `breakdowns` verilmediğinde (4 saatlik katman)
    # bölüm boştur ama VARDIR — sayfanın "eski JSON mu, kırılımsız katman mı" ayrımını
    # bir anahtarın yokluğundan tahmin etmesi gerekmesin.
    assert payload["breakdowns"] == {}


# --------------------------------------------------------------------------- #
# Çıkış yönetiminin DURUMU ve maliyet alanları (docs/positions.html'in okuduğu yüzey)
# --------------------------------------------------------------------------- #
def test_open_position_carries_the_risk_amount_and_margin(tmp_path: Path) -> None:
    """Riske edilen tutar ile marj AYRI kolonlardır: biri diğerinden türetilemez."""
    ledger = _ledger(tmp_path, "m", positions=[_position(margin=50.0)])
    row = open_positions(["m"], ledger=ledger, marks={"BTC-USDT-SWAP": 100.0})[0]
    assert row["risk_amount"] == pytest.approx(2.0 * abs(100.0 - 97.0))
    assert row["margin"] == pytest.approx(50.0)
    assert row["initial_qty"] == pytest.approx(2.0)


def test_stopless_position_has_no_risk_amount(tmp_path: Path) -> None:
    """Stop'suz referans pozisyonda (kural 15) 1R yoktur — 0.0 da değil, None."""
    ledger = _ledger(tmp_path, "m", positions=[_position(stop_price=None, initial_stop_price=None)])
    row = open_positions(["m"], ledger=ledger, marks={"BTC-USDT-SWAP": 100.0})[0]
    assert row["risk_amount"] is None
    assert math.isnan(row["r"])


def test_position_without_exit_management_reports_no_state(tmp_path: Path) -> None:
    """Mekanizmayı BİLDİRMEYEN model hiçbir rozet almaz (kural 13b: varsayılan kapalı)."""
    ledger = _ledger(tmp_path, "m", positions=[_position(trailing_atr=None)])
    row = open_positions(["m"], ledger=ledger, marks={"BTC-USDT-SWAP": 100.0})[0]
    assert row["breakeven_at_r"] is None
    assert row["breakeven_done"] is False
    assert row["partial_tp"] is None
    assert row["partial_done"] is False
    assert row["trailing_active"] is False
    assert row["stop_rule"] == ""
    assert row["stop_moved"] is False


def test_breakeven_is_reported_only_after_the_stop_actually_moved(tmp_path: Path) -> None:
    """Rozet İSTEĞİ değil OLAYI gösterir: stop girişe çekilene kadar yanmaz."""
    waiting = _position(breakeven_at_r=1.0, stop_price=97.0, initial_stop_price=97.0)
    ledger = _ledger(tmp_path, "m", positions=[waiting])
    row = open_positions(["m"], ledger=ledger, marks={"BTC-USDT-SWAP": 100.0})[0]
    assert row["breakeven_at_r"] == pytest.approx(1.0)
    assert row["breakeven_done"] is False
    assert row["stop_moved"] is False

    moved = _position(breakeven_at_r=1.0, stop_price=100.0, initial_stop_price=97.0,
                      stop_rule="breakeven")
    ledger = _ledger(tmp_path, "m2", positions=[moved])
    row = open_positions(["m2"], ledger=ledger, marks={"BTC-USDT-SWAP": 100.0})[0]
    assert row["breakeven_done"] is True
    assert row["stop_moved"] is True
    assert row["stop_rule"] == "breakeven"


def test_short_breakeven_uses_the_favourable_side(tmp_path: Path) -> None:
    """Short'ta başabaş stop'u girişin ALTINDA ya da girişte olur, üstünde değil."""
    below = _position(direction="short", stop_price=100.0, initial_stop_price=103.0,
                      breakeven_at_r=1.0, high_water=101.0, low_water=95.0)
    ledger = _ledger(tmp_path, "m", positions=[below])
    assert open_positions(["m"], ledger=ledger, marks={})[0]["breakeven_done"] is True

    above = _position(direction="short", stop_price=103.0, initial_stop_price=103.0,
                      breakeven_at_r=1.0, high_water=101.0, low_water=95.0)
    ledger = _ledger(tmp_path, "m2", positions=[above])
    assert open_positions(["m2"], ledger=ledger, marks={})[0]["breakeven_done"] is False


def test_giveback_trailing_is_active_only_after_the_partial_filled(tmp_path: Path) -> None:
    """Geri verme takibi kısmi çıkıştan ÖNCE devreye girmez (core/engine.py::_giveback_stop)."""
    armed = _position(trailing_atr=None, trail_giveback_pct=0.5,
                      partial_tp={"r": 1.5, "fraction": 0.5}, partial_done=False)
    ledger = _ledger(tmp_path, "m", positions=[armed])
    row = open_positions(["m"], ledger=ledger, marks={})[0]
    assert row["partial_tp"] == {"r": 1.5, "fraction": 0.5}
    assert row["trailing_active"] is False

    filled = _position(trailing_atr=None, trail_giveback_pct=0.5,
                       partial_tp={"r": 1.5, "fraction": 0.5}, partial_done=True)
    ledger = _ledger(tmp_path, "m2", positions=[filled])
    row = open_positions(["m2"], ledger=ledger, marks={})[0]
    assert row["partial_done"] is True
    assert row["trailing_active"] is True


def test_atr_trailing_counts_as_active_without_a_partial(tmp_path: Path) -> None:
    """ATR takibi (kural 9) kısmi çıkış şartına bağlı değildir."""
    ledger = _ledger(tmp_path, "m", positions=[_position(trailing_atr=1.0)])
    assert open_positions(["m"], ledger=ledger, marks={})[0]["trailing_active"] is True


def test_trade_rows_carry_leverage_margin_and_the_exit_rule() -> None:
    """Çıkışın ALT sebebi `notes` kuyruğundan okunur; `exit_reason` onu taşımaz."""
    rows = recent_trades({"m": [_trade(
        exit_reason="stop", leverage=3.0, margin=40.0,
        notes="hedef 105 | exit_rule=trailing_atr",
    )]})
    assert rows[0]["leverage"] == pytest.approx(3.0)
    assert rows[0]["margin"] == pytest.approx(40.0)
    assert rows[0]["exit_rule"] == "trailing_atr"
    assert rows[0]["is_partial"] is False


def test_trade_without_an_exit_rule_tag_is_not_invented() -> None:
    """Etiketi olmayan satır uydurulmaz: "ilk stop aldı" etiketin YOKLUĞUdur."""
    rows = recent_trades({"m": [_trade(exit_reason="stop", notes="")]})
    assert rows[0]["exit_rule"] == ""


def test_partial_exit_rows_are_flagged() -> None:
    """Kısmi çıkış tamamlanmış bir işlem değildir; sayfa onu istatistikten çıkarabilsin."""
    rows = recent_trades({"m": [_trade(exit_reason="partial")]})
    assert rows[0]["is_partial"] is True


# --------------------------------------------------------------------------- #
# Portföy yoğunlaşması (ÖLÇÜM, kural değil)
# --------------------------------------------------------------------------- #
def test_concentration_reports_net_and_gross_exposure(tmp_path: Path) -> None:
    """Net yönlü maruziyet, brüt maruziyet ve en büyük sembol payı ayrı sorulardır.

    Net, "yönlü ne kadar açığız" (kripto evreninde kabaca BTC betasının vekili); brüt,
    "kaldıracın gerçekleşen hâli"; pay ise "risk tek sembolde mi toplanmış". Üçünü tek
    sayıya indirmek, birbirini götüren iki pozisyonu risksiz göstermek olurdu.
    """
    ledger = _ledger(
        tmp_path, "m",
        positions=[
            _position(symbol="BTC-USDT-SWAP", direction="long", qty=30.0, margin=1_000.0),
            _position(symbol="ETH-USDT-SWAP", direction="short", qty=10.0, margin=1_000.0),
        ],
    )
    marks = {"BTC-USDT-SWAP": 100.0, "ETH-USDT-SWAP": 100.0}
    rows = concentration(["m"], ledger=ledger, config=load_config(), marks=marks)["m"]

    # 3.000 long − 1.000 short = 2.000 net, 4.000 brüt; en büyük sembol brütün 3/4'ü.
    assert rows["top_symbol_share"] == pytest.approx(0.75)
    assert rows["gross_exposure"] == pytest.approx(2.0 * rows["net_exposure"])


def test_concentration_is_zero_without_open_positions(tmp_path: Path) -> None:
    """Pozisyon yoksa maruziyet SIFIRDIR, `nan` değil: "ölçüldü ve sıfır çıktı" doğrudur."""
    ledger = Ledger(tmp_path)
    ledger.reset_model("m", initial_capital=10_000.0)
    rows = concentration(["m"], ledger=ledger, config=load_config(), marks={})["m"]
    assert rows == {"net_exposure": 0.0, "gross_exposure": 0.0, "top_symbol_share": 0.0}
