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
