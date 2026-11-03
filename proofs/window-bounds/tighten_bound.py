"""Continue objective minimisation with necessary connected-support cuts."""
from pathlib import Path
import argparse,json,itertools,math,time
from ortools.sat.python import cp_model
from check_remaining import components

ROOT=Path(__file__).resolve().parent
def run(mode,rate,seconds,k=5):
    start=time.monotonic();deadline=start+seconds;budget=(k-1)*1800;path=ROOT/mode/f'rate-{rate}-k{k}-minimise'
    previous=json.loads(path.with_suffix('.json').read_text());model=cp_model.CpModel();model.proto.parse_text_format(path.with_suffix('.model.pbtxt').read_text())
    V=tuple(sorted(json.loads((ROOT/(mode+'-profiles.json')).read_text())));words=list(itertools.product(V,repeat=k));nodes=list(itertools.product(V,repeat=k-1))
    x={w:model.get_int_var_from_proto_index(i) for i,w in enumerate(words)};active={u:model.get_bool_var_from_proto_index(len(words)+i) for i,u in enumerate(nodes)}
    chosen={tuple(k.split('>')):v for k,v in previous['chain'].items()};rounds=[];s=cp_model.CpSolver();s.parameters.num_search_workers=4;s.parameters.random_seed=4711;s.parameters.log_search_progress=True;s.parameters.log_to_stdout=False
    s.best_bound_callback=lambda b:s.stop_search() if b>budget+0.0001 else None
    output=path.with_name(path.name+'-connected')
    with output.with_suffix('.solver.log').open('w') as log:
        s.log_callback=lambda msg:log.write(msg+'\n')
        for iteration in range(30):
            groups=components(chosen)
            if len(groups)==1:break
            for group in groups:
                inside=min(group);outside=min(next(g for g in groups if g is not group))
                model.add(sum(x[w] for w in words if w[:-1] in group and w[1:] not in group)>=active[inside]+active[outside]-1)
            model.clear_hints()
            for w in words:model.add_hint(x[w],chosen.get(w,0))
            remaining=deadline-time.monotonic()
            if remaining<1:break
            s.parameters.max_time_in_seconds=remaining;status=s.solve(model)
            feasible=status in (cp_model.OPTIMAL,cp_model.FEASIBLE)
            chosen={w:s.value(x[w]) for w in words if s.value(x[w])} if feasible else {}
            lower=math.ceil(s.best_objective_bound-1e-6)
            row=dict(iteration=iteration,status=s.status_name(status),lower_bound=lower,best=s.objective_value if feasible else None,components=len(components(chosen)) if chosen else None)
            rounds.append(row);print(mode,rate,row,flush=True)
            result=dict(mode=mode,rate=rate,k=k,rounds=rounds,status=s.status_name(status),integer_lower_bound=lower,budget=budget,excluded_in_fixed_profile_model=lower>budget or status==cp_model.INFEASIBLE,seconds=round(time.monotonic()-start,2),chain={'>'.join(w):z for w,z in chosen.items()})
            output.with_suffix('.json').write_text(json.dumps(result,indent=2)+'\n')
            if result['excluded_in_fixed_profile_model'] or status!=cp_model.OPTIMAL or row['components']==1:break
    model.export_to_file(str(output.with_suffix('.model.pbtxt')));output.with_suffix('.response.txt').write_text(str(s.response_proto))

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--mode',required=True);p.add_argument('--rate',type=int,required=True);p.add_argument('--seconds',type=float,default=180);p.add_argument('--k',type=int,default=5);a=p.parse_args();run(a.mode,a.rate,a.seconds,a.k)
