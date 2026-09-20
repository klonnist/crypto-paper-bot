"""Model 11'in öğrenme kuralları: ısınma, taban tahsis, kayan pencere, posterior'ın kaynağı.

Bu dosyanın ölçtüğü şey "bandit kâr ediyor mu" değil — ölçülemez ve projenin sorusu da o
değil. Ölçtüğü şey, tahsisin BEKLENEN kurallara uyup uymadığı: ısınma bitmeden öğrenmediği,
hiçbir kolu susturmadığı, posterior'ını yalnızca KAPANMIŞ işlemlerden kurduğu ve aynı
defterle aynı sonucu tekrar ürettiği.
"""

from __future__ import annotations

import math
import random
from collections import Counter
from typing import Any, Sequence

import pandas as pd
import pytest

from core.config import load_config
from core.layers import resolve_layer
from core.tags import TagError, format_tags
from strategies.base import ClosedTrade
from strategies.scalp.arms import ARM_NAMES
from strategies.scalp_bandit import ScalpBandit

ARMS = list(ARM_NAMES)
START = pd.Timestamp("2026-03-02 00:00:00", tz="UTC")


@pytest.fixture()
def config() -> dict[str, Any]:
    return resolve_layer(load_config(), "scalp").config


def _trade(arm: str, r: float | None, *, index: int = 0, symbol: str = "BTC-USDT-SWAP") -> ClosedTrade:
    return ClosedTrade(
        symbol=symbol,
        direction="long",
        opened_at=START + pd.Timedelta(minutes=15 * index),
        closed_at=START + pd.Timedelta(minutes=15 * (index + 1)),
        r_multiple=r,
        signal_reason=format_tags("kurulum", arm=arm, post_r=0.0),
        exit_reason="target",
    )


def _history(per_arm: dict[str, Sequence[float]]) -> list[ClosedTrade]:
    trades: list[ClosedTrade] = []
    for arm, values in per_arm.items():
        for offset, value in enumerate(values):
            trades.append(_trade(arm, value, index=len(trades) + offset))
    return trades


def _warm(r_by_arm: dict[str, float], *, count: int = 20) -> list[ClosedTrade]:
    """Her kola `count` işlem: ısınmayı bitiren asgari geçmiş."""
    return _history({arm: [value] * count for arm, value in r_by_arm.items()})


# --------------------------------------------------------------------------- #
# Isınma
# --------------------------------------------------------------------------- #
def test_warmup_keeps_every_arm_equally_weighted(config: dict[str, Any]) -> None:
    """İlk 20 işleme kadar seçim eşit çekiliştir: 2-3 işlemlik gürültü tercih üretmemeli."""
    model = ScalpBandit(config=config)
    # Bir kol açık ara önde ama henüz ısınmamış: öğrenme BAŞLAMAMALI.
    model.observe_closed_trades(_history({ARMS[0]: [5.0] * 19, ARMS[1]: [-1.0] * 19}))

    rng = random.Random(11)
    picks = Counter(model.choose_arm(ARMS, rng=rng)[0] for _ in range(2000))

    assert set(picks) == set(ARMS)
    for arm in ARMS:
        assert picks[arm] == pytest.approx(2000 / len(ARMS), rel=0.2), arm


def test_warmup_ends_only_when_every_arm_is_measured(config: dict[str, Any]) -> None:
    """Tek bir kol bile 20'ye ulaşmadıysa öğrenme başlamaz: az örneklemli kol denenmeye devam."""
    model = ScalpBandit(config=config)
    history = _warm({arm: 1.0 for arm in ARMS[:-1]})
    history += _history({ARMS[-1]: [1.0] * 19})
    model.observe_closed_trades(history)

    # RNG bir kez kurulur: her çağrıda yeniden tohumlamak aynı çekilişi 1000 kez yapardı.
    rng = random.Random(3)
    picks = Counter(model.choose_arm(ARMS, rng=rng)[0] for _ in range(1000))

    assert picks[ARMS[-1]] == pytest.approx(200, rel=0.3)


def test_learning_starts_after_warmup(config: dict[str, Any]) -> None:
    """Isınma bitince açık ara iyi olan kol belirgin biçimde öne geçer."""
    model = ScalpBandit(config=config)
    rewards = {arm: -0.5 for arm in ARMS}
    rewards[ARMS[2]] = 1.5
    model.observe_closed_trades(_warm(rewards))

    rng = random.Random(5)
    picks = Counter(model.choose_arm(ARMS, rng=rng)[0] for _ in range(1000))

    assert picks[ARMS[2]] > 700


# --------------------------------------------------------------------------- #
# Taban tahsis
# --------------------------------------------------------------------------- #
def test_floor_allocation_keeps_the_worst_arm_alive(config: dict[str, Any]) -> None:
    """Susturulan kol bir daha ÖLÇÜLEMEZ: en kötü kol bile %5 tahsisin altına düşmez."""
    model = ScalpBandit(config=config)
    rewards = {arm: 2.0 for arm in ARMS}
    rewards[ARMS[4]] = -2.0  # felaket kol
    model.observe_closed_trades(_warm(rewards))

    rounds = 4000
    rng = random.Random(17)
    picks = Counter(model.choose_arm(ARMS, rng=rng)[0] for _ in range(rounds))

    floor = float(config["scalp"]["bandit"]["min_allocation"])
    # Taban bir OLASILIKTIR, bir kota değil: 4000 turda %5'in etrafında dalgalanır
    # (3σ ≈ %1). Test payı o dalgalanmayı soğurur, ama sıfıra düşmeyi yakalar.
    assert picks[ARMS[4]] / rounds >= floor * 0.7


def test_floor_applies_only_to_arms_with_setups(config: dict[str, Any]) -> None:
    """O turda kurulum üretmemiş kola pay ayırmak turu boşa harcamak olurdu."""
    model = ScalpBandit(config=config)
    model.observe_closed_trades(_warm({arm: 0.5 for arm in ARMS}))

    available = ARMS[:2]
    picks = {model.choose_arm(available, rng=random.Random(seed))[0] for seed in range(50)}

    assert picks <= set(available)


# --------------------------------------------------------------------------- #
# Posterior'ın kaynağı: YALNIZCA kapanmış işlemler
# --------------------------------------------------------------------------- #
def test_posterior_only_sees_what_it_is_given(config: dict[str, Any]) -> None:
    """Açık pozisyon posterior'a giremez: model YALNIZCA kapanmış işlem listesi görür.

    İzolasyon yapısaldır — `core/engine.py` listeyi defterin KAPANMIŞ satırlarından
    kurar (bkz. tests/test_engine_history.py); model tarafında ise açık pozisyonu
    okuyabileceği bir yüzey hiç yoktur.
    """
    model = ScalpBandit(config=config)
    model.observe_closed_trades(_history({ARMS[0]: [1.0, 2.0]}))

    posterior = model.posteriors()[ARMS[0]]

    assert posterior.trades == 2
    assert posterior.mean_r == pytest.approx(1.5)
    assert not hasattr(model, "positions")


def test_posterior_is_rebuilt_from_scratch_every_round(config: dict[str, Any]) -> None:
    """Artımlı sayaç, defterle modelin hafızasının ayrışmasına kapı açardı."""
    model = ScalpBandit(config=config)
    model.observe_closed_trades(_history({ARMS[0]: [1.0, 1.0, 1.0]}))
    model.observe_closed_trades(_history({ARMS[0]: [1.0]}))  # defter küçüldü (elle düzeltme)

    assert model.posteriors()[ARMS[0]].trades == 1


def test_posterior_ignores_rows_without_risk(config: dict[str, Any]) -> None:
    """R'si olmayan satır ortalamaya giremez: metrics de o satırı R'ye katmaz."""
    model = ScalpBandit(config=config)
    model.observe_closed_trades([_trade(ARMS[0], 1.0), _trade(ARMS[0], None, index=2)])

    assert model.posteriors()[ARMS[0]].trades == 1


def test_missing_arm_tag_raises_instead_of_being_skipped(config: dict[str, Any]) -> None:
    """Etiketsiz satırı atlamak, posterior'ı defterde görünmeyen bir geçmişe bağlardı."""
    model = ScalpBandit(config=config)
    orphan = ClosedTrade(
        symbol="BTC-USDT-SWAP",
        direction="long",
        opened_at=START,
        closed_at=START + pd.Timedelta(minutes=15),
        r_multiple=1.0,
        signal_reason="kol etiketi olmayan eski satır",
        exit_reason="target",
    )

    with pytest.raises(TagError):
        model.observe_closed_trades([orphan])


def test_sliding_window_drops_the_oldest_trades(config: dict[str, Any]) -> None:
    """Kayan pencere: piyasa rejimi değişir, iki yıl önceki ortalama bugünü belirlememeli."""
    window = int(config["scalp"]["bandit"]["window_trades"])
    model = ScalpBandit(config=config)
    model.observe_closed_trades(_history({ARMS[0]: [-1.0] * window + [1.0] * window}))

    posterior = model.posteriors()[ARMS[0]]

    assert posterior.trades == window
    assert posterior.mean_r == pytest.approx(1.0)  # eski yarı tamamen düştü


# --------------------------------------------------------------------------- #
# Tekrar üretilebilirlik ve denetim izi
# --------------------------------------------------------------------------- #
def test_same_history_and_seed_reproduce_the_same_choice(config: dict[str, Any]) -> None:
    """Durum ayrı bir dosyada değil, defterin saf bir fonksiyonudur: aynı defter = aynı karar."""
    history = _warm({arm: 0.3 for arm in ARMS})
    first, second = ScalpBandit(config=config), ScalpBandit(config=config)
    first.observe_closed_trades(history)
    second.observe_closed_trades(history)

    picks_a = [first.choose_arm(ARMS, rng=random.Random(f"seed:{i}"))[0] for i in range(50)]
    picks_b = [second.choose_arm(ARMS, rng=random.Random(f"seed:{i}"))[0] for i in range(50)]

    assert picks_a == picks_b


def test_choice_reports_the_posterior_of_the_chosen_arm(config: dict[str, Any]) -> None:
    """`post_r` etiketi deftere yazılır: geçmiş her karar sonradan geri okunabilsin."""
    model = ScalpBandit(config=config)
    model.observe_closed_trades(_warm({arm: 0.75 for arm in ARMS}))

    arm, posterior = model.choose_arm(ARMS, rng=random.Random(1))

    assert posterior == pytest.approx(model.posteriors()[arm].mean_r)
    assert posterior == pytest.approx(0.75)


def test_unmeasured_arm_reports_nan_not_zero(config: dict[str, Any]) -> None:
    """`0.0` "ölçtüm, sıfır çıktı" demektir; ölçülmemiş kolun posterior'ı nan'dır."""
    model = ScalpBandit(config=config)

    _, posterior = model.choose_arm(ARMS, rng=random.Random(2))

    assert math.isnan(posterior)


def test_state_summary_lists_every_arm(config: dict[str, Any]) -> None:
    """Tur logu durumun tamamını yazar: hangi kolun kaç işlemle nerede durduğu denetlenebilir."""
    model = ScalpBandit(config=config)
    model.observe_closed_trades(_history({ARMS[0]: [1.0]}))

    summary = model.state_summary()

    assert all(arm in summary for arm in ARMS)
    assert "n=1" in summary
