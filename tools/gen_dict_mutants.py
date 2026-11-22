"""Mutants that change one constant inside one entry of a class-level dict literal.
Usage: python gen_dict_mutants.py REPO OUT_DIR N SEED [DK_PICKLE]"""
import ast, difflib, json, os, pickle, random, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "selector"))
REPO, OUT, N, SEED = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
from analyze import _class_dicts, _key_label
dk = pickle.load(open(sys.argv[5], "rb")) if len(sys.argv) > 5 else {"unsafe": set(), "unsafe_keys": {}}
rng = random.Random(SEED)
sites = []
for dp, dn, fn in os.walk(os.path.join(REPO, "sqlglot")):
    for f in fn:
        if not f.endswith(".py"):
            continue
        path = os.path.relpath(os.path.join(dp, f), REPO)
        src = open(os.path.join(REPO, path)).read()
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        for (cp, name), st in _class_dicts(tree).items():
            if name in dk["unsafe"]:
                continue
            for k, v in zip(st.value.keys, st.value.values):
                lb = _key_label(k) if k is not None else None
                if lb is None or lb in dk["unsafe_keys"].get(name, ()):
                    continue
                for c in ast.walk(v):
                    if isinstance(c, ast.Constant) and c.lineno == c.end_lineno and c.value is not None and not isinstance(c.value, bytes):
                        sites.append((path, c.lineno, c.col_offset, c.end_col_offset, c.value, name, lb))
                        break
rng.shuffle(sites)
os.makedirs(os.path.join(OUT, "commits"), exist_ok=True)
made = 0
for path, ln, c0, c1, val, name, lb in sites:
    if made >= N:
        break
    if isinstance(val, bool):
        new = repr(not val)
    elif isinstance(val, int):
        new = str(val + 1)
    elif isinstance(val, float):
        new = repr(val + 1.0)
    elif isinstance(val, str):
        new = repr(val + "x" if val else "x")
    else:
        continue
    lines = open(os.path.join(REPO, path)).read().split("\n")
    old = lines[ln - 1]
    lines2 = list(lines)
    lines2[ln - 1] = old[:c0] + new + old[c1:]
    diff = difflib.unified_diff([l + "\n" for l in lines], [l + "\n" for l in lines2], f"a/{path}", f"b/{path}", n=3)
    text = f"diff --git a/{path} b/{path}\n" + "".join(diff)
    cid = f"d{SEED}_{made:03d}"
    open(os.path.join(OUT, "commits", cid + ".patch"), "w").write(text)
    made += 1
    print(cid, path, ln, name, lb, repr(val), "->", new)
