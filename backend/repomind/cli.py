import argparse
import re
import subprocess
import sys
from pathlib import Path

from .config import Settings
from .ingestion.errors import IngestionError
from .logging_setup import setup_logging
from .services.analysis_service import AnalysisService
from .services.repository_service import RepositoryService

COMMANDS = ("ingest", "analyze", "graph", "trace", "impact", "ask", "serve")
_GIT_REF = re.compile(r"^[A-Za-z0-9_./~^@{}][A-Za-z0-9_./~^@{}-]*$")   # cannot start with "-"


def git_diff(root: str, ref: str) -> str:
    """`git diff -U0 <ref>` for the analysed folder. The ref is validated so it can never be
    interpreted as a command-line option."""
    if not _GIT_REF.match(ref):
        raise ValueError(f"Invalid git reference: {ref!r}")
    proc = subprocess.run(["git", "-C", root, "diff", "--relative", "-U0", ref, "--"],
                          capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise ValueError(f"git diff failed: {proc.stderr.strip() or proc.returncode}")
    return proc.stdout


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

    if analysis.unresolved:
        print(f"\nNot connected (reported, never guessed): {analysis.unresolved_counts_by_reason}")
    for err in analysis.stats.get("analysis_errors", []):
        print(f"  ! {err}")

    print("\nSample Cross-Language Code Traces:")
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


def print_trace(graph, source: str, target: str) -> int:
    a, b = graph.resolve(source), graph.resolve(target)
    if not a or not b:
        print(f"Could not resolve {'source' if not a else 'target'}: {source if not a else target}")
        return 1
    paths = graph.find_paths(a[0].id, b[0].id)
    if not paths:
        print(f"No execution path from {a[0].id} to {b[0].id}")
        return 1
    for i, path in enumerate(paths, 1):
        print(f"Path {i} ({len(path) - 1} hops):")
        for hop in graph.describe_path(path):
            tag = f" [{hop['kind']}, {hop['confidence']:.2f}]" if hop["kind"] == "inferred" else ""
            print(f"  {hop['from']}\n    --{hop['type']}-->{tag} {hop['to']}")
    return 0


def print_impact(report) -> None:
    print("=" * 64)
    print(f"Impact of changing: {report.target}")
    print(f"Blast radius: {report.blast_radius}   Risk: {report.risk.upper()} ({report.risk_reason})")
    print(f"By language: {report.by_language}")
    print(f"By type    : {report.by_type}")
    print(f"By category: {report.by_category}")
    if report.suggested_tests:
        print("Suggested tests to re-run:")
        for t in report.suggested_tests:
            print(f"  - {t}")
    if report.affected_entry_points:
        print("User-facing entry points affected:")
        for e in report.affected_entry_points:
            print(f"  - {e}")
    for title, items in (("Direct", report.direct), ("Transitive", report.transitive)):
        if items:
            print(f"\n{title} impact:")
            for c in items:
                flag = " (uncertain)" if c.uncertain else ""
                print(f"  d={c.distance} {c.category} conf={c.confidence:.2f}{flag} {c.entity_id}")
                print(f"      why: {c.explanation}")
    print("=" * 64)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="repomind",
        description="RepoMind: Multi-Language Repository Intelligence and Code Impact Analysis",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    for name, help_ in (("ingest", "list the files RepoMind will analyse"),
                        ("analyze", "extract entities and relationships")):
        p = sub.add_parser(name, help=help_)
        p.add_argument("location", help="local folder or https://github.com/<owner>/<repo>")
        p.add_argument("--json", action="store_true", help="print output as JSON")
        p.add_argument("--exclude", action="append", default=[], metavar="PATTERN",
                       help="directory name or glob to skip (repeatable; also see .repomindignore)")

    p = sub.add_parser("graph", help="export the knowledge graph (json or graphml)")
    p.add_argument("location")
    p.add_argument("--format", choices=["json", "graphml"], default="json")
    p.add_argument("--output", "-o", help="file to write (default: stdout for json)")
    p.add_argument("--exclude", action="append", default=[], metavar="PATTERN")

    p = sub.add_parser("trace", help="execution paths between two components")
    p.add_argument("location")
    p.add_argument("--source", required=True, help="entity id, qualified name or name")
    p.add_argument("--target", required=True)
    p.add_argument("--exclude", action="append", default=[], metavar="PATTERN")

    p = sub.add_parser("impact", help="blast radius of changing a component")
    p.add_argument("location")
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--target", help="entity id, qualified name or name")
    group.add_argument("--since", metavar="GIT_REF", help="impact of everything changed since this git ref")
    group.add_argument("--diff-file", metavar="PATH", help="impact of a unified diff stored in a file")
    p.add_argument("--max-depth", type=int, default=10)
    p.add_argument("--json", action="store_true")
    p.add_argument("--exclude", action="append", default=[], metavar="PATTERN")

    p = sub.add_parser("ask", help="ask a question about the repository")
    p.add_argument("location")
    p.add_argument("question")
    p.add_argument("--provider", default=None, help="template (default, offline) or anthropic")
    p.add_argument("--json", action="store_true")
    p.add_argument("--exclude", action="append", default=[], metavar="PATTERN")

    p = sub.add_parser("serve", help="start the REST API and web UI")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # Backwards compatibility: `repomind <path> [--analyze]` used to mean ingest / analyze.
    if argv and argv[0] not in COMMANDS and not argv[0].startswith("-"):
        analyze = "--analyze" in argv
        argv = ["analyze" if analyze else "ingest"] + [a for a in argv if a != "--analyze"]
    args = build_parser().parse_args(argv)

    settings = Settings.from_env()
    setup_logging(settings.log_level)

    if args.command == "serve":
        import uvicorn
        uvicorn.run("repomind.api.app:app", host=args.host, port=args.port)
        return 0

    try:
        if args.command == "ingest":
            inv = RepositoryService(settings).ingest(args.location, exclude=args.exclude)
            if args.json:
                print(inv.model_dump_json(indent=2))
            else:
                print_inventory(inv)
            return 0

        analysis = AnalysisService(settings).analyze(args.location, exclude=args.exclude)
        if args.command == "analyze":
            if args.json:
                print(analysis.model_dump_json(indent=2))
            else:
                print_analysis(analysis)
            return 0

        from .graph import RepositoryGraph
        graph = RepositoryGraph.from_analysis(analysis)

        if args.command == "graph":
            if args.format == "graphml":
                if not args.output:
                    print("Error: --output is required for graphml")
                    return 1
                graph.export_graphml(args.output)
                print(f"Wrote {args.output}")
            else:
                import json
                text = json.dumps(graph.subgraph_json(), indent=2)
                if args.output:
                    Path(args.output).write_text(text, encoding="utf-8")
                    print(f"Wrote {args.output}")
                else:
                    print(text)
            return 0

        if args.command == "trace":
            return print_trace(graph, args.source, args.target)

        if args.command == "impact":
            from .services.impact_service import ImpactError, ImpactService
            service = ImpactService(graph)
            try:
                if args.target:
                    report = service.analyze_impact(args.target, args.max_depth)
                else:
                    diff = (Path(args.diff_file).read_text(encoding="utf-8") if args.diff_file
                            else git_diff(analysis.root_path, args.since))
                    report = service.analyze_diff(diff, args.max_depth)
            except (ImpactError, ValueError, OSError, subprocess.SubprocessError) as exc:
                print(f"Error: {exc}")
                return 1
            if args.json:
                print(report.model_dump_json(indent=2))
            else:
                print_impact(report)
            return 0

        if args.command == "ask":
            from .qa import QAService, get_provider
            ans = QAService(analysis, graph, get_provider(args.provider)).ask(args.question)
            if args.json:
                print(ans.model_dump_json(indent=2))
            else:
                print(ans.answer)
                for w in ans.warnings:
                    print(f"\nWarning: {w}")
            return 0 if ans.grounded else 2
    except IngestionError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
