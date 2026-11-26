"""Compare retrieval methods on hand-labelled queries (backend/eval_data/retrieval_<app>.json; kept outside test_apps so they are not analysed as repo files).

    python backend/evaluate_retrieval.py [--no-dense] [--json OUT]

Methods: tfidf (lexical), dense (sentence-transformers), hybrid (lexical + dense + entity + graph),
and hybrid ablations. Metrics, averaged over queries:
    Precision@K   relevant hits in the top K / K
    Recall@K      relevant hits in the top K / all relevant entities for the query
    MRR           1 / rank of the first relevant hit (0 if none in the top 10)
    Coverage@K    share of queries with at least one relevant hit in the top K
A query is "exact" if every word of some relevant entity's name appears in it, otherwise "paraphrase".
Labels were written from the source code, not from tool output. The set is small (24 queries, 2 apps):
it supports a comparison, not a claim about large repositories.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

from repomind.config import Settings                                                  # noqa: E402
from repomind.graph import RepositoryGraph                                            # noqa: E402
from repomind.models import RelationType                                              # noqa: E402
from repomind.retrieval import SemanticIndex, build_chunks                            # noqa: E402
from repomind.retrieval.embedder import TfidfEmbedder, get_dense_embedder, tokenize   # noqa: E402
from repomind.retrieval.hybrid import DEFAULT_WEIGHTS, HybridIndex                    # noqa: E402
from repomind.services.analysis_service import AnalysisService                        # noqa: E402

KS = (1, 3, 5)


def build_indexes(analysis, graph, with_dense: bool) -> dict[str, HybridIndex]:
    chunks = [c for c in build_chunks(analysis) if c.kind == "code"]
    lexical = SemanticIndex(chunks, TfidfEmbedder())
    dense = SemanticIndex(chunks, get_dense_embedder()) if with_dense else None

    def neighbors(eid: str) -> set[str]:
        return {e["target"] if e["source"] == eid else e["source"]
                for e in graph.incoming(eid) + graph.outgoing(eid) if e["type"] != RelationType.CONTAINS.value}

    def make(mode, **w):
        return HybridIndex(chunks, lexical, dense, mode, neighbors, {**DEFAULT_WEIGHTS, **w})

    methods = {"tfidf": make("tfidf")}
    if with_dense:
        methods.update({"dense": make("dense"), "hybrid": make("hybrid"),
                        "hybrid-no-graph": make("hybrid", graph=0.0),
                        "hybrid-no-entity": make("hybrid", entity=0.0)})
    return methods


def is_exact(query: str, relevant: list[str]) -> bool:
    q = set(tokenize(query))
    return any((t := set(tokenize(r.rsplit(":", 1)[-1]))) and t <= q for r in relevant)


def score(index: HybridIndex, queries: list[dict]) -> list[dict]:
    rows = []
    for item in queries:
        rel = set(item["relevant"])
        ranked = [h.chunk.entity_id for h in index.rank(item["q"], 10)]
        row = {"q": item["q"], "exact": is_exact(item["q"], item["relevant"]), "top3": ranked[:3]}
        for k in KS:
            hit = len(set(ranked[:k]) & rel)
            row[f"P@{k}"], row[f"R@{k}"], row[f"C@{k}"] = hit / k, hit / len(rel), 1.0 if hit else 0.0
        row["MRR"] = next((1 / (i + 1) for i, e in enumerate(ranked) if e in rel), 0.0)
        rows.append(row)
    return rows


def mean(rows: list[dict], key: str) -> float:
    return sum(r[key] for r in rows) / len(rows) if rows else 0.0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-dense", action="store_true")
    ap.add_argument("--json", default=None)
    args = ap.parse_args()
    svc = AnalysisService(Settings(workspace_dir=Path(tempfile.mkdtemp()), log_level="ERROR"))
    per_method: dict[str, list[dict]] = {}
    latency: dict[str, float] = {}
    for app in sorted((ROOT / "backend" / "eval_data").glob("retrieval_*_app.json")):
        app_dir = ROOT / "test_apps" / app.stem.removeprefix("retrieval_")
        analysis = svc.analyze(str(app_dir))
        graph = RepositoryGraph.from_analysis(analysis)
        queries = json.loads(app.read_text(encoding="utf-8"))["queries"]
        valid = {e.id for e in analysis.entities}
        bad = [r for q in queries for r in q["relevant"] if r not in valid]
        assert not bad, f"{app_dir.name}: labels point at unknown entities: {bad}"
        for name, index in build_indexes(analysis, graph, not args.no_dense).items():
            t = time.perf_counter()
            per_method.setdefault(name, []).extend(score(index, queries))
            latency[name] = latency.get(name, 0.0) + (time.perf_counter() - t)
    n_q = len(next(iter(per_method.values())))
    print(f"{n_q} queries over 2 apps; {sum(r['exact'] for r in next(iter(per_method.values())))} exact, rest paraphrase\n")
    header = f"{'method':18}{'subset':11}" + "".join(f"{m:>8}" for m in ("P@1", "P@3", "R@3", "R@5", "MRR", "C@1", "C@3")) + "  ms/query"
    print(header)
    report = {}
    for name, rows in per_method.items():
        for subset, sel in (("all", rows), ("exact", [r for r in rows if r["exact"]]), ("paraphrase", [r for r in rows if not r["exact"]])):
            vals = {m: mean(sel, m) for m in ("P@1", "P@3", "R@3", "R@5", "MRR", "C@1", "C@3")}
            report.setdefault(name, {})[subset] = vals
            ms = f"{1000 * latency[name] / len(rows):8.1f}" if subset == "all" else ""
            print(f"{name:18}{subset:11}" + "".join(f"{v:8.2f}" for v in vals.values()) + f"  {ms}")
    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
