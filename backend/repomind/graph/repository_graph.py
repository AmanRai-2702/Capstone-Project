"""Knowledge graph over a RepositoryAnalysis (Stage 4).

The graph stores every entity as a node and every relationship as a directed edge,
exactly as the analyzers emitted them ("raw" view). Raw directions are *syntactic*
(who mentions whom), not *semantic*, so two derived views are provided:

  * flow view        - direction of execution:  html -TRIGGERS-> js -REQUESTS-> api
                       -(HANDLES reversed)-> python handler -CALLS-> ...
  * dependency view  - "A depends on B": used by impact analysis (reverse BFS).

Both views are built from the small tables below, so the semantics are explicit and testable.
"""
from __future__ import annotations

from collections import deque
from itertools import islice
from typing import Iterable

import networkx as nx

from repomind.models import Entity, RelationType, RepositoryAnalysis

# --- Flow view: which edges carry execution, and in which direction ------------------
# value True = reversed: execution goes target -> source.
FLOW_EDGES: dict[RelationType, bool] = {
    RelationType.TRIGGERS: False,   # html element  -> js handler
    RelationType.REQUESTS: False,   # js function   -> api endpoint
    RelationType.HANDLES: True,     # api endpoint  -> python handler (HANDLES points back)
    RelationType.CALLS: False,      # caller        -> callee
}

# --- Dependency view: edge A -> B means "A depends on B" ------------------------------
# value True = reversed: the *target* depends on the *source*.
DEPENDENCY_EDGES: dict[RelationType, bool] = {
    RelationType.CALLS: False,       # caller depends on callee
    RelationType.REQUESTS: False,    # client depends on endpoint
    RelationType.HANDLES: True,      # endpoint's behaviour depends on its handler
    RelationType.TRIGGERS: False,    # element behaviour depends on its handler
    RelationType.REFERENCES: False,  # referrer depends on referenced element/file
    RelationType.IMPORTS: False,     # importer depends on imported file
    RelationType.INHERITS: False,    # subclass depends on base class
    RelationType.USES: False,        # user depends on the type it uses
    RelationType.STYLES: True,       # element's appearance depends on the CSS rule
}


class RepositoryGraph:
    def __init__(self) -> None:
        self.g: nx.MultiDiGraph = nx.MultiDiGraph()
        self.entities: dict[str, Entity] = {}
        self.repo_name: str = ""
        self._views: dict[str, nx.DiGraph] = {}

    # ------------------------------------------------------------------ building
    @classmethod
    def from_analysis(cls, analysis: RepositoryAnalysis) -> "RepositoryGraph":
        graph = cls()
        graph.repo_name = analysis.name
        for e in analysis.entities:
            graph.entities[e.id] = e
            graph.g.add_node(
                e.id, type=e.type.value, name=e.name, qualified_name=e.qualified_name,
                language=e.language.value,
            )
        for r in analysis.relationships:
            if r.source_id not in graph.entities or r.target_id not in graph.entities:
                continue  # AnalysisService already drops these; guard anyway
            graph.g.add_edge(
                r.source_id, r.target_id, key=r.type.value,
                type=r.type.value, kind=r.kind.value, confidence=r.confidence,
                evidence=[ev.model_dump() for ev in r.evidence],
            )
        return graph

    # ------------------------------------------------------------------ lookup
    def __len__(self) -> int:
        return self.g.number_of_nodes()

    def has_node(self, node_id: str) -> bool:
        return node_id in self.entities

    def get_entity(self, node_id: str) -> Entity | None:
        return self.entities.get(node_id)

    def get_node(self, node_id: str) -> dict | None:
        return dict(self.g.nodes[node_id]) if node_id in self.g else None

    def resolve(self, query: str) -> list[Entity]:
        """Find entities from a user-supplied string. Preference order:
        exact id > exact qualified_name > exact name > case-insensitive substring of id."""
        if query in self.entities:
            return [self.entities[query]]
        for attr in ("qualified_name", "name"):
            hits = [e for e in self.entities.values() if getattr(e, attr) == query]
            if hits:
                return sorted(hits, key=lambda e: e.id)
        q = query.lower()
        return sorted((e for e in self.entities.values() if q in e.id.lower()), key=lambda e: e.id)

    # ------------------------------------------------------------------ neighbours
    def outgoing(self, node_id: str, rel_types: Iterable[RelationType] | None = None) -> list[dict]:
        wanted = {t.value for t in rel_types} if rel_types else None
        return [
            {"source": u, "target": v, **data}
            for u, v, data in self.g.out_edges(node_id, data=True)
            if wanted is None or data["type"] in wanted
        ]

    def incoming(self, node_id: str, rel_types: Iterable[RelationType] | None = None) -> list[dict]:
        wanted = {t.value for t in rel_types} if rel_types else None
        return [
            {"source": u, "target": v, **data}
            for u, v, data in self.g.in_edges(node_id, data=True)
            if wanted is None or data["type"] in wanted
        ]

    def get_successors(self, node_id: str) -> list[str]:
        return sorted(set(self.g.successors(node_id)))

    def get_predecessors(self, node_id: str) -> list[str]:
        return sorted(set(self.g.predecessors(node_id)))

    def children(self, node_id: str) -> list[str]:
        """Direct CONTAINS children (methods of a class, entities of a file)."""
        return sorted(e["target"] for e in self.outgoing(node_id, [RelationType.CONTAINS]))

    def descendants(self, node_id: str) -> set[str]:
        """Everything transitively contained in node_id."""
        seen: set[str] = set()
        queue = deque([node_id])
        while queue:
            for child in self.children(queue.popleft()):
                if child not in seen:
                    seen.add(child)
                    queue.append(child)
        return seen

    # ------------------------------------------------------------------ derived views
    def _derive(self, table: dict[RelationType, bool]) -> nx.DiGraph:
        """Collapse the multigraph into a DiGraph according to a direction table.
        Per (a,b) the highest-confidence edge is kept."""
        d = nx.DiGraph()
        d.add_nodes_from(self.g.nodes)
        for u, v, data in self.g.edges(data=True):
            rtype = RelationType(data["type"])
            if rtype not in table:
                continue
            a, b = (v, u) if table[rtype] else (u, v)
            prev = d.get_edge_data(a, b)
            if prev is None or data["confidence"] > prev["confidence"]:
                d.add_edge(a, b, type=data["type"], kind=data["kind"],
                           confidence=data["confidence"], evidence=data["evidence"])
        return d

    def flow_view(self) -> nx.DiGraph:
        if "flow" not in self._views:
            self._views["flow"] = self._derive(FLOW_EDGES)
        return self._views["flow"]

    def dependency_view(self) -> nx.DiGraph:
        if "dep" not in self._views:
            self._views["dep"] = self._derive(DEPENDENCY_EDGES)
        return self._views["dep"]

    # ------------------------------------------------------------------ paths
    def find_paths(self, source_id: str, target_id: str, max_depth: int = 10,
                   limit: int = 20) -> list[list[str]]:
        """Simple execution paths from source to target over the flow view,
        shortest first, at most `limit`."""
        if source_id not in self.g or target_id not in self.g:
            return []
        flow = self.flow_view()
        paths = list(islice(nx.all_simple_paths(flow, source_id, target_id, cutoff=max_depth), 1000))
        paths.sort(key=len)
        return paths[:limit]

    def describe_path(self, path: list[str]) -> list[dict]:
        """Hop dicts (relation type, kind, confidence, evidence) for a flow-view path."""
        flow = self.flow_view()
        hops = []
        for a, b in zip(path, path[1:]):
            data = flow.get_edge_data(a, b) or {}
            hops.append({"from": a, "to": b, "type": data.get("type"), "kind": data.get("kind"),
                         "confidence": data.get("confidence"), "evidence": data.get("evidence", [])})
        return hops

    def entry_points(self, types: tuple[str, ...] = ("html_element", "api_endpoint")) -> list[str]:
        """External entry points: UI elements / API endpoints that start a flow
        (no incoming flow edge, at least one outgoing)."""
        flow = self.flow_view()
        return sorted(
            n for n in flow
            if flow.in_degree(n) == 0 and flow.out_degree(n) > 0 and self.g.nodes[n]["type"] in types
        )

    # ------------------------------------------------------------------ export
    def subgraph_json(self, node_ids: set[str] | None = None) -> dict:
        """Nodes/edges as plain dicts for UIs (Cytoscape/D3)."""
        keep = set(self.entities) if node_ids is None else node_ids & set(self.entities)
        nodes = []
        for nid in sorted(keep):
            e = self.entities[nid]
            nodes.append({
                "id": nid, "label": e.qualified_name or e.name, "type": e.type.value,
                "language": e.language.value,
                "location": str(e.location) if e.location else None,
            })
        edges = [
            {"source": u, "target": v, "type": d["type"], "kind": d["kind"], "confidence": d["confidence"]}
            for u, v, d in self.g.edges(data=True) if u in keep and v in keep
        ]
        return {"nodes": nodes, "edges": edges}

    def export_graphml(self, path) -> None:
        """Write the graph as GraphML (opens in Gephi / yEd / Cytoscape desktop). Evidence lists are
        dropped because GraphML attributes must be scalars."""
        simple = nx.MultiDiGraph()
        for nid, data in self.g.nodes(data=True):
            e = self.entities[nid]
            simple.add_node(nid, **{k: str(v) for k, v in data.items()},
                            location=str(e.location) if e.location else "")
        for u, v, key, data in self.g.edges(keys=True, data=True):
            simple.add_edge(u, v, key=key, type=data["type"], kind=data["kind"], confidence=float(data["confidence"]))
        nx.write_graphml(simple, str(path))
