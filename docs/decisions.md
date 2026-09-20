# Tasarım Kararları

Bu dosya, projenin ilerleyişi sırasında alınan önemli tasarım kararlarını ve gerekçelerini
kayıt altına alır.

## 1. Proje sözleşmesi ve klasör iskeleti

`CLAUDE.md` ve boş klasör iskeleti oluşturuldu. Strateji arayüzü henüz onaylanmamıştı.

## 2. Strateji arayüzü onaylandı: açılış/kapanış ayrımı, doğrulama kapısı

- `entry_type` alanı `"market" | "limit"` olarak tanımlı ama v1'de yalnızca `"market"`
  işlenir; `"limit"` `core/validate.py`'de `NotImplementedError` fırlatır. Limit emri
  desteklemek bekleyen emir/geçerlilik süresi/mum-içi dokunma kavramları gerektirir ve
  look-ahead hatası riski taşır — kapsam dışı bırakıldı, alan sözleşmede korundu.
- Kapanış/kısmi çıkış kararı ayrı bir metodla verilir: `Strategy.manage_positions(market,
  positions) -> list[ExitInstruction]`, varsayılan implementasyon boş liste döner.
  `generate_signals` yalnızca açılış önerir. Bu ayrım, engine'in sinyal niyetini tahmin
  etmesini gereksiz kılar.
- `trailing_atr` alanı `Signal`'a eklendi ama uygulaması strateji değil `core/engine.py`
  sorumluluğunda — tekrarlanan trailing mantığının 10 modelde ayrı ayrı yazılmasının önüne
  geçer.
- Tek hedefli `take_profit: float | None` yerine `take_profits: list[TakeProfit]`
  (`price`, `fraction`) kullanılır — kısmi çıkış yapan stratejiler için özel durum
  gerektirmez.
- `allowed_directions` ihlali veya geçersiz sinyal geometrisi sessizce filtrelenmez,
  `ValueError`/`NotImplementedError` fırlatılır — bu doğrulama `core/validate.py`'de tek
  yerde toplanır (stop==entry sıfıra bölme, stop yanlış taraf, TP yönü tutarsızlığı,
  fraction toplamı > 1.0, evren dışı sembol). Sessiz filtreleme bir modelin performansını
  fark edilmeden bozabileceği için "ölçüm projesi" ilkesiyle çelişir.

`strategies/base.py` ve `core/validate.py` bu kararlarla uygulandı, testler
`tests/test_base.py` ve `tests/test_validate.py`'de.

## 3. config.yaml şeması netleşti, veri katmanı (OKX) uygulandı

- **`config.yaml` CLAUDE.md şemasına göre yeniden yazıldı.** Eski taslaktaki
  `commission_rate: 0.0004` ve `account`/`universe`/`schedule` grupları kaldırıldı; tek
  kaynak artık düz anahtarlardan oluşuyor. Komisyon `fee_rate: 0.001` (OKX **taker**):
  modeller `entry_type="market"` ile giriyor, maker oranı kullanmak hiç gerçekleşmeyen bir
  dolumu varsayıp tüm sonuçları optimist gösterirdi.
- **`core/config.py` eklendi** (CLAUDE.md modül tablosuna da yazıldı): config'in tek okuma
  kapısı, hiçbir varsayılan değer taşımaz, eksik anahtarda `ConfigError` fırlatır. Sessiz
  varsayılan, modellerin farklı maliyet/risk varsayımlarıyla yarışması demektir. Loader'ın
  ayrı modül olması, ayara ihtiyaç duyan her modülü `core/data.py`'ye bağlamamak içindir.
- **`Signal.take_profits` / `Position.take_profits` `tuple`'a çevrildi.** `frozen=True` bir
  dataclass içinde mutable liste taşımak dondurmayı yarım bırakıyordu: bir strateji ya da
  `peer_signals` okuyan bir meta model, sinyalin hedeflerini yerinde değiştirebilirdi.
- **`strategies/base.py` sözleşmenin tamamına getirildi**: `MarketData.funding` zaman
  indeksli seri, `MarketData.as_of`, `Strategy.is_meta` ve `generate_signals(..., peer_signals)`
  eklendi — `core/data.py`'nin ürettiği anlık görüntü ancak bu alanlarla tanımlı.
- **Veri kaynağı OKX public API v5** (auth gerekmez): `/public/instruments` + `/market/tickers`
  ile evren, `/market/candles` + `/market/history-candles` ile barlar,
  `/public/funding-rate-history` ile funding. Evren 24s **cirosuna** göre sıralanır
  (`volCcy24h × last`; SWAP'te `volCcy24h` base cinsindendir, sembolleri karşılaştırılabilir
  kılmak için fiyatla çarpılır), eşitlikte alfabetik — aynı veri aynı evreni vermeli.
- **Look-ahead (kural 12) iki katmanlı savunmayla uygulandı**: OKX mumları ters kronolojik
  döner ve ilk satır genelde kapanmamış bardır (`confirm="0"`). (1) `confirm != "1"` olan bar
  atılır, (2) kapanış zamanı `now`'ı aşan bar da atılır (confirm taşımayan yanıtlara karşı).
  `as_of` ayakta kalan son kapanmış barın zamanıdır.
- **`as_of` sembollerin ortak son barıydı (minimum);** bkz. karar 4 — bu yaklaşım BTC
  çıpasıyla değiştirildi.
- **Önbellek**: sembol başına parquet (`data/cache/`), her koşuda yalnızca eksik barlar çekilir
  (önbellekteki son bara ulaşıldığında sayfalama durur). Sayfalama `after` imleciyle yeniye
  doğru değil eskiye doğru ilerler; imleç ilerlemezse döngü kırılır. Rate limit (HTTP 429 /
  OKX `50011`) ve geçici 5xx hataları exponential backoff ile yeniden denenir, jitter
  `random_seed`'den beslenir. Kalıcı hata `OKXError` olur ve tek sembolün hatası turu düşürmez.
- **Yeni bağımlılık: `pyarrow`** — parquet önbelleği için gerekli (CLAUDE.md "harici bağımlılık
  minimumda" kuralı gereği gerekçe: mumların sütunlu ve tipli saklanması, CSV'ye göre hem
  tekrar okuma maliyetini hem tip kaybı riskini düşürür). CI kurulumuna eklendi.
- **Manuel doğrulama aracı**: `scripts/manual_data_check.py` 3 sembol için veriyi çeker, son
  barları ve funding'i yazdırır, son barın gerçekten kapanmış olduğunu ekrana basar.

## 4. `as_of` sembollerin ortağı değil, BTC çıpasıdır

Karar 3'teki "ortak (minimum) son kapanmış bar" tanımı iki sorun taşıyordu:

- **Döngüsel bağımlılık:** bayat sembolü dışlamak için önce `as_of`'a, `as_of`'u hesaplamak
  için önce sembol listesine ihtiyaç vardı. `max_staleness_bars` bu döngüyü "en yeni bara
  göre tolerans" diye kırıyordu, ama tanım kendi kendine dayanıyordu.
- **Tek gecikmiş sembol bütün turu geri çekiyordu:** tolerans içinde kalan tek bir sembol
  `as_of`'u bir bar (4 saat) geriye taşıyabiliyordu. O bar zaten işlenmişse sonuç ya çift
  işlem ya da hiç ilerlemeyen bir tur olurdu — ikisi de ölçümü geçersiz kılar.

**Yeni kural:** `as_of`, `exchange.btc_reference` (BTC-USDT-SWAP) sembolünün son kapanmış
barıdır. BTC zaten her modelin rejim filtresinde referans, hem de evrenin en likit ve en az
gecikecek sembolü; çıpa olarak sabit ve dışarıdan denetlenebilir bir tanım verir.

- `as_of` barına sahip olmayan semboller (geç kalan, durdurulan, serisinde boşluk olan) o tur
  **dışlanır** ve `logger.warning` ile gerekçesi yazılır; ayrıca her tur tek satırlık bir
  `logger.info` özeti (`as_of`, görülebilen sembol sayısı, dışlananların listesi) düşer —
  hangi turda modellerin kaç sembol görebildiği sonradan denetlenebilsin diye.
- BTC'den **ileride** olan semboller dışlanmaz, `as_of`'a kırpılır: çıpa geri gittiğinde tur
  yine tek bir "şimdi" görür (kural 5).
- `data.max_staleness_bars` anlamını değiştirdi: artık sembol başına tolerans değil, **çıpanın
  kendi tazelik sınırı.** BTC verisi beklenen bardan bu kadar barlık gecikmeyi aşarsa
  `OKXError` fırlatılır ve anlık görüntü hiç üretilmez — veri kesintisinde eski bir barı
  yeniymiş gibi işlemek, atlanan bir turdan daha pahalıdır.


## 5. Motor, portföy, defter ve funding uygulandı

`core/ledger.py`, `core/portfolio.py`, `core/funding.py` ve `core/engine.py` iskeletten
çıkarıldı. Alınan kararlar ve gerekçeleri:

### Defter (`core/ledger.py`)

- Model başına `ledgers/<model>/` altında üç dosya: `positions.json` (koşular arası taşınan
  durum — nakit, açık pozisyonlar, bekleyen emirler, işlenmiş son bar), `trades.csv`
  (kapanan her işlem/kısmi çıkış) ve `equity.csv` (bar başına bakiye görüntüsü).
- **Her yazma atomiktir**: geçici dosya + `fsync` + `os.replace`. Cron adımının ortasında
  düşen bir koşu yarım yazılmış defter bırakırsa, sonraki koşu yanlış bakiyeyle devam eder
  ve ölçüm sessizce bozulur. Append'ler de aynı yoldan gider; mevcut içerik bayt bayt
  korunduğu için "append-only" bir kural değil dosya sözleşmesidir.
- **Şema kayması reddedilir**: CSV başlığı beklenenden farklıysa `LedgerError`. Eski satırlar
  yeni kolonlarla karışırsa denetim izi sessizce anlamsızlaşır.
- `initialize_model` mevcut deftere dokunmaz, `reset_model` sıfırdan başlatır: yarışmaya yeni
  eklenen bir modelin defteri kendiliğinden boş açılır, mevcut bir modelinki elle sıfırlanır.

### Boyutlandırma ve marj (`core/portfolio.py`)

- **Sermaye tanımı**: kural 11'deki "sermaye" hesabın toplam özsermayesidir (nakit + marj +
  gerçekleşmemiş PnL), yani riske atılan %1 her zaman hesabın %1'idir. Kaldıraç tavanı ise
  eldeki **nakit** üzerinden uygulanır (marj nakitten fazla olamaz); bu, hesabın toplam
  notional'ını da `leverage_cap × özsermaye` sınırında tutar.
- **Kaldıraç bir sonuçtur**: yalnızca gereken notional nakdi aşarsa devreye girer. Tavana
  takılan işlem atlanmaz, tavana sığacak şekilde küçültülür ve gerekçe `trades.csv`'nin
  `notes` kolonuna yazılır (kural 11). Marj + komisyon nakdi aşarsa aynı doğrusal ölçekle
  ikinci bir kırpma yapılır — yine atlama değil.
- **Likidasyon fiyatı** giriş notional'ı üzerinden hesaplanır: `entry × (1 ± mm) ∓ marj/adet`.
  Mum içi mark fiyatına göre yeniden hesaplamak her barda farklı bir eşik üretir ve aynı
  senaryo iki koşuda farklı sonuç verebilirdi; tekrarlanabilirlik önce gelir. Kısmi çıkışta
  marj ve miktar orantılı azaldığı için `marj/adet` — dolayısıyla likidasyon fiyatı — sabit
  kalır.
- **Nakit muhasebesi**: açılışta `nakit -= marj + komisyon`, açıkken funding doğrudan nakde
  işler, kapanışta `nakit += marj + brüt PnL - komisyon`, likidasyonda hiçbir şey dönmez
  (marjın tamamı gider). `Trade.pnl` işlemin nakde net etkisidir; kapanan işlemlerin `pnl`
  toplamı bakiyedeki toplam değişime eşittir (test edilir).
- **Kayma yorumu**: config yalnızca iki kayma sabiti tanımlar. `slippage_long` her dolumun
  **taban** kayması olarak, `slippage_short_stop` ise short stop dolumlarının özel hâli
  olarak uygulanır. Short girişlere/TP'lere kayma uygulamamak, tam da ölçtüğümüz long/short
  farkını shortlar lehine bozardı; tek asimetri config'in açıkça istediğidir.
- **Stop boşluklu barda açılıştan dolar** (stop ile açılıştan aleyhte olanı seçilir); TP ise
  kendi fiyatından dolar ve lehte boşluk kâr yazılmaz — iyimser varsayımdan kaçınılır.
- Aynı sembolde aynı yönde ikinci pozisyon açılmaz; ters yön ayrı bir pozisyondur.
  `max_positions` ve `max_short_positions` açılış anında kontrol edilir.

### Funding (`core/funding.py`)

- Kural tek cümle: **pozitif funding'de long öder, short alır**; tutar = oran × notional.
  Modül maliyeti hesaplar, nakde `core/portfolio.py` işler (kural 2/7).
- Oranlar **uydurulmaz**: yalnızca tam zaman eşleşmesi kabul edilir, kayıt yoksa o periyot
  atlanır ve loglanır. `ffill` yapmak veri boşluğunu sentetik maliyete çevirirdi.
- Notional barın **açılış** fiyatından hesaplanır (mum içi bir fiyat seçmek look-ahead
  olurdu); funding anında henüz açılmamış pozisyon o periyodu ödemez.

### Tur akışı (`core/engine.py`)

- **Bekleyen emir kuyruğu**: kural 13 sinyalin bir sonraki barın açılışında dolmasını şart
  koşar; tur `as_of` barında bittiği için dolum bir sonraki turun ilk barıdır. Emirler bu
  yüzden `positions.json` içinde koşular arası taşınır. `manage_positions` çıkışları da aynı
  kuyruğa girer ve açılışlardan önce doldurulur (aynı turda kapanıp yeniden açılan sembolde
  kotanın yapay olarak dolu görünmemesi için).
- **Zaman ızgarası BTC'nindir**: işlenecek barlar `market.btc.index` üzerinden, son işlenmiş
  bardan `as_of`'a kadar alınır — `as_of`'un tanımı da odur (karar 4). Bir tur birden fazla
  bar ilerletebilir (cron kaçırılmışsa), her bar tam olarak bir kez işlenir.
- **Aynı `as_of` ile ikinci koşu sinyal üretmez**: aksi hâlde elle tekrar ya da cron retry,
  aynı bar için ikinci bir pozisyon kuyruğa alırdı (çift işlem — karar 4'ün uyardığı hata).
- **Bar içi sıra**: funding → bekleyen emir dolumu → likidasyon/stop/TP → trailing → özsermaye
  satırı. Trailing stop kontrolden **sonra** güncellenir: barın high/low'una bakıp aynı barın
  stop'unu değiştirmek, o barın içinde geçmişe dönük karar vermek olurdu (kural 12).
- **Trailing stop** chandelier kuralıyla uygulanır (long: en yüksek zirve − ATR × kat) ve
  yalnızca sıkışır. ATR periyodu config'e eklendi (`trailing.atr_period: 14`): her modelin
  kendi periyodunu seçmesi, aynı `trailing_atr` değerinin modelden modele farklı stop
  mesafesi anlamına gelmesi demekti.
- **Hata izolasyonu** (kural 8): `generate_signals`/doğrulama ya da `manage_positions` hata
  fırlatırsa yalnızca o model o turu boş geçer, gerekçe loglanır ve `RoundReport`a yazılır;
  koşu devam eder. Modelin mevcut pozisyonları yine core tarafından işlenir.
- **Doğrulama referans fiyatı** `as_of` barının kapanışıdır: gerçek dolum fiyatı (bir sonraki
  barın açılışı) sinyal anında bilinemez. Dolum stop'un ötesine düşerse emir açılışta
  reddedilir.
- Bir sonraki barda sembolü görülemeyen emir **iptal** edilir; gecikmeli dolum kural 13'ün
  tanımına uymaz. Barı olmayan sembolün açık pozisyonuna da dokunulmaz (elde olmayan mumla
  stop tetiklemek uydurma olurdu).

### Kapsam dışı bırakılanlar

- `core/metrics.py` hâlâ iskelet: `trades.csv` kolonları (yön, giriş/çıkış, komisyon,
  funding, PnL) long/short ayrıştırmasını besleyecek biçimde tasarlandı ama hesaplama ayrı
  bir adım.
- Turu başlatan bir çalıştırıcı (`main.py`/workflow adımı) henüz yok; `config.yaml > models`
  boş olduğu için çalıştıracak model de yok. İlk strateji eklendiğinde yazılacak.

## 6. Ortalama R birinci sınıf metrik; toplam getiri ikinci sırada

Karar 5'te sermaye tanımı "hesabın toplam özsermayesi (nakit + marj + gerçekleşmemiş PnL)"
olarak sabitlendi. Bu standart yaklaşım ama bir yan etkisi var: **açık kârı olan model daha
büyük risk alır.** Yani toplam getiri kısmen "model ne kadar hızlı bileşiklendi"yi ölçer,
sinyal kalitesini değil. Aynı sinyal kalitesine sahip iki modelden erken kâr yakalayanı
toplam getiride öne geçer — oysa ölçmek istediğimiz şey bu değil.

Bu yüzden `core/metrics.py`'de **işlem başına ortalama R** birinci sınıf metriktir:

```
R = işlemin net PnL'i / o işlemde AÇILIŞTA riske edilen tutar
```

- Payda `trades.csv`'nin **`risk_amount`** kolonudur (`adet × |giriş − ilk stop|`) ve deftere
  açılış anında yazılır. Türetilmiş olan R'nin kendisi deftere yazılmaz: defter olguları,
  `metrics.py` türetmeleri tutar — aynı hesabın iki yerde durması, iki yerde bozulması demek.
- **İlk stop** kullanılır, güncel stop değil: trailing stop sonradan kısaldığında R'nin
  tabanı değişirse aynı işlem sonradan daha başarılı görünürdü.
- Kaldıraç tavanına takılıp küçülen pozisyonda payda **gerçekten riske edilen** tutardır,
  modelin niyeti değil — model kırpma yüzünden ne ödüllendirilir ne cezalandırılır.
- Likidasyonda R −1'in altına iner (marjın tamamı gider). Bu bir hata değil, tam da ölçmek
  istediğimiz risk farkının görünür hâlidir; `liquidations` ayrıca sayılır.
- Karşılaştırma tablosu **ortalama R'ye göre sıralanır** ve kolon sırası bilinçlidir: önce R,
  sonra USDT getirisi. Toplam getiriye göre sıralamak, ayıklamaya çalıştığımız bileşiklenme
  etkisini geri sokardı. Toplam getiri atılmaz, ikinci sırada durur.

### Long/short ayrıştırmasının sınırı

İşlem-tabanlı her metrik (R, win-rate, profit factor, PnL) long ve short için ayrı hesaplanır.
Özsermaye eğrisinden gelenler (hesap getirisi, hesap max drawdown, hesap Sharpe) **yön bazında
ayrıştırılamaz** — hesapta tek bakiye vardır. Bunlar `AccountStats` altında açıkça hesap
düzeyi olarak raporlanır; yönlerin kendi risk profili bunun yerine o yönün **kümülatif R
eğrisinden** ölçülür (`r_sharpe`, `max_drawdown_r`).

### Tanımsız metrik `nan` döner

İşlem yoksa, varyans sıfırsa ya da kazanan işlem yoksa metrik `nan`'dır ve raporda `—` yazılır.
0.0 döndürmek "ölçüldü ve sıfır çıktı" ile "ölçülemedi"yi aynı sayıya indirger; 10 modelin
sıralandığı bir tabloda bu sessiz bir sıralama hatasıdır. `risk_amount`'ı olmayan satırlar da
R ortalamasına girmez, `unmeasured` olarak sayılır ve raporda uyarı satırı düşer.

### Gözlem: bir stop gerçekte 1R'den pahalıdır

İlk uçtan uca koşuda 1R'lik bir stop long'da **−1.04R**, short'ta **−1.16R** ile kapandı.
Fark maliyetlerden geliyor ve yapısal: komisyon notional ile ölçeklenir, risk ise stop
mesafesiyle. Dar stop kullanan bir model aynı 1R için daha büyük pozisyon taşır, dolayısıyla
R başına daha çok komisyon öder; short stop'ların kayması ayrıca 3 kat (`slippage_short_stop`).
Bu, **stop mesafesi tercihinin ölçüme sızdığı** anlamına gelir: modeller karşılaştırılırken
ortalama stop mesafesi de bakılması gereken bir değişkendir.

## 7. `slippage_base` adlandırması, R başına maliyet kolonları ve stop mesafesi bandı

Tetikleyen bulgu: **stop mesafesi aynı zamanda maliyet ölçeğidir.** Boyut
`risk / |giriş − stop|` olduğundan dar stop kuran model aynı 1R'yi daha büyük notional ile
taşır ve R başına daha çok komisyon+kayma öder. Bu, projenin ana sorusunu doğrudan kirletir:
"short modeller daha başarılı" sonucu sinyalden değil, yalnızca daha geniş stop kullanıp daha
az maliyet ödemelerinden geliyor olabilir. Ölçülmeden bırakılamaz.

- **`slippage_long` → `slippage_base` olarak yeniden adlandırıldı** (config.yaml, `core/config.py`,
  testler ve CLAUDE.md). Oran yöne bağlı değildi: short stop dolumu dışındaki **her** dolumda
  (long/short giriş, çıkış, kısmi TP) uygulanıyor. Eski ad, short girişlerin kaymasız dolduğu
  izlenimini veriyordu — yani tam da ölçtüğümüz long/short maliyet farkını yanlış anlatıyordu.
  `tests/test_config.py` eski adın geri sızmasını da test ediyor: iki anahtarın bir süre birlikte
  yaşaması, modellerin farklı maliyet varsayımlarıyla yarışması demek olurdu.
- **`core/metrics.py` iki maliyet kolonu raporlar** (model ve yön bazında):
  `avg_stop_distance_pct` ve `cost_per_r`. Böylece bir model öne çıktığında, farkın sinyalden mi
  maliyet avantajından mı geldiği ayrıştırılabilir. Tanımlar CLAUDE.md > "Rapor Kolonları"nda.
  - Payda **ilk stop**tur, trailing ile yürüyen stop değil: R giriş anında üstlenilen risktir.
    Yürüyen stop'u kullanmak iyi giden işlemlerin paydasını küçültür, `cost_per_r`'yi şişirir ve
    bu şişme yalnızca trailing kullanan modellerde olur — kıyası bozar.
  - `risk_amount` = `boyut × |giriş − ilk stop|`, yani **gerçekleşen** 1R. Boyutlandırma
    formülünün payı (`risk_per_trade × sermaye`) değil: kaldıraç tavanı boyutu küçülttüğünde
    (kural 11) gerçek risk de küçülür ve payı kullanmak o işlemlerin maliyetini olduğundan düşük
    gösterirdi.
  - **Veri yoksa `nan`, `0.0` değil.** `0.0` "işlem yaptı, tam sıfır çıktı" demektir; hiç short
    açmamış bir modeli "maliyetsiz short yapan model" gibi göstermek ortalamaları bozar.
  - **Yön bazlı Sharpe tek bakiyeyi bölerek üretilmez.** Hesap tektir (ortak margin, ortak
    funding, ortak nakit); bakiye eğrisini zorla ikiye ayırmak uydurma sayı verir. Yön Sharpe'ı o
    yöndeki işlemlerin kapanış sırasına dizilmiş **R cinsinden** getiri dizisinden hesaplanır;
    hesap Sharpe'ı ayrı bir satır olarak gerçek bakiye eğrisinden gelir ve iki yönün toplamı
    ya da ortalaması değildir.
- **Stop mesafesi bandı kural 14 olarak yazıldı:** modeller kabaca **1×–2.5×ATR** bandında kalır
  (2× ile 2.5× kıyaslanabilir; 0.5× ile 3× kıyaslanamaz). Prompt 3-5'teki parametreler zaten bu
  bandda. Tek risk, stop'unu veriye bağlı kuran "failed breakout" modeli (fitil tepesinin üstü
  **veya** 1×ATR, hangisi genişse): uzun bir fitil bandın dışına taşabilir. Bu yüzden
  `max_stop_atr_multiple: 3.0` config'e eklendi ve mesafe tavanı aşarsa **işlem atlanır.**
  - Stop tavana çekilmez: bu, modelin "stop fitilin üstünde olmalı" tezini sessizce başka bir
    modele çevirirdi — atlamak yalnızca ölçülemeyen işlemi eler.
  - `core/validate.py` burada hata fırlatmaz; geniş stop programlama hatası değil (kural 8),
    karşılaştırılamaz bir piyasa durumudur.
  - Atlama `logger.info` ile kaydedilir. Kural 11'in "atlamak işlem sayısını sessizce düşürür"
    itirazı böyle karşılanır: düşüş denetlenebilir, bandın tutup tutmadığı ise
    `avg_stop_distance_pct` kolonundan okunur.

Not: `core/metrics.py` hâlâ stub. Yukarıdaki kolon tanımları sözleşmeye yazıldı; uygulaması
ledger kayıt şemasıyla birlikte (portfolio/ledger promptu) gelecek.

## 8. Karar 7'nin `core/` tarafı uygulandı

Karar 7 sözleşmeyi (config, CLAUDE.md) değiştirdi ama `core/` tarafı motor dalında duruyordu;
merge sırasında uygulandı:

- **`slippage_long` → `slippage_base`** yalnızca ad değişikliği: davranış zaten yönden bağımsızdı
  (karar 5), ad onu gizliyordu.
- **`slippage_cost` deftere eklendi.** `cost_per_r` komisyon **ve** kayma ister; kayma dolum
  fiyatının içine gömülü olduğu için ayrıca yazılmazsa maliyetin yarısı ölçülemez kalırdı. Giriş
  kayması pozisyonda USDT olarak taşınır ve kısmi çıkışlarda komisyon gibi orantılı dağıtılır.
  Likidasyonda çıkış kayması yazılmaz: pozisyonun değeri zaten sıfırlanmıştır, üstüne kayma
  eklemek kaybı marjın ötesine taşırdı.
- **Kural 14 `core/engine.py`'de bir filtre olarak uygulandı**, `core/validate.py`'de değil —
  kuralın kendisi bunu şart koşuyor: geniş stop programlama hatası değil, karşılaştırılamayacak
  bir piyasa durumu. Elenen her sinyal `logger.info` ile gerekçelenir ve `RoundReport`'ta
  `skipped_signals` olarak sayılır; kural 11'in "atlamak işlem sayısını sessizce düşürür"
  itirazı böyle karşılanır. Kural 8 ile karışmaz: model atlanmaz, yalnızca o sinyal düşer.
- **ATR doğrulanamıyorsa sinyal elenmez, uyarı yazılır.** Tavanın aşılıp aşılmadığı
  bilinmiyorken işlemi atmak, ölçülemeyen bir nedenle işlem sayısını düşürürdü; bandın gerçekten
  tutup tutmadığı zaten sonradan `avg_stop_distance_pct` kolonundan denetlenir. Kural 14'ün ATR'si
  `trailing.atr_period`'dur — proje tek bir ATR tanımı taşır, yoksa "3×ATR" iki farklı mesafe
  demeye başlardı.
- **`cost_per_r` ve `avg_stop_distance_pct` `DirectionStats`e eklendi** ve tabloda getiri
  kolonlarından ÖNCE durur. Yön bazlı R dizisi kapanış zamanına göre sıralanır: defter zaten bu
  sırada yazılır, sıralama garantiyi satırlar başka yoldan gelse de gerçek kılar.

## 9. İlk iki yarışmacı model ve ortak gösterge modülü

`buyhold` çıpasının üstüne ilk iki YARIŞMACI eklendi: `trend` (Donchian kırılımı + EMA rejim
filtresi) ve `meanrev` (RSI + Bollinger ortalamaya dönüş). Boru hattı (veri → motor → portföy →
defter → metrikler) değiştirilmedi; modeller yalnızca `Signal` üretir.

### Göstergeler tek modülde: `core/indicators.py`

Her strateji kendi RSI/ATR/EMA'sını yazsaydı, iki model aynı barda **farklı sayı** görürdü — bu
kural 5'in ("tüm stratejiler aynı anlık görüntüyü görür") sayısal düzeyde delinmesidir ve sinyal
farkını gösterge farkıyla karıştırır. Bu yüzden gösterge matematiği stratejilerden çıkarıldı.

`average_true_range` **taşındı**, kopyalanmadı: `core/engine.py` artık onu indicators'tan import
edip yeniden dışa veriyor. İkinci bir ATR uygulaması, trailing mesafesi (kural 9), stop bandı
(kural 14) ve stratejinin stop'unun üç ayrı sayıya bölünmesi demekti. Dış davranış aynı; mevcut
testler değiştirilmeden geçiyor.

Tanım kararları ve gerekçeleri:

- **Pencere-yerel ortalamalar (ATR, RSI) Wilder yumuşatması kullanmaz.** Wilder özyinelemelidir,
  yani sonuç çerçeveye kaç bar geçmiş verildiğine bağlıdır: önbellek farklı ısındığında ya da
  sembol geç listelendiğinde aynı bar için farklı sayı çıkar. Basit ortalama yalnızca son
  `period` bara bakar — aynı bar her zaman aynı sayıyı verir.
- **EMA'da bu kaçış yok** (tanımı gereği özyinelemeli), o yüzden tohum açıkça ilk `period` barın
  SMA'sına sabitlendi; pandas `ewm` varsayılanına bırakılsaydı tanım kodda görünmez olurdu.
- **Donchian son barı HARİÇ tutar.** Kanal değerlendirilen barı da içerseydi `upper >= high >= close`
  olurdu ve "tepenin üstüne kapanış" yalnızca kapanış tam zirveye eşitken sağlanabilirdi — kırılım
  pratikte hiç oluşmazdı. Kanal, kırılımı ölçülen barın GEÇMİŞİDİR.
- **Bollinger sapması popülasyon sapmasıdır (ddof=0).** Hangisinin "doğru" olduğu değil, tek ve
  yazılı olması önemli: ddof=1, 20 barlık pencerede bandı ~%2.6 genişletir.
- **Yeterli bar yoksa `None` döner**, kısmi pencereyle hesaplanmış bir sayı değil. Yeni listelenmiş
  bir sembolde 30 barla "200 EMA" üretmek, rejim filtresini o sembolde sessizce anlamsız kılardı.

### `strategies/trend.py`

Kırılım (20 bar Donchian) **ve** rejim (50/200 EMA) kapılarının ikisi de aranır; stop 2×ATR(14),
trailing 1×ATR. Rejim filtresinin işlevi kârı artırmak değil, **ölçülen şeyi daraltmaktır**:
filtresiz bir kırılım modeli yatay piyasada iki yönde de tetiklenir ve sonuç "kırılım işe yarıyor
mu" sorusuna değil "hangi rejimde ne kadar testere yedik" sorusuna cevap verir.

Stop neden sabit 2×ATR: stop mesafesi aynı zamanda maliyet ölçeğidir (kural 14) ve 2×, 1×–2.5×ATR
bandının ortasıdır — `max_stop_atr_multiple` (3.0) tavanına takılmaz, yani bu model tavan yüzünden
sessizce işlem kaybetmez.

### `strategies/meanrev.py`

RSI(14) eşiği **ve** Bollinger(20,2) kırılımı birlikte aranır; stop 2×ATR, tek TP = orta bant
(20 SMA), fraction 1.0. Hedef bir ATR katı değil göstergenin kendi orta bandıdır: modelin tezi
"ne kadar kazanacağım" değil "fiyat ortalamasına döner"dir, TP'yi tezden bağımsız bir sayıya
bağlamak ölçülen şeyi ortalamaya dönüş olmaktan çıkarırdı. Kısmi çıkış (kâr koruma) ayrı bir
tezdir, bu modele karıştırılmadı — aynı nedenle trailing de istenmiyor.

**Short'un BTC rejim kapısı** modelin ölçüm açısından en kritik kuralı: short sinyali yalnızca BTC
4h kapanışı kendi 200 EMA'sının altındayken geçerli. Altcoin'lerin çoğu BTC ile yüksek
korelasyonlu olduğundan boğa rejiminde "aşırı alım" sürekli tetiklenir; filtresiz bir short kolu
sinyal kalitesini değil **rejim yönünü** ölçer. Projenin ana sorusu "short işlemler long'lardan
daha mı başarılı" olduğuna göre bu, cevabı baştan kirletirdi.

Kapı BTC'ye bağlı çünkü `as_of` çıpası da BTC (karar 4): her model aynı barı "şimdi" sayar,
dolayısıyla filtre tüm semboller için aynı anda açılıp kapanır. Sembol başına ayrı rejim tanımı,
aynı turda birbiriyle çelişen kapılar üretirdi.

BTC'nin 200 EMA'sı hesaplanamıyorsa (yeterli bar yok) kapı **kapalı** kalır. Açık varsaymak,
filtrenin var olmadığı bir dönemde short açmak — modeli sessizce başka bir modele çevirmek —
olurdu; kapalı varsaymanın bedeli yalnızca eksik işlemdir ve o `logger.info` ile kaydedilir.

### `reason` alanı bir denetim aracıdır, süs değil

İki model de gerekçeyi **ölçüm ve eşiği yan yana** yazar ("RSI(14) 24 (<30); alt Bollinger(20,2)
90.28 bandının %1.80 altında kapanış (88.66); stop 2×ATR(14)=6.71 uzakta (81.95); TP orta bant
99"). Yalnızca ölçümü yazmak, sonradan "eşik neydi" sorusunu commit geçmişinde aramaya zorlardı.
Alan `Position` üzerinden kapanan işlemin `signal_reason` kolonuna taşınır: "model neden bu işlemi
yaptı" denetimi defterden, koşuyu tekrar üretmeye gerek kalmadan yapılabilir.

### Atlamalar sessiz değil

ATR üretilemediği için düşen sinyal `logger.info` ile kaydedilir (kural 14'ün gerekçesi burada da
geçerli: ölçülemeyen bir nedenle düşen işlem sayısı denetlenebilir olmalı). `as_of` barını
taşımayan sembol sinyal üretmez — `core/data.py` bunları zaten dışlar, ama bir bar geriden sinyal
üretmek look-ahead kadar sessiz bir ölçüm hatasıdır.

### Yan etki: `tests/test_main.py` config'e bağlandı

`models` listesi büyüdüğü için `payload["models"]` artık literal `["buyhold"]` ile değil
`load_config()["models"]` ile karşılaştırılıyor. Testin ölçtüğü şey "hangi modeller var" değil,
"main config'in TAMAMINI koşturdu mu" — 10 model hedefiyle bu ayrım kalıcı.

## 10. İki yarışmacı daha: `momentum` ve `squeeze` (`confluence` beklemede)

Yarışmacı sayısı 2'den 4'e çıktı: `momentum` (kesitsel momentum, haftalık dengeleme) ve
`squeeze` (Bollinger sıkışması + hacim teyitli kırılım). Boru hattına (veri → motor → portföy →
defter → metrikler) dokunulmadı; iki model de yalnızca `Signal` üretir, göstergelerini
`core/indicators.py`'den okur ve kendi boyut/komisyon/bakiye hesabı yapmaz (kural 1/2/3/7).

### `strategies/momentum.py`

Evren her dengelemede 7 günlük getiriye göre sıralanır; ilk 5 long, son 5 short, stop 2.5×ATR(14).
Model mutlak bir yön iddiası taşımaz — aynı anda iki yönde de açar; ölçtüğü şey **sıralamanın
uçlarıdır.**

- **Dengeleme yalnızca Pazartesi 00:00 UTC barında.** Sıralama her turda (4 saatte bir) yeniden
  hesaplansaydı model haftada 42 kez dengelerdi; sıra değiştiren her küçük oynama bir işlem açar
  ve sonuç "momentum işe yarıyor mu" sorusuna değil "komisyon+kayma ne kadar yiyor" sorusuna
  cevap verirdi. Sabit tek bar, işlem sayısını tezin zaman ölçeğine (haftalık) bağlar ve hangi
  turda dengelendiği sonradan `as_of`'tan okunur. Diğer turlarda sinyal üretilmez; açık
  pozisyonların stop/trailing takibi motorun işidir (kural 9).
- **Geriye bakış GÜN cinsinden tanımlı, bar cinsinden değil.** `42` gibi bir sabit, `timeframe`
  değiştiğinde tezi sessizce başka bir teze çevirirdi; bar sayısı config'in `timeframe`'inden
  türetilir.
- **Uçlar örtüşürse tur atlanır.** Sıralanabilen sembol sayısı 10'un altına düşerse aynı sembol
  hem long hem short listesine girerdi. Listeyi küçültmek (ilk 2 / son 2) modeli sessizce başka
  bir modele çevirir; atlamak yalnızca ölçülemeyen turu ölçüyormuş gibi göstermez ve loglanır.
- **Eşitlikte sıra sembol adına göre.** Sözlük/veri katmanı sırasına bırakmak, koşuları
  tekrarlanamaz kılardı (`random_seed`'in aynı gerekçesi).
- Stop **2.5×ATR**, yani 1×–2.5×ATR bandının üst ucu: haftalık tutulan bir pozisyonun 4 saatlik
  gürültüye takılmaması için bilinçli olarak geniş, ama `max_stop_atr_multiple` (3.0) tavanının
  altında — model tavan yüzünden sessizce işlem kaybetmez.
- Kısmi pencereyle getiri hesaplanmaz: 43 barı olmayan sembol sıralamaya hiç girmez. Aksi hâlde
  3 günlük getiri 7 günlük sanılır ve iki farklı uzunluk aynı kolonda yarışırdı.

### `strategies/squeeze.py`

Bant genişliği son 50 barın en dar %20'sindeyken banda kapanış + hacim teyidi; yukarı kırılım
long, aşağı kırılım short. Stop, sıkışma aralığının karşı ucu.

- **Hacim teyidi (20 bar ortalamasının 1.5 katı) opsiyonel bir ek değil, modelin ana filtresidir.**
  Sıkışmadan çıkan kapanışların çoğu sahte kırılımdır; teyitsiz bir koşul modeli "dar banttan
  çıkışları say" ölçümüne indirger ve tezin kendisi (hacmin katılımı) hiç test edilmemiş olur.
  Teyit yoksa **işlem yok**, ve bastırılan her kırılım loglanır — filtrenin kaç işlemi elediği
  denetlenebilir olmalı.
- **Bantlar ve hacim ortalaması kırılım barını HARİÇ tutar.** Gerekçe Donchian'ınkiyle birebir
  aynı (karar 9): kırılım barının kendi hareketi aynı barın standart sapmasını, kendi hacmi de
  20 barlık ortalamayı şişirir — yani en güçlü kırılımlar kendi kendini elerdi. İki ölçü de
  kırılım barının GEÇMİŞİNDEN alınır; böylece "sıkışma" ile "kırılım" iki ayrı bara oturur ve
  tanım denetlenebilir olur.
- **Genişlik orta banda bölünür (yüzdesel).** Mutlak genişlik fiyat seviyesiyle ölçeklenir;
  aynı sembolün altı ay önceki bandıyla bugünküsü kıyaslanamaz hâle gelirdi.
- **Stop tezle aynı yerde: sıkışma aralığının karşı ucu.** Kırılım geçersizse fiyat aralığın
  içine döner ve diğer uca ulaşır. Bu mesafeyi veriye bağlar, dolayısıyla `max_stop_atr_multiple`
  burada bir **tavandır** (kural 14): mesafe tavanı aşarsa işlem **atlanır**, stop tavana
  çekilmez — çekmek modelin tezini sessizce başka bir modele çevirirdi. Motor aynı tavanı ayrıca
  uygular; modelin kendi kapısı, atlamanın gerekçesini (hangi sıkışma, hangi mesafe) modelin
  diliyle loglamak içindir.
- Sıkışma aralığı kapanışı içeriyorsa (stop girişin yanlış tarafında) sinyal üretilmez ve
  loglanır: bu bir programlama hatası değil veri durumudur, `core/validate.py`'ye taşınsaydı
  `ValueError` ile TÜM modeli düşürürdü (kural 8 ile karıştırılmamalı).

### `tests/helpers_market.py` genişletildi

`frame()` artık `volumes` ve `start` alıyor. Hacim teyidini ölçen bir model sabit 1.0 hacimle
test edilemez; dengeleme barına duyarlı bir model de takvim günü sabitlenmeden test edilemez.
Kurucuyu kopyalamak yerine ortak dosyaya eklendi — iki testin farklı bar aralığı kullanması,
"aynı veriyi gördüler" varsayımını testlerde bile bozardı.

### `confluence` bu turda yazılmadı

Model, mevcut crypto-scanner reposundaki zigzag pivot mantığına dayanıyor ve o kod bu depoda yok.
İkinci bir zigzag uygulaması yazmak, karar 9'un ("gösterge matematiği tek yerde") tam tersi
olurdu: aynı pivot iki farklı tanımla iki farklı sayı üretir ve modelin ölçtüğü şeyin kaynak
modelle aynı olduğu iddiası kanıtlanamaz hâle gelir. Kaynak dosyalar yapıştırılana kadar
`confluence` ne `strategies/registry.py`'ye ne de `config.yaml`'ın `models` listesine eklendi —
kayıtlı olmayan bir ad koşuyu hata koduyla düşürür (`main.py`), yarı yazılmış bir model ise
sessizce eksik yarışırdı.

## 11. `confluence`: kaynak tarayıcıdan taşıma, birebir pivot, bilinçli sapmalar

`confluence`, crypto-scanner deposundaki ana gate'in (`find_confluence_candidates` +
`evaluate_confluence_entry`) bu projeye taşınmış hâlidir. Tez: fiyatın kendi yapısından çıkan
iki bağımsız swing'in Fibonacci seviyeleri aynı fiyatta çakışıyorsa (büyük dalganın
0.618/0.786 retracement'i, küçük ABC bacağının 1.272/1.618 extension'ı) orası tek bir
seviyeden güçlü bir dönüş bölgesidir; yönü RSI belirler (<35 long, >65 short, arası işlem yok).

### Pivot matematiği kopyalanmadı, TAŞINDI

`find_zigzag_pivots`, `_merge_short_legs` ve `compute_fib_levels` `core/indicators.py`'ye
birebir taşındı (eşik karşılaştırmaları, canlı uç davranışı, çift silme mantığı dâhil).
Yeniden yazmak, karar 9'un ("aynı göstergenin iki uygulaması iki farklı sayı demektir")
tam tersi olurdu — üstelik burada ikinci uygulama, modelin ölçtüğü şeyin kaynak tarayıcıyla
aynı olduğu iddiasını da kanıtlanamaz kılardı.

Taşımanın doğruluğu **kaynağa karşı** doğrulandı: 600 rastgele çerçeve × 3 parametre setinde
pivot listeleri (zaman, fiyat, tip) birebir aynı; 300 çerçevede büyük dalga/küçük bacak seçimi,
seçilen oranlar, seviye fiyatları, mesafeler ve `within_tolerance` kararı birebir aynı.

Look-ahead (kural 12) açısından temiz: fonksiyonlar yalnızca verilen çerçeveyi okur, çağıran
taraf çerçeveyi `bars_until` ile `as_of`'ta keser. Kaynaktaki "henüz teyit edilmemiş canlı uç"
pivotu KORUNDU: bir sonraki barda yer değiştirebilir ama geleceği görmez; atmak swing'in
güncel ucunu tümden kaybettirirdi.

### Kaynaktan bilinçli sapmalar

- **Confidence kademeleri (low/medium/high) ve 0.5R/1.0R/1.5R çarpanı taşınmadı.** Kaynakta
  confidence'ın TEK işlevi pozisyon boyutunu çarpmaktı. Bu projede boyutlandırma stratejinin
  işi değildir (kural 3/11): çarpanı taşımak ortak risk birimini (1R) modele göre değiştirir
  ve tabloyu kıyaslanamaz kılardı. Çarpan olmadan confidence hiçbir ölçülen büyüklüğü
  etkilemez; yalnızca `reason` metnini süslemek için ~300 satırlık Double Bottom/Top + RSI
  diverjans + Wyckoff katmanını taşımak, ölçüm değeri olmayan bakım yükü olurdu.
  **Kademeler ölçülmek isteniyorsa doğru yol ayrı model satırlarıdır** (ör. `confluence` ve
  yalnızca "tam teyitli" kurulumları alan `confluence_confirmed`): o zaman kademe farkı
  tabloda iki satır olarak, aynı risk biriminde yarışır — ki bu projenin cevap verebildiği
  soru biçimi tam olarak budur.
- **RSI ve ATR `core/indicators.py`'den okunur.** Kaynak Wilder yumuşatması kullanıyor
  (`ewm(alpha=1/period)`); bu depo onu açıkça reddediyor (karar 9: özyineleme, sonucu çerçeveye
  kaç bar geçmiş verildiğine bağlı kılar). Eşikler (35/65, ATR periyodu 14) aynı; sayılar
  kaynakla birebir aynı çıkmaz. Bu, ölçümün tekrarlanabilirliği uğruna kabul edilmiş bir farktır.
- **Stop 2.5×ATR, kaynaktaki 3.0×ATR değil.** 3.0 bu projede `max_stop_atr_multiple`
  TAVANININ kendisidir (kural 14). Dolum bir sonraki barın açılışında olduğundan (kural 13)
  motorun ölçtüğü mesafe girişin kapanışına göre hesaplanandan farklıdır ve tavanın hemen
  üstüne çıkabilir: model, sinyal üretip sessizce elenen işlemlerle ölçülemez hâle gelirdi.
  2.5, bandın (1×–2.5×ATR) üst ucudur ve `momentum` ile aynı ölçektedir.
- **Telegram/state/cooldown/CSV ve likidite-stablecoin filtreleri taşınmadı**: ilki raporlama
  katmanı (burada defter ve `docs/data/metrics.json` var), ikincisi evren katmanının işi
  (`core/data.py` zaten hacme göre ilk 50 USDT perpetual'ı seçiyor).

### Kapının kaç kez yönsüz açıldığı loglanır

Confluence toleransı geçip RSI'ın nötr bölgede kaldığı durum `logger.info` ile kaydedilir.
Sessiz geçmek, eşiklerin (35/65) gerçekten mi yoksa geometrinin mi eleme yaptığını sonradan
ayırt edilemez kılardı.

## 12. Confluence confidence kademesi: hesaplanır, yalnızca deftere yazılır

Karar 11'de kademe hiç taşınmamıştı. Kullanıcı kararıyla üçüncü yol seçildi: **kademe
hesaplanır ama hiçbir kararı etkilemez**; `reason`ın sonuna ayrıştırılabilir biçimde
`| confidence=low|medium|high` olarak eklenir ve `trades.csv`in `signal_reason` kolonundan
gruplanarak sonradan incelenir. Giriş kararı, yön, stop mesafesi, pozisyon boyutu ve ölçüm
tablosu bundan **etkilenmez.**

Ayrı model açılmadı (`confluence_confirmed`): teyitli kurulumlar `confluence`ın ALT KÜMESİ
olduğu için iki satır büyük ölçüde aynı işlemleri taşır; korelasyon matrisinde bağımsız bir
model gibi görünüp 10 modelin birini gereksiz tüketirdi.

### Sınır tek bir dosyada duruyor

Hesap `strategies/confluence_confidence.py`'de; `strategies/confluence.py` oradan yalnızca
bir string alır. "Kademe hiçbir şeyi etkilemiyor" iddiası böylece tek yerden denetlenebilir.
İki koruma test altında:

- Kademe zorla değiştirildiğinde sinyalin `stop_price`, `direction`, `sizing`, `take_profits`
  alanları ve `reason`ın geri kalanı **birebir aynı** kalır.
- Kademe katmanı istisna fırlatırsa sinyal **değişmeden** üretilir, etiket `unknown` olur ve
  `logger.warning` düşer. Süs amaçlı bir katmanın ölçümü düşürmesi kabul edilemez.

Etiket serbest cümlenin içine gömülmez (` | confidence=high` olarak sonda durur): gömülü bir
etiket, defterden gruplama için metin ayrıştırmak zorunda bırakırdı.

### Taşınan kod ve kaynağa karşı doğrulama

Kademe tanımı kaynaktaki `evaluate_confluence_entry` ile aynıdır: `high` = yapı var ve tam
teyitli, `medium` = yalnızca yapı var, `low` = yapı yok. "Tam teyit" kaynaktaki üç kapıdır
(çift dip/tepe + kırılım/hacim, RSI diverjansı, fiyatın 0.618-0.786 bandında olması).
Kaynağın Wyckoff/Elliott/Motor-1 katmanları taşınmadı: onlar `is_valid`i değil yalnızca
kaynağın kendi confidence/bonus alanlarını etkiliyordu — bu kademede karşılıkları yok.

`signal_validation.py`'ye karşı 800 karşılaştırmada (400 rastgele çerçeve × 2 yön):

| Katman | Sonuç |
|---|---|
| Yapı var mı (`structure_present`) | 800/800 aynı |
| Kırılım + hacim teyidi | 800/800 aynı |
| Tam teyit (`is_valid`) ve kademe | 799/800 aynı |

Tek fark RSI tanımından geliyor ve beklenen bir farktır (karar 9/11): aynı çift dipte Wilder
RSI'ı 31.0 -> 34.1 (fark 3.1, "en az 5 puan" eşiğini geçmiyor), pencere-yerel RSI 23.0 -> 40.4
(fark 17.4, geçiyor). Bu fark **yalnızca etiketi** etkiler; hiçbir işlem, boyut veya metrik
ondan türemez.

### `core/indicators.py`'ye eklenenler

- **`rsi_series`**: RSI'ın bar bazlı hâli. Diverjans kontrolü "şimdiki" RSI'ı değil, iki
  PİVOT barının RSI'ını karşılaştırır. `rsi()` artık bu serinin son değeridir — iki ayrı
  hesap tutmak, aynı modelin pivotta okuduğu RSI ile eşik karşılaştırdığı RSI'ı farklı
  tanımlardan besleyecekti.
- **`local_lows` / `local_highs`**: kaynaktaki `find_local_lows`/`find_local_highs` (fraktal,
  `order=3`). Zigzag'dan **bilerek ayrı** bir pivot tanımıdır — kaynak da çift dip yapısını
  bununla arar; birini diğerinin yerine kullanmak kaynak modelin ölçtüğü yapıyı değiştirirdi.
  Son `order` bar hiçbir zaman pivot olamaz: bu bir gecikmedir, look-ahead değil.

---

## 13. Son dört model: `failed_breakout`, `downtrend_rally`, `avwap`, `random_ctrl`

Kayıtlı yarışmacı sayısı 9'a çıktı (buyhold hâlâ referans çıpası, kural 15). Dördünün de
göstergeleri `core/indicators.py`'den okunur; hiçbiri kendi RSI/ATR/EMA/pivot matematiğini
yazmaz (karar 9'un gerekçesi aynen geçerli).

### `strategies/failed_breakout.py` (short-only)

Tuzak kırılım: 20 bar zirvesini FİTİLLE süpüren, ardından 1-2 bar içinde aynı seviyenin
altına kapanan, hacmi kendi 20 bar ortalamasının altında kalan ve RSI ayı uyumsuzluğu taşıyan
kurulum.

- **Süpürme fitille, teyit kapanışla tanımlı.** Kırılımı kapanışla tanımlamak tuzağın
  kendisini eler ve model "başarılı kırılım" aramaya başlardı.
- **Teyit BİRİNCİ kapanışta aranır.** Aradaki barlardan biri zaten seviyenin altına
  kapanmışsa kurulum orada oluşmuştur; aksi hâlde aynı tuzak iki tur üst üste sinyal üretir,
  `core/portfolio.py` ikincisini `duplicate_position` ile reddeder ve modelin sinyal sayısı
  ölçülemeyen bir nedenle şişerdi.
- **Funding önceliği boyut değil SIRA.** Son 3 periyodun ortalaması pozitif ve yükseliyorsa
  sinyal listenin başına alınır. Boyutla ödüllendirmek kural 3/11'i delerdi; motor
  `max_positions` dolana kadar sinyalleri geldikleri sırada doldurduğu için sıra,
  boyutlandırmaya dokunmayan tek "öncelik" kanalıdır. Gerekçe `reason`a yazılır.
- **Stop fitil tepesi ya da 1×ATR — hangisi genişse; tavanı aşarsa işlem ATLANIR** (kural 14).
  ATR burada bir TABANDIR: dar fitilli bir süpürmede stop'u fitilin hemen üstüne koymak R'yi
  gürültü ölçeğine indirir ve R başına maliyeti diğer modellerle kıyaslanamaz yapardı.
- **Hedef önce son swing dip, yoksa 2R.** Sabit R hedefini dipten öne almak, modeli "tuzak"
  modelinden "sabit R hasat eden" bir modele çevirirdi.

### `strategies/downtrend_rally.py` (short-only)

Rejim (200 EMA altı + 50 EMA < 200 EMA) ve kesitsel zayıflık (7g getiri evrenin alt %20'si)
altında, son düşüş bacağının 0.382-0.618 bandına ya da 20 EMA'ya **aşağıdan ilk dokunuşta**,
RSI(14) < 45 VE hacim 20 bar ortalamasının üstündeyken short.

- **Zayıflık kesitseldir.** "Düşüyor" mutlak bir ifade değil, sıralamada bir yerdir; mutlak
  tanım modeli bir piyasa yönü bahsine indirgerdi. Alt %20'nin tek bir sembolü
  adlandırabilmesi için en az 5 sıralanabilir sembol aranır, yoksa tur sinyalsiz geçer ve
  loglanır.
- **Bacağın dibi pivottan değil, barların kendi en düşüğünden okunur.** Zigzag kısa bacakları
  ÇİFT hâlinde eler (`_merge_short_legs`): taze bir dip, onu teyit eden ralli 8 bardan kısaysa
  pivot listesinde hiç görünmez. Dibi pivotlardan okumak, modelin "son düşüş bacağı" derken
  hep bir önceki bacağı kastetmesi — yani düzeltme bandını fiyattan onlarca yüzde uzağa
  koyması ve kapının pratikte hiç açılmaması — demekti. Bacağın BAŞI ise teyitli bir zigzag
  zirvesi olmak zorundadır.
- **Funding kapısı (ortalama < %-0.01 ise girme) bir kâr filtresi değil ölçüm filtresidir:**
  kalabalık zaten short taraftayken açılan işlem sinyal kalitesini değil squeeze riskini
  ölçer. Veri yoksa kapı KAPALI kalır — `meanrev`in BTC kapısıyla aynı gerekçe.
- **Hedefin yarısı önceki dip, kalanı 1×ATR trailing.** Yarı yarıya bölünme bir tercihtir:
  tezin bittiği yerde kârın yarısını almakla trendin devamını ölçmeye devam etmek arasında,
  ikisini de ölçülebilir bırakan tek nokta odur.

### `strategies/avwap.py` (long + short)

Çapa **en son kesinleşmiş** zigzag pivotu; çapadan itibaren hacim ağırlıklı ortalama ve hacim
ağırlıklı σ. Long: çapa dip, kapanış −2σ altında ve fiyat hâlâ çapanın üstünde. Short: çapa
tepe, kapanış +2σ üstünde ve BTC 200 EMA altında. Hedefler ±1σ (yarısı) ve AVWAP çizgisi.

- **`core/indicators.py`'ye `anchored_vwap` + `typical_price` eklendi.** Hacim ağırlıklı
  ortalama ve sapması bir göstergedir; strateji içine yazılmış ikinci bir uygulama modül
  docstring'indeki itirazın tam hedefidir. Sapma popülasyon (ddof=0) tanımındadır — Bollinger
  kararıyla tutarlı olsun ve "2σ" bu depoda tek bir şey ifade etsin diye.
- **Teyit "listenin sonuncusunu at" ile değil, zigzag'ın KENDİ eşiğiyle sorulur.** Canlı uç
  çapa yapılamaz (her barda yer değiştirebilir, AVWAP'ın başlangıcı kayardı), ama kısa bacak
  elemesi canlı ucu bazen zaten silmiş olur; sonuncuyu koşulsuz atmak çapayı bir swing geriye
  — bazen serinin başındaki sentetik ankraja — kaydırırdı. Teyit ölçüsü: pivottan sonra fiyat
  `pct_threshold` kadar ters yöne dönmüş mü. Bu, pivot matematiğinin ikinci bir uygulaması
  değil, aynı eşikle yapılan bir süzmedir.
- **Eğim filtresi ATR cinsindendir.** "Sert ters" ölçüsünü yüzdeyle tanımlamak eşiği sembolün
  oynaklığına göre farklı bir şeye çevirirdi; 0.5×ATR/5 bar, projenin ortak birimini kullanır.
- **Stop çapanın ötesi ya da 1.5×ATR — hangisi genişse.** Uzak çapada mesafe tavanı aşar ve
  işlem atlanır (kural 14). Bu, modeli doğal olarak GÜNCEL çapalara yönlendirir; stop'u tavana
  çekmek ise "tez çapanın ötesinde ölür" iddiasını sessizce başka bir iddiaya çevirirdi.

### `strategies/random_ctrl.py` — kontrol grubu

Her turda evrenden bilgisiz bir çekiliş: rastgele sembol, rastgele yön, 2×ATR stop. Boyut,
maliyet, limit ve defter kuralları diğer modellerle birebir aynıdır.

- **`is_benchmark = False`.** Çıpa (buyhold) "piyasa ne yaptı"yı ölçer; kontrol "sinyalin
  kendisi bir şey söylüyor mu"yu. İkincisinin cevabı ancak yarışmacılarla AYNI sütunda,
  aynı ortalama R sıralamasında okunabilir — ayrı bir bölüme koymak onu kıyasın dışına atardı.
- **Tohum tur bazlı karıştırılır:** `random.Random(f"{random_seed}:{as_of}")`. Sabit tohumla
  süreç başına tek RNG kurmak, her koşu ayrı bir süreç olduğu için HER TURDA aynı çekilişi
  yapardı — kontrol bilgisiz değil SABİT olurdu. Tur bazlı tohum hem tekrarlanabilirliği
  (aynı `as_of` → aynı sinyal) hem turlar arası bağımsızlığı verir.
- **RNG `core/data.py`'nin jitter RNG'siyle ve global `random` ile paylaşılmaz.** Paylaşmak,
  çekilişi o turda kaç kez yeniden denendiğine — borsanın o günkü keyfine — bağlardı; aynı
  `as_of` iki koşuda farklı sembol seçebilirdi.
- **Elemede görüş yok:** yalnızca `as_of` barı olmayan ve ATR'si hesaplanamayan semboller
  (işlem kurulamaz) çekiliş dışıdır. Buraya eklenecek her filtre kontrolü sessizce bir
  stratejiye çevirir ve diğer modellerin farkı neye karşı ölçtüğü bilinmez hâle gelir.
- **Stop 2×ATR**, `trend` ile aynı: kontrolün R ölçeği bandın (1×–2.5×ATR) dışında kalsaydı
  `cost_per_r` kolonu onu haksız biçimde iyi ya da kötü gösterirdi.

### `tests/helpers_market.py` genişletildi

`market(...)` artık `funding` alıyor ve `funding_series(rates, end=as_of)` eklendi: funding
kapısı/önceliği olan iki model (failed_breakout, downtrend_rally) onsuz test edilemezdi. Seri
`as_of`ta biter — kural 12 funding tarafında da geçerlidir.

---

## 14. `ensemble`: ilk meta model ve `is_meta` yolunun canlı doğrulaması

Onuncu model kayıtlı: `strategies/ensemble.py`, projedeki tek `is_meta = True` model. Kendi
sinyal mantığı yoktur — tek girdisi `peer_signals`'tır (kural 4'ün dar istisnası). Tezi tek
cümledir: *bağımsız modellerin aynı sembolde aynı yönde üst üste binmesi, tek bir modelin
sinyalinden daha iyi bir giriş midir?*

### Kendi kapısını eklemez

Ensemble'a bir rejim ya da hacim filtresi eklemek ölçtüğü şeyi "üst üste binme + benim
filtrem" yapardı: iyi sonucun örtüşmeden mi filtreden mi geldiği ayrılamaz, tez test edilemez
hâle gelirdi. Bu yüzden model hiçbir gösterge hesaplamaz; fiyat verisine yalnızca "sembol bu
turun anlık görüntüsünde `as_of` barını taşıyor mu" kontrolü için bakar.

### Oy veren havuz: `random_ctrl` ve `buyhold` dışarıda

- **`random_ctrl` oy vermez.** Kontrol bilgisiz bir çekiliştir; oyu sayılsaydı ensemble kısmen
  rastgele olurdu. Bozulma tek taraflı da değildir: kontrolün "bilgisiz sinyal" tanımı
  ensemble üzerinden dolaylı olarak bir stratejiye bağlanır, yani iki modelin ölçümü birden
  kirlenir.
- **`buyhold` oy vermez.** Çıpa yönlü bir görüş bildirmez (kural 15): her turda aynı iki
  sinyali üretir ve oyu sayılsaydı BTC/ETH'de kalıcı bir "long" tabanı yaratırdı.
- Havuz `VOTER_POOL` sabitinde açıkça yazılıdır — "hepsi hariç şunlar" biçiminde bir dışlama
  listesi, ileride eklenecek bir modelin sessizce oy vermeye başlaması demekti.

### Eşik iki AYRI model; oylar eşit ağırlıklı

Aynı model bir sembolde aynı yönde iki sinyal üretse bile tek oy sayar: ölçülen şey modellerin
üst üste binmesi, sinyal sayısı değil. Short-only modeller (`failed_breakout`,
`downtrend_rally`) short tarafta diğerleriyle aynı ağırlığı taşır. **Ağırlıklandırma yapılmadı**
— "hangi model daha iyi oy veriyor" ayrı bir tezdir ve karıştırıldığında iyi sonucun ağırlıktan
mı örtüşmeden mi geldiği ayrılamaz.

### Zıt yön: çakışma varsa işlem yok, tek muhalif veto değil

Bir sembolde hem long hem short eşiği geçiyorsa işlem yoktur ve durum `logger.info` ile
(hangi modellerin hangi tarafta olduğu dâhil) yazılır. Oy farkına ya da bir öncelik sırasına
bakarak bir tarafı seçmek ensemble'ı gizli bir ağırlıklandırmaya çevirirdi.

Eşiği geçmeyen TEK bir karşı oy ise çakışma sayılmaz: iki long oyuna karşı bir short oyu,
konsensüsün tanımı gereği konsensüsü bozmaz. Aksi kural, modeli "hiç kimse itiraz etmedi"
tezini ölçen bambaşka bir modele çevirirdi — tek bir muhalif her işlemi veto ederdi.

### Seviyeler devralınır, yeniden hesaplanmaz

- **Stop: katılanların en GENİŞİ.** En muhafazakâr seçim budur; dar stop aynı 1R'yi daha büyük
  notional ile taşımak ve R başına daha çok maliyet ödemek demektir (kural 14). Ortalama almak
  ya da kendi ATR katını kurmak, ensemble'ı katılımcıların hiçbirinin savunmadığı ÜÇÜNCÜ bir
  modele çevirirdi. En geniş stop `max_stop_atr_multiple` tavanını aşarsa işlem
  `core/engine.py` tarafından atlanır ve loglanır — bilinçli sonuç: konsensüsün taşıdığı risk
  ölçülemiyorsa işlem de açılmaz.
- **Hedef: en YAKIN, tek TP, fraction 1.0.** Kısmi çıkışları birleştirmek (fraction toplamak,
  kademe sıraya dizmek) katılımcıların hiçbirinin planına benzemeyen bir çıkış üretirdi.
  Hiçbir katılımcının hedefi yoksa sinyal hedefsiz gider.
- **`trailing_atr` istenmez.** Trailing bir çıkış tezidir ve katılımcılar arasında farklıdır;
  üçüncü bir birleştirme kuralı, ölçülen şeye ensemble'ın kendi katkısını eklerdi.

### `reason` ayrıştırılabilir bir kuyrukla biter

`... | voters=squeeze,trend | stop_from=squeeze | tp_from=none`. Karar 12'deki `| confidence=`
ile aynı gerekçe: hangi model bileşiminin kazandığı defterden (`trades.csv`) gruplanarak
sorulabilsin, ölçüm tablosunun kolonları hiç kirlenmesin. Oy listesi ve `stop_from`/`tp_from`
alfabetik seçilir — eşitlikte etiketin akranların çalışma sırasına göre değişmemesi için.

### `is_meta` yolu canlı doğrulandı

Bu yol bugüne kadar yalnızca sahte modellerle test edilmişti. `tests/test_ensemble.py` gerçek
Ensemble'ı `core/engine.py` içinde koşturur ve şunları çiviler:

- normal modeller bittikten SONRA meta geçişi çalışır; iki oy tek konsensüs sinyaline dönüşür
  ve bir sonraki barda dolar (kural 13),
- normal modeller `peer_signals=None` alır,
- metalar birbirini görmez: ensemble'ın yanında koşan ikinci bir meta'nın gördüğü küme yalnızca
  normal modellerdir (sıralamadan bağımsız), ve bir meta'nın sinyali ensemble'a oy olarak
  girmez,
- akran kümesi derin kopyadır ve `MappingProxyType`'tır (yazılamaz).

Kod tarafında bir değişiklik gerekmedi: `_collect_signals` `peer_base`'i meta döngüsünden ÖNCE
dondurduğu için metaların çıktısı akran kümesine hiç girmiyor, her meta kendi derin kopyasını
alıyor.

### Bilinen ve kabul edilen kenar durum: soğuk başlangıç

`core/engine.py` sinyal üretimini yalnızca `as_of` barına BU turda ulaşan modellere açar
(`reached_as_of`, aynı turun iki kez koşmasına karşı). Ensemble defteri boş olarak listeye
eklendiğinde, diğer modeller o barı zaten işlemişse ensemble tek başına "taze" olur ve akran
kümesi BOŞ gelir. Bu tek turluk bir durumdur ve sessiz değildir: model "havuzdan hiç sinyal
gelmedi" diye loglar. Guard'ı gevşetmek (metaları tazelik kontrolünden muaf tutmak) çift sinyal
riskini geri getirirdi; bir turluk boş oy ondan ucuzdur.

## 15. Dashboard ve Telegram özeti: raporlama katmanı

Ölçüm tamamdı, okunması değil. Bu adım hiçbir ölçüm kuralına dokunmaz — tek işi defterde
ve `docs/data/metrics.json`'da zaten duran sayıları telefondan okunabilir hâle getirmek.
Ayrım bilinçlidir ve aşağıdaki kararların çoğu ondan türer: **raporlama katmanı ölçümü ne
değiştirebilir ne de düşürebilir.**

### Neden ayrı bir modül: `core/report.py`

Dashboard'un ihtiyaç duyduğu her şey `core/metrics.py`'ye eklenebilirdi. Eklenmedi, çünkü
metrics bir ÖLÇÜM modülüdür ve sunum kararları (kaç işlem gösterilir, eğri kaç noktaya
seyreltilir, açık pozisyon nasıl işaretlenir) oraya sızsaydı ölçümün tanımı sunuma
bağlanırdı. Bölüşüm şöyle:

- **`core/metrics.py`** — havuzlanmış yön istatistikleri, kabul bayrakları, getiri
  korelasyonu. Üçü de metriktir: girdisi defter satırı, çıktısı sayı.
- **`core/report.py`** — bu sayıların yanına bağlam koyar (açık pozisyonlar, son işlemler,
  özsermaye eğrileri, 24 saatlik hareket) ve JSON bölümlerini kurar. Sunum sabitleri
  (`RECENT_TRADE_LIMIT`, `EQUITY_MAX_POINTS`) burada durur, `config.yaml`'da değil:
  config.yaml "tüm modeller için birebir aynı ölçüm koşulu" sözleşmesidir (kural 6) ve kaç
  satır çizildiği o sözleşmenin parçası değildir.

`main.py` ince kaldı: tek bir `build_dashboard(...)` çağrısı, defter kapsamı hâlâ açıkken.
Böylece `--dry-run` sayfayı da turun geçici kopyasından üretir, gerçek defterden değil.

### Havuz modele değil İŞLEME oy verir

"Short işlemler daha mı başarılı" sorusunun cevabı model başına ortalama R'lerin ortalaması
olamaz: 2 işlemlik bir model 200 işlemlik bir modelle eşit ağırlık alır ve sonuç işlemlerin
değil model sayısının ortalaması olur. Bu yüzden `pooled_direction_stats` tüm yarışmacıların
satırlarını tek havuzda birleştirip `direction_stats`'ı ONUN üzerinde çalıştırır.

Havuza kimin gireceğine `core/report.py` karar verir, `core/metrics.py` değil: modül defterin
içeriğinden başka bir şey varsaymamalı. Referans çıpası havuz dışıdır (stop'suz işlemin R'si
yoktur, kural 15); `random_ctrl` içeridedir, çünkü boyutlandırması ve stop ölçeği
yarışmacılarla birebir aynıdır.

### Kabul çıtası: İKİ KAPI + BİR UYARI, tek bir puan değil

Tek bir "skor" üretmek farklı soruları tek sayıya yığardı ve nerede takılındığı görünmez
olurdu. Ayrı ayrı raporlanır (tanımlar: `CLAUDE.md > Kabul Çıtası`):

- **Ö (örneklem, KAPI)** — iki işlemle +3R yapmış bir model, bu kapı olmadan tablonun
  başına oturur. Eşik 30 işlem.
- **E (edge, KAPI)** — ortalama R pozitif, kontrol grubu **marjla** aşılmış ve çıpa
  geçilmiş.
- **⚠ B (band, UYARI)** — kural 14'ün maliyet ölçeği kuralının denetlenebilir hâli.

`E`'nin üç koşulu ayrı bayrak yapılmadı: üçü aynı soruyu farklı yerlerden soruyor ("bu
sonuç sinyalden mi geliyor, piyasadan ve şanstan mı"), ve rozet sayısını artırmak mobil
tabloda okunaksız bir sütun üretirdi. Hangi koşulun düştüğü rozetin tooltip'inde ve
Telegram özetinin "en yakını" satırında yazılı.

#### Band neden bir KAPI değil

İlk hâlinde band da bir kapıydı ve bandın dışında kalan model "çıtayı geçemedi" sayılıyordu.
Bu iki ayrı soruyu birbirine karıştırıyordu:

1. *"Bu model doğrulandı mı?"* — modelin **kendi** verisiyle cevaplanır: yeterli örneklem
   var mı, sonuç kontrolü ve piyasayı geçiyor mu.
2. *"Bu model şu modelle kıyaslanabilir mi?"* — ancak bir **çiftle** cevaplanır ve
   cevabı modelin kalitesi hakkında hiçbir şey söylemez.

Bandın dışında kalmak ikinci sorunun konusudur. 1.2% stop mesafesiyle çalışan bir model,
diğerleri 3% civarındayken, aynı 1R'yi çok daha büyük notional ile taşır ve R başına çok
daha fazla komisyon+kayma öder — ama bu onun ölçümünü geçersiz kılmaz, yalnızca o satırı
başka bir satırın yanına koyarken `cost_per_r` farkının sonucu tek başına açıklayıp
açıklamadığını sormayı zorunlu kılar (CLAUDE.md > Rapor Kolonları zaten bunu söylüyordu).

Kapı olarak bırakmanın somut zararı: geniş ya da dar stop kuran bir modelin **tezi** —
"stop fitilin üstünde olmalı", "stop sıkı olmalı" — tam da ölçülmek istenen şeydir. Onu
"doğrulanamaz" diye işaretlemek, ölçüme girmeden önce elemek olurdu. Bu yüzden band artık
`passed`'a girmiyor; tabloda yalnızca bandın DIŞINDAKİ satırlarda görünen bir uyarı
ikonudur (`⚠ B`), tooltip'i ne yapılacağını söyler. Bandın içindeki satırlarda hiç
çizilmez: her satırda duran bir uyarı, uyarı olmaktan çıkıp süse döner.

#### Edge'in marjı

Kontrolü 0.01R ile geçen bir model marj olmadan "geçti" sayılırdı. `random_ctrl` bilgisiz
bir çekiliştir ve kendi ortalama R'si de bir örneklem tahminidir: kıl payı bir fark, iki
gürültülü ortalamanın farkından ibaret olabilir. `acceptance.edge_margin_r` (0.15R) bu
farkı anlamlı olana kadar bekletir.

Karşılaştırma `fark >= marj` şeklindedir ("en az bu kadar"). Sınırın ULP düzeyinde tanımı
anlamsızdır — iki kayan noktalı ortalamanın farkı söz konusudur ve 0.15 ile
0.1499999999999999 arasındaki ayrım gürültünün çok altındadır — bu yüzden koda yapay bir
tolerans eklenmedi ve test de sınırı iki yandan açıkça yokluyor.

#### Bandın çapası neden medyan

Kural 14'ün bandı **ATR katı** cinsindendir; `trades.csv`'de ATR yoktur ve işlem kapandıktan
sonra "giriş anındaki ATR" geri hesaplanamaz — geriye dönük yeniden hesaplamak look-ahead
için yeni bir kapı açardı. Sabit bir yüzde eşiği de kullanılamazdı: %3 stop, düşük volatil
bir dönemde geniş, yüksek volatil bir dönemde dardır.

Medyan bu iki sorunu birden çözer: aynı evrende aynı barlarda işlem yapan modellerin ortak
volatilite ölçeğini taşır ve rejim değiştikçe kendisi de kayar. Band `medyan/√oran ..
medyan×√oran` olarak kurulur — uçtan uca tam `stop_band_ratio` kadar geniş, yani kural 14'ün
1×–2.5× bandıyla aynı genişlikte.

#### Çıpayı geçme koşulu ve "en yüksek çıpa" kuralı

Kural 15'in sorusu: "model piyasayı yendi mi, yoksa yalnızca yükselen bir piyasada mı
durdu?" Birden çok çıpa varsa **en yükseği** zemindir. Alternatif (ilk çıpayı ya da
ortalamayı almak) çıtayı, hangi çıpaların listede olduğuna göre sessizce indirirdi.

Kontrol ya da çıpa kümede yoksa koşul değerlendirilemez ve `edge` geri kalanlara düşer —
ama `logger.warning` ile. Eksik bir çıta, geçilmiş bir çıta gibi görünmemelidir.

### Korelasyon: örtüşme par bazında, yetersizse `nan`

Modellerin bar getirilerinin Pearson korelasyonu, "sıralamanın ne kadarı gerçekten farklı
fikirlerden geliyor" sorusunu sorar: yüksek korelasyonla yarışan iki model bağımsız iki
ölçüm değil, aynı ölçümün iki kopyasıdır.

Kesişim **par bazında** alınır. Global kesişim kullanılsaydı, yarışmaya sonradan eklenen bir
modelin kısa geçmişi TÜM çiftleri onun uzunluğuna kırpardı. Örtüşme `min_overlap`'in
altındaysa hücre `nan`'dır ve sayfada boş kalır: `0.0` "ilişkisiz" demektir, "ölçülemedi"
değil (aynı gerekçe `core/metrics.py`'nin tamamında geçerli).

Korelasyon matrisi referans çıpasını DA içerir — orada ölçülen R değil bar getirisidir ve
"modeller piyasadan ne kadar ayrışıyor" sorusunun cevabı tam olarak çıpayla karşılaştırmayı
gerektirir.

### `docs/index.html`: sıfır bağımlılık

Sayfa tek dosyadır: framework yok, CDN yok, build adımı yok. Gerekçe, sayfanın ölçümün YÜZÜ
olup parçası olmamasıdır: bir grafik kütüphanesinin sürüm değişikliği ya da CDN kesintisi
raporu okunamaz hâle getirebilmemeli. Ölçüm zaten defterde ve JSON'da duruyor.

- **Renk ataması kimliğe bağlıdır, sıraya değil.** Filtre ya da sıralama değiştiğinde hiçbir
  modelin rengi değişmez; "trend maviydi" diye öğrenen okuyucu yanılmamalı.
- **Nötr griler bir zemindir, bir fikir değil.** Referans çıpası ve kontrol grubu kategorik
  renk slotu almaz — grafikte de tabloda da hue'suz durur.
- **Dokuzuncu model üretilmiş bir renk almaz.** İlk slotun rengini KESİKLİ çizgiyle (tabloda:
  içi boş kare) tekrar kullanır; kimliği renk + desen birlikte taşır. Sekizden sonra üretilen
  renkler renk körlüğünde birbirinden ayrılamaz.
- **Yön kimliği ile sayının işareti farklı renk ailelerindedir.** Short'a "negatif kırmızı"yı
  vermek, kârdaki bir short'u da kırmızı gösterip "kötü" diye okuturdu. Yön kimliği kategorik
  (mavi/turuncu), işaret ise diverging çift (mavi/kırmızı).
- **Tanımsız metrik `—`'dir.** JSON'da `null` (bkz. `main.py::_jsonable`), sayfada tire.
  `0` yazmak "ölçüldü, sıfır çıktı" ile "ölçülemedi"yi aynı hücreye yığardı.
- **Açık pozisyonun PnL'i çıkış maliyeti hariçtir** ve sayfa bunu söyler: pozisyon kapanmadı,
  çıkış fiyatı da komisyonu da bilinmiyor. Mevcut fiyattan kapanmış saymak, deftere hiç
  girmeyecek bir sayıyı kapanmış işlemlerin yanına koyardı.
- **Fiyatı olmayan sembol kâr/zararda gösterilmez:** giriş fiyatından işaretlenir (aynı kural
  `core/portfolio.py::_mark`'ta) ve satır `*` ile işaretlenir.

`main.py::_write_metrics` artık `_jsonable`'ı yükün TAMAMINA uygular, yalnızca model
tablosuna değil. Dönüşümü tek bir dala uygulamak, yeni bir bölüm eklendiği gün sessizce
GEÇERSİZ JSON üretirdi ve sayfa veriyi hiç çizemeden ölürdü.

### Telegram: kapı `as_of`'ta, hata yutuluyor

"Günde bir kez, 20:00 UTC turunda" kararını script verir, cron değil. 4H barlarda gün içinde
altı tur koşar; "günde bir"in tekrarlanabilir tanımı **"as_of'u 20:00 olan tur"**dur. Duvar
saatine bakan bir kapı, cron geciktiğinde ya da tur elle tekrarlandığında özeti ya iki kez
ya hiç yollardı.

Saat kapısı tek başına yetmiyor: tur düşerse depodaki `metrics.json` bir önceki turdan kalır
ve DÜNKÜ 20:00 raporu bugün tekrar yollanabilirdi. Bu yüzden `generated_at` üzerinden bir
tazelik kapısı daha var (`MAX_REPORT_AGE_HOURS = 3`; bar 4H olduğu için taze bir rapor her
zaman bundan gençtir).

**Script her yolda 0 döner.** Eksik token, ağ hatası, Telegram 4xx'i, bozuk JSON — hepsi
loglanır ve geçilir. Workflow adımı ayrıca `continue-on-error: true` taşır ve defter
commit'inden SONRA gelir. Üç ayrı emniyet, tek bir gerekçeyle: özet bir bildirimdir, ölçümün
parçası değil. Telegram'ın kesintisi yüzünden turun kırmızı dönmesi, defterin commit'lenip
commit'lenmediğine dair gerçek sinyali gürültüye boğardı.

Mesaj `parse_mode: HTML` ile yollanır, Markdown ile değil: model adları alt çizgi içerir
(`failed_breakout`, `downtrend_rally`) ve Markdown'da alt çizgi italik açar — mesaj ya bozuk
biçimlenir ya Telegram 400 döner. HTML'de kaçırılması gereken üç karakter vardır ve
`html.escape` hepsini kapatır.

Sıralamaya yalnızca ortalama R'si ÖLÇÜLEBİLEN modeller girer; kaç modelin ölçülemediği ayrı
bir satırda söylenir. Hiç kapanmış işlemi olmayan bir modeli "ilk 3"e koymak, ölçülmemiş bir
modeli ölçülmüş bir modelin önüne geçirirdi. Kontrol grubu sıralamada `⚠` ile anılır:
bilgisiz çekilişin ilk üçte olması gizlenecek bir kusur değil, raporlanacak bir sonuçtur.

### Referans satırlarının görsel ayrışması — kısmi bir sapma

İstenen "referans satırları (`buyhold`, `random_ctrl`) görsel olarak ayrışsın, yarışmacı
değiller" idi. Görsel ayrışma ikisine de uygulandı (nötr gri, ayrı etiket), ama
**`random_ctrl` sıralamada bırakıldı.** Gerekçe `README.md`'de zaten yazılı: `random_ctrl`
`is_benchmark` DEĞİLDİR — boyutlandırması, stop ölçeği ve limitleri yarışmacılarla birebir
aynıdır, tek farkı sinyalin bilgisiz olmasıdır. Onu ayrı bir bölüme almak "edge'in referansı
ancak aynı sütunda okunabilir" ilkesini bozardı. Yalnızca `buyhold` ayrı REFERANS bölümünde
durur (kural 15). Ayrım tabloda KONTROL / REFERANS etiketleriyle görünür.

---

## 16. Dashboard iki seviyeye ayrıldı: genel bakış + model detayı

Karar 15'in sayfası tek düzlemdi: her şey tek akışta, tek tabloda. Model sayısı 11'e
çıkınca iki ayrı okuma isteği aynı ekranda çakışmaya başladı — *"hangi model önde"* ile
*"bu model ne yaptı"*. Birincisi tüm modelleri yan yana ister, ikincisi tek bir modelin
işlem işlem geçmişini. Sayfa ikiye ayrıldı; ÖLÇÜM tarafında hiçbir şey değişmedi.

### Seviye 1 tablo değil kart: leaderboard'ın kaybolmaması için rütbe sayısı taşındı

Eski leaderboard 10 kolonluk bir tabloydu ve `min-width: 760px` ile yatay kaydırmaya
sarılıydı: telefonda `cost_per_r` kolonu ekranın dışındaydı, yani pratikte yoktu. Yerine
model kartları geldi ve kartlar **tez tipine göre** gruplandı (trend & momentum, ortalamaya
dönüş, kırılım & tuzak, sadece short, meta, referans). Gruplama bir süs değil: iki trend
modelini yan yana okumak, birini bir ortalamaya-dönüş modelinin yanında okumaktan başka bir
şey söyler.

Gruplama tek başına bir bilgiyi kaybettiriyordu — **sıralama.** Kartlar gruba göre dizildiği
için "kim önde" artık düzenden okunamıyor. Bu yüzden her yarışmacı kartı ortalama R
sıralamasındaki **rütbesini** (`#1`, `#2`, …) sayıyla taşır ve özet kartlarından biri lideri
adıyla yazar. Rütbe yalnızca R'si ölçülebilen yarışmacılara verilir: çıpanın R'si yoktur
(kural 15), ölçülmemiş bir modeli sıraya sokmak ölçülmüş bir modelin önüne geçirirdi.

Listede olmayan bir model sessizce DÜŞMEZ, "Gruplanmamış" başlığına düşer. Sessiz düşmek,
yeni eklenen bir modelin aylarca sayfada görünmemesi demekti — tam da kural 6'nın
engellemek istediği şey.

### Çıpanın kartında hero sayı ort. R değil, hesap getirisi

Referans kartı diğerleriyle aynı şablonu kullansaydı en büyük sayısı `—` olurdu: stop'u
olmayanın 1R'si yoktur. Kart bunun yerine hesap getirisini büyütür — kural 15'in "çıpanın
taşıdığı bilgi sıralamada değil, hesap düzeyi getirisindedir" cümlesinin görsel karşılığı.

### Detay AYNI sayfada ve adresi `#model=<ad>`

İkinci bir HTML dosyası ya da bir yönlendirme kütüphanesi "tek dosya, build adımı yok"
kuralını bozardı. Hash yönlendirmesi ikisini de bozmadan geri tuşunu, yer imini ve
paylaşılan linki çalıştırır. Genel bakışa dönüşte okuyucunun bıraktığı kaydırma konumu
geri yüklenir; doğrudan bir model linkiyle girildiyse geri düğmesi `history.back()` yerine
hash'i temizler (aksi hâlde siteden çıkarırdı).

### Mobil: geniş tablolar KART'a dönüşür, kaydırmaya sarılmaz

Kural, "yatay kaydırma yok" idi ve bunun tek dürüst uygulaması tabloyu dar ekranda
bırakmak: bir tabloyu `overflow-x` içine koymak K/Z kolonunu ekranın dışında tutar, yani
sayfa o sayıyı hiç göstermemiş olur. Açık pozisyonlar bu yüzden iki düzende yazılır (aynı
veri, CSS hangisinin görüneceğine karar verir) ve korelasyon matrisi dar ekranda "en güçlü
çiftler" listesine döner. Tablo eşiği (1060px) tahminle değil ölçülerek seçildi: uç
değerlerle (yedi haneli miktar, sekiz ondalıklı fiyat) 11 kolon ~1040px istiyor.

Sayı asla kırpılmaz ve asla ortasından bölünmez: her sayı `.nw` ile atomiktir, hücre ise
sarılabilir. İkisi birden gerekiyordu — hücreye top yekûn `nowrap` vermek iki hedefli bir
"hedef" değerini ekran dışına taşırıyordu, `normal` bırakıp sayıyı sarmamak ise fiyatı
ortadan bölüyordu. Fiyat ondalığı sembole göre uyarlanır (BTC'de 2, `0.7232`'de 6): sabit
iki hane küçük fiyatlı sembollerde girişi ve stop'u aynı sayı gösterirdi.

Denetim ölçülerek yapıldı: dört genişlikte (320 / 390 / 700 / 1060 / 1280) iki seviye de
taranıp yatay kaydırma, ekran dışına taşan öğe, 44px altındaki dokunma hedefi ve kırpılmış
metin arandı; üç ayrı veri kümesiyle (gerçek defter, uç değerli stres kümesi, `model_trades`
bölümünden önce üretilmiş eski bir yük) tekrarlandı.

### Veri tarafı: `model_trades` bölümü (yalnızca `core/report.py`)

Detay görünümü modelin kendi geçmişini sayfalı gösterir; 20 satırlık ortak akış (`recent_trades`)
buna yetmiyordu ve akışı modele göre süzmek de çözüm değildi — çok işlem yapan bir model 20
satırı doldurup diğerlerini akıştan siler. Yeni bölüm model başına **son 100** kapanmış işlemi
ve kırpılmadan önceki `total`'ı taşır; `total` olmadan kırpılmış bir liste modelin tüm geçmişi
gibi okunurdu. Tamamı taşınmaz: defter append-only büyür, JSON her turda baştan yazılır —
denetim izi `ledgers/` altındadır, sayfa onun son penceresini çizer.

Bölüm `core/metrics.py`'ye DEĞİL `core/report.py`'ye eklendi; gerekçe karar 15'inkiyle aynı:
"kaç satır gösterilir" bir sunum sabitidir, ölçümün tanımı ona bağlanmamalı. İşlem satırına
`qty`, `notional` ve `risk_amount` eklendi (`risk_amount` R'nin paydasıdır: onsuz sayfa "bu
işlemin R'si neye göre" sorusunu cevaplayamaz ve okuyucu paydayı `risk_per_trade × sermaye`
sanar — kaldıraç tavanı boyutu küçülttüğünde ikisi ayrışır). Açık pozisyon satırına `take_profits`
eklendi: KALAN hedefler, çünkü kısmi TP dolduğunda `core/portfolio.py` onu pozisyondan düşer
ve dolmuş bir hedefi hâlâ beklenen gibi çizmek yanlış olurdu.

Açık pozisyonların güncel fiyatı ve gerçekleşmemiş K/Z'si zaten yükte vardı (karar 15) —
eklenmedi, olduğu gibi kullanıldı.


## 17. Scalp katmanı (15 dakika): model 11 ve 12, `--layer` ve `core/layers.py`

Yeni bir zaman dilimi eklendi ve mevcut 4 saatlik boru hattına dokunulmadı. Ölçülmek istenen
soru ikisi birden: (a) 15 dakikalık scalp kurulumları ölçülebilir bir edge üretiyor mu,
(b) **kollar arası tahsisi ÖĞRENMEK, eşit dağıtmaktan daha iyi mi.** İkinci soru bu katmanın
varlık sebebidir ve cevabı ancak model 11 ile 12 arasındaki fark tek bir şeye indirgenirse
okunabilir.

### Katman mı, ayrı giriş noktası mı

Üç seçenek tartıldı:

1. `main.py --layer scalp` + `config.yaml > layers` bloğu (seçilen)
2. `main_scalp.py` + `core/runner.py` (main.py'nin gövdesi ortak bir modüle taşınır)
3. `main_scalp.py` + `scalp_config.yaml` (tam ayrışma)

(3) elendi: `risk_per_trade`, `fee_rate`, `slippage_*`, `leverage_cap`, `initial_capital` ve
`maintenance_margin` iki katmanda da birebir aynı olmak ZORUNDA (kural 6). İki dosyaya
bölmek, bir gün birinin sessizce ayrışması ve iki katmanın farklı maliyet varsayımlarıyla
koşması demekti — üstelik bunu hiçbir test yakalamazdı.

(2) elendi çünkü ayrışan şey ölçümün AKIŞI değil KOŞULLARI. Çekirdek zaten tamamen config
sürümlüydü (`core/engine.py`, `core/portfolio.py`, `core/metrics.py`, `core/data.py` barı,
evreni ve tavanı config'ten okuyor); ayrı bir giriş noktası yalnızca orkestrasyonu — model
kurulumu, iki katmanlı hata izolasyonu, dry-run kopyası, yük yazımı — ikinci kez yazmak
olurdu. Mevcut `main.py`'yi refactor etme maliyeti de cabası; oysa istek 4 saatlik boru
hattına dokunmamaktı.

`core/layers.py` katman bloğunu kökün üzerine **derin birleştirmeyle** bindirir ve çözülmüş
config'ten `layers` anahtarını DÜŞÜRÜR: çekirdek modüller `get_setting(config, "timeframe")`
ile okuduğu için, blok kalsaydı bir modül yanlışlıkla diğer katmanın barını görebilirdi.
Katmanın kimliği (defter kökü, rapor dosyası, sabit evren, saklama penceresi, kırılımlar)
çözülmüş config'e karışmaz; yalnızca `main.py` okur. Böylece `Engine`, `Portfolio` ve
`metrics` katmanı hiç bilmez — katman farkındalığı tek bir dosyada durur.

Tanınmayan katman adı sessizce `base`e düşmez: bir yazım hatası, scalp turunun 4 saatlik
defteri ezmesi demek olurdu.

### Evren neden sabit, stop tavanı neden 8×ATR

Evren elle yazılmış 14 semboldür ve hacimden hesaplanmaz. Gerekçe kıyasın kendisidir: evren
zamanla kayarsa geçmiş performans başka bir sembol kümesine ait olur ve iki modelin sayıları
aynı yarışın sayıları olmaktan çıkar.

> **Güncelleme (karar 24):** liste 13 sembole indi — TON-USDT-SWAP OKX'te mevcut olmadığı
> için zaten hiçbir turda çekiliş uzayına girmiyordu. Evrenin SABİT olması ilkesi
> değişmedi; değişen, config'in yazdığı kümenin turun gerçekte gördüğü kümeyle
> eşleşmesidir.

Stop tavanı (kural 14) scalp'te 3× değil **8×ATR**'dir. 15 dakikalık barda ATR tipik olarak
fiyatın %0.15-0.25'idir, oysa scalp modellerinin stop TABANI %1'dir (aşağıya bakınız). 3×
tavanı bırakmak, kurulumların neredeyse tamamının "band dışı" diye elenmesi ve iki modelin de
30 işlemlik örneklem kapısına hiç ulaşamaması demekti — ölçüm başlamadan biterdi. Tavan katman
İÇİ kıyası bozmaz: iki model de aynı tavanı görür, katmanlar arası kıyas ise zaten yapılmıyor.

### İki modelin farkı tek satır: `choose_arm`

Beş kol (`strategies/scalp/arms.py`) ve tüm kapılar (`strategies/scalp/model.py`) tek
kopyadır; `scalp_bandit` ve `scalp_fixed` yalnızca `choose_arm`u uygular. Kollar kopyalansaydı
iki modelin farkı zamanla "adaptasyonun katkısı" olmaktan çıkar, iki ayrı uygulamanın farkı
hâline gelirdi — oysa model 12 tam da model 11'in NULL HİPOTEZİDİR.

**Stop her kolda aynı, hedef her kolda kendi yapısından.** Stop mesafesi maliyet ölçeğidir
(kural 14): kollar farklı stop mantıkları kullansaydı kol bazlı ortalama R tablosu bir sinyal
değil bir stop-mesafesi karşılaştırması olurdu ve bandit sinyali değil, en ucuz stop ölçeğini
öğrenirdi. Hedef ise kolun tezinin parçası: projeksiyon (`2R`) ile kolun yapısal engelinin
(günün zirvesi, aralığın ölçülü hareketi, Bollinger orta bandı, patlamanın büyüklüğü, VWAP)
YAKIN olanı alınır. Bu ikili kural bir denemenin sonucudur: yalnızca projeksiyon, her
kurulumun hedef/stop oranını aynı sayıya sabitleyip 1.5R kapısını ölü koda çeviriyordu;
yalnızca yapısal seviye ise sentetik veride kurulumların tamamını kapıda eliyordu (RR 0.02-0.33)
— iki model de hiç işlem açamıyordu.

### Bandit durumu: ayrı dosya YOK

Posterior `ledgers_scalp/scalp_bandit/trades.csv`'nin saf bir fonksiyonudur ve her turda
sıfırdan kurulur. Ayrı bir `bandit_state.json` daha açık bir "durum" olurdu ama defterle
senkron kalması ayrıca test edilmesi gereken İKİNCİ bir doğruluk kaynağı yaratırdı; ikisi
ayrıştığında hangisinin doğru olduğu bilinemezdi. Artımlı sayaç da aynı nedenle reddedildi
(bir tur atlanır, bir satır elle düzeltilir — hafıza defterden ayrılır).

Modelin kendi kapanmış işlemlerini görmesi için sözleşmeye dar bir kanca eklendi
(kural 16, `Strategy.observe_closed_trades`), `peer_signals` kadar dar: **açık pozisyon bu
listeye hiçbir yoldan giremez** ve model yalnızca KENDİ işlemlerini görür. Kancayı
uygulamayan modeller için defter hiç okunmaz — 4 saatlik katmanın on bir modeli bu eklemeden
etkilenmez. Besleme, defterdeki satırlara BU turda kapanan işlemleri de ekler: 15 dakikalık
bir modelde pozisyon aynı turda açılıp kapanabilir ve defteri beklemek, modelin en taze
sonucu bir tur geç görmesi demekti.

Kol etiketi (`| arm=... | post_r=...`) `core/tags.py`'de tek yerde tanımlıdır; okuyan taraf
etiketi bulamazsa `TagError` fırlatır. Sessizce atlamak, posterior'ı ve kol kırılımını
defterde görünmeyen bir geçmişe bağlardı.

### Thompson sampling neden Normal-Normal

Ödül R'dir: sürekli, sınırsız (likidasyonda −1'in altına iner) ve işaretli. Beta/Bernoulli
posterior yalnızca kazanma oranını öğrenirdi — 1R kazanan kol ile 3R kazanan kolu aynı sayardı.
Kolun ortalama R'si üzerine normal posterior kullanılır, çekiliş `Normal(ortalama, σ/√n)`dan
yapılır; gözlemsiz kolun σ'sı önselden gelir.

Üç koruma: **ısınma** (kolların HEPSİ 20 işleme ulaşana kadar eşit çekiliş — erken öğrenme
2-3 işlemlik gürültüyü kalıcı tercihe çevirir), **taban tahsis** (%5 × uygun kol sayısı
olasılıkla eşit çekiliş — susturulan kol bir daha ÖLÇÜLEMEZ) ve **kayan pencere** (kol başına
son 100 işlem — iki yıl önceki bir ortalama bugünkü tahsisi belirlememeli). Taban bir kota
değil bir OLASILIKTIR; testler de bunu dalgalanma payıyla ölçer.

### Saklama penceresi: `equity.csv` sıkıştırması

Günde 96 tur × 2 model, her tur commit ediliyor: sıkıştırma olmadan `equity.csv` yılda ~35
bin satıra çıkar ve depo geçmişi ölçümle ilgisiz satırlarla şişer. 30 günden eski satırlar
GÜNLÜK özete (o günün SON barı — ortalaması değil; ortalama, eski dönemin drawdown'ını
olduğundan iyi gösterirdi) indirilir. Bu, `core/ledger.py`'nin append-only sözleşmesinde
bilinçli ve dar bir istisnadır: `equity.csv` bar başına bir ANLIK GÖRÜNTÜ serisidir, aynı
bilginin türevi; `trades.csv` ise denetim izidir ve orada istisna YOKTUR. Taze 30 gün bar
bazında kalır, sıkıştırma her model için birebir aynı uygulanır ve metrikler sıkıştırılmış
eğriden hesaplanır (rapor ile defter her zaman aynı şeyi söylesin).

JSON'a model başına son 100 değil **50** işlem yazılır; aynı gerekçe (dosya her 15 dakikada
bir commit ediliyor). İkisi de `layers.<ad>.retention` altındadır — katmana göre değiştikleri
için `core/report.py`'deki tek değerli sunum sabitlerinin yanına konamazlardı.

### Dashboard: ayrı bölüm, ayrı dosya

Sayfa ikinci bir `fetch` ile `data/metrics_scalp.json`u okur ve scalp modellerini kendi
bölümünde, kendi tablosunda gösterir. Tek dosyada birleştirmek, 15 dakikada bir koşan
katmanın 4 saatlik raporu da her seferinde yeniden yazması demekti. Aynı tabloda sıralamak
ise zaman dilimi farkını bir performans farkı gibi gösterirdi: işlem sıklığı, maliyetin R
içindeki payı ve tutma süresi bambaşka. Dosya yoksa bölüm sessizce gizli kalır — eksik bir
dosya bir arıza değil, henüz olmayan bir ölçümdür.

Kırılımlar (`core/metrics.py::breakdown`) katman ayarıdır: kol kırılımı tahsisin eşitten
sapıp sapmadığını, sembol kırılımı kayma varsayımının ince kitaplı sembollerde (PENGU, ETHFI)
tutup tutmadığını gösterir. Grup başına yön ayrımı YAPILMAZ (JSON'u üçe katlardı ve yön
sorusunun cevabı zaten model tablosunda ve havuz panelinde duruyor).

### Bilinen sınır: nakit, eşzamanlı pozisyon sayısını ~1'e indiriyor

Boyutlandırma kuralı (kural 11) + %1 stop tabanı birlikte şu sonucu veriyor:
`notional = risk / stop%` = `100 / 0.01` = **$10.000**, yani başlangıç sermayesinin tamamı.
Bir pozisyon açıldığında serbest nakit tükeniyor ve aynı turdaki ikinci sinyal `zero_size`
ile reddediliyor. Yani scalp modelleri pratikte `max_positions` (5) kotasına hiç ulaşmıyor,
aynı anda ~1 pozisyon taşıyor ve devir hızını zaman stop'u belirliyor (16+1 bar ≈ 4.25 saat,
günde ~5 işlem). Kural 11 tek yetkili boyutlandırma kuralıdır ve katmana göre değiştirilemez;
bu yüzden davranış olduğu gibi bırakıldı ve burada kayda geçirildi. Ölçümü bozmuyor (iki model
de aynı kısıtı görüyor) ama tur loglarında sık `zero_size` WARNING'i üretiyor — bunlar bir
arıza değil, sermayenin dolu olması.

---

## 18. Scalp katmanına üç model: çıkış yönetimi motoru, `is_replica` ve modeller 13-15

Katmana üç model eklendi ve bunun için çekirdeğe **iki yeni yetenek** girdi: üç aşamalı
çıkış yönetimi ve kopya (replica) model bayrağı. Üçü de aynı eksende ölçüm yapar ama üç
farklı soruyu sorar:

| Model | Soru | Kıyas hedefi |
|---|---|---|
| 13 `vwap_clone` | Dış sistem BİZİM varsayımlarımızla ne yapardı? | (yarışma dışı) |
| 14 `vwap_managed` | Aynı sinyal ev kurallarıyla ne yapar? | `vwap_clone`, `scalp_fixed` |
| 15 `scalp_managed` | Çıkış yönetimi TEK BAŞINA ne katar? | `scalp_fixed` |

### Çıkış yönetimi: neden motorda, stratejide değil

`breakeven_at_r`, `partial_tp` ve `trail_giveback_pct` `Signal`e eklendi; üçü de
opsiyoneldir ve varsayılanı `None`dır — doldurmayan model (mevcut on üç modelin hepsi) bu
eklemeden hiçbir biçimde etkilenmez. Uygulama kural 9'un `trailing_atr` için koyduğu sınırın
aynısına tabidir: **strateji yalnızca isteğini bildirir.** Stop hareketleri
`core/engine.py`de, kısmi dolum `core/portfolio.py`dedir. Stratejide uygulansaydı kural
1/3/7 delinir (model kendi pozisyon durumunu tutmaya başlardı) ve iki modelin "aynı
yönetim" iddiası koddan denetlenemez hâle gelirdi.

**Mum içi sıralama: likidasyon -> stop -> kısmi -> TP.** Kısmi çıkışın stop'tan SONRA
gelmesi kural 13'ün aynı kuralıdır: aynı mumda ikisi de aralığa giriyorsa mum içi sıra
bilinemez ve KÖTÜ olan gerçekleşmiş varsayılır.

**Kısmi çıkışın çektiği stop, o mumda bir daha kontrol edilmez.** Bu karar mekanizmanın
ölçülebilirliğinin tamamıdır. Yeni stop, kısmi dolum gerçekleştikten SONRA verilmiş bir
emirdir ve mumun daha önceki hareketleri sırasında piyasada durduğu varsayılamaz (kural
13'ün "emir bir sonraki barda geçerlidir" ilkesi). Tersini yapmak — yeni stop'u aynı mumda
da kontrol etmek — kısmi çıkışı neredeyse her mumda anında tam çıkışa çevirir ve "kalanı
koştur" tezini hiç sınanamaz kılardı.

**Stop hareketleri bar KAPANDIKTAN sonra.** Breakeven ve geri verme takibi, mevcut ATR
trailing'iyle aynı adımda ve aynı gerekçeyle çalışır: barın high/low'una bakıp aynı barın
stop'unu değiştirmek, o barın içinde geçmişe dönük karar vermek olurdu (kural 12). Üçü de
yalnızca SIKIŞTIRIR, bu yüzden sıraları önemsizdir — her zaman en sıkı olan kalır.

**`trailing_atr` ile `trail_giveback_pct` aynı anda kullanılamaz** (`core/validate.py`
`ValueError` fırlatır). İkisi de stop'u sıkıştıran ayrı mekanizmalardır; birlikte
çalıştıklarında çıkışı hangi kuralın ürettiği defterden okunamaz hâle gelir — oysa bu
modeller tam olarak çıkış kuralını ölçmek için var. Aynı gerekçeyle `partial_tp.fraction`
1.0 olamaz (o bir kısmi çıkış değil tam çıkıştır) ve takip eden stop **orijinal hedefi asla
aşamaz**: aşsaydı hedef hiç dolmaz ve her işlem stop'la kapanırdı, yani `exit_reason`
kolonunun tüm anlamı kaybolurdu.

`ExitReason`a `"partial"` eklendi: kısmi çıkış aynı anda stop'u da hareket ettirir ve
defterde `"tp"`den ayırt edilemezse "modelin hedefi doldu" ile "yönetim kuralı devreye
girdi" aynı satıra çöker.

### `is_replica`: neden `is_benchmark`in yanına ikinci bir bayrak

Kopya model bir dış sistemin kurallarını yeniden üretir ve o sistemin boyutlandırması
**sabit teminattır**, %1 risk değil. Kural 15'in muafiyetini (`notional_fraction`) ona da
açmak gerekiyordu, ama `is_benchmark`i kullanmak iki ayrı şeyi tek bayrağa bindirirdi:

- **Ölçtükleri soru farklı.** Çıpa "piyasa ne yaptı", kopya "dış sistem bizim maliyet,
  kayma, funding ve likidasyon varsayımlarımız altında ne yapardı" der. Kabul çıtasının
  "edge" kapısı çıpayı ZEMİN olarak kullanır (kural 15); kopyayı da zemin saymak, çıtayı
  bir stratejinin performansına bağlamak olurdu. Bu yüzden `_benchmark_return` yalnızca
  `is_benchmark` satırlarını okur.
- **Stop kuralları TERS.** Çıpanın tanımı stop'suz olmasıdır ve uydurma bir stop uydurma
  bir R üretir; bu yüzden `stop_price` None OLMALIDIR. Kopyanın stop'u ise kopyalanan
  sistemin kendi kuralıdır ve stop yönetimi tam olarak ölçülmek istenen şeydir; bu yüzden
  `stop_price` ZORUNLUDUR. Tek bayrakla iki kuralı birden ifade etmek imkânsızdı.

Ortak yanları da var ve o yüzden ikisi de `is_competitor = False`: ikisi de kendi
boyutlandırma kuralıyla koşar, dolayısıyla **ortalama R sıralamasına girmez**, kabul
kapılarına tabi değildir, stop bandı medyanına katılmaz ve maliyet ölçeği kolonlarında
(`avg_stop_distance_pct`, `cost_per_r`) `nan` alır.

Kopyada bu son nokta bilinçli bir seçimdir: **sayı hesaplanabilir ama kıyas anlamsızdır.**
Kopya 1R'yi sabit teminattan türetir, yarışmacılar sermayenin %1'inden; ikisini aynı sütuna
koymak farklı paydaya sahip iki oranı karşılaştırılabilirmiş gibi sunardı. R'nin KENDİSİ
ölçülmeye devam eder (kopyanın kendi geçmişi ve kendi bandit'i ona dayanır), yalnızca
maliyet kolonları boştur.

**`ModelLimits` yalnızca kopya modellere açıktır.** Kaynak sistemin eşzamanlı pozisyon
sayısı, yön kotası, portföy riski tavanı ve kaldıracı onun kendi kurallarıdır. Model bunları
kendi içinde uygulayamaz — açık pozisyonlarını `generate_signals`ta göremez (kural 4/16) ve
boyut/kaldıraç hesabı zaten `core/portfolio.py`nindir (kural 3) — bu yüzden limitler bir
BİLDİRİMDİR ve uygulayan yine tek yetkili yerdir. Yarışmacılara kapalı olması kural 6'nın
kendisidir: tabloda yan yana duran iki satır farklı limitlerle koşamaz. Kapı
`core/validate.py::validate_model`dedir ve `strategies/registry.py` modeli kurarken çağırır;
kurulumda patlayan model `main.py` tarafından atlanır ve koşu hata koduyla biter (sessiz
eksik yarışma olmaz).

Kaldıraç tavanı `REPLICA_LEVERAGE_CAP = 10`dur. Kök `leverage_cap` (5) ölçümün ortak
tavanıdır ve yarışmacıların hepsi ona tabidir; tavansız bırakmak ise tek bir config
satırıyla 50x bir satırın tabloya girmesi demekti.

### Kopya modelde likidasyon kapatılmaz

**Bu kararın tek bir gerekçesi var ve pazarlık konusu değil: 10x'te likidasyon gerçek bir
risktir.** Bakım marjı %0.5 iken 10x bir pozisyon girişin ~%9.5 ötesinde likide olur. Model
13'ün stop'ları bundan dardır (tipik olarak %0.4-1.0), yani çoğu işlemde stop önce
tetiklenir — ama boşluklu açılışlarda, funding birikiminde ve stop'un geniş kaldığı
kombinasyonlarda likidasyon devreye girer ve girmelidir.

Alternatif — kopya için likidasyon kontrolünü atlamak ya da bakım marjını gevşetmek —
kopyayı **haksız biçimde iyi gösterirdi.** Kopyanın cevapladığı soru "dış sistem bizim
varsayımlarımız altında ne yapardı"dır; o varsayımların içinde likidasyon da vardır ve
kaldıracın bedeli tam olarak budur. Likidasyonu kapatmak, kopyaya yarışmacıların hiçbirinin
sahip olmadığı bir muafiyet vermek ve "10x kullanmak bedava" demek olurdu. Kural 15'in
"muafiyet YALNIZCA boyutlandırmadadır" cümlesi burada da geçerlidir: komisyon, kayma,
funding, likidasyon, evren ve dolum kuralı kopyaya da birebir aynı uygulanır.

Aynı gerekçe `liquidation_price`ın giriş notional'ı üzerinden hesaplanmasını da korur
(karar 5): eşiği mum içi mark fiyatına göre yeniden hesaplamak tekrarlanabilirliği bozardı.

### Model 13: kopyanın kendi kuralları

> **Bu alt bölüm 23 numaralı kararla ÜSTÜNE YAZILDI.** Aşağıdaki sinyal, stop, hedef,
> grid ve evren kuralları o zamanki hâli anlatır; bugünkü kurallar için bkz. 23.
> Boyutlandırma, limitler, çıkış yönetimi ve "ev kapıları uygulanmaz" ilkesi DEĞİŞMEDİ.

- **Sinyal:** gün-çapalı VWAP'ten `band_mult × sapma` kadar uzaklaşıp DÖNMEYE BAŞLAYAN bar.
  Dönüş şartı zorunludur: yalnızca "bant dışında" olmak, güçlü bir trendde her barda aynı
  sinyali üretirdi.
- **Boyutlandırma:** `notional_fraction = 0.5` + `leverage = 10`. Başlangıç sermayesinde
  (10.000) bu tam olarak "500 USDT teminat, 5.000 notional"dır. Sabit bir USDT tutarı yazmak
  yerine oran yazıldı: hesap iki katına çıktığında kopya, kaynak sistemin yarısı kadar risk
  alan bir şeye dönüşürdü.
- **Ev kapıları UYGULANMAZ.** %1 stop tabanı ve 1.5R kapısı kaynak sistemde yok; eklemek
  kopyayı "ev kurallarıyla koşan bir model"e çevirirdi — ki o zaten model 14'tür ve ikisinin
  farkı tam olarak bu kapıların (artı boyutlandırmanın) katkısıdır.
- **Parametre öğrenimi:** epsilon-greedy (0.25), 4 ATR çarpanı × 3 hedef çarpanı = 12
  kombinasyon. İstatistik SEMBOL bazındadır — aynı kurulum BTC'de ve PENGU'da aynı stop
  çarpanıyla aynı sonucu vermez — ama sembol başına veri geç birikir; 3 örnekten az veri
  varken sembolün kendi gürültüsüne uymak, genel ortalamadan DAHA KÖTÜ bir tahmindir, o
  yüzden genele düşülür. Geri düşüş kombinasyon bazındadır, sembolün tamamı için değil:
  bir kombinasyon 10 kez, diğeri 1 kez oynanmış olabilir.
- **Durum deftere yazılır, ayrı state dosyası YOKTUR** (`scalp_bandit` ile aynı desen ve aynı
  gerekçe): ikinci bir doğruluk kaynağı, defterle ayrıştığında hangisinin doğru olduğunu
  bilinemez kılardı. Denetim izi `reason` kuyruğundadır: `| arm=vwap_revert | combo=atr2_tp1`.
- **Hedef çarpanlarının en küçüğü (1.5) kısmi çıkış seviyesine EŞİTTİR.** Daha küçük bir
  hedef, kısmi çıkış hiç tetiklenmeden pozisyonu tamamen kapatır ve üç aşamalı yönetimin
  ikinci/üçüncü aşaması hiç ölçülemezdi.
- **Evren: 13 sembol** (SUI yok). Katmanın evreni 14'tür ve aradaki fark bilinçlidir:
  fazladan bir sembolde işlem açmak kopyayı kopya olmaktan çıkarırdı.

### Model 15: eşleştirilmiş deney ve paylaşılan çekiliş kimliği

Model 15 `ScalpFixed`ten TÜRER (`ScalpModel`den değil): kol seçimi kuralının ikinci bir
kopyası, "aynı eşit ağırlıklı çekiliş" iddiasını koddan denetlenemez kılardı. Miras,
iddiayı bir yorum olmaktan çıkarıp bir olguya çevirir — `choose_arm` tek bir yerde tanımlı.

`ScalpModel`e `rng_identity` alanı eklendi ve model 15 onu `scalp_fixed`in adına bağlar.
Bu, projenin başka bir yerinde bilinçli olarak TERSİ olan bir karardır ve ikisi de aynı
ilkeden gelir: **çekiliş, ölçülmeyen eksende paylaşılmalı; ölçülen eksende bağımsız
olmalı.**

- Model 11 ↔ 12'de ölçülen şey SEÇİMİN kendisidir; çekilişi paylaşsalardı fark adaptasyonun
  değil tesadüfün ölçüsü olurdu (karar 17).
- Model 12 ↔ 15'te ölçülen şey seçim DEĞİL, aynı seçimin nasıl yönetildiğidir; bağımsız
  çekiliş, farkın içine "hangi model şanslı kurulumu çekti" gürültüsünü katardı.

Bu, iki modelin defterlerinin birebir aynı olacağı anlamına gelmez ve gelmemelidir: yönetim
bazı pozisyonları erken kapatır, dolayısıyla `max_positions` doluluğu ve
`duplicate_position` retleri zamanla ayrışır. Ayrışan şey DOLUMLARDIR, sinyaller değil — ve
bu ayrışmanın kendisi yönetimin bir sonucudur, ölçümün bir kusuru değil.

### Çıkış yönetimi tek bir modülde: `strategies/exit_management.py`

Üç model de aynı config bloğunu (`exit_management`) aynı sınıf üzerinden okur. Model 15 ile
`scalp_fixed` arasındaki ortalama R farkının "çıkış yönetiminin katkısı" olarak
okunabilmesi, yönetimin tek bir yerde tanımlı olmasına bağlıdır: üç dosyaya kopyalansaydı
bir gün birinin `partial_tp.r` değeri sessizce kayar ve fark "iki ayrı yönetimin farkı"
hâline gelirdi. Aynı gerekçe `strategies/vwap/signal.py` için de geçerli — model 13 ve 14
sinyali tek kopyadan görür.

### Model 14'ün hedefi neden VWAP ile kırpılıyor

Model 14'ün 1.5R kapısının canlı kalması için hedef, projeksiyon (`2.0 × stop`) ile
yapısal engelin (VWAP) YAKIN olanıdır — `strategies/scalp/arms.py`nin aynı gerekçesi.
Yalnızca projeksiyon kullanmak, oranı sabit bir sayıya çivileyip kapıyı ölü koda
çevirirdi. Model 13 ise yalnızca projeksiyon kullanır, çünkü kaynak sistemde böyle bir kapı
yok ve hedef çarpanı öğrenilen bir parametredir.

Model 14'ün stop ölçeği (`vwap.managed.atr_multiple = 5.0`) scalp kollarınınkiyle
(`scalp.stop_atr_multiple`) BİREBİR aynı tutuldu: ayrışsalardı 14 ↔ `scalp_fixed` farkı
kısmen maliyet ölçeği farkı olurdu (kural 14 / Rapor Kolonları) ve sinyal farkı gölgelenirdi.

### Kol etiketi: `arm=vwap_revert`

Scalp katmanı kol kırılımı üretir ve `core/metrics.py::arm_of` etiketsiz satırda `TagError`
fırlatır (kural: "her işlem bir kola aittir"). Model 13 ve 14'ün kolu yoktur ama etiketi
vardır: ikisi de `arm=vwap_revert` yazar. Alternatif — kırılımı bu modeller için atlamak —
kırılım toplamı ile model toplamını sessizce ayrıştırırdı.

> **Güncelleme (karar 23):** iki model artık aynı kurulumu aramadığı için etiketleri de
> ayrıldı — model 13 `arm=vwap_revert_src` yazar. Etiketin ZORUNLU olması değişmedi.

## 19. Telafi edilen barlarda sinyal (`signals_per_bar`) ve saatlik scalp cron'u

Ölçülen sorun: `.github/workflows/run-scalp.yml`in 15 dakikalık cron'u (`3,18,33,48 * * * *`)
14 saatlik bir pencerede 57 slot için **5** kez tetiklendi (~%9). Gerçekleşen turlar arası
aralık 1sa 54dk ile 3sa 17dk arasındaydı; tetiklenenler bile slot'undan 7-14 dakika
gecikmeliydi. GitHub'ın zamanlanmış tetiklemeleri en iyi çaba ilkesiyle çalışır ve kadans
sıklaştıkça düşme oranı artar.

Motor atlanan turları zaten telafi ediyordu (karar 17, `core/engine.py > _timeline`): son
işlenmiş bardan `as_of`'a kadarki her bar sırayla ilerletilir, dolayısıyla stop/TP/
likidasyon/funding hiçbir barda atlanmaz — `ledgers_scalp/*/equity.csv` bu yüzden
delikşizdi. Kaybolan şey **sinyaldi**: `generate_signals` yalnızca `as_of` barında
çağrılırdı, yani 96 sinyal fırsatının ~87'si hiç sorulmadan geçiyordu.

### Neden bu bir ölçüm hatasıdır, bir verim kaybı değil

Kayıp yalnızca "daha az işlem" demek olsaydı, çare beklemek olurdu. Ama:

- **Tablo modelin değil cron'un kadansını ölçer.** `acceptance.min_trades` (30) kapısına
  ulaşma hızı stratejinin işlem sıklığına değil, GitHub'ın o gün ne kadar tetiklediğine
  bağlanır.
- **Kayıp turdan tura değişir.** Hangi barların düştüğü rastgeledir; bir model şanslı
  barlarda, diğeri şanssız barlarda ölçülmüş olabilir. Model 11 ↔ 12 farkının (adaptasyonun
  katkısı) içine cron gürültüsü karışır — oysa katman tam olarak o farkı ölçmek için var.
- **Bandit doğrudan zarar görür.** Posterior kapanmış işlemlerden beslenir; işlem sayısı
  onda bire inince ısınma (kol başına 20 işlem) haftalarca tamamlanmaz ve model ömrünün
  büyük kısmını taban tahsiste geçirir — yani ölçülen şey "adaptasyon" olmaktan çıkar.

### Karar: her telafi barı kendi sinyalini üretir

`core/engine.py` turu artık bar bazında yürütür (`_advance_bar` + `_trade_step`) ve
`signals_per_bar` açıkken her bar için sinyal üretir. Doğruluk ölçütü bir **eşdeğerliktir**:
bir turda telafi edilen N bar, N ayrı turda koşulan N bar ile **birebir aynı defteri**
üretmelidir. `tests/test_engine_per_bar.py` bunu iki defteri karşılaştırarak ölçer —
`trades.csv`, `equity.csv` ve durum dosyası dâhil.

Eşdeğerliği taşıyan dört kural:

1. **Toplu değerlendirme yok.** Her bar için: sinyal üret -> emri kuyruğa al -> BİR SONRAKİ
   barın açılışından doldur (kural 13) -> o barın mum içi kontrolü. Barları birleştirip tek
   değerlendirme yapmak, ara barlarda tetiklenecek çıkışları yok saymak olurdu.
2. **Limitler bar bazında.** `max_positions` / `max_short_positions` her barın dolumunda
   yeniden sorulur. Tur başına uygulansaydı ya altı sinyalin hepsi tek kotadan geçer ya da
   hepsi birden düşerdi; ikisi de gerçek borsanın davranışı değil.
3. **Anlık görüntü barda kesilir (kural 12).** `_snapshot` her çerçeveyi o barda keser,
   funding serisini kırpar ve o barı TAŞIMAYAN sembolü o barın evreninden düşürür —
   `core/data.py`'nin `as_of` için uyguladığı kuralın aynısı. Sembolü düşürmemek, doğrulamada
   referans fiyat bulunamadığı için o barın tüm sinyallerini düşürürdü (kural 8): gecikmeli
   tek bir sembol koca bir barı sessizce boşa çıkarırdı. `as_of` barında kesme hiç yapılmaz,
   çünkü o görüntüyü `core/data.py` zaten bu kurallarla kurmuştur.
4. **`as_of`'tan sonrası işlenmez.** Kapanmamış bar ne sinyal ne özsermaye satırı üretir.

Modeller değişmedi: `ScalpModel` çekilişi zaten `random_seed` + `as_of` + model kimliği ile
tohumlar, yani her barın çekilişi ayrı ve tekrarlanabilirdir; model 12 ↔ 15'in paylaşılan
çekiliş kimliği (karar 18) bar bazında da aynı kolu seçmeye devam eder.

### Neden yalnızca scalp katmanında

Ayar kökte **kapalı**, `layers.scalp`'te açıktır. Base katmanının cron'u (`run.yml`, 6
saatte bir) güvenilir tetikleniyor, yani telafi orada nadiren devreye girer — ama devreye
girdiği turlarda defterin kuralı sessizce değişirdi: biriken geçmişin bir kısmı "tur başına
tek sinyal", bir kısmı "bar başına tek sinyal" ile üretilmiş olur ve iki dönemin işlem
sıklığı kıyaslanamazdı. Tek bir defterin tek bir kuralla yazılması, nadiren kazanılacak
birkaç sinyalden önemlidir. Katmanlar arası kıyas zaten yapılmaz; katman içinde ise beş
modelin hepsi aynı ayarı görür (kural 6).

### Cron: 15 dakikalık değil saatlik

`signals_per_bar` açıkken tetikleme sıklığı artık sinyal sayısını belirlemiyor — yalnızca
sonucun ne kadar gecikmeyle deftere yazıldığını belirliyor. Bu yüzden cron `3 * * * *`
oldu: saatlik tetiklemeler belirgin biçimde daha güvenilir ve her koşu aradaki dört barı
işliyor. Bedel yalnızca zamanlamadır (bir sinyal en çok bir saat sonra yazılır) ve ölçümü
değiştirmez, çünkü fiyatlar barın kendi fiyatlarıdır. Kazanç, hem düşen tetikleme sayısının
azalması hem de günde 96 yerine 24 commit.

Alternatif — 15 dakikalık cron'u bırakmak — reddedildi: telafi zaten aynı sonucu verdiği
için sık tetikleme yalnızca dört kat commit ve dört kat runner dakikası demek olurdu,
üstelik tetiklemelerin çoğu yine düşerdi.

## 20. İkinci sayfa: `docs/positions.html`, paylaşılan varlıklar ve çıkışın alt sebebi

Dashboard'ın (karar 15-16) cevapladığı soru "hangi model önde"dir: özet, kabul rozetleri,
eğriler, model detayı. Cevaplamadığı soru ise **"şu anda tam olarak ne açık ve bugüne
kadar tam olarak ne kapandı"**dır — açık pozisyonun margin'i, riske edilen tutarı, birikmiş
funding'i, çıkış yönetiminin hangi aşamasında olduğu; kapanmış işlemin kaldıracı,
komisyonu, çıkış sebebi. Bunlar ölçümün sonucunu değiştirmez ama denetlenebilirliğin
kendisidir: defterdeki satırı okunur hâlde görmenin tek yolu.

Bu bilgiyi model detayının içine sıkıştırmak iki şeyi bozardı: (a) detay sayfası "bu model
nasıl işlem yapıyor" sorusuna odaklıdır ve 15 kolonluk bir tablo onu boğardı; (b) karşılaştırma
tek modelle yapılmaz — "hangi modelde kaç pozisyon açık" sorusu tüm modellerin aynı tabloda
olmasını gerektirir. Bu yüzden ikinci bir sayfa: `docs/positions.html`.

### İki katman aynı sayfada, tek istatistikte değil

Sayfa iki yükü de okur (`data/metrics.json` + `data/metrics_scalp.json`) ve satırları tek
listeye indirger — ama **katman etiketi her satırda durur** ve özet şeridi yalnızca AKTİF
FİLTREYE göre hesaplanır. Katmanlar arası kıyas yapılmaz (CLAUDE.md > Katmanlar); iki
katmanın ortalama R'sini tek bir sayıda toplamak, tam da o yasağı sessizce delmek olurdu.
Bir defter görünümünde ise satırların yan yana durması bir kıyas iddiası değildir: "şu an
ne açık" sorusunun cevabı zaten iki katmanın toplamıdır.

### Kısmi çıkışlar görünür ama sayılmaz

`exit_reason="partial"` satırları tabloda **ayrı satır** olarak durur (defterde de öyle
durur; kural 1) ama sayfanın istatistiğine **girmez** ve bu tablo başlığında yazar. Kısmi
çıkış tamamlanmış bir işlem değildir: hâlâ açık bir pozisyonun bir dilimidir ve pozisyon
kapandığında aynı kurulum ikinci kez satır üretir. Saymak, aynı pozisyonu iki kez ölçüme
sokar ve kazanma oranını yapay olarak yukarı çekerdi — kısmi çıkış tanımı gereği kârda
gerçekleşir. Gizlemek de olmazdı: o satır gerçekten deftere yazılmış bir nakit hareketidir.

### Ortak varlıklar: `shared.css` + `shared.js`

İki sayfa da aynı ölçümü çizer. Biçimlendirme ölçümün parçasıdır, süs değil: aynı sayının
bir sayfada "—", diğerinde "0.00" görünmesi okuyucuya iki ayrı ölçüm gibi gelir. Bu yüzden
renk paleti, sayı hizası, kart/tablo düzeni, dokunma hedefi tabanı ve tüm biçimleyiciler
(`num`, `price`, `qty`, `usd`, `fullTs`, çıkış etiketleri, renk/kimlik ataması) tek kopya
olarak kardeş dosyalarda durur. Sayfaya özgü görseller (eğri grafiği, korelasyon ısı
haritası, model kartları, açık pozisyon rozetleri) kendi dosyalarında kalır.

Kopyalamak (iki dosyaya aynı CSS/JS) reddedildi: bir gün birinin sessizce ayrışması ve
aynı defterin iki sayfada farklı okunması demekti. Bir bundler/framework de reddedildi —
"sıfır bağımlılık, build adımı yok" kuralı (karar 15) duruyor: iki ek `<link>`/`<script>`
etiketi GitHub Pages'te build gerektirmez.

### Kırılım: 480px

Dashboard'ın tablo/kart eşiği 1060px'tir (11 kolon o genişlikte kırpılmadan sığıyor). Bu
sayfanın tablosu 15 kolondur ve hiçbir genişlikte "tam sığdı" demek mümkün değil. Karar:
**480px altında kart düzeni** (hiçbir sayı kırpılmaz, sayfa yatay kaymaz), üstünde tablo —
ama tablo KENDİ konteynerinde kayar, sayfa değil. Sayfayı kaydırmak kritik kolonu (K/Z)
ekranın dışında bırakır ve sayfa onu hiç göstermemiş olur; tabloyu kaydırmak ise okuyucunun
bildiği bir hareket ve hiçbir sayıyı gizlemez.

### `exit_rule`: `exit_reason`'ın taşıyamadığı ayrım

Sayfa "çıkış sebebi" kolonunu isteyince ortaya çıktı ki defter bu soruyu **cevaplayamıyor**.
`exit_reason` beş kaba koddur (`liquidation`/`stop`/`partial`/`tp`/`signal`) ve ikisi
birleşiktir:

- `stop` hem ilk stop'u hem takip eden stop'u (ATR trailing, breakeven, geri verme, kısmi
  çıkışın çektiği stop) anlatır,
- `signal` hem scalp katmanının **zaman stop'unu** hem başka bir strateji çıkışını anlatır.

Oysa modeller 13/14/15'in ölçtüğü şey tam olarak **yönetimin katkısıdır**: "takip eden
stop'un aldığı işlem" ile "ilk stop'un aldığı işlem" aynı satıra çökerse o katkı defterden
okunamaz. Ayrım sonradan geri hesaplanamaz — deftere yalnızca İLK stop yazılır
(`stop_price` kolonu, R'nin paydası olduğu için) ve stop'un sonradan nereye çekildiği
yalnızca kapanış anında bilinir.

Çözüm iki parçalı ve dar tutuldu:

1. `OpenPosition.stop_rule` — stop'u EN SON hangi kuralın taşıdığı, hareket anında
   yazılır (`core/portfolio.py::_tighten_stop`, kuralı veren `core/engine.py::_update_stops`).
   Yalnızca stop GERÇEKTEN hareket ettiğinde yazılır: reddedilen bir hareketin kuralını
   saklamak, işlemi hiç uygulanmamış bir yönetimle etiketlemek olurdu.
2. Kapanışta `| exit_rule=<kural>` etiketi `notes` kolonuna düşer (`core/tags.py`'nin tek
   format tanımı). Strateji çıkışlarında kural talimatın kendi etiketinden gelir
   (`ExitInstruction.reason` içindeki `exit_rule=time_stop`).

**Neden yeni bir kolon değil:** `trades.csv` başlığı değişirse eski satırlar okunamaz hâle
gelir (`core/ledger.py::_assert_header`) ve defterin elle taşınması gerekir. `notes` zaten
serbest bir kuyruk ve etiket formatı zaten tek yerde tanımlı.

**Neden "ilk stop aldı" için etiket yok:** etiketin YOKLUĞU o bilgiyi taşır. Uydurma bir
`exit_rule=initial` yazmak, hiç hareket etmemiş bir stop'u bir yönetim kararıymış gibi
gösterirdi.

### Yeni rapor alanları

`core/report.py` açık pozisyon satırına şunları ekledi: `risk_amount`, `margin`,
`initial_qty`, `breakeven_at_r`, `breakeven_done`, `partial_tp`, `partial_done`,
`trail_giveback_pct`, `trailing_active`, `stop_rule`, `stop_moved`; kapanmış işlem satırına
`leverage`, `margin`, `exit_rule`, `is_partial`, `notes`.

İki ayrım bilinçli:

- **İstek ile OLAY ayrı taşınır.** `breakeven_at_r` modelin açılışta bildirdiği isteği,
  `breakeven_done` stop'un gerçekten girişe çekilmiş olduğunu söyler. Tek alana indirmek,
  mekanizmayı bildiren ama henüz tetiklenmemiş bir pozisyonu "yönetildi" gibi gösterirdi.
- **`breakeven_done` stop'un KENDİSİNDEN okunur**, "pozisyon o R'a ulaştı mı" hesabı burada
  tekrarlanmaz: `core/report.py` salt okunurdur ve motorun kuralını ikinci kez yazmak,
  ikisinin bir gün sessizce ayrışması demekti (tek uygulayıcı `core/engine.py::_breakeven_stop`).

`risk_amount` ile `margin` ayrı kolonlardır ve biri diğerinden türetilemez: marj
notional/kaldıraçtır, risk ise ilk stop'a olan mesafedir (kural 11'in paydası). "Ne kadarı
bağlı" ile "ne kadarı riskte" aynı soru değildir.

---

## 21. Ölçümün birimi: dolum değil, POZİSYON

Karar 20 sayfaya "kısmi çıkışlar görünür ama sayılmaz" ilkesini yazdı ve bunu
`docs/positions.html` içinde uyguladı. Denetim (`@yapisal-denetci`, PR #20 sonrası) aynı
ilkenin **ölçümün kendisinde uygulanmadığını** buldu: `core/metrics.py::direction_stats`
`trades.csv`'nin TÜM satırlarını bir işlem sayıyordu. İki yüzey açıkça çelişiyordu ve aynı
model iki sayfada iki farklı kazanma oranı gösterecekti.

### Sorun `exit_reason == "partial"`den büyük

`trades.csv` bir **dolum** defteridir: `core/portfolio.py::_close` `fraction_of_initial`
kadarını kapatır ve o dilim için bir satır yazar. Bir pozisyonun birden çok satır üretmesinin
İKİ yolu var ve yalnızca biri "partial" kodunu taşıyor:

| Yol | `exit_reason` | Kullanan |
|---|---|---|
| Üç aşamalı çıkışın kısmi dolumu | `partial` | modeller 13, 14, 15 (`exit_management`) |
| `TakeProfit.fraction < 1.0` | `tp` | `avwap` (0.5 + 0.5), `downtrend_rally` (0.5) |

Yani `exit_reason == "partial"` filtresi scalp katmanını düzeltir ama **ana yarışmadaki iki
yarışmacıyı** (`avwap`, `downtrend_rally`) çift saymaya devam ederdi. Kodun ayırt etmesi
gereken şey çıkış sebebi değil, **pozisyon kimliği**dir.

### Neden kısmi satırı atmak da yanlış olurdu

İlk akla gelen düzeltme kısmi satırları elemekti. O yol ölçümü **ters yönde** bozardı.
Somut örnek — giriş 100, ilk stop 99, 100 adet (1R = 100 USDT), kısmi çıkış 1.5R'da yarı
boyutla, kalan dilim 3R hedefinde:

| Satır | `pnl` | `risk_amount` | satırın R'si |
|---|---|---|---|
| kısmi (1.5R) | +75 | 50 | 1.5 |
| kalan (3R) | +150 | 50 | 3.0 |
| **pozisyon** | **+225** | **100** | **2.25** |

- **Hepsini saymak:** 2 işlem, kazanma oranı %100, örneklem kapısı iki kat hızlı geçilir.
- **Kısmiyi atmak:** 1 işlem ama R = **3.0** — kilitlenen 75 USDT ölçümden düşer.
  Kalan dilim başabaşa çekilmiş stop'ta kapansaydı daha da kötü: pozisyonun gerçek R'si
  +0.75 iken ölçüm **0.0** derdi. Yönetimli model (15), yönetimsiz ikizine (12) karşı
  haksızca kötü görünürdü — ki 12 ↔ 15 ekseninin ölçtüğü şey tam olarak yönetimin katkısı.
- **Toplamak:** 1 işlem, R = 225/100 = **2.25**. Doğru olan bu.

### Karar

`core/metrics.py::merge_fills` dolum satırlarını pozisyon başına tek ölçüm satırına
indirger. Kimlik `strategy + symbol + direction + opened_at`; `strategy` şarttır çünkü
havuz (`pooled_direction_stats`) birden çok modelin satırını tek listede birleştirir ve
onsuz iki modelin aynı sembolde aynı barda açtığı pozisyonlar tek pozisyon sanılırdı
(kural 4'ün izolasyonu ölçümde delinirdi).

- **Nakit kolonları TOPLANIR, atılmaz** (`pnl`, `fee`, `slippage_cost`, `funding`):
  "Σpnl = bakiye değişimi" değişmezi böyle korunur. Denetim izi (`trades.csv`) zaten
  dokunulmazdır (kural 1); birleştirme yalnızca OKUMA tarafındadır.
- **Kapanış alanları pozisyonu KAPATAN son dolumdan gelir** (`closed_at`, `exit_reason`,
  `notes`). Ara dilimin sebebini pozisyonun sebebi saymak, kısmi çıkışla kapanmış gibi
  görünen bir işlem üretirdi — likidasyonla kapanan bir pozisyon "kısmi çıkış" görünürdü.
- **`opened_at` taşımayan satır kendi başına bir pozisyondur.** Bilinmeyen kimliği ortak
  kabul edip hepsini tek pozisyonda toplamak sessiz bir veri kaybı olurdu.
- Kapı tek yerdedir: `direction_stats`. `breakdown` (kol/sembol) ve `pooled_direction_stats`
  onu çağırdığı için kırılımlar da pozisyon birimiyle çalışır; kol ve sembol pozisyonun
  özellikleri olduğu için bir pozisyonun tüm dilimleri zaten aynı gruba düşer.

**Yan kazanç:** `cost_per_r` artık tanımına uyuyor. CLAUDE.md "işlemin TÜM dolumlarında
ödenen komisyon + kayma" der; satır bazında hesaplandığında bu yalnızca o dilimin
maliyetiydi. Birleştirilmiş satırda hem pay hem payda pozisyonun tamamını taşır.

### Sayfa hesaplamayı bıraktı (kural 7)

Aynı denetim `docs/positions.html`'in ortalama R'yi ve kazanma oranını **JS'te kendi
kuralıyla** hesapladığını buldu. İki hesap yolu bugün hizalansa bile yarın ayrışır —
birinin kısmi çıkışı sayması yeter — ve JS tarafında hiçbir test yoktur. `docs/shared.css`'in
tek kopya olma gerekçesi burada da geçerli: aynı sayının iki sayfada farklı görünmesi
okuyucuya iki ayrı ölçüm gibi gelir.

Karar: sayfa ölçümü **yükten okur** (`docs/shared.js::statsSlice`, iki sayfanın da tek
yolu). Model seçiliyse `models[<ad>][<yön>]`, seçili değilse havuz (`pooled`) okunur —
havuz yalnızca yarışmacıları içerir (kural 15/15b) ve kart bunu SÖYLER, çünkü tablo
çıpanın ve kopyanın satırlarını yine gösterir.

Yükün cevaplayamadığı filtreler (sonuç, tarih aralığı) yalnızca **tabloyu** daraltır;
ölçüm defterin tamamından gelir ve bu da yazılır. Sessiz kalmak, okuyucuya "seçtiğim
aralığın ortalaması" dedirtirdi. Dolum satırları listede kalmaya devam eder ve "DİLİM"
rozetiyle "istatistiğe dahil değil" der — KAZANÇ/KAYIP yazmak, kârda gerçekleşen bir
kısmi çıkışı tamamlanmış bir kazanç gibi gösterirdi.

### Özet şeridi katman başına ayrı

`view.layer` varsayılanı `"all"`dır ve eski şerit o filtreyle gelen satırların tamamından
**tek** bir ortalama R üretiyordu: sayfa açılışta 4 saatlik ve 15 dakikalık katmanın
R'lerini tek hücrede topluyordu. Kod "aktif filtreye göre" hesaplıyordu ama varsayılan
filtre katman sınırı çizmiyordu — dashboard'ın ayrı tablolarla özenle engellediği kıyas,
defter sayfasının ilk ekranında yapılmış oluyordu (karar 20'nin kendi ilkesinin ihlali).

Karar: şerit **katman başına ayrı bir kart** verir ve kartlar alt alta durur. `layer=all`
dâhil hiçbir görünümde iki katmanın R'si tek sayıda toplanmaz. Yan yana koymak da
yapılmadı: aynı satırda duran iki sayı kıyaslanacak bir çift gibi okunur.

### Ne yeniden hesaplanır

`docs/data/metrics*.json` defterin **saf bir türevidir**: `compare()` her turda
`trades.csv`'yi baştan okur ve `_write_metrics` dosyayı bütünüyle yeniden yazar. Yani
geçmiş metrikler "eski kuralla yazılmış" olarak kalmaz — bir sonraki turda TÜM geçmiş yeni
kuralla yeniden hesaplanır. Göç adımı, elle düzeltme ve `trades.csv`'ye dokunma yoktur
(kural 1). `equity.csv` de etkilenmez: birleştirme nakit hareketini değiştirmez, yalnızca
aynı nakdin kaç işlem olarak sayıldığını değiştirir.

---

## 22. Scalp katmanının anlık sinyal bildirimi

`scripts/telegram_report.py` **günde bir kez** koşar ve bir PERFORMANS özetidir: hangi
model önde, kabul çıtası geçildi mi, long/short farkı ne. Okunması zamana bağlı değildir —
aynı özet üç saat sonra da aynı şeyi söyler.

Scalp katmanının sorusu bambaşkadır: **"şu anda ne açılıyor"**. Cevabın raf ömrü bir
bardır (15 dakika), yani günlük özetin kadansına sığmaz. Bu yüzden ayrı bir script, ayrı
bir workflow adımı ve ayrı bir tetikleyici olay. `run.yml`in günlük özetine
DOKUNULMAMIŞTIR: ikisi farklı soruları, farklı kadanslarda, farklı rapor dosyalarından
cevaplar.

### Tetikleyen tek olay: yeni sinyal

Kapanış, funding tahakkuku ve bar ilerlemesi mesaj ÜRETMEZ. Hepsi zaten defterde ve
dashboard'da durur ve hiçbiri "şimdi bak" demez. Kapanış bildirimi üstelik aktif bir zarar
da verirdi: scalp katmanı günde onlarca pozisyon kapatır, akış okunamaz hâle gelir ve
gerçekten zamana bağlı olan tek mesaj — açılan pozisyon — gürültünün içinde kaybolurdu.

### Kaynak neden defter değil, tur raporu

Sinyalin haber değeri olduğu an, defterde henüz **hiçbir satırı yoktur**: emir bir sonraki
barın açılışında dolar (kural 13) ve ancak kapandığında `trades.csv`'ye yazılır.
`state.json`'ın `pending_orders` alanı ise yalnızca turun son barından sonrasını taşır ve
sinyalin hangi barın KAPANIŞINDA üretildiğini, o kapanışın fiyatını ya da band elemesinden
(kural 14) geçip geçmediğini bilmez.

Bu yüzden kayıt motorda tutulur (`core/engine.py::EmittedSignal`) ve tur raporuna düşer —
`rejections` ile aynı statüde bir **denetim izi**. Ölçüme girmez: metrikler defterden
hesaplanmaya devam eder, kayıt yalnızca `docs/data/metrics_*.json > round.models[].emitted`
altında durur ve hangi sinyalin üretileceğini ya da sıralarını etkilemez.

Kayıt `bar` ile `fills_at`i AYRI taşır. İkisi de turun `as_of`'undan farklı olabilir
(`signals_per_bar`, bkz. 19) ve "bu sinyal hangi barın kapanışına ait" sorusunun cevabı
sonradan geri hesaplanamaz — tam olarak aşağıdaki ilk filtrenin dayandığı bilgi budur.

### Üç filtre

**1. Yalnızca SON barın sinyalleri.** Saatlik cron her turda dört 15m barını işler ve
telafi edilen barlar da kendi sinyallerini üretir (bkz. 19). O sinyaller deftere yazılır ve
ölçüme tam olarak girer — ama BİLDİRİLMEZ. Gerekçe ölçüm değil, mesajın kendisidir: 45
dakika önceki bir barın emri çoktan dolmuştur ve mesaj okuyucuyu artık girilemeyecek bir
işleme yönlendirirdi. Bildirimin susması burada bir kayıp değil, doğru davranıştır.

**2. Aynı (model, sembol, yön) için 4 bar susturma.** Aynı kurulumu üst üste barlarda
öneren bir model (ya da elle tetiklenip `as_of`'u birkaç bar ilerleten bir koşu) aynı
mesajı tekrar tekrar yollardı. Durum `state/telegram_scalp.json`'da tutulur ve koşular
arası commit edilir; runner her koşuda sıfırdan kurulduğu için commit edilmezse pencere
her turda kaybolurdu. Dosya defterden AYRI durur: defter denetim izidir (kural 1), bu ise
silindiğinde ölçümün hiç değişmediği, en kötü ihtimalle bir mesajı tekrarlatan bir
kolaylıktır. Bozuk ya da eksik bir durum dosyası bu yüzden bildirimi SUSTURMAZ — pencere o
tur uygulanmaz ve mesaj gider.

**3. Beşten fazla sinyalde tek toplu mesaj.** Volatil bir barda beş model birden sinyal
üretebilir; tek tek yollamak bildirim akışını kullanılamaz hâle getirirdi. Toplu mesajda
`reason` metni yer almaz: altı gerekçe tek başına Telegram'ın 4096 karakterlik sınırını
aşabilir ve gerekçe zaten dashboard'da durur.

### Zorunlu uyarı satırı

Her mesaj şu satırla biter: *"Bot bu emri bir sonraki bar açılışından dolduracak
(&lt;bar+15dk&gt; UTC). Senin girişin farklı bir fiyattan olacak."*

Bu satır opsiyonel değildir. Bildirim, sinyalin üretildiği barın KAPANIŞINDA gider; emir
ise bir SONRAKİ barın açılışından dolar (kural 13) ve o fiyat mesaj yazılırken henüz
bilinmemektedir. Uyarı olmadan mesaj, defterde ölçülen sonucun okuyucu tarafından
tekrarlanabileceği izlenimini verirdi — oysa proje bir ÖLÇÜM projesidir (CLAUDE.md >
Amaç), bir sinyal servisi değil.

### Bildirim ölçümü düşüremez

`scripts/telegram_report.py` ile birebir aynı söz: eksik token, ağ hatası, Telegram 4xx'i,
bozuk JSON ya da bozuk durum dosyası loglanır ve geçilir; her yol **0 ile biter**. Workflow
tarafında da aynı iki emniyet kemeri var (`continue-on-error` + `if: always()`) ve adım
defter commit'inden SONRA gelir — burada ne olursa olsun tur çoktan kaydedilmiştir.

Durum dosyasının commit'i de bu yüzden AYRI bir adımdır. Onu defter commit'ine katmak,
bildirim adımını (yani ağ erişimi olan tek adımı) turun kaydedilmesinin ÖNÜNE koymayı
gerektirirdi. Ayrı ve `continue-on-error` bir adım, en kötü ihtimalde tekrar eden bir
mesaja mal olur; ölçüme hiç dokunmaz.

Bir mesaj yollanamadıysa durum dosyası da güncellenmez: yollanmamış bir mesajı "bildirildi"
saymak, susturma penceresi boyunca o bildirimi sessizce kaybetmek olurdu.

## 23. Model 13 SADIK kopyaya çevrildi: kaynağın kuralları, kendi sinyal modülünde

Model 13, kaynak sistemin (klonnist/Hasanwavebot) kurallarını yeniden ürettiğini iddia
ediyordu ama kaynak kodu satır satır okunduğunda **sinyal ve öğrenme katmanının
kaynağınkiyle uyuşmadığı** görüldü. Hesap katmanı (sabit teminat × 10x, 5 pozisyon, yönde
3, portföy riski %8, üç aşamalı çıkış yönetiminin dört parametresi) birebir oturuyordu;
ayrışma tam olarak "kopya ne yapar" sorusunun cevabındaydı:

| | Kaynak (`vwap_detector.py` + `learner.py`) | Model 13 (önce) |
|---|---|---|
| VWAP | son 300 barın KÜMÜLATİFİ, çapa her koşuda kayar | gün-çapalı |
| σ | `dist.rolling(20).std()`, ağırlıksız, ddof=1 | hacim ağırlıklı, ddof=0, gün penceresi |
| Bant dışı olan bar | MEVCUT bar | ÖNCEKİ bar |
| Dönüş şartı | yalnızca `close > prev_close` | sapma daralmış + VWAP geçilmemiş |
| Minimum bar | 25 bar, gün kavramı yok | gün çapasından ≥ 8 bar |
| Stop | `band_mult × sl_mult × σ` (`sl_mult` sabit 0.5) | öğrenilen ATR çarpanı |
| Hedef | `max(\|VWAP − giriş\|, 0) × tp_mult`, `tp_mult ≤ 1` | `target_reward_risk × stop` |
| Öğrenilen eksenler | `band_mult` × `tp_mult` = 9 kombinasyon | `atr_multiple` × `target_reward_risk` = 12 |
| Isınma | o sembolde denenmemiş kol ÖNCE | yok |
| Tarama sırası | sembol listesi sırası | sapma gücüne göre |

Sonuç şuydu: model 13 "dış sistem bizim varsayımlarımızla ne yapardı" sorusunu değil,
"kaynağa benzeyen bir sinyal, ev-dışı ama ev-icadı kurallarla ne yapardı" sorusunu
cevaplıyordu. Model 13 ↔ 14 ekseninin ("ev kurallarının katkısı") her iki ucu da ev
yapımıydı, yani eksen boştu.

Bu kararla yukarıdaki sekiz satırın tamamı kaynağın hâline çevrildi.

### Neden ayrı bir dosya: `strategies/vwap/clone_signal.py`

Alternatif, `strategies/vwap/signal.py`'ye bir bayrak (`source_rules=True`) koyup her
kuralı dallandırmaktı. Daha az dosya, daha çok risk:

- **Ölçülen fark, dallanmanın durumuna bağlı hâle gelirdi.** 13 ↔ 14 ekseni "ev
  kurallarının katkısı"nı ölçer; o katkının tanımı, sekiz `if`in hangi dalının hangi model
  için açık olduğuna bağlı bir şey olamaz.
- **Bir gün biri diğerinin dalını değiştirirdi.** Ortak bir modülde model 14 için yapılan
  masum bir düzeltme (ör. σ tanımını iyileştirmek) kopyayı sessizce kopya olmaktan
  çıkarırdı — ve hiçbir test bunu yakalamazdı, çünkü iki dal aynı testlerden geçerdi.
- **Kaynağın "kusurları" korunamazdı.** Kopyada σ hacim ağırlıksızken VWAP ağırlıklıdır,
  `z` typical price'tan okunurken dönüş kapanıştan okunur, `std_window` parametre değil
  sabittir. Bunlar ortak bir modülde birer bug gibi görünür ve er geç "düzeltilir".

Bu yüzden kural mantığı PAYLAŞILMAZ, aritmetik paylaşılır: iki modül de
`core/indicators.py`'nin `typical_price` ve `bars_until` yardımcılarını kullanır.
`signal.py`'ye bu kararda **hiç dokunulmadı**; model 14'ün gördüğü adaylar, sıraları ve
seçimi bit düzeyinde aynıdır (tek değişiklik, aşağıdaki sayım kaydı için `propose` yerine
onun sessiz ikizi `scan`ın çağrılmasıdır — ikisi aynı listeyi döndürür).

### Kol etiketi ayrıldı: `arm=vwap_revert_src`

İki model artık aynı kurulumu aramıyor. Aynı `arm` etiketini yazmaya devam etselerdi kol
kırılımı (`core/metrics.py::breakdown`) iki farklı kural kümesini tek bir ad altında
gösterir ve okuyucu iki satırı "aynı kolun iki modeldeki hâli" sanırdı. Kırılım model
BAZINDA üretildiği için bugün sayısal bir karışma olmazdı — ama etiketin işi sayıyı
karıştırmamak değil, satırın ne olduğunu söylemektir.

### Evrenden TON çıkarıldı: kopya 12 sembol

TON-USDT-SWAP OKX'te kalıcı olarak yok (51001) ve her turda veri çekiminden düşüyor.
Kaynak listede 13 sembol yazıyor, ama listede tutmak **hiç taranmayan bir sembolü
taranıyormuş gibi göstermek** olurdu: `survey` sayımında her tur `bar_yok` olarak
görünür ve "kopyanın evreni kaç sembol" sorusunun iki farklı cevabı olurdu.

Katmanın kendi evreni (14) DEĞİŞTİRİLMEDİ ve bu bilinçlidir: oradan bir sembol çıkarmak
modeller 11/12/15'in çekiliş uzayını değiştirir, yani ölçülmeyen bir eksende üç modelin
geçmişiyle geleceğini ayrıştırırdı. Katman ile kopya arasındaki fark artık iki semboldür
(SUI ve TON) ve ikisinin gerekçesi farklıdır: SUI kaynak sistemde hiç yok, TON borsada
yok.

### Sadık kopyanın sınırları

Bir kopya, **kopyalanamayacak şeyleri de yazmak zorundadır.** Aşağıdaki beş sapma
düzeltilmeyecektir ve her biri sonucu belirli bir yöne kaydırır; kopyanın satırı
okunurken bunlar bilinmelidir.

**C1 — Kaynak kapanmamış barı kullanır, kopya kullanmaz (kural 12).** Kaynağın
`data_feed.fetch_ohlcv`i ccxt çıktısını olduğu gibi alır; son satır OLUŞMAKTA OLAN bardır.
Yani kaynağın `z`si, dönüş kontrolü ve girişi canlı fiyattan hesaplanır. Burada tüm barlar
kapanmıştır. **Etkisi:** kaynak aynı kurulumu bar içinde, kopya bar kapanışında görür;
kopya bazı kurulumları hiç görmez (bar içinde bant dışına çıkıp kapanışta içeri dönenler)
ve bazılarını farklı bir `z` ile görür. Bu kural tek bir modeli değil TÜM ölçümü geçersiz
kılabilecek bir kuraldır (kural 12) ve kopya için de esnetilmez.

**C2 — Kaynak anlık fiyattan girer, kopya bir sonraki barın açılışından (kural 13).**
Kaynakta `entry = last_close` ve pozisyon o anda o fiyattan açılır. **Etkisi:** kopyanın
gerçek giriş fiyatı, sinyalin doğduğu kapanıştan farklıdır (üstelik komisyon ve kayma da
eklenir). Dönüş barının ertesinde devam eden bir harekette kopya daha kötü, geri çekilen
bir harekette daha iyi girer. Ortalamada bu bir maliyet kalemidir ve kaynağın kâğıt
hesabında yoktur.

**C3 — Tarama kadansı farklıdır.** Kaynak 15 dakikada bir GitHub Actions koşusuyla tek bir
tarama yapar; scalp katmanı saatte bir koşar ve dört 15m barını sırayla işler
(`signals_per_bar`, karar 19). **Etkisi:** kopya, kaynağın düşen tetiklemelerinde
kaçırdığı barları da tarar — yani kopya, kaynaktan DAHA ÇOK kurulum görür. Ters yönü
düzeltmek (bazı barları kasten atlamak) ölçümü cron'un kadansına bağlardı ve karar 19'un
tam tersi olurdu.

**C4 — `as_of` tüm semboller için ortaktır, kaynakta her sembol kendi fetch anında
değerlenir.** Kaynak `POPULAR_COINS`i sırayla gezerken her sembol için ayrı bir HTTP
isteği yapar; listenin sonundaki sembol, başındakinden saniyeler sonraki bir "şimdi"yi
görür. Burada tek bir anlık görüntü vardır (karar 4). **Etkisi:** kopyanın sembolleri
arasında zaman tutarlılığı kaynaktakinden İYİDİR; bu bir sapma ama ölçümün lehine bir
sapmadır ve kural 5'in ("tüm stratejiler aynı anlık görüntüyü görür") gereğidir.

**A6 — Öğrenme durumu deftere bağlıdır, ayrı bir JSON dosyasına değil.** Kaynak
`learner_state.json` ve `..._by_symbol.json` dosyalarını tutar ve commit eder; burada
posterior her turda `trades.csv`den sıfırdan kurulur. **Etkisi:** sayısal olarak aynı
sonucu verir (aynı işlemler, aynı R'ler, aynı ortalamalar) — fark dayanıklılıktadır:
ikinci bir durum dosyası defterle ayrıştığında hangisinin doğru olduğu bilinemezdi
(`scalp_bandit` ile aynı gerekçe). Tek gözlemlenebilir fark, kaynağın durum dosyası
silindiğinde öğrenmeyi kaybetmesi, kopyanın kaybetmemesidir.

**Bir de motorun tek ev kapısı kopyaya uygulanmaya devam eder:** stop mesafesi tavanı
(kural 14, scalp katmanında 8×ATR, `core/engine.py::_within_stop_band`). Kaynakta böyle
bir tavan yoktur. Kapı model düzeyinde değil MOTOR düzeyinde tanımlıdır ve onu kopya için
delmek, "hangi modellerin hangi motor kurallarından muaf olduğu"nu model başına bir
listeye çevirirdi — kural 6'nın engellemek istediği şeyin ta kendisi.

Kapının pratikte bağlayıp bağlamayacağı **canlı veriyle ÖLÇÜLMEDİ** (bu kararın alındığı
ortamda borsaya erişim yoktu). Yerine 1 dakikalık geometrik Brown hareketinden 15m OHLC
üretilip σ/ATR oranına bakıldı — gerçek veri değil, bir MERTEBE TAHMİNİ: yıllık %30-%80
oynaklık ve sıfırdan %500'e kadar sürüklenme altında stop mesafesi 0.9-1.6×ATR
(p95 ≈ 2.1×) çıkıyor, yani 8× tavanının çok altında; 120 denemenin hiçbirinde tavan
aşılmadı. Sebep, σ'nın sapmanın SEVİYESİ değil 20 barlık penceredeki DEĞİŞKENLİĞİ olması:
trend σ'yı şişirmiyor, çünkü kümülatif VWAP'ten uzaklaşma pencere içinde düzgün ilerliyor.
Gerçek veride bağladığı turlar `skipped_signals` kolonunda görünür; orada sıfırdan farklı
bir sayı çıkarsa bu karar yeniden açılmalıdır.

Aynı ölçüm bir başka şeyi de öngörüyor ve o beklenen bir sonuçtur, kusur değil: kopyanın
stop mesafesi normal oynaklıkta fiyatın **%0.4-0.9'u** kadardır, yani evin %1'lik stop
tabanının ALTINDA. Tur maliyeti ~%0.25 olduğuna göre kopya R başına 0.25-0.6R maliyet
öder; model 14 ise tabanı uyguladığı için en fazla 0.25R. Kopyanın `cost_per_r` kolonu bu
yüzden `nan`dır (kural 15b: kıyaslanamaz) ama fark hesap düzeyi getirisinde görünecektir —
ve "ev kapılarının katkısı" sorusunun cevabının önemli bir parçası tam olarak budur.

### Ev kapılarının kopyaya uygulanmadığı yeniden teyit edildi

%1 stop tabanı, 1.5R hedef/stop kapısı ve 16 barlık zaman stop'u kaynak sistemde yoktur ve
kopyaya geçmez (karar 18'de alınan karar; burada değişmedi). Yeni kural setiyle birlikte
bu artık ÖLÇÜLEBİLİR bir fark: kopyanın hedefi VWAP mesafesinin kesridir, yani R:R sık sık
1.5'in altına düşer — `tests/test_vwap_clone_signal.py::test_the_reward_risk_is_emergent_not_imposed`
tam olarak bunu çiviler. Ev kapısı sızsaydı o kurulumların tamamı elenirdi ve iki model
arasındaki fark yeniden görünmez olurdu.

### `Survey` sayımı tur raporuna taşındı

Sayım (hangi sembol hangi sebeple elendi) yalnızca koşu logunda duruyordu ve GitHub
Actions logları siliniyor. Yeni bir sözleşme kancası eklendi — `Strategy.take_survey()`,
varsayılanı `None` — ve motor her BARDAN sonra okuyup tur raporuna topluyor
(`ModelReport.survey`, oradan `docs/data/metrics_*.json > round.models[].survey`).

- **Neden `rejections`ın yanında ayrı bir alan:** `rejections` "emir neden dolmadı"yı,
  `survey` "sinyal neden hiç üretilmedi"yi sayar. İkisi turun iki ayrı aşamasıdır ve tek
  bir sayıya çökerse model 13'ün `signals=0` geçtiği bir tur, "bugün kurulum yoktu" ile
  "sinyal modülü sessizce bozuldu" arasında ayrım bırakmaz. Model 13'ün hiçbir ev kapısı
  olmadığı için bu ayrım onda özellikle kritiktir.
- **Bar bazında TOPLANIR, son barınki saklanmaz.** `signals_per_bar` açıkken bir tur dört
  bar işler ve dördü de ölçüme girer; yalnızca sonuncusunun sayımını tutmak, telafi edilen
  barlarda kolun ne gördüğünü kaydın dışında bırakırdı.
- **Patlayan bir `generate_signals`ın sayımı kaydedilmez** (kural 8): yarım bir sayım,
  "bu barda şu kadar sembol incelendi" satırını yanlış yapardı.
- **Ölçüme girmez.** `emitted` ve `rejections` ile aynı statüdedir: metrikler defterden
  hesaplanmaya devam eder, sayım hangi sinyalin üretileceğini ya da sıralarını hiçbir
  biçimde etkilemez. Sözleşme bunu açıkça yazar.

Model 14 de kancayı uygular. Bunun için `generate_signals` artık `vwap_signal.propose`
yerine `vwap_signal.scan` çağırıyor ve log satırını kendisi yazıyor: ikisi aynı aday
listesini döndürür (`propose` = `scan` + tek bir log satırı) ve log metni `Survey.describe`
ile tek kaynaktan gelmeye devam eder. Modelin davranışı değişmedi; değişen tek şey sayımın
bir DEĞER olarak elde kalmasıdır.

### Defter sıfırlaması gerekmedi

`ledgers_scalp/vwap_clone/trades.csv` yalnızca başlık satırını taşıyordu (0 kapanmış
işlem), yani eski kurallarla üretilmiş tek bir ölçüm satırı bile yoktu. Sıfırlama
gerekseydi bu bir karar konusu olurdu: kural 1 defter satırının hiçbir gerekçeyle
değişmeyeceğini söyler, dolayısıyla tek meşru yol yeni bir model ADI (dolayısıyla yeni bir
defter klasörü) açmaktı. `equity.csv` başlangıç bakiyesinden başka bir şey taşımadığı için
o da olduğu yerde kaldı.

Eski kombinasyon anahtarları (`atr<i>_tp<j>`) yeni kümede (`band<i>_tp<j>`) tanınmaz ve
`observe_closed_trades` onları `logger.warning` ile sayıp öğrenmenin dışında bırakır —
defterde hiç satır olmasa da bu yol bilinçli olarak korundu ve test edildi
(`test_clone_ignores_rows_from_a_retired_combination`).

### Testler: her kural için üreten VE üretmeyen senaryo

`tests/test_vwap_clone_signal.py` kaynağın her kuralını iki senaryoyla çiviler; yalnızca
"üretiyor" tarafını test etmek, kuralın gerçekten bir kapı olduğunu göstermezdi (kaldırılsa
da testler yeşil kalırdı). Beklenen sayılar üretim kodunun pandas çağrıları tekrarlanarak
değil, `statistics.stdev` ile BAĞIMSIZ kurulur — aksi hâlde test kodun kendisini değil
kendi kopyasını doğrulardı.

Birkaç test kasten model 14 ile yan yana koşar (aynı bar, iki farklı cevap). Amaç
karşılaştırma değil ayrımın gerçekten geçtiğini göstermektir: iki modül aynı cevabı
verseydi 13 ↔ 14 ekseni boş olurdu. En keskin ikisi:

- Banda geri dönmüş bir bar: model 14 için kurulum, kopya için `bant_ici`.
- UTC gün başındaki ikinci bar: kopya için kurulum, model 14 için `vwap_yok`.

## 24. TON-USDT-SWAP scalp evreninden çıkarıldı: 13 sembol

Sembol OKX'te mevcut değil (`51001`). `core/data.py` her turda mum verisini çekemiyor,
`logger.warning` ile atlıyor ve sembolü o turun anlık görüntüsüne hiç koymuyor — yani TON
**hiçbir turda çekiliş uzayının parçası olmadı.** Listede durması ölçümü değiştirmiyordu;
değiştirdiği tek şey, "bu katmanın evreni kaç sembol" sorusunun iki farklı cevabı
olmasıydı: config 14 diyordu, turun gerçekte gördüğü küme 13'tü.

Bu ayrışma tek başına bir arıza değil ama ölçüm hijyeni açısından kabul edilemez, çünkü
evrenin SABİT olması (karar 17) bir kıyas koşuludur ve o koşulun denetlenebilir olması,
yazılı listenin gerçekten koşulan liste olmasına bağlıdır. Her tur tekrarlanan bir uyarı,
gerçek bir veri arızasının (ör. likit bir sembolün geçici olarak çekilememesi) gürültü
içinde kaybolmasına da yol açar — uyarının anlamı "beklenmedik bir şey oldu" olmalıdır,
"her zamanki gibi" değil.

`core/validate.py`'nin sembol evreni kapısı da artık dürüst: kapı, bir modelin hiçbir
koşulda işlem açamayacağı bir sembolü "izinli" saymıyor.

**Katman içi kıyasa etkisi yok.** Beş modelin hiçbiri için çekiliş uzayı değişmedi; TON
zaten hiçbirinin görmediği bir addı. Defterlerde TON satırı yok, dolayısıyla geçmiş
performans da başka bir sembol kümesine ait hâle gelmiyor — karar 17'nin "evren kayarsa
geçmiş performans başka bir kümeye ait olur" itirazı burada devreye girmiyor, çünkü kayan
bir şey yok: yalnızca yazılı liste gerçeğe eşitlendi.

Kopyanın (model 13) evreni 12'de kaldı. Katman ile kopya arasındaki fark artık TEK bir
semboldür — SUI — ve gerekçesi tektir: kaynak sistemde SUI yok (karar 23).

### Gerekçenin bir parçası doğrulanmadı

Bu değişiklik istenirken öne sürülen gerekçelerden biri, `random_ctrl`ün var olmayan bir
sembol seçebileceğiydi. **Bu mekanizma bu katmanda işlemiyor** ve karar ona dayanmıyor:

- `random_ctrl` bir BASE katman modelidir (`config.yaml > models`), scalp katmanında hiç
  koşmuyor;
- seçimini config'in evren listesinden değil `market.ohlcv`den yapıyor
  (`strategies/random_ctrl.py`), yani verisi çekilememiş bir sembol çekilişine zaten
  giremiyor — aynı şey beş kollu modellerin `symbol_views` yardımcısı için de geçerli.

Yanlış bir gerekçeyi karar kaydına doğruymuş gibi yazmak, ileride o mekanizmaya dayanan
başka bir kararın sessizce hatalı kurulmasına yol açardı. Kararın gerçek gerekçesi
yukarıdaki ilk iki paragraftır ve tek başına yeterlidir.

Regresyon kapısı `tests/test_layers.py::test_scalp_universe_is_fixed_and_complete`
içindedir: sembol listeye geri eklenirse test düşer.

## 25. `fee_rate` Bybit taker oranına çekildi: 0.001 -> 0.00055

Komisyon oranı `0.001` idi ve config yorumu onu "OKX taker oranı" diye tanımlıyordu. İki
ayrı hata vardı.

**1. Yanlış borsa.** Veri OKX'ten çekiliyor (`exchange.*`) çünkü evrenin tamamı orada tek
kaynaktan ve aynı bar çapasıyla okunabiliyor. Ama defterin simüle ettiği hesap **Bybit'te**
tutuluyor; komisyonu ödeyen taraf orası. Veri kaynağının oranını maliyet olarak yazmak, hiç
ödenmeyen bir komisyonu ölçüme sokmak demekti. `fee_rate` bundan sonra **işlem yapılan**
borsanın oranıdır, veri çekilenin değil — ikisi ayrışabilir ve ayrıştığında maliyet tarafı
kazanır.

**2. Yanlış değer.** Bybit USDT perpetual, standart (VIP0) kademe: maker %0.02, **taker
%0.055**. Modeller `entry_type="market"` ile girip çıktığı için iki bacak da taker, yani
doğru değer `0.00055`. Eski `0.001` bunun neredeyse iki katıydı.

### Neden bu tek satır ölçümün işaretini belirliyor

Kararı tetikleyen şey model 13'ün (`vwap_clone`) kaynak sistemle karşılaştırılmasıydı.
Kaynak (`klonnist/Hasanwavebot`) komisyon ve kayma **hiç** uygulamıyor — yalnızca funding
modelliyor — ve 6 günde 256 pozisyonda +2156 USDT (+%21.5) üretmiş. Ham edge'i işlem başına
**+8.42 USDT**. Bizim o güne kadar tahsil ettiğimiz sürtünme ise işlem başına **15.00 USDT**
(5000 notional, giriş+çıkış). Yani kaynağın edge'i, bizim maliyet varsayımımızdan küçüktü.

Aynı 256 pozisyona farklı maliyet varsayımları uygulandığında:

| Senaryo | $/işlem | Net PnL | Getiri |
|---|---|---|---|
| eski config (fee .001 + kayma .0005) | 15.00 | −1684 | −%16.8 |
| **yeni (fee .00055 + kayma .0005)** | **10.50** | **−532** | **−%5.3** |
| fee .00055 + kayma .0002 | 7.50 | +236 | +%2.4 |
| maliyet yok (kaynağın varsayımı) | 0.00 | +2156 | +%21.6 |

Sistem bu bandın içinde işaret değiştiriyor. "Bu strateji kârlı mı" sorusunun cevabı, büyük
ölçüde maliyet sabitini doğru koymaya bağlı — ve yanlış bir sabitle ay boyu veri biriktirip
sonra hepsini çöpe atmak, düzeltmeyi geciktirmenin bedeli olurdu.

**Değişmeyen: `slippage_base` ve `slippage_short_stop`.** Kayma bir borsa tarifesi değil,
kitap derinliği ve emir tipiyle ilgili ayrı bir varsayımdır; komisyon oranının yanlış olması
onun hakkında hiçbir şey söylemiyor. Ayrı bir kanıtla ayrı bir kararda ele alınır.

### Defterde iki maliyet rejimi oluşuyor — ve defter SİLİNMEDİ

Bu değişiklik geriye dönük uygulanamaz: `trades.csv` append-only bir denetim izidir ve bir
işlem satırı hiçbir gerekçeyle değişmez veya silinmez (CLAUDE.md > `core/ledger.py`). Yani
scalp defterinin 14-15 Eylül arası kısmı `0.001` ile, sonrası `0.00055` ile yazılmış olacak
ve iki dönemin `cost_per_r` değerleri doğrudan kıyaslanamaz.

Defteri sıfırlamak da düşünüldü ve **yapılmadı**: silmek, kuralın tek istisnasını açmak ve
gerçekten olmuş 80 pozisyonun kaydını yok etmek olurdu. Bunun yerine kırılma noktası burada
yazılıdır. Kırılmanın kendisi zaten okunabilir — `cost_per_r` kolonu iki dönemde belirgin
biçimde ayrışacak — ve ayrıştığında sebebi aranacak yer bu kayıttır.

**Katman içi kıyasa etkisi yok:** oran tek sabittir ve her model için birebir aynı anda
değişti (kural 6). Bozulan şey modeller ARASI kıyas değil, aynı modelin ESKİ ve YENİ dönemi
arasındaki kıyastır.

### Testler

`tests/test_config.py::test_repository_config_matches_claude_md_values` yeni değeri bekliyor.
`tests/test_portfolio.py::test_notional_fraction_pays_the_same_fees_as_everyone` oranı artık
`portfolio.fee_rate`ten okuyor, sabit yazmıyor: testin iddiası "referans model de herkesle
aynı ORANI öder", "oran şu sayıdır" değil — sabit yazmak, oran her değiştiğinde ilgisiz bir
testi kırıp asıl iddiayı gizlerdi.

## 26. Model 14'ün stop ölçeği kalibre edildi: `vwap.managed.atr_multiple` 5.0 -> 2.5

Model 14 (`vwap_managed`) canlıya alındığından beri **hiç sinyal üretmemişti** — 15 Eylül
itibarıyla arka arkaya 24 turun hepsinde `sinyal=0`, defteri boş, hesabı 10.000.00'da.
Turun logu her seferinde aynı şeyi diyordu:

```
vwap_managed vwap_revert bandı=2.00σ -> 13 sembol; bant_ici=13;
en uzak sapma: BTC-USDT-SWAP 1.39σ
```

Bu satır suçluyu **bandı** gösteriyor gibi duruyordu. Ölçüm (`scripts/measure_vwap_signal.py`,
30 gün, 13 sembol) suçlunun band olmadığını gösterdi.

### Ölçümün söylediği

`band_mult=2.0` sabitken, `atr_multiple` süpürmesi:

| atr_x | aday | stop tabanı eledi | 1.5R kapısı eledi | GEÇEN | sinyal/hafta | stop% ort | 30 işleme |
|---|---|---|---|---|---|---|---|
| 1.5 | 983 | 620 | 159 | 204 | 30.8 | 1.48 | 1.0 hafta |
| 2.0 | 983 | 425 | 371 | 187 | 25.7 | 1.65 | 1.2 hafta |
| **2.5** | 983 | 307 | 548 | **128** | **16.8** | **2.05** | **1.8 hafta** |
| 3.0 | 983 | 225 | 672 | 86 | 11.2 | 2.52 | 2.7 hafta |
| 4.0 | 983 | 138 | 825 | 20 | 3.0 | 3.44 | 9.9 hafta |
| **5.0 (eski)** | 983 | 80 | 903 | **0** | **0.0** | — | ∞ |

Band 30 günde 983 aday üretiyor — yani band çalışıyor, sadece seyrek. Adayları öldüren
**1.5R kapısı**: 5×ATR'lik bir stopla hedef (VWAP mesafesi) stop'un 1.5 katına hiç
ulaşamıyor, 983 adayın 903'ü orada eleniyor ve kalan 80'i de %1 stop tabanı alıyor.

Band'ı düşürmek bunu çözmüyor: `atr_multiple=5` sabitken band 2.0'dan 0.5'e indirildiğinde
aday sayısı 983'ten 12.864'e çıkıyor ama **GEÇEN yine 32'de takılıyor** — çünkü bağlayıcı
kısıt band değil, stop ölçeği.

### Neden 5.0 baştan yanlış gerekçelendirilmişti

Config yorumu değeri "scalp kollarıyla AYNI stop ölçeği (`scalp.stop_atr_multiple`)" diye
savunuyordu. Amaç doğruydu — model 14 ↔ `scalp_fixed` kıyasının tek değişkeni sinyal + çıkış
yönetimi olmalı, maliyet ölçeği olmamalı (kural 14) — ama araç yanlıştı:

**Kıyaslanabilirliği belirleyen şey ATR KATI değil, GERÇEKLEŞEN stop mesafesidir.** Kural
14'ün bandı da, kabul çıtasının ⚠B uyarısı da `avg_stop_distance_pct`e bakar, config'teki
çarpana değil. Beş kollu modeller `stop_atr_multiple=5.0` ile defterde ~**%1.95** stop
mesafesi gerçekleştiriyor; model 14 ise `atr_multiple=2.5` ile ~**%2.05**. Yani aynı sayıyı
kopyalamak ölçekleri eşitlemiyordu; 2.5 eşitliyor.

İki modelin ATR tabanının neden farklı ölçekte oturduğu ayrı bir soru (kolların yapısal
seviyeleri ve %1 tabanı stop'u ayrıca biçimlendiriyor) ve bu karar ona dayanmıyor: dayandığı
şey defterde ÖLÇÜLEN mesafe.

### Neden 2.5, 1.5 veya 2.0 değil

Üçü de kural 14'ün bandında ve üçü de kabul çıtasının ⚠B penceresinde kalıyor (yarışmacı
medyanı ~%1.95, band `medyan/√2.5 .. medyan×√2.5` = %1.23–%3.08). Ayrım kıyas hedefinden
geliyor: **2.5, `scalp_fixed`in gerçekleşen stop mesafesine en yakın olanı** (%2.05 ↔ %1.95,
oran 1.05). 1.5 ve 2.0 daha çok sinyal üretirdi (30.8 ve 25.7/hafta) ama stop ölçeğini kıyas
hedefinden uzaklaştırırdı (%1.48 ve %1.65) — yani örneklem kapısını daha hızlı geçmek için
tam da ölçmek istediğimiz farkı kirletmek olurdu.

Bedeli örneklem hızıdır: 30 işlemlik kapıya 1.0 hafta yerine ~1.8 hafta. Kabul edilebilir,
çünkü kapının amacı gürültüyü elemek ve daha hızlı doldurulmuş bir kapı daha iyi bir kapı
değildir.

### Sinyal + maliyet beklentisi

2.5'te: haftada ~16.8 sinyal, stop% ort 2.05 / medyan 1.97, R:R medyan 1.89, ve maliyet/R
≈ **0.11R** (karar 25'in yeni `fee_rate`i ile). Kıyas için: model 13'ün (`vwap_clone`)
gerçekleşen maliyet/R'si **0.87R** — çünkü sabit 5.000 notional'ı ~%0.48'lik bir stopla
taşıyor. Ev kurallarının (%1 stop tabanı) ne işe yaradığı tam olarak burada görünüyor.

### Değişmeyen

`band_mult` 2.0'da kaldı: ölçüm onun bağlayıcı kısıt olmadığını gösterdi, ve dokunmamak
model 14'ün sinyal tanımını olduğu gibi bırakıyor. `target_reward_risk` 2.0'da kaldı.
Model 13'ün `vwap.clone.*` bloğu bu karardan hiç etkilenmiyor — kopya kendi kurallarını
okur (karar 23) ve yarışmacı değildir.

## 27. Seans kırılımı: bir ÖLÇÜM eklendi, bir kural eklenmedi

Bir gözlem bildirildi: "Almanya saatiyle sabaha karşı ve öğleden önce (Asya piyasası
açıkken) yapılan işlemlerin doğruluk oranı daha yüksek; öğleden sonra açılanların neredeyse
hepsi başarısız oldu." İstenen şey, saate göre işlem sıklığını ya da kriterleri uyarlamaktı.

**Kural eklenmedi. Eklenen şey, kuralın gerekeceğini gösterecek ÖLÇÜM.**

### Gözlem kendi verisinde doğrulandı

Kopyanın (model 13) o günkü defteri gözlemle uyumluydu — 80 pozisyon, Berlin saatiyle:

| Seans | n | ort. R | kazanç% |
|---|---|---|---|
| 02–09 Asya | 24 | −0.60 | 29% |
| 09–14 AB | 2 | −1.55 | 0% |
| **14–18 ABD** | 26 | **−1.38** | **15%** |
| 18–02 gece | 28 | −0.46 | 43% |

Öğleden sonra açıkça en kötü blok; 16:00 saatinde 9 işlemin 9'u zarar.

### Ama her seans kovasında tek bir GÜN vardı

Kopya depoya 14 Eylül 12:46'da girmişti; defter 18,8 saatlikti. "ABD seansı kötü" cümlesinin
gerçek içeriği *"14 Eylül öğleden sonrası kötüydü"*. Seans etkisi ile o günün piyasası aynı
sayıya çöküyordu ve ayrıştırılamıyordu.

Bir ipucu daha vardı: 14–18 penceresinde long (−1.37) ve short (−1.39) **birlikte**
kaybetmişti. Trend olsaydı bir taraf kazanırdı; iki tarafın birden kaybetmesi testere
piyasası imzasıdır — yani mesele muhtemelen saat değil, volatilite rejimi.

### Kaynak sistemin 6 günlük defteri gözlemi TERSİNE çevirdi

`klonnist/Hasanwavebot`ın kendi defterinde 6 gün ve 256 pozisyon var. Sıralama gözlemin
tersi çıktı:

| Seans (Berlin) | n | gün | ort $/işlem | kazanç% |
|---|---|---|---|---|
| 02–09 **Asya** | 53 | 6 | **+3.67** | 40% |
| 09–14 AB | 64 | 7 | +4.97 | 34% |
| 14–18 **ABD** | 60 | 6 | **+6.99** | 42% |
| 18–02 gece | 79 | 7 | **+15.49** | 44% |

Asya en kötü, ABD ikinci en iyi. Gözleme dayanıp "öğleden sonra azalt, sabah artır"
denseydi, 6 günlük veriye göre sistem daha KÖTÜ hale getirilmiş olacaktı.

### "Gece en iyi" de bir sonuç değil

Permütasyon testi (seans etiketleri 20.000 kez karıştırıldı):

```
gözlenen en büyük–en küçük farkı : 11.81 USDT
p (fark tesadüfen bu kadar büyük olur mu)      = 0.575
p (gece seansı tesadüfen bu kadar iyi olur mu) = 0.075
```

p=0.575, yani dağılım "seansın hiçbir etkisi yok" varsayımıyla tamamen uyumlu. Bir
overfit'i başka bir overfit'le değiştirmemek için bu da kural yapılmadı.

### Bu yüzden eklenen şey kırılım

`session` kırılımı (`core/metrics.py::session_of`, `layers.scalp.breakdowns`) her turda
seans bazında n / ort.R / kazanç% / `cost_per_r` biriktirir. Hiçbir modelin davranışına
dokunmaz, hiçbir sinyali elemez, adillik koşullarını (kural 6) değiştirmez — `rejections`,
`survey` ve `emitted` ile aynı statüde bir denetim izidir.

Sınırlar **UTC'de sabittir**, yerel saatte değil: yaz saati geçişi sınırları yılda iki kez
kaydırır ve aynı defter iki farklı kırılım üretirdi. Tekrarlanabilirlik burada `random_seed`
ile aynı statüdedir. Ölçüt `opened_at`tır — soru "hangi koşulda ALINDI", "hangi koşulda
kapandı" değil.

### Karar kapısı

Bir saat/rejim filtresi ancak şu iki koşul birlikte sağlandığında gündeme gelir: seans
başına **≥30 pozisyon** (`acceptance.min_trades`) VE **≥10 ayrı gün**. İkincisi birincisinden
önemlidir: bir seansın tek bir gününü ölçmek, o seansı değil o günü ölçmektir.

Koşullar sağlanır ve etki gerçek çıkarsa, uygulama yolu **yeni bir model açmaktır** —
mevcut bir modele filtre eklemek değil. Gerekçe CLAUDE.md'nin kendi kuralıdır: tek
değişkenli bir eksen isteniyorsa yeni model açılır; `vwap_managed` (filtresiz) ↔
`vwap_session` (filtreli) çifti filtrenin katkısını ölçer, modele gömmek ise "iyileşti mi"
sorusunu cevaplanamaz kılar.

Filtrenin şekli de muhtemelen saat olmayacak: veri rejimi işaret ediyor (iki yönün birden
kaybetmesi). Volatilite/yönsüzlük kapısı hem daha genel hem de zaman diliminden bağımsızdır;
saat onun zayıf bir vekilidir.

## 28. Kayıp serisi kırılımı: kümelenme GERÇEK, ama sayaç yanlış tetikleyici

Bir kural istendi: *"üst üste 5 kere işlem kayıpla kapanırsa bir süre bekle — belli ki
işlem açmak için doğru zaman değil."* Karar 27'nin aynı yolu izlendi: önce test, sonra
kural. Bu sefer sonuç ikiye bölündü — **öncül doğru çıktı, mekanizma yanlış.**

### Kümelenme gerçek ve anlamlı

Kaynak sistemin (`klonnist/Hasanwavebot`) 256 pozisyonluk 6 günlük defterinde, ardışık k
kayıptan sonraki işlemin kayıpla kapanma olasılığı:

| k | örneklem | P(kayıp) | taban | p (permütasyon, 20.000) |
|---|---|---|---|---|
| 1 | 153 | 0.673 | 0.598 | **0.0011** |
| 2 | 103 | 0.709 | 0.598 | **0.0013** |
| 3 | 73 | 0.753 | 0.598 | **0.0006** |
| 5 | 41 | 0.732 | 0.598 | 0.052 |

Sonuç dizisi rastgele karıştırıldığında bu yükselme binde bir çıkıyor. Kayıp serisi
uzunluk dağılımı da destekliyor: **14 ardışık kayıp** var; bağımsızlık altında bu
uzunlukta bir seri beklenenden ~12 kat fazla. Karar 27'nin saat hipotezinin aksine burada
gerçek bir yapı var — ve piyasada volatilite kümelenmesi zaten en sağlam ampirik
olgulardan biridir.

### Ama sayaç, beklenen değerin toparlandığı yerde ateşliyor

Aynı defterde ardışık k kayıptan sonraki işlemin **ortalama PnL'i**:

| k | örneklem | ort. PnL | genel ortalama |
|---|---|---|---|
| 0 | 256 | 8.42 | 8.42 |
| 1 | 153 | 1.96 | 8.42 |
| 2 | 103 | 3.55 | 8.42 |
| 3 | 73 | 2.42 | 8.42 |
| **4** | 55 | **8.89** | 8.42 |
| **5** | 41 | **9.08** | 8.42 |

Kayıp OLASILIĞI k ile artıyor ama ortalama PnL k=4'te tabana DÖNÜYOR: seri uzadıkça
kayıplar sıklaşıyor, buna karşılık gelen kazançlar da büyüyor ve dengeliyor. İstenen eşik
(5) tam olarak beklenen değerin normale döndüğü noktada duruyor — yani kural, elemek
istediği kötü işlemleri değil ortalama kaliteli işlemleri elerdi.

### Kuralın birebir simülasyonu: parametre yüzeyi gürültü

12 kombinasyon (tetik 3/4/5/6 × bekleme 1/2/4 saat), maliyetsiz taban 8.42/işlem:

| | 1 saat | 2 saat | 4 saat |
|---|---|---|---|
| 3 kayıp | 9.18 | 8.01 | 4.80 |
| 4 kayıp | 7.02 | 4.30 | 7.06 |
| 5 kayıp | 7.90 | 6.32 | 10.42 |
| 6 kayıp | 7.27 | 8.43 | 10.67 |

3'ü tabanı geçiyor, 9'u geçmiyor ve komşu parametreler zıt sonuç veriyor — bir etkinin
değil, gürültünün imzası. Kesin test: **en iyi** kombinasyonun taban üzeri kazancı
(+2.24 USDT/işlem), kümelenmesi yok edilmiş (karıştırılmış) dizide de **p=0.48**
olasılıkla çıkıyor; bizim maliyetimizle p=0.33. Yani 12 seçenek arasından en iyisini
seçmek, hiçbir yapı olmasa bile bu kadar "iyi" bir sonuç üretiyor.

**Bir tuzak daha:** bizim maliyetimizle (karar 25) sistem zaten −2.08/işlem. Daha az işlem
yapan HER kural toplam PnL'i mekanik olarak iyileştirir. O yüzden karar ölçütü toplam değil
**işlem başına ortalama** olmalıdır; toplam bakılsaydı `(6 kayıp, 4 saat)` "işe yarıyor"
görünürdü.

### Bu yüzden eklenen şey yine kırılım

`loss_streak` kırılımı pozisyonun AÇILDIĞI andaki ardışık kayıp sayısını kova bazında
(`0/1/2/3/4/5+`) biriktirir. Hiçbir modelin davranışına dokunmaz.

**Mimari fark:** kol, sembol, seans ve çıkış kuralı satırın kendi alanlarından türer; kayıp
serisi ise satırın kendisinde DEĞİL, ondan önce kapanmış pozisyonların sırasındadır. Bu
yüzden `core/report.py`ye bir ÖN HAZIRLIK kancası eklendi (`_BREAKDOWN_PREPARE`) ve
`core/metrics.py::annotate_loss_streak` ölçütü türetilmiş bir alana yazıyor. Alternatif,
`breakdown`ın tek satırlık `key` sözleşmesini bozmaktı — o sözleşme diğer dört kırılımın
sadeliğini taşıyor ve tek bir istisna için gevşetilmedi.

**Kesim `opened_at`tır, `closed_at` değil.** Sayılan şey modelin KARAR ANINDA görebildiği
seridir; model yalnızca kapanmış işlemleri görebilir (kural 16). Kesimi kapanışa taşımak,
pozisyonun kendi ömrü boyunca kapanan işlemleri de sayıya katardı — ölçüm, modelin o an
sahip olmadığı bir bilgiyle kurulmuş olurdu ve bir cooldown kuralının ölçüsü olmaktan
çıkardı.

**Kayıp `pnl < 0`dır; tam sıfır seriyi kırar.** Breakeven stop'la kapanan bir işlem kayıp
değildir; kayıp saymak serileri yapay uzatırdı ve bunu tam da üç aşamalı çıkış yönetimi
kullanan modellerde (13/14/15) yapardı — yani kıyasın bir tarafında.

### İlk okuma (küçük örneklem, yorumlanmadan önce beklenmeli)

Mevcut defterlerimizde kovalar şöyle dolmuş (scalp katmanı, ~1 günlük veri):

| model | 0 | 1 | 2 | 3 | 4 | 5+ |
|---|---|---|---|---|---|---|
| `vwap_clone` (ort. R) | −0.29 | −0.52 | −0.86 | −0.79 | −1.07 | **−1.46** |
| n | 17 | 18 | 12 | 6 | 6 | 21 |

Kopyada bozulma **monoton** görünüyor ve kaynağın verisindeki "k=4'te toparlanma"
görülmüyor. İki fark bunu açıklayabilir: (a) bizim maliyet varsayımımız her işleme sabit
bir yük bindiriyor ve seri uzadıkça bileşikleniyor; (b) örneklem tek bir güne ait ve
n kovalarda 6'ya kadar düşüyor. Üç beş kollu modelde ise eğilim ters yönde — ama n=15–19
ile hiçbir şey söylenemez.

### Karar kapısı

Bir cooldown kuralı ancak şu üçü birlikte sağlandığında gündeme gelir: kova başına
**≥30 pozisyon**, **≥10 ayrı gün**, ve **ortalama R'nin k ile monoton düşmesi** (yalnızca
kazanma oranının düşmesi yetmez — kaynağın verisi tam olarak o ayrımda kuralı çürüttü).

Sağlanırsa uygulama yolu yine **yeni bir model**: `vwap_managed` (filtresiz) ↔
`vwap_cooldown` (filtreli), parametreler bu veriden TARANMADAN, önceden sabitlenmiş
olarak. Mimari yüzey hazır — kural 16'nın `observe_closed_trades` kancası modelin kendi
kapanmış işlemlerini okumasına zaten izin veriyor, yani bir kayıp serisi sayacı meşru bir
yerde durur.

**Yapılamayacak olan:** "işlem BÜYÜKLÜĞÜNÜ düşür" kısmı bu mimaride modele kapalıdır.
Boyutlandırma `core/portfolio.py`nin tekelindedir (kural 3/11) ve `risk_per_trade` tüm
modeller için tek sabittir (kural 6); bir model kendi riskini oynatırsa ortak 1R birimi
kaybolur ve modeller aynı ölçekte yarışmaz. **İşlem açmamak** serbesttir — model yalnızca
sinyal üretmez.

## 29. Backtest harness: ikinci bir motor değil, canlı motorun geçmiş penceresi

Backtest altyapısı istendi ve **değerlendirme kuralları sonuç görülmeden önce yazıldı**
(`docs/backtest.md`, ayrı commit). Bu kaydın konusu harness'ın kendisidir.

### Neden yeni bir motor yazılmadı

`core/engine.py` atlanan turları telafi ederken zaten tam olarak bir backtest yapıyor: son
işlenmiş bardan `as_of`'a kadar her barı SIRAYLA işliyor, emirler kendi barının ertesinden
doluyor (kural 13), stop/TP/likidasyon her barın kendi `high`/`low`'uyla kontrol ediliyor
(kural 12/13), pozisyon limitleri her barda yeniden soruluyor.

Ve bu yolun sadakati iddia değil, **testle sabit**:
`tests/test_engine_per_bar.py::test_catch_up_matches_running_each_bar_in_its_own_round`
bir turda telafi edilen N bar ile N ayrı turda koşulan N barın BİREBİR aynı defteri
ürettiğini gösteriyor (karar 19).

Ayrı bir backtest motoru yazmak, bu projede yasaklanan şeyin ta kendisi olurdu: maliyet,
dolum, likidasyon ve metrik kurallarının ikinci bir kopyası. İki kopya bugün hizalansa
bile yarın ayrışır ve backtest, canlıda koşandan başka bir sistemi ölçmeye başlar — üstelik
bunu hiçbir test yakalamaz, çünkü iki uygulama da kendi içinde tutarlı olur.

### Harness'ın yaptığı üç şey

1. **Ayrı defter kökü** (`backtests/<koşu-id>/`). Gerçek defter açılmaz; workflow'un
   `permissions: contents: read` olması bunu niyetle değil YETKİYLE garanti eder.
2. **`last_processed_bar` tohumlama.** `_timeline` boş defterli bir model için bilinçli
   olarak `index[-1:]` döner — canlıda yeni açılan bir modelin geçmişi geriye dönük
   işlemesi yalnızca boş özsermaye satırı üretirdi. Backtest tam olarak o geçmişi istediği
   için başlangıç barını kendisi yazar; motorun kuralı DEĞİŞMEZ, ona canlıdakiyle aynı
   girdi verilir. Pencere yarı açıktır: `start` işlenmez, `end` işlenir.
3. **`signals_per_bar: true`.** Kapalıyken tüm pencere tek bir sinyal üretirdi.

Model kurulumu `main.py::build_strategies`ten gelir (bu yüzden alt çizgisi kaldırıldı).
Backtest'in kendi kurulumunu yazması, `main.py`'nin tek giriş noktası olma gerekçesini
delerdi.

### Kapı 0: harness kanıtlanmadan hiçbir sayı okunmaz

Backtest'lerin çoğu burada sessizce yalan söyler, o yüzden doğrulama bir KAPI yapıldı.

Yer gerçeği elimizde: canlı turların `round.models[].emitted` kaydı — her barda her modelin
tam olarak hangi sinyali ürettiği. Harness aynı pencerede koşturulur ve sinyaller
karşılaştırılır. Kayıt git GEÇMİŞİNDEN okunur, çünkü `docs/data/metrics_*.json` her turda
üzerine yazılır; canlının bar bar ne ürettiği yalnızca commit geçmişinde durur — ve orası
zaten değiştirilemez bir kayıttır.

**Ayrım kancadan TÜRETİLİR, elle listelenmez:**

| Sınıf | Beklenti | Gerekçe |
|---|---|---|
| `observe_closed_trades` UYGULAMAYAN (`scalp_fixed`, `scalp_managed`, `vwap_managed`) | **birebir eşleşme ZORUNLU** | Sinyal, piyasa verisinin ve sabit tohumun saf fonksiyonu. `ScalpModel._round_rng` her barı `random_seed:as_of:kimlik` ile yeniden tohumlar — çekiliş durum TAŞIMAZ. `vwap_managed`de rastgelelik hiç yok. |
| UYGULAYAN (`scalp_bandit`, `vwap_clone`) | eşleşme **beklenmez** | İkisi de kendi kapanmış işlemlerinden öğrenir (kural 16); boş defterden başlayan koşu farklı bir geçmiş görür. Ayrışma raporlanır ama kapı sayılmaz. |

Listenin elle yazılmaması bilinçli: yeni bir uyarlanabilir model eklendiği gün elle yazılmış
liste sessizce yanlış olur ve Kapı 0 o modelden haksız yere birebir eşleşme beklerdi.

Karşılaştırmanın birimi `(model, bar, sembol, yön)`; fiyat alanları KASTEN dışarıda. Kayan
nokta eşitliği kırılgandır ve sorulan soru "aynı kurulumu buldu mu", "ondalık basamağına
kadar aynı mı" değil.

**Kapı 0 penceresi config'in DEĞİŞMEDİĞİ bir aralık olmalıdır:** `fee_rate` (karar 25) ve
`vwap.managed.atr_multiple` (karar 26) 15 Eylül'de değişti; öncesi ile sonrası aynı
kurallarla koşmadı.

### Geçerlilik kapısı B-2 mevcut sayaçlardan gelir

`missing_bars` ve `unchecked_position_bars` (karar: PR #27) sıfırdan büyükse pencere eksik
bir geçmişin üstüne yazılmıştır ve sonucu yorumlanamaz. Backtest'e yeni bir kontrol
eklenmedi — motorun zaten tuttuğu sayaçlar kapı olarak kullanıldı. Bu, `data.history_bars`
penceresi aşıldığında da sessiz kalmayı engeller: seri `last_processed_bar`a ulaşmazsa sayı
raporda görünür.

### Kabul edilen sapma: base katmanı

Canlıda base `signals_per_bar: false` ve 6 saatte bir koşar; backtest bayrağı açar, yani
**canlıdan çok işlem yapar**. Kapalı koşmak alternatif değil — tüm pencere tek sinyal
üretirdi. Sapma `manifest.json > signals_per_bar_forced` alanında her koşuda yazılı durur.
Base için yalnızca ortalama R karara girer; `R/gün` ve toplam getiri bilgi olarak kalır.

### Koşu kendini denetleyebilir kılar

Her koşu `manifest.json` yazar: pencere, katman, model listesi, uyarlanabilir model
listesi, **config parmak izi** (maliyet/risk sabitlerinin özeti), harness git SHA'sı ve
model başına geçerlilik sayaçları. Aynı girdilerle tekrar koşulduğunda aynı sonucu
vermelidir (`random_seed` sabit). Çıktılar `backtests/` altındadır ve depoya girmez: bir
backtest ölçümün kendisi değil, ölçüm hakkında bir denemedir.

---

## 30. İlk uzun pencere backtest'i: kurulum geometrisi kendi içinde tutarsız

**Koşu:** `scalp`, 2026-09-04T08:30Z → 2026-09-16T08:00Z, 1149 bar (≈12 gün),
`missing_bars=0`, `unchecked_position_bars=0` (kapı B-2 ✅). Kapı 0 ayrı bir pencerede
geçmişti (kapıya tabi iki modelde de 9/9).

**Sonuç: dört yarışmacının HİÇBİRİ canlıya alma eşiğini geçmedi** (`docs/backtest.md > 4`).

| model | n | ort. R | kazanç% | PF | getiri |
|---|---:|---:|---:|---:|---:|
| `vwap_managed` | 15 | +0.07 | 46.7 | 1.21 | +0.92% |
| `scalp_bandit` | 154 | −0.21 | 29.2 | 0.35 | −25.05% |
| `scalp_fixed` | 151 | −0.21 | 28.5 | 0.37 | −26.99% |
| `scalp_managed` | 158 | −0.21 | 28.5 | 0.36 | −27.70% |
| `vwap_clone` (kopya) | 707 | −0.56 | 34.5 | 0.39 | −46.68% |

`vwap_managed` C-1'i geçen tek satır ama **B-1 örneklem kapısında düşüyor** (n=15 < 30) ve
pencere onun için IN-SAMPLE (`atr_multiple` tam bu veriyle kalibre edildi, karar 26). İki
bağımsız sebeple kanıt sayılmaz. Çoklu karşılaştırma (§7.5): 4 model test edildi, 0 geçti.

### Teşhis: hedef, verilen sürede ulaşılamaz

`exit_rule` kırılımı (`scalp_fixed`, 151 pozisyon; birim DİLİM):

| çıkış | n | pay | ort. R | ortKaz.R | ortKay.R | topl. R |
|---|---:|---:|---:|---:|---:|---:|
| `signal:time_stop` | 127 | %84 | −0.09 | +0.37 | −0.30 | −10.97 |
| `stop` | 22 | %15 | −1.12 | — | −1.12 | −24.72 |
| `tp` | **2** | **%1.3** | +1.85 | +1.85 | — | +3.69 |

**151 pozisyondan 2'si hedefe vardı.** Kapı `hedef/stop ≥ 1.5` dayatıyor ama gerçekleşen
yapı bu değil; ortalama KAZANAN (+0.37R) ortalama KAYBEDENDEN (−0.30R) büyük olsa bile
%15'lik tam stop dilimi (−1.12R) toplamı gömüyor.

Sebep aritmetik ve modelden bağımsız:

```
stop   = 5×ATR      (scalp.stop_atr_multiple)
hedef  = 10×ATR     (target_reward_risk 2.0 × stop)
16 barlık tipik yayılım = √16 = 4×ATR
hedefin gerektirdiği süre = 10² = 100 bar ≈ 25 saat
mevcut zaman stop'u = 16 bar = 4 saat           -> 6.2× KISA
```

Sürüklenmesiz rastgele yürüyüşün hedefe varma olasılığı (yansıma ilkesi) **%1.24**;
gözlenen **%1.32**. Yani hedefe varma oranı, sinyalin hiçbir katkısı olmadığı varsayımının
öngördüğü sayıyla örtüşüyor. **Sinyal iyi ya da kötü değil — hedefe ulaşmak için verilen
süre yetmiyor, bu yüzden sinyalin ne söylediği sonuca yansımıyor.** (ATR bir aralık
ölçüsüdür, kapanış-kapanış σ'sı değil; bu bir mertebe kontrolüdür, virgül sonrası bir
iddia değil.)

### Yan bulgu: "beş kollu" modeller pratikte TEK kollu

Kol kırılımı: `rsi2_reversal` 141/158 (%89), `opening_range_breakout` 12, `vwap_pullback`
5, `momentum_burst` **0**, `funding_spike_fade` **0**. Bandit'in tahsis edecek bir şeyi
yok — `scalp_bandit ↔ scalp_fixed` ekseninin neden ölçülemediği burada. Eksen bozuk
değil; ölçtüğü değişken bu pencerede hiç değişmemiş.

### Karar 27 ve 28 daha büyük örneklemde doğrulandı

- **Seans:** tüm seanslar negatif, `scalp_fixed` için −0.16 (asya) … −0.28 (avrupa). Saat
  etkisi yine YOK; karar 27'nin "bir ölçüm ekle, kural ekleme" sonucu korunuyor.
- **Kayıp serisi:** scalp modellerinde tek yönlü DEĞİL (0:−0.17, 1:−0.24, 2:−0.30,
  3:−0.39, sonra 4:−0.24, 5+:−0.15) — kötüleşip toparlıyor, yani sayaç yanlış
  tetikleyici. Karar 28 korunuyor.
- **Ama `vwap_clone`da tablo farklı ve güçlü:** 0:−0.15 (n=210) → 3:−0.92 (n=82) →
  5+:−0.84 (n=111). Kayda geçiyor, KURAL YAZILMIYOR: permütasyon testi yapılmadan ve
  seçim etkisi elenmeden hareket etmek karar 28'de düşülen tuzağın ta kendisi.

### Bundan SONRA ne yapılabilir, ne yapılamaz

Teşhis net bir düzeltme öneriyor (stop'u daraltmak ya da zaman stop'unu uzatmak; ikisi de
hedefi ulaşılabilir kılar). **`docs/backtest.md > 7.1` bunu BU pencerede test etmeyi
yasaklar:** parametre değiştirip aynı veride yeniden koşmak ölçüm değil, eğri uydurmadır.
Düzeltme YENİ bir hipotezdir ve TAZE bir OOS penceresi gerektirir — bkz. karar 31.

---

## 31. `scalp_patient` (model 16): karar 30'un teşhisine tek değişkenli cevap

Karar 30 şunu ölçtü: `scalp_fixed`in 151 pozisyonundan 2'si hedefe vardı (%1.3) ve bu
oran, sürüklenmesiz rastgele yürüyüşün öngördüğü %1.24 ile örtüşüyor. Sinyalin iyi ya da
kötü olmasıyla ilgili değil — **hedefe ulaşmak için verilen süre yetmiyor.**

```
stop  = 5×ATR,  hedef = 2.0 × stop = 10×ATR
16 barlık tipik yayılım = √16 = 4×ATR
hedefin gerektirdiği süre = (10)² = 100 bar ≈ 25 saat
```

**Düzeltme: süreyi hedefe uydur.** `scalp_patient`, `scalp_fixed`in ikizidir; ayrışan tek
şey `time_stop_bars` (16 → 100). Kol seçimi, kapılar, stop/hedef geometrisi ve çekiliş
kimliği miras alınır — `scalp_managed` ile aynı desen (eşleştirilmiş deney).

**100 TEORİDEN gelir, veriden değil.** `N = (hedef/ATR)² = 10² = 100`. Birkaç değer
süpürüp en iyisini seçmek `docs/backtest.md > 7.1`in yasakladığı şeydir; tek değer
ön-kayıtla sabitlendi ve taze bir OOS penceresinde sınanır.

**Neden stop'u daraltmak DEĞİL.** Hedefi yaklaştırmanın diğer yolu stop'u küçültmekti:

| | stop'u daralt | süreyi uzat (seçilen) |
|---|---|---|
| hedef ulaşılabilir olur mu | evet | evet |
| %1 `min_stop_pct` tabanı | ATR≈%0.48 iken 2×ATR≈%0.96 → **taban altı, kurulumlar elenir** | dokunulmaz |
| maliyet/R | 0.11 → **~0.25** (kural 14'ün ölçüm kolonu bozulur) | 0.11'de kalır |
| tutuş süresi | 4 saat | ~25 saat, `max_positions` daha uzun dolu |

Taban keyfi değil: tur maliyeti ~%0.25 ve daha dar stop'ta maliyet 0.25R'yi aşar. Yani
stop'u daraltmak bir sorunu çözerken ölçümün kendisini bozardı.

**Neden YENİ bir model.** `scalp_fixed` model 11'in null hipotezidir; zaman stop'unu
değiştirmek onun ölçtüğü ekseni (adaptasyonun katkısı) sessizce başka bir şeye çevirirdi.
CLAUDE.md'nin kuralı: tek değişkenli bir eksen isteniyorsa yolu yeni bir model açmaktır.

**Kural TEK KOPYA kalır.** `TimeStop.from_config(settings, key=...)` yalnızca DEĞERİN
nereden okunacağını söyler; zaman stop'unun ne yaptığı `strategies/time_stop.py`de tek
yerdedir. Kuralı kopyalamak, `scalp_fixed ↔ scalp_patient` farkını "iki ayrı uygulamanın
farkı" hâline getirirdi.

**Canlıda KOŞMAZ.** Kayıt defterinde durur (backtest `--models` ile çağırır) ama
`layers.scalp.models` listesinde yoktur ve bu bir testle çivilidir
(`tests/test_scalp_patient.py::test_the_candidate_is_not_in_the_live_layer`). Doğrulanmamış
bir adayın gerçek deftere yazması, bu altyapının engellemek için kurulduğu şeydir.

---

## 32. OOS sonucu: teşhis doğru, düzeltme yetersiz — ve maliyet artık tek kaldıraç

**Pencere:** `scalp`, 2026-07-19 → 2026-09-04, 4545 bar (47.4 gün), `--history-bars 6000`.
IS penceresiyle (karar 30) örtüşme YOK. Geçerlilik kapıları temiz.

| model | n | ort. R | kazanç% | PF | getiri | maxDD | maliyet/R |
|---|---:|---:|---:|---:|---:|---:|---:|
| `scalp_patient` | 230 | **−0.01** | 47.4 | 0.98 | −4.04% | −19.98% | 0.124 |
| `scalp_managed` | 624 | −0.14 | 35.9 | 0.48 | −53.16% | −54.96% | 0.121 |
| `scalp_fixed` | 650 | −0.15 | 34.3 | 0.47 | −55.41% | −56.94% | 0.121 |
| `scalp_bandit` | 650 | −0.15 | 34.8 | 0.46 | −58.14% | −59.73% | 0.120 |
| `vwap_managed` | 57 | −0.21 | 43.9 | 0.56 | −11.95% | −14.79% | 0.139 |
| `vwap_clone` (kopya) | 2842 | −0.76 | 30.2 | 0.27 | −97.02% | −97.02% | — |

### Karar 30'un teşhisi DOĞRULANDI

| | `scalp_fixed` | `scalp_patient` |
|---|---:|---:|
| hedefe ulaşan dilim | 8/650 = **%1.23** | 41/230 = **%17.83** |
| zaman stop'u diliminin ort. R'si | −0.07 (kaz %37) | **+0.24** (kaz %69) |
| ortKazanç/ortKayıp | 0.90 | 1.09 |
| kazanma oranı | %34.3 | %47.4 |

Hedefe ulaşma oranı **14.5 kat** arttı, ortalama R −0.15 → −0.01, maxDD −%57 → −%20.
Kıyas geçerli: maliyet/R 0.121 ↔ 0.124, stop mesafesi %2.17 ↔ %2.29, ikisi de bandın
içinde (kural 14) — iyileşme bir maliyet farkından GELMİYOR.

### Ama C-1 DÜŞTÜ: canlıya alınmaz

Eşik ortalama R **> 0**; −0.01 bunu sağlamıyor. `scalp_patient` örneklem kapısını (n=230)
ve stop bandını geçiyor ama başabaşın hemen altında. **Canlıya alma eşiği geçilmedi.**
`time_stop_bars`ı sıfırın üstüne çıkana kadar oynatmak `docs/backtest.md > 7.1`in
yasakladığı şeydir ve OOS penceresi artık harcanmıştır.

### Asıl bulgu: brüt edge var, friksiyon yiyor

| model | net R | maliyet/R | **brüt R** |
|---|---:|---:|---:|
| `scalp_patient` | −0.01 | 0.124 | **+0.114** |
| `scalp_fixed` | −0.15 | 0.121 | −0.029 |
| `scalp_bandit` | −0.15 | 0.120 | −0.030 |
| `vwap_managed` | −0.21 | 0.139 | −0.071 |

Diğer üçünde brüt de negatif — orada maliyet suçlu değil, kurulumun kendisi.
`scalp_patient` ilk kez pozitif brüt edge gösteriyor (+0.11R) ve friksiyon onu tam olarak
siliyor. **Kaldıraç artık sinyalde değil, maliyette.**

### İki eksen OOS'ta da SIFIR fark verdi

| Eksen | Çift | IS | OOS |
|---|---|---|---|
| Adaptasyonun katkısı | `scalp_bandit` ↔ `scalp_fixed` | −0.21 ↔ −0.21 | −0.15 ↔ −0.15 |
| Çıkış yönetiminin katkısı | `scalp_managed` ↔ `scalp_fixed` | −0.21 ↔ −0.21 | −0.14 ↔ −0.15 |
| **Sürenin katkısı** | `scalp_patient` ↔ `scalp_fixed` | — | **−0.01 ↔ −0.15** |

Adaptasyon ekseninin sıfır çıkmasının sebebi kol kırılımında görünür: `rsi2_reversal`
568/650 (%87), `opening_range_breakout` 56, `vwap_pullback` 26, `momentum_burst` **0**,
`funding_spike_fade` **0**. Log'da her iki model de "1 uygun kol" görüyor. **Bandit'in
tahsis edecek bir şeyi yok** — eksen bozuk değil, ölçtüğü değişken hiç değişmiyor.

### Karar 27 ve 28 üçüncü kez doğrulandı

Seans: tüm seanslar negatif (−0.08 … −0.18), saat etkisi yok. Kayıp serisi: scalp
modellerinde tek yönlü değil. `vwap_clone`da yine güçlü ve tek yönlü (0:−0.47, n=657 →
3:−0.90, n=329) — kayda geçiyor, **kural yazılmıyor.**

---

## 33. Sadeleştirme: ölçüt performans DEĞİL, ÖLÇÜLEBİLİRLİK

16 model → 8. Emeklilik gerekçesi hiçbir modelde "kaybediyor" değildir; iki ayrı gerekçe var
ve ikisi de sonuçtan bağımsız olarak savunulabilir.

### Scalp: iki eksen İKİ kez "fark yok" dedi

| Eksen | Çift | IS (karar 30) | OOS (karar 32) |
|---|---|---|---|
| adaptasyon | `scalp_bandit` ↔ `scalp_fixed` | −0.21 ↔ −0.21 | −0.15 ↔ −0.15 |
| çıkış yönetimi | `scalp_managed` ↔ `scalp_fixed` | −0.21 ↔ −0.21 | −0.14 ↔ −0.15 |
| **süre** | `scalp_patient` ↔ `scalp_fixed` | — | **−0.01 ↔ −0.15** |

Adaptasyon ekseninin sıfır çıkmasının sebebi kol kırılımındadır: `rsi2_reversal` %87,
`momentum_burst` ve `funding_spike_fade` HİÇ tetiklemedi. Log her turda "1 uygun kol" diyor —
**bandit'in tahsis edecek bir şeyi yok.** Eksen bozuk değil; ölçtüğü değişken hiç değişmiyor.
İki kez alınan "fark yok" cevabını kabul etmek bilgi kaybı değildir.

Emekli: `scalp_bandit`, `scalp_managed`. Kalan: `scalp_fixed` (kontrol), `scalp_patient`
(hareket eden tek eksenin diğer ucu), `vwap_managed`, `vwap_clone` (kopya).

### Base: 4 GÜNLÜK katmanda performans yorumlanamaz, ölçülebilirlik yorumlanır

Base 2026-09-12'de başladı: **29 bar.** Performansa bakıp kesmek, karar 27 ve 28'de iki kez
düşülen tuzağın aynısı olurdu. Ama "bu model n=30'a ne zaman ulaşır" sorusu ŞİMDİ
cevaplanabilir:

| model | n | poz/bar | n=30 için | karar |
|---|---:|---:|---:|---|
| `trend` | 14 | 0.48 | 10 gün | kalır |
| `meanrev` | 8 | 0.28 | 18 gün | kalır |
| `random_ctrl` | 4 | 0.16 | 31 gün | **kalır** (kontrol grubu, `acceptance.control_model`) |
| `buyhold` | — | — | — | **kalır** (çıpa; 0 işlem bir arıza DEĞİL, hiç kapatmaz) |
| `avwap` | 3 | 0.12 | 42 gün | emekli |
| `confluence` | 2 | 0.07 | 72 gün | emekli |
| `ensemble` | 1 | 0.04 | 125 gün | emekli |
| `momentum` | 0 | 0 | **asla** | emekli |
| `squeeze` | 0 | 0 | **asla** | emekli |
| `failed_breakout` | 0 | 0 | **asla** | emekli |
| `downtrend_rally` | 0 | 0 | **asla** | emekli |

Ne kadar iyi olduklarını ASLA öğrenemeyeceğimiz satırlar tabloda yalnızca gürültü üretir.
`ensemble` ayrıca meta bir modeldir: emekli edilenler azaldıkça okuyacağı sinyal kümesi
daralır, yani ölçtüğü şey kadro değişikliğine bağlı hâle gelirdi.

### "Emekli" ile "silinmiş" aynı şey değildir

Kod ve defter DURUYOR (kural 1: append-only denetim izi). Değişen tek şey katmanın `models`
listesidir; geri eklemek bir commit. Her emekli modelin testi bunu çiviler: canlı listede
YOK, ama `REGISTRY`de VAR.

### `scalp_patient` kâğıt katmanına girdi — eşiği geçtiği anlamına GELMEZ

`scalp` bir KÂĞIT ölçüm katmanıdır. `docs/backtest.md > 4`ün canlıya alma eşiği gerçek
parayla işlem açmayı düzenler ve model onu geçmedi (C-1: OOS ortalama R −0.01). Kâğıtta
koşması, ileriye dönük kanıtın biriktiği tek yerdir.

### Dashboard: ana sayı artık "10.000 $ nerede"

Her kartın hero'su hesap özsermayesi + yüzde oldu; ort. R, işlem sayısı, MDD, kıvrım ve
rozetler katlanır `detaylar` bölümüne indi. Gerekçe: **özsermaye her model için AYNI
birimdir**, ort. R değildir — çıpanın ve kopyanın 1R'si yarışmacılarınkiyle aynı şeyi
ölçmez (kural 15/15b), yani tek bakışta kıyaslanabilen tek satır özsermayedir. Ort. R
birincil METRİK olmayı sürdürür ve SIRALAMAYI o belirler (kartın rütbesi); yalnızca kartın
ilk bakışta gösterdiği sayı değişti. Kart artık `<a>` değil: içinde katlanır bölüm var ve
tıklamak onu açmalı, sayfayı değiştirmemeli — detay bağlantısı katlanan bölümün içinde.

### 33-DÜZELTME (2026-09-16, aynı gün): tablo KAPANMIŞ işlemleri saydı, AÇILIŞLARI değil

Karar 33'ün base tablosu `trades.csv` satırlarından türetildi; o dosya yalnızca **kapanmış**
pozisyonları taşır. Açık pozisyonlar `positions.json`dadır ve sayıma girmedi. Sonuç iki
yerde yanlış kayıtlandı. **Emeklilik kararlarının HİÇBİRİ değişmiyor** — hepsi 0.3 eşiğinin
çok altında kalmaya devam ediyor — ama gerekçe denetim izinin parçasıdır ve yanlış duramaz.

Pozisyon-farkındalıklı yeniden sayım (kimlik `symbol+direction+opened_at`, yani
`merge_fills` ile aynı birim; kapanmış + açık):

| model | kapanmış | açık | AÇILIŞ | bar | açılış/bar | not |
|---|---:|---:|---:|---:|---:|---|
| `trend` | 13 | 3 | 16 | 27 | 0.593 | |
| `meanrev` | 7 | 5 | 12 | 27 | 0.444 | **5/5 SLOT DOLU** |
| `random_ctrl` | 2 | 5 | 7 | 23 | 0.304 | **5/5 SLOT DOLU** |
| `avwap` | 3 | 3 | 6 | 23 | 0.261 | 4 dolum = 3 pozisyon |
| `confluence` | 2 | 2 | 4 | 27 | 0.148 | |
| **`squeeze`** | 0 | **2** | **2** | 27 | **0.074** | sıfır DEĞİL |
| `buyhold` | 0 | 2 | 2 | 29 | 0.069 | çıpa: hiç kapatmaz |
| `ensemble` | 1 | 0 | 1 | 23 | 0.043 | |
| `momentum` | 0 | 0 | 0 | 27 | 0.000 | |
| `failed_breakout` | 0 | 0 | 0 | 23 | 0.000 | |
| `downtrend_rally` | 0 | 0 | 0 | 23 | 0.000 | |

**Düzeltme 1 — sıfır üreten model DÖRT değil ÜÇ.** `squeeze` 2 pozisyon açtı
(`ledgers/squeeze/positions.json`); hiçbiri kapanmadı çünkü hedefi yok ve stop'u uzakta.
Yani `squeeze` **ölü kod değil, yavaş kod**. Emekliliği geçerli (0.074 ≪ 0.3) ama sebebi
"hiç sinyal üretmiyor" değil, "ürettiği sinyal ölçülebilir sıklığın çok altında".

**Düzeltme 2 — "poz/bar" iki ayrı değişkeni tek sayıya çökertiyordu.** Gerçekleşen oran
şunun minimumudur:

```
poz/bar = min( sinyal/bar ,  max_positions / tutma_barı )
```

`meanrev` ve `random_ctrl` şu anda **5/5 slotta doymuş**. Onların ölçülen hızı bir sinyal
ölçüsü DEĞİL, bir **devir** ölçüsüdür ve slotlar dolu kaldıkça daha da yavaşlar. Bu, karar
33'ün "meanrev n=30'a ~18 günde ulaşır" tahminini iyimser yapar ve çareyi değiştirir:
`meanrev`in darboğazı sinyal arzı değil, **süre sınırının olmamasıdır** (aynı teşhis
`scalp_fixed ↔ scalp_patient` ekseninde ölçüldü, karar 31/32).

**Yöntem notu:** bundan sonra sıklık her zaman `positions.json` + `trades.csv` BİRLİKTE
sayılır ve slot doluluğu ayrıca raporlanır. Yalnızca deftere bakmak, hedefi olmayan bir
modeli "sinyal üretmiyor" diye gösterir.

### 33-DÜZELTME (2): belge ile cron ayrışmış

`.github/workflows/run.yml` cron'u `5 0,4,8,12,16,20` — yani **4 saatte bir, 4 saatlik bar
başına tam bir tur.** `config.yaml` (iki yerde) ve CLAUDE.md katman tablosu "6 saatte bir"
diyordu. Fark önemsiz değil: "6 saatte bir" doğru olsaydı base'de telafi RUTİN olurdu ve
`signals_per_bar: false` her turda sinyal fırsatı kaybettirirdi. Gerçekte telafi
İSTİSNADIR (yalnızca kaçan cron'da), ki `signals_per_bar`ın kökte kapalı olmasının gerekçesi
tam olarak budur. Üç yerde de düzeltildi.

---

## 34. `momentum_burst` doğduğunda ölüydü: kapı aritmetiği kolu imkânsız kılıyor

Karar 32, `momentum_burst` ve `funding_spike_fade` kollarının 47 günde HİÇ tetiklemediğini
ölçtü ama sebebini yazmadı. Sebep piyasa değil, **geometri**.

`strategies/scalp/arms.py::momentum_burst` engeli `closes[-1] ± burst` olarak veriyor, yani
**tanımı gereği her zaman girişin ÖNÜNDE.** `_maybe_setup` önde olan engeli hedef yapar
(`min(projeksiyon, engel)`), dolayısıyla:

```
hedef mesafesi = min(10×ATR, burst) = burst        (burst < 10×ATR olduğu sürece)
stop mesafesi  = 5×ATR
kapı: hedef/stop ≥ 1.5   ⟹   burst ≥ 7.5 × ATR
```

`burst` 3 barlık kapanış-kapanış hareketidir; tipik büyüklüğü ~1×ATR mertebesindedir.
**Koşu log'larından gözlenen gerçek oranlar: 0.20, 0.26, 0.29, 0.31, 0.32, 0.36, 0.37,
0.39, 0.43, 0.45** — yani `burst ≈ 1.0–2.25 × ATR`. Kapının istediği 7.5×ATR, gözlenenin
**3-7 katı.** Kol hiçbir piyasa koşulunda tetiklenemez.

**Genel kural (diğer kollara da uygulanır):** stop `k×ATR` iken yapısal engel en az
`1.5k×ATR` ötede olmalı. `k=5` için bu **7.5×ATR**tır ve 15 dakikalık barda hiçbir gün-içi
yapısal seviye (Bollinger orta bandı, gün zirvesi, VWAP) tipik olarak o kadar uzakta
değildir. Sonuç: kapıyı geçen kurulumlar ağırlıklı olarak **engeli GERİDE kalmış**
olanlardır; onlarda hedef projeksiyona düşer ve R:R tam 2.00 olur. Yani **hayatta kalan
işlemlerde kolun kendi tez seviyesi hedefe hiç girmez** — beş kol geometrik olarak aynı
projeksiyon modelini oynuyor.

Bu, karar 30 ve 32'nin iki ayrı gözlemini tek sebeple açıklıyor: hedefe varma oranının
rastgele yürüyüş nullüyle örtüşmesi, ve bandit'in tahsis edecek bir şeyinin olmaması.

**Yöntem sonucu:** `ScalpModel` `take_survey` UYGULAMIYOR (`metrics_scalp.json`de beş kollu
modellerin `survey` alanı `{}`). Bu yüzden "bir kol hiç kurulum üretmiyor" bilgisi iki
backtest sonra öğrenildi, oysa ilk turun yük dosyasında görünebilirdi. **Yeni bir kol ya da
tez eklenecekse `take_survey` o modelin ilk gereksinimidir.**

---

## 35. Çıta bir R değeri değil, bir YÜZDE sürüklenmesidir

Üç özdeşlik (ölçülen sayılarla doğrulandı):

```
cost_per_r      = maliyet% / stop%
net R           = (brüt sürüklenme% − maliyet%) / stop%
brüt sürüklenme% = (ort.R + cost_per_r) × avg_stop_distance_pct
```

**Sonuç: stop genişliği net R'nin İŞARETİNİ değiştiremez.** Stop'u genişletmek maliyet/R'yi
düşürür ama R cinsinden brüt edge'i aynı oranda düşürür. Karar 31'de stop'u daraltmamayı
seçmiştik ve gerekçe doğruydu; bu özdeşlik onun tersinin de kurtarmayacağını gösteriyor —
"stop ölçeği" tartışması birinci mertebede boştur.

`scalp_patient`e uygulanınca:

| | |
|---|---|
| brüt sürüklenme% | `0.114 × %2.29` = **%0.261** |
| tur maliyeti% | `0.124 × %2.29` = **%0.284** |

**Ortalama R'nin −0.01 çıkması tesadüf değil, bu iki sayının neredeyse eşit olmasıdır.**

Yeni bir giriş tezinin geçmesi gereken çıta bu yüzden R cinsinden değil **% cinsinden**
yazılmalıdır: **pozisyon başına > %0.30 brüt sürüklenme.** R ölçeği seçilebilir bir
birimdir, yüzde değildir; ayrıca bu birim katman ve stop bağımsızdır.

**Sürüklenme çıtasının kaldıraçları:**

| kaldıraç | etki | durum |
|---|---|---|
| maliyet% ↓ | doğrudan | ayrı çalışma (maker/kademe) |
| stop% (R ölçeği) | **nötr** | tartışmaya değmez |
| tutuş süresi ↑ | sürüklenme ↑ (√N), maliyet sabit | karar 31/32'de ölçüldü: −0.15 → −0.01 |
| **volatilite rejimi (ATR%) ↑** | edge σ ile ölçekleniyorsa sürüklenme ↑, maliyet SABİT | **hiç denenmedi** |

---

## 36. `scalp_vol` (model 17): ön-kayıtlı P1 DÜŞTÜ — "edge σ ile ölçeklenir" yanlış

**Koşu.** `backtest.yml` #10 (`35099002276`), pencere 2026-07-19 → 2026-09-04,
`--history-bars 6000`, modeller `scalp_patient,scalp_vol`. Ön-kayıt:
`docs/backtest.md > 6b`, commit `a7c08ae` — koşudan ÖNCE.

| model | n | ort.R | kaz% | PF | stopMes% | maliyet/R | getiri | maxDD | topl.R | PnL |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `scalp_patient` | 230 | −0.01 | 47.4 | 0.98 | 2.29 | 0.124 | −4.04% | −19.98% | −2.07 | −348.38 |
| `scalp_vol` | 223 | −0.01 | 43.0 | 0.98 | 2.70 | 0.103 | +6.06% | −15.41% | −2.17 | +575.27 |

### Tahminlerin karnesi

| # | Ölçüm | Tahmin | Gerçekleşen | Sonuç |
|---|---|---|---|---|
| **P1** | brüt sürüklenme% | `vol` > `patient` | **0.253 < 0.263** | **DÜŞTÜ** |
| P2 | maliyet/R | `vol` < 0.124 | 0.103 (stop 2.29 → 2.70) | tuttu |
| P3 | örneklem | n ≥ 30 | n = 223 | tuttu |
| P4 | stop bandı | ⚠B yanmasın | band 1.58–3.94, ikisi de içeride | tuttu |

P2 bir sağlamaydı ve tuttu: kapı gerçekten daha yüksek ATR'li sembolleri seçti (stop mesafesi
%2.29 → %2.70, maliyet/R 0.124 → 0.103). Yani P1'in sonucu **yorumlanabilir** — kapı çalıştı,
tez tutmadı.

### Ne öğrenildi

Ön-kayıt bu durumun anlamını önceden yazmıştı: *"P1 tutmazsa 'edge σ ile ölçeklenir'
varsayımı YANLIŞTIR."* Ölçüm bunu söylüyor. Volatilite %18 arttığında (stop mesafesi
vekiliyle) brüt sürüklenme artmadı, **hafifçe azaldı.** Yani bu geometride sinyalin
sürüklenmesi σ ile değil, kabaca SABİT bir yüzdedir — tıpkı maliyet gibi. Karar 35'in
kaldıraç tablosundaki tek denenmemiş satır böylece kapandı:

| kaldıraç | beklenen | ölçülen |
|---|---|---|
| stop% (R ölçeği) | nötr | nötr (karar 35, özdeşlik) |
| tutuş süresi ↑ | sürüklenme ↑ | doğrulandı (−0.15 → −0.01, karar 31/32) |
| **volatilite rejimi ↑** | sürüklenme ↑ | **YANLIŞ (0.263 → 0.253)** |
| maliyet% ↓ | doğrudan | henüz denenmedi |

Geriye tek kaldıraç kalıyor: **maliyeti düşürmek** ya da **sürüklenmesi gerçekten daha büyük
bir SİNYAL bulmak.** Rejim seçerek mevcut sinyalden daha fazlasını çıkarmak mümkün değil.

### +%6.06 getiri bir BAŞARI DEĞİLDİR — ve nedeni ölçülebilir

En çarpıcı gözlem: iki modelin toplam R'si neredeyse aynı (−2.07 ↔ −2.17) ama hesap getirisi
ters işaretli (−%4.04 ↔ +%6.06). Bunu "scalp_vol kazandırdı" diye okumak **§7.4'ün
yasakladığı metrik değiştirmedir**: birincil metrik ortalama R'dir, ön-kayıtta öyle yazılıdır
ve ikisi de −0.01'dir.

Mekanizma da zaten edge değil, **ölçekleme yolu**: boyut `risk_per_trade × GÜNCEL sermaye`
ile kurulur, yani her işlemin R'si o anki bakiyeyle ağırlıklanır. 223 işlemde |R| akışı
~200R iken NET R ~2R'dir. Yani hangi R'lerin yüksek bakiyede gerçekleştiği, net edge'den
**iki mertebe büyük** bir etkidir; brüt akışın %10'luk bir ağırlık kayması tek başına 10
puanlık getiri farkı üretir. Bu bir sıralama/bileşiklenme etkisidir, sinyal farkı değil.

**Yöntem sonucu (yeni):** `|net R| ≈ 0` iken **hesap getirisi bir edge ölçüsü değildir.**
Karar 33 dashboard'un ana sayısını özsermaye yapmıştı ve bu doğru kalır — özsermaye her model
için AYNI birimdir — ama sıralamayı ortalama R'nin belirlemesi de aynı kararda yazılıydı ve
bu ölçüm onun neden zorunlu olduğunu gösteriyor.

Kurtarma denemesi yapılmadı: bu gözlem **yeni ve sınanmamış bir hipotezdir** (R'nin işareti
ile bakiye seviyesi arasında korelasyon), düşen bir hipotezin kurtarıcısı değil. Sınanacaksa
kendi ön-kaydıyla gelir.

### Sonuç

- `scalp_vol` canlıya (kâğıt katmanına bile) **ALINMAZ.** C-1 (ort. R > 0) sağlanmıyor
  (−0.01) ve tezi zaten düştü. `REGISTRY`de kalır, `layers.scalp.models`te yoktur.
- Destekleyici kırılımlar tezin lehine bir şey söylemiyor: çıkış kuralı dağılımı neredeyse
  aynı (`tp` 41 ↔ 39, `stop` 90 ↔ 85, `signal:time_stop` 99 ↔ 99). Kol kırılımında `vol`un
  `rsi2_reversal`ı +0.02 (patient −0.02) ama `vwap_pullback`ı −1.01 (n=6) — ikisi de
  örneklem kapısının altında, yorumlanmaz.
- **Çoklu karşılaştırma (§7.5): araştırmadan çıkan 10 önerinin 1.'si test edildi.**

---

## 37. `vwap_managed` temiz pencere (C-5): örneklem kapısı GEÇİLMEDİ, brüt sürüklenme NEGATİF

Bu koşu karar 31-32 döneminde kuyruğa alınmıştı ve **sonucu hiç okunmamıştı.** Kayda
geçiriliyor: okunmamış bir ölçüm, yapılmamış bir ölçümden daha kötüdür — yapılmış gibi
görünür.

**Koşu.** `backtest.yml` #9 (`35086158249`), pencere 2026-07-19 → 2026-08-17 (2783 bar),
yalnızca `vwap_managed`.

| yön | n | ort.R | medyan R | topl.R | kaz% | PF | stopMes% | maliyet/R | PnL |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| long | 15 | −0.36 | −0.70 | −5.42 | 33.3 | 0.40 | 1.45 | 0.151 | −507.22 |
| short | 10 | −0.21 | −0.05 | −2.12 | 40.0 | 0.48 | 1.43 | 0.177 | −232.54 |
| **TOPLAM** | **25** | **−0.30** | −0.33 | −7.54 | 36.0 | 0.42 | 1.44 | 0.161 | −739.77 |

hesap: getiri −%7.40 | maxDD −%10.78

### Kapılar

- **B-1 (örneklem) DÜŞTÜ: n = 25 < 30.** Bu yüzden aşağıdaki hiçbir sayı bir SONUÇ değildir;
  yön kırılımları (n=15 / n=10) hiç yorumlanmaz.
- **C-1 (ort. R > 0) düştü** ve kıl payı değil: −0.30.

### Yorumlanmayan ama kaydedilen gözlem

Karar 35'in birimiyle: brüt sürüklenme = `(−0.30 + 0.161) × %1.44` = **−%0.20**, tur maliyeti
`0.161 × %1.44` = %0.23. Yani `scalp_patient`ten farklı olarak burada **maliyet sıfırlansa
bile** model kaybediyor — sinyalin kendisi yanlış yöne sürükleniyor. Bu, maliyet çalışmasının
`vwap_managed`i kurtaramayacağı anlamına gelir. n=25 olduğu için bir sonuç değil, sınanacak
bir hipotezdir; kapıyı geçtiğinde ilk bakılacak sayı budur.

### Katman kadrosuna etkisi: YOK (şimdilik)

`vwap_managed` scalp KÂĞIT katmanında kalır. Karar 33'ün ölçütü performans değil
ÖLÇÜLEBİLİRLİKTİR ve bu model 29 günde 25 pozisyon açıyor (~0.86/gün, n=30'a ~35 gün) —
yani ölçülebilir. Kâğıtta koşması onun eşiği geçtiği anlamına gelmez; `scalp_patient` için
karar 33'te yazılan cümlenin aynısı geçerlidir.

**Bu koşu sonucuna göre HİÇBİR parametre değiştirilmedi** (§7.1): `vwap.managed.*` olduğu
gibi duruyor.

---

## 38. Model 14 tripwire'ı ateşledi — ama çıtası, projenin kendi sonraki ölçümüyle çürümüştü

**Gözlem.** 2026-09-15T11:45 → 2026-09-16T17:15 arası 119 canlı bar (`origin/main`
geçmişinden, `round.models[].survey` toplanarak): `vwap_managed` **32 kurulum** buldu,
**0 sinyal** üretti. `skipped_signals=0`, yani kural 14'ün stop tavanı hiç tetiklemedi —
eleme tamamen ev kapılarında.

Tarama sayımı (aynı pencere): `bant_ici` 1414, `z_ge_1_0` 690, `z_ge_1_5` 276,
`z_ge_2_0` 42, `z_ge_2_5` 5, `donus_yok` 10, `vwap_yok` 91.

### Elemenin YERİ: tek bir turun TAM dökümü

`as_of=2026-09-16T08:30`, kurulum=5, beşi de elendi (Actions log'u, run 196):

| sembol | kapı | değer |
|---|---|---|
| BTC | %1 stop tabanı | stop %0.539 |
| BNB | %1 stop tabanı | stop %0.696 |
| DOGE | %1 stop tabanı | stop %0.826 |
| PENGU | 1.5R | hedef/stop 0.91 |
| LINK | 1.5R | hedef/stop 0.99 |

Stop `2.5 × ATR` olduğu için bu satırlar doğrudan bir **volatilite ölçümüdür**:
ATR% = %0.22 / %0.28 / %0.33. Ağustos penceresinden (C-5 backtest, karar 37) örneklenen
11 eleme de aynı deseni veriyor: 7 stop tabanı (%0.305–0.829), 4 R:R (0.67–1.08).

**Bir tuzak: bu R:R değerleri KAPIYA TAKILANLARIN değerleridir.** Hepsi tanım gereği
1.5'in altında; medyanlarını kalibrasyonun KOŞULSUZ medyanıyla (1.89) kıyaslamak seçim
yanlılığıdır ve yapılmadı. Geçerli olan kıyas dağılımın kendisidir: medyan gerçekten 1.89
olsaydı 32 kurulumun 0'ının 1.5'i geçmesi imkânsıza yakındı.

### Asıl bulgu: üç ayrı taban, üç ayrı cevap

| taban | kaynak | beklenen sinyal (119 bar) | P(0 gözlem) |
|---|---|---:|---:|
| "kurulumların %13'ü geçer" | kalibrasyon (karar 26) | 4.16 | **%1.2** |
| "haftada 16.8 sinyal" | kalibrasyon (karar 26) | 2.97 | %5.1 |
| **0.86 dolum/gün** | **C-5 backtest, 29 gün, AYNI motor (karar 37)** | **1.07** | **%34** |

Tripwire birinci satıra göre kurulmuştu. Üçüncü satır — canlı motorun taze bir 29 günlük
pencerede gerçekten ne ürettiği — bugün ilk kez okundu (karar 37) ve kalibrasyonun
oranını **2.8 kat** aşağı düzeltiyor. O tabana göre 29 saatte 0 sinyal görmek **hiç
şaşırtıcı değildir.**

**Yani ateşlenen şey modelin arızası değil, tripwire'ın eski tabanıdır.** Kalibrasyon
`scripts/measure_vwap_signal.py`'nin bir süpürmesiydi; backtest ise maliyet, dolum,
likidasyon ve kota dâhil tam motordur. İkisi çeliştiğinde motor kazanır.

### Hiçbir parametre DEĞİŞTİRİLMEDİ

- **§7.1:** sonucu görüp `atr_multiple` ya da `min_stop_pct` oynatmak tam olarak yasak olan
  şeydir. `atr_multiple` zaten bir kez süpürülüp 2.5'e sabitlendi (karar 26).
- **`band_mult` bir frekans düğmesi DEĞİLDİR** ve bu ölçülmüştür
  (`scripts/measure_vwap_signal.py` modül başlığı): aday olmanın şartı `|z_now| < |z_prev|`
  olduğu için bandı düşürmek `|z_now|`ı da düşürür, R:R küçülür, yeni adaylar aynı kapıda
  ölür.
- **Kapıları gevşetmek bu modelde ZARARLI olurdu:** karar 37 `vwap_managed`in brüt
  sürüklenmesini **−%0.20** ölçtü. Maliyet sıfırlansa bile kaybeden bir sinyalde frekansı
  artırmak, yalnızca kaybı hızlandırır.

### Karar

Tripwire'ın tabanı C-5 backtest'in oranına çekildi (0.86 dolum/gün): yeni eşik **7 gün
üst üste 0 dolum** (P ≈ e^−6 ≈ %0.25). Kontrol devam ediyor, model kadroda kalıyor
(karar 37: ölçüt ölçülebilirlik).

Yan gözlem: `origin/main`in canlı kadrosu hâlâ 5 model (`scalp_bandit`, `scalp_managed`
dâhil) — karar 33'ün sadeleştirmesi dalda, henüz merge edilmedi.

---

## 39. Base katmanı barlarının **%26'sı sinyalsiz geçiyor** — ve kaybolan barlar RASTGELE değil

Karar 33 base için ölçütü "performans değil ÖLÇÜLEBİLİRLİK" diye koymuştu ve modellerin
n=30'a ne zaman ulaşacağını hesaplamıştı. O hesap, her barın bir sinyal fırsatı ürettiğini
varsayıyordu. **Varsayım yanlış.**

**Ölçüm** (`origin/main` geçmişi, `round.bars_processed`, 2026-09-12T04:00 → 09-16T12:00):

| | |
|---|---|
| benzersiz tur | 20 |
| işlenen bar | 27 |
| `bars_processed > 1` olan tur | 7 |
| **sinyal fırsatı hiç doğmayan bar** | **7 / 27 = %25.9** |
| `missing_bars` | 0 (veri kaybı YOK) |

Telafi edilen bar `signals_per_bar: false` yüzünden pozisyon yönetimi yapar (stop/TP/
likidasyon/funding) ama **sinyal üretmez** — bu, ayarın belgelenmiş davranışıdır. Sorun
ayarda değil, ayarın DAYANDIĞI varsayımda.

### Kaybolan barlar sistematik

```
2026-09-13T00:00   2026-09-14T00:00   2026-09-14T08:00
2026-09-15T00:00   2026-09-15T08:00
2026-09-16T00:00   2026-09-16T08:00        saate göre: 00:00 → 4 kez, 08:00 → 3 kez
```

Yani kaybolan bar **her zaman 00:00 ya da 08:00 barıdır** ve 09-14'ten beri **her gün
ikisi de** kayboluyor. Günlük altı bardan ikisi, yani sinyal fırsatının **üçte biri**,
hep aynı iki saatte siliniyor.

Sebep: `run.yml`in cron'u `5 0,4,8,12,16,20` ve GitHub'ın zamanlanmış tetikleyicileri
düşüyor/gecikiyor. Gözlenen son beş tetikleme: 16:24, 22:43, 03:39, 09:07, 16:16 — yani
2.6 saate varan gecikmeler. Bir tetikleme düştüğünde motor barı TELAFİ eder (bu yüzden
`missing_bars = 0`), ama telafi barı sinyal üretmez.

### Neden CLAUDE.md bunu öngörmüyordu

Belge şunu yazıyor: *"`run.yml` güvenilir tetikleniyor, yani base katmanında telafi nadiren
devreye girer."* Bu cümle `signals_per_bar: false`un TEK gerekçesiydi ve ölçüm onu
çürütüyor: telafi nadir değil, **turların %35'i** (7/20). Scalp katmanında 15 dakikalık
cron'un %91'inin düştüğü ölçülüp `signals_per_bar: true` yapılmıştı; base'de aynı arıza
daha küçük ölçekte ve fark edilmeden sürüyor.

**Üstelik base'deki hâli scalp'tekinden daha kötü huylu:** scalp'te kayıp turdan tura
değişiyordu (gürültü), base'de kayıp **saate kilitli** (yanlılık). 00:00 barının sinyali
bir sonraki barın açılışından, yani 04:00'te dolardı (kural 13) — o dolumlar
`00-07_asya` seansına düşerdi. 08:00 barınınki 12:00'de dolar, `12-16_abd`ye düşerdi.
Bugün base'de seans kırılımı YOK, ama açılırsa bu iki seans yapısal olarak eksik
örneklenmiş olur.

### Ne DEĞİŞTİRİLMEDİ ve neden

Hiçbir ayar oynatılmadı. İki yol da canlı ölçümün koşullarını değiştirir ve bu bir
parametre değil, kadans kararıdır:

| yol | etkisi | bedeli |
|---|---|---|
| **A — `run.yml` cron'unu sıklaştır** (ör. saatlik) | her 4H barı KENDİ turunda kapanır, `bars_processed` 1'de kalır, defterin kuralı DEĞİŞMEZ | yeni bar kapanmayan turlar da `generated_at` yüzünden commit üretir: günde 6 yerine 24 commit |
| **B — base'de `signals_per_bar: true`** | telafi barı da sinyal üretir | defterin kuralı akış ortasında değişir; biriken 27 barın bir kısmı "tur başına", kalanı "bar başına" ile üretilmiş olur ve iki dönemin işlem sıklığı kıyaslanamaz (CLAUDE.md'nin bu ayarı base'de kapalı tutma gerekçesinin ta kendisi) |

**A tercih edilir**, çünkü ölçüm kuralına hiç dokunmaz: yalnızca tetikleyiciyi, belgenin
zaten varsaydığı güvenilirliğe getirir. B, tam olarak kaçınılmak istenen ayrışmayı üretir.
Karar kullanıcınındır; ikisi de canlı kadansı değiştirdiği için tek taraflı uygulanmadı.

**Karar 33'ün n=30 projeksiyonları bu oranda iyimserdir** (~%26): `trend` için "10 gün"
gerçekte ~13 gün, `meanrev` için "18 gün" ~24 gündür.

---

## 39-UYGULAMA (aynı gün): A uygulandı

Kullanıcı A'yı seçti. `run.yml`in cron'u `5 0,4,8,12,16,20` -> **`5 * * * *`**.

Her 4H barı artık dört bağımsız tetikleme şansı alır (20:00 barı 00:05'te kapanır;
tutmazsa 01:05, 02:05, 03:05). **Ölçüm kuralı değişmedi:** `signals_per_bar` base'de
kapalı kalır, defter yine "bar başına tek sinyal" ile yazılır.

### Saatlik kadansın iki yan etkisi vardı ve ikisi de tek bir kapıyla kesildi

Turların dörtte üçü yeni bar bulamaz (`core/engine.py::_timeline` boş döner, defter ve
`last_processed_bar` aynı kalır). Ama `main.py` her koşuda `generated_at`i tazeler, yani
`metrics.json` "değişmiş" görünür ve mevcut "değişiklik yoksa commit atlanır" kapısı bunu
YAKALAMAZ. Commit edilselerdi:

1. **Denetim izi ezilirdi.** HEAD'deki `round` bölümü `bars_processed=0` olan boş bir
   turla değişirdi — `emitted`, `survey` ve `rejections` oradan okunur (kural 15) ve
   kararlar 38-39 tam olarak o alanları okudu.
2. **Günlük özet dört kez giderdi.** `scripts/telegram_report.py`nin kapısı `as_of`
   saatidir ve `as_of` dört tur boyunca 20:00'de sabit kalır.

Bu yüzden yeni bir adım eklendi (`Did the round advance?`): turun gerçekten bar işleyip
işlemediğini `round.models[].bars_processed`in maksimumundan okur ve hem commit'i hem
bildirimi ona bağlar. `round` bölümü hiç yoksa kapı KAPALI sayar — bozuk bir yükü commit
etmektense atlamak doğru arıza modudur.

Kapı üç durumda sınandı: gerçek canlı yük (telafi turu, `bars_processed=2`) -> açık;
tüm modeller 0 -> kapalı; `round` yok -> kapalı.

**Bu bir onarımdır, bir parametre değişikliği değil** (§7.1 ile çelişmez): maliyet, risk,
dolum, likidasyon ve metrik tanımlarının hiçbirine dokunulmadı. Değişen tek şey,
belgenin zaten varsaydığı tetikleyici güvenilirliğidir.

**Beklenen etki:** base'in sinyal fırsatı ~%26 artar ve 00:00/08:00 barlarındaki
yapısal boşluk kapanır. Karar 33'ün n=30 projeksiyonları yeniden geçerli olur
(`trend` ~10 gün, `meanrev` ~18 gün). Bu bir TAHMİNDİR; doğrulaması birkaç gün sonra
`bars_processed` dağılımının 1'e yakınsamasıdır.


---

## 40. Ölçümü sağlamlaştırma turu: altı değişiklik, hiçbiri bir modelin davranışına dokunmuyor

Araştırmadan çıkan 10 öneri (bkz. karar 36, "10 öneri") tek tek değerlendirildi. Bu karar
**uygulanan altısını** ve **uygulanmayan dördünün gerekçesini** kaydeder. Ortak özellik:
uygulananların hiçbiri bir sinyali, bir dolumu, bir boyutu ya da bir maliyet sabitini
değiştirmez — yani **defteri tarihli olarak BÖLMEZLER.** Bu bir tesadüf değil, seçim
ölçütüydü.

### Ölçüt: kural katmanı mı, ölçüm katmanı mı

On öneri iki sınıfa ayrılıyordu:

- **Ölçüm katmanı** — defteri değiştirmez, hiçbir modelin davranışına dokunmaz, geriye
  dönük kıyası bozmaz. Bedeli düşük, riski sıfıra yakın.
- **Kural katmanı** — maliyet/dolum/sinyal kurallarını değiştirir. Her biri defteri
  tarihli bir rejim kırığına böler (karar 25'te `fee_rate`in böldüğü gibi) ve bazıları
  mevcut ölçüm eksenlerini yok eder.

Sıra bu ölçüte göre kuruldu: **önce ölçümü sağlamlaştır, sonra modeli çoğalt.**

### Uygulananlar

**1. Kabul kapısının iki deliği.** `_control_avg_r` kontrolün ortalamasını döndürüyor,
o ortalamanın KAÇ işlemden geldiğini sormuyordu. Canlı base defterinde `random_ctrl`
**n=4** iken `trend` (n=17) ona karşı 0.15R marjla "ölçülüyordu" — kapı çalışıyormuş gibi
görünürken gürültüyü gürültüyle kıyaslıyordu. İkinci delik: marj bir ETKİ BÜYÜKLÜĞÜ
eşiğidir, kesinlik eşiği değil; 8 işlemle ölçülen 0.40R marjı geçer ama güven aralığı
sıfırı fazlasıyla içerir.

Eklenenler: `acceptance.control_min_trades` (kontrol kendi kapısını geçmeden edge
DEĞERLENDİRİLEMEZ) ve farkın yüzdelik bootstrap aralığı (alt sınır > 0). Bootstrap,
normal varsayımı değil: stop'lu bir sistemde R dağılımı çarpıktır (kayıplar −1R'de
kümelenir, kazançlar uzun kuyruk yapar).

Bilinçli davranış değişikliği: kontrol n=0 iken eskiden edge VERİLİYORDU, yani model
rozeti kontrolün ilk işlemini kapattığı anda kaybediyordu. Kontrolün kümede HİÇ olmaması
ise ayrı statü olarak kaldı (koşul düşer) — ikisini birleştirmek, kontrolü listeden
çıkarmayı kapıyı geçmenin bir yolu hâline getirirdi.

Üçüncü değişiklik sunumda: kapıyı geçmeyen satır artık SIRALANMIYOR (tabloda ayrı bölüm,
dashboard'da rütbe yerine `n=<sayı>`). Rozette "kapı geçilmedi" yazarken kartın "#1"
demesi, okuyucunun ikincisini okuması demekti. Satır gizlenmiyor: base'in ölçütü
(karar 33) zaten "model n=30'a ulaşabiliyor mu"dur.

**2. README ↔ config drifti.** Karar 33 kadroyu sadeleştirirken `config.yaml` ve
`CLAUDE.md` güncellendi, README güncellenmedi; belge "10 farklı strateji modeli",
`5 0,4,8,12,16,20` cron'u ve OKX'te var olmayan bir sembolü (TON) anlatmaya devam etti.
README üçe ayrıldı (aktif lig / katalog / yarışma dışı) ve `tests/test_docs_sync.py`
kümelerin eşitliğini kapıya bağladı. Belge artık bir hatırlatma değil, kırmızıya dönen
bir test.

**3. Kural 13'ün bedeli.** Aynı mumda hem stop hem hedef aralığa girdiğinde kural 13 kötü
olanın gerçekleştiğini varsayar. Varsayım muhafazakâr ve doğru tarafta, ama ne sıklıkta
BAĞLADIĞI hiç ölçülmemişti — yani "modeller kaybediyor" sonucunun ne kadarının ona ait
olduğu bilinmiyordu. `ModelReport.stop_exits` / `ambiguous_stop_exits` bunu sayar
(denetim izi; hiçbir dolumu değiştirmez, test bunu ayrıca sabitliyor). Kümülatif cevabı
backtest verir. **Varsayımın kendisi oynatılmadı ve canlıda asla oynatılmayacak:**
defterin kuralı tek olmalıdır.

**4. Ön-kayıt sicili + Benjamini-Hochberg.** §7.5 "çoklu karşılaştırma raporlanır" diyordu
ama sayıyı tutan bir yer yoktu; bu karar 36'da fark edildi ("10 önerinin 1.'si") ve sayaç
hiçbir yerde durmuyordu. §6c sicili açtı: hipotez koşudan ÖNCE satır alır, sonucu ne
olursa olsun kalır. Düşen satırı silmek paydayı küçültüp kalanları olduğundan anlamlı
gösterirdi.

Düzeltme BH (q=0.10), Bonferroni değil: Bonferroni 10 test için eşiği 0.005'e indirir ve
bu geometride (n≈200, ort.R ≈ −0.01) gerçek bir edge bile geçemez — prosedür her şeyi
eler. BH yanlış keşif ORANINI kontrol eder; kâğıt katmanında doğru denge budur.

**5. Kayma varsayımının ölçüm aracı.** Karar 35/36'dan sonra açık kalan tek kaldıraç
maliyettir ve maliyetin kayma parçası bir VARSAYIMDIR, hiç sınanmadı.
`scripts/measure_slippage.py` onu sembol sembol ölçer: kitap **Bybit**'ten okunur (maliyeti
ödeyen taraf; `fee_rate`in aynı ayrımı), emir boyu **defterden** gelir (sembol başına
medyan notional), tek snapshot yerine N örneğin **medyanı** raporlanır. Script
`config.yaml`a DOKUNMAZ — sembole bağlı kayma ayrı bir karardır ve bu ölçümü beklemek
zorundadır.

**6. Portföy yoğunlaşma ölçümü + veri önbelleği.** `Portfolio.concentration` net/brüt
maruziyeti ve en büyük sembol payını raporlar — ÖLÇÜM, kural değil. `backtest.yml` mum
önbelleğini koşular arasında taşır; "2-3 yıllık research backtest" ayrı bir motor değil
bir DERİNLİK sorunudur ve sınır ağdadır (OKX istek başına 100 bar).

### Uygulanmayanlar ve gerekçeleri

| Öneri | Karar | Gerekçe |
|---|---|---|
| İşlem sıklığı tavanı (`cost_per_r` eşiğini aşan kol sussun) | **RED** | §7.2'nin yasakladığı post-hoc filtrenin otomatikleştirilmiş hâli. Ayrıca `min_allocation`ın gerekçesiyle ters çalışır ("susturulan kol ölçülemez") ve yanlış değişkeni hedefler: `cost_per_r = maliyet% / stop%`, yani yüksek değer dar stop demektir — kolun kötü olduğunu değil. Karar 35 R ölçeğine göre elemenin birinci mertebede BOŞ olduğunu özdeşlikle gösterdi. |
| İnce sembolleri evrenden çıkar / notional küçült / kayma uyarısını kural yap | **RED** | Motivasyon sonuçtansa §7.2'nin adıyla yasakladığı şey ("şu sembolü çıkarsak"). Evreni değiştirmek biriken defteri kıyaslanamaz yapar. Notional'ı sembole bağlamak R'nin tanımını sembole bağlar ve `cost_per_r`'nin paydası modeller arası kıyaslanamaz hâle gelir. Uyarıyı kurala çevirmek ise bandın kendi gerekçesine aykırı: yüksek maliyet modelin kusuru değil, VARSAYIMIN o sembolde tutmadığının göstergesidir — cevabı satırı elemek değil, varsayımı ölçmektir (madde 5). |
| Rejim filtresi (BTC realized vol + EMA eğimi ortak kapı) | **ERTELENDİ** | "Ortak kapı" olarak yasak: mevcut bir modele filtre eklemek o modelin ölçtüğü ekseni değiştirir ve biriken defteri anlamsız kılar. Yeni model olarak kabul edilebilir (`ScalpModel.regime_filter` noktası zaten var) ama prior'ı düşük: karar 36 bu ailenin bir üyesini (`scalp_vol`) ön-kayıtla çürüttü. Maliyet çalışmasının (madde 5) arkasında. |
| Purged walk-forward makinesi | **ERTELENDİ** | Purging ağır biçimiyle belirsiz süreli etiket örtüşmesi ve çok sayıda süpürülmüş parametre için gerekir; burada tutuş süresi sınırlı ve BİLİNEN, süpürülmüş parametre pratikte tek. Eklenmesi gereken tek şey örtüşme boşluğuydu ve o eklendi (`--embargo-bars`, §6.1). Süpürülen parametre sayısı 1'i geçtiğinde karar yeniden açılır. |
| Korelasyon / net beta tavanı | **ERTELENDİ** | Önce ölç (madde 6). Tavan bedava değil: 13 sembollük kripto evreninde `max_positions` kotasını pratikte 1-2'ye indirir ve n=30 kapısına zaten zor ulaşan katmanda ölçümü durdurur. |

### Bu turda DEĞİŞTİRİLMEYEN şeyler

Hiçbir maliyet sabiti, risk sabiti, stop geometrisi, kol tanımı, kapı eşiği (`min_stop_pct`,
`min_reward_risk`, `time_stop_bars`) ya da evren değişmedi. `signals_per_bar` iki katmanda
da olduğu gibi kaldı. Modellerin ürettiği sinyaller bu commit'ten önce ve sonra
BİREBİR AYNIDIR — değişen yalnızca o sinyallerin nasıl RAPORLANDIĞI ve neyin sayıldığıdır.

Tek davranış değişikliği kabul rozetindedir (madde 1) ve rozet defterin değil, defterin
OKUNMASININ parçasıdır.

---

## 41. Friksiyon HIZI ölçülüyor: `cost_per_r` işlem başınaydı, hesabı eriten şey gün başına

Araştırma listesinden gelen 11 önerinin **raporlama maddesi** uygulandı. Karar 40'ın
ölçütü aynen geçerli: bu değişiklik bir sinyale, bir doluma, bir boyuta ya da bir maliyet
sabitine dokunmaz — **defteri tarihli olarak bölmez.** Modellerin ürettiği sinyaller bu
commit'ten önce ve sonra BİREBİR AYNIDIR; değişen yalnızca defterin nasıl OKUNDUĞUDUR.

### Neden `cost_per_r` yetmiyordu

`cost_per_r` maliyeti **işlem başına** ölçer ve işlem SIKLIĞINI görmez. Karar 35'in
özdeşliği (`net R = (brüt sürüklenme% − maliyet%) / stop%`) tek bir pozisyonun içindedir;
hesabın ne kadar hızlı eridiğini söyleyen şey ise o özdeşliğin **gün başına kaç kez**
uygulandığıdır. İki model aynı `cost_per_r` ile koşup biri diğerinden on kat hızlı
çevirebilir ve tabloda bu fark hiçbir yerde görünmüyordu.

İkinci delik: `cost_per_r` kopya ve çıpa satırlarında `nan`dır (1R'leri başka bir
birimden gelir, kural 15/15b). Yani **yarışma dışı satırların friksiyonu hiç ölçülmüyordu**
— oysa ölçülmesi gereken tam olarak oydu.

### Eklenenler (üçü de KAPI DEĞİL, okuma yardımı)

| Kolon | Ne söyler |
|---|---|
| `avg_r_ci_low` / `avg_r_ci_high` | Ortalama R'nin yüzdelik bootstrap aralığı. Alfa ve örnekleme sayısı `acceptance.*`tan gelir (ikinci anahtar açılmadı), tohum `random_seed ^ model ^ yön` — aynı defter her zaman aynı aralığı verir. Havuzda (long ↔ short) da hesaplanır: projenin ana sorusu tam orada okunur. |
| `cost_pct` | `Σ(komisyon+kayma) / Σnotional`. `cost_per_r`den farkı PAYDADIR ve fark önemlidir: notional **ortak bir birimdir**, yani kopya ve çıpa dâhil HER satırda hesaplanır. |
| `turnover_per_day`, `cost_drag_pct_per_day` (`FrictionStats`) | Günde kaç kat sermaye çevrildi ve bu ne kadara mal oldu. Payda BAŞLANGIÇ sermayesi (güncel bakiyeye bölmek ciroyu modelin performansına bağlardı) ve özsermaye DAMGALARINDAN gelen takvim günü (bar sayısı değil: saklama penceresi eski satırları günlük özete indirir). |

Ayrıca `beklenti` ayrışması rapora girdi. **Yeni bir metrik değildir**, bir özdeşliktir:
R biriminde `WR × ort.kazanç + (1−WR) × ort.kayıp` tam olarak ortalama R'dir (test
sabitliyor). Değeri sayının kendisinde değil, negatifliğin kazanma oranından mı ödeme
oranından mı geldiğini göstermesinde.

### İlk okuma: hipotez canlı defterde doğrulandı

Öneri şunu iddia ediyordu: *"`vwap_clone` turnover'dan ölürdü, R'den önce."* Ölçüm
(2026-09-17, canlı defterler):

| model | ciro/gün | friksiyon/gün | tur maliyeti% | pencere | hesap getirisi | friksiyonun zarardaki payı |
|---|---:|---:|---:|---:|---:|---:|
| `vwap_clone` (kopya) | **24.9x** | **%6.94** | %0.278 | 3.2 gün | −%27.60 | **%78** (2226 $ / 2871 $) |
| `scalp_fixed` | 4.3x | %1.13 | %0.264 | 3.8 gün | −%11.10 | %38 (429 $ / 1119 $) |
| `trend` (base) | 0.6x | %0.17 | %0.302 | 5.5 gün | −%1.81 | — |

**Tur maliyeti üçünde de aynı** (%0.26–0.30) — yani fark sinyalde ya da maliyet
varsayımında değil, **sıklıkta.** `vwap_clone` günde 24.9 kat sermaye çeviriyor ve
zararının dörtte üçü hiçbir sinyal kararından değil, kapıdan geçerken ödenen paradan
geliyor. Bu sayı bugüne kadar hiçbir tabloda yoktu; `cost_per_r` onu tanım gereği
gösteremezdi.

`scalp_fixed`in ortalama R aralığı `[−0.48, −0.15]` (n=37): tamamı sıfırın altında, yani
"−0.31 gürültü olabilir" savunması bu defterde artık yapılamaz.

### Bu bir ÖLÇÜM, bir kural değil

Hiçbir sinyal ciroya göre elenmez, hiçbir boyut ona göre değişmez. **İşlem sıklığı tavanı
karar 40'ta açıkça REDDEDİLDİ** ve bu kolon o reddi geri almaz — tavanın yokluğunun
bedelini ölçer. Seans, kayıp serisi ve portföy yoğunlaşması ölçümleriyle aynı statüdedir.

Sicile (§6c) de girmez: bir modelin performansı hakkında bir İDDİA taşımıyor, bu yüzden
hipotez değil. Çoklu karşılaştırma paydası 1'de kalır.

---

## 42. Geriye dönük değişiklik: hangi düzeltme meşru, hangisi sonuca bakıp geçmişi yazmak

Soru şuydu: *"Çıktıya göre geçmişi düzeltmenin büyük hata olduğunu biliyorum — ama baştan
bir şeyi atladıysak ya da daha iyi bir çözüm varsa, onu da görmezden gelmiş olmuyor
muyuz?"*

İkisi gerçekten farklı şeylerdir ve proje bugüne kadar ayrımı **örtük** yapıyordu.
`docs/backtest.md > 7` yalnızca yasak olanı yazıyor; meşru olanın adı hiçbir yerde yoktu.
Bu karar adı koyuyor.

### Üç katman, üç ayrı cevap

| Katman | Ne | Geriye dönük değişiklik |
|---|---|---|
| **1. Defter** | `trades.csv`, `equity.csv` | **ASLA.** Yanlış görünen bir satır bile düzeltilmez (kural 1). Defter ne olduğunun kaydıdır, ne olması gerektiğinin değil. |
| **2. Ölçüm** | `core/metrics.py`, `core/report.py`, dashboard | **SERBEST, hatta zorunlu.** Metrik defterin saf bir fonksiyonudur; tanım düzeldiğinde TÜM geçmiş yeniden hesaplanır. Yeni tanım eski satırlara da uygulanmazsa defterin bir kısmı bir metrikle, kalanı başkasıyla okunur. |
| **3. Kural** | sinyal, maliyet, dolum, boyutlandırma, evren | **HAYIR — ama yasak değil, İMKÂNSIZ.** Defter o kuralla üretilmedi. Meşru karşılığı vardır: aynı pencerede **backtest**. "Baştan bu kuralla koşsaydık ne olurdu" sorusunun cevabı odur ve yasak olan şey o değil, sonucu görüp pencereyi/parametreyi/metriği seçmektir. |

Proje katman 2'de bunu zaten **yaptı**: `merge_fills` (dolum → pozisyon) geriye dönük bir
ölçüm düzeltmesidir ve biriken tüm defteri yeniden okudu. Kimse buna "geçmişi düzeltmek"
demedi, çünkü değildi — **yanlış olan ölçüydü, sonuç değil.**

### Beş test: bir düzeltme meşru mu?

Bir değişiklik aşağıdaki beşini de geçiyorsa geriye dönük uygulanır; birini bile
geçmiyorsa ileri yönlüdür ve yeni bir hipotezdir (ön-kayıt + taze OOS).

1. **Sonuç-körlüğü.** Düzeltme, sonucu HİÇ görmemiş biri tarafından da aynı cümleyle
   savunulabiliyor mu? *"Aynı pozisyonu iki kez saymak yanlıştır"* hiçbir sonuca bakmaz.
   *"Bu sembol çıkarılsa tablo düzelir"* yalnızca sonuca bakar.
2. **Yön.** Düzeltme belirli bir modeli/satırı iyileştiriyor mu? Herkese aynı yönde
   uygulanıp bazılarını KÖTÜLEŞTİRİYORSA temizdir. (`merge_fills` yönetimli modeli
   kötüleştirebilirdi ve yine de uygulandı.)
3. **Katman.** Defter mi, ölçüm mü, kural mı (yukarıdaki tablo).
4. **Tekrarlanabilirlik.** Düzeltme geriye dönük uygulandığında aynı girdilerden aynı
   çıktı üretilebiliyor mu? Anlık spread, order book derinliği ve sonradan düzenlenebilen
   bir takvim **üretilemez** — bunlar ahlaki değil TEKNİK olarak geriye dönük
   uygulanamazdır ve Kapı 0'ı (canlı ↔ backtest eşleşmesi) tanımsız kılarlar.
5. **Ön-kayıt borcu.** Kural katmanındaysa hipotez sicile (§6c) girer ve çoklu
   karşılaştırma paydasını büyütür. Ölçüm katmanındaysa girmez — bir performans iddiası
   taşımaz.

### Bu çerçeve neyi AÇAR

Ayrım şunu görünür kılıyor: *"reddedildi"* demek *"içindeki gözlem yanlıştı"* demek
değildir. Reddedilen bir KURAL önerisinin içinde, katman 2'ye ait meşru bir ÖLÇÜM
eksikliği durabilir — ve o eksiklik reddedildiği için birlikte çöpe gider. Bu karar,
reddedilen önerilerin ölçüm çekirdeğini ayrı ayrı değerlendirmeyi zorunlu kılar.

---

## 43. Piyasa kontrolü: ana sorunun cevabı, ölçüldüğü pencerenin yönüyle karışıyordu

Karar 42'nin çerçevesi (üç katman, beş test) reddedilen önerilerin içindeki **ölçüm
çekirdeğini** ayrı değerlendirmeyi zorunlu kıldı. İlk sonucu bu: "market-neutral kitap"
önerisi bir KURAL olarak ertelendi (yeni bir boyutlandırma modu gerektirir, karar 40'ın
"önce ölç" kaydı), ama içindeki gözlem katman 2'ye aitti ve **projenin ana sorusunu
doğrudan vuruyordu.**

### Eksiklik

`CLAUDE.md`nin ilk satırı şunu söyler: *"Projenin cevaplamaya çalıştığı ana soru: short
işlemler long işlemlerden daha mı başarılı?"* Tablo bu soruyu yön bazlı ortalama R ile
cevaplıyordu — ama **piyasanın o pencerede ne yaptığı hiçbir yerde ölçülmüyordu.** Düşen
bir pencerede her short daha iyi görünür; bu bir sinyal bulgusu değil, bir takvim
bulgusudur. Kabul çıtasının "çıpayı geç" koşulu (kural 15) bunu HESAP düzeyinde soruyor,
yön düzeyinde değil — oysa ayrışma tam olarak yön düzeyinde raporlanıyor.

Beş testin hepsini geçiyor: sonuç-körü (cümle hiçbir sonuca bakmadan kurulur), yönsüz
(her modele aynı uygulanır ve bazılarını kötüleştirir), katman 2, tekrarlanabilir (çıpa
serisi + defter), ön-kayıt borcu yok (performans iddiası taşımaz).

### Ölçü

```
tailwind% = (çıpa[kapanış] / çıpa[açılış] − 1) × 100 × (long: +1, short: −1)
market_R  = tailwind% / stop_mesafesi%            (pozisyon bazında, sonra ortalama)
```

Çıpa **BTC**: projenin zaten seçilmiş referansı, iki katmanda da var ve bir sepet ağırlığı
seçmek serbest parametre demekti. `beta = 1` varsayımı AÇIKTA durur — `market_r` ortalama
R'den çıkarılmaz, yanında raporlanır (§7.4: birincil metrik değiştirilmez).

### Uygulamadan ÖNCE koşuldu — iki katmanda da mantıklı çıktı

Kural: bir değişiklik önce mevcut defter üzerinde koşulur, sayı mantıklıysa koda bağlanır.

**Scalp** (çıpa: defterdeki gerçek BTC dolum fiyatları, 60 nokta, pencere −%1.54):

| küme | yön | n | ort.R | tailwind% | market_R | R−market_R |
|---|---|---:|---:|---:|---:|---:|
| havuz (yarışmacı) | long | 17 | −0.201 | −0.100 | −0.074 | −0.127 |
| havuz (yarışmacı) | short | 14 | −0.301 | −0.093 | −0.097 | −0.204 |
| `vwap_clone` | toplam | 184 | −0.814 | −0.019 | −0.060 | −0.754 |

İki bağımsız okuma:

1. **Havuzda ham long ↔ short farkı 0.100R; piyasa çıkarıldığında 0.077R.** Yani farkın
   kabaca dörtte biri sinyalden değil, iki yönün gördüğü FARKLI pencereden geliyor. Fark
   yok olmuyor — ama "short'lar daha kötü" cümlesi artık bir büyüklükle nitelenebiliyor.
2. **İki yönün de tailwind'i NEGATİF** (long −0.100, short −0.093): long'lar BTC düşerken,
   short'lar BTC yükselirken tutulmuş. Bu, ortalama R'nin neden iki yönde de negatif
   olduğuna dair kendi başına bir ipucu ve n=17/14 ile yorumlanmaz, kaydedilir.
3. **`vwap_clone`un tailwind'i ≈ 0** (−0.019): 184 pozisyon pencereye yayıldığı için piyasa
   ortalamada siliniyor. Yani onun −0.81R'si piyasadan GELMİYOR — ve bu, karar 41'in
   friksiyon bulgusuyla (zararın %78'i komisyon+kayma) **bağımsız olarak örtüşüyor.** İki
   ayrı ölçünün aynı cevaba varması, ölçünün kendisinin sağlaması oldu.

**Base** (çıpa: `buyhold` 50/50 eğrisi, pencere −%1.38):

| model | yön | n | ort.R | market_R | R−market_R |
|---|---|---:|---:|---:|---:|
| `meanrev` | long | 10 | −0.442 | −0.735 | **+0.293** |
| `trend` | long | 11 | −0.241 | −0.064 | −0.177 |
| `trend` | short | 6 | +0.075 | −0.028 | +0.104 |

`meanrev`in **tüm zararı** (ve fazlası) beta=1 altında piyasa hareketiyle açıklanıyor:
long-only bir kitap, düşen bir pencerede tutulmuş. Bu satırın "model kötü" diye okunması
artık savunulamaz — n=10 ile "model iyi" diye okunması da savunulamaz, ama ölçü hangi
sorunun sorulacağını değiştiriyor.

### Sınırlar (iddia edilmeyecekler)

- **beta = 1 bir varsayımdır**, ölçüm değil. Altcoin'lerin BTC'ye betası 1 değildir;
  sayı bir ÜST SINIR sezgisi verir, bir düzeltme değildir.
- Çıpa serisi `data.history_bars` kadar geriye gider; daha eski pozisyonlar
  **fiyatlanamaz** ve `market_measured` bunu sayar (canlı doğrulamada 30/37).
- Doğrulama koşusunda çıpa, ağ erişimi kapalı olduğu için defterdeki BTC dolum
  fiyatlarından kuruldu (canlıda `MarketData.btc`). Fiyatlar gerçek, seri seyrek.

### Değişmeyenler

Hiçbir sinyal, dolum, boyut, maliyet sabiti ya da kabul kapısı değişmedi. `avg_r`
dokunulmadan durdu. Modellerin ürettiği sinyaller bu commit'ten önce ve sonra BİREBİR
AYNIDIR.

---

## 44. Kırılım grupları kapıya bağlandı; kümülatif R eğrisi ÖLÇÜLDÜ ve ERTELENDİ

Karar 42'nin çerçevesinden çıkan iki ölçüm maddesi. İkisi de aynı kuralla ele alındı:
**önce mevcut defter üzerinde koş, sayı mantıklıysa koda bağla.** Biri bağlandı, biri
ölçümün kendisi tarafından reddedildi.

### Bağlanan: kırılım gruplarına örneklem kapısı + aralık

Kırılımlar (`arm`, `symbol`, `exit_rule`, `session`, `loss_streak`) grup başına ortalama R
gösteriyordu — **örneklemsiz ve aralıksız.** Oysa model satırı için bu tam olarak yasak:
`acceptance.min_trades` (30) altındaki bir ortalama sıralanmıyor bile (karar 40/1).

Ölçüm (`scalp_fixed`, 37 pozisyon) sorunun büyüklüğünü verdi:

| kırılım | grup | kapıyı geçen | en büyük grup |
|---|---:|---:|---|
| `symbol` | 11 | **0** | BNB n=9 |
| `session` | 4 | **0** | 00-07 asya n=14 |
| `arm` | 2 | 1 | rsi2_reversal n=33 |
| `exit_rule` | 2 | 1 | signal:time_stop n=31 |

**Dashboard 11 sembol satırını ortalama R'leriyle gösteriyordu ve hiçbiri bir ölçüm
değildi.** En uç örnek XRP: n=3, ort. R −1.03. Karar 27 ve 28 tam olarak böyle bir
satıra bakıp kural yazma denemeleriydi ve ikisi de daha uzun örneklemde çürüdü; yani bu,
projenin iki kez düştüğü tuzağın arayüze yerleşmiş hâliydi.

Artık her grup kendi bootstrap aralığını taşıyor (tohum grup adına bağlı, deterministik),
kapı altındaki satır **solgun** çiziliyor ve `Ö` işareti taşıyor. Satır GİZLENMİYOR:
base katmanının ölçütü zaten "n=30'a ulaşılabiliyor mu"dur.

**Yan bulgu — bootstrap'ın tanım sınırı.** İlk koşuda n=1 olan grup `[+0.08, +0.08]`
gibi DEJENERE bir aralık üretti: okuyucuya kıl payı kesinlik vaat ediyor, oysa yeniden
örneklenecek dağılım yok. `bootstrap_mean_ci` artık iki gözlemden azında `nan` döndürüyor
— `_stdev`in zaten uyguladığı sözleşmenin aynısı. Bu bir eşik SEÇİMİ değil, bootstrap'ın
tanım sınırıdır.

### Ertelenen: kümülatif R eğrisi

Tez karar 36'dan geliyordu: boyut `risk_per_trade × GÜNCEL sermaye` ile kurulduğu için
özsermaye eğrisi bileşiklenme yolunu taşır ve `|net R| ≈ 0` iken hesap getirisi bir edge
ölçüsü DEĞİLDİR (orada iki model aynı toplam R ile −%4.04 ↔ +%6.06 getirmişti). Öneri:
özsermaye eğrisinin yanına bileşiklenmesiz bir kümülatif R eğrisi koymak.

Ölçüm (canlı scalp defteri) tezi bu pencerede **doğrulamadı:**

| model | n | toplam R | maxDD(R) | getiri | maxDD |
|---|---:|---:|---:|---:|---:|
| `scalp_fixed` | 37 | −11.48 | −12.94 | −%11.10 | −%12.89 |
| `scalp_bandit` | 32 | −9.42 | −9.44 | −%9.25 | −%9.64 |
| `vwap_clone` | 184 | −149.72 | −150.03 | −%27.60 | −%27.84 |

İki eğri **aynı hikâyeyi** anlatıyor. Sebep açık: hesap henüz yeterince hareket etmedi
(10.000 → 8.890), yani bileşiklenme ayrışacak kadar birikmedi. Karar 36'nın ayrışması
223 pozisyonluk bir backtest penceresinde ve iki mertebe daha büyük |R| akışıyla oluşmuştu.

Üstelik o ayrışmayı yakalayan sayılar **zaten var:** `total_r` ve `max_drawdown_r` karar
36'da tam olarak bu işi gördü. Eğri bilgi eklemiyor, görselleştirme ekliyor.

**Karar: ERTELENDİ.** Tetikleyici yazılıdır — `|toplam R|` ile hesap getirisi (yüzde
olarak) arasındaki fark %20'yi aştığında ya da bir backtest penceresi bunu gösterdiğinde
karar yeniden açılır. O zamana kadar iki skaler yeterlidir.

Bu, ölçüm katmanının kendi kuralını kendine uygulamasıdır: bir aracın eklenmesi de
"ölçülmeden karar verilmez" ilkesine tabidir.

---

## 45. `ema_trend` (model 18) ve `ema` katmanı: dış bir sistemin tezi, evin kurallarıyla

Öneri dışarıdan geldi: modelin sahibi EMA(21/55) kesişimine dayanan long-only bir trend
modelini TradingView'de (Pine Script) geliştirmiş ve orada doğrulamış. İstek, onu bu
projenin ölçüm koşullarında yeniden kurmak ve kadroya alıp almamaya **ölçüyle** karar
vermekti.

Bu kararın kaydettiği şey modelin sonucu DEĞİL (henüz koşmadı), koşudan önce sabitlenen
üç seçimdir. Ön-kaydın tamamı `docs/backtest.md > 6d`dedir.

### 1. Kopya değil, YARIŞMACI

`vwap_clone` (kural 15b) dış bir sistemin kurallarını **boyutlandırması dâhil** yeniden
üretir ve yarışmaz. `ema_trend` öyle değil: yalnızca SİNYAL kuralı dışarıdan geliyor,
boyutlandırma (risk %1), kaldıraç tavanı, maliyet, funding ve likidasyon evin kuralları.
Bu yüzden `is_replica` DEĞİLDİR, tam bir yarışmacıdır ve ortalama R sıralamasına girer.

Ayrım önemli: kopyanın `nan` alan maliyet kolonları (1R'si sabit teminattan türediği için
kıyaslanamaz) bu modelde `nan` DEĞİLDİR — 1R'si diğer yarışmacılarla aynı paydadan gelir,
yani stop bandı (kural 14) ve `cost_per_r` onun için de anlamlıdır.

### 2. Ayrı katman (`ema`), ve kıyas hedefi katmanın İÇİNE alınıyor

Backtest 13 sabit coinde koşuyor. Base katmanının evreni hacme göre seçilen 50 coindir ve
30 günde bir kayar; modeli oraya almak, backtest'in ölçtüğünden başka bir evrende
koşturmak olurdu. Ayrı katman ikisini eşitler (`layers.ema`: 4H, sabit 13 sembol,
`ledgers_ema/`, `docs/data/metrics_ema.json`).

Bedeli, katmanlar arası kıyasın yapılamamasıdır (CLAUDE.md > Katmanlar) — model sahibinin
asıl istediği şey ise tam olarak `trend` ile yan yana görmekti. Çözüm kıyası katmanın
İÇİNE almak oldu: `ema` katmanı `trend` (kıyas hedefi), `random_ctrl` (kabul çıtasının
kontrolü, `acceptance.control_model`) ve `buyhold` (çıpa, C-3) modellerini de koşar. Aynı
evren, aynı barlar, aynı maliyet, aynı defter kuralları — yani kıyas katman İÇİ ve
geçerli.

Alternatif "base evrenini 13 coine sabitlemek"ti ve reddedildi: `trend`, `meanrev`,
`random_ctrl` ve `buyhold` bugünden itibaren başka bir evren görürdü, yani mevcut
defterlerinin geçmişi yeni dönemle kıyaslanamaz hâle gelirdi (karar 25'te `fee_rate`in
defteri tarihli olarak bölmesiyle aynı hata).

### 3. İki kapı da bağlayıcı — ve tek istisna sonuçtan ÖNCE yazıldı

Model sahibinin kendi kabul kriteri var (6 coinde OOS kâr faktörü > 1.1, toplam > 300
işlem, coin başına drawdown ≤ %25). Bu kriter repo'nun kapılarının (`docs/backtest.md`
§3 ve §4) YERİNE geçmiyor, ÜSTÜNE geliyor. Gerekçeyi model sahibi kendisi koydu ve doğru:
diğer modeller repo eşiğinden geçti; yeni bir modele daha gevşek kapı açmak onu iyi olduğu
için değil **kapısı kolay olduğu için** önde gösterirdi — kural 6'nın kendisi.

Tek istisna da sonuç görülmeden yazıldı: model YALNIZCA C-3'ten (hesap getirisi çıpayı
geçer) kalıyorsa koşu **durur** ve karar insana gider; başka herhangi bir kapıdan kalırsa
doğrudan bloke. İstisnanın gerekçesi, C-3'ün cevabının modelin verisinde değil pencerenin
yönünde olmasıdır — model sahibinin TradingView ölçümünde 2022-2026 BTC penceresinde
strateji +%4.89, al-tut +%110 yapmış, yani C-3'ün düşmesi BEKLENİYOR ve bu beklenti
ön-kayda `P4` olarak yazıldı. İstisna kapıyı kaldırmıyor, kararı otomatikten insana
taşıyor.

### Zaman stop'unun yokluğu bir ölçüm sorunu üretti

Model TP/SL dışında çıkış taşımıyor, yani pozisyon ömrü SINIRSIZ. §6.1'in embargo kuralı
("doğru boşluk azami tutuş süresidir") bu modelde tanımsız kalıyordu. Çözüm sınırı
ÖLÇMEK oldu: dönem A'da gözlenen azami tutuş süresi embargo olarak uygulanır, uygulanan
değer ve tutuş süresi dağılımı (medyan / p90 / azami) raporlanır.

Dağılım tek başına da bir ölçüm: uzun kuyruk, kurulumların hedefe varmadan beklediğini
gösterir ve ön-kayıtlı P2 tahmininin (hedefe ulaşma oranı < %35) bağımsız kontrolüdür.
**Zaman stop'u eklemek bu koşuda YAPILMAZ** — parametreler sabit (§7.1); sonuçlar
görüldükten sonra ayrı bir varyant olarak değerlendirilebilir, ki o taze bir OOS penceresi
ister.

### Maliyet sapmasının YÖNÜ ölçüldü, varsayılmadı

Model sahibi TradingView varsayımını istedi (komisyon %0.075 tek yön, kayma 2 tick);
repo %0.055 + 5bp koşuyor. `config.yaml` DEĞİŞTİRİLMEDİ (kural 6 + karar 25); sapma
harness bayraklarıyla uygulanır ve `manifest.json`a yazılır.

Yönü hesaplamak gerekti, çünkü sezgi yanıltıyordu: dolum başına backtest 0.00075 + 0.0001
= **0.00085**, canlı 0.00055 + 0.0005 = **0.00105**. Yani daha yüksek komisyona rağmen
backtest canlıdan **UCUZ** koşuyor (kayma farkı komisyon farkını yutuyor). Sonuç: forward
test backtest'in biraz ALTINDA kalmalıdır; üstünde çıkması bir sürpriz değil, bir
uyarıdır. "2 tick" düz bir 1bp ile temsil edildi — sembole bağlı kayma ayrı bir karardır
(CLAUDE.md > `scripts/measure_slippage.py`) ve defteri tarihli olarak bölerdi.

### Belge onarımı (yan iş)

`docs/backtest.md`'nin §7 BAŞLIĞI yoktu: metin "Bu liste bağlayıcıdır..." diye başlıyor ve
belgenin başka yerlerinde §7.1/§7.3/§7.4/§7.5 diye atıf yapılıyordu. Başlık eklendi;
içerik değişmedi.

---

## 46. ATR yumuşatması: modelin SPEC'i ile projenin varsayılanı çakıştığında

`ema_trend`in kuralları TradingView'de geliştirildi ve orada doğrulandı. Pine'ın `ta.atr`ı
**Wilder** yumuşatması (RMA) kullanır; bu repo ise `simple` (TR'lerin düz ortalaması)
kullanıyordu. Model bir ATR katından stop kuruyor ve hedef o stop mesafesinin katı, yani
**iki sistem aynı kuralı koşmuyordu** — fark bir parametre farkı değil, spec farkıydı.

Bu, "sonucu görüp implementasyon değiştirme" kokusu taşıyan bir karardır ve kokunun
farkındayız. Üç şeyle savunulabilir kılındı:

1. **Zamanlama.** Değişiklik tam koşudan ÖNCE yapıldı; gerekçe ön-kayda (docs/backtest.md
   > 6d > TADİLAT-1) karar anında yazıldı, sonuç görüldükten sonra değil.
2. **Bir kapıyı kurtarmıyor.** Değişiklikten önce ölçülen P1 (BTC, dönem A) ZATEN GEÇMİŞTİ:
   kâr faktörü 1.40 ↔ referans 1.551 (tolerans ±0.2), ödeme oranı 1.76 ↔ 1.79. Düşen bir
   kapıyı geçirmek için yapılmış bir değişiklik olsaydı savunulamazdı.
3. **Eski sayı silinmedi.** Tadilat öncesi ölçümler (tek sembollü ve 13 sembollü portföy)
   ön-kayıtta duruyor ve orada kalacak. Silmek, paydayı küçültüp kalanı anlamlı göstermenin
   ta kendisiydi (§6c'nin sicil kuralıyla aynı gerekçe).

### Neden KÜRESEL değiştirilmedi

Tanımı topyekûn Wilder'a çevirmek tek satırlık bir değişiklikti ve reddedildi: `trend`,
`meanrev`, beş kollu scalp modelleri ve `vwap_managed` bugün canlı koşuyor ve stop
mesafelerini aynı fonksiyondan alıyor. Değişiklik, o commit'ten itibaren hepsinin stop
ölçeğini kaydırırdı; biriken defterin bir kısmı bir ölçekle, kalanı başkasıyla üretilmiş
olur ve iki dönem kıyaslanamazdı — karar 25'te `fee_rate`in defteri tarihli olarak böldüğü
hatanın aynısı. Üstelik bu, bir modelin spec'ini düzeltmek için ölçümün tamamını
bozmak olurdu.

Seçilen yol: `core/indicators.py::average_true_range` iki yumuşatmayı da verir,
**varsayılan `simple` kalır** ve `ema_trend` kendi yumuşatmasını `config.yaml` üzerinden
BİLDİRİR. TR dizisi tek tanımdan gelir (`true_range`), yani "fark yumuşatmada mı TR'de mi"
sorusu sorulabilir kalır.

### Bedeli ve nereye taşındığı

**"1.5×ATR" artık modeller arasında birebir kıyaslanabilir değildir.** Bu gerçek bir kayıp
ve gizlenmiyor. İki şey onu sınırlı tutuyor:

- **Kural 14'ün kıyas ölçütü zaten ATR katı değildi:** band `avg_stop_distance_pct`
  (GERÇEKLEŞEN stop mesafesi) üzerinden kurulur ve ⚠B uyarısı ona bakar. Defterde ATR
  yoktur; kıyas hep yüzdeden okunuyordu.
- **Motorun tavan kontrolü ORTAK tanımda kaldı.** Tavanı modelin kendi yumuşatmasıyla
  ölçmek, her modele kendi tavanını genişletme imkânı verirdi ve tavan bir kural olmaktan
  çıkardı. Bunun bir sonucu var ve ölçüldü: sentetik bir seride Wilder/simple oranı ~0.89,
  yani model 1.5×Wilder ile ortak tanımda ~1.34× görünüyor — 3.0 tavanının çok altında,
  dolayısıyla tavan bu modelde pratikte bağlamıyor. Bir test bunu çiviliyor.

### Bu bir emsal değildir

Yumuşatma bildirimi **kuralları dış bir sistemden gelen** modeller içindir. Ev modellerinden
biri "benim tezim Wilder ister" derse cevap hayırdır: o bir spec uyumu değil, bir parametre
tercihidir ve tercihler ortak tanımın içinde yapılır. Bugün bu bayrağı yalnızca `ema_trend`
taşıyor ve config'te tek bir satır olarak duruyor — grep'lenebilir, denetlenebilir.

---

## 47. `ema_trend` koşuldu: P1 tuttu, hipotez düştü — model canlıya ALINMADI

**Karar:** `ema_trend` (model 18) ön-kayıtlı backtest'i koşuldu ve **BLOKE** edildi.
`.github/workflows/run-ema.yml` açılmadı; `ema` katmanı tetikleyicisiz kalıyor. Kod,
katman tanımı ve defter kökü DURUYOR — `scalp_vol`un `REGISTRY`de durup `models`
listesinde olmaması ile aynı statü (kod ölçülmeden yarışmaz).

**Sonuç iki parçadır ve karıştırılmamalı.**

**(1) Harness doğrulandı.** P1 — TradingView paritesi — bir tahmin değil KAPIYDI ve
geçti: BTC tek-sembollü kâr faktörü dönem A'da 1.549 (referans 1.551, sapma 0.002),
dönem B'de 1.206 (referans 1.299, sapma 0.093); tolerans ±0.2. Yani "model kötü" ile
"ölçüm bozuk" ayrımı yapılabiliyor ve okunan sayılar kaynak sistemin sayıları.
Dönem A'daki binde iki sapma, karar 46'nın (Wilder ATR) doğru tadilat olduğunu
gösteriyor — tadilat koşudan ÖNCE yapıldı ve gerekçesi o zaman yazıldı, bu yüzden
sonuç onu doğrulayabilir.

**(2) Tez düştü.** Ortalama R iki dönemde de negatif: A −0.0015 (n=369), B −0.0165
(n=389). Hesap getirisi çıpanın altında: A −%8.6 ↔ `buyhold` +%43.4, B −%20.4 ↔ −%7.3.
Yani repo kabul kapılarının **E** kapısı iki dönemde de düştü (**Ö** ve band uyarısı
geçti).

**Neden "yalnızca çıpadan kaldı" istisnası işletilmedi.** Ön-kayıt (docs/backtest.md >
6d) tek bir istisna tanımlıyordu: model YALNIZCA C-3'ten (çıpayı geçme) kalıyor ve
diğer her şeyi geçiyorsa koşu durur ve karar model sahibine gider. Model C-3'ün yanında
**C-1'den de** (ortalama R > 0) kalıyor, yani istisnanın şartı sağlanmıyor. İstisnanın
kapsamı sonucu görmeden bir kapıyla sınırlandırılmıştı ve sonucu gördükten sonra
genişletilmedi — genişletmek, istisnayı "kalan her modelin geçtiği kapı" hâline
getirirdi.

**Model sahibinin kapıları geçti, ama yerine geçmiyor.** K-1 (≥6 coinde dönem B
PF > 1.1) tam 6 ile, K-2 (>300 işlem) 978 ile, K-3 (coin DD < %25) azami %21.3 ile
geçti. İki kapı kümesinin de bağlayıcı olması tam olarak bunun içindi: gevşek kapı
modeli iyi olduğu için değil kapısı kolay olduğu için önde gösterirdi (kural 6). K-1'in
kıl payı geçmesi (sınırdaki satır LINK 1.101, eşik 1.1) ayrıca not edildi — eşik
sonuçtan sonra ne aşağı ne yukarı oynatıldı.

**Ölçülen şey bir başarısızlık değil, bir sayıdır.** Dönem B'de ödeme oranı 1.61 ve
kazanma oranı %34.7 — 2R hedefli bir sistemde başabaşın sınırı. `cost_per_r` 0.057
işareti negatife çeviriyor. Bu bir GÖZLEMDİR ve bir sonraki adımın gerekçesi DEĞİLDİR:
parametre oynatmak, maliyeti yeniden varsaymak ya da modele zaman stop'u eklemek
docs/backtest.md > 7'nin yasakladığı şeydir. Yeni bir tez ancak yeni bir ön-kayıt
satırıyla, yeni bir model olarak gelir.

**Ön-kayıtlı tahminlerin tamamı sicile yazıldı, düşenler dâhil** (docs/backtest.md >
6c, satır 2): P1 tuttu, P2 dönem A'da kıl payı düştü (%35.23 ↔ eşik %35) ve B'de tuttu,
P3 düştü, P4 tuttu. Düşen tahmini silmek, paydayı küçültüp kalan sonuçları olduğundan
anlamlı göstermek olurdu — sicilin var oluş sebebi budur.

**Sonuçlar depoda:** `docs/data/backtest_ema_trend.json` (site yükü) ve
`docs/data/backtest_ema_trend.csv` (coin tablosu; her sembolün kendi başlangıç tarihi ve
işlem sayısı kolonlarda, çünkü dönem A'da evren 13 değil 9 sembolle başlıyor).
`docs/backtest.html` ikisini de çizer ve HİÇBİRİNİ yeniden hesaplamaz (kural 7):
verdikt de yükten okunur.

---

## 48. Beş kollu modeller üç kolla koşuyor: kapsam tanımları yanlış yazılı

**Karar:** Bir BELGE düzeltmesidir, kod değişikliği değil. `momentum_burst` (4. kol) ve
`funding_spike_fade` (5. kol) katmanın canlı ömrü boyunca tek bir sinyal üretmedi; bu
yüzden "beş ortak kol" ifadesi ve ona dayanan eksen kapsamları README.md ile CLAUDE.md'de
GERÇEĞİ ANLATMIYOR. Kollar SİLİNMEDİ, eşikler kurcalanmadı, `take_survey` uygulanmadı —
bu turda yalnızca yazılı kapsam düzeltildi.

**Ölçüm dayanağı.** Kaynak defterin kendisidir (kural 1: append-only, yani dosya tüm
geçmişi taşır), `core/metrics.py::breakdown` ile `arm_of` ölçütünde okundu:

| Kol | Pozisyon | Açık pozisyon |
|---|---:|---:|
| `vwap_pullback` | 1 | 0 |
| `opening_range_breakout` | 21 | 2 |
| `rsi2_reversal` | 101 | 8 |
| `momentum_burst` | **0** | **0** |
| `funding_spike_fade` | **0** | **0** |

Pencere 2026-09-13T19:30 → 2026-09-19T10:45 UTC, 123 pozisyon, dört defter birden
(`scalp_bandit`, `scalp_fixed`, `scalp_managed`, `scalp_patient` — emekli modellerin
defterleri de duruyor ve okundu). Elde bulunan git geçmişinde (sığ klon, 2026-09-18
sonrası) `metrics_scalp.json > round.models[].emitted` kayıtlarında da iki kolun adı hiç
geçmiyor; bu ikincil bir teyittir, birincil kanıt defterdir.

**Olgu YENİ DEĞİL, yazılı kapsam yanlış.** Karar 32 iki kolun 47 günlük backtest
penceresinde hiç tetiklemediğini ölçtü, karar 34 `momentum_burst`un sebebini yazdı
(kapı aritmetiği: `hedef/stop ≥ 1.5` ile `stop = 5×ATR`, yapısal engelin 7.5×ATR ötede
olmasını istiyor; `burst` tipik olarak 1–2.25×ATR). Yapılmayan şey, bu bulgunun
**kapsam tanımlarına işlenmesiydi**: README.md ve CLAUDE.md iki kararın ikisinden de
habersiz, "beş ortak kol" demeye devam etti. Bu turda düzeltilen odur.

**Eksen SONUÇLARI geçersiz değil, eksen KAPSAMLARI yanlış.** 11 ↔ 12, 12 ↔ 15 ve açık
olan 12 ↔ 16 çiftlerinin ikisi de aynı üç kolu gördü — kural 6 (aynı evren, aynı kurallar)
bozulmadı, çiftler hâlâ tek değişkende ayrışıyor ve ortalama R farkı hâlâ o değişkenin
ölçüsü. Bozulan şey, sonucun NE HAKKINDA olduğudur: "beş kollu bir modelde adaptasyonun
katkısı yok" ile "üç kollu bir modelde adaptasyonun katkısı yok" aynı cümle değildir.
İkincisi çok daha dar bir iddiadır ve karar 33'ün "bandit'in tahsis edecek bir şeyi yok"
teşhisini güçlendirir: tahsis uzayı beşte üç değil, pratikte ikidir — `rsi2_reversal`
123 pozisyonun 101'ini, `opening_range_breakout` 21'ini taşıyor, kalan bir tanesi
`vwap_pullback`.

**5. kolun sebebi BİLİNMİYOR ve bu turda ölçülmedi.** `momentum_burst`tan farkı budur:
onun sebebi geometriden türetilebiliyordu, `funding_spike_fade`inki türetilemiyor.
Kolun sırayla geçmesi gereken kapıları var — funding serisinin varlığı ve uzunluğu,
tazelik penceresi (`FUNDING_FRESH_HOURS`), `FUNDING_SPIKE_FLOOR` tabanı, `FUNDING_SPIKE_MULTIPLE`
çarpanı, gün-çapalı VWAP'in hesaplanabilmesi, sonra ev kapıları — ama hangisinde
elendiği DEFTERDE YAZMIYOR. Bir aday listesi yazıp birini seçmek, ölçmeden sebep
atamak olurdu; kayıt sebebi değil, sebebin BİLİNMEDİĞİNİ tutar.

**Bunun mümkün olmasının sebebi `ScalpModel`in `take_survey` uygulamamasıdır**
(`metrics_scalp.json`de beş kollu modellerin `survey` alanı `{}`). Karar 34 bunu zaten
görmüştü ve "yeni bir kol ya da tez eklenecekse `take_survey` o modelin ilk gereksinimidir"
yazdı — ama kuralı YALNIZCA yeni kollara bağladı, mevcut ölü kollara uygulanmadı ve
sonuç olarak 5. kolun sebebi bugün hâlâ bilinmiyor.

**AÇIK İŞ (bu turda yapılmadı):** `ScalpModel.take_survey` — beş kollu modellerin her
barda hangi kolun hangi kapıda elendiğini sebep koduyla sayması. Bu bir denetim izidir
(kural 15 statüsü: ölçüme girmez, sinyalleri ve sıralarını etkilemez) ve `funding_spike_fade`in
sebebini öğrenmenin TEK yoludur. Ayrı bir turda, ayrı bir kararla gelir.

**Yapılmayanlar ve gerekçeleri.** Ölü kolların kodu DURUYOR: `momentum_burst` bir
ölçüm sonucudur (karar 34'ün kapı aritmetiği onun üzerinden türetildi) ve silinmesi o
sonucun dayanağını yok ederdi; `funding_spike_fade` ise henüz ölçülmemiş bir tezdir —
sebebi bilinmeden silmek, "ölçtük ve tutmadı" ile "hiç ölçemedik"i aynı hücreye yazmak
olurdu (kural: veri yoksa `nan`, `0.0` değil). Eşikler de kurcalanmadı: bir kolu
tetikleyene kadar eşik gevşetmek, docs/backtest.md > 7'nin yasakladığı sonuca bakarak
parametre oynatmanın ta kendisidir.
## 49. Çıkış varyantı turu: hipotez teşhis aşamasında düştü, tur KAPANDI

**Soru dışarıdan geldi ve makuldü:** `ema_trend` bloke edilmişti (karar 47), kenar
giriş sinyalinde görünüyordu (dönem B'de ödeme oranı 1.61, kazanma oranı %34.7) ve
"çıkış geometrisi kenarı yiyor" tezi, çıkış mekanizması varyantlarını denemeyi
öneriyordu. Bu kayıt, o turun neden hiç kurulmadığını yazar.

**Hipotezin öncülü YAPI düzeyinde yanlıştı.** Tez, ters sinyalde pozisyonun çevrildiği
bir mekanizma varsayıyordu — kaynak sistemde (TradingView `strategy.entry`) gerçekten
öyle, ama repo implementasyonunda öyle değil: `strategies/ema_trend.py`
`manage_positions`ı uygulamaz, yani çıkış evreni `{tp, stop, liquidation}`tır ve
likidasyon iki dönemde de 0'dır. Ölçülen ters sinyal çıkışı payı **%0** (doğrulama
ölçütü >%30). Ödeme oranının hedefin (2R) altında kalmasının gerçek sebebi de başkaydı:
dolum kuralı (kural 13) emri SONRAKİ barın açılışından doldurur, yani kesişim barından
sonraki boşluk girişi yukarı kaydırır — hedefte kapanan pozisyon 2.0R değil **1.93R**,
stop'ta kapanan −1.00R değil **−1.05R** yazar. Kapı işini yaptı: hipotez düştü ve
varyant kurulmadı.

**Yerine geçen soru ölçülebilirdi, ama ancak YOL verisiyle.** Sürüklenmesiz bir
yürüyüşte hedefe (+3×ATR) stop'tan (−1.5×ATR) önce varma olasılığı %33.3'tür; ölçülen
%35.2 (A) ve %34.7 (B). Yani sapma var, küçük (1.4–1.9 puan) ve nerede yoğunlaştığı
bilinmiyordu — brüt kenar +0.057R/+0.041R, friksiyon 0.057R. Şeklini ölçmeden varyant
tanımlamak tahmin olurdu; tahminle tanımlanan varyant ise ilk kapıda "kurtarmak için
ayar arama"ya dönerdi (docs/backtest.md > 7.1).

**Ölçüm ayrı bir araçla ve salt okunur yapıldı** (`scripts/diagnose_ema_exits.py`,
Adım 0b): ikinci bir backtest değil (`run_backtest`i çağırır, pencereyi
`backtest_ema.py`den ithal eder), metrik hesaplamaz (kural 7), dönem B parametresi
YOKTUR — B, varyantların OOS penceresiydi ve yol istatistiğini bir kez görmek, ondan
sonra seçilen her eşiği B'ye bakarak seçilmiş yapardı. Determinizm kapısı içindedir ve
geçti: karara giren koşunun dönem A sayıları birebir yeniden üretildi (369 pozisyon,
130 tp, 239 stop, ort. R −0.0014889765).

**Seçim kuralı koşudan ÖNCE yazıldı, KODA konuldu ve bir kez çalıştı.** Dört dal
(kuyruk / geri dönüş / oyalanma / hiçbiri), yazılı öncelik sırası ve gerekçelendirilmiş
eşikler (docs/backtest.md > 6e). Kuralın metin değil FONKSİYON olmasının sebebi, tek
işinin "sonucu görüp seçmedik"i kanıtlamak olmasıydı: teşhis çıktısını bir insanın
okuyup dalı seçmesi, kuralın kapatmak için var olduğu serbestliği geri açardı. Ölçülen:
M2 +0.153R (eşik +0.25R), M1 0.552R (eşik 1.0R), M4 0.667× (eşik 2.0×). **Hiçbiri
tetiklemedi -> 4. dal: tur kapandı.**

**Üç şey öğrenildi ve üçü de kayıttır, kural değil:**

1. **Zaman stop'unun ön kabulü tersine çıktı:** kaybedenler kazananlardan daha HIZLI
   ölüyor (medyan 6 ↔ 9 bar). Bir zaman stop'u bu dağılımda önce kazananları keserdi.
2. **Kaybedenlerin yarısı hiç kâra geçmiyor** (medyan MFE +0.55R; %46.8'i +0.5R'yi bile
   görmüyor), yani breakeven'ın koruyacağı kâr çoğu kayıpta hiç oluşmuyor.
3. **Hedefte kesilen kuyruk ölçülemiyor:** çıkış sonrası İŞARETLİ hareketin medyanı
   ufuklar arasında işaret değiştiriyor (5 bar −0.10R, 10 bar −0.23R, 20 bar +0.15R,
   40 bar −0.37R). Azami yükseliş ölçüsü büyük çıkar ama sürüklenmesiz bir yürüyüşte de
   büyüktür — bu yüzden kural ona değil işaretli ölçüye bakar.

**Güç hesabı da bu turdan önce yazıldı ve turun ne vaat edebileceğini sınırlıyordu.**
`ema_trend`in R dağılımı iki noktalıdır (sd 1.42), n≈370'te SE ≈ 0.074, yani MDE
+0.20R. Gerçekçi bir çıkış iyileştirmesi (0.02–0.06R) bunun üçte biri ile beşte biri
arasındadır. Bunun iki sonucu vardı ve ikisi de ön-kayda girdi: (a) mevcut sonuç
"model negatif kanıtlandı" değil **"sıfır kanıtlandı"** demektir (BLOKE verdikti yine de
değişmez — C-3'ten de kalıyordu ve kanıtlanmamış bir kenara sermaye ayrılmaz);
(b) eksen istatistiği eşleştirilmiş ΔR olacaktı (varyantlar girişi ve stop'u miras
aldığı için aynı yolu paylaşırlar; ρ≈0.8 beklentisiyle MDE ≈ +0.13R). Tur kapandığı
için ikisi de kullanılmadı ama ön-kayıtta duruyor: bir sonraki tur aynı çıtayı bulacak.

**Sicile 3. satır olarak yazıldı** (docs/backtest.md > 6c), düştüğü hâlde — hatta tam
da düştüğü için. Satır BH paydasına girmez (hipotez bir model koşusuna hiç dönüşmedi,
düzeltilecek bir `p` yok) ama sicilde durur: kaç denemenin yapıldığı görünmezse,
kalan sonuçlar olduğundan anlamlı görünür.

**Açık kalan tek kaldıraç friksiyondur ve bu turun konusu DEĞİLDİ.** `cost_per_r ≈ 2c /
stop%` özdeşliği R başına friksiyonu stop MESAFESİNE bağlar, hedefe değil: 1.5×ATR'den
3.0×ATR'ye geçmek onu kabaca yarıya indirir. Bu ayrı bir geometri eksenidir; çıkış
ekseniyle birlikte oynatılsaydı aradaki fark iki değişkenin toplamı olurdu (13 ↔ 14
çiftinin düştüğü hata). Kaynak sistemde SL 2.5 denemesi çökmüştü ama orada hedef de
stop'la birlikte kaymıştı — stop ile hedefin AYRIŞTIRILMASI hiç test edilmedi. Sırası
gelirse kendi ön-kaydıyla gelir (docs/backtest.md > 6e > KAYIT).

## 50. Fonlama tezi BIRAKILMADI, ENGELLENDİ — engel veri yolu, tezin kendisi değil

**Ayrım kaydın sebebidir.** Bir tez ölçülüp düştüğünde (karar 36, karar 49) sonucu sicile
girer ve kapanır. Bu tez ÖLÇÜLEMEDİ: `scripts/measure_funding.py` dönem A'nın dağılımını
soruyordu ve soruyu soramadan **veri yolunda** durdu. İkisini aynı rafa koymak — "denendi,
olmadı" — yanlış olurdu: düşmüş bir tezi yeniden açmak yeni kanıt ister, engellenmiş bir
tezi açmaksa yalnızca yolun onarılmasını.

**Tez duruyor:** fonlama oranı kalabalıklaşmanın ölçülebilir bir işaretidir ve ekstremleri
bir kenar taşıyabilir. Bu kayıt o iddia hakkında hiçbir şey söylemiyor — ne lehte ne
aleyhte. Söylediği tek şey, iddianın bugünkü veri yoluyla SINANAMADIĞIdır.

**Açık iş, iki parçalı ve sırası bağlayıcı:**

1. **Dönem A için fonlama arşivi.** Ölçümün istediği pencere yıllarla ölçülür; canlı
   yolun verdiği pencere 60 gündür (aşağısı). Arşiv ayrı bir depo alanıdır ve
   `data/cache/`e YAZILMAZ — `core/data.py::fetch_funding` o önbelleği
   `data.funding_history_periods` ile budar, yani aynı dosyaya iki saklama kuralıyla
   yazmak canlı turun okuduğu seriyi bu aracın penceresine bağlardı
   (`scripts/measure_funding.py`nin zaten uyguladığı ayrım).
2. **Damga bazlı tutarlılık kanıtı, 60 günlük ÖRTÜŞMEDE.** Arşiv ile canlı serinin
   kesiştiği pencerede her damga birebir eşleşmelidir: aynı zaman damgası, aynı oran.
   **Kanıt geçmeden arşiv KULLANILMAZ** — ne bir dağılım raporunda, ne bir eşik
   seçiminde, ne bir backtest'te. Gerekçe kural 5'in aynısıdır: iki kaynaktan beslenen
   bir seri, ölçümün hangi veriyi gördüğünü belirsiz bırakır; ve sessizce ayrışan bir
   arşiv, sonucu veriye değil indirme tarihine bağlardı.

Sıra bir tercih değil: örtüşme kanıtı olmadan arşivin doğruluğu hakkında söylenebilecek
tek şey "başka bir kaynaktan geldi"dir.

**`data.funding_history_periods = 180` (≈60 gün) artık BİLİNÇLİ BİR KISITTIR.** Bugüne
kadar bir uygulama ayrıntısı gibi duruyordu; bundan sonra öyle okunmayacak. Tavanın
bağladığı yerler:

- **Backtest:** kaydı olmayan anda `core/funding.py::rate_at` None döner ve maliyet
  İŞLENMEZ (uydurma yok). 4 yıllık bir pencerede bu, eski dönemi sistematik olarak
  İYİMSER yapar — `ema_trend` koşusunda gerçekten böyle oldu ve
  docs/backtest.md > 6d'nin kabul edilen sapmalar listesinde 3. madde olarak duruyor.
- **Her türlü fonlama-persentili tezi:** göreli bir eşik (p99 gibi) sembolün kendi
  geçmiş dağılımını ister; 180 periyot o dağılımı tanımlamaya yetmez. Açık işin 1.
  parçası tam olarak budur.
- **`funding_spike_fade` kolu bu sığ pencereyle BESLENDİ** — katmanın tüm ömrü boyunca,
  istisnasız. Kol `MarketData.funding[sembol]` serisini okur ve o seri hiçbir zaman 180
  periyottan derin olmadı.

⚠ **Bu, kolun neden hiç tetiklemediğinin AÇIKLAMASI DEĞİLDİR** ve öyle okunmamalıdır.
Kolun kendi geriye bakışı `FUNDING_LOOKBACK = 8` periyottur (≈2.7 gün): taban oranı son
8 damganın ortalamasıdır, yani kol 9 kayıtla çalışabilir ve 180'lik tavan onu susturmaz.
Karar 48'in tespiti yerinde duruyor: **`funding_spike_fade`in sessizliğinin sebebi hâlâ
BİLİNMİYOR** (`ScalpModel` `take_survey` uygulamıyor, yani hangi kapıda elendiği hiçbir
yere yazılmıyor) ve bu açık bir iştir. Sığ pencereyi o boşluğa cevap diye koymak, karar
34'ün dersinin tersini yapmak olurdu: ölçülmemiş bir sebebi ölçülmüş gibi kaydetmek.

**Ölçüm aracının çıktısı YALNIZCA log'dur** (`.github/workflows/measure-funding.yml`
artifact bile üretmez), yani koşunun kendisi kalıcı bir iz bırakmaz. Engelin tam
mekanizması — hangi uç noktanın nereye kadar sayfalayabildiği — bu kayda girmiyor;
girmesi gereken yer arşiv işinin ön-kaydıdır.

**ÖLÇÜLDÜ (2026-09-19).** O ön-kayıt açıldı (docs/backtest.md > 6f) ve mekanizma
`scripts/probe_funding_depth.py` ile ölçüldü (`measure-funding` #35463452072). Sonuç
**(a) borsa tabanı**, iki sembolde de: uç nokta `/api/v5/public/funding-rate-history`
**~3 aylık KAYAN bir pencere** tutuyor (283 kayıt, en eski 2026-06-17), dönem A'nın
tamamı o pencerenin ~26 ay dışında ve doğrudan `after=2024-06-30` isteği BOŞ dönüyor.
Sayılar ve üç bağımsız doğrulama §6f > "SONUÇ — Adım A KOŞULDU"da; log süreli olduğu
için kalıcı kayıt oradadır, burada değil.

Üç yan bulgu ve biri bir DÜZELTMEdir:

1. **`fetch_history` kayıp vermiyor** — yürüyüş, tek istekle ulaşılabilen azami
   derinliğin tamamına ulaşıyor. Tavanı büyütmenin yolu kodda değil, bu kaydın 1.
   maddesindeki arşivdedir; yani yukarıdaki "açık iş" olduğu gibi duruyor.
2. **`/api/v5/public/history-funding-rate` diye bir uç nokta YOK** (HTTP 404).
3. ⚠ **"400 kayıt tavanı" hipotezi ÇÜRÜDÜ.** `limit=400` kabul ediliyor ama yalnızca
   283 kayıt var: kısıt bir sayfa boyu değil, kayan pencerenin kendisi. PR #37'nin
   "312 kayıt, 400'ün altında durdu, demek ki taban bizde olabilir" okuması yanlıştı.
   Çürütme buraya YAZILIYOR çünkü yanlış bir izin kaydı, doğru sonucun kaydı kadar
   iş görür: yazılmazsa aynı iz yeniden sürülür ve probe boşuna tekrar koşturulur.

**Tez hâlâ ENGELLENMİŞ, ama engel artık ÖLÇÜLDÜ.** Bu kaydın başındaki ayrım yerinde
duruyor: ölçülüp düşmüş bir tez değil, veri yolunda duran bir tez. Değişen tek şey,
yolun nerede ve neden bittiğinin artık tahmin değil ölçüm olmasıdır.

**A-2 de KAPANDI (2026-09-20): OKX-içi bir yol yok.** Tarihsel veri portalının sunduğu
olarak anılan kümeler tick bazlı işlem verisi ve OHLCV mumları; fonlama geçmişi
listelenmiyor ve fonlama için aynı kaynaklar REST API'yi işaret ediyor — probe'un
tabanına çarptığı uç nokta. Kanıt SINIFI kayda geçiyor: bu bir ikincil kaynak taraması,
portalın indirme listesinin doğrudan görüntüsü değil; doğrudan bir gözlem bunu tersine
çevirirse A-2 yeniden açılır. Bugünkü hâliyle iki bağımsız yol (doğrudan ölçüm + kaynak
taraması) aynı yöne işaret ediyor.

**Bu kaydın 1. maddesindeki "açık iş" artık BAŞLAYABİLİR.** Adım A kapandı, yani
docs/backtest.md > 6f'nin sabit aday sırası devreye girdi: **Bybit.** Sıra veriye
bakılarak değil venue tercihiyle seçilmişti ve bağlayıcıdır. **2. madde — damga bazlı
tutarlılık kanıtı — aynen geçerli ve hâlâ ÖN KOŞUL:** arşiv kurulsa bile C kapısı
geçilmeden hiçbir yerde kullanılmaz. Tek fark, cross-venue bir adayda o kanıtın literal
"aynı oran" biçimiyle değil §6f'nin üç koşuluyla (damga hizası, p95 olay kümesi Jaccard,
işaret uyumu) verilecek olmasıdır — gerekçe ve bedeli §6f > Adım C'de yazılı.

---

## 39-DOĞRULAMA: tahmin kıl payı tuttu, ama onarımın DAYANDIĞI MEKANİZMA çürüdü

Karar 39'un ön-kayıtlı tahmini: *`bars_processed` dağılımı 1'e yakınsamalı, sinyalsiz bar
oranı belirgin düşmeli, 00:00/08:00 yanlılığı kaybolmalı.* Onarım 2026-09-16T20:26'da
canlıya indi; ölçüm penceresi 2026-09-17T00:00 → 09-19T00:00 (elle tetiklenen 16:00 turu
DIŞLANDI — karışık kadans).

| | ÖNCE | SONRA |
|---|---:|---:|
| tur | 20 | 12 |
| işlenen bar | 27 | 14 |
| telafi barı (sinyalsiz) | 7 | 2 |
| **sinyalsiz bar oranı** | **%25.9** | **%14.3** |
| telafi yapan tur oranı | %35 | %16.7 |
| `missing_bars` | 0 | 0 |
| kaybolan saatler | 00:00 (4×), 08:00 (3×) | 20:00 (1×), 04:00 (1×) |

**Nokta tahmini iyileşti ama n=14 ile bu fark KANITLANMIŞ DEĞİLDİR** — 2/14 ile 7/27
arasındaki farkı ayırt edecek örneklem yok. Yanlılık iddiası ise önceki veride güçlüydü
(7 kaybın 7'si tam iki saatte; kayıplar altı bar-saatine düzgün dağılsaydı olasılık
≈ (2/6)⁷ ≈ 0.0005) ve o desen artık YOK — ama n=2 ile yokluğu da kanıtlanmaz.

### Asıl bulgu: "bara dört şans" diye bir şey olmadı

Karar 39-UYGULAMA şunu yazmıştı: *"Saatlik cron her 4H barına DÖRT bağımsız şans verir."*
Ölçüldü ve **yanlış:**

| | |
|---|---|
| zamanlanmış koşu | 14 (2026-09-16T23:37 → 09-19T07:16) |
| pencere | 55.6 saat |
| beklenen (saatlik cron) | ~56 |
| **teslim oranı** | **%23** |
| tetiklemeler arası aralık | medyan **4.1 saat** (min 2.5, max 6.2) |
| **4H bar başına düşen tetikleme** | **0.97** — varsayılan: 4 |

GitHub, cron ne yazarsa yazsın bu depoya kabaca **4 saatte bir** zamanlanmış olay
teslim ediyor. Saatlik cron, tetikleme SAYISINI artırmadı.

### O hâlde iyileşme nereden geldi

Eski cron (`5 0,4,8,12,16,20`) de bar başına ~1 tetikleme veriyordu, ama o tetiklemeler
bar kapanışına **ÇİVİLİYDİ** (:05). Düşen tetikleme her zaman aynı bar-saatini vuruyordu
— yanlılığın kaynağı buydu. Saatlik cron'da teslim edilen ~1 tetikleme **serbest fazda**
geziniyor; hangi bar penceresine düşeceği turdan tura değişiyor.

Yani onarım **oranı değil, KAYBIN CİNSİNİ** değiştirdi: sistematik (yanlı) kayıp,
rastgele (yansız) kayba döndü. Bu ölçüm için gerçek bir kazanımdır — yanlı kayıp seans
kırılımını kalıcı olarak bozar, yansız kayıp yalnızca örneklemi seyreltir — ama
karar 39-UYGULAMA'nın vaat ettiği şey bu değildi ve o cümle yanlıştı.

İki kaybın ikisi de bu mekanizmayla birebir açıklanıyor: 09-16T20:00 barı (kapanış 00:00)
23:37 ile 04:43 tetiklemeleri arasında kaldı; 09-18T04:00 barı (kapanış 08:00) 07:20 ile
12:55 arasında kaldı. İkisinde de aradaki boşluk bir bar penceresini tamamen yuttu.

### Ne yapılmadı ve neden

Hiçbir ayar değiştirilmedi. Kanıtlanmış alternatif ortada duruyor: **`run-scalp.yml`in
cron'u YOKTUR** ve depo dışı bir tetikleyiciyle ~15 dakikada bir koşuyor, hiç aksamadan
(bu ölçüm penceresinde scalp katmanı tek bar kaçırmadı). `run.yml`i de aynı yola almak
sinyalsiz bar oranını sıfıra yaklaştırır.

Ama bu bir **KADANS KARARIDIR ve kullanıcınındır:** dış tetikleyici depo dışında bir
bağımlılık demektir ve onun da düşmesi mümkündür — fark, düştüğünde görünür olmasıdır.
Öneri sunulmuştur, tek taraflı uygulanmamıştır.

**Ön-kayıt sicili (§6c) DEĞİŞMEDİ:** bu bir kural hipotezi değil, bir altyapı ölçümüdür ve
bir modelin performansı hakkında iddia taşımaz (karar 40'ın ölçütü).

---

## 51. Boş bir rapor YEŞİL dönemişti: `measure_funding.py`nin çıkış kodu kusuru

**Bu kayıt karar 50'nin BULGUSU hakkında değildir** — veri yolunun tıkalı olduğu, engelin
mekanizması ve açık iş orada yazılı ve orası tek kaynaktır. Burada kayda geçen şey aracın
o bulguyu nasıl SUNDUĞUDUR ve bu ayrı bir kusurdur.

**Kusur.** İlk koşu (`measure-funding` #35441623091) 13 sembolün hepsinde dönem A
penceresinde sıfır kayıt buldu ve bunu `nan` dolu bir dağılım tablosu, her eşikte "0 olay"
yazan bir sayım ve **çıkış kodu 0** ile bildirdi. Yani *"ölçtük ve bulamadık"* ile *"hiç
ölçemedik"* aynı hücreye yazıldı — `core/metrics.py`nin "veri yoksa `nan`, `0.0` değil"
kuralının tam olarak yasakladığı şey, bu kez çıkış kodunda. O hâliyle rapor **"dönem A'da
fonlama ekstremi yok"** diye okunurdu; doğrusu **"dönem A'da fonlama VERİSİ yok"**tur ve
ikisi aynı cümle değildir.

**İki onarım, ikisi de sayıya değil sayının DENETLENEBİLİRLİĞİNE dokunur:**

- **(a0) ÇEKİM İZİ** — sembol başına kaç sayfa çekildi, pencere filtresinden ÖNCE kaç ham
  kayıt görüldü, bunların en eskisi/en yenisi hangi tarihti ve sayfalama NEDEN durdu.
  Gerekçe: `coverage` yalnızca pencerenin İÇİNE bakar, oysa teşhis pencerenin DIŞINDA
  duruyordu. İz üç sebebi ayırt eder — sembol listelenmemiş (`boş sayfa`), kaynak o kadar
  geriye vermiyor (`kısa sayfa`), sayfalamamız bozuk (`imleç ilerlemedi`). İlk koşuda bu
  üçü tek bir "0 kayıt" satırına çökmüştü.
- **VERİ KAPISI** — pencerede hiçbir sembolde kayıt yoksa çıkış kodu **3**. Yeşil bir koşu
  okunabilir bir rapor demektir.

⚠ **Bu turda üretilen MEKANİZMA okuması YANLIŞTI ve karar 50 onu çürüttü.** Koşu log'undan
"sembol başına ~3 istek, 312 kayıt, 400'ün altında durdu" okunup sebep bir **sayfalama
tavanı** sanılmıştı — yani tabanın BİZDE olabileceği ihtimali açık bırakılmıştı.
`scripts/probe_funding_depth.py` ölçtü: `limit=400` kabul ediliyor ama o kadar kayıt YOK;
kısıt bir sayfa boyu değil, uç noktanın tuttuğu **~3 aylık kayan pencerenin kendisidir** ve
`fetch_history` ulaşılabilen azami derinliğin tamamına ulaşıyor, kayıp vermiyor.

**Dersin kendisi kaydın sebebidir:** yanlış olan şey teşhisin içeriği değil, teşhisin
ÜRETİLME BİÇİMİYDİ. Sayı log'un satır zaman damgalarından ve bir throttle sabitinden geri
hesaplanmıştı — yani ölçülmemiş bir mekanizma, ölçülmüş bir sayıdan türetilmişti. Doğru
yol karar 50'nin yaptığıdır: soruyu soran ayrı bir probe yazmak ve ön-kaydını koşudan önce
commit etmek. Bir araç kendi teşhisini üretemez; teşhis de ölçülür.
