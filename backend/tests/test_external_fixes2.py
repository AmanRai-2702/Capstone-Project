"""Two defects found by reviewing a sample of relationships on real repositories (Phase 8):
scoped `el.querySelector(...)` lookups and `from package import submodule`."""
from repomind.config import Settings
from repomind.services.analysis_service import AnalysisService


def analyze(tmp_path, files):
    repo = tmp_path / "repo"
    for rel, text in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text, encoding="utf-8")
    return AnalysisService(Settings(workspace_dir=tmp_path / "ws", log_level="WARNING")).analyze(str(repo))


def refs_from(a, source_suffix):
    classes = {e.id: e.metadata.get("classes", []) for e in a.entities if e.type.value == "html_element"}
    return {tuple(classes[r.target_id]) for r in a.relationships
            if r.type.value == "REFERENCES" and r.source_id.endswith(source_suffix) and r.target_id in classes}


PAGE = """<button id="play"><i class="fas back"></i><i class="fas mid"></i></button>
<i class="fas outside"></i>
<script src="a.js"></script>"""


def test_scoped_query_selector_only_matches_inside_the_receiver(tmp_path):
    a = analyze(tmp_path, {"index.html": PAGE, "a.js": "const play = document.getElementById('play');\nfunction toggle() { play.querySelector('i.fas'); }"})
    got = refs_from(a, ":toggle")
    assert ("fas", "outside") not in got                       # outside #play: never matched
    assert {("fas", "back"), ("fas", "mid")} <= got            # inside #play: both icons, flagged as ambiguous below
    kinds = {r.kind.value for r in a.relationships if r.type.value == "REFERENCES" and r.source_id.endswith(":toggle")}
    assert "heuristic" in kinds                                # several candidates: never presented as certain


def test_a_unique_scoped_match_is_precise(tmp_path):
    a = analyze(tmp_path, {"index.html": '<div id="box"><span class="only"></span></div><span class="only"></span><script src="a.js"></script>',
                           "a.js": "const box = document.getElementById('box');\nfunction f() { box.querySelector('span.only'); }"})
    edges = [r for r in a.relationships if r.type.value == "REFERENCES" and r.source_id.endswith(":f") and r.target_id.startswith("html:")]
    assert len(edges) == 1 and edges[0].target_id == "html:index.html:span@1"           # the span INSIDE #box, not the one outside
    inside = next(e for e in a.entities if e.id == edges[0].target_id)
    assert [d["t"] for d in inside.metadata["dom"]["path"]][-1] == "div"


def test_query_selector_on_an_unknown_receiver_creates_no_edge(tmp_path):
    a = analyze(tmp_path, {"index.html": '<p id="x" class="a"></p><script src="a.js"></script>',
                           "a.js": "function f(parent) { parent.querySelector('.a'); getRoot().querySelector('.a'); }"})
    assert not [r for r in a.relationships if r.type.value == "REFERENCES" and r.source_id.endswith(":f")]
    assert not [u for u in a.unresolved if u.source_id.endswith(":f")]       # nothing was asserted, so nothing is unresolved


def test_document_level_lookups_are_unchanged(tmp_path):
    a = analyze(tmp_path, {"index.html": '<p id="x" class="a"></p><script src="a.js"></script>',
                           "a.js": "function f() { document.querySelector('.a'); document.getElementById('x'); document.body.querySelector('#x'); }"})
    assert len([r for r in a.relationships if r.type.value == "REFERENCES" and r.source_id.endswith(":f")]) >= 1
    assert not [u for u in a.unresolved if u.source_id.endswith(":f")]


def imports(a, source):
    return {r.target_id for r in a.relationships if r.type.value == "IMPORTS" and r.source_id == f"file:{source}"}


def test_from_package_import_submodule_reaches_the_submodule(tmp_path):
    a = analyze(tmp_path, {"pkg/__init__.py": "from . import db\nfrom pkg import views\n", "pkg/db.py": "def get_db(): pass\n",
                           "pkg/views.py": "x = 1\n", "main.py": "from pkg import db, get_thing\nfrom pkg.db import get_db\n"})
    assert {"file:pkg/db.py", "file:pkg/views.py"} <= imports(a, "pkg/__init__.py")
    assert {"file:pkg/__init__.py", "file:pkg/db.py"} <= imports(a, "main.py")            # the package AND the submodule


def test_importing_a_function_from_a_package_does_not_invent_a_submodule(tmp_path):
    a = analyze(tmp_path, {"pkg/__init__.py": "def helper(): pass\n", "pkg/other.py": "y = 2\n", "main.py": "from pkg import helper\n"})
    assert imports(a, "main.py") == {"file:pkg/__init__.py"}


def test_a_module_never_gets_an_import_edge_to_itself(tmp_path):
    a = analyze(tmp_path, {"pkg/__init__.py": "from pkg import __init__\n", "pkg/a.py": "z = 1\n"})
    assert all(r.source_id != r.target_id for r in a.relationships if r.type.value == "IMPORTS" and r.source_id.endswith("a.py"))
    assert not [r for r in a.relationships if r.type.value == "IMPORTS" and r.target_id == "file:pkg/__init__.py/__init__.py"]


def test_two_elements_of_the_same_tag_on_one_line_are_both_kept(tmp_path):
    a = analyze(tmp_path, {"index.html": '<ul><li class="a">1</li><li class="b">2</li><li class="c">3</li></ul>'})
    ids = sorted(e.id for e in a.entities if e.type.value == "html_element" and e.metadata.get("tag") == "li")
    assert ids == ["html:index.html:li@1", "html:index.html:li@1:2", "html:index.html:li@1:3"]
    assert [e.metadata["classes"] for e in sorted((e for e in a.entities if e.id in ids), key=lambda e: e.id)] == [["a"], ["b"], ["c"]]
