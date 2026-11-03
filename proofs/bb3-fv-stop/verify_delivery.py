"""Check decision coverage, solver artifacts and feasible controls in the package."""
from collections import Counter
import hashlib,json
from check_survivors import ROOT,SOURCE,W

audit=json.loads((ROOT/'certificate-audit.json').read_text())
one=json.loads((ROOT/'survivor-results.json').read_text())
two=json.loads((ROOT/'independent-tight-results.json').read_text())
words=set((ROOT/'surviving-type-orders.txt').read_text().split())
digest=hashlib.sha256((SOURCE/'fv_stop-profiles.json').read_bytes()).hexdigest()
assert audit['status']=='PASS' and audit['profile_sha256']==digest
assert audit['canonical_words_covered']==137280 and audit['positive_cycle_certificates']==137130
assert audit['certificate_sha256']==hashlib.sha256((ROOT/'order-certificates.tsv').read_bytes()).hexdigest()
for result,folder,key,period_suffix in [(one,'orders','type_word',''),(two,'independent-tight-orders','word','-p1800')]:
    assert result['total']==150 and result['statuses']=={'INFEASIBLE':150}
    assert {r[key] for r in result['results']}==words
    for row in result['results']:
        assert row['status']=='INFEASIBLE'
        if 'profile_sha256' in row:assert row['profile_sha256']==digest
        path=ROOT/folder/(row[key]+period_suffix)
        assert path.with_suffix('.model.pbtxt').stat().st_size>0
        assert 'status: INFEASIBLE' in path.with_suffix('.response.txt').read_text()
        assert path.with_suffix('.solver.log').stat().st_size>0

def check_control(control,counts):
    assert control['status'] in ['OPTIMAL','FEASIBLE']
    variants,ts,p=control['variants'],control['entries'],control['period']
    assert Counter(v[:2] for v in variants)==Counter(counts)
    assert ts[0]==0 and all(a<b for a,b in zip(ts,ts[1:])) and ts[-1]<p
    for i,A in enumerate(variants):
        for j,B in enumerate(variants):
            for shift in [-p,0,p]:
                if i==j and shift==0:continue
                for r in set(W[A])&set(W[B]):
                    a,b=W[A][r];c,d=W[B][r]
                    assert max(ts[i]+a,ts[j]+shift+c)>=min(ts[i]+b,ts[j]+shift+d),(i,j,shift,r)
check_control(two['positive_32h'],{'FV':7,'RV':6,'GV':3})
check_control(two['positive_1810s'],{'FV':7,'RV':7,'GV':3})
sim=json.loads((SOURCE/'known-32h-simulator-summary.json').read_text())
assert sim['status']=='completed' and sim['trains_arrived']==sim['trains_expected']==96
assert sim['trains_unfinished']==sim['trains_not_started']==sim['delayed_train_count']==sim['headway_conflict_count']==sim['max_terminal_delay_seconds']==0
result=dict(status='PASS',canonical_type_words=137280,arithmetic_exclusions=137130,
            exact_survivors_checked_twice=150,all_exact_statuses='INFEASIBLE',
            positive_controls='PASS',retained_32h_simulator='96/96 arrived, zero delay, zero conflicts',
            fixed_profile_capacity_trains_per_hour=32,excluded_next_rate=34,
            profile_sha256=digest,models_called=[])
(ROOT/'delivery-audit.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result))
