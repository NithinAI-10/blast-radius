import sys,json,pickle,zlib,os,collections
sys.path.insert(0,'/home/claude/entry')
import analyze
M=pickle.loads(zlib.decompress(open('/home/claude/entry/map.bin','rb').read()))
tests=json.load(open('/home/claude/kit/sample/tests.json'))['tests']
R='/home/claude/sqlglot'
def rd(p):
    try: return open(os.path.join(R,p),encoding='utf-8',newline='').read()
    except Exception: return None
def list_py():
    out=[]
    for top in ('sqlglot','tests'):
        for dp,dn,fn in os.walk(os.path.join(R,top)):
            dn[:]=[d for d in dn if d!='__pycache__']
            out+=[os.path.relpath(os.path.join(dp,f),R) for f in fn if f.endswith('.py')]
    return sorted(out)
refs=analyze.RefIndex(M,rd,list_py)
SK={x['number'] for x in tests if x['id'].split('::')[-1] in ('test_max_nodes','test_execute_tpcds','test_generator_memory')}
sets=[] if __import__('os').environ.get('NO_SAMPLE') else [('/home/claude/kit/sample/answers.json','/home/claude/kit/sample/commits')]
if len(sys.argv)>1: sets.append((sys.argv[1],sys.argv[2]))
nc=collections.Counter(); nf=collections.Counter(); commits_any=0; commits=0
for ansf,cdir in sets:
    ans=json.load(open(ansf))['commits']
    for c,v in ans.items():
        ctx=analyze.analyze_patch(M,rd,open(f'{cdir}/{c}.patch').read(),refs)
        cand=[i for i in range(len(tests)) if ctx.bits>>i&1 and i not in SK]
        f=set(v['failing'])
        for i in cand:
            nc[i]+=1; nf[i]+= i in f
        commits+=1; commits_any+= bool(cand)
tot_c=sum(nc.values()); tot_f=sum(nf.values())
print('commits',commits,'cand-pairs',tot_c,'fails',tot_f,'rate',round(tot_f/tot_c,3))
heavy=[i for i in nc if tests[i]['seconds']>=0.3]
hc=sum(nc[i] for i in heavy); hf=sum(nf[i] for i in heavy)
print('heavy (>=0.3s) cand',hc,'fails',hf,'rate',round(hf/max(hc,1),3))
for i in sorted(heavy,key=lambda i:-nc[i])[:12]: print(nc[i],nf[i],tests[i]['id'].split('::')[-1])
K=4
pf={}
for i in range(len(tests)):
    p0=0.35 if tests[i]['seconds']>=0.3 else 0.06
    if tests[i]['id'].endswith('test_max_nodes'): p0=0.05
    if tests[i]['id'].endswith('test_generator_memory'): p0=0.05
    if tests[i]['id'].endswith('test_execute_tpcds'): p0=0.4
    pf[i]=round((nf[i]+K*p0)/(nc[i]+K),4)
json.dump(pf,open('/home/claude/map/pfail.json','w'))
print('wrote pfail')
