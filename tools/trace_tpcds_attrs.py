"""Which class-level constants, lookup-table entries (dict:NAME:key, dict:NAME:* for whole
reads) and missing-argument validations (req:Class.arg) test_execute_tpcds touches in
each phase: P = parse_one in
the test, O = optimize in the executor, T = transpile(..., write="duckdb"), R = the rest
(class setup, planning, Python execution, comparison). Same class-creation hook as
tools/trace_attrs.py; import-time reads are not recorded (those keys are never narrowed).
Usage: python trace_tpcds_attrs.py REPO OUT_JSON"""
import importlib, json, os, pkgutil, sys
REPO, OUT = os.path.realpath(sys.argv[1]), sys.argv[2]
os.environ["SKIP_INTEGRATION"] = "0"
sys.path.insert(0, REPO); os.chdir(REPO)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "selector"))
for m in ("numpy", "pandas", "duckdb", "pandas.testing"):
    __import__(m)
import syncpool  # noqa: E402
syncpool.install()
import builtins, enum as _enum, types  # noqa: E402

PHASE = ["R"]
PH = {"R": set(), "P": set(), "O": set(), "T": set()}
IMPORT = set()
KEYS = set()
IMPORTING = [True]
SKIP_MODULES = {"sqlglot.diff", "sqlglot._typing", "sqlglot.lineage", "sqlglot.optimizer.pushdown_projections"}


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


class RecDict(dict):
    __slots__ = ("_n", "_c")

    def _r(self, k):
        try:
            lb = label(k)
        except Exception:
            lb = "*"
        if not IMPORTING[0]:
            PH[PHASE[0]].add(f"dict:{self._n}:{lb}")
            PH[PHASE[0]].add(f"dictc:{getattr(self, '_c', '?')}.{self._n}:{lb}")

    def _w(self):
        if not IMPORTING[0]:
            PH[PHASE[0]].add(f"dict:{self._n}:*")
            PH[PHASE[0]].add(f"dictc:{getattr(self, '_c', '?')}.{self._n}:*")

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


class RecAttr:
    __slots__ = ("v", "k")

    def __init__(self, v, k):
        self.v = v
        self.k = k

    def __get__(self, obj, owner=None):
        if not IMPORTING[0]:
            PH[PHASE[0]].add(self.k)
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
        if not k.startswith("__") and type(v) is dict:
            p = RecDict(v)
            p._n = k
            p._c = qual.rsplit(".", 1)[-1]  # defining class name
            ns[k] = p
            continue
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
import unittest  # noqa: E402
from sqlglot.expressions.core import Expression  # noqa: E402

_em = Expression.error_messages


def error_messages(self, args=None):
    if not IMPORTING[0]:
        for k in type(self).arg_types:
            v = self.args.get(k)
            if v is None or (isinstance(v, list) and not v):
                PH[PHASE[0]].add(f"req:{type(self).__name__}.{k}")
    return _em(self, args)


Expression.error_messages = error_messages
import sqlglot.executor as ex  # noqa: E402
import tests.test_executor as te  # noqa: E402


def wrap(ph, fn):
    def w(*a, **kw):
        prev = PHASE[0]
        PHASE[0] = ph
        try:
            return fn(*a, **kw)
        finally:
            PHASE[0] = prev
    return w


ex.optimize = wrap("O", ex.optimize)
te.parse_one = wrap("P", te.parse_one)
te.transpile = wrap("T", te.transpile)
res = unittest.TestResult()
unittest.TestSuite([te.TestExecutor("test_execute_tpcds")]).run(res)
print("ok", res.wasSuccessful(), {k: len(v) for k, v in PH.items()})
json.dump({"ok": res.wasSuccessful(), **{k: sorted(v) for k, v in PH.items()}}, open(OUT, "w"))
