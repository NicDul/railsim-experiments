"""Canonical periodic fixed-profile timing/platform model (integer seconds)."""

import time
from ortools.sat.python import cp_model
from .profiles import intervals


def solve(
    products,
    options,
    windows,
    seconds,
    rules,
    workers,
    min_gap=0,
    no_overlap=False,
    *,
    period=1800,
    seed=4711,
    order=None,
    platforms=None,
    entry=None,
    resource_orders=None,
    hint=None,
    entry_orders=None,
    forbidden_patterns=None,
):
    """Any departure and platform variant per train; exclusive holds at least min_gap seconds apart.

    Identical trains are interchangeable, so they are kept in departure order, and the first train
    departs at 0 (every timetable can be shifted so that it does). Each pair of possible holds gets
    one disjunction; no_overlap adds the same condition once per resource, which propagates better
    on some instances and worse on others.
    """
    model = cp_model.CpModel()
    n = len(products)
    departure = [model.NewIntVar(0, period - 1, f"d{i}") for i in range(n)]
    choice = [
        {v: model.NewBoolVar(f"x{i}_{v}") for v in options[products[i]]}
        for i in range(n)
    ]
    model.Add(departure[0] == 0)
    for i in range(n):
        model.AddExactlyOne(choice[i].values())
        for v, x in choice[i].items():
            if any(
                r - g + min_gap > period
                for value in windows[v].values() for _, g, r in intervals(value)
            ):
                model.Add(x == 0)
        if (
            order is None
            and not resource_orders
            and i
            and products[i] == products[i - 1]
        ):
            model.Add(departure[i] > departure[i - 1])

    if order is not None and entry_orders is not None:
        raise ValueError("use order or entry_orders, not both")
    if order is not None:
        if list(order) != list(products):
            raise ValueError("order solve requires products in proposed order")
        grants = []
        for i, product in enumerate(products):
            values = {windows[v][entry][1] for v in options[product]}
            if len(values) != 1:
                raise ValueError("entry grant must be platform independent")
            grants.append(values.pop())
            if platforms and platforms[i] != "any":
                for v, x in choice[i].items():
                    if not v.endswith("_" + platforms[i]):
                        model.Add(x == 0)
        # Cyclic entry order relative to first train, including wrap.
        arrivals = [model.NewIntVar(0, period - 1, f"e{i}") for i in range(n)]
        for i in range(n):
            model.AddModuloEquality(
                arrivals[i], departure[i] + grants[i] - grants[0] + period * 10, period
            )
        for i in range(1, n):
            model.Add(arrivals[i] > arrivals[i - 1])

    if entry_orders is not None:
        covered = set()
        for resource, indices in sorted(entry_orders.items()):
            if not isinstance(resource, str) or not indices:
                raise ValueError("each entry order requires a resource and train indices")
            if len(set(indices)) != len(indices) or any(
                type(i) is not int or i < 0 or i >= n for i in indices
            ):
                raise ValueError("entry order indices must be distinct train slots")
            if covered.intersection(indices):
                raise ValueError("a train slot cannot use multiple entry orders")
            covered.update(indices)
            grants = []
            for i in indices:
                values = [
                    windows[v][resource][1]
                    for v in options[products[i]]
                    if resource in windows[v]
                ]
                if len(values) != len(options[products[i]]) or len(set(values)) != 1:
                    raise ValueError(
                        "entry grant must exist and be variant independent for every train"
                    )
                grants.append(values[0])
            arrivals = [
                model.NewIntVar(0, period - 1, f"e_{resource}_{i}")
                for i in indices
            ]
            for position, i in enumerate(indices):
                model.AddModuloEquality(
                    arrivals[position],
                    departure[i] + grants[position] - grants[0] + period * 10,
                    period,
                )
            for position in range(1, len(indices)):
                model.Add(arrivals[position] > arrivals[position - 1])
        if covered != set(range(n)):
            raise ValueError("entry orders must cover every train slot exactly once")

    if forbidden_patterns:
        selected_variant = []
        for i in range(n):
            variants = list(options[products[i]])
            index = model.NewIntVar(0, len(variants) - 1, f"variant_{i}")
            for variant_index, variant in enumerate(variants):
                model.Add(index == variant_index).OnlyEnforceIf(choice[i][variant])
            selected_variant.append(index)
        assignments = departure + selected_variant
        variant_indices = {
            i: {variant: j for j, variant in enumerate(options[products[i]])}
            for i in range(n)
        }
        for pattern in forbidden_patterns:
            if len(pattern) != n:
                raise ValueError("forbidden pattern must contain one row per train slot")
            times = []
            variants = []
            for i, row in enumerate(pattern):
                if (
                    len(row) != 3
                    or row[0] != products[i]
                    or row[2] not in variant_indices[i]
                    or type(row[1]) is not int
                ):
                    raise ValueError("forbidden pattern does not match train slots")
                times.append(row[1])
                variants.append(variant_indices[i][row[2]])
            model.AddForbiddenAssignments(assignments, [times + variants])

    from .guidance import constrain

    constrain(model, departure, choice, windows, resource_orders, period)

    def apart(i, j, first, second, when):
        # first (window of train i) and second (window of train j) never overlap, in any period:
        # a1 - b0 + gap <= d_j - d_i + k*period <= period - (b1 - a0) - gap for some integer k.
        (a0, a1), (b0, b1) = first, second
        low, high = a1 - b0 + min_gap, period - (b1 - a0) - min_gap
        if low > high:
            model.AddBoolOr([w.Not() for w in when])
            return
        smallest = -(
            -(low - (period - 1)) // period
        )  # departure differences lie in -(P-1)..P-1
        largest = (high + period - 1) // period
        k = model.NewIntVar(smallest, largest, "")
        gap = departure[j] - departure[i] + period * k
        model.Add(gap >= low).OnlyEnforceIf(when)
        model.Add(gap <= high).OnlyEnforceIf(when)

    for i in range(n):
        for v, x in choice[i].items():
            for value in windows[v].values():
                visits = intervals(value)
                for a, (_, ga, ra) in enumerate(visits):
                    for _, gb, rb in visits[a + 1:]:
                        apart(i, i, (ga, ra), (gb, rb), [x])

    for i in range(n):
        for j in range(i + 1, n):
            for vi, xi in choice[i].items():
                for vj, xj in choice[j].items():
                    for resource in sorted(set(windows[vi]) & set(windows[vj])):
                        for ci, gi, ri in intervals(windows[vi][resource]):
                            for cj, gj, rj in intervals(windows[vj][resource]):
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
                for resource, value in windows[v].items():
                    for _, g, r in intervals(value):
                        for m in range(3):
                            holds.setdefault(resource, []).append(
                                model.NewOptionalFixedSizeIntervalVar(
                                    departure[i] + g % period + m * period,
                                    max(r - g + min_gap, 1),
                                    x,
                                    "",
                                )
                            )
        for resource_intervals in holds.values():
            model.AddNoOverlap(resource_intervals)
    if hint:
        by_group = {}
        for group, t, v in hint:
            by_group.setdefault(group, []).append((t, v))
        for group in by_group:
            by_group[group].sort()
        for i, group in enumerate(products):
            if by_group.get(group):
                t, v = by_group[group].pop(0)
                if 0 <= t < period and v in choice[i]:
                    model.AddHint(departure[i], t)
                    for variant, x in choice[i].items():
                        model.AddHint(x, int(variant == v))
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = seconds
    solver.parameters.num_workers = workers
    solver.parameters.random_seed = seed
    started = time.monotonic()
    status = solver.Solve(model)
    result = dict(
        status=solver.StatusName(status),
        solve_seconds=round(time.monotonic() - started, 2),
    )
    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        slot_pattern = [
            [
                products[i],
                solver.Value(departure[i]),
                next(v for v, x in choice[i].items() if solver.Value(x)),
            ]
            for i in range(n)
        ]
        rows = sorted(slot_pattern, key=lambda r: (r[1], r[0]))
        result["pattern"] = rows
        result["slot_pattern"] = slot_pattern
    return result
