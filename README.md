# crypto-paper-bot

Strateji modellerinin aynı kripto piyasa verisini görüp kendi izole sanal hesabıyla
paper-trading yaptığı bir **ölçüm projesi.** Amaç kâr etmek değil, stratejileri adil ve
tekrarlanabilir koşullarda kıyaslamaktır. Kurallar ve mimari için bkz. [`CLAUDE.md`](./CLAUDE.md).

Kaç modelin koştuğu sabit bir sayı DEĞİLDİR ve burada tekrar edilmez: **tek kaynak
`config.yaml`'ın katman `models` listeleridir** (bkz. [Model listesi](#model-listesi)).
Bir eksen kapandığında model emekli edilir, kodu ve defteri durur — kadro hareket eder.

Ölçüm **iki katmanda** yürür ve ikisi de aynı çekirdeği kullanır: `base` 4 saatlik ana
yarışma, `scalp` 15 dakikalık scalp katmanıdır (bkz. [Katmanlar](#katmanlar)). Katmanlar ayrı
defterlere yazar, ayrı cron'la koşar ve **aynı tabloda kıyaslanmaz** — zaman dilimi farkı
doğrudan kıyası yanıltıcı yapar.

## Nasıl çalışır (özet)

- `core/engine.py` her turda tüm stratejileri aynı `MarketData` anlık görüntüsüyle çağırır.
- Her strateji yalnızca `Signal` (yön + giriş/çıkış seviyeleri) üretir; defter yazımı,
  komisyon/funding hesabı ve pozisyon boyutlandırma her zaman `core/` içinde, tüm modeller
  için aynı kurallarla yapılır.
- Sinyal üretildiği barda dolmaz: **bir sonraki barın açılışından** dolar (kural 13). Bu
  yüzden emirler koşular arasında defterde bekler.
- Her barda açık pozisyonlar şu sırayla kontrol edilir: **likidasyon → stop → take-profit.**
  Stop ve TP aynı mumun aralığındaysa kötü olan (stop) gerçekleşmiş varsayılır.
- Her stratejinin işlemleri kendi defterine (`ledgers/`) yazılır ve `core/metrics.py`
  tarafından karşılaştırılır.

## Çalıştırma

```bash
python main.py                    # 4 saatlik katmanda bir tur (varsayılan: --layer base)
python main.py --layer scalp      # 15 dakikalık scalp katmanında bir tur
python main.py --dry-run          # deftere ve rapor dosyasına YAZMADAN aynı turu raporla
```

`main.py` bir turu uçtan uca yürütür: veriyi çeker, `as_of`'u belirler, `config.yaml`'daki
modelleri sırayla koşturur, metrikleri üretir ve `docs/data/metrics.json`'a yazar. `--dry-run`
defterin bir kopyası üzerinde çalışır — motor turu ilerletirken durumu yazdığı için "yazmayan
motor" metrikleri üretecek satırları hiç oluşturmazdı.

Hata izolasyonu kasten iki katmanlıdır: **model kurulumu** (tanınmayan ad, kurucu hatası) ve
**model kodu** (sinyal/çıkış üretimi) izole edilir — biri düşse de diğer modeller koşar. Tanınmayan
bir model koşuyu ayrıca hata koduyla bitirir; sessizce eksik yarışan bir küme kural 6'yı bozardı.
**Defter ve veri hataları izole EDİLMEZ:** bozuk defter ya da bayat anlık görüntü turu tümden
düşürür — bunlar model hatası değil ölçüm hatasıdır.

Scalp katmanı `.github/workflows/run-scalp.yml` ile koşar ve yalnızca `ledgers_scalp/` +
`docs/data/metrics_scalp.json` commit eder; `run.yml`e hiç dokunmaz. Dosyada **cron
YOKTUR** — yalnızca `workflow_dispatch` — ve tetikleyici depo dışındadır (gözlenen kadans
~15 dakika, tur başına bir 15m barı). GitHub'ın kendi cron'u bu sıklıkta ölçüldü ve
tetiklemelerin ~%91'i düşüyordu; `schedule:` bloğu bu yüzden kaldırıldı. Bir tetikleme
gecikse ya da düşse bile ölçüm bozulmaz: motor son işlenmiş bardan `as_of`'a kadar aradaki
tüm barları sırayla ilerletir ve scalp'te `signals_per_bar: true` olduğu için her telafi
barı kendi sinyalini de üretir.

`.github/workflows/run.yml` base katmanını **saatlik** cron'la (`5 * * * *`) çalıştırır —
4 saatlik değil. Gerekçe ölçüldü (docs/decisions.md > 39): `5 0,4,8,12,16,20` iken turların
%35'i telafi yapıyor ve barların **%26'sı sinyalsiz** geçiyordu, üstelik kaybolan bar hep
00:00 ya da 08:00 barıydı — yani kayıp gürültü değil YANLILIK. Saatlik kadans her 4H barına
dört bağımsız şans vermeyi HEDEFLER; ölçülen teslim 0.97'dir (karar 39-DOĞRULAMA) ve
ölçüm kuralına dokunmaz (`signals_per_bar` base'de KAPALI
kalır). Turların dörtte üçü yeni bar bulamaz; onları `advanced` kapısı süzer, çünkü commit
edilseler HEAD'deki tur denetim izini boş bir turla ezerlerdi. Koşudan sonra `ledgers/` ve
`docs/data/` commit edilir (değişiklik yoksa boş commit atılmaz). **Defter bu yüzden depoya
girer:** runner her koşuda sıfırdan kurulur, commit edilmezse her tur boş bakiyeyle başlar
ve ölçüm hiç birikmez.

Her tur `docs/data/metrics.json`'a bir **tur raporu** da yazar: model başına işlenen bar,
dolum, kapanan işlem, sinyal sayısı ve **doldurulamayan emirlerin sebep kodu dökümü**
(`rejections`). Bu döküm opsiyonel değil: "sinyal üretildi ama işlem açılmadı" iki bambaşka
şeyin aynı görünümüdür — beklenen bir tekrar (`duplicate_position`, örn. alım-tut çıpasının
zaten taşıdığı pozisyon) ile gerçek bir boyutlandırma arızası (`zero_size`,
`insufficient_cash`). Kod olmadan ikisi aylar sonra ayırt edilemez. Log seviyesi de aynı
ayrımı taşır: beklenen tekrar `INFO`, arıza `WARNING`.

## Katmanlar

`config.yaml > layers` iki katman tanımlar; ikisi de **aynı** `main.py`, `core/engine.py`,
`core/portfolio.py`, `core/ledger.py` ve `core/metrics.py` kodunu koşar. Kopyalanan hiçbir
şey yoktur — katman yalnızca ölçümün koşullarını değiştirir.

| | `base` | `scalp` |
|---|---|---|
| Bar | 4H | 15m |
| Evren | hacme göre ilk 50, 30 günde bir yenilenir | **sabit 13 sembol** (BTC, ETH, SOL, XRP, DOGE, BNB, AVAX, LINK, ADA, SUI, NEAR, PENGU, ETHFI) |
| Modeller | bkz. [Model listesi](#model-listesi) — tek kaynak `config.yaml > models` | bkz. [Model listesi](#model-listesi) — tek kaynak `config.yaml > layers.scalp.models` |
| Defter | `ledgers/` | `ledgers_scalp/` |
| Rapor | `docs/data/metrics.json` | `docs/data/metrics_scalp.json` |
| Cron | `run.yml` — **saatlik** (`5 * * * *`); hedef bar başına 4 şans, ölçülen 0.97 (karar 39-DOĞRULAMA) | `run-scalp.yml` — cron YOK, dış tetikleyici (~15 dk) |
| Telafi barında sinyal | yok (`signals_per_bar: false`) | var (`signals_per_bar: true`) |
| Stop tavanı | 3×ATR | 8×ATR |
| Kırılımlar | yok | kol + sembol + çıkış kuralı + seans + kayıp serisi |

**Maliyet ve risk sabitleri iki katmanda da birebir aynıdır** (`risk_per_trade`, `fee_rate`,
`slippage_*`, `leverage_cap`, `initial_capital`, `maintenance_margin`): kök config tek
kaynaktır, katman bloğu yalnızca farkı yazar. Ayrı bir `scalp_config.yaml`, bu sabitlerin bir
gün sessizce ayrışmasına kapı açardı.

Evrenin **sabit** olması bir tercih değil bir kıyas koşuludur: evren zamanla kayarsa geçmiş
performans başka bir sembol kümesine ait olur ve iki modelin sayıları aynı yarışın sayıları
olmaktan çıkar.

### Scalp katmanının beş kollu modelleri

`scalp_bandit` (11), `scalp_fixed` (12), `scalp_managed` (15), `scalp_patient` (16) ve
`scalp_vol` (17) **beş ortak kolu** (`strategies/scalp/arms.py`) oynar; hangilerinin canlı
koştuğu [Model listesi](#model-listesi)ndedir. Kollar:

1. **VWAP geri çekilme** — gün-çapalı VWAP'e trend yönünde dokunuş
2. **Açılış aralığı kırılımı** — günün ilk 4 barının aralığı + hacim teyidi
3. **RSI(2) aşırılık dönüşü** — yalnızca aralık rejiminde
4. **Momentum patlaması devamı** — 3 bar üst üste aynı yön + hacim — ⚠ **ÖLÜ KOL**
5. **Funding sıçraması fade'i** — funding aniden yükseldiğinde short — ⚠ **ÖLÜ KOL**

⚠ **Kol tanımı beş, gerçekte koşan üç.** 4. ve 5. kol katmanın canlı ömrü boyunca
(2026-09-13'ten beri, 123 pozisyon) **tek bir sinyal üretmedi**: deftere sıfır işlem,
açık pozisyonlarda sıfır kayıt. Bu yüzden aşağıdaki eksenlerin hepsi beş kolun değil
**üç kolun** ölçümüdür (bkz. [Ölçüm eksenleri](#ölçüm-eksenleri) ve karar 48).
Kol tanımları SİLİNMEDİ: `momentum_burst`un ölü olma sebebi ölçüldü ve yazıldı (karar
34 — `hedef/stop ≥ 1.5` kapısı, `stop = 5×ATR` iken yapısal engelin 7.5×ATR ötede
olmasını istiyor, `burst` ise tipik olarak 1–2.25×ATR), `funding_spike_fade`in sebebi
ise **BİLİNMİYOR** — `ScalpModel` `take_survey` uygulamadığı için hangi kapıda elendiği
hiçbir yere yazılmıyor. Onu öğrenmek açık bir iştir.

Modeller birbirinden **tek bir eksende** ayrışır ve her eksen bir soruyu ölçer
(bkz. [Ölçüm eksenleri](#ölçüm-eksenleri)): `scalp_bandit` kol tahsisini öğrenir,
`scalp_fixed` eşit ağırlık oynar (adaptasyonun katkısı); `scalp_managed` üç aşamalı çıkış
yönetimi ekler (yönetimin katkısı); `scalp_patient` yalnızca zaman stop'unun sınırını
değiştirir (sürenin katkısı); `scalp_vol` bir volatilite rejim kapısı ekler (rejimin
katkısı). Tek değişkenli olmaları şart: fark başka bir yerde de varsa ortalama R farkı o
eksenin ölçüsü olmaktan çıkar.

Hepsinde aynı kısıtlar: stop mesafesi girişin **%1'inin altındaysa işlem alınmaz**
(tur maliyeti ~%0.25; daha dar stop'ta maliyet 0.25R'yi aşar), **hedef/stop en az 1.5**,
**16 bar (4 saat) sonra zaman stop'u**, long ve short açık, bütçe ve risk kuralları 4 saatlik
modellerle aynı. Stop **genişletilmez**, kurulum atlanır ve her atlama loglanır.

Bandit'in durumu ayrı bir dosyada tutulmaz: posterior `ledgers_scalp/scalp_bandit/trades.csv`
üzerinden her turda yeniden kurulur (kol etiketi + gerçekleşen R + son 100 işlem). Her
işlemin `signal_reason` kuyruğunda o anki karar durur:
`... | arm=vwap_pullback | post_r=0.31`. Kol etiketi olmayan bir satır sessizce atlanmaz,
hata fırlatılır.

### Ölçüm eksenleri

Scalp katmanı bir "en iyi model" yarışı değil, bir **eksen tablosudur**: her eksende tam
olarak TEK bir değişken ayrışır ve iki modelin ortalama R farkı o değişkenin ölçüsüdür.
Bir eksen kapandığında satır silinmez — kapanışın kendisi bir ölçüm sonucudur.

| Eksen | Çift | Ayrışan tek şey | Kapsam | Durum |
|---|---|---|---|---|
| Sürenin katkısı | `scalp_fixed` (12) ↔ `scalp_patient` (16) | zaman stop'u sınırı (16 ↔ 100 bar) | **üç kol** | **AÇIK** — hareket eden tek eksen (−0.15 ↔ −0.01) |
| İki sistemin toplam farkı ⚠ | `vwap_clone` (13) ↔ `vwap_managed` (14) | **tek değişken DEĞİL** — altı eksende birden ayrışır | kol yok (ayrı sinyal modülleri) | AÇIK |
| Adaptasyonun katkısı | `scalp_bandit` (11) ↔ `scalp_fixed` (12) | kol seçimi | **üç kol** | KAPALI — iki kez "fark yok" (karar 33) |
| Çıkış yönetiminin katkısı | `scalp_fixed` (12) ↔ `scalp_managed` (15) | üç aşamalı çıkış | **üç kol** | KAPALI — iki kez "fark yok" (karar 33) |
| Volatilite rejiminin katkısı | `scalp_patient` (16) ↔ `scalp_vol` (17) | kesitsel ATR% medyan kapısı | **üç kol** | KAPALI — ön-kayıtlı P1 düştü (karar 36) |

⚠ **Kapsam kolonu neden var.** Beş kollu modellerin dördünde de 4. ve 5. kol hiç
tetiklemedi (karar 48), yani bu eksenlerin hiçbiri "beş kollu bir modelde" ölçülmedi —
üçünde ölçüldü ve pratikte ikisinde (`rsi2_reversal` 123 pozisyonun 101'ini,
`opening_range_breakout` 21'ini taşıyor). Eksen SONUÇLARI geçersiz değildir: çiftin iki
tarafı da aynı üç kolu gördü, yani kural 6 bozulmadı ve fark hâlâ ayrışan tek
değişkenin ölçüsüdür. Geçersiz olan, sonucun beş kol HAKKINDA okunmasıdır — "beş kollu
bir modelde adaptasyonun katkısı yok" ile "üç kollu bir modelde adaptasyonun katkısı
yok" aynı iddia değildir, ikincisi çok daha dardır.

**13 ↔ 14 bir eksen DEĞİL, bir toplam farktır.** İki model sinyal kuralları, boyutlandırma,
ev kapıları, seçim politikası, bar başına sinyal sayısı ve evren olmak üzere altı eksende
birden ayrışır; böyle bir fark "ev kurallarının katkısı" olarak okunamaz. Tek değişkenli bir
eksen isteniyorsa yolu yeni bir model açmaktır — mevcut ikisinden birini diğerine
yaklaştırmak değil (bu, model 13'ü kopya olmaktan çıkarırdı).

**Çekiliş, ölçülmeyen eksende PAYLAŞILIR, ölçülen eksende BAĞIMSIZDIR.** Model 11 ↔ 12'de
ölçülen şey seçimin kendisidir; çekilişi paylaşsalardı fark adaptasyonun değil tesadüfün
ölçüsü olurdu. Model 12 ↔ 15'te ise ölçülen şey seçim değil aynı seçimin nasıl
yönetildiğidir — bu yüzden 15, 12'nin çekiliş kimliğini kullanır (eşleştirilmiş deney).

## Dashboard (GitHub Pages)

`docs/index.html` statik bir sayfadır: harici framework yok, CDN yok, build adımı yok.
Tek veri kaynağı kardeş dosya `docs/data/metrics.json`'dır — her turda `main.py` üretir,
koşu workflow'u defterlerle birlikte commit eder. Ortak tasarım dili ve biçimleyiciler iki
sayfanın paylaştığı kardeş dosyalarda durur (`docs/shared.css`, `docs/shared.js`): aynı
sayının iki sayfada farklı görünmesi okuyucuya iki ayrı ölçüm gibi gelirdi. Sayfayı yayına almak için depo
ayarlarında **Settings → Pages → Source: Deploy from a branch → `main` / `/docs`** seçmek
yeterlidir; ayrı bir deploy workflow'u gerekmez.

Sayfa scalp katmanını **ayrı bir bölümde** gösterir ve ikinci bir dosyadan okur
(`docs/data/metrics_scalp.json`): scalp modelleri 4 saatliklerle aynı tabloda hiçbir koşulda
sıralanmaz. O bölümde ayrıca **kol** ve **sembol** kırılımı vardır — kol kırılımı tahsisin
eşitten sapıp sapmadığını, sembol kırılımı ise kayma varsayımının ince kitaplı sembollerde
(PENGU, ETHFI) tutup tutmadığını gösterir. Dosya yoksa (katman henüz koşmadıysa) bölüm
sessizce gizli kalır.

Sayfa **iki seviyelidir**: genel bakış tüm modelleri yan yana koyar, model kartına
dokunulduğunda o modelin detayı açılır. Detayın adresi `#model=<ad>` hash'idir — geri tuşu,
yer imi ve paylaşılan link çalışır.

### İkinci sayfa: pozisyonlar & işlemler (`docs/positions.html`)

Dashboard "hangi model önde" der; `docs/positions.html` **"tam olarak ne açık ve tam olarak
ne kapandı"** der. **Tüm katmanlar ve tüm modeller tek sayfadadır** (iki yükü de okur) ve
katman/model/yön/sonuç/tarih filtreleriyle daraltılır; filtrelenmiş görünümün adresi
paylaşılabilir (durum adres çubuğunda durur).

| Tablo | Kolonlar |
|---|---|
| Açık pozisyonlar | model, sembol, yön, giriş, anlık fiyat, anlık K/Z (USDT), anlık R, TP, SL, **durum rozetleri** (başabaş alındı mı / kısmi alındı mı / takip aktif mi), kaldıraç, margin, riske edilen tutar, birikmiş funding, açılış (UTC) ve kırpılmamış strateji gerekçesi. |
| Kapanmış işlemler | model, sembol, yön, **sonuç** (KAZANÇ/KAYIP + çıkış sebebi: TP / stop / takip eden stop / likidasyon / zaman stop'u), giriş, çıkış, miktar, kaldıraç, K/Z, komisyon, funding, R, açılış ve kapanış (UTC), gerekçe. Sayfalı, 50'şer. |

Bu tablo bir **dolum** defteridir: kısmi çıkışlar ve fraksiyonel hedefler ayrı satır
olarak görünür ama **istatistiğe DAHİL EDİLMEZ** ve "DİLİM" rozetiyle durur — tamamlanmış
işlem değil, aynı pozisyonun dilimidirler. Ölçüm onları pozisyon başına birleştirir
(`core/metrics.py::merge_fills`), bu yüzden **işlem sayısı satır sayısından az olabilir**.

Sayfa ortalama R'yi ve kazanma oranını **hesaplamaz**, yükten okur
(`docs/shared.js::statsSlice`): ikinci bir hesap yolu bugün hizalansa bile yarın ayrışır ve
aynı model iki sayfada iki farklı kazanma oranı gösterirdi. Katman etiketi her satırda
durur ve ölçüm şeridi **katman başına ayrı bir kart** verir — katmanlar arası kıyas burada
da yapılmaz. Sonuç ve tarih filtreleri yalnızca tabloyu daraltır; bu da yazar. 480px
altında tablolar kart düzenine döner; hiçbir sayı kırpılmaz.

**Seviye 1 — genel bakış**

| Bölüm | Ne gösterir |
|---|---|
| Özet kartları | Yarışmacıların toplam kapanmış işlem sayısı, ortalama R'ye göre lider model (kapılarını geçip geçmediğiyle) ve tek satırda long vs short. |
| LONG vs SHORT | Projenin ana sorusu: tüm yarışmacıların long işlemleri havuzu vs short işlemleri havuzu — ortalama R, kazanma oranı, işlem sayısı, net funding katkısı ayrı ayrı. Havuz **işleme** oy verir, modele değil: model ortalamalarının ortalaması 2 işlemlik bir modeli 200 işlemlik bir modelle eşitlerdi. |
| Model kartları | Tez tipine göre gruplanmış (trend & momentum, ortalamaya dönüş, kırılım & tuzak, sadece short, meta, referans): ortalama R, hesap getirisi, işlem sayısı, mini özsermaye kıvrımı, kabul rozetleri — **iki kapı** (Ö, E) ve **bir uyarı** (⚠ B); bkz. Kabul çıtası. Gruplama sıralamayı gizlemesin diye her yarışmacı kartı ortalama R sıralamasındaki rütbesini taşır. `random_ctrl` KONTROL etiketiyle yarışmacılar arasında sıralanır; `buyhold` REFERANS grubundadır ve kartında hero sayı ortalama R değil hesap getirisidir (kural 15: stop'u olmayanın 1R'si yoktur). |
| Özsermaye eğrileri | Tüm modeller tek grafikte; bir modele tıklayınca yalnız o kalır. Kesikli gri çizgi başlangıç sermayesi. |
| Getiri korelasyonu | Modellerin bar getirilerinin Pearson korelasyonu (çift bazında örtüşme). Yüksek korelasyonla yarışan iki model bağımsız iki ölçüm değil, aynı ölçümün iki kopyasıdır. Dar ekranda matris yerine en güçlü çiftler listelenir. |

**Seviye 2 — model detayı (`#model=<ad>`)**

| Bölüm | Ne gösterir |
|---|---|
| Üst şerit | Ortalama R, getiri, max drawdown, kazanma oranı, profit factor, işlem sayısı, `cost_per_r`, `avg_stop_distance_pct`, hesap Sharpe'ı ve son özsermaye; yanında kabul rozetleri — rozete dokununca gerekçesi açılır (dokunmatikte `title` okunamaz). |
| Long / short kırılımı | Modelin kendi yön ayrışması: ortalama R, kazanma, toplam R, profit factor, PnL, funding + yön bazlı stop mesafesi, R başına maliyet ve R Sharpe'ı. |
| Açık pozisyonlar | Sembol, yön, miktar, giriş, güncel fiyat, notional, gerçekleşmemiş K/Z (USDT ve %), stop, hedef(ler), açılış zamanı ve stratejinin gerekçesi. K/Z **çıkış maliyeti hariçtir** (pozisyon kapanmadı, çıkış fiyatı bilinmiyor) ve sayfa bunu söyler. Masaüstünde tablo, dar ekranda kart. |
| Kapanmış işlemler | Model başına son 100 işlemden 20'şerlik sayfalar, yeniden eskiye, yön filtresiyle (hepsi / long / short): sembol, yön, giriş-çıkış fiyatı ve zamanı, miktar, R, net K/Z, gerçekleşen 1R, komisyon, kayma, funding, çıkış sebebi (stop / hedef / likidasyon / sinyal) ve `signal_reason` kırpılmadan — ensemble'ın oy sayısı, confluence'ın güven kuyruğu dâhil. |
| Özsermaye eğrisi | Yalnızca o modelin eğrisi, başlangıç sermayesi çizgisiyle. |

Mobilde yatay kaydırma yoktur: geniş tablolar dar ekranda kart düzenine döner (kaydırma
çubuğuna sarılmaz — ekran dışına itilen bir K/Z kolonu hiç gösterilmemiş demektir), sayılar
ortadan bölünmez ve dokunma hedefleri en az 44px'dir.

Sayfa bir **süs katmanıdır**: ölçüm defterde ve JSON'dadır, sayfa yalnızca onu çizer.
Yerelde açmak için `python -m http.server` gerekir (`file://` ile `fetch` engellenir).

## Telegram özeti

`scripts/telegram_report.py` günde bir kez, **`as_of` saati 20:00 UTC olan turda** tek bir
mesaj yollar: long vs short güncel durum, ortalama R'ye göre ilk 3 ve son 3 model, son 24
saatte açılan/kapanan işlemler ve kabul çıtasını geçen model olup olmadığı (yoksa en yakını
hangi kapıda takıldığı). Band uyarısı geçen modelin yanında `⚠` olarak anılır — kapı
olmadığı için "geçemedi" diye raporlanmaz.

Özet yalnızca **4 saatlik katmanı** kapsar (`docs/data/metrics.json`): günde 96 tur koşan
scalp katmanını aynı bildirime eklemek, günlük bir özeti iki farklı zaman ölçeğinin
karışımına çevirirdi.

```bash
python scripts/telegram_report.py --dry-run --force   # yollamadan mesajı gör
```

Token'lar GitHub Secrets'tan ortama geçer: **`TELEGRAM_BOT_TOKEN`** ve **`TELEGRAM_CHAT_ID`**
(Settings → Secrets and variables → Actions). Tanımlı değilse script bunu bilgi olarak
loglar ve sessizce geçer.

Kapı duvar saatine değil `as_of` barına bakar: 4H barlarda gün içinde altı tur koşar ve
"günde bir"in tekrarlanabilir tanımı "as_of'u 20:00 olan tur"dur. Raporun tazeliği de ayrıca
kontrol edilir, yoksa tur düştüğünde depoda kalan dünkü 20:00 raporu yeniden yollanırdı.

**Telegram hatası koşuyu düşürmez.** Script her yolda 0 döner (eksik token, ağ hatası,
Telegram 4xx'i, bozuk JSON — hepsi loglanıp geçilir), workflow adımı ayrıca
`continue-on-error: true` taşır ve defter commit'inden **sonra** gelir. Özet bir bildirimdir;
onun kesintisi yüzünden turun kırmızı dönmesi, defterin commit'lenip commit'lenmediğine dair
gerçek sinyali gürültüye boğardı.

## Scalp sinyal bildirimi (anlık)

`scripts/telegram_signals.py` yukarıdaki günlük özetten **ayrıdır** ve ona dokunmaz: özet
"hangi model önde" der ve okunması zamana bağlı değildir, bu ise **"şu anda ne açılıyor"**
der ve cevabın raf ömrü bir bardır. Scalp katmanının her turunda (saatlik) koşar ve
**yalnızca yeni sinyalde** mesaj yollar — pozisyon kapanışı, funding tahakkuku ve bar
ilerlemesi mesaj üretmez.

Mesaj şunları taşır: model ve kol (`scalp_bandit / rsi2_reversal`), sembol, yön, sinyalin
üretildiği bar ve o barın kapanış fiyatı, planlanan stop/hedef ve R:R oranı, kısaltılmış
gerekçe — ve zorunlu bir uyarı satırı:

> ⚠️ Bot bu emri bir sonraki bar açılışından dolduracak (… UTC). Senin girişin farklı bir
> fiyattan olacak.

Uyarı opsiyonel değildir: bildirim barın kapanışında gider, emir ise bir **sonraki** barın
açılışından dolar (kural 13) — arada piyasadan giren kişinin fiyatı botunkiyle aynı olmaz.

**Üç filtre gürültüyü keser:**

- **Yalnızca son barın sinyalleri.** Saatlik cron her turda dört 15m barını işler ve telafi
  edilen barlar da kendi sinyallerini üretir; onlar deftere yazılır ve ölçüme girer ama
  bildirilmez — 45 dakika önceki bir barın emri çoktan dolmuştur.
- **Aynı (model, sembol, yön) için 4 bar susturma.** Durum `state/telegram_scalp.json`'da
  tutulur ve koşular arası commit edilir; ölçümün parçası değildir, silinse en kötü ihtimalle
  bir mesaj tekrar eder.
- **5'ten fazla sinyalde tek toplu mesaj**, tek tek değil.

```bash
python scripts/telegram_signals.py --dry-run --force   # yollamadan mesajları gör
```

Secret'lar günlük özetle **aynıdır** (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`) ve söz de
aynı: script her yolda 0 döner, workflow adımı `continue-on-error: true` taşır ve defter
commit'inden **sonra** gelir. Bildirim ölçümü düşüremez.

## Defter formatı (`ledgers/<model>/`, scalp için `ledgers_scalp/<model>/`)

| Dosya | İçerik |
|---|---|
| `positions.json` | Koşular arası taşınan durum: nakit, açık pozisyonlar, bekleyen emirler, işlenmiş son bar. |
| `trades.csv` | Kapanan her işlem (kısmi çıkışlar dâhil): giriş/çıkış zamanı ve fiyatı, yön, miktar, notional, ilk stop, **riske edilen tutar (`risk_amount`)**, kaldıraç, marj, komisyon, funding, **ödenen kayma (`slippage_cost`)**, PnL, çıkış sebebi (`stop`/`tp`/`liquidation`/`signal`) ve stratejinin gerekçesi. |
| `equity.csv` | Bar başına nakit, kullanılan marj, gerçekleşmemiş PnL, özsermaye ve açık pozisyon sayısı. |

Yazmalar atomiktir (geçici dosya + `rename`); yazılmış bir işlem satırı asla değiştirilmez.
Tek istisna `equity.csv`'dir ve yalnızca saklama penceresi tanımlı katmanlarda geçerlidir:
scalp katmanında 30 günden eski özsermaye satırları günlük özete indirilir (günde 96 tur ×
2 model, yılda on binlerce satır — depo geçmişi ölçümle ilgisiz satırlarla şişmesin). Taze 30
gün bar bazında kalır ve `trades.csv` hiçbir koşulda dokunulmaz: denetim izi odur.
Kapanan işlemlerin `pnl` toplamı bakiyedeki değişime eşittir — defter bu yüzden
denetlenebilir. `risk_amount` (`adet × |giriş − ilk stop|`) deftere yazılır çünkü
karşılaştırmanın birinci sınıf metriği olan **R** (`pnl / risk_amount`) onun üzerinden
hesaplanır; R'nin kendisi türetilmiş bir değer olduğu için deftere değil `core/metrics.py`'ye
aittir.

## Karşılaştırma

```python
from core.config import load_config
from core.metrics import compare, format_report

print(format_report(compare(
    ["buyhold", "model_a"], config=load_config(), benchmarks=["buyhold"]
)))
```

Kayma dolum fiyatının içine gömülü olduğu için `slippage_cost` deftere ayrıca yazılır:
`cost_per_r` komisyon **ve** kayma ister, yazılmazsa maliyetin yarısı görünmez kalırdı.

Referans modeller (kural 15) bu sıralamaya girmez: tablonun altında ayrı bir **REFERANS**
bölümünde durur ve `avg_stop_distance_pct` / `cost_per_r` kolonlarında `—` gösterir. Stop'u
olmayanın 1R'si yoktur; çıpanın taşıdığı bilgi sıralamada değil hesap düzeyi getirisindedir
("model piyasayı yendi mi?").

Tablo **ortalama R'ye göre** sıralanır ve her model için long / short / TOPLAM satırlarını
ayrı gösterir. Toplam getiri (USDT ve %) ikinci sırada, hesap düzeyinde raporlanır: hesapta
tek bakiye olduğu için getiri, max drawdown ve Sharpe yön bazında ayrıştırılamaz — yönlerin
risk profili bunun yerine o yönün kümülatif R eğrisinden ölçülür (`r_sharpe`,
`max_drawdown_r`). Ölçülemeyen bir metrik `—` yazılır; 0.0 gösterilmez.

## Kurulum ve manuel veri kontrolü

```bash
pip install requests pandas numpy pyyaml pyarrow pytest
pytest -q

# 3 sembol için veri çekip son 5 barı yazdırır (evrenden ilk 3 sembol için argümansız çalışır)
python scripts/manual_data_check.py BTC-USDT-SWAP ETH-USDT-SWAP SOL-USDT-SWAP
```

Veri kaynağı **OKX public API v5**'tir (API anahtarı gerekmez). Semboller OKX `instId`
biçimindedir (`BTC-USDT-SWAP`). Evren `data/universe.json`'a, mumlar ve funding geçmişi
`data/cache/` altına parquet olarak yazılır; `data/` klasörü depoya girmez ve her koşuda
yalnızca eksik barlar çekilir. Son bar **kapanmamışsa atılır** — `MarketData.as_of` her
zaman son kapanmış barın zamanıdır (bkz. CLAUDE.md kural 12). `as_of` sabit bir çıpadan
okunur: **BTC-USDT-SWAP'ın son kapanmış barı.** Bu bara sahip olmayan semboller o tur
dışlanır ve loglanır; böylece tek bir gecikmiş sembol turun "şimdi"sini geri çekemez.

## Model listesi

**Bu bölümün tek kaynağı `config.yaml`'dır ve bağ bir testle korunur**
(`tests/test_docs_sync.py`): aşağıdaki AKTİF LİG tabloları katmanların `models` listeleriyle
birebir eşleşmezse test kırmızıya döner. Bu koruma bir titizlik değil, ölçülmüş bir ihtiyaç:
karar 33 kadroyu sadeleştirirken `config.yaml` ve `CLAUDE.md` güncellendi, README
güncellenmedi ve belge aylarca "10 yarışmacı" demeye devam etti.

Üç küme vardır ve karışmamaları önemlidir: **aktif lig** (katmanın `models` listesi — bugün
defter yazan modeller), **katalog** (`strategies/registry.py`'de kayıtlı ama listede olmayan
— emekli ya da aday), **yarışma dışı** (çıpa ve kopya; kural 15/15b).

### Aktif lig — `base` (4H, `config.yaml > models`)

| # | Strateji | Yön | Tez |
|---|---|---|---|
| — | `buyhold` | long | **referans çıpası** (kural 15), yarışmacı değil |
| 1 | `trend` | long + short | Donchian kırılımı + EMA rejim filtresi |
| 2 | `meanrev` | long + short | RSI + Bollinger ortalamaya dönüş (short'ta BTC rejim kapısı) |
| 9 | `random_ctrl` | long + short | **kontrol grubu**: bilgisiz çekiliş, edge'in referansı |

### Aktif lig — `scalp` (15m, `config.yaml > layers.scalp.models`)

| # | Strateji | Yön | Tez |
|---|---|---|---|
| 12 | `scalp_fixed` | long + short | beş kol (üçü canlı, bkz. karar 48), eşit ağırlıklı çekiliş, öğrenme yok — eksenlerin KONTROLÜ |
| 16 | `scalp_patient` | long + short | `scalp_fixed`in ikizi, tek farkı zaman stop'u sınırı (16 ↔ 100 bar) |
| 13 | `vwap_clone` | long + short | **dış sistem kopyası** (kural 15b), yarışmacı değil |
| 14 | `vwap_managed` | long + short | VWAP sapma-dönüş sinyali, ev kurallarıyla (risk boyutlandırma, %1 taban, 1.5R) |

### Kadro — `ema` (tanımlı, tetikleyicisi YOK)

4H, **sabit 13 sembol** (scalp katmanının evreninin aynısı), `config.yaml > layers.ema`.
Bu katmanın **cron'u yoktur ve `run-ema.yml` henüz eklenmemiştir**: tanım var, koşu yok.
Bugün onu okuyan tek şey `scripts/backtest.py`'dir ve hiçbir deftere yazılmaz. Canlıya
alınması, ön-kayıtlı kapıların (`docs/backtest.md > 6d`) geçilmesine bağlıdır.

Katmanın varlık sebebi evrenin sabitliğidir: `ema_trend`in backtest'i 13 coinde koşuyor,
base evreni ise hacme göre seçilen 50 coindir ve 30 günde bir kayar — modeli base'e almak,
backtest'in ölçtüğünden başka bir evrende koşturmak olurdu. Kıyas hedefi (`trend`), kontrol
grubu (`random_ctrl`) ve çıpa (`buyhold`) katmanın İÇİNE alındı, çünkü katmanlar arası kıyas
yapılmaz (CLAUDE.md > Katmanlar) ve sorulan soru tam olarak "bu model `trend`den iyi mi".

| # | Strateji | Yön | Tez |
|---|---|---|---|
| — | `buyhold` | long | **referans çıpası** (kural 15), yarışmacı değil |
| 1 | `trend` | long + short | Donchian kırılımı + EMA rejim filtresi — bu katmanda KIYAS HEDEFİ |
| 9 | `random_ctrl` | long + short | **kontrol grubu**: bilgisiz çekiliş, edge'in referansı |
| 18 | `ema_trend` | **yalnızca long** | EMA(21) EMA(55)'i yukarı keser; stop 1.5×ATR, hedef 2R, trailing ve zaman stop'u YOK |

### Katalog — kayıtlı ama listede değil

Emekli bir modelin **kodu ve defteri DURUR** (kural 1: defter append-only); listeye geri
eklemek bir commit'tir. Backtest onları `--models` ile hâlâ çağırabilir.

| # | Strateji | Durum | Gerekçe |
|---|---|---|---|
| 3 | `momentum` | emekli | 29 barlık geçmişte HİÇ sinyal üretmedi (karar 33) |
| 4 | `squeeze` | emekli | aynı — ölçülemeyen satır tabloda yalnızca gürültü üretir |
| 5 | `confluence` | emekli | örneklem kapısına (n=30) ~72 günde ulaşırdı |
| 6 | `failed_breakout` | emekli | 29 barlık geçmişte HİÇ sinyal üretmedi |
| 7 | `downtrend_rally` | emekli | aynı |
| 8 | `avwap` | emekli | örneklem kapısına ~42 günde ulaşırdı |
| 10 | `ensemble` | emekli | **meta** (kural 4); örneklem kapısına ~125 günde ulaşırdı |
| 11 | `scalp_bandit` | emekli | adaptasyon ekseni İKİ bağımsız pencerede de sıfır fark verdi (karar 33) |
| 15 | `scalp_managed` | emekli | çıkış yönetimi ekseni İKİ bağımsız pencerede de sıfır fark verdi (karar 33) |
| 17 | `scalp_vol` | aday, canlıda koşmaz | ön-kayıtlı birincil tahmin P1 DÜŞTÜ (karar 36) |

**Emekli ≠ silinmiş.** Ölçüt performans değil ÖLÇÜLEBİLİRLİKTİR: ne kadar iyi olduğunu asla
öğrenemeyeceğimiz bir satır, tabloda bir bilgi değil bir gürültü kaynağıdır.

### Yarışma dışı (kural 15 / 15b)

`buyhold` sayıya dâhil değildir: BTC %50 / ETH %50, 1x, stop'suz, bir kez alınıp hiç satılmaz.
Tek işi yarışmacılara bir zemin vermektir.

`vwap_clone` bir **dış sistem kopyasıdır** (`is_replica = True`, kural 15b): dış bir sistemin
kurallarını bizim maliyet, kayma, funding ve likidasyon varsayımlarımız altında yeniden
üretir. Sabit teminat × 10x ile koşar, kendi limitleri ve kendi 12 sembollük evreni vardır.
Çıpa gibi yarışmacı değildir ama AYRI bir bayraktır, çünkü ölçtüğü soru farklıdır: çıpa
"piyasa ne yaptı" der, kopya "dış sistem bizim varsayımlarımızla ne yapardı" der. Kabul
çıtasının zemini yalnızca `is_benchmark` satırlarından gelir; kopyayı zemin saymak, çıtayı
bir stratejinin performansına bağlamak olurdu.

`ensemble` (emekli, katalogda) projedeki tek META modeldir (`is_meta = True`): kendi sinyal
mantığı yoktur, o turda normal modellerin ürettiği sinyalleri salt okunur okur ve aynı
sembolde aynı yönde en az 2 modelin birleştiği yerde işlem açar. Oy veren havuz
`random_ctrl` (kontrol grubu) ile `buyhold` (referans çıpası) DIŞINDADIR; oylar eşit
ağırlıklıdır. Stop katılımcıların en genişi, hedef en yakını (tek TP, tamamı). Ölçtüğü tek
şey: **üst üste binme işe yarıyor mu.**

`random_ctrl` ise sayıya dâhildir ve `is_benchmark` DEĞİLDİR: boyutlandırması, stop ölçeği ve
limitleri yarışmacılarla birebir aynıdır, tek farkı sinyalin bilgisiz olmasıdır. Çıpa "piyasa
ne yaptı"yı ölçer, kontrol ise "sinyalin kendisi bir şey söylüyor mu"yu — ikincisinin cevabı
ancak aynı sütunda, aynı ortalama R sıralamasında okunabilir.

## Kabul çıtası

İki ayrı çıta vardır ve karıştırılmamalıdır: **kod çıtası** (bir strateji "tamamlandı" mı)
ve **sonuç çıtası** (bir modelin ölçülmüş sonucu okunabilir mi).

### Sonuç çıtası — iki kapı + bir uyarı (dashboard rozetleri)

Eşikler `config.yaml > acceptance` altındadır; hesap `core/metrics.py::acceptance_flags`.
Bir model ancak **iki kapı da** yeşilken doğrulanmış sayılır.

| Kapı | Soru | Geçme koşulu |
|---|---|---|
| **Ö** örneklem | Bu ortalama bir ölçüm mü, gürültü mü? | R'ye giren kapanmış işlem ≥ `acceptance.min_trades` (**30**) |
| **E** edge | Sonuç sinyalden mi geliyor? | ort. R > 0 **ve** `random_ctrl`'ü **en az `edge_margin_r` = 0.15R marjla** aşıyor **ve** farkın bootstrap güven aralığının **alt sınırı > 0** **ve** hesap getirisi `buyhold` çıpasını geçiyor. `random_ctrl`'ün KENDİ örneklemi `control_min_trades` (**30**) altındaysa kapı **değerlendirilemez** |

Marj olmadan kontrolü 0.01R ile geçen bir model de "geçti" sayılırdı; oysa bilgisiz
çekilişin kendi gürültüsü o kadar farkı tek başına üretir.

| Uyarı | Soru | Tetiklenme |
|---|---|---|
| **⚠ B** band | Bu satır başka bir satırla aynı maliyet ölçeğinde mi? (kural 14) | `avg_stop_distance_pct`, yarışmacı medyanının `medyan/√2.5 .. medyan×√2.5` bandının **dışında** |

**Band bir kapı değildir ve doğrulamayı engellemez.** Bandın dışında kalmak bir kusur değil
bir kıyas koşuludur: modelin kendi ölçümü geçerlidir, ama o satırı bir başkasının yanına
koyarken **maliyet farkı (`cost_per_r`) dikkate alınmalıdır** — model aynı 1R'yi farklı
notional ile taşımış, yani R başına farklı komisyon+kayma ödemiştir. Tabloda uyarı ikonu
olarak durur; tooltip ne yapılacağını söyler.

Referans çıpası bu kapılara hiç girmez (kural 15): yarışmacı olmadığı için ölçmediği bir
yarışta not almaz. `random_ctrl` girer — bilgisiz çekilişin sıralamada nerede durduğu
gizlenecek bir kusur değil, raporlanacak bir sonuçtur. Gerekçeler için bkz.
[`CLAUDE.md` > Kabul Çıtası](./CLAUDE.md#kabul-çıtası-iki-kapı--bir-uyarı).

### Kod çıtası (taslak)

Bir stratejinin "tamamlandı" sayılması için:

- [ ] `strategies/base.py::Strategy` arayüzünü eksiksiz uygular, `core/`'a doğrudan erişmez.
- [ ] Kendi birim testi vardır ve mock `MarketData` ile bağımsız çalıştırılabilir.
- [ ] En az bir tam değerlendirme döngüsünde hatasız `Signal` üretir.
- [ ] Ürettiği `Signal.reason` alanı boş değildir (deftere yazılan gerekçe).
- [ ] `allowed_directions` dışında yön içeren sinyal üretmez.
- [ ] Stop mesafesi 1×–2.5×ATR bandındadır; veriye bağlı stop kuruyorsa
      `max_stop_atr_multiple` tavanını aşan işlemi atlar ve atlamayı loglar (kural 14).

Projenin "tamamlandı" sayılması için:

- [x] 10 strateji de arayüze uygun şekilde çalışıyor (9 yarışmacı + 1 meta; `buyhold` çıpa).
- [x] `core/portfolio.py`, `core/funding.py`, `core/ledger.py` tüm modeller için aynı kuralları
      uyguladığını kanıtlayan testlere sahip (`tests/test_portfolio.py`, `tests/test_funding.py`,
      `tests/test_ledger.py`, `tests/test_engine.py`).
- [x] `core/metrics.py` tüm stratejileri aynı tabloda karşılaştırabiliyor; her metrik
      long/short ayrı ve `avg_stop_distance_pct` + `cost_per_r` kolonları raporlanıyor
      (`tests/test_metrics.py`).
- [x] `.github/workflows/run.yml` periyodik çalıştırmayı yapıyor; testleri `ci.yml` doğruluyor.
- [x] `main.py` boru hattını uçtan uca çalıştırıyor ve `docs/data/metrics.json` üretiyor
      (`tests/test_main.py`).
- [x] Ölçüme bir referans çıpası (`buyhold`) eklendi (kural 15, `tests/test_buyhold.py`).
- [x] `docs/index.html` sonuçları tek sayfada, iki seviyede gösteriyor: LONG vs SHORT
      paneli ve tez gruplarına ayrılmış model kartları genel bakışta, modelin açık/kapanmış
      işlemleri `#model=<ad>` detayında; iki kabul kapısı ve band uyarısı her iki seviyede
      (`tests/test_report.py`).
- [x] `docs/positions.html` açık pozisyonları ve kapanmış işlemleri satır satır, iki
      katman ve tüm modeller için tek sayfada gösteriyor; kısmi çıkışlar görünür ama
      istatistiğe girmiyor (`tests/test_report.py`).
- [x] `scripts/telegram_report.py` günlük özeti yolluyor ve hatası turu düşürmüyor
      (`tests/test_telegram_report.py`).
- [x] `scripts/telegram_signals.py` scalp katmanının yeni sinyallerini anlık bildiriyor;
      yalnızca son barın sinyalleri gidiyor, aynı kurulum 4 bar susturuluyor, 5'ten fazla
      sinyal tek mesajda toplanıyor ve hatası turu düşürmüyor
      (`tests/test_telegram_signals.py`).

Bu çıta taslaktır, onay/düzeltme bekliyor.
