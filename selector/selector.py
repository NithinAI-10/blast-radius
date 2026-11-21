"""Blast Radius selector.

For each patch:
  1. map the change to candidate tests with a coverage/type/open map of the
     pinned sqlglot (analyze.py),
  2. run the candidates that are cheaper to verify than to list against the
     patched tree ourselves (runner.py); heavy fixture-driven tests are re-run
     on just the fixture items the change can reach,
  3. print the tests that failed plus anything not verified, packed onto the
     worker count that minimises the commit's cost.
"""

from __future__ import annotations

import json
import os
import pickle
import select
import sys
import time
import zlib

T0 = time.monotonic()
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import analyze  # noqa: E402
import fixtures_patch  # noqa: E402
import runner  # noqa: E402
import surrogates  # noqa: E402

DATA = os.environ.get("BR_DATA", "/data")
BASE = os.environ.get("BR_BASE", "/app/sqlglot")
MAP = os.environ.get("BR_MAP", os.path.join(HERE, "map.bin"))
TMP = os.environ.get("BR_TMP", "/tmp")
WALL_LIMIT = float(os.environ.get("BR_WALL", "150"))  # seconds; the scorer allows 180 (270 in the final)
PAR = int(os.environ.get("BR_PAR", "2"))
NO_RUN = os.environ.get("BR_NORUN") == "1"
NO_ITEMS = os.environ.get("BR_NOITEMS") == "1"

WORKER_MS = 2000
CHILD_OVERHEAD = 0.12  # s CPU: fork, overlay, sqlglot import
HEAVY_MODS = ("numpy", "pandas", "duckdb", "pandas.testing")
HEAVY_FILES = ("tests/test_executor.py", "tests/test_optimizer.py", "tests/test_serde.py")
DEFAULT_RATIO = 1.15
P_FAIL = float(os.environ.get("BR_PFAIL", "0.2"))  # chance a verified candidate fails and gets listed anyway

PRINTED = set()


def log(*a):
    print(*a, file=sys.stderr)


def partition(ms):
    """ms: [(number, duration_ms)] -> (cost_ms, workers)."""
    if not ms:
        return 0, []
    items = sorted(ms, key=lambda x: -x[1])
    best = None
    total = sum(d for _, d in items)
    for k in range(1, 9):
        if k > len(items):
            break
        if best is not None and max(total / k, items[0][1]) + WORKER_MS * k > best[0]:
            break
        loads = [0] * k
        ws = [[] for _ in range(k)]
        for n, d in items:
            j = loads.index(min(loads))
            loads[j] += d
            ws[j].append(n)
        cost = max(loads) + WORKER_MS * k
        if best is None or cost < best[0]:
            best = (cost, ws)
    return best


def bits_to_list(b):
    out = []
    while b:
        low = b & -b
        out.append(low.bit_length() - 1)
        b ^= low
    return out


def main():
    M = pickle.loads(zlib.decompress(open(MAP, "rb").read()))
    rt = json.load(open(os.path.join(DATA, "tests.json")))["tests"]
    rt_by_id = {t["id"]: t for t in rt}
    idx_to_rt = {}
    rt_to_idx = {}
    for num, tid, _f, _s in M["tests"]:
        t = rt_by_id.get(tid)
        if t is not None:
            idx_to_rt[num] = t["number"]
            rt_to_idx[t["number"]] = num
    known_ids = {tid for _, tid, _, _ in M["tests"]}
    unknown_rt = [t["number"] for t in rt if t["id"] not in known_ids]
    if unknown_rt:
        log(f"warning: {len(unknown_rt)} runtime tests not in map; treating them as always affected")
    dur_ms = {t["number"]: round(t["seconds"] * 1000) for t in rt}
    id_of = {t["number"]: t["id"] for t in rt}
    cpu_tab = M.get("cpu", {})  # map idx -> seconds of CPU when run in our runner
    cls_setup = M.get("cls_setup", {})  # "path::Class" -> seconds
    items_tab = {} if NO_ITEMS else M.get("items", {})
    pfail = M.get("pfail", {})

    def cpu_of(n):
        if id_of[n] in surrogates.SURROGATE_CPU:
            return surrogates.SURROGATE_CPU[id_of[n]]
        i = rt_to_idx.get(n)
        if i is not None and i in cpu_tab:
            return cpu_tab[i]
        return dur_ms[n] / 1000 * DEFAULT_RATIO

    def cls_of(n):
        path, cls, _ = id_of[n].split("::")
        return f"{path}::{cls}"

    def read_orig(p):
        try:
            with open(os.path.join(BASE, p), encoding="utf-8", newline="") as f:
                return f.read()
        except (FileNotFoundError, IsADirectoryError, UnicodeDecodeError, NotADirectoryError):
            return None

    def list_py():
        out = []
        for top in ("sqlglot", "tests"):
            for dp, dn, fn in os.walk(os.path.join(BASE, top)):
                dn[:] = [d for d in dn if d != "__pycache__"]
                for f in fn:
                    if f.endswith(".py"):
                        out.append(os.path.relpath(os.path.join(dp, f), BASE))
        return sorted(out)

    refs = analyze.RefIndex(M, read_orig, list_py)
    commits = sorted(f[:-6] for f in os.listdir(os.path.join(DATA, "commits")) if f.endswith(".patch"))
    plans = {}
    for cid in commits:
        with open(os.path.join(DATA, "commits", cid + ".patch"), encoding="utf-8", newline="") as f:
            text = f.read()
        try:
            ctx = analyze.analyze_patch(M, read_orig, text, refs)
            cand = {idx_to_rt[i] for i in bits_to_list(ctx.bits) if i in idx_to_rt}
            files = getattr(ctx, "files", {})
            if getattr(ctx, "unverifiable", False) or any(not f.startswith(("sqlglot/", "tests/")) for f in files):
                # outside the traced trees (and some tests resolve symlinks there): list, don't verify
                files = None
                ctx.coarse = True
        except Exception as e:  # never crash: fall back to everything, unverified
            log(f"{cid}: analysis error {e!r}; selecting all")
            ctx = None
            cand = set(dur_ms)
            files = None
        if ctx is None or files is None or cand or ctx.coarse:
            cand |= set(unknown_rt)
        plans[cid] = {"cand": cand, "files": files, "ctx": ctx}

    # ---- item restriction for heavy fixture-driven tests
    def restriction(n, p):
        """Return {fixture: [items]} to run test n on, or None for the full test."""
        ctx = p["ctx"]
        i = rt_to_idx.get(n)
        info = items_tab.get(i) if i is not None else None
        if not info or ctx is None or ctx.coarse:
            return None
        sel = {f: 0 for f in info["files"]}
        # code changes: lines outside the base run reach only some items
        for path, span in ctx.spans:
            base = info["base"].get(path, ())
            per = info["it"]
            for ln in span:
                if ln in base:
                    return None
                for f in sel:
                    sel[f] |= per[f].get(path, {}).get(ln, 0)
        if ctx.spans and not any(sel.values()):
            return None
        # fixture file changes: only the items that differ
        for path in getattr(ctx, "data_files", []):
            if not path.startswith("tests/fixtures/"):
                continue
            f = path[len("tests/fixtures/"):]
            if f not in sel:
                continue
            old = read_orig(path)
            new = (p["files"] or {}).get(path)
            if old is None or new is None:
                return None
            ch = fixtures_patch.changed_items_n(old, new, info["files"][f])
            if ch is None:
                return None
            for k in ch:
                sel[f] |= 1 << k
        if not any(sel.values()):
            return None
        return {f: bits_to_list(b) for f, b in sel.items()}

    def restricted_cpu(n, rs):
        info = items_tab[rt_to_idx[n]]
        full = cpu_of(n)
        w = info.get("w")
        if w:
            frac = sum(w[f][k] for f, ks in rs.items() for k in ks if f in w and k < len(w[f]))
            return 0.05 + full * frac * 1.1
        total = sum(info["files"].values()) or 1
        chosen = sum(len(v) for v in rs.values())
        return full * (0.2 + 0.8 * chosen / total)

    sc = M.get("tpcds_sc")
    sc_num = rt_by_id[sc["test"]]["number"] if sc and sc["test"] in rt_by_id else None

    def shortcut_ok(p):
        """True if every changed line this patch can reach runs, for test_execute_tpcds,
        only inside parse/optimize (so unchanged optimized plans imply unchanged runs)."""
        ctx = p["ctx"]
        if sc_num is None or ctx is None or ctx.coarse or p["files"] is None:
            return False
        if any(f.startswith("tests/") and f.endswith(".py") for f in p["files"]):
            return False
        for path, span in ctx.spans:
            pb = sc["phaseB"].get(path, ())
            if any(ln in pb for ln in span):
                return False
        return True

    # ---- decide what to verify by running
    for cid in commits:
        p = plans[cid]
        cand = p["cand"]
        restr = {}
        cost_run = {}
        p["shortcut"] = sc_num in cand and shortcut_ok(p)
        for n in cand:
            rs = restriction(n, p) if p["files"] is not None else None
            if rs is not None:
                restr[n] = rs
                cost_run[n] = restricted_cpu(n, rs)
            else:
                cost_run[n] = cpu_of(n)
            if n == sc_num and p["shortcut"]:
                # parse+optimize of the chosen queries (~11% of the test) plus some re-execution
                cost_run[n] = 1.4 + 0.11 * cost_run[n] + 1.0
        # failure odds depend on the kind of change (measured on 230 labeled commits)
        kinds = p["ctx"].kinds if p["ctx"] is not None else set()
        code_kinds = kinds - {"data", "comments-only", "docstring"}
        if "data" in kinds and not code_kinds:
            kind_factor = None  # fixture edits: the reading tests nearly always fail
        else:
            kind_factor = 1.0

        def p_eff(n):
            if kind_factor is None:
                return 0.98
            return min(0.98, pfail.get(rt_to_idx.get(n), P_FAIL) * kind_factor)

        # run tests in order of run-cost per listed second, keep the best prefix
        order = sorted(cand, key=lambda n: (cost_run[n] / max(dur_ms[n] / 1000, 0.001), cost_run[n]))
        out_only = partition([(n, dur_ms[n]) for n in cand])[0] / 1000
        best = (out_only, 0)
        if p["files"] is not None and not NO_RUN:
            acc = CHILD_OVERHEAD
            classes = set()
            all_pass = 1.0
            for k, n in enumerate(order):
                pe = p_eff(n)
                acc += cost_run[n] + pe * dur_ms[n] / 1000
                all_pass *= 1.0 - pe
                c = cls_of(n)
                if c not in classes:
                    classes.add(c)
                    acc += cls_setup.get(c, 0.0)
                rest = order[k + 1:]
                est = acc + partition([(m, dur_ms[m]) for m in rest])[0] / 1000
                if not rest:
                    est += (1.0 - all_pass) * WORKER_MS / 1000  # a failure still costs a worker
                if est < best[0]:
                    best = (est, k + 1)
        run = order[: best[1]]
        p["out"] = order[best[1]:]
        p["est"] = best[0]
        # jobs: one child for unrestricted tests plus restricted ones; a second
        # child for unrestricted tests that load a fixture we restricted
        jobs = []
        if run:
            merged = {}
            for n in run:
                for f, ks in restr.get(n, {}).items():
                    merged.setdefault(f, set()).update(ks)
            clash, main_job = [], []
            for n in run:
                if n not in restr:
                    info = items_tab.get(rt_to_idx.get(n))
                    if info and set(info["files"]) & set(merged):
                        clash.append(n)
                        continue
                main_job.append(n)
            if main_job:
                jobs.append(({f: sorted(v) for f, v in merged.items()} or None, main_job))
            if clash:
                jobs.append((None, clash))
            # the shortcut patches the executor's optimize; keep it in the job that runs the test
            jobs = [(rs_, nums_, sc["table"] if p["shortcut"] and sc_num in nums_ else None) for rs_, nums_ in jobs]
        p["jobs"] = jobs
        p["run"] = run
        p["cost_run"] = {n: cost_run[n] for n in run}
        why = "; ".join(p["ctx"].why) if p["ctx"] is not None else "error"
        log(
            f"{cid}: cand={len(cand)} run={len(run)} restricted={sum(1 for n in run if n in restr)} "
            f"{'shortcut ' if p['shortcut'] and sc_num in run else ''}"
            f"out={len(p['out'])} est={best[0]:.2f}s | {why[:240]}"
        )

    # ---- run verifications, PAR at a time, cheapest first
    queue = []
    for cid in commits:
        for rs, nums, sct in plans[cid]["jobs"]:
            queue.append((sum(cpu_of(n) for n in nums), cid, rs, nums, sct))
    queue.sort(key=lambda x: x[0])
    if queue and any(id_of[n].startswith(HEAVY_FILES) for _, _, _, nums, _ in queue for n in nums):
        for m in HEAVY_MODS:  # imported once here, shared by every forked child
            try:
                __import__(m)
            except Exception:
                pass
    if queue:
        import unittest  # noqa: F401  (shared by children)

        import syncpool  # noqa: F401

    running = {}
    results = {c: {} for c in commits}
    child_cpu = {c: 0.0 for c in commits}

    def launch(cid, rs, nums, sct=None):
        p = plans[cid]
        exp = p["cost_run"]

        def per_test_timeout(n):
            # generous for small tests, ~3x expected CPU for big ones; a timeout lists the test
            return 4.0 + 3.0 * exp.get(n, cpu_of(n))

        groups = {}
        for n in nums:
            path, cls, meth = id_of[n].split("::")
            groups.setdefault((path[:-3].replace("/", "."), cls), []).append((n, meth))
        g = [(m, c, items) for (m, c), items in groups.items()]
        root = os.path.join(TMP, "br", cid)
        pid, fd, root = runner.start(BASE, root, p["files"], g, per_test_timeout, rs, sct)
        deadline = time.monotonic() + 8.0 + sum(per_test_timeout(n) for n in nums)
        running[fd] = [cid, pid, b"", deadline, root]

    def finish(fd, kill=False):
        cid, pid, buf, _dl, root = running.pop(fd)
        if kill:
            try:
                os.kill(pid, 9)
            except ProcessLookupError:
                pass
        try:
            while True:
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                buf += chunk
        except BlockingIOError:
            pass
        os.close(fd)
        _, _, ru = os.wait4(pid, 0)
        child_cpu[cid] += ru.ru_utime + ru.ru_stime
        for line in buf.decode(errors="replace").split("\n"):
            parts = line.split()
            if len(parts) == 2 and parts[0].isdigit():
                n, s = int(parts[0]), parts[1]
                # a later F (e.g. tearDownClass error) overrides an earlier P
                if results[cid].get(n) != "F":
                    results[cid][n] = s
        _rmtree(root)

    if queue:
        os.makedirs(os.path.join(TMP, "br"), exist_ok=True)
    while queue or running:
        now = time.monotonic()
        while queue and len(running) < PAR and now - T0 < WALL_LIMIT - 10:
            _, cid, rs, nums, sct = queue.pop(0)
            launch(cid, rs, nums, sct)
        if not running:
            break
        fds = list(running)
        timeout = max(0.0, min(min(running[f][3] for f in fds), T0 + WALL_LIMIT) - time.monotonic())
        ready, _, _ = select.select(fds, [], [], min(timeout, 1.0))
        for fd in ready:
            try:
                chunk = os.read(fd, 65536)
            except BlockingIOError:
                continue
            if chunk:
                running[fd][2] += chunk
            else:
                finish(fd)
        now = time.monotonic()
        for fd in list(running):
            if now > running[fd][3] or now - T0 > WALL_LIMIT:
                log(f"{running[fd][0]}: verification killed at deadline")
                finish(fd, kill=True)
        if time.monotonic() - T0 > WALL_LIMIT:
            queue.clear()

    # ---- answer
    total_est = 0
    for cid in commits:
        p = plans[cid]
        res = results[cid]
        final = set(p["out"])
        failed = 0
        for n in p["run"]:
            s = res.get(n)
            if s != "P":
                final.add(n)
                failed += s == "F"
        cost, workers = partition([(n, dur_ms[n]) for n in final])
        total_est += cost
        print(json.dumps({"commit": cid, "workers": workers}, separators=(",", ":")))
        PRINTED.add(cid)
        log(
            f"{cid}: cpu={child_cpu[cid]:.2f} ran={len(p['run'])} failed={failed} "
            f"unknown={sum(1 for n in p['run'] if n not in res)} final={len(final)} cost={cost/1000:.2f}s"
        )
    sys.stdout.flush()
    log(f"simulated≈{total_est/1000:.1f}s child_cpu={sum(child_cpu.values()):.1f}s wall={time.monotonic()-T0:.1f}s")


def _rmtree(path):
    try:
        for dirpath, dirnames, filenames in os.walk(path, topdown=False, followlinks=False):
            for f in filenames:
                os.unlink(os.path.join(dirpath, f))
            for d in dirnames:
                full = os.path.join(dirpath, d)
                if os.path.islink(full):
                    os.unlink(full)
                else:
                    os.rmdir(full)
        os.rmdir(path)
    except OSError:
        pass


def fallback():
    """Last resort: everything for every commit not yet answered. Valid, slow, never misses."""
    rt = json.load(open(os.path.join(DATA, "tests.json")))["tests"]
    allw = partition([(t["number"], round(t["seconds"] * 1000)) for t in rt])[1]
    for f in sorted(os.listdir(os.path.join(DATA, "commits"))):
        if f.endswith(".patch") and f[:-6] not in PRINTED:
            print(json.dumps({"commit": f[:-6], "workers": allw}, separators=(",", ":")))
    sys.stdout.flush()


if __name__ == "__main__":
    try:
        main()
    except BaseException as e:  # noqa: BLE001
        import traceback

        traceback.print_exc()
        log(f"fatal: {e!r}; falling back")
        fallback()
