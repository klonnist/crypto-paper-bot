"""MODEL 12 — beş kol, EŞİT ağırlık, öğrenme yok: model 11'in NULL HİPOTEZİ.

Bu model bir strateji fikri değil, bir ÖLÇÜM ARACIDIR. Model 11 (`scalp_bandit`) kollar
arası tahsisi Thompson sampling ile öğrenir; bu model aynı kolları, aynı kapılarla, aynı
stop/hedef geometrisiyle ve aynı zaman stop'uyla oynar — tek farkı kol seçiminin öğrenmeden
yapılmasıdır. İki modelin ortalama R farkı bu yüzden tek bir şeyin ölçüsüdür:
**adaptasyonun katkısı.**

**Tasarımı bozulamaz** (`strategies/random_ctrl.py` ile aynı gerekçe). Buraya eklenen her
iyileştirme — "kötü kolu ele", "hacmi düşük sembolde oynama", ağırlıkları elle ayarlamak —
null hipotezini sessizce bir stratejiye çevirir ve model 11'in farkının neye karşı
ölçüldüğü bilinemez hâle gelir. Eşit ağırlık burada bir parametre değil, tanımın kendisidir.

**Neden eşit ağırlık "her kolu sırayla oynamak" değil de çekiliş.** Sıralı (round-robin)
tahsis, kolların o turda kurulum üretip üretmemesine bağlı olarak sistematik bir sıra
oluşturur: hep aynı kol günün aynı saatine denk gelir. Uygun kollar arasından tekdüze
çekiliş, uzun vadede eşit tahsisi verir ve saat/kol eşleşmesi üretmez. Çekiliş sabit
tohumludur (bkz. `ScalpModel._round_rng`), yani tekrarlanabilir.

**`post_r` neden `nan`.** Bu modelin posterior'ı yoktur; kol seçimi hiçbir tahminden
beslenmez. `0.0` yazmak "ölçtüm, sıfır çıktı" demek olurdu — oysa burada ölçülen bir şey
yok. Etiketin kendisi (`| arm=...`) yine yazılır: kol bazlı kırılım iki model için de
aynı yoldan okunur, yoksa model 12'nin kol tablosu hiç üretilemezdi.

Rollere dikkat: bu modül boyut/komisyon/bakiye hesaplamaz (kural 1/2/3/7) ve deftere
yazmaz (kural 1). Kol mantığı ve kapılar `strategies/scalp/`dedir; bu dosyada yalnızca
seçim kuralı vardır.
"""

from __future__ import annotations

import logging
import random
from typing import Sequence

from strategies.base import Direction
from strategies.scalp.model import ScalpModel

logger = logging.getLogger(__name__)

NAN = float("nan")


class ScalpFixed(ScalpModel):
    name = "scalp_fixed"
    allowed_directions: list[Direction] = ["long", "short"]

    def choose_arm(
        self, available: Sequence[str], *, rng: random.Random
    ) -> tuple[str, float]:
        """Uygun kollar arasından tekdüze çekiliş. Geçmişe BAKILMAZ.

        `observe_closed_trades` bilinçli olarak uygulanmaz: kanca uygulanmadığı için motor
        bu modele defteri hiç okutmaz ve "öğrenmiyor" iddiası koddan denetlenebilir olur —
        modelin geçmişe erişimi yoktur, kullanmamayı seçmesi gerekmez.
        """
        arms = list(available)
        if not arms:
            raise ValueError(f"{self.name}: seçilecek kol yok")
        choice = rng.choice(arms)
        logger.info(
            "%s: eşit ağırlıklı çekiliş (%d uygun kol) -> %s", self.name, len(arms), choice
        )
        return choice, NAN
