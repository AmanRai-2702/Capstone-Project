import json
from pathlib import Path

import pytest

from repomind.analyzers.javascript_analyzer import JavaScriptAnalyzer
from repomind.models import EntityType, RelationKind, RelationType

LOGIN_APP = Path(__file__).resolve().parents[2] / "test_apps" / "login_app"
LOGIN_JS_PATH = LOGIN_APP / "static" / "login.js"


@pytest.fixture
def analyzer() -> JavaScriptAnalyzer:
    return JavaScriptAnalyzer()


def test_analyze_login_js_entities(analyzer: JavaScriptAnalyzer):
    code = LOGIN_JS_PATH.read_text(encoding="utf-8")
    result = analyzer.analyze_file("static/login.js", code)

    ids = result.entity_ids()
    assert "javascript:static/login.js:showMessage" in ids
    assert "javascript:static/login.js:handleLogin" in ids
    assert "javascript:static/login.js:loadProfile" in ids

    funcs = result.entities_by_type(EntityType.FUNCTION)
    assert len(funcs) == 4                      # 3 named functions + the anonymous #profile-btn click handler
    anonymous = [f for f in funcs if f.metadata.get("anonymous")]
    assert [f.name for f in anonymous] == ["#profile-btn click handler"]


def test_analyze_login_js_calls(analyzer: JavaScriptAnalyzer):
    code = LOGIN_JS_PATH.read_text(encoding="utf-8")
    result = analyzer.analyze_file("static/login.js", code)

    calls = [r for r in result.relationships if r.type == RelationType.CALLS]
    call_pairs = {(r.source_id, r.target_id) for r in calls}

    assert (
        "javascript:static/login.js:handleLogin",
        "javascript:static/login.js:showMessage",
    ) in call_pairs
    assert (
        "javascript:static/login.js:loadProfile",
        "javascript:static/login.js:showMessage",
    ) in call_pairs


def test_analyze_login_js_requests(analyzer: JavaScriptAnalyzer):
    code = LOGIN_JS_PATH.read_text(encoding="utf-8")
    result = analyzer.analyze_file("static/login.js", code)

    requests = [r for r in result.relationships if r.type == RelationType.REQUESTS]
    req_map = {r.source_id: (r.target_id, r.kind, r.confidence) for r in requests}

    assert "javascript:static/login.js:handleLogin" in req_map
    tgt, kind, conf = req_map["javascript:static/login.js:handleLogin"]
    assert tgt == "api:POST:/api/login"
    assert kind == RelationKind.DETECTED
    assert conf == 1.0

    assert "javascript:static/login.js:loadProfile" in req_map
    tgt2, kind2, conf2 = req_map["javascript:static/login.js:loadProfile"]
    assert tgt2 == "api:GET:/api/users/{user_id}"
    assert kind2 == RelationKind.INFERRED
    assert conf2 == 0.85


def test_analyze_login_js_dom_references(analyzer: JavaScriptAnalyzer):
    code = LOGIN_JS_PATH.read_text(encoding="utf-8")
    result = analyzer.analyze_file("static/login.js", code)

    refs = [r for r in result.relationships if r.type == RelationType.REFERENCES]
    ref_pairs = {(r.source_id, r.target_id) for r in refs}

    # Top-level DOM queries
    assert ("file:static/login.js", "html:static/index.html:#login-form") in ref_pairs
    assert ("file:static/login.js", "html:static/index.html:#message") in ref_pairs
    assert ("file:static/login.js", "html:static/index.html:#profile-btn") in ref_pairs

    # In-function DOM queries
    assert (
        "javascript:static/login.js:handleLogin",
        "html:static/index.html:#username",
    ) in ref_pairs
    assert (
        "javascript:static/login.js:handleLogin",
        "html:static/index.html:#password",
    ) in ref_pairs


def test_analyze_login_js_triggers(analyzer: JavaScriptAnalyzer):
    code = LOGIN_JS_PATH.read_text(encoding="utf-8")
    result = analyzer.analyze_file("static/login.js", code)

    triggers = [r for r in result.relationships if r.type == RelationType.TRIGGERS]
    trig_map = {r.source_id: (r.target_id, r.kind) for r in triggers}

    # Direct handler reference: form.addEventListener("submit", handleLogin)
    assert "html:static/index.html:#login-form" in trig_map
    assert trig_map["html:static/index.html:#login-form"] == (
        "javascript:static/login.js:handleLogin",
        RelationKind.DETECTED,
    )

    # Arrow function callback: document.getElementById("profile-btn").addEventListener("click", () => loadProfile(1))
    # The callback is an entity of its own: the element triggers it, and it calls loadProfile.
    assert trig_map["html:static/index.html:#profile-btn"] == (
        "javascript:static/login.js:handler@35",
        RelationKind.DETECTED,
    )
    calls = {(r.source_id, r.target_id) for r in result.relationships if r.type == RelationType.CALLS}
    assert ("javascript:static/login.js:handler@35", "javascript:static/login.js:loadProfile") in calls


def test_javascript_ground_truth_matches(analyzer: JavaScriptAnalyzer):
    """Verify all JS entities and JS-related relationships in ground_truth.json match."""
    gt = json.loads((LOGIN_APP / "ground_truth.json").read_text(encoding="utf-8"))
    code = LOGIN_JS_PATH.read_text(encoding="utf-8")
    result = analyzer.analyze_file("static/login.js", code)

    # 1. Expected JS entities
    expected_js_entities = {
        eid for eid, _ in gt["entities"]
        if eid.startswith("javascript:")
    }
    extracted_entities = result.entity_ids()
    missing_entities = expected_js_entities - extracted_entities
    assert not missing_entities, f"Missing JS entities: {missing_entities}"

    # 2. Expected relationships where JS is source or target (excluding HTML CONTAINS)
    expected_js_rels = {
        (src, rtype, tgt, kind)
        for src, rtype, tgt, kind in gt["relationships"]
        if (src.startswith("javascript:") or src == "file:static/login.js" or (rtype == "TRIGGERS" and tgt.startswith("javascript:")))
    }

    extracted_rels = {
        (r.source_id, r.type.value, r.target_id, r.kind.value)
        for r in result.relationships
    }

    missing_rels = expected_js_rels - extracted_rels
    assert not missing_rels, f"Missing JS relationships: {missing_rels}"


def test_chained_add_event_listener_is_not_a_dom_reference(analyzer: JavaScriptAnalyzer):
    code = 'document.getElementById("profile-btn").addEventListener("click", () => {});'
    result = analyzer.analyze_file("static/app.js", code)
    targets = {r.target_id for r in result.relationships}
    assert not any(t.endswith("#click") for t in targets)
    assert "html:static/index.html:#profile-btn" in targets


def test_class_toggle_is_recorded_as_dynamic_class(analyzer: JavaScriptAnalyzer):
    code = LOGIN_JS_PATH.read_text(encoding="utf-8")
    result = analyzer.analyze_file("static/login.js", code)
    assert result.dynamic_classes == {"message": {"error"}}
