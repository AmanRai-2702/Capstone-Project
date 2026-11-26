from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from repomind.api.app import create_app
from repomind.config import Settings

LOGIN_APP = Path(__file__).resolve().parents[2] / "test_apps" / "login_app"
FIND = "python:repositories/user_repository.py:UserRepository.find_by_username"
FORM = "html:static/index.html:#login-form"


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    app = create_app(Settings(workspace_dir=tmp_path_factory.mktemp("ws"), log_level="WARNING"))
    c = TestClient(app)
    r = c.post("/api/repositories", json={"location": str(LOGIN_APP)})
    assert r.status_code == 200, r.text
    return c


R = "/api/repositories/login_app"
EXPECTED_RELS = 63   # keep in sync with login_app/ground_truth.json (phase 6: +2 READS_STATE, anonymous handler entity)


def test_summary(client):
    d = client.get(R).json()
    assert d["name"] == "login_app" and d["entity_count"] == 40 and d["relationship_count"] == EXPECTED_RELS
    assert client.get("/api/repositories").json()[0]["repo_id"] == "login_app"


def test_unknown_repository_404(client):
    assert client.get("/api/repositories/nope").status_code == 404


def test_bad_location_400(client):
    r = client.post("/api/repositories", json={"location": "/definitely/not/here"})
    assert r.status_code == 400


def test_graph_and_entities(client):
    g = client.get(f"{R}/graph").json()
    assert len(g["nodes"]) == 40 and len(g["edges"]) == EXPECTED_RELS
    assert len(client.get(f"{R}/graph", params={"include_files": False}).json()["nodes"]) == 32
    found = client.get(f"{R}/entities", params={"q": "handleLogin"}).json()
    assert found[0]["id"] == "javascript:static/login.js:handleLogin"


def test_entity_detail(client):
    d = client.get(f"{R}/entity", params={"id": "AuthService.authenticate"}).json()
    assert d["entity"]["name"] == "authenticate"
    assert any(e["source"] == "python:app.py:login" for e in d["incoming"])
    assert client.get(f"{R}/entity", params={"id": "zzzz-none"}).status_code == 404


def test_trace(client):
    d = client.get(f"{R}/trace", params={"source": FORM, "target": FIND}).json()
    assert len(d["paths"][0]["nodes"]) == 6
    assert d["paths"][0]["hops"][0]["type"] == "TRIGGERS"


def test_flows(client):
    flows = client.get(f"{R}/flows").json()
    assert FORM in {f["entry"] for f in flows}


def test_impact(client):
    d = client.get(f"{R}/impact", params={"target": FIND}).json()
    assert d["risk"] == "high" and d["blast_radius"] == 7
    assert client.get(f"{R}/impact", params={"target": "xyzzy"}).status_code == 404


def test_ask(client):
    d = client.post(f"{R}/ask", json={"question": "How does the login flow work?"}).json()
    assert d["grounded"] and "handleLogin" in d["answer"] and d["citations"]
    assert client.post(f"{R}/ask", json={"question": "  "}).status_code == 422


def test_source_and_path_traversal_blocked(client):
    d = client.get(f"{R}/source", params={"file": "static/login.js", "start": 4, "end": 7}).json()
    assert d["lines"][0].startswith("function showMessage")
    assert client.get(f"{R}/source", params={"file": "../../HANDOVER.md"}).status_code == 404
