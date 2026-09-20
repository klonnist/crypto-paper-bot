"""Model 15: `scalp_fixed`in ikizi, TEK farkı üç aşamalı çıkış yönetimi.

Bu dosyanın çivilediği şey bir EŞİTLİK: iki modelin kol seçimi, kapıları, geometrisi ve
zaman stop'u birebir aynı olmalı. Eşitlik bozulduğu an aradaki ortalama R farkı "çıkış
yönetiminin katkısı" olmaktan çıkar ve iki ayrı modelin farkı hâline gelir — testler o
bozulmayı yakalamak için var.
"""

from __future__ import annotations

import random
from typing import Any

import pandas as pd
import pytest

from core.config import load_config
from core.layers import resolve_layer
from core.tags import parse_tag
from strategies.base import MarketData, Position, Strategy
from strategies.scalp.arms import ARM_NAMES
from strategies.scalp_fixed import ScalpFixed
from strategies.scalp_managed import ScalpManaged
from tests.helpers_market import frame, market

ARMS = list(ARM_NAMES)
START = pd.Timestamp("2026-01-02 00:00:00", tz="UTC")


@pytest.fixture()
def config() -> dict[str, Any]:
    return resolve_layer(load_config(), "scalp").config


def _market() -> MarketData:
    """Momentum patlaması kolunu tetikleyen sentetik gün: 3 yükselen bar + hacim teyidi.

    Kurulum bilinçli olarak GEÇERLİ bir kurulumdur (stop tabanı ve 1.5R kapısından geçer):
    ölçülen şey kapılar değil, iki modelin aynı kurulumu aynı turda seçip seçmediği.
    """
    flat = [100.0 + (index % 3) * 0.05 for index in range(80)]
    closes = flat + [flat[-1] + 1.5 * (step + 1) for step in range(3)]
    volumes = [1.0] * 80 + [10.0] * 3
    frames = {
        symbol: frame(closes, spread=0.1, freq="15min", start=START, volumes=volumes)
        for symbol in ("BTC-USDT-SWAP", "ETH-USDT-SWAP")
    }
    return market(frames)


# --------------------------------------------------------------------------- #
# Eşitlik: aynı seçim, aynı geometri
# --------------------------------------------------------------------------- #
def test_arm_selection_is_inherited_not_reimplemented() -> None:
    """Kol kuralının ikinci bir kopyası, "aynı çekiliş" iddiasını denetlenemez kılardı."""
    assert ScalpManaged.choose_arm is ScalpFixed.choose_arm


def test_the_twin_shares_the_control_models_draw_identity(config: dict[str, Any]) -> None:
    """Eşleştirilmiş deney: aynı turda aynı kol ve aynı sembol seçilmeli."""
    assert ScalpManaged(config=config).rng_identity == ScalpFixed.name
    assert ScalpFixed(config=config).rng_identity is None  # kendi adını kullanır


def test_both_models_pick_the_same_setup_in_the_same_round(config: dict[str, Any]) -> None:
    data = _market()
    fixed = ScalpFixed(config=config).generate_signals(data)
    managed = ScalpManaged(config=config).generate_signals(data)

    assert len(fixed) == len(managed) == 1
    assert fixed[0].symbol == managed[0].symbol
    assert fixed[0].direction == managed[0].direction
    assert fixed[0].stop_price == pytest.approx(managed[0].stop_price)
    assert fixed[0].take_profits == managed[0].take_profits
    assert parse_tag(fixed[0].reason, "arm") == parse_tag(managed[0].reason, "arm")


def test_the_draw_identity_actually_changes_the_pick(config: dict[str, Any]) -> None:
    """Kimlik paylaşımı bir tesadüf değil: farklı kimlik farklı dizi üretir."""
    model = ScalpManaged(config=config)
    shared = [model.choose_arm(ARMS, rng=random.Random(f"{ScalpFixed.name}:{i}"))[0]
              for i in range(40)]
    other = [model.choose_arm(ARMS, rng=random.Random(f"{ScalpManaged.name}:{i}"))[0]
             for i in range(40)]

    assert shared != other


def test_the_twin_does_not_learn() -> None:
    """Öğrenme bu eksende ölçülmüyor: kanca uygulanmaz, defter hiç okunmaz."""
    assert ScalpManaged.observe_closed_trades is Strategy.observe_closed_trades


# --------------------------------------------------------------------------- #
# Fark: yalnızca çıkış yönetimi
# --------------------------------------------------------------------------- #
def test_only_the_exit_management_differs(config: dict[str, Any]) -> None:
    data = _market()
    fixed = ScalpFixed(config=config).generate_signals(data)[0]
    managed = ScalpManaged(config=config).generate_signals(data)[0]

    assert fixed.breakeven_at_r is None
    assert fixed.partial_tp is None
    assert fixed.trail_giveback_pct is None

    assert managed.breakeven_at_r == pytest.approx(1.0)
    assert managed.partial_tp is not None
    assert managed.partial_tp.r == pytest.approx(1.5)
    assert managed.partial_tp.fraction == pytest.approx(0.5)
    assert managed.trail_giveback_pct == pytest.approx(0.5)


def test_the_twin_reads_the_same_exit_block_as_the_vwap_models(
    config: dict[str, Any]
) -> None:
    """Tek kopya sözleşmesi: üç model aynı config bloğunu okur."""
    from strategies.vwap_managed import VwapManaged

    assert ScalpManaged(config=config).exit_management == VwapManaged(config=config)._exit


def test_the_management_is_written_into_the_ledger_reason(config: dict[str, Any]) -> None:
    """Denetim izi: hangi satırın hangi kuralla kapandığı defterden okunabilmeli."""
    managed = ScalpManaged(config=config).generate_signals(_market())[0]

    assert "yönetim:" in managed.reason
    assert "breakeven" in managed.reason


def test_the_twin_keeps_the_house_time_stop(config: dict[str, Any]) -> None:
    """Zaman stop'u ev kuralıdır ve iki modelde de aynı kalır."""
    data = _market()
    # 5 saat = 20 bar > 16 barlık zaman stop'u: iki model de kapatmalı.
    aged = Position(
        symbol="BTC-USDT-SWAP",
        direction="long",
        entry_price=100.0,
        stop_price=99.0,
        opened_at=data.as_of - pd.Timedelta(hours=5),
    )

    fixed = ScalpFixed(config=config).manage_positions(data, [aged])
    managed = ScalpManaged(config=config).manage_positions(data, [aged])

    assert [item.reason for item in fixed] == [item.reason for item in managed]
    assert fixed and fixed[0].action == "close"


def test_existing_models_are_untouched(config: dict[str, Any]) -> None:
    """Model 11 ve 12 bu eklemeden etkilenmez: yönetim alanları kapalı kalır."""
    from strategies.scalp_bandit import ScalpBandit

    assert ScalpFixed(config=config).exit_management is None
    assert ScalpBandit(config=config).exit_management is None
