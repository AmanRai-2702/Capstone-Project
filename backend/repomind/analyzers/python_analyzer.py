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

    def __init__(self, rel_path: str, source_lines: list[str], tree: ast.AST | None = None):
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
        # `import services.pricing as pr` -> {"pr": "services/pricing.py"}; `import a.b` -> {"a.b": "a/b.py"}
        self.module_aliases: dict[str, str] = {}

        # Pre-pass index of what this module defines, so calls to things defined *later*
        # in the file (or later in a class) still resolve.
        self.local_functions: set[str] = set()
        self.local_classes: set[str] = set()
        self.class_methods: dict[str, set[str]] = {}
        self.class_bases: dict[str, list[str]] = {}
        if tree is not None:
            for node in getattr(tree, "body", []):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    self.local_functions.add(node.name)
                elif isinstance(node, ast.ClassDef):
                    self.local_classes.add(node.name)
                    self.class_bases[node.name] = [b.id for b in node.bases if isinstance(b, ast.Name)]
                    self.class_methods[node.name] = {
                        n.name for n in node.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                    }

    def _method_owner(self, cls: str, method: str):
        """Where does `self.method()` inside class `cls` go? -> (file, class, kind, confidence) or None.
        Own class: certain. Local base class that defines it: inferred 0.9 (a subclass could override).
        Imported base class: unverifiable, so only a weak heuristic."""
        if method in self.class_methods.get(cls, ()):
            return (self.rel_path, cls, RelationKind.DETECTED, 1.0)
        seen: set[str] = set()
        queue = list(self.class_bases.get(cls, []))
        while queue:
            base = queue.pop(0)
            if base in seen:
                continue
            seen.add(base)
            if base in self.class_methods:
                if method in self.class_methods[base]:
                    return (self.rel_path, base, RelationKind.INFERRED, 0.9)
                queue.extend(self.class_bases.get(base, []))
            elif base in self.imported_symbols:
                return (self.imported_symbols[base], base, RelationKind.HEURISTIC, 0.6)
        return None

    def _module_path(self, module: str | None, level: int = 0) -> str:
        """Dotted module (+ relative-import level) -> repo-relative .py path."""
        parts = module.split(".") if module else []
        if level:
            base = PurePosixPath(self.rel_path).parent.parts
            base = base[: max(len(base) - (level - 1), 0)]
            parts = [*base, *parts]
        return "/".join(parts) + ".py"

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
            self.module_aliases[alias.asname or alias.name] = mod_path
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
        if node.module or node.level:
            mod_path = self._module_path(node.module, node.level)
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

    @staticmethod
    def _annotation_class(ann: ast.expr | None) -> str | None:
        """Class named by a type annotation: Foo, "Foo", Optional[Foo], Foo | None. Else None."""
        if isinstance(ann, ast.Name):
            return ann.id
        if isinstance(ann, ast.Constant) and isinstance(ann.value, str) and ann.value.isidentifier():
            return ann.value
        if isinstance(ann, ast.Subscript) and isinstance(ann.value, ast.Name) and ann.value.id == "Optional":
            return PythonASTVisitor._annotation_class(ann.slice)
        if isinstance(ann, ast.BinOp) and isinstance(ann.op, ast.BitOr):           # Foo | None
            for side in (ann.left, ann.right):
                if not (isinstance(side, ast.Constant) and side.value is None):
                    return PythonASTVisitor._annotation_class(side)
        return None

    @staticmethod
    def _dotted(expr: ast.expr) -> str | None:
        """a.b.c -> "a.b.c" for a pure attribute chain on a name, else None."""
        parts: list[str] = []
        while isinstance(expr, ast.Attribute):
            parts.append(expr.attr)
            expr = expr.value
        return ".".join([expr.id, *reversed(parts)]) if isinstance(expr, ast.Name) else None

    @staticmethod
    def _target_key(target: ast.expr) -> str | None:
        if isinstance(target, ast.Name):
            return target.id
        if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name):
            return f"{target.value.id}.{target.attr}"          # e.g. "self.users"
        return None

    def _value_class(self, value: ast.expr) -> str | None:
        """Class an assigned expression evaluates to, when that is statically clear:
        Foo()                      constructor call
        other / self.other         copy of something whose type is already known (annotated parameter, alias)
        a or Foo()  /  Foo() if c else b     the first operand that has a known class
        """
        if isinstance(value, ast.Call) and isinstance(value.func, ast.Name):
            return value.func.id if value.func.id in self.local_classes or value.func.id in self.imported_symbols else None
        if isinstance(value, (ast.Name, ast.Attribute)):
            key = self._target_key(value)
            return self.var_types.get(key) if key else None
        if isinstance(value, ast.BoolOp):
            return next((c for v in value.values if (c := self._value_class(v))), None)
        if isinstance(value, ast.IfExp):
            return self._value_class(value.body) or self._value_class(value.orelse)
        return None

    def visit_Assign(self, node: ast.Assign) -> None:
        """Track what a variable holds (see _value_class), so `x = Foo(); x.run()` and
        `repo = self.repo; repo.get()` resolve to Foo.run / the repository's get."""
        cls_name = self._value_class(node.value)
        if cls_name:
            for target in node.targets:
                key = self._target_key(target)
                if key:
                    self.var_types[key] = cls_name
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        """x: Foo = ...  /  self.x: Foo = ...  (the annotation wins over the value)."""
        cls_name = self._annotation_class(node.annotation)
        key = self._target_key(node.target)
        if cls_name and key:
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

        # Class INHERITS Base. Resolved through same-file classes or imports; bases from
        # external packages (e.g. pydantic.BaseModel) are reported as unresolved later.
        for base in node.bases:
            if isinstance(base, ast.Name):
                base_name = base.id
                if base_name in self.local_classes:
                    base_path = self.rel_path
                elif base_name in self.imported_symbols:
                    base_path = self.imported_symbols[base_name]
                else:
                    continue
                self.result.add_relationship(
                    Relationship(
                        source_id=entity_id,
                        target_id=make_entity_id("python", base_path, base_name),
                        type=RelationType.INHERITS,
                        kind=RelationKind.DETECTED,
                        confidence=1.0,
                        evidence=[Evidence(
                            snippet=self._get_snippet(node.lineno, node.lineno),
                            location=SourceLocation(file_path=self.rel_path, start_line=node.lineno, end_line=node.lineno),
                        )],
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

        # Parameter annotations that name a known class: function USES that type
        # (e.g. login(request: LoginRequest) -> the request model).
        for arg in [*node.args.args, *node.args.kwonlyargs]:
            ann = arg.annotation
            if isinstance(ann, ast.Name) and arg.arg not in ("self", "cls"):
                if ann.id in self.local_classes:
                    type_path = self.rel_path
                elif ann.id in self.imported_symbols:
                    type_path = self.imported_symbols[ann.id]
                else:
                    continue
                self.result.add_relationship(
                    Relationship(
                        source_id=entity_id,
                        target_id=make_entity_id("python", type_path, ann.id),
                        type=RelationType.USES,
                        kind=RelationKind.DETECTED,
                        confidence=1.0,
                        evidence=[Evidence(
                            snippet=self._get_snippet(node.lineno, node.lineno),
                            location=SourceLocation(file_path=self.rel_path, start_line=node.lineno, end_line=node.lineno),
                        )],
                    )
                )

        # Check decorators for API Endpoints (e.g. @app.get("/"), @app.post("/api/login"))
        self._check_route_decorators(node, func_entity)

        # Traverse body with current function context for call tracking
        # Decorators and default values run at definition time in the *enclosing*
        # scope, so they must not be attributed to this function as calls.
        for dec in node.decorator_list:
            self.visit(dec)
        for default in [*node.args.defaults, *node.args.kw_defaults]:
            if default is not None:
                self.visit(default)

        prev_callable = self.current_callable_id
        self.current_callable_id = entity_id
        outer_types = dict(self.var_types)
        for arg in [*node.args.args, *node.args.kwonlyargs]:
            cls = self._annotation_class(arg.annotation)
            if cls and arg.arg not in ("self", "cls"):
                self.var_types[arg.arg] = cls                # parameter annotation = declared type
        for stmt in node.body:
            self.visit(stmt)
        # Locals and parameters must not leak into the next function; `self.x` attributes do persist
        # (they are set in __init__ and used by other methods).
        self.var_types = {**outer_types, **{k: v for k, v in self.var_types.items() if k.startswith("self.")}}
        self.current_callable_id = prev_callable

    def _check_route_decorators(
        self,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
        func_entity: Entity,
    ) -> None:
        for dec in node.decorator_list:
            if not isinstance(dec, ast.Call):
                continue

            methods: list[str] = []
            route = None

            # Pattern: @app.get("/path"), @router.post("/path")
            if isinstance(dec.func, ast.Attribute):
                attr_name = dec.func.attr.lower()
                if attr_name in HTTP_METHODS:
                    methods = [attr_name.upper()]
                elif attr_name in ("route", "api_route"):
                    methods = ["GET"]  # default Flask/FastAPI route method
                    for kw in dec.keywords:
                        if kw.arg == "methods" and isinstance(kw.value, (ast.List, ast.Tuple, ast.Set)):
                            # methods=["GET", "POST"]: the function handles every listed method
                            listed = [str(e.value).upper() for e in kw.value.elts
                                      if isinstance(e, ast.Constant) and isinstance(e.value, str)]
                            methods = listed or methods

            # First arg is typically the URL route path string
            if methods and dec.args and isinstance(dec.args[0], ast.Constant):
                val = dec.args[0].value
                if isinstance(val, str):
                    route = val

            if route:
                for method in methods:
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

            owner = (self._method_owner(self.class_stack[-1], method_name)
                     if isinstance(node.func.value, ast.Name) and node.func.value.id == "self" and self.class_stack
                     else None)
            if owner:
                # self.helper(): own class (detected), local base (inferred) or imported base (heuristic)
                owner_path, owner_cls, owner_kind, owner_conf = owner
                self.result.add_relationship(
                    Relationship(
                        source_id=self.current_callable_id,
                        target_id=make_entity_id("python", owner_path, f"{owner_cls}.{method_name}"),
                        type=RelationType.CALLS,
                        kind=owner_kind,
                        confidence=owner_conf,
                        evidence=[Evidence(snippet=snippet, location=call_loc)],
                    )
                )
            elif (mod := self.module_aliases.get(self._dotted(node.func.value) or "")) and                     (node.func.value.id if isinstance(node.func.value, ast.Name) else self._dotted(node.func.value)) not in self.var_types:
                # pricing.total(...) where `import services.pricing as pricing`: a function in that module.
                # (The resolver reports it as an external package if the module is not part of the repository.)
                self.result.add_relationship(
                    Relationship(
                        source_id=self.current_callable_id,
                        target_id=make_entity_id("python", mod, method_name),
                        type=RelationType.CALLS,
                        kind=RelationKind.DETECTED,
                        confidence=1.0,
                        evidence=[Evidence(snippet=snippet, location=call_loc)],
                    )
                )
            elif isinstance(node.func.value, ast.Name):
                receiver = node.func.value.id  # e.g. "auth_service"
            elif isinstance(node.func.value, ast.Attribute) and isinstance(node.func.value.value, ast.Name):
                receiver = f"{node.func.value.value.id}.{node.func.value.attr}"  # e.g. "self.users"

            if receiver and receiver in self.var_types:
                cls_name = self.var_types[receiver]
                mod_path = self.imported_symbols.get(cls_name) or (
                    self.rel_path if cls_name in self.local_classes else None)
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
            if func_name in self.local_functions or func_name in self.local_classes:
                # Function/class defined in this same module: syntactically certain.
                own_id = make_entity_id("python", self.rel_path, func_name)
                if self.current_callable_id != own_id:      # ignore plain recursion noise
                    self.result.add_relationship(
                        Relationship(
                            source_id=self.current_callable_id,
                            target_id=own_id,
                            type=RelationType.CALLS,
                            kind=RelationKind.DETECTED,
                            confidence=1.0,
                            evidence=[Evidence(snippet=snippet, location=call_loc)],
                        )
                    )
            # If function is imported from another module
            elif func_name in self.imported_symbols:
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
            return AnalysisResult(errors=[f"{rel_path}: syntax error at line {err.lineno}: {err.msg}"])

        source_lines = content.splitlines()
        visitor = PythonASTVisitor(rel_path=rel_path, source_lines=source_lines, tree=tree)
        visitor.visit(tree)
        return visitor.result
