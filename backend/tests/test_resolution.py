from repomind.models import Entity, EntityType, Language, RelationKind, RelationType, Relationship
from repomind.resolution import RelationshipResolver, normalize_route


def test_route_normalization_unifies_parameter_syntaxes():
    forms = ["/api/users/{user_id}", "/api/users/<int:uid>", "/api/users/:id", "/api/users/${id}/",
             "http://localhost:8000/api/users/{x}?a=1"]
    assert {normalize_route(f) for f in forms} == {"/api/users/{}"}
    assert normalize_route("/") == "/" and normalize_route("/login/") == "/login"


def _ent(eid, etype, lang=Language.PYTHON, name=None, **meta):
    return Entity(id=eid, type=etype, name=name or eid, qualified_name=name or eid, language=lang, metadata=meta)


def _world():
    ents = {e.id: e for e in [
        _ent("file:site/a.html", EntityType.FILE, Language.HTML, "site/a.html"),
        _ent("file:site/b.html", EntityType.FILE, Language.HTML, "site/b.html"),
        _ent("file:site/app.js", EntityType.FILE, Language.JAVASCRIPT, "site/app.js"),
        _ent("file:pkg/__init__.py", EntityType.FILE, Language.PYTHON, "pkg/__init__.py"),
        _ent("file:main.py", EntityType.FILE, Language.PYTHON, "main.py"),
        _ent("html:site/a.html:#go", EntityType.HTML_ELEMENT, Language.HTML, "#go", id="go", classes=["btn"]),
        _ent("html:site/b.html:#other", EntityType.HTML_ELEMENT, Language.HTML, "#other", id="other", classes=[]),
        _ent("javascript:site/app.js:run", EntityType.FUNCTION, Language.JAVASCRIPT, "run"),
        _ent("api:GET:/items/<int:item_id>", EntityType.API_ENDPOINT, Language.UNKNOWN, "GET /items",
             method="GET", route="/items/<int:item_id>"),
        _ent("python:pkg/__init__.py:helper", EntityType.FUNCTION, Language.PYTHON, "helper"),
    ]}
    return ents


def _rel(src, tgt, rtype, kind=RelationKind.DETECTED, conf=1.0):
    return Relationship(source_id=src, target_id=tgt, type=rtype, kind=kind, confidence=conf)


def test_js_dom_reference_pairs_with_the_page_that_includes_it_not_index_html():
    ents = _world()
    rels = [
        _rel("file:site/a.html", "file:site/app.js", RelationType.REFERENCES),     # <script src>
        _rel("javascript:site/app.js:run", "html:site/index.html:#go", RelationType.REFERENCES),  # guessed page
    ]
    resolved, unresolved = RelationshipResolver(ents).resolve(rels)
    keys = {r.key for r in resolved}
    assert "javascript:site/app.js:run -REFERENCES-> html:site/a.html:#go" in keys
    assert unresolved == []


def test_class_selector_reference_matches_by_class_with_lower_confidence():
    ents = _world()
    rels = [_rel("file:site/a.html", "file:site/app.js", RelationType.REFERENCES),
            _rel("javascript:site/app.js:run", "html:site/index.html:.btn", RelationType.REFERENCES)]
    resolved, _ = RelationshipResolver(ents).resolve(rels)
    r = next(r for r in resolved if r.target_id == "html:site/a.html:#go")
    assert r.kind == RelationKind.HEURISTIC and r.confidence <= 0.7


def test_request_matches_endpoint_despite_different_param_syntax():
    ents = _world()
    rels = [_rel("javascript:site/app.js:run", "api:GET:/items/{id}", RelationType.REQUESTS,
                 RelationKind.INFERRED, 0.85)]
    resolved, unresolved = RelationshipResolver(ents).resolve(rels)
    assert [r.target_id for r in resolved] == ["api:GET:/items/<int:item_id>"]
    assert resolved[0].kind == RelationKind.INFERRED and unresolved == []


def test_method_mismatch_and_missing_endpoint_are_reported_not_invented():
    ents = _world()
    rels = [_rel("javascript:site/app.js:run", "api:POST:/items/5", RelationType.REQUESTS),
            _rel("javascript:site/app.js:run", "api:GET:/nothing", RelationType.REQUESTS)]
    resolved, unresolved = RelationshipResolver(ents).resolve(rels)
    assert resolved == []
    assert {(u.reason) for u in unresolved} == {"method_mismatch", "no_matching_endpoint"}
    assert len(unresolved) == 2          # no duplicates


def test_prefix_mounted_backend_matches_heuristically():
    ents = _world()
    ents["api:GET:/v1/items/{id}"] = _ent("api:GET:/v1/items/{id}", EntityType.API_ENDPOINT, Language.UNKNOWN,
                                          "GET", method="GET", route="/v1/items/{id}")
    rels = [_rel("javascript:site/app.js:run", "api:GET:/items/{id}", RelationType.REQUESTS)]
    resolved, _ = RelationshipResolver({k: v for k, v in ents.items() if "<int" not in k}).resolve(rels)
    assert resolved[0].target_id == "api:GET:/v1/items/{id}" and resolved[0].kind == RelationKind.HEURISTIC


def test_package_init_fallback_and_external_classification():
    ents = _world()
    rels = [_rel("file:main.py", "file:pkg.py", RelationType.IMPORTS),
            _rel("file:main.py", "file:requests.py", RelationType.IMPORTS)]
    resolved, unresolved = RelationshipResolver(ents).resolve(rels)
    assert [r.target_id for r in resolved] == ["file:pkg/__init__.py"]
    assert unresolved[0].reason == "external_package" and unresolved[0].detail == "requests"


def test_concrete_value_matches_parameterised_route():
    ents = _world()
    rels = [_rel("javascript:site/app.js:run", "api:GET:/items/7", RelationType.REQUESTS)]
    resolved, unresolved = RelationshipResolver(ents).resolve(rels)
    assert [r.target_id for r in resolved] == ["api:GET:/items/<int:item_id>"] and unresolved == []
