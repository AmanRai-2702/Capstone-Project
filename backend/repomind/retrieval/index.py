"""In-memory vector index with an inverted index for fast sparse cosine search.

Chosen over an external vector database on purpose: a repository has thousands of entities,
not millions, so this is instant, has zero setup and is easy to explain. The `Embedder`
interface is the seam where a dense model or ChromaDB could be plugged in later.
"""
from __future__ import annotations

from collections import defaultdict

from repomind.models import RepositoryAnalysis

from .chunker import Chunk, build_chunks
from .embedder import Embedder, Vector, get_embedder


class SemanticIndex:
    def __init__(self, chunks: list[Chunk], embedder: Embedder | None = None):
        self.chunks = chunks
        self.embedder = embedder or get_embedder()
        self.embedder.fit([c.text for c in chunks])
        self._postings: dict[object, list[tuple[int, float]]] = defaultdict(list)
        texts = [c.text for c in chunks]
        vectors = self.embedder.embed_many(texts) if hasattr(self.embedder, "embed_many") else [self.embedder.embed(t) for t in texts]
        for i, vec in enumerate(vectors):
            for term, weight in vec.items():
                self._postings[term].append((i, weight))

    @classmethod
    def from_analysis(cls, analysis: RepositoryAnalysis, embedder: Embedder | None = None) -> "SemanticIndex":
        return cls(build_chunks(analysis), embedder)

    def scores(self, query: str, kind: str | None = "code") -> dict[int, float]:
        """Cosine similarity of the query to every chunk that shares a dimension with it
        (vectors are L2-normalised, so a dot product). Returns {chunk index: score}."""
        q: Vector = self.embedder.embed(query)
        out: dict[int, float] = defaultdict(float)
        for term, qw in q.items():
            for doc, dw in self._postings.get(term, ()):
                out[doc] += qw * dw
        return {i: s for i, s in out.items() if kind is None or self.chunks[i].kind == kind}

    def search(self, query: str, k: int = 5, min_score: float = 0.0, kind: str | None = "code") -> list[tuple[float, Chunk]]:
        """Top-k chunks by cosine similarity. `kind` limits the search to "code" (default),
        "doc", or None for both."""
        ranked = sorted(((s, self.chunks[i]) for i, s in self.scores(query, kind).items() if s > min_score),
                        key=lambda x: (-x[0], x[1].entity_id))
        return ranked[:k]
