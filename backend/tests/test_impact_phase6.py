"""Phase 6a: repository-wide type facts, RETURNS / DATA_FLOW / READS_STATE edges, anonymous-handler entities
and the `data_flow` impact category."""
from pathlib import Path

import pytest

from repomind.config import Settings
from repomind.graph import RepositoryGraph
from repomind.services.analysis_service import AnalysisService
from repomind.services.impact_service import ImpactService

APPS = Path(__file__).resolve().parents[2] / "test_apps"
ROOT = Path(__file__).resolve().parents[2]


def analyze(tmp_path, files: dict[str, str]):
    repo = tmp_path / "repo"
    for rel, text in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text, encoding="utf-8")
    return AnalysisService(Settings(workspace_dir=tmp_path / "ws", log_level="WARNING")).analyze(str(repo))


def edges(a, rtype):
    return {(r.source_id.split(":", 2)[2], r.target_id.split(":", 2)[2], r.kind.value)
            for r in a.relationships if r.type.value == rtype}


def pairs(a, rtype):
    return {(s, t) for s, t, _ in edges(a, rtype)}


# --------------------------------------------------------------------------- nested functions and return types
def test_nested_functions_can_be_called_from_their_siblings(tmp_path):
    a = analyze(tmp_path, {"m.py": "def outer():\n    def a():\n        return b()\n\n    def b():\n        return 1\n\n    return a\n"})
    assert ("a", "b") in pairs(a, "CALLS")


def test_return_annotation_resolves_chained_calls_across_files(tmp_path):
    a = analyze(tmp_path, {
        "models.py": "class Store:\n    def put(self, x): pass\n\nclass Session:\n    store: Store\n\ndef make() -> Session:\n    return Session()\n",
        "use.py": "from models import make\n\ndef go():\n    make().store.put(1)\n\ndef go2():\n    s = make()\n    s.store.put(2)\n"})
    calls = pairs(a, "CALLS")
    assert ("go", "Store.put") in calls and ("go2", "Store.put") in calls


def test_method_return_annotations_are_followed(tmp_path):
    a = analyze(tmp_path, {"m.py": "class B:\n    def hit(self): pass\n\nclass A:\n    def get(self) -> B:\n        return B()\n\n"
                                   "def f(a: A):\n    a.get().hit()\n"})
    assert ("f", "B.hit") in pairs(a, "CALLS")


def test_ambiguous_class_names_are_never_guessed(tmp_path):
    a = analyze(tmp_path, {
        "a.py": "class Thing:\n    def run(self): pass\n", "b.py": "class Thing:\n    def run(self): pass\n",
        "c.py": "def make() -> Thing:\n    pass\n\ndef go():\n    make().run()\n"})
    assert not [t for _, t in pairs(a, "CALLS") if t.endswith("Thing.run")]


def test_an_imported_function_is_not_mistaken_for_a_class(tmp_path):
    a = analyze(tmp_path, {"lib.py": "class Repo:\n    def put(self): pass\n\ndef make_repo() -> Repo:\n    return Repo()\n",
                           "use.py": "from lib import make_repo\n\ndef go():\n    r = make_repo()\n    r.put()\n"})
    calls = pairs(a, "CALLS")
    assert ("go", "Repo.put") in calls and not [t for _, t in calls if "make_repo.put" in t]


def test_attribute_types_from_constructors_and_annotated_parameters(tmp_path):
    a = analyze(tmp_path, {"m.py": "class Repo:\n    def save(self): pass\n\nclass Cache:\n    def drop(self): pass\n\n"
                                   "class Svc:\n    def __init__(self, cache: Cache):\n        self.repo = Repo()\n        self.cache = cache\n\n"
                                   "    def run(self):\n        self.repo.save()\n        self.cache.drop()\n"})
    assert {("Svc.run", "Repo.save"), ("Svc.run", "Cache.drop")} <= pairs(a, "CALLS")


# --------------------------------------------------------------------------- RETURNS
def test_returns_from_annotation_is_detected_and_from_constructor_is_inferred(tmp_path):
    a = analyze(tmp_path, {"m.py": "class A: pass\nclass B: pass\n\ndef annotated() -> A:\n    return None\n\ndef built():\n    return B()\n"})
    assert ("annotated", "A", "detected") in edges(a, "RETURNS")
    assert ("built", "B", "inferred") in edges(a, "RETURNS")


def test_returns_ignores_builtins_and_unknown_types(tmp_path):
    a = analyze(tmp_path, {"m.py": "def f() -> int:\n    return 1\n\ndef g() -> Unknown:\n    return None\n\ndef h():\n    return [1]\n"})
    assert not edges(a, "RETURNS")


# --------------------------------------------------------------------------- DATA_FLOW
FLOW = "def load(): return [1]\ndef clean(x): return x\ndef save(x): return x\n"


def test_data_flow_through_variables_and_nested_calls(tmp_path):
    a = analyze(tmp_path, {"m.py": FLOW + "def run():\n    raw = load()\n    c = clean(raw)\n    return save(c)\n\n"
                                          "def nested():\n    return save(clean(load()))\n"})
    flow = pairs(a, "DATA_FLOW")
    assert flow == {("clean", "load"), ("save", "clean")}            # consumer -> producer; never save -> load


def test_data_flow_follows_attributes_and_subscripts_of_a_produced_value(tmp_path):
    a = analyze(tmp_path, {"m.py": "class Order:\n    pass\n\ndef consume(x): pass\n\ndef go():\n    o = Order()\n    consume(o.items)\n    consume(o['k'])\n"})
    assert pairs(a, "DATA_FLOW") == {("consume", "Order")}


def test_reassigning_a_variable_ends_its_data_flow(tmp_path):
    a = analyze(tmp_path, {"m.py": FLOW + "def run():\n    x = load()\n    x = [9]\n    save(x)\n"})
    assert not pairs(a, "DATA_FLOW")


def test_data_flow_needs_both_ends_to_be_resolved(tmp_path):
    a = analyze(tmp_path, {"m.py": "import os\n\ndef load(): return 1\n\ndef go():\n    x = os.getcwd()\n    load2(x)\n    print(x)\n    y = load()\n    print(y)\n"})
    assert not pairs(a, "DATA_FLOW")                                 # external consumers / producers create nothing


def test_data_flow_is_not_created_between_a_function_and_itself(tmp_path):
    a = analyze(tmp_path, {"m.py": "def f(x):\n    y = f(1)\n    return f(y)\n"})
    assert not pairs(a, "DATA_FLOW")


# --------------------------------------------------------------------------- READS_STATE
def test_reads_state_between_methods_of_one_class(tmp_path):
    a = analyze(tmp_path, {"m.py": "class C:\n    def __init__(self):\n        self.n = 0\n\n    def inc(self):\n        self.n += 1\n\n"
                                   "    def get(self):\n        return self.n\n\n    def other(self):\n        return 1\n"})
    state = pairs(a, "READS_STATE")
    assert {("C.inc", "C.__init__"), ("C.get", "C.__init__"), ("C.get", "C.inc")} <= state
    assert not [p for p in state if p[0] == "C.other" or p[1] == "C.other"]
    assert ("C.inc", "C.inc") not in state                           # a method never depends on itself


def test_reads_state_follows_local_base_classes_but_not_method_calls(tmp_path):
    a = analyze(tmp_path, {"m.py": "class Base:\n    def __init__(self):\n        self.cache = {}\n\n    def helper(self):\n        return 1\n\n"
                                   "class Child(Base):\n    def lookup(self):\n        self.helper()\n        return self.cache\n"})
    state = pairs(a, "READS_STATE")
    assert ("Child.lookup", "Base.__init__") in state and ("Child.lookup", "Base.helper") not in state


def test_attributes_of_other_objects_are_not_state_of_this_class(tmp_path):
    a = analyze(tmp_path, {"m.py": "class C:\n    def __init__(self, other):\n        self.x = 1\n        other.y = 2\n\n    def get(self, other):\n        return other.y\n"})
    assert not [p for p in pairs(a, "READS_STATE") if p[0] == "C.get"]


# --------------------------------------------------------------------------- impact categories
@pytest.fixture(scope="module")
def shop(tmp_path_factory):
    svc = AnalysisService(Settings(workspace_dir=tmp_path_factory.mktemp("ws"), log_level="WARNING"))
    return RepositoryGraph.from_analysis(svc.analyze(str(APPS / "shop_app")))


def categories(graph, target):
    return {c.entity_id.split(":", 2)[2]: c.category for c in ImpactService(graph).analyze_impact(target).impacted}


def test_data_flow_dependents_are_reported_with_their_own_category(shop):
    cats = categories(shop, "python:models/order.py:Order")
    assert cats["total"] == "direct" and cats["OrderRepository.save"] == "direct"          # given an Order
    assert cats["OrderService.place"] == "direct"                    # it also constructs Order itself
    assert cats["orders"] == "execution_flow"                        # orders -CALLS-> place -CALLS-> Order


def test_state_dependents_are_data_flow_and_callers_of_them_too(shop):
    cats = categories(shop, "python:repositories/order_repository.py:OrderRepository.__init__")
    assert cats["OrderRepository.save"] == "direct"                  # reads self.rows that __init__ sets
    assert cats["OrderService.place"] == "data_flow"                 # place -CALLS-> save -READS_STATE-> __init__


def test_data_edges_are_inferred_so_their_impact_is_uncertain(shop):
    report = ImpactService(shop).analyze_impact("python:models/order.py:Order")
    hit = {c.entity_id.split(":", 2)[2]: c for c in report.impacted}
    assert hit["total"].uncertain and hit["total"].has_inferred_hop and hit["total"].confidence == pytest.approx(0.7)
    assert "is given data produced by" in hit["total"].explanation


# --------------------------------------------------------------------------- the whole point: a real cross-language trace
def test_repomind_ui_chat_flow_reaches_qaservice_ask(tmp_path):
    """JS handler -> request -> FastAPI endpoint -> handler -> helper -> `session(id).qa.ask()` (return type
    + attribute annotation). Before phase 6 this chain broke at the anonymous handler and again at do_ask."""
    a = AnalysisService(Settings(workspace_dir=tmp_path / "ws", log_level="WARNING")).analyze(str(ROOT))
    g = RepositoryGraph.from_analysis(a)
    form = next(e for e in g.entities if e.endswith("#chatform") and "api/static" in e)
    ask = next(e for e in g.entities if e.endswith("qa/service.py:QAService.ask"))
    path = g.find_paths(form, ask)[0]
    short = [n.split(":", 2)[-1].rsplit("/", 1)[-1] for n in path]
    assert short[0] == "#chatform" and short[-1] == "QAService.ask" and short[-2] == "do_ask"
    summary = g.path_summary(path)
    assert summary["uncertain"] is True and 0 < summary["confidence"] < 1
    assert [h["type"] for h in summary["hops"]][:3] == ["TRIGGERS", "REQUESTS", "HANDLES"]
