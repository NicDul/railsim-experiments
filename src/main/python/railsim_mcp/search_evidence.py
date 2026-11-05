"""Bounded, measured-period evidence for a timetable-search policy.

Exclusive capacity allocations are distinct from DLA look-ahead claim markers.
This module supplies timing evidence, never a substitute for simulation success.
"""
from collections import defaultdict
import json
import math
from pathlib import Path
import xml.etree.ElementTree as ET

from .uc1 import events, PERIOD, TYPES


def merged(intervals):
    out = []
    for a, b in sorted(intervals):
        if b < a:
            raise ValueError('negative resource interval')
        if out and a <= out[-1][1]:
            out[-1] = (out[-1][0], max(b, out[-1][1]))
        else:
            out.append((a, b))
    return out


def layout(network):
    result = {}
    for link in ET.parse(network).getroot().findall('./links/link'):
        attrs = {a.get('name'): a.text for a in link.findall('./attributes/attribute')}
        if attrs.get('railsimTrainCapacity') == '1':
            result[link.get('id')] = attrs.get('railsimResourceId', link.get('id'))
    return result


def holds(path, links, horizon=None):
    active, groups, censored = {}, defaultdict(list), set()
    for e in events(path):
        if e['type'] != 'railsimLinkStateChangeEvent' or e.get('dla') == 'true' or e.get('link') not in links:
            continue
        link, vehicle, t = e['link'], e['vehicle'], float(e['time'])
        if e['state'] == 'EXHAUSTED':
            if link in active and active[link][0] != vehicle:
                raise ValueError('conflicting exclusive owners')
            active.setdefault(link, (vehicle, t))
        elif e['state'] == 'EMPTY' and link in active:
            owner, start = active.pop(link)
            if owner != vehicle or t < start:
                raise ValueError('inconsistent resource release')
            groups[links[link], vehicle].append((start, t))
    if active and horizon is None:
        raise ValueError('unclosed exclusive holds')
    for link,(vehicle,start) in active.items():
        if type(horizon) not in (int,float) or not math.isfinite(horizon) or horizon<start:
            raise ValueError('invalid censoring horizon')
        key=(links[link],vehicle)
        groups[key].append((start,horizon));censored.add(key)
    rows = [dict(resource=r, vehicle=v, start=a, end=b,
                 **({'right_censored':True} if (r,v) in censored and b==horizon else {}))
            for (r, v), intervals in groups.items() for a, b in merged(intervals)]
    for r in set(links.values()):
        spans = [(x['start'], x['end']) for x in rows if x['resource'] == r]
        if sum(b-a for a,b in spans) != sum(b-a for a,b in merged(spans)):
            raise ValueError('overlapping exclusive owners on ' + r)
    return rows


def reference_context(runs_root, baselines):
    templates, links = {}, None
    for product, base in baselines.items():
        run = json.loads((runs_root / base['run_id'] / 'run.json').read_text())
        current = layout(Path(run['snapshot_directory']) / 'network.xml')
        if links is not None and current != links:
            raise ValueError('reference resource maps differ')
        links = current
        intervals = holds(run['events_file'], links)
        templates[product] = {row['resource']: dict(granted=row['start'], released=row['end']) for row in intervals}
    resources = {r: [link for link, resource in links.items() if resource == r] for r in dict.fromkeys(links.values())}
    pair_gaps = {}
    types = tuple(baselines)
    for a in types:
        for b in types:
            # Only resources both products use constrain their order (BB2/BB3 have separate platforms).
            pair_gaps[a, b] = max([1]+[templates[a][r]['released'] - templates[b][r]['granted']
                                       for r in resources if r in templates[a] and r in templates[b]])
    return dict(resources=resources, exclusive_reference_offsets=templates,
                same_resource_order_gap_seconds={a: {b: pair_gaps[a,b] for b in types} for a in types},
                meaning='Offsets from scheduled origin departure. Gap matrix makes isolated exclusive-hold templates disjoint if resource order follows this product sequence, including last→first. It is a proposal heuristic, not a feasibility proof; feeder overtakes and braking may change order/timing.')


def measured_context(run, traces, references):
    links = layout(Path(run['snapshot_directory']) / 'network.xml')
    intervals = holds(run['events_file'], links, run.get('horizon_seconds'))
    cohort = sorted((t for t in run['trains'] if 3600 <= t['scheduled_departure'] < 5400), key=lambda t:t['scheduled_departure'])
    rows = []
    for train in cohort:
        vehicle = train['vehicle_id']; product = vehicle.split('_')[0]; dep = train['scheduled_departure']
        overlaps = []
        for r, reference in references['exclusive_reference_offsets'][product].items():
            needed = dep + reference['granted']
            own = next((i for i in intervals if i['resource'] == r and i['vehicle'] == vehicle), None)
            for held in intervals:
                if held['resource'] == r and held['vehicle'] != vehicle and held['start'] <= needed < held['end']:
                    overlaps.append(dict(resource=r, holder=held['vehicle'], reference_needed=needed-3600,
                                         holder_releases=None if held.get('right_censored') else held['end']-3600,
                                         **(dict(release_unknown_at_horizon=True,holder_observed_until=held['end']-3600) if held.get('right_censored') else {}),
                                         overlap_seconds=held['end']-needed,
                                         actual_grant=None if own is None else own['start']-3600,
                                         crosses_period=not held['vehicle'].endswith('_p2')))
        deviations = []
        base = references.get('entry_offsets', {}).get(product, {})
        for link, t in traces.get(vehicle, {}).get('entries', {}).items():
            if link in base and t-dep-base[link] > 0:
                deviations.append(dict(link=link, delay=t-dep-base[link]))
        rows.append(dict(vehicle=vehicle, slot=int(vehicle.split('_')[1]), product=product, departure=dep-3600,
                         exit_delay=train.get('terminal_delay_seconds'), first_late_links=deviations[:2],
                         reservation_overlap_candidates=sorted(overlaps, key=lambda x:x['reference_needed'])[:5]))
    usage = []
    for r in references['resources']:
        busy = sum(max(0, min(5400,i['end'])-max(3600,i['start'])) for i in intervals if i['resource']==r)
        usage.append(dict(resource=r, exclusive_busy=busy, idle=PERIOD-busy, utilization_percent=round(100*busy/PERIOD,1)))
    periods = []
    for k in range(6):
        trains = [t for t in run['trains'] if k*PERIOD <= t['scheduled_departure'] < (k+1)*PERIOD]
        delays = [max(0,t['terminal_delay_seconds']) for t in trains if t.get('terminal_delay_seconds') is not None]
        periods.append(dict(period=k, expected=len(trains), completed=sum(t['status']=='terminal_arrived' for t in trains),
                            total_delay=sum(delays), max_delay=max(delays, default=None)))
    return dict(representative_measured_period=2, train_rows=rows, resource_usage=usage, period_evolution=periods,
                timing_origin='Times in train/overlap rows are seconds after 3600; may be negative for a preceding-period holder.',
                caveat='Overlap candidates are timing evidence, not proven dispatcher causality. Open holds are right-censored at the known simulation horizon: release remains unknown, not invented. Unfinished trains remain incomplete. Idle time is fragmented; all-resource compatibility and cyclic seam matter.')
