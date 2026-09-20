"""strategies/ema_trend.py: EMA(21/55) kesişimi, long-only.

Modelin ölçüm değeri üç şeye bağlı ve üçü de burada sabitlenir:

1. **Sinyal bir OLAYDIR, bir DURUM değil.** "fast > slow" her barda doğru olabilir;
   model yalnızca KESİŞİM barında tetiklemelidir. Durum kuralına kayarsa model bir trend
   takipçisi olmaktan çıkar, "yukarı rejimde sürekli alım" olur ve ölçtüğü şey değişir.
2. **Yön kilidi.** Short kaynak sistemde sistematik kaybettiriyordu ve model long-only
   bildirildi (kural 8); aşağı kesişimde sinyal üretmemesi bir tercih değil, sözleşmedir.
3. **Geometri.** Stop 1.5×ATR (kural 14'ün 1–2.5 bandı, katmanın tavanının altında),
   hedef tam 2.0R ve tek dilim — ön-kayıt (docs/backtest.md > 6d) bu dört sayıyı
   sabitledi, yani bir regresyon sessiz bir parametre değişikliğidir.
"""

from __future__ import annotations

import logging

import pytest

from core.config import get_setting, load_config
from core.indicators import average_true_range, ema
from core.layers import resolve_layer
from core.validate import validate_signal
from strategies.ema_trend import EmaTrend
from tests.helpers_market import frame, market

SYMBOL = "BTC-USDT-SWAP"
LAYER = "ema"


def _config() -> dict:
    return resolve_layer(load_config(), LAYER).config


def _strategy() -> EmaTrend:
    return EmaTrend(config=_config())


def _signals(closes: list[float], *, symbol: str = SYMBOL, spread: float = 0.5):
    return _strategy().generate_signals(market({symbol: frame(closes, spread=spread)}))


# --------------------------------------------------------------------------- #
# Fiyat senaryoları
#
# EMA55 tohumlaması ilk 55 barın SMA'sıdır, bu yüzden her senaryo 200 barın üstünde
# geçmişle kurulur: kesişimin tohumlama artığından değil fiyattan gelmesi gerekir.
# --------------------------------------------------------------------------- #
def _cross_up() -> list[float]:
    """Uzun düşüş, sonra keskin toparlanma: fast son barda slow'u yukarı keser.

    Toparlanmanın uzunluğu (8 bar) keyfi değil, ÖLÇÜLDÜ: kesişim tam son barda olmalı ki
    "olay mı durum mu" testi bir şey ölçsün. Bir bar fazlası zaten `_already_crossed`.
    """
    closes = [200.0 - 0.5 * i for i in range(220)]  # 200 -> 90.5
    closes += [closes[-1] + 6.0 * (i + 1) for i in range(8)]
    return closes


def _already_crossed() -> list[float]:
    """Aynı toparlanma, bir bar DAHA uzun: kesişim önceki barda oldu, bu bar sinyal ÜRETMEZ."""
    closes = _cross_up()
    return closes + [closes[-1] + 6.0]


def _cross_down() -> list[float]:
    """Uzun yükseliş, sonra keskin düşüş: fast slow'u AŞAĞI keser (long-only: sinyal yok)."""
    closes = [90.0 + 0.5 * i for i in range(220)]
    closes += [closes[-1] - 6.0 * (i + 1) for i in range(8)]
    return closes


def _flat() -> list[float]:
    return [100.0] * 260


def _crossed(closes: list[float]) -> bool:
    """Senaryonun gerçekten yukarı kesişim taşıdığının BAĞIMSIZ doğrulaması.

    Modelin kendi kodunu değil `core/indicators.ema`yı çağırır: senaryo kurucusu sessizce
    bozulursa (ör. bar sayısı değişir) testler "sinyal yok" diye yeşil kalabilirdi.
    """
    series = frame(closes)["close"]
    fast, slow = ema(series, 21), ema(series, 55)
    fast_prev, slow_prev = ema(series.iloc[:-1], 21), ema(series.iloc[:-1], 55)
    assert None not in (fast, slow, fast_prev, slow_prev)
    return fast_prev <= slow_prev and fast > slow


# --------------------------------------------------------------------------- #
# Sözleşme
# --------------------------------------------------------------------------- #
def test_is_a_long_only_competitor() -> None:
    """Yarışmacı: kendi boyutunu belirlemez (kural 3/11), kopya da çıpa da değildir."""
    strategy = _strategy()
    assert strategy.name == "ema_trend"
    assert strategy.allowed_directions == ["long"]
    assert strategy.is_benchmark is False
    assert strategy.is_replica is False
    assert strategy.is_meta is False
    assert strategy.limits is None


def test_registry_resolves_the_model_name() -> None:
    from strategies.registry import build

    assert isinstance(build("ema_trend", config=_config()), EmaTrend)


def test_model_runs_in_the_ema_layer_only() -> None:
    """Katman `ema`nın listesinde VAR, base ve scalp listelerinde YOK.

    Base'e sızması, modelin backtest'te ölçüldüğünden başka bir evrende (hacme göre
    seçilen 50 sembol) koşması demekti — ön-kaydın ayrı katman gerekçesi tam olarak bu.
    """
    config = load_config()
    assert "ema_trend" in resolve_layer(config, "ema").models
    assert "ema_trend" not in resolve_layer(config, "base").models
    assert "ema_trend" not in resolve_layer(config, "scalp").models


def test_comparison_target_and_acceptance_rows_share_the_layer() -> None:
    """Kıyas hedefi, kontrol ve çıpa katmanın İÇİNDE (katmanlar arası kıyas yapılmaz)."""
    layer = resolve_layer(load_config(), "ema")
    assert {"trend", "random_ctrl", "buyhold"} <= set(layer.models)
    assert get_setting(load_config(), "acceptance.control_model") == "random_ctrl"


def test_model_does_not_learn_from_its_own_history() -> None:
    """`observe_closed_trades` UYGULANMAZ: model uyarlanabilir değildir (kural 16).

    Bu bir ayrıntı değil, backtest'in Kapı 0'ının dayanağı: uyarlanabilir olmayan modelden
    canlı kayıtla BİREBİR eşleşme beklenir (docs/backtest.md > 1).
    """
    from strategies.base import Strategy

    assert EmaTrend.observe_closed_trades is Strategy.observe_closed_trades


# --------------------------------------------------------------------------- #
# Sinyal: olay mı, durum mu
# --------------------------------------------------------------------------- #
def test_cross_up_opens_a_long() -> None:
    closes = _cross_up()
    assert _crossed(closes)
    signals = _signals(closes)
    assert len(signals) == 1
    assert signals[0].direction == "long"
    assert signals[0].symbol == SYMBOL


def test_bar_after_the_cross_produces_nothing() -> None:
    """Kesişim bir OLAYDIR: fast hâlâ slow'un üstünde ama sinyal yalnızca kesişim barında."""
    closes = _already_crossed()
    series = frame(closes)["close"]
    assert ema(series, 21) > ema(series, 55)  # durum sürüyor
    assert not _crossed(closes)  # olay geçti
    assert _signals(closes) == []


def test_cross_down_produces_nothing_because_the_model_is_long_only() -> None:
    closes = _cross_down()
    series = frame(closes)["close"]
    assert ema(series, 21) < ema(series, 55)
    assert _signals(closes) == []


def test_flat_market_produces_nothing() -> None:
    assert _signals(_flat()) == []


def test_symbol_without_the_as_of_bar_is_skipped() -> None:
    """`as_of` barını taşımayan sembolde sinyal üretmek, bir bar geriden işlem açmaktır."""
    candles = frame(_cross_up())
    stale = candles.iloc[:-1]
    snapshot = market({SYMBOL: candles, "ETH-USDT-SWAP": stale}, as_of=candles.index[-1])
    assert [signal.symbol for signal in _strategy().generate_signals(snapshot)] == [SYMBOL]


# --------------------------------------------------------------------------- #
# Geometri (ön-kayıtlı sayılar)
# --------------------------------------------------------------------------- #
def test_stop_sits_one_and_a_half_wilder_atr_below_the_close() -> None:
    """Yumuşatma WILDER'dır ve bu bir spec uyumudur (docs/backtest.md > 6d > TADİLAT-1).

    Test iki şeyi birden çiviler: doğru çarpan VE doğru yumuşatma. Yalnızca çarpanı
    ölçen bir test, yumuşatma sessizce `simple`a dönse yeşil kalırdı — oysa stop ve
    hedef mesafelerinin tamamı o değerden türüyor.
    """
    closes = _cross_up()
    candles = frame(closes)
    wilder = average_true_range(candles, 14, smoothing="wilder")
    simple = average_true_range(candles, 14)
    assert wilder is not None and simple is not None
    assert wilder != pytest.approx(simple), "senaryo iki yumuşatmayı ayırmıyor: test bir şey ölçmez"

    signal = _signals(closes)[0]
    assert signal.stop_price == pytest.approx(closes[-1] - 1.5 * wilder)
    assert signal.stop_price != pytest.approx(closes[-1] - 1.5 * simple)


def test_atr_smoothing_is_declared_in_config_not_hardcoded() -> None:
    """Bildirim config'te durur: sessiz bir varsayılan, spec farkını görünmez kılardı."""
    assert get_setting(_config(), "ema_trend.atr_smoothing") == "wilder"


def test_the_project_default_smoothing_is_unchanged() -> None:
    """KÜRESEL tanım DEĞİŞMEDİ: bu modelin bildirimi diğer modellere sızmaz.

    Küresel bir değişiklik, canlı koşan her modelin stop ölçeğini o commit'ten itibaren
    kaydırır ve biriken defteri ikiye bölerdi (docs/decisions.md > 25'in `fee_rate` hatası).
    """
    from strategies.trend import Trend

    candles = frame(_cross_up())
    assert average_true_range(candles, 14) == pytest.approx(
        average_true_range(candles, 14, smoothing="simple")
    )
    # Kıyas hedefi `trend` hâlâ düz ortalamayı kullanıyor: stop'u 2×simple ATR'dir.
    signals = Trend(config=_config()).generate_signals(
        market({SYMBOL: frame([200.0 - 0.5 * i for i in range(220)] +
                              [200.0 - 0.5 * 219 + 6.0 * (i + 1) for i in range(8)])})
    )
    simple = average_true_range(frame([200.0 - 0.5 * i for i in range(220)] +
                                      [200.0 - 0.5 * 219 + 6.0 * (i + 1) for i in range(8)]), 14)
    for signal in signals:
        if signal.direction == "long":
            assert abs(signal.stop_price - (
                float(200.0 - 0.5 * 219 + 6.0 * 8) - 2.0 * simple)) < 1e-6


def test_stop_stays_under_the_shared_atr_ceiling_despite_its_own_smoothing() -> None:
    """Kural 14'ün tavanı ORTAK tanımla ölçülür (core/engine.py), modelinkiyle değil.

    Tavanı modelin kendi yumuşatmasıyla ölçmek, her modele kendi tavanını genişletme
    imkânı verirdi — tavan o zaman bir kural olmaktan çıkardı. Bu yüzden model, ORTAK
    tanıma göre de tavanın altında kalmalıdır; kalmasaydı motor sinyali eler ve model
    ölçülemeyen bir nedenle işlem kaybederdi.
    """
    ceiling = float(get_setting(_config(), "max_stop_atr_multiple"))
    closes = _cross_up()
    simple = average_true_range(frame(closes), 14)
    assert simple is not None
    signal = _signals(closes)[0]
    assert (closes[-1] - signal.stop_price) / simple <= ceiling


def test_target_is_exactly_two_r_in_a_single_slice() -> None:
    """Hedef 2.0R ve TEK dilim: fraction < 1.0 olsaydı pozisyon iki ölçüm satırı üretirdi."""
    closes = _cross_up()
    signal = _signals(closes)[0]
    risk = closes[-1] - signal.stop_price  # R, modelin kendi ATR'sinden gelir
    assert len(signal.take_profits) == 1
    assert signal.take_profits[0].fraction == pytest.approx(1.0)
    assert signal.take_profits[0].price == pytest.approx(closes[-1] + 2.0 * risk)


def test_stop_distance_stays_inside_the_cost_comparability_band() -> None:
    """Kural 14: 1.5×ATR bandın (1–2.5) içinde — modelin KENDİ yumuşatmasıyla ölçülür.

    Band bir MODEL kuralıdır ("bu model hangi ölçekte işlem yapıyor"), tavan ise bir
    KATMAN kuralıdır; bu yüzden band modelin kendi tanımıyla, tavan ortak tanımla ölçülür.
    """
    closes = _cross_up()
    atr = average_true_range(frame(closes), 14, smoothing="wilder")
    assert atr is not None
    signal = _signals(closes)[0]
    multiple = (closes[-1] - signal.stop_price) / atr
    assert 1.0 <= multiple <= 2.5


def test_signal_uses_common_risk_sizing_and_no_exit_management() -> None:
    """Trailing ve üç aşamalı yönetim YOK: ikisi de kaynak sistemde yoktu (ön-kayıt)."""
    signal = _signals(_cross_up())[0]
    assert signal.sizing == "risk"
    assert signal.notional_fraction is None
    assert signal.entry_type == "market"
    assert signal.trailing_atr is None
    assert signal.breakeven_at_r is None
    assert signal.partial_tp is None
    assert signal.trail_giveback_pct is None


def test_signal_passes_the_validation_gate() -> None:
    layer = resolve_layer(load_config(), LAYER)
    signal = _signals(_cross_up())[0]
    validate_signal(
        signal,
        entry_price=_cross_up()[-1],
        allowed_directions=_strategy().allowed_directions,
        symbol_universe=list(layer.symbols),
    )


def test_reason_records_both_emas_and_the_stop_multiple() -> None:
    """Defterin `reason` kuyruğu denetim izidir: hangi kesişim, hangi mesafe."""
    signal = _signals(_cross_up())[0]
    assert "EMA21" in signal.reason and "EMA55" in signal.reason
    assert "1.5×ATR(14,wilder)" in signal.reason


# --------------------------------------------------------------------------- #
# Hesaplanamayan girdiler: atlama SESSİZ olamaz (kural 14'ün gerekçesi)
# --------------------------------------------------------------------------- #
def test_zero_atr_skips_the_setup_and_says_so(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """ATR 0: stop mesafesi üretilemez. Sinyal yok VE log var (kural 14: atlama sessiz olamaz).

    Guard doğrudan sınanır çünkü gerçek fiyatla ATR=0 ile KESİŞİM bir arada kurulamaz:
    ATR'yi sıfırlayan 15 düz bar, EMA'ları da birbirine yapıştırıp kesişimi öldürür.
    Senaryoyu zorlamak yerine göstergeyi değiştirmek, tam olarak bu dalı ölçer.
    """
    monkeypatch.setattr(
        "strategies.ema_trend.average_true_range",
        lambda frame, period, *, smoothing="simple": 0.0,
    )
    with caplog.at_level(logging.INFO, logger="strategies.ema_trend"):
        signals = _signals(_cross_up())

    assert signals == []
    assert any("ATR" in record.message for record in caplog.records)


def test_stop_below_zero_skips_the_setup_and_says_so(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """ATR fiyattan büyük: stop sıfırın altına düşer, geometri kurulamaz.

    Sinyali yine de üretmek, core/validate.py'nin PROGRAMLAMA hatası saydığı (kural 8) bir
    geometriyi motora sokmak olurdu — oysa bu bir piyasa durumudur ve atlanması gerekir.
    """
    closes = _cross_up()
    monkeypatch.setattr(
        "strategies.ema_trend.average_true_range",
        lambda frame, period, *, smoothing="simple": closes[-1],
    )
    with caplog.at_level(logging.INFO, logger="strategies.ema_trend"):
        signals = _signals(closes)

    assert signals == []
    assert any("stop sıfırın altında" in record.message for record in caplog.records)


def test_not_enough_history_produces_nothing() -> None:
    """EMA55 için yeterli bar yok: model sessizce boş döner (hata değil, veri durumu)."""
    assert _signals([100.0 + i for i in range(30)]) == []
