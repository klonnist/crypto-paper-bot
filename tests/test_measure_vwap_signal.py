"""scripts/measure_vwap_signal.py `--reentry`: "dönüş bandın İÇİNE kapanmalı" varyantının frekansı.

Ölçülen şey varyantın baz kümenin ALT KÜMESİ olmasıdır (şart yalnızca eler) ve aracın
`verify`sinin bunu GERÇEK `vwap_signal.scan()` çıktısına süzgeç uygulayarak kanıtlamasıdır.
Araç getiri/R'ye bakmaz: yalnızca kurulum ve kapı sayılarını verir.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pandas as pd
import pytest

from tests.helpers_market import frame
from tests.test_vwap_models import SPREAD, START, SYMBOL, _reverting

_SPEC = importlib.util.spec_from_file_location(
    "measure_vwap_signal",
    Path(__file__).resolve().parent.parent / "scripts" / "measure_vwap_signal.py",
)
assert _SPEC and _SPEC.loader
mvs = importlib.util.module_from_spec(_SPEC)
sys.modules["measure_vwap_signal"] = mvs
_SPEC.loader.exec_module(mvs)

BAND = 2.0


def _point(z_prev: float, z_now: float) -> mvs.BarPoint:
    return mvs.BarPoint(
        symbol=SYMBOL, as_of=START, close=100.0, atr=1.0, vwap=101.0,
        deviation=1.0, z_prev=z_prev, z_now=z_now,
    )


@pytest.mark.parametrize(
    ("z_prev", "z_now", "base", "reentry"),
    [
        (-2.5, -1.9, ("long", mvs.PASSED), ("long", mvs.PASSED)),            # bandın İÇİNE döndü
        (-2.5, -2.3, ("long", mvs.PASSED), (None, mvs.OUTSIDE_AFTER_RETURN)),  # dönüş var, hâlâ dışında
        (2.5, 1.9, ("short", mvs.PASSED), ("short", mvs.PASSED)),
        (2.5, 2.3, ("short", mvs.PASSED), (None, mvs.OUTSIDE_AFTER_RETURN)),
        (-2.5, -3.0, (None, mvs.STILL_EXTENDING), (None, mvs.STILL_EXTENDING)),  # dönüş yok
        (-2.5, 0.2, (None, mvs.CROSSED), (None, mvs.CROSSED)),                 # VWAP geçildi
        (-1.0, -0.5, (None, mvs.INSIDE_BAND), (None, mvs.INSIDE_BAND)),        # bant içi
    ],
)
def test_the_variant_only_ever_removes_candidates(
    z_prev: float, z_now: float, base: tuple, reentry: tuple
) -> None:
    point = _point(z_prev, z_now)

    assert mvs.arm_verdict(point, band_mult=BAND) == base
    assert mvs.arm_verdict(point, band_mult=BAND, reentry=True) == reentry


def test_a_band_exactly_at_the_threshold_is_outside() -> None:
    """`|z_now| < band_mult` KESİN eşitsizliktir: tam eşikteki dönüş bandın içinde sayılmaz."""
    point = _point(2.6, 2.0)

    assert mvs.arm_verdict(point, band_mult=BAND, reentry=True) == (None, mvs.OUTSIDE_AFTER_RETURN)


def test_reentry_candidates_are_a_subset_of_the_base_candidates() -> None:
    points = [_point(z_prev, z_now) for z_prev in (-3.1, -2.4, 2.2, 2.9, 1.0)
              for z_now in (-2.8, -2.0, -1.2, 0.3, 1.5, 2.1, 2.5)]

    base = {id(item.point) for item in mvs.candidates_of(points, band_mult=BAND)}
    variant = {id(item.point) for item in mvs.candidates_of(points, band_mult=BAND, reentry=True)}

    assert variant <= base and variant != base


def _frames(**shape: float) -> dict[str, pd.DataFrame]:
    return {SYMBOL: frame(_reverting(**shape), spread=SPREAD, freq="15min", start=START)}


def test_verify_proves_the_variant_against_the_real_scan() -> None:
    frames = _frames()
    points = mvs.measure(frames, atr_period=14, min_vwap_bars=8, start=START)
    assert points

    mvs.verify(
        frames, points,
        atr_period=14, min_vwap_bars=8, band_mult=BAND, sample=len(points), seed=1,
    )


def test_verify_fails_when_the_fast_path_disagrees(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hızlı yolun varyantı gerçek `scan()` süzgecinden ayrışırsa script hata koduyla biter.

    `rebound=1.0` fikstürü dönüşü bandın İÇİNE kapatır (|z_now| < band): varyantı sessizce
    boşaltmak bu yüzden gerçek bir ayrışmadır. Varsayılan fikstürde dönüş hâlâ bant dışında
    kaldığı için aynı kırılma görünmezdi.
    """
    frames = _frames(rebound=1.0)
    points = mvs.measure(frames, atr_period=14, min_vwap_bars=8, start=START)
    original = mvs.candidates_of

    def broken(points_, *, band_mult, reentry=False):  # noqa: ANN001
        found = original(points_, band_mult=band_mult, reentry=reentry)
        return [] if reentry else found          # varyantı sessizce boşalt

    monkeypatch.setattr(mvs, "candidates_of", broken)
    with pytest.raises(SystemExit):
        mvs.verify(
            frames, points,
            atr_period=14, min_vwap_bars=8, band_mult=BAND, sample=len(points), seed=1,
        )


def test_the_variant_table_renders_base_and_variant_rows() -> None:
    points = mvs.measure(_frames(), atr_period=14, min_vwap_bars=8, start=START)

    table = mvs.reentry_table(
        points, [2.5],
        band_mult=BAND, target_reward_risk=2.0, min_stop_pct=0.01, min_reward_risk=1.5,
        weeks=1.0,
    )

    assert "TABLO 4" in table
    assert any(line.split()[1:2] == ["baz"] for line in table.splitlines()[2:])
    assert any(line.split()[1:2] == ["varyant"] for line in table.splitlines()[2:])
