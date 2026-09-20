"""MODEL 13 — dış bir sistemin KOPYASI (`is_replica=True`), yarışmacı değil.

**Ölçtüğü soru başkalarınınkinden farklıdır.** Yarışmacılar "bu sinyal fikri işe yarıyor
mu" diye sorar; bu model *"dışarıda koşan şu sistem, BİZİM maliyet, kayma, funding ve
likidasyon varsayımlarımız altında ne yapardı"* diye sorar. Cevap bir sıralama değil, bir
zemindir — tıpkı alım-tut çıpası gibi ama başka bir eksende: çıpa "piyasa ne yaptı"yı,
kopya "dış sistem ne yapardı"yı ölçer. Bu yüzden tabloda ayrı bir bölümdedir
(`REFERANS (dış sistem)`), ortalama R sıralamasına girmez ve maliyet ölçeği kolonlarında
`nan` alır: 1R'sini sabit teminattan türettiği için yarışmacılarınkiyle aynı birim değil.

**Kaynak sistemin kuralları, olduğu gibi** (klonnist/Hasanwavebot; `vwap_detector.py`,
`learner.py`, `main.py`):

- *Sinyal:* `strategies/vwap/clone_signal.py` — kopyanın KENDİ modülü. Model 14'ün
  `strategies/vwap/signal.py`'si ile paylaşılmaz: ikisi aynı fikri BAŞKA kurallarla
  ölçer ve model 13 ↔ 14 ekseni ("ev kurallarının katkısı") tam olarak o farkı okur.
- *Stop:* `entry ∓ band_mult × sl_mult × σ`. ATR YOKTUR — kaynak sistemin VWAP kolunda
  ATR hiç geçmez (orada ATR yalnızca Elliott Wave kolunundur).
- *Hedef:* `entry ± max(|VWAP − entry|, 0) × tp_mult`, `tp_mult ≤ 1`. Hedef VWAP'i ASLA
  aşmaz ve dayatılmış bir hedef/stop oranı yoktur: R:R bu iki kuralın SONUCUDUR.
- *Boyutlandırma:* sabit teminat × sabit kaldıraç. `sizing="notional_fraction"` ve
  `ModelLimits.leverage` ile ifade edilir: notional = fraction × sermaye, marj =
  notional / kaldıraç. Başlangıç sermayesinde (10.000) fraction 0.5 ve 10x, tam olarak
  "500 USDT teminat, 5.000 notional" demektir; sermaye büyüdükçe teminat da orantılı
  büyür — sabit bir USDT tutarı yazmak, hesap iki katına çıktığında kopyayı gerçek
  sistemin yarısı kadar risk alan bir şeye çevirirdi.
- *Çıkış:* üç aşamalı yönetim (`strategies/exit_management.py`, modeller 14-15 ile tek
  kopya): breakeven 1R, kısmi %50 @ 1.5R, geri verme takibi %50. Kaynağın
  `--breakeven-r / --partial-tp-r / --partial-tp-fraction / --trail-giveback-pct`
  varsayılanlarıyla birebir aynı.
- *Limitler:* eşzamanlı en çok 5 pozisyon, aynı yönde en çok 3, açık toplam risk
  sermayenin %8'ini aşamaz (`ModelLimits`; uygulayan core/portfolio.py, kural 3).
- *Parametre öğrenimi:* epsilon-greedy bandit, 3 bant çarpanı × 3 hedef çarpanı = 9
  kombinasyon. Seçim sırası kaynağınkidir: (a) o sembolde HİÇ DENENMEMİŞ kombinasyon
  varsa önce onlardan biri, (b) `epsilon` olasılıkla rastgele, (c) sömürü. İstatistik
  SEMBOL BAZINDA tutulur ve `min_symbol_samples` örnekten az veri varken tüm semboller
  genelindeki ortalamaya düşülür.
- *Evren:* kaynak sistemin sembolleri (bkz. `config.yaml > vwap.clone.universe`).
- *Tarama sırası:* evren listesinin SIRASI. Güce göre sıralama yoktur — kaynak
  `POPULAR_COINS`i baştan sona tarar ve kota dolunca kalanlara hiç bakmaz.

**Ev kapıları UYGULANMAZ.** Scalp katmanının %1 stop tabanı, 1.5R hedef/stop kapısı ve
16 barlık zaman stop'u bu modele geçmez: kaynak sistemde yoktur. Kapıları eklemek kopyayı
"ev kurallarıyla koşan bir model"e çevirirdi — ki o zaten model 14'tür ve ikisinin farkı
tam olarak bu kapıların (artı boyutlandırmanın) katkısıdır.

**Likidasyon modellemesi KORUNUR** ve bu bir tercih değil, ölçümün koşuludur: 10x'te
likidasyon gerçek bir risktir ve onu kapatmak kopyayı haksız biçimde iyi gösterirdi
(gerekçe: docs/decisions.md > "Kopya modelde likidasyon kapatılmaz").

**Kopyanın sınırları** (kural 12/13 gereği kopyalanmayan üç şey ve motorun tek ev kapısı)
docs/decisions.md > "Sadık kopyanın sınırları" altında tek tek yazılıdır; burada
tekrarlanmaz ki bir gün ayrışmasınlar.

**Bandit durumu deftere yazılır, ayrı bir durum dosyası YOKTUR** (`scalp_bandit` ile aynı
desen ve aynı gerekçe): posterior `trades.csv`'nin saf bir fonksiyonudur ve her turda
sıfırdan kurulur. Kaynak sistem durumu iki JSON dosyasında tutar; burada tutmak, defterle
senkron kalması ayrıca test edilmesi gereken ikinci bir doğruluk kaynağı yaratırdı.
Denetim izi `reason` kuyruğundadır: `... | arm=vwap_revert_src | combo=band1_tp2 | ...`.

Rollere dikkat: bu modül boyut/komisyon/bakiye hesaplamaz (kural 1/2/3/7) ve deftere
yazmaz (kural 1); gördüğü tek geçmiş KENDİ kapanmış işlemleridir (kural 16).
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from core.config import get_setting, load_config
from core.tags import find_tag, format_tags
from strategies.base import (
    ClosedTrade,
    Direction,
    MarketData,
    ModelLimits,
    Signal,
    Strategy,
    TakeProfit,
)
from strategies.exit_management import ExitManagement
from strategies.vwap import clone_signal

logger = logging.getLogger(__name__)

NAN = float("nan")


@dataclass(frozen=True, kw_only=True)
class Combo:
    """Bandit'in bir kolu: bir bant çarpanı + bir hedef çarpanı (sl_mult sabit).

    `key` deftere yazılan ve geri okunan etikettir. İNDEKSTEN türetilir, sayıdan değil:
    "2.5" gibi bir kayan noktayı etiket anahtarı yapmak, biçimlendirme (`%.4g`) ile geri
    ayrıştırmanın bir gün ayrışması demekti — ve posterior sessizce boşalırdı.
    """

    key: str
    params: clone_signal.CloneParams

    @property
    def band_mult(self) -> float:
        return self.params.band_mult

    @property
    def tp_mult(self) -> float:
        return self.params.tp_mult


@dataclass(frozen=True, kw_only=True)
class ComboStats:
    """Bir kombinasyonun gözlem özeti. `nan` = ölçülmedi (0.0 değil)."""

    trades: int
    mean_r: float

    @property
    def measured(self) -> bool:
        return self.trades > 0


class VwapClone(Strategy):
    name = "vwap_clone"
    allowed_directions: list[Direction] = ["long", "short"]
    is_replica = True

    def __init__(self, *, config: Mapping[str, Any] | None = None) -> None:
        settings = dict(config) if config is not None else load_config()
        self._universe = [str(s) for s in get_setting(settings, "vwap.clone.universe")]
        self._notional_fraction = float(get_setting(settings, "vwap.clone.notional_fraction"))
        self._vwap_window = int(get_setting(settings, "vwap.clone.vwap_window"))
        self._std_window = int(get_setting(settings, "vwap.clone.std_window"))
        self._min_bars = int(get_setting(settings, "vwap.clone.min_bars"))
        self._sl_mult = float(get_setting(settings, "vwap.clone.sl_mult"))
        self._epsilon = float(get_setting(settings, "vwap.clone.bandit.epsilon"))
        self._min_symbol_samples = int(
            get_setting(settings, "vwap.clone.bandit.min_symbol_samples")
        )
        self._exit = ExitManagement.from_config(settings)
        self._seed = int(get_setting(settings, "random_seed"))

        self._combos = _build_combos(
            [float(v) for v in get_setting(settings, "vwap.clone.band_mults")],
            [float(v) for v in get_setting(settings, "vwap.clone.tp_mults")],
            sl_mult=self._sl_mult,
        )
        if not 0.0 <= self._epsilon <= 1.0:
            raise ValueError(f"vwap.clone.bandit.epsilon 0 ile 1 arasında olmalı: {self._epsilon}")
        # Pencere tutarlılığı KURULUMDA denetlenir, sinyal anında değil: `std_window`
        # `min_bars`tan büyükse rolling σ son barda hep NaN kalır ve model her turu
        # sessizce "band_yok" ile geçer — yani hiç sinyal üretmeyen bir model, kurulumu
        # bozuk bir modelden ayırt edilemez hâle gelir (kural 15'in tam tersi).
        if self._std_window > self._min_bars:
            raise ValueError(
                f"vwap.clone.std_window ({self._std_window}) min_bars ({self._min_bars}) "
                "değerini aşamaz: σ son barda hiçbir zaman hesaplanamazdı"
            )
        if self._vwap_window < self._min_bars:
            raise ValueError(
                f"vwap.clone.vwap_window ({self._vwap_window}) min_bars "
                f"({self._min_bars}) değerinin altında olamaz: pencere kendi eşiğini "
                "hiçbir zaman dolduramazdı"
            )
        if self._sl_mult <= 0.0:
            raise ValueError(f"vwap.clone.sl_mult pozitif olmalı: {self._sl_mult}")

        self.limits = ModelLimits(
            max_positions=int(get_setting(settings, "vwap.clone.max_positions")),
            max_per_direction=int(get_setting(settings, "vwap.clone.max_per_direction")),
            max_portfolio_risk=float(get_setting(settings, "vwap.clone.max_portfolio_risk")),
            leverage=float(get_setting(settings, "vwap.clone.leverage")),
        )

        # Sembol bazlı ve genel istatistik; ikisi de defterden kurulur (aşağıya bkz.).
        self._by_symbol: dict[str, dict[str, ComboStats]] = {}
        self._global: dict[str, ComboStats] = _empty_stats(self._combos)
        # Son taramanın eleme sayımı (kural 15'in "atlama sessiz olamaz" şartı). Motor
        # her bardan sonra okur ve tur raporuna toplar; ölçüme girmez.
        self._survey: clone_signal.Survey | None = None

    # ------------------------------------------------------------------ #
    # Öğrenme (kural 16)
    # ------------------------------------------------------------------ #
    def observe_closed_trades(self, trades: Sequence[ClosedTrade]) -> None:
        """Kendi KAPANMIŞ işlemlerinden kombinasyon istatistiklerini sıfırdan kurar.

        Artımlı güncelleme YOK (`scalp_bandit` ile aynı gerekçe): durum defterin saf bir
        fonksiyonu olmazsa, defterle modelin hafızası bir gün ayrışır ve hangisinin doğru
        olduğu bilinemez.

        Etiketi TANINMAYAN satır sessizce atlanmaz, loglanır: kombinasyon kümesi
        değişmişse (config'te çarpan listesi düzenlenmişse) eski satırlar yeni kümeye
        ait değildir ve onları yeni kolların ortalamasına katmak, öğrenmeyi defterde
        görünmeyen bir geçmişe bağlardı.

        `arm=` etiketi olmayan satırda `TagError` fırlatılmaz — burada aranan etiket
        `combo`dur ve yokluğu ayrıca sayılır; kol kırılımının bütünlüğünü
        `core/metrics.py::arm_of` zaten kendi başına denetler.
        """
        by_symbol: dict[str, dict[str, list[float]]] = {}
        overall: dict[str, list[float]] = {combo.key: [] for combo in self._combos}
        unknown: set[str] = set()
        untagged = 0

        for trade in sorted(trades, key=lambda item: item.closed_at):
            key = find_tag(trade.signal_reason, "combo")
            if key is None:
                untagged += 1
                continue
            if key not in overall:
                unknown.add(key)
                continue
            if trade.r_multiple is None:
                continue
            overall[key].append(float(trade.r_multiple))
            by_symbol.setdefault(trade.symbol, {k: [] for k in overall})[key].append(
                float(trade.r_multiple)
            )

        if untagged:
            logger.warning(
                "%s: %d kapanmış işlemde combo etiketi yok, öğrenmeye girmedi",
                self.name, untagged,
            )
        if unknown:
            logger.warning(
                "%s: defterde tanınmayan kombinasyon: %s — kombinasyon kümesi değişmiş "
                "olabilir, bu satırlar öğrenmeye girmedi",
                self.name, ", ".join(sorted(unknown)),
            )

        self._global = {key: _summarize(values) for key, values in overall.items()}
        self._by_symbol = {
            symbol: {key: _summarize(values) for key, values in per_combo.items()}
            for symbol, per_combo in by_symbol.items()
        }
        logger.info("%s öğrenme durumu: %s", self.name, self.state_summary())

    def state_summary(self) -> str:
        return " ".join(
            f"{key}(n={item.trades},R={item.mean_r:.3f})" if item.measured else f"{key}(n=0)"
            for key, item in self._global.items()
        )

    def stats_for(self, symbol: str) -> dict[str, ComboStats]:
        """Sembolün istatistikleri; testler ve denetim için salt okunur kopya."""
        return dict(self._by_symbol.get(symbol, {}))

    # ------------------------------------------------------------------ #
    # Sinyal
    # ------------------------------------------------------------------ #
    def generate_signals(
        self,
        market: MarketData,
        peer_signals: Mapping[str, tuple[Signal, ...]] | None = None,
    ) -> list[Signal]:
        """Kaynak sistemin taraması: evren listesini BAŞTAN SONA, sembol sırasıyla.

        Güce göre sıralama YOKTUR (model 14'ün `strongest`i burada geçersizdir): kaynak
        `POPULAR_COINS`i sırayla gezer, her sembolde önce kombinasyonu çeker
        (`learner.select(symbol)`), sonra kurulumu arar. Çekilişin kurulumdan ÖNCE
        gelmesi kaynağın davranışıdır ve burada da öyle korunur — aksi hâlde aynı turda
        çekilen kombinasyon dizisi (dolayısıyla keşif payının dağılımı) başkalaşırdı.

        Turda tek sinyalle sınırlanmaz (scalp kollarının kuralı burada yok): kaynak sistem
        aynı anda beş pozisyona kadar taşır. Liste yine de modelin kendi pozisyon kotasıyla
        (`ModelLimits.max_positions`) kırpılır — kotanın ötesindeki her emir bir sonraki
        barda zaten `max_positions` koduyla reddedilir ve tur raporunu gerçek olmayan
        retlerle doldururdu. Kırpma listenin SONUNDAN yapılır, yani kaynağın "kota dolunca
        kalan sembollere hiç bakma" davranışıyla aynı sonucu verir.
        """
        rng = self._round_rng(market)
        counts = clone_signal.empty_counts()
        signals: list[Signal] = []

        for symbol in self._universe:
            combo, stats, pick = self.choose_combo(symbol, rng=rng)
            candidate, reason = clone_signal.detect(
                market.ohlcv.get(symbol),
                symbol=symbol,
                as_of=market.as_of,
                params=combo.params,
                vwap_window=self._vwap_window,
                std_window=self._std_window,
                min_bars=self._min_bars,
            )
            counts[reason] += 1
            if candidate is not None:
                signals.append(self._signal(candidate, combo=combo, stats=stats, pick=pick))

        survey = clone_signal.Survey(counts=counts)
        self._survey = survey
        logger.info(
            "%s %s %s -> %s",
            self.name, clone_signal.ARM_NAME, market.as_of.isoformat(), survey.describe(),
        )

        limit = self.limits.max_positions if self.limits.max_positions else len(signals)
        return signals[:limit]

    def take_survey(self) -> Mapping[str, int] | None:
        """Son taramanın eleme sayımı (kural 15). Sinyalleri hiçbir biçimde etkilemez."""
        return None if self._survey is None else dict(self._survey.counts)

    def _signal(
        self,
        candidate: clone_signal.CloneCandidate,
        *,
        combo: Combo,
        stats: ComboStats,
        pick: str,
    ) -> Signal:
        return Signal(
            symbol=candidate.symbol,
            direction=candidate.direction,
            # Kaynak sistemin boyutlandırması: sabit teminat × sabit kaldıraç.
            sizing="notional_fraction",
            notional_fraction=self._notional_fraction,
            stop_price=candidate.stop_price,
            take_profits=(TakeProfit(price=candidate.target_price, fraction=1.0),),
            **self._exit.signal_fields(),
            reason=format_tags(
                f"{candidate.detail()}; stop {combo.band_mult:g}×{self._sl_mult:g}σ "
                f"({candidate.stop_price:.6g}), hedef VWAP mesafesinin "
                f"%{combo.tp_mult * 100:g}'i ({candidate.target_price:.6g}); "
                f"{self._exit.describe()}",
                arm=clone_signal.ARM_NAME,
                combo=combo.key,
                combo_r=stats.mean_r,
                combo_n=stats.trades,
                pick=pick,
            ),
        )

    # ------------------------------------------------------------------ #
    # Epsilon-greedy seçim (kaynak: learner.py::select)
    # ------------------------------------------------------------------ #
    def choose_combo(
        self, symbol: str, *, rng: random.Random
    ) -> tuple[Combo, ComboStats, str]:
        """Kaynağın üç adımlı seçimi; dönen üçüncü değer deftere yazılan `pick` etiketidir.

        1. **Denenmemiş öncelik.** O SEMBOLDE hiç örneği olmayan kombinasyon varsa
           önce onlardan biri çekilir. Bu adım kaynağın kendi ısınmasıdır: her sembolde
           dokuz kombinasyonun hepsi en az bir kez oynanmadan sömürüye geçilmez. Kontrol
           sembolün KENDİ sayımına bakar, genele düşmüş hücreye değil — genel ortalama
           dolu diye o sembolde hiç denenmemiş bir kolu "denenmiş" saymak, ısınmayı ilk
           sembolden sonra tamamen atlamak olurdu.
        2. **Keşif.** `epsilon` olasılıkla rastgele. Pay sabittir ve sıfıra inmez:
           sıfırlanan bir kombinasyon bir daha ÖLÇÜLEMEZ ve "kötüydü" iddiası sınanamaz
           hâle gelir (`scalp_bandit`'in taban tahsisiyle aynı gerekçe).
        3. **Sömürü.** En yüksek ortalama R. İstatistik önce SEMBOL bazında aranır: aynı
           kurulum BTC'de ve PENGU'da aynı bant çarpanıyla aynı sonucu vermez (oynaklık
           ve kitap derinliği farklı). Ama sembol başına veri geç birikir;
           `min_symbol_samples` örneğe ulaşmamış bir sembolde sembolün kendi gürültüsüne
           uymak, tüm semboller genelindeki ortalamadan DAHA KÖTÜ bir tahmindir — o yüzden
           genele düşülür.
        """
        stats = self._stats_for_choice(symbol)

        unexplored = [combo for combo in self._combos if not self._seen(symbol, combo)]
        if unexplored:
            combo = rng.choice(unexplored)
            logger.info(
                "%s %s: denenmemiş kombinasyon (%d/%d kaldı) -> %s",
                self.name, symbol, len(unexplored), len(self._combos), combo.key,
            )
            return combo, stats[combo.key], "unexplored"

        if rng.random() < self._epsilon:
            combo = rng.choice(self._combos)
            logger.info(
                "%s %s: keşif çekilişi (epsilon=%.2f) -> %s",
                self.name, symbol, self._epsilon, combo.key,
            )
            return combo, stats[combo.key], "explore"

        measured = [combo for combo in self._combos if stats[combo.key].measured]
        if not measured:
            # Birinci adım her kombinasyonu en az bir kez oynattığı için buraya ancak
            # defter etiketleri okunamadığında düşülür (bkz. observe_closed_trades'in
            # "tanınmayan kombinasyon" uyarısı). Sabit bir kombinasyonu ayrıcalıklı
            # kılmamak için çekiliş yapılır.
            combo = rng.choice(self._combos)
            logger.info(
                "%s %s: henüz ölçüm yok, eşit çekiliş -> %s", self.name, symbol, combo.key
            )
            return combo, stats[combo.key], "explore"

        combo = max(measured, key=lambda item: (stats[item.key].mean_r, item.key))
        logger.info(
            "%s %s: en iyi kombinasyon -> %s (R=%.3f, n=%d)",
            self.name, symbol, combo.key, stats[combo.key].mean_r, stats[combo.key].trades,
        )
        return combo, stats[combo.key], "exploit"

    def _seen(self, symbol: str, combo: Combo) -> bool:
        """Bu SEMBOLDE bu kombinasyonun en az bir kapanmış işlemi var mı?"""
        local = self._by_symbol.get(symbol, {}).get(combo.key)
        return local is not None and local.measured

    def _stats_for_choice(self, symbol: str) -> dict[str, ComboStats]:
        """Kombinasyon -> istatistik; sembolde yeterli örnek yoksa o hücre GENELE düşer.

        Geri düşüş kombinasyon BAZINDADIR, sembolün tamamı için değil: bir sembolde bir
        kombinasyon 10 kez, diğeri 1 kez oynanmış olabilir ve ölçülmüş olanı genelin
        ortalamasına feda etmek, sembolün gerçekten taşıdığı bilgiyi atmak olurdu.
        """
        per_symbol = self._by_symbol.get(symbol, {})
        resolved: dict[str, ComboStats] = {}
        for combo in self._combos:
            local = per_symbol.get(combo.key)
            if local is not None and local.trades >= self._min_symbol_samples:
                resolved[combo.key] = local
            else:
                resolved[combo.key] = self._global[combo.key]
        return resolved

    def _round_rng(self, market: MarketData) -> random.Random:
        """Tur ve model başına bağımsız RNG; tohum sabit, seçim tekrarlanabilir."""
        return random.Random(f"{self._seed}:{market.as_of.isoformat()}:{self.name}")


# --------------------------------------------------------------------------- #
# Yardımcılar
# --------------------------------------------------------------------------- #
def _build_combos(
    band_mults: Sequence[float], tp_mults: Sequence[float], *, sl_mult: float
) -> list[Combo]:
    """Kartezyen çarpım, SABİT sırayla. Sıra tekrarlanabilirliğin parçasıdır.

    `sl_mult` grid'in bir ekseni DEĞİLDİR (kaynakta `VWAP_SL_MULT` sabittir): stop
    mesafesi zaten `band_mult` üzerinden oynar ve üçüncü bir eksen grid'i 27 kombinasyona
    çıkarıp her birinin örneklemini üçe bölerdi.
    """
    if not band_mults or not tp_mults:
        raise ValueError("vwap.clone çarpan listeleri boş olamaz")
    if any(value <= 0.0 for value in band_mults):
        raise ValueError(f"vwap.clone.band_mults pozitif olmalı: {list(band_mults)}")
    # `tp_mult > 1.0` hedefi VWAP'in ÖTESİNE taşırdı; kaynağın "hedef VWAP'i asla aşmaz"
    # kuralı `max(...)` kırpmasında değil, çarpanın kendisinde durur.
    if any(not 0.0 < value <= 1.0 for value in tp_mults):
        raise ValueError(
            f"vwap.clone.tp_mults (0, 1] aralığında olmalı — hedef VWAP'i aşamaz: "
            f"{list(tp_mults)}"
        )
    return [
        Combo(
            key=f"band{band_index}_tp{tp_index}",
            params=clone_signal.CloneParams(
                band_mult=float(band_mult), tp_mult=float(tp_mult), sl_mult=float(sl_mult)
            ),
        )
        for band_index, band_mult in enumerate(band_mults)
        for tp_index, tp_mult in enumerate(tp_mults)
    ]


def _empty_stats(combos: Sequence[Combo]) -> dict[str, ComboStats]:
    return {combo.key: ComboStats(trades=0, mean_r=NAN) for combo in combos}


def _summarize(values: Sequence[float]) -> ComboStats:
    if not values:
        return ComboStats(trades=0, mean_r=NAN)
    return ComboStats(trades=len(values), mean_r=sum(values) / len(values))
