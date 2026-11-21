"""Generate sample-like mutant patches against the pinned sqlglot.

Usage: python gen_mutants.py OUT_DIR COUNT SEED
"""

import difflib
import glob
import os
import random
import re
import sys

R = "/home/claude/sqlglot"
out, count, seed = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
rng = random.Random(seed)
os.makedirs(out, exist_ok=True)

py_files = [p for p in glob.glob(f"{R}/sqlglot/**/*.py", recursive=True) if "__pycache__" not in p]
# weight like the sample: dialect-specific modules are common
fixtures = glob.glob(f"{R}/tests/fixtures/optimizer/*.sql") + glob.glob(f"{R}/tests/fixtures/*.sql")

OPS = [
    (r"\band\b", "or"),
    (r"\bor\b", "and"),
    (r"\bis not\b", "!="),
    (r"\bis\b(?! not)", "=="),
    (r"\bif not\b", "if"),
    (r"\bTrue\b", "False"),
    (r"\bFalse\b", "True"),
    (r"(?<![=!<>])==(?!=)", "!="),
    (r"!=", "=="),
    (r"(?<![<>=])<(?![<=])", "<="),
    (r"(?<![<>=])>(?![>=])", ">="),
    (r"\b(\d+)\b", None),  # +1
]
COMMENTS = ["# Cheap check first", "# Keep in sync with the generator", "# Intentionally not cached", "# TODO: revisit", "# Fast path"]


def mutate_line(line):
    code = line.split("#", 1)[0]
    if not code.strip() or code.strip().startswith(("def ", "class ", "import ", "from ", "@", '"""', "'")):
        return None
    choices = []
    for pat, rep in OPS:
        for m in re.finditer(pat, code):
            # skip matches inside string literals (rough)
            before = code[: m.start()]
            if before.count('"') % 2 or before.count("'") % 2:
                continue
            choices.append((m, rep))
    if not choices:
        return None
    m, rep = rng.choice(choices)
    if rep is None:
        rep = str(int(m.group(1)) + rng.choice([1, -1, 1]))
        if rep == "-1" or rep.startswith("-"):
            rep = str(int(m.group(1)) + 1)
    return line[: m.start()] + rep + line[m.end():]


def make_patch(rel, old, new):
    d = difflib.unified_diff(old.splitlines(keepends=True), new.splitlines(keepends=True), f"a/{rel}", f"b/{rel}", n=3)
    s = "".join(d)
    return f"diff --git a/{rel} b/{rel}\n" + s.replace("--- a/", "--- a/", 1)


def code_mutant():
    for _ in range(1000):
        p = rng.choice(py_files)
        src = open(p).read()
        lines = src.split("\n")
        i = rng.randrange(len(lines))
        new = mutate_line(lines[i])
        if new is None or new == lines[i]:
            continue
        lines2 = lines[:]
        lines2[i] = new
        rel = os.path.relpath(p, R)
        return "code", [(rel, src, "\n".join(lines2))]
    raise RuntimeError


def comment_mutant(p=None):
    p = p or rng.choice(py_files)
    src = open(p).read()
    lines = src.split("\n")
    for _ in range(200):
        i = rng.randrange(1, len(lines))
        if lines[i].strip() and not lines[i].strip().startswith(("#", ")", "]", "}")):
            ind = re.match(r"\s*", lines[i]).group(0)
            lines2 = lines[:i] + [ind + rng.choice(COMMENTS)] + lines[i:]
            return "comment", [(os.path.relpath(p, R), src, "\n".join(lines2))]
    return comment_mutant()


def fixture_mutant():
    p = rng.choice(fixtures)
    src = open(p).read()
    lines = src.split("\n")
    idx = [i for i, l in enumerate(lines) if l.strip() and not l.startswith("#") and "(" in l]
    if not idx:
        return fixture_mutant()
    i = rng.choice(idx)
    l = lines[i]
    k = l.index("(")
    variants = [l[:k] + "() 1" + l[k + 2:] if l[k + 1:k + 2] == ")" else l[:k] + "(1, " + l[k + 1:], l.replace(" AS ", " AS x_", 1), l.replace("SELECT", "SELECT DISTINCT", 1)]
    new = rng.choice([v for v in variants if v != l] or [l + " "])
    lines[i] = new
    return "fixture", [(os.path.relpath(p, R), src, "\n".join(lines))]


for n in range(count):
    r = rng.random()
    if r < 0.55:
        kind, ch = code_mutant()
    elif r < 0.7:
        kind, ch = comment_mutant()
    elif r < 0.9:
        kind, ch = fixture_mutant()
    else:
        kind = "multi"
        _, a = comment_mutant()
        _, b = rng.choice([code_mutant, fixture_mutant])()
        ch = a + b
        if a[0][0] == b[0][0]:
            ch = b
    text = "".join(make_patch(rel, o, nw) for rel, o, nw in ch)
    cid = f"m{seed}_{n:03d}"
    open(f"{out}/{cid}.patch", "w").write(text)
    open(f"{out}/{cid}.kind", "w").write(kind)
print("ok", file=sys.stderr)
