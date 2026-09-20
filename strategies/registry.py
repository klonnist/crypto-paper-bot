"""Model adı -> strateji kurucusu eşlemesi. `config.yaml`'ın `models` listesi buradan çözülür.

Neden ayrı bir kayıt defteri: `config.yaml` yarışmaya giren kümeyi tanımlar (kural 6), ama
bir YAML dosyası sınıf örnekleyemez. Alternatif, main.py'nin modül adını dinamik import
etmesiydi; o yol config'e yazılan herhangi bir dizenin kod çalıştırabilmesi demekti ve
"hangi 10 model yarışıyor" sorusunun cevabını grep'lenemez hâle getirirdi.

Yeni bir model eklemek iki satırdır: sınıfı import et, `REGISTRY`ye adıyla yaz. Ad,
sınıfın `name` alanıyla birebir aynı olmalıdır — defter klasörü o addan türer.
"""

from __future__ import annotations

import inspect
from typing import Any, Callable, Mapping

from core.validate import validate_model
from strategies.avwap import Avwap
from strategies.base import Strategy
from strategies.buyhold import BuyHold
from strategies.confluence import Confluence
from strategies.downtrend_rally import DowntrendRally
from strategies.ema_trend import EmaTrend
from strategies.ensemble import Ensemble
from strategies.failed_breakout import FailedBreakout
from strategies.meanrev import MeanReversion
from strategies.momentum import Momentum
from strategies.random_ctrl import RandomControl
from strategies.scalp_bandit import ScalpBandit
from strategies.scalp_fixed import ScalpFixed
from strategies.scalp_managed import ScalpManaged
from strategies.scalp_patient import ScalpPatient
from strategies.scalp_vol import ScalpVol
from strategies.squeeze import Squeeze
from strategies.trend import Trend
from strategies.vwap_clone import VwapClone
from strategies.vwap_managed import VwapManaged

StrategyFactory = Callable[[], Strategy]

REGISTRY: Mapping[str, StrategyFactory] = {
    BuyHold.name: BuyHold,
    Trend.name: Trend,
    MeanReversion.name: MeanReversion,
    Momentum.name: Momentum,
    Squeeze.name: Squeeze,
    Confluence.name: Confluence,
    FailedBreakout.name: FailedBreakout,
    DowntrendRally.name: DowntrendRally,
    Avwap.name: Avwap,
    Ensemble.name: Ensemble,
    RandomControl.name: RandomControl,
    # ema_trend — `ema` katmanının (4H, sabit 13 sembol) ölçtüğü model. Kuralları dış bir
    # sistemden gelir ama KOPYA değildir (kural 15b): dışarıdan gelen yalnızca sinyal,
    # boyutlandırma ve maliyet evin. Ön-kayıt: docs/backtest.md > 6d.
    EmaTrend.name: EmaTrend,
    # 15 dakikalık scalp katmanı (config.yaml > layers.scalp). Kayıt defteri katmandan
    # bağımsızdır: hangi modelin hangi turda koşacağını katmanın `models` listesi söyler.
    ScalpBandit.name: ScalpBandit,
    ScalpFixed.name: ScalpFixed,
    # Scalp katmanının çıkış yönetimi kanadı (modeller 13-15):
    #   vwap_clone    — dış sistem KOPYASI (is_replica), sabit teminat × 10x
    #   vwap_managed  — aynı sinyal, EV kurallarıyla (risk boyutlandırma, %1 taban, 1.5R)
    #   scalp_managed — scalp_fixed'in ikizi, tek farkı üç aşamalı çıkış yönetimi
    ScalpManaged.name: ScalpManaged,
    # scalp_patient — scalp_fixed'in ikizi, tek farkı zaman stop'unun SINIRI (16 ↔ 100).
    # Katmanın `models` listesinde YOKTUR: canlıya alınmadan önce taze bir OOS penceresinde
    # doğrulanmalı (docs/backtest.md > 4, C-5). Backtest onu `--models` ile çağırır.
    ScalpPatient.name: ScalpPatient,
    # scalp_vol — scalp_patient'in ikizi, tek farkı KESİTSEL volatilite rejimi kapısı.
    # Katmanın `models` listesinde YOKTUR: önce taze bir OOS penceresinde ölçülür.
    ScalpVol.name: ScalpVol,
    VwapClone.name: VwapClone,
    VwapManaged.name: VwapManaged,
}


class UnknownModelError(KeyError):
    """config.yaml'da kayıtlı olmayan bir model adı geçiyor."""


def build(name: str, *, config: Mapping[str, Any] | None = None) -> Strategy:
    """Adı verilen modeli örnekler; ad tanınmıyorsa UnknownModelError.

    Sessizce atlamak, yazım hatası yüzünden aylarca eksik yarışan bir küme demekti —
    tam da kural 6'nın ("izin verilen küme tüm modeller için aynı") engellemek istediği şey.

    `config` KATMANIN çözülmüş ayarlarıdır (core/layers.py) ve ayarı okuyan her modele
    verilir. Verilmezse model `load_config()` ile KÖK ayarları okur — 15 dakikalık katmanda
    bu, modelin 4 saatlik bar süresini görmesi ve zaman stop'unu 4 saat yerine 64 saat
    sanması demekti. Kurucusunda `config` parametresi olmayan model, config'ten hiçbir şey
    okumayan modeldir (ör. alım-tut çıpasının sabit ağırlıkları); bir gün okumaya
    başlarsa parametreyi kurucusuna eklemek zorundadır.
    """
    factory = REGISTRY.get(name)
    if factory is None:
        raise UnknownModelError(
            f"{name!r} strategies/registry.py'de kayıtlı değil (kayıtlı: {sorted(REGISTRY)})"
        )
    strategy = factory(config=config) if config is not None and _accepts_config(factory) else factory()
    if strategy.name != name:
        raise ValueError(
            f"kayıt adı ({name!r}) ile strateji adı ({strategy.name!r}) uyuşmuyor: "
            "defter klasörü strateji adından türer, ikisi ayrışırsa defter kaybolur"
        )
    # Bayrak/limit bildiriminin kapısı (core/validate.py): kaldıraç tavanı ve
    # "ModelLimits yalnızca kopya modellere açıktır" kuralı KURULUMDA denetlenir. Sinyal
    # kapısında denetlemek modeli aylarca "bu turda sinyal üretmedi" gibi gösterirdi;
    # burada patlayan model main.py tarafından atlanır ve koşu hata koduyla biter.
    validate_model(strategy)
    return strategy


def _accepts_config(factory: StrategyFactory) -> bool:
    """Kurucu `config` anahtar argümanını kabul ediyor mu?"""
    try:
        return "config" in inspect.signature(factory).parameters
    except (TypeError, ValueError):  # C düzeyinde kurucu: imza okunamaz
        return False
