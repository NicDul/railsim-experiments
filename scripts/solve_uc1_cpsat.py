"""Exact periodic timetabling for UC1 with OR-Tools CP-SAT; simulation validates.

Zero exit delay means every train runs exactly as in its isolated reference run,
so its exclusive hold on each capacity-one resource is the reference hold
shifted by its departure. A timetable can only be delay-free without recovery if
no two windows on a resource overlap, including across the period wrap. CP-SAT
either finds such departures (then the simulator checks them) or proves that
none exist under this no-hold model. A proof is not a simulator proof: a train
that is briefly held and still exits on time is outside the model.

  solve_uc1_cpsat.py --checkpoint search.json --project SIM --state-root STATE \\
      --volumes 12 13 14 [--simulate | --simulate-best] [--output result.json]

With --calibrate --building-block BB2 --mode MAINLINE/FV_PASS a missing checkpoint
is created first: one isolated reference run per product in a frozen workspace.
"""
import argparse
import json
import math
import os
from pathlib import Path
import shutil
import time
import uuid

from ortools.sat.python import cp_model

from railsim_mcp import server
from railsim_mcp.configuration import Settings
from railsim_mcp.search import evaluate, Search, SIM_JVM_OPTIONS
from railsim_mcp.search_evidence import reference_context, layout
from railsim_mcp.uc1 import events
from railsim_mcp.uc1 import PERIOD, pattern_departures
from railsim_mcp.uc1_modes import rounded_counts
from railsim_mcp.workspace import ScenarioWorkspace


def reference_windows(runs_root, baselines):
    """product -> resource -> (claim, granted, released) seconds after departure, whole seconds.

    granted..released is the exclusive hold (railsim EXHAUSTED, as in the gap template).
    claim is the earliest deadlock-avoidance (DLA) look-ahead claim on the resource, or
    granted when there is none. In railsim a DLA claim cannot be placed on a resource
    another train holds exclusively, but two claims can coexist.
    """
    offsets = reference_context(runs_root, baselines)['exclusive_reference_offsets']
    windows = {}
    for product, baseline in baselines.items():
        run = json.loads((runs_root/baseline['run_id']/'run.json').read_text())
        links = layout(Path(run['snapshot_directory'])/'network.xml')
        claims = {}
        for event in events(run['events_file']):
            if (event['type'] == 'railsimLinkStateChangeEvent' and event.get('dla') == 'true'
                    and event.get('link') in links and event['state'] != 'EMPTY'):
                resource = links[event['link']]
                claims[resource] = min(claims.get(resource, math.inf), float(event['time']))
        windows[product] = {resource: (math.floor(min(claims.get(resource, w['granted']), w['granted'])),
                                       math.floor(w['granted']), math.ceil(w['released']))
                            for resource, w in offsets[product].items()}
    return windows


def solve(products, windows, seconds, rules=frozenset(), workers=8):
    """Departures maximizing the smallest gap between conflicting windows on any shared resource.

    Exclusive holds never overlap. For a learned rule (holder, claimer, resource) the
    claimer's DLA claim..release must also avoid the holder's exclusive hold.
    """
    model = cp_model.CpModel()
    n = len(products)
    departure = [model.NewIntVar(0, PERIOD-1, f'd{i}') for i in range(n)]
    margin = model.NewIntVar(0, PERIOD, 'margin')
    model.Add(departure[0] == 0)  # the timetable is cyclic: fix one train
    for i in range(1, n):
        if products[i] == products[i-1]:
            model.Add(departure[i] > departure[i-1])  # identical trains: break symmetry
    pairs = 0
    def apart(i, j, first, second, name):
        # (d_j - d_i) mod PERIOD in [end_A - start_B + margin, PERIOD - (end_B - start_A) - margin]
        (a0, a1), (b0, b1) = first, second
        k = model.NewIntVar(-3, 3, name)
        gap = departure[j] - departure[i] + PERIOD*k
        model.Add(gap >= a1 - b0 + margin)
        model.Add(gap <= PERIOD - (b1 - a0) - margin)
    for i in range(n):
        for j in range(i+1, n):
            pi, pj = products[i], products[j]
            for resource in set(windows[pi]) & set(windows[pj]):
                ci, gi, ri = windows[pi][resource]
                cj, gj, rj = windows[pj][resource]
                apart(i, j, (gi, ri), (gj, rj), f'k{i}_{j}_{resource}')
                if (pi, pj, resource) in rules:
                    apart(i, j, (gi, ri), (cj, rj), f'h{i}_{j}_{resource}')
                if (pj, pi, resource) in rules:
                    apart(i, j, (ci, ri), (gj, rj), f'c{i}_{j}_{resource}')
                pairs += 1
    model.Maximize(margin)
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = seconds
    solver.parameters.num_workers = workers
    started = time.monotonic()
    status = solver.Solve(model)
    result = dict(status=solver.StatusName(status), pair_constraints=pairs, solve_seconds=round(time.monotonic()-started, 2))
    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        result.update(margin_seconds=solver.Value(margin), margin_proven_optimal=status == cp_model.OPTIMAL,
                      pattern=sorted([[products[i], solver.Value(departure[i])] for i in range(n)], key=lambda r: (r[1], r[0])))
    return result


def learn(run, pattern, windows):
    """(holder, claimer, resource) where a delayed train's DLA claim overlapped another train's hold."""
    late = {row['vehicle_id'] for row in run['trains'] if (row.get('terminal_delay_seconds') or 0) > 0}
    learned = set()
    for vehicle in late:
        v = int(vehicle.split('_')[1])
        claimer, dv = pattern[v]
        for u, (holder, du) in enumerate(pattern):
            if u == v:
                continue
            for resource in set(windows[claimer]) & set(windows[holder]):
                c, g, _ = windows[claimer][resource]
                _, hg, hr = windows[holder][resource]
                if c >= g:
                    continue
                for shift in range(-3, 4):
                    start, end = dv + c, dv + g
                    hold_start, hold_end = du + hg + shift*PERIOD, du + hr + shift*PERIOD
                    if start < hold_end and hold_start < end:
                        learned.add((holder, claimer, resource))
    return learned


def simulate(ws, settings, source_workspace, pattern, routes, baselines, seed):
    """Run the timetable in a fresh copy of the frozen workspace and score it like the search."""
    workspace = 'cpsat-' + uuid.uuid4().hex[:12]
    shutil.copytree(ws.workspace_root/source_workspace, ws.workspace_root/workspace)
    server.replace_timetable(workspace, pattern_departures(pattern, routes), 6, PERIOD)
    run = ws.run_once(workspace, seed, 12600)
    expected = {f'{p}_{i:02}_p{k}': (t+k*PERIOD, p) for k in (2, 3) for i, (p, t) in enumerate(pattern)}
    score = evaluate(run, expected, {p: b['terminal'] for p, b in baselines.items()})
    return dict(run_id=run['run_id'], workspace=workspace, passed=score['passed'], max_delay=score['max_delay'],
                total_delay=score['total_delay'], completed=score['completed'], expected=score['expected']), run


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--checkpoint', type=Path, required=True, help='search checkpoint with frozen baselines')
    parser.add_argument('--project', required=True)
    parser.add_argument('--state-root', required=True)
    parser.add_argument('--volumes', type=int, nargs='+', required=True, help='trains per half-hour period')
    parser.add_argument('--seconds', type=float, default=60, help='CP-SAT time limit per volume')
    parser.add_argument('--simulate', action='store_true', help='simulate every solution found')
    parser.add_argument('--simulate-best', action='store_true',
                        help='simulate solutions from the highest feasible volume down until one passes')
    parser.add_argument('--max-learning', type=int, default=6, help='learned-rule rounds per volume')
    parser.add_argument('--calibrate', action='store_true', help='create the checkpoint if missing')
    parser.add_argument('--building-block', default='BB1', choices=['BB1', 'BB2', 'BB3'])
    parser.add_argument('--mode', help='declared UC1 mode for --calibrate')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    settings = Settings.load(args.project, args.state_root)
    os.environ['MAVEN_OPTS'] = (os.environ.get('MAVEN_OPTS', '')+' '+SIM_JVM_OPTIONS).strip()
    if args.calibrate and not args.checkpoint.exists():
        # Reference runs only: the search loop itself is never started.
        search = Search(settings, args.checkpoint, policy='repair', simulations=10, mode=args.mode,
                        building_block=args.building_block)
        search.setup()
        search.save()
    checkpoint = json.loads(args.checkpoint.read_text())
    ws = ScenarioWorkspace(settings.project/'src/main/resources/scenarios', settings.state/'workspaces',
                           project_root=settings.project, runs_root=settings.state/'runs')
    server.WORKSPACES = ws
    mode = checkpoint['mode_definition']
    windows = reference_windows(ws.runs_root, checkpoint['baselines'])
    report = dict(checkpoint=str(args.checkpoint), building_block=checkpoint.get('building_block', 'BB1'),
                  mode=mode['id'], windows=windows, volumes=[])
    for volume in args.volumes:
        counts = rounded_counts(volume, mode['shares'])
        products = [p for p in sorted(counts) for _ in range(counts[p])]
        row = dict(volume=volume, trains_per_hour=volume*2, composition=counts, **solve(products, windows, args.seconds))
        if args.simulate and 'pattern' in row:
            row['simulation'] = simulate(ws, settings, checkpoint['workspace'], row['pattern'], mode['routes'],
                                         checkpoint['baselines'], checkpoint['seed'])[0]
        report['volumes'].append(row)
        print(json.dumps({k: v for k, v in row.items() if k != 'pattern'}), flush=True)
    if args.simulate_best:
        # Highest feasible volume first. A failed simulation teaches claim/hold rules
        # for the delayed trains, then the same volume is solved again.
        rules, report['learning'] = set(), []
        candidates = sorted((r['volume'] for r in report['volumes'] if 'pattern' in r), reverse=True)
        for volume in candidates:
            counts = rounded_counts(volume, mode['shares'])
            products = [p for p in sorted(counts) for _ in range(counts[p])]
            for attempt in range(args.max_learning+1):
                row = dict(volume=volume, trains_per_hour=volume*2, composition=counts, rules=sorted(rules),
                           **solve(products, windows, args.seconds, frozenset(rules)))
                if 'pattern' not in row:
                    report['learning'].append(row)
                    print(json.dumps(dict(volume=volume, status=row['status'], rules=len(rules))), flush=True)
                    break
                row['simulation'], run = simulate(ws, settings, checkpoint['workspace'], row['pattern'], mode['routes'],
                                                  checkpoint['baselines'], checkpoint['seed'])
                new = learn(run, row['pattern'], windows) - rules
                row['learned'] = sorted(new)
                report['learning'].append(row)
                print(json.dumps(dict(volume=volume, margin=row['margin_seconds'], passed=row['simulation']['passed'],
                                      max_delay=row['simulation']['max_delay'], learned=sorted(new))), flush=True)
                if row['simulation']['passed'] or not new:
                    break
                rules |= new
            if report['learning'][-1].get('simulation', {}).get('passed'):
                report['best'] = report['learning'][-1]
                break
        report['rules'] = sorted(rules)
    if args.output:
        args.output.write_text(json.dumps(report, indent=2)+'\n')


if __name__ == '__main__':
    main()
