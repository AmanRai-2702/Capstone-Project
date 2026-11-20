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
    Relationship,
    RepositoryAnalysis,
    RepositoryInventory,
)
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

    def analyze(self, location: str) -> RepositoryAnalysis:
        """Ingest repository from path/URL and perform full multi-language analysis."""
        repo_service = RepositoryService(self.settings)
        inventory = repo_service.ingest(location)
        return self.analyze_inventory(inventory)

    def analyze_inventory(self, inventory: RepositoryInventory) -> RepositoryAnalysis:
        """Analyze all source files in an already-ingested inventory."""
        root = Path(inventory.root_path)

        all_entities: dict[str, Entity] = {}
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
        for hf in html_files:
            content = self._read_file(root / hf.path)
            res = self.html_analyzer.analyze_file(hf.path, content)
            self._merge_results(res.entities, res.relationships, all_entities, all_relationships)
            html_elements.extend(res.entities_by_type(EntityType.HTML_ELEMENT))

        # Phase 2: Parse Python files
        for pf in python_files:
            content = self._read_file(root / pf.path)
            res = self.python_analyzer.analyze_file(pf.path, content)
            self._merge_results(res.entities, res.relationships, all_entities, all_relationships)

        # Phase 3: Parse JavaScript files
        for jf in js_files:
            content = self._read_file(root / jf.path)
            res = self.javascript_analyzer.analyze_file(jf.path, content)
            self._merge_results(res.entities, res.relationships, all_entities, all_relationships)

        # Phase 4: Parse CSS files, passing discovered HTML elements for STYLES resolution
        for cf in css_files:
            content = self._read_file(root / cf.path)
            res = self.css_analyzer.analyze_file(cf.path, content, html_elements=html_elements)
            self._merge_results(res.entities, res.relationships, all_entities, all_relationships)

        analysis = RepositoryAnalysis(
            repo_id=inventory.repo_id,
            name=inventory.name,
            origin=inventory.origin,
            root_path=inventory.root_path,
            entities=list(all_entities.values()),
            relationships=list(all_relationships.values()),
            stats={
                "source_files_count": len(source_files),
                "total_files_count": len(inventory.files),
            },
        )
        log.info(
            "Analyzed %s: %d entities, %d relationships",
            inventory.name,
            analysis.entity_count,
            analysis.relationship_count,
        )
        return analysis

    def _read_file(self, full_path: Path) -> str:
        try:
            return full_path.read_text(encoding="utf-8")
        except Exception as e:
            log.warning("Failed to read %s: %s", full_path, e)
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
