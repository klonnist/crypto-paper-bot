"""`confluence` için confidence kademesi: YALNIZCA deftere yazılan bir etiket üretir.

Bu modül bir model DEĞİLDİR ve hiçbir kararı etkilemez. Ürettiği tek şey `reason` metninin
sonuna `| confidence=low|medium|high` olarak eklenen bir etikettir; giriş kararı, yön, stop
mesafesi ve pozisyon boyutu bu modülü hiç görmez. Ayrı dosyada durmasının nedeni de budur:
"kademe hiçbir şeyi etkilemiyor" iddiası tek bir yerden denetlenebilmeli — `confluence.py`
bu modülden yalnızca bir string alır.

**Neden boyutu etkilemiyor:** kaynak tarayıcıda (crypto-scanner) confidence'ın TEK işlevi
pozisyon boyutunu çarpmaktı (low=0.5R, medium=1.0R, high=1.5R). Bu projede boyutlandırma
stratejinin işi değildir (kural 3/11): çarpanı taşımak, modelleri ortak risk biriminden (1R)
çıkarır ve tabloyu kıyaslanamaz kılardı. Kademe yine de hesaplanıyor, çünkü defterden
(`trades.csv`) sonradan gruplanarak "teyit katmanı gerçekten ayırt ediyor mu" sorusu ölçüm
tablosunu hiç kirletmeden cevaplanabilir.

Kademe tanımı kaynaktaki `evaluate_confluence_entry` ile birebir aynıdır:

    high   = yapı VAR ve tam teyitli (evaluate_signal / evaluate_signal_short geçti)
    medium = yalnızca yapı var (iki dip/tepe + mesafe + seviye toleransı)
    low    = yapı yok

"Tam teyit" kaynakta üç kapıdır: çift dip/tepe (kırılım + hacim), RSI diverjansı ve fiyatın
Fibonacci 0.618-0.786 bandında olması. Kaynaktaki Wyckoff/Elliott/Motor-1 katmanları burada
taşınmadı: onlar `is_valid`i değil yalnızca kaynağın kendi confidence/bonus alanlarını
etkiliyordu, yani bu kademede karşılığı yok.

RSI `core/indicators.py`'den okunur (kaynak Wilder yumuşatması kullanıyor, bu depo onu
reddediyor) — diverjans kararı kaynakla birebir aynı çıkmayabilir; bu fark `docs/decisions.md`
karar 12'de kayıtlıdır ve yalnızca ETİKETİ etkiler.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import pandas as pd

from core.indicators import local_highs, local_lows, rsi_series
from strategies.base import Direction

Tier = Literal["low", "medium", "high"]

FRACTAL_ORDER = 3
MIN_BAR_GAP = 8
LEVEL_TOLERANCE = 0.05
VOLUME_MULTIPLE = 1.15
BREAKOUT_WINDOW = 5
VOLUME_PERIOD = 20
RSI_PERIOD = 14
RSI_OVERSOLD = 35.0
RSI_OVERBOUGHT = 65.0
MIN_RSI_GAP = 5.0
DIVERGENCE_MIN_BAR_GAP = 6
FIB_ZONE_TOLERANCE = 0.003
SWING_LOOKBACK = 60


@dataclass(frozen=True, kw_only=True)
class _Structure:
    """Çift dip/tepe yapısı. `found=False` ise yapı hiç kurulmamıştır (kademe: low)."""

    found: bool
    first_index: int = -1
    second_index: int = -1
    confirmed: bool = False  # kırılım + hacim teyidi


@dataclass(frozen=True, kw_only=True)
class Assessment:
    tier: Tier
    structure_present: bool
    fully_confirmed: bool


def assess(frame: pd.DataFrame, direction: Direction) -> Assessment:
    """Kademeyi hesaplar. Çağıran taraf sonucu YALNIZCA metne yazar."""
    structure = (
        _double_bottom(frame) if direction == "long" else _double_top(frame)
    )
    if not structure.found:
        return Assessment(tier="low", structure_present=False, fully_confirmed=False)

    confirmed = structure.confirmed and _divergence(frame, structure, direction) and _in_fib_zone(frame)
    return Assessment(
        tier="high" if confirmed else "medium",
        structure_present=True,
        fully_confirmed=confirmed,
    )


def _double_bottom(frame: pd.DataFrame) -> _Structure:
    return _double_extreme(frame, direction="long")


def _double_top(frame: pd.DataFrame) -> _Structure:
    return _double_extreme(frame, direction="short")


def _double_extreme(frame: pd.DataFrame, *, direction: Direction) -> _Structure:
    """Kaynaktaki `check_double_bottom`/`check_double_top` ile aynı sıra ve eşikler."""
    long_side = direction == "long"
    column = "low" if long_side else "high"
    pivots = (
        local_lows(frame[column], order=FRACTAL_ORDER)
        if long_side
        else local_highs(frame[column], order=FRACTAL_ORDER)
    )
    if len(pivots) < 2:
        return _Structure(found=False)

    second_index, first_index = pivots[-1], pivots[-2]
    if second_index - first_index < MIN_BAR_GAP:
        return _Structure(found=False)

    first_price = float(frame[column].iloc[first_index])
    second_price = float(frame[column].iloc[second_index])
    if abs(second_price - first_price) / first_price > LEVEL_TOLERANCE:
        return _Structure(found=False)

    middle = frame.iloc[first_index : second_index + 1]
    level = float(middle["high"].max()) if long_side else float(middle["low"].min())
    average_volume = float(frame["volume"].iloc[-(VOLUME_PERIOD + 1) : -1].mean())

    # Kaynaktaki pencere mantığı: kırılım ile hacim spike'ı AYNI barda çakışmalı, ama son
    # `BREAKOUT_WINDOW` barın herhangi birinde olabilir.
    breakout = False
    volume_ok = False
    for offset in range(1, BREAKOUT_WINDOW + 1):
        if offset > len(frame):
            break
        bar = frame.iloc[-offset]
        broke = float(bar["close"]) > level if long_side else float(bar["close"]) < level
        if not broke:
            continue
        bar_volume_ok = float(bar["volume"]) > average_volume * VOLUME_MULTIPLE
        if not breakout:
            breakout = True
            volume_ok = bar_volume_ok
        if bar_volume_ok:
            volume_ok = True
            break

    return _Structure(
        found=True,
        first_index=first_index,
        second_index=second_index,
        confirmed=breakout and volume_ok,
    )


def _divergence(frame: pd.DataFrame, structure: _Structure, direction: Direction) -> bool:
    """Fiyat yeni bir uç yaparken RSI yapmıyorsa diverjans vardır (kaynaktaki eşiklerle)."""
    if structure.second_index - structure.first_index < DIVERGENCE_MIN_BAR_GAP:
        return False

    strength = rsi_series(frame["close"], RSI_PERIOD)
    first = float(strength.iloc[structure.first_index])
    second = float(strength.iloc[structure.second_index])
    if pd.isna(first) or pd.isna(second):
        return False

    if direction == "long":
        first_price = float(frame["low"].iloc[structure.first_index])
        second_price = float(frame["low"].iloc[structure.second_index])
        return (
            second_price <= first_price
            and second > first
            and first < RSI_OVERSOLD
            and (second - first) >= MIN_RSI_GAP
        )

    first_price = float(frame["high"].iloc[structure.first_index])
    second_price = float(frame["high"].iloc[structure.second_index])
    return (
        second_price >= first_price
        and second < first
        and first > RSI_OVERBOUGHT
        and (first - second) >= MIN_RSI_GAP
    )


def _in_fib_zone(frame: pd.DataFrame) -> bool:
    """Fiyat son `SWING_LOOKBACK` barın 0.618-0.786 bandında mı (kaynakta yön bağımsız)."""
    window = frame.iloc[-SWING_LOOKBACK:]
    swing_high = float(window["high"].max())
    swing_low = float(window["low"].min())
    span = swing_high - swing_low
    first = swing_high - 0.618 * span
    second = swing_high - 0.786 * span
    lower = min(first, second) * (1 - FIB_ZONE_TOLERANCE)
    upper = max(first, second) * (1 + FIB_ZONE_TOLERANCE)
    return lower <= float(frame["close"].iloc[-1]) <= upper
