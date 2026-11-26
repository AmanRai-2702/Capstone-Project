import logging
import posixpath
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


# Tags kept even without an id/class; structural/metadata tags are never kept for their class alone.
NOTABLE_TAGS = {"form", "button", "input", "select", "textarea", "a"}
IGNORED_TAGS = {"html", "head", "body", "script", "style", "link", "meta", "title"}


def _strip_quotes(text: str) -> str:
    text = text.strip()
    if len(text) >= 2 and text[0] in ("'", '"') and text[-1] == text[0]:
        return text[1:-1]
    return text


def _is_local(raw_path: str) -> bool:
    """False for CDN / absolute URLs and data URIs, which are not files of this repository."""
    return not raw_path.strip().lower().startswith(("http://", "https://", "//", "data:", "mailto:"))


def _clean_asset_path(raw_path: str, html_rel_path: str) -> str:
    """Repo-relative path of a linked asset.
    '/static/login.js'  -> 'static/login.js'            (root-relative)
    'js/app.js' in 'static/index.html' -> 'static/js/app.js'   (relative to the page)
    """
    clean = raw_path.strip().split("?")[0].split("#")[0]
    if clean.startswith("/"):
        return clean.lstrip("/")
    return posixpath.normpath(posixpath.join(posixpath.dirname(html_rel_path), clean))


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
        # Full DOM position of every element (also those that do not become entities), so the CSS matcher
        # can evaluate `a > b`, `a b` and `:first-child` without guessing about missing ancestors.
        dom_stack: list[dict] = []                 # ancestors of the element being visited
        sibling_counters: list[list[int]] = [[0]]  # one counter of element children per open element
        dom_info: dict[str, tuple[list[dict], dict]] = {}   # entity id -> (ancestor descriptors, own descriptor)

        def walk(node: tree_sitter.Node) -> None:
            nonlocal id_ancestor_stack
            pushed_id = None
            pushed_dom = False

            if node.type in ("element", "script_element", "style_element"):
                tag_name, attrs = self._parse_element_tag_and_attrs(node)
                counter = sibling_counters[-1]
                descriptor = {"t": tag_name, "i": attrs.get("id", ""), "c": attrs.get("class", "").split(),
                              "a": {k: v[:200] for k, v in attrs.items()}, "n": counter[0], "_sib": counter}
                counter[0] += 1
                ancestors = list(dom_stack)
                dom_stack.append(descriptor)
                sibling_counters.append([0])
                pushed_dom = True
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

                # Inline script: remember its text and where it starts so the JS analyzer can read it
                if tag_name == "script" and "src" not in attrs:
                    raw = next((c for c in node.children if c.type == "raw_text"), None)
                    if raw is not None and raw.text.strip():
                        result.inline_scripts.append((raw.start_point[0], raw.text.decode("utf-8", errors="replace")))

                # Asset reference: <script src="...">
                if tag_name == "script" and "src" in attrs and _is_local(attrs["src"]):
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
                elif tag_name == "link" and "href" in attrs and _is_local(attrs["href"]):
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
                # Navigation link to another page: <a href="archive.html">
                if tag_name == "a" and attrs.get("href", "").split("#")[0].lower().endswith((".html", ".htm")) \
                        and not attrs["href"].startswith(("http://", "https://")):
                    result.add_relationship(
                        Relationship(
                            source_id=file_id,
                            target_id=make_file_id(_clean_asset_path(attrs["href"].split("#")[0], rel_path)),
                            type=RelationType.REFERENCES,
                            kind=RelationKind.DETECTED,
                            confidence=1.0,
                            evidence=[Evidence(snippet=snippet, location=loc)],
                        )
                    )

                # Elements with an id are always entities. Elements without one are kept when they
                # matter for the application: form controls, links, or anything with a class
                # (so CSS class selectors have something to match).
                if "id" in attrs or tag_name in NOTABLE_TAGS or (
                        "class" in attrs and tag_name not in IGNORED_TAGS):
                    raw_id = attrs.get("id", "")
                    elem_selector = f"#{raw_id}" if raw_id else f"{tag_name}@{start_line}"
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
                    dom_info[entity_id] = (ancestors, descriptor)

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
            if pushed_dom:
                dom_stack.pop()
                sibling_counters.pop()

        walk(tree.root_node)

        # Sibling counts are only known once the parents are closed: finalise the descriptors now.
        def finish(d: dict) -> dict:
            return {"t": d["t"], "i": d["i"], "c": d["c"], "a": d["a"], "n": d["n"], "s": d["_sib"][0]}
        for entity in result.entities:
            if entity.id in dom_info:
                ancestors, own = dom_info[entity.id]
                entity.metadata["dom"] = {"path": [finish(a) for a in ancestors], "self": finish(own)}
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
