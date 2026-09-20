"""Sinyal doğrulamasının tek geçidi. Her strateji sinyali motora girmeden buradan geçer.

Programlama hatası (izin dışı yön, tutarsız stop/TP geometrisi, bilinmeyen sembol)
sessizce filtrelenmez — ValueError fırlatılır. Actions workflow'u model bazında hatayı
yakalayıp yalnızca o modeli atlar, koşu devam eder (CLAUDE.md kural 8).

Boyutlandırma modu da burada denetlenir (kural 15): `sizing="notional_fraction"` YALNIZCA
`is_benchmark=True` (referans çıpası) ve `is_replica=True` (dış sistem kopyası) modellere
açıktır. Bu kapı olmadan kural 3/11 delinebilir — her model kendi boyutunu "referans gibi"
belirlemeye başlar ve ortak risk birimi (1R) ortadan kalkar, yani modeller artık aynı
ölçekte yarışmaz. İki alanın birbirini dışlaması da sessiz düzeltmeye değil hataya
bağlanmıştır: `sizing="risk"` gelen bir sinyalde dolu bir `notional_fraction`'ı yok
saymak, modelin hangi boyutlandırmayı istediğini tahmin etmek olurdu.

**Çıpa ile kopyanın stop kuralı BİLİNÇLİ olarak terstir.** Çıpanın (alım-tut) tanımı
stop'suz olmasıdır ve uydurma bir stop `risk_amount` üzerinden uydurma bir R üretirdi —
bu yüzden `stop_price` None OLMALIDIR. Kopyanın stop'u ise kopyalanan sistemin kendi
kuralıdır ve stop yönetimi (breakeven, kısmi çıkış, takip) tam olarak ölçülmek istenen
şeydir — bu yüzden `stop_price` ZORUNLUDUR. İkisi de aynı sebeple ortalama R
sıralamasına girmez (farklı boyutlandırma), ama sebep aynı diye kural aynı değildir.

**Üç aşamalı çıkış yönetimi de burada denetlenir.** `breakeven_at_r`, `partial_tp` ve
`trail_giveback_pct` R cinsinden tanımlıdır; R'nin paydası ilk stop'tur, dolayısıyla
stop'suz bir sinyalde bu alanların anlamı yoktur. `trailing_atr` ile `trail_giveback_pct`
İKİ AYRI takip mekanizmasıdır ve aynı anda kullanılamazlar: ikisi de stop'u sıkıştırır,
birlikte çalıştıklarında hangi kuralın çıkışı ürettiği defterden okunamaz hâle gelir —
oysa bu modeller tam olarak çıkış kuralını ölçmek için var.
"""

from __future__ import annotations

import logging

from strategies.base import Direction, ModelLimits, Signal, Strategy

logger = logging.getLogger(__name__)

VALID_SIZING_MODES: tuple[str, ...] = ("risk", "notional_fraction")

# Kopya modellerin kendi kaldıraç tavanı. Kök `leverage_cap` (5) ölçümün ortak tavanıdır
# ve yarışmacıların hepsi ona tabidir; kopya ise kopyaladığı sistemin kaldıracını taşır.
# Tavansız bırakmak, tek bir config satırıyla 50x bir satırın tabloya girmesi demekti —
# kopya yarışmasa da aynı likidasyon modelini (kural 13) ve aynı defteri kullanıyor.
REPLICA_LEVERAGE_CAP = 10.0


def validate_signal(
    signal: Signal,
    *,
    entry_price: float,
    allowed_directions: list[Direction],
    symbol_universe: list[str],
    is_benchmark: bool = False,
    is_replica: bool = False,
) -> None:
    """Sinyali reddeder ya da sessizce geçirir; bayrakların varsayılanı kısıtlayıcı olandır."""
    if signal.entry_type != "market":
        raise NotImplementedError(f"entry_type={signal.entry_type!r} henüz desteklenmiyor")

    if signal.symbol not in symbol_universe:
        raise ValueError(f"{signal.symbol} sembol evreninde değil")

    if signal.direction not in allowed_directions:
        raise ValueError(
            f"{signal.direction!r} yönü bu strateji için izinli değil "
            f"(allowed_directions={allowed_directions})"
        )

    _validate_sizing(signal, is_benchmark=is_benchmark, is_replica=is_replica)

    if signal.stop_price is not None:
        _validate_stop_geometry(signal, entry_price=entry_price)

    _validate_take_profits(signal, entry_price=entry_price)
    _validate_exit_management(signal)


def validate_model(strategy: Strategy) -> None:
    """Modelin bayrak/limit bildirimini kurulum anında denetler (kapı: strategies/registry.py).

    Neden sinyal kapısında değil: limitler ve kaldıraç sinyale değil MODELE aittir ve
    sinyal üretilmeden önce, boyutlandırmanın ilk anında geçerlidir. Kurulumda patlamak
    ayrıca doğru izolasyon seviyesidir — main.py kurulamayan modeli atlar ve koşu hata
    koduyla biter (sessiz eksik yarışma olmaz), oysa sinyal kapısında patlamak modeli
    aylarca "bu turda sinyal üretmedi" gibi gösterirdi.
    """
    if strategy.is_benchmark and strategy.is_replica:
        raise ValueError(
            f"{strategy.name}: is_benchmark ve is_replica aynı anda True olamaz — "
            "çıpa 'piyasa ne yaptı'yı, kopya 'dış sistem ne yapardı'yı ölçer; "
            "ikisi tabloda ayrı bölümlerdir"
        )
    if strategy.limits is None:
        return
    if not strategy.is_replica:
        raise ValueError(
            f"{strategy.name}: ModelLimits yalnızca is_replica=True modellere açıktır "
            "(CLAUDE.md kural 6: limitler tüm yarışmacılar için birebir aynıdır)"
        )
    _validate_limits(strategy.name, strategy.limits)


def _validate_limits(model: str, limits: ModelLimits) -> None:
    for field_name in ("max_positions", "max_per_direction"):
        value = getattr(limits, field_name)
        if value is not None and int(value) <= 0:
            raise ValueError(f"{model}: ModelLimits.{field_name} pozitif olmalı: {value}")
    if limits.max_portfolio_risk is not None and not 0.0 < limits.max_portfolio_risk <= 1.0:
        raise ValueError(
            f"{model}: ModelLimits.max_portfolio_risk 0 ile 1.0 arasında olmalı: "
            f"{limits.max_portfolio_risk}"
        )
    if limits.leverage is not None:
        if limits.leverage <= 0.0:
            raise ValueError(f"{model}: ModelLimits.leverage pozitif olmalı: {limits.leverage}")
        if limits.leverage > REPLICA_LEVERAGE_CAP:
            raise ValueError(
                f"{model}: ModelLimits.leverage tavanı {REPLICA_LEVERAGE_CAP:g}x, "
                f"{limits.leverage:g}x istendi"
            )


def _validate_sizing(signal: Signal, *, is_benchmark: bool, is_replica: bool) -> None:
    if signal.sizing not in VALID_SIZING_MODES:
        raise ValueError(
            f"bilinmeyen sizing modu: {signal.sizing!r} (geçerli: {list(VALID_SIZING_MODES)})"
        )

    if signal.sizing == "risk":
        if signal.stop_price is None:
            raise ValueError('sizing="risk" iken stop_price zorunludur')
        if signal.notional_fraction is not None:
            raise ValueError(
                'sizing="risk" iken notional_fraction dolu olamaz '
                f"(geldi: {signal.notional_fraction})"
            )
        return

    # sizing == "notional_fraction"
    if not (is_benchmark or is_replica):
        raise ValueError(
            'sizing="notional_fraction" yalnızca is_benchmark=True (kural 15) ya da '
            "is_replica=True modellere açıktır; yarışmacı modeller kendi boyutunu "
            "belirleyemez"
        )
    if is_benchmark and signal.stop_price is not None:
        # Sessizce yok saymak, deftere yazılan "ilk stop"un hiç kullanılmayan bir sayı
        # olması demekti: risk_amount ve cost_per_r o sayıdan türüyor.
        raise ValueError(
            'sizing="notional_fraction" + is_benchmark iken stop_price None olmalıdır '
            f"(geldi: {signal.stop_price})"
        )
    if is_replica and not is_benchmark and signal.stop_price is None:
        # Kopyanın stop'u kopyalanan sistemin parçasıdır ve likidasyon modellemesiyle
        # birlikte tam olarak ölçülmek istenen şeydir; stop'suz bir kopya başka bir
        # sistemi kopyalar.
        raise ValueError(
            'sizing="notional_fraction" + is_replica iken stop_price zorunludur '
            "(kopyalanan sistemin stop kuralı ölçümün parçasıdır)"
        )
    if signal.notional_fraction is None:
        raise ValueError('sizing="notional_fraction" iken notional_fraction zorunludur')
    if not 0.0 < signal.notional_fraction <= 1.0:
        raise ValueError(
            f"notional_fraction 0 ile 1.0 arasında olmalı: {signal.notional_fraction}"
        )


def _validate_stop_geometry(signal: Signal, *, entry_price: float) -> None:
    stop_price = signal.stop_price
    assert stop_price is not None  # çağıran garanti eder

    if stop_price == entry_price:
        raise ValueError("stop_price giriş fiyatına eşit olamaz (boyutlamada sıfıra bölme)")

    if signal.direction == "long" and stop_price >= entry_price:
        raise ValueError("long pozisyonda stop_price giriş fiyatının altında olmalı")
    if signal.direction == "short" and stop_price <= entry_price:
        raise ValueError("short pozisyonda stop_price giriş fiyatının üzerinde olmalı")


def _validate_take_profits(signal: Signal, *, entry_price: float) -> None:
    """TP geometrisi boyutlandırma modundan bağımsızdır: hedef girişin doğru tarafında olmalı."""
    total_fraction = 0.0
    for tp in signal.take_profits:
        if signal.direction == "long" and tp.price <= entry_price:
            raise ValueError("long pozisyonda take profit giriş fiyatının üzerinde olmalı")
        if signal.direction == "short" and tp.price >= entry_price:
            raise ValueError("short pozisyonda take profit giriş fiyatının altında olmalı")
        total_fraction += tp.fraction
    if total_fraction > 1.0:
        raise ValueError(f"take_profits fraction toplamı 1.0'ı aşıyor: {total_fraction}")


def _validate_exit_management(signal: Signal) -> None:
    """Üç aşamalı çıkış yönetiminin geometrisi ve mekanizma çakışması."""
    managed = (
        signal.breakeven_at_r is not None
        or signal.partial_tp is not None
        or signal.trail_giveback_pct is not None
    )
    if managed and signal.stop_price is None:
        # R'nin paydası ilk stop'tur: stop'suz bir sinyalde "1.5R'da yarısını al"ın
        # fiyat karşılığı yoktur. Sessizce yok saymak, modelin bildirdiği çıkış
        # kuralının hiç uygulanmadığı bir defter üretirdi.
        raise ValueError(
            "breakeven_at_r / partial_tp / trail_giveback_pct stop_price olmadan "
            "kullanılamaz: üçü de R cinsindendir ve R'nin paydası ilk stop'tur"
        )

    if signal.trailing_atr is not None and signal.trail_giveback_pct is not None:
        raise ValueError(
            "trailing_atr ve trail_giveback_pct aynı anda kullanılamaz: ikisi de ayrı "
            "bir takip mekanizmasıdır ve birlikte çalıştıklarında çıkışı hangi kuralın "
            "ürettiği defterden okunamaz"
        )

    if signal.breakeven_at_r is not None and signal.breakeven_at_r <= 0.0:
        raise ValueError(f"breakeven_at_r pozitif olmalı: {signal.breakeven_at_r}")

    partial = signal.partial_tp
    if partial is not None:
        if partial.r <= 0.0:
            raise ValueError(f"partial_tp.r pozitif olmalı: {partial.r}")
        if not 0.0 < partial.fraction < 1.0:
            # 1.0 bir kısmi çıkış değil, tam çıkıştır: sonrasında taşınacak bakiye ve
            # çekilecek stop kalmaz. O istek `take_profits` ile ifade edilir.
            raise ValueError(
                f"partial_tp.fraction 0 ile 1.0 ARASINDA olmalı (1.0 hariç): {partial.fraction}"
            )

    giveback = signal.trail_giveback_pct
    if giveback is not None:
        if not 0.0 < giveback < 1.0:
            # 0.0 "hiç geri verme" (stop tepe noktasında, her barda dolar), 1.0 "hepsini
            # geri ver" (stop hiç ilerlemez) demektir; ikisi de takip değil.
            raise ValueError(
                f"trail_giveback_pct 0 ile 1.0 arasında olmalı (uçlar hariç): {giveback}"
            )
        if signal.partial_tp is None:
            # Takip KISMİ ÇIKIŞTAN SONRA devreye girer (sözleşme): kısmi çıkış olmayan bir
            # sinyalde alan hiç uygulanmaz ve modelin bildirdiği kural ölü kod olurdu.
            raise ValueError(
                "trail_giveback_pct yalnızca partial_tp ile birlikte kullanılabilir: "
                "takip kısmi çıkıştan SONRA devreye girer"
            )
