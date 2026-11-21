import sys,json,pickle,zlib,glob,os
sys.path.insert(0,'/home/claude/entry')
import analyze
M=pickle.loads(zlib.decompress(open('/home/claude/entry/map.bin','rb').read()))
tests=json.load(open('/home/claude/kit/sample/tests.json'))['tests']
d={t['number']:t['seconds'] for t in tests}
ans=json.load(open(sys.argv[1]))['commits']
cdir=sys.argv[2]
R='/home/claude/sqlglot'
def rd(p):
    try: return open(os.path.join(R,p),encoding='utf-8',newline='').read()
    except FileNotFoundError: return None
miss=0;tot=0
def list_py():
    out=[]
    for top in ('sqlglot','tests'):
        for dp,dn,fn in os.walk(os.path.join(R,top)):
            dn[:]=[d for d in dn if d!='__pycache__']
            out+= [os.path.relpath(os.path.join(dp,f),R) for f in fn if f.endswith('.py')]
    return sorted(out)
refs=analyze.RefIndex(M,rd,list_py)
for cid,a in sorted(ans.items()):
    ctx=analyze.analyze_patch(M,rd,open(f'{cdir}/{cid}.patch').read(),refs)
    c={i for i in range(len(tests)) if ctx.bits>>i&1}
    f=set(a['failing'])
    m=f-c
    s=sum(d[i] for i in c)
    tot+=s
    if m: miss+=1
    print(f"{cid} {a['kind']:8s} fail={len(f):3d} cand={len(c):4d} cand_s={s:7.2f} {'MISS '+str([tests[i]['id'] for i in m][:3]) if m else ''} | {'; '.join(ctx.why)[:150]}")
print('misses',miss,'total cand seconds',round(tot,1))
