from pathlib import Path

import pytest

from repomind.config import Settings
from repomind.qa import QAService
from repomind.qa.models import Context, Fact
from repomind.qa.service import verify_citations
from repomind.services.analysis_service import AnalysisService

LOGIN_APP = Path(__file__).resolve().parents[2] / "test_apps" / "login_app"


@pytest.fixture(scope="module")
def qa(tmp_path_factory) -> QAService:
    settings = Settings(workspace_dir=tmp_path_factory.mktemp("ws"), log_level="WARNING")
    return QAService(AnalysisService(settings).analyze(str(LOGIN_APP)))


def test_flow_question_returns_full_login_chain(qa):
    a = qa.ask("How does the login flow work?")
    assert a.intent == "flow" and a.grounded
    text = a.answer
    for expected in ("#login-form", "handleLogin", "POST /api/login", "AuthService.authenticate",
                     "UserRepository.find_by_username"):
        assert expected in text
    # order matters: UI first, repository last
    assert text.index("#login-form") < text.index("handleLogin") < text.index("find_by_username")


def test_answers_carry_real_line_citations(qa):
    a = qa.ask("How does the login flow work?")
    assert "static/login.js:32-32" in a.citations           # form.addEventListener("submit", handleLogin)
    assert any(c.startswith("app.py:") for c in a.citations)
    assert any(c.startswith("services/auth_service.py:") for c in a.citations)


def test_impact_question(qa):
    a = qa.ask("What breaks if I change UserRepository.find_by_username?")
    assert a.intent == "impact" and a.grounded
    assert "risk high" in a.answer
    assert "#login-form" in a.answer and "AuthService.authenticate" in a.answer


def test_relations_question_incoming(qa):
    a = qa.ask("Who calls AuthService.authenticate?")
    assert a.intent == "relations"
    assert "`login`" in a.answer and "calls" in a.answer


def test_explain_question(qa):
    a = qa.ask("What is showMessage?")
    assert a.intent == "explain"
    assert "showMessage" in a.answer and "static/login.js:4-7" in a.citations


def test_unknown_topic_is_refused_not_invented(qa):
    a = qa.ask("How does the payment checkout work?")
    assert a.grounded is False
    assert "could not find" in a.answer.lower()
    assert a.citations == []


def test_citation_verifier_rejects_invented_lines():
    ctx = Context(question="q", intent="explain", facts=[Fact("x", "app.py:10-20")])
    assert verify_citations("see app.py:12", ctx) == []
    assert verify_citations("see app.py:99 and other.py:1", ctx) == ["app.py:99", "other.py:1"]


def test_bad_llm_output_falls_back_to_grounded_facts(qa):
    class LyingProvider:
        name = "liar"

        def generate(self, context):
            return "It is implemented in app.py:9999."

    qa2 = QAService(qa.retriever.analysis, graph=qa.graph, provider=LyingProvider())
    a = qa2.ask("How does the login flow work?")
    assert a.warnings and "9999" not in a.answer
    assert "handleLogin" in a.answer


@pytest.mark.parametrize("question", [
    "What breaks if I change UserRepository.find_by_username?",
    "What will be affected if I modify authenticate?",
    "If I rename handleLogin what is the blast radius?",
    "Changing find_by_username would break what?",
])
def test_impact_phrasings_are_recognised(qa, question):
    assert qa.retriever.classify(question) == "impact"


@pytest.mark.parametrize("question", [
    "How does the UI show the impact of a change?",
    "Where is impact analysis implemented?",
    "Explain the impact feature",
])
def test_the_word_impact_alone_is_not_an_impact_question(qa, question):
    assert qa.retriever.classify(question) != "impact"


def test_impact_question_without_a_named_entity_does_not_guess_a_target(qa):
    a = qa.ask("What breaks if I change the way users are authenticated?")
    assert a.intent in ("locate", "none")            # similarity only -> show relevant code, never fake an impact report
    assert "may affect" not in a.answer
