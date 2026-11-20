# RepoMind — Master Project Handover & Development Roadmap

**Project Name**: RepoMind — AI-Powered Multi-Language Repository Intelligence and Code Impact Analysis  
**Project Level**: BTech Capstone Project (Final Year Computer Science, AI/ML Specialization)  
**Current Date**: October / November 2026  
**Environment**: Windows 11, Python 3.13.0, PowerShell  
**Active Test Suite Status**: **45 / 45 Tests Passing (100%) in ~0.5s**  

---

## 1. Executive Summary & Core Identity

### What is RepoMind?
RepoMind is **not** a basic GitHub snippet retrieval chatbot. It is a repository intelligence system that statically analyzes polyglot codebases (Python, JavaScript, HTML, CSS), extracts syntactic and semantic components, constructs a repository knowledge graph, traces end-to-end application flows, and performs **change-impact analysis** (blast-radius calculation).

### The Core Value Proposition: Cross-Language Tracing
Modern web applications span multiple languages. RepoMind connects components across these boundaries:
$$\text{HTML UI Element} \xrightarrow{\text{TRIGGERS}} \text{JS Event Handler} \xrightarrow{\text{REQUESTS}} \text{FastAPI Endpoint} \xrightarrow{\text{HANDLES}} \text{Python Service Method} \xrightarrow{\text{CALLS}} \text{DB Repository}$$

It differentiates between:
- **`detected` relationships**: Directly observed via syntax (e.g. decorator `@app.post("/api/login")`, DOM query `getElementById("login-form")`).
- **`inferred` relationships**: Derived via static heuristics or local type propagation (e.g. dynamic class toggling `.message.error`, imported instance calls `auth_service.authenticate()`).

---

## 2. Project Architecture & Directory Layout

```text
Capstone Project/
├── backend/
│   ├── pytest.ini                         # Pytest configuration (pythonpath = ., testpaths = tests)
│   ├── requirements.txt                   # Backend dependencies
│   ├── repomind/
│   │   ├── __init__.py
│   │   ├── cli.py                         # Unified CLI (ingest & analyze commands)
│   │   ├── config.py                      # Settings & environment variables
│   │   ├── logging_setup.py               # Configured logging
│   │   ├── models/                        # STAGE 0: Pydantic Foundation Models
│   │   │   ├── __init__.py
│   │   │   ├── analysis.py                # RepositoryAnalysis container model
│   │   │   ├── entity.py                  # Unified Entity schema
│   │   │   ├── enums.py                   # Language, EntityType, RelationType, RelationKind
│   │   │   ├── ids.py                     # Deterministic, stable ID generators
│   │   │   ├── location.py                # SourceLocation (file, start_line, end_line)
│   │   │   ├── relationship.py            # Relationship & Evidence schemas
│   │   │   └── repository.py              # FileRecord & RepositoryInventory models
│   │   ├── ingestion/                     # STAGE 1: Discovery & Filtering
│   │   │   ├── __init__.py
│   │   │   ├── classifier.py              # File categorization (source, doc, config, other)
│   │   │   ├── discovery.py               # Directory walking, ignore rules, size limits
│   │   │   ├── errors.py                  # Ingestion exception classes
│   │   │   └── sources.py                 # LocalFolderSource & GitHubSource (shallow clone)
│   │   ├── analyzers/                     # STAGE 2: AST & Tree-Sitter Analyzers
│   │   │   ├── __init__.py
│   │   │   ├── base.py                    # BaseAnalyzer interface & AnalysisResult container
│   │   │   ├── python_analyzer.py         # Python AST parser (classes, methods, FastAPI routes, calls)
│   │   │   ├── javascript_analyzer.py     # JS Tree-sitter parser (DOM, fetch, event listeners, calls)
│   │   │   ├── html_analyzer.py           # HTML Tree-sitter parser (elements, IDs, containment, assets)
│   │   │   └── css_analyzer.py            # CSS Tree-sitter parser (selectors, STYLES matching)
│   │   └── services/                      # STAGE 1 & 3: High-Level Services
│   │       ├── __init__.py
│   │       ├── repository_service.py      # Ingests repo to RepositoryInventory
│   │       └── analysis_service.py        # Orchestrates multi-language parsing pipeline
│   └── tests/                             # Comprehensive Automated Test Suite (45 tests)
│       ├── test_models.py                 # 7 tests: Schema validation, stable IDs, roundtrips
│       ├── test_ingestion.py              # 16 tests: Ignore filters, cloning, classification
│       ├── test_python_analyzer.py        # 6 tests: Classes, methods, FastAPI decorators, calls
│       ├── test_javascript_analyzer.py    # 6 tests: Functions, DOM queries, fetch routes, triggers
│       ├── test_html_analyzer.py          # 4 tests: Element IDs, containment, asset links
│       ├── test_css_analyzer.py           # 3 tests: Selectors, styles matching, dead CSS
│       └── test_analysis_service.py       # 3 tests: Full pipeline integration & cross-language trace
├── test_apps/
│   └── login_app/                         # Evaluation Benchmark Application
│       ├── app.py                         # FastAPI backend
│       ├── requirements.txt
│       ├── ground_truth.json              # 34 expected entities & 34 relationships
│       ├── services/auth_service.py       # Authentication business logic
│       ├── repositories/user_repository.py# In-memory user database
│       └── static/
│           ├── index.html                 # UI structure
│           ├── login.js                   # Client logic, fetch calls, DOM handlers
│           └── style.css                  # UI styling
├── .venv/                                 # Installed virtual environment
├── requirements.txt                       # Root requirements
└── HANDOVER.md                            # THIS FILE
```

---

## 3. Work Completed So Far (Stages 0 to 3)

### Stage 0: Foundation Data Models
- **`Entity`**: Stable ID (`language:filepath:qualified_name`), type (`CLASS`, `METHOD`, `FUNCTION`, `HTML_ELEMENT`, `CSS_SELECTOR`, `API_ENDPOINT`), location (`SourceLocation`), parent ID, metadata.
- **`Relationship`**: `source_id`, `target_id`, `type` (`CONTAINS`, `HANDLES`, `CALLS`, `REQUESTS`, `TRIGGERS`, `STYLES`, `REFERENCES`, `IMPORTS`), `kind` (`detected` vs `inferred`), `confidence` (0.0 to 1.0), and `evidence` (code snippet and line numbers).
- **`ids.py`**: Deterministic generators:
  - `make_entity_id("python", "app.py", "login")` $\to$ `"python:app.py:login"`
  - `make_endpoint_id("POST", "/api/login")` $\to$ `"api:POST:/api/login"`
  - `make_file_id("static/login.js")` $\to$ `"file:static/login.js"`

### Stage 1: Repository Ingestion & Discovery
- Implemented in `backend/repomind/ingestion/` and `RepositoryService.ingest(location)`.
- Discovers files while safely ignoring noise (`.git`, `node_modules`, `__pycache__`, binaries, minified bundles, `.env`, oversized files).
- Produces typed `RepositoryInventory` with language breakdown, category counts, and line numbers.

### Stage 2: Single-Language AST & Tree-Sitter Analyzers
- **Python Analyzer (`PythonAnalyzer`)**:
  - Uses Python's built-in `ast` module.
  - Extracts classes, methods, top-level functions.
  - Automatically identifies FastAPI/Flask route decorators (`@app.get("/")`, `@app.post("/api/login")`) and generates `api:METHOD:/route` endpoint entities and `HANDLES` relationships.
  - Performs local type tracking (`auth_service = AuthService()`) to resolve inter-module method calls (`auth_service.authenticate()`).
- **JavaScript Analyzer (`JavaScriptAnalyzer`)**:
  - Uses `tree-sitter` and `tree-sitter-javascript`.
  - Extracts functions and arrow functions.
  - Identifies DOM selections (`document.getElementById("login-form")`, `querySelector("#message")`) as `REFERENCES` to HTML elements.
  - Analyzes `fetch()` calls: static routes (`/api/login`) and template literals (`` `/api/users/${userId}` `` $\to$ `/api/users/{user_id}`).
  - Resolves event listeners (`form.addEventListener("submit", handleLogin)`) as `TRIGGERS` relationships.
- **HTML Analyzer (`HTMLAnalyzer`)**:
  - Uses `tree-sitter-html`.
  - Extracts all elements with IDs (`#login-card`, `#username`, `#password`, etc.).
  - Builds DOM containment hierarchy (`#login-card` `CONTAINS` `#login-form`).
  - Identifies asset tags (`<script src>`, `<link href>`) as `REFERENCES` to files.
- **CSS Analyzer (`CSSAnalyzer`)**:
  - Uses `tree-sitter-css`.
  - Extracts CSS rule selectors.
  - Matches selectors against parsed HTML elements to emit `STYLES` relationships.
  - Supports inferred dynamic class styling (e.g. `.message.error` on `#message` toggled dynamically in JS).
  - Flags unused CSS selectors (e.g. `.unused-banner`).

### Stage 3: Multi-Language Analysis Service & CLI
- **`AnalysisService`**:
  - Coordinates multi-language execution in correct dependency order (HTML $\to$ Python & JS $\to$ CSS).
  - Merges and deduplicates entities and relationships.
  - Produces a unified `RepositoryAnalysis` object.
- **`cli.py`**:
  - Supports `python -m repomind.cli ingest <path>` (file inventory).
  - Supports `python -m repomind.cli analyze <path>` (deep AST analysis with human-readable summary).
  - Supports `--json` flag for machine-readable JSON output.
- **Benchmark Evaluation**:
  - Verified 100% against `test_apps/login_app/ground_truth.json` (all 34 entities and 34 relationships matched).

---

## 4. Commands & How to Run

Always use the virtual environment Python in `.venv`:

### Run Full Test Suite
```powershell
& ".\.venv\Scripts\python.exe" -m pytest -c backend/pytest.ini backend/tests
```

### Run Repository Ingestion (CLI)
```powershell
$env:PYTHONPATH="backend"; & ".\.venv\Scripts\python.exe" -m repomind.cli ingest test_apps/login_app
```

### Run Deep Multi-Language Analysis (CLI)
```powershell
$env:PYTHONPATH="backend"; & ".\.venv\Scripts\python.exe" -m repomind.cli analyze test_apps/login_app
```

### Run Analysis with JSON Output
```powershell
$env:PYTHONPATH="backend"; & ".\.venv\Scripts\python.exe" -m repomind.cli analyze test_apps/login_app --json
```

---

## 5. Remaining Roadmap & Detailed Implementation Specifications

The remaining work is divided into 4 coherent stages (Stages 4 through 7).

```
[Stages 0–3: DONE (60%)] ➔ [Stage 4: Graph (15%)] ➔ [Stage 5: Impact (10%)] ➔ [Stage 6: API & UI (10%)] ➔ [Stage 7: RAG (5%)]
```

---

### STAGE 4: Repository Knowledge Graph (`backend/repomind/graph/`)

**Goal**: Load the `RepositoryAnalysis` into an in-memory graph using `networkx.MultiDiGraph`, enabling graph traversal and path queries.

#### Files to Create:
1. `backend/repomind/graph/__init__.py`
2. `backend/repomind/graph/repository_graph.py`
3. `backend/tests/test_graph.py`

#### Key Class: `RepositoryGraph`
```python
import networkx as nx
from repomind.models import RepositoryAnalysis, Entity, Relationship

class RepositoryGraph:
    def __init__(self):
        self.g = nx.MultiDiGraph()
        self.entity_lookup: dict[str, Entity] = {}

    @classmethod
    def from_analysis(cls, analysis: RepositoryAnalysis) -> "RepositoryGraph":
        # 1. Add all analysis.entities as nodes:
        #    node_id = entity.id
        #    attributes = { "type": entity.type.value, "name": entity.name, "language": entity.language.value, "location": entity.location, ... }
        # 2. Add all analysis.relationships as directed edges:
        #    u = rel.source_id, v = rel.target_id
        #    key = rel.type.value
        #    attributes = { "type": rel.type.value, "kind": rel.kind.value, "confidence": rel.confidence, "evidence": rel.evidence }
        pass

    def get_node(self, node_id: str) -> dict | None: ...
    def get_successors(self, node_id: str) -> list[str]: ...   # outgoing
    def get_predecessors(self, node_id: str) -> list[str]: ...  # incoming

    def find_paths(self, source_id: str, target_id: str, max_depth: int = 10) -> list[list[str]]:
        """Return all simple execution paths from source component to target component."""
        return list(nx.all_simple_paths(self.g, source=source_id, target=target_id, cutoff=max_depth))

    def get_subgraph(self, node_ids: set[str]) -> dict:
        """Export subgraph as { 'nodes': [...], 'edges': [...] } for UI visualizers (Cytoscape/D3)."""
        pass
```

#### Acceptance Criteria for Stage 4:
- `test_graph.py` tests node and edge insertion from `login_app` analysis.
- `find_paths("html:static/index.html:#login-form", "python:repositories/user_repository.py:UserRepository.find_by_username")` returns the complete 5-hop path:
  `#login-form` $\to$ `handleLogin` $\to$ `api:POST:/api/login` $\to$ `login` $\to$ `AuthService.authenticate` $\to$ `UserRepository.find_by_username`.

---

### STAGE 5: Code Flow Tracing & Change-Impact / Blast-Radius Analysis

**Goal**: Determine the ripple effect of changing any component in the repository.

#### Files to Create:
1. `backend/repomind/services/impact_service.py`
2. `backend/repomind/models/impact.py` (`ImpactReport`, `ImpactedComponent`)
3. `backend/tests/test_impact.py`

#### The Concept: Blast Radius
If a developer changes `UserRepository.find_by_username`:
- What calls this directly? (`AuthService.authenticate`)
- What calls `AuthService.authenticate`? (`app.py:login`)
- What API endpoint does `login` handle? (`api:POST:/api/login`)
- What JavaScript function requests this endpoint? (`handleLogin`)
- What HTML element triggers `handleLogin`? (`#login-form`)
- **Result**: Modifying the database repository method impacts the login form UI!

#### Implementation Logic:
```python
class ImpactService:
    def __init__(self, graph: RepositoryGraph):
        self.graph = graph

    def analyze_impact(self, target_entity_id: str, max_depth: int = 10) -> ImpactReport:
        # Traverse reverse edges (predecessors) using BFS or DFS
        # Classify impact level based on distance:
        # Depth 1: Direct Impact
        # Depth 2+: Transitive / Indirect Impact
        # Group by language and component type
        pass
```

#### CLI Extension:
Add impact command:
```powershell
python -m repomind.cli impact test_apps/login_app --target "python:repositories/user_repository.py:UserRepository.find_by_username"
```

---

### STAGE 6: FastAPI REST API & Web Dashboard

**Goal**: Provide a clean HTTP interface and an interactive visual browser dashboard for viva demonstrations.

#### Files to Create:
1. `backend/repomind/api/app.py`
2. `backend/repomind/api/routes.py`
3. `backend/repomind/api/static/index.html` (interactive graph viewer using Cytoscape.js or Vis.js via CDN)
4. `backend/tests/test_api.py`

#### API Endpoints:
- `POST /api/analyze?location=...`: Runs analysis and returns repository summary.
- `GET /api/graph`: Returns graph nodes & edges in JSON format.
- `GET /api/trace?source=...&target=...`: Returns paths connecting two components.
- `GET /api/impact?entity_id=...`: Returns change-impact report for a selected component.

#### Running the Server:
```powershell
& ".\.venv\Scripts\uvicorn.exe" repomind.api.app:app --reload --port 8000
```

---

### STAGE 7: Graph-Augmented RAG / Repository Q&A

**Goal**: Combine the factual knowledge graph with an LLM prompt to answer repository architectural questions without hallucinations.

#### The Problem with Naive RAG:
A normal RAG agent retrieves isolated text snippets and guesses how they connect. It frequently hallucinates frontend-backend connections.

#### The RepoMind Approach:
1. User asks: *"How does the user login flow work in this codebase?"*
2. RepoMind queries `RepositoryGraph.find_paths("html:static/index.html:#login-form", "python:repositories/user_repository.py:UserRepository.find_by_username")`.
3. RepoMind formats the exact path and file:line evidence snippets into an LLM prompt.
4. The LLM generates a clear, 100% verified explanation citing exact lines of code.

---

## 6. Important Guidelines for the Next AI / Developer

1. **Preserve Existing Implementations**: Do not rewrite or modify Stages 0–3 unnecessarily. They are fully working and tested.
2. **Always Run Tests**: Before and after introducing changes, run:
   ```powershell
   & ".\.venv\Scripts\python.exe" -m pytest -c backend/pytest.ini backend/tests
   ```
3. **Keep Code Simple & Explainable**: This is a BTech Capstone project. Readable, modular, and defensible code is vastly superior to over-engineered abstractions.
4. **Follow the Ground Truth Convention**: Whenever validating features, test against `test_apps/login_app/` and its `ground_truth.json`.
