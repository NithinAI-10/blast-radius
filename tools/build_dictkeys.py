"""Fold map/dictkeys/*.json (tools/trace_dictkeys.py) into the map entry "dk":
  keys   : {name: {key label: test bits}}  runtime readers of that entry
  whole  : {name: test bits}                runtime readers of every value
  unsafe_keys : {name: {labels}}            read while importing other than by a
                                            same-name {**Parent.NAME, ...} copy
  unsafe : {names}                          aliased, or every value read while importing
  failed : test bits                        tests whose outcome changed under the trace
Bits use the map's test indices. Usage: imported by build_map.py, or
python build_dictkeys.py REPO DICTKEYS_DIR TESTS_JSON OUT_PICKLE"""
import ast, glob, json, os, pickle, sys


def _same_name_copy(repo, where, name, cache={}):
    path, _, line = where.rpartition(":")
    if not path or not line.isdigit():
        return False
    line = int(line)
    try:
        tree = cache.get(path) or cache.setdefault(path, ast.parse(open(os.path.join(repo, path)).read()))
    except (OSError, SyntaxError):
        return False
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.lineno <= line <= node.end_lineno:
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if not (len(targets) == 1 and isinstance(targets[0], ast.Name) and targets[0].id == name):
                return False
            if not isinstance(node.value, ast.Dict):
                return False
            # the copy is reported at some line of the statement: accept it only if the
            # name appears in the value solely as **NAME / **Parent.NAME elements
            copies, others = 0, 0
            for k, v in zip(node.value.keys, node.value.values):
                is_copy = k is None and ((isinstance(v, ast.Attribute) and v.attr == name) or (isinstance(v, ast.Name) and v.id == name))
                for sub in ast.walk(v) if not is_copy else ():
                    if (isinstance(sub, ast.Attribute) and sub.attr == name) or (isinstance(sub, ast.Name) and sub.id == name):
                        others += 1
                if k is not None:
                    for sub in ast.walk(k):
                        if (isinstance(sub, ast.Attribute) and sub.attr == name) or (isinstance(sub, ast.Name) and sub.id == name):
                            others += 1
                copies += is_copy
            return copies > 0 and others == 0
    return False


def _key_only(n, parent):
    """True if this use of a dict can see only its keys: `x in D`, iterating D, D.keys()."""
    p = parent.get(n)
    if isinstance(p, ast.Compare) and n in p.comparators and all(isinstance(o, (ast.In, ast.NotIn)) for o in p.ops):
        return True
    if isinstance(p, ast.comprehension) and p.iter is n:
        return True
    if isinstance(p, (ast.For, ast.AsyncFor)) and p.iter is n:
        return True
    if isinstance(p, ast.Attribute) and p.value is n and p.attr == "keys" and isinstance(parent.get(p), ast.Call):
        return True
    return False


def in_body_names(repo):
    """Names of class-level dicts that their own class body also uses as a bare name
    (e.g. TYPE_MAPPING = {**STRING_TYPE_MAPPING}): the class body reads the original
    dict before the tracer swaps it, so those reads are never recorded."""
    out = set()
    for dp, dn, fn in os.walk(os.path.join(repo, "sqlglot")):
        for f in fn:
            if not f.endswith(".py"):
                continue
            try:
                tree = ast.parse(open(os.path.join(dp, f)).read())
            except (OSError, SyntaxError):
                continue
            for cls in ast.walk(tree):
                if not isinstance(cls, ast.ClassDef):
                    continue
                names = set()
                for st in cls.body:
                    if isinstance(st, (ast.Assign, ast.AnnAssign)) and isinstance(st.value, ast.Dict):
                        for t in (st.targets if isinstance(st, ast.Assign) else [st.target]):
                            if isinstance(t, ast.Name):
                                names.add(t.id)
                if not names:
                    continue
                for st in cls.body:
                    parent = {}
                    for a in ast.walk(st):
                        for c in ast.iter_child_nodes(a):
                            parent[c] = a
                    for n in ast.walk(st):
                        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id in names:
                            if not _key_only(n, parent):
                                out.add(n.id)
    return out


def build(repo, d, num_to_idx):
    meta = json.load(open(os.path.join(d, "_meta.json")))
    keys, whole, failed = {}, {}, 0
    for f in glob.glob(os.path.join(d, "[0-9]*.json")):
        t = json.load(open(f))
        i = num_to_idx.get(t["number"])
        if i is None:
            continue
        bit = 1 << i
        if t["outcome"] != "passed":
            failed |= bit
        for name, ks in t["read"].items():
            dn = keys.setdefault(name, {})
            for k in ks:
                dn[k] = dn.get(k, 0) | bit
        for name in t["whole"]:
            whole[name] = whole.get(name, 0) | bit
    unsafe = set(meta["aliased"]) | in_body_names(repo)
    unsafe_keys = {}
    for name, k, locs in meta["import"]:
        if all(_same_name_copy(repo, w, name) for w in locs):
            continue
        if k == "*":
            unsafe.add(name)
        else:
            unsafe_keys.setdefault(name, set()).add(k)
    return {"keys": keys, "whole": whole, "unsafe_keys": unsafe_keys, "unsafe": unsafe, "failed": failed}


if __name__ == "__main__":
    repo, d, tj, out = sys.argv[1:5]
    tests = json.load(open(tj))["tests"]
    dk = build(repo, d, {t["number"]: t["number"] for t in tests})
    pickle.dump(dk, open(out, "wb"))
    print("names", len(dk["keys"]), "unsafe", len(dk["unsafe"]), "unsafe_keys", sum(len(v) for v in dk["unsafe_keys"].values()))
