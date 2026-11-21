"""Restrict which fixture items sqlglot's test helpers hand out.

install(restrict) wraps tests.helpers.load_sql_fixtures / load_sql_fixture_pairs.
restrict: {filename: [indices]} keeps only those items (0-based, in the order
the original loader yields them) for the named files; other files are untouched.
restrict=None leaves everything as is. Returns a dict that records, per
filename, how many items the original loader produced (for probing).
"""

import sys

NAMES = ("load_sql_fixtures", "load_sql_fixture_pairs")
_wrapped = {}


def install(restrict):
    import tests.helpers as h

    loads = {}
    for name in NAMES:
        orig = getattr(h, name)
        orig = getattr(orig, "__wrapped_orig__", orig)

        def make(orig):
            def loader(filename):
                items = list(orig(filename))
                loads[filename] = len(items)
                allowed = None if restrict is None else restrict.get(filename)
                if allowed is None:
                    yield from items
                else:
                    keep = set(allowed)
                    for i, it in enumerate(items):
                        if i in keep:
                            yield it

            loader.__wrapped_orig__ = orig
            return loader

        w = make(orig)
        _wrapped[name] = w
        setattr(h, name, w)
    for mod in list(sys.modules.values()):
        patch_module(mod)
    return loads


def patch_module(mod):
    name = getattr(mod, "__name__", "") or ""
    if not name.startswith("tests"):
        return
    for n, w in _wrapped.items():
        if getattr(mod, n, None) is not None and getattr(mod, n) is not w:
            setattr(mod, n, w)


# ---- pure re-implementations of tests/helpers.py loaders, for diffing a patched fixture file


def _filter_comments(s):
    return "\n".join([line for line in s.splitlines() if line and not line.startswith("--")])


def _extract_meta(sql):
    meta = {}
    sql_lines = sql.split("\n")
    i = 0
    while sql_lines[i].startswith("#"):
        key, val = sql_lines[i].split(":", maxsplit=1)
        meta[key.lstrip("#").strip()] = val.strip()
        i += 1
    return "\n".join(sql_lines[i:]), meta


def items_fixtures(text):
    out, lines = [], []
    for line in _filter_comments(text).splitlines():
        lines.append(line)
        if not line.startswith("#"):
            out.append("\n".join(lines))
            lines.clear()
    return out


def items_pairs(text):
    statements = _filter_comments(text).split(";")
    out = []
    for i in range(0, len(statements), 2):
        if i + 1 < len(statements):
            sql = statements[i].strip()
            sql, meta = _extract_meta(sql)
            out.append((meta, sql, statements[i + 1].strip()))
    return out


def changed_items_n(old_text, new_text, n):
    """Changed item indices under the reading that yields n items, else None."""
    try:
        for f in (items_pairs, items_fixtures):
            a = f(old_text)
            if len(a) != n:
                continue
            b = f(new_text)
            if len(b) != n:
                return None
            return {i for i, (x, y) in enumerate(zip(a, b)) if x != y}
    except Exception:
        return None
    return None


def changed_items(old_text, new_text):
    """Indices of items that differ, or None if the item lists cannot be aligned.

    Both loader flavours are checked; an item counts as changed if it differs
    under either reading (a file is read by one of them, we do not know which).
    """
    out = set()
    try:
        for f in (items_fixtures, items_pairs):
            a, b = f(old_text), f(new_text)
            if len(a) != len(b):
                return None
            out |= {i for i, (x, y) in enumerate(zip(a, b)) if x != y}
    except Exception:
        return None
    return out
