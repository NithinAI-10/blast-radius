import json,sys,re
sys.path.insert(0,'/home/claude/entry')
from selector import partition
d,outf,errf=sys.argv[1:4]
tests=json.load(open('/home/claude/kit/sample/tests.json'))['tests']
t={x['number']:round(x['seconds']*1000) for x in tests}
a=json.load(open(f'{d}/answers.json'))['commits']
out={json.loads(l)['commit']:json.loads(l)['workers'] for l in open(outf)}
err=open(errf).read()
rows=[];tw=0;tcpu=0
for c,v in a.items():
    oc=partition([(n,t[n]) for n in v['failing']])[0]/1000
    ws=out[c]; mc=(max(sum(t[n] for n in w) for w in ws)+2000*len(ws))/1000 if ws else 0
    extra=set(n for w in ws for n in w)-set(v['failing'])
    m=re.search(rf'^{c}: cand=.*$',err,re.M); cm=re.search(rf'^{c}: cpu=([\d.]+)',err,re.M)
    cpu=float(cm.group(1)) if cm else 0
    rows.append((mc-oc+cpu,c,oc,mc,cpu,[tests[n]['id'].split('::')[-1]+f'({t[n]/1000:.1f})' for n in sorted(extra,key=lambda n:-t[n])[:3]],m.group(0)[:160] if m else ''))
for r in sorted(rows,reverse=True)[:int(sys.argv[4]) if len(sys.argv)>4 else 8]: print(f"waste+cpu={r[0]:6.2f} {r[1]} oracle={r[2]:.2f} mine={r[3]:.2f} cpu={r[4]:.2f} extra={r[5]}\n      {r[6]}")
