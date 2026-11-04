import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from railsim_mcp.capacity import save
from railsim_mcp.exhaustive import digest
from railsim_mcp.flexibility import _ordering_id
from railsim_mcp.solver_curve import import_calibration, summarize


class SolverCurveTests(unittest.TestCase):
    def test_summary_requires_saved_witness_and_never_promotes_to_validated(self):
        order = {"block_1": ["FV"]}
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            (output / "solver-results/1").mkdir(parents=True)
            path = output / "solver-results/1" / (_ordering_id(order) + ".json")
            record = {
                "volume": 1, "ordering_id": _ordering_id(order), "ordering": order,
                "identity_sha256": "identity", "status": "solver_feasible",
                "slot_pattern": [["FV", 0, "FV_P1"]], "simulations": [],
                "simulator_calls": 0, "wall_seconds": 0.1, "cpu_seconds": 0.1,
            }
            save(path, record)
            report = summarize(output, [(1, order)], "identity")
            self.assertTrue(report["model_only"])
            self.assertTrue(report["complete"])
            self.assertEqual(report["cases"][0]["model_feasible_percent"], 100)
            self.assertEqual(report["simulator_calls"], 0)
            save(path, record | {"slot_pattern": []})
            with self.assertRaisesRegex(ValueError, "saved witness"):
                summarize(output, [(1, order)], "identity")
            save(path, record | {"identity_sha256": "changed"})
            with self.assertRaisesRegex(ValueError, "identity"):
                summarize(output, [(1, order)], "identity")

    def test_calibration_import_checks_physics_evidence_and_no_rerouting(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project, state, output = root / "project", root / "state", root / "output"
            for path in (project, state, output):
                path.mkdir()
            physics = project / "src/main/java/Physics.java"
            physics.parent.mkdir(parents=True)
            physics.write_text("unchanged")
            scenario = project / "scenario.xml"
            scenario.write_text("<scenario/>")
            workspace = state / "workspaces/calibrated"
            workspace.mkdir(parents=True)
            (workspace / "network.xml").write_text("<network/>")
            run = state / "runs/baseline"
            run.mkdir(parents=True)
            save(run / "run.json", {"trains_arrived": 1, "trains_expected": 1, "events_file": "unused"})
            reference = {
                "workspace": "calibrated",
                "workspace_hashes": {"network.xml": digest(workspace / "network.xml")},
                "baselines": {"FV_P1": {"run_id": "baseline"}},
                "evidence_hashes": {"baseline/run.json": digest(run / "run.json")},
                "windows": {"FV_P1": {"block_1": [0, 0, 10]}},
            }
            source = root / "checkpoint.json"
            save(source, {
                "identity": {
                    "config": {"use_case": "UC1", "building_block": "BB3", "mode": "MAINLINE/FV_STOP",
                               "period_seconds": 1800, "seed": 4711},
                    "inputs": {"scenario.xml": digest(scenario)},
                    "code": {"src/main/java/Physics.java": digest(physics)},
                    "runtime": {}, "state": str(state),
                },
                "calibration": reference,
            })
            with patch("railsim_mcp.solver_curve.reference_windows", return_value=reference["windows"]), \
                 patch("railsim_mcp.solver_curve.events", return_value=iter([])), \
                 patch.dict("os.environ", {}, clear=True):
                imported = import_calibration(project, source, output)
                self.assertEqual(imported, reference)
                self.assertEqual(json.loads((output / "calibration/origin.json").read_text())["new_simulator_calls"], 0)
                (output / "calibration/runs/baseline/run.json").write_text("corrupt")
                with self.assertRaisesRegex(ValueError, "copied calibration evidence"):
                    import_calibration(project, source, output)
                physics.write_text("changed")
                with self.assertRaisesRegex(ValueError, "physics"):
                    import_calibration(project, source, output)
