import copy, json, tempfile, unittest
from pathlib import Path
from railsim_mcp.validation import evaluate
from railsim_mcp.runtime import bounded_process


class SearchTests(unittest.TestCase):
    def fixture(self):
        expected = {"FV_0": (3600, "FV"), "RV_0": (7199, "RV")}
        refs = {"FV": 100, "RV": 200}
        rows = [
            dict(
                vehicle_id=k,
                status="terminal_arrived",
                scheduled_departure=t,
                planned_arrival=t + refs[p],
                actual_arrival=t + refs[p],
                terminal_delay_seconds=0,
            )
            for k, (t, p) in expected.items()
        ]
        return (
            dict(status="completed", trains=rows, headway_conflicts=[{"deficit": 30}]),
            expected,
            refs,
        )

    def test_headways_and_noncohort_delays_are_diagnostic(self):
        run, expected, refs = self.fixture()
        run["trains"].append(
            dict(vehicle_id="outside", scheduled_departure=7200, status="unfinished")
        )
        self.assertTrue(evaluate(run, expected, refs)["passed"])

    def test_raw_positive_delay_fails(self):
        run, expected, refs = self.fixture()
        run["trains"][0]["actual_arrival"] += 0.00001
        run["trains"][0]["terminal_delay_seconds"] = 0.00001
        self.assertFalse(evaluate(run, expected, refs)["passed"])

    def test_missing_and_invalid_cannot_pass(self):
        for mode in (
            "missing",
            "unfinished",
            "nan",
            "reference",
            "duplicate",
            "notcompleted",
        ):
            run, expected, refs = self.fixture()
            if mode == "missing":
                run["trains"].pop()
            if mode == "unfinished":
                run["trains"][0]["status"] = "unfinished"
            if mode == "nan":
                run["trains"][0]["actual_arrival"] = float("nan")
            if mode == "reference":
                run["trains"][0]["planned_arrival"] += 1
            if mode == "duplicate":
                run["trains"].append(copy.deepcopy(run["trains"][0]))
            if mode == "notcompleted":
                run["status"] = "timeout"
            self.assertFalse(evaluate(run, expected, refs)["passed"], mode)
        self.assertFalse(
            evaluate(dict(status="completed", trains=[]), {}, refs)["passed"]
        )

    def test_recovered_delay_is_accepted(self):
        run, expected, refs = self.fixture()
        run["intermediate_delay"] = 10
        self.assertTrue(evaluate(run, expected, refs)["passed"])

    def test_subprocess_timeout(self):
        import subprocess, sys

        with self.assertRaises(subprocess.TimeoutExpired):
            bounded_process([sys.executable, "-c", "import time;time.sleep(3)"], 0.05)
