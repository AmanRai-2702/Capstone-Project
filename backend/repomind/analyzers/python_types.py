"""Repository-wide Python type facts, gathered in a cheap first pass before the per-file analysis.

The per-file analyzer cannot know that `session(repo_id)` returns a `RepoSession` (declared in the same
file here, but often in another one) or that `RepoSession.qa` is a `QAService`. This index records, from
annotations and obvious constructor assignments only:

  classes          class name -> files that define it
  func_returns     function name -> [(file, returned class name)]       def f(...) -> Cls
  method_returns   (file, class) -> {method: returned class name}
  attrs            (file, class) -> {attribute: class name}             `qa: QAService`, `self.x: T`,
                                                                        `self.x = Foo()`, `self.x = param`
                                                                        (param annotated)

A class name is only turned into a concrete class when the repository defines exactly one class of that
name (or the file itself defines it): ambiguity means "unknown", never a guess.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass, field


def annotation_class(ann: ast.expr | None) -> str | None:
    """Class named by an annotation: Foo, "Foo", Optional[Foo], Foo | None, list[Foo] is NOT a Foo."""
    if isinstance(ann, ast.Name):
        return ann.id
    if isinstance(ann, ast.Constant) and isinstance(ann.value, str) and ann.value.isidentifier():
        return ann.value
    if isinstance(ann, ast.Subscript) and isinstance(ann.value, ast.Name) and ann.value.id == "Optional":
        return annotation_class(ann.slice)
    if isinstance(ann, ast.BinOp) and isinstance(ann.op, ast.BitOr):
        for side in (ann.left, ann.right):
            if not (isinstance(side, ast.Constant) and side.value is None):
                return annotation_class(side)
    return None


@dataclass
class TypeIndex:
    classes: dict[str, list[str]] = field(default_factory=dict)
    func_returns: dict[str, list[tuple[str, str]]] = field(default_factory=dict)
    method_returns: dict[tuple[str, str], dict[str, str]] = field(default_factory=dict)
    attrs: dict[tuple[str, str], dict[str, str]] = field(default_factory=dict)
    # module-level `Name = Annotated[Real, ...]` / `Name = Optional[Real]`: Name is an ALIAS, not a class. Value is the
    # real class name, or "" when it is not a repository class we can name (then a variable typed Name is untyped).
    aliases: dict[str, str] = field(default_factory=dict)
    # method name -> every (file, class) that defines it. Used only for a LABELLED guess when a receiver is untyped.
    method_owners: dict[str, list[tuple[str, str]]] = field(default_factory=dict)

    # ------------------------------------------------------------------ building
    @classmethod
    def build(cls, sources: dict[str, str]) -> "TypeIndex":
        return cls.build_with_trees(sources)[0]

    @classmethod
    def build_with_trees(cls, sources: dict[str, str]) -> "tuple[TypeIndex, dict[str, ast.AST]]":
        """The index plus the parsed trees (so the caller can reuse them instead of parsing again)."""
        idx = cls()
        trees: dict[str, ast.AST] = {}
        for path, text in sources.items():
            try:
                trees[path] = ast.parse(text)
            except (SyntaxError, ValueError):
                continue
            for node in ast.walk(trees[path]):
                if isinstance(node, ast.ClassDef):
                    idx.classes.setdefault(node.name, [])
                    if path not in idx.classes[node.name]:
                        idx.classes[node.name].append(path)
        for path, tree in trees.items():
            idx._collect(path, tree)
        return idx, trees

    def _collect_aliases(self, tree: ast.AST) -> None:
        for stmt in getattr(tree, "body", []):
            if not isinstance(stmt, ast.Assign) or len(stmt.targets) != 1 or not isinstance(stmt.targets[0], ast.Name):
                continue
            v = stmt.value
            if isinstance(v, ast.Subscript) and isinstance(v.value, ast.Name) and v.value.id in ("Annotated", "Optional", "Union"):
                inner = v.slice.elts[0] if isinstance(v.slice, ast.Tuple) and v.slice.elts else v.slice
                self.aliases[stmt.targets[0].id] = annotation_class(inner) or ""

    def _collect(self, path: str, tree: ast.AST) -> None:
        self._collect_aliases(tree)
        class_bodies = {id(f): c for c in ast.walk(tree) if isinstance(c, ast.ClassDef)
                        for f in c.body if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))}
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                ret = annotation_class(node.returns)
                owner = class_bodies.get(id(node))
                if ret and owner is None:
                    self.func_returns.setdefault(node.name, []).append((path, ret))
                elif ret and owner is not None:
                    self.method_returns.setdefault((path, owner.name), {})[node.name] = ret
            elif isinstance(node, ast.ClassDef):
                self._collect_class_attrs(path, node)

    def _collect_class_attrs(self, path: str, cls_node: ast.ClassDef) -> None:
        for fn in cls_node.body:
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and not (fn.name.startswith("__") and fn.name.endswith("__")):
                self.method_owners.setdefault(fn.name, []).append((path, cls_node.name))
        found = self.attrs.setdefault((path, cls_node.name), {})
        for stmt in cls_node.body:                                    # dataclass / pydantic style fields
            if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                name = annotation_class(stmt.annotation)
                if name:
                    found[stmt.target.id] = name
        for fn in cls_node.body:
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            params = {a.arg: annotation_class(a.annotation) for a in [*fn.args.args, *fn.args.kwonlyargs]}
            for node in ast.walk(fn):
                if isinstance(node, ast.AnnAssign) and _is_self_attr(node.target):
                    name = annotation_class(node.annotation)
                    if name:
                        found[node.target.attr] = name
                elif isinstance(node, ast.Assign):
                    for target in node.targets:
                        if _is_self_attr(target):
                            name = self._assigned_class(node.value, params)
                            if name:
                                found.setdefault(target.attr, name)

    @staticmethod
    def _assigned_class(value: ast.expr, params: dict[str, str | None]) -> str | None:
        """Foo() / param (annotated) / a or Foo() / Foo() if c else x  -> class name."""
        if isinstance(value, ast.Call) and isinstance(value.func, ast.Name):
            return value.func.id if value.func.id[:1].isupper() else None   # constructor-like only
        if isinstance(value, ast.Name):
            return params.get(value.id)
        if isinstance(value, ast.BoolOp):
            return next((c for v in value.values if (c := TypeIndex._assigned_class(v, params))), None)
        if isinstance(value, ast.IfExp):
            return TypeIndex._assigned_class(value.body, params) or TypeIndex._assigned_class(value.orelse, params)
        return None

    # ------------------------------------------------------------------ lookups
    def unalias(self, name: str | None) -> str | None:
        """The class a type name really stands for: aliases are replaced by their target ("" -> None)."""
        if name and name in self.aliases and name not in self.classes:
            return self.aliases[name] or None
        return name

    def resolve_class(self, name: str | None, prefer_file: str | None = None) -> tuple[str, str] | None:
        """(defining file, class name) if the class is unambiguous: defined in `prefer_file`, or the only
        class with that name in the repository."""
        if not name:
            return None
        files = self.classes.get(name, [])
        if prefer_file in files:
            return prefer_file, name
        return (files[0], name) if len(files) == 1 else None

    def function_return(self, name: str, file_hint: str | None, imported_from: str | None) -> tuple[str, str] | None:
        """Class returned by function `name`: the one in this file, else the one in the module it was imported
        from, else the only function of that name in the repository."""
        cands = self.func_returns.get(name, [])
        if not cands:
            return None
        local = [c for c in cands if c[0] == file_hint]
        if local:
            return self.resolve_class(local[0][1], local[0][0])
        if imported_from:
            hit = [c for c in cands if c[0] == imported_from or c[0].endswith("/" + imported_from)]
            if len(hit) == 1:
                return self.resolve_class(hit[0][1], hit[0][0])
        return self.resolve_class(cands[0][1], cands[0][0]) if len(cands) == 1 else None


def _is_self_attr(node: ast.expr) -> bool:
    return isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "self"
