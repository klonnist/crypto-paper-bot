"""Model 16: `scalp_fixed`in ikizi, TEK farkı zaman stop'unun SINIRI (16 ↔ 100 bar).

`tests/test_scalp_managed.py` ile aynı iş: bir EŞİTLİĞİ çiviler. İki modelin kol seçimi,
kapıları ve stop/hedef geometrisi birebir aynı olmalı; ayrışan tek şey SÜREdir. Eşitlik
bozulduğu an aradaki ortalama R farkı "kuruluma hedefine varacak süreyi vermenin katkısı"
olmaktan çıkar (karar 30) ve iki ayrı modelin farkı hâline gelir.
"""

from __future__ import annotations

from typing import Any

import pandas as pd
import pytest

from core.config import load_config
from core.layers import resolve_layer
from core.tags import parse_tag
from strategies.scalp_fixed import ScalpFixed
from strategies.scalp_patient import ScalpPatient
from tests.helpers_market import frame, market
from strategies.base import MarketData

START = pd.Timestamp("2026-01-02 00:00:00", tz="UTC")


@pytest.fixture()
def config() -> dict[str, Any]:
    return resolve_layer(load_config(), "scalp").config


def _market() -> MarketData:
    flat = [100.0 + (index % 3) * 0.05 for index in range(80)]
    closes = flat + [flat[-1] + 1.5 * (step + 1) for step in range(3)]
    volumes = [1.0] * 80 + [10.0] * 3
    frames = {
        symbol: frame(closes, spread=0.1, freq="15min", start=START, volumes=volumes)
        for symbol in ("BTC-USDT-SWAP", "ETH-USDT-SWAP")
    }
    return market(frames)


# --------------------------------------------------------------------------- #
# Ayrışan TEK şey: süre
# --------------------------------------------------------------------------- #
def test_only_the_time_stop_limit_differs(config: dict[str, Any]) -> None:
    """Eksenin tanımı. 100 = (hedef/ATR)² = 10², teoriden gelir (karar 30)."""
    assert ScalpFixed(config=config)._time_stop.bars == 16
    assert ScalpPatient(config=config)._time_stop.bars == 100


def test_the_time_stop_rule_stays_a_single_copy() -> None:
    """Kuralı kopyalamak, farkı "iki ayrı zaman stop'u uygulamasının farkı" yapardı.

    Ayrışan yalnızca DEĞERİN okunduğu config anahtarıdır; `TimeStop`ın kendisi ortaktır.
    """
    from strategies.time_stop import CONFIG_KEY

    assert ScalpFixed.time_stop_key == CONFIG_KEY
    assert ScalpPatient.time_stop_key != CONFIG_KEY
    assert ScalpPatient.manage_positions is ScalpFixed.manage_positions


def test_arm_selection_is_inherited_not_reimplemented() -> None:
    assert ScalpPatient.choose_arm is ScalpFixed.choose_arm


def test_the_twin_shares_the_control_models_draw_identity(config: dict[str, Any]) -> None:
    """Eşleştirilmiş deney: aynı turda aynı kol ve aynı sembol seçilmeli."""
    assert ScalpPatient(config=config).rng_identity == ScalpFixed.name


def test_both_models_pick_the_same_setup_in_the_same_round(config: dict[str, Any]) -> None:
    """Sinyaller AYNI; ayrışma ancak pozisyonun ne kadar taşındığında başlar."""
    data = _market()
    fixed = ScalpFixed(config=config).generate_signals(data)
    patient = ScalpPatient(config=config).generate_signals(data)

    assert len(fixed) == len(patient) == 1
    assert fixed[0].symbol == patient[0].symbol
    assert fixed[0].direction == patient[0].direction
    assert fixed[0].stop_price == pytest.approx(patient[0].stop_price)
    assert fixed[0].take_profits == patient[0].take_profits
    assert parse_tag(fixed[0].reason, "arm") == parse_tag(patient[0].reason, "arm")


def test_no_exit_management_is_inherited(config: dict[str, Any]) -> None:
    """Üç aşamalı yönetim KAPALI kalmalı: açılsaydı eksene ikinci bir değişken girerdi."""
    assert ScalpPatient(config=config).exit_management is None


# --------------------------------------------------------------------------- #
# Kâğıt katmanında ölçülür — bu "canlıya alma eşiği geçildi" DEMEK DEĞİLDİR
# --------------------------------------------------------------------------- #
def test_the_candidate_is_measured_in_the_paper_layer() -> None:
    """Karar 33'ten sonra aday katmanın `models` listesindedir.

    Ayrım önemli: `scalp` katmanı KÂĞIT ölçümdür, gerçek para değil.
    `docs/backtest.md > 4`ün canlıya alma eşiği gerçek parayla işlem açmayı düzenler ve
    `scalp_patient` onu GEÇMEDİ (C-1: OOS ortalama R −0.01, > 0 değil — karar 32). Kâğıt
    katmanında koşması, ileriye dönük kanıt biriktirmesinin tek yoludur; eşiği geçmiş
    sayılması değil.

    Test listede DURDUĞUNU çiviler ki ileride kazara düşürülmesi sessiz kalmasın —
    düşerse `scalp_fixed ↔ scalp_patient` ekseni (hareket eden TEK eksen) ölçülmez olur.
    """
    layer = resolve_layer(load_config(), "scalp")
    assert ScalpPatient.name in layer.models
    assert ScalpFixed.name in layer.models, "eksenin diğer ucu da durmalı"


def test_the_retired_axes_are_gone_from_the_layer() -> None:
    """Karar 33: iki kez "fark yok" demiş eksenler kapatıldı.

    Kodları ve defterleri duruyor (kural 1); ölçülmeyi bırakan şey yalnızca canlı turdur.
    """
    models = resolve_layer(load_config(), "scalp").models
    assert "scalp_bandit" not in models
    assert "scalp_managed" not in models
