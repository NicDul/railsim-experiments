"""Exhaustive UC1 model-only ordering curves using verified existing calibration."""

import argparse
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
import fcntl
import json
import multiprocessing
import os
from pathlib import Path
import shutil
import time
import xml.etree.ElementTree as ET

from .capacity import save
from .exhaustive import digest, solve_ordering
from .flexibility import _ordering_id, enumerate_orderings, necklace_count, order_groups
from .profiles import reference_windows
from .uc1_modes import UC1Mode
from .uc1 import events


def import_calibration(project, source, output):
    report = json.loads(source.read_text())
    identity = report["identity"]
    config = identity["config"]
    if (config["use_case"], config["building_block"], config["mode"], config["period_seconds"], config["seed"]) != (
        "UC1", "BB3", "MAINLINE/FV_STOP", 1800, 4711,
    ):
        raise ValueError("calibration domain mismatch")
    for relative, expected in identity["inputs"].items():
        if digest(project / relative) != expected:
            raise ValueError(f"calibration scenario changed: {relative}")
    protected = {
        relative: expected for relative, expected in identity["code"].items()
        if relative.startswith("src/main/java/")
        or relative in ("pom.xml", "src/main/python/railsim_mcp/simulation.py",
                        "src/main/python/railsim_mcp/workspace.py",
                        "src/main/python/railsim_mcp/profiles.py",
                        "src/main/python/railsim_mcp/timetable.py",
                        "src/main/python/railsim_mcp/uc1.py")
    }
    if not protected:
        raise ValueError("calibration lacks simulator physics identity")
    for relative, expected in protected.items():
        if digest(project / relative) != expected:
            raise ValueError(f"calibration simulator physics changed: {relative}")
    for key in ("RAILSIM_DEADLOCK_AVOIDANCE", "RAILSIM_SIM_JVM_OPTIONS"):
        if os.environ.get(key) != identity["runtime"].get(key):
            raise ValueError(f"calibration simulation setting changed: {key}")
    ref = report["calibration"]
    state = Path(identity["state"])
    workspace = state / "workspaces" / ref["workspace"]
    hashes = {p.name: digest(p) for p in workspace.iterdir() if p.suffix in (".xml", ".json")}
    if hashes != ref["workspace_hashes"]:
        raise ValueError("calibrated workspace changed")
    if any(a.get("name") in ("railsimEntry", "railsimExit")
           for a in ET.parse(workspace / "network.xml").iter("attribute")):
        raise ValueError("calibrated BB3 workspace enables rerouting")
    for relative, expected in ref["evidence_hashes"].items():
        if digest(state / "runs" / relative) != expected:
            raise ValueError(f"calibration evidence changed: {relative}")
    windows = reference_windows(state / "runs", ref["baselines"])
    if json.loads(json.dumps(windows)) != ref["windows"]:
        raise ValueError("reconstructed calibration resource windows differ")
    for baseline in ref["baselines"].values():
        raw = json.loads((state / "runs" / baseline["run_id"] / "run.json").read_text())
        if raw["trains_arrived"] != raw["trains_expected"] or any(
            event["type"] == "railsimDetourEvent" for event in events(raw["events_file"])
        ):
            raise ValueError("isolated calibration has missing trains or rerouting events")
    target = output / "calibration"
    target.mkdir(exist_ok=True)
    if not (target / "workspace").exists():
        shutil.copytree(workspace, target / "workspace")
    for relative in ref["evidence_hashes"]:
        destination = target / "runs" / relative
        if not destination.exists():
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(state / "runs" / relative, destination)
        if digest(destination) != ref["evidence_hashes"][relative]:
            raise ValueError(f"copied calibration evidence changed: {relative}")
    if {p.name: digest(p) for p in (target / "workspace").iterdir()
        if p.suffix in (".xml", ".json")} != ref["workspace_hashes"]:
        raise ValueError("copied calibrated workspace changed")
    save(target / "reference.json", ref)
    save(target / "origin.json", {
        "source_checkpoint": str(source), "source_sha256": digest(source),
        "source_state": str(state), "input_hashes": identity["inputs"],
        "protected_simulator_code_hashes": protected,
        "runtime": identity["runtime"], "new_simulator_calls": 0,
        "reuse": "isolated profiles only; no capacity or ordering results reused",
    })
    return ref


_REFERENCE = None
_OUTPUT = None
_IDENTITY = None


def initialize(reference, output, identity):
    global _REFERENCE, _OUTPUT, _IDENTITY
    _REFERENCE, _OUTPUT, _IDENTITY = reference, Path(output), identity


def job(task):
    volume, ordering = task
    started, cpu = time.monotonic(), time.process_time()
    result = solve_ordering(ordering, _REFERENCE)
    result.update(volume=volume, identity_sha256=_IDENTITY,
                  wall_seconds=time.monotonic() - started,
                  cpu_seconds=time.process_time() - cpu,
                  worker_pid=os.getpid(), simulator_calls=0)
    save(_OUTPUT / "solver-results" / str(volume) / (result["ordering_id"] + ".json"), result)
    return result


def execute(pool, tasks, workers, output):
    queue = iter(tasks)
    active = set()
    stopping = (output / "STOP").exists()
    if not stopping:
        for _ in range(workers):
            task = next(queue, None)
            if task is not None:
                active.add(pool.submit(job, task))
    completed = 0
    while active:
        done, _ = wait(active, return_when=FIRST_COMPLETED)
        for future in done:
            active.remove(future)
            future.result()
            completed += 1
            stopping = stopping or (output / "STOP").exists()
            if not stopping:
                task = next(queue, None)
                if task is not None:
                    active.add(pool.submit(job, task))
            if completed % 1000 == 0:
                print(json.dumps({"completed_this_phase": completed, "phase_tasks": len(tasks)}), flush=True)
    return stopping


def summarize(output, tasks, identity_sha):
    rows = {}
    seen = set()
    for volume, ordering in tasks:
        key = _ordering_id(ordering)
        if key in seen:
            raise ValueError("duplicate ordering ID")
        seen.add(key)
        row = rows.setdefault(volume, {
            "volume": volume, "total_trains_per_hour": 2 * volume,
            "denominator": 0, "solver_feasible": 0, "conditional_no": 0,
            "unresolved": 0, "pending": 0, "gap1_infeasible_zero_feasible": 0,
            "cpu_seconds": 0, "wall_seconds_sum": 0,
        })
        row["denominator"] += 1
        path = output / "solver-results" / str(volume) / (key + ".json")
        if not path.exists():
            row["pending"] += 1
            continue
        result = json.loads(path.read_text())
        if result["ordering"] != ordering or result["ordering_id"] != key or result["identity_sha256"] != identity_sha:
            raise ValueError(f"resumed ordering identity mismatch: {path}")
        if result["simulations"] or result["simulator_calls"]:
            raise ValueError("solver-only results contain simulation")
        if result["status"] == "solver_feasible" and not result.get("slot_pattern"):
            raise ValueError("feasible ordering lacks saved witness")
        row[result["status"]] += 1
        row["gap1_infeasible_zero_feasible"] += bool(result.get("gap1_infeasible_zero_feasible"))
        row["cpu_seconds"] += result["cpu_seconds"]
        row["wall_seconds_sum"] += result["wall_seconds"]
    for row in rows.values():
        if row["denominator"] != sum(row[k] for k in ("solver_feasible", "conditional_no", "unresolved", "pending")):
            raise ValueError("labels do not partition denominator")
        row["model_feasible_percent"] = 100 * row["solver_feasible"] / row["denominator"]
    report = {"model_only": True, "simulator_calls": 0,
              "complete": not any(r["pending"] for r in rows.values()), "cases": list(rows.values())}
    save(output / "solver-summary.json", report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--calibration-from", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--pilot-count", type=int, default=256)
    parser.add_argument("--continue-under-hours", type=float, default=6)
    args = parser.parse_args()
    if not 1 <= args.workers <= min(os.cpu_count() or 1, 8) or args.pilot_count < 1:
        parser.error("use1..8 workers within available CPU count and positive pilot count")
    output, project = args.output.resolve(), args.project.resolve()
    output.mkdir(parents=True, exist_ok=True)
    with (output / ".lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        reference = import_calibration(project, args.calibration_from.resolve(), output)
        mode = UC1Mode.load(project / "src/main/resources/scenarios/use_case_1/uc1_operational_plan.json", "MAINLINE/FV_STOP")
        tasks, denominators = [], {}
        for volume in range(1, 17):
            (output / "solver-results" / str(volume)).mkdir(parents=True, exist_ok=True)
            counts = {p: n for p, n in mode.composition(volume).items() if n}
            groups = order_groups("UC1", None, counts, reference["windows"])
            orders = list(enumerate_orderings(groups))
            if len(orders) != necklace_count(counts):
                raise ValueError("enumeration disagrees with Burnside count")
            tasks.extend((volume, order) for order in orders)
            denominators[volume] = len(orders)
        if len(tasks) != 108235:
            raise ValueError("unexpected BB3 denominator")
        identity = {
            "domain": {"use_case": "UC1", "building_block": "BB3", "mode": "MAINLINE/FV_STOP",
                       "period_seconds": 1800, "volumes": list(range(1, 17)), "denominators": denominators},
            "calibration_checkpoint_sha256": digest(args.calibration_from),
            "workspace_hashes": reference["workspace_hashes"],
            "source_hashes": {
                str(path.relative_to(project)): digest(path)
                for path in (project / "src/main/python").rglob("*.py")
            },
            "policy": "margin1 first; zero-gap only after margin1 INFEASIBLE; UNKNOWN retry3s->15s; no simulations",
            "seed": 4711, "workers": args.workers,
        }
        identity = json.loads(json.dumps(identity))
        identity_path = output / "identity.json"
        if identity_path.exists() and json.loads(identity_path.read_text()) != identity:
            raise ValueError("resume domain/calibration/source identity changed")
        save(identity_path, identity)
        identity_sha = digest(identity_path)
        top = [(v, o) for v, o in tasks if v == 16]
        count = min(args.pilot_count, len(top))
        pilot = [top[i * len(top) // count] for i in range(count)]
        pilot_path = output / "pilot.json"
        with ProcessPoolExecutor(max_workers=args.workers, mp_context=multiprocessing.get_context("spawn"),
                                 initializer=initialize, initargs=(reference, str(output), identity_sha)) as pool:
            if not pilot_path.exists():
                started = time.monotonic()
                pending_pilot = [(v, o) for v, o in pilot if not (
                    output / "solver-results" / str(v) / (_ordering_id(o) + ".json")
                ).exists()]
                stopping = execute(pool, pending_pilot, args.workers, output)
                elapsed = time.monotonic() - started
                if stopping:
                    summarize(output, tasks, identity_sha)
                    return
                results = [json.loads((output / "solver-results/16" / (_ordering_id(o) + ".json")).read_text()) for _, o in pilot]
                mean_order = sum(r["wall_seconds"] for r in results) / count
                projected = max(mean_order / args.workers, elapsed / max(1, len(pending_pilot))) * len(tasks)
                save(pilot_path, {
                    "count": count, "volume": 16, "ordering_ids": [_ordering_id(o) for _, o in pilot],
                    "wall_seconds": elapsed, "aggregate_cpu_seconds": sum(r["cpu_seconds"] for r in results),
                    "orders_per_wall_second": len(pending_pilot) / elapsed,
                    "new_pilot_orders": len(pending_pilot),
                    "mean_order_wall_seconds": mean_order,
                    "projected_total_wall_seconds": projected,
                    "continue_under_hours": args.continue_under_hours,
                    "continue": projected < args.continue_under_hours * 3600,
                    "note": "Timing-only deterministic spread pilot; not a flexibility sample or inference.",
                    "labels": {label: sum(r["status"] == label for r in results)
                               for label in ("solver_feasible", "conditional_no", "unresolved")},
                })
            pilot_report = json.loads(pilot_path.read_text())
            print(json.dumps({"pilot_complete": pilot_report}), flush=True)
            if not pilot_report["continue"]:
                summarize(output, tasks, identity_sha)
                return
            for volume in range(1, 17):
                pending = [(v, o) for v, o in tasks if v == volume and not (
                    output / "solver-results" / str(v) / (_ordering_id(o) + ".json")
                ).exists()]
                stopping = execute(pool, pending, args.workers, output)
                report = summarize(output, tasks, identity_sha)
                print(json.dumps({"completed_volume": volume, "case": report["cases"][volume - 1]}), flush=True)
                if stopping:
                    return
        summarize(output, tasks, identity_sha)


if __name__ == "__main__":
    main()
