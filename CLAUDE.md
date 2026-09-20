# CLAUDE.md

## Amaç

Bu proje, 10 farklı strateji modelinin **aynı piyasa verisini** görüp kendi izole sanal
hesabıyla paper-trading yaptığı ve performanslarının objektif biçimde karşılaştırıldığı bir
**ölçüm projesidir.** Kâr üretmek hedef değildir — hedef, stratejileri adil, tekrarlanabilir
ve manipüle edilemez koşullarda kıyaslamaktır. Bu ilke tüm tasarım kararlarına önceliklidir:
bir değişiklik ölçümün adilliğini bozuyorsa, ne kadar "daha iyi trading" sağlarsa sağlasın
reddedilir.

Projenin cevaplamaya çalıştığı ana soru: **short işlemler long işlemlerden daha mı
başarılı?** Bu yüzden long/short ayrımı raporlamanın merkezindedir (bkz. `core/metrics.py`).

Ölçüm **ÜÇ KATMANDA** yürür ve üçü de aynı çekirdeği kullanır (bkz. "Katmanlar"):
`base` 4 saatlik ana yarışma, `scalp` 15 dakikalık scalp katmanı, `ema` ise 4 saatlik ama
SABİT evrenli ölçüm kadrosudur (bugün tetikleyicisi yoktur — tanım var, koşu yok). Katman
ölçümün koşullarını (bar, sembol evreni, model listesi, defter, rapor dosyası) değiştirir;
kurallarını — maliyet, risk, likidasyon, dolum, metrik tanımları — DEĞİŞTİREMEZ. Katmanların
sonuçları aynı tabloda sıralanmaz: `base` ↔ `scalp` arasında zaman dilimi farkı, `base` ↔
`ema` arasında evren farkı doğrudan kıyası yanıltıcı yapar. Kıyas katman İÇİNDE yapılır —
bu yüzden `ema` katmanı kendi kıyas hedefini (`trend`), kontrolünü (`random_ctrl`) ve
çıpasını (`buyhold`) İÇİNDE taşır.

## Klasör Yapısı ve Modül Sorumlulukları

| Yol | Tek Sorumluluk |
|---|---|
| `main.py` | Bir turu uçtan uca çalıştıran giriş noktası: katmanı çöz -> veri çek -> `as_of` -> katmanın modellerini çalıştır -> metrikleri üret -> katmanın rapor dosyası. `--layer` hangi katmanın koşacağını seçer (varsayılan `base`); İKİ katman da bu tek giriş noktasını ve tek çekirdeği kullanır — ayrı bir giriş noktası orkestrasyonu (model kurulumu, hata izolasyonu, dry-run kopyası, yük yazımı) kopyalar ve iki katmanın sessizce ayrışmasına kapı açardı. İnce bir orkestrasyon katmanıdır, iş mantığı taşımaz. `--dry-run` deftere yazmadan raporlar. Model KURULUMU burada izole edilir (tanınmayan/patlayan model atlanır, koşu hata koduyla biter); defter ve veri hataları izole edilmez — onlar ölçüm hatasıdır, tur düşer. |
| `config.yaml` | Evrensel ayarlar: sembol evreni, zaman dilimi, başlangıç bakiyesi, çalışma sıklığı, funding parametreleri ve tüm risk/maliyet sabitleri — `risk_per_trade`, `leverage_cap`, `max_positions`, `max_short_positions`, `fee_rate`, `slippage_base`, `slippage_short_stop`, `maintenance_margin`, `max_stop_atr_multiple`, `random_seed`. Tüm modeller için tek kaynak; hiçbir modül bu değerlerin kendi kopyasını taşımaz. Değerler için bkz. "config.yaml Değerleri". |
| `core/layers.py` | Katman çözücü: `config.yaml > layers.<ad>` bloğunu kök ayarların üzerine derin birleştirmeyle bindirir ve katmanın kimliğini (defter kökü, rapor dosyası, sabit evren, saklama penceresi, kırılımlar) verir. Çözülmüş config'te `layers` anahtarı YOKTUR — çekirdek modüller tek bir "şimdi geçerli" değer görür. Eksik katman anahtarı `ConfigError`; tanınmayan katman adı sessizce `base`e düşmez. |
| `core/tags.py` | Defter `reason` alanındaki `\| anahtar=değer` etiketlerinin tek tanımı. Etiketi yazan taraf strateji, okuyan taraf `core/metrics.py`'dir; format tek yerde durur. `parse_tag` bulamadığı etikette `TagError` fırlatır — kol kırılımının anlamı "her işlem bir kola aittir" varsayımına dayanır ve etiketsiz satırı atlamak kırılımı sessizce eksiltirdi. |
| `core/config.py` | `config.yaml`'ı okuyan tek kapı. Eksik anahtarda `ConfigError` fırlatır; hiçbir varsayılan değer taşımaz — sessiz varsayılan, modellerin farklı maliyet/risk varsayımlarıyla yarışması demektir. |
| `core/data.py` | Piyasa verisi çekme/önbellekleme. Borsadan OHLCV + funding geçmişini çeker, `MarketData` üretir. Kapanmamış barı atmak (kural 12) ve `as_of`'u BTC referansından belirlemek burasının işidir; `as_of` barına sahip olmayan semboller o tur dışlanır ve loglanır. Strateji mantığı barındırmaz. |
| `core/engine.py` | Orkestrasyon: her BARI **iki geçişli** yürütür — önce normal modeller, sonra meta modeller (kural 4) — ürettikleri `Signal` listelerini `portfolio`'ya iletir. Trailing stop mantığı da burada. Zamanlama/akış kontrolü burada, iş mantığı değil. Doldurulamayan emirleri sebep koduna göre sayıp tur raporuna yazar (bkz. kural 15); kuyruğa GİREN sinyalleri de aynı raporda döker (`EmittedSignal`: bar, dolum barı, o barın kapanışı, stop/hedef/R:R) — `signals` bir sayıdır ve hangi sinyalin üretildiğini söylemez, oysa anlık bildirim (`scripts/telegram_signals.py`) tam olarak onu sorar ve defterde cevabı yoktur (dolum bir sonraki barda, kural 13). Kayıt salt denetim izidir: ölçüme girmez, sinyalleri ve sıralarını etkilemez. Modellerin kendi TARAMA SAYIMINI da toplar (`Strategy.take_survey` -> `ModelReport.survey`): `rejections` "emir neden dolmadı"yı sayar, bu ise "sinyal neden hiç üretilmedi"yi — ikisi turun ayrı aşamalarıdır ve tek sayıya çökerse "kurulum yoktu" ile "sinyal modülü bozuldu" ayırt edilemez. Sayım bar bazında TOPLANIR (telafi edilen bar da kendi taramasını yapar); patlayan bir çağrının yarım sayımı kaydedilmez. Atlanan turları telafi eder: son işlenmiş bardan `as_of`'a kadarki TÜM barları sırayla ilerletir (yalnızca son bara atlamak, atlanan barlardaki stop/TP/likidasyon kontrolünü hiç yapmamak olurdu) ve `signals_per_bar` açıkken her telafi barı KENDİ sinyalini de üretir (bkz. "Telafi edilen barlarda sinyal"). Anlık görüntü son işlenmiş bara kadar geri gitmiyorsa telafi mümkün değildir; o barlar `missing_bars` olarak sayılır ve loglanır — atlama sessiz olamaz. Aynı gerekçeyle `unchecked_position_bars`: bar İŞLENDİĞİ hâlde o sembolün mumu anlık görüntüde yoksa açık pozisyon o barda stop/TP/likidasyon kontrolünden GEÇMEZ (elde olmayan mumla tetiklemek uydurma olurdu) ve `last_processed_bar` ilerlediği için bar bir daha gelmez — kayıp telafi edilemez, yalnızca sayılabilir. İki sayaç ayrı durur çünkü sebepleri ayrıdır: turun geç kalması ≠ tek bir sembolün veri boşluğu. |
| `core/portfolio.py` | Pozisyon açma/kapama, boyutlandırma, **likidasyon kontrolü**, stop/TP tetikleme, bakiye güncelleme. Açık pozisyonların YOĞUNLAŞMASINI da burası hesaplar (`concentration`: net/brüt maruziyet, en büyük sembol payı) — bir ÖLÇÜMDÜR, kural DEĞİL: hiçbir sinyal bu sayılara göre elenmez, hiçbir boyut onlara göre değişmez (seans ve kayıp serisi kırılımlarıyla aynı statü). Gerekçe: korelasyon/net beta tavanı önerisi ancak modellerin gerçekten yoğunlaştığı GÖSTERİLİRSE tartışılabilir ve o sayı hiçbir yerde yoktu; üstelik tavan bedava değildir — 13 sembollük bir kripto evreninde pozisyonlar yüksek korelasyonludur, yani bir korelasyon tavanı `max_positions` kotasını (5) pratikte 1-2'ye indirir ve `min_trades` (30) kapısına zaten zor ulaşan bir katmanda ölçümü DURDURUR (kural 11'in "atlamak işlem sayısını sessizce düşürür" itirazının sert hâli). Kaba bir portföy katmanı zaten vardır: `max_positions`, `max_short_positions`, kopyanın `max_portfolio_risk`i. Her strateji için izole hesap durumu tutar. Pozisyon boyutlandırmasının **tek yetkili kaynağı.** Her barda sıra: önce `maintenance_margin` ile likidasyon kontrolü (mum içi `high`/`low` kullanılarak), **sonra** stop/TP kontrolü. Likidasyon stop'tan önce gelir; likide olan pozisyon stop'a hiç ulaşmaz. Kapanan işlemin `notes` kuyruğuna çıkışın ALT sebebini `| exit_rule=<kural>` etiketiyle yazar (bkz. kural 13c). |
| `core/funding.py` | Açık pozisyonlara funding/borrow maliyeti uygular. Borsa kurallarını simüle eder. |
| `core/metrics.py` | Performans metrikleri. **Birinci sınıf metrik: işlem başına ortalama R** (`PnL / risk_amount`) — bileşiklenmeden bağımsız olduğu için "bu model iyi mi" sorusuna toplam getiriden daha temiz cevap verir; tablo da ona göre sıralanır. Toplam getiri, Sharpe, max drawdown ve win-rate ikinci sırada raporlanır, atılmaz. **Her işlem metriği long ve short için AYRI hesaplanır ve ayrı raporlanır** (toplam değer de verilir, ama ayrışma yerine geçmez); özsermaye eğrisinden gelenler tek bakiye olduğu için hesap düzeyinde kalır. Ayrıca **maliyet ölçeği kolonlarını** (`avg_stop_distance_pct`, `cost_per_r`) model ve yön bazında raporlar — bkz. "Rapor Kolonları". Projenin ana sorusu "short işlemler daha mı başarılı" olduğu için bunların hiçbiri opsiyonel değil. **Ölçümün birimi POZİSYONDUR, defter satırı değil:** `merge_fills` aynı pozisyonun dolumlarını (kısmi çıkış ve `fraction < 1.0` olan take-profit'ler) tek ölçüm satırına indirger ve R'yi `Σpnl / Σrisk` olarak kurar — bkz. "Dolum ve pozisyon". Ayrıca üç **okuma yardımı** üretir (ortalama R'nin bootstrap aralığı, tur maliyeti yüzdesi, friksiyon hızı); hiçbiri kapı değildir, bkz. "Rapor Kolonları". Salt okunur — ledger'ı değiştirmez. |
| `core/report.py` | Dashboard yükü: `docs/data/metrics.json`'un tablo dışında kalan bölümleri (özsermaye eğrileri, açık pozisyonlar, son işlemler, model başına son 100 kapanmış işlem, son 24 saatin hareketi, havuz ve kabul bayraklarının toplanması). Salt okunur; hiçbir şey hesaplamaz ki `core/portfolio.py` zaten hesaplamış olsun. Tek istisna açık pozisyonun güncel PnL'idir ve kapanış formülünün aynı parçalarından kurulur (brüt − giriş komisyonu + funding; çıkış maliyeti YOK). Açık pozisyon satırı ayrıca çıkış yönetiminin DURUMUNU taşır — `breakeven_at_r`/`breakeven_done`, `partial_tp`/`partial_done`, `trail_giveback_pct`/`trailing_active`, `stop_rule`/`stop_moved` — ve İSTEK ile OLAY ayrı alanlardadır: mekanizmayı bildiren ama henüz tetiklenmemiş bir pozisyonu "yönetildi" göstermek, modeller 13/14/15'in ölçtüğü katkıyı yanlış okuturdu. Sunum sabitleri (kaç işlem gösterilir, eğri kaç noktaya seyreltilir) burada durur, `config.yaml`'da değil. |
| `core/ledger.py` | Her işlemi ve bakiye değişimini kalıcı, append-only biçimde katmanın defter kökü altına yazar. Sistemin denetim izi (audit trail) burasıdır. **Tek istisna `compact_equity`:** katmanın saklama penceresinden eski `equity.csv` satırlarını günlük özete indirir (bkz. "Katmanlar > Saklama penceresi"). `trades.csv` için istisna YOKTUR — bir işlem satırı hiçbir gerekçeyle değişmez veya silinmez. |
| `core/validate.py` | Her `Signal`in motora girmeden geçtiği tek doğrulama kapısı: izinli yön, stop/TP geometrisi, sıfıra bölme, fraction toplamı, sembol evreni, çıkış yönetimi alanlarının tutarlılığı (`trailing_atr` ile `trail_giveback_pct` aynı anda kullanılamaz). Ayrıca `validate_model`: model bayrak/limit bildiriminin kapısı (`ModelLimits` yalnızca `is_replica`, kaldıraç tavanı `REPLICA_LEVERAGE_CAP`), `strategies/registry.py` kurulumda çağırır. Geçersizde `ValueError`/`NotImplementedError` fırlatır, sessizce filtrelemez. |
| `strategies/base.py` | Tüm stratejilerin uyacağı soyut arayüz (`Strategy`, `Signal`, `Position`, `ExitInstruction`, `MarketData`). Mantık içermez, yalnızca sözleşme. |
| `strategies/scalp/arms.py` | Scalp katmanının **beş ortak kolu** (VWAP geri çekilme, açılış aralığı kırılımı, RSI(2) dönüşü, momentum patlaması, funding sıçraması fade'i) — ama **canlıda koşan ÜÇÜ:** `momentum_burst` ve `funding_spike_fade` katmanın tüm ömrü boyunca tek sinyal üretmedi (karar 48; defterde 0 işlem, 0 açık pozisyon). İlkinin sebebi ölçüldü (karar 34: kapı aritmetiği kolu imkânsız kılıyor), ikincisinin sebebi **BİLİNMİYOR** — `ScalpModel` `take_survey` uygulamadığı için hangi kapıda elendiği hiçbir yere yazılmıyor ve bu AÇIK BİR İŞTİR. Ölü kolların tanımı SİLİNMEZ: biri bir ölçüm sonucunun dayanağı, öteki henüz ölçülmemiş bir tez — ikisini de silmek "ölçtük ve tutmadı" ile "hiç ölçemedik"i aynı hücreye yazmak olurdu. İki model de bu tek kopyayı görür. Stop mesafesi her kolda aynıdır (`stop_atr_multiple × ATR`) — kollar stop ölçeğinde ayrışsaydı kol tablosu bir sinyal değil maliyet karşılaştırması olurdu. Hedef ise projeksiyon (`target_reward_risk × stop`) ile kolun yapısal engelinin YAKIN olanıdır. |
| `strategies/scalp/model.py` | Scalp modellerinin ortak gövdesi: stop tabanı (%1), hedef/stop kapısı (1.5R), zaman stop'u (16 bar), sinyal kurulumu, kol etiketi. Alt sınıfın değiştirebileceği noktalar SAYILIDIR ve **her biri ölçülen bir eksene karşılık gelmek zorundadır** — kural sayı değil, bu karşılıklılıktır: `choose_arm` (kol seçimi, 11 ↔ 12), `exit_management` (çıkış yönetimi, 12 ↔ 15), `rng_identity` (çekiliş kimliği), `time_stop_key` (zaman stop'unun SINIRI, 12 ↔ 16), `regime_filter` (ek rejim kapısı, 16 ↔ 17). Liste uzayabilir; uzatmanın bedeli şudur: **karşılığı bir eksen olmayan bir override noktası eklenemez.** Fark tek bir noktaya indirgenmezse modeller arası ortalama R farkı bir eksenin ölçüsü olmaktan çıkar. |
| `strategies/scalp_bandit.py` | **Model 11:** Thompson sampling ile kollar arası tahsis. Posterior yalnızca KAPANMIŞ işlemlerin gerçekleşmiş R'sinden beslenir ve her turda defterden sıfırdan kurulur (ayrı durum dosyası yoktur — ikinci bir doğruluk kaynağı olurdu). Isınma 20 işlem/kol, taban tahsis %5, kayan pencere 100 işlem. |
| `strategies/scalp_fixed.py` | **Model 12 (KONTROL):** aynı beş kol (canlıda üçü — karar 48), eşit ağırlıklı çekiliş, öğrenme yok. Model 11'in null hipotezi; `observe_closed_trades`ı bilinçli olarak UYGULAMAZ, yani geçmişe erişimi hiç yoktur. |
| `strategies/time_stop.py` | Zaman stop'unun TEK tanımı (modeller 11, 12, 14 ve 15 aynı kopyayı okur): `scalp.time_stop_bars` bar boyunca ne stop'a ne hedefe değmiş pozisyon piyasa fiyatından kapatılır. Yalnızca config'i okuyup `ExitInstruction` üretir (kural 10); dolum bir SONRAKİ barın açılışındadır (kural 13), yani gerçek ömür `bars + 1` bardır. Beş kollu modeller kuralı `ScalpModel` gövdesinden alıyordu, model 14 o gövdeden türemediği için HİÇ almıyordu — kural buraya çıkarıldı, çünkü iki uygulama `model 14 ↔ scalp_fixed` kıyasına ölçülmeyen bir değişken koyardı. Model 13'e UYGULANMAZ (kaynak sistemde yok, kural 15b). |
| `strategies/exit_management.py` | Üç aşamalı çıkış yönetiminin TEK tanımı (modeller 13, 14, 15 aynı kopyayı okur): breakeven -> kısmi çıkış + stop kaydırma -> geri verme takibi. Yalnızca config'i okuyup `Signal` alanlarına çevirir; uygulama `core/engine.py` (stop hareketleri) ve `core/portfolio.py`dedir (kısmi dolum) — kural 9'un `trailing_atr` için koyduğu sınırın aynısı. Üç dosyaya kopyalansaydı model 15 ile `scalp_fixed` arasındaki fark "yönetimin katkısı" olmaktan çıkar, "iki ayrı yönetimin farkı" olurdu. |
| `strategies/vwap/signal.py` | **Model 14'ün** sinyali (EV kuralları): gün-çapalı VWAP'ten `band_mult × hacim ağırlıklı sapma` kadar uzaklaşıp DÖNMEYE BAŞLAYAN bar — önceki bar bandın DIŞINDA kapanmış, bu bar VWAP'e doğru bir adım atmış ve HÂLÂ aynı tarafta olmalıdır. Dönüş şartı zorunludur: yalnızca "bant dışında" olmak, güçlü bir trendde her barda aynı sinyali üretirdi. Modül kurulumun YERİNİ verir; stop (sabit ATR katı) ve hedef (projeksiyon ile VWAP'in yakın olanı) model 14'ün kuralıdır. **Model 13 bu modülü OKUMAZ** — onun kuralları kaynak sistemin kurallarıdır ve `strategies/vwap/clone_signal.py`'dedir. Aday BULAMADIĞI barı da kaydeder (`Survey`): her tarama eleme sebeplerine göre sayılır, loglanır ve `take_survey` ile tur raporuna düşer — kural 15'in "ret sebep koduyla kaydedilir" şartının bu koldaki karşılığı. Sayım İKİ parçalıdır: eleme SEBEPLERİ (ayrık, Σ = taranan sembol) ve `|z_prev|` KOVALARI (`z_ge_1_0/1_5/2_0/2_5`, kümülatif, üst üste biner). Kovalar olmadan `bant_ici=13` satırı bandın kıl payı mı yoksa fersah fersah mı kaçırdığını söylemez — yani bandın ÖLÇEĞİ denetlenemez. İkisi ayrı alanlarda tutulur (`counts` / `extensions`), tur raporuna birlikte düşer (`Survey.report`). Sayım salt denetim izidir: hangi adayın üretileceğini ve sıralarını etkilemez. |
| `strategies/vwap/clone_signal.py` | **Model 13'ün** sinyali: kaynak sistemin (`vwap_detector.py`) kuralları, olduğu gibi. VWAP son `vwap_window` barın KÜMÜLATİFİdir (gün çapası yok, çapa her barda kayar); σ o sapma serisinin AĞIRLIKSIZ örneklem sapmasıdır (`rolling(std_window)`, ddof=1) — VWAP ağırlıklı, σ değil, ve bu tutarsızlık kaynakta gerçekten böyledir. Bant dışı olma şartı MEVCUT bara bakar; dönüş şartının tamamı `close > prev_close`tur ("sapma daraldı" şartı YOK). Stop ve hedef BURADA kurulur (`band_mult × sl_mult × σ` ve `max(\|VWAP − giriş\|,0) × tp_mult`), çünkü kaynakta geometri kontrolü (`sl < entry < tp`) onlara bakar — "VWAP geçilmiş" için ayrı bir eleme kuralı yoktur, eleme oradan gelir. `signal.py` ile kural mantığı PAYLAŞILMAZ (paylaşılsaydı model 13 ↔ 14 ekseninin tanımı bir dallanmanın durumuna bağlı olurdu); ortak olan yalnızca `core/indicators.py` yardımcılarıdır. Kendi `Survey`'i ve kendi kol etiketi (`vwap_revert_src`) vardır. Bkz. docs/decisions.md > 23. || `strategies/vwap_clone.py` | **Model 13 (KOPYA, `is_replica=True`):** dış bir sistemin kurallarını birebir yeniden üretir. Sinyali `strategies/vwap/clone_signal.py`'dedir (model 14 ile PAYLAŞILMAZ). Sabit teminat × 10x (`notional_fraction` + `ModelLimits.leverage`), üç aşamalı çıkış yönetimi, kendi limitleri (5 pozisyon, yönde 3, portföy riski %8), 12 sembollük kendi evreni, epsilon-greedy parametre öğrenimi (3 bant × 3 hedef = 9 kombinasyon, sembol bazlı, 3 örnek altında genele düşer). Seçim kaynağın üç adımıdır: o sembolde DENENMEMİŞ kombinasyon varsa önce o, sonra `epsilon` ile keşif, sonra sömürü. Evreni SIRAYLA tarar — güce göre sıralama yoktur. Ev kapıları (%1 stop tabanı, 1.5R, zaman stop'u) UYGULANMAZ — kaynak sistemde yok. Kopyalanamayan sapmalar docs/decisions.md > "Sadık kopyanın sınırları" altında yazılıdır. Yarışmacı değildir. |
| `strategies/vwap_managed.py` | **Model 14:** VWAP sapma-dönüş sinyali, EV kurallarıyla (`strategies/vwap/signal.py`; model 13 kendi kurallarını okuduğu için sinyal artık ortak DEĞİLDİR ve 13 ↔ 14 farkı sinyal farkını da içerir) — `sizing="risk"`, katmanın `leverage_cap`i, %1 stop tabanı ve 1.5R kapısı geçerli, parametre öğrenimi YOK (sabit çarpanlar config'te). Barda tek sinyal. Tam yarışmacı; kıyas hedefleri model 13 (ev kurallarının katkısı) ve `scalp_fixed`. |
| `strategies/scalp_managed.py` | **Model 15:** `scalp_fixed`in BİREBİR ikizi (aynı beş kol — canlıda üçü, karar 48 —, aynı eşit ağırlıklı çekiliş — `choose_arm` miras alınır, kopyalanmaz), tek farkı üç aşamalı çıkış yönetimi. Çekiliş kimliği (`rng_identity`) bilinçli olarak `scalp_fixed` ile PAYLAŞILIR: iki model her turda aynı kolu ve aynı sembolü seçer, aradaki ortalama R farkı yalnızca yönetimden gelir (eşleştirilmiş deney). |
| `strategies/scalp_patient.py` | **Model 16 (kâğıt katmanında KOŞAR, eşiği geçmiş DEĞİL):** `scalp_fixed`in ikizi, tek farkı zaman stop'unun SINIRI (16 ↔ 100 bar). Kol seçimi, kapılar, geometri ve çekiliş kimliği `ScalpFixed`ten MİRAS ALINIR. Cevapladığı soru: *kuruluma hedefine varacak süreyi vermek işe yarıyor mu?* Gerekçe karar 30'un ölçümüdür: 5×ATR stop + 10×ATR hedef, 16 barlık tipik yayılımın (√16 = 4×ATR) içinde ulaşılamaz ve `scalp_fixed`in 151 pozisyonundan 2'si hedefe vardı (%1.3 — sürüklenmesiz rastgele yürüyüşün öngördüğü %1.24). 100 sayısı TEORİDEN gelir (`N = (hedef/ATR)²`), veriden değil. Karar 33'te katmanın `models` listesine ALINDI — hareket eden tek eksenin diğer ucu odur ve ileriye dönük kanıt yalnızca kâğıtta birikir. Bu, `docs/backtest.md > 4`ün canlıya alma eşiğini geçtiği anlamına GELMEZ: C-1 (OOS ort. R > 0) sağlanmıyor (−0.01), yani gerçek parayla işlem açamaz. |
| `strategies/scalp_vol.py` | **Model 17 (ADAY — tezi DÜŞTÜ, karar 36; canlıda KOŞMAZ):** `scalp_patient`in ikizi, tek farkı **kesitsel volatilite rejimi kapısı** — sembolün `ATR/close` değeri o bardaki evrenin MEDYANININ altındaysa kurulum atlanır. Tez karar 35'in özdeşliğinden gelir: friksiyon notional'ın sabit yüzdesi, sürüklenme volatiliteyle ölçekleniyor; `scalp_patient`in başabaş noktası tam olarak medyan volatilitede (brüt %0.261 ↔ maliyet %0.284). Eşik SERBEST PARAMETRE DEĞİLDİR (medyan, süpürülmez) ve EVRENDEN hesaplanır, adaylardan değil — adaylara göre olsaydı eşik "o barda kaç aday var"a bağlanırdı. `take_survey` UYGULAR (karar 34'ün dersi: sayım olmadan ölü kol iki backtest sonra fark edilir). Ön-kayıt: `docs/backtest.md > 6b`; **sonuç: ön-kayıtlı birincil tahmin P1 düştü** (brüt sürüklenme %0.263 → %0.253, yani artmadı) — bkz. docs/decisions.md > 36. Katmanın `models` listesinde YOKTUR; `REGISTRY`de durur ve backtest onu `--models` ile çağırır. |
| `strategies/ema_trend.py` | **Model 18 (`ema` katmanı):** EMA(21) EMA(55)'i YUKARI kestiğinde long. Sinyal bir OLAYDIR, bir durum değil — "fast > slow" kuralı her barda tetiklenir ve modeli bir trend takipçisinden "yukarı rejimde sürekli alım"a çevirirdi. Kuralları dış bir sistemden (TradingView) gelir ama KOPYA DEĞİLDİR (kural 15b): kopya dış sistemin boyutlandırmasını da taşır ve yarışmaz; burada dışarıdan gelen yalnızca sinyaldir, boyut (risk %1), kaldıraç tavanı, maliyet, funding ve likidasyon evin kuralıdır — yani tam bir yarışmacıdır ve maliyet ölçeği kolonlarında `nan` ALMAZ. Stop 1.5×ATR (kural 14'ün bandının içinde), hedef tam 2.0R ve TEK dilim (`fraction=1.0`; kesirli hedef pozisyonu iki ölçüm satırına bölerdi). Trailing, üç aşamalı çıkış ve ZAMAN STOP'U yoktur — üçü de kaynak sistemde yok, eklemek modeli ölçülmek isteneni başka bir şeye çevirirdi. Zaman stop'unun yokluğunun bedeli ölçümdedir: pozisyon ömrü sınırsız olduğu için OOS embargosu (docs/backtest.md > 6.1) VARSAYILAMAZ, dönem A'da gözlenen azami tutuş süresinden ÖLÇÜLÜR. "Pozisyondayken sinyal yok sayılır" kuralı bu modülde DEĞİL `core/portfolio.py`dedir (kural 4: model kendi açık pozisyonunu göremez) ve ret bir sebep koduyla (`duplicate_position`) tur raporuna düşer. Parametreler `config.yaml > ema_trend` bloğunda ve ön-kayıtlıdır (docs/backtest.md > 6d): koşu sonucuna göre değiştirilemezler. **ATR YUMUŞATMASI bu modelde `wilder`dır** (RMA), projenin varsayılanı `simple` değil — bir parametre tercihi değil SPEC uyumu: kaynak sistem `ta.atr` ile doğrulandı ve stop/hedef mesafelerinin tamamı o değerden türüyor. Yumuşatmayı KÜRESEL çevirmek reddedildi (canlı koşan her modelin stop ölçeğini kaydırır ve defteri ikiye böler — karar 25'in `fee_rate` hatası). PERİYOT ortak kalır; ayrışan yalnızca yumuşatmadır ve config'te BİLDİRİLİR. Bedeli: "1.5×ATR" artık modeller arası birebir kıyaslanabilir DEĞİLDİR — kıyas `avg_stop_distance_pct`ten okunur ve motorun tavan kontrolü ORTAK tanımda kalır (her model kendi yumuşatmasıyla kendi tavanını genişletebilseydi tavan bir kural olmaktan çıkardı). Bkz. docs/backtest.md > 6d > TADİLAT-1. |
| `strategies/buyhold.py` | **Referans çıpası** (kural 15), yarışmacı değil: BTC %50 / ETH %50, 1x, stop'suz, bir kez alınır ve hiç satılmaz. `is_benchmark = True`. |
| `strategies/registry.py` | Model adı -> strateji sınıfı eşlemesi. `config.yaml`'ın `models` listesi buradan çözülür; tanınmayan ad sessizce atlanmaz. |
| `strategies/*.py` (ileride) | `Strategy`'den türeyen, yalnızca `generate_signals` uygulayan bağımsız, birbirinden habersiz modüller. |
| `data/` | Çalışma zamanı veri deposu (depoya girmez): `data/universe.json` ve `data/cache/<sembol>_<bar>.parquet`. Mum/funding önbelleği burada tutulur, her koşuda yalnızca eksik barlar çekilir. |
| `ledgers/` | Her stratejinin işlem/bakiye kayıtlarının tutulduğu çıktı klasörü (strateji başına dosya/alt klasör). |
| `docs/` | Tasarım kararları, metrik tanımları, kabul kriterleri. |
| `docs/index.html` | GitHub Pages dashboard'u: harici framework/CDN yok, build adımı yok (ortak tasarım dili ve yardımcılar `docs/shared.css` + `docs/shared.js`'te). Tek veri kaynağı `docs/data/metrics.json`. **İki seviye:** (1) genel bakış — üç özet kartı, LONG vs SHORT paneli (ana soru), tez tipine göre gruplanmış model kartları (ort. R, getiri, işlem sayısı, mini eğri, kabul rozetleri, rütbe), özsermaye eğrileri (tıklayınca izole), modeller arası getiri korelasyonu; (2) model detayı — üst şerit metrikler + rozet gerekçeleri, modelin long/short kırılımı, açık pozisyonlar, sayfalı kapanmış işlem listesi (20'şer, yön filtresiyle), modelin kendi eğrisi. Detayın adresi `#model=<ad>` hash'idir: geri tuşu, yer imi ve paylaşılan link çalışır. Koyu tema, mobil öncelikli: yatay kaydırma yoktur, geniş tablolar dar ekranda kart düzenine döner, dokunma hedefleri en az 44px. Süs katmanıdır: ölçüm defterde ve JSON'dadır, sayfa yalnızca onu çizer. Satır satır defter görünümü bu sayfada DEĞİL, `docs/positions.html`'dedir. |
| `docs/positions.html` | Yoğun defter görünümü: **tüm katmanlar ve tüm modeller tek sayfada**, model/katman/yön/sonuç/tarih filtreleriyle. İki tablo — açık pozisyonlar (anlık K/Z, anlık R, TP/SL, çıkış yönetimi rozetleri, kaldıraç, margin, riske edilen tutar, birikmiş funding, tam `reason`) ve kapanmış işlemler (sayfalı, 50'şer; sonuç = KAZANÇ/KAYIP + çıkış sebebi). Dashboard "hangi model önde" der, burası "tam olarak ne açık, tam olarak ne kapandı" der. **Kısmi çıkış ve fraksiyonel hedef satırları ayrı satır olarak görünür ama istatistiğe GİRMEZ** ve "DİLİM" etiketiyle durur — tamamlanmış işlem değil, aynı pozisyonun dilimidirler. **Sayfa ortalama R'yi ve kazanma oranını HESAPLAMAZ** (kural 7): ikisini de yükten okur (`docs/shared.js::statsSlice` -> `core/metrics.py`). İkinci bir hesap yolu bugün hizalansa bile yarın ayrışır ve aynı model iki sayfada iki farklı kazanma oranı gösterirdi. Katman etiketi her satırda durur ve ölçüm şeridi **katman başına AYRI bir kart** verir — `layer=all` dâhil hiçbir görünümde iki katmanın R'si tek sayıda toplanmaz: katmanlar arası kıyas burada da yapılmaz. Yükün cevaplayamadığı filtreler (sonuç, tarih) yalnızca tabloyu daraltır ve bu SÖYLENİR. 480px altında tablolar kart düzenine döner. |
| `docs/shared.css` | İki sayfanın ORTAK tasarım dili: renk paleti, tipografi, kart/tablo düzeni, rozetler, filtre ve sayfalama kontrolleri, dokunma hedefi tabanı. Sayfaya özgü görseller (eğri grafiği, ısı haritası, model kartları) kendi dosyasında kalır. Tek kopya olmasının gerekçesi biçimlendirmenin ölçümün parçası olmasıdır: aynı sayının iki sayfada farklı görünmesi okuyucuya iki ayrı ölçüm gibi gelir. |
| `docs/shared.js` | İki sayfanın ORTAK yardımcıları: biçimleyiciler (`num`, `price`, `qty`, `usd`, `pct`, `fullTs` — tanımsız metrik `—` olur, `0` DEĞİL), çıkış sebebi/alt sebep etiketleri, renk-kimlik ataması (`assignStyles`), yük okuma (`fetchPayload`) ve **ölçüm okuma (`statsSlice`)** — iki sayfanın da ortalama R / kazanma oranı / işlem sayısını aldığı TEK yol; `model` verilmezse havuz (yalnızca yarışmacılar) okunur. Klasik script; build adımı, bundler ve CDN yok. |
| `scripts/telegram_report.py` | **Günlük PERFORMANS özeti** (base katmanı). `as_of` saati 20:00 (UTC) olan turda yollanır; token'lar ortamdan (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`) okunur. **Her yolda 0 döner** — eksik token, ağ hatası, Telegram 4xx'i, bozuk JSON: hepsi loglanıp geçilir. Özet bir bildirimdir, ölçümün parçası değil; Telegram kesintisi turu kırmızıya çeviremez. |
| `scripts/telegram_signals.py` | **Anlık SİNYAL bildirimi** (scalp katmanı). Günlük özetten AYRIDIR ve ona dokunmaz: tetikleyen tek olay yeni sinyaldir — pozisyon kapanışı, funding tahakkuku ve bar ilerlemesi mesaj üretmez. Kaynağı turun kendi raporudur (`round.models[].emitted`, bkz. `core/engine.py::EmittedSignal`); defteri okumak yetmezdi, çünkü sinyalin haber değeri olduğu an henüz DOLMAMIŞTIR (kural 13). **Üç filtre:** yalnızca `as_of` barının sinyalleri (telafi barlarınınki deftere yazılır ama bildirilmez — emri çoktan dolmuştur), aynı (model, sembol, yön) için 4 bar susturma (durum: `state/telegram_scalp.json`), 5'ten fazla sinyalde tek toplu mesaj. Mesaj ZORUNLU bir uyarı satırı taşır: emir bir sonraki barın açılışından dolacağı için okuyucunun girişi bottan farklı fiyattan olur. `telegram_report.py` ile aynı söz: **her yolda 0 döner** ve aynı secret'ları kullanır. |
| `scripts/measure_vwap_signal.py` | **Kalibrasyon ÖLÇÜM aracı** (model 14), ölçümün parçası değil: salt okunur, deftere yazmaz, hiçbir modelin davranışına dokunmaz. Kurulumların hangi kapıda elendiğini sayar — aday, %1 stop tabanı, 1.5R kapısı, geçen — ve `vwap.managed.atr_multiple` eksenini süpürür (`--bands` ile eski `band_mult` ekseni de koşulur). Kendi gösterge/sinyal matematiğini YAZMAZ: `core/indicators.py`'yi çağırır, bant/dönüş karşılaştırmaları `strategies/vwap/signal.py::_evaluate`in, kapılar `strategies/vwap_managed.py::_passes_gates`in birebir aynısıdır ve `--verify` bunu rastgele barlarda `vwap_signal.scan()` ile karşılaştırıp kanıtlar — ikinci bir uygulama, ölçtüğü modelden sessizce ayrışabilecek bir sayı üretirdi. Model 13'ün `clone_signal.py`'sini import ETMEZ. Tetikleyicisi `.github/workflows/measure-vwap.yml` (yalnızca `workflow_dispatch`, cron yok). |
| `scripts/measure_slippage.py` | **Kayma varsayımının ÖLÇÜM aracı**, ölçümün parçası değil: salt okunur, deftere yazmaz, `config.yaml`a dokunmaz, hiçbir modelin davranışını değiştirmez. Cevapladığı soru: *`slippage_base` her sembolde aynı şeyi mi anlatıyor?* Kitabı **BYBIT**'ten okur — veri OKX'ten gelir ama maliyeti ödeyen taraf hesabın tutulduğu yerdir (`fee_rate`in aynı ayrımı); `--venue okx` ile öteki taraf da ölçülür ve FARK da bir bilgidir. **Emir boyu uydurulmaz, DEFTERDEN okunur** (sembol başına medyan notional): impact emir boyuna bağlıdır ve "tipik" bir boy varsaymak ölçümü o varsayıma bağlardı; defterde işlemi olmayan sembol `—` ile geçilir. Tek anlık görüntü yerine N örneğin MEDYANI alınır (spread seansa göre değişir). Gerekçe karar 35/36'nın bıraktığı tek açık kaldıraçtır: maliyet. **Sembole bağlı kayma UYGULAMAZ** — o ayrı bir karardır, defteri tarihli olarak böler (karar 25'te `fee_rate`in böldüğü gibi) ve geriye dönük uygulanamaz (kural 1). |
| `scripts/measure_funding.py` | **Fonlama dağılımının ÖLÇÜM aracı**, ölçümün parçası değil: salt okunur, deftere yazmaz, `config.yaml`a dokunmaz, `data/cache/`e bile yazmaz (`core/data.py::fetch_funding` önbelleği `data.funding_history_periods` ile budar, bu ölçüm yıllar ister — aynı dosyaya iki saklama kuralıyla yazmak canlı turun önbelleğini bu aracın penceresine bağlardı) ve hiçbir modelin davranışını değiştirmez. Cevapladığı soru: *4H'de bir fonlama-ekstremi tezi bu veriyle ÖLÇÜLEBİLİR Mİ — hangi eşikte kaç olay var?* **Getiri/R/PnL HESAPLAMAZ** ve `core/metrics.py`, `core/portfolio.py`, `core/ledger.py`, `strategies/*` modüllerini import ETMEZ (test: `tests/test_measure_funding.py`); ayrım kasıtlıdır — dağılımı görmek eşik seçimini kirletmez, sonucu görmek kirletirdi (docs/backtest.md > 7). Eşik bu raporun SONUCU DEĞİLDİR; ayrı bir adımda ön-kayıtla seçilir. **Kapsam YALNIZCA dönem A'dır ve bu bir KAPIDIR:** sınırlar `scripts/backtest_ema.py`den İTHAL EDİLİR (iki yerde yazılı bir pencere bir gün ayrışır) ve `--end` kesimi aşarsa betik HATA KODUYLA BİTER — dönem B bir sonraki tezin OOS penceresidir. Isınma penceresi dönem A'nın İÇİNDEN yenmez, ÖNCESİNDEN alınır: kısmi pencereyle hesaplanan bir p99 iki gözlemle tanımlanır. İki kural SEÇİLİR ve raporun başlığında YAZILIR: kayan pencere 270 periyot (90 gün, cari kaydı `shift(1)` ile DIŞLAR — içerseydi olay kendi eşiğini tanımlardı) ve kümeleme (ardışık damgalar tek olay; 24 saatlik soğuma varyantı da AYRI raporlanır, çünkü "bir damga düşüp geri çıktı" tek epizod mu iki mi sorusu bir yargıdır ve tek sayıya indirmek onu gizlerdi). Kümeleme SEMBOL bazındadır. Pozitif ve negatif kuyruk AYRI sayılır: ikisi ayrı tezdir (aynı gerekçe `funding_spike_fade`in tek yönlü olması). **İLK KOŞUNUN SONUCU: OKX bu pencereyi VERMİYOR** — 13 sembolün hepsinde pencerede sıfır kayıt. Bu bir ARAÇ hatası değil VERİ bulgusudur; sebebi ve ölçülmüş mekanizması karar 50'dedir (uç nokta ~3 aylık KAYAN bir pencere tutuyor, 283 kayıt — `scripts/probe_funding_depth.py` ölçtü). ⚠ Bu satır bir zamanlar sebebi "~300-400 kayıtlık sayfalama tavanı" diye yazıyordu; o okuma ÇÜRÜDÜ (`limit=400` kabul ediliyor ama o kadar kayıt YOK) ve `fetch_history` kayıp vermiyor — ulaşılabilen azami derinliğin tamamına ulaşıyor. Araç yine de iki yerde onarıldı (karar 51): **(a0) ÇEKİM İZİ** bölümü borsanın sayfalama TABANINI raporlar (üç sebebi ayırt eder: sembol listelenmemiş / borsa o kadar geriye vermiyor / sayfalamamız bozuk — `coverage` yalnızca pencerenin İÇİNE baktığı için teşhis pencerenin DIŞINDA kalıyordu), ve bir **VERİ KAPISI** eklendi: pencerede hiçbir sembolde kayıt yoksa çıkış kodu **3** (ilk koşu bu raporu SIFIR kodla döndürmüştü). Kapının gerekçesi `core/metrics.py`nin "veri yoksa `nan`, `0.0` değil" kuralının çıkış kodundaki karşılığıdır — yeşil bir koşu okunabilir bir rapor demektir, `nan` dolu bir tablo değil. Tetikleyicisi `.github/workflows/measure-funding.yml` (yalnızca `workflow_dispatch`, cron yok). |
| `scripts/probe_funding_depth.py` | **Fonlama veri yolunun DERİNLİK PROBE'u**, ölçümün parçası değil: salt okunur, deftere yazmaz, `config.yaml`a dokunmaz, `data/cache/`e yazmaz, hiçbir modelin davranışını değiştirmez. Cevapladığı soru `measure_funding.py`nin sorusu DEĞİL, onun ÖNÜNDEKİ engeldir: *dönem A'nın fonlama geçmişine OKX'in kendi yollarından ULAŞILABİLİYOR MU?* (karar 50; ön-kayıt docs/backtest.md > 6f — belge bu betik hiç koşmadan commit edildi). **Rapor YALNIZCA meta veri taşır — durum, kayıt sayısı, zaman damgası, sayfa boyu — ve hiçbir yerde bir fonlama ORANI yazmaz;** yanıtın oran alanının adı kaynakta hiç geçmez (test: `tests/test_probe_funding_depth.py`). Bu, salt okunurluktan AYRI ve ondan DAR bir sözdür: derinliği ölçmek en taze sayfadan geriye yürümeyi gerektirir, yani istekler dönem B'ye denk gelen damgaları da getirir (`fetch_history` de aynısını yapıp atar) — damga saymak o pencereye BAKMAK değildir, oran okumak olurdu. **Yürüyüş İKİNCİ KEZ YAZILMAZ:** ölçülen şey tam olarak `measure_funding.py::fetch_history`in davranışıdır, bu yüzden probe o fonksiyonu ÇAĞIRIR ve yalnızca dönen serinin İNDEKSİNE bakar (aynı gerekçe `backtest_ema.py`nin `run_backtest`i çağırmasında). `measure_funding.py`ye bir bayrak olarak EKLENMEZ: orada dönem A kesimi bir KAPIDIR (`--end` aşarsa hata) ve derinlik sorusu tanımı gereği uç noktanın TAMAMINA sorar — ikisini tek betiğe koymak o kapıyı gevşetmeyi gerektirirdi. (a) borsa tabanı ↔ (b) yürüyüş tabanı ayrımı **MEKANİKTİR** (`classify_depth_floor`; kural docs/backtest.md > 6f'de probe koşmadan ÖNCE yazıldı — aynı gerekçe `diagnose_ema_exits.py::select_primary_family`) ve ölçütü "daha ESKİ kayıt döndü mü"dür, "kayıt döndü mü" değil: `after` yok sayılırsa uç nokta en taze sayfayı döndürür ve o, derinlik hakkında hiçbir şey söylemez. Belirsiz bir sonuç (istek hatası) sessizce "(a) borsa tabanı"na DÜŞMEZ — betik hata koduyla biter. **KOŞULDU (2026-09-19, #35463452072); sonuç: (a) borsa tabanı** — uç nokta ~3 aylık KAYAN bir pencere tutuyor (283 kayıt) ve dönem A o pencerenin ~26 ay dışında; `fetch_history` kayıp VERMİYOR, veri orada değil. "400 kayıt tavanı" hipotezi de çürüdü (`limit=400` kabul ediliyor, kayıt yok). A-2 (OKX tarihsel veri portalı) 2026-09-20'de KAPANDI — portal tick + OHLCV sunuyor, fonlama geçmişi yok ve fonlama için REST API'ye, yani aynı tabana işaret ediliyor. **ADIM A KAPALI, ADIM B AÇIK (Bybit).** Sayılar ve kanıt sınıfı docs/backtest.md > 6f > SONUÇ; karar 50. Tetikleyicisi `.github/workflows/measure-funding.yml`in ilk adımıdır (yalnızca `workflow_dispatch`, cron yok). |
| `scripts/backtest.py` | **Backtest harness**, ölçümün parçası değil: canlı MOTORU geçmiş bir pencerede koşturur. **İkinci bir motor YAZMAZ** — `core/engine.py`nin telafi yolu zaten bar bar bir backtest'tir ve sadakati `tests/test_engine_per_bar.py::test_catch_up_matches_running_each_bar_in_its_own_round` ile sabittir. Harness'ın yaptığı üç şey: ayrı defter kökü (`backtests/`), `last_processed_bar` TOHUMLAMA (boş defterde `_timeline` bilinçli olarak yalnızca son barı işler) ve `signals_per_bar: true`. Model kurulumunu `main.py::build_strategies`ten ALIR, kopyalamaz — kurulum iki yerde ayrışırsa backtest canlıda koşandan başka bir model kümesini ölçer. Maliyet/dolum/likidasyon/metrik sabiti İÇERMEZ; hepsi katmanın config'inden gelir. Bayrakları İKİ SINIFTIR ve karışmazlar (docs/backtest.md > 5g): sonucu DEĞİŞTİRMEMESİ sınanan derinlik ayarları (`--history-bars`, `--funding-periods`; ikisi de yalnızca derinleştirir) ve sonucu DOĞRUDAN kaydıran koşul ayarları (`--fee-rate`/`--slippage-base` dış bir referansla parite için, `--symbols` evreni DARALTMAK için — genişletemez, `--signal-cutoff` dönem ataması için: yeni sinyali durdurur, pozisyon yönetimini durdurmaz). İkinci sınıfın her kullanımı `manifest.json > deviations`a yazılır ve maliyet override'ı ayrıca bağırır. `--results-json` metrik/kırılım/tutuş süresi/sapma yükünü makine okunur yazar — hizalanmış bir tabloyu elle ayrıştırmak, sayıların ikinci bir kopyası demekti. `--verify-live` **Kapı 0**'dır: canlı turların `emitted` kayıtlarıyla (git geçmişinden) sinyal karşılaştırması yapar; uyarlanabilir OLMAYAN modeller birebir eşleşmeli, uyarlanabilir olanlardan (kural 16) eşleşme beklenmez ve kapı sayılmaz. Uyarlanabilirlik `observe_closed_trades` kancasından TÜRETİLİR, elle listelenmez. |
| `scripts/backtest_ema.py` | **`ema_trend`in ön-kayıtlı koşusu** (docs/backtest.md > 6d), ölçümün parçası değil: iki dönem × (portföy + coin başına) koşusunu ön-kayıtta sabitlenen SIRAYLA çağırır ve tek bir versiyonlanabilir yüke indirir. **İkinci bir backtest DEĞİLDİR** — `scripts/backtest.py::run_backtest`i çağırır ve hiçbir metrik burada hesaplanmaz (kural 7): bir ortalama ya da kâr faktörü burada hesaplansaydı aynı defterin iki cevabı olurdu. İki koşu SINIFI ayrı durur ve toplanmaz: portföy koşusunda kota (`max_positions`) BAĞLAR ve kabul çıtası ondan okunur (canlı onu yapacak), coin başına koşuda kota bağlamaz ve dış bir referansla (TradingView) kıyasın tek geçerli biçimi odur. Dönem B'nin embargosu VARSAYILMAZ, dönem A'dan ÖLÇÜLÜR (`holding_stats`): model zaman stop'u taşımadığı için docs/backtest.md > 6.1'in dayandığı üst sınır tanım gereği yoktur — bu yüzden B, A bitmeden başlatılamaz. Kapıları MEKANİK uygular (`evaluate_gates`): P1 paritesi bir tahmin değil KAPIDIR, model sahibinin K-1..K-3'ü ile repo'nun C-1..C-4'ü (canlıyla aynı `acceptance_flags`) BİRLİKTE bağlayıcıdır ve tek istisna (yalnız çıpa koşulundan kalma) otomatik geçiş değil DURMA üretir. Tetikleyicisi `.github/workflows/backtest-ema.yml` (yalnızca `workflow_dispatch`, cron yok). |
| `scripts/diagnose_ema_exits.py` | **`ema_trend`in dönem A YOL teşhisi** (Adım 0b), ölçümün parçası değil: salt okunur, deftere yazmaz, `config.yaml`a dokunmaz, hiçbir modelin davranışını değiştirmez ve hiçbir kabul kapısı buradan okunmaz. Cevapladığı soru: *kesişim sinyalinin taşıdığı sapma (TP payı %35.2 ↔ sürüklenmesiz yürüyüşün %33.3'ü) fiyat YOLUNUN neresinde yoğunlaşıyor?* **İkinci bir backtest DEĞİLDİR** — pencereyi `scripts/backtest.py::run_backtest` koşar ve dönem A'nın parametreleri `scripts/backtest_ema.py`den İTHAL EDİLİR (iki yerde ayrışan bir pencere, teşhisin karara giren koşudan başka bir şeyi ölçmesi demekti). Metrik BURADA HESAPLANMAZ (kural 7): ortalama R, kırılımlar ve tutuş süresi `core/metrics.py`den, yüzdelikler onun tek tanımından gelir. Kendi hesapladığı tek şey **yol istatistiğidir** (MFE/MAE, çıkış sonrası devam) ve o `core/metrics.py`de yoktur. MFE/MAE'nin KAPANIŞ BARI HARİÇ hâli ayrıca tutulur: stop hareketleri bar kapandıktan sonra uygulanır (kural 13b), yani bir çıkış kuralı ancak önceki barların hareketini görebilirdi — kapanış barını dâhil etmek o kurala pozisyonu öldüren barın içindeki bilgiyi vermek olurdu. **Dönem B parametresi YOKTUR** ve bu yapısaldır: B, çıkış varyantlarının OOS penceresidir; yol istatistiğini bir kez görmek, ondan sonra seçilen her eşiği B'ye bakarak seçilmiş yapardı. İçinde bir **determinizm kapısı** taşır — karara giren koşunun (`backtest-ema` #35391881083) dönem A sayılarını birebir üretmezse hiçbir yol istatistiği okunmaz ve betik hata koduyla biter (docs/backtest.md > 1'in aynı mantığı). Ayrıca **birincil varyant seçim kuralının ÇALIŞTIRILABİLİR kopyasını** taşır (`select_primary_family`): kural teşhisin İÇİNDE, mekanik olarak uygulanır ve çıktısı log'a basılır — sonucu bir insanın okuyup dalı seçmesi, kuralın kapatmak için var olduğu "baktım, en iyi görüneni seçtim" serbestliğini geri açardı. Eşikler burada sabittir ve docs/backtest.md > 6e onları ALINTILAR; iki yerde yazılı bir kural bir gün sessizce ayrışır ve kuralın kanıt değerini yok ederdi. Tetikleyicisi `.github/workflows/diagnose-ema-exits.yml` (yalnızca `workflow_dispatch`, cron yok). |
| `.github/workflows/diagnose-ema-exits.yml` | Yol teşhisinin elle tetikleyicisi. `backtest-ema.yml`den ayrıdır çünkü o ön-kayıtlı bir SIRAYI (A → embargo → B) koşar ve karara giren sayıları üretir; bu iş karar üretmez ve dönem B'ye hiç dokunmaz — ikisini tek workflow'a koymak, bir teşhis koşusunun yanlışlıkla OOS penceresini de koşturması demekti. `permissions: contents: read`; sonuç log'a basılır (özet yük, pozisyon satırları hariç) ve artifact'e çıkar. Cron YOKTUR (docs/backtest.md > 7). |
| `.github/workflows/measure-funding.yml` | Fonlama VERİ YOLUNUN elle tetikleyicisi; **İKİ adım, İKİ ayrı soru:** (A) derinlik probe'u — *dönem A'ya ulaşılıyor mu?* —, (B) dağılım ölçümü — *hangi eşikte kaç olay var?* **Probe varsayılan olarak AÇIK, ölçüm varsayılan olarak KAPALIDIR:** (B) bugün veri yolunda duruyor (karar 50) ve (A) tam olarak o engelin teşhisidir — arşiv docs/backtest.md > 6f'nin kapılarını geçmeden (B)'yi koşturmak, 60 günlük bir pencereyle 30 aylık bir soruyu cevaplamaya çalışmaktır. Probe ÜÇÜNCÜ bir workflow'a konmaz: bu dosya zaten "fonlama verisi, salt okunur, cron yok, `contents: read`" işidir ve probe tam olarak buradaki ölçümün önünü açıyor — ayrı bir dosya, bir soruyu onu cevaplayacak araçtan ayırırdı. `backtest-ema.yml`den ayrılığı DEĞİŞMEDİ: o ön-kayıtlı bir SIRAYI (A → embargo → B) koşar ve karara giren sayıları üretir; buradaki iki adım karar üretmez ve dönem B'ye hiç dokunmaz — ölçüm betiği kesimi aşan `--end`de hata koduyla biter, probe ise yalnızca damga sayar, oran OKUMAZ. Dönem sınırları workflow GİRDİSİ olarak açılmaz: açılsalardı dönem B'ye bakmak bir yazım hatası kadar kolay olurdu. Probe'un sembol girdisi komut metnine gömülmez, ortam değişkeninden okunur (kabuk enjeksiyonu kapısı). `permissions: contents: read` — bu iş yazamaz. Çıktı YALNIZCA log'dur; artifact bile üretilmez. Cron YOKTUR (docs/backtest.md > 7). |
| `.github/workflows/backtest-ema.yml` | `ema_trend` koşusunun elle tetikleyicisi. `backtest.yml`den ayrıdır çünkü o tek bir pencereyi tek komutla koşar; bu koşu ön-kayıtlı bir SIRA gerektirir (A -> embargo ölçümü -> B) ve sırayı workflow girdilerine bırakmak, sıranın koşudan koşuya değişebilmesi demekti. `permissions: contents: read` — bu iş yazamaz; sonuç dosyaları artifact olarak çıkar ve log'a da basılır. Cron YOKTUR (docs/backtest.md > 7). |
| `docs/backtest.md` | Backtest'in **ön-kayıtlı** değerlendirme kuralları: sonuç üretilmeden önce yazıldı ve commit edildi (tarih damgası git'te). Birincil metrik, geçerlilik kapıları, canlıya alma eşiği, IS/OOS kontaminasyonu, kabul edilen sapmalar ve "sonucu gördükten sonra yapılmayacaklar" listesi. Gerekçe projenin kendi geçmişidir (kararlar 27 ve 28): veriye bakıp sonradan kural yazmak iki kez yanlış sonuca götürdü. |
| `.github/workflows/backtest.yml` | Backtest'in elle tetikleyicisi (`workflow_dispatch`, cron YOK). `permissions: contents: read` — bu iş yazamaz; gerçek defterin bir backtest tarafından değiştirilmesi niyetle değil YETKİYLE imkânsızdır. `fetch-depth: 0` şart: Kapı 0 canlı `emitted` kayıtlarını git geçmişinden okur. Cron'un olmaması bilinçli: periyodik backtest, `docs/backtest.md > 7`nin yasakladığı "sonucu görüp tekrar koşma"yı otomatikleştirirdi. |
| `backtests/` | Backtest çıktıları (depoya GİRMEZ). Defterin aksine (kural 1) geçmişi tutulmaz: bir backtest ölçümün kendisi değil, ölçüm hakkında bir denemedir ve aynı girdilerle yeniden üretilebilir (`random_seed` sabit, koşu kendi config parmak izini `manifest.json`a yazar). Karara giren sayılar `docs/`a yazılır. |
| `tests/` | Her `core` modülü ve her strateji için bağımsız birim testleri. |
| `state/` | Bildirim bookkeeping'i, ölçüm DEĞİL: `state/telegram_scalp.json` yalnızca "hangi kurulum en son hangi barda bildirildi" bilgisini tutar (susturma penceresi). Defterden ayrı durur çünkü defter denetim izidir (kural 1) ve bu dosya silinse ölçüm hiç değişmez — en kötü ihtimalle bir mesaj tekrar eder. Koşular arası commit edilir (runner her koşuda sıfırdan kurulur), ama KENDİ adımında: defter commit'ine katmak, ağ erişimi olan bildirim adımını turun kaydedilmesinin önüne koymayı gerektirirdi. |
| `ledgers_scalp/` | Scalp katmanının defteri. İki katman asla aynı defteri paylaşmaz: paylaşsalardı 15 dakikalık turlar 4 saatlik modellerin `last_processed_bar` değerini ileri taşır ve iki ölçüm birbirinin bakiyesini bozardı. |
| `ledgers_ema/` | `ema` katmanının defteri. Üç katman asla aynı defteri paylaşmaz: paylaşsalardı bir katmanın turu diğerinin `last_processed_bar` değerini ileri taşır ve iki ölçüm birbirinin bakiyesini bozardı (aynı gerekçe `ledgers_scalp/`). Bugün BOŞTUR — katmanın tetikleyicisi yok. |
| `.github/workflows/run-scalp.yml` | Scalp katmanının periyodik turu (`main.py --layer scalp`). Dosyada cron YOKTUR, yalnızca `workflow_dispatch`; gözlenen kadans ~15 dakikadır (her koşu bir 15m barı işler) ve tetikleyici depo dışındadır. Ayrı cron, ayrı concurrency grubu, ayrı commit kapsamı (`ledgers_scalp` + `docs/data/metrics_scalp.json`). `run.yml`e dokunmaz. Defter commit'inden SONRA anlık sinyal bildirimi (`scripts/telegram_signals.py`) ve onun durum dosyasının kendi commit'i gelir; ikisi de `continue-on-error` — bildirim katmanı ölçümü düşüremez. 15 dakikalık cron ölçüldü ve tetiklemelerin ~%91'i düşüyordu; saatlik kadans `signals_per_bar` sayesinde sinyal kaybı üretmez (bkz. "Telafi edilen barlarda sinyal"). |
| `.github/workflows/run.yml` | Base katmanının periyodik turu. Cron **SAATLİKTİR**, 4 saatlik değil: tek tetikleme başına tek bar, GitHub'ın düşen cron'uyla birleşince barların %26'sını sinyalsiz bırakıyordu (karar 39). Saatlik kadansın AMACI her 4H barına dört bağımsız şans vermekti; **ölçüldü ve GitHub o kadar teslim etmiyor** — zamanlanmış olaylar bu depoya kabaca 4 saatte bir geliyor (teslim oranı %23, bar başına 0.97 tetikleme; karar 39-DOĞRULAMA). Saatlik cron yine de kayıp ORANINI yarıya indirdi ve asıl kazanımı KAYBIN CİNSİNİ değiştirmesi oldu: eski cron bar kapanışına çiviliydi ve hep aynı bar-saatini düşürüyordu (yanlılık), saatlik cron'un teslim edilen tetiklemesi serbest fazda gezinir (yansız). Ölçüm kuralına DOKUNMAZ — `signals_per_bar` kapalı kalır. Turların dörtte üçü yeni bar bulamaz; onları `advanced` kapısı süzer, çünkü `main.py` her koşuda `generated_at`i tazeler ve commit edilseler HEAD'deki `round` denetim izini (`emitted`/`survey`/`rejections`) BOŞ bir turla ezerlerdi — ayrıca `as_of` dört tur sabit kaldığı için günlük Telegram özeti dört kez giderdi. Telegram adımı defter commit'inden **sonra** gelir ve `continue-on-error` ile korunur: bildirim katmanı ölçümü düşüremez. |

### config.yaml Değerleri

| Anahtar | Değer | Anlamı |
|---|---|---|
| `risk_per_trade` | `0.01` | İşlem başına riske atılan sermaye oranı (%1). Boyutlandırma formülünün payı. |
| `leverage_cap` | `5` | İzin verilen azami kaldıraç. Hiçbir koşulda aşılmaz. |
| `max_positions` | `5` | Bir stratejinin aynı anda taşıyabileceği toplam pozisyon sayısı. |
| `max_short_positions` | `3` | Bunların en fazla kaçının short olabileceği. |
| `fee_rate` | `0.00055` | Tek yön komisyon oranı; giriş ve çıkışta ayrı ayrı uygulanır. **İşlem yapılan borsanın** (Bybit) standart kademe **taker** oranıdır — veri çekilen borsanın (OKX) değil: maliyeti ödeyen taraf hesabın tutulduğu yerdir. Modeller `entry_type="market"` ile girip çıktığı için iki bacak da taker. |
| `slippage_base` | `0.0005` | Yönden bağımsız olarak **her** dolumda (long/short giriş, çıkış, TP) uygulanan temel kayma. |
| `slippage_short_stop` | `0.0015` | Short pozisyonların stop dolumunda `slippage_base` yerine geçen kayma (short stop'lar yukarı boşluklarda daha kötü dolar). |
| `max_stop_atr_multiple` | `3.0` | Stop mesafesi tavanı: stop mesafesi ATR'nin 3 katını aşan sinyal açılmaz, işlem atlanır (kural 14). |
| `maintenance_margin` | `0.005` | Likidasyon eşiği. Pozisyonun mum içi zararı bu seviyeyi geçerse likide edilir. |
| `random_seed` | (sabit tam sayı) | Rastgelelik kullanan her yol bu tohumdan beslenir; koşular tekrarlanabilir olmalıdır. |
| `initial_capital` | `10000` | Her stratejinin izole sanal hesabının başlangıç bakiyesi (USDT). |
| `signals_per_bar` | `false` (scalp: `true`) | Telafi edilen barlarda da sinyal üretilsin mi. Kapalıyken sinyal yalnızca `as_of` barında üretilir; açıkken her bar kendi sinyalini üretir ve tur, o barların her birinde ayrı ayrı koşulmuş gibi sonuçlanır. Bkz. "Telafi edilen barlarda sinyal". |
| `timeframe` | `"4H"` | Tek zaman dilimi; OKX bar kodu ve bar süresi bundan türetilir. |
| `universe_size` | `50` | 24s hacme göre seçilen USDT perpetual sayısı. |
| `universe_refresh_days` | `30` | Evren bu süre dolmadan yeniden hesaplanmaz (kıyas kümesi sabit kalsın). |
| `trailing.atr_period` | `14` | Projenin **tek ATR PERİYODU**: hem trailing stop (kural 9) hem stop mesafesi bandı (kural 14) bu periyodu kullanır ve hiçbir model kendi periyodunu seçemez. YUMUŞATMA ayrı bir eksendir: varsayılan `simple`dır ve kuralları dış bir sistemden gelen bir model (yalnızca `ema_trend`) `wilder` BİLDİREBİLİR — o zaman "×ATR" ifadesi modeller arası kıyaslanabilir olmaktan çıkar ve kıyas `avg_stop_distance_pct`e taşınır. İkisi ayrışırsa "3×ATR" iki farklı mesafe demeye başlar. Uygulama `core/engine.py`'dedir; periyot ortak olmalı ki aynı `trailing_atr` değeri her modelde aynı stop mesafesi anlamına gelsin. |
| `acceptance.min_trades` | `30` | **Kapı** — örneklem: R'ye giren kapanmış işlem bu sayının altındaysa ortalama R bir ölçüm değil gürültüdür. |
| `acceptance.control_model` | `"random_ctrl"` | **Kapı** — edge'in kontrol referansı. `is_benchmark` değildir (aynı sütunda yarışır), bu yüzden adı `benchmarks` listesinden türetilemez. |
| `acceptance.control_min_trades` | `30` | **Kapı** — kontrolün KENDİ örneklemi. Marj kontrolün ortalamasına göre ölçülür; kontrol bu sayıya ulaşmadıysa edge değerlendirilemez. |
| `acceptance.edge_margin_r` | `0.15` | **Kapı** — edge marjı: kontrolü geçmek için ortalama R farkının en az bu kadar olması gerekir. Çekilişin kendi gürültüsü kıl payı bir farkı tek başına üretebilir. |
| `acceptance.edge_ci_alpha` | `0.05` | **Kapı** — edge'in kesinlik koşulu: (model − kontrol) farkının yüzdelik bootstrap aralığı bu alfa ile kurulur, ALT SINIRI sıfırın üstünde olmalıdır. |
| `acceptance.bootstrap_samples` | `2000` | Bootstrap yeniden örnekleme sayısı. Tohum `random_seed`den türer: aynı defter her zaman aynı aralığı verir. |
| `acceptance.stop_band_ratio` | `2.5` | **Uyarı** — band (kural 14). Defterde ATR olmadığı için band yarışmacıların `avg_stop_distance_pct` medyanına göre kurulur: `medyan/√oran .. medyan×√oran`, uçtan uca tam bu oran kadar geniş. Bandın dışında kalmak doğrulamayı ENGELLEMEZ; raporda uyarı ikonudur. |
| `funding.*` | `enabled`, `interval_hours` | Funding simülasyonunun açık/kapalı olması ve periyodu. |
| `exchange.*` | OKX erişimi | `rest_base`, `inst_type`, `quote_ccy`, `btc_reference`, istek limitleri, timeout, throttle ve retry/backoff sabitleri. |
| `data.*` | yerel depo | `cache_dir`, `universe_file`, `history_bars`, `funding_history_periods`, `max_staleness_bars` (BTC çıpasının azami bayatlığı). **`funding_history_periods` (180 ≈ 60 gün) bilinçli bir KISITTIR** (karar 50): canlı turun ihtiyacı kadar derindir ama yıllara uzanan bir pencereyi kapatmaz — backtest'te kaydı olmayan anda `core/funding.py::rate_at` None döner ve maliyet işlenmez (eski dönem İYİMSER çıkar, docs/backtest.md > 6d'nin 3. sapması), fonlama-persentili tezleri ise bu derinlikle hiç ölçülemez. Derinleştirmenin yolu tavanı büyütmek DEĞİL, ayrı bir arşiv + 60 günlük örtüşmede damga bazlı tutarlılık kanıtıdır (karar 50); kanıt geçmeden arşiv kullanılmaz. **Tavanı büyütmenin neden işe yaramayacağı ÖLÇÜLDÜ** (`scripts/probe_funding_depth.py`, 2026-09-19): OKX uç noktası ~3 aylık KAYAN bir pencere tutuyor, yani 180'i yükseltmek daha derin veri getirmez — sınır bizim değil borsanın. |
| `layers.*` | katmanlar | Her katmanın FARKI: `ledger_dir`, `metrics_file`, `universe` (sabit liste ya da `null`), `retention.equity_compaction_days`, `retention.model_trade_limit`, `breakdowns` ve kökü ezen ayarlar (`timeframe`, `models`, `signals_per_bar`, …). Bkz. "Katmanlar". |
| `scalp.*` | scalp kısıtları | Beş kollu modellerin (11, 12, 15; canlıda üç kol tetikliyor — karar 48) ve model 14'ün BİREBİR aynı okuduğu değerler: `min_stop_pct` (0.01), `min_reward_risk` (1.5), `time_stop_bars` (16; `patient.time_stop_bars` 100 — yalnızca model 16), `stop_atr_multiple` (5.0), `target_reward_risk` (2.0) ve `bandit.*` (`warmup_trades` 20, `min_allocation` 0.05, `window_trades` 100, `prior_r_sigma` 1.0). |
| `exit_management.*` | üç aşamalı çıkış | Modeller 13, 14 ve 15'in TEK kaynağı: `breakeven_at_r` (1.0), `partial_tp.r` (1.5), `partial_tp.fraction` (0.5), `trail_giveback_pct` (0.5). Model başına ayrı bloklar, bir gün birinin sessizce ayrışması ve model 15 ↔ `scalp_fixed` farkının "iki ayrı yönetimin farkı"na dönüşmesi demekti. |
| `ema_trend.*` | model 18 | `fast_period` (21), `slow_period` (55), `stop_atr_multiple` (1.5), `target_reward_risk` (2.0), `atr_smoothing` (`wilder` — YALNIZCA bu model; projenin varsayılanı `simple` ve değişmedi). Yalnızca `ema_trend` okur. ATR PERİYODU BURADA YOKTUR: projenin tek ATR tanımı `trailing.atr_period`tır ve modele özel bir periyot, aynı "1.5×ATR" ifadesinin modelden modele farklı mesafe anlamına gelmesi demekti. Dört sayı da docs/backtest.md > 6d'de ön-kayıtlıdır. |
| `vwap.*` | modeller 13-14 | Model 14'ün sinyali (`band_mult` 2.0, `min_vwap_bars` 8 — YALNIZCA model 14'ün, kopya okumaz) ve sabit çarpanları (`managed.*`); kopyanın KENDİ kuralları (`clone.*`: sabit teminat oranı, kaldıraç, limitler, kaynağın sinyal sabitleri `vwap_window` 300 / `std_window` 20 / `min_bars` 25 / `sl_mult` 0.5, 3×3 = 9 kombinasyon, epsilon, 12 sembollük evren). İki blok ayrıdır ve `band_mult`i paylaşmaz: model 13 onu ÖĞRENİR, model 14 config'ten sabit okur. |

## Değişmez Kurallar

1. Hiçbir strateji kendi defterine (ledger) doğrudan yazamaz. Strateji yalnızca `Signal`
   üretir; yazma işlemi merkezi olarak `core/ledger.py` üzerinden yapılır.
2. Hiçbir strateji komisyon, funding veya borçlanma maliyeti hesaplamaz. Bunlar
   `core/funding.py` ve `core/portfolio.py` içinde, tüm modeller için birebir aynı kurallarla
   uygulanır.
3. Hiçbir strateji pozisyon boyutu (miktar/USD tutarı) belirlemez. Boyutlandırma
   `core/portfolio.py`'de tanımlı ortak bir kurala göre yapılır (bkz. kural 11); strateji
   yalnızca yön ve giriş/çıkış seviyelerini önerir.
4. Stratejiler birbirinin verisine, pozisyonuna veya iç durumuna erişemez; her biri tam
   izolasyonla çalışır. **Tek istisna — meta-stratejiler:** `is_meta = True` ile işaretlenen
   bir model, diğer modellerin **o turdaki sinyallerini** salt okunur bir kopya olarak alır.
   Bunun için `core/engine.py` her turu iki geçişte yürütür: önce normal modeller (kendi
   aralarında izole), sonra meta modeller. Meta modeller **birbirinin** sinyallerini okuyamaz
   — meta geçişine giren sinyal kümesi yalnızca normal modellerin çıktısıdır ve her meta
   model için aynıdır. Meta modeller yine de birbirinin pozisyonuna, bakiyesine veya iç
   durumuna erişemez; istisna yalnızca "o turda üretilmiş sinyaller" ile sınırlıdır.
5. Tüm stratejiler aynı `MarketData` anlık görüntüsünü görür; hiçbiri farklı veya gecikmeli
   veri kullanamaz.
6. Karşılaştırma adil olmalıdır: başlangıç bakiyesi, komisyon oranı ve izin verilen sembol
   evreni tüm modeller için birebir aynıdır.
7. `core/` dışındaki hiçbir modül bakiye, komisyon veya pozisyon durumunu tutmaz/hesaplamaz.
8. `allowed_directions` ihlali veya geçersiz sinyal geometrisi (stop yanlış tarafta,
   `stop_price == entry_price`, take-profit yönü tutarsız, fraction toplamı > 1.0, evren
   dışı sembol) bir piyasa durumu değil, programlama hatasıdır — `core/validate.py`
   `ValueError` ile reddeder; sessizce filtrelenmez. Bir modelin sinyali bu kapıdan
   geçemezse yalnızca o model atlanır, koşu durmaz.
9. Trailing stop mantığı stratejide değil `core/engine.py`'de uygulanır. Strateji yalnızca
   `Signal.trailing_atr` ile isteğini bildirir.
10. Mevcut pozisyonlarda kapanış/kısmi çıkış kararı yalnızca `Strategy.manage_positions`
    üzerinden verilir; `generate_signals` yalnızca yeni pozisyon açılışı önerir. İkisi
    karıştırılmaz.
11. **Boyutlandırma kuralı** (tek yetkili uygulayıcı `core/portfolio.py`):

    ```
    boyut = (risk_per_trade × sermaye) / |giriş fiyatı − stop fiyatı|
    ```

    Kaldıraç bir hedef değil, bir sonuçtur: yalnızca gereken notional eldeki nakdi aşarsa
    devreye girer ve **hiçbir koşulda `leverage_cap`'i aşmaz.** Gereken kaldıraç
    `leverage_cap`'i aşacaksa pozisyon `leverage_cap`'e sığacak şekilde **küçültülür** —
    işlem atlanmaz. (Atlamak, geniş stop kullanan modellerin işlem sayısını sessizce
    düşürür ve tam da ölçmeye çalıştığımız karşılaştırmayı bozar.)
12. **Look-ahead yasağı:** `MarketData` yalnızca **kapanmış** barları içerir. Borsa API'si
    kapanmamış (oluşmakta olan) bir bar dönerse `core/data.py` bu barı atar; `as_of`
    BTC referans sembolünün son kapanmış barının zamanıdır ve stratejiler "şimdi"yi buradan
    okur.
    Bu kuralın ihlali tek bir modeli değil, **tüm sonuçları geçersiz kılar.**
13. **Dolum kuralı:** Sinyaller üretildikleri barda değil, **bir sonraki barın açılışından**
    dolar. Bir barın aralığında hem stop hem take-profit varsa, mum içi sıralama
    bilinemeyeceği için **kötü olan (stop) gerçekleşmiş varsayılır.** Aynı barda likidasyon
    seviyesi de dokunulmuşsa likidasyon her ikisinden de önce gelir (bkz. `core/portfolio.py`).
    - **Bu varsayımın ne sıklıkta BAĞLADIĞI sayılır** (`ModelReport.stop_exits` /
      `ambiguous_stop_exits`; ölçüt `core/portfolio.py::_favourable_level_in_range`).
      Varsayım muhafazakârdır ve doğru taraftadır — iyimser olan, elde olmayan bir
      bilgiyle kâr yazmak olurdu — ama bedeli hiç ölçülmemişti: "modeller kaybediyor"
      sonucunun ne kadarı sinyalden, ne kadarı bu varsayımdan geliyor bilinmiyordu.
      Kısmi çıkış seviyesi de lehte sayılır (aynı mumda stop'a öncelik vermek onu da
      yutar); zaten dolmuş kısmi sayılmaz.
    - Sayım `rejections`/`survey`/`emitted` ile **aynı statüde bir denetim izidir:**
      hiçbir dolumu, fiyatı ya da sırayı değiştirmez. Canlı tur raporu yalnızca o turun
      stop'larını sayar; kümülatif cevap backtest'ten okunur (pencerenin tamamı tek turda
      işlenir — `scripts/backtest.py::format_fill_ambiguity`).
    - **Varsayımın kendisini oynatmak ayrı bir karardır ve canlı deftere ASLA girmez:**
      defterin kuralı tek olmalıdır (aynı gerekçe `signals_per_bar`in base'de kapalı
      tutulmasıdır — biriken geçmişin bir kısmı bir kuralla, kalanı başka bir kuralla
      üretilemez). Duyarlılık ancak harness'ta koşulur.

13b. **Üç aşamalı çıkış yönetimi motorun yeteneğidir, stratejinin değil** (kural 9'un aynı
sınırı). `Signal.breakeven_at_r`, `Signal.partial_tp` ve `Signal.trail_giveback_pct`
OPSİYONELDİR, varsayılanları `None`dır ve doldurmayan model bu yetenekten hiçbir biçimde
etkilenmez. Strateji yalnızca isteğini bildirir; stop hareketlerini `core/engine.py`,
kısmi dolumu `core/portfolio.py` uygular.
- **Mum içi sıra likidasyon -> stop -> kısmi -> TP'dir.** Kısmi çıkışın stop'tan sonra
  gelmesi kural 13'ün aynı kuralıdır: aynı mumda ikisi de aralığa giriyorsa kötü olan
  gerçekleşmiş varsayılır.
- **Kısmi çıkışın çektiği stop o mumda BİR DAHA KONTROL EDİLMEZ.** O stop, kısmi dolum
  gerçekleştikten sonra verilmiş yeni bir emirdir ve mumun daha önceki hareketleri
  sırasında piyasada durduğu varsayılamaz. Tersini yapmak kısmi çıkışı her mumda anında
  tam çıkışa çevirir ve mekanizmayı ölçülemez kılardı.
- **Stop hareketleri bar KAPANDIKTAN sonra uygulanır** (kural 12; ATR trailing ile aynı
  adım) ve yalnızca SIKIŞTIRIR, bu yüzden aralarındaki sıra sonucu değiştirmez.
- **`trailing_atr` ile `trail_giveback_pct` aynı anda kullanılamaz** (`ValueError`): ikisi
  de stop'u sıkıştıran ayrı mekanizmalardır ve birlikte çalıştıklarında çıkışı hangi
  kuralın ürettiği defterden okunamaz — oysa bu modeller tam olarak çıkış kuralını ölçüyor.
- **Takip eden stop orijinal hedefi ASLA aşmaz:** aşsaydı hedef hiç dolmaz, her işlem
  stop'la kapanır ve `exit_reason` kolonunun anlamı kaybolurdu.

13c. **Çıkışın ALT sebebi deftere yazılır (`exit_rule`).** `exit_reason` beş kaba koddur ve
ikisi birleşiktir: `stop` hem İLK stop'u hem takip eden stop'u (ATR trailing, breakeven,
geri verme, kısmi çıkışın çektiği stop), `signal` hem zaman stop'unu hem başka bir strateji
çıkışını anlatır. Oysa modeller 13/14/15'in ölçtüğü şey tam olarak yönetimin katkısıdır:
iki çıkış aynı satıra çökerse o katkı defterden okunamaz. Ayrım **sonradan geri
hesaplanamaz** — deftere yalnızca İLK stop yazılır (`stop_price`, R'nin paydası) ve stop'un
sonradan nereye çekildiği yalnızca kapanış anında bilinir.
- Stop'u en son hangi kuralın taşıdığı, hareket ANINDA pozisyonda saklanır
  (`OpenPosition.stop_rule`; yazan `core/portfolio.py::_tighten_stop`, kuralı veren
  `core/engine.py::_update_stops`) ve `positions.json`'a düşer — hareket bir turda,
  çıkış başka bir turda olabilir.
- Kural yalnızca stop GERÇEKTEN hareket ettiğinde yazılır: reddedilen bir hareketin
  (gevşeme yönü) kuralını saklamak, işlemi hiç uygulanmamış bir yönetimle etiketlerdi.
- Strateji çıkışlarında kural TALİMATIN kendi etiketinden gelir
  (`ExitInstruction.reason` içindeki `exit_rule=time_stop`).
- Etiket `notes` kolonuna `core/tags.py` formatıyla düşer; **yeni kolon açılmaz** —
  `trades.csv` başlığı değişirse eski satırlar okunamaz hâle gelir
  (`core/ledger.py::_assert_header`).
- **"İlk stop aldı" için etiket YOKTUR:** etiketin yokluğu o bilgiyi taşır. Uydurma bir
  `exit_rule=initial`, hiç hareket etmemiş bir stop'u bir yönetim kararıymış gibi
  gösterirdi.

14. **Stop mesafesi bandı (maliyet karşılaştırılabilirliği):** Stop mesafesi yalnızca bir risk
    tercihi değil, aynı zamanda **maliyet ölçeğidir.** Boyut `risk / |giriş − stop|` olduğu için
    dar stop kuran model aynı 1R'yi daha büyük notional ile taşır ve R başına daha çok
    komisyon+kayma öder. Bu yüzden modellerin stop mesafeleri kabaca aynı bantta — **1×–2.5×
    ATR** — tutulur: 2×ATR ile 2.5×ATR kıyaslanabilir, 0.5×ATR ile 3×ATR kıyaslanamaz, çünkü
    maliyet farkı sinyal farkını gölgeler. Stop'unu veriye bağlı kuran modeller (ör. "fitil
    tepesinin üstü veya 1×ATR, hangisi genişse") bandın dışına taşabilir; onlar için
    `max_stop_atr_multiple` bir **tavandır**: mesafe tavanı aşıyorsa **işlem atlanır.**
    - Stop tavana **çekilmez** — bu, modelin "stop fitilin üstünde olmalı" tezini sessizce başka
      bir modele çevirirdi.
    - `core/validate.py` burada hata fırlatmaz: geniş stop bir programlama hatası değil,
      karşılaştırılamayacak bir piyasa durumudur (kural 8 ile karışmaz).
    - Atlama **sessiz olamaz:** model atladığı her sinyali gerekçesiyle `logger.info` ile yazar.
      Kural 11'in "atlamak işlem sayısını sessizce düşürür" itirazı burada denetlenebilir kayıtla
      karşılanır; bandın gerçekten tuttuğu ise `avg_stop_distance_pct` kolonundan okunur.

15. **Referans (benchmark) modeller ve boyutlandırma muafiyeti:** `is_benchmark = True` ile
    işaretlenen bir model **yarışmacı değil, zemindir.** Cevapladığı soru tek: *"model piyasayı
    yendi mi, yoksa yalnızca yükselen bir piyasada mı durdu?"* On modelin hepsi pozitif getiri
    üretse bile hiçbiri bu satırı geçemiyorsa ölçümün sonucu "stratejiler işe yarıyor" değildir.

    Böyle bir model `Signal.sizing = "notional_fraction"` kullanabilir ve boyutu şöyle belirlenir
    (uygulayıcı yine tek yetkili yer, `core/portfolio.py`):

    ```
    boyut = (notional_fraction × sermaye) / giriş fiyatı      # kaldıraç ZORLA 1x
    ```

    - **Neden muafiyet gerekiyor:** alım-tut'un tanımı stop'suz olmasıdır. Ona yapay bir stop
      takmak onu bir trend modeline çevirirdi; kural 11'in formülüne (`risk / |giriş − stop|`)
      sokmak ise imkânsızdır — paydası yoktur. Muafiyet olmadan çıpa ya var olamaz ya da
      ölçtüğü şey artık alım-tut olmaz.
    - **Muafiyet YALNIZCA boyutlandırmadadır.** Komisyon, kayma, funding, likidasyon, evren,
      `allowed_directions`, dolum kuralı (kural 13) ve defter kuralları (kural 1/2/7) referans
      modele de birebir aynı uygulanır. Kaldıraç 1x'e **sabitlenir**: `leverage_cap` bir tavandır,
      çıpa için ise kaldıraç hiç devreye girmez — çıpanın işi "piyasa ne yaptı"yı ölçmek, onu
      kaldıraçla büyütmek değil.
    - **Kapı `core/validate.py`'dedir:** `sizing = "notional_fraction"` gelen bir sinyal
      `is_benchmark` VE `is_replica` olmayan bir modelden geliyorsa `ValueError`. Bu kapı
      olmadan kural 3/11 delinir — her model kendi boyutunu "referans gibi" belirlemeye başlar,
      ortak risk birimi (1R) ortadan kalkar ve modeller artık aynı ölçekte yarışmaz.
    - **İki alan birbirini dışlar, sessiz düzeltme yoktur:** `sizing = "risk"` iken `stop_price`
      zorunludur ve `notional_fraction` dolu olamaz; `sizing = "notional_fraction"` iken
      `stop_price` **None olmalıdır** (dolu gelirse hata — yok saymak, deftere yazılan "ilk
      stop"u hiç kullanılmayan bir sayı yapardı, oysa `risk_amount` ve `cost_per_r` ondan türer).
    - **Raporlama:** referans satırı `avg_stop_distance_pct` ve `cost_per_r` kolonlarında `nan`
      alır — stop'u olmayanın 1R'si, dolayısıyla R başına maliyeti de yoktur. Tabloda ortalama R
      sıralamasına girmez; ayrı bir **REFERANS** bölümünde durur. Aynı sütunda sıralamak, farklı
      boyutlandırma kuralıyla çalışan bir satırı risk-birimi yarışının parçasıymış gibi
      gösterirdi. Referansın taşıdığı bilgi sıralamada değil, **hesap düzeyi getirisindedir.**
    - Kural 14'ün stop bandı referans modele uygulanmaz: band bir maliyet ölçeği kuralıdır ve
      stop mesafesi üzerinden tanımlıdır; stop'suz sinyalde uygulanacak bir şey yoktur.
    - **Çıpanın "bir kez al, sonra tut" davranışı sessiz olamaz.** Model defterini göremediği
      için (kural 4/7) her turda aynı sinyalleri üretir; tekrarı reddeden tek yetkili yer
      `core/portfolio.py`'dir. Bu, her turda `signals=2 / filled=0` görünümü demektir — ki
      **gerçek bir boyutlandırma arızası da tam olarak böyle görünür.** İkisi ayırt edilebilsin
      diye her ret bir **sebep koduyla** kaydedilir (`RejectReason`; `duplicate_position`,
      `zero_size`, `insufficient_cash`, ...) ve tur raporunun `rejections` alanında
      (`docs/data/metrics.json` dâhil) sayılarak durur. Log seviyesi de ayrımı taşır: beklenen
      tekrar `INFO`, boyutlandırma arızası `WARNING`. Kodsuz bir "hiç dolmadı" turu yalnızca
      kural 13'ün kuyruğudur (emir bir sonraki barda dolacak) ve ayrıca öyle loglanır.

15b. **Kopya (replica) modeller: `is_replica = True`.** Bir kopya, dış bir sistemin
kurallarını bizim maliyet, kayma, funding ve likidasyon varsayımlarımız altında yeniden
üretir. Çıpa gibi yarışmacı değildir ve aynı sonuçları doğurur — ortalama R sıralamasına
girmez, kabul kapılarına tabi değildir, stop bandı medyanına katılmaz ve maliyet ölçeği
kolonlarında `nan` alır — ama AYRI bir bayraktır, çünkü:
- **Ölçtüğü soru farklıdır.** Çıpa "piyasa ne yaptı", kopya "dış sistem bizim
  varsayımlarımızla ne yapardı" der. Kabul çıtasının zemini (kural 15) yalnızca
  `is_benchmark` satırlarından gelir; kopyayı zemin saymak, çıtayı bir stratejinin
  performansına bağlamak olurdu. Tabloda da ayrı bir **REFERANS (dış sistem)** bölümünde
  durur.
- **Stop kuralı TERSTİR.** Çıpanın `stop_price`ı None OLMALIDIR (stop'suzluk tanımının
  kendisidir); kopyanın `stop_price`ı ZORUNLUDUR, çünkü stop yönetimi kopyalanan
  sistemin parçasıdır ve tam olarak ölçülmek istenen şeydir.
- **`nan` kolonları burada "hesaplanamaz" değil "kıyaslanamaz" demektir.** Kopya 1R'yi
  sabit teminattan, yarışmacılar sermayenin %1'inden türetir; aynı sütuna koymak farklı
  paydaya sahip iki oranı karşılaştırılabilirmiş gibi sunardı. R'nin kendisi ölçülmeye
  devam eder (kopyanın kendi öğrenmesi ona dayanır).
- **`ModelLimits` yalnızca kopya modellere açıktır** (kapı: `core/validate.py::validate_model`,
  çağıran `strategies/registry.py`). Kaynak sistemin kaldıracı, eşzamanlı pozisyon sayısı,
  yön kotası ve portföy riski tavanı onun kendi kurallarıdır; model bunları kendi içinde
  uygulayamaz (kural 4/16: açık pozisyonlarını göremez; kural 3: boyut hesabı
  `core/portfolio.py`nindir), bu yüzden bir BİLDİRİMDİR ve uygulayan yine tek yetkili
  yerdir. Yarışmacılara kapalı olması kural 6'nın kendisidir. Limitler kök kotayı yalnızca
  DARALTIR; kaldıraç tavanı `REPLICA_LEVERAGE_CAP` (10x).
- **Muafiyet YALNIZCA boyutlandırmadadır** (kural 15 ile birebir aynı): komisyon, kayma,
  funding, **likidasyon**, evren, `allowed_directions` ve dolum kuralı kopyaya da aynen
  uygulanır. Likidasyonu kapatmak, 10x kullanmanın bedelini silip kopyayı haksız biçimde
  iyi göstermek olurdu (bkz. docs/decisions.md > "Kopya modelde likidasyon kapatılmaz").

16. **Model kendi KAPANMIŞ işlemlerini okuyabilir, açık pozisyonlarını okuyamaz.** Uyarlanabilir
    modeller (bkz. `strategies/scalp_bandit.py`) geçmiş sonuçlarından öğrenir; bunun tek meşru
    kaynağı defterin kapanmış satırlarıdır. Motor, `Strategy.observe_closed_trades` kancasını
    **uygulayan** modellere her turda kendi kapanmış işlemlerinin salt okunur bir görünümünü
    (`ClosedTrade`) verir; uygulamayan model için defter hiç okunmaz ve davranışı değişmez.
    - **Açık pozisyon bu listeye hiçbir yoldan giremez.** Girseydi model, henüz gerçekleşmemiş
      bir sonucu öğrenir ve kâğıt üstündeki kârı ölçüme sokardı — üstelik pozisyon kapandığında
      aynı sonucu ikinci kez sayardı.
    - **İzolasyon korunur (kural 4):** model yalnızca KENDİ işlemlerini görür; başka modelin
      işlemine, pozisyonuna, bakiyesine ya da iç durumuna erişimi yoktur.
    - **Kural 1/7 korunur:** bu bir OKUMA yüzeyidir. Model deftere yazmaz, bakiye/pozisyon
      durumu tutmaz; gördüğü `r_multiple` `core/metrics.py`'deki tek R tanımından gelir.
    - Besleme, defterdeki satırlara **bu turda kapanan** işlemleri de ekler: 15 dakikalık bir
      modelde pozisyon aynı turda açılıp kapanabilir ve defteri beklemek, modelin en taze
      sonucu bir tur geç görmesi demekti.
    - Kanca patlarsa yalnızca o modelin turu boş geçer (kural 8): yarım öğrenilmiş bir
      posterior ile sinyal üretmek, modelin ne ölçtüğünü bilinmez kılardı.

## Katmanlar (`core/layers.py`)

Ölçüm üç katmanda yürür ve üçü de **AYNI çekirdeği** koşar: `core/engine.py`,
`core/portfolio.py`, `core/ledger.py`, `core/funding.py`, `core/metrics.py`, `core/report.py`
ve `main.py` tek kopyadır. Katman, ölçümün **koşullarını** değiştirir:

| | `base` | `scalp` | `ema` |
|---|---|---|---|
| Bar | 4H | 15m | 4H |
| Evren | hacme göre ilk 50 (30 günde bir yenilenir) | **SABİT 13 sembol**, otomatik seçim yok | **SABİT 13 sembol** (scalp'in aynısı) |
| Modeller | 3 yarışmacı + 1 referans çıpası | 4 yarışmacı (12, 13, 14, 16) + 1 dış sistem kopyası (13) | 3 yarışmacı (18 + kıyas hedefi `trend` + kontrol) + 1 çıpa |
| Defter | `ledgers/` | `ledgers_scalp/` | `ledgers_ema/` |
| Rapor | `docs/data/metrics.json` | `docs/data/metrics_scalp.json` | `docs/data/metrics_ema.json` |
| Cron | `run.yml` (SAATLİK; hedef bar başına dört şanstı, GERÇEKLEŞEN ~0.97 — karar 39-DOĞRULAMA. Bar ilerletmeyen turlar commit ve bildirim üretmez) | `run-scalp.yml` (cron yok, dış tetikleyici; ~15 dk, tur başına 1 bar) | **YOK** — tetikleyicisi yok, tanım var koşu yok (bkz. aşağısı) |
| Telafi barında sinyal | yok (`signals_per_bar: false`) | var (`signals_per_bar: true`) | yok (`signals_per_bar: false`) |
| Stop tavanı (kural 14) | 3×ATR | 8×ATR | 3×ATR |
| Kırılımlar | yok | kol + sembol + çıkış kuralı + seans + kayıp serisi | sembol + çıkış kuralı + seans + kayıp serisi |
| Yarışma dışı satır | `buyhold` (`is_benchmark`) | `vwap_clone` (`is_replica`) | `buyhold` (`is_benchmark`) |

**`ema` katmanının tetikleyicisi YOKTUR ve bu bilinçlidir.** Katman bugün yalnızca
`scripts/backtest.py` tarafından okunur; hiçbir deftere yazılmaz. Canlıya alınması
(`run-ema.yml`), ön-kayıtlı kapıların geçilmesine bağlıdır (docs/backtest.md > 6d) —
"tanımlı ama koşmuyor" hâli, `scalp_vol`un `REGISTRY`de durup `models` listesinde olmaması
ile aynı statüdedir: kod ölçülmeden yarışmaz.

**`ema_trend`in ÇIKIŞ EKSENİ ölçüldü ve KAPANDI** (karar 49; docs/backtest.md > 6e >
SONUÇ). "Kenar giriş sinyalinde, çıkış geometrisi yiyor" tezi bir varyant ailesine
dönüşmeden düştü: modelin ters sinyal çıkışı zaten yoktur (`manage_positions`
uygulanmaz, çıkış evreni `{tp, stop, liquidation}`) ve dönem A'nın yol ölçümünde
ön-kayıtlı seçim kuralının üç dalı da tetiklemedi — kaybedenler kazananlardan daha
HIZLI ölüyor (medyan 6 ↔ 9 bar), kaybedenlerin yarısı hiç kâra geçmiyor (medyan MFE
+0.55R) ve hedef sonrası işaretli hareket sıfır etrafında salınıyor. Ekseni yeniden
açmanın yolu yeni bir ön-kayıttır; açık kalan tek kaldıraç çıkış değil **stop mesafesi**
eksenidir (docs/backtest.md > 6e > KAYIT) ve o da kendi ön-kaydıyla gelir.

**Neden `ema` ayrı bir katman.** Modelin backtest'i sabit 13 coinde koşuyor; `base` evreni
hacme göre seçilir ve 30 günde bir kayar. Modeli `base`e almak, backtest'in ölçtüğünden
BAŞKA bir evrende koşturmak, yani forward test ile backtest'i baştan ayrıştırmak olurdu.
Alternatif ("base evrenini 13 coine sabitlemek") reddedildi: `trend`, `meanrev`,
`random_ctrl` ve `buyhold` o günden itibaren başka bir evren görür ve mevcut defterlerinin
geçmişi yeni dönemle kıyaslanamaz hâle gelirdi (karar 25'te `fee_rate`in defteri tarihli
olarak bölmesiyle aynı hata). Bkz. docs/decisions.md > 45.

**Neden ayrı bir `scalp_config.yaml` değil.** `risk_per_trade`, `fee_rate`, `slippage_*`,
`leverage_cap`, `initial_capital`, `maintenance_margin` iki katmanda da BİREBİR aynıdır
(kural 6). İki dosyaya bölmek, bir gün birinin sessizce ayrışması demekti — ve iki katman
farklı maliyet varsayımlarıyla koştuğunda bunu hiçbir test yakalamazdı. Kök tek kaynaktır;
katman bloğu yalnızca **farkı** yazar (derin birleştirme: `data.history_bars` yazmak `data`nın
geri kalanını silmez).

**Neden ayrı bir giriş noktası değil.** Çekirdek zaten tamamen config sürümlüdür; ayrı bir
`main_scalp.py` yalnızca orkestrasyonu (model kurulumu, hata izolasyonu, dry-run kopyası, yük
yazımı) ikinci kez yazmak olurdu. Ayrışan şey ölçümün koşulları, akışı değil.

**Katmanlar arası kıyas YAPILMAZ.** Dashboard scalp modellerini ayrı bir bölümde, ayrı bir
tabloda gösterir. 15 dakikalık bir modelin ortalama R'si ile 4 saatlik bir modelinki aynı
sütuna konsaydı, aradaki fark strateji farkı gibi okunurdu — oysa işlem sıklığı, maliyetin R
içindeki payı ve tutma süresi bambaşkadır. Katman **içi** kıyas (model 11 ↔ model 12) ise
tam olarak tasarımın amacıdır.

**Telafi edilen barlarda sinyal (`signals_per_bar`).** Motor atlanan turları zaten telafi
ederdi — son işlenmiş bardan `as_of`'a kadarki her barı sırayla ilerletir — ama sinyal
YALNIZCA `as_of` barında üretilirdi: atlanan barların pozisyon yönetimi (stop/TP/likidasyon/
funding) yapılır, sinyal fırsatı ise kaybolurdu. 15 dakikalık katmanda bu kayıp ölçülebilir
bir sorundur: GitHub cron'unun 15 dakikalık tetiklemelerinin ~%91'i düşüyordu, yani tablo
modelin değil **cron'un kadansını** ölçüyordu. Üstelik kayıp turdan tura değiştiği için
modeller arası farkın kendisi de gürültüye karışırdı.

Ayar açıkken tur, o barların her birinde **ayrı ayrı koşulmuş gibi** sonuçlanır ve bunun
testi bir eşdeğerliktir (`tests/test_engine_per_bar.py`): bir turda telafi edilen N bar ile
N ayrı turda koşulan N bar **birebir aynı defteri** üretir. Bunu sağlayan dört kural:

- **Barlar sırayla işlenir, toplu değerlendirme YOKTUR:** her bar için sinyal üretilir,
  emir bir SONRAKİ barın açılışından dolar (kural 13), sonra o barın stop/TP/likidasyon
  kontrolü yapılır. Barları birleştirip tek bir değerlendirme yapmak, ara barlarda
  tetiklenecek çıkışları yok saymak olurdu.
- **Pozisyon limitleri her barda yeniden sorulur** (`core/portfolio.py`): kota barın kendi
  doluluğuna bakar, turun toplamına değil.
- **Model her barda yalnızca o bara kadarki veriyi görür** (kural 12): `core/engine.py`
  çerçeveleri o barda keser ve o barı taşımayan sembolü o barın evreninden düşürür —
  `core/data.py`'nin `as_of` için uyguladığı kuralın aynısı. Aksi, telafi barında geleceği
  görerek sinyal üretmek olurdu.
- **`as_of`'tan sonrası işlenmez:** kapanmamış bar ne sinyal ne özsermaye satırı üretir.

Ayar **kökte kapalıdır** (4 saatlik katman), scalp katmanında açıktır. Gerekçe kural 6'nın
kendisi değil, defterin tek bir kuralla yazılmasıdır: devreye girdiği turlarda defterin
kuralı sessizce değişir ve biriken geçmişin bir kısmı "tur başına tek sinyal", bir kısmı
"bar başına tek sinyal" ile üretilmiş olurdu. İki dönemin işlem sıklığı kıyaslanamazdı.

⚠ **Bu ayarın base'deki dayanağı ÖLÇÜLDÜ, çürüdü ve ONARILDI.** Burada bir zamanlar
"`run.yml` güvenilir tetikleniyor, yani base katmanında telafi nadiren devreye girer"
yazıyordu. Ölçüm (karar 39): turların %35'i telafi yapıyordu ve barların **%26'sı
sinyalsiz** geçiyordu — üstelik kaybolan bar her zaman 00:00 ya da 08:00 barıydı, yani
kayıp gürültü değil YANLILIK. Sebep GitHub cron'unun düşmesi/gecikmesiydi (2.6 saate
varan). **Onarım ayarda değil TETİKLEYİCİDE yapıldı:** `run.yml` saatlik cron'a geçti. Hedef
bar başına dört bağımsız şanstı; ÖLÇÜLDÜ ve gerçekleşen 0.97'dir (karar 39-DOĞRULAMA) —
GitHub bu depoya cron ne yazarsa yazsın ~4 saatte bir teslim ediyor. Kayıp yine de %26'dan
%14'e indi ve sistematik olmaktan çıkıp yansızlaştı. `signals_per_bar` base'de KAPALI kalır — defterin
kuralı değişmedi, yalnızca belgenin zaten varsaydığı güvenilirlik sağlandı. Alternatif
(base'de ayarı açmak) tam olarak yukarıdaki dönem ayrışmasını üretirdi; bkz.
docs/decisions.md > 39.
Katmanlar arası kıyas zaten yapılmadığı için ayarın katmana göre farklı olması bir
tutarsızlık değildir; katman **içi** kıyasta ise beş model de aynı ayarı görür.

**Saklama penceresi (`retention`).** 15 dakikalık katman günde 96 tur koşar ve her turu commit
eder: `equity.csv` yılda on binlerce satıra çıkar ve depo geçmişi ölçümle ilgisiz satırlarla
şişer. Bu yüzden scalp katmanında 30 günden eski özsermaye satırları **günlük özete** (o günün
son barı) indirilir ve JSON'a model başına son **50** işlem yazılır. İkisi de ölçümü
değiştirmez: `trades.csv` — denetim izi — hiçbir koşulda dokunulmaz, taze 30 gün bar bazında
kalır ve sıkıştırma her model için birebir aynı uygulanır.

### Scalp katmanının model kuralları

Katmanın ölçüm eksenleri; her eksende yalnızca TEK bir değişken ayrışır. Bir eksen
kapandığında satır SİLİNMEZ — kapanışın kendisi bir ölçüm sonucudur:

| Eksen | Çift | Ayrışan tek şey | Kapsam | Durum |
|---|---|---|---|---|
| Sürenin katkısı | `scalp_fixed` (12) ↔ `scalp_patient` (16) | zaman stop'u sınırı (16 ↔ 100 bar) | **üç kol** | **AÇIK** (tek hareket eden eksen: −0.15 ↔ −0.01) |
| İki sistemin toplam farkı ⚠ | `vwap_clone` (13) ↔ `vwap_managed` (14) | **tek değişken DEĞİL** — bkz. aşağısı | kol yok (ayrı sinyal modülleri) | AÇIK |
| Adaptasyonun katkısı | `scalp_bandit` (11) ↔ `scalp_fixed` (12) | kol seçimi | **üç kol** | KAPALI — iki kez "fark yok" (karar 33); 11 emekli |
| Çıkış yönetiminin katkısı | `scalp_fixed` (12) ↔ `scalp_managed` (15) | üç aşamalı çıkış | **üç kol** | KAPALI — iki kez "fark yok" (karar 33); 15 emekli |
| Volatilite rejiminin katkısı | `scalp_patient` (16) ↔ `scalp_vol` (17) | kesitsel ATR% medyan kapısı | **üç kol** | KAPALI — ön-kayıtlı P1 düştü (karar 36); 17 canlıda koşmaz |

⚠ **KAPSAM kolonu: hiçbir eksen beş kolda ölçülmedi.** `momentum_burst` ve
`funding_spike_fade` katmanın tüm ömrü boyunca tek sinyal üretmedi (karar 48), yani
yukarıdaki her satır ÜÇ kolun ölçümüdür — pratikte ikisinin: `rsi2_reversal` 123
pozisyonun 101'ini, `opening_range_breakout` 21'ini, `vwap_pullback` 1'ini taşıyor.
Eksen SONUÇLARI bundan geçersiz olmaz: çiftin iki tarafı da aynı üç kolu gördü, kural 6
bozulmadı ve fark hâlâ ayrışan tek değişkenin ölçüsüdür. Geçersiz olan, sonucun beş kol
HAKKINDA okunmasıdır. Kapsamı daraltmak karar 33'ün teşhisini de güçlendirir: bandit'in
tahsis edecek bir şeyinin olmaması, beş kolluk bir uzayda değil pratikte iki kolluk bir
uzayda gözlendi.

**Emekli model = katmanın `models` listesinde yok; kodu ve defteri DURUYOR** (kural 1).
Kapalı bir eksenin çifti backtest'te `--models` ile yeniden çağrılabilir.

**13 ↔ 14 bir EKSEN DEĞİL, bir toplam farktır.** Bu satır bir zamanlar "ev kurallarının
katkısı" olarak yazılıydı ve o zaman doğruydu: iki model aynı sinyal modülünü okuyordu.
Karar 23'ten sonra model 13 kendi kurallarına (`strategies/vwap/clone_signal.py`) geçti ve
fark altı eksende birden ayrıştı:

| # | Ayrışan | model 13 | model 14 |
|---|---|---|---|
| 1 | sinyal kuralları | kayan kümülatif VWAP, ağırlıksız σ (`rolling(20)`), `close > prev_close` | gün-çapalı VWAP, hacim ağırlıklı σ, bant dışı + dönüş + aynı taraf |
| 2 | boyutlandırma | sabit teminat × 10x | risk %1, `leverage_cap` 5 |
| 3 | ev kapıları | yok | %1 stop tabanı + 1.5R + zaman stop'u |
| 4 | seçim politikası | evren listesi sırası | en güçlü tek aday |
| 5 | bar başına sinyal | kotaya kadar 5 | 1 |
| 6 | evren | kaynağın 12 sembolü | katmanın tamamı |

Altı değişkenli bir fark "ev kurallarının katkısı" olarak OKUNAMAZ; okunabilen tek şey
"iki sistemin toplam farkı"dır ve tabloda da öyle durur. Tek değişkenli bir eksen
isteniyorsa yolu yeni bir model açmaktır — mevcut ikisinden birini diğerine yaklaştırmak
değil (bu, model 13'ü kopya olmaktan çıkarırdı, kural 15b).

**Çekiliş, ölçülmeyen eksende PAYLAŞILIR, ölçülen eksende BAĞIMSIZDIR.** `ScalpModel`in
`rng_identity` alanı bunu taşır. Model 11 ↔ 12'de ölçülen şey seçimin kendisidir; çekilişi
paylaşsalardı fark adaptasyonun değil tesadüfün ölçüsü olurdu. Model 12 ↔ 15'te ise ölçülen
şey seçim DEĞİL, aynı seçimin nasıl yönetildiğidir — bu yüzden model 15 `scalp_fixed`in
çekiliş kimliğini kullanır ve ikisi her turda aynı kolu, aynı sembolü seçer (eşleştirilmiş
deney). Bu, defterlerinin birebir aynı olacağı anlamına gelmez: yönetim bazı pozisyonları
erken kapatır ve `max_positions` doluluğu zamanla ayrışır — ayrışan şey DOLUMLARDIR,
sinyaller değil, ve bu ayrışmanın kendisi yönetimin bir sonucudur.

Beş kollu üç model (11, 12, 15) **beş ortak kolu** (`strategies/scalp/arms.py`) aynı
kapılardan geçirir — ama kapılardan geçen ÜÇ koldur: `momentum_burst` ve
`funding_spike_fade` katmanın tüm ömrü boyunca hiç tetiklemedi (karar 48), yani bu
modellerin ve onlara dayanan eksenlerin gerçek kapsamı beş değil üç koldur. VWAP modelleri (13, 14) ise **ayrı sinyal modülü** okur — model 14
`strategies/vwap/signal.py` (ev kuralları), model 13 `strategies/vwap/clone_signal.py`
(kaynak sistemin kuralları) — ve kol kırılımında ayrı etiketle durur (`arm=vwap_revert`
↔ `arm=vwap_revert_src`). Modüller bilerek ayrıdır: ortak bir modülde her kural bir
dallanma olurdu ve "ev kurallarının katkısı" ekseninin tanımı, o dallanmanın hangi dalının
hangi model için açık olduğuna bağlı bir şeye dönüşürdü (bkz. docs/decisions.md > 23).
Aynı gerekçe etiketler için de geçerli: tek bir ad, iki farklı kural kümesini aynı kolmuş
gibi gösterirdi.

- **Stop tabanı %1** — tur maliyeti ~%0.25'tir; daha dar stop'ta maliyet 0.25R'yi aşar ve
  model daha başlamadan geride başlar. Stop **genişletilmez**, işlem **atlanır** (kural 14'ün
  aynı gerekçesi) ve her atlama `logger.info` ile yazılır.
- **Hedef/stop ≥ 1.5** — sağlamayan kurulum atlanır. Hedef, projeksiyon
  (`target_reward_risk × stop`) ile kolun yapısal engelinin YAKIN olanıdır: yalnızca
  projeksiyon kullanmak kapıyı ölü koda çevirirdi, yalnızca yapısal seviye kullanmak ise
  15 dakikalık barda hemen hiçbir kurulumun 1.5R'ye ulaşmaması demekti.
- **Zaman stop'u 16 bar (4 saat)** — `manage_positions` üzerinden istenir (kural 10), dolum
  bir sonraki barın açılışındadır (kural 13), yani gerçek ömür 17 bardır.
- **Trailing yok** — yürüyen stop, gerçekleşen R ile kurulumun vaat ettiği R arasındaki bağı
  koparır ve bandit'in öğrendiği sinyali bulanıklaştırırdı.
- **BARDA tek sinyal** — beş kolu birden oynamak kol tahsisini anlamsız kılardı (pratikte çekiliş üç kol üzerinde yapılıyor; `generate_signals` yalnızca o barda kurulum ÜRETMİŞ kollar arasından çeker, bkz. karar 48). Birim
  `bar`dır, `tur` değil: `signals_per_bar` açık olduğu için bir tur kaç bar telafi ediyorsa
  o kadar sinyal üretilir (saatlik cron'da 4). Model 14 de barda tek sinyal oynar (kıyas
  hedefi `scalp_fixed` bar başına tek pozisyon açar); model 13 ise kaynak sistemin kuralı
  gereği kendi pozisyon kotasına (5) kadar sinyal üretir.
- **Ev kapıları (stop tabanı, 1.5R, zaman stop'u) modeller 11, 12, 14 ve 15'te GEÇERLİ,
  model 13'e UYGULANMAZ.** Zaman stop'unun uygulaması `strategies/time_stop.py`de tek
  kopyadır; model 14 `ScalpModel` gövdesinden türemediği için kuralı oradan alamaz ve iki
  ayrı uygulama, `model 14 ↔ scalp_fixed` kıyasına ölçülmeyen bir dördüncü değişken
  koyardı. Kopyaya uygulanmamasının gerekçesi ise şudur: kaynak sistemde
  yoktur; eklemek kopyayı model 14'e çevirirdi ve ikisinin farkı ölçülemez hâle gelirdi.
  Kopyanın hedefi VWAP mesafesinin bir KESRİDİR (`tp_mult ≤ 1`), yani hedef/stop oranı
  dayatılmaz, kurulumdan doğar ve sık sık 1.5'in altına düşer — kapı sızsaydı o
  kurulumların tamamı elenirdi.
- **BARDA tek sinyal kuralı model 13'e de UYGULANMAZ:** kaynak sistem evrenini baştan sona
  tarar ve kendi pozisyon kotasına (5) kadar sinyal üretir; sıra evren listesinin
  sırasıdır, sapma gücü değil.
- **Üç aşamalı çıkış yönetimi** (`exit_management` bloğu) modeller 13, 14 ve 15'te aynıdır
  ve tek kopyadan (`strategies/exit_management.py`) okunur.

**Bandit durumu deftere yazılır ve tekrar üretilebilir.** Ayrı bir `bandit_state.json`
YOKTUR: posterior `ledgers_scalp/scalp_bandit/trades.csv`'nin saf bir fonksiyonudur (kol
etiketi + gerçekleşen R + kayan pencere) ve her turda sıfırdan kurulur. Ayrı bir durum
dosyası, defterle senkron kalması ayrıca test edilmesi gereken ikinci bir doğruluk kaynağı
yaratırdı. Denetim izi `reason` kuyruğundadır: `... | arm=vwap_pullback | post_r=0.31`.
Etiketi olmayan bir satır **sessizce atlanmaz**, `TagError` fırlatılır (kural 8 gereği
yalnızca o modelin turu boş geçer) — atlamak, posterior'ı defterde görünmeyen bir geçmişe
bağlardı.

## Dolum ve Pozisyon (ölçümün birimi)

`trades.csv` bir **DOLUM defteridir**, işlem defteri değil. `core/portfolio.py::_close`
her dilim için ayrı satır yazar ve bir pozisyon birden çok satır üretebilir:

- **kısmi çıkış** (`exit_reason="partial"`) — üç aşamalı çıkış yönetimi (modeller 13/14/15);
- **fraksiyonel hedef** (`exit_reason="tp"`, `TakeProfit.fraction < 1.0`) — `avwap` iki TP
  seviyesi, `downtrend_rally` yarım TP kullanır.

Ölçümün birimi ise **POZİSYONDUR.** `core/metrics.py::merge_fills` aynı pozisyonun
satırlarını tek ölçüm satırına indirger; kimlik `strategy + symbol + direction + opened_at`
(havuz birden çok modelin satırını birleştirdiği için `strategy` şart — kural 4 ölçümde de
geçerlidir). Nakit kolonları TOPLANIR, kapanış alanları pozisyonu KAPATAN son dolumdan gelir.

**Neden saymak yanlış:** aynı pozisyon iki kez ölçüme girer, `acceptance.min_trades` (30)
örneklem kapısı iki kat hızlı geçilir ve kazanma oranı yapay yükselir — kısmi çıkış tanımı
gereği kârda gerçekleşir, yani daima "kazanç" satırıdır.

**Neden kısmi satırı ATMAK da yanlış:** pozisyonun kilitlenmiş kârı ölçümden düşer ve
kalan dilimin R'si tüm pozisyonun R'si sanılır. Yönetimli model (15) yönetimsiz ikizine
(12) karşı bu kez haksızca KÖTÜ görünürdü — aynı hatanın ters yönü. Ayrıca `exit_reason`
filtresi fraksiyonel hedefleri hiç yakalamaz: `avwap` ve `downtrend_rally` "partial" kodu
taşımaz, "tp" taşır.

**Toplama ikisinden de kaçınır:** `R = Σpnl / Σrisk`, yani pozisyonun gerçek R'si.
`Σpnl = bakiye değişimi` değişmezi de korunur, çünkü nakit kolonları atılmaz toplanır
(test: `tests/test_metrics.py`).

`docs/positions.html` dolum satırlarını GÖRÜNMEYE devam ettirir ("DİLİM" etiketiyle) ama
istatistik hesaplamaz — ölçümü yükten okur. Bu yüzden **işlem sayısı tablodaki satır
sayısından az olabilir** ve sayfa bunu söyler.

## Rapor Kolonları

`core/metrics.py` her modeli **aynı tabloda**, her metriği **long / short / toplam** olarak
raporlar. Performans kolonlarının (`trades`, `win_rate`, `pnl`, `sharpe`, `max_drawdown`)
yanında iki **maliyet ölçeği** kolonu zorunludur:

| Kolon | Tanım |
|---|---|
| `avg_stop_distance_pct` | İşlem bazında `\|giriş − ilk stop\| / giriş` değerlerinin ortalaması (yüzde). Modelin hangi R ölçeğinde işlem yaptığını gösterir. |
| `cost_per_r` | İşlemin **tüm** dolumlarında ödenen komisyon + kayma toplamının (giriş, kısmi TP'ler, çıkış) `risk_amount`'a bölümü; model/yön bazında ortalaması. "Tüm dolumlar" şartını sağlayan şey `merge_fills`tir: tek bir dilimin maliyet/risk oranı pozisyonun maliyetini anlatmaz. |

`risk_amount` = `pozisyon boyutu × |giriş − ilk stop|` (yani **gerçekleşen** 1R). Formülün payı
(`risk_per_trade × sermaye`) değil: kaldıraç tavanı boyutu küçülttüyse (kural 11) gerçek risk de
küçülmüştür ve payı kullanmak o işlemlerin maliyetini olduğundan düşük gösterirdi.

Bu kolonlar opsiyonel değildir, çünkü projenin ana sorusunu doğrudan kirleten etkiyi ölçerler:
"short modeller daha iyi" sonucu **sinyalden** mi geliyor, yoksa short modellerin daha geniş stop
kullanıp R başına daha az maliyet ödemesinden mi? İki modelin `avg_stop_distance_pct` değerleri
bandın dışında ayrışıyorsa ve `cost_per_r` farkı performans farkını tek başına açıklayabiliyorsa,
kıyas **geçersiz** sayılır; sonuç yorumlanmaz, modelin stop parametresi düzeltilir.

**Okuma yardımları (üç kolon, hiçbiri KAPI değil).** Kabul çıtası bu üçüne bakmaz; işleri
tablodaki bir sayının nasıl okunacağını söylemek, bir satırı elemek değil (band uyarısıyla
aynı statü):

| Kolon | Tanım |
|---|---|
| `avg_r_ci_low` / `avg_r_ci_high` | Ortalama R'nin yüzdelik bootstrap aralığı (`core/metrics.py::bootstrap_mean_ci`). Alfa ve yeniden örnekleme sayısı `acceptance.edge_ci_alpha` / `acceptance.bootstrap_samples`tan gelir — İKİNCİ bir anahtar açılmaz, yoksa aynı tabloda iki farklı kesinlik ölçüsü dururdu. Tohum `random_seed ^ model ^ yön`: aynı defter her zaman aynı aralığı verir. Havuz satırında (long ↔ short) da hesaplanır, çünkü projenin ana sorusu tam orada okunur: "short'lar daha iyi" ancak iki aralık ayrıştığında söylenebilir. |
| `cost_pct` | `Σ(komisyon + kayma) / Σnotional` (yüzde) — karar 35'in "tur maliyeti%" değişkeninin defterden okunan hâli (`net R = (brüt sürüklenme% − maliyet%) / stop%`). `cost_per_r`den farkı PAYDADIR: o maliyeti 1R'ye böler ve kopya/çıpa satırlarında `nan`dır (1R'leri başka birimden gelir), bu ise notional'a böler — **tek ve ortak bir birim**, yani HER satırda hesaplanır. Yarışma dışı satırların friksiyonunu görebilmenin tek yolu budur. |
| `turnover_per_day` / `cost_drag_pct_per_day` | `Σnotional / başlangıç sermayesi / gün` ve `Σ(komisyon+kayma) / başlangıç sermayesi / gün` (yüzde), `FrictionStats` altında. Payda GÜNCEL bakiye değildir: ona bölmek ciroyu modelin kendi performansına bağlar (kaybeden modelin cirosu yapay yükselir). Gün sayısı bar SAYISINDAN değil özsermaye damgalarından gelir — saklama penceresi eski satırları günlük özete indirir ve bar sayısı o noktadan sonra geçen zamanı anlatmayı bırakır. |

**Neden friksiyon HIZI ayrı bir ölçü:** `cost_per_r` maliyeti işlem başına ölçer ve işlem
SIKLIĞINI görmez; iki model aynı `cost_per_r` ile koşup biri diğerinden on kat hızlı
çevirebilir. Karar 35'in özdeşliği tek bir pozisyonun içindedir, hesabın ne kadar hızlı
eridiğini söyleyen şey o özdeşliğin gün başına kaç kez uygulandığıdır. Bu bir **ÖLÇÜMDÜR,
kural DEĞİL** (seans, kayıp serisi ve yoğunlaşma ölçümleriyle aynı statü): hiçbir sinyal
ciroya göre elenmez, hiçbir boyut ona göre değişmez — işlem sıklığı tavanı karar 40'ta
açıkça reddedildi ve bu kolon o tavanın yokluğunun bedelini ölçer.

**Piyasa kontrolü (ana sorunun karıştırıcısı).** Projenin ana sorusu *"short işlemler
long işlemlerden daha mı başarılı"* ve bu soru, ölçüldüğü pencerede piyasanın hangi yöne
gittiğiyle **tanım gereği** karışır: düşen bir pencerede her short daha iyi görünür.
Tabloda bu karışımı okunur kılan iki kolon vardır:

| Kolon | Tanım |
|---|---|
| `market_tailwind_pct` | Pozisyonun tutuş penceresinde ÇIPANIN getirisi, **pozisyonun yönüne çevrilmiş** (long: +hareket, short: −hareket), yüzde. Çıpa **BTC**'dir — projenin zaten seçilmiş referansı (`exchange.btc_reference`, `as_of` çapası) ve iki katmanda da var; bir sepet (ör. 50/50) ağırlık seçimi, yani serbest bir parametre demekti. |
| `market_r` | `market_tailwind_pct / avg_stop_distance_pct`, pozisyon bazında hesaplanıp ORTALANIR (`cost_per_r` ile birebir aynı sözleşme: önce oran, sonra ortalama). Pozisyon çıpayla birebir hareket etseydi kazanacağı R'dir. |
| `market_measured` | Çıpa penceresinde fiyatlanabilen pozisyon sayısı. Çıpa serisi `data.history_bars` kadar geriye gider; daha eski pozisyon **ölçülmez** ve bu sayıyla söylenir — uydurma bir başlangıç fiyatı, "ölçemedik" ile "piyasa katkı vermedi"yi aynı hücreye yazardı. |

**`market_r` ortalama R'den ÇIKARILMAZ.** Ölçü `beta = 1` varsayımına dayanır (pozisyon
çıpayla birebir hareket eder) ve bu varsayımı birincil metriğin İÇİNE gömmek,
`docs/backtest.md > 7.4`ün yasakladığı metrik değiştirmedir — üstelik sembol bazlı bir beta
tahmini serbest bir parametre açardı (pencere, yöntem, yenileme sıklığı). İki sayı **yan
yana** durur; farkı okuyucu kurar ve varsayım görünür kalır. Sayfa da bu yüzden çıkarma
yapmaz (kural 7): yalnızca `market_r`'yi ortalama R'nin yanında gösterir.

**Bu da bir ÖLÇÜMDÜR, kural DEĞİL:** hiçbir sinyal piyasa yönüne göre elenmez, hiçbir
boyut ona göre değişmez. Veri `core/metrics.py`ye ENJEKTE edilir (`compare(reference=...)`,
canlıda `MarketData.btc["close"]`); modül borsaya hiç dokunmaz ve defterden başka bir
şey okumaz.

**Beklenti (`expectancy`) ayrı bir kolon DEĞİLDİR, bir AYRIŞMADIR.** R biriminde
`WR × ort.kazanç + (1−WR) × ort.kayıp` tam olarak ortalama R'ye eşittir (test:
`tests/test_metrics.py`), yani yeni bir sayı değil aynı sayının parçalarıdır. Değeri
"ortalama R negatif" bilgisinde değil, bunun **kazanma oranından mı yoksa ödeme oranından
mı** geldiğindedir; bu yüzden rapora ayrı bir kolon olarak değil, model bloğunun kuyruğunda
bir ayrışma satırı olarak girer.

**Kırılımlar (yalnızca katmanın istediği yerde).** `core/metrics.py::breakdown` işlemleri bir
gruplama ölçütüne göre böler ve her grup için aynı metrikleri hesaplar; hangi kırılımların
üretileceği katman ayarıdır (`layers.<ad>.breakdowns`). Scalp katmanı ikisini de ister:

⚠ **Grup ortalaması, MODEL satırıyla aynı ölçütle okunur: örneklem kapısı ve aralık.** Her
grup kendi `avg_r_ci_low/high` değerini alır (tohum grup adına bağlıdır) ve `acceptance.min_trades`
altında kalan satır dashboard'da **solgun** çizilip `Ö` işareti taşır. Satır gizlenmez — base
katmanının ölçütü zaten "n=30'a ulaşılabiliyor mu"dur (karar 33) — ama ortalaması bir ölçüm
değil gürültüdür. Gerekçe projenin kendi geçmişidir: karar 27 (saat hipotezi) ve karar 28
(kayıp serisi cooldown'u) tam olarak bir KIRILIM grubunun ortalamasına bakıp kural yazma
denemeleriydi ve ikisi de daha uzun örneklemde çürüdü. Grup ortalamasını örneklemsiz
göstermek, o hatayı arayüzün içine yerleştirmek olurdu.

- **kol** (`arm`) — hangi kolun kaç işlem yaptığı, ortalama R'si ve kazanma oranı. Grup ölçütü
  `signal_reason` kuyruğundaki `arm=` etiketidir; etiket yoksa `TagError` fırlatılır, satır
  sessizce atlanmaz (atlamak kırılım toplamı ile model toplamını ayrıştırırdı).
- **sembol** (`symbol`) — sembol başına işlem sayısı, ortalama R ve `cost_per_r`. Kayma
  varsayımı (`slippage_base`) evrendeki her sembol için tek bir sayıdır; ince kitapta işlem
  gören bir sembolde (PENGU, ETHFI) `cost_per_r` belirgin biçimde ayrışıyorsa varsayım orada
  tutmuyor demektir ve o satırın sonucu yorumlanmadan önce bu bilinmelidir.
- **çıkış kuralı** (`exit_rule`) — hangi kuralın kaç kez tetiklediği. Grup ölçütü
  `exit_reason` ile `notes` kuyruğundaki `exit_rule` etiketinin BİRLEŞİMİDİR
  (`core/metrics.py::exit_rule_of`): `stop` "ilk stop aldı", `stop:giveback` "geri verme
  takibinin çektiği stop aldı", `signal:time_stop` "zaman stop'u" demektir. İkisi birlikte
  etiketlenir çünkü ad uzayları çakışır (`partial` hem bir `exit_reason` hem bir stop
  kuralıdır). Etiketin YOKLUĞU bir bilgidir (kural 13c) ve uydurma bir `initial` değeri
  üretilmez. Gerekçe: modeller 13/14/15'in ölçtüğü şey üç aşamalı yönetimin katkısıdır ve
  bu katkı, üç aşamanın kaç kez tetiklendiği bilinmeden okunamaz — bugünkü geometride
  (`R:R ∈ [1.5, 2.0]`, kısmi çıkış tam 1.5R'da) üçüncü aşamanın hiç tetiklenmemesi
  mümkündür ve bu, ölçülmesi gereken bir olgudur.
  **Bu kırılımın birimi DİLİMDİR, pozisyon değil:** çıkış kuralı pozisyonun değil dilimin
  özelliğidir, yani kısmi çıkışlı bir pozisyon iki gruba birden düşer. Nakit toplamı
  korunur (Σpnl değişmez), işlem SAYISI korunmaz — grupların `trades` toplamı model
  tablosundan büyük olabilir. Kol ve sembol kırılımlarında bu sorun yoktur (ikisi de
  pozisyonun özelliğidir).
- **seans** (`session`) — işlemin AÇILDIĞI seans; sınırlar UTC'de SABİTTİR
  (`core/metrics.py::_SESSIONS`: 00-07 asya, 07-12 avrupa, 12-16 abd, 16-24 gece). Ölçüt
  `opened_at`tır, `closed_at` değil: sorulan şey "bu kurulum hangi piyasa koşulunda
  ALINDI". Yerel saat (ör. Europe/Berlin) kullanılmaz — yaz saati geçişi sınırları yılda
  iki kez kaydırır ve aynı defter iki farklı kırılım üretirdi; tekrarlanabilirlik
  `random_seed` ile aynı statüdedir. Birimi POZİSYONDUR (kol ve sembol gibi), yani
  grupların `trades` toplamı model tablosuyla tutarlı kalır.
  **Bu bir ÖLÇÜMDÜR, bir kural DEĞİL:** hiçbir model seansa bakmaz ve hiçbir sinyal bu
  etikete göre elenmez. Gerekçe, "sabah iyi / öğleden sonra kötü" türü bir gözlemin tek
  bir günün verisiyle test edildiğinde doğrulanır GİBİ görünmesi, daha uzun örneklemde ise
  tersine dönmesidir (bkz. docs/decisions.md > 27). Kırılım kanıt biriktirir; bir saat
  filtresi ancak kanıt örneklem kapısını geçtiğinde ve o zaman da YENİ BİR MODEL olarak
  gelir — mevcut bir modele filtre eklemek, o modelin ölçtüğü ekseni değiştirirdi.
- **kayıp serisi** (`loss_streak`) — pozisyon AÇILIRKEN geçerli olan ardışık kayıp sayısı;
  kovalar `0 / 1 / 2 / 3 / 4 / 5+`. Diğer dördünden farklı olarak ölçüt tek bir satırdan
  OKUNAMAZ: seri, satırın kendi alanlarında değil ondan önce kapanmış pozisyonların
  SIRASINDA durur. Bu yüzden bir ön hazırlık adımı vardır
  (`core/metrics.py::annotate_loss_streak`, `core/report.py::_BREAKDOWN_PREPARE`) ve
  ölçüt türetilmiş bir alandan okunur — `breakdown`ın tek satırlık `key` sözleşmesi
  bozulmaz.
  **Kesim `opened_at`tır:** sayılan şey modelin KARAR ANINDA görebildiği seridir ve model
  yalnızca kapanmış işlemleri görebilir (kural 16). Kesimi kapanışa taşımak, pozisyonun
  kendi ömrü boyunca kapananları da sayardı — yani ölçüm, modelin o an sahip olmadığı bir
  bilgiyle kurulur ve bir cooldown kuralının ölçüsü olmaktan çıkardı.
  **Kayıp `pnl < 0` demektir; tam sıfır seriyi KIRAR** — başabaş kapanan bir işlem kayıp
  değildir ve onu kayıp saymak serileri yapay uzatırdı, üstelik tam da breakeven stop
  kullanan modellerde (13/14/15), yani kıyasın bir tarafında.
  Birimi POZİSYONDUR, yani grupların `trades` toplamı model tablosuyla tutarlı kalır.
  **Bu da bir ÖLÇÜMDÜR, bir kural DEĞİL:** hiçbir model kayıp serisine bakmaz. Gerekçe
  "üst üste N kayıptan sonra bir süre bekle" fikrinin sınanmasıdır — kümelenme gerçek
  çıktı ama sayaç yanlış tetikleyici olduğu için kural yazılmadı (bkz.
  docs/decisions.md > 28).

Referans çıpaları (kural 15) ve dış sistem kopyaları (kural 15b) bu iki kolonu **`nan`** alır
ve tablonun AYRI İKİ bölümünde durur. Çıpada gerekçe hesaplanamazlıktır: stop'u olmayanın 1R'si
yoktur. Kopyada ise sayı hesaplanabilir ama **kıyaslanamaz**: 1R'si sabit teminattan türer,
yarışmacılarınki sermayenin %1'inden — aynı sütuna koymak farklı paydaya sahip iki oranı
karşılaştırılabilirmiş gibi sunardı. İkisi de aynı bölümde toplanmaz, çünkü ölçtükleri soru
farklıdır. Onların taşıdığı bilgi bu kolonlarda değil, hesap düzeyi getirisindedir.

Tanım kararları:

- **Payda her zaman ilk stop'tur, trailing ile güncellenen stop değil.** R, giriş anında üstlenilen
  risktir; trailing yalnızca kârı korur. Yürüyen stop'u kullanmak iyi giden işlemlerin paydasını
  sonradan küçültüp `cost_per_r`'yi şişirirdi — üstelik bu şişme trailing kullanan modellerde
  farklı olurdu, yani tam da kıyaslanmak istenen şeyi bozardı.
- **Veri yoksa `nan`, `0.0` değil.** Bir modelin o yönde hiç işlemi yoksa kolon `nan` olur. `0.0`
  "işlem yaptı ve tam sıfır çıktı" demektir; ikisini aynı hücreye yazmak, hiç short açmamış bir
  modeli "maliyetsiz short yapan model" gibi gösterir ve model ortalamalarını aşağı çeker.
- **Yön bazlı Sharpe tek bakiyeyi bölerek değil, kümülatif R eğrisinden türetilir.** Hesap tektir
  ve bölünemez (ortak margin, ortak funding, ortak nakit); bakiye eğrisini zorla long/short diye
  ikiye ayırmak uydurma sayılar üretir. Bunun yerine yön bazlı Sharpe, o yöndeki işlemlerin
  kapanış sırasına göre dizilmiş **R cinsinden getiri dizisinden** hesaplanır. Toplam (hesap)
  Sharpe'ı ise gerçek bakiye eğrisinden gelir ve iki yön Sharpe'ının toplamı ya da ortalaması
  **değildir** — tabloda ayrı bir satır olarak durur.

## Kabul Çıtası (iki kapı + bir uyarı)

Tablo "hangi model önde" der; çıta "bu satır okunabilir mi" der. Hesap
`core/metrics.py::acceptance_flags`, eşikler `config.yaml > acceptance`.

**İki KAPI** — bir model ancak **ikisi birden** yeşilken doğrulanmış (`passed`) sayılır:

| Kapı | Soru | Geçme koşulu |
|---|---|---|
| **Ö** — örneklem | Bu ortalama bir ölçüm mü, gürültü mü? | R'ye giren kapanmış POZİSYON ≥ `acceptance.min_trades` (30); kısmi çıkış ve fraksiyonel hedef dilimleri ayrı sayılmaz (bkz. "Dolum ve Pozisyon") |
| **E** — edge | Sonuç sinyalden mi geliyor, piyasadan ve şanstan mı? | ortalama R > 0 **ve** kontrol grubunun ortalama R'sini **en az `acceptance.edge_margin_r` (0.15R) marjla** aşıyor **ve** farkın bootstrap güven aralığının **alt sınırı > 0** (`acceptance.edge_ci_alpha`) **ve** hesap getirisi referans çıpasını geçiyor. Kontrolün KENDİ örneklemi `acceptance.control_min_trades`in altındaysa kapı **değerlendirilemez** ve geçilmiş sayılmaz |

**Bir UYARI** — `passed`'ı **etkilemez**, yalnızca sonucun nasıl okunacağını söyler:

| Uyarı | Soru | Tetiklenme |
|---|---|---|
| **⚠ B** — band | Bu satır başka bir satırla aynı maliyet ölçeğinde mi? (kural 14) | `avg_stop_distance_pct`, yarışmacı medyanının `medyan/√oran .. medyan×√oran` bandının **dışında** |

Kararlar:

- **Band neden kapı değil.** Bandın dışında kalmak bir kusur değil, bir **kıyas koşuludur.**
  Modelin kendi ölçümü geçerlidir; sorun ancak o satır bir başkasının yanına konduğunda
  doğar — model aynı 1R'yi farklı notional ile taşımış, yani R başına farklı komisyon+kayma
  ödemiştir. Kapı yapmak iki ayrı soruyu birbirine karıştırırdı: *"bu model doğrulandı mı"*
  ile *"bu model şu modelle kıyaslanabilir mi."* Birincisi modelin kendi verisiyle
  cevaplanır, ikincisi ancak bir çiftle. Bu yüzden band tabloda bir uyarı ikonudur:
  görüldüğünde önce `cost_per_r` kolonuna bakılır, çünkü fark sonucu tek başına açıklıyor
  olabilir (CLAUDE.md > Rapor Kolonları).
- **Edge'in marjı neden var.** Kontrolü 0.01R ile geçen bir model marj olmadan "geçti"
  sayılırdı; oysa bilgisiz çekilişin kendi gürültüsü o kadar farkı tek başına üretir.
  `edge_margin_r`, kontrolü **anlamlı biçimde** geçmiş modeli kıl payı önde olandan ayırır.
  Karşılaştırma `fark >= marj` şeklindedir ("en az bu kadar"); iki kayan noktalı ortalamanın
  farkı söz konusu olduğu için sınırın ULP düzeyinde tanımı anlamsızdır ve koda yapay bir
  tolerans eklenmez.
- **Marj ile güven aralığı birbirinin yerine geçmez; ikisi de gerekir.** Marj bir ETKİ
  BÜYÜKLÜĞÜ eşiğidir ("fark yeterince büyük mü"), güven aralığı bir KESİNLİK eşiğidir
  ("fark örneklem gürültüsünden ayırt edilebiliyor mu"). 8 işlemle ölçülen 0.40R'lik bir
  fark marjı rahatça geçer ama aralığı sıfırı fazlasıyla içerir. Aralık **yüzdelik
  bootstrap** ile kurulur (`core/metrics.py::bootstrap_diff_ci`), normal varsayımıyla
  değil: stop'lu bir sistemde R dağılımı tanım gereği çarpıktır — kayıplar −1R civarında
  kümelenir, kazançlar uzun kuyruk yapar. İki örneklem BAĞIMSIZ yeniden örneklenir
  (model ile kontrol aynı barlarda aynı sembollerde işlem açmaz, eşleştirilecek çift
  yoktur). Hesap **deterministiktir**: tohum `random_seed`den türer ve model adına
  bağlanır, yani aynı defter her zaman aynı aralığı verir — rozetin koşudan koşuya
  titremesi, çıtayı bir ölçü olmaktan çıkarıp bir çekilişe çevirirdi.
- **Kontrolün KENDİ örneklemi de bir kapıdır** (`acceptance.control_min_trades`). Marj
  kontrolün ortalamasına göre ölçülür ve o ortalama da bir örneklemden gelir; kontrol
  n=4 iken bir modeli ona karşı 0.15R marjla "ölçmek" gürültüyü gürültüyle kıyaslamaktır
  (base katmanında gerçekten böyleydi). Kontrol kapısını geçmemişse `edge`
  **değerlendirilemez** ve `logger.warning` yazılır. **Kontrolün kümede HİÇ olmaması ayrı
  bir durumdur** ve yukarıdaki "koşul düşer" kuralı orada geçerli kalır: ikisini tek
  sayıya indirmek, kontrolü listeden çıkarmayı kapıyı geçmenin bir yolu hâline getirirdi.
- **Kapıyı geçmeyen satır SIRALANMAZ, ama gizlenmez de.** `format_report` ve dashboard
  rütbeyi yalnızca **Ö** kapısını geçen yarışmacılara verir; geçmeyenler ayrı bir bölümde
  ve işlem SAYISINA göre dizilir. Rozette "kapı geçilmedi" yazarken satırı "#1" olarak
  göstermek, okuyucunun ikincisini okuması demekti; 4 işlemlik bir ortalamayı 200
  işlemlik bir ortalamayla aynı sütunda sıralamak ise zaten kapının reddettiği kıyastır.
  Satır gizlenmez çünkü base katmanının ölçütü (karar 33) tam olarak "model n=30'a
  ulaşabiliyor mu"dur — bu yüzden ayrı bölümün sırası R değil, kapıya uzaklıktır.
- **Kapılar yalnızca yarışmacılara uygulanır.** Referans çıpası (kural 15) yarışmacı
  değildir; ölçmediği bir yarışta not vermek, çıpanın ne olduğunu yanlış anlatırdı. Çıpa
  `acceptance` bölümüne hiç girmez, tabloda bayrak sütununda `—` görünür.
- **Kontrol grubu kapılara girer.** `random_ctrl` bir yarışmacıdır (`is_benchmark = False`)
  ve kendi edge kapısında kendini geçemez (kendisiyle farkı 0, gereken marj 0.15R). Bilgisiz
  çekilişin sıralamada nerede durduğu gizlenecek bir kusur değil, raporlanacak bir sonuçtur.
- **Band neden medyana bağlı:** kural 14'ün bandı ATR katı cinsindendir, defterde ise ATR
  yoktur — işlem kapandıktan sonra "o anki ATR" geri hesaplanamaz ve geriye dönük yeniden
  hesaplamak look-ahead kapısı açardı. Medyan, aynı evrende aynı barlarda işlem yapan
  modellerin ortak volatilite ölçeğini taşır.
- **Edge'in "çıpayı geç" koşulu** kural 15'in sorusudur: on model de pozitif getirse ama
  hiçbiri çıpayı geçemese sonuç "stratejiler işe yarıyor" değildir. Birden çok çıpa varsa
  **en yükseği** zemindir; geçilmesi en kolay olanı seçmek çıtayı sessizce indirirdi.
- **Kontrol ya da çıpa kümede yoksa** ilgili koşul değerlendirilemez ve `edge` geri kalan
  koşullara düşer — ama bu sessiz olmaz, `logger.warning` ile söylenir. Eksik bir çıta,
  geçilmiş bir çıta gibi görünmemelidir.

## Kod Stili

- Python 3.11+. Tüm fonksiyon ve metod imzalarında type hint zorunludur.
- Harici bağımlılık minimumda tutulur: `requests`, `pandas`, `numpy`, `pyyaml` dışında yeni
  bağımlılık eklenmeden önce gerekçelendirilmelidir.
- Her modül, diğer `core` modülleri mock/stub'lanarak tek başına test edilebilir olmalıdır
  (gevşek bağlılık, açık ve dar arayüzler).
- Yorum yalnızca "neden" sorusuna cevap verdiğinde yazılır; kodun zaten söylediği "ne
  yaptığını" tekrar eden yorum yazılmaz.

## Strateji Arayüz Sözleşmesi (onaylandı — `strategies/base.py` içinde uygulandı)

```python
class Strategy(ABC):
    name: str
    allowed_directions: list[Direction]   # ["long"], ["short"] veya ["long", "short"]
    is_meta: bool = False                 # True ise engine'in ikinci geçişinde çalışır
    is_benchmark: bool = False            # True ise yarışmacı değil referans çıpası (kural 15)
    is_replica: bool = False              # True ise yarışmacı değil dış sistem kopyası (kural 15b)
    limits: ModelLimits | None = None     # yalnızca is_replica (kapı: core/validate.py)

    @abstractmethod
    def generate_signals(
        self,
        market: MarketData,
        peer_signals: Mapping[str, tuple[Signal, ...]] | None = None,
    ) -> list[Signal]:
        """Yeni pozisyon açılışı önerir. Mevcut pozisyonlara dokunmaz.

        peer_signals yalnızca is_meta=True modellere doldurulur: strateji adı ->
        o turda üretilmiş sinyallerin salt okunur kopyası. Normal modellerde None.
        """

    def manage_positions(
        self, market: MarketData, positions: list[Position]
    ) -> list[ExitInstruction]:
        """Mevcut pozisyonlarda kapanış/kısmi çıkış önerir. Varsayılan: hiçbir şey yapma."""
        return []

    def observe_closed_trades(self, trades: Sequence[ClosedTrade]) -> None:
        """Modelin KENDİ kapanmış işlemleri; generate_signals'tan ÖNCE, turda bir kez.

        Varsayılan: hiçbir şey yapma. Motor kancayı yalnızca uygulayan modeller için
        doldurur (kural 16); açık pozisyon bu listeye asla giremez.
        """
        return None

    def take_survey(self) -> Mapping[str, int] | None:
        """Son generate_signals çağrısının ELEME SAYIMI: sebep kodu -> sembol sayısı.

        Varsayılan None = bu model sayım tutmaz. Motor her BARDAN sonra okur ve tur
        raporuna toplar (core/engine.py::ModelReport.survey -> metrics_*.json >
        round.models[].survey). `rejections`/`emitted` ile aynı statüde bir DENETİM
        İZİDİR (kural 15): ölçüme girmez, sinyalleri ve sıralarını etkilemez.
        """
        return None


@dataclass(frozen=True, kw_only=True)
class TakeProfit:
    price: float
    fraction: float          # 0 < fraction <= 1.0; bir Signal içindeki toplam <= 1.0


@dataclass(frozen=True, kw_only=True)
class PartialTakeProfit:
    """Kısmi çıkış İSTEĞİ: fiyat değil R SEVİYESİ (kural 13b)."""
    r: float                 # giriş anındaki riskin katı; fiyata çeviren core/portfolio.py
    fraction: float          # 0 < fraction < 1.0; 1.0 kısmi değil TAM çıkıştır


@dataclass(frozen=True, kw_only=True)
class ModelLimits:
    """Kopya modelin KENDİ kuralları; kök kotaları yalnızca DARALTIR (kural 15b)."""
    max_positions: int | None = None
    max_per_direction: int | None = None
    max_portfolio_risk: float | None = None   # Σ açık risk / sermaye tavanı
    leverage: float | None = None             # yalnızca sizing="notional_fraction"; tavan 10x


SizingMode = Literal["risk", "notional_fraction"]


@dataclass(frozen=True, kw_only=True)
class Signal:
    symbol: str
    direction: Direction               # "long" | "short"
    stop_price: float | None = None    # sizing="risk" iken ZORUNLU, aksi hâlde None OLMALI
    sizing: SizingMode = "risk"        # "notional_fraction" yalnızca is_benchmark (kural 15)
    notional_fraction: float | None = None   # yalnızca sizing="notional_fraction" iken
    entry_type: Literal["market", "limit"] = "market"   # v1'de yalnızca "market" işlenir
    take_profits: tuple[TakeProfit, ...] = ()
    trailing_atr: float | None = None  # uygulaması core/engine.py'de, strateji yazmaz
    # Üç aşamalı çıkış yönetimi (kural 13b): hepsi OPSİYONEL, varsayılan KAPALI.
    breakeven_at_r: float | None = None        # bu R'a ulaşınca stop girişe çekilir
    partial_tp: PartialTakeProfit | None = None  # bu R'da kısmi çıkış + stop o seviyeye
    trail_giveback_pct: float | None = None    # kısmi SONRASI takip; trailing_atr ile birlikte OLMAZ
    reason: str = ""                   # deftere yazılacak serbest metin


@dataclass(frozen=True, kw_only=True)
class Position:
    """manage_positions'a verilen salt okunur pozisyon görünümü."""
    symbol: str
    direction: Direction
    entry_price: float
    stop_price: float | None           # referans çıpasında stop yoktur (kural 15)
    take_profits: tuple[TakeProfit, ...] = ()
    trailing_atr: float | None = None
    breakeven_at_r: float | None = None
    partial_tp: PartialTakeProfit | None = None
    trail_giveback_pct: float | None = None
    partial_done: bool = False         # kısmi çıkış doldu mu (bir İSTEK değil, bir OLAY)
    opened_at: pd.Timestamp


@dataclass(frozen=True, kw_only=True)
class ClosedTrade:
    """Modelin KENDİ kapanmış işleminin salt okunur görünümü (kural 16)."""
    symbol: str
    direction: Direction
    opened_at: pd.Timestamp
    closed_at: pd.Timestamp
    r_multiple: float | None           # pnl / risk_amount; risk bilinmiyorsa None (0.0 değil)
    signal_reason: str
    exit_reason: str


@dataclass(frozen=True, kw_only=True)
class ExitInstruction:
    symbol: str
    action: Literal["close", "reduce"]
    fraction: float = 1.0    # yalnızca action="reduce" için anlamlı
    reason: str = ""


@dataclass(frozen=True, kw_only=True)
class MarketData:
    ohlcv: dict[str, pd.DataFrame]     # sembol -> 4h OHLCV, yalnızca KAPANMIŞ barlar
    btc: pd.DataFrame                  # BTC referans verisi (aynı kural)
    funding: dict[str, pd.Series]      # sembol -> zaman indeksli funding geçmişi
    as_of: pd.Timestamp                # değerlendirilen son KAPANMIŞ barın zamanı
```

Tasarım kararları:

- **`entry_type`**: sözleşmede `"limit"` de yazılabilir, ama v1'de yalnızca `"market"`
  işlenir — `core/validate.py` `"limit"` gördüğünde `NotImplementedError` fırlatır. Limit
  emri desteklemek "bekleyen emirler", geçerlilik süresi ve mum-içi dokunma kontrolü
  gerektirir; bu da look-ahead hatası için yeni bir kapı açar. Alan sözleşmede duruyor,
  uygulaması ileri bir karar.
- **`funding` tek oran değil, seri**: bazı modeller son N periyodun funding trendine bakar
  (funding'in yönü ve hızlanması bir kalabalıklık göstergesidir). Tek bir `float` bu bilgiyi
  taşıyamadığı için alan zaman indeksli `pd.Series`'e çevrildi. Seri de kural 12'ye tabidir:
  yalnızca `as_of` ve öncesindeki funding değerlerini içerir.
- **`as_of` neden sözleşmede**: stratejinin "şimdi"yi `pd.Timestamp.now()` ile ya da
  DataFrame'in son satırından tahmin ederek okuması, look-ahead hatasının en sık kapısıdır.
  Tek ve açık bir "şimdi" tanımı, kural 12'yi test edilebilir kılar.
- **Kapanış sinyali `generate_signals` üzerinden değil**: `generate_signals` sadece açılış,
  `manage_positions` sadece kapanış/kısmi çıkış. Engine'in "bu sinyal yeni pozisyon mu,
  mevcut pozisyona müdahale mi" diye tahmin yürütmesi gerekmez.
- **Meta-stratejiler ve iki geçişli tur**: bir modelin diğerlerinin sinyallerini okuması
  (konsensüs sayma, aykırı gitme, sinyal filtreleme) ölçmeye değer bir strateji sınıfıdır,
  ama izolasyonu kırma riski taşır. Bu yüzden istisna dar tutuldu: meta model **yalnızca o
  turun sinyallerini**, **yalnızca kopya olarak** görür; pozisyon/bakiye/iç duruma erişimi
  yoktur ve **başka meta modelleri göremez.** Meta'ların birbirini okuması sıralamaya bağlı
  sonuç (kim önce çalıştıysa avantajlı) üretirdi — bu da adil karşılaştırmayı bozar.
  `peer_signals` derin kopya olarak verilir: `Signal` frozen olsa da `take_profits` listesi
  değiştirilebilir olduğundan, kopyalamadan paylaşmak bir modelin diğerinin sinyalini
  bozmasına izin verirdi.
- **`take_profits` tuple, liste değil**: `Signal` ve `Position` `frozen=True` ama mutable bir
  liste taşıdıklarında dondurma yarım kalır — bir strateji (ya da `peer_signals` okuyan bir meta
  model) kendi veya başkasının sinyalinin hedeflerini yerinde değiştirebilirdi. Tuple, sözleşmeyi
  gerçekten salt okunur yapar ve `peer_signals` kopyalamasının garantisini tamamlar.
- **`allowed_directions` ihlali → `ValueError`**: bu bir piyasa durumu değil, programlama
  hatasıdır. Sessiz filtreleme, short-only bir modelin aylarca yarı yarıya az işlem yapıp
  bunu kimsenin fark etmemesi demek — tam da ölçmeye çalıştığımız şeyi bozar. CI/çalıştırma
  workflow'u modeli bazında hatayı yakalayıp yalnızca o modeli atlar, koşu durmaz.
- **`core/validate.py`**: yukarıdaki doğrulamayı (ve stop==entry sıfıra bölme, stop yanlış
  taraf, TP yönü tutarsız, fraction toplamı > 1.0, evren dışı sembol kontrollerini) tek yere
  toplar. Her strateji sinyali motora girmeden bu kapıdan geçer.
- **Likidasyon stop'tan önce**: gerçek borsada bakım marjı ihlali stop emrini beklemez.
  Kontrolü stop'tan sonra yapmak, yüksek kaldıraçlı modellere gerçekte var olmayan bir
  kurtulma şansı verir ve tam da kıyaslamak istediğimiz risk farkını gizler. Kontrol mum içi
  `high`/`low` ile yapılır; kapanış fiyatıyla yapmak aynı hatanın daha yumuşak hâlidir.
- **`as_of` sabit bir çıpaya bağlıdır — BTC**: `core/data.py` anlık görüntüyü kurarken
  `as_of`'u `exchange.btc_reference` sembolünün son kapanmış barından okur. Sembollerin
  **ortak** (minimum) barını kullanmak iki sorun üretiyordu: (a) döngüsel bağımlılık — bayat
  sembolü dışlamak için `as_of`, `as_of` için sembol listesi gerekiyordu; (b) tek bir gecikmiş
  sembol turun "şimdi"sini bir bar geri çekiyordu, bu da zaten işlenmiş bir barı tekrar işlemek
  (çift işlem) ya da turun hiç ilerlememesi (donmuş sistem) demekti. BTC hem her modelin rejim
  filtresinde referans hem de en likit ve en az gecikecek sembol. `as_of` barına sahip olmayan
  semboller **o tur dışlanır ve loglanır** — hangi turda kaç sembolün görülebildiği sonradan
  denetlenebilsin diye. `data.max_staleness_bars` artık sembol başına tolerans değil, çıpanın
  kendi tazeliğinin sınırıdır: BTC verisi bundan daha geride kalmışsa anlık görüntü hiç
  üretilmez (bayat veriyle işlem açmaktansa tur düşer).
- **Boyutlandırma muafiyeti neden bir bayrağa bağlandı (kural 15)**: alternatif, alım-tut
  çıpasına yapay bir stop (örn. girişin %99 altında) takıp kural 11'i hiç değiştirmemekti. O yol
  daha az kod değiştiriyordu ama iki şeyi bozuyordu: (a) uydurma stop, `risk_amount` üzerinden
  uydurma bir R üretir ve çıpa yarışmacılarla aynı sütunda sıralanabilir hâle gelirdi — oysa
  ölçtüğü şey farklı; (b) %1 risk kuralı çıpayı sermayenin küçük bir dilimine hapseder, "piyasa
  ne yaptı" sorusunun cevabı ise sermayenin tamamının piyasada olmasını gerektirir. Bayrak,
  muafiyeti **tek bir yerde denetlenebilir** kılar: `core/validate.py`'de bir satır, tablonun
  ayrı bir bölümü. Muafiyetin gizli kalmadığı, `is_benchmark` alanının hem sözleşmede hem
  raporda görünmesiyle güvence altındadır.
- **Long/short metriklerinin ayrılması**: projenin ana sorusu short işlemlerin görece
  başarısı olduğu için birleşik bir Sharpe ya da win-rate cevabı vermez. Ayrıştırma
  raporlamanın varsayılanıdır, ek bir seçenek değil.
