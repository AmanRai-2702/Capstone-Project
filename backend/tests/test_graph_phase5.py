"""Phase 5: named graph views, constructor edges, bounded path search, filtering, neighbourhoods,
edge evidence, file-level view and the matching API endpoints."""
import random
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from repomind.api.app import create_app
from repomind.config import Settings
from repomind.graph import RepositoryGraph
from repomind.graph.repository_graph import CONSTRUCTS, VIEWS
from repomind.models import Entity, EntityType, Language
from repomind.qa import QAService
from repomind.services.analysis_service import AnalysisService
from repomind.services.impact_service import ImpactService

APPS = Path(__file__).resolve().parents[2] / "test_apps"
FORM = "html:static/index.html:#login-form"
HANDLE = "javascript:static/login.js:handleLogin"
ENDPOINT = "api:POST:/api/login"
LOGIN = "python:app.py:login"
AUTH = "python:services/auth_service.py:AuthService.authenticate"


@pytest.fixture(scope="module")
def ws(tmp_path_factory):
    return tmp_path_factory.mktemp("ws")


@pytest.fixture(scope="module")
def analyses(ws):
    svc = AnalysisService(Settings(workspace_dir=ws, log_level="WARNING"))
    return {name: svc.analyze(str(APPS / name)) for name in ("login_app", "shop_app", "notes_app")}


@pytest.fixture(scope="module")
def login(analyses):
    return RepositoryGraph.from_analysis(analyses["login_app"])


@pytest.fixture(scope="module")
def shop(analyses):
    return RepositoryGraph.from_analysis(analyses["shop_app"])


# ------------------------------------------------------------------ four directions, never mixed
def test_the_four_views_exist_and_unknown_view_is_rejected(login):
    assert VIEWS == ("syntactic", "execution", "dependency", "impact")
    for name in VIEWS:
        assert login.view(name).number_of_nodes() == len(login)
    with pytest.raises(ValueError):
        login.view("runtime")


def test_syntactic_view_keeps_raw_direction_and_containment(login):
    syn = login.syntactic_view()
    assert syn.has_edge(LOGIN, ENDPOINT) and syn.edges[LOGIN, ENDPOINT]["type"] == "HANDLES"   # decorator on the handler
    assert syn.has_edge("file:app.py", LOGIN) and syn.edges["file:app.py", LOGIN]["type"] == "CONTAINS"


def test_execution_view_runs_request_then_handler(login):
    ex = login.flow_view()
    assert ex.has_edge(ENDPOINT, LOGIN) and not ex.has_edge(LOGIN, ENDPOINT)
    assert ex.edges[ENDPOINT, LOGIN]["reversed"] is True               # runs against the raw HANDLES direction
    assert not any(d["type"] == "CONTAINS" for _, _, d in ex.edges(data=True))


def test_impact_view_is_exactly_the_dependency_view_reversed(login):
    dep, imp = login.dependency_view(), login.impact_view()
    assert {(b, a) for a, b in dep.edges} == set(imp.edges)


def test_the_same_pair_of_nodes_gets_different_answers_in_each_view(login):
    """handler -> UI element: no execution path, no syntactic path, but a change-impact path."""
    assert login.find_paths(LOGIN, FORM, view="execution") == []
    assert login.find_paths(LOGIN, FORM, view="syntactic") == []
    assert login.find_paths(LOGIN, FORM, view="impact")[0] == [LOGIN, ENDPOINT, HANDLE, FORM]
    # and the reverse question has the opposite answer: UI element -> handler runs forward
    assert login.find_paths(FORM, LOGIN, view="execution")[0] == [FORM, HANDLE, ENDPOINT, LOGIN]
    assert login.find_paths(FORM, LOGIN, view="impact") == []


# ------------------------------------------------------------------ constructor edges
def test_calling_a_class_continues_into_its_constructor(shop):
    ex = shop.flow_view()
    ctor = "python:repositories/order_repository.py:OrderRepository.__init__"
    edge = ex.edges["python:repositories/order_repository.py:OrderRepository", ctor]
    assert (edge["type"], edge["kind"], edge["derived"], edge["confidence"]) == (CONSTRUCTS, "detected", True, 1.0)
    assert shop.g.number_of_edges() == sum(1 for _ in shop.g.edges)     # the raw graph is untouched
    assert not any(d["type"] == CONSTRUCTS for _, _, d in shop.g.edges(data=True))


def test_inherited_constructor_is_inferred_and_less_certain(analyses):
    g = RepositoryGraph.from_analysis(analyses["notes_app"])
    edge = g.flow_view().edges["python:repositories/note_repository.py:NoteRepository",
                               "python:repositories/note_repository.py:BaseRepository.__init__"]
    assert (edge["kind"], edge["confidence"]) == ("inferred", 0.9)


def test_a_class_without_any_constructor_gets_no_edge(analyses):
    g = RepositoryGraph.from_analysis(analyses["login_app"])
    assert g.constructor_of("python:repositories/user_repository.py:UserRepository") is None
    out = g.flow_view().out_edges("python:repositories/user_repository.py:UserRepository", data=True)
    assert [b for _, b, d in out if d["type"] == CONSTRUCTS] == []


def test_changing_a_constructor_affects_whoever_instantiates_the_class(shop):
    report = ImpactService(shop).analyze_impact("python:repositories/order_repository.py:OrderRepository.__init__")
    hit = {c.entity_id: c for c in report.impacted}
    assert "python:services/order_service.py:OrderService.__init__" in hit        # it calls OrderRepository(...)
    assert "python:repositories/order_repository.py:OrderRepository" in hit
    caller = hit["python:services/order_service.py:OrderService.__init__"]
    assert caller.distance == 2 and caller.category == "execution_flow"        # ctor -> class -> its caller


def test_javascript_constructor_is_followed(tmp_path):
    root = tmp_path / "js"
    root.mkdir()
    (root / "a.js").write_text("class A { constructor() { helper(); } }\nfunction helper() {}\nfunction make() { return new A(); }\n"
                               "function go() { A(); }\n")
    g = RepositoryGraph.from_analysis(AnalysisService(Settings(workspace_dir=tmp_path / "ws", log_level="WARNING")).analyze(str(root)))
    assert g.constructor_of("javascript:a.js:A") == ("javascript:a.js:A.constructor", False)


# ------------------------------------------------------------------ bounded path search
def synthetic(n, m, seed=1):
    random.seed(seed)
    g = RepositoryGraph()
    for i in range(n):
        e = Entity(id=f"python:f.py:f{i}", type=EntityType.FUNCTION, name=f"f{i}", qualified_name=f"f{i}", language=Language.PYTHON)
        g.entities[e.id] = e
        g.g.add_node(e.id, type="function", name=e.name, qualified_name=e.name, language="python")
    for _ in range(m):
        a, b = random.sample(range(n), 2)
        g.g.add_edge(f"python:f.py:f{a}", f"python:f.py:f{b}", key="CALLS", type="CALLS", kind="detected", confidence=1.0, evidence=[])
    return g


def test_unreachable_target_is_fast_even_on_a_dense_graph():
    """Used to take ~42 s at this size (all simple paths were enumerated before giving up)."""
    g = synthetic(1200, 6000)
    iso = Entity(id="python:f.py:island", type=EntityType.FUNCTION, name="island", qualified_name="island", language=Language.PYTHON)
    g.entities[iso.id] = iso
    g.g.add_node(iso.id, type="function", name="island", qualified_name="island", language="python")
    start = time.perf_counter()
    assert g.find_paths("python:f.py:f0", iso.id) == []
    assert time.perf_counter() - start < 3


def test_paths_come_shortest_first_and_respect_limit_and_depth():
    g = synthetic(300, 1200)
    paths = g.find_paths("python:f.py:f0", "python:f.py:f1", limit=5)
    assert 1 <= len(paths) <= 5 and [len(p) for p in paths] == sorted(len(p) for p in paths)
    assert all(p[0] == "python:f.py:f0" and p[-1] == "python:f.py:f1" and len(set(p)) == len(p) for p in paths)
    assert all(len(p) - 1 <= 2 for p in g.find_paths("python:f.py:f0", "python:f.py:f1", max_depth=2))


def test_cycles_terminate_and_same_node_or_unknown_node_gives_no_path():
    g = synthetic(5, 0)
    for a, b in ((0, 1), (1, 2), (2, 0), (2, 3)):
        g.g.add_edge(f"python:f.py:f{a}", f"python:f.py:f{b}", key="CALLS", type="CALLS", kind="detected", confidence=1.0, evidence=[])
    assert g.find_paths("python:f.py:f0", "python:f.py:f3")[0] == ["python:f.py:f0", "python:f.py:f1", "python:f.py:f2", "python:f.py:f3"]
    assert g.find_paths("python:f.py:f0", "python:f.py:f0") == [] and g.find_paths("nope", "python:f.py:f0") == []


def test_path_summary_confidence_is_the_product_and_flags_inferred_hops(shop):
    nodes = ["api:POST:/api/orders", "python:app.py:orders", "python:services/order_service.py:OrderService.place",
             "python:repositories/order_repository.py:OrderRepository.save"]
    summary = shop.path_summary(shop.find_paths(nodes[0], nodes[-1])[0])
    product = 1.0
    for h in summary["hops"]:
        product *= h["confidence"]
    assert summary["nodes"] == nodes and summary["confidence"] == pytest.approx(product, abs=1e-4)
    assert summary["uncertain"] is True and summary["confidence"] < 1.0           # CALLS via self.repo is inferred
    assert [h["reversed"] for h in summary["hops"]] == [True, False, False]


# ------------------------------------------------------------------ selection, neighbourhood, export
def test_select_nodes_filters_combine(login):
    funcs = login.select_nodes(types=["function"])
    assert funcs and all(login.entities[n].type.value == "function" for n in funcs)
    js = login.select_nodes(types=["function"], languages=["javascript"])
    assert {n.split(":")[-1] for n in js} == {"showMessage", "handleLogin", "loadProfile"}
    assert login.select_nodes(query="AUTHENTICATE") == {AUTH}
    assert all(not n.startswith("file:") for n in login.select_nodes(include_files=False))


def test_neighborhood_depth_direction_and_relation_filter(login):
    assert login.neighborhood(HANDLE, 1, "out", ["REQUESTS"]) == {HANDLE, ENDPOINT}
    assert login.neighborhood(ENDPOINT, 1, "in", ["REQUESTS", "HANDLES"]) == {ENDPOINT, HANDLE, LOGIN}
    two = login.neighborhood(FORM, 2, "out", view="execution")
    assert two == {FORM, HANDLE, "javascript:static/login.js:showMessage", ENDPOINT}
    assert login.neighborhood("missing", 2) == set()


def test_neighborhood_max_nodes_keeps_the_nearest():
    g = synthetic(60, 300)
    near = g.neighborhood("python:f.py:f0", 3, "out", max_nodes=8)
    first_ring = g.neighborhood("python:f.py:f0", 1, "out")
    assert len(near) == 8 and first_ring <= near              # the whole nearest ring is kept before anything farther


def test_top_nodes_keeps_required_nodes_and_prefers_connected_ones(login):
    everything = set(login.entities)
    top = login.top_nodes(everything, 5, always=[FORM])
    assert len(top) == 5 and FORM in top
    ranked = sorted(everything - {FORM}, key=lambda n: (-login.g.degree(n), n))
    assert set(ranked[:4]) <= top


def test_subgraph_filters_and_edge_ids(login):
    full = login.subgraph_json()
    calls = login.subgraph_json(rel_types=["calls"])
    assert calls["edges"] and {e["type"] for e in calls["edges"]} == {"CALLS"} and len(calls["edges"]) < len(full["edges"])
    assert {e["kind"] for e in login.subgraph_json(kinds=["inferred"])["edges"]} == {"inferred"}
    strong = login.subgraph_json(min_confidence=0.95)
    assert strong["edges"] and all(e["confidence"] >= 0.95 for e in strong["edges"])
    e = calls["edges"][0]
    assert e["id"] == f"{e['source']}|CALLS|{e['target']}" and e["evidence_count"] >= 1 and "evidence" not in e
    assert all("evidence" in x for x in login.subgraph_json(include_evidence=True)["edges"])
    assert all(n["degree"] == login.g.degree(n["id"]) for n in full["nodes"])


def test_edge_details_returns_evidence_or_none(login):
    d = login.edge_details(HANDLE, "requests", ENDPOINT)
    assert d["type"] == "REQUESTS" and d["evidence"] and "fetch" in d["evidence"][0]["snippet"]
    assert d["evidence"][0]["location"]["file_path"] == "static/login.js"
    assert login.edge_details(HANDLE, "REQUESTS", "api:GET:/nothing") is None


def test_module_graph_collapses_to_files_and_shows_cross_language_links(login):
    mg = login.module_graph()
    nodes = {n["id"]: n for n in mg["nodes"]}
    edge = {(e["source"], e["target"], e["type"]): e for e in mg["edges"]}
    assert edge[("file:static/login.js", "file:app.py", "REQUESTS")]["count"] == 2     # login + get_user endpoints
    assert ("file:app.py", "file:services/auth_service.py", "IMPORTS") in edge
    assert all(e["source"] != e["target"] and e["type"] != "CONTAINS" for e in mg["edges"])
    assert nodes["file:app.py"]["language"] == "python" and nodes["file:app.py"]["entities"] > 0
    assert all(n["id"].startswith("file:") for n in mg["nodes"])


# ------------------------------------------------------------------ Q&A still copes with derived edges
def test_flow_answers_mention_constructors_without_errors(analyses):
    qa = QAService(analyses["shop_app"], settings=Settings(workspace_dir=Path("."), log_level="WARNING"))
    a = qa.ask("What happens when POST /api/orders is called?")
    assert a.grounded and "OrderService.place" in a.answer and "runs the constructor" in a.answer


# ------------------------------------------------------------------ API
@pytest.fixture(scope="module")
def client(ws):
    c = TestClient(create_app(Settings(workspace_dir=ws, log_level="WARNING")))
    for app in ("login_app", "shop_app"):
        assert c.post("/api/repositories", json={"location": str(APPS / app)}).status_code == 200
    return c


R = "/api/repositories/login_app"


def test_api_graph_filters(client):
    d = client.get(f"{R}/graph", params={"types": "function", "languages": "javascript", "rel_types": "CALLS"}).json()
    assert {n["type"] for n in d["nodes"]} == {"function"} and {n["language"] for n in d["nodes"]} == {"javascript"}
    assert {e["type"] for e in d["edges"]} == {"CALLS"} and d["meta"]["level"] == "entity" and not d["meta"]["truncated"]
    assert all(e["confidence"] >= 0.9 for e in client.get(f"{R}/graph", params={"min_confidence": 0.9}).json()["edges"])
    assert {e["kind"] for e in client.get(f"{R}/graph", params={"kinds": "inferred"}).json()["edges"]} == {"inferred"}
    assert [n["id"] for n in client.get(f"{R}/graph", params={"q": "authenticate"}).json()["nodes"]] == [AUTH]


def test_api_graph_max_nodes_truncates_and_says_so(client):
    d = client.get(f"{R}/graph", params={"max_nodes": 10}).json()
    assert len(d["nodes"]) == 10 and d["meta"]["truncated"] and d["meta"]["total_nodes"] > 10
    ids = {n["id"] for n in d["nodes"]}
    assert all(e["source"] in ids and e["target"] in ids for e in d["edges"])


def test_api_file_level_graph(client):
    d = client.get(f"{R}/graph", params={"level": "file"}).json()
    assert d["meta"]["level"] == "file" and all(n["id"].startswith("file:") for n in d["nodes"])
    assert any(e["type"] == "REQUESTS" and e["source"] == "file:static/login.js" for e in d["edges"])
    small = client.get(f"{R}/graph", params={"level": "file", "max_nodes": 5}).json()
    assert len(small["nodes"]) == 5 and small["meta"]["truncated"]
    assert client.get(f"{R}/graph", params={"level": "galaxy"}).status_code == 422


def test_api_neighborhood(client):
    d = client.get(f"{R}/graph/neighborhood", params={"id": FORM, "depth": 2, "direction": "out", "view": "execution"}).json()
    assert {n["id"] for n in d["nodes"]} == {FORM, HANDLE, ENDPOINT, "javascript:static/login.js:showMessage"}
    assert d["meta"]["center"] == FORM and d["meta"]["view"] == "execution"
    assert client.get(f"{R}/graph/neighborhood", params={"id": FORM, "view": "nope"}).status_code == 422
    assert client.get(f"{R}/graph/neighborhood", params={"id": "zzz-nothing"}).status_code == 404


def test_api_edge_evidence(client):
    d = client.get(f"{R}/graph/edge", params={"source": HANDLE, "type": "REQUESTS", "target": ENDPOINT}).json()
    assert d["evidence"] and d["id"] == f"{HANDLE}|REQUESTS|{ENDPOINT}"
    assert client.get(f"{R}/graph/edge", params={"source": HANDLE, "type": "CALLS", "target": ENDPOINT}).status_code == 404


def test_api_trace_has_confidence_views_and_reports_ambiguity(client):
    d = client.get(f"{R}/trace", params={"source": FORM, "target": AUTH}).json()
    best = d["paths"][0]
    assert best["nodes"][0] == FORM and best["nodes"][-1] == AUTH and 0 < best["confidence"] <= 1
    assert all({"from", "to", "type", "reversed", "derived"} <= set(h) for h in best["hops"])
    assert d["view"] == "execution" and d["ambiguous"] == {}
    back = client.get(f"{R}/trace", params={"source": LOGIN, "target": FORM, "view": "impact"}).json()
    assert back["paths"][0]["nodes"] == [LOGIN, ENDPOINT, HANDLE, FORM]
    assert client.get(f"{R}/trace", params={"source": LOGIN, "target": FORM}).json()["paths"] == []
    amb = client.get(f"{R}/trace", params={"source": "find_by", "target": FORM}).json()      # substring: two methods match
    assert set(amb["ambiguous"]["find_by"]) == {"python:repositories/user_repository.py:UserRepository.find_by_id",
                                                "python:repositories/user_repository.py:UserRepository.find_by_username"}
    assert client.get(f"{R}/trace", params={"source": "get_user", "target": FORM}).json()["ambiguous"] == {}   # exact name wins
    assert client.get(f"{R}/trace", params={"source": FORM, "target": AUTH, "view": "x"}).status_code == 422
    posted = client.post(f"{R}/trace", json={"source": LOGIN, "target": FORM, "view": "impact"}).json()
    assert posted["paths"][0]["nodes"][-1] == FORM


def test_api_flows_include_the_constructor_hop(client):
    flows = client.get("/api/repositories/shop_app/flows").json()
    nodes = {n for f in flows for p in f["paths"] for n in p}
    assert "python:models/order.py:Order.__init__" in nodes
