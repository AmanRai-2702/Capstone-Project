"""Embedders turn text into sparse vectors (dict: term/dimension -> weight, L2-normalised).

`TfidfEmbedder` is the default: pure Python, no downloads, no keys, deterministic. It splits
identifiers (camelCase / snake_case), drops stop words and language keywords, and applies a
crude suffix stemmer so `authentication`, `authenticate` and `authenticating` all match.

`SentenceTransformerEmbedder` is an optional dense alternative (pip install sentence-transformers;
set REPOMIND_EMBEDDER=sentence-transformers). It shares the same interface, so nothing else changes.
"""
from __future__ import annotations

import math
import os
import re
from collections import Counter
from typing import Protocol

Vector = dict[object, float]

STOP = {
    "a", "an", "the", "is", "are", "was", "of", "in", "on", "to", "for", "and", "or", "how", "does", "do",
    "what", "which", "who", "where", "when", "why", "this", "that", "it", "me", "my", "i", "we", "work",
    "works", "flow", "show", "tell", "explain", "about", "if", "there", "any", "all", "with", "from", "by",
    "implemented", "implementation", "defined", "find", "located", "file", "code", "function", "method",
    "class", "def", "return", "self", "const", "let", "var", "import", "export", "none", "null", "true", "false",
    "new", "this", "elif", "else", "then", "pass", "not", "can", "will", "get", "set",
}
_SUFFIXES = ("ications", "ication", "icating", "icated", "ations", "ation", "icates", "icate", "ating", "ated", "ates", "ate",
             "ings", "ing", "ions", "ion", "ers", "er", "ed", "es", "ly", "s")


def stem(word: str) -> str:
    for suf in _SUFFIXES:
        if word.endswith(suf) and len(word) - len(suf) >= 4:
            return word[: -len(suf)]
    return word


def tokenize(text: str) -> list[str]:
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)
    words = (w.lower() for w in re.split(r"[^A-Za-z0-9]+", spaced) if len(w) > 1)
    return [stem(w) for w in words if w not in STOP]


class Embedder(Protocol):
    name: str

    def fit(self, documents: list[str]) -> None: ...
    def embed(self, text: str) -> Vector: ...


def _normalise(vec: Vector) -> Vector:
    norm = math.sqrt(sum(v * v for v in vec.values()))
    return {k: v / norm for k, v in vec.items()} if norm else {}


class TfidfEmbedder:
    name = "tfidf"

    def __init__(self) -> None:
        self.idf: dict[str, float] = {}

    def fit(self, documents: list[str]) -> None:
        n = len(documents)
        df: Counter[str] = Counter()
        for doc in documents:
            df.update(set(tokenize(doc)))
        self.idf = {t: math.log((n + 1) / (c + 1)) + 1.0 for t, c in df.items()}

    def embed(self, text: str) -> Vector:
        tf = Counter(tokenize(text))
        return _normalise({t: (1.0 + math.log(c)) * self.idf[t] for t, c in tf.items() if t in self.idf})


class SentenceTransformerEmbedder:
    """Optional dense embeddings. Not exercised by the test-suite (needs a model download)."""
    name = "sentence-transformers"

    def __init__(self, model: str = "sentence-transformers/all-MiniLM-L6-v2") -> None:
        from sentence_transformers import SentenceTransformer  # lazy: optional dependency

        self._model = SentenceTransformer(model)

    def fit(self, documents: list[str]) -> None:  # nothing to fit; the model is pre-trained
        return None

    def embed(self, text: str) -> Vector:
        return _normalise({i: float(v) for i, v in enumerate(self._model.encode(text))})

    def embed_many(self, texts: list[str]) -> list[Vector]:
        """Batch encoding: far faster than one call per chunk."""
        return [_normalise({i: float(v) for i, v in enumerate(row)})
                for row in self._model.encode(texts, batch_size=32, show_progress_bar=False)]


_DENSE_CACHE: dict[str, "SentenceTransformerEmbedder"] = {}


def get_dense_embedder(model: str | None = None) -> "SentenceTransformerEmbedder":
    """Loads the model once per process (loading takes seconds). Raises if unavailable."""
    model = model or os.getenv("REPOMIND_EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2")
    if model not in _DENSE_CACHE:
        _DENSE_CACHE[model] = SentenceTransformerEmbedder(model)
    return _DENSE_CACHE[model]


def get_embedder(name: str | None = None) -> Embedder:
    """TF-IDF unless a dense model is requested AND loads; otherwise degrade to TF-IDF."""
    name = (name or os.getenv("REPOMIND_EMBEDDER", "tfidf")).lower()
    if name.startswith("sentence"):
        try:
            return get_dense_embedder()
        except Exception:  # package or model unavailable -> degrade gracefully
            pass
    return TfidfEmbedder()
