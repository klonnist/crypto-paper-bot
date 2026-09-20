"""README ile config/workflow arasındaki bağ: belge bir niyet değil, bir kapı.

**Neden bu test var.** Karar 33 kadroyu sadeleştirirken `config.yaml` ve `CLAUDE.md`
güncellendi, README güncellenmedi: belge aylarca "10 farklı strateji modeli" demeye,
`5 0,4,8,12,16,20` cron'unu ve artık var olmayan bir sembolü (TON) anlatmaya devam etti.
Drift sessizdi çünkü onu yakalayacak bir şey yoktu — "belgeyi de güncelle" bir hatırlatmadır,
bir garanti değil.

Testin ölçtüğü şey içerik kalitesi değil, **kümelerin eşitliği**: README'nin AKTİF LİG
tablolarında yazan model adları katmanın `models` listesiyle birebir aynı olmalı, ve
KATALOG tablosu `strategies/registry.py`de kayıtlı olup listede olmayanların tamamını
saymalı. Bir model emekli edildiğinde ya da kadroya alındığında bu test kırmızıya döner ve
README'nin güncellenmesi bir seçenek olmaktan çıkar.

Test README'yi ÜRETMEZ, yalnızca karşılaştırır: üretilen bir tablo, yanındaki gerekçe
sütununu (hangi karar, neden emekli) taşıyamazdı — ve asıl değerli olan o sütundur.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from core.config import PROJECT_ROOT, load_config
from core.layers import resolve_layer
from strategies.registry import REGISTRY

README = PROJECT_ROOT / "README.md"

# README'deki tablo başlıkları. Bir bölüm yeniden adlandırılırsa test bunu da yakalar:
# testin bakamadığı bir tablo, korunmayan bir tablodur.
_ACTIVE_HEADINGS = {
    "base": "### Aktif lig — `base`",
    "scalp": "### Aktif lig — `scalp`",
    # `ema` katmanı TANIMLI ama tetikleyicisi yok (run-ema.yml henüz eklenmedi): kadro
    # backtest'in ölçtüğü kümedir, koşan bir lig değil. Başlık, kapılar geçilip workflow
    # eklendiğinde "Aktif lig" olarak yeniden adlandırılacak.
    "ema": "### Kadro — `ema` (tanımlı, tetikleyicisi YOK)",
}
_CATALOG_HEADING = "### Katalog — kayıtlı ama listede değil"


def _readme() -> str:
    return README.read_text(encoding="utf-8")


def _section(text: str, heading: str) -> str:
    """Bir başlıktan bir SONRAKİ `###`/`##` başlığına kadarki gövde."""
    start = text.index(heading) + len(heading)
    rest = text[start:]
    end = len(rest)
    for marker in ("\n## ", "\n### "):
        found = rest.find(marker)
        if found != -1:
            end = min(end, found)
    return rest[:end]


def _models_in(section: str) -> set[str]:
    """Tablo satırlarındaki `` `model_adi` `` hücrelerini toplar.

    Yalnızca İKİNCİ hücre okunur: gerekçe sütununda geçen model adları (örn. "karar 33'te
    scalp_fixed ile kıyaslandı") tabloya üye sayılmamalıdır.
    """
    names: set[str] = set()
    for line in section.splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) < 2:
            continue
        match = re.fullmatch(r"`([a-z_0-9]+)`", cells[1])
        if match:
            names.add(match.group(1))
    return names


def test_every_configured_layer_has_a_readme_section() -> None:
    """Katman listesi config'ten OKUNUR, bu dosyada elle tutulmaz.

    Bu testin kendisi bir kez kör kaldı: `_ACTIVE_HEADINGS` iki katmanı sayıyordu ve
    üçüncü bir katman eklendiğinde onun modelleri "kayıtlı ama hiçbir katmanda koşmuyor"
    sayıldı — yani belge kapısı, tam da korumak için yazıldığı drift'i üretti. Kapıyı
    koruyan şey artık bir liste değil, bir eşitlik.
    """
    configured = set(load_config()["layers"])
    assert configured == set(_ACTIVE_HEADINGS), (
        "config.yaml'daki katmanlar ile README bölümleri ayrışmış.\n"
        f"  bölümü olmayan katman: {sorted(configured - set(_ACTIVE_HEADINGS))}\n"
        f"  config'te olmayan bölüm: {sorted(set(_ACTIVE_HEADINGS) - configured)}"
    )


@pytest.mark.parametrize("layer_name", sorted(_ACTIVE_HEADINGS))
def test_readme_active_league_matches_config(layer_name: str) -> None:
    """AKTİF LİG tablosu = katmanın `models` listesi. Fazlası da eksiği de hatadır."""
    layer = resolve_layer(load_config(), layer_name)
    documented = _models_in(_section(_readme(), _ACTIVE_HEADINGS[layer_name]))
    configured = set(layer.models)

    assert documented == configured, (
        f"README '{_ACTIVE_HEADINGS[layer_name]}' tablosu config ile ayrışmış.\n"
        f"  yalnızca README'de: {sorted(documented - configured)}\n"
        f"  yalnızca config'te: {sorted(configured - documented)}"
    )


def test_readme_catalog_covers_every_registered_but_inactive_model() -> None:
    """KATALOG = kayıtlı olup hiçbir katmanda koşmayanların TAMAMI.

    Eksik bırakmak, kodu duran bir modeli belgede yok etmek olurdu — oysa emekli model
    silinmez (kural 1) ve backtest onu `--models` ile hâlâ çağırabilir.
    """
    config = load_config()
    active = {
        model
        for layer_name in _ACTIVE_HEADINGS
        for model in resolve_layer(config, layer_name).models
    }
    inactive = set(REGISTRY) - active
    documented = _models_in(_section(_readme(), _CATALOG_HEADING))

    assert documented == inactive, (
        "README katalog tablosu registry ile ayrışmış.\n"
        f"  yalnızca README'de: {sorted(documented - inactive)}\n"
        f"  yalnızca registry'de: {sorted(inactive - documented)}"
    )


def test_readme_quotes_the_real_cron_expressions() -> None:
    """Cron dizeleri workflow dosyasından okunur, belgeden değil.

    README bir zamanlar `5 0,4,8,12,16,20` yazıyordu; karar 39 cron'u saatliğe çektikten
    sonra da yazmaya devam etti. Bir kadans iddiası, o kadansı üreten dosyayla
    doğrulanabilir olmalıdır.
    """
    workflow = (PROJECT_ROOT / ".github/workflows/run.yml").read_text(encoding="utf-8")
    crons = re.findall(r"- cron:\s*\"([^\"]+)\"", workflow)
    assert crons, "run.yml'de cron yok: README'nin iddiası da güncellenmeli"

    readme = _readme()
    for cron in crons:
        assert f"`{cron}`" in readme, f"run.yml'deki '{cron}' cron'u README'de geçmiyor"


def test_readme_does_not_advertise_a_scalp_cron_that_does_not_exist() -> None:
    """`run-scalp.yml`de `schedule:` YOKTUR (ölçüldü: 15 dk cron'un ~%91'i düşüyordu).

    Belgenin var olmayan bir cron'u anlatması, okuyucuya turların garanti altında
    olduğunu söylerdi.
    """
    workflow = (PROJECT_ROOT / ".github/workflows/run-scalp.yml").read_text(encoding="utf-8")
    # Yorum satırları gerekçeyi anlatmak için "schedule:" yazabilir; aranan şey ETKİN
    # bir anahtardır.
    active = [
        line for line in workflow.splitlines()
        if line.strip().startswith("schedule:")
    ]
    assert not active, f"run-scalp.yml'de etkin schedule var: {active}"
    assert "cron YOK" in _readme()


def test_readme_scalp_universe_matches_config() -> None:
    """Evrenin KAÇ sembol olduğu ve hangileri olduğu tek kaynaktan okunur.

    README bir zamanlar "sabit 14 sembol" diyor ve listeye TON'u koyuyordu; TON OKX'te
    yok (51001) ve config'ten çıkarılmıştı. Belgedeki evren ile turun gerçekte gördüğü
    küme ayrışırsa, "aynı evren" iddiası (kural 6) denetlenemez hâle gelir.
    """
    universe = resolve_layer(load_config(), "scalp").symbols
    readme = _readme()

    assert f"sabit {len(universe)} sembol" in readme
    for symbol in universe:
        base = symbol.split("-")[0]
        assert re.search(rf"\b{re.escape(base)}\b", readme), f"{base} README'de yok"
