"""For each labeled commit where test_execute_tpcds is a candidate: whether the wider
TPC-DS shortcut applies, whether optimize/transpile must be re-run, and whether the test
failed. Run on the 120 development mutants to get the shortcut prior.
Usage: python tools/tpcds_prior.py selector EVAL_DIR [EVAL_DIR ...]"""
import sys, os, json, pickle, zlib
sys.path.insert(0, sys.argv[1])
import analyze
M = pickle.loads(zlib.decompress(open(os.path.join(sys.argv[1],'map.bin'),'rb').read()))
sc = M["tpcds_sc2"]
TP = 'tests/test_executor.py::TestExecutor::test_execute_tpcds'
def read_orig(p):
    try: return open(os.path.join('/home/claude/sqlglot',p)).read()
    except OSError: return None
def list_py():
    out=[]
    for top in ('sqlglot','tests'):
        for dp,dn,fn in os.walk(os.path.join('/home/claude/sqlglot',top)):
            out += [os.path.relpath(os.path.join(dp,f),'/home/claude/sqlglot') for f in fn if f.endswith('.py')]
    return sorted(out)
refs = analyze.RefIndex(M, read_orig, list_py)
idx = {t[1]: t[0] for t in M['tests']}[TP]
def hits(ctx, lines):
    defs={(p,s.start,s.stop) for p,s in ctx.def_spans}
    for p,s in list(ctx.spans)+list(ctx.read_spans):
        if (p,s.start,s.stop) in defs: continue
        ls=lines.get(p,())
        if any(l in ls for l in s): return True
    return False
for d in sys.argv[2:]:
    tests={t['number']:t['id'] for t in json.load(open(f'{d}/tests.json'))['tests']}
    tpn=[n for n,i in tests.items() if i==TP][0]
    ans=json.load(open(f'{d}/answers.json'))['commits']
    for c in sorted(ans):
        ctx = analyze.analyze_patch(M, read_orig, open(f'{d}/commits/{c}.patch').read(), refs)
        if not (ctx.bits >> idx) & 1: continue
        files = getattr(ctx,'files',{})
        ok = not ctx.coarse and all(f.startswith('sqlglot/') and f.endswith('.py') for f in files) and not hits(ctx, sc['resid'])
        print(d.split('/')[-1], c, 'elig' if ok else 'full', 'O' if ok and hits(ctx, sc['O']) else '-', 'T' if ok and hits(ctx, sc['T']) else '-', 'FAIL' if tpn in ans[c]['failing'] else 'pass', ctx.why[0][:70])
