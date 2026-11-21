"""Re-run tests whose traced opens include .py files; keep only non-import reads."""
import json, os, pickle, sys, zlib, unittest
sys.path.insert(0, "/home/claude/entry")
import syncpool
syncpool.install()
REPO = "/home/claude/sqlglot"
os.environ["SKIP_INTEGRATION"] = "0"
for m in ("numpy", "pandas", "duckdb"):
    __import__(m)
M = pickle.loads(zlib.decompress(open("/home/claude/entry/map.bin", "rb").read()))
tests = M["tests"]
susp = set()
for f, b in M["opens"].items():
    if f.endswith(".py"):
        for i in range(len(tests)):
            if b >> i & 1:
                susp.add(i)
out = {}
for i in sorted(susp):
    tid = tests[i][1]
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(r)
        os.chdir(REPO); sys.path.insert(0, REPO)
        dn = os.open(os.devnull, os.O_WRONLY); os.dup2(dn, 1); os.dup2(dn, 2)
        seen = set()
        def hook(ev, args):
            if ev != "open":
                return
            p = args[0]
            if not isinstance(p, (str, bytes)):
                return
            p = os.path.realpath(os.fsdecode(p))
            if not p.startswith(REPO + "/") or not p.endswith(".py"):
                return
            f = sys._getframe(1)
            while f is not None:
                fn = f.f_code.co_filename
                if "importlib" in fn or fn.startswith("<frozen"):
                    return
                f = f.f_back
            seen.add(p[len(REPO) + 1:])
        sys.addaudithook(hook)
        path, cls, meth = tid.split("::")
        try:
            mod = __import__(path[:-3].replace("/", "."), fromlist=["_"])
            unittest.TestSuite([getattr(mod, cls)(meth)]).run(unittest.TestResult())
        except BaseException:
            pass
        os.write(w, json.dumps(sorted(seen)).encode()); os._exit(0)
    os.close(w)
    data = b""
    while True:
        c = os.read(r, 65536)
        if not c: break
        data += c
    os.waitpid(pid, 0)
    out[i] = json.loads(data or b"[]")
    if out[i]:
        print(tid, out[i][:4], flush=True)
json.dump(out, open("/home/claude/map/py_opens.json", "w"))
print("checked", len(susp))
