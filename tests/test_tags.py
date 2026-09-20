"""core/tags.py: `reason` kuyruğundaki `| anahtar=değer` sözleşmesi.

Etiketi YAZAN taraf bir strateji, OKUYAN taraf salt okunur bir metrik modülüdür. Bu
dosyanın ölçtüğü şey ikisinin aynı formatta buluşması ve eksik etiketin SESSİZCE
geçmemesi — kol kırılımının anlamı "her işlem bir kola aittir" varsayımına dayanıyor.
"""

from __future__ import annotations

import pytest

from core.tags import TagError, find_tag, format_tags, parse_tag


def test_tags_are_appended_to_the_free_text() -> None:
    reason = format_tags("VWAP geri çekilme kurulumu", arm="vwap_pullback", post_r=0.3125)

    assert reason == "VWAP geri çekilme kurulumu | arm=vwap_pullback | post_r=0.3125"


def test_round_trip() -> None:
    reason = format_tags("metin", arm="momentum_burst", post_r=-0.5)

    assert parse_tag(reason, "arm") == "momentum_burst"
    assert parse_tag(reason, "post_r") == "-0.5"


def test_missing_tag_raises() -> None:
    """Eksik etiket, kırılım toplamı ile model toplamının sessizce ayrışması demekti."""
    with pytest.raises(TagError):
        parse_tag("etiketsiz eski satır", "arm")


def test_find_tag_tolerates_absence() -> None:
    """Tolerans gereken yerler (sayfa) için ayrı bir kapı: hata fırlatan yol tek değil."""
    assert find_tag("etiketsiz", "arm") is None


def test_nan_is_written_as_nan_not_zero() -> None:
    """Ölçülmemiş posterior `nan`dır; `0.0` yazmak "ölçtüm, sıfır çıktı" demek olurdu."""
    reason = format_tags("metin", post_r=float("nan"))

    assert parse_tag(reason, "post_r") == "nan"


def test_values_with_spaces_are_rejected() -> None:
    """Ayıraç " | " olduğu için boşluklu değer etiketi ikiye bölerdi."""
    with pytest.raises(TagError):
        format_tags("metin", arm="iki kelime")


def test_invalid_key_is_rejected() -> None:
    with pytest.raises(TagError):
        format_tags("metin", **{"kol adı": "x"})


def test_empty_reason_produces_only_tags() -> None:
    assert format_tags("", arm="x") == "arm=x"


def test_tag_survives_a_reason_that_contains_pipes() -> None:
    """Serbest metin ayıraç içerse bile SON etiketler ayrıştırılabilir kalmalı."""
    reason = format_tags("a | b içeren metin", arm="rsi2_reversal")

    assert parse_tag(reason, "arm") == "rsi2_reversal"
