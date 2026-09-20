"""Kayma varsayımının sembol bazında TUTUP TUTMADIĞINI ölçer. SALT OKUNUR.

Cevapladığı soru tek: *`slippage_base = 0.0005` her sembolde aynı şeyi mi anlatıyor?*

**Neden şimdi bu ölçüm.** Karar 35'in kaldıraç tablosu ve karar 36 birlikte tek bir açık
kapı bırakıyor:

    stop% (R ölçeği)     nötr        (karar 35, özdeşlik)
    tutuş süresi ↑       doğrulandı  (−0.15 -> −0.01, karar 31/32)
    volatilite rejimi ↑  YANLIŞ      (0.263 -> 0.253, karar 36)
    maliyet% ↓           HENÜZ DENENMEDİ

Yani "sürüklenmesi daha büyük bir sinyal bulmak" dışındaki tek kaldıraç maliyettir. Ve
maliyetin bir parçası — komisyon — borsanın ilan ettiği bir sayıdır, ölçülecek bir şey
yoktur; kayma ise bir VARSAYIMDIR (`docs/backtest.md > 5c` bunu açıkça kabul eder) ve hiç
sınanmadı.

**Bu script bir ÖLÇÜMDÜR, bir değişiklik değil.** Deftere yazmaz, `config.yaml`a dokunmaz,
hiçbir modelin davranışını değiştirmez (CLAUDE.md kural 1/2/3/7). Sembole bağlı kayma
uygulamak AYRI bir karardır ve iki sebeple bu scriptin sonucunu beklemek zorundadır:

1. **Veri olmadan 13 varsayım, tek varsayımdan kötüdür.** Tek bir düz sayı yanlışsa
   yanlışlığı her sembolde AYNI yöndedir ve modeller arası kıyası bozmaz (kural 6). On üç
   uydurma sayı ise hangi satırın neden ayrıştığını görünmez yapar.
2. **Defteri tarihli olarak böler.** `fee_rate` karar 25'te değiştiğinde öncesi ve sonrası
   aynı kurallarla koşmadı ve Kapı 0'ın geçerlilik sınırı bunu yazmak zorunda kaldı
   (`docs/backtest.md > 1`). Geriye dönük uygulanamaz (kural 1: defter append-only).

**Hangi borsanın kitabı okunuyor: BYBIT.** Veri OKX'ten gelir (`exchange.*`) ama maliyeti
ödeyen taraf hesabın tutulduğu yerdir ve o Bybit'tir — `config.yaml`ın `fee_rate` yorumu
tam olarak bu ayrımı yazar. OKX'in kitabını ölçüp Bybit'in kaymasını bildiğimizi iddia
etmek, aynı hatanın kitap tarafındaki hâli olurdu. `--venue okx` ile öteki taraf da
ölçülebilir; ikisinin FARKI da bir bilgidir.

**Emir boyu uydurulmaz, DEFTERDEN okunur.** Kitap derinliğinin maliyeti emir boyuna
bağlıdır; "tipik" bir boy varsaymak ölçümü o varsayıma bağlardı. Script her sembol için
yarışmacıların gerçekten açtığı pozisyonların MEDYAN notional'ını kullanır (`--notional`
ile ezilebilir). Defterde o sembolün işlemi yoksa satır `—` ile geçilir: uydurulmuş bir
boyla hesaplanan impact, ölçülmemiş bir sayıyı ölçülmüş gibi gösterirdi.

**Tek anlık görüntü yeterli değildir.** Spread seansa göre değişir (Asya gecesi ile ABD
açılışı aynı kitap değildir), bu yüzden script N örnek alır ve MEDYANI raporlar. Tek
snapshot, ölçümü tesadüfen o dakikaya bağlardı.

Kullanım (depo kökünden):
    python scripts/measure_slippage.py --samples 20 --interval 30
    python scripts/measure_slippage.py --layer scalp --venue okx
    python scripts/measure_slippage.py --samples 1 --notional 5000   # hızlı bakış
"""

from __future__ import annotations

import argparse
import json
import logging
import statistics
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config import get_setting, load_config  # noqa: E402
from core.layers import DEFAULT_LAYER, resolve_layer  # noqa: E402
from core.ledger import Ledger  # noqa: E402

logger = logging.getLogger("measure_slippage")

# Kitabın kaç seviyesi çekiliyor. 50, 5.000-20.000 USDT'lik bir emrin likit
# perpetual'larda tükettiğinden fazlasıdır; yetmediği durum RAPORLANIR (`derinlik yok`),
# sessizce son seviyeden doldurulmuş gibi yazılmaz.
BOOK_DEPTH = 50


# --------------------------------------------------------------------------- #
# Kitap: saf hesap (ağ yok — test edilebilir)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, kw_only=True)
class Book:
    """Tek bir anlık görüntü: (fiyat, miktar) çiftleri, en iyiden kötüye sıralı."""

    bids: tuple[tuple[float, float], ...]
    asks: tuple[tuple[float, float], ...]

    @property
    def mid(self) -> float:
        return (self.bids[0][0] + self.asks[0][0]) / 2.0


def half_spread_pct(book: Book) -> float:
    """Yarı spread, mid'in yüzdesi.

    Piyasa emriyle giren biri en iyi karşı fiyattan doldurulur, yani mid'e göre bedeli
    spread'in YARISIDIR. Tam spread'i kullanmak, aynı maliyeti giriş ve çıkışta ikişer
    kez saymak olurdu — `core/portfolio.py` kaymayı zaten her dolumda ayrı uygular.
    """
    return (book.asks[0][0] - book.bids[0][0]) / 2.0 / book.mid * 100.0


def impact_pct(book: Book, *, notional: float, side: str) -> float | None:
    """`notional` USDT'lik piyasa emrinin mid'e göre ortalama dolum sapması (%).

    Kitabın seviyeleri sırayla tüketilir; sonuç dolum VWAP'inin mid'den uzaklığıdır ve
    yarı spread'i DE içerir (ilk seviye zaten en iyi karşı fiyattır).

    Kitap emri karşılamaya yetmezse None döner: son seviyeden devam etmiş gibi hesaplamak,
    ölçülemeyen bir derinliği ölçülmüş gibi gösterirdi.
    """
    levels = book.asks if side == "buy" else book.bids
    mid = book.mid
    remaining = float(notional)
    spent = 0.0
    filled = 0.0

    for price, size in levels:
        level_notional = price * size
        take = min(remaining, level_notional)
        spent += take
        filled += take / price
        remaining -= take
        if remaining <= 0.0:
            break

    if remaining > 0.0 or filled <= 0.0:
        return None

    fill_price = spent / filled
    return abs(fill_price - mid) / mid * 100.0


def round_trip_cost_pct(book: Book, *, notional: float) -> float | None:
    """Giriş + çıkışın toplam kayma maliyeti (%): alışın impact'i + satışın impact'i.

    İki yön ayrı hesaplanır çünkü kitap simetrik değildir; ortalamayı almak, tek taraflı
    bir kitapta maliyeti sistematik olarak olduğundan düşük gösterirdi.
    """
    buy = impact_pct(book, notional=notional, side="buy")
    sell = impact_pct(book, notional=notional, side="sell")
    if buy is None or sell is None:
        return None
    return buy + sell


# --------------------------------------------------------------------------- #
# Emir boyu: DEFTERDEN
# --------------------------------------------------------------------------- #
def typical_notional(trades: Iterable[Mapping[str, Any]]) -> dict[str, float]:
    """Sembol -> yarışmacıların açtığı pozisyonların MEDYAN notional'ı.

    Medyan, ortalama değil: tek bir büyük pozisyon (dar stop -> büyük boyut) ortalamayı
    kendine çeker ve impact'i hiç açılmamış bir emir boyunda ölçerdik.
    """
    by_symbol: dict[str, list[float]] = {}
    for row in trades:
        symbol = str(row.get("symbol", ""))
        try:
            notional = float(row.get("notional", "") or 0.0)
        except (TypeError, ValueError):
            continue
        if symbol and notional > 0.0:
            by_symbol.setdefault(symbol, []).append(notional)
    return {
        symbol: statistics.median(values) for symbol, values in sorted(by_symbol.items())
    }


# --------------------------------------------------------------------------- #
# Ağ: iki borsanın kitabı
# --------------------------------------------------------------------------- #
class BookError(RuntimeError):
    """Kitap çekilemedi."""


def _get_json(url: str, params: Mapping[str, str], *, timeout: float) -> dict[str, Any]:
    query = urllib.parse.urlencode(dict(params))
    with urllib.request.urlopen(f"{url}?{query}", timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_book_bybit(symbol: str, *, timeout: float) -> Book:
    """Bybit lineer perpetual kitabı. Sembol `BTC-USDT-SWAP` -> `BTCUSDT`."""
    base, quote, *_ = symbol.split("-")
    payload = _get_json(
        "https://api.bybit.com/v5/market/orderbook",
        {"category": "linear", "symbol": f"{base}{quote}", "limit": str(BOOK_DEPTH)},
        timeout=timeout,
    )
    if str(payload.get("retCode")) != "0":
        raise BookError(f"bybit {symbol}: {payload.get('retMsg')}")
    result = payload.get("result") or {}
    return _book_from_levels(result.get("b") or [], result.get("a") or [], symbol=symbol)


def fetch_book_okx(symbol: str, *, rest_base: str, timeout: float) -> Book:
    """OKX kitabı — kıyas için. Maliyeti ödeyen taraf burası DEĞİLDİR (bkz. modül başlığı)."""
    payload = _get_json(
        f"{rest_base.rstrip('/')}/api/v5/market/books",
        {"instId": symbol, "sz": str(BOOK_DEPTH)},
        timeout=timeout,
    )
    if str(payload.get("code", "0")) != "0":
        raise BookError(f"okx {symbol}: {payload.get('msg')}")
    data = payload.get("data") or []
    if not data:
        raise BookError(f"okx {symbol}: boş kitap")
    return _book_from_levels(data[0].get("bids") or [], data[0].get("asks") or [], symbol=symbol)


def _book_from_levels(bids: Sequence[Any], asks: Sequence[Any], *, symbol: str) -> Book:
    def parse(levels: Sequence[Any]) -> tuple[tuple[float, float], ...]:
        return tuple(
            (float(level[0]), float(level[1]))
            for level in levels
            if len(level) >= 2 and float(level[1]) > 0.0
        )

    parsed_bids, parsed_asks = parse(bids), parse(asks)
    if not parsed_bids or not parsed_asks:
        raise BookError(f"{symbol}: kitabın bir tarafı boş")
    return Book(bids=parsed_bids, asks=parsed_asks)


# --------------------------------------------------------------------------- #
# Ölçüm
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, kw_only=True)
class SymbolResult:
    symbol: str
    samples: int
    notional: float | None
    half_spread_pct: float | None
    round_trip_pct: float | None
    too_thin: int  # kitabın emri karşılayamadığı örnek sayısı


def measure(
    symbols: Sequence[str],
    *,
    notionals: Mapping[str, float],
    fetch: Any,
    samples: int,
    interval: float,
    sleep: Any = time.sleep,
) -> list[SymbolResult]:
    """Her sembol için N örnek alır ve MEDYANLARI döndürür.

    Örnekler sembolleri dolaşarak alınır, sembol sembol değil: aksi hâlde BTC sabah,
    PENGU öğleden sonra ölçülür ve aradaki fark spread farkı sanılırdı.
    """
    spreads: dict[str, list[float]] = {symbol: [] for symbol in symbols}
    trips: dict[str, list[float]] = {symbol: [] for symbol in symbols}
    thin: dict[str, int] = {symbol: 0 for symbol in symbols}

    for index in range(samples):
        if index:
            sleep(interval)
        for symbol in symbols:
            try:
                book = fetch(symbol)
            except Exception as exc:  # noqa: BLE001 — ölçüm aracı; sembol düşerse devam
                logger.warning("%s kitabı alınamadı: %s", symbol, exc)
                continue
            spreads[symbol].append(half_spread_pct(book))
            notional = notionals.get(symbol)
            if notional is None:
                continue
            trip = round_trip_cost_pct(book, notional=notional)
            if trip is None:
                thin[symbol] += 1
            else:
                trips[symbol].append(trip)

    return [
        SymbolResult(
            symbol=symbol,
            samples=len(spreads[symbol]),
            notional=notionals.get(symbol),
            half_spread_pct=statistics.median(spreads[symbol]) if spreads[symbol] else None,
            round_trip_pct=statistics.median(trips[symbol]) if trips[symbol] else None,
            too_thin=thin[symbol],
        )
        for symbol in symbols
    ]


def format_results(
    results: Sequence[SymbolResult], *, assumed_round_trip_pct: float, venue: str
) -> str:
    """Ölçüleni VARSAYILANIN yanına koyar: tek başına bir spread sayısı bir şey söylemez."""
    out = [
        f"\nKAYMA ÖLÇÜMÜ — {venue} kitabı (SALT OKUNUR; config değişikliği AYRI bir karardır)",
        "-" * 84,
        f"{'sembol':22s}{'örnek':>6}{'notional':>11}{'yarıSpread%':>13}"
        f"{'turMaliyet%':>13}{'varsayım%':>11}{'kat':>7}",
    ]
    ratios: list[float] = []
    for row in results:
        measured = row.round_trip_pct
        ratio = None if measured is None else measured / assumed_round_trip_pct
        if ratio is not None:
            ratios.append(ratio)
        out.append(
            f"{row.symbol:22s}{row.samples:6d}"
            f"{_cell(row.notional, '{:11.0f}')}"
            f"{_cell(row.half_spread_pct, '{:13.4f}')}"
            f"{_cell(measured, '{:13.4f}')}"
            f"{assumed_round_trip_pct:11.4f}"
            f"{_cell(ratio, '{:7.2f}')}"
            + (f"   ⚠ {row.too_thin} örnekte derinlik yetmedi" if row.too_thin else "")
        )
    out.append("-" * 84)
    out.append(
        "`varsayım%` = 2 × slippage_base (giriş + çıkış), yani config'in bugün her sembol "
        "için ödediğini söylediği kayma."
    )
    out.append(
        "`kat` 1'e yakınsa varsayım o sembolde tutuyor; 1'in belirgin üstündeyse o satırın "
        "ort. R'si olduğundan İYİ raporlanıyor demektir."
    )
    if ratios:
        out.append(
            f"Kat dağılımı: min {min(ratios):.2f} · medyan {statistics.median(ratios):.2f} "
            f"· maks {max(ratios):.2f}"
        )
        out.append(
            "Yayılım dar (ör. 0.7–1.4) ise tek bir düz sayı savunulabilir kalır ve "
            "sembole bağlı kayma GEREKMEZ — 13 ayrı varsayım, tek varsayımdan kötüdür."
        )
    out.append(
        "\nBu tablo bir ÖLÇÜMDÜR. config.yaml'a sembol bazlı kayma yazmak ayrı bir karardır: "
        "defteri tarihli olarak böler (karar 25'te fee_rate'in böldüğü gibi) ve geriye dönük "
        "uygulanamaz (kural 1)."
    )
    return "\n".join(out) + "\n"


def _cell(value: float | None, template: str) -> str:
    width = int(template.split(":")[1].split(".")[0])
    return "—".rjust(width) if value is None else template.format(value)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")

    layer = resolve_layer(load_config(args.config), args.layer)
    config = layer.config
    symbols = list(layer.symbols) if layer.symbols else []
    if not symbols:
        logger.error(
            "%s katmanının sabit evreni yok: kayma ölçümü sembol listesi ister "
            "(hacimden seçilen evren tur tur değişir ve ölçüm neyi ölçtüğünü söyleyemez)",
            layer.name,
        )
        return 2

    if args.notional:
        notionals = {symbol: float(args.notional) for symbol in symbols}
    else:
        ledger = Ledger(layer.ledger_root)
        rows: list[Mapping[str, Any]] = []
        for model in layer.models:
            try:
                rows.extend(ledger.read_trades(model))
            except Exception as exc:  # noqa: BLE001 — defteri olmayan model ölçümü düşürmez
                logger.warning("%s defteri okunamadı: %s", model, exc)
        notionals = typical_notional(rows)
        missing = [symbol for symbol in symbols if symbol not in notionals]
        if missing:
            logger.warning(
                "defterde işlemi olmayan sembollerde tur maliyeti ölçülmez (%s): "
                "uydurulmuş bir emir boyu, ölçülmemiş bir sayıyı ölçülmüş gibi gösterirdi",
                ", ".join(missing),
            )

    timeout = float(get_setting(config, "exchange.request_timeout_sec"))
    if args.venue == "bybit":
        def fetch(symbol: str) -> Book:
            return fetch_book_bybit(symbol, timeout=timeout)
    else:
        rest_base = str(get_setting(config, "exchange.rest_base"))
        def fetch(symbol: str) -> Book:
            return fetch_book_okx(symbol, rest_base=rest_base, timeout=timeout)

    results = measure(
        symbols, notionals=notionals, fetch=fetch,
        samples=args.samples, interval=args.interval,
    )
    assumed = 2.0 * float(get_setting(config, "slippage_base")) * 100.0
    print(format_results(results, assumed_round_trip_pct=assumed, venue=args.venue))
    return 0


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--layer", default=DEFAULT_LAYER)
    parser.add_argument("--config", default=None)
    parser.add_argument(
        "--venue", default="bybit", choices=["bybit", "okx"],
        help="kitabı okunacak borsa; VARSAYILAN bybit (maliyeti ödeyen taraf)",
    )
    parser.add_argument(
        "--samples", type=int, default=20,
        help="sembol başına anlık görüntü sayısı; medyan raporlanır",
    )
    parser.add_argument(
        "--interval", type=float, default=30.0, help="örnekler arası bekleme (saniye)",
    )
    parser.add_argument(
        "--notional", type=float, default=None,
        help="emir boyunu defterden okumak yerine sabitle (USDT)",
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(main())
