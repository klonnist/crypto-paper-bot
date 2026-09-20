# Backtest: ön-kayıtlı değerlendirme kuralları

**Bu belge sonuç üretilmeden ÖNCE yazıldı ve commit edildi.** Tarih damgası git geçmişindedir.

Gerekçe bu oturumun kendi geçmişidir. İki kez, veriye bakıp sonradan kural yazmanın nasıl
yanlış sonuca götürdüğünü ölçtük:

- **Saat hipotezi** (karar 27): tek günün verisinde doğrulanır göründü, altı günde TERSİNE
  döndü, permütasyon testinde p=0.575 çıktı.
- **Kayıp serisi cooldown'u** (karar 28): kümelenme gerçekti (p<0.002) ama 12 parametre
  kombinasyonundan en iyisini seçmenin kazancı, kümelenmesiz karıştırılmış dizide de
  p=0.48 olasılıkla çıkıyordu.

İkisinde de kuralı **önce** yazmış olsaydık tuzağa hiç girmezdik. Backtest, bu tuzağın en
verimli üreme alanıdır: pencereyi, parametreyi ve metriği sonuca bakarak seçebildiğin
sürece her sistem kârlı görünür.

---

## 0. Mimari: ikinci bir uygulama YOKTUR

Backtest ayrı bir motor değildir. `core/engine.py` atlanan turları telafi ederken zaten
tam olarak bir backtest yapar: son işlenmiş bardan `as_of`'a kadar **her barı sırayla**
işler, emirler kendi barının ertesinden dolar (kural 13), stop/TP/likidasyon her barın
kendi `high`/`low`'uyla kontrol edilir (kural 12/13).

Sadakat iddia değil, **testle sabittir**:

```
tests/test_engine_per_bar.py::test_catch_up_matches_running_each_bar_in_its_own_round
  -> bir turda telafi edilen N bar ile N ayrı turda koşulan N bar
     BİREBİR AYNI defteri üretir
```

Harness'ın yaptığı üç şey vardır ve hiçbiri çekirdeğe dokunmaz:

1. Ayrı bir defter kökü (`backtests/<koşu-id>/`) — gerçek defter hiçbir koşulda yazılmaz
2. Her modelin `last_processed_bar`'ını pencere başına **tohumlar** (boş defterde
   `core/engine.py::_timeline` yalnızca son barı işler)
3. `signals_per_bar: true` (bkz. §5, base katmanının bilinçli sapması)

Backtest'e özel hiçbir maliyet, dolum, likidasyon ya da metrik kuralı YOKTUR. Hepsi
canlıyla aynı koddan gelir; ayrışsalardı backtest kendi uydurduğu bir dünyayı ölçerdi.

---

## 1. Kapı 0 — harness kanıtlanmadan hiçbir sayı okunmaz

**Hiçbir backtest sonucu, harness canlı veriye karşı doğrulanmadan yorumlanmaz.**

Elimizde yer gerçeği var: canlı scalp defteri ve her turun `round.models[].emitted`
kaydı — yani her barda her modelin tam olarak hangi sinyali ürettiği.

Test: config'in DEĞİŞMEDİĞİ bir canlı pencere seçilir, backtest aynı pencerede koşturulur
ve üretilen sinyaller canlı kayıtla karşılaştırılır.

| Model sınıfı | Beklenti |
|---|---|
| **Uyarlanabilir OLMAYAN** (`scalp_fixed`, `scalp_managed`, `vwap_managed`) | Sinyaller **birebir** eşleşmeli. Sinyal, piyasa verisinin ve sabit tohumun saf fonksiyonudur. |
| **Uyarlanabilir** (`scalp_bandit`, `vwap_clone`) | Eşleşme BEKLENMEZ ve aranmaz: ikisi de kendi kapanmış işlemlerinden öğrenir (kural 16), boş defterden başlayan bir koşu farklı bir geçmiş görür. Rapor edilir, kapı sayılmaz. |

**Birebir eşleşme sağlanmazsa hiçbir backtest çıktısı yorumlanmaz.** Önce fark açıklanır.

Config'in değiştiği pencereler bu teste uygun değildir: `fee_rate` (karar 25) ve
`vwap.managed.atr_multiple` (karar 26) 15 Eylül'de değişti, o tarihten öncesi ile sonrası
aynı kurallarla koşmadı.

**Kapı 0 HARNESS düzeyindedir, pencere düzeyinde DEĞİL.** Kapı "bu harness canlı motoru
yeniden üretiyor mu" diye sorar; cevap harness'ın bir özelliğidir ve her yeni pencerede
yeniden kazanılması gerekmez. Bu ayrım pratik bir zorunluluktur: canlı scalp katmanı
2026-09-13'te başladı, yani ondan ÖNCEYE uzanan her pencerede canlı kayıt yoktur ve
karşılaştırma zorunlu olarak "yalnız backtest" dolu bir liste üretir. O liste bir sadakat
hatası DEĞİLDİR — ölçtüğü şey "canlı o tarihte henüz koşmuyordu"dur, ve onu kapı saymak
kapıyı veri kapsamının ölçüsüne çevirirdi.

Kural şu: Kapı 0, canlının TAMAMEN kapsadığı bir pencerede koşulur ve geçmelidir; geçtikten
sonra aynı config ile koşulan daha uzun pencereler o doğrulamaya dayanır. Config değişirse
(yukarıdaki iki karar gibi) kapı yeniden koşulur — çünkü o zaman ölçülen harness değil,
harness'ın okuduğu kurallar değişmiştir.

---

## 2. Ölçülen şey

**Birincil metrik: pozisyon başına ortalama R** — canlıyla birebir aynı tanım
(`core/metrics.py`, `merge_fills` ile pozisyona indirgenmiş). Toplam getiri birincil
DEĞİLDİR: bileşiklenme hızını ölçer, sinyal kalitesini değil.

İkincil (raporlanır, karara tek başına girmez): `R/gün` (ort.R × işlem/gün),
`max_drawdown_r`, `cost_per_r`, `avg_stop_distance_pct`, işlem/gün, long/short kırılımı.

---

## 3. Geçerlilik kapıları — sağlanmazsa satır OKUNMAZ

| # | Kapı | Eşik |
|---|---|---|
| **B-0** | Kapı 0 (§1) geçilmiş | zorunlu |
| **B-1** | Örneklem | R'ye giren kapanmış **pozisyon ≥ 30** (`acceptance.min_trades`) |
| **B-2** | Veri bütünlüğü | `missing_bars = 0` **ve** `unchecked_position_bars = 0` |

B-2'nin gerekçesi: ikisi de "o barda stop/TP/likidasyon hiç sorulmadı" demektir. Sıfırdan
büyükse pencere eksik bir geçmişin üstüne yazılmıştır ve sonucu yorumlanamaz.

---

## 4. Canlıya alma eşiği — **hepsi** sağlanmalı

Bir model ancak aşağıdakilerin **tamamı** sağlanırsa canlıya alınır:

| # | Koşul |
|---|---|
| **C-1** | ortalama R **> 0** |
| **C-2** | `random_ctrl`'ün ortalama R'sini **≥ 0.15R** marjla geçer (`acceptance.edge_margin_r`) |
| **C-3** | hesap getirisi referans çıpasını (`buyhold`) geçer |
| **C-4** | `avg_stop_distance_pct` yarışmacı bandının İÇİNDE (⚠B yanmıyor) |
| **C-5** | **C-1, C-2 ve C-3 OOS penceresinde de sağlanıyor** (§6) |

**Scalp katmanında C-3 değerlendirilemez:** o katmanda referans çıpası yoktur
(`vwap_clone` bir kopyadır, çıpa değil — kural 15b). `core/metrics.py::acceptance_flags`
bu durumu zaten `logger.warning` ile söyler ve koşul düşer. Scalp'e bir çıpa eklemek AYRI
bir karardır; bu belge onu varsaymaz ve eksik çıtayı geçilmiş çıta gibi göstermez.

---

## 5. Bilerek kabul edilen sapmalar

Bunlar sonuç görülmeden yazıldı ve sonuç okunurken hatırlanacak.

### 5a. `base` katmanı: `signals_per_bar` sapması — KABUL EDİLDİ

Canlıda base katmanı `signals_per_bar: false` ile ve 6 saatte bir koşar (4 saatlik barda),
yani bazı barların sinyal fırsatını hiç kullanmaz. Backtest ise her barın sinyalini üretir.

**Sonuç: base backtest'i canlıdan DAHA ÇOK işlem yapar.** Kapalı koşmak alternatif değil —
o hâlde tüm pencere tek bir sinyal üretir ve ölçüm hiç olmaz.

Bu sapma base katmanının backtest'ini **işlem sıklığı açısından iyimser** kılar. Ortalama
R'yi doğrudan şişirmez (R işlem başınadır), ama `R/gün` ve toplam getiri satırları canlıda
elde edilemeyecek bir kadansı varsayar. Base için **yalnızca ortalama R** karara girer;
`R/gün` ve toplam getiri bilgi olarak raporlanır.

Scalp katmanında böyle bir sapma yoktur: canlıda da `signals_per_bar: true`.

### 5b. Evren look-ahead

`base` evreni "24s hacme göre ilk 50"dir ve bugünün hacmiyle seçilmiştir; geçmiş bir
pencereye uygulamak survivorship bias taşır (bugün likit olan sembol o gün olmayabilirdi,
ve o gün likit olup bugün listeden düşmüş semboller hiç görünmez). `scalp`'in sabit 13
sembolü de bugün bilinerek seçildi.

Bu, modeller ARASI kıyası bozmaz (hepsi aynı evreni görür, kural 6) ama mutlak getiriyi
iyimser gösterir. **Backtest'in mutlak getirisi bir tahmin değildir.**

### 5c. Maliyet modeli varsayımdır, gözlem değil

`fee_rate`, `slippage_base`, `slippage_short_stop` ve funding birer varsayımdır. Backtest
canlıyla **aynı** varsayımı kullanır, dolayısıyla göreli kıyas geçerlidir; ama gerçek
dolum, kitap derinliği ve borsa kesintisi modellenmez.

### 5e. Dolum belirsizliği tek yönlü bir varsayımdır

Kural 13 aynı mumda hem stop hem hedef aralığa girdiğinde **kötü olanın (stop)
gerçekleştiğini** varsayar. Varsayım muhafazakârdır ve doğru taraftadır — iyimser olanı,
elde olmayan bir bilgiyle kâr yazmak olurdu — ama tek yönlüdür, yani sonuçları
sistematik olarak AŞAĞI çeker.

Payı artık ölçülüyor: `ModelReport.stop_exits` / `ambiguous_stop_exits` ve backtest
çıktısındaki **AYNI-BAR BELİRSİZLİĞİ** tablosu, stop'la kapanan pozisyonların kaçında
aynı mumun hedefe (ya da kısmi çıkış seviyesine) de değdiğini söyler.

**Sayı bir düzeltme değil, bir tetikleyicidir.** Oran küçükse varsayım tartışmaya değmez.
Büyükse sıra varsayımını oynatan bir duyarlılık koşusu gerekir — ve o koşu **yalnızca
harness'ta** yapılır: canlı defterin kuralı tek olmalıdır (aynı gerekçe `signals_per_bar`in
base'de kapalı tutulmasıdır — biriken geçmişin bir kısmı bir kuralla, kalanı başka bir
kuralla üretilemez). Duyarlılık koşusu bir sonuç DEĞİL, sonucun etrafındaki banttır.

### 5d. Veri derinliği

`data.history_bars` pencereyi sınırlar (base 600 bar ≈ 100 gün; scalp 1500 bar ≈ 15,6 gün).
Pencere derinliği aşarsa çıpanın serisi `last_processed_bar`'a ulaşmaz ve **B-2 kapısı
düşer** — sessiz bir kısalma olmaz, sayı raporda görünür.

---

### 5b. Derinlik override'ı (`--history-bars`)

`data.history_bars` bir ÖLÇÜM kuralı DEĞİLDİR: maliyet, risk, dolum, likidasyon ve metrik
tanımlarına dokunmaz; yalnızca anlık görüntünün ne kadar geriye gittiğini söyler. Backtest
onu DERİNLEŞTİREBİLİR.

**Neden config'te global olarak yükseltilmiyor:** canlı bundan faydalanmaz. Modellerin
lookback'leri sınırlıdır (`tail(300)`, `rolling(20)`, EMA50, gün-çapalı VWAP), yani daha
derin geçmiş canlı sinyalini değiştirmez — ama `data/` depoya girmediği için her saatlik
tur veriyi baştan indirir ve global bir artış, faydasız yere her turda kat kat indirme
demekti.

**"Değiştirmez" bir varsayım değil, SINANAN bir iddiadır:** aynı override ile koşulan
Kapı 0, canlı kayıtla birebir eşleşmeye devam etmelidir. Eşleşmezse override bir ölçüm
sapması üretiyor demektir ve kullanılamaz.

**Yalnızca derinleştirir, sığlaştırmaz** (`ValueError`): `tail(300)` okuyan bir model 200
barlık görüntüde BAŞKA bir sinyal üretir ve backtest artık canlıyı değil, kendi uydurduğu
bir modeli ölçerdi.

Kullanılan değer her koşuda `manifest.json > history_bars` altında (`config` ve `used`)
yazılı durur.

### 5g. Harness override'ları İKİ SINIFA ayrılır ve karıştırılmazlar

Bayrakların hepsi "koşuyu değiştiren ayar" değildir; ikisi arasındaki fark, sonucun nasıl
okunacağını belirler.

**Sınıf 1 — sonucu DEĞİŞTİRMEMESİ beklenen (ve sınanan) ayarlar.** `--history-bars`
(§5b) ve `--funding-periods`: ikisi de anlık görüntünün derinliğini artırır. İddia
"modelin gördüğü sinyal değişmez"dir ve bu bir varsayım değil, Kapı 0'da SINANAN bir
iddiadır. İkisi de yalnızca DERİNLEŞTİRİR; sığlaştırma `ValueError`dır, çünkü sığ veri
modeli canlıda olmadığı bir şeye çevirir. `--funding-periods`in ayrıca bir yönü vardır:
varsayılan 180 periyot ≈ 60 gündür ve yıllara uzanan bir pencerede kaydı olmayan anda
`core/funding.py::rate_at` None döner — funding HİÇ işlenmez (uydurma yok), yani eski
dönem sistematik olarak İYİMSER çıkar. Derinleştirmek bu yanlılığı kapatır.

**Sınıf 2 — sonucu DOĞRUDAN kaydıran ayarlar.** `--fee-rate`, `--slippage-base`,
`--symbols`, `--signal-cutoff`. Bunlar ölçümün koşullarını değiştirir ve sonucu canlı
defterle aynı dünyada bırakmaz:

| Bayrak | Ne için | Neden canlıya girmez |
|---|---|---|
| `--fee-rate`, `--slippage-base` | dış bir referansla (başka bir backtest aracı) parite | kural 6: maliyet tüm modeller için tek kaynaktır; `fee_rate` bir kez değiştiğinde defteri tarihli olarak böler (karar 25) |
| `--symbols` | tek sembollü parite koşusu | çok sembollü koşuda portföy kotası (`max_positions`) sinyal REDDEDER ve dışarıdaki tek sembollü bir koşuyla kıyas, modelin değil kotanın ölçüsü olur. Evreni yalnızca DARALTIR: genişletmek katmanın tanımını (kural 6) harness'a devretmek olurdu |
| `--signal-cutoff` | dönem ataması | bir işlem GİRİŞ tarihine göre döneme aittir; dönemin son kurulumları sınırı aşsa bile kapanışına kadar o döneme sayılır. Kesim YENİ sinyali durdurur, pozisyon yönetimini DURDURMAZ — dondurmak, o kurulumları kendi çıkış kurallarından mahrum bırakıp sonucu uydururdu |

Sınıf 2'nin her kullanımı `manifest.json > deviations` altında durur ve maliyet override'ı
ayrıca `logger.warning` ile bağırır: **bir sayının hangi dünyada ölçüldüğü, sayının kendisi
kadar ölçümün parçasıdır.**

---

### 5f. Uzun pencere bir DERİNLİK sorunudur, bir niyet değil

"2-3 yıllık araştırma backtest'i" ayrı bir motor ya da ayrı bir mimari gerektirmez —
harness zaten canlı motoru geçmiş bir pencerede koşturur (§0). Gerektiren şey **veridir**
ve sınır ağdadır, diskte değil:

- OKX `history-candles` istek başına **100 bar** verir. 3 yıllık 15m verisi sembol başına
  ~105.000 bar, 13 sembol için ~13.600 istek demektir — throttle ile saatler.
- `data/` depoya GİRMEZ (çalışma zamanı verisi, denetim izi değil) ve runner her koşuda
  sıfırdan kurulur; önbelleksiz her koşu bu maliyeti baştan öder.

Bu yüzden `backtest.yml` mum önbelleğini koşular arasında **taşır** (`actions/cache`,
rolling anahtar). Önbellek ölçümü değiştiremez: `core/data.py` yalnızca EKSİK barları
çeker ve kapanmamış barı zaten atar (kural 12). Bayat bir önbellek **eksik** bar üretir,
yanlış bar değil — ve eksik bar B-2 kapısında (`missing_bars`,
`unchecked_position_bars`) görünür, sessizce geçmez.

**Pencere uzadıkça §5b'nin evren look-ahead uyarısı AĞIRLAŞIR.** `scalp`in sabit 13
sembolü bugün bilinerek seçildi; 2023'e uzanan bir pencerede bu listenin bir kısmı ya
listelenmemişti ya da ince kitaplıydı, ve o dönemde likit olup bugün düşmüş semboller hiç
görünmez. Uzun pencerenin MUTLAK getirisi bu yüzden bir tahmin değildir; modeller ARASI
kıyas geçerli kalır (hepsi aynı evreni görür, kural 6).

---

## 6. Kontaminasyon ve OOS

Bir modelin parametresi hangi veride seçildiyse o veri onun için **in-sample**'dır ve
üzerinde ölçülen başarı kanıt değildir.

| Model | Durum |
|---|---|
| `trend`, `meanrev`, `momentum`, `squeeze`, `confluence`, `failed_breakout`, `downtrend_rally`, `avwap`, `random_ctrl`, `ensemble`, `buyhold` | Parametreleri son 30 günün verisinden seçilmedi -> **tüm pencere OOS** |
| `scalp_bandit`, `scalp_fixed`, `scalp_managed` | Aynı -> **tüm pencere OOS** |
| `vwap_clone` | Kaynak sistemin parametreleri; bizim veriyle seçilmedi -> **OOS** |
| **`vwap_managed`** | `atr_multiple=2.5`, **17 Ağu – 16 Eyl 2026** verisinde seçildi (karar 26) -> o pencere **IN-SAMPLE** |
| **`vwap_fast`** (henüz kurulmadı) | `band_mult` aynı pencerede ölçüldü -> o pencere **IN-SAMPLE** |
| **`ema_trend`** | Parametreleri dış bir sistemde (TradingView), **2022-01 – 2024-06** BTC verisinde doğrulandı -> o pencere **IN-SAMPLE**; 2024-07 sonrası OOS (§6d) |

**Kural:** `vwap_managed` ve `vwap_fast` için C-1..C-3, **17 Ağu 2026'dan ÖNCEKİ** bir
pencerede de sağlanmalıdır (C-5). O pencerede sağlanmıyorsa kalibrasyon o 30 güne
uydurulmuş demektir.

### 6.1 Embargo — OOS penceresinin başındaki boşluk

Parametresi `T` tarihine kadarki veriyle seçilmiş bir model için, OOS penceresini `T`'den
hemen başlatmak **temiz değildir.** Sebep pozisyonların zaman içinde ÖRTÜŞMESİDİR: IS
penceresinin son kurulumları `T`'den sonraki barlarda çözülür, yani o barların fiyat
hareketi IS etiketlerinin sonucuna zaten katkı yapmıştır. Aynı barları OOS'un ilk
sonuçları olarak okumak, "bağımsız pencere" iddiasını sessizce çürütür.

**Doğru boşluk tam olarak azami tutuş süresidir** (`scalp.time_stop_bars`; `patient` için
100 bar), çünkü bu geometride hiçbir pozisyon ondan uzun yaşamaz — zaman stop'u kapatır.
Daha uzun bir embargo veri harcar, daha kısası örtüşmeyi bırakır.

**Neden tam bir purged walk-forward makinesi değil.** Purging/embargo literatürdeki ağır
biçimiyle, etiketleri belirsiz sürelerle örtüşen ve ÇOK sayıda parametresi süpürülen
kurulumlar içindir. Burada ikisi de yok: tutuş süresi sınırlı ve BİLİNEN, süpürülmüş
parametre ise pratikte tek (`vwap.managed.atr_multiple`, karar 26 — ve o zaten §6'da
IN-SAMPLE olarak işaretli). `scalp.patient.time_stop_bars = 100` veriden değil teoriden
gelir (`N = (hedef/ATR)²`), `scalp_vol`'un eşiği medyandır (serbest parametre değil).
Yani düzeltilecek olan seçim yanlılığı burada elle ve açıkça yönetiliyor; eklenen tek şey
örtüşme boşluğudur. Süpürülen parametre sayısı 1'i geçtiğinde bu karar yeniden açılır.

**Uygulama:** `scripts/backtest.py --embargo-bars N`. Pencere N bar ileri kaydırılır ve
`manifest.json > window` hem `requested_start`i hem uygulanan `start`i yazar — boşluk
sessiz olamaz.

---

## 6b. ÖN-KAYIT — `scalp_vol` (model 17)

**Bu bölüm koşudan ÖNCE yazıldı ve commit edildi; tarih damgası git'tedir.** Sonuç
görüldükten sonra hiçbir satırı değiştirilmeyecek (§7).

**Hipotez.** Friksiyon notional'ın sabit bir yüzdesidir; sinyalin sürüklenmesi volatiliteyle
ölçeklenir. `scalp_patient`in başabaş noktası evrenin MEDYAN volatilitesindedir (karar 35:
brüt %0.261 ↔ maliyet %0.284). O hâlde medyanın ÜSTÜNDEKİ kurulumlarla sınırlanmış aynı
model, daha yüksek brüt sürüklenme üretmelidir.

**Eksen.** `scalp_patient` ↔ `scalp_vol`; ayrışan TEK şey kesitsel ATR% medyan kapısı.
Kol seçimi, kapılar, geometri, 100 barlık zaman stop'u ve çekiliş kimliği miras.

**Pencere.** 2026-07-19 → 2026-09-04, `--history-bars 6000`. Bu pencere `scalp_vol` için
TAZEDİR (model o koşudan sonra yazıldı). Sonuca göre kaydırılmayacak (§7.3).

**ÖN-KAYITLI TAHMİNLER** (sonucu görmeden):

| # | Ölçüm | Tahmin | Çürütür |
|---|---|---|---|
| **P1** | brüt sürüklenme% | `scalp_vol` > `scalp_patient` (%0.261) | ≤ %0.261 |
| **P2** | maliyet/R | `scalp_vol` < `scalp_patient` (0.124) — stop ATR ile büyür | ≥ 0.124 |
| **P3** | örneklem | n ≥ 30 | n < 30 → satır okunmaz (B-1) |
| **P4** | stop bandı | ⚠B yanmaz (C-4) | yanarsa kıyas geçersiz |

**P1 birincildir.** P1 tutmazsa "edge σ ile ölçeklenir" varsayımı YANLIŞTIR — o zaman edge
mutlak, maliyet oranlıdır ve bu geometride hiçbir 15m tezi kurtarılamaz. Bu, **olumsuz
çıktığında da değerli** bir ölçümdür ve o durumda doğru hamle katmanı kapatmak ya da maliyet
çalışmasına geçmektir.

**P2 bir SAĞLAMADIR, bir başarı ölçütü değil.** Kapı ATR'yi seçiyorsa stop mesafesi
büyümek zorundadır. P2 tutmazsa kapı ATR'yi değil başka bir şeyi seçmiştir ve P1'in sonucu
yorumlanamaz.

**Canlıya alma eşiği (§4) AYRICA geçilmelidir.** P1'in tutması `scalp_vol`u canlıya almaz;
C-1 (ort. R > 0) ayrı bir çıtadır ve `scalp_patient` onu −0.01 ile geçememişti.

**Çoklu karşılaştırma (§7.5).** Araştırmadan 10 öneri çıktı; bu, test edilen **1.**sidir.
Sonuç raporlanırken bu sayı yazılacak.

---

## 6c. ÖN-KAYIT SİCİLİ — her hipotez, sonucu ne olursa olsun, buraya yazılır

**Bu tablo §7.5'in ("çoklu karşılaştırma açıkça raporlanır") tutulan hâlidir.** §7.5 bir
RAPORLAMA kuralıydı ve sayıyı tutan bir yer yoktu; karar 36 "10 önerinin 1.'si" diye
saymaya başlamıştı ama sayaç hiçbir yerde durmuyordu. Sicil olmadan çoklu karşılaştırma
düzeltmesi yapılamaz, çünkü düzeltme tam olarak "kaç test yapıldı" sayısına dayanır.

**Kayıt kuralı:** bir hipotez koşulmadan ÖNCE satırı açılır (ön-kayıt commit'i ile), koşu
bittiğinde sonucu yazılır. **Düşen hipotez silinmez.** Silmek, paydayı küçültüp kalan
sonuçları olduğundan anlamlı gösterirdi — yayın yanlılığının kendisi budur ve bu projede
onu üretecek olan tek şey bu tabloyu düzenlemektir.

| # | Hipotez | Ön-kayıt | Pencere | Birincil tahmin | Sonuç |
|---|---|---|---|---|---|
| 1 | `scalp_vol`: edge σ ile ölçeklenir | §6b, commit `a7c08ae` | 2026-07-19 → 09-04 | P1: brüt sürüklenme% `vol` > `patient` | **DÜŞTÜ** (0.253 < 0.263) — karar 36 |
| 2 | `ema_trend`: EMA(21/55) kesişimi long-only bir edge taşır (dış sistemden) | §6d, commit `e912efd` (TADİLAT-1: `93cd891`) | A: 2022-01-01 → 2024-12-30 (sinyal kesimi 06-30), B: 2024-07-21 → 2026-09-18 | P1: BTC tek-sembollü PF A 1.551±0.2 / B 1.299±0.2 | **P1 TUTTU** (1.549 / 1.206) ama **hipotez DÜŞTÜ**: ortalama R A −0.0015 / B −0.0165 (C-1), çıpa da geçilemedi (C-3) → **BLOKE**, §6d > SONUÇ |
| 3 | `ema_trend` çıkış varyantları: kenar giriş sinyalinde, çıkış geometrisi yiyor | §6e (güç ve kabul kuralları), commit `9ce4f34`; varyant tanımları HİÇ yazılmadı | A: 2022-01-01 → 2024-12-30 (teşhis; B'ye dokunulmadı) | ön-kayıtlı seçim kuralının bir dalının tetiklemesi | **DÜŞTÜ — teşhis aşamasında** (M2 0.153 < 0.25, M1 0.552 < 1.0, M4 0.667 < 2.0): tur kapandı, varyant kurulmadı — §6e > SONUÇ, karar 49 |

**3. satır BH paydasına GİRMEZ ve bu bir muafiyet değil bir tanımdır:** hipotez bir
model koşusuna hiç dönüşmedi, yani ortada düzeltilecek bir `p` değeri yok. Satırın
sicilde durmasının sebebi paydanın kendisi değil, **kaç denemenin yapıldığının
görünmesidir** — düşen bir denemeyi silmek, sicilin engellemek için var olduğu yayın
yanlılığının ta kendisidir.

**Sicildeki 2. satır bu paydaya AİT DEĞİLDİR:** `ema_trend` hipotezi dış bir sistemden geldi, ev içi arama uzayından seçilmedi (bkz. §6d > Çoklu karşılaştırma). İki payda ayrı tutulur.

**Araştırmadan çıkan öneri sayısı: 10.** Bunların 1'i test edildi (yukarıdaki), 4'ü
ölçüm katmanı olduğu için hipotez DEĞİLDİR ve sicile girmez (kabul kapısı, belge
senkronu, dolum belirsizliği sayımı, sicilin kendisi — hiçbiri bir modelin performansı
hakkında bir iddia taşımaz), 2'si reddedildi (işlem sıklığı tavanı, sembol eleme), 3'ü
kuyrukta (maliyet modeli, rejim filtresi, portföy tavanı).

### Çoklu karşılaştırma düzeltmesi: Benjamini-Hochberg

`m` ön-kayıtlı birincil tahminin `p` değerleri küçükten büyüğe sıralanır (`p₍₁₎ ≤ … ≤ p₍ₘ₎`)
ve `p₍ᵢ₎ ≤ (i/m) × q` koşulunu sağlayan en büyük `i`ye kadarki hipotezler kabul edilir;
`q = 0.10` (yanlış keşif oranı).

**Neden Bonferroni değil.** Bonferroni aile düzeyinde HATA OLASILIĞINI kontrol eder ve 10
test için eşiği 0.005'e indirir; bu geometride (n≈200, ort. R ≈ −0.01) gerçek bir edge
bile o eşiği geçemez, yani prosedür her şeyi eler ve ölçüm anlamını yitirir. BH ise
yanlış keşif ORANINI kontrol eder: bir tanesinin yanlışlıkla geçmesini tamamen engellemez
ama geçenlerin çoğunluğunun gerçek olmasını sağlar. Bir kâğıt katmanında doğru denge budur
— yanlış bir modeli kâğıtta koşturmanın bedeli, gerçek bir edge'i hiç görmemenin
bedelinden küçüktür.

**`q` sonucu görmeden sabitlendi ve §7.4 gereği sonuca göre değiştirilmeyecektir.**

**Düzeltme kabul çıtasının (`acceptance`) YERİNE geçmez, ONDAN SONRA gelir.** Çıta tek bir
modelin kendi verisiyle cevaplanabilen soruyu sorar ("bu satır okunabilir mi"); BH ise
"kaç hipotez denendi" sorusunu sorar ve cevabı tek bir modelin defterinde yoktur. Bir
model iki kapıyı geçip BH'de düşebilir; o zaman sonuç "edge yok" değil, **"bu kadar
denemeden sonra bu kadar farkı görmek beklenirdi"** demektir.

## 6d. ÖN-KAYIT — `ema_trend` (model 18) ve `ema` katmanı

**Bu bölüm koşudan ÖNCE yazıldı ve commit edildi; tarih damgası git'tedir.** Sonuç
görüldükten sonra hiçbir satırı değiştirilmeyecek (§7).

**Kökeni dışarıdır.** Hipotez bu projenin araştırma listesinden (§6c) çıkmadı: modelin
kuralları dış bir sistemde (TradingView / Pine Script) geliştirildi ve orada doğrulandı.
Bu, kopyalardan (kural 15b) farklıdır — kopya dış sistemin BOYUTLANDIRMASINI da taşır,
bu model ise evin kurallarıyla (risk %1, `leverage_cap`, ev maliyetleri) koşan tam bir
YARIŞMACIDIR. Dışarıdan gelmesi ön-kaydı gereksiz kılmaz, tersine zorunlu kılar: dış
sistemin doğruladığı pencere bizim için de IN-SAMPLE'dır (§6).

### Sabitlenen kurallar — koşudan sonra DEĞİŞMEZ

| | |
|---|---|
| Yön | **yalnızca long** (`allowed_directions=["long"]`); short kaynak sistemde denendi ve sistematik kaybettiriyordu |
| Giriş | EMA(21), EMA(55)'i YUKARI keser (önceki bar `fast <= slow`, bu bar `fast > slow`) |
| Stop | giriş barının kapanışı − `1.5 × ATR(14)` |
| Hedef | giriş + (giriş − stop) × `2.0`, tek dilim (`fraction=1.0`) |
| Trailing | **yok** |
| Zaman stop'u | **yok** (bkz. embargo notu) |
| Pozisyon | sembol başına tek; tekrar sinyal `core/portfolio.py`'de `duplicate_position` ile reddedilir (model kendi pozisyonunu göremez — kural 4/16) |
| Dolum | kural 13: sinyal bar kapanışında, emir bir SONRAKİ barın açılışında |

**Parametreler bu koşu için SABİTTİR.** Sonuç kötü çıkarsa ayar aranmaz (§7.1); zaman
stop'u eklemek dâhil her değişiklik YENİ bir hipotezdir ve taze bir OOS penceresi ister.

### Katman

Model `ema` katmanında koşar (4H, `config.yaml > layers.ema`): sabit 13 sembol (scalp
katmanının evreni), `ledgers_ema/`, `docs/data/metrics_ema.json`. Katmanın model listesi
`ema_trend` ile birlikte `trend` (kıyas hedefi), `random_ctrl` (kabul çıtasının kontrolü)
ve `buyhold` (çıpa, C-3) içerir.

**Neden base katmanına değil.** Backtest 13 sabit coinde koşuyor; base evreni hacme göre
seçilen 50 coindir ve zamanla kayar. Modeli base'e almak, backtest'in ölçtüğü evrenden
başka bir evrende koşturmak olurdu. Ayrı katman ikisini eşitler. Bedeli katmanlar arası
kıyasın yapılamamasıdır (CLAUDE.md > Katmanlar) — bu yüzden kıyas hedefi `trend` KATMANIN
İÇİNE alınır: aynı evren, aynı barlar, aynı maliyet, aynı defter kuralları.

### Pencereler ve dönem atama

| Dönem | Aralık | Durum |
|---|---|---|
| **A** | 2022-01-01 → 2024-06-30 (sinyal kesimi) | **IN-SAMPLE** — kaynak sistem parametrelerini bu veride doğruladı |
| **B** | 2024-06-30 + embargo → koşu günü | **OOS** |

**Dönem atama ölçütü GİRİŞ tarihidir.** Dönem A'da açılan bir işlem sınırı aşsa bile
kapanışına kadar A'ya sayılır. Uygulaması harness'ta `--signal-cutoff`tur: kesimden
sonraki barlar pozisyon yönetimi için işlenir (stop/TP/likidasyon/funding) ama YENİ sinyal
üretilmez. Kuyruk 2024-12-31'de biter; o tarihte hâlâ açık olan pozisyonların SAYISI
raporlanır ve kapanmış işlem istatistiğine girmez (girseydi gerçekleşmemiş bir sonuç
ölçüme girerdi).

Kesim olmadan alternatif, sınırda açık olan pozisyonları düşürmekti; bu, dönem A'nın en
uzun yaşayan kurulumlarını sistematik olarak eleyip ortalamayı kısa işlemlere doğru
çekerdi.

### Embargo

Modelin zaman stop'u YOKTUR, yani §6.1'in "doğru boşluk azami tutuş süresidir" kuralının
dayandığı üst sınır bu modelde tanım gereği yok. Bu yüzden sınır ÖLÇÜLÜR: dönem A'da
gözlenen **azami tutuş süresi** embargo olarak uygulanır ve uygulanan değer
(`manifest.json > window.embargo_bars`) raporlanır.

**Tutuş süresi dağılımı ayrıca raporlanır** (medyan, 90. yüzdelik, azami — bar ve gün
cinsinden). Bu bir yan çıktı değil, ön-kayıtlı bir ölçümdür: uzun kuyruk, pozisyonların
hedefe/stop'a varmadan beklediğini gösterir ve P2'nin bağımsız kontrolüdür.

### TADİLAT-1 — ATR yumuşatması Wilder'a çevrildi (SPEC uyumu, sonuca bakılarak DEĞİL)

**Bu tadilat tam koşudan ÖNCE yapıldı ve gerekçesi burada, karar anında yazıldı.** §7 bir
ön-kaydın sonucu görüldükten sonra değiştirilmesini yasaklar; aşağıdaki kayıt o yasağın
denetlenebilir kalması içindir — okuyucu değişikliğin ne zaman, neye bakılarak yapıldığını
buradan görür.

**Sorun bir parametre farkı değil, SPEC farkıydı.** Kaynak sistem (TradingView / Pine
Script) `ta.atr` kullanır ve o Wilder yumuşatmasıdır (RMA); bu repo `simple` (TR'lerin düz
ortalaması) kullanıyordu. Stop mesafesi ve hedefin TAMAMI bu değerden türüyor, yani iki
sistem aynı kuralı koşmuyordu: "koşulan şey" ile "doğrulanan şey" ayrışmıştı.

**Değişiklik:** `ema_trend` artık `config.yaml > ema_trend.atr_smoothing: "wilder"` okur.
`core/indicators.py::average_true_range` iki yumuşatmayı da verir; **varsayılan `simple`
olarak KALDI** ve başka hiçbir modelin sayısı değişmedi.

**Küresel değişiklik REDDEDİLDİ.** Tanımı topyekûn Wilder'a çevirmek, bugün canlı koşan her
modelin (`trend`, `meanrev`, beş kollu scalp modelleri, `vwap_managed`) stop ölçeğini o
commit'ten itibaren kaydırırdı; biriken defterin bir kısmı bir ölçekle, kalanı başkasıyla
üretilmiş olur ve iki dönem kıyaslanamazdı — `fee_rate`in defteri tarihli olarak böldüğü
hatanın aynısı (docs/decisions.md > 25).

**Bedeli açıkça yazılıdır:** "1.5×ATR" ifadesi artık modeller arasında birebir
kıyaslanabilir DEĞİLDİR. Kural 14'ün kıyas ölçütü zaten ATR katı değil GERÇEKLEŞEN stop
mesafesidir (`avg_stop_distance_pct` ve ⚠B bandı), yani kıyas o kolondan okunmaya devam
eder. Motorun tavan kontrolü (`core/engine.py`) ORTAK tanımda kalır: her model kendi
yumuşatmasını seçerek kendi tavanını genişletebilseydi tavan bir kural olmaktan çıkardı.

**Bu tadilat bir kapıyı KURTARMIYOR.** Değişiklikten ÖNCE ölçülen BTC parite koşusu
(`backtest.yml` run 35373017427, dönem A, tek sembollü, ön-kayıtlı maliyetle) şunu verdi:

| | TradingView referansı | ölçülen (`simple` ATR) |
|---|---|---|
| kâr faktörü | 1.551 | **1.40** (sapma −0.151, tolerans ±0.2 → P1 GEÇTİ) |
| kazanma oranı | %46.67 | %44.2 |
| ödeme oranı | 1.79 | **1.76** |
| n | — | 43 |

Yani P1 kapısı `simple` ATR ile de GEÇİLMİŞTİ; tadilat düşen bir kapıyı geçirmek için
değil, spec uyumu için yapıldı.

**Tadilat SONRASI aynı pencere** (`backtest.yml` run 35382583335, `wilder` ATR):

| | TradingView referansı | `simple` (tadilat öncesi) | `wilder` (tadilat sonrası) |
|---|---|---|---|
| kâr faktörü | 1.551 | 1.40 (−0.151) | **1.55 (−0.001)** |
| kazanma oranı | %46.67 | %44.2 | **%47.0** |
| ödeme oranı | 1.79 | 1.76 | **1.77** |
| ortalama R | — | +0.24 | +0.32 |
| n | — | 43 | 45 |

**İki sayı da burada duruyor ve duracak** — ilk koşuyu silmek, sonucu görüp geçmişi
yazmak olurdu. Sonuç, tadilatın gerekçesini de doğruluyor: kalan sapmanın neredeyse
tamamı ATR yumuşatmasındanmış (kâr faktörü farkı −0.151 → −0.001). Veri kaynağı farkı
(OKX perp ↔ kaynak sistemin borsası) beklenenden küçük çıktı.

**Tutuş süresi (dönem A, BTC):** medyan 9 bar, azami **46 bar** (7.7 gün). Portföy
koşusundan ölçülecek embargo bu mertebede beklenir.

Aynı koşudan gelen, tadilat ÖNCESİ portföy referansı (13 sembol, dönem A, `backtest.yml`
run 35373248059): `ema_trend` n=359, ortalama R ≈ **−0.06**. Tek sembollü BTC koşusu
(+0.24R) ile portföy koşusunun ayrışması ölçümün kendisidir, tadilatın konusu değil.

### Veri kapsamı — KOŞUDAN ÖNCE ölçüldü

Bu bölüm bir sonuç değil, bir VERİ OLGUSUDUR ve ana koşudan önce ölçülmüştür (prob koşusu:
`backtest.yml`, run 35372230431, 2026-09-18; pencere 2022-01-01 → 2022-02-01). Sonuca göre
yazılmadığı için §7'nin kapsamına girmez; buraya yazılmasının sebebi tersidir — sonucu
görünce "zaten biliyorduk" denmesin.

- **OKX 4H verisi 2022-01'e uzanıyor**, yani dönem A'nın başlangıcı veri tarafından
  destekleniyor.
- **Dönem A'nın başında evren 13 değil 9 semboldür.** 2022-01'de OKX'te kapanmış barı
  olmayanlar: **BNB, SUI, PENGU, ETHFI**. (SUI/PENGU/ETHFI beklenen listelenme
  tarihleriyle uyumlu; BNB'nin de o tarihte OKX perpetual'i yok.) Semboller verileri
  başladığı anda evrene girer — `core/data.py` `as_of` barını taşımayan sembolü zaten o
  tur dışlar (kural 12), yani bu bir sessiz kayıp değil, loglanan bir kapsam sınırıdır.
- **Sonuç okunurken:** dönem A'nın erken kısmı daha DAR bir evrende ölçülür; coin başına
  tabloda o semboller `—` ile durur. Bu, K-1 kapısının (dönem B) birimini etkilemez —
  dört sembolün de dönem B'de verisi vardır.
- **Funding kayıtları 2022'de seyrek**: `core/funding.py::rate_at` kaydı olmayan anda None
  döner ve maliyet işlenmez (uydurma yok). Bu, dönem A'yı İYİMSER yapar ve §6d'nin
  "kabul edilen sapmalar" listesindeki 3. maddenin ölçülmüş hâlidir.

### Maliyet — bu koşu canlı config ile AYNI DEĞİLDİR

| | backtest | canlı (`config.yaml`) |
|---|---|---|
| `fee_rate` | **0.00075** (tek yön) | 0.00055 |
| `slippage_base` | **0.0001** | 0.0005 |

Talep eden taraf modelin sahibidir (TradingView koşusunun varsayımı). `config.yaml`
DEĞİŞTİRİLMEZ: kural 6 maliyeti tüm modeller için tek kaynağa bağlar ve `fee_rate` bir kez
değiştiğinde defteri tarihli olarak böler (karar 25). Bu yüzden sapma harness'ta,
`--fee-rate` / `--slippage-base` bayraklarıyla ve `manifest.json`'a yazılarak uygulanır.

**"2 tick" oransal kayma modeline birebir çevrilmez:** tick boyu sembole göre değişir ve
sembole bağlı kayma AYRI bir karardır (CLAUDE.md > `scripts/measure_slippage.py`). 0.0001
(1bp) bunun düz bir temsilidir ve seçim sonuçtan önce yapılmıştır.

**Yön bilinmektedir:** backtest komisyonda canlıdan pahalı (+0.0002 × 2 bacak), kaymada
ucuzdur (−0.0004 × 2 bacak); net etki tipik bir kurulumda kaymanın lehinedir, yani bu
koşu canlı maliyetten **daha ucuz** bir dünyayı ölçer. Forward test bu yüzden backtest'in
biraz ALTINDA kalmalıdır; tersi bir sürpriz değil, bir uyarıdır.

### Kapılar — İKİSİ DE bağlayıcı

**Repo kapıları birincildir** (§3 B-0/B-1/B-2 ve §4 C-1..C-5). Gerekçe ölçümün kendisidir:
diğer modeller bu eşikten geçti; yeni bir modele daha gevşek kapı açmak, onu iyi olduğu
için değil kapısı kolay olduğu için önde gösterirdi (kural 6).

**Model sahibinin kapısı EK filtredir**, yerine geçmez:

| # | Koşul |
|---|---|
| **K-1** | En az **6 coinde** dönem B kâr faktörü **> 1.1** (tek-sembollü koşulardan) |
| **K-2** | Toplam işlem sayısı (tüm coinler, dönem A+B) **> 300** |
| **K-3** | Hiçbir coinde max drawdown **%25'i geçmiyor** |

**K-3'ün tanımı** (sonuçtan önce sabitlenir): hesap tektir ve coin başına bölünemez
(ortak nakit, ortak margin — CLAUDE.md > Rapor Kolonları'nın yön bazlı Sharpe için verdiği
aynı gerekçe). Bu yüzden coin bazlı drawdown, o coinin **kümülatif PnL eğrisinin** en
büyük tepe-dip düşüşünün BAŞLANGIÇ SERMAYESİNE oranıdır (`initial_capital`, 10.000).
Hesap düzeyi `max_drawdown` ayrıca ve ayrı olarak raporlanır.

### Tek istisna — ve o da sonuçtan ÖNCE yazılıyor

Model **YALNIZCA C-3'ten** (hesap getirisi `buyhold` çıpasını geçer) kalıyor ve diğer tüm
kapıları (B-0..B-2, C-1, C-2, C-4, C-5, K-1..K-3) geçiyorsa: koşu **DURUR**, karar model
sahibine gider, otomatik geçiş YOKTUR. Başka herhangi bir kapıdan kalırsa doğrudan
**BLOKE** — istisna tek bir kapı içindir ve genişletilemez.

Gerekçe: C-3 diğer kapılardan farklı bir soru sorar ("piyasayı yendi mi") ve cevabı
modelin kendi verisiyle değil, ölçüldüğü pencerenin yönüyle belirlenir — 2022-2026
penceresinde BTC yaklaşık %110 yükseldi. Bu, kapıyı yumuşatmak için gerekçe DEĞİLDİR;
istisna kapıyı kaldırmıyor, kararı otomatikten insana taşıyor.

### ÖN-KAYITLI TAHMİNLER (sonucu görmeden)

| # | Ölçüm | Tahmin | Çürütür |
|---|---|---|---|
| **P1** | BTC tek-sembollü PF | A: **1.551 ± 0.2**, B: **1.299 ± 0.2** (TradingView referansı) | dışına çıkması → harness ↔ TV ayrışması araştırılır, sonuç YORUMLANMAZ |
| **P2** | hedefe ulaşma oranı | `tp` ile kapanan pozisyon payı **< %35** | ≥ %35 |
| **P3** | ortalama R | **> 0** (C-1) | ≤ 0 |
| **P4** | çıpa | hesap getirisi `buyhold`un **ALTINDA** (C-3 DÜŞER) | çıpayı geçerse |

**P1 birincildir ve bir KAPIDIR, bir tahmin değil.** Tutmazsa hiçbir sayı yorumlanmaz ve
paper trading'e geçilmez — modelin iyi ya da kötü olduğu değil, harness'ın kaynak sistemi
yeniden üretmediği anlaşılır. Tek-sembollü koşu şart: portföy kotası (`max_positions: 5`)
13 coinlik koşuda BTC sinyallerinin bir kısmını reddeder ve tek sembollü bir TradingView
koşusuyla kıyas geçersiz olurdu.

**P4 modelin sahibinin açık beklentisidir** (TradingView'de BTC 2022-2026: strateji
+%4.89, al-tut +%110) ve tam da bu yüzden ön-kayda yazılıyor: sonucu görüp "zaten
biliyorduk" demek ile önceden yazmak arasındaki fark, ölçümün kendisidir.

### Bu koşuya özgü, KABUL EDİLEN sapmalar (§5'in üstüne)

Kaynak sistemle birebir eşleşme beklenmez; sebepler sonuçtan önce yazılıdır:

1. **ATR tanımı.** Repo `core/indicators.py` TR'lerin DÜZ ortalamasını kullanır;
   TradingView `ta.atr` Wilder (RMA) yumuşatmasını. Projenin tek ATR tanımı vardır
   (`trailing.atr_period`) ve modele özel bir ATR, aynı "1.5×ATR" ifadesinin modelden
   modele farklı mesafe anlamına gelmesi demekti. P1 düşerse ilk sınanacak ayrışma budur.
2. **Veri kaynağı.** Repo OKX perpetual (`BTC-USDT-SWAP`); kaynak sistemin grafiği başka
   bir borsa olabilir. Kesişim barları sınırda ayrışır.
3. **Funding.** Repo açık pozisyona funding işletir, kaynak sistem işletmez. Ayrıca
   `data.funding_history_periods` (180 ≈ 60 gün) 4 yıllık pencereyi kapsamaz: kaydı
   olmayan anda `core/funding.py::rate_at` None döner ve maliyet İŞLENMEZ (uydurma yok).
   Bu, dönem A'yı İYİMSER yapar. Harness `--funding-periods` ile derinliği artırır; OKX'in
   kendi sınırı ilk koşuda ölçülür ve raporlanır.
4. **Dolum belirsizliği** (§5e): aynı mumda hem stop hem hedef varsa repo STOP varsayar.
   Bu geometride (stop 1.5×ATR, hedef 3×ATR) payın küçük olması beklenir; AYNI-BAR
   BELİRSİZLİĞİ tablosu bunu sayar.
5. **Portföy kotası.** 13 coinlik koşuda `max_positions: 5` sinyal reddeder; tek-sembollü
   koşularda bağlamaz. İki koşu AYRI raporlanır ve karıştırılmaz.
6. **Stop'un çapası.** `Signal.stop_price` mutlak bir fiyattır ve sinyal barının
   kapanışından kurulur; dolum bir sonraki barın açılışındadır (kural 13). Kaynak sistem
   stop'u gerçekleşen giriş fiyatından kuruyorsa aradaki fark bir bar boşluğu kadardır.

### Çoklu karşılaştırma

Bu, sicilin (**§6c**) **2.** satırıdır ve §6c'deki "araştırmadan çıkan 10 öneri"
paydasına AİT DEĞİLDİR: hipotez dışarıdan geldi, o listeden seçilmedi. BH düzeltmesinde
iki payda ayrı tutulur; aksi hâlde dışarıdan gelen her hipotez, ev içi arama uzayının
cezasını ödemiş gibi görünürdü.

### SONUÇ — koşuldu, kapılar okundu: **BLOKE**

Koşu: `backtest-ema` #35391881083, commit `4c5bfac`, 2026-09-18. Ham çıktı
`docs/data/backtest_ema_trend.json` (site yükü) ve `docs/data/backtest_ema_trend.csv`
(coin tablosu). **Bu bölüm sonuçları KAYDEDER, kuralları değiştirmez** — yukarıdaki
hiçbir eşik, tahmin ya da istisna koşudan sonra dokunulmadı.

**Pencereler ve embargo (ölçüldü, varsayılmadı).** Dönem A `2022-01-01T00:00` →
`2024-12-30T20:00` (sinyal kesimi `2024-06-30`; A'da açılan pozisyonlar kesimden sonra
da yönetildi, kural: dönem ataması GİRİŞ tarihine göre). A'da `ema_trend`in azami tutuş
süresi **129 bar (21.5 gün)** ölçüldü, embargo bu sayıdır ve dönem B ondan sonra başlar:
`2024-07-21T12:00` → `2026-09-18T08:00`.

| Tutuş süresi (`ema_trend`) | medyan | p90 | azami |
|---|---|---|---|
| Dönem A | 7 bar (1.17 gün) | 21 bar (3.5 gün) | **129 bar (21.5 gün)** |
| Dönem B | 6 bar (1.0 gün) | 19.2 bar (3.2 gün) | 65 bar (10.8 gün) |

#### P1 — TradingView paritesi (KAPI): **GEÇTİ**

| Dönem | Referans (TV) | Ölçülen | Sapma | Tolerans ±0.2 |
|---|---|---|---|---|
| A | 1.551 | **1.549** | −0.002 | ✅ |
| B | 1.299 | **1.206** | −0.093 | ✅ |

Harness kaynak sistemi yeniden üretiyor; sayılar yorumlanabilir. Dönem A'daki sapmanın
binde iki olması, TADİLAT-1'in (Wilder ATR) doğru tadilat olduğunun kanıtıdır — koşudan
ÖNCE yapıldı ve gerekçesi o zaman yazıldı.

#### Model sahibinin kapıları: **ÜÇÜ DE GEÇTİ**

| # | Koşul | Ölçülen | Sonuç |
|---|---|---|---|
| K-1 | ≥ 6 coinde dönem B PF > 1.1 | **tam 6**: SOL 2.204, BNB 1.369, DOGE 1.222, BTC 1.206, XRP 1.149, LINK 1.101 | ✅ |
| K-2 | toplam işlem > 300 | **978** | ✅ |
| K-3 | hiçbir coinde DD > %25 | azami **%21.3** (XRP, dönem A) | ✅ |

⚠ **K-1 kıl payı geçti ve bu bir okuma notudur, kural değil:** eşik 6, ölçülen 6 ve
sınırdaki satır (LINK 1.101) eşiğin 0.001 üstünde. Tek bir işlemin yeri değişse kapı
düşerdi. Eşik SONUÇTAN SONRA değiştirilmedi — ne aşağı (kurtarmak için) ne yukarı.

#### Repo kabul kapıları (BİRİNCİL): **İKİ DÖNEMDE DE DÜŞTÜ**

| | Ö (örneklem) | ⚠ B (band) | E (edge) | `passed` |
|---|---|---|---|---|
| Dönem A | ✅ n=369 | ✅ 3.62% ∈ [3.42, 8.56] | ❌ | **false** |
| Dönem B | ✅ n=389 | ✅ 3.56% ∈ [3.36, 8.39] | ❌ | **false** |

Edge kapısının hangi koşulda düştüğü:

| Koşul | Dönem A | Dönem B |
|---|---|---|
| C-1 ortalama R > 0 | **−0.0015** ❌ | **−0.0165** ❌ |
| C-2 kontrolü ≥ 0.15R marjla geç | +1.031 ✅ (kontrol −1.033) | +1.046 ✅ (kontrol −1.063) |
| C-2 farkın bootstrap CI alt sınırı > 0 | +0.887 ✅ | +0.897 ✅ |
| C-3 hesap getirisi çıpayı geç | **−%8.6 ↔ +%43.4** ❌ | **−%20.4 ↔ −%7.3** ❌ |

#### Verdikt: **BLOKE — paper trading'e GEÇİLMEZ**

Ön-kayıttaki tek istisna (**yalnızca** C-3'ten kalma → koşu durur, karar model sahibine
gider) **UYGULANMAZ**: model C-3'ün yanında **C-1'den de** kalıyor (ortalama R iki
dönemde de negatif). İstisnanın kapsamı sonuçtan önce tek bir kapıyla sınırlandı ve
burada genişletilmiyor — "diğer her şeyi geçiyorsa" şartı sağlanmıyor.

`.github/workflows/run-ema.yml` AÇILMAZ; `ema` katmanı tetikleyicisiz kalır.

#### Ön-kayıtlı tahminlerin sonucu (hepsi, düşen dâhil)

| # | Tahmin | Sonuç |
|---|---|---|
| P1 | BTC PF A 1.551±0.2 / B 1.299±0.2 | **TUTTU** (1.549 / 1.206) |
| P2 | `tp` ile kapanan pozisyon payı < %35 | **A'da DÜŞTÜ (%35.23), B'de tuttu (%34.70)** — eşiğin iki tarafında, kıl payı |
| P3 | ortalama R > 0 (C-1) | **DÜŞTÜ** (A −0.0015, B −0.0165) |
| P4 | hesap getirisi çıpanın ALTINDA (C-3 düşer) | **TUTTU** (model sahibinin beklentisi doğrulandı) |

**P2 nasıl ölçüldü:** model tek dilimli hedef kullanır (`fraction=1.0`) ve ne trailing
ne zaman stop'u taşır, yani bir pozisyon ya hedefte ya stop'ta ya da likidasyonda kapanır.
Pay bu yüzden `(pozisyon − stop çıkışı − likidasyon) / pozisyon`tur: A 369'da 239 stop ve
0 likidasyon → **%35.23**; B 389'da 254 stop ve 0 likidasyon → **%34.70**. Tahmin A'da
yüzde binde 23 ile düştü. **Eşik yuvarlanarak kurtarılmıyor** (§7.4): "yaklaşık %35" demek,
sonucu gördükten sonra tahmini gevşetmek olurdu. P2 zaten bir KAPI değil bir tahmindi ve
verdikti değiştirmez — düşüşü buraya, geçtiği yer kadar açık yazılıyor.

**P3 ile P4 birlikte okunur:** model çıpayı yenemedi VE bilgisiz çekilişin üstünde bir
beklenen değer üretmedi. İkincisi C-2'yi geçmesiyle çelişmez — C-2 kontrolün (`random_ctrl`)
ortalama R'sine göre ölçülür ve kontrol −1.03R ile neredeyse her işlemi stop'ta
kapatıyor; onu geçmek sıfırın üstüne çıkmak demek değildir. Modelin kendi eşiği C-1'dir
ve sıfırın altındadır.

#### Ne öğrenildi (bir kural değil, bir kayıt)

Kesişim sinyali maliyet ÖNCESİ bir kenar taşıyor gibi görünüyor: ödeme oranı (payoff)
dönem B'de 1.61 ve kazanma oranı %34.7 — bu çift, 2R hedefli bir sistemde başabaşın tam
sınırıdır. Friksiyon (`cost_per_r` 0.057) işareti negatife çeviren şeydir. Bu bir
GÖZLEMDİR; parametre değiştirmenin, maliyeti yeniden varsaymanın ya da yeni bir eksen
açmanın gerekçesi DEĞİLDİR (§7.1/§7.2/§7.4). Yeni bir tez ancak yeni bir ön-kayıt
satırıyla, yeni bir model olarak gelir.

---

## 6e. ÖN-KAYIT — çıkış varyantı turunun İSTATİSTİKSEL GÜCÜ

**Bu bölüm varyant tanımlarından ÖNCE ve Adım 0b'nin (yol teşhisi) sonucu ortaya
çıkmadan yazıldı; tarih damgası git'tedir.** Sebebi usuldür: aşağıdaki eşikler sonuç
görüldükten sonra yazılsaydı, "sonuç kötü çıktı, eşik yeniden yorumlanıyor" ile ayırt
edilemezdi (§7.4). Varyantların kendisi (tanımlar, parametreler, P tahminleri, sicil
satırı) AYRI bir ön-kayıtla ve yine koşudan önce gelecek; bu bölüm yalnızca o turun
**okunabilirlik koşullarını** sabitler.

### Ölçümün çözünürlüğü — `ema_trend`in R dağılımı İKİ NOKTALIDIR

Model ne trailing ne zaman stop'u taşır ve hedefi tek dilimdir; yani her pozisyon ya
hedefte ya ilk stop'ta kapanır (likidasyon iki dönemde de 0). Dağılım bu yüzden iki
noktalıdır: `%35 → +1.93R`, `%65 → −1.05R`.

| | hesap | değer |
|---|---|---|
| standart sapma | `√(p(1−p)·(1.9296+1.0519)²)` | **1.424** |
| standart hata, A (n=369) | `1.424/√369` | **0.0742** |
| standart hata, B (n=389) | `1.424/√389` | **0.0722** |

Bağımsız sağlaması defterdedir: `core/metrics.py::bootstrap_mean_ci`in ürettiği aralık
(A: −0.147 … +0.143) analitik `±1.96·SE = ±0.145` ile çakışıyor.

**§6d'nin sonucu bu ışıkta şöyle okunur:** A `−0.0015 ± 0.074`, B `−0.0165 ± 0.072`.
İkisi de sıfırdan ayırt edilemez. **Model negatif KANITLANMADI; sıfır kanıtlandı.**
Bu, §6d > SONUÇ'un **BLOKE** verdiktini DEĞİŞTİRMEZ ve o bölümün hiçbir satırına
dokunulmaz: model C-3'ten de kalıyordu, ayrıca kanıtlanmamış bir kenara sermaye
ayrılmaz. Değiştirdiği şey verdikt değil, **bundan sonraki turun ne vaat edebileceğidir.**

### Asgari tespit edilebilir etki (MDE) — turdan ÖNCE yazılır

`n ≈ 400`, `sd ≈ 1.42`, `α = 0.05`, güç `0.80` için:

| test | gereken GERÇEK etki |
|---|---|
| iki yanlı | **+0.202R** |
| tek yanlı | +0.180R |

**Bundan küçük bir iyileşme bu kurulumda görülemez.** Ölçek şudur: brüt kenar A'da
+0.057R, B'de +0.041R; friksiyon 0.057R. Yani gerçekçi bir çıkış iyileştirmesi
(0.02–0.06R mertebesi) MDE'nin **üçte biri ile beşte biri** arasındadır. Bunun iki
dürüst sonucu var ve ikisi de şimdi yazılıyor:

1. **"Ortalama R > 0 çıktı" tek başına bir bulgu değildir.** Gerçekten değersiz bir
   varyantın dönem B'de pozitif çıkma ihtimali ≈ %50 — C-1 bu örneklemde neredeyse
   yazı-turadır.
2. **Turun olumlu çıktısı bir kabul değil, bir SIRALAMA ve bir ELEME olabilir.** Bu tur
   "şu varyant kazandırıyor"u kanıtlayamaz; kanıtlayabileceği şey "şu varyant,
   ölçülebilir bir farkla KÖTÜLEŞTİRMİYOR / kötüleştiriyor"dur.

**Her varyantın MDE'si KENDİ dağılımından yeniden hesaplanır ve sonucunun yanına
yazılır.** İki noktalı dağılım yalnızca `ema_trend`in geometrisine aittir: trailing ya
da hedefsiz bir varyantın sağ kuyruğu şişer, `sd` büyür ve MDE **kötüleşir**. Tek bir
MDE sayısını tüm varyantlara uygulamak, kuyruğu geniş bir varyantı olduğundan
ölçülebilir göstermek olurdu.

### Eksen istatistiği EŞLEŞTİRİLMİŞTİR: pozisyon başına ΔR

Varyantlar `ema_trend`in girişini ve stop'unu MİRAS ALIR (varyant tanımları AYRI bir
ön-kayıtla ve yine koşudan önce gelecek):
aynı sembolde, aynı barda, aynı stop mesafesiyle girilir ve ayrışan tek şey çıkıştır.
İki model bu yüzden **aynı fiyat yolunu** paylaşır ve farkın varyansı, iki bağımsız
ölçümün varyansından çok daha küçüktür.

Eksen istatistiği bu yüzden marjinal ortalama R değil, **eşleşen pozisyonlarda
`ΔR = R_varyant − R_taban`in ortalamasıdır.** Precedent katmanın içindedir: model 15 ↔
`scalp_fixed` çifti aynı gerekçeyle çekiliş kimliğini paylaşır (CLAUDE.md > "Çekiliş,
ölçülmeyen eksende PAYLAŞILIR").

`ρ` (iki modelin R'leri arasındaki korelasyon) **bilinmiyor ve ÖLÇÜLECEK**; ön-kayıtlı
beklenti ve ondan çıkan güç şudur:

| varsayılan ρ | `sd(ΔR) = 1.42·√(2(1−ρ))` | MDE (n=369, iki yanlı, güç 0.80) |
|---|---|---|
| 0.7 | 1.10 | +0.160R |
| **0.8 (ön-kayıtlı beklenti)** | **0.90** | **+0.131R** |
| 0.9 | 0.64 | +0.093R |

Yani eşleştirme MDE'yi +0.20R'den **~+0.13R**'ye indirir. Hâlâ beklenen etkinin
(0.02–0.06R) üstündedir — eşleştirme güç sorununu çözmez, **küçültür**; bu da sonucun
"ayırt edilemedi" ile bitme ihtimalini düşürür.

Üç şart, sonucu görmeden:

1. **`ρ` ve gerçekleşen `sd(ΔR)` sonucun yanına YAZILIR.** Yukarıdaki tablo bir beklenti;
   MDE gerçekleşen `sd`den yeniden hesaplanır. Beklentiyi tutmuş gibi raporlamak, gücü
   olduğundan iyi göstermek olurdu.
2. **`n` eşleşen pozisyonların sayısıdır, iki defterin toplamı değil.** Dolumlar
   ayrışır (çıkış kuralı kotayı ve nakdi farklı zamanlarda serbest bırakır — CLAUDE.md
   aynı ayrışmayı model 15 için yazar), yani eşleşme kesişimdir ve kesişimin dışında
   kalan pozisyon sayısı da raporlanır.
3. **C-1 BAĞLAYICI KALIR ve eşleştirme onun YERİNE GEÇMEZ.** Varyant tabandan
   ölçülebilir biçimde iyi olup hâlâ negatif olabilir; o durumda bulunan şey "daha az
   kötü"dür ve canlıya alınacak bir şey değildir. İkisi birlikte okunur: ΔR ekseni
   *"çıkış kuralı bir fark yaratıyor mu"*, C-1 *"bu model para kazanıyor mu"* sorusunu
   cevaplar.

`ΔR`nin aralığı **eşleştirilmiş bootstrap** ile kurulur (pozisyon çiftleri birlikte
yeniden örneklenir; bağımsız yeniden örnekleme eşleştirmenin kazancını geri verirdi) ve
hesap `core/metrics.py`ye girer — ikinci bir R tanımı, aynı defterin iki cevabı demekti
(kural 7).

### Kabul kuralı — C-1'in yanına bir KESİNLİK koşulu

C-1 (`ortalama R > 0`) bu örneklemde tek başına yetersizdir (yukarıdaki 1. madde). Bu
yüzden varyant turunda C-1, modelin KENDİ ortalama R'sinin güven aralığıyla birlikte
okunur: **aralığın ALT SINIRI > 0.** Böylece "pozitif çıktı" ile "pozitif olduğu
gösterildi" ayrışır — kabul çıtasının edge kapısında (E) kontrol farkı için zaten
uygulanan ayrımın aynısı (CLAUDE.md > Kabul Çıtası).

Aralık `core/metrics.py::bootstrap_mean_ci` ile kurulur ve tabloda zaten vardır
(`avg_r_ci_low` / `avg_r_ci_high`); alfa `acceptance.edge_ci_alpha`tan (0.05) gelir.
**İKİNCİ bir alfa anahtarı açılmaz** (CLAUDE.md > Rapor Kolonları): aynı tabloda iki
farklı kesinlik ölçüsü durması, hangi satırın hangi ölçüyle okunacağını belirsiz
bırakırdı — ve "hangi tabloya bakıyorduk" sorusunun cevabı bir gün belirsizleşirse
kesinlik ölçüsünün kendisi işe yaramaz hâle gelir. Hazır kolon kullanılmasının ikinci
kazancı yeni kod yazılmamasıdır: yeni kod yeni hata demektir.

⚠ **Bu seçim SONUÇ GÖRÜLMEDEN ve DAHA SIKI olduğu BİLİNEREK yapıldı.** `edge_ci_alpha`
iki yanlı %95'tir, yani alt sınırı tek yanlı α=0.025'e denk gelir; tartışılan alternatif
(iki yanlı %90, yani tek yanlı α=0.05) daha gevşek olurdu. Kayıt buraya, seçim anına
yazılıyor ki ileride bir varyant kıl payı kalırsa "keşke %90 deseydik" tartışması
açılmasın — o tartışma, eşiği sonuca göre yeniden yorumlamanın kendisidir (§7.4).

### Çoklu karşılaştırma — tek BİRİNCİL varyant

Birden fazla varyant sınanacaksa, birinin şansla geçme ihtimali tek varyanttan
yüksektir. Bu yüzden:

- **Koşudan önce TEK bir varyant BİRİNCİL ilan edilir**; kalanlar raporlanır ama kabul
  çıtasına aday değildir. "Beşinin arasından B'de en iyisini seçmek", B'yi OOS olmaktan
  çıkarırdı.
- Birincil varyantın tahmini §6c sicilinin **3.** satırıdır ve BH düzeltmesine
  (`q = 0.10`) girer. Satır, varyant tanımlarıyla birlikte ve koşudan ÖNCE açılır;
  sonucu ne olursa olsun orada kalır (§7.5).
- Birincil varyantın seçim KURALI da koşudan önce yazılır: Adım 0b'nin hangi çıktısının
  hangi varyanta işaret ettiği, teşhis sonucunu GÖRMEDEN sabitlenir. Aksi hâlde
  "baktım, en iyi görüneni seçtim" olurdu.

### BİRİNCİL VARYANTIN SEÇİM KURALI — teşhis çıktısı GÖRÜLMEDEN sabitlendi

Kuralın **çalıştırılabilir kopyası** `scripts/diagnose_ema_exits.py::select_primary_family`
fonksiyonudur ve eşikler orada sabittir; bu bölüm onu ALINTILAR. Gerekçe: kuralın tek işi
"sonucu görüp seçmedik"i kanıtlamaktır, iki yerde yazılı bir kural ise bir gün sessizce
ayrışır ve o kanıtı yok eder. Seçim bir metin değil bir fonksiyondur, teşhisin İÇİNDE
çalışır ve çıktısı log'a basılır.

Dallar **öncelik sırasıyla** denenir:

| sıra | dal | ölçü (dönem A teşhisi) | eşik | seçilen aile |
|---|---|---|---|---|
| 1 | **M2** — kuyruk | `tp` çıkışlarından 20 bar sonraki **İŞARETLİ** hareketin medyanı | ≥ **+0.25R** | kazananı koşturan (trailing / hedefin kaldırılması) |
| 2 | **M1** — geri dönüş | `stop` çıkışlarının medyan MFE'si (kapanış barı hariç) | ≥ **1.0R** | breakeven + kısmi çıkış |
| 3 | **M4** — oyalanma | medyan `stop` tutuşu / medyan `tp` tutuşu | ≥ **2.0×** | zaman stop'u |
| 4 | — | hiçbiri tetiklenmedi | — | **tur KAPANIR, varyant kurulmaz** |

**Eşiklerin gerekçesi (hiçbiri veriden türetilmedi):**

- **M1 = 1.0R** keyfi DEĞİLDİR: `config.yaml > exit_management.breakeven_at_r` değerinin
  ta kendisidir ve bir breakeven kuralının tetiklenebilmesi için gereken hareket odur.
- **M2 = +0.25R** ölçülen friksiyonun (0.057R) kabaca dört katıdır. **YUVARLAK bir
  sayıdır ve öyle seçildiği burada yazılıdır** — sonradan eşik tartışması açılmasın.
- **M2'nin ufku = 20 bar**, dönem A'nın p90 tutuş süresinin (21 bar) yuvarlanmış hâlidir.
  O sayı §6d'de koşudan önce yayımlandı; teşhis çıktısından gelmiyor.
- **M4 = 2.0×** yuvarlak bir katsayıdır ve açıkça yuvarlak seçilmiştir.

**M2 neden İŞARETLİ hareketi ölçer, azami yükselişi değil.** "Çıkıştan sonra ne kadar
yükseldi" (azami) tanım gereği ≥ 0'dır ve sürüklenmesiz bir yürüyüşte bile ufukla
birlikte `√H` hızında büyür — 20 barda ~2.4R. Onu eşiğe bağlamak, M2 dalını HER koşulda
tetiklemek olurdu. İşaretli kapanış farkının medyanı ise martingal altında sıfırdır,
yani sıfırdan sapması gerçek bir sürüklenmedir. Teşhis ikisini de basar; kural yalnızca
ikincisine bakar.

**Sıranın gerekçesi.** M2 ile M1 aynı pozisyonlar üzerinde TERS yönde çalışır (biri
kuyruğu uzatır, öteki keser); ikisi birden tetiklendiğinde hangisinin seçileceği
önceden yazılmazsa, "hangisi daha mantıklı" tartışması kuralın kapatmak için var olduğu
serbestliği geri açardı. M4 en sona konur çünkü ölçtüğü şey R değil SERMAYE HIZIDIR ve
C-1 bir R kapısıdır.

**4. dal bir boşluk değil, bir SONUÇTUR.** Hiçbir dal tetiklenmezse yolda çıkışın
sömürebileceği bir yapı yok demektir: varyant turu kurulmaz, sicile bu yazılır ve açık
kalan tek kaldıraç friksiyondur — o da çıkış ekseninde değil, aşağıdaki KAYIT'ta duran
stop mesafesi ekseninde.

**Kural BİR KEZ çalışır.** Teşhis geldiğinde uygulanır, birincil aile belirlenir ve
çıkan aile beklenen olmasa bile tartışılmaz. `nan` bir dalı tetiklemez: ölçülemeyen bir
koşul sağlanmış sayılamaz (eksik bir çıta, geçilmiş bir çıta gibi görünmemelidir — §4'ün
aynı ilkesi).

### KAYIT (bir varyant önerisi DEĞİL) — friksiyon stop mesafesine bağlıdır, hedefe değil

`cost_per_r ≈ 2c / stop%` özdeşliği, R başına friksiyonun **stop mesafesiyle** ters
orantılı olduğunu söyler: 1.5×ATR'den 3.0×ATR'ye geçmek onu kabaca YARIYA indirir
(ölçülen 0.057R → ~0.028R). Hedefin yeri bu sayıya girmez.

**Bu, çıkış varyantı turunun konusu DEĞİLDİR ve o turda denenmeyecektir:** stop
mesafesi ayrı bir geometri eksenidir ve çıkış ekseniyle birlikte oynatılırsa aradaki
fark iki değişkenin toplamı olur (CLAUDE.md > Scalp katmanının model kuralları'nın
"13 ↔ 14 bir eksen değil, bir toplam farktır" gerekçesi). Kaynak sistemde SL 2.5
denemesi çökmüştü, ama orada hedef de stop'la birlikte kaymıştı — **stop ile hedefin
AYRIŞTIRILMASI hiç test edilmedi.** Sırası gelirse kendi ön-kaydıyla gelir.

---

### SONUÇ — kural koşuldu, TUR KAPANDI (varyant kurulmadı)

Koşu: `diagnose-ema-exits` #35435506689, commit `d0332da`, 2026-09-19. Ham çıktı
artifact'te (`diagnose-ema-exits`, 14 gün); özet yük koşunun log'una basıldı.
**Bu bölüm sonucu KAYDEDER, kuralları değiştirmez** — yukarıdaki hiçbir eşik, öncelik
sırası ya da dal tanımı koşudan sonra dokunulmadı.

**Determinizm kapısı GEÇTİ.** Teşhis koşusu, karara giren koşunun (`backtest-ema`
#35391881083) dönem A sayılarını birebir üretti: 369 pozisyon, 130 tp, 239 stop,
ortalama R −0.0014889765, azami tutuş 129 bar. `missing_bars = 0`,
`unchecked_position_bars = 0`. Yani yol istatistiği okunabilir.

**Kuralın uygulanması (bir kez çalıştı):**

| dal | aile | ölçülen | eşik | sonuç |
|---|---|---|---|---|
| M2 | kuyruk (trailing / hedefsiz) | **+0.153R** | ≥ +0.25R | tetiklemedi |
| M1 | geri dönüş (breakeven + kısmi) | **0.552R** | ≥ 1.0R | tetiklemedi |
| M4 | oyalanma (zaman stop'u) | **0.667×** | ≥ 2.0× | tetiklemedi |

**SEÇİLEN: 4. dal — tur kapanır, varyant kurulmaz.**

#### Ölçülen yol (kayıt)

| çıkış | n | tutuş: medyan / p75 / p90 / azami (bar) | MFE\* medyan | MAE\* medyan |
|---|---|---|---|---|
| `tp` | 130 | 9 / 15.8 / 22 / 129 | +1.62R | −0.41R |
| `stop` | 239 | 6 / 12 / 20 / 43 | +0.55R | −0.77R |

\* kapanış barı HARİÇ (kural 13b): bir çıkış kuralının karar anında görebileceği hareket.

Stop'la kapananların MFE dağılımı (kapanış barı hariç, n=222 — kapanıştan önce barı
olan pozisyonlar): `%46.8` hiç +0.5R'ye ulaşamadı, `%24.3` 0.5–1.0R, `%16.7` 1.0–1.5R,
`%12.2` 1.5–2.0R. Hedefe varanların MAE'si: `%34.7` −0.25R'den derine hiç inmedi,
`%17.7` −0.75R'nin altına indi.

TP sonrası İŞARETLİ hareketin medyanı ufuklar arasında işaret değiştiriyor:
5 bar −0.10R, 10 bar −0.23R, **20 bar +0.15R**, 40 bar −0.37R. Kural yalnızca 20 barlık
ufka bakar (o ufuk koşudan önce sabitlenmişti) ve orada da eşiğin altında kalıyor;
diğer üç ufkun negatif olması, +0.15R'nin kararlı bir sürüklenme DEĞİL gürültü
olduğunu söylüyor — martingal ile uyumlu. Azami YÜKSELİŞ ölçüsü (20 barda medyan
+1.46R) bu tabloda bir bulgu değildir: sürüklenmesiz bir yürüyüşte de aynı mertebede
çıkar ve kural bu yüzden ona bakmıyor.

#### Ne öğrenildi (bir kural değil, bir kayıt)

- **Zaman stop'unun ön kabulü TERSİNE çıktı.** Kaybedenler kazananlardan DAHA HIZLI
  ölüyor (medyan 6 ↔ 9 bar, p90 20 ↔ 22 bar). Bir zaman stop'u bu dağılımda önce
  kazananları keserdi; "oyalanan kaybedenleri kes" tezinin dayanağı bu veride yok.
- **Kaybedenlerin yarısı hiç kâra geçmiyor:** medyan MFE +0.55R ve `%46.8`'i +0.5R'yi
  bile görmüyor. Breakeven'ın koruyacağı bir kâr çoğu kayıpta hiç oluşmamış.
- **Hedefte kesilen kuyruk ölçülebilir değil:** çıkış sonrası işaretli hareket sıfır
  etrafında salınıyor. "TP'ler erken kesiyor" iddiası bu pencerede desteklenmiyor.
- Üçü birlikte, §6e'nin 4. dalının tanımladığı durumdur: **yolda çıkışın sömürebileceği
  bir yapı yok.** Kenar sinyalin kendisinde ve küçük (brüt +0.057R ↔ friksiyon 0.057R);
  onu çıkış geometrisini oynatarak büyütmenin bu veride bir dayanağı yok.

Açık kalan tek kaldıraç friksiyondur ve o, çıkış ekseninde değil **stop mesafesi**
ekseninde durur (yukarıdaki KAYIT). O eksen kendi ön-kaydıyla gelir; bu tur onu
denemedi ve denemeyecek.

---

## 6f. ÖN-KAYIT — dönem A fonlama arşivi (karar 50'nin açık işi)

**Bu bir TEZİN ön-kaydı değil, bir VERİ YOLUNUN ön-kaydıdır.** Sabitlenen şey bir modelin
performansı hakkında bir tahmin değil, *"dönem A'nın fonlama dağılımı hangi veriyle
sorulacak"* sorusunun cevabıdır. Bu yüzden §6c'nin sicil tablosuna **girmez** ve BH
paydasına eklenmez: sicil bir modelin performansı hakkında iddia taşıyan satırları sayar
ve bu satırın öyle bir iddiası yoktur (§6c'nin "ölçüm katmanı hipotez DEĞİLDİR" ayrımı).
Arşivle beslenen bir TEZ geldiğinde o tez kendi satırını açar.

**Neden şimdi yazılıyor.** Karar 50 arşiv işinin sırasını bağlayıcı kıldı (önce arşiv,
sonra örtüşme kanıtı, ancak sonra kullanım) ve "engelin tam mekanizması … girmesi gereken
yer arşiv işinin ön-kaydıdır" dedi. Burası o yer. Kapılar veri GÖRÜLMEDEN sabitleniyor;
tersi §7.1'in yasakladığı şeyin ta kendisidir. Belge, `scripts/probe_funding_depth.py`
hiç koşmadan commit edilir — probe'un ürettiği hiçbir sayı bu sayfadaki hiçbir eşiği
seçmedi.

### Adımlar ve sıra (bağlayıcı)

| Adım | Soru | Nerede cevaplanır |
|---|---|---|
| **A** | OKX'in KENDİ yolu dönem A'ya ulaşıyor mu? | `scripts/probe_funding_depth.py` log'u |
| **B** | Ulaşmıyorsa hangi arşiv? | aday sırası aşağıda SABİT |
| **C** | Arşiv canlı seriyle tutarlı mı? | 60 günlük örtüşmede üç koşul |

**A geçerse B ve C DÜŞER.** OKX-içi bir yol bir kaynak değişikliği değildir: kural 5'in
"tüm modeller aynı veriyi görür" şartı zaten sağlanır ve karar 50'nin örtüşme kanıtı
konusuz kalır. Bu yüzden probe'un sırası da A-2 (OKX'in tarihsel veri portalı) ile başlar:
portal erişilebilir ve fonlama veri kümesi taşıyorsa aşağıdaki (a)/(b) ayrımı bile
gereksizdir.

### Adım A — iki hipotez, tek ayrım

PR #37'nin izi `BTC-USDT-SWAP` için **4 sayfa / 312 kayıt / en eski 2026-06-11** verdi ve
"kısa sayfa" (12 < 100) ile durdu. Kısa sayfa İKİ ayrı şeyin imzasıdır ve iz ikisini
ayırt etmiyor:

- **(a) borsa tabanı** — uç nokta bu yoldan gerçekten ~3-4 ay veriyor;
- **(b) yürüyüş tabanı** — `fetch_history`nin `after`-yürüyüşü sunucu tarafı bir pencereyi
  tüketiyor ve doğrudan parametrelenmiş bir istek daha geriye ulaşıyor.

312 kayıt, bir ikincil kaynağın bildirdiği 400 kayıtlık tavanın **altında** durdu; bu (b)
lehine bir işarettir ama kanıt değildir — kanıt tek bir istektir.

**Ayrım MEKANİKTİR ve kodda yazılıdır** (`probe_funding_depth.py::classify_depth_floor`).
Gerekçe `diagnose_ema_exits.py::select_primary_family`in aynısıdır: sonucu bir insanın
okuyup "bana (b) gibi göründü" demesi, kuralın kapatmak için var olduğu serbestliği geri
açardı. Kural, probe koşmadan önce şudur:

1. Yürüyüşün ulaştığı en eski damgadan **doğrudan** bir `after` isteği daha eski kayıt
   döndürüyorsa → **(b) yürüyüş tabanı.**
2. Döndürmüyorsa, dönem A kesimine (2024-06-30) **doğrudan** atlayan bir `after` isteği
   kayıt döndürüyorsa → yine **(b).**
3. İkisi de boşsa → **(a) borsa tabanı.**
4. Herhangi bir istek hata verirse → **belirsiz** (probe hata koduyla biter; belirsiz bir
   sonuç "(a) çıktı" diye okunamaz).

### SONUÇ — Adım A KOŞULDU: (a) borsa tabanı

Koşu: `measure-funding` #35463452072, `main` @ `b542954`, 2026-09-19 19:09 UTC.
Semboller: `BTC-USDT-SWAP`, `SUI-USDT-SWAP` (biri en derin kitap, öteki dönem A'yı
KISMEN kapsayan bir listeleme — ikisi farklı şey sınıyor).

| Ölçüm | BTC-USDT-SWAP | SUI-USDT-SWAP |
|---|---|---|
| `limit=100` | 100 kayıt | 100 kayıt |
| `limit=200` | **200 kayıt** | **200 kayıt** |
| `limit=300` | 283 kayıt | 283 kayıt |
| `limit=400` | 283 kayıt | 284 kayıt |
| yürüyüş (`fetch_history`) | 283 kayıt, en eski 2026-06-17 16:00 | 283 kayıt, en eski 2026-06-17 16:00 |
| `after=<yürüyüş tabanı>` | **0 kayıt** | **0 kayıt** |
| `after=2024-06-30` | **0 kayıt** | **0 kayıt** |
| `before=2024-06-30` | 100 kayıt, 2026-06-17 → 2026-07-20 | aynı |

**Mekanik ayrım (`classify_depth_floor`): (a) BORSA TABANI, iki sembolde de.** Üç
bağımsız kanıt aynı yeri gösteriyor: (1) `limit=300` ile `limit=400` aynı 283 kaydı
veriyor, yani sayfa boyu bağlamıyor; (2) yürüyüş de tam olarak aynı 283'e ulaşıyor;
(3) tabanın altını isteyen iki `after` isteği de boş dönüyor. `before=2024-06-30` ise
pencerenin EN ESKİ ucundan başlıyor (2026-06-17) — 2024'e bakan bir istek bile tabanın
altına inemiyor. Uç nokta **~3 aylık KAYAN bir pencere** tutuyor ve dönem A'nın (2022-01
→ 2024-06) tamamı o pencerenin ~26 ay dışında.

**⚠ DÜZELTME — "400 kayıt tavanı" hipotezi YANLIŞTI.** Bu bölüm ilk yazıldığında
"312 kayıt, bir ikincil kaynağın bildirdiği 400 kayıtlık tavanın ALTINDA durdu; bu (b)
lehine bir işarettir" diyordu. Probe onu çürüttü: `limit=400` KABUL EDİLİYOR ama ortada
yalnızca 283 kayıt var — yani 400 bir kayıt tavanı değil, **kısıt ~3 aylık kayan bir
penceredir.** PR #37'nin 312 kaydı da bir tavanın altında durmuş değildi; o gün pencere
o kadardı. İki koşunun karşılaştırması bunu doğruluyor: #37'de en eski damga 2026-06-11,
bugün 2026-06-17 — pencere İLERİ kaydı. SUI'de taban tek koşunun içinde bile bir damga
oynadı (`limit=300` → 16:00, `limit=400` → 08:00), yani bu bir arşiv değil canlı bir
penceredir. **Yanlış hipotezin çürütülüşü sonucun kendisi kadar kayda değerdir:** yazılı
durmazsa ileride biri aynı izi yeniden sürer ve probe boşuna tekrar koşturulur.

**`fetch_history` KAYIP VERMİYOR.** Yürüyüş (`limit=100`, 3 sayfa) tek istekle
ulaşılabilen azami derinliğin (283) TAMAMINI alıyor. Kodda onarılacak bir şey yok —
veri orada değil. `limit>100`ün kabul edilmesi yalnızca istek SAYISINI düşürürdü (3 → 2)
ve derinliği değiştirmez; bu yüzden canlı yolun `--request-limit` varsayılanı
DEĞİŞTİRİLMEDİ (kural 5'in okuduğu seriye dokunmayan bir kazanç, dokunma riskini
karşılamıyor).

**Alternatif uç nokta adı YOK:** `/api/v5/public/history-funding-rate` HTTP 404.
İkincil kaynağın verdiği ad gerçek değil; `/api/v5/public/funding-rate-history` tek yol.

**A-2 — probe'un ölçtüğü (2026-09-19):** portal
(`https://www.okx.com/en-us/historical-data`) HTTP 200 döndü ve HTML'inde hem `funding`
hem `2022` geçiyor — ama bu **hiçbir şey kanıtlamıyor** ve probe da öyle raporladı:
59 KB'lık bir sayfa, veri kümesi listesi büyük olasılıkla JS ile yükleniyor, o iki kelime
menü/altbilgi metninden gelebilir. Bir YOKLUK KANITI da değildi. Probe A-2'yi **açık**
bıraktı ve elle kapatılmasını istedi.

**A-2 KAPANDI (2026-09-20): OKX-İÇİ BİR YOL YOK.** Portalın sunduğu olarak anılan veri
kümeleri **tick bazlı işlem verisi ve OHLCV mum verisi**; fonlama oranı geçmişi
listelenmiyor ve fonlama için aynı kaynaklar **REST API'yi** işaret ediyor — yani
probe'un tabanına çarptığı uç noktanın ta kendisi. İki bağımsız yol aynı yere çıkıyor:
ölçüm uç noktanın ~3 ayda bittiğini gösterdi, kaynak taraması da portalın o boşluğu
dolduracak bir veri kümesi sunmadığını.

⚠ **Kanıt SINIFI yazılı olsun:** bu bir **ikincil kaynak taraması**, portalın indirme
listesinin doğrudan görüntüsü DEĞİL. Aynı sınıf kanıt bu belgede bir kez çürüdü (yukarıdaki
"400 kayıt tavanı"). Aradaki fark kaydedilmeye değer: orada tek bir ikincil iddia bir
ÖLÇÜMLE çelişiyordu, burada ikincil kaynaklar ile doğrudan ölçüm **aynı yöne** işaret
ediyor. Yine de portalın indirme listesine doğrudan bakan bir gözlem bunu tersine
çevirebilir; o gözlem gelirse **A-2 yeniden açılır** ve Adım B düşer.

**ADIM A KAPANDI → ADIM B AÇILDI (2026-09-20).** A-1 (a) borsa tabanı, A-2'de OKX-içi yol
yok. §6f'nin SABİT aday sırası devreye giriyor: **Bybit.** Bunun iki sonucu var ve ikisi de
şimdiden yazılı:

1. **C kapısı üç koşullu biçimiyle koşacak** (C-a/b/c), karar 50'nin literal "aynı oran"
   şartıyla değil — çünkü aday başka bir borsadır ve orada o şart sağlanamadığı için değil
   ANLAMSIZ olduğu için geçersizdir (yukarısı).
2. **Cross-venue muafiyeti artık teorik değil, KULLANILACAK.** Bedeli iki yere yazılır:
   §8'in "iddia edilmeyecekler" satırı ZATEN yazılı (bu bölümle aynı commit dizisinde);
   §5'in kabul edilen sapmalar listesine girecek satır ise **arşiv gerçekten kullanıldığında**
   eklenir — bugün henüz kullanılmıyor, yalnızca adayı seçilmiş durumda.

**Adım B BAŞLAMADI, yalnızca AÇILDI:** aday belli, arşiv kurulmadı. Arşivin kurulması ayrı
bir iştir ve kurulduktan sonra bile **C kapısı geçilmeden hiçbir yerde kullanılamaz** —
ne bir dağılım raporunda, ne bir eşik seçiminde, ne bir backtest'te (karar 50'nin sırası).

### Adım B — aday sırası, SONUÇTAN ÖNCE sabitlendi *(AÇILDI: 2026-09-20)*

1. **Bybit** — `/v5/market/funding/history`
2. **Binance** — `data.binance.vision` aylık `fundingRate` dökümleri (sembol eşleme gerekir:
   `BTCUSDT` ↔ `BTC-USDT-SWAP`)
3. **Ücretli kaynaklar** (Tardis.dev, CoinAPI, CryptoHFTData) — **kapsam dışı**

**Bybit'in önceliği kapsamadan DEĞİL, venue tercihinden gelir:** hesabın tutulduğu yer
zaten Bybit'tir — `fee_rate` Bybit'in taker oranıdır ve `scripts/measure_slippage.py`
kitabı Bybit'ten okur. Sıranın tek gerekçesi budur ve **bağlayıcıdır: Binance daha iyi
kapsama verse bile Bybit seçilir.** Kasıtlı; aksi hâlde arşiv kapsamaya bakılarak seçilir
ve kapsama sonuçla korelasyonlu olabilir (§7.2'nin "post-hoc filtre yok" kuralının kaynak
seçimine uygulanmış hâli).

Bybit C kapısında düşerse sıradaki aday Binance'tir — ama bu bir SEÇİM değil bir
DÜŞMEDİR: Bybit'in hangi koşulda düştüğü yazılır ve Binance kendi C kapısından baştan
geçmek zorundadır.

### Adım C — örtüşme kapısı (veri görülmeden sabitlendi)

**Pencere:** arşiv ile canlı serinin kesiştiği **son 60 gün** (`data.funding_history_periods`
= 180 periyot, karar 50). **Evren:** `ema` katmanının 13 sembolü — üçü de bu pencerede
mevcuttur (aşağıdaki listeleme notu C'yi etkilemez). **Üç koşul, ÜÇÜ BİRDEN:**

| Kapı | Ölçü | Geçme koşulu |
|---|---|---|
| **C-a** | damga hizası | iki serinin damga kümeleri **≥ %95** örtüşür (Jaccard, damga bazında) |
| **C-b** | olay kümesi | p95 eşiğinin ürettiği olay kümelerinin **Jaccard ≥ 0.70** |
| **C-c** | işaret uyumu | ortak damgaların **≥ %95**'inde oranın İŞARETİ aynı |

Üç sayı da bu commit'te sabittir ve koşudan sonra §7.1/§7.4 gereği değiştirilmez.

**C-b'nin eşiği STATİKTİR, kayan değil:** ölçümün kendi kuralı 270 periyotluk kayan
pencere + `shift(1)`tir ve o pencere 60 güne SIĞMAZ. C-b'de p95, örtüşme penceresinin
kendi dağılımından hesaplanır. Bu bilinçli bir sapmadır, yalnızca C kapısında geçerlidir
ve dağılım raporunun kuralını DEĞİŞTİRMEZ.

**C-b'nin örneklemi küçüktür ve bu yazılı olsun:** 60 gün = sembol başına ~180 damga, p95
≈ 9 olay, 13 sembolde ≈ 117 olay. `Jaccard ≥ 0.70`, iki kümenin her birinin ~%82'sinin
ortak olması demektir — yani ≈117 olayda her iki tarafta ~21 olayın kayması tolere edilir.
Kapı bu çözünürlükte okunmalıdır: geçmesi "seriler aynı" demek değil, "olay kümeleri bu
örneklemde ayırt edilemiyor" demektir.

⚠ **C, karar 50'nin LİTERAL şartından daha zayıftır ve bu sessizce geçilmiyor.** Karar 50
"aynı zaman damgası, aynı oran" dedi. O şart **aynı borsanın** arşivi için doğrudur ve
adım A geçerse aynen uygulanır — C-a/b/c'nin yerine birebir eşitlik aranır. Ama B'nin
adayları BAŞKA borsalardır ve başka bir borsanın fonlama oranı tanım gereği başka bir
sayıdır: "aynı oran" orada sağlanamadığı için değil, **anlamsız olduğu için** geçersizdir.
Cross-venue bir arşivde ölçülebilen şey "aynı veri" değil **"aynı olgu"**dur ve yukarıdaki
üç koşul tam olarak onu ölçer.

**Bedeli de yazılı:** cross-venue bir arşivle seçilen bir eşik, OKX mumlarıyla koşacak bir
backtest'e uygulandığında bir **cross-venue varsayımı** taşır — §5c'nin "maliyet modeli
varsayımdır, gözlem değil" maddesiyle aynı statü. Arşiv kullanılırsa bu sapma §5'in kabul
edilen sapmalar listesine girer ve §8'in "iddia edilmeyecekler" listesine bir satır ekler:
*eşiğin OKX'te de aynı olayları seçtiği gösterilmedi, yalnızca ayırt edilemediği.*

### Örneklem bütçesi — n hesabı 13 DEĞİL ~10.6 sembol üzerinden

Dönem A (2022-01-01 → 2024-06-30, 912 gün) boyunca `ema` katmanının 13 sembolünün üçü
pencereyi tam göremez. Bu bir arşiv kusuru değil bir **listeleme tarihidir** ve
`measure_funding.py::coverage` ayrımı zaten yapar (beklenen sayı sembolün KENDİ ilk
kaydından sayılır):

| Sembol | Listeleme (ikincil kaynak; probe'un kapsam tablosu DOĞRULAYACAK) | Dönem A payı |
|---|---|---|
| PENGU | perp 2024-12-18 | **0.00** — dönem A 2024-06-30'da biter |
| ETHFI | 2024-03-18 | ~0.11 (≈3.4 ay / 30) |
| SUI | 2023-05 | ~0.47 (≈14 ay / 30) |
| diğer 10 | 2022-01-01 öncesi (VARSAYIM, doğrulanacak) | 1.00 |

**Etkin sembol sayısı ≈ 10 + 0.47 + 0.11 + 0.00 = 10.58.** Beklenen damga sayısı
`912 × 3 × 10.58 ≈ 29.000` — 13 sembol varsayımının (`13 × 912 × 3 ≈ 35.600`) **%81'i.**

**ÖN-KAYIT: bu tezle ilgili her n hesabı, güç hesabı ve "kaç olay beklenir" beklentisi
10.6 sembol üzerinden kurulur.** 13 üzerinden kurulmuş bir beklenti olay sayısını ~%20
fazla sayar; fonlama ekstremleri zaten seyrekse bu fark tezi ölçülebilir taraftan
ölçülemez tarafa itebilir — ve `acceptance.min_trades` (30) kapısına ulaşıp ulaşılamadığı
tam olarak bu sayıya bağlıdır. Sayı koşudan SONRA "aslında 13'tü" diye düzeltilemez
(§7.1).

**Bu üç satır C kapısını ETKİLEMEZ** (örtüşme penceresi son 60 gündür, orada üçü de
vardır). Etkilediği şey dönem A'nın TEZİDİR. Kapsama tablosunda bu üç sembolün düşük
`completeness` değeri **arşiv kusuru olarak okunamaz**; hangi arşiv gelirse gelsin dönem
A'da 13/13 mümkün değildir.

### Ölçüm adımının varsayılanı KAPALI — gerekçesi burada durur

`measure-funding.yml`in dağılım adımı (`run_measure`) **varsayılan olarak `false`**;
probe (`run_probe`) varsayılan olarak `true`. Gerekçe bu bölümün tamamıdır: bugünkü veri
yolu 60 gün veriyor ve dönem A 30 ay — ölçümü elle açmadan koşturmak, 30 aylık bir soruya
60 günlük bir pencereyle cevap arayıp neredeyse boş bir rapor üretmek demektir. Boş bir
rapor zararsız değildir: "ölçtük, olay yok" ile "ölçemedik" aynı log'a benzer ve karar
50'nin tam olarak ayırmak istediği şey budur (engellenmiş tez ↔ düşmüş tez).

**Bu bir kapı DEĞİL bir varsayılandır:** kimse engellenmiyor, tek kutu işaretlenerek
koşuluyor. Arşiv bu bölümün kapılarını geçtiğinde varsayılan `true`ya döner ve o
değişikliğin gerekçesi de buraya yazılır — "bu neden kapalıydı" sorusunun cevabı
workflow'un git geçmişinde değil, ön-kayıtta durmalı.

### Bu ön-kayıt neyi SEÇMİYOR

**Eşiği.** Hangi persentil, hangi yön (pozitif/negatif kuyruk), hangi kümeleme kuralı bir
olay sayar — hiçbiri burada yok, çünkü hiçbiri veri görülmeden seçilemez ve
`measure_funding.py`nin raporu da onları önermez (§7). Eşik, arşiv C kapısını geçtikten ve
dağılım GÖRÜLDÜKTEN sonra **ayrı bir ön-kayıtla** seçilir ve o ön-kayıt §6c'nin siciline
kendi satırını açar. Bu belge yalnızca **hangi veriyle** sorulacağını sabitler, **ne
sorulacağını** değil.

---

## 7. Sonucu gördükten sonra YAPILMAYACAKLAR

Bu liste bağlayıcıdır. İhlal edilirse backtest bir ölçüm olmaktan çıkar.

1. **Parametre değiştirip yeniden koşmak yok.** Değiştirirsek bu YENİ bir hipotezdir ve
   TAZE bir OOS penceresi gerektirir; eski pencerede "artık geçiyor" demek geçersizdir.
2. **Post-hoc filtre yok** — "şu sembolü çıkarsak", "şu saati elesek", "şu ayı atlasak".
3. **Pencereyi sonuca göre kaydırmak yok.** Pencere §6'nın kurallarıyla, sonuç görülmeden
   sabitlenir.
4. **Metrik değiştirmek yok.** Ortalama R kötü çıkıp "ama Sharpe iyi" demek yasaktır;
   birincil metrik §2'de sabittir.
5. **Çoklu karşılaştırma açıkça raporlanır VE sicile yazılır** (§6c). Kaç model test
   edildi ve kaçı geçti, sonucun yanında yazılır; hipotez sicile koşudan ÖNCE girer ve
   **sonucu ne olursa olsun orada kalır.** 6 modelden 1'inin kıl payı geçmesi gürültüdür,
   bulgu değil — ~%26 olasılıkla şansa bağlıdır. Düşen satırı sicilden silmek, paydayı
   küçültüp kalanları olduğundan anlamlı göstermek olurdu.

---

## 8. Backtest'in cevaplayamayacağı sorular

Bunlar eksiklik değil, kapsam dışıdır; sonuç okunurken iddia edilmeyecek şeylerdir.

- Gerçek dolum fiyatı ve kitap derinliği
- Borsa kesintisi, API arızası, likidite kuruması
- Rejim değişimi (geçmişte kârlı olan gelecekte kârlı değildir)
- Kayma varsayımının ince kitaplı sembollerde tutup tutmadığı
  (`breakdowns.symbol > cost_per_r` ipucu verir ama kanıt değildir)
- **§6f'nin C kapısının GEÇMESİ, iki serinin AYNI olduğu anlamına gelmez.** Geçmesi
  "olay kümeleri bu örneklemde AYIRT EDİLEMİYOR" demektir — 60 günde sembol başına ~180
  damga, p95 ≈ 9 olay, 13 sembolde ≈ 117 olay, ve `Jaccard ≥ 0.70` her iki tarafta ~21
  olayın kaymasına izin verir. Bu satır buraya, ileride birinin kapıyı "arşiv
  doğrulandı" diye okuyacağı için yazıldı: kapı bir doğrulama değil, bir AYIRT
  EDİLEMEZLİK ölçüsüdür ve örneklemi küçüktür.
- **Cross-venue bir arşivle seçilen bir eşiğin OKX'te de aynı olayları seçtiği**
  (§6f > Adım C). Gösterilen şey yalnızca ayırt edilemediğidir; "aynı oran" şartı başka
  bir borsada anlamsızdır ve bu sapma arşiv kullanılırsa §5'e yazılır. Satır arşiv
  gelmeden önce buraya kondu — sonradan eklenseydi, sonucu gördükten sonra yazılmış bir
  uyarı olurdu.

---

## 9. Koşunun kendisi denetlenebilir olmalı

Her backtest koşusu çıktısına şunları yazar: katman, pencere (başlangıç/bitiş), model
listesi, **config parmak izi** (koşuda geçerli tüm ayarların özeti) ve harness sürümü
(git SHA). Aynı girdilerle tekrar koşulduğunda aynı sonucu vermelidir — `random_seed`
sabittir ve rastgelelik kullanan her yol ondan beslenir.

Sonuç dosyaları `backtests/` altına yazılır ve **depoya girmez**: bir backtest ölçümün
kendisi değil, ölçüm hakkında bir denemedir. Karara giren sayılar bu belgeye ve
`docs/decisions.md`'ye yazılır.
