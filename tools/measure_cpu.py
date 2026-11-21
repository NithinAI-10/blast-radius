"""Measure CPU seconds per test (and per-class setup) when run the way runner.py runs them.

Usage: python measure_cpu.py REPO TESTS_JSON OUT_JSON
"""

import json
import os
import resource
import sys

REPO, TESTS_JSON, OUT = sys.argv[1:4]
sys.path.insert(0, "/home/claude/entry")
os.environ["SKIP_INTEGRATION"] = "0"
import syncpool  # noqa: E402

syncpool.install()
import unittest  # noqa: E402

for m in ("numpy", "pandas", "duckdb", "pandas.testing"):
    __import__(m)

tests = json.load(open(TESTS_JSON))["tests"]
groups = {}
for t in tests:
    path, cls, meth = t["id"].split("::")
    groups.setdefault((path, cls), []).append((t["number"], meth))


def cpu():
    r = resource.getrusage(resource.RUSAGE_SELF)
    return r.ru_utime + r.ru_stime


out = {"cpu": {}, "cls_setup": {}}
for (path, cls), items in groups.items():
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(r)
        dn = os.open(os.devnull, os.O_WRONLY)
        os.dup2(dn, 1)
        os.dup2(dn, 2)
        os.chdir(REPO)
        sys.path.insert(0, REPO)
        c0 = cpu()
        mod = __import__(path[:-3].replace("/", "."), fromlist=["_"])
        C = getattr(mod, cls)
        objs = {id(o): n for n, o in ((n, C(m)) for n, m in items)}
        rec = {}
        marks = {}

        class R(unittest.TestResult):
            def startTest(self, test):
                super().startTest(test)
                marks["s"] = cpu()
                marks.setdefault("first", marks["s"])

            def stopTest(self, test):
                e = cpu()
                rec[objs[id(test)]] = e - marks["s"]
                marks["last"] = e
                super().stopTest(test)

        suite = unittest.TestSuite([o for o in [C(m) for _, m in items]])
        objs = {id(o): n for o, (n, _) in zip(suite._tests, items)}
        suite.run(R())
        c1 = cpu()
        setup = (marks.get("first", c1) - c0) + (c1 - marks.get("last", c1))
        os.write(w, json.dumps({"rec": rec, "setup": setup}).encode())
        os._exit(0)
    os.close(w)
    data = b""
    while True:
        c = os.read(r, 65536)
        if not c:
            break
        data += c
    os.close(r)
    os.waitpid(pid, 0)
    d = json.loads(data)
    for n, v in d["rec"].items():
        out["cpu"][int(n)] = round(v, 4)
    out["cls_setup"][f"{path}::{cls}"] = round(d["setup"], 4)
    print(path, cls, f"setup={d['setup']:.2f}", f"sum={sum(d['rec'].values()):.2f}", flush=True)
json.dump(out, open(OUT, "w"))
