"""Trace test_execute_tpcds by phase.

Phases: P = parse_one in the test, O = optimize inside the executor,
T = transpile(..., write="duckdb") in cached_execute, R = everything else
(imports, setUpClass, planner/python executor, comparison).

Writes: residual lines (R), the per-query fingerprint table
{canon(parsed): [canon(optimized), transpiled duckdb SQL]}, and phase timings.
Usage: python trace_phase2.py REPO OUT_JSON
"""
import json, os, pickle, sys, time
REPO, OUT = os.path.realpath(sys.argv[1]), sys.argv[2]
os.environ["SKIP_INTEGRATION"] = "0"
sys.path.insert(0, REPO); sys.path.insert(0, "/home/claude/entry"); os.chdir(REPO)
for m in ("numpy", "pandas", "duckdb", "pandas.testing"):
    __import__(m)

PREF = (os.path.join(REPO, "sqlglot") + os.sep, os.path.join(REPO, "tests") + os.sep)
PL = len(REPO) + 1
resid = {}
depth = [0]
mon = sys.monitoring
tool = mon.PROFILER_ID
mon.use_tool_id(tool, "phase2")


def on_line(code, line):
    if depth[0]:
        return None
    fn = code.co_filename
    if fn.startswith(PREF):
        resid.setdefault(fn[PL:], set()).add(line)
    return mon.DISABLE


mon.register_callback(tool, mon.events.LINE, on_line)
mon.set_events(tool, mon.events.LINE)
# imports happen while tracing: import-time lines count as residual
import syncpool; syncpool.install()
import unittest
import sqlglot.executor as ex
import tests.test_executor as te
from canon import canon

T = {"P": 0.0, "O": 0.0, "T": 0.0}
opt_of = {}
duck_of = {}
parsed_of = {}

real_opt = ex.optimize


def optimize(sql, schema=None, *a, **kw):
    depth[0] += 1
    t0 = time.process_time()
    try:
        ih = None if isinstance(sql, str) else canon(sql)
        out = real_opt(sql, schema, *a, **kw)
        if ih is not None:
            opt_of[ih] = canon(out)
        return out
    finally:
        T["O"] += time.process_time() - t0
        depth[0] -= 1


ex.optimize = optimize
real_parse = te.parse_one


def parse_one(sql, *a, **kw):
    depth[0] += 1
    t0 = time.process_time()
    try:
        e = real_parse(sql, *a, **kw)
        parsed_of[sql] = canon(pickle.loads(pickle.dumps(e)))
        return e
    finally:
        T["P"] += time.process_time() - t0
        depth[0] -= 1


te.parse_one = parse_one
real_tr = te.transpile


def transpile(sql, *a, **kw):
    depth[0] += 1
    t0 = time.process_time()
    try:
        out = real_tr(sql, *a, **kw)
        if kw.get("write") == "duckdb" and not a:
            duck_of[sql] = out[0]
        return out
    finally:
        T["T"] += time.process_time() - t0
        depth[0] -= 1


te.transpile = transpile
t0 = time.process_time()
res = unittest.TestResult()
unittest.TestSuite([te.TestExecutor("test_execute_tpcds")]).run(res)
total = time.process_time() - t0
mon.set_events(tool, 0)
print("counts", len(parsed_of), len(opt_of), len(duck_of), sum(1 for p in parsed_of.values() if p in opt_of), sum(1 for q in parsed_of if q in duck_of))
table = {}
for sql, ph in parsed_of.items():
    if ph in opt_of and sql in duck_of:
        table[ph] = [opt_of[ph], duck_of[sql]]
print("ok", res.wasSuccessful(), res.errors[:1], res.failures[:1], "queries", len(parsed_of), "table", len(table),
      "resid lines", sum(len(v) for v in resid.values()), "times", {k: round(v, 2) for k, v in T.items()}, "total", round(total, 2))
json.dump({"ok": res.wasSuccessful(), "table": table, "resid": {k: sorted(v) for k, v in resid.items()},
           "times": T, "total": total}, open(OUT, "w"))
