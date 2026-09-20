"""Strateji testleri için sentetik MarketData kurucuları.

Neden ortak dosya: iki strateji testi de "200+ barlık geçmiş + kontrollü son bar" kurmak
zorunda (EMA200 ve Bollinger/RSI pencereleri bunu şart koşar). Kurucuyu kopyalamak, iki
testin farklı `as_of` ya da farklı bar aralığı kullanmasına ve "aynı veriyi gördüler"
varsayımının testlerde bile bozulmasına yol açardı.
"""

from __future__ import annotations

from typing import Mapping, Sequence

import pandas as pd

from strategies.base import MarketData

START = pd.Timestamp("2026-01-01 00:00:00", tz="UTC")


def frame(
    closes: Sequence[float],
    *,
    spread: float = 0.5,
    volumes: Sequence[float] | None = None,
    start: pd.Timestamp = START,
    freq: str = "4h",
    highs: Sequence[float] | None = None,
    lows: Sequence[float] | None = None,
) -> pd.DataFrame:
    """Kapanış dizisinden OHLCV çerçevesi; high/low kapanışın `spread` kadar uzağında.

    `volumes` yalnızca hacim teyidini ölçen modeller için doldurulur (varsayılan sabit 1.0,
    yani "hacim ayrımı yok"). `start` ise dengeleme barına duyarlı modeller içindir: takvim
    gününü sabitlemeden Pazartesi 00:00 UTC koşulu test edilemez.

    `freq` scalp katmanı içindir ("15min"): iki katman aynı kurucuyu kullanmalı ki testler
    de "aynı veriyi gördüler" varsayımını iki zaman diliminde birden kurabilsin. `highs`/
    `lows` ise barın aralığını kapanıştan bağımsız kurması gereken testler için (VWAP'e
    dokunuş, açılış aralığı): `spread` tüm barlara aynı genişliği verir, bu ikisi bara
    özel aralık yazar.
    """
    index = pd.date_range(start, periods=len(closes), freq=freq, tz="UTC", name="ts")
    values = [float(close) for close in closes]
    return pd.DataFrame(
        {
            "open": values,
            "high": [close + spread for close in values] if highs is None else [float(h) for h in highs],
            "low": [close - spread for close in values] if lows is None else [float(l) for l in lows],
            "close": values,
            "volume": [1.0] * len(values) if volumes is None else [float(v) for v in volumes],
        },
        index=index,
    )


def market(
    ohlcv: Mapping[str, pd.DataFrame],
    *,
    btc: pd.DataFrame | None = None,
    as_of: pd.Timestamp | None = None,
    funding: Mapping[str, pd.Series] | None = None,
) -> MarketData:
    frames = dict(ohlcv)
    reference = btc if btc is not None else next(iter(frames.values()))
    return MarketData(
        ohlcv=frames,
        btc=reference,
        funding=dict(funding) if funding is not None else {},
        as_of=as_of if as_of is not None else reference.index[-1],
    )


def funding_series(
    rates: Sequence[float], *, end: pd.Timestamp, interval_hours: int = 8
) -> pd.Series:
    """Sona `end` anında biten, geriye doğru `interval_hours` adımlı funding geçmişi.

    Seri, funding kapılarını ölçen modeller (failed_breakout önceliği, downtrend_rally'nin
    squeeze kapısı) için var. `end` çıpası `as_of`tur: kural 12 gereği seri "şimdi"den
    ileriye uzanamaz, dolayısıyla son kayıt en fazla `as_of` olabilir.
    """
    index = pd.date_range(
        end=end, periods=len(rates), freq=f"{interval_hours}h", tz="UTC", name="ts"
    )
    return pd.Series([float(rate) for rate in rates], index=index, dtype="float64")


def rising(bars: int = 260, *, start: float = 100.0, step: float = 1.0) -> list[float]:
    """EMA50 > EMA200 rejimi üreten monoton yükseliş."""
    return [start + step * i for i in range(bars)]


def falling(bars: int = 260, *, start: float = 400.0, step: float = 1.0) -> list[float]:
    """EMA50 < EMA200 rejimi üreten monoton düşüş."""
    return [start - step * i for i in range(bars)]


def flat(bars: int = 260, *, level: float = 100.0) -> list[float]:
    return [level] * bars
