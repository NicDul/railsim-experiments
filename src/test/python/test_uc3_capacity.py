"""UC3 turnbacks are complete trips with repeated resource visits, not UC2 aliases."""

from collections import Counter
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from railsim_mcp.budget import Exhausted
from railsim_mcp.capacity import Workflow, occupation_exclusion, validate_config
from railsim_mcp.configuration import Settings
from railsim_mcp.guidance import catalogue
from railsim_mcp.profiles import reference_windows
from railsim_mcp.replay import replay
from railsim_mcp.routing import RouteMode, topology
from railsim_mcp.solver import solve
from railsim_mcp.validation import evaluate

ROOT = Path(__file__).resolve().parents[3]
SCENARIO = ROOT / "src/main/resources/scenarios/use_case_3"


def config(**changes):
    return dict(
        use_case="UC3", building_block="BB1", mode="METRO/LOW_REVERSAL",
        volume_range=[1, 6, 1], rounds=0, runner="none",
    ) | changes


class UC3ModeTests(unittest.TestCase):
    def test_native_domain_and_unchanged_uc1_uc2_domains(self):
        self.assertEqual(validate_config(config())["volumes"], list(range(1, 7)))
        for changes in [
            {"volume_range": [0, 6, 1]}, {"volume_range": [1, 7, 1]},
            {"building_block": "BB3"}, {"guidance": "entry"},
            {"volume_range": [True, 6, 1]},
        ]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_config(config(**changes))
        for uc in ["UC1", "UC2"]:
            with self.subTest(uc=uc), self.assertRaises(ValueError):
                validate_config(config(use_case=uc))

    def test_all_declared_modes_exact_counts_and_route_identity(self):
        expected = {
            "LOW_REVERSAL": [0, 0, 2, 2, 2, 2],
            "HIGH_REVERSAL": [0, 2, 2, 4, 4, 4],
        }
        plan = json.loads((SCENARIO / "uc3_operational_plan.json").read_text())
        self.assertEqual(plan["modes"], [
            {"mix": "METRO", "patterns": ["LOW_REVERSAL", "HIGH_REVERSAL"]}
        ])
        for pattern, reversals in expected.items():
            mode = RouteMode.load(SCENARIO / "uc3_operational_plan.json", "METRO/" + pattern)
            self.assertEqual(set(mode.variants), {"RV_LMR", "RV_RML", "RV_LML"})
            self.assertEqual(mode.variants["RV_LML"]["direction"], "one-way")
            for volume, lml in enumerate(reversals, 1):
                composition = mode.composition(volume)
                self.assertEqual(sum(composition.values()), 2 * volume)
                self.assertEqual(composition.get("RV_LML", 0), lml)
                self.assertEqual(composition.get("RV_LMR", 0), volume - lml // 2)
                self.assertEqual(composition.get("RV_RML", 0), volume - lml // 2)
            for bb in [1, 2]:
                result = topology(SCENARIO / f"building_block_{bb}", mode.variants)
                reversal = result["route_profiles"]["RV_LML"]
                self.assertEqual([stop["refId"] for stop in reversal],
                                 ["L_F", "M_REVERSING", "L_R"])
                self.assertEqual(reversal[1]["minimumStopDuration"], "00:08:00")
                self.assertIn("platform_reversing",
                              result["capacity_one_resources"]["RV_LML"])
            for volume in [0, 7, True]:
                with self.assertRaises(ValueError):
                    mode.composition(volume)


class MultiVisitTests(unittest.TestCase):
    def test_calibration_keeps_disjoint_visits_and_dwell(self):
        rows = [
            dict(resource="switch", vehicle="RV_00", start=10.4, end=20.1),
            dict(resource="turnback", vehicle="RV_00", start=30, end=510),
            dict(resource="switch", vehicle="RV_00", start=520.4, end=530.1),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "r").mkdir()
            (root / "r/run.json").write_text(json.dumps(
                dict(snapshot_directory=tmp, events_file="events")
            ))
            with patch("railsim_mcp.profiles.holds", return_value=rows), \
                 patch("railsim_mcp.profiles.layout", return_value={}), \
                 patch("railsim_mcp.profiles.events", return_value=[]):
                windows = reference_windows(root, {"RV_LML": dict(run_id="r")})
        self.assertEqual(windows["RV_LML"]["switch"], [(10, 10, 21), (520, 520, 531)])
        self.assertEqual(windows["RV_LML"]["turnback"], (30, 30, 510))
        loads = occupation_exclusion(
            {"RV_LML": 2}, {"RV_LML": ["RV_LML"]}, windows, 1800
        )
        self.assertEqual(loads["resource_minimum_seconds"]["switch"], 44)
        self.assertEqual(loads["resource_minimum_seconds"]["turnback"], 960)

    def test_periodic_multi_visit_cross_and_self_conflicts(self):
        for no_overlap in [False, True]:
            windows = {"R": {"switch": [(0, 0, 10), (40, 40, 50)]}}
            result = solve(["R", "R"], {"R": ["R"]}, windows, 1, set(), 1,
                           period=100, no_overlap=no_overlap)
            self.assertIn(result["status"], ["OPTIMAL", "FEASIBLE"])
            spans = [
                (t + grant + k * 100, t + release + k * 100)
                for _, t, _ in result["pattern"]
                for _, grant, release in windows["R"]["switch"]
                for k in range(-2, 3)
            ]
            for i, (start, end) in enumerate(spans):
                self.assertTrue(all(end <= a or b <= start for a, b in spans[i + 1:]))
            # A return visit can conflict with the *next repetition* of the same train.
            bad = {"R": {"switch": [(0, 0, 10), (95, 95, 105)]}}
            self.assertEqual(solve(["R"], {"R": ["R"]}, bad, 1, set(), 1,
                                   period=100, no_overlap=no_overlap)["status"], "INFEASIBLE")
            # A convex envelope would falsely exclude this feasible two-train case.
            sparse = {"R": {"switch": [(0, 0, 10), (80, 80, 90)]}}
            self.assertIn(solve(["R", "R"], {"R": ["R"]}, sparse, 1, set(), 1,
                                period=100, no_overlap=no_overlap)["status"],
                          ["OPTIMAL", "FEASIBLE"])

    def test_resource_guidance_excludes_multi_visit_orders_only(self):
        windows = {"R": {"switch": [(0, 0, 10), (40, 40, 50)],
                         "turnback": (10, 10, 40)}}
        result = catalogue(["R", "R"], {"R": ["R"]}, windows)
        self.assertEqual(result["resources"], {"turnback": ["t000", "t001"]})


class UC3GateTests(unittest.TestCase):
    def test_uc1_replay_retains_independent_volume_and_uc2_checks_routes(self):
        for use_case, mode_id in [("UC1", "MAINLINE/FV_PASS"), ("UC2", "METRO/BALANCED")]:
            with self.subTest(use_case=use_case), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                w = Workflow(
                    Settings(ROOT, root / "state"),
                    dict(use_case=use_case, building_block="BB1", mode=mode_id,
                         volumes=[4], runner="none"),
                    root / "output", ["test"],
                    simulator=Mock(return_value=(dict(passed=True, run_id="sim"), {})),
                )
                if use_case == "UC1":
                    variants = {p + "_P1": {"product": p} for p in w.mode.shares}
                    pattern = [[p, i * 100, p + "_P1"]
                               for i, p in enumerate(["FV", "FV", "RV", "RV", "GV"])]
                else:
                    variants = w.mode.variants
                    pattern = [[v, i * 100, v]
                               for i, v in enumerate(
                                   v for v, n in w.mode.composition(4).items() for _ in range(n)
                               )]
                w.calibrate = Mock(return_value=dict(
                    workspace="synthetic", variants=variants,
                    baselines={v: dict(terminal=100) for v in variants},
                ))
                w.export = Mock()
                path = root / "witness.json"
                witness = dict(use_case=use_case, building_block="BB1", mode=mode_id,
                               period_seconds=1800, pattern=pattern)
                path.write_text(json.dumps(witness))
                self.assertTrue(replay(w, path)["simulation"]["passed"])
                self.assertEqual(w.report["budget"]["simulations"], 1)
                if use_case == "UC2":
                    replacement = next(v for v in variants if v != pattern[0][2])
                    pattern[0] = [replacement, 0, replacement]
                    path.write_text(json.dumps(witness))
                    with self.assertRaisesRegex(ValueError, "composition"):
                        replay(w, path)

    def test_route_mismatch_cannot_pass_even_with_zero_delay(self):
        expected = {"RV_0": (3600, "RV_LML")}
        row = dict(vehicle_id="RV_0", route_id="RV_LML", status="terminal_arrived",
                   scheduled_departure=3600, planned_arrival=4800, actual_arrival=4800,
                   terminal_delay_seconds=0)
        run = dict(status="completed", trains=[row])
        self.assertTrue(evaluate(run, expected, {"RV_LML": 1200}, check_routes=True)["passed"])
        row["route_id"] = "RV_LMR"
        self.assertFalse(evaluate(run, expected, {"RV_LML": 1200}, check_routes=True)["passed"])

    def workflow(self, root):
        mode = RouteMode.load(SCENARIO / "uc3_operational_plan.json", "METRO/HIGH_REVERSAL")
        simulator = Mock(return_value=(dict(passed=True, run_id="sim"), {}))
        w = Workflow(Settings(ROOT, root / "state"),
                     config(mode=mode.id), root / "output", ["test"], simulator=simulator)
        ref = dict(workspace="synthetic", variants=mode.variants,
                   baselines={v: dict(terminal=1200) for v in mode.variants})
        w.calibrate = Mock(return_value=ref)
        w.export = Mock()
        return w

    def test_replay_checks_route_mix_and_counts_validation_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            w = self.workflow(root)
            path = root / "witness.json"
            witness = dict(use_case="UC3", building_block="BB1", mode=w.mode.id,
                           period_seconds=1800, pattern=[
                               ["RV", 0, "RV_LML"], ["RV", 600, "RV_LML"],
                               ["RV", 100, "RV_LMR"], ["RV", 200, "RV_RML"],
                           ])
            path.write_text(json.dumps(witness))
            self.assertTrue(replay(w, path)["simulation"]["passed"])
            self.assertEqual(
                [row[1] for row in w.report["replays"][0]["pattern"]], [0, 100, 200, 600]
            )
            self.assertEqual(w.report["budget"]["simulations"], 1)
            self.assertEqual(w.report["bounds"]["simulator_lower_bound_trains_per_hour"], 8)
            witness["pattern"][0][2] = "RV_LMR"
            path.write_text(json.dumps(witness))
            with self.assertRaisesRegex(ValueError, "composition"):
                replay(w, path)
            self.assertEqual(w.simulator.call_count, 1)
            w.budget.limits["simulations"] = 1
            witness["pattern"][0][2] = "RV_LML"
            path.write_text(json.dumps(witness))
            with self.assertRaises(Exhausted):
                replay(w, path)

    def test_solver_route_groups_are_mapped_to_products_for_simulator(self):
        with tempfile.TemporaryDirectory() as tmp:
            w = self.workflow(Path(tmp))
            w.config["volumes"] = [2]
            ref = w.calibrate.return_value
            ref["windows"] = {v: {"track": (0, 0, 10)} for v in w.mode.variants}
            pattern = [[v, i * 100, v]
                       for i, v in enumerate(["RV_LML", "RV_LML", "RV_LMR", "RV_RML"])]
            w.solver = Mock(return_value=dict(status="OPTIMAL", pattern=pattern))
            result = w.run()
            self.assertEqual(result["rates"][0]["trains_per_hour"], 8)
            self.assertEqual(result["rates"][0]["status"], "simulator_feasible")
            self.assertEqual(Counter(row[0] for row in w.simulator.call_args.args[2]),
                             {"RV": 4})


if __name__ == "__main__":
    unittest.main()
