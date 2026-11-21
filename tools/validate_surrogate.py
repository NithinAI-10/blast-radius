import json, os, sys, glob, time
sys.path.insert(0, "/home/claude/entry")
import runner, analyze
BASE = "/home/claude/sqlglot"
G = [("tests.test_parser", "TestParser", [(1490, "test_max_nodes")])]


def run(files, real):
    if real:
        os.environ["BR_NO_SURROGATE"] = "1"
    else:
        os.environ.pop("BR_NO_SURROGATE", None)
    pid, fd, root = runner.start(BASE, "/tmp/vs/r", files, G, lambda n: 120)
    os.set_blocking(fd, True)
    b = b""
    while True:
        c = os.read(fd, 4096)
        if not c:
            break
        b += c
    _, _, ru = os.wait4(pid, 0)
    os.system("rm -rf " + root)
    return b.decode().strip(), ru.ru_utime + ru.ru_stime


os.makedirs("/tmp/vs", exist_ok=True)
for p in sorted(glob.glob(sys.argv[1] + "/*.patch")):
    files = {}
    for fc in analyze.parse_patch(open(p).read()):
        old = open(os.path.join(BASE, fc.old_path)).read()
        files[fc.path] = analyze.apply_change(old, fc)[0]
    s, cs = run(files, False)
    r, cr = run(files, True)
    flag = "AGREE" if s.split()[-1:] == r.split()[-1:] else ("SAFE" if s.endswith("F") else "MISS!!")
    print(os.path.basename(p), "surrogate", s, round(cs, 1), "real", r, round(cr, 1), flag, flush=True)
