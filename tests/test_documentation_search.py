from __future__ import annotations

import json
from pathlib import Path

import pytest

from ngsolve_gui.documentation_search import (
    DocumentationSearch,
    DocumentationSearchError,
    INDEX_SCHEMA,
    INDEX_SCHEMA_VERSION,
    tokens,
)


VERSION = "0.2.8.dev333"


def _write_site(root: Path, *, version=VERSION):
    pages = {
        "user_guide/studies.html": (
            "studies",
            "Harmonic frequency configuration",
            "Reuse a valid DC background field for a coupled study after checking its source model and dependency signature.",
        ),
        "user_guide/axisymmetric.html": (
            "axisymmetric",
            "Axisymmetric frequency list",
            "Enter finite positive frequency values or expressions evaluated using the axisymmetric model parameters.",
        ),
        "user_guide/rom.html": (
            "rom",
            "Mechanical modal ROM enrichment",
            "The 3D static load and boundary enrichment option adds static responses for configured loads and supports.",
        ),
        "user_guide/cad.html": (
            "cad",
            "Recover named selections after CAD changes",
            "Review the unresolved assignment and select the intended body or face to recover its CAD selection.",
        ),
        "advanced_reference/notebooks/benchmark.html": (
            "benchmark",
            "Magnetic sphere benchmark",
            "The magnetic sphere reference compares the computed radial field with the analytical solution.",
        ),
        "advanced_reference/notebooks/benchmark-copy.html": (
            "benchmark-copy",
            "Magnetic sphere benchmark",
            "The magnetic sphere notebook records a second view of the analytical comparison.",
        ),
        "reference/currents.html": (
            "currents",
            "Lorentz current components",
            "The complex Lorentz current JL_x is reported in the Cartesian component convention.",
        ),
    }
    entries = []
    for page, (fragment, title, text) in pages.items():
        target = root / page
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f'<html><section id="{fragment}"><h2>{title}</h2><p>{text}</p></section></html>', encoding="utf-8")
        page_title = "Engineering guide" if "user_guide" in page else "Advanced reference"
        entries.append({
            "title": title,
            "page_title": page_title,
            "page": page,
            "fragment": fragment,
            "text": text,
        })
    payload = {
        "schema": INDEX_SCHEMA,
        "schema_version": INDEX_SCHEMA_VERSION,
        "version": version,
        "pages": len(pages),
        "entries": entries,
    }
    (root / "search-index.json").write_text(json.dumps(payload), encoding="utf-8")
    return entries


def test_search_ranks_engineering_workflow_and_technical_reference(tmp_path):
    _write_site(tmp_path)
    engine = DocumentationSearch(tmp_path, VERSION)

    assert not engine.loaded
    dc = engine.search("DC background reuse")
    assert dc[0]["title"] == "Harmonic frequency configuration"
    assert "Reuse a valid DC background field" in dc[0]["excerpt"]

    frequency = engine.search("axisymmetric frequency ENTRY")
    assert frequency[0]["title"] == "Axisymmetric frequency list"
    assert "expressions" in frequency[0]["excerpt"]

    rom = engine.search("ROM enrichment")
    assert rom[0]["title"] == "Mechanical modal ROM enrichment"

    cad = engine.search("CAD assignment recovery")
    assert cad[0]["title"] == "Recover named selections after CAD changes"

    benchmark = engine.search("magnetic sphere benchmark analytical")
    assert benchmark[0]["title"] == "Magnetic sphere benchmark"
    assert len({result["title"].casefold() for result in benchmark}) == len(benchmark)
    assert len(benchmark) <= 5

    current = engine.search("J_Lx")
    assert current[0]["title"] == "Lorentz current components"
    assert engine.loaded
    assert engine.last_load_seconds >= 0
    assert engine.last_search_seconds >= 0


def test_empty_query_is_lazy_and_no_match_is_empty(tmp_path):
    _write_site(tmp_path)
    engine = DocumentationSearch(tmp_path, VERSION)

    assert engine.search(" !!! ") == []
    assert not engine.loaded
    assert engine.search("unobtainium quux zzzness") == []
    assert engine.loaded


def test_local_result_url_validates_root_page_and_real_heading(tmp_path):
    _write_site(tmp_path)
    engine = DocumentationSearch(tmp_path, VERSION)
    result = engine.search("CAD assignment recovery")[0]

    url = engine.local_url(result)
    assert url.casefold().startswith(
        ((tmp_path / "user_guide" / "cad.html").as_uri() + "#").casefold()
    )
    assert "cad" in url.rsplit("#", 1)[1]

    with pytest.raises(DocumentationSearchError, match="heading is no longer available"):
        engine.local_url({**result, "fragment": "missing-heading"})
    with pytest.raises(DocumentationSearchError, match="path or heading is invalid"):
        engine.local_url({**result, "page": "../outside.html"})


@pytest.mark.parametrize("payload, expected_message", [
    (None, "missing or invalid"),
    ({"schema": "other", "schema_version": 1, "version": VERSION, "entries": []}, "unsupported format"),
    ({"schema": INDEX_SCHEMA, "schema_version": INDEX_SCHEMA_VERSION, "version": "old", "entries": []}, "does not match"),
])
def test_missing_invalid_and_mismatched_indexes_fail_safely(tmp_path, payload, expected_message):
    if payload is not None:
        (tmp_path / "search-index.json").write_text(json.dumps(payload), encoding="utf-8")
    engine = DocumentationSearch(tmp_path, VERSION)
    with pytest.raises(DocumentationSearchError, match=expected_message):
        engine.search("physics")


def test_unsafe_or_missing_rows_are_skipped_without_leaving_site_root(tmp_path):
    entries = _write_site(tmp_path)
    payload_path = tmp_path / "search-index.json"
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    payload["entries"].append({
        "title": "Unsafe entry", "page_title": "External", "page": "../outside.html",
        "fragment": "outside", "text": "outside site",
    })
    payload["entries"].append({
        "title": "Missing page", "page_title": "Missing", "page": "gone.html",
        "fragment": "gone", "text": "not shipped",
    })
    payload_path.write_text(json.dumps(payload), encoding="utf-8")

    engine = DocumentationSearch(tmp_path, VERSION)
    results = engine.search("unsafe outside site")
    assert results == []
    assert engine.skipped_entries == 2


def test_tokens_handle_punctuation_acronyms_and_compound_terms():
    assert tokens("J_Lx, B_x time-harmonic ROM") == ["jlx", "bx", "time", "harmonic", "rom"]
