"""Göstergelerin TEK uygulaması: ATR, RSI, Bollinger, EMA, SMA, Donchian.

Neden ortak modül: aynı göstergenin iki farklı uygulaması iki modelin aynı barda farklı
sayı görmesi demektir — bu da kural 5'i ("tüm stratejiler aynı anlık görüntüyü görür")
sayısal düzeyde deler. `generate_signals` içinde "hızlıca" yazılmış bir RSI, modellerin
sinyal farkını gösterge farkıyla karıştırır ve kıyası geçersizleştirir. Bu yüzden hiçbir
strateji kendi gösterge matematiğini yazmaz; hepsi buradan okur.

Bu modül SALT OKUNUR ve durumsuzdur: config okumaz, bakiye/pozisyon bilmez (kural 7),
yalnızca verilen çerçeveden sayı üretir. Periyot gibi parametreler çağıranın sorumluluğudur
— ATR periyodu projenin tek ATR tanımıdır ve `config.yaml`'ın `trailing.atr_period`
değerinden gelir (bkz. CLAUDE.md, "config.yaml Değerleri").

Tanım kararları (hepsi aynı gerekçeye dayanır: **denetlenebilirlik > gelenek**):

- **Yeterli bar yoksa `None` döner, kısmi pencereyle hesaplanmış bir sayı değil.** Yeni
  listelenmiş bir sembolde 30 barlık veriyle "200 EMA" üretmek, o sembolde rejim filtresini
  sessizce anlamsız kılardı; `None` çağıranı sinyal üretmemeye zorlar.
- **Pencere-yerel ortalamalar (ATR, RSI) Wilder yumuşatması kullanmaz.** Wilder özyinelemeli
  olduğu için sonuç çerçeveye kaç bar geçmiş verildiğine bağlıdır; önbellek farklı ısındığında
  ya da sembol geç listelendiğinde aynı bar için farklı sayı çıkar. Basit ortalama yalnızca
  son `period` bara bakar: aynı bar her zaman aynı sayıyı verir.
- **EMA'da bu kaçış yok** — EMA tanımı gereği özyinelemelidir. Bu yüzden başlangıç değeri
  açıkça ilk `period` barın SMA'sına sabitlenmiştir (pandas `ewm` varsayılanına bırakılmaz):
  aynı çerçeve her koşuda aynı EMA'yı verir ve tanım tek satırda denetlenebilir.
- **Bollinger sapması POPÜLASYON standart sapmasıdır (ddof=0).** Örneklem sapması (ddof=1)
  20 barlık pencerede bandı ~%2.6 genişletir; hangisi "doğru" olduğu değil, tek ve yazılı
  olması önemlidir.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Sequence

import numpy as np
import pandas as pd

PivotKind = Literal["low", "high"]


@dataclass(frozen=True, kw_only=True)
class BollingerBands:
    upper: float
    middle: float
    lower: float


@dataclass(frozen=True, kw_only=True)
class DonchianChannel:
    upper: float
    lower: float


@dataclass(frozen=True, kw_only=True)
class Pivot:
    """Zigzag dönüş noktası. `kind` kaynak tarayıcıdaki "dip"/"zirve" ayrımıdır."""

    time: pd.Timestamp
    price: float
    kind: PivotKind


def bars_until(frame: pd.DataFrame, as_of: pd.Timestamp) -> pd.DataFrame:
    """`as_of` dâhil, sonrasını atan dilim — look-ahead yasağının (kural 12) tek satırı.

    Stratejilerin `frame.tail(...)` ya da `iloc[-1]` ile "şimdi"yi tahmin etmesi kuralın en
    sık delindiği yerdir: anlık görüntü doğruysa ikisi aynı sonucu verir, bir sembol bir bar
    ileri kaçtığında vermez. Kesme tek yerde durursa hata da tek yerde aranır.
    """
    return frame.loc[:as_of]


def sma(series: pd.Series, period: int) -> float | None:
    """Son `period` değerin basit ortalaması; yeterli veri yoksa None."""
    _require_positive(period, "period")
    if len(series) < period:
        return None
    return float(series.to_numpy(dtype="float64")[-period:].mean())


def ema(series: pd.Series, period: int) -> float | None:
    """Üstel hareketli ortalama; ilk `period` barın SMA'sı ile tohumlanır.

    Tohumun açıkça yazılmasının nedeni modül docstring'inde: EMA özyinelemelidir ve
    başlangıç değeri kütüphane varsayılanına bırakılırsa tanım kodda görünmez olur.
    """
    _require_positive(period, "period")
    values = series.to_numpy(dtype="float64")
    if len(values) < period:
        return None
    alpha = 2.0 / (period + 1.0)
    current = float(values[:period].mean())
    for value in values[period:]:
        current = alpha * float(value) + (1.0 - alpha) * current
    return current


def rsi(series: pd.Series, period: int) -> float | None:
    """Son `period` değişimin kazanç/kayıp ortalamasından RSI; yeterli veri yoksa None.

    `rsi_series`in son değeridir — iki ayrı hesap tutmamak için oradan türetilir: bar bazlı
    seri ile "şimdiki" RSI ayrışırsa, aynı modelin pivotta okuduğu RSI ile eşik karşılaştırdığı
    RSI farklı tanımlardan gelirdi.
    """
    strength = rsi_series(series, period)
    if strength.empty:
        return None
    last = float(strength.iloc[-1])
    return None if np.isnan(last) else last


def rsi_series(series: pd.Series, period: int) -> pd.Series:
    """Her bar için RSI; ilk `period` barda NaN (kısmi pencereyle sayı üretilmez).

    Kayıp ortalaması sıfırsa (kesintisiz yükseliş) 100.0; her iki ortalama da sıfırsa
    (fiyat hiç değişmemiş) 50.0 — 100 demek, hareketsiz bir seriyi "azami aşırı alım" ilan
    etmek olurdu.

    Seri hâli, geçmiş bir barın (ör. bir pivotun) RSI'ını okumak zorunda olan modeller için
    var: diverjans kontrolü iki dibin RSI'ını karşılaştırır. Pencere-yerel basit ortalama
    burada da korunur (bkz. modül docstring'i) — Wilder yumuşatması, aynı barın RSI'ını
    çerçeveye kaç bar geçmiş verildiğine bağımlı kılardı.
    """
    _require_positive(period, "period")
    values = series.astype("float64")
    changes = values.diff()
    average_gain = changes.clip(lower=0.0).rolling(period).mean()
    average_loss = (-changes).clip(lower=0.0).rolling(period).mean()

    strength = pd.Series(np.nan, index=series.index, dtype="float64")
    moving = average_loss > 0.0
    strength[moving] = 100.0 - 100.0 / (1.0 + average_gain[moving] / average_loss[moving])
    strength[(average_loss == 0.0) & (average_gain > 0.0)] = 100.0
    strength[(average_loss == 0.0) & (average_gain == 0.0)] = 50.0
    return strength


def local_lows(series: pd.Series, *, order: int) -> list[int]:
    """Her iki yanında `order` bar daha yüksek olan noktaların KONUM indeksleri.

    Zigzag'dan farklı, ikinci bir pivot tanımıdır: kaynak tarayıcı da (crypto-scanner,
    `find_local_lows`) çift dip yapısını bununla arar — zigzag "trend dönüşü", bu ise
    "yerel fraktal" sorar. İkisi bilerek ayrı tutuldu; birini diğerinin yerine kullanmak
    kaynak modelin ölçtüğü yapıyı değiştirirdi.

    Son `order` bar hiçbir zaman pivot olamaz (sağ yanı henüz oluşmadı) — bu bir gecikmedir,
    look-ahead değil: karar yalnızca kapanmış barlarla verilir.
    """
    return _fractals(series, order=order, extreme="min")


def local_highs(series: pd.Series, *, order: int) -> list[int]:
    """`local_lows`un aynası: her iki yanında `order` bar daha düşük olan noktalar."""
    return _fractals(series, order=order, extreme="max")


def _fractals(series: pd.Series, *, order: int, extreme: Literal["min", "max"]) -> list[int]:
    _require_positive(order, "order")
    found: list[int] = []
    for index in range(order, len(series) - order):
        window = series.iloc[index - order : index + order + 1]
        reference = window.min() if extreme == "min" else window.max()
        if series.iloc[index] == reference:
            found.append(index)
    return found


def bollinger(series: pd.Series, period: int, num_std: float) -> BollingerBands | None:
    """Orta bant = SMA(period); bantlar = orta ± num_std × popülasyon sapması."""
    _require_positive(period, "period")
    if num_std <= 0.0:
        raise ValueError(f"num_std pozitif olmalı: {num_std}")
    values = series.to_numpy(dtype="float64")
    if len(values) < period:
        return None
    window = values[-period:]
    middle = float(window.mean())
    deviation = float(window.std(ddof=0)) * num_std
    return BollingerBands(upper=middle + deviation, middle=middle, lower=middle - deviation)


def donchian(frame: pd.DataFrame, period: int) -> DonchianChannel | None:
    """Son barı HARİÇ tutan `period` barlık en yüksek zirve / en düşük dip.

    Son barın dışlanması bir ayrıntı değil, tanımın kendisidir: kanal son barı da içerseydi
    `upper >= high >= close` olurdu ve "tepenin üstüne kapanış" koşulu yalnızca kapanışın
    tam olarak barın zirvesine eşit olduğu durumda sağlanabilirdi — yani kırılım pratikte
    hiç oluşmazdı. Kanal, kırılımı ölçülen barın GEÇMİŞİDİR.
    """
    _require_positive(period, "period")
    if len(frame) < period + 1:
        return None
    window = frame.iloc[-(period + 1):-1]
    return DonchianChannel(
        upper=float(window["high"].to_numpy(dtype="float64").max()),
        lower=float(window["low"].to_numpy(dtype="float64").min()),
    )


AtrSmoothing = Literal["simple", "wilder"]


def true_range(frame: pd.DataFrame) -> "np.ndarray":
    """Her bar için gerçek aralık (ilk bar hariç: önceki kapanış gerekir).

    Tek tanım: iki yumuşatma da bu diziyi okur. TR'yi iki kez yazmak, "aynı ATR ama farklı
    yumuşatma" iddiasını doğrulanamaz kılardı — fark yumuşatmada mı TR'de mi bilinmezdi.
    """
    high = frame["high"].to_numpy(dtype="float64")
    low = frame["low"].to_numpy(dtype="float64")
    close = frame["close"].to_numpy(dtype="float64")
    previous_close = close[:-1]
    return np.maximum(
        high[1:] - low[1:],
        np.maximum(np.abs(high[1:] - previous_close), np.abs(low[1:] - previous_close)),
    )


def average_true_range(
    frame: pd.DataFrame, period: int, *, smoothing: AtrSmoothing = "simple"
) -> float | None:
    """Ortalama gerçek aralık; yeterli bar yoksa None.

    **Periyot projenin ORTAK ayarıdır** (`trailing.atr_period`), yumuşatma ise modelin
    bildirdiği bir seçimdir ve varsayılanı `simple`dır — yani bu imza eklenmeden önceki
    her çağrı birebir aynı sayıyı almaya devam eder.

    - `simple`: son `period` TR'nin düz ortalaması. Projenin varsayılanı ve BÜTÜN ev
      modellerinin kullandığı tanım.
    - `wilder`: Wilder yumuşatması (RMA), ilk `period` TR'nin ortalamasıyla tohumlanır ve
      sonrasında `atr = (atr × (period − 1) + tr) / period` ile ilerler. TradingView'in
      `ta.atr`ı budur.

    **Neden İKİ tanım duruyor ve neden `simple` küresel olarak DEĞİŞTİRİLMEDİ.** Kuralları
    dış bir sistemden gelen bir model (bkz. `strategies/ema_trend.py`), orada hangi ATR ile
    doğrulandıysa onunla koşmalıdır — yoksa "koşulan şey" ile "doğrulanan şey" ayrışır.
    Ama tanımı KÜRESEL olarak Wilder'a çevirmek, bugün canlı koşan her modelin (trend,
    meanrev, beş kollu scalp modelleri, vwap_managed) stop mesafesini o commit'ten itibaren
    değiştirirdi; biriken defterin bir kısmı bir stop ölçeğiyle, kalanı başkasıyla
    üretilmiş olurdu — `fee_rate`in defteri tarihli olarak böldüğü hatanın aynısı
    (docs/decisions.md > 25).

    **Bedeli açıkça yazılıdır:** "1.5×ATR" ile "2×ATR" artık modeller arasında birebir
    kıyaslanabilir DEĞİLDİR. Kural 14'ün kıyas ölçütü zaten ATR katı değil GERÇEKLEŞEN
    stop mesafesidir (`avg_stop_distance_pct` ve ⚠B bandı), yani kıyas o kolondan okunur.
    Motorun tavan kontrolü (kural 14, `core/engine.py`) ORTAK tanımda kalır: tavan katmanın
    kuralıdır ve her model kendi yumuşatmasını seçerek kendi tavanını genişletebilseydi
    tavan bir kural olmaktan çıkardı.
    """
    if period <= 0:
        raise ValueError(f"atr_period pozitif olmalı: {period}")
    if len(frame) < period + 1:
        return None
    if smoothing not in ("simple", "wilder"):
        raise ValueError(f"tanınmayan ATR yumuşatması: {smoothing!r}")

    if smoothing == "simple":
        return float(true_range(frame.tail(period + 1)).mean())

    # Wilder: özyineleme ELDEKİ TÜM barlar üzerinde yürür, yalnızca son `period` üzerinde
    # değil — RMA'nın tanımı budur ve kısa bir pencereye uygulamak onu düz ortalamaya
    # yaklaştırırdı. Tohumun etkisi `(1 − 1/period)^n` ile söner: 12.000 barlık bir
    # backtest penceresinde tohum tamamen unutulmuş olur, yani sayı pencere başlangıcına
    # bağlı değildir.
    values = true_range(frame)
    if len(values) < period:
        return None
    current = float(values[:period].mean())
    for value in values[period:]:
        current = (current * (period - 1) + float(value)) / period
    return current


def _require_positive(value: int, name: str) -> None:
    if value <= 0:
        raise ValueError(f"{name} pozitif olmalı: {value}")


# --------------------------------------------------------------------------- #
# Zigzag pivotları ve Fibonacci seviyeleri
#
# Bu iki fonksiyon crypto-scanner deposundaki `find_zigzag_pivots`,
# `_merge_short_legs` ve `compute_fib_levels`'in BİREBİR taşınmış hâlidir (eşik
# karşılaştırmaları, canlı uç davranışı ve çift silme mantığı dâhil). Yeniden
# yazılmadılar: aynı pivot tanımının ikinci bir uygulaması, bu depodaki modelin
# ölçtüğü şeyin kaynak tarayıcıyla aynı olduğu iddiasını kanıtlanamaz kılardı —
# modül docstring'indeki "aynı göstergenin iki uygulaması" itirazının tam hâli.
#
# Look-ahead (kural 12) açısından temiz: her iki fonksiyon da yalnızca kendilerine
# verilen çerçeveyi okur, çağıran taraf çerçeveyi `bars_until` ile `as_of`'ta keser.
# Son pivot "henüz teyit edilmemiş" canlı uçtur — bir sonraki barda yer değiştirebilir
# ama GELECEĞİ görmez; kaynaktaki davranış korundu, çünkü onu atmak swing'in güncel
# ucunu tümden kaybettirirdi.
# --------------------------------------------------------------------------- #


def zigzag_pivots(
    frame: pd.DataFrame, *, pct_threshold: float, min_leg_bars: int
) -> list[Pivot]:
    """Fiyat, ekstremden `pct_threshold` kadar ters yöne dönünce bir pivot onaylanır.

    Süresi `min_leg_bars`'ın altında kalan bacaklar ÇİFT HÂLİNDE elenir: tek pivot
    silmek dip/zirve alternansını bozardı, çift silmek bozmaz.
    """
    if pct_threshold <= 0.0:
        raise ValueError(f"pct_threshold pozitif olmalı: {pct_threshold}")
    if len(frame) < 3:
        return []

    highs = frame["high"].to_numpy(dtype="float64")
    lows = frame["low"].to_numpy(dtype="float64")
    times = frame.index

    pivots: list[Pivot] = []
    trend: Literal["up", "down"] | None = None
    extreme_index = 0
    extreme_price = float(frame["close"].iloc[0])

    for index in range(1, len(frame)):
        high, low = float(highs[index]), float(lows[index])

        if trend is None:
            if high >= extreme_price * (1 + pct_threshold):
                trend, extreme_index, extreme_price = "up", index, high
            elif low <= extreme_price * (1 - pct_threshold):
                trend, extreme_index, extreme_price = "down", index, low
            continue

        if trend == "up":
            if high > extreme_price:
                extreme_index, extreme_price = index, high
            elif low <= extreme_price * (1 - pct_threshold):
                pivots.append(
                    Pivot(time=times[extreme_index], price=extreme_price, kind="high")
                )
                trend, extreme_index, extreme_price = "down", index, low
        else:
            if low < extreme_price:
                extreme_index, extreme_price = index, low
            elif high >= extreme_price * (1 + pct_threshold):
                pivots.append(
                    Pivot(time=times[extreme_index], price=extreme_price, kind="low")
                )
                trend, extreme_index, extreme_price = "up", index, high

    if trend is not None:
        # Oluşmakta olan, reversal ile henüz onaylanmamış ekstrem: swing'in canlı ucu.
        pivots.append(
            Pivot(
                time=times[extreme_index],
                price=extreme_price,
                kind="high" if trend == "up" else "low",
            )
        )

    if pivots:
        # Başlangıç ankrajı: ilk onaylanan pivotun ZIT tipinde, serinin ilk barından —
        # ilk swing de (başlangıç -> ilk pivot) aday olarak değerlendirilebilsin diye.
        pivots.insert(
            0,
            Pivot(
                time=times[0],
                price=float(frame["close"].iloc[0]),
                kind="low" if pivots[0].kind == "high" else "high",
            ),
        )

    return _merge_short_legs(frame, pivots, min_leg_bars)


def fib_levels(
    *, a_price: float, b_price: float, a_kind: PivotKind, ratios: Sequence[float]
) -> dict[float, float]:
    """A-B bacağının verilen oranlardaki seviyeleri; yön A'nın tipinden okunur.

    A dip ise seviyeler B'den AŞAĞI (destek gibi), A zirve ise B'den YUKARI (direnç
    gibi) projekte edilir — kaynak tarayıcıdaki `compute_fib_levels` ile aynı yön mantığı.
    """
    distance = abs(b_price - a_price)
    return {
        ratio: (b_price - ratio * distance if a_kind == "low" else b_price + ratio * distance)
        for ratio in ratios
    }


def _merge_short_legs(
    frame: pd.DataFrame, pivots: list[Pivot], min_leg_bars: int
) -> list[Pivot]:
    if min_leg_bars <= 0 or len(pivots) < 3:
        return pivots

    merged = list(pivots)
    time_to_index = {stamp: index for index, stamp in enumerate(frame.index)}

    changed = True
    while changed and len(merged) >= 3:
        changed = False
        for index in range(len(merged) - 1):
            first = time_to_index.get(merged[index].time)
            second = time_to_index.get(merged[index + 1].time)
            if first is None or second is None:
                continue
            if (second - first) < min_leg_bars:
                del merged[index : index + 2]
                changed = True
                break

    return merged


# --------------------------------------------------------------------------- #
# Çapalı VWAP (anchored VWAP)
#
# Neden burada ve neden stratejinin içinde değil: hacim ağırlıklı ortalama ve onun
# hacim ağırlıklı sapması bir GÖSTERGEDİR, iki kapanışın oranı gibi tek satırlık bir
# aritmetik değil. Modül docstring'indeki itiraz aynen geçerli — aynı göstergenin
# strateji içine yazılmış ikinci bir uygulaması, iki modelin aynı barda farklı sayı
# görmesinin kapısıdır. Şu an tek kullanıcısı `strategies/avwap.py` olsa da tanımın
# tek ve denetlenebilir yeri burasıdır.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, kw_only=True)
class AnchoredVwap:
    """Çapadan itibaren hacim ağırlıklı ortalama ve onun hacim ağırlıklı sapması."""

    value: float
    deviation: float
    bars: int


def typical_price(frame: pd.DataFrame) -> pd.Series:
    """(high + low + close) / 3 — VWAP'ın klasik fiyat girdisi.

    Yalnızca kapanışı kullanmak barın işlem gördüğü aralığı yok sayar; VWAP'ın iddiası
    "bu hacim hangi fiyattan el değiştirdi" olduğu için barın ortası daha dürüst bir
    temsildir. Tanım tek satır ve tek yerde durur ki hangi fiyatın ağırlıklandırıldığı
    sonradan aranmasın.
    """
    return (frame["high"] + frame["low"] + frame["close"]) / 3.0


def anchored_vwap(frame: pd.DataFrame, *, anchor: pd.Timestamp) -> AnchoredVwap | None:
    """`anchor` barı DÂHİL, çerçevenin sonuna kadar hacim ağırlıklı ortalama ve sapma.

    Sapma popülasyon (ddof=0) tanımıyla ve aynı hacim ağırlıklarıyla hesaplanır: Bollinger
    kararıyla (bkz. modül docstring'i) tutarlı olsun ve "2σ" ifadesi bu depoda tek bir şey
    ifade etsin diye.

    Çapa çerçevede yoksa, çapadan sonra bar kalmamışsa ya da toplam hacim sıfırsa None
    döner — kısmi/anlamsız bir pencereyle sayı üretmek, bandı sembolden sembole farklı bir
    ölçüye çevirirdi.
    """
    if anchor not in frame.index:
        return None
    window = frame.loc[anchor:]
    if window.empty:
        return None

    prices = typical_price(window).to_numpy(dtype="float64")
    volumes = window["volume"].to_numpy(dtype="float64")
    total_volume = float(volumes.sum())
    if total_volume <= 0.0:
        # Hacimsiz pencerede "hacim ağırlıklı" ortalamanın tanımı yoktur; eşit ağırlığa
        # düşmek, göstergeyi sessizce başka bir göstergeye (basit ortalama) çevirirdi.
        return None

    value = float((prices * volumes).sum() / total_volume)
    variance = float((volumes * (prices - value) ** 2).sum() / total_volume)
    return AnchoredVwap(value=value, deviation=float(np.sqrt(variance)), bars=len(window))
