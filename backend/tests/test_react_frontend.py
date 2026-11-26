"""The React build is served at "/", client-side routes load it, and the legacy page stays reachable."""
import pytest
from fastapi.testclient import TestClient

import importlib

app_module = importlib.import_module("repomind.api.app")   # the package re-exports an `app` object of the same name
from repomind.config import Settings


@pytest.fixture
def dist(tmp_path):
    d = tmp_path / "dist"
    (d / "assets").mkdir(parents=True)
    (d / "index.html").write_text("<!doctype html><title>RepoMind React</title><div id=root></div>", encoding="utf-8")
    (d / "assets" / "app.js").write_text("console.log(1)", encoding="utf-8")
    (d / "favicon.svg").write_text("<svg/>", encoding="utf-8")
    return d


def make_client(tmp_path, monkeypatch, dist_dir):
    monkeypatch.setattr(app_module, "FRONTEND_DIST", dist_dir)
    return TestClient(app_module.create_app(Settings(workspace_dir=tmp_path / "ws", log_level="WARNING")))


def test_root_and_client_routes_serve_the_react_app(tmp_path, monkeypatch, dist):
    c = make_client(tmp_path, monkeypatch, dist)
    assert "RepoMind React" in c.get("/").text
    assert "RepoMind React" in c.get("/repo/abc123/graph").text            # deep link / browser refresh
    assert c.get("/assets/app.js").text == "console.log(1)"
    assert c.get("/favicon.svg").text == "<svg/>"


def test_api_paths_are_never_swallowed_by_the_spa_fallback(tmp_path, monkeypatch, dist):
    c = make_client(tmp_path, monkeypatch, dist)
    assert c.get("/api/repositories").json() == []
    assert c.get("/api/nope").status_code == 404
    assert c.get("/api/repositories/missing").status_code == 404


def test_path_traversal_cannot_leave_the_build_folder(tmp_path, monkeypatch, dist):
    (tmp_path / "secret.txt").write_text("TOP SECRET", encoding="utf-8")
    c = make_client(tmp_path, monkeypatch, dist)
    for attempt in ("/../secret.txt", "/%2e%2e/secret.txt", "/..%2fsecret.txt"):
        assert "TOP SECRET" not in c.get(attempt).text


def test_legacy_page_stays_available_and_is_the_fallback_without_a_build(tmp_path, monkeypatch, dist):
    assert 'id="chatform"' in make_client(tmp_path, monkeypatch, dist).get("/legacy").text
    c = make_client(tmp_path, monkeypatch, tmp_path / "does-not-exist")
    assert 'id="chatform"' in c.get("/").text
