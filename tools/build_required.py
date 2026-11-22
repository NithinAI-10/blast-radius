"""Fold map/required/*.json (tools/trace_required.py) into the map entry "argtypes":
  missing : {"Class.arg": test bits}  tests that validated Class while arg was missing
  derived : {class name: [class names]}  classes whose arg_types values come from it:
            itself, subclasses that inherit its arg_types, and classes whose arg_types
            literal copies it with **X.arg_types (transitively)
  failed  : test bits whose outcome changed under the trace
  ok      : False unless arg_types values are read only to build required_args, and
            required_args only in Expression.error_messages (checked on the source)
Usage: imported by build_map.py"""
import ast, glob, importlib, json, os, pkgutil, re, sys


def _source_ok(repo):
    reads = []
    for dp, dn, fn in os.walk(os.path.join(repo, "sqlglot")):
        for f in fn:
            if f.endswith(".py"):
                src = open(os.path.join(dp, f)).read()
                for m in re.finditer(r"arg_types\s*\[|arg_types\.(items|values|get|copy|pop|setdefault)\b|required_args", src):
                    line = src.count("\n", 0, m.start()) + 1
                    reads.append((os.path.relpath(os.path.join(dp, f), repo), line, src.splitlines()[line - 1].strip()))
    allowed = {
        "cls.required_args = {k for k, v in cls.arg_types.items() if v}",
        "required_args: t.ClassVar[set[str]] = {\"this\"}",
        "for k in self.required_args:",
    }
    return all(r[2] in allowed for r in reads), reads


def _copies(repo):
    """{class name: {names X it copies via **X.arg_types}}"""
    out = {}
    for dp, dn, fn in os.walk(os.path.join(repo, "sqlglot")):
        for f in fn:
            if not f.endswith(".py"):
                continue
            tree = ast.parse(open(os.path.join(dp, f)).read())
            for cls in ast.walk(tree):
                if not isinstance(cls, ast.ClassDef):
                    continue
                for st in cls.body:
                    if isinstance(st, (ast.Assign, ast.AnnAssign)):
                        tg = st.targets if isinstance(st, ast.Assign) else [st.target]
                        if any(isinstance(t, ast.Name) and t.id == "arg_types" for t in tg) and st.value is not None:
                            for n in ast.walk(st.value):
                                if isinstance(n, ast.Attribute) and n.attr == "arg_types":
                                    v = n.value
                                    nm = v.attr if isinstance(v, ast.Attribute) else v.id if isinstance(v, ast.Name) else None
                                    out.setdefault(cls.name, set()).add(nm or "*")
    return out


def derived(repo):
    sys.path.insert(0, repo)
    import sqlglot
    from sqlglot.expressions.core import Expression
    for m in pkgutil.walk_packages(sqlglot.__path__, "sqlglot."):
        try:
            importlib.import_module(m.name)
        except Exception:
            pass
    classes, stack = [], [Expression]
    while stack:
        c = stack.pop()
        classes.append(c)
        stack.extend(c.__subclasses__())
    copies = _copies(repo)
    # sources[D] = names of classes whose literal values D's arg_types may hold
    src = {}
    for c in classes:
        s = set()
        for b in c.__mro__:
            if "arg_types" in b.__dict__:
                s.add(b.__name__)
                s |= copies.get(b.__name__, set())
                break
        src.setdefault(c.__name__, set()).update(s)
    changed = True
    while changed:  # copies of copies
        changed = False
        for d, s in src.items():
            extra = set()
            for x in s:
                extra |= src.get(x, set())
            if not extra <= s:
                s |= extra
                changed = True
    out = {}
    for d, s in src.items():
        for x in s:
            out.setdefault(x, set()).add(d)
    if any("*" in s for s in src.values()):
        out["*"] = set(src)
    return {k: sorted(v) for k, v in out.items()}


def build(repo, d, num_to_idx):
    ok, reads = _source_ok(repo)
    missing, failed = {}, 0
    for f in glob.glob(os.path.join(d, "[0-9]*.json")):
        t = json.load(open(f))
        i = num_to_idx.get(t["number"])
        if i is None:
            continue
        bit = 1 << i
        if t["outcome"] != "passed" or "*.*" in t["missing"]:
            failed |= bit
        for p in t["missing"]:
            missing[p] = missing.get(p, 0) | bit
    return {"missing": missing, "derived": derived(repo), "failed": failed, "ok": ok}
