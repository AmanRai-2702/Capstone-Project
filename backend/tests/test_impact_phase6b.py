"""Phase 6b: API contracts, signature/body diff classification, dependency & deleted-file diffs, ranked
test suggestions and evidence-backed risk factors."""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from repomind.api.app import create_app
from repomind.config import Settings
from repomind.graph import RepositoryGraph
from repomind.services.analysis_service import AnalysisService
from repomind.services.contract_service import ContractService
from repomind.services.impact_service import (ImpactError, ImpactService, changed_packages, parse_diff_files)

APPS = Path(__file__).resolve().parents[2] / "test_apps"


def analyze(tmp_path, files):
    repo = tmp_path / "repo"
    for rel, text in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text, encoding="utf-8")
    return AnalysisService(Settings(workspace_dir=tmp_path / "ws", log_level="WARNING")).analyze(str(repo))


def endpoint_meta(a, eid):
    return next(e for e in a.entities if e.id == eid).metadata


# --------------------------------------------------------------------------- Python side of a contract
def test_response_keys_from_dict_literals_jsonify_and_http_exceptions(tmp_path):
    a = analyze(tmp_path, {"app.py": '''from fastapi import FastAPI, HTTPException
app = FastAPI()

@app.get("/a")
def a(x: int):
    if x:
        raise HTTPException(status_code=400, detail="bad")
    return {"ok": True, "count": 1}

@app.get("/b")
def b():
    return jsonify(name="x", age=2)

@app.get("/c")
def c():
    return jsonify({"k": 1})

@app.get("/d")
def d():
    return compute()

@app.get("/e")
def e(flag: bool):
    if flag:
        return {"a": 1}
    return {"b": 2, **extra}
'''})
    meta = lambda r: endpoint_meta(a, f"api:GET:/{r}")
    assert meta("a")["response_keys"] == ["count", "detail", "ok"] and meta("a")["response_complete"]
    assert meta("b")["response_keys"] == ["age", "name"] and meta("b")["response_complete"]
    assert meta("c")["response_keys"] == ["k"]
    assert meta("d")["response_keys"] == [] and not meta("d")["response_complete"]     # unreadable: unknown, not "none"
    assert meta("e")["response_keys"] == ["a", "b"] and not meta("e")["response_complete"]  # ** spread: incomplete


def test_response_model_fields_define_the_response(tmp_path):
    a = analyze(tmp_path, {"app.py": '''from fastapi import FastAPI
from pydantic import BaseModel
app = FastAPI()

class Out(BaseModel):
    id: int
    name: str

@app.get("/x", response_model=Out)
def x():
    return load()
'''})
    m = endpoint_meta(a, "api:GET:/x")
    assert m["response_keys"] == ["id", "name"] and m["response_complete"] is True


def test_request_fields_from_models_and_flask_json_access(tmp_path):
    a = analyze(tmp_path, {"app.py": '''from fastapi import FastAPI
from pydantic import BaseModel
app = FastAPI()

class In(BaseModel):
    user: str
    note: str = ""

@app.post("/m")
def m(body: In):
    return {}

@app.post("/f")
def f():
    data = request.get_json()
    a = data["title"]
    b = data.get("tag")
    return {}
'''})
    assert endpoint_meta(a, "api:POST:/m")["request_fields"] == {"note": False, "user": True}
    assert endpoint_meta(a, "api:POST:/f")["request_fields"] == {"tag": False, "title": True}


def test_nested_functions_do_not_contribute_to_the_response(tmp_path):
    a = analyze(tmp_path, {"app.py": '''from fastapi import FastAPI
app = FastAPI()

@app.get("/n")
def n():
    def helper():
        return {"inner": 1}
    return {"outer": helper()}
'''})
    assert endpoint_meta(a, "api:GET:/n")["response_keys"] == ["outer"]


# --------------------------------------------------------------------------- JavaScript side of a contract
SERVER = '''from fastapi import FastAPI, HTTPException
app = FastAPI()

@app.post("/api/save")
def save(body: dict):
    return {"id": 1, "status": "ok"}
'''


def client_of(a, fn_suffix):
    return next(r for r in a.relationships if r.type.value == "REQUESTS" and r.source_id.endswith(fn_suffix)).metadata


def test_fetch_sent_and_read_keys(tmp_path):
    a = analyze(tmp_path, {"app.py": SERVER, "index.html": '<script src="a.js"></script>', "a.js": '''
async function save(name, age) {
  const res = await fetch("/api/save", { method: "POST", body: JSON.stringify({ name, age: age, "x-y": 1 }) });
  const data = await res.json();
  show(data.id, data["status"], data.items.length);
}
async function load() {
  const res = await fetch("/api/save", { method: "POST" });
  const { id, status: s } = await res.json();
  return id;
}
'''})
    assert client_of(a, ":save") == {"sent_keys": ["age", "name", "x-y"], "read_keys": ["id", "items", "status"]}
    assert client_of(a, ":load") == {"sent_keys": [], "read_keys": ["id", "status"]}


def test_axios_response_data_keys_and_body(tmp_path):
    a = analyze(tmp_path, {"app.py": SERVER, "index.html": '<script src="a.js"></script>', "a.js": '''import axios from "axios";
async function save(item) {
  const res = await axios.post("/api/save", { title: "t" });
  return res.data.id + res.data.status;
}
async function save2(item) {
  const res = await axios.post("/api/save", item);
  return res.data;
}
'''})
    assert client_of(a, ":save") == {"sent_keys": ["title"], "read_keys": ["id", "status"]}
    assert client_of(a, ":save2") == {"sent_keys": None, "read_keys": None}       # variable body, response passed on whole


def test_unknown_is_not_empty_several_requests_and_array_methods(tmp_path):
    a = analyze(tmp_path, {"app.py": SERVER, "index.html": '<script src="a.js"></script>', "a.js": '''
async function two() {
  const a = await fetch("/api/save", { method: "POST" });
  const b = await fetch("/api/save", { method: "POST" });
  const d = await a.json();
  return d.id;
}
async function arr() {
  const res = await fetch("/api/save", { method: "POST" });
  const rows = await res.json();
  return rows.length + rows.map(x => x).length;
}
'''})
    assert client_of(a, ":two")["read_keys"] is None                 # two requests in one function: cannot attribute
    assert client_of(a, ":arr")["read_keys"] is None                 # .length / .map are the array, not response keys


# --------------------------------------------------------------------------- ContractService
@pytest.fixture(scope="module")
def login(tmp_path_factory):
    svc = AnalysisService(Settings(workspace_dir=tmp_path_factory.mktemp("ws"), log_level="WARNING"))
    return ContractService(RepositoryGraph.from_analysis(svc.analyze(str(APPS / "login_app"))))


def test_matching_contract_has_no_issues(login):
    c = login.contract("api:POST:/api/login")
    assert c["response_keys"] == ["detail", "message"] and c["request_fields"] == {"password": True, "username": True}
    assert c["clients"][0]["sent_keys"] == ["password", "username"] and c["clients"][0]["read_keys"] == ["detail", "message"]
    assert c["issues"] == []


def test_mismatches_are_reported_with_severity(tmp_path):
    a = analyze(tmp_path, {
        "app.py": '''from fastapi import FastAPI
from pydantic import BaseModel
app = FastAPI()

class In(BaseModel):
    title: str
    owner: str

@app.post("/api/n")
def n(body: In):
    return {"id": 1, "created": True}
''',
        "index.html": '<script src="a.js"></script>',
        "a.js": '''async function go(title) {
  const res = await fetch("/api/n", { method: "POST", body: JSON.stringify({ title, extra: 1 }) });
  const d = await res.json();
  return d.id + d.name;
}'''})
    c = ContractService(RepositoryGraph.from_analysis(a)).contract("api:POST:/api/n")
    found = {(i["type"], i["key"], i["severity"]) for i in c["issues"]}
    assert ("response_key_not_returned", "name", "high") in found            # client reads a key that never exists
    assert ("required_field_not_sent", "owner", "high") in found             # handler requires it, client omits it
    assert ("unknown_field_sent", "extra", "medium") in found
    assert ("response_key_unused", "created", "info") in found
    assert not any(k == "id" for _, k, _ in found)


def test_incomplete_responses_and_unknown_clients_produce_no_false_alarms(tmp_path):
    a = analyze(tmp_path, {
        "app.py": 'from fastapi import FastAPI\napp = FastAPI()\n\n@app.get("/api/d")\ndef d():\n    return compute()\n',
        "index.html": '<script src="a.js"></script>',
        "a.js": 'async function go() {\n  const r = await fetch("/api/d");\n  const x = await r.json();\n  return x.anything;\n}'})
    assert ContractService(RepositoryGraph.from_analysis(a)).contract("api:GET:/api/d")["issues"] == []


def test_change_impact_separates_certain_possible_and_unaffected_clients(tmp_path):
    a = analyze(tmp_path, {
        "app.py": 'from fastapi import FastAPI\napp = FastAPI()\n\n@app.get("/api/s")\ndef s():\n    return {"a": 1, "b": 2}\n',
        "index.html": '<script src="a.js"></script>',
        "a.js": '''async function readsA() {
  const r = await fetch("/api/s");
  const d = await r.json();
  return d.a;
}
async function readsB() {
  const r = await fetch("/api/s");
  const d = await r.json();
  return d.b;
}
async function opaque() {
  const r = await fetch("/api/s");
  return r.json();
}'''})
    impact = ContractService(RepositoryGraph.from_analysis(a)).change_impact("api:GET:/api/s", response_keys=["a"])
    by = {c["client"].split(":")[-1]: c["certainty"] for c in impact["clients"]}
    assert by == {"readsA": "certain", "opaque": "possible"}
    assert [c.split(":")[-1] for c in [x for x in impact["unaffected_clients"]]] == ["readsB"]
    assert impact["handlers"] == ["python:app.py:s"]


def test_contract_api(tmp_path):
    c = TestClient(create_app(Settings(workspace_dir=tmp_path / "ws", log_level="WARNING")))
    assert c.post("/api/repositories", json={"location": str(APPS / "login_app")}).status_code == 200
    d = c.get("/api/repositories/login_app/contracts").json()
    assert d["issue_count"] == 0 and {e["endpoint"] for e in d["endpoints"]} >= {"api:POST:/api/login"}
    assert c.get("/api/repositories/login_app/contracts", params={"only_issues": True}).json()["endpoints"] == []
    r = c.post("/api/repositories/login_app/contracts/impact", json={"endpoint": "POST /api/login", "response_keys": ["message"]})
    assert r.status_code == 200 and r.json()["clients"][0]["client"].endswith("handleLogin")
    assert c.post("/api/repositories/login_app/contracts/impact", json={"endpoint": "nonsense"}).status_code == 404


# --------------------------------------------------------------------------- diffs
SERVICE = '''def price(item):
    return item * 2

def total(items):
    return sum(price(i) for i in items)
'''
USER = "from service import total\n\ndef report(items):\n    return total(items)\n"
TEST = "from service import total\n\ndef test_total():\n    assert total([1]) == 2\n\ndef test_other():\n    assert 1\n"


@pytest.fixture()
def svc(tmp_path):
    a = analyze(tmp_path, {"service.py": SERVICE, "user.py": USER, "tests/test_service.py": TEST,
                           "lonely.py": "def a():\n    pass\n\ndef b():\n    a()\n",
                           "requirements.txt": "flask==2.0\nrequests>=2\n", "app.py": "import flask\n\ndef home():\n    return 1\n",
                           "uses_dead.py": "import dead\n\ndef go():\n    return dead.gone()\n"})     # dead.py is already deleted
    return ImpactService(RepositoryGraph.from_analysis(a), a.unresolved)


def hunk(path, line, count=1):
    return f"--- a/{path}\n+++ b/{path}\n@@ -{line},{count} +{line},{count} @@\n-x\n+y\n"


def test_signature_versus_body_changes_are_classified(svc):
    sig = svc.analyze_diff(hunk("service.py", 1))                    # the `def price(item):` line
    body = svc.analyze_diff(hunk("service.py", 2))                   # inside the function
    assert sig.change_kinds == {"python:service.py:price": "signature"}
    assert body.change_kinds == {"python:service.py:price": "body"}
    assert svc.classify_changes({"service.py": [(1, 2)]}) == {"python:service.py:price": "signature"}


def test_signature_change_with_dependents_raises_risk_and_says_why(svc):
    sig = svc.analyze_diff(hunk("service.py", 4))                    # def total(items):  (a dependent calls it)
    factor = next(f for f in sig.risk_factors if f.factor == "signature_change")
    assert factor.severity == "medium" and "service.py" in factor.evidence[0] and "total" in factor.detail
    assert sig.risk in ("medium", "high")
    body = svc.analyze_diff(hunk("service.py", 5))
    assert not [f for f in body.risk_factors if f.factor == "signature_change"]


def test_changed_dependency_marks_the_files_that_import_it(svc):
    diff = "--- a/requirements.txt\n+++ b/requirements.txt\n@@ -1,2 +1,2 @@\n-flask==2.0\n+flask==3.0\n requests>=2\n"
    r = svc.analyze_diff(diff)
    assert r.change_kinds == {"file:app.py": "dependency"}
    assert "flask" in r.notes[0] and "1 file(s) import them" in r.notes[0]
    assert "python:app.py:home" in r.changed


def test_changed_dependency_nobody_imports_is_reported_honestly(svc):
    diff = "--- a/requirements.txt\n+++ b/requirements.txt\n@@ -2 +2 @@\n-requests>=2\n+requests>=3\n"
    with pytest.raises(ImpactError, match="no analysed file imports them"):
        svc.analyze_diff(diff)                                       # nothing to analyse, and it says why


def test_deleted_file_marks_whatever_still_imports_it(svc):
    diff = "--- a/dead.py\n+++ /dev/null\n@@ -1,2 +0,0 @@\n-def gone():\n-    pass\n"
    r = svc.analyze_diff(diff)
    assert r.change_kinds == {"file:uses_dead.py": "deleted_file"} and "dead.py was deleted" in r.notes[0]


def test_diff_parsing_helpers():
    diff = "--- a/requirements.txt\n+++ b/requirements.txt\n@@ -1 +1 @@\n-Flask-Login==1\n+Flask-Login==2\n"
    files, deleted = parse_diff_files(diff)
    assert files["requirements.txt"] == {"added": ["Flask-Login==2"], "removed": ["Flask-Login==1"]} and not deleted
    assert changed_packages("requirements.txt", ["Flask-Login==2", "# comment", "-r base.txt"], []) == {"flask_login"}
    assert changed_packages("package.json", ['  "react": "^18.0.0",'], []) == {"react"}
    assert changed_packages("README.md", ["flask"], []) == set()


# --------------------------------------------------------------------------- ranked tests and risk factors
def test_suggested_tests_are_ranked_with_reasons(svc):
    r = svc.analyze_impact("python:service.py:price")
    top = r.suggested_test_details[0]
    assert top.file == "tests/test_service.py" and r.suggested_tests == ["tests/test_service.py"]
    assert top.score >= 3 and any("calls" in x for x in top.reasons) and any("imports service.py" in x for x in top.reasons)


def test_no_test_reaching_the_code_is_a_risk_factor_only_when_something_is_affected(svc):
    covered = svc.analyze_impact("python:service.py:price")                  # tests/test_service.py calls total -> price
    assert covered.suggested_tests and not [f for f in covered.risk_factors if f.factor == "no_tests"]
    lonely = svc.analyze_impact("python:lonely.py:a")                        # b() calls a(); no test touches either
    assert lonely.blast_radius == 1 and lonely.suggested_tests == []
    assert [f.severity for f in lonely.risk_factors if f.factor == "no_tests"] == ["medium"] and lonely.risk == "medium"
    alone = svc.analyze_impact("python:lonely.py:b")                         # nothing depends on b(): nothing to test
    assert alone.blast_radius == 0 and not [f for f in alone.risk_factors if f.factor == "no_tests"]


def test_risk_factors_carry_evidence_and_match_the_level(tmp_path):
    analysis = AnalysisService(Settings(workspace_dir=tmp_path / "ws", log_level="WARNING")).analyze(str(APPS / "login_app"))
    svc = ImpactService(RepositoryGraph.from_analysis(analysis))
    r = svc.analyze_impact("python:repositories/user_repository.py:UserRepository.find_by_username")
    assert r.risk == "high"
    entry = next(f for f in r.risk_factors if f.factor == "entry_points")
    assert entry.severity == "high" and entry.evidence and all(":" in e for e in entry.evidence)
    assert r.risk_reason.startswith("reaches")
    assert any(f.factor == "cross_language" for f in r.risk_factors)
    assert [f.factor for f in r.risk_factors].count("entry_points") == 1


def test_a_pure_python_change_does_not_claim_a_language_boundary(svc):
    assert not [f for f in svc.analyze_impact("python:service.py:price").risk_factors if f.factor == "cross_language"]


# --------------------------------------------------------------------------- impact benchmark and strict mode
def test_impact_benchmark_is_exact_in_both_modes():
    """11 hand-derived scenarios (backend/eval_data/impact_gt.json): the reported set must equal the expected set."""
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import evaluate_impact
    for strict in (False, True):
        result = evaluate_impact.run(strict=strict)
        wrong = [(r["id"], r["missing"], r["unexpected"]) for r in result["scenarios"] if r["missing"] or r["unexpected"]]
        assert not wrong, (strict, wrong)
        assert result["overall"]["category_accuracy"] == 1.0 and result["overall"]["scenarios"] >= 11


@pytest.fixture(scope="module")
def login_graph(tmp_path_factory):
    svc = AnalysisService(Settings(workspace_dir=tmp_path_factory.mktemp("ws"), log_level="WARNING"))
    a = svc.analyze(str(APPS / "login_app"))
    return RepositoryGraph.from_analysis(a), a.unresolved


FIND_USER = "python:repositories/user_repository.py:UserRepository.find_by_username"


def test_default_reports_referrers_of_an_element_as_indirect(login_graph):
    graph, unresolved = login_graph
    report = ImpactService(graph, unresolved).analyze_impact(FIND_USER)
    by_id = {c.entity_id: c for c in report.impacted}
    assert by_id["file:static/login.js"].category == "indirect" and "file:static/index.html" in by_id
    assert report.blast_radius == 7


def test_strict_mode_stops_at_the_element_and_keeps_everything_else(login_graph):
    graph, unresolved = login_graph
    default = {c.entity_id for c in ImpactService(graph, unresolved).analyze_impact(FIND_USER).impacted}
    strict = {c.entity_id for c in ImpactService(graph, unresolved, strict_references=True).analyze_impact(FIND_USER).impacted}
    assert default - strict == {"file:static/login.js", "file:static/index.html"}
    assert strict < default and "html:static/index.html:#login-form" in strict       # the element itself is still reported


def test_strict_mode_still_reaches_referrers_when_the_element_itself_changes(login_graph):
    graph, unresolved = login_graph
    form = "html:static/index.html:#login-form"
    strict = ImpactService(graph, unresolved, strict_references=True).analyze_impact(form)
    assert "file:static/login.js" in {c.entity_id for c in strict.impacted}           # login.js looks the element up
    per_call = ImpactService(graph, unresolved).analyze_impact(FIND_USER, strict=True)  # per-call override
    assert per_call.blast_radius == 5


def test_strict_flag_through_the_api_and_the_report_text(tmp_path):
    c = TestClient(create_app(Settings(workspace_dir=tmp_path / "ws", log_level="WARNING")))
    assert c.post("/api/repositories", json={"location": str(APPS / "login_app")}).status_code == 200
    base = "/api/repositories/login_app/impact"
    assert c.get(base, params={"target": FIND_USER}).json()["blast_radius"] == 7
    assert c.get(base, params={"target": FIND_USER, "strict": True}).json()["blast_radius"] == 5
    assert c.post(base, json={"target": FIND_USER, "strict_references": True}).json()["blast_radius"] == 5
    full = c.get(base, params={"target": FIND_USER}).json()
    assert full["risk_factors"] and all({"factor", "severity", "detail", "evidence"} <= set(f) for f in full["risk_factors"])
    assert full["suggested_test_details"] == [] and full["change_kinds"] == {FIND_USER: "target"}
