"""Verify coverage and positive-cycle certificates from raw profile data.

This verifier does not run the enumerator or a graph-search algorithm.
It checks explicit contradictory sums of necessary inequalities.
"""
from collections import Counter
from math import factorial,gcd
import hashlib,itertools,json,time
from check_survivors import ROOT,SOURCE,W,P

start=time.monotonic();types='RFG';names={'R':'RV','F':'FV','G':'GV'}
variants={q:[v for v in W if v.startswith(names[q])] for q in types}
entry={q:min(W[v]['block_1'][1] for v in variants[q]) for q in types}
headway={(q,r):min(max(W[a][s][1]-W[b][s][0] for s in ['block_2','switch_2'])
                 for a,b in itertools.product(variants[q],variants[r]))
         for q,r in itertools.product(types,repeat=2)}
overtake={(q,r):max(W[a]['block_2'][0]-W[b]['block_2'][1]
                  for a,b in itertools.product(variants[q],variants[r]))
          for q,r in itertools.product(types,repeat=2)}
assert entry==dict(R=85,F=95,G=95)
assert headway==dict(zip(itertools.product(types,repeat=2),[61,-47,226,186,78,351,-67,-175,98]))
assert all(overtake[q,r]<entry[q] for q,r in overtake if (q,r)!=('F','G'))
assert overtake['F','G']==175<entry['F']+min(entry.values())
base=7*entry['R']+7*entry['F']+3*entry['G']
assert base==1545 and base+headway['F','G']-entry['F']==1801>P
assert all(W[v]['block_1'][0]==0 for v in W)

def edges(word):
    es=[(i,(i+1)%17,entry[q]-(P if i==16 else 0)) for i,q in enumerate(word)]
    out=[];i=0
    while i<17:
        if word[i:i+2]=='FG':out.extend([i+1,i]);i+=2
        else:out.append(i);i+=1
    for k,i in enumerate(out):
        j=out[(k+1)%17]
        es.append((i,j,headway[word[i],word[j]]-(P if k==16 else 0)))
    return es

seen=set();passes=set();minimum_contradiction=10**9
for line in (ROOT/'order-certificates.tsv').read_text().splitlines():
    word,cert,total=line.split('\t');total=int(total)
    assert len(word)==17 and Counter(word)==Counter(R=7,F=7,G=3)
    encoded=word.translate(str.maketrans('RFG','012'))
    assert encoded==min(encoded[k:]+encoded[:k] for k in range(17))
    assert word not in seen;seen.add(word)
    if cert=='PASS':
        assert total==0;passes.add(word);continue
    indices=[int(x) for x in cert.split(',')];es=edges(word)
    assert 1<=len(indices)<=17 and len(set(indices))==len(indices)
    assert all(0<=i<len(es) for i in indices)
    cycle=[es[i] for i in indices]
    assert all(cycle[k][1]==cycle[(k+1)%len(cycle)][0] for k in range(len(cycle)))
    assert sum(e[2] for e in cycle)==total and total>0
    minimum_contradiction=min(minimum_contradiction,total)
expected=factorial(17)//(factorial(7)*factorial(7)*factorial(3))//17
assert gcd(gcd(7,7),3)==1 and len(seen)==expected==137280
assert passes==set((ROOT/'surviving-type-orders.txt').read_text().split()) and len(passes)==150
result=dict(status='PASS',canonical_words_covered=len(seen),positive_cycle_certificates=len(seen)-len(passes),
            survivors=len(passes),minimum_positive_cycle_sum=minimum_contradiction,
            entry_minima=entry,exit_headways={a+b:h for (a,b),h in headway.items()},
            maximum_overtaking_entry_gaps={a+b:h for (a,b),h in overtake.items()},
            mandatory_entry_workload=base,unpassed_FG_minimum_period=1801,
            seconds=round(time.monotonic()-start,3),
            profile_sha256=hashlib.sha256((SOURCE/'fv_stop-profiles.json').read_bytes()).hexdigest(),
            certificate_sha256=hashlib.sha256((ROOT/'order-certificates.tsv').read_bytes()).hexdigest())
(ROOT/'certificate-audit.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result))
