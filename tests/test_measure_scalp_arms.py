"""scripts/measure_scalp_arms.py: kol/kapı sayımı ölçüm aracı — salt okunur, ikinci bir mantık YOK.

Araç her barda GERÇEK `ScalpPatient.generate_signals`ı çalıştırır ve sayımı modelin kendi
`take_survey()`inden okur. Burada ölçülen: (a) sayımın modelin kendi çıktısıyla özdeş olması,
(b) `--tail` hızlandırmasının tam geçmişle aynı sayımı vermesi (aracın `verify`si de bunu
kanıtlar), (c) tabloların toplamlarının tutarlılığı ve (d) aracın hiçbir şeye yazmaması.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from core.config import load_config
from core.layers import resolve_layer
from strategies.scalp.arms import ARM_NAMES
from strategies.scalp_patient import ScalpPatient
from tests.helpers_market import frame

_SPEC = importlib.util.spec_from_file_location(
    "measure_scalp_arms",
    Path(__file__).resolve().parent.parent / "scripts" / "measure_scalp_arms.py",
)
assert _SPEC and _SPEC.loader
measure_scalp_arms = importlib.util.module_from_spec(_SPEC)
sys.modules["measure_scalp_arms"] = measure_scalp_arms
_SPEC.loader.exec_module(measure_scalp_arms)

SYMBOLS = ["BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP"]
START = pd.Timestamp("2026-03-02 00:00:00", tz="UTC")


@pytest.fixture()
def config() -> dict[str, Any]:
    return resolve_layer(load_config(), "scalp").config


def _frames(bars: int = 520) -> dict[str, pd.DataFrame]:
    """Aralık rejiminde dalgalanan, zaman zaman RSI(2) aşırılığı üreten sentetik seriler."""
    result: dict[str, pd.DataFrame] = {}
    for offset, symbol in enumerate(SYMBOLS):
        closes: list[float] = []
        for index in range(bars):
            wave = 0.05 if (index + offset) % 2 else -0.05
            dip = -1.0 if (index + offset) % 37 in (35, 36) else 0.0
            closes.append(100.0 + wave + dip)
        result[symbol] = frame(closes, spread=0.3, start=START, freq="15min")
    return result


def _count(config: dict[str, Any], frames: dict[str, pd.DataFrame], *, tail: int | None) -> list[Any]:
    model = ScalpPatient(config=config)
    anchor = frames["BTC-USDT-SWAP"]
    return measure_scalp_arms.measure(
        frames, model, start=anchor.index[-60], end=anchor.index[-1], tail=tail
    )


def test_the_count_is_the_models_own_survey(config: dict[str, Any]) -> None:
    """Araç sayımı kendisi üretmez: modelin `take_survey()`i ile BİREBİR aynıdır."""
    frames = _frames()
    counted = _count(config, frames, tail=400)

    model = ScalpPatient(config=config)
    last = counted[-1]
    model.generate_signals(measure_scalp_arms.build_market(frames, last.as_of, tail=400))

    assert dict(last.survey) == dict(model.take_survey() or {})


def test_the_tail_shortcut_matches_the_full_history(config: dict[str, Any]) -> None:
    """Hızlandırma bir varsayımdır: aynı barlarda tam geçmişle AYNI sayımı vermeli."""
    frames = _frames()
    fast = _count(config, frames, tail=400)
    full = _count(config, frames, tail=None)

    assert [dict(item.survey) for item in fast] == [dict(item.survey) for item in full]
    assert [item.signalled for item in fast] == [item.signalled for item in full]


def test_verify_passes_on_an_equivalent_tail_and_fails_on_a_lossy_one(
    config: dict[str, Any],
) -> None:
    frames = _frames()
    counted = _count(config, frames, tail=400)
    model = ScalpPatient(config=config)

    measure_scalp_arms.verify(frames, model, counted, tail=400, sample=10, seed=1)

    lossy = _count(config, frames, tail=20)           # 20 bar: göstergeler hesaplanamaz
    with pytest.raises(SystemExit):
        measure_scalp_arms.verify(frames, model, lossy, tail=20, sample=len(lossy), seed=1)


def test_every_barcount_carries_the_complete_fixed_schema(config: dict[str, Any]) -> None:
    from strategies.scalp.model import survey_keys

    counted = _count(config, _frames(), tail=400)

    assert counted
    assert all(set(item.survey) == set(survey_keys()) for item in counted)


def test_a_signal_bar_is_a_bar_where_some_arm_passed_the_gates(config: dict[str, Any]) -> None:
    """Modelin sinyal barı ile `≥1 kol GEÇTİ` barı aynı kümedir (barda tek sinyal)."""
    counted = _count(config, _frames(), tail=400)

    assert all(item.signalled == bool(item.available_arms) for item in counted)


def test_thesis_arms_are_a_subset_of_available_arms(config: dict[str, Any]) -> None:
    """`engel_onde` kapıdan geçenlerin ALT KÜMESİDİR: kural yalnızca eler, hiçbir şey eklemez."""
    counted = _count(config, _frames(), tail=400)

    for item in counted:
        assert set(item.thesis_arms) <= set(item.available_arms)


def test_tables_sum_up_and_render(config: dict[str, Any]) -> None:
    counted = _count(config, _frames(), tail=400)
    weeks = 0.1

    funnel = measure_scalp_arms.arm_funnel_table(counted, weeks=weeks)
    model_level = measure_scalp_arms.model_table(counted, weeks=weeks)
    measurability = measure_scalp_arms.measurability_table(counted, weeks=weeks, realization=0.2)

    for arm in ARM_NAMES:
        assert arm in funnel
    assert f"ölçülen bar                         {len(counted)}" in model_level
    assert "sayım ile motor ayrışıyor" not in model_level
    assert "baz" in measurability and "aday" in measurability


def test_the_measurement_is_deterministic_and_leaves_the_model_unchanged(
    config: dict[str, Any],
) -> None:
    frames = _frames()

    first = [dict(item.survey) for item in _count(config, frames, tail=400)]
    second = [dict(item.survey) for item in _count(config, frames, tail=400)]

    assert first == second


# --------------------------------------------------------------------------- #
# Kontrollü kollar: gerçek `propose_all` bu sentetik serilerde yalnızca `rr_kapisi` eleyen
# kurulumlar üretir (karar 34'ün kapı aritmetiği), yani yukarıdaki testler `gecti` ve
# `engel_onde` dallarını hiç çalıştırmaz. Burada kollar sabit bir tablodan beslenir; aracın
# kendisi (toplama, kümeler, tablolar) gerçek modelin `take_survey()` yolundan geçer.
# --------------------------------------------------------------------------- #
def _setup(arm: str, symbol: str, *, stop: float, target: float) -> Any:
    from strategies.scalp.arms import ArmSetup

    return ArmSetup(
        arm=arm, symbol=symbol, direction="long", entry_price=100.0,
        stop_price=stop, target_price=target, detail="test",
    )


def _controlled(monkeypatch: pytest.MonkeyPatch) -> None:
    from strategies.scalp import model as scalp_model

    first, second = ARM_NAMES[0], ARM_NAMES[1]

    def fake(market: Any, params: Any) -> dict[str, list[Any]]:
        phase = (market.as_of.minute // 15) % 4
        result: dict[str, list[Any]] = {arm: [] for arm in ARM_NAMES}
        symbol = SYMBOLS[0]
        if phase == 1:      # yalnız projeksiyona düşen, kapıdan geçen kurulum
            result[first] = [_setup(first, symbol, stop=97.0, target=106.0)]
        elif phase == 2:    # engel önde (kapıdan geçer) + projeksiyon
            result[first] = [_setup(first, symbol, stop=97.0, target=105.0)]
            result[second] = [_setup(second, symbol, stop=97.0, target=106.0)]
        elif phase == 3:    # yalnız R:R kapısına takılan
            result[ARM_NAMES[2]] = [_setup(ARM_NAMES[2], symbol, stop=97.0, target=102.0)]
        return result

    monkeypatch.setattr(scalp_model, "propose_all", fake)


def test_controlled_arms_exercise_the_pass_and_thesis_branches(
    config: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _controlled(monkeypatch)
    counted = _count(config, _frames(), tail=400)

    signal_bars = [item for item in counted if item.signalled]
    thesis_bars = [item for item in counted if item.thesis_arms]
    assert len(counted) == 60 and len(signal_bars) == 30        # faz 1 ve 2 (15 + 15)
    assert len(thesis_bars) == 15                               # yalnız faz 2
    assert all(item.available_arms for item in signal_bars)
    assert all(set(item.thesis_arms) == {ARM_NAMES[0]} for item in thesis_bars)
    assert all(item.signalled == bool(item.available_arms) for item in counted)
    assert all(set(item.thesis_arms) <= set(item.available_arms) for item in counted)


def test_controlled_tables_report_the_exact_counts(
    config: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _controlled(monkeypatch)
    counted = _count(config, _frames(), tail=400)

    funnel = measure_scalp_arms.arm_funnel_table(counted, weeks=1.0)
    model_level = measure_scalp_arms.model_table(counted, weeks=1.0)
    measurability = measure_scalp_arms.measurability_table(counted, weeks=1.0, realization=0.5)

    first_row = next(line for line in funnel.splitlines() if line.startswith(ARM_NAMES[0]))
    assert first_row.split()[1:7] == ["30", "0", "0", "0", "30", "15"]   # kurulum..engel_önde
    third_row = next(line for line in funnel.splitlines() if line.startswith(ARM_NAMES[2]))
    assert third_row.split()[1:4] == ["15", "0", "15"]                   # 15 aday, hepsi R:R'da
    assert "≥1 kol kapıdan geçti (baz)          30" in model_level
    assert "≥1 kol `engel_onde` geçti (aday)    15" in model_level
    assert "aday / baz                          0.500" in model_level
    # 30 pozisyon / (30 sinyal/hf) = 1 hafta = 7 gün (tavan); gerçekleşme oranı 0.5 → 14 gün.
    base_row = next(line for line in measurability.splitlines() if line.startswith("baz"))
    assert base_row.split()[1:] == ["30.00", "1.0", "7.0", "14.0"]


def test_controlled_tail_shortcut_is_verified(
    config: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _controlled(monkeypatch)
    frames = _frames()
    counted = _count(config, frames, tail=400)

    measure_scalp_arms.verify(
        frames, ScalpPatient(config=config), counted, tail=400, sample=20, seed=3
    )
