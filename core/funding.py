"""Funding/borrow maliyeti simülasyonu (CLAUDE.md kural 2).

Perpetual sözleşmede funding, borsanın belirli anlarda (OKX'te 00:00/08:00/16:00 UTC)
açık pozisyonlar arasında el değiştirdiği ödemedir. Kural tek cümle:

    **Pozitif funding'de LONG öder, SHORT alır; negatifte tersi.**
    tutar = oran × notional  (notional = miktar × o andaki fiyat)

Bu modül maliyeti *hesaplar*, nakde işlemez — nakit hareketi core/portfolio.py'nin işidir.
Kuralın tek yerde durması, 10 modelin de birebir aynı funding'i görmesinin garantisidir.

Oranlar UYDURULMAZ. `MarketData.funding` gerçek OKX funding geçmişidir ve yalnızca TAM
zaman eşleşmesi kabul edilir: bir periyodun verisi yoksa o periyot atlanır ve loglanır.
İleri/geri doldurma (ffill) yapmak, veri boşluğunu sessizce sentetik bir maliyete
çevirmek olurdu — hangi modelin ne kadar funding ödediği o noktadan sonra denetlenemez.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Protocol

import pandas as pd

from core.config import get_setting
from strategies.base import Direction

logger = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class FundingCharge:
    """Tek bir pozisyonun tek bir funding anındaki tahakkuku."""

    symbol: str
    direction: Direction
    ts: pd.Timestamp
    rate: float
    notional: float
    amount: float  # + : alınan, - : ödenen


class FundingPosition(Protocol):
    """Funding'in ihtiyaç duyduğu dar pozisyon yüzü (core/portfolio.OpenPosition uyar)."""

    symbol: str
    direction: Direction
    qty: float
    opened_at: pd.Timestamp


def funding_times(
    *, start: pd.Timestamp, end: pd.Timestamp, interval_hours: int
) -> list[pd.Timestamp]:
    """[start, end) aralığındaki funding anları; UTC gün başından itibaren adımlanır.

    Çıpa gün başıdır (00:00 UTC), barın açılışı değil: OKX'in periyotları takvime bağlıdır,
    bizim bar ızgaramıza değil.
    """
    if interval_hours <= 0:
        raise ValueError(f"funding.interval_hours pozitif olmalı: {interval_hours}")
    step = pd.Timedelta(hours=interval_hours)
    cursor = pd.Timestamp(start).normalize()
    while cursor < start:
        cursor += step
    stamps: list[pd.Timestamp] = []
    while cursor < end:
        stamps.append(cursor)
        cursor += step
    return stamps


def rate_at(series: pd.Series | None, ts: pd.Timestamp) -> float | None:
    """Verilen anın gerçek funding oranı; kayıt yoksa None (uydurma yok)."""
    if series is None or len(series) == 0:
        return None
    try:
        value = series.loc[ts]
    except KeyError:
        return None
    if isinstance(value, pd.Series):  # aynı ts iki kez geldiyse sonuncusu geçerlidir
        value = value.iloc[-1]
    if pd.isna(value):
        return None
    return float(value)


def funding_amount(*, direction: Direction, qty: float, price: float, rate: float) -> float:
    """İşaretli tutar: + alınan, - ödenen. Pozitif oranda long öder, short alır."""
    notional = qty * price
    return -notional * rate if direction == "long" else notional * rate


def accrue(
    positions: Iterable[FundingPosition],
    *,
    bar_open: pd.Timestamp,
    bar_close: pd.Timestamp,
    prices: Mapping[str, float],
    funding: Mapping[str, pd.Series],
    interval_hours: int,
    enabled: bool = True,
    model: str = "",
) -> list[FundingCharge]:
    """Bir barın kapsadığı funding anları için tahakkukları üretir.

    Notional, barın AÇILIŞ fiyatı üzerinden hesaplanır: elimizdeki tek "o an" fiyatı odur
    ve mum içi bir fiyat seçmek (high/low/close) look-ahead olurdu.

    Yalnızca funding anından ÖNCE açılmış pozisyonlar öder/alır. Tam o barın açılışında
    dolan pozisyon, borsanın anlık görüntüsünde henüz yoktur; "aynı anda" varsaymak,
    doldurma sırasına bağlı ve denetlenemez bir maliyet farkı üretirdi.
    """
    if not enabled:
        return []

    open_positions = [position for position in positions if position.qty > 0.0]
    if not open_positions:
        return []

    charges: list[FundingCharge] = []
    for ts in funding_times(start=bar_open, end=bar_close, interval_hours=interval_hours):
        for position in open_positions:
            if position.opened_at >= ts:
                continue
            price = prices.get(position.symbol)
            if price is None:
                logger.warning(
                    "%s%s funding atlandı: %s anında fiyat yok", _prefix(model), position.symbol, ts
                )
                continue
            rate = rate_at(funding.get(position.symbol), ts)
            if rate is None:
                logger.warning(
                    "%s%s funding atlandı: %s için OKX funding kaydı yok",
                    _prefix(model),
                    position.symbol,
                    ts,
                )
                continue
            charges.append(
                FundingCharge(
                    symbol=position.symbol,
                    direction=position.direction,
                    ts=ts,
                    rate=rate,
                    notional=position.qty * float(price),
                    amount=funding_amount(
                        direction=position.direction,
                        qty=position.qty,
                        price=float(price),
                        rate=rate,
                    ),
                )
            )
    return charges


def _prefix(model: str) -> str:
    return f"{model} " if model else ""


def settings(config: Mapping[str, Any]) -> tuple[bool, int]:
    """config'ten funding ayarlarını okur (enabled, interval_hours)."""
    config_dict = dict(config)
    return (
        bool(get_setting(config_dict, "funding.enabled")),
        int(get_setting(config_dict, "funding.interval_hours")),
    )
