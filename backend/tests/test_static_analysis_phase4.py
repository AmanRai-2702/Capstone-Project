"""Phase 4 static-analysis improvements: CSS selector engine, JS (axios, dynamic import, DOM helpers,
anonymous handlers), Python (aliases, module calls, routes, inheritance) and the benchmark gate."""
import sys
from pathlib import Path

import pytest

from repomind.analyzers import css_selectors as cs
from repomind.config import Settings
from repomind.services.analysis_service import AnalysisService

BACKEND = Path(__file__).resolve().parents[1]
APPS = BACKEND.parent / "test_apps"


def analyze(tmp_path, files: dict[str, str]):
    root = tmp_path / "repo"
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    return AnalysisService(Settings(workspace_dir=tmp_path / "ws", log_level="WARNING")).analyze(str(root))


def edges(a, rtype=None):
    return {(r.source_id, r.type.value, r.target_id, r.kind.value) for r in a.relationships
            if rtype is None or r.type.value == rtype}


# --------------------------------------------------------------------------- benchmark gate
def test_every_benchmark_app_passes_its_ground_truth(tmp_path, monkeypatch):
    """login_app / notes_app are exhaustive; shop_app / dash_app / style_app are probes of known limitations."""
    monkeypatch.setenv("REPOMIND_WORKSPACE", str(tmp_path / "ws"))
    sys.path.insert(0, str(BACKEND))
    import evaluate_ground_truth as ev
    apps = sorted(p.parent for p in APPS.glob("*/ground_truth.json"))
    assert {a.name for a in apps} >= {"login_app", "notes_app", "shop_app", "dash_app", "style_app"}
    for app in apps:
        res = ev.evaluate_app(app)
        assert res["ok"], (app.name, res["relationships"]["missing"], res["entities"]["missing"], res["forbidden_hit"])
        assert res["relationships"]["recall"] == 1.0


# --------------------------------------------------------------------------- selector engine
def d(tag, id="", classes=(), attrs=None, n=0, s=1):
    return {"t": tag, "i": id, "c": list(classes), "a": attrs or {}, "n": n, "s": s}


def m(selector, *chain, **kw):
    parsed = cs.parse_selector(selector)
    assert parsed is not None, selector
    return cs.match(parsed, list(chain), **kw)


def test_split_top_level_respects_parentheses_brackets_and_quotes():
    assert cs.split_top_level('a, :is(b, c), [x="1,2"], d') == ["a", ":is(b, c)", '[x="1,2"]', "d"]


@pytest.mark.parametrize("selector", [".a + .b", ".a ~ .b", ".a:has(.b)", "a:nth-of-type(2)", ":made-up", "&.x",
                                      "a >", "> a", "a > > b", ".a:not(.b .c)"])
def test_unsupported_or_invalid_selectors_do_not_parse(selector):
    assert cs.parse_selector(selector) is None


def test_descendant_versus_child():
    ul, li = d("ul", "u"), d("li", "l")
    wrapper = d("div", classes=["w"])
    assert m(".w li", wrapper, ul, li) == cs.YES                  # descendant through an intermediate element
    assert m(".w > li", wrapper, ul, li) == cs.NO                 # not a direct child
    assert m("ul > li", wrapper, ul, li) == cs.YES


def test_descendant_backtracks_over_several_candidates():
    outer, mid, inner = d("div", classes=["a"]), d("div", classes=["a"]), d("p", classes=["b"])
    assert m(".a .a .b", outer, mid, inner) == cs.YES
    assert m(".a .a .a .b", outer, mid, inner) == cs.NO


@pytest.mark.parametrize("selector, attrs, expected", [
    ('input[type="text"]', {"type": "text"}, cs.YES), ('input[type="text"]', {"type": "password"}, cs.NO),
    ("input[type=text]", {"type": "text"}, cs.YES), ("input[disabled]", {"disabled": ""}, cs.YES),
    ("input[disabled]", {}, cs.NO), ('input[class~="b"]', {"class": "a b c"}, cs.YES),
    ('input[lang|="en"]', {"lang": "en-US"}, cs.YES), ('input[href^="http"]', {"href": "https://x"}, cs.YES),
    ('input[href$=".pdf"]', {"href": "a.pdf"}, cs.YES), ('input[href*="ex"]', {"href": "an example"}, cs.YES),
    ('input[type="TEXT" i]', {"type": "text"}, cs.YES), ('input[href^=""]', {"href": "x"}, cs.NO),
])
def test_attribute_selectors(selector, attrs, expected):
    assert m(selector, d("input", attrs=attrs)) == expected


def test_structural_pseudo_classes():
    first, middle, last = d("li", n=0, s=3), d("li", n=1, s=3), d("li", n=2, s=3)
    assert [m("li:first-child", x) for x in (first, middle, last)] == [cs.YES, cs.NO, cs.NO]
    assert [m("li:last-child", x) for x in (first, middle, last)] == [cs.NO, cs.NO, cs.YES]
    assert [m("li:nth-child(2)", x) for x in (first, middle, last)] == [cs.NO, cs.YES, cs.NO]
    assert [m("li:nth-child(odd)", x) for x in (first, middle, last)] == [cs.YES, cs.NO, cs.YES]
    assert [m("li:nth-child(2n+1)", x) for x in (first, middle, last)] == [cs.YES, cs.NO, cs.YES]
    assert [m("li:nth-child(-n+2)", x) for x in (first, middle, last)] == [cs.YES, cs.YES, cs.NO]
    assert m("li:only-child", d("li", n=0, s=1)) == cs.YES and m("li:only-child", first) == cs.NO
    assert m(":root", d("html")) == cs.YES and m(":root", d("div")) == cs.NO


def test_unknown_position_never_matches_structural_pseudo():
    no_position = {"t": "li", "i": "", "c": [], "a": {}, "n": None, "s": None}
    assert m("li:first-child", no_position) == cs.NO


def test_state_pseudo_classes_and_pseudo_elements_are_possible_not_certain():
    notes: list[str] = []
    assert m("a:hover", d("a"), notes=notes) == cs.MAYBE and notes == ["state"]
    assert m("p::before", d("p")) == cs.MAYBE and m("p:before", d("p")) == cs.MAYBE
    assert m("a:hover", d("p")) == cs.NO                                 # the base selector must still match


def test_logical_pseudo_classes():
    assert m("p:not(.x)", d("p", classes=["y"])) == cs.YES
    assert m("p:not(.x)", d("p", classes=["x"])) == cs.NO
    assert m(":is(h1, h2)", d("h2")) == cs.YES and m(":where(h1, h2)", d("h3")) == cs.NO
    assert m("a:not(:hover)", d("a")) == cs.MAYBE


def test_classes_added_by_javascript_make_a_match_possible():
    notes: list[str] = []
    assert m("#t.vis", d("div", "t"), extra_classes=frozenset({"vis"}), notes=notes) == cs.MAYBE
    assert notes == ["dynamic"] and m("#t.never", d("div", "t"), extra_classes=frozenset({"vis"})) == cs.NO


def test_ancestor_state_makes_the_match_possible():
    assert m(".menu:hover a", d("nav", classes=["menu"]), d("a")) == cs.MAYBE


# --------------------------------------------------------------------------- HTML DOM information
def test_html_records_the_full_dom_path_including_non_entity_elements(tmp_path):
    a = analyze(tmp_path, {"i.html": '<body><section><ul><li id="x">a</li><li id="y">b</li></ul></section></body>'})
    x = next(e for e in a.entities if e.name == "#x")
    assert [p["t"] for p in x.metadata["dom"]["path"]][-3:] == ["body", "section", "ul"]    # section/ul have no id
    assert (x.metadata["dom"]["self"]["n"], x.metadata["dom"]["self"]["s"]) == (0, 2)


def test_child_combinator_is_not_fooled_by_a_non_entity_parent(tmp_path):
    """`.menu > li` must NOT match when an (id-less, class-less) <ul> sits between them."""
    a = analyze(tmp_path, {
        "i.html": '<link rel="stylesheet" href="s.css"><nav class="menu"><ul><li id="x">a</li></ul></nav>',
        "s.css": ".menu > li { color: red }\n.menu > ul { color: red }\n.menu li { color: red }",
    })
    styled = {(s.split(":", 2)[2], t.split(":", 2)[2]) for s, _, t, _ in edges(a, "STYLES")}
    assert (".menu li", "#x") in styled and (".menu > li", "#x") not in styled


def test_selector_list_with_a_pseudo_function_is_not_split_inside_parentheses(tmp_path):
    a = analyze(tmp_path, {
        "i.html": '<link rel="stylesheet" href="s.css"><h1 id="a"></h1><h2 id="b"></h2><h3 id="c"></h3>',
        "s.css": ":is(h1, h2), h3 { color: red }",
    })
    sels = {e.name for e in a.entities if e.type.value == "css_selector"}
    assert sels == {":is(h1, h2)", "h3"}
    assert {t.split(":", 2)[2] for s, _, t, _ in edges(a, "STYLES") if s.endswith(":is(h1, h2)")} == {"#a", "#b"}


# --------------------------------------------------------------------------- JavaScript
PAGE = '<script type="module" src="app.js"></script><button id="go"></button><div id="out"></div>'


def js(tmp_path, code, extra=None):
    return analyze(tmp_path, {"index.html": PAGE, "app.js": code, **(extra or {})})


def test_axios_verbs_config_and_instances(tmp_path):
    a = js(tmp_path, '''import axios from "axios";
const api = axios.create({ baseURL: "/api/" });
export const a = () => axios.get("/x");
export const b = (id) => axios.put(`/x/${id}`, {});
export const c = () => api.post("/items");
export const d = () => axios({ method: "delete", url: "/y" });
export const e = () => axios.request({ url: "/z" });
export const f = () => axios.patch(someVariable);
''')
    req = {(s.split(":", 2)[2], t) for s, _, t, _ in edges(a, "REQUESTS")} | \
          {(s.split(":", 2)[2], t) for u in a.unresolved if u.type == "REQUESTS" for s, t in [(u.source_id, u.target_id)]}
    assert ("a", "api:GET:/x") in req and ("b", "api:PUT:/x/{id}") in req
    assert ("c", "api:POST:/api/items") in req and ("d", "api:DELETE:/y") in req and ("e", "api:GET:/z") in req
    assert not any(src == "f" for src, _ in req)                       # URL is a variable: no request invented


def test_a_variable_named_axios_without_the_import_is_not_a_request(tmp_path):
    a = js(tmp_path, 'const axios = { get() {} };\nfunction f() { axios.get("/x"); }')
    assert not edges(a, "REQUESTS") and not [u for u in a.unresolved if u.type == "REQUESTS"]


def test_dynamic_import_and_namespace_imports(tmp_path):
    a = js(tmp_path, '''import * as util from "./util.js";
export async function lazy() {
  const m = await import("./lazy.js");
  m.run();
  util.helper();
}
''', {"util.js": "export function helper() {}", "lazy.js": "export function run() {}"})
    e = edges(a)
    assert ("file:app.js", "IMPORTS", "file:lazy.js", "detected") in e
    assert ("file:app.js", "IMPORTS", "file:util.js", "detected") in e
    assert ("javascript:app.js:lazy", "CALLS", "javascript:lazy.js:run", "detected") in e
    assert ("javascript:app.js:lazy", "CALLS", "javascript:util.js:helper", "detected") in e


def test_dom_helper_with_variable_argument_creates_no_reference(tmp_path):
    a = js(tmp_path, 'const $ = (s) => document.querySelector(s);\nfunction f(sel) { return $(sel); }')
    assert not [r for r in a.relationships if r.type.value == "REFERENCES" and r.target_id.startswith("html:")]
    assert not [u for u in a.unresolved if u.target_id.endswith(":selector")]


def test_dom_helper_getelementbyid_style_and_local_use(tmp_path):
    a = js(tmp_path, '''function byId(id) { return document.getElementById(id); }
function show() { byId("out").textContent = "x"; }
byId("go").addEventListener("click", show);
''')
    e = {(s, t) for s, _, t, _ in edges(a)}
    assert ("javascript:app.js:show", "html:index.html:#out") in e
    assert ("html:index.html:#go", "javascript:app.js:show") in e


def test_every_call_in_an_anonymous_handler_is_a_trigger_and_handlers_do_not_leak(tmp_path):
    a = js(tmp_path, '''function one() {}
function two() {}
function three() {}
document.getElementById("go").addEventListener("click", () => { one(); two(); });
document.getElementById("out").addEventListener("click", () => three());
''')
    trig = {(s.split(":", 2)[2], t.split(":", 2)[2]) for s, _, t, _ in edges(a, "TRIGGERS")}
    assert trig == {("#go", "handler@4"), ("#out", "handler@5")}          # each element its own handler
    calls = {(s.split(":", 2)[2], t.split(":", 2)[2]) for s, _, t, _ in edges(a, "CALLS")}
    assert {("handler@4", "one"), ("handler@4", "two"), ("handler@5", "three")} <= calls
    assert not {c for c in calls if c[0] == "handler@5" and c[1] in ("one", "two")}   # no leaking
    handlers = [e for e in a.entities if e.metadata.get("anonymous")]
    assert {e.name for e in handlers} == {"#go click handler", "#out click handler"}
    assert all(e.metadata["element"].startswith("html:index.html:#") for e in handlers)


def test_handler_on_an_unknown_element_triggers_nothing(tmp_path):
    a = js(tmp_path, 'function one() {}\nitems.forEach((li) => li.addEventListener("click", () => one()));')
    assert not edges(a, "TRIGGERS")


# --------------------------------------------------------------------------- Python
def test_route_with_several_methods_creates_one_endpoint_per_method(tmp_path):
    a = analyze(tmp_path, {"app.py": '''from flask import Flask
app = Flask(__name__)

@app.route("/x", methods=["GET", "POST"])
def x(): pass

@app.route("/y")
def y(): pass

@app.api_route("/z", methods=("PUT",))
def z(): pass
'''})
    ids = {e.id for e in a.entities if e.type.value == "api_endpoint"}
    assert ids == {"api:GET:/x", "api:POST:/x", "api:GET:/y", "api:PUT:/z"}
    handles = {(s.split(":", 2)[2], t) for s, _, t, _ in edges(a, "HANDLES")}
    assert ("x", "api:GET:/x") in handles and ("x", "api:POST:/x") in handles


def test_type_tracking_through_or_ifexp_and_alias(tmp_path):
    a = analyze(tmp_path, {"m.py": '''class Repo:
    def save(self): pass
    def load(self): pass

class Svc:
    def __init__(self, repo=None, flag=False):
        self.repo = repo or Repo()
        self.other = Repo() if flag else None

    def a(self):
        self.repo.save()

    def b(self):
        r = self.other
        r.load()

    def c(self):
        x = Repo()
        y = x
        y.save()
'''})
    calls = {(s.split(":", 2)[2], t.split(":", 2)[2]) for s, _, t, _ in edges(a, "CALLS")}
    assert {("Svc.a", "Repo.save"), ("Svc.b", "Repo.load"), ("Svc.c", "Repo.save")} <= calls


def test_untracked_receivers_create_no_calls(tmp_path):
    a = analyze(tmp_path, {"m.py": '''class Repo:
    def save(self): pass

def f(thing, other):
    thing.save()
    helper = thing
    helper.save()
    x = foo()
    x.save()
'''})
    assert not [t for _, _, t, _ in edges(a, "CALLS") if t.endswith("Repo.save")]


def test_module_alias_calls_resolve_to_repository_files_and_external_ones_are_reported(tmp_path):
    a = analyze(tmp_path, {
        "util.py": "def go(): pass",
        "m.py": "import util as u\nimport os.path\nimport util\n\ndef f():\n    u.go()\n    util.go()\n    os.path.join('a')\n"})
    calls = {(s.split(":", 2)[2], t) for s, _, t, _ in edges(a, "CALLS")}
    assert ("f", "python:util.py:go") in calls
    assert any(u.reason == "external_package" and "os" in u.target_id for u in a.unresolved)


def test_inherited_method_resolves_across_files_and_transitively(tmp_path):
    a = analyze(tmp_path, {
        "base.py": "class A:\n    def hello(self): pass\n",
        "mid.py": "from base import A\nclass B(A):\n    pass\n",
        "top.py": "from mid import B\nclass C:\n    def run(self):\n        b = B()\n        b.hello()\n"})
    calls = edges(a, "CALLS")
    assert ("python:top.py:C.run", "CALLS", "python:base.py:A.hello", "inferred") in calls
    assert not [u for u in a.unresolved if u.target_id.endswith("B.hello")]


def test_method_defined_nowhere_in_the_hierarchy_stays_unresolved(tmp_path):
    a = analyze(tmp_path, {
        "base.py": "class A:\n    pass\n",
        "top.py": "from base import A\nclass C:\n    def run(self):\n        a = A()\n        a.missing()\n"})
    assert not [t for _, _, t, _ in edges(a, "CALLS") if t.endswith("missing")]
    assert [u for u in a.unresolved if u.target_id.endswith("A.missing")]


# --------------------------------------------------------------------------- property-style handlers (el.onclick = ...)
def test_property_handlers_link_elements_to_functions(tmp_path):
    a = js(tmp_path, '''const $ = (s) => document.querySelector(s);
function named() {}
function inner() {}
function other() {}
$("#go").onclick = () => { inner(); };
document.getElementById("out").onchange = named;
const btn = document.getElementById("go");
btn.onmouseover = other;
''')
    trig = {(s.split(":", 2)[2], t.split(":", 2)[2], k) for s, _, t, k in edges(a, "TRIGGERS")}
    assert ("#go", "handler@5", "detected") in trig  # anonymous handler is an entity the element triggers
    assert ("#out", "named", "detected") in trig     # named handler
    assert ("#go", "other", "detected") in trig      # element held in a variable
    assert ("javascript:app.js:handler@5", "CALLS", "javascript:app.js:inner", "detected") in edges(a, "CALLS")


def test_property_handlers_on_unknown_elements_or_non_event_properties_are_ignored(tmp_path):
    a = js(tmp_path, '''function f() {}
const made = document.createElement("div");
made.onclick = f;
document.getElementById("go").once = f;
document.getElementById("go").onlinestatus = f;
const sel = "#go";
document.querySelector(sel).onclick = f;
''')
    assert not edges(a, "TRIGGERS")


def test_repomind_own_ui_handlers_are_now_linked(tmp_path):
    """RepoMind's UI uses `$("#go").onclick = ...` everywhere; before this fix it had no UI entry points."""
    svc = AnalysisService(Settings(workspace_dir=tmp_path / "ws", log_level="WARNING"))
    a = svc.analyze(str(BACKEND / "repomind" / "api"))
    triggers = [r for r in a.relationships if r.type.value == "TRIGGERS"]
    assert {r.source_id.split(":", 2)[2] for r in triggers} >= {"#go", "#chatform", "#diffGo"}
