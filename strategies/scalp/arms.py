"""15 dakikalık scalp katmanının BEŞ ORTAK KOLU — iki modelin de birebir aynı gördüğü mantık.

Model 11 (`scalp_bandit`) ve model 12 (`scalp_fixed`) arasındaki tek fark **hangi kolun
oynanacağına nasıl karar verildiğidir.** Kolların kendisi — hangi barda, hangi sembolde,
hangi yönde bir kurulum gördükleri — tek kopya olarak burada yaşar. Kopyalansaydı iki
modelin farkı zamanla "adaptasyonun katkısı" olmaktan çıkar, iki ayrı kol kümesinin farkı
olurdu; oysa ölçülmek istenen tam olarak birincisidir.

**STOP her kolda aynı, HEDEF her kolda kendi yapısından gelir.** İkisi bilinçli olarak
farklı kurallara bağlıdır:

- *Stop mesafesi* yalnızca bir risk tercihi değil, aynı zamanda MALİYET ÖLÇEĞİDİR
  (CLAUDE.md kural 14): boyut `risk / |giriş − stop|` olduğu için dar stop kuran kol aynı
  1R'yi daha büyük notional ile taşır ve R başına daha çok komisyon+kayma öder. Kollar
  farklı stop mantıkları kullansaydı (biri fitil, biri aralık, biri ATR) kol bazlı ortalama
  R tablosu bir SİNYAL karşılaştırması değil bir stop-mesafesi karşılaştırması olurdu — ve
  bandit sinyali değil, en ucuz stop ölçeğini öğrenirdi. Bu yüzden mesafe her kolda
  `stop_atr_multiple × ATR`tır.
- *Hedef* maliyet ölçeğine dokunmaz (1R stop'tan tanımlıdır) ve iki parçadan oluşur:
  **projeksiyon** (`target_reward_risk × stop mesafesi`) ile kolun kendi **yapısal
  engeli** (VWAP geri çekilmesinde günün zirvesi, kırılımda aralığın ölçülü hareketi, RSI
  dönüşünde Bollinger orta bandı, momentumda patlamanın kendi büyüklüğü, funding fade'inde
  VWAP). Hedef ikisinin YAKIN olanıdır: engel yoldaysa oraya kadar, yolda engel yoksa
  projeksiyona kadar.

  Neden ikisi birden: yalnızca projeksiyon kullanmak her kurulumun hedef/stop oranını aynı
  sayıya sabitler ve 1.5R kapısını hiçbir şeyi elemeyen ölü bir koda çevirirdi. Yalnızca
  yapısal seviye kullanmak ise tersini yapardı: 15 dakikalık barda yapısal hedefler
  çoğunlukla stop mesafesinden yakındır (%1'lik stop tabanıyla birlikte neredeyse hiçbir
  kurulum 1.5R'ye ulaşmaz) ve iki model de hiç işlem açamazdı — ölçüm başlamadan biterdi.
  Yakın olanı almak ikisini de çözer: engeli yakın olan kurulum kapıda elenir (kapı
  canlıdır), önü açık olan kurulum projeksiyonuyla oynanır (işlem üretilir).

**Ortak kapılar burada değil, `strategies/scalp/model.py`'de.** Stop tabanı (%1) ve 1.5R
hedef/stop kapısı kolun değil MODELİN kuralıdır: iki model de aynı kapıdan geçer ve
atlanan her kurulum gerekçesiyle loglanır (kural 14'ün "atlama sessiz olamaz" şartı).

Rollere dikkat: bu modül boyut/komisyon/bakiye hesaplamaz (kural 1/2/3/7) ve kendi gösterge
matematiğini yazmaz — hepsi `core/indicators.py`'dedir.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Sequence

import pandas as pd

from core.indicators import (
    anchored_vwap,
    average_true_range,
    bars_until,
    bollinger,
    ema,
    rsi,
    sma,
)
from strategies.base import Direction, MarketData

logger = logging.getLogger(__name__)

# --- Kol eşikleri. Kolun TEZİNİ tanımlarlar, ölçümün koşullarını değil, bu yüzden
# config.yaml'da değil burada dururlar: config "tüm modeller için birebir aynı ölçüm
# koşulu" sözleşmesidir (kural 6); "RSI(2) 5'in altında" ise bu kolun ne olduğudur.
OPENING_RANGE_BARS = 4  # ilk 4 bar = günün ilk saati (15m × 4)
OPENING_RANGE_MAX_AGE_BARS = 16  # kırılım günün ilk 5 saatinde aranır, akşam değil
VOLUME_LOOKBACK = 20
VOLUME_CONFIRM_MULTIPLE = 1.5
RSI_PERIOD = 2
RSI_OVERSOLD = 5.0
RSI_OVERBOUGHT = 95.0
BOLLINGER_PERIOD = 20
BOLLINGER_STD = 2.0
RANGE_REGIME_PCT = 0.002  # |EMA20 − EMA50| / kapanış bunun altındaysa "aralık rejimi"
TREND_FAST = 20
TREND_SLOW = 50
BURST_BARS = 3
FUNDING_LOOKBACK = 8  # sıçramanın kıyaslandığı önceki periyot sayısı (8 × 8s ≈ 2.7 gün)
FUNDING_SPIKE_MULTIPLE = 2.0
FUNDING_SPIKE_FLOOR = 0.0005  # periyot başına %0.05; altındaki "sıçrama" gürültüdür
FUNDING_FRESH_HOURS = 2.0  # sıçrama TAZE olmalı: yayınlanalı bu kadar saatten az geçmiş


@dataclass(frozen=True, kw_only=True)
class ArmParams:
    """İki modelin de config'ten okuduğu ortak kurulum parametreleri."""

    atr_period: int
    stop_atr_multiple: float
    target_reward_risk: float


@dataclass(frozen=True, kw_only=True)
class ArmSetup:
    """Bir kolun önerdiği kurulum. Henüz sinyal DEĞİLDİR: model kapılarından geçmemiştir."""

    arm: str
    symbol: str
    direction: Direction
    entry_price: float  # `as_of` kapanışı — dolum bir sonraki barın açılışındadır (kural 13)
    stop_price: float
    target_price: float
    detail: str  # deftere yazılacak serbest gerekçe metni

    @property
    def stop_distance(self) -> float:
        return abs(self.entry_price - self.stop_price)

    @property
    def stop_distance_pct(self) -> float:
        return self.stop_distance / self.entry_price

    @property
    def reward_risk(self) -> float:
        return abs(self.target_price - self.entry_price) / self.stop_distance


@dataclass(frozen=True, kw_only=True)
class SymbolView:
    """Tek sembolün `as_of` barına kadar kesilmiş görünümü (kural 12)."""

    symbol: str
    frame: pd.DataFrame
    as_of: pd.Timestamp
    close: float
    atr: float
    funding: pd.Series | None


def symbol_views(market: MarketData, *, atr_period: int) -> list[SymbolView]:
    """`as_of` barını taşıyan ve ATR'si hesaplanabilen semboller, ADI SIRALI.

    Sıra sözlük sırasına bırakılmaz: kol seçimi sabit tohumlu bir çekilişle yapılır ve
    aday listesinin sırası o çekilişin tekrarlanabilirliğinin parçasıdır (bkz.
    strategies/random_ctrl.py'deki aynı gerekçe).

    Eleme ölçütü bir görüş değil, kurulumun hesaplanabilirliğidir: ATR'si olmayan sembolde
    stop mesafesi üretilemez.
    """
    views: list[SymbolView] = []
    for symbol in sorted(market.ohlcv):
        frame = bars_until(market.ohlcv[symbol], market.as_of)
        if frame.empty or frame.index[-1] != market.as_of:
            continue
        atr = average_true_range(frame, atr_period)
        if atr is None or atr <= 0.0:
            continue
        close = float(frame["close"].iloc[-1])
        if close <= 0.0:
            continue
        views.append(
            SymbolView(
                symbol=symbol,
                frame=frame,
                as_of=market.as_of,
                close=close,
                atr=atr,
                funding=market.funding.get(symbol),
            )
        )
    return views


def _maybe_setup(
    *,
    arm: str,
    view: SymbolView,
    direction: Direction,
    params: ArmParams,
    obstacle: float,
    detail: str,
) -> ArmSetup | None:
    """Ortak STOP geometrisi + hedef: projeksiyon ile kolun yapısal engelinin YAKIN olanı.

    `obstacle` kolun kendi tezinden gelen seviyedir. Girişin GERİSİNDE kalıyorsa (fiyat
    günün zirvesini çoktan aşmıştır) yolda engel yok demektir ve hedef projeksiyondur;
    önünde duruyorsa hedef odur — bilinen bir seviyenin ötesini hedeflemek, o seviyenin
    orada olmadığını varsaymak olurdu.

    Hedefin girişin doğru tarafında olması ayrıca bir geometri şartıdır: `core/validate.py`
    aksini programlama hatası sayar ve modelin TÜM turunu düşürür (kural 8). Projeksiyon
    her zaman doğru taraftadır; engel ise yalnızca doğru taraftayken kullanılır.
    """
    distance = view.atr * params.stop_atr_multiple
    sign = 1.0 if direction == "long" else -1.0
    projected = view.close + sign * distance * params.target_reward_risk
    ahead = (obstacle - view.close) * sign > 0.0
    nearest = min(projected, obstacle) if direction == "long" else max(projected, obstacle)
    return ArmSetup(
        arm=arm,
        symbol=view.symbol,
        direction=direction,
        entry_price=view.close,
        stop_price=view.close - sign * distance,
        target_price=nearest if ahead else projected,
        detail=detail,
    )


# --------------------------------------------------------------------------- #
# 1) VWAP geri çekilme
# --------------------------------------------------------------------------- #
def vwap_pullback(views: Sequence[SymbolView], params: ArmParams) -> list[ArmSetup]:
    """Gün-çapalı VWAP'e TREND YÖNÜNDE dokunuş.

    Tez: gün içi trend süren bir sembolde VWAP, gün boyunca biriken ortalama maliyettir;
    fiyat oraya geri çektiğinde trend yönünde devam etme olasılığı, rastgele bir bardan
    yüksektir. Çapa günün ilk barıdır (00:00 UTC) — "gün içi" ifadesinin tek ve
    denetlenebilir tanımı takvim günüdür.

    Koşul üç parçalıdır: (a) trend yönü EMA20/EMA50 ile sabitlenir, (b) fiyat VWAP'e
    DOKUNUR (barın aralığı VWAP'i içerir), (c) bar trend yönünde KAPANIR — dokunup
    öbür tarafa geçen bar bir geri çekilme değil, bir kırılımdır.
    """
    setups: list[ArmSetup] = []
    for view in views:
        anchor = view.as_of.normalize()
        vwap = anchored_vwap(view.frame, anchor=anchor)
        if vwap is None or vwap.bars < 2:
            continue
        fast = ema(view.frame["close"], TREND_FAST)
        slow = ema(view.frame["close"], TREND_SLOW)
        if fast is None or slow is None or fast == slow:
            continue

        bar = view.frame.iloc[-1]
        low, high = float(bar["low"]), float(bar["high"])
        touched = low <= vwap.value <= high
        if not touched:
            continue

        direction: Direction | None = None
        if fast > slow and view.close > vwap.value:
            direction = "long"
        elif fast < slow and view.close < vwap.value:
            direction = "short"
        if direction is None:
            continue

        day = view.frame.loc[anchor:]
        # Hedef: günün trend yönündeki ucu. Tez "trend sürüyor" olduğuna göre hareketin
        # sınandığı yer günün o ana kadarki uç noktasıdır; ötesi tahmin, berisi ise
        # tezin henüz doğrulanmadığı bölgedir.
        target = float(day["high"].max()) if direction == "long" else float(day["low"].min())
        setup = _maybe_setup(
                arm="vwap_pullback",
                view=view,
                direction=direction,
                params=params,
                obstacle=target,
                detail=(
                    f"VWAP geri çekilme: gün çapası {anchor:%Y-%m-%d} 00:00 UTC, "
                    f"VWAP={vwap.value:.6g} ({vwap.bars} bar); bar aralığı "
                    f"[{low:.6g}, {high:.6g}] VWAP'e dokundu ve {view.close:.6g} ile "
                    f"{'üstünde' if direction == 'long' else 'altında'} kapandı; "
                    f"trend EMA{TREND_FAST}={fast:.6g} "
                    f"{'>' if fast > slow else '<'} EMA{TREND_SLOW}={slow:.6g}; "
                    f"engel günün {'zirvesi' if direction == 'long' else 'dibi'} {target:.6g}"
                ),
        )
        if setup is not None:
            setups.append(setup)
    return setups


# --------------------------------------------------------------------------- #
# 2) Açılış aralığı kırılımı
# --------------------------------------------------------------------------- #
def opening_range_breakout(views: Sequence[SymbolView], params: ArmParams) -> list[ArmSetup]:
    """Günün ilk 4 barının (ilk saat) aralığından hacim teyitli kırılım.

    Tez: gün başındaki denge aralığı, günün geri kalanında bir referans seviyedir; oradan
    HACİMLE çıkış, gün içi yönün belirlendiği andır. Hacim teyidi şart: hacimsiz kırılım
    aralığın içine geri düşen bir fitil olmaya en yatkın harekettir.

    Kırılım günün ilk `OPENING_RANGE_MAX_AGE_BARS` barında aranır. Sınır olmasaydı aynı
    aralık gün sonuna kadar geçerli sayılır ve kol, tezinin ("gün açılışı") ölçemeyeceği
    bir akşam hareketine işlem açardı.
    """
    setups: list[ArmSetup] = []
    for view in views:
        day = view.frame.loc[view.as_of.normalize():]
        if len(day) <= OPENING_RANGE_BARS:
            continue  # aralık henüz tamamlanmadı
        age = len(day) - OPENING_RANGE_BARS
        if age > OPENING_RANGE_MAX_AGE_BARS:
            continue

        opening = day.iloc[:OPENING_RANGE_BARS]
        high = float(opening["high"].max())
        low = float(opening["low"].min())
        if not high > low:
            continue

        volume = float(view.frame["volume"].iloc[-1])
        average = sma(view.frame["volume"], VOLUME_LOOKBACK)
        if average is None or average <= 0.0:
            continue
        if volume < average * VOLUME_CONFIRM_MULTIPLE:
            continue

        direction: Direction | None = None
        if view.close > high:
            direction = "long"
        elif view.close < low:
            direction = "short"
        if direction is None:
            continue

        # Hedef: klasik ölçülü hareket — kırılan seviyeden aralık genişliği kadar ileri.
        # Aralığın kendisi, kırılımın taşıyacağı hareketin piyasa tarafından üretilmiş
        # ölçüsüdür; sabit bir ATR katı bu bilgiyi atıp yerine bizim varsayımımızı koyardı.
        width = high - low
        target = (high + width) if direction == "long" else (low - width)
        setup = _maybe_setup(
                arm="opening_range_breakout",
                view=view,
                direction=direction,
                params=params,
                obstacle=target,
                detail=(
                    f"Açılış aralığı kırılımı: ilk {OPENING_RANGE_BARS} bar "
                    f"[{low:.6g}, {high:.6g}], kapanış {view.close:.6g} "
                    f"{'üstte' if direction == 'long' else 'altta'} ({age}. bar); "
                    f"hacim {volume:.6g} ≥ {VOLUME_CONFIRM_MULTIPLE:g}× "
                    f"SMA{VOLUME_LOOKBACK}({average:.6g}); engel ölçülü hareket "
                    f"{target:.6g} (aralık genişliği {width:.6g})"
                ),
        )
        if setup is not None:
            setups.append(setup)
    return setups


# --------------------------------------------------------------------------- #
# 3) RSI(2) aşırılık dönüşü
# --------------------------------------------------------------------------- #
def rsi2_reversal(views: Sequence[SymbolView], params: ArmParams) -> list[ArmSetup]:
    """RSI(2) aşırılığından ortalamaya dönüş — YALNIZCA aralık rejiminde.

    Tez: kısa pencereli RSI'ın uç değeri, aralık içinde hareket eden bir semboldeki geçici
    bir sapmadır ve ortalamaya döner. Rejim kapısı bu kolun can damarıdır: trend içinde
    aynı uç değer bir dönüş değil, hareketin GÜCÜDÜR — kapı olmadan kol, trendlere karşı
    işlem açan bir makineye dönüşür ve tezinden bambaşka bir şey ölçer.

    Rejim ölçüsü EMA20/EMA50 arasındaki göreli mesafedir: iki ortalama birbirine yapışıksa
    yön yoktur. Ayrı bir gösterge (ADX vb.) eklemek, aynı bilgiyi ikinci bir tanımla
    ölçmek olurdu.
    """
    setups: list[ArmSetup] = []
    for view in views:
        fast = ema(view.frame["close"], TREND_FAST)
        slow = ema(view.frame["close"], TREND_SLOW)
        if fast is None or slow is None:
            continue
        separation = abs(fast - slow) / view.close
        if separation >= RANGE_REGIME_PCT:
            continue  # trend rejimi: bu kol burada işlem açmaz

        strength = rsi(view.frame["close"], RSI_PERIOD)
        if strength is None:
            continue
        direction: Direction | None = None
        if strength <= RSI_OVERSOLD:
            direction = "long"
        elif strength >= RSI_OVERBOUGHT:
            direction = "short"
        if direction is None:
            continue

        # Hedef: Bollinger ORTA bandı. Kolun tezi "sapma ortalamaya döner"dir; o tezin
        # hedefi tanım gereği ortalamanın kendisidir. Karşı banda kadar hedeflemek,
        # dönüşün devam edip rejimi tersine çevireceğini varsaymak olurdu — bu kolun
        # iddiası o değil.
        bands = bollinger(view.frame["close"], BOLLINGER_PERIOD, BOLLINGER_STD)
        if bands is None:
            continue
        setup = _maybe_setup(
                arm="rsi2_reversal",
                view=view,
                direction=direction,
                params=params,
                obstacle=bands.middle,
                detail=(
                    f"RSI({RSI_PERIOD}) aşırılık dönüşü: RSI={strength:.2f} "
                    f"({'≤' if direction == 'long' else '≥'} "
                    f"{RSI_OVERSOLD if direction == 'long' else RSI_OVERBOUGHT:g}); "
                    f"aralık rejimi doğrulandı: |EMA{TREND_FAST}−EMA{TREND_SLOW}|/kapanış="
                    f"{separation * 100:.3f}% < {RANGE_REGIME_PCT * 100:.3f}%; "
                    f"engel Bollinger({BOLLINGER_PERIOD}) orta bandı {bands.middle:.6g}"
                ),
        )
        if setup is not None:
            setups.append(setup)
    return setups


# --------------------------------------------------------------------------- #
# 4) Momentum patlaması devamı
# --------------------------------------------------------------------------- #
def momentum_burst(views: Sequence[SymbolView], params: ArmParams) -> list[ArmSetup]:
    """Üst üste 3 bar aynı yönde + hacim teyidi → devam.

    Tez: kısa vadeli momentum kendini besler; art arda aynı yönde kapanan barlar ve
    genişleyen hacim, bir sonraki barda da aynı yönü favori yapar. Hacim koşulu burada da
    aynı gerekçeyle: hacimsiz üç bar bir patlama değil, bir sürüklenmedir.
    """
    setups: list[ArmSetup] = []
    for view in views:
        if len(view.frame) < BURST_BARS + 1:
            continue
        closes = view.frame["close"].to_numpy(dtype="float64")[-(BURST_BARS + 1):]
        changes = [closes[i + 1] - closes[i] for i in range(BURST_BARS)]
        if all(change > 0.0 for change in changes):
            direction: Direction = "long"
        elif all(change < 0.0 for change in changes):
            direction = "short"
        else:
            continue

        volume = float(view.frame["volume"].iloc[-1])
        average = sma(view.frame["volume"], VOLUME_LOOKBACK)
        if average is None or average <= 0.0 or volume < average * VOLUME_CONFIRM_MULTIPLE:
            continue

        burst = abs(closes[-1] - closes[0])
        move = burst / closes[0] * 100.0
        # Hedef: patlamanın kendi büyüklüğü kadar devam. Ölçü piyasadan gelir — "momentum
        # devam eder" tezinin doğal birimi, devam etmesi beklenen hareketin ta kendisidir.
        target = closes[-1] + (burst if direction == "long" else -burst)
        setup = _maybe_setup(
                arm="momentum_burst",
                view=view,
                direction=direction,
                params=params,
                obstacle=float(target),
                detail=(
                    f"Momentum patlaması: {BURST_BARS} bar üst üste "
                    f"{'yukarı' if direction == 'long' else 'aşağı'} (%{move:.2f} hareket); "
                    f"hacim {volume:.6g} ≥ {VOLUME_CONFIRM_MULTIPLE:g}× "
                    f"SMA{VOLUME_LOOKBACK}({average:.6g}); engel aynı büyüklükte devam "
                    f"{target:.6g}"
                ),
        )
        if setup is not None:
            setups.append(setup)
    return setups


# --------------------------------------------------------------------------- #
# 5) Funding sıçraması fade'i
# --------------------------------------------------------------------------- #
def funding_spike_fade(views: Sequence[SymbolView], params: ArmParams) -> list[ArmSetup]:
    """Funding aniden yükseldiğinde SHORT — kalabalıklaşmış long tarafını fade etmek.

    Tez: funding oranı long tarafın short tarafa ödediği bedeldir; ani yükselişi, kaldıraçlı
    long'ların kalabalıklaştığının ölçülebilir işaretidir ve kalabalık taraf tasfiyeye en
    açık taraftır. Bu kol tek yönlüdür (yalnızca short) — tezin simetrik hâli ("negatif
    funding'de long") ayrı bir tezdir ve ayrı ölçülmelidir; aynı kola koymak iki farklı
    sonucu tek ortalamada eritirdi.

    **Tazelik kapısı zorunlu.** Funding 8 saatte bir yayınlanır, bar ise 15 dakikalıktır:
    kapı olmasaydı aynı sıçrama 32 tur boyunca "yeni" sayılır ve kol tek bir olayı onlarca
    kez oynardı — hem işlem sayısını hem kol ortalamasını tek bir olaya bağlardı.
    """
    setups: list[ArmSetup] = []
    freshness = pd.Timedelta(hours=FUNDING_FRESH_HOURS)
    for view in views:
        series = view.funding
        if series is None or len(series) < FUNDING_LOOKBACK + 1:
            continue
        last_ts = series.index[-1]
        if view.as_of - last_ts > freshness:
            continue  # sıçrama taze değil: aynı olayı tekrar tekrar oynama

        latest = float(series.iloc[-1])
        history = series.iloc[-(FUNDING_LOOKBACK + 1):-1].to_numpy(dtype="float64")
        baseline = float(abs(history).mean())
        if latest < FUNDING_SPIKE_FLOOR:
            continue
        if latest < baseline * FUNDING_SPIKE_MULTIPLE:
            continue

        # Hedef: gün-çapalı VWAP. Kalabalık long tarafın fade'i, fiyatın günün ortalama
        # maliyetine geri dönmesi demektir; tezin hedefi o seviyedir. VWAP hesaplanamıyorsa
        # (gün başı barı yok, hacim sıfır) hedef de yoktur: kurulum kurulmaz.
        vwap = anchored_vwap(view.frame, anchor=view.as_of.normalize())
        if vwap is None:
            continue
        setup = _maybe_setup(
                arm="funding_spike_fade",
                view=view,
                direction="short",
                params=params,
                obstacle=vwap.value,
                detail=(
                    f"Funding sıçraması fade: son oran {latest * 100:.4f}% "
                    f"({last_ts:%Y-%m-%d %H:%M} UTC, {(view.as_of - last_ts).total_seconds() / 3600:.1f} "
                    f"saat önce) ≥ {FUNDING_SPIKE_MULTIPLE:g}× önceki {FUNDING_LOOKBACK} periyot "
                    f"ortalaması ({baseline * 100:.4f}%) ve tabanın "
                    f"({FUNDING_SPIKE_FLOOR * 100:.4f}%) üstünde; engel gün-çapalı VWAP "
                    f"{vwap.value:.6g}"
                ),
        )
        if setup is not None:
            setups.append(setup)
    return setups


ArmFunction = Callable[[Sequence[SymbolView], ArmParams], list[ArmSetup]]

# Kol sırası SABİTTİR: eşit ağırlıklı çekiliş ve bandit'in argmax'ı bu sıradan besleniyor.
# Sözlük sırası değişirse aynı tohumla aynı tur farklı kol seçebilirdi.
ARMS: dict[str, ArmFunction] = {
    "vwap_pullback": vwap_pullback,
    "opening_range_breakout": opening_range_breakout,
    "rsi2_reversal": rsi2_reversal,
    "momentum_burst": momentum_burst,
    "funding_spike_fade": funding_spike_fade,
}

ARM_NAMES: tuple[str, ...] = tuple(ARMS)


def propose_all(market: MarketData, params: ArmParams) -> dict[str, list[ArmSetup]]:
    """Her kolun o turdaki kurulumları. Kol hata verirse tur düşmez, kol boş geçer.

    Kolun patlaması modelin turunu tümden düşürseydi, tek bir sembolün bozuk serisi beş
    kolu birden susturur ve iki modelin de o turu sessizce boş geçerdi. Kol bazında
    izolasyon, hatayı hem daraltır hem görünür kılar.
    """
    views = symbol_views(market, atr_period=params.atr_period)
    results: dict[str, list[ArmSetup]] = {}
    for name, arm in ARMS.items():
        try:
            results[name] = arm(views, params)
        except Exception as exc:
            logger.error("scalp kolu %s %s barında hata verdi: %s", name, market.as_of, exc)
            results[name] = []
    return results
