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


def solve_ordering(ordering, ref, *, seconds=3, seed=4711, solver=solve):
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
        result["gap1_infeasible_zero_feasible"] = margin == 0
        result["status"] = "solver_feasible"
        result["slot_pattern"] = trial["slot_pattern"]
        break
    return result


def simulate_ordering(plan, ref, ws, *, seed=4711, simulator=simulate):
    result = dict(plan, simulations=[])
    if plan["status"] != "solver_feasible":
        return result
    pattern = plan["slot_pattern"]
    score, raw = simulator(
        ws, ref["workspace"], pattern,
        {variant: baseline["terminal"] for variant, baseline in ref["baselines"].items()},
        seed, 1800,
    )
    audit = audit_simulation(ws, score, raw, pattern, plan["ordering"], ref["variants"])
    result["simulations"].append({
        "min_gap": plan["solver_model_gap"], "pattern": pattern,
        "score": score, "audit": audit,
    })
    result["status"] = "works" if _simulation_passed(score) else "unresolved"
    if result["status"] == "unresolved":
        result["unresolved_reason"] = "simulator_rejected_solver_witness"
    return result


def evaluate_ordering(ordering, ref, ws, *, seconds=3, seed=4711, solver=solve, simulator=simulate):
    return simulate_ordering(
        solve_ordering(ordering, ref, seconds=seconds, seed=seed, solver=solver),
        ref, ws, seed=seed, simulator=simulator,
    )


def reuse_solver_result(result, final_path, identity_sha256):
    plan = dict(result, simulations=[], plan_identity_sha256=identity_sha256,
                reused_final_record=str(final_path), reused_final_sha256=digest(final_path))
    if result["solver_status"] in ("OPTIMAL", "FEASIBLE"):
        candidates = result["simulations"]
        if not candidates:
            raise ValueError("retained feasible result has no simulator witness")
        selected = next((candidate for candidate in candidates if _simulation_passed(candidate["score"])), candidates[0])
        plan.update(status="solver_feasible", slot_pattern=selected["pattern"],
                    solver_model_gap=selected["min_gap"])
    elif result["solver_status"] == "INFEASIBLE":
        plan["status"] = "conditional_no"
    else:
        plan["status"] = "unresolved"
    return plan


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


def _job(task, solver_only=False):
    directory, ws, ref = _WORKER
    volume, ordering = task
    cpu_start = resource.getrusage(resource.RUSAGE_CHILDREN)
    started = time.monotonic()
    cpu_self = time.process_time()
    if shutil.disk_usage(directory).free < 2 * 1024**3:
        raise RuntimeError("less than2 GiB free; stop safely rather than lose witness evidence")
    if solver_only:
        result = solve_ordering(ordering, ref)
        result["plan_identity_sha256"] = ref["plan_identity_sha256"]
    else:
        plan = json.loads((Path(ref["output"]) / "solver-results" / str(volume) / (_ordering_id(ordering) + ".json")).read_text())
        if plan["ordering"] != ordering or plan["plan_identity_sha256"] != ref["plan_identity_sha256"]:
            raise ValueError("saved solver witness identity mismatch")
        result = simulate_ordering(plan, ref, ws)
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    measured_cpu = (
        time.process_time() - cpu_self + usage.ru_utime + usage.ru_stime
        - cpu_start.ru_utime - cpu_start.ru_stime
    )
    result.update(
        volume=volume,
        worker=directory.name,
        execution_generation=ref.get("generation", "legacy"),
        wall_seconds=time.monotonic() - started,
        cpu_seconds=measured_cpu + (0 if solver_only else plan["cpu_seconds"]),
    )
    if not solver_only:
        result["solver_phase_cpu_seconds"] = plan["cpu_seconds"]
        result["simulation_phase_cpu_seconds"] = measured_cpu
    if solver_only:
        save(Path(ref["output"]) / "solver-results" / str(volume) / (result["ordering_id"] + ".json"), result)
    if not solver_only or result["status"] != "solver_feasible":
        save(Path(ref["output"]) / "orders" / str(volume) / (result["ordering_id"] + ".json"), result)
    # Retain immutable inputs, raw events, run.json, simulation.json and route audit.
    # The redundant dense train-state CSV is not used by the witness checker.
    for simulation in result["simulations"]:
        run_id = simulation["score"]["run_id"]
        redundant = ws.runs_root / run_id / "output/ITERS/it.0" / f"{run_id}.0.railsimTrainStates.csv.gz"
        if redundant.is_file():
            redundant.unlink()
    return result


def _solve_job(task):
    return _job(task, solver_only=True)


def run_tasks(pool, function, tasks, workers, output):
    queue = iter(tasks)
    futures = set()
    stopping = (output / "STOP").exists()
    if not stopping:
        for _ in range(workers):
            task = next(queue, None)
            if task is not None:
                futures.add(pool.submit(function, task))
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
                    futures.add(pool.submit(function, task))
            if completed % 100 == 0:
                print(json.dumps({"phase": function.__name__, "completed": completed, "total": len(tasks)}), flush=True)
    return stopping


def summarize_models(output, tasks):
    rows = {}
    for volume, ordering in tasks:
        row = rows.setdefault(volume, {
            "volume": volume, "denominator": 0, "solver_feasible": 0,
            "conditional_no": 0, "unresolved": 0, "pending": 0,
            "simulator_validated": 0, "distinct_simulated": 0,
        })
        row["denominator"] += 1
        key = _ordering_id(ordering)
        path = output / "solver-results" / str(volume) / (key + ".json")
        if path.exists():
            plan = json.loads(path.read_text())
            if plan["ordering"] != ordering or plan["ordering_id"] != key:
                raise ValueError("solver summary ordering identity mismatch")
            row[plan["status"]] += 1
        else:
            row["pending"] += 1
        final = output / "orders" / str(volume) / (key + ".json")
        if final.exists():
            record = json.loads(final.read_text())
            row["simulator_validated"] += record["status"] == "works"
            row["distinct_simulated"] += bool(record["simulations"])
    points = list(rows.values())
    for row in points:
        if row["denominator"] != row["solver_feasible"] + row["conditional_no"] + row["unresolved"] + row["pending"]:
            raise ValueError("solver classifications do not partition denominator")
        row["model_feasible_percent"] = 100 * row["solver_feasible"] / row["denominator"]
    report = {"provisional_model_only": True, "cases": points,
              "complete": not any(row["pending"] for row in points)}
    save(output / "solver-summary.json", report)
    lines = [
        "| UC1 / BB2 MAINLINE/FV_STOP volume (total trains/hour) | Orders | Model feasible | Conditional no | Solver unresolved | Pending | Model-feasible % (provisional) | Simulator validated |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in points:
        lines.append(
            f"| MAINLINE/FV_STOP @ {row['volume']} ({2 * row['volume']}/hour) | {row['denominator']} | "
            f"{row['solver_feasible']} | {row['conditional_no']} | {row['unresolved']} | "
            f"{row['pending']} | {row['model_feasible_percent']:.2f}% | {row['simulator_validated']} |"
        )
    lines += ["", "Provisional fixed-profile solver curve, NOT simulator-validated flexibility. "
              "One-second separation is preferred; only original zero-gap INFEASIBLE is conditional no. "
              "Every feasible saved schedule is queued for strict simulation unless that ordering already has "
              "an individually audited final simulation result. Model-feasible includes rejected zero-gap "
              "fallbacks; simulator passage is shown separately. No sampling or physical-infeasibility claim."]
    (output / "solver-curve.md").write_text("\n".join(lines) + "\n")
    return report


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
    parser.add_argument("--phase", choices=("all", "solve", "simulate"), default="all")
    args = parser.parse_args()
    if not 1 <= args.workers <= min(os.cpu_count() or 1, 8):
        parser.error("workers must be between1 and8 and no more than available CPUs")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    with (output / ".lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        stage = output / "stages/solver-first-v3"
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
        workflow = Workflow(Settings(args.project.resolve(), output / "calibration-state"), config, stage / "calibration", ["exhaustive-solver-first-v3"], minimum_volume=1)
        ref = workflow.calibrate()
        ref = dict(ref, project=str(args.project.resolve()), output=str(output),
                   workspace_path=str(workflow.ws.workspace_root / ref["workspace"]),
                   generation="solver-first-v3", classpath=args.batch_classpath.read_text().strip())
        tasks = []
        expected = {}
        for volume in range(1, 14):
            (output / "orders" / str(volume)).mkdir(parents=True, exist_ok=True)
            (output / "solver-results" / str(volume)).mkdir(parents=True, exist_ok=True)
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
        ref["plan_identity_sha256"] = digest(identity_path)
        for volume, ordering in tasks:
            key = _ordering_id(ordering)
            final = output / "orders" / str(volume) / (key + ".json")
            model = output / "solver-results" / str(volume) / (key + ".json")
            if model.exists():
                if json.loads(model.read_text())["plan_identity_sha256"] != ref["plan_identity_sha256"]:
                    raise ValueError("resumed solver plan identity mismatch")
            elif final.exists():
                save(model, reuse_solver_result(json.loads(final.read_text()), final, ref["plan_identity_sha256"]))
        started = time.monotonic()
        with ProcessPoolExecutor(max_workers=args.workers, mp_context=multiprocessing.get_context("spawn"), initializer=_initialize_worker, initargs=(str(output), ref)) as pool:
            if args.phase in ("all", "solve"):
                pending = [(v, o) for v, o in tasks if not (output / "solver-results" / str(v) / (_ordering_id(o) + ".json")).exists()]
                if run_tasks(pool, _solve_job, pending, args.workers, output):
                    summarize_models(output, tasks)
                    summarize(output, tasks)
                    return
            model_report = summarize_models(output, tasks)
            if not model_report["complete"]:
                raise ValueError("simulation phase requires every ordering's solver classification")
            print(json.dumps({"solver_pass_complete": True, "cases": model_report["cases"], "elapsed": time.monotonic() - started}), flush=True)
            if args.phase in ("all", "simulate"):
                pending = [(v, o) for v, o in tasks if not (output / "orders" / str(v) / (_ordering_id(o) + ".json")).exists()]
                run_tasks(pool, _job, pending, args.workers, output)
        summarize(output, tasks)
        summarize_models(output, tasks)


if __name__ == "__main__":
    main()
