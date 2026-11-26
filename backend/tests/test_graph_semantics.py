"""Graph semantics against hand-written ground truth (backend/eval_data/graph_semantics.json):
exact shortest execution paths, paths that must not exist, and reverse-impact answers."""
import json
from pathlib import Path

import pytest

from repomind.config import Settings
from repomind.graph import RepositoryGraph
from repomind.services.analysis_service import AnalysisService
from repomind.services.impact_service import ImpactService

BACKEND = Path(__file__).resolve().parents[1]
GT = json.loads((BACKEND / "eval_data" / "graph_semantics.json").read_text(encoding="utf-8"))
APPS = {k: v for k, v in GT.items() if not k.startswith("_")}


@pytest.fixture(scope="module")
def graphs(tmp_path_factory):
    svc = AnalysisService(Settings(workspace_dir=tmp_path_factory.mktemp("ws"), log_level="WARNING"))
    return {app: RepositoryGraph.from_analysis(svc.analyze(str(BACKEND.parent / "test_apps" / app))) for app in APPS}


CASES = [(app, p["nodes"]) for app, spec in APPS.items() for p in spec["paths"]]
NEGATIVE = [(app, a, b) for app, spec in APPS.items() for a, b in spec["no_path"]]
IMPACTS = [(app, i) for app, spec in APPS.items() for i in spec["impact"]]


@pytest.mark.parametrize("app, nodes", CASES, ids=[f"{a}:{n[0].split(':')[-1]}->{n[-1].split(':')[-1]}" for a, n in CASES])
def test_shortest_execution_path_is_exactly_the_expected_chain(graphs, app, nodes):
    paths = graphs[app].find_paths(nodes[0], nodes[-1])
    assert paths, f"no execution path {nodes[0]} -> {nodes[-1]}"
    assert paths[0] == nodes


@pytest.mark.parametrize("app, a, b", NEGATIVE, ids=[f"{a}:{x.split(':')[-1]}-/->{y.split(':')[-1]}" for a, x, y in NEGATIVE])
def test_forbidden_execution_paths_do_not_exist(graphs, app, a, b):
    assert graphs[app].find_paths(a, b) == []


@pytest.mark.parametrize("app, spec", IMPACTS, ids=[f"{a}:{s['target'].split(':')[-1]}" for a, s in IMPACTS])
def test_impact_includes_dependents_and_excludes_unrelated_components(graphs, app, spec):
    report = ImpactService(graphs[app]).analyze_impact(spec["target"])
    hit = {c.entity_id for c in report.impacted}
    assert set(spec["must_include"]) <= hit, sorted(set(spec["must_include"]) - hit)
    assert not set(spec["must_exclude"]) & hit, sorted(set(spec["must_exclude"]) & hit)
