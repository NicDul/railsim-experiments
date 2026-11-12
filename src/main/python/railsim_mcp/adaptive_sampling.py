"""Checkpointed sampling of cyclic entry-order feasibility."""

from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from dataclasses import dataclass
import hashlib
import json
import math
import multiprocessing
from pathlib import Path
import random
import sys
import time

from .flexibility import (
    _ordering_id,
    canonical_rotation,
    necklace_count,
    sample_necklace,
)
from .capacity import save as _capacity_save
from .solver import solve


DEFAULT_CHECKPOINTS = (100, 200, 400, 800, 1600, 3200, 6400)
OUTCOMES = frozenset(("feasible", "conditional_no", "unknown"))


def _json_bytes(value):
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
        + "\n"
    ).encode()


def identity_digest(value):
    return hashlib.sha256(_json_bytes(value)).hexdigest()


def _sampler_code_identity():
    source = Path(__file__)
    flexibility_source = source.with_name("flexibility.py")
    return {
        "adaptive_sampling_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "necklace_sampler_sha256": hashlib.sha256(
            flexibility_source.read_bytes()
        ).hexdigest(),
    }


def _save_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    _capacity_save(path, value)


@dataclass(frozen=True)
class SamplingCase:
    key: str
    volume: int
    entry: str
    class_counts: dict
    denominator: int | None = None

    def __post_init__(self):
        if not isinstance(self.key, str) or not self.key:
            raise ValueError("case key must be a non-empty string")
        if type(self.volume) is not int or self.volume < 1:
            raise ValueError("volume must be a positive integer")
        if not isinstance(self.entry, str) or not self.entry:
            raise ValueError("entry must be a non-empty string")
        if not isinstance(self.class_counts, dict) or not self.class_counts:
            raise ValueError("class_counts must be a non-empty object")
        if any(
            not isinstance(name, str)
            or not name
            or type(count) is not int
            or count < 0
            for name, count in self.class_counts.items()
        ):
            raise ValueError("class_counts must map class names to nonnegative integers")
        if sum(self.class_counts.values()) != self.volume:
            raise ValueError("class counts must sum to volume")
        computed = necklace_count(self.class_counts)
        if self.denominator is not None and self.denominator != computed:
            raise ValueError("denominator does not match the multiset composition")
        object.__setattr__(self, "denominator", computed)

    def record(self):
        return {
            "key": self.key,
            "volume": self.volume,
            "entry": self.entry,
            "class_counts": dict(sorted(self.class_counts.items())),
            "denominator": self.denominator,
        }


def draw_orderings(case, seed, count):
    if type(seed) is not int or seed < 0:
        raise ValueError("seed must be a nonnegative integer")
    if type(count) is not int or count < 0:
        raise ValueError("draw count must be a nonnegative integer")
    rng = random.Random(seed + case.volume)
    return [
        tuple(sample_necklace(case.class_counts, rng))
        for _ in range(count)
    ]


def ordering_id(case, ordering):
    return _ordering_id(
        {case.entry: list(canonical_rotation(ordering))}
    )


def _continued_fraction_beta(a, b, x):
    qab = a + b
    qap = a + 1
    qam = a - 1
    tiny = 1e-300
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < tiny:
        d = tiny
    d = 1.0 / d
    value = d
    for iteration in range(1, 201):
        twice = 2 * iteration
        numerator = (
            iteration * (b - iteration) * x
            / ((qam + twice) * (a + twice))
        )
        d = 1.0 + numerator * d
        if abs(d) < tiny:
            d = tiny
        c = 1.0 + numerator / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        value *= d * c

        numerator = (
            -(a + iteration)
            * (qab + iteration)
            * x
            / ((a + twice) * (qap + twice))
        )
        d = 1.0 + numerator * d
        if abs(d) < tiny:
            d = tiny
        c = 1.0 + numerator / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        delta = d * c
        value *= delta
        if abs(delta - 1.0) <= 3e-14:
            return value
    raise ArithmeticError("incomplete beta continued fraction did not converge")


def _regularized_beta(x, a, b):
    if not 0 <= x <= 1 or a <= 0 or b <= 0:
        raise ValueError("invalid incomplete beta arguments")
    if x == 0:
        return 0.0
    if x == 1:
        return 1.0
    log_term = (
        math.lgamma(a + b)
        - math.lgamma(a)
        - math.lgamma(b)
        + a * math.log(x)
        + b * math.log1p(-x)
    )
    term = math.exp(log_term) if log_term > -745 else 0.0
    if x < (a + 1) / (a + b + 2):
        value = term * _continued_fraction_beta(a, b, x) / a
    else:
        value = 1.0 - term * _continued_fraction_beta(b, a, 1.0 - x) / b
    return min(1.0, max(0.0, value))


def _beta_quantile_lower(probability, a, b):
    if not 0 < probability < 1 or a <= 0 or b <= 0:
        raise ValueError("invalid beta quantile arguments")
    low, high = 0.0, 1.0
    for _ in range(90):
        middle = low + (high - low) / 2
        if middle == low or middle == high:
            break
        if _regularized_beta(middle, a, b) < probability:
            low = middle
        else:
            high = middle
    return low


def one_sided_binomial_lower(successes, trials, alpha):
    if (
        type(trials) is not int
        or type(successes) is not int
        or trials < 1
        or not 0 <= successes <= trials
        or not 0 < alpha < 1
    ):
        raise ValueError("invalid one-sided binomial lower-bound arguments")
    if successes == 0:
        return 0.0
    if successes == trials:
        value = math.exp(math.log(alpha) / trials)
        return max(0.0, math.nextafter(value, -math.inf))
    value = _beta_quantile_lower(alpha, successes, trials - successes + 1)
    return max(0.0, math.nextafter(value, -math.inf))


def one_sided_binomial_upper(successes, trials, alpha):
    if (
        type(trials) is not int
        or type(successes) is not int
        or trials < 1
        or not 0 <= successes <= trials
        or not 0 < alpha < 1
    ):
        raise ValueError("invalid one-sided binomial upper-bound arguments")
    if successes == trials:
        return 1.0
    if successes == 0:
        value = -math.expm1(math.log(alpha) / trials)
        return min(1.0, math.nextafter(value, math.inf))
    complement = _beta_quantile_lower(alpha, trials - successes, successes + 1)
    return min(1.0, math.nextafter(1.0 - complement, math.inf))


def conservative_interval(
    feasible,
    unknown,
    trials,
    *,
    alpha=0.05,
    comparison_points=4,
    checkpoints=DEFAULT_CHECKPOINTS,
):
    if (
        type(feasible) is not int
        or type(unknown) is not int
        or type(trials) is not int
        or trials < 1
        or feasible < 0
        or unknown < 0
        or feasible + unknown > trials
    ):
        raise ValueError("invalid outcome counts")
    if not 0 < alpha < 1:
        raise ValueError("alpha must be in (0,1)")
    if type(comparison_points) is not int or comparison_points < 1:
        raise ValueError("comparison_points must be a positive integer")
    if trials not in checkpoints:
        raise ValueError("interval requested at an undeclared checkpoint")
    per_endpoint_alpha = alpha / (
        2 * comparison_points * len(checkpoints)
    )
    lower = one_sided_binomial_lower(feasible, trials, per_endpoint_alpha)
    upper = one_sided_binomial_upper(
        feasible + unknown, trials, per_endpoint_alpha
    )
    return {
        "lower": lower,
        "upper": upper,
        "width": upper - lower,
        "confidence": 1 - alpha,
        "per_endpoint_alpha": per_endpoint_alpha,
        "method": (
            "one-sided exact Clopper-Pearson endpoints with Bonferroni "
            "allocation over all declared points and checkpoints; unknown "
            "draws widen only the upper endpoint"
        ),
    }


def _validate_plan(cases, checkpoints, alpha, target_width, comparison_points):
    cases = tuple(cases)
    if not cases or len({case.key for case in cases}) != len(cases):
        raise ValueError("cases must have unique keys")
    if len({case.volume for case in cases}) != len(cases):
        raise ValueError("volumes must be unique")
    if (
        not isinstance(checkpoints, (tuple, list))
        or not checkpoints
        or any(type(n) is not int or n < 1 for n in checkpoints)
        or list(checkpoints) != sorted(set(checkpoints))
    ):
        raise ValueError("checkpoints must be strictly increasing positive integers")
    if type(comparison_points) is not int or comparison_points < len(cases):
        raise ValueError("comparison_points cannot be smaller than the planned cases")
    if not 0 < alpha < 1:
        raise ValueError("alpha must be in (0,1)")
    if not 0 < target_width <= 1:
        raise ValueError("target_width must be in (0,1]")
    return cases


class _ResultCache:
    def __init__(self, directory, identity_sha256):
        self.directory = Path(directory)
        self.identity_sha256 = identity_sha256

    def path(self, case, ordering_key):
        return self.directory / str(case.volume) / f"{ordering_key}.json"

    def load(self, case, ordering_key, ordering):
        path = self.path(case, ordering_key)
        if not path.exists():
            return None
        data = json.loads(path.read_text())
        if (
            data.get("identity_sha256") != self.identity_sha256
            or data.get("case_key") != case.key
            or data.get("volume") != case.volume
            or data.get("ordering_id") != ordering_key
            or data.get("ordering") != {case.entry: list(ordering)}
            or data.get("outcome") not in OUTCOMES
        ):
            raise ValueError(f"cached result identity mismatch: {path.name}")
        return data["provider_result"]

    def save(self, case, ordering_key, ordering, provider_result):
        outcome = provider_result.get("outcome")
        if outcome not in OUTCOMES:
            raise ValueError("provider result must have a valid outcome")
        _save_json(
            self.path(case, ordering_key),
            {
                "schema_version": 1,
                "identity_sha256": self.identity_sha256,
                "case_key": case.key,
                "volume": case.volume,
                "ordering_id": ordering_key,
                "ordering": {case.entry: list(ordering)},
                "outcome": outcome,
                "provider_result": provider_result,
            },
        )


def _task_cache_result(cache, case, draw):
    return cache.load(case, draw["ordering_id"], draw["ordering"])


def _cache_tasks(provider, cache, tasks, case_by_key):
    requested = {
        (row["case"]["key"], row["ordering_id"]): row for row in tasks
    }
    seen = set()
    for task, result in provider.evaluate_batch(tasks):
        case = case_by_key[task["case"]["key"]]
        task_key = (case.key, task["ordering_id"])
        if task_key in seen or task_key not in requested:
            raise ValueError("provider returned a duplicate or unrequested task")
        if task != requested[task_key]:
            raise ValueError("provider changed a requested task")
        seen.add(task_key)
        cache.save(
            case,
            task["ordering_id"],
            task["ordering"][case.entry],
            result,
        )
        on_result = getattr(provider, "on_result", None)
        if on_result is not None:
            on_result(task, result)


def _summarize_draws(case, draws, cache, *, alpha, checkpoints, target_width, comparison_points):
    results = [
        _task_cache_result(cache, case, draw)
        for draw in draws
    ]
    if any(result is None for result in results):
        raise ValueError("cannot summarize an incomplete draw batch")
    outcomes = Counter(result["outcome"] for result in results)
    unique = {}
    budgets = {}
    sources = {}
    source_observations = {}
    for draw, result in zip(draws, results):
        unique.setdefault(draw["ordering_id"], result)
        budget = result.get("budget_label", "unlabelled")
        source = result.get("source", "unlabelled")
        budgets.setdefault(budget, Counter())[result["outcome"]] += 1
        budgets[budget]["draws"] += 1
        sources.setdefault(source, Counter())[result["outcome"]] += 1
        sources[source]["draws"] += 1
        observations = result.get("source_history", [result])
        for observation in observations:
            name = observation.get("source", "unlabelled")
            row = source_observations.setdefault(
                name,
                {
                    "budget_label": observation.get(
                        "budget_label", "unlabelled"
                    ),
                    "outcomes": Counter(),
                },
            )
            row["outcomes"][observation["outcome"]] += 1
            row["outcomes"]["draws"] += 1
    unique_outcomes = Counter(result["outcome"] for result in unique.values())
    unique_provider_wall = sum(
        result.get("provider_wall_seconds", result.get("wall_seconds", 0.0))
        for result in unique.values()
        if isinstance(
            result.get("provider_wall_seconds", result.get("wall_seconds")),
            (int, float),
        )
    )
    unique_source_lookup_wall = sum(
        result.get("source_lookup_wall_seconds", 0.0)
        for result in unique.values()
        if isinstance(result.get("source_lookup_wall_seconds"), (int, float))
    )
    feasible = outcomes["feasible"]
    unknown = outcomes["unknown"]
    interval = conservative_interval(
        feasible,
        unknown,
        len(draws),
        alpha=alpha,
        comparison_points=comparison_points,
        checkpoints=checkpoints,
    )
    return {
        "checkpoint": len(draws),
        "draw_count": len(draws),
        "unique_checked": len(unique),
        "draw_outcomes": {
            "feasible": outcomes["feasible"],
            "conditional_no": outcomes["conditional_no"],
            "unknown": outcomes["unknown"],
        },
        "unique_outcomes": {
            "feasible": unique_outcomes["feasible"],
            "conditional_no": unique_outcomes["conditional_no"],
            "unknown": unique_outcomes["unknown"],
        },
        "budget_draws": {
            key: dict(value)
            for key, value in sorted(budgets.items())
        },
        "source_draws": {
            key: dict(value)
            for key, value in sorted(sources.items())
        },
        "source_observations": {
            key: {
                "budget_label": value["budget_label"],
                "draws": dict(value["outcomes"]),
            }
            for key, value in sorted(source_observations.items())
        },
        "unique_provider_wall_seconds": unique_provider_wall,
        "unique_source_lookup_wall_seconds": unique_source_lookup_wall,
        "definite_feasible_fraction": feasible / len(draws),
        "possible_feasible_fraction": (feasible + unknown) / len(draws),
        "point_estimate": feasible / len(draws) if unknown == 0 else None,
        "interval": interval,
        "precision_target_width": target_width,
        "precision_met": interval["width"] <= target_width,
    }


class AdaptiveSampler:
    """Run fixed-checkpoint adaptive sampling through a result provider."""

    def __init__(
        self,
        cases,
        provider,
        output,
        *,
        seed,
        checkpoints=DEFAULT_CHECKPOINTS,
        alpha=0.05,
        target_width=0.10,
        comparison_points=4,
    ):
        self.cases = _validate_plan(
            cases, checkpoints, alpha, target_width, comparison_points
        )
        if type(seed) is not int or seed < 0:
            raise ValueError("seed must be a nonnegative integer")
        self.provider = provider
        self.output = Path(output)
        self.output.mkdir(parents=True, exist_ok=True)
        self.seed = seed
        self.checkpoints = tuple(checkpoints)
        self.alpha = alpha
        self.target_width = target_width
        self.comparison_points = comparison_points
        self.plan = {
            "seed": seed,
            "python_version": sys.version.split()[0],
            "checkpoints": list(self.checkpoints),
            "alpha": alpha,
            "target_width": target_width,
            "comparison_points": comparison_points,
            "cases": [case.record() for case in self.cases],
            "draw_method": (
                "uniform multiset linear draw with stabilizer/length "
                "rejection and canonical cyclic rotation"
            ),
        }
        self.identity = {
            "sampler_version": 1,
            "sampler_code": _sampler_code_identity(),
            "plan": self.plan,
            "provider": provider.identity,
        }
        self.identity_sha256 = identity_digest(self.identity)
        self.state_path = self.output / "adaptive-state.json"
        self.report_path = self.output / "adaptive-report.json"
        self.cache = _ResultCache(
            self.output / "order-results", self.identity_sha256
        )
        self.case_by_key = {case.key: case for case in self.cases}
        self.state = self._load_or_create_state()

    def _load_or_create_state(self):
        if self.state_path.exists():
            state = json.loads(self.state_path.read_text())
            if (
                state.get("identity_sha256") != self.identity_sha256
                or state.get("identity") != self.identity
            ):
                raise ValueError(
                    "adaptive sampling identity changed; use a new output directory"
                )
            return state
        state = {
            "schema_version": 1,
            "identity": self.identity,
            "identity_sha256": self.identity_sha256,
            "status": "pending",
            "stop_reason": None,
            "partial_checkpoint": None,
            "cases": {
                case.key: {
                    "volume": case.volume,
                    "last_complete_checkpoint": 0,
                    "stopped": False,
                    "stop_reason": None,
                    "checkpoints": [],
                }
                for case in self.cases
            },
        }
        _save_json(self.state_path, state)
        return state

    def _draws(self, case, count):
        return [
            {
                "ordering_id": ordering_id(case, ordering),
                "ordering": list(ordering),
            }
            for ordering in draw_orderings(case, self.seed, count)
        ]

    def _report(self):
        report = {
            "schema_version": 1,
            "status": self.state["status"],
            "stop_reason": self.state.get("stop_reason"),
            "partial_checkpoint": self.state.get("partial_checkpoint"),
            "identity_sha256": self.identity_sha256,
            "identity": self.identity,
            "cases": [
                {
                    "case": case.record(),
                    **self.state["cases"][case.key],
                }
                for case in self.cases
            ],
        }
        _save_json(self.report_path, report)
        return report

    def run(self):
        self.state["status"] = "running"
        self.state["stop_reason"] = None
        self.state["partial_checkpoint"] = None
        _save_json(self.state_path, self.state)
        for checkpoint in self.checkpoints:
            active = [
                case
                for case in self.cases
                if not self.state["cases"][case.key]["stopped"]
                and self.state["cases"][case.key]["last_complete_checkpoint"]
                < checkpoint
            ]
            if not active:
                continue
            planned = {case.key: self._draws(case, checkpoint) for case in active}
            tasks = []
            for case in active:
                unique = {}
                for draw in planned[case.key]:
                    unique.setdefault(draw["ordering_id"], draw["ordering"])
                for key, ordering in unique.items():
                    if self.cache.load(case, key, ordering) is None:
                        tasks.append(
                            {
                                "case": case.record(),
                                "ordering_id": key,
                                "ordering": {case.entry: ordering},
                            }
                        )
            _cache_tasks(self.provider, self.cache, tasks, self.case_by_key)
            pending = {}
            for case in active:
                unique = {
                    draw["ordering_id"]: draw["ordering"]
                    for draw in planned[case.key]
                }
                missing = [
                    key
                    for key, ordering in unique.items()
                    if self.cache.load(case, key, ordering) is None
                ]
                if missing:
                    pending[case.key] = missing
            if pending:
                completed = {
                    case.key: len(
                        {
                            draw["ordering_id"]
                            for draw in planned[case.key]
                            if self.cache.load(
                                case, draw["ordering_id"], draw["ordering"]
                            )
                            is not None
                        }
                    )
                    for case in active
                }
                self.state["status"] = "precision_not_reached"
                self.state["stop_reason"] = "checkpoint_batch_incomplete"
                self.state["partial_checkpoint"] = {
                    "checkpoint": checkpoint,
                    "planned_draws_by_case": {
                        case.key: checkpoint for case in active
                    },
                    "completed_unique_by_case": completed,
                    "required_unique_by_case": {
                        case.key: len(
                            {
                                draw["ordering_id"]
                                for draw in planned[case.key]
                            }
                        )
                        for case in active
                    },
                    "pending_ordering_ids_by_case": pending,
                    "provider_stop_reason": getattr(
                        self.provider, "last_batch_stop_reason", None
                    ),
                    "last_complete_checkpoint_by_case": {
                        case.key: self.state["cases"][case.key][
                            "last_complete_checkpoint"
                        ]
                        for case in active
                    },
                }
                _save_json(self.state_path, self.state)
                return self._report()
            for case in active:
                summary = _summarize_draws(
                    case,
                    planned[case.key],
                    self.cache,
                    alpha=self.alpha,
                    checkpoints=self.checkpoints,
                    target_width=self.target_width,
                    comparison_points=self.comparison_points,
                )
                case_state = self.state["cases"][case.key]
                case_state["checkpoints"].append(summary)
                case_state["last_complete_checkpoint"] = checkpoint
                if summary["precision_met"]:
                    case_state["stopped"] = True
                    case_state["stop_reason"] = "precision_target_met"
            _save_json(self.state_path, self.state)
        unfinished = [
            case.key
            for case in self.cases
            if not self.state["cases"][case.key]["stopped"]
        ]
        if unfinished:
            self.state["status"] = "precision_not_reached"
            self.state["stop_reason"] = "maximum_declared_checkpoint_reached"
        else:
            self.state["status"] = "precision_reached"
            self.state["stop_reason"] = "all_points_met_precision"
        _save_json(self.state_path, self.state)
        return self._report()


def run_fixed_pilot(
    cases,
    provider,
    output,
    *,
    seed,
    draws_per_case,
):
    """Evaluate one predeclared timing batch without making a precision claim."""
    cases = tuple(cases)
    if not cases or len({case.key for case in cases}) != len(cases):
        raise ValueError("pilot cases must have unique keys")
    if type(seed) is not int or seed < 0:
        raise ValueError("seed must be a nonnegative integer")
    if type(draws_per_case) is not int or draws_per_case < 1:
        raise ValueError("draws_per_case must be a positive integer")
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    identity = {
        "sampler_version": 1,
        "python_version": sys.version.split()[0],
        "sampler_code": _sampler_code_identity(),
        "pilot": {
            "seed": seed,
            "draws_per_case": draws_per_case,
            "cases": [case.record() for case in cases],
        },
        "provider": provider.identity,
    }
    identity_sha256 = identity_digest(identity)
    cache = _ResultCache(output / "order-results", identity_sha256)
    plan_path = output / "pilot-plan.json"
    if plan_path.exists():
        plan = json.loads(plan_path.read_text())
        if (
            plan.get("identity") != identity
            or plan.get("identity_sha256") != identity_sha256
        ):
            raise ValueError("pilot identity changed; use a new output directory")
    else:
        plan = {
            "schema_version": 1,
            "identity": identity,
            "identity_sha256": identity_sha256,
            "cases": {
                case.key: [
                    {
                        "ordering_id": ordering_id(case, ordering),
                        "ordering": list(ordering),
                    }
                    for ordering in draw_orderings(
                        case, seed, draws_per_case
                    )
                ]
                for case in cases
            },
        }
        _save_json(plan_path, plan)
    case_by_key = {case.key: case for case in cases}
    tasks = []
    for case in cases:
        unique = {}
        for draw in plan["cases"][case.key]:
            unique.setdefault(draw["ordering_id"], draw["ordering"])
        for key, ordering in unique.items():
            if cache.load(case, key, ordering) is None:
                tasks.append(
                    {
                        "case": case.record(),
                        "ordering_id": key,
                        "ordering": {case.entry: ordering},
                    }
                )
    _cache_tasks(provider, cache, tasks, case_by_key)
    summaries = []
    for case in cases:
        draws = plan["cases"][case.key]
        results = [
            cache.load(case, draw["ordering_id"], draw["ordering"])
            for draw in draws
        ]
        completed = [result for result in results if result is not None]
        counts = Counter(result["outcome"] for result in completed)
        unique_ids = {draw["ordering_id"] for draw in draws}
        unique_results = {}
        for draw, result in zip(draws, results):
            if result is not None:
                unique_results.setdefault(draw["ordering_id"], result)
        pending = sorted(unique_ids - set(unique_results))
        wall = [
            result.get("wall_seconds")
            for result in unique_results.values()
            if isinstance(result.get("wall_seconds"), (int, float))
        ]
        cpu = [
            result.get("cpu_seconds")
            for result in unique_results.values()
            if isinstance(result.get("cpu_seconds"), (int, float))
        ]
        summaries.append(
            {
                "case": case.record(),
                "planned_draws": len(draws),
                "completed_draws": len(completed),
                "unique_orderings": len(unique_ids),
                "unique_checked": len(unique_results),
                "pending_unique_orderings": len(pending),
                "pending_ordering_ids": pending,
                "draw_outcomes": {
                    "feasible": counts["feasible"],
                    "conditional_no": counts["conditional_no"],
                    "unknown": counts["unknown"],
                },
                "unique_wall_seconds": wall,
                "unique_cpu_seconds": cpu,
                "mean_wall_seconds_per_unique": (
                    sum(wall) / len(wall) if wall else None
                ),
                "mean_cpu_seconds_per_unique": (
                    sum(cpu) / len(cpu) if cpu else None
                ),
            }
        )
    complete = all(not row["pending_ordering_ids"] for row in summaries)
    report = {
        "schema_version": 1,
        "status": "complete" if complete else "budget_limited",
        "identity": identity,
        "identity_sha256": identity_sha256,
        "precision_not_reached": True,
        "precision_reason": (
            "fixed-size timing pilot is not a declared confidence checkpoint"
        ),
        "cases": summaries,
        "provider_stop_reason": getattr(
            provider, "last_batch_stop_reason", None
        ),
    }
    _save_json(output / "pilot-results.json", report)
    return report


@dataclass(frozen=True)
class SolverResultSource:
    name: str
    directory: Path
    budget_label: str
    identity: dict

    def __post_init__(self):
        if not self.name or not self.budget_label:
            raise ValueError("source name and budget label are required")
        if not Path(self.directory).is_dir():
            raise ValueError(f"solver result directory is missing: {self.directory}")


class SavedSolverResultProvider:
    """Read solver results by volume and ordering ID, retaining source budgets."""

    def __init__(
        self,
        sources,
        *,
        model_identity_sha256,
        corpus_identity=None,
    ):
        self.sources = tuple(sources)
        if not self.sources or len({source.name for source in self.sources}) != len(
            self.sources
        ):
            raise ValueError("saved result sources must have unique names")
        if not model_identity_sha256:
            raise ValueError("model_identity_sha256 is required")
        self.model_identity_sha256 = model_identity_sha256
        self.corpus_identity = corpus_identity or {}
        self.identity = {
            "kind": "saved_solver_results",
            "model_identity_sha256": model_identity_sha256,
            "corpus_identity": self.corpus_identity,
            "sources": [
                {
                    "name": source.name,
                    "budget_label": source.budget_label,
                    "identity": source.identity,
                }
                for source in self.sources
            ],
            "merge_policy": (
                "prefer the highest-precedence sound feasible/no classification; "
                "retain unknown only when no source has a sound classification"
            ),
        }
        self.last_batch_stop_reason = None

    def entry_for_volume(self, volume):
        for source in reversed(self.sources):
            directory = Path(source.directory) / str(volume)
            for path in sorted(directory.glob("*.json")) if directory.exists() else ():
                record = json.loads(path.read_text())
                ordering = record.get("ordering")
                if isinstance(ordering, dict) and len(ordering) == 1:
                    return next(iter(ordering))
        raise FileNotFoundError(f"no saved orderings for volume {volume}")

    def _read_record(self, source, task):
        case = task["case"]
        path = (
            Path(source.directory)
            / str(case["volume"])
            / f'{task["ordering_id"]}.json'
        )
        if not path.exists():
            return None
        record_bytes = path.read_bytes()
        record = json.loads(record_bytes)
        expected_ordering = task["ordering"]
        if (
            record.get("ordering_id") != task["ordering_id"]
            or record.get("volume") != case["volume"]
            or record.get("ordering") != expected_ordering
        ):
            raise ValueError(f"saved ordering identity mismatch: {path.name}")
        if record.get("identity_sha256") != self.model_identity_sha256:
            raise ValueError(f"saved model identity mismatch: {path.name}")
        raw_status = record.get("status")
        solver_status = record.get("solver_status")
        gap = record.get("solver_model_gap")
        trials = record.get("trials", [])
        attempts = [
            {
                "min_gap": trial.get("min_gap"),
                "status": trial.get("solver", {}).get("status"),
            }
            for trial in trials
            if isinstance(trial, dict)
        ]
        error = None
        if raw_status == "solver_feasible":
            if (
                solver_status in ("FEASIBLE", "OPTIMAL")
                and record.get("slot_pattern")
            ):
                outcome = "feasible"
            else:
                outcome = "unknown"
                error = "feasible record lacks a validated solver witness"
        elif raw_status == "conditional_no":
            zero_gap_infeasible = (
                gap == 0 and solver_status == "INFEASIBLE"
            ) or any(
                attempt.get("min_gap") == 0
                and attempt.get("status") == "INFEASIBLE"
                for attempt in attempts
            )
            if zero_gap_infeasible:
                outcome = "conditional_no"
            else:
                outcome = "unknown"
                error = "no classification lacks zero-gap INFEASIBLE evidence"
        elif raw_status == "unresolved":
            outcome = "unknown"
        else:
            outcome = "unknown"
            error = f"unrecognized saved status: {raw_status!r}"
        return {
            "outcome": outcome,
            "source": source.name,
            "budget_label": source.budget_label,
            "record_sha256": hashlib.sha256(record_bytes).hexdigest(),
            "execution_identity_sha256": record.get(
                "execution_identity_sha256"
            ),
            "raw_status": raw_status,
            "solver_status": solver_status,
            "solver_model_gap": gap,
            "attempts": attempts,
            "record_validation_error": error,
            "unresolved_reason": record.get("unresolved_reason"),
            "execution_status": record.get("execution_status"),
        }

    def evaluate(self, task):
        started = time.monotonic()
        matches = []
        for source in self.sources:
            result = self._read_record(source, task)
            if result is not None:
                matches.append(result)
        if not matches:
            raise FileNotFoundError(
                "saved result missing for sampled ordering "
                + task["ordering_id"]
            )
        classified = [
            result for result in matches if result["outcome"] != "unknown"
        ]
        if classified:
            outcomes = {result["outcome"] for result in classified}
            if len(outcomes) != 1:
                raise ValueError(
                    "saved sources disagree on a sound ordering classification: "
                    + task["ordering_id"]
                )
            selected = classified[-1]
        else:
            selected = matches[-1]
        elapsed = time.monotonic() - started
        return {
            **selected,
            "matched_sources": [result["source"] for result in matches],
            "source_history": matches,
            "provider_wall_seconds": elapsed,
            "source_lookup_wall_seconds": elapsed,
        }

    def evaluate_batch(self, tasks):
        self.last_batch_stop_reason = None
        for task in tasks:
            yield task, self.evaluate(task)


def _solve_fixed_order(task, windows, variants, period, seed, first_seconds, retry_seconds):
    started_wall = time.monotonic()
    started_cpu = time.process_time()
    ordering_id_value = task["ordering_id"]
    entry, products = next(iter(task["ordering"].items()))
    options = {
        product: sorted(
            variant
            for variant, info in variants.items()
            if info["product"] == product
        )
        for product in set(products)
    }
    attempts = []
    final = None
    witness_hash = None
    unresolved_reason = None
    for attempt_index, margin in enumerate((1, 0)):
        solve_seed = (
            seed + int(ordering_id_value[:8], 16) + attempt_index
        ) % 2147483647
        attempt_cpu = time.process_time()
        trial = solve(
            products,
            options,
            windows,
            first_seconds,
            frozenset(),
            1,
            min_gap=margin,
            period=period,
            seed=solve_seed,
            order=products,
            entry=entry,
        )
        first_cpu_seconds = time.process_time() - attempt_cpu
        attempts.append(
            {
                "min_gap": margin,
                "attempt": 1,
                "solver_seed": solve_seed,
                "status": trial.get("status", "MISSING"),
                "wall_seconds": trial.get("solve_seconds"),
                "cpu_seconds": first_cpu_seconds,
            }
        )
        if trial.get("status") == "UNKNOWN":
            attempt_cpu = time.process_time()
            retry = solve(
                products,
                options,
                windows,
                retry_seconds,
                frozenset(),
                1,
                min_gap=margin,
                period=period,
                seed=solve_seed,
                order=products,
                entry=entry,
            )
            retry_cpu_seconds = time.process_time() - attempt_cpu
            attempts.append(
                {
                    "min_gap": margin,
                    "attempt": 2,
                    "solver_seed": solve_seed,
                    "status": retry.get("status", "MISSING"),
                    "wall_seconds": retry.get("solve_seconds"),
                    "cpu_seconds": retry_cpu_seconds,
                }
            )
            trial = retry
        final = trial
        if margin == 1 and trial.get("status") == "INFEASIBLE":
            continue
        if margin == 0 and trial.get("status") == "INFEASIBLE":
            outcome = "conditional_no"
            break
        if trial.get("status") in ("FEASIBLE", "OPTIMAL"):
            pattern = trial.get("slot_pattern")
            if pattern and len(pattern) == len(products):
                outcome = "feasible"
                witness_hash = hashlib.sha256(_json_bytes(pattern)).hexdigest()
                break
            outcome = "unknown"
            witness_hash = None
            unresolved_reason = "feasible solver status lacks a complete slot pattern"
            break
        outcome = "unknown"
        witness_hash = None
        unresolved_reason = (
            "solver_" + str(trial.get("status", "missing")).lower()
            + "_after_bounded_attempts"
        )
        break
    else:
        outcome = "unknown"
        witness_hash = None
        unresolved_reason = "no conclusive solver result"
    total_wall = time.monotonic() - started_wall
    total_cpu = time.process_time() - started_cpu
    return {
        "outcome": outcome,
        "source": "fresh_solver",
        "budget_label": (
            f"wall-time attempts {first_seconds:g}s then "
            f"{retry_seconds:g}s on UNKNOWN"
        ),
        "solver_status": final.get("status") if final else "MISSING",
        "solver_model_gap": attempts[-1]["min_gap"] if attempts else None,
        "attempts": attempts,
        "attempt_count": len(attempts),
        "wall_seconds": total_wall,
        "provider_wall_seconds": total_wall,
        "cpu_seconds": total_cpu,
        "witness_sha256": witness_hash,
        "unresolved_reason": unresolved_reason,
        "simulator_calls": 0,
        "deterministic_time_equivalent": False,
    }


class LocalFixedProfileSolverProvider:
    """Solve sampled orders locally with at most two one-thread CP-SAT workers."""

    def __init__(
        self,
        reference,
        *,
        period,
        seed=4711,
        workers=2,
        first_seconds=0.546,
        retry_seconds=61.96,
        deadline=None,
        active_wall_limit_seconds=900,
        model_identity_sha256,
        calibration_sha256,
        on_result=None,
    ):
        if type(workers) is not int or not 1 <= workers <= 2:
            raise ValueError("local pilot supports one or two worker processes")
        if not 0 < first_seconds <= 600 or not 0 < retry_seconds <= 600:
            raise ValueError("solver attempt budgets must be in (0,600]")
        if type(seed) is not int or seed < 0:
            raise ValueError("solver seed must be nonnegative")
        self.reference = reference
        self.period = period
        self.seed = seed
        self.workers = workers
        self.first_seconds = first_seconds
        self.retry_seconds = retry_seconds
        self.deadline = deadline
        self.active_wall_limit_seconds = active_wall_limit_seconds
        self.on_result = on_result
        self.max_order_wall_seconds = 2 * (
            first_seconds + retry_seconds
        ) + 5.0
        self.last_batch_stop_reason = None
        self.identity = {
            "kind": "fresh_fixed_profile_solver",
            "model_identity_sha256": model_identity_sha256,
            "calibration_sha256": calibration_sha256,
            "period_seconds": period,
            "solver_seed": seed,
            "worker_processes": workers,
            "num_search_workers": 1,
            "first_attempt_wall_seconds": first_seconds,
            "unknown_retry_wall_seconds": retry_seconds,
            "active_wall_limit_seconds": active_wall_limit_seconds,
            "maximum_order_wall_seconds_including_margin": (
                self.max_order_wall_seconds
            ),
            "policy": (
                "gap 1 first; retry UNKNOWN at same gap; try gap 0 only after "
                "gap-1 INFEASIBLE; only gap-0 INFEASIBLE is conditional_no"
            ),
            "deterministic_time_equivalent": False,
            "simulator_calls": 0,
        }

    def evaluate_batch(self, tasks):
        self.last_batch_stop_reason = None
        if not tasks:
            return
        iterator = iter(tasks)
        active = {}
        context = multiprocessing.get_context("spawn")
        with ProcessPoolExecutor(
            max_workers=self.workers, mp_context=context
        ) as executor:
            def submit_next():
                try:
                    task = next(iterator)
                except StopIteration:
                    return False
                if (
                    self.deadline is not None
                    and time.monotonic() + self.max_order_wall_seconds
                    > self.deadline
                ):
                    self.last_batch_stop_reason = "active_wall_budget"
                    return False
                future = executor.submit(
                    _solve_fixed_order,
                    task,
                    self.reference["windows"],
                    self.reference["variants"],
                    self.period,
                    self.seed,
                    self.first_seconds,
                    self.retry_seconds,
                )
                active[future] = task
                return True

            for _ in range(self.workers):
                if not submit_next():
                    break
            while active:
                completed, _ = wait(active, return_when=FIRST_COMPLETED)
                for future in completed:
                    task = active.pop(future)
                    yield task, future.result()
                    submit_next()
