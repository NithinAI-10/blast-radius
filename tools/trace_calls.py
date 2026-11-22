"""Per-check coverage for tests built on tests/dialects/test_dialect.py's Validator.

Class-level constants are recorded too ("attr:module:Qualname.NAME"), through the same
class-creation hook as tools/trace_attrs.py.

A Validator test is a sequence of independent checks (validate_identity, validate_all,
validate_transpile). For each such test we run every check k in its own forked child,
starting from a state where sqlglot and the test modules are imported but no check has
run, with every other check skipped (a skipped validate_identity/validate_transpile
returns a stand-in that runs the real check if anything touches it). We record the
sqlglot functions (code objects, keyed "path:first line") started inside check k: that
is everything k needs computed from scratch, so caches filled by earlier checks are
covered. We also record functions started outside any check (setUp, the test body) and
whether skipping changed the test's outcome (then the test is never restricted).

Usage: python trace_calls.py REPO TESTS_JSON OUT_DIR WORKER_INDEX WORKER_COUNT"""
import importlib, json, os, pkgutil, sys, time, unittest

REPO, TESTS_JSON, OUT, WI, WN = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4]), int(sys.argv[5])
REPO = os.path.realpath(REPO)
os.environ["SKIP_INTEGRATION"] = "0"
sys.path.insert(0, REPO)
os.chdir(REPO)
sys.path.insert(0, "/home/claude/entry")
import syncpool  # noqa: E402

syncpool.install()
for mod in ("numpy", "pandas", "duckdb", "pandas.testing"):
    try:
        __import__(mod)
    except Exception:
        pass
import builtins, enum as _enum, types  # noqa: E402

CUR = [None]
IMPORT = set()
KEYS = set()
IMPORTING = [True]
SKIP_MODULES = {"sqlglot.diff", "sqlglot._typing", "sqlglot.lineage", "sqlglot.optimizer.pushdown_projections"}


class RecAttr:
    __slots__ = ("v", "k")

    def __init__(self, v, k):
        self.v = v
        self.k = k

    def __get__(self, obj, owner=None):
        if IMPORTING[0]:
            IMPORT.add(self.k)
        elif CUR[0] is not None:
            seen, state = CUR[0]
            (seen["in"] if state.depth else seen["out"]).add("attr:" + self.k)
        return self.v


SCALARS = (bool, int, float, str, type(None))
_orig_build_class = builtins.__build_class__


def _build_class(func, name, *bases, metaclass=None, **kwds):
    mod = func.__globals__.get("__name__", "")
    kw = dict(kwds)
    if metaclass is not None:
        kw["metaclass"] = metaclass
    if not (mod == "sqlglot" or mod.startswith("sqlglot.")) or mod in SKIP_MODULES:
        return _orig_build_class(func, name, *bases, **kw)
    orig_bases = bases
    rb = types.resolve_bases(bases)
    meta = metaclass if metaclass is not None else (type(rb[0]) if rb else type)
    if isinstance(meta, type):
        meta = types._calculate_meta(meta, rb)
    if isinstance(meta, _enum.EnumMeta) or any(isinstance(b, type) and issubclass(b, _enum.Enum) for b in rb):
        return _orig_build_class(func, name, *bases, **kw)
    ns = meta.__prepare__(name, rb, **kwds) if hasattr(meta, "__prepare__") else {}
    exec(func.__code__, func.__globals__, ns, closure=func.__closure__)
    qual = ns.get("__qualname__", name)
    for k, v in list(ns.items()):
        if not k.startswith("__") and type(v) in SCALARS:
            key = f"{mod}:{qual}.{k}"
            ns[k] = RecAttr(v, key)
            KEYS.add(key)
    if rb is not orig_bases:
        ns["__orig_bases__"] = orig_bases
    return meta(name, rb, ns, **kwds)


builtins.__build_class__ = _build_class
import sqlglot  # noqa: E402

for m in pkgutil.walk_packages(sqlglot.__path__, "sqlglot."):
    try:
        importlib.import_module(m.name)
    except Exception:
        pass
builtins.__build_class__ = _orig_build_class
IMPORTING[0] = False
from tests.dialects.test_dialect import Validator  # noqa: E402

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "selector"))
import callskip  # noqa: E402

PREF = os.path.join(REPO, "sqlglot") + os.sep
PL = len(REPO) + 1
mon = sys.monitoring
TOOL = mon.PROFILER_ID


def run_one(cls, meth, keep):
    """Run test cls.meth keeping only check indices in keep (None = all). Returns
    (passed, n_checks, {"in": set, "out": set})."""
    seen = {"in": set(), "out": set()}
    state = callskip.State(keep)

    def on_start(code, offset):
        fn = code.co_filename
        if fn.startswith(PREF):
            (seen["in"] if state.depth else seen["out"]).add(f"{fn[PL:]}:{code.co_firstlineno}")
        return None

    mon.use_tool_id(TOOL, "calls")
    mon.register_callback(TOOL, mon.events.PY_START, on_start)
    callskip.install(cls, state)
    CUR[0] = (seen, state)
    mon.set_events(TOOL, mon.events.PY_START)
    res = unittest.TestResult()
    try:
        unittest.TestSuite([cls(meth)]).run(res)
    finally:
        mon.set_events(TOOL, 0)
    ok = res.wasSuccessful() and not res.skipped
    return ok, state.count, seen


def in_child(fn):
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(r)
        try:
            out = fn()
        except BaseException as e:  # noqa: BLE001
            out = {"error": repr(e)}
        os.write(w, json.dumps(out).encode())
        os._exit(0)
    os.close(w)
    buf = b""
    while True:
        c = os.read(r, 1 << 20)
        if not c:
            break
        buf += c
    os.close(r)
    os.waitpid(pid, 0)
    return json.loads(buf) if buf else {"error": "no output"}


def main():
    tests = json.load(open(TESTS_JSON))["tests"]
    mine = [t for t in tests if t["number"] % WN == WI]
    os.makedirs(OUT, exist_ok=True)
    for t in mine:
        path, clsname, meth = t["id"].split("::")
        out_path = os.path.join(OUT, f"{t['number']}.json")
        if os.path.exists(out_path):
            continue
        try:
            mod = importlib.import_module(path[:-3].replace("/", "."))
            cls = getattr(mod, clsname)
        except Exception:
            continue
        if not (isinstance(cls, type) and issubclass(cls, Validator)):
            continue
        t0 = time.time()

        def full():
            ok, n, seen = run_one(cls, meth, None)
            return {"ok": ok, "n": n, "out": sorted(seen["out"])}

        base = in_child(full)
        data = {"number": t["number"], "n": base.get("n", 0), "ok": bool(base.get("ok")), "outside": base.get("out", []), "calls": {}}
        if data["ok"] and 0 < data["n"] <= 4000:
            masks = {}
            outside = set(data["outside"])
            for k in range(data["n"]):
                def one(k=k):
                    ok, n, seen = run_one(cls, meth, {k})
                    return {"ok": ok, "n": n, "in": sorted(seen["in"]), "out": sorted(seen["out"])}

                r = in_child(one)
                if not r.get("ok") or r.get("n") != data["n"]:
                    data["ok"] = False  # skipping checks changes this test: never restrict it
                    break
                for key in r["in"]:
                    masks[key] = masks.get(key, 0) | (1 << k)
                outside.update(r["out"])
            data["calls"] = {k: v for k, v in masks.items()}
            data["outside"] = sorted(outside)
        data["secs"] = round(time.time() - t0, 2)
        with open(out_path + ".tmp", "w") as f:
            json.dump(data, f)
        os.replace(out_path + ".tmp", out_path)


main()
