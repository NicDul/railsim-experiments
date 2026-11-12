"""Adaptive entry-order replay and bounded local solver pilot."""

import argparse
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import subprocess
import sys
import time

import ortools

from .adaptive_sampling import (
    DEFAULT_CHECKPOINTS,
    AdaptiveSampler,
    LocalFixedProfileSolverProvider,
    SavedSolverResultProvider,
    SamplingCase,
    SolverResultSource,
    identity_digest,
    run_fixed_pilot,
)
from .budget import Exhausted, cpu_seconds
from .capacity import Workflow, digest, save
from .configuration import Settings
from .flexibility import order_groups
from .runtime import SIM_JVM_OPTIONS
from .uc1_modes import UC1Mode


def _load_experiment_config(path):
    config = json.loads(Path(path).read_text())
    allowed = {
        "use_case",
        "building_block",
        "mode",
        "period_seconds",
        "volumes",
        "seed",
        "pilot_seed",
        "solver_seed",
        "checkpoints",
        "alpha",
        "target_width",
        "comparison_points",
    }
    if set(config) - allowed:
        raise ValueError("experiment config contains unknown fields")
    if config.get("use_case") != "UC1":
        raise ValueError("adaptive entry-order sampling currently supports UC1 only")
    if config.get("building_block") not in ("BB1", "BB2", "BB3"):
        raise ValueError("building_block must be BB1, BB2 or BB3")
    if not isinstance(config.get("mode"), str) or not config["mode"]:
        raise ValueError("mode is required")
    if type(config.get("period_seconds")) is not int or config["period_seconds"] < 1:
        raise ValueError("period_seconds must be a positive integer")
    volumes = config.get("volumes")
    if (
        not isinstance(volumes, list)
        or len(volumes) != 4
        or any(type(volume) is not int or volume < 1 for volume in volumes)
        or volumes != sorted(set(volumes))
    ):
        raise ValueError("volumes must contain four sorted, distinct positive integers")
    for name in ("seed", "pilot_seed", "solver_seed"):
        value = config.get(name, 4711)
        if type(value) is not int or value < 0:
            raise ValueError(f"{name} must be a nonnegative integer")
        config[name] = value
    checkpoints = config.get("checkpoints", list(DEFAULT_CHECKPOINTS))
    if (
        not isinstance(checkpoints, list)
        or not checkpoints
        or any(type(value) is not int or value < 1 for value in checkpoints)
        or checkpoints != sorted(set(checkpoints))
    ):
        raise ValueError("checkpoints must be sorted unique positive integers")
    config["checkpoints"] = checkpoints
    config["alpha"] = config.get("alpha", 0.05)
    config["target_width"] = config.get("target_width", 0.10)
    if (
        type(config["alpha"]) not in (int, float)
        or not math.isfinite(config["alpha"])
        or not 0 < config["alpha"] < 1
    ):
        raise ValueError("alpha must be finite and in (0,1)")
    if (
        type(config["target_width"]) not in (int, float)
        or not math.isfinite(config["target_width"])
        or not 0 < config["target_width"] <= 1
    ):
        raise ValueError("target_width must be finite and in (0,1]")
    config["comparison_points"] = config.get("comparison_points", 4)
    if (
        type(config["comparison_points"]) is not int
        or config["comparison_points"] < 4
    ):
        raise ValueError("comparison_points must retain all four declared points")
    return config


def _mode(project, config):
    plan = (
        project
        / "src/main/resources/scenarios/use_case_1/uc1_operational_plan.json"
    )
    mode = UC1Mode.load(plan, config["mode"])
    return mode, plan


def _case_manifest(project, config, mode, plan_path, cases, extra=None):
    source = (
        project
        / "src/main/resources/scenarios/use_case_1"
        / f'building_block_{config["building_block"][-1]}'
    )
    paths = [plan_path, *sorted(path for path in source.iterdir() if path.is_file())]
    return {
        "domain": {
            "use_case": config["use_case"],
            "building_block": config["building_block"],
            "mode": config["mode"],
            "period_seconds": config["period_seconds"],
            "volumes_per_period": config["volumes"],
            "total_trains_per_hour": [
                3600 * volume / config["period_seconds"]
                for volume in config["volumes"]
            ],
        },
        "mode": mode.record(),
        "integer_compositions": [
            {
                "volume": volume,
                "counts": mode.composition(volume),
                "ordering_denominator": case.denominator,
                "entry": case.entry,
            }
            for volume, case in zip(config["volumes"], cases)
        ],
        "source_hashes": {
            str(path.relative_to(project)): digest(path) for path in paths
        },
        "assumptions": [
            "UC1 fixed routes and largest-remainder 40/40/20 product allocation.",
            "Cyclic rotations are equivalent; platform and timing choices are solver freedoms.",
            "Fixed-profile results do not establish simulator-validated flexibility.",
        ],
        "census_aggregates_used_for_sampling_or_stopping": False,
        "sampled_outcomes_drive_checkpoint_stopping": True,
        **(extra or {}),
    }


def _saved_source_identity(results_directory):
    path = Path(results_directory).parent / "identity.json"
    content = path.read_bytes()
    value = json.loads(content)
    return {
        "identity_file_sha256": hashlib.sha256(content).hexdigest(),
        "identity_object_sha256": identity_digest(value),
    }


def _compare_current_sources(project, saved_identity):
    mismatches = []
    for relative, expected in sorted(saved_identity.get("source_hashes", {}).items()):
        path = project / relative
        actual = digest(path) if path.is_file() else None
        if actual != expected:
            mismatches.append(
                {"path": relative, "saved_sha256": expected, "current_sha256": actual}
            )
    return mismatches


def _make_cases(config, mode, entries):
    cases = []
    for volume in config["volumes"]:
        counts = {
            product: count
            for product, count in mode.composition(volume).items()
            if count
        }
        cases.append(
            SamplingCase(
                key=str(volume),
                volume=volume,
                entry=entries[volume],
                class_counts=counts,
            )
        )
    return cases


def _environment(*, include_java=False):
    value = {
        "python": sys.version.split()[0],
        "python_implementation": platform.python_implementation(),
        "ortools": ortools.__version__,
        "os": platform.system(),
        "machine": platform.machine(),
    }
    if include_java:
        java = subprocess.run(
            ["java", "-version"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        value["java"] = (java.stderr or java.stdout).splitlines()[0]
    return value


def _load_replay_provider(args, audit):
    if not isinstance(audit.get("model_identity_sha256"), str):
        raise ValueError("source audit lacks model_identity_sha256")
    base_identity = _saved_source_identity(args.base_results)
    overlay_identity = _saved_source_identity(args.overlay_results)
    if any(
        source["identity_file_sha256"] != audit["model_identity_sha256"]
        for source in (base_identity, overlay_identity)
    ):
        raise ValueError("saved result identity files do not match the source audit")
    provider = SavedSolverResultProvider(
        [
            SolverResultSource(
                "base",
                args.base_results,
                args.base_budget_label,
                base_identity,
            ),
            SolverResultSource(
                "overlay",
                args.overlay_results,
                args.overlay_budget_label,
                overlay_identity,
            ),
        ],
        model_identity_sha256=audit["model_identity_sha256"],
        corpus_identity={
            "source_audit_sha256": digest(args.source_audit),
            "replay_cli_sha256": digest(Path(__file__)),
            "inventory": audit.get("inventory", {}),
            "execution_identity_sha256": audit.get(
                "execution_identity_sha256"
            ),
        },
    )
    return provider, base_identity, overlay_identity


def _run_replay(args, project, config, output):
    replay_started = time.monotonic()
    audit = json.loads(args.source_audit.read_text())
    provider, base_identity, overlay_identity = _load_replay_provider(
        args, audit
    )
    mode, plan_path = _mode(project, config)
    base_identity_file = Path(args.base_results).parent / "identity.json"
    saved_identity = json.loads(base_identity_file.read_text())
    domain = saved_identity.get("domain", {})
    if (
        domain.get("use_case") != config["use_case"]
        or domain.get("building_block") != config["building_block"]
        or domain.get("mode") != config["mode"]
        or domain.get("period_seconds") != config["period_seconds"]
    ):
        raise ValueError("saved result domain does not match the replay config")
    entries = {
        volume: provider.entry_for_volume(volume)
        for volume in config["volumes"]
    }
    cases = _make_cases(config, mode, entries)
    saved_denominators = domain.get("denominators", {})
    for case in cases:
        if saved_denominators.get(str(case.volume)) != case.denominator:
            raise ValueError(
                f"saved ordering denominator mismatch at volume {case.volume}"
            )
    mismatch_paths = _compare_current_sources(project, saved_identity)
    manifest = _case_manifest(
        project,
        config,
        mode,
        plan_path,
        cases,
        extra={
            "sampling": {
                "seed": config["seed"],
                "checkpoints": config["checkpoints"],
                "alpha": config["alpha"],
                "comparison_points": config["comparison_points"],
                "target_absolute_width": config["target_width"],
            },
            "saved_corpus": {
                "model_identity_sha256": audit["model_identity_sha256"],
                "source_audit_sha256": digest(args.source_audit),
                "inventory": audit.get("inventory", {}),
                "base_identity": base_identity,
                "overlay_identity": overlay_identity,
                "ordering_domain_match": True,
                "curve_points_sha256": (
                    digest(args.curve_points) if args.curve_points else None
                ),
                "current_source_mismatches": mismatch_paths,
                "current_source_matches_saved_model": not mismatch_paths,
            },
        },
    )
    save(output / "case-manifest.json", manifest)
    preprocessing_wall = time.monotonic() - replay_started
    started = time.monotonic()
    sampler = AdaptiveSampler(
        cases,
        provider,
        output,
        seed=config["seed"],
        checkpoints=config["checkpoints"],
        alpha=config["alpha"],
        target_width=config["target_width"],
        comparison_points=config["comparison_points"],
    )
    report = sampler.run()
    final_checkpoints = [
        row["checkpoints"][-1]
        for row in report["cases"]
        if row["checkpoints"]
    ]
    report["execution"] = {
        "kind": "saved-result replay; no solver or simulator execution",
        "wall_seconds": time.monotonic() - started,
        "preprocessing_wall_seconds": preprocessing_wall,
        "environment": _environment(),
        "unique_provider_wall_seconds": sum(
            row["unique_provider_wall_seconds"] for row in final_checkpoints
        ),
        "unique_source_lookup_wall_seconds": sum(
            row["unique_source_lookup_wall_seconds"]
            for row in final_checkpoints
        ),
        "solver_calls": 0,
        "simulator_calls": 0,
        "external_model_provider_calls": 0,
        "unique_saved_orderings_checked": sum(
            row["unique_checked"] for row in final_checkpoints
        ),
    }
    save(output / "adaptive-report.json", report)
    return report


def _run_fresh_pilot(args, project, config, output):
    setup_started = time.monotonic()
    if not args.state_root:
        raise ValueError("--state-root is required for fresh-pilot")
    if not 1 <= args.workers <= 2:
        raise ValueError("fresh-pilot supports one or two worker processes")
    if not 1 <= args.pilot_orders_per_case <= 4:
        raise ValueError("pilot orders per case must be in 1..4")
    if not 0 < args.active_wall_limit_seconds <= 900:
        raise ValueError("active wall limit must be in (0,900] seconds")
    if not 0 < args.first_seconds <= 600 or not 0 < args.retry_seconds <= 600:
        raise ValueError("solver attempt limits must be in (0,600]")
    settings = Settings.load(project, args.state_root)
    mode, plan_path = _mode(project, config)
    workflow_config = {
        "use_case": "UC1",
        "building_block": config["building_block"],
        "mode": config["mode"],
        "period_seconds": config["period_seconds"],
        "volumes": config["volumes"],
        "seed": config["solver_seed"],
        "workers": 1,
        "solver_seconds": args.retry_seconds,
        "rounds": 0,
        "proposals": 1,
        "runner": "none",
        "limits": {
            "wall_seconds": args.active_wall_limit_seconds,
            "cpu_seconds": args.active_wall_limit_seconds * args.workers,
            "simulations": 120,
            "model_calls": 0,
        },
    }
    calibration_output = output / "calibration-workflow"
    invocation = [
        sys.executable,
        "-m",
        "railsim_mcp.adaptive_sampling_cli",
        "fresh-pilot",
        *sys.argv[1:],
    ]
    os.environ["MAVEN_OPTS"] = (
        os.environ.get("MAVEN_OPTS", "") + " " + SIM_JVM_OPTIONS
    ).strip()
    environment = _environment(include_java=True)
    setup_wall = time.monotonic() - setup_started
    started_wall = time.monotonic()
    started_cpu = cpu_seconds()
    workflow = Workflow(
        settings,
        workflow_config,
        calibration_output,
        invocation,
        minimum_volume=4,
    )
    previous_wall = workflow.budget.state.get("wall_seconds", 0.0)
    previous_cpu = workflow.budget.state.get("cpu_seconds", 0.0)
    try:
        reference = workflow.calibrate()
    except Exhausted as exc:
        workflow.budget.refresh()
        workflow.persist()
        report = {
            "schema_version": 1,
            "status": "calibration_budget_exhausted",
            "precision_not_reached": True,
            "reason": str(exc),
            "active_wall_limit_seconds": args.active_wall_limit_seconds,
            "active_wall_seconds": workflow.budget.state["wall_seconds"],
            "active_wall_this_invocation_seconds": (
                time.monotonic() - started_wall
            ),
            "environment": environment,
            "aggregate_cpu_seconds": workflow.budget.state["cpu_seconds"],
            "cpu_seconds_this_invocation": cpu_seconds() - started_cpu,
            "budget": dict(workflow.report.get("budget", {})),
            "solver_calls": 0,
            "simulator_calls": workflow.report.get("budget", {}).get(
                "simulations", 0
            ),
            "external_model_provider_calls": 0,
            "setup_source_io_wall_seconds": setup_wall,
        }
        save(output / "pilot-execution.json", report)
        return report
    workflow.budget.refresh()
    workflow.persist()
    cumulative_wall = workflow.budget.state["wall_seconds"]
    cumulative_cpu = workflow.budget.state["cpu_seconds"]
    calibration_wall = cumulative_wall - previous_wall
    calibration_cpu = cumulative_cpu - previous_cpu
    remaining_wall = max(0.0, args.active_wall_limit_seconds - cumulative_wall)
    deadline = time.monotonic() + remaining_wall
    calibration_sha256 = identity_digest(reference)
    model_identity_sha256 = identity_digest(workflow.identity)
    source = (
        project
        / "src/main/resources/scenarios/use_case_1"
        / f'building_block_{config["building_block"][-1]}'
    )
    entries = {}
    for volume in config["volumes"]:
        counts = {
            product: count
            for product, count in mode.composition(volume).items()
            if count
        }
        groups = order_groups("UC1", source, counts, reference["windows"])
        if len(groups) != 1:
            raise ValueError("UC1 adaptive sampling expects one shared entry")
        entries[volume] = next(iter(groups))
    cases = _make_cases(config, mode, entries)
    source_identity = {
        "current_model_identity_sha256": model_identity_sha256,
        "calibration_reference_sha256": calibration_sha256,
        "source_revision": workflow.report.get("source_revision"),
        "source_dirty": workflow.report.get("source_dirty"),
        "source_hashes": workflow.code,
        "input_hashes": workflow.inputs,
        "simulator_calls_during_calibration": workflow.report.get(
            "budget", {}
        ).get("simulations", 0),
        "solver_attempt_budget": {
            "first_wall_seconds": args.first_seconds,
            "unknown_retry_wall_seconds": args.retry_seconds,
            "deterministic_time_equivalent": False,
        },
        "worker_processes": args.workers,
        "num_search_workers": 1,
        "nice_value": os.nice(0),
    }
    provider = LocalFixedProfileSolverProvider(
        reference,
        period=config["period_seconds"],
        seed=config["solver_seed"],
        workers=args.workers,
        first_seconds=args.first_seconds,
        retry_seconds=args.retry_seconds,
        deadline=deadline,
        active_wall_limit_seconds=args.active_wall_limit_seconds,
        model_identity_sha256=model_identity_sha256,
        calibration_sha256=calibration_sha256,
        on_result=lambda _task, _result: workflow.persist(),
    )
    pilot_plan_path = output / "fresh-pilot" / "pilot-plan.json"
    if pilot_plan_path.exists():
        existing_plan = json.loads(pilot_plan_path.read_text())
        orders_per_case = existing_plan["identity"]["pilot"]["draws_per_case"]
    else:
        max_full_orders = int(
            remaining_wall / provider.max_order_wall_seconds * args.workers
        )
        orders_per_case = min(
            args.pilot_orders_per_case,
            max_full_orders // len(cases),
        )
    manifest = _case_manifest(
        project,
        config,
        mode,
        plan_path,
        cases,
        extra={
            "sampling": {
                "pilot_seed": config["pilot_seed"],
                "solver_seed": config["solver_seed"],
                "draws_per_case": orders_per_case,
                "precision_claim": False,
            },
            "fresh_model": source_identity,
        },
    )
    save(output / "case-manifest.json", manifest)
    if orders_per_case < 1:
        report = {
            "schema_version": 1,
            "status": "budget_limited_before_solver_pilot",
            "precision_not_reached": True,
            "reason": (
                "remaining active-wall allowance cannot fit one worst-case order "
                "per comparison point"
            ),
            "active_wall_limit_seconds": args.active_wall_limit_seconds,
            "aggregate_cpu_seconds": workflow.budget.state["cpu_seconds"],
            "calibration_wall_seconds": calibration_wall,
            "calibration_cpu_seconds": calibration_cpu,
            "calibration_reference_sha256": calibration_sha256,
            "model_identity_sha256": model_identity_sha256,
            "environment": environment,
            "solver_calls": 0,
            "simulator_calls": workflow.report.get("budget", {}).get(
                "simulations", 0
            ),
            "external_model_provider_calls": 0,
            "setup_source_io_wall_seconds": setup_wall,
            "active_wall_seconds": cumulative_wall,
            "active_wall_this_invocation_seconds": (
                time.monotonic() - started_wall
            ),
        }
        save(output / "pilot-execution.json", report)
        workflow.persist()
        return report
    pilot_started = time.monotonic()
    report = run_fixed_pilot(
        cases,
        provider,
        output / "fresh-pilot",
        seed=config["pilot_seed"],
        draws_per_case=orders_per_case,
    )
    pilot_wall = time.monotonic() - pilot_started
    workflow.budget.refresh()
    workflow.persist()
    total_wall = time.monotonic() - started_wall
    total_cpu = cpu_seconds() - started_cpu
    cumulative_wall = workflow.budget.state["wall_seconds"]
    cumulative_cpu = workflow.budget.state["cpu_seconds"]
    solve_records = []
    for case in cases:
        case_dir = output / "fresh-pilot" / "order-results" / str(case.volume)
        for path in case_dir.glob("*.json"):
            record = json.loads(path.read_text())
            solve_records.append(record["provider_result"])
    execution = {
        "active_wall_limit_seconds": args.active_wall_limit_seconds,
        "active_wall_seconds": cumulative_wall,
        "active_wall_this_invocation_seconds": total_wall,
        "setup_source_io_wall_seconds": setup_wall,
        "aggregate_cpu_seconds": cumulative_cpu,
        "cpu_seconds_this_invocation": total_cpu,
        "calibration_wall_seconds": calibration_wall,
        "calibration_cpu_seconds": calibration_cpu,
        "pilot_solver_wall_seconds": pilot_wall,
        "solver_cpu_seconds": sum(
            row.get("cpu_seconds", 0) for row in solve_records
        ),
        "fresh_unique_orderings_solved": len(solve_records),
        "calibration_simulator_calls": workflow.report.get("budget", {}).get(
            "simulations", 0
        ),
        "external_model_provider_calls": 0,
        "solver_calls": sum(
            row.get("attempt_count", 0) for row in solve_records
        ),
        "simulator_calls_after_calibration": 0,
        "worker_processes": args.workers,
        "num_search_workers": 1,
        "nice_value": os.nice(0),
        "environment": environment,
        "current_model_identity_sha256": model_identity_sha256,
        "calibration_reference_sha256": calibration_sha256,
        "solver_budget_comparison": (
            "Numeric caps mirror 0.546s + 61.96s, but the portable solver "
            "accepts wall-time limits, not deterministic-time limits; status "
            "and cost comparability is therefore limited."
        ),
    }
    report["execution"] = execution
    save(output / "pilot-execution.json", report)
    save(output / "fresh-pilot/pilot-results.json", report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("replay", "fresh-pilot"))
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--state-root", type=Path)
    parser.add_argument("--base-results", type=Path)
    parser.add_argument("--overlay-results", type=Path)
    parser.add_argument("--source-audit", type=Path)
    parser.add_argument("--curve-points", type=Path)
    parser.add_argument("--base-budget-label", default="wall-3s+15s")
    parser.add_argument(
        "--overlay-budget-label", default="deterministic-0.546+61.96"
    )
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--pilot-orders-per-case", type=int, default=4)
    parser.add_argument("--active-wall-limit-seconds", type=float, default=900)
    parser.add_argument("--first-seconds", type=float, default=0.546)
    parser.add_argument("--retry-seconds", type=float, default=61.96)
    args = parser.parse_args()
    project = args.project.expanduser().resolve()
    output = args.output.expanduser().resolve()
    try:
        config = _load_experiment_config(args.config)
        output.mkdir(parents=True, exist_ok=True)
        if output.is_relative_to(project):
            raise ValueError("output must be outside the simulator checkout")
        with (output / ".lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if args.mode == "replay":
                if not all(
                    (args.base_results, args.overlay_results, args.source_audit)
                ):
                    raise ValueError(
                        "replay requires base/overlay result directories and source audit"
                    )
                report = _run_replay(args, project, config, output)
            else:
                report = _run_fresh_pilot(args, project, config, output)
        print(json.dumps(report, indent=2, sort_keys=True))
    except (ValueError, OSError, RuntimeError) as exc:
        parser.exit(2, f"adaptive-flexibility: {exc}\n")


if __name__ == "__main__":
    main()
