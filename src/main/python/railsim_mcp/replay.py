"""Explicit witness replay, separate from proposal generation and search discovery."""

from collections import Counter
from .capacity import digest, save


def replay(workflow, path):
    import json

    ref = workflow.calibrate()
    witness = json.loads(path.read_text())
    config = workflow.config
    if witness.get("use_case", config["use_case"]) != config["use_case"]:
        raise ValueError("witness domain mismatch: use_case")
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
            or product not in (ref["variants"][variant]["product"], variant)
        ):
            raise ValueError("invalid witness variant")
        if type(departure) is not int or not 0 <= departure < config["period_seconds"]:
            raise ValueError("invalid witness departure")
    actual = Counter(
        ref["variants"][variant]["product"] if workflow.use_case == "UC1" else variant
        for _, _, variant in pattern
    )
    volumes = [len(pattern)] if workflow.use_case == "UC1" else config["volumes"]
    if not any(
        actual == Counter(workflow.mode.composition(volume))
        for volume in volumes
    ):
        raise ValueError("witness composition does not match mode")
    pattern = sorted(pattern, key=lambda r: (r[1], r[0]))
    workflow.ws.timeout_seconds = workflow.budget.allowance("simulation", 300)
    workflow.persist()
    result, _ = workflow.simulator(
        workflow.ws,
        ref["workspace"],
        [
            [ref["variants"][v]["product"], t, v]
            for _, t, v in pattern
        ],
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
    from .capacity import assemble

    workflow.report["bounds"] = assemble(workflow.report)
    save(workflow.out / "bounds-report.json", workflow.report)
    workflow.export()
    return record
