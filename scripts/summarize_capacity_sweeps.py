"""Summarise a bounded sweep domain without promoting failures to upper bounds."""

import argparse
import json
from pathlib import Path


def summary(report):
    config = report["identity"]["config"]
    rates = report["rates"]
    passing = [r for r in rates if r.get("witness")]
    best = max(passing, key=lambda r: r["trains_per_hour"], default=None)
    excluded = set()
    for row in rates:
        if row.get("occupation", {}).get("excluded_resources") or any(
            t["scope"] == "all_orders"
            and t.get("min_gap", 0) == 0
            and t.get("solver", {}).get("status") == "INFEASIBLE"
            for t in row["trials"]
        ):
            excluded.add(row["volume"])
    return dict(
        use_case=config["use_case"],
        building_block=config["building_block"],
        mode=config["mode"],
        tested_revision=report["source_revision"],
        requested_volumes=config["volumes"],
        period_seconds=config["period_seconds"],
        volume_definition="per direction" if config["use_case"] == "UC2" else "total",
        discovered_lower_bound_trains_per_hour=best["trains_per_hour"]
        if best
        else None,
        discovered_best_volume=best["volume"] if best else None,
        new_discoveries=[
            dict(
                volume=r["volume"],
                rate=r["trains_per_hour"],
                run_id=r["witness"]["simulation"]["run_id"],
            )
            for r in passing
        ],
        fixed_surrogate_exclusions=sorted(excluded),
        undecided_in_surrogate_domain=[
            r["volume"]
            for r in rates
            if not r.get("witness") and r["volume"] not in excluded
        ],
        no_simulator_witness=[r["volume"] for r in rates if not r.get("witness")],
        next_exclusion_above_best=min(
            (v for v in excluded if best and v > best["volume"]), default=None
        ),
        budget=report.get("budget"),
        limits=config.get("limits"),
        model_calls=report["model_calls"],
        model_rounds=[
            dict(volume=r["volume"], **record) for r in rates for record in r["rounds"]
        ],
        incomplete_domain=sorted(set(config["volumes"]) - {r["volume"] for r in rates}),
        maximum=None,
        interpretation="Finite-domain discoveries versus conditional fixed-profile surrogate exclusions; no unrestricted simulator upper bound or global maximum.",
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = {}
    for path in sorted(args.root.glob("uc*/checkpoint.json")):
        report = json.loads(path.read_text())
        result[path.parent.name] = summary(report)
    data = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.write_text(data)
    else:
        print(data)
