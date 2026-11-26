"""Behaviour that login_app cannot exercise, tested on small synthetic repositories."""
from pathlib import Path

from repomind.config import Settings
from repomind.services.analysis_service import AnalysisService


def _analyze(tmp_path: Path, files: dict[str, str]):
    repo = tmp_path / "repo"
    for name, text in files.items():
        f = repo / name
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text, encoding="utf-8")
    settings = Settings(workspace_dir=tmp_path / "ws", log_level="WARNING")
    return AnalysisService(settings).analyze(str(repo))


def _keys(analysis, rtype=None):
    return {(r.source_id, r.type.value, r.target_id, r.kind.value) for r in analysis.relationships
            if rtype is None or r.type.value == rtype}


def test_html_asset_paths_resolve_relative_to_the_page(tmp_path):
    a = _analyze(tmp_path, {
        "site/index.html": '<link rel="stylesheet" href="css/a.css"><script src="../shared/x.js"></script>'
                           '<script src="https://cdn.example.com/lib.js"></script>',
        "site/css/a.css": ".x { color: red }",
        "shared/x.js": "function f() {}",
    })
    refs = {(s, t) for s, _, t, _ in _keys(a, "REFERENCES")}
    assert ("file:site/index.html", "file:site/css/a.css") in refs
    assert ("file:site/index.html", "file:shared/x.js") in refs
    assert not any("cdn.example.com" in t for _, t in refs)          # CDN is not a repo file
    assert not any("cdn.example.com" in u.target_id for u in a.unresolved)


def test_stylesheet_only_styles_pages_that_link_it(tmp_path):
    a = _analyze(tmp_path, {
        "linked.html": '<link rel="stylesheet" href="s.css"><div id="a" class="x"></div>',
        "other.html": '<div id="b" class="x"></div>',
        "s.css": ".x { color: red }",
    })
    styled = {t for _, _, t, _ in _keys(a, "STYLES")}
    assert styled == {"html:linked.html:#a"}


def test_function_local_dom_variable_makes_compound_selector_inferred(tmp_path):
    a = _analyze(tmp_path, {
        "page.html": '<link rel="stylesheet" href="s.css"><div id="t"></div><script src="a.js"></script>',
        "s.css": "#t { opacity: 0 }\n#t.vis { opacity: 1 }\n#t.never { opacity: 2 }",
        "a.js": 'function show() { const el = document.getElementById("t"); el.classList.add("vis"); }',
    })
    styles = {(s.split(":", 2)[2], k) for s, _, _, k in _keys(a, "STYLES")}
    assert ("#t", "detected") in styles
    assert ("#t.vis", "inferred") in styles                       # JS adds "vis" inside a function
    assert not any(sel == "#t.never" for sel, _ in styles)         # no evidence -> no link


def test_combinator_and_pseudo_selectors_match_the_static_dom(tmp_path):
    css = " ".join([".x .y { color: red }", ".x > .y { color: red }", ".x:hover { color: red }", "a[href] { color: red }"])
    a = _analyze(tmp_path, {
        "p.html": '<link rel="stylesheet" href="s.css"><div id="a" class="x"><span class="y"></span></div>',
        "s.css": css,
    })
    styles = {(s.split(":", 2)[2], t.split(":", 2)[2], k) for s, _, t, k in _keys(a, "STYLES")}
    assert (".x .y", "span@1", "detected") in styles
    assert (".x > .y", "span@1", "detected") in styles
    assert (".x:hover", "#a", "inferred") in styles               # state-dependent: possible, not certain
    assert not any(sel == "a[href]" for sel, _, _ in styles)       # the page has no <a>: nothing to match


def test_truly_unsupported_selectors_are_never_guessed(tmp_path):
    css = " ".join([".y + .z { color: red }", ".y ~ .z { color: red }", ".x:has(.y) { color: red }",
                    ".y:nth-of-type(1) { color: red }", ".x:made-up { color: red }"])
    a = _analyze(tmp_path, {
        "p.html": '<link rel="stylesheet" href="s.css"><div id="a" class="x"><span class="y"></span>'
                  '<span class="z"></span></div>',
        "s.css": css,
    })
    assert _keys(a, "STYLES") == set()


def test_inherited_self_call_resolves_to_base_class(tmp_path):
    a = _analyze(tmp_path, {"m.py": (
        "class Base:\n    def helper(self):\n        pass\n\n"
        "class Child(Base):\n    def run(self):\n        self.helper()\n")})
    calls = _keys(a, "CALLS")
    assert ("python:m.py:Child.run", "CALLS", "python:m.py:Base.helper", "inferred") in calls
    assert ("python:m.py:Child", "INHERITS", "python:m.py:Base", "detected") in _keys(a, "INHERITS")


def test_js_file_imported_by_an_included_script_inherits_its_page(tmp_path):
    a = _analyze(tmp_path, {
        "web/index.html": '<p id="msg"></p><script type="module" src="js/main.js"></script>',
        "web/js/main.js": 'import { say } from "./util.js";\nsay();',
        "web/js/util.js": 'export function say() { document.getElementById("msg").textContent = "hi"; }',
    })
    refs = {(s, t) for s, _, t, _ in _keys(a, "REFERENCES")}
    assert ("javascript:web/js/util.js:say", "html:web/index.html:#msg") in refs


def test_unmatched_request_is_reported_as_unresolved(tmp_path):
    a = _analyze(tmp_path, {
        "app.py": 'from fastapi import FastAPI\napp = FastAPI()\n@app.get("/ping")\ndef ping():\n    return 1\n',
        "web/a.js": 'function go() { fetch("/pong"); }',
    })
    reasons = {(u.target_id, u.reason) for u in a.unresolved}
    assert ("api:GET:/pong", "no_matching_endpoint") in reasons
    assert not any(r.type.value == "REQUESTS" for r in a.relationships)


def test_src_layout_imports_resolve(tmp_path):
    a = _analyze(tmp_path, {
        "src/shop/core.py": "def price():\n    return 1\n",
        "src/shop/cart.py": "from shop.core import price\n\ndef total():\n    return price()\n",
    })
    assert ("python:src/shop/cart.py:total", "CALLS", "python:src/shop/core.py:price", "inferred") in _keys(a, "CALLS")
    assert ("file:src/shop/cart.py", "IMPORTS", "file:src/shop/core.py", "detected") in _keys(a, "IMPORTS")


def test_package_reexport_through_init_is_followed(tmp_path):
    a = _analyze(tmp_path, {
        "pkg/__init__.py": "from .engine import Engine\n",
        "pkg/engine.py": "class Engine:\n    def start(self):\n        pass\n",
        "main.py": "from pkg import Engine\n\ndef boot(e: Engine):\n    e.start()\n",
    })
    calls = {(s, t, k, round(c, 2)) for s, t, k, c in
             ((r.source_id, r.target_id, r.kind.value, r.confidence) for r in a.relationships if r.type.value == "CALLS")}
    assert ("python:main.py:boot", "python:pkg/engine.py:Engine.start", "inferred", 0.85) in calls
    assert not any(u.reason == "external_package" for u in a.unresolved)


def test_analysing_a_folder_that_is_itself_the_package(tmp_path):
    repo = tmp_path / "mypkg"
    for name, text in {"util.py": "def helper():\n    pass\n",
                       "main.py": "from mypkg.util import helper\n\ndef run():\n    helper()\n"}.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(text, encoding="utf-8")
    a = AnalysisService(Settings(workspace_dir=tmp_path / "ws", log_level="WARNING")).analyze(str(repo))
    assert ("python:main.py:run", "python:util.py:helper") in {(r.source_id, r.target_id) for r in a.relationships}


def test_external_package_is_never_matched_to_a_local_module_of_the_same_name(tmp_path):
    a = _analyze(tmp_path, {
        "utils.py": "def helper():\n    pass\n",
        "app.py": "from werkzeug.utils import helper\n\ndef run():\n    helper()\n",
    })
    assert not any(r.type.value in ("CALLS", "IMPORTS") for r in a.relationships)
    assert any(u.reason == "external_package" and u.detail.startswith("werkzeug") for u in a.unresolved)


INLINE_PAGE = """<!DOCTYPE html>
<html>
<body>
  <button id="go">Go</button>
  <p id="out"></p>
  <script>
    async function api(path, opts) {
      const r = await fetch(path, opts);
      return r.json();
    }
    async function save() {
      await api("/api/items", { method: "POST" });
      await api(`/api/items/${id}`);
      document.getElementById("out").textContent = "ok";
    }
    document.getElementById("go").addEventListener("click", save);
  </script>
</body>
</html>
"""
BACKEND = (
    "from fastapi import FastAPI\napp = FastAPI()\n"
    '@app.post("/api/items")\ndef create():\n    pass\n'
    '@app.get("/api/items/{item_id}")\ndef read(item_id):\n    pass\n'
)


def test_inline_script_is_analysed_with_correct_file_line_numbers(tmp_path):
    a = _analyze(tmp_path, {"web/index.html": INLINE_PAGE})
    save = next(e for e in a.entities if e.id == "javascript:web/index.html:save")
    assert save.location.file_path == "web/index.html"
    assert (save.location.start_line, save.location.end_line) == (11, 15)       # real lines in the HTML file
    keys = _keys(a)
    assert ("html:web/index.html:#go", "TRIGGERS", "javascript:web/index.html:save", "detected") in keys
    assert ("javascript:web/index.html:save", "REFERENCES", "html:web/index.html:#out", "detected") in keys


def test_fetch_wrapper_calls_become_requests_to_real_endpoints(tmp_path):
    a = _analyze(tmp_path, {"web/index.html": INLINE_PAGE, "app.py": BACKEND})
    reqs = {(r.source_id, r.target_id, r.kind.value) for r in a.relationships if r.type.value == "REQUESTS"}
    assert ("javascript:web/index.html:save", "api:POST:/api/items", "inferred") in reqs       # via api() wrapper
    assert ("javascript:web/index.html:save", "api:GET:/api/items/{item_id}", "inferred") in reqs
    # the wrapper's own fetch(path) is not a request to anything
    assert not any(s == "javascript:web/index.html:api" for s, _, _ in reqs)


def test_a_helper_that_does_not_call_fetch_is_not_a_wrapper(tmp_path):
    a = _analyze(tmp_path, {"web/a.js": 'function log(msg) { console.log(msg); }\nfunction go() { log("/api/x"); }'})
    assert not any(r.type.value == "REQUESTS" for r in a.relationships)
    assert not any(u.type == "REQUESTS" for u in a.unresolved)


def test_repomind_ui_is_connected_to_repomind_api(tmp_path):
    """Dogfooding: the web UI shipped with RepoMind is wired to the API it calls."""
    root = Path(__file__).resolve().parents[1] / "repomind"
    a = AnalysisService(Settings(workspace_dir=tmp_path / "ws", log_level="WARNING")).analyze(str(root))
    # named functions or, for anonymous event handlers, the page file itself
    clients = {t for s, _, t, _ in _keys(a, "REQUESTS")
               if s.startswith(("javascript:api/static/index.html:", "file:api/static/index.html"))}
    assert "api:GET:/api/repositories/{repo_id}/graph" in clients
    assert "api:POST:/api/repositories/{repo_id}/ask" in clients
    assert not any(u.reason == "no_matching_endpoint" for u in a.unresolved)    # every UI call matches a route
