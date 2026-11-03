"""UC1 trajectory and cyclic-gap helpers from the original optimizer."""
import gzip, math, xml.etree.ElementTree as ET
from functools import lru_cache
PERIOD=1800
ROUTES={"FV":"FV_LR","RV":"RV_LMR","GV":"GV_LR"}
HEADWAY={"FV":120,"RV":90,"GV":180}
TYPES=tuple(ROUTES)
def stamp(t):
    t = int(t)
    return f'{t//3600:02}:{t%3600//60:02}:{t%60:02}'


def events(path):
    with gzip.open(path) as stream:
        for _, element in ET.iterparse(stream, events=['end']):
            if element.tag == 'event':
                yield dict(element.attrib)
            element.clear()


def trace(path):
    trains = {}
    for e in events(path):
        vehicle = e.get('vehicle')
        if not vehicle:
            continue
        row = trains.setdefault(vehicle, {'entries': {}, 'leaves': {}, 'facilities': [], 'resources': {}})
        t = float(e['time'])
        kind = e['type']
        if kind == 'entered link':
            row['entries'].setdefault(e['link'], t)
        elif kind == 'railsimTrainLeavesLinkEvent':
            row['leaves'][e['link']] = t
        elif kind in ('VehicleArrivesAtFacility', 'VehicleDepartsAtFacility'):
            row['facilities'].append(e)
        elif kind == 'railsimLinkStateChangeEvent' and e.get('link') in {f'f{i:02}' for i in range(2, 9)}:
            resource = row['resources'].setdefault(e['link'], {})
            if e['state'] != 'EMPTY':
                resource.setdefault('reserved', t)
            else:
                resource['released'] = t
    return trains


def pattern_departures(pattern, routes=None):
    routes = ROUTES if routes is None else routes
    return [{'route_id':routes[product], 'departure_id':f'{product}_{i:02}',
             'vehicle_id':f'{product}_{i:02}', 'vehicle_type':product,
             'departure_time':stamp(departure)} for i,(product,departure) in enumerate(pattern)]


def best_cycle(volume, gaps, counts=None, types=TYPES):
    """Exact minimum pair-gap cycle for the small fixed product-count state space."""
    if counts is None:
        if volume <= 0 or volume % 5:
            raise ValueError('exact MAINLINE mix requires a positive multiple of five')
        counts = (volume*2//5, volume*2//5, volume//5)
    if len(counts)!=len(types) or sum(counts) != volume or any(n < 0 for n in counts) or volume<=0:
        raise ValueError('nonnegative product counts must sum to positive volume')
    first_index = next(i for i,n in enumerate(counts) if n>0)
    first = types[first_index]
    @lru_cache(None)
    def visit(remaining, previous):
        if not any(remaining):
            return gaps[previous, first], ()
        choices = []
        for i, product in enumerate(types):
            if remaining[i]:
                rest = list(remaining); rest[i] -= 1
                cost, suffix = visit(tuple(rest), product)
                choices.append((gaps[previous, product]+cost, (product,)+suffix))
        return min(choices)
    rest = list(counts); rest[first_index] -= 1
    cost, suffix = visit(tuple(rest), first)
    sequence = (first,) + suffix
    return cost, sequence


def cyclic_pattern(sequence, gaps):
    cost = sum(gaps[a,b] for a,b in zip(sequence, sequence[1:]+sequence[:1]))
    if cost > PERIOD:
        return None
    slack = PERIOD-cost
    pattern, t = [], 0
    for i, a in enumerate(sequence):
        pattern.append((a,t))
        b = sequence[(i+1)%len(sequence)]
        t += gaps[a,b] + slack//len(sequence) + (i < slack%len(sequence))
    return pattern


def analyse(run, traces, baselines):
    findings = []
    for train in run['trains']:
        v = train['vehicle_id']; product = v.split('_')[0]
        if product not in baselines:
            continue
        actual, base = traces.get(v), baselines[product]
        if actual is None:
            continue
        deviations = [{'link': link, 'actual': t, 'isolated_expected': train['scheduled_departure']+base['entries'][link],
                       'excess_seconds': t-train['scheduled_departure']-base['entries'][link]}
                      for link,t in actual['entries'].items() if link in base['entries']
                      and t-train['scheduled_departure']-base['entries'][link] > 0]
        if deviations:
            first = deviations[0]
            candidates = []
            for other, other_trace in traces.items():
                if other == v:
                    continue
                for link, occ in other_trace['resources'].items():
                    desired = train['scheduled_departure']+base['resources'][link]['reserved']
                    if occ.get('reserved', math.inf) <= desired < occ.get('released', -math.inf):
                        candidates.append({'other_vehicle': other, 'link': link,
                                           'desired_reservation': desired, **occ})
            findings.append({'vehicle': v, 'first_late_link': first, 'link_deviations': deviations,
                             'overlapping_resource_candidates': candidates,
                             'causality': 'temporal resource-overlap candidates, not proven dispatcher causality'})
    return findings


