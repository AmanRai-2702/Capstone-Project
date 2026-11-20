import json
from pathlib import Path

import pytest

from repomind.analyzers.python_analyzer import PythonAnalyzer
from repomind.models import EntityType, RelationKind, RelationType

LOGIN_APP = Path(__file__).resolve().parents[2] / "test_apps" / "login_app"


@pytest.fixture
def analyzer() -> PythonAnalyzer:
    return PythonAnalyzer()


def test_analyze_login_app_app_py(analyzer: PythonAnalyzer):
    path = LOGIN_APP / "app.py"
    result = analyzer.analyze_file("app.py", path.read_text(encoding="utf-8"))

    # Entity checks
    ids = result.entity_ids()
    assert "python:app.py:LoginRequest" in ids
    assert "python:app.py:index" in ids
    assert "python:app.py:login" in ids
    assert "python:app.py:get_user" in ids

    # API Endpoint checks
    assert "api:GET:/" in ids
    assert "api:POST:/api/login" in ids
    assert "api:GET:/api/users/{user_id}" in ids

    # Relationship checks: HANDLES
    handles_rels = [r for r in result.relationships if r.type == RelationType.HANDLES]
    assert len(handles_rels) == 3
    rel_map = {r.source_id: r.target_id for r in handles_rels}
    assert rel_map["python:app.py:index"] == "api:GET:/"
    assert rel_map["python:app.py:login"] == "api:POST:/api/login"
    assert rel_map["python:app.py:get_user"] == "api:GET:/api/users/{user_id}"

    # Inferred inter-module calls
    calls_rels = [r for r in result.relationships if r.type == RelationType.CALLS]
    calls_map = {(r.source_id, r.target_id) for r in calls_rels}
    assert ("python:app.py:login", "python:services/auth_service.py:AuthService.authenticate") in calls_map
    assert ("python:app.py:get_user", "python:services/auth_service.py:AuthService.get_user") in calls_map


def test_analyze_auth_service(analyzer: PythonAnalyzer):
    path = LOGIN_APP / "services" / "auth_service.py"
    result = analyzer.analyze_file("services/auth_service.py", path.read_text(encoding="utf-8"))

    ids = result.entity_ids()
    assert "python:services/auth_service.py:AuthService" in ids
    assert "python:services/auth_service.py:AuthService.__init__" in ids
    assert "python:services/auth_service.py:AuthService.authenticate" in ids
    assert "python:services/auth_service.py:AuthService.get_user" in ids

    # Calls UserRepository methods
    calls_rels = [r for r in result.relationships if r.type == RelationType.CALLS]
    calls_map = {(r.source_id, r.target_id) for r in calls_rels}
    assert (
        "python:services/auth_service.py:AuthService.authenticate",
        "python:repositories/user_repository.py:UserRepository.find_by_username",
    ) in calls_map
    assert (
        "python:services/auth_service.py:AuthService.get_user",
        "python:repositories/user_repository.py:UserRepository.find_by_id",
    ) in calls_map


def test_analyze_user_repository(analyzer: PythonAnalyzer):
    path = LOGIN_APP / "repositories" / "user_repository.py"
    result = analyzer.analyze_file("repositories/user_repository.py", path.read_text(encoding="utf-8"))

    ids = result.entity_ids()
    assert "python:repositories/user_repository.py:UserRepository" in ids
    assert "python:repositories/user_repository.py:UserRepository.find_by_username" in ids
    assert "python:repositories/user_repository.py:UserRepository.find_by_id" in ids


def test_python_ground_truth_entities_match(analyzer: PythonAnalyzer):
    """All Python entities and endpoints in ground_truth.json must be extracted by the analyzer."""
    gt = json.loads((LOGIN_APP / "ground_truth.json").read_text(encoding="utf-8"))

    expected_python_entities = {
        eid for eid, etype in gt["entities"]
        if eid.startswith("python:") or eid.startswith("api:")
    }

    extracted_entities = set()
    for rel_path in ["app.py", "services/auth_service.py", "repositories/user_repository.py"]:
        res = analyzer.analyze_file(rel_path, (LOGIN_APP / rel_path).read_text(encoding="utf-8"))
        extracted_entities.update(res.entity_ids())

    missing = expected_python_entities - extracted_entities
    assert not missing, f"Missing ground-truth entities: {missing}"


def test_python_ground_truth_relationships_match(analyzer: PythonAnalyzer):
    """All Python-only and Python-to-API relationships in ground_truth.json must be extracted."""
    gt = json.loads((LOGIN_APP / "ground_truth.json").read_text(encoding="utf-8"))

    # Extract all relationships where source starts with python:
    expected_rels = {
        (src, rtype, tgt)
        for src, rtype, tgt, _ in gt["relationships"]
        if src.startswith("python:")
    }

    extracted_rels = set()
    for rel_path in ["app.py", "services/auth_service.py", "repositories/user_repository.py"]:
        res = analyzer.analyze_file(rel_path, (LOGIN_APP / rel_path).read_text(encoding="utf-8"))
        for r in res.relationships:
            extracted_rels.add((r.source_id, r.type.value, r.target_id))

    missing = expected_rels - extracted_rels
    assert not missing, f"Missing ground-truth relationships: {missing}"


def test_syntax_error_handled_gracefully(analyzer: PythonAnalyzer):
    bad_code = "def invalid_syntax(:\n    pass"
    result = analyzer.analyze_file("bad.py", bad_code)
    assert result.entities == []
    assert result.relationships == []


def test_decorators_are_not_attributed_as_calls():
    code = (
        "from fastapi import FastAPI\n"
        "app = FastAPI()\n"
        "@app.get('/x')\n"
        "def x():\n    return 1\n"
    )
    result = PythonAnalyzer().analyze_file("app.py", code)
    assert [r for r in result.relationships if r.type.value == "CALLS"] == []


SAMPLE = """
from models.base import Base
from .util import helper

class Child(Base):
    def run(self):
        self.step()
        helper()
        local_fn()

    def step(self):
        pass


def local_fn():
    return Child()
"""


def _rels(result, rtype):
    return {(r.source_id, r.target_id) for r in result.relationships if r.type.value == rtype}


def test_inheritance_is_detected():
    result = PythonAnalyzer().analyze_file("pkg/mod.py", SAMPLE)
    assert ("python:pkg/mod.py:Child", "python:models/base.py:Base") in _rels(result, "INHERITS")


def test_self_calls_and_same_module_calls_are_detected():
    calls = _rels(PythonAnalyzer().analyze_file("pkg/mod.py", SAMPLE), "CALLS")
    assert ("python:pkg/mod.py:Child.run", "python:pkg/mod.py:Child.step") in calls     # self.step()
    assert ("python:pkg/mod.py:Child.run", "python:pkg/mod.py:local_fn") in calls       # defined later in file
    assert ("python:pkg/mod.py:local_fn", "python:pkg/mod.py:Child") in calls           # Child()


def test_relative_import_resolves_against_package():
    calls = _rels(PythonAnalyzer().analyze_file("pkg/mod.py", SAMPLE), "CALLS")
    assert ("python:pkg/mod.py:Child.run", "python:pkg/util.py:helper") in calls


def test_typed_parameter_is_a_uses_relationship():
    code = "class Req:\n    pass\n\ndef handler(r: Req, n: int):\n    pass\n"
    uses = _rels(PythonAnalyzer().analyze_file("a.py", code), "USES")
    assert uses == {("python:a.py:handler", "python:a.py:Req")}


TYPED = """
from graph import Graph
from typing import Optional

class Local:
    def ping(self):
        pass

class Service:
    def __init__(self, graph: Graph, other: "Local", maybe: Optional[Graph] = None):
        self.graph = graph
        self.other = other

    def run(self):
        self.graph.build()
        self.other.ping()

    def unrelated(self, graph):
        graph.build()          # untyped here: the annotation of __init__ must not leak into this scope


def free(g: Graph | None):
    g.build()


def later(g):
    g.build()                  # `g` was annotated in `free`, not here
"""


def test_annotated_parameters_drive_call_resolution():
    calls = _rels(PythonAnalyzer().analyze_file("svc.py", TYPED), "CALLS")
    assert ("python:svc.py:Service.run", "python:graph.py:Graph.build") in calls          # self.graph = graph
    assert ("python:svc.py:Service.run", "python:svc.py:Local.ping") in calls            # string annotation, same file
    assert ("python:svc.py:free", "python:graph.py:Graph.build") in calls                # Graph | None


def test_type_information_does_not_leak_between_functions():
    calls = _rels(PythonAnalyzer().analyze_file("svc.py", TYPED), "CALLS")
    assert ("python:svc.py:Service.unrelated", "python:graph.py:Graph.build") not in calls
    assert ("python:svc.py:later", "python:graph.py:Graph.build") not in calls


def test_annotated_assignment_sets_type():
    code = "from graph import Graph\n\ndef f():\n    g: Graph = make()\n    g.build()\n"
    assert ("python:a.py:f", "python:graph.py:Graph.build") in _rels(PythonAnalyzer().analyze_file("a.py", code), "CALLS")
