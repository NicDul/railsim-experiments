"""CP-SAT timetabling for UC1 where every train may use any platform (experiment).

In a copy of the building block, each product's declared route gets one variant
per platform: the same entry and exit, the platform's own path through the
station and, for stopping routes, the same stop moved to that platform. The
scenario files, network, vehicles and simulator settings are not changed.
Each variant is measured once in isolation and its route profile frozen to the
measured times. CP-SAT then chooses a departure and a platform variant per train
so that exclusive holds never overlap (plus claim/hold rules learned from failed
simulations, as in solve_uc1_cpsat.py), and every answer is simulated.

  solve_uc1_platforms.py --building-block BB3 --mode MAINLINE/FV_PASS \\
      --project SIM --state-root STATE --output-dir DIR --volumes 10 11 12 ...
"""
import argparse
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import time
import uuid
import xml.etree.ElementTree as ET

from ortools.sat.python import cp_model

from railsim_mcp import server
from railsim_mcp.configuration import Settings
from railsim_mcp.search import evaluate, SIM_JVM_OPTIONS
from railsim_mcp.search_evidence import layout
from railsim_mcp.uc1 import PERIOD, events, stamp, trace
from railsim_mcp.uc1_modes import UC1Mode, rounded_counts
from railsim_mcp.workspace import ScenarioWorkspace
from solve_uc1_cpsat import reference_windows


def link_graph(network):
    links = {}
    for link in ET.parse(network).getroot().iter('link'):
        attrs = {a.get('name'): a.text for a in link.iter('attribute')}
        links[link.get('id')] = dict(start=link.get('from'), end=link.get('to'), resource=attrs.get('railsimResourceId'))
    return links


def paths(links, first, last):
    """All simple link paths from link `first` to link `last` (the station area is acyclic)."""
    found = []
    def walk(path):
        if path[-1] == last:
            found.append(path)
            return
        for nxt, link in links.items():
            if link['start'] == links[path[-1]]['end'] and nxt not in path and not nxt.startswith('loop'):
                walk(path+[nxt])
    walk([first])
    return found


def add_platform_variants(schedule_path, network_path, base_routes):
    """Add <route>_P<platform> variants for every base route; return variant -> (product, platform)."""
    text = schedule_path.read_text()
    prolog = text[:text.index('<transitSchedule')]
    root = ET.fromstring(text)
    links = link_graph(network_path)
    platforms = sorted(l for l, v in links.items() if (v['resource'] or '').startswith('platform'))
    stops = {f.get('linkRefId'): f.get('id') for f in root.iter('stopFacility')}
    routes = {r.get('id'): r for r in root.iter('transitRoute')}
    line_of = {r.get('id'): line for line in root.iter('transitLine') for r in line.iter('transitRoute')}
    variants = {}
    for product, base_id in base_routes.items():
        base = routes[base_id]
        sequence = [l.get('refId') for l in base.find('route')]
        # entry/exit outside the station: the first and last link shared by every path
        inner = [l for l in sequence if not l.startswith('loop')]
        candidates = paths(links, inner[0], inner[-1])
        for platform in platforms:
            options = [p for p in candidates if platform in p]
            if not options:
                continue
            path = min(options, key=len)
            variant = copy.deepcopy(base)
            variant_id = f'{base_id}_P{platforms.index(platform)+1}'
            variant.set('id', variant_id)
            route = variant.find('route')
            for child in list(route):
                route.remove(child)
            for link in ['loop01']+path+['loop02']:
                ET.SubElement(route, 'link', refId=link)
            for stop in variant.find('routeProfile'):
                if stop.get('refId') not in ('L1', 'R1'):
                    stop.set('refId', stops[platform])  # the M stop moves to the chosen platform
            departures = variant.find('departures')
            if departures is not None:
                for child in list(departures):
                    departures.remove(child)
            line_of[base_id].append(variant)
            variants[variant_id] = dict(product=product, platform=platform, base=base_id, links=path)
    schedule_path.write_text(prolog+ET.tostring(root, encoding='unicode')+'\n')
    return variants


def calibrate(ws, workspace, variant, product, seed):
    """Isolated run of one variant; freeze its route profile to the measured stop times."""
    server.replace_timetable(workspace, [dict(route_id=variant, departure_id=f'{product}_00', vehicle_id=f'{product}_00',
                                              vehicle_type=product, departure_time='00:00:00')], 1, PERIOD)
    run = ws.run_once(workspace, seed, 12600)
    row = run['trains'][0]
    if row['status'] != 'terminal_arrived':
        raise RuntimeError(f'isolated {variant} did not complete')
    trajectory = trace(run['events_file'])[product+'_00']
    route = ws.get_entity(workspace, 'transitRoute', variant)
    profile = copy.deepcopy(next(c for c in route['children'] if c['type'] == 'routeProfile'))
    for stop in profile['children']:
        for e in trajectory['facilities']:
            if e['facility'] == stop['attributes']['refId']:
                key = 'arrivalOffset' if e['type'] == 'VehicleArrivesAtFacility' else 'departureOffset'
                stop['attributes'][key] = stamp(float(e['time']))
    def clean(record):
        return {k: ([clean(c) for c in v] if k == 'children' else v) for k, v in record.items() if v is not None}
    ws.edit(workspace, [dict(action='update', entity_type='transitRoute', entity_id=variant,
                             replace_children={'routeProfile': [clean(profile)]})])
    detours = sum(1 for e in events(run['events_file']) if e['type'] == 'railsimDetourEvent')
    return dict(run_id=run['run_id'], terminal=row['actual_arrival'], detours=detours)


def solve(products, options, windows, seconds, rules=frozenset(), workers=8):
    """Choose a departure and one variant per train; exclusive holds never overlap modulo the period.

    rules holds (holder, claimer, resource) claim rules and ('entry', holder, claimer, resource, e)
    entry checks: at e seconds after its departure the claimer checks its whole path, so no other
    train may hold the resource then.
    """
    entry = {}
    for rule in rules:
        if rule[0] == 'entry':
            entry.setdefault(tuple(rule[1:4]), set()).add(rule[4])
    model = cp_model.CpModel()
    n = len(products)
    departure = [model.NewIntVar(0, PERIOD-1, f'd{i}') for i in range(n)]
    choice = [{v: model.NewBoolVar(f'x{i}_{v}') for v in options[products[i]]} for i in range(n)]
    margin = model.NewIntVar(0, PERIOD, 'margin')
    model.Add(departure[0] == 0)
    for i in range(n):
        model.AddExactlyOne(choice[i].values())
        if i and products[i] == products[i-1]:
            model.Add(departure[i] > departure[i-1])
    def apart(i, j, first, second, when, name):
        (a0, a1), (b0, b1) = first, second
        k = model.NewIntVar(-3, 3, name)
        gap = departure[j] - departure[i] + PERIOD*k
        model.Add(gap >= a1 - b0 + margin).OnlyEnforceIf(when)
        model.Add(gap <= PERIOD - (b1 - a0) - margin).OnlyEnforceIf(when)
    count = 0
    for i in range(n):
        for j in range(i+1, n):
            for vi, xi in choice[i].items():
                for vj, xj in choice[j].items():
                    for resource in set(windows[vi]) & set(windows[vj]):
                        ci, gi, ri = windows[vi][resource]
                        cj, gj, rj = windows[vj][resource]
                        apart(i, j, (gi, ri), (gj, rj), [xi, xj], f'k{i}_{j}_{vi}_{vj}_{resource}')
                        if (vi, vj, resource) in rules:
                            apart(i, j, (gi, ri), (cj, rj), [xi, xj], f'h{i}_{j}_{vi}_{vj}_{resource}')
                        if (vj, vi, resource) in rules:
                            apart(i, j, (ci, ri), (gj, rj), [xi, xj], f'c{i}_{j}_{vi}_{vj}_{resource}')
                        for e in entry.get((vi, vj, resource), ()):
                            apart(i, j, (gi, ri), (e, e+1), [xi, xj], f'e{i}_{j}_{vi}_{vj}_{resource}_{e}')
                        for e in entry.get((vj, vi, resource), ()):
                            apart(i, j, (e, e+1), (gj, rj), [xi, xj], f'f{i}_{j}_{vi}_{vj}_{resource}_{e}')
                        count += 1
    model.Maximize(margin)
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = seconds
    solver.parameters.num_workers = workers
    started = time.monotonic()
    status = solver.Solve(model)
    result = dict(status=solver.StatusName(status), conditional_pairs=count, solve_seconds=round(time.monotonic()-started, 2))
    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        rows = [[products[i], solver.Value(departure[i]), next(v for v, x in choice[i].items() if solver.Value(x))]
                for i in range(n)]
        result.update(margin_seconds=solver.Value(margin), margin_proven_optimal=status == cp_model.OPTIMAL,
                      pattern=sorted(rows, key=lambda r: (r[1], r[0])))
    return result


def learn(run, pattern, windows, avoided_by=None):
    """Rules from a failed simulation: claim/hold rules for late trains and entry checks for rerouted ones.

    At the entry switch railsim checks a train's whole path to the exit; if another train has any of
    it reserved, even a resource needed minutes later, the train is rerouted to another platform,
    where it can make a third train late. Each reroute adds an entry check against the trains reserving
    a resource of the planned path that the detour avoids; shared resources cannot cause a reroute.
    `avoided_by(variant, event)` gives those resources (default: every resource of the variant).
    """
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
    for event in events(run['events_file']):
        if event['type'] != 'railsimDetourEvent':
            continue
        _, index, repeat = event['vehicle'].split('_')
        _, dv, claimer = pattern[int(index)]
        e = round(float(event['time'])) - dv - int(repeat[1:])*PERIOD
        avoided = avoided_by(claimer, event) if avoided_by else set(windows[claimer])
        for u, (_, du, holder) in enumerate(pattern):
            if u == int(index):
                continue
            for resource in avoided & set(windows[holder]):
                _, hg, hr = windows[holder][resource]
                if any(du+hg+s*PERIOD <= dv+e < du+hr+s*PERIOD for s in range(-3, 4)):
                    learned.add(('entry', holder, claimer, resource, e))
    return learned


def simulate(ws, source, pattern, terminals, seed):
    workspace = 'plat-'+uuid.uuid4().hex[:12]
    shutil.copytree(ws.workspace_root/source, ws.workspace_root/workspace)
    departures = [dict(route_id=v, departure_id=f'{p}_{i:02}', vehicle_id=f'{p}_{i:02}', vehicle_type=p,
                       departure_time=stamp(t)) for i, (p, t, v) in enumerate(pattern)]
    server.replace_timetable(workspace, departures, 6, PERIOD)
    run = ws.run_once(workspace, seed, 12600)
    expected = {f'{p}_{i:02}_p{k}': (t+k*PERIOD, v) for k in (2, 3) for i, (p, t, v) in enumerate(pattern)}
    score = evaluate(run, expected, terminals)
    detours = sum(1 for e in events(run['events_file']) if e['type'] == 'railsimDetourEvent')
    return dict(run_id=run['run_id'], passed=score['passed'], max_delay=score['max_delay'], total_delay=score['total_delay'],
                errors=score['errors'][:3], missing=len(score['missing']), detours=detours,
                platform_use={v: sum(r[2] == v for r in pattern) for v in {r[2] for r in pattern}}), run


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--building-block', required=True, choices=['BB1', 'BB2', 'BB3'])
    parser.add_argument('--mode', required=True)
    parser.add_argument('--project', required=True)
    parser.add_argument('--state-root', required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--volumes', type=int, nargs='+', required=True)
    parser.add_argument('--seconds', type=float, default=60)
    parser.add_argument('--max-learning', type=int, default=6)
    parser.add_argument('--seed', type=int, default=4711)
    parser.add_argument('--workers', type=int, default=8, help='CP-SAT search workers')
    args = parser.parse_args()
    settings = Settings.load(args.project, args.state_root)
    ws = ScenarioWorkspace(settings.project/'src/main/resources/scenarios', settings.state/'workspaces',
                           project_root=settings.project, runs_root=settings.state/'runs')
    server.WORKSPACES = ws
    os.environ['MAVEN_OPTS'] = (os.environ.get('MAVEN_OPTS', '')+' '+SIM_JVM_OPTIONS).strip()
    mode = UC1Mode.load(settings.project/'src/main/resources/scenarios/use_case_1/uc1_operational_plan.json', args.mode)
    name = f"{args.building_block}-{mode.id}".lower().replace('/', '-')
    args.output_dir.mkdir(parents=True, exist_ok=True)
    reference_path = args.output_dir/f'{name}-variants.json'
    if reference_path.exists():
        reference = json.loads(reference_path.read_text())
    else:
        workspace = 'plat-'+name[:20]+'-'+uuid.uuid4().hex[:8]
        root = ws.create(workspace, 'UC1', args.building_block)
        variants = add_platform_variants(root/'schedule.xml', root/'network.xml', dict(mode.routes))
        baselines = {v: calibrate(ws, workspace, v, info['product'], args.seed) for v, info in variants.items()}
        source = settings.project/'src/main/resources/scenarios/use_case_1'/f'building_block_{args.building_block[-1]}'
        unchanged = {f: hashlib.sha256((root/f).read_bytes()).hexdigest() == hashlib.sha256((source/f).read_bytes()).hexdigest()
                     for f in ('network.xml', 'config.xml', 'vehicles.xml')}
        reference = dict(building_block=args.building_block, mode=mode.record(), workspace=workspace, seed=args.seed,
                         variants=variants, baselines=baselines, unchanged_inputs=unchanged)
        reference_path.write_text(json.dumps(reference, indent=2)+'\n')
    windows = reference_windows(ws.runs_root, reference['baselines'])
    options = {}
    for variant, info in reference['variants'].items():
        options.setdefault(info['product'], []).append(variant)
    terminals = {v: b['terminal'] for v, b in reference['baselines'].items()}
    links = link_graph(ws.workspace_root/reference['workspace']/'network.xml')
    resource_of = lambda link: links[link]['resource'] or link

    def avoided_by(variant, event):
        planned = reference['variants'][variant]['links']
        inner = planned[planned.index(event['start'])+1:planned.index(event['end'])]
        return {resource_of(l) for l in inner} - {resource_of(l) for l in event['detour'].split(',')}
    report = dict(reference=str(reference_path), windows=windows, volumes=[], learning=[])
    feasible = []
    for volume in args.volumes:
        counts = rounded_counts(volume, mode.shares)
        products = [p for p in sorted(counts) for _ in range(counts[p])]
        row = dict(volume=volume, trains_per_hour=volume*2, **solve(products, options, windows, args.seconds, workers=args.workers))
        row.pop('pattern', None)
        report['volumes'].append(row)
        print(json.dumps(row), flush=True)
        if row['status'] in ('OPTIMAL', 'FEASIBLE'):
            feasible.append(volume)
    rules = set()
    for volume in sorted(feasible, reverse=True):
        counts = rounded_counts(volume, mode.shares)
        products = [p for p in sorted(counts) for _ in range(counts[p])]
        carried = bool(rules)
        for _ in range(args.max_learning+1):
            row = dict(volume=volume, trains_per_hour=volume*2, rules=sorted(rules),
                       **solve(products, options, windows, args.seconds, frozenset(rules), args.workers))
            if 'pattern' not in row:
                report['learning'].append(row)
                print(json.dumps(dict(volume=volume, status=row['status'])), flush=True)
                if carried:
                    # Rules learned at a higher volume can over-constrain this one: retry it from scratch.
                    rules, carried = set(), False
                    continue
                break
            row['simulation'], run = simulate(ws, reference['workspace'], row['pattern'], terminals, args.seed)
            new = learn(run, row['pattern'], windows, avoided_by) - rules
            row['learned'] = sorted(new)
            report['learning'].append(row)
            print(json.dumps(dict(volume=volume, margin=row['margin_seconds'], **{k: row['simulation'][k] for k in
                                  ('passed', 'max_delay', 'detours', 'platform_use')}, learned=sorted(new))), flush=True)
            if row['simulation']['passed'] or not new:
                break
            rules |= new
        if report['learning'] and report['learning'][-1].get('simulation', {}).get('passed'):
            report['best'] = report['learning'][-1]
            break
    report['rules'] = sorted(rules)
    (args.output_dir/f'{name}.json').write_text(json.dumps(report, indent=2)+'\n')


if __name__ == '__main__':
    main()
