import pytest

from core.validate import REPLICA_LEVERAGE_CAP, validate_model, validate_signal
from strategies.base import (
    ModelLimits,
    PartialTakeProfit,
    Signal,
    Strategy,
    TakeProfit,
)

UNIVERSE = ["BTC-USDT-SWAP", "ETH-USDT-SWAP"]


def _signal(**overrides: object) -> Signal:
    defaults: dict[str, object] = dict(symbol="BTC-USDT-SWAP", direction="long", stop_price=90.0)
    defaults.update(overrides)
    return Signal(**defaults)  # type: ignore[arg-type]


def test_valid_long_signal_passes() -> None:
    validate_signal(
        _signal(),
        entry_price=100.0,
        allowed_directions=["long"],
        symbol_universe=UNIVERSE,
    )


def test_limit_entry_type_not_implemented() -> None:
    with pytest.raises(NotImplementedError):
        validate_signal(
            _signal(entry_type="limit"),
            entry_price=100.0,
            allowed_directions=["long"],
            symbol_universe=UNIVERSE,
        )


def test_symbol_outside_universe_rejected() -> None:
    with pytest.raises(ValueError, match="sembol evreninde"):
        validate_signal(
            _signal(symbol="DOGE-USDT-SWAP"),
            entry_price=100.0,
            allowed_directions=["long"],
            symbol_universe=UNIVERSE,
        )


def test_direction_not_allowed_rejected() -> None:
    with pytest.raises(ValueError, match="izinli değil"):
        validate_signal(
            _signal(direction="short", stop_price=110.0),
            entry_price=100.0,
            allowed_directions=["long"],
            symbol_universe=UNIVERSE,
        )


def test_stop_equal_entry_rejected() -> None:
    with pytest.raises(ValueError, match="sıfıra bölme"):
        validate_signal(
            _signal(stop_price=100.0),
            entry_price=100.0,
            allowed_directions=["long"],
            symbol_universe=UNIVERSE,
        )


def test_long_stop_on_wrong_side_rejected() -> None:
    with pytest.raises(ValueError, match="altında olmalı"):
        validate_signal(
            _signal(stop_price=110.0),
            entry_price=100.0,
            allowed_directions=["long"],
            symbol_universe=UNIVERSE,
        )


def test_short_stop_on_wrong_side_rejected() -> None:
    with pytest.raises(ValueError, match="üzerinde olmalı"):
        validate_signal(
            _signal(direction="short", stop_price=90.0),
            entry_price=100.0,
            allowed_directions=["short"],
            symbol_universe=UNIVERSE,
        )


def test_take_profit_on_wrong_side_rejected() -> None:
    with pytest.raises(ValueError, match="giriş fiyatının üzerinde olmalı"):
        validate_signal(
            _signal(take_profits=(TakeProfit(price=95.0, fraction=1.0),)),
            entry_price=100.0,
            allowed_directions=["long"],
            symbol_universe=UNIVERSE,
        )


def test_take_profit_fraction_sum_over_one_rejected() -> None:
    with pytest.raises(ValueError, match="fraction toplamı"):
        validate_signal(
            _signal(
                take_profits=(
                    TakeProfit(price=110.0, fraction=0.6),
                    TakeProfit(price=120.0, fraction=0.6),
                )
            ),
            entry_price=100.0,
            allowed_directions=["long"],
            symbol_universe=UNIVERSE,
        )


# --------------------------------------------------------------------------- #
# Boyutlandırma modu kapısı (CLAUDE.md kural 15)
# --------------------------------------------------------------------------- #
def _benchmark_signal(**overrides: object) -> Signal:
    defaults: dict[str, object] = dict(
        symbol="BTC-USDT-SWAP",
        direction="long",
        sizing="notional_fraction",
        notional_fraction=0.5,
    )
    defaults.update(overrides)
    return Signal(**defaults)  # type: ignore[arg-type]


def test_notional_fraction_allowed_for_benchmark() -> None:
    validate_signal(
        _benchmark_signal(),
        entry_price=100.0,
        allowed_directions=["long"],
        symbol_universe=UNIVERSE,
        is_benchmark=True,
    )


def test_notional_fraction_rejected_for_competitor() -> None:
    """Yarışmacı modelin kendi boyutunu belirlemesi kural 3/11'i deler."""
    with pytest.raises(ValueError, match="is_benchmark=True"):
        validate_signal(
            _benchmark_signal(),
            entry_price=100.0,
            allowed_directions=["long"],
            symbol_universe=UNIVERSE,
            is_benchmark=False,
        )


def test_is_benchmark_defaults_to_restrictive() -> None:
    """Varsayılan, muafiyeti KAPALI olandır: çağıran unutursa kural sıkı tarafa düşer."""
    with pytest.raises(ValueError, match="is_benchmark=True"):
        validate_signal(
            _benchmark_signal(),
            entry_price=100.0,
            allowed_directions=["long"],
            symbol_universe=UNIVERSE,
        )


def test_notional_fraction_with_stop_price_rejected() -> None:
    """Dolu stop sessizce yok sayılmaz: risk_amount ve cost_per_r ondan türer."""
    with pytest.raises(ValueError, match="stop_price None olmalıdır"):
        validate_signal(
            _benchmark_signal(stop_price=90.0),
            entry_price=100.0,
            allowed_directions=["long"],
            symbol_universe=UNIVERSE,
            is_benchmark=True,
        )


def test_notional_fraction_requires_fraction() -> None:
    with pytest.raises(ValueError, match="notional_fraction zorunludur"):
        validate_signal(
            _benchmark_signal(notional_fraction=None),
            entry_price=100.0,
            allowed_directions=["long"],
            symbol_universe=UNIVERSE,
            is_benchmark=True,
        )


@pytest.mark.parametrize("fraction", [0.0, -0.5, 1.5])
def test_notional_fraction_out_of_range_rejected(fraction: float) -> None:
    with pytest.raises(ValueError, match="0 ile 1.0 arasında"):
        validate_signal(
            _benchmark_signal(notional_fraction=fraction),
            entry_price=100.0,
            allowed_directions=["long"],
            symbol_universe=UNIVERSE,
            is_benchmark=True,
        )


def test_risk_sizing_requires_stop_price() -> None:
    with pytest.raises(ValueError, match="stop_price zorunludur"):
        validate_signal(
            _signal(stop_price=None),
            entry_price=100.0,
            allowed_directions=["long"],
            symbol_universe=UNIVERSE,
        )


def test_risk_sizing_rejects_notional_fraction() -> None:
    """Muafiyet bir referans modelde bile modlar arası karışmaya açılmaz."""
    with pytest.raises(ValueError, match="notional_fraction dolu olamaz"):
        validate_signal(
            _signal(notional_fraction=0.5),
            entry_price=100.0,
            allowed_directions=["long"],
            symbol_universe=UNIVERSE,
            is_benchmark=True,
        )


def test_unknown_sizing_mode_rejected() -> None:
    with pytest.raises(ValueError, match="bilinmeyen sizing modu"):
        validate_signal(
            _signal(sizing="kelly"),
            entry_price=100.0,
            allowed_directions=["long"],
            symbol_universe=UNIVERSE,
            is_benchmark=True,
        )


def test_benchmark_still_bound_by_allowed_directions() -> None:
    """Muafiyet YALNIZCA boyutlandırmadadır; yön kuralı referans modelde de geçerli."""
    with pytest.raises(ValueError, match="izinli değil"):
        validate_signal(
            _benchmark_signal(direction="short"),
            entry_price=100.0,
            allowed_directions=["long"],
            symbol_universe=UNIVERSE,
            is_benchmark=True,
        )


def test_benchmark_still_bound_by_universe() -> None:
    with pytest.raises(ValueError, match="sembol evreninde"):
        validate_signal(
            _benchmark_signal(symbol="DOGE-USDT-SWAP"),
            entry_price=100.0,
            allowed_directions=["long"],
            symbol_universe=UNIVERSE,
            is_benchmark=True,
        )


def test_benchmark_take_profit_geometry_still_checked() -> None:
    with pytest.raises(ValueError, match="take profit"):
        validate_signal(
            _benchmark_signal(take_profits=(TakeProfit(price=90.0, fraction=0.5),)),
            entry_price=100.0,
            allowed_directions=["long"],
            symbol_universe=UNIVERSE,
            is_benchmark=True,
        )


# --------------------------------------------------------------------------- #
# Üç aşamalı çıkış yönetimi
# --------------------------------------------------------------------------- #
def _managed(**overrides: object) -> Signal:
    defaults: dict[str, object] = dict(
        breakeven_at_r=1.0,
        partial_tp=PartialTakeProfit(r=1.5, fraction=0.5),
        trail_giveback_pct=0.5,
    )
    defaults.update(overrides)
    return _signal(**defaults)


def _check(signal: Signal, **kwargs: object) -> None:
    validate_signal(
        signal,
        entry_price=100.0,
        allowed_directions=["long", "short"],
        symbol_universe=UNIVERSE,
        **kwargs,  # type: ignore[arg-type]
    )


def test_full_exit_management_passes() -> None:
    _check(_managed(take_profits=(TakeProfit(price=120.0, fraction=1.0),)))


def test_trailing_atr_and_giveback_cannot_be_combined() -> None:
    """İki ayrı takip mekanizması: birlikte çalışırsa çıkışı hangisinin ürettiği okunamaz."""
    with pytest.raises(ValueError, match="aynı anda kullanılamaz"):
        _check(_managed(trailing_atr=3.0))


def test_giveback_requires_a_partial_exit() -> None:
    with pytest.raises(ValueError, match="partial_tp ile birlikte"):
        _check(_signal(trail_giveback_pct=0.5))


def test_management_requires_a_stop() -> None:
    """R'nin paydası ilk stop'tur: stop'suz sinyalde "1.5R'da yarısını al"ın karşılığı yok."""
    with pytest.raises(ValueError, match="stop_price olmadan"):
        validate_signal(
            Signal(
                symbol="BTC-USDT-SWAP",
                direction="long",
                sizing="notional_fraction",
                notional_fraction=0.5,
                breakeven_at_r=1.0,
            ),
            entry_price=100.0,
            allowed_directions=["long"],
            symbol_universe=UNIVERSE,
            is_benchmark=True,
        )


@pytest.mark.parametrize("fraction", [0.0, 1.0, 1.5, -0.1])
def test_partial_fraction_must_be_strictly_between_zero_and_one(fraction: float) -> None:
    """1.0 bir kısmi çıkış değil tam çıkıştır: sonrasında çekilecek stop kalmaz."""
    with pytest.raises(ValueError, match="partial_tp.fraction"):
        _check(_signal(partial_tp=PartialTakeProfit(r=1.5, fraction=fraction)))


@pytest.mark.parametrize("giveback", [0.0, 1.0, 1.2, -0.3])
def test_giveback_must_be_strictly_between_zero_and_one(giveback: float) -> None:
    with pytest.raises(ValueError, match="trail_giveback_pct"):
        _check(
            _signal(
                partial_tp=PartialTakeProfit(r=1.5, fraction=0.5),
                trail_giveback_pct=giveback,
            )
        )


@pytest.mark.parametrize("value", [0.0, -1.0])
def test_breakeven_and_partial_levels_must_be_positive(value: float) -> None:
    with pytest.raises(ValueError, match="pozitif olmalı"):
        _check(_signal(breakeven_at_r=value))
    with pytest.raises(ValueError, match="pozitif olmalı"):
        _check(_signal(partial_tp=PartialTakeProfit(r=value, fraction=0.5)))


# --------------------------------------------------------------------------- #
# is_replica: boyutlandırma kapısı ve model bildirimi
# --------------------------------------------------------------------------- #
def _replica_signal(**overrides: object) -> Signal:
    defaults: dict[str, object] = dict(
        symbol="BTC-USDT-SWAP",
        direction="long",
        sizing="notional_fraction",
        notional_fraction=0.5,
        stop_price=95.0,
    )
    defaults.update(overrides)
    return Signal(**defaults)  # type: ignore[arg-type]


def test_replica_may_use_notional_fraction_with_a_stop() -> None:
    """Kopyanın stop'u kopyalanan sistemin parçasıdır: çıpanın aksine ZORUNLUDUR."""
    _check(_replica_signal(), is_replica=True)


def test_replica_without_a_stop_is_rejected() -> None:
    with pytest.raises(ValueError, match="is_replica iken stop_price zorunludur"):
        _check(_replica_signal(stop_price=None), is_replica=True)


def test_benchmark_with_a_stop_is_still_rejected() -> None:
    """Çıpanın kuralı ters yöndedir ve değişmedi: stop takmak onu trend modeline çevirirdi."""
    with pytest.raises(ValueError, match="stop_price None olmalıdır"):
        _check(_replica_signal(), is_benchmark=True)


def test_plain_competitor_still_cannot_use_notional_fraction() -> None:
    with pytest.raises(ValueError, match="yalnızca is_benchmark"):
        _check(_replica_signal())


def test_replica_stop_geometry_is_still_checked() -> None:
    with pytest.raises(ValueError, match="stop_price giriş fiyatının altında"):
        _check(_replica_signal(stop_price=105.0), is_replica=True)


class _Model(Strategy):
    name = "test_model"
    allowed_directions = ["long"]

    def generate_signals(self, market, peer_signals=None):  # type: ignore[no-untyped-def]
        return []


def _model(**attrs: object) -> Strategy:
    model = _Model()
    for key, value in attrs.items():
        setattr(model, key, value)
    return model


def test_validate_model_accepts_a_plain_competitor() -> None:
    validate_model(_Model())


def test_limits_are_closed_to_competitors() -> None:
    """Kural 6: limitler tüm yarışmacılar için birebir aynıdır."""
    with pytest.raises(ValueError, match="yalnızca is_replica"):
        validate_model(_model(limits=ModelLimits(max_positions=3)))


def test_replica_leverage_is_capped() -> None:
    validate_model(_model(is_replica=True, limits=ModelLimits(leverage=REPLICA_LEVERAGE_CAP)))
    with pytest.raises(ValueError, match="tavanı"):
        validate_model(
            _model(is_replica=True, limits=ModelLimits(leverage=REPLICA_LEVERAGE_CAP + 0.1))
        )


def test_a_model_cannot_be_both_anchor_and_replica() -> None:
    with pytest.raises(ValueError, match="aynı anda True olamaz"):
        validate_model(_model(is_benchmark=True, is_replica=True))


@pytest.mark.parametrize(
    "limits",
    [
        ModelLimits(max_positions=0),
        ModelLimits(max_per_direction=-1),
        ModelLimits(max_portfolio_risk=0.0),
        ModelLimits(max_portfolio_risk=1.5),
        ModelLimits(leverage=0.0),
    ],
)
def test_nonsensical_limits_are_rejected(limits: ModelLimits) -> None:
    with pytest.raises(ValueError):
        validate_model(_model(is_replica=True, limits=limits))
