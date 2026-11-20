from repomind.analyzers.javascript_analyzer import JavaScriptAnalyzer
from repomind.models import EntityType

CODE = """
import { login as doLogin, logout } from "./api.js";
import axios from "axios";

export function exported() { return 1; }

export class Admin extends User {
  run() { this.step(); doLogin(); }
  step() { logout(); }
}

const arrow = () => { exported(); };
"""


def _analyze():
    return JavaScriptAnalyzer().analyze_file("static/app.js", CODE)


def _rels(result, rtype):
    return {(r.source_id, r.target_id) for r in result.relationships if r.type.value == rtype}


def test_exported_functions_and_classes_are_extracted():
    ids = _analyze().entity_ids()
    assert "javascript:static/app.js:exported" in ids
    assert "javascript:static/app.js:Admin" in ids
    assert "javascript:static/app.js:Admin.run" in ids
    types = {e.id: e.type for e in _analyze().entities}
    assert types["javascript:static/app.js:Admin.step"] == EntityType.METHOD


def test_relative_import_creates_imports_edge_and_external_is_skipped():
    imports = _rels(_analyze(), "IMPORTS")
    assert imports == {("file:static/app.js", "file:static/api.js")}


def test_named_import_calls_resolve_including_alias():
    calls = _rels(_analyze(), "CALLS")
    assert ("javascript:static/app.js:Admin.run", "javascript:static/api.js:login") in calls
    assert ("javascript:static/app.js:Admin.step", "javascript:static/api.js:logout") in calls


def test_this_method_call_and_inheritance():
    result = _analyze()
    assert ("javascript:static/app.js:Admin.run", "javascript:static/app.js:Admin.step") in _rels(result, "CALLS")
    assert ("javascript:static/app.js:Admin", "javascript:static/app.js:User") in _rels(result, "INHERITS")


def test_calls_inside_const_arrow_function_are_attributed_to_it():
    calls = _rels(_analyze(), "CALLS")
    assert ("javascript:static/app.js:arrow", "javascript:static/app.js:exported") in calls
    assert ("file:static/app.js", "javascript:static/app.js:exported") not in calls
