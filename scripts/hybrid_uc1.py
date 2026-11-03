"""Hybrid UC1 loop: a reasoning model proposes train orders, CP-SAT fixes the seconds, railsim judges (experiment).

Each tool does what it is good at:
  - The model (a reasoning model, no tools) looks at the problem and at every result so far and proposes
    up to five entry orders of train types, optionally with a platform per train, and says why.
  - CP-SAT turns each order into exact departure seconds and platforms (solve_order from
    order_uc1.py), or proves the order cannot work. For an impossible order it also finds the
    closest timetable: how many seconds it misses by, and which trains clash on which resource.
  - railsim simulates every timetable CP-SAT finds; only a zero-delay run counts.
The loop stops at the first pass or after --rounds model calls.

  hybrid_uc1.py --variants DIR/bb3-mainline-fv_pass-variants.json --mode MAINLINE/FV_PASS --volume 19 \\
      --known DIR/bb3-mainline-fv_pass.json --project SIM --state-root STATE --output-dir OUT
"""
import argparse
import json
import os
from pathlib import Path
import time

from railsim_mcp import server
from railsim_mcp.configuration import Settings
from railsim_mcp.model_runner import model_identity, request_action
from railsim_mcp.search import SIM_JVM_OPTIONS, bounded_process
from railsim_mcp.uc1 import PERIOD
from railsim_mcp.uc1_modes import UC1Mode, rounded_counts
from railsim_mcp.workspace import ScenarioWorkspace
from occupation_bound_uc1 import occupation
from order_uc1 import sections, solve_order, type_headways
from solve_uc1_cpsat import reference_windows
from solve_uc1_platforms import simulate

NAMES = dict(F='FV', R='RV', G='GV')


def describe(pattern, windows, entry):
    """Entry order, platforms and departures of a timetable, by entry time."""
    rows = sorted(((d + windows[v][entry[0]][1]) % PERIOD, p, d, v) for p, d, v in pattern)
    return dict(order=''.join(p[0] for _, p, _, _ in rows), platforms=[v.split('_')[-1] for *_, v in rows],
                departures=[d for _, _, d, _ in rows])


def canonical(order, platforms):
    rotations = [(order[i:]+order[:i], tuple(platforms[i:]+platforms[:i])) for i in range(len(order))]
    return min(rotations)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--variants', type=Path, required=True)
    parser.add_argument('--mode', required=True)
    parser.add_argument('--volume', type=int, required=True, help='trains per 30 minutes')
    parser.add_argument('--known', type=Path, help='solve_uc1_binding.py report with the passing lower rate')
    parser.add_argument('--project', required=True)
    parser.add_argument('--state-root', required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--rounds', type=int, default=8)
    parser.add_argument('--proposals', type=int, default=5)
    parser.add_argument('--seconds', type=float, default=30, help='CP-SAT limit per order')
    parser.add_argument('--diagnose-seconds', type=float, default=20)
    parser.add_argument('--runner', choices=['codex', 'anthropic'], default='codex')
    parser.add_argument('--model')
    parser.add_argument('--workers', type=int, default=8)
    parser.add_argument('--seed', type=int, default=4711)
    args = parser.parse_args()
    settings = Settings.load(args.project, args.state_root)
    ws = ScenarioWorkspace(settings.project/'src/main/resources/scenarios', settings.state/'workspaces',
                           project_root=settings.project, runs_root=settings.state/'runs')
    server.WORKSPACES = ws
    os.environ['MAVEN_OPTS'] = (os.environ.get('MAVEN_OPTS', '')+' '+SIM_JVM_OPTIONS).strip()
    reference = json.loads(args.variants.read_text())
    windows = reference_windows(ws.runs_root, reference['baselines'])
    terminals = {v: b['terminal'] for v, b in reference['baselines'].items()}
    options = {}
    for variant, info in reference['variants'].items():
        options.setdefault(info['product'], []).append(variant)
    mode = UC1Mode.load(settings.project/'src/main/resources/scenarios/use_case_1/uc1_operational_plan.json', args.mode)
    counts = {p: k for p, k in rounded_counts(args.volume, mode.shares).items() if k}
    products = [p for p in sorted(counts) for _ in range(counts[p])]
    letters = {p[0]: k for p, k in counts.items()}
    platforms_of = {p[0]: sorted(v.split('_')[-1] for v in options[p]) for p in counts}
    entry, exit_ = sections(windows)
    name = f"{reference['building_block']}-{mode.id}-{2*args.volume}".lower().replace('/', '-')
    if args.runner != 'codex':
        name += '-' + (args.model or args.runner)
    out = args.output_dir/name
    out.mkdir(parents=True, exist_ok=True)
    identity = model_identity(args.runner, args.model)

    def table(section):
        h = type_headways(options, windows, section, 0)
        return {f'{p[0]} then {q[0]}': v for (p, q), v in h.items() if p in counts and q in counts}

    context = dict(
        building_block=reference['building_block'], mode=mode.id, trains_per_hour=2*args.volume, period_seconds=PERIOD,
        trains={c: dict(count=k, product=NAMES[c], platforms=platforms_of[c]) for c, k in letters.items()},
        holding_windows={v: {r: f'{g}-{rel}' for r, (_, g, rel) in sorted(w.items(), key=lambda kv: kv[1][1])}
                         for v, w in windows.items()},
        entry_track=dict(resources=entry, minimum_headway_seconds=table(entry)),
        exit_track=dict(resources=exit_, minimum_headway_seconds=table(exit_)),
        occupation_bound={k: v for k, v in occupation(options, counts, windows).items() if k in ('busiest_seconds', 'busiest_resources', 'split')})
    known = None
    if args.known:
        report = json.loads(args.known.read_text())
        best = report['best']
        known = dict(trains_per_hour=best['trains_per_hour'], **describe(best['pattern'], windows, entry))
        near = [a for a in report['attempts'] if a['trains_per_hour'] == 2*args.volume and 'pattern' in a]
        context['known_passing_timetable_at_lower_rate'] = known
        if near:
            a = near[0]
            context['earlier_near_miss_at_this_rate'] = dict(
                **describe(a['pattern'], windows, entry), gap=a['min_gap'],
                railsim=dict(max_delay=a['simulation']['max_delay'], late=a['simulation']['errors']))

    history, tried = [], set()
    log = dict(context=context, model=identity, rounds=[], started=time.time())
    passed = None
    for round_ in range(1, args.rounds+1):
        schema = dict(type='object', additionalProperties=False, required=['analysis', 'proposals'], properties=dict(
            analysis=dict(type='string'),
            proposals=dict(type='array', items=dict(
                type='object', additionalProperties=False, required=['order', 'platforms', 'reason'], properties=dict(
                    order=dict(type='string'), reason=dict(type='string'),
                    platforms=dict(type='array', items=dict(type='string', enum=['any']+sorted({x for v in platforms_of.values() for x in v}))))))))
        prompt = (
            'You help find a periodic railway timetable through one station, working with a constraint solver and a '
            'simulator. Use no tools, files or shell. You choose STRUCTURE; the solver computes exact seconds.\n'
            f'Goal: {2*args.volume} trains per hour, i.e. the trains listed below every {PERIOD} s, repeating. '
            'A proposal is the cyclic ORDER in which trains reach the single-track entry section, as a string of '
            f'letters with exactly these counts: {letters} ({len(products)} letters in total; count them before '
            'answering, wrong counts are rejected). Optionally give a platform per position '
            '(same length as the order, "any" lets the solver choose; an empty list means all "any").\n'
            'Rules the solver enforces: no two trains may hold the same resource at the same time, also across the '
            'repeat into the next period. holding_windows give, for each train variant (type and platform), when it '
            'holds each resource in seconds after its departure. Trains on different platforms can overtake inside '
            'the station, so the exit order may differ from the entry order.\n'
            'For each proposal you get: "impossible" with the closest timetable (seconds missed and the clashing '
            'train pairs, named by type letter and entry position, with resource and platforms), or "found" with '
            'the simulator verdict. One railsim pass with zero delay ends the search.\n'
            'Learn from the results: avoid structures that failed, explain what you change. Give diverse proposals. '
            'Do not repeat an order already tried (rotations count as the same).\n'
            'Problem:\n'+json.dumps(context)+'\nResults so far (oldest first):\n'+json.dumps(history))
        prefix = out/f'round-{round_}'
        prefix.with_suffix('.prompt.txt').write_text(prompt)

        def validate(action, schema):
            if not isinstance(action.get('proposals'), list) or not action['proposals']:
                raise ValueError('no proposals')
            return action
        started = time.monotonic()
        action, metrics = request_action(identity, prompt, schema, prefix, 600, bounded_process, validate)
        record = dict(round=round_, model_seconds=round(time.monotonic()-started, 1), analysis=action['analysis'],
                      usage=metrics.get('usage'), proposals=[])
        print(json.dumps(dict(round=round_, model_seconds=record['model_seconds'], analysis=action['analysis'][:300])), flush=True)
        for proposal in action['proposals'][:args.proposals]:
            order, platforms = proposal['order'].strip().upper(), proposal['platforms'] or []
            row = dict(order=order, platforms=platforms, reason=proposal['reason'])
            counted = {c: order.count(c) for c in letters}
            if counted != letters or len(order) != len(products) or (platforms and len(platforms) != len(order)):
                row['result'] = (f'rejected: needs exactly {letters} ({len(products)} letters), got {counted} '
                                 f'({len(order)} letters)' + (f' and {len(platforms)} platforms' if platforms else ''))
            elif any(p != 'any' and p not in platforms_of[c] for c, p in zip(order, platforms)):
                row['result'] = 'rejected: a platform is not available for that train type'
            else:
                key = canonical(order, platforms or ['any']*len(order))
                if key in tried:
                    row['result'] = 'rejected: already tried'
                else:
                    tried.add(key)
                    sequence = [NAMES[c] for c in order]
                    for gap in (0, 1):
                        solved = solve_order(sequence, products, options, windows, entry, gap, args.seconds,
                                             args.workers, platforms or None)
                        row[f'solver_gap_{gap}'] = dict(status=solved['status'], seconds=solved['solve_seconds'])
                        if 'pattern' not in solved:
                            if solved['status'] == 'INFEASIBLE' and gap == 0:
                                close = solve_order(sequence, products, options, windows, entry, 0,
                                                    args.diagnose_seconds, args.workers, platforms or None, slack=True)
                                row['result'] = 'impossible'
                                row['closest'] = dict(miss_seconds=close.get('miss_seconds'),
                                                      platforms=describe(close['pattern'], windows, entry)['platforms'] if 'pattern' in close else None,
                                                      conflicts=close.get('conflicts'))
                            else:
                                row['result'] = row.get('result') or f"solver {solved['status'].lower()}"
                            break
                        sim, _ = simulate(ws, reference['workspace'], solved['pattern'], terminals, args.seed)
                        row['result'] = 'passed' if sim['passed'] else 'found, late in railsim'
                        row['timetable'] = describe(solved['pattern'], windows, entry)
                        row['railsim'] = dict(max_delay=sim['max_delay'], late=sim['errors'], run_id=sim['run_id'], gap=gap)
                        if sim['passed']:
                            passed = dict(row, pattern=solved['pattern'])
                            break
            record['proposals'].append(row)
            history.append({k: v for k, v in row.items() if k != 'reason'} | dict(round=round_))
            print(json.dumps(dict(round=round_, order=order, platforms=''.join(p[-1] if p != 'any' else '.' for p in platforms),
                                  result=row['result'], miss=row.get('closest', {}).get('miss_seconds'),
                                  railsim=row.get('railsim', {}).get('max_delay'))), flush=True)
            if passed:
                break
        log['rounds'].append(record)
        (out/'log.json').write_text(json.dumps(log, indent=2)+'\n')
        if passed:
            break
    log['passed'] = passed
    log['orders_tried'] = len(tried)
    log['elapsed_seconds'] = round(time.time()-log['started'])
    (out/'log.json').write_text(json.dumps(log, indent=2)+'\n')
    print(json.dumps(dict(done=True, passed=bool(passed), orders_tried=len(tried), elapsed=log['elapsed_seconds'])), flush=True)


if __name__ == '__main__':
    main()
