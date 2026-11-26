"""Hybrid retrieval: lexical (TF-IDF) + semantic (dense) + entity-name match + graph proximity.

The ranking is deliberately a plain weighted sum so it can be explained in one sentence and every
hit shows its own feature values (`RankedHit.features`):

    score = w_lex * lexical + w_sem * semantic + w_ent * entity_match + w_graph * graph_proximity

* lexical   - TF-IDF cosine, divided by the best lexical score of the query (0..1)
* semantic  - dense cosine, divided by the best semantic score of the query (0..1)
* entity    - share of the entity's own name words that appear in the question (0..1)
* markup    - html/css chunks are multiplied by 0.6 unless the question mentions pages, styling or UI
* graph     - 1 if the chunk's entity is directly linked in the knowledge graph to one of the
              three best text matches, else 0

Defaults: lexical 0.4, semantic 0.6, entity 0, graph 0.1.
A chunk is only a candidate if it has real evidence (lexical >= LEX_FLOOR or semantic >= SEM_FLOOR in
raw cosine), so the normalisation can never turn noise into a confident match.
When no dense model is available the semantic weight is dropped and the rest are renormalised.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from .chunker import Chunk
from .embedder import tokenize
from .index import SemanticIndex

MODES = ("tfidf", "dense", "hybrid")
LEX_FLOOR = 0.12          # raw TF-IDF cosine below this is "no lexical match" (same value Q&A used before)
# Raw dense cosine below this is "no semantic match". Calibrated on MiniLM with 24 relevant best hits
# (23 of them >= 0.23) and 16 off-topic questions (all <= 0.26): a small sample, so the margin is thin.
SEM_FLOOR = 0.22
MARKUP_TYPES = {"html_element", "css_selector"}
MARKUP_PENALTY = 0.6      # markup chunks rank lower unless the question is about pages/styling
_UI_WORDS = {"page", "html", "element", "button", "form", "input", "style", "css", "selector", "ui", "screen",
             "layout", "markup", "dom", "tag", "stylesheet", "card", "div", "visual", "widget"}
# Chosen from a flat optimum on the 24-query set in evaluate_retrieval.py (MRR 0.66-0.72 for every sane
# weighting), so treat them as reasonable defaults, not tuned results. The entity feature did not help
# there (the Retriever already links exact names separately), so its default weight is 0.
DEFAULT_WEIGHTS = {"lexical": 0.4, "semantic": 0.6, "entity": 0.0, "graph": 0.1}


@dataclass
class RankedHit:
    chunk: Chunk
    score: float
    features: dict[str, float] = field(default_factory=dict)


class HybridIndex:
    """Drop-in for SemanticIndex.search(); `mode` picks which signals are used."""

    def __init__(self, chunks: list[Chunk], lexical: SemanticIndex, dense: SemanticIndex | None = None,
                 mode: str = "hybrid", neighbors: Callable[[str], set[str]] | None = None,
                 weights: dict[str, float] | None = None):
        if mode not in MODES:
            raise ValueError(f"unknown retrieval mode {mode!r}; choose from {MODES}")
        self.chunks = chunks
        self.lexical, self.dense, self.neighbors = lexical, dense, neighbors
        self.requested_mode = mode
        # Degrade honestly: asking for dense without a dense model means TF-IDF, and we say so.
        self.mode = mode if (dense is not None or mode == "tfidf") else "tfidf"
        self.weights = dict(weights or DEFAULT_WEIGHTS)
        self._name_tokens = {i: set(tokenize(c.entity_id.rsplit(":", 1)[-1])) for i, c in enumerate(chunks)}

    # ------------------------------------------------------------------ public
    def rank(self, query: str, k: int = 5, kind: str | None = "code") -> list[RankedHit]:
        lex = self.lexical.scores(query, kind) if self.mode in ("tfidf", "hybrid") else {}
        sem = self.dense.scores(query, kind) if self.mode in ("dense", "hybrid") and self.dense else {}
        candidates = {i for i, s in lex.items() if s >= LEX_FLOOR} | {i for i, s in sem.items() if s >= SEM_FLOOR}
        if self.mode == "tfidf":
            candidates = {i for i in lex if lex[i] > 0}      # historical behaviour: caller applies its own floor
        if not candidates:
            return []
        max_lex = max((lex.get(i, 0.0) for i in candidates), default=0.0) or 1.0
        max_sem = max((sem.get(i, 0.0) for i in candidates), default=0.0) or 1.0
        q_tokens = set(tokenize(query))
        wants_markup = bool(q_tokens & set(tokenize(" ".join(_UI_WORDS))))
        w = self.weights
        hits: dict[int, RankedHit] = {}
        for i in candidates:
            name = self._name_tokens[i]
            feats = {"lexical": lex.get(i, 0.0), "semantic": sem.get(i, 0.0),
                     "entity": len(name & q_tokens) / len(name) if name else 0.0, "graph": 0.0}
            if self.mode == "tfidf":
                score = feats["lexical"]
            elif self.mode == "dense":
                score = feats["semantic"]
            else:
                score = (w["lexical"] * feats["lexical"] / max_lex + w["semantic"] * feats["semantic"] / max_sem
                         + w["entity"] * feats["entity"])
            if self.mode != "tfidf" and not wants_markup and self.chunks[i].etype in MARKUP_TYPES:
                score *= MARKUP_PENALTY       # "which code ..." should not be answered with an HTML element
                feats["markup_penalty"] = MARKUP_PENALTY
            hits[i] = RankedHit(self.chunks[i], score, feats)
        if self.mode == "hybrid" and self.neighbors and w["graph"]:
            seeds = sorted(hits.values(), key=lambda h: -h.score)[:3]
            near: set[str] = set()
            for h in seeds:
                near |= self.neighbors(h.chunk.entity_id)
            for h in hits.values():
                if h.chunk.entity_id in near and h not in seeds:
                    h.features["graph"] = 1.0
                    h.score += w["graph"]
        return sorted(hits.values(), key=lambda h: (-h.score, h.chunk.entity_id))[:k]

    def search(self, query: str, k: int = 5, min_score: float = 0.0, kind: str | None = "code") -> list[tuple[float, Chunk]]:
        """Same contract as SemanticIndex.search. `min_score` only applies in tfidf mode, where the score
        is a raw cosine; the other modes already gate candidates on raw evidence floors."""
        hits = self.rank(query, max(k, 1) if self.mode != "tfidf" else 10_000, kind)
        if self.mode == "tfidf":
            hits = [h for h in hits if h.score > min_score]
        return [(h.score, h.chunk) for h in hits[:k]]


def build_index(analysis, graph, mode: str = "tfidf", embedding_model: str | None = None,
                cache_dir=None) -> HybridIndex:
    """Index factory used by the Q&A retriever. Never raises for a missing dense model: it logs and
    degrades to TF-IDF (check `.mode` vs `.requested_mode`)."""
    import logging

    from repomind.models import RelationType

    from .chunker import build_chunks
    from .embedder import TfidfEmbedder, get_dense_embedder

    chunks = build_chunks(analysis)
    lexical = SemanticIndex(chunks, TfidfEmbedder())
    dense = None
    if mode in ("dense", "hybrid"):
        try:
            dense = SemanticIndex(chunks, get_dense_embedder(embedding_model, cache_dir))
        except Exception as exc:   # package or model download unavailable
            logging.getLogger(__name__).warning("Dense embeddings unavailable (%s); using TF-IDF retrieval", type(exc).__name__)

    def neighbors(eid: str) -> set[str]:
        return {e["target"] if e["source"] == eid else e["source"]
                for e in graph.incoming(eid) + graph.outgoing(eid) if e["type"] != RelationType.CONTAINS.value}

    return HybridIndex(chunks, lexical, dense, mode, neighbors)
