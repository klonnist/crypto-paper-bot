"""Zaman stop'unun TEK tanımı — modeller 11, 12, 14 ve 15 aynı kopyayı okur.

**Tez.** Scalp tezleri saatler ölçeğindedir. `time_stop_bars` bar sonra hâlâ ne stop'a ne
hedefe değmiş bir pozisyon, tezin ölçtüğü hareketin gerçekleşmediğinin kanıtıdır ve
sermayeyi (ve marjı) tutmaya devam etmesinin bir gerekçesi yoktur.

**Neden tek modül.** `strategies/exit_management.py` ile birebir aynı gerekçe: kuralı
okuyan model sayısı birden fazladır ve modeller arası fark ancak kural TEK KOPYA olduğunda
bir eksenin ölçüsü olur. Beş kollu modeller (11, 12, 15) bu kuralı `ScalpModel` gövdesinden
alıyordu; model 14 (`vwap_managed`) o gövdeden türemediği için kuralı HİÇ almıyordu. İki
uygulamaya bölmek yerine kural buraya çıkarıldı: aksi hâlde `model 14 ↔ scalp_fixed`
ekseninde "16 bar sonra kapanır mı" sessiz bir dördüncü değişken olarak kalırdı.

**Neden model 14'te de gerekli.** %1 stop tabanı ile kural 11 birlikte notional'ı
sermayenin tamamına yaklaştırır, yani pratikte aynı anda ~1 pozisyon taşınır. Zaman stop'u
olmadan tek bir takılı pozisyon, zaten seyrek olan sinyal akışını süresiz bloklar ve model
`acceptance.min_trades` örneklem kapısına hiç ulaşamaz — ölçülemeyen bir model, ölçüm
projesinde bir modelin en kötü hâlidir.

**Model 13'e UYGULANMAZ** (`vwap_clone`): kaynak sistemde zaman stop'u yoktur ve eklemek
kopyayı kopya olmaktan çıkarırdı (kural 15b).

Uygulama BURADA DEĞİLDİR (kural 10): bu modül yalnızca config'i okur ve bir
`ExitInstruction` üretir. Kapanışın dolumu bir SONRAKİ barın açılışındadır (kural 13),
yani pozisyonun gerçek ömrü `bars + 1` bardır; "16 bar" kararın verildiği bardır.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

import pandas as pd

from core.config import get_setting
from core.data import bar_duration
from core.tags import format_tags
from strategies.base import ExitInstruction, MarketData, Position

CONFIG_KEY = "scalp.time_stop_bars"

# Çıkışın ALT sebebi (kural 13c). `exit_reason` yalnızca "signal" der; zaman stop'u ile
# başka bir strateji çıkışı aynı satıra çökerse yönetimin katkısı defterden okunamaz.
EXIT_RULE = "time_stop"


@dataclass(frozen=True, kw_only=True)
class TimeStop:
    """`config.yaml > scalp.time_stop_bars` ve katmanın bar süresinin birleşimi."""

    bars: int
    duration: pd.Timedelta

    @classmethod
    def from_config(
        cls, settings: Mapping[str, Any], *, key: str = CONFIG_KEY
    ) -> "TimeStop":
        """`key` DEĞERİN nereden okunacağını söyler; KURAL yine tek kopyadır.

        Bir modelin başka bir sınır kullanması (bkz. `strategies/scalp_patient.py`) bu
        modülü ikiye bölmeyi gerektirmez: zaman stop'unun ne YAPTIĞI ortak kalır, yalnızca
        kaç bar olduğu ayrışır — ve ayrışan tek şey zaten ölçülmek istenen eksendir. Kuralı
        kopyalamak, `scalp_fixed ↔ scalp_patient` farkını "iki ayrı zaman stop'u
        uygulamasının farkı" hâline getirirdi.
        """
        config = dict(settings)
        return cls(
            bars=int(get_setting(config, key)),
            # Bar süresi katmanın `timeframe`inden gelir: aynı "16 bar" scalp katmanında
            # 4 saat, base katmanında 64 saat demektir ve kural sayı olarak değil BAR
            # olarak tanımlıdır.
            duration=bar_duration(str(get_setting(config, "timeframe"))),
        )

    def instructions(
        self, market: MarketData, positions: Sequence[Position]
    ) -> list[ExitInstruction]:
        """Süresi dolmuş pozisyonlar için piyasa fiyatından kapanış talimatı."""
        deadline = self.duration * self.bars
        instructions: list[ExitInstruction] = []
        for position in positions:
            age = market.as_of - pd.Timestamp(position.opened_at)
            if age < deadline:
                continue
            instructions.append(
                ExitInstruction(
                    symbol=position.symbol,
                    action="close",
                    reason=format_tags(
                        f"zaman stop'u: pozisyon {int(age / self.duration)} bardır açık "
                        f"({self.bars} bar sınırı), piyasa fiyatından kapatılıyor",
                        exit_rule=EXIT_RULE,
                    ),
                )
            )
        return instructions

    def describe(self) -> str:
        """Deftere yazılan serbest metin özeti — kural satırdan okunabilsin."""
        return f"zaman stop'u {self.bars} bar"
