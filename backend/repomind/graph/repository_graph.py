"""Knowledge graph over a RepositoryAnalysis (Stage 4).

The graph stores every entity as a node and every relationship as a directed edge,
exactly as the analyzers emitted them. Edge direction means different things depending on the
question, so there are four named views and they must not be mixed up:

  syntactic  - "who mentions whom", as written in the code (the raw edges, incl. CONTAINS).
               HANDLES points handler -> endpoint because the decorator is on the handler.
  execution  - "what runs next":  html -TRIGGERS-> js -REQUESTS-> api -> python handler -CALLS-> ...
               (HANDLES reversed: a request reaches the endpoint, then its handler; calling a class
               runs its __init__, see CONSTRUCTS). Used for flow tracing.
  dependency - "A -> B means A depends on B". Used to reason about what a change can break.
  impact     - the dependency view reversed: "B -> A means a change in B may affect A". Used for
               blast-radius queries (reachability from the changed component).

The views are built from the small tables below, so the semantics are explicit and testable.
"""
from __future__ import annotations

from collections import defaultdict, deque
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


# Derived (not extracted) edge: calling a class runs its constructor. Present in the execution and
# dependency views only; the raw graph stays exactly what the analyzers saw.
CONSTRUCTS = "CONSTRUCTS"
VIEWS = ("syntactic", "execution", "dependency", "impact")
_CONSTRUCTOR_NAMES = ("__init__", "constructor")
# Relationship types worth aggregating when collapsing the graph to files (CONTAINS is structure, not coupling).
COUPLING_TYPES = ("CALLS", "IMPORTS", "REFERENCES", "REQUESTS", "HANDLES", "TRIGGERS", "INHERITS", "USES", "STYLES")


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
                           confidence=data["confidence"], evidence=data["evidence"],
                           reversed=table[rtype], derived=False)
        self._add_constructor_edges(d)
        return d

    def constructor_of(self, class_id: str) -> tuple[str, bool] | None:
        """(constructor entity id, inherited?) for a class: its own __init__/constructor, else the nearest
        base class's (following INHERITS, breadth first). None if no constructor is defined anywhere."""
        queue, seen, first = deque([class_id]), set(), True
        while queue:
            cls = queue.popleft()
            if cls in seen:
                continue
            seen.add(cls)
            for child in self.children(cls):
                e = self.entities.get(child)
                if e is not None and e.type.value == "method" and e.name in _CONSTRUCTOR_NAMES:
                    return child, not first
            first = False
            queue.extend(x["target"] for x in self.outgoing(cls, [RelationType.INHERITS]))
        return None

    def _add_constructor_edges(self, d: nx.DiGraph) -> None:
        """class -> its constructor, in the derived view `d`. Calling `Order(...)` executes `Order.__init__`
        (or the inherited one), so execution continues there and a change to the constructor affects
        everything that instantiates the class."""
        for node, data in self.g.nodes(data=True):
            if data["type"] != "class":
                continue
            found = self.constructor_of(node)
            if found and not d.has_edge(node, found[0]):
                ctor, inherited = found
                d.add_edge(node, ctor, type=CONSTRUCTS, kind="inferred" if inherited else "detected",
                           confidence=0.9 if inherited else 1.0, evidence=[], reversed=False, derived=True)

    def flow_view(self) -> nx.DiGraph:
        if "flow" not in self._views:
            self._views["flow"] = self._derive(FLOW_EDGES)
        return self._views["flow"]

    def dependency_view(self) -> nx.DiGraph:
        if "dep" not in self._views:
            self._views["dep"] = self._derive({**DEPENDENCY_EDGES})
        return self._views["dep"]

    def impact_view(self) -> nx.DiGraph:
        """Dependency view with every edge reversed: B -> A means "a change in B may affect A"."""
        if "impact" not in self._views:
            self._views["impact"] = self.dependency_view().reverse(copy=True)
        return self._views["impact"]

    def syntactic_view(self) -> nx.DiGraph:
        """All raw edges (incl. CONTAINS) collapsed to one DiGraph; the best-confidence edge per pair."""
        if "syntactic" not in self._views:
            d = nx.DiGraph()
            d.add_nodes_from(self.g.nodes)
            for u, v, data in self.g.edges(data=True):
                prev = d.get_edge_data(u, v)
                if prev is None or data["confidence"] > prev["confidence"]:
                    d.add_edge(u, v, type=data["type"], kind=data["kind"], confidence=data["confidence"],
                               evidence=data["evidence"], reversed=False, derived=False)
            self._views["syntactic"] = d
        return self._views["syntactic"]

    def view(self, name: str) -> nx.DiGraph:
        if name not in VIEWS:
            raise ValueError(f"unknown view {name!r}; choose from {VIEWS}")
        return {"syntactic": self.syntactic_view, "execution": self.flow_view,
                "dependency": self.dependency_view, "impact": self.impact_view}[name]()

    # ------------------------------------------------------------------ paths
    def find_paths(self, source_id: str, target_id: str, max_depth: int = 10, limit: int = 20,
                   view: str = "execution") -> list[list[str]]:
        """Simple paths from source to target over a view (default: execution), shortest first.

        Cost is bounded: an unreachable target is rejected by one BFS, and paths are produced lazily in
        increasing length (Yen's algorithm), stopping at `limit` paths or `max_depth` hops. (Enumerating
        all simple paths first, as this method used to, takes tens of seconds on a 1,000-node call graph
        when the target cannot be reached.)"""
        if source_id not in self.g or target_id not in self.g or source_id == target_id:
            return []
        graph = self.view(view)
        if not nx.has_path(graph, source_id, target_id):
            return []
        paths: list[list[str]] = []
        for path in nx.shortest_simple_paths(graph, source_id, target_id):
            if len(path) - 1 > max_depth:
                break
            paths.append(path)
            if len(paths) >= limit:
                break
        return paths

    def describe_path(self, path: list[str], view: str = "execution") -> list[dict]:
        """One dict per hop: relation type, kind, confidence, evidence, and whether the hop runs against
        the raw edge direction (`reversed`, e.g. endpoint -> handler) or is derived (`derived`, e.g.
        class -> constructor)."""
        graph = self.view(view)
        hops = []
        for a, b in zip(path, path[1:]):
            data = graph.get_edge_data(a, b) or {}
            hops.append({"from": a, "to": b, "type": data.get("type"), "kind": data.get("kind"),
                         "confidence": data.get("confidence"), "evidence": data.get("evidence", []),
                         "reversed": data.get("reversed", False), "derived": data.get("derived", False)})
        return hops

    def path_summary(self, path: list[str], view: str = "execution") -> dict:
        """Path + hops + a heuristic confidence (product of hop confidences; NOT a probability) and
        `uncertain` = any hop is inferred/heuristic."""
        hops = self.describe_path(path, view)
        conf = 1.0
        for h in hops:
            conf *= h["confidence"] if h["confidence"] is not None else 1.0
        return {"nodes": path, "hops": hops, "confidence": round(conf, 4),
                "uncertain": any(h["kind"] in ("inferred", "heuristic") for h in hops)}

    def entry_points(self, types: tuple[str, ...] = ("html_element", "api_endpoint")) -> list[str]:
        """External entry points: UI elements / API endpoints that start a flow
        (no incoming flow edge, at least one outgoing)."""
        flow = self.flow_view()
        return sorted(
            n for n in flow
            if flow.in_degree(n) == 0 and flow.out_degree(n) > 0 and self.g.nodes[n]["type"] in types
        )

    # ------------------------------------------------------------------ selection and queries
    def select_nodes(self, types: Iterable[str] | None = None, languages: Iterable[str] | None = None,
                     query: str | None = None, include_files: bool = True) -> set[str]:
        """Node ids matching ALL given filters (None = no filter). `query` is a case-insensitive fragment of
        the id or qualified name."""
        types, languages = set(types) if types else None, set(languages) if languages else None
        q = query.lower() if query else None
        out = set()
        for nid, e in self.entities.items():
            if not include_files and e.type.value == "file":
                continue
            if types and e.type.value not in types or languages and e.language.value not in languages:
                continue
            if q and q not in nid.lower() and q not in (e.qualified_name or "").lower():
                continue
            out.add(nid)
        return out

    def neighborhood(self, center: str, depth: int = 1, direction: str = "both",
                     rel_types: Iterable[str] | None = None, view: str = "syntactic",
                     max_nodes: int | None = None) -> set[str]:
        """Nodes within `depth` hops of `center`, nearest first, following edges `out`, `in` or `both`
        ways over a view. `rel_types` limits which relationship types are followed (e.g. leave out CONTAINS
        to see only coupling). With `max_nodes`, the nearest nodes win."""
        if center not in self.g:
            return set()
        graph = self.view(view)
        wanted = {t.upper() for t in rel_types} if rel_types else None
        seen, queue = {center: 0}, deque([center])
        while queue and (max_nodes is None or len(seen) < max_nodes):
            node = queue.popleft()
            if seen[node] >= depth:
                continue
            steps = []
            if direction in ("out", "both"):
                steps += [(v, d) for _, v, d in graph.out_edges(node, data=True)]
            if direction in ("in", "both"):
                steps += [(u, d) for u, _, d in graph.in_edges(node, data=True)]
            for other, data in sorted(steps, key=lambda x: x[0]):
                if other in seen or (wanted and data["type"] not in wanted):
                    continue
                seen[other] = seen[node] + 1
                queue.append(other)
                if max_nodes is not None and len(seen) >= max_nodes:
                    break
        return set(seen)

    def top_nodes(self, node_ids: set[str], max_nodes: int, always: Iterable[str] = ()) -> set[str]:
        """The `max_nodes` best-connected nodes of a selection (by degree), always keeping `always`."""
        keep = set(always) & node_ids
        rest = sorted(node_ids - keep, key=lambda n: (-self.g.degree(n), n))
        return keep | set(rest[: max(max_nodes - len(keep), 0)])

    # ------------------------------------------------------------------ export
    def subgraph_json(self, node_ids: set[str] | None = None, rel_types: Iterable[str] | None = None,
                      kinds: Iterable[str] | None = None, min_confidence: float = 0.0,
                      include_evidence: bool = False) -> dict:
        """Nodes/edges as plain dicts for UIs (Cytoscape/D3). Edges can be filtered by relationship type,
        detection kind and minimum confidence; each edge has a stable `id` ("source|TYPE|target") that
        `edge_details` accepts, and optionally its evidence inline."""
        keep = set(self.entities) if node_ids is None else node_ids & set(self.entities)
        wanted = {t.upper() for t in rel_types} if rel_types else None
        kinds = {k.lower() for k in kinds} if kinds else None
        nodes = []
        for nid in sorted(keep):
            e = self.entities[nid]
            nodes.append({
                "id": nid, "label": e.qualified_name or e.name, "type": e.type.value,
                "language": e.language.value,
                "location": str(e.location) if e.location else None,
                "degree": self.g.degree(nid),
            })
        edges = []
        for u, v, key, d in self.g.edges(keys=True, data=True):
            if u not in keep or v not in keep:
                continue
            if wanted and d["type"] not in wanted or kinds and d["kind"] not in kinds or d["confidence"] < min_confidence:
                continue
            edge = {"id": f"{u}|{d['type']}|{v}", "source": u, "target": v, "type": d["type"], "kind": d["kind"],
                    "confidence": d["confidence"], "evidence_count": len(d["evidence"])}
            if include_evidence:
                edge["evidence"] = d["evidence"]
            edges.append(edge)
        return {"nodes": nodes, "edges": edges}

    def edge_details(self, source: str, rel_type: str, target: str) -> dict | None:
        """Everything known about one edge, including its evidence (source snippets and locations)."""
        data = self.g.get_edge_data(source, target, key=rel_type.upper())
        if data is None:
            return None
        return {"id": f"{source}|{data['type']}|{target}", "source": source, "target": target, "type": data["type"],
                "kind": data["kind"], "confidence": data["confidence"], "evidence": data["evidence"]}

    def module_graph(self, rel_types: Iterable[str] = COUPLING_TYPES) -> dict:
        """The graph collapsed to FILES, for large repositories: one node per source file, one edge per
        (file, file, relationship type) with a count. Cross-language links show up here too, e.g. a
        JavaScript file REQUESTS a Python file (through the endpoint it handles)."""
        wanted = {t.upper() for t in rel_types}

        def file_of(nid: str) -> str | None:
            e = self.entities[nid]
            if e.type.value == "file":
                return nid
            return f"file:{e.location.file_path}" if e.location and f"file:{e.location.file_path}" in self.entities else None

        counts: dict[tuple[str, str, str], list[float]] = defaultdict(list)
        for u, v, d in self.g.edges(data=True):
            if d["type"] not in wanted:
                continue
            a, b = file_of(u), file_of(v)
            if a and b and a != b:
                counts[(a, b, d["type"])].append(d["confidence"])
        files = {f for a, b, _ in counts for f in (a, b)}
        size = {f: len(self.descendants(f)) for f in files}
        nodes = [{"id": f, "label": self.entities[f].qualified_name, "type": "file",
                  "language": self.entities[f].language.value, "entities": size[f],
                  "directory": self.entities[f].qualified_name.rsplit("/", 1)[0] if "/" in self.entities[f].qualified_name else "."}
                 for f in sorted(files)]
        edges = [{"id": f"{a}|{t}|{b}", "source": a, "target": b, "type": t, "count": len(c),
                  "min_confidence": min(c)} for (a, b, t), c in sorted(counts.items())]
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
