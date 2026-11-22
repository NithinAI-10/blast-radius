"""Turn a unified diff against the pinned sqlglot into candidate tests.

The map (built from per-test traces of the pinned commit) gives, as bitsets:
  rl[file][line]  tests whose *function* code executed that line
  il[file][line]  tests that executed that line as import-time code
  types[cls]      tests that called a method on an instance of cls (or subclass)
  opens[file]     tests that opened the file
Every rule below over-approximates: a candidate that cannot fail only costs
CPU when we verify it, a missed failure costs 300 s.
"""

from __future__ import annotations

import ast
import copy
import bisect
import io
import re
import tokenize

_NL = re.compile(r"\n")
HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


# ---------------------------------------------------------------- patch parsing


class FileChange:
    __slots__ = ("old_path", "new_path", "hunks")

    def __init__(self, old_path, new_path):
        self.old_path = old_path
        self.new_path = new_path
        self.hunks = []  # (old_start, old_lines[list of (tag, text)])

    @property
    def path(self):
        return self.new_path if self.new_path is not None else self.old_path


def _strip_prefix(p):
    p = p.split("\t", 1)[0].strip()
    if p == "/dev/null":
        return None
    if p.startswith("a/") or p.startswith("b/"):
        return p[2:]
    return p


def parse_patch(text: str) -> list[FileChange]:
    files: list[FileChange] = []
    cur = None
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        ln = lines[i]
        if ln.startswith("--- ") and i + 1 < len(lines) and lines[i + 1].startswith("+++ "):
            cur = FileChange(_strip_prefix(ln[4:]), _strip_prefix(lines[i + 1][4:]))
            files.append(cur)
            i += 2
            continue
        m = HUNK.match(ln)
        if m and cur is not None:
            old_start = int(m.group(1))
            old_len = int(m.group(2)) if m.group(2) is not None else 1
            new_len = int(m.group(4)) if m.group(4) is not None else 1
            body = []
            i += 1
            o = n = 0
            while i < len(lines) and (o < old_len or n < new_len):
                b = lines[i]
                if b.startswith("\\"):
                    i += 1
                    continue
                tag = b[:1] if b else " "
                body.append((tag, b[1:]))
                if tag in (" ", "-"):
                    o += 1
                if tag in (" ", "+"):
                    n += 1
                i += 1
            while i < len(lines) and lines[i].startswith("\\"):
                i += 1
            cur.hunks.append((old_start, body))
            continue
        i += 1
    return files


def apply_change(old_text: str | None, fc: FileChange):
    """Return (new_text, removed_old_lines, insert_gaps, edits).

    edits: list of (old_line_or_None, gap_after_old_line, new_text_line, old_text_line)
    describing each changed line pairing for column-level localisation.
    """
    old = old_text.split("\n") if old_text is not None else []
    out = []
    pos = 0  # 0-based index into old
    removed = set()
    gaps = set()  # insertion after old line g (1-based; 0 = top of file)
    pairs = []  # (old_lineno, old_text, new_text) for modified lines
    for old_start, body in fc.hunks:
        start = max(old_start - 1, 0) if old_start > 0 else 0
        out.extend(old[pos:start])
        pos = start
        k = 0
        while k < len(body):
            tag, txt = body[k]
            if tag == " ":
                out.append(old[pos] if pos < len(old) else txt)
                pos += 1
                k += 1
                continue
            # collect a block of - then +
            minus, plus = [], []
            while k < len(body) and body[k][0] == "-":
                minus.append(body[k][1])
                k += 1
            while k < len(body) and body[k][0] == "+":
                plus.append(body[k][1])
                k += 1
            first_old = pos + 1
            for j in range(len(minus)):
                removed.add(pos + 1)
                pos += 1
            if minus and plus:
                for j in range(min(len(minus), len(plus))):
                    pairs.append((first_old + j, minus[j], plus[j]))
                if len(plus) > len(minus):
                    gaps.add(first_old + len(minus) - 1)
            elif plus:
                gaps.add(first_old - 1)
            out.extend(plus)
    out.extend(old[pos:])
    return "\n".join(out), removed, gaps, pairs


# ---------------------------------------------------------------- python analysis

SKIP_TOK = {tokenize.COMMENT, tokenize.NL, tokenize.ENCODING}


def _tokens(src: str):
    try:
        return [
            (t.type, t.string)
            for t in tokenize.generate_tokens(io.StringIO(src).readline)
            if t.type not in SKIP_TOK
        ]
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return None


def tokens_equal(a: str, b: str) -> bool:
    ta, tb = _tokens(a), _tokens(b)
    return ta is not None and ta == tb


def _coding_line(src: str) -> bool:
    for ln in src.split("\n")[:2]:
        if re.match(r"^[ \t\f]*#.*?coding[:=][ \t]*[-\w.]+", ln):
            return True
    return False


class PyIndex:
    """Parent links and statement lookup for one source file."""

    def __init__(self, src: str):
        self.tree = ast.parse(src)
        self.parent = {}
        for node in ast.walk(self.tree):
            for ch in ast.iter_child_nodes(node):
                self.parent[ch] = node

    def nodes_at(self, line: int, c0: int | None = None, c1: int | None = None):
        """Deepest nodes whose span covers (line, c0..c1)."""
        best = []
        for node in ast.walk(self.tree):
            if not hasattr(node, "lineno") or node.end_lineno is None:
                continue
            if not (node.lineno <= line <= node.end_lineno):
                continue
            if c0 is not None:
                s = node.col_offset if node.lineno == line else -1
                e = node.end_col_offset if node.end_lineno == line else 10**9
                if not (s <= c0 and c1 <= e):
                    continue
            best.append(node)
        return best

    def deepest(self, line, c0=None, c1=None):
        cands = self.nodes_at(line, c0, c1)
        if not cands:
            return None
        # deepest = the one with most ancestors
        def depth(n):
            d = 0
            while n in self.parent:
                n = self.parent[n]
                d += 1
            return d

        return max(cands, key=depth)

    def ancestors(self, node):
        out = []
        while node in self.parent:
            node = self.parent[node]
            out.append(node)
        return out


FUNC = (ast.FunctionDef, ast.AsyncFunctionDef)


def _in_func_body(node, fn) -> bool:
    if isinstance(fn, ast.Lambda):
        b = fn.body
        return (b.lineno, b.col_offset) <= (node.lineno, node.col_offset) and (
            node.end_lineno,
            node.end_col_offset,
        ) <= (b.end_lineno, b.end_col_offset)
    first = fn.body[0]
    return node.lineno >= first.lineno and node is not fn


def _stmt_of(idx: PyIndex, node):
    n = node
    while n is not None and not isinstance(n, ast.stmt):
        n = idx.parent.get(n)
    return n


def _header_span(stmt):
    """Lines of a statement that run every time it is reached."""
    body = getattr(stmt, "body", None)
    if isinstance(body, list) and body and isinstance(stmt, (ast.If, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith, ast.Try, ast.Match)):
        end = max(stmt.lineno, body[0].lineno - 1)
        return range(stmt.lineno, end + 1)
    if isinstance(stmt, FUNC + (ast.ClassDef,)):
        end = stmt.body[0].lineno - 1
        start = min([d.lineno for d in stmt.decorator_list] + [stmt.lineno])
        return range(start, max(end, stmt.lineno) + 1)
    return range(stmt.lineno, stmt.end_lineno + 1)


def _bits_lines(m, lines):
    b = 0
    if m:
        for ln in lines:
            b |= m.get(ln, 0)
    return b


def _bits_file(m):
    b = 0
    if m:
        for v in m.values():
            b |= v
    return b


def module_name(path: str) -> str:
    mod = path[:-3].replace("/", ".")
    if mod.endswith(".__init__"):
        mod = mod[: -len(".__init__")]
    return mod


class Ctx:
    """What one change affects. bits: candidate tests. why: notes for stderr."""

    def __init__(self, refs=None):
        self.bits = 0
        self.why = []
        self.broad = False  # true when we fell back to a coarse rule
        self.refs = refs  # refs(name, path, stmt) -> bits of tests running a line that mentions name
        self.spans = []  # [(path, range)] runtime lines whose execution the change can alter
        self.coarse = False  # some change is not tied to specific runtime lines
        self.kinds = set()  # kinds of change seen (runtime, weak, class-attr, data, ...)
        self.dict_changes = {}  # path -> {statement line: (name, {key labels})}
        self.argtypes_changes = {}  # path -> {statement line: (class, {arg: (old, new required)})}


def analyze_location(M, path, idx: PyIndex, line, c0, c1, ctx: Ctx, insertion=False, flip=False):
    rl = M["rl"].get(path, {})
    il = M["il"].get(path, {})
    node = idx.deepest(line, c0, c1) if c0 is not None else None
    if node is None:
        node = idx.deepest(line)
    if node is None:
        # blank area of the module: anything importing it
        ctx.bits |= _bits_file(il) | _bits_file(rl)
        ctx.broad = True
        ctx.coarse = True
        ctx.why.append(f"{path}:{line} module-blank")
        return
    anc = [node] + idx.ancestors(node)

    # innermost function-like scope whose *body* holds the change
    fn = None
    for a in anc[1:]:
        if isinstance(a, FUNC + (ast.Lambda,)):
            if _in_func_body(node, a):
                fn = a
            break
        if isinstance(a, ast.ClassDef):
            break

    stmt = _stmt_of(idx, node)

    if stmt is not None and _is_docstring(idx, stmt):
        ctx.why.append(f"{path}:{line} docstring")
        return

    if fn is not None:
        if insertion:
            # code inserted inside a block: anything that runs the enclosing function
            span = range(fn.lineno, fn.end_lineno + 1)
        elif isinstance(fn, ast.Lambda):
            span = range(fn.body.lineno, fn.body.end_lineno + 1)
        else:
            span = _header_span(stmt) if stmt is not None else range(line, line + 1)
        b = _bits_lines(rl, span)
        ctx.bits |= b
        ctx.spans.append((path, span))
        ctx.why.append(f"{path}:{line} runtime span={span.start}-{span.stop-1} n={b.bit_count()}")
        return

    # Import-time code. Find the innermost function whose header holds it
    # (decorators, defaults, annotations) and the enclosing class chain.
    hdr_fn = None
    for a in anc:
        if isinstance(a, FUNC):
            hdr_fn = a
            break
        if isinstance(a, ast.ClassDef):
            break
    if hdr_fn is not None:
        body_span = range(hdr_fn.body[0].lineno, hdr_fn.end_lineno + 1)
        b = _bits_lines(rl, body_span)
        ctx.bits |= b
        ctx.spans.append((path, body_span))
        ctx.why.append(f"{path}:{line} def-header {hdr_fn.name} n={b.bit_count()}")
        # decorator changes can also alter registration at import time
        hdr_start = min([d.lineno for d in hdr_fn.decorator_list] + [hdr_fn.lineno])
        ctx.spans.append((path, range(hdr_start, hdr_fn.body[0].lineno)))
        if any(d.lineno <= line <= d.end_lineno for d in hdr_fn.decorator_list):
            cls_bits = _class_bits(M, path, idx, anc)
            if cls_bits is not None:
                ctx.bits |= cls_bits
        return

    cls_bits = _class_bits(M, path, idx, anc)
    at = ctx.argtypes_changes.get(path, {}).get(stmt.lineno) if stmt is not None else None
    atm = M.get("argtypes")
    if cls_bits is not None and at is not None and atm is not None and atm["ok"]:
        # arg_types values only decide required_args, which only error_messages reads:
        # tests that validated an affected class while a flipped arg was missing
        cname, flips = at
        args = [k for k, (o, n) in flips.items() if o != n]
        classes = set(atm["derived"].get(cname, [cname])) | set(atm["derived"].get("*", ())) | {cname}
        b = atm["failed"]
        for c in classes:
            for k in args:
                b |= atm["missing"].get(f"{c}.{k}", 0)
        ctx.bits |= b
        ctx.spans.append((path, range(stmt.lineno, stmt.end_lineno + 1)))
        ctx.why.append(f"{path}:{line} arg-types {cname}[{','.join(args)}] n={b.bit_count()}")
        return
    de = ctx.dict_changes.get(path, {}).get(stmt.lineno) if stmt is not None else None
    dk = M.get("dk")
    if cls_bits is not None and de is not None and dk is not None:
        name, labels = de
        if name not in dk["unsafe"] and not (labels & dk["unsafe_keys"].get(name, set())):
            # only constants in these entries' values changed: tests that read them
            b = dk["whole"].get(name, 0) | dk["failed"]
            for lb in labels:
                b |= dk["keys"].get(name, {}).get(lb, 0)
            ctx.bits |= b
            ctx.spans.append((path, range(stmt.lineno, stmt.end_lineno + 1)))
            ctx.why.append(f"{path}:{line} dict-entry {name}[{','.join(sorted(labels))[:60]}] n={b.bit_count()}")
            return
    if cls_bits is not None:
        names = _assigned_names(idx, stmt, anc)
        if names and ctx.refs is not None:
            ref = 0
            for nm in names:
                ref |= ctx.refs(nm, path, stmt, flip=flip)
            narrowed = cls_bits & ref
            ctx.bits |= narrowed
            ctx.spans.append((path, range(stmt.lineno, stmt.end_lineno + 1)))
            ctx.why.append(f"{path}:{line} class-attr {','.join(names)} n={narrowed.bit_count()} (class n={cls_bits.bit_count()})")
            return
        ctx.bits |= cls_bits
        if stmt is not None:
            ctx.spans.append((path, range(stmt.lineno, stmt.end_lineno + 1)))
        else:
            ctx.coarse = True
        ctx.why.append(f"{path}:{line} class-body n={cls_bits.bit_count()}")
        return

    # Module-level constant: tests that run a line reading it.
    names = _module_assigned_names(idx, stmt)
    if names and ctx.refs is not None:
        ref = 0
        for nm in names:
            ref |= ctx.refs(nm, path, stmt, flip=flip)
        ctx.bits |= ref
        ctx.spans.append((path, range(stmt.lineno, stmt.end_lineno + 1)))
        ctx.why.append(f"{path}:{line} module-const {','.join(names)} n={ref.bit_count()}")
        return

    # Plain module-level statement: every test that imported the module.
    b = _bits_file(il) | _bits_file(rl)
    ctx.bits |= b
    ctx.broad = True
    if stmt is not None:
        ctx.spans.append((path, range(stmt.lineno, stmt.end_lineno + 1)))
    else:
        ctx.coarse = True
    ctx.why.append(f"{path}:{line} module-level n={b.bit_count()}")


def _is_docstring(idx, stmt) -> bool:
    if not (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant) and isinstance(stmt.value.value, str)):
        return False
    par = idx.parent.get(stmt)
    body = getattr(par, "body", None)
    return isinstance(par, (ast.Module, ast.ClassDef) + FUNC) and isinstance(body, list) and body and body[0] is stmt


def _assigned_names(idx, stmt, anc):
    """Names bound by a statement sitting directly in a class body."""
    if stmt is None or not isinstance(idx.parent.get(stmt), ast.ClassDef):
        return None
    if isinstance(stmt, ast.Assign):
        targets = stmt.targets
    elif isinstance(stmt, (ast.AnnAssign, ast.AugAssign)):
        targets = [stmt.target]
    else:
        return None
    names = []
    for t in targets:
        if isinstance(t, ast.Name):
            names.append(t.id)
        elif isinstance(t, ast.Tuple) and all(isinstance(e, ast.Name) for e in t.elts):
            names.extend(e.id for e in t.elts)
        else:
            return None
    return names or None


def _module_assigned_names(idx, stmt):
    """Names bound by a simple assignment directly at module level (not __all__ and the like)."""
    if stmt is None or not isinstance(idx.parent.get(stmt), ast.Module):
        return None
    if isinstance(stmt, ast.Assign):
        targets = stmt.targets
    elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
        targets = [stmt.target]
    else:
        return None
    names = []
    for t in targets:
        if isinstance(t, ast.Name) and not (t.id.startswith("__") and t.id.endswith("__")):
            names.append(t.id)
        else:
            return None
    return names or None


def _class_bits(M, path, idx, anc):
    chain = [a.name for a in reversed(anc) if isinstance(a, ast.ClassDef)]
    if not chain:
        return None
    # qualname of the innermost class, built from the enclosing classes
    # (classes nested inside functions do not appear in our type map).
    for a in anc:
        if isinstance(a, FUNC):
            return None
    q = f"{module_name(path)}:{'.'.join(chain)}"
    b = M["types"].get(q, 0)
    # Class bodies also run at import; a class nobody instantiates may still be
    # read through the class (enums, constant holders). Fall back to importers.
    if b == 0:
        il = M["il"].get(path, {})
        rl = M["rl"].get(path, {})
        b = _bits_file(il) | _bits_file(rl)
    return b


# operator swaps the probe map can decide: old token -> (new token, ast op type)
_SWAPS = [
    (re.compile(r"\bis not\b"), "!=", ast.IsNot),
    (re.compile(r"\bis\b(?! not\b)"), "==", ast.Is),
    (re.compile(r"(?<![<>=!])<(?![<=])"), "<=", ast.Lt),
    (re.compile(r"(?<![<>=!])<=(?!=)"), "<", ast.LtE),
    (re.compile(r"(?<![<>=!-])>(?![>=])"), ">=", ast.Gt),
    (re.compile(r"(?<![<>=!])>=(?!=)"), ">", ast.GtE),
]


def operator_swap(old: str, new: str):
    """If new == old with one comparison operator swapped (probe-decidable), return (col, op_type)."""
    found = None
    for pat, rep, op in _SWAPS:
        for m in pat.finditer(old):
            for r in ([rep, f" {rep} "] if op in (ast.IsNot, ast.Is) else [rep]):
                cand = old[: m.start()] + r + old[m.end():]
                if cand == new or (op in (ast.IsNot, ast.Is) and old[: m.start()] + rep + old[m.end():] == new):
                    if found is not None and found != (m.start(), op):
                        return None
                    found = (m.start(), op)
    return found


_BOOL = re.compile(r"\b(and|or)\b")


def boolop_swap(old: str, new: str):
    """If new == old with exactly one `and`/`or` swapped, return (col, ast op type of the original)."""
    found = None
    for m in _BOOL.finditer(old):
        rep = "or" if m.group(1) == "and" else "and"
        if old[: m.start()] + rep + old[m.end():] == new:
            if found is not None:
                return None
            found = (m.start(), ast.And if m.group(1) == "and" else ast.Or)
    return found


def boolop_site(idx: "PyIndex", path: str, line: int, col: int, op):
    """(site, position) of the BoolOp whose operator at (line, col) is being swapped."""
    best = None
    for node in ast.walk(idx.tree):
        if not isinstance(node, ast.BoolOp) or not isinstance(node.op, op):
            continue
        for i in range(1, len(node.values)):
            a, b = node.values[i - 1], node.values[i]
            if (a.end_lineno, a.end_col_offset) <= (line, col) < (b.lineno, b.col_offset):
                span = (node.end_lineno - node.lineno, node.end_col_offset - node.col_offset)
                if best is None or span < best[2]:
                    best = (node, i, span)
    if best is None:
        return None
    node, i, _ = best
    return f"{path}:{node.lineno}:{node.col_offset}:{node.end_lineno}:{node.end_col_offset}", i


def probe_site(idx: "PyIndex", path: str, line: int, col: int, op):
    """Site key of the single-op Compare whose operator sits at (line, col)."""
    best = None
    for node in ast.walk(idx.tree):
        if not isinstance(node, ast.Compare) or len(node.ops) != 1 or type(node.ops[0]) is not op:
            continue
        L, R = node.left, node.comparators[0]
        if (L.end_lineno, L.end_col_offset) <= (line, col) < (R.lineno, R.col_offset):
            if best is None or (node.lineno, node.col_offset) > (best.lineno, best.col_offset):
                best = node
    if best is None:
        return None
    return f"{path}:{best.lineno}:{best.col_offset}:{best.end_lineno}:{best.end_col_offset}"


def _is_bool_flip(old: str, new: str) -> bool:
    """`NAME = True` <-> `NAME = False` (one literal swapped, nothing else)."""
    m = re.match(r"^(\s*\w+\s*(?::[^=]+)?=\s*)(True|False)(\s*(#.*)?)$", old)
    if not m:
        return False
    other = "False" if m.group(2) == "True" else "True"
    return new == m.group(1) + other + m.group(3)


def col_span(old: str, new: str):
    i = 0
    while i < min(len(old), len(new)) and old[i] == new[i]:
        i += 1
    j = 0
    while j < min(len(old), len(new)) - i and old[len(old) - 1 - j] == new[len(new) - 1 - j]:
        j += 1
    return i, max(i, len(old) - j)


def _key_label(k):
    if isinstance(k, ast.Constant) and isinstance(k.value, (str, int, float, bool)):
        return str(k.value)
    if isinstance(k, ast.Attribute):
        return k.attr
    return None


def _const_free(node):
    """ast.dump with every constant's value blanked: equal for two trees that differ
    only in constants."""
    node = copy.deepcopy(node)
    for n in ast.walk(node):
        if isinstance(n, ast.Constant):
            n.value = None
    return ast.dump(node, include_attributes=False)


def _class_dicts(tree):
    """{(class path, name): Assign/AnnAssign node} for dict literals bound to one name
    in class bodies."""
    out = {}
    stack = [((), n) for n in tree.body if isinstance(n, ast.ClassDef)]
    while stack:
        path, cls = stack.pop()
        here = path + (cls.name,)
        for st in cls.body:
            if isinstance(st, ast.ClassDef):
                stack.append((here, st))
            elif isinstance(st, (ast.Assign, ast.AnnAssign)) and isinstance(st.value, ast.Dict):
                targets = st.targets if isinstance(st, ast.Assign) else [st.target]
                if len(targets) == 1 and isinstance(targets[0], ast.Name):
                    out[(here, targets[0].id)] = st
    return out


def dict_entry_changes(old_tree, new_tree):
    """{old statement line: (name, {key labels})} for class-level dict literals whose
    keys are unchanged and where only constants inside some entries' values changed."""
    out = {}
    try:
        new = _class_dicts(new_tree)
        for key, ost in _class_dicts(old_tree).items():
            nst = new.get(key)
            if nst is None:
                continue
            od, nd = ost.value, nst.value
            if ast.dump(od, include_attributes=False) == ast.dump(nd, include_attributes=False):
                continue
            if len(od.keys) != len(nd.keys):
                continue
            labels = set()
            ok = True
            for ok_, nk, ov, nv in zip(od.keys, nd.keys, od.values, nd.values):
                if (ok_ is None) != (nk is None):
                    ok = False
                    break
                if ok_ is not None and ast.dump(ok_, include_attributes=False) != ast.dump(nk, include_attributes=False):
                    ok = False
                    break
                if ast.dump(ov, include_attributes=False) == ast.dump(nv, include_attributes=False):
                    continue
                lb = _key_label(ok_) if ok_ is not None else None
                if lb is None or _const_free(ov) != _const_free(nv):
                    ok = False
                    break
                labels.add(lb)
            if ok and labels:
                out[ost.lineno] = (key[1], labels)
    except Exception:  # noqa: BLE001
        return {}
    return out


def argtypes_changes(old_tree, new_tree):
    """{old statement line: (class name, {arg: (old required, new required)})} for
    arg_types dict literals whose keys are unchanged and whose changed values are all
    constants (required-ness is their truth value)."""
    out = {}
    try:
        new = _class_dicts(new_tree)
        for key, ost in _class_dicts(old_tree).items():
            if key[1] != "arg_types":
                continue
            nst = new.get(key)
            if nst is None:
                continue
            od, nd = ost.value, nst.value
            if len(od.keys) != len(nd.keys):
                continue
            flips, ok = {}, True
            for ok_, nk, ov, nv in zip(od.keys, nd.keys, od.values, nd.values):
                if (ok_ is None) != (nk is None) or (ok_ is not None and ast.dump(ok_) != ast.dump(nk)):
                    ok = False
                    break
                if ast.dump(ov) == ast.dump(nv):
                    continue
                if not (isinstance(ok_, ast.Constant) and isinstance(ov, ast.Constant) and isinstance(nv, ast.Constant)):
                    ok = False
                    break
                flips[str(ok_.value)] = (bool(ov.value), bool(nv.value))
            if ok and flips:
                out[ost.lineno] = (key[0][-1], flips)
    except Exception:  # noqa: BLE001
        return {}
    return out


def analyze_py(M, path, old_src, new_src, removed, gaps, pairs, ctx: Ctx):
    if tokens_equal(old_src, new_src) and not (_coding_line(new_src) != _coding_line(old_src)):
        ctx.why.append(f"{path} comments-only")
        return
    try:
        idx = PyIndex(old_src)
    except SyntaxError:
        idx = None
    try:
        ast.parse(new_src)
        new_ok = True
    except SyntaxError:
        new_ok = False
    if idx is None or not new_ok:
        # the module will not import: everything that imports it fails
        b = _bits_file(M["il"].get(path, {})) | _bits_file(M["rl"].get(path, {}))
        ctx.bits |= b
        ctx.coarse = True
        ctx.why.append(f"{path} syntax-error n={b.bit_count()}")
        return

    if M.get("dk") is not None:
        try:
            ntree = ast.parse(new_src)
            ctx.dict_changes[path] = dict_entry_changes(idx.tree, ntree)
            if M.get("argtypes") is not None and path.startswith("sqlglot/"):
                ctx.argtypes_changes[path] = argtypes_changes(idx.tree, ntree)
        except SyntaxError:
            pass
    paired = {p[0] for p in pairs}
    old_lines = old_src.split("\n")
    probes = M.get("probe")
    for ln, o, n in pairs:
        if o.strip().startswith("#") and n.strip().startswith("#"):
            continue
        bw = boolop_swap(o, n) if probes is not None and M.get("probe_boolsites") else None
        if bw is not None:
            hit = boolop_site(idx, path, ln, bw[0], bw[1])
            if hit is not None and hit[0] in M["probe_boolsites"]:
                key = f"{hit[0]}#{hit[1]}"
                if key not in M["probe_import"]:
                    b = probes.get(key, 0) | M["probe_failed"]
                    ctx.bits |= b
                    node = idx.deepest(ln, bw[0], bw[0] + 1)
                    stmt = _stmt_of(idx, node) if node is not None else None
                    if stmt is not None:
                        ctx.spans.append((path, _header_span(stmt)))
                    else:
                        ctx.coarse = True
                    ctx.why.append(f"{path}:{ln} weak-mutation BoolOp n={b.bit_count()}")
                    continue
        sw = operator_swap(o, n) if probes is not None else None
        if sw is not None:
            site = probe_site(idx, path, ln, sw[0], sw[1])
            if site is not None and site not in M["probe_import"]:
                b = probes.get(site, 0) | M["probe_failed"]
                ctx.bits |= b
                node = idx.deepest(ln, sw[0], sw[0] + 1)
                stmt = _stmt_of(idx, node) if node is not None else None
                if stmt is not None:
                    ctx.spans.append((path, _header_span(stmt)))
                else:
                    ctx.coarse = True
                ctx.why.append(f"{path}:{ln} weak-mutation {sw[1].__name__} n={b.bit_count()}")
                continue
        c0, c1 = col_span(o, n)
        flip = _is_bool_flip(o, n)
        # localise on the first non-blank char at/after c0 inside the line
        if c1 <= c0:
            c1 = c0 + 1
        c1 = min(c1, len(o))
        c0 = min(c0, max(len(o) - 1, 0))
        analyze_location(M, path, idx, ln, c0, c1, ctx, flip=flip)
    for ln in removed - paired:
        if old_lines[ln - 1].strip() == "" or old_lines[ln - 1].strip().startswith("#"):
            continue
        analyze_location(M, path, idx, ln, None, None, ctx)
    for g in gaps:
        # insertion after old line g: attribute to neighbours
        for ln in (g, g + 1):
            if 1 <= ln <= len(old_lines) and old_lines[ln - 1].strip():
                analyze_location(M, path, idx, ln, None, None, ctx, insertion=True)


def derive_kinds(ctx):
    out = set()
    for w in ctx.why:
        parts = w.split(" ")
        if "data/opened" in w:
            out.add("data")
        elif len(parts) > 1:
            k = parts[1]
            out.add("weak" if k == "weak-mutation" else k)
    return out


class RefIndex:
    """Find every line in sqlglot/ and tests/ that mentions an identifier."""

    def __init__(self, M, repo_read, list_py):
        self.M = M
        self.read = repo_read
        self.list_py = list_py
        self._src = None
        self._cache = {}

    def _sources(self):
        if self._src is None:
            out = []
            for p in self.list_py():
                src = self.read(p) or ""
                nl = [i for i, c in enumerate(src) if c == "\n"] if False else None
                out.append([p, src, nl])
            self._src = out
        return self._src

    def _ast_info(self, ent):
        """(import_lines, header_ranges) for a source file; header_ranges: [(start, end, body_start, body_end)]."""
        if len(ent) > 3:
            return ent[3]
        imports, headers = set(), []
        try:
            tree = ast.parse(ent[1])
            for node in ast.walk(tree):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    imports.update(range(node.lineno, node.end_lineno + 1))
                elif isinstance(node, FUNC) and node.body:
                    start = min([d.lineno for d in node.decorator_list] + [node.lineno])
                    headers.append((start, node.body[0].lineno - 1, node.body[0].lineno, node.end_lineno))
        except SyntaxError:
            pass
        ent.append((imports, headers))
        return ent[3]

    def _flip_sites(self, ent, name):
        """{(line, col): site#j} for reads of `name` that are (possibly negated) operands of probed BoolOps."""
        cache = ent[4] if len(ent) > 4 else None
        if cache is None:
            if len(ent) == 3:
                self._ast_info(ent)
            cache = {}
            ent.append(cache)
        if name in cache:
            return cache[name]
        out = {}
        bsites = self.M.get("probe_boolsites") or ()
        try:
            tree = ast.parse(ent[1])
        except SyntaxError:
            tree = None
        if tree is not None and bsites:
            for node in ast.walk(tree):
                if not isinstance(node, ast.BoolOp):
                    continue
                site = f"{ent[0]}:{node.lineno}:{node.col_offset}:{node.end_lineno}:{node.end_col_offset}"
                if site not in bsites:
                    continue
                for j, v in enumerate(node.values):
                    inner = v.operand if isinstance(v, ast.UnaryOp) and isinstance(v.op, ast.Not) else v
                    if isinstance(inner, ast.Attribute) and inner.attr == name:
                        out[(inner.end_lineno, inner.end_col_offset - len(name))] = f"{site}!{j}"
                    elif isinstance(inner, ast.Name) and inner.id == name:
                        out[(inner.lineno, inner.col_offset)] = f"{site}!{j}"
        cache[name] = out
        return out

    def __call__(self, name, path, stmt, flip=False):
        key = (name, path, stmt.lineno, flip)
        if key in self._cache:
            return self._cache[key]
        pat = re.compile(r"\b" + re.escape(name) + r"\b")
        defpat = re.compile(r"^\s*" + re.escape(name) + r"\s*(:[^=]*)?(=(?!=)|$)")
        rl, il = self.M["rl"], self.M["il"]
        b = 0
        for ent in self._sources():
            p, src, nl = ent[0], ent[1], ent[2]
            if name not in src:
                continue
            if nl is None:
                nl = ent[2] = [m.start() for m in _NL.finditer(src)]
            r = rl.get(p, {})
            i = il.get(p, {})
            for m in pat.finditer(src):
                ln = bisect.bisect_left(nl, m.start()) + 1
                if p == path and stmt.lineno <= ln <= stmt.end_lineno:
                    continue
                # another class defining its own value is not a read
                ls = nl[ln - 2] + 1 if ln > 1 else 0
                le = nl[ln - 1] if ln - 1 < len(nl) else len(src)
                text = src[ls:le]
                if defpat.match(text) and len(pat.findall(text)) == 1:
                    continue
                imports, headers = self._ast_info(ent)
                if ln in imports:
                    continue  # importing a name does not read its value
                if flip:
                    col = m.start() - ls
                    fk = self._flip_sites(ent, name).get((ln, col))
                    if fk is not None and fk not in self.M.get("probe_import", ()):
                        # a boolean flag read as an and/or operand: only tests where negating it mattered
                        b |= self.M["probe"].get(fk, 0) | self.M["probe_failed"]
                        continue
                hdr = next((h for h in headers if h[0] <= ln <= h[1]), None)
                if hdr is not None:
                    # a default argument is read by calls that run the body
                    for bl in range(hdr[2], hdr[3] + 1):
                        b |= r.get(bl, 0)
                    continue
                b |= r.get(ln, 0) | i.get(ln, 0)
        self._cache[key] = b
        return b


_GIT_HDR = re.compile(r"^diff --git a/(\S+) b/(\S+)", re.M)


def unparsed_files(text: str, parsed: set) -> set:
    """Files named in `diff --git` headers that have no text hunks we applied (binary, etc.)."""
    out = set()
    blocks = re.split(r"(?m)^(?=diff --git )", text)
    for b in blocks:
        m = _GIT_HDR.match(b)
        if not m:
            continue
        a, bb = m.group(1), m.group(2)
        if "GIT binary patch" in b or re.search(r"(?m)^Binary files ", b):
            out.update({a, bb})
        elif a not in parsed and bb not in parsed and re.search(r"(?m)^(new|deleted) file mode|^rename (from|to) ", b):
            out.update({a, bb})
    return out


def analyze_patch(M, repo_read, text: str, refs=None) -> Ctx:
    """repo_read(path) -> original text or None."""
    ctx = Ctx(refs)
    parsed = {p for fc in parse_patch(text) for p in (fc.old_path, fc.new_path) if p}
    odd = unparsed_files(text, parsed)
    if odd:
        # content we cannot reconstruct: list every test that reads or imports these, unverified
        ctx.unverifiable = True
        ctx.coarse = True
        for path in odd:
            b = M["opens"].get(path, 0) | _bits_file(M["il"].get(path, {})) | _bits_file(M["rl"].get(path, {}))
            if path.startswith("tests/fixtures/") and not b:
                # a fixture nobody opened directly may be read via a directory walk: be broad
                d = path.rsplit("/", 1)[0]
                for f, bits in M["opens"].items():
                    if f.startswith(d):
                        b |= bits
            ctx.bits |= b
            ctx.why.append(f"{path} unparsed-change n={b.bit_count()}")
    for fc in parse_patch(text):
        path = fc.path
        old_src = repo_read(fc.old_path) if fc.old_path else None
        new_src, removed, gaps, pairs = apply_change(old_src, fc)
        ctx.files = getattr(ctx, "files", {})
        ctx.files[path] = new_src if fc.new_path is not None else None
        if fc.old_path and fc.new_path and fc.old_path != fc.new_path:
            ctx.files[fc.old_path] = None
        if path.endswith(".py") and old_src is not None and fc.new_path is not None:
            analyze_py(M, path, old_src, new_src, removed, gaps, pairs, ctx)
        else:
            # data file (fixtures etc), new or deleted file
            b = M["opens"].get(path, 0)
            if path.endswith(".py"):
                b |= _bits_file(M["il"].get(path, {})) | _bits_file(M["rl"].get(path, {}))
                if old_src is None:
                    ctx.broad = True
            ctx.bits |= b
            ctx.data_files = getattr(ctx, "data_files", []) + [path]
            ctx.why.append(f"{path} data/opened n={b.bit_count()}")
        # tests that read the file as text (e.g. source inspection)
        ctx.bits |= M["opens"].get(path, 0)
    ctx.kinds = derive_kinds(ctx)
    return ctx
