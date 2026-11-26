"""Relationship resolution (Phase 3).

Analyzers work on one file at a time, so they emit *candidate* links using deterministic
ids (`html:static/index.html:#login-form`, `api:POST:/api/login`). This pass connects
those candidates to the entities that really exist, across files and languages, and
never invents a connection: whatever cannot be resolved is returned as an
`UnresolvedReference` with a reason.

Rules, in order:
  1. HTML pairing   - a JS file's DOM references are resolved against the HTML page(s)
                      that include it through <script src>, not a guessed index.html.
  2. API matching   - request routes are normalised (`{id}` == `<int:id>` == `:id` ==
                      `${x}`) and matched on (method, path). A unique path match under a
                      different method is reported as a method mismatch.
  3. Packages       - `a/b.py` falls back to `a/b/__init__.py`.
  3b. Inheritance   - `Child.method` that does not exist resolves to the nearest base class that
                      defines `method` (following INHERITS edges across files), as an inferred link:
                      a subclass elsewhere could override it.
  4. Leftovers      - classified as external package or unresolved target.
"""
from __future__ import annotations

import re
from collections import defaultdict
from urllib.parse import urlsplit

from repomind.models import (
    Entity, EntityType, RelationKind, RelationType, Relationship,
)
from repomind.models.resolution import UnresolvedReference

_HTML_ID = re.compile(r"^html:(?P<path>[^:]+):(?P<sel>.+)$")
_PARAM = re.compile(r"\{[^}]*\}|<[^>]*>|\$\{[^}]*\}|:[A-Za-z_]\w*")


def normalize_route(route: str) -> str:
    """'/api/users/<int:user_id>/' , '/api/users/:id' , '/api/users/${id}' -> '/api/users/{}'"""
    path = urlsplit(route).path or route.split("?")[0].split("#")[0]
    path = _PARAM.sub("{}", path)
    if len(path) > 1:
        path = path.rstrip("/")
    return path or "/"


def routes_match(a: str, b: str) -> bool:
    """Segment-wise equality where a `{}` parameter matches any single segment, so a request
    to '/api/users/1' matches the route '/api/users/{id}' (and vice versa)."""
    sa, sb = a.strip("/").split("/"), b.strip("/").split("/")
    return len(sa) == len(sb) and all(x == y or "{}" in (x, y) for x, y in zip(sa, sb))


class RelationshipResolver:
    def __init__(self, entities: dict[str, Entity], root_name: str | None = None):
        self.entities = entities
        self.root_name = root_name          # folder name of the analysed root (it may itself be a package)
        self.py_files = {e.qualified_name for e in entities.values()
                         if e.type == EntityType.FILE and e.qualified_name.endswith(".py")}
        self.py_by_qual: dict[str, list[Entity]] = defaultdict(list)
        for e in entities.values():
            if e.language.value == "python" and e.type != EntityType.FILE and e.location:
                self.py_by_qual[e.qualified_name].append(e)
        self.file_paths = {e.qualified_name for e in entities.values() if e.type == EntityType.FILE}
        self.html_elements = [e for e in entities.values() if e.type == EntityType.HTML_ELEMENT]
        self.endpoints = [e for e in entities.values() if e.type == EntityType.API_ENDPOINT]

    # ------------------------------------------------------------------ public
    def resolve(self, relationships: list[Relationship]) -> tuple[list[Relationship], list[UnresolvedReference]]:
        includers = self._script_includers(relationships)
        self._bases = self._base_classes(relationships)
        out: dict[str, Relationship] = {}
        unresolved: list[UnresolvedReference] = []

        def keep(rel: Relationship) -> None:
            prev = out.get(rel.key)
            if prev is None or rel.confidence > prev.confidence:
                out[rel.key] = rel

        for rel in relationships:
            if rel.source_id in self.entities and rel.target_id in self.entities:
                keep(rel)
                continue
            fixed = (
                self._html_pairing(rel, includers)
                or self._api_match(rel, unresolved)
                or self._package_fallback(rel)
            )
            if fixed:
                for r in fixed:
                    keep(r)
            elif not any(u.source_id == rel.source_id and u.target_id == rel.target_id and u.type == rel.type.value
                         for u in unresolved):
                unresolved.append(self._classify(rel))
        return list(out.values()), unresolved

    # ------------------------------------------------------------------ rule 1: HTML pairing
    def _script_includers(self, relationships: list[Relationship]) -> dict[str, list[str]]:
        """js path -> [html paths that include it], from the HTML analyzer's REFERENCES edges."""
        inc: dict[str, list[str]] = defaultdict(list)
        for r in relationships:
            if (r.type == RelationType.REFERENCES and r.source_id.startswith("file:")
                    and r.source_id.lower().endswith((".html", ".htm")) and r.target_id.startswith("file:")):
                inc[r.target_id.removeprefix("file:")].append(r.source_id.removeprefix("file:"))
        # A module imported by an included script runs on those pages too (transitively).
        imports = [(r.source_id.removeprefix("file:"), r.target_id.removeprefix("file:")) for r in relationships
                   if r.type == RelationType.IMPORTS and r.source_id.endswith((".js", ".mjs", ".jsx"))]
        changed = True
        while changed:
            changed = False
            for importer, imported in imports:
                for page in list(inc.get(importer, [])):
                    if page not in inc[imported]:
                        inc[imported].append(page)
                        changed = True
        return inc

    @staticmethod
    def _owner_path(entity_id: str) -> str | None:
        if entity_id.startswith("file:"):
            return entity_id.removeprefix("file:")
        if entity_id.startswith("javascript:"):
            return entity_id.split(":", 2)[1]
        return None

    def _html_pairing(self, rel: Relationship, includers: dict[str, list[str]]) -> list[Relationship] | None:
        # the dangling endpoint must be an html element id; the other end identifies the JS file
        for dangling_is_target in (True, False):
            dangling = rel.target_id if dangling_is_target else rel.source_id
            other = rel.source_id if dangling_is_target else rel.target_id
            m = _HTML_ID.match(dangling)
            if not m or dangling in self.entities or other not in self.entities:
                continue
            js_path = self._owner_path(other)
            pages = includers.get(js_path or "", [])
            selector = m["sel"]
            matches = self._find_elements(pages, selector)
            if not matches:
                return None
            exact = len(matches) == 1 and selector.startswith("#")
            kind = rel.kind if exact else RelationKind.HEURISTIC
            conf = rel.confidence if exact else min(rel.confidence, 0.6 if selector.startswith("#") else 0.7)
            fixed = []
            for el in matches:
                src, tgt = (other, el.id) if dangling_is_target else (el.id, other)
                fixed.append(rel.model_copy(update={"source_id": src, "target_id": tgt,
                                                    "kind": kind, "confidence": conf}))
            return fixed
        return None

    def _find_elements(self, pages: list[str], selector: str) -> list[Entity]:
        hits = []
        for el in self.html_elements:
            page = el.id.split(":", 2)[1]
            if page not in pages:
                continue
            if selector.startswith("#") and el.metadata.get("id") == selector[1:]:
                hits.append(el)
            elif selector.startswith(".") and selector[1:] in el.metadata.get("classes", []):
                hits.append(el)
        return hits

    # ------------------------------------------------------------------ rule 2: API matching
    def _api_match(self, rel: Relationship, unresolved: list[UnresolvedReference]) -> list[Relationship] | None:
        if rel.type != RelationType.REQUESTS or not rel.target_id.startswith("api:"):
            return None
        _, method, route = rel.target_id.split(":", 2)
        wanted = normalize_route(route)
        same_path = [e for e in self.endpoints if routes_match(normalize_route(e.metadata.get("route", "")), wanted)]
        exact = [e for e in same_path if e.metadata.get("method") == method]
        if exact:
            heur = len(exact) > 1
            return [rel.model_copy(update={
                "target_id": e.id,
                "kind": RelationKind.HEURISTIC if heur else rel.kind,
                "confidence": min(rel.confidence, 0.6) if heur else rel.confidence,
            }) for e in exact]
        if same_path:
            have = ", ".join(sorted({e.metadata.get("method", "?") for e in same_path}))
            unresolved.append(self._classify(rel, "method_mismatch",
                                             f"request uses {method} but the backend defines {have} for this path"))
            return []        # handled: reported, do not classify again
        # Backends are often mounted under a prefix (APIRouter(prefix="/api")): segment-wise suffix match
        suffix = [e for e in self.endpoints if e.metadata.get("method") == method
                  and (normalize_route(e.metadata.get("route", "")).endswith(wanted)
                       or wanted.endswith(normalize_route(e.metadata.get("route", ""))))
                  and wanted != "/"]
        if len(suffix) == 1:
            return [rel.model_copy(update={"target_id": suffix[0].id, "kind": RelationKind.HEURISTIC,
                                           "confidence": min(rel.confidence, 0.5)})]
        unresolved.append(self._classify(rel, "no_matching_endpoint",
                                         f"no backend route matches {method} {route}"))
        return []

    # ------------------------------------------------------------------ rule 3: python modules / packages
    def _module_files(self, mod: str) -> list[str]:
        """Repo files a dotted module path (as 'a/b') can refer to, most specific rule first:
        exact path, package __init__, src-layout suffix ('src/a/b.py'), and - only when the analysed
        folder is itself the package (first segment == folder name) - the path without that segment.
        Never drops arbitrary leading segments: that would match `werkzeug.utils` to your own utils.py."""
        variants = [mod]
        if self.root_name and mod.split("/")[0] == self.root_name and "/" in mod:
            variants.append(mod.split("/", 1)[1])
        for sub in variants:
            exact = [f for f in (f"{sub}.py", f"{sub}/__init__.py") if f in self.py_files]
            if exact:
                return exact
            suffix = sorted(f for f in self.py_files
                            if f.endswith((f"/{sub}.py", f"/{sub}/__init__.py")))
            if suffix:
                return suffix
        return []

    def _fix_python_id(self, eid: str) -> tuple[str | None, bool]:
        """-> (existing entity id, changed_by_reexport_search). None if it cannot be resolved."""
        if eid in self.entities:
            return eid, False
        m = re.match(r"^(?P<kind>file|python):(?P<mod>[^:]+?)\.py(?::(?P<qual>.*))?$", eid)
        if not m:
            return None, False
        for module_file in self._module_files(m["mod"]):
            if m["kind"] == "file":
                return f"file:{module_file}", False
            direct = f"python:{module_file}:{m['qual']}"
            if direct in self.entities:
                return direct, False
        if m["kind"] == "python":
            # Re-export: `from pkg import Name` where pkg/__init__.py only re-exports Name from a submodule.
            for module_file in self._module_files(m["mod"]):
                if module_file.endswith("__init__.py"):
                    package_dir = module_file.rsplit("/", 1)[0] + "/" if "/" in module_file else ""
                    hits = [e for e in self.py_by_qual.get(m["qual"], []) if e.location.file_path.startswith(package_dir)]
                    if len(hits) == 1:
                        return hits[0].id, True
        return None, False

    def _base_classes(self, relationships: list[Relationship]) -> dict[str, list[str]]:
        """class entity id -> ids of its (resolved) base classes, from INHERITS edges."""
        bases: dict[str, list[str]] = defaultdict(list)
        for r in relationships:
            if r.type != RelationType.INHERITS:
                continue
            src, _ = self._fix_python_id(r.source_id)
            tgt, _ = self._fix_python_id(r.target_id)
            if src and tgt and tgt not in bases[src]:
                bases[src].append(tgt)
        return bases

    def _inherited_member(self, eid: str) -> str | None:
        """`python:a.py:Child.method` -> `python:b.py:Base.method` if Child (transitively) inherits it."""
        m = re.match(r"^python:(?P<mod>[^:]+?)\.py:(?P<cls>.+)\.(?P<member>[^.]+)$", eid)
        if not m:
            return None
        cls_id, _ = self._fix_python_id(f"python:{m['mod']}.py:{m['cls']}")
        queue, seen = list(self._bases.get(cls_id or "", [])), set()
        while queue:
            base = queue.pop(0)
            if base in seen:
                continue
            seen.add(base)
            if f"{base}.{m['member']}" in self.entities:
                return f"{base}.{m['member']}"
            queue.extend(self._bases.get(base, []))
        return None

    def _package_fallback(self, rel: Relationship) -> list[Relationship] | None:
        s, s_re = self._fix_python_id(rel.source_id)
        t, t_re = self._fix_python_id(rel.target_id)
        inherited = False
        if s and not t:
            t = self._inherited_member(rel.target_id)
            inherited = t is not None
        if not (s and t):
            return None
        update: dict = {"source_id": s, "target_id": t}
        if s_re or t_re:                        # followed a re-export by name: a little less certain
            update["kind"] = RelationKind.INFERRED if rel.kind == RelationKind.DETECTED else rel.kind
            update["confidence"] = min(rel.confidence, 0.85)
        if inherited:                           # a subclass of the receiver's class could override it
            update["kind"] = RelationKind.INFERRED if rel.kind == RelationKind.DETECTED else rel.kind
            update["confidence"] = min(rel.confidence, 0.85)
        return [rel.model_copy(update=update)]

    # ------------------------------------------------------------------ rule 4: classification
    def _top_level_in_repo(self, name: str) -> bool:
        if name == self.root_name:
            return True                         # our own package: failing to find it is a gap, not "external"
        return f"{name}.py" in self.file_paths or any(p.startswith(f"{name}/") for p in self.file_paths)

    def _classify(self, rel: Relationship, reason: str | None = None, detail: str = "") -> UnresolvedReference:
        if reason is None:
            reason = "unresolved_target"
            m = re.match(r"^(?:file|python):(?P<p>[^:]+?)\.py", rel.target_id)
            if m and not self._top_level_in_repo(m["p"].split("/")[0]):
                reason, detail = "external_package", m["p"].replace("/", ".")
        ev = rel.evidence[0] if rel.evidence else None
        return UnresolvedReference(
            source_id=rel.source_id, target_id=rel.target_id, type=rel.type.value, reason=reason,
            detail=detail, snippet=(ev.snippet[:200] if ev else ""), location=ev.location if ev else None)
