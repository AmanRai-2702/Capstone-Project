"""Graph-grounded retrieval for repository questions.

Pipeline:  question -> intent -> entity linking -> graph query -> Context (facts + citations)

Everything returned is read out of the knowledge graph or the source files on disk;
nothing is generated here, which is what keeps the final answer from hallucinating.
"""
from __future__ import annotations

import re
from pathlib import Path

from repomind.graph import RepositoryGraph
from collections import Counter

import networkx as nx

from repomind.models import Entity, RelationType, RepositoryAnalysis
from repomind.retrieval.hybrid import build_index
from repomind.services.impact_service import ImpactError, ImpactService

from .models import Context, Fact

STOPWORDS = {
    "a", "an", "the", "is", "are", "was", "of", "in", "on", "to", "for", "and", "or", "how", "does",
    "do", "what", "which", "who", "where", "when", "why", "this", "that", "it", "me", "my", "i", "we",
    "work", "works", "flow", "trace", "path", "show", "tell", "explain", "about", "if", "change",
    "changes", "modify", "affect", "affected", "impact", "break", "breaks", "happens", "happen",
    "calls", "call", "called", "uses", "use", "used", "by", "with", "from", "there", "any", "all",
    "api", "py", "js", "html", "css", "function", "method", "class", "file", "end",
}

# An impact question names a *change* and asks about consequences. The bare word "impact" is not
# enough ("how does the UI show the impact?" is about a feature, not about a change).
_CHANGE = r"(change|changes|changing|modify|modifying|edit|editing|rename|renaming|delete|deleting|remove|removing|update|updating|refactor)"
IMPACT_RE = re.compile(
    rf"\b((what|which|who)\b.{{0,40}}\b(break|breaks|broken|affected|impacted|affects?)\b|"
    rf"if (i|we|you) .{{0,20}}\b{_CHANGE}\b|"
    rf"\b{_CHANGE}\b.{{0,60}}\b(affect|affects|break|breaks|impact|impacts)\b|"
    rf"impact of {_CHANGE}|blast radius|ripple effect|what depends on)", re.I)
INCOMING_RE = re.compile(r"\b(who calls|what calls|called by|callers?|who uses|used by|dependents?|"
                         r"what (uses|depends on)|who (references|triggers|requests))\b", re.I)
OUTGOING_RE = re.compile(r"\b(what does .+ (call|use|depend on)|callees?|dependencies|depends on)\b", re.I)
_REPO = r"(repo|repository|project|app|application|codebase|code base|system|software)"
OVERVIEW_RE = re.compile(
    rf"\b(purpose|what (is|does) (this|the|my) {_REPO}( do| for| about)?|what is (this|the) {_REPO}|"
    rf"what (is|does) it (do|for)|about (this|the) {_REPO}|summari[sz]e (this|the|my)? ?{_REPO}|"
    rf"(repo|repository|project|codebase) summary|overview|general idea|in a nutshell|"
    rf"goal of (this|the) {_REPO}|why (does|was) (this|the) {_REPO})\b", re.I)
TECH_RE = re.compile(
    r"\b((programming )?languages?|tech(nology)?[- ]?(stack|stacks)?|technologies|frameworks?|libraries|"
    r"built (with|using)|written in|tools? (used|does)|stack|"
    rf"(packages|dependencies|requirements) (does|do|is|are|used by) (this|the|it)|what .{{0,20}}\b(use|uses|used)\b.{{0,15}}\b(framework|librar\w+|package|database)\w*)\b", re.I)
COMPONENTS_RE = re.compile(
    r"\b(main|major|key|core|primary|important) (components?|modules?|parts?|files?|packages?|directories|folders)|"
    r"(components?|modules?|parts) (of|in) (this|the) |responsibilit\w+ of|project structure|"
    r"(directory|folder|file) (structure|layout|tree)|list (the |all )?(files|modules|components)|"
    r"how is (this|the) .{0,30}(organi[sz]ed|structured)|what files", re.I)
ARCH_RE = re.compile(
    r"\b(architecture|high[- ]level|structure of|"
    r"front[- ]?end.{0,40}back[- ]?end|back[- ]?end.{0,40}front[- ]?end|client.{0,20}server|"
    r"how (do|does) (the )?(ui|front[- ]?end|client|page|pages).{0,30}(talk|communicat\w*|connect\w*|call|reach)|"
    r"communicat\w*)\b", re.I)
DOCS_RE = re.compile(r"\b(readme|documentation|docs|installation|install(ing)?|setup|set up|getting started|"
                     r"how (do i|to) (run|start|use|install|deploy))\b", re.I)
REPO_INTENTS = ("overview", "architecture", "tech", "components", "docs")
LOCATE_RE = re.compile(r"\b(where is|where are|where's|which files?|find the|locate|implemented|"
                       r"implementation of|defined)\b", re.I)
VECTOR_MIN_SCORE = 0.12   # cosine similarity below this is treated as "no semantic match"
# "what happens when THE USER clicks a seat": "the user" is the grammatical subject of the question, not something to
# look for in the code. Only removed before an action verb, so a real `User` class is still found by "who calls User".
_ACTOR_RE = re.compile(r"\b(?:the|a|an) users?\b(?=\s+(?:click|clicks|clicked|press|presses|tap|taps|submit|submits|enter|enters|"
                       r"type|types|select|selects|choose|chooses|open|opens|close|closes|hover|hovers|drag|drags|scroll|scrolls|"
                       r"log|logs|sign|signs|visit|visits|navigate|navigates|load|loads|upload|uploads|add|adds|remove|removes))", re.I)
FLOW_RE = re.compile(r"\b(flow|trace|journey|end[- ]to[- ]end|how does|how do|what happens when|"
                     r"walk me through|sequence|lifecycle)\b", re.I)

PHRASE = {
    "TRIGGERS": "triggers", "REQUESTS": "sends a request to", "HANDLES": "is handled by",
    "CALLS": "calls", "REFERENCES": "references", "IMPORTS": "imports", "STYLES": "styles",
    "CONTAINS": "contains", "INHERITS": "inherits from", "USES": "uses",
    "CONSTRUCTS": "runs the constructor", "RETURNS": "returns an instance of",
    "DATA_FLOW": "is given data produced by", "READS_STATE": "reads state written by",
}


def split_identifier(text: str) -> set[str]:
    """'handleLogin' -> {handle, login};  'auth_service.py' -> {auth, service, py}."""
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)
    return {t.lower() for t in re.split(r"[^A-Za-z0-9]+", spaced) if t}


class Retriever:
    def __init__(self, analysis: RepositoryAnalysis, graph: RepositoryGraph, impact: ImpactService,
                 retrieval_mode: str = "tfidf", embedding_model: str | None = None, cache_dir=None):
        self.analysis = analysis
        self.graph = graph
        self.impact = impact
        self.root = Path(analysis.root_path)
        self._file_cache: dict[str, list[str]] = {}
        self.index = build_index(analysis, graph, retrieval_mode, embedding_model, cache_dir)
        self._by_id = {e.id: e for e in analysis.entities}
        self._children: dict[str, list[Entity]] = {}           # parent id -> entities directly inside it
        for e in analysis.entities:
            if e.parent_id:
                self._children.setdefault(e.parent_id, []).append(e)
        self._lower_names = {e.id: (e.id.lower(), (e.qualified_name or "").lower(), (e.name or "").lower())
                             for e in analysis.entities}
        self._entity_tokens = {
            e.id: split_identifier(e.name) | split_identifier(e.qualified_name)
            for e in analysis.entities
        }

    # ------------------------------------------------------------------ public
    def retrieve(self, question: str) -> Context:
        intent = self.classify(question)
        if intent in REPO_INTENTS:                    # needs no entity match: summarises the whole repository
            ctx = self._repo_level(question, intent)
            if not ctx.empty:
                return ctx
            intent = "explain"
        seeds, vector_only = self._link(question)
        if not seeds:
            # A vague question names no entity. Before refusing, look for supporting documentation.
            docs = self._docs(question, intent="docs", readme_fallback=False)
            return docs if not docs.empty else Context(question=question, intent="none")
        builders = {"impact": self._impact, "relations": self._relations, "locate": self._locate,
                    "flow": self._flow, "explain": self._explain}
        if intent in ("impact", "relations", "explain") and seeds[0][1].id in vector_only:
            intent = "locate"        # no entity was named: list the most relevant candidates instead of
                                     # presenting one similarity match as "the" answer
        ctx = builders[intent](question, seeds)
        if ctx.empty and intent != "explain":      # e.g. flow asked about something with no flow
            ctx = self._explain(question, seeds)
        if ctx.entity_ids and ctx.entity_ids[0] in vector_only:
            ctx.headline += " (matched by semantic similarity, not by an exact name)"
        return ctx

    def classify(self, question: str) -> str:
        if IMPACT_RE.search(question):
            return "impact"
        if OVERVIEW_RE.search(question):
            return "overview"
        if COMPONENTS_RE.search(question):
            return "components"
        if TECH_RE.search(question):
            return "tech"
        if ARCH_RE.search(question):
            return "architecture"
        if INCOMING_RE.search(question) or OUTGOING_RE.search(question):
            return "relations"
        if DOCS_RE.search(question):
            return "docs"
        if LOCATE_RE.search(question):
            return "locate"
        if FLOW_RE.search(question):
            return "flow"
        return "explain"

    # ------------------------------------------------------------------ entity linking
    def link_entities(self, question: str, k: int = 6) -> list[tuple[float, Entity]]:
        return self._link(question, k)[0]

    def _link(self, question: str, k: int = 6) -> tuple[list[tuple[float, Entity]], set[str]]:
        """Hybrid entity linking: exact/token matches from the graph, plus semantic (TF-IDF cosine)
        matches from the vector index. Vector-only hits get a modest weight and are reported, so a
        similarity match is never presented as an exact one."""
        question = _ACTOR_RE.sub(" ", question)
        seeds = self._graph_link(question, k)
        # An entity is NAMED by the question only if its id / qualified name / name appears in it (graph score >= 10).
        # Sharing a word ("user" in "user accounts") is evidence, but not a name: those seeds are reported as unnamed.
        named = {e.id for sc, e in seeds if sc >= 10}
        present = {e.id for _, e in seeds}
        vector_only: set[str] = set()
        for sim, chunk in self.index.search(question, k=5, min_score=VECTOR_MIN_SCORE):
            ent = self._by_id.get(chunk.entity_id)
            if ent is None:
                continue
            if ent.id in present:
                # found by name/word match AND by text similarity: two independent signals agree, so it ranks higher
                seeds = [(sc + 10.0 * sim, e) if e.id == ent.id else (sc, e) for sc, e in seeds]
            else:
                seeds.append((2.0 + 10.0 * sim, ent))
                vector_only.add(ent.id)
        seeds.sort(key=lambda x: (-x[0], x[1].id))
        return seeds[: k + 3], vector_only | ({e.id for _, e in seeds} - named)

    def _graph_link(self, question: str, k: int = 6) -> list[tuple[float, Entity]]:
        q_lower = question.lower()
        q_tokens = {t for t in split_identifier(question) if t not in STOPWORDS and len(t) > 1}
        scored: list[tuple[float, Entity]] = []
        for e in self.analysis.entities:
            score = 0.0
            for t, bonus in zip(self._lower_names[e.id], (15, 12, 10)):
                # A plain substring test is ~100x cheaper than a regex: only names that occur at all get the
                # word-boundary check (this loop runs once per entity per question, so it dominated large repos).
                if len(t) >= 3 and t in q_lower and re.search(rf"(?<![a-z0-9_]){re.escape(t)}(?![a-z0-9_])", q_lower):
                    score = max(score, bonus)
            toks = self._entity_tokens[e.id] - STOPWORDS
            overlap = len(q_tokens & toks)
            if overlap:
                score += 2 * overlap + (2 if toks and toks <= q_tokens else 0)
            if e.type.value == "file":
                score -= 1
            if score >= 2:
                scored.append((score, e))
        scored.sort(key=lambda x: (-x[0], x[1].id))
        return scored[:k]

    # ------------------------------------------------------------------ source access
    def _lines(self, file_path: str) -> list[str]:
        if file_path not in self._file_cache:
            try:
                text = (self.root / file_path).read_text(encoding="utf-8")
                self._file_cache[file_path] = text.splitlines()
            except OSError:
                self._file_cache[file_path] = []
        return self._file_cache[file_path]

    def _snippet(self, file_path: str, start: int, end: int, max_lines: int = 8) -> str:
        lines = self._lines(file_path)
        return "\n".join(lines[start - 1: min(end, start - 1 + max_lines, len(lines))])

    @staticmethod
    def _cite(file_path: str, start: int, end: int) -> str:
        return f"{file_path}:{start}-{end}"

    def _entity_cite(self, e: Entity) -> tuple[str | None, str]:
        if e.location:
            l = e.location
            return self._cite(l.file_path, l.start_line, l.end_line), self._snippet(l.file_path, l.start_line, l.end_line)
        return None, ""

    def _edge_cite(self, edge: dict, fallback: Entity | None) -> tuple[str | None, str]:
        for ev in edge.get("evidence") or []:
            loc = ev.get("location")
            if loc:
                return self._cite(loc["file_path"], loc["start_line"], loc["end_line"]), ev.get("snippet", "")[:400]
        return self._entity_cite(fallback) if fallback else (None, "")

    def _label(self, entity_id: str) -> str:
        e = self.graph.get_entity(entity_id)
        if not e:
            return entity_id
        kind = e.type.value.replace("_", " ")
        if e.language.value in ("unknown", "html", "css") and kind.startswith(e.language.value):
            desc = kind                       # "html element", not "html html element"
        elif e.language.value == "unknown":
            desc = kind                       # api endpoints belong to no single language
        else:
            desc = f"{e.language.value} {kind}"
        return f"`{e.qualified_name or e.name}` ({desc})"

    @staticmethod
    def _tag(edge: dict) -> str:
        return f" [inferred, confidence {edge['confidence']:.2f}]" if edge.get("kind") == "inferred" else ""

    # ------------------------------------------------------------------ intents
    def _best_target(self, seeds: list[tuple[float, Entity]]) -> Entity:
        top = seeds[0][0]
        close = [e for s, e in seeds if s >= top - 1e-9]
        non_file = [e for e in close if e.type.value not in ("file",)]
        return (non_file or close)[0]

    def _flow(self, question: str, seeds: list[tuple[float, Entity]]) -> Context:
        flow = self.graph.flow_view()
        weights = {e.id: s for s, e in seeds}
        scored = []
        import networkx as nx
        for entry in self.graph.entry_points():
            reach = nx.descendants(flow, entry) | {entry}
            score = sum(w for nid, w in weights.items() if nid in reach)
            if score > 0:
                scored.append((score, entry, reach))
        if not scored:   # seeds are not reachable from any UI/API entry point: show their own flow
            reach_nodes = {n for w in weights for n in nx.descendants(flow, w)} | set(weights)
            return self._flow_from_paths(question, [], reach_nodes, flow, seeds)
        scored.sort(key=lambda x: (-x[0], x[1]))
        best = scored[0][0]
        chosen = [(e, r) for s, e, r in scored if s >= 0.5 * best][:3]
        return self._flow_from_paths(question, [e for e, _ in chosen], set().union(*[r for _, r in chosen]), flow, seeds)

    def _flow_from_paths(self, question, entries, reach, flow, seeds) -> Context:
        import networkx as nx
        starts = entries or [e.id for _, e in seeds[:1]]
        facts: list[Fact] = []
        seen_hops: set[tuple[str, str]] = set()
        involved: list[str] = []
        for start in starts:
            sinks = sorted(n for n in nx.descendants(flow, start) if flow.out_degree(n) == 0) or [start]
            for sink in sinks:
                paths = [[start]] if sink == start else self.graph.find_paths(start, sink, limit=3)
                for path in paths:
                    for a, b in zip(path, path[1:]):
                        if (a, b) in seen_hops:
                            continue
                        seen_hops.add((a, b))
                        edge = flow.get_edge_data(a, b)
                        target = self.graph.get_entity(b)
                        cite, snip = self._edge_cite(edge, target)
                        facts.append(Fact(
                            text=f"{self._label(a)} {PHRASE.get(edge['type'], edge['type'].lower())} {self._label(b)}{self._tag(edge)}",
                            citation=cite, snippet=snip))
                        for n in (a, b):
                            if n not in involved:
                                involved.append(n)
        if not facts:
            return Context(question=question, intent="flow")
        heads = ", ".join(f"`{self.graph.get_entity(s).qualified_name}`" for s in starts)
        return Context(question=question, intent="flow", entity_ids=involved,
                       headline=f"Execution flow starting from {heads}:", facts=facts)

    def _impact(self, question: str, seeds: list[tuple[float, Entity]]) -> Context:
        target = self._best_target(seeds)
        try:
            report = self.impact.analyze_impact(target.id)
        except ImpactError:
            return Context(question=question, intent="impact")
        if report.blast_radius == 0:
            cite, snip = self._entity_cite(target)
            return Context(question=question, intent="impact", entity_ids=[target.id],
                           headline=f"Changing {self._label(target.id)} impacts nothing else in the analysed graph.",
                           facts=[Fact(f"{self._label(target.id)} is defined here", cite, snip)])
        facts: list[Fact] = []
        for c in report.impacted[:15]:
            hop = c.chain[0]
            cite = hop.location
            e = self.graph.get_entity(c.entity_id)
            if cite is None:
                cite, _ = self._entity_cite(e)
            conf = f", confidence {c.confidence:.2f}" if c.has_inferred_hop else ""
            facts.append(Fact(
                text=f"{self._label(c.entity_id)} - distance {c.distance}{conf}: {c.explanation}",
                citation=cite, snippet=hop.evidence))
        more = f" (showing 15 of {report.blast_radius})" if report.blast_radius > 15 else ""
        entry = (" User-facing entry points affected: " + ", ".join(f"`{self.graph.get_entity(i).qualified_name}`"
                 for i in report.affected_entry_points) + ".") if report.affected_entry_points else ""
        return Context(question=question, intent="impact", entity_ids=[target.id] + [c.entity_id for c in report.impacted],
                       headline=(f"Changing {self._label(target.id)} may affect {report.blast_radius} component(s){more}; "
                                 f"risk {report.risk} ({report.risk_reason}).{entry}"),
                       facts=facts)

    def _relations(self, question: str, seeds: list[tuple[float, Entity]]) -> Context:
        target = self._best_target(seeds)
        want_in = bool(INCOMING_RE.search(question))
        want_out = bool(OUTGOING_RE.search(question)) or not want_in
        want_in = want_in or not want_out
        edges = []
        if want_in:
            edges += [(e, "in") for e in self.graph.incoming(target.id) if e["type"] != "CONTAINS"]
        if want_out:
            edges += [(e, "out") for e in self.graph.outgoing(target.id) if e["type"] != "CONTAINS"]
        facts = []
        for edge, direction in edges[:20]:
            src, tgt = edge["source"], edge["target"]
            src_e = self.graph.get_entity(src)
            cite, snip = self._edge_cite(edge, src_e)
            facts.append(Fact(
                text=f"{self._label(src)} {PHRASE.get(edge['type'], edge['type'].lower())} {self._label(tgt)}{self._tag(edge)}",
                citation=cite, snippet=snip))
        if not facts:
            return Context(question=question, intent="relations")
        return Context(question=question, intent="relations", entity_ids=[target.id],
                       headline=f"Relationships of {self._label(target.id)}:", facts=facts)

    def _explain(self, question: str, seeds: list[tuple[float, Entity]]) -> Context:
        target = self._best_target(seeds)
        cite, snip = self._entity_cite(target)
        facts = [Fact(f"{self._label(target.id)} is defined here", cite, snip)]
        if target.parent_id and self.graph.get_entity(target.parent_id):
            facts.append(Fact(f"It belongs to {self._label(target.parent_id)}"))
        elif target.parent_id:
            facts.append(Fact(f"It is defined in `{target.parent_id.removeprefix('file:')}`"))
        for edge in [e for e in self.graph.outgoing(target.id) + self.graph.incoming(target.id)
                     if e["type"] != "CONTAINS"][:8]:
            src_e = self.graph.get_entity(edge["source"])
            c, s = self._edge_cite(edge, src_e)
            facts.append(Fact(
                text=f"{self._label(edge['source'])} {PHRASE.get(edge['type'], edge['type'].lower())} {self._label(edge['target'])}{self._tag(edge)}",
                citation=c, snippet=s))
        return Context(question=question, intent="explain", entity_ids=[target.id],
                       headline=f"About {self._label(target.id)}:", facts=facts)

    def _locate(self, question: str, seeds: list[tuple[float, Entity]]) -> Context:
        """'Where is X implemented?': the most relevant code, with location and source."""
        facts, ids = [], []
        for _, e in seeds:
            if e.type.value == "file" or e.id in ids:
                continue
            cite, snip = self._entity_cite(e)
            facts.append(Fact(f"{self._label(e.id)} is defined here", cite, snip))
            ids.append(e.id)
            if len(facts) == 5:
                break
        if not facts:
            return Context(question=question, intent="locate")
        return Context(question=question, intent="locate", entity_ids=ids,
                       headline="Most relevant code:", facts=facts)

    # ------------------------------------------------------------------ repository-level questions
    def _repo_level(self, question: str, intent: str) -> Context:
        """Whole-repository questions need no entity name. Sections are ordered by what was asked,
        and every fact is read from the graph, the docs or a manifest file (never generated)."""
        if intent == "docs":
            return self._docs(question, intent="docs")
        a = self.analysis
        sections = {
            "overview": ("readme", "languages", "tech", "components", "endpoints"),
            "architecture": ("languages", "components", "endpoints", "flows", "hubs", "gaps", "readme"),
            "tech": ("languages", "tech", "external"),
            "components": ("components", "endpoints", "hubs"),
        }[intent]
        builders = {"readme": self._readme_facts, "languages": self._language_facts, "tech": self._tech_facts,
                    "components": self._component_facts, "endpoints": self._endpoint_facts,
                    "flows": self._entry_flow_facts, "hubs": self._hub_facts, "gaps": self._gap_facts,
                    "external": self._external_facts}
        facts: list[Fact] = []
        for name in sections:
            facts += builders[name]()
        ids = [e.id for e in self.analysis.entities if e.type.value == "api_endpoint"]
        heads = {"overview": f"Overview of `{a.name}`:", "architecture": f"Architecture overview of `{a.name}`:",
                 "tech": f"Technologies used in `{a.name}`:", "components": f"Main components of `{a.name}`:"}
        return Context(question=question, intent=intent, entity_ids=ids, headline=heads[intent], facts=facts)

    def _architecture(self, question: str) -> Context:      # kept for callers of the old API
        return self._repo_level(question, "architecture")

    def _doc_files(self) -> list[Entity]:
        return [e for e in self.analysis.entities if e.type.value == "file"
                and e.metadata.get("category") == "doc"
                and e.qualified_name.lower().endswith((".md", ".rst", ".txt", ".adoc"))]

    def _readme_facts(self, max_lines: int = 25) -> list[Fact]:
        readmes = sorted((e for e in self._doc_files() if e.name.lower().startswith("readme")),
                         key=lambda e: (e.qualified_name.count("/"), e.qualified_name))
        if not readmes:
            return []
        path = readmes[0].qualified_name
        lines = self._lines(path)
        picked = [(n, ln) for n, ln in enumerate(lines, 1)
                  if ln.strip() and not ln.lstrip().startswith(("![", "[!["))][:max_lines]
        if not picked:
            return []
        start, end = picked[0][0], picked[-1][0]
        return [Fact(f"The repository documents itself in `{path}` (excerpt)", self._cite(path, start, end),
                     "\n".join(ln for _, ln in picked), quote=True)]

    def _docs(self, question: str, intent: str = "docs", readme_fallback: bool = True) -> Context:
        hits = self.index.search(question, k=4, min_score=VECTOR_MIN_SCORE, kind="doc")
        facts = [Fact(f"Documentation excerpt from `{c.citation.rsplit(':', 1)[0]}`", c.citation,
                      "\n".join(c.text.splitlines()[1:12]), quote=True) for _, c in hits if c.citation]
        ids = list(dict.fromkeys(c.entity_id for _, c in hits))
        if not facts and readme_fallback:
            facts = self._readme_facts()
        if not facts:
            return Context(question=question, intent=intent)
        return Context(question=question, intent=intent, entity_ids=ids, headline="Relevant documentation:", facts=facts)

    def _language_facts(self) -> list[Fact]:
        a = self.analysis
        files = [e for e in a.entities if e.type.value == "file" and e.metadata.get("category") == "source"]
        by_lang = Counter(e.language.value for e in files)
        code = [e for e in a.entities if e.type.value != "file"]
        return [Fact("Source files: " + ", ".join(f"{n} {lang}" for lang, n in sorted(by_lang.items()))
                     + f"; {len(code)} code entities and {a.relationship_count} relationships were extracted")]

    # Well-known packages -> what they are. Only used to *describe* a package that the repository
    # itself declares or imports; a package that is not found in the repo is never mentioned.
    KNOWN_PACKAGES = {
        "fastapi": "FastAPI web framework", "flask": "Flask web framework", "django": "Django web framework",
        "pydantic": "Pydantic data validation", "sqlalchemy": "SQLAlchemy ORM", "uvicorn": "Uvicorn ASGI server",
        "requests": "HTTP client", "httpx": "HTTP client", "numpy": "NumPy", "pandas": "pandas",
        "react": "React UI library", "vue": "Vue UI framework", "express": "Express web framework",
        "axios": "Axios HTTP client", "jquery": "jQuery", "pytest": "pytest test framework",
        "bootstrap": "Bootstrap CSS framework", "tailwindcss": "Tailwind CSS", "jest": "Jest test framework",
    }

    def _tech_facts(self) -> list[Fact]:
        facts: list[Fact] = []
        for e in self.analysis.entities:
            if e.type.value != "file" or e.metadata.get("category") != "config":
                continue
            name = e.name.lower()
            if name.startswith("requirements") and name.endswith(".txt"):
                lines = self._lines(e.qualified_name)
                pkgs = [(n, re.split(r"[<>=!~\[; #]", ln.strip(), maxsplit=1)[0]) for n, ln in enumerate(lines, 1)
                        if ln.strip() and not ln.lstrip().startswith(("#", "-"))]
                if pkgs:
                    facts.append(Fact(f"Python packages declared in `{e.qualified_name}`: {self._describe(p for _, p in pkgs)}",
                                      self._cite(e.qualified_name, pkgs[0][0], pkgs[-1][0]),
                                      "\n".join(lines[pkgs[0][0] - 1: pkgs[-1][0]][:12])))
            elif name == "package.json":
                try:
                    import json
                    data = json.loads("\n".join(self._lines(e.qualified_name)))
                except (ValueError, TypeError):
                    continue
                deps = sorted({*(data.get("dependencies") or {}), *(data.get("devDependencies") or {})})
                if deps:
                    n = max(len(self._lines(e.qualified_name)), 1)
                    facts.append(Fact(f"JavaScript packages declared in `{e.qualified_name}`: {self._describe(deps)}",
                                      self._cite(e.qualified_name, 1, n)))
        imported = sorted({u.detail.split(".")[0].split("/")[0] for u in self.analysis.unresolved
                           if u.reason == "external_package" and u.detail})
        known = [p for p in imported if p.lower() in self.KNOWN_PACKAGES]
        if known:
            facts.append(Fact("Frameworks and libraries imported by the code: "
                              + ", ".join(f"{p} ({self.KNOWN_PACKAGES[p.lower()]})" for p in known)))
        return facts

    def _describe(self, names) -> str:
        names = list(dict.fromkeys(names))
        shown = [f"{n} ({self.KNOWN_PACKAGES[n.lower()]})" if n.lower() in self.KNOWN_PACKAGES else n
                 for n in names[:15]]
        return ", ".join(shown) + (f" and {len(names) - 15} more" if len(names) > 15 else "")

    def _component_facts(self, max_groups: int = 8, max_names: int = 5) -> list[Fact]:
        """One fact per top-level directory: its source files and the main things defined in them."""
        groups: dict[str, list[Entity]] = {}
        for e in self.analysis.entities:
            if e.type.value == "file" and e.metadata.get("category") == "source":
                top = e.qualified_name.split("/", 1)[0] if "/" in e.qualified_name else "(repository root)"
                groups.setdefault(top, []).append(e)
        facts = []
        for top, files in sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0]))[:max_groups]:
            names = []
            for f in sorted(files, key=lambda x: x.qualified_name):
                defined = [x for x in self._children.get(f.id, ()) if x.type.value in ("class", "function", "api_endpoint")]
                names.append(f"`{f.qualified_name}`"
                             + (f" ({', '.join(d.name for d in defined[:max_names])})" if defined else ""))
            facts.append(Fact(f"`{top}` holds {len(files)} source file(s): " + "; ".join(names[:6])
                              + (f"; and {len(names) - 6} more" if len(names) > 6 else "")))
        return facts

    def _endpoint_facts(self, limit: int = 25) -> list[Fact]:
        facts = []
        endpoints = sorted((e for e in self.analysis.entities if e.type.value == "api_endpoint"), key=lambda e: e.id)
        # Large repositories have hundreds of endpoints: describe the best connected ones and say how many were left out.
        shown = sorted(sorted(endpoints, key=lambda e: -self.graph.g.degree(e.id))[:limit], key=lambda e: e.id)
        for ep in shown:
            handlers = [x["source"] for x in self.graph.incoming(ep.id, [RelationType.HANDLES])]
            clients = [x["source"] for x in self.graph.incoming(ep.id, [RelationType.REQUESTS])]
            text = f"{self._label(ep.id)}"
            text += (" is handled by " + ", ".join(self._label(h) for h in handlers)) if handlers else " has no handler in the repository"
            text += (" and is called from " + ", ".join(self._label(c) for c in clients)) if clients else " and no client in the repository calls it"
            cite, snip = self._entity_cite(ep)
            facts.append(Fact(text, cite, snip))
        if len(endpoints) > len(shown):
            facts.append(Fact(f"{len(endpoints) - len(shown)} further API endpoint(s) are not listed here (showing the {len(shown)} best connected of {len(endpoints)})"))
        return facts

    def _entry_flow_facts(self, limit: int = 10) -> list[Fact]:
        flow, facts = self.graph.flow_view(), []
        entries = self.graph.entry_points()
        busiest = sorted(sorted(entries, key=lambda n: -flow.out_degree(n))[:limit])      # reachability only for these
        if len(entries) > len(busiest):
            facts.append(Fact(f"{len(entries)} entry points exist; flows are summarised for the {len(busiest)} with the most outgoing links"))
        for entry in busiest:
            reach = nx.descendants(flow, entry)
            sinks = sorted(n for n in reach if flow.out_degree(n) == 0)
            if reach:
                cite, _ = self._entity_cite(self.graph.get_entity(entry))
                facts.append(Fact(f"Entry point {self._label(entry)} starts a flow reaching {len(reach)} components, "
                                  f"ending in {', '.join(self._label(s) for s in sinks[:3])}", cite))
        return facts

    def _hub_facts(self) -> list[Fact]:
        dep = self.graph.dependency_view()
        code = [e for e in self.analysis.entities if e.type.value in ("class", "function", "method")]
        hubs = [e for e in sorted(code, key=lambda e: (-dep.in_degree(e.id), e.id)) if dep.in_degree(e.id) > 0][:3]
        return [Fact(f"Central component: {self._label(e.id)} has {dep.in_degree(e.id)} direct dependents",
                     self._entity_cite(e)[0]) for e in hubs]

    def _external_facts(self) -> list[Fact]:
        external = sorted({u.detail for u in self.analysis.unresolved if u.reason == "external_package" and u.detail})
        return [Fact("External packages used (not analysed): " + ", ".join(external))] if external else []

    def _gap_facts(self) -> list[Fact]:
        facts = self._external_facts()
        broken = [u for u in self.analysis.unresolved if u.reason in ("no_matching_endpoint", "method_mismatch")]
        if broken:
            facts.append(Fact(f"{len(broken)} frontend request(s) could not be matched to a backend route: "
                              + ", ".join(sorted({u.target_id for u in broken}))))
        return facts
