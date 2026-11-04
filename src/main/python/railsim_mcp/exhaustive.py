"""Exhaustive UC1 entry-order solving with isolated, resumable process workers."""

import argparse
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
import fcntl
import hashlib
import json
import multiprocessing
import multiprocessing.util
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
from .batch import PersistentScenarioWorkspace


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
    """Prefer simulator-safe separation; only zero-gap infeasibility excludes."""
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
    for attempt, margin in enumerate((1, 0)):
        solve_seed = (seed + int(result["ordering_id"][:8], 16) + attempt) % 2147483647
        trial = solver(
            products, options, ref["windows"], seconds, frozenset(), 1,
            min_gap=margin, period=1800, seed=solve_seed,
            order=products, entry=entry,
        )
        result["trials"].append({"min_gap": margin, "solver": trial})
        if trial["status"] == "UNKNOWN":
            trial = solver(
                products, options, ref["windows"], max(seconds * 5, 15),
                frozenset(), 1, period=1800, seed=solve_seed,
                min_gap=margin, order=products, entry=entry,
            )
            result["trials"].append({"min_gap": margin, "solver": trial})
        result["solver_status"] = trial["status"]
        result["solver_model_gap"] = margin
        if margin == 1 and trial["status"] == "INFEASIBLE":
            result["preferred_gap_infeasible"] = True
            continue
        if trial["status"] == "INFEASIBLE" and margin == 0:
            result["status"] = "conditional_no"
            break
        if trial["status"] not in ("OPTIMAL", "FEASIBLE"):
            result["unresolved_reason"] = (
                "zero_gap_solver_" + trial["status"].lower()
                if margin == 0 else "one_second_solver_" + trial["status"].lower()
            )
            break
        pattern = trial["slot_pattern"]
        result["gap1_infeasible_zero_feasible"] = margin == 0
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
        result["unresolved_reason"] = "simulator_rejected_solver_witness"
        break
    return result


_WORKER = None


def _initialize_worker(root, ref):
    global _WORKER
    worker = multiprocessing.current_process().name.replace("-", "").lower()
    directory = Path(root) / "workers"
    if ref.get("generation"):
        directory /= ref["generation"]
    directory /= worker
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
    backend = PersistentScenarioWorkspace if ref.get("classpath") else ScenarioWorkspace
    backend_options = (
        {"classpath": str(project / "target/classes") + os.pathsep + ref["classpath"]}
        if ref.get("classpath") else {}
    )
    ws = backend(
        project / "src/main/resources/scenarios",
        directory / "state/workspaces",
        project_root=project,
        runs_root=directory / "state/runs",
        **backend_options,
    )
    if isinstance(ws, PersistentScenarioWorkspace):
        multiprocessing.util.Finalize(None, ws.close, exitpriority=10)
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
        execution_generation=ref.get("generation", "legacy"),
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
        row["preferred_gap_infeasible"] = row.get("preferred_gap_infeasible", 0) + bool(result.get("preferred_gap_infeasible"))
        row["gap1_infeasible_zero_feasible"] = row.get("gap1_infeasible_zero_feasible", 0) + bool(result.get("gap1_infeasible_zero_feasible"))
    rows = list(by_volume.values())
    for row in rows:
        if row["denominator"] != row["works"] + row["conditional_no"] + row["unresolved"] + row["pending"]:
            raise ValueError("unique result labels do not partition denominator")
        row["bounds"] = [row["works"] / row["denominator"], (row["works"] + row["unresolved"] + row["pending"]) / row["denominator"]]
    save(output / "summary.json", {"cases": rows})
    return rows


def validate_upgrade(output, project, report_path):
    legacy = json.loads((output / "identity.json").read_text())
    for relative, expected in legacy["calibration_identity"]["code"].items():
        if relative != "src/main/python/railsim_mcp/exhaustive.py" and digest(project / relative) != expected:
            raise ValueError(f"physics/solver/snapshot source changed: {relative}")
    proof = json.loads(report_path.read_text())
    cases = proof.get("cases", [])
    if (
        proof.get("legacy_identity_sha256") != digest(output / "identity.json")
        or len({case["ordering_id"] for case in cases}) < 10
        or not any(case["previously_repaired"] for case in cases)
        or not all(case["events_equal"] and case["score_equal"] and case["audit_equal"] for case in cases)
    ):
        raise ValueError("batch upgrade requires ten exact event/score/audit equivalence cases including repairs")
    for relative in (
        "src/main/python/railsim_mcp/batch.py",
        "src/main/java/org/matsim/project/RunBatchSimulation.java",
    ):
        if proof["adapter_sha256"].get(relative) != digest(project / relative):
            raise ValueError(f"batch equivalence adapter changed: {relative}")
    return {
        "legacy_identity_sha256": digest(output / "identity.json"),
        "equivalence_report": str(report_path),
        "equivalence_report_sha256": digest(report_path),
        "retained_record_sha256": {
            str(path.relative_to(output)): digest(path)
            for path in (output / "orders").glob("*/*.json")
        },
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--batch-classpath", type=Path, required=True)
    parser.add_argument("--equivalence-report", type=Path, required=True)
    args = parser.parse_args()
    if not 1 <= args.workers <= min(os.cpu_count() or 1, 8):
        parser.error("workers must be between1 and8 and no more than available CPUs")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    with (output / ".lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        stage = output / "stages/batch-v2"
        stage.mkdir(parents=True, exist_ok=True)
        migration_path = stage / "migration.json"
        if not migration_path.exists():
            save(migration_path, validate_upgrade(output, args.project.resolve(), args.equivalence_report.resolve()))
        migration = json.loads(migration_path.read_text())
        for relative, expected_hash in migration["retained_record_sha256"].items():
            if digest(output / relative) != expected_hash:
                raise ValueError(f"completed pre-upgrade record changed: {relative}")
        config = {
            "use_case": "UC1", "building_block": "BB2", "mode": "MAINLINE/FV_STOP",
            "period_seconds": 1800, "volumes": list(range(1, 14)),
            "workers": 1, "runner": "none",
            "limits": {"cpu_seconds": 100000, "wall_seconds": 100000, "simulations": 20000, "model_calls": 0},
            "calibration_from": str(output / "calibration/checkpoint.json"),
        }
        workflow = Workflow(Settings(args.project.resolve(), output / "calibration-state"), config, stage / "calibration", ["exhaustive-batch-v2"], minimum_volume=1)
        ref = workflow.calibrate()
        ref = dict(ref, project=str(args.project.resolve()), output=str(output),
                   workspace_path=str(workflow.ws.workspace_root / ref["workspace"]),
                   generation="batch-v2", classpath=args.batch_classpath.read_text().strip())
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
        identity = {"calibration_identity": workflow.identity, "workspace_hashes": ref["workspace_hashes"], "workers": args.workers, "runner_sha256": digest(__file__), "denominators": expected,
                    "classpath_sha256": digest(args.batch_classpath)}
        identity_path = stage / "identity.json"
        normalized = json.loads(json.dumps(identity))
        if identity_path.exists() and json.loads(identity_path.read_text()) != normalized:
            raise ValueError("exhaustive identity changed; use a different output")
        save(identity_path, identity)
        remaining = [(v, o) for v, o in tasks if not (output / "orders" / str(v) / (_ordering_id(o) + ".json")).exists()]
        started = time.monotonic()
        with ProcessPoolExecutor(max_workers=args.workers, mp_context=multiprocessing.get_context("spawn"), initializer=_initialize_worker, initargs=(str(output), ref)) as pool:
            for volume in range(1, 14):
                pending = [(v, o) for v, o in remaining if v == volume]
                queue = iter(pending)
                stopping = (output / "STOP").exists()
                futures = set()
                if not stopping:
                    for _ in range(args.workers):
                        task = next(queue, None)
                        if task is not None:
                            futures.add(pool.submit(_job, task))
                completed = 0
                while futures:
                    finished, _ = wait(futures, return_when=FIRST_COMPLETED)
                    for future in finished:
                        futures.remove(future)
                        future.result()
                        completed += 1
                        stopping = stopping or (output / "STOP").exists()
                        if not stopping:
                            task = next(queue, None)
                            if task is not None:
                                futures.add(pool.submit(_job, task))
                        if completed % 50 == 0:
                            print(json.dumps({"volume": volume, "completed": completed, "total": len(pending), "elapsed": time.monotonic() - started}), flush=True)
                rows = summarize(output, tasks)
                if stopping:
                    print(json.dumps({"stopped_at_ordering_boundary": True, "cases": rows}), flush=True)
                    return
                print(json.dumps({"completed_volume": volume, "cases": rows[:volume], "elapsed": time.monotonic() - started}), flush=True)
        summarize(output, tasks)


if __name__ == "__main__":
    main()
