"""Turn a RepositoryAnalysis into retrievable chunks: one per code entity, not per arbitrary
N-line window, so every hit maps straight back to a graph node with a file:line citation."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from repomind.models import EntityType, RepositoryAnalysis

DOC_CHUNK_LINES = 30          # lines per documentation chunk
MAX_DOC_CHUNKS_PER_FILE = 12  # bounds the index size for huge docs
MAX_DOC_FILE_BYTES = 200_000
# Never index files that usually hold secrets, even if they look like config.
_SECRET_FILE = re.compile(r"(^|/)(\.env(\.(?!example|sample|template).*)?|.*\.(pem|key|p12)|id_rsa.*|.*secrets?.*)$", re.I)
_LOCKFILES = {"package-lock.json", "yarn.lock", "poetry.lock", "pipfile.lock", "pnpm-lock.yaml"}

CHUNKED_TYPES = {
    EntityType.CLASS, EntityType.FUNCTION, EntityType.METHOD, EntityType.HTML_ELEMENT,
    EntityType.CSS_SELECTOR, EntityType.API_ENDPOINT,
}


@dataclass(frozen=True)
class Chunk:
    entity_id: str
    text: str                 # what gets embedded: names + type + source lines
    citation: str | None      # "path:start-end"
    kind: str = "code"        # "code" (a graph entity) or "doc" (README / markdown / config text)
    etype: str = ""           # entity type value ("function", "html_element", ...); "" for docs


def build_chunks(analysis: RepositoryAnalysis, max_lines: int = 40, include_docs: bool = True) -> list[Chunk]:
    root = Path(analysis.root_path)
    cache: dict[str, list[str]] = {}

    def lines_of(path: str) -> list[str]:
        if path not in cache:
            try:
                cache[path] = (root / path).read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                cache[path] = []
        return cache[path]

    chunks = []
    for e in analysis.entities:
        if e.type not in CHUNKED_TYPES:
            continue
        header = f"{e.qualified_name} {e.type.value.replace('_', ' ')} {e.language.value}"
        body, citation = "", None
        if e.location:
            loc = e.location
            body = "\n".join(lines_of(loc.file_path)[loc.start_line - 1: min(loc.end_line, loc.start_line - 1 + max_lines)])
            citation = f"{loc.file_path}:{loc.start_line}-{loc.end_line}"
        chunks.append(Chunk(entity_id=e.id, text=f"{header}\n{body}", citation=citation, etype=e.type.value))
    if include_docs:
        chunks.extend(build_doc_chunks(analysis))
    return chunks


def is_indexable_doc(rel_path: str, category: str) -> bool:
    name = rel_path.rsplit("/", 1)[-1].lower()
    if category not in ("doc", "config") or name in _LOCKFILES or _SECRET_FILE.search(rel_path):
        return False
    return True


def build_doc_chunks(analysis: RepositoryAnalysis) -> list[Chunk]:
    """README / markdown / requirements / package.json text, split into ~30-line windows that keep
    their real line numbers (so they can be cited like code)."""
    root = Path(analysis.root_path)
    chunks: list[Chunk] = []
    for e in analysis.entities:
        if e.type != EntityType.FILE:
            continue
        category = str(e.metadata.get("category", ""))
        if not is_indexable_doc(e.qualified_name, category):
            continue
        if (e.metadata.get("size_bytes") or 0) > MAX_DOC_FILE_BYTES:
            continue
        try:
            lines = (root / e.qualified_name).read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for n, start in enumerate(range(0, len(lines), DOC_CHUNK_LINES)):
            if n >= MAX_DOC_CHUNKS_PER_FILE:
                break
            window = lines[start:start + DOC_CHUNK_LINES]
            if not any(w.strip() for w in window):
                continue
            end = start + len(window)
            chunks.append(Chunk(entity_id=e.id, kind="doc", citation=f"{e.qualified_name}:{start + 1}-{end}",
                                text=f"{e.qualified_name} {category} documentation\n" + "\n".join(window)))
    return chunks
