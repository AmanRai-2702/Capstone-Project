import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from repomind.models import (
    Entity, EntityType, Evidence, Language, RelationKind, RelationType,
    Relationship, SourceLocation, make_endpoint_id, make_entity_id,
)

GROUND_TRUTH = (
    Path(__file__).resolve().parents[2]
    / "test_apps" / "login_app" / "ground_truth.json"
)


def test_make_entity_id():
    eid = make_entity_id("python", "services/auth_service.py", "AuthService.authenticate")
    assert eid == "python:services/auth_service.py:AuthService.authenticate"
    assert make_endpoint_id("post", "/api/login") == "api:POST:/api/login"


def test_make_entity_id_normalizes_paths():
    assert make_entity_id("python", "./a\\b.py", "f") == "python:a/b.py:f"


def test_entity_json_roundtrip():
    e = Entity(
        id=make_entity_id("python", "auth.py", "login"),
        type=EntityType.FUNCTION,
        name="login",
        qualified_name="login",
        language=Language.PYTHON,
        location=SourceLocation(file_path="auth.py", start_line=20, end_line=40),
    )
    assert Entity.model_validate_json(e.model_dump_json()) == e


def test_relationship_roundtrip_and_key():
    r = Relationship(
        source_id="javascript:login.js:login",
        target_id="api:POST:/api/login",
        type=RelationType.REQUESTS,
        kind=RelationKind.DETECTED,
        confidence=0.97,
        evidence=[Evidence(snippet="fetch('/api/login')")],
    )
    assert Relationship.model_validate_json(r.model_dump_json()) == r
    assert r.key == "javascript:login.js:login -REQUESTS-> api:POST:/api/login"


def test_confidence_out_of_range_rejected():
    with pytest.raises(ValidationError):
        Relationship(source_id="a", target_id="b", type=RelationType.CALLS, confidence=1.5)


def test_source_location_order_rejected():
    with pytest.raises(ValidationError):
        SourceLocation(file_path="x.py", start_line=10, end_line=5)


def test_ground_truth_is_consistent():
    gt = json.loads(GROUND_TRUTH.read_text())
    ids = set()
    for entity_id, etype in gt["entities"]:
        EntityType(etype)
        assert entity_id not in ids, f"duplicate id {entity_id}"
        ids.add(entity_id)
    for src, rtype, tgt, kind in gt["relationships"]:
        RelationType(rtype)
        RelationKind(kind)
        assert src in ids, f"unknown source {src}"
        assert tgt in ids, f"unknown target {tgt}"