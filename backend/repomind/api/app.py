"""REST API (Stage 7). Thin layer: all logic lives in the services/graph/qa packages.

Entity ids contain '/' and ':' so they are always passed as *query parameters* or JSON bodies.
Run:  python -m repomind.cli serve        (or: uvicorn repomind.api.app:app, with PYTHONPATH=backend)

Endpoints (all under /api):
  POST   /repositories[?background=true]  {location, exclude[]}  analyse a folder / GitHub URL / .zip path
  POST   /repositories/upload?name=x.zip  raw zip bytes          analyse an uploaded archive
  GET    /jobs/{job_id}                                           status of a background analysis
  GET    /repositories | /repositories/{id}        DELETE /repositories/{id}
  GET    /repositories/{id}/graph | entities | entity | relationships | flows | unresolved | source
  GET    /repositories/{id}/trace?source=&target=      POST same with a JSON body
  GET    /repositories/{id}/impact?target=             POST {target | diff, max_depth}
  POST   /repositories/{id}/ask  (alias: /chat)        {question}
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import networkx as nx
from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from repomind.config import Settings
from repomind.graph import RepositoryGraph
from repomind.ingestion.errors import IngestionError
from repomind.models import RepositoryAnalysis
from repomind.qa import QAService, get_provider
from repomind.services.analysis_service import AnalysisService
from repomind.services.impact_service import ImpactError, ImpactService
from repomind.storage import AnalysisStore

log = logging.getLogger("repomind.api")
STATIC_DIR = Path(__file__).parent / "static"


@dataclass
class RepoSession:
    analysis: RepositoryAnalysis
    graph: RepositoryGraph
    impact: ImpactService
    qa: QAService


class AnalyzeRequest(BaseModel):
    location: str = Field(min_length=1, max_length=2048,
                          description="local folder, .zip path, or https://github.com/<owner>/<repo>")
    exclude: list[str] = Field(default_factory=list, max_length=50,
                               description="extra directory names / glob patterns to skip")


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)


class TraceRequest(BaseModel):
    source: str = Field(min_length=1, max_length=512)
    target: str = Field(min_length=1, max_length=512)
    max_depth: int = Field(default=10, ge=1, le=50)


class ImpactRequest(BaseModel):
    target: str | None = Field(default=None, max_length=512, description="entity id / name")
    diff: str | None = Field(default=None, max_length=2_000_000, description="unified diff text")
    max_depth: int = Field(default=10, ge=1, le=50)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    allow_local = os.getenv("REPOMIND_ALLOW_LOCAL_PATHS", "1") == "1"
    store = AnalysisStore(settings.store_dir)
    sessions: dict[str, RepoSession] = {}
    jobs: dict[str, dict] = {}
    lock = threading.Lock()
    api = FastAPI(title="RepoMind", version="0.2.0",
                  description="Multi-language repository intelligence and change-impact analysis")

    # ------------------------------------------------------------------ helpers
    def build_session(analysis: RepositoryAnalysis) -> RepoSession:
        graph = RepositoryGraph.from_analysis(analysis)
        return RepoSession(analysis, graph, ImpactService(graph), QAService(analysis, graph, get_provider()))

    def session(repo_id: str) -> RepoSession:
        with lock:
            if repo_id in sessions:
                return sessions[repo_id]
        try:
            analysis = store.load(repo_id)               # survives restarts; rebuilt lazily
        except ValueError:
            analysis = None
        if analysis is None:
            raise HTTPException(404, f"Unknown repository '{repo_id}'. POST /api/repositories first.")
        built = build_session(analysis)
        with lock:
            sessions[repo_id] = built
        return built

    def known_ids() -> list[str]:
        with lock:
            return sorted(set(sessions) | set(store.ids()))

    def run_analysis(location: str, exclude: list[str]) -> str:
        analysis = AnalysisService(settings).analyze(location, exclude=exclude)
        store.save(analysis)
        built = build_session(analysis)
        with lock:
            sessions[analysis.repo_id] = built
        return analysis.repo_id

    def entity_or_404(s: RepoSession, query: str):
        matches = s.graph.resolve(query)
        if not matches:
            raise HTTPException(404, f"No entity matches '{query}'")
        return matches[0]

    def summary(repo_id: str) -> dict:
        a = session(repo_id).analysis
        return {
            "repo_id": a.repo_id, "name": a.name, "origin": a.origin,
            "entity_count": a.entity_count, "relationship_count": a.relationship_count,
            "entity_counts_by_type": a.entity_counts_by_type,
            "relationship_counts_by_type": a.relationship_counts_by_type,
            "relationship_counts_by_kind": a.relationship_counts_by_kind,
            "unresolved_counts_by_reason": a.unresolved_counts_by_reason,
            "unresolved_external_references": len(a.stats.get("unresolved_references", {})),
            "analysis_errors": a.stats.get("analysis_errors", []),
            "skipped_files": a.stats.get("skipped_files", {}),
        }

    @api.middleware("http")
    async def log_requests(request: Request, call_next):
        start = time.perf_counter()
        response = await call_next(request)
        log.info("%s %s -> %d (%.0f ms)", request.method, request.url.path, response.status_code,
                 (time.perf_counter() - start) * 1000)
        return response

    # ------------------------------------------------------------------ repositories
    @api.post("/api/repositories")
    def analyze_repository(req: AnalyzeRequest, background: bool = False,
                           tasks: BackgroundTasks = None) -> dict:
        if not req.location.startswith(("http://", "https://")) and not allow_local:
            raise HTTPException(403, "Local paths are disabled on this server.")
        if background:
            job_id = uuid.uuid4().hex[:12]
            jobs[job_id] = {"job_id": job_id, "status": "running", "repo_id": None, "error": None}

            def work() -> None:
                try:
                    jobs[job_id].update(repo_id=run_analysis(req.location, req.exclude), status="done")
                except Exception as exc:        # reported through the job, never raised into the server
                    log.exception("Background analysis failed")
                    jobs[job_id].update(status="error", error=str(exc))

            tasks.add_task(work)
            return jobs[job_id]
        try:
            return summary(run_analysis(req.location, req.exclude))
        except IngestionError as exc:
            raise HTTPException(400, str(exc)) from exc

    @api.get("/api/jobs/{job_id}")
    def get_job(job_id: str) -> dict:
        if job_id not in jobs:
            raise HTTPException(404, "Unknown job.")
        return jobs[job_id]

    @api.post("/api/repositories/upload")
    async def upload_archive(request: Request, name: str = "upload.zip") -> dict:
        """Body = the raw bytes of a .zip file (no multipart dependency needed)."""
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", Path(name).name)
        if not safe.lower().endswith(".zip"):
            raise HTTPException(400, "Only .zip archives are accepted.")
        limit = settings.max_zip_mb * 1024 * 1024
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > limit:
            raise HTTPException(413, f"Upload exceeds the {settings.max_zip_mb} MB limit.")
        uploads = settings.workspace_dir / "uploads"
        uploads.mkdir(parents=True, exist_ok=True)
        path = uploads / f"{uuid.uuid4().hex[:8]}_{safe}"
        size = 0
        try:
            with open(path, "wb") as out:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > limit:                       # enforce even when Content-Length lies
                        raise HTTPException(413, f"Upload exceeds the {settings.max_zip_mb} MB limit.")
                    out.write(chunk)
            repo_id = await run_in_threadpool(run_analysis, str(path), [])
        except IngestionError as exc:
            raise HTTPException(400, str(exc)) from exc
        finally:
            path.unlink(missing_ok=True)                  # the extracted copy is what we keep
        return summary(repo_id)

    @api.get("/api/repositories")
    def list_repositories() -> list[dict]:
        return [summary(r) for r in known_ids()]

    @api.get("/api/repositories/{repo_id}")
    def get_repository(repo_id: str) -> dict:
        return summary(repo_id)

    @api.delete("/api/repositories/{repo_id}")
    def delete_repository(repo_id: str) -> dict:
        session(repo_id)                                  # 404 if unknown
        with lock:
            sessions.pop(repo_id, None)
        store.delete(repo_id)
        return {"deleted": repo_id}

    # ------------------------------------------------------------------ graph
    @api.get("/api/repositories/{repo_id}/graph")
    def get_graph(repo_id: str, include_files: bool = True) -> dict:
        s = session(repo_id)
        keep = {e.id for e in s.analysis.entities if include_files or e.type.value != "file"}
        return s.graph.subgraph_json(keep)

    @api.get("/api/repositories/{repo_id}/entities")
    def search_entities(repo_id: str, q: str = Query("", description="name, qualified name or id fragment"),
                        type: str | None = None, language: str | None = None,
                        limit: int = Query(50, ge=1, le=1000)) -> list[dict]:
        s = session(repo_id)
        found = s.graph.resolve(q) if q else sorted(s.analysis.entities, key=lambda e: e.id)
        found = [e for e in found if (not type or e.type.value == type) and (not language or e.language.value == language)]
        return [{"id": e.id, "name": e.qualified_name or e.name, "type": e.type.value,
                 "language": e.language.value, "location": str(e.location) if e.location else None}
                for e in found[:limit]]

    @api.get("/api/repositories/{repo_id}/entity")
    def get_entity(repo_id: str, id: str) -> dict:
        s = session(repo_id)
        e = entity_or_404(s, id)

        def edges(items):
            return [{"source": x["source"], "target": x["target"], "type": x["type"],
                     "kind": x["kind"], "confidence": x["confidence"]} for x in items]
        return {"entity": e.model_dump(mode="json"),
                "outgoing": edges(s.graph.outgoing(e.id)), "incoming": edges(s.graph.incoming(e.id))}

    @api.get("/api/repositories/{repo_id}/relationships")
    def get_relationships(repo_id: str, type: str | None = None, kind: str | None = None,
                          source: str | None = None, target: str | None = None,
                          limit: int = Query(200, ge=1, le=5000)) -> dict:
        rels = session(repo_id).analysis.relationships
        out = [r for r in rels
               if (not type or r.type.value == type.upper()) and (not kind or r.kind.value == kind.lower())
               and (not source or source in r.source_id) and (not target or target in r.target_id)]
        return {"total": len(out), "relationships": [r.model_dump(mode="json") for r in out[:limit]]}

    @api.get("/api/repositories/{repo_id}/unresolved")
    def get_unresolved(repo_id: str) -> dict:
        """What RepoMind could NOT connect (external packages, unmatched requests...), with reasons."""
        a = session(repo_id).analysis
        return {"counts": a.unresolved_counts_by_reason, "items": [u.model_dump(mode="json") for u in a.unresolved]}

    @api.get("/api/repositories/{repo_id}/flows")
    def get_flows(repo_id: str) -> list[dict]:
        """For every UI/API entry point, the execution paths down to its sinks."""
        s = session(repo_id)
        flow = s.graph.flow_view()
        out = []
        for entry in s.graph.entry_points():
            sinks = sorted(n for n in nx.descendants(flow, entry) if flow.out_degree(n) == 0)
            paths = []
            for sink in sinks:
                paths += s.graph.find_paths(entry, sink, limit=3)
            out.append({"entry": entry, "paths": paths})
        return out

    def do_trace(repo_id: str, source: str, target: str, max_depth: int) -> dict:
        s = session(repo_id)
        a, b = entity_or_404(s, source), entity_or_404(s, target)
        paths = s.graph.find_paths(a.id, b.id, max_depth=max_depth)
        return {"source": a.id, "target": b.id,
                "paths": [{"nodes": p, "hops": s.graph.describe_path(p)} for p in paths]}

    @api.get("/api/repositories/{repo_id}/trace")
    def trace_get(repo_id: str, source: str, target: str, max_depth: int = Query(10, ge=1, le=50)) -> dict:
        return do_trace(repo_id, source, target, max_depth)

    @api.post("/api/repositories/{repo_id}/trace")
    def trace_post(repo_id: str, req: TraceRequest) -> dict:
        return do_trace(repo_id, req.source, req.target, req.max_depth)

    # ------------------------------------------------------------------ impact / Q&A / source
    @api.get("/api/repositories/{repo_id}/impact")
    def impact_get(repo_id: str, target: str, max_depth: int = Query(10, ge=1, le=50)) -> dict:
        try:
            return session(repo_id).impact.analyze_impact(target, max_depth=max_depth).model_dump(mode="json")
        except ImpactError as exc:
            raise HTTPException(404, str(exc)) from exc

    @api.post("/api/repositories/{repo_id}/impact")
    def impact_post(repo_id: str, req: ImpactRequest) -> dict:
        if bool(req.target) == bool(req.diff):
            raise HTTPException(422, "Provide exactly one of 'target' or 'diff'.")
        s = session(repo_id)
        try:
            report = (s.impact.analyze_impact(req.target, req.max_depth) if req.target
                      else s.impact.analyze_diff(req.diff, req.max_depth))
        except ImpactError as exc:
            raise HTTPException(404, str(exc)) from exc
        return report.model_dump(mode="json")

    def do_ask(repo_id: str, req: AskRequest) -> dict:
        if not req.question.strip():
            raise HTTPException(422, "Question must not be empty.")
        return session(repo_id).qa.ask(req.question).model_dump(mode="json")

    @api.post("/api/repositories/{repo_id}/ask")
    def ask(repo_id: str, req: AskRequest) -> dict:
        return do_ask(repo_id, req)

    @api.post("/api/repositories/{repo_id}/chat")
    def chat(repo_id: str, req: AskRequest) -> dict:
        return do_ask(repo_id, req)

    @api.get("/api/repositories/{repo_id}/source")
    def source(repo_id: str, file: str, start: int = Query(1, ge=1), end: int | None = None) -> dict:
        root = Path(session(repo_id).analysis.root_path).resolve()
        path = (root / file).resolve()
        if not path.is_relative_to(root) or not path.is_file():   # blocks ../ traversal
            raise HTTPException(404, "File not found in repository.")
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        end = end or len(lines)
        return {"file": file, "start": start, "end": min(end, len(lines)), "lines": lines[start - 1:end]}

    # ------------------------------------------------------------------ frontend
    @api.get("/", include_in_schema=False)
    def index():
        page = STATIC_DIR / "index.html"
        if page.exists():
            return FileResponse(page)
        return {"service": "RepoMind", "docs": "/docs"}

    if STATIC_DIR.exists():
        api.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return api


app = create_app()
