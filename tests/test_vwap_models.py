"""Modeller 13 (`vwap_clone`) ve 14 (`vwap_managed`): AYRI sinyaller, ayrı kurallar.

İkisi bir zamanlar tek bir sinyal modülünü paylaşırdı; artık paylaşmıyor. Model 13 dış
bir sistemin SADIK kopyasıdır (`strategies/vwap/clone_signal.py`) ve o sistemin sinyal
kuralları evinkilerden ayrışır; model 14 ev kurallarıyla koşar
(`strategies/vwap/signal.py`). 13 ↔ 14 ekseni bu yüzden "ev kurallarının katkısı"nı
ölçer ve o katkı yalnızca boyutlandırma/kapı farkı değil, SİNYAL farkını da içerir.

Buradaki testler MODEL düzeyindedir: boyutlandırma, limitler, etiketler, öğrenme,
evren, tarama sırası. Kaynağın sinyal kurallarının tek tek çivilendiği yer
`tests/test_vwap_clone_signal.py`'dir — orada her kural için sinyal üreten ve üretmeyen
birer senaryo vardır.
"""

from __future__ import annotations

import random
from typing import Any

import pandas as pd
import pytest

from core.config import load_config
from core.layers import resolve_layer
from core.tags import find_tag, parse_tag
from core.validate import validate_signal
from strategies.base import ClosedTrade, MarketData
from strategies.vwap import clone_signal
from strategies.vwap import signal as vwap_signal
from strategies.vwap_clone import VwapClone
from strategies.vwap_managed import VwapManaged
from tests.helpers_market import frame, market

SYMBOL = "BTC-USDT-SWAP"
OTHER = "ETH-USDT-SWAP"
START = pd.Timestamp("2026-01-01 00:00:00", tz="UTC")


@pytest.fixture()
def config() -> dict[str, Any]:
    return resolve_layer(load_config(), "scalp").config


# --------------------------------------------------------------------------- #
# Sentetik kurulum: gün içinde aşağı sapma, sonra dönüş
# --------------------------------------------------------------------------- #
# Bar aralığı kasten DAR (spread=0.1): ATR barların kendi genişliğinden beslenir, VWAP
# sapması ise günün fiyat YAYILIMINDAN. İkisini ayırmak, model 14'ün 1.5R kapısının
# gerçekten canlı olduğu (hedefin VWAP tarafından kırpıldığı) bir kurulum üretmenin tek
# yoludur — geniş barlarda stop mesafesi VWAP'e olan mesafeyi yutar ve her kurulum elenir.
SPREAD = 0.1


def _reverting(
    *,
    step: float = 0.5,
    spike: float = 0.0,
    flat_bars: int = 20,
    ramp_bars: int = 14,
    rebound: float = 0.25,
) -> list[float]:
    """Düz bir gün, ardından kademeli düşüş ve SON barda küçük bir toparlanma.

    Bant dışına çıkan bar SON BAR DEĞİL bir öncekidir: kolun şartı "önceki bar bandın
    dışında kapandı, bu bar VWAP'e doğru bir adım attı"dır.

    `spike` son iniş barını derinleştirir, yani kurulumun GÜCÜNÜ (|z_prev|) artırır —
    stop mesafesini neredeyse hiç değiştirmeden. Güç sırasını ölçen test bu kolu kullanır.
    """
    closes = [100.0] * flat_bars + [100.0 - step * (i + 1) for i in range(ramp_bars)]
    closes[-1] -= spike
    closes.append(closes[-1] + rebound)
    return closes


def _market(closes: list[float] | None = None, *, symbol: str = SYMBOL) -> MarketData:
    values = _reverting() if closes is None else closes
    frames = {symbol: frame(values, spread=SPREAD, freq="15min", start=START)}
    return market(frames)


def _candidates(data: MarketData, *, band_mult: float = 2.0) -> list[Any]:
    return vwap_signal.propose(
        data, atr_period=14, band_mult=band_mult, min_vwap_bars=8
    )


# --------------------------------------------------------------------------- #
# Ortak sinyal
# --------------------------------------------------------------------------- #
def test_a_reverting_bar_below_the_band_produces_a_long() -> None:
    candidates = _candidates(_market())

    assert [item.symbol for item in candidates] == [SYMBOL]
    assert candidates[0].direction == "long"
    assert candidates[0].z_prev <= -2.0
    assert candidates[0].z_prev < candidates[0].z_now < 0.0


def test_a_bar_that_is_still_extending_is_not_a_setup() -> None:
    """Bant dışında olmak bir sinyal değildir: dönüş BAŞLAMIŞ olmalı.

    Şart olmasaydı güçlü bir trendde kol her barda aynı sinyali üretirdi.
    """
    closes = _reverting()
    closes[-1] = closes[-2] - 1.0  # dönüş yok, düşüş sürüyor

    assert _candidates(_market(closes)) == []


def test_a_bar_that_crossed_the_vwap_is_not_a_setup() -> None:
    """VWAP'i geçmiş bar bir dönüş başlangıcı değil, başka bir kurulumdur.

    Toparlanma ÖLÇÜLÜ olmalı: son bar aynı zamanda gün-çapalı VWAP'in kendi penceresindedir
    ve uç bir değer (ör. 120) VWAP ile sapmayı öyle kaydırır ki ÖNCEKİ bar da bandın içine
    düşer — eleme o zaman "VWAP geçildi"den değil "bant içi"nden gelir ve test adının
    söylediği dalı hiç çalıştırmaz. Sayım (`Survey`) bu ayrımı görünür kılar.
    """
    closes = _reverting(rebound=6.0)

    _, survey = vwap_signal.scan(
        _market(closes), atr_period=14, band_mult=2.0, min_vwap_bars=8
    )

    assert _candidates(_market(closes)) == []
    assert survey.counts[vwap_signal.CROSSED] == 1


def test_the_band_multiple_actually_gates() -> None:
    """Bant çarpanı bir parametre değil, kolun tezinin kendisidir: yükseltmek eler."""
    data = _market()

    assert _candidates(data, band_mult=2.0)
    assert _candidates(data, band_mult=50.0) == []


def test_the_two_models_do_not_share_a_signal_module(config: dict[str, Any]) -> None:
    """Kopya kendi modülünü okur; ev modeli evinkini. Paylaşım bir REGRESYON olurdu.

    Aynı barda ikisi de sinyal üretebilir (fikstür bilerek öyle kurulmuştur) ama bunu
    AYRI kurallarla yapar: kopyanın kolu bile ayrı bir etiket taşır. Modüller yeniden
    tek kopyaya indirilirse bu test düşer — ve düşmesi gerekir, çünkü o gün model 13
    artık bir kopya olmaz.
    """
    data = _market()
    clone = VwapClone(config=config).generate_signals(data)
    managed = VwapManaged(config=config).generate_signals(data)

    assert clone and managed
    assert parse_tag(clone[0].reason, "arm") == clone_signal.ARM_NAME
    assert parse_tag(managed[0].reason, "arm") == vwap_signal.ARM_NAME
    assert clone_signal.ARM_NAME != vwap_signal.ARM_NAME
    # Stop ölçekleri de ayrı: kopya σ'dan, ev modeli ATR'den türetir.
    assert clone[0].stop_price != pytest.approx(managed[0].stop_price)


def test_candidates_are_ordered_by_strength() -> None:
    """Sıra tekrarlanabilirliğin parçası: iki model de en güçlü adayı seçer."""
    weak = _reverting(spike=0.0)
    strong = _reverting(spike=3.0)
    data = market(
        {
            SYMBOL: frame(weak, spread=SPREAD, freq="15min", start=START),
            OTHER: frame(strong, spread=SPREAD, freq="15min", start=START),
        }
    )

    candidates = _candidates(data)

    assert [item.symbol for item in candidates] == [OTHER, SYMBOL]
    assert candidates[0].extension > candidates[1].extension


# --------------------------------------------------------------------------- #
# Model 13 — kopya
# --------------------------------------------------------------------------- #
def test_clone_is_a_replica_with_its_own_leverage(config: dict[str, Any]) -> None:
    model = VwapClone(config=config)

    assert model.is_replica is True
    assert model.is_benchmark is False
    assert model.limits is not None
    assert model.limits.leverage == pytest.approx(10.0)
    assert model.limits.max_positions == 5
    assert model.limits.max_per_direction == 3
    assert model.limits.max_portfolio_risk == pytest.approx(0.08)


def test_clone_signals_use_fixed_margin_sizing_and_keep_their_stop(
    config: dict[str, Any]
) -> None:
    signals = VwapClone(config=config).generate_signals(_market())

    assert signals
    signal = signals[0]
    assert signal.sizing == "notional_fraction"
    assert signal.notional_fraction == pytest.approx(0.5)
    # Çıpanın aksine kopyanın stop'u VARDIR: stop yönetimi kopyalanan sistemin parçasıdır.
    assert signal.stop_price is not None and signal.stop_price < signal.take_profits[0].price
    validate_signal(
        signal,
        entry_price=signal.take_profits[0].price - 1.0,
        allowed_directions=["long", "short"],
        symbol_universe=[SYMBOL],
        is_replica=True,
    )


def test_clone_carries_the_three_stage_management(config: dict[str, Any]) -> None:
    signal = VwapClone(config=config).generate_signals(_market())[0]

    assert signal.breakeven_at_r == pytest.approx(1.0)
    assert signal.partial_tp is not None
    assert signal.partial_tp.r == pytest.approx(1.5)
    assert signal.partial_tp.fraction == pytest.approx(0.5)
    assert signal.trail_giveback_pct == pytest.approx(0.5)
    assert signal.trailing_atr is None  # iki mekanizma aynı anda kullanılamaz


def test_clone_ignores_the_house_gates(config: dict[str, Any]) -> None:
    """%1 stop tabanı ve 1.5R kapısı kaynak sistemde yok; kopyaya da geçmez."""
    tight = dict(config)
    tight["scalp"] = {**config["scalp"], "min_stop_pct": 0.5, "min_reward_risk": 99.0}

    assert VwapClone(config=tight).generate_signals(_market())


def test_clone_stays_inside_its_own_universe(config: dict[str, Any]) -> None:
    """Katmanın evreni geniş olsa da kopya yalnızca kendi 12 sembolünü oynar.

    İki sembol bilerek dışarıdadır ve gerekçeleri farklıdır: SUI kaynak sistemde hiç
    yoktur (fazladan bir sembolde işlem açmak kopyayı kopya olmaktan çıkarırdı), TON ise
    OKX'te kalıcı olarak yoktur (51001) ve listede tutmak hiç taranmayan bir sembolü
    taranıyormuş gibi gösterirdi.
    """
    model = VwapClone(config=config)
    assert len(model._universe) == 12
    assert "SUI-USDT-SWAP" not in model._universe
    assert "TON-USDT-SWAP" not in model._universe

    for outsider in ("SUI-USDT-SWAP", "TON-USDT-SWAP"):
        assert model.generate_signals(_market(symbol=outsider)) == []


def _multi_symbol_market(config: dict[str, Any], *, count: int = 7) -> tuple[MarketData, list[str]]:
    """Kopyanın evreninin ilk `count` sembolünde AYNI kurulum; güçleri bilerek farklı.

    `spike` sembol sırasına göre büyür: evren sırasında SONRAKİ semboller daha güçlü
    saparlar. Sıra ölçütünü güçten ayırt edebilmenin tek yolu budur — ikisi aynı yöne
    işaret etseydi test hangi kuralın geçerli olduğunu söyleyemezdi.
    """
    universe = [str(s) for s in config["vwap"]["clone"]["universe"]][:count]
    data = market(
        {
            symbol: frame(_reverting(spike=0.5 * index), spread=SPREAD,
                          freq="15min", start=START)
            for index, symbol in enumerate(universe)
        }
    )
    return data, universe


def test_clone_scans_in_universe_order_not_by_strength(config: dict[str, Any]) -> None:
    """Kaynak `POPULAR_COINS`i baştan sona gezer; güce göre sıralama YOKTUR.

    Model 14 en güçlü adayı seçer (`vwap_signal.strongest`); kopya seçmez. Fikstürde en
    güçlü sapma evren sırasının SONUNDADIR, yani iki kural farklı cevaplar verir.
    """
    data, universe = _multi_symbol_market(config)

    signals = VwapClone(config=config).generate_signals(data)

    assert [signal.symbol for signal in signals] == universe[:5]

    strongest = vwap_signal.propose(data, atr_period=14, band_mult=2.0, min_vwap_bars=8)
    assert strongest[0].symbol == universe[-1]  # güç sırası TERS yönde


def test_clone_never_exceeds_its_own_position_quota(config: dict[str, Any]) -> None:
    """Kotanın ötesindeki emir zaten reddedilirdi; tur raporunu sahte retle doldurmaz.

    Kırpma listenin SONUNDAN yapılır: kaynak kota dolunca kalan sembollere hiç bakmaz.
    """
    data, universe = _multi_symbol_market(config)

    signals = VwapClone(config=config).generate_signals(data)

    assert len(signals) == 5 < len(universe)


def test_clone_tags_the_arm_and_the_combo(config: dict[str, Any]) -> None:
    """Kol etiketi kırılım için, combo etiketi öğrenme için — ikisi de defterden okunur."""
    signal = VwapClone(config=config).generate_signals(_market())[0]

    assert parse_tag(signal.reason, "arm") == clone_signal.ARM_NAME
    assert parse_tag(signal.reason, "combo").startswith("band")
    assert find_tag(signal.reason, "pick") in {"unexplored", "explore", "exploit"}


def test_clone_reports_its_survey(config: dict[str, Any]) -> None:
    """Sayım tur raporuna düşer (kural 15); sinyalleri etkilemez."""
    model = VwapClone(config=config)
    assert model.take_survey() is None  # tarama yapılmadan sayım da yok

    data, universe = _multi_symbol_market(config)
    signals = model.generate_signals(data)
    survey = model.take_survey()

    assert survey is not None
    # Evrenin TAMAMI incelenir; verisi olmayan semboller de sebebiyle sayılır.
    assert sum(survey.values()) == len(model._universe)
    assert survey[clone_signal.SETUP] == len(universe)
    assert survey[clone_signal.NO_BAR] == len(model._universe) - len(universe)
    # Kota kırpması sayımı DEĞİŞTİRMEZ: sayım taramanın izidir, dolumun değil.
    assert len(signals) == 5 < survey[clone_signal.SETUP]


# --------------------------------------------------------------------------- #
# Model 13 — epsilon-greedy öğrenme
# --------------------------------------------------------------------------- #
def _closed(combo: str, r: float, *, symbol: str = SYMBOL, hour: int = 0) -> ClosedTrade:
    return ClosedTrade(
        symbol=symbol,
        direction="long",
        opened_at=START,
        closed_at=START + pd.Timedelta(hours=hour),
        r_multiple=r,
        signal_reason=f"test | arm={clone_signal.ARM_NAME} | combo={combo}",
        exit_reason="tp",
    )


class _Exploit(random.Random):
    """Keşif dalını kapatan RNG: seçim tamamen istatistiğe kalsın.

    `choice` de geçersiz kılınır: yalnızca `random`ı geçersiz kılan bir alt sınıfta
    `random.Random.choice`, `_randbelow_without_getrandbits` üzerinden sonsuz döngüye
    girer (sabit 1.0 hiçbir zaman eşiğin altına inmez). Dönen değer testlerde
    kullanılmaz; önemli olan çağrının SONLANMASIDIR.
    """

    def random(self) -> float:
        return 1.0

    def choice(self, seq: Any) -> Any:
        return seq[0]


def _warmed(model: VwapClone, *, symbol: str = SYMBOL, hour: int = 0) -> list[ClosedTrade]:
    """Her kombinasyonun o sembolde EN AZ bir örneği: "denenmemiş öncelik" kapansın.

    Isınma olmadan `choose_combo` birinci adımda takılır ve sömürü dalı hiç
    çalışmazdı — kaynak sistemin davranışı da tam olarak budur.
    """
    return [
        _closed(combo.key, 0.0, symbol=symbol, hour=hour + index)
        for index, combo in enumerate(model._combos)
    ]


def test_clone_tries_every_untested_combination_before_exploiting(
    config: dict[str, Any]
) -> None:
    """Kaynağın ısınması: o sembolde hiç denenmemiş kol varsa önce o çekilir.

    Kontrol SEMBOLÜN KENDİ sayımına bakar, genele düşmüş hücreye değil: başka bir
    sembolde ölçülmüş bir kolu burada "denenmiş" saymak, ısınmayı ilk sembolden sonra
    tamamen atlamak olurdu.
    """
    model = VwapClone(config=config)
    best = model._combos[0].key
    untested = model._combos[-1].key
    model.observe_closed_trades(
        [_closed(best, 9.0, hour=index) for index in range(5)]
        + [_closed(combo.key, 0.0, symbol=OTHER, hour=50 + index)
           for index, combo in enumerate(model._combos)]
    )

    combo, _, pick = model.choose_combo(SYMBOL, rng=_Exploit())

    assert pick == "unexplored"
    assert combo.key != best
    assert combo.key == untested or not model._seen(SYMBOL, combo)


def test_clone_exploits_once_every_combination_has_been_tried(
    config: dict[str, Any]
) -> None:
    model = VwapClone(config=config)
    good, bad = model._combos[4].key, model._combos[0].key
    model.observe_closed_trades(
        _warmed(model)
        + [_closed(good, 2.0, hour=100 + i) for i in range(5)]
        + [_closed(bad, -1.0, hour=200 + i) for i in range(5)]
    )

    combo, stats, pick = model.choose_combo(SYMBOL, rng=_Exploit())

    assert pick == "exploit"
    assert combo.key == good
    assert stats.mean_r > 0.0


def test_clone_falls_back_to_the_global_average_below_the_sample_floor(
    config: dict[str, Any]
) -> None:
    """Az örnekli sembolde sembolün kendi gürültüsü, genel ortalamadan daha kötü bir tahmin."""
    model = VwapClone(config=config)
    good = "band1_tp2"
    model.observe_closed_trades(
        # OTHER'da bolca veri: bu kombinasyon genelde iyi.
        [_closed(good, 3.0, symbol=OTHER, hour=i) for i in range(5)]
        # SYMBOL'de yalnızca 1 örnek (eşik 3) ve kötü: sembol verisi yeterli değil.
        + [_closed(good, -2.0, symbol=SYMBOL, hour=20)]
    )

    stats = model._stats_for_choice(SYMBOL)

    assert stats[good].trades == 6  # genele düşüldü (5 + 1)
    assert stats[good].mean_r > 0.0


def test_clone_prefers_the_symbols_own_statistics_once_there_is_enough_data(
    config: dict[str, Any]
) -> None:
    model = VwapClone(config=config)
    combo = "band1_tp2"
    model.observe_closed_trades(
        [_closed(combo, 3.0, symbol=OTHER, hour=i) for i in range(10)]
        + [_closed(combo, -2.0, symbol=SYMBOL, hour=20 + i) for i in range(3)]
    )

    stats = model._stats_for_choice(SYMBOL)

    assert stats[combo].trades == 3
    assert stats[combo].mean_r == pytest.approx(-2.0)


def test_clone_state_is_rebuilt_from_scratch_every_round(config: dict[str, Any]) -> None:
    """Ayrı bir durum dosyası yok: posterior defterin SAF bir fonksiyonu olmalı."""
    model = VwapClone(config=config)
    model.observe_closed_trades([_closed("band0_tp0", 5.0)])
    model.observe_closed_trades([_closed("band0_tp0", -5.0)])

    assert model.stats_for(SYMBOL)["band0_tp0"].trades == 1
    assert model.stats_for(SYMBOL)["band0_tp0"].mean_r == pytest.approx(-5.0)


def test_clone_ignores_rows_from_a_retired_combination(config: dict[str, Any]) -> None:
    """Çarpan listesi değişmişse eski satırlar yeni kolların ortalamasına karışmamalı."""
    model = VwapClone(config=config)
    model.observe_closed_trades([_closed("atr9_tp9", 5.0)])

    assert all(not item.measured for item in model.stats_for(SYMBOL).values())


def test_clone_exploration_share_never_drops_to_zero(config: dict[str, Any]) -> None:
    """Susturulan kombinasyon bir daha ÖLÇÜLEMEZ; keşif payı bunu engeller.

    Isınma bilerek tamamlanır (`_warmed`): birinci adım açıkken her çekiliş zaten
    "denenmemiş" olurdu ve epsilon'un payı hiç ölçülemezdi.
    """
    model = VwapClone(config=config)
    model.observe_closed_trades(
        _warmed(model) + [_closed(model._combos[0].key, 9.0, hour=100 + i) for i in range(10)]
    )
    rng = random.Random(7)

    picks = [model.choose_combo(SYMBOL, rng=rng)[2] for _ in range(400)]

    assert "unexplored" not in picks
    assert picks.count("explore") == pytest.approx(100, rel=0.35)  # epsilon = 0.25


def test_clone_grid_is_the_sources_grid(config: dict[str, Any]) -> None:
    """3 bant × 3 hedef = 9 kombinasyon; `sl_mult` bir eksen DEĞİLDİR (kaynakta sabit)."""
    model = VwapClone(config=config)

    assert len(model._combos) == 9
    assert len({combo.key for combo in model._combos}) == 9
    assert sorted({combo.band_mult for combo in model._combos}) == [1.5, 2.0, 2.5]
    assert sorted({combo.tp_mult for combo in model._combos}) == [0.5, 0.75, 1.0]
    assert {combo.params.sl_mult for combo in model._combos} == {0.5}


def test_clone_does_not_learn_a_reward_risk_target(config: dict[str, Any]) -> None:
    """Kaynakta dayatılmış bir hedef/stop oranı yoktur: hedef VWAP mesafesinin kesridir.

    Ev tarafında (model 14) hedef bir R projeksiyonudur ve 1.5R kapısına tabidir; kopyada
    öyle bir eksen hiç bulunmaz. Bu testin düşmesi, ev kuralının kopyaya sızdığı anlamına
    gelir.
    """
    model = VwapClone(config=config)

    assert all(not hasattr(combo, "target_reward_risk") for combo in model._combos)
    assert all(combo.tp_mult <= 1.0 for combo in model._combos)
    assert "target_reward_risks" not in config["vwap"]["clone"]
    assert "atr_multiples" not in config["vwap"]["clone"]


# --------------------------------------------------------------------------- #
# Model 14 — ev kuralları
# --------------------------------------------------------------------------- #
def test_managed_uses_risk_sizing(config: dict[str, Any]) -> None:
    signal = VwapManaged(config=config).generate_signals(_market())[0]

    assert signal.sizing == "risk"
    assert signal.notional_fraction is None
    assert signal.stop_price is not None


def test_managed_applies_the_one_percent_stop_floor(config: dict[str, Any]) -> None:
    """Stop GENİŞLETİLMEZ, kurulum ATLANIR (kural 14'ün aynı gerekçesi)."""
    tight = dict(config)
    tight["scalp"] = {**config["scalp"], "min_stop_pct": 0.9}

    assert VwapManaged(config=tight).generate_signals(_market()) == []


def test_managed_applies_the_reward_risk_gate(config: dict[str, Any]) -> None:
    strict = dict(config)
    strict["scalp"] = {**config["scalp"], "min_reward_risk": 99.0}

    assert VwapManaged(config=strict).generate_signals(_market()) == []


def test_managed_target_is_capped_by_the_vwap(config: dict[str, Any]) -> None:
    """Hedef, projeksiyon ile VWAP'in YAKIN olanıdır — kapının canlı kalmasının koşulu."""
    model = VwapManaged(config=config)
    data = _market()
    candidate = _candidates(data)[0]
    signal = model.generate_signals(data)[0]

    assert signal.take_profits[0].price <= candidate.vwap + 1e-9


def test_managed_plays_one_signal_per_round(config: dict[str, Any]) -> None:
    """Kıyas hedefi scalp_fixed tur başına tek pozisyon açar; beşi birden açmak farkı bozardı."""
    universe = [f"{name}-USDT-SWAP" for name in ("BTC", "ETH", "SOL", "XRP")]
    data = market(
        {
            symbol: frame(_reverting(spike=0.5 * index), spread=SPREAD,
                          freq="15min", start=START)
            for index, symbol in enumerate(universe)
        }
    )

    assert len(VwapManaged(config=config).generate_signals(data)) == 1


def test_managed_carries_the_same_management_as_the_clone(config: dict[str, Any]) -> None:
    """Tek kopya sözleşmesi: iki model aynı çıkış kuralını görmezse fark yönetimin olmaz."""
    data = _market()
    clone = VwapClone(config=config).generate_signals(data)[0]
    managed = VwapManaged(config=config).generate_signals(data)[0]

    assert managed.breakeven_at_r == clone.breakeven_at_r
    assert managed.partial_tp == clone.partial_tp
    assert managed.trail_giveback_pct == clone.trail_giveback_pct


def test_managed_does_not_learn(config: dict[str, Any]) -> None:
    """Kanca uygulanmadığı için motor defteri hiç okutmaz — üçüncü değişken yok."""
    from strategies.base import Strategy

    assert VwapManaged.observe_closed_trades is Strategy.observe_closed_trades
    assert VwapClone.observe_closed_trades is not Strategy.observe_closed_trades


def test_managed_tags_the_arm(config: dict[str, Any]) -> None:
    """Kol kırılımı scalp katmanının rapor sözleşmesi: etiketsiz satır TagError üretir."""
    signal = VwapManaged(config=config).generate_signals(_market())[0]

    assert parse_tag(signal.reason, "arm") == vwap_signal.ARM_NAME


def test_managed_stop_scale_stays_in_the_comparability_band(config: dict[str, Any]) -> None:
    """Model 14'ün stop ölçeği kural 14'ün kıyaslanabilirlik bandında (1×–2.5× ATR) kalmalı.

    Bu test bir zamanlar `atr_multiple == scalp.stop_atr_multiple` diye yazılıydı: amaç
    doğruydu (maliyet ölçeği eşit olmazsa 14 ↔ scalp_fixed farkı kısmen maliyet farkı olur)
    ama araç yanlıştı ve iddiayı TERSİNE çeviriyordu — 5.0'ı, yani modelin hiç sinyal
    üretemediği değeri, "doğru" sayıp koruyordu (docs/decisions.md > 26).

    İki sebeple: (a) kıyaslanabilirliği belirleyen şey config'teki ATR KATI değil defterde
    GERÇEKLEŞEN stop mesafesidir (`avg_stop_distance_pct`; kural 14'ün bandı ve kabul
    çıtasının ⚠B uyarısı ona bakar) ve iki modelin ATR tabanı aynı katta aynı mesafeyi
    vermiyor; (b) iki sayının eşitliği, ikisi de ölçüme hiç girmese bile sağlanır — oysa
    hiç işlem açmayan bir model kıyas hedefiyle aynı ölçekte de olamaz.

    Bir birim testi gerçekleşen mesafeyi ölçemez (piyasa verisi ister); ölçebileceği şey
    değerin kural 14'ün yazılı bandında durduğudur. Bu band 5.0'ı reddeder.
    """
    atr_multiple = config["vwap"]["managed"]["atr_multiple"]
    assert 1.0 <= atr_multiple <= 2.5, "kural 14: stop mesafeleri 1×–2.5× ATR bandında tutulur"
    assert atr_multiple < config["max_stop_atr_multiple"], "katmanın stop tavanının altında"


def test_no_setup_means_no_signal(config: dict[str, Any]) -> None:
    flat = market({SYMBOL: frame([100.0] * 40, spread=SPREAD, freq="15min", start=START)})

    assert VwapClone(config=config).generate_signals(flat) == []
    assert VwapManaged(config=config).generate_signals(flat) == []


def test_short_setups_are_symmetric(config: dict[str, Any]) -> None:
    closes = [200.0 - value for value in _reverting()]  # aynı kurulumun aynası
    data = _market(closes)

    signals = VwapManaged(config=config).generate_signals(data)

    assert signals and signals[0].direction == "short"
    assert signals[0].stop_price is not None
    assert signals[0].stop_price > signals[0].take_profits[0].price


# --------------------------------------------------------------------------- #
# Eleme sayımı (Survey) — "aday yok" barının denetim izi
# --------------------------------------------------------------------------- #
# Neden test ediliyor: model 13'ün hiçbir kapısı yoktur, yani `signals=0` demek
# "propose hiç aday bulmadı" demektir ve defterdeki boşluk tek başına bunu sessizce
# bozulmuş bir sinyal modülünden ayırt etmez. Sayım o ayrımı taşıyan tek kayıttır.
def test_the_survey_counts_a_found_setup() -> None:
    candidates, survey = vwap_signal.scan(
        _market(), atr_period=14, band_mult=2.0, min_vwap_bars=8
    )

    assert len(candidates) == 1
    assert survey.examined == 1
    assert survey.candidates == 1
    assert survey.counts[vwap_signal.SETUP] == 1
    assert survey.furthest_symbol == SYMBOL
    assert survey.max_extension == pytest.approx(abs(candidates[0].z_prev))


def test_the_survey_separates_no_reversal_from_a_crossed_vwap() -> None:
    """İkisi farklı şeyler söyler: biri trendin sürdüğünü, diğeri dönüşün kaçırıldığını."""
    extending = _reverting()
    extending[-1] = extending[-2] - 1.0
    _, still = vwap_signal.scan(
        _market(extending), atr_period=14, band_mult=2.0, min_vwap_bars=8
    )
    _, crossed = vwap_signal.scan(
        _market(_reverting(rebound=6.0)), atr_period=14, band_mult=2.0, min_vwap_bars=8
    )

    assert still.counts[vwap_signal.STILL_EXTENDING] == 1
    assert still.counts[vwap_signal.CROSSED] == 0
    assert crossed.counts[vwap_signal.CROSSED] == 1
    assert crossed.counts[vwap_signal.STILL_EXTENDING] == 0


def test_the_survey_reports_how_close_the_band_came() -> None:
    """0 aday üreten bir barda "en uzak sembol kaç σ'daydı" sorusunun cevabı budur.

    Bandı 2σ'da bırakıp hiç aday görmeyen bir gün ile bandın kıl payı ötesinde duran bir
    gün bambaşka iki durumdur; sayım olmadan ikisi de "sinyal yok" satırıdır.
    """
    closes = _reverting()
    closes[-1] = 120.0  # sapmayı öyle kaydırır ki önceki bar da bandın içine düşer

    candidates, survey = vwap_signal.scan(
        _market(closes), atr_period=14, band_mult=2.0, min_vwap_bars=8
    )

    assert candidates == []
    assert survey.counts[vwap_signal.INSIDE_BAND] == 1
    assert 0.0 < survey.max_extension < 2.0


def test_the_survey_counts_symbols_without_a_usable_vwap() -> None:
    """Sapması sıfır olan pencerede z tanımsızdır: bant içi DEĞİL, ölçülemez sayılır."""
    flat = market({SYMBOL: frame([100.0] * 40, spread=SPREAD, freq="15min", start=START)})

    _, survey = vwap_signal.scan(flat, atr_period=14, band_mult=2.0, min_vwap_bars=8)

    assert survey.examined == 1
    assert survey.counts[vwap_signal.NO_VWAP] == 1
    assert survey.furthest_symbol is None
    assert survey.describe()


def test_the_survey_only_counts_the_models_own_universe() -> None:
    """Kopyanın evreni dışındaki sembol taranmaz: sayım modelin gördüğü kadardır."""
    data = market(
        {
            SYMBOL: frame(_reverting(), spread=SPREAD, freq="15min", start=START),
            OTHER: frame(_reverting(), spread=SPREAD, freq="15min", start=START),
        }
    )

    _, survey = vwap_signal.scan(
        data, atr_period=14, band_mult=2.0, min_vwap_bars=8, symbols=[SYMBOL]
    )

    assert survey.examined == 1


def test_the_survey_does_not_change_what_propose_returns() -> None:
    """Sayım bir denetim izidir; ölçümü etkilerse eklenmemiş olması gerekirdi."""
    data = _market()

    proposed = vwap_signal.propose(data, atr_period=14, band_mult=2.0, min_vwap_bars=8)
    scanned, _ = vwap_signal.scan(data, atr_period=14, band_mult=2.0, min_vwap_bars=8)

    assert proposed == scanned


def test_propose_logs_the_survey_with_the_model_label(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """İki model aynı barı ayrı evrenlerle tarar; iki sayım satırı ayırt edilebilmeli."""
    with caplog.at_level("INFO", logger="strategies.vwap.signal"):
        vwap_signal.propose(
            _market(), atr_period=14, band_mult=2.0, min_vwap_bars=8, model="vwap_clone"
        )

    assert any(
        "vwap_clone" in record.message and vwap_signal.ARM_NAME in record.message
        for record in caplog.records
    )


# --------------------------------------------------------------------------- #
# Model 13 — kurulum kapıları (bozuk config sessizce sinyalsiz bir modele dönüşmesin)
# --------------------------------------------------------------------------- #
def _clone_config(config: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    broken = dict(config)
    broken["vwap"] = {**config["vwap"], "clone": {**config["vwap"]["clone"], **overrides}}
    return broken


def test_a_std_window_wider_than_the_minimum_bar_count_is_refused(
    config: dict[str, Any]
) -> None:
    """σ son barda hiç hesaplanamazdı: model her turu sessizce `band_yok` ile geçerdi."""
    with pytest.raises(ValueError, match="std_window"):
        VwapClone(config=_clone_config(config, std_window=30, min_bars=25))


def test_a_vwap_window_below_the_minimum_bar_count_is_refused(config: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="vwap_window"):
        VwapClone(config=_clone_config(config, vwap_window=20, min_bars=25))


def test_a_target_multiple_above_one_is_refused(config: dict[str, Any]) -> None:
    """Hedef VWAP'i ASLA aşmaz; kural çarpanın kendisinde durur, bir kırpmada değil."""
    with pytest.raises(ValueError, match="tp_mults"):
        VwapClone(config=_clone_config(config, tp_mults=[0.5, 1.5]))


def test_the_shipped_configuration_builds(config: dict[str, Any]) -> None:
    """Kapılar gerçek config'i reddetmiyor — testin kendisi de bir regresyon kapısıdır."""
    assert VwapClone(config=config)._combos


# --------------------------------------------------------------------------- #
# Survey: |z_prev| kovaları (denetim izi, kural 15)
# --------------------------------------------------------------------------- #
def _scan(data: MarketData, *, band_mult: float = 2.0):
    return vwap_signal.scan(
        data, atr_period=14, band_mult=band_mult, min_vwap_bars=8
    )[1]


def test_elimination_counts_stay_exhaustive() -> None:
    """`counts` AYRIKTIR: Σ = taranan sembol. Kovalar buraya karışsaydı bu bozulurdu."""
    survey = _scan(_market())

    assert sum(survey.counts.values()) == survey.examined


def test_extension_buckets_are_cumulative() -> None:
    """Kova "en az bu kadar uzaktı" sayar: eşik büyüdükçe sayı ARTAMAZ."""
    survey = _scan(_market())
    values = [survey.extensions[vwap_signal.bucket_key(t)]
              for t in vwap_signal.EXTENSION_BUCKETS]

    assert values == sorted(values, reverse=True)
    assert values[0] >= 1  # kurulumun kendisi en az 2σ'daydı, yani 1.0 kovasındadır


def test_a_symbol_without_a_vwap_lands_in_no_bucket() -> None:
    """z hesaplanamamışsa kova da sayılmaz: `nan` bir uzaklık değildir."""
    survey = _scan(_market([100.0] * 40))

    assert survey.examined == 1
    assert survey.counts[vwap_signal.NO_VWAP] == 1
    assert set(survey.extensions.values()) == {0}


def test_the_report_merges_reasons_and_buckets() -> None:
    survey = _scan(_market())
    report = survey.report()

    assert report[vwap_signal.SETUP] == survey.counts[vwap_signal.SETUP]
    assert report["z_ge_2_0"] == survey.extensions["z_ge_2_0"]


def test_managed_take_survey_carries_the_buckets(config: dict[str, Any]) -> None:
    """Kovalar tur raporuna düşmezse `bant_ici=13` satırı bandın ölçeğini denetleyemez."""
    model = VwapManaged(config=config)
    model.generate_signals(_market())
    survey = model.take_survey()

    assert survey is not None
    assert set(survey) >= {"z_ge_1_0", "z_ge_1_5", "z_ge_2_0", "z_ge_2_5"}
    assert all(isinstance(value, int) for value in survey.values())


def test_the_buckets_never_change_the_candidates() -> None:
    """Sayım bir denetim izidir (kural 15): adayları ve sıralarını etkilemez."""
    data = _market()
    candidates, survey = vwap_signal.scan(
        data, atr_period=14, band_mult=2.0, min_vwap_bars=8
    )

    assert [item.symbol for item in candidates] == [item.symbol for item in _candidates(data)]
    assert survey.candidates == len(candidates)
