import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from railsim_mcp.configuration import Settings
from railsim_mcp.flexibility import (
    FlexibilityWorkflow,
    _classify_solver,
    _entry_resource_by_route,
    _simulation_passed,
    clopper_pearson,
    enumerate_orderings,
    iter_necklaces,
    necklace_count,
    order_groups,
    ordering_universe,
    sample_necklace,
    validate_flexibility,
)
from railsim_mcp.routing import RouteMode
from railsim_mcp.simulation import simulate
from railsim_mcp.solver import solve
from railsim_mcp.uc1_modes import UC1Mode


ROOT = Path(__file__).resolve().parents[3]


class OrderingTests(unittest.TestCase):
    def test_small_uc1_volumes_use_native_deterministic_rounding(self):
        mode = UC1Mode.load(
            ROOT / "src/main/resources/scenarios/use_case_1/uc1_operational_plan.json",
            "MAINLINE/FV_STOP",
        )
        self.assertEqual(
            mode.composition(1), {"FV": 1, "RV": 0, "GV": 0}
        )
        self.assertEqual(mode.composition(2), {"FV": 1, "RV": 1, "GV": 0})
        self.assertEqual(mode.composition(3), {"FV": 1, "RV": 1, "GV": 1})

    def test_uc2_flexibility_keeps_its_existing_volume_floor(self):
        config = {
            "use_case": "UC2",
            "building_block": "BB1",
            "mode": "METRO/BALANCED",
            "period_seconds": 1800,
            "volumes": [1],
            "runner": "none",
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings(ROOT, root / "state")
            with self.assertRaisesRegex(ValueError, "4..100"):
                FlexibilityWorkflow(settings, config, root / "output", ["test"])

    def test_necklace_counts_and_periodic_rotation_canonicalization(self):
        self.assertEqual(necklace_count({"F": 4}), 1)
        self.assertEqual(necklace_count({"F": 2, "R": 2}), 2)
        cycles = list(iter_necklaces({"F": 2, "R": 2}))
        self.assertEqual(cycles, [("F", "F", "R", "R"), ("F", "R", "F", "R")])
        self.assertEqual(len(set(cycles)), 2)
        self.assertEqual(necklace_count({"A": 1, "B": 1, "C": 1}), 2)

    def test_independent_uc2_entry_sequences_multiply_denominators(self):
        groups = {
            "entry-A": {"FV_AB": 2, "RV_AC": 2},
            "entry-B": {"FV_BA": 1},
            "entry-C": {"RV_CA": 3},
        }
        self.assertEqual(ordering_universe(groups), 2)
        results = list(enumerate_orderings(groups))
        self.assertEqual(len(results), 2)
        self.assertEqual(
            {tuple(row["entry-A"]) for row in results},
            {
                ("FV_AB", "FV_AB", "RV_AC", "RV_AC"),
                ("FV_AB", "RV_AC", "FV_AB", "RV_AC"),
            },
        )

    def test_uc2_mode_uses_distinct_entry_markers_and_flow_classes(self):
        plan = ROOT / "src/main/resources/scenarios/use_case_2/uc2_operational_plan.json"
        mode = RouteMode.load(plan, "METRO/BALANCED")
        composition = mode.composition(4)
        source = ROOT / "src/main/resources/scenarios/use_case_2/building_block_1"
        entries = _entry_resource_by_route(source)
        windows = {
            route: {entries[route]: (0, 0, 1)} for route in composition
        }
        groups = order_groups("UC2", source, composition, windows)
        self.assertGreaterEqual(len(groups), 3)
        self.assertGreater(ordering_universe(groups), 1)

    def test_sampler_is_reproducible_and_returns_canonical_representatives(self):
        import random

        counts = {"A": 2, "B": 2}
        first = sample_necklace(counts, random.Random(78))
        second = sample_necklace(counts, random.Random(78))
        self.assertEqual(first, second)
        self.assertEqual(first, min(first[i:] + first[:i] for i in range(len(first))))

    def test_sampler_corrects_for_periodic_orbit_sizes(self):
        import random

        rng = random.Random(137)
        draws = [sample_necklace({"A": 2, "B": 2}, rng) for _ in range(3000)]
        counts = {cycle: draws.count(cycle) for cycle in set(draws)}
        self.assertEqual(len(counts), 2)
        self.assertLess(abs(counts[("A", "A", "B", "B")] - 1500), 150)
        self.assertLess(abs(counts[("A", "B", "A", "B")] - 1500), 150)

    def test_flexibility_settings_validate_sampling_caps_and_budgets(self):
        config = {
            "building_block": "BB1",
            "mode": "MAINLINE/FV_PASS",
            "volumes": [4],
            "flexibility": {"sample_count": 5, "seed": 9},
        }
        checked, flex = validate_flexibility(config)
        self.assertEqual(flex["sample_count"], 5)
        self.assertEqual(flex["seed"], 9)
        self.assertEqual(checked["limits"]["simulations"], 120)
        for invalid in (
            {"exact_ordering_cap": 0},
            {"sample_count": True},
            {"candidate_retries": 11},
            {"seed": -1},
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                validate_flexibility(config | {"flexibility": invalid})

class SolverOrderTests(unittest.TestCase):
    def test_solver_enforces_each_uc2_entry_sequence(self):
        products = ["A", "B", "C", "D"]
        options = {product: [product] for product in products}
        windows = {
            "A": {"entry-A": (0, 0, 4), "core": (0, 0, 4)},
            "B": {"entry-A": (0, 20, 24), "core": (0, 0, 4)},
            "C": {"entry-B": (0, 10, 14), "core": (0, 0, 4)},
            "D": {"entry-B": (0, 30, 34), "core": (0, 0, 4)},
        }
        entry_orders = {"entry-A": [0, 1], "entry-B": [2, 3]}
        result = solve(
            products,
            options,
            windows,
            2,
            frozenset(),
            1,
            period=100,
            entry_orders=entry_orders,
        )
        self.assertIn(result["status"], ("OPTIMAL", "FEASIBLE"))
        for entry, indices in entry_orders.items():
            arrivals = [
                (result["slot_pattern"][i][1] + windows[products[i]][entry][1])
                % 100
                for i in indices
            ]
            phases = [(arrival - arrivals[0]) % 100 for arrival in arrivals]
            self.assertEqual(phases, sorted(set(phases)))

    def test_solver_retries_exclude_a_previously_rejected_schedule(self):
        products = ["A", "A"]
        options = {"A": ["A"]}
        windows = {"A": {"track": (0, 0, 10)}}
        first = solve(
            products, options, windows, 2, frozenset(), 1, period=100
        )
        second = solve(
            products,
            options,
            windows,
            2,
            frozenset(),
            1,
            period=100,
            forbidden_patterns=[first["slot_pattern"]],
        )
        self.assertEqual(first["status"], "OPTIMAL")
        self.assertIn(second["status"], ("OPTIMAL", "FEASIBLE"))
        self.assertNotEqual(first["slot_pattern"], second["slot_pattern"])


class ClassificationAndIntervalTests(unittest.TestCase):
    def test_statuses_never_promote_unknown_or_candidate_exhaustion_to_no(self):
        self.assertEqual(_classify_solver("INFEASIBLE"), "conditional_no")
        self.assertEqual(_classify_solver("UNKNOWN"), "unresolved")
        self.assertEqual(_classify_solver("INFEASIBLE", has_candidate=True), "unresolved")
        self.assertEqual(_classify_solver("OPTIMAL"), "solver_feasible")

    def test_simulator_gate_requires_zero_delay_no_detours_and_all_trains(self):
        passed = {
            "passed": True,
            "max_delay": 0,
            "total_delay": 0,
            "missing": 0,
            "detours": 0,
        }
        self.assertTrue(_simulation_passed(passed))
        for change in (
            {"max_delay": 1},
            {"total_delay": 1},
            {"missing": 1},
            {"detours": 1},
        ):
            self.assertFalse(_simulation_passed(passed | change))

    def test_clopper_pearson_intervals_cover_extreme_samples(self):
        low, high = clopper_pearson(0, 10)
        self.assertEqual(low, 0)
        self.assertGreater(high, 0.25)
        low, high = clopper_pearson(10, 10)
        self.assertLess(low, 0.75)
        self.assertEqual(high, 1)

    def test_sound_necessary_rejection_covers_known_denominator_exactly(self):
        workflow = object.__new__(FlexibilityWorkflow)
        summary = workflow._summarize(
            {
                "method": "sound_necessary_condition",
                "ordering_denominator": 7,
                "necessary_check": {
                    "excluded_resources": {"track": 1801},
                },
                "composition": {"A": 7},
                "volume": 7,
            }
        )
        self.assertEqual(summary["conditional_no"], 7)
        self.assertEqual(summary["percentage"]["status"], "exact")
        self.assertEqual(summary["percentage"]["value"], 0)

    def test_uc2_simulation_maps_order_class_to_operational_vehicle_type(self):
        run = {
            "trains": [],
            "events_file": "events",
            "run_id": "run",
        }
        workspace = Mock()
        workspace.workspace_root = Path("/tmp/railsim-flexibility-test")
        workspace.run_once.return_value = run
        pattern = [["RV_AB", 0, "RV_AB"]]
        with (
            unittest.mock.patch("railsim_mcp.simulation.shutil.copytree"),
            unittest.mock.patch(
                "railsim_mcp.simulation.replace_timetable"
            ) as replace,
            unittest.mock.patch(
                "railsim_mcp.simulation.evaluate",
                return_value={
                    "passed": True,
                    "max_delay": 0,
                    "total_delay": 0,
                    "missing": [],
                    "errors": [],
                    "total_delay": 0,
                },
            ),
            unittest.mock.patch("railsim_mcp.simulation.events", return_value=[]),
        ):
            simulate(
                workspace,
                "source",
                pattern,
                {"RV_AB": 100},
                17,
                vehicle_types={"RV_AB": "RV"},
            )
        departures = replace.call_args.args[2]
        self.assertEqual(departures[0]["route_id"], "RV_AB")
        self.assertEqual(departures[0]["vehicle_id"], "RV_AB_00")
        self.assertEqual(departures[0]["vehicle_type"], "RV")


class ResumeTests(unittest.TestCase):
    def test_completed_ordering_results_resume_without_solver_replay(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings(ROOT, root / "state")
            ref = {
                "workspace": "fixture",
                "variants": {
                    "FV_P1": {"product": "FV"},
                    "RV_P1": {"product": "RV"},
                    "GV_P1": {"product": "GV"},
                },
                "baselines": {
                    variant: {"terminal": 100}
                    for variant in ("FV_P1", "RV_P1", "GV_P1")
                },
                "windows": {
                    variant: {
                        "entry": (0, 0, 1),
                        "platform1": (0, 2, 3),
                    }
                    for variant in ("FV_P1", "RV_P1", "GV_P1")
                },
            }

            def solver(products, options, _windows, *_args, **kwargs):
                slot_pattern = [
                    [product, i * 10, options[product][0]]
                    for i, product in enumerate(products)
                ]
                return {
                    "status": "OPTIMAL",
                    "slot_pattern": slot_pattern,
                    "pattern": sorted(slot_pattern, key=lambda row: row[1]),
                }

            simulator = Mock(
                return_value=(
                    {
                        "passed": True,
                        "max_delay": 0,
                        "total_delay": 0,
                        "missing": 0,
                        "detours": 0,
                    },
                    {},
                )
            )
            solver_mock = Mock(side_effect=solver)
            config = {
                "use_case": "UC1",
                "building_block": "BB1",
                "mode": "MAINLINE/FV_PASS",
                "period_seconds": 1800,
                "volumes": [4],
                "seed": 12,
                "workers": 1,
                "solver_seconds": 1,
                "rounds": 0,
                "proposals": 1,
                "runner": "none",
                "flexibility": {
                    "exact_ordering_cap": 100,
                    "sample_count": 10,
                    "candidate_retries": 1,
                },
            }

            first = FlexibilityWorkflow(
                settings, config, root / "out", ["test"],
                solver=solver_mock, simulator=simulator
            )
            first.capacity.calibrate = Mock(return_value=copy.deepcopy(ref))
            result = first.run()
            self.assertEqual(result["cases"][0]["percentage"]["status"], "exact")
            self.assertEqual(result["cases"][0]["solver_coverage"], 1.0)
            self.assertEqual(result["cases"][0]["validation_coverage"], 1.0)
            calls = solver_mock.call_count

            resumed = FlexibilityWorkflow(
                settings, config, root / "out", ["resume"],
                solver=solver_mock, simulator=simulator
            )
            resumed.capacity.calibrate = Mock(return_value=copy.deepcopy(ref))
            again = resumed.run()
            self.assertEqual(solver_mock.call_count, calls)
            self.assertEqual(again["cases"][0]["validated_works"], 3)


if __name__ == "__main__":
    unittest.main()
