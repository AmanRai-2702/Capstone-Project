"""Phase 8 evaluation on unfamiliar repositories.

    python backend/evaluate_external.py clone                 # fetch the pinned repositories (read-only use)
    python backend/evaluate_external.py robustness [id ...]   # analysis success, time, memory, censuses
    python backend/evaluate_external.py sample [id ...]       # stratified relationship sample for manual review
    python backend/evaluate_external.py retrieval             # docstring-as-query retrieval comparison
    python backend/evaluate_external.py qa [--provider ...]   # question answering quality (needs the LLM for openrouter)

Results are written to backend/eval_data/phase8_*.json. See EVALUATION.md for how to read them.

Honesty notes. A "census" is an independent re-count with separate code (ast decorators for routes, regular
expressions for `fetch(` and `id="..."`); it checks that nothing is silently dropped, it is not a full ground truth
(both sides can miss the same thing). Manual sampling is small-n and judged by one reviewer.
"""
from __future__ import annotations

import argparse
import ast
import json
import random
import re
import subprocess
import sys
import tempfile
import time
import tracemalloc
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
EXTERNAL = ROOT / "workspace" / "external"
DATA = ROOT / "backend" / "eval_data"

from repomind.config import Settings                                  # noqa: E402
from repomind.graph import RepositoryGraph                            # noqa: E402
from repomind.services.analysis_service import AnalysisService        # noqa: E402

CONFIG = json.loads((DATA / "external_repos.json").read_text(encoding="utf-8"))
TARGETS = {t["id"]: t for t in CONFIG["targets"]}
HTTP = {"get", "post", "put", "delete", "patch", "options", "head"}


def target_path(t: dict) -> Path:
    return (EXTERNAL / t["repo"] / t["path"]).resolve()


def settings() -> Settings:
    return Settings(workspace_dir=Path(tempfile.mkdtemp(prefix="repomind_p8_")), log_level="ERROR",
                    max_files=20_000, retrieval_mode="tfidf", llm_provider="template")


# ------------------------------------------------------------------ clone
def clone() -> None:
    EXTERNAL.mkdir(parents=True, exist_ok=True)
    for r in CONFIG["repos"]:
        dest = EXTERNAL / r["name"]
        if not dest.exists():
            subprocess.run(["git", "clone", "-q", "--depth", "1", r["url"], str(dest)], check=True, timeout=300)
        head = subprocess.run(["git", "-C", str(dest), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
        print(f"{r['name']}: {head[:10]} ({'matches pin' if head == r['commit'] else 'DIFFERENT from pinned ' + r['commit'][:10]})")


# ------------------------------------------------------------------ censuses (independent re-counts)
def py_census(root: Path, files: list[str]) -> tuple[set[tuple[str, str]], set[tuple[str, str]], int]:
    """(file, name) of every def/class, and the (METHOD, route) pairs of route decorators, by plain ast."""
    names, routes, unparsable = set(), set(), 0
    for rel in files:
        try:
            tree = ast.parse((root / rel).read_text(encoding="utf-8", errors="replace"))
        except (SyntaxError, ValueError):
            unparsable += 1
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add((rel, node.name))
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for dec in node.decorator_list:
                    if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute) and dec.args \
                            and isinstance(dec.args[0], ast.Constant) and isinstance(dec.args[0].value, str):
                        attr = dec.func.attr.lower()
                        if attr in HTTP:
                            routes.add((attr.upper(), dec.args[0].value))
                        elif attr in ("route", "api_route"):
                            methods = ["GET"]
                            for kw in dec.keywords:
                                if kw.arg == "methods" and isinstance(kw.value, (ast.List, ast.Tuple, ast.Set)):
                                    methods = [str(e.value).upper() for e in kw.value.elts if isinstance(e, ast.Constant)] or methods
                            routes.update((m, dec.args[0].value) for m in methods)
    return names, routes, unparsable


JS_FUNC = [re.compile(r"\bfunction\s*\*?\s+([A-Za-z_$][\w$]*)\s*\("),
           re.compile(r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:function\b|\([^)]*\)\s*=>|[A-Za-z_$][\w$]*\s*=>)"),
           re.compile(r"\bclass\s+([A-Za-z_$][\w$]*)")]
FETCH_SITE = re.compile(r"\b(?:fetch|axios(?:\.(?:get|post|put|patch|delete))?|\$\.(?:ajax|get|post))\s*\(")
FETCH_LITERAL = re.compile(r"\b(?:fetch|axios(?:\.(?:get|post|put|patch|delete))?)\s*\(\s*(['\"`])\s*/?[\w./:{}$-]")
HTML_ID = re.compile(r"""(?<![\w-])id\s*=\s*["']([^"'{}<>]+)["']""")      # not data-id= or similar


def js_census(root: Path, files: list[str]) -> set[tuple[str, str]]:
    out = set()
    for rel in files:
        text = (root / rel).read_text(encoding="utf-8", errors="replace")
        for rx in JS_FUNC:
            out.update((rel, m.group(1)) for m in rx.finditer(text))
    return out


def inline_script_files(root: Path, html_files: list[str]) -> list[str]:
    return html_files


# ------------------------------------------------------------------ robustness
def analyze_target(t: dict) -> dict:
    path = target_path(t)
    svc = AnalysisService(settings())
    start = time.perf_counter()
    a = svc.analyze(str(path))
    analyze_s = time.perf_counter() - start
    start = time.perf_counter()
    graph = RepositoryGraph.from_analysis(a)
    graph_s = time.perf_counter() - start

    files = [e for e in a.entities if e.type.value == "file"]
    by_lang = Counter(e.language.value for e in files if e.metadata.get("category") == "source")
    py_files = [e.qualified_name for e in files if e.language.value == "python" and e.metadata.get("category") == "source"]
    js_files = [e.qualified_name for e in files if e.language.value == "javascript" and e.metadata.get("category") == "source"]
    html_files = [e.qualified_name for e in files if e.language.value == "html" and e.metadata.get("category") == "source"]

    row = {"id": t["id"], "kind": t["kind"], "files": len(files), "source_files_by_language": dict(by_lang),
           "entities": a.entity_count, "relationships": a.relationship_count,
           "relationships_by_type": a.relationship_counts_by_type, "relationships_by_kind": a.relationship_counts_by_kind,
           "unresolved": len(a.unresolved), "unresolved_by_reason": dict(Counter(u.reason for u in a.unresolved)),
           "analysis_errors": a.stats.get("analysis_errors", []), "skipped_files": a.stats.get("skipped_files", {}),
           "analyze_seconds": round(analyze_s, 2), "graph_seconds": round(graph_s, 3)}
    # unresolved that are NOT simply "an external library" are gaps in our understanding
    row["unresolved_non_external"] = [f"{u.reason}: {u.source_id.split(':', 2)[-1]} -{u.type}-> {u.target_id.split(':', 2)[-1]}"
                                      for u in a.unresolved if u.reason != "external_package"][:15]

    ent_names = {}
    for e in a.entities:
        if e.type.value in ("function", "method", "class") and e.location:
            ent_names.setdefault(e.language.value, set()).add((e.location.file_path, e.name))
    if py_files:
        names, routes, bad = py_census(path, py_files)
        got = ent_names.get("python", set())
        endpoints = {(e.metadata.get("method"), e.metadata.get("route")) for e in a.entities if e.type.value == "api_endpoint"}
        row["python"] = {
            "census_definitions": len(names), "extracted": len(names & got), "definition_recall": round(len(names & got) / len(names), 4) if names else 1.0,
            "missing_examples": sorted(f"{f}:{n}" for f, n in names - got)[:8], "unparsable_files": bad,
            "census_routes": len(routes), "routes_found": len(routes & endpoints),
            "route_recall": round(len(routes & endpoints) / len(routes), 4) if routes else None,
            "routes_missed": sorted(f"{m} {r}" for m, r in routes - endpoints)[:8],
            "routes_extra": sorted(f"{m} {r}" for m, r in endpoints - routes)[:8]}
    if js_files:
        census = js_census(path, js_files)
        got = ent_names.get("javascript", set())
        sites = literal = 0
        for rel in js_files + html_files:
            text = (path / rel).read_text(encoding="utf-8", errors="replace")
            sites += len(FETCH_SITE.findall(text))
            literal += len(FETCH_LITERAL.findall(text))
        requests = [r for r in a.relationships if r.type.value == "REQUESTS"]
        row["javascript"] = {"census_definitions": len(census), "extracted": len(census & got),
                             "definition_recall": round(len(census & got) / len(census), 4) if census else 1.0,
                             "missing_examples": sorted(f"{f}:{n}" for f, n in census - got)[:8],
                             "request_call_sites": sites, "with_literal_url": literal, "request_relationships": len(requests)}
    if html_files:
        ids = sum(len(HTML_ID.findall((path / rel).read_text(encoding="utf-8", errors="replace"))) for rel in html_files)
        got = sum(1 for e in a.entities if e.type.value == "html_element" and e.metadata.get("id"))
        row["html"] = {"census_ids": ids, "elements_with_id": got, "id_recall": round(min(got, ids) / ids, 4) if ids else 1.0}
    row["graph"] = {"entry_points": len(graph.entry_points()), "nodes": len(graph)}
    return row


def peak_memory_mb(t: dict) -> float:
    tracemalloc.start()
    AnalysisService(settings()).analyze(str(target_path(t)))
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return round(peak / 1e6, 1)


def robustness(ids: list[str]) -> list[dict]:
    rows = []
    for tid in ids or list(TARGETS):
        t = TARGETS[tid]
        try:
            row = analyze_target(t)
            row["peak_python_heap_mb"] = peak_memory_mb(t)
        except Exception as exc:                        # a crash IS the result
            row = {"id": tid, "kind": t["kind"], "crashed": f"{type(exc).__name__}: {exc}"}
        rows.append(row)
        print(json.dumps({k: v for k, v in row.items() if k in ("id", "files", "entities", "relationships", "unresolved", "analyze_seconds",
                                                               "peak_python_heap_mb", "crashed")}))
    (DATA / "phase8_robustness.json").write_text(json.dumps(rows, indent=1), encoding="utf-8")
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=["clone", "robustness", "sample", "retrieval", "qa", "impact"])
    ap.add_argument("ids", nargs="*")
    args = ap.parse_args()
    if args.command == "clone":
        clone()
    elif args.command == "robustness":
        robustness(args.ids)
    else:
        import evaluate_external_ext as ext                        # noqa: F401  (sample / retrieval / qa live there)
        return ext.run(args.command, args.ids)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
