"""Evaluate a selector dir on an eval set. Labels are used only here (scoring), never by the selector.
Usage: python evalsel.py SELECTOR_DIR EVAL_DIR [topN]"""
import json, os, subprocess, sys, resource, time
sys.path.insert(0, "/home/claude/entry")
from selector import partition  # same partition function as the entry
sel, d = sys.argv[1], sys.argv[2]
top = int(sys.argv[3]) if len(sys.argv) > 3 else 0
tests = {t["number"]: t for t in json.load(open(f"{d}/tests.json"))["tests"]}
ms = {n: round(t["seconds"] * 1000) for n, t in tests.items()}
ans = json.load(open(f"{d}/answers.json"))["commits"]
env = dict(os.environ, BR_DATA=d, BR_BASE="/home/claude/sqlglot", BR_TMP="/tmp/brtmp")
r0 = resource.getrusage(resource.RUSAGE_CHILDREN)
p = subprocess.run([sys.executable, f"{sel}/selector.py"], env=env, capture_output=True, text=True)
r1 = resource.getrusage(resource.RUSAGE_CHILDREN)
cpu = (r1.ru_utime + r1.ru_stime) - (r0.ru_utime + r0.ru_stime)
out = {json.loads(l)["commit"]: json.loads(l)["workers"] for l in p.stdout.splitlines() if l.strip()}
sim = 0.0; orc = 0.0; miss = 0; rows = []
for c, v in ans.items():
    ws = out[c]
    sel_ = {n for w in ws for n in w}
    cost = (max(sum(ms[n] for n in w) for w in ws) + 2000 * len(ws)) / 1000 if ws else 0.0
    m = bool(set(v["failing"]) - sel_)
    miss += m
    o = partition([(n, ms[n]) for n in v["failing"]])[0] / 1000
    sim += cost + 300 * m; orc += o
    extra = sorted(sel_ - set(v["failing"]), key=lambda n: -ms[n])
    rows.append((cost - o, c, cost, o, len(v["failing"]), [tests[n]["id"].split("::")[-1] + f"({ms[n]/1000:.1f})" for n in extra[:3]]))
print(f"{os.path.basename(d.rstrip('/'))}: n={len(ans)} sim={sim:.1f} cpu={cpu:.1f} total={sim+cpu:.1f} misses={miss} oracle={orc:.1f} gap={sim-orc:.1f}")
for r in sorted(rows, reverse=True)[:top]:
    print(f"  gap={r[0]:6.2f} {r[1]} cost={r[2]:.2f} oracle={r[3]:.2f} failing={r[4]} extra={r[5]}")
