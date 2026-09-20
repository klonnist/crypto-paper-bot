"""scripts/backtest.py: harness canlı motoru geçmiş bir pencerede koşturuyor mu.

Buradaki testler backtest'in SONUÇLARINI değil, harness'ın kendisini ölçer — doğru barları
işliyor mu, gerçek deftere dokunuyor mu, uyarlanabilir modelleri doğru tanıyor mu,
geçerlilik kapılarını gerçekten düşürüyor mu.

Motorun bar bazlı sadakati burada TEKRAR test edilmez: onun yeri
`tests/test_engine_per_bar.py`dir ve backtest tam olarak o yolu kullanır. İkinci bir kopya,
harness'ın kendi motorunu yazdığı izlenimi verirdi — oysa yazmıyor (docs/backtest.md > 0).
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any, Mapping, Sequence

import pandas as pd
import pytest

from core.config import load_config
from core.engine import Engine, EmittedSignal, ModelReport, RoundReport
from core.ledger import Ledger
from core.portfolio import Portfolio
from scripts import backtest
from scripts.backtest import (
    SignalKey,
    check_validity,
    compare_signals,
    emitted_keys,
    is_adaptive,
    payload_keys,
)
from strategies.base import (
    ClosedTrade,
    Direction,
    MarketData,
    Signal,
    Strategy,
)

SYMBOL = "BTC-USDT-SWAP"
START = pd.Timestamp("2026-01-01 00:00:00", tz="UTC")
INDEX = pd.date_range(START, periods=8, freq="15min", tz="UTC", name="ts")


def _frame() -> pd.DataFrame:
    frame = pd.DataFrame(
        [(100.0 + i, 100.6 + i, 99.4 + i, 100.0 + i) for i in range(len(INDEX))],
        index=INDEX,
        columns=["open", "high", "low", "close"],
    )
    frame["volume"] = 1.0
    return frame


def _market(bars: int) -> MarketData:
    window = _frame().head(bars)
    return MarketData(ohlcv={SYMBOL: window}, btc=window, funding={}, as_of=window.index[-1])


def _config(**overrides: Any) -> dict[str, Any]:
    config = load_config()
    config.update(
        fee_rate=0.0, slippage_base=0.0, slippage_short_stop=0.0,
        timeframe="15m", signals_per_bar=True,
    )
    config.update(overrides)
    return config


class _EveryBar(Strategy):
    allowed_directions: list[Direction] = ["long"]
    name = "her_bar"

    def __init__(self) -> None:
        self.signal_bars: list[pd.Timestamp] = []

    def generate_signals(
        self, market: MarketData, peer_signals: Mapping[str, tuple[Signal, ...]] | None = None
    ) -> list[Signal]:
        self.signal_bars.append(market.as_of)
        price = float(market.ohlcv[SYMBOL]["close"].iloc[-1])
        return [Signal(symbol=SYMBOL, direction="long", stop_price=price * 0.9)]


class _Learner(_EveryBar):
    """`observe_closed_trades`ı UYGULAYAN model (kural 16)."""

    name = "ogrenen"

    def observe_closed_trades(self, trades: Sequence[ClosedTrade]) -> None:
        return None


# --------------------------------------------------------------------------- #
# Tohumlama: harness'ın tek gerçek müdahalesi
# --------------------------------------------------------------------------- #
def test_seeding_the_start_bar_makes_the_engine_process_the_whole_window(
    tmp_path: Path,
) -> None:
    """Tohumlanmış `last_processed_bar` tüm pencereyi işletir; tohumsuz yalnızca son barı.

    Bu, harness'ın var olma sebebidir. `core/engine.py::_timeline` boş defterli bir model
    için bilinçli olarak `index[-1:]` döner — canlıda yeni açılan bir modelin geçmişi
    geriye dönük işlemesi yalnızca boş özsermaye satırı üretirdi. Backtest tam olarak o
    geçmişi istediği için barı KENDİSİ yazar; motorun kuralı değişmez.
    """
    config = _config()
    market = _market(6)

    unseeded = _EveryBar()
    ledger_a = Ledger(tmp_path / "unseeded")
    ledger_a.initialize_model(unseeded.name, initial_capital=10_000.0)
    Engine([unseeded], config=config, ledger=ledger_a, portfolio=Portfolio(config)).run_round(market)

    seeded = _EveryBar()
    ledger_b = Ledger(tmp_path / "seeded")
    ledger_b.initialize_model(seeded.name, initial_capital=10_000.0)
    state = ledger_b.load_state(seeded.name)
    assert state is not None
    state["last_processed_bar"] = INDEX[0].isoformat()
    ledger_b.write_state(seeded.name, state)
    Engine([seeded], config=config, ledger=ledger_b, portfolio=Portfolio(config)).run_round(market)

    assert unseeded.signal_bars == [INDEX[5]], "tohumsuz: yalnızca son bar"
    assert seeded.signal_bars == list(INDEX[1:6]), "tohumlanan bar İŞLENMEZ, sonrası işlenir"


def test_the_seeded_bar_itself_is_not_processed(tmp_path: Path) -> None:
    """Pencere yarı açıktır: `start` işlenmez, `end` işlenir.

    `_timeline` `ts > last_bar` süzer. Tohumlanan barı da işlemek, canlıda zaten işlenmiş
    bir barı ikinci kez işlemek demekti — backtest'te de aynı kural geçerli olmalı ki iki
    koşu aynı pencereyi aynı biçimde saysın.
    """
    config = _config()
    strategy = _EveryBar()
    ledger = Ledger(tmp_path / "ledger")
    ledger.initialize_model(strategy.name, initial_capital=10_000.0)
    state = ledger.load_state(strategy.name)
    assert state is not None
    state["last_processed_bar"] = INDEX[2].isoformat()
    ledger.write_state(strategy.name, state)

    Engine([strategy], config=config, ledger=ledger, portfolio=Portfolio(config)).run_round(
        _market(5)
    )

    assert INDEX[2] not in strategy.signal_bars
    assert strategy.signal_bars == [INDEX[3], INDEX[4]]


# --------------------------------------------------------------------------- #
# Uyarlanabilirlik: kancadan TÜRETİLİR, elle yazılmaz
# --------------------------------------------------------------------------- #
def test_adaptive_models_are_detected_from_the_hook_not_a_hardcoded_list() -> None:
    """Elle yazılan bir liste, yeni bir uyarlanabilir model eklendiği gün sessizce
    yanlış olur ve Kapı 0 o modelden haksız yere birebir eşleşme beklerdi."""
    assert is_adaptive(_Learner()) is True
    assert is_adaptive(_EveryBar()) is False


def test_the_shipped_scalp_models_split_into_adaptive_and_not() -> None:
    """Kapı 0'ın hangi modelleri bağladığı canlı kümede de doğrulanır (docs/backtest.md > 1)."""
    from core.layers import resolve_layer
    from strategies.registry import build

    config = resolve_layer(load_config(), "scalp").config
    adaptive = {
        name for name in ("scalp_bandit", "scalp_fixed", "scalp_managed",
                          "vwap_clone", "vwap_managed")
        if is_adaptive(build(name, config=config))
    }
    assert adaptive == {"scalp_bandit", "vwap_clone"}


# --------------------------------------------------------------------------- #
# Geçerlilik kapıları (docs/backtest.md > 3)
# --------------------------------------------------------------------------- #
def _report(**counters: int) -> RoundReport:
    return RoundReport(
        as_of=START,
        models=(ModelReport(model="m", **counters),),
    )


def test_a_clean_window_has_no_validity_violations() -> None:
    assert check_validity(_report(bars_processed=10)) == {}


@pytest.mark.parametrize(
    ("counters", "expected"),
    [
        ({"missing_bars": 3}, "missing_bars=3"),
        ({"unchecked_position_bars": 2}, "unchecked_position_bars=2"),
    ],
)
def test_a_gap_in_the_window_fails_the_validity_gate(
    counters: dict[str, int], expected: str
) -> None:
    """İkisi de "o barda stop/TP/likidasyon hiç sorulmadı" demektir.

    Sıfırdan büyükse pencere eksik bir geçmişin üstüne yazılmıştır; sonucu yorumlamak,
    olmamış bir çıkışın hayatta kalmasını performans sanmak olurdu.
    """
    violations = check_validity(_report(bars_processed=10, **counters))
    assert violations == {"m": [expected]}


# --------------------------------------------------------------------------- #
# Kapı 0: sinyal karşılaştırması
# --------------------------------------------------------------------------- #
def _key(model: str, bar: str, symbol: str = SYMBOL, direction: str = "long") -> SignalKey:
    return SignalKey(model=model, bar=bar, symbol=symbol, direction=direction)


def test_identical_signal_sets_match() -> None:
    keys = {_key("sabit", "2026-01-01T00:00:00+00:00")}
    table = compare_signals(keys, keys, adaptive=())
    assert table["sabit"]["match"] is True
    assert table["sabit"]["gate"] is True


def test_a_missing_live_signal_is_reported_on_the_right_side() -> None:
    mine = {_key("sabit", "2026-01-01T00:00:00+00:00")}
    theirs = {_key("sabit", "2026-01-01T00:15:00+00:00")}
    row = compare_signals(mine, theirs, adaptive=())["sabit"]

    assert row["match"] is False
    assert row["only_backtest"] == ["2026-01-01T00:00:00+00:00 BTC-USDT-SWAP long"]
    assert row["only_live"] == ["2026-01-01T00:15:00+00:00 BTC-USDT-SWAP long"]


def test_adaptive_models_are_reported_but_do_not_gate() -> None:
    """Uyarlanabilir modelden eşleşme beklenmez: boş defterden başlayan koşu farklı bir
    geçmiş görür (kural 16). Ayrışması bir arıza değil, tanımın sonucudur."""
    mine = {_key("ogrenen", "2026-01-01T00:00:00+00:00")}
    theirs = {_key("ogrenen", "2026-01-01T00:15:00+00:00")}
    row = compare_signals(mine, theirs, adaptive=("ogrenen",))["ogrenen"]

    assert row["gate"] is False
    assert row["match"] is False  # ayrışma gizlenmez, yalnızca kapı sayılmaz


def test_the_two_sides_of_gate_zero_render_the_same_bar_identically() -> None:
    """Rapor tarafı `pd.Timestamp`, canlı taraf JSON metni taşır; anahtar AYNI olmalı.

    Kapı 0'ı ilk koşusunda düşüren hata tam olarak buydu: rapor tarafı `str(Timestamp)`
    ile `"... 15:15:00+00:00"`, canlı taraf `isoformat()` ile `"...T15:15:00+00:00"`
    üretiyordu. Birebir aynı sinyal kümesi sıfır kesişimle "AYRIŞTI" göründü — yani kapı,
    ölçmesi gereken sadakat yerine kendi biçimlendirme farkını raporladı.

    Test İKİ şeyi birden çivilemek zorunda, çünkü tek başına hiçbiri yetmiyor:
    (a) iki tarafın anahtarı EŞİT — biri `pd.Timestamp`ten, diğeri JSON metninden kurulur;
    (b) ortak biçim ISO-8601 ('T') — (a) tek başına, iki taraf aynı bozuk biçimi
    paylaştığında da geçerdi, çünkü ikisi artık aynı fonksiyondan geçiyor.
    """
    bar = pd.Timestamp("2026-09-15 15:15:00", tz="UTC")
    window = {"start": bar - pd.Timedelta("1min"), "end": bar + pd.Timedelta("1min")}

    report = RoundReport(
        as_of=bar,
        models=(
            ModelReport(
                model="scalp_fixed",
                emitted=(
                    EmittedSignal(
                        model="scalp_fixed",
                        symbol="BNB-USDT-SWAP",
                        direction="short",
                        bar=bar,
                        fills_at=bar + pd.Timedelta("15min"),
                        close=100.0,
                    ),
                ),
            ),
        ),
    )
    payload = {
        "round": {
            "models": [
                {
                    "model": "scalp_fixed",
                    "emitted": [
                        {
                            "model": "scalp_fixed",
                            "symbol": "BNB-USDT-SWAP",
                            "direction": "short",
                            "bar": bar.isoformat(),
                        }
                    ],
                }
            ]
        }
    }

    mine = emitted_keys(report)
    theirs = payload_keys(payload, **window)
    assert mine == theirs, "aynı an, iki farklı biçim -> kapı kendi hatasını ölçer"
    assert compare_signals(mine, theirs, adaptive=())["scalp_fixed"]["match"] is True
    assert next(iter(mine)).bar == "2026-09-15T15:15:00+00:00", "ortak biçim ISO-8601"


def test_emitted_keys_ignore_price_fields() -> None:
    """Karşılaştırmanın birimi (model, bar, sembol, yön); fiyat DEĞİL.

    Kayan nokta eşitliği kırılgandır ve sorulan soru "aynı kurulumu buldu mu".
    """
    assert emitted_keys(
        RoundReport(as_of=START, models=(ModelReport(model="m", signals=0),))
    ) == set()
    assert {field for field in SignalKey.__dataclass_fields__} == {
        "model", "bar", "symbol", "direction",
    }


# --------------------------------------------------------------------------- #
# Backtest gerçek deftere DOKUNMAZ
# --------------------------------------------------------------------------- #
def test_backtest_writes_only_under_its_own_root(tmp_path: Path) -> None:
    """Bir backtest denetim izine (kural 1) yazmaz — okumaz da.

    Gerçek defter kökü sabit diskteki `ledgers_scalp/`dir; harness'ın `Ledger`'ı yalnızca
    kendi dizinini görür. Test bunu dizin düzeyinde sabitler: koşudan sonra çıktı dizini
    dışında hiçbir şey oluşmamalı.
    """
    config = _config()
    root = tmp_path / "backtests" / "kosu"
    ledger = Ledger(root / "ledger")
    strategy = _EveryBar()
    ledger.initialize_model(strategy.name, initial_capital=10_000.0)
    state = ledger.load_state(strategy.name)
    assert state is not None
    state["last_processed_bar"] = INDEX[0].isoformat()
    ledger.write_state(strategy.name, state)

    Engine([strategy], config=config, ledger=ledger, portfolio=Portfolio(config)).run_round(
        _market(5)
    )

    written = {path.relative_to(tmp_path).parts[0] for path in tmp_path.rglob("*") if path.is_file()}
    assert written == {"backtests"}
    assert (root / "ledger" / strategy.name / "trades.csv").exists()


# --------------------------------------------------------------------------- #
# Derinlik override'ı: yalnızca DERİNLEŞTİRİR
# --------------------------------------------------------------------------- #
def test_history_bars_override_refuses_to_shallow_the_window(tmp_path: Path) -> None:
    """Sığlaştırmak, modelin canlıda gördüğünden AZ veri görmesi demekti.

    Derinleştirmek zararsızdır (lookback'ler sınırlı), sığlaştırmak değildir: `tail(300)`
    okuyan bir model 200 barlık bir görüntüde BAŞKA bir sinyal üretir ve backtest artık
    canlıyı değil, kendi uydurduğu bir modeli ölçer. Sessiz kırpma yerine hata.
    """
    from scripts.backtest import run_backtest

    with pytest.raises(ValueError, match="DERİNLEŞTİRİR"):
        run_backtest(
            layer_name="scalp",
            start=START,
            end=START + pd.Timedelta("1D"),
            out_dir=tmp_path / "out",
            history_bars=1,
        )


def test_history_bars_override_reaches_the_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    """Override'ın config'e GERÇEKTEN işlediğini sabitler.

    Koşu süresinin uzaması ya da manifest'e bir sayı yazılması "daha derin veri çekildi"nin
    KANITI değildir; kanıt, `core/data.py`ye giden config'in o değeri taşımasıdır. Bayrak
    sessizce düşerse backtest sığ veriyle koşar ve bunu kimse fark etmez.
    """
    seen: dict[str, Any] = {}

    def _capture(config: Any, **kwargs: Any) -> Any:
        seen["history_bars"] = config["data"]["history_bars"]
        raise RuntimeError("dur")  # anlık görüntüden sonrasına gerek yok

    monkeypatch.setattr("scripts.backtest.load_market_data", _capture)

    from scripts.backtest import run_backtest

    with pytest.raises(RuntimeError, match="dur"):
        run_backtest(
            layer_name="scalp",
            start=START,
            end=START + pd.Timedelta("1D"),
            out_dir=Path("/tmp/does-not-matter"),
            history_bars=6000,
        )
    assert seen["history_bars"] == 6000



# --------------------------------------------------------------------------- #
# Embargo (docs/backtest.md > 6.1)
# --------------------------------------------------------------------------- #
def test_embargo_shifts_the_window_forward_by_whole_bars() -> None:
    """OOS penceresi, IS kurulumlarının çözüldüğü barlardan SONRA başlamalı.

    Doğru boşluk azami tutuş süresidir: bu geometride hiçbir pozisyon zaman stop'undan
    uzun yaşamaz, yani o kadar bar sonrası hiçbir IS etiketi bu pencereyle örtüşmez.
    """
    start = pd.Timestamp("2026-07-19T00:00:00+00:00")
    with pytest.raises(ValueError, match="embargo penceriyi tüketti"):
        backtest.run_backtest(
            layer_name="scalp", start=start,
            end=start + pd.Timedelta(minutes=15) * 4,   # 4 bar, embargo 16 bar
            out_dir=Path(tempfile.mkdtemp()), embargo_bars=16,
        )


def test_embargo_rejects_a_negative_gap() -> None:
    """Negatif embargo, pencereyi GERİ kaydırıp kontaminasyonu artırırdı."""
    start = pd.Timestamp("2026-07-19T00:00:00+00:00")
    with pytest.raises(ValueError, match="negatif olamaz"):
        backtest.run_backtest(
            layer_name="scalp", start=start, end=start + pd.Timedelta(days=5),
            out_dir=Path(tempfile.mkdtemp()), embargo_bars=-4,
        )


# --------------------------------------------------------------------------- #
# Evren daraltma, maliyet override'ı, funding derinliği, sinyal kesimi
#
# Dördü de `--history-bars`tan FARKLI bir sınıfta: o, sonucu değiştirmemesi SINANAN bir
# derinlik ayarıdır; bunlar ölçümün koşullarını (hangi semboller, hangi maliyet, hangi
# funding, hangi barlarda sinyal) doğrudan kaydırır. Bu yüzden hepsi manifest'e yazılır
# ve hiçbiri sessizce uygulanmaz.
# --------------------------------------------------------------------------- #
def test_symbols_can_narrow_the_universe_but_never_widen_it() -> None:
    """Evren katmanın TANIMIDIR (kural 6); harness onu büyütemez, yalnızca daraltır."""
    with pytest.raises(ValueError, match="GENİŞLETEMEZ"):
        backtest.run_backtest(
            layer_name="scalp",
            start=START,
            end=START + pd.Timedelta("1D"),
            out_dir=Path(tempfile.mkdtemp()),
            symbols=["BTC-USDT-SWAP", "DOGEDOGE-USDT-SWAP"],
        )


def test_narrowed_universe_reaches_the_data_layer(monkeypatch: pytest.MonkeyPatch) -> None:
    """Daraltmanın KANITI, `core/data.py`ye giden sembol listesidir."""
    seen: dict[str, Any] = {}

    def _capture(config: Any, **kwargs: Any) -> Any:
        seen["symbols"] = kwargs.get("symbols")
        raise RuntimeError("dur")

    monkeypatch.setattr("scripts.backtest.load_market_data", _capture)

    with pytest.raises(RuntimeError, match="dur"):
        backtest.run_backtest(
            layer_name="scalp",
            start=START,
            end=START + pd.Timedelta("1D"),
            out_dir=Path(tempfile.mkdtemp()),
            symbols=["BTC-USDT-SWAP"],
        )
    assert seen["symbols"] == ["BTC-USDT-SWAP"]


def test_cost_override_reaches_the_engine_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """Maliyet override'ı config'e GERÇEKTEN işler; manifest'e yazılan sayı bir iddia değil."""
    seen: dict[str, Any] = {}

    def _capture(config: Any, **kwargs: Any) -> Any:
        seen["fee_rate"] = config["fee_rate"]
        seen["slippage_base"] = config["slippage_base"]
        raise RuntimeError("dur")

    monkeypatch.setattr("scripts.backtest.load_market_data", _capture)

    with pytest.raises(RuntimeError, match="dur"):
        backtest.run_backtest(
            layer_name="ema",
            start=START,
            end=START + pd.Timedelta("1D"),
            out_dir=Path(tempfile.mkdtemp()),
            fee_rate=0.00075,
            slippage_base=0.0001,
        )
    assert seen["fee_rate"] == pytest.approx(0.00075)
    assert seen["slippage_base"] == pytest.approx(0.0001)


def test_cost_override_never_touches_the_live_config() -> None:
    """Kural 6: canlı `config.yaml` bir backtest tarafından değiştirilemez."""
    from core.config import load_config

    before = load_config()["fee_rate"]
    with pytest.raises(RuntimeError):
        backtest.run_backtest(
            layer_name="ema",
            start=START,
            end=START + pd.Timedelta("1D"),
            out_dir=Path(tempfile.mkdtemp()),
            fee_rate=0.00075,
            models=["yok-boyle-bir-model"],
        )
    assert load_config()["fee_rate"] == before


def test_negative_costs_are_rejected() -> None:
    for kwargs in ({"fee_rate": -0.001}, {"slippage_base": -0.001}):
        with pytest.raises(ValueError, match="negatif olamaz"):
            backtest.run_backtest(
                layer_name="ema",
                start=START,
                end=START + pd.Timedelta("1D"),
                out_dir=Path(tempfile.mkdtemp()),
                **kwargs,
            )


def test_funding_depth_only_deepens() -> None:
    """Sığlaştırmak, funding'i canlıda ödenenden AZ göstermek demekti."""
    with pytest.raises(ValueError, match="DERİNLEŞTİRİR"):
        backtest.run_backtest(
            layer_name="ema",
            start=START,
            end=START + pd.Timedelta("1D"),
            out_dir=Path(tempfile.mkdtemp()),
            funding_periods=1,
        )


def test_signal_cutoff_stops_new_signals_but_not_position_management() -> None:
    """Kesimin anlamı "yeni kurulum alma", "açık pozisyonu dondur" DEĞİL.

    Dondurmak, dönemin son kurulumlarını kendi çıkış kurallarından mahrum bırakıp
    sonucu uydururdu — oysa dönem ataması giriş tarihine göredir ve o kurulumlar
    kapanışına kadar döneme aittir.
    """
    from strategies.base import MarketData, Signal, Strategy
    from tests.helpers_market import frame, market as snapshot

    calls: list[pd.Timestamp] = []

    class _Spy(Strategy):
        name = "spy"
        allowed_directions = ["long"]

        def generate_signals(self, market: MarketData, peer_signals: Any = None) -> list[Signal]:
            calls.append(market.as_of)
            return [Signal(symbol="BTC-USDT-SWAP", direction="long", stop_price=1.0)]

        def manage_positions(self, market: MarketData, positions: Any) -> list[Any]:
            return []

    strategy = _Spy()
    cutoff = pd.Timestamp("2026-01-02T00:00:00+00:00")
    backtest._silence_signals_after(strategy, cutoff)

    candles = frame(
        [100.0, 101.0, 102.0], start=pd.Timestamp("2026-01-01T00:00:00+00:00"), freq="1D"
    )
    before = snapshot({"BTC-USDT-SWAP": candles.iloc[:1]})
    on_cutoff = snapshot({"BTC-USDT-SWAP": candles.iloc[:2]})   # as_of == kesim barı
    after = snapshot({"BTC-USDT-SWAP": candles})

    assert len(strategy.generate_signals(before)) == 1      # kesimden önce: sinyal var
    # Kesim barının KENDİSİ hâlâ sinyal üretir: kural "bu bardan SONRA" der. Dışlayıcı
    # olsaydı dönemin son barı sessizce ölçüm dışı kalırdı.
    assert len(strategy.generate_signals(on_cutoff)) == 1
    assert strategy.generate_signals(after) == []           # kesimden sonra: sinyal yok
    assert calls == [before.as_of, on_cutoff.as_of]          # çağrı bile gitmedi
    # Pozisyon yönetimi gölgelenmedi: kesim çıkışları durdurmaz.
    assert strategy.manage_positions(after, []) == []


def test_signal_cutoff_keeps_the_adaptivity_probe_intact() -> None:
    """Gölgeleme ÖRNEK düzeyindedir: Kapı 0'ın uyarlanabilirlik tespiti sınıfa bakar.

    Modeli bir sarmalayıcı SINIFA koymak, `type(strategy).observe_closed_trades`
    kontrolünü bozar ve harness ölçtüğü modelin kimliğini değiştirmiş olurdu.
    """
    from strategies.registry import build

    adaptive = build("vwap_clone")
    plain = build("ema_trend")
    cutoff = pd.Timestamp("2026-01-02T00:00:00+00:00")

    backtest._silence_signals_after(adaptive, cutoff)
    backtest._silence_signals_after(plain, cutoff)

    assert backtest.is_adaptive(adaptive) is True
    assert backtest.is_adaptive(plain) is False


def test_signal_cutoff_must_sit_inside_the_window() -> None:
    with pytest.raises(ValueError, match="pencerenin içinde"):
        backtest.run_backtest(
            layer_name="ema",
            start=START,
            end=START + pd.Timedelta("5D"),
            out_dir=Path(tempfile.mkdtemp()),
            signal_cutoff=START - pd.Timedelta("1D"),
        )
