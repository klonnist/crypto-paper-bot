"""Fonlama uç noktasının DERİNLİK PROBE'u. SALT OKUNUR, ölçümün parçası DEĞİL.

Cevapladığı soru tek: *dönem A'nın fonlama geçmişine OKX'in kendi yollarından
ULAŞILABİLİYOR MU?* Yani bu bir dağılım ölçümü değil, o ölçümün önündeki engelin
teşhisidir — `scripts/measure_funding.py` dönem A'yı soruyor ve soruyu soramadan veri
yolunda duruyor (karar 50).

**Bu bir eşik seçmez, bir dağılım göstermez, bir tez sınamaz.** Getiri, R, PnL
hesaplanmaz ve `core/portfolio.py`, `core/metrics.py`, `core/ledger.py`, `strategies/*`
modülleri import EDİLMEZ (test: `tests/test_probe_funding_depth.py`). Ön-kaydı
docs/backtest.md > 6f'tir ve o belge bu betik hiç koşmadan commit edildi.

**Rapor YALNIZCA meta veri taşır: HTTP durumu, kayıt SAYISI, zaman DAMGASI, sayfa boyu.
Hiçbir yerde bir fonlama ORANI yazılmaz** ve modül yanıtın oran alanına hiç dokunmaz
— o alanın adı kaynakta hiç geçmez (test: `tests/test_probe_funding_depth.py`).
Bu, salt okunurluktan ayrı ve ondan daha dar bir sözdür: derinliği ölçmek için uç
noktanın en TAZE sayfalarından başlayıp geriye yürümek gerekir, yani istekler dönem B'ye
denk gelen damgaları da getirir (`measure_funding.py::fetch_history` de aynısını yapar ve
onları atar). Damga saymak o pencereye BAKMAK değildir; oran okumak olurdu.

**Salt okunur.** Deftere yazmaz, `config.yaml`a dokunmaz, `docs/`a bir şey koymaz,
`data/cache/`e yazmaz, hiçbir modelin davranışını değiştirmez (kural 1/2/3/7). Çıktısı
yalnızca log'dur.

**Neden `measure_funding.py`nin içinde bir bayrak değil.** O betikte dönem A kesimi bir
KAPIDIR: `--end` kesimi aşarsa hata koduyla biter ve bu, dönem B'ye bakmayı bir yazım
hatası kadar kolay olmaktan çıkarır. Derinlik probe'u ise tanımı gereği uç noktanın
TAMAMINA sorar (en taze kayıttan en eskiye kadar nereye ulaşıyor). İkisini tek betiğe
koymak o kapıyı gevşetmeyi gerektirirdi — kapıyı korumanın bedeli ayrı bir dosyadır ve
bu bedel ucuzdur.

**Yürüyüş İKİNCİ KEZ YAZILMAZ.** Ölçülen şey tam olarak `measure_funding.py::fetch_history`
in davranışıdır; kopyalanmış bir yürüyüş ondan sessizce ayrışabilir ve probe, ölçtüğünü
sandığı koddan başka bir şeyi ölçerdi (aynı gerekçe `scripts/backtest_ema.py`nin
`run_backtest`i çağırmasında). Probe o fonksiyonu ÇAĞIRIR ve yalnızca dönen serinin
INDEKSİNE bakar.

Kullanım (depo kökünden):
    python scripts/probe_funding_depth.py
    python scripts/probe_funding_depth.py --symbols BTC-USDT-SWAP SUI-USDT-SWAP
    python scripts/probe_funding_depth.py --skip-portal
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd  # noqa: E402

from core.config import load_config  # noqa: E402
from core.data import OKXClient  # noqa: E402
from core.layers import resolve_layer  # noqa: E402
from scripts.backtest_ema import PERIOD_A_CUTOFF, PERIOD_A_START  # noqa: E402
from scripts.measure_funding import FUNDING_ENDPOINT, fetch_history  # noqa: E402

logger = logging.getLogger("probe_funding_depth")

# A-1'in üçüncü sorusu: bir ikincil kaynak bu uç noktayı başka bir adla anıyor. İki ad
# tek uç noktanın iki yazımı mı, yoksa iki ayrı uç nokta mı — tek istekle görülür.
ENDPOINT_CANDIDATES: tuple[str, ...] = (
    FUNDING_ENDPOINT,                        # bugün kullandığımız
    "/api/v5/public/history-funding-rate",   # ikincil kaynağın verdiği ad
)

# A-1'in ikinci sorusu: sayfa boyu tavanı nerede. 312 kayıt, bildirilen 400 tavanının
# ALTINDA durdu; tavan gerçekten 400 ise bu (b) lehine bir işarettir.
LIMIT_CANDIDATES: tuple[int, ...] = (100, 200, 300, 400)

# A-2: OKX'in tarihsel veri portalı. Bu adresler DOĞRULANMAMIŞ adaylardır ve probe'un işi
# tam olarak onları doğrulamaktır — erişilebilirlik raporlanır, varlık İDDİA EDİLMEZ.
PORTAL_CANDIDATES: tuple[tuple[str, str], ...] = (
    ("portal-en", "https://www.okx.com/en-us/historical-data"),
    ("portal-root", "https://www.okx.com/historical-data"),
)
PORTAL_KEYWORDS: tuple[str, ...] = ("funding", "swaprate", "2022", ".zip", ".csv")
PORTAL_TIMEOUT_SEC = 20.0

# Yürüyüşün derinliğini sınırlayan tek şey uç nokta olsun diye: `fetch_history` `since`in
# gerisine düşünce durur, bu yüzden `since` pratikte ulaşılamayacak kadar eski verilir.
WALK_SINCE = pd.Timestamp("2015-01-01T00:00:00+00:00")


# --------------------------------------------------------------------------- #
# Yardımcılar — YALNIZCA damga okunur, oran OKUNMAZ
# --------------------------------------------------------------------------- #
def _stamps(page: Sequence[dict[str, Any]]) -> list[pd.Timestamp]:
    """Ham sayfadan yalnızca `fundingTime` alanını çıkarır.

    Oran alanına hiç dokunulmaz (modül başlığı): probe derinlik ölçer, dağılım değil.
    """
    out: list[pd.Timestamp] = []
    for raw in page:
        value = raw.get("fundingTime")
        if value is None:
            continue
        out.append(pd.Timestamp(int(value), unit="ms", tz="UTC"))
    return out


def _span(stamps: Sequence[pd.Timestamp]) -> str:
    if not stamps:
        return "kayıt yok"
    return f"{min(stamps):%Y-%m-%d %H:%M} → {max(stamps):%Y-%m-%d %H:%M}"


def _ms(stamp: pd.Timestamp) -> str:
    return str(int(stamp.value // 1_000_000))


# --------------------------------------------------------------------------- #
# A-2 — OKX'in tarihsel veri portalı (ÖNCE koşar)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, kw_only=True)
class PortalProbe:
    label: str
    url: str
    status: int | None
    final_url: str
    content_type: str
    size: int
    keywords: tuple[str, ...]
    error: str


def probe_portal(url: str, *, label: str, timeout: float = PORTAL_TIMEOUT_SEC) -> PortalProbe:
    """Portalın ERİŞİLEBİLİRLİĞİNİ ölçer; veri kümesinin varlığını İDDİA ETMEZ."""
    import requests  # yerel import: probe'un geri kalanı ağsız test edilebilir kalsın

    try:
        response = requests.get(
            url,
            timeout=timeout,
            allow_redirects=True,
            headers={"User-Agent": "crypto-paper-bot/probe (read-only)"},
        )
    except Exception as exc:
        return PortalProbe(
            label=label, url=url, status=None, final_url="", content_type="",
            size=0, keywords=(), error=f"{type(exc).__name__}: {exc}",
        )

    body = (getattr(response, "text", "") or "").lower()
    return PortalProbe(
        label=label,
        url=url,
        status=int(getattr(response, "status_code", 0)),
        final_url=str(getattr(response, "url", url)),
        content_type=str(response.headers.get("content-type", "")) if hasattr(response, "headers") else "",
        size=len(body),
        keywords=tuple(k for k in PORTAL_KEYWORDS if k in body),
        error="",
    )


def format_portal(probes: Sequence[PortalProbe]) -> list[str]:
    lines = [
        "",
        "=" * 78,
        "A-2 — OKX TARİHSEL VERİ PORTALI (erişilebilirlik; derinlik KANITI DEĞİL)",
        "=" * 78,
    ]
    for probe in probes:
        if probe.error:
            lines.append(f"{probe.label:<14} ULAŞILAMADI — {probe.error}")
            continue
        found = ", ".join(probe.keywords) if probe.keywords else "—"
        lines.append(
            f"{probe.label:<14} HTTP {probe.status} | {probe.size:>7} bayt | "
            f"anahtar kelime: {found}"
        )
        if probe.final_url != probe.url:
            lines.append(f"{'':<14} yönlendirme → {probe.final_url}")
    lines += [
        "",
        "OKUMA NOTU: HTTP 200 + 'funding' anahtar kelimesi portalın fonlama veri kümesi",
        "SUNDUĞUNU kanıtlamaz; sayfanın o kelimeyi içerdiğini söyler. Tersi de doğru:",
        "anahtar kelimenin YOKLUĞU bir yokluk kanıtı DEĞİLDİR — liste JS ile yükleniyor",
        "olabilir. Portal erişilebilir çıkarsa sıradaki adım, sunulan veri kümelerine",
        "elle bakmaktır (docs/backtest.md > 6f > Adım A).",
    ]
    return lines


# --------------------------------------------------------------------------- #
# A-1 — uç nokta adı ve sayfa boyu tavanı
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, kw_only=True)
class SingleRequest:
    label: str
    count: int
    stamps: tuple[pd.Timestamp, ...]
    error: str

    @property
    def oldest(self) -> pd.Timestamp | None:
        return min(self.stamps) if self.stamps else None


def request_once(
    client: OKXClient, *, label: str, path: str, params: dict[str, str]
) -> SingleRequest:
    """Tek istek; hatayı YUTMAZ, rapora yazar (belirsiz sonuç 'taban' diye okunamaz)."""
    try:
        page = client.get(path, params)
    except Exception as exc:
        return SingleRequest(label=label, count=0, stamps=(), error=f"{type(exc).__name__}: {exc}")
    stamps = _stamps(page)
    return SingleRequest(label=label, count=len(page), stamps=tuple(stamps), error="")


def probe_endpoint_names(client: OKXClient, symbol: str) -> list[SingleRequest]:
    return [
        request_once(
            client, label=path, path=path, params={"instId": symbol, "limit": "1"}
        )
        for path in ENDPOINT_CANDIDATES
    ]


def probe_limits(client: OKXClient, symbol: str) -> list[SingleRequest]:
    return [
        request_once(
            client,
            label=f"limit={limit}",
            path=FUNDING_ENDPOINT,
            params={"instId": symbol, "limit": str(limit)},
        )
        for limit in LIMIT_CANDIDATES
    ]


# --------------------------------------------------------------------------- #
# A-1 — (a) borsa tabanı ↔ (b) yürüyüş tabanı ayrımı
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, kw_only=True)
class SymbolProbe:
    symbol: str
    walk_records: int
    walk_oldest: pd.Timestamp | None
    walk_newest: pd.Timestamp | None
    walk_error: str
    resume: SingleRequest | None
    deep_jump: SingleRequest | None
    before_jump: SingleRequest | None
    limits: tuple[SingleRequest, ...]
    endpoints: tuple[SingleRequest, ...]

    @property
    def errors(self) -> list[str]:
        out = [f"walk: {self.walk_error}"] if self.walk_error else []
        for probe in (self.resume, self.deep_jump):
            if probe is not None and probe.error:
                out.append(f"{probe.label}: {probe.error}")
        return out


def _older_than(probe: SingleRequest | None, reference: pd.Timestamp | None) -> bool:
    """`probe` referanstan KESİN OLARAK daha eski en az bir damga döndürdü mü?

    Ölçüt "kayıt döndü mü" DEĞİL "daha eski kayıt döndü mü"dür: `after` yok sayılırsa
    uç nokta en taze sayfayı döndürür ve o, derinlik hakkında hiçbir şey söylemez.
    """
    if probe is None or probe.error or reference is None or not probe.stamps:
        return False
    return min(probe.stamps) < reference


def classify_depth_floor(probe: SymbolProbe) -> str:
    """Tabanın (a) borsada mı (b) yürüyüşte mi olduğunu MEKANİK olarak söyler.

    Kural docs/backtest.md > 6f > "Adım A"da, probe koşmadan ÖNCE yazıldı ve burada
    çalıştırılabilir kopyası durur (aynı gerekçe
    `diagnose_ema_exits.py::select_primary_family`): sonucu bir insanın okuyup dalı
    seçmesi, kuralın kapatmak için var olduğu serbestliği geri açardı.
    """
    if probe.errors:
        return "belirsiz"
    if probe.walk_oldest is None:
        return "belirsiz"
    if _older_than(probe.resume, probe.walk_oldest):
        return "(b) yürüyüş tabanı"
    if _older_than(probe.deep_jump, probe.walk_oldest):
        return "(b) yürüyüş tabanı"
    return "(a) borsa tabanı"


def probe_symbol(client: OKXClient, symbol: str, *, limit: int, cutoff: pd.Timestamp) -> SymbolProbe:
    endpoints = tuple(probe_endpoint_names(client, symbol))
    limits = tuple(probe_limits(client, symbol))

    # Yürüyüş: ölçülen şey `measure_funding.py`nin GERÇEK davranışıdır, bu yüzden o
    # fonksiyon çağrılır. Yalnızca indeks okunur — oranlara dokunulmaz (modül başlığı).
    walk_records, walk_oldest, walk_newest, walk_error = 0, None, None, ""
    try:
        series = fetch_history(
            client,
            symbol,
            since=WALK_SINCE,
            until=pd.Timestamp.now(tz="UTC") + pd.Timedelta(days=1),
            limit=limit,
        )
        index = series.index
        walk_records = int(index.size)
        if walk_records:
            walk_oldest, walk_newest = index.min(), index.max()
    except Exception as exc:
        walk_error = f"{type(exc).__name__}: {exc}"

    resume = deep_jump = before_jump = None
    if walk_oldest is not None:
        # (1) Yürüyüşün durduğu damgadan DOĞRUDAN devam: kısa sayfa gerçekten taban mı?
        resume = request_once(
            client,
            label=f"after={walk_oldest:%Y-%m-%d}",
            path=FUNDING_ENDPOINT,
            params={"instId": symbol, "limit": str(limit), "after": _ms(walk_oldest)},
        )
        # (2) Dönem A kesimine DOĞRUDAN atlama: yürümeden 2024'e ulaşılıyor mu?
        deep_jump = request_once(
            client,
            label=f"after={cutoff:%Y-%m-%d} (dönem A kesimi)",
            path=FUNDING_ENDPOINT,
            params={"instId": symbol, "limit": str(limit), "after": _ms(cutoff)},
        )
        # (3) Açık `before`: ters yön parametresi ayrı bir pencere mi açıyor?
        before_jump = request_once(
            client,
            label=f"before={cutoff:%Y-%m-%d}",
            path=FUNDING_ENDPOINT,
            params={"instId": symbol, "limit": str(limit), "before": _ms(cutoff)},
        )

    return SymbolProbe(
        symbol=symbol,
        walk_records=walk_records,
        walk_oldest=walk_oldest,
        walk_newest=walk_newest,
        walk_error=walk_error,
        resume=resume,
        deep_jump=deep_jump,
        before_jump=before_jump,
        limits=limits,
        endpoints=endpoints,
    )


# --------------------------------------------------------------------------- #
# Raporlama
# --------------------------------------------------------------------------- #
def _format_single(probe: SingleRequest | None) -> str:
    if probe is None:
        return "koşulmadı"
    if probe.error:
        return f"HATA — {probe.error}"
    return f"{probe.count:>4} kayıt | {_span(probe.stamps)}"


def format_symbol(probe: SymbolProbe) -> list[str]:
    lines = ["", "-" * 78, f"{probe.symbol}", "-" * 78, "", "uç nokta adı (limit=1):"]
    for item in probe.endpoints:
        lines.append(f"  {item.label:<42} {_format_single(item)}")

    lines += ["", "sayfa boyu tavanı (dönen kayıt sayısı):"]
    for item in probe.limits:
        lines.append(f"  {item.label:<42} {_format_single(item)}")

    lines += ["", "yürüyüş (measure_funding.py::fetch_history, DEĞİŞTİRİLMEDEN):"]
    if probe.walk_error:
        lines.append(f"  HATA — {probe.walk_error}")
    else:
        newest = f"{probe.walk_newest:%Y-%m-%d %H:%M}" if probe.walk_newest is not None else "—"
        oldest = f"{probe.walk_oldest:%Y-%m-%d %H:%M}" if probe.walk_oldest is not None else "—"
        lines.append(f"  {probe.walk_records} kayıt | en taze {newest} | en eski {oldest}")

    lines += ["", "ayrım istekleri (tek istek, yürüyüş YOK):"]
    for item in (probe.resume, probe.deep_jump, probe.before_jump):
        label = item.label if item is not None else "—"
        lines.append(f"  {label:<42} {_format_single(item)}")

    verdict = classify_depth_floor(probe)
    lines += ["", f"  >>> MEKANİK AYRIM: {verdict}"]
    if verdict == "belirsiz":
        for message in probe.errors:
            lines.append(f"      {message}")
        lines.append("      Belirsiz bir sonuç '(a) borsa tabanı' diye OKUNAMAZ.")
    elif verdict.startswith("(b)"):
        lines.append(
            "      Arşiv daha derine gidiyor; taban bizim sayfalamamızda. B ve C adımları"
        )
        lines.append("      GEREKMEYEBİLİR — önce fetch_history'nin imleci onarılır.")
    else:
        lines.append(
            "      Uç nokta bu yoldan dönem A'ya ulaşmıyor. Adım B devreye girer"
        )
        lines.append("      (docs/backtest.md > 6f: Bybit, sonra Binance).")
    return lines


def format_header(*, symbols: Sequence[str], cutoff: pd.Timestamp, start: pd.Timestamp) -> list[str]:
    return [
        "",
        "#" * 78,
        "# FONLAMA DERİNLİK PROBE'u — salt okunur, ölçümün parçası DEĞİL",
        "# Dağılım göstermez, eşik önermez, getiri/R/PnL hesaplamaz.",
        "# Rapor YALNIZCA meta veri taşır: durum, sayı, damga. Hiçbir ORAN yazılmaz.",
        f"# Hedef pencere: dönem A {start:%Y-%m-%d} → {cutoff:%Y-%m-%d}",
        f"# Sembol: {', '.join(symbols)}",
        "# Ön-kayıt: docs/backtest.md > 6f (bu betik koşmadan ÖNCE commit edildi)",
        "#" * 78,
    ]


# --------------------------------------------------------------------------- #
# Giriş noktası
# --------------------------------------------------------------------------- #
def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    cutoff = pd.Timestamp(PERIOD_A_CUTOFF)
    start = pd.Timestamp(PERIOD_A_START)

    layer = resolve_layer(load_config(args.config), args.layer)
    symbols = list(args.symbols) if args.symbols else ["BTC-USDT-SWAP"]
    unknown = [s for s in symbols if layer.symbols and s not in layer.symbols]
    if unknown:
        logger.error("evren dışı sembol: %s", ", ".join(unknown))
        return 2

    lines = format_header(symbols=symbols, cutoff=cutoff, start=start)

    # A-2 ÖNCE: portal erişilebilir ve fonlama veri kümesi taşıyorsa (a)/(b) ayrımı
    # gereksizdir — portal zaten OKX-içi bir yoldur (docs/backtest.md > 6f).
    if not args.skip_portal:
        lines += format_portal([probe_portal(url, label=label) for label, url in PORTAL_CANDIDATES])
    else:
        lines += ["", "A-2 atlandı (--skip-portal)."]

    lines += ["", "=" * 78, "A-1 — SAYFALAMA MEKANİĞİ: taban borsada mı, yürüyüşte mi?", "=" * 78]

    client = OKXClient.from_config(layer.config)
    verdicts: list[str] = []
    for symbol in symbols:
        probe = probe_symbol(client, symbol, limit=args.request_limit, cutoff=cutoff)
        lines += format_symbol(probe)
        verdicts.append(classify_depth_floor(probe))

    lines += ["", "=" * 78, "TOPLU SONUÇ", "=" * 78]
    if "belirsiz" in verdicts:
        lines.append("BELİRSİZ — en az bir sembolde istek hata verdi. Hiçbir adım açılmaz.")
        exit_code = 1
    elif any(v.startswith("(b)") for v in verdicts):
        lines.append("(b) YÜRÜYÜŞ TABANI — en az bir sembolde arşiv daha derine gidiyor.")
        lines.append("Sıradaki iş fetch_history'nin imlecini onarmaktır, arşiv aramak değil.")
        exit_code = 0
    else:
        lines.append("(a) BORSA TABANI — uç nokta bu yoldan dönem A'ya ulaşmıyor.")
        lines.append("Adım B açılır: docs/backtest.md > 6f'nin SABİT aday sırası (Bybit → Binance).")
        exit_code = 0

    lines += [
        "",
        "=" * 78,
        "NOT: Bu rapor bir eşik ÖNERMEZ ve bir dağılım GÖSTERMEZ. Cevapladığı tek soru",
        "'dönem A'ya ulaşılabiliyor mu'dur; 'orada ne var' sorusu bilinçli olarak",
        "SORULMADI (docs/backtest.md > 7).",
        "=" * 78,
    ]
    print("\n".join(lines))
    return exit_code


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--layer", default="ema", help="sembol evreninin alınacağı katman")
    parser.add_argument("--config", default=None)
    parser.add_argument("--symbols", nargs="*", default=None, help="varsayılan: BTC-USDT-SWAP")
    parser.add_argument("--request-limit", type=int, default=100, help="yürüyüşün sayfa boyu")
    parser.add_argument(
        "--skip-portal", action="store_true", help="A-2'yi atla, yalnızca sayfalama mekaniği"
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(main())
