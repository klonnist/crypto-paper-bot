"""Fonlama dağılımını ve ekstrem OLAY SAYISINI ölçer. SALT OKUNUR.

Cevapladığı soru tek: *4 saatlik barda bir fonlama-ekstremi tezi bu veriyle ÖLÇÜLEBİLİR
Mİ?* Yani hangi eşikte kaç olay var, olaylar hangi sembollerde ve hangi aylarda duruyor,
ve pozitif kuyruk ile negatif kuyruk simetrik mi.

**Bu bir strateji koşusu DEĞİLDİR ve öyle olmaması kasıtlıdır.** Getiri, R, PnL, kazanma
oranı hesaplanmaz ve raporlanmaz; modül `core/portfolio.py`, `core/metrics.py` ve hiçbir
`strategies/*` modülünü import ETMEZ. Gerekçe ön-kayıt disiplinidir (docs/backtest.md > 7):
**dağılımı görmek eşik seçimini kirletmez, sonucu görmek kirletirdi.** Bir eşiğin kaç olay
ürettiğini bilmek "ölçülebilir mi" sorusunu cevaplar; o eşiğin kaç R kazandırdığını bilmek
ise eşiği sonuca bakarak seçmek demektir. Bu yüzden eşik bu raporun SONUCU DEĞİLDİR —
eşik ayrı bir adımda, ön-kayıtta seçilir.

**Salt okunurluk.** Deftere yazmaz, `config.yaml`a dokunmaz, `docs/`a bir şey koymaz,
hiçbir modelin davranışını değiştirmez (kural 1/2/3/7). Fonlama geçmişini `data/cache/`
önbelleğine de YAZMAZ: `core/data.py::fetch_funding` önbelleği `data.funding_history_periods`
(180 periyot ≈ 60 gün) ile budar, oysa bu ölçüm yıllar istiyor — aynı dosyaya iki farklı
saklama kuralıyla yazmak, canlı turun okuduğu önbelleği bu aracın penceresine bağlardı.
Sayfalama bellekte yapılır ve koşu bittiğinde iz bırakmaz.

**Kapsam YALNIZCA dönem A'dır ve bu bir KAPIDIR, varsayılan değil.** Sınırlar
`scripts/backtest_ema.py`den İTHAL EDİLİR (`PERIOD_A_START`, `PERIOD_A_CUTOFF`) —
burada yeniden yazılsalardı iki yerde sessizce ayrışırlardı. `--end` dönem A kesimini
aşarsa betik HATA KODUYLA BİTER: dönem B bir sonraki tezin OOS penceresidir ve bir
dağılım raporunun ona bakması, o pencereyi bakılmış yapardı (docs/backtest.md > 6.1'in
embargo mantığı).

**Isınma penceresi dönem A'nın İÇİNDEN yenmez, ÖNCESİNDEN alınır.** Göreli eşikler
sembolün kendi geçmiş dağılımının persentilidir; pencere dolmadan hesaplanan bir p99
iki gözlemle tanımlanır. Bu yüzden veri `PERIOD_A_START`tan `window` periyot ÖNCESİNDEN
çekilir ve o kayıtlar yalnızca eşiği kurar, olay olarak SAYILMAZ. Dönem A'dan geriye
gitmek dönem B'ye dokunmaz.

**Seçilen iki kural ve gerekçeleri** (ikisi de raporun başlığında yazılıdır):

1. **Kayan pencere = 270 periyot (90 gün).** Fonlama 8 saatte bir yayınlanır, yani günde
   3 kayıt. 270 kayıt bir p99'un ~3 gözlemle değil ~3 gözlem ÜSTÜNDE tanımlanmasına yeter
   ve rejim değişimini hâlâ takip eder. Pencere GEÇMİŞE bakar ve **cari kaydı DIŞLAR**
   (`shift(1)`): içerseydi olay kendi eşiğini tanımlardı.
2. **Kümeleme: ardışık fonlama damgaları TEK olaydır.** 8 saatlik bir seride eşiği üst
   üste aşan üç damga tek bir kalabalıklaşma epizodudur; üç saymak, olay sayısını
   epizodun UZUNLUĞUNA bağlardı. İki ölçü birden raporlanır — bitişik damga kuralı
   (`adjacent`) ve 24 saatlik soğuma kuralı (`cooldown_24h`: yeni epizod için eşiğin
   altında en az 3 damga) — çünkü "bir damga düşüp geri çıktı" durumunun tek epizod mu
   iki epizod mu olduğu bir yargıdır ve tek sayıya indirmek o yargıyı gizlerdi.

**4H bar karşılığı.** Fonlama damgaları 00:00/08:00/16:00 UTC'dedir ve 4H bar sınırları
(00/04/08/12/16/20) bunları tam kapsar: her damga tam bir 4H barına düşer, her epizod
`2 × damga sayısı` bar sürer. Bu bir çeviridir, bir ölçüm değil — mum verisi hiç
çekilmez.

⚠ **İLK KOŞUNUN SONUCU: OKX bu pencereyi VERMİYOR** (koşu #35441623091). 13 sembolün
hepsinde dönem A penceresinde SIFIR kayıt bulundu. Sebep bu aracın filtresi değil,
kaynağın sınırıdır: `/api/v5/public/funding-rate-history` **~3 aylık KAYAN bir pencere**
tutuyor (283 kayıt, en eski 2026-06-17) ve dönem A'nın tamamı o pencerenin ~26 ay
dışında kalıyor — ölçen `scripts/probe_funding_depth.py`, kayıt karar 50 ve
docs/backtest.md > 6f. Projenin canlı yolu da bunu zaten varsayıyor:
`data.funding_history_periods` 180 (60 gün).

⚠ **Bu başlık bir zamanlar sebebi "~300-400 kayıtlık sayfalama tavanı" diye yazıyordu ve
o okuma ÇÜRÜDÜ** (karar 51): `limit=400` kabul ediliyor ama o kadar kayıt YOK, yani kısıt
bir sayfa boyu değil pencerenin kendisidir; `fetch_history` de kayıp vermiyor —
ulaşılabilen azami derinliğin tamamına ulaşıyor. Yanlış olan teşhisin içeriği değil
ÜRETİLME BİÇİMİYDİ: mekanizma log'un zaman damgalarından geri hesaplanmıştı, ölçülmemişti.

Bu bir ARAÇ hatası değil, bir VERİ bulgusudur ve tam olarak aracın cevaplamak için
yazıldığı sorunun cevabıdır: *bu veriyle ölçülebilir mi?* — **OKX public REST ile
HAYIR.** Araçta düzeltilen şey sonucun kendisi değil, SUNULUŞUDUR: koşu sıfır kodla
bitiyordu, yani "ölçtük ve bulamadık" ile "hiç ölçemedik" aynı hücreye yazılıyordu
(`core/metrics.py`nin "veri yoksa `nan`, `0.0` değil" kuralının çıkış kodundaki
karşılığı). İki şey eklendi: **(a0) ÇEKİM İZİ** bölümü (kaynağın gerçekte verdiği en eski damgayı
gösterir — üç ayrı sebebi ayırt eder: sembol listelenmemiş / borsa o kadar geriye
vermiyor / sayfalamamız bozuk) ve bir **VERİ KAPISI** (pencerede hiçbir sembolde kayıt
yoksa çıkış kodu 3).

Dönem A'yı gerçekten ölçmek AYRI bir karardır ve bu araçta verilmedi: başka bir kaynak
(üçüncü taraf funding arşivi ya da başka bir borsa) yeni bir veri varsayımı demektir ve
`scripts/measure_slippage.py`nin Bybit'i seçerken yazdığı türden bir gerekçe ister.

Kullanım (depo kökünden):
    python scripts/measure_funding.py
    python scripts/measure_funding.py --window-periods 180
    python scripts/measure_funding.py --symbols BTC-USDT-SWAP ETH-USDT-SWAP
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd  # noqa: E402

from core.data import OKXClient  # noqa: E402
from core.layers import resolve_layer  # noqa: E402
from core.config import load_config  # noqa: E402
from scripts.backtest_ema import PERIOD_A_CUTOFF, PERIOD_A_START  # noqa: E402

logger = logging.getLogger("measure_funding")

FUNDING_ENDPOINT = "/api/v5/public/funding-rate-history"
FUNDING_INTERVAL_HOURS = 8
PERIODS_PER_DAY = 24 // FUNDING_INTERVAL_HOURS
PERIODS_PER_YEAR = PERIODS_PER_DAY * 365
BAR_HOURS = 4
BARS_PER_PERIOD = FUNDING_INTERVAL_HOURS // BAR_HOURS

DEFAULT_WINDOW_PERIODS = 270  # 90 gün; gerekçe modül başlığında
COOLDOWN_PERIODS = 3  # 24 saat

# Göreli eşikler: sembolün KENDİ geçmiş dağılımının persentili.
RELATIVE_QUANTILES = (0.90, 0.95, 0.99)
# Mutlak eşikler: yıllıklandırılmış oran. Periyot başına karşılık = ann / PERIODS_PER_YEAR.
ABSOLUTE_ANNUAL = (0.20, 0.50, 1.00)
# Dağılım raporunun persentilleri.
REPORT_QUANTILES = (0.01, 0.05, 0.10, 0.25, 0.50, 0.75, 0.90, 0.95, 0.99)


def annualize(rate_per_period: float) -> float:
    """8 saatlik oranı yıllık orana çevirir (basit ölçekleme, bileşik DEĞİL).

    Bileşiklemek, ekstrem bir tek damgayı astronomik bir yıllık orana çevirirdi ve
    eşikler o abartının içinde okunamaz hâle gelirdi; borsaların ilan ettiği "yıllık
    funding" de bu basit ölçeklemedir.
    """
    return float(rate_per_period) * PERIODS_PER_YEAR


def deannualize(annual_rate: float) -> float:
    return float(annual_rate) / PERIODS_PER_YEAR


# --------------------------------------------------------------------------- #
# (a) Veri kapsamı
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, kw_only=True)
class Coverage:
    symbol: str
    first: pd.Timestamp | None
    last: pd.Timestamp | None
    observed: int
    expected: int
    gaps: tuple[tuple[pd.Timestamp, pd.Timestamp, int], ...]

    @property
    def missing(self) -> int:
        return max(self.expected - self.observed, 0)

    @property
    def completeness(self) -> float:
        return float("nan") if self.expected <= 0 else self.observed / self.expected


def coverage(
    series: pd.Series, *, start: pd.Timestamp, end: pd.Timestamp, min_gap_periods: int = 2
) -> Coverage:
    """Sembolün dönem içi kapsamı: ilk/son kayıt, beklenen ↔ gerçekleşen, eksik dönemler.

    Beklenen sayı SEMBOLÜN KENDİ ilk kaydından sayılır, dönem başından değil: PENGU
    2022'de listelenmemişti ve "listelenmeden önceki eksik kayıt" bir veri boşluğu değil,
    bir listeleme tarihidir. İkisini tek sayıya toplamak, seyrek veriyi geç listelemeyle
    karıştırırdı — §6d'nin "2022 kayıtları seyrek" cümlesi tam olarak bu ayrımı istiyor.
    """
    window = series.loc[(series.index >= start) & (series.index < end)]
    if window.empty:
        return Coverage(
            symbol=str(series.name or ""),
            first=None,
            last=None,
            observed=0,
            expected=0,
            gaps=(),
        )

    first = window.index[0]
    last = window.index[-1]
    span_hours = (last - first).total_seconds() / 3600
    expected = int(round(span_hours / FUNDING_INTERVAL_HOURS)) + 1

    gaps: list[tuple[pd.Timestamp, pd.Timestamp, int]] = []
    stamps = list(window.index)
    for previous, current in zip(stamps, stamps[1:]):
        steps = int(round((current - previous).total_seconds() / 3600 / FUNDING_INTERVAL_HOURS))
        if steps >= min_gap_periods:
            gaps.append((previous, current, steps - 1))

    return Coverage(
        symbol=str(series.name or ""),
        first=first,
        last=last,
        observed=int(window.size),
        expected=expected,
        gaps=tuple(gaps),
    )


# --------------------------------------------------------------------------- #
# (b) Dağılım
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, kw_only=True)
class Distribution:
    label: str
    count: int
    mean: float
    sd: float
    quantiles: Mapping[float, float]
    positive: int
    negative: int
    zero: int
    positive_mean: float
    negative_mean: float
    max: float
    min: float


def distribution(values: Sequence[float], *, label: str) -> Distribution:
    """Fonlama oranı dağılımı. Pozitif ve negatif kuyruk AYRI raporlanır.

    Ayrı olmasının sebebi tezin kendisidir: pozitif ekstrem long kalabalığını, negatif
    ekstrem short kalabalığını anlatır ve bunlar ayrı tezlerdir (aynı gerekçe
    `strategies/scalp/arms.py::funding_spike_fade`in tek yönlü olmasıdır). Tek bir
    |oran| dağılımı ikisini tek ortalamada eritirdi.
    """
    data = pd.Series([float(v) for v in values], dtype="float64").dropna()
    if data.empty:
        nan = float("nan")
        return Distribution(
            label=label,
            count=0,
            mean=nan,
            sd=nan,
            quantiles={q: nan for q in REPORT_QUANTILES},
            positive=0,
            negative=0,
            zero=0,
            positive_mean=nan,
            negative_mean=nan,
            max=nan,
            min=nan,
        )
    positives = data[data > 0]
    negatives = data[data < 0]
    return Distribution(
        label=label,
        count=int(data.size),
        mean=float(data.mean()),
        sd=float(data.std(ddof=1)) if data.size > 1 else float("nan"),
        quantiles={q: float(data.quantile(q)) for q in REPORT_QUANTILES},
        positive=int(positives.size),
        negative=int(negatives.size),
        zero=int((data == 0).sum()),
        positive_mean=float(positives.mean()) if positives.size else float("nan"),
        negative_mean=float(negatives.mean()) if negatives.size else float("nan"),
        max=float(data.max()),
        min=float(data.min()),
    )


# --------------------------------------------------------------------------- #
# (c) Olay sayımı ve kümeleme
# --------------------------------------------------------------------------- #
def rolling_quantile_threshold(
    series: pd.Series, *, quantile: float, window: int
) -> pd.Series:
    """Her damga için, kendisinden ÖNCEKİ `window` kaydın persentili.

    `shift(1)` şart: cari kayıt pencereye girseydi olay kendi eşiğini tanımlardı ve
    yeterince büyük bir sıçrama her zaman kendi p99'unu aşardı. Pencere dolmamışsa
    değer `nan`dır — kısmi pencereyle hesaplanan bir p99, erken dönemin eşiğini geç
    dönemden sistematik olarak farklı kılardı.
    """
    return series.shift(1).rolling(window=window, min_periods=window).quantile(quantile)


def flag_relative(
    series: pd.Series, *, quantile: float, window: int, side: str
) -> pd.Series:
    """Göreli eşiği aşan damgalar. `side`: "positive" üst kuyruk, "negative" alt kuyruk.

    Alt kuyruk için eşik simetrik persentildir (`1 - quantile`) ve karşılaştırma
    `<=` yönündedir: "p99'un altında" demek negatif kuyrukta p1'in altında demektir.
    """
    if side not in {"positive", "negative"}:
        raise ValueError(f"bilinmeyen kuyruk: {side!r}")
    if side == "positive":
        threshold = rolling_quantile_threshold(series, quantile=quantile, window=window)
        return (series > threshold) & threshold.notna()
    threshold = rolling_quantile_threshold(series, quantile=1.0 - quantile, window=window)
    return (series < threshold) & threshold.notna()


def flag_absolute(series: pd.Series, *, annual: float, side: str) -> pd.Series:
    if side not in {"positive", "negative"}:
        raise ValueError(f"bilinmeyen kuyruk: {side!r}")
    level = deannualize(annual)
    return series > level if side == "positive" else series < -level


@dataclass(frozen=True, kw_only=True)
class Episode:
    start: pd.Timestamp
    end: pd.Timestamp
    stamps: int

    @property
    def bars(self) -> int:
        """Epizodun kapsadığı 4H bar sayısı: her 8 saatlik damga iki bar sürer."""
        return self.stamps * BARS_PER_PERIOD


def cluster(stamps: Sequence[pd.Timestamp], *, cooldown_periods: int = 1) -> list[Episode]:
    """Damgaları epizotlara indirger.

    `cooldown_periods=1` bitişik damga kuralıdır: yalnızca tam 8 saat arayla gelen
    damgalar birleşir. Daha büyük bir değer, aradaki eşik-altı damgaları tolere eder
    (24 saat = 3) — "bir damga düşüp geri çıktı" durumunu tek epizod sayar.
    """
    if cooldown_periods < 1:
        raise ValueError("cooldown_periods en az 1 olmalı")
    ordered = sorted(stamps)
    episodes: list[Episode] = []
    for stamp in ordered:
        if episodes:
            last = episodes[-1]
            steps = int(round((stamp - last.end).total_seconds() / 3600 / FUNDING_INTERVAL_HOURS))
            if steps <= cooldown_periods:
                episodes[-1] = Episode(start=last.start, end=stamp, stamps=last.stamps + 1)
                continue
        episodes.append(Episode(start=stamp, end=stamp, stamps=1))
    return episodes


@dataclass(frozen=True, kw_only=True)
class EventCount:
    threshold: str
    side: str
    stamps: int
    episodes_adjacent: int
    episodes_cooldown: int
    bars: int
    per_symbol: Mapping[str, int] = field(default_factory=dict)
    monthly: Mapping[str, int] = field(default_factory=dict)


def monthly_histogram(stamps: Sequence[pd.Timestamp]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for stamp in sorted(stamps):
        key = f"{stamp.year:04d}-{stamp.month:02d}"
        counts[key] = counts.get(key, 0) + 1
    return counts


def count_events(
    flags: Mapping[str, pd.Series], *, threshold: str, side: str
) -> EventCount:
    """Sembol bazında bayrakları tek bir olay sayımına indirger.

    Kümeleme SEMBOL BAZINDA yapılır, havuzda değil: iki sembolde aynı saatte görülen
    ekstrem, aynı epizodun iki ayağı değil iki ayrı gözlemdir ve havuzda birleştirmek
    olay sayısını sembol sayısına bağlardı.
    """
    per_symbol: dict[str, int] = {}
    all_stamps: list[pd.Timestamp] = []
    adjacent = 0
    cooled = 0
    bars = 0
    for symbol, flag in sorted(flags.items()):
        hits = list(flag.index[flag.fillna(False).astype(bool)])
        if not hits:
            continue
        per_symbol[symbol] = len(hits)
        all_stamps.extend(hits)
        episodes = cluster(hits, cooldown_periods=1)
        adjacent += len(episodes)
        cooled += len(cluster(hits, cooldown_periods=COOLDOWN_PERIODS))
        bars += sum(episode.bars for episode in episodes)
    return EventCount(
        threshold=threshold,
        side=side,
        stamps=len(all_stamps),
        episodes_adjacent=adjacent,
        episodes_cooldown=cooled,
        bars=bars,
        per_symbol=per_symbol,
        monthly=monthly_histogram(all_stamps),
    )


# --------------------------------------------------------------------------- #
# Veri çekme (bellekte; önbelleğe YAZILMAZ)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, kw_only=True)
class FetchTrace:
    """Çekimin KENDİ izi: kaç kayıt geldi, en eskisi neydi, sayfalama NEDEN durdu.

    Ayrı bir kayıt olmasının sebebi bu aracın ilk koşusudur: 13 sembolün hepsi dönem A
    penceresinde "0 kayıt" verdi ve rapor bunu `nan` dolu bir tabloyla, SIFIR çıkış
    koduyla bildirdi. O tabloda eksik olan tek bilgi, çekimin gerçekte NEREYE kadar
    gidebildiğiydi — `coverage` yalnızca pencerenin İÇİNE bakar, oysa teşhis pencerenin
    DIŞINDA duruyordu (kaynağın verdiği en eski damga). İz, "sembol listelenmemiş", "borsa o
    kadar geriye vermiyor" ve "sayfalamamız bozuk" durumlarını birbirinden ayırır.
    """

    symbol: str
    fetched: int          # sayfalarda GÖRÜLEN kayıt (pencere filtresinden ÖNCE)
    kept: int             # pencereye giren kayıt
    oldest_seen: pd.Timestamp | None
    newest_seen: pd.Timestamp | None
    pages: int
    stop_reason: str


def fetch_history(
    client: OKXClient, symbol: str, *, since: pd.Timestamp, until: pd.Timestamp, limit: int
) -> tuple[pd.Series, FetchTrace]:
    """`since`..`until` aralığındaki fonlama geçmişini sayfalayarak çeker.

    `core/data.py::fetch_funding` yerine ayrı bir sayfalama var çünkü o fonksiyon
    `data.funding_history_periods` (180) ile budar ve `data/cache/`e YAZAR; bu ölçüm
    yıllar istiyor ve önbelleğe dokunmamalı (modül başlığı).

    Seriyle birlikte `FetchTrace` döner: boş bir seri tek başına sebebini söylemez.
    """
    rows: dict[pd.Timestamp, float] = {}
    cursor_ms: int | None = None
    pages = 0
    fetched = 0
    oldest_seen: pd.Timestamp | None = None
    newest_seen: pd.Timestamp | None = None
    stop_reason = "pencere tamamlandı"

    while True:
        params: dict[str, str] = {"instId": symbol, "limit": str(limit)}
        if cursor_ms is not None:
            params["after"] = str(cursor_ms)
        page = client.get(FUNDING_ENDPOINT, params)
        pages += 1
        if not page:
            stop_reason = "boş sayfa (borsa daha geriye vermiyor)"
            break
        previous_cursor = cursor_ms
        oldest_ms: int | None = None
        for raw in page:
            funding_time = int(raw["fundingTime"])
            oldest_ms = funding_time if oldest_ms is None else min(oldest_ms, funding_time)
            stamp = pd.Timestamp(funding_time, unit="ms", tz="UTC")
            fetched += 1
            oldest_seen = stamp if oldest_seen is None else min(oldest_seen, stamp)
            newest_seen = stamp if newest_seen is None else max(newest_seen, stamp)
            if stamp >= until or stamp < since:
                continue
            rows[stamp] = float(raw["fundingRate"])
        cursor_ms = oldest_ms
        if cursor_ms is None:
            stop_reason = "imleç yok"
            break
        if cursor_ms == previous_cursor:
            stop_reason = "imleç ilerlemedi (uç `after`'ı yok sayıyor)"
            break
        if len(page) < limit:
            stop_reason = f"kısa sayfa ({len(page)} < {limit}) — sayfalama TABANI"
            break
        if pd.Timestamp(cursor_ms, unit="ms", tz="UTC") < since:
            stop_reason = "ısınma başlangıcının gerisine geçildi"
            break

    trace = FetchTrace(
        symbol=symbol,
        fetched=fetched,
        kept=len(rows),
        oldest_seen=oldest_seen,
        newest_seen=newest_seen,
        pages=pages,
        stop_reason=stop_reason,
    )
    if not rows:
        empty = pd.Series(
            [], index=pd.DatetimeIndex([], tz="UTC", name="ts"), dtype="float64", name=symbol
        )
        return empty, trace
    series = pd.Series(rows, dtype="float64").sort_index()
    series.index.name = "ts"
    series.name = symbol
    return series, trace


def format_traces(traces: Sequence[FetchTrace], *, start: pd.Timestamp) -> list[str]:
    """Çekim izi — kapsam tablosundan ÖNCE gelir çünkü onu okunur kılan şey budur.

    "Pencerede 0 kayıt" satırı tek başına üç farklı sebebi anlatabilir; hangisi olduğu
    yalnızca ÇEKİLEN aralığa bakılarak ayırt edilir.
    """
    out = [
        "",
        "=" * 78,
        "(a0) ÇEKİM İZİ — borsa gerçekte nereye kadar veriyor?",
        "=" * 78,
        "'çekilen' pencere filtresinden ÖNCEki ham kayıt sayısıdır; 'en eski' o kayıtların",
        "en eskisi. Dönem başı hedefi: " + f"{start:%Y-%m-%d}. En eski kayıt bu tarihten",
        "YENİYSE borsa o kadar geriye vermiyor demektir — pencerede veri olmaması bizim",
        "filtremizin değil, kaynağın sınırıdır.",
        "",
        f"{'sembol':22s} {'sayfa':>5s} {'çekilen':>8s} {'pencerede':>10s} {'en eski':16s} {'en yeni':16s}  durma sebebi",
    ]
    for item in traces:
        oldest = f"{item.oldest_seen:%Y-%m-%d %H:%M}" if item.oldest_seen is not None else "—"
        newest = f"{item.newest_seen:%Y-%m-%d %H:%M}" if item.newest_seen is not None else "—"
        out.append(
            f"{item.symbol:22s} {item.pages:5d} {item.fetched:8d} {item.kept:10d} "
            f"{oldest:16s} {newest:16s}  {item.stop_reason}"
        )
    floors = [item.oldest_seen for item in traces if item.oldest_seen is not None]
    if floors:
        floor = max(floors)
        out.append("")
        out.append(
            f"Sayfalama TABANI (en geç kalan 'en eski' kayıt): {floor:%Y-%m-%d %H:%M} UTC — "
            f"dönem başından {(floor - start).days} gün SONRA."
            if floor > start
            else f"Sayfalama tabanı {floor:%Y-%m-%d %H:%M} UTC: dönem başını kapsıyor."
        )
    return out


# --------------------------------------------------------------------------- #
# Raporlama
# --------------------------------------------------------------------------- #
def _pct(value: float) -> str:
    return "—" if value != value else f"{value * 100:.4f}%"


def _ann(value: float) -> str:
    return "—" if value != value else f"{annualize(value) * 100:7.1f}%"


def format_coverage(items: Sequence[Coverage], *, start: pd.Timestamp, end: pd.Timestamp) -> list[str]:
    out = [
        "",
        "=" * 78,
        f"(a) VERİ KAPSAMI — dönem A penceresi {start:%Y-%m-%d} → {end:%Y-%m-%d}",
        "=" * 78,
        "Beklenen sayı SEMBOLÜN İLK KAYDINDAN itibaren sayılır (listeleme tarihi bir",
        "veri boşluğu değildir). 'eksik' = beklenen − gerçekleşen, o aralığın içinde.",
        "",
        f"{'sembol':22s} {'ilk kayıt':16s} {'son kayıt':16s} {'gerçek':>7s} {'beklenen':>9s} {'eksik':>6s} {'tam%':>7s} {'boşluk':>7s}",
    ]
    for item in items:
        if item.observed == 0:
            out.append(f"{item.symbol:22s} {'— dönemde kayıt yok':>16s}")
            continue
        out.append(
            f"{item.symbol:22s} {item.first:%Y-%m-%d %H:%M} {item.last:%Y-%m-%d %H:%M} "
            f"{item.observed:7d} {item.expected:9d} {item.missing:6d} "
            f"{item.completeness * 100:6.2f}% {len(item.gaps):7d}"
        )
    biggest = [
        (gap_len, sym.symbol, a, b)
        for sym in items
        for (a, b, gap_len) in sym.gaps
    ]
    biggest.sort(reverse=True)
    if biggest:
        out.append("")
        out.append("En büyük veri boşlukları (eksik periyot sayısına göre, ilk 10):")
        for gap_len, symbol, a, b in biggest[:10]:
            out.append(
                f"  {symbol:22s} {a:%Y-%m-%d %H:%M} → {b:%Y-%m-%d %H:%M}  "
                f"{gap_len} periyot ({gap_len * FUNDING_INTERVAL_HOURS} saat)"
            )
    else:
        out.append("")
        out.append("Veri boşluğu yok: her sembol kendi ilk kaydından itibaren kesintisiz.")
    return out


def format_distribution(items: Sequence[Distribution]) -> list[str]:
    out = [
        "",
        "=" * 78,
        "(b) FONLAMA ORANI DAĞILIMI (periyot başına, 8 saatlik oran)",
        "=" * 78,
        "",
        f"{'sembol':22s} {'n':>6s} " + " ".join(f"{'p' + str(int(q * 100)):>9s}" for q in REPORT_QUANTILES) + f" {'ort':>9s} {'sd':>9s}",
    ]
    for item in items:
        cells = " ".join(f"{item.quantiles[q] * 100:9.4f}" for q in REPORT_QUANTILES)
        out.append(
            f"{item.label:22s} {item.count:6d} {cells} {item.mean * 100:9.4f} {item.sd * 100:9.4f}"
        )
    out.append("")
    out.append("Aynı tablo YILLIKLANDIRILMIŞ (× 3 × 365, basit ölçekleme):")
    out.append(
        f"{'sembol':22s} {'n':>6s} " + " ".join(f"{'p' + str(int(q * 100)):>9s}" for q in REPORT_QUANTILES)
    )
    for item in items:
        cells = " ".join(f"{annualize(item.quantiles[q]) * 100:9.2f}" for q in REPORT_QUANTILES)
        out.append(f"{item.label:22s} {item.count:6d} {cells}")

    out.append("")
    out.append("(e) POZİTİF ↔ NEGATİF KUYRUK (simetri kontrolü)")
    out.append(
        f"{'sembol':22s} {'poz n':>7s} {'neg n':>7s} {'sıfır':>6s} {'poz%':>7s} "
        f"{'poz ort':>10s} {'neg ort':>10s} {'azami':>10s} {'asgari':>10s}"
    )
    for item in items:
        share = item.positive / item.count * 100 if item.count else float("nan")
        out.append(
            f"{item.label:22s} {item.positive:7d} {item.negative:7d} {item.zero:6d} "
            f"{share:6.2f}% {item.positive_mean * 100:10.4f} {item.negative_mean * 100:10.4f} "
            f"{item.max * 100:10.4f} {item.min * 100:10.4f}"
        )
    return out


def format_events(counts: Sequence[EventCount], *, window: int) -> list[str]:
    out = [
        "",
        "=" * 78,
        "(c) OLAY SAYIMI",
        "=" * 78,
        f"Göreli eşik: sembolün KENDİ geçmişinin persentili, kayan pencere {window} periyot",
        f"  ({window / PERIODS_PER_DAY:.0f} gün), pencere cari kaydı DIŞLAR (shift(1)) ve",
        "  dolmadan eşik üretmez. Isınma dönem A'nın öncesinden alınır, içinden yenmez.",
        f"Mutlak eşik: yıllıklandırılmış seviye; periyot karşılığı = ann / {PERIODS_PER_YEAR}.",
        "Kümeleme: 'bitişik' = ardışık damgalar tek olay; 'soğuma' = yeni olay için eşiğin",
        f"  altında en az {COOLDOWN_PERIODS} damga (24 saat). Kümeleme SEMBOL bazındadır.",
        f"4H bar: her damga {BARS_PER_PERIOD} bar sürer; 'bar' kolonu bitişik epizotların toplam barı.",
        "",
        f"{'eşik':24s} {'kuyruk':9s} {'damga':>7s} {'olay(bitişik)':>14s} {'olay(soğuma)':>13s} {'4H bar':>7s} {'sembol':>7s}",
    ]
    for item in counts:
        out.append(
            f"{item.threshold:24s} {item.side:9s} {item.stamps:7d} "
            f"{item.episodes_adjacent:14d} {item.episodes_cooldown:13d} {item.bars:7d} "
            f"{len(item.per_symbol):7d}"
        )
    out.append("")
    out.append("Sembol bazında dağılım (damga sayısı):")
    for item in counts:
        if not item.per_symbol:
            out.append(f"  {item.threshold} / {item.side}: olay yok")
            continue
        cells = ", ".join(
            f"{sym.split('-')[0]}={n}"
            for sym, n in sorted(item.per_symbol.items(), key=lambda kv: (-kv[1], kv[0]))
        )
        out.append(f"  {item.threshold} / {item.side}: {cells}")
    return out


def format_monthly(counts: Sequence[EventCount]) -> list[str]:
    out = [
        "",
        "=" * 78,
        "(d) OLAYLARIN ZAMAN DAĞILIMI — aylık histogram (damga sayısı)",
        "=" * 78,
    ]
    months = sorted({month for item in counts for month in item.monthly})
    if not months:
        out.append("Hiçbir eşikte olay yok.")
        return out
    for item in counts:
        if not item.monthly:
            continue
        peak = max(item.monthly.values())
        out.append("")
        out.append(f"{item.threshold} / {item.side}  (toplam {item.stamps} damga)")
        for month in months:
            value = item.monthly.get(month, 0)
            width = 0 if peak <= 0 else int(round(value / peak * 48))
            out.append(f"  {month}  {value:5d} {'█' * width}")
    return out


# --------------------------------------------------------------------------- #
# Koşu
# --------------------------------------------------------------------------- #
def measure(
    series_by_symbol: Mapping[str, pd.Series],
    *,
    start: pd.Timestamp,
    end: pd.Timestamp,
    window: int,
) -> tuple[list[Coverage], list[Distribution], list[EventCount]]:
    """Saf ölçüm: çekilmiş serileri rapor parçalarına çevirir (ağ erişimi yok)."""
    coverages: list[Coverage] = []
    distributions: list[Distribution] = []
    pooled: list[float] = []
    in_period: dict[str, pd.Series] = {}

    for symbol in sorted(series_by_symbol):
        series = series_by_symbol[symbol]
        coverages.append(coverage(series, start=start, end=end))
        window_values = series.loc[(series.index >= start) & (series.index < end)]
        in_period[symbol] = window_values
        distributions.append(distribution(list(window_values.to_numpy()), label=symbol))
        pooled.extend(float(v) for v in window_values.to_numpy())
    distributions.append(distribution(pooled, label=f"HAVUZ ({len(series_by_symbol)} sembol)"))

    counts: list[EventCount] = []
    for quantile in RELATIVE_QUANTILES:
        for side in ("positive", "negative"):
            flags = {}
            for symbol, series in series_by_symbol.items():
                flag = flag_relative(series, quantile=quantile, window=window, side=side)
                # Isınma penceresi dönem A'nın ÖNCESİNDEN alınır; olay YALNIZCA dönem içinde sayılır.
                flags[symbol] = flag.loc[(flag.index >= start) & (flag.index < end)]
            label = f"göreli p{round(quantile * 100)}" if side == "positive" else f"göreli p{round((1 - quantile) * 100)}"
            counts.append(count_events(flags, threshold=label, side=side))

    for annual in ABSOLUTE_ANNUAL:
        for side in ("positive", "negative"):
            flags = {
                symbol: flag_absolute(series, annual=annual, side=side)
                for symbol, series in in_period.items()
            }
            sign = "+" if side == "positive" else "−"
            counts.append(
                count_events(flags, threshold=f"mutlak {sign}%{annual * 100:.0f} yıllık", side=side)
            )
    return coverages, distributions, counts


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    start = pd.Timestamp(args.start)
    end = pd.Timestamp(args.end)
    cutoff = pd.Timestamp(PERIOD_A_CUTOFF)
    if end > cutoff:
        logger.error(
            "--end (%s) dönem A kesimini (%s) aşıyor. Dönem B bir sonraki tezin OOS "
            "penceresidir ve bu araç ona BAKAMAZ (docs/backtest.md > 6.1).",
            end,
            cutoff,
        )
        return 2
    if start >= end:
        logger.error("--start (%s) --end'den (%s) önce olmalı", start, end)
        return 2

    layer = resolve_layer(load_config(args.config), args.layer)
    symbols = list(args.symbols) if args.symbols else list(layer.symbols or [])
    if not symbols:
        logger.error("katman %r sabit bir evren taşımıyor; --symbols verin", args.layer)
        return 2
    unknown = [s for s in symbols if layer.symbols and s not in layer.symbols]
    if unknown:
        logger.error("evren dışı sembol: %s", ", ".join(unknown))
        return 2

    warmup_start = start - pd.Timedelta(hours=FUNDING_INTERVAL_HOURS * args.window_periods)
    logger.info(
        "dönem A: %s → %s | ısınma %s'ten (%d periyot) | %d sembol",
        start,
        end,
        warmup_start,
        args.window_periods,
        len(symbols),
    )

    client = OKXClient.from_config(layer.config)
    series_by_symbol: dict[str, pd.Series] = {}
    traces: list[FetchTrace] = []
    failures: list[str] = []
    for symbol in symbols:
        try:
            series, trace = fetch_history(
                client, symbol, since=warmup_start, until=end, limit=args.request_limit
            )
        except Exception as exc:  # ölçüm aracı: tek sembolün hatası koşuyu düşürmez
            logger.warning("%s fonlama geçmişi alınamadı: %s", symbol, exc)
            failures.append(symbol)
            series = pd.Series(
                [], index=pd.DatetimeIndex([], tz="UTC", name="ts"), dtype="float64", name=symbol
            )
            trace = FetchTrace(
                symbol=symbol,
                fetched=0,
                kept=0,
                oldest_seen=None,
                newest_seen=None,
                pages=0,
                stop_reason=f"HATA: {exc}",
            )
        logger.info(
            "%-22s pencerede %5d kayıt (çekilen %5d, en eski %s)",
            symbol,
            series.size,
            trace.fetched,
            f"{trace.oldest_seen:%Y-%m-%d}" if trace.oldest_seen is not None else "—",
        )
        series_by_symbol[symbol] = series
        traces.append(trace)

    coverages, distributions, counts = measure(
        series_by_symbol, start=start, end=end, window=args.window_periods
    )

    lines: list[str] = [
        "",
        "#" * 78,
        "# FONLAMA DAĞILIMI ÖLÇÜMÜ — salt okunur, ölçümün parçası DEĞİL",
        "# Getiri/R/PnL hesaplanmaz. Eşik seçimi bu raporun sonucu DEĞİLDİR.",
        f"# Katman: {args.layer} | Pencere: dönem A {start:%Y-%m-%d} → {end:%Y-%m-%d}",
        f"# Kayan pencere: {args.window_periods} periyot | Soğuma: {COOLDOWN_PERIODS} periyot (24s)",
        "#" * 78,
    ]
    lines += format_traces(traces, start=start)
    lines += format_coverage(coverages, start=start, end=end)
    lines += format_distribution(distributions)
    lines += format_events(counts, window=args.window_periods)
    lines += format_monthly(counts)
    lines += [
        "",
        "=" * 78,
        "NOT: Bu rapor bir eşik ÖNERMEZ. Hangi eşiğin seçileceği ayrı bir adımda,",
        "ön-kayıtla belirlenir (docs/backtest.md > 7). Burada yalnızca 'kaç olay var'",
        "sorusu cevaplandı; 'o olaylar ne kazandırdı' sorusu bilinçli olarak SORULMADI.",
        "=" * 78,
    ]
    print("\n".join(lines))

    # VERİ KAPISI. Bu aracın ilk koşusu 13 sembolün hepsinde pencerede sıfır kayıt buldu
    # ve bunu `nan` dolu bir tabloyla, SIFIR çıkış koduyla bildirdi — yani "ölçtük ve
    # bulamadık" ile "hiç ölçemedik" aynı hücreye yazıldı. Bu, `core/metrics.py`nin
    # "veri yoksa nan, 0.0 değil" kuralının çıkış kodundaki karşılığıdır ve aynı
    # gerekçeyle bir KAPIDIR: yeşil bir koşu, okunabilir bir rapor demektir.
    measured = sum(1 for item in coverages if item.observed > 0)
    if measured == 0:
        logger.error(
            "HİÇBİR sembolde dönem A penceresinde (%s → %s) kayıt yok. "
            "Rapordaki her sayı `nan`dır ve hiçbir soru cevaplanmadı.",
            start,
            end,
        )
        logger.error(
            "Teşhis için (a0) ÇEKİM İZİ bölümüne bakın: sayfalama tabanı dönem başından "
            "SONRAYSA sebep bizim filtremiz değil, kaynağın geçmiş sınırıdır."
        )
        return 3
    if failures:
        logger.warning("çekilemeyen sembol(ler): %s", ", ".join(failures))
    logger.info("%d/%d sembolde dönem A verisi var", measured, len(coverages))
    return 0


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--layer", default="ema", help="sembol evreninin alınacağı katman")
    parser.add_argument("--config", default=None)
    parser.add_argument("--symbols", nargs="*", default=None, help="evrenin alt kümesi")
    parser.add_argument("--start", default=PERIOD_A_START)
    parser.add_argument(
        "--end",
        default=PERIOD_A_CUTOFF,
        help="dönem A kesimi; aşan değer HATA (dönem B'ye bakılmaz)",
    )
    parser.add_argument(
        "--window-periods",
        type=int,
        default=DEFAULT_WINDOW_PERIODS,
        help=f"göreli eşiğin kayan penceresi (varsayılan {DEFAULT_WINDOW_PERIODS} = 90 gün)",
    )
    parser.add_argument("--request-limit", type=int, default=100, help="OKX sayfa boyu")
    return parser.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(main())
