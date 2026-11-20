import logging
from pathlib import PurePosixPath

import tree_sitter
import tree_sitter_css as tscss

from repomind.models import (
    Entity,
    EntityType,
    Evidence,
    Language,
    RelationKind,
    RelationType,
    Relationship,
    SourceLocation,
    make_file_id,
)
from .base import AnalysisResult, BaseAnalyzer

log = logging.getLogger(__name__)


class CSSAnalyzer(BaseAnalyzer):
    """
    Parses CSS stylesheets using Tree-sitter.
    Extracts:
    - CSS rule selectors (EntityType.CSS_SELECTOR)
    - STYLES relationships targeting matching HTML elements
    """

    def __init__(self):
        self.language = tree_sitter.Language(tscss.language())
        self.parser = tree_sitter.Parser(self.language)

    def analyze_file(
        self,
        rel_path: str,
        content: str,
        html_elements: list[Entity] | None = None,
    ) -> AnalysisResult:
        rel_path = rel_path.replace("\\", "/")
        source_bytes = content.encode("utf-8")
        source_lines = content.splitlines()
        result = AnalysisResult()
        file_id = make_file_id(rel_path)

        tree = self.parser.parse(source_bytes)
        if not tree.root_node:
            return result

        def get_snippet(start_line: int, end_line: int) -> str:
            if 1 <= start_line <= len(source_lines):
                end = min(end_line, len(source_lines))
                return "\n".join(source_lines[start_line - 1 : end]).strip()
            return ""

        # Walk AST to find rule_sets
        for node in tree.root_node.named_children:
            if node.type != "rule_set":
                continue

            selectors_node = None
            for child in node.named_children:
                if child.type == "selectors":
                    selectors_node = child
                    break

            if not selectors_node:
                continue

            raw_selector = selectors_node.text.decode("utf-8").strip()
            # Split comma-separated selector lists, or handle individual selector
            individual_selectors = [s.strip() for s in raw_selector.split(",") if s.strip()]

            start_line = node.start_point[0] + 1
            end_line = node.end_point[0] + 1
            loc = SourceLocation(
                file_path=rel_path,
                start_line=start_line,
                end_line=end_line,
                start_col=node.start_point[1],
                end_col=node.end_point[1],
            )
            snippet = get_snippet(start_line, end_line)

            for sel in individual_selectors:
                selector_entity_id = f"css:{rel_path}:{sel}"
                selector_entity = Entity(
                    id=selector_entity_id,
                    type=EntityType.CSS_SELECTOR,
                    name=sel,
                    qualified_name=sel,
                    language=Language.CSS,
                    location=loc,
                    parent_id=file_id,
                    metadata={"selector": sel},
                )
                result.add_entity(selector_entity)

                # File CONTAINS Selector
                result.add_relationship(
                    Relationship(
                        source_id=file_id,
                        target_id=selector_entity_id,
                        type=RelationType.CONTAINS,
                        kind=RelationKind.DETECTED,
                        confidence=1.0,
                    )
                )

                # If HTML elements are provided, match and emit STYLES relationships
                if html_elements:
                    self._match_styles(
                        selector_entity=selector_entity,
                        selector=sel,
                        html_elements=html_elements,
                        loc=loc,
                        snippet=snippet,
                        result=result,
                    )

        return result

    def _match_styles(
        self,
        selector_entity: Entity,
        selector: str,
        html_elements: list[Entity],
        loc: SourceLocation,
        snippet: str,
        result: AnalysisResult,
    ) -> None:
        """Match CSS selector against known HTML element entities."""
        for elem in html_elements:
            if elem.type != EntityType.HTML_ELEMENT:
                continue

            elem_id_attr = elem.metadata.get("id", "")
            elem_classes = set(elem.metadata.get("classes", []))

            # Case 1: ID selector, e.g. #login-card
            if selector.startswith("#") and selector[1:] == elem_id_attr:
                result.add_relationship(
                    Relationship(
                        source_id=selector_entity.id,
                        target_id=elem.id,
                        type=RelationType.STYLES,
                        kind=RelationKind.DETECTED,
                        confidence=1.0,
                        evidence=[Evidence(snippet=snippet, location=loc)],
                    )
                )

            # Case 2: Class selector, e.g. .btn, .btn.primary, .field, .message.error
            elif selector.startswith("."):
                req_classes = set([c for c in selector.split(".") if c])

                # Exact / Subset match (element has all required classes)
                if req_classes and req_classes <= elem_classes:
                    result.add_relationship(
                        Relationship(
                            source_id=selector_entity.id,
                            target_id=elem.id,
                            type=RelationType.STYLES,
                            kind=RelationKind.DETECTED,
                            confidence=1.0,
                            evidence=[Evidence(snippet=snippet, location=loc)],
                        )
                    )

                # Inferred match: compound selector where element has base class but lacks secondary
                # (e.g. .message.error matching <p id="message" class="message"> toggled dynamically by JS)
                elif len(req_classes) > 1 and len(req_classes & elem_classes) >= 1:
                    result.add_relationship(
                        Relationship(
                            source_id=selector_entity.id,
                            target_id=elem.id,
                            type=RelationType.STYLES,
                            kind=RelationKind.INFERRED,
                            confidence=0.85,
                            evidence=[Evidence(snippet=snippet, location=loc)],
                        )
                    )
