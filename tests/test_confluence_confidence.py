"""strategies/confluence_confidence.py: yalnızca ETİKET üreten kademe katmanı.

Bu katmanın ölçüm açısından tek sözü şudur: **hiçbir kararı etkilemez.** Testlerin ağırlığı
da oradadır — kademe tanımının kaynakla (crypto-scanner `evaluate_confluence_entry`) aynı
kalması ve etiketin sinyale sızmaması. Kaynağa karşı doğrulama `docs/decisions.md` karar
12'de kayıtlı (800 karşılaştırmada yapı ve kırılım/hacim teyidi birebir).
"""

from __future__ import annotations

import logging

import pandas as pd
import pytest

from core.indicators import rsi_series
from strategies import confluence_confidence as confidence
from strategies.confluence import Confluence
from tests.helpers_market import frame, market

SPREAD = 0.3
BREAKOUT_VOLUME = 1000.0
QUIET_VOLUME = 100.0


def _closes(start: float, segments: list[tuple[float, int]]) -> list[float]:
    closes = [start]
    for target, bars in segments:
        begin = closes[-1]
        closes.extend(begin + (target - begin) * (step + 1) / bars for step in range(bars))
    return closes


def _candles(segments: list[tuple[float, int]], *, spike_bars: int = 7) -> pd.DataFrame:
    closes = _closes(100.0, segments)
    volumes = [QUIET_VOLUME] * len(closes)
    for index in range(-spike_bars, 0):
        volumes[index] = BREAKOUT_VOLUME
    return frame(closes, spread=SPREAD, volumes=volumes)


# Tam teyitli çift dip: uzun düşüş (RSI ilk dipte aşırı satımda) -> toparlanma -> ikinci ve
# daha derin dip (RSI daha yüksek = pozitif diverjans) -> hacimli kırılım -> 0.618-0.786
# bandına geri çekilme. Üç kapı da (yapı+hacim, diverjans, fib bandı) aynı anda sağlanır.
CONFIRMED_PATH = [(70.0, 30), (78.0, 10), (69.0, 10), (84.0, 4), (78.5, 3)]
# Aynı yapı, kırılım barlarında hacim artışı YOK: yapı var ama teyit yok.
UNCONFIRMED_PATH = CONFIRMED_PATH


def _confirmed() -> pd.DataFrame:
    return _candles(CONFIRMED_PATH)


def _unconfirmed() -> pd.DataFrame:
    return _candles(UNCONFIRMED_PATH, spike_bars=0)


def _structureless() -> pd.DataFrame:
    return _candles([(160.0, 60)])  # tek yönlü yükseliş: iki dip yok


# --------------------------------------------------------------------------- #
# Kademe tanımı
# --------------------------------------------------------------------------- #
def test_all_three_gates_together_produce_the_high_tier() -> None:
    assessment = confidence.assess(_confirmed(), "long")
    assert assessment.tier == "high"
    assert assessment.structure_present is True
    assert assessment.fully_confirmed is True


def test_structure_without_volume_confirmation_stays_medium() -> None:
    """Kaynaktaki ayrım: yapı VAR ama tam teyit YOK — kademe medium."""
    assessment = confidence.assess(_unconfirmed(), "long")
    assert assessment.tier == "medium"
    assert assessment.structure_present is True
    assert assessment.fully_confirmed is False


def test_no_structure_means_the_low_tier() -> None:
    assessment = confidence.assess(_structureless(), "long")
    assert assessment.tier == "low"
    assert assessment.structure_present is False


def test_two_bottoms_closer_than_the_bar_gap_are_not_a_structure() -> None:
    """Kaynaktaki 8 bar eşiği: birbirine yakın iki dip aynı gürültü hareketinin parçası olabilir."""
    tight = _candles([(70.0, 30), (74.0, 3), (69.0, 3), (84.0, 4), (78.5, 3)])
    assert confidence._double_bottom(tight).found is False


def test_bottoms_further_apart_than_the_level_tolerance_are_not_a_structure() -> None:
    """%5 seviye toleransı: iki dip aynı seviyede değilse bu bir çift dip değil, düşen kanaldır."""
    skewed = _candles([(70.0, 30), (78.0, 10), (60.0, 10), (84.0, 4), (78.5, 3)])
    assert confidence._double_bottom(skewed).found is False


def test_divergence_requires_the_first_bottom_to_be_oversold() -> None:
    """RSI eşiği ilk dipte aranır: aşırı satım olmadan "diverjans" yalnızca bir dalgalanmadır."""
    candles = _confirmed()
    structure = confidence._double_bottom(candles)
    strength = rsi_series(candles["close"], 14)
    assert float(strength.iloc[structure.first_index]) < 35.0
    assert confidence._divergence(candles, structure, "long") is True


def test_the_short_side_is_the_mirror_image() -> None:
    """Çift tepe, çift dibin aynası: aynı eşikler, ters yön."""
    mirrored = _candles([(130.0, 30), (122.0, 10), (131.0, 10), (116.0, 4), (121.5, 3)])
    assert confidence._double_top(mirrored).found is True
    assert confidence.assess(mirrored, "short").structure_present is True


# --------------------------------------------------------------------------- #
# Etiketin sinyale sızmaması — bu katmanın asıl sözleşmesi
# --------------------------------------------------------------------------- #
def test_reason_ends_with_a_parseable_confidence_tag() -> None:
    """Etiket serbest cümleye gömülmez: defterden (trades.csv) gruplanabilmesi tek amacı."""
    from tests.test_confluence import _long_setup

    reason = Confluence().generate_signals(market({"BTC-USDT-SWAP": _long_setup()}))[0].reason
    assert " | confidence=" in reason
    tail = reason.rsplit(" | ", 1)[1]
    assert tail.startswith("confidence=")
    assert tail.split("=", 1)[1] in {"low", "medium", "high", "unknown"}


def test_a_broken_confidence_layer_never_changes_the_signal(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Süs amaçlı bir katmanın istisnası ölçümü düşüremez: sinyal aynı kalır, etiket unknown olur."""
    from tests.test_confluence import _long_setup

    candles = _long_setup()
    healthy = Confluence().generate_signals(market({"BTC-USDT-SWAP": candles}))[0]

    def explode(*args: object, **kwargs: object) -> confidence.Assessment:
        raise RuntimeError("kademe katmanı patladı")

    monkeypatch.setattr(confidence, "assess", explode)
    with caplog.at_level(logging.WARNING, logger="strategies.confluence"):
        broken = Confluence().generate_signals(market({"BTC-USDT-SWAP": candles}))[0]

    assert broken.direction == healthy.direction
    assert broken.stop_price == healthy.stop_price
    assert broken.sizing == healthy.sizing
    assert broken.take_profits == healthy.take_profits
    assert broken.reason.endswith("| confidence=unknown")
    assert healthy.reason.rsplit(" | ", 1)[0] == broken.reason.rsplit(" | ", 1)[0]
    assert any("confidence kademesi hesaplanamadı" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("tier", ["low", "medium", "high"])
def test_the_tier_does_not_move_the_stop_or_the_direction(tier: str) -> None:
    """Kaynakta kademe pozisyon boyutunu ÇARPIYORDU; burada hiçbir sayıya dokunamaz.

    Kademe zorla değiştirilir ve sinyalin ölçülen her alanının aynı kaldığı doğrulanır:
    "etiket kararı etkilemiyor" iddiası ancak böyle denetlenebilir.
    """
    from tests.test_confluence import _long_setup

    candles = _long_setup()
    signal = Confluence().generate_signals(market({"BTC-USDT-SWAP": candles}))[0]

    patched = Confluence()
    patched._confidence = lambda *args, **kwargs: tier  # type: ignore[method-assign]
    other = patched.generate_signals(market({"BTC-USDT-SWAP": candles}))[0]

    assert other.stop_price == signal.stop_price
    assert other.direction == signal.direction
    assert other.sizing == signal.sizing
    assert other.take_profits == signal.take_profits
    assert other.reason.endswith(f"| confidence={tier}")
    assert other.reason.rsplit(" | ", 1)[0] == signal.reason.rsplit(" | ", 1)[0]
