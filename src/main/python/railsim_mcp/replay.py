"""Explicit witness replay, separate from proposal generation and search discovery."""

from collections import Counter
from .capacity import digest, save


def replay(workflow, path):
    import json

    ref = workflow.calibrate()
    witness = json.loads(path.read_text())
    config = workflow.config
    for key in ("building_block", "mode", "period_seconds"):
        if witness.get(key) != config[key]:
            raise ValueError("witness domain mismatch: " + key)
    pattern = witness["pattern"]
    if not isinstance(pattern, list) or not pattern:
        raise ValueError("empty witness")
    for row in pattern:
        if not isinstance(row, list) or len(row) != 3:
            raise ValueError("invalid witness row")
        product, departure, variant = row
        if (
            variant not in ref["variants"]
            or ref["variants"][variant]["product"] != product
        ):
            raise ValueError("invalid witness variant")
        if type(departure) is not int or not 0 <= departure < config["period_seconds"]:
            raise ValueError("invalid witness departure")
    from .uc1_modes import rounded_counts

    if Counter(row[0] for row in pattern) != Counter(
        rounded_counts(len(pattern), workflow.mode.shares)
    ):
        raise ValueError("witness composition does not match mode")
    result, _ = workflow.simulator(
        workflow.ws,
        ref["workspace"],
        sorted(pattern, key=lambda r: (r[1], r[0])),
        {v: b["terminal"] for v, b in ref["baselines"].items()},
        config["seed"],
        config["period_seconds"],
    )
    record = dict(
        witness_sha256=digest(path),
        source=witness.get("source"),
        pattern=pattern,
        simulation=result,
        evidence_label="explicit replay; not discovered by this search",
    )
    workflow.report.setdefault("replays", []).append(record)
    workflow.persist()
    save(workflow.out / "replay.json", record)
    return record
