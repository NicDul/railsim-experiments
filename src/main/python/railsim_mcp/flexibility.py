"""Entry-order flexibility analysis, separate from the capacity frontier."""

from collections import Counter
from itertools import product
import hashlib
import json
import math
import random
import xml.etree.ElementTree as ET
from pathlib import Path

from .budget import Exhausted
from .capacity import Workflow, occupation_exclusion, save


DEFAULT_FLEXIBILITY = {
    "exact_ordering_cap": 5000,
    "sample_count": 30,
    "candidate_retries": 1,
}
DEFAULT_LIMITS = {
    "wall_seconds": 1200,
    "cpu_seconds": 600,
    "simulations": 120,
    "model_calls": 0,
}


def canonical_rotation(sequence):
    sequence = tuple(sequence)
    if not sequence:
        return sequence
    return min(sequence[i:] + sequence[:i] for i in range(len(sequence)))


def _divisors(value):
    return [n for n in range(1, value + 1) if value % n == 0]


def _totient(value):
    return sum(math.gcd(value, n) == 1 for n in range(1, value + 1))


def necklace_count(counts):
    """Burnside count of distinct cyclic multiset sequences."""
    values = tuple(counts.values())
    if any(type(n) is not int or n < 0 for n in values):
        raise ValueError("class counts must be nonnegative integers")
    counts = tuple(sorted((n for n in values if n), reverse=True))
    if any(n < 0 for n in counts):
        raise ValueError("class counts must be nonnegative")
    if not counts:
        return 1
    length = sum(counts)
    common = math.gcd(*counts)
    fixed_sum = 0
    for repetitions in _divisors(common):
        fixed = math.factorial(length // repetitions)
        for count in counts:
            fixed //= math.factorial(count // repetitions)
        fixed_sum += _totient(repetitions) * fixed
    return fixed_sum // length


def iter_necklaces(counts):
    """Enumerate canonical representatives without expanding duplicate permutations."""
    remaining = Counter({k: n for k, n in counts.items() if n})
    if any(type(n) is not int or n < 0 for n in counts.values()):
        raise ValueError("class counts must be nonnegative integers")
    classes = sorted(remaining)
    length = sum(remaining.values())
    if not length:
        yield ()
        return
    sequence = []
    seen = set()

    def visit():
        if len(sequence) == length:
            value = canonical_rotation(sequence)
            if value not in seen:
                seen.add(value)
                yield value
            return
        for cls in classes:
            if remaining[cls]:
                remaining[cls] -= 1
                sequence.append(cls)
                yield from visit()
                sequence.pop()
                remaining[cls] += 1

    yield from sorted(visit())


def sample_necklace(counts, rng):
    """Draw uniformly from distinct cyclic multiset sequences by orbit-size rejection."""
    classes = [cls for cls in sorted(counts) for _ in range(counts[cls])]
    if not classes:
        return ()
    length = len(classes)
    while True:
        rng.shuffle(classes)
        stabilizer = sum(
            classes == classes[offset:] + classes[:offset]
            for offset in range(length)
        )
        if rng.randrange(length) < stabilizer:
            return canonical_rotation(classes)


def clopper_pearson(successes, trials, alpha=0.05):
    """Two-sided exact binomial confidence limits, using only the standard library."""
    if trials <= 0 or not 0 <= successes <= trials or not 0 < alpha < 1:
        raise ValueError("invalid binomial interval arguments")

    def cdf(k, probability):
        return sum(
            math.comb(trials, i)
            * probability**i
            * (1 - probability) ** (trials - i)
            for i in range(k + 1)
        )

    lower = 0.0
    if successes:
        low, high = 0.0, 1.0
        for _ in range(60):
            middle = (low + high) / 2
            tail = 1 - cdf(successes - 1, middle)
            if tail < alpha / 2:
                low = middle
            else:
                high = middle
        lower = (low + high) / 2

    upper = 1.0
    if successes < trials:
        low, high = 0.0, 1.0
        for _ in range(60):
            middle = (low + high) / 2
            if cdf(successes, middle) > alpha / 2:
                low = middle
            else:
                high = middle
        upper = (low + high) / 2
    return lower, upper


def validate_flexibility(config):
    config = dict(config)
    if config.get("use_case", "UC1") not in ("UC1", "UC2"):
        raise ValueError("entry-order flexibility supports UC1 and UC2 only")
    flex = config.get("flexibility", {})
    if not isinstance(flex, dict) or set(flex) - {
        "exact_ordering_cap",
        "sample_count",
        "candidate_retries",
        "seed",
    }:
        raise ValueError("invalid flexibility settings")
    settings = DEFAULT_FLEXIBILITY | flex
    for name, low, high in [
        ("exact_ordering_cap", 1, 1000000),
        ("sample_count", 1, 10000),
        ("candidate_retries", 0, 10),
        ("seed", 0, 2147483647),
    ]:
        if type(settings.get(name, config.get("seed", 4711))) is not int:
            raise ValueError(f"flexibility.{name} must be an integer")
        value = settings.get(name, config.get("seed", 4711))
        if not low <= value <= high:
            raise ValueError(f"flexibility.{name} must be in {low}..{high}")
        settings[name] = value
    if "seed" not in flex:
        settings["seed"] = config.get("seed", 4711)
    limits = config.get("limits", {})
    if not isinstance(limits, dict):
        raise ValueError("limits must be an object")
    limits = DEFAULT_LIMITS | limits
    config["limits"] = limits
    config["flexibility"] = settings
    return config, settings


def _entry_resource_by_route(source):
    network = ET.parse(source / "network.xml").getroot()
    attributes = {
        link.get("id"): {
            attr.get("name"): attr.text
            for attr in link.findall("./attributes/attribute")
        }
        for link in network.findall("./links/link")
    }
    entries = {
        link for link, attrs in attributes.items() if attrs.get("railsimEntry") == "true"
    }
    routes = {}
    schedule = ET.parse(source / "schedule.xml").getroot()
    for route in schedule.iter("transitRoute"):
        links = [link.get("refId") for link in route.findall("./route/link")]
        markers = [link for link in links if link in entries]
        if len(markers) != 1:
            raise ValueError(
                f"route {route.get('id')} must traverse exactly one entry marker"
            )
        marker = markers[0]
        routes[route.get("id")] = attributes[marker].get(
            "railsimResourceId", marker
        )
    return routes


def _uc1_entry(windows):
    common = set.intersection(*(set(value) for value in windows.values()))
    candidates = [
        resource
        for resource in sorted(common)
        if not resource.startswith("platform")
        and all(
            windows[variant][resource][1]
            <= min(
                grant
                for name, (_, grant, _) in windows[variant].items()
                if name.startswith("platform")
            )
            for variant in windows
        )
    ]
    if not candidates:
        raise ValueError("UC1 entry order needs a shared entry before all platforms")
    return candidates[0]


def order_groups(use_case, source, composition, windows):
    """Return per-entry class multiplicities for one exact mode composition."""
    if use_case == "UC1":
        entry = _uc1_entry(windows)
        if any(entry not in windows[v] for v in windows):
            raise ValueError("UC1 entry resource is not present for every variant")
        return {entry: dict(composition)}
    by_route = _entry_resource_by_route(source)
    groups = {}
    for route, count in composition.items():
        if route not in by_route:
            raise ValueError(f"UC2 route has no entry marker: {route}")
        entry = by_route[route]
        if entry not in windows.get(route, {}):
            raise ValueError(f"UC2 entry profile is missing for route {route}")
        groups.setdefault(entry, {})[route] = count
    return groups


def ordering_universe(groups):
    total = 1
    for counts in groups.values():
        total *= necklace_count(counts)
    return total


def _ordering_id(ordering):
    data = json.dumps(ordering, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(data.encode()).hexdigest()[:24]


def enumerate_orderings(groups):
    entries = sorted(groups)
    cycles = [list(iter_necklaces(groups[entry])) for entry in entries]
    for values in product(*cycles):
        yield {entry: list(sequence) for entry, sequence in zip(entries, values)}


def sample_ordering(groups, rng):
    return {
        entry: list(sample_necklace(groups[entry], rng))
        for entry in sorted(groups)
    }


def _slots(ordering):
    classes = []
    entry_orders = {}
    for entry, sequence in sorted(ordering.items()):
        indices = []
        for cls in sequence:
            indices.append(len(classes))
            classes.append(cls)
        entry_orders[entry] = indices
    return classes, entry_orders


def _slot_pattern(result, products):
    if result.get("slot_pattern"):
        return result["slot_pattern"]
    remaining = {}
    for row in result.get("pattern", []):
        remaining.setdefault(row[0], []).append(row)
    for rows in remaining.values():
        rows.sort(key=lambda row: row[1])
    return [remaining[cls].pop(0) for cls in products]


def _simulation_passed(result):
    return (
        result.get("passed") is True
        and result.get("max_delay") == 0
        and result.get("total_delay") == 0
        and result.get("missing") == 0
        and result.get("detours") == 0
    )


def _classify_solver(status, has_candidate=False):
    if status in ("FEASIBLE", "OPTIMAL"):
        return "solver_feasible"
    if status == "INFEASIBLE" and not has_candidate:
        return "conditional_no"
    return "unresolved"


class FlexibilityWorkflow:
    def __init__(self, settings, config, output, invocation, *, solver=None, simulator=None):
        self.config, self.flex = validate_flexibility(config)
        self.out = Path(output).resolve()
        self.out.mkdir(parents=True, exist_ok=True)
        if self.out.is_relative_to(settings.project):
            raise ValueError("output must be outside simulator checkout")
        kwargs = {}
        if solver is not None:
            kwargs["solver"] = solver
        if simulator is not None:
            kwargs["simulator"] = simulator
        minimum_volume = 1 if self.config.get("use_case", "UC1") == "UC1" else 4
        self.capacity = Workflow(
            settings,
            self.config,
            self.out / "calibration",
            invocation,
            minimum_volume=minimum_volume,
            **kwargs,
        )
        self.checkpoint = self.out / "flexibility-checkpoint.json"
        self.report = None

    def _identity(self):
        return {
            "capacity_identity": self.capacity.identity,
            "flexibility": self.flex,
        }

    def _load_or_create(self):
        identity = self._identity()
        if self.checkpoint.exists():
            self.report = json.loads(self.checkpoint.read_text())
            if self.report.get("identity") != identity:
                raise ValueError(
                    "flexibility resume identity changed; use a new output directory"
                )
        else:
            self.report = {
                "schema_version": 1,
                "identity": identity,
                "source_revision": self.capacity.report["source_revision"],
                "assumptions": [
                    "Ordering classes are the configured UC1 products or UC2 product/flow/direction routes.",
                    "Cyclic rotations within each entry are equivalent; UC2 entry sequences combine with free relative phase.",
                    "Conditional negatives rely on the current outward-rounded fixed-profile CP-SAT model, not unrestricted simulator infeasibility.",
                    "A work result requires simulator passage with zero raw delay, no detours and every train present in the scored periods.",
                ],
                "cases": [],
            }
        self._save()

    def _save(self):
        self.capacity.persist()
        self.report["budget"] = dict(self.capacity.report.get("budget", {}))
        save(self.checkpoint, self.report)

    def _case(self, ref, volume, options):
        counts = {
            cls: n
            for cls, n in self.capacity.mode.composition(volume).items()
            if n
        }
        source = (
            self.capacity.settings.project
            / f"src/main/resources/scenarios/use_case_{self.capacity.use_case[-1]}"
            / ("building_block_" + self.config["building_block"][-1])
        )
        groups = order_groups(
            self.capacity.use_case, source, counts, ref["windows"]
        )
        denominator = ordering_universe(groups)
        necessary_check = occupation_exclusion(
            counts, options, ref["windows"], self.config["period_seconds"]
        )
        excluded = bool(necessary_check["excluded_resources"])
        tph = (
            3600
            * sum(counts.values())
            / self.config["period_seconds"]
        )
        case = {
            "volume_per_direction": volume if self.capacity.use_case == "UC2" else None,
            "volume": volume,
            "trains_per_hour": tph,
            "total_trains_per_hour": tph,
            "composition": counts,
            "entry_composition": groups,
            "ordering_denominator": denominator,
            "necessary_check": necessary_check,
            "method": (
                "sound_necessary_condition"
                if excluded
                else "exact_ordering_enumeration"
                if denominator <= self.flex["exact_ordering_cap"]
                else "uniform_ordering_level_sample"
            ),
            "ordering_sampler": (
                "uniform multiset linear draw; accept with stabilizer/length; canonicalize rotation"
            ),
            "orderings": {},
            "draws": [],
        }
        existing = next(
            (row for row in self.report["cases"] if row["volume"] == volume), None
        )
        if existing:
            if (
                existing["composition"] != counts
                or existing["entry_composition"] != groups
                or existing["ordering_denominator"] != denominator
                or existing["necessary_check"] != necessary_check
            ):
                raise ValueError("resumed flexibility composition changed")
            return existing, groups
        if not excluded and denominator <= self.flex["exact_ordering_cap"]:
            orders = list(enumerate_orderings(groups))
            if len(orders) != denominator:
                raise ValueError("enumerated ordering count disagrees with Burnside count")
            case["ordering_ids"] = []
            for ordering in orders:
                key = _ordering_id(ordering)
                case["ordering_ids"].append(key)
                case["orderings"][key] = {
                    "ordering": ordering,
                    "status": "pending_solver",
                    "candidates": [],
                    "trials": [],
                }
        elif not excluded:
            rng = random.Random(self.flex["seed"] + volume)
            for _ in range(self.flex["sample_count"]):
                ordering = sample_ordering(groups, rng)
                key = _ordering_id(ordering)
                case["draws"].append({"ordering_id": key, "status": "pending"})
                case["orderings"].setdefault(
                    key,
                    {
                        "ordering": ordering,
                        "status": "pending_solver",
                        "candidates": [],
                        "trials": [],
                    },
                )
        self.report["cases"].append(case)
        self._save()
        return case, groups

    def _options(self, ref):
        options = {}
        for variant, info in ref["variants"].items():
            cls = info["product"] if self.capacity.use_case == "UC1" else variant
            options.setdefault(cls, []).append(variant)
        return {cls: sorted(values) for cls, values in options.items()}

    def _solve(self, case, record, ref, options, entry, products, entry_orders):
        candidates = record["candidates"]
        attempt = len(candidates)
        forbidden = [candidate["slot_pattern"] for candidate in candidates]
        try:
            seconds = self.capacity.budget.allowance(
                "solver", self.config["solver_seconds"], self.config["workers"]
            )
            record["solver_attempted"] = True
            result = self.capacity.solver(
                products,
                options,
                ref["windows"],
                seconds,
                frozenset(),
                self.config["workers"],
                period=self.config["period_seconds"],
                seed=(self.flex["seed"] + int(_ordering_id(record["ordering"])[:8], 16) + attempt)
                % 2147483647,
                order=products if self.capacity.use_case == "UC1" else None,
                entry=entry if self.capacity.use_case == "UC1" else None,
                entry_orders=entry_orders if self.capacity.use_case == "UC2" else None,
                no_overlap=self.config.get("resource_no_overlap", False),
                forbidden_patterns=forbidden,
            )
            trial = {"attempt": attempt, "solver": result}
            record["trials"].append(trial)
            status = _classify_solver(result.get("status"), bool(candidates))
            if status == "solver_feasible":
                slot_pattern = _slot_pattern(result, products)
                if len(slot_pattern) != len(products):
                    raise ValueError("solver candidate omitted train-slot schedule")
                candidate = {
                    "slot_pattern": slot_pattern,
                    "status": "pending_validation",
                }
                candidates.append(candidate)
                record["solver_classification"] = "solver_feasible"
                record["status"] = "solver_feasible"
            elif status == "conditional_no":
                record["solver_classification"] = "conditional_no"
                record["status"] = "conditional_no"
            else:
                if candidates:
                    record["solver_classification"] = "solver_feasible"
                    record["status"] = "unresolved"
                else:
                    record["solver_classification"] = "unresolved"
                    record["status"] = "unresolved"
                record["unresolved_reason"] = (
                    "additional_candidate_solver_infeasible"
                    if candidates and result.get("status") == "INFEASIBLE"
                    else "solver_" + str(result.get("status", "missing")).lower()
                )
        except Exhausted as exc:
            record.setdefault("solver_classification", "unresolved")
            record["status"] = "unresolved"
            record["unresolved_reason"] = "budget_exhausted: " + str(exc)
        except (ValueError, RuntimeError, OSError) as exc:
            record.setdefault("solver_classification", "unresolved")
            record["status"] = "unresolved"
            record["unresolved_reason"] = "solver_execution_failed: " + str(exc)
        self._save()

    def _validate_order(self, case, record, ref, options, entry, products, entry_orders):
        max_candidates = self.flex["candidate_retries"] + 1
        while record["status"] == "solver_feasible":
            pending = next(
                (
                    candidate
                    for candidate in record["candidates"]
                    if candidate["status"] == "pending_validation"
                ),
                None,
            )
            if pending is None:
                if len(record["candidates"]) >= max_candidates:
                    record["status"] = "unresolved"
                    record["unresolved_reason"] = "all_bounded_candidates_rejected"
                    self._save()
                    return
                self._solve(case, record, ref, options, entry, products, entry_orders)
                continue
            try:
                self.capacity.ws.timeout_seconds = self.capacity.budget.allowance(
                    "simulation", 300
                )
                simulation, _ = self.capacity.simulator(
                    self.capacity.ws,
                    ref["workspace"],
                    pending["slot_pattern"],
                    {v: b["terminal"] for v, b in ref["baselines"].items()},
                    self.flex["seed"],
                    self.config["period_seconds"],
                    **(
                        {
                            "vehicle_types": {
                                route: ref["variants"][route]["product"]
                                for route in options
                            }
                        }
                        if self.capacity.use_case == "UC2"
                        else {}
                    ),
                )
                pending["simulation"] = simulation
                if _simulation_passed(simulation):
                    pending["status"] = "simulator_passed"
                    record["status"] = "works"
                    record["working_candidate"] = len(record["candidates"]) - 1
                    self._save()
                    return
                pending["status"] = "simulator_rejected"
            except Exhausted as exc:
                pending["status"] = "budget_exhausted"
                record["status"] = "unresolved"
                record["unresolved_reason"] = "budget_exhausted: " + str(exc)
            except (ValueError, RuntimeError, OSError) as exc:
                pending["status"] = "simulation_failed"
                record["status"] = "unresolved"
                record["unresolved_reason"] = "simulation_failed: " + str(exc)
            self._save()

    def _prepare_validation_draws(self, case):
        if case["method"] != "exact_ordering_enumeration":
            return
        if "validation_draws" in case:
            return
        feasible = [
            key
            for key in case["ordering_ids"]
            if case["orderings"][key]["status"] == "solver_feasible"
        ]
        if len(feasible) <= self.flex["sample_count"]:
            draws = feasible
        elif feasible:
            rng = random.Random(self.flex["seed"] ^ case["volume"] ^ 0x5EED)
            draws = rng.choices(feasible, k=self.flex["sample_count"])
        else:
            draws = []
        case["validation_draws"] = draws
        case["validation_results"] = []
        self._save()

    def _run_exact(self, case, ref, options, entries):
        for key in case["ordering_ids"]:
            record = case["orderings"][key]
            if record["status"] == "pending_solver":
                products, entry_orders = _slots(record["ordering"])
                self._solve(
                    case,
                    record,
                    ref,
                    options,
                    entries[0] if self.capacity.use_case == "UC1" else None,
                    products,
                    entry_orders,
                )
        self._prepare_validation_draws(case)
        while len(case["validation_results"]) < len(case["validation_draws"]):
            key = case["validation_draws"][len(case["validation_results"])]
            record = case["orderings"][key]
            products, entry_orders = _slots(record["ordering"])
            self._validate_order(
                case,
                record,
                ref,
                options,
                entries[0] if self.capacity.use_case == "UC1" else None,
                products,
                entry_orders,
            )
            case["validation_results"].append(
                {"ordering_id": key, "status": record["status"]}
            )
            self._save()

    def _run_sampled(self, case, ref, options, entries):
        for draw in case["draws"]:
            if draw["status"] != "pending":
                continue
            key = draw["ordering_id"]
            record = case["orderings"][key]
            products, entry_orders = _slots(record["ordering"])
            if record["status"] == "pending_solver":
                self._solve(
                    case,
                    record,
                    ref,
                    options,
                    entries[0] if self.capacity.use_case == "UC1" else None,
                    products,
                    entry_orders,
                )
            if record["status"] == "solver_feasible":
                self._validate_order(
                    case,
                    record,
                    ref,
                    options,
                    entries[0] if self.capacity.use_case == "UC1" else None,
                    products,
                    entry_orders,
                )
            draw["status"] = record["status"]
            self._save()

    def _summarize(self, case):
        if case["method"] == "sound_necessary_condition":
            result = {
                key: value
                for key, value in case.items()
                if key not in ("orderings", "ordering_ids", "draws")
            }
            result.update(
                solver_ordering_counts={"conditional_no": case["ordering_denominator"]},
                unique_orderings_evaluated=0,
                conditional_no=case["ordering_denominator"],
                solver_feasible=0,
                solver_unknown=0,
                solver_unresolved=0,
                unresolved_unique=0,
                solver_classification_complete=True,
                solver_feasible_count=0,
                solver_coverage=1.0,
                sample_size=0,
                planned_sample_size=0,
                unique_validated_orderings=0,
                validation_coverage=0.0,
                validation_outcomes={},
                validated_works=0,
                unresolved_draws=0,
                sample_complete=True,
                unvalidated_solver_feasible=0,
                unresolved_total=0,
                status_counts={
                    "validated_works": 0,
                    "conditional_no": case["ordering_denominator"],
                    "unresolved_draws": 0,
                },
                percentage={
                    "status": "exact",
                    "value": 0.0,
                    "interval": [0.0, 0.0],
                    "method": "sound whole-composition resource-occupation necessary-condition rejection under the fixed-profile assumptions",
                },
                result_method="sound whole-composition necessary-condition rejection",
            )
            return result
        unique = list(case["orderings"].values())
        statuses = Counter(record["status"] for record in unique)
        classifications = Counter(
            record.get("solver_classification", "unresolved") for record in unique
        )
        result = {
            key: value
            for key, value in case.items()
            if key not in ("orderings", "ordering_ids", "draws", "validation_draws", "validation_results")
        }
        result.update(
            solver_ordering_counts=dict(statuses),
            solver_classification_counts=dict(classifications),
            unique_orderings_evaluated=len(unique),
            conditional_no=classifications["conditional_no"],
            solver_feasible=classifications["solver_feasible"],
            solver_unknown=sum(
                any(
                    trial.get("solver", {}).get("status") == "UNKNOWN"
                    for trial in record["trials"]
                )
                for record in unique
            ),
            solver_unresolved=classifications["unresolved"],
            unresolved_unique=statuses["unresolved"],
        )
        if case["method"] == "uniform_ordering_level_sample":
            draws = case["draws"]
            completed = [draw for draw in draws if draw["status"] != "pending"]
            counts = Counter(draw["status"] for draw in completed)
            result.update(
                sample_size=len(completed),
                planned_sample_size=len(draws),
                coverage=len(set(draw["ordering_id"] for draw in completed))
                / case["ordering_denominator"],
                sample_outcomes=dict(counts),
                validated_works=counts["works"],
                conditional_no=counts["conditional_no"],
                unresolved_draws=counts["unresolved"],
                unresolved_total=counts["unresolved"],
                sample_complete=len(completed) == len(draws),
            )
        else:
            draws = case.get("validation_results", [])
            counts = Counter(draw["status"] for draw in draws)
            result.update(
                solver_classification_complete=(
                    len(case["orderings"]) == case["ordering_denominator"]
                    and all(
                        record.get("solver_attempted", False)
                        for record in unique
                    )
                ),
                solver_feasible_count=sum(
                    record.get("solver_classification") == "solver_feasible"
                    for record in unique
                ),
                solver_coverage=sum(
                    record.get("solver_attempted", False)
                    for record in unique
                )
                / case["ordering_denominator"],
                sample_size=len(draws),
                planned_sample_size=len(case.get("validation_draws", [])),
                unique_validated_orderings=sum(
                    record["status"] in ("works", "unresolved")
                    and any(
                        candidate.get("simulation")
                        for candidate in record["candidates"]
                    )
                    for record in unique
                ),
                validation_coverage=sum(
                    record.get("solver_classification") == "solver_feasible"
                    and any(
                        candidate.get("simulation")
                        for candidate in record["candidates"]
                    )
                    for record in unique
                )
                / max(
                    1,
                    sum(
                        record.get("solver_classification") == "solver_feasible"
                        for record in unique
                    ),
                ),
                validation_outcomes=dict(counts),
                validated_works=counts["works"],
                unresolved_draws=counts["unresolved"],
                sample_complete=len(draws) == len(case.get("validation_draws", [])),
                unvalidated_solver_feasible=sum(
                    record.get("solver_classification") == "solver_feasible"
                    and not any(
                        candidate.get("simulation")
                        for candidate in record["candidates"]
                    )
                    for record in unique
                ),
            )
            result["unresolved_total"] = (
                result["solver_unresolved"]
                + result["unresolved_draws"]
                + result["unvalidated_solver_feasible"]
            )
        result["status_counts"] = {
            name: result.get(name, 0)
            for name in ("validated_works", "conditional_no", "unresolved_draws")
        }
        result["percentage"] = self._percentage(case, result, unique)
        if case["method"] == "uniform_ordering_level_sample":
            result["result_method"] = "uniform full-universe ordering-level sample"
        elif result["percentage"]["status"].startswith("sampled"):
            result["result_method"] = (
                "exact order enumeration; sampled simulator validation among solver-feasible orders"
            )
        else:
            result["result_method"] = case["method"]
        return result

    def _percentage(self, case, summary, unique):
        denominator = case["ordering_denominator"]
        if case["method"] == "uniform_ordering_level_sample":
            n = summary["sample_size"]
            if not summary["sample_complete"] or not n:
                return {"status": "unresolved", "value": None, "interval": None}
            outcomes = summary["sample_outcomes"]
            works, unresolved = outcomes.get("works", 0), outcomes.get("unresolved", 0)
            if not unresolved:
                lower, upper = clopper_pearson(works, n)
                return {
                    "status": "sampled",
                    "value": works / n,
                    "interval": [lower, upper],
                    "confidence": 0.95,
                    "method": "two-sided exact binomial (Clopper-Pearson), independent uniform ordering draws with replacement",
                }
            lower, _ = clopper_pearson(works, n, alpha=0.025)
            _, upper = clopper_pearson(works + unresolved, n, alpha=0.025)
            return {
                "status": "sampled_with_unresolved",
                "value": None,
                "interval": [lower, upper],
                "confidence": 0.95,
                "method": "95% conservative exact-binomial confidence bounds; unresolved outcomes widen the upper endpoint",
            }
        if not summary["solver_classification_complete"]:
            return {"status": "unresolved", "value": None, "interval": None}
        feasible = summary["solver_feasible_count"]
        solver_unresolved = summary["solver_unresolved"]
        draws = case.get("validation_results", [])
        if not feasible:
            if solver_unresolved:
                return {"status": "unresolved", "value": None, "interval": None}
            return {
                "status": "exact",
                "value": 0.0,
                "interval": [0.0, 0.0],
                "method": "all distinct orderings solved; every ordering conditionally infeasible",
            }
        if not summary["sample_complete"] or not draws:
            return {"status": "unresolved", "value": None, "interval": None}
        outcomes = Counter(draw["status"] for draw in draws)
        works = outcomes["works"]
        validation_unknown = outcomes["unresolved"]
        scale = feasible / denominator
        if not solver_unresolved and not validation_unknown and feasible == len(draws):
            return {
                "status": "exact",
                "value": works / denominator,
                "interval": [works / denominator, works / denominator],
                "method": "exact ordering enumeration, exact fixed-order solve, simulator validation of every solver-feasible ordering",
            }
        if not draws:
            return {"status": "unresolved", "value": None, "interval": None}
        lower, _ = clopper_pearson(works, len(draws))
        _, upper = clopper_pearson(
            works + validation_unknown, len(draws)
        )
        return {
            "status": "sampled_with_unresolved"
            if solver_unresolved or validation_unknown
            else "sampled",
            "value": None if solver_unresolved or validation_unknown else scale * works / len(draws),
            "interval": [
                scale * lower,
                min(1.0, solver_unresolved / denominator + scale * upper),
            ],
            "confidence": 0.95,
            "method": "exact solver-feasible denominator times a uniform with-replacement validation sample; conservative exact-binomial bounds include unresolved orderings",
        }

    def run(self):
        ref = self.capacity.calibrate()
        self._load_or_create()
        options = self._options(ref)
        for volume in self.config["volumes"]:
            case, groups = self._case(ref, volume, options)
            entries = sorted(groups)
            if case["method"] == "sound_necessary_condition":
                self._save()
                continue
            if case["method"] == "exact_ordering_enumeration":
                self._run_exact(case, ref, options, entries)
            else:
                self._run_sampled(case, ref, options, entries)
            self._save()
        summaries = [self._summarize(case) for case in self.report["cases"]]
        result = {
            "use_case": self.capacity.use_case,
            "building_block": self.config["building_block"],
            "mode": self.capacity.mode.id,
            "period_seconds": self.config["period_seconds"],
            "flexibility": self.flex,
            "cases": summaries,
            "budget": dict(self.capacity.report.get("budget", {})),
        }
        save(self.out / "flexibility-report.json", result)
        save(self.out / "flexibility-checkpoint.json", self.report)
        (self.out / "flexibility-results.md").write_text(render_markdown([result]))
        return result


def _format_percentage(row):
    value = row["percentage"]
    if value["status"] == "exact":
        return f'{100 * value["value"]:.1f}% exact'
    if value["interval"] is not None and value["status"].startswith("sampled"):
        lower, upper = value["interval"]
        return f'{100 * lower:.1f}-{100 * upper:.1f}% 95% CI'
    return "Unresolved (no valid percentage)"


def render_markdown(reports):
    reports = sorted(reports, key=lambda report: report["use_case"])
    lines = []
    for use_case in sorted({report["use_case"] for report in reports}):
        rows = sorted(
            (report for report in reports if report["use_case"] == use_case),
            key=lambda row: (row["mode"], row["building_block"]),
        )
        blocks = sorted({row["building_block"] for row in rows})
        lines.extend(
            [
                f'| {use_case} flexibility: mode x volume (trains/hour) | '
                + " | ".join(blocks)
                + " |",
                "| --- | " + " | ".join("---:" for _ in blocks) + " |",
            ]
        )
        labels = sorted(
            {
                f'{row["mode"]} @ {case["volume"]} / period '
                f'({case["total_trains_per_hour"]:.1f} total trains/hour'
                + (
                    f'; {case["volume_per_direction"]} per direction'
                    if case["volume_per_direction"] is not None
                    else ""
                )
                + ")"
                for row in rows
                for case in row["cases"]
            }
        )
        for label in labels:
            cells = []
            for block in blocks:
                match = next(
                    (
                        case
                        for row in rows
                        if row["building_block"] == block
                        for case in row["cases"]
                        if label.startswith(row["mode"] + " @ ")
                        and label.split(" @ ", 1)[1].startswith(str(case["volume"]) + " /")
                    ),
                    None,
                )
                cells.append(_format_percentage(match) if match else "-")
            lines.append(f"| {label} | " + " | ".join(cells) + " |")
        lines.append("")
    lines.extend(
        [
            "## Counts and provenance",
            "",
            "| Use case | Block | Mode | Volume convention | Denominator | Works (validated) | Conditional no | Unresolved | Sample/coverage | Method |",
            "| --- | --- | --- | --- | ---: | ---: | ---: | --- | --- | --- |",
        ]
    )
    for report in reports:
        for case in report["cases"]:
            lines.append(
                "| {uc} | {block} | {mode} | {volume} | {den} | {works} | {no} | {unresolved} | {sample}/{planned}; {coverage} | {method} |".format(
                    uc=report["use_case"],
                    block=report["building_block"],
                    mode=report["mode"],
                    volume=(
                        f'{case["volume_per_direction"]} per direction, '
                        f'{case["total_trains_per_hour"]:.1f} trains/hour total'
                        if case["volume_per_direction"] is not None
                        else f'{case["volume"]} trains / {report["period_seconds"]} s'
                    ),
                    den=case["ordering_denominator"],
                    works=case["validated_works"],
                    no=case["conditional_no"],
                    unresolved=(
                        str(case["unresolved_draws"])
                        if case["method"] == "uniform_ordering_level_sample"
                        else (
                            f'{case["solver_unresolved"]} solver; '
                            f'{case["unresolved_draws"]} sampled validation; '
                            f'{case["unvalidated_solver_feasible"]} feasible unvalidated'
                        )
                    ),
                    sample=case["sample_size"],
                    planned=case["planned_sample_size"],
                    coverage=(
                        f'{case["coverage"]:.1%} distinct orderings'
                        if case["method"] == "uniform_ordering_level_sample"
                        else (
                            f'{case["solver_coverage"]:.1%} solver; '
                            f'{case["validation_coverage"]:.1%} feasible-order validation'
                        )
                    ),
                    method=case.get("result_method", case["method"]),
                )
            )
    lines.extend(
        [
            "",
            "Sampled intervals are two-sided exact-binomial confidence intervals over independent uniform ordering draws with replacement; when outcomes remain unresolved, the reported interval conservatively widens to include them. Exact negatives are conditional on the fixed-profile capacity-model assumptions. A solver UNKNOWN, simulator rejection, or unvalidated candidate is unresolved, never a no.",
            "",
            "## Assumptions",
            "",
            "- UC1 order classes are the deterministic mode products; UC2 classes are product/flow/direction route IDs, grouped into independent cyclic sequences at each physical entry marker.",
            "- Cyclic rotations within each entry are equivalent. UC2 entry sequences are combined independently because their relative timing is free. Platform/route alternatives are not separate orderings.",
            "- A work requires simulator validation: zero raw delay, no detours, and every expected train present in the scored periods. Candidate retries are bounded and checkpointed.",
            "- The existing capacity frontier/report is separate and unchanged. Conditional no means only that the fixed-order fixed-profile CP-SAT model is infeasible under its current assumptions.",
            "",
        ]
    )
    return "\n".join(lines)
