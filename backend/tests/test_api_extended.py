import io
import time
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from repomind.api.app import create_app
from repomind.config import Settings

APPS = Path(__file__).resolve().parents[2] / "test_apps"
LOGIN, NOTES = str(APPS / "login_app"), str(APPS / "notes_app")
DIFF = ("--- a/services/auth_service.py\n+++ b/services/auth_service.py\n"
        "@@ -9,1 +9,1 @@\n-x\n+y\n")


def _client(tmp_path, **kw):
    return TestClient(create_app(Settings(workspace_dir=tmp_path / "ws", log_level="WARNING", **kw)))


@pytest.fixture()
def client(tmp_path):
    c = _client(tmp_path)
    assert c.post("/api/repositories", json={"location": LOGIN}).status_code == 200
    return c


R = "/api/repositories/login_app"


def test_repositories_survive_a_restart(tmp_path):
    c1 = _client(tmp_path)
    c1.post("/api/repositories", json={"location": LOGIN})
    c2 = _client(tmp_path)                                    # brand-new app, same workspace
    assert [r["repo_id"] for r in c2.get("/api/repositories").json()] == ["login_app"]
    assert c2.post(f"{R}/ask", json={"question": "How does the login flow work?"}).json()["grounded"]


def test_delete_repository(client):
    assert client.delete(R).json() == {"deleted": "login_app"}
    assert client.get(R).status_code == 404
    assert client.get("/api/repositories").json() == []


def test_background_analysis_reports_job_status(tmp_path):
    c = _client(tmp_path)
    job = c.post("/api/repositories", params={"background": True}, json={"location": NOTES}).json()
    assert job["status"] in ("running", "done")
    for _ in range(50):
        state = c.get(f"/api/jobs/{job['job_id']}").json()
        if state["status"] != "running":
            break
        time.sleep(0.1)
    assert state["status"] == "done" and state["repo_id"] == "notes_app"
    assert c.get("/api/repositories/notes_app").json()["entity_count"] == 52
    bad = c.post("/api/repositories", params={"background": True}, json={"location": "/no/such/dir"}).json()
    time.sleep(0.3)
    assert c.get(f"/api/jobs/{bad['job_id']}").json()["status"] == "error"
    assert c.get("/api/jobs/nope").status_code == 404


def _zip_bytes(entries):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for n, d in entries.items():
            z.writestr(n, d)
    return buf.getvalue()


def test_zip_upload_end_to_end(tmp_path):
    c = _client(tmp_path)
    data = _zip_bytes({"proj/app.py": "def hello():\n    pass\n"})
    r = c.post("/api/repositories/upload", params={"name": "My Proj.zip"}, content=data)
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "proj" and r.json()["entity_count"] >= 2
    assert not list((tmp_path / "ws" / "uploads").glob("*.zip"))      # temp upload cleaned up


def test_upload_rejects_non_zip_garbage_and_oversize(tmp_path):
    c = _client(tmp_path, max_zip_mb=1)
    assert c.post("/api/repositories/upload", params={"name": "x.exe"}, content=b"hi").status_code == 400
    assert c.post("/api/repositories/upload", params={"name": "x.zip"}, content=b"not a zip").status_code == 400
    assert c.post("/api/repositories/upload", params={"name": "x.zip"}, content=b"0" * (1024 * 1024 + 10)).status_code == 413
    evil = _zip_bytes({"../evil.py": "x=1"})
    assert c.post("/api/repositories/upload", params={"name": "e.zip"}, content=evil).status_code == 400


def test_relationships_endpoint_filters(client):
    d = client.get(f"{R}/relationships", params={"type": "calls", "kind": "inferred"}).json()
    assert d["total"] > 0 and all(r["type"] == "CALLS" and r["kind"] == "inferred" for r in d["relationships"])
    d2 = client.get(f"{R}/relationships", params={"target": "api:POST:/api/login", "limit": 1}).json()
    assert d2["total"] == 2 and len(d2["relationships"]) == 1      # REQUESTS + HANDLES


def test_unresolved_endpoint_lists_external_packages_with_reasons(client):
    d = client.get(f"{R}/unresolved").json()
    assert d["counts"]["external_package"] >= 4
    assert {i["reason"] for i in d["items"]} == {"external_package"}


def test_post_trace_impact_and_chat_alias(client):
    t = client.post(f"{R}/trace", json={"source": "#login-form", "target": "UserRepository.find_by_username"}).json()
    assert len(t["paths"][0]["nodes"]) == 6
    i = client.post(f"{R}/impact", json={"target": "UserRepository.find_by_username"}).json()
    assert i["by_category"]["direct"] == 1 and i["risk"] == "high"
    d = client.post(f"{R}/impact", json={"diff": DIFF}).json()
    assert "python:app.py:login" in {c["entity_id"] for c in d["direct"] + d["transitive"]}
    assert client.post(f"{R}/chat", json={"question": "Who calls AuthService.authenticate?"}).json()["intent"] == "relations"


def test_input_validation(client):
    assert client.post(f"{R}/impact", json={}).status_code == 422                       # neither target nor diff
    assert client.post(f"{R}/impact", json={"target": "a", "diff": "b"}).status_code == 422
    assert client.post(f"{R}/trace", json={"source": "a", "target": "b", "max_depth": 0}).status_code == 422
    assert client.post(f"{R}/ask", json={"question": ""}).status_code == 422
    assert client.post(f"{R}/ask", json={"question": "x" * 5000}).status_code == 422
    assert client.post("/api/repositories", json={"location": ""}).status_code == 422
    assert client.get(f"{R}/entities", params={"limit": 0}).status_code == 422


def test_entity_filters_and_local_paths_can_be_disabled(client, tmp_path, monkeypatch):
    py = client.get(f"{R}/entities", params={"language": "python", "type": "class"}).json()
    assert {e["name"] for e in py} == {"LoginRequest", "AuthService", "UserRepository"}
    monkeypatch.setenv("REPOMIND_ALLOW_LOCAL_PATHS", "0")
    assert _client(tmp_path / "x").post("/api/repositories", json={"location": LOGIN}).status_code == 403


def test_corrupt_store_entry_does_not_break_listing(tmp_path):
    c = _client(tmp_path)
    store = tmp_path / "ws" / "analyses"
    store.mkdir(parents=True)
    (store / "broken.json").write_text("{nope", encoding="utf-8")
    assert c.get("/api/repositories/broken").status_code == 404
    assert c.get("/api/repositories/..%2Fetc").status_code in (404, 422)
