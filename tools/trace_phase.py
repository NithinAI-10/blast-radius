"""Trace test_execute_tpcds: lines run outside parse/optimize, and the
input->optimized fingerprint table. Usage: python trace_phase.py REPO OUT_JSON"""
import json, os, sys
REPO, OUT = os.path.realpath(sys.argv[1]), sys.argv[2]
os.environ["SKIP_INTEGRATION"] = "0"
sys.path.insert(0, REPO); sys.path.insert(0, "/home/claude/entry"); os.chdir(REPO)
import syncpool; syncpool.install()
import unittest
for m in ("numpy", "pandas", "duckdb", "pandas.testing"):
    __import__(m)
from canon import canon

PREF = (os.path.join(REPO, "sqlglot") + os.sep, os.path.join(REPO, "tests") + os.sep)
PL = len(REPO) + 1
lines = {}
depth = [0]
mon = sys.monitoring
tool = mon.PROFILER_ID
mon.use_tool_id(tool, "phase")

def on_line(code, line):
    if depth[0]:
        return None
    fn = code.co_filename
    if fn.startswith(PREF):
        lines.setdefault(fn[PL:], set()).add(line)
    return mon.DISABLE

mon.register_callback(tool, mon.events.LINE, on_line)
mon.set_events(tool, mon.events.LINE)
import sqlglot.executor as ex
import tests.test_executor as te
table = {}
real_opt = ex.optimize
def optimize(sql, schema=None, *a, **kw):
    depth[0] += 1
    try:
        ih = None if isinstance(sql, str) else canon(sql)
        out = real_opt(sql, schema, *a, **kw)
        if ih is not None:
            table[ih] = canon(out)
        return out
    finally:
        depth[0] -= 1
ex.optimize = optimize
real_parse = te.parse_one
def parse_one(*a, **kw):
    depth[0] += 1
    try:
        return real_parse(*a, **kw)
    finally:
        depth[0] -= 1
te.parse_one = parse_one
res = unittest.TestResult()
unittest.TestSuite([te.TestExecutor("test_execute_tpcds")]).run(res)
mon.set_events(tool, 0)
print("ok", res.wasSuccessful(), "table", len(table), "phaseB lines", sum(len(v) for v in lines.values()))
json.dump({"ok": res.wasSuccessful(), "table": table, "phaseB": {k: sorted(v) for k, v in lines.items()}}, open(OUT, "w"))
