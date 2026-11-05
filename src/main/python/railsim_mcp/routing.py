"""Explicit route/flow allocation and topology evidence for UC2 and UC3."""

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
    volume_bounds: tuple = (4, 100)

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
            (plan["volumes"]["min"], plan["volumes"]["max"])
            if path.name == "uc3_operational_plan.json" else (4, 100),
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
                    mapping = self.flows[flow]["routes"][product]
                    if self.bidirectional and mapping["forward"] == mapping["reverse"]:
                        if direction == "reverse":
                            continue
                        direction = "one-way"
                    result[route] = dict(
                        product=product, flow=flow, direction=direction, base=route
                    )
        return result

    def composition(self, volume):
        low, high = self.volume_bounds
        if type(volume) is not int or not low <= volume <= high:
            raise ValueError(f"volume must be an integer in {low}..{high}")
        result = Counter()
        counts = (
            allocate(volume, self.shares)
            if self.volume_bounds == (1, 6)
            else rounded_counts(volume, self.shares)
        )
        for product, n in counts.items():
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
            volume_bounds=list(self.volume_bounds),
            allocation="largest remainder products then flows (lexical flow ties); same count each direction; identical direction routes receive twice the count as complete one-way trips; volume is per-direction allocation, total departures = 2 * volume when bidirectional",
        )


def topology(root, variants):
    network = ET.parse(root / "network.xml").getroot()
    schedule = ET.parse(root / "schedule.xml").getroot()
    routes = {
        r.get("id"): [link.get("refId") for link in r.find("route")]
        for r in schedule.iter("transitRoute")
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
        route_profiles={
            r.get("id"): [dict(stop.attrib) for stop in r.find("routeProfile")]
            for r in schedule.iter("transitRoute") if r.get("id") in variants
        },
        capacity_one_resources={v: sorted(r) for v, r in used.items()},
        shared_resources=sorted(common),
        entry_markers=sum(a.get("railsimEntry") == "true" for a in attrs.values()),
        platform_resources=sorted(
            {r for rs in used.values() for r in rs if r.startswith("platform")}
        ),
        ordered_guidance_supported=False,
        original_uc1_incompatibility="Branching bidirectional multi-route flows and multiple entry markers; UC1 platform/entry assumptions do not apply.",
        adaptation="Keep original network and routes; allocate declared product/flow/direction counts explicitly; direct solver on route groups. Shared-entry order proposals remain unsupported.",
    )
