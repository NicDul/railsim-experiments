"""Explicit route/flow allocation and topology evidence for UC2 transfer tests."""

from collections import Counter
from dataclasses import dataclass
from fractions import Fraction
import json
import xml.etree.ElementTree as ET
from .uc1_modes import rounded_counts


def allocate(total, shares):
    """Largest remainder, stable lexical tie break; sum exactly equals total."""
    weights = {k: Fraction(str(v)) for k, v in shares.items() if v > 0}
    if not weights or sum(weights.values()) != 1:
        raise ValueError("route shares must sum to one")
    targets = {k: total * v for k, v in weights.items()}
    result = {k: int(v) for k, v in targets.items()}
    for key in sorted(targets, key=lambda k: (-(targets[k] - result[k]), k))[
        : total - sum(result.values())
    ]:
        result[key] += 1
    return result


@dataclass(frozen=True)
class RouteMode:
    id: str
    shares: dict
    flows: dict
    patterns: dict
    bidirectional: bool

    @classmethod
    def load(cls, path, mode):
        plan = json.loads(path.read_text())
        mix, pattern = mode.split("/")
        if not any(m["mix"] == mix and pattern in m["patterns"] for m in plan["modes"]):
            raise ValueError("mode not declared")
        return cls(
            mode,
            plan["mixes"][mix]["shares"],
            plan["flows"],
            plan["patterns"][pattern]["shares"],
            plan["volumes"]["bidirectional"],
        )

    @property
    def variants(self):
        result = {}
        for product, share in self.shares.items():
            if not share:
                continue
            for flow, weight in self.patterns[product].items():
                if not weight:
                    continue
                for direction in (
                    ["forward", "reverse"] if self.bidirectional else ["forward"]
                ):
                    route = self.flows[flow]["routes"][product][direction]
                    result[route] = dict(
                        product=product, flow=flow, direction=direction, base=route
                    )
        return result

    def composition(self, volume):
        result = Counter()
        for product, n in rounded_counts(volume, self.shares).items():
            for flow, count in allocate(n, self.patterns[product]).items():
                for direction in (
                    ["forward", "reverse"] if self.bidirectional else ["forward"]
                ):
                    result[self.flows[flow]["routes"][product][direction]] += count
        return {k: n for k, n in sorted(result.items()) if n}

    def record(self):
        return dict(
            id=self.id,
            shares=self.shares,
            patterns=self.patterns,
            variants=self.variants,
            bidirectional=self.bidirectional,
            allocation="largest remainder products then flows; same count each direction; volume is per direction",
        )


def topology(root, variants):
    network = ET.parse(root / "network.xml").getroot()
    routes = {
        r.get("id"): [link.get("refId") for link in r.find("route")]
        for r in ET.parse(root / "schedule.xml").getroot().iter("transitRoute")
    }
    attrs = {
        link.get("id"): {a.get("name"): a.text for a in link.iter("attribute")}
        for link in network.iter("link")
    }
    missing = set(variants) - set(routes)
    if missing:
        raise ValueError("missing routes: " + str(sorted(missing)))
    used = {
        v: {
            attrs[l].get("railsimResourceId", l)
            for l in routes[v]
            if attrs[l].get("railsimTrainCapacity") == "1"
        }
        for v in variants
    }
    common = set.intersection(*used.values())
    return dict(
        routes=routes,
        capacity_one_resources={v: sorted(r) for v, r in used.items()},
        shared_resources=sorted(common),
        entry_markers=sum(a.get("railsimEntry") == "true" for a in attrs.values()),
        platform_resources=sorted(
            {r for rs in used.values() for r in rs if r.startswith("platform")}
        ),
        ordered_guidance_supported=False,
        original_uc1_incompatibility="UC2 has branching bidirectional multi-route flows, multiple entry markers and no platforms; UC1 mode loader and platform/entry assumptions do not apply.",
        adaptation="Keep original network and routes; allocate declared product/flow/direction counts explicitly; direct solver on route groups. Shared-entry order proposals remain unsupported.",
    )
