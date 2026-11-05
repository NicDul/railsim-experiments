"""Reproducible, sequential diagnostics over completed exhaustive solver records."""

import argparse
from collections import Counter
from datetime import datetime, timezone
import fcntl
import json
import math
import os
from pathlib import Path
import platform
import random
import resource
import shutil
import sys
import time
import xml.etree.ElementTree as ET

from .batch import PersistentScenarioWorkspace
from .capacity import save
from .exhaustive import audit_simulation, cyclic_entry_matches, digest
from .flexibility import _ordering_id, _simulation_passed
from .simulation import simulate
from .solver import solve
from .uc1 import events


def utc():
    return datetime.now(timezone.utc).isoformat()


def read(path):
    return json.loads(Path(path).read_text())


def select(population, count, seed):
    """Sorting before sampling makes traversal order irrelevant."""
    keys = sorted(population)
    if count < 0 or count > len(keys):
        raise ValueError(f"sample count {count} exceeds population {len(keys)}")
    return random.Random(seed).sample(keys, count)


def validate_limits(seconds, simulations):
    if not math.isfinite(seconds) or not 0 < seconds <= 600:
        raise ValueError("solver allowance must be in (0, 600] seconds per ordering")
    if type(simulations) is not int or not 0 <= simulations <= 20:
        raise ValueError("simulation limit must be an integer in [0, 20]")


def prepare(source, output, *, origin, volumes=(13, 14), count=5, seed=20261105,
            seconds=600, simulations=10):
    validate_limits(seconds, simulations)
    source, output = Path(source).resolve(), Path(output).resolve()
    if output == source or source in output.parents or output in source.parents:
        raise ValueError("source and output must be disjoint")
    if not 1 <= count <= 10:
        raise ValueError("sample count must be between 1 and 10")
    settings = dict(origin=origin, volumes=list(volumes), count=count, seed=seed,
                    seconds=seconds, simulations=simulations, source=str(source))
    manifest_path = output / "sample.json"
    if manifest_path.exists():
        manifest = read(manifest_path)
        if manifest["settings"] != settings:
            raise ValueError("resume settings differ from immutable sample")
        verify(output, manifest)
        return manifest
    identity_hash = digest(source / "identity.json")
    groups = {"unknown": {}, "feasible": {}}
    population = []
    for volume in sorted(set(volumes)):
        for path in sorted((source / "solver-results" / str(volume)).glob("*.json")):
            row = read(path)
            if (row["ordering_id"] != _ordering_id(row["ordering"])
                    or path.stem != row["ordering_id"] or row["volume"] != volume):
                raise ValueError(f"record ordering identity mismatch: {path}")
            if row.get("identity_sha256", row.get("plan_identity_sha256")) != identity_hash:
                raise ValueError(f"record model identity mismatch: {path}")
            group = ("unknown" if row.get("solver_status") == "UNKNOWN" else
                     "feasible" if row["status"] == "solver_feasible" else None)
            key = f"{volume}/{path.stem}"
            item = dict(key=key, sha256=digest(path), group=group,
                        status=row["status"], solver_status=row.get("solver_status"),
                        execution_origin=("server" if row.get("execution_identity_sha256")
                                          else "mac-pilot" if volume == 16 else "original-mac"),
                        execution_identity=row.get("execution_identity_sha256"),
                        source_path=str(path), snapshot_origin=origin)
            population.append(item)
            if group:
                groups[group][key] = item
    selected = []
    for index, (group, candidates) in enumerate(groups.items()):
        for key in select(candidates, count, seed + index):
            selected.append(dict(candidates[key], group=group))
    output.mkdir(parents=True, exist_ok=True)
    hashes = {}
    copies = ["identity.json", "frozen/manifest.json", "calibration/reference.json",
              "calibration/origin.json"]
    if (source / "server-execution-manifest.json").exists():
        copies.append("server-execution-manifest.json")
    copies += [str(p.relative_to(source)) for p in (source / "calibration/workspace").iterdir()
               if p.is_file()]
    for relative in copies:
        destination = output / "frozen" / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / relative, destination)
        hashes[str(destination.relative_to(output))] = digest(destination)
    for item in selected:
        destination = output / "originals" / (item["key"] + ".json")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(item["source_path"], destination)
        if digest(destination) != item["sha256"]:
            raise ValueError("source changed while copying")
        item["original"] = str(destination.relative_to(output))
        hashes[item["original"]] = item["sha256"]
    manifest = dict(schema=1, created_utc=utc(), settings=settings,
                    selection="completed records only, sorted keys, without replacement per group",
                    population=population, counts={k: len(v) for k, v in groups.items()},
                    samples=selected, hashes=hashes, identity_sha256=identity_hash,
                    policy="one long gap1 attempt; gap0 only after gap1 INFEASIBLE; "
                           "same seeds; remaining shared wall allowance; sequential execution")
    save(manifest_path, manifest)
    return manifest


def verify(output, manifest):
    for relative, expected in manifest["hashes"].items():
        if digest(Path(output) / relative) != expected:
            raise ValueError(f"immutable evidence changed: {relative}")


def audit_witness(plan, ref):
    """Check assignments and periodic hold inequalities without CP-SAT."""
    products = next(iter(plan["ordering"].values()))
    entry = next(iter(plan["ordering"]))
    pattern = plan["slot_pattern"]
    gap = plan["solver_model_gap"]
    if gap not in (0, 1) or len(products) != len(pattern):
        raise ValueError("invalid witness shape/gap")
    holds = {}
    phases = []
    first_grant = ref["windows"][pattern[0][2]][entry][1]
    for product, row in zip(products, pattern):
        p, departure, variant = row
        if (p != product or type(departure) is not int or not 0 <= departure < 1800
                or ref["variants"][variant]["product"] != product):
            raise ValueError("invalid witness assignment")
        phases.append((departure + ref["windows"][variant][entry][1] - first_grant) % 1800)
        for name, (_, grant, release) in ref["windows"][variant].items():
            if release - grant + gap > 1800:
                raise ValueError("self-period hold conflict")
            holds.setdefault(name, []).append((departure + grant, departure + release))
    if pattern[0][1] != 0 or any(a >= b for a, b in zip(phases, phases[1:])):
        raise ValueError("witness violates exact cyclic entry order")
    pairs = 0
    for intervals in holds.values():
        for i, (start, end) in enumerate(intervals):
            for other_start, other_end in intervals[i + 1:]:
                low, high = end - other_start + gap, 1800 - (other_end - start) - gap
                if math.ceil(low / 1800) > math.floor(high / 1800):
                    raise ValueError("periodic resource conflict")
                pairs += 1
    return dict(passed=True, train_count=len(pattern), resource_pairs=pairs,
                exact_order=True, gap=gap, rules=[], period=1800)


def retry(original, ref, seconds, checkpoint, *, solver=solve, stopped=lambda: False):
    """Charge reservations before execution: interrupted stages are never silently rerun."""
    state = read(checkpoint) if Path(checkpoint).exists() else {
        "status": "pending", "ordering": original["ordering"],
        "ordering_id": original["ordering_id"], "trials": [], "spent_wall_seconds": 0,
    }
    if state["status"] != "pending":
        return state
    entry = next(iter(original["ordering"]))
    products = original["ordering"][entry]
    options = {p: sorted(v for v, info in ref["variants"].items() if info["product"] == p)
               for p in set(products)}
    for attempt, gap in enumerate((1, 0)):
        if any(t["min_gap"] == gap for t in state["trials"]):
            continue
        remaining = max(0, seconds - state["spent_wall_seconds"])
        if stopped() or remaining <= 0:
            break
        seed = (4711 + int(original["ordering_id"][:8], 16) + attempt) % 2147483647
        state["status"] = "interrupted"
        state["reserved_seconds"] = remaining
        save(checkpoint, state)
        wall, cpu = time.monotonic(), time.process_time()
        try:
            result = solver(products, options, ref["windows"], remaining, frozenset(), 1,
                            min_gap=gap, period=1800, seed=seed, order=products, entry=entry)
        except Exception as exc:
            state.update(status="execution_error", error=f"{type(exc).__name__}: {exc}")
            save(checkpoint, state)
            raise
        elapsed = time.monotonic() - wall
        state["spent_wall_seconds"] += elapsed
        state["trials"].append(dict(min_gap=gap, seed=seed, budget_seconds=remaining,
                                    wall_seconds=elapsed, cpu_seconds=time.process_time() - cpu,
                                    solver=result))
        state.update(solver_status=result["status"], solver_model_gap=gap, reserved_seconds=0)
        if result["status"] in ("FEASIBLE", "OPTIMAL"):
            state.update(status="solver_feasible", slot_pattern=result["slot_pattern"])
            try:
                state["witness_audit"] = audit_witness(state, ref)
            except ValueError as exc:
                state.update(status="audit_error", error=str(exc))
                save(checkpoint, state)
                raise
        elif result["status"] == "INFEASIBLE":
            state["status"] = "conditional_no" if gap == 0 else "pending"
        elif result["status"] == "UNKNOWN":
            state["status"] = "still_unknown"
        else:
            state.update(status="execution_error", error="unexpected solver status: " + result["status"])
        save(checkpoint, state)
        if state["status"] != "pending":
            break
    return state


def replay(plan, ref, ws):
    audit = audit_witness(plan, ref)
    started = time.monotonic()
    score, raw = simulate(ws, ref["workspace"], plan["slot_pattern"],
                          {v: b["terminal"] for v, b in ref["baselines"].items()}, 4711, 1800)
    checked = audit_simulation(ws, score, raw, plan["slot_pattern"],
                               plan["ordering"], ref["variants"])
    expected = {f"{p}_{i:02}_p{k}": v
                for i, (p, _, v) in enumerate(plan["slot_pattern"]) for k in range(6)}
    rows = {r["vehicle_id"]: r for r in raw["trains"]}
    missing = [v for v in expected if rows.get(v, {}).get("status") != "terminal_arrived"]
    entry_resource = next(iter(plan["ordering"]))
    network = ET.parse(ws.runs_root / score["run_id"] / "inputs/network.xml")
    entry_links = {
        link.get("id") for link in network.iter("link")
        if any(a.get("name") == "railsimResourceId" and a.text == entry_resource
               for a in link.iter("attribute"))
    }
    observed = {}
    entries = {}
    for event in events(raw["events_file"]):
        if event["type"] == "entered link" and event.get("vehicle") in expected:
            observed.setdefault(event["vehicle"], []).append(event["link"])
        if (event["type"] == "railsimLinkStateChangeEvent"
                and event.get("vehicle") in expected and event.get("link") in entry_links
                and event.get("state") == "EXHAUSTED" and event.get("dla") != "true"):
            entries.setdefault(event["vehicle"], float(event["time"]))
    route_errors = [vehicle for vehicle, variant in expected.items()
                    if rows.get(vehicle, {}).get("route_id") != variant
                    or [link for link in observed.get(vehicle, []) if not link.startswith("loop")]
                    != ref["variants"][variant]["links"]]
    entry_errors = []
    for k in range(6):
        vehicles = [f"{p}_{i:02}_p{k}" for i, (p, _, _) in enumerate(plan["slot_pattern"])]
        products = {f"{p}_{i:02}_p{k}": p for i, (p, _, _) in enumerate(plan["slot_pattern"])}
        if (any(v not in entries for v in vehicles) or not cyclic_entry_matches(
                plan["ordering"][entry_resource], vehicles, entries, products, 1800)):
            entry_errors.append(k)
    passed = (_simulation_passed(score) and not missing and not route_errors
              and not entry_errors
              and not checked["route_errors"] and not checked["entry_order_errors"]
              and checked["detour_event_count"] == 0 and not checked["rerouting_markers"])
    run_root = ws.runs_root / score["run_id"]
    return dict(status="passed" if passed else "rejected", score=score,
                witness_audit=audit, audit=checked, repeated_expected=len(expected),
                repeated_missing=missing, repeated_route_errors=route_errors,
                repeated_entry_order_errors=entry_errors,
                raw_run_sha256=digest(run_root / "run.json"),
                score_sha256=digest(run_root / "output/simulation.json"),
                wall_seconds=time.monotonic() - started)


def verify_runtime(project, output):
    import ortools
    if ortools.__version__ != "9.15.6755":
        raise ValueError("this frozen experiment requires OR-Tools 9.15.6755")
    identity = read(output / "frozen/identity.json")
    protected = read(output / "frozen/calibration/origin.json")["protected_simulator_code_hashes"]
    for relative, expected in protected.items():
        if digest(project / relative) != expected:
            raise ValueError(f"simulator physics differs: {relative}")
    expected_solver = identity["source_hashes"]["src/main/python/railsim_mcp/solver.py"]
    if digest(project / "src/main/python/railsim_mcp/solver.py") != expected_solver:
        raise ValueError("canonical frozen solver changed")
    ref = read(output / "frozen/calibration/reference.json")
    for filename, expected in ref["workspace_hashes"].items():
        if digest(output / "frozen/calibration/workspace" / filename) != expected:
            raise ValueError("calibrated workspace changed: " + filename)
    for key, expected in read(output / "frozen/calibration/origin.json")["runtime"].items():
        if key in ("RAILSIM_DEADLOCK_AVOIDANCE", "RAILSIM_SIM_JVM_OPTIONS"):
            if os.environ.get(key) != expected:
                raise ValueError("simulation environment differs: " + key)
    return ref


def run(output, project, classpath):
    output, project = Path(output).resolve(), Path(project).resolve()
    manifest = read(output / "sample.json")
    verify(output, manifest)
    ref = verify_runtime(project, output)
    settings = manifest["settings"]
    validate_limits(settings["seconds"], settings["simulations"])
    state = output / "state"
    ws = PersistentScenarioWorkspace(
        project / "src/main/resources/scenarios", state / "workspaces",
        project_root=project, runs_root=state / "runs",
        classpath=str(project / "target/classes") + os.pathsep + classpath)
    ws.timeout_seconds = 120
    destination = ws.workspace_root / ref["workspace"]
    if not destination.exists():
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(output / "frozen/calibration/workspace", destination)
    invocation = dict(started_utc=utc(), python=sys.version,
         ortools="9.15.6755", platform=platform.platform(), project=str(project),
         java_home=os.environ.get("JAVA_HOME"), jvm_options=os.environ.get("MAVEN_OPTS"),
         workers=1, concurrency="sequential solver and JVM; no overlapping compute",
         command=sys.argv, sample_sha256=digest(output / "sample.json"))
    history = read(output / "invocations.json") if (output / "invocations.json").exists() else []
    history.append(invocation)
    save(output / "invocations.json", history)
    if not (output / "execution.json").exists():
        save(output / "execution.json", invocation)
    failures = []
    try:
        # Original random witnesses first: no budget can be consumed by rediscovery.
        for item in sorted(manifest["samples"], key=lambda item: item["group"] != "feasible"):
            if (output / "STOP").exists():
                break
            original = read(output / item["original"])
            case = output / "results" / item["key"]
            case.mkdir(parents=True, exist_ok=True)
            try:
                plan = (retry(original, ref, settings["seconds"], case / "retry.json",
                              stopped=lambda: (output / "STOP").exists())
                        if item["group"] == "unknown" else original)
                if plan["status"] != "solver_feasible":
                    continue
                target = case / "simulation.json"
                if target.exists():
                    continue
                used = list((output / "results").glob("*/*/simulation.json"))
                if len(used) >= settings["simulations"] or (output / "STOP").exists():
                    continue
                # Reservation survives interruption and consumes one call on resume.
                save(target, dict(status="interrupted", reserved_utc=utc()))
                wall, cpu = time.monotonic(), time.process_time()
                before = resource.getrusage(resource.RUSAGE_CHILDREN)
                try:
                    result = replay(plan, ref, ws)
                except Exception as exc:
                    save(target, dict(status="execution_error", error=f"{type(exc).__name__}: {exc}"))
                    raise
                finally:
                    # Reap JVM per case so child CPU accounting includes its real cost.
                    ws.close()
                after = resource.getrusage(resource.RUSAGE_CHILDREN)
                result.update(cpu_seconds=time.process_time() - cpu + after.ru_utime + after.ru_stime
                              - before.ru_utime - before.ru_stime,
                              wall_seconds=time.monotonic() - wall)
                save(target, result)
            except Exception as exc:
                failures.append(dict(key=item["key"], error=f"{type(exc).__name__}: {exc}"))
                save(case / "error.json", failures[-1])
                print(json.dumps(failures[-1]), file=sys.stderr, flush=True)
            finally:
                report(output)
    finally:
        ws.close()
        verify(output, manifest)
        report(output)
    rows = read(output / "report.json")["cases"]
    failed_states = {"execution_error", "audit_error", "interrupted", "rejected"}
    if failures or any(row["new_status"] in failed_states
                       or (row["simulation"] or {}).get("status") in failed_states for row in rows):
        return 1
    if (output / "STOP").exists() or any(
            row["group"] == "unknown" and row["new_status"] in ("unresolved", "pending")
            for row in rows):
        return 2
    return 0


def report(output):
    output = Path(output)
    manifest = read(output / "sample.json")
    rows = []
    for item in manifest["samples"]:
        original = read(output / item["original"])
        case = output / "results" / item["key"]
        retry_result = read(case / "retry.json") if (case / "retry.json").exists() else None
        sim = read(case / "simulation.json") if (case / "simulation.json").exists() else None
        plan = retry_result or original
        rows.append(dict(key=item["key"], group=item["group"],
                         source=item["execution_origin"], original_status=original["solver_status"],
                         original_gap=original["solver_model_gap"],
                         new_status=plan["status"] if retry_result else "original witness",
                         gap=plan.get("solver_model_gap"),
                         allowance=manifest["settings"]["seconds"] if item["group"] == "unknown" else None,
                         actual_wall=plan.get("spent_wall_seconds") if retry_result else None,
                         cpu_seconds=sum(t["cpu_seconds"] for t in plan.get("trials", [])
                                         if "cpu_seconds" in t),
                         audit=(sim or {}).get("witness_audit", plan.get("witness_audit")),
                         simulation=sim))
    summary = Counter(row["new_status"] for row in rows if row["group"] == "unknown")
    usage = dict(
        solver_wall_seconds=sum(row["actual_wall"] or 0 for row in rows),
        solver_cpu_seconds=sum(row["cpu_seconds"] for row in rows),
        simulation_wall_seconds=sum((row["simulation"] or {}).get("wall_seconds", 0) for row in rows),
        simulation_cpu_seconds=sum((row["simulation"] or {}).get("cpu_seconds", 0) for row in rows),
        simulation_calls=sum(row["simulation"] is not None for row in rows),
        provider_calls=0,
    )
    save(output / "report.json", dict(updated_utc=utc(), summary=dict(summary), cases=rows, usage=usage))
    rates = "/".join(str(v * 2) for v in manifest["settings"]["volumes"])
    lines = [
        "| UC1 mode | BB1 | BB2 | BB3 total trains/hour |",
        "| --- | --- | --- | --- |",
        f"| MAINLINE/FV_STOP | Not sampled | Not sampled | {rates} diagnostic sample only* |",
        "",
        "*No flexibility curve or capacity maximum is estimated. Higher and unsampled orderings remain unclassified.",
        "",
        f"Frozen sample: seed {manifest['settings']['seed']}; completed-only populations "
        f"{manifest['counts']}; snapshot {manifest['created_utc']}; origin {manifest['settings']['origin']}.",
        "Sorted populations were sampled without replacement, separately for UNKNOWN and saved feasible witnesses.",
        "Original witnesses are replayed unchanged. Newly resolved witnesses form a separate additional replay set.",
        "",
        "| Case | Set | Origin | Original status/gap | New status/gap | Solver cap s | Solver actual s | Audit | Replay / max delay | Replay actual s |",
        "| --- | --- | --- | --- | --- | ---: | ---: | --- | --- | ---: |",
    ]
    for row in rows:
        sim = row["simulation"]
        replay_text = (f"{sim['status']} / {sim.get('score', {}).get('max_delay', '-')}"
                       if sim else "not attempted")
        actual = "-" if row["actual_wall"] is None else f"{row['actual_wall']:.2f}"
        simulation_actual = f"{sim['wall_seconds']:.2f}" if sim and "wall_seconds" in sim else "-"
        lines.append(f"| {row['key']} | {row['group']} | {row['source']} | "
                     f"{row['original_status']}/g{row['original_gap']} | "
                     f"{row['new_status']}/g{row['gap']} | {row['allowance'] or '-'} | "
                     f"{actual} | {'pass' if row['audit'] else 'not available'} | "
                     f"{replay_text} | {simulation_actual} |")
    lines += ["", f"UNKNOWN outcomes: {dict(summary)}.",
              f"Measured local usage: solver {usage['solver_wall_seconds']:.2f} wall / "
              f"{usage['solver_cpu_seconds']:.2f} CPU seconds; simulator "
              f"{usage['simulation_wall_seconds']:.2f} wall / {usage['simulation_cpu_seconds']:.2f} "
              f"CPU seconds; {usage['simulation_calls']} simulator calls; zero provider calls. "
              "Build, snapshot and test overhead are separate from these experiment measurements.",
              "UNKNOWN never means infeasible. Only gap0 INFEASIBLE is a conditional exclusion "
              "of this exact fixed-profile ordering, not unrestricted physical impossibility.",
              "Server/Mac hardware and runtime differences confound time-only causal claims. "
              "A small completed-case sample cannot classify all UNKNOWNs. No active run's budget is changed.",
              "Original solve policy: gap1 at 3 seconds, retry UNKNOWN at 15 seconds; "
              "gap0 only after gap1 INFEASIBLE. Diagnostic policy: one long attempt per eligible "
              "gap, with the original seed and a shared 600-second ceiling (or configured smaller cap). "
              "Actual times can be far below their allowances when the solver proves an exclusion early.",
              "Execution is sequential: one CP-SAT worker, then one bounded JVM replay; "
              "at most the recorded per-order allowance and simulation limit. "
              "Interrupted reservations are retained, never silently rerun or resampled.",
              "Per-attempt seeds, budgets, wall/CPU, schedules, errors, hashes and raw evidence "
              "are retained in sample.json, results/, execution.json and state/runs/."]
    if summary["solver_feasible"] > manifest["settings"]["count"] / 2:
        lines += ["Most sampled UNKNOWNs resolved feasible: evidence supports a longer-budget "
                  "follow-up, not an automatic server-timeout change."]
    elif summary["solver_feasible"] + summary["conditional_no"] > manifest["settings"]["count"] / 2:
        lines += ["Most sampled UNKNOWNs reached a definite model classification under the "
                  "long-attempt policy. This supports a larger-budget diagnostic follow-up to "
                  "reduce unresolved classifications, not a claim that the earlier UNKNOWNs "
                  "were feasible or that time alone caused the difference."]
    if (output / "verification.json").exists():
        verification = read(output / "verification.json")
        lines += ["", "## Execution and verification", ""]
        for check in verification["checks"]:
            lines.append(f"- `{check['command']}`: {check['result']}")
        lines += ["", verification["environment"], "", verification["notes"]]
    (output / "report.md").write_text("\n".join(lines) + "\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    prep = sub.add_parser("prepare")
    prep.add_argument("--source", type=Path, required=True)
    prep.add_argument("--origin", required=True)
    prep.add_argument("--volumes", type=int, nargs="+", default=[13, 14])
    prep.add_argument("--count", type=int, default=5)
    prep.add_argument("--seed", type=int, default=20261105)
    prep.add_argument("--seconds", type=float, default=600)
    prep.add_argument("--simulations", type=int, default=10)
    execute = sub.add_parser("run")
    execute.add_argument("--project", type=Path, required=True)
    execute.add_argument("--classpath-file", type=Path, required=True)
    sub.add_parser("report")
    for command in sub.choices.values():
        command.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "prepare":
        source, output = args.source.resolve(), args.output.resolve()
        if source == output or source in output.parents or output in source.parents:
            parser.error("source and output must be disjoint")
    args.output.mkdir(parents=True, exist_ok=True)
    with (args.output / ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.command == "prepare":
            prepare(args.source, args.output, origin=args.origin, volumes=args.volumes,
                    count=args.count, seed=args.seed, seconds=args.seconds, simulations=args.simulations)
        elif args.command == "run":
            return run(args.output, args.project, args.classpath_file.read_text().strip())
        else:
            report(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
