"""OKX public API v5'ten piyasa verisi çekme ve önbellekleme.

Sorumluluk sınırı (CLAUDE.md): burada strateji mantığı, pozisyon boyutlandırma ya da
maliyet hesabı YOKTUR. Bu modül üç şey yapar: (a) sembol evrenini belirler,
(b) kapanmış barları ve funding geçmişini toplar/önbelleğe alır, (c) MarketData üretir.

LOOK-AHEAD (CLAUDE.md kural 12) — bu modülün en kritik davranışı:
OKX /api/v5/market/candles mumları TERS KRONOLOJİK (en yenisi ilk) döndürür ve
listenin ilk elemanı çoğu zaman HENÜZ KAPANMAMIŞ, oluşmakta olan bardır; OKX bunu
satırın son alanı olan `confirm` ile bildirir ("0" = kapanmadı, "1" = kapandı).
O bar veri kümesine girerse strateji, gerçekte henüz bilinmeyen bir kapanışı görür —
bu tek bir modeli değil TÜM sonuçları geçersiz kılar. Bu yüzden:
  1) `confirm != "1"` olan her bar atılır,
  2) ek güvenlik olarak bar kapanış zamanı (bar_ts + timeframe) `now`'ı aşan barlar da
     atılır (confirm alanını taşımayan/eski yanıtlara karşı),
  3) `as_of` sabit bir çıpadan okunur: BTC referans sembolünün son KAPANMIŞ barı
     (bkz. `load_market_data`); stratejiler "şimdi"yi buradan okur.

Zaman damgaları her yerde tz-aware UTC'dir (OKX ms epoch döndürür).
"""

from __future__ import annotations

import json
import logging
import random
import time
from pathlib import Path
from typing import Any, Iterable, Protocol, Sequence

import pandas as pd

from core.config import get_setting, load_config, project_path
from strategies.base import MarketData

logger = logging.getLogger(__name__)

OHLCV_COLUMNS: tuple[str, ...] = ("open", "high", "low", "close", "volume")
FUNDING_COLUMN = "funding_rate"

_CONFIRMED = "1"
_CANDLE_ENDPOINT = "/api/v5/market/candles"
_HISTORY_CANDLE_ENDPOINT = "/api/v5/market/history-candles"
_INSTRUMENTS_ENDPOINT = "/api/v5/public/instruments"
_TICKERS_ENDPOINT = "/api/v5/market/tickers"
_FUNDING_ENDPOINT = "/api/v5/public/funding-rate-history"

# OKX hata kodları: geçici (yeniden denenebilir) olanlar. 50011 = rate limit,
# 50013 = sistem meşgul, 50026 = sistem hatası.
_RETRYABLE_OKX_CODES = frozenset({"50011", "50013", "50026"})
_OKX_OK = "0"


class OKXError(RuntimeError):
    """OKX isteği kalıcı olarak başarısız oldu (retry'lar tükendi ya da kod kalıcı hata)."""


class HttpResponse(Protocol):
    status_code: int

    @property
    def text(self) -> str: ...

    def json(self) -> Any: ...


class HttpSession(Protocol):
    """requests.Session'ın kullandığımız dar yüzü — testler stub geçebilsin diye."""

    def get(
        self, url: str, *, params: dict[str, str], timeout: float
    ) -> HttpResponse: ...


# --------------------------------------------------------------------------- #
# Zaman yardımcıları
# --------------------------------------------------------------------------- #
def bar_duration(timeframe: str) -> pd.Timedelta:
    """config'teki timeframe'i ("4H") Timedelta'ya çevirir.

    OKX bar kodu büyük harfli birim bekler ("4H"), pandas 2.2+ ise "H" birimini
    deprecate etti — dönüşüm tek yerde yapılır ki iki taraf da doğru kalsın.
    """
    if not timeframe or not timeframe[:-1].isdigit():
        raise ValueError(f"geçersiz timeframe: {timeframe!r}")
    amount, unit = timeframe[:-1], timeframe[-1]
    pandas_unit = {"m": "min", "H": "h", "D": "D", "W": "W"}.get(unit)
    if pandas_unit is None:
        raise ValueError(f"desteklenmeyen timeframe birimi: {timeframe!r}")
    return pd.Timedelta(f"{amount}{pandas_unit}")


def okx_bar(config: dict[str, Any]) -> str:
    return str(get_setting(config, "timeframe"))


def _utc_now(now: pd.Timestamp | None = None) -> pd.Timestamp:
    if now is None:
        return pd.Timestamp.now(tz="UTC")
    stamp = pd.Timestamp(now)
    return stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")


def _to_utc(ms: str | int | float) -> pd.Timestamp:
    return pd.Timestamp(int(ms), unit="ms", tz="UTC")


def _to_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


# --------------------------------------------------------------------------- #
# OKX istemcisi: throttle + retry/backoff + sayfalama yardımı
# --------------------------------------------------------------------------- #
class OKXClient:
    """OKX public v5 istemcisi. Auth gerekmez; yalnızca GET yapar."""

    def __init__(
        self,
        *,
        base_url: str,
        timeout: float,
        max_retries: int,
        backoff_base_sec: float,
        backoff_max_sec: float,
        min_request_interval_sec: float,
        session: HttpSession | None = None,
        sleep: Any = time.sleep,
        random_seed: int = 0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = float(timeout)
        self._max_retries = int(max_retries)
        self._backoff_base = float(backoff_base_sec)
        self._backoff_max = float(backoff_max_sec)
        self._min_interval = float(min_request_interval_sec)
        self._session = session if session is not None else _default_session()
        self._sleep = sleep
        # Jitter de tohumdan beslenir (CLAUDE.md random_seed): koşu tekrarlanabilir kalsın.
        self._rng = random.Random(random_seed)
        self._last_request_at: float | None = None

    @classmethod
    def from_config(
        cls, config: dict[str, Any], *, session: HttpSession | None = None
    ) -> "OKXClient":
        return cls(
            base_url=str(get_setting(config, "exchange.rest_base")),
            timeout=float(get_setting(config, "exchange.request_timeout_sec")),
            max_retries=int(get_setting(config, "exchange.max_retries")),
            backoff_base_sec=float(get_setting(config, "exchange.backoff_base_sec")),
            backoff_max_sec=float(get_setting(config, "exchange.backoff_max_sec")),
            min_request_interval_sec=float(
                get_setting(config, "exchange.min_request_interval_sec")
            ),
            session=session,
            random_seed=int(get_setting(config, "random_seed")),
        )

    def get(self, path: str, params: dict[str, str]) -> list[dict[str, Any]]:
        """OKX zarfını açar ve `data` listesini döndürür.

        Rate limit (HTTP 429 / kod 50011) ve geçici sunucu hataları exponential
        backoff ile yeniden denenir; kalıcı hatalar OKXError olur.
        """
        url = f"{self._base_url}{path}"
        last_error = ""

        for attempt in range(self._max_retries + 1):
            self._throttle()
            try:
                response = self._session.get(url, params=params, timeout=self._timeout)
            except Exception as exc:  # ağ hatası: yeniden denenebilir
                last_error = f"ağ hatası: {exc}"
                if not self._backoff(attempt, last_error, path):
                    break
                continue

            status = getattr(response, "status_code", 0)
            if status == 429 or status >= 500:
                last_error = f"HTTP {status}"
                if not self._backoff(attempt, last_error, path):
                    break
                continue
            if status != 200:
                raise OKXError(f"{path} HTTP {status}: {_response_text(response)}")

            try:
                payload = response.json()
            except Exception as exc:
                raise OKXError(f"{path} yanıtı JSON değil: {exc}") from exc

            code = str(payload.get("code", _OKX_OK))
            if code in _RETRYABLE_OKX_CODES:
                last_error = f"OKX code {code}: {payload.get('msg', '')}"
                if not self._backoff(attempt, last_error, path):
                    break
                continue
            if code != _OKX_OK:
                raise OKXError(f"{path} OKX code {code}: {payload.get('msg', '')}")

            data = payload.get("data") or []
            if not isinstance(data, list):
                raise OKXError(f"{path} beklenmeyen data tipi: {type(data).__name__}")
            return data

        raise OKXError(f"{path} {self._max_retries + 1} denemede başarısız ({last_error})")

    def _throttle(self) -> None:
        if self._min_interval <= 0:
            return
        if self._last_request_at is not None:
            wait = self._min_interval - (time.monotonic() - self._last_request_at)
            if wait > 0:
                self._sleep(wait)
        self._last_request_at = time.monotonic()

    def _backoff(self, attempt: int, reason: str, path: str) -> bool:
        if attempt >= self._max_retries:
            return False
        delay = min(self._backoff_base * (2**attempt), self._backoff_max)
        delay += self._rng.uniform(0.0, self._backoff_base)
        logger.warning(
            "%s yeniden denenecek (%s), %.2fs bekleniyor [deneme %d/%d]",
            path,
            reason,
            delay,
            attempt + 1,
            self._max_retries,
        )
        self._sleep(delay)
        return True


def _default_session() -> HttpSession:
    import requests  # yerel import: testler ve offline kullanım requests'e ihtiyaç duymaz

    return requests.Session()


def _response_text(response: Any) -> str:
    return str(getattr(response, "text", ""))[:200]


# --------------------------------------------------------------------------- #
# Sembol evreni
# --------------------------------------------------------------------------- #
def load_universe(
    config: dict[str, Any],
    *,
    client: OKXClient | None = None,
    now: pd.Timestamp | None = None,
    force_refresh: bool = False,
) -> list[str]:
    """Evreni data/universe.json'dan okur; universe_refresh_days dolmadan yeniden hesaplamaz.

    Evrenin her koşuda yeniden hesaplanması, kıyaslanan sembol kümesini sürekli
    değiştirir ve modellerin karşılaştırmasını anlamsızlaştırır (CLAUDE.md kural 6).
    """
    path = project_path(str(get_setting(config, "data.universe_file")))
    size = int(get_setting(config, "universe_size"))
    refresh_days = int(get_setting(config, "universe_refresh_days"))
    stamp = _utc_now(now)

    cached = _read_universe_file(path)
    if not force_refresh and cached is not None:
        age = stamp - _utc_now(cached["computed_at"])
        if age < pd.Timedelta(days=refresh_days) and len(cached["symbols"]) == size:
            return list(cached["symbols"])

    active = client if client is not None else OKXClient.from_config(config)
    symbols = _compute_universe(active, config, size)
    _write_universe_file(path, symbols, stamp, config)
    logger.info("evren yeniden hesaplandı: %d sembol -> %s", len(symbols), path)
    return symbols


def _compute_universe(client: OKXClient, config: dict[str, Any], size: int) -> list[str]:
    inst_type = str(get_setting(config, "exchange.inst_type"))
    quote_ccy = str(get_setting(config, "exchange.quote_ccy"))

    instruments = client.get(_INSTRUMENTS_ENDPOINT, {"instType": inst_type})
    eligible = {
        row["instId"]
        for row in instruments
        if row.get("state") == "live"
        and row.get("settleCcy") == quote_ccy
        and row.get("ctType", "linear") == "linear"
    }
    if not eligible:
        raise OKXError(f"{inst_type} içinde {quote_ccy} ile takas edilen perpetual bulunamadı")

    tickers = client.get(_TICKERS_ENDPOINT, {"instType": inst_type})
    ranked: list[tuple[float, str]] = []
    for row in tickers:
        inst_id = row.get("instId")
        if inst_id not in eligible:
            continue
        # SWAP'te volCcy24h hacmi BASE cinsindendir (kontrat x ctVal); sembolleri
        # karşılaştırılabilir kılmak için son fiyatla çarpıp USD cirosuna çeviriyoruz.
        turnover = _to_float(row.get("volCcy24h")) * _to_float(row.get("last"))
        ranked.append((turnover, str(inst_id)))

    if not ranked:
        raise OKXError("ticker yanıtı evrenle kesişmedi")

    # Eşit ciroda alfabetik sıra: aynı veriyle aynı evren çıkmalı (tekrarlanabilirlik).
    ranked.sort(key=lambda item: (-item[0], item[1]))
    return [inst_id for _, inst_id in ranked[:size]]


def _read_universe_file(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        logger.warning("evren dosyası okunamadı, yeniden hesaplanacak: %s", path)
        return None
    if not isinstance(payload, dict) or "symbols" not in payload or "computed_at" not in payload:
        return None
    return payload


def _write_universe_file(
    path: Path, symbols: Sequence[str], stamp: pd.Timestamp, config: dict[str, Any]
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "source": str(get_setting(config, "exchange.name")),
        "inst_type": str(get_setting(config, "exchange.inst_type")),
        "quote_ccy": str(get_setting(config, "exchange.quote_ccy")),
        "universe_size": int(get_setting(config, "universe_size")),
        "computed_at": stamp.isoformat(),
        "symbols": list(symbols),
    }
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
        handle.write("\n")


# --------------------------------------------------------------------------- #
# Mum verisi
# --------------------------------------------------------------------------- #
def fetch_ohlcv(
    config: dict[str, Any],
    symbol: str,
    *,
    client: OKXClient | None = None,
    now: pd.Timestamp | None = None,
) -> pd.DataFrame:
    """Sembolün kapanmış barlarını döndürür; önbellekte olanları yeniden çekmez."""
    stamp = _utc_now(now)
    bar = okx_bar(config)
    duration = bar_duration(bar)
    history_bars = int(get_setting(config, "data.history_bars"))
    active = client if client is not None else OKXClient.from_config(config)

    cache_file = _cache_path(config, symbol, bar)
    cached = _read_cache(cache_file, OHLCV_COLUMNS)
    last_cached = cached.index[-1] if not cached.empty else None

    fresh = _download_candles(
        active,
        config,
        symbol,
        bar=bar,
        duration=duration,
        now=stamp,
        stop_at=last_cached,
        max_bars=history_bars,
    )
    merged = _merge_frames(cached, fresh)
    if not merged.empty:
        _write_cache(cache_file, merged)
    return merged.tail(history_bars)


def _download_candles(
    client: OKXClient,
    config: dict[str, Any],
    symbol: str,
    *,
    bar: str,
    duration: pd.Timedelta,
    now: pd.Timestamp,
    stop_at: pd.Timestamp | None,
    max_bars: int,
) -> pd.DataFrame:
    """Barları yeniden eskiye doğru sayfalayarak indirir.

    OKX istek başına bar sayısını sınırlar (candles ~300, history-candles ~100), bu yüzden
    sayfalama şart. `after` parametresi "verilen ts'den DAHA ESKİ kayıtlar" demektir; en yeni
    sayfadan başlayıp her turda gördüğümüz en eski ts'yi imleç olarak geri veriyoruz.
    Güncel uç /market/candles ile, daha derin geçmiş /market/history-candles ile gelir.
    `stop_at` verilirse (önbellekteki son bar) o barda durur — yalnızca eksik barlar çekilir.
    """
    endpoints = (
        (_CANDLE_ENDPOINT, int(get_setting(config, "exchange.candles_limit"))),
        (_HISTORY_CANDLE_ENDPOINT, int(get_setting(config, "exchange.history_candles_limit"))),
    )

    rows: dict[pd.Timestamp, tuple[float, ...]] = {}
    cursor_ms: int | None = None
    endpoint_index = 0

    while endpoint_index < len(endpoints):
        path, limit = endpoints[endpoint_index]
        params: dict[str, str] = {"instId": symbol, "bar": bar, "limit": str(limit)}
        if cursor_ms is not None:
            params["after"] = str(cursor_ms)

        page = client.get(path, params)
        if not page:
            endpoint_index += 1
            continue
        previous_cursor_ms = cursor_ms

        oldest_ms: int | None = None
        for raw in page:
            oldest_ms = int(raw[0]) if oldest_ms is None else min(oldest_ms, int(raw[0]))
            parsed = _parse_candle(raw, duration=duration, now=now)
            if parsed is None:
                continue
            ts, values = parsed
            if stop_at is not None and ts <= stop_at:
                continue
            rows[ts] = values

        cursor_ms = oldest_ms
        oldest_ts = _to_utc(oldest_ms) if oldest_ms is not None else None

        reached_cache = stop_at is not None and oldest_ts is not None and oldest_ts <= stop_at
        if reached_cache or len(rows) >= max_bars:
            break
        # İmleç ilerlemediyse uç aynı sayfayı tekrarlıyor demektir: sonsuz döngüye
        # girmek yerine bir sonraki uca geçilir (ya da indirme bitirilir).
        if cursor_ms is None or cursor_ms == previous_cursor_ms or len(page) < limit:
            endpoint_index += 1

    return _rows_to_frame(rows).tail(max_bars)


def _parse_candle(
    raw: Sequence[Any], *, duration: pd.Timedelta, now: pd.Timestamp
) -> tuple[pd.Timestamp, tuple[float, ...]] | None:
    """OKX mum satırını çevirir; KAPANMAMIŞ bar için None döner (look-ahead yasağı)."""
    if len(raw) < 6:
        return None
    # Satırın son alanı `confirm`: "0" = bar hâlâ oluşuyor. Bu barı almak stratejiye
    # henüz gerçekleşmemiş bir kapanışı göstermek olurdu (CLAUDE.md kural 12).
    if len(raw) >= 9 and str(raw[8]) != _CONFIRMED:
        return None
    ts = _to_utc(raw[0])
    # confirm alanı olmayan/eski yanıtlara karşı ikinci savunma: barın kapanış anı
    # (açılış + timeframe) henüz gelmediyse bar kapanmamıştır.
    if ts + duration > now:
        return None
    values = tuple(_to_float(raw[index]) for index in range(1, 6))
    return ts, values


def _rows_to_frame(rows: dict[pd.Timestamp, tuple[float, ...]]) -> pd.DataFrame:
    if not rows:
        return _empty_frame(OHLCV_COLUMNS)
    frame = pd.DataFrame.from_dict(rows, orient="index", columns=list(OHLCV_COLUMNS))
    frame.index.name = "ts"
    return frame.sort_index()


# --------------------------------------------------------------------------- #
# Funding geçmişi
# --------------------------------------------------------------------------- #
def fetch_funding(
    config: dict[str, Any],
    symbol: str,
    *,
    client: OKXClient | None = None,
    now: pd.Timestamp | None = None,
) -> pd.Series:
    """Sembolün funding oranı geçmişini (zaman indeksli seri) döndürür."""
    stamp = _utc_now(now)
    if not bool(get_setting(config, "funding.enabled")):
        return _empty_funding_series()

    periods = int(get_setting(config, "data.funding_history_periods"))
    limit = int(get_setting(config, "exchange.funding_limit"))
    active = client if client is not None else OKXClient.from_config(config)

    cache_file = _cache_path(config, symbol, "funding")
    cached = _read_cache(cache_file, (FUNDING_COLUMN,))
    stop_at = cached.index[-1] if not cached.empty else None

    rows: dict[pd.Timestamp, tuple[float, ...]] = {}
    cursor_ms: int | None = None
    while len(rows) < periods:
        params: dict[str, str] = {"instId": symbol, "limit": str(limit)}
        if cursor_ms is not None:
            params["after"] = str(cursor_ms)
        page = active.get(_FUNDING_ENDPOINT, params)
        if not page:
            break
        previous_cursor_ms = cursor_ms

        oldest_ms: int | None = None
        for raw in page:
            funding_time = int(raw["fundingTime"])
            oldest_ms = funding_time if oldest_ms is None else min(oldest_ms, funding_time)
            ts = _to_utc(funding_time)
            # Gelecekte tahakkuk edecek funding bilgisi de look-ahead'dir.
            if ts > stamp or (stop_at is not None and ts <= stop_at):
                continue
            rows[ts] = (_to_float(raw.get("fundingRate")),)

        cursor_ms = oldest_ms
        oldest_ts = _to_utc(oldest_ms) if oldest_ms is not None else None
        if stop_at is not None and oldest_ts is not None and oldest_ts <= stop_at:
            break
        # İmleç ilerlemiyorsa (uç `after`'ı yok sayıyorsa) sayfalama sonsuza gider.
        if cursor_ms is None or cursor_ms == previous_cursor_ms or len(page) < limit:
            break

    fresh = pd.DataFrame.from_dict(rows, orient="index", columns=[FUNDING_COLUMN])
    if not fresh.empty:
        fresh.index.name = "ts"
        fresh = fresh.sort_index()
    merged = _merge_frames(cached, fresh).tail(periods)
    if not merged.empty:
        _write_cache(cache_file, merged)
    return merged[FUNDING_COLUMN].rename(symbol)


def _empty_funding_series() -> pd.Series:
    index = pd.DatetimeIndex([], tz="UTC", name="ts")
    return pd.Series([], index=index, dtype="float64", name=FUNDING_COLUMN)


# --------------------------------------------------------------------------- #
# MarketData anlık görüntüsü
# --------------------------------------------------------------------------- #
def load_market_data(
    config: dict[str, Any] | None = None,
    *,
    symbols: Iterable[str] | None = None,
    client: OKXClient | None = None,
    now: pd.Timestamp | None = None,
) -> MarketData:
    """Tüm stratejilerin göreceği tek anlık görüntüyü üretir (CLAUDE.md kural 5).

    `as_of` sabit bir çıpaya bağlıdır: BTC referans sembolünün son kapanmış barı.
    Sembollerin ortak (minimum) barını kullanmak iki sorun üretiyordu: (a) döngüsel
    bağımlılık — bayat sembolü dışlamak için `as_of`, `as_of` için sembol listesi
    gerekiyordu; (b) tek bir gecikmiş sembol turun "şimdi"sini bir bar geri çekebiliyordu,
    bu da zaten işlenmiş bir barın tekrar işlenmesi (çift işlem) ya da turun ilerlememesi
    (donmuş sistem) demekti. BTC hem her modelin rejim filtresinde referans hem de en likit
    sembol; en az gecikecek çıpa odur.

    `as_of` barına sahip olmayan semboller o tur dışlanır ve loglanır — hangi turda kaç
    sembolün görülebildiği sonradan denetlenebilsin diye. `data.max_staleness_bars` artık
    sembol başına tolerans değil, çıpanın kendi tazeliğinin sınırıdır: BTC verisi bundan
    daha geride kalmışsa anlık görüntü üretilmez (bayat veriyle işlem açmaktansa tur düşer).
    """
    active_config = config if config is not None else load_config()
    stamp = _utc_now(now)
    active = client if client is not None else OKXClient.from_config(active_config)
    duration = bar_duration(okx_bar(active_config))

    requested = (
        list(symbols)
        if symbols is not None
        else load_universe(active_config, client=active, now=stamp)
    )
    btc_symbol = str(get_setting(active_config, "exchange.btc_reference"))
    wanted = list(dict.fromkeys([*requested, btc_symbol]))

    frames: dict[str, pd.DataFrame] = {}
    for symbol in wanted:
        try:
            frame = fetch_ohlcv(active_config, symbol, client=active, now=stamp)
        except OKXError as exc:
            # Tek sembolün başarısızlığı turu düşürmez; hangi semboller eksik, defterden
            # değil logtan izlenir.
            logger.warning("%s mum verisi alınamadı, atlanıyor: %s", symbol, exc)
            continue
        if frame.empty:
            logger.warning("%s için kapanmış bar yok, atlanıyor", symbol)
            continue
        frames[symbol] = frame

    if btc_symbol not in frames:
        raise OKXError(f"BTC referansı ({btc_symbol}) olmadan anlık görüntü üretilemez")

    as_of = _anchor_as_of(
        frames[btc_symbol],
        symbol=btc_symbol,
        now=stamp,
        duration=duration,
        max_staleness_bars=int(get_setting(active_config, "data.max_staleness_bars")),
    )
    usable = _symbols_at_anchor(frames, as_of=as_of, duration=duration)

    ohlcv = {
        symbol: frames[symbol].loc[:as_of]
        for symbol in usable
        if symbol != btc_symbol or symbol in requested
    }
    _log_snapshot_coverage(as_of, requested=requested, visible=ohlcv)

    funding: dict[str, pd.Series] = {}
    for symbol in ohlcv:
        try:
            series = fetch_funding(active_config, symbol, client=active, now=stamp)
        except OKXError as exc:
            logger.warning("%s funding geçmişi alınamadı: %s", symbol, exc)
            continue
        funding[symbol] = series.loc[:as_of]

    return MarketData(
        ohlcv=ohlcv,
        btc=frames[btc_symbol].loc[:as_of],
        funding=funding,
        as_of=as_of,
    )


def _anchor_as_of(
    btc_frame: pd.DataFrame,
    *,
    symbol: str,
    now: pd.Timestamp,
    duration: pd.Timedelta,
    max_staleness_bars: int,
) -> pd.Timestamp:
    """Turun "şimdi"si: BTC'nin son kapanmış barı.

    Çıpanın kendisi bayatsa hiçbir sembol bunu telafi edemez — bu durumda anlık görüntü
    üretmek, borsa/veri kesintisi sırasında eski bir barı yeniymiş gibi işlemek olurdu.
    """
    as_of = btc_frame.index[-1]
    expected = now.floor(duration) - duration
    if as_of < expected - duration * max_staleness_bars:
        raise OKXError(
            f"{symbol} çıpası bayat: son kapanmış bar {as_of}, beklenen {expected}"
        )
    return as_of


def _symbols_at_anchor(
    frames: dict[str, pd.DataFrame],
    *,
    as_of: pd.Timestamp,
    duration: pd.Timedelta,
) -> list[str]:
    """`as_of` barına sahip sembolleri seçer, kalanları gerekçesiyle loglar."""
    usable: list[str] = []
    # Sıra `frames`in ekleme sırasıdır (evrenin ciro sıralaması); alfabetik sıralamak
    # evrenin rank anlamını sessizce değiştirirdi.
    for symbol, frame in frames.items():
        if as_of in frame.index:
            usable.append(symbol)
            continue
        last = frame.index[-1]
        if last < as_of:
            reason = f"son bar {last}, çıpanın {int((as_of - last) / duration)} bar gerisinde"
        else:
            reason = f"seride {as_of} barı yok (son bar {last}, seri boşluklu)"
        logger.warning("%s tura alınmıyor: %s", symbol, reason)
    return usable


def _log_snapshot_coverage(
    as_of: pd.Timestamp, *, requested: Sequence[str], visible: dict[str, pd.DataFrame]
) -> None:
    """Tur başına tek satırlık kapsama özeti.

    Modellerin o turda kaç sembol görebildiği (ve hangilerini göremediği) sonradan
    denetlenebilmeli: bir modelin zayıf sonucu stratejiden mi yoksa daralmış bir
    evrenden mi geldiği ancak böyle ayrılabilir.
    """
    missing = [symbol for symbol in requested if symbol not in visible]
    logger.info(
        "anlık görüntü as_of=%s: %d/%d sembol görülebilir%s",
        as_of,
        len(requested) - len(missing),
        len(requested),
        f", dışlanan: {', '.join(missing)}" if missing else "",
    )


# --------------------------------------------------------------------------- #
# Parquet önbelleği
# --------------------------------------------------------------------------- #
def _cache_path(config: dict[str, Any], symbol: str, suffix: str) -> Path:
    directory = project_path(str(get_setting(config, "data.cache_dir")))
    safe_symbol = symbol.replace("/", "_")
    return directory / f"{safe_symbol}_{suffix}.parquet"


def _empty_frame(columns: Sequence[str]) -> pd.DataFrame:
    index = pd.DatetimeIndex([], tz="UTC", name="ts")
    return pd.DataFrame({column: pd.Series(dtype="float64") for column in columns}, index=index)


def _read_cache(path: Path, columns: Sequence[str]) -> pd.DataFrame:
    if not path.is_file():
        return _empty_frame(columns)
    try:
        frame = pd.read_parquet(path)
    except Exception as exc:  # bozuk önbellek koşuyu düşürmez, yeniden indirilir
        logger.warning("önbellek okunamadı, yeniden indirilecek (%s): %s", path, exc)
        return _empty_frame(columns)
    return _normalize_frame(frame, columns)


def _write_cache(path: Path, frame: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(path)


def _normalize_frame(frame: pd.DataFrame, columns: Sequence[str]) -> pd.DataFrame:
    if frame.empty:
        return _empty_frame(columns)
    index = pd.DatetimeIndex(frame.index)
    index = index.tz_localize("UTC") if index.tz is None else index.tz_convert("UTC")
    normalized = frame.copy()
    normalized.index = index
    normalized.index.name = "ts"
    missing = [column for column in columns if column not in normalized.columns]
    if missing:
        raise OKXError(f"önbellek dosyasında eksik kolon: {missing}")
    return normalized[list(columns)].sort_index()


def _merge_frames(cached: pd.DataFrame, fresh: pd.DataFrame) -> pd.DataFrame:
    if fresh.empty:
        return cached
    if cached.empty:
        return fresh
    combined = pd.concat([cached, fresh])
    # Aynı ts iki kez geldiyse yeni çekilen kayıt geçerlidir (borsa düzeltmeleri).
    combined = combined[~combined.index.duplicated(keep="last")]
    return combined.sort_index()
