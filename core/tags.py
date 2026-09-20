"""Defter `reason` alanındaki `| anahtar=değer` etiketlerinin TEK tanımı.

Neden ayrı modül: etiketi YAZAN taraf bir stratejidir (`strategies/scalp/…`), OKUYAN
taraf ise salt okunur bir metrik modülüdür (`core/metrics.py`). İkisinin formatı kendi
içinde kurması, bir gün birinin ayıracı değiştirmesi ve kırılımın sessizce boşalması
demekti. Format burada bir kez durur; iki taraf da buradan geçer.

`core/metrics.py`'nin `strategies/` içinden import yapmaması bilinçli bir sınırdır
(bkz. `compare` docstring'i): bu modül yalnızca `str` işler, hiçbir şey import etmez,
dolayısıyla iki tarafın ortak noktası olabilir.

**Eksik etiket sessizce atlanmaz.** `parse_tag` bulamadığı etikette `TagError` fırlatır:
kol bazlı kırılımın (bkz. CLAUDE.md > Scalp Katmanı) anlamı "her işlem bir kola aittir"
varsayımına dayanır. Etiketsiz bir satırı atlamak, o işlemin R'sini kırılımdan düşürüp
toplamla kırılım toplamını sessizce ayrıştırırdı — tam da denetlenebilirlik için
eklenmiş bir kolonun denetlenemez hâle gelmesi.
"""

from __future__ import annotations

import re

TAG_SEPARATOR = " | "

# Değer boşluk içermez: ayıraç " | " olduğu için değerin içindeki bir " | " etiketi
# ikiye bölerdi. Dar tutmak, "serbest metin" ile "ayrıştırılabilir kuyruk" arasındaki
# sınırı kod düzeyinde korur.
_TAG_PATTERN = r"[A-Za-z_][A-Za-z0-9_]*"
_VALUE_PATTERN = r"[^\s|]+"


class TagError(ValueError):
    """`reason` kuyruğunda beklenen etiket yok ya da biçimi bozuk."""


def format_tags(reason: str, **tags: object) -> str:
    """Serbest metnin sonuna `| anahtar=değer` kuyruğu ekler.

    Anahtar sırası çağıranın verdiği sıradır (Python 3.7+ kwargs sırası korunur):
    defterde okunan satırın kuyruğu her turda aynı sırada görünsün diye.
    """
    parts = [reason.strip()] if reason.strip() else []
    for key, value in tags.items():
        if not re.fullmatch(_TAG_PATTERN, key):
            raise TagError(f"geçersiz etiket anahtarı: {key!r}")
        text = _format_value(value)
        if not re.fullmatch(_VALUE_PATTERN, text):
            raise TagError(f"geçersiz etiket değeri ({key}): {text!r}")
        parts.append(f"{key}={text}")
    return TAG_SEPARATOR.join(parts)


def find_tag(reason: str, key: str) -> str | None:
    """Etiketin değeri; yoksa None. Tolerans gereken yerler için (ör. dashboard)."""
    match = re.search(rf"(?:^|\|)\s*{re.escape(key)}=({_VALUE_PATTERN})", str(reason))
    return match.group(1) if match else None


def parse_tag(reason: str, key: str) -> str:
    """Etiketin değeri; yoksa TagError.

    Kırılım hesaplayan yollar bunu kullanır: eksik etiket bir veri durumu değil, etiketi
    yazması gereken modelin hatasıdır ve kırılımı sessizce eksiltmesine izin verilmez.
    """
    value = find_tag(reason, key)
    if value is None:
        raise TagError(f"{key!r} etiketi reason kuyruğunda yok: {str(reason)!r}")
    return value


def _format_value(value: object) -> str:
    if isinstance(value, float):
        return f"{value:.4g}"
    return str(value)
