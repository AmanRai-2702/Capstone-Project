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
from .python_types import TypeIndex

log = logging.getLogger(__name__)

# Standard HTTP methods supported by FastAPI, Flask, Starlette route decorators
HTTP_METHODS = {"get", "post", "put", "delete", "patch", "options", "head"}


class PythonASTVisitor(ast.NodeVisitor):
    """
    Walks a Python AST to extract classes, functions, methods,
    FastAPI endpoints, and method/function calls.
    """

    def __init__(self, rel_path: str, source_lines: list[str], tree: ast.AST | None = None,
                 types: TypeIndex | None = None):
        self.rel_path = rel_path.replace("\\", "/")
        self.types = types
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
        self.import_names: dict[str, str] = {}      # local alias -> original name (`from x import A as B` -> {"B": "A"})
        # Variables whose class came from the repository-wide type index: name -> (defining file, class name)
        self.var_refs: dict[str, tuple[str, str]] = {}
        # name -> entity id of the call whose result it holds (`order = Order(...)`): feeds DATA_FLOW edges
        self.producers: dict[str, str] = {}
        self._call_targets: dict[int, str] = {}          # id(ast.Call) -> entity id it was resolved to
        # `self.attr` bookkeeping per class, for READS_STATE: attr -> {method id: location}
        self.attr_writes: dict[str, dict[str, SourceLocation]] = {}
        self.attr_reads: dict[str, dict[str, SourceLocation]] = {}
        self.class_attr_writes: dict[str, dict[str, dict[str, SourceLocation]]] = {}   # class -> attr -> method -> loc
        self.class_attr_reads: dict[str, dict[str, dict[str, SourceLocation]]] = {}
        # `import services.pricing as pr` -> {"pr": "services/pricing.py"}; `import a.b` -> {"a.b": "a/b.py"}
        self.module_aliases: dict[str, str] = {}

        # Pre-pass index of what this module defines, so calls to things defined *later*
        # in the file (or later in a class) still resolve.
        self.local_functions: set[str] = set()
        self.local_classes: set[str] = set()
        self.class_methods: dict[str, set[str]] = {}
        self.class_bases: dict[str, list[str]] = {}
        # class name -> [(field, required)] for classes with annotated fields (pydantic / dataclass style models)
        self.class_fields: dict[str, list[tuple[str, bool]]] = {}
        if tree is not None:
            # Functions defined inside other functions (factories like `def create_app(): def session(): ...`) are
            # callable from their siblings, so they count too. Methods (directly in a class body) do not.
            methods = {id(f) for c in ast.walk(tree) if isinstance(c, ast.ClassDef)
                       for f in c.body if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))}
            self.local_functions |= {n.name for n in ast.walk(tree)
                                     if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and id(n) not in methods}
            for node in getattr(tree, "body", []):
                if isinstance(node, ast.ClassDef):
                    self.class_fields[node.name] = [(s.target.id, s.value is None) for s in node.body
                                                    if isinstance(s, ast.AnnAssign) and isinstance(s.target, ast.Name)]
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
                return (self.imported_symbols[base], self._orig(base), RelationKind.HEURISTIC, 0.6)
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
                self.import_names[alias.asname or alias.name] = alias.name

        self.generic_visit(node)

    def _typed(self, ann: ast.expr | None) -> str | None:
        """Class named by an annotation, with repository type aliases replaced by what they stand for."""
        cls = self._annotation_class(ann)
        return self.types.unalias(cls) if cls and self.types is not None else cls

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

    # ------------------------------------------------------------------ expression types
    def _orig(self, name: str) -> str:
        """The name a symbol has where it is defined (undoes `import X as Y`)."""
        return self.import_names.get(name, name) if name in self.imported_symbols and name not in self.local_classes else name

    def _class_ref(self, name: str | None, prefer: str | None = None) -> tuple[str, str] | None:
        """(defining file, class) for a class NAME as seen from this file; None if unknown or ambiguous."""
        if not name:
            return None
        if name in self.local_classes:
            return self.rel_path, name
        if self.types is not None:
            ref = self.types.resolve_class(name, prefer)
            if ref:
                return ref
        if name in self.imported_symbols:
            return self.imported_symbols[name], self._orig(name)
        return None

    def _expr_ref(self, e: ast.expr | None) -> tuple[str, str] | None:
        """The class an expression evaluates to, when statically clear:  self, a typed variable, `x.attr`
        (attribute annotations), `f()` (return annotation), `Foo()` (instantiation), `obj.method()` (return
        annotation), `await ...`. Anything else is None - no guess."""
        if isinstance(e, ast.Await):
            return self._expr_ref(e.value)
        if isinstance(e, ast.Name):
            if e.id == "self" and self.class_stack:
                return self.rel_path, self.class_stack[-1]
            if e.id in self.var_refs:
                return self.var_refs[e.id]
            return self._class_ref(self.var_types.get(e.id))
        if isinstance(e, ast.Attribute):
            key = self._target_key(e)
            if key and key in self.var_refs:
                return self.var_refs[key]
            if key and key in self.var_types:
                return self._class_ref(self.var_types[key])
            base = self._expr_ref(e.value)
            if base and self.types is not None:
                return self._class_ref(self.types.attrs.get(base, {}).get(e.attr), base[0])
            return None
        if isinstance(e, ast.Call):
            f = e.func
            if isinstance(f, ast.Name):
                if f.id in self.local_classes or (f.id in self.imported_symbols and f.id[:1].isupper()):
                    return self._class_ref(f.id)
                if self.types is not None:
                    ret = self.types.function_return(
                        f.id, self.rel_path if f.id in self.local_functions else None, self.imported_symbols.get(f.id))
                    if ret:
                        return ret
                return None
            if isinstance(f, ast.Attribute) and self.types is not None:
                base = self._expr_ref(f.value)
                if base:
                    return self._class_ref(self.types.method_returns.get(base, {}).get(f.attr), base[0])
                mod = self.module_aliases.get(self._dotted(f.value) or "")
                if mod:
                    return self.types.function_return(f.attr, mod, mod)
        return None

    def _value_class(self, value: ast.expr) -> str | None:
        """Class an assigned expression evaluates to, when that is statically clear:
        Foo()                      constructor call
        other / self.other         copy of something whose type is already known (annotated parameter, alias)
        a or Foo()  /  Foo() if c else b     the first operand that has a known class
        """
        if isinstance(value, ast.Call) and isinstance(value.func, ast.Name):
            name = value.func.id
            if name in self.local_classes:
                return name
            if name in self.imported_symbols:
                if self.types is not None:        # an imported *function* (make_store()) is not a class
                    if name in self.types.classes:
                        return name
                    return None if (name in self.types.func_returns or not name[:1].isupper()) else name
                return name
            return None
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
        ref = self._expr_ref(node.value)
        for target in node.targets:
            key = self._target_key(target)
            if key and cls_name:
                self.var_types[key] = cls_name
            if key and ref:
                self.var_refs[key] = ref
            elif key and not cls_name:
                self.var_refs.pop(key, None)
            self._record_attr_write(target, node)
        self.generic_visit(node)           # visiting the value resolves its calls, so producers can be read now
        value = node.value.value if isinstance(node.value, ast.Await) else node.value
        produced = self._call_targets.get(id(value)) if isinstance(value, ast.Call) else None
        for target in node.targets:
            if isinstance(target, ast.Name):
                if produced:
                    self.producers[target.id] = produced
                else:
                    self.producers.pop(target.id, None)         # reassigned: no longer holds that result

    def _record_attr_write(self, target: ast.expr, node: ast.AST) -> None:
        if (self.current_callable_id and self.class_stack and isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name) and target.value.id == "self"):
            self.attr_writes.setdefault(target.attr, {}).setdefault(self.current_callable_id, SourceLocation(
                file_path=self.rel_path, start_line=node.lineno, end_line=node.end_lineno or node.lineno))

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        self._record_attr_write(node.target, node)
        if isinstance(node.target, ast.Attribute):           # `self.n += 1` also reads self.n
            self._record_attr_read(node.target, node)
        self.generic_visit(node)

    def _record_attr_read(self, node: ast.Attribute, at: ast.AST) -> None:
        if (self.current_callable_id and self.class_stack and isinstance(node.value, ast.Name)
                and node.value.id == "self" and node.attr not in self.class_methods.get(self.class_stack[-1], ())):
            self.attr_reads.setdefault(node.attr, {}).setdefault(self.current_callable_id, SourceLocation(
                file_path=self.rel_path, start_line=at.lineno, end_line=at.end_lineno or at.lineno))

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if isinstance(node.ctx, ast.Load):
            self._record_attr_read(node, node)
        self.generic_visit(node)

    def visit_Return(self, node: ast.Return) -> None:
        """`return Foo(...)` / `return typed_var`: the function RETURNS that class (inferred)."""
        if self.current_callable_id and node.value is not None:
            ref = self._expr_ref(node.value)
            if ref:
                self._add_returns(ref, node, RelationKind.INFERRED, 0.8)
        self.generic_visit(node)

    def _add_returns(self, ref: tuple[str, str], at: ast.AST, kind: RelationKind, confidence: float) -> None:
        loc = SourceLocation(file_path=self.rel_path, start_line=at.lineno, end_line=at.end_lineno or at.lineno)
        self.result.add_relationship(Relationship(
            source_id=self.current_callable_id, target_id=make_entity_id("python", ref[0], ref[1]),
            type=RelationType.RETURNS, kind=kind, confidence=confidence,
            evidence=[Evidence(snippet=self._get_snippet(loc.start_line, loc.end_line), location=loc)]))

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        """x: Foo = ...  /  self.x: Foo = ...  (the annotation wins over the value)."""
        cls_name = self._typed(node.annotation)
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
                        target_id=make_entity_id("python", base_path, self._orig(base_name) if base_path != self.rel_path else base_name),
                        type=RelationType.INHERITS,
                        kind=RelationKind.DETECTED,
                        confidence=1.0,
                        evidence=[Evidence(
                            snippet=self._get_snippet(node.lineno, node.lineno),
                            location=SourceLocation(file_path=self.rel_path, start_line=node.lineno, end_line=node.lineno),
                        )],
                    )
                )

        outer_writes, outer_reads = self.attr_writes, self.attr_reads
        self.attr_writes, self.attr_reads = {}, {}
        self.class_stack.append(node.name)
        self.generic_visit(node)
        self.class_stack.pop()
        self.class_attr_writes[node.name], self.class_attr_reads[node.name] = self.attr_writes, self.attr_reads
        self.attr_writes, self.attr_reads = outer_writes, outer_reads

    def finalize(self) -> None:
        """Once the whole module has been visited: a method that reads `self.x` depends on every method that
        writes `self.x` - in its own class or in a base class defined in this file - `__init__` included.
        Change what the writer stores and the reader may break. Inferred: we do not know which write the
        reader actually sees at runtime. (Bases from other files are not followed.)"""
        for cls, reads in self.class_attr_reads.items():
            chain, queue = [], [cls]
            while queue:                                            # the class and its local ancestors
                c = queue.pop(0)
                if c not in chain:
                    chain.append(c)
                    queue.extend(self.class_bases.get(c, []))
            for attr, readers in reads.items():
                writers = {w: loc for c in chain for w, loc in self.class_attr_writes.get(c, {}).get(attr, {}).items()}
                for reader, loc in readers.items():
                    for writer in writers:
                        if writer != reader:
                            self.result.add_relationship(Relationship(
                                source_id=reader, target_id=writer, type=RelationType.READS_STATE,
                                kind=RelationKind.INFERRED, confidence=0.8,
                                evidence=[Evidence(snippet=self._get_snippet(loc.start_line, loc.end_line), location=loc)]))

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
            if isinstance(ann, ast.Name) and arg.arg not in ("self", "cls") and \
                    (self.types is None or self.types.unalias(ann.id) == ann.id):      # an alias is not a class to USE
                if ann.id in self.local_classes:
                    type_path = self.rel_path
                elif ann.id in self.imported_symbols:
                    type_path = self.imported_symbols[ann.id]
                else:
                    continue
                self.result.add_relationship(
                    Relationship(
                        source_id=entity_id,
                        target_id=make_entity_id("python", type_path, self._orig(ann.id) if type_path != self.rel_path else ann.id),
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
        outer_refs, outer_producers = dict(self.var_refs), self.producers
        self.producers = {}
        returned = self._class_ref(self._annotation_class(node.returns))
        if returned:                                          # def f(...) -> Foo
            self._add_returns(returned, node, RelationKind.DETECTED, 1.0)
        for arg in [*node.args.args, *node.args.kwonlyargs]:
            cls = self._typed(arg.annotation)
            if cls and arg.arg not in ("self", "cls"):
                self.var_types[arg.arg] = cls                # parameter annotation = declared type
        for stmt in node.body:
            self.visit(stmt)
        # Locals and parameters must not leak into the next function; `self.x` attributes do persist
        # (they are set in __init__ and used by other methods).
        self.var_types = {**outer_types, **{k: v for k, v in self.var_types.items() if k.startswith("self.")}}
        self.var_refs = {**outer_refs, **{k: v for k, v in self.var_refs.items() if k.startswith("self.")}}
        self.producers = outer_producers
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
                        metadata={"method": method, "route": route, "framework": "fastapi",
                              **self._endpoint_contract(node, dec)},
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

    # ------------------------------------------------------------------ API contract of a route handler
    _RESPONSE_CALLS = {"jsonify", "JSONResponse", "dict"}

    def _endpoint_contract(self, fn: ast.FunctionDef | ast.AsyncFunctionDef, dec: ast.Call) -> dict:
        """What the handler returns and accepts, from the code alone:
          response_keys     keys of dict literals it returns (`return {"k": ..}`, `jsonify({..})`, `jsonify(k=..)`),
                            the fields of `response_model=Model`, and `detail` if it raises HTTPException
          response_complete False if some return could not be read (a variable, another call...)
          request_fields    {field: required} from a model-typed parameter (`body: LoginRequest`) or from
                            `data = request.get_json()` followed by `data["k"]` (required) / `data.get("k")`
        Static facts only; a handler that builds its response dynamically is reported as incomplete."""
        keys: set[str] = set()
        complete, any_return = True, False
        model = next((k.value.id for k in dec.keywords if k.arg == "response_model" and isinstance(k.value, ast.Name)), None)
        stack = list(fn.body)
        returns, raises = [], []
        while stack:                                          # this function only, not nested ones
            n = stack.pop()
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                continue
            if isinstance(n, ast.Return):
                returns.append(n)
            elif isinstance(n, ast.Raise):
                raises.append(n)
            stack.extend(ast.iter_child_nodes(n))
        for r in returns:
            if r.value is None:
                continue
            any_return = True
            found = self._dict_keys(r.value)
            if found is None:
                complete = False
            else:
                keys |= found[0]
                complete = complete and found[1]
        if any(isinstance(x.exc, ast.Call) and isinstance(x.exc.func, ast.Name) and x.exc.func.id == "HTTPException"
               for x in raises):
            keys.add("detail")
        if model in self.class_fields:
            keys = {f for f, _ in self.class_fields[model]}
            complete = True
        contract: dict = {"response_keys": sorted(keys), "response_complete": bool(complete and (any_return or model))}

        fields: dict[str, bool] = {}
        data_vars: set[str] = set()
        for arg in [*fn.args.args, *fn.args.kwonlyargs]:
            cls = self._annotation_class(arg.annotation)
            if cls in self.class_fields:
                fields.update({f: req for f, req in self.class_fields[cls]})
        for n in ast.walk(fn):
            if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name):
                v = n.value
                if (isinstance(v, ast.Call) and isinstance(v.func, ast.Attribute) and v.func.attr == "get_json"
                        and isinstance(v.func.value, ast.Name) and v.func.value.id == "request") or \
                        (isinstance(v, ast.Attribute) and v.attr == "json" and isinstance(v.value, ast.Name)
                         and v.value.id == "request"):
                    data_vars.add(n.targets[0].id)
        for n in ast.walk(fn):
            if isinstance(n, ast.Subscript) and isinstance(n.value, ast.Name) and n.value.id in data_vars \
                    and isinstance(n.slice, ast.Constant) and isinstance(n.slice.value, str):
                fields[n.slice.value] = True
            elif isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "get" \
                    and isinstance(n.func.value, ast.Name) and n.func.value.id in data_vars \
                    and n.args and isinstance(n.args[0], ast.Constant) and isinstance(n.args[0].value, str):
                fields.setdefault(n.args[0].value, False)
        if fields:
            contract["request_fields"] = dict(sorted(fields.items()))
        return contract

    def _dict_keys(self, value: ast.expr) -> tuple[set[str], bool] | None:
        """(keys, complete) for a returned dict literal / jsonify(...) call, or None if unreadable."""
        if isinstance(value, ast.Dict):
            keys = {k.value for k in value.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)}
            return keys, all(isinstance(k, ast.Constant) and isinstance(k.value, str) for k in value.keys)
        if isinstance(value, ast.Call):
            name = value.func.id if isinstance(value.func, ast.Name) else (
                value.func.attr if isinstance(value.func, ast.Attribute) else "")
            if name in self._RESPONSE_CALLS:
                if value.args and isinstance(value.args[0], ast.Dict):
                    return self._dict_keys(value.args[0])
                content = next((k.value for k in value.keywords if k.arg == "content"), None)
                if isinstance(content, ast.Dict):
                    return self._dict_keys(content)
                if not value.args and value.keywords and all(k.arg for k in value.keywords):
                    return {k.arg for k in value.keywords}, True
        return None

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
        first_new = len(self.result.relationships)

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

            is_self = isinstance(node.func.value, ast.Name) and node.func.value.id == "self"
            handled = owner is not None or (receiver and receiver in self.var_types)
            # `self.x()` was already decided by _method_owner: the class (or a local/imported base) defines x, or we
            # cannot know (an external base such as unittest.TestCase supplies it). Never invent `Class.x` for it.
            if not handled and not is_self and self.types is not None:
                # `session(id).qa.ask()`, `x.attr.method()`: the receiver is an expression, not a plain name.
                ref = self._expr_ref(node.func.value)
                if ref:
                    self.result.add_relationship(
                        Relationship(
                            source_id=self.current_callable_id,
                            target_id=make_entity_id("python", ref[0], f"{ref[1]}.{method_name}"),
                            type=RelationType.CALLS,
                            kind=RelationKind.INFERRED,
                            confidence=0.8,
                            evidence=[Evidence(snippet=snippet, location=call_loc)],
                        )
                    )
            if receiver and receiver in self.var_types:
                cls_name = self.var_types[receiver]
                mod_path = self.imported_symbols.get(cls_name) or (
                    self.rel_path if cls_name in self.local_classes else None)
                if mod_path is None and self.types is not None:
                    # the class was reached through an alias or re-export: ask the repository-wide index
                    ref = self.types.resolve_class(cls_name, self.rel_path)
                    mod_path = ref[0] if ref else None
                if mod_path:
                    target_id = make_entity_id("python", mod_path, f"{self._orig(cls_name)}.{method_name}")
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
                target_id = make_entity_id("python", mod_path, self._orig(func_name))
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

        resolved = [r.target_id for r in self.result.relationships[first_new:] if r.type == RelationType.CALLS]
        if resolved:
            self._call_targets[id(node)] = resolved[0]
        self.generic_visit(node)               # nested calls (arguments) are resolved before we look at data flow
        self._emit_data_flow(node, snippet, call_loc)

    def _emit_data_flow(self, node: ast.Call, snippet: str, loc: SourceLocation) -> None:
        """`g(x)` where `x = f(...)` (or `g(f(...))`, `g(x.attr)`): g consumes data that f produced, so a change to
        f's result can break g. Only resolved calls take part; the edge is inferred, never certain."""
        consumer = self._call_targets.get(id(node))
        if not consumer:
            return
        for arg in [*node.args, *(k.value for k in node.keywords)]:
            arg = arg.value if isinstance(arg, (ast.Starred, ast.Await)) else arg
            if isinstance(arg, ast.Call):
                producer = self._call_targets.get(id(arg))
            else:
                root = arg
                while isinstance(root, (ast.Attribute, ast.Subscript)):
                    root = root.value
                producer = self.producers.get(root.id) if isinstance(root, ast.Name) else None
            if producer and producer != consumer and producer != self.current_callable_id:
                self.result.add_relationship(Relationship(
                    source_id=consumer, target_id=producer, type=RelationType.DATA_FLOW,
                    kind=RelationKind.INFERRED, confidence=0.7, evidence=[Evidence(snippet=snippet, location=loc)]))


class PythonAnalyzer(BaseAnalyzer):
    """Parses Python source files using Python's standard library ast module."""

    def __init__(self) -> None:
        self._types: TypeIndex | None = None
        self._trees: dict[str, ast.AST] = {}
        self._sources: dict[str, str] = {}

    def prepare(self, files: dict[str, str]) -> None:
        """Optional first pass over every Python file (path -> source): builds the repository-wide type index
        (return annotations, attribute types) used to resolve calls like `session(id).qa.ask()`. The parsed trees
        are kept so analyze_file() does not parse the same text a second time."""
        self._types, self._trees = TypeIndex.build_with_trees({k.replace("\\", "/"): v for k, v in files.items()})
        self._sources = {k.replace("\\", "/"): v for k, v in files.items()}

    def analyze_file(self, rel_path: str, content: str) -> AnalysisResult:
        try:
            key = rel_path.replace("\\", "/")
            tree = self._trees.get(key) if self._sources.get(key) == content else None
            if tree is None:
                tree = ast.parse(content, filename=rel_path)
        except SyntaxError as err:
            log.warning("SyntaxError parsing %s: %s", rel_path, err)
            return AnalysisResult(errors=[f"{rel_path}: syntax error at line {err.lineno}: {err.msg}"])

        source_lines = content.splitlines()
        visitor = PythonASTVisitor(rel_path=rel_path, source_lines=source_lines, tree=tree, types=self._types)
        visitor.visit(tree)
        visitor.finalize()
        return visitor.result
