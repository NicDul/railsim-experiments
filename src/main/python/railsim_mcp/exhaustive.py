"""Exhaustive UC1 entry-order solving with isolated, resumable process workers."""

import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
import fcntl
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import resource
import shutil
import time
import xml.etree.ElementTree as ET

from .capacity import Workflow, save
from .configuration import Settings
from .flexibility import (
    _ordering_id,
    _simulation_passed,
    enumerate_orderings,
    order_groups,
    ordering_universe,
)
from .simulation import simulate
from .solver import solve
from .uc1 import events
from .workspace import ScenarioWorkspace


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def cyclic_entry_matches(sequence, vehicles, entries, products, period):
    actual = [
        products[vehicle]
        for vehicle in sorted(vehicles, key=lambda vehicle: entries[vehicle] % period)
    ]
    return any(actual == sequence[i:] + sequence[:i] for i in range(len(sequence)))


def audit_simulation(ws, score, raw, pattern, ordering, variants, period=1800):
    root = ws.runs_root / score["run_id"]
    for filename, expected in raw["input_file_sha256"].items():
        if digest(root / "inputs" / filename) != expected:
            raise ValueError(f"frozen input hash changed: {filename}")
    network = ET.parse(root / "inputs/network.xml")
    markers = [
        attr.get("name")
        for attr in network.iter("attribute")
        if attr.get("name") in ("railsimEntry", "railsimExit")
        and attr.text == "true"
    ]
    if markers:
        raise ValueError("UC1 candidate still enables entry/exit rerouting")
    schedule = ET.parse(root / "inputs/schedule.xml")
    routes = {
        route.get("id"): [link.get("refId") for link in route.findall("./route/link")]
        for route in schedule.iter("transitRoute")
    }
    expected_ids = {
        f"{product}_{i:02}_p{k}": (product, departure, variant, k)
        for i, (product, departure, variant) in enumerate(pattern)
        for k in (2, 3)
    }
    observed = {}
    entries = {}
    detours = 0
    entry_resource = next(iter(ordering))
    entry_links = {
        link.get("id")
        for link in network.iter("link")
        if any(
            attr.get("name") == "railsimResourceId" and attr.text == entry_resource
            for attr in link.iter("attribute")
        )
    }
    for event in events(raw["events_file"]):
        if event["type"] == "railsimDetourEvent":
            detours += 1
        vehicle = event.get("vehicle")
        if vehicle not in expected_ids:
            continue
        if event["type"] == "entered link":
            observed.setdefault(vehicle, []).append(event["link"])
        if (
            event["type"] == "railsimLinkStateChangeEvent"
            and event.get("link") in entry_links
            and event.get("state") == "EXHAUSTED"
            and event.get("dla") != "true"
        ):
            entries.setdefault(vehicle, float(event["time"]))
    rows = {row["vehicle_id"]: row for row in raw["trains"]}
    if len(rows) != len(raw["trains"]):
        raise ValueError("duplicate simulator train IDs")
    route_errors = []
    for vehicle, (_, departure, variant, k) in expected_ids.items():
        row = rows.get(vehicle)
        if row is None or row.get("route_id") != variant:
            route_errors.append(vehicle)
            continue
        if row.get("scheduled_departure") != departure + k * period:
            raise ValueError("candidate train departure/score-period mismatch")
        expected_route = variants[variant]["links"]
        if [link for link in routes[variant] if not link.startswith("loop")] != expected_route:
            raise ValueError("frozen schedule differs from selected solver route")
        if [link for link in observed.get(vehicle, []) if not link.startswith("loop")] != expected_route:
            route_errors.append(vehicle)
    entry_errors = []
    sequence = next(iter(ordering.values()))
    for k in (2, 3):
        selected = [
            vehicle for vehicle in expected_ids if expected_ids[vehicle][3] == k
        ]
        if any(vehicle not in entries for vehicle in selected):
            entry_errors.append(k)
            continue
        if not cyclic_entry_matches(
            sequence, selected, entries,
            {vehicle: expected_ids[vehicle][0] for vehicle in selected}, period,
        ):
            entry_errors.append(k)
    if _simulation_passed(score) and (detours or route_errors or entry_errors):
        raise ValueError(
            "passing simulator witness changed route or cyclic entry order: "
            + json.dumps({"run_id": score["run_id"], "detours": detours, "route_errors": route_errors, "entry_errors": entry_errors, "entries": entries})
        )
    return {
        "run_id": score["run_id"],
        "inputs_hash_verified": True,
        "rerouting_markers": markers,
        "detour_event_count": detours,
        "scored_train_count": len(expected_ids),
        "route_errors": route_errors,
        "entry_order_errors": entry_errors,
        "observed_entry_times": entries,
        "event_sha256": digest(raw["events_file"]),
        "event_file": raw["events_file"],
    }


def evaluate_ordering(ordering, ref, ws, *, seconds=3, seed=4711, solver=solve, simulator=simulate):
    """Keep zero-gap negatives distinct from stricter candidate-repair failures."""
    entry = next(iter(ordering))
    products = ordering[entry]
    options = {
        product: sorted(
            variant
            for variant, info in ref["variants"].items()
            if info["product"] == product
        )
        for product in set(products)
    }
    result = {
        "ordering_id": _ordering_id(ordering),
        "ordering": ordering,
        "status": "unresolved",
        "trials": [],
        "simulations": [],
    }
    forbidden = []
    # A stricter repair is a candidate search, never an infeasibility certificate.
    for attempt, margin in enumerate((0, 1, 2)):
        solve_seed = (seed + int(result["ordering_id"][:8], 16) + attempt) % 2147483647
        trial = solver(
            products, options, ref["windows"], seconds, frozenset(), 1,
            min_gap=margin, period=1800, seed=solve_seed,
            order=products, entry=entry, forbidden_patterns=forbidden,
        )
        result["trials"].append({"min_gap": margin, "solver": trial})
        if margin == 0:
            result["solver_status"] = trial["status"]
        if trial["status"] == "UNKNOWN" and margin == 0:
            trial = solver(
                products, options, ref["windows"], max(seconds * 5, 15),
                frozenset(), 1, period=1800, seed=solve_seed,
                order=products, entry=entry,
            )
            result["trials"].append({"min_gap": 0, "solver": trial})
            result["solver_status"] = trial["status"]
        if trial["status"] == "INFEASIBLE" and margin == 0:
            result["status"] = "conditional_no"
            break
        if trial["status"] not in ("OPTIMAL", "FEASIBLE"):
            result["unresolved_reason"] = (
                "zero_gap_solver_" + trial["status"].lower()
                if margin == 0 else "repair_solver_" + trial["status"].lower()
            )
            break
        pattern = trial["slot_pattern"]
        score, raw = simulator(
            ws, ref["workspace"], pattern,
            {variant: baseline["terminal"] for variant, baseline in ref["baselines"].items()},
            seed, 1800,
        )
        audit = audit_simulation(ws, score, raw, pattern, ordering, ref["variants"])
        result["simulations"].append(
            {"min_gap": margin, "pattern": pattern, "score": score, "audit": audit}
        )
        if _simulation_passed(score):
            result["status"] = "works"
            result.pop("unresolved_reason", None)
            break
        forbidden.append(pattern)
        result["unresolved_reason"] = "simulator_rejected_all_bounded_candidates"
    return result


_WORKER = None


def _initialize_worker(root, ref):
    global _WORKER
    worker = multiprocessing.current_process().name.replace("-", "").lower()
    directory = Path(root) / "workers" / worker
    directory.mkdir(parents=True, exist_ok=True)
    project = directory / "project"
    source = Path(ref["project"])
    if not project.exists():
        project.mkdir()
        for filename in ("pom.xml", "mvnw", ".git"):
            if (source / filename).is_file():
                shutil.copy2(source / filename, project / filename)
        for dirname in (".mvn", "src", "target"):
            shutil.copytree(source / dirname, project / dirname)
    ws = ScenarioWorkspace(
        project / "src/main/resources/scenarios",
        directory / "state/workspaces",
        project_root=project,
        runs_root=directory / "state/runs",
    )
    ws.timeout_seconds = 120
    workspace = ws.workspace_root / ref["workspace"]
    if not workspace.exists():
        workspace.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(Path(ref["workspace_path"]), workspace)
    if {
        path.name: digest(path)
        for path in workspace.iterdir()
        if path.suffix in (".xml", ".json")
    } != ref["workspace_hashes"]:
        raise ValueError("worker calibrated workspace hash mismatch")
    _WORKER = (directory, ws, ref)


def _job(task):
    directory, ws, ref = _WORKER
    volume, ordering = task
    cpu_start = resource.getrusage(resource.RUSAGE_CHILDREN)
    started = time.monotonic()
    cpu_self = time.process_time()
    if shutil.disk_usage(directory).free < 2 * 1024**3:
        raise RuntimeError("less than2 GiB free; stop safely rather than lose witness evidence")
    result = evaluate_ordering(ordering, ref, ws)
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    result.update(
        volume=volume,
        worker=directory.name,
        wall_seconds=time.monotonic() - started,
        cpu_seconds=time.process_time() - cpu_self
        + usage.ru_utime + usage.ru_stime - cpu_start.ru_utime - cpu_start.ru_stime,
    )
    save(Path(ref["output"]) / "orders" / str(volume) / (result["ordering_id"] + ".json"), result)
    # Retain immutable inputs, raw events, run.json, simulation.json and route audit.
    # The redundant dense train-state CSV is not used by the witness checker.
    for simulation in result["simulations"]:
        run_id = simulation["score"]["run_id"]
        redundant = ws.runs_root / run_id / "output/ITERS/it.0" / f"{run_id}.0.railsimTrainStates.csv.gz"
        if redundant.is_file():
            redundant.unlink()
    return result


def summarize(output, tasks):
    by_volume = {}
    for volume, ordering in tasks:
        key = _ordering_id(ordering)
        row = by_volume.setdefault(volume, {"volume": volume, "denominator": 0, "works": 0, "conditional_no": 0, "unresolved": 0, "pending": 0, "solver_feasible": 0, "solver_unknown": 0, "simulated": 0, "simulator_calls": 0, "rejected_candidates": 0})
        row["denominator"] += 1
        path = output / "orders" / str(volume) / (key + ".json")
        if not path.exists():
            row["pending"] += 1
            continue
        result = json.loads(path.read_text())
        if result["ordering_id"] != key or result["ordering"] != ordering:
            raise ValueError("resumed result ordering identity mismatch")
        row[result["status"]] += 1
        row["solver_feasible"] += result["solver_status"] in ("OPTIMAL", "FEASIBLE")
        row["solver_unknown"] += result["solver_status"] not in ("OPTIMAL", "FEASIBLE", "INFEASIBLE")
        row["simulated"] += bool(result["simulations"])
        row["simulator_calls"] += len(result["simulations"])
        row["rejected_candidates"] += sum(not _simulation_passed(s["score"]) for s in result["simulations"])
    rows = list(by_volume.values())
    for row in rows:
        if row["denominator"] != row["works"] + row["conditional_no"] + row["unresolved"] + row["pending"]:
            raise ValueError("unique result labels do not partition denominator")
        row["bounds"] = [row["works"] / row["denominator"], (row["works"] + row["unresolved"] + row["pending"]) / row["denominator"]]
    save(output / "summary.json", {"cases": rows})
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()
    if not 1 <= args.workers <= min(os.cpu_count() or 1, 8):
        parser.error("workers must be between1 and8 and no more than available CPUs")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    with (output / ".lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        config = {
            "use_case": "UC1", "building_block": "BB2", "mode": "MAINLINE/FV_STOP",
            "period_seconds": 1800, "volumes": list(range(1, 14)),
            "workers": 1, "runner": "none",
            "limits": {"cpu_seconds": 100000, "wall_seconds": 100000, "simulations": 20000, "model_calls": 0},
        }
        workflow = Workflow(Settings(args.project.resolve(), output / "calibration-state"), config, output / "calibration", ["exhaustive"], minimum_volume=1)
        ref = workflow.calibrate()
        ref = dict(ref, project=str(args.project.resolve()), output=str(output), workspace_path=str(workflow.ws.workspace_root / ref["workspace"]))
        tasks = []
        expected = {}
        for volume in range(1, 14):
            (output / "orders" / str(volume)).mkdir(parents=True, exist_ok=True)
            groups = order_groups("UC1", None, {p: n for p, n in workflow.mode.composition(volume).items() if n}, ref["windows"])
            orders = list(enumerate_orderings(groups))
            if len(orders) != ordering_universe(groups):
                raise ValueError("enumeration disagrees with exact denominator")
            tasks.extend((volume, order) for order in orders)
            expected[volume] = len(orders)
        if sum(expected.values()) != 8131:
            raise ValueError("unexpected exhaustive BB2 denominator")
        identity = {"calibration_identity": workflow.identity, "workspace_hashes": ref["workspace_hashes"], "workers": args.workers, "runner_sha256": digest(__file__), "denominators": expected}
        identity_path = output / "identity.json"
        normalized = json.loads(json.dumps(identity))
        if identity_path.exists() and json.loads(identity_path.read_text()) != normalized:
            raise ValueError("exhaustive identity changed; use a different output")
        save(identity_path, identity)
        remaining = [(v, o) for v, o in tasks if not (output / "orders" / str(v) / (_ordering_id(o) + ".json")).exists()]
        started = time.monotonic()
        with ProcessPoolExecutor(max_workers=args.workers, mp_context=multiprocessing.get_context("spawn"), initializer=_initialize_worker, initargs=(str(output), ref)) as pool:
            for volume in range(1, 14):
                pending = [(v, o) for v, o in remaining if v == volume]
                futures = [pool.submit(_job, task) for task in pending]
                completed = 0
                for future in as_completed(futures):
                    future.result()
                    completed += 1
                    if completed % 50 == 0:
                        print(json.dumps({"volume": volume, "completed": completed, "total": len(pending), "elapsed": time.monotonic() - started}), flush=True)
                rows = summarize(output, tasks)
                print(json.dumps({"completed_volume": volume, "cases": rows[:volume], "elapsed": time.monotonic() - started}), flush=True)
        summarize(output, tasks)


if __name__ == "__main__":
    main()
