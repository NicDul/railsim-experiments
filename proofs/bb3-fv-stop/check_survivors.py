"""Complete platform/time checks for the surviving entry-type orders.

Every type word eliminated by enumerate_orders.cpp violates necessary shared
entry/exit constraints. This stage uses every actual resource profile.
"""
from pathlib import Path
from collections import Counter
from concurrent.futures import ThreadPoolExecutor,as_completed
import argparse,hashlib,itertools,json,time
import ortools
from ortools.sat.python import cp_model

ROOT=Path(__file__).resolve().parent
SOURCE=ROOT/'inputs'
W=json.loads((SOURCE/'fv_stop-profiles.json').read_text());P=1800
PREFIX={'R':'RV_LMR','F':'FV_LMR','G':'GV_LR'}

def intervals(A,B,lo,hi):
    bad=set()
    for r in set(A)&set(B):
        a,b=A[r];c,d=B[r]
        bad.update(range(max(lo,a-d+1),min(hi,b-c-1)+1))
    out=[]
    for delta in range(lo,hi+1):
        if delta in bad:continue
        if out and delta==out[-1][1]+1:out[-1][1]=delta
        else:out.append([delta,delta])
    return out

def verify(order,entries):
    assert len(order)==len(entries)
    for i,A in enumerate(order):
        for j,B in enumerate(order):
            for shift in [-P,0,P]:
                if i==j and not shift:continue
                for r in set(W[A])&set(W[B]):
                    a,b=W[A][r];c,d=W[B][r]
                    assert max(entries[i]+a,entries[j]+c+shift)>=min(entries[i]+b,entries[j]+d+shift),(i,j,shift,r)

def check(word,seconds=5,control=False):
    n=len(word);m=cp_model.CpModel()
    platforms=[m.new_int_var(0,2,f'p{i}') for i in range(n)]
    gaps=[m.new_int_var(85,P-85*(n-1),f'g{i}') for i in range(n)]
    entries=[m.new_int_var(0,P-1,f't{i}') for i in range(n)]
    m.add(sum(gaps)==P)
    for i in range(n):m.add(entries[i]==sum(gaps[:i]))
    for i,q in enumerate(word):
        durations=[W[f'{PREFIX[q]}_P{p}']['block_1'][1] for p in [1,2,3]]
        entry_duration=m.new_int_var(min(durations),max(durations),'duration')
        m.add_element(platforms[i],durations,entry_duration);m.add(gaps[i]>=entry_duration)
    for h in range(1,5):
        lo=85*h;hi=P-85*(n-h)
        for i in range(n):
            j=(i+h)%n;table=[]
            for a,b in itertools.product(range(3),repeat=2):
                A=W[f'{PREFIX[word[i]]}_P{a+1}'];B=W[f'{PREFIX[word[j]]}_P{b+1}']
                table.extend((a,b,l,u) for l,u in intervals(A,B,lo,hi))
            lower=m.new_int_var(lo,hi,'lower');upper=m.new_int_var(lo,hi,'upper')
            m.add_allowed_assignments([platforms[i],platforms[j],lower,upper],table)
            delta=sum(gaps[(i+k)%n] for k in range(h))
            m.add(delta>=lower);m.add(delta<=upper)
    if not control:
        # Necessary exit order, proved separately for the 7F/7R/3G mix.
        assert Counter(word)==Counter(F=7,R=7,G=3) and word[0]=='R'
        order=[];i=0
        while i<n:
            if i+1<n and word[i:i+2]=='FG':order.extend([i+1,i]);i+=2
            else:order.append(i);i+=1
        for k,i in enumerate(order):
            j=order[(k+1)%n];table=[]
            for a,b in itertools.product(range(3),repeat=2):
                A=W[f'{PREFIX[word[i]]}_P{a+1}'];B=W[f'{PREFIX[word[j]]}_P{b+1}']
                bound=max(A[r][1]-B[r][0] for r in ['block_2','switch_2'])
                table.append((a,b,bound))
            headway=m.new_int_var(min(row[-1] for row in table),max(row[-1] for row in table),'exit_headway')
            m.add_allowed_assignments([platforms[i],platforms[j],headway],table)
            m.add(entries[j]-entries[i]+(P if k==n-1 else 0)>=headway)
    path=ROOT/'orders'/word;path.parent.mkdir(exist_ok=True)
    m.export_to_file(str(path.with_suffix('.model.pbtxt')))
    s=cp_model.CpSolver();s.parameters.num_search_workers=1;s.parameters.max_time_in_seconds=seconds
    s.parameters.log_search_progress=True;s.parameters.log_to_stdout=False
    start=time.monotonic()
    with path.with_suffix('.solver.log').open('w',buffering=1) as log:
        s.log_callback=lambda msg:log.write(msg+'\n')
        status=s.solve(m)
    result=dict(type_word=word,status=s.status_name(status),seconds=round(time.monotonic()-start,3),
                ortools=ortools.__version__,profile_sha256=hashlib.sha256((SOURCE/'fv_stop-profiles.json').read_bytes()).hexdigest())
    if status in [cp_model.OPTIMAL,cp_model.FEASIBLE]:
        order=[f'{PREFIX[q]}_P{s.value(platforms[i])+1}' for i,q in enumerate(word)]
        ts=[s.value(t) for t in entries];verify(order,ts)
        result.update(variant_order=order,entries=ts,independent_pair_check='PASS')
    path.with_suffix('.response.txt').write_text(str(s.response_proto))
    path.with_suffix('.json').write_text(json.dumps(result,indent=2)+'\n')
    return result

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--seconds',type=float,default=5);p.add_argument('--workers',type=int,default=4)
    args=p.parse_args();words=(ROOT/'surviving-type-orders.txt').read_text().split();start=time.monotonic();rows=[]
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        jobs=[pool.submit(check,w,args.seconds) for w in words]
        for job in as_completed(jobs):
            row=job.result();rows.append(row)
            print(len(rows),'/',len(words),row['type_word'],row['status'],row['seconds'],flush=True)
    result=dict(total=len(rows),statuses=dict(Counter(row['status'] for row in rows)),
                seconds=round(time.monotonic()-start,3),results=sorted(rows,key=lambda x:x['type_word']))
    (ROOT/'survivor-results.json').write_text(json.dumps(result,indent=2)+'\n')
    print('SUMMARY',json.dumps({k:v for k,v in result.items() if k!='results'}),flush=True)
