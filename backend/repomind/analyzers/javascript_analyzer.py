import logging
import posixpath
import re
from pathlib import PurePosixPath

import tree_sitter
import tree_sitter_javascript as tsjs

from repomind.models import (
    Entity,
    EntityType,
    Evidence,
    Language,
    RelationKind,
    RelationType,
    Relationship,
    SourceLocation,
    make_endpoint_id,
    make_entity_id,
    make_file_id,
)
from .base import AnalysisResult, BaseAnalyzer

log = logging.getLogger(__name__)


def _camel_to_snake(s: str) -> str:
    """Convert camelCase string to snake_case (e.g. 'userId' -> 'user_id')."""
    return re.sub(r"(?<!^)(?=[A-Z])", "_", s).lower()


def _strip_quotes(text: str) -> str:
    """Strip single, double quotes or backticks."""
    text = text.strip()
    if len(text) >= 2 and text[0] in ("'", '"', "`") and text[-1] == text[0]:
        return text[1:-1]
    return text


class JavaScriptAnalyzer(BaseAnalyzer):
    """
    Parses JavaScript source code using Tree-sitter.
    Extracts:
    - Function declarations and arrow functions
    - DOM queries (getElementById, querySelector)
    - Fetch API requests (GET, POST routes)
    - Event listeners (TRIGGERS relationships)
    - Intra-file function calls
    """

    def __init__(self, default_html_path: str | None = None):
        self.language = tree_sitter.Language(tsjs.language())
        self.parser = tree_sitter.Parser(self.language)
        self.default_html_path = default_html_path
        self._imported: dict[str, tuple[str, str]] = {}   # local name -> (repo path, original name)
        # DOM helpers per file (path -> {function name -> "querySelector" | "getElementById"}), filled by
        # prepare() so `import { $ } from "./dom.js"; $("#x")` can be understood while analysing another file.
        self._helper_index: dict[str, dict[str, str]] = {}
        self._dom_helpers: dict[str, str] = {}            # helpers defined in the file being analysed
        self._namespaces: dict[str, str] = {}             # `import * as ns` / `ns = await import(..)` -> repo path
        self._axios: dict[str, str] = {}                  # axios name or instance -> baseURL ("" if none)

    def prepare(self, files: dict[str, str]) -> None:
        """Optional first pass over all JS files (path -> source): records which functions are DOM-lookup
        helpers, so other files that import them can be analysed correctly."""
        self._helper_index = {}
        for path, content in files.items():
            try:
                root = self.parser.parse(content.encode("utf-8")).root_node
            except Exception:                              # a file that cannot be parsed has no helpers
                continue
            helpers = self._find_dom_helpers(root)
            if helpers:
                self._helper_index[path.replace("\\", "/")] = helpers

    @staticmethod
    def _find_dom_helpers(root) -> dict[str, str]:
        """Top-level functions whose body is just `document.querySelector(param)` / `getElementById(param)`
        with their first parameter, e.g. `const $ = (s) => document.querySelector(s)`."""
        def first_param(fn):
            p = fn.child_by_field_name("parameters") or fn.child_by_field_name("parameter")
            if p is None:
                return None
            if p.type == "identifier":
                return p.text.decode("utf-8")
            ids = [c for c in p.named_children if c.type == "identifier"]
            return ids[0].text.decode("utf-8") if ids else None

        def lookup_method(fn, param):
            body = fn.child_by_field_name("body")
            if body is None:
                return None
            if body.type == "statement_block":
                rets = [c for c in body.named_children if c.type == "return_statement"]
                body = rets[0].named_children[0] if len(rets) == 1 and rets[0].named_children else None
            if body is None or body.type != "call_expression":
                return None
            f, a = body.child_by_field_name("function"), body.child_by_field_name("arguments")
            if f is None or f.type != "member_expression" or a is None or not a.named_children:
                return None
            obj, prop = f.child_by_field_name("object"), f.child_by_field_name("property")
            if obj is None or obj.text != b"document" or prop is None:
                return None
            method = prop.text.decode("utf-8")
            arg = a.named_children[0]
            if method in ("querySelector", "getElementById") and arg.type == "identifier" and arg.text.decode("utf-8") == param:
                return method
            return None

        found: dict[str, str] = {}
        for node in root.named_children:
            if node.type == "export_statement":
                node = node.child_by_field_name("declaration") or node
            if node.type == "function_declaration":
                name, param = node.child_by_field_name("name"), first_param(node)
                if name is not None and param and (m := lookup_method(node, param)):
                    found[name.text.decode("utf-8")] = m
            elif node.type == "lexical_declaration":
                for decl in node.named_children:
                    val, name = decl.child_by_field_name("value"), decl.child_by_field_name("name")
                    if val is not None and name is not None and val.type in ("arrow_function", "function"):
                        param = first_param(val)
                        if param and (m := lookup_method(val, param)):
                            found[name.text.decode("utf-8")] = m
        return found

    def _helper_method(self, name: str) -> str | None:
        """"querySelector"/"getElementById" if `name` is a DOM helper defined here or imported."""
        if name in self._dom_helpers:
            return self._dom_helpers[name]
        if name in self._imported:
            path, original = self._imported[name]
            return self._helper_index.get(path, {}).get(original)
        return None

    def _resolve_html_path(self, js_rel_path: str) -> str:
        """Infer paired HTML file (e.g. static/login.js -> static/index.html)."""
        if js_rel_path.lower().endswith((".html", ".htm")):
            return js_rel_path                       # inline <script>: the page itself is the DOM
        if self.default_html_path:
            return self.default_html_path
        parent = PurePosixPath(js_rel_path).parent.as_posix()
        if parent and parent != ".":
            return f"{parent}/index.html"
        return "index.html"

    def analyze_file(self, rel_path: str, content: str) -> AnalysisResult:
        rel_path = rel_path.replace("\\", "/")
        source_bytes = content.encode("utf-8")
        source_lines = content.splitlines()
        result = AnalysisResult()
        file_id = make_file_id(rel_path)
        html_path = self._resolve_html_path(rel_path)
        self._imported = {}
        self._wrappers = set()
        self._namespaces = {}
        self._axios = {}
        self._handler_ids = {}                 # tree-sitter node id of an anonymous handler -> its entity id
        self._handler_entities = set()

        tree = self.parser.parse(source_bytes)
        if not tree.root_node:
            return result
        self._wrappers = self._find_fetch_wrappers(tree.root_node)
        self._dom_helpers = self._find_dom_helpers(tree.root_node)

        def get_snippet(start_line: int, end_line: int) -> str:
            if 1 <= start_line <= len(source_lines):
                end = min(end_line, len(source_lines))
                return "\n".join(source_lines[start_line - 1 : end]).strip()
            return ""

        # Map variable names to DOM selectors, e.g. "form" -> "#login-form"
        var_to_dom: dict[str, str] = {}
        # Known declared function names in this file
        declared_functions: dict[str, str] = {}  # name -> entity_id

        # Pass 1: Discover all declared functions and DOM variable assignments
        for node in tree.root_node.named_children:
            if node.type == "export_statement":          # export function f() {} / export class C {}
                inner = node.child_by_field_name("declaration")
                if inner is None:
                    continue
                node = inner
            if node.type == "import_statement":
                self._handle_import(node, rel_path, file_id, get_snippet, result)
                continue
            if node.type == "class_declaration":
                self._handle_class(node, rel_path, file_id, declared_functions, get_snippet, result)
                continue

            # function showMessage(text, isError) { ... }
            if node.type == "function_declaration":
                name_node = node.child_by_field_name("name")
                if name_node:
                    func_name = name_node.text.decode("utf-8")
                    start_line = node.start_point[0] + 1
                    end_line = node.end_point[0] + 1
                    func_id = make_entity_id("javascript", rel_path, func_name)
                    declared_functions[func_name] = func_id

                    func_entity = Entity(
                        id=func_id,
                        type=EntityType.FUNCTION,
                        name=func_name,
                        qualified_name=func_name,
                        language=Language.JAVASCRIPT,
                        location=SourceLocation(
                            file_path=rel_path,
                            start_line=start_line,
                            end_line=end_line,
                            start_col=node.start_point[1],
                            end_col=node.end_point[1],
                        ),
                        parent_id=file_id,
                    )
                    result.add_entity(func_entity)
                    result.add_relationship(
                        Relationship(
                            source_id=file_id,
                            target_id=func_id,
                            type=RelationType.CONTAINS,
                            kind=RelationKind.DETECTED,
                            confidence=1.0,
                        )
                    )

            # const form = document.getElementById("login-form")
            elif node.type == "lexical_declaration":
                for decl in node.named_children:
                    if decl.type == "variable_declarator":
                        name_node = decl.child_by_field_name("name")
                        val_node = decl.child_by_field_name("value")
                        if name_node and val_node:
                            var_name = name_node.text.decode("utf-8")
                            # If assigning a function: const fn = () => ...
                            if val_node.type in ("arrow_function", "function"):
                                start_line = decl.start_point[0] + 1
                                end_line = decl.end_point[0] + 1
                                func_id = make_entity_id("javascript", rel_path, var_name)
                                declared_functions[var_name] = func_id
                                result.add_entity(
                                    Entity(
                                        id=func_id,
                                        type=EntityType.FUNCTION,
                                        name=var_name,
                                        qualified_name=var_name,
                                        language=Language.JAVASCRIPT,
                                        location=SourceLocation(
                                            file_path=rel_path,
                                            start_line=start_line,
                                            end_line=end_line,
                                        ),
                                        parent_id=file_id,
                                    )
                                )
                                result.add_relationship(
                                    Relationship(
                                        source_id=file_id,
                                        target_id=func_id,
                                        type=RelationType.CONTAINS,
                                        kind=RelationKind.DETECTED,
                                        confidence=1.0,
                                    )
                                )
                            # If assigning a DOM element
                            dom_selector = self._extract_dom_selector_from_node(val_node)
                            if dom_selector:
                                var_to_dom[var_name] = dom_selector

        # Pass 2: Traverse entire AST to extract references, calls, fetch requests, event listeners
        self._traverse_node(
            node=tree.root_node,
            rel_path=rel_path,
            html_path=html_path,
            file_id=file_id,
            current_func_id=None,
            declared_functions=declared_functions,
            var_to_dom=var_to_dom,
            get_snippet=get_snippet,
            result=result,
        )

        return result

    def _extract_dom_selector_from_node(self, node: tree_sitter.Node) -> str | None:
        """Extract selector string from document.getElementById or querySelector call."""
        if node.type != "call_expression":
            return None
        func_node = node.child_by_field_name("function")
        args_node = node.child_by_field_name("arguments")
        if not func_node or not args_node or not args_node.named_children:
            return None

        # Only a *direct* DOM-lookup call counts: the callee must be a member expression
        # whose property is exactly getElementById / querySelector. (A substring test
        # wrongly matched `getElementById(...).addEventListener("click", ...)`.)
        first = args_node.named_children[0]
        if first.type not in ("string", "template_string") or "${" in first.text.decode("utf-8"):
            return None                       # a variable selector says nothing about which element
        if func_node.type == "identifier":    # user-defined helper: const $ = (s) => document.querySelector(s)
            method = self._helper_method(func_node.text.decode("utf-8")) or ""
        elif func_node.type == "member_expression":
            prop = func_node.child_by_field_name("property")
            method = prop.text.decode("utf-8") if prop else ""
        else:
            return None
        arg_text = _strip_quotes(first.text.decode("utf-8"))

        if method == "getElementById":
            return arg_text if arg_text.startswith("#") else f"#{arg_text}"
        if method == "querySelector":
            return arg_text
        return None

    def _traverse_node(
        self,
        node: tree_sitter.Node,
        rel_path: str,
        html_path: str,
        file_id: str,
        current_func_id: str | None,
        declared_functions: dict[str, str],
        var_to_dom: dict[str, str],
        get_snippet,
        result: AnalysisResult,
    ) -> None:
        # Check if entering a function scope
        new_func_id = current_func_id
        if node.id in self._handler_ids:                  # anonymous event handler: its body is attributed to it
            new_func_id = self._handler_ids[node.id]
        elif node.type == "function_declaration":
            name_node = node.child_by_field_name("name")
            if name_node:
                new_func_id = declared_functions.get(name_node.text.decode("utf-8"))
        elif node.type == "variable_declarator":
            val = node.child_by_field_name("value")
            name_node = node.child_by_field_name("name")
            if val is not None and name_node is not None and val.type in ("arrow_function", "function"):
                new_func_id = declared_functions.get(name_node.text.decode("utf-8"), new_func_id)
            elif val is not None and name_node is not None and self._dynamic_import_target(val, rel_path):
                self._namespaces[name_node.text.decode("utf-8")] = self._dynamic_import_target(val, rel_path)
            elif val is not None and name_node is not None and self._axios_instance(val) is not None:
                self._axios[name_node.text.decode("utf-8")] = self._axios_instance(val)
            elif val is not None and name_node is not None and current_func_id is not None:
                # `const el = document.getElementById("x")` inside a function: remember it from here
                # on (flow-insensitive, file-wide map; a reused variable name overwrites the entry).
                selector = self._extract_dom_selector_from_node(val)
                if selector:
                    var_to_dom[name_node.text.decode("utf-8")] = selector
        elif node.type == "method_definition":
            name_node = node.child_by_field_name("name")
            cls = node.parent.parent if node.parent is not None else None   # class_body -> class_declaration
            cls_name = cls.child_by_field_name("name") if cls is not None and cls.type == "class_declaration" else None
            if name_node is not None and cls_name is not None:
                key = f"{cls_name.text.decode('utf-8')}.{name_node.text.decode('utf-8')}"
                new_func_id = declared_functions.get(key, new_func_id)

        # el.onclick = handler
        if node.type == "assignment_expression":
            self._handle_property_handler(node, rel_path, html_path, declared_functions, var_to_dom, get_snippet, result)

        # Inspect call expressions
        if node.type == "call_expression":
            self._handle_call(
                node=node,
                rel_path=rel_path,
                html_path=html_path,
                caller_id=new_func_id or file_id,
                declared_functions=declared_functions,
                var_to_dom=var_to_dom,
                get_snippet=get_snippet,
                result=result,
            )

        # Recursively visit children
        for child in node.children:
            self._traverse_node(
                node=child,
                rel_path=rel_path,
                html_path=html_path,
                file_id=file_id,
                current_func_id=new_func_id,
                declared_functions=declared_functions,
                var_to_dom=var_to_dom,
                get_snippet=get_snippet,
                result=result,
            )

    @staticmethod
    def _dynamic_import_spec(node) -> str | None:
        """The module string of `import("./x.js")`, also when wrapped in `await`."""
        if node is not None and node.type == "await_expression" and node.named_children:
            node = node.named_children[0]
        if node is None or node.type != "call_expression":
            return None
        f, a = node.child_by_field_name("function"), node.child_by_field_name("arguments")
        if f is None or f.type != "import" or a is None or not a.named_children or a.named_children[0].type != "string":
            return None
        return _strip_quotes(a.named_children[0].text.decode("utf-8"))

    def _dynamic_import_target(self, node, rel_path: str) -> str | None:
        spec = self._dynamic_import_spec(node)
        return self._resolve_module(rel_path, spec) if spec else None

    def _axios_instance(self, node) -> str | None:
        """baseURL ("" if none) when `node` is `axios.create({...})` on a known axios name, else None."""
        if node is None or node.type != "call_expression":
            return None
        f, a = node.child_by_field_name("function"), node.child_by_field_name("arguments")
        if f is None or f.type != "member_expression" or a is None:
            return None
        obj, prop = f.child_by_field_name("object"), f.child_by_field_name("property")
        if obj is None or prop is None or obj.text.decode("utf-8") not in self._axios or prop.text != b"create":
            return None
        for opt in a.named_children:
            if opt.type == "object":
                for pair in opt.named_children:
                    k, v = pair.child_by_field_name("key"), pair.child_by_field_name("value")
                    if pair.type == "pair" and k is not None and v is not None and k.text.decode("utf-8") == "baseURL" and v.type == "string":
                        return _strip_quotes(v.text.decode("utf-8")).rstrip("/")
        return ""

    @staticmethod
    def _find_fetch_wrappers(root) -> set[str]:
        """Names of top-level helpers whose FIRST parameter flows into fetch(), e.g.
        `async function api(path, opts) { return fetch(path, opts); }`. Calls to them are requests."""
        def params_of(fn):
            p = fn.child_by_field_name("parameters")
            if p is None:
                p = fn.child_by_field_name("parameter")
            if p is None:
                return []
            if p.type == "identifier":
                return [p.text.decode("utf-8")]
            return [c.text.decode("utf-8") for c in p.named_children if c.type == "identifier"]

        def fetches(fn, param: str) -> bool:
            stack = [fn]
            while stack:
                n = stack.pop()
                if n.type == "call_expression":
                    f, a = n.child_by_field_name("function"), n.child_by_field_name("arguments")
                    if (f is not None and f.type == "identifier" and f.text == b"fetch"
                            and a is not None and a.named_children
                            and re.search(rf"\b{re.escape(param)}\b", a.named_children[0].text.decode("utf-8"))):
                        return True
                stack.extend(n.children)
            return False

        found: set[str] = set()
        for node in root.named_children:
            if node.type == "export_statement":
                inner = node.child_by_field_name("declaration")
                node = inner if inner is not None else node
            if node.type == "function_declaration":
                name = node.child_by_field_name("name")
                ps = params_of(node)
                if name is not None and ps and fetches(node, ps[0]):
                    found.add(name.text.decode("utf-8"))
            elif node.type == "lexical_declaration":
                for decl in node.named_children:
                    val, name = decl.child_by_field_name("value"), decl.child_by_field_name("name")
                    if val is not None and name is not None and val.type in ("arrow_function", "function"):
                        ps = params_of(val)
                        if ps and fetches(val, ps[0]):
                            found.add(name.text.decode("utf-8"))
        return found

    @staticmethod
    def _resolve_module(from_path: str, spec: str) -> str | None:
        """'./api.js' relative to static/app.js -> 'static/api.js'. Bare package names -> None."""
        if not spec.startswith("."):
            return None
        target = posixpath.normpath(posixpath.join(posixpath.dirname(from_path), spec))
        if not posixpath.splitext(target)[1]:
            target += ".js"
        return target

    def _handle_import(self, node, rel_path, file_id, get_snippet, result) -> None:
        source = node.child_by_field_name("source")
        if source is None:
            return
        spec_text = _strip_quotes(source.text.decode("utf-8"))
        if spec_text == "axios":
            default = next((c for c in node.children if c.type == "import_clause"), None)
            ident = next((c for c in (default.children if default is not None else []) if c.type == "identifier"), None)
            if ident is not None:
                self._axios[ident.text.decode("utf-8")] = ""
        target = self._resolve_module(rel_path, spec_text)
        if target is None:
            return                                        # external package: not part of the repo graph
        line = node.start_point[0] + 1
        loc = SourceLocation(file_path=rel_path, start_line=line, end_line=node.end_point[0] + 1)
        result.add_relationship(Relationship(
            source_id=file_id, target_id=make_file_id(target), type=RelationType.IMPORTS,
            kind=RelationKind.DETECTED, confidence=1.0,
            evidence=[Evidence(snippet=get_snippet(loc.start_line, loc.end_line), location=loc)]))
        for spec in self._named_specifiers(node):
            self._imported[spec[1]] = (target, spec[0])
        stack = list(node.children)                       # import * as ns from "./x.js"
        while stack:
            n = stack.pop()
            if n.type == "namespace_import":
                ident = next((c for c in n.children if c.type == "identifier"), None)
                if ident is not None:
                    self._namespaces[ident.text.decode("utf-8")] = target
            else:
                stack.extend(n.children)

    @staticmethod
    def _named_specifiers(import_node):
        """Yield (original_name, local_name) for `import { a, b as c } from ...`."""
        stack = list(import_node.children)
        while stack:
            n = stack.pop()
            if n.type == "import_specifier":
                name = n.child_by_field_name("name")
                alias = n.child_by_field_name("alias")
                if name is not None:
                    orig = name.text.decode("utf-8")
                    yield (orig, alias.text.decode("utf-8") if alias is not None else orig)
            else:
                stack.extend(n.children)

    def _handle_class(self, node, rel_path, file_id, declared_functions, get_snippet, result) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        cls_name = name_node.text.decode("utf-8")
        cls_id = make_entity_id("javascript", rel_path, cls_name)
        loc = SourceLocation(file_path=rel_path, start_line=node.start_point[0] + 1, end_line=node.end_point[0] + 1)
        result.add_entity(Entity(id=cls_id, type=EntityType.CLASS, name=cls_name, qualified_name=cls_name,
                                 language=Language.JAVASCRIPT, location=loc, parent_id=file_id))
        result.add_relationship(Relationship(source_id=file_id, target_id=cls_id, type=RelationType.CONTAINS,
                                             kind=RelationKind.DETECTED, confidence=1.0))
        # class Child extends Base
        for child in node.children:
            if child.type == "class_heritage":
                base = next((c for c in child.children if c.type == "identifier"), None)
                if base is not None:
                    base_name = base.text.decode("utf-8")
                    base_path = self._imported.get(base_name, (rel_path, base_name))
                    result.add_relationship(Relationship(
                        source_id=cls_id, target_id=make_entity_id("javascript", base_path[0], base_path[1]),
                        type=RelationType.INHERITS, kind=RelationKind.DETECTED, confidence=1.0,
                        evidence=[Evidence(snippet=get_snippet(loc.start_line, loc.start_line), location=loc)]))
        body = node.child_by_field_name("body")
        for member in (body.named_children if body is not None else []):
            if member.type != "method_definition":
                continue
            m_name = member.child_by_field_name("name")
            if m_name is None:
                continue
            qualified = f"{cls_name}.{m_name.text.decode('utf-8')}"
            m_id = make_entity_id("javascript", rel_path, qualified)
            declared_functions[qualified] = m_id
            m_loc = SourceLocation(file_path=rel_path, start_line=member.start_point[0] + 1,
                                   end_line=member.end_point[0] + 1)
            result.add_entity(Entity(id=m_id, type=EntityType.METHOD, name=m_name.text.decode("utf-8"),
                                     qualified_name=qualified, language=Language.JAVASCRIPT,
                                     location=m_loc, parent_id=cls_id))
            result.add_relationship(Relationship(source_id=cls_id, target_id=m_id, type=RelationType.CONTAINS,
                                                 kind=RelationKind.DETECTED, confidence=1.0))

    def _track_class_change(self, func_node, args_node, var_to_dom, result: AnalysisResult) -> None:
        """Record classes that JS adds/toggles at runtime, so CSS can link to them honestly."""
        if func_node.type != "member_expression" or not args_node.named_children:
            return
        prop = func_node.child_by_field_name("property")
        if not prop or prop.text.decode("utf-8") not in ("toggle", "add", "remove"):
            return
        class_list = func_node.child_by_field_name("object")
        if not class_list or class_list.type != "member_expression":
            return
        cl_prop = class_list.child_by_field_name("property")
        if not cl_prop or cl_prop.text.decode("utf-8") != "classList":
            return
        target = class_list.child_by_field_name("object")
        selector = None
        if target is not None and target.type == "identifier":
            selector = var_to_dom.get(target.text.decode("utf-8"))
        elif target is not None:
            selector = self._extract_dom_selector_from_node(target)
        first = args_node.named_children[0]
        if selector and selector.startswith("#") and first.type == "string":
            result.dynamic_classes.setdefault(selector[1:], set()).add(_strip_quotes(first.text.decode("utf-8")))

    def _handle_call(
        self,
        node: tree_sitter.Node,
        rel_path: str,
        html_path: str,
        caller_id: str,
        declared_functions: dict[str, str],
        var_to_dom: dict[str, str],
        get_snippet,
        result: AnalysisResult,
    ) -> None:
        func_node = node.child_by_field_name("function")
        args_node = node.child_by_field_name("arguments")
        if not func_node or not args_node:
            return

        func_text = func_node.text.decode("utf-8")
        start_line = node.start_point[0] + 1
        end_line = node.end_point[0] + 1
        loc = SourceLocation(file_path=rel_path, start_line=start_line, end_line=end_line)
        snippet = get_snippet(start_line, end_line)

        # 1. DOM References: document.getElementById or document.querySelector
        dom_selector = self._extract_dom_selector_from_node(node)
        if dom_selector:
            target_elem_id = f"html:{html_path}:{dom_selector}"
            result.add_relationship(
                Relationship(
                    source_id=caller_id,
                    target_id=target_elem_id,
                    type=RelationType.REFERENCES,
                    kind=RelationKind.DETECTED,
                    confidence=1.0,
                    evidence=[Evidence(snippet=snippet, location=loc)],
                )
            )

        # 0. import("./charts.js"): a load-time dependency created at runtime
        dyn = self._dynamic_import_target(node, rel_path)
        if dyn:
            result.add_relationship(Relationship(
                source_id=make_file_id(rel_path), target_id=make_file_id(dyn), type=RelationType.IMPORTS,
                kind=RelationKind.DETECTED, confidence=1.0, evidence=[Evidence(snippet=snippet, location=loc)]))

        # 0b. namespace.member(): ns.drawChart() where ns came from `import * as ns` or `await import(..)`
        if func_node.type == "member_expression":
            obj, prop = func_node.child_by_field_name("object"), func_node.child_by_field_name("property")
            if obj is not None and obj.type == "identifier" and prop is not None and obj.text.decode("utf-8") in self._namespaces:
                result.add_relationship(Relationship(
                    source_id=caller_id,
                    target_id=make_entity_id("javascript", self._namespaces[obj.text.decode("utf-8")], prop.text.decode("utf-8")),
                    type=RelationType.CALLS, kind=RelationKind.DETECTED, confidence=1.0,
                    evidence=[Evidence(snippet=snippet, location=loc)]))

        # 0c. axios.get/post/put/patch/delete(url, ...), axios({url, method}), instance.get(...)
        self._handle_axios_call(func_node, args_node, caller_id, loc, snippet, result)

        # 1b. Dynamic classes: el.classList.toggle/add/remove("error")
        self._track_class_change(func_node, args_node, var_to_dom, result)

        # 2. HTTP Requests: fetch(url, options)
        if func_text == "fetch" and args_node.named_children:
            self._handle_fetch_call(
                node=node,
                args_node=args_node,
                caller_id=caller_id,
                loc=loc,
                snippet=snippet,
                result=result,
            )

        # 3. Event Listeners: el.addEventListener("event", handler)
        if "addEventListener" in func_text and len(args_node.named_children) >= 2:
            self._handle_add_event_listener(
                func_node=func_node,
                args_node=args_node,
                html_path=html_path,
                rel_path=rel_path,
                declared_functions=declared_functions,
                var_to_dom=var_to_dom,
                loc=loc,
                snippet=snippet,
                result=result,
            )

        # 3b. this.method() inside a class method -> sibling method of the same class
        if func_node.type == "member_expression" and caller_id != "":
            obj = func_node.child_by_field_name("object")
            prop = func_node.child_by_field_name("property")
            if obj is not None and obj.type == "this" and prop is not None:
                qualified = caller_id.split(":", 2)[-1]
                if "." in qualified:
                    key = f"{qualified.rsplit('.', 1)[0]}.{prop.text.decode('utf-8')}"
                    if key in declared_functions and declared_functions[key] != caller_id:
                        result.add_relationship(Relationship(
                            source_id=caller_id, target_id=declared_functions[key],
                            type=RelationType.CALLS, kind=RelationKind.DETECTED, confidence=1.0,
                            evidence=[Evidence(snippet=snippet, location=loc)]))

        # 3c. Call to a helper that wraps fetch: api("/api/x", {method: "POST"}) -> a request, one hop removed
        if func_node.type == "identifier" and func_text in self._wrappers and args_node.named_children:
            self._handle_fetch_call(node=node, args_node=args_node, caller_id=caller_id, loc=loc,
                                    snippet=snippet, result=result, via_wrapper=True)

        # 4. Intra-file Function Calls: e.g. showMessage(...)
        if func_node.type == "identifier":
            fn_name = func_text
            if fn_name in self._imported and fn_name not in declared_functions:
                # Named import: `import { login } from "./api.js"` then login()
                target_path, original = self._imported[fn_name]
                result.add_relationship(Relationship(
                    source_id=caller_id,
                    target_id=make_entity_id("javascript", target_path, original),
                    type=RelationType.CALLS, kind=RelationKind.DETECTED, confidence=1.0,
                    evidence=[Evidence(snippet=snippet, location=loc)]))
            if fn_name in declared_functions and declared_functions[fn_name] != caller_id:
                result.add_relationship(
                    Relationship(
                        source_id=caller_id,
                        target_id=declared_functions[fn_name],
                        type=RelationType.CALLS,
                        kind=RelationKind.DETECTED,
                        confidence=1.0,
                        evidence=[Evidence(snippet=snippet, location=loc)],
                    )
                )

    def _handle_axios_call(self, func_node, args_node, caller_id, loc, snippet, result) -> None:
        verbs = {"get", "post", "put", "patch", "delete", "head"}
        method, base, url_arg = None, "", None
        if func_node.type == "member_expression":
            obj, prop = func_node.child_by_field_name("object"), func_node.child_by_field_name("property")
            if obj is None or prop is None or obj.text.decode("utf-8") not in self._axios:
                return
            base = self._axios[obj.text.decode("utf-8")]
            name = prop.text.decode("utf-8")
            if name in verbs and args_node.named_children:
                method, url_arg = name.upper(), args_node.named_children[0]
            elif name == "request" and args_node.named_children and args_node.named_children[0].type == "object":
                method, url_arg = self._config_request(args_node.named_children[0])
        elif func_node.type == "identifier" and func_node.text.decode("utf-8") in self._axios \
                and args_node.named_children and args_node.named_children[0].type == "object":
            base = self._axios[func_node.text.decode("utf-8")]          # axios({ method, url })
            method, url_arg = self._config_request(args_node.named_children[0])
        if method and url_arg is not None:
            self._handle_fetch_call(node=None, args_node=args_node, caller_id=caller_id, loc=loc, snippet=snippet,
                                    result=result, method=method, base=base, url_node=url_arg)

    @staticmethod
    def _config_request(obj):
        """(METHOD, url node) from an axios config object {method: "delete", url: "/x"}; method defaults to GET."""
        method, url = "GET", None
        for pair in obj.named_children:
            k, v = pair.child_by_field_name("key"), pair.child_by_field_name("value")
            if pair.type != "pair" or k is None or v is None:
                continue
            key = k.text.decode("utf-8").strip("\"'")
            if key == "url":
                url = v
            elif key == "method" and v.type == "string":
                method = _strip_quotes(v.text.decode("utf-8")).upper()
        return (method, url) if url is not None else (None, None)

    def _handle_fetch_call(
        self,
        node: tree_sitter.Node,
        args_node: tree_sitter.Node,
        caller_id: str,
        loc: SourceLocation,
        snippet: str,
        result: AnalysisResult,
        via_wrapper: bool = False,
        method: str | None = None,
        base: str = "",
        url_node: tree_sitter.Node | None = None,
    ) -> None:
        url_arg = url_node if url_node is not None else args_node.named_children[0]
        explicit_method = method is not None            # axios.post(...) etc.: the verb is not in an options object
        method = method or "GET"
        kind = RelationKind.DETECTED
        confidence = 1.0
        route = ""

        # String literal: fetch("/api/login", ...)
        if url_arg.type == "string":
            route = _strip_quotes(url_arg.text.decode("utf-8"))

        # Template literal: fetch(`/api/users/${userId}`)
        elif url_arg.type == "template_string":
            raw_text = url_arg.text.decode("utf-8")
            # Replace ${param} with {snake_case(param)}
            def replace_sub(match):
                var_name = match.group(1).strip()
                return f"{{{_camel_to_snake(var_name)}}}"

            route = _strip_quotes(re.sub(r"\$\{([^}]+)\}", replace_sub, raw_text))
            kind = RelationKind.INFERRED
            confidence = 0.85

        # Check options object for method: "POST"
        if not explicit_method and len(args_node.named_children) > 1 and args_node.named_children[1].type == "object":
            opt_node = args_node.named_children[1]
            for prop in opt_node.named_children:
                if prop.type == "pair":
                    key_node = prop.child_by_field_name("key")
                    val_node = prop.child_by_field_name("value")
                    if key_node and val_node and key_node.text.decode("utf-8").lower() == "method":
                        method = _strip_quotes(val_node.text.decode("utf-8")).upper()

        if route and base and route.startswith("/"):
            route = base + route                            # axios.create({baseURL: "/api"})
        if route and via_wrapper and kind == RelationKind.DETECTED:
            kind, confidence = RelationKind.INFERRED, 0.85        # reached through a helper, not a literal fetch
        if route:
            endpoint_id = make_endpoint_id(method, route)
            result.add_relationship(
                Relationship(
                    source_id=caller_id,
                    target_id=endpoint_id,
                    type=RelationType.REQUESTS,
                    kind=kind,
                    confidence=confidence,
                    evidence=[Evidence(snippet=snippet, location=loc)],
                )
            )

    def _collect_namespaces_in(self, node, rel_path: str) -> None:
        """Register `const ns = await import("./x.js")` declared inside a handler before its calls are read
        (the handler argument is analysed here before the main traversal reaches its body)."""
        stack = [node]
        while stack:
            n = stack.pop()
            if n.type == "variable_declarator":
                val, name = n.child_by_field_name("value"), n.child_by_field_name("name")
                target = self._dynamic_import_target(val, rel_path) if val is not None else None
                if target and name is not None:
                    self._namespaces[name.text.decode("utf-8")] = target
            stack.extend(n.children)

    def _handle_add_event_listener(
        self,
        func_node: tree_sitter.Node,
        args_node: tree_sitter.Node,
        html_path: str,
        rel_path: str,
        declared_functions: dict[str, str],
        var_to_dom: dict[str, str],
        loc: SourceLocation,
        snippet: str,
        result: AnalysisResult,
    ) -> None:
        # Determine the target DOM element
        dom_selector = None

        # Case A: form.addEventListener(...)
        caller_node = func_node.named_children[0] if func_node.named_children else None
        if caller_node and caller_node.type == "identifier":
            var_name = caller_node.text.decode("utf-8")
            dom_selector = var_to_dom.get(var_name)

        # Case B: document.getElementById("profile-btn").addEventListener(...)
        elif caller_node and caller_node.type == "call_expression":
            dom_selector = self._extract_dom_selector_from_node(caller_node)

        if not dom_selector:
            return

        elem_id = f"html:{html_path}:{dom_selector}"
        handler_arg = args_node.named_children[1]

        first = args_node.named_children[0] if args_node.named_children else None
        event = _strip_quotes(first.text.decode("utf-8")) if first is not None and first.type == "string" else ""
        self._link_event_handler(elem_id, handler_arg, declared_functions, rel_path, loc, snippet, result, event=event)

    # Real DOM event names, so `el.once = x` or `el.online = y` is not mistaken for a handler.
    _DOM_EVENTS = frozenset((
        "click dblclick contextmenu mousedown mouseup mouseover mouseout mouseenter mouseleave mousemove wheel "
        "keydown keyup keypress input change submit reset select focus blur focusin focusout load unload error "
        "scroll resize drag dragstart dragend dragover dragenter dragleave drop touchstart touchend touchmove "
        "touchcancel pointerdown pointerup pointermove pointerenter pointerleave toggle copy cut paste "
        "animationend transitionend play pause ended hashchange popstate beforeunload message").split())

    def _handle_property_handler(self, node, rel_path, html_path, declared_functions, var_to_dom, get_snippet, result) -> None:
        """`$("#go").onclick = async () => {...}`, `form.onsubmit = handleLogin`: an event handler assigned
        to a DOM property. The element must be identifiable (a known variable or a literal-selector lookup);
        anything else (`btn.onclick = f` where btn came from createElement) is not linked."""
        left, right = node.child_by_field_name("left"), node.child_by_field_name("right")
        if left is None or right is None or left.type != "member_expression":
            return
        prop, obj = left.child_by_field_name("property"), left.child_by_field_name("object")
        if prop is None or obj is None or not prop.text.decode("utf-8").startswith("on") or prop.text.decode("utf-8")[2:] not in self._DOM_EVENTS:
            return
        selector = None
        if obj.type == "identifier":
            selector = var_to_dom.get(obj.text.decode("utf-8"))
        elif obj.type == "call_expression":
            selector = self._extract_dom_selector_from_node(obj)
        if not selector:
            return
        loc = SourceLocation(file_path=rel_path, start_line=node.start_point[0] + 1, end_line=node.end_point[0] + 1)
        self._link_event_handler(f"html:{html_path}:{selector}", right, declared_functions, rel_path, loc,
                                 get_snippet(loc.start_line, loc.end_line), result, event=prop.text.decode("utf-8")[2:])

    def _link_event_handler(self, elem_id, handler_arg, declared_functions, rel_path, loc, snippet, result,
                            event: str = "") -> None:
        """TRIGGERS edges from an element to what its handler runs: a named function (detected), or every
        function an anonymous callback calls (inferred). Shared by addEventListener and `el.onclick = ...`."""
        # Case 1: Direct identifier handler: addEventListener("submit", handleLogin)
        if handler_arg.type == "identifier":
            fn_name = handler_arg.text.decode("utf-8")
            if fn_name in declared_functions:
                result.add_relationship(
                    Relationship(
                        source_id=elem_id,
                        target_id=declared_functions[fn_name],
                        type=RelationType.TRIGGERS,
                        kind=RelationKind.DETECTED,
                        confidence=1.0,
                        evidence=[Evidence(snippet=snippet, location=loc)],
                    )
                )

        # Case 2: Arrow function or callback: addEventListener("click", () => loadProfile(1))
        elif handler_arg.type in ("arrow_function", "function", "function_expression"):
            # The callback has no name, so it becomes an entity of its own: the element triggers it, and
            # everything inside (calls, requests, DOM lookups) is attributed to it instead of the file.
            handler_id = self._register_handler(handler_arg, elem_id, event, rel_path, result)
            result.add_relationship(
                Relationship(
                    source_id=elem_id,
                    target_id=handler_id,
                    type=RelationType.TRIGGERS,
                    kind=RelationKind.DETECTED,
                    confidence=1.0,
                    evidence=[Evidence(snippet=snippet, location=loc)],
                )
            )

    def _register_handler(self, node, elem_id: str, event: str, rel_path: str, result: AnalysisResult) -> str:
        """Create (once) the entity for an anonymous handler and return its id."""
        if node.id in self._handler_ids:
            return self._handler_ids[node.id]
        line, col = node.start_point[0] + 1, node.start_point[1]
        handler_id = make_entity_id("javascript", rel_path, f"handler@{line}")
        if handler_id in self._handler_entities:            # two handlers starting on the same line
            handler_id = make_entity_id("javascript", rel_path, f"handler@{line}:{col}")
        self._handler_entities.add(handler_id)
        self._handler_ids[node.id] = handler_id
        selector = elem_id.split(":", 2)[2]
        name = f"{selector} {event or 'event'} handler"
        file_id = make_file_id(rel_path)
        result.add_entity(Entity(
            id=handler_id, type=EntityType.FUNCTION, name=name, qualified_name=name, language=Language.JAVASCRIPT,
            location=SourceLocation(file_path=rel_path, start_line=line, end_line=node.end_point[0] + 1,
                                    start_col=col, end_col=node.end_point[1]),
            parent_id=file_id, metadata={"anonymous": True, "event": event, "element": elem_id}))
        result.add_relationship(Relationship(source_id=file_id, target_id=handler_id, type=RelationType.CONTAINS,
                                             kind=RelationKind.DETECTED, confidence=1.0))
        return handler_id
