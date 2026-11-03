"""Adaptive sliding-window exclusion checks; local CPU only, no model API.

uv run --with ortools==9.15.6755 python check_remaining.py --mode fv_pass --rates 40 42
uv run --with ortools==9.15.6755 python check_remaining.py --mode fv_stop --rates 34 36 38

Uses the same necessary conditions as the BB2 report. The master is a decision
problem: can a connected circulation satisfy every j-window budget, including k?
INFEASIBLE excludes the fixed-profile timetable; FEASIBLE never certifies one.
"""
from pathlib import Path
from collections import Counter,defaultdict
from fractions import Fraction
import argparse,hashlib,itertools,json,math,time,platform
import ortools
from ortools.sat.python import cp_model

ROOT=Path(__file__).resolve().parent
P=1800

def counts(rate):
    n=rate//2;types=['FV','RV','GV'];targets=[Fraction(2,5)*n,Fraction(2,5)*n,Fraction(1,5)*n]
    c=[int(x) for x in targets]
    for i in sorted(range(3),key=lambda i:(-(targets[i]-c[i]),i))[:n-sum(c)]:c[i]+=1
    return dict(zip(types,c))

def components(chosen):
    links=defaultdict(set)
    for w in chosen:links[w[:-1]].add(w[1:]);links[w[1:]].add(w[:-1])
    unseen=set(links);groups=[]
    while unseen:
        todo=[min(unseen)];seen=set()
        while todo:
            u=todo.pop()
            if u not in seen:seen.add(u);todo.extend(links[u]-seen)
        unseen-=seen;groups.append(seen)
    return groups

def local_cost(word,W):
    m=cp_model.CpModel();t=[m.new_int_var(0,5400,'') for _ in word];m.add(t[0]==0)
    for i in range(len(word)-1):m.add(t[i+1]>=t[i]+1)
    for i,j in itertools.combinations(range(len(word)),2):
        for r in sorted(set(W[word[i]])&set(W[word[j]])):
            a,b=W[word[i]][r];c,d=W[word[j]][r];z=m.new_bool_var('')
            m.add(t[i]+b<=t[j]+c).only_enforce_if(z)
            m.add(t[j]+d<=t[i]+a).only_enforce_if(z.Not())
    m.minimize(t[-1]);s=cp_model.CpSolver();s.parameters.num_search_workers=1;s.parameters.max_time_in_seconds=10
    status=s.solve(m)
    if status!=cp_model.OPTIMAL:raise RuntimeError(('local cost not optimal',word,s.status_name(status)))
    ts=[s.value(x) for x in t]
    for i,j in itertools.combinations(range(len(word)),2):
        for r in set(W[word[i]])&set(W[word[j]]):
            a,b=W[word[i]][r];c,d=W[word[j]][r]
            assert ts[i]+b<=ts[j]+c or ts[j]+d<=ts[i]+a
    assert abs(s.best_objective_bound-ts[-1])<1e-6,(word,ts,s.best_objective_bound,s.objective_value,s.status_name(status))
    return ts[-1],ts

def catalogue(mode,W,k):
    path=ROOT/mode/f'local-k{k}.json';digest=hashlib.sha256((ROOT/(mode+'-profiles.json')).read_bytes()).hexdigest()
    if path.exists():
        data=json.loads(path.read_text());assert data['input_sha256']==digest
        return {tuple(r['word']):r['cost'] for r in data['rows']}
    start=time.monotonic();rows=[]
    for i,w in enumerate(itertools.product(sorted(W),repeat=k),1):
        cost,ts=local_cost(w,W);rows.append(dict(word=w,cost=cost,witness=ts))
        if i%5000==0:print(mode,'local k',k,i,'/',len(W)**k,round(time.monotonic()-start,1),'s',flush=True)
    path.write_text(json.dumps(dict(input_sha256=digest,ortools=ortools.__version__,all_statuses='OPTIMAL',seconds=round(time.monotonic()-start,2),rows=rows),separators=(',',':'))+'\n')
    print(mode,'CATALOGUE DONE',k,len(rows),round(time.monotonic()-start,1),'s',flush=True)
    return {tuple(r['word']):r['cost'] for r in rows}

def decision(mode,W,rate,k,cost,seconds,cut_limit):
    started=time.monotonic();n=rate//2;mix=counts(rate);V=tuple(sorted(W));words=list(itertools.product(V,repeat=k));nodes=list(itertools.product(V,repeat=k-1))
    m=cp_model.CpModel();x={w:m.new_int_var(0,n,'') for w in words}
    active={u:m.new_bool_var('') for u in nodes}
    for u in nodes:
        incoming=sum(x[(v,)+u] for v in V);outgoing=sum(x[u+(v,)] for v in V)
        m.add(incoming==outgoing);m.add(incoming>=1).only_enforce_if(active[u]);m.add(incoming==0).only_enforce_if(active[u].Not())
    for q,c in mix.items():m.add(sum(x[w] for w in words if w[-1][:2]==q)==c)
    for j in range(2,k+1):m.add(sum(cost[w[:j]]*x[w] for w in words)<=(j-1)*P)
    s=cp_model.CpSolver();s.parameters.num_search_workers=4;s.parameters.random_seed=4711
    # A feasible disconnected circulation is enough to generate valid cuts;
    # no global objective or optimum is needed for this exclusion decision.
    rounds=[];chosen={};path=ROOT/mode/f'rate-{rate}-k{k}';deadline=time.monotonic()+seconds
    with path.with_suffix('.solver.log').open('w') as log:
        s.parameters.log_search_progress=True;s.parameters.log_to_stdout=False;s.log_callback=lambda message:log.write(message+'\n')
        for it in range(cut_limit+1):
            remaining=deadline-time.monotonic()
            if remaining<=0:status=cp_model.UNKNOWN;break
            s.parameters.max_time_in_seconds=remaining
            status=s.solve(m);name=s.status_name(status)
            if status==cp_model.INFEASIBLE:
                rounds.append(dict(iteration=it,status=name));break
            if status not in (cp_model.OPTIMAL,cp_model.FEASIBLE):
                rounds.append(dict(iteration=it,status=name));break
            chosen={w:s.value(x[w]) for w in words if s.value(x[w])};groups=components(chosen)
            rounds.append(dict(iteration=it,status=name,components=len(groups)))
            print(mode,rate,'k',k,'round',it,name,'components',len(groups),flush=True)
            if len(groups)==1:break
            if it==cut_limit:break
            for group in groups:
                inside=min(group);outside=min(next(g for g in groups if g is not group))
                m.add(sum(x[w] for w in words if w[:-1] in group and w[1:] not in group)>=active[inside]+active[outside]-1)
    exclusion=status==cp_model.INFEASIBLE
    connected_witness=status in (cp_model.OPTIMAL,cp_model.FEASIBLE) and len(components(chosen))==1
    totals=None
    if connected_witness:
        incoming=Counter();outgoing=Counter();composition=Counter()
        for w,z in chosen.items():incoming[w[1:]]+=z;outgoing[w[:-1]]+=z;composition[w[-1][:2]]+=z
        assert incoming==outgoing and composition==Counter(mix) and sum(chosen.values())==n
        totals={j:sum(cost[w[:j]]*z for w,z in chosen.items()) for j in range(2,k+1)}
        assert all(totals[j]<=(j-1)*P for j in totals)
    m.export_to_file(str(path.with_suffix('.model.pbtxt')))
    path.with_suffix('.response.txt').write_text(str(s.response_proto))
    row=dict(mode=mode,rate=rate,counts=mix,k=k,status=s.status_name(status),excluded_in_fixed_profile_model=exclusion,connected_relaxation_witness=connected_witness,window_totals=totals,rounds=rounds,seconds=round(time.monotonic()-started,2),chain={'>'.join(w):z for w,z in chosen.items()} if connected_witness else None)
    path.with_suffix('.json').write_text(json.dumps(row,indent=2)+'\n');print('RESULT',json.dumps({key:value for key,value in row.items() if key not in ['chain','rounds']}),flush=True)
    return row

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--mode',required=True);parser.add_argument('--rates',type=int,nargs='+',required=True);parser.add_argument('--max-k',type=int,default=5);parser.add_argument('--seconds',type=float,default=120);parser.add_argument('--cut-limit',type=int,default=100);args=parser.parse_args()
    started=time.monotonic();W=json.loads((ROOT/(args.mode+'-profiles.json')).read_text());(ROOT/args.mode).mkdir(exist_ok=True)
    cost={};pending=list(args.rates);results=[]
    for k in range(2,args.max_k+1):
        if not pending:break
        cost.update(catalogue(args.mode,W,k))
        for rate in list(pending):
            row=decision(args.mode,W,rate,k,cost,args.seconds,args.cut_limit);results.append(row)
            if row['excluded_in_fixed_profile_model']:pending.remove(rate)
            (ROOT/args.mode/'summary.json').write_text(json.dumps(dict(ortools=ortools.__version__,python=platform.python_version(),results=results,unexcluded=pending,elapsed_seconds=round(time.monotonic()-started,2)),indent=2)+'\n')
    print('MODE COMPLETE',args.mode,'unexcluded',pending,'seconds',round(time.monotonic()-started,1),flush=True)

if __name__=='__main__':main()
