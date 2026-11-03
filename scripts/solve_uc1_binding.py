"""Simplest UC1 capacity check: binding platforms, feasibility only, zero gap (experiment).

In a copy of the building block, railsim's entry/exit markers are removed, so trains keep the
platform the timetable gives them (no automatic rerouting). Every product route gets one variant
per platform, each measured once in isolation; its planned times are that variant's own times.

For each rate, from low to high, CP-SAT looks for any departure time and platform per train such
that no two trains hold the same section, switch or platform at once, with at least --min-gap
seconds between them (default 1 s: railsim reserves the next resource a moment early and works in
whole seconds, so 0 s often costs 1 s of delay), including the wrap into the next period. There is
no optimisation. The answer is simulated; only a zero-delay run counts.
If a train is late, the claim/hold rule from solve_uc1_cpsat.py is learned and the rate re-solved.
Learned rules apply to every pair of trains on those variants, so they are kept for one rate only.
The search stops at the first rate that cannot be made to pass.

  solve_uc1_binding.py --building-block BB3 --mode MAINLINE/FV_PASS \\
      --project SIM --state-root STATE --output-dir DIR
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import time
import uuid

from ortools.sat.python import cp_model

from railsim_mcp import server
from railsim_mcp.configuration import Settings
from railsim_mcp.search import SIM_JVM_OPTIONS
from railsim_mcp.uc1 import PERIOD
from railsim_mcp.uc1_modes import UC1Mode, rounded_counts
from railsim_mcp.workspace import ScenarioWorkspace
from solve_uc1_cpsat import reference_windows
from solve_uc1_platforms import add_platform_variants, calibrate, simulate

MARKER = re.compile(r'\s*<attribute name="railsim(Entry|Exit)" class="java.lang.Boolean">true</attribute>')


def solve(products, options, windows, seconds, rules, workers, min_gap, no_overlap=False):
    """Any departure and platform variant per train; exclusive holds at least min_gap seconds apart.

    Identical trains are interchangeable, so they are kept in departure order, and the first train
    departs at 0 (every timetable can be shifted so that it does). Each pair of possible holds gets
    one disjunction; no_overlap adds the same condition once per resource, which propagates better
    on some instances and worse on others.
    """
    model = cp_model.CpModel()
    n = len(products)
    departure = [model.NewIntVar(0, PERIOD-1, f'd{i}') for i in range(n)]
    choice = [{v: model.NewBoolVar(f'x{i}_{v}') for v in options[products[i]]} for i in range(n)]
    model.Add(departure[0] == 0)
    for i in range(n):
        model.AddExactlyOne(choice[i].values())
        if i and products[i] == products[i-1]:
            model.Add(departure[i] > departure[i-1])

    def apart(i, j, first, second, when):
        # first (window of train i) and second (window of train j) never overlap, in any period:
        # a1 - b0 + gap <= d_j - d_i + k*PERIOD <= PERIOD - (b1 - a0) - gap for some integer k.
        (a0, a1), (b0, b1) = first, second
        low, high = a1 - b0 + min_gap, PERIOD - (b1 - a0) - min_gap
        if low > high:
            model.AddBoolOr([w.Not() for w in when])
            return
        smallest = -(-(low - (PERIOD-1)) // PERIOD)  # departure differences lie in -(P-1)..P-1
        largest = (high + PERIOD - 1) // PERIOD
        k = model.NewIntVar(smallest, largest, '')
        gap = departure[j] - departure[i] + PERIOD*k
        model.Add(gap >= low).OnlyEnforceIf(when)
        model.Add(gap <= high).OnlyEnforceIf(when)

    for i in range(n):
        for j in range(i+1, n):
            for vi, xi in choice[i].items():
                for vj, xj in choice[j].items():
                    for resource in set(windows[vi]) & set(windows[vj]):
                        ci, gi, ri = windows[vi][resource]
                        cj, gj, rj = windows[vj][resource]
                        apart(i, j, (gi, ri), (gj, rj), [xi, xj])
                        if (vi, vj, resource) in rules:
                            apart(i, j, (gi, ri), (cj, rj), [xi, xj])
                        if (vj, vi, resource) in rules:
                            apart(i, j, (ci, ri), (gj, rj), [xi, xj])
    if no_overlap:
        # Holds repeat every period: three copies cover every relative shift, since departures lie
        # within one period and every hold is shorter than it.
        holds = {}
        for i in range(n):
            for v, x in choice[i].items():
                for resource, (_, g, r) in windows[v].items():
                    for m in range(3):
                        holds.setdefault(resource, []).append(model.NewOptionalFixedSizeIntervalVar(
                            departure[i] + g + m*PERIOD, max(r - g + min_gap, 1), x, ''))
        for intervals in holds.values():
            model.AddNoOverlap(intervals)
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = seconds
    solver.parameters.num_workers = workers
    started = time.monotonic()
    status = solver.Solve(model)
    result = dict(status=solver.StatusName(status), solve_seconds=round(time.monotonic()-started, 2))
    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        rows = [[products[i], solver.Value(departure[i]), next(v for v, x in choice[i].items() if solver.Value(x))]
                for i in range(n)]
        result['pattern'] = sorted(rows, key=lambda r: (r[1], r[0]))
    return result


def learn(run, pattern, windows):
    """(holder, claimer, resource) where a late train's look-ahead claim met another train's hold."""
    late = {row['vehicle_id'] for row in run['trains'] if (row.get('terminal_delay_seconds') or 0) > 0}
    learned = set()
    for vehicle in late:
        _, dv, claimer = pattern[int(vehicle.split('_')[1])]
        for u, (_, du, holder) in enumerate(pattern):
            if pattern[u][2] == claimer and du == dv:
                continue
            for resource in set(windows[claimer]) & set(windows[holder]):
                c, g, _ = windows[claimer][resource]
                _, hg, hr = windows[holder][resource]
                if c < g and any(dv+c < du+hr+s*PERIOD and du+hg+s*PERIOD < dv+g for s in range(-3, 4)):
                    learned.add((holder, claimer, resource))
    return learned


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--building-block', required=True, choices=['BB1', 'BB2', 'BB3'])
    parser.add_argument('--mode', required=True)
    parser.add_argument('--project', required=True)
    parser.add_argument('--state-root', required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--first-volume', type=int, default=4, help='trains per 30 min to start from')
    parser.add_argument('--last-volume', type=int, default=24)
    parser.add_argument('--seconds', type=float, default=120, help='CP-SAT time limit per solve')
    parser.add_argument('--min-gap', type=int, default=1,
                        help='seconds between exclusive holds; a rate that fails with it is retried with 0 s')
    parser.add_argument('--resource-no-overlap', action='store_true',
                        help='add one no-overlap constraint per resource to the pairwise model')
    parser.add_argument('--max-learning', type=int, default=8)
    parser.add_argument('--workers', type=int, default=3)
    parser.add_argument('--seed', type=int, default=4711)
    args = parser.parse_args()
    settings = Settings.load(args.project, args.state_root)
    ws = ScenarioWorkspace(settings.project/'src/main/resources/scenarios', settings.state/'workspaces',
                           project_root=settings.project, runs_root=settings.state/'runs')
    server.WORKSPACES = ws
    os.environ['MAVEN_OPTS'] = (os.environ.get('MAVEN_OPTS', '')+' '+SIM_JVM_OPTIONS).strip()
    mode = UC1Mode.load(settings.project/'src/main/resources/scenarios/use_case_1/uc1_operational_plan.json', args.mode)
    name = f"{args.building_block}-{mode.id}".lower().replace('/', '-')
    args.output_dir.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()

    reference_path = args.output_dir/f'{name}-variants.json'
    if reference_path.exists():
        reference = json.loads(reference_path.read_text())
    else:
        workspace = 'bind-'+name[:20]+'-'+uuid.uuid4().hex[:8]
        root = ws.create(workspace, 'UC1', args.building_block)
        network = root/'network.xml'
        text = network.read_text()
        stripped = MARKER.sub('', text)
        if 'railsimEntry' in stripped or 'railsimExit' in stripped or len(MARKER.findall(text)) != 2:
            raise RuntimeError('expected exactly one entry and one exit marker')
        network.write_text(stripped)
        variants = add_platform_variants(root/'schedule.xml', network, dict(mode.routes))
        baselines = {v: calibrate(ws, workspace, v, info['product'], args.seed) for v, info in variants.items()}
        source = settings.project/'src/main/resources/scenarios/use_case_1'/f'building_block_{args.building_block[-1]}'
        digest = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
        reference = dict(building_block=args.building_block, mode=mode.record(), workspace=workspace, seed=args.seed,
                         network_change='railsimEntry/railsimExit markers removed (no automatic rerouting)',
                         network_equal_apart_from_markers=stripped == MARKER.sub('', source.joinpath('network.xml').read_text()),
                         config_unchanged=digest(root/'config.xml') == digest(source/'config.xml'),
                         variants=variants, baselines=baselines)
        reference_path.write_text(json.dumps(reference, indent=2)+'\n')
    windows = reference_windows(ws.runs_root, reference['baselines'])
    options = {}
    for variant, info in reference['variants'].items():
        options.setdefault(info['product'], []).append(variant)
    terminals = {v: b['terminal'] for v, b in reference['baselines'].items()}

    gaps = [args.min_gap, 0] if args.min_gap else [0]
    report = dict(reference=str(reference_path), building_block=args.building_block, mode=mode.id,
                  min_gap_seconds=gaps, no_overlap=args.resource_no_overlap, solve_time_limit_seconds=args.seconds,
                  windows=windows, attempts=[], best=None, stopped_at=None)
    for volume in range(args.first_volume, args.last_volume+1):
        counts = rounded_counts(volume, mode.shares)
        products = [p for p in sorted(counts) for _ in range(counts[p])]
        passed = False
        for min_gap in gaps:  # 0 s only where the safer gap does not work
            rules = set()
            for _ in range(args.max_learning+1):
                row = dict(volume=volume, trains_per_hour=volume*2, min_gap=min_gap, rules=len(rules),
                           **solve(products, options, windows, args.seconds, frozenset(rules), args.workers, min_gap,
                                   args.resource_no_overlap))
                if 'pattern' in row:
                    row['simulation'], run = simulate(ws, reference['workspace'], row['pattern'], terminals, args.seed)
                    if row['simulation']['detours']:
                        raise RuntimeError('a train was rerouted although the markers were removed')
                    new = learn(run, row['pattern'], windows) - rules
                    row['learned'] = sorted(new)
                report['attempts'].append(row)
                print(json.dumps({k: v for k, v in row.items() if k != 'pattern'}), flush=True)
                if 'pattern' not in row or row['simulation']['passed'] or not row['learned']:
                    break
                rules |= new
            if 'simulation' in row and row['simulation']['passed']:
                passed = True
                break
        if not passed:
            last = report['attempts'][-1]
            reason = ('no timetable exists under the occupation model' if last['status'] == 'INFEASIBLE' and not last['rules']
                      else 'no timetable under the learned claim rules (not a proof)' if last['status'] == 'INFEASIBLE'
                      else 'solver time limit, undecided' if last['status'] == 'UNKNOWN'
                      else 'simulation still late after learning')
            report['stopped_at'] = dict(volume=volume, trains_per_hour=volume*2, min_gap=last['min_gap'], reason=reason)
            break
        report['best'] = dict(report['attempts'][-1], rule_list=sorted(rules))
    report['elapsed_seconds'] = round(time.monotonic()-started)
    (args.output_dir/f'{name}.json').write_text(json.dumps(report, indent=2)+'\n')


if __name__ == '__main__':
    main()
