"""MODEL 19 — `vwap_managed`in İKİZİ, TEK farkı giriş onayı: dönüş bandın İÇİNE kapanmalı.

**Neyi ölçer.** `vwap_managed ↔ vwap_reentry` ekseninin tek değişkeni sinyalin giriş
onayıdır. Model 14 "önceki bar bandın DIŞINDA kapandı ve bu bar VWAP'e doğru bir adım attı"
der; dönüş şartı `0 < z_now < z_prev` olduğu için 0.01σ'lık bir geri adım yeter (karar 52 >
V2: bant dışı barların %75'i "dönüş" sayılıyor). Bu model aynı kurulumun üstüne **bu barın
kapanışının bandın İÇİNE girmiş olmasını** ekler (`|z_now| < band_mult`). Cevapladığı soru:
*bandın içine geri dönmüş dönüşler, yalnızca küçük bir adım atmış dönüşlerden ayırt
edilebilir biçimde mi farklı?*

**Neden var.** Karar 52 > V3: canlı pencerede kapanışı bandın içine dönmüş 13 pozisyon +0.46R,
hâlâ dışında kalan 12 pozisyon −0.10R (tek yönlü permütasyon p≈0.03). Bu sayı POST-HOC bir
bölmedir ve kanıt değildir: sonuç görüldükten sonra seçilmiş bir eşiktir. Bu yüzden hipotez
canlı pencerede DEĞİL, `docs/backtest.md > 6g`deki ön-kayıtla taze bir pencerede sınanır.

**Tek değişkenli ve BİREBİR aynı olanlar.** Kapılar (%1 stop tabanı, 1.5R), stop
(`vwap.managed.atr_multiple × ATR`), hedef (projeksiyon ∧ VWAP), üç aşamalı çıkış yönetimi, 16
barlık zaman stop'u, "barda tek sinyal, en güçlü geçen aday" seçimi ve evren `VwapManaged`ten
MİRAS ALINIR, kopyalanmaz: bir gün model 14'te yapılan masum bir düzeltme ikizi sessizce
ayrıştırırdı ve hiçbir test bunu yakalamazdı. **Yeni config anahtarı YOKTUR**: model 14'ün
`vwap.*` anahtarlarını okur — ayrı anahtar, ayrı çıta ve ölçülmeyen ikinci bir değişken demekti.

**Aday kümesi baz kümenin ALT KÜMESİDİR** (şart yalnızca eler, hiçbir şey eklemez): bu bir
testle çivilidir (`tests/test_vwap_reentry.py`) ve `scripts/measure_vwap_signal.py --verify`
aynı cebri gerçek `scan()` çıktısında kanıtlar. Ama OYNANAN sinyaller alt küme olmak ZORUNDA
DEĞİLDİR: barda tek sinyal kuralı iki kümede ayrı uygulanır (baz, bandın dışına kapanan en güçlü
adayı oynayabilir; ikiz onu eler ve daha zayıf bir adayı oynar). Bu yüzden eşleştirilmiş eksen
(`scripts/paired_axis.py`) kesişimde ΔR'yi, kesişimin dışını ayrıca verir ve iki modelin
pozisyonları arasındaki fark çoğunlukla ΔR'den değil KÜME farkından gelir (docs/backtest.md > 6g).

**Kol etiketi ayrıdır** (`arm=vwap_revert_reentry`): tek bir ad iki farklı giriş şartını tek
kırılım satırı altında toplardı (karar 23'ün aynı gerekçesi).

**Canlıda KOŞMAZ.** `REGISTRY`de durur (backtest `--models` ile çağırır) ama
`layers.scalp.models` listesinde yoktur ve bu bir testle çivilidir
(`tests/test_vwap_reentry.py::test_the_candidate_is_not_in_the_live_layer`): doğrulanmamış bir
adayın gerçek (kâğıt) deftere yazması, bu altyapının engellemek için kurulduğu şeydir.

Rollere dikkat: bu modül boyut/komisyon/bakiye hesaplamaz (kural 1/2/3/7) ve deftere yazmaz.
"""

from __future__ import annotations

from strategies.vwap import signal as vwap_signal
from strategies.vwap_managed import VwapManaged


class VwapReentry(VwapManaged):
    name = "vwap_reentry"
    # Ayrışan TEK şey ve etiketi. Kapılar, stop, hedef, çıkış, zaman stop'u ve seçim miras.
    _reentry = True
    _arm_name = vwap_signal.REENTRY_ARM_NAME
