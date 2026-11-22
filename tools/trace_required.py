"""Per test, which (expression class, argument) pairs were checked for a missing
required argument. In sqlglot an arg_types value (True = required) is read only when a
class is created, to build required_args, which Expression.error_messages alone uses.
So flipping arg_types[k] of a class can change a test only if that test validates an
instance of an affected class while k is missing. We record every (class name, arg)
that is missing whenever error_messages runs, for every arg in the class's arg_types.
Usage: python trace_required.py REPO TESTS_JSON OUT_DIR WORKER_INDEX WORKER_COUNT"""
import json, os, sys, time

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

from sqlglot.expressions.core import Expression  # noqa: E402

SEEN = set()
CALLS = [0]
_orig = Expression.error_messages


def error_messages(self, args=None):
    CALLS[0] += 1
    try:
        cls = type(self)
        a = self.args
        for k in cls.arg_types:
            v = a.get(k)
            if v is None or (isinstance(v, list) and not v):
                SEEN.add((cls.__name__, k))
    except Exception:
        SEEN.add(("*", "*"))
    return _orig(self, args)


Expression.error_messages = error_messages


def main():
    tests = json.load(open(TESTS_JSON))["tests"]
    mine = [t for t in tests if t["number"] % WN == WI]
    os.makedirs(OUT, exist_ok=True)
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
            data = {"number": t["number"], "missing": sorted(f"{c}.{k}" for c, k in SEEN), "calls": CALLS[0],
                    "outcome": outcome[0], "secs": round(time.time() - t0, 3)}
            with open(path + ".tmp", "w") as f:
                json.dump(data, f)
            os.replace(path + ".tmp", path)
            os._exit(0)
        os.waitpid(pid, 0)


main()
