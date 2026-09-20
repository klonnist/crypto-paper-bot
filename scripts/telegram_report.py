#!/usr/bin/env python3
"""Günlük Telegram özeti: `docs/data/metrics.json`'u okur, tek bir mesaj yollar.

Kullanım:
    python scripts/telegram_report.py                 # yalnızca 20:00 UTC turunda yollar
    python scripts/telegram_report.py --dry-run       # yollamaz, mesajı stdout'a yazar
    python scripts/telegram_report.py --force         # saat kapısını atlar
    python scripts/telegram_report.py --hour 8        # başka bir turda yolla

**Bu script koşuyu ASLA düşürmez.** Her yol 0 ile biter: eksik token, ağ hatası,
Telegram'ın 4xx'i, bozuk JSON — hepsi loglanır ve geçilir. Gerekçe: özet bir bildirimdir,
ölçümün parçası değil. Telegram'ın kesintisi yüzünden turun kırmızı dönmesi, defterin
commit'lenip commit'lenmediğine dair gerçek sinyali gürültüye boğardı. Aynı sebeple
workflow'da bu adım defter commit'inden SONRA gelir: burada ne olursa olsun tur zaten
kaydedilmiştir. (Kapı atlanırsa bu da loglanır: sessiz bir "yollamadım" ile sessiz bir
"yollayamadım" ayırt edilebilir olmalı.)

Saat kapısı DUVAR SAATİNE değil turun `as_of` barına bakar. 4H barlarda gün içinde altı
tur koşar; "günde bir" demenin tekrarlanabilir tanımı "as_of'u 20:00 olan tur"dur. Duvar
saati kullanmak, cron geciktiğinde ya da tur elle tekrarlandığında özeti ya iki kez ya
hiç yollamazdı.

Gizli anahtarlar ortamdan okunur (GitHub Secrets -> env): TELEGRAM_BOT_TOKEN,
TELEGRAM_CHAT_ID. Değerler hiçbir log satırına yazılmaz.
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

METRICS_PATH = Path("docs/data/metrics.json")
API_TEMPLATE = "https://api.telegram.org/bot{token}/sendMessage"
DEFAULT_HOUR = 20
# Rapor bu yaştan eskiyse özet yollanmaz. Tur düşerse depodaki metrics.json bir önceki
# turdan kalır; saat kapısı tek başına DÜNKÜ 20:00 raporunu bugün tekrar yollamayı
# engelleyemez. Bar 4H olduğu için taze bir rapor her zaman bundan gençtir.
MAX_REPORT_AGE_HOURS = 3
REQUEST_TIMEOUT_SEC = 20
TOP_N = 3
# Telegram tek mesajda 4096 karakter kabul eder; kırpma sınırı buna göre.
MAX_MESSAGE_CHARS = 3900

logger = logging.getLogger("telegram_report")


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper()),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    try:
        return _run(args)
    except Exception as exc:  # noqa: BLE001 — bilinçli geniş yakalama, bkz. modül docstring
        logger.warning("Telegram özeti üretilemedi, geçiliyor: %s", exc)
        return 0


def _run(args: argparse.Namespace) -> int:
    path = Path(args.metrics)
    if not path.is_file():
        logger.warning("%s yok: özet yollanmıyor", path)
        return 0

    payload = json.loads(path.read_text(encoding="utf-8"))

    if payload.get("dry_run"):
        logger.info("rapor bir --dry-run turundan: özet yollanmıyor")
        return 0

    hour = _as_of_hour(payload)
    if not args.force and hour != args.hour:
        logger.info(
            "bu tur özet turu değil (as_of saati %s, beklenen %s): geçiliyor", hour, args.hour
        )
        return 0

    age = _report_age_hours(payload)
    if not args.force and age is not None and age > args.max_age_hours:
        logger.warning(
            "rapor %.1f saatlik (sınır %s): tur bu koşuda güncellenmemiş olabilir, "
            "özet yollanmıyor", age, args.max_age_hours,
        )
        return 0

    message = build_message(payload)
    if args.dry_run:
        print(message)
        return 0

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        # Sık ve beklenen bir durum (fork, yerel koşu): uyarı değil bilgi.
        logger.info("TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID tanımlı değil: özet yollanmıyor")
        return 0

    _send(token=token, chat_id=chat_id, message=message)
    return 0


def _send(*, token: str, chat_id: str, message: str) -> None:
    import requests  # yerel import: --dry-run ve testler requests olmadan da çalışsın

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
    if response.status_code != 200:
        # Gövde Telegram'ın hata açıklamasıdır (token içermez); teşhis için gerekli.
        logger.warning(
            "Telegram %s döndü, özet yollanamadı: %s", response.status_code, response.text[:300]
        )
        return
    logger.info("Telegram özeti yollandı (%d karakter)", len(message))


# --------------------------------------------------------------------------- #
# Mesaj
# --------------------------------------------------------------------------- #
def build_message(payload: Mapping[str, Any]) -> str:
    """Tek bir Telegram mesajı (HTML parse_mode).

    HTML seçildi çünkü model adları alt çizgi içerir (`failed_breakout`,
    `downtrend_rally`) ve Markdown'da alt çizgi italik açar: Telegram mesajı ya bozuk
    biçimlenir ya da 400 döner. HTML'de kaçırılması gereken üç karakter vardır ve
    `html.escape` hepsini kapatır.
    """
    lines: list[str] = []
    as_of = str(payload.get("as_of", ""))
    lines.append(f"<b>📊 Paper Trading</b> — {_esc(as_of[:16].replace('T', ' '))} UTC")

    lines.append("")
    lines.extend(_long_short_block(payload))

    competitors = _competitors(payload)
    ranked = _ranked(competitors)
    lines.append("")
    lines.extend(_ranking_block(ranked, unmeasured=len(competitors) - len(ranked)))

    lines.append("")
    lines.extend(_activity_block(payload))

    lines.append("")
    lines.extend(_acceptance_block(payload, ranked))

    lines.append("")
    lines.extend(_benchmark_block(payload, competitors))

    link = _dashboard_url()
    if link:
        lines.append("")
        lines.append(f'<a href="{_esc(link)}">Dashboard</a>')

    message = "\n".join(lines)
    if len(message) > MAX_MESSAGE_CHARS:
        message = message[:MAX_MESSAGE_CHARS] + "\n…"
    return message


def _long_short_block(payload: Mapping[str, Any]) -> list[str]:
    """Projenin ana sorusu: mesajın en üstünde, ilk 3'ten bile önce."""
    directions = (payload.get("pooled") or {}).get("directions") or {}
    long = directions.get("long") or {}
    short = directions.get("short") or {}
    if not long and not short:
        return ["<b>LONG vs SHORT</b>", "havuz verisi yok"]

    lines = ["<b>LONG vs SHORT</b> (yarışmacı havuzu)"]
    for label, stats in (("long ", long), ("short", short)):
        lines.append(
            f"<code>{label}</code> {_r(stats.get('avg_r'))} · "
            f"{_int(stats.get('trades'))} işlem · kazanma {_rate(stats.get('win_rate'))} · "
            f"funding {_usd(stats.get('funding'))}"
        )
    left, right = _num(long.get("avg_r")), _num(short.get("avg_r"))
    if left is not None and right is not None:
        better = "Short" if right > left else "Long"
        lines.append(f"→ <b>{better}</b> işlem başına {abs(right - left):.2f}R önde")
    return lines


def _ranking_block(ranked: Sequence[Mapping[str, Any]], *, unmeasured: int) -> list[str]:
    """Sıralamaya YALNIZCA ortalama R'si ölçülebilen modeller girer.

    Hiç kapanmış işlemi olmayan bir modeli "ilk 3"e koymak, ölçülmemiş bir modeli
    ölçülmüş bir modelin önüne geçirirdi; kaç modelin ölçülemediği ise atlanmaz,
    ayrı bir satır olarak söylenir.
    """
    if not ranked:
        return [
            "<b>Sıralama</b>",
            f"ölçülebilir işlem yok ({unmeasured} model henüz kapanmış işlem üretmedi)",
        ]

    lines = [f"<b>İlk {min(TOP_N, len(ranked))}</b> (ort. R)"]
    lines.extend(_rank_line(index + 1, item) for index, item in enumerate(ranked[:TOP_N]))

    # İlk 3 ile son 3 çakışıyorsa (az model) son bölümü tekrar yazmanın anlamı yok.
    tail = [item for item in ranked[-TOP_N:] if item not in ranked[:TOP_N]]
    if tail:
        lines.append(f"<b>Son {len(tail)}</b>")
        offset = len(ranked) - len(tail)
        lines.extend(_rank_line(offset + index + 1, item) for index, item in enumerate(tail))
    if any(item.get("_control") for item in list(ranked[:TOP_N]) + tail):
        lines.append("<i>⚠ kontrol grubu: bilgisiz çekiliş, sinyalin referansı</i>")
    if unmeasured:
        lines.append(f"<i>{unmeasured} model henüz kapanmış işlem üretmedi</i>")
    return lines


def _rank_line(position: int, item: Mapping[str, Any]) -> str:
    total = item.get("total") or {}
    account = item.get("account") or {}
    mark = " ⚠" if item.get("_control") else ""
    return (
        f"{position}. <b>{_esc(item['model'])}</b>{mark} {_r(total.get('avg_r'))} · "
        f"{_int(total.get('trades'))} işlem · getiri {_pct(account.get('total_return'), 2)}"
    )


def _activity_block(payload: Mapping[str, Any]) -> list[str]:
    activity = payload.get("activity") or {}
    if not activity:
        return ["<b>Son 24 saat</b>", "hareket kaydı yok"]

    hours = activity.get("hours", 24)
    opened = _int(activity.get("opened"))
    closed_count = int(activity.get("closed") or 0)
    lines = [
        f"<b>Son {hours} saat</b>",
        f"{opened} açılan · {closed_count} kapanan"
        + (f" ({activity.get('still_open')} hâlâ açık)" if activity.get("still_open") else ""),
    ]
    if closed_count:
        wins = int(activity.get("closed_wins") or 0)
        lines.append(
            f"kapananlar: ort. {_r(activity.get('closed_avg_r'))} · "
            f"{wins}/{closed_count} kazanç · PnL {_usd(activity.get('closed_pnl'))} USDT"
        )
        for trade in (activity.get("trades") or [])[:5]:
            lines.append(
                f"<code>{_esc(str(trade.get('direction', ''))[:5].ljust(5))}</code> "
                f"{_esc(trade.get('symbol', ''))} {_r(trade.get('r'))} "
                f"({_esc(trade.get('exit_reason', ''))}) — {_esc(trade.get('model', ''))}"
            )
    return lines


def _acceptance_block(
    payload: Mapping[str, Any], ranked: Sequence[Mapping[str, Any]]
) -> list[str]:
    """Kabul çıtası: iki kapıyı geçen var mı, yoksa en yakını hangisinde takıldı.

    "Geçen yok" satırını atlamak çıtayı görünmez kılardı; geçilememesi de bir sonuçtur
    (CLAUDE.md kural 15: hepsi pozitif getirse bile çıpayı geçemiyorsa cevap "stratejiler
    işe yarıyor" değildir).

    Band bir kapı DEĞİLDİR (bkz. core/metrics.py): geçen modelin yanında `⚠` olarak
    anılır. Onu "geçemedi" diye raporlamak, doğrulanmış bir modeli reddedilmiş gibi
    gösterirdi; hiç anmamak ise kıyasta dikkate alınması gereken maliyet farkını gizlerdi.
    """
    acceptance = payload.get("acceptance") or {}
    flags = acceptance.get("models") or []
    if not flags:
        return ["<b>Kabul çıtası</b>", "değerlendirilecek yarışmacı yok"]

    passed = [item for item in flags if item.get("passed")]
    if passed:
        names = ", ".join(
            f"<b>{_esc(item['model'])}</b>" + ("" if item.get("band", True) else " ⚠")
            for item in passed
        )
        lines = ["<b>Kabul çıtası</b>", f"✅ iki kapıyı da geçen: {names}"]
        if any(not item.get("band", True) for item in passed):
            lines.append(
                "<i>⚠ stop mesafesi yarışmacı bandının dışında: kıyasta maliyet farkı "
                "(cost_per_r) dikkate alınmalı — doğrulamayı engellemez</i>"
            )
        return lines

    order = {row["model"]: index for index, row in enumerate(ranked)}
    best = max(
        flags,
        key=lambda item: (
            sum(bool(item.get(key)) for key in ("sample", "edge")),
            -order.get(item["model"], len(flags)),
        ),
    )
    gates = " ".join(
        f"{letter}{'✓' if best.get(key) else '✗'}"
        for key, letter in (("sample", "Ö"), ("edge", "E"))
    )
    return [
        "<b>Kabul çıtası</b>",
        f"geçen model yok — en yakını <b>{_esc(best['model'])}</b> ({gates})",
        _gate_hint(best, acceptance),
    ]


def _gate_hint(flags: Mapping[str, Any], acceptance: Mapping[str, Any]) -> str:
    """Takılınan KAPInın sayıları. Band buraya girmez — o bir kapı değil, uyarıdır."""
    if not flags.get("sample"):
        return (
            f"örneklem {_int(flags.get('measured_trades'))}/"
            f"{_int(flags.get('min_trades') or acceptance.get('min_trades'))} işlem"
        )
    margin = _num(flags.get("edge_margin_r"))
    if margin is None:
        margin = _num(acceptance.get("edge_margin_r"))
    return (
        f"ort. R {_r(flags.get('avg_r'))} · kontrol {_r(flags.get('control_avg_r'))} "
        f"(gereken marj {'—' if margin is None else f'{margin:.2f}R'}) · "
        f"getiri {_pct(flags.get('total_return'), 2)} · çıpa {_pct(flags.get('benchmark_return'), 2)}"
    )


def _benchmark_block(
    payload: Mapping[str, Any], competitors: Sequence[Mapping[str, Any]]
) -> list[str]:
    names = set(payload.get("benchmarks") or ())
    rows = [item for item in (payload.get("models") or []) if item.get("model") in names]
    if not rows:
        return []
    lines = ["<b>Referans (kural 15)</b>"]
    for item in rows:
        account = item.get("account") or {}
        lines.append(
            f"{_esc(item['model'])} {_pct(account.get('total_return'), 2)} "
            f"(maxDD {_pct(account.get('max_drawdown'), 2)})"
        )
    # Çıpayı geçmek bir GETİRİ sorusudur (kural 15), ortalama R sorusu değil: burada
    # sıralamanın lideri değil, en yüksek getirili yarışmacı karşılaştırılır.
    scored = [
        (value, row["model"])
        for row in competitors
        if (value := _num((row.get("account") or {}).get("total_return"))) is not None
    ]
    if scored:
        value, model = max(scored)
        lines.append(f"en yüksek yarışmacı getirisi: {_esc(model)} {_pct(value, 2)}")
    return lines


def _competitors(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Yarışmacı satırları. Referans çıpası (kural 15) ve dış sistem kopyası (kural 15b) girmez.

    İkisi de kendi boyutlandırma kuralıyla koşar, yani 1R'leri yarışmacılarınkiyle aynı
    birim değildir; ortalama R sıralamasına sokmak `core/metrics.py`nin tam da dışarıda
    bıraktığı kıyası özette geri getirirdi.

    Kontrol grubu girer ve `_control` ile işaretlenir: bilgisiz çekilişin ilk üçte
    olması özetin taşıması gereken bir bilgidir, gizlenecek bir kusur değil.
    """
    control = (payload.get("acceptance") or {}).get("control_model")
    replicas = set(payload.get("replicas") or ())
    rows = [
        {
            **item,
            "model": item.get("model") or item.get("name"),
            "_control": (item.get("model") or item.get("name")) == control,
        }
        for item in (payload.get("models") or [])
        if not item.get("is_benchmark")
        and not item.get("is_replica")
        and (item.get("model") or item.get("name")) not in replicas
    ]
    return [row for row in rows if row["model"]]


def _ranked(competitors: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Ortalama R'si ÖLÇÜLEBİLEN yarışmacılar, büyükten küçüğe.

    Ölçülemeyenler listeden düşer, sona eklenmez: `nan` bir sıra değeri değildir ve
    "sıfır R" ile aynı hücreye yazılamaz (bkz. core/metrics.py).
    """
    measured = [
        (value, row)
        for row in competitors
        if (value := _num((row.get("total") or {}).get("avg_r"))) is not None
    ]
    measured.sort(key=lambda pair: (-pair[0], pair[1]["model"]))
    return [dict(row) for _, row in measured]


def _dashboard_url() -> str:
    """GitHub Pages adresi, `GITHUB_REPOSITORY` varsa. Yoksa mesaja link eklenmez."""
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    if "/" not in repository:
        return ""
    owner, _, name = repository.partition("/")
    return f"https://{owner}.github.io/{name}/"


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


def _r(value: Any) -> str:
    number = _num(value)
    return "—" if number is None else f"{number:+.2f}R"


def _rate(value: Any, digits: int = 1) -> str:
    """Oran (kazanma yüzdesi): İŞARETSİZ. `+%51.5` bir değişim gibi okunurdu."""
    number = _num(value)
    return "—" if number is None else f"{number * 100:.{digits}f}%"


def _pct(value: Any, digits: int = 1) -> str:
    number = _num(value)
    return "—" if number is None else f"{number * 100:+.{digits}f}%"


def _pct_raw(value: Any, digits: int = 2) -> str:
    number = _num(value)
    return "—" if number is None else f"{number:.{digits}f}%"


def _usd(value: Any) -> str:
    number = _num(value)
    return "—" if number is None else f"{number:+.2f}"


def _int(value: Any) -> str:
    number = _num(value)
    return "—" if number is None else f"{int(number)}"


def _esc(value: Any) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def _report_age_hours(payload: Mapping[str, Any]) -> float | None:
    """`generated_at`'ten bu yana geçen saat; damga okunamazsa None (kapı uygulanmaz)."""
    stamp = str(payload.get("generated_at", ""))
    if not stamp:
        return None
    try:
        generated = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    if generated.tzinfo is None:
        generated = generated.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - generated).total_seconds() / 3600.0


def _as_of_hour(payload: Mapping[str, Any]) -> int | None:
    """`as_of`'un UTC saati. Tarih kütüphanesi kullanmaz: ISO metninin saat alanı yeter."""
    as_of = str(payload.get("as_of", ""))
    if len(as_of) < 13 or as_of[10] != "T":
        return None
    try:
        return int(as_of[11:13])
    except ValueError:
        return None


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Günlük Telegram özeti yollar.")
    parser.add_argument("--metrics", default=str(METRICS_PATH), help="metrics.json yolu")
    parser.add_argument(
        "--hour", type=int, default=DEFAULT_HOUR,
        help=f"özetin yollanacağı as_of saati (UTC, varsayılan {DEFAULT_HOUR})",
    )
    parser.add_argument(
        "--max-age-hours", type=float, default=MAX_REPORT_AGE_HOURS,
        help=f"rapor bundan eskiyse yollama (varsayılan {MAX_REPORT_AGE_HOURS})",
    )
    parser.add_argument("--force", action="store_true", help="saat ve tazelik kapılarını atla")
    parser.add_argument("--dry-run", action="store_true", help="yollama, mesajı yazdır")
    parser.add_argument(
        "--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"]
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    sys.exit(main())
