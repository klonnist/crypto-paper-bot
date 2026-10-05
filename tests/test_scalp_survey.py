"""`ScalpModel.take_survey`: kol × sebep sayımı bir DENETİM İZİDİR (kural 15, karar 48'in açık işi).

Üç şey ölçülür: (a) anahtar şeması SABİTTİR (sıfırlar dâhil — bir kolun raporda
bulunmaması "sıfır" ile "ölçülmedi"yi aynı hücreye yazardı), (b) sayımlar iç tutarlıdır
(her kolda sebeplerin toplamı incelenen sembol sayısı, `gecti` iki alt sayacın toplamı) ve
(c) sayım sinyal dizisini ve çekiliş akışını ASLA değiştirmez.
"""

from __future__ import annotations

import random
from typing import Any, Sequence

import pytest

from core.config import load_config
from core.layers import resolve_layer
from strategies.base import MarketData, Signal
from strategies.scalp import model as scalp_model
from strategies.scalp.arms import ARM_NAMES, ArmSetup
from strategies.scalp.model import SURVEY_REASONS, SURVEY_TARGETS, survey_key, survey_keys
from strategies.scalp_fixed import ScalpFixed
from tests.helpers_market import flat, frame, market

SYMBOLS = ["BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP", "XRP-USDT-SWAP", "ADA-USDT-SWAP"]


@pytest.fixture()
def config() -> dict[str, Any]:
    return resolve_layer(load_config(), "scalp").config


def _market(symbols: Sequence[str] = SYMBOLS) -> MarketData:
    return market({symbol: frame(flat(80), freq="15min") for symbol in symbols})


def _setup(
    arm: str,
    symbol: str,
    *,
    stop: float,
    target: float,
    direction: str = "long",
) -> ArmSetup:
    return ArmSetup(
        arm=arm,
        symbol=symbol,
        direction=direction,  # type: ignore[arg-type]
        entry_price=100.0,
        stop_price=stop,
        target_price=target,
        detail="test",
    )


# Giriş 100; stop%1 tabanı ve 1.5R kapısı config'ten (scalp.min_stop_pct / min_reward_risk),
# projeksiyon = 100 + 2.0 × stop mesafesi.
def _fails_stop_floor(arm: str, symbol: str) -> ArmSetup:
    return _setup(arm, symbol, stop=99.5, target=101.0)          # stop %0.5 < %1


def _fails_reward_risk(arm: str, symbol: str) -> ArmSetup:
    return _setup(arm, symbol, stop=97.0, target=102.0)          # R:R 0.67 < 1.5


def _passes_projection(arm: str, symbol: str) -> ArmSetup:
    return _setup(arm, symbol, stop=97.0, target=106.0)          # hedef = projeksiyon (2.0R)


def _passes_obstacle(arm: str, symbol: str) -> ArmSetup:
    return _setup(arm, symbol, stop=97.0, target=105.0)          # engel yakın: R:R 1.67


def _patched(
    monkeypatch: pytest.MonkeyPatch, proposals: dict[str, list[ArmSetup]]
) -> None:
    complete = {arm: proposals.get(arm, []) for arm in ARM_NAMES}
    monkeypatch.setattr(scalp_model, "propose_all", lambda market, params: complete)


def test_the_key_schema_is_every_arm_times_every_reason() -> None:
    expected = {
        survey_key(arm, reason)
        for arm in ARM_NAMES
        for reason in (*SURVEY_REASONS, *SURVEY_TARGETS)
    }

    assert set(survey_keys()) == expected
    assert len(survey_keys()) == len(ARM_NAMES) * (len(SURVEY_REASONS) + len(SURVEY_TARGETS))


def test_there_is_no_survey_before_the_first_scan(config: dict[str, Any]) -> None:
    assert ScalpFixed(config=config).take_survey() is None


def test_the_schema_is_fixed_even_when_no_arm_sees_anything(
    config: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sıfırlar da raporda durur: eksik anahtar "ölçülmedi" demek olurdu."""
    _patched(monkeypatch, {})
    model = ScalpFixed(config=config)

    assert model.generate_signals(_market()) == []
    survey = model.take_survey()

    assert survey is not None and set(survey) == set(survey_keys())
    for arm in ARM_NAMES:
        assert survey[survey_key(arm, "kurulum_yok")] == len(SYMBOLS)
        assert all(
            survey[survey_key(arm, reason)] == 0
            for reason in (*SURVEY_REASONS[1:], *SURVEY_TARGETS)
        ), arm


def test_each_gate_outcome_lands_in_its_own_bucket(
    config: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    arm = ARM_NAMES[0]
    _patched(
        monkeypatch,
        {
            arm: [
                _fails_stop_floor(arm, SYMBOLS[0]),
                _fails_reward_risk(arm, SYMBOLS[1]),
                _passes_projection(arm, SYMBOLS[2]),
                _passes_obstacle(arm, SYMBOLS[3]),
            ]
        },
    )
    model = ScalpFixed(config=config)
    model.generate_signals(_market())
    survey = model.take_survey()

    assert survey is not None
    assert survey[survey_key(arm, "stop_tabani")] == 1
    assert survey[survey_key(arm, "rr_kapisi")] == 1
    assert survey[survey_key(arm, "rejim")] == 0
    assert survey[survey_key(arm, "gecti")] == 2
    assert survey[survey_key(arm, "engel_geride_veya_uzak")] == 1
    assert survey[survey_key(arm, "engel_onde")] == 1
    assert survey[survey_key(arm, "kurulum_yok")] == len(SYMBOLS) - 4


def test_the_stop_floor_is_checked_before_the_reward_risk_gate(
    config: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """İkisini de ihlal eden kurulum TEK bir sebebe yazılır (sıra: stop tabanı, sonra R:R)."""
    arm = ARM_NAMES[0]
    both = _setup(arm, SYMBOLS[0], stop=99.9, target=100.05)   # stop %0.1 ve R:R 0.5
    _patched(monkeypatch, {arm: [both]})
    model = ScalpFixed(config=config)
    model.generate_signals(_market())
    survey = model.take_survey()

    assert survey is not None
    assert survey[survey_key(arm, "stop_tabani")] == 1
    assert survey[survey_key(arm, "rr_kapisi")] == 0


def test_the_regime_filter_gets_its_own_bucket(
    config: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`rejim` = ev kapılarından GEÇİP ek rejim kapısında elenenler; `gecti` = hepsinden geçenler."""

    class Filtered(ScalpFixed):
        name = "scalp_filtered"

        def regime_filter(self, setups: Sequence[ArmSetup], market: MarketData) -> list[ArmSetup]:
            return [setup for setup in setups if setup.symbol != SYMBOLS[0]]

    arm = ARM_NAMES[1]
    _patched(
        monkeypatch,
        {arm: [_passes_projection(arm, SYMBOLS[0]), _passes_projection(arm, SYMBOLS[1])]},
    )
    model = Filtered(config=config)
    model.generate_signals(_market())
    survey = model.take_survey()

    assert survey is not None
    assert survey[survey_key(arm, "rejim")] == 1
    assert survey[survey_key(arm, "gecti")] == 1
    assert survey[survey_key(arm, "engel_geride_veya_uzak")] == 1


def test_reasons_sum_to_the_examined_symbols_for_every_arm(
    config: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Σ(kurulum_yok, stop_tabani, rr_kapisi, rejim, gecti) = incelenen sembol; ayrık sayım."""
    _patched(
        monkeypatch,
        {
            ARM_NAMES[0]: [_fails_stop_floor(ARM_NAMES[0], SYMBOLS[0]),
                           _passes_obstacle(ARM_NAMES[0], SYMBOLS[1])],
            ARM_NAMES[2]: [_fails_reward_risk(ARM_NAMES[2], s) for s in SYMBOLS],
            ARM_NAMES[3]: [_passes_projection(ARM_NAMES[3], s) for s in SYMBOLS[:3]],
        },
    )
    model = ScalpFixed(config=config)
    model.generate_signals(_market())
    survey = model.take_survey()

    assert survey is not None
    for arm in ARM_NAMES:
        total = sum(survey[survey_key(arm, reason)] for reason in SURVEY_REASONS)
        assert total == len(SYMBOLS), arm
        targets = sum(survey[survey_key(arm, target)] for target in SURVEY_TARGETS)
        assert targets == survey[survey_key(arm, "gecti")], arm


def test_symbols_without_data_are_not_counted_as_examined(
    config: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`symbol_views` verisi olmayan sembolü düşürür; sayım da onu incelenmiş saymaz."""
    _patched(monkeypatch, {})
    data = market({
        SYMBOLS[0]: frame(flat(80), freq="15min"),
        SYMBOLS[1]: frame(flat(80), freq="15min").iloc[:-1],   # as_of barı yok
    }, btc=frame(flat(80), freq="15min"))
    model = ScalpFixed(config=config)
    model.generate_signals(data)
    survey = model.take_survey()

    assert survey is not None
    assert survey[survey_key(ARM_NAMES[0], "kurulum_yok")] == 1


def test_take_survey_returns_a_copy(config: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    _patched(monkeypatch, {})
    model = ScalpFixed(config=config)
    model.generate_signals(_market())

    first = model.take_survey()
    assert first is not None
    first[survey_key(ARM_NAMES[0], "gecti")] = 999

    second = model.take_survey()
    assert second is not None and second[survey_key(ARM_NAMES[0], "gecti")] == 0


def test_a_new_scan_replaces_the_previous_survey(
    config: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sayım BAR başınadır (motor her bardan sonra okuyup toplar); birikmez."""
    arm = ARM_NAMES[0]
    model = ScalpFixed(config=config)
    _patched(monkeypatch, {arm: [_passes_projection(arm, SYMBOLS[0])]})
    model.generate_signals(_market())
    _patched(monkeypatch, {})
    model.generate_signals(_market())

    survey = model.take_survey()
    assert survey is not None and survey[survey_key(arm, "gecti")] == 0


def _run(model: ScalpFixed, data: MarketData) -> tuple[list[Signal], tuple[Any, ...]]:
    """Sinyaller VE çekiliş akışının bıraktığı durum (aynı tohum → aynı durum)."""
    captured: list[random.Random] = []
    original = model._round_rng

    def spy(snapshot: MarketData) -> random.Random:
        rng = original(snapshot)
        captured.append(rng)
        return rng

    model._round_rng = spy  # type: ignore[method-assign]
    signals = model.generate_signals(data)
    return signals, (captured[0].getstate() if captured else ())


def test_the_survey_never_changes_the_signals_or_the_draw(
    config: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sözleşme: sayım sinyalleri, sıralarını ve RNG akışını HİÇBİR biçimde etkilemez.

    Sayım kapatılmış (`_tally` boş sözlük döndürür) ve açık modelin ürettiği sinyaller ile
    çekiliş sonrası RNG durumu birebir aynı olmalı — aksi hâlde denetim izi ölçülen şeyi
    değiştiriyor demektir.
    """
    proposals = {
        ARM_NAMES[0]: [_passes_projection(ARM_NAMES[0], s) for s in SYMBOLS[:3]],
        ARM_NAMES[1]: [_passes_obstacle(ARM_NAMES[1], SYMBOLS[3]),
                       _fails_reward_risk(ARM_NAMES[1], SYMBOLS[4])],
        ARM_NAMES[2]: [_passes_projection(ARM_NAMES[2], SYMBOLS[4])],
    }
    _patched(monkeypatch, proposals)
    data = _market()

    with_survey = _run(ScalpFixed(config=config), data)
    monkeypatch.setattr(ScalpFixed, "_tally", lambda self, *args, **kwargs: {})
    without_survey = _run(ScalpFixed(config=config), data)

    assert with_survey[0] and with_survey[0] == without_survey[0]
    assert with_survey[1] == without_survey[1]


def test_real_arms_feed_the_survey_end_to_end(config: dict[str, Any]) -> None:
    """Sahte kol yok: gerçek `propose_all` ile de şema, toplamlar ve alt sayaç değişmezi tutar."""
    closes = [100.0 + (0.05 if i % 2 else -0.05) for i in range(78)] + [99.0, 98.5]
    data = market({
        SYMBOLS[0]: frame(closes, spread=0.3, freq="15min"),
        SYMBOLS[1]: frame(closes, spread=0.3, freq="15min"),
    })
    model = ScalpFixed(config=config)
    model.generate_signals(data)
    survey = model.take_survey()

    assert survey is not None and set(survey) == set(survey_keys())
    rsi = "rsi2_reversal"
    # Aralık rejiminde iki sembolde de RSI dönüşü kurulur; Bollinger orta bandı girişin ÖNÜNDE
    # ve stop 5×ATR uzakta olduğu için hedef/stop 1.5'in altında kalır → R:R kapısı eler.
    assert survey[survey_key(rsi, "kurulum_yok")] == 0
    assert survey[survey_key(rsi, "rr_kapisi")] == 2
    assert survey[survey_key(rsi, "gecti")] == 0
    for arm in ARM_NAMES:
        assert sum(survey[survey_key(arm, r)] for r in SURVEY_REASONS) == 2, arm
