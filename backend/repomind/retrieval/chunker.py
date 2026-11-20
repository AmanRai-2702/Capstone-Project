"""Turn a RepositoryAnalysis into retrievable chunks: one per code entity, not per arbitrary
N-line window, so every hit maps straight back to a graph node with a file:line citation."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from repomind.models import EntityType, RepositoryAnalysis

CHUNKED_TYPES = {
    EntityType.CLASS, EntityType.FUNCTION, EntityType.METHOD, EntityType.HTML_ELEMENT,
    EntityType.CSS_SELECTOR, EntityType.API_ENDPOINT,
}


@dataclass(frozen=True)
class Chunk:
    entity_id: str
    text: str                 # what gets embedded: names + type + source lines
    citation: str | None      # "path:start-end"


def build_chunks(analysis: RepositoryAnalysis, max_lines: int = 40) -> list[Chunk]:
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
        chunks.append(Chunk(entity_id=e.id, text=f"{header}\n{body}", citation=citation))
    return chunks
