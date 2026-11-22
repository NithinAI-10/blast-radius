"""Mutants that change one class-level NAME = <constant> in sqlglot (bool flipped,
number + 1, string extended). Usage: python gen_attr_mutants.py REPO OUT_DIR N SEED"""
import ast, difflib, os, random, sys
REPO, OUT, N, SEED = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
rng = random.Random(SEED)
sites = []
for dp, dn, fn in os.walk(os.path.join(REPO, "sqlglot")):
    for f in fn:
        if not f.endswith(".py"):
            continue
        path = os.path.relpath(os.path.join(dp, f), REPO)
        tree = ast.parse(open(os.path.join(REPO, path)).read())
        for cls in ast.walk(tree):
            if not isinstance(cls, ast.ClassDef):
                continue
            for st in cls.body:
                if isinstance(st, (ast.Assign, ast.AnnAssign)) and isinstance(st.value, ast.Constant):
                    c = st.value
                    tg = st.targets if isinstance(st, ast.Assign) else [st.target]
                    if len(tg) == 1 and isinstance(tg[0], ast.Name) and not tg[0].id.startswith("__") and c.lineno == c.end_lineno and c.value is not None and not isinstance(c.value, bytes):
                        sites.append((path, c.lineno, c.col_offset, c.end_col_offset, c.value, tg[0].id))
rng.shuffle(sites)
os.makedirs(os.path.join(OUT, "commits"), exist_ok=True)
made = 0
for path, ln, c0, c1, val, name in sites:
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
    l2 = list(lines)
    l2[ln - 1] = lines[ln - 1][:c0] + new + lines[ln - 1][c1:]
    diff = difflib.unified_diff([l + "\n" for l in lines], [l + "\n" for l in l2], f"a/{path}", f"b/{path}", n=3)
    cid = f"a{SEED}_{made:03d}"
    open(os.path.join(OUT, "commits", cid + ".patch"), "w").write(f"diff --git a/{path} b/{path}\n" + "".join(diff))
    made += 1
    print(cid, path, ln, name, repr(val), "->", new)
