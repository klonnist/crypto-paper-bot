# TASLAK — P3: motor kuralları (ONAY BEKLİYOR)

> **Bu belge bir KARAR DEĞİLDİR.** Hiçbir `core/` kodu yazılmadı, hiçbir config değeri
> değişmedi, hiçbir model davranışı oynamadı. Üç kural değişikliği önerisinin ÖNCE ÖLÇÜLMÜŞ
> sayıları, seçenekleri, etkilenen modelleri ve geçiş planı vardır; hangisinin (ve hiç)
> uygulanacağı kullanıcı onayına bağlıdır. Onaylanan her madde AYRI bir karar olarak
> `docs/decisions.md`ye yeni numarayla yazılır (karar 25/42 deseni); bu taslak o zaman
> `docs/decisions.md > 54+`ya taşınır ve burada bir gönderme bırakılır.
>
> Ölçümler `scripts/measure_engine_rules.py` ile üretilir (salt okunur, getiri/R'ye bakmaz,
> `tests/test_measure_engine_rules.py`). Pencere: canlı defter `2026-09-20 → 2026-10-05T19:15Z`,
> 1468 tur / 1469 bar. Hiçbir getiri/R sayısı bu taslağın dayanağı değildir (§7).

## 0. Ortak çerçeve (üç madde için de geçerli)

**Neden kural 6 + kural 1 bunu zorlaştırıyor.** Üçü de `core/` kuralıdır. Kural 6 bir
katmanın ölçüm KOŞULLARINI değiştirebileceğini ama KURALLARINI (maliyet, risk, likidasyon,
dolum, metrik tanımı) değiştiremeyeceğini söyler; yani bir motor kuralı **bütün katmanlarda ve
bütün modellerde** aynı anda değişir. Kural 1 defterin geriye dönük düzeltilemeyeceğini söyler;
karar 42'nin tablosuna göre kural katmanındaki değişiklik geriye dönük uygulanamaz — meşru
karşılığı aynı pencerede **backtest**'tir.

**Ortak geçiş planı (her seçenek için aynı iskelet).**

1. **Config anahtarı, varsayılan = BUGÜNKÜ davranış.** Kural `config.yaml`a açık bir anahtar
   olarak girer (kural 6: tek kaynak); varsayılan değer bugünkü davranıştır, yani anahtarı
   eklemek hiçbir defteri değiştirmez ve tek başına bir commit olarak güvenle çıkar.
2. **Backtest harness'ta her iki ayarla** (`scripts/backtest.py`, Kapı 0 geçmiş bir pencerede
   canlıyla eşleştirilerek): ön-kayıt commit'i koşudan ÖNCE; yalnızca SAYIM ve geçerlilik
   kapıları okunur (kaç pozisyon, hangi ret kodları, `missing_bars` …); getiri/R yorumlanmaz
   çünkü bu bir performans hipotezi değil bir ÖLÇÜM GEÇERLİLİĞİ düzeltmesidir (karar 25'in
   `fee_rate` düzeltmesiyle aynı statü).
3. **TARİHLİ KESİM.** Anahtar değeri belirli bir `as_of`ta değişir ve kesim zamanı
   `docs/decisions.md`ye yazılır: defter iki rejime bölünür (karar 25'teki gibi), `trades.csv`
   hiçbir satırı değişmez, iki dönemin metrikleri doğrudan kıyaslanamaz ve bu satır kesim
   kararında açıkça yazılır.
4. **Sicil.** Ön-kayıt borcu (karar 42, test 5): bu değişiklikler bir performans iddiası
   taşımıyorsa BH paydasına (§6c) GİRMEZ ama sicile "kural değişikliği, payda dışı" satırıyla
   yazılır — silinmeyen bir kayıt (§6c). Bu yorumun kendisi onayınıza bağlıdır (Soru S1).

**Katmanlar arası etki özeti** (ölçülmüş; kapanmış + açık pozisyon):

| katman | model | pozisyon | (a) girişte ters yönlü | (a) aralık çakışması | (b) kaldıraçlı (`leverage>1`) / kapanmış |
|---|---|---:|---:|---:|---:|
| scalp | `scalp_fixed` | 201 | **18** | **36** | **105 / 198** |
| scalp | `scalp_patient` | 84 | **19** | **35** | **51 / 80** |
| scalp | `vwap_managed` | 26 | 0 | 0 | 4 / 25 |
| scalp | `vwap_clone` | 905 | 0 | 0 | 904 / 904 (kendi boyutlandırması, bu kurallardan bağımsız) |
| base | `trend` | 84 | 0 | 0 | 7 / 83 |
| base | `meanrev` | 17 | 0 | 0 | 4 / 14 |
| base | `random_ctrl` | 11 | 0 | 0 | 2 / 6 |
| base | `buyhold` | 2 | 0 | 0 | 0 / 0 (`notional_fraction`) |

---

## 1. P3a — Ters yönlü pozisyon

### Ölçüm

`core/portfolio.py::Account.find(symbol, direction)` yalnızca **aynı yöne** bakar; aynı
sembolde karşı yönde açık bir pozisyon açılmayı ENGELLEMEZ ve iki pozisyon ayrı marj, ayrı
komisyon ve ayrı stop'la birlikte taşınır.

- **Yalnızca iki model etkileniyor:** `scalp_fixed` (201 pozisyonun **18**'i girişte, **36**'sı
  aralıkça çakışıyor: %9 / %18) ve `scalp_patient` (84 pozisyonun **19**'u girişte, **35**'i
  çakışıyor: %23 / %42). Başka hiçbir modelde, hiçbir katmanda tek bir örnek yok (`vwap_clone`
  905 pozisyonda 0; `vwap_managed` 26'da 0; base'in dört modeli 0).
- Çakışma oranı patient'ta iki kat: 100 barlık tutuş (karar 31) ters yönlü bir sinyalin
  gelmesi için daha uzun bir pencere bırakıyor — yani etki **`scalp_fixed ↔ scalp_patient`
  ekseninin ölçmediği bir değişken** (süre değişkeninin yan etkisi; karar 52 > 17-DÜZELTME ile
  aynı aile).
- Gerçek bir Bybit USDT perpetual hesabında bu davranış hesap moduna bağlıdır (tek yönlü
  modda iki pozisyon netleşir, hedge modunda ayrı durur). Defter hangi modu simüle ettiğini
  YAZMIYOR; bu taslak bir mod tercihi yapmaz — yalnızca mevcut davranışı ve seçenekleri sayar.

### Seçenekler

| | davranış | etki (ölçülmüş üst sınır) | not |
|---|---|---|---|
| **A0** | Dokunma; belgele | — | Davranış karar 17-DÜZELTME'de zaten kayıtlı |
| **A1** | `opposite_position` ret kodu: karşı yönde açık pozisyon varken dolum reddedilir | patient'ta **19/84 (%23)**, fixed'te **18/201 (%9)** pozisyon açılmazdı; ikinci derece etki (boşalan nakit/kota sonraki sinyalleri açar) ÖLÇÜLEMEZ | `RejectReason`e yeni kod (kural 15 gereği sayılır); en küçük kural |
| **A2** | Kapat-ve-çevir: yeni sinyal karşı pozisyonu piyasa fiyatından kapatır, sonra açar | patient'ın 100 barlık tutuşunu ters sinyaller KESER → `12 ↔ 16` ekseninin değişkeni (süre) kendi etkisini kaybeder | Yeni bir çıkış sebebi (`flip`), `exit_rule` etiketi, kısmi dolum davranışı; en çok kod |
| **A3** | Kuralı değiştirme; **ölçüm ekle**: raporda ters yönlü eşzamanlı pozisyon sayısı ve süresi | etki yok; defter dokunulmaz | Katman 2 (ölçüm): karar 42'ye göre geriye dönük uygulanır, defter bölünmez |

### Etkilenen modeller / geçiş

- **A1/A2 etkilenen:** `scalp_fixed`, `scalp_patient` (ve bu kolları miras alan her gelecek model).
  Başka model etkilenmez; ama kural bütün modellere uygulanır (kural 6), yani base katmanının
  defteri de "aynı kural rejimine" geçer (ölçülmüş sıfır etkiyle).
- **Geçiş:** §0'daki iskelet. `portfolio.opposite_position: allow | reject | flip`, varsayılan
  `allow`. Kesim zamanı bir `as_of`.
- **Dikkat (A1):** reddedilen sinyaller `signals` sayısında kalır (kural 15: üretildi, açılmadı),
  yani `signals → pozisyon` oranı düşer; `acceptance.min_trades` kapısına ulaşma süresi uzar.

**Öneri (tartışmaya açık):** önce **A3** (ölçüm, hemen, geriye dönük, defteri bölmez); A1'e
geçme kararı ancak A3'ün gösterdiği çakışma bir modelin ölçümünü okunamaz kılıyorsa. Gerekçe:
çakışmanın bir ÖLÇÜM sorusunu mu (hangi model neyi taşıyor) yoksa bir SİMÜLASYON GEÇERLİLİĞİ
sorusunu mu (hesap modu) ilgilendirdiği bilinmeden kural yazmak, karar 25'in "yanlış borsa"
hatasını başka bir yerde tekrarlamak olur.

---

## 2. P3b — Teminat kuralı

### Ölçüm

`core/portfolio.py::size_position`: `leverage = notional/free_cash` (nakdi aşınca),
`margin = min(notional/leverage, free_cash) = free_cash` — yani notional serbest nakdi aşan İLK
pozisyon nakdin TAMAMINI marj yapar ve sonraki sinyaller `zero_size` ile reddedilir; `leverage_cap`
(5x) bu yolda hiç devreye girmez (tavanın ALTINDA kaldıraç kullanılıyor ama nakit tükeniyor).

- **Yaygın:** kaldıraçlı (nakdi aşıp marjı tüm nakit yapan) pozisyonlar `scalp_fixed`'te
  **105/198 (%53)**, `scalp_patient`'ta **51/80 (%64)**, `vwap_managed`'ta 4/25 (%16), `trend`
  7/83 (%8), `meanrev` 4/14 (%29), `random_ctrl` 2/6 (%33). Risk boyutlu 406 kapanmış pozisyonun
  **173'ü (%43)** bu yoldan açıldı. Karar 17'nin "~1 pozisyon" varsayımı %1 stop'a dayanıyordu;
  bugün ortalama stop %2.8–3.2 (karar 52 > 17-DÜZELTME).
- **`zero_size` retleri nakitten geliyor, kotadan değil:** `scalp_patient` 90 ret — açık pozisyon
  sayısı **{0: 1, 2: 1, 3: 10, 4: 78}**; `scalp_fixed` 96 ret — **{2: 27, 3: 52, 4: 17}**. 186 retin
  **hiçbiri** kota doluyken (5 açık) yaşanmadı (kota doluyken ret `max_positions` koduyla gelir).
- **Nakit sürekli tükenmiş durumda:** nakit özsermayenin %2'sinin altında geçen bar payı
  `scalp_patient` **904/1469 (%61.5)**, `scalp_fixed` **547/1469 (%37.2)**.
- **Yan etki (önemsiz ama kayıtlı):** komisyon marjdan SONRA ödendiği için nakit hafif eksiye
  düşüyor — `scalp_patient` 262, `scalp_fixed` 70 barda negatif, en kötü **−0.87 $ (−%0.010)**.

### Seçenekler

| | davranış | etki (ölçülmüş üst sınır) | not |
|---|---|---|---|
| **B0** | Dokunma; belgele | — | `zero_size` kota-dışı bir sınır olarak kalır |
| **B1** | Sabit kaldıraç tavanı: notional nakdi aşınca `margin = notional / leverage_cap` | patient'ta 90, fixed'te 96 ret açılışa dönüşebilir (**+%107 / +%48** pozisyon, kota ve diğer kapılar izin verdiği ölçüde) | Toplam notional'ın `leverage_cap × özsermaye`yi aşmaması ayrı bir kapı ister (bugün marj=nakit bunu örtük sağlıyor); likidasyon fiyatı DEĞİŞİR (daha az marj) |
| **B2** | Pozisyon başına teminat bütçesi (`özsermaye / max_positions`); aşarsa boyut KÜÇÜLTÜLÜR (kural 11'in "atlama, küçült" ilkesi) | zero_size büyük ölçüde kalkar; R'nin paydası (`risk_amount`) küçülür → kırpılan pozisyonların risk birimi farklılaşır | `risk_per_trade` %1'in ALTINA düşen pozisyonlar oluşur: "1R" tanımı pozisyonlar arası eşit olmaktan çıkar (kural 11/metrik) |
| **B3** | Kuralı değiştirme; **ölçüm ekle**: raporda `zero_size` ↔ açık pozisyon sayısı, nakit-tükenmiş bar payı | etki yok | Katman 2; geriye dönük, defteri bölmez |

### Etkilenen modeller / geçiş

- **Kapsam kural 6 gereği HER risk-boyutlu model:** `scalp_fixed`, `scalp_patient`, `vwap_managed`,
  base'te `trend`/`meanrev`/`random_ctrl`, `ema` katmanının modelleri. `vwap_clone`
  (`notional_fraction`, kendi sabit teminatı) ve `buyhold` etkilenmez.
- **Defter bölünmesi tüm katmanlarda olur** (base dahil; orada kaldıraçlı pozisyon payı küçük:
  13 / 103).
- **Geçiş:** §0 iskeleti; `portfolio.margin_rule: all_free_cash | leverage_cap | per_slot`,
  varsayılan `all_free_cash`.
- **B1/B2'nin ölçüm eksenlerine etkisi:** `12 ↔ 16` ekseninde fixed ve patient `zero_size`'dan
  FARKLI biçimde etkileniyor (fixed'te retler 2/3/4 açık pozisyonla 27/52/17; patient'ta 90 retin
  78'i dördüncü pozisyondayken); kural değişince iki modelin pozisyon kümesi de değişir — karar
  52'deki "56 ortak / 24 + 140 yalnız" tablosu yeniden ölçülmeli.

**Öneri (tartışmaya açık):** **B3** (ölçüm) hemen; B1/B2 ancak `scalp_patient ↔ scalp_fixed`
ekseninin okunabilirliği bu kuraldan etkileniyorsa (bugün etkileniyor: eşleşen n = 56'da 24/140
yalnız) — ve B1/B2'den hangisinin gerektiğine ancak backtest'te iki ayarla koşup SAYIMA bakarak
karar verilir. B2'nin "1R eşit değil" bedeli ağır görünüyor; B1'in toplam-notional kapısı ayrı
tasarım ister.

---

## 3. P3c — `vwap_managed`'da mükerrer sinyal kaybı

### Ölçüm

Model barda TEK sinyal oynar (en güçlü geçen aday) ve motor onu bir sonraki barın açılışında
doldurmayı dener; `duplicate_position` ile reddedilirse aynı bardaki başka adaylar KAYBOLUR
(kural 4: model kendi açık pozisyonunu göremez, yani yinelenen sinyali kendisi eleyemez).
**İki ayrı kayıp mekanizması karışmamalı:**

1. **"Barda tek sinyal" kaybı (kural değil, modelin tanımı):** kapılardan geçen adayların hepsi
   değil yalnızca en güçlüsü oynanır. Canlı pencerede **55 geçen aday → 35 sinyal barı (20 aday
   kaybolur, %36; 7 barda ≥ 2 geçen aday)**; canlı-öncesi 8.57 haftalık pencerede **192 → 127 (65,
   %34; 28 bar)**. Bunu "ilk doldurulabilir" kuralı GERİ ALMAZ: o adaylar hiç sıraya girmiyor.
2. **Yinelenen sinyalin ret kaybı:** 35 sinyalin **9'u `duplicate_position`** ile reddedildi.
   **Bu 9 retten yalnızca 2'sinde** aynı barda kapıları geçen BAŞKA bir aday vardı (2026-10-02
   19:15 ve 19:30'da SUI reddedildi; ETHFI, NEAR, SOL geçiyordu). Kalan 7'sinin tamamı aynı
   sembolde ardışık yinelenen sinyal (ör. NEAR long üç ardışık bar) — alternatifi yok. Yani
   "ilk doldurulabilir" kuralı canlıda 15 günde en fazla **+2 pozisyon** (26'nın %8'i) kazandırırdı.
   (Bu alternatif analizi canlı pencerede, aynı araç fonksiyonlarıyla ve defterdeki
   `emitted` kayıtlarıyla yapıldı; ayrı bir betik olarak depoda DEĞİL, yöntemi bu paragrafta.)

### Seçenekler

| | davranış | etki (ölçülmüş) | not |
|---|---|---|---|
| **C0** | Dokunma; belgele | — | Kayıp sayıları yukarıda; karar 52 > V5 ile tutarlı |
| **C1** | Sıralı aday listesi: model SIRALI bir aday grubu üretir, motor dolum anında ilk doldurulabileni alır | ≤ **+2 pozisyon / 15 gün** (%8); 20 kayıp adayı GERİ ALMAZ | `core/engine.py` (bekleyen emir grupları), `core/validate.py`, `strategies/base.py` sözleşmesi (kural 3/4) ve defter kuralı değişir — küçük kazanç için büyük yüzey |
| **C2** | "Barda tek sinyal" tanımını gevşet: AYRI bir ikiz model (`vwap_multi`), motora dokunmadan | +%36 sinyal (20/55); `vwap_managed ↔ scalp_fixed` kıyasının "bar başına tek pozisyon" şartı (model 14 docstring'i) ayrı bir modelde taşınır | Kural DEĞİL model (kural: yeni eksen = yeni model); defter bölünmez |
| **C3** | Model tarafında yinelemeyi bastır | **İmkânsız:** kural 4, model kendi açık pozisyonunu göremez | Sadece belge için |

### Etkilenen modeller / geçiş

- **C1:** yalnızca `vwap_managed` aday grubu üretir ama sözleşme (Signal/engine) HER modeli
  ilgilendirir; geçiş §0 iskeleti (`engine.fallback_candidates: off | on`, varsayılan `off`).
- **C2:** hiçbir mevcut model etkilenmez; yeni bir model, ön-kayıt ve ölçülebilirlik testiyle
  (karar 53) gelir.

**Öneri (tartışmaya açık):** **C0** — kazanç (+%8) engine sözleşmesini değiştirmeye yetmiyor ve
asıl kaybın (%36) çaresi bir kural değil yeni bir modeldir (C2), ki o da `vwap_reentry`
sonuçlanmadan öncelikli değildir. Bu, P4a'dan sonra yeniden değerlendirilebilir.

---

## 4. Onayınızı bekleyen sorular

- **S1.** Üç değişiklik de bir performans hipotezi DEĞİL, ölçüm geçerliliği düzeltmesidir;
  §6c sicile "payda dışı kural değişikliği" olarak mı yazılsın, yoksa BH paydasına mı girsin?
- **S2 (P3a).** A3 (ölçüm kolonu) önerisi uygun mu, yoksa A1/A2'den biri doğrudan mı istenir? Bybit
  hesabı tek yönlü mü hedge modunda mı kabul ediliyor (hesap modunu defter yazmıyor)?
- **S3 (P3b).** B3 önerisi uygun mu? B1/B2'den biri isteniyorsa hangisi (B2'nin "1R eşit değil"
  bedeli kabul edilebilir mi)?
- **S4 (P3c).** C0 önerisi uygun mu, yoksa C1/C2 mi?
- **S5 (hepsi).** Onaylananlar için geçiş: §0 iskeleti (varsayılan-bugün anahtar → backtest →
  tarihli kesim) kabul mü, yoksa tek adımda mı?
