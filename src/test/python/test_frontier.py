"""Budgets and per-resource guidance at the public solver/workflow seams."""

import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock
from railsim_mcp.budget import Budget, Exhausted
from railsim_mcp.guidance import catalogue, validate
from railsim_mcp.solver import solve
from railsim_mcp.capacity import Workflow, assemble, validate_config
from railsim_mcp.configuration import Settings

ROOT = Path(__file__).resolve().parents[3]


class BudgetTests(unittest.TestCase):
    def test_counts_cpu_wall_and_resume(self):
        clock = [0.0]
        cpu = [0.0]
        state = {}
        b = Budget(
            dict(cpu_seconds=10, wall_seconds=20, simulations=1),
            state,
            lambda: clock[0],
            lambda: cpu[0],
        )
        self.assertEqual(b.allowance("solver", 20, workers=2), 5)
        cpu[0] = 3
        clock[0] = 4
        self.assertEqual(b.allowance("simulation", 300), 7)
        with self.assertRaises(Exhausted):
            b.allowance("simulation", 300)
        resumed = Budget(dict(cpu_seconds=10), state, lambda: clock[0], lambda: cpu[0])
        cpu[0] = 10
        with self.assertRaises(Exhausted):
            resumed.allowance("solver", 30)
        self.assertEqual(state["cpu_seconds"], 10)

    def test_range_is_explicit_and_requires_zero_gap_for_proof_trial(self):
        c = validate_config(
            dict(
                building_block="BB1",
                mode="MAINLINE/FV_PASS",
                volume_range=[8, 12, 2],
                direct_gaps=[1, 0],
            )
        )
        self.assertEqual(c["volumes"], [8, 10, 12])
        for gaps in [[1], [], [-1, 0]]:
            with self.assertRaises(ValueError):
                validate_config(
                    dict(building_block="BB1", volumes=[4], direct_gaps=gaps)
                )

    def test_positive_gap_exclusion_does_not_certify_zero_gap_domain(self):
        row = dict(
            volume=4,
            trains_per_hour=8,
            composition={"FV": 4},
            trials=[
                dict(
                    id=0, scope="all_orders", min_gap=1, solver={"status": "INFEASIBLE"}
                )
            ],
        )
        self.assertEqual(assemble({"rates": [row]})["fixed_profile_exclusions"], [])


class ResourceOrderTests(unittest.TestCase):
    def fixture(self):
        products = ["A", "B", "C"]
        options = {p: [p] for p in products}
        windows = {
            "A": {"x": (0, 0, 5), "y": (0, 30, 35)},
            "B": {"x": (0, 0, 5)},
            "C": {"y": (0, 0, 5)},
        }
        return products, options, windows

    def test_no_common_entry_required_and_rotations_are_deduplicated(self):
        products, options, windows = self.fixture()
        self.assertEqual(
            catalogue(products, options, windows)["resources"],
            {"x": ["t000", "t001"], "y": ["t000", "t002"]},
        )
        a, key = validate(
            {"resource_orders": [{"resource": "x", "trains": ["t000", "t001"]}]},
            products,
            options,
            windows,
        )
        b, key2 = validate(
            {"resource_orders": [{"resource": "x", "trains": ["t001", "t000"]}]},
            products,
            options,
            windows,
        )
        self.assertEqual(key, key2)
        result = solve(
            products, options, windows, 2, frozenset(), 1, period=100, resource_orders=a
        )
        self.assertEqual(result["status"], "OPTIMAL")

    def test_local_cyclic_orders_and_wrap_checked_independently(self):
        products = ["A", "B", "C"]
        options = {p: [p] for p in products}
        windows = {
            p: {"x": (0, 0, 10), "y": (0, 30 - i * 10, 35 - i * 10)}
            for i, p in enumerate(products)
        }
        guide, _ = validate(
            {
                "resource_orders": [
                    {"resource": "x", "trains": ["t000", "t002", "t001"]},
                    {"resource": "y", "trains": ["t000", "t001", "t002"]},
                ]
            },
            products,
            options,
            windows,
        )
        result = solve(
            products,
            options,
            windows,
            2,
            frozenset(),
            1,
            period=100,
            resource_orders=guide,
        )
        self.assertEqual(result["status"], "OPTIMAL")
        times = {p: t for p, t, v in result["pattern"]}
        for row in guide:
            arrivals = [
                times[products[i]] + windows[products[i]][row["resource"]][1]
                for i in row["indices"]
            ]
            phases = [(t - arrivals[0]) % 100 for t in arrivals]
            self.assertEqual(phases, sorted(set(phases)))

    def test_invalid_or_variant_dependent_visitors_are_rejected(self):
        products, options, windows = self.fixture()
        for data in [
            {"resource_orders": []},
            {"resource_orders": [{"resource": "unknown", "trains": ["t000"]}]},
            {"resource_orders": [{"resource": "x", "trains": ["t000", "t000"]}]},
            {"resource_orders": [{"resource": "x", "trains": ["t000"]}]},
        ]:
            with self.assertRaises(ValueError):
                validate(data, products, options, windows)
        options["B"].append("B2")
        windows["B2"] = {"y": (0, 0, 5)}
        self.assertNotIn("x", catalogue(products, options, windows)["resources"])

    def test_hint_is_not_a_constraint(self):
        result = solve(
            ["A", "A"],
            {"A": ["A"]},
            {"A": {"x": (0, 0, 10)}},
            2,
            frozenset(),
            1,
            period=100,
            hint=[["A", 0, "A"], ["A", 0, "A"]],
        )
        self.assertEqual(result["status"], "OPTIMAL")
        self.assertNotEqual(result["pattern"][0][1], result["pattern"][1][1])

    def test_uc2_model_escalation_reaches_resource_solver_and_simulator(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            products, options, windows = self.fixture()
            # Use declared UC2 route groups in a synthetic measured profile.
            variants = {
                "RV_AB": {"product": "RV"},
                "RV_BA": {"product": "RV"},
                "RV_AC": {"product": "RV"},
                "RV_CA": {"product": "RV"},
            }
            ref = dict(
                workspace="fixture",
                variants=variants,
                baselines={v: {"terminal": 100} for v in variants},
                windows={v: {"switch": (0, 0, 5)} for v in variants},
            )
            model = Mock(
                side_effect=lambda context: {
                    "proposals": [
                        {
                            "resource_orders": [
                                {
                                    "resource": "switch",
                                    "trains": list(
                                        reversed(
                                            context["resource_order_catalogue"][
                                                "resources"
                                            ]["switch"]
                                        )
                                    ),
                                }
                            ]
                        }
                    ]
                }
            )
            pattern = [["RV_AB", i * 10, "RV_AB"] for i in range(8)]
            solver = Mock(
                side_effect=[
                    {"status": "UNKNOWN"},
                    {"status": "OPTIMAL", "pattern": pattern},
                ]
            )
            simulator = Mock(return_value=({"passed": True, "run_id": "good"}, {}))
            config = dict(
                use_case="UC2",
                building_block="BB1",
                mode="METRO/BALANCED",
                volumes=[4],
                runner="fixture",
                rounds=1,
            )
            w = Workflow(
                Settings(ROOT, root / "state"),
                config,
                root / "out",
                ["test"],
                solver=solver,
                simulator=simulator,
                model=model,
            )
            w.calibrate = Mock(return_value=ref)
            w.export = Mock()
            report = w.run()
            self.assertTrue(solver.call_args.kwargs["resource_orders"])
            self.assertEqual(report["rates"][0]["trials"][1]["scope"], "one_order")
            self.assertEqual(report["bounds"]["search_lower_bound_trains_per_hour"], 16)
