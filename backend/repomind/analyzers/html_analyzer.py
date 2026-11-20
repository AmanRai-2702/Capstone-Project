import logging
from pathlib import PurePosixPath

import tree_sitter
import tree_sitter_html as tshtml

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


def _strip_quotes(text: str) -> str:
    text = text.strip()
    if len(text) >= 2 and text[0] in ("'", '"') and text[-1] == text[0]:
        return text[1:-1]
    return text


def _clean_asset_path(raw_path: str, html_rel_path: str) -> str:
    """Normalize asset link path (e.g. '/static/login.js' or 'login.js' -> 'static/login.js')."""
    clean = raw_path.strip().lstrip("/")
    return clean


class HTMLAnalyzer(BaseAnalyzer):
    """
    Parses HTML documents using Tree-sitter.
    Extracts:
    - DOM elements with IDs (EntityType.HTML_ELEMENT)
    - DOM hierarchy containment (CONTAINS)
    - Linked assets (<script src="...">, <link href="...">) as REFERENCES
    """

    def __init__(self):
        self.language = tree_sitter.Language(tshtml.language())
        self.parser = tree_sitter.Parser(self.language)

    def analyze_file(self, rel_path: str, content: str) -> AnalysisResult:
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

        # Walk AST tracking parent elements that have IDs
        id_ancestor_stack: list[str] = []

        def walk(node: tree_sitter.Node) -> None:
            nonlocal id_ancestor_stack
            pushed_id = None

            if node.type in ("element", "script_element", "style_element"):
                tag_name, attrs = self._parse_element_tag_and_attrs(node)
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

                # Asset reference: <script src="...">
                if tag_name == "script" and "src" in attrs:
                    asset_rel = _clean_asset_path(attrs["src"], rel_path)
                    target_file_id = make_file_id(asset_rel)
                    result.add_relationship(
                        Relationship(
                            source_id=file_id,
                            target_id=target_file_id,
                            type=RelationType.REFERENCES,
                            kind=RelationKind.DETECTED,
                            confidence=1.0,
                            evidence=[Evidence(snippet=snippet, location=loc)],
                        )
                    )

                # Asset reference: <link rel="stylesheet" href="...">
                elif tag_name == "link" and "href" in attrs:
                    asset_rel = _clean_asset_path(attrs["href"], rel_path)
                    target_file_id = make_file_id(asset_rel)
                    result.add_relationship(
                        Relationship(
                            source_id=file_id,
                            target_id=target_file_id,
                            type=RelationType.REFERENCES,
                            kind=RelationKind.DETECTED,
                            confidence=1.0,
                            evidence=[Evidence(snippet=snippet, location=loc)],
                        )
                    )

                # HTML Element with ID
                if "id" in attrs:
                    raw_id = attrs["id"]
                    elem_selector = f"#{raw_id}"
                    entity_id = f"html:{rel_path}:{elem_selector}"
                    classes = attrs.get("class", "").split()

                    entity = Entity(
                        id=entity_id,
                        type=EntityType.HTML_ELEMENT,
                        name=elem_selector,
                        qualified_name=elem_selector,
                        language=Language.HTML,
                        location=loc,
                        parent_id=id_ancestor_stack[-1] if id_ancestor_stack else file_id,
                        metadata={
                            "tag": tag_name,
                            "id": raw_id,
                            "classes": classes,
                            "attributes": attrs,
                        },
                    )
                    result.add_entity(entity)

                    # Containment relationship from nearest ancestor with an ID
                    if id_ancestor_stack:
                        parent_elem_id = id_ancestor_stack[-1]
                        result.add_relationship(
                            Relationship(
                                source_id=parent_elem_id,
                                target_id=entity_id,
                                type=RelationType.CONTAINS,
                                kind=RelationKind.DETECTED,
                                confidence=1.0,
                                evidence=[Evidence(snippet=snippet, location=loc)],
                            )
                        )

                    id_ancestor_stack.append(entity_id)
                    pushed_id = entity_id

            for child in node.children:
                walk(child)

            if pushed_id:
                id_ancestor_stack.pop()

        walk(tree.root_node)
        return result

    def _parse_element_tag_and_attrs(self, node: tree_sitter.Node) -> tuple[str, dict[str, str]]:
        tag_name = ""
        attrs: dict[str, str] = {}

        for child in node.children:
            if child.type == "start_tag" or child.type == "self_closing_tag":
                for item in child.children:
                    if item.type == "tag_name":
                        tag_name = item.text.decode("utf-8").lower()
                    elif item.type == "attribute":
                        name, val = self._parse_attribute(item)
                        if name:
                            attrs[name] = val
                break
            elif child.type == "tag_name" and not tag_name:
                tag_name = child.text.decode("utf-8").lower()
            elif child.type == "attribute":
                name, val = self._parse_attribute(child)
                if name:
                    attrs[name] = val

        return tag_name, attrs

    def _parse_attribute(self, attr_node: tree_sitter.Node) -> tuple[str, str]:
        name = ""
        val = ""
        for c in attr_node.children:
            if c.type == "attribute_name":
                name = c.text.decode("utf-8").lower()
            elif c.type in ("attribute_value", "quoted_attribute_value"):
                val = _strip_quotes(c.text.decode("utf-8"))
        return name, val
