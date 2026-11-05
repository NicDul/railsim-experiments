"""Declared UC1 mode -> fixed routes, mix and deterministic integer allocation."""
from dataclasses import dataclass
from fractions import Fraction
import json
from pathlib import Path

DEFAULT_MODE = 'MAINLINE/FV_PASS'
PRODUCT_ORDER = ('FV','RV','GV')


def rounded_counts(volume, shares):
    if type(volume) is not int or not 1 <= volume <= 100:
        raise ValueError('volume must be an integer in 1..100')
    products = [p for p in PRODUCT_ORDER if shares.get(p,0)>0]
    if not products or set(shares)-set(PRODUCT_ORDER):
        raise ValueError('unsupported or empty product mix')
    weights = [Fraction(str(shares[p])) for p in products]
    if sum(weights) != 1 or any(w<=0 for w in weights):
        raise ValueError('positive mix shares must sum to one')
    targets = [w*volume for w in weights]
    counts = [int(t) for t in targets]
    for i in sorted(range(len(products)), key=lambda i:(-(targets[i]-counts[i]),i))[:volume-sum(counts)]:
        counts[i] += 1
    return dict(zip(products, counts))


@dataclass(frozen=True)
class UC1Mode:
    id: str
    shares: dict
    routes: dict

    @classmethod
    def from_plan(cls, plan, mode_id):
        if mode_id not in [m['mix']+'/'+p for m in plan['modes'] for p in m['patterns']]:
            raise ValueError('mode is not declared in UC1: '+mode_id)
        mix, pattern = mode_id.split('/')
        shares = {p:plan['mixes'][mix]['shares'][p] for p in PRODUCT_ORDER if plan['mixes'][mix]['shares'].get(p,0)>0}
        rounded_counts(4,shares)
        routes = {}
        for product in shares:
            flows = [(flow,weight) for flow,weight in plan['patterns'][pattern]['shares'][product].items() if weight>0]
            if len(flows)!=1 or flows[0][1]!=1:
                raise ValueError('search requires one fixed route per product')
            routes[product] = plan['flows'][flows[0][0]]['routes'][product]['forward']
        return cls(mode_id,shares,routes)

    @classmethod
    def load(cls, path, mode_id=DEFAULT_MODE):
        return cls.from_plan(json.loads(Path(path).read_text()),mode_id)

    @classmethod
    def legacy_default(cls):
        return cls(DEFAULT_MODE,dict(FV=.4,RV=.4,GV=.2),dict(FV='FV_LR',RV='RV_LMR',GV='GV_LR'))

    def composition(self, volume):
        return rounded_counts(volume,self.shares)

    def record(self):
        return dict(id=self.id,shares=self.shares,routes=self.routes)

    def description(self):
        stopping = [p for p,r in self.routes.items() if r.endswith('_LMR')]
        passing = [p for p in self.routes if p not in stopping]
        return f"Mode {self.id}; fixed mix {self.shares}; fixed routes {self.routes}. Stop at M: {stopping}; pass through M: {passing}."
