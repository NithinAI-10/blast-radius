"""Per test, which scalar class attributes (bool/int/float/str/None set in a class body
of a sqlglot class) it reads, keyed by the class that defines the value read:
"module:Qualname.NAME". Class creation is hooked before sqlglot is imported: each such
attribute goes into the class as a descriptor that records the read and returns the
same value, so lookups that resolve to a subclass's own value never reach it, and reads
by metaclasses, __init_subclass__ and any code run while importing are recorded too
(IMPORT). Enums and modules with dataclasses/NamedTuples are left alone; the set of
instrumented attributes is written out so nothing else is ever narrowed.
Usage: python trace_attrs.py REPO TESTS_JSON OUT_DIR WORKER_INDEX WORKER_COUNT"""
import importlib, json, os, pkgutil, sys, time

REPO, TESTS_JSON, OUT, WI, WN = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4]), int(sys.argv[5])
REPO = os.path.realpath(REPO)
os.environ["SKIP_INTEGRATION"] = "0"
sys.path.insert(0, REPO)
os.chdir(REPO)
import pytest  # noqa: E402

sys.path.insert(0, "/home/claude/entry")
import syncpool  # noqa: E402

syncpool.install()
for mod in ("numpy", "pandas", "duckdb", "unittest", "doctest", "dateutil", "pytz"):
    try:
        __import__(mod)
    except Exception:
        pass
import builtins, enum as _enum, types  # noqa: E402

SEEN = set()
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
        (IMPORT if IMPORTING[0] else SEEN).add(self.k)
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
n = len(KEYS)


def main():
    tests = json.load(open(TESTS_JSON))["tests"]
    mine = [t for t in tests if t["number"] % WN == WI]
    os.makedirs(OUT, exist_ok=True)
    if WI == 0:
        json.dump({"attrs": n, "keys": sorted(KEYS), "import": sorted(IMPORT)}, open(os.path.join(OUT, "_meta.json"), "w"))
    for t in mine:
        path = os.path.join(OUT, f"{t['number']}.json")
        if os.path.exists(path):
            continue
        pid = os.fork()
        if pid == 0:
            outcome = ["passed"]

            class Plug:
                @pytest.hookimpl(hookwrapper=True)
                def pytest_runtest_makereport(self, item, call):
                    o = yield
                    if o.get_result().failed:
                        outcome[0] = "failed"

            t0 = time.time()
            try:
                rc = pytest.main([t["id"], "-q", "-p", "no:cacheprovider", "-p", "no:randomly", "--no-header", "-x"], plugins=[Plug()])
                if rc != 0:
                    outcome[0] = "failed"
            except BaseException:
                outcome[0] = "error"
            data = {"number": t["number"], "read": sorted(SEEN), "outcome": outcome[0], "secs": round(time.time() - t0, 3)}
            with open(path + ".tmp", "w") as f:
                json.dump(data, f)
            os.replace(path + ".tmp", path)
            os._exit(0)
        os.waitpid(pid, 0)


main()
