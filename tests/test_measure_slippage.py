"""scripts/measure_slippage.py: kitap matematiği ve emir boyunun defterden okunması.

Ağ gerektiren kısım (kitabı çekmek) test edilmez; test edilen şey **hesaptır** — bir
ölçüm aracının değeri, ölçtüğü sayının doğruluğu kadardır. Kitaplar elle kurulur ki
beklenen sayı elle de doğrulanabilsin.
"""

from __future__ import annotations

import math

import pytest

from scripts.measure_slippage import (
    Book,
    format_results,
    half_spread_pct,
    impact_pct,
    measure,
    round_trip_cost_pct,
    typical_notional,
)


def _book(*, bid: float, ask: float, size: float = 100.0, levels: int = 5) -> Book:
    """Her seviyede `size` ADET bulunan, 1 birim aralıklı basit bir kitap."""
    return Book(
        bids=tuple((bid - index, size) for index in range(levels)),
        asks=tuple((ask + index, size) for index in range(levels)),
    )


def test_half_spread_is_measured_against_the_mid() -> None:
    """Piyasa emri en iyi karşı fiyattan dolar: mid'e göre bedel spread'in YARISIDIR."""
    book = _book(bid=99.0, ask=101.0)
    assert book.mid == pytest.approx(100.0)
    assert half_spread_pct(book) == pytest.approx(1.0)  # (101−99)/2 / 100 = %1


def test_impact_includes_the_half_spread_at_the_touch() -> None:
    """İlk seviye zaten en iyi karşı fiyattır: küçük emrin impact'i = yarı spread."""
    book = _book(bid=99.0, ask=101.0, size=1000.0)
    # 1.000 USDT, ilk ask seviyesinin (101 × 1000) çok altında: tamamı 101'den dolar.
    assert impact_pct(book, notional=1_000.0, side="buy") == pytest.approx(1.0)


def test_impact_grows_as_the_order_eats_deeper_levels() -> None:
    """Derinlik tükendikçe dolum VWAP'i mid'den uzaklaşır — ölçülmek istenen tam da bu."""
    book = _book(bid=99.0, ask=101.0, size=10.0)      # seviye başına ~1.010 USDT
    shallow = impact_pct(book, notional=1_000.0, side="buy")
    deep = impact_pct(book, notional=4_000.0, side="buy")
    assert shallow is not None and deep is not None
    assert deep > shallow


def test_impact_is_none_when_the_book_cannot_fill_the_order() -> None:
    """Son seviyeden devam etmiş gibi hesaplamak, ölçülemeyeni ölçülmüş gibi gösterirdi."""
    book = _book(bid=99.0, ask=101.0, size=1.0, levels=3)   # toplam ~300 USDT
    assert impact_pct(book, notional=100_000.0, side="buy") is None


def test_round_trip_adds_both_sides_instead_of_averaging() -> None:
    """Kitap simetrik değildir; ortalama almak maliyeti sistematik olarak düşük gösterirdi."""
    book = Book(bids=((99.0, 1000.0),), asks=((102.0, 1000.0),))
    trip = round_trip_cost_pct(book, notional=1_000.0)
    buy = impact_pct(book, notional=1_000.0, side="buy")
    sell = impact_pct(book, notional=1_000.0, side="sell")
    assert trip == pytest.approx(buy + sell)


def test_typical_notional_uses_the_median_not_the_mean() -> None:
    """Dar stop'lu tek bir büyük pozisyon ortalamayı kendine çeker.

    Impact o zaman hiç açılmamış bir emir boyunda ölçülürdü.
    """
    trades = [
        {"symbol": "BTC-USDT-SWAP", "notional": "1000"},
        {"symbol": "BTC-USDT-SWAP", "notional": "1200"},
        {"symbol": "BTC-USDT-SWAP", "notional": "90000"},
    ]
    assert typical_notional(trades) == {"BTC-USDT-SWAP": 1200.0}


def test_typical_notional_ignores_unusable_rows() -> None:
    """Boş/bozuk notional satırı ölçümü düşürmez ama sayıya da girmez."""
    trades = [
        {"symbol": "X-USDT-SWAP", "notional": ""},
        {"symbol": "X-USDT-SWAP", "notional": "abc"},
        {"symbol": "X-USDT-SWAP", "notional": "500"},
        {"symbol": "", "notional": "700"},
    ]
    assert typical_notional(trades) == {"X-USDT-SWAP": 500.0}


def test_measure_reports_the_median_across_samples() -> None:
    """Tek anlık görüntü ölçümü tesadüfen o dakikaya bağlardı; medyan raporlanır."""
    books = [
        _book(bid=99.0, ask=101.0, size=1000.0),    # yarı spread %1
        _book(bid=99.5, ask=100.5, size=1000.0),    # %0.5
        _book(bid=98.0, ask=102.0, size=1000.0),    # %2
    ]
    calls = {"n": 0}

    def fetch(symbol: str) -> Book:
        book = books[calls["n"]]
        calls["n"] += 1
        return book

    results = measure(
        ["BTC-USDT-SWAP"], notionals={}, fetch=fetch,
        samples=3, interval=0.0, sleep=lambda _: None,
    )
    assert results[0].samples == 3
    assert results[0].half_spread_pct == pytest.approx(1.0)  # 0.5 / 1.0 / 2.0 -> medyan 1.0


def test_measure_counts_symbols_whose_book_is_too_thin() -> None:
    """Derinlik yetmediği örnek sayılır: sessizce atlamak eksik bir ölçümü tam gösterirdi."""
    def fetch(symbol: str) -> Book:
        return _book(bid=99.0, ask=101.0, size=0.1, levels=2)   # ~20 USDT

    results = measure(
        ["THIN-USDT-SWAP"], notionals={"THIN-USDT-SWAP": 50_000.0}, fetch=fetch,
        samples=2, interval=0.0, sleep=lambda _: None,
    )
    assert results[0].too_thin == 2
    assert results[0].round_trip_pct is None


def test_measure_survives_a_symbol_whose_book_fails() -> None:
    """Bir sembolün düşmesi ölçümü düşürmez; o satır `—` olur, sayı uydurulmaz."""
    def fetch(symbol: str) -> Book:
        raise RuntimeError("borsa yanıt vermedi")

    results = measure(
        ["X-USDT-SWAP"], notionals={}, fetch=fetch,
        samples=2, interval=0.0, sleep=lambda _: None,
    )
    assert results[0].samples == 0
    assert results[0].half_spread_pct is None


def test_report_puts_the_measurement_next_to_the_assumption() -> None:
    """Tek başına bir spread sayısı bir şey söylemez: kıyas varsayımladır."""
    results = measure(
        ["BTC-USDT-SWAP"],
        notionals={"BTC-USDT-SWAP": 1_000.0},
        fetch=lambda symbol: _book(bid=99.0, ask=101.0, size=1000.0),
        samples=1, interval=0.0, sleep=lambda _: None,
    )
    text = format_results(results, assumed_round_trip_pct=0.1, venue="bybit")

    assert "varsayım%" in text and "kat" in text
    assert "AYRI bir karardır" in text        # config değişikliği bu scriptin işi değil
    assert "20.00" in text                    # ölçülen %2.0 turMaliyet / varsayılan %0.1
