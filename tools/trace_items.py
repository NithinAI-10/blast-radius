"""Per-fixture-item traces for heavy tests that iterate over sqlglot fixture files.

For a test T that pulls items from fixture files through tests.helpers'
loaders, we run T (each run in its own forked process, unittest, traced):
  - once with every fixture list empty  -> base lines B
  - once per item k of each file F, with only that item -> lines L(F,k)
At selection time, a changed line outside B can only affect the items whose
runs executed it, so the test can be re-run with just those items.

Usage: python trace_items.py REPO TESTS_JSON OUT_JSON MIN_SECONDS WORKER_INDEX WORKER_COUNT
"""

import json
import os
import sys

REPO, TESTS_JSON, OUT, MIN_S, WI, WN = sys.argv[1], sys.argv[2], sys.argv[3], float(sys.argv[4]), int(sys.argv[5]), int(sys.argv[6])
REPO = os.path.realpath(REPO)
os.environ["SKIP_INTEGRATION"] = "0"
sys.path.insert(0, REPO)
sys.path.insert(0, "/home/claude/entry")
os.chdir(REPO)

import syncpool  # noqa: E402

syncpool.install()
import unittest  # noqa: E402,F401

for mod in ("numpy", "pandas", "duckdb", "pandas.testing"):
    try:
        __import__(mod)
    except Exception:
        pass

import fixtures_patch  # noqa: E402

PREFIXES = (os.path.join(REPO, "sqlglot") + os.sep, os.path.join(REPO, "tests") + os.sep)
PLEN = len(REPO) + 1


def run_in_child(test_id, restrict, want_lines):
    """Fork; run one test with fixture restriction; return (lines, loads, ok)."""
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(r)
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, 1)
        os.dup2(devnull, 2)
        lines = {}
        if want_lines:
            mon = sys.monitoring
            tool = mon.PROFILER_ID
            mon.use_tool_id(tool, "items")

            def on_line(code, line):
                fn = code.co_filename
                if fn.startswith(PREFIXES):
                    s = lines.get(fn[PLEN:])
                    if s is None:
                        s = lines[fn[PLEN:]] = set()
                    s.add(line)
                return mon.DISABLE

            mon.register_callback(tool, mon.events.LINE, on_line)
            mon.set_events(tool, mon.events.LINE)
        loads = fixtures_patch.install(restrict)
        path, cls, meth = test_id.split("::")
        ok = False
        try:
            mod = __import__(path[:-3].replace("/", "."), fromlist=["_"])
            fixtures_patch.patch_module(mod)
            res = unittest.TestResult()
            unittest.TestSuite([getattr(mod, cls)(meth)]).run(res)
            ok = res.wasSuccessful()
        except BaseException:
            pass
        if want_lines:
            sys.monitoring.set_events(tool, 0)
        data = json.dumps({"lines": {k: sorted(v) for k, v in lines.items()}, "loads": loads, "ok": ok})
        with os.fdopen(w, "w") as f:
            f.write(data)
        os._exit(0)
    os.close(w)
    with os.fdopen(r) as f:
        data = f.read()
    _, _, ru = os.wait4(pid, 0)
    d = json.loads(data) if data else {"lines": {}, "loads": {}, "ok": False}
    d["cpu"] = ru.ru_utime + ru.ru_stime
    return d


def main():
    tests = json.load(open(TESTS_JSON))["tests"]
    cands = [
        t for t in tests
        if t["file"] in ("tests/test_optimizer.py", "tests/test_executor.py", "tests/test_transpile.py", "tests/test_serde.py")
        and t["seconds"] >= MIN_S
    ]
    out = json.load(open(OUT)) if os.path.exists(OUT) else {}
    only_cpu = os.environ.get("ONLY_CPU") == "1"
    for j, t in enumerate(cands):
        key = str(t["number"])
        if only_cpu:
            v = out.get(key)
            if not v or "item_cpu" in v or j % WN != WI:
                continue
            empty = {f: [] for f in v["loads"]}
            v["base_cpu"] = round(run_in_child(t["id"], empty, False)["cpu"], 4)
            v["item_cpu"] = {}
            for f, n in v["loads"].items():
                pc = []
                for k in range(n):
                    rs = dict(empty)
                    rs[f] = [k]
                    pc.append(round(run_in_child(t["id"], rs, False)["cpu"], 4))
                v["item_cpu"][f] = pc
            with open(OUT + ".tmp", "w") as fh:
                json.dump(out, fh)
            os.rename(OUT + ".tmp", OUT)
            print("cpu", t["id"], flush=True)
            continue
        if j % WN != WI or str(t["number"]) in out:
            continue
        probe = run_in_child(t["id"], None, False)
        loads = probe["loads"]  # filename -> item count
        if not loads:
            out[str(t["number"])] = None
            continue
        empty = {f: [] for f in loads}
        base = run_in_child(t["id"], empty, True)
        items = {}
        cpus = {}
        for f, n in loads.items():
            per = []
            pc = []
            for k in range(n):
                rs = dict(empty)
                rs[f] = [k]
                r1 = run_in_child(t["id"], rs, True)
                per.append(r1["lines"])
                pc.append(round(r1["cpu"], 4))
            items[f] = per
            cpus[f] = pc
        out[str(t["number"])] = {
            "loads": loads, "base": base["lines"], "base_ok": base["ok"], "items": items,
            "base_cpu": round(base["cpu"], 4), "item_cpu": cpus, "probe_cpu": round(probe["cpu"], 4),
        }
        with open(OUT + ".tmp", "w") as fh:
            json.dump(out, fh)
        os.rename(OUT + ".tmp", OUT)
        print(t["id"], loads, flush=True)


if __name__ == "__main__":
    main()
