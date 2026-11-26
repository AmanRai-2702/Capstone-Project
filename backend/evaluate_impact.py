"""Score change-impact analysis against hand-written ground truth (backend/eval_data/impact_gt.json).

    python backend/evaluate_impact.py [scenario-id-substring] [--json OUT]

For each scenario the set of components RepoMind reports as impacted is compared with the complete expected
set:   precision = reported-and-expected / reported      (how much of the report is right)
       recall    = reported-and-expected / expected      (how much of the real blast radius was found)
and, where the scenario pins them, whether each component got the expected category
(direct / execution_flow / data_flow / indirect).

Honest reading: 11 scenarios on 5 tiny apps. The expectations were written from the source before the tool
was run on them, so a mismatch is either a bug in the analysis or a mistake in the expectation; both are
worth knowing. This measures the dependency/impact logic on code the analyzers can read, it does not say
anything about how well the analyzers read unfamiliar repositories.
Both modes are scored (see ImpactService strict_references). Exit code 1 if any scenario has a missing or an
unexpected component in either mode."""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

from repomind.config import Settings                              # noqa: E402
from repomind.graph import RepositoryGraph                        # noqa: E402
from repomind.services.analysis_service import AnalysisService    # noqa: E402
from repomind.services.impact_service import ImpactService        # noqa: E402


def run(filter_text: str | None = None, strict: bool = False) -> dict:
    gt = json.loads((ROOT / "backend" / "eval_data" / "impact_gt.json").read_text(encoding="utf-8"))
    svc = AnalysisService(Settings(workspace_dir=Path(tempfile.mkdtemp()), log_level="ERROR"))
    graphs: dict[str, RepositoryGraph] = {}
    rows = []
    for sc in gt["scenarios"]:
        if filter_text and filter_text not in sc["id"]:
            continue
        if sc["app"] not in graphs:
            graphs[sc["app"]] = RepositoryGraph.from_analysis(svc.analyze(str(ROOT / "test_apps" / sc["app"])))
        report = ImpactService(graphs[sc["app"]], strict_references=strict).analyze_impact(sc["target"])
        got = {c.entity_id: c for c in report.impacted}
        expected = set(sc["expected"]) | (set() if strict else set(sc.get("structural", [])))
        hit = expected & set(got)
        cats = {i: c for i, c in sc.get("categories", {}).items() if i in expected}
        cat_ok = {i: (got[i].category if i in got else None) for i in cats}
        rows.append({
            "id": sc["id"], "expected": len(expected), "reported": len(got), "correct": len(hit),
            "precision": len(hit) / len(got) if got else 1.0, "recall": len(hit) / len(expected) if expected else 1.0,
            "missing": sorted(expected - set(got)), "unexpected": sorted(set(got) - expected),
            "category_checks": len(cats), "category_correct": sum(1 for i, c in cats.items() if cat_ok[i] == c),
            "category_wrong": {i: {"expected": cats[i], "got": cat_ok[i]} for i in cats if cat_ok[i] != cats[i]},
            "risk": report.risk,
        })
    tp = sum(r["correct"] for r in rows)
    reported, expected = sum(r["reported"] for r in rows), sum(r["expected"] for r in rows)
    checks = sum(r["category_checks"] for r in rows)
    return {"scenarios": rows, "overall": {
        "scenarios": len(rows), "precision": tp / reported if reported else 1.0, "recall": tp / expected if expected else 1.0,
        "category_accuracy": sum(r["category_correct"] for r in rows) / checks if checks else 1.0,
        "exact_matches": sum(1 for r in rows if not r["missing"] and not r["unexpected"])}}


def show(result: dict, title: str) -> None:
    print(f"\n=== {title} ===")
    print(f"{'scenario':38}{'expected':>9}{'reported':>9}{'P':>7}{'R':>7}  categories  risk")
    for r in result["scenarios"]:
        cats = f"{r['category_correct']}/{r['category_checks']}" if r["category_checks"] else "-"
        print(f"{r['id']:38}{r['expected']:>9}{r['reported']:>9}{r['precision']:>7.2f}{r['recall']:>7.2f}  {cats:>10}  {r['risk']}")
        for key, label in (("missing", "MISSING"), ("unexpected", "UNEXPECTED")):
            for item in r[key]:
                print(f"      {label}: {item}")
        for item, d in r["category_wrong"].items():
            print(f"      CATEGORY {item}: expected {d['expected']}, got {d['got']}")
    o = result["overall"]
    print(f"{o['scenarios']} scenarios: precision {o['precision']:.1%}, recall {o['recall']:.1%}, "
          f"category accuracy {o['category_accuracy']:.1%}, exact matches {o['exact_matches']}/{o['scenarios']}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("filter", nargs="?")
    ap.add_argument("--json", default=None)
    args = ap.parse_args()
    results = {"default": run(args.filter, strict=False), "strict": run(args.filter, strict=True)}
    show(results["default"], "default mode (structural dependents reported as 'indirect')")
    show(results["strict"], "strict mode (no impact through REFERENCES into an element's referrers)")
    if args.json:
        Path(args.json).write_text(json.dumps(results, indent=2), encoding="utf-8")
    return 0 if all(r["overall"]["exact_matches"] == r["overall"]["scenarios"] for r in results.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
