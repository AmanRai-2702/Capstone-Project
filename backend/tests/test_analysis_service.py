import json
from pathlib import Path

import pytest

from repomind.config import Settings
from repomind.models import EntityType, RelationKind, RelationType, RepositoryAnalysis
from repomind.services.analysis_service import AnalysisService

LOGIN_APP = Path(__file__).resolve().parents[2] / "test_apps" / "login_app"


@pytest.fixture
def analysis_service(tmp_path: Path) -> AnalysisService:
    settings = Settings(workspace_dir=tmp_path, log_level="INFO")
    return AnalysisService(settings)


def test_full_repository_analysis_ground_truth(analysis_service: AnalysisService):
    analysis = analysis_service.analyze(str(LOGIN_APP))

    # Basic repository metadata
    assert analysis.name == "login_app"
    assert analysis.entity_count > 0
    assert analysis.relationship_count > 0

    # Ground truth validation
    gt = json.loads((LOGIN_APP / "ground_truth.json").read_text(encoding="utf-8"))

    # 1. Verify every entity in ground_truth.json is present in the analysis
    extracted_entity_ids = {e.id for e in analysis.entities}
    for expected_id, expected_type in gt["entities"]:
        assert expected_id in extracted_entity_ids, f"Missing entity {expected_id} ({expected_type})"

    # 2. Verify every relationship in ground_truth.json is present in the analysis
    extracted_rel_tuples = {
        (r.source_id, r.type.value, r.target_id, r.kind.value)
        for r in analysis.relationships
    }

    for src, rtype, tgt, kind in gt["relationships"]:
        assert (src, rtype, tgt, kind) in extracted_rel_tuples, (
            f"Missing relationship {src} -{rtype}-> {tgt} ({kind})"
        )


def test_cross_language_chain_is_connected(analysis_service: AnalysisService):
    """
    Test the fundamental value proposition of RepoMind:
    Cross-language trace from HTML -> JS -> API -> Python Service -> DB Repository.
    """
    analysis = analysis_service.analyze(str(LOGIN_APP))

    rels = {(r.source_id, r.type.value, r.target_id) for r in analysis.relationships}

    # 1. HTML form triggers JS handleLogin
    assert ("html:static/index.html:#login-form", "TRIGGERS", "javascript:static/login.js:handleLogin") in rels

    # 2. JS handleLogin requests POST /api/login
    assert ("javascript:static/login.js:handleLogin", "REQUESTS", "api:POST:/api/login") in rels

    # 3. Python login function handles POST /api/login
    assert ("python:app.py:login", "HANDLES", "api:POST:/api/login") in rels

    # 4. Python login calls AuthService.authenticate
    assert ("python:app.py:login", "CALLS", "python:services/auth_service.py:AuthService.authenticate") in rels

    # 5. AuthService.authenticate calls UserRepository.find_by_username
    assert (
        "python:services/auth_service.py:AuthService.authenticate",
        "CALLS",
        "python:repositories/user_repository.py:UserRepository.find_by_username",
    ) in rels


def test_repository_analysis_json_roundtrip(analysis_service: AnalysisService):
    analysis = analysis_service.analyze(str(LOGIN_APP))
    json_str = analysis.model_dump_json()
    reconstructed = RepositoryAnalysis.model_validate_json(json_str)

    assert reconstructed.repo_id == analysis.repo_id
    assert reconstructed.entity_count == analysis.entity_count
    assert reconstructed.relationship_count == analysis.relationship_count
