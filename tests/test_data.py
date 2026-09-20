"""core/data.py birim testleri.

Tüm testler OKX yanıtlarını taklit eden bir stub session ile çalışır: modül ağ erişimi
olmadan, tek başına test edilebilir olmalı (CLAUDE.md > Kod Stili).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from core.data import (
    OKXClient,
    OKXError,
    bar_duration,
    fetch_funding,
    fetch_ohlcv,
    load_market_data,
    load_universe,
)

BAR_MS = 4 * 60 * 60 * 1000
NOW = pd.Timestamp("2024-03-01 12:00:00", tz="UTC")


class StubResponse:
    def __init__(self, payload: dict[str, Any], status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload)

    def json(self) -> dict[str, Any]:
        return self._payload


class StubSession:
    """Path -> yanıt listesi eşlemesi. Her çağrı kaydedilir ki sayfalama iddiaları ölçülebilsin."""

    def __init__(self, routes: dict[str, list[dict[str, Any]] | Any]) -> None:
        self.routes = routes
        self.calls: list[tuple[str, dict[str, str]]] = []

    def get(self, url: str, *, params: dict[str, str], timeout: float) -> StubResponse:
        path = url.split("okx-test", 1)[-1] if "okx-test" in url else url
        for route, handler in self.routes.items():
            if path.endswith(route):
                self.calls.append((route, dict(params)))
                data = handler(params) if callable(handler) else handler
                if isinstance(data, StubResponse):
                    return data
                return StubResponse({"code": "0", "msg": "", "data": data})
        raise AssertionError(f"stub'da tanımsız yol: {url}")


def _candle(ts_ms: int, close: float, *, confirm: str = "1") -> list[str]:
    return [
        str(ts_ms),
        f"{close - 1:.1f}",
        f"{close + 2:.1f}",
        f"{close - 2:.1f}",
        f"{close:.1f}",
        "100",
        "100",
        "100",
        confirm,
    ]


def _candles(count: int, *, end_ms: int, start_close: float = 100.0) -> list[list[str]]:
    """OKX gibi TERS kronolojik (en yeni ilk) bar listesi üretir."""
    return [
        _candle(end_ms - index * BAR_MS, start_close + index) for index in range(count)
    ]


def _config(tmp_path: Path, **overrides: Any) -> dict[str, Any]:
    config: dict[str, Any] = {
        "initial_capital": 10000,
        "timeframe": "4H",
        "universe_size": 2,
        "universe_refresh_days": 30,
        "random_seed": 7,
        "models": [],
        "funding": {"enabled": True, "interval_hours": 8},
        "exchange": {
            "name": "okx",
            "rest_base": "https://okx-test",
            "inst_type": "SWAP",
            "quote_ccy": "USDT",
            "btc_reference": "BTC-USDT-SWAP",
            "candles_limit": 3,
            "history_candles_limit": 2,
            "funding_limit": 2,
            "request_timeout_sec": 1,
            "min_request_interval_sec": 0,
            "max_retries": 2,
            "backoff_base_sec": 0,
            "backoff_max_sec": 0,
        },
        "data": {
            "cache_dir": str(tmp_path / "cache"),
            "universe_file": str(tmp_path / "universe.json"),
            "history_bars": 10,
            "funding_history_periods": 4,
            "max_staleness_bars": 2,
        },
    }
    config.update(overrides)
    return config


def _client(session: StubSession, config: dict[str, Any]) -> OKXClient:
    client = OKXClient.from_config(config, session=session)
    return client


# --------------------------------------------------------------------------- #
# Look-ahead: kapanmamış bar
# --------------------------------------------------------------------------- #
def test_unconfirmed_bar_is_dropped(tmp_path: Path) -> None:
    open_bar_ms = int(NOW.timestamp() * 1000)  # 12:00, henüz kapanmadı
    page = [_candle(open_bar_ms, 200.0, confirm="0"), *_candles(2, end_ms=open_bar_ms - BAR_MS)]
    config = _config(tmp_path)
    session = StubSession({"/market/candles": page, "/market/history-candles": []})

    frame = fetch_ohlcv(config, "BTC-USDT-SWAP", client=_client(session, config), now=NOW)

    assert frame.index[-1] == pd.Timestamp("2024-03-01 08:00:00", tz="UTC")
    assert open_bar_ms not in [int(ts.timestamp() * 1000) for ts in frame.index]


def test_bar_whose_close_time_is_in_the_future_is_dropped(tmp_path: Path) -> None:
    """confirm alanı olmayan yanıtlarda ikinci savunma devrede olmalı."""
    future_ms = int(NOW.timestamp() * 1000)
    row_without_confirm = [str(future_ms), "1", "2", "0.5", "1.5", "10"]
    closed_ms = future_ms - BAR_MS
    config = _config(tmp_path)
    session = StubSession(
        {
            "/market/candles": [row_without_confirm, [str(closed_ms), "1", "2", "0.5", "1.5", "10"]],
            "/market/history-candles": [],
        }
    )

    frame = fetch_ohlcv(config, "BTC-USDT-SWAP", client=_client(session, config), now=NOW)

    assert list(frame.index) == [pd.Timestamp(closed_ms, unit="ms", tz="UTC")]


def test_timestamps_are_utc_tz_aware(tmp_path: Path) -> None:
    config = _config(tmp_path)
    end_ms = int(NOW.timestamp() * 1000) - BAR_MS
    session = StubSession({"/market/candles": _candles(2, end_ms=end_ms), "/market/history-candles": []})

    frame = fetch_ohlcv(config, "BTC-USDT-SWAP", client=_client(session, config), now=NOW)

    assert str(frame.index.tz) == "UTC"
    assert list(frame.columns) == ["open", "high", "low", "close", "volume"]


# --------------------------------------------------------------------------- #
# Sayfalama ve önbellek
# --------------------------------------------------------------------------- #
def test_pagination_walks_backwards_and_switches_to_history_endpoint(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config["data"]["history_bars"] = 7
    end_ms = int(NOW.timestamp() * 1000) - BAR_MS

    # /market/candles yalnızca güncel ucu verir (burada en yeni 4 bar), daha derin geçmiş
    # /market/history-candles'tan gelir — OKX'in gerçek davranışı.
    all_bars = _candles(12, end_ms=end_ms)  # ters kronolojik
    recent_floor = end_ms - 3 * BAR_MS

    def _page(bars: list[list[str]], params: dict[str, str], limit: int) -> list[list[str]]:
        cursor = int(params["after"]) if "after" in params else None
        selected = [bar for bar in bars if cursor is None or int(bar[0]) < cursor]
        return selected[:limit]

    def candles(params: dict[str, str]) -> list[list[str]]:
        return _page([b for b in all_bars if int(b[0]) >= recent_floor], params, 3)

    def history(params: dict[str, str]) -> list[list[str]]:
        assert "after" in params, "history-candles imleçsiz çağrılmamalı"
        return _page(all_bars, params, 2)

    session = StubSession({"/market/candles": candles, "/market/history-candles": history})
    frame = fetch_ohlcv(config, "BTC-USDT-SWAP", client=_client(session, config), now=NOW)

    assert len(frame) == 7
    assert frame.index.is_monotonic_increasing
    paths = [path for path, _ in session.calls]
    assert "/market/candles" in paths and "/market/history-candles" in paths
    afters = [int(params["after"]) for _, params in session.calls if "after" in params]
    assert afters == sorted(afters, reverse=True), "imleç eskiye doğru ilerlemeli"


def test_second_run_only_fetches_missing_bars(tmp_path: Path) -> None:
    config = _config(tmp_path)
    end_ms = int(NOW.timestamp() * 1000) - BAR_MS
    first = StubSession(
        {"/market/candles": _candles(3, end_ms=end_ms), "/market/history-candles": []}
    )
    fetch_ohlcv(config, "BTC-USDT-SWAP", client=_client(first, config), now=NOW)

    later = NOW + pd.Timedelta("8h")
    new_end_ms = end_ms + 2 * BAR_MS
    second = StubSession(
        {"/market/candles": _candles(3, end_ms=new_end_ms), "/market/history-candles": []}
    )
    frame = fetch_ohlcv(config, "BTC-USDT-SWAP", client=_client(second, config), now=later)

    assert len(frame) == 5, "önbellekteki 3 bar korunup 2 yeni bar eklenmeli"
    assert len(second.calls) == 1, "önbelleğe ulaşıldığında sayfalama durmalı"
    assert (tmp_path / "cache" / "BTC-USDT-SWAP_4H.parquet").is_file()


# --------------------------------------------------------------------------- #
# Rate limit / retry
# --------------------------------------------------------------------------- #
def test_rate_limited_request_is_retried(tmp_path: Path) -> None:
    config = _config(tmp_path)
    attempts: list[int] = []

    def flaky(params: dict[str, str]) -> Any:
        attempts.append(1)
        if len(attempts) == 1:
            return StubResponse({"code": "50011", "msg": "Too Many Requests", "data": []})
        if len(attempts) == 2:
            return StubResponse({"code": "0", "msg": "", "data": []}, status_code=429)
        return [["1", "BTC"]]

    session = StubSession({"/market/tickers": flaky})
    client = _client(session, config)

    assert client.get("/api/v5/market/tickers", {"instType": "SWAP"}) == [["1", "BTC"]]
    assert len(attempts) == 3


def test_retries_are_exhausted_into_okx_error(tmp_path: Path) -> None:
    config = _config(tmp_path)
    session = StubSession(
        {"/market/tickers": lambda params: StubResponse({"code": "0", "data": []}, status_code=500)}
    )
    with pytest.raises(OKXError, match="denemede başarısız"):
        _client(session, config).get("/api/v5/market/tickers", {})


def test_permanent_okx_error_is_not_retried(tmp_path: Path) -> None:
    config = _config(tmp_path)
    calls: list[int] = []

    def broken(params: dict[str, str]) -> Any:
        calls.append(1)
        return StubResponse({"code": "51001", "msg": "Instrument ID does not exist", "data": []})

    session = StubSession({"/market/candles": broken})
    with pytest.raises(OKXError, match="51001"):
        _client(session, config).get("/api/v5/market/candles", {})
    assert len(calls) == 1


# --------------------------------------------------------------------------- #
# Evren
# --------------------------------------------------------------------------- #
def _universe_session() -> StubSession:
    return StubSession(
        {
            "/public/instruments": [
                {"instId": "BTC-USDT-SWAP", "state": "live", "settleCcy": "USDT", "ctType": "linear"},
                {"instId": "ETH-USDT-SWAP", "state": "live", "settleCcy": "USDT", "ctType": "linear"},
                {"instId": "SOL-USDT-SWAP", "state": "live", "settleCcy": "USDT", "ctType": "linear"},
                {"instId": "BTC-USD-SWAP", "state": "live", "settleCcy": "BTC", "ctType": "inverse"},
                {"instId": "OLD-USDT-SWAP", "state": "suspend", "settleCcy": "USDT", "ctType": "linear"},
            ],
            "/market/tickers": [
                {"instId": "BTC-USDT-SWAP", "volCcy24h": "1000", "last": "60000"},
                {"instId": "ETH-USDT-SWAP", "volCcy24h": "20000", "last": "3000"},
                {"instId": "SOL-USDT-SWAP", "volCcy24h": "10000", "last": "100"},
                {"instId": "BTC-USD-SWAP", "volCcy24h": "99999", "last": "60000"},
                {"instId": "OLD-USDT-SWAP", "volCcy24h": "99999", "last": "1"},
            ],
        }
    )


def test_universe_ranks_by_quote_volume_and_excludes_non_usdt(tmp_path: Path) -> None:
    config = _config(tmp_path)
    session = _universe_session()

    symbols = load_universe(config, client=_client(session, config), now=NOW)

    # ciro: BTC 60M, ETH 60M, SOL 1M -> eşitlikte alfabetik, inverse/suspend dışarıda
    assert symbols == ["BTC-USDT-SWAP", "ETH-USDT-SWAP"]
    stored = json.loads((tmp_path / "universe.json").read_text(encoding="utf-8"))
    assert stored["symbols"] == symbols
    assert stored["computed_at"].startswith("2024-03-01")


def test_universe_is_not_recomputed_before_refresh_window(tmp_path: Path) -> None:
    config = _config(tmp_path)
    first = _universe_session()
    load_universe(config, client=_client(first, config), now=NOW)

    second = _universe_session()
    symbols = load_universe(
        config, client=_client(second, config), now=NOW + pd.Timedelta(days=29)
    )

    assert symbols == ["BTC-USDT-SWAP", "ETH-USDT-SWAP"]
    assert second.calls == [], "refresh penceresi dolmadan borsaya gidilmemeli"


def test_universe_is_recomputed_after_refresh_window(tmp_path: Path) -> None:
    config = _config(tmp_path)
    load_universe(config, client=_client(_universe_session(), config), now=NOW)

    session = _universe_session()
    load_universe(config, client=_client(session, config), now=NOW + pd.Timedelta(days=31))

    assert [path for path, _ in session.calls] == ["/public/instruments", "/market/tickers"]


# --------------------------------------------------------------------------- #
# Funding
# --------------------------------------------------------------------------- #
def test_funding_history_is_paginated_and_excludes_future(tmp_path: Path) -> None:
    config = _config(tmp_path)
    eight_hours = 8 * 60 * 60 * 1000
    base = int(NOW.timestamp() * 1000)

    def funding(params: dict[str, str]) -> list[dict[str, str]]:
        cursor = int(params["after"]) if "after" in params else base + eight_hours
        return [
            {"instId": "BTC-USDT-SWAP", "fundingTime": str(cursor - eight_hours), "fundingRate": "0.0001"},
            {"instId": "BTC-USDT-SWAP", "fundingTime": str(cursor - 2 * eight_hours), "fundingRate": "-0.0002"},
        ]

    session = StubSession({"/public/funding-rate-history": funding})
    series = fetch_funding(config, "BTC-USDT-SWAP", client=_client(session, config), now=NOW)

    assert len(series) == 4  # funding_history_periods
    assert series.index.max() <= NOW
    assert str(series.index.tz) == "UTC"


def test_funding_disabled_returns_empty_series_without_requests(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config["funding"]["enabled"] = False
    session = StubSession({})

    series = fetch_funding(config, "BTC-USDT-SWAP", client=_client(session, config), now=NOW)

    assert series.empty
    assert session.calls == []


# --------------------------------------------------------------------------- #
# MarketData anlık görüntüsü
# --------------------------------------------------------------------------- #
def _market_session(*, lag_bars: dict[str, int] | None = None) -> StubSession:
    """Her sembol için 4 barlık seri; `lag_bars` ile sembol başına gecikme verilir."""
    end_ms = int(NOW.timestamp() * 1000) - BAR_MS
    lags = lag_bars or {}

    def candles(params: dict[str, str]) -> list[list[str]]:
        last = end_ms - lags.get(params["instId"], 0) * BAR_MS
        return _candles(4, end_ms=last)

    def funding(params: dict[str, str]) -> list[dict[str, str]]:
        return [{"fundingTime": str(end_ms), "fundingRate": "0.0001"}]

    return StubSession(
        {
            "/market/candles": candles,
            "/market/history-candles": [],
            "/public/funding-rate-history": funding,
        }
    )


def test_market_data_snapshot_is_shared_and_as_of_is_last_closed_bar(tmp_path: Path) -> None:
    config = _config(tmp_path)
    session = _market_session()

    market = load_market_data(
        config,
        symbols=["BTC-USDT-SWAP", "ETH-USDT-SWAP"],
        client=_client(session, config),
        now=NOW,
    )

    assert market.as_of == pd.Timestamp("2024-03-01 08:00:00", tz="UTC")
    assert set(market.ohlcv) == {"BTC-USDT-SWAP", "ETH-USDT-SWAP"}
    for frame in market.ohlcv.values():
        assert frame.index[-1] == market.as_of
    assert market.btc.index[-1] == market.as_of
    assert set(market.funding) == set(market.ohlcv)
    assert all(series.index.max() <= market.as_of for series in market.funding.values())


def test_btc_reference_is_fetched_even_when_not_requested(tmp_path: Path) -> None:
    config = _config(tmp_path)
    session = _market_session()

    market = load_market_data(
        config, symbols=["ETH-USDT-SWAP"], client=_client(session, config), now=NOW
    )

    assert "BTC-USDT-SWAP" not in market.ohlcv, "referans, evrene sessizce eklenmemeli"
    assert not market.btc.empty


@pytest.mark.parametrize("lag", [1, 5])
def test_lagging_symbol_is_excluded_instead_of_dragging_as_of_back(
    tmp_path: Path, lag: int
) -> None:
    """Tek bir gecikmiş sembol turun "şimdi"sini geri çekemez; kendisi dışlanır."""
    config = _config(tmp_path)
    session = _market_session(lag_bars={"SOL-USDT-SWAP": lag})

    market = load_market_data(
        config,
        symbols=["BTC-USDT-SWAP", "SOL-USDT-SWAP"],
        client=_client(session, config),
        now=NOW,
    )

    assert set(market.ohlcv) == {"BTC-USDT-SWAP"}
    assert market.as_of == pd.Timestamp("2024-03-01 08:00:00", tz="UTC")


def test_as_of_follows_btc_even_when_other_symbols_are_ahead(tmp_path: Path) -> None:
    """Çıpa BTC: BTC geride kalırsa as_of geri gider, ileri semboller kırpılır."""
    config = _config(tmp_path)
    session = _market_session(lag_bars={"BTC-USDT-SWAP": 1})

    market = load_market_data(
        config,
        symbols=["BTC-USDT-SWAP", "ETH-USDT-SWAP"],
        client=_client(session, config),
        now=NOW,
    )

    assert market.as_of == pd.Timestamp("2024-03-01 04:00:00", tz="UTC")
    assert set(market.ohlcv) == {"BTC-USDT-SWAP", "ETH-USDT-SWAP"}
    for frame in market.ohlcv.values():
        assert frame.index[-1] == market.as_of


def test_symbol_with_a_hole_at_the_anchor_bar_is_excluded(tmp_path: Path) -> None:
    """Çıpadan ileride olmak yetmez: as_of barı seride yoksa sembol tura girmez."""
    config = _config(tmp_path)
    end_ms = int(NOW.timestamp() * 1000) - BAR_MS  # 08:00
    anchor_ms = end_ms - BAR_MS  # 04:00 — BTC bir bar geride

    def candles(params: dict[str, str]) -> list[list[str]]:
        if params["instId"].startswith("BTC"):
            return _candles(4, end_ms=anchor_ms)
        # ETH'nin 04:00 barı yok (borsa boşluğu), ama daha yeni bir barı var.
        return [
            _candle(end_ms, 100.0),
            *[_candle(anchor_ms - (index + 1) * BAR_MS, 101.0 + index) for index in range(3)],
        ]

    session = StubSession(
        {
            "/market/candles": candles,
            "/market/history-candles": [],
            "/public/funding-rate-history": [
                {"fundingTime": str(anchor_ms), "fundingRate": "0.0001"}
            ],
        }
    )

    market = load_market_data(
        config,
        symbols=["BTC-USDT-SWAP", "ETH-USDT-SWAP"],
        client=_client(session, config),
        now=NOW,
    )

    assert market.as_of == pd.Timestamp("2024-03-01 04:00:00", tz="UTC")
    assert set(market.ohlcv) == {"BTC-USDT-SWAP"}


def test_excluded_symbols_are_logged_for_audit(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    config = _config(tmp_path)
    session = _market_session(lag_bars={"SOL-USDT-SWAP": 3})

    with caplog.at_level(logging.INFO, logger="core.data"):
        load_market_data(
            config,
            symbols=["BTC-USDT-SWAP", "SOL-USDT-SWAP"],
            client=_client(session, config),
            now=NOW,
        )

    messages = [record.getMessage() for record in caplog.records]
    assert any("SOL-USDT-SWAP tura alınmıyor" in message for message in messages)
    assert any(
        "1/2 sembol görülebilir, dışlanan: SOL-USDT-SWAP" in message
        for message in messages
    )


def test_stale_btc_anchor_aborts_the_snapshot(tmp_path: Path) -> None:
    """Çıpa bayatsa tur düşer: eski barı yeniymiş gibi işlemek çift işlem üretir."""
    config = _config(tmp_path)
    session = _market_session(lag_bars={"BTC-USDT-SWAP": 3})  # max_staleness_bars = 2

    with pytest.raises(OKXError):
        load_market_data(
            config,
            symbols=["BTC-USDT-SWAP", "ETH-USDT-SWAP"],
            client=_client(session, config),
            now=NOW,
        )


def test_bar_duration_rejects_unsupported_timeframe() -> None:
    assert bar_duration("1D") == pd.Timedelta("1D")
    with pytest.raises(ValueError):
        bar_duration("1Y")
