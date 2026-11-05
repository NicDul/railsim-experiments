"""Recover an objective lower bound from the validated threshold formulation.

Relaxes only its k-window threshold, adds the local-cost objective and retains
all previously generated valid connectivity cuts and shorter-window budgets.
"""
from pathlib import Path
import argparse,json,itertools,math,time
from ortools.sat.python import cp_model
from check_remaining import components
from check_window_candidates import spell

ROOT=Path(__file__).resolve().parent

def run(mode,rate,seconds,k=5,exact=False):
    start=time.monotonic();budget=(k-1)*1800;path=ROOT/mode/f'rate-{rate}-k{k}';W=json.loads((ROOT/(mode+'-profiles.json')).read_text());V=tuple(sorted(W));words=list(itertools.product(V,repeat=k))
    cost={tuple(r['word']):r['cost'] for r in json.loads((ROOT/mode/f'local-k{k}.json').read_text())['rows']}
    model=cp_model.CpModel();model.proto.parse_text_format(path.with_suffix('.model.pbtxt').read_text())
    indices=[i for i,c in enumerate(model.proto.constraints) if len(c.linear.vars)>100 and list(c.linear.domain)[-1:]==[budget]]
    assert len(indices)==1
    threshold=model.proto.constraints[indices[0]].linear.domain
    threshold[1]=10**9
    x={w:model.get_int_var_from_proto_index(i) for i,w in enumerate(words)}
    model.minimize(sum(cost[w]*x[w] for w in words))
    for length in range(k-1,1,-1):
        f=ROOT/mode/f'rate-{rate}-k{length}.json'
        if not f.exists():continue
        d=json.loads(f.read_text())
        if not d.get('connected_relaxation_witness'):continue
        order=spell(d['chain']);n=len(order);from collections import Counter
        hint=Counter(tuple(order[(i+j)%n] for j in range(k)) for i in range(n))
        for w in words:model.add_hint(x[w],hint[w])
        break
    s=cp_model.CpSolver();s.parameters.num_search_workers=4;s.parameters.max_time_in_seconds=seconds;s.parameters.random_seed=4711
    s.parameters.log_search_progress=True;s.parameters.log_to_stdout=False
    bounds=[]
    def bound(value):
        bounds.append(value)
        if value>budget+0.0001:s.stop_search()
    if not exact:s.best_bound_callback=bound
    target=path.with_name(path.name+'-minimise')
    with target.with_suffix('.solver.log').open('w') as f:
        s.log_callback=lambda message:f.write(message+'\n');status=s.solve(model)
    lower=math.ceil(s.best_objective_bound-1e-6)
    feasible=status in (cp_model.OPTIMAL,cp_model.FEASIBLE)
    chosen={w:s.value(x[w]) for w in words if s.value(x[w])} if feasible else {}
    result=dict(mode=mode,rate=rate,k=k,status=s.status_name(status),best_bound_raw=s.best_objective_bound,integer_lower_bound=lower,budget=budget,excluded_in_fixed_profile_model=lower>budget or status==cp_model.INFEASIBLE,best_objective=s.objective_value if feasible else None,components=len(components(chosen)) if chosen else None,seconds=round(time.monotonic()-start,2),bound_updates=bounds,chain={'>'.join(w):z for w,z in chosen.items()})
    model.export_to_file(str(target.with_suffix('.model.pbtxt')));target.with_suffix('.response.txt').write_text(str(s.response_proto));target.with_suffix('.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps({k:v for k,v in result.items() if k!='chain'}),flush=True)

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--mode',required=True);p.add_argument('--rate',type=int,required=True);p.add_argument('--seconds',type=float,default=180);p.add_argument('--k',type=int,default=5);p.add_argument('--exact',action='store_true');a=p.parse_args();run(a.mode,a.rate,a.seconds,a.k,a.exact)
