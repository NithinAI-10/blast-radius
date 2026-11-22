"""Run a set of sqlglot unittest tests against a patched tree, in a child process.

The child streams one line per finished test to a pipe: "<number> P" or "<number> F".
The parent kills the child at its deadline; tests without a line are unknown.
"""

from __future__ import annotations

import os
import signal
import sys
import time

_SKIP = {"__pycache__"}


def build_overlay(base: str, root: str, files: dict[str, str | None]) -> None:
    """Make `root` look like `base` with `files` replaced (None = deleted).

    Only directories on the path to a changed file are real; everything
    else is a symlink into the read-only base tree, so pyc caches still hit.
    """
    os.makedirs(root)  # must be fresh; callers pick a unique path
    real_dirs = {""}
    for rel in files:
        parts = rel.split("/")[:-1]
        for i in range(1, len(parts) + 1):
            real_dirs.add("/".join(parts[:i]))

    def fill(rel_dir: str):
        src_dir = os.path.join(base, rel_dir) if rel_dir else base
        dst_dir = os.path.join(root, rel_dir) if rel_dir else root
        os.makedirs(dst_dir, exist_ok=True)
        try:
            names = os.listdir(src_dir)
        except FileNotFoundError:
            names = []
        for name in names:
            rel = f"{rel_dir}/{name}" if rel_dir else name
            if rel in files:
                continue
            if rel in real_dirs:
                fill(rel)
            else:
                os.symlink(os.path.join(src_dir, name), os.path.join(dst_dir, name))

    fill("")
    for rel, text in files.items():
        if text is None:
            continue
        p = os.path.join(root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8", newline="") as f:
            f.write(text)


def child_main(root: str, groups: list[tuple[str, str, list[tuple[int, str]]]], wfd: int, per_test_timeout, restrict=None, shortcut=None, calls=None) -> None:
    """groups: [(module_name, class_name, [(number, method), ...]), ...]"""
    import unittest

    import surrogates
    import syncpool

    syncpool.install()
    fp = None
    devnull = os.open(os.environ.get("BR_CHILD_LOG", os.devnull), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    os.dup2(devnull, 1)
    os.dup2(devnull, 2)
    sys.dont_write_bytecode = True
    os.environ["SKIP_INTEGRATION"] = "0"
    os.chdir(root)
    sys.path.insert(0, root)
    out = os.fdopen(wfd, "w", buffering=1)
    if restrict:
        import fixtures_patch as fp

        fp.install(restrict)
    if shortcut:
        try:
            import tpcds_shortcut

            if isinstance(shortcut, tuple) and shortcut[0] == "v2":
                tpcds_shortcut.install2(*shortcut[1:])
            else:
                tpcds_shortcut.install(shortcut)
        except BaseException:
            pass  # without the shortcut the test simply runs in full

    class Timeout(BaseException):
        pass

    def on_alarm(signum, frame):
        raise Timeout()

    signal.signal(signal.SIGPROF, on_alarm)

    class Result(unittest.TestResult):
        """Streams one status line per test; class-level errors fail the rest."""

        def __init__(self, nums):
            super().__init__()
            self.nums = nums  # id(test) -> number
            self.cur = None
            self.bad = False
            self.done = set()
            self.class_error = False

        def startTest(self, test):
            super().startTest(test)
            if CALLSTATE is not None:
                keep = (calls or {}).get(self.nums.get(id(test)))
                CALLSTATE.keep = set(keep) if keep is not None else None
                CALLSTATE.count = 0
            self.cur = test
            self.bad = False
            signal.setitimer(signal.ITIMER_PROF, per_test_timeout(self.nums[id(test)]))

        def stopTest(self, test):
            signal.setitimer(signal.ITIMER_PROF, 0)
            n = self.nums.get(id(test))
            if n is not None and n not in self.done:
                self.done.add(n)
                out.write(f"{n} {'F' if self.bad else 'P'}\n")
            self.cur = None
            super().stopTest(test)

        def _mark(self, test):
            if id(test) in self.nums or (self.cur is not None and getattr(test, "test_case", None) is self.cur):
                self.bad = True
            elif self.cur is not None and test is self.cur:
                self.bad = True
            else:
                # setUpClass / tearDownClass / module fixtures
                self.class_error = True

        def addError(self, test, err):
            if os.environ.get("BR_CHILD_LOG"):
                import traceback; traceback.print_exception(*err)
            self._mark(test)

        def addFailure(self, test, err):
            if os.environ.get("BR_CHILD_LOG"):
                import traceback; traceback.print_exception(*err, limit=-3)
            self._mark(test)

        def addSubTest(self, test, subtest, err):
            if err is not None:
                if os.environ.get("BR_CHILD_LOG"):
                    import traceback; print("SUBTEST", subtest, file=sys.stderr); traceback.print_exception(*err, limit=-3)
                self.bad = True
                sc = sys.modules.get("tpcds_shortcut")
                if sc is not None:
                    sc.FAILED = True

        def addUnexpectedSuccess(self, test):
            self.bad = True

    CALLSTATE = None
    for modname, clsname, items in groups:
        try:
            mod = __import__(modname, fromlist=["_"])
            if fp is not None:
                fp.patch_module(mod)
            cls = getattr(mod, clsname)
            if calls and any(num in calls for num, _ in items):
                import callskip

                if CALLSTATE is None:
                    CALLSTATE = callskip.State(None)
                callskip.install(cls, CALLSTATE)
            for _num, meth in items:
                surrogates.apply(mod, cls, meth)
            tests = [(num, cls(meth)) for num, meth in items]
        except BaseException:
            if os.environ.get("BR_CHILD_LOG"):
                import traceback; traceback.print_exc()
            for num, _ in items:
                out.write(f"{num} F\n")
            continue
        res = Result({id(t): n for n, t in tests})
        try:
            unittest.TestSuite([t for _, t in tests]).run(res)
        except BaseException:
            if os.environ.get("BR_CHILD_LOG"):
                import traceback; traceback.print_exc()
            res.class_error = True
        finally:
            signal.setitimer(signal.ITIMER_PROF, 0)
        for num, _ in tests:
            if num not in res.done:
                out.write(f"{num} F\n")
            elif res.class_error:
                # a failing tearDownClass is reported by pytest as an error on the last test
                pass
        if res.class_error and tests:
            out.write(f"{tests[-1][0]} F\n")
    out.close()
    os._exit(0)


def start(base: str, root: str, files, groups, per_test_timeout, restrict=None, shortcut=None, calls=None):
    k = 0
    while os.path.lexists(root if k == 0 else f"{root}.{k}"):
        k += 1
    root = root if k == 0 else f"{root}.{k}"
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(r)
        try:
            build_overlay(base, root, files)
            child_main(root, groups, w, per_test_timeout, restrict, shortcut, calls)
        except BaseException:
            pass
        os._exit(0)
    os.close(w)
    os.set_blocking(r, False)
    return pid, r, root
