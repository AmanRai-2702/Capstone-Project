"""sample / retrieval / qa commands of evaluate_external.py (kept separate so each part stays readable)."""
from __future__ import annotations

import ast
import json
import random
import re
import statistics
import time
from collections import defaultdict
from pathlib import Path

import evaluate_external as base
from evaluate_external import DATA, TARGETS, settings, target_path

from repomind.graph import RepositoryGraph
from repomind.services.analysis_service import AnalysisService

SAMPLE_TARGETS = ["microblog", "flask-tutorial", "flask-javascript-example", "fastapi-backend", "vanilla-web-projects", "flask-lib"]
PER_TARGET = 24


# ------------------------------------------------------------------ stratified relationship sample
def _line(path: Path, loc, width: int = 150) -> str:
    try:
        lines = (path / loc.file_path).read_text(encoding="utf-8", errors="replace").splitlines()
        return lines[loc.start_line - 1].strip()[:width]
    except (OSError, IndexError, AttributeError):
        return ""


def sample(ids: list[str]) -> int:
    rng = random.Random(8)                       # fixed seed: the same sample every run
    out = []
    for tid in ids or SAMPLE_TARGETS:
        t = TARGETS[tid]
        root = target_path(t)
        a = AnalysisService(settings()).analyze(str(root))
        ents = {e.id: e for e in a.entities}
        strata = defaultdict(list)
        for r in a.relationships:
            if r.type.value == "CONTAINS":
                continue
            strata[(r.type.value, r.kind.value)].append(r)
        picked = []
        # round-robin over strata so rare types are represented; common ones fill what is left
        keys = sorted(strata, key=lambda k: len(strata[k]))
        for k in keys:
            rng.shuffle(strata[k])
        while len(picked) < PER_TARGET and any(strata.values()):
            for k in keys:
                if strata[k] and len(picked) < PER_TARGET:
                    picked.append(strata[k].pop())
        for r in picked:
            ev = r.evidence[0] if r.evidence else None
            tgt = ents.get(r.target_id)
            src = ents.get(r.source_id)
            out.append({
                "target": tid, "type": r.type.value, "kind": r.kind.value, "confidence": r.confidence,
                "source": r.source_id.split(":", 2)[-1], "dest": r.target_id.split(":", 2)[-1],
                "evidence": (ev.snippet.replace("\n", " ")[:170] if ev else ""),
                "where": f"{ev.location.file_path}:{ev.location.start_line}" if ev and ev.location else "",
                "source_def": _line(root, src.location) if src and src.location else "",
                "dest_def": _line(root, tgt.location) if tgt and tgt.location else "",
                "dest_file": tgt.location.file_path if tgt and tgt.location else "",
                "total_in_stratum": len(strata[(r.type.value, r.kind.value)]) + 1,
            })
        print(f"{tid}: sampled {len(picked)} of {a.relationship_count} relationships")
    (DATA / "phase8_precision_sample.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
    for i, x in enumerate(out):
        print(f"[{i:03}] {x['target'][:10]:10} {x['type']:10} {x['kind']:9} {x['source'][:38]:38} -> {x['dest'][:42]:42} | {x['where']} | {x['evidence'][:90]}")
    return 0


# ------------------------------------------------------------------ retrieval: docstring-as-query on real code
def _docstrings(root: Path, py_files: list[str]) -> dict[tuple[str, str], tuple[str, tuple[int, int]]]:
    """(file, qualified name) -> (first sentence of docstring, line range of the docstring) for def/class with one."""
    out = {}
    for rel in py_files:
        try:
            tree = ast.parse((root / rel).read_text(encoding="utf-8", errors="replace"))
        except (SyntaxError, ValueError):
            continue

        def visit(node, prefix=""):
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    q = f"{prefix}{child.name}"
                    doc = ast.get_docstring(child, clean=True)
                    if doc and child.body and isinstance(child.body[0], ast.Expr):
                        first = re.split(r"(?<=[.!?])\s|\n\n", doc.strip())[0].replace("\n", " ")
                        out[(rel, q)] = (first, (child.body[0].lineno, child.body[0].end_lineno))
                    visit(child, q + "." if isinstance(child, ast.ClassDef) else prefix)
                else:
                    visit(child, prefix)
        visit(tree)
    return out


def retrieval(ids: list[str]) -> int:
    from repomind.models import RelationType
    from repomind.retrieval import SemanticIndex
    from repomind.retrieval.chunker import Chunk
    from repomind.retrieval.embedder import TfidfEmbedder, get_dense_embedder, tokenize
    from repomind.retrieval.hybrid import HybridIndex

    tid = ids[0] if ids else "flask-lib"
    t = TARGETS[tid]
    root = target_path(t)
    a = AnalysisService(settings()).analyze(str(root))
    graph = RepositoryGraph.from_analysis(a)
    py_files = [e.qualified_name for e in a.entities if e.type.value == "file" and e.language.value == "python"
                and e.metadata.get("category") == "source" and "tests/" not in e.qualified_name]
    docs = _docstrings(root, py_files)

    # chunks WITHOUT their docstring: otherwise the query text sits inside the chunk and lexical search is trivial
    chunks, qa = [], []
    for e in a.entities:
        if e.type.value not in ("function", "method", "class") or not e.location or e.location.file_path not in py_files:
            continue
        loc = e.location
        lines = (root / loc.file_path).read_text(encoding="utf-8", errors="replace").splitlines()
        window = list(range(loc.start_line - 1, min(loc.end_line, loc.start_line - 1 + 40)))
        doc = docs.get((loc.file_path, e.qualified_name))
        if doc:
            window = [i for i in window if not (doc[1][0] - 1 <= i <= doc[1][1] - 1)]
        body = "\n".join(lines[i] for i in window)
        chunks.append(Chunk(entity_id=e.id, text=f"{e.qualified_name} {e.type.value} python\n{body}", citation=None, etype=e.type.value))
        if doc and len(doc[0].split()) >= 6:
            name_tokens = set(tokenize(e.name))
            leaks = bool(name_tokens & set(tokenize(doc[0])))
            qa.append({"q": doc[0], "id": e.id, "leaks_name": leaks})
    rng = random.Random(8)
    rng.shuffle(qa)
    qa = qa[:200]
    print(f"{tid}: {len(chunks)} chunks, {len(qa)} docstring queries ({sum(not x['leaks_name'] for x in qa)} without any word of the name)")

    lexical = SemanticIndex(chunks, TfidfEmbedder())
    t0 = time.perf_counter()
    dense = SemanticIndex(chunks, get_dense_embedder(None, settings().workspace_dir / "emb"))
    print(f"dense index (model load + encoding {len(chunks)} chunks): {time.perf_counter() - t0:.1f}s")

    def neighbors(eid):
        return {e["target"] if e["source"] == eid else e["source"]
                for e in graph.incoming(eid) + graph.outgoing(eid) if e["type"] != RelationType.CONTAINS.value}

    modes = {"tfidf": HybridIndex(chunks, lexical, None, "tfidf"), "dense": HybridIndex(chunks, lexical, dense, "dense", neighbors),
             "hybrid": HybridIndex(chunks, lexical, dense, "hybrid", neighbors)}
    result = {"target": tid, "chunks": len(chunks), "queries": len(qa)}
    for subset, items in (("all", qa), ("no name leakage", [x for x in qa if not x["leaks_name"]])):
        for name, idx in modes.items():
            rr, hits1, hits5, hits10, ms = [], 0, 0, 0, []
            for x in items:
                t1 = time.perf_counter()
                ranked = [h.chunk.entity_id for h in idx.rank(x["q"], 10)]
                ms.append((time.perf_counter() - t1) * 1000)
                pos = ranked.index(x["id"]) + 1 if x["id"] in ranked else 0
                rr.append(1 / pos if pos else 0.0)
                hits1 += pos == 1
                hits5 += 0 < pos <= 5
                hits10 += 0 < pos <= 10
            n = len(items)
            result[f"{subset}/{name}"] = {"n": n, "MRR": round(sum(rr) / n, 3), "R@1": round(hits1 / n, 3), "R@5": round(hits5 / n, 3),
                                          "R@10": round(hits10 / n, 3), "ms_per_query": round(statistics.mean(ms), 2)}
    (DATA / "phase8_retrieval.json").write_text(json.dumps(result, indent=1), encoding="utf-8")
    for k, v in result.items():
        if "/" in k:
            print(f"{k:28} {v}")
    return 0


def run(command: str, ids: list[str]) -> int:
    if command == "sample":
        return sample(ids)
    if command == "retrieval":
        return retrieval(ids)
    if command == "impact":
        return impact_external(ids)
    if command == "qa":
        import evaluate_external_qa as qa
        return qa.run(ids)
    return 1


# ------------------------------------------------------------------ impact: direct callers vs an independent ast census
def _callers_census(root: Path, py_files: list[str]):
    """{function name: set of (file, qualified enclosing function)} for every Call to a name; plus the module-level
    function definitions (file, name). Independent of RepoMind's own call resolution (plain ast)."""
    callers = defaultdict(set)
    defs = defaultdict(set)                     # name -> {(file, kind)}
    for rel in py_files:
        try:
            tree = ast.parse((root / rel).read_text(encoding="utf-8", errors="replace"))
        except (SyntaxError, ValueError):
            continue
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                defs[node.name].add(rel)

        def walk(node, scope):
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    walk(child, scope + [child.name])
                elif isinstance(child, ast.ClassDef):
                    walk(child, scope + [child.name])
                else:
                    if isinstance(child, ast.Call):
                        f = child.func
                        name = f.id if isinstance(f, ast.Name) else (f.attr if isinstance(f, ast.Attribute) else None)
                        if name and scope:
                            callers[name].add((rel, ".".join(scope)))
                    walk(child, scope)
        walk(tree, [])
    return callers, defs


def impact_external(ids: list[str]) -> int:
    from repomind.services.impact_service import ImpactService
    rng = random.Random(8)
    rows = []
    for tid in ids or ["flask-tutorial", "microblog", "fastapi-backend", "flask-lib"]:
        t = TARGETS[tid]
        root = target_path(t)
        a = AnalysisService(settings()).analyze(str(root))
        graph = RepositoryGraph.from_analysis(a)
        svc = ImpactService(graph, a.unresolved)
        py = [e.qualified_name for e in a.entities if e.type.value == "file" and e.language.value == "python" and e.metadata.get("category") == "source"]
        callers, defs = _callers_census(root, py)
        cands = sorted(n for n, files in defs.items() if len(files) == 1 and not n.startswith("_") and len({c for c in callers.get(n, ())}) >= 2)
        # a name defined ONCE at module level and never as a method/nested function elsewhere keeps the census unambiguous
        meth = {e.name for e in a.entities if e.type.value == "method"}
        cands = [n for n in cands if n not in meth]
        rng.shuffle(cands)
        for name in cands[:6]:
            (dfile,) = defs[name]
            target = f"python:{dfile}:{name}"
            if not graph.has_node(target):
                continue
            # Compare (file, innermost function name) on both sides. Callers only: a DATA_FLOW / READS_STATE dependent is
            # real impact but not a call site. Recursion and same-named calls inside the target itself are not callers.
            exp_keys = {(f, q.split(".")[-1]) for f, q in callers[name] if q.split(".")[-1] != name}
            report = svc.analyze_impact(target)
            direct_c = [c for c in report.direct if c.type in ("function", "method") and c.chain and c.chain[0].relation == "CALLS"]
            direct_keys = {(graph.get_entity(c.entity_id).location.file_path, graph.get_entity(c.entity_id).name) for c in direct_c
                           if graph.get_entity(c.entity_id) and graph.get_entity(c.entity_id).location}
            direct = direct_keys
            exp_ids = exp_keys
            hit = exp_ids & direct
            rows.append({"target": tid, "function": name, "expected_callers": len(exp_ids), "reported_direct": len(direct), "correct": len(hit),
                         "precision": round(len(hit) / len(direct), 3) if direct else 1.0, "recall": round(len(hit) / len(exp_ids), 3),
                         "missed": sorted(f"{f.split('/')[-1]}:{n}" for f, n in exp_ids - direct)[:4], "extra": sorted(f"{f.split('/')[-1]}:{n}" for f, n in direct - exp_ids)[:4],
                         "reported_by_kind": {k: sum(1 for c in direct_c if c.chain[0].kind == k) for k in ("detected", "inferred", "heuristic")}})
    tp = sum(r["correct"] for r in rows); rep = sum(r["reported_direct"] for r in rows); exp = sum(r["expected_callers"] for r in rows)
    summary = {"functions": len(rows), "micro_precision": round(tp / rep, 3) if rep else None, "micro_recall": round(tp / exp, 3) if exp else None}
    (DATA / "phase8_impact_external.json").write_text(json.dumps({"summary": summary, "rows": rows}, indent=1), encoding="utf-8")
    for r in rows:
        print(f"{r['target'][:16]:16} {r['function'][:34]:34} exp {r['expected_callers']:2} rep {r['reported_direct']:2} P {r['precision']:.2f} R {r['recall']:.2f}  missed {r['missed'][:2]}  extra {r['extra'][:2]}")
    print(summary)
    return 0
