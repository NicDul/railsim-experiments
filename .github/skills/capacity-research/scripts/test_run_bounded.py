import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest


SCRIPT = Path(__file__).with_name("run_bounded.py")
SPEC = importlib.util.spec_from_file_location("run_bounded", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class BoundedActionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.ledger = Path(self.temp.name) / "execution.json"

    def test_completed_action_records_output_and_usage(self):
        result = MODULE.run(
            [sys.executable, "-c", "print('verified')"], 5, self.ledger
        )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["returncode"], 0)
        self.assertIn("verified", Path(result["stdout"]).read_text())
        self.assertGreater(result["elapsed_wall_seconds"], 0)
        self.assertGreaterEqual(result["completed_child_cpu_seconds"], 0)
        self.assertEqual(json.loads(self.ledger.read_text()), result)

    def test_failure_retains_nonzero_exit(self):
        result = MODULE.run([sys.executable, "-c", "raise SystemExit(7)"], 5, self.ledger)
        self.assertEqual((result["status"], result["returncode"]), ("failed", 7))

    def test_missing_executable_is_explicit(self):
        result = MODULE.run([str(Path(self.temp.name) / "missing")], 5, self.ledger)
        self.assertEqual(result["status"], "launch_failed")
        self.assertIn("FileNotFoundError", result["error"])

    def test_existing_ledger_is_not_overwritten(self):
        self.ledger.write_text("retained")
        with self.assertRaises(FileExistsError):
            MODULE.run([sys.executable, "-c", "print('unexpected')"], 5, self.ledger)
        self.assertEqual(self.ledger.read_text(), "retained")

    def test_timeout_stops_descendant_in_new_session(self):
        marker = Path(self.temp.name) / "escaped.txt"
        child = (
            "import time; from pathlib import Path; time.sleep(2); "
            f"Path({str(marker)!r}).write_text('escaped')"
        )
        parent = (
            "import subprocess,sys,time; "
            f"subprocess.Popen([sys.executable,'-c',{child!r}],start_new_session=True); "
            "print('child-started',flush=True); time.sleep(60)"
        )
        result = MODULE.run([sys.executable, "-c", parent], 0.5, self.ledger)
        self.assertEqual((result["status"], result["returncode"]), ("timeout", 124))
        self.assertIn("child-started", Path(result["stdout"]).read_text())
        time.sleep(2)
        self.assertFalse(marker.exists())

    def test_cli_rejects_unbounded_time(self):
        for seconds in ("nan", "inf", "0", "-1"):
            result = subprocess.run(
                [sys.executable, str(SCRIPT), "--seconds", seconds, "--ledger",
                 str(self.ledger), "--", sys.executable, "-c", "print('unexpected')"],
                capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 2)
        self.assertFalse(self.ledger.exists())


if __name__ == "__main__":
    unittest.main()
