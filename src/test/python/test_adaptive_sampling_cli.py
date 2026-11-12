import contextlib
from collections import Counter
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from railsim_mcp import adaptive_sampling_cli
from railsim_mcp.uc1_modes import UC1Mode


class FreshRunCliTests(unittest.TestCase):
    def test_config_preserves_the_global_51_point_family_and_pilot_domain(self):
        config = {
            "use_case": "UC1",
            "building_blocks": ["BB1", "BB2", "BB3"],
            "mode": "MAINLINE/FV_PASS",
            "period_seconds": 1800,
            "volumes_by_building_block": {
                "BB1": list(range(1, 14)),
                "BB2": list(range(1, 19)),
                "BB3": list(range(1, 21)),
            },
            "seed": 20261112,
            "solver_seed": 4711,
            "comparison_points": 51,
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps(config))
            fresh = adaptive_sampling_cli._load_experiment_config(
                path, full_fresh=True
            )
            with self.assertRaisesRegex(ValueError, "only by fresh-run"):
                adaptive_sampling_cli._load_experiment_config(path)
        self.assertEqual(
            sum(map(len, fresh["volumes_by_building_block"].values())),
            51,
        )
        self.assertEqual(fresh["comparison_points"], 51)

    def test_fresh_run_stop_drains_and_resume_uses_only_missing_cached_orders(self):
        class FakeWorkflow:
            def __init__(self, settings, config, output, invocation, minimum_volume):
                self.config = config
                self.identity = {"config": config, "source": "fake"}
                self.report = {
                    "source_revision": "fake-revision",
                    "source_dirty": False,
                    "budget": {
                        "wall_seconds": 0.1,
                        "cpu_seconds": 0.1,
                        "simulations": 3,
                    },
                }

            def calibrate(self):
                return {
                    "variants": {"FV_P1": {"product": "FV"}},
                    "windows": {},
                    "building_block": self.config["building_block"],
                }

            def persist(self):
                return None

        class FakeProvider:
            instances = []
            evaluated = []

            def __init__(
                self,
                reference,
                *,
                stop_file,
                on_admit,
                on_result,
                model_identity_sha256,
                calibration_sha256,
                **kwargs,
            ):
                self.stop_file = Path(stop_file)
                self.on_admit = on_admit
                self.on_result = on_result
                self.last_batch_stop_reason = None
                self.active_worker_count = 0
                self.max_active_worker_count = 0
                self.active_worker_process_count = 0
                self.max_active_worker_process_count = 0
                self.worker_process_count = (
                    1 if reference["building_block"] == "BB1" else 2
                )
                self.last_batch_requested_by_volume = Counter()
                self.last_batch_admitted_by_volume = Counter()
                self.last_batch_completed_by_volume = Counter()
                self.stop_after_first = not self.instances
                self.identity = {
                    "kind": "test-provider",
                    "model_identity_sha256": model_identity_sha256,
                    "calibration_sha256": calibration_sha256,
                }
                self.instances.append(self)

            def evaluate_batch(self, tasks):
                self.last_batch_stop_reason = None
                self.last_batch_requested_by_volume = Counter(
                    task["case"]["volume"] for task in tasks
                )
                self.last_batch_admitted_by_volume = Counter()
                self.last_batch_completed_by_volume = Counter()
                for task in tasks:
                    if self.stop_file.exists():
                        self.last_batch_stop_reason = "stop_requested"
                        return
                    self.active_worker_count = 1
                    self.max_active_worker_count = 1
                    self.active_worker_process_count = self.worker_process_count
                    self.max_active_worker_process_count = max(
                        self.max_active_worker_process_count,
                        self.worker_process_count,
                    )
                    volume = task["case"]["volume"]
                    self.last_batch_admitted_by_volume[volume] += 1
                    self.on_admit(task)
                    self.last_batch_completed_by_volume[volume] += 1
                    self.evaluated.append(task["ordering_id"])
                    yield task, {
                        "outcome": "feasible",
                        "source": "fresh_solver",
                        "budget_label": "test wall-time",
                        "attempt_count": 1,
                        "cpu_seconds": 0.01,
                        "wall_seconds": 0.01,
                        "provider_wall_seconds": 0.01,
                        "worker_pid": os.getpid(),
                        "worker_nice": 19,
                    }
                    if self.stop_after_first:
                        self.stop_file.touch()
                        self.last_batch_stop_reason = "stop_requested"
                        self.active_worker_count = 0
                        self.active_worker_process_count = 0
                        return
                self.active_worker_count = 0
                self.active_worker_process_count = 0

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            scenario = (
                project / "src/main/resources/scenarios/use_case_1"
            )
            scenario.mkdir(parents=True)
            plan = scenario / "uc1_operational_plan.json"
            plan.write_text("{}\n")
            for block in ("BB1", "BB2"):
                directory = scenario / f"building_block_{block[-1]}"
                directory.mkdir()
                (directory / "config.xml").write_text("<config/>\n")
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "use_case": "UC1",
                        "building_blocks": ["BB1", "BB2"],
                        "mode": "MAINLINE/FV_PASS",
                        "period_seconds": 1800,
                        "volumes_by_building_block": {
                            "BB1": [3],
                            "BB2": [3],
                        },
                        "seed": 20261112,
                        "solver_seed": 4711,
                        "checkpoints": [100],
                        "target_width": 1.0,
                        "comparison_points": 51,
                    }
                )
            )
            output = root / "fresh-run"
            state_root = root / "state"
            args = [
                "railsim-adaptive-flexibility",
                "fresh-run",
                "--config",
                str(config_path),
                "--project",
                str(project),
                "--state-root",
                str(state_root),
                "--output",
                str(output),
            ]
            fake_settings = SimpleNamespace(project=project, state=state_root)
            fake_snapshot = ("fake-revision", False, {"scenario": "sha256"})
            providers = patch.object(
                adaptive_sampling_cli,
                "LocalFixedProfileSolverProvider",
                FakeProvider,
            )
            with (
                patch.object(adaptive_sampling_cli.sys, "argv", args),
                patch.object(
                    adaptive_sampling_cli.Settings,
                    "load",
                    return_value=fake_settings,
                ),
                patch.object(
                    adaptive_sampling_cli,
                    "Workflow",
                    FakeWorkflow,
                ),
                patch.object(
                    adaptive_sampling_cli,
                    "_source_snapshot",
                    return_value=fake_snapshot,
                ),
                patch.object(
                    adaptive_sampling_cli,
                    "_mode",
                    return_value=(UC1Mode.legacy_default(), plan.resolve()),
                ),
                patch.object(
                    adaptive_sampling_cli,
                    "_environment",
                    return_value={"python": "test", "java": "test"},
                ),
                patch.object(
                    adaptive_sampling_cli,
                    "_ensure_simulator_options",
                ),
                patch.object(
                    adaptive_sampling_cli,
                    "order_groups",
                    return_value={"entry"},
                ),
                providers,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                adaptive_sampling_cli.main()
            first = json.loads((output / "run-report.json").read_text())
            self.assertEqual(first["status"], "stopped")
            self.assertEqual(
                first["execution"]["completed_unique_orderings"], 1
            )
            self.assertEqual(
                first["results"][0]["stop_reason"], "stop_requested"
            )
            self.assertTrue((output / "STOP").is_file())
            self.assertEqual(len(FakeProvider.evaluated), 1)

            resumed_args = args + ["--resume"]
            with (
                patch.object(adaptive_sampling_cli.sys, "argv", resumed_args),
                patch.object(
                    adaptive_sampling_cli.Settings,
                    "load",
                    return_value=fake_settings,
                ),
                patch.object(
                    adaptive_sampling_cli,
                    "Workflow",
                    FakeWorkflow,
                ),
                patch.object(
                    adaptive_sampling_cli,
                    "_source_snapshot",
                    return_value=fake_snapshot,
                ),
                patch.object(
                    adaptive_sampling_cli,
                    "_mode",
                    return_value=(UC1Mode.legacy_default(), plan.resolve()),
                ),
                patch.object(
                    adaptive_sampling_cli,
                    "_environment",
                    return_value={"python": "test", "java": "test"},
                ),
                patch.object(
                    adaptive_sampling_cli,
                    "_ensure_simulator_options",
                ),
                patch.object(
                    adaptive_sampling_cli,
                    "order_groups",
                    return_value={"entry"},
                ),
                providers,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                adaptive_sampling_cli.main()
            final = json.loads((output / "run-report.json").read_text())
            progress = json.loads((output / "progress.json").read_text())
            self.assertFalse((output / "STOP").exists())
            self.assertEqual(final["status"], "complete")
            self.assertEqual(final["precision_reached_points"], 2)
            self.assertEqual(final["sampling_family_points"], 51)
            self.assertEqual(final["results"][0]["checkpoint"], 100)
            self.assertAlmostEqual(
                final["results"][0]["interval"]["per_endpoint_alpha"],
                0.05 / (2 * 51),
            )
            self.assertEqual(
                progress["totals"]["completed_unique_orderings"], 4
            )
            self.assertLessEqual(
                progress["totals"]["max_active_worker_process_count"], 2
            )
            self.assertEqual(
                progress["building_blocks"]["BB1"][
                    "max_active_worker_process_count"
                ],
                1,
            )
            self.assertEqual(
                progress["building_blocks"]["BB2"][
                    "max_active_worker_process_count"
                ],
                2,
            )
            self.assertIn(19, progress["totals"]["worker_nice_values"])
            self.assertTrue((output / "fv-pass-results.md").is_file())
            self.assertTrue((output / "fv-pass-overlay.svg").is_file())


class FreshResultsPresentationTests(unittest.TestCase):
    def test_overlay_is_line_only_and_does_not_bridge_missing_rates(self):
        config = {"mode": "MAINLINE/FV_PASS", "period_seconds": 1800}
        building_blocks = [("BB1", [1, 2, 3])]

        def case(volume):
            return {
                "case": {"volume": volume},
                "checkpoints": [
                    {
                        "interval": {
                            "lower": 0.2 + volume / 100,
                            "upper": 0.4 + volume / 100,
                        }
                    }
                ],
            }

        with_gap = adaptive_sampling_cli._fresh_results_svg(
            config,
            building_blocks,
            {"BB1": {"cases": [case(1), case(3)]}},
        )
        continuous = adaptive_sampling_cli._fresh_results_svg(
            config,
            building_blocks,
            {"BB1": {"cases": [case(1), case(2), case(3)]}},
        )
        self.assertNotIn("<circle", with_gap)
        self.assertNotIn(" L ", with_gap)
        self.assertNotIn("<circle", continuous)
        self.assertIn(" L ", continuous)


if __name__ == "__main__":
    unittest.main()
