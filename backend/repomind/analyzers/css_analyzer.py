import logging
import re
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
from . import css_selectors as cs
from .base import AnalysisResult, BaseAnalyzer

log = logging.getLogger(__name__)


class CSSAnalyzer(BaseAnalyzer):
    """
    Parses CSS stylesheets using Tree-sitter.
    Extracts:
    - CSS rule selectors (EntityType.CSS_SELECTOR)
    - STYLES relationships targeting matching HTML elements (compound, descendant/child, attribute and
      pseudo-class selectors; see css_selectors.py)
    """

    def __init__(self):
        self.language = tree_sitter.Language(tscss.language())
        self.parser = tree_sitter.Parser(self.language)

    def analyze_file(
        self,
        rel_path: str,
        content: str,
        html_elements: list[Entity] | None = None,
        dynamic_classes: dict[str, set[str]] | None = None,
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
            individual_selectors = cs.split_top_level(raw_selector)

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
                        dynamic_classes=dynamic_classes,
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
        dynamic_classes: dict[str, set[str]] | None = None,
    ) -> None:
        """Match one CSS selector against known HTML element entities (see css_selectors for the
        supported syntax). Anything unsupported matches nothing - never a guess. Match kinds:
          detected - tag/id/classes/attributes/structure all hold in the static HTML
          inferred - the element matches only in some state (:hover, ::before ...) or only because
                     JavaScript is seen adding the missing class to it
        """
        parsed = cs.parse_selector(selector)
        if parsed is None:
            return
        if len(parsed.parts) == 1 and parsed.parts[0].is_universal and not parsed.parts[0].pseudo_elements:
            return                                          # bare `*` would "match" the whole page
        if any(len(set(c.ids)) > 1 for c in parsed.parts):
            return

        for elem in html_elements:
            if elem.type != EntityType.HTML_ELEMENT:
                continue
            ancestors, own = self._descriptors(elem)
            elem_id = elem.metadata.get("id", "")
            extra = frozenset((dynamic_classes or {}).get(elem_id, ())) if elem_id else frozenset()
            notes: list[str] = []
            outcome = cs.match(parsed, [*ancestors, own], extra, notes)
            if outcome == cs.NO:
                continue
            if outcome == cs.YES:
                kind, conf = RelationKind.DETECTED, 1.0
            else:
                kind, conf = RelationKind.INFERRED, 0.85 if set(notes) == {"dynamic"} else 0.8
            result.add_relationship(
                Relationship(
                    source_id=selector_entity.id,
                    target_id=elem.id,
                    type=RelationType.STYLES,
                    kind=kind,
                    confidence=conf,
                    evidence=[Evidence(snippet=snippet, location=loc)],
                )
            )

    @staticmethod
    def _descriptors(elem: Entity) -> tuple[list[dict], dict]:
        """(ancestor descriptors, own descriptor) from the DOM info the HTML analyzer stored. Without it
        (older saved analyses) only the element itself is known: combinators and :first-child cannot match."""
        dom = elem.metadata.get("dom")
        if dom:
            return dom["path"], dom["self"]
        meta = elem.metadata
        return [], {"t": meta.get("tag", "").lower(), "i": meta.get("id", ""), "c": list(meta.get("classes", [])),
                    "a": dict(meta.get("attributes", {})), "n": None, "s": None}
