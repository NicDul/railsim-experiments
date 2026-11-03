"""Check actual full-period resource feasibility of a window-model witness.

These positive/negative checks concern a single fixed cyclic variant order;
they do not replace the global window exclusion proof.
"""
from pathlib import Path
from collections import defaultdict,Counter
import argparse,json,itertools,time
from ortools.sat.python import cp_model

ROOT=Path(__file__).resolve().parent

def spell(chain):
    edges=defaultdict(list)
    for key,z in sorted(chain.items()):
        w=tuple(key.split('>'))
        for _ in range(z):edges[w[:-1]].append(w[1:])
    stack=[min(edges)];path=[]
    while stack:
        u=stack[-1]
        if edges[u]:stack.append(edges[u].pop())
        else:path.append(stack.pop())
    path.reverse();assert path[0]==path[-1]
    order=[u[0] for u in path[:-1]]
    k=len(path[0])+1
    assert Counter(tuple(order[(i+j)%len(order)] for j in range(k)) for i in range(len(order)))==Counter({tuple(key.split('>')):z for key,z in chain.items()})
    return order

def check(input_file,seconds=30):
    data=json.loads(input_file.read_text());order=spell(data['chain']);W=json.loads((ROOT/(data['mode']+'-profiles.json')).read_text());m=cp_model.CpModel();t=[m.new_int_var(0,1799,'') for _ in order];m.add(t[0]==0)
    for i in range(len(order)-1):m.add(t[i+1]>=t[i]+1)
    for i,j in itertools.combinations(range(len(order)),2):
        for r in sorted(set(W[order[i]])&set(W[order[j]])):
            a,b=W[order[i]][r];c,d=W[order[j]][r];wrap=m.new_int_var(-1,1,'');delta=t[j]-t[i]+1800*wrap
            m.add(delta>=b-c);m.add(delta<=1800-(d-a))
    s=cp_model.CpSolver();s.parameters.num_search_workers=2;s.parameters.max_time_in_seconds=seconds
    started=time.monotonic();status=s.solve(m);entries=None
    if status in (cp_model.OPTIMAL,cp_model.FEASIBLE):
        entries=[s.value(x) for x in t]
        for i,j in itertools.combinations(range(len(order)),2):
            for shift in [-1800,0,1800]:
                for r in set(W[order[i]])&set(W[order[j]]):
                    a,b=W[order[i]][r];c,d=W[order[j]][r]
                    assert max(entries[i]+a,entries[j]+shift+c)>=min(entries[i]+b,entries[j]+shift+d)
    result=dict(source=input_file.name,mode=data['mode'],rate=data['rate'],k=data['k'],status=s.status_name(status),seconds=round(time.monotonic()-started,2),variant_order=order,entries=entries)
    output=input_file.with_name(input_file.stem+'-full-period-check.json');output.write_text(json.dumps(result,indent=2)+'\n');print(result['mode'],result['rate'],'k',result['k'],'full-period candidate',result['status'],flush=True)

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('paths',type=Path,nargs='+');args=parser.parse_args()
    for path in args.paths:check(path)
