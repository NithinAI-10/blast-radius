"""Fold map/attrs/*.json (tools/trace_attrs.py) into the map entry "attrs":
  read   : {"module:Qualname.NAME": test bits}  tests that read that class's own value
  keys   : instrumented attributes; no other attribute is ever narrowed
  unsafe : keys read while sqlglot imports (metaclasses, __init_subclass__, constants
           built at import), or used by bare name in their own class body (that read
           happens before the descriptor exists)
  failed : test bits whose outcome changed under the trace
Usage: imported by build_map.py"""
import ast, glob, json, os


def _module(path):
    m = path[:-3].replace("/", ".")
    return m[:-9] if m.endswith(".__init__") else m


def in_body_keys(repo):
    out = set()
    for dp, dn, fn in os.walk(os.path.join(repo, "sqlglot")):
        for f in fn:
            if not f.endswith(".py"):
                continue
            path = os.path.relpath(os.path.join(dp, f), repo)
            try:
                tree = ast.parse(open(os.path.join(repo, path)).read())
            except SyntaxError:
                continue
            stack = [((), n) for n in tree.body if isinstance(n, ast.ClassDef)]
            for top in ast.walk(tree):  # classes nested in functions too
                if isinstance(top, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    stack.extend(((top.name, "<locals>"), n) for n in top.body if isinstance(n, ast.ClassDef))
            while stack:
                pre, cls = stack.pop()
                qual = pre + (cls.name,)
                names = set()
                for st in cls.body:
                    if isinstance(st, ast.ClassDef):
                        stack.append((qual, st))
                    elif isinstance(st, (ast.Assign, ast.AnnAssign)):
                        for t in (st.targets if isinstance(st, ast.Assign) else [st.target]):
                            if isinstance(t, ast.Name):
                                names.add(t.id)
                for st in cls.body:
                    if isinstance(st, ast.ClassDef):
                        continue
                    for n in ast.walk(st):
                        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id in names:
                            out.add(f"{_module(path)}:{'.'.join(qual)}.{n.id}")
    return out


def build(repo, d, num_to_idx):
    meta = json.load(open(os.path.join(d, "_meta.json")))
    read, failed = {}, 0
    for f in glob.glob(os.path.join(d, "[0-9]*.json")):
        t = json.load(open(f))
        i = num_to_idx.get(t["number"])
        if i is None:
            continue
        bit = 1 << i
        if t["outcome"] != "passed":
            failed |= bit
        for k in t["read"]:
            read[k] = read.get(k, 0) | bit
    return {"read": read, "keys": set(meta["keys"]), "unsafe": set(meta["import"]) | in_body_keys(repo), "failed": failed}
