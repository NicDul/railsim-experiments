"""Raw terminal arrival witness gate, independent of aggregate delay rounding."""

import math


def evaluate(run, expected, references):
    """Independent expected IDs -> raw terminal times; no rounded aggregate gate."""
    rows = run.get("trains", [])
    by_id = {}
    errors = []
    for row in rows:
        ident = row.get("vehicle_id")
        if ident in by_id:
            errors.append("duplicate vehicle: " + str(ident))
        by_id[ident] = row
    delays = []
    missing = []
    for ident, (departure, product) in expected.items():
        row = by_id.get(ident)
        if row is None or row.get("status") != "terminal_arrived":
            missing.append(ident)
            continue
        actual = row.get("actual_arrival")
        reported = row.get("terminal_delay_seconds")
        planned = departure + references[product]
        if (
            row.get("scheduled_departure") != departure
            or row.get("planned_arrival") != planned
            or any(
                type(n) not in (int, float) or not math.isfinite(n)
                for n in (actual, reported)
            )
        ):
            errors.append("invalid times or changed reference: " + ident)
            continue
        delay = max(0, actual - planned)
        if max(0, reported) != delay:
            errors.append("inconsistent raw delay: " + ident)
        delays.append(delay)
    complete = (
        bool(expected) and not missing and not errors and len(delays) == len(expected)
    )
    return dict(
        passed=run.get("status") == "completed"
        and complete
        and all(d == 0 for d in delays),
        expected=len(expected),
        completed=len(delays),
        missing=missing,
        errors=errors,
        max_delay=max(delays, default=None),
        total_delay=sum(delays),
        headway_conflicts_diagnostic=run.get("headway_conflicts", []),
        acceptance="all measured departures in [3600,7200) complete; raw positive exit delay = 0; headways diagnostic",
    )
