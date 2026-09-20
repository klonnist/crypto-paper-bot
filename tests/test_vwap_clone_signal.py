"""Model 13'ün sinyali: kaynak sistemin (`vwap_detector.py`) HER kuralı için iki senaryo.

Bu dosyanın sözleşmesi tek cümledir: **kaynağın her kuralı, sinyal ÜRETEN ve ÜRETMEYEN
birer senaryoyla çivilenir.** Yalnızca "üretiyor" tarafını test etmek, kuralın gerçekten
bir kapı olduğunu göstermez — kaldırılsa da testler yeşil kalırdı.

Birkaç test kasten `strategies/vwap/signal.py` (model 14) ile YAN YANA koşar. Amaç
karşılaştırma değil, ayrımın gerçekten geçtiğini göstermektir: aynı barda iki modülün
FARKLI cevap verdiği yerler, model 13 ↔ 14 ekseninin ("ev kurallarının katkısı") ölçtüğü
şeyin ta kendisidir. İki modül aynı cevabı verseydi o eksen boş olurdu.

**Beklenen sayılar bağımsız kurulur.** `_bands` kaynağın formülünü testin kendi
aritmetiğiyle yeniden kurar (`statistics.stdev` = ddof=1, hacim ağırlıksız); üretim
kodunun pandas çağrılarını tekrarlamaz. Aksi hâlde test, kodun kendisini değil kendi
kopyasını doğrulardı.

Fikstürün bir özelliği ölçümü kolaylaştırır ve bilerek kullanılır: `frame()` barın
yüksek/düşüğünü kapanışın `spread` kadar iki yanına koyar, yani typical price = kapanış.
Typical ile kapanışın AYRIŞTIĞI kurallar (dönüş şartı, geometri) için fikstür `highs`/
`lows` ile bilerek bozulur — o iki kural tam olarak bu ayrımda yaşar.
"""

from __future__ import annotations

from statistics import pstdev, stdev
from typing import Sequence

import pandas as pd
import pytest

from strategies.vwap import clone_signal
from strategies.vwap import signal as house_signal
from tests.helpers_market import frame, market

SYMBOL = "BTC-USDT-SWAP"
START = pd.Timestamp("2026-01-01 00:00:00", tz="UTC")
SPREAD = 0.1

# Kaynağın sabitleri (config.yaml > vwap.clone). Testte açıkça yazılır ki config'te bir
# değer değişirse test "hangi kuralı ölçüyordum" sorusunu hâlâ cevaplayabilsin.
WINDOW = 300
STD_WINDOW = 20
MIN_BARS = 25
BAND = 2.0
SL_MULT = 0.5


def params(
    *, band_mult: float = BAND, tp_mult: float = 1.0, sl_mult: float = SL_MULT
) -> clone_signal.CloneParams:
    return clone_signal.CloneParams(band_mult=band_mult, tp_mult=tp_mult, sl_mult=sl_mult)


def dropping(
    *, flat: int = 20, ramp: int = 8, step: float = 0.5, rebound: float = 0.25
) -> list[float]:
    """Düz bir geçmiş, kademeli düşüş ve SON barda bir toparlanma.

    Bant dışına çıkan bar SON BARDIR (kaynağın şartı budur); toparlanma yalnızca kapanış
    dönüşünü sağlar ve `rebound` büyüdükçe son barı banda geri sokar.
    """
    closes = [100.0] * flat + [100.0 - step * (index + 1) for index in range(ramp)]
    closes.append(closes[-1] + rebound)
    return closes


def rising(*, flat: int = 20, ramp: int = 8, step: float = 0.5, pullback: float = 0.25) -> list[float]:
    """`dropping`in aynası: yukarı sapma ve son barda geri çekilme (short kurulumu)."""
    closes = [100.0] * flat + [100.0 + step * (index + 1) for index in range(ramp)]
    closes.append(closes[-1] - pullback)
    return closes


def build(
    closes: Sequence[float],
    *,
    highs: Sequence[float] | None = None,
    lows: Sequence[float] | None = None,
    volumes: Sequence[float] | None = None,
    start: pd.Timestamp = START,
) -> pd.DataFrame:
    return frame(
        list(closes), spread=SPREAD, freq="15min", start=start,
        highs=highs, lows=lows, volumes=volumes,
    )


def detect(
    data: pd.DataFrame,
    *,
    combo: clone_signal.CloneParams | None = None,
    window: int = WINDOW,
    std_window: int = STD_WINDOW,
    min_bars: int = MIN_BARS,
) -> tuple[clone_signal.CloneCandidate | None, str]:
    return clone_signal.detect(
        data,
        symbol=SYMBOL,
        as_of=data.index[-1],
        params=combo if combo is not None else params(),
        vwap_window=window,
        std_window=std_window,
        min_bars=min_bars,
    )


def _bands(
    data: pd.DataFrame, *, window: int = WINDOW, std_window: int = STD_WINDOW
) -> tuple[float, float, float]:
    """Kaynağın formülünün BAĞIMSIZ karşılığı: (VWAP, σ, z).

    VWAP kümülatif ve HACİM AĞIRLIKLI, σ ise o sapma serisinin AĞIRLIKSIZ örneklem
    sapmasıdır (`statistics.stdev` = ddof=1). İkisinin ağırlıklandırması kaynakta
    farklıdır ve burada da bilerek farklıdır.
    """
    tail = data.tail(window)
    prices = [(h + l + c) / 3.0 for h, l, c in zip(tail["high"], tail["low"], tail["close"])]
    volumes = list(tail["volume"])
    vwaps: list[float] = []
    price_volume = volume = 0.0
    for price, size in zip(prices, volumes):
        price_volume += price * size
        volume += size
        vwaps.append(price_volume / volume)
    distance = [price - vwap for price, vwap in zip(prices, vwaps)]
    deviation = stdev(distance[-std_window:])
    return vwaps[-1], deviation, distance[-1] / deviation


def _house(data: pd.DataFrame) -> tuple[int, str]:
    """Model 14'ün aynı bardaki cevabı: (aday sayısı, baskın eleme sebebi)."""
    candidates, survey = house_signal.scan(
        market({SYMBOL: data}), atr_period=14, band_mult=BAND, min_vwap_bars=8
    )
    reason = max(survey.counts, key=lambda key: survey.counts[key])
    return len(candidates), reason


# --------------------------------------------------------------------------- #
# Kural 1: bant dışı olma şartı MEVCUT bara bakar (önceki bara değil)
# --------------------------------------------------------------------------- #
def test_a_current_bar_outside_the_band_that_turns_is_a_setup() -> None:
    candidate, reason = detect(build(dropping()))

    assert reason == clone_signal.SETUP
    assert candidate is not None
    assert candidate.direction == "long"
    assert candidate.z <= -BAND


def test_a_bar_that_came_back_inside_the_band_is_not_a_setup() -> None:
    """Kaynakta kapı SON barın z'sidir: banda geri dönmüş bar kurulum değildir.

    Aynı bar model 14 için kurulumdur (onun şartı "ÖNCEKİ bar bant dışında kapandı,
    bu bar VWAP'e doğru bir adım attı"dır). İki modülün burada ayrışması bir tutarsızlık
    değil, model 13 ↔ 14 ekseninin kendisidir.
    """
    data = build(dropping(rebound=3.0))

    candidate, reason = detect(data)

    assert candidate is None
    assert reason == clone_signal.INSIDE_BAND
    assert _house(data) == (1, house_signal.SETUP)


# --------------------------------------------------------------------------- #
# Kural 2: dönüş şartı YALNIZCA kapanıştır ("sapma daraldı" şartı yok)
# --------------------------------------------------------------------------- #
def test_the_smallest_closing_turn_is_enough() -> None:
    candidate, reason = detect(build(dropping(rebound=0.01)))

    assert reason == clone_signal.SETUP
    assert candidate is not None


def test_a_close_that_did_not_turn_is_not_a_setup() -> None:
    data = build(dropping(rebound=-0.5))

    candidate, reason = detect(data)

    assert candidate is None
    assert reason == clone_signal.NO_TURN


def test_the_turn_is_read_from_the_close_even_when_the_deviation_widens() -> None:
    """Son barın typical price'ı DÜŞERKEN kapanışı yükselirse kurulum yine de doğar.

    Kaynak dönüşü `last_close > prev_close` ile ölçer; sapmanın (typical − VWAP) daralıp
    daralmadığına bakmaz. Uzun alt fitilli bir bar tam olarak bu ayrımı üretir: typical
    aşağı, kapanış yukarı. Şart kapanış yerine sapmadan okunsaydı bu bar elenirdi.
    """
    closes = dropping(rebound=0.05)
    highs = [close + SPREAD for close in closes]
    lows = [close - SPREAD for close in closes]
    lows[-1] = closes[-1] - 3.0  # derin alt fitil: typical düşer, kapanış yükselir
    data = build(closes, highs=highs, lows=lows)

    typical = (data["high"] + data["low"] + data["close"]) / 3.0
    assert typical.iloc[-1] < typical.iloc[-2]      # sapma GENİŞLEDİ
    assert data["close"].iloc[-1] > data["close"].iloc[-2]  # ama kapanış döndü

    candidate, reason = detect(data)

    assert reason == clone_signal.SETUP
    assert candidate is not None


# --------------------------------------------------------------------------- #
# Kural 3: bant çarpanı gerçekten bir kapıdır
# --------------------------------------------------------------------------- #
def test_the_band_multiple_gates_the_same_bar() -> None:
    data = build(dropping())
    _, _, z = _bands(data)
    assert -2.5 < z <= -2.0  # fikstürün kapıyı iki çarpan arasına düşürdüğünün teyidi

    assert detect(data, combo=params(band_mult=2.0))[1] == clone_signal.SETUP
    assert detect(data, combo=params(band_mult=2.5))[1] == clone_signal.INSIDE_BAND


# --------------------------------------------------------------------------- #
# Kural 4: VWAP kümülatif ve pencereye bağlıdır, gün-çapalı DEĞİLDİR
# --------------------------------------------------------------------------- #
def test_the_vwap_window_is_capped_and_slides() -> None:
    """Çapa gün başı değil, son `vwap_window` bardır: pencere değişince VWAP de değişir."""
    closes = [100.0] * 350 + dropping(flat=0)
    data = build(closes)

    wide, _ = detect(data, window=300)
    narrow, _ = detect(data, window=40)

    assert wide is not None and narrow is not None
    assert wide.bars == 300      # 359 barlık geçmiş 300'e kırpıldı
    assert narrow.bars == 40
    assert wide.vwap != pytest.approx(narrow.vwap)
    assert wide.stop_price != pytest.approx(narrow.stop_price)


def test_there_is_no_day_boundary() -> None:
    """Gün başındaki ikinci barda bile kurulum doğar — kaynakta gün kavramı yoktur.

    Model 14 aynı barda `min_vwap_bars` (8) şartına takılır ve hiç aday üretmez: onun
    çapası UTC gün başıdır. Kopyaya o şartı taşımak, kaynağın her gün ilk iki saat kör
    kalmamasını sessizce değiştirmek olurdu.
    """
    data = build(dropping(), start=pd.Timestamp("2026-01-01 17:15:00", tz="UTC"))
    assert data.index[-1] == pd.Timestamp("2026-01-02 00:15:00", tz="UTC")

    candidate, reason = detect(data)

    assert reason == clone_signal.SETUP
    assert candidate is not None
    assert candidate.bars == 29  # gün sınırı pencereyi kesmedi
    assert _house(data) == (0, house_signal.NO_VWAP)


# --------------------------------------------------------------------------- #
# Kural 5: VWAP hacim AĞIRLIKLI, σ hacim AĞIRLIKSIZ (ddof=1)
# --------------------------------------------------------------------------- #
def test_the_vwap_is_volume_weighted() -> None:
    closes = dropping()
    flat = build(closes)
    skewed = build(closes, volumes=[1.0] * 20 + [5.0] * (len(closes) - 20))

    assert detect(flat)[0].vwap != pytest.approx(detect(skewed)[0].vwap)


def test_the_deviation_is_an_unweighted_sample_std() -> None:
    """σ, sapma serisinin AĞIRLIKSIZ örneklem sapmasıdır: ddof=1, hacimden bağımsız.

    Hacmi çarpık bir barda ağırlıklı (popülasyon) sapma belirgin biçimde farklı çıkar;
    testin iki ayrı `assert`i o yüzden birlikte durur — biri tanımı çiviler, diğeri
    yanlış tanımın gerçekten farklı bir sayı ürettiğini gösterir.
    """
    data = build(dropping(), volumes=[1.0] * 20 + [5.0] * 9)
    candidate, _ = detect(data)
    _, deviation, z = _bands(data)

    assert candidate is not None
    assert candidate.std == pytest.approx(deviation)
    assert candidate.z == pytest.approx(z)

    tail = data.tail(WINDOW)
    prices = [(h + l + c) / 3.0 for h, l, c in zip(tail["high"], tail["low"], tail["close"])]
    volumes = list(tail["volume"])
    running = [
        sum(p * v for p, v in zip(prices[: i + 1], volumes[: i + 1]))
        / sum(volumes[: i + 1])
        for i in range(len(prices))
    ]
    distance = [p - w for p, w in zip(prices, running)][-STD_WINDOW:]
    assert candidate.std != pytest.approx(pstdev(distance))  # ddof=0 DEĞİL


# --------------------------------------------------------------------------- #
# Kural 6: minimum bar şartı (gün sınırı değil, BAR SAYISI)
# --------------------------------------------------------------------------- #
def test_a_frame_below_the_minimum_bar_count_is_not_measured() -> None:
    data = build(dropping(ramp=3))
    assert len(data) == MIN_BARS - 1

    candidate, reason = detect(data)

    assert candidate is None
    assert reason == clone_signal.NO_BANDS


def test_exactly_the_minimum_bar_count_is_enough() -> None:
    data = build(dropping(ramp=4))
    assert len(data) == MIN_BARS

    assert detect(data)[1] == clone_signal.SETUP


def test_a_flat_series_has_no_deviation_to_measure() -> None:
    """σ = 0 olan pencerede "kaç σ uzakta" sorusunun cevabı yoktur; sıfıra bölünmez."""
    candidate, reason = detect(build([100.0] * 40))

    assert candidate is None
    assert reason == clone_signal.NO_BANDS


def test_a_symbol_without_the_current_bar_is_counted_separately() -> None:
    data = build(dropping())
    stale = data.iloc[:-1]

    candidate, reason = clone_signal.detect(
        stale,
        symbol=SYMBOL,
        as_of=data.index[-1],
        params=params(),
        vwap_window=WINDOW,
        std_window=STD_WINDOW,
        min_bars=MIN_BARS,
    )

    assert candidate is None
    assert reason == clone_signal.NO_BAR


# --------------------------------------------------------------------------- #
# Kural 7: stop = band_mult × sl_mult × σ (ATR YOK)
# --------------------------------------------------------------------------- #
def test_the_stop_is_a_sigma_multiple_not_an_atr_multiple() -> None:
    data = build(dropping())
    _, deviation, _ = _bands(data)

    candidate, _ = detect(data, combo=params(band_mult=2.0, sl_mult=0.5))

    assert candidate is not None
    assert candidate.stop_price == pytest.approx(
        candidate.entry_price - deviation * 2.0 * 0.5
    )


def test_the_band_multiple_also_scales_the_stop() -> None:
    """Kaynakta `band_mult` hem giriş eşiği hem stop mesafesidir; ikisi ayrılmaz."""
    data = build(dropping(ramp=14, step=0.8))

    narrow, _ = detect(data, combo=params(band_mult=1.5))
    wide, _ = detect(data, combo=params(band_mult=2.5))

    assert narrow is not None and wide is not None
    narrow_distance = narrow.entry_price - narrow.stop_price
    wide_distance = wide.entry_price - wide.stop_price
    assert wide_distance == pytest.approx(narrow_distance * (2.5 / 1.5))


# --------------------------------------------------------------------------- #
# Kural 8: hedef = VWAP'e olan mesafenin kesri; VWAP'i ASLA aşmaz
# --------------------------------------------------------------------------- #
def test_a_full_target_multiple_lands_exactly_on_the_vwap() -> None:
    candidate, _ = detect(build(dropping()), combo=params(tp_mult=1.0))

    assert candidate is not None
    assert candidate.target_price == pytest.approx(candidate.vwap)


def test_a_partial_target_multiple_stops_short_of_the_vwap() -> None:
    data = build(dropping())

    half, _ = detect(data, combo=params(tp_mult=0.5))
    full, _ = detect(data, combo=params(tp_mult=1.0))

    assert half is not None and full is not None
    assert half.target_price < full.target_price == pytest.approx(half.vwap)
    assert half.target_price == pytest.approx(
        half.entry_price + (half.vwap - half.entry_price) * 0.5
    )


def test_the_reward_risk_is_emergent_not_imposed() -> None:
    """Kaynakta bir hedef/stop çıtası YOKTUR: R:R iki kuralın sonucudur.

    Aynı barda tp_mult küçüldükçe R:R düşer ve 1.5'in altına inebilir — ev kuralı olsaydı
    (model 14'ün `min_reward_risk`i) o kurulum hiç üretilmezdi.
    """
    data = build(dropping())

    ratios = []
    for tp_mult in (0.5, 0.75, 1.0):
        candidate, _ = detect(data, combo=params(tp_mult=tp_mult))
        assert candidate is not None
        ratios.append(
            abs(candidate.target_price - candidate.entry_price)
            / abs(candidate.entry_price - candidate.stop_price)
        )

    assert ratios == sorted(ratios)
    assert min(ratios) < 1.5  # ev kapısı burada olsaydı bu kurulum elenirdi


# --------------------------------------------------------------------------- #
# Kural 9: "VWAP geçilmiş" ayrı bir eleme değil, GEOMETRİ kontrolünün sonucudur
# --------------------------------------------------------------------------- #
def test_a_target_on_the_wrong_side_of_the_entry_fails_the_geometry_check() -> None:
    """Typical bant altında ama kapanış VWAP'in ÜSTÜNDE: hedef girişin gerisinde kalır.

    Kaynakta `max(vwap − entry, 0)` bu mesafeyi sıfıra kırpar, hedef girişe eşitlenir ve
    `sl < entry < tp` düşer. Ayrı bir "VWAP geçildi" kuralı yoktur — eleme buradan gelir.
    """
    closes = dropping()
    closes[-1] = 100.6  # kapanış VWAP'in üstünde
    highs = [close + SPREAD for close in closes]
    lows = [close - SPREAD for close in closes]
    lows[-1] = 88.0     # typical'ı bandın altında tutan derin fitil
    data = build(closes, highs=highs, lows=lows)

    candidate, reason = detect(data)

    assert candidate is None
    assert reason == clone_signal.BAD_GEOMETRY


def test_a_setup_whose_geometry_holds_is_produced() -> None:
    candidate, reason = detect(build(dropping()))

    assert reason == clone_signal.SETUP
    assert candidate is not None
    assert candidate.stop_price < candidate.entry_price < candidate.target_price


# --------------------------------------------------------------------------- #
# Kural 10: short tarafı simetriktir
# --------------------------------------------------------------------------- #
def test_short_setups_are_symmetric() -> None:
    candidate, reason = detect(build(rising()))

    assert reason == clone_signal.SETUP
    assert candidate is not None
    assert candidate.direction == "short"
    assert candidate.z >= BAND
    assert candidate.target_price < candidate.entry_price < candidate.stop_price


def test_a_short_that_keeps_extending_is_not_a_setup() -> None:
    candidate, reason = detect(build(rising(pullback=-0.5)))

    assert candidate is None
    assert reason == clone_signal.NO_TURN


# --------------------------------------------------------------------------- #
# Sayım (denetim izi)
# --------------------------------------------------------------------------- #
def test_every_examined_symbol_lands_in_exactly_one_reason() -> None:
    counts = clone_signal.empty_counts()
    for closes in (dropping(), dropping(rebound=-0.5), dropping(rebound=3.0), [100.0] * 40):
        counts[detect(build(closes))[1]] += 1

    survey = clone_signal.Survey(counts=counts)

    assert survey.examined == 4
    assert survey.candidates == 1
    assert counts[clone_signal.NO_TURN] == 1
    assert counts[clone_signal.INSIDE_BAND] == 1
    assert counts[clone_signal.NO_BANDS] == 1


def test_the_survey_describes_only_the_reasons_it_saw() -> None:
    counts = clone_signal.empty_counts()
    counts[clone_signal.SETUP] = 2

    text = clone_signal.Survey(counts=counts).describe()

    assert "2 sembol" in text
    assert clone_signal.SETUP in text
    assert clone_signal.NO_TURN not in text


def test_the_arm_label_is_not_the_house_arm() -> None:
    """Aynı etiket iki farklı kural kümesini tek ad altında gösterirdi (kol kırılımı)."""
    assert clone_signal.ARM_NAME != house_signal.ARM_NAME
