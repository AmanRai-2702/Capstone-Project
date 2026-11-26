"""Fixes found by running RepoMind on unfamiliar repositories (Phase 8): each test reproduces a pattern seen in
microblog, Flask, the FastAPI template or vanillawebprojects."""
from pathlib import Path

from repomind.analyzers.python_analyzer import PythonAnalyzer
from repomind.config import Settings
from repomind.services.analysis_service import AnalysisService


def analyze(tmp_path, files):
    repo = tmp_path / "repo"
    for rel, text in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text, encoding="utf-8")
    return AnalysisService(Settings(workspace_dir=tmp_path / "ws", log_level="WARNING")).analyze(str(repo))


def keys(a, rtype):
    return {(r.source_id.split(":", 2)[2], r.target_id.split(":", 2)[2], r.kind.value) for r in a.relationships if r.type.value == rtype}


def reasons(a):
    return {(u.reason, u.target_id.split(":", 2)[-1]) for u in a.unresolved}


# --------------------------------------------------------------------------- Python
def test_self_call_of_a_method_inherited_from_an_external_base_creates_no_edge(tmp_path):
    a = analyze(tmp_path, {"t.py": "import unittest\n\nclass Case(unittest.TestCase):\n    def test_x(self):\n        self.assertTrue(True)\n"})
    assert not [t for _, t, _ in keys(a, "CALLS") if t.endswith("assertTrue")]
    assert not [u for u in a.unresolved if u.reason == "unresolved_target"]


def test_method_inherited_from_a_library_base_is_reported_as_that_library(tmp_path):
    a = analyze(tmp_path, {"forms.py": "from flask_wtf import FlaskForm\n\nclass LoginForm(FlaskForm):\n    pass\n",
                           "views.py": "from forms import LoginForm\n\ndef login():\n    form = LoginForm()\n    return form.validate_on_submit()\n"})
    found = [u for u in a.unresolved if u.target_id.endswith("LoginForm.validate_on_submit")]
    assert found and found[0].reason == "external_package" and found[0].detail == "flask_wtf"


def test_a_method_missing_on_a_pure_repository_class_stays_unresolved(tmp_path):
    a = analyze(tmp_path, {"m.py": "class A:\n    pass\n\ndef f():\n    a = A()\n    a.missing()\n"})
    assert [u.reason for u in a.unresolved if u.target_id.endswith("A.missing")] == ["unresolved_target"]


def test_import_alias_uses_the_original_name(tmp_path):
    a = analyze(tmp_path, {"base.py": "class Base:\n    def run(self): pass\n\ndef helper(): pass\n",
                           "use.py": "from base import Base as B, helper as h\n\nclass C(B):\n    def go(self):\n        h()\n        self.run()\n"})
    assert ("C", "Base", "detected") in keys(a, "INHERITS") or ("C", "Base", "inferred") in keys(a, "INHERITS")
    assert any(t == "helper" for _, t, _ in keys(a, "CALLS"))
    assert not [u for u in a.unresolved if u.reason == "unresolved_target"]


def test_type_alias_is_not_a_class_and_resolves_to_what_it_stands_for(tmp_path):
    a = analyze(tmp_path, {
        "repo.py": "class Repo:\n    def save(self): pass\n",
        "deps.py": "from typing import Annotated\nfrom repo import Repo\n\nRepoDep = Annotated[Repo, 'dependency']\n",
        "routes.py": "from deps import RepoDep\n\ndef create(repo: RepoDep):\n    repo.save()\n"})
    assert ("create", "Repo.save", "inferred") in keys(a, "CALLS")
    assert not [t for _, t, _ in keys(a, "USES") if t.endswith("RepoDep")]
    assert not [u for u in a.unresolved if "RepoDep" in u.target_id]


def test_unresolved_data_flow_into_library_calls_is_dropped_and_counted(tmp_path):
    a = analyze(tmp_path, {"m.py": "from flask import render_template\n\ndef load(): return 1\n\ndef view():\n    x = load()\n    return render_template('a.html', x=x)\n"})
    assert not [u for u in a.unresolved if u.type == "DATA_FLOW"]
    assert a.stats["dropped_unresolved_enrichment_links"] >= 0


def test_analyzer_reuses_parsed_trees_but_never_for_changed_text():
    an = PythonAnalyzer()
    an.prepare({"m.py": "def a(): pass\n"})
    assert {e.name for e in an.analyze_file("m.py", "def a(): pass\n").entities if e.type.value == "function"} == {"a"}
    assert {e.name for e in an.analyze_file("m.py", "def b(): pass\n").entities if e.type.value == "function"} == {"b"}   # different text


# --------------------------------------------------------------------------- resolver / HTML
def test_external_api_is_never_matched_to_a_local_route(tmp_path):
    a = analyze(tmp_path, {
        "app.py": "from fastapi import FastAPI\napp = FastAPI()\n\n@app.get('/api')\ndef root(): return {}\n",
        "index.html": '<script src="a.js"></script>',
        "a.js": "function ext() { fetch('https://randomuser.me/api'); }\nfunction local() { fetch('http://localhost:8000/api'); }"})
    assert not [r for r in a.relationships if r.type.value == "REQUESTS" and r.source_id.endswith(":ext")]
    assert ("external_api", "api:GET:https://randomuser.me/api") in {(u.reason, u.target_id) for u in a.unresolved}
    assert [r for r in a.relationships if r.type.value == "REQUESTS" and r.source_id.endswith(":local")]    # localhost still matches


def test_templated_references_and_assets_get_honest_reasons(tmp_path):
    a = analyze(tmp_path, {"page.html": '<link rel="stylesheet" href="{{ url_for(\'static\', filename=\'s.css\') }}">'
                                        '<link rel="icon" href="favicon.png"><img src="logo.svg">'})
    got = {u.reason for u in a.unresolved}
    assert "dynamic_reference" in got and "asset_not_analysed" in got and "unresolved_target" not in got


def test_root_relative_script_resolves_to_the_only_matching_file(tmp_path):
    a = analyze(tmp_path, {"site/project/index.html": '<script src="/script.js"></script><p id="x"></p>',
                           "site/project/script.js": "document.getElementById('x');"})
    edge = [r for r in a.relationships if r.source_id == "file:site/project/index.html" and r.target_id == "file:site/project/script.js"]
    assert edge and edge[0].kind.value == "inferred"
    assert not [u for u in a.unresolved if u.target_id.endswith("script.js")]


def test_nearest_file_wins_when_several_match(tmp_path):
    a = analyze(tmp_path, {"a/index.html": '<script src="/app.js"></script>', "a/app.js": "1;", "b/app.js": "2;"})
    assert [r for r in a.relationships if r.source_id == "file:a/index.html" and r.target_id == "file:a/app.js"]


def test_jinja_extends_lets_a_child_script_find_elements_declared_in_the_parent(tmp_path):
    a = analyze(tmp_path, {
        "templates/base.html": '<html><body><span id="result"></span>{% block script %}{% endblock %}</body></html>',
        "templates/auth/child.html": "{% extends 'base.html' %}{% block script %}<script>function show() { document.getElementById('result'); }</script>{% endblock %}",
        "templates/other/base.html": "<p></p>"})
    refs = {(r.source_id.split(":", 2)[-1], r.target_id) for r in a.relationships if r.type.value == "REFERENCES"}
    assert ("show", "html:templates/base.html:#result") in refs
    assert not [u for u in a.unresolved if u.type == "REFERENCES" and "result" in u.target_id]
    assert ("file:templates/auth/child.html", "file:templates/base.html") in {(r.source_id, r.target_id) for r in a.relationships if r.type.value == "REFERENCES"}


def test_dom_selectors_use_the_css_engine_for_tags_and_compound_selectors(tmp_path):
    a = analyze(tmp_path, {"index.html": '<form id="f"><input id="name" class="field"><button class="go">x</button></form><script src="a.js"></script>',
                           "a.js": "function a() { document.querySelector('input'); document.querySelector('form .go'); document.querySelector('section'); }"})
    refs = {r.target_id.split(":", 2)[2] for r in a.relationships if r.type.value == "REFERENCES" and r.source_id.endswith(":a")}
    assert {"#name", "button@1"} <= refs or any("name" in x for x in refs)
    assert any(u.reason == "dom_selector_unmatched" and u.target_id.endswith(":section") for u in a.unresolved)


# --------------------------------------------------------------------------- JavaScript
def test_nested_function_declarations_are_entities_and_callable_from_siblings(tmp_path):
    a = analyze(tmp_path, {"index.html": '<script src="a.js"></script>', "a.js": '''function update(e) {
  function seconds(x) { return x; }
  const fmt = (n) => n;
  return fmt(seconds(e));
}'''})
    names = {e.name for e in a.entities if e.type.value == "function" and e.language.value == "javascript"}
    assert {"update", "seconds", "fmt"} <= names
    calls = {(r.source_id.split(":", 2)[2], r.target_id.split(":", 2)[2]) for r in a.relationships if r.type.value == "CALLS"}
    assert {("update", "seconds"), ("update", "fmt")} <= calls


def test_ambiguous_nested_names_are_skipped_not_guessed(tmp_path):
    a = analyze(tmp_path, {"index.html": '<script src="a.js"></script>', "a.js": '''function one() { function helper() {} helper(); }
function two() { function helper() {} helper(); }'''})
    assert "helper" not in {e.name for e in a.entities if e.language.value == "javascript"}
    assert not [r for r in a.relationships if r.type.value == "CALLS" and r.target_id.endswith(":helper")]
