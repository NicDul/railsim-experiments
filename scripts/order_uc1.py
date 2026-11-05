"""Historical proof helper imports; use railsim-capacity for search."""
from railsim_mcp.profiles import reference_windows
from railsim_mcp.uc1 import PERIOD
from ortools.sat.python import cp_model
import time
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
