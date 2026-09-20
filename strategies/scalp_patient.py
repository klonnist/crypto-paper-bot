"""MODEL 16 — `scalp_fixed`in İKİZİ, tek farkı zaman stop'unun SINIRI.

**Neyi ölçer.** `scalp_fixed ↔ scalp_patient` ekseninin tek değişkeni `time_stop_bars`tır
(16 ↔ 100) ve cevapladığı soru şudur: *kuruluma hedefine varacak süreyi vermek işe yarıyor
mu?*

**Neden var (karar 30).** İlk uzun pencere backtest'i şunu ölçtü: `scalp_fixed`in 151
pozisyonundan YALNIZCA 2'si hedefe vardı (%1.3); %84'ü 16 barlık zaman stop'unda kesildi.
Sebep modelde değil, geometridedir:

    stop  = 5×ATR,  hedef = 2.0 × stop = 10×ATR
    16 barlık tipik yayılım = √16 = 4×ATR
    hedefin gerektirdiği süre = (10)² = 100 bar

Sürüklenmesiz rastgele yürüyüşün hedefe varma olasılığı %1.24; gözlenen %1.32. Yani hedefe
varma oranı, sinyalin hiçbir katkısı olmadığı varsayımının öngördüğü sayıyla örtüşüyor —
sinyal iyi ya da kötü değil, **hedefe ulaşmak için verilen süre yetmiyordu.**

**100 sayısı teoriden gelir, veriden DEĞİL.** `N = (hedef/ATR)² = 10² = 100`. Birkaç değer
deneyip en iyisini seçmek `docs/backtest.md > 7.1`in yasakladığı şeydir; o yüzden tek bir
değer ön-kayıtla sabitlendi ve taze bir OOS penceresinde sınanır.

**Neden `stop_atr_multiple`i küçültmek DEĞİL.** Hedefi yaklaştırmanın diğer yolu stop'u
daraltmaktı, ama %1'lik `min_stop_pct` tabanı buna izin vermez: ATR ≈ %0.48 iken 2×ATR ≈
%0.96 tabanın altında kalır ve kurulumların neredeyse tamamı elenirdi. Taban keyfi değil,
tur maliyetinin (~%0.25) 0.25R'yi aşmaması için vardır. Stop'u daraltmak ayrıca maliyet/R'yi
0.11'den ~0.25'e çıkarır — yani bir sorunu çözerken ölçüm kolonunu (kural 14) bozar.

**Neden YENİ bir model, `scalp_fixed`in parametresi değil.** `scalp_fixed` model 11'in null
hipotezidir; zaman stop'unu değiştirmek onun ölçtüğü ekseni (adaptasyonun katkısı) sessizce
başka bir şeye çevirirdi. CLAUDE.md'nin kuralı: tek değişkenli bir eksen isteniyorsa yolu
yeni bir model açmaktır.

**Çekiliş PAYLAŞILIR** (`rng_identity = "scalp_fixed"`): `scalp_managed` ile aynı gerekçe.
Ölçülen şey kol SEÇİMİ değil, aynı seçimin ne kadar taşındığıdır; iki model her barda aynı
kolu ve aynı sembolü seçer, aradaki ortalama R farkı yalnızca süreden gelir (eşleştirilmiş
deney). Defterlerinin birebir aynı olacağı anlamına gelmez: uzun tutuş `max_positions`
doluluğunu ayrıştırır — ve bu ayrışmanın kendisi sürenin bir SONUCUDUR.

**Kâğıt katmanında ölçülür, ama canlıya alma eşiğini GEÇMEDİ.** Taze OOS penceresinde
(karar 32) hedefe ulaşma oranını 14.5 kat artırdı ve ortalama R'yi −0.15'ten −0.01'e
taşıdı — ama C-1 (ortalama R > 0) sağlanmadı. Karar 33 ile katmanın `models` listesine
GİRDİ: `scalp` bir KÂĞIT ölçüm katmanıdır ve ileriye dönük kanıt ancak orada birikir.
Gerçek parayla işlem açmak ayrı bir karardır ve eşik henüz geçilmemiştir.

Rollere dikkat: bu modül boyut/komisyon/bakiye hesaplamaz (kural 1/2/3/7) ve deftere
yazmaz (kural 1). Kol seçimi `ScalpFixed`ten MİRAS ALINIR, kopyalanmaz.
"""

from __future__ import annotations

from strategies.scalp_fixed import ScalpFixed

CONFIG_KEY = "scalp.patient.time_stop_bars"


class ScalpPatient(ScalpFixed):
    name = "scalp_patient"
    # Çekiliş kimliği `scalp_fixed` ile ORTAK: eşleştirilmiş deney (bkz. docstring).
    rng_identity = "scalp_fixed"
    # Ayrışan TEK şey. Kural yine `strategies/time_stop.py`de tek kopyadır.
    time_stop_key = CONFIG_KEY
