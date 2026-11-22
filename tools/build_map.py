"""Compile raw per-test traces into the compact map the selector loads.

Test sets are stored as Python ints used as bitsets (bit i = test number i),
deduplicated, and the whole thing is pickled + zlib-compressed.

Usage: python build_map.py RAW_DIR TESTS_JSON OUT_FILE
"""

import glob
import json
import os
import pickle
import sys
import zlib
from collections import defaultdict

raw_dir, tests_json, out = sys.argv[1:4]
extra = sys.argv[4:]  # cpu.json and items_*.json files
tests = json.load(open(tests_json))["tests"]
n = len(tests)

rl = defaultdict(lambda: defaultdict(int))  # runtime lines: file -> line -> bits
il = defaultdict(lambda: defaultdict(int))  # import-time lines
types = defaultdict(int)
opens = defaultdict(int)
spawn = 0
failed_here = 0
secs_here = {}
missing = []

for t in tests:
    i = t["number"]
    p = os.path.join(raw_dir, f"{i}.json")
    if not os.path.exists(p):
        missing.append(i)
        continue
    d = json.load(open(p))
    b = 1 << i
    for f, ls in d["lines"].items():
        m = rl[f]
        for ln in ls:
            m[ln] |= b
    for f, ls in d.get("ilines", {}).items():
        m = il[f]
        for ln in ls:
            m[ln] |= b
    for ty in d["types"]:
        types[ty] |= b
    for f in d["opens"]:
        opens[f] |= b
    if d["spawn"]:
        spawn |= b
    if d["outcome"] != "passed":
        failed_here |= b
    secs_here[i] = d["secs"]

# .py opens recorded during tracing include bytecode recompiles by the import
# system; keep only real reads, as rechecked by recheck_opens.py
py_opens_path = os.path.join(os.path.dirname(raw_dir), "py_opens.json")
if os.path.exists(py_opens_path):
    real = json.load(open(py_opens_path))
    for f in list(opens):
        if f.endswith(".py"):
            b = 0
            for i, fl in real.items():
                if f in fl:
                    b |= 1 << int(i)
            opens[f] = b
    opens = defaultdict(int, {k: v for k, v in opens.items() if v})

pool = {}


def intern(v):
    return pool.setdefault(v, v)


def freeze(m):
    return {f: {ln: intern(v) for ln, v in lines.items()} for f, lines in m.items()}


data = {
    "n": n,
    "tests": [(t["number"], t["id"], t["file"], t["seconds"]) for t in tests],
    "rl": freeze(rl),
    "il": freeze(il),
    "types": {k: intern(v) for k, v in types.items()},
    "opens": {k: intern(v) for k, v in opens.items()},
    "spawn": spawn,
}
probe_files = sorted(glob.glob(os.path.join(os.path.dirname(raw_dir), "..", "probes", "p_*.json")))
if probe_files:
    probe, pimport, pfailed, seen, bsites = defaultdict(int), set(), 0, set(), set()
    for pf_ in probe_files:
        d = json.load(open(pf_))
        pimport.update(d.pop("_import", []))
        bsites.update(d.pop("_boolsites", []))
        for k, sites in d.items():
            i = int(k)
            seen.add(i)
            for st in sites:
                if st == "!failed":
                    pfailed |= 1 << i
                else:
                    probe[st] |= 1 << i
    for t in tests:  # tests the probe run never reached count as always differing
        if t["number"] not in seen:
            pfailed |= 1 << t["number"]
    data["probe"] = {k: intern(v) for k, v in probe.items()}
    data["probe_import"] = frozenset(pimport)
    data["probe_failed"] = pfailed
    data["probe_boolsites"] = frozenset(bsites)
    print(f"probe sites={len(probe)} import={len(pimport)} failed_under_probe={pfailed.bit_count()}", file=sys.stderr)

ph_path = os.path.join(os.path.dirname(raw_dir), "tpcds_phase.json")
if os.path.exists(ph_path):
    ph = json.load(open(ph_path))
    if ph.get("ok"):
        data["tpcds_sc"] = {
            "test": "tests/test_executor.py::TestExecutor::test_execute_tpcds",
            "phaseB": {k: frozenset(v) for k, v in ph["phaseB"].items()},
            "table": ph["table"],
        }

ph2_path = os.path.join(os.path.dirname(raw_dir), "tpcds_phase2.json")
if os.path.exists(ph2_path):
    ph2 = json.load(open(ph2_path))
    if ph2.get("ok"):
        data["tpcds_sc2"] = {
            "test": "tests/test_executor.py::TestExecutor::test_execute_tpcds",
            "resid": {k: frozenset(v) for k, v in ph2["resid"].items()},
            "table": {k: tuple(v) for k, v in ph2["table"].items()},
        }
        ph3_path = os.path.join(os.path.dirname(raw_dir), "tpcds_phase3.json")
        ph3 = json.load(open(ph3_path))
        assert ph3.get("ok")
        for k in ("O", "T"):
            data["tpcds_sc2"][k] = {p: frozenset(v) for p, v in ph3[k].items()}

dk_dir = os.path.join(os.path.dirname(raw_dir), "dictkeys")
if os.path.isdir(dk_dir):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import build_dictkeys

    repo_dir = os.environ.get("BR_REPO", "/home/claude/sqlglot")  # the pinned checkout
    data["dk"] = build_dictkeys.build(repo_dir, dk_dir, {t["number"]: t["number"] for t in tests})

pf_path = os.path.join(os.path.dirname(raw_dir), "pfail.json")
if os.path.exists(pf_path):
    data["pfail"] = {int(k): v for k, v in json.load(open(pf_path)).items()}

for e in extra:
    d = json.load(open(e))
    if "cpu" in d:
        data["cpu"] = {int(k): v for k, v in d["cpu"].items()}
        # tests that spawn a subprocess spend CPU we did not measure in-process
        for t in tests:
            if spawn >> t["number"] & 1:
                data["cpu"][t["number"]] = max(data["cpu"].get(t["number"], 0), t["seconds"] * 1.5)
        data["cls_setup"] = d["cls_setup"]
        continue
    items = data.setdefault("items", {})
    for k, v in d.items():
        if not v:
            continue
        base = {p: set(ls) for p, ls in v["base"].items()}
        it = {}
        for f, per in v["items"].items():
            m = {}
            for kidx, lines in enumerate(per):
                b = 1 << kidx
                for p, ls in lines.items():
                    bl = base.get(p, ())
                    mp = m.setdefault(p, {})
                    for ln in ls:
                        if ln not in bl:
                            mp[ln] = mp.get(ln, 0) | b
            it[f] = m
        w = None
        if "item_cpu" in v:
            bc = v.get("base_cpu", 0.0)
            inc = {f: [max(0.0, c - bc) for c in cs] for f, cs in v["item_cpu"].items()}
            S = sum(sum(x) for x in inc.values())
            if S > 0:
                w = {f: [x / S for x in xs] for f, xs in inc.items()}
        items[int(k)] = {"files": v["loads"], "base": {p: frozenset(ls) for p, ls in base.items()}, "it": it, "w": w}

blob = zlib.compress(pickle.dumps(data, protocol=5), 9)
open(out, "wb").write(blob)
print(
    f"tests={n} missing={len(missing)} unique_sets={len(pool)} files_rl={len(rl)} "
    f"types={len(types)} opens={len(opens)} spawn={bin(spawn).count('1')} "
    f"failed_here={bin(failed_here).count('1')} items={len(data.get('items', {}))} cpu={len(data.get('cpu', {}))} size={len(blob)/1e6:.2f}MB",
    file=sys.stderr,
)
if failed_here:
    print("failed here:", [t[1] for t in data["tests"] if failed_here >> t[0] & 1][:20], file=sys.stderr)
