"""Performance benchmark: how does RepoMind scale with repository size?

    python backend/benchmark_performance.py            # sizes 50, 200, 800 modules + RepoMind itself
    python backend/benchmark_performance.py 100 500    # custom sizes

For each synthetic repository (N Python modules chained by imports/calls, N/5 JS files, HTML pages and
a Flask-style app) it times each pipeline stage and reports peak memory (tracemalloc, Python heap only).
It also analyses RepoMind's own source ("dogfooding") as a real, unseen codebase.
ingest_cold_ms is the first read of the files (on Windows, freshly written files are scanned by the OS, which
can be 10x slower); ingest_ms is the warm figure and reflects RepoMind's own cost.
Results are written to performance_report.json. Numbers depend on the machine; compare ratios, not absolutes.
"""
import json
import random
import sys
import tempfile
import time
import tracemalloc
from pathlib import Path

from repomind.config import Settings
from repomind.graph import RepositoryGraph
from repomind.qa import QAService
from repomind.services.analysis_service import AnalysisService
from repomind.services.impact_service import ImpactService
from repomind.services.repository_service import RepositoryService

ROOT = Path(__file__).resolve().parents[1]


def make_repo(root: Path, n: int) -> None:
    (root / "pkg").mkdir(parents=True)
    for i in range(n):
        dep = f"from pkg.mod{i - 1} import Svc{i - 1}\n\n" if i else ""
        init = f"        self.dep = Svc{i - 1}()\n" if i else "        self.dep = None\n"
        (root / "pkg" / f"mod{i}.py").write_text(
            f"{dep}class Svc{i}:\n    def __init__(self):\n{init}\n"
            f"    def run(self, x):\n        return self.helper(x)\n\n"
            f"    def helper(self, x):\n        return self.dep.run(x) if self.dep else x\n", encoding="utf-8")
    routes = "\n".join(f'@app.get("/api/r{i}")\ndef r{i}():\n    return Svc{i}().run(1)\n' for i in range(0, n, 5))
    imports = "\n".join(f"from pkg.mod{i} import Svc{i}" for i in range(0, n, 5))
    (root / "app.py").write_text(f"from fastapi import FastAPI\n{imports}\napp = FastAPI()\n{routes}", encoding="utf-8")
    (root / "web").mkdir()
    for j in range(max(n // 5, 1)):
        (root / "web" / f"p{j}.html").write_text(
            f'<form id="f{j}"><button id="b{j}" class="btn"></button></form><script src="p{j}.js"></script>', encoding="utf-8")
        (root / "web" / f"p{j}.js").write_text(
            f'async function go{j}() {{ await fetch("/api/r{j * 5}"); }}\n'
            f'document.getElementById("f{j}").addEventListener("submit", go{j});\n', encoding="utf-8")


def timed(fn):
    t = time.perf_counter()
    out = fn()
    return out, (time.perf_counter() - t) * 1000


def bench(label: str, location: str, workspace: Path) -> dict:
    settings = Settings(workspace_dir=workspace, log_level="ERROR")
    _, t_cold = timed(lambda: RepositoryService(settings).ingest(location))      # first touch of the files
    inventory, t_ingest = timed(lambda: RepositoryService(settings).ingest(location))   # warm: RepoMind's own cost
    tracemalloc.start()
    analysis, t_analyze = timed(lambda: AnalysisService(settings).analyze_inventory(inventory))
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    graph, t_graph = timed(lambda: RepositoryGraph.from_analysis(analysis))
    impact = ImpactService(graph)
    code = [e.id for e in analysis.entities if e.type.value in ("function", "method")]
    random.seed(1)
    sample = random.sample(code, min(20, len(code))) if code else []
    _, t_impact = timed(lambda: [impact.analyze_impact(i) for i in sample])
    qa, t_qa_build = timed(lambda: QAService(analysis, graph))
    _, t_ask = timed(lambda: [qa.ask(q) for q in ("Explain the architecture", "Where is run implemented?",
                                                  "What breaks if I change helper?")])
    row = {
        "label": label, "files": len(inventory.files), "entities": analysis.entity_count,
        "relationships": analysis.relationship_count, "ingest_cold_ms": round(t_cold, 1),
        "ingest_ms": round(t_ingest, 1),
        "analyze_ms": round(t_analyze, 1), "graph_build_ms": round(t_graph, 1),
        "impact_query_ms_avg": round(t_impact / max(len(sample), 1), 2),
        "qa_index_build_ms": round(t_qa_build, 1), "qa_answer_ms_avg": round(t_ask / 3, 1),
        "peak_memory_mb": round(peak / 1_048_576, 1),
    }
    return row


def main(argv: list[str]) -> int:
    sizes = [int(a) for a in argv] or [50, 200, 800]
    rows = []
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        for n in sizes:
            repo = tmp / f"synthetic_{n}"
            make_repo(repo, n)
            rows.append(bench(f"synthetic ({n} modules)", str(repo), tmp / "ws"))
        rows.append(bench("RepoMind itself (backend/repomind)", str(ROOT / "backend" / "repomind"), tmp / "ws"))

    cols = ["label", "files", "entities", "relationships", "ingest_cold_ms", "ingest_ms", "analyze_ms", "graph_build_ms",
            "impact_query_ms_avg", "qa_index_build_ms", "qa_answer_ms_avg", "peak_memory_mb"]
    widths = [max(len(c), *(len(str(r[c])) for r in rows)) for c in cols]
    print("  ".join(c.ljust(w) for c, w in zip(cols, widths)))
    for r in rows:
        print("  ".join(str(r[c]).ljust(w) for c, w in zip(cols, widths)))
    (ROOT / "performance_report.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")
    print("\nSaved performance_report.json")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
