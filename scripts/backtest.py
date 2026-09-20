#!/usr/bin/env python3
"""Backtest harness: CANLI MOTORU geçmiş bir pencerede koşturur.

Kurallar `docs/backtest.md`'dedir ve sonuç üretilmeden ÖNCE yazılmıştır; bu dosya onların
uygulamasıdır, yeni bir kural koymaz.

**Burada ikinci bir motor YOKTUR.** `core/engine.py` atlanan turları telafi ederken zaten
tam olarak bir backtest yapar: son işlenmiş bardan `as_of`'a kadar her barı SIRAYLA işler,
emirler kendi barının ertesinden dolar (kural 13), stop/TP/likidasyon her barın kendi
`high`/`low`'uyla kontrol edilir. Sadakat iddia değil, testle sabittir —
`tests/test_engine_per_bar.py::test_catch_up_matches_running_each_bar_in_its_own_round`
bir turda telafi edilen N barın, N ayrı turda koşulan N bar ile BİREBİR aynı defteri
ürettiğini gösterir.

Harness'ın yaptığı üç şey vardır ve hiçbiri çekirdeğe dokunmaz:

1. **Ayrı defter kökü** (`backtests/<koşu-id>/`). Gerçek defter hiçbir koşulda açılmaz —
   okunmaz da: bir backtest denetim izine (kural 1) yazmaz.
2. **`last_processed_bar` tohumlama.** Boş defterde `core/engine.py::_timeline` yalnızca
   son barı işler (`run.last_bar is None -> index[-1:]`), çünkü canlıda yeni açılan bir
   modelin geçmişi geriye dönük işlemesi yalnızca boş özsermaye satırı üretirdi. Backtest
   tam olarak o geçmişi istediği için başlangıç barını kendisi yazar.
3. **`signals_per_bar: true`.** Kapalıyken sinyal yalnızca `as_of` barında üretilir, yani
   tüm pencere tek bir sinyal verirdi. Base katmanı canlıda kapalı koşar; bu, bilinçli
   kabul edilmiş bir sapmadır (docs/backtest.md > 5a) ve koşu çıktısında AÇIKÇA yazılır.

Model kurulumu `main.py::build_strategies`ten gelir, burada yeniden yazılmaz: kurulum iki
yerde ayrışırsa backtest, canlıda koşandan başka bir model kümesini ölçmeye başlar.

Maliyet, dolum, likidasyon, funding ve metrik tanımlarının hiçbiri burada YOKTUR; hepsi
katmanın çözülmüş config'inden ve `core/`den gelir. Backtest'e özel bir sabit eklemek,
backtest'in kendi uydurduğu bir dünyayı ölçmesi demek olurdu.

## Kapı 0 (`--verify-live`)

Hiçbir backtest sayısı, harness canlı veriye karşı doğrulanmadan okunmaz. Yer gerçeği
canlı turların `round.models[].emitted` kaydıdır: her barda her modelin tam olarak hangi
sinyali ürettiği. Harness aynı pencerede koşturulur ve sinyaller karşılaştırılır.

- **Uyarlanabilir OLMAYAN modeller birebir eşleşmeli.** Sinyalleri piyasa verisinin ve
  sabit tohumun saf fonksiyonudur: `ScalpModel._round_rng` her barı `random_seed`, `as_of`
  ve model kimliğiyle yeniden tohumlar, yani çekiliş durum TAŞIMAZ; `vwap_managed`de
  rastgelelik hiç yoktur.
- **Uyarlanabilir modellerden eşleşme BEKLENMEZ** (`scalp_bandit`, `vwap_clone`): ikisi de
  kendi kapanmış işlemlerinden öğrenir (kural 16) ve boş defterden başlayan bir koşu farklı
  bir geçmiş görür. Rapor edilir, kapı sayılmaz.

Karşılaştırma penceresi config'in DEĞİŞMEDİĞİ bir aralık olmalıdır: `fee_rate` (karar 25)
ve `vwap.managed.atr_multiple` (karar 26) 15 Eylül'de değişti, öncesi ile sonrası aynı
kurallarla koşmadı.
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd  # noqa: E402

from core.config import get_setting, load_config  # noqa: E402
from core.data import bar_duration, load_market_data  # noqa: E402
from core.engine import Engine, RoundReport  # noqa: E402
from core.layers import DEFAULT_LAYER, Layer, resolve_layer  # noqa: E402
from core.ledger import Ledger  # noqa: E402
from core.metrics import (  # noqa: E402
    AcceptanceFlags, ModelMetrics, acceptance_flags, buy_hold_return, compare,
    format_report, holding_stats, r_series,
)
from core.portfolio import Portfolio  # noqa: E402
from core.report import model_breakdowns  # noqa: E402
from main import build_strategies, jsonable  # noqa: E402
from strategies.base import MarketData, Signal, Strategy  # noqa: E402

logger = logging.getLogger("backtest")

BACKTEST_ROOT = Path("backtests")
MANIFEST_FILENAME = "manifest.json"

# Config parmak izine giren anahtarlar: koşunun hangi kurallarla yapıldığını sonradan
# okuyabilmek için (docs/backtest.md > 9). Ölçümün anlamını belirleyen her sabit burada
# olmalı — eksik bir anahtar, iki backtest'in neden ayrıştığını açıklanamaz kılar.
_FINGERPRINT_KEYS: tuple[str, ...] = (
    "initial_capital", "risk_per_trade", "leverage_cap", "max_positions",
    "max_short_positions", "fee_rate", "slippage_base", "slippage_short_stop",
    "maintenance_margin", "max_stop_atr_multiple", "timeframe", "random_seed",
    "signals_per_bar",
)

# Kendi kapanmış işlemlerinden öğrenen modeller (kural 16). Kapı 0'da bunlardan birebir
# eşleşme BEKLENMEZ: boş defterden başlayan bir koşu farklı bir geçmiş görür. Liste
# `Strategy.observe_closed_trades`ın uygulanıp uygulanmadığından TÜRETİLİR, elle
# yazılmaz — elle yazılan bir liste, yeni bir uyarlanabilir model eklendiği gün sessizce
# yanlış olurdu ve Kapı 0 o modelden haksız yere eşleşme beklerdi.
def is_adaptive(strategy: Strategy) -> bool:
    return type(strategy).observe_closed_trades is not Strategy.observe_closed_trades


@dataclass(frozen=True, kw_only=True)
class BacktestResult:
    layer: str
    start: pd.Timestamp
    end: pd.Timestamp
    out_dir: Path
    report: RoundReport
    metrics: tuple[ModelMetrics, ...]
    build_failures: Mapping[str, str]
    # Katmanın kendi kırılımları (`layers.<ad>.breakdowns`). Tablo "hangi model önde" der;
    # kırılım "neden" der — ve bir backtest'in cevaplaması istenen soru tam olarak odur.
    # Tabloyu görüp kırılımı görmemek, ortalama R'nin ARDINDAKİ mekanizmayı (hangi çıkış
    # kuralı kaç kez tetikledi, hangi kol/sembol taşıdı) çıkarıma bırakırdı.
    breakdowns: Mapping[str, Any] = field(default_factory=dict)
    # Örneklem kapısı (B-1) tabloyu da böler: kapıyı geçmeyen model sıralanmaz
    # (bkz. core/metrics.py::format_report). Koşunun config'inden okunur ki backtest ile
    # canlı tablo aynı çıtayı göstersin.
    min_trades: int = 0
    # Tutuş süresi dağılımı (model -> HoldingStats alanları). Zaman stop'u olmayan bir
    # modelde OOS embargosu (docs/backtest.md > 6.1) VARSAYILAMAZ: "doğru boşluk azami
    # tutuş süresidir" kuralının dayandığı üst sınır orada tanım gereği yoktur ve
    # buradan ÖLÇÜLÜR.
    holding: Mapping[str, Any] = field(default_factory=dict)
    # Kabul çıtası (CLAUDE.md > Kabul Çıtası), canlıyla AYNI fonksiyondan. Backtest bu
    # kapıları zaten `docs/backtest.md > 4`te soruyordu (C-1..C-4) ama cevabı göz kararı
    # okunuyordu: ortalama R'ye bak, kontrolle farkı zihinden çıkar, bandı tahmin et.
    # Göz kararı bir kapı değildir — hesap tek yerde, canlıyla aynı kodda durmalı.
    acceptance: tuple[AcceptanceFlags, ...] = ()
    # Sembol başına al-tut getirisi (yüzde), pencerenin kendisinden. Bir sembolün
    # ortalama R'sini, o sembolün o pencerede ne yaptığını bilmeden okumak yanıltıcıdır:
    # çöken bir sembolde long-only bir modelin kaybetmesi bir sinyal kusuru değildir.
    buy_hold: Mapping[str, float] = field(default_factory=dict)
    # Sembol başına VERİ KAPSAMI: pencerenin içinde o sembolün kaç barı var ve hangi
    # tarihte başlıyor. Sabit bir evren listesi, sembollerin o pencerede GERÇEKTEN
    # işlem görebildiği anlamına gelmez — OKX'te 2022-01'de 13 sembolün yalnızca 9'u
    # vardı. Kapsam yazılmazsa, iki yıllık veriye dayanan bir kâr faktörü ile iki
    # aylık veriye dayanan biri tabloda aynı görünür.
    coverage: Mapping[str, Any] = field(default_factory=dict)
    # Koşunun canlıdan sapan varsayımları; raporun başına basılır ki bir sayı, hangi
    # dünyada ölçüldüğü bilinmeden okunmasın.
    deviations: Mapping[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Koşu
# --------------------------------------------------------------------------- #
def run_backtest(
    *,
    layer_name: str,
    start: pd.Timestamp,
    end: pd.Timestamp,
    out_dir: Path,
    models: Sequence[str] | None = None,
    config_path: str | None = None,
    history_bars: int | None = None,
    embargo_bars: int | None = None,
    symbols: Sequence[str] | None = None,
    fee_rate: float | None = None,
    slippage_base: float | None = None,
    funding_periods: int | None = None,
    signal_cutoff: pd.Timestamp | None = None,
) -> BacktestResult:
    """Katmanı `start`..`end` penceresinde koşturur ve ayrı bir deftere yazar.

    `start` TOHUMLANAN bardır: motor ondan SONRAKİ ilk bardan başlar (`_timeline`
    `ts > last_bar` süzer). Yani pencere yarı açıktır — `start` işlenmez, `end` işlenir.

    `symbols` katmanın evrenini DARALTIR (genişletemez): dış bir referansla (ör. tek
    sembollü bir TradingView koşusu) kıyas ancak tek sembollü bir koşuyla kurulabilir,
    çünkü portföy kotası (`max_positions`) çok sembollü bir koşuda sinyal REDDEDER ve
    aradaki fark modelin değil kotanın ölçüsü olur. Genişletememesi kural 6'dır: evren
    katmanın tanımıdır, harness onu büyütemez.

    `fee_rate` / `slippage_base` maliyet varsayımını değiştirir ve bu bir ÖLÇÜM KURALI
    değişikliğidir — `--history-bars`ın aksine sonucun kendisini kaydırır. Bu yüzden
    yalnızca dış bir referansla parite kurmak için vardır, gürültüyle bağırır
    (`logger.warning`) ve `manifest.json`ın config parmak izine düşer. Canlı `config.yaml`
    hiçbir koşulda değişmez (kural 6 + karar 25: `fee_rate` defteri tarihli olarak böler).

    `funding_periods` funding geçmişinin derinliğidir (`data.funding_history_periods`).
    Varsayılan 180 periyot ≈ 60 gündür; yıllara uzanan bir pencerede kaydı olmayan anda
    `core/funding.py::rate_at` None döner ve funding HİÇ işlenmez (uydurma yok) — yani
    eski dönem sistematik olarak İYİMSER çıkar. Derinleştirmek bunu kapatır; borsanın
    kendi sınırı `manifest.json`daki kapsama ile birlikte okunur.

    `signal_cutoff` bu bardan SONRA yeni sinyal üretilmesini durdurur; barlar işlenmeye
    devam eder (stop/TP/likidasyon/funding). Dönem atamasının karşılığıdır: bir işlem
    GİRİŞ tarihine göre döneme aittir ve dönemin son kurulumları sınırı aşsa bile
    kapanışına kadar o döneme sayılır. Kesim olmadan alternatif, sınırda açık olan
    pozisyonları düşürmekti — bu, dönemin en uzun yaşayan kurulumlarını sistematik olarak
    eleyip ortalamayı kısa işlemlere doğru çekerdi. Uygulaması modelin ÖRNEĞİNİ gölgeler
    (sınıfını değil): `type(strategy).observe_closed_trades` ile kurulan uyarlanabilirlik
    tespiti (Kapı 0) bozulmasın diye.

    `embargo_bars` bir OOS penceresinin başına konan boşluktur (docs/backtest.md > 6):
    parametresi `start`e kadarki veriyle seçilmiş bir model için, `start`ten hemen sonra
    başlamak temiz değildir — IS penceresinin SON kurulumları `start`ten sonraki barlarda
    çözülür, yani o barlar IS etiketlerinin sonucuna katkı yapmıştır. Doğru boşluk tam
    olarak azami tutuş süresidir (`scalp.time_stop_bars`), çünkü hiçbir pozisyon ondan
    uzun yaşamaz. Verilirse işlenen pencere o kadar bar İLERİ kaydırılır ve `manifest.json`
    hem istenen hem uygulanan başlangıcı yazar — boşluk sessiz olamaz.
    """
    if end <= start:
        raise ValueError(f"pencere boş: start={start} >= end={end}")

    layer = resolve_layer(load_config(config_path), layer_name)
    config = dict(layer.config)

    requested_start = start
    if embargo_bars:
        if embargo_bars < 0:
            raise ValueError(f"--embargo-bars negatif olamaz: {embargo_bars}")
        start = start + embargo_bars * bar_duration(str(get_setting(config, "timeframe")))
        if end <= start:
            raise ValueError(
                f"embargo penceriyi tüketti: {embargo_bars} bar sonrası start={start} >= end={end}"
            )
        logger.info(
            "embargo: %d bar -> pencere başı %s yerine %s (IS kurulumlarının çözüldüğü "
            "barlar OOS'a girmesin diye)", embargo_bars, requested_start, start,
        )
    # Bilinçli sapma (docs/backtest.md > 5a): kapalıyken tüm pencere TEK sinyal üretirdi.
    signals_per_bar_was = bool(config.get("signals_per_bar"))
    config["signals_per_bar"] = True

    # Derinlik override'ı (docs/backtest.md > 5b): `data.history_bars` bir ÖLÇÜM kuralı
    # değil, anlık görüntünün ne kadar geriye gittiğidir — maliyet, risk, dolum ve metrik
    # tanımlarına dokunmaz. Backtest'e özel olmasının sebebi canlının ondan FAYDALANMAMASI:
    # modellerin lookback'leri sınırlıdır (`tail(300)`, `rolling(20)`, EMA50, gün-çapalı
    # VWAP), yani daha derin geçmiş canlı sinyalini değiştirmez — ama `data/` depoya
    # girmediği için her saatlik tur veriyi baştan indirir ve global bir artış, faydasız
    # yere her turda 4× indirme demekti.
    #
    # "Değiştirmez" bir varsayım değil, SINANAN bir iddiadır: aynı override ile koşulan
    # Kapı 0 canlı kayıtla birebir eşleşmeye devam etmelidir. Eşleşmezse override bir
    # ölçüm sapması üretiyor demektir ve kullanılamaz.
    history_bars_was = int(get_setting(config, "data.history_bars"))
    if history_bars is not None:
        if history_bars < history_bars_was:
            raise ValueError(
                f"--history-bars yalnızca DERİNLEŞTİRİR: {history_bars} < {history_bars_was}. "
                "Sığlaştırmak modelin canlıda gördüğünden AZ veri görmesi demekti."
            )
        config = {**config, "data": {**config["data"], "history_bars": history_bars}}
        logger.info("derinlik override: history_bars %d -> %d", history_bars_was, history_bars)

    # Maliyet override'ı: `--history-bars`tan FARKLI bir şeydir ve farkı gizlenmez.
    # O, anlık görüntünün derinliğini değiştirir ve sonucu değiştirmemesi SINANIR (Kapı 0);
    # bu, doğrudan sonucun kendisini kaydırır. Bu yüzden yalnızca dış bir referansla parite
    # için vardır ve her koşuda bağırır.
    if fee_rate is not None or slippage_base is not None:
        if fee_rate is not None and fee_rate < 0.0:
            raise ValueError(f"--fee-rate negatif olamaz: {fee_rate}")
        if slippage_base is not None and slippage_base < 0.0:
            raise ValueError(f"--slippage-base negatif olamaz: {slippage_base}")
        live_fee = float(get_setting(config, "fee_rate"))
        live_slip = float(get_setting(config, "slippage_base"))
        config = {
            **config,
            "fee_rate": live_fee if fee_rate is None else float(fee_rate),
            "slippage_base": live_slip if slippage_base is None else float(slippage_base),
        }
        logger.warning(
            "MALİYET OVERRIDE: bu koşu CANLI MALİYETLE KOŞMUYOR — "
            "fee_rate %.5f -> %.5f, slippage_base %.5f -> %.5f (dolum başına %.5f -> %.5f). "
            "Sonuçlar canlı defterle aynı varsayımı taşımaz.",
            live_fee, config["fee_rate"], live_slip, config["slippage_base"],
            live_fee + live_slip, config["fee_rate"] + config["slippage_base"],
        )

    if funding_periods is not None:
        was = int(get_setting(config, "data.funding_history_periods"))
        if funding_periods < was:
            raise ValueError(
                f"--funding-periods yalnızca DERİNLEŞTİRİR: {funding_periods} < {was}. "
                "Sığlaştırmak, funding'i canlıda ödenenden AZ göstermek demekti."
            )
        config = {**config, "data": {**config["data"], "funding_history_periods": funding_periods}}
        logger.info("funding derinliği: %d -> %d periyot", was, funding_periods)

    names = list(models) if models is not None else layer.models
    if not names:
        raise ValueError(f"{layer.name} katmanında model yok")

    deviations: dict[str, Any] = {
        "signals_per_bar": {"live": signals_per_bar_was, "run": True},
        "history_bars": {
            "config": history_bars_was, "used": int(get_setting(config, "data.history_bars"))
        },
    }
    if fee_rate is not None or slippage_base is not None:
        deviations["costs"] = {
            "live_fee_rate": float(load_config(config_path)["fee_rate"]),
            "live_slippage_base": float(load_config(config_path)["slippage_base"]),
            "run_fee_rate": float(get_setting(config, "fee_rate")),
            "run_slippage_base": float(get_setting(config, "slippage_base")),
        }
    if funding_periods is not None:
        deviations["funding_history_periods"] = int(
            get_setting(config, "data.funding_history_periods")
        )
    if signal_cutoff is not None:
        deviations["signal_cutoff"] = signal_cutoff.isoformat()

    universe = list(layer.symbols) if layer.symbols is not None else None
    if symbols is not None:
        requested = list(symbols)
        if not requested:
            raise ValueError("--symbols boş olamaz")
        if universe is not None:
            outside = [symbol for symbol in requested if symbol not in universe]
            if outside:
                raise ValueError(
                    f"--symbols katmanın evrenini GENİŞLETEMEZ, yalnızca daraltır: {outside} "
                    f"{layer.name} evreninde yok (kural 6: evren katmanın tanımıdır)"
                )
        universe = requested
        logger.info("evren daraltıldı: %d sembol (%s)", len(universe), ", ".join(universe))

    strategies, build_failures = build_strategies(names, config)
    if not strategies:
        raise RuntimeError("hiçbir model kurulamadı")

    if signal_cutoff is not None:
        if signal_cutoff <= start:
            raise ValueError(f"--signal-cutoff pencerenin içinde olmalı: {signal_cutoff} <= {start}")
        for strategy in strategies:
            _silence_signals_after(strategy, signal_cutoff)
        logger.info(
            "sinyal kesimi: %s sonrası YENİ sinyal yok; barlar pozisyon yönetimi için "
            "işlenmeye devam eder (dönem ataması giriş tarihine göredir)", signal_cutoff,
        )

    out_dir.mkdir(parents=True, exist_ok=True)
    ledger = Ledger(out_dir / "ledger")
    initial_capital = float(config["initial_capital"])
    for strategy in strategies:
        ledger.initialize_model(strategy.name, initial_capital=initial_capital)
        _seed_start_bar(ledger, strategy.name, start=start)

    # Anlık görüntü `end`e kadar kesilir: `now` verildiğinde core/data.py hem çıpayı hem
    # tüm serileri oraya kadar budar, yani model geleceği GÖREMEZ (kural 12). Backtest'in
    # look-ahead güvencesi burada başlar ve motorun bar bazlı dilimlemesiyle sürer.
    # `now` GELECEKTE olamaz. `load_market_data` bunu "şimdi" sayar ve çıpanın tazeliğini
    # ona göre ölçer (`data.max_staleness_bars`); gelecek bir `end` ile BTC'nin son kapanmış
    # barı zorunlu olarak "bayat" çıkar ve anlık görüntü hiç üretilmez. Oysa istenen şey
    # "eldeki en taze bara kadar koş"tur ve onun karşılığı zaten aşağıdaki uyarıdır.
    # Kısaltma SESSİZ değildir: burada da, `market.as_of < end` dalında da söylenir.
    now = pd.Timestamp.now(tz="UTC")
    if end > now:
        logger.warning("istenen bitiş (%s) gelecekte; anlık görüntü şimdiye (%s) kadar kurulur", end, now)
    market = load_market_data(config, symbols=universe, now=min(end, now))
    logger.info(
        "katman=%s pencere=(%s, %s] as_of=%s sembol=%d model=%d",
        layer.name, start, end, market.as_of, len(market.ohlcv), len(strategies),
    )
    if market.as_of < end:
        logger.warning(
            "anlık görüntünün son barı (%s) istenen bitişten (%s) geride: pencere kısaldı",
            market.as_of, end,
        )

    report = Engine(
        strategies, config=config, ledger=ledger, portfolio=Portfolio(config)
    ).run_round(market)

    metrics = compare(
        [strategy.name for strategy in strategies],
        ledger=ledger,
        config=config,
        benchmarks=[s.name for s in strategies if s.is_benchmark],
        replicas=[s.name for s in strategies if s.is_replica],
        # Canlıdaki çıpanın AYNISI (main.py): harness kendi referansını seçemez,
        # yoksa aynı defter iki farklı piyasa kontrolüyle okunurdu.
        reference=market.btc.get("close"),
    )

    _write_manifest(
        out_dir,
        layer=layer,
        config=config,
        start=start,
        end=market.as_of,
        report=report,
        strategies=strategies,
        build_failures=build_failures,
        signals_per_bar_was=signals_per_bar_was,
        embargo=(requested_start, int(embargo_bars or 0)),
        history_bars=(history_bars_was, int(get_setting(config, "data.history_bars"))),
        deviations=deviations,
        universe=universe if universe is not None else sorted(market.ohlcv),
    )
    # Kırılımlar `core/report.py`den OKUNUR, burada yeniden yazılmaz: kırılımın tanımı
    # (kol etiketi, çıkış kuralı birleşimi, seans sınırları, kayıp serisi kesimi) ölçümün
    # parçasıdır ve ikinci bir kopya, backtest ile dashboard'un sessizce ayrışması demekti.
    trades = {s.name: ledger.read_trades(s.name) for s in strategies}
    breakdowns = model_breakdowns(trades, kinds=layer.breakdowns)
    step = bar_duration(str(get_setting(config, "timeframe")))
    holding = {
        name: asdict(holding_stats(rows, bar_duration=step)) for name, rows in trades.items()
    }
    # Al-tut çıpası SEMBOL başınadır ve pencerenin kendisinden okunur; `buyhold` modeli
    # (kural 15) hesap düzeyinde tek bir sayı verir ve "bu sembol ne yaptı" sorusuna
    # cevap taşımaz.
    buy_hold = {
        symbol: buy_hold_return(frame["close"], start=start, end=market.as_of)
        for symbol, frame in sorted(market.ohlcv.items())
    }
    # Kapsam PENCERENİN İÇİNDE ölçülür (`start` dışlanır, kural: `start` tohumlanan bardır
    # ve işlenmez): anlık görüntü `history_bars` kadar daha geriye gider ama o barlar
    # gösterge ısınması içindir, ölçümün penceresi değil.
    coverage = {}
    for symbol, frame in sorted(market.ohlcv.items()):
        window = frame.loc[(frame.index > start) & (frame.index <= market.as_of)]
        coverage[symbol] = {
            "bars": int(len(window)),
            "first_bar": window.index[0].isoformat() if len(window) else None,
            "last_bar": window.index[-1].isoformat() if len(window) else None,
        }

    # Kontrol modeli yarışmacı listesinde olmasa bile örnekleme girer: farkın öteki
    # tarafı odur (core/report.py ile aynı sözleşme).
    flags = acceptance_flags(
        metrics,
        min_trades=int(get_setting(config, "acceptance.min_trades")),
        stop_band_ratio=float(get_setting(config, "acceptance.stop_band_ratio")),
        control_model=str(get_setting(config, "acceptance.control_model")),
        edge_margin_r=float(get_setting(config, "acceptance.edge_margin_r")),
        control_min_trades=int(get_setting(config, "acceptance.control_min_trades")),
        r_samples={name: r_series(rows) for name, rows in trades.items()},
        ci_alpha=float(get_setting(config, "acceptance.edge_ci_alpha")),
        bootstrap_samples=int(get_setting(config, "acceptance.bootstrap_samples")),
        seed=int(get_setting(config, "random_seed")),
    )

    return BacktestResult(
        layer=layer.name, start=start, end=market.as_of, out_dir=out_dir,
        report=report, metrics=tuple(metrics), build_failures=build_failures,
        breakdowns=breakdowns,
        min_trades=int(get_setting(config, "acceptance.min_trades")),
        holding=holding,
        buy_hold=buy_hold,
        coverage=coverage,
        deviations=deviations,
        acceptance=tuple(flags),
    )


def _silence_signals_after(strategy: Strategy, cutoff: pd.Timestamp) -> None:
    """`cutoff`tan sonraki barlarda modelin YENİ sinyal üretmesini durdurur.

    Gölgeleme ÖRNEK düzeyindedir, sınıf düzeyinde değil: Kapı 0'ın uyarlanabilirlik
    tespiti `type(strategy).observe_closed_trades`a bakar (docs/backtest.md > 1) ve
    modeli bir sarmalayıcı sınıfa koymak o tespiti bozardı — harness, ölçtüğü modelin
    kimliğini değiştiremez.

    Pozisyon yönetimi (`manage_positions`) DOKUNULMAZ: kesimin anlamı "yeni kurulum
    alma", "açık pozisyonu dondur" değil. Dondurmak, dönemin son kurulumlarını kendi
    çıkış kurallarından mahrum bırakıp sonucu uydururdu.
    """
    original = strategy.generate_signals

    def gated(
        market: MarketData, peer_signals: Mapping[str, tuple[Signal, ...]] | None = None
    ) -> list[Signal]:
        if market.as_of > cutoff:
            return []
        return original(market, peer_signals)

    strategy.generate_signals = gated  # type: ignore[method-assign]


def _seed_start_bar(ledger: Ledger, model: str, *, start: pd.Timestamp) -> None:
    """`last_processed_bar`ı pencere başına yazar.

    Bu olmadan motor yalnızca son barı işler: boş defterli bir model için `_timeline`
    bilinçli olarak `index[-1:]` döner (canlıda geçmişi geriye dönük işlemek yalnızca boş
    özsermaye satırı üretirdi). Backtest tam olarak o geçmişi istediği için barı KENDİSİ
    yazar — motorun kuralını değiştirmeden, ona canlıdakiyle aynı girdiyi vererek.
    """
    state = ledger.load_state(model)
    if state is None:
        raise RuntimeError(f"{model}: defter durumu kurulamadı")
    state["last_processed_bar"] = start.isoformat()
    ledger.write_state(model, state)


# --------------------------------------------------------------------------- #
# Denetim izi: koşunun kendisi tekrar üretilebilir olmalı (docs/backtest.md > 9)
# --------------------------------------------------------------------------- #
def _write_manifest(
    out_dir: Path,
    *,
    layer: Layer,
    config: Mapping[str, Any],
    start: pd.Timestamp,
    end: pd.Timestamp,
    report: RoundReport,
    strategies: Sequence[Strategy],
    build_failures: Mapping[str, str],
    signals_per_bar_was: bool,
    history_bars: tuple[int, int],
    embargo: tuple[pd.Timestamp, int] = (pd.NaT, 0),
    deviations: Mapping[str, Any] | None = None,
    universe: Sequence[str] | None = None,
) -> None:
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "harness_sha": _git_sha(),
        "layer": layer.name,
        "window": {
            "start": start.isoformat(),
            "end": end.isoformat(),
            # Embargo sessiz olamaz: istenen pencere ile İŞLENEN pencere farklıysa,
            # sonucu okuyan "hangi barlardan itibaren ölçüldü" sorusunu manifest'ten
            # cevaplayabilmelidir (docs/backtest.md > 6 ve 9).
            "requested_start": (
                None if embargo[1] == 0 else pd.Timestamp(embargo[0]).isoformat()
            ),
            "embargo_bars": embargo[1],
        },
        # Sapmalar koşunun kendi kaydında durur: sonradan "hangi ayarla koşmuştu" diye
        # sorulduğunda cevap log'da değil, manifest'te olmalı (docs/backtest.md > 9).
        "history_bars": {"config": history_bars[0], "used": history_bars[1]},
        # Canlıdan sapan HER varsayım tek bir yerde: maliyet override'ı, funding
        # derinliği, sinyal kesimi. Maliyet sapması ayrıca `config_fingerprint`e de
        # düşer (fee_rate/slippage_base orada), ama burada niyetiyle birlikte durur:
        # parmak izi "hangi değerle koştu" der, bu "canlıdan farklı mı" der.
        "deviations": dict(deviations or {}),
        "universe": {"layer": list(layer.symbols or ()), "used": list(universe or ())},
        "models": [s.name for s in strategies],
        "adaptive_models": [s.name for s in strategies if is_adaptive(s)],
        "build_failures": dict(build_failures),
        "config_fingerprint": {key: config.get(key) for key in _FINGERPRINT_KEYS},
        # Sapma gizlenmez: base katmanı canlıda signals_per_bar=false koşar ve backtest
        # onu açar, yani canlıdan ÇOK işlem yapar (docs/backtest.md > 5a).
        "signals_per_bar_forced": not signals_per_bar_was,
        # Geçerlilik kapısı B-2: ikisi de "o barda stop/TP/likidasyon hiç sorulmadı" demek.
        "validity": {
            model.model: {
                "bars_processed": model.bars_processed,
                "missing_bars": model.missing_bars,
                "unchecked_position_bars": model.unchecked_position_bars,
                "signals": model.signals,
                "filled": model.filled,
            }
            for model in report.models
        },
        # Kural 13'ün mum içi sıralama varsayımının ne sıklıkta bağladığı. Geçerlilik
        # kapısı DEĞİL (varsayım muhafazakârdır ve doğru taraftadır), ama sonucu okuyan
        # payı bilmelidir.
        "fill_ambiguity": {
            model.model: {
                "stop_exits": model.stop_exits,
                "ambiguous_stop_exits": model.ambiguous_stop_exits,
            }
            for model in report.models
            if model.stop_exits
        },
    }
    (out_dir / MANIFEST_FILENAME).write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _git_sha() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10
        )
    except Exception:  # noqa: BLE001 — SHA bir kolaylıktır, koşuyu düşüremez
        return "unknown"
    return result.stdout.strip() if result.returncode == 0 else "unknown"


def check_validity(report: RoundReport) -> dict[str, list[str]]:
    """Geçerlilik kapıları B-2 (docs/backtest.md > 3): model -> ihlal listesi.

    B-1 (n >= 30) burada DEĞİL: örneklem kapısı metriklerden okunur ve `acceptance_flags`
    zaten onu canlıyla aynı eşikle uygular. Burada yalnızca backtest'e özgü olan, yani
    pencerenin kendisinin sağlam olup olmadığı sorulur.
    """
    violations: dict[str, list[str]] = {}
    for model in report.models:
        reasons = []
        if model.missing_bars:
            reasons.append(f"missing_bars={model.missing_bars}")
        if model.unchecked_position_bars:
            reasons.append(f"unchecked_position_bars={model.unchecked_position_bars}")
        if reasons:
            violations[model.model] = reasons
    return violations


# --------------------------------------------------------------------------- #
# Kapı 0: harness canlı veriye karşı doğrulanır
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, kw_only=True)
class SignalKey:
    """Karşılaştırmanın birimi: hangi model, hangi barda, hangi sembolde, hangi yönde.

    Fiyat alanları (stop/hedef) KASTEN dışarıda: kayan nokta eşitliği kırılgandır ve
    sorulan soru "aynı kurulumu buldu mu", "ondalık basamağına kadar aynı mı" değil.
    Fiyat farkı varsa zaten sembol/yön/bar üçlüsü tutmazdı.
    """
    model: str
    bar: str
    symbol: str
    direction: str


def bar_key(value: Any) -> str:
    """Barın karşılaştırmada kullanılan TEK metin biçimi: UTC ISO-8601.

    İki taraf aynı anı iki farklı biçimde yazdığı an kapı anlamını yitirir — ve sessizce
    yitirir: `str(pd.Timestamp)` `"... 15:15:00+00:00"`, `isoformat()` ise
    `"...T15:15:00+00:00"` üretir, yani birebir aynı sinyal kümesi sıfır kesişimle
    "AYRIŞTI" görünür. Kapı 0'ın işi harness'ı denetlemek; kendi biçimlendirme farkını
    bir sadakat hatası diye raporlaması, denetimin kendisini bozar.

    Ayrıştırılamayan bir değer ATILMAZ, ham metniyle durur: düşürmek o sinyali
    karşılaştırmadan sessizce çıkarır ve eksik bir küme eşleşme gibi görünebilirdi.
    """
    stamp = _stamp(value)
    return stamp.isoformat() if stamp is not None else str(value)


def emitted_keys(report: RoundReport) -> set[SignalKey]:
    return {
        SignalKey(
            model=model.model,
            bar=bar_key(signal.bar),
            symbol=str(signal.symbol),
            direction=str(signal.direction),
        )
        for model in report.models
        for signal in model.emitted
    }


def live_emitted_keys(
    metrics_path: str, *, start: pd.Timestamp, end: pd.Timestamp
) -> set[SignalKey]:
    """Canlı turların `emitted` kayıtları, git geçmişinden toplanır.

    Neden git: `docs/data/metrics_*.json` her turda ÜZERİNE yazılır, yani dosyanın son hâli
    yalnızca son turu taşır. Canlının bar bar ne ürettiği ancak commit geçmişinde durur —
    ve orası zaten değiştirilemez bir kayıttır, tam da bir yer gerçeğinden istenen şey.
    """
    keys: set[SignalKey] = set()
    for sha in _git_log_shas(metrics_path):
        blob = _git_show(f"{sha}:{metrics_path}")
        if not blob:
            continue
        try:
            payload = json.loads(blob)
        except json.JSONDecodeError:
            continue
        keys |= payload_keys(payload, start=start, end=end)
    return keys


def payload_keys(
    payload: Mapping[str, Any], *, start: pd.Timestamp, end: pd.Timestamp
) -> set[SignalKey]:
    """Tek bir `metrics_*.json` yükünün `emitted` kayıtları -> anahtar kümesi.

    Git yürüyüşünden AYRI durur ki anahtarın kurulumu depo geçmişi olmadan test
    edilebilsin: kapıyı düşüren ilk hata tam olarak buradaydı ve iki tarafı elle aynı
    biçimde yazan bir test onu göremezdi.
    """
    keys: set[SignalKey] = set()
    for model in (payload.get("round") or {}).get("models") or ():
        for signal in model.get("emitted") or ():
            bar = _stamp(signal.get("bar"))
            if bar is None or not (start < bar <= end):
                continue
            keys.add(
                SignalKey(
                    model=str(signal.get("model") or model.get("model")),
                    bar=bar_key(bar),
                    symbol=str(signal.get("symbol")),
                    direction=str(signal.get("direction")),
                )
            )
    return keys


def compare_signals(
    backtest: set[SignalKey], live: set[SignalKey], *, adaptive: Iterable[str]
) -> dict[str, dict[str, Any]]:
    """Model bazında eşleşme. Uyarlanabilir modeller `gate=False` ile işaretlenir."""
    adaptive_names = set(adaptive)
    models = {key.model for key in backtest} | {key.model for key in live}
    result: dict[str, dict[str, Any]] = {}
    for name in sorted(models):
        mine = {key for key in backtest if key.model == name}
        theirs = {key for key in live if key.model == name}
        result[name] = {
            "gate": name not in adaptive_names,
            "backtest": len(mine),
            "live": len(theirs),
            "both": len(mine & theirs),
            "only_backtest": sorted(f"{k.bar} {k.symbol} {k.direction}" for k in mine - theirs),
            "only_live": sorted(f"{k.bar} {k.symbol} {k.direction}" for k in theirs - mine),
            "match": mine == theirs,
        }
    return result


def format_drift(metrics: Sequence[ModelMetrics]) -> str:
    """Brüt sürüklenme% — R ölçeğinden BAĞIMSIZ tek karşılaştırma birimi (karar 35).

        brüt sürüklenme% = (ort.R + cost_per_r) × avg_stop_distance_pct

    Neden gerekli: `cost_per_r = maliyet% / stop%` özdeşliği yüzünden stop'u genişletmek
    maliyet/R'yi düşürür AMA R cinsinden brüt edge'i aynı oranda düşürür — yani **stop
    genişliği net R'nin İŞARETİNİ değiştiremez** ve iki modelin ort. R'sini kıyaslamak,
    farklı stop ölçeklerinde farklı şeyleri kıyaslamak olur. Yüzde cinsinden sürüklenme
    o ölçekten bağımsızdır ve maliyet duvarıyla (~%0.26/tur) doğrudan kıyaslanabilir.

    Burada TÜRETİLİR, `core/metrics.py`ye eklenmez: ölçümün kendisi değil, ölçümün
    okunma birimidir ve yalnızca backtest kararlarında kullanılır. Çıpa ve kopya
    satırları `nan` taşır (kural 15/15b), o yüzden `—` görünür.
    """
    if not metrics:
        return ""
    out = ["\nBRÜT SÜRÜKLENME (karar 35) — R ölçeğinden bağımsız birim",
           "-" * 78,
           f"{'model':18s}{'ort.R':>8}{'maliyet/R':>11}{'stopMes.%':>11}"
           f"{'brüt%':>9}{'maliyet%':>10}"]
    for item in metrics:
        t = item.total
        r, cost, stop = _num(t.avg_r), _num(t.cost_per_r), _num(t.avg_stop_distance_pct)
        if r is None or cost is None or stop is None:
            out.append(f"{item.model:18s}{_cell(t.avg_r):>8}{_cell(t.cost_per_r):>11}"
                       f"{_cell(t.avg_stop_distance_pct):>11}{'—':>9}{'—':>10}")
            continue
        out.append(f"{item.model:18s}{r:8.2f}{cost:11.3f}{stop:11.2f}"
                   f"{(r + cost) * stop:9.3f}{cost * stop:10.3f}")
    return "\n".join(out) + "\n"


def _num(value: Any) -> float | None:
    """`nan` ve None aynı şeyi söyler burada: bu satır için sayı YOK."""
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if number != number else number


def format_fill_ambiguity(report: RoundReport) -> str:
    """Kural 13'ün mum içi sıralama varsayımı ne sıklıkta BAĞLADI?

    Aynı mumda hem stop hem hedef aralığa giriyorsa mum içi sıra bilinemez ve kural 13
    **kötü olanın (stop) gerçekleştiğini varsayar**. Varsayım muhafazakârdır ve doğru
    taraftadır — iyimser olan, elde olmayan bir bilgiyle kâr yazmak olurdu — ama BEDELİ
    bugüne kadar hiç ölçülmedi: "modeller kaybediyor" sonucunun ne kadarı sinyalden, ne
    kadarı bu varsayımdan geliyor bilinmiyordu.

    Sayı buradan okunur çünkü canlı tur raporu yalnızca O TURUN stop'larını sayar; bir
    backtest ise pencerenin tamamını tek turda işler, yani kümülatif cevabı verir.

    **Bu bir duyarlılık ANALİZİ DEĞİL, onun tetikleyicisidir.** Oran küçükse tartışma
    biter. Büyükse sıra varsayımını oynatan bir duyarlılık koşusu gerekir — ve o koşu
    yalnızca harness'ta yapılır, canlı defterde asla: defterin kuralı tek olmalıdır
    (bkz. docs/backtest.md > 5e).
    """
    rows = [model for model in report.models if model.stop_exits]
    if not rows:
        return ""
    out = ["\nAYNI-BAR BELİRSİZLİĞİ (kural 13) — stop varsayımı ne sıklıkta bağladı",
           "-" * 78,
           f"{'model':18s}{'stop çıkış':>12}{'belirsiz':>10}{'oran':>8}"]
    total_stops = total_ambiguous = 0
    for model in rows:
        share = model.ambiguous_stop_exits / model.stop_exits
        total_stops += model.stop_exits
        total_ambiguous += model.ambiguous_stop_exits
        out.append(f"{model.model:18s}{model.stop_exits:12d}"
                   f"{model.ambiguous_stop_exits:10d}{share:8.1%}")
    if len(rows) > 1:
        out.append(f"{'TOPLAM':18s}{total_stops:12d}{total_ambiguous:10d}"
                   f"{total_ambiguous / total_stops:8.1%}")
    return "\n".join(out) + "\n"


def format_breakdowns(breakdowns: Mapping[str, Any]) -> str:
    """Katmanın kırılımlarını okunur bir tabloya çevirir.

    Sayıları `core/metrics.py` üretir; buradaki iş yalnızca BİÇİMLENDİRMEDİR. Kırılımın
    kendi tanımına (grup ölçütü, birim) dokunulmaz — ona dokunmak, aynı defterin backtest
    ile dashboard'da iki farklı kırılım göstermesi demekti.

    `exit_rule` kırılımının birimi DİLİMDİR (kural 13c), diğerlerininki pozisyon; bu yüzden
    o bölümün `n` toplamı model tablosundan büyük olabilir ve satır bunu SÖYLER.
    """
    if not breakdowns:
        return ""
    out: list[str] = []
    for kind, per_model in breakdowns.items():
        unit = "dilim" if kind == "exit_rule" else "pozisyon"
        out.append(f"\nKIRILIM: {kind}  (birim: {unit})")
        out.append("-" * 104)
        out.append(
            f"{'model':16s}{'grup':26s}{'n':>6}{'ort.R':>9}{'kazanç%':>10}"
            f"{'ortKaz.R':>10}{'ortKay.R':>10}{'PF':>8}{'topl.R':>10}"
        )
        for model, groups in per_model.items():
            if not groups:
                continue
            for group, stats in sorted(
                groups.items(), key=lambda kv: -(kv[1].get("trades") or 0)
            ):
                out.append(
                    f"{model:16s}{str(group):26s}"
                    f"{stats.get('trades') or 0:6d}"
                    f"{_cell(stats.get('avg_r')):>9}"
                    f"{_cell(stats.get('win_rate')):>10}"
                    f"{_cell(stats.get('avg_win_r')):>10}"
                    f"{_cell(stats.get('avg_loss_r')):>10}"
                    f"{_cell(stats.get('profit_factor')):>8}"
                    f"{_cell(stats.get('total_r')):>10}"
                )
    return "\n".join(out) + "\n"


def format_holding(result: BacktestResult) -> str:
    """Tutuş süresi dağılımı. Zaman stop'u OLMAYAN modellerde bir zorunluluktur.

    `docs/backtest.md > 6.1`in embargo kuralı "doğru boşluk azami tutuş süresidir" der ve
    bu, zaman stop'u olan modellerde config'ten OKUNUR. Olmayanlarda üst sınır tanım
    gereği yoktur, yani embargo ancak ÖLÇÜLEREK bulunur — ve ölçüm basılmazsa koşuyu
    yapan kişi onu uydurmak zorunda kalır.

    Dağılımın kendisi de bir bulgudur: uzun bir kuyruk, kurulumların hedefe ya da stop'a
    varmadan beklediğini söyler.
    """
    if not result.holding:
        return ""
    out = [
        "",
        "TUTUŞ SÜRESİ (kapanmış POZİSYON başına; embargo bundan ölçülür)",
        "-" * 78,
        f"{'model':16s}{'n':>6}{'medyan':>10}{'p90':>10}{'azami':>10}{'azami gün':>12}",
    ]
    for model, stats in sorted(result.holding.items()):
        if not stats.get("positions"):
            continue
        out.append(
            f"{model:16s}{stats['positions']:6d}"
            f"{_cell(stats.get('median_bars')):>10}"
            f"{_cell(stats.get('p90_bars')):>10}"
            f"{_cell(stats.get('max_bars')):>10}"
            f"{_cell(stats.get('max_days')):>12}"
        )
    out.append("")
    out.append("Bar cinsindendir; OOS embargosu için AZAMİ değer yukarı yuvarlanır.")
    return "\n".join(out) + "\n"


def _cell(value: Any) -> str:
    """Tanımsız metrik `—` olur, `0` DEĞİL (docs/shared.js ile aynı söz)."""
    if value is None:
        return "—"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    return "—" if number != number else f"{number:.2f}"


def results_payload(result: BacktestResult) -> dict[str, Any]:
    """Koşunun MAKİNE OKUNUR özeti: metrikler + kırılımlar + tutuş süresi + sapmalar.

    Neden ayrı bir yük: `format_report` insan içindir ve hizalanmış bir tablo, iki koşuyu
    (IS ↔ OOS) birleştirip sembol bazlı bir tablo üretmek için elle ayrıştırılmak zorunda
    kalırdı — ayrıştırma, sayıların ikinci bir kopyası demek olurdu. Yük doğrudan
    `core/metrics.py`nin dataclass'larından türer; burada hiçbir şey yeniden hesaplanmaz.

    `deviations` yükün İÇİNDEDİR ve dışarıda bırakılamaz: bir sayının hangi dünyada
    (hangi maliyet, hangi funding derinliği, hangi sinyal kesimi) ölçüldüğü, sayının
    kendisi kadar ölçümün parçasıdır.
    """
    return {
        "layer": result.layer,
        "window": {"start": result.start.isoformat(), "end": result.end.isoformat()},
        "min_trades": result.min_trades,
        "deviations": dict(result.deviations),
        "build_failures": dict(result.build_failures),
        "validity": {
            model.model: {
                "bars_processed": model.bars_processed,
                "missing_bars": model.missing_bars,
                "unchecked_position_bars": model.unchecked_position_bars,
                "signals": model.signals,
                "filled": model.filled,
                "rejections": dict(model.rejections),
                "stop_exits": model.stop_exits,
                "ambiguous_stop_exits": model.ambiguous_stop_exits,
            }
            for model in result.report.models
        },
        "models": [asdict(metrics) for metrics in result.metrics],
        "acceptance": [asdict(flag) for flag in result.acceptance],
        "breakdowns": jsonable(result.breakdowns),
        "holding": dict(result.holding),
        "buy_hold_pct": dict(result.buy_hold),
        "coverage": dict(result.coverage),
    }


def _git_log_shas(path: str) -> list[str]:
    result = subprocess.run(
        ["git", "log", "--format=%H", "--", path], capture_output=True, text=True
    )
    return result.stdout.split() if result.returncode == 0 else []


def _git_show(spec: str) -> str:
    result = subprocess.run(["git", "show", spec], capture_output=True, text=True)
    return result.stdout if result.returncode == 0 else ""


def _stamp(value: Any) -> pd.Timestamp | None:
    if not value:
        return None
    try:
        stamp = pd.Timestamp(str(value))
    except (ValueError, TypeError):
        return None
    if pd.isna(stamp):
        return None
    return stamp.tz_convert("UTC") if stamp.tzinfo is not None else stamp.tz_localize("UTC")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper()),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    start, end = _stamp(args.start), _stamp(args.end)
    if start is None or end is None:
        logger.error("--start ve --end ISO zaman damgası olmalı")
        return 2

    cutoff = _stamp(args.signal_cutoff)
    if args.signal_cutoff and cutoff is None:
        logger.error("--signal-cutoff ISO zaman damgası olmalı")
        return 2

    run_id = args.run_id or f"{args.layer}-{start:%Y%m%dT%H%M}-{end:%Y%m%dT%H%M}"
    out_dir = Path(args.out) if args.out else BACKTEST_ROOT / run_id

    try:
        result = run_backtest(
            layer_name=args.layer, start=start, end=end, out_dir=out_dir,
            models=args.models.split(",") if args.models else None,
            config_path=args.config,
            history_bars=args.history_bars,
            embargo_bars=args.embargo_bars,
            symbols=args.symbols.split(",") if args.symbols else None,
            fee_rate=args.fee_rate,
            slippage_base=args.slippage_base,
            funding_periods=args.funding_periods,
            signal_cutoff=cutoff,
        )
    except Exception as exc:  # noqa: BLE001 — CLI sınırı; gerekçe kullanıcıya gider
        logger.error("backtest koşulamadı: %s", exc)
        return 1

    print(format_report(list(result.metrics), min_trades=result.min_trades or None))
    print(format_drift(result.metrics))
    print(format_fill_ambiguity(result.report))
    print(format_holding(result))
    print(format_breakdowns(result.breakdowns))

    if args.results_json:
        path = Path(args.results_json)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(jsonable(results_payload(result)), ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        logger.info("sonuç yükü: %s", path)

    violations = check_validity(result.report)
    if violations:
        # Kapı B-2 düştü: pencere eksik bir geçmişin üstüne yazılmış demektir. Sayılar
        # yine de basılır (gizlemek daha kötü olurdu) ama koşu KIRMIZI döner.
        logger.error("GEÇERLİLİK KAPISI DÜŞTÜ (docs/backtest.md > 3):")
        for model, reasons in sorted(violations.items()):
            logger.error("  %-16s %s", model, ", ".join(reasons))

    if args.verify_live:
        ok = _report_gate_zero(result, metrics_path=args.verify_live, start=start, end=end)
        if not ok:
            return 1

    logger.info("çıktı: %s", out_dir)
    return 1 if violations or result.build_failures else 0


def _report_gate_zero(
    result: BacktestResult, *, metrics_path: str, start: pd.Timestamp, end: pd.Timestamp
) -> bool:
    manifest = json.loads((result.out_dir / MANIFEST_FILENAME).read_text(encoding="utf-8"))
    adaptive = manifest["adaptive_models"]
    live = live_emitted_keys(metrics_path, start=start, end=end)
    if not live:
        logger.error("KAPI 0: %s geçmişinde bu pencereye ait canlı kayıt yok", metrics_path)
        return False

    table = compare_signals(emitted_keys(result.report), live, adaptive=adaptive)
    logger.info("KAPI 0 — harness canlı veriye karşı (docs/backtest.md > 1)")
    failed = []
    for name, row in table.items():
        mark = "KAPI" if row["gate"] else "bilgi"
        status = "EŞLEŞTİ" if row["match"] else "AYRIŞTI"
        logger.info(
            "  %-16s [%-5s] backtest=%d canlı=%d ortak=%d -> %s",
            name, mark, row["backtest"], row["live"], row["both"], status,
        )
        for line in row["only_backtest"][:5]:
            logger.info("      yalnız backtest: %s", line)
        for line in row["only_live"][:5]:
            logger.info("      yalnız canlı   : %s", line)
        if row["gate"] and not row["match"]:
            failed.append(name)

    if failed:
        logger.error(
            "KAPI 0 DÜŞTÜ: %s birebir eşleşmedi. docs/backtest.md > 1 gereği "
            "hiçbir backtest sonucu yorumlanmaz.", ", ".join(failed),
        )
        return False
    logger.info("KAPI 0 GEÇTİ: uyarlanabilir olmayan modellerin sinyalleri birebir eşleşti")
    return True


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Canlı motoru geçmiş bir pencerede koşturur (docs/backtest.md)."
    )
    parser.add_argument("--layer", default=DEFAULT_LAYER)
    parser.add_argument("--start", required=True, help="pencere başı (ISO); bu bar İŞLENMEZ")
    parser.add_argument("--end", required=True, help="pencere sonu (ISO); bu bar işlenir")
    parser.add_argument("--models", default=None, help="virgülle; boş = katmanın listesi")
    parser.add_argument("--out", default=None, help="çıktı dizini")
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--config", default=None)
    parser.add_argument(
        "--history-bars", type=int, default=None, metavar="N",
        help="anlık görüntü derinliği; yalnızca DERİNLEŞTİRİR (config'teki değerin altına inemez)",
    )
    parser.add_argument(
        "--embargo-bars", type=int, default=None, metavar="N",
        help=(
            "OOS penceresinin başına konan boşluk (docs/backtest.md > 6): IS kurulumlarının "
            "çözüldüğü barlar OOS'a girmesin diye. Doğru değer azami tutuş süresidir "
            "(katmanın time_stop_bars'ı)."
        ),
    )
    parser.add_argument(
        "--symbols", default=None,
        help=(
            "virgülle sembol listesi; katmanın evrenini DARALTIR (genişletemez). Dış bir "
            "referansla parite için tek sembollü koşu: çok sembollü koşuda portföy kotası "
            "(max_positions) sinyal reddeder ve kıyas modelin değil kotanın ölçüsü olur."
        ),
    )
    parser.add_argument(
        "--fee-rate", type=float, default=None, metavar="ORAN",
        help=(
            "tek yön komisyon override'ı. ÖLÇÜM KURALINI değiştirir (--history-bars'ın "
            "aksine sonucu kaydırır): yalnızca dış bir referansla parite için; koşu "
            "bağırır ve manifest'e yazılır. Canlı config DEĞİŞMEZ."
        ),
    )
    parser.add_argument(
        "--slippage-base", type=float, default=None, metavar="ORAN",
        help="temel kayma override'ı; --fee-rate ile aynı uyarılar geçerli",
    )
    parser.add_argument(
        "--funding-periods", type=int, default=None, metavar="N",
        help=(
            "funding geçmişi derinliği; yalnızca DERİNLEŞTİRİR. Varsayılan 180 periyot "
            "≈ 60 gündür: yıllara uzanan pencerede kaydı olmayan an funding ÖDEMEZ ve "
            "eski dönem iyimser çıkar."
        ),
    )
    parser.add_argument(
        "--signal-cutoff", default=None, metavar="ISO",
        help=(
            "bu bardan sonra YENİ sinyal üretilmez; barlar pozisyon yönetimi için "
            "işlenmeye devam eder. Dönem ataması giriş tarihine göredir: dönemin son "
            "kurulumları sınırı aşsa bile kapanışına kadar o döneme sayılır."
        ),
    )
    parser.add_argument(
        "--results-json", default=None, metavar="PATH",
        help="metrik + kırılım + tutuş süresi + sapmaları makine okunur biçimde yazar",
    )
    parser.add_argument(
        "--verify-live", default=None, metavar="METRICS_PATH",
        help="KAPI 0: sinyalleri bu rapor dosyasının git geçmişiyle karşılaştır",
    )
    parser.add_argument(
        "--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"]
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    sys.exit(main())
