"""Occupation bound for UC1 (experiment): does the total holding time fit on every resource?

A resource (platform, switch, track section) can hold one train at a time, so over one period the
holding times of all trains using it add up to at most 1800 s. Each train picks a platform, so
CP-SAT chooses how many trains of each type use each platform to make the busiest resource as
light as possible. If even then some resource needs more than 1800 s, the rate is impossible.
Also reports the simpler check: total platform time <= number of platforms x 1800 s.
"""
import argparse
import json
from pathlib import Path

from ortools.sat.python import cp_model

from railsim_mcp.uc1 import PERIOD
from railsim_mcp.uc1_modes import UC1Mode, rounded_counts
from solve_uc1_cpsat import reference_windows


def occupation(options, counts, windows, gap=0):
    variants = [v for p in counts for v in options[p]]
    resources = sorted({r for v in variants for r in windows[v]})
    model = cp_model.CpModel()
    n = {v: model.NewIntVar(0, sum(counts.values()), v) for v in variants}
    for p, k in counts.items():
        model.Add(sum(n[v] for v in options[p]) == k)
    load = {r: sum((windows[v][r][2] - windows[v][r][1] + gap)*n[v] for v in variants if r in windows[v])
            for r in resources}
    busiest = model.NewIntVar(0, 100*PERIOD, 'busiest')
    for r in resources:
        model.Add(busiest >= load[r])
    model.Minimize(busiest)
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = 30
    status = solver.Solve(model)
    loads = {r: solver.Value(load[r]) for r in resources}
    platforms = [r for r in resources if r.startswith('platform')]
    platform_total = min(  # simple check: least total platform time over all splits = each type's cheapest platform
        sum(k*min(windows[v][r][2] - windows[v][r][1] for v in options[p] for r in windows[v] if r.startswith('platform'))
            for p, k in counts.items()), 10**9)
    return dict(busiest_seconds=solver.Value(busiest), proven=status == cp_model.OPTIMAL,
                busiest_resources=sorted((r for r in resources if loads[r] == solver.Value(busiest))),
                loads=dict(sorted(loads.items(), key=lambda kv: -kv[1])),
                split={v: solver.Value(n[v]) for v in variants if solver.Value(n[v])},
                platform_total_seconds=platform_total, platform_capacity_seconds=len(platforms)*PERIOD)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--variants', type=Path, required=True)
    parser.add_argument('--mode', required=True)
    parser.add_argument('--volumes', type=int, nargs='+', required=True)
    parser.add_argument('--runs-root', type=Path, required=True)
    parser.add_argument('--plan', type=Path, required=True)
    args = parser.parse_args()
    reference = json.loads(args.variants.read_text())
    windows = reference_windows(args.runs_root, reference['baselines'])
    options = {}
    for variant, info in reference['variants'].items():
        options.setdefault(info['product'], []).append(variant)
    mode = UC1Mode.load(args.plan, args.mode)
    for volume in args.volumes:
        counts = {p: k for p, k in rounded_counts(volume, mode.shares).items() if k}
        print(json.dumps(dict(trains_per_hour=2*volume, counts=counts, **occupation(options, counts, windows))), flush=True)


if __name__ == '__main__':
    main()
