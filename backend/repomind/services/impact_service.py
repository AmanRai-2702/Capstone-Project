"""Change-impact / blast-radius analysis (Stage 5).

Idea: if A depends on B, then changing B may break A. RepositoryGraph.dependency_view()
encodes "A -> B means A depends on B", so the impacted set is everything that can
*reach* the changed entities. We run a multi-source BFS on the reversed dependency view:
BFS gives the shortest dependency chain, which doubles as the explanation.

Changing a container (a file or class) means everything it contains changes too,
so those are used as the BFS sources.

In strict mode (off by default) impact does not flow through a REFERENCES edge into the referrer of an HTML
element that was itself reached only through its handler (identity vs behaviour, see _analyze). The default
reports those referrers as "indirect", which over-approximates but never misses a structural dependent.

Each impacted component is classified:
  direct         depends on the changed component itself (distance 1)
  execution_flow reached only through execution edges (CALLS / REQUESTS / HANDLES / TRIGGERS)
  data_flow      reached through execution edges and at least one DATA edge: a value handed on
                 (DATA_FLOW), a returned object (RETURNS) or shared `self.attr` state (READS_STATE)
  indirect       reached through at least one structural edge (IMPORTS, REFERENCES, STYLES, ...)
and flagged `uncertain` when a hop is inferred/heuristic or the chain confidence is below 0.8.
(Data flow is extracted for Python only: values passed between calls, returned objects and
shared `self.attr` state; JavaScript data flow is not tracked.)

Inputs: a single entity, or a unified diff (`analyze_diff`) which is mapped to the entities
whose source lines were touched.

Limits (say this in the viva): the analysis is only as complete as the extracted
relationships; unresolved dynamic calls are invisible, and confidence is the product
along one shortest chain, not a probabilistic model.
"""
from __future__ import annotations

import re
from collections import deque

from repomind.graph import RepositoryGraph
from repomind.graph.repository_graph import CONSTRUCTS, FLOW_EDGES
from repomind.models import RelationType
from repomind.models.impact import ImpactedComponent, ImpactHop, ImpactReport, RiskFactor, TestSuggestion

TEST_FILE = re.compile(r"(^|/)(tests?|__tests__|spec)/|(^|/)test_[^/]*\.py$|\.(test|spec)\.[jt]sx?$", re.I)
FLOW_TYPES = {t.value for t in FLOW_EDGES} | {CONSTRUCTS}
# Edges along which DATA (not control) travels: a value, a returned object or shared `self.attr` state.
DATA_TYPES = {"DATA_FLOW", "READS_STATE", "RETURNS"}


class ImpactError(ValueError):
    """Target could not be resolved to exactly one entity."""


def parse_unified_diff(diff: str) -> dict[str, list[tuple[int, int]]]:
    """Unified diff text -> {file: [(start_line, end_line), ...]} in the NEW file's numbering.
    A pure deletion is attributed to the line where it happened. Deleted files are skipped
    (their entities no longer exist in the analysed tree)."""
    changes: dict[str, list[tuple[int, int]]] = {}
    current: str | None = None
    for line in diff.splitlines():
        if line.startswith("+++ "):
            path = line[4:].split("\t")[0].strip()
            current = None if path == "/dev/null" else (path[2:] if path.startswith("b/") else path)
        elif line.startswith("@@") and current:
            m = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", line)
            if m:
                start, count = int(m[1]), int(m[2]) if m[2] is not None else 1
                changes.setdefault(current, []).append((max(start, 1), max(start, 1) + max(count, 1) - 1))
    return changes


def parse_diff_files(diff: str) -> tuple[dict[str, dict[str, list[str]]], set[str]]:
    """({file: {"added": [lines], "removed": [lines]}}, deleted files) from unified diff text."""
    lines_by_file: dict[str, dict[str, list[str]]] = {}
    deleted: set[str] = set()
    old = new = None
    for line in diff.splitlines():
        if line.startswith("--- "):
            path = line[4:].split("\t")[0].strip()
            old = None if path == "/dev/null" else (path[2:] if path.startswith("a/") else path)
        elif line.startswith("+++ "):
            path = line[4:].split("\t")[0].strip()
            new = None if path == "/dev/null" else (path[2:] if path.startswith("b/") else path)
            if new is None and old:
                deleted.add(old)
        elif line.startswith(("+", "-")) and (new or old):
            bucket = lines_by_file.setdefault(new or old, {"added": [], "removed": []})
            bucket["added" if line[0] == "+" else "removed"].append(line[1:])
    return lines_by_file, deleted


_REQ_LINE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")
_PKG_JSON_LINE = re.compile(r'"([^"]+)"\s*:\s*"[^"]*"')


def changed_packages(path: str, added: list[str], removed: list[str]) -> set[str]:
    """Normalised package names whose lines changed in requirements*.txt / package.json."""
    names: set[str] = set()
    name = path.rsplit("/", 1)[-1].lower()
    for line in [*added, *removed]:
        if name.startswith("requirements") and name.endswith(".txt"):
            m = _REQ_LINE.match(line) if not line.lstrip().startswith(("#", "-")) else None
        elif name == "package.json":
            m = _PKG_JSON_LINE.search(line)
        else:
            m = None
        if m:
            names.add(m.group(1).lower().replace("-", "_").replace(".", "_"))
    return names


class ImpactService:
    def __init__(self, graph: RepositoryGraph, unresolved: list | None = None, strict_references: bool = False):
        self.graph = graph
        self.strict_references = strict_references
        # What the analysis could not connect (external packages, deleted files...). Needed to map a changed
        # dependency or a deleted file to the code that used it.
        self.unresolved = list(unresolved or [])

    # ------------------------------------------------------------------ entry points
    def resolve_target(self, target: str) -> str:
        matches = self.graph.resolve(target)
        if not matches:
            raise ImpactError(f"No entity matches '{target}'.")
        if len(matches) > 1:
            options = ", ".join(e.id for e in matches[:8])
            raise ImpactError(f"'{target}' is ambiguous ({len(matches)} matches): {options}")
        return matches[0].id

    def analyze_impact(self, target: str, max_depth: int = 10, strict: bool | None = None) -> ImpactReport:
        target_id = self.resolve_target(target)
        return self._analyze([target_id], target_id, max_depth, strict=strict)

    def entities_for_changes(self, changes: dict[str, list[tuple[int, int]]]) -> list[str]:
        """Map changed line ranges to the smallest entities that contain them
        (falling back to the file when a change is outside every entity)."""
        return sorted(self.classify_changes(changes))

    def classify_changes(self, changes: dict[str, list[tuple[int, int]]]) -> dict[str, str]:
        """entity id -> "signature" | "body" | "file". A change that touches an entity's first line (its
        `def`/`class`/`function` line) is a signature change: callers may no longer fit. Anything else inside
        it is a body change: behaviour may differ but the interface is intact."""
        kinds: dict[str, str] = {}
        selected: set[str] = set()
        for file_path, ranges in changes.items():
            file_id = f"file:{file_path}"
            file_entity = self.graph.get_entity(file_id)
            if file_entity is None or file_entity.metadata.get("category") != "source":
                continue                                    # docs and config files hold no code that can break
            inside = [e for e in self.graph.entities.values()
                      if e.location and e.location.file_path == file_path and e.type.value != "file"]
            for start, end in ranges:
                hits = [e for e in inside if e.location.start_line <= end and e.location.end_line >= start]
                # keep only leaves: drop an entity if a smaller selected entity lies inside it
                leaves = [e for e in hits if not any(
                    o is not e and e.location.start_line <= o.location.start_line
                    and o.location.end_line <= e.location.end_line and o.id != e.id
                    and (o.location.end_line - o.location.start_line) < (e.location.end_line - e.location.start_line)
                    for o in hits)]
                if leaves:
                    for e in leaves:
                        touches_head = e.location.start_line >= start and e.location.start_line <= end
                        if touches_head and e.type.value in ("function", "method", "class"):
                            kinds[e.id] = "signature"
                        else:
                            kinds.setdefault(e.id, "body")
                else:
                    kinds.setdefault(file_id, "file")
        return kinds

    def analyze_diff(self, diff: str, max_depth: int = 10) -> ImpactReport:
        changes = parse_unified_diff(diff)
        kinds = self.classify_changes(changes)
        notes: list[str] = []
        files, deleted = parse_diff_files(diff)

        # A changed dependency (requirements.txt / package.json): the files that import it may break.
        for path, lines in files.items():
            pkgs = changed_packages(path, lines["added"], lines["removed"])
            if not pkgs:
                continue
            users = {self._file_id_of(u.source_id) for u in self.unresolved if u.reason == "external_package"
                     and u.detail and u.detail.split(".")[0].lower().replace("-", "_") in pkgs}
            users.discard(None)
            for f in users:
                kinds.setdefault(f, "dependency")
            notes.append(f"{path}: package(s) {', '.join(sorted(pkgs))} changed; "
                         + (f"{len(users)} file(s) import them and are treated as changed."
                            if users else "no analysed file imports them (JavaScript packages are not tracked)."))
        # A deleted file: whatever still imports it is now broken.
        for path in deleted:
            module = re.sub(r"(/__init__)?\.py$", "", path).replace("/", ".")
            # the importers of a missing module look like either an unmatched file target or an "external"
            # package of that name (the resolver cannot tell a deleted module from an absent library)
            users = {self._file_id_of(u.source_id) for u in self.unresolved
                     if (u.reason == "unresolved_target" and self._target_path(u.target_id) == path)
                     or (u.reason == "external_package" and u.detail and (u.detail == module or u.detail.startswith(module + ".")))}
            users.discard(None)
            for f in users:
                kinds.setdefault(f, "deleted_file")
            notes.append(f"{path} was deleted; " + (f"{len(users)} file(s) still reference it and are treated as changed."
                                                    if users else "nothing in the analysed tree references it."))
        ids = sorted(kinds)
        if not ids:
            raise ImpactError("The diff does not touch any analysed source file." + (" " + " ".join(notes) if notes else ""))
        return self._analyze(ids, f"diff touching {len(ids)} component(s)", max_depth, kinds, notes)

    # ------------------------------------------------------------------ core
    def _file_id_of(self, entity_id: str) -> str | None:
        path = self._file_of(entity_id)
        return f"file:{path}" if path and self.graph.has_node(f"file:{path}") else None

    @staticmethod
    def _target_path(target_id: str) -> str | None:
        m = re.match(r"^(?:file|python|javascript):(?P<p>[^:]+\.\w+)", target_id)
        return m["p"] if m else None

    def _analyze(self, target_ids: list[str], label: str, max_depth: int, kinds: dict[str, str] | None = None,
                 notes: list[str] | None = None, strict: bool | None = None) -> ImpactReport:
        strict = self.strict_references if strict is None else strict
        changed: set[str] = set()
        for t in target_ids:
            changed |= {t} | self.graph.descendants(t)
        dep = self.graph.dependency_view()

        # parent[x] = the node x depends on (one step closer to the change)
        dist: dict[str, int] = {c: 0 for c in changed}
        parent: dict[str, str] = {}
        queue = deque(sorted(changed))
        while queue:
            node = queue.popleft()
            if dist[node] >= max_depth:
                continue
            behavioural_only = strict and dist[node] > 0 and self.graph.entities[node].type.value == "html_element"
            for dependent in dep.predecessors(node):   # edge dependent -> node
                # Strict mode only. A REFERENCES edge to an HTML element is an *identity* dependency: the referrer
                # needs the element to exist. If just what the element DOES changed (it was reached through its
                # handler), the referrer is not affected, so impact stops there. A change to the element itself
                # (distance 0) still reaches every referrer. The default keeps the original, broader behaviour.
                if behavioural_only and dep.edges[dependent, node]["type"] == "REFERENCES":
                    continue
                if dependent not in dist:
                    dist[dependent] = dist[node] + 1
                    parent[dependent] = node
                    queue.append(dependent)

        components = [self._component(n, dist[n], parent, dep) for n in dist if n not in changed]
        components.sort(key=lambda c: (c.distance, -c.confidence, c.entity_id))

        direct = [c for c in components if c.distance == 1]
        transitive = [c for c in components if c.distance > 1]
        by_language: dict[str, int] = {}
        by_type: dict[str, int] = {}
        by_category: dict[str, int] = {}
        for c in components:
            by_language[c.language] = by_language.get(c.language, 0) + 1
            by_type[c.type] = by_type.get(c.type, 0) + 1
            by_category[c.category] = by_category.get(c.category, 0) + 1

        entry_ids = {c.entity_id for c in components if c.type in ("html_element", "api_endpoint")}
        report = ImpactReport(
            target=label, changed=sorted(changed), direct=direct, transitive=transitive,
            blast_radius=len(components), by_language=by_language, by_type=by_type,
            by_category=by_category, affected_entry_points=sorted(entry_ids),
        )
        details = self._rank_tests(changed, {c.entity_id for c in components})
        report.suggested_test_details = details
        report.suggested_tests = [d.file for d in details]
        report.change_kinds = dict(kinds or {t: "target" for t in target_ids})
        report.notes = list(notes or [])
        report.risk, report.risk_reason, report.risk_factors = self._assess_risk(report, components)
        return report

    # ------------------------------------------------------------------ helpers
    def _file_of(self, entity_id: str) -> str | None:
        e = self.graph.get_entity(entity_id)
        if e is None:
            return None
        if e.type.value == "file":
            return e.qualified_name
        return e.location.file_path if e.location else None

    def _rank_tests(self, changed: set[str], impacted: set[str]) -> list[TestSuggestion]:
        """Test files worth re-running, most relevant first. A test file qualifies when it holds an impacted
        component, imports a file with changed/impacted code, or its code CALLS / USES / REQUESTS a
        changed or impacted component. Test files holding the *changed* code itself are excluded.
        Score = 2 per distinct impacted/changed component its code reaches directly, +1 for each import of
        an involved file; reasons list what it reaches. (Recognised by name: tests/, test_*.py, *.test.js.)"""
        changed_files = {f for f in (self._file_of(i) for i in changed) if f}
        involved_ids = changed | impacted
        involved_files = changed_files | {f for f in (self._file_of(i) for i in impacted) if f}
        reasons: dict[str, list[str]] = {}
        scores: dict[str, int] = {}

        def add(test: str, points: int, reason: str) -> None:
            if reason not in reasons.setdefault(test, []):
                reasons[test].append(reason)
                scores[test] = scores.get(test, 0) + points

        for f in involved_files:
            if TEST_FILE.search(f):
                add(f, 1, "contains an impacted component")
        for u, v, data in self.graph.g.edges(data=True):
            src_file = self._file_of(u)
            if not src_file or not TEST_FILE.search(src_file):
                continue
            if data["type"] == RelationType.IMPORTS.value and self._file_of(v) in involved_files:
                add(src_file, 1, f"imports {self._file_of(v)}")
            elif data["type"] in ("CALLS", "USES", "REQUESTS", "DATA_FLOW") and v in involved_ids:
                add(src_file, 2, f"{data['type'].lower()} {self._short(v)}")
        return [TestSuggestion(file=f, score=scores[f], reasons=reasons[f])
                for f in sorted(scores, key=lambda f: (-scores[f], f)) if f not in changed_files]

    def _suggest_tests(self, changed: set[str], impacted: set[str]) -> list[str]:
        return [t.file for t in self._rank_tests(changed, impacted)]

    def _component(self, node: str, distance: int, parent: dict[str, str], dep) -> ImpactedComponent:
        chain: list[ImpactHop] = []
        cur = node
        while cur in parent:
            nxt = parent[cur]
            data = dep.get_edge_data(cur, nxt)
            ev = data["evidence"][0] if data.get("evidence") else None
            loc = None
            if ev and ev.get("location"):
                l = ev["location"]
                loc = f"{l['file_path']}:{l['start_line']}-{l['end_line']}"
            chain.append(ImpactHop(
                dependent=cur, dependency=nxt, relation=data["type"], kind=data["kind"],
                confidence=data["confidence"], evidence=(ev or {}).get("snippet", ""), location=loc,
            ))
            cur = nxt
        confidence = 1.0
        for hop in chain:
            confidence *= hop.confidence
        if distance == 1:
            category = "direct"
        elif all(h.relation in FLOW_TYPES for h in chain):
            category = "execution_flow"
        elif all(h.relation in FLOW_TYPES | DATA_TYPES for h in chain):
            category = "data_flow"          # reached through execution edges AND at least one data edge
        else:
            category = "indirect"
        entity = self.graph.get_entity(node)
        return ImpactedComponent(
            entity_id=node, name=entity.qualified_name or entity.name, type=entity.type.value,
            language=entity.language.value,
            location=str(entity.location) if entity and entity.location else None,
            distance=distance, category=category,
            uncertain=confidence < 0.8 or any(h.kind != "detected" for h in chain),
            confidence=round(confidence, 4),
            has_inferred_hop=any(h.kind != "detected" for h in chain),
            chain=chain, explanation=self._explain(chain),
        )

    def _short(self, node_id: str) -> str:
        e = self.graph.get_entity(node_id)
        return e.qualified_name if e else node_id

    def _explain(self, chain: list[ImpactHop]) -> str:
        phrases = {
            "CALLS": "calls", "REQUESTS": "sends a request to", "TRIGGERS": "triggers",
            "REFERENCES": "references", "IMPORTS": "imports", "INHERITS": "inherits from", "USES": "uses",
            "HANDLES": "is served by", "STYLES": "is styled by", CONSTRUCTS: "runs the constructor of",
            "RETURNS": "returns an instance of", "DATA_FLOW": "is given data produced by",
            "READS_STATE": "reads state written by",
        }
        parts = []
        for h in chain:
            tag = f" [{h.kind}, {h.confidence:.2f}]" if h.kind != "detected" else ""
            parts.append(f"{self._short(h.dependent)} {phrases.get(h.relation, h.relation.lower())} "
                         f"{self._short(h.dependency)}{tag}")
        return "; ".join(parts)

    def _assess_risk(self, report: ImpactReport, components: list[ImpactedComponent]):
        """Transparent heuristic, not a trained model, and its confidence figures are not probabilities.

        Level:  high    - reaches a UI element or API endpoint (user-visible), or radius >= 10, or a signature
                          change with >= 3 direct dependents
                medium  - radius >= 3, or a signature change with any dependent
                low     - otherwise
        Every level comes with the factors that explain it, each with source locations as evidence."""
        factors: list[RiskFactor] = []

        def loc(ids, limit=4):
            out: list[str] = []
            for i in ids:
                e = self.graph.get_entity(i)
                where = f"{e.location.file_path}:{e.location.start_line}-{e.location.end_line}" if e and e.location else None
                if where and where not in out:
                    out.append(where)
            return out[:limit]

        direct = len(report.direct)
        signature = [i for i, k in report.change_kinds.items() if k == "signature"]
        if report.affected_entry_points:
            names = ", ".join(self._short(i) for i in report.affected_entry_points[:3])
            factors.append(RiskFactor(factor="entry_points", severity="high",
                                      detail=f"reaches {len(report.affected_entry_points)} user-facing entry point(s): {names}",
                                      evidence=loc(report.affected_entry_points)))
        if signature and direct:
            factors.append(RiskFactor(
                factor="signature_change", severity="high" if direct >= 3 else "medium",
                detail=f"the first line (signature) of {', '.join(self._short(i) for i in signature[:3])} is edited and "
                       f"{direct} component(s) depend on it directly, so call sites may no longer fit",
                evidence=loc(signature)))
        if report.blast_radius >= 10:
            factors.append(RiskFactor(factor="blast_radius", severity="high",
                                      detail=f"large blast radius ({report.blast_radius} components)"))
        elif report.blast_radius >= 3:
            factors.append(RiskFactor(factor="blast_radius", severity="medium",
                                      detail=f"blast radius {report.blast_radius}"))
        data = [c for c in components if c.category == "data_flow"]
        if data:
            factors.append(RiskFactor(factor="data_flow", severity="info",
                                      detail=f"{len(data)} component(s) are affected through data they receive or "
                                             "shared state, which is easy to miss when reading call sites",
                                      evidence=loc([c.entity_id for c in data])))
        uncertain = [c for c in components if c.uncertain]
        if components and len(uncertain) * 2 > len(components):
            factors.append(RiskFactor(factor="uncertain_links", severity="info",
                                      detail=f"{len(uncertain)} of {len(components)} impacts rest on inferred links: "
                                             "verify them before relying on the list"))
        languages = ({c.language for c in components}
                     | {self.graph.get_entity(i).language.value for i in report.changed if self.graph.get_entity(i)})
        if len(languages - {"unknown"}) > 1:
            factors.append(RiskFactor(factor="cross_language", severity="info",
                                      detail="the change crosses a language boundary (e.g. Python backend <-> JavaScript client)"))
        if report.blast_radius and not report.suggested_tests:
            factors.append(RiskFactor(factor="no_tests", severity="medium",
                                      detail="no test file reaches the changed or impacted code"))

        high = any(f.severity == "high" for f in factors)
        medium = any(f.severity == "medium" for f in factors)
        level = "high" if high else "medium" if medium else "low"
        ranked = [f for f in factors if f.severity != "info"] or factors
        if level == "low" and not factors:
            reason = f"blast radius {report.blast_radius}"
        else:
            reason = "; ".join(f.detail for f in sorted(ranked, key=lambda f: f.severity != "high")[:2]) \
                if ranked else f"blast radius {report.blast_radius}"
        return level, reason, factors

    @staticmethod
    def _risk(report: ImpactReport) -> tuple[str, str]:
        """Kept for callers of the old API: the entry-point / radius rule only."""
        if report.affected_entry_points:
            return "high", f"reaches {len(report.affected_entry_points)} user-facing entry point(s)"
        if report.blast_radius >= 10:
            return "high", f"large blast radius ({report.blast_radius})"
        if report.blast_radius >= 3:
            return "medium", f"blast radius {report.blast_radius}"
        return "low", f"blast radius {report.blast_radius}"
