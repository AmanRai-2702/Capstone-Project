import json
from pathlib import Path

import pytest

from repomind.analyzers.html_analyzer import HTMLAnalyzer
from repomind.models import EntityType, RelationKind, RelationType

LOGIN_APP = Path(__file__).resolve().parents[2] / "test_apps" / "login_app"
INDEX_HTML_PATH = LOGIN_APP / "static" / "index.html"


@pytest.fixture
def analyzer() -> HTMLAnalyzer:
    return HTMLAnalyzer()


def test_analyze_html_entities(analyzer: HTMLAnalyzer):
    code = INDEX_HTML_PATH.read_text(encoding="utf-8")
    result = analyzer.analyze_file("static/index.html", code)

    ids = result.entity_ids()
    assert "html:static/index.html:#login-card" in ids
    assert "html:static/index.html:#login-form" in ids
    assert "html:static/index.html:#username" in ids
    assert "html:static/index.html:#password" in ids
    assert "html:static/index.html:#login-btn" in ids
    assert "html:static/index.html:#message" in ids
    assert "html:static/index.html:#profile-btn" in ids

    elements = result.entities_by_type(EntityType.HTML_ELEMENT)
    assert len(elements) == 7


def test_analyze_html_containment(analyzer: HTMLAnalyzer):
    code = INDEX_HTML_PATH.read_text(encoding="utf-8")
    result = analyzer.analyze_file("static/index.html", code)

    contains_rels = [
        (r.source_id, r.target_id)
        for r in result.relationships
        if r.type == RelationType.CONTAINS and r.source_id.startswith("html:")
    ]

    expected_contains = {
        ("html:static/index.html:#login-card", "html:static/index.html:#login-form"),
        ("html:static/index.html:#login-card", "html:static/index.html:#message"),
        ("html:static/index.html:#login-card", "html:static/index.html:#profile-btn"),
        ("html:static/index.html:#login-form", "html:static/index.html:#username"),
        ("html:static/index.html:#login-form", "html:static/index.html:#password"),
        ("html:static/index.html:#login-form", "html:static/index.html:#login-btn"),
    }
    assert expected_contains <= set(contains_rels)


def test_analyze_html_asset_references(analyzer: HTMLAnalyzer):
    code = INDEX_HTML_PATH.read_text(encoding="utf-8")
    result = analyzer.analyze_file("static/index.html", code)

    ref_rels = [
        (r.source_id, r.target_id)
        for r in result.relationships
        if r.type == RelationType.REFERENCES and r.source_id == "file:static/index.html"
    ]

    assert ("file:static/index.html", "file:static/login.js") in ref_rels
    assert ("file:static/index.html", "file:static/style.css") in ref_rels


def test_html_ground_truth_matches(analyzer: HTMLAnalyzer):
    """Verify all HTML entities and HTML-based relationships in ground_truth.json match."""
    gt = json.loads((LOGIN_APP / "ground_truth.json").read_text(encoding="utf-8"))
    code = INDEX_HTML_PATH.read_text(encoding="utf-8")
    result = analyzer.analyze_file("static/index.html", code)

    # 1. Expected HTML entities
    expected_entities = {
        eid for eid, _ in gt["entities"]
        if eid.startswith("html:")
    }
    missing_entities = expected_entities - result.entity_ids()
    assert not missing_entities, f"Missing HTML entities: {missing_entities}"

    # 2. Expected HTML relationships (HTML asset refs and HTML CONTAINS)
    expected_rels = {
        (src, rtype, tgt)
        for src, rtype, tgt, _ in gt["relationships"]
        if src.startswith("html:") and rtype == "CONTAINS"
        or (src == "file:static/index.html" and rtype == "REFERENCES")
    }

    extracted_rels = {
        (r.source_id, r.type.value, r.target_id)
        for r in result.relationships
    }

    missing_rels = expected_rels - extracted_rels
    assert not missing_rels, f"Missing HTML relationships: {missing_rels}"
