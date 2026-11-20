
import json
from pathlib import Path

from repomind.services.analysis_service import AnalysisService

# Project locations
PROJECT_ROOT = Path(__file__).resolve().parents[1]
APP_DIR = PROJECT_ROOT / "test_apps" / "login_app"
GROUND_TRUTH_FILE = APP_DIR / "ground_truth.json"


def calculate_scores(expected, actual):
    """Calculate exact-match precision, recall, and F1."""
    correct = expected & actual

    precision = len(correct) / len(actual) if actual else 0.0
    recall = len(correct) / len(expected) if expected else 0.0

    f1 = (
        2 * precision * recall / (precision + recall)
        if precision + recall
        else 0.0
    )

    return {
        "expected": len(expected),
        "actual": len(actual),
        "correct": len(correct),
        "missing": expected - actual,
        "extra": actual - expected,
        "precision": precision,
        "recall": recall,
        "f1": f1,
    }


def show_results(title, result):
    print(f"\n{'=' * 65}")
    print(title)
    print("=" * 65)
    print(f"Expected items : {result['expected']}")
    print(f"Actual items   : {result['actual']}")
    print(f"Exact matches  : {result['correct']}")
    print(f"Missing items  : {len(result['missing'])}")
    print(f"Extra items    : {len(result['extra'])}")
    print(f"Precision      : {result['precision']:.2%}")
    print(f"Recall         : {result['recall']:.2%}")
    print(f"F1-score       : {result['f1']:.2%}")

    if result["missing"]:
        print("\nMISSING — expected but not detected:")
        for item in sorted(result["missing"]):
            print("  -", item)

    if result["extra"]:
        print("\nEXTRA — detected but not in ground truth:")
        for item in sorted(result["extra"]):
            print("  -", item)


def main():
    if not GROUND_TRUTH_FILE.exists():
        raise FileNotFoundError(
            f"Ground-truth file not found: {GROUND_TRUTH_FILE}"
        )

    ground_truth = json.loads(
        GROUND_TRUTH_FILE.read_text(encoding="utf-8")
    )

    print("Running RepoMind analysis...")
    analysis = AnalysisService().analyze(str(APP_DIR))

    # Compare entity ID + entity type
    expected_entities = {
        (entity_id, entity_type)
        for entity_id, entity_type in ground_truth["entities"]
    }

    actual_entities = {
        (entity.id, entity.type.value)
        for entity in analysis.entities
    }

    # Compare source, relationship type, target, and detection kind
    expected_relationships = {
        (source, relation_type, target, kind)
        for source, relation_type, target, kind
        in ground_truth["relationships"]
    }

    actual_relationships = {
        (
            relation.source_id,
            relation.type.value,
            relation.target_id,
            relation.kind.value,
        )
        for relation in analysis.relationships
    }

    entity_results = calculate_scores(
        expected_entities, actual_entities
    )
    relationship_results = calculate_scores(
        expected_relationships, actual_relationships
    )

    print(f"\nRepository: {analysis.name}")
    print(f"Total analyzed entities: {analysis.entity_count}")
    print(f"Total analyzed relationships: {analysis.relationship_count}")

    show_results("ENTITY EVALUATION", entity_results)
    show_results("RELATIONSHIP EVALUATION", relationship_results)

    # Save a machine-readable report
    report = {
        "repository": analysis.name,
        "entities": {
            key: value
            for key, value in entity_results.items()
            if key not in ("missing", "extra")
        },
        "relationships": {
            key: value
            for key, value in relationship_results.items()
            if key not in ("missing", "extra")
        },
        "missing_entities": sorted(entity_results["missing"]),
        "extra_entities": sorted(entity_results["extra"]),
        "missing_relationships": sorted(
            relationship_results["missing"]
        ),
        "extra_relationships": sorted(
            relationship_results["extra"]
        ),
    }

    report_path = PROJECT_ROOT / "evaluation_report.json"
    report_path.write_text(
        json.dumps(report, indent=2),
        encoding="utf-8",
    )

    print(f"\nReport saved to: {report_path}")


if __name__ == "__main__":
    main()
