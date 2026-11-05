"""Cyclic resource orders: structural guidance without a common network entry."""

import json


def catalogue(products, options, windows):
    trains = {
        f"t{i:03}": dict(index=i, group=p, variants=options[p])
        for i, p in enumerate(products)
    }
    resources = {}
    for resource in sorted({r for w in windows.values() for r in w}):
        visitors = []
        ambiguous = False
        for token, train in trains.items():
            use = [resource in windows[v] for v in train["variants"]]
            if any(use) and not all(use):
                ambiguous = True
                break
            if all(use):
                visitors.append(token)
        if not ambiguous and len(visitors) >= 2:
            resources[resource] = visitors
    return dict(trains=trains, resources=resources)


def validate(value, products, options, windows):
    if (
        not isinstance(value, dict)
        or not isinstance(value.get("resource_orders"), list)
        or not value["resource_orders"]
    ):
        raise ValueError("requires nonempty resource_orders")
    allowed = catalogue(products, options, windows)
    clean = []
    seen = set()
    for row in value["resource_orders"]:
        if not isinstance(row, dict):
            raise ValueError("resource order must be an object")
        resource, tokens = row.get("resource"), row.get("trains")
        if (
            not isinstance(resource, str)
            or resource not in allowed["resources"]
            or resource in seen
        ):
            raise ValueError("unknown, variant-dependent or duplicate resource")
        if (
            not isinstance(tokens, list)
            or any(not isinstance(t, str) for t in tokens)
            or len(tokens) != len(set(tokens))
            or set(tokens) != set(allowed["resources"][resource])
        ):
            raise ValueError("resource order must contain exactly every visitor once")
        rotations = [tokens[i:] + tokens[:i] for i in range(len(tokens))]
        canonical = min(rotations)
        clean.append(
            dict(
                resource=resource,
                indices=[allowed["trains"][t]["index"] for t in canonical],
            )
        )
        seen.add(resource)
    clean.sort(key=lambda row: row["resource"])
    return clean, json.dumps(clean, sort_keys=True)


def constrain(model, departure, choice, windows, resource_orders, period):
    """Local grant phases relative to each resource's own first visitor.

    Selected variant determines its measured grant offset. Distinct resources
    may have distinct orders; no artificial order between nonconflicting trains.
    """
    for row in resource_orders or []:
        resource, indices = row["resource"], row["indices"]
        grants = {}
        for i in indices:
            values = [windows[v][resource][1] for v in choice[i]]
            grant = model.NewIntVar(min(values), max(values), f"grant_{resource}_{i}")
            for v, x in choice[i].items():
                model.Add(grant == windows[v][resource][1]).OnlyEnforceIf(x)
            grants[i] = grant
        anchor = indices[0]
        spread = max(windows[v][resource][1] for i in indices for v in choice[i]) - min(
            windows[v][resource][1] for i in indices for v in choice[i]
        )
        offset = ((spread + period) // period + 1) * period
        phases = []
        for i in indices:
            phase = model.NewIntVar(0, period - 1, f"phase_{resource}_{i}")
            model.AddModuloEquality(
                phase,
                departure[i] + grants[i] - departure[anchor] - grants[anchor] + offset,
                period,
            )
            phases.append(phase)
        for a, b in zip(phases, phases[1:]):
            model.Add(b > a)


def valid_parts(value, products, options, windows):
    """Accept independently valid resource restrictions, reporting every dropped row.

    Removing a malformed restriction never creates a proof: guided results still
    concern the accepted subset only. An entirely invalid proposal is rejected.
    """
    if not isinstance(value, dict) or not isinstance(
        value.get("resource_orders"), list
    ):
        raise ValueError("requires resource_orders list")
    accepted = []
    errors = []
    seen = set()
    for index, row in enumerate(value["resource_orders"]):
        try:
            clean, _ = validate({"resource_orders": [row]}, products, options, windows)
            if clean[0]["resource"] in seen:
                raise ValueError("duplicate resource")
            seen.add(clean[0]["resource"])
            accepted.extend(clean)
        except ValueError as exc:
            errors.append(
                dict(
                    index=index,
                    status="invalid_resource_order",
                    error=str(exc),
                    source=row,
                )
            )
    if not accepted:
        raise ValueError("no valid resource orders: " + json.dumps(errors))
    accepted.sort(key=lambda row: row["resource"])
    return accepted, json.dumps(accepted, sort_keys=True), errors
