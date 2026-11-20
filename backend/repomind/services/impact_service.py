"""Change-impact / blast-radius analysis (Stage 5).

Idea: if A depends on B, then changing B may break A. RepositoryGraph.dependency_view()
encodes "A -> B means A depends on B", so the impacted set is everything that can
*reach* the changed entities. We run a multi-source BFS on the reversed dependency view:
BFS gives the shortest dependency chain, which doubles as the explanation.

Changing a container (a file or class) means everything it contains changes too,
so those are used as the BFS sources.

Each impacted component is classified:
  direct         depends on the changed component itself (distance 1)
  execution_flow reached only through execution edges (CALLS / REQUESTS / HANDLES / TRIGGERS)
  indirect       reached through at least one structural edge (IMPORTS, REFERENCES, STYLES, ...)
and flagged `uncertain` when a hop is inferred/heuristic or the chain confidence is below 0.8.
(Data-flow dependencies are not extracted, so they are not reported.)

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
from repomind.graph.repository_graph import FLOW_EDGES
from repomind.models import RelationType
from repomind.models.impact import ImpactedComponent, ImpactHop, ImpactReport

TEST_FILE = re.compile(r"(^|/)(tests?|__tests__|spec)/|(^|/)test_[^/]*\.py$|\.(test|spec)\.[jt]sx?$", re.I)
FLOW_TYPES = {t.value for t in FLOW_EDGES}


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


class ImpactService:
    def __init__(self, graph: RepositoryGraph):
        self.graph = graph

    # ------------------------------------------------------------------ entry points
    def resolve_target(self, target: str) -> str:
        matches = self.graph.resolve(target)
        if not matches:
            raise ImpactError(f"No entity matches '{target}'.")
        if len(matches) > 1:
            options = ", ".join(e.id for e in matches[:8])
            raise ImpactError(f"'{target}' is ambiguous ({len(matches)} matches): {options}")
        return matches[0].id

    def analyze_impact(self, target: str, max_depth: int = 10) -> ImpactReport:
        target_id = self.resolve_target(target)
        return self._analyze([target_id], target_id, max_depth)

    def entities_for_changes(self, changes: dict[str, list[tuple[int, int]]]) -> list[str]:
        """Map changed line ranges to the smallest entities that contain them
        (falling back to the file when a change is outside every entity)."""
        selected: set[str] = set()
        for file_path, ranges in changes.items():
            file_id = f"file:{file_path}"
            if not self.graph.has_node(file_id):
                continue
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
                    selected.update(e.id for e in leaves)
                else:
                    selected.add(file_id)
        return sorted(selected)

    def analyze_diff(self, diff: str, max_depth: int = 10) -> ImpactReport:
        changes = parse_unified_diff(diff)
        ids = self.entities_for_changes(changes)
        if not ids:
            raise ImpactError("The diff does not touch any analysed source file.")
        return self._analyze(ids, f"diff touching {len(ids)} component(s)", max_depth)

    # ------------------------------------------------------------------ core
    def _analyze(self, target_ids: list[str], label: str, max_depth: int) -> ImpactReport:
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
            for dependent in dep.predecessors(node):   # edge dependent -> node
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
            suggested_tests=self._suggest_tests(changed, {c.entity_id for c in components}),
        )
        report.risk, report.risk_reason = self._risk(report)
        return report

    # ------------------------------------------------------------------ helpers
    def _file_of(self, entity_id: str) -> str | None:
        e = self.graph.get_entity(entity_id)
        if e is None:
            return None
        if e.type.value == "file":
            return e.qualified_name
        return e.location.file_path if e.location else None

    def _suggest_tests(self, changed: set[str], impacted: set[str]) -> list[str]:
        """Test files worth re-running: those containing an impacted component, or importing a file
        that contains changed/impacted code. Recognised by name (tests/, test_*.py, *.test.js ...).
        Test files that hold the *changed* code itself are excluded."""
        changed_files = {f for f in (self._file_of(i) for i in changed) if f}
        involved_files = changed_files | {f for f in (self._file_of(i) for i in impacted) if f}
        tests = {f for f in involved_files if TEST_FILE.search(f)}
        for u, v, data in self.graph.g.edges(data=True):
            if data["type"] == RelationType.IMPORTS.value and self._file_of(v) in involved_files:
                src = self._file_of(u)
                if src and TEST_FILE.search(src):
                    tests.add(src)
        return sorted(tests - changed_files)

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
            "HANDLES": "is served by", "STYLES": "is styled by",
        }
        parts = []
        for h in chain:
            tag = f" [{h.kind}, {h.confidence:.2f}]" if h.kind != "detected" else ""
            parts.append(f"{self._short(h.dependent)} {phrases.get(h.relation, h.relation.lower())} "
                         f"{self._short(h.dependency)}{tag}")
        return "; ".join(parts)

    @staticmethod
    def _risk(report: ImpactReport) -> tuple[str, str]:
        """Transparent heuristic, not a trained model:
        high   - reaches a UI element or API endpoint (user-visible), or radius >= 10
        medium - radius >= 3
        low    - otherwise"""
        if report.affected_entry_points:
            return "high", f"reaches {len(report.affected_entry_points)} user-facing entry point(s)"
        if report.blast_radius >= 10:
            return "high", f"large blast radius ({report.blast_radius})"
        if report.blast_radius >= 3:
            return "medium", f"blast radius {report.blast_radius}"
        return "low", f"blast radius {report.blast_radius}"
