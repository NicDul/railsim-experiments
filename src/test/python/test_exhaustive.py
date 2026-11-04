import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from railsim_mcp.capacity import save
from railsim_mcp.exhaustive import cyclic_entry_matches, evaluate_ordering, summarize
from railsim_mcp.flexibility import _ordering_id
import railsim_mcp.exhaustive as exhaustive


class ExhaustiveTests(unittest.TestCase):
    def test_entry_audit_uses_periodic_phase_not_departure_period_label(self):
        vehicles = ["first", "wrapped", "third"]
        products = {"first": "F", "wrapped": "G", "third": "R"}
        self.assertTrue(cyclic_entry_matches(
            ["F", "G", "R"], vehicles,
            {"first": 3815, "wrapped": 5715, "third": 4011},
            products, 1800,
        ))

    def setUp(self):
        self.order = {"entry": ["A", "B"]}
        self.ref = {
            "workspace": "calibrated",
            "variants": {"A_P1": {"product": "A"}, "B_P1": {"product": "B"}},
            "windows": {
                "A_P1": {"entry": (0, 0, 10)},
                "B_P1": {"entry": (0, 0, 10)},
            },
            "baselines": {"A_P1": {"terminal": 20}, "B_P1": {"terminal": 20}},
        }

    def test_one_second_gap_is_simulated_first_without_zero_gap_replay(self):
        pattern = [["A", 0, "A_P1"], ["B", 10, "B_P1"]]
        solver = Mock(return_value={"status": "OPTIMAL", "slot_pattern": pattern})
        passed = {"passed": True, "max_delay": 0, "total_delay": 0, "missing": 0, "detours": 0}
        simulator = Mock(return_value=(passed, {}))
        with patch("railsim_mcp.exhaustive.audit_simulation", return_value={"verified": True}):
            result = evaluate_ordering(
                self.order, self.ref, Mock(), solver=solver, simulator=simulator
            )
        self.assertEqual(result["status"], "works")
        self.assertEqual(len(result["simulations"]), 1)
        self.assertEqual([call.kwargs["min_gap"] for call in solver.call_args_list], [1])
        self.assertEqual(result["simulations"][0]["score"]["max_delay"], 0)
        self.assertFalse(result["gap1_infeasible_zero_feasible"])

    def test_one_second_infeasible_falls_back_to_zero_gap_but_rejection_is_unresolved(self):
        pattern = [["A", 0, "A_P1"], ["B", 10, "B_P1"]]
        solver = Mock(side_effect=[
            {"status": "INFEASIBLE"},
            {"status": "OPTIMAL", "slot_pattern": pattern},
        ])
        simulator = Mock(return_value=(
            {"passed": False, "max_delay": 1, "total_delay": 2, "missing": 0, "detours": 0},
            {},
        ))
        with patch("railsim_mcp.exhaustive.audit_simulation", return_value={}):
            result = evaluate_ordering(
                self.order, self.ref, Mock(), solver=solver, simulator=simulator
            )
        self.assertEqual(result["status"], "unresolved")
        self.assertEqual(result["solver_status"], "OPTIMAL")
        self.assertEqual(simulator.call_count, 1)
        self.assertTrue(result["gap1_infeasible_zero_feasible"])
        self.assertEqual([call.kwargs["min_gap"] for call in solver.call_args_list], [1, 0])

    def test_one_second_unknown_is_not_promoted_to_zero_gap_negative(self):
        solver = Mock(return_value={"status": "UNKNOWN"})
        simulator = Mock()
        result = evaluate_ordering(self.order, self.ref, Mock(), solver=solver, simulator=simulator)
        self.assertEqual(result["status"], "unresolved")
        self.assertEqual([call.kwargs["min_gap"] for call in solver.call_args_list], [1, 1])
        simulator.assert_not_called()

    def test_one_second_feasible_rejection_does_not_weaken_raw_gate(self):
        pattern = [["A", 0, "A_P1"], ["B", 11, "B_P1"]]
        solver = Mock(return_value={"status": "OPTIMAL", "slot_pattern": pattern})
        simulator = Mock(return_value=(
            {"passed": True, "max_delay": 1, "total_delay": 2, "missing": 0, "detours": 0},
            {},
        ))
        with patch("railsim_mcp.exhaustive.audit_simulation", return_value={}):
            result = evaluate_ordering(self.order, self.ref, Mock(), solver=solver, simulator=simulator)
        self.assertEqual(result["status"], "unresolved")
        self.assertEqual(solver.call_count, 1)

    def test_zero_gap_infeasible_is_not_simulated_unknown_is_not_no(self):
        simulator = Mock()
        for status in ("INFEASIBLE", "UNKNOWN"):
            result = evaluate_ordering(
                self.order, self.ref, Mock(),
                solver=Mock(return_value={"status": status}), simulator=simulator,
            )
            self.assertEqual(result["status"], "conditional_no" if status == "INFEASIBLE" else "unresolved")
        simulator.assert_not_called()

    def test_unique_results_partition_denominator_and_reject_wrong_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            directory = output / "orders/2"
            directory.mkdir(parents=True)
            key = _ordering_id(self.order)
            path = directory / (key + ".json")
            save(path, {
                "ordering_id": key,
                "ordering": self.order,
                "status": "works",
                "solver_status": "OPTIMAL",
                "simulations": [{"score": {"passed": True, "max_delay": 0, "total_delay": 0, "missing": 0, "detours": 0}}],
            })
            rows = summarize(output, [(2, self.order)])
            self.assertEqual(rows[0]["denominator"], 1)
            self.assertEqual(rows[0]["works"], 1)
            self.assertEqual(rows[0]["simulated"], 1)
            self.assertEqual(rows[0]["bounds"], [1, 1])
            record = json.loads(path.read_text())
            save(path, record | {"ordering": {"entry": ["B", "A"]}})
            with self.assertRaisesRegex(ValueError, "identity"):
                summarize(output, [(2, self.order)])

    def test_worker_project_state_and_calibration_copies_are_isolated(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "source"
            for directory in (".mvn", "src", "target"):
                (project / directory).mkdir(parents=True)
            (project / "pom.xml").write_text("<project/>")
            (project / "mvnw").write_text("runner")
            workspace = root / "calibrated"
            workspace.mkdir()
            (workspace / "network.xml").write_text("<network/>")
            ref = {
                "project": str(project),
                "workspace": "calibrated",
                "workspace_path": str(workspace),
                "workspace_hashes": {"network.xml": exhaustive.digest(workspace / "network.xml")},
            }
            results = []
            previous = exhaustive._WORKER
            try:
                for worker in ("spawn1", "spawn2"):
                    with patch("railsim_mcp.exhaustive.multiprocessing.current_process") as process:
                        process.return_value.name = worker
                        exhaustive._initialize_worker(root / "output", ref)
                    results.append(exhaustive._WORKER)
                self.assertNotEqual(results[0][1].project_root, results[1][1].project_root)
                self.assertNotEqual(results[0][1].runs_root, results[1][1].runs_root)
                first = results[0][1].workspace_root / "calibrated/network.xml"
                second = results[1][1].workspace_root / "calibrated/network.xml"
                first.write_text("<changed/>")
                self.assertEqual(second.read_text(), "<network/>")
                self.assertEqual((workspace / "network.xml").read_text(), "<network/>")
            finally:
                exhaustive._WORKER = previous


if __name__ == "__main__":
    unittest.main()
