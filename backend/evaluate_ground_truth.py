"""Evaluate RepoMind against every benchmark app that has a ground_truth.json.

    python backend/evaluate_ground_truth.py              # all apps in test_apps/
    python backend/evaluate_ground_truth.py login_app    # one app

Per app it reports exact-match precision/recall/F1 for entities and relationships,
broken down by relationship type and by detection kind, plus two integrity checks that
need no ground truth:
  * dangling edges  - relationships whose source/target entity does not exist
  * forbidden edges - known false positives listed in ground_truth.json

Exit code is 1 if any app has missing items, dangling edges or forbidden edges, so this
doubles as a regression gate. Honest reading: recall < 100% means RepoMind missed something
that is really in the code; precision < 100% needs a manual look (the extra edge may be
correct but absent from the ground truth).
"""
import json
import sys
from pathlib import Path

from repomind.services.analysis_service import AnalysisService

PROJECT_ROOT = Path(__file__).resolve().parents[1]
APPS_DIR = PROJECT_ROOT / "test_apps"


def calculate_scores(expected: set, actual: set) -> dict:
    correct = expected & actual
    precision = len(correct) / len(actual) if actual else 0.0
    recall = len(correct) / len(expected) if expected else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "expected": len(expected), "actual": len(actual), "correct": len(correct),
        "missing": expected - actual, "extra": actual - expected,
        "precision": precision, "recall": recall, "f1": f1,
    }


def _pct(x: float) -> str:
    return f"{x:.1%}"


def _strip(r: dict) -> dict:
    return {k: v for k, v in r.items() if k not in ("missing", "extra")}


def evaluate_app(app_dir: Path) -> dict:
    gt = json.loads((app_dir / "ground_truth.json").read_text(encoding="utf-8"))
    analysis = AnalysisService().analyze(str(app_dir))

    exp_ent = {(i, t) for i, t in gt["entities"]}
    act_ent = {(e.id, e.type.value) for e in analysis.entities}
    exp_rel = {tuple(r) for r in gt["relationships"]}
    act_rel = {(r.source_id, r.type.value, r.target_id, r.kind.value) for r in analysis.relationships}
    forbidden = {tuple(r) for r in gt.get("forbidden_relationships", [])}

    ent = calculate_scores(exp_ent, act_ent)
    rel = calculate_scores(exp_rel, act_rel)
    ids = {e.id for e in analysis.entities}
    dangling = sorted(r.key for r in analysis.relationships if r.source_id not in ids or r.target_id not in ids)
    forbidden_hit = sorted(forbidden & act_rel)
    by_type = {t: calculate_scores({x for x in exp_rel if x[1] == t}, {x for x in act_rel if x[1] == t})
               for t in sorted({x[1] for x in exp_rel | act_rel})}
    by_kind = {k: calculate_scores({x for x in exp_rel if x[3] == k}, {x for x in act_rel if x[3] == k})
               for k in ("detected", "inferred", "heuristic")}
    ok = not ent["missing"] and not rel["missing"] and not dangling and not forbidden_hit
    return {
        "app": app_dir.name, "analysis": analysis, "entities": ent, "relationships": rel,
        "by_type": by_type, "by_kind": by_kind, "dangling": dangling, "forbidden_hit": forbidden_hit, "ok": ok,
    }


def print_app(res: dict) -> None:
    a = res["analysis"]
    print(f"\n{'=' * 70}\n{res['app']}  ({a.entity_count} entities, {a.relationship_count} relationships, "
          f"{len(a.unresolved)} unresolved)\n{'=' * 70}")
    for title, r in (("ENTITIES", res["entities"]), ("RELATIONSHIPS", res["relationships"])):
        print(f"{title}: expected={r['expected']} actual={r['actual']} correct={r['correct']} "
              f"P={_pct(r['precision'])} R={_pct(r['recall'])} F1={_pct(r['f1'])}")
    print("  by type:  " + "  ".join(f"{t} {r['correct']}/{r['expected']}" for t, r in res["by_type"].items() if r["expected"]))
    print("  by kind:  " + "  ".join(f"{k} {r['correct']}/{r['expected']}" for k, r in res["by_kind"].items() if r["expected"]))
    for label, items in (
        ("MISSING entities", res["entities"]["missing"]), ("EXTRA entities", res["entities"]["extra"]),
        ("MISSING relationships", res["relationships"]["missing"]), ("EXTRA relationships", res["relationships"]["extra"]),
        ("DANGLING edges", res["dangling"]), ("FORBIDDEN edges present", res["forbidden_hit"]),
    ):
        if items:
            print(f"\n{label}:")
            for i in sorted(items, key=str):
                print("  -", i)
    if a.unresolved:
        print("\nUNRESOLVED (reported, not guessed):")
        for u in a.unresolved:
            print(f"  - [{u.reason}] {u.source_id} -{u.type}-> {u.target_id}")
    print("RESULT:", "PASS" if res["ok"] else "FAIL")


def main(argv: list[str]) -> int:
    names = argv or sorted(p.parent.name for p in APPS_DIR.glob("*/ground_truth.json"))
    results = [evaluate_app(APPS_DIR / n) for n in names]
    report = {}
    for res in results:
        print_app(res)
        report[res["app"]] = {
            "entities": _strip(res["entities"]), "relationships": _strip(res["relationships"]),
            "relationships_by_type": {t: _strip(r) for t, r in res["by_type"].items()},
            "relationships_by_kind": {k: _strip(r) for k, r in res["by_kind"].items()},
            "dangling_edges": res["dangling"], "forbidden_edges_present": [list(x) for x in res["forbidden_hit"]],
            "unresolved": [u.model_dump(mode="json") for u in res["analysis"].unresolved],
            "missing_entities": sorted(res["entities"]["missing"]), "extra_entities": sorted(res["entities"]["extra"]),
            "missing_relationships": sorted(res["relationships"]["missing"]),
            "extra_relationships": sorted(res["relationships"]["extra"]),
        }
    tot_exp = sum(r["relationships"]["expected"] for r in results)
    tot_cor = sum(r["relationships"]["correct"] for r in results)
    tot_act = sum(r["relationships"]["actual"] for r in results)
    report["_overall"] = {"apps": len(results), "relationship_recall": tot_cor / tot_exp if tot_exp else 0.0,
                          "relationship_precision": tot_cor / tot_act if tot_act else 0.0}
    print(f"\nOVERALL ({len(results)} apps): relationship recall {_pct(report['_overall']['relationship_recall'])}, "
          f"precision {_pct(report['_overall']['relationship_precision'])}")
    (PROJECT_ROOT / "evaluation_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return 0 if all(r["ok"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
