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
                        dynamic_classes=dynamic_classes,
                    )

        return result

    # tag?, then any number of #id / .class parts:  button.btn  #toast.visible  .message.error
    _COMPOUND = re.compile(r"^(?P<tag>[A-Za-z][\w-]*)?(?P<rest>(?:[#.][\w-]+)*)$")

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
        """Match one CSS selector against known HTML element entities.

        Supported: compound selectors of tag / #id / .class parts. Descendant/child combinators,
        attribute selectors and pseudo-classes are NOT supported and produce no match (never a
        guess). Match kinds:
          detected - the element has every required tag/id/class
          inferred - only classes are missing, and JS is seen adding all of them to that element
        """
        m = self._COMPOUND.match(selector.strip())
        if not m or not (m["tag"] or m["rest"]):
            return
        tag = (m["tag"] or "").lower()
        ids = re.findall(r"#([\w-]+)", m["rest"])
        classes = set(re.findall(r"\.([\w-]+)", m["rest"]))
        if len(set(ids)) > 1:
            return

        for elem in html_elements:
            if elem.type != EntityType.HTML_ELEMENT:
                continue
            elem_id = elem.metadata.get("id", "")
            if tag and elem.metadata.get("tag", "").lower() != tag:
                continue
            if ids and ids[0] != elem_id:
                continue
            missing = classes - set(elem.metadata.get("classes", []))
            if not missing:
                kind, conf = RelationKind.DETECTED, 1.0
            elif elem_id and missing <= (dynamic_classes or {}).get(elem_id, set()):
                kind, conf = RelationKind.INFERRED, 0.85
            else:
                continue
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
