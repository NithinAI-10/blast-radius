"""Lines test_execute_tpcds runs inside each pure phase: P = parse_one in the test,
T = transpile(sql, write="duckdb"), O = optimize inside the executor. Query execution
is skipped (it belongs to the residual phase traced by trace_phase2.py).
Usage: python trace_phase3.py REPO OUT_JSON"""
import json, os, pickle, sys
REPO, OUT = os.path.realpath(sys.argv[1]), sys.argv[2]
os.environ["SKIP_INTEGRATION"] = "0"
sys.path.insert(0, REPO); sys.path.insert(0, "/home/claude/entry"); os.chdir(REPO)
for m in ("numpy", "pandas", "duckdb", "pandas.testing"):
    __import__(m)
import syncpool; syncpool.install()
import unittest
import sqlglot.executor as ex
import tests.test_executor as te

PREF = (os.path.join(REPO, "sqlglot") + os.sep, os.path.join(REPO, "tests") + os.sep)
PL = len(REPO) + 1
lines = {"P": {}, "O": {}, "T": {}}
cur = [None]
mon = sys.monitoring
tool = mon.PROFILER_ID
mon.use_tool_id(tool, "phase3")


def on_line(code, line):
    ph = cur[0]
    if ph is None:
        return None
    fn = code.co_filename
    if fn.startswith(PREF):
        lines[ph].setdefault(fn[PL:], set()).add(line)
    return None


mon.register_callback(tool, mon.events.LINE, on_line)


class Skip(Exception):
    pass


real_opt, real_parse, real_tr = ex.optimize, te.parse_one, te.transpile


def phase(ph, fn, *a, **kw):
    prev = cur[0]
    cur[0] = ph
    try:
        return fn(*a, **kw)
    finally:
        cur[0] = prev


def optimize(sql, schema=None, *a, **kw):
    phase("O", real_opt, sql, schema, *a, **kw)
    raise Skip()


def parse_one(sql, *a, **kw):
    e = phase("P", real_parse, sql, *a, **kw)
    phase("T", real_tr, sql, write="duckdb")
    return e


ex.optimize = optimize
te.parse_one = parse_one
real_mp = te.mp_execute


def mp_execute(expression, meta):
    try:
        return real_mp(expression, meta)
    except Skip:
        return None


mp_execute.__module__ = te.__name__
mp_execute.__qualname__ = "mp_execute"
te.mp_execute = mp_execute
mon.set_events(tool, mon.events.LINE)
res = unittest.TestResult()
unittest.TestSuite([te.TestExecutor("test_execute_tpcds")]).run(res)
mon.set_events(tool, 0)
print("ok", res.wasSuccessful(), res.errors[:1], {k: sum(len(s) for s in v.values()) for k, v in lines.items()})
json.dump({"ok": res.wasSuccessful(), **{k: {p: sorted(s) for p, s in v.items()} for k, v in lines.items()}}, open(OUT, "w"))
