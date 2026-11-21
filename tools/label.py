"""Ground-truth labeler: run (almost) the whole suite against each patch.

Usage: python label.py COMMITS_DIR OUT_JSON WORKER_INDEX WORKER_COUNT [skip_ids_file]
Writes {commit: {"failing": [...], "unknown": [...]}} (merged across workers by caller).
"""

import json
import os
import sys
import time

sys.path.insert(0, "/home/claude/entry")
import analyze  # noqa: E402
import runner  # noqa: E402

BASE = "/home/claude/sqlglot"
tests = json.load(open("/home/claude/kit/sample/tests.json"))["tests"]
SKIP = {
    "tests/test_parser.py::TestParser::test_max_nodes",
    "tests/test_executor.py::TestExecutor::test_execute_tpcds",
    "tests/test_generator.py::TestGenerator::test_generator_memory",
}
cdir, out, wi, wn = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
commits = sorted(f[:-6] for f in os.listdir(cdir) if f.endswith(".patch"))
res = json.load(open(out)) if os.path.exists(out) else {}

groups = {}
for t in tests:
    if t["id"] in SKIP:
        continue
    path, cls, meth = t["id"].split("::")
    groups.setdefault((path[:-3].replace("/", "."), cls), []).append((t["number"], meth))
G = [(m, c, items) for (m, c), items in groups.items()]
dur = {t["number"]: t["seconds"] for t in tests}
HEAVY = {"TestExecutor", "TestOptimizer"}
CHUNKS = [[g] for g in G if g[1] in HEAVY]
rest = [g for g in G if g[1] not in HEAVY]
for i in range(4):
    CHUNKS.append(rest[i::4])


def rd(p):
    try:
        return open(os.path.join(BASE, p), encoding="utf-8", newline="").read()
    except FileNotFoundError:
        return None


if os.environ.get("REVERSE") == "1":
    commits = commits[::-1]
for k, cid in enumerate(commits):
    if k % wn != wi or cid in res:
        continue
    text = open(os.path.join(cdir, cid + ".patch")).read()
    files = {}
    for fc in analyze.parse_patch(text):
        old = rd(fc.old_path) if fc.old_path else None
        new = analyze.apply_change(old, fc)[0]
        files[fc.path] = new if fc.new_path else None
    t0 = time.time()
    root = f"/tmp/label/{cid}"
    buf = b""
    for chunk in CHUNKS:
        pid, fd, r2 = runner.start(BASE, root, files, chunk, lambda n: 20 + 10 * dur[n])
        os.set_blocking(fd, True)
        while True:
            c = os.read(fd, 65536)
            if not c:
                break
            buf += c
        os.close(fd)
        os.waitpid(pid, 0)
        os.system(f"rm -rf {r2}")
    st = {}
    for line in buf.decode().split("\n"):
        if line:
            n, s = line.split()
            st[int(n)] = s
    failing = sorted(n for n, s in st.items() if s == "F")
    unknown = sorted(t["number"] for t in tests if t["id"] not in SKIP and t["number"] not in st)
    res[cid] = {"failing": failing, "unknown": unknown}
    os.system(f"rm -rf {root}")
    print(cid, len(failing), len(unknown), f"{time.time()-t0:.1f}s", flush=True)
    json.dump(res, open(out + ".tmp", "w"))
    os.rename(out + ".tmp", out)
