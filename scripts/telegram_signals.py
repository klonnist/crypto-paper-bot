#!/usr/bin/env python3
"""Scalp katmanının ANLIK sinyal bildirimi: yeni sinyal üretildiğinde Telegram mesajı.

`scripts/telegram_report.py` ile KARIŞTIRILMAMALIDIR ve ona hiç dokunmaz: o, 4 saatlik
katmanın günde bir kez (as_of 20:00) yolladığı PERFORMANS özetidir; bu ise scalp
katmanının her turunda çalışan ve yalnızca YENİ SİNYAL olayını bildiren ayrı bir
script'tir. İkisi ayrı workflow'larda, ayrı rapor dosyalarını okuyarak koşar.

**Tetikleyen tek olay: yeni sinyal.** Pozisyon kapanışı, funding tahakkuku ve bar
ilerlemesi mesaj üretmez — bunlar zaten defterde ve dashboard'da durur, anlık bildirim
ise okunması ZAMANA BAĞLI olan tek şey içindir: bir sonraki barın açılışında dolacak
bir emir.

Kaynak, turun kendi raporudur (`docs/data/metrics_scalp.json > round.models[].emitted`,
bkz. core/engine.py::EmittedSignal). Defteri okumak yetmezdi: deftere yalnızca DOLAN
emirler girer ve dolum bir sonraki bardadır (kural 13) — yani sinyalin haber değeri
olduğu an defterde henüz hiçbir satırı yoktur.

**Üç filtre (mesaj gürültüsünü engeller):**

1. **Yalnızca SON barın sinyalleri.** Saatlik cron her turda dört 15m barını işler
   (`signals_per_bar`, bkz. CLAUDE.md > Telafi edilen barlarda sinyal) ve telafi edilen
   barlar da kendi sinyallerini üretir. O sinyaller deftere yazılır ve ölçüme girer, ama
   BİLDİRİLMEZ: 45 dakika önceki bir barın emri çoktan dolmuştur, mesaj okuyucuyu
   girilemeyecek bir işleme yönlendirirdi.
2. **Aynı (model, sembol, yön) için 4 bar tekrar yok.** Durum `state/telegram_scalp.json`
   dosyasında tutulur ve koşular arası commit edilir (bkz. run-scalp.yml).
3. **5'ten fazla sinyalde tek TOPLU mesaj.** Tek tek yollamak bildirim akışını
   kullanılamaz hâle getirirdi.

**Bu script koşuyu ASLA düşürmez** — `scripts/telegram_report.py` ile aynı söz: eksik
token, ağ hatası, Telegram 4xx'i, bozuk JSON ya da bozuk durum dosyası loglanır ve
geçilir, her yol 0 ile biter. Bildirim ölçümün parçası değildir.

Gizli anahtarlar ortamdan okunur (GitHub Secrets -> env): TELEGRAM_BOT_TOKEN,
TELEGRAM_CHAT_ID — günlük özetle AYNI secret'lar. Değerler hiçbir log satırına yazılmaz.
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import math
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Kol adı `reason` kuyruğundaki `arm=` etiketinden okunur ve etiketin formatı tek yerde
# durur (bkz. core/tags.py): burada kendi regex'ini kurmak, ayıraç bir gün değiştiğinde
# mesajın sessizce "kol yok" demeye başlaması demekti.
from core.tags import find_tag  # noqa: E402 — sys.path yukarıda kuruluyor

METRICS_PATH = Path("docs/data/metrics_scalp.json")
STATE_PATH = Path("state/telegram_scalp.json")
API_TEMPLATE = "https://api.telegram.org/bot{token}/sendMessage"
DEFAULT_LAYER = "scalp"

# Rapor bu yaştan eskiyse bildirim yollanmaz: tur düşmüşse depodaki rapor bir önceki
# turdan kalır ve o turun sinyalleri artık "yeni" değildir. Script turun hemen ardından
# koştuğu için taze bir rapor her zaman birkaç dakikalıktır; sınır saatlik cron'un bir
# periyodundan (60 dk) belirgin biçimde küçük olmalıdır.
MAX_REPORT_AGE_MINUTES = 30.0
# Aynı (model, sembol, yön) için susturma penceresi, BAR cinsinden.
DEDUPE_BARS = 4
# Bu sayıdan fazla sinyal varsa tek toplu mesaj.
MAX_SINGLE_MESSAGES = 5
# Yalnızca bu modellerin sinyalleri bildirilir (kullanıcı tercihi — diğer scalp
# modelleri de deftere yazılır ve ölçüme girer, sadece Telegram'a düşmezler).
NOTIFY_MODELS = frozenset({"scalp_patient"})
# Durum dosyasında tutulan kaydın azami yaşı (bar): susturma penceresinin katı kadar
# geçmiş yeter, fazlası dosyayı sonsuza kadar büyütürdü.
STATE_RETENTION_BARS = 8 * DEDUPE_BARS
REQUEST_TIMEOUT_SEC = 20
# Telegram tek mesajda 4096 karakter kabul eder.
MAX_MESSAGE_CHARS = 3900
REASON_CHARS = 200

logger = logging.getLogger("telegram_signals")


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper()),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    try:
        return _run(args)
    except Exception as exc:  # noqa: BLE001 — bilinçli geniş yakalama, bkz. modül docstring
        logger.warning("sinyal bildirimi üretilemedi, geçiliyor: %s", exc)
        return 0


def _run(args: argparse.Namespace) -> int:
    path = Path(args.metrics)
    if not path.is_file():
        logger.warning("%s yok: bildirim yollanmıyor", path)
        return 0

    payload = json.loads(path.read_text(encoding="utf-8"))

    if payload.get("dry_run"):
        logger.info("rapor bir --dry-run turundan: bildirim yollanmıyor")
        return 0

    layer = str(payload.get("layer", ""))
    if layer != args.layer:
        # Yanlış dosyaya bakmak, 4 saatlik katmanın sinyallerini scalp bildirimi diye
        # yollamak demekti. Katman raporun kendi alanından doğrulanır, dosya adından değil.
        logger.warning(
            "rapor %r katmanına ait, beklenen %r: bildirim yollanmıyor", layer, args.layer
        )
        return 0

    age = _report_age_minutes(payload)
    if not args.force and age is not None and age > args.max_age_minutes:
        logger.warning(
            "rapor %.1f dakikalık (sınır %.0f): tur bu koşuda güncellenmemiş olabilir, "
            "bildirim yollanmıyor", age, args.max_age_minutes,
        )
        return 0

    as_of = _stamp(payload.get("as_of"))
    signals = _last_bar_signals(payload, as_of=as_of)
    signals = [s for s in signals if s.get("model") in NOTIFY_MODELS]
    if not signals:
        logger.info("%s barında yeni sinyal yok: bildirim yollanmıyor", payload.get("as_of"))
        return 0

    span = _bar_span(signals)
    state = _load_state(Path(args.state))
    fresh = _without_recent(signals, state=state, bar_span=span)
    if not fresh:
        logger.info(
            "%d sinyalin hepsi son %d barda zaten bildirilmişti: bildirim yollanmıyor",
            len(signals), DEDUPE_BARS,
        )
        return 0

    messages = build_messages(fresh)
    if args.dry_run:
        # Durum dosyasına YAZILMAZ: yollanmamış bir mesajı "bildirildi" saymak, gerçek
        # koşuda aynı sinyali susturur ve bildirimi sessizce kaybederdi.
        print("\n\n---\n\n".join(text for text, _ in messages))
        return 0

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        # Sık ve beklenen bir durum (fork, yerel koşu): uyarı değil bilgi.
        logger.info("TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID tanımlı değil: bildirim yollanmıyor")
        return 0

    # Durum, mesaj mesaj güncellenir: yollanamayan bir mesajın sinyalleri "bildirildi"
    # SAYILMAZ, yoksa susturma penceresi hiç gitmemiş bir bildirimi susturmuş olurdu.
    # Bir mesajın hatası kalanları da susturmaz — hepsi denenir.
    notified = [
        signal
        for text, covered in messages
        if _send(token=token, chat_id=chat_id, message=text)
        for signal in covered
    ]
    if not notified:
        logger.warning("hiçbir mesaj yollanamadı, durum dosyası güncellenmedi")
        return 0

    _save_state(Path(args.state), state=state, notified=notified, as_of=as_of, bar_span=span)
    return 0


def _send(*, token: str, chat_id: str, message: str) -> bool:
    import requests  # yerel import: --dry-run ve testler requests olmadan da çalışsın

    try:
        response = requests.post(
            API_TEMPLATE.format(token=token),
            json={
                "chat_id": chat_id,
                "text": message,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=REQUEST_TIMEOUT_SEC,
        )
    except Exception as exc:  # noqa: BLE001 — ağ hatası turu düşüremez
        logger.warning("Telegram isteği başarısız, bildirim yollanamadı: %s", exc)
        return False
    if response.status_code != 200:
        # Gövde Telegram'ın hata açıklamasıdır (token içermez); teşhis için gerekli.
        logger.warning(
            "Telegram %s döndü, bildirim yollanamadı: %s",
            response.status_code, response.text[:300],
        )
        return False
    logger.info("sinyal bildirimi yollandı (%d karakter)", len(message))
    return True


# --------------------------------------------------------------------------- #
# Sinyal seçimi
# --------------------------------------------------------------------------- #
def _last_bar_signals(
    payload: Mapping[str, Any], *, as_of: datetime | None
) -> list[dict[str, Any]]:
    """Turun SON barında (`as_of`) üretilmiş sinyaller.

    Telafi edilen barların sinyalleri deftere yazılır ve ölçüme girer ama bildirilmez:
    saatlik cron'da bir tur dört barı işler, yani en eski bar 45 dakika öncesine aittir
    ve emri çoktan dolmuştur. Geçmiş bir bar için mesaj atmak, okuyucuyu artık
    girilemeyecek bir işleme yönlendirirdi.

    `as_of` okunamıyorsa hiçbir sinyal bildirilmez: hangi barın "son" olduğunu bilmeden
    filtre uygulanamaz ve filtresiz bildirim tam da engellenmek istenen gürültüdür.
    """
    if as_of is None:
        logger.warning("raporun as_of değeri okunamadı: bildirim yollanmıyor")
        return []

    signals: list[dict[str, Any]] = []
    for model in (payload.get("round") or {}).get("models") or ():
        for item in model.get("emitted") or ():
            bar = _stamp(item.get("bar"))
            if bar is None or bar != as_of:
                continue
            signals.append({**item, "model": item.get("model") or model.get("model")})
    # Sıra deterministik: aynı rapor iki kez okunduğunda mesaj da birebir aynı olsun.
    signals.sort(key=lambda item: (str(item.get("model")), str(item.get("symbol"))))
    return signals


def _bar_span(signals: Sequence[Mapping[str, Any]]) -> timedelta | None:
    """Bar süresi: sinyalin kendi `fills_at - bar` farkı (kural 13'ün bir barı).

    Config'ten okunmaz, çünkü rapor zaten ikisini de taşır ve script'in katmanın bar
    ayarına dair ikinci bir varsayım kurması, iki kaynağın bir gün ayrışması demekti.
    """
    for item in signals:
        bar, fills_at = _stamp(item.get("bar")), _stamp(item.get("fills_at"))
        if bar is not None and fills_at is not None and fills_at > bar:
            return fills_at - bar
    return None


def _without_recent(
    signals: Sequence[Mapping[str, Any]],
    *,
    state: dict[str, str],
    bar_span: timedelta | None,
) -> list[dict[str, Any]]:
    """Son `DEDUPE_BARS` bar içinde aynı (model, sembol, yön) bildirilmişse eler.

    Pencere olmadan, aynı kurulumu üst üste barlarda öneren bir model (ya da elle
    tetiklenip `as_of`'u birkaç bar ilerleten bir koşu) aynı mesajı tekrar tekrar
    yollardı. Bar süresi ölçülemiyorsa pencere UYGULANMAZ: susturma bir kolaylıktır,
    onu tahmini bir süreyle uygulamak gerçek bir sinyali sessizce düşürebilirdi.
    """
    window = None if bar_span is None else bar_span * DEDUPE_BARS
    kept: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in signals:
        key = _key(item)
        if key in seen:
            continue
        seen.add(key)
        bar = _stamp(item.get("bar"))
        last = _stamp(state.get(key))
        if window is not None and bar is not None and last is not None and bar - last < window:
            logger.info(
                "%s: son %d bar içinde (%s) zaten bildirildi, atlanıyor",
                key, DEDUPE_BARS, state.get(key),
            )
            continue
        kept.append(dict(item))
    return kept


def _key(signal: Mapping[str, Any]) -> str:
    return f"{signal.get('model')}|{signal.get('symbol')}|{signal.get('direction')}"


# --------------------------------------------------------------------------- #
# Durum dosyası (yalnızca bildirim bookkeeping'i — ölçümün parçası DEĞİL)
# --------------------------------------------------------------------------- #
def _load_state(path: Path) -> dict[str, str]:
    """Son bildirim zamanları. Dosya yoksa/bozuksa BOŞ döner, hata fırlatmaz.

    Bozuk bir durum dosyası yüzünden bildirimin susması, susturma penceresinin
    engellemeye çalıştığından daha büyük bir kayıptır: en kötü ihtimalle bir mesaj
    tekrar eder.
    """
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        sent = payload.get("sent") or {}
        return {str(key): str(value) for key, value in sent.items()}
    except FileNotFoundError:
        return {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("%s okunamadı, susturma penceresi bu turda uygulanmıyor: %s", path, exc)
        return {}


def _save_state(
    path: Path,
    *,
    state: Mapping[str, str],
    notified: Iterable[Mapping[str, Any]],
    as_of: datetime | None,
    bar_span: timedelta | None,
) -> None:
    updated = dict(state)
    for item in notified:
        bar = item.get("bar")
        if bar:
            updated[_key(item)] = str(bar)

    payload = {
        "note": "Telegram sinyal bildiriminin susturma penceresi; ölçümün parçası değildir.",
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "sent": dict(sorted(_pruned(updated, as_of=as_of, bar_span=bar_span).items())),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except Exception as exc:  # noqa: BLE001 — yazamamak en kötü ihtimalle tekrar mesajdır
        logger.warning("%s yazılamadı: %s", path, exc)
        return
    logger.info("bildirim durumu güncellendi: %s (%d kayıt)", path, len(payload["sent"]))


def _pruned(
    state: Mapping[str, str], *, as_of: datetime | None, bar_span: timedelta | None
) -> dict[str, str]:
    """Susturma penceresinin çoktan dışında kalan kayıtları atar (dosya sınırsız büyümesin).

    Eşik penceresinin katıdır, penceresinin kendisi değil: kıl payı dışarıda kalmış bir
    kaydı hemen atmak, dosyayı her turda baştan yazmaktan başka bir şey kazandırmazdı.
    Bar süresi bilinmiyorsa budama YAPILMAZ — kayıt sayısı zaten model × sembol × yön ile
    sınırlıdır, yanlış bir eşikle susturma penceresini kesmektense dosya biraz büyür.
    """
    if as_of is None or bar_span is None:
        return dict(state)
    cutoff = as_of - bar_span * STATE_RETENTION_BARS
    kept: dict[str, str] = {}
    for key, value in state.items():
        stamp = _stamp(value)
        if stamp is None or stamp >= cutoff:
            kept[key] = value
    return kept


# --------------------------------------------------------------------------- #
# Mesaj
# --------------------------------------------------------------------------- #
def build_messages(
    signals: Sequence[Mapping[str, Any]],
) -> list[tuple[str, list[Mapping[str, Any]]]]:
    """Bildirilecek mesajlar: 5'e kadar tek tek, fazlasında TEK toplu mesaj.

    Her mesaj KAPSADIĞI sinyallerle birlikte döner: susturma penceresi ancak mesaj gerçekten
    yollandığında güncellenmeli ve hangi sinyalin hangi mesajda gittiği çağıranda
    bilinmelidir.

    HTML parse_mode, günlük özetle aynı gerekçeyle (bkz. scripts/telegram_report.py):
    model ve kol adları alt çizgi içerir (`scalp_bandit`, `rsi2_reversal`) ve Markdown'da
    alt çizgi italik açar.
    """
    if len(signals) > MAX_SINGLE_MESSAGES:
        return [(_batch_message(signals), list(signals))]
    return [(_single_message(signal), [signal]) for signal in signals]


def _single_message(signal: Mapping[str, Any]) -> str:
    lines = [
        "<b>⚡ SCALP SİNYALİ</b>",
        f"<b>{_esc(signal.get('model'))}</b> / {_esc(_arm(signal))}",
        f"{_esc(signal.get('symbol'))} · <b>{_esc(_direction(signal))}</b>",
        f"bar {_esc(_clock(signal.get('bar')))} UTC · kapanış <code>{_price(signal.get('close'))}</code>",
        f"stop <code>{_price(signal.get('stop_price'))}</code> · "
        f"hedef <code>{_price(signal.get('target_price'))}</code> · "
        f"R:R {_ratio(signal.get('reward_risk'))}",
    ]
    reason = _reason(signal)
    if reason:
        lines.append(f"<i>{_esc(reason)}</i>")
    lines.append("")
    lines.append(_warning(signal))
    return _clipped("\n".join(lines))


def _batch_message(signals: Sequence[Mapping[str, Any]]) -> str:
    """Tek mesajda tüm sinyaller. `reason` metni GİRMEZ: mesaj sınırı 4096 karakterdir
    ve altı sinyalin gerekçesi tek başına onu aşabilirdi; gerekçe dashboard'da durur."""
    lines = [
        f"<b>⚡ SCALP SİNYALİ — {len(signals)} yeni sinyal</b>",
        f"bar {_esc(_clock(signals[0].get('bar')))} UTC",
        "",
    ]
    for index, signal in enumerate(signals, start=1):
        lines.append(
            f"{index}. <b>{_esc(signal.get('model'))}</b> / {_esc(_arm(signal))} — "
            f"{_esc(signal.get('symbol'))} <b>{_esc(_direction(signal))}</b>"
        )
        lines.append(
            f"   kapanış <code>{_price(signal.get('close'))}</code> · "
            f"stop <code>{_price(signal.get('stop_price'))}</code> · "
            f"hedef <code>{_price(signal.get('target_price'))}</code> · "
            f"R:R {_ratio(signal.get('reward_risk'))}"
        )
    lines.append("")
    lines.append(_warning(signals[0]))
    return _clipped("\n".join(lines))


def _warning(signal: Mapping[str, Any]) -> str:
    """ZORUNLU uyarı satırı: bildirim ile dolum arasındaki fiyat farkı.

    Bu satır opsiyonel değildir. Sinyal, üretildiği barın KAPANIŞINDA duyurulur; emir ise
    bir SONRAKİ barın açılışından dolar (kural 13). Mesajı okuyan kişi o arada piyasadan
    girerse fiyatı botunkiyle aynı olmaz — uyarı olmadan mesaj, defterdeki sonucun
    tekrarlanabileceği izlenimini verirdi.
    """
    fills_at = _clock(signal.get("fills_at"))
    return (
        f"⚠️ Bot bu emri bir sonraki bar açılışından dolduracak ({_esc(fills_at)} UTC). "
        "Senin girişin farklı bir fiyattan olacak."
    )


def _arm(signal: Mapping[str, Any]) -> str:
    """Kol adı: `reason` kuyruğundaki `arm=` etiketi (bkz. core/tags.py).

    Etiket yoksa `—`: burada TagError fırlatmak, bildirim katmanının ölçüm katmanının
    kuralını taklit etmesi olurdu. Kırılımın sessizce eksilmesi sorunu metriklerde
    geçerlidir (core/metrics.py hata fırlatır), bir mesaj satırında değil.
    """
    return find_tag(str(signal.get("reason", "")), "arm") or "—"


def _reason(signal: Mapping[str, Any]) -> str:
    text = " ".join(str(signal.get("reason", "")).split())
    return text[: REASON_CHARS - 1] + "…" if len(text) > REASON_CHARS else text


def _direction(signal: Mapping[str, Any]) -> str:
    return str(signal.get("direction", "")).upper()


def _clipped(message: str) -> str:
    return message if len(message) <= MAX_MESSAGE_CHARS else message[:MAX_MESSAGE_CHARS] + "\n…"


# --------------------------------------------------------------------------- #
# Biçimlendirme — tanımsız değer "—", 0 DEĞİL (bkz. core/metrics.py)
# --------------------------------------------------------------------------- #
def _num(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(number) else number


def _price(value: Any) -> str:
    """Fiyat: anlamlı basamakla. Evren BTC (60.000) ile PENGU (0,03) arasında değişir;
    sabit ondalık sayı biri için gereksiz, diğeri için okunamaz olurdu."""
    number = _num(value)
    return "—" if number is None else f"{number:.6g}"


def _ratio(value: Any) -> str:
    number = _num(value)
    return "—" if number is None else f"{number:.2f}"


def _clock(value: Any) -> str:
    """ISO damgasını `YYYY-MM-DD HH:MM` biçimine indirir (mesajda saniye/ofset gürültüdür)."""
    stamp = _stamp(value)
    return "—" if stamp is None else stamp.strftime("%Y-%m-%d %H:%M")


def _esc(value: Any) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def _stamp(value: Any) -> datetime | None:
    """ISO metnini UTC'ye çevirir; okunamayan damga None (0 ya da "şimdi" DEĞİL)."""
    if not value:
        return None
    try:
        stamp = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return stamp.replace(tzinfo=timezone.utc) if stamp.tzinfo is None else stamp.astimezone(timezone.utc)


def _report_age_minutes(payload: Mapping[str, Any]) -> float | None:
    """`generated_at`'ten bu yana geçen dakika; damga okunamazsa None (kapı uygulanmaz)."""
    generated = _stamp(payload.get("generated_at"))
    if generated is None:
        return None
    return (datetime.now(timezone.utc) - generated).total_seconds() / 60.0


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Scalp katmanının yeni sinyallerini Telegram'a bildirir."
    )
    parser.add_argument("--metrics", default=str(METRICS_PATH), help="katmanın rapor dosyası")
    parser.add_argument("--state", default=str(STATE_PATH), help="susturma penceresi durumu")
    parser.add_argument(
        "--layer", default=DEFAULT_LAYER,
        help=f"raporun ait olması gereken katman (varsayılan {DEFAULT_LAYER})",
    )
    parser.add_argument(
        "--max-age-minutes", type=float, default=MAX_REPORT_AGE_MINUTES,
        help=f"rapor bundan eskiyse yollama (varsayılan {MAX_REPORT_AGE_MINUTES:.0f})",
    )
    parser.add_argument("--force", action="store_true", help="tazelik kapısını atla")
    parser.add_argument("--dry-run", action="store_true", help="yollama, mesajları yazdır")
    parser.add_argument(
        "--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"]
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    sys.exit(main())
