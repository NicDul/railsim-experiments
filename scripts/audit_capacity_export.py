"""Standard-library audit of a portable capacity export, independent of solver construction."""

import argparse
from collections import Counter
from fractions import Fraction
import gzip
import hashlib
import json
import math
from pathlib import Path
import xml.etree.ElementTree as ET


def allocate(total, shares):
    targets = {key: total * Fraction(str(share)) for key, share in shares.items() if share > 0}
    counts = {key: int(target) for key, target in targets.items()}
    for key in sorted(targets, key=lambda key: (counts[key] - targets[key], key))[
        :total - sum(counts.values())
    ]:
        counts[key] += 1
    return counts


def composition(plan, mode, volume):
    mix, pattern = mode.split("/")
    counts = Counter()
    for product, number in allocate(volume, plan["mixes"][mix]["shares"]).items():
        for flow, amount in allocate(number, plan["patterns"][pattern]["shares"][product]).items():
            mapping = plan["flows"][flow]["routes"][product]
            counts[mapping["forward"]] += amount
            counts[mapping["reverse"]] += amount
    return +counts


def turnback_events(directory):
    """Independent raw dwell and detour check, without importing the workflow."""
    paths = list(directory.glob("*.events.xml.gz"))
    assert len(paths) == 1
    stops = {}
    with gzip.open(paths[0]) as stream:
        for _, element in ET.iterparse(stream, events=["end"]):
            if element.tag == "event":
                event = element.attrib
                assert event["type"] != "railsimDetourEvent"
                if event.get("facility") == "M_REVERSING" and event["type"] in (
                    "VehicleArrivesAtFacility", "VehicleDepartsAtFacility"
                ):
                    key = (event["vehicle"], event["type"])
                    assert key not in stops
                    stops[key] = float(event["time"])
            element.clear()
    return stops


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
    config = report["identity"]["config"]
    uc3_plan = None
    if config["use_case"] == "UC3":
        baseline = next(iter(ref["baselines"].values()))["run_id"]
        uc3_plan = json.loads(
            (root / "runs" / baseline / "inputs/uc3_operational_plan.json").read_text()
        )
    passing = 0
    exclusions = 0
    rows = report["rates"] + [
        dict(witness=r, trains_per_hour=3600 * len(r["pattern"]) / period)
        for r in report.get("replays", [])
        if r["simulation"]["passed"]
    ]
    for row in rows:
        if uc3_plan:
            if "volume" in row:
                assert Counter(row["composition"]) == composition(uc3_plan, config["mode"], row["volume"])
            assert row["trains_per_hour"] == (
                3600 * (sum(row["composition"].values()) if "composition" in row else len(row["witness"]["pattern"]))
                / period
            )
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
                    sum(end - start for _, start, end in visits(profiles[v].get(resource, [0, 0, 0])))
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
            if uc3_plan:
                actual = Counter(variant for _, _, variant in witness["pattern"])
                assert any(
                    actual == composition(uc3_plan, config["mode"], volume)
                    for volume in ([row["volume"]] if "volume" in row else config["volumes"])
                )
                stops = turnback_events(root / "runs" / witness["simulation"]["run_id"])
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
                    assert train["route_id"] == variant
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
                    if uc3_plan and variant == "RV_LML":
                        vehicle = f"{product}_{index:02}_p{repeat}"
                        assert (
                            stops[vehicle, "VehicleDepartsAtFacility"]
                            - stops[vehicle, "VehicleArrivesAtFacility"] >= 480
                        )
            passing += 1
    lower = max(
        (row["trains_per_hour"] for row in rows if row.get("witness")),
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


def visits(value):
    return [value] if isinstance(value[0], (int, float)) else value


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("export", type=Path)
    print(json.dumps(audit(parser.parse_args().export), indent=2))
