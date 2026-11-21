"""Weak-mutation probes: for every single-operator comparison site in sqlglot
(is, is not, <, <=, >, >=), record per test whether the mutated operator would
ever have produced a different value than the original during that test.

  is      -> ==    differs when (a is b) != bool(a == b)  (or == raises)
  is not  -> !=    differs when (a is not b) != bool(a != b)
  < <-> <=, > <-> >=  differ exactly when a == b at some evaluation

If a site never differs in a test, the mutant behaves identically there.

Usage: python trace_probes.py REPO TESTS_JSON OUT_JSON WORKER_INDEX WORKER_COUNT
"""

import ast
import builtins
import importlib.abc
import importlib.machinery
import importlib.util
import json
import os
import pkgutil
import sys

REPO, TESTS_JSON, OUT, WI, WN = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4]), int(sys.argv[5])
REPO = os.path.realpath(REPO)
os.environ["SKIP_INTEGRATION"] = "0"
sys.path.insert(0, REPO)
sys.path.insert(0, "/home/claude/entry")
os.chdir(REPO)

import syncpool  # noqa: E402

syncpool.install()
import unittest  # noqa: E402

for m in ("numpy", "pandas", "duckdb", "pandas.testing"):
    __import__(m)

PKG = os.path.join(REPO, "sqlglot") + os.sep
OPS = {ast.Is: "is", ast.IsNot: "isnot", ast.Lt: "lt", ast.LtE: "le", ast.Gt: "gt", ast.GtE: "ge"}

DIFF = set()


def _probe(site, a, b, op):
    if op == "is":
        r = a is b
    elif op == "isnot":
        r = a is not b
    elif op == "lt":
        r = a < b
    elif op == "le":
        r = a <= b
    elif op == "gt":
        r = a > b
    else:
        r = a >= b
    if site not in DIFF:
        try:
            if op == "is":
                d = r != bool(a == b)
            elif op == "isnot":
                d = r != bool(a != b)
            else:
                d = bool(a == b)
        except Exception:
            d = True
        if d:
            DIFF.add(site)
    return r


builtins.__br_probe__ = _probe

_UNSET = object()


def _boolop(site, op, ctx, *thunks):
    n = len(thunks)
    cache = [_UNSET] * n
    errs = [None] * n

    def val(i):
        if cache[i] is _UNSET and errs[i] is None:
            try:
                cache[i] = thunks[i]()
            except BaseException as e:  # noqa: BLE001
                errs[i] = e
        if errs[i] is not None:
            raise errs[i]
        return cache[i]

    # original semantics, short-circuit, in order
    if op == "and":
        for i in range(n):
            r = val(i)
            if not r:
                break
    else:
        for i in range(n):
            r = val(i)
            if r:
                break
    # operand negation: what if operand j's truth value were flipped (a boolean flag flip)?
    for j in range(n):
        key = f"{site}!{j}"
        if key in DIFF:
            continue
        try:
            vj = val(j)
            if not isinstance(vj, bool):
                raise TypeError  # only model flips of real booleans
            m = None
            for i in range(n):
                m = (not vj) if i == j else val(i)
                if (op == "and" and not m) or (op == "or" and m):
                    break
            d = (bool(m) != bool(r)) if ctx == "b" else (m is not r)
        except BaseException:  # noqa: BLE001
            d = True
        if d:
            DIFF.add(key)
    other = "or" if op == "and" else "and"
    for pos in range(1, n):
        key = f"{site}#{pos}"
        if key in DIFF:
            continue
        ops = [op] * (n - 1)
        ops[pos - 1] = other
        try:
            # precedence: and binds tighter than or
            groups, cur = [], [0]
            for j, o in enumerate(ops, start=1):
                if o == "and":
                    cur.append(j)
                else:
                    groups.append(cur)
                    cur = [j]
            groups.append(cur)
            m = None
            for g in groups:
                for j in g:
                    m = val(j)
                    if not m:
                        break
                if m:
                    break
            d = (bool(m) != bool(r)) if ctx == "b" else (m is not r)
        except BaseException:  # noqa: BLE001
            d = True
        if d:
            DIFF.add(key)
    return r


builtins.__br_boolop__ = _boolop
PURE_CALLS = {"isinstance", "issubclass", "len", "id", "type", "bool", "hasattr", "callable", "getattr", "__br_probe__", "__br_boolop__"}
IMPURE_ATTRS = {"all_columns", "columns", "dag", "external_columns", "local_columns", "meta", "pivots", "references", "select_aliases", "selected_sources", "supported_table_args", "table"}
SITES = set()


def pure(node):
    if isinstance(node, (ast.Name, ast.Constant)):
        return True
    if isinstance(node, ast.Attribute):
        return node.attr not in IMPURE_ATTRS and pure(node.value)
    if isinstance(node, ast.Subscript):
        return pure(node.value) and pure(node.slice)
    if isinstance(node, ast.Compare):
        return pure(node.left) and all(pure(c) for c in node.comparators)
    if isinstance(node, ast.UnaryOp):
        return pure(node.operand)
    if isinstance(node, ast.BinOp):
        return pure(node.left) and pure(node.right)
    if isinstance(node, ast.BoolOp):
        return all(pure(v) for v in node.values)
    if isinstance(node, ast.IfExp):
        return pure(node.test) and pure(node.body) and pure(node.orelse)
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return all(pure(e) for e in node.elts)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "__br_boolop__":
        return True  # an already-instrumented pure BoolOp
    if isinstance(node, ast.Call):
        return (
            isinstance(node.func, ast.Name)
            and node.func.id in PURE_CALLS
            and not node.keywords
            and all(pure(a) and not isinstance(a, ast.Starred) for a in node.args)
        )
    return False


class T(ast.NodeTransformer):
    def __init__(self, rel):
        self.rel = rel
        self.fn_depth = 0
        self.bool_ctx = set()  # id() of BoolOp nodes in a truthiness-only context

    def _func(self, node):
        self.fn_depth += 1
        self.generic_visit(node)
        self.fn_depth -= 1
        return node

    visit_FunctionDef = _func
    visit_AsyncFunctionDef = _func
    visit_Lambda = _func

    def _mark_bool(self, expr):
        if isinstance(expr, ast.BoolOp):
            self.bool_ctx.add(id(expr))

    def visit_If(self, node):
        self._mark_bool(node.test)
        return self.generic_visit(node)

    visit_While = visit_If

    def visit_IfExp(self, node):
        self._mark_bool(node.test)
        return self.generic_visit(node)

    def visit_Assert(self, node):
        self._mark_bool(node.test)
        return self.generic_visit(node)

    def visit_UnaryOp(self, node):
        if isinstance(node.op, ast.Not):
            self._mark_bool(node.operand)
        return self.generic_visit(node)

    def visit_comprehension(self, node):
        for c in node.ifs:
            self._mark_bool(c)
        return self.generic_visit(node)

    def visit_BoolOp(self, node):
        in_bool = id(node) in self.bool_ctx
        if in_bool:
            for v in node.values:
                self._mark_bool(v)
        self.generic_visit(node)
        if self.fn_depth == 0 or not all(pure(v) for v in node.values):
            return node
        site = f"{self.rel}:{node.lineno}:{node.col_offset}:{node.end_lineno}:{node.end_col_offset}"
        SITES.add(site)
        op = "and" if isinstance(node.op, ast.And) else "or"
        args = [ast.Constant(site), ast.Constant(op), ast.Constant("b" if in_bool else "v")]
        for v in node.values:
            args.append(ast.Lambda(args=ast.arguments(posonlyargs=[], args=[], kwonlyargs=[], kw_defaults=[], defaults=[]), body=v))
        call = ast.Call(func=ast.Name(id="__br_boolop__", ctx=ast.Load()), args=args, keywords=[])
        return ast.copy_location(call, node)

    def visit_Compare(self, node):
        self.generic_visit(node)
        if len(node.ops) != 1 or type(node.ops[0]) not in OPS:
            return node
        site = f"{self.rel}:{node.lineno}:{node.col_offset}:{node.end_lineno}:{node.end_col_offset}"
        call = ast.Call(
            func=ast.Name(id="__br_probe__", ctx=ast.Load()),
            args=[ast.Constant(site), node.left, node.comparators[0], ast.Constant(OPS[type(node.ops[0])])],
            keywords=[],
        )
        return ast.copy_location(call, node)


CACHE = "/tmp/probe_code_cache"
os.makedirs(CACHE, exist_ok=True)


class Loader(importlib.machinery.SourceFileLoader):
    def source_to_code(self, data, path, *, _optimize=-1):
        rel = os.path.relpath(path, REPO)
        tree = ast.parse(data, filename=path)
        t = T(rel)
        tree = ast.fix_missing_locations(t.visit(tree))
        return compile(tree, path, "exec", dont_inherit=True)

    def get_code(self, fullname):
        import hashlib
        import marshal

        path = self.get_filename(fullname)
        with open(path, "rb") as f:
            data = f.read()
        key = os.path.join(CACHE, hashlib.sha1(path.encode() + data).hexdigest())
        try:
            with open(key, "rb") as f:
                return marshal.load(f)
        except Exception:
            pass
        code = self.source_to_code(data, path)
        tmp = f"{key}.{os.getpid()}"
        with open(tmp, "wb") as f:
            marshal.dump(code, f)
        os.replace(tmp, key)
        return code


class Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if not (fullname == "sqlglot" or fullname.startswith("sqlglot.")):
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if spec is None or not spec.origin or not spec.origin.startswith(PKG) or not spec.origin.endswith(".py"):
            return spec
        spec.loader = Loader(fullname, spec.origin)
        return spec


# the universe of instrumented BoolOp sites, from a transform-only pass over every file
for dp, dn, fn in os.walk(PKG):
    dn[:] = [d for d in dn if d != "__pycache__"]
    for f in fn:
        if f.endswith(".py"):
            full = os.path.join(dp, f)
            T(os.path.relpath(full, REPO)).visit(ast.parse(open(full, "rb").read(), filename=full))

sys.meta_path.insert(0, Finder())
import sqlglot  # noqa: E402  (what every test process imports first; dialects stay lazy)

IMPORT_DIFF = sorted(DIFF)
DIFF.clear()


def main():
    tests = json.load(open(TESTS_JSON))["tests"]
    out = json.load(open(OUT)) if os.path.exists(OUT) else {}
    for t in tests:
        if t["number"] % WN != WI or str(t["number"]) in out:
            continue
        r, w = os.pipe()
        pid = os.fork()
        if pid == 0:
            os.close(r)
            dn = os.open(os.devnull, os.O_WRONLY)
            os.dup2(dn, 1)
            os.dup2(dn, 2)
            path, cls, meth = t["id"].split("::")
            ok = False
            try:
                mod = __import__(path[:-3].replace("/", "."), fromlist=["_"])
                res = unittest.TestResult()
                unittest.TestSuite([getattr(mod, cls)(meth)]).run(res)
                ok = res.wasSuccessful()
            except BaseException:
                pass
            os.write(w, json.dumps(sorted(DIFF) + ([] if ok else ["!failed"])).encode())
            os._exit(0)
        os.close(w)
        data = b""
        while True:
            c = os.read(r, 1 << 16)
            if not c:
                break
            data += c
        os.close(r)
        os.waitpid(pid, 0)
        out[str(t["number"])] = json.loads(data or b"[]")
        if len(out) % 50 == 0:
            json.dump(out, open(OUT, "w"))
            print(len(out), flush=True)
    out["_import"] = IMPORT_DIFF
    out["_boolsites"] = sorted(SITES)
    json.dump(out, open(OUT, "w"))
    print("done", len(out), flush=True)


if __name__ == "__main__":
    main()
