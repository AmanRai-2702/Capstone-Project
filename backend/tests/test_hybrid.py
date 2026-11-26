"""Hybrid retrieval (lexical + dense + entity + graph). A tiny deterministic fake 'dense' embedder
stands in for the real model so these tests are fast and offline; the real model has its own opt-in test."""
import math
import os
from pathlib import Path

import pytest

from repomind.config import Settings
from repomind.graph import RepositoryGraph
from repomind.qa import QAService
from repomind.retrieval import SemanticIndex
from repomind.retrieval.chunker import Chunk
from repomind.retrieval.embedder import TfidfEmbedder
from repomind.retrieval.hybrid import HybridIndex, build_index
from repomind.services.analysis_service import AnalysisService

LOGIN_APP = Path(__file__).resolve().parents[2] / "test_apps" / "login_app"

# A hand-made 'semantic space': words in the same group share a dimension, so synonyms match.
GROUPS = [{"authenticate", "login", "credential", "credentials", "password", "verify"},
          {"user", "person", "account"}, {"message", "toast", "notify", "alert"}]


class FakeDense:
    name = "fake-dense"

    def fit(self, docs):
        return None

    def embed(self, text):
        words = set(text.lower().replace("_", " ").replace(".", " ").split())
        vec = {i: float(len(words & g)) for i, g in enumerate(GROUPS) if words & g}
        n = math.sqrt(sum(v * v for v in vec.values()))
        return {k: v / n for k, v in vec.items()} if n else {}


def chunk(eid, text):
    return Chunk(entity_id=eid, text=text, citation=f"{eid.split(':')[1]}:1-2")


@pytest.fixture
def chunks():
    return [chunk("python:a.py:verify_credentials", "verify credentials password"),
            chunk("python:b.py:show_toast", "show toast alert"),
            chunk("python:c.py:load_account", "load account person")]


def index(chunks, mode, **kw):
    return HybridIndex(chunks, SemanticIndex(chunks, TfidfEmbedder()), SemanticIndex(chunks, FakeDense()), mode, **kw)


def test_dense_finds_synonyms_that_tfidf_cannot(chunks):
    q = "authenticate the login"          # shares no word with the chunk text
    assert index(chunks, "tfidf").search(q, k=3, min_score=0.12) == []
    top = index(chunks, "dense").search(q, k=3)
    assert top and top[0][1].entity_id == "python:a.py:verify_credentials"


def test_hybrid_score_is_the_documented_weighted_sum(chunks):
    idx = index(chunks, "hybrid")
    hit = idx.rank("verify password", k=1)[0]
    assert hit.chunk.entity_id == "python:a.py:verify_credentials"
    assert set(hit.features) == {"lexical", "semantic", "entity", "graph"}
    assert hit.features["lexical"] > 0 and hit.features["semantic"] > 0
    # the only candidate matches best on both signals, so both normalised terms are exactly 1
    assert hit.score == pytest.approx(idx.weights["lexical"] + idx.weights["semantic"], rel=1e-9)


def test_unrelated_query_returns_nothing_in_every_mode(chunks):
    for mode in ("tfidf", "dense", "hybrid"):
        assert index(chunks, mode).search("zebra giraffe", k=3, min_score=0.12) == []


def test_graph_proximity_boosts_a_neighbour_of_the_best_matches():
    many = [chunk(f"python:f{i}.py:fn{i}", "widget " + "filler " * i) for i in range(6)]
    base = index(many, "hybrid")
    plain = {h.chunk.entity_id: h for h in base.rank("widget", k=6)}
    ordered = sorted(plain.values(), key=lambda h: -h.score)
    seed, outsider = ordered[0].chunk.entity_id, ordered[-1].chunk.entity_id    # last place is never a seed
    boosted = {h.chunk.entity_id: h for h in index(
        many, "hybrid", neighbors=lambda e: {outsider} if e == seed else set()).rank("widget", k=6)}
    assert boosted[outsider].features["graph"] == 1.0
    assert boosted[outsider].score == pytest.approx(plain[outsider].score + base.weights["graph"])
    assert boosted[seed].score == pytest.approx(plain[seed].score) and boosted[seed].features["graph"] == 0.0


def test_dense_request_without_model_degrades_to_tfidf(chunks):
    idx = HybridIndex(chunks, SemanticIndex(chunks, TfidfEmbedder()), None, "hybrid")
    assert idx.requested_mode == "hybrid" and idx.mode == "tfidf"
    assert idx.search("verify password", k=1, min_score=0.12)[0][1].entity_id == "python:a.py:verify_credentials"


def test_unknown_mode_is_rejected(chunks):
    with pytest.raises(ValueError):
        index(chunks, "magic")


def test_tfidf_mode_matches_the_plain_semantic_index(chunks):
    lex = SemanticIndex(chunks, TfidfEmbedder())
    for q in ("verify password", "toast alert", "account"):
        assert [c.entity_id for _, c in index(chunks, "tfidf").search(q, k=3, min_score=0.12)] == \
               [c.entity_id for _, c in lex.search(q, k=3, min_score=0.12)]


@pytest.fixture(scope="module")
def analysis(tmp_path_factory):
    return AnalysisService(Settings(workspace_dir=tmp_path_factory.mktemp("ws"), log_level="WARNING")).analyze(str(LOGIN_APP))


def test_qa_service_defaults_to_tfidf_and_reports_the_mode(analysis):
    qa = QAService(analysis, settings=Settings(workspace_dir=Path("."), log_level="WARNING"))
    assert qa.status()["retrieval_mode"] == "tfidf"


def test_qa_service_degrades_when_dense_model_unavailable(analysis, monkeypatch):
    import repomind.retrieval.embedder as emb

    def offline(model=None):
        raise OSError("simulated: no network, model not cached")

    monkeypatch.setattr(emb, "get_dense_embedder", offline)
    qa = QAService(analysis, settings=Settings(workspace_dir=Path("."), log_level="WARNING", retrieval_mode="hybrid"))
    st = qa.status()
    assert st["retrieval_requested"] == "hybrid" and st["retrieval_mode"] == "tfidf"
    assert qa.ask("Where is handleLogin implemented?").grounded


def test_build_index_tfidf_needs_no_dense_package(analysis):
    idx = build_index(analysis, RepositoryGraph.from_analysis(analysis), "tfidf")
    assert idx.mode == "tfidf" and idx.dense is None


@pytest.mark.skipif(not os.getenv("REPOMIND_RUN_DENSE_TESTS"),
                    reason="loads the real sentence-transformers model (~25 s; needs network the first time): set REPOMIND_RUN_DENSE_TESTS=1")
def test_real_sentence_transformer_hybrid_answers_a_paraphrase(analysis):
    qa = QAService(analysis, settings=Settings(workspace_dir=Path("."), log_level="WARNING", retrieval_mode="hybrid"))
    assert qa.status()["retrieval_mode"] == "hybrid"
    a = qa.ask("Where is the password verified?")
    assert a.grounded and "authenticate" in a.answer
