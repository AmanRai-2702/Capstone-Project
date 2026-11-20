import ast
import logging
from pathlib import PurePosixPath

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

# Standard HTTP methods supported by FastAPI, Flask, Starlette route decorators
HTTP_METHODS = {"get", "post", "put", "delete", "patch", "options", "head"}


class PythonASTVisitor(ast.NodeVisitor):
    """
    Walks a Python AST to extract classes, functions, methods,
    FastAPI endpoints, and method/function calls.
    """

    def __init__(self, rel_path: str, source_lines: list[str]):
        self.rel_path = rel_path.replace("\\", "/")
        self.source_lines = source_lines
        self.result = AnalysisResult()

        # Context stacks during traversal
        self.class_stack: list[str] = []
        self.current_callable_id: str | None = None

        # Tracking for local call resolution:
        # e.g. "AuthService": "services/auth_service.py"
        self.imported_symbols: dict[str, str] = {}
        # e.g. "auth_service": "AuthService", "self.users": "UserRepository"
        self.var_types: dict[str, str] = {}

    def _get_snippet(self, start_line: int, end_line: int) -> str:
        """Extract lines of code as a trimmed snippet."""
        if 1 <= start_line <= len(self.source_lines):
            end = min(end_line, len(self.source_lines))
            return "\n".join(self.source_lines[start_line - 1 : end]).strip()
        return ""

    def visit_Import(self, node: ast.Import) -> None:
        file_id = make_file_id(self.rel_path)
        for alias in node.names:
            mod_path = alias.name.replace(".", "/") + ".py"
            target_file_id = make_file_id(mod_path)
            self.result.add_relationship(
                Relationship(
                    source_id=file_id,
                    target_id=target_file_id,
                    type=RelationType.IMPORTS,
                    kind=RelationKind.DETECTED,
                    confidence=1.0,
                    evidence=[
                        Evidence(
                            snippet=self._get_snippet(node.lineno, node.end_lineno or node.lineno),
                            location=SourceLocation(
                                file_path=self.rel_path,
                                start_line=node.lineno,
                                end_line=node.end_lineno or node.lineno,
                            ),
                        )
                    ],
                )
            )
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        file_id = make_file_id(self.rel_path)
        if node.module:
            mod_path = node.module.replace(".", "/") + ".py"
            target_file_id = make_file_id(mod_path)
            self.result.add_relationship(
                Relationship(
                    source_id=file_id,
                    target_id=target_file_id,
                    type=RelationType.IMPORTS,
                    kind=RelationKind.DETECTED,
                    confidence=1.0,
                    evidence=[
                        Evidence(
                            snippet=self._get_snippet(node.lineno, node.end_lineno or node.lineno),
                            location=SourceLocation(
                                file_path=self.rel_path,
                                start_line=node.lineno,
                                end_line=node.end_lineno or node.lineno,
                            ),
                        )
                    ],
                )
            )
            for alias in node.names:
                # Track symbol name to source module path
                self.imported_symbols[alias.asname or alias.name] = mod_path

        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        """Track variable instantiations: e.g. auth_service = AuthService() or self.users = UserRepository()"""
        if isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Name):
            cls_name = node.value.func.id
            for target in node.targets:
                if isinstance(target, ast.Name):
                    self.var_types[target.id] = cls_name
                elif isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name):
                    key = f"{target.value.id}.{target.attr}"
                    self.var_types[key] = cls_name

        self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        file_id = make_file_id(self.rel_path)
        qualified_name = f"{'.'.join(self.class_stack)}.{node.name}" if self.class_stack else node.name
        entity_id = make_entity_id("python", self.rel_path, qualified_name)

        class_entity = Entity(
            id=entity_id,
            type=EntityType.CLASS,
            name=node.name,
            qualified_name=qualified_name,
            language=Language.PYTHON,
            location=SourceLocation(
                file_path=self.rel_path,
                start_line=node.lineno,
                end_line=node.end_lineno or node.lineno,
            ),
            parent_id=file_id,
            metadata={
                "bases": [ast.unparse(b) for b in node.bases],
            },
        )
        self.result.add_entity(class_entity)

        # File CONTAINS Class
        self.result.add_relationship(
            Relationship(
                source_id=file_id,
                target_id=entity_id,
                type=RelationType.CONTAINS,
                kind=RelationKind.DETECTED,
                confidence=1.0,
            )
        )

        self.class_stack.append(node.name)
        self.generic_visit(node)
        self.class_stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._handle_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._handle_function(node)

    def _handle_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        file_id = make_file_id(self.rel_path)
        is_method = len(self.class_stack) > 0
        parent_class = self.class_stack[-1] if is_method else None

        if is_method and parent_class:
            qualified_name = f"{parent_class}.{node.name}"
            entity_type = EntityType.METHOD
            parent_id = make_entity_id("python", self.rel_path, parent_class)
        else:
            qualified_name = node.name
            entity_type = EntityType.FUNCTION
            parent_id = file_id

        entity_id = make_entity_id("python", self.rel_path, qualified_name)

        func_entity = Entity(
            id=entity_id,
            type=entity_type,
            name=node.name,
            qualified_name=qualified_name,
            language=Language.PYTHON,
            location=SourceLocation(
                file_path=self.rel_path,
                start_line=node.lineno,
                end_line=node.end_lineno or node.lineno,
            ),
            parent_id=parent_id,
            metadata={"is_async": isinstance(node, ast.AsyncFunctionDef)},
        )
        self.result.add_entity(func_entity)

        # Parent CONTAINS Function/Method
        self.result.add_relationship(
            Relationship(
                source_id=parent_id,
                target_id=entity_id,
                type=RelationType.CONTAINS,
                kind=RelationKind.DETECTED,
                confidence=1.0,
            )
        )

        # Check decorators for API Endpoints (e.g. @app.get("/"), @app.post("/api/login"))
        self._check_route_decorators(node, func_entity)

        # Traverse body with current function context for call tracking
        prev_callable = self.current_callable_id
        self.current_callable_id = entity_id
        self.generic_visit(node)
        self.current_callable_id = prev_callable

    def _check_route_decorators(
        self,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
        func_entity: Entity,
    ) -> None:
        for dec in node.decorator_list:
            if not isinstance(dec, ast.Call):
                continue

            method = None
            route = None

            # Pattern: @app.get("/path"), @router.post("/path")
            if isinstance(dec.func, ast.Attribute):
                attr_name = dec.func.attr.lower()
                if attr_name in HTTP_METHODS:
                    method = attr_name.upper()
                elif attr_name == "route":
                    method = "GET"  # default Flask/FastAPI route method
                    for kw in dec.keywords:
                        if kw.arg == "methods" and isinstance(kw.value, (ast.List, ast.Tuple)):
                            if kw.value.elts and isinstance(kw.value.elts[0], ast.Constant):
                                method = str(kw.value.elts[0].value).upper()

            # First arg is typically the URL route path string
            if method and dec.args and isinstance(dec.args[0], ast.Constant):
                val = dec.args[0].value
                if isinstance(val, str):
                    route = val

            if method and route:
                endpoint_id = make_endpoint_id(method, route)
                endpoint_loc = SourceLocation(
                    file_path=self.rel_path,
                    start_line=dec.lineno,
                    end_line=dec.end_lineno or dec.lineno,
                )
                endpoint_entity = Entity(
                    id=endpoint_id,
                    type=EntityType.API_ENDPOINT,
                    name=f"{method} {route}",
                    qualified_name=f"{method} {route}",
                    language=Language.UNKNOWN,
                    location=endpoint_loc,
                    metadata={"method": method, "route": route, "framework": "fastapi"},
                )
                self.result.add_entity(endpoint_entity)

                # Python Function HANDLES API Endpoint
                snippet = self._get_snippet(dec.lineno, dec.end_lineno or dec.lineno)
                self.result.add_relationship(
                    Relationship(
                        source_id=func_entity.id,
                        target_id=endpoint_id,
                        type=RelationType.HANDLES,
                        kind=RelationKind.DETECTED,
                        confidence=1.0,
                        evidence=[Evidence(snippet=snippet, location=endpoint_loc)],
                    )
                )

    def visit_Call(self, node: ast.Call) -> None:
        """Inspect calls inside functions/methods and attempt resolution."""
        if not self.current_callable_id:
            self.generic_visit(node)
            return

        call_loc = SourceLocation(
            file_path=self.rel_path,
            start_line=node.lineno,
            end_line=node.end_lineno or node.lineno,
        )
        snippet = self._get_snippet(node.lineno, node.end_lineno or node.lineno)

        # Case 1: Method call on variable or attribute, e.g. auth_service.authenticate() or self.users.find_by_id()
        if isinstance(node.func, ast.Attribute):
            method_name = node.func.attr
            receiver = None

            if isinstance(node.func.value, ast.Name):
                receiver = node.func.value.id  # e.g. "auth_service"
            elif isinstance(node.func.value, ast.Attribute) and isinstance(node.func.value.value, ast.Name):
                receiver = f"{node.func.value.value.id}.{node.func.value.attr}"  # e.g. "self.users"

            if receiver and receiver in self.var_types:
                cls_name = self.var_types[receiver]
                mod_path = self.imported_symbols.get(cls_name)
                if mod_path:
                    target_id = make_entity_id("python", mod_path, f"{cls_name}.{method_name}")
                    self.result.add_relationship(
                        Relationship(
                            source_id=self.current_callable_id,
                            target_id=target_id,
                            type=RelationType.CALLS,
                            kind=RelationKind.INFERRED,
                            confidence=0.85,
                            evidence=[Evidence(snippet=snippet, location=call_loc)],
                        )
                    )

        # Case 2: Direct function call in same module, e.g. helper_func()
        elif isinstance(node.func, ast.Name):
            func_name = node.func.id
            # If function is imported from another module
            if func_name in self.imported_symbols:
                mod_path = self.imported_symbols[func_name]
                target_id = make_entity_id("python", mod_path, func_name)
                self.result.add_relationship(
                    Relationship(
                        source_id=self.current_callable_id,
                        target_id=target_id,
                        type=RelationType.CALLS,
                        kind=RelationKind.INFERRED,
                        confidence=0.85,
                        evidence=[Evidence(snippet=snippet, location=call_loc)],
                    )
                )

        self.generic_visit(node)


class PythonAnalyzer(BaseAnalyzer):
    """Parses Python source files using Python's standard library ast module."""

    def analyze_file(self, rel_path: str, content: str) -> AnalysisResult:
        try:
            tree = ast.parse(content, filename=rel_path)
        except SyntaxError as err:
            log.warning("SyntaxError parsing %s: %s", rel_path, err)
            return AnalysisResult()

        source_lines = content.splitlines()
        visitor = PythonASTVisitor(rel_path=rel_path, source_lines=source_lines)
        visitor.visit(tree)
        return visitor.result
