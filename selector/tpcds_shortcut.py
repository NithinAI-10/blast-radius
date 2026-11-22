"""Skip executing TPC-DS queries whose optimized plan input is unchanged.

tests/test_executor.py::TestExecutor::test_execute_tpcds parses each query,
optimizes it inside sqlglot.executor.execute, then plans and runs it in pure
Python (~30 s total, ~3 s of which is parse+optimize). When a patch only touches
code that runs inside parse/optimize for this test (checked against a trace of
the lines this test runs outside those calls), a query whose parsed input and
optimized output are structurally identical to the pinned commit's will execute
identically, so its execution and comparison are skipped.
"""

import concurrent.futures as cf
import pickle

from canon import canon

FAILED = False  # set by the runner when any subtest of the running test fails


class _Abort(Exception):
    """A query already failed: the test fails; stop executing the rest."""


class _LazyFuture(cf.Future):
    def __init__(self, pool, fn, args, kwargs):
        super().__init__()
        self._job = (pool, fn, args, kwargs)

    def _run(self):
        if self._job is None:
            return
        pool, fn, args, kwargs = self._job
        self._job = None
        try:
            if FAILED:
                raise _Abort()
            pool._init_once()
            fn2, a2, k2 = pickle.loads(pickle.dumps((fn, args, kwargs)))
            self.set_result(pickle.loads(pickle.dumps(fn2(*a2, **k2))))
        except BaseException as e:  # noqa: BLE001
            self.set_exception(e)

    def result(self, timeout=None):
        self._run()
        return super().result(timeout)

    def exception(self, timeout=None):
        self._run()
        return super().exception(timeout)


class LazyPool(cf.Executor):
    """Runs each call when its result is asked for, so a failed comparison stops the rest."""

    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **kw):
        self._init = (initializer, initargs)
        self._ready = False

    def _init_once(self):
        if not self._ready:
            self._ready = True
            init, iargs = self._init
            if init is not None:
                init(*iargs)

    def submit(self, fn, /, *args, **kwargs):
        return _LazyFuture(self, fn, args, kwargs)

    def shutdown(self, wait=True, *, cancel_futures=False):
        pass


class _Same(Exception):
    pass


class TooManyChanged(Exception):
    """Many plans changed: the test is likely broken and too costly to verify; list it."""


def install(table, max_changed=10):
    """table: {canon(parsed input): canon(optimized output)} from the pinned commit."""
    import sqlglot.executor as ex
    import tests.test_executor as te

    real_opt = ex.optimize
    changed = [0]

    def optimize(sql, schema=None, *a, **kw):
        ih = None if isinstance(sql, str) else canon(sql)
        out = real_opt(sql, schema, *a, **kw)
        if ih is not None and table.get(ih) == canon(out):
            raise _Same()
        changed[0] += 1
        if changed[0] > max_changed:
            raise TooManyChanged()
        return out

    ex.optimize = optimize
    real_mp = te.mp_execute

    def mp_execute(expression, meta):
        try:
            return real_mp(expression, meta)
        except _Same:
            return None

    mp_execute.__module__ = te.__name__
    mp_execute.__qualname__ = "mp_execute"
    te.mp_execute = mp_execute
    te.ProcessPoolExecutor = LazyPool


class _LazyTables(dict):
    """Stands in for TestExecutor.tpcds_tables until a query actually needs the data."""

    def __init__(self, load):
        super().__init__()
        self._load = load

    def _real(self):
        return self._load()

    def __getitem__(self, k):
        return self._real()[k]

    def get(self, k, d=None):
        return self._real().get(k, d)

    def __contains__(self, k):
        return k in self._real()

    def __iter__(self):
        return iter(self._real())

    def __len__(self):
        return len(self._real())

    def keys(self):
        return self._real().keys()

    def values(self):
        return self._real().values()

    def items(self):
        return self._real().items()


class _LazyConn:
    def __init__(self, load, attr):
        self._load = load
        self._attr = attr
        self._loaded = False

    def __getattr__(self, name):
        self._loaded = True
        return getattr(self._load(self._attr), name)

    def close(self):
        if self._loaded:
            self._load(self._attr).close()


def _lazy_setup(te):
    """Load the TPC-H/TPC-DS tables only when a query has to execute. Used only when
    test_execute_tpcds is the sole TestExecutor test in this process."""
    cls = te.TestExecutor
    real_setup = cls.setUpClass.__func__
    state = {}

    def load(attr=None):
        if not state:
            lazy = {k: getattr(cls, k) for k in ("tpch_tables", "tpcds_tables", "tpch_conn", "tpcds_conn")}
            real_setup(cls)
            state.update({k: getattr(cls, k) for k in lazy})
        return state[attr] if attr else state

    def setUpClass(klass):
        klass.tpch_tables = _LazyTables(lambda: load("tpch_tables"))
        klass.tpcds_tables = _LazyTables(lambda: load("tpcds_tables"))
        klass.tpch_conn = _LazyConn(load, "tpch_conn")
        klass.tpcds_conn = _LazyConn(load, "tpcds_conn")
        klass.cache = {}
        klass.tpch_sqls = list(te.load_sql_fixture_pairs("optimizer/tpc-h/tpc-h.sql"))
        klass.tpcds_sqls = list(te.load_sql_fixture_pairs("optimizer/tpc-ds/tpc-ds.sql"))

    def tearDownClass(klass):
        if state:
            state["tpch_conn"].close()
            state["tpcds_conn"].close()

    cls.setUpClass = classmethod(setUpClass)
    cls.tearDownClass = classmethod(tearDownClass)


def install2(table, need_opt=True, need_tr=True, lazy=False, max_changed=10):
    """Wider shortcut. table: {canon(parsed input): (canon(optimized), duckdb SQL)} from the
    pinned commit. A query is skipped only if its parsed input, its optimized plan and the
    duckdb SQL the test compares against are identical to the pinned commit's. The selector
    uses this only when no changed line (or read of a changed attribute/constant) runs for
    this test outside parse_one, optimize and transpile. need_opt / need_tr are False when no
    changed line runs inside optimize / transpile for this test: identical inputs then give
    identical outputs, so those calls are not repeated."""
    import sqlglot.executor as ex
    import tests.test_executor as te

    real_opt = ex.optimize
    real_parse = te.parse_one
    real_tr = te.transpile
    duck_ok = {}
    changed = [0]

    def parse_one(sql, *a, **kw):
        e = real_parse(sql, *a, **kw)
        try:
            k = canon(pickle.loads(pickle.dumps(e)))
            ent = table.get(k)
            if ent is not None and isinstance(sql, str) and not a and not kw:
                duck_ok[k] = (not need_tr) or real_tr(sql, write="duckdb")[0] == ent[1]
        except Exception:  # noqa: BLE001  (the query then simply runs)
            pass
        return e

    def optimize(sql, schema=None, *a, **kw):
        ih = None if isinstance(sql, str) else canon(sql)
        if ih is not None and duck_ok.get(ih) and not need_opt and ih in table:
            raise _Same()
        out = real_opt(sql, schema, *a, **kw)
        if ih is not None and duck_ok.get(ih):
            ent = table.get(ih)
            if ent is not None and ent[0] == canon(out):
                raise _Same()
        changed[0] += 1
        if changed[0] > max_changed:
            raise TooManyChanged()
        return out

    if lazy:
        _lazy_setup(te)
    te.parse_one = parse_one
    ex.optimize = optimize
    real_mp = te.mp_execute

    def mp_execute(expression, meta):
        try:
            return real_mp(expression, meta)
        except _Same:
            return None

    mp_execute.__module__ = te.__name__
    mp_execute.__qualname__ = "mp_execute"
    te.mp_execute = mp_execute
    te.ProcessPoolExecutor = LazyPool
