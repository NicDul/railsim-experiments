import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from railsim_mcp.capacity import save
from railsim_mcp.exhaustive import digest
from railsim_mcp.flexibility import _ordering_id
from railsim_mcp.spot_check import (
    audit_witness, prepare, replay, retry, select, validate_limits, verify,
)


class SpotCheckTests(unittest.TestCase):
    def setUp(self):
        self.order = {"entry": ["A", "B"]}
        self.original = dict(ordering=self.order, ordering_id=_ordering_id(self.order),
                             solver_status="UNKNOWN", solver_model_gap=1, volume=2,
                             status="unresolved")
        self.ref = dict(
            variants={"A_P1": {"product": "A"}, "B_P1": {"product": "B"}},
            windows={"A_P1": {"entry": [0, 0, 10]},
                     "B_P1": {"entry": [0, 0, 10]}},
        )
        self.pattern = [["A", 0, "A_P1"], ["B", 11, "B_P1"]]

    def test_sampling_is_deterministic_without_replacement_and_not_traversal_dependent(self):
        population = list(range(100))
        chosen = select(population, 5, 4711)
        self.assertEqual(chosen, select(reversed(population), 5, 4711))
        self.assertEqual(len(set(chosen)), 5)
        self.assertNotEqual(chosen, select(population, 5, 4712))
        with self.assertRaises(ValueError):
            select(population, 101, 4711)

    def test_unknown_stays_unknown_and_cannot_trigger_zero_gap(self):
        with tempfile.TemporaryDirectory() as tmp:
            solver = Mock(return_value={"status": "UNKNOWN"})
            result = retry(self.original, self.ref, 600, Path(tmp) / "case.json", solver=solver)
            self.assertEqual(result["status"], "still_unknown")
            self.assertEqual(solver.call_count, 1)
            self.assertEqual(solver.call_args.kwargs["min_gap"], 1)
            self.assertLessEqual(solver.call_args.args[3], 600)
            self.assertEqual(solver.call_args.args[4:6], (frozenset(), 1))

    def test_gap_fallback_shares_budget_and_preserves_original_seed_logic(self):
        with tempfile.TemporaryDirectory() as tmp:
            solver = Mock(side_effect=[{"status": "INFEASIBLE"}, {"status": "INFEASIBLE"}])
            with patch("railsim_mcp.spot_check.time.monotonic", side_effect=[100, 120, 120, 130]):
                result = retry(self.original, self.ref, 600, Path(tmp) / "case.json", solver=solver)
            self.assertEqual(result["status"], "conditional_no")
            calls = solver.call_args_list
            self.assertEqual([c.args[3] for c in calls], [600, 580])
            self.assertEqual([c.kwargs["min_gap"] for c in calls], [1, 0])
            seed = (4711 + int(self.original["ordering_id"][:8], 16)) % 2147483647
            self.assertEqual([c.kwargs["seed"] for c in calls], [seed, (seed + 1) % 2147483647])

    def test_feasible_witness_is_checked_and_resume_never_recomputes_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "case.json"
            solver = Mock(return_value={"status": "OPTIMAL", "slot_pattern": self.pattern})
            result = retry(self.original, self.ref, 600, path, solver=solver)
            self.assertTrue(result["witness_audit"]["passed"])
            self.assertEqual(retry(self.original, self.ref, 600, path, solver=solver), result)
            self.assertEqual(solver.call_count, 1)

    def test_interruption_reservation_does_not_grant_another_budget_on_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "case.json"
            with self.assertRaises(KeyboardInterrupt):
                retry(self.original, self.ref, 600, path,
                      solver=Mock(side_effect=KeyboardInterrupt))
            solver = Mock()
            result = retry(self.original, self.ref, 600, path, solver=solver)
            self.assertEqual(result["status"], "interrupted")
            self.assertEqual(result["reserved_seconds"], 600)
            solver.assert_not_called()

    def test_stop_schedules_no_solver_and_resume_can_continue_pending(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "case.json"
            solver = Mock()
            result = retry(self.original, self.ref, 600, path, solver=solver, stopped=lambda: True)
            self.assertEqual(result["status"], "pending")
            solver.assert_not_called()

    def test_solver_failure_is_persisted_and_never_reported_as_unknown_or_negative(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "case.json"
            with self.assertRaisesRegex(RuntimeError, "engine failed"):
                retry(self.original, self.ref, 600, path, solver=Mock(side_effect=RuntimeError("engine failed")))
            self.assertEqual(json.loads(path.read_text())["status"], "execution_error")

    def test_arithmetic_audit_rejects_resource_conflict_and_invalid_order(self):
        plan = dict(self.original, solver_model_gap=1, slot_pattern=self.pattern)
        self.assertTrue(audit_witness(plan, self.ref)["passed"])
        for pattern in (
            [["A", 0, "A_P1"], ["B", 10, "B_P1"]],
            [["A", 0, "A_P1"], ["B", 1790, "B_P1"]],
            [["A", 0, "A_P1"], ["B", 0, "B_P1"]],
        ):
            with self.assertRaises(ValueError):
                audit_witness(dict(plan, slot_pattern=pattern), self.ref)

    def test_prepare_preserves_sources_and_rejects_changed_settings_or_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, output = Path(tmp) / "source", Path(tmp) / "experiment"
            (source / "calibration/workspace").mkdir(parents=True)
            (source / "frozen").mkdir()
            (source / "solver-results/2").mkdir(parents=True)
            for name in ("identity.json", "frozen/manifest.json", "calibration/reference.json",
                         "calibration/origin.json"):
                save(source / name, {})
            (source / "calibration/workspace/network.xml").write_text("<network/>")
            unknown = dict(self.original, identity_sha256=digest(source / "identity.json"))
            feasible = dict(unknown, ordering={"entry": ["A", "A"]},
                            status="solver_feasible", solver_status="OPTIMAL")
            feasible["ordering_id"] = _ordering_id(feasible["ordering"])
            for row in (unknown, feasible):
                save(source / "solver-results/2" / (row["ordering_id"] + ".json"), row)
            before = {str(p): p.read_bytes() for p in source.rglob("*") if p.is_file()}
            kwargs = dict(origin="test", volumes=(2,), count=1)
            manifest = prepare(source, output, **kwargs)
            self.assertEqual(manifest, prepare(source, output, **kwargs))
            self.assertEqual(before, {str(p): p.read_bytes() for p in source.rglob("*") if p.is_file()})
            with self.assertRaisesRegex(ValueError, "settings"):
                prepare(source, output, **dict(kwargs, seed=10))
            (output / manifest["samples"][0]["original"]).write_text("{}")
            with self.assertRaisesRegex(ValueError, "immutable"):
                verify(output, manifest)
            with self.assertRaisesRegex(ValueError, "disjoint"):
                prepare(source, source / "bad", **kwargs)

    def test_limits_reject_unbounded_or_oversized_experiment(self):
        for seconds, calls in [(0, 10), (601, 10), (float("nan"), 10), (600, 21), (600, -1)]:
            with self.assertRaises(ValueError):
                validate_limits(seconds, calls)

    def test_replay_checks_all_repetitions_and_never_relaxes_one_tick_delay(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "run/inputs").mkdir(parents=True)
            (root / "run/output").mkdir()
            (root / "run/inputs/network.xml").write_text(
                '<network><link id="entry"><attribute name="railsimResourceId">'
                'entry</attribute></link></network>')
            for name in ("run/run.json", "run/output/simulation.json"):
                save(root / name, {})
            ws = SimpleNamespace(runs_root=root)
            ref = dict(self.ref, workspace="source", baselines={},
                       variants={v: dict(info, links=["entry"])
                                 for v, info in self.ref["variants"].items()})
            plan = dict(self.original, solver_model_gap=1, slot_pattern=self.pattern)
            rows, event_rows = [], []
            for k in range(6):
                for i, (p, departure, variant) in enumerate(self.pattern):
                    vehicle = f"{p}_{i:02}_p{k}"
                    rows.append(dict(vehicle_id=vehicle, status="terminal_arrived", route_id=variant))
                    event_rows.extend([
                        dict(type="entered link", vehicle=vehicle, link="entry"),
                        dict(type="railsimLinkStateChangeEvent", vehicle=vehicle, link="entry",
                             state="EXHAUSTED", time=departure + k * 1800),
                    ])
            score = dict(run_id="run", passed=True, max_delay=0, total_delay=0, missing=0, detours=0)
            raw = dict(trains=rows, events_file="events")
            checked = dict(route_errors=[], entry_order_errors=[], detour_event_count=0, rerouting_markers=[])
            with patch("railsim_mcp.spot_check.simulate", return_value=(score, raw)), \
                 patch("railsim_mcp.spot_check.audit_simulation", return_value=checked), \
                 patch("railsim_mcp.spot_check.events", side_effect=lambda _: iter(event_rows)):
                self.assertEqual(replay(plan, ref, ws)["status"], "passed")
                score["max_delay"] = 1
                self.assertEqual(replay(plan, ref, ws)["status"], "rejected")
                score["max_delay"] = 0
                rows[0]["status"] = "missing"
                self.assertEqual(replay(plan, ref, ws)["status"], "rejected")
                rows[0]["status"] = "terminal_arrived"
                rows[0]["route_id"] = "wrong"
                self.assertEqual(replay(plan, ref, ws)["status"], "rejected")
                rows[0]["route_id"] = "A_P1"
                event_rows.pop()
                self.assertEqual(replay(plan, ref, ws)["status"], "rejected")


if __name__ == "__main__":
    unittest.main()
