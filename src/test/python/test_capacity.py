"""Behavior tests at workflow boundaries; synthetic models never use provider quota."""

import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from railsim_mcp.capacity import (
    Workflow,
    assemble,
    occupation_exclusion,
    proposal,
    validate_config,
    digest,
)
from railsim_mcp.configuration import Settings
from railsim_mcp.solver import solve
from railsim_mcp.timetable import replace_timetable

ROOT = Path(__file__).resolve().parents[3]


def config(**kwargs):
    return dict(
        building_block="BB1",
        mode="MAINLINE/FV_PASS",
        volumes=[4],
        runner="fixture",
        rounds=2,
        proposals=3,
        **kwargs,
    )


class ProposalTests(unittest.TestCase):
    def test_capacity_volume_floor_remains_four(self):
        with self.assertRaises(ValueError):
            validate_config(config() | {"volumes": [1]})

        extended = validate_config(
            config() | {"volumes": [1, 2, 3]}, minimum_volume=1
        )
        self.assertEqual(extended["volumes"], [1, 2, 3])

    def test_rotation_dedup_includes_platform_assignment(self):
        opts = {"FV": ["FV_P1", "FV_P2"], "RV": ["RV_P1"], "GV": ["GV_P1"]}
        counts = {"FV": 2, "RV": 1, "GV": 1}
        a = proposal(dict(order="FFRG", platforms=[]), counts, opts)
        b = proposal(dict(order="FRGF", platforms=[]), counts, opts)
        self.assertEqual(a[2], b[2])
        self.assertNotEqual(
            a[2],
            proposal(
                dict(order="FFRG", platforms=["P2", "any", "any", "any"]), counts, opts
            )[2],
        )
        for bad in [
            None,
            {},
            dict(order="FFFF"),
            dict(order="FFRG", platforms="P1"),
            dict(order="FFRG", platforms=[7] * 4),
            dict(order="FFRG", platforms=["P9"] * 4),
        ]:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                proposal(bad, counts, opts)

    def test_config_budget_validation(self):
        for change in [
            dict(period_seconds=0),
            dict(rounds=-1),
            dict(volumes=[True]),
            dict(solver_seconds=float("nan")),
            dict(workers=0),
            dict(runner="anthropic"),
        ]:
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_config(config() | change)


class SolverTests(unittest.TestCase):
    def test_non_default_period_wrap_and_self_overlap(self):
        opts = {"FV": ["FV_P1"]}
        for duration, status in [(50, "OPTIMAL"), (51, "INFEASIBLE")]:
            result = solve(
                ["FV", "FV"],
                opts,
                {"FV_P1": {"track": (0, 0, duration)}},
                2,
                frozenset(),
                1,
                period=100,
            )
            self.assertEqual(result["status"], status)
        self.assertEqual(
            solve(
                ["FV"],
                opts,
                {"FV_P1": {"track": (0, 0, 101)}},
                2,
                frozenset(),
                1,
                period=100,
            )["status"],
            "INFEASIBLE",
        )

    def test_order_and_platform_constraints_and_positive_control(self):
        windows = {
            "FV_P1": {"entry": (0, 0, 5), "platform1": (5, 5, 15)},
            "RV_P1": {"entry": (0, 0, 5), "platform1": (5, 5, 15)},
        }
        result = solve(
            ["FV", "RV"],
            {"FV": ["FV_P1"], "RV": ["RV_P1"]},
            windows,
            2,
            frozenset(),
            1,
            period=100,
            order=["FV", "RV"],
            platforms=["P1", "P1"],
            entry="entry",
        )
        self.assertEqual(result["status"], "OPTIMAL")
        pattern = result["pattern"]
        self.assertEqual(len(pattern), 2)
        # Independent pairwise periodic overlap check on the witness.
        for i, (_, a, va) in enumerate(pattern):
            for j, (_, b, vb) in enumerate(pattern):
                for shift in (-100, 0, 100):
                    if i == j and not shift:
                        continue
                    for resource in windows[va].keys() & windows[vb].keys():
                        _, g, r = windows[va][resource]
                        _, h, s = windows[vb][resource]
                        self.assertGreaterEqual(
                            max(a + g, b + h + shift), min(a + r, b + s + shift)
                        )

    def test_occupation_bound_minimises_over_platform_choices(self):
        result = occupation_exclusion(
            {"FV": 3},
            {"FV": ["FV_P1", "FV_P2"]},
            {
                "FV_P1": {"p1": (0, 0, 99), "track": (0, 0, 40)},
                "FV_P2": {"p2": (0, 0, 99), "track": (0, 0, 50)},
            },
            100,
        )
        self.assertEqual(result["excluded_resources"], {"track": 120})


class BoundsTests(unittest.TestCase):
    def test_unknown_failed_simulation_and_one_order_do_not_bound_capacity(self):
        rows = [
            dict(volume=4, trains_per_hour=8, witness={"run": "pass"}, trials=[]),
            dict(
                volume=5,
                trains_per_hour=10,
                witness=None,
                composition={"FV": 5},
                trials=[
                    dict(id=0, scope="one_order", solver={"status": "INFEASIBLE"}),
                    dict(id=1, scope="all_orders", solver={"status": "UNKNOWN"}),
                ],
            ),
        ]
        result = assemble({"rates": rows})
        self.assertEqual(result["simulator_lower_bound_trains_per_hour"], 8)
        self.assertEqual(result["fixed_profile_exclusions"], [])
        self.assertIsNone(result["maximum"])
        rows[1]["trials"].append(
            dict(id=2, scope="all_orders", solver={"status": "INFEASIBLE"})
        )
        self.assertEqual(len(assemble({"rates": rows})["fixed_profile_exclusions"]), 1)
        self.assertIsNone(assemble({"rates": rows})["maximum"])


class ReplayBoundsTests(unittest.TestCase):
    def test_validated_replay_is_an_available_bound_with_explicit_origin(self):
        report = dict(
            identity={"config": {"period_seconds": 1800}},
            rates=[],
            replays=[
                dict(
                    pattern=[["FV", 0, "FV_P1"]] * 4,
                    simulation={"passed": True},
                    evidence_label="explicit replay",
                )
            ],
        )
        result = assemble(report)
        self.assertEqual(result["simulator_lower_bound_trains_per_hour"], 8)
        self.assertEqual(result["best_witness_origin"], "explicit_replay")
        self.assertIsNone(result["search_lower_bound_trains_per_hour"])
        self.assertIsNone(result["maximum"])


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.out = self.root / "output"
        self.settings = Settings(ROOT, self.root / "state")
        self.ref = dict(
            workspace="synthetic",
            variants={
                "FV_P1": {"product": "FV"},
                "RV_P1": {"product": "RV"},
                "GV_P1": {"product": "GV"},
            },
            baselines={},
            windows={
                v: {"entry": (0, 0, 3), "platform1": (4, 4, 8)}
                for v in ["FV_P1", "RV_P1", "GV_P1"]
            },
        )

    def make(self, solver, simulator=None, model=None, conf=None):
        w = Workflow(
            self.settings,
            conf or config(),
            self.out,
            ["example"],
            solver=solver,
            simulator=simulator,
            model=model,
        )
        w.calibrate = Mock(return_value=copy.deepcopy(self.ref))
        w.export = Mock()
        return w

    def test_feedback_invalid_duplicate_unknown_and_resume_budget(self):
        solver = Mock(return_value={"status": "UNKNOWN"})
        model = Mock(
            return_value={
                "proposals": [
                    {"order": "bad"},
                    {"order": "FFRG", "platforms": []},
                    {"order": "FRGF", "platforms": []},
                ]
            }
        )
        workflow = self.make(solver, model=model)
        report = workflow.run()
        self.assertEqual(
            solver.call_count, 2
        )  # unrestricted plus one distinct cyclic order
        self.assertEqual(model.call_count, 2)
        self.assertEqual(report["model_calls"], 2)
        statuses = [
            r["status"]
            for record in report["rates"][0]["rounds"]
            for r in record["results"]
        ]
        self.assertIn("invalid_proposal", statuses)
        self.assertIn("duplicate", statuses)
        self.assertIsNone(report["bounds"]["maximum"])
        resumed = self.make(solver, model=model)
        resumed.run()
        self.assertEqual(solver.call_count, 2)
        self.assertEqual(model.call_count, 2)
        self.assertEqual(
            model.call_args.args[0]["feedback"][0]["solver"]["status"], "UNKNOWN"
        )
        with self.assertRaisesRegex(ValueError, "resume"):
            self.make(solver, conf=config() | {"seed": 5})

    def test_simulator_rejection_feedback_then_pass_retains_witness(self):
        pattern = [
            ["FV", 0, "FV_P1"],
            ["FV", 50, "FV_P1"],
            ["RV", 100, "RV_P1"],
            ["GV", 150, "GV_P1"],
        ]
        solver = Mock(return_value=dict(status="OPTIMAL", pattern=pattern))
        simulator = Mock(
            side_effect=[
                ({"passed": False, "max_delay": 1, "run_id": "late"}, {}),
                ({"passed": True, "max_delay": 0, "run_id": "good"}, {}),
            ]
        )
        model = Mock(return_value={"proposals": [dict(order="FRFG", platforms=[])]})
        report = self.make(solver, simulator, model).run()
        self.assertEqual(report["bounds"]["simulator_lower_bound_trains_per_hour"], 8)
        self.assertEqual(
            report["bounds"]["best_witness"]["simulation"]["run_id"], "good"
        )
        self.assertEqual(
            model.call_args.args[0]["feedback"][0]["status"], "simulator_rejected"
        )
        self.assertEqual(model.call_count, 1)

    def test_simulator_failure_and_solver_model_invalid_are_unresolved(self):
        for solved, sim in [
            (dict(status="MODEL_INVALID"), None),
            (
                dict(status="OPTIMAL", pattern=[]),
                Mock(side_effect=RuntimeError("sim failed")),
            ),
        ]:
            with self.subTest(solved=solved):
                # Fresh output for each independent budget.
                self.out = self.root / str(len(list(self.root.iterdir())))
                report = self.make(
                    Mock(return_value=solved),
                    simulator=sim,
                    conf=config() | {"runner": "none"},
                ).run()
                self.assertIsNone(
                    report["bounds"]["simulator_lower_bound_trains_per_hour"]
                )
                self.assertEqual(report["bounds"]["unresolved_volumes"], [4])

    def test_model_timeout_spends_round_and_continues_other_rates(self):
        import subprocess

        self.out = self.root / "timeout"
        report = self.make(
            Mock(return_value={"status": "UNKNOWN"}),
            model=Mock(side_effect=subprocess.TimeoutExpired("mock", 1)),
            conf=config() | {"rounds": 1, "volumes": [4, 5]},
        ).run()
        self.assertEqual(
            [r["status"] for row in report["rates"] for r in row["rounds"]],
            ["model_timeout", "model_timeout"],
        )
        self.assertEqual(report["model_calls"], 2)

    def test_portable_prompt_and_manifest(self):
        w = self.make(
            Mock(return_value={"status": "UNKNOWN"}), conf=config() | {"runner": "none"}
        )
        w.run()
        w.report["calibration"] = copy.deepcopy(self.ref)
        # Execute real exporter over a tiny synthetic retained run.
        source = w.ws.runs_root / "run"
        (source / "inputs").mkdir(parents=True)
        (source / "run.json").write_text("{}")
        (source / "inputs" / "network.xml").write_text("<network/>")
        (source / "inputs" / "network.xml").chmod(0o444)
        w.report["calibration"]["baselines"] = {"FV_P1": dict(run_id="run")}
        Workflow.export(w)
        Workflow.export(w)  # idempotent with read-only frozen inputs
        handoff = self.out / "proof-handoff"
        manifest = json.loads((handoff / "manifest.json").read_text())
        self.assertTrue(all(digest(handoff / p) == sha for p, sha in manifest.items()))
        prompt = (handoff / "prompt.md").read_text()
        self.assertIn("No coverage", prompt)
        self.assertIn("conjectures separately", prompt)
        self.assertTrue((handoff / "runs/run/inputs/network.xml").exists())

    def test_timetable_operation_has_explicit_workspace(self):
        ws = Mock()
        ws._seconds.side_effect = lambda value, label: int(value)
        replace_timetable(
            ws,
            "test",
            [
                dict(
                    route_id="r",
                    departure_id="d",
                    vehicle_id="v",
                    departure_time="5",
                    vehicle_type="FV",
                )
            ],
            2,
            600,
        )
        rows = ws.edit.call_args.args[1][0]["departures"]
        self.assertEqual(rows[1]["vehicle_id"], "v_p1")
        self.assertEqual(rows[1]["departure_time"], "00:10:05.000")

    def test_no_protocol_dependency_or_server_import(self):
        self.assertIsNone(importlib.util.find_spec("mcp"))
        self.assertFalse((ROOT / "src/main/python/railsim_mcp/server.py").exists())


class TransferTests(unittest.TestCase):
    def test_declared_uc2_modes_preserve_directions_and_volume(self):
        from railsim_mcp.routing import RouteMode, topology

        path = ROOT / "src/main/resources/scenarios/use_case_2"
        for mode in [
            "TRANSIT/BALANCED",
            "TRANSIT/TRUNK",
            "METRO/BALANCED",
            "METRO/TRUNK",
        ]:
            routing = RouteMode.load(path / "uc2_operational_plan.json", mode)
            for n in [4, 5, 6, 9]:
                counts = routing.composition(n)
                self.assertEqual(sum(counts.values()), 2 * n)
                forward = sum(
                    k
                    for v, k in counts.items()
                    if routing.variants[v]["direction"] == "forward"
                )
                self.assertEqual(forward, n)
                self.assertTrue(set(counts) <= set(routing.variants))
            for bb in [1, 2]:
                result = topology(path / f"building_block_{bb}", routing.variants)
                self.assertFalse(result["ordered_guidance_supported"])
                self.assertEqual(result["platform_resources"], [])
                self.assertGreater(result["entry_markers"], 1)

    def test_full_model_infeasibility_never_calls_model(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            w = Workflow(
                Settings(ROOT, root / "state"),
                config(),
                root / "out",
                ["test"],
                solver=Mock(return_value={"status": "INFEASIBLE"}),
                model=Mock(),
            )
            w.calibrate = Mock(
                return_value=dict(
                    variants={
                        "FV_P1": {"product": "FV"},
                        "RV_P1": {"product": "RV"},
                        "GV_P1": {"product": "GV"},
                    },
                    windows={
                        v: {"track": (0, 0, 3)} for v in ["FV_P1", "RV_P1", "GV_P1"]
                    },
                )
            )
            w.export = Mock()
            result = w.run()
            w.model.assert_not_called()
            self.assertEqual(
                result["rates"][0]["escalation_reason"],
                "full_model_infeasible_cannot_be_repaired_by_order",
            )
            self.assertEqual(len(result["bounds"]["fixed_profile_exclusions"]), 1)
            self.assertIsNone(result["bounds"]["maximum"])

    def test_interrupted_solver_witness_spends_budget_without_resimulation(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            settings = Settings(ROOT, root / "state")
            out = root / "out"
            w = Workflow(settings, config() | {"runner": "none"}, out, ["test"])
            w.report["rates"] = [
                dict(
                    volume=4,
                    trains_per_hour=8,
                    composition={"FV": 2, "RV": 1, "GV": 1},
                    trials=[
                        dict(
                            id=0,
                            status="solver_optimal",
                            scope="all_orders",
                            solver={
                                "status": "OPTIMAL",
                                "pattern": [["FV", 0, "FV_P1"]],
                            },
                        )
                    ],
                    rounds=[],
                    tried=[],
                    witness=None,
                )
            ]
            w.persist()
            resumed = Workflow(settings, config() | {"runner": "none"}, out, ["test"])
            self.assertEqual(
                resumed.report["rates"][0]["trials"][0]["status"], "interrupted"
            )
