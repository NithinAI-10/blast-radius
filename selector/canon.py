"""Structural fingerprint of a sqlglot AST that does not rely on sqlglot's own
__eq__/__hash__ (which a patch might change): class names, arg keys and leaf
values, plus attached types, walked iteratively.
"""

import hashlib

_END_NODE = object()
_END_LIST = object()


class _Key:
    __slots__ = ("k",)

    def __init__(self, k):
        self.k = k


def canon(e) -> str:
    h = hashlib.sha1()
    up = h.update
    st = [e]
    while st:
        x = st.pop()
        if x is _END_NODE:
            up(b")")
            continue
        if x is _END_LIST:
            up(b"]")
            continue
        if type(x) is _Key:
            up(b"|" + str(x.k).encode() + b"=")
            continue
        args = getattr(x, "args", None)
        if isinstance(args, dict) and type(x).__module__.startswith("sqlglot"):
            up(type(x).__name__.encode() + b"(")
            st.append(_END_NODE)
            for k, v in reversed(list(args.items())):
                st.append(v)
                st.append(_Key(k))
            t = getattr(x, "_type", None)
            if t is not None:
                st.append(t)
                st.append(_Key("_type"))
        elif isinstance(x, list):
            up(b"[")
            st.append(_END_LIST)
            st.extend(reversed(x))
        else:
            up(type(x).__name__.encode() + b":" + repr(x).encode() + b",")
    return h.hexdigest()
