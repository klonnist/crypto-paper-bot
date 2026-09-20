"""Tek bir turu uçtan uca çalıştıran giriş noktası.

    veri çek -> as_of -> config'deki modelleri çalıştır -> metrikleri üret -> rapor

Akış bilinçli olarak İNCEDİR: iş mantığının tamamı `core/` içindedir, burada yalnızca
sıra ve hata izolasyonu vardır. Cron bu dosyayı çağırır (.github/workflows/run.yml),
defterler ve `docs/data/metrics.json` koşudan sonra commit edilir.

**Hata izolasyonu iki katmanlıdır ve kasten farklıdır:**

- *Model kurulumu* burada izole edilir: `config.yaml`'da tanınmayan ya da kurulurken
  patlayan bir model, diğer dokuz modelin turunu düşürmez — yalnızca kendisi atlanır ve
  çıkış kodu 1 olur (sessiz eksik yarışma olmasın, kural 6).
- *Sinyal/çıkış üretimi* `core/engine.py` içinde izole edilir (kural 8): modelin kodu
  patlarsa o modelin turu boş geçer, koşu sürer.
- *Defter/veri hatası izole EDİLMEZ:* bozuk bir defter ya da bayat bir anlık görüntü
  turu tümden düşürür. Bunlar model hatası değil ölçüm hatasıdır; "yarısı yazılmış"
  bir turla devam etmek denetim izini sessizce bozardı (bkz. core/ledger.py).

`--layer` TURUN KOŞULLARINI seçer (bkz. core/layers.py): `base` 4 saatlik ana yarışma,
`scalp` 15 dakikalık scalp katmanıdır. İki katman da AYNI çekirdeği koşar — bu dosya,
`core/engine.py`, `core/portfolio.py`, `core/ledger.py`, `core/metrics.py` tek kopyadır;
değişen yalnızca bar, sembol evreni, model listesi, defter kökü ve rapor dosyasıdır.
Ayrı bir giriş noktası açmak orkestrasyonu (model kurulumu, hata izolasyonu, dry-run
kopyası, yük yazımı) ikiye kopyalar ve iki katmanın sessizce ayrışmasına kapı açardı.

`--dry-run` deftere yazmaz: defterin bir KOPYASI geçici dizine alınır, tur orada koşar ve
rapor oradan üretilir. Gerçek defteri okuyup yazmayı atlamak yetmezdi — motor turu
ilerletirken durumu yazar, yazmayan bir motor da metrikleri üretecek satırları hiç
oluşturmazdı. `docs/data/metrics.json` de dry-run'da yazılmaz: o dosya defterin türevidir,
defterle birlikte commit edilir; kalıcı olmayan bir turdan üretilmiş hâli ikisini ayrıştırır.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import shutil
import sys
import tempfile
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import pandas as pd

from core.config import get_setting, load_config
from core.data import load_market_data
from core.engine import Engine, RoundReport
from core.layers import DEFAULT_LAYER, Layer, resolve_layer
from core.ledger import Ledger
from core.metrics import ModelMetrics, compare, format_report
from core.portfolio import Portfolio
from core.report import build_dashboard
from strategies.base import MarketData, Strategy
from strategies.registry import build

logger = logging.getLogger("main")


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper()),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    layer = resolve_layer(load_config(args.config), args.layer)
    config = layer.config
    names = layer.models
    if not names:
        logger.error("%s katmanında models listesi boş: çalıştırılacak model yok", layer.name)
        return 1
    logger.info(
        "katman=%s bar=%s modeller=%s defter=%s rapor=%s",
        layer.name, layer.timeframe, ", ".join(names),
        layer.ledger_root.name, layer.metrics_path,
    )

    strategies, build_failures = build_strategies(names, config)
    if not strategies:
        logger.error("hiçbir model kurulamadı, tur çalıştırılmadı")
        return 1

    market = load_market_data(config, symbols=layer.symbols)
    logger.info(
        "anlık görüntü hazır: as_of=%s, %d sembol, %d model",
        market.as_of, len(market.ohlcv), len(strategies),
    )

    with _ledger_for(args.dry_run, layer) as ledger:
        report = Engine(
            strategies, config=config, ledger=ledger, portfolio=Portfolio(config)
        ).run_round(market)
        _log_round(report)

        metrics = compare(
            [strategy.name for strategy in strategies],
            ledger=ledger,
            config=config,
            benchmarks=[s.name for s in strategies if s.is_benchmark],
            replicas=[s.name for s in strategies if s.is_replica],
            # Piyasa kontrolünün çıpası BTC'dir — projenin zaten seçilmiş referansı
            # (`exchange.btc_reference`, `as_of` çapası) ve iki katmanda da var. Bir
            # sepet (ör. 50/50) ağırlık seçimi demekti, yani serbest bir parametre.
            reference=market.btc.get("close"),
        )
        # Örneklem kapısı tabloya da uygulanır: kapıyı geçmeyen satır SIRALANMAZ
        # (bkz. core/metrics.py::format_report).
        print(format_report(
            metrics, min_trades=int(get_setting(config, "acceptance.min_trades"))
        ))

        _compact_equity(ledger, [s.name for s in strategies], layer=layer, as_of=market.as_of)

        payload = _payload(
            layer=layer,
            config=config,
            market=market,
            report=report,
            metrics=metrics,
            strategies=strategies,
            build_failures=build_failures,
            dry_run=args.dry_run,
        )
        # Dashboard bölümleri defteri OKUR, bu yüzden defter kapsamı hâlâ açıkken üretilir:
        # --dry-run'da sayfa da turun geçici kopyasını yansıtır, gerçek defteri değil.
        payload.update(
            build_dashboard(
                metrics,
                ledger=ledger,
                config=config,
                market=market,
                model_trade_limit=layer.retention.model_trade_limit,
                breakdowns=layer.breakdowns,
            )
        )

    if args.dry_run:
        logger.info("--dry-run: defter ve %s yazılmadı", layer.metrics_path)
    else:
        _write_metrics(payload, layer.metrics_path)

    # Kurulamayan model = eksik yarışma. Tur başarılı olsa bile koşu kırmızı dönmeli.
    return 1 if build_failures else 0


# --------------------------------------------------------------------------- #
# Model kurulumu
# --------------------------------------------------------------------------- #
def build_strategies(
    names: Sequence[str], config: Mapping[str, Any]
) -> tuple[list[Strategy], dict[str, str]]:
    """Modelleri sırayla kurar; biri patlarsa yalnızca o atlanır (gerekçesiyle).

    `config` KATMANIN çözülmüş ayarıdır: ayarı okuyan model kök değerleri değil katmanın
    değerlerini görmelidir (15 dakikalık katmanda bar süresi, stop tavanı, sembol evreni).

    Alt çizgisiz (herkese açık) çünkü ikinci bir meşru çağıran var: `scripts/backtest.py`.
    Backtest'in kendi model kurulumunu yazması, tam da `main.py`'nin tek giriş noktası
    olma gerekçesini delerdi — kurulum iki yerde ayrışırsa backtest, canlıda koşandan
    başka bir model kümesini ölçmeye başlar ve bunu hiçbir test yakalamaz.
    """
    strategies: list[Strategy] = []
    failures: dict[str, str] = {}
    for name in names:
        try:
            strategies.append(build(name, config=config))
        except Exception as exc:
            logger.error("%s modeli kurulamadı, atlanıyor: %s", name, exc)
            failures[name] = str(exc)
    return strategies, failures


# --------------------------------------------------------------------------- #
# Defter (dry-run kopyası)
# --------------------------------------------------------------------------- #
class _LedgerScope:
    """`--dry-run` için defterin geçici kopyasını, aksi hâlde katmanın gerçek defterini verir.

    Defter kökü KATMANDAN gelir: iki katman asla aynı defteri paylaşmaz. Paylaşsalardı
    15 dakikalık turlar 4 saatlik modellerin `last_processed_bar` değerini ileri taşır ve
    iki ölçüm birbirinin bakiyesini bozardı.
    """

    def __init__(self, dry_run: bool, layer: Layer) -> None:
        self._dry_run = dry_run
        self._layer = layer
        self._tmp: tempfile.TemporaryDirectory[str] | None = None

    def __enter__(self) -> Ledger:
        source = self._layer.ledger_root
        if not self._dry_run:
            return Ledger(source)
        self._tmp = tempfile.TemporaryDirectory(prefix="paper-bot-dryrun-")
        target = Path(self._tmp.name) / source.name
        if source.is_dir():
            shutil.copytree(source, target)
        else:
            target.mkdir(parents=True)
        logger.info("--dry-run: defter kopyası %s", target)
        return Ledger(target)

    def __exit__(self, *exc_info: object) -> None:
        if self._tmp is not None:
            self._tmp.cleanup()


def _ledger_for(dry_run: bool, layer: Layer) -> _LedgerScope:
    return _LedgerScope(dry_run, layer)


def _compact_equity(
    ledger: Ledger, models: Sequence[str], *, layer: Layer, as_of: pd.Timestamp
) -> None:
    """Katmanın saklama penceresinden eski equity satırlarını günlük özete indirir.

    15 dakikalık katman günde 96 tur koşar ve her turu commit eder: sıkıştırma olmadan
    `equity.csv` yılda on binlerce satıra çıkar ve depo geçmişi ölçümle ilgisiz satırlarla
    şişer. `trades.csv`'ye DOKUNULMAZ — denetim izi odur (bkz. core/ledger.py).

    Çağrı turun SONUNDA, metrikler üretilmeden ÖNCE yapılır: metrikler sıkıştırılmış
    eğriden hesaplansın ki raporlanan sayı ile defterdeki seri her zaman aynı şeyi söylesin.
    """
    days = layer.retention.equity_compaction_days
    if days is None:
        return
    cutoff = (as_of - pd.Timedelta(days=days)).isoformat()
    for model in models:
        ledger.compact_equity(model, older_than=cutoff)


# --------------------------------------------------------------------------- #
# Çıktı
# --------------------------------------------------------------------------- #
def _payload(
    *,
    layer: Layer,
    config: dict[str, Any],
    market: MarketData,
    report: RoundReport,
    metrics: Sequence[ModelMetrics],
    strategies: Sequence[Strategy],
    build_failures: dict[str, str],
    dry_run: bool,
) -> dict[str, Any]:
    """Katmanın rapor dosyasının (`layer.metrics_file`) içeriği.

    `layer` ve `timeframe` yükün en üstünde durur: sayfa iki katmanı AYRI bölümlerde
    çizer ve hangi dosyanın hangi zaman dilimine ait olduğunu tahmin etmemelidir.

    `as_of` ve koşu koşulları (maliyet sabitleri, görülen sembol sayısı) da yazılır:
    tablo tek başına "hangi varsayımlarla ölçüldü" sorusuna cevap veremez, oysa sonucun
    yorumu tam da ona bağlıdır.
    """
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "layer": layer.name,
        "as_of": market.as_of.isoformat(),
        "dry_run": dry_run,
        "universe_size": len(market.ohlcv),
        "settings": {
            key: get_setting(config, key)
            for key in (
                "initial_capital", "risk_per_trade", "leverage_cap", "max_positions",
                "max_short_positions", "fee_rate", "slippage_base", "slippage_short_stop",
                "maintenance_margin", "max_stop_atr_multiple", "timeframe",
            )
        },
        "round": {
            "as_of": report.as_of.isoformat(),
            "models": [asdict(item) for item in report.models],
        },
        "build_failures": build_failures,
        "models": [
            {**jsonable(asdict(item)), "name": item.model}
            for item in metrics
        ],
        "benchmarks": [strategy.name for strategy in strategies if strategy.is_benchmark],
        # Kopya modeller ayrı bir liste: sayfa onları çıpayla aynı bölümde çizemez
        # (ölçtükleri soru farklı) ve yarışmacı tablosuna hiç sokmamalıdır.
        "replicas": [strategy.name for strategy in strategies if strategy.is_replica],
    }


def _write_metrics(payload: dict[str, Any], path: Path) -> None:
    """Yükü diske yazar. `jsonable` TÜM yüke uygulanır, yalnızca model tablosuna değil.

    Dashboard bölümleri de tanımsız metrik taşır (açık pozisyonun R'si, hiç işlem
    görmemiş bir günün ortalama R'si). Dönüşümü yükün yalnızca bir dalına uygulamak,
    yeni bir bölüm eklendiği gün sessizce GEÇERSİZ JSON üretirdi — ve sayfa veriyi
    hiç çizemeden ölürdü.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(jsonable(payload), indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    logger.info("metrikler yazıldı: %s", path)


def jsonable(value: Any) -> Any:
    """nan/inf -> null. `json.dumps` bunları `NaN` yazar ve ortaya GEÇERSİZ JSON çıkar.

    nan burada "ölçülemedi" demektir (bkz. core/metrics.py) ve JSON'da onun karşılığı
    null'dır; 0.0'a çevirmek "ölçüldü, sıfır çıktı" ile karıştırırdı.

    **Tek kopyadır ve backtest harness'ı da bunu çağırır** (`scripts/backtest.py`,
    `scripts/backtest_ema.py`). İkinci bir uygulama, canlı yükü okuyan sayfa ile backtest
    yükünü okuyan sayfanın farklı geçerlilikte JSON görmesi demekti — harness'ın kendi
    kopyası tam olarak bunu yapıyordu: dataclass'ı sözlüğe indiriyor ama nan'ı OLDUĞU GİBİ
    bırakıyordu, yani `docs/backtest.html` dosyayı hiç ayrıştıramadan ölürdü.
    """
    if isinstance(value, dict):
        return {key: jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if is_dataclass(value) and not isinstance(value, type):
        return jsonable(asdict(value))
    return value


def _log_round(report: RoundReport) -> None:
    """Tur özeti. Ret dökümü `signals` ve `filled` kadar birinci sınıftır.

    "sinyal üretildi ama işlem açılmadı" tek başına iki bambaşka şeyin aynı görünümüdür:
    beklenen bir tekrar (referans modelin zaten taşıdığı pozisyon) ile gerçek bir
    boyutlandırma arızası. Kod dökümü olmadan ikisi aylar sonra ayırt edilemez, o yüzden
    her turda yazılır — sinyal üretilip hiçbiri dolmadığında ayrıca vurgulanır.
    """
    logger.info("tur tamamlandı: as_of=%s", report.as_of)
    for model in report.models:
        logger.info(
            "  %-16s bar=%d dolum=%d kapanan=%d sinyal=%d çıkış=%d band-atlanan=%d%s%s%s%s",
            model.model, model.bars_processed, model.filled, model.closed,
            model.signals, model.exits, model.skipped_signals,
            # Telafi edilemeyen bar: kaçırılan turların barları normalde bu turda sırayla
            # işlenir (core/engine.py > _timeline). Bu sayı sıfırdan büyükse anlık görüntü
            # son işlenmiş bara kadar geri gitmemiş, yani o barların stop/TP kontrolü hiç
            # yapılmamıştır — tur özetinde ret dökümü kadar birinci sınıf durması gerekir.
            f" telafi-edilemeyen-bar={model.missing_bars}" if model.missing_bars else "",
            # Kontrol edilemeyen pozisyon-barı: bar İŞLENDİ ama o sembolün mumu anlık
            # görüntüde yoktu, yani açık pozisyonun o mumdaki stop/TP/likidasyon kontrolü
            # hiç yapılmadı ve bar bir daha gelmeyecek. Telafi edilemeyen bardan ayrı
            # yazılır: sebepleri farklı (turun gecikmesi ≠ tek sembolün veri boşluğu).
            f" kontrol-edilemeyen-pozisyon-barı={model.unchecked_position_bars}"
            if model.unchecked_position_bars else "",
            f" ret={_format_rejections(model.rejections)}" if model.rejections else "",
            f" ATLANDI: {model.skipped}" if model.skipped else "",
        )
        if model.signals and not model.filled and not model.rejections:
            # Kodsuz bir "hiç dolmadı" turu: emirler bir sonraki barda dolacağı için
            # normaldir (kural 13), ama kodlu hâliyle karışmasın diye ayrıca söylenir.
            logger.info(
                "  %-16s sinyaller kuyrukta: dolum bir sonraki barın açılışında (kural 13)",
                model.model,
            )


def _format_rejections(rejections: Mapping[str, int]) -> str:
    return ", ".join(f"{code}×{count}" for code, count in sorted(rejections.items()))


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Bir paper-trading turu çalıştırır ve metrikleri üretir."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="deftere ve docs/data/metrics.json'a yazmadan turu çalıştırıp raporlar",
    )
    parser.add_argument(
        "--layer",
        default=DEFAULT_LAYER,
        help=(
            "koşulacak katman (config.yaml > layers): varsayılan %(default)s. "
            "Katman bar, sembol evreni, model listesi, defter ve rapor dosyasını belirler; "
            "maliyet ve risk sabitleri iki katmanda da birebir aynıdır."
        ),
    )
    parser.add_argument("--config", default=None, help="config.yaml yolu (varsayılan: proje kökü)")
    parser.add_argument(
        "--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"]
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    sys.exit(main())
