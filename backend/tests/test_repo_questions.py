"""Regression tests for repository-level questions (purpose, components, tech stack, docs).

These failed before Phase 1: a question naming no function or class fell through to the
"could not find anything" refusal."""
from pathlib import Path

import pytest

from repomind.config import Settings
from repomind.qa import QAService
from repomind.retrieval import build_chunks
from repomind.services.analysis_service import AnalysisService

README = "# Shop Notes\n\nShop Notes lets a small shop keep a list of notes.\nIt has a Flask API and a plain JavaScript page.\n"


@pytest.fixture(scope="module")
def repo(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("repo")
    (root / "README.md").write_text(README, encoding="utf-8")
    (root / "requirements.txt").write_text("flask>=3.0\nrequests\n", encoding="utf-8")
    (root / ".env").write_text("API_SECRET=do-not-index-me\n", encoding="utf-8")
    (root / "app.py").write_text(
        "from flask import Flask, jsonify\napp = Flask(__name__)\n\n"
        "@app.get('/api/notes')\ndef list_notes():\n    return jsonify([])\n", encoding="utf-8")
    (root / "static").mkdir()
    (root / "static" / "index.html").write_text('<button id="load">Load</button><script src="notes.js"></script>', encoding="utf-8")
    (root / "static" / "notes.js").write_text(
        "async function loadNotes() { await fetch('/api/notes'); }\n"
        "document.getElementById('load').addEventListener('click', loadNotes);\n", encoding="utf-8")
    return root


@pytest.fixture(scope="module")
def analysis(repo, tmp_path_factory):
    return AnalysisService(Settings(workspace_dir=tmp_path_factory.mktemp("ws"), log_level="WARNING")).analyze(str(repo))


@pytest.fixture(scope="module")
def qa(analysis) -> QAService:
    return QAService(analysis)


@pytest.mark.parametrize("question", [
    "What is the purpose of this repository?", "What does this application do?", "Summarize the repository",
    "Give me an overview of this project"])
def test_purpose_questions_use_the_readme(qa, question):
    a = qa.ask(question)
    assert a.intent == "overview" and a.grounded
    assert any(c.startswith("README.md:") for c in a.citations)
    assert "small shop" in a.answer or "Shop Notes" in a.answer


@pytest.mark.parametrize("question", [
    "What are the main components of this application?", "List the main modules", "Show the project structure"])
def test_component_questions(qa, question):
    a = qa.ask(question)
    assert a.intent == "components" and a.grounded
    assert "app.py" in a.answer and "static/notes.js" in a.answer


@pytest.mark.parametrize("question", [
    "Which programming languages are used?", "What technologies and frameworks are used?", "What is the tech stack?"])
def test_tech_stack_questions(qa, question):
    a = qa.ask(question)
    assert a.intent == "tech" and a.grounded
    assert "Flask web framework" in a.answer and "python" in a.answer and "javascript" in a.answer
    assert any(c.startswith("requirements.txt:") for c in a.citations)


def test_frontend_backend_connection_question(qa):
    a = qa.ask("How is the frontend connected to the backend?")
    assert a.intent == "architecture" and a.grounded
    assert "/api/notes" in a.answer and "loadNotes" in a.answer


def test_documentation_question_cites_doc_lines(qa):
    a = qa.ask("What does the readme say about the API?")
    assert a.grounded and any(c.startswith("README.md:") for c in a.citations)


def test_function_question_is_not_hijacked_by_repo_intents(qa):
    assert qa.ask("What does loadNotes do?").intent != "overview"
    assert qa.ask("Who calls loadNotes?").intent == "relations"


def test_vague_question_without_docs_is_still_refused(qa):
    a = qa.ask("How does the payment checkout work?")
    assert a.grounded is False


def test_secret_files_are_never_indexed(analysis):
    text = "\n".join(c.text for c in build_chunks(analysis))
    assert "do-not-index-me" not in text
    assert "README.md" in text and "requirements.txt" in text


def test_doc_chunks_do_not_change_code_search(analysis):
    from repomind.retrieval import SemanticIndex
    idx = SemanticIndex.from_analysis(analysis)
    assert all(c.kind == "code" for _, c in idx.search("notes", k=10))
    assert all(c.kind == "doc" for _, c in idx.search("shop notes", k=10, kind="doc"))
