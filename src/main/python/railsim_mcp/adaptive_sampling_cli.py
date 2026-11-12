"""Adaptive entry-order replay, fresh runs and bounded local solver pilots."""

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import signal
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


def _load_experiment_config(path, *, full_fresh=False):
    config = json.loads(Path(path).read_text())
    allowed = {
        "use_case",
        "building_block",
        "building_blocks",
        "mode",
        "period_seconds",
        "volumes",
        "volumes_by_building_block",
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
    if not isinstance(config.get("mode"), str) or not config["mode"]:
        raise ValueError("mode is required")
    if type(config.get("period_seconds")) is not int or config["period_seconds"] < 1:
        raise ValueError("period_seconds must be a positive integer")
    multi_block = (
        "building_blocks" in config
        or "volumes_by_building_block" in config
    )
    point_count = 0
    if multi_block:
        if not full_fresh:
            raise ValueError(
                "multi-building-block configs are supported only by fresh-run"
            )
        if "building_block" in config or "volumes" in config:
            raise ValueError(
                "use building_blocks/volumes_by_building_block or "
                "building_block/volumes, not both"
            )
        blocks = config.get("building_blocks")
        volumes_by_block = config.get("volumes_by_building_block")
        if (
            not isinstance(blocks, list)
            or not blocks
            or any(block not in ("BB1", "BB2", "BB3") for block in blocks)
            or blocks != sorted(set(blocks))
        ):
            raise ValueError(
                "building_blocks must be a sorted unique list from BB1, BB2, BB3"
            )
        if (
            not isinstance(volumes_by_block, dict)
            or set(volumes_by_block) != set(blocks)
        ):
            raise ValueError(
                "volumes_by_building_block must contain exactly the declared blocks"
            )
        normalized = {}
        for block in blocks:
            volumes = volumes_by_block[block]
            if (
                not isinstance(volumes, list)
                or not volumes
                or any(type(volume) is not int or not 1 <= volume <= 100 for volume in volumes)
                or volumes != sorted(set(volumes))
            ):
                raise ValueError(
                    f"{block} volumes must be sorted distinct integers in 1..100"
                )
            normalized[block] = volumes
            point_count += len(volumes)
        config["building_blocks"] = blocks
        config["volumes_by_building_block"] = normalized
    else:
        if config.get("building_block") not in ("BB1", "BB2", "BB3"):
            raise ValueError("building_block must be BB1, BB2 or BB3")
        volumes = config.get("volumes")
        minimum_count = 1 if full_fresh else 4
        if (
            not isinstance(volumes, list)
            or len(volumes) < minimum_count
            or (not full_fresh and len(volumes) != 4)
            or any(type(volume) is not int or not 1 <= volume <= 100 for volume in volumes)
            or volumes != sorted(set(volumes))
        ):
            if full_fresh:
                raise ValueError(
                    "volumes must be sorted distinct positive integers in 1..100"
                )
            raise ValueError(
                "volumes must contain four sorted, distinct positive integers"
            )
        point_count = len(volumes)
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
    if full_fresh and checkpoints[-1] > DEFAULT_CHECKPOINTS[-1]:
        raise ValueError(
            f"fresh-run checkpoint cap is {DEFAULT_CHECKPOINTS[-1]} draws"
        )
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
    minimum_family_size = max(4, point_count) if full_fresh else 4
    config["comparison_points"] = config.get(
        "comparison_points", minimum_family_size
    )
    if (
        type(config["comparison_points"]) is not int
        or config["comparison_points"] < minimum_family_size
    ):
        raise ValueError(
            "comparison_points must be at least the number of declared family points"
        )
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


def _building_block_specs(config):
    if "building_blocks" in config:
        return [
            (block, config["volumes_by_building_block"][block])
            for block in config["building_blocks"]
        ]
    return [(config["building_block"], config["volumes"])]


def _source_snapshot(project, building_blocks):
    revision = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=project, text=True
    ).strip()
    dirty = subprocess.check_output(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=project,
        text=True,
    ).strip()
    plan = (
        project
        / "src/main/resources/scenarios/use_case_1/uc1_operational_plan.json"
    )
    paths = [plan]
    for block, _ in building_blocks:
        paths.extend(
            path
            for path in (
                project
                / "src/main/resources/scenarios/use_case_1"
                / f"building_block_{block[-1]}"
            ).iterdir()
            if path.is_file()
        )
    hashes = {
        str(path.relative_to(project)): digest(path)
        for path in sorted(set(paths))
    }
    return revision, bool(dirty), hashes


def _utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _ensure_simulator_options():
    current = os.environ.get("MAVEN_OPTS", "").strip()
    if SIM_JVM_OPTIONS and SIM_JVM_OPTIONS not in current:
        current = f"{current} {SIM_JVM_OPTIONS}".strip()
    os.environ["MAVEN_OPTS"] = current


@contextmanager
def _stop_signal_handlers(stop_file):
    previous = {}

    def request_stop(_signum, _frame):
        stop_file.touch(exist_ok=True)

    for name in ("SIGINT", "SIGTERM"):
        signum = getattr(signal, name, None)
        if signum is not None:
            previous[signum] = signal.signal(signum, request_stop)
    try:
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def _environment(*, include_java=False, include_maven=False, project=None):
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
    if include_maven:
        if project is None:
            raise ValueError("project is required to record the Maven runtime")
        maven = subprocess.run(
            [str(Path(project) / "mvnw"), "-version"],
            cwd=project,
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        value["maven"] = (maven.stdout or maven.stderr).splitlines()[0]
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
    environment = _environment(
        include_java=True, include_maven=True, project=project
    )
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


def _new_fresh_progress(run_identity_sha256, config, building_blocks, mode):
    rows = {}
    for building_block, volumes in building_blocks:
        points = {}
        for volume in volumes:
            counts = {
                product: count
                for product, count in mode.composition(volume).items()
                if count
            }
            case = SamplingCase(str(volume), volume, "entry", counts)
            points[str(volume)] = {
                "volume": volume,
                "total_trains_per_hour": (
                    3600 * volume / config["period_seconds"]
                ),
                "class_counts": counts,
                "ordering_population_size": case.denominator,
                "status": "pending",
                "last_complete_checkpoint": 0,
                "active_checkpoint": None,
                "draws": 0,
                "unique_checked": 0,
                "unknown_draws": 0,
                "unknown_unique": 0,
                "interval": None,
                "stop_reason": None,
                "stop_time_utc": None,
                "admitted_unique_orderings": 0,
                "completed_unique_orderings": 0,
                "active_batch_requested_unique_orderings": 0,
                "active_batch_admitted_unique_orderings": 0,
                "active_batch_completed_unique_orderings": 0,
                "solver_attempts": 0,
                "solver_cpu_seconds": 0.0,
                "solver_task_wall_seconds": 0.0,
                "worker_pids": [],
                "worker_nice_values": [],
            }
        rows[building_block] = {
            "status": "pending",
            "calibration": None,
            "sampling_wall_seconds": 0.0,
            "active_worker_count": 0,
            "max_active_worker_count": 0,
            "active_worker_process_count": 0,
            "max_active_worker_process_count": 0,
            "points": points,
        }
    return {
        "schema_version": 1,
        "run_identity_sha256": run_identity_sha256,
        "status": "pending",
        "stop_reason": None,
        "created_at_utc": _utc_now(),
        "updated_at_utc": _utc_now(),
        "completed_at_utc": None,
        "invocation_count": 0,
        "totals": {
            "admitted_unique_orderings": 0,
            "completed_unique_orderings": 0,
            "unknown_unique_outcomes": 0,
            "solver_attempts": 0,
            "solver_cpu_seconds": 0.0,
            "solver_task_wall_seconds": 0.0,
            "worker_pids": [],
            "worker_nice_values": [],
            "max_active_worker_count": 0,
            "max_active_worker_process_count": 0,
        },
        "timings": {
            "wall_seconds": 0.0,
            "cpu_seconds": 0.0,
            "sampling_wall_seconds": 0.0,
            "estimated_remaining_unique_orderings_to_max": None,
            "estimated_remaining_solver_wall_seconds": None,
            "measured_unique_orders_per_wall_second": None,
            "eta_method": (
                "domain-only expected unseen ordering count to the maximum "
                "declared checkpoint divided by in-run completed-order throughput; "
                "not used for sampling or stopping"
            ),
        },
        "building_blocks": rows,
    }


def _sync_point_progress(block_state, report, checkpoints):
    partial = report.get("partial_checkpoint") or {}
    pending = partial.get("pending_ordering_ids_by_case", {})
    provider_stop = partial.get("provider_stop_reason")
    updated_at = _utc_now()
    max_checkpoint = checkpoints[-1]
    for case_row in report.get("cases", []):
        case = case_row["case"]
        point = block_state["points"][str(case["volume"])]
        point["last_complete_checkpoint"] = case_row["last_complete_checkpoint"]
        summaries = case_row["checkpoints"]
        summary = summaries[-1] if summaries else None
        if summary is not None:
            point["draws"] = summary["draw_count"]
            point["unique_checked"] = summary["unique_checked"]
            point["unknown_draws"] = summary["draw_outcomes"]["unknown"]
            point["unknown_unique"] = summary["unique_outcomes"]["unknown"]
            point["interval"] = dict(summary["interval"])
            point["precision_met"] = summary["precision_met"]
        else:
            point["precision_met"] = False
        if case_row["stopped"]:
            point["status"] = "precision_reached"
            point["stop_reason"] = case_row["stop_reason"]
        elif case["key"] in pending:
            point["status"] = "precision_not_reached"
            point["stop_reason"] = (
                "stop_requested"
                if provider_stop == "stop_requested"
                else provider_stop or "checkpoint_batch_incomplete"
            )
            point["active_checkpoint"] = case_row[
                "last_complete_checkpoint"
            ]
        elif case_row["last_complete_checkpoint"] >= max_checkpoint:
            point["status"] = "precision_not_reached"
            point["stop_reason"] = "precision_not_reached_at_max_checkpoint"
            point["active_checkpoint"] = None
        else:
            point["status"] = "sampling"
            point["stop_reason"] = None
            point["active_checkpoint"] = case_row[
                "last_complete_checkpoint"
            ]
        if case_row["stopped"]:
            point["active_checkpoint"] = None
        point["active_batch_requested_unique_orderings"] = 0
        point["active_batch_admitted_unique_orderings"] = 0
        point["active_batch_completed_unique_orderings"] = 0
        if point["stop_reason"] is not None and point["stop_time_utc"] is None:
            point["stop_time_utc"] = (
                summary.get("completed_at_utc")
                if summary
                and point["stop_reason"]
                in (
                    "precision_target_met",
                    "precision_not_reached_at_max_checkpoint",
                )
                else updated_at
            )
        point["updated_at_utc"] = updated_at


def _save_text(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(value)
    temporary.replace(path)


def _result_rows(config, building_blocks, reports, progress):
    rows = []
    for building_block, volumes in building_blocks:
        cases = {
            case_row["case"]["volume"]: case_row
            for case_row in reports.get(building_block, {}).get("cases", [])
        }
        for volume in volumes:
            case_row = cases.get(volume)
            summary = (
                case_row["checkpoints"][-1]
                if case_row and case_row["checkpoints"]
                else None
            )
            point = progress["building_blocks"][building_block]["points"][
                str(volume)
            ]
            interval = summary["interval"] if summary else None
            rows.append(
                {
                    "building_block": building_block,
                    "mode": config["mode"],
                    "volume": volume,
                    "total_trains_per_hour": (
                        3600 * volume / config["period_seconds"]
                    ),
                    "counts": (
                        case_row["case"]["class_counts"]
                        if case_row
                        else point["class_counts"]
                    ),
                    "ordering_population_size": (
                        case_row["case"]["denominator"]
                        if case_row
                        else point["ordering_population_size"]
                    ),
                    "checkpoint": summary["checkpoint"] if summary else 0,
                    "draws": summary["draw_count"] if summary else 0,
                    "unique_checked": (
                        summary["unique_checked"] if summary else 0
                    ),
                    "unknown_draws": (
                        summary["draw_outcomes"]["unknown"] if summary else 0
                    ),
                    "unknown_unique": (
                        summary["unique_outcomes"]["unknown"] if summary else 0
                    ),
                    "interval": interval,
                    "stop_reason": point["stop_reason"] or "not_attempted",
                    "stop_time_utc": point["stop_time_utc"],
                    "solver_cpu_seconds": point["solver_cpu_seconds"],
                    "solver_task_wall_seconds": point[
                        "solver_task_wall_seconds"
                    ],
                    "status": point["status"],
                }
            )
    return rows


def _format_rate(rate):
    return f"{rate:g}"


def _fresh_results_markdown(config, building_blocks, reports, progress):
    rows = _result_rows(config, building_blocks, reports, progress)
    by_block = {
        building_block: [
            row for row in rows if row["building_block"] == building_block
        ]
        for building_block, _ in building_blocks
    }
    columns = " | ".join(block for block, _ in building_blocks)
    header = f"| Mode | {columns} |"
    separator = "| --- | " + " | ".join("---" for _ in building_blocks) + " |"
    matrix_cells = []
    for building_block, _ in building_blocks:
        details = []
        for row in by_block[building_block]:
            if row["interval"] is None:
                details.append(
                    f'{_format_rate(row["total_trains_per_hour"])}/h: '
                    "not attempted"
                )
                continue
            lower = 100 * row["interval"]["lower"]
            upper = 100 * row["interval"]["upper"]
            details.append(
                f'{_format_rate(row["total_trains_per_hour"])}/h: '
                f'{lower:.2f}-{upper:.2f}% '
                f'(n={row["draws"]}, unique={row["unique_checked"]}, '
                f'unknown={row["unknown_draws"]}, {row["stop_reason"]})'
            )
        matrix_cells.append("<br>".join(details))
    lines = [
        f'# UC1 {config["mode"]} fresh fixed-profile results',
        "",
        header,
        separator,
        f'| {config["mode"]} | ' + " | ".join(matrix_cells) + " |",
        "",
        (
            "These are fresh fixed-profile solver classifications, not "
            "simulator-validated capacity. Intervals are "
            "[L(F,n), H(F+U,n)]; display endpoints are rounded to two decimals "
            "only, while stopping uses unrounded values. M="
            f'{config["comparison_points"]}, checkpoints '
            f'{", ".join(map(str, config["checkpoints"]))}, target total width '
            f'{config["target_width"]:.0%}. Unknown draws widen the upper endpoint. '
            "No feasibility census, saved ordering labels, or prior capacity "
            "classification was used to generate or stop this run."
        ),
        "",
        "## Per-rate intervals and timing",
        "",
        "| BB | Total/hour | FV/RV/GV | Population | Checkpoint | Draws | Unique | Unknown draws/unique | 95% family interval | Stop reason | Stop time (UTC) | Solver CPU sum (s) | Solver task wall sum (s) |",
        "| --- | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- | ---: | ---: |",
    ]
    for row in rows:
        counts = row["counts"]
        mix = "/".join(str(counts.get(product, 0)) for product in ("FV", "RV", "GV"))
        interval = (
            "not sampled"
            if row["interval"] is None
            else (
                f'{100 * row["interval"]["lower"]:.2f}-'
                f'{100 * row["interval"]["upper"]:.2f}%'
            )
        )
        lines.append(
            "| "
            + " | ".join(
                (
                    row["building_block"],
                    _format_rate(row["total_trains_per_hour"]),
                    mix,
                    str(row["ordering_population_size"]),
                    str(row["checkpoint"]),
                    str(row["draws"]),
                    str(row["unique_checked"]),
                    f'{row["unknown_draws"]}/{row["unknown_unique"]}',
                    interval,
                    row["stop_reason"],
                    row["stop_time_utc"] or "not stopped",
                    f'{row["solver_cpu_seconds"]:.3f}',
                    f'{row["solver_task_wall_seconds"]:.3f}',
                )
            )
            + " |"
        )
    lines.extend(["", "## Mac execution"])
    for building_block, _ in building_blocks:
        stage = progress["building_blocks"][building_block]
        calibration = stage.get("calibration") or {}
        lines.append(
            f'- **{building_block}:** calibration '
            f'{calibration.get("wall_seconds", 0):.2f}s wall / '
            f'{calibration.get("cpu_seconds", 0):.2f}s CPU; total '
            f'{stage.get("wall_seconds", 0):.2f}s wall / '
            f'{stage.get("cpu_seconds", 0):.2f}s CPU; '
            f'{stage.get("max_active_worker_process_count", 0)} max active '
            "worker processes."
        )
    lines.append(
        "- **Run total:** "
        f'{progress["timings"]["wall_seconds"]:.2f}s wall / '
        f'{progress["timings"]["cpu_seconds"]:.2f}s CPU; '
        f'{progress["totals"]["completed_unique_orderings"]} unique solver '
        "orderings completed. CPU is process plus child CPU; solver task wall "
        "sums may exceed wall time because workers run concurrently."
    )
    lines.extend(
        [
            "",
            "## Overlay",
            "",
            (
                "The SVG overlay is line-only: lower/upper interval endpoints "
                "for each building block, with no markers, extrapolation, or "
                "connections across missing adjacent rates. It shows fixed-profile "
                "solver feasibility, not simulator-validated capacity."
            ),
            "",
        ]
    )
    return "\n".join(lines)


def _fresh_results_svg(config, building_blocks, reports):
    width, height = 900, 520
    left, right, top, bottom = 78, 28, 54, 72
    plot_width = width - left - right
    plot_height = height - top - bottom
    rates = [
        3600 * volume / config["period_seconds"]
        for _, volumes in building_blocks
        for volume in volumes
    ]
    x_min, x_max = min(rates), max(rates)
    if x_min == x_max:
        x_max = x_min + 1

    def x_for(rate):
        return left + (rate - x_min) / (x_max - x_min) * plot_width

    def y_for(fraction):
        return top + (1 - fraction) * plot_height

    colors = {"BB1": "#0072B2", "BB2": "#D55E00", "BB3": "#009E73"}
    lines = [
        (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" '
            f'height="{height}" viewBox="0 0 {width} {height}" '
            'role="img" aria-labelledby="title description">'
        ),
        "<title id=\"title\">UC1 fixed-profile feasibility interval overlay</title>",
        (
            '<desc id="description">Line-only lower and upper confidence '
            "interval endpoints for the fresh UC1 MAINLINE/FV_PASS sweep; "
            "no markers, extrapolation, or missing-gap bridging.</desc>"
        ),
        '<rect width="100%" height="100%" fill="white"/>',
        (
            f'<text x="{width / 2:g}" y="25" text-anchor="middle" '
            'font-family="sans-serif" font-size="16">'
            f'UC1 {config["mode"]}: fixed-profile solver feasibility</text>'
        ),
    ]
    for tick in range(0, 101, 20):
        y = y_for(tick / 100)
        lines.append(
            f'<path d="M {left} {y:.2f} H {width - right}" '
            'stroke="#dddddd" stroke-width="1"/>'
        )
        lines.append(
            f'<text x="{left - 10}" y="{y + 4:.2f}" text-anchor="end" '
            'font-family="sans-serif" font-size="11">'
            f"{tick}%</text>"
        )
    lines.extend(
        [
            (
                f'<path d="M {left} {top} V {height - bottom} '
                f'H {width - right}" fill="none" stroke="#333333"/>'
            ),
            (
                f'<text x="{left + plot_width / 2:g}" y="{height - 24}" '
                'text-anchor="middle" font-family="sans-serif" font-size="12">'
                "Total trains/hour</text>"
            ),
            (
                f'<text transform="translate(20 {top + plot_height / 2:g}) '
                'rotate(-90)" text-anchor="middle" font-family="sans-serif" '
                'font-size="12">Feasible share interval endpoint</text>'
            ),
        ]
    )
    for rate in sorted(set(rates)):
        x = x_for(rate)
        lines.append(
            f'<path d="M {x:.2f} {top} V {height - bottom}" '
            'stroke="#f0f0f0" stroke-width="1"/>'
        )
        lines.append(
            f'<text x="{x:.2f}" y="{height - bottom + 18}" '
            'text-anchor="middle" font-family="sans-serif" font-size="10">'
            f"{_format_rate(rate)}</text>"
        )
    legend_y = height - 7
    legend_x = left
    for building_block, volumes in building_blocks:
        report_cases = {
            row["case"]["volume"]: row
            for row in reports.get(building_block, {}).get("cases", [])
        }
        ordered = []
        for volume in volumes:
            row = report_cases.get(volume)
            summary = (
                row["checkpoints"][-1]
                if row and row["checkpoints"]
                else None
            )
            ordered.append(
                {
                    "volume": volume,
                    "rate": 3600 * volume / config["period_seconds"],
                    "lower": (
                        summary["interval"]["lower"] if summary else None
                    ),
                    "upper": (
                        summary["interval"]["upper"] if summary else None
                    ),
                }
            )
        for endpoint, dash in (("lower", None), ("upper", "5 4")):
            segment = []
            previous_volume = None

            def flush_segment():
                if len(segment) < 2:
                    return
                commands = []
                for index, point in enumerate(segment):
                    command = "M" if index == 0 else "L"
                    commands.append(
                        f'{command} {x_for(point["rate"]):.2f} '
                        f'{y_for(point[endpoint]):.2f}'
                    )
                dash_attr = (
                    f' stroke-dasharray="{dash}"' if dash else ""
                )
                lines.append(
                    f'<path d="{" ".join(commands)}" fill="none" '
                    f'stroke="{colors[building_block]}" stroke-width="2"'
                    f'{dash_attr}/>'
                )

            for point in ordered:
                if point[endpoint] is None:
                    flush_segment()
                    segment = []
                    previous_volume = None
                    continue
                if (
                    previous_volume is not None
                    and point["volume"] != previous_volume + 1
                ):
                    flush_segment()
                    segment = []
                segment.append(point)
                previous_volume = point["volume"]
            flush_segment()
        if legend_x > width - 190:
            legend_x = left
            legend_y -= 20
        lines.append(
            f'<path d="M {legend_x} {legend_y - 4} h 22" '
            f'stroke="{colors[building_block]}" stroke-width="2"/>'
        )
        lines.append(
            f'<path d="M {legend_x + 28} {legend_y - 4} h 22" '
            f'stroke="{colors[building_block]}" stroke-width="2" '
            'stroke-dasharray="5 4"/>'
        )
        lines.append(
            f'<text x="{legend_x + 54}" y="{legend_y}" '
            'font-family="sans-serif" font-size="11">'
            f"{building_block} lower / upper</text>"
        )
        legend_x += 170
    lines.append("</svg>")
    return "\n".join(lines) + "\n"


def _run_fresh_sweep(args, project, config, output):
    if not args.state_root:
        raise ValueError("--state-root is required for fresh-run")
    if not 1 <= args.workers <= 2:
        raise ValueError("fresh-run supports one or two worker processes globally")
    if not 0 < args.first_seconds <= 600 or not 0 < args.retry_seconds <= 600:
        raise ValueError("solver attempt limits must be in (0,600]")
    building_blocks = _building_block_specs(config)
    mode, plan_path = _mode(project, config)
    source_revision, source_dirty, source_hashes = _source_snapshot(
        project, building_blocks
    )
    if source_dirty:
        raise ValueError("fresh-run requires a committed, clean source revision")
    _ensure_simulator_options()
    environment = _environment(include_java=True)
    nice_value = os.nice(0) if hasattr(os, "nice") else None
    run_identity = {
        "schema_version": 1,
        "kind": "fresh_adaptive_flexibility_sweep",
        "config": config,
        "domain": {
            "use_case": "UC1",
            "mode": config["mode"],
            "period_seconds": config["period_seconds"],
            "volume_grid_by_building_block": {
                block: volumes for block, volumes in building_blocks
            },
            "total_trains_per_hour_by_building_block": {
                block: [
                    3600 * volume / config["period_seconds"]
                    for volume in volumes
                ]
                for block, volumes in building_blocks
            },
            "total_family_points": config["comparison_points"],
        },
        "source_revision": source_revision,
        "source_dirty": False,
        "source_hashes": source_hashes,
        "state_root": str(args.state_root.expanduser().resolve()),
        "environment": environment,
        "runtime": {
            "java_home": os.environ.get("JAVA_HOME"),
            "maven_opts": os.environ.get("MAVEN_OPTS", ""),
            "simulator_jvm_options": SIM_JVM_OPTIONS,
            "nice_value": nice_value,
        },
        "calibration": {
            "kind": "fresh isolated profile simulations, once per variant and BB",
            "seed": config["solver_seed"],
            "per_simulation_wall_cap_seconds": 300,
            "max_simulator_calls": 120,
            "simulator_calls_after_calibration": 0,
        },
        "solver": {
            "seed": config["solver_seed"],
            "worker_processes_global_max": args.workers,
            "cp_sat_threads_per_process": 1,
            "first_attempt_wall_seconds": args.first_seconds,
            "unknown_retry_wall_seconds": args.retry_seconds,
            "deterministic_time_equivalent": False,
            "policy": (
                "gap 1 first; retry UNKNOWN at the same gap; try gap 0 only "
                "after gap-1 INFEASIBLE; only gap-0 INFEASIBLE is conditional_no"
            ),
            "bb_execution_order": [block for block, _ in building_blocks],
            "simulator_calls_after_calibration": 0,
        },
        "sampling": {
            "seed": config["seed"],
            "checkpoints": config["checkpoints"],
            "alpha": config["alpha"],
            "target_total_width": config["target_width"],
            "comparison_points": config["comparison_points"],
            "per_endpoint_alpha": (
                config["alpha"]
                / (2 * config["comparison_points"] * len(config["checkpoints"]))
            ),
            "draw_method": (
                "uniform cyclic multiset order draws with replacement; "
                "cache only exact order results and retain draw multiplicity"
            ),
            "prior_feasibility_results_used": False,
        },
    }
    run_identity_sha256 = identity_digest(run_identity)
    identity_path = output / "run-identity.json"
    progress_path = output / "progress.json"
    stop_file = output / "STOP"
    if identity_path.exists():
        if not args.resume:
            raise ValueError(
                "fresh-run output already has an identity; pass --resume to continue"
            )
        stored_identity = json.loads(identity_path.read_text())
        if (
            stored_identity.get("identity_sha256") != run_identity_sha256
            or stored_identity.get("identity") != run_identity
        ):
            raise ValueError(
                "fresh-run identity changed; use a new output directory"
            )
    else:
        if args.resume:
            raise ValueError("--resume requires an existing fresh-run identity")
        if stop_file.exists():
            raise ValueError("remove the stale STOP file before a new fresh-run")
        stages_path = output / "stages"
        if progress_path.exists() or (
            stages_path.is_dir() and any(stages_path.iterdir())
        ):
            raise ValueError(
                "fresh-run output contains data without an identity; use a new directory"
            )
        save(
            identity_path,
            {
                "schema_version": 1,
                "identity": run_identity,
                "identity_sha256": run_identity_sha256,
            },
        )
    if progress_path.exists():
        progress = json.loads(progress_path.read_text())
        if progress.get("run_identity_sha256") != run_identity_sha256:
            raise ValueError("progress file does not match the fresh-run identity")
    else:
        stages_path = output / "stages"
        if args.resume and stages_path.is_dir() and any(stages_path.iterdir()):
            raise ValueError(
                "progress file is missing for existing stage data; use the original output"
            )
        progress = _new_fresh_progress(
            run_identity_sha256, config, building_blocks, mode
        )
    if args.resume and stop_file.exists():
        stop_file.unlink()
    progress["status"] = "running"
    progress["stop_reason"] = None
    progress["completed_at_utc"] = None
    progress["invocation_count"] = progress.get("invocation_count", 0) + 1
    progress["invocation_started_at_utc"] = _utc_now()
    for building_block, _ in building_blocks:
        block_state = progress["building_blocks"][building_block]
        if block_state["status"] not in ("complete", "precision_not_reached"):
            block_state["status"] = "pending"

    started_wall = time.monotonic()
    started_cpu = cpu_seconds()
    previous_wall = progress["timings"].get("wall_seconds", 0.0)
    previous_cpu = progress["timings"].get("cpu_seconds", 0.0)
    previous_sampling_wall = progress["timings"].get(
        "sampling_wall_seconds", 0.0
    )
    active_sampling_started = None
    active_sampling_building_block = None
    progress["invocation_admitted_unique_orderings"] = 0
    progress["invocation_completed_unique_orderings"] = 0
    last_progress_write = time.monotonic()
    progress_events_since_write = 0

    def update_timings():
        now = time.monotonic()
        progress["timings"]["wall_seconds_this_invocation"] = now - started_wall
        progress["timings"]["cpu_seconds_this_invocation"] = max(
            0.0, cpu_seconds() - started_cpu
        )
        progress["timings"]["wall_seconds"] = (
            previous_wall + progress["timings"]["wall_seconds_this_invocation"]
        )
        progress["timings"]["cpu_seconds"] = (
            previous_cpu + progress["timings"]["cpu_seconds_this_invocation"]
        )
        progress["timings"]["sampling_wall_seconds"] = (
            previous_sampling_wall
            + (
                max(0.0, now - active_sampling_started)
                if active_sampling_started is not None
                else 0.0
            )
        )
        for building_block, _ in building_blocks:
            block_state = progress["building_blocks"][building_block]
            if block_state["status"] == "sampling":
                calibration = block_state.get("calibration") or {}
                active_elapsed = (
                    max(0.0, now - active_sampling_started)
                    if active_sampling_building_block == building_block
                    and active_sampling_started is not None
                    else 0.0
                )
                block_state["wall_seconds"] = (
                    calibration.get("wall_seconds", 0.0)
                    + block_state["sampling_wall_seconds"]
                    + active_elapsed
                )
                block_state["cpu_seconds"] = (
                    calibration.get("cpu_seconds", 0.0)
                    + sum(
                        point["solver_cpu_seconds"]
                        for point in block_state["points"].values()
                    )
                )
        completed = progress["totals"]["completed_unique_orderings"]
        sampling_wall = progress["timings"]["sampling_wall_seconds"]
        throughput = completed / sampling_wall if sampling_wall > 0 else None
        remaining_unique = 0.0
        maximum_checkpoint = config["checkpoints"][-1]
        for building_block, _ in building_blocks:
            for point in progress["building_blocks"][building_block][
                "points"
            ].values():
                if point["status"] in (
                    "precision_reached",
                    "precision_not_reached",
                ):
                    if point["last_complete_checkpoint"] >= maximum_checkpoint or point[
                        "status"
                    ] == "precision_reached":
                        continue
                population = point["ordering_population_size"]
                checkpoint = (
                    point["active_checkpoint"]
                    or point["last_complete_checkpoint"]
                )
                remaining_draws = max(0, maximum_checkpoint - checkpoint)
                admitted = point["admitted_unique_orderings"]
                completed_for_point = point["completed_unique_orderings"]
                pending = max(
                    admitted - completed_for_point,
                    point["active_batch_requested_unique_orderings"]
                    - point["active_batch_completed_unique_orderings"],
                    0,
                )
                seen_after_batch = max(
                    admitted,
                    admitted
                    - point["active_batch_admitted_unique_orderings"]
                    + point["active_batch_requested_unique_orderings"],
                )
                unseen = max(0, population - seen_after_batch)
                expected_fraction_seen = (
                    -math.expm1(
                        remaining_draws * math.log1p(-1 / population)
                    )
                    if population > 1 and remaining_draws
                    else (1.0 if population == 1 and remaining_draws else 0.0)
                )
                remaining_unique += pending + unseen * expected_fraction_seen
        progress["timings"]["estimated_remaining_unique_orderings_to_max"] = (
            remaining_unique
            if throughput is not None
            else None
        )
        progress["timings"]["measured_unique_orders_per_wall_second"] = (
            throughput
        )
        progress["timings"]["estimated_remaining_solver_wall_seconds"] = (
            remaining_unique / throughput
            if throughput is not None and throughput > 0
            else None
        )

    def write_progress():
        nonlocal last_progress_write, progress_events_since_write
        progress["updated_at_utc"] = _utc_now()
        update_timings()
        save(progress_path, progress)
        last_progress_write = time.monotonic()
        progress_events_since_write = 0

    def record_progress_event(force=False):
        nonlocal progress_events_since_write
        progress_events_since_write += 1
        if (
            force
            or progress_events_since_write >= 25
            or time.monotonic() - last_progress_write >= 5
        ):
            write_progress()

    write_progress()
    stage_contexts = {}
    settings = Settings.load(project, args.state_root)
    invocation = ["railsim-adaptive-flexibility", "fresh-run"]
    workflow_config_base = {
        "use_case": "UC1",
        "mode": config["mode"],
        "period_seconds": config["period_seconds"],
        "seed": config["solver_seed"],
        "workers": 1,
        "solver_seconds": args.retry_seconds,
        "rounds": 0,
        "proposals": 1,
        "runner": "none",
        "limits": {"simulations": 120, "model_calls": 0},
    }

    def verify_frozen_source():
        revision, dirty, hashes = _source_snapshot(project, building_blocks)
        if revision != source_revision or dirty or hashes != source_hashes:
            raise ValueError(
                "source revision or scenario inputs changed during fresh-run"
            )

    try:
        with _stop_signal_handlers(stop_file):
            for building_block, volumes in building_blocks:
                verify_frozen_source()
                stage_dir = output / "stages" / building_block
                stage_dir.mkdir(parents=True, exist_ok=True)
                block_state = progress["building_blocks"][building_block]
                block_state["status"] = "calibrating"
                block_state["calibration_started_at_utc"] = (
                    block_state.get("calibration_started_at_utc") or _utc_now()
                )
                write_progress()
                workflow_config = dict(
                    workflow_config_base,
                    building_block=building_block,
                    volumes=list(volumes),
                )
                workflow = Workflow(
                    settings,
                    workflow_config,
                    stage_dir / "calibration-workflow",
                    invocation,
                    minimum_volume=1,
                )
                try:
                    reference = workflow.calibrate()
                except Exhausted as exc:
                    workflow.persist()
                    block_state["status"] = "calibration_budget_exhausted"
                    block_state["calibration_error"] = str(exc)
                    progress["status"] = "calibration_budget_exhausted"
                    progress["stop_reason"] = str(exc)
                    write_progress()
                    return _finish_fresh_run(
                        output,
                        run_identity,
                        run_identity_sha256,
                        config,
                        building_blocks,
                        progress,
                        {},
                    )
                workflow.persist()
                calibration_budget = dict(
                    workflow.report.get("budget", {})
                )
                calibration_sha256 = identity_digest(reference)
                model_identity_sha256 = identity_digest(workflow.identity)
                source = (
                    project
                    / "src/main/resources/scenarios/use_case_1"
                    / f"building_block_{building_block[-1]}"
                )
                entries = {}
                for volume in volumes:
                    counts = {
                        product: count
                        for product, count in mode.composition(volume).items()
                        if count
                    }
                    groups = order_groups(
                        "UC1", source, counts, reference["windows"]
                    )
                    if len(groups) != 1:
                        raise ValueError(
                            "UC1 adaptive sampling expects one shared entry"
                        )
                    entries[volume] = next(iter(groups))
                block_config = dict(
                    config,
                    building_block=building_block,
                    volumes=list(volumes),
                )
                block_config.pop("building_blocks", None)
                block_config.pop("volumes_by_building_block", None)
                cases = _make_cases(block_config, mode, entries)
                provider = None
                sampler = None

                def on_admit(task):
                    volume = task["case"]["volume"]
                    point = block_state["points"][str(volume)]
                    point["admitted_unique_orderings"] += 1
                    progress["totals"]["admitted_unique_orderings"] += 1
                    progress["invocation_admitted_unique_orderings"] += 1
                    point["active_batch_requested_unique_orderings"] = (
                        provider.last_batch_requested_by_volume[volume]
                    )
                    point["active_batch_admitted_unique_orderings"] = (
                        provider.last_batch_admitted_by_volume[volume]
                    )
                    point["active_batch_completed_unique_orderings"] = (
                        provider.last_batch_completed_by_volume[volume]
                    )
                    if sampler is not None:
                        case_state = sampler.state["cases"][str(volume)]
                        point["last_complete_checkpoint"] = case_state[
                            "last_complete_checkpoint"
                        ]
                        point["active_checkpoint"] = next(
                            (
                                checkpoint
                                for checkpoint in config["checkpoints"]
                                if checkpoint
                                > case_state["last_complete_checkpoint"]
                            ),
                            None,
                        )
                    block_state["active_worker_count"] = (
                        provider.active_worker_count
                    )
                    block_state["active_worker_process_count"] = (
                        provider.active_worker_process_count
                    )
                    block_state["max_active_worker_count"] = max(
                        block_state["max_active_worker_count"],
                        provider.max_active_worker_count,
                    )
                    block_state["max_active_worker_process_count"] = max(
                        block_state["max_active_worker_process_count"],
                        provider.max_active_worker_process_count,
                    )
                    progress["totals"]["max_active_worker_count"] = max(
                        progress["totals"]["max_active_worker_count"],
                        provider.max_active_worker_count,
                    )
                    progress["totals"][
                        "max_active_worker_process_count"
                    ] = max(
                        progress["totals"][
                            "max_active_worker_process_count"
                        ],
                        provider.max_active_worker_process_count,
                    )
                    record_progress_event(
                        force=progress["invocation_admitted_unique_orderings"]
                        == 1
                    )

                def on_result(task, result):
                    volume = task["case"]["volume"]
                    point = block_state["points"][str(volume)]
                    point["completed_unique_orderings"] += 1
                    point["solver_attempts"] += result.get(
                        "attempt_count", 0
                    )
                    point["solver_cpu_seconds"] += result.get(
                        "cpu_seconds", 0.0
                    )
                    point["solver_task_wall_seconds"] += result.get(
                        "provider_wall_seconds",
                        result.get("wall_seconds", 0.0),
                    )
                    point["active_batch_requested_unique_orderings"] = (
                        provider.last_batch_requested_by_volume[volume]
                    )
                    point["active_batch_admitted_unique_orderings"] = (
                        provider.last_batch_admitted_by_volume[volume]
                    )
                    point["active_batch_completed_unique_orderings"] = (
                        provider.last_batch_completed_by_volume[volume]
                    )
                    if result["outcome"] == "unknown":
                        point["unknown_unique"] += 1
                        progress["totals"]["unknown_unique_outcomes"] += 1
                    pid = result.get("worker_pid")
                    if isinstance(pid, int) and pid not in point["worker_pids"]:
                        point["worker_pids"].append(pid)
                    worker_nice = result.get("worker_nice")
                    if (
                        worker_nice is not None
                        and worker_nice not in point["worker_nice_values"]
                    ):
                        point["worker_nice_values"].append(worker_nice)
                    progress["totals"]["completed_unique_orderings"] += 1
                    progress["invocation_completed_unique_orderings"] += 1
                    progress["totals"]["solver_attempts"] += result.get(
                        "attempt_count", 0
                    )
                    progress["totals"]["solver_cpu_seconds"] += result.get(
                        "cpu_seconds", 0.0
                    )
                    progress["totals"]["solver_task_wall_seconds"] += result.get(
                        "provider_wall_seconds",
                        result.get("wall_seconds", 0.0),
                    )
                    if isinstance(pid, int) and pid not in progress["totals"][
                        "worker_pids"
                    ]:
                        progress["totals"]["worker_pids"].append(pid)
                    if (
                        worker_nice is not None
                        and worker_nice
                        not in progress["totals"]["worker_nice_values"]
                    ):
                        progress["totals"]["worker_nice_values"].append(
                            worker_nice
                        )
                    block_state["active_worker_count"] = (
                        provider.active_worker_count
                    )
                    block_state["active_worker_process_count"] = (
                        provider.active_worker_process_count
                    )
                    block_state["max_active_worker_count"] = max(
                        block_state["max_active_worker_count"],
                        provider.max_active_worker_count,
                    )
                    block_state["max_active_worker_process_count"] = max(
                        block_state["max_active_worker_process_count"],
                        provider.max_active_worker_process_count,
                    )
                    progress["totals"]["max_active_worker_count"] = max(
                        progress["totals"]["max_active_worker_count"],
                        provider.max_active_worker_count,
                    )
                    progress["totals"][
                        "max_active_worker_process_count"
                    ] = max(
                        progress["totals"][
                            "max_active_worker_process_count"
                        ],
                        provider.max_active_worker_process_count,
                    )
                    record_progress_event(
                        force=progress["invocation_completed_unique_orderings"]
                        == 1
                    )

                provider = LocalFixedProfileSolverProvider(
                    reference,
                    period=config["period_seconds"],
                    seed=config["solver_seed"],
                    workers=args.workers,
                    first_seconds=args.first_seconds,
                    retry_seconds=args.retry_seconds,
                    deadline=None,
                    active_wall_limit_seconds=None,
                    stop_file=stop_file,
                    on_admit=on_admit,
                    model_identity_sha256=model_identity_sha256,
                    calibration_sha256=calibration_sha256,
                    on_result=on_result,
                )
                manifest = _case_manifest(
                    project,
                    block_config,
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
                            "building_block_point_count": (
                                len(config["volumes_by_building_block"][building_block])
                                if "volumes_by_building_block" in config
                                else len(config["volumes"])
                            ),
                            "total_family_points": config[
                                "comparison_points"
                            ],
                        },
                        "fresh_model": {
                            "source_revision": source_revision,
                            "model_identity_sha256": model_identity_sha256,
                            "calibration_reference_sha256": calibration_sha256,
                            "calibration_simulator_calls": calibration_budget.get(
                                "simulations", 0
                            ),
                            "solver_seed": config["solver_seed"],
                            "worker_processes_global_max": args.workers,
                            "cp_sat_threads_per_process": 1,
                            "first_attempt_wall_seconds": args.first_seconds,
                            "unknown_retry_wall_seconds": args.retry_seconds,
                            "deterministic_time_equivalent": False,
                            "prior_feasibility_results_used": False,
                        },
                    },
                )
                save(stage_dir / "case-manifest.json", manifest)
                if block_state["calibration"] is None:
                    block_state["calibration"] = {
                        "status": "complete",
                        "completed_at_utc": _utc_now(),
                        "wall_seconds": calibration_budget.get(
                            "wall_seconds", 0.0
                        ),
                        "cpu_seconds": calibration_budget.get(
                            "cpu_seconds", 0.0
                        ),
                        "simulator_calls": calibration_budget.get(
                            "simulations", 0
                        ),
                        "calibration_reference_sha256": calibration_sha256,
                        "model_identity_sha256": model_identity_sha256,
                    }
                block_state["status"] = "calibrated"
                block_state["calibration_completed_at_utc"] = _utc_now()
                if stop_file.exists():
                    progress["status"] = "stopped"
                    progress["stop_reason"] = "stop_requested_during_calibration"
                    write_progress()
                    return _finish_fresh_run(
                        output,
                        run_identity,
                        run_identity_sha256,
                        config,
                        building_blocks,
                        progress,
                        {},
                    )
                sampler = AdaptiveSampler(
                    cases,
                    provider,
                    stage_dir / "sampling",
                    seed=config["seed"],
                    checkpoints=config["checkpoints"],
                    alpha=config["alpha"],
                    target_width=config["target_width"],
                    comparison_points=config["comparison_points"],
                )
                stage_identity = {
                    "schema_version": 1,
                    "run_identity_sha256": run_identity_sha256,
                    "building_block": building_block,
                    "volumes": list(volumes),
                    "model_identity_sha256": model_identity_sha256,
                    "calibration_reference_sha256": calibration_sha256,
                    "provider_identity": provider.identity,
                    "adaptive_identity": sampler.identity,
                    "adaptive_identity_sha256": sampler.identity_sha256,
                }
                stage_identity_path = stage_dir / "stage-identity.json"
                if stage_identity_path.exists():
                    stored_stage = json.loads(stage_identity_path.read_text())
                    if stored_stage != stage_identity:
                        raise ValueError(
                            f"{building_block} stage identity changed; use a new output directory"
                        )
                else:
                    save(stage_identity_path, stage_identity)
                block_state["status"] = "calibrated"
                stage_contexts[building_block] = {
                    "stage_dir": stage_dir,
                    "workflow": workflow,
                    "reference": reference,
                    "provider": provider,
                    "sampler": sampler,
                    "cases": cases,
                    "calibration_budget": calibration_budget,
                }
                write_progress()
                if stop_file.exists():
                    progress["status"] = "stopped"
                    progress["stop_reason"] = "stop_requested_during_calibration"
                    write_progress()
                    return _finish_fresh_run(
                        output,
                        run_identity,
                        run_identity_sha256,
                        config,
                        building_blocks,
                        progress,
                        {},
                    )

            verify_frozen_source()
            for building_block, _ in building_blocks:
                if stop_file.exists():
                    progress["status"] = "stopped"
                    progress["stop_reason"] = "stop_requested_before_sampling"
                    break
                context = stage_contexts[building_block]
                block_state = progress["building_blocks"][building_block]
                sampler = context["sampler"]
                provider = context["provider"]
                for case in context["cases"]:
                    case_state = sampler.state["cases"][case.key]
                    point = block_state["points"][str(case.volume)]
                    point["last_complete_checkpoint"] = case_state[
                        "last_complete_checkpoint"
                    ]
                    point["active_checkpoint"] = case_state[
                        "last_complete_checkpoint"
                    ]
                    point["active_batch_requested_unique_orderings"] = 0
                    point["active_batch_admitted_unique_orderings"] = 0
                    point["active_batch_completed_unique_orderings"] = 0
                block_state["status"] = "sampling"
                write_progress()
                active_sampling_started = time.monotonic()
                active_sampling_building_block = building_block
                try:
                    sampling_report = sampler.run()
                finally:
                    elapsed_sampling = max(
                        0.0, time.monotonic() - active_sampling_started
                    )
                    previous_sampling_wall += elapsed_sampling
                    active_sampling_started = None
                    active_sampling_building_block = None
                    context["workflow"].persist()
                    block_state["sampling_wall_seconds"] += elapsed_sampling
                    block_state["wall_seconds"] = context["workflow"].report.get(
                        "budget", {}
                    ).get("wall_seconds", 0.0)
                    block_state["cpu_seconds"] = context["workflow"].report.get(
                        "budget", {}
                    ).get("cpu_seconds", 0.0)
                _sync_point_progress(
                    block_state, sampling_report, config["checkpoints"]
                )
                block_state["active_worker_count"] = 0
                block_state["active_worker_process_count"] = 0
                block_state["sampling_report_sha256"] = digest(
                    context["stage_dir"]
                    / "sampling"
                    / "adaptive-report.json"
                )
                unfinished = [
                    row
                    for row in sampling_report["cases"]
                    if not row["stopped"]
                ]
                incomplete_batch = sampling_report.get("partial_checkpoint")
                if stop_file.exists() and incomplete_batch:
                    block_state["status"] = "stopped"
                    progress["status"] = "stopped"
                    progress["stop_reason"] = (
                        incomplete_batch.get("provider_stop_reason")
                        or "stop_requested"
                    )
                elif unfinished:
                    block_state["status"] = "precision_not_reached"
                else:
                    block_state["status"] = "complete"
                write_progress()
                if progress["status"] == "stopped":
                    break
            if progress["status"] != "stopped":
                progress["status"] = "complete"
                progress["stop_reason"] = None
                progress["completed_at_utc"] = _utc_now()
            return _finish_fresh_run(
                output,
                run_identity,
                run_identity_sha256,
                config,
                building_blocks,
                progress,
                {},
            )
    except (
        ValueError,
        OSError,
        RuntimeError,
        subprocess.SubprocessError,
    ) as exc:
        progress["status"] = "error"
        progress["stop_reason"] = f"{type(exc).__name__}: {exc}"
        write_progress()
        raise


def _finish_fresh_run(
    output,
    run_identity,
    run_identity_sha256,
    config,
    building_blocks,
    progress,
    reports,
):
    output = Path(output)
    for building_block, _ in building_blocks:
        report_path = (
            output
            / "stages"
            / building_block
            / "sampling"
            / "adaptive-report.json"
        )
        if report_path.exists():
            reports[building_block] = json.loads(report_path.read_text())
    rows = _result_rows(config, building_blocks, reports, progress)
    precision_reached = sum(
        1 for row in rows if row["stop_reason"] == "precision_target_met"
    )
    progress["precision_status"] = (
        "reached"
        if precision_reached == len(rows)
        else "not_reached_or_incomplete"
    )
    if progress["status"] == "complete":
        progress["completed_at_utc"] = progress.get(
            "completed_at_utc"
        ) or _utc_now()
    progress["updated_at_utc"] = _utc_now()
    save(output / "progress.json", progress)
    markdown_path = output / "fv-pass-results.md"
    overlay_path = output / "fv-pass-overlay.svg"
    _save_text(
        markdown_path,
        _fresh_results_markdown(config, building_blocks, reports, progress),
    )
    _save_text(
        overlay_path,
        _fresh_results_svg(config, building_blocks, reports),
    )
    report = {
        "schema_version": 1,
        "status": progress["status"],
        "stop_reason": progress.get("stop_reason"),
        "precision_status": progress["precision_status"],
        "run_identity_sha256": run_identity_sha256,
        "source_revision": run_identity["source_revision"],
        "sampling_family_points": config["comparison_points"],
        "precision_reached_points": precision_reached,
        "requested_points": len(rows),
        "execution": {
            "wall_seconds": progress["timings"]["wall_seconds"],
            "cpu_seconds": progress["timings"]["cpu_seconds"],
            "sampling_wall_seconds": progress["timings"][
                "sampling_wall_seconds"
            ],
            "estimated_remaining_solver_wall_seconds": progress["timings"][
                "estimated_remaining_solver_wall_seconds"
            ],
            "measured_unique_orders_per_wall_second": progress["timings"][
                "measured_unique_orders_per_wall_second"
            ],
            "admitted_unique_orderings": progress["totals"][
                "admitted_unique_orderings"
            ],
            "completed_unique_orderings": progress["totals"][
                "completed_unique_orderings"
            ],
            "unknown_unique_outcomes": progress["totals"][
                "unknown_unique_outcomes"
            ],
            "solver_attempts": progress["totals"]["solver_attempts"],
            "solver_cpu_seconds": progress["totals"][
                "solver_cpu_seconds"
            ],
            "solver_task_wall_seconds": progress["totals"][
                "solver_task_wall_seconds"
            ],
            "worker_pids": progress["totals"]["worker_pids"],
            "worker_nice_values": progress["totals"]["worker_nice_values"],
            "max_active_worker_count": progress["totals"][
                "max_active_worker_count"
            ],
            "max_active_worker_process_count": progress["totals"][
                "max_active_worker_process_count"
            ],
            "worker_processes_global_max": run_identity["solver"][
                "worker_processes_global_max"
            ],
            "cp_sat_threads_per_process": 1,
            "per_building_block": {
                building_block: progress["building_blocks"][building_block]
                for building_block, _ in building_blocks
            },
        },
        "artifacts": {
            "status": "progress.json",
            "results_markdown": markdown_path.name,
            "overlay_svg": overlay_path.name,
            "building_block_reports": {
                building_block: (
                    f"stages/{building_block}/sampling/adaptive-report.json"
                    if building_block in reports
                    else None
                )
                for building_block, _ in building_blocks
            },
        },
        "results": rows,
    }
    save(output / "run-report.json", report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "mode", choices=("replay", "fresh-pilot", "fresh-run")
    )
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
    parser.add_argument(
        "--active-wall-limit-seconds",
        type=float,
        default=900,
        help="fresh-pilot only; fresh-run has no 15-minute active-wall cap",
    )
    parser.add_argument("--first-seconds", type=float, default=0.546)
    parser.add_argument("--retry-seconds", type=float, default=61.96)
    parser.add_argument(
        "--resume",
        action="store_true",
        help="resume a fresh-run with its immutable identity and saved checkpoints",
    )
    args = parser.parse_args()
    project = args.project.expanduser().resolve()
    output = args.output.expanduser().resolve()
    try:
        if args.resume and args.mode != "fresh-run":
            raise ValueError("--resume is supported only by fresh-run")
        config = _load_experiment_config(
            args.config, full_fresh=args.mode == "fresh-run"
        )
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
            elif args.mode == "fresh-run":
                report = _run_fresh_sweep(args, project, config, output)
            else:
                report = _run_fresh_pilot(args, project, config, output)
        print(json.dumps(report, indent=2, sort_keys=True))
    except (
        ValueError,
        OSError,
        RuntimeError,
        subprocess.SubprocessError,
    ) as exc:
        parser.exit(2, f"adaptive-flexibility: {exc}\n")


if __name__ == "__main__":
    main()
