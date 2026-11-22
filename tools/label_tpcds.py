import json, os, sys
sys.path.insert(0, "/home/claude/entry")
import runner, analyze
BASE = "/home/claude/sqlglot"
G = [("tests.test_executor", "TestExecutor", [(1, "test_execute_tpcds")])]
out = {}
for p in sys.argv[2:]:
    files = {}
    for fc in analyze.parse_patch(open(p).read()):
        old = open(os.path.join(BASE, fc.old_path)).read() if fc.old_path else None
        files[fc.path] = analyze.apply_change(old, fc)[0] if fc.new_path else None
    pid, fd, root = runner.start(BASE, f"/tmp/lt/{os.getpid()}", files, G, lambda n: 300)
    os.set_blocking(fd, True); b = b""
    while True:
        c = os.read(fd, 4096)
        if not c: break
        b += c
    os.waitpid(pid, 0); os.system("rm -rf " + root)
    out[os.path.basename(p)[:-6]] = b.decode().strip()
    print(os.path.basename(p), b.decode().strip(), flush=True)
json.dump(out, open(sys.argv[1], "w"))
