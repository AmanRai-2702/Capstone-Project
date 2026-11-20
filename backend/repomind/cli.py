import argparse
import sys

from .config import Settings
from .ingestion.errors import IngestionError
from .logging_setup import setup_logging
from .services.analysis_service import AnalysisService
from .services.repository_service import RepositoryService


def print_inventory(inv) -> None:
    print(f"Repository : {inv.name}")
    print(f"Origin     : {inv.origin}")
    print(f"Files kept : {len(inv.files)}")
    print("By language (source files):")
    for lang, n in sorted(inv.language_counts.items()):
        print(f"  {lang:<14}{n}")
    print("By category:")
    for cat, n in sorted(inv.category_counts.items()):
        print(f"  {cat:<14}{n}")
    print("Skipped    :", inv.skipped or "nothing")


def print_analysis(analysis) -> None:
    print("=" * 64)
    print(f"RepoMind Analysis: {analysis.name}")
    print(f"Origin : {analysis.origin}")
    print("=" * 64)
    print(f"Entities Discovered ({analysis.entity_count}):")
    for etype, count in sorted(analysis.entity_counts_by_type.items()):
        print(f"  {etype:<18} {count}")

    print(f"\nRelationships Discovered ({analysis.relationship_count}):")
    for rtype, count in sorted(analysis.relationship_counts_by_type.items()):
        print(f"  {rtype:<18} {count}")

    print("\nBy Detection Kind:")
    for kind, count in sorted(analysis.relationship_counts_by_kind.items()):
        print(f"  {kind:<18} {count}")

    print("\nSample Cross-Language Code Traces:")
    # Highlight cross-language connections (TRIGGERS, REQUESTS, HANDLES)
    triggers = analysis.get_relationships_by_type("TRIGGERS")
    requests = analysis.get_relationships_by_type("REQUESTS")
    handles = analysis.get_relationships_by_type("HANDLES")

    for t in triggers[:3]:
        print(f"  [UI Trigger]   {t.source_id.split(':')[-1]} --TRIGGERS--> {t.target_id.split(':')[-1]}")
    for req in requests[:3]:
        print(f"  [HTTP Request] {req.source_id.split(':')[-1]} --REQUESTS--> {req.target_id}")
    for h in handles[:3]:
        print(f"  [API Handler]  {h.source_id.split(':')[-1]} --HANDLES--> {h.target_id}")
    print("=" * 64)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="RepoMind: Multi-Language Repository Intelligence and Code Impact Analysis"
    )
    parser.add_argument("command_or_location", help="Subcommand ('ingest' or 'analyze') or repository location")
    parser.add_argument("location_opt", nargs="?", default=None, help="Repository location if subcommand was used")
    parser.add_argument("--analyze", action="store_true", help="perform deep multi-language AST/CST analysis")
    parser.add_argument("--json", action="store_true", help="print output as JSON")
    args = parser.parse_args()

    # Determine command and target location
    if args.command_or_location in ("ingest", "analyze"):
        command = args.command_or_location
        location = args.location_opt
        if not location:
            parser.error(f"Command '{command}' requires a location argument.")
    else:
        command = "analyze" if args.analyze else "ingest"
        location = args.command_or_location

    settings = Settings.from_env()
    setup_logging(settings.log_level)

    try:
        if command == "analyze":
            analysis = AnalysisService(settings).analyze(location)
            if args.json:
                print(analysis.model_dump_json(indent=2))
            else:
                print_analysis(analysis)
        else:
            inv = RepositoryService(settings).ingest(location)
            if args.json:
                print(inv.model_dump_json(indent=2))
            else:
                print_inventory(inv)
    except IngestionError as exc:
        raise SystemExit(f"Error: {exc}")


if __name__ == "__main__":
    main()