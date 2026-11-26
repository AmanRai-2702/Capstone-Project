import logging
from pathlib import Path
from typing import Any

from repomind.analyzers import (
    CSSAnalyzer,
    HTMLAnalyzer,
    JavaScriptAnalyzer,
    PythonAnalyzer,
)
from repomind.config import Settings
from repomind.models import (
    Entity,
    EntityType,
    FileCategory,
    Language,
    RelationType,
    Relationship,
    RepositoryAnalysis,
    RepositoryInventory,
)
from repomind.resolution import RelationshipResolver
from .repository_service import RepositoryService

log = logging.getLogger(__name__)


class AnalysisService:
    """
    Coordinates multi-language repository analysis:
    1. Ingests file inventory.
    2. Dispatches source files to their respective AST/Tree-sitter analyzers.
    3. Bridges cross-language relationships (e.g. HTML elements to CSS selectors).
    4. Combines results into a unified RepositoryAnalysis model.
    """

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or Settings.from_env()
        self.python_analyzer = PythonAnalyzer()
        self.javascript_analyzer = JavaScriptAnalyzer()
        self.html_analyzer = HTMLAnalyzer()
        self.css_analyzer = CSSAnalyzer()

    def analyze(self, location: str, exclude: list[str] | None = None) -> RepositoryAnalysis:
        """Ingest repository from path/URL/zip and perform full multi-language analysis."""
        repo_service = RepositoryService(self.settings)
        inventory = repo_service.ingest(location, exclude=exclude)
        return self.analyze_inventory(inventory)

    def analyze_inventory(self, inventory: RepositoryInventory) -> RepositoryAnalysis:
        """Analyze all source files in an already-ingested inventory."""
        root = Path(inventory.root_path)
        self._read_errors: list[str] = []

        all_entities: dict[str, Entity] = {}
        errors: list[str] = []
        all_relationships: dict[str, Relationship] = {}

        # 1. Add all files in the inventory as FILE entities
        for f in inventory.files:
            file_entity = f.to_entity()
            all_entities[file_entity.id] = file_entity

        # Separate files by language
        source_files = [f for f in inventory.files if f.category == FileCategory.SOURCE]
        html_files = [f for f in source_files if f.language == Language.HTML]
        python_files = [f for f in source_files if f.language == Language.PYTHON]
        js_files = [f for f in source_files if f.language == Language.JAVASCRIPT]
        css_files = [f for f in source_files if f.language == Language.CSS]

        # Phase 1: Parse HTML files first to discover DOM structure and element IDs
        html_elements: list[Entity] = []
        inline_scripts: list[tuple[str, int, str]] = []        # (html path, line offset, js text)
        for hf in html_files:
            content = self._read_file(root / hf.path)
            res = self._safe(self.html_analyzer.analyze_file, hf.path, content, errors=errors)
            self._merge_results(res.entities, res.relationships, all_entities, all_relationships)
            html_elements.extend(res.entities_by_type(EntityType.HTML_ELEMENT))
            inline_scripts.extend((hf.path, offset, text) for offset, text in res.inline_scripts)

        # Phase 2: Parse Python files
        py_sources = {pf.path: self._read_file(root / pf.path) for pf in python_files}
        self.python_analyzer.prepare(py_sources)          # repository-wide return/attribute types
        for pf in python_files:
            content = py_sources[pf.path]
            res = self._safe(self.python_analyzer.analyze_file, pf.path, content, errors=errors)
            self._merge_results(res.entities, res.relationships, all_entities, all_relationships)

        # Phase 3: Parse JavaScript files
        dynamic_classes: dict[str, set[str]] = {}
        # Pre-pass: which functions are DOM-lookup helpers (`const $ = s => document.querySelector(s)`),
        # so files that import them are analysed correctly whatever the file order.
        js_sources = {jf.path: self._read_file(root / jf.path) for jf in js_files}
        self.javascript_analyzer.prepare(js_sources)
        for jf in js_files:
            content = js_sources[jf.path]
            res = self._safe(self.javascript_analyzer.analyze_file, jf.path, content, errors=errors)
            for el_id, classes in res.dynamic_classes.items():
                dynamic_classes.setdefault(el_id, set()).update(classes)
            self._merge_results(res.entities, res.relationships, all_entities, all_relationships)

        # Phase 3b: inline <script> blocks, analysed as JavaScript that lives in the HTML file
        for html_path, offset, text in inline_scripts:
            res = self._safe(self.javascript_analyzer.analyze_file, html_path, text, errors=errors)
            self._shift_lines(res, offset)
            for el_id, classes in res.dynamic_classes.items():
                dynamic_classes.setdefault(el_id, set()).update(classes)
            self._merge_results(res.entities, res.relationships, all_entities, all_relationships)

        # Phase 4: Parse CSS files, passing discovered HTML elements for STYLES resolution
        for cf in css_files:
            content = self._read_file(root / cf.path)
            # A stylesheet only styles pages that link it. If no page links it (template-injected,
            # for example) we cannot tell, so fall back to considering every page.
            css_id = f"file:{cf.path}"
            pages = {r.source_id.removeprefix("file:") for r in all_relationships.values()
                     if r.type == RelationType.REFERENCES and r.target_id == css_id
                     and r.source_id.lower().endswith((".html", ".htm"))}
            candidates = [e for e in html_elements if e.id.split(":", 2)[1] in pages] if pages else html_elements
            res = self._safe(self.css_analyzer.analyze_file, cf.path, content, errors=errors,
                             html_elements=candidates, dynamic_classes=dynamic_classes)
            self._merge_results(res.entities, res.relationships, all_entities, all_relationships)

        # Resolution pass: connect candidate links across files/languages; whatever cannot be
        # connected is reported (with a reason) instead of silently dropped.
        resolver = RelationshipResolver(all_entities, root_name=inventory.name)
        resolved, unresolved = resolver.resolve(list(all_relationships.values()))
        all_relationships = {r.key: r for r in resolved}
        external: dict[str, int] = {}
        for u in unresolved:
            if u.reason == "external_package":
                external[u.target_id] = external.get(u.target_id, 0) + 1

        analysis = RepositoryAnalysis(
            repo_id=inventory.repo_id,
            name=inventory.name,
            origin=inventory.origin,
            root_path=inventory.root_path,
            entities=list(all_entities.values()),
            relationships=list(all_relationships.values()),
            unresolved=unresolved,
            stats={
                "source_files_count": len(source_files),
                "total_files_count": len(inventory.files),
                "unresolved_references": dict(sorted(external.items())),
                "analysis_errors": errors + self._read_errors,
                "skipped_files": inventory.skipped,
                # DATA_FLOW / READS_STATE / RETURNS links whose far end could not be resolved (a library call...)
                "dropped_unresolved_enrichment_links": resolver.dropped_enrichment,
            },
        )
        log.info(
            "Analyzed %s: %d entities, %d relationships",
            inventory.name,
            analysis.entity_count,
            analysis.relationship_count,
        )
        return analysis

    @staticmethod
    def _shift_lines(res, offset: int) -> None:
        """Inline-script line numbers are relative to the script text; make them file line numbers."""
        if not offset:
            return
        for e in res.entities:
            if e.location:
                e.location = e.location.model_copy(update={"start_line": e.location.start_line + offset,
                                                           "end_line": e.location.end_line + offset})
        for r in res.relationships:
            for ev in r.evidence:
                if ev.location:
                    ev.location = ev.location.model_copy(update={"start_line": ev.location.start_line + offset,
                                                                 "end_line": ev.location.end_line + offset})

    @staticmethod
    def _safe(fn, path: str, content: str, errors: list[str], **kwargs):
        """Run one analyzer on one file; a crash is recorded, never fatal to the whole run."""
        from repomind.analyzers.base import AnalysisResult
        try:
            res = fn(path, content, **kwargs)
        except Exception as exc:  # malformed or hostile input must not take the analysis down
            log.exception("Analyzer failed on %s", path)
            errors.append(f"{path}: analyzer crashed ({type(exc).__name__}: {exc})")
            return AnalysisResult()
        errors.extend(res.errors)
        return res

    def _read_file(self, full_path: Path) -> str:
        try:
            return full_path.read_text(encoding="utf-8")
        except Exception as e:
            log.warning("Failed to read %s: %s", full_path, e)
            self._read_errors.append(f"{full_path.name}: unreadable ({type(e).__name__})")
            return ""

    def _merge_results(
        self,
        entities: list[Entity],
        relationships: list[Relationship],
        all_entities: dict[str, Entity],
        all_relationships: dict[str, Relationship],
    ) -> None:
        for e in entities:
            if e.id not in all_entities:
                all_entities[e.id] = e
        for r in relationships:
            if r.key not in all_relationships:
                all_relationships[r.key] = r
