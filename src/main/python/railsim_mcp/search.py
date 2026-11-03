"""Bounded periodic UC1 capacity search. Simulation alone certifies a witness.

The CLI and MCP evaluator use this same code. State is outside both repositories.
"""
from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import random
import signal
import shutil
import subprocess
import tempfile
import time
import uuid
import xml.etree.ElementTree as ET
from . import server
from .model_runner import model_identity, request_action, check_runner
from .configuration import Settings
from .workspace import ScenarioWorkspace
from .analysis import RunAnalysis
from .uc1 import TYPES, ROUTES, PERIOD, HEADWAY, stamp, trace, pattern_departures, best_cycle, cyclic_pattern, analyse
from .search_evidence import reference_context, measured_context
from .uc1_modes import UC1Mode, DEFAULT_MODE, rounded_counts


MODEL_POLICIES = ('luna', 'model', 'pick')
PARALLEL_POLICIES = ('repair', 'random')
# The simulation itself is single-threaded; by default the JVM adds C2 compiler and
# parallel GC threads (~2.3 cores per run). C1-only JIT and serial GC keep a run near
# one core with identical events, so more runs fit in parallel.
SIM_JVM_OPTIONS = os.environ.get('RAILSIM_SIM_JVM_OPTIONS', '-XX:TieredStopAtLevel=1 -XX:+UseSerialGC')


def worker_environment():
    env = os.environ.copy()
    env['MAVEN_OPTS'] = (env.get('MAVEN_OPTS', '')+' '+SIM_JVM_OPTIONS).strip()
    return env
PICK_OPTIONS = 12


def composition(volume):
    return rounded_counts(volume,dict(FV=.4,RV=.4,GV=.2))


def validate_pattern(pattern, volume, expected_counts=None):
    expected_counts = composition(volume) if expected_counts is None else expected_counts
    counts = dict.fromkeys(expected_counts, 0)
    clean = []
    for product, departure in pattern:
        if product not in counts or type(departure) is not int or not 0 <= departure < PERIOD:
            raise ValueError('each train requires a fixed product and integer departure in [0,1800)')
        counts[product] += 1
        clean.append([product, departure])
    if counts != expected_counts:
        raise ValueError('pattern does not preserve the predefined composition')
    return sorted(clean, key=lambda row: (row[1], row[0]))


def evaluate(run, expected, references):
    """Independent expected IDs -> raw terminal times; no rounded aggregate gate."""
    rows = run.get('trains', [])
    by_id = {}
    errors = []
    for row in rows:
        ident = row.get('vehicle_id')
        if ident in by_id:
            errors.append('duplicate vehicle: '+str(ident))
        by_id[ident] = row
    delays = []
    missing = []
    for ident, (departure, product) in expected.items():
        row = by_id.get(ident)
        if row is None or row.get('status') != 'terminal_arrived':
            missing.append(ident)
            continue
        actual = row.get('actual_arrival')
        reported = row.get('terminal_delay_seconds')
        planned = departure + references[product]
        if (row.get('scheduled_departure') != departure or row.get('planned_arrival') != planned
                or any(type(n) not in (int, float) or not math.isfinite(n) for n in (actual, reported))):
            errors.append('invalid times or changed reference: '+ident)
            continue
        delay = max(0, actual-planned)
        if max(0, reported) != delay:
            errors.append('inconsistent raw delay: '+ident)
        delays.append(delay)
    complete = bool(expected) and not missing and not errors and len(delays) == len(expected)
    return dict(passed=run.get('status') == 'completed' and complete and all(d == 0 for d in delays),
                expected=len(expected), completed=len(delays), missing=missing, errors=errors,
                max_delay=max(delays, default=None), total_delay=sum(delays),
                headway_conflicts_diagnostic=run.get('headway_conflicts', []),
                acceptance='all measured departures in [3600,7200) complete; raw positive exit delay = 0; headways diagnostic')


def assess_retained_run(workspaces, run_id, routes=None):
    run = workspaces.inspect_run(run_id)
    root = ET.parse(Path(run['snapshot_directory'])/'schedule.xml').getroot()
    if routes is None:
        fleet = ET.parse(Path(run['snapshot_directory'])/'vehicles.xml').getroot()
        types = {v.get('id'):v.get('type') for v in fleet.iter() if v.tag.rsplit('}',1)[-1]=='vehicle'}
        routes = {}
        for route in root.iter('transitRoute'):
            departures = route.find('departures')
            if departures is None or not len(departures): continue
            products = {types.get(d.get('vehicleRefId')) for d in departures}
            if len(products)!=1 or next(iter(products)) not in TYPES:
                raise ValueError('search assessment requires a known fixed product per active route')
            product = next(iter(products))
            if product in routes and routes[product]!=route.get('id'):
                raise ValueError('search assessment requires one active route per product')
            routes[product] = route.get('id')
    expected, refs = {}, {}
    for route in root.iter('transitRoute'):
        product = next((p for p,r in routes.items() if r == route.get('id')), None)
        if product is None:
            continue
        stops = route.find('routeProfile').findall('stop')
        offset = workspaces._seconds(stops[-1].get('arrivalOffset'), 'offset')
        refs[product] = offset
        for dep in route.find('departures'):
            t = workspaces._seconds(dep.get('departureTime'), 'departure')
            if 3600 <= t < 7200:
                expected[dep.get('vehicleRefId')] = (t, product)
    return evaluate(run, expected, refs)


def quality(score):
    """Lexicographic search progress: incomplete/invalid trains, then worst and total delay."""
    return [score['expected']-score['completed']+len(score['errors']),
            score['max_delay'] if score['max_delay'] is not None else 1e30, score['total_delay']]


def distinct_conflicts(evidence, limit=6):
    """Worst finding per timetable train; the same train repeats in every simulated period."""
    worst = {}
    for row in sorted(evidence, key=lambda r:-r['first_late_link']['excess_seconds']):
        worst.setdefault(row['vehicle'].rsplit('_p', 1)[0], row)
    return list(worst.values())[:limit]


def entry_diagnostics(run, trajectories):
    ordered = sorted(((t['vehicle_id'], t['scheduled_departure']) for t in run['trains']),
                     key=lambda pair: trajectories.get(pair[0],{}).get('entries',{}).get('f03', math.inf))
    pairs, missing = [], []
    for (a,_),(b,departure) in zip(ordered,ordered[1:]):
        if not 3600 <= departure < 7200: continue
        head = trajectories.get(a,{}).get('entries',{}).get('f03')
        tail = trajectories.get(a,{}).get('leaves',{}).get('f03')
        follow = trajectories.get(b,{}).get('entries',{}).get('f03')
        if head is None or follow is None:
            missing.append(b)
            continue
        required = HEADWAY[a.split('_')[0]]
        pairs.append(dict(previous=a, following=b, front_gap=follow-head,
                          rear_gap=None if tail is None else follow-tail,
                          front_deficit=max(0,required-(follow-head)),
                          rear_deficit=None if tail is None else max(0,required-(follow-tail)),
                          crosses_pattern_boundary=a.rsplit('_p',1)[-1]!=b.rsplit('_p',1)[-1]))
    return dict(entry_link='f03', pairs=pairs, missing=missing, acceptance_gate=False)


def bounded_process(command, timeout, **kwargs):
    """Kill the whole process group on timeout/cancellation, including Maven/Java."""
    input_text = kwargs.pop("input", None)
    p = subprocess.Popen(command, start_new_session=True, **kwargs)
    try:
        out, err = p.communicate(input=input_text, timeout=max(.01, timeout))
        return subprocess.CompletedProcess(command, p.returncode, out, err)
    except BaseException:
        os.killpg(p.pid, signal.SIGTERM)
        try:
            p.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            os.killpg(p.pid, signal.SIGKILL)
            p.communicate()
        raise


class Search:
    def __init__(self, settings, destination, policy='random', seed=4711, simulations=100, seconds=1800, turns=30, resume=False, attempts_per_volume=30, warm_start=None, target_volumes=None, mode=None, model_runner="codex", model=None, workers=1, building_block='BB1'):
        self.settings, self.destination = settings, Path(destination)
        self.ws = ScenarioWorkspace(settings.project/'src/main/resources/scenarios', settings.state/'workspaces',
                                    project_root=settings.project, runs_root=settings.state/'runs')
        server.WORKSPACES = self.ws
        server.ANALYSIS = RunAnalysis(self.ws.runs_root)
        self.started = time.monotonic()
        if type(workers) is not int or not 1 <= workers <= 32:
            raise ValueError('workers must be an integer in 1..32')
        if workers > 1 and policy not in PARALLEL_POLICIES:
            raise ValueError('parallel batches support only the '+'/'.join(PARALLEL_POLICIES)+' policies')
        self.workers = workers
        if resume:
            self.state = json.loads(self.destination.read_text())
            if self.state.get('version') != 2:
                raise ValueError('legacy checkpoints require --warm-start into a new search')
            if warm_start:
                raise ValueError('use either resume or warm-start')
            if self.state['policy'] != policy or self.state['seed'] != seed:
                raise ValueError('resume policy and seed must match')
            if self.state['project_revision'] != self.revision(settings.project):
                raise ValueError('simulator changed since checkpoint')
            current_revision = self.revision(Path(__file__).resolve().parents[4])
            if current_revision != self.state.get('controller_revision'):
                self.state.setdefault('controller_revisions', [self.state.get('controller_revision')]).append(current_revision)
                self.state['controller_revision'] = current_revision
        else:
            if self.destination.exists():
                raise ValueError('state already exists; use --resume')
            self.state = dict(version=2, policy=policy, seed=seed, workspace='search-'+uuid.uuid4().hex[:16],
                              simulations=0, turns=0, attempts=0, elapsed=0, trials=[], decisions=[], baselines={},
                              project_revision=self.revision(settings.project), controller_revision=self.revision(Path(__file__).resolve().parents[4]),
                              limits=dict(simulations=simulations, seconds=seconds, turns=turns, attempts_per_volume=attempts_per_volume),
                              active_volume=4, volumes={}, status='initializing')
        self.state.setdefault('simulator_jvm_options', []).append(SIM_JVM_OPTIONS)
        identity = model_identity(model_runner, model)
        if policy in MODEL_POLICIES:
            previous_identity = self.state.get('model_config', model_identity('codex', None))
            if resume and previous_identity != identity:
                raise ValueError('resume cannot change model runner, model or provider; use a new checkpoint')
            self.state['model_config'] = identity
        recorded_mode = self.state.get('operating_mode',DEFAULT_MODE)
        if resume and mode is not None and mode != recorded_mode:
            raise ValueError('resume cannot change the operating mode')
        mode_id = recorded_mode if resume else mode or DEFAULT_MODE
        plan_path = settings.project/'src/main/resources/scenarios/use_case_1/uc1_operational_plan.json'
        self.mode = UC1Mode.load(plan_path,mode_id) if plan_path.exists() else UC1Mode.legacy_default()
        if self.mode.id != mode_id:
            raise ValueError('mode definition unavailable')
        if resume and self.state.get('mode_definition',self.mode.record()) != self.mode.record():
            raise ValueError('mode definition changed since checkpoint')
        self.state.update(operating_mode=self.mode.id,mode_definition=self.mode.record())
        if resume and building_block != self.state.get('building_block','BB1'):
            raise ValueError('resume cannot change the building block')
        self.state.setdefault('building_block', building_block)
        self.types = tuple(self.mode.routes)
        self.previous_elapsed = self.state['elapsed']
        self.rng = random.Random(seed)
        if 'rng' in self.state:
            def tuples(x): return tuple(tuples(i) for i in x) if isinstance(x, list) else x
            self.rng.setstate(tuples(self.state['rng']))
        if warm_start:
            if resume: raise ValueError('use either resume or warm-start')
            self.import_witness(Path(warm_start))
        if not resume and target_volumes:
            targets = sorted(set(target_volumes))
            if any(type(v) is not int or not 4 <= v <= 100 for v in targets):
                raise ValueError('target volumes must be integers in 4..100 trains per period')
            self.state.update(target_volumes=targets, active_volume=targets[0])
            self.save()

    @staticmethod
    def revision(path):
        return subprocess.check_output(['git','rev-parse','HEAD'], cwd=path, text=True).strip()

    def composition(self, volume):
        return self.mode.composition(volume)

    def validate_pattern(self, pattern, volume):
        return validate_pattern(pattern,volume,self.composition(volume))

    def remaining(self):
        return self.state['limits']['seconds'] - self.previous_elapsed - (time.monotonic()-self.started)

    def save(self):
        self.state['elapsed'] = self.previous_elapsed+time.monotonic()-self.started
        self.state['rng'] = self.rng.getstate()
        passing = [t for t in self.state['trials'] if t.get('score',{}).get('passed')]
        self.state['best_volume'] = max((t['volume'] for t in passing), default=None)
        self.destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.destination.with_suffix('.tmp')
        temporary.write_text(json.dumps(self.state, indent=2, allow_nan=False)+'\n')
        temporary.replace(self.destination)

    def simulate(self, pattern, periods=6):
        if self.remaining() <= 0 or self.state['simulations'] >= self.state['limits']['simulations']:
            raise TimeoutError('simulation/time budget exhausted')
        if periods == 6:
            bucket = self.volume_state()
            if bucket['simulation_calls'] >= self.state['limits']['attempts_per_volume']:
                raise TimeoutError('per-volume simulation budget exhausted')
            bucket['simulation_calls'] += 1
        self.state['simulations'] += 1
        self.save()  # A crash or timeout still consumes the invocation.
        server.replace_timetable(self.state['workspace'], pattern_departures(pattern,self.mode.routes), periods, PERIOD)
        # Separate process prevents cancellation leaving an unbounded JVM behind.
        command = [os.sys.executable, '-m', 'railsim_mcp.search', '--worker', self.state['workspace'],
                   '--project', str(self.settings.project), '--state-root', str(self.settings.state), '--seed', str(self.state['seed'])]
        result = bounded_process(command, self.remaining(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                 env=worker_environment())
        if result.returncode:
            raise RuntimeError(result.stderr[-4000:])
        return json.loads(result.stdout)

    def setup(self):
        s = self.state
        if s.get('frozen'):
            if self.fingerprint() != s['frozen']:
                raise ValueError('frozen scenario was changed since checkpoint')
            return
        self.ws.create(s['workspace'], 'UC1', s.get('building_block','BB1')) if not (self.ws.workspace_root/s['workspace']).exists() else None
        for product in self.types:
            if product in s['baselines']:
                continue
            run = self.simulate([(product,0)], periods=1)
            row = run['trains'][0]
            if row['status'] != 'terminal_arrived':
                raise ValueError('isolated reference did not complete')
            trajectory = trace(run['events_file'])[product+'_00']
            route = self.ws.get_entity(s['workspace'], 'transitRoute', self.mode.routes[product])
            profile = copy.deepcopy(next(c for c in route['children'] if c['type']=='routeProfile'))
            for stop in profile['children']:
                for e in trajectory['facilities']:
                    if e['facility'] == stop['attributes']['refId']:
                        stop['attributes']['arrivalOffset' if e['type']=='VehicleArrivesAtFacility' else 'departureOffset'] = stamp(float(e['time']))
            def clean(record):
                return {k:([clean(c) for c in v] if k=='children' else v) for k,v in record.items() if v is not None}
            self.ws.edit(s['workspace'], [dict(action='update',entity_type='transitRoute',entity_id=self.mode.routes[product],replace_children={'routeProfile':[clean(profile)]})])
            s['baselines'][product] = dict(run_id=run['run_id'], trajectory=trajectory, terminal=row['actual_arrival'])
            self.save()
        s['frozen'] = self.fingerprint()
        s['status'] = 'searching'
        self.save()

    def fingerprint(self, workspace=None):
        root = self.ws.workspace_root/(workspace or self.state['workspace'])
        hashes = {}
        for name in ['network.xml','config.xml','uc1_operational_plan.json','schedule.xml','vehicles.xml']:
            tree = ET.fromstring((root/name).read_text()) if name.endswith('.xml') else None
            if tree is not None:
                for parent in tree.iter():
                    for child in list(parent):
                        if child.tag.rsplit('}',1)[-1] in ('departures','vehicle'): parent.remove(child)
                data = ET.tostring(tree)
            else:
                data = (root/name).read_bytes()
            hashes[name] = hashlib.sha256(data).hexdigest()
        return hashes

    def random_pattern(self, volume):
        products = [p for p,n in self.composition(volume).items() for _ in range(n)]
        self.rng.shuffle(products)
        return list(zip(products, sorted(self.rng.sample(range(PERIOD), volume))))

    def rule_pattern(self, volume):
        # This action is an explicit reference-template seed, not an edit inferred
        # from prose in the model's reason. Concrete repairs require submit.
        pattern = next((seed['pattern'] for seed in self.reference_cycle_seeds(volume) if seed['pattern'] is not None), None)
        if pattern is None:
            raise ValueError('rules unavailable: no reference cycle fits; choose random explicitly or submit a concrete timetable')
        return pattern

    def reference_cycle_seeds(self, volume):
        matrix = self.reference_evidence()['same_resource_order_gap_seconds']
        result = []
        # Margin is a proposal aid, not an operating rule. A tight zero-margin
        # proposal still needs actual simulation; do not silently reject it.
        for margin in (2,1,0):
            gaps = {(a,b):math.ceil(matrix[a][b])+margin for a in self.types for b in self.types}
            cost, seq = best_cycle(volume, gaps, tuple(self.composition(volume).values()), self.types)
            probe = None
            if margin == 0 and cost > PERIOD:
                t, probe = 0, []
                for i, product in enumerate(seq):
                    probe.append([product, min(PERIOD-1, math.floor(t*PERIOD/cost))])
                    t += gaps[product,seq[(i+1)%len(seq)]]
            result.append(dict(extra_margin_seconds=margin, template_cycle_seconds=cost,
                               fits_template=cost<=PERIOD, pattern=cyclic_pattern(seq,gaps),
                               compressed_probe=probe))
        return result

    def reference_evidence(self):
        if not hasattr(self, '_reference_evidence'):
            self._reference_evidence = reference_context(self.ws.runs_root, self.state['baselines'])
            self._reference_evidence['entry_offsets'] = {p:b['trajectory']['entries'] for p,b in self.state['baselines'].items()}
        return self._reference_evidence

    def next_volume(self):
        targets = self.state.get('target_volumes')
        if targets:
            remaining = [v for v in targets if v > self.state['active_volume']]
            if not remaining:
                self.state['targets_completed'] = True
            else:
                self.state['active_volume'] = remaining[0]
        else:
            self.state['active_volume'] += 1

    def volume_state(self):
        bucket = self.state['volumes'].setdefault(str(self.state['active_volume']),
            dict(attempts=0, simulation_calls=0, model_turns=0, stagnant=0,
                 best_quality=None, best_key=None, restart_required=False,
                 restarts=0, status='searching'))
        if bucket['best_key'] is None:
            retained = [t for t in self.state['trials'] if t['volume']==self.state['active_volume'] and not t['score']['passed']]
            if retained:
                best = min(retained, key=lambda t:(t['score']['max_delay'],t['score']['total_delay']))
                bucket.update(best_key=best['key'],best_quality=quality(best['score']))
        return bucket

    def import_witness(self, path):
        """Reuse retained evidence only after checking scenario, inputs and raw arrivals."""
        source = json.loads(path.read_text())
        if source.get('operating_mode',DEFAULT_MODE) != self.mode.id:
            raise ValueError('warm-start cannot reuse a different operating mode')
        if source['project_revision'] != self.state['project_revision'] or source['seed'] != self.state['seed']:
            raise ValueError('warm-start requires identical simulator revision and seed')
        source_id = source['workspace']
        source_root = self.ws.workspace_root/source_id
        if (not isinstance(source_id,str) or Path(source_id).name != source_id
                or not source_root.resolve().is_relative_to(self.ws.workspace_root.resolve())):
            raise ValueError('warm-start workspace escapes configured state root')
        passing = [t for t in source['trials'] if t.get('score',{}).get('passed')]
        if not passing: raise ValueError('warm-start has no passing witness')
        witness = copy.deepcopy(max(passing, key=lambda t:t['volume']))
        witness['pattern'] = self.validate_pattern(witness['pattern'], witness['volume'])
        run = self.ws.inspect_run(witness['run_id'])
        expected = {f'{p}_{i:02}_p{k}':(t+k*PERIOD,p) for k in (2,3) for i,(p,t) in enumerate(witness['pattern'])}
        refs = {p:b['terminal'] for p,b in source['baselines'].items()}
        for product,baseline in source['baselines'].items():
            calibration = self.ws.inspect_run(baseline['run_id'])
            rows = calibration.get('trains',[])
            if (calibration.get('status') != 'completed' or len(rows) != 1
                    or rows[0].get('status') != 'terminal_arrived'
                    or rows[0].get('route_id') != self.mode.routes[product]
                    or rows[0].get('actual_arrival') != baseline['terminal']):
                raise ValueError('warm-start isolated reference cannot be verified')
        if not evaluate(run,expected,refs)['passed'] or run['input_sha256'] != witness['input_sha256']:
            raise ValueError('warm-start witness failed independent validation')
        original_workspace = self.state['workspace']
        self.state['workspace'] = source['workspace']
        try:
            if self.fingerprint() != source['frozen']: raise ValueError('warm-start frozen inputs changed')
            # Check the retained witness snapshot against the same fixed scenario.
            with tempfile.TemporaryDirectory(dir=self.ws.workspace_root) as d:
                snapshot = Path(d)/'witness'
                shutil.copytree(Path(run['snapshot_directory']), snapshot)
                self.state['workspace'] = snapshot.relative_to(self.ws.workspace_root).as_posix()
                if self.fingerprint() != source['frozen']: raise ValueError('witness uses different fixed inputs')
        finally:
            self.state['workspace'] = original_workspace
        shutil.copytree(self.ws.workspace_root/source['workspace'], self.ws.workspace_root/original_workspace)
        self.state.update(baselines=copy.deepcopy(source['baselines']), frozen=copy.deepcopy(source['frozen']),
                          active_volume=witness['volume']+1, status='searching',
                          warm_start=dict(source=str(path.resolve()), run_id=witness['run_id'],
                                          reused_calibration_runs={p:b['run_id'] for p,b in source['baselines'].items()}))
        witness['inherited'] = True
        self.state['trials'].append(witness)
        # Preserve a small number of validated near misses as context, without
        # charging their historic simulations against the newly authorized run.
        failed = sorted((t for t in source['trials'] if t['volume'] > witness['volume'] and not t['score']['passed']),
                        key=lambda t:(t['volume'],t['score']['max_delay'],t['score']['total_delay']))[:3]
        for old in failed:
            pattern = self.validate_pattern(old['pattern'], old['volume'])
            retained = self.ws.inspect_run(old['run_id'])
            expected = {f'{p}_{i:02}_p{k}':(t+k*PERIOD,p) for k in (2,3) for i,(p,t) in enumerate(pattern)}
            score = evaluate(retained, expected, refs)
            with tempfile.TemporaryDirectory(dir=self.ws.workspace_root) as d:
                snapshot = Path(d)/'evidence'
                shutil.copytree(Path(retained['snapshot_directory']), snapshot)
                self.state['workspace'] = snapshot.relative_to(self.ws.workspace_root).as_posix()
                try:
                    if self.fingerprint() != source['frozen']: raise ValueError('retained near miss uses changed fixed inputs')
                finally:
                    self.state['workspace'] = original_workspace
            if score['errors'] or score['missing'] or score['passed'] or retained['input_sha256'] != old['input_sha256']:
                raise ValueError('retained near miss cannot be verified')
            copy_trial = copy.deepcopy(old)
            copy_trial.update(pattern=pattern, score=score, inherited=True)
            self.state['trials'].append(copy_trial)
        self.save()

    def warm_pattern(self, volume):
        """Preserve a passing pattern; adjust the mix and insert new trains in large gaps."""
        passing = [t for t in self.state['trials'] if t['score']['passed'] and t['volume'] < volume]
        if not passing: return self.random_pattern(volume)
        pattern = copy.deepcopy(max(passing,key=lambda t:t['volume'])['pattern'])
        desired = self.composition(volume)
        for product in self.types:
            while sum(p == product for p,_ in pattern) > desired[product]:
                index = next(i for i in range(len(pattern)-1,-1,-1) if pattern[i][0] == product)
                pattern.pop(index)
        for product in self.types:
            while sum(p == product for p,_ in pattern) < desired[product]:
                times = sorted(t for _,t in pattern)
                start,end = max(zip(times,times[1:]+[times[0]+PERIOD]),key=lambda pair:pair[1]-pair[0])
                pattern.append([product, ((start+end)//2)%PERIOD])
        return self.validate_pattern(pattern,volume)

    def record_progress(self, trial):
        bucket = self.volume_state()
        score = trial['score']
        current = quality(score)
        if bucket['best_quality'] is None or current < bucket['best_quality']:
            bucket.update(best_quality=current,best_key=trial['key'],stagnant=0)
        else:
            bucket['stagnant'] += 1
        # A parallel batch counts as one step, so plateaus scale with the batch size.
        if bucket['stagnant'] >= 5*self.workers: bucket['restart_required'] = True
        if score['passed']:
            bucket['status'] = 'passed'
            self.next_volume()

    def observation(self):
        s = self.state
        volume = s['active_volume']
        bucket = self.volume_state()
        candidates = [t for t in s['trials'] if t['volume'] == volume]
        best = next((t for t in candidates if t['key']==bucket['best_key']),None)
        def compact(t, detailed=False):
            score = {k:v for k,v in t['score'].items() if k!='headway_conflicts_diagnostic'}
            score['headway_conflict_count_diagnostic'] = len(t['score'].get('headway_conflicts_diagnostic',[]))
            result = {k:t[k] for k in ('volume','pattern','key','run_id') if k in t}
            result['score'] = score
            if detailed and 'run_id' in t:
                if not hasattr(self, '_candidate_evidence'): self._candidate_evidence = {}
                if t['run_id'] not in self._candidate_evidence:
                    run = self.ws.inspect_run(t['run_id'])
                    self._candidate_evidence[t['run_id']] = measured_context(run,trace(run['events_file']),self.reference_evidence())
                result['measured_evidence'] = self._candidate_evidence[t['run_id']]
            return result
        warm = self.warm_pattern(volume)
        cycles = self.reference_cycle_seeds(volume)
        rule = next((seed['pattern'] for seed in cycles if seed['pattern'] is not None), None)
        return dict(objective=f"UC1 {self.state.get('building_block','BB1')} {self.mode.id}; zero exit delay; failed search is unresolved",
                    operating_mode=self.mode.record(),
                    scenario=dict(measured_departures=[3600,7200],repetitions=6,simulation_cutoff=12600,
                                  fixed='Infrastructure, train mix, routes, stops, speeds and isolated arrival offsets. Only origin departures/order change.',
                                  topology='Forward-only, one platform. Approach f01 is 9.65km with capacity 999, so faster trains can overtake there. f02–f08 are capacity-one; f03/f04 and f06/f07 share switches. '+self.mode.description(),
                                  objective_detail='All required measured trains complete with raw positive exit delay exactly zero. Do not add timetable padding. Headways only diagnostic.'),
                    reference_timing=self.reference_evidence(),
                    proposal_seeds=dict(insert_into_passing=warm,resource_gap_cycle=rule,
                                        reference_cycle_options=cycles,
                                        rule_action='rules returns the first fitting resource-gap cycle, preferring extra margins 2, then 1, then 0. If resource_gap_cycle is null, rules is unavailable; it never silently becomes random. Choose random explicitly or submit an edited pattern. A compressed_probe is only a grouped diagnostic proposal scaled into the period: it violates template gaps and must be explicitly submitted/simulated. A too-long template is not an infeasibility proof. Proposal margins are not acceptance constraints; prose edits are not executed.'),
                    period=PERIOD, allowed_volumes=[volume], compositions={str(volume):self.composition(volume)},
                    remaining_simulations=s['limits']['simulations']-s['simulations'], seconds_left=self.remaining(),
                    volume_budget=bucket, attempts_left=s['limits']['attempts_per_volume']-bucket['attempts'],
                    restart_required=bucket['restart_required'],
                    best_volume=s.get('best_volume'), references={p:b['terminal'] for p,b in s['baselines'].items()},
                    best_candidate=compact(best,True) if best else None,
                    latest_candidate=compact(candidates[-1],True) if candidates and candidates[-1] is not best else None,
                    recent_trials=[compact(t) for t in candidates[-4:]], recent_decisions=s['decisions'][-2:])

    @staticmethod
    def repair_moves(trial):
        """Single-train shifts that would clear each measured resource overlap.

        Worst first-late train first; for each candidate step: delay the late train,
        advance the occupying train, or split the shift between them.
        """
        pattern = trial['pattern']
        def index(vehicle):
            parts = vehicle.split('_')
            i = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else -1
            return i if 0 <= i < len(pattern) and pattern[i][0] == parts[0] else None
        moves, seen = [], set()
        def add(changes, reason):
            shifted = [list(row) for row in pattern]
            for i, seconds in changes:
                shifted[i][1] = (shifted[i][1]+seconds) % PERIOD
            key = json.dumps(sorted(shifted, key=lambda row:(row[1],row[0])))
            if key not in seen:
                seen.add(key)
                moves.append(dict(pattern=shifted, reason=reason))
        findings = sorted(trial.get('conflict_evidence', []), key=lambda f:-f['first_late_link']['excess_seconds'])
        for finding in findings:
            victim = index(finding['vehicle'])
            if victim is None: continue
            overlaps = [c for c in finding.get('overlapping_resource_candidates', []) if c.get('released') is not None]
            steps = sorted({math.ceil(finding['first_late_link']['excess_seconds'])}
                           | {math.ceil(c['released']-c['desired_reservation']) for c in overlaps})
            blockers = list(dict.fromkeys(b for b in (index(c['other_vehicle']) for c in overlaps) if b not in (None, victim)))
            for step in (s for s in steps if s > 0):
                add([(victim, step)], f'delay {finding["vehicle"]} by {step}s')
                for b in blockers:
                    add([(b, -step)], f'advance {pattern[b][0]}_{b:02} by {step}s')
                    add([(victim, (step+1)//2), (b, -(step//2))], f'split {step}s between {finding["vehicle"]} and {pattern[b][0]}_{b:02}')
        return moves

    def pattern_key(self, pattern):
        return hashlib.sha256(json.dumps(pattern).encode()).hexdigest()

    def rules_untried(self, volume, mark=True):
        """Whether the reference resource-gap cycle is available and unused; mark=True consumes it."""
        bucket = self.volume_state()
        if bucket.get('rules_tried'):
            return False
        if mark:
            bucket['rules_tried'] = True
        try:
            pattern = self.validate_pattern(self.rule_pattern(volume), volume)
        except ValueError:
            return False
        return self.pattern_key(pattern) not in {t['key'] for t in self.state['trials']}

    def repair_candidates(self, volume):
        """Basin best trial and its untried repair moves, in deterministic priority order."""
        bucket = self.volume_state()
        tried = {t['key'] for t in self.state['trials']}
        basin = [t for t in self.state['trials'][bucket.get('basin_start', 0):]
                 if t['volume'] == volume and not t['score']['passed'] and 'pattern' in t]
        base = min(basin, key=lambda t:quality(t['score']), default=None)
        moves = []
        for move in self.repair_moves(base) if base else []:
            pattern = self.validate_pattern(move['pattern'], volume)
            if self.pattern_key(pattern) not in tried:
                moves.append(dict(pattern=pattern, reason=move['reason']))
        return base, moves

    @staticmethod
    def submit(volume, move):
        return dict(action='submit', volume=volume, reason=move['reason'],
                    pattern=[dict(product=p, departure=t) for p,t in move['pattern']])

    def repair_action(self, volume):
        """Model-free policy: reference seed once, then repair moves from the current basin's best trial."""
        if self.volume_state()['restart_required']:
            return dict(action='random', volume=volume, pattern=[], reason='restart after plateau')
        if self.rules_untried(volume):
            return dict(action='rules', volume=volume, pattern=[], reason='reference resource-gap cycle')
        _, moves = self.repair_candidates(volume)
        return self.submit(volume, moves[0]) if moves else dict(action='random', volume=volume, pattern=[], reason='no untried repair move')

    def pick_action(self, volume):
        """A small model or decision model picks one code-generated option; code builds the timetable.

        Plateau restarts and an empty move list are handled by code without a model call.
        """
        bucket = self.volume_state()
        if bucket['restart_required']:
            return dict(action='random', volume=volume, pattern=[], reason='restart after plateau')
        base, moves = self.repair_candidates(volume)
        options = [dict(id=i, move=m['reason']) for i, m in enumerate(moves[:PICK_OPTIONS])]
        rules = self.rules_untried(volume, mark=False)
        if rules: options.append(dict(id=len(options), move='try the reference resource-gap cycle timetable'))
        options.append(dict(id=len(options), move='restart from a new random timetable'))
        if len(options) == 1:
            return dict(action='random', volume=volume, pattern=[], reason='no untried repair move')
        def outcome(t):
            return dict(move=t.get('decision'), passed=t['score']['passed'], max_delay=t['score']['max_delay'],
                        total_delay=t['score']['total_delay'], incomplete=t['score']['expected']-t['score']['completed'])
        conflicts = []
        for f in sorted(base.get('conflict_evidence', []) if base else [], key=lambda f:-f['first_late_link']['excess_seconds'])[:5]:
            overlaps = [c for c in f.get('overlapping_resource_candidates', []) if c.get('released') is not None]
            conflicts.append(dict(late_train=f['vehicle'], link=f['first_late_link']['link'], late_seconds=f['first_late_link']['excess_seconds'],
                                  blocked_by=sorted({c['other_vehicle'] for c in overlaps}),
                                  overlap_seconds=sorted({math.ceil(c['released']-c['desired_reservation']) for c in overlaps})))
        observation = dict(trains_per_half_hour=volume, composition=self.composition(volume),
                           attempts_left=self.state['limits']['attempts_per_volume']-bucket['attempts'],
                           current_best=outcome(base) if base else None, conflicts_in_current_best=conflicts,
                           recent_results=[outcome(t) for t in self.state['trials'] if t['volume']==volume][-6:],
                           options=options)
        schema = dict(type='object', additionalProperties=False, required=['choice','reason'], properties={
            'choice':dict(type='integer', enum=[o['id'] for o in options]), 'reason':dict(type='string')})
        prompt = ('You steer a railway timetable search. Goal: every train exits with zero delay. '
                  'Each option is a small edit to the current best timetable, or a restart. '
                  'Prefer edits that fix the largest late_seconds conflict; avoid directions that made recent results worse; '
                  'restart when recent edits stop improving. Reply with JSON {"choice": <option id>, "reason": <short>}.\n'
                  + json.dumps(observation))
        def validate(answer, schema):
            if (not isinstance(answer, dict) or set(answer) != {'choice','reason'} or type(answer['choice']) is not int
                    or answer['choice'] not in schema['properties']['choice']['enum'] or not isinstance(answer['reason'], str)):
                raise ValueError('model must return an offered integer choice and a reason')
            return answer
        answer = self.model_turn(prompt, observation, schema, validate)
        chosen = answer['choice']
        if chosen < len(moves[:PICK_OPTIONS]):
            action = self.submit(volume, moves[chosen])
        elif rules and chosen == len(options)-2:
            self.volume_state()['rules_tried'] = True
            action = dict(action='rules', volume=volume, pattern=[], reason='reference resource-gap cycle')
        else:
            action = dict(action='random', volume=volume, pattern=[], reason='model chose restart')
        action.update(choice=chosen, model_reason=answer['reason'][:160])
        return action

    def model_turn(self, prompt, observation, schema, validate=None):
        """Charge one model turn, record the observation and metrics, return the validated answer."""
        bucket = self.volume_state()
        if bucket['model_turns'] >= self.state['limits']['turns']:
            raise TimeoutError('per-volume model turn budget exhausted')
        bucket['model_turns'] += 1
        self.state['turns'] += 1
        self.save()
        directory = self.destination.parent/(self.destination.stem+'-'+self.state['policy'])
        directory.mkdir(exist_ok=True)
        prefix = directory/str(self.state['turns'])
        prefix.with_suffix('.prompt.json').write_text(json.dumps(observation, indent=2))
        answer, metrics = request_action(self.state['model_config'], prompt, schema, prefix,
                                         min(120, self.remaining()), bounded_process, validate)
        self.state.setdefault('model_metrics', []).append(metrics)
        self.state.setdefault('model_usage', []).extend(metrics['usage'])
        return answer

    def luna_action(self):
        bucket = self.volume_state()
        if bucket['model_turns'] >= self.state['limits']['turns']:
            raise TimeoutError('per-volume model turn budget exhausted')
        bucket['model_turns'] += 1
        self.state['turns'] += 1
        self.save()
        directory = self.destination.parent/(self.destination.stem+'-luna')
        directory.mkdir(exist_ok=True)
        prefix = directory/str(self.state['turns'])
        schema = dict(type='object', additionalProperties=False, required=['action','volume','pattern','reason'], properties={
            'action':dict(type='string',enum=['random','rules','submit','stop']), 'volume':dict(type='integer'),
            'pattern':dict(type='array',items=dict(type='object',additionalProperties=False,required=['product','departure'],properties={
                'product':dict(type='string',enum=list(self.types)), 'departure':dict(type='integer')})), 'reason':dict(type='string')})
        schema_path = directory/'schema.json'
        schema_path.write_text(json.dumps(schema))
        observation = self.observation()
        prompt = ('You control a bounded railway timetable search. Use no tools, files or shell. Return one structured action. '
                  'Choose random to restart, rules for the explicit resource-gap-cycle seed, submit to supply a complete edited timetable, or stop for this volume. '
                  'For random/rules/stop use pattern=[]. Preserve the given composition, only change departure times/order. '
                  'Headways and intermediate delays are diagnostics, never pass/fail. Simulation validates each train. '
                  'Use only the active volume; code advances immediately on success. '
                  'If restart_required is true choose random or submit a different train-product order. '
                  'Evaluate concrete timing changes against the given resource offsets, including the last-to-first period seam. '
                  'The gap-cycle seed exploits product grouping; consider it before blind random sampling. It is not a simulation result. '
                  'When resource_gap_cycle is null do not select rules; use submit for a concrete proposal/repair or random explicitly. '
                  'A too-long reference template alone cannot establish infeasibility; use measured evidence and the remaining budget. '
                  'Do not describe an edit in reason while selecting rules: submit an explicit pattern to perform that edit. '
                  'Do not repeat rejected candidates. Preserve best evidence while exploring. Observation JSON:\n'+json.dumps(observation))
        prefix.with_suffix('.prompt.json').write_text(json.dumps(observation, indent=2))
        action, metrics = request_action(self.state['model_config'], prompt, schema, prefix,
                                         min(120, self.remaining()), bounded_process)
        if action['volume'] != self.state['active_volume']:
            raise ValueError('model response must use the active volume')
        self.state.setdefault('model_metrics', []).append(metrics)
        self.state.setdefault('model_usage', []).extend(metrics['usage'])
        return action

    def make_trial(self, volume, pattern, key, run, action):
        refs = {p:b['terminal'] for p,b in self.state['baselines'].items()}
        expected = {f'{p}_{i:02}_p{k}':(t+k*PERIOD,p) for k in (2,3) for i,(p,t) in enumerate(pattern)}
        score = evaluate(run,expected,refs)
        trajectories = trace(run['events_file'])
        evidence = analyse(run, trajectories, {p:b['trajectory'] for p,b in self.state['baselines'].items()})
        for row in evidence:
            row['link_deviations'] = row['link_deviations'][:3]
            row['overlapping_resource_candidates'] = row['overlapping_resource_candidates'][:4]
        return dict(volume=volume,composition=self.composition(volume),pattern=pattern,key=key,run_id=run['run_id'],
                    input_sha256=run['input_sha256'],score=score,conflict_evidence=distinct_conflicts(evidence),decision=action.get('reason'),
                    entry_diagnostics=entry_diagnostics(run,trajectories))

    def record_trial(self, trial):
        self.state['trials'].append(trial)
        # Later members of a parallel batch may finish a volume that already passed.
        if trial['volume'] == self.state['active_volume']:
            self.record_progress(trial)
        self.save()
        print(json.dumps(dict(policy=self.state['policy'],volume=trial['volume'],passed=trial['score']['passed'],
                              max_delay=trial['score']['max_delay'],simulations=self.state['simulations'],
                              volume_attempts=self.state['volumes'][str(trial['volume'])]['attempts'])),flush=True)

    def slot_workspaces(self, count):
        """Frozen copies of the search workspace, one per concurrent simulation."""
        slots = [self.state['workspace']]+[f"{self.state['workspace']}-w{i}" for i in range(1, count)]
        for slot in slots[1:]:
            if not (self.ws.workspace_root/slot).exists():
                shutil.copytree(self.ws.workspace_root/self.state['workspace'], self.ws.workspace_root/slot)
        return slots

    def simulate_batch(self, patterns):
        """Simulate patterns concurrently, one frozen workspace copy each; every call is charged first."""
        bucket = self.volume_state()
        limits = self.state['limits']
        if (self.remaining() <= 0 or self.state['simulations']+len(patterns) > limits['simulations']
                or bucket['simulation_calls']+len(patterns) > limits['attempts_per_volume']):
            raise TimeoutError('simulation/time budget exhausted')
        bucket['simulation_calls'] += len(patterns)
        self.state['simulations'] += len(patterns)
        self.save()
        slots = self.slot_workspaces(len(patterns))
        for slot, pattern in zip(slots, patterns):
            server.replace_timetable(slot, pattern_departures(pattern,self.mode.routes), 6, PERIOD)
            if self.fingerprint(slot) != self.state['frozen']: raise ValueError('frozen inputs changed')
        def worker(slot):
            command = [os.sys.executable, '-m', 'railsim_mcp.search', '--worker', slot, '--project', str(self.settings.project),
                       '--state-root', str(self.settings.state), '--seed', str(self.state['seed'])]
            return bounded_process(command, self.remaining(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                   env=worker_environment())
        with ThreadPoolExecutor(len(slots)) as pool:
            results = list(pool.map(worker, slots))
        for slot in slots:
            if self.fingerprint(slot) != self.state['frozen']: raise ValueError('simulation changed frozen inputs')
        failed = next((r for r in results if r.returncode), None)
        if failed: raise RuntimeError(failed.stderr[-4000:])
        return [json.loads(r.stdout) for r in results]

    def batch_actions(self, volume, count):
        bucket = self.volume_state()
        def random_action(reason): return dict(action='random', volume=volume, pattern=[], reason=reason)
        actions = []
        if bucket['attempts'] == 0:
            actions.append(dict(action='warm',volume=volume,reason='Insert required products into the best passing pattern',pattern=[]))
        if bucket['restart_required']:
            return actions+[random_action('restart after plateau')]*(count-len(actions))
        if self.state['policy'] == 'repair':
            if len(actions) < count and self.rules_untried(volume):
                actions.append(dict(action='rules', volume=volume, pattern=[], reason='reference resource-gap cycle'))
            _, moves = self.repair_candidates(volume)
            actions += [self.submit(volume, move) for move in moves[:count-len(actions)]]
        return actions+[random_action('parallel exploration')]*(count-len(actions))

    def batch_step(self):
        """Propose up to `workers` timetables, simulate them concurrently, record in proposal order."""
        volume, bucket, limits = self.state['active_volume'], self.volume_state(), self.state['limits']
        # A new volume first tries the warm insert alone: it usually passes, and a full
        # batch there would spend simulations that a fixed budget needs later.
        count = min(1 if bucket['attempts'] == 0 else self.workers,
                    limits['attempts_per_volume']-bucket['attempts'], 300*self.workers-self.state['attempts'],
                    limits['simulations']-self.state['simulations'], limits['attempts_per_volume']-bucket['simulation_calls'])
        if count <= 0:
            raise TimeoutError('per-volume simulation budget exhausted')
        bucket['status'] = 'searching'
        actions = [copy.deepcopy(a) for a in self.batch_actions(volume, count)]
        self.state['attempts'] += len(actions)
        bucket['attempts'] += len(actions)
        keys, accepted = {t['key'] for t in self.state['trials']}, []
        for action in actions:
            self.state['decisions'].append(action)
            try:
                pattern = self.validate_pattern(
                    self.warm_pattern(volume) if action['action']=='warm'
                    else self.random_pattern(volume) if action['action']=='random'
                    else self.rule_pattern(volume) if action['action']=='rules'
                    else [[r['product'],r['departure']] for r in action['pattern']], volume)
                key = self.pattern_key(pattern)
                if key in keys: raise ValueError('duplicate timetable')
                if bucket['restart_required']:
                    if action['action'] != 'random': raise ValueError('plateau: restart randomly')
                    bucket.update(restart_required=False,stagnant=0,restarts=bucket['restarts']+1,
                                  basin_start=len(self.state['trials']))
                    action['plateau_restart'] = True
            except (ValueError, TypeError, KeyError) as exc:
                action['rejected'] = str(exc)
                continue
            keys.add(key)
            accepted.append((action, pattern, key))
        self.save()
        if not accepted:
            return
        runs = self.simulate_batch([pattern for _, pattern, _ in accepted])
        for (action, pattern, key), run in zip(accepted, runs):
            self.record_trial(self.make_trial(volume, pattern, key, run, action))

    def run(self):
        try:
            if self.state['policy'] in MODEL_POLICIES:
                self.state['model_installation'] = check_runner(self.state['model_config'], self.remaining(), bounded_process)
                self.save()
            self.setup()
            self.state['status'] = 'searching'
            while True:
                if self.state.get('targets_completed'):
                    self.state.update(status='targets_completed',stop_reason='all requested volumes tested; consult each volume status')
                    break
                if self.remaining() <= 0 or self.state['simulations'] >= self.state['limits']['simulations']:
                    self.state.update(status='budget_exhausted',stop_reason='overall simulation/time budget exhausted')
                    break
                if self.state['active_volume'] > 100:
                    self.state.update(status='implementation_ceiling',stop_reason='100 trains per period implementation bound')
                    break
                bucket = self.volume_state()
                if (bucket['attempts'] >= self.state['limits']['attempts_per_volume']
                        or bucket['model_turns'] >= self.state['limits']['turns']):
                    bucket['status'] = 'unresolved'
                    if self.state.get('target_volumes'):
                        self.next_volume()
                        self.save()
                        continue
                    self.state.update(status='volume_unresolved',stop_reason='per-volume attempt/turn budget exhausted')
                    break
                if self.state['attempts'] >= 300*self.workers:
                    self.state.update(status='budget_exhausted',stop_reason='overall attempt safety limit exhausted')
                    break
                if self.workers > 1:
                    self.batch_step()
                    continue
                volume = self.state['active_volume']
                bucket['status'] = 'searching'
                self.state['attempts'] += 1
                bucket['attempts'] += 1
                self.save()
                if bucket['attempts'] == 1 and not (self.state.get('target_volumes') and self.state['policy'] in MODEL_POLICIES):
                    action = dict(action='warm',volume=volume,reason='Insert required products into the best passing pattern',pattern=[])
                elif self.state['policy'] in ('luna','model'):
                    action = self.luna_action()
                elif self.state['policy'] == 'repair':
                    action = self.repair_action(volume)
                elif self.state['policy'] == 'pick':
                    action = self.pick_action(volume)
                else:
                    action = dict(action='random' if bucket['restart_required'] else self.state['policy'],
                                  volume=volume,reason='restart after plateau' if bucket['restart_required'] else 'repair current volume',pattern=[])
                self.state['decisions'].append(action)
                if action['action'] == 'stop':
                    bucket['status'] = 'unresolved'
                    if self.state.get('target_volumes'):
                        self.next_volume()
                        self.save()
                        continue
                    self.state.update(status='policy_stopped',stop_reason='policy stopped; active volume unresolved')
                    break
                try:
                    if type(action['volume']) is not int or action['volume'] != volume:
                        raise ValueError('only the active volume may be searched')
                    pattern = (self.warm_pattern(volume) if action['action']=='warm'
                               else self.random_pattern(volume) if action['action']=='random'
                               else self.rule_pattern(volume) if action['action']=='rules'
                               else [[r['product'],r['departure']] for r in action['pattern']] if action['action']=='submit'
                               else None)
                    pattern = self.validate_pattern(pattern,volume)
                    key = hashlib.sha256(json.dumps(pattern).encode()).hexdigest()
                    if any(t['key']==key for t in self.state['trials']): raise ValueError('duplicate timetable')
                    if bucket['restart_required']:
                        best = next((t for t in self.state['trials'] if t['key']==bucket['best_key']),None)
                        if action['action'] != 'random' and (action['action'] != 'submit' or
                                best and [p for p,_ in best['pattern']]==[p for p,_ in pattern]):
                            raise ValueError('plateau: restart randomly or submit a different product order')
                        bucket.update(restart_required=False,stagnant=0,restarts=bucket['restarts']+1,
                                      basin_start=len(self.state['trials']))
                        action['plateau_restart'] = True
                except (ValueError, TypeError, KeyError) as exc:
                    action['rejected'] = str(exc)
                    self.save()
                    # A deterministic duplicate invokes an explicit exploratory restart next time.
                    if self.state['policy']=='rules': bucket['restart_required'] = True
                    continue
                if self.fingerprint() != self.state['frozen']: raise ValueError('frozen inputs changed')
                run = self.simulate(pattern)
                if self.fingerprint() != self.state['frozen']: raise ValueError('simulation changed frozen inputs')
                self.record_trial(self.make_trial(volume, pattern, key, run, action))
        except (TimeoutError, subprocess.TimeoutExpired) as exc:
            self.state['status'] = 'budget_exhausted'
            self.state['stop_reason'] = str(exc)
        except BaseException as exc:
            self.state['status'] = 'error'
            self.state['stop_reason'] = str(exc)
            raise
        finally:
            if self.state['status'] in ('budget_exhausted','error') and self.state['active_volume'] <= 100:
                bucket = self.volume_state()
                bucket['status'] = 'unresolved' if bucket['attempts'] else 'untested'
            self.save()


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--project', required=True)
    p.add_argument('--state-root', required=True)
    p.add_argument('--output', type=Path)
    p.add_argument('--policy', choices=['random','rules','repair','pick','luna','model'], default='random')
    p.add_argument('--model-runner', choices=['codex','copilot','openai','anthropic'], default='codex')
    p.add_argument('--model', help='explicit CLI model ID (Copilot requires this); no automatic fallback')
    p.add_argument('--seed',type=int,default=4711)
    p.add_argument('--simulations',type=int,default=100)
    p.add_argument('--seconds',type=int,default=1800)
    p.add_argument('--turns',type=int,default=30,help='maximum model turns per volume')
    p.add_argument('--attempts-per-volume',type=int,default=30)
    p.add_argument('--warm-start',type=Path,help='reuse validated witness/calibration from an earlier checkpoint')
    p.add_argument('--target-volumes',type=int,nargs='+',help='independently test these train counts per half-hour; stop after these targets')
    p.add_argument('--mode',help='declared UC1 mode, e.g. MAINLINE/FV_STOP; defaults to MAINLINE/FV_PASS for new searches')
    p.add_argument('--workers',type=int,default=1,help='concurrent simulations per step (repair/random policies)')
    p.add_argument('--building-block',default='BB1',choices=['BB1','BB2','BB3'])
    p.add_argument('--resume',action='store_true')
    p.add_argument('--worker')
    a=p.parse_args()
    settings=Settings.load(a.project,a.state_root)
    if a.worker:
        def cancel(signum, frame): raise TimeoutError('worker cancelled')
        signal.signal(signal.SIGTERM, cancel)
        ws=ScenarioWorkspace(settings.project/'src/main/resources/scenarios',settings.state/'workspaces',project_root=settings.project,runs_root=settings.state/'runs')
        print(json.dumps(ws.run_once(a.worker,a.seed,12600)))
    else:
        if not a.output or min(a.simulations,a.seconds,a.turns,a.attempts_per_volume)<=0: p.error('positive limits and --output required')
        Search(settings,a.output,a.policy,a.seed,a.simulations,a.seconds,a.turns,a.resume,a.attempts_per_volume,a.warm_start,a.target_volumes,a.mode,a.model_runner,a.model,a.workers,a.building_block).run()

if __name__=='__main__': main()
