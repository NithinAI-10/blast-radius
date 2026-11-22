"""Lines of sqlglot/ that run while sqlglot and all its submodules are imported (module
and class bodies, and functions they call such as metaclasses). Code here runs before
trace_dictkeys.py swaps in its recording dicts, so the selector only narrows a changed
table entry when every read of that table in these lines is key-only.
Usage: python trace_import_lines.py REPO OUT_JSON"""
import importlib, json, os, pkgutil, sys
REPO, OUT = os.path.realpath(sys.argv[1]), sys.argv[2]
sys.path.insert(0, REPO); os.chdir(REPO)
PREF = os.path.join(REPO, "sqlglot") + os.sep
PL = len(REPO) + 1
lines = {}
mon = sys.monitoring
mon.use_tool_id(mon.PROFILER_ID, "imp")


def on_line(code, line):
    fn = code.co_filename
    if fn.startswith(PREF):
        lines.setdefault(fn[PL:], set()).add(line)
    return mon.DISABLE


mon.register_callback(mon.PROFILER_ID, mon.events.LINE, on_line)
mon.set_events(mon.PROFILER_ID, mon.events.LINE)
import sqlglot
for m in pkgutil.walk_packages(sqlglot.__path__, "sqlglot."):
    try:
        importlib.import_module(m.name)
    except Exception:
        pass
mon.set_events(mon.PROFILER_ID, 0)
print("files", len(lines), "lines", sum(len(v) for v in lines.values()))
json.dump({k: sorted(v) for k, v in lines.items()}, open(OUT, "w"))
