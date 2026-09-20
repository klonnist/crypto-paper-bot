"""MODEL 15 — `scalp_fixed`in BİREBİR İKİZİ, tek farkı üç aşamalı çıkış yönetimi.

Ölçtüğü soru tek: **çıkış yönetimi tek başına ne katıyor?**

    aynı beş kol            (strategies/scalp/arms.py)
    aynı eşit ağırlıklı çekiliş   (ScalpFixed.choose_arm — miras alınır, kopyalanmaz)
    aynı kapılar            (%1 stop tabanı, 1.5R hedef/stop)
    aynı zaman stop'u       (16 bar)
    aynı evren, aynı maliyet, aynı limitler
    FARK: breakeven -> kısmi çıkış + stop kaydırma -> geri verme takibi

**Eşleştirilmiş deney: çekiliş kimliği bilinçli olarak PAYLAŞILIR.** `rng_identity`
`scalp_fixed`in adına bağlıdır, yani iki model her turda AYNI kolu ve AYNI sembolü seçer.
Model 11 ↔ model 12'de tersi doğrudur (orada ölçülen şey seçimin kendisidir ve bağımsız
çekiliş şarttır); burada ölçülen şey seçim DEĞİL, aynı seçimin nasıl yönetildiğidir —
bağımsız çekiliş, farkın içine "hangi model şanslı kurulumu çekti" gürültüsünü katardı.

**Bu, iki modelin defterlerinin birebir aynı olacağı anlamına gelmez** ve gelmemelidir:
yönetim bazı pozisyonları erken kapatır, dolayısıyla `max_positions` doluluğu ve
`duplicate_position` retleri zamanla ayrışır. Ayrışan şey DOLUMLARDIR, sinyaller değil —
ve bu ayrışmanın kendisi yönetimin bir sonucudur, ölçümün bir kusuru değil.

**Neden `ScalpFixed`ten türer, `ScalpModel`den değil.** Kol seçimi kuralının ikinci bir
kopyası, iki modelin "aynı eşit ağırlıklı çekiliş" iddiasını koddan denetlenemez kılardı.
Miras, iddiayı bir yorum olmaktan çıkarıp bir olguya çevirir: `choose_arm` tek bir yerde
tanımlıdır.

**`observe_closed_trades` burada da UYGULANMAZ** (`ScalpFixed`ten devralınan sözleşme):
model geçmişe erişmez, dolayısıyla motor ona defteri hiç okutmaz. Öğrenme bu eksende
ölçülmüyor.

Rollere dikkat: bu modül boyut/komisyon/bakiye hesaplamaz (kural 1/2/3/7) ve deftere
yazmaz (kural 1). Çıkış yönetiminin TANIMI `strategies/exit_management.py`'de (modeller
13-14 ile tek kopya), UYGULAMASI `core/engine.py` ve `core/portfolio.py`'dedir (kural 9).
"""

from __future__ import annotations

from typing import Any, Mapping

from core.config import load_config
from strategies.base import Direction
from strategies.exit_management import ExitManagement
from strategies.scalp_fixed import ScalpFixed


class ScalpManaged(ScalpFixed):
    name = "scalp_managed"
    allowed_directions: list[Direction] = ["long", "short"]
    # Eşleştirilmiş kıyasın tamamı bu satırda: aynı tohum, aynı kurulum, farklı çıkış.
    rng_identity = ScalpFixed.name

    def __init__(self, *, config: Mapping[str, Any] | None = None) -> None:
        settings = dict(config) if config is not None else load_config()
        super().__init__(config=settings)
        self.exit_management = ExitManagement.from_config(settings)
