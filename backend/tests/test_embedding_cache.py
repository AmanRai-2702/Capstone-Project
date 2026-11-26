"""On-disk embedding cache, markup ranking prior and candidate listing for vague questions.
A fake SentenceTransformer is injected so nothing is downloaded."""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

np = pytest.importorskip("numpy")

from repomind.config import Settings                                   # noqa: E402
from repomind.qa import QAService                                      # noqa: E402
from repomind.retrieval import SemanticIndex                           # noqa: E402
from repomind.retrieval.chunker import Chunk                           # noqa: E402
from repomind.retrieval.embedder import SentenceTransformerEmbedder, TfidfEmbedder   # noqa: E402
from repomind.retrieval.hybrid import HybridIndex                      # noqa: E402
from repomind.services.analysis_service import AnalysisService         # noqa: E402

LOGIN_APP = Path(__file__).resolve().parents[2] / "test_apps" / "login_app"


class FakeModel:
    """Deterministic 8-dim 'embeddings'; counts how many texts it was asked to encode."""
    encoded = 0

    def __init__(self, name):
        pass

    def encode(self, texts, batch_size=32, show_progress_bar=False):
        single = isinstance(texts, str)
        texts = [texts] if single else texts
        FakeModel.encoded += len(texts)
        rows = [np.array([(sum(map(ord, t)) * (i + 1)) % 17 + 1.0 for i in range(8)], dtype="float32") for t in texts]
        return rows[0] if single else np.array(rows)


@pytest.fixture(autouse=True)
def fake_sentence_transformers(monkeypatch):
    monkeypatch.setitem(sys.modules, "sentence_transformers", SimpleNamespace(SentenceTransformer=FakeModel))
    FakeModel.encoded = 0


def test_second_run_encodes_nothing_and_returns_identical_vectors(tmp_path):
    texts = ["alpha function", "beta class", "gamma endpoint"]
    first = SentenceTransformerEmbedder("fake/model", tmp_path).embed_many(texts)
    assert FakeModel.encoded == 3
    again = SentenceTransformerEmbedder("fake/model", tmp_path)          # new object, same cache dir
    second = again.embed_many(texts)
    assert FakeModel.encoded == 3 and again.cache_hits == 3 and again.cache_misses == 0
    for a, b in zip(first, second):
        assert a.keys() == b.keys() and all(a[k] == pytest.approx(b[k], abs=1e-6) for k in a)


def test_only_new_or_changed_chunks_are_encoded(tmp_path):
    SentenceTransformerEmbedder("fake/model", tmp_path).embed_many(["one", "two"])
    FakeModel.encoded = 0
    SentenceTransformerEmbedder("fake/model", tmp_path).embed_many(["one", "two changed", "three"])
    assert FakeModel.encoded == 2


def test_each_model_has_its_own_cache_file(tmp_path):
    SentenceTransformerEmbedder("fake/model-a", tmp_path).embed_many(["x"])
    SentenceTransformerEmbedder("fake/model-b", tmp_path).embed_many(["x"])
    assert FakeModel.encoded == 2 and len(list(tmp_path.glob("*.npz"))) == 2


def test_corrupt_cache_is_ignored_not_fatal(tmp_path):
    SentenceTransformerEmbedder("fake/model", tmp_path).embed_many(["x"])
    (next(tmp_path.glob("*.npz"))).write_bytes(b"not a real npz file")
    FakeModel.encoded = 0
    out = SentenceTransformerEmbedder("fake/model", tmp_path).embed_many(["x"])
    assert FakeModel.encoded == 1 and len(out) == 1


def test_works_without_a_cache_dir():
    e = SentenceTransformerEmbedder("fake/model")
    assert len(e.embed_many(["a", "b"])) == 2 and e._cache_file is None


# ------------------------------------------------------------------ markup prior
def _chunks():
    text = "login handler for the sign in"
    return [Chunk("python:a.py:login", text, "a.py:1-2", etype="function"),
            Chunk("html:i.html:#login-card", text, "i.html:1-2", etype="html_element")]


def _index(chunks):
    lex = SemanticIndex(chunks, TfidfEmbedder())
    return HybridIndex(chunks, lex, SemanticIndex(chunks, TfidfEmbedder()), "hybrid")


def test_code_outranks_identical_markup_unless_the_question_is_about_the_page():
    idx = _index(_chunks())
    code_first = idx.rank("login handler sign in", k=2)
    assert [h.chunk.etype for h in code_first] == ["function", "html_element"]
    assert code_first[1].features["markup_penalty"] == 0.6
    ui = {h.chunk.etype: h for h in idx.rank("login handler sign in element on the page", k=2)}
    assert "markup_penalty" not in ui["html_element"].features


def test_tfidf_mode_ignores_the_markup_prior():
    chunks = _chunks()
    idx = HybridIndex(chunks, SemanticIndex(chunks, TfidfEmbedder()), None, "tfidf")
    assert all("markup_penalty" not in h.features for h in idx.rank("login handler", k=2))


# ------------------------------------------------------------------ vague questions list candidates
@pytest.fixture(scope="module")
def qa(tmp_path_factory):
    s = Settings(workspace_dir=tmp_path_factory.mktemp("ws"), log_level="WARNING")
    return QAService(AnalysisService(s).analyze(str(LOGIN_APP)), settings=s)


def test_vague_question_lists_several_candidates_not_one_guess(qa):
    a = qa.ask("how are user accounts stored")
    assert a.intent == "locate" and a.grounded
    assert "UserRepository" in a.answer and a.answer.count("is defined here") >= 2
    assert "semantic similarity" in a.answer


def test_named_entity_questions_are_still_explained(qa):
    assert qa.ask("What does handleLogin do?").intent == "explain"


def test_real_chunks_carry_their_entity_type(qa):
    """Guards the chunker -> ranker hand-off that the hand-built chunks above would not catch."""
    from repomind.retrieval import build_chunks
    types = {c.entity_id: c.etype for c in build_chunks(qa.analysis) if c.kind == "code"}
    assert types["python:app.py:login"] == "function"
    assert any(t == "html_element" for t in types.values())
    assert all(t for t in types.values())
