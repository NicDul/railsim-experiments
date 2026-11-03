"""Standard-library audit of a portable capacity export, independent of solver construction."""

import argparse
import hashlib
import json
import math
from pathlib import Path


def audit(root):
    root = Path(root).resolve()
    manifest = json.loads((root / "manifest.json").read_text())
    for name, sha in manifest.items():
        path = (root / name).resolve()
        assert path.is_relative_to(root) and path.is_file(), name
        assert hashlib.sha256(path.read_bytes()).hexdigest() == sha, name
    report = json.loads((root / "report.json").read_text())
    profiles = json.loads((root / "profiles.json").read_text())
    period = report["identity"]["config"]["period_seconds"]
    ref = report["calibration"]
    passing = 0
    exclusions = 0
    for row in report["rates"]:
        for resource, claimed in (
            row.get("occupation", {}).get("resource_minimum_seconds", {}).items()
        ):
            options = {}
            for variant, info in ref["variants"].items():
                key = (
                    info["product"]
                    if report["identity"]["config"]["use_case"] == "UC1"
                    else variant
                )
                options.setdefault(key, []).append(variant)
            measured = sum(
                n
                * min(
                    profiles[v].get(resource, [0, 0, 0])[2]
                    - profiles[v].get(resource, [0, 0, 0])[1]
                    for v in options[p]
                )
                for p, n in row["composition"].items()
            )
            assert measured == claimed
            if resource in row["occupation"]["excluded_resources"]:
                assert measured > period
                exclusions += 1
        witness = row.get("witness")
        if witness:
            run = json.loads(
                (
                    root / "runs" / witness["simulation"]["run_id"] / "run.json"
                ).read_text()
            )
            assert run["status"] == "completed"
            trains = {r["vehicle_id"]: r for r in run["trains"]}
            assert len(trains) == len(run["trains"])
            for repeat in [2, 3]:
                for index, (_, time, variant) in enumerate(witness["pattern"]):
                    product = ref["variants"][variant]["product"]
                    train = trains[f"{product}_{index:02}_p{repeat}"]
                    expected = (
                        time + repeat * period + ref["baselines"][variant]["terminal"]
                    )
                    assert train["status"] == "terminal_arrived"
                    assert train["scheduled_departure"] == time + repeat * period
                    assert train["planned_arrival"] == expected
                    assert all(
                        type(train[k]) in [int, float] and math.isfinite(train[k])
                        for k in ["actual_arrival", "terminal_delay_seconds"]
                    )
                    assert (
                        train["actual_arrival"] <= expected
                        and train["terminal_delay_seconds"] <= 0
                    )
            passing += 1
    lower = max(
        (row["trains_per_hour"] for row in report["rates"] if row.get("witness")),
        default=None,
    )
    assert report["bounds"]["simulator_lower_bound_trains_per_hour"] == lower
    assert report["bounds"]["maximum"] is None
    return dict(
        status="PASS",
        manifest_files=len(manifest),
        passing_rates=passing,
        resource_exclusions=exclusions,
        caveat="Checks retained raw terminal times and occupancy arithmetic, not universal simulator/model equivalence or CP-SAT infeasibility proof.",
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("export", type=Path)
    print(json.dumps(audit(parser.parse_args().export), indent=2))
