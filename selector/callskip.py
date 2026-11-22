"""Run only some of a Validator test's checks.

Checks are counted in the order the test makes them; a check whose index is not kept is
skipped. A skipped validate_identity/validate_transpile returns a stand-in that runs the
real check the moment anything touches it (attribute, comparison, str, isinstance), so
a test that uses a result still gets the real one, checked. A skipped validate_all
returns None, as validate_all always does.
"""

_METHODS = ("validate_identity", "validate_all", "validate_transpile")


class State:
    def __init__(self, keep):
        self.keep = keep  # set of check indices, or None for all
        self.count = 0
        self.depth = 0  # > 0 while a check (or a forced stand-in) is running


class _Lazy:
    __slots__ = ("_thunk", "_val", "_done")

    def __init__(self, thunk):
        object.__setattr__(self, "_thunk", thunk)
        object.__setattr__(self, "_done", False)

    def _force(self):
        if not object.__getattribute__(self, "_done"):
            object.__setattr__(self, "_val", object.__getattribute__(self, "_thunk")())
            object.__setattr__(self, "_done", True)
        return object.__getattribute__(self, "_val")

    def __getattr__(self, name):
        return getattr(self._force(), name)

    @property
    def __class__(self):
        return type(self._force())

    def __eq__(self, o):
        return self._force() == o

    def __ne__(self, o):
        return self._force() != o

    def __hash__(self):
        return hash(self._force())

    def __str__(self):
        return str(self._force())

    def __repr__(self):
        return repr(self._force())

    def __bool__(self):
        return bool(self._force())

    def __iter__(self):
        return iter(self._force())

    def __len__(self):
        return len(self._force())

    def __contains__(self, x):
        return x in self._force()

    def __getitem__(self, k):
        return self._force()[k]

    def __call__(self, *a, **kw):
        return self._force()(*a, **kw)


def install(cls, state):
    """Patch the check methods as seen from cls (a Validator subclass)."""
    for name in _METHODS:
        orig = getattr(cls, name, None)
        if orig is None or getattr(orig, "_callskip", False):
            continue

        def make(orig, name):
            def wrapper(self, *a, **kw):
                i = state.count
                state.count += 1

                def real():
                    state.depth += 1
                    try:
                        return orig(self, *a, **kw)
                    finally:
                        state.depth -= 1

                if state.keep is None or i in state.keep:
                    return real()
                if name == "validate_all":
                    return None
                return _Lazy(real)

            wrapper._callskip = True
            return wrapper

        setattr(cls, name, make(orig, name))
