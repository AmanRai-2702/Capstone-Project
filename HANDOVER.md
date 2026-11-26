# RepoMind: Handover, Spec Compliance and Viva Guide

**RepoMind: AI-Powered Multi-Language Repository Intelligence and Code Impact Analysis**
BTech final-year capstone. Windows 11, Python 3.13, PowerShell or cmd. Needs Python 3.11+ (`StrEnum`).

## 1. Status (every number below was produced by running the code)

| Check | Result |
|---|---|
| Automated tests | **181 pass** (`python -m pytest -c backend/pytest.ini backend/tests`) |
| Benchmark `login_app` (FastAPI + vanilla JS) | 39/39 entities, 60/60 relationships; 0 missing, 0 dangling, 0 forbidden edges |
| Benchmark `notes_app` (Flask + multi-page + ES modules), built *after* the analyzers | 52/52 entities, 83/83 relationships. It first scored **84.3% recall** and exposed 6 real bugs (section 5) |
| Browser end-to-end checks (headless Edge): `backend/e2e/ui_check.py` (original journeys) and `backend/e2e/ui_phase7.py` (Phase 7 features) | 20/20 and 42/42 journeys pass, no console errors |
| Dogfooding: RepoMind analysing its own source | works; its own UI is connected to its own API routes |

Benchmark ground truth was written from the *source code*, not from tool output. `forbidden_relationships` lists known false positives that must never come back.

## 2. Spec compliance (your project description, section by section)

| # | Spec item | State |
|---|---|---|
| A | Ingestion: local folder, GitHub URL, **ZIP** | Done. ZIP extraction rejects zip-slip, absolute paths, symlinks, zip bombs |
| A | Configurable exclusions, ignore generated/binary, unreadable files, metadata | Done: built-in list, `REPOMIND_IGNORE`, `--exclude`, `.repomindignore`, size and file-count limits; unreadable/malformed files are reported, not fatal |
| B | Python analyzer: classes, functions, async, methods, imports, calls, **inheritance**, decorators, endpoints, **request model** | Done (annotation-based type tracking, `self.x()` incl. inherited, relative and `src/` imports, `__init__` re-exports). Not done: module/variable entities |
| B | JavaScript: functions, arrows, **classes, imports/exports**, calls, DOM, events, fetch | Done, plus inline `<script>` blocks and `fetch` wrapper helpers. Phase 4 added axios (verbs, config object, `axios.create` baseURL), dynamic `import()`, `import * as ns`, and DOM helpers like `$("#x")`, and property handlers (`el.onclick = fn`, only for real DOM event names on an identifiable element). Not done: frameworks (React/Vue) |
| B | HTML: elements, ids, **classes**, forms/buttons/inputs, links, scripts, stylesheets, containment | Done (page-relative asset paths, CDN skipped, navigation links) |
| B | CSS: selectors, matching, unmatched, dynamic, unsupported | Done: compound `tag#id.class`; a stylesheet only styles pages that link it; dynamic classes are *inferred* only with JS evidence; Phase 4 added descendant/child combinators, attribute selectors, `:first-child`/`:nth-child`/`:not`/`:is`, and state pseudo-classes and pseudo-elements (matched as *inferred*: possible, not certain). Sibling combinators, `:has()`, `:nth-of-type` and unknown pseudo-classes are still never guessed |
| 5 | Unified model, stable ids, evidence, confidence, 4 detection levels | Done: `detected` / `inferred` / `heuristic` + a separate `unresolved` list with reasons |
| 5 | Relationship taxonomy | CONTAINS, IMPORTS, CALLS, REFERENCES, TRIGGERS, REQUESTS, HANDLES, STYLES, INHERITS, USES, plus (Phase 6) **RETURNS** (function -> class it returns), **DATA_FLOW** (consumer -> producer of a value it is given) and **READS_STATE** (method -> method writing a `self.attr` it reads). Declared but **never produced**: DEFINES, RENDERS, IMPLEMENTS, DEPENDS_ON. Anonymous event handlers are entities (`handler@LINE`): the element TRIGGERS it, it CALLS/REQUESTS what its body does. `Relationship.metadata` carries extra facts (REQUESTS: the JSON keys sent/read) |
| 6 | Knowledge graph (NetworkX), traversal, paths, export, visualisation | Done: raw + flow + dependency views, GraphML/JSON export, Cytoscape UI. Neo4j not used (deliberate) |
| 7 | Cross-language flow, route normalisation, method matching, ambiguity, unresolved | Done (`resolution/`): `{id}` = `<int:id>` = `:id` = `${x}`, concrete values match parameters, method mismatch reported, prefix-mounted routes matched heuristically, JS paired to the page that includes it (also through imports) |
| 8 | Impact analysis: direct/indirect/execution-flow/uncertain, paths, evidence, risk | Done, plus **diff-based** impact (`--since REF`, `--diff-file`, API `diff`; signature vs body changes, changed `requirements.txt`/`package.json` packages, deleted files), **ranked suggested tests** with reasons, **evidence-backed risk factors**, a `data_flow` category, and **API contract analysis** (`/contracts`, `/contracts/impact`: keys a handler returns/requires vs keys each client reads/sends; mismatches; who breaks if a key changes). Data flow is Python-only and static (values handed between resolved calls, returned objects, `self.attr` state). Benchmark: `python backend/evaluate_impact.py` (11 hand-derived scenarios; both modes exact). Optional strict mode (`--strict-references`, API `strict`) does not let behavioural impact flow through REFERENCES into an HTML element's referrers (precision 88.6% -> 100% on the first-draft expectations; off by default because the original design reports them as `indirect`) |
| 9 | Q&A / RAG: hybrid retrieval, grounded, cited, refuses, provider-independent | Done: graph linking + TF-IDF vector index, intents (flow, impact, relations, locate, architecture, explain), citation verification, prompt-injection fencing. Dense embeddings (`sentence-transformers`) and `AnthropicProvider` are written but **never run** |
| 10 | Modular architecture | Done (see section 3). No `impact/` package (it lives in `services/impact_service.py`) |
| 11 | Frontend | Done as one static page (Cytoscape), **not** Next.js/React. Dashboard (files, entities, languages, deterministic README/tech/components summary), directory-tree explorer with search, source viewer with go-to-line; graph with server-side filters (type, language, relationship, kind, confidence, max nodes), a file-level view that is the default for repositories above 600 nodes, node search, click-an-edge evidence, double-click neighbourhood focus; execution tracing (choose start/end, four direction views, per-hop kind/confidence/evidence, "read against the raw edge" notes, honest no-path message); impact with risk factors, ranked tests, strict toggle; diff impact; API contracts with a "who breaks if I change this key" form; chat with provider status, suggested questions, per-answer provider/model and a fallback badge; loading and error states everywhere |
| 12 | CLI | `ingest analyze graph trace impact ask serve`, `--json`, `--exclude`, helpful errors |
| 13 | API | All listed endpoints plus relationships, upload, jobs (background analysis), unresolved, delete; validation, limits, request logging, persistence across restarts |
| 14 | Testing | Unit + integration + 2 benchmarks + performance script. Part of the spec not covered: evaluation of Q&A answer quality at scale, impact-accuracy ground truth |
| 15 | Security | Repository code is never executed; zip/path-traversal/symlink defences; URL validation; git ref validation; resource limits; prompt-injection fencing; local paths can be disabled |
| 17 | Phase 9 (agents, suggestions, auto-fix) | **Not implemented** |

## 3. Architecture (`backend/repomind/`)

    ingestion/   sources.py (local, GitHub, zip)   discovery.py (walk, ignore, limits)   classifier.py
    analyzers/   python_analyzer.py (ast)   javascript_analyzer.py / html_analyzer.py / css_analyzer.py (Tree-sitter)
    resolution/  resolver.py     candidate links -> real entities across files/languages; unresolved with reasons
    services/    repository_service.py  analysis_service.py (orchestration, error isolation)  impact_service.py
    graph/       repository_graph.py    NetworkX multigraph + flow view + dependency view + export
    retrieval/   chunker.py  embedder.py (TF-IDF, optional dense)  index.py (inverted-index cosine search)
    qa/          retriever.py (intents, hybrid linking)  providers.py (template, anthropic)  service.py (citation check)
    storage/     store.py        JSON persistence of analyses
    api/         app.py + static/index.html
    models/      Entity, Relationship, Evidence, RepositoryAnalysis, UnresolvedReference, Impact*

Data flow: path/URL/zip -> inventory -> analyzers (HTML first, then Python and JS incl. inline scripts, then CSS) -> `RelationshipResolver` -> `RepositoryAnalysis` -> `RepositoryGraph` -> impact / Q&A / API / UI.

## 4. Design decisions you must be able to defend

* **Four directions, never mixed.** `RepositoryGraph.view(name)`: *syntactic* (raw edges: who mentions whom; `HANDLES` points handler -> endpoint), *execution* (what runs next: UI -> JS -> endpoint -> handler -> callees; calling a class continues into its `__init__`/`constructor`, a derived `CONSTRUCTS` edge), *dependency* (A depends on B) and *impact* (dependency reversed: a change in B may affect A). The same two nodes give different answers per view, e.g. handler -> UI element has no execution path but does have an impact path. Tables `FLOW_EDGES` / `DEPENDENCY_EDGES` make the rules explicit and testable. `find_paths` is bounded (reachability check, then shortest-first, `max_depth`, `limit`): the old enumeration took ~42 s on a 1,200-node call graph when the target was unreachable, now ~40 ms. Graph API: `/graph` (filters: `types, languages, rel_types, kinds, min_confidence, q, max_nodes`, `level=file` for large repositories), `/graph/neighborhood`, `/graph/edge` (evidence), `/trace?view=` (path confidence, `reversed`/`derived` hops, ambiguity report).
* **Impact = reverse reachability** over the dependency view; BFS gives the shortest chain, which doubles as the explanation. Confidence is the product of hop confidences; risk is a transparent rule, not a trained model.
* **Never invent a link.** Anything the resolver cannot connect goes to `unresolved` with a reason (`external_package`, `no_matching_endpoint`, `method_mismatch`, ...). A request to `/pong` with no backend route is reported, not attached to a guess.
* **Q&A cannot hallucinate by construction.** Facts come only from the graph and source; the default provider only re-formats them; citations in any LLM answer are verified; no matching entity means refusal; a similarity-only match is labelled as such; repository text sent to an LLM is fenced as untrusted.
* **Why TF-IDF and an in-memory index** instead of a vector database: repositories have thousands of entities, so it is instant, needs no setup or downloads, is deterministic and easy to explain. The `Embedder` interface is the seam for dense models or ChromaDB.
* **Why a second benchmark.** 100% on the app the analyzers were built against proves little. `notes_app` was written afterwards, scored 84.3% recall, and the failures drove real fixes. That is the evidence that the evaluation can fail.

## 5. Bugs the benchmarks and dogfooding found (good viva material)

1. `getElementById(...).addEventListener("click")` produced a fake `#click` reference (substring test).
2. Decorators were counted as calls made by the function.
3. Edges to external packages pointed at entities that did not exist.
4. CSS compound selectors were "inferred" without any evidence.
5. HTML asset paths ignored the page's folder (`js/app.js` became `js/app.js`, not `static/js/app.js`), which silently broke most JS-to-HTML links.
6. `from repomind.graph import X` never resolved when the analysed folder is the package itself, or with `src/` layouts and `__init__` re-exports.
7. Python type tracking ignored annotations, so typed code (e.g. FastAPI services) lost its call edges.
8. A suggested-tests bug, a stemmer inconsistency, a stale-panel UI bug and a favicon 404, all found by their own tests.

## 6. Performance (`python backend/benchmark_performance.py`, this machine)

| Repo | files | entities | analyse | graph | impact query | Q&A answer | peak mem |
|---|---|---|---|---|---|---|---|
| 50 modules | 71 | 321 | 0.25 s | 8 ms | 1.1 ms | 58 ms | 1.6 MB |
| 200 modules | 281 | 1,281 | 0.87 s | 24 ms | 2.7 ms | 206 ms | 6.2 MB |
| 800 modules | 1,121 | 5,121 | 3.6 s | 162 ms | 11.8 ms | 878 ms | 24.8 MB |

Analysis scales roughly linearly (4x size gives about 4.2x time). Ingestion on Windows is dominated by the OS scanning freshly written files (cold 5.9 s vs warm 0.67 s for 1,121 files), so the benchmark reports both. Memory is Python heap only (tracemalloc). Not measured: repositories above ~1,000 files, concurrent users.

## 7. Commands

    $env:PYTHONPATH="backend"                       # PowerShell     (cmd: set PYTHONPATH=backend)
    python -m pytest -c backend/pytest.ini backend/tests
    python backend/evaluate_ground_truth.py [app]   # exit 1 on regression
    python backend/benchmark_performance.py [sizes]
    python -m repomind.cli analyze|ingest <path|url|.zip> [--json] [--exclude PATTERN]
    python -m repomind.cli graph  <path> [--format json|graphml] [-o FILE]
    python -m repomind.cli trace  <path> --source A --target B
    python -m repomind.cli impact <path> (--target X | --since GIT_REF | --diff-file FILE) [--json]
    python -m repomind.cli ask    <path> "question" [--provider template|anthropic]
    python -m repomind.cli serve [--port 8000]      # UI at /, API docs at /docs

Environment: `REPOMIND_WORKSPACE`, `REPOMIND_LOG_LEVEL`, `REPOMIND_IGNORE`, `REPOMIND_MAX_FILES`, `REPOMIND_MAX_FILE_BYTES`, `REPOMIND_MAX_ZIP_MB`, `REPOMIND_ALLOW_LOCAL_PATHS` (set `0` on any shared server), `REPOMIND_LLM_PROVIDER`, `REPOMIND_LLM_MODEL`, `REPOMIND_EMBEDDER`, `ANTHROPIC_API_KEY`.

## 8. Known limitations (say these honestly in the viva)

* **Real-world accuracy is measured only on a small sample** (Phase 8, see `EVALUATION.md`): 7 targets from 4 open-source repositories, 133 hand-reviewed relationships (95.5% correct before fixes, one reviewer), direct-caller impact on 21 functions (98.4% precision / 100% recall). Heuristic links (unique method name, confidence 0.5) are the weakest class. TypeScript is not analysed; files using syntax newer than the host Python (e.g. 3.14 `except A, B:`) are reported in `analysis_errors` and cascade into noisy unresolved links.
* Phase 8 resolver additions: unresolved reasons `external_api`, `dynamic_reference`, `asset_not_analysed`, `dom_selector_unmatched`; Jinja `extends/include`; submodule imports; scoped `querySelector`; `tag@line:N` ids for same-line elements; nested JS/Python functions. Reproduce with `python backend/evaluate_external.py clone|robustness|sample|retrieval|impact|qa` (raw results in `backend/eval_data/phase8_*.json`). Q&A at 10k entities is now <300 ms (entity-link prefilter, capped architecture answer).
* Python: no full flow analysis, no dynamic dispatch, no `**kwargs`/decorator magic. Tracked since Phase 4: `x = Foo()`, `a or Foo()`, aliases (`r = self.repo`), module-alias calls (`import m as x; x.f()`), multi-method routes, and inherited methods resolved across files (as *inferred*). Not tracked: `from pkg import module; module.f()`, return-value types, containers (`items[0].f()`).
* JavaScript: calls inside an anonymous event handler are still attributed to the file, but the element now gets a TRIGGERS edge (inferred) to every function the handler calls. A DOM helper is followed only when its whole body is `return document.querySelector(param)` / `getElementById(param)`. No framework awareness, no TypeScript.
* CSS: selectors outside the supported subset (see `analyzers/css_selectors.py`) match nothing. Structural matching uses the static HTML only; elements created or moved at runtime cannot be matched.
* Benchmarks: login_app and notes_app are exhaustive. shop_app, dash_app and style_app are *probes*: they list facts a correct analyzer must find (and false links it must not make) for the limitations above. Before Phase 4 their recall was 72.7%, 47.6% and 16.7%; now 100% each. The probes were written first, so they measure exactly what was fixed, not general accuracy.
* Q&A intent detection is rule-based (regex); entity linking is token overlap plus TF-IDF. Vague or unusual phrasing can give a weak answer (it is labelled as a similarity match, or refused).
* `AnthropicProvider` and `SentenceTransformerEmbedder` were not executed (no key / no model download); only the offline path is tested.
* Return/attribute types come from annotations and obvious constructors only (a repository-wide index; an ambiguous class name is never guessed). Phase 6 closed RepoMind's own chat flow: `#chatform > handler > POST /ask > ask > do_ask > QAService.ask` (confidence 0.68, two inferred hops). Not tracked: types through containers (`items[0].f()`), `from pkg import module; module.f()`, reassignment through branches.
* Impact "confidence" is the product along one shortest chain, not a probability. Data dependencies exist for Python only; JavaScript data flow, cross-class attribute writes (`order.total = ...` read elsewhere) and data carried through containers are not tracked. READS_STATE links every reader of `self.x` to every writer (`__init__` included): true but dense. A contract is only as good as what the code shows: a handler that builds its response dynamically is reported as `response_complete: false` and its clients as *possibly* affected, never as safe. Signature vs body is decided from the entity's first line only (a multi-line `def` edited on a later line counts as body).
* The API keeps sessions in memory (analyses persist as JSON); it is single-process and has no authentication. Do not expose it publicly.
* **Frontend:** a React 18 + TypeScript + Vite application in `frontend/` (React Router, Cytoscape.js, no UI framework, light/dark theme; 9 Vitest tests, 37-check browser e2e `backend/e2e/ui_react.py`). The API serves its production build (`frontend/dist`, git-ignored) at `/` with a client-side-routing fallback; the original single page remains at `/legacy` (and is the fallback when there is no build). Not Next.js: there is no server-side rendering need and Vite avoids a second server. The sentence that follows describes the legacy page.
* The UI is a single static page, not a React/Next.js application. Large repositories are handled by the file-level graph and a node cap, not by virtualised rendering: the explorer tree still renders every entity row (fine to a few thousand). The UI has no authentication of its own (see the API security note). The entity datalist for tracing is capped at 3000 entries; longer lists need typing a name or id.

## 9. Not implemented (Phase 9)

Agent-based exploration, automated code suggestions and fixes with approval, additional languages (TypeScript, Java, ...). Extension points: `BaseAnalyzer` (new language), `LLMProvider`, `Embedder`, `RepositoryGraph`/`AnalysisStore` (Neo4j or a database).
