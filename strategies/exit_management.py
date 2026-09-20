"""Üç aşamalı çıkış yönetiminin TEK tanımı — modeller 13, 14 ve 15 aynı kopyayı okur.

Üç aşama tek bir tezin parçasıdır: *"kurulum çalışmaya başlayınca riski sıfırla, bir
kısmını cebe koy, kalanı koştur ama kazandığının çoğunu geri verme."*

    1. breakeven_at_r     — bu R'a ULAŞILDIĞINDA stop girişe çekilir (risk sıfırlanır)
    2. partial_tp         — bu R'da pozisyonun `fraction` kadarı kapanır ve stop aynı
                            anda o seviyeye çekilir (kâr kilitlenir)
    3. trail_giveback_pct — kısmi çıkıştan SONRA stop, en iyi kazancın en çok bu oranını
                            geri verecek yerde durur ve orijinal hedefi asla aşmaz

**Neden tek modül.** Üç model bu kuralı ölçüyor:

    model 13 (`vwap_clone`)    dış sistemin kendi kuralı, kopyalanıyor
    model 14 (`vwap_managed`)  aynı yönetim, ev kurallarıyla (risk boyutlandırma)
    model 15 (`scalp_managed`) scalp_fixed'in ikizi, TEK farkı bu yönetim

Model 15 ile `scalp_fixed` arasındaki ortalama R farkının "çıkış yönetiminin katkısı"
olarak okunabilmesi, yönetimin tek bir yerde tanımlı olmasına bağlıdır. Üç dosyaya
kopyalansaydı, bir gün birinin `partial_tp.r` değeri sessizce kayar ve model 15'in farkı
"yönetimin katkısı" olmaktan çıkıp "iki ayrı yönetimin farkı" hâline gelirdi — tam da
`strategies/scalp/arms.py`'nin beş kolu tek kopyada tutma gerekçesi.

Uygulama BURADA DEĞİLDİR (kural 9): bu modül yalnızca config'i okur ve `Signal`
alanlarına çevirir. Stop hareketlerini `core/engine.py`, kısmi dolumu
`core/portfolio.py` uygular.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from core.config import get_setting
from strategies.base import PartialTakeProfit

CONFIG_PREFIX = "exit_management"


@dataclass(frozen=True, kw_only=True)
class ExitManagement:
    """`config.yaml > exit_management` bloğunun yazılabilir olmayan karşılığı."""

    breakeven_at_r: float
    partial_r: float
    partial_fraction: float
    trail_giveback_pct: float

    @classmethod
    def from_config(cls, settings: Mapping[str, Any]) -> "ExitManagement":
        config = dict(settings)
        return cls(
            breakeven_at_r=float(get_setting(config, f"{CONFIG_PREFIX}.breakeven_at_r")),
            partial_r=float(get_setting(config, f"{CONFIG_PREFIX}.partial_tp.r")),
            partial_fraction=float(get_setting(config, f"{CONFIG_PREFIX}.partial_tp.fraction")),
            trail_giveback_pct=float(get_setting(config, f"{CONFIG_PREFIX}.trail_giveback_pct")),
        )

    def signal_fields(self) -> dict[str, Any]:
        """`Signal(**fields)` içine doğrudan açılan alanlar.

        Sözlük olarak dönmesi bilinçli: üç alanı üç modelde elle yazmak, birinin bir gün
        yalnızca ikisini yazması demekti — ve eksik alan sessizce "o aşama kapalı" anlamına
        geldiği için hiçbir test bunu yakalamazdı.
        """
        return {
            "breakeven_at_r": self.breakeven_at_r,
            "partial_tp": PartialTakeProfit(r=self.partial_r, fraction=self.partial_fraction),
            "trail_giveback_pct": self.trail_giveback_pct,
        }

    def describe(self) -> str:
        """Deftere yazılan serbest metin özeti — denetim izi kuralı satırdan okunabilsin."""
        return (
            f"yönetim: breakeven {self.breakeven_at_r:g}R, kısmi "
            f"%{self.partial_fraction * 100:g} @ {self.partial_r:g}R, "
            f"takip geri verme %{self.trail_giveback_pct * 100:g}"
        )
