"""An in-process stand-in for ProcessPoolExecutor.

sqlglot's optimizer and executor tests fan work out to a spawn-based process
pool. Spawning costs a fresh interpreter per worker (and hides the work from
tracing), so we run each call in-process. Arguments and results still go
through pickle, so anything a mutant breaks in pickling still fails.
"""

import concurrent.futures as cf
import pickle


class SyncProcessPoolExecutor(cf.Executor):
    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **kw):
        self._init = (initializer, initargs)
        self._ready = False

    def submit(self, fn, /, *args, **kwargs):
        f = cf.Future()
        try:
            if not self._ready:
                self._ready = True
                init, iargs = self._init
                if init is not None:
                    init(*iargs)  # test-owned data; no sqlglot objects
            fn2, args2, kwargs2 = pickle.loads(pickle.dumps((fn, args, kwargs)))
            res = fn2(*args2, **kwargs2)
            f.set_result(pickle.loads(pickle.dumps(res)))
        except BaseException as e:  # noqa: BLE001
            f.set_exception(e)
        return f

    def shutdown(self, wait=True, *, cancel_futures=False):
        pass


def install():
    cf.ProcessPoolExecutor = SyncProcessPoolExecutor
    try:
        import concurrent.futures.process as p

        p.ProcessPoolExecutor = SyncProcessPoolExecutor
    except Exception:
        pass
