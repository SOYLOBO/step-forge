"""Heuristic OpenSCAD → build123d translator.

Handles the common subset:
    variables, modules, if/else at top level,
    union/difference/intersection, translate/rotate/mirror/scale,
    linear_extrude, cube/cylinder/sphere/square/circle.

Anything it can't handle gets a `# TODO:` comment with the original source
so the user can patch the result by hand. The goal is not 100% coverage —
it's to get 80% of files compiling on the first try.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional


# ---------------------------------------------------------------- tokenize ---

TOKEN_SPEC = [
    ("BLOCKCOM", r"/\*[\s\S]*?\*/"),
    ("LINECOM",  r"//[^\n]*"),
    ("NUMBER",   r"\d+\.\d+|\d+"),
    ("STRING",   r'"[^"\n]*"'),
    ("IDENT",    r"[a-zA-Z_$][a-zA-Z_$0-9]*"),
    ("OP2",      r"==|!=|<=|>=|&&|\|\|"),
    ("PUNCT",    r"[{}()\[\];,?:=<>!+\-*/%]"),
    ("WS",       r"\s+"),
]
TOK_RE = re.compile("|".join(f"(?P<{n}>{p})" for n, p in TOKEN_SPEC))


@dataclass
class Tok:
    kind: str
    val: str
    line: int = 0


def tokenize(src: str) -> list[Tok]:
    out: list[Tok] = []
    pos = 0
    line = 1
    while pos < len(src):
        m = TOK_RE.match(src, pos)
        if m is None:
            # Unknown character — preserve it as a TODO marker so the parser
            # can flag it without dying.
            out.append(Tok("UNK", src[pos], line))
            pos += 1
            continue
        kind = m.lastgroup or ""
        val = m.group()
        if kind == "WS":
            line += val.count("\n")
            pos = m.end()
            continue
        if kind in ("LINECOM", "BLOCKCOM"):
            out.append(Tok("COMMENT", val, line))
            line += val.count("\n")
            pos = m.end()
            continue
        out.append(Tok(kind, val, line))
        pos = m.end()
    return out


# ------------------------------------------------------------------ parse ---

@dataclass
class Call:
    name: str
    args: list[str]              # positional, raw source strings (will be remapped)
    kwargs: dict[str, str]


@dataclass
class CSGNode:
    """A CSG action: head call applied to a body of children.

    body=None  → leaf (primitive or user module call), ends in `;`
    body=[...] → either { ... } block or a single chained child
    """
    head: Call
    body: Optional[list["CSGNode"]] = None
    comment: Optional[str] = None


@dataclass
class IfStmt:
    cond: str
    then: list                   # list[CSGNode | IfStmt | str]
    else_: list = field(default_factory=list)


@dataclass
class ModuleDef:
    name: str
    params: list[tuple[str, Optional[str]]]   # (name, default_source)
    body: list                                 # statements


@dataclass
class Assign:
    name: str
    expr: str


@dataclass
class CommentStmt:
    text: str


@dataclass
class RawStmt:
    """Verbatim fallback for syntax we can't parse — emitted as a TODO."""
    text: str


class ParseError(Exception):
    pass


class Parser:
    def __init__(self, src: str):
        self.src = src
        self.toks = tokenize(src)
        self.i = 0

    # ----- token cursor helpers -----
    def peek(self, k: int = 0) -> Optional[Tok]:
        j = self.i + k
        return self.toks[j] if 0 <= j < len(self.toks) else None

    def eat(self) -> Tok:
        t = self.toks[self.i]
        self.i += 1
        return t

    def at(self, kind: str, val: Optional[str] = None) -> bool:
        t = self.peek()
        if t is None:
            return False
        if t.kind != kind:
            return False
        if val is not None and t.val != val:
            return False
        return True

    def consume(self, kind: str, val: Optional[str] = None) -> Tok:
        if not self.at(kind, val):
            t = self.peek()
            where = f"line {t.line}" if t else "EOF"
            raise ParseError(f"{where}: expected {kind} {val!r}, got {t}")
        return self.eat()

    # ----- top-level -----
    def parse_program(self) -> list:
        stmts: list = []
        while self.peek() is not None:
            try:
                s = self.parse_statement()
            except ParseError as e:
                # Skip to next ';' or '}' so we can keep going.
                stmts.append(RawStmt(f"# TODO unparsed: {e}"))
                self._skip_to_recovery()
                continue
            if s is not None:
                stmts.append(s)
        return stmts

    def _skip_to_recovery(self) -> None:
        depth = 0
        while self.peek() is not None:
            t = self.eat()
            if t.val == "{":
                depth += 1
            elif t.val == "}":
                if depth == 0:
                    return
                depth -= 1
            elif t.val == ";" and depth == 0:
                return

    def parse_statement(self):
        t = self.peek()
        if t is None:
            return None
        if t.kind == "COMMENT":
            self.eat()
            return CommentStmt(t.val)
        if t.kind == "IDENT" and t.val == "module":
            return self.parse_module()
        if t.kind == "IDENT" and t.val == "function":
            # Skip OpenSCAD `function` defs for now — surface as a TODO.
            line = t.line
            buf = []
            while self.peek() is not None and self.peek().val != ";":
                buf.append(self.eat().val)
            if self.peek():
                self.eat()
            return RawStmt(f"# TODO function (line {line}): " + " ".join(buf))
        if t.kind == "IDENT" and t.val == "if":
            return self.parse_if()
        if t.kind == "IDENT" and t.val == "for":
            return self.parse_for()
        # Assignment? `IDENT = expr ;`
        if t.kind == "IDENT" and self.peek(1) is not None and self.peek(1).val == "=":
            return self.parse_assign()
        # Otherwise — a CSG action.
        return self.parse_csg()

    # ----- variables -----
    def parse_assign(self) -> Assign:
        name = self.consume("IDENT").val
        self.consume("PUNCT", "=")
        expr = self._read_expr_until([";"])
        self.consume("PUNCT", ";")
        # OpenSCAD globals like $fn / $fa / $fs aren't representable in Python.
        # Pass them through as comments so the user can see what was set.
        if name.startswith("$"):
            return RawStmt(f"# OpenSCAD setting (no build123d equivalent): {name} = {expr.strip()}")
        return Assign(name, _expr_to_py(expr.strip()))

    # ----- modules -----
    def parse_module(self) -> ModuleDef:
        self.consume("IDENT", "module")
        name = self.consume("IDENT").val
        self.consume("PUNCT", "(")
        params = self._parse_param_list()
        self.consume("PUNCT", ")")
        self.consume("PUNCT", "{")
        body: list = []
        while not self.at("PUNCT", "}"):
            s = self.parse_statement()
            if s is not None:
                body.append(s)
        self.consume("PUNCT", "}")
        return ModuleDef(name, params, body)

    def _parse_param_list(self) -> list[tuple[str, Optional[str]]]:
        params: list[tuple[str, Optional[str]]] = []
        if self.at("PUNCT", ")"):
            return params
        while True:
            name = self.consume("IDENT").val
            default = None
            if self.at("PUNCT", "="):
                self.eat()
                default = self._read_expr_until([",", ")"]).strip()
            params.append((name, default))
            if self.at("PUNCT", ","):
                self.eat()
                continue
            break
        return params

    # ----- if / for -----
    def parse_if(self) -> IfStmt:
        self.consume("IDENT", "if")
        self.consume("PUNCT", "(")
        cond = self._read_expr_until([")"]).strip()
        self.consume("PUNCT", ")")
        then = self._parse_block_or_single()
        else_: list = []
        if self.at("IDENT", "else"):
            self.eat()
            else_ = self._parse_block_or_single()
        return IfStmt(cond, then, else_)

    def parse_for(self):
        # OpenSCAD `for (i = [a:b]) child;` — translate as Python for.
        self.consume("IDENT", "for")
        self.consume("PUNCT", "(")
        var = self.consume("IDENT").val
        self.consume("PUNCT", "=")
        iter_expr = self._read_expr_until([")"]).strip()
        self.consume("PUNCT", ")")
        body = self._parse_block_or_single()
        return ("for_loop", var, iter_expr, body)

    def _parse_block_or_single(self) -> list:
        if self.at("PUNCT", "{"):
            self.eat()
            body: list = []
            while not self.at("PUNCT", "}"):
                s = self.parse_statement()
                if s is not None:
                    body.append(s)
            self.consume("PUNCT", "}")
            return body
        s = self.parse_statement()
        return [s] if s is not None else []

    # ----- CSG -----
    def parse_csg(self) -> CSGNode:
        head = self._parse_call()
        if self.at("PUNCT", "{"):
            self.eat()
            body: list = []
            while not self.at("PUNCT", "}"):
                t = self.peek()
                if t and t.kind == "COMMENT":
                    body.append(CommentStmt(self.eat().val))
                    continue
                if t and t.kind == "IDENT" and t.val == "if":
                    body.append(self.parse_if())
                    continue
                if t and t.kind == "IDENT" and t.val == "for":
                    body.append(self.parse_for())
                    continue
                body.append(self.parse_csg())
            self.consume("PUNCT", "}")
            return CSGNode(head, body)
        if self.at("PUNCT", ";"):
            self.eat()
            return CSGNode(head, None)
        # Chained child: another CSG action follows.
        child = self.parse_csg()
        return CSGNode(head, [child])

    def _parse_call(self) -> Call:
        name = self.consume("IDENT").val
        # Some OpenSCAD calls have no parens (e.g. `child();` rare). We
        # require parens — surface as RawStmt via ParseError otherwise.
        self.consume("PUNCT", "(")
        args, kwargs = self._parse_arg_list()
        self.consume("PUNCT", ")")
        return Call(name, args, kwargs)

    def _parse_arg_list(self) -> tuple[list[str], dict[str, str]]:
        args: list[str] = []
        kwargs: dict[str, str] = {}
        if self.at("PUNCT", ")"):
            return args, kwargs
        while True:
            # Keyword arg?  IDENT '=' EXPR
            if (
                self.peek() is not None
                and self.peek().kind == "IDENT"
                and self.peek(1) is not None
                and self.peek(1).val == "="
            ):
                key = self.eat().val
                self.eat()  # '='
                val = self._read_expr_until([",", ")"]).strip()
                kwargs[key] = val
            else:
                val = self._read_expr_until([",", ")"]).strip()
                args.append(val)
            if self.at("PUNCT", ","):
                self.eat()
                continue
            break
        return args, kwargs

    def _read_expr_until(self, stop: list[str]) -> str:
        """Read raw expression source until we hit one of `stop` at depth 0."""
        depth = 0
        out: list[str] = []
        while self.peek() is not None:
            t = self.peek()
            if depth == 0 and t.val in stop:
                break
            if t.val in "([{":
                depth += 1
            elif t.val in ")]}":
                if depth == 0:
                    break
                depth -= 1
            out.append(self.eat().val)
        # Add spaces around operators for readability.
        return _rejoin(out)


def _expr_to_py(s: str) -> str:
    """Light cleanup of OpenSCAD expressions for Python.

    - is_undef(x) -> (x is None)
    - undef -> None
    - true/false -> True/False
    """
    # Replace is_undef(EXPR) with (EXPR is None). Naive but works for the
    # common single-identifier case the rotor uses.
    s = re.sub(r"is_undef\(\s*([^)]+?)\s*\)", r"(\1 is None)", s)
    # Whole-word keyword swaps.
    s = re.sub(r"\bundef\b", "None", s)
    s = re.sub(r"\btrue\b", "True", s)
    s = re.sub(r"\bfalse\b", "False", s)
    return s


def _rejoin(toks: list[str]) -> str:
    # Tight join with selective spacing — keeps expressions terse but legible.
    s = ""
    for t in toks:
        if t in ",":
            s += ", "
        elif t in "+-*/%<>=!" and s and s[-1].isalnum():
            s += f" {t} "
        else:
            s += t
    return s.replace("  ", " ")


# ------------------------------------------------------------------- emit ---

TRANSFORM_OPS = {"translate", "rotate", "mirror", "scale", "color", "resize"}
EXTRUDE_OPS = {"linear_extrude", "rotate_extrude"}
CSG_OPS = {"union", "difference", "intersection"}
PRIMITIVES_3D = {"cube", "cylinder", "sphere"}
PRIMITIVES_2D = {"square", "circle", "polygon"}

# Rough mapping table for transforms.
def _emit_transform(call: Call) -> Optional[str]:
    if call.name == "translate":
        v = _vec(call.args[0] if call.args else "")
        if v is None:
            return None
        x, y, z = v
        if z is None:
            return f"Pos({x}, {y})"
        return f"Pos({x}, {y}, {z})"
    if call.name == "rotate":
        if not call.args:
            return None
        v = _vec(call.args[0])
        if v is None:
            return f"Rot(Z={call.args[0]})"
        x, y, z = v
        parts = []
        if x not in (None, "0"):
            parts.append(f"X={x}")
        if y not in (None, "0"):
            parts.append(f"Y={y}")
        if z not in (None, "0"):
            parts.append(f"Z={z}")
        if not parts:
            return "Rot()"
        return "Rot(" + ", ".join(parts) + ")"
    if call.name == "mirror":
        v = _vec(call.args[0] if call.args else "")
        return f"# TODO mirror({call.args}, {call.kwargs})  (build123d: use mirror() helper)"
    if call.name == "scale":
        return f"# TODO scale({call.args}, {call.kwargs})  (build123d: use scale() helper)"
    return None


def _vec(src: str) -> Optional[tuple]:
    """Parse `[a, b, c]` (or `[a, b]`) into a 3-tuple (z=None for 2-tuples)."""
    s = src.strip()
    if not (s.startswith("[") and s.endswith("]")):
        return None
    inner = s[1:-1]
    parts = _split_top(inner, ",")
    parts = [p.strip() for p in parts]
    if len(parts) == 2:
        return (parts[0], parts[1], None)
    if len(parts) == 3:
        return (parts[0], parts[1], parts[2])
    return None


def _split_top(s: str, sep: str) -> list[str]:
    depth = 0
    out: list[str] = []
    buf = ""
    for ch in s:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == sep and depth == 0:
            out.append(buf)
            buf = ""
        else:
            buf += ch
    if buf:
        out.append(buf)
    return out


def _emit_primitive(call: Call) -> str:
    n = call.name
    if n == "cube":
        if call.args:
            v = _vec(call.args[0])
            if v is not None:
                x, y, z = v
                return f"Box({x}, {y}, {z})"
            return f"Box({call.args[0]}, {call.args[0]}, {call.args[0]})"
        return "Box(1, 1, 1)"
    if n == "cylinder":
        d = call.kwargs.get("d") or call.kwargs.get("d1")
        r = call.kwargs.get("r") or call.kwargs.get("r1")
        h = call.kwargs.get("h") or (call.args[0] if call.args else "1")
        radius = f"({d})/2" if d else (r or "1")
        return f"Cylinder({radius}, {h})"
    if n == "sphere":
        d = call.kwargs.get("d")
        r = call.kwargs.get("r") or (call.args[0] if call.args else "1")
        radius = f"({d})/2" if d else r
        return f"Sphere({radius})"
    if n == "square":
        if call.args:
            v = _vec(call.args[0])
            if v is not None:
                x, y, _ = v
                return f"Rectangle({x}, {y})"
            return f"Rectangle({call.args[0]}, {call.args[0]})"
        return "Rectangle(1, 1)"
    if n == "circle":
        d = call.kwargs.get("d")
        r = call.kwargs.get("r") or (call.args[0] if call.args else "1")
        radius = f"({d})/2" if d else r
        return f"Circle({radius})"
    return f"# TODO primitive {n}({call.args}, {call.kwargs})"


class EmitCtx:
    """Shared state for emitting CSG. The `prelude` list collects imperative
    statements (e.g. `var = expr; if cond: var = var + branch`) that must be
    written BEFORE the expression that references them.
    """

    def __init__(self, indent: str = ""):
        self.prelude: list[str] = []
        self.counter = 0
        self.indent = indent

    def fresh(self, base: str = "_t") -> str:
        self.counter += 1
        return f"{base}{self.counter}"

    def emit(self, line: str) -> None:
        self.prelude.append(self.indent + line)


def _emit_csg(node, ctx: EmitCtx) -> str:
    if isinstance(node, CommentStmt):
        # Comments inside CSG bodies — drop them from the expression and
        # surface in the prelude so they aren't lost.
        ctx.emit(_comment_to_py(node.text))
        return ""
    if isinstance(node, IfStmt):
        # Inside an expression context: caller (union) handles by switching
        # to imperative emit. If we get here directly something's off.
        return f"None  # TODO unexpected if (cond: {node.cond})"
    if not isinstance(node, CSGNode):
        return f"None  # TODO unhandled node {node!r}"

    call = node.head
    name = call.name

    if name in TRANSFORM_OPS:
        t = _emit_transform(call)
        if t is None:
            return f"None  # TODO transform {name}({call.args})"
        child_expr = _emit_csg_body(node.body, ctx) if node.body else "None"
        return f"{t} * {child_expr}"

    if name in EXTRUDE_OPS:
        if name == "linear_extrude":
            h = call.kwargs.get("height") or (call.args[0] if call.args else "1")
            child_expr = _emit_csg_body(node.body, ctx)
            return f"extrude({child_expr}, {h})"
        return f"None  # TODO {name}({call.args}, {call.kwargs})"

    if name in CSG_OPS:
        return _emit_csg_op(node, ctx)

    if name in PRIMITIVES_3D or name in PRIMITIVES_2D:
        return _emit_primitive(call)

    # User module / function call.
    pos = ", ".join(call.args)
    kw = ", ".join(f"{k}={v}" for k, v in call.kwargs.items())
    arg_str = ", ".join(p for p in [pos, kw] if p)
    return f"{name}({arg_str})"


def _emit_csg_op(node: CSGNode, ctx: EmitCtx) -> str:
    """Union/difference/intersection — switches to imperative form when an
    `if` child appears, so conditional branches aren't silently dropped."""
    name = node.head.name
    children = list(node.body or [])
    has_if = any(isinstance(c, IfStmt) for c in children)

    if not has_if:
        exprs: list[str] = []
        for c in children:
            e = _emit_csg(c, ctx)
            if not e or e.startswith("None"):
                continue
            exprs.append(e)
        if not exprs:
            return "None"
        if name == "union":
            return _wrap_sum(exprs)
        if name == "difference":
            return _wrap_diff(exprs) if len(exprs) > 1 else exprs[0]
        if name == "intersection":
            return " & ".join(f"({e})" for e in exprs)
        return "None"

    # Imperative path. Build the result step by step into a temp variable so
    # we can conditionally append branches.
    var = ctx.fresh("_csg")
    initialized = False
    op_sym = {"union": "+", "difference": "-", "intersection": "&"}[name]

    for c in children:
        if isinstance(c, CommentStmt):
            ctx.emit(_comment_to_py(c.text))
            continue
        if isinstance(c, IfStmt):
            if not initialized:
                # Skip leading-if special case: emit an empty initializer that
                # gets overwritten in the if-branch. (Rare; falls back to None.)
                ctx.emit(f"{var} = None")
                initialized = True
            cond_py = _expr_to_py(c.cond)
            ctx.emit(f"if {cond_py}:")
            inner = EmitCtx(indent=ctx.indent + "    ")
            inner.counter = ctx.counter
            branch_exprs: list[str] = []
            for tc in c.then:
                if isinstance(tc, CSGNode):
                    e = _emit_csg(tc, inner)
                    if e:
                        branch_exprs.append(e)
                elif isinstance(tc, CommentStmt):
                    inner.emit(_comment_to_py(tc.text))
                elif isinstance(tc, Assign):
                    inner.emit(f"{tc.name} = {tc.expr}")
                elif isinstance(tc, RawStmt):
                    inner.emit(tc.text)
            ctx.counter = inner.counter
            ctx.prelude.extend(inner.prelude)
            if branch_exprs:
                combined = _wrap_sum(branch_exprs) if len(branch_exprs) > 1 else branch_exprs[0]
                # For union we ADD, for difference we SUBTRACT, etc.
                ctx.emit(f"    {var} = {var} {op_sym} ({combined})")
            # else branch (rare).
            if c.else_:
                ctx.emit("else:")
                for tc in c.else_:
                    if isinstance(tc, CSGNode):
                        e = _emit_csg(tc, ctx)
                        if e:
                            ctx.emit(f"    {var} = {var} {op_sym} ({e})")
            continue
        # Plain CSG child.
        e = _emit_csg(c, ctx)
        if not e or e.startswith("None"):
            continue
        if not initialized:
            ctx.emit(f"{var} = {e}")
            initialized = True
        else:
            ctx.emit(f"{var} = {var} {op_sym} ({e})")
    return var


def _wrap_sum(parts: list[str]) -> str:
    if len(parts) == 1:
        return parts[0]
    return "(" + "\n    + ".join(parts) + ")"


def _wrap_diff(parts: list[str]) -> str:
    head = parts[0]
    tail = parts[1:]
    return f"({head}\n    - " + "\n    - ".join(tail) + ")"


def _emit_csg_body(body: list, ctx: EmitCtx) -> str:
    """A CSG node's body holds the children of one operation. For
    transforms/extrudes there's exactly one effective child."""
    if not body:
        return "None"
    children = [b for b in body if isinstance(b, CSGNode)]
    if not children:
        return "None"
    if len(children) == 1:
        return _emit_csg(children[0], ctx)
    return _wrap_sum([_emit_csg(c, ctx) for c in children])


def _comment_to_py(text: str) -> str:
    if text.startswith("//"):
        return "#" + text[2:]
    if text.startswith("/*") and text.endswith("*/"):
        inner = text[2:-2]
        return "\n".join("#" + ln for ln in inner.splitlines())
    return f"# {text}"


# ----------------------------------------------------------- top-level emit ---

def emit_program(stmts: list) -> str:
    """Emit a build123d-flavored Python source string."""
    out: list[str] = ["from build123d import *", ""]
    has_csg_actions = False
    result_assigned = False

    def emit_stmt(s, indent: int = 0) -> list[str]:
        pad = "    " * indent
        if isinstance(s, CommentStmt):
            return [pad + _comment_to_py(s.val if hasattr(s, "val") else s.text)]
        if isinstance(s, RawStmt):
            return [pad + s.text]
        if isinstance(s, Assign):
            return [f"{pad}{s.name} = {s.expr}"]
        if isinstance(s, ModuleDef):
            args = ", ".join(
                f"{n}={d}" if d is not None else n for n, d in s.params
            )
            head = f"{pad}def {s.name}({args}):"
            inner: list[str] = []
            csg_children = [c for c in s.body if isinstance(c, CSGNode)]
            for c in s.body:
                if isinstance(c, CSGNode):
                    continue
                inner.extend(emit_stmt(c, indent + 1))
            if csg_children:
                ctx = EmitCtx(indent=pad + "    ")
                # Build a synthetic union of the module body's CSG actions and
                # emit its prelude + expr. If the union is fully inline we just
                # use _wrap_sum on the children.
                child_exprs: list[str] = []
                for c in csg_children:
                    child_exprs.append(_emit_csg(c, ctx))
                if ctx.prelude:
                    inner.extend(ctx.prelude)
                    if len(child_exprs) > 1:
                        inner.append(f"{pad}    return {_wrap_sum(child_exprs)}")
                    else:
                        inner.append(f"{pad}    return {child_exprs[0]}")
                else:
                    ret_expr = _wrap_sum(child_exprs) if len(child_exprs) > 1 else child_exprs[0]
                    inner.append(f"{pad}    return {ret_expr}")
            else:
                inner.append(f"{pad}    pass")
            return [head] + inner
        if isinstance(s, IfStmt):
            lines = [f"{pad}if {_expr_to_py(s.cond)}:"]
            for c in s.then:
                lines.extend(emit_stmt(c, indent + 1))
            if not s.then:
                lines.append(f"{pad}    pass")
            if s.else_:
                lines.append(f"{pad}else:")
                for c in s.else_:
                    lines.extend(emit_stmt(c, indent + 1))
            return lines
        if isinstance(s, CSGNode):
            ctx = EmitCtx(indent=pad)
            expr = _emit_csg(s, ctx)
            return [*ctx.prelude, f"{pad}result = {expr}"]
        if isinstance(s, tuple) and s and s[0] == "for_loop":
            _, var, it, body = s
            inner_lines: list[str] = []
            for c in body:
                inner_lines.extend(emit_stmt(c, indent + 1))
            return [
                f"{pad}# TODO for ({var} = {it}) — review",
                f"{pad}for {var} in {it}:",
                *inner_lines,
            ]
        return [f"{pad}# TODO unhandled stmt: {s!r}"]

    last_csg_idx = -1
    for i, s in enumerate(stmts):
        if isinstance(s, CSGNode):
            last_csg_idx = i

    for i, s in enumerate(stmts):
        if isinstance(s, CSGNode):
            has_csg_actions = True
            ctx = EmitCtx(indent="")
            expr = _emit_csg(s, ctx)
            out.extend(ctx.prelude)
            if i == last_csg_idx:
                out.append(f"result = {expr}")
                result_assigned = True
            else:
                out.append(f"_csg_{i} = {expr}")
        else:
            out.extend(emit_stmt(s, 0))
    if has_csg_actions and not result_assigned:
        out.append("result = _csg_0  # TODO: pick the final CSG output")
    elif not has_csg_actions:
        out.append("# TODO: no top-level CSG action found — assign your part to `result`.")
    return "\n".join(out) + "\n"


def translate(src: str) -> str:
    """Translate OpenSCAD source to build123d Python source."""
    parser = Parser(src)
    stmts = parser.parse_program()
    return emit_program(stmts)


_SCAD_MODULE_RE = re.compile(r"^\s*module\s+\w+\s*\(", re.MULTILINE)
_SCAD_FN_RE = re.compile(r"\$f[nas]\s*=")
_SCAD_OPBLOCK_RE = re.compile(
    r"\b(union|difference|intersection|linear_extrude|rotate_extrude)\s*\(",
)


def looks_like_openscad(src: str) -> bool:
    """Heuristic: would running this through the translator help?

    We're conservative — Python code with `//` floor division shouldn't trip
    this. We require either a structural OpenSCAD construct OR multiple
    leading `//` line-comments AND no Python `import`/`def` keywords.
    """
    if _SCAD_MODULE_RE.search(src):
        return True
    if _SCAD_FN_RE.search(src):
        return True
    if _SCAD_OPBLOCK_RE.search(src) and "{" in src:
        return True
    line_starts = [ln.lstrip() for ln in src.splitlines() if ln.strip()]
    if not line_starts:
        return False
    scad_comments = sum(1 for ln in line_starts if ln.startswith("//"))
    if scad_comments >= 2 and "def " not in src and "import " not in src:
        return True
    return False
