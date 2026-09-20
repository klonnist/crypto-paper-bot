#!/usr/bin/env python3
"""Manuel veri kontrolü: 3 sembol için veri çekip son barları yazdırır.

Kullanım:
    python scripts/manual_data_check.py                      # evrenin ilk 3 sembolü
    python scripts/manual_data_check.py BTC-USDT-SWAP ETH-USDT-SWAP SOL-USDT-SWAP
    python scripts/manual_data_check.py --bars 10 --base-url http://127.0.0.1:8000

Bu bir test değil, gözle doğrulama aracıdır: son barın gerçekten KAPANMIŞ olduğunu
(as_of + timeframe <= şimdi) ve zaman damgalarının UTC geldiğini insan gözüyle kontrol
etmek için. Otomatik doğrulama tests/test_data.py içinde.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config import get_setting, load_config  # noqa: E402
from core.data import (  # noqa: E402
    OKXClient,
    OKXError,
    bar_duration,
    load_market_data,
    load_universe,
    okx_bar,
)

DEFAULT_SYMBOL_COUNT = 3


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("symbols", nargs="*", help="OKX instId listesi (boşsa evrenden ilk 3)")
    parser.add_argument("--bars", type=int, default=5, help="yazdırılacak son bar sayısı")
    parser.add_argument("--base-url", default=None, help="OKX REST taban adresini geçersiz kıl")
    parser.add_argument("--verbose", action="store_true", help="retry/backoff loglarını göster")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    config = load_config()
    if args.base_url:
        config["exchange"]["rest_base"] = args.base_url

    client = OKXClient.from_config(config)
    timeframe = okx_bar(config)
    duration = bar_duration(timeframe)

    symbols = args.symbols
    if not symbols:
        try:
            universe = load_universe(config, client=client)
        except OKXError as exc:
            return _fail(exc)
        symbols = universe[:DEFAULT_SYMBOL_COUNT]
        print(f"evren: {len(universe)} sembol, ilk {len(symbols)} kullanılıyor")

    print(f"borsa      : {get_setting(config, 'exchange.rest_base')}")
    print(f"timeframe  : {timeframe} ({duration})")
    print(f"semboller  : {', '.join(symbols)}")

    try:
        market = load_market_data(config, symbols=symbols, client=client)
    except OKXError as exc:
        return _fail(exc)
    now = pd.Timestamp.now(tz="UTC")

    print(f"\nas_of      : {market.as_of}  (şimdi: {now}, çıpa: BTC referansı)")
    dropped = [symbol for symbol in symbols if symbol not in market.ohlcv]
    if dropped:
        print(f"tura alınmayan semboller: {', '.join(dropped)}  (as_of barı yok)")
    closed = market.as_of + duration <= now
    print(f"son bar kapanmış mı: {'EVET' if closed else 'HAYIR — LOOK-AHEAD!'}")

    for symbol, frame in market.ohlcv.items():
        print(f"\n=== {symbol} — {len(frame)} kapanmış bar, son {args.bars} ===")
        print(frame.tail(args.bars).to_string())
        series = market.funding.get(symbol)
        if series is not None and not series.empty:
            print(f"--- funding (son 3 / {len(series)} periyot) ---")
            print(series.tail(3).to_string())
        else:
            print("--- funding verisi yok ---")

    print(f"\nBTC referansı: {len(market.btc)} bar, son bar {market.btc.index[-1]}")
    return 0 if closed else 1


def _fail(exc: OKXError) -> int:
    """Borsaya ulaşılamadı: traceback yerine okunur bir mesaj ve ayırt edilebilir çıkış kodu."""
    print(f"\nHATA: OKX'e ulaşılamadı -> {exc}", file=sys.stderr)
    print(
        "Ağ/çıkış politikası OKX'i engelliyorsa --base-url ile erişilebilir bir uç verin.",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
