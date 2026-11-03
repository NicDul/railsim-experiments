"""Two-step UC1 timetabling prototype: first the order of trains, then the exact times (experiment).

Works on a solve_uc1_binding.py variants file (binding platforms, no rerouting).

Every train, whatever its platform, crosses the single-track entry section (resources used by every
variant before the station) and exit section (after it). On a single-track section trains keep
their order, and a train b following a can enter it no earlier than
    h(a, b) = max over the section's resources of (release_a - grant_b) + gap
seconds after a, counted from departure. Around one period these headways add up to exactly
the period, so for any cyclic order the sum of consecutive headways must be <= 1800 s.

1. bound: the cheapest cyclic order on each section (a small flow problem; platform split free).
   If it exceeds the period on either section, the rate is impossible, for every timetable.
2. orders: list every cyclic order of train types at the entry whose headway sum can still fit
   (depth-first, pruned with the same sums). `--forbid` drops orders containing a given pattern.
3. times: for each remaining order, cheapest first, CP-SAT chooses the exact departures and
   platforms with that entry order fixed (1 s gap, then 0 s); a solution is simulated.

  order_uc1.py --variants DIR/bb3-mainline-fv_pass-variants.json --mode MAINLINE/FV_PASS \\
      --volume 19 --project SIM --state-root STATE --output DIR/order-bb3-mainline-fv_pass-38.json
"""
import argparse
import itertools
import json
import os
from pathlib import Path
import time

from ortools.sat.python import cp_model

from railsim_mcp import server
from railsim_mcp.configuration import Settings
from railsim_mcp.search import SIM_JVM_OPTIONS
from railsim_mcp.uc1 import PERIOD
from railsim_mcp.uc1_modes import UC1Mode, rounded_counts
from railsim_mcp.workspace import ScenarioWorkspace
from solve_uc1_cpsat import reference_windows
from solve_uc1_platforms import simulate


def sections(windows):
    """Resources every variant uses, split into before (entry) and after (exit) the platforms."""
    common = set.intersection(*(set(w) for w in windows.values())) - {r for w in windows.values() for r in w
                                                                        if r.startswith('platform')}
    platform_grant = {v: min(g for r, (_, g, _) in w.items() if r.startswith('platform')) for v, w in windows.items()}
    entry = {r for r in common if all(windows[v][r][1] <= platform_grant[v] for v in windows)}
    return sorted(entry), sorted(common - entry)


def headway(windows, section, a, b, gap):
    return max(windows[a][r][2] - windows[b][r][1] for r in section) + gap


def bound(options, counts, windows, section, gap):
    """Cheapest cyclic order on one section; platform split free. A lower bound on the period used."""
    variants = [v for p in counts for v in options[p]]
    model = cp_model.CpModel()
    n = {v: model.NewIntVar(0, sum(counts.values()), f'n_{v}') for v in variants}
    for p, k in counts.items():
        model.Add(sum(n[v] for v in options[p]) == k)
    x = {(a, b): model.NewIntVar(0, sum(counts.values()), f'x_{a}_{b}') for a in variants for b in variants}
    for v in variants:
        model.Add(sum(x[v, b] for b in variants) == n[v])
        model.Add(sum(x[a, v] for a in variants) == n[v])
    cost = sum(headway(windows, section, a, b, gap)*x[a, b] for a, b in x)
    model.Minimize(cost)
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = 60
    status = solver.Solve(model)
    return dict(seconds=round(solver.ObjectiveValue()), proven=status == cp_model.OPTIMAL,
                platform_split={v: solver.Value(n[v]) for v in variants if solver.Value(n[v])})


def type_headways(options, windows, section, gap):
    """Smallest headway between two train types over all platform choices (keeps the bound valid)."""
    return {(p, q): min(headway(windows, section, a, b, gap) for a in options[p] for b in options[q])
            for p in options for q in options}


def orders(counts, h, limit, forbid=()):
    """Cyclic type orders (canonical rotation) whose headway sum fits in the period, cheapest first."""
    types = sorted(counts)
    incoming = {q: min(h[p, q] for p in types) for q in types}
    n = sum(counts.values())
    found, visited = [], 0
    first = types[0]
    left = dict(counts)
    left[first] -= 1
    sequence = [first]

    def canonical(seq):
        return min(tuple(seq[i:]+seq[:i]) for i in range(len(seq))) == tuple(seq)

    def walk(cost):
        nonlocal visited
        visited += 1
        if len(sequence) == n:
            total = cost + h[sequence[-1], sequence[0]]
            cyclic = ''.join(sequence)*2
            if total <= PERIOD and canonical(sequence) and not any(f in cyclic for f in forbid):
                found.append((total, ''.join(sequence)))
            return
        if len(found) >= limit:
            return
        optimistic = cost + sum(incoming[q]*k for q, k in left.items()) + incoming[first]
        if optimistic > PERIOD:
            return
        for q in types:
            if left[q]:
                left[q] -= 1
                sequence.append(q)
                walk(cost + h[sequence[-2], q])
                sequence.pop()
                left[q] += 1
    walk(0)
    return sorted(found), visited


def solve_order(order, products, options, windows, entry, gap, seconds, workers, platforms=None, slack=False):
    """Exact departures and platforms with the entry order fixed; None if this order cannot work.

    platforms: optional per-position platform ('P1', 'P2', ... or 'any').
    slack: instead of a yes/no answer, find how many seconds the order misses by at least, and the
    train pairs and resources that conflict in that closest timetable.

    With the order known, train i reaches the entry e_i = d_i + g_i (g_i: its entry grant, the same
    for all its platforms), and e_0 < e_1 < ... < e_0 + period. So every departure difference lies in
    a known window of less than one period, which fixes, for each pair and resource, which repetition
    of the other train can conflict: the either-or choice becomes one or two linear cases.
    """
    model = cp_model.CpModel()
    n = len(order)
    index = {p: iter([i for i, q in enumerate(products) if q == p]) for p in set(products)}
    trains = [next(index[p]) for p in order]
    position = {t: k for k, t in enumerate(trains)}
    entry_resource = entry[0]
    grant = {}
    for i in range(n):
        values = {windows[v][entry_resource][1] for v in options[products[i]]}
        if len(values) != 1:
            raise RuntimeError('entry grant differs between platforms of one product')
        grant[i] = values.pop()
    g0 = grant[trains[0]]
    # e_i in [g0, g0 + period - 1]; the first train in the order departs at 0
    departure = {i: model.NewIntVar(g0 - grant[i], g0 + PERIOD - 1 - grant[i], f'd{i}') for i in range(n)}
    model.Add(departure[trains[0]] == 0)
    for k in range(n-1):
        a, b = trains[k], trains[k+1]
        model.Add(departure[b] + grant[b] > departure[a] + grant[a])
    choice = [{v: model.NewBoolVar(f'x{i}_{v}') for v in options[products[i]]} for i in range(n)]
    for i in range(n):
        model.AddExactlyOne(choice[i].values())
    for k, platform in enumerate(platforms or []):
        if platform != 'any':
            wanted = [x for v, x in choice[trains[k]].items() if v.endswith('_'+platform)]
            if not wanted:
                raise ValueError(f'{order[k]} cannot use {platform}')
            model.Add(wanted[0] == 1)
    widen = 600 if slack else 0
    miss = model.NewIntVar(0, widen, 'miss')
    checks = []
    cases = 0
    for i in range(n):
        for j in range(i+1, n):
            if position[i] > position[j]:
                i_, j_ = j, i
            else:
                i_, j_ = i, j
            # e_j - e_i in [1, period - 1]  =>  d_j - d_i in [lo, hi]
            lo = 1 + grant[i_] - grant[j_]
            hi = PERIOD - 1 + grant[i_] - grant[j_]
            for vi, xi in choice[i_].items():
                for vj, xj in choice[j_].items():
                    for resource in set(windows[vi]) & set(windows[vj]):
                        _, a0, a1 = windows[vi][resource]
                        _, b0, b1 = windows[vj][resource]
                        low, high = a1 - b0 + gap, PERIOD - (b1 - a0) - gap
                        # need low <= diff + k*period <= high for some k, with diff in [lo, hi]
                        ks = [k for k in range(-3, 4)
                              if low - widen - k*PERIOD <= hi and high + widen - k*PERIOD >= lo]
                        diff = departure[j_] - departure[i_]
                        checks.append((i_, j_, vi, vj, resource, low, high))
                        if not ks:
                            model.AddBoolOr([xi.Not(), xj.Not()])
                        elif len(ks) == 1:
                            model.Add(diff + miss >= low - ks[0]*PERIOD).OnlyEnforceIf([xi, xj])
                            model.Add(diff - miss <= high - ks[0]*PERIOD).OnlyEnforceIf([xi, xj])
                        else:
                            k = model.NewIntVar(min(ks), max(ks), '')
                            model.Add(diff + PERIOD*k + miss >= low).OnlyEnforceIf([xi, xj])
                            model.Add(diff + PERIOD*k - miss <= high).OnlyEnforceIf([xi, xj])
                        cases += len(ks)
    if slack:
        model.Minimize(miss)
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = seconds
    solver.parameters.num_workers = workers
    started = time.monotonic()
    status = solver.Solve(model)
    result = dict(status=solver.StatusName(status), solve_seconds=round(time.monotonic()-started, 2), linear_cases=cases)
    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        rows = [[products[i], solver.Value(departure[i]) % PERIOD,
                 next(v for v, x in choice[i].items() if solver.Value(x))] for i in range(n)]
        shift = min(r[1] for r in rows)
        result['pattern'] = sorted([[p, t-shift, v] for p, t, v in rows], key=lambda r: (r[1], r[0]))
        if slack:
            result['miss_seconds'] = solver.Value(miss)
            chosen = {i: next(v for v, x in choice[i].items() if solver.Value(x)) for i in range(n)}
            names = {t: f'{order[k][0]}{k+1}' for k, t in enumerate(trains)}  # train by entry position
            conflicts = []
            for i_, j_, vi, vj, resource, low, high in checks:
                if chosen[i_] != vi or chosen[j_] != vj:
                    continue
                diff = solver.Value(departure[j_]) - solver.Value(departure[i_])
                over = min(max(low - (diff + k*PERIOD), (diff + k*PERIOD) - high, 0) for k in range(-3, 4))
                if over > 0:
                    conflicts.append(dict(trains=[names[i_], names[j_]], platforms=[vi.split('_')[-1], vj.split('_')[-1]],
                                          resource=resource, overlap_seconds=over))
            result['conflicts'] = sorted(conflicts, key=lambda c: -c['overlap_seconds'])[:6]
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--variants', type=Path, required=True)
    parser.add_argument('--mode', required=True)
    parser.add_argument('--volume', type=int, required=True, help='trains per 30 minutes')
    parser.add_argument('--project', required=True)
    parser.add_argument('--state-root', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--max-orders', type=int, default=2_000_000, help='stop listing orders after this many')
    parser.add_argument('--try-orders', type=int, default=0, help='orders to try in step 3 (0: none)')
    parser.add_argument('--seconds', type=float, default=20, help='CP-SAT limit per order')
    parser.add_argument('--workers', type=int, default=8)
    parser.add_argument('--forbid', nargs='*', default=[], help='type patterns such as GG that no order may contain')
    parser.add_argument('--most-mixed-first', action='store_true',
                        help='try orders with the most changes of train type first')
    parser.add_argument('--seed', type=int, default=4711)
    args = parser.parse_args()
    settings = Settings.load(args.project, args.state_root)
    ws = ScenarioWorkspace(settings.project/'src/main/resources/scenarios', settings.state/'workspaces',
                           project_root=settings.project, runs_root=settings.state/'runs')
    server.WORKSPACES = ws
    os.environ['MAVEN_OPTS'] = (os.environ.get('MAVEN_OPTS', '')+' '+SIM_JVM_OPTIONS).strip()
    reference = json.loads(args.variants.read_text())
    windows = reference_windows(ws.runs_root, reference['baselines'])
    options = {}
    for variant, info in reference['variants'].items():
        options.setdefault(info['product'], []).append(variant)
    mode = UC1Mode.load(settings.project/'src/main/resources/scenarios/use_case_1/uc1_operational_plan.json', args.mode)
    counts = rounded_counts(args.volume, mode.shares)
    counts = {p: k for p, k in counts.items() if k}
    products = [p for p in sorted(counts) for _ in range(counts[p])]
    entry, exit_ = sections(windows)
    single_platform = all(len(options[p]) == 1 for p in counts)
    letters = {p: p[0] for p in counts}
    report = dict(variants=str(args.variants), mode=mode.id, volume=args.volume, trains_per_hour=2*args.volume,
                  counts=counts, entry_section=entry, exit_section=exit_, forbid=args.forbid)
    started = time.monotonic()

    # 1. bound
    checked = [('entry', entry), ('exit', exit_)]
    if single_platform:  # no overtaking anywhere: the whole shared path keeps the order
        checked.append(('path', sorted(set(entry) | set(exit_) | {r for w in windows.values() for r in w})))
    report['bound'] = {name: {gap: bound(options, counts, windows, section, gap) for gap in (0, 1)}
                       for name, section in checked}
    print(json.dumps(dict(step='bound', **report['bound'])), flush=True)
    if any(b[0]['seconds'] > PERIOD and b[0]['proven'] for b in report['bound'].values()):
        report['verdict'] = 'impossible: the cheapest order on a single-track section needs more than 1800 s'
    else:
        # 2. orders at the entry (types only; platforms and exact times are left to step 3)
        h = type_headways(options, windows, entry, 0)
        h = {(letters[p], letters[q]): v for (p, q), v in h.items()}
        found, visited = orders({letters[p]: k for p, k in counts.items()}, h, args.max_orders, args.forbid)
        report['orders'] = dict(listed=len(found), complete=len(found) < args.max_orders, nodes_visited=visited,
                                list_seconds=round(time.monotonic()-started, 1),
                                cheapest=found[:10], most_expensive=found[-3:])
        print(json.dumps(dict(step='orders', **{k: v for k, v in report['orders'].items() if k != 'most_expensive'})), flush=True)
        if not found and report['orders']['complete']:
            report['verdict'] = 'impossible: no order of train types fits at the entry'
        # 3. times for the cheapest orders
        report['tries'] = []
        if args.most_mixed_first:
            found.sort(key=lambda o: (-sum(a != b for a, b in zip(o[1], o[1][1:]+o[1][0])), o[0]))
        reference_terminals = {v: b['terminal'] for v, b in reference['baselines'].items()}
        for total, sequence in found[:args.try_orders]:
            order = [next(p for p in counts if letters[p] == c) for c in sequence]
            for gap in (0, 1):  # 0 s first: if even that is impossible, so is 1 s
                row = dict(order=sequence, entry_headway_sum=total, gap=gap,
                           **solve_order(order, products, options, windows, entry, gap, args.seconds, args.workers))
                if 'pattern' in row:
                    row['simulation'], _ = simulate(ws, reference['workspace'], row['pattern'], reference_terminals, args.seed)
                report['tries'].append(row)
                print(json.dumps({k: v for k, v in row.items() if k != 'pattern'}), flush=True)
                if 'pattern' not in row or row['simulation']['passed']:
                    break
            if report['tries'][-1].get('simulation', {}).get('passed'):
                report['verdict'] = f"possible: order {sequence} passes the simulator"
                break
    tries = report.get('tries', [])
    if 'verdict' not in report and tries and 'orders' in report:
        settled = {t['order'] for t in tries if t['status'] == 'INFEASIBLE' and t['gap'] == 0}
        tried = {t['order'] for t in tries}
        if report['orders']['complete'] and len(settled) == report['orders']['listed']:
            report['verdict'] = 'impossible: every order of train types was ruled out'
        else:
            report['verdict'] = (f"open: {len(settled)} of {report['orders']['listed']} orders ruled out, "
                                 f"{len(tried - settled)} undecided or late in the simulator, "
                                 f"{report['orders']['listed'] - len(tried)} not tried")
    report['elapsed_seconds'] = round(time.monotonic()-started)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2)+'\n')


if __name__ == '__main__':
    main()
