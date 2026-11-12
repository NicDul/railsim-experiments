import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from railsim_mcp.adaptive_sampling import (
    DEFAULT_CHECKPOINTS,
    AdaptiveSampler,
    LocalFixedProfileSolverProvider,
    SavedSolverResultProvider,
    SamplingCase,
    SolverResultSource,
    _solve_fixed_order,
    conservative_interval,
    draw_orderings,
    one_sided_binomial_lower,
    one_sided_binomial_upper,
    ordering_id,
    run_fixed_pilot,
)


class BinomialBoundTests(unittest.TestCase):
    def test_one_sided_limits_match_independent_reference_values(self):
        alpha = 1 / 1120
        self.assertEqual(one_sided_binomial_lower(0, 100, alpha), 0)
        self.assertAlmostEqual(
            one_sided_binomial_upper(0, 100, alpha),
            0.067802744952,
            places=12,
        )
        self.assertAlmostEqual(
            one_sided_binomial_lower(50, 100, alpha),
            0.343250174343,
            places=12,
        )
        self.assertAlmostEqual(
            one_sided_binomial_upper(50, 100, alpha),
            0.656749825657,
            places=12,
        )
        self.assertAlmostEqual(
            one_sided_binomial_lower(3200, 6400, alpha),
            0.480406818243,
            places=12,
        )
        self.assertAlmostEqual(
            one_sided_binomial_upper(3200, 6400, alpha),
            0.519593181757,
            places=12,
        )

    def test_boundaries_use_stable_closed_forms(self):
        alpha = 1 / 1120
        self.assertAlmostEqual(
            one_sided_binomial_lower(6400, 6400, alpha),
            0.998903557164,
            places=12,
        )
        self.assertAlmostEqual(
            one_sided_binomial_upper(0, 6400, alpha),
            0.001096442836,
            places=12,
        )
        self.assertEqual(one_sided_binomial_upper(6400, 6400, alpha), 1)

    def test_unknowns_widen_only_upper_endpoint_at_planned_checkpoint(self):
        result = conservative_interval(
            50, 10, 100, checkpoints=(100,), comparison_points=4
        )
        alpha = 0.05 / (2 * 4 * 1)
        self.assertAlmostEqual(result["per_endpoint_alpha"], alpha)
        self.assertAlmostEqual(
            result["lower"], one_sided_binomial_lower(50, 100, alpha)
        )
        self.assertAlmostEqual(
            result["upper"], one_sided_binomial_upper(60, 100, alpha)
        )
        with self.assertRaisesRegex(ValueError, "undeclared checkpoint"):
            conservative_interval(50, 0, 101, checkpoints=(100,))

    def test_family_error_allocation_is_fixed_for_all_declared_points(self):
        result = conservative_interval(
            50, 0, 800, checkpoints=DEFAULT_CHECKPOINTS, comparison_points=4
        )
        self.assertAlmostEqual(result["per_endpoint_alpha"], 1 / 1120)


class _AlwaysFeasibleProvider:
    identity = {"kind": "test-provider"}

    def __init__(self):
        self.calls = []
        self.last_batch_stop_reason = None

    def evaluate_batch(self, tasks):
        for task in tasks:
            self.calls.append(task["ordering_id"])
            yield task, {
                "outcome": "feasible",
                "source": "test",
                "budget_label": "test",
            }


class SamplingRunTests(unittest.TestCase):
    def test_repeated_draws_count_multiplicity_and_cache_one_order(self):
        case = SamplingCase("one", 1, "entry", {"A": 1})
        provider = _AlwaysFeasibleProvider()
        with tempfile.TemporaryDirectory() as tmp:
            result = AdaptiveSampler(
                [case],
                provider,
                tmp,
                seed=11,
                checkpoints=(100,),
                comparison_points=1,
            ).run()
        summary = result["cases"][0]["checkpoints"][0]
        self.assertEqual(result["status"], "precision_reached")
        self.assertEqual(summary["draw_count"], 100)
        self.assertEqual(summary["unique_checked"], 1)
        self.assertEqual(summary["draw_outcomes"]["feasible"], 100)
        self.assertEqual(len(provider.calls), 1)

    def test_per_building_block_sampler_keeps_the_51_point_family_allocation(self):
        case = SamplingCase("one", 1, "entry", {"A": 1})
        with tempfile.TemporaryDirectory() as tmp:
            report = AdaptiveSampler(
                [case],
                _AlwaysFeasibleProvider(),
                tmp,
                seed=11,
                checkpoints=DEFAULT_CHECKPOINTS,
                target_width=1,
                comparison_points=51,
            ).run()
        summary = report["cases"][0]["checkpoints"][0]
        self.assertAlmostEqual(
            summary["interval"]["per_endpoint_alpha"],
            0.05 / (2 * 51 * len(DEFAULT_CHECKPOINTS)),
        )

    def test_incomplete_batch_keeps_last_checkpoint_and_resumes_same_stream(self):
        case = SamplingCase("six", 6, "entry", {"A": 3, "B": 3})
        self.assertGreater(len(set(draw_orderings(case, 78, 100))), 1)

        class InterruptingProvider(_AlwaysFeasibleProvider):
            def __init__(self, interrupt_on_batch=None):
                super().__init__()
                self.interrupt_on_batch = interrupt_on_batch
                self.batch_count = 0

            def evaluate_batch(self, tasks):
                self.batch_count += 1
                for index, task in enumerate(tasks):
                    if self.batch_count == self.interrupt_on_batch and index == 0:
                        self.last_batch_stop_reason = "test budget"
                        return
                    self.calls.append(task["ordering_id"])
                    yield task, {
                        "outcome": "feasible",
                        "source": "test",
                        "budget_label": "test",
                    }

        with tempfile.TemporaryDirectory() as tmp:
            first = AdaptiveSampler(
                [case],
                InterruptingProvider(interrupt_on_batch=2),
                tmp,
                seed=78,
                checkpoints=(1, 100),
                comparison_points=1,
            ).run()
            self.assertEqual(first["status"], "precision_not_reached")
            self.assertEqual(first["cases"][0]["last_complete_checkpoint"], 1)
            self.assertEqual(
                first["partial_checkpoint"]["last_complete_checkpoint_by_case"][
                    "six"
                ],
                1,
            )
            resumed_provider = InterruptingProvider()
            resumed = AdaptiveSampler(
                [case],
                resumed_provider,
                tmp,
                seed=78,
                checkpoints=(1, 100),
                comparison_points=1,
            ).run()
        self.assertEqual(resumed["status"], "precision_reached")
        self.assertEqual(resumed["cases"][0]["checkpoints"][-1]["draw_count"], 100)

    def test_checkpoint_waits_for_every_active_point_batch(self):
        cases = [
            SamplingCase("one", 1, "entry", {"A": 1}),
            SamplingCase("two", 2, "entry", {"A": 1, "B": 1}),
        ]

        class PartialProvider(_AlwaysFeasibleProvider):
            def evaluate_batch(self, tasks):
                task = next(iter(tasks))
                self.calls.append(task["ordering_id"])
                self.last_batch_stop_reason = "test budget"
                yield task, {
                    "outcome": "feasible",
                    "source": "test",
                    "budget_label": "test",
                }

        with tempfile.TemporaryDirectory() as tmp:
            report = AdaptiveSampler(
                cases,
                PartialProvider(),
                tmp,
                seed=13,
                checkpoints=(100,),
                comparison_points=4,
            ).run()
        self.assertEqual(report["status"], "precision_not_reached")
        self.assertEqual(
            [row["last_complete_checkpoint"] for row in report["cases"]],
            [0, 0],
        )
        self.assertEqual([row["checkpoints"] for row in report["cases"]], [[], []])
        self.assertTrue(report["partial_checkpoint"]["pending_ordering_ids_by_case"])

    def test_fixed_timing_pilot_does_not_claim_a_confidence_interval(self):
        case = SamplingCase("one", 1, "entry", {"A": 1})
        provider = _AlwaysFeasibleProvider()
        with tempfile.TemporaryDirectory() as tmp:
            report = run_fixed_pilot(
                [case], provider, tmp, seed=12, draws_per_case=4
            )
        self.assertEqual(report["status"], "complete")
        self.assertTrue(report["precision_not_reached"])
        self.assertNotIn("interval", report["cases"][0])
        self.assertEqual(report["cases"][0]["completed_draws"], 4)
        self.assertEqual(report["cases"][0]["unique_checked"], 1)
        self.assertEqual(len(provider.calls), 1)

    def test_cyclic_draw_stream_is_reproducible_and_rotation_canonical(self):
        case = SamplingCase("four", 4, "entry", {"A": 2, "B": 2})
        first = draw_orderings(case, 78, 100)
        second = draw_orderings(case, 78, 100)
        self.assertEqual(first, second)
        self.assertEqual(set(first), {("A", "A", "B", "B"), ("A", "B", "A", "B")})
        for order in first:
            self.assertEqual(order, min(order[i:] + order[:i] for i in range(len(order))))
            self.assertEqual(
                ordering_id(case, order),
                ordering_id(case, order[1:] + order[:1]),
            )


class SavedResultProviderTests(unittest.TestCase):
    def test_overlay_classification_replaces_unknown_without_pooling_sources(self):
        case = SamplingCase("one", 1, "entry", {"A": 1})
        ordering = ("A",)
        key = ordering_id(case, ordering)
        model_identity = "a" * 64
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = root / "base"
            overlay = root / "overlay"
            for directory in (base, overlay):
                (directory / "1").mkdir(parents=True)
            common = {
                "identity_sha256": model_identity,
                "ordering_id": key,
                "ordering": {"entry": ["A"]},
                "volume": 1,
            }
            (base / "1" / f"{key}.json").write_text(
                json.dumps(common | {"status": "unresolved"})
            )
            (overlay / "1" / f"{key}.json").write_text(
                json.dumps(
                    common
                    | {
                        "status": "conditional_no",
                        "solver_status": "INFEASIBLE",
                        "solver_model_gap": 0,
                        "trials": [],
                    }
                )
            )
            provider = SavedSolverResultProvider(
                [
                    SolverResultSource("base", base, "short", {"sha": "base"}),
                    SolverResultSource(
                        "overlay", overlay, "long", {"sha": "overlay"}
                    ),
                ],
                model_identity_sha256=model_identity,
            )
            task = {
                "case": case.record(),
                "ordering_id": key,
                "ordering": {"entry": ["A"]},
            }
            result = provider.evaluate(task)
            report = AdaptiveSampler(
                [case],
                provider,
                root / "run",
                seed=13,
                checkpoints=(100,),
                comparison_points=1,
            ).run()
        self.assertEqual(result["outcome"], "conditional_no")
        self.assertEqual(result["source"], "overlay")
        self.assertEqual(result["budget_label"], "long")
        self.assertEqual(result["matched_sources"], ["base", "overlay"])
        self.assertEqual(
            [row["outcome"] for row in result["source_history"]],
            ["unknown", "conditional_no"],
        )
        summary = report["cases"][0]["checkpoints"][0]
        self.assertEqual(summary["budget_draws"]["long"]["conditional_no"], 100)
        self.assertEqual(
            summary["source_observations"]["base"]["draws"]["unknown"], 100
        )
        self.assertEqual(
            summary["source_observations"]["overlay"]["draws"][
                "conditional_no"
            ],
            100,
        )

    def test_conflicting_sound_source_labels_are_rejected(self):
        case = SamplingCase("one", 1, "entry", {"A": 1})
        ordering = ("A",)
        key = ordering_id(case, ordering)
        model_identity = "b" * 64
        with tempfile.TemporaryDirectory() as tmp:
            sources = []
            for name, status, detail in (
                ("base", "solver_feasible", {"solver_status": "OPTIMAL", "slot_pattern": [["A", 0, "A"]]}),
                ("overlay", "conditional_no", {"solver_status": "INFEASIBLE", "solver_model_gap": 0}),
            ):
                directory = Path(tmp) / name
                (directory / "1").mkdir(parents=True)
                (directory / "1" / f"{key}.json").write_text(
                    json.dumps(
                        {
                            "identity_sha256": model_identity,
                            "ordering_id": key,
                            "ordering": {"entry": ["A"]},
                            "volume": 1,
                            "status": status,
                            **detail,
                        }
                    )
                )
                sources.append(
                    SolverResultSource(name, directory, name, {"source": name})
                )
            provider = SavedSolverResultProvider(
                sources, model_identity_sha256=model_identity
            )
            with self.assertRaisesRegex(ValueError, "disagree"):
                provider.evaluate(
                    {
                        "case": case.record(),
                        "ordering_id": key,
                        "ordering": {"entry": ["A"]},
                    }
                )


class LocalSolverPolicyTests(unittest.TestCase):
    def test_stop_file_prevents_new_solver_task_admission(self):
        task = {
            "ordering_id": "0123456789abcdef01234567",
            "case": {"key": "one", "volume": 1},
            "ordering": {"entry": ["A"]},
        }
        with tempfile.TemporaryDirectory() as tmp:
            stop_file = Path(tmp) / "STOP"
            stop_file.touch()
            admitted = []
            provider = LocalFixedProfileSolverProvider(
                {"windows": {}, "variants": {}},
                period=1800,
                workers=1,
                stop_file=stop_file,
                on_admit=admitted.append,
                model_identity_sha256="model",
                calibration_sha256="calibration",
            )
            self.assertEqual(list(provider.evaluate_batch([task])), [])
        self.assertEqual(provider.last_batch_stop_reason, "stop_requested")
        self.assertEqual(provider.active_worker_count, 0)
        self.assertEqual(provider.active_worker_process_count, 0)
        self.assertEqual(admitted, [])

    def test_stop_drains_admitted_tasks_without_submitting_pending_ones(self):
        tasks = [
            {
                "case": {"key": "one", "volume": 1},
                "ordering_id": f"{index:024x}",
                "ordering": {"entry": ["A"]},
            }
            for index in range(3)
        ]
        submitted = []

        class FakeFuture:
            def __init__(self, task):
                self.task = task

            def result(self):
                return {"outcome": "feasible"}

        class FakeExecutor:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def submit(self, _function, task, *_args):
                submitted.append(task)
                return FakeFuture(task)

        def finish_one(active, return_when):
            future = next(iter(active))
            return {future}, set(active) - {future}

        with tempfile.TemporaryDirectory() as tmp:
            stop_file = Path(tmp) / "STOP"
            provider = LocalFixedProfileSolverProvider(
                {"windows": {}, "variants": {}},
                period=1800,
                workers=2,
                stop_file=stop_file,
                model_identity_sha256="model",
                calibration_sha256="calibration",
            )
            yielded = []
            with (
                patch(
                    "railsim_mcp.adaptive_sampling.ProcessPoolExecutor",
                    FakeExecutor,
                ),
                patch(
                    "railsim_mcp.adaptive_sampling.wait",
                    side_effect=finish_one,
                ),
            ):
                for task, _result in provider.evaluate_batch(tasks):
                    yielded.append(task)
                    if len(yielded) == 1:
                        stop_file.touch()

        self.assertEqual(len(submitted), 2)
        self.assertEqual(len(yielded), 2)
        self.assertEqual(provider.last_batch_stop_reason, "stop_requested")
        self.assertEqual(provider.active_worker_count, 0)
        self.assertEqual(provider.active_worker_process_count, 0)

    def test_only_zero_gap_infeasibility_is_a_conditional_no(self):
        task = {
            "ordering_id": "0123456789abcdef01234567",
            "ordering": {"entry": ["A"]},
        }
        with patch(
            "railsim_mcp.adaptive_sampling.solve",
            side_effect=[
                {"status": "INFEASIBLE"},
                {"status": "INFEASIBLE"},
            ],
        ) as solver:
            result = _solve_fixed_order(
                task,
                {},
                {"A_P1": {"product": "A"}},
                1800,
                4711,
                0.546,
                61.96,
            )
        self.assertEqual(result["outcome"], "conditional_no")
        self.assertEqual(
            [call.kwargs["min_gap"] for call in solver.call_args_list], [1, 0]
        )

    def test_gap_zero_is_not_attempted_after_gap_one_feasible(self):
        task = {
            "ordering_id": "0123456789abcdef01234567",
            "ordering": {"entry": ["A"]},
        }
        pattern = [["A", 0, "A_P1"]]
        with patch(
            "railsim_mcp.adaptive_sampling.solve",
            return_value={
                "status": "FEASIBLE",
                "slot_pattern": pattern,
                "solve_seconds": 0.01,
            },
        ) as solver:
            result = _solve_fixed_order(
                task,
                {},
                {"A_P1": {"product": "A"}},
                1800,
                4711,
                0.546,
                61.96,
            )
        self.assertEqual(result["outcome"], "feasible")
        self.assertEqual(solver.call_count, 1)
        self.assertEqual(solver.call_args.kwargs["min_gap"], 1)


if __name__ == "__main__":
    unittest.main()
