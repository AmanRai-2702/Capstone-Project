"""Persist analyses as JSON files so repositories survive a server restart.

Deliberately simple: one `<repo_id>.json` per repository (the Pydantic model, verbatim). The graph,
the vector index and the Q&A service are cheap to rebuild from it. A database would add setup for
no benefit at this scale; swap this class if that ever changes.
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path

from repomind.models import RepositoryAnalysis

log = logging.getLogger(__name__)
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


class AnalysisStore:
    def __init__(self, directory: Path):
        self.directory = Path(directory)

    def _path(self, repo_id: str) -> Path:
        # repo ids come from folder names / URLs: never let one escape the store directory
        if not _SAFE_ID.match(repo_id) or ".." in repo_id:
            raise ValueError(f"Unsafe repository id: {repo_id!r}")
        return self.directory / f"{repo_id}.json"

    def save(self, analysis: RepositoryAnalysis) -> Path:
        path = self._path(analysis.repo_id)
        self.directory.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(analysis.model_dump_json(), encoding="utf-8")
        os.replace(tmp, path)                      # atomic: a crash never leaves a half-written file
        return path

    def load(self, repo_id: str) -> RepositoryAnalysis | None:
        path = self._path(repo_id)
        if not path.is_file():
            return None
        try:
            return RepositoryAnalysis.model_validate_json(path.read_text(encoding="utf-8"))
        except Exception as exc:                   # corrupt or from an incompatible version
            log.warning("Ignoring unreadable stored analysis %s: %s", path.name, exc)
            return None

    def ids(self) -> list[str]:
        if not self.directory.is_dir():
            return []
        return sorted(p.stem for p in self.directory.glob("*.json") if _SAFE_ID.match(p.stem))

    def delete(self, repo_id: str) -> bool:
        path = self._path(repo_id)
        if path.is_file():
            path.unlink()
            return True
        return False
