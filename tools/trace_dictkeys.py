"""Per test, which keys of sqlglot's lookup tables (dicts stored as class attributes)
it reads at runtime, and which keys are read while sqlglot is imported.

The parent hooks class creation before importing sqlglot: once a class body has run,
each dict it defines is replaced by a recording subclass holding the same items, so
metaclasses and later class bodies read through it too. Import-time reads are logged
with their source line (IMPORT). The parent then imports every sqlglot module. Each test then runs in its own forked
child. A value read by key (d[k], d.get(k), setdefault, pop) records (name, key);
anything that exposes every value (items, values, copy, |, ==, pickling) records
the name as read whole. Membership tests and key iteration record nothing, since a
changed value cannot change them.

Safety, recorded for the selector:
  aliased : names of dicts that something besides a class/module namespace still
            references after the swap (reads through such a reference would go
            unrecorded)
  outcome : a test whose result differs from a plain run counts as reading everything
Usage: python trace_dictkeys.py REPO TESTS_JSON OUT_DIR WORKER_INDEX WORKER_COUNT
"""
import enum, gc, importlib, json, os, pkgutil, sys, time

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

READ = {}
WHOLE = set()
IMPORTING = [True]
IMPORT = {}  # (name, key label or "*") -> set("path:line")
PL = len(REPO) + 1


def label(k):
    if isinstance(k, str):
        return k
    if isinstance(k, enum.Enum):
        return k.name
    if isinstance(k, type):
        return k.__name__
    if isinstance(k, (int, float, bool)) or k is None:
        return str(k)
    return "repr:" + repr(k)


def _imp(name, lb):
    f = sys._getframe(2)
    while f is not None and f.f_code.co_filename == __file__:
        f = f.f_back
    where = "?"
    if f is not None:
        fn = f.f_code.co_filename
        where = (fn[PL:] if fn.startswith(REPO) else fn) + ":" + str(f.f_lineno)
    IMPORT.setdefault((name, lb), set()).add(where)


class RecDict(dict):
    __slots__ = ("_n",)

    def _r(self, k):
        try:
            lb = label(k)
        except Exception:
            lb = "*"
        if IMPORTING[0]:
            _imp(self._n, lb)
        elif lb == "*":
            WHOLE.add(self._n)
        else:
            READ.setdefault(self._n, set()).add(lb)

    def _w(self):
        if IMPORTING[0]:
            _imp(self._n, "*")
        else:
            WHOLE.add(self._n)

    def __getitem__(self, k):
        self._r(k)
        return dict.__getitem__(self, k)

    def get(self, k, d=None):
        self._r(k)
        return dict.get(self, k, d)

    def setdefault(self, k, d=None):
        self._r(k)
        return dict.setdefault(self, k, d)

    def pop(self, k, *a):
        self._r(k)
        return dict.pop(self, k, *a)

    def popitem(self):
        self._w()
        return dict.popitem(self)

    def __iter__(self):  # keys only; defined so C-level merges take the slow path
        return dict.__iter__(self)

    def keys(self):
        return dict.keys(self)

    def items(self):
        self._w()
        return dict.items(self)

    def values(self):
        self._w()
        return dict.values(self)

    def copy(self):
        self._w()
        return dict.copy(self)

    def __or__(self, o):
        self._w()
        return dict.__or__(self, o)

    def __ror__(self, o):
        self._w()
        return dict.__ror__(self, o)

    def __eq__(self, o):
        self._w()
        return dict.__eq__(self, o)

    def __ne__(self, o):
        self._w()
        return dict.__ne__(self, o)

    __hash__ = None

    def __reduce_ex__(self, proto):
        self._w()
        return (dict, (dict(dict.items(self)),))

    def __repr__(self):
        self._w()
        return dict.__repr__(self)


import builtins, types  # noqa: E402

_orig_build_class = builtins.__build_class__
ORIGINALS = []  # (original dict, name) of every swapped class-body dict
SAME_OBJ = set()  # names whose dict object was bound to two names in one class body


def _build_class(func, name, *bases, metaclass=None, **kwds):
    mod = func.__globals__.get("__name__", "")
    if not (mod == "sqlglot" or mod.startswith("sqlglot.")):
        return _orig_build_class(func, name, *bases, **({"metaclass": metaclass} if metaclass is not None else {}), **kwds)
    orig_bases = bases
    bases = types.resolve_bases(bases)
    if metaclass is None:
        metaclass = type(bases[0]) if bases else type
    if isinstance(metaclass, type):
        metaclass = types._calculate_meta(metaclass, bases)
    ns = metaclass.__prepare__(name, bases, **kwds) if hasattr(metaclass, "__prepare__") else {}
    exec(func.__code__, func.__globals__, ns, closure=func.__closure__)
    seen = {}
    for k, v in list(ns.items()):
        if type(v) is dict and not (k.startswith("__") and k.endswith("__")):
            if id(v) in seen:
                SAME_OBJ.update({k, seen[id(v)]})
            seen[id(v)] = k
            p = RecDict(v)
            p._n = k
            ORIGINALS.append((v, k))
            ns[k] = p
    if bases is not orig_bases:
        ns["__orig_bases__"] = orig_bases
    return metaclass(name, bases, ns, **kwds)


builtins.__build_class__ = _build_class
import sqlglot  # noqa: E402

for m in pkgutil.walk_packages(sqlglot.__path__, "sqlglot."):
    try:
        importlib.import_module(m.name)
    except Exception:
        pass
builtins.__build_class__ = _orig_build_class
IMPORTING[0] = False


def aliases():
    """Names of class-body dicts whose original object is still referenced somewhere."""
    out = set(SAME_OBJ)
    gc.collect()
    frame_t = type(sys._getframe())
    for i, (old, name) in enumerate(ORIGINALS):
        for r in gc.get_referrers(old):
            if isinstance(r, frame_t) or (isinstance(r, tuple) and len(r) == 2 and r[0] is old):
                continue
            out.add(name)
            break
    return out


ALIASED = aliases()
n_dicts = len(ORIGINALS)
ORIGINALS.clear()


def main():
    tests = json.load(open(TESTS_JSON))["tests"]
    mine = [t for t in tests if t["number"] % WN == WI]
    os.makedirs(OUT, exist_ok=True)
    if WI == 0:
        json.dump({"dicts": n_dicts, "aliased": sorted(ALIASED),
                   "import": [[n, k, sorted(v)] for (n, k), v in IMPORT.items()]}, open(os.path.join(OUT, "_meta.json"), "w"))
    for t in mine:
        path = os.path.join(OUT, f"{t['number']}.json")
        if os.path.exists(path):
            continue
        r, w = os.pipe()
        pid = os.fork()
        if pid == 0:
            os.close(r)
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
                if rc not in (0,):
                    outcome[0] = "failed"
            except BaseException:
                outcome[0] = "error"
            data = {"number": t["number"], "read": {k: sorted(v) for k, v in READ.items()}, "whole": sorted(WHOLE),
                    "outcome": outcome[0], "secs": round(time.time() - t0, 3)}
            with open(path + ".tmp", "w") as f:
                json.dump(data, f)
            os.replace(path + ".tmp", path)
            os._exit(0)
        os.close(w)
        os.waitpid(pid, 0)


main()
