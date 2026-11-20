from pathlib import Path

import pytest

from repomind.config import Settings
from repomind.graph import RepositoryGraph
from repomind.services.analysis_service import AnalysisService
from repomind.services.impact_service import ImpactError, ImpactService, parse_unified_diff

LOGIN_APP = Path(__file__).resolve().parents[2] / "test_apps" / "login_app"
FIND = "python:repositories/user_repository.py:UserRepository.find_by_username"


@pytest.fixture(scope="module")
def svc(tmp_path_factory) -> ImpactService:
    settings = Settings(workspace_dir=tmp_path_factory.mktemp("ws"), log_level="WARNING")
    return ImpactService(RepositoryGraph.from_analysis(AnalysisService(settings).analyze(str(LOGIN_APP))))


def test_categories_distinguish_direct_execution_flow_and_indirect(svc):
    by_id = {c.entity_id: c for c in svc.analyze_impact(FIND).impacted}
    assert by_id["python:services/auth_service.py:AuthService.authenticate"].category == "direct"
    assert by_id["python:app.py:login"].category == "execution_flow"
    assert by_id["html:static/index.html:#login-form"].category == "execution_flow"
    assert by_id["file:static/login.js"].category == "indirect"       # reached through a REFERENCES edge


def test_uncertain_flag_follows_inferred_hops(svc):
    by_id = {c.entity_id: c for c in svc.analyze_impact(FIND).impacted}
    assert by_id["python:app.py:login"].uncertain                      # two inferred CALLS hops
    # impact of the JS function: all hops are detected
    report = svc.analyze_impact("javascript:static/login.js:showMessage")
    assert not next(c for c in report.impacted if c.entity_id == "javascript:static/login.js:handleLogin").uncertain


DIFF = """diff --git a/services/auth_service.py b/services/auth_service.py
--- a/services/auth_service.py
+++ b/services/auth_service.py
@@ -9,1 +9,2 @@ class AuthService:
-        user = self.users.find_by_username(username)
+        user = self.users.find_by_username(username.lower())
+        # normalised
"""


def test_parse_unified_diff_uses_new_file_line_numbers():
    assert parse_unified_diff(DIFF) == {"services/auth_service.py": [(9, 10)]}
    assert parse_unified_diff("--- a/x\n+++ /dev/null\n@@ -1,3 +0,0 @@\n") == {}


def test_diff_maps_to_smallest_entity_and_reports_impact(svc):
    ids = svc.entities_for_changes({"services/auth_service.py": [(9, 10)]})
    assert ids == ["python:services/auth_service.py:AuthService.authenticate"]   # method, not class/file
    report = svc.analyze_diff(DIFF)
    assert "python:app.py:login" in {c.entity_id for c in report.impacted}
    assert report.risk == "high"


def test_change_outside_any_entity_falls_back_to_the_file(svc):
    assert svc.entities_for_changes({"app.py": [(1, 1)]}) == ["file:app.py"]


def test_diff_touching_nothing_known_raises(svc):
    with pytest.raises(ImpactError):
        svc.analyze_diff("--- a/nope.py\n+++ b/nope.py\n@@ -1 +1 @@\n")


def test_suggested_tests_find_test_files_that_use_impacted_code(tmp_path):
    repo = tmp_path / "r"
    (repo / "tests").mkdir(parents=True)
    (repo / "core.py").write_text("def price():\n    return 1\n", encoding="utf-8")
    (repo / "shop.py").write_text("from core import price\n\ndef total():\n    return price()\n", encoding="utf-8")
    (repo / "tests" / "test_shop.py").write_text(
        "from shop import total\n\ndef test_total():\n    assert total() == 1\n", encoding="utf-8")
    (repo / "tests" / "test_other.py").write_text("def test_nothing():\n    pass\n", encoding="utf-8")
    analysis = AnalysisService(Settings(workspace_dir=tmp_path / "ws", log_level="WARNING")).analyze(str(repo))
    report = ImpactService(RepositoryGraph.from_analysis(analysis)).analyze_impact("price")
    assert report.suggested_tests == ["tests/test_shop.py"]
