"""EMA(21/55) kesişimi — long-only trend takibi (yarışmacı model, `ema` katmanı).

Tez: hızlı ortalama yavaşını YUKARI kestiğinde, kesişim yönünde bir trend başlama
olasılığı rastgeleden yüksektir. Model bunu tek bir olaya indirger — kesişimin KENDİSİNE,
"fast > slow" durumuna değil: durum kuralı her barda tetiklenir ve model bir trend
takipçisi değil, "yukarı rejimde sürekli alım" modeli olurdu.

**Kuralları dış bir sistemden gelir** (TradingView / Pine Script) ve orada doğrulandı.
Bu onu bir KOPYA yapmaz (kural 15b): kopya dış sistemin BOYUTLANDIRMASINI da taşır ve
yarışmaz; burada dışarıdan gelen yalnızca sinyaldir. Boyut (risk %1), kaldıraç tavanı,
komisyon, kayma, funding ve likidasyon evin kurallarıdır — yani model tam bir
yarışmacıdır, ortalama R sıralamasına girer ve kabul çıtasına tabidir. Ön-kayıt
`docs/backtest.md > 6d`dedir; parametreler o belge gereği SABİTTİR.

Neden yalnızca long: short yönü kaynak sistemde denendi ve sistematik kaybettiriyordu.
`allowed_directions` bunu bir BİLDİRİM yapar — sessizce short üretmeyen bir model ile
short'u yasaklanmış bir model, defterden bakıldığında ayırt edilemezdi (kural 8:
ihlal `ValueError`, sessiz filtre değil).

Neden stop 1.5×ATR: stop mesafesi aynı zamanda MALİYET ÖLÇEĞİDİR (kural 14) — boyut
`risk / |giriş − stop|` olduğu için dar stop kuran model aynı 1R'yi daha büyük notional
ile taşır ve R başına daha çok komisyon öder. 1.5, kuralın 1×–2.5×ATR bandının içindedir
ve katmanın `max_stop_atr_multiple` tavanına (3.0) takılmaz.

**ATR yumuşatması Wilder'dır (RMA), projenin varsayılanı `simple` değil.** Bu bir
parametre tercihi değil SPEC uyumudur: kaynak sistem `ta.atr` (Wilder) ile geliştirildi ve
doğrulandı, stop ve hedef mesafelerinin tamamı o değerden türüyor. Yumuşatmayı KÜRESEL
olarak değiştirmek ise reddedildi — canlı koşan her modelin stop ölçeği o commit'ten
itibaren değişir ve biriken defter iki ayrı ölçekten üretilmiş olurdu (docs/decisions.md >
25'in `fee_rate` hatası). Periyot ORTAK kalır (`trailing.atr_period`); ayrışan yalnızca
yumuşatmadır ve `config.yaml > ema_trend.atr_smoothing` ile BİLDİRİLİR. Bedeli:
"1.5×ATR" artık modeller arası birebir kıyaslanabilir değildir — kıyas
`avg_stop_distance_pct` kolonundan okunur (kural 14'ün zaten kullandığı ölçüt) ve motorun
tavan kontrolü ortak tanımda kalır. Bkz. docs/backtest.md > 6d > TADİLAT-1.

Neden zaman stop'u ve trailing YOK: ikisi de kaynak sistemde yok. Eklemek modeli
ölçülmek istenen şeyden başka bir şeye çevirirdi. Bedeli ön-kayıtta yazılıdır: pozisyon
ömrü sınırsızdır, bu yüzden OOS penceresinin embargosu VARSAYILMAZ, ÖLÇÜLÜR (dönem A'da
gözlenen azami tutuş süresi).

"Pozisyondayken yeni sinyal yok sayılır" kuralı BURADA DEĞİLDİR: model kendi açık
pozisyonlarını göremez (kural 4/16), yalnızca kapanmışlarını görebilir — onu da bu model
istemiyor. Tekrarı reddeden tek yetkili yer `core/portfolio.py`dir ve ret bir sebep
koduyla (`duplicate_position`) tur raporuna düşer (kural 15'in çıpa için koyduğu aynı
denetlenebilirlik). Pratikte tekrar nadirdir: kesişim bir OLAYDIR, tek barda doğrudur.

Rollere dikkat: bu modül boyut, komisyon ya da bakiye HESAPLAMAZ (kural 1/2/3/7) ve
gösterge matematiği yazmaz — `core/indicators.py` tek kaynaktır (aynı göstergenin ikinci
bir uygulaması, iki modelin farklı sayı görmesi demek olurdu).
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

from core.config import get_setting, load_config
from core.indicators import average_true_range, bars_until, ema
from strategies.base import Direction, MarketData, Signal, Strategy, TakeProfit

logger = logging.getLogger(__name__)


class EmaTrend(Strategy):
    name = "ema_trend"
    allowed_directions: list[Direction] = ["long"]

    def __init__(self, *, config: Mapping[str, Any] | None = None) -> None:
        settings = dict(config) if config is not None else load_config()
        # ATR periyodu parametre DEĞİL: projenin tek ATR tanımı `trailing.atr_period`tır
        # (CLAUDE.md). Modelin kendi periyodunu seçmesi, aynı "1.5×ATR" ifadesinin
        # modelden modele farklı mesafe anlamına gelmesi demek olurdu.
        self._atr_period = int(get_setting(settings, "trailing.atr_period"))
        # Model parametreleri config'ten okunur, koda gömülmez: ön-kayıt (docs/backtest.md
        # > 6d) bu dört sayıyı sabitledi ve bir sabitin nerede durduğu, onu değiştirmenin
        # ne kadar görünür olacağını belirler.
        self._fast_period = int(get_setting(settings, "ema_trend.fast_period"))
        self._slow_period = int(get_setting(settings, "ema_trend.slow_period"))
        self._stop_atr_multiple = float(get_setting(settings, "ema_trend.stop_atr_multiple"))
        self._target_reward_risk = float(get_setting(settings, "ema_trend.target_reward_risk"))
        # Yumuşatma bu modelin BİLDİRİMİDİR (bkz. modül docstring'i): periyot ortak kalır,
        # yumuşatma kaynak sistemin spec'inden gelir. `get_setting` eksik anahtarda patlar,
        # yani sessiz bir varsayılana düşmek mümkün değildir.
        self._atr_smoothing = str(get_setting(settings, "ema_trend.atr_smoothing"))

    def generate_signals(
        self,
        market: MarketData,
        peer_signals: Mapping[str, tuple[Signal, ...]] | None = None,
    ) -> list[Signal]:
        signals: list[Signal] = []
        # Sıralı gezinti: sinyal sırası max_positions dolduğunda hangi sembolün girdiğini
        # belirler; sözlük sırasına bırakmak koşuyu veri katmanının sırasına bağlardı.
        for symbol in sorted(market.ohlcv):
            signal = self._evaluate(symbol, market)
            if signal is not None:
                signals.append(signal)
        return signals

    def _evaluate(self, symbol: str, market: MarketData) -> Signal | None:
        frame = bars_until(market.ohlcv[symbol], market.as_of)
        if frame.empty or frame.index[-1] != market.as_of:
            # Sembol `as_of` barını taşımıyor: core/data.py bunları zaten dışlar, ama bir
            # sinyali bir bar geriden üretmek look-ahead kadar sessiz bir ölçüm hatasıdır.
            return None

        # Kesişim İKİ barın karşılaştırmasıdır: bu barda fast > slow, önceki barda değil.
        # Önceki barın değerleri aynı seriden, son bar DÜŞÜLEREK hesaplanır — ikinci bir
        # EMA uygulaması yazmak yerine aynı tanımı bir bar kısa veriye uygulamak, iki
        # değerin aynı tohumlama kuralını paylaşmasını garanti eder.
        closes = frame["close"]
        fast, slow = ema(closes, self._fast_period), ema(closes, self._slow_period)
        previous = closes.iloc[:-1]
        fast_prev, slow_prev = ema(previous, self._fast_period), ema(previous, self._slow_period)
        if None in (fast, slow, fast_prev, slow_prev):
            return None
        assert fast is not None and slow is not None  # noqa: S101 — tip daraltma
        assert fast_prev is not None and slow_prev is not None  # noqa: S101

        crossed_up = fast_prev <= slow_prev and fast > slow
        if not crossed_up:
            return None

        atr = average_true_range(frame, self._atr_period, smoothing=self._atr_smoothing)
        if atr is None or atr <= 0.0:
            # Stop mesafesi üretilemiyor. Sessiz atlamak, bu modelin işlem sayısını
            # ölçülemeyen bir nedenle düşürürdü — kural 14'ün "atlama sessiz olamaz"
            # gerekçesi burada da geçerli.
            logger.info(
                "%s %s: kesişim atlandı, ATR(%d, %s) hesaplanamadı",
                self.name, symbol, self._atr_period, self._atr_smoothing,
            )
            return None

        close = float(closes.iloc[-1])
        distance = atr * self._stop_atr_multiple
        stop_price = close - distance
        if stop_price <= 0.0:
            # Stop sıfırın altına düşüyor: geometri kurulamaz ve core/validate.py bunu bir
            # PROGRAMLAMA hatası sayardı (kural 8). Atlamak doğru olan, çünkü bu bir piyasa
            # durumudur — ATR fiyatın kendisinden büyük.
            logger.info(
                "%s %s: kesişim atlandı, stop sıfırın altında (kapanış %.6g, %g×ATR %.6g)",
                self.name, symbol, close, self._stop_atr_multiple, distance,
            )
            return None
        target = close + distance * self._target_reward_risk

        return Signal(
            symbol=symbol,
            direction="long",
            stop_price=stop_price,
            take_profits=(TakeProfit(price=target, fraction=1.0),),
            reason=(
                f"EMA{self._fast_period} ({fast:.6g}) EMA{self._slow_period}'i ({slow:.6g}) "
                f"YUKARI kesti (önceki bar {fast_prev:.6g} <= {slow_prev:.6g}); "
                f"kapanış {close:.6g}, stop {self._stop_atr_multiple:g}×ATR"
                f"({self._atr_period},{self._atr_smoothing})={distance:.6g} uzakta "
                f"({stop_price:.6g}), "
                f"hedef {self._target_reward_risk:g}R ({target:.6g})"
            ),
        )
