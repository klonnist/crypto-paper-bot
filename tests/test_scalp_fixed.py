"""Model 12: eşit ağırlık, öğrenme yok — model 11'in null hipotezi.

Bu dosyanın ölçtüğü şey bir YOKLUK: modelin geçmişe erişimi olmadığı ve kol seçiminin
hiçbir tahminden beslenmediği. Null hipotezin değeri tam olarak buradan gelir; bir gün
buraya bir filtre ya da ağırlık eklenirse model 11'in farkı neye karşı ölçtüğü bilinmez
hâle gelir ve bu testler o eklemeyi yakalar.
"""

from __future__ import annotations

import math
import random
from collections import Counter
from typing import Any

import pytest

from core.config import load_config
from core.layers import resolve_layer
from strategies.base import Strategy
from strategies.scalp.arms import ARM_NAMES
from strategies.scalp_bandit import ScalpBandit
from strategies.scalp_fixed import ScalpFixed

ARMS = list(ARM_NAMES)


@pytest.fixture()
def config() -> dict[str, Any]:
    return resolve_layer(load_config(), "scalp").config


def test_arms_are_drawn_uniformly(config: dict[str, Any]) -> None:
    model = ScalpFixed(config=config)
    rng = random.Random(4)

    picks = Counter(model.choose_arm(ARMS, rng=rng)[0] for _ in range(5000))

    for arm in ARMS:
        assert picks[arm] == pytest.approx(1000, rel=0.15), arm


def test_allocation_never_shifts_with_results(config: dict[str, Any]) -> None:
    """Model 12 öğrenmez: aynı tohum, geçmişten bağımsız olarak aynı diziyi verir."""
    model = ScalpFixed(config=config)

    first = [model.choose_arm(ARMS, rng=random.Random(f"t{i}"))[0] for i in range(30)]
    second = [model.choose_arm(ARMS, rng=random.Random(f"t{i}"))[0] for i in range(30)]

    assert first == second


def test_model_does_not_implement_the_history_hook() -> None:
    """Kanca uygulanmadığı için motor defteri hiç okutmaz: "öğrenmiyor" koddan denetlenebilir."""
    assert ScalpFixed.observe_closed_trades is Strategy.observe_closed_trades
    assert ScalpBandit.observe_closed_trades is not Strategy.observe_closed_trades


def test_posterior_is_nan_not_zero(config: dict[str, Any]) -> None:
    """`0.0` "ölçtüm, sıfır çıktı" demektir; bu modelde ölçülen bir şey yok."""
    _, posterior = ScalpFixed(config=config).choose_arm(ARMS, rng=random.Random(1))

    assert math.isnan(posterior)


def test_only_available_arms_are_drawn(config: dict[str, Any]) -> None:
    model = ScalpFixed(config=config)
    available = ARMS[1:3]

    picks = {model.choose_arm(available, rng=random.Random(seed))[0] for seed in range(40)}

    assert picks <= set(available)


def test_empty_arm_list_raises(config: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        ScalpFixed(config=config).choose_arm([], rng=random.Random(0))


def test_two_models_draw_independently(config: dict[str, Any]) -> None:
    """Aynı turda aynı seçimi yapsalardı, fark adaptasyonun değil tesadüfün ölçüsü olurdu.

    Bağımsızlık `ScalpModel._round_rng` üzerinden gelir: tohum model ADIYLA karışır.
    """
    from tests.helpers_market import frame, market

    bandit, fixed = ScalpBandit(config=config), ScalpFixed(config=config)
    snapshot = market({"BTC-USDT-SWAP": frame([100.0] * 60, freq="15min")})

    assert bandit._round_rng(snapshot).random() != fixed._round_rng(snapshot).random()


def test_same_round_reproduces_the_same_draw(config: dict[str, Any]) -> None:
    """Aynı `as_of` ile yeniden koşulan tur birebir aynı çekilişi görmeli."""
    from tests.helpers_market import frame, market

    model = ScalpFixed(config=config)
    snapshot = market({"BTC-USDT-SWAP": frame([100.0] * 60, freq="15min")})

    assert model._round_rng(snapshot).random() == model._round_rng(snapshot).random()
