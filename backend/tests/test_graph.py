from pathlib import Path

import pytest

from repomind.config import Settings
from repomind.graph import RepositoryGraph
from repomind.models import RelationType
from repomind.services.analysis_service import AnalysisService

LOGIN_APP = Path(__file__).resolve().parents[2] / "test_apps" / "login_app"

FORM = "html:static/index.html:#login-form"
HANDLE_LOGIN = "javascript:static/login.js:handleLogin"
ENDPOINT = "api:POST:/api/login"
LOGIN = "python:app.py:login"
AUTHENTICATE = "python:services/auth_service.py:AuthService.authenticate"
FIND_BY_USERNAME = "python:repositories/user_repository.py:UserRepository.find_by_username"


@pytest.fixture(scope="module")
def analysis(tmp_path_factory):
    settings = Settings(workspace_dir=tmp_path_factory.mktemp("ws"), log_level="WARNING")
    return AnalysisService(settings).analyze(str(LOGIN_APP))


@pytest.fixture(scope="module")
def graph(analysis) -> RepositoryGraph:
    return RepositoryGraph.from_analysis(analysis)


def test_graph_has_all_entities_and_edges(graph, analysis):
    assert len(graph) == analysis.entity_count
    assert graph.g.number_of_edges() == analysis.relationship_count


def test_node_attributes(graph):
    node = graph.get_node(HANDLE_LOGIN)
    assert node["type"] == "function" and node["language"] == "javascript"
    assert graph.get_node("nope") is None


def test_incoming_and_outgoing(graph):
    assert HANDLE_LOGIN in graph.get_successors(FORM)
    assert FORM in graph.get_predecessors(HANDLE_LOGIN)
    assert any(e["target"] == ENDPOINT for e in graph.outgoing(HANDLE_LOGIN, [RelationType.REQUESTS]))
    assert any(e["source"] == LOGIN for e in graph.incoming(ENDPOINT, [RelationType.HANDLES]))


def test_children_and_descendants(graph):
    assert "python:services/auth_service.py:AuthService" in graph.children("file:services/auth_service.py")
    assert AUTHENTICATE in graph.descendants("file:services/auth_service.py")


def test_full_cross_language_path(graph):
    """The headline capability: HTML form -> JS -> API -> Python -> service -> repository."""
    paths = graph.find_paths(FORM, FIND_BY_USERNAME)
    assert [FORM, HANDLE_LOGIN, ENDPOINT, LOGIN, AUTHENTICATE, FIND_BY_USERNAME] in paths


def test_describe_path_has_relation_types_and_evidence(graph):
    path = [FORM, HANDLE_LOGIN, ENDPOINT, LOGIN, AUTHENTICATE, FIND_BY_USERNAME]
    hops = graph.describe_path(path)
    assert [h["type"] for h in hops] == ["TRIGGERS", "REQUESTS", "HANDLES", "CALLS", "CALLS"]
    assert all(h["evidence"] for h in hops)


def test_no_backwards_path(graph):
    assert graph.find_paths(FIND_BY_USERNAME, FORM) == []


def test_entry_points_are_ui_elements(graph):
    eps = graph.entry_points()
    assert FORM in eps
    assert "html:static/index.html:#profile-btn" in eps
    # api:GET:/ is hit directly by the browser; nothing in the repo calls it, so it is an entry too
    assert {graph.get_node(e)["type"] for e in eps} <= {"html_element", "api_endpoint"}


def test_resolve_by_name_and_qualified_name(graph):
    assert [e.id for e in graph.resolve("handleLogin")] == [HANDLE_LOGIN]
    assert [e.id for e in graph.resolve("AuthService.authenticate")] == [AUTHENTICATE]
    assert graph.resolve("does-not-exist-anywhere") == []


def test_subgraph_json(graph):
    data = graph.subgraph_json({FORM, HANDLE_LOGIN})
    assert {n["id"] for n in data["nodes"]} == {FORM, HANDLE_LOGIN}
    assert len(data["edges"]) == 1 and data["edges"][0]["type"] == "TRIGGERS"
