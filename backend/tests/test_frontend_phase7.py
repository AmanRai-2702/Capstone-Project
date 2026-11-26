"""Phase 7: the dashboard's backend data and static guarantees about the single-page UI.

The browser behaviour itself is covered by backend/e2e/ui_check.py and backend/e2e/ui_phase7.py (real Edge);
these tests run anywhere, with no browser."""
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from repomind.api.app import create_app
from repomind.config import Settings
from repomind.qa import QAService
from repomind.services.analysis_service import AnalysisService

APPS = Path(__file__).resolve().parents[2] / "test_apps"
INDEX = Path(__file__).resolve().parents[1] / "repomind" / "api" / "static" / "index.html"


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    c = TestClient(create_app(Settings(workspace_dir=tmp_path_factory.mktemp("ws"), log_level="WARNING")))
    assert c.post("/api/repositories", json={"location": str(APPS / "login_app")}).status_code == 200
    return c


# --------------------------------------------------------------------------- dashboard data
def test_summary_has_file_and_language_counts_for_the_dashboard(client):
    d = client.get("/api/repositories/login_app").json()
    assert d["language_counts"] == {"python": 3, "javascript": 1, "html": 1, "css": 1}
    assert d["source_file_count"] == 6 and d["file_count"] >= d["source_file_count"]
    assert sum(d["files_by_category"].values()) == d["file_count"] and d["files_by_category"]["source"] == 6


def test_overview_is_deterministic_grounded_and_needs_no_llm(client, tmp_path):
    d = client.get("/api/repositories/login_app/overview").json()
    assert d["headline"] == "Overview of `login_app`:"
    texts = " ".join(f["text"] for f in d["facts"])
    assert "Source files:" in texts and "python" in texts and "requirements.txt" in texts
    cited = [f["citation"] for f in d["facts"] if f["citation"]]
    assert cited and all(re.fullmatch(r".+:\d+-\d+", c) for c in cited)

    class Broken:                 # a provider that explodes must not affect the dashboard
        name, model = "broken", ""

        def generate(self, context):
            raise RuntimeError("must not be called")

    analysis = AnalysisService(Settings(workspace_dir=tmp_path, log_level="WARNING")).analyze(str(APPS / "login_app"))
    assert QAService(analysis, provider=Broken()).overview()["facts"]


def test_overview_unknown_repository_is_a_clean_404(client):
    assert client.get("/api/repositories/nope/overview").status_code == 404


# --------------------------------------------------------------------------- the page itself
def test_page_is_served_and_has_every_element_the_journeys_rely_on(client):
    html = client.get("/").text
    for element_id in ("loc", "go", "zip", "repoSel", "status", "cy", "level", "langSel", "typeSel", "relList", "minConf", "maxNodes",
                       "nodeSearch", "banner", "q", "chatlog", "chatform", "askBtn", "llmStatus", "chips", "impactBody", "strictRefs",
                       "traceFrom", "traceTo", "traceView", "traceGo", "traceOut", "flowList", "diffText", "diffGo", "diffOut",
                       "p-contracts", "p-gaps", "p-src", "p-detail", "entityList"):
        assert f'id="{element_id}"' in html, element_id
    for tab in ("ask", "detail", "impact", "flows", "src", "diff", "contracts", "gaps"):
        assert f'data-p="{tab}"' in html, tab


def test_ui_never_injects_repository_text_as_html():
    """Repository names, comments and source are untrusted: only text nodes / textContent may carry them."""
    code = re.sub(r"(?m)(^|\s)//.*$", "", INDEX.read_text(encoding="utf-8"))      # a comment may talk about innerHTML
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
        assert sink not in code, sink


def test_the_page_never_mentions_a_secret_or_calls_an_llm_directly():
    code = INDEX.read_text(encoding="utf-8").lower()
    assert "openrouter.ai" not in code and "api_key" not in code and "authorization" not in code
    assert "sk-" not in code


def test_every_api_path_the_ui_calls_exists(client):
    """A renamed or mistyped endpoint would otherwise only show up as a broken panel in a browser."""
    code = INDEX.read_text(encoding="utf-8")
    paths = set(re.findall(r"[`\"'](/api/[^`\"'?\s]*)", code))
    assert len(paths) >= 12
    routes = [r for r in client.app.routes if hasattr(r, "path") and hasattr(r, "methods")]

    def matches(path):
        generic = re.sub(r"\$\{[^}]*\}", "x", path).rstrip("/")
        return any(re.fullmatch(re.sub(r"\{[^}]+\}", "[^/]+", r.path), generic) for r in routes)

    missing = sorted(p for p in paths if not matches(p))
    assert not missing, missing


def test_ui_uses_each_new_endpoint():
    code = INDEX.read_text(encoding="utf-8")
    for endpoint in ("/graph/neighborhood", "/graph/edge", "/trace", "/contracts", "/contracts/impact", "/overview",
                     "/api/llm/status", "/impact", "/flows", "/unresolved", "/source", "/entity", "/ask"):
        assert endpoint in code, endpoint
