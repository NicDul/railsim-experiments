"""Window bound for UC1 (experiment): can k consecutive trains at the entry fit in the period?

Number the trains by when they enter the single-track entry section (cyclically). For every run of
k consecutive trains (type and platform), CP-SAT computes the shortest possible time from the first
entry to the last, respecting every resource between those k trains (0 s gap). Around the cycle,
each gap between consecutive entries lies in k-1 such runs, so the spans of all n runs add up to
exactly (k-1) x 1800 s. A small flow problem then finds the cheapest way to chain runs with the
required number of trains of each type (platform split free). If even that needs more than
(k-1) x 1800 s, no timetable exists at this rate.

This captures interactions between trains up to k-1 places apart, such as an RV that keeps an
FV from entering 101-242 s after it, which the per-resource and track bounds cannot see.
"""
import argparse
import itertools
import json
from pathlib import Path
import time

from ortools.sat.python import cp_model

from railsim_mcp.uc1 import PERIOD
from railsim_mcp.uc1_modes import UC1Mode, rounded_counts
from solve_uc1_cpsat import reference_windows
from order_uc1 import sections


def span(run, windows, entry, gap):
    """Shortest first-to-last entry time for this run of variants entering in this order."""
    model = cp_model.CpModel()
    t = [model.NewIntVar(0, 3*PERIOD, f't{i}') for i in range(len(run))]
    model.Add(t[0] == 0)
    for i in range(len(run)-1):
        model.Add(t[i+1] >= t[i] + 1)
    grant = [windows[v][entry][1] for v in run]
    for i, j in itertools.combinations(range(len(run)), 2):
        a, b = windows[run[i]], windows[run[j]]
        for r in set(a) & set(b):
            # times relative to each train's entry grant; train j enters after train i
            a0, a1 = a[r][1]-grant[i], a[r][2]-grant[i]
            b0, b1 = b[r][1]-grant[j], b[r][2]-grant[j]
            first = model.NewBoolVar('')
            model.Add(t[i] + a1 + gap <= t[j] + b0).OnlyEnforceIf(first)
            model.Add(t[j] + b1 + gap <= t[i] + a0).OnlyEnforceIf(first.Not())
    model.Minimize(t[-1])
    solver = cp_model.CpSolver()
    solver.parameters.num_workers = 1
    solver.parameters.max_time_in_seconds = 10
    status = solver.Solve(model)
    if status != cp_model.OPTIMAL:
        raise RuntimeError(f'span of {run} not proven ({solver.StatusName(status)})')
    return solver.Value(t[-1])


def components(chosen):
    """Groups of (k-1)-tuple states linked by the chosen runs (undirected connectivity)."""
    link = {}
    for run in chosen:
        link.setdefault(run[:-1], set()).add(run[1:])
        link.setdefault(run[1:], set()).add(run[:-1])
    seen, groups = set(), []
    for start in link:
        if start in seen:
            continue
        group, todo = set(), [start]
        while todo:
            node = todo.pop()
            if node not in group:
                group.add(node)
                todo.extend(link[node] - group)
        seen |= group
        groups.append(group)
    return groups


def bound(options, counts, windows, entry, k, gap, joint=False, connected=False, max_cuts=200):
    """joint: the same chain must also satisfy the bound for every shorter run length (2..k):
    in a cycle, each run of j < k trains is the start of exactly one run of k trains."""
    variants = [v for p in counts for v in options[p]]
    product = {v: p for p in counts for v in options[p]}
    runs = list(itertools.product(variants, repeat=k))
    started = time.monotonic()
    spans = {}
    for length in (range(2, k+1) if joint else [k]):
        for run in itertools.product(variants, repeat=length):
            spans[run] = span(run, windows, entry, gap)
    cost = {run: spans[run] for run in runs}
    span_seconds = time.monotonic() - started
    model = cp_model.CpModel()
    n = sum(counts.values())
    x = {run: model.NewIntVar(0, n, '') for run in runs}
    # circulation on (k-1)-tuples: each run is one position; in = out at every tuple
    for node in itertools.product(variants, repeat=k-1):
        model.Add(sum(x[node+(v,)] for v in variants) == sum(x[(v,)+node] for v in variants))
    for p, c in counts.items():
        model.Add(sum(x[run] for run in runs if product[run[-1]] == p) == c)
    total = sum(cost[run]*x[run] for run in runs)
    if joint:
        for length in range(2, k):
            model.Add(sum(spans[run[:length]]*x[run] for run in runs) <= (length-1)*PERIOD)
    model.Minimize(total)
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = 300
    solver.parameters.num_workers = 8
    status = solver.Solve(model)
    cuts = 0
    if connected:
        # A real timetable is one cycle through all n trains, so the chosen runs must form one
        # connected circulation. Each time the optimum splits into separate loops, require some
        # run to leave the first loop's states whenever both it and another loop's states are used.
        nodes = list(itertools.product(variants, repeat=k-1))
        used = {node: model.NewBoolVar('') for node in nodes}
        for node in nodes:
            inflow = sum(x[(v,)+node] for v in variants)
            model.Add(inflow >= 1).OnlyEnforceIf(used[node])
            model.Add(inflow == 0).OnlyEnforceIf(used[node].Not())
        while status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            loops = components({r: solver.Value(x[r]) for r in runs if solver.Value(x[r])})
            if len(loops) == 1 or cuts >= max_cuts:
                break
            for loop in loops:
                outside = next(n for other in loops if other is not loop for n in other)
                inside = next(iter(loop))
                model.Add(sum(x[r] for r in runs if r[:-1] in loop and r[1:] not in loop)
                          >= used[inside] + used[outside] - 1)
            cuts += 1
            status = solver.Solve(model)
    proven = status in (cp_model.OPTIMAL, cp_model.INFEASIBLE)
    best = solver.Value(total) if status in (cp_model.OPTIMAL, cp_model.FEASIBLE) else None
    # INFEASIBLE in joint mode: no chain meets even the shorter-run limits
    lower = round(solver.BestObjectiveBound()) if status != cp_model.INFEASIBLE else 10**9
    used = {'>'.join(r): solver.Value(x[r]) for r in runs if status in (cp_model.OPTIMAL, cp_model.FEASIBLE) and solver.Value(x[r])}
    return dict(k=k, joint=joint, connected=connected, connectivity_cuts=cuts, status=solver.StatusName(status),
                loops=len(components({r: c for r, c in ((r, solver.Value(x[r])) for r in runs) if c}))
                if status in (cp_model.OPTIMAL, cp_model.FEASIBLE) else None,
                needed_seconds=lower, best_found=best, proven=proven, available_seconds=(k-1)*PERIOD,
                impossible=lower > (k-1)*PERIOD, runs=len(runs), span_seconds=round(span_seconds, 1), chain=used)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--variants', type=Path, required=True)
    parser.add_argument('--mode', required=True)
    parser.add_argument('--volumes', type=int, nargs='+', required=True)
    parser.add_argument('--k', type=int, nargs='+', default=[2, 3, 4])
    parser.add_argument('--gap', type=int, default=0)
    parser.add_argument('--joint', action='store_true', help='also require every shorter run length on the same chain')
    parser.add_argument('--connected', action='store_true', help='require one loop through all trains (iterative cuts)')
    parser.add_argument('--runs-root', type=Path, required=True)
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    reference = json.loads(args.variants.read_text())
    windows = reference_windows(args.runs_root, reference['baselines'])
    options = {}
    for variant, info in reference['variants'].items():
        options.setdefault(info['product'], []).append(variant)
    entry = sections(windows)[0][0]
    mode = UC1Mode.load(args.plan, args.mode)
    results = []
    for volume in args.volumes:
        counts = {p: c for p, c in rounded_counts(volume, mode.shares).items() if c}
        for k in args.k:
            row = dict(trains_per_hour=2*volume, counts=counts, **bound(options, counts, windows, entry, k, args.gap, args.joint, args.connected))
            print(json.dumps({key: v for key, v in row.items() if key != 'chain'}), flush=True)
            results.append(row)
            if row['impossible']:
                break
    if args.output:
        args.output.write_text(json.dumps(results, indent=2)+'\n')


if __name__ == '__main__':
    main()
