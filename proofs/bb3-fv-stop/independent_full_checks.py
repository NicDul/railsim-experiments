"""Independent complete periodic model: no forced exit order or gap tables.

Each train has three Boolean platform alternatives and an entry time. Every
pair of resource intervals is constrained against every relevant period shift.
"""
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor,as_completed
from collections import Counter
import itertools,json,time
from ortools.sat.python import cp_model
from check_survivors import ROOT,SOURCE,W,P,PREFIX

def check(word,period=1800,seconds=10):
    n=len(word);m=cp_model.CpModel();t=[m.new_int_var(0,period-1,f't{i}') for i in range(n)]
    m.add(t[0]==0)
    entry_min={q:min(W[f'{PREFIX[q]}_P{k}']['block_1'][1] for k in [1,2,3]) for q in PREFIX}
    durations=[entry_min[q] for q in word]
    for i in range(n-1):m.add(t[i+1]>=t[i]+durations[i])
    m.add(t[-1]+durations[-1]<=period)
    choices=[]
    for i,q in enumerate(word):
        choices.append({f'{PREFIX[q]}_P{k}':m.new_bool_var(f'p{i}_{k}') for k in [1,2,3]})
        m.add_exactly_one(choices[-1].values())
    for i,j in itertools.combinations(range(n),2):
        difference_lo=sum(durations[i:j]);difference_hi=period-(sum(durations)-difference_lo)
        for A,a_present in choices[i].items():
            for B,b_present in choices[j].items():
                for r in set(W[A])&set(W[B]):
                    a,b=W[A][r];c,d=W[B][r]
                    lower=b-c;upper=period-(d-a)
                    possible=[k for k in [-1,0,1] if lower-k*period<=difference_hi and upper-k*period>=difference_lo]
                    if not possible:
                        m.add_bool_or([a_present.Not(),b_present.Not()]);continue
                    z=possible[0] if len(possible)==1 else m.new_int_var(min(possible),max(possible),'period_shift')
                    delta=t[j]-t[i]+period*z
                    m.add(delta>=lower).only_enforce_if([a_present,b_present])
                    m.add(delta<=upper).only_enforce_if([a_present,b_present])
    path=ROOT/'independent-tight-orders'/f'{word}-p{period}';path.parent.mkdir(exist_ok=True)
    m.export_to_file(str(path.with_suffix('.model.pbtxt')))
    s=cp_model.CpSolver();s.parameters.num_search_workers=1;s.parameters.max_time_in_seconds=seconds
    s.parameters.log_search_progress=True;s.parameters.log_to_stdout=False;start=time.monotonic()
    with path.with_suffix('.solver.log').open('w',buffering=1) as log:
        s.log_callback=lambda msg:log.write(msg+'\n');status=s.solve(m)
    result=dict(word=word,period=period,status=s.status_name(status),seconds=round(time.monotonic()-start,3))
    if status in [cp_model.FEASIBLE,cp_model.OPTIMAL]:
        result['entries']=[s.value(x) for x in t]
        result['variants']=[next(A for A,x in c.items() if s.value(x)) for c in choices]
    path.with_suffix('.response.txt').write_text(str(s.response_proto))
    path.with_suffix('.json').write_text(json.dumps(result,indent=2)+'\n')
    return result

if __name__=='__main__':
    start=time.monotonic();words=(ROOT/'surviving-type-orders.txt').read_text().split();rows=[]
    with ThreadPoolExecutor(max_workers=4) as pool:
        for job in as_completed([pool.submit(check,w) for w in words]):
            row=job.result();rows.append(row)
            print(len(rows),'/',len(words),row['word'],row['status'],row['seconds'],flush=True)
    known=next(r for r in json.loads((SOURCE/'known-feasible-audit.json').read_text()) if r['mode']=='fv_stop')
    word=''.join(v[:1] for v,t in known['entry_pattern'])
    positive=check(word,seconds=30)
    long=json.loads((SOURCE/'diagnostic-1810.json').read_text())
    longer=check(''.join(v[:1] for v in long['variant_order']),period=1810,seconds=30)
    result=dict(total=len(rows),statuses=dict(Counter(r['status'] for r in rows)),
                seconds=round(time.monotonic()-start,3),positive_32h=positive,positive_1810s=longer,
                results=sorted(rows,key=lambda r:r['word']))
    (ROOT/'independent-tight-results.json').write_text(json.dumps(result,indent=2)+'\n')
    print('SUMMARY',json.dumps({k:v for k,v in result.items() if k!='results'}),flush=True)
