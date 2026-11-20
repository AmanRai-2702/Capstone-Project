import json
from pathlib import Path

import pytest

from repomind.analyzers.css_analyzer import CSSAnalyzer
from repomind.analyzers.html_analyzer import HTMLAnalyzer
from repomind.models import EntityType, RelationKind, RelationType

LOGIN_APP = Path(__file__).resolve().parents[2] / "test_apps" / "login_app"
STYLE_CSS_PATH = LOGIN_APP / "static" / "style.css"
INDEX_HTML_PATH = LOGIN_APP / "static" / "index.html"


@pytest.fixture
def analyzer() -> CSSAnalyzer:
    return CSSAnalyzer()


@pytest.fixture
def html_elements() -> list:
    html_analyzer = HTMLAnalyzer()
    res = html_analyzer.analyze_file(
        "static/index.html",
        INDEX_HTML_PATH.read_text(encoding="utf-8"),
    )
    return res.entities_by_type(EntityType.HTML_ELEMENT)


def test_analyze_css_entities(analyzer: CSSAnalyzer):
    code = STYLE_CSS_PATH.read_text(encoding="utf-8")
    result = analyzer.analyze_file("static/style.css", code)

    ids = result.entity_ids()
    assert "css:static/style.css:.login-card" in ids
    assert "css:static/style.css:.field" in ids
    assert "css:static/style.css:.btn" in ids
    assert "css:static/style.css:.btn.primary" in ids
    assert "css:static/style.css:.message" in ids
    assert "css:static/style.css:.message.error" in ids
    assert "css:static/style.css:.unused-banner" in ids

    selectors = result.entities_by_type(EntityType.CSS_SELECTOR)
    assert len(selectors) == 7


def test_analyze_css_styles_relationships(analyzer: CSSAnalyzer, html_elements: list):
    code = STYLE_CSS_PATH.read_text(encoding="utf-8")
    result = analyzer.analyze_file("static/style.css", code, html_elements=html_elements)

    styles = [
        (r.source_id, r.target_id, r.kind)
        for r in result.relationships
        if r.type == RelationType.STYLES
    ]
    styles_map = {(src, tgt): kind for src, tgt, kind in styles}

    # Verify detected exact matches
    assert ("css:static/style.css:.login-card", "html:static/index.html:#login-card") in styles_map
    assert styles_map[("css:static/style.css:.login-card", "html:static/index.html:#login-card")] == RelationKind.DETECTED

    assert ("css:static/style.css:.field", "html:static/index.html:#username") in styles_map
    assert ("css:static/style.css:.field", "html:static/index.html:#password") in styles_map
    assert ("css:static/style.css:.btn", "html:static/index.html:#login-btn") in styles_map
    assert ("css:static/style.css:.btn", "html:static/index.html:#profile-btn") in styles_map
    assert ("css:static/style.css:.btn.primary", "html:static/index.html:#login-btn") in styles_map
    assert ("css:static/style.css:.message", "html:static/index.html:#message") in styles_map

    # Verify inferred dynamic match (.message.error)
    assert ("css:static/style.css:.message.error", "html:static/index.html:#message") in styles_map
    assert styles_map[("css:static/style.css:.message.error", "html:static/index.html:#message")] == RelationKind.INFERRED

    # Verify unused selector produces no styles relationship
    unused_targets = [tgt for src, tgt, _ in styles if ".unused-banner" in src]
    assert unused_targets == []


def test_css_ground_truth_matches(analyzer: CSSAnalyzer, html_elements: list):
    """Verify all CSS entities and STYLES relationships in ground_truth.json match."""
    gt = json.loads((LOGIN_APP / "ground_truth.json").read_text(encoding="utf-8"))
    code = STYLE_CSS_PATH.read_text(encoding="utf-8")
    result = analyzer.analyze_file("static/style.css", code, html_elements=html_elements)

    # 1. Expected CSS entities
    expected_entities = {
        eid for eid, _ in gt["entities"]
        if eid.startswith("css:")
    }
    missing_entities = expected_entities - result.entity_ids()
    assert not missing_entities, f"Missing CSS entities: {missing_entities}"

    # 2. Expected CSS STYLES relationships
    expected_rels = {
        (src, rtype, tgt, kind)
        for src, rtype, tgt, kind in gt["relationships"]
        if src.startswith("css:")
    }

    extracted_rels = {
        (r.source_id, r.type.value, r.target_id, r.kind.value)
        for r in result.relationships
        if r.type == RelationType.STYLES
    }

    missing_rels = expected_rels - extracted_rels
    assert not missing_rels, f"Missing CSS relationships: {missing_rels}"
