from pathlib import Path

import pytest

from repomind.config import Settings
from repomind.graph import RepositoryGraph
from repomind.services.analysis_service import AnalysisService
from repomind.services.impact_service import ImpactError, ImpactService

LOGIN_APP = Path(__file__).resolve().parents[2] / "test_apps" / "login_app"

FORM = "html:static/index.html:#login-form"
HANDLE_LOGIN = "javascript:static/login.js:handleLogin"
ENDPOINT = "api:POST:/api/login"
LOGIN = "python:app.py:login"
AUTHENTICATE = "python:services/auth_service.py:AuthService.authenticate"
FIND_BY_USERNAME = "python:repositories/user_repository.py:UserRepository.find_by_username"


@pytest.fixture(scope="module")
def svc(tmp_path_factory) -> ImpactService:
    settings = Settings(workspace_dir=tmp_path_factory.mktemp("ws"), log_level="WARNING")
    analysis = AnalysisService(settings).analyze(str(LOGIN_APP))
    return ImpactService(RepositoryGraph.from_analysis(analysis))


def test_repository_change_reaches_the_login_form(svc):
    """Changing the DB repository method ripples all the way up to the UI."""
    report = svc.analyze_impact(FIND_BY_USERNAME)
    by_id = {c.entity_id: c for c in report.impacted}

    assert by_id[AUTHENTICATE].distance == 1
    assert by_id[LOGIN].distance == 2
    assert by_id[ENDPOINT].distance == 3
    assert by_id[HANDLE_LOGIN].distance == 4
    assert by_id[FORM].distance == 5
    assert FORM in report.affected_entry_points and ENDPOINT in report.affected_entry_points
    assert report.risk == "high"


def test_impact_is_directional(svc):
    """Changing the UI must not impact the database layer."""
    report = svc.analyze_impact(FORM)
    ids = {c.entity_id for c in report.impacted}
    assert FIND_BY_USERNAME not in ids and AUTHENTICATE not in ids


def test_explanation_and_confidence(svc):
    report = svc.analyze_impact(FIND_BY_USERNAME)
    form = next(c for c in report.impacted if c.entity_id == FORM)
    assert [h.relation for h in form.chain] == ["TRIGGERS", "REQUESTS", "HANDLES", "CALLS", "CALLS"]
    assert form.has_inferred_hop is True
    assert form.confidence == pytest.approx(0.85 * 0.85)   # two inferred CALLS hops
    assert "calls" in form.explanation and "is served by" in form.explanation


def test_container_target_uses_contents_as_changed(svc):
    report = svc.analyze_impact("file:services/auth_service.py")
    assert AUTHENTICATE in report.changed
    assert LOGIN in {c.entity_id for c in report.impacted}


def test_resolves_short_names(svc):
    assert svc.analyze_impact("UserRepository.find_by_username").target == FIND_BY_USERNAME


def test_unknown_and_ambiguous_targets_raise(svc):
    with pytest.raises(ImpactError):
        svc.analyze_impact("nothing_like_this_xyz")
    with pytest.raises(ImpactError):
        svc.analyze_impact("find_by")   # matches find_by_username and find_by_id


def test_max_depth_limits_radius(svc):
    report = svc.analyze_impact(FIND_BY_USERNAME, max_depth=2)
    assert max(c.distance for c in report.impacted) == 2
