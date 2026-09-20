"""`scripts/probe_funding_depth.py`nin SAF mantığı: ağ erişimi yok, defter yok.

Sınanan şey probe'un dört sözleşmesidir: (1) (a)/(b) ayrımı MEKANİKTİR ve ölçütü
"daha eski kayıt döndü mü"dür, "kayıt döndü mü" değil; (2) belirsiz bir sonuç
sessizce "(a) borsa tabanı"na düşmez; (3) probe yalnızca DAMGA okur, oran alanına
hiç dokunmaz; (4) ölçümün parçası değildir — R/PnL üreten modülleri import etmez.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from scripts.probe_funding_depth import (
    SingleRequest,
    SymbolProbe,
    _older_than,
    _span,
    _stamps,
    classify_depth_floor,
    main,
)

WALK_OLDEST = pd.Timestamp("2026-06-11T08:00:00+00:00")


def _request(label: str, stamps: list[str], *, error: str = "") -> SingleRequest:
    parsed = tuple(pd.Timestamp(s) for s in stamps)
    return SingleRequest(label=label, count=len(parsed), stamps=parsed, error=error)


def _probe(
    *,
    resume: SingleRequest | None = None,
    deep_jump: SingleRequest | None = None,
    walk_oldest: pd.Timestamp | None = WALK_OLDEST,
    walk_error: str = "",
) -> SymbolProbe:
    return SymbolProbe(
        symbol="BTC-USDT-SWAP",
        walk_records=312,
        walk_oldest=walk_oldest,
        walk_newest=pd.Timestamp("2026-09-19T08:00:00+00:00"),
        walk_error=walk_error,
        resume=resume,
        deep_jump=deep_jump,
        before_jump=None,
        limits=(),
        endpoints=(),
    )


# --------------------------------------------------------------------------- #
# Damga okuma — oran alanına DOKUNULMAZ
# --------------------------------------------------------------------------- #
def test_stamps_reads_only_the_time_field():
    page = [
        {"fundingTime": "1750000000000", "fundingRate": "0.0001"},
        {"fundingTime": "1750028800000", "fundingRate": "-0.0009"},
    ]
    stamps = _stamps(page)
    assert stamps == [
        pd.Timestamp(1750000000000, unit="ms", tz="UTC"),
        pd.Timestamp(1750028800000, unit="ms", tz="UTC"),
    ]


def test_stamps_skips_rows_without_a_time_field():
    assert _stamps([{"fundingRate": "0.0001"}]) == []


def test_span_of_empty_page_is_not_a_fabricated_range():
    assert _span([]) == "kayıt yok"


# --------------------------------------------------------------------------- #
# Ayrımın ÖLÇÜTÜ: "daha eski kayıt döndü mü", "kayıt döndü mü" DEĞİL
# --------------------------------------------------------------------------- #
def test_newest_page_is_not_evidence_of_depth():
    """`after` yok sayılırsa uç nokta en TAZE sayfayı döndürür; o derinlik kanıtı değildir."""
    newest_page = _request("after=...", ["2026-09-18T08:00:00+00:00"])
    assert _older_than(newest_page, WALK_OLDEST) is False
    assert classify_depth_floor(_probe(resume=newest_page)) == "(a) borsa tabanı"


def test_strictly_older_record_proves_the_walk_stopped_early():
    older = _request("after=2026-06-11", ["2026-06-11T00:00:00+00:00"])
    assert _older_than(older, WALK_OLDEST) is True
    assert classify_depth_floor(_probe(resume=older)) == "(b) yürüyüş tabanı"


def test_deep_jump_alone_is_enough_for_the_walk_verdict():
    """Devam isteği boş dönse bile, 2024'e doğrudan atlayan istek kayıt getiriyorsa (b)."""
    probe = _probe(
        resume=_request("after=2026-06-11", []),
        deep_jump=_request("after=2024-06-30", ["2024-06-29T16:00:00+00:00"]),
    )
    assert classify_depth_floor(probe) == "(b) yürüyüş tabanı"


def test_both_jumps_empty_means_the_floor_is_the_exchange():
    probe = _probe(
        resume=_request("after=2026-06-11", []),
        deep_jump=_request("after=2024-06-30", []),
    )
    assert classify_depth_floor(probe) == "(a) borsa tabanı"


# --------------------------------------------------------------------------- #
# Belirsizlik sessizce "(a)"ya DÜŞMEZ
# --------------------------------------------------------------------------- #
def test_request_error_yields_inconclusive_not_a_floor():
    probe = _probe(
        resume=_request("after=2026-06-11", [], error="OKXError: HTTP 403"),
        deep_jump=_request("after=2024-06-30", []),
    )
    assert probe.errors
    assert classify_depth_floor(probe) == "belirsiz"


def test_walk_error_yields_inconclusive():
    probe = _probe(walk_oldest=None, walk_error="OKXError: 3 denemede başarısız")
    assert classify_depth_floor(probe) == "belirsiz"


def test_empty_walk_without_error_is_still_inconclusive():
    """Hiç kayıt gelmediyse karşılaştıracak referans yoktur; taban iddia edilemez."""
    assert classify_depth_floor(_probe(walk_oldest=None)) == "belirsiz"


# --------------------------------------------------------------------------- #
# Evren kapısı
# --------------------------------------------------------------------------- #
def test_symbol_outside_universe_is_refused():
    assert main(["--symbols", "NOTALISTED-USDT-SWAP"]) == 2


# --------------------------------------------------------------------------- #
# Probe ölçümün parçası DEĞİL
# --------------------------------------------------------------------------- #
def _source() -> str:
    return Path("scripts/probe_funding_depth.py").read_text(encoding="utf-8")


def test_module_does_not_import_measurement_or_strategy_modules():
    text = _source()
    for banned in ("core.portfolio", "core.metrics", "core.ledger", "strategies."):
        assert f"import {banned}" not in text and f"from {banned}" not in text


def test_module_never_names_the_rate_field():
    """Rapor meta veri taşır: damga, sayı, durum. Bir ORAN okumanın yolu kapalıdır."""
    assert "fundingRate" not in _source()


def test_module_reuses_the_real_walk_instead_of_reimplementing_it():
    """Ölçülen şey fetch_history'nin DAVRANIŞIDIR; kopya bir yürüyüş ondan ayrışabilirdi."""
    assert "from scripts.measure_funding import" in _source()
