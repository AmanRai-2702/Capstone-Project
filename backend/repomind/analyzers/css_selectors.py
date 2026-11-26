"""A small CSS selector engine for *static* matching of selectors against parsed HTML.

Supported
  compound parts   tag, *, #id, .class, [attr], [attr=v] [~=] [|=] [^=] [$=] [*=] (quoted or not, `i` flag)
  combinators      descendant (space) and child (>)
  pseudo-classes   structural   :first-child :last-child :only-child :nth-child(n | odd | even | an+b) :root
                   logical      :not(...) :is(...) :where(...)   (arguments: compound selectors, no combinators)
                   state        :hover :focus :active :visited :checked :disabled ... - these depend on runtime
                                state, so the element is a *possible* match, never a certain one
  pseudo-elements  ::before ::after ::placeholder ... - style generated content of the element: possible match

Not supported -> the selector matches nothing (we never guess): sibling combinators (+ ~), :has(),
:nth-of-type / :first-of-type, :empty, unknown pseudo-classes, namespaces, nesting (&).

`match()` answers with three values: NO, MAYBE (state-dependent / pseudo-element / class added by JS), YES.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

NO, MAYBE, YES = 0, 1, 2

STATE_PSEUDOS = {
    "hover", "focus", "focus-within", "focus-visible", "active", "visited", "link", "any-link", "target",
    "checked", "disabled", "enabled", "required", "optional", "read-only", "read-write", "valid", "invalid",
    "placeholder-shown", "indeterminate", "default", "in-range", "out-of-range", "user-invalid", "user-valid",
}
LEGACY_PSEUDO_ELEMENTS = {"before", "after", "first-line", "first-letter"}
_IDENT = re.compile(r"-?[_a-zA-Z\\][\w\\-]*|-?\d[\w-]*")
_ATTR = re.compile(r"""^\s*([\w:.-]+)\s*(?:([~|^$*]?=)\s*(?:"([^"]*)"|'([^']*)'|([^\s\]]+))\s*([iIsS])?)?\s*$""")
_NTH = re.compile(r"^\s*([+-]?\d*)\s*n\s*(?:([+-])\s*(\d+))?\s*$")


@dataclass
class Compound:
    tag: str | None = None                                   # lower-case, None = any
    ids: list[str] = field(default_factory=list)
    classes: list[str] = field(default_factory=list)
    attrs: list[tuple[str, str | None, str | None, bool]] = field(default_factory=list)  # name, op, value, ignore-case
    pseudos: list[tuple[str, object]] = field(default_factory=list)   # (name, argument)
    pseudo_elements: list[str] = field(default_factory=list)

    @property
    def is_universal(self) -> bool:
        return not (self.tag or self.ids or self.classes or self.attrs or self.pseudos)


@dataclass
class Selector:
    parts: list[Compound]
    combinators: list[str]                                   # " " or ">" between consecutive parts


# --------------------------------------------------------------------------- parsing
def split_top_level(text: str, sep: str = ",") -> list[str]:
    """Split on `sep` outside (), [] and quotes: 'a, :is(b, c), [x="1,2"]' -> three items."""
    out, depth, quote, cur = [], 0, "", []
    for ch in text:
        if quote:
            cur.append(ch)
            quote = "" if ch == quote else quote
        elif ch in "\"'":
            quote = ch
            cur.append(ch)
        elif ch in "([":
            depth += 1
            cur.append(ch)
        elif ch in ")]":
            depth -= 1
            cur.append(ch)
        elif ch == sep and depth == 0:
            out.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    out.append("".join(cur).strip())
    return [x for x in out if x]


def _closing(text: str, start: int, open_ch: str, close_ch: str) -> int:
    depth, quote, i = 0, "", start
    while i < len(text):
        ch = text[i]
        if quote:
            quote = "" if ch == quote else quote
        elif ch in "\"'":
            quote = ch
        elif ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _parse_compound(s: str, i: int) -> tuple[Compound | None, int]:
    comp, n, first = Compound(), len(s), True
    start = i
    while i < n and not s[i].isspace() and s[i] not in ">+~,":
        ch = s[i]
        if ch == "*" and first:
            i += 1
        elif ch in "#.":
            m = _IDENT.match(s, i + 1)
            if not m:
                return None, i
            (comp.ids if ch == "#" else comp.classes).append(m.group().replace("\\", ""))
            i = m.end()
        elif ch == "[":
            end = _closing(s, i, "[", "]")
            m = _ATTR.match(s[i + 1:end]) if end > 0 else None
            if not m:
                return None, i
            value = next((g for g in m.group(3, 4, 5) if g is not None), None)
            comp.attrs.append((m.group(1).lower(), m.group(2), value, (m.group(6) or "").lower() == "i"))
            i = end + 1
        elif ch == ":":
            is_element = s.startswith("::", i)
            m = _IDENT.match(s, i + (2 if is_element else 1))
            if not m:
                return None, i
            name, i = m.group().lower(), m.end()
            arg = None
            if i < n and s[i] == "(":
                end = _closing(s, i, "(", ")")
                if end < 0:
                    return None, i
                arg, i = s[i + 1:end], end + 1
            if is_element or (name in LEGACY_PSEUDO_ELEMENTS and arg is None):
                comp.pseudo_elements.append(name)
            else:
                parsed = _parse_pseudo_class(name, arg)
                if parsed is None:
                    return None, i
                comp.pseudos.append(parsed)
        elif first and _IDENT.match(s, i):
            m = _IDENT.match(s, i)
            comp.tag = m.group().lower()
            i = m.end()
        else:
            return None, i                                   # &, |, unknown syntax
        first = False
    if i == start:
        return None, i
    return comp, i


def _parse_pseudo_class(name: str, arg: str | None):
    if name in STATE_PSEUDOS and arg is None:
        return (name, None)
    if name in ("first-child", "last-child", "only-child", "root") and arg is None:
        return (name, None)
    if name == "nth-child" and arg is not None:
        a = arg.strip().lower()
        if a in ("odd", "even"):
            return (name, a)
        if re.fullmatch(r"[+-]?\d+", a):
            return (name, (0, int(a)))
        m = _NTH.match(a)
        if m:
            coef = m.group(1)
            step = -1 if coef == "-" else 1 if coef in ("", "+") else int(coef)
            return (name, (step, int(m.group(3) or 0) * (-1 if m.group(2) == "-" else 1)))
        return None
    if name in ("not", "is", "where") and arg:
        alts = []
        for part in split_top_level(arg):
            comp, end = _parse_compound(part, 0)
            if comp is None or end != len(part) or comp.pseudo_elements:
                return None                                  # combinators or pseudo-elements inside: unsupported
            alts.append(comp)
        return (name, alts) if alts else None
    return None


def parse_selector(text: str) -> Selector | None:
    """Parse ONE complex selector (no top-level commas). None = unsupported syntax: matches nothing."""
    s = text.strip()
    parts: list[Compound] = []
    combs: list[str] = []
    pending: str | None = None
    i, n = 0, len(s)
    while i < n:
        ch = s[i]
        if ch.isspace():
            j = i
            while j < n and s[j].isspace():
                j += 1
            if parts and pending is None and j < n and s[j] not in ">+~":
                pending = " "
            i = j
        elif ch in ">+~":
            if not parts or pending not in (None, " "):
                return None
            if ch != ">":
                return None                                  # sibling combinators are not supported
            pending = ">"
            i += 1
        else:
            comp, i = _parse_compound(s, i)
            if comp is None:
                return None
            if parts:
                if pending is None:
                    return None
                combs.append(pending)
            parts.append(comp)
            pending = None
    if not parts or (pending is not None and pending != " "):
        return None
    return Selector(parts, combs)


# --------------------------------------------------------------------------- matching
def _attr_matches(attrs: dict[str, str], name: str, op: str | None, value: str | None, icase: bool) -> bool:
    if name not in attrs:
        return False
    if op is None:
        return True
    have, want = attrs[name], value or ""
    if icase:
        have, want = have.lower(), want.lower()
    if op == "=":
        return have == want
    if op == "~=":
        return bool(want) and want in have.split()
    if op == "|=":
        return have == want or have.startswith(want + "-")
    if not want:
        return False                                         # ^= $= *= with an empty value match nothing
    return {"^=": have.startswith, "$=": have.endswith, "*=": lambda w: w in have}[op](want)


def _nth(arg, position: int) -> bool:
    if arg == "odd":
        return position % 2 == 1
    if arg == "even":
        return position % 2 == 0
    step, offset = arg
    if step == 0:
        return position == offset
    k, rem = divmod(position - offset, step)
    return rem == 0 and k >= 0


def match_compound(c: Compound, d: dict, extra_classes: frozenset[str] = frozenset(),
                   notes: list[str] | None = None) -> int:
    """NO / MAYBE / YES for one compound against an element descriptor
    {"t": tag, "i": id, "c": [classes], "a": {attrs}, "n": index among siblings, "s": sibling count}.
    `extra_classes` are classes JavaScript is seen adding to this element (they make a class miss MAYBE)."""
    if c.tag and d["t"] != c.tag:
        return NO
    if any(d["i"] != i for i in c.ids):
        return NO
    result = YES
    for cls in c.classes:
        if cls in d["c"]:
            continue
        if cls in extra_classes:
            result = min(result, MAYBE)
            if notes is not None:
                notes.append("dynamic")
        else:
            return NO
    for name, op, value, icase in c.attrs:
        if not _attr_matches(d["a"], name, op, value, icase):
            return NO
    for name, arg in c.pseudos:
        if name in STATE_PSEUDOS:
            result = min(result, MAYBE)
            if notes is not None:
                notes.append("state")
        elif name == "root":
            if d["t"] != "html":
                return NO
        elif name == "first-child":
            if d.get("n") != 0:
                return NO
        elif name == "last-child":
            if d.get("n") is None or d.get("n") != d.get("s", -1) - 1:
                return NO
        elif name == "only-child":
            if d.get("s") != 1:
                return NO
        elif name == "nth-child":
            if d.get("n") is None or not _nth(arg, d["n"] + 1):
                return NO
        elif name in ("is", "where"):
            best = max(match_compound(alt, d, notes=notes) for alt in arg)
            if best == NO:
                return NO
            result = min(result, best)
        elif name == "not":
            inner = max(match_compound(alt, d) for alt in arg)
            if inner == YES:
                return NO
            if inner == MAYBE:
                result = min(result, MAYBE)
    if c.pseudo_elements:
        result = min(result, MAYBE)
        if notes is not None:
            notes.append("pseudo-element")
    return result


def match(sel: Selector, chain: list[dict], extra_classes: frozenset[str] = frozenset(),
          notes: list[str] | None = None) -> int:
    """Does `sel` match the LAST element of `chain` (ancestors from the root down, then the element)?"""
    def at(k: int, pos: int) -> int:
        r = match_compound(sel.parts[k], chain[pos], extra_classes if k == len(sel.parts) - 1 else frozenset(),
                           notes if k == len(sel.parts) - 1 else None)
        if r == NO or k == 0:
            return r
        if sel.combinators[k - 1] == ">":
            return NO if pos == 0 else min(r, at(k - 1, pos - 1))
        best = NO
        for q in range(pos - 1, -1, -1):
            best = max(best, at(k - 1, q))
            if best == YES:
                break
        return min(r, best)

    return at(len(sel.parts) - 1, len(chain) - 1)
