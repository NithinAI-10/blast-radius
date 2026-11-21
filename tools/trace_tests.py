"""Build a per-test dependency map for sqlglot at the pinned commit.

Each test runs in its own forked process (pytest + heavy third-party libs are
pre-imported in the parent, sqlglot is not), so import-time lines and caches
are attributed to every test that needs them.

Per test we record:
  lines   : {relpath: [line, ...]} for every executed line under sqlglot/ and tests/
  opens   : repo-relative files the test opened (fixtures, sources read as text)
  spawn   : True if the test started a subprocess
  outcome : passed/failed as seen here
  secs    : wall seconds here

Usage: python trace_tests.py REPO TESTS_JSON OUT_DIR WORKER_INDEX WORKER_COUNT
"""

import json
import os
import sys
import time

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


class Tracer:
    def __init__(self):
        self.lines = {}
        self.opens = set()
        self.spawn = False
        self.outcome = "passed"
        mon = sys.monitoring
        self.tool = mon.PROFILER_ID
        mon.use_tool_id(self.tool, "tracer")
        prefix_s = os.path.join(REPO, "sqlglot") + os.sep
        prefix_t = os.path.join(REPO, "tests") + os.sep
        lines = self.lines
        DISABLE = mon.DISABLE
        plen = len(REPO) + 1

        ilines = self.ilines = {}

        def on_line(code, line):
            fn = code.co_filename
            if fn.startswith(prefix_s) or fn.startswith(prefix_t):
                rel = fn[plen:]
                d = lines if code.co_flags & 1 else ilines
                s = d.get(rel)
                if s is None:
                    s = d[rel] = set()
                s.add(line)
            return DISABLE

        types = self.types = set()
        getframe = sys._getframe

        def on_start(code, offset):
            fn = code.co_filename
            if not fn.startswith(prefix_s):
                return DISABLE
            if code.co_argcount:
                v0 = code.co_varnames[0]
                if v0 == "self":
                    try:
                        obj = getframe(1).f_locals[v0]
                    except Exception:
                        return
                    tp = obj if v0 == "cls" else type(obj)
                    if isinstance(tp, type):
                        types.add(tp)

        mon.register_callback(self.tool, mon.events.LINE, on_line)
        mon.register_callback(self.tool, mon.events.PY_START, on_start)
        mon.set_events(self.tool, mon.events.LINE | mon.events.PY_START)

        def audit(event, args):
            if event == "open":
                p = args[0]
                if isinstance(p, (str, bytes)):
                    p = os.fsdecode(p)
                    try:
                        ap = os.path.realpath(p)
                    except Exception:
                        return
                    if ap.startswith(REPO + os.sep) and "__pycache__" not in ap:
                        self.opens.add(ap[plen:])
            elif event in ("subprocess.Popen", "os.posix_spawn", "os.exec", "os.fork"):
                self.spawn = True

        sys.addaudithook(audit)

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_makereport(self, item, call):
        outcome = yield
        rep = outcome.get_result()
        if rep.failed:
            self.outcome = "failed"


def main():
    tests = json.load(open(TESTS_JSON))["tests"]
    mine = [t for t in tests if t["number"] % WN == WI]
    os.makedirs(OUT, exist_ok=True)
    for t in mine:
        path = os.path.join(OUT, f"{t['number']}.json")
        if os.path.exists(path):
            continue
        r, w = os.pipe()
        pid = os.fork()
        if pid == 0:
            os.close(r)
            tr = Tracer()
            t0 = time.time()
            try:
                pytest.main(
                    [t["id"], "-q", "-p", "no:cacheprovider", "-p", "no:randomly", "--no-header", "-x"],
                    plugins=[tr],
                )
            except BaseException:
                tr.outcome = "error"
            sys.monitoring.set_events(tr.tool, 0)
            data = {
                "number": t["number"],
                "lines": {k: sorted(v) for k, v in tr.lines.items()},
                "ilines": {k: sorted(v) for k, v in tr.ilines.items()},
                "opens": sorted(tr.opens),
                "spawn": tr.spawn,
                "types": sorted({f"{c.__module__}:{c.__qualname__}" for tp in tr.types for c in tp.__mro__ if c.__module__.startswith("sqlglot")}),
                "outcome": tr.outcome,
                "secs": round(time.time() - t0, 3),
            }
            with open(path + ".tmp", "w") as f:
                json.dump(data, f)
            os.rename(path + ".tmp", path)
            os._exit(0)
        os.close(w)
        os.close(r)
        os.waitpid(pid, 0)


if __name__ == "__main__":
    main()
