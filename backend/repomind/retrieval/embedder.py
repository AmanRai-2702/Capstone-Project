"""Embedders turn text into sparse vectors (dict: term/dimension -> weight, L2-normalised).

`TfidfEmbedder` is the default: pure Python, no downloads, no keys, deterministic. It splits
identifiers (camelCase / snake_case), drops stop words and language keywords, and applies a
crude suffix stemmer so `authentication`, `authenticate` and `authenticating` all match.

`SentenceTransformerEmbedder` is an optional dense alternative (pip install sentence-transformers;
set REPOMIND_EMBEDDER=sentence-transformers). It shares the same interface, so nothing else changes.
"""
from __future__ import annotations

import hashlib
import logging
import math
import os
import re
from pathlib import Path
from collections import Counter
from typing import Protocol

log = logging.getLogger(__name__)

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
    """Optional dense embeddings (pip install sentence-transformers; downloads the model on first use).

    With `cache_dir`, chunk embeddings are stored on disk keyed by a hash of the chunk text, so opening
    a repository again (or after a small edit) only encodes the chunks that are new. The key includes
    nothing but the text because each model has its own cache file."""
    name = "sentence-transformers"
    MAX_CACHE_ENTRIES = 50_000

    def __init__(self, model: str = "sentence-transformers/all-MiniLM-L6-v2", cache_dir: Path | str | None = None) -> None:
        from sentence_transformers import SentenceTransformer  # lazy: optional dependency

        self.model_name = model
        self._model = SentenceTransformer(model)
        self._cache_file = (Path(cache_dir) / (re.sub(r"[^A-Za-z0-9_.-]+", "_", model) + ".npz")) if cache_dir else None
        self._cache: dict[str, object] = {}
        self.cache_hits = self.cache_misses = 0
        self._load_cache()

    def fit(self, documents: list[str]) -> None:  # nothing to fit; the model is pre-trained
        return None

    def embed(self, text: str) -> Vector:
        return _normalise({i: float(v) for i, v in enumerate(self._model.encode(text))})

    @staticmethod
    def _key(text: str) -> str:
        return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()

    def _load_cache(self) -> None:
        if not self._cache_file or not self._cache_file.exists():
            return
        try:
            import numpy as np
            with np.load(self._cache_file, allow_pickle=False) as data:
                self._cache = dict(zip(data["keys"].tolist(), data["vecs"]))
        except Exception as exc:       # corrupt or from another version: start clean, never fail
            log.warning("Ignoring unreadable embedding cache %s (%s)", self._cache_file.name, type(exc).__name__)
            self._cache = {}

    def _save_cache(self) -> None:
        if not self._cache_file:
            return
        try:
            import numpy as np
            while len(self._cache) > self.MAX_CACHE_ENTRIES:
                self._cache.pop(next(iter(self._cache)))          # drop the oldest entries
            self._cache_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._cache_file.with_name(self._cache_file.name + ".tmp.npz")
            np.savez(tmp, keys=np.array(list(self._cache), dtype=str), vecs=np.array(list(self._cache.values()), dtype="float32"))
            tmp.replace(self._cache_file)                          # atomic: a crash cannot leave a half file
        except OSError as exc:
            log.warning("Could not save embedding cache (%s)", exc)

    def embed_many(self, texts: list[str]) -> list[Vector]:
        """Batch encoding with the on-disk cache: only unseen texts go through the model."""
        keys = [self._key(t) for t in texts]
        todo = {k: t for k, t in zip(keys, texts) if k not in self._cache}
        self.cache_hits += len(texts) - len(todo)
        self.cache_misses += len(todo)
        if todo:
            rows = self._model.encode(list(todo.values()), batch_size=32, show_progress_bar=False)
            self._cache.update(zip(todo, rows))
            self._save_cache()
        return [_normalise({i: float(v) for i, v in enumerate(self._cache[k])}) for k in keys]


_DENSE_CACHE: dict[tuple, "SentenceTransformerEmbedder"] = {}


def get_dense_embedder(model: str | None = None, cache_dir: Path | str | None = None) -> "SentenceTransformerEmbedder":
    """Loads the model once per process (loading takes seconds). Raises if unavailable."""
    model = model or os.getenv("REPOMIND_EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2")
    key = (model, str(cache_dir) if cache_dir else None)
    if key not in _DENSE_CACHE:
        _DENSE_CACHE[key] = SentenceTransformerEmbedder(model, cache_dir)
    return _DENSE_CACHE[key]


def get_embedder(name: str | None = None) -> Embedder:
    """TF-IDF unless a dense model is requested AND loads; otherwise degrade to TF-IDF."""
    name = (name or os.getenv("REPOMIND_EMBEDDER", "tfidf")).lower()
    if name.startswith("sentence"):
        try:
            return get_dense_embedder()
        except Exception:  # package or model unavailable -> degrade gracefully
            pass
    return TfidfEmbedder()
