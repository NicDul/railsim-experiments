import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from railsim_mcp.batch import PersistentScenarioWorkspace
from railsim_mcp.workspace import WorkspaceError


FAKE_SERVER = """
import json,sys
from pathlib import Path
for line in sys.stdin:
    job=json.loads(line)
    args=job['args']
    output=Path(args[args.index('--output')+1])
    output.mkdir(exist_ok=True)
    (output/'simulation.json').write_text(json.dumps({'run_id':job['run_id'],'trains':[]}))
    print(json.dumps({'run_id':job['run_id'],'exit_code':0}),flush=True)
"""


class PersistentWorkspaceTests(unittest.TestCase):
    def test_reuses_process_and_preserves_per_run_snapshot_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ws = PersistentScenarioWorkspace(root, root / "workspaces",
                                             project_root=root, runs_root=root / "runs",
                                             classpath="unused")
            popen = subprocess.Popen
            processes = []

            def launch(command, **kwargs):
                process = popen([sys.executable, "-u", "-c", FAKE_SERVER], **kwargs)
                processes.append(process)
                return process

            try:
                with patch("railsim_mcp.batch.subprocess.Popen", side_effect=launch):
                    results = []
                    for run_id in ("first", "second"):
                        run_root = root / "runs" / run_id
                        run_root.mkdir(parents=True)
                        results.append(ws._execute_run(
                            run_id, run_root, run_root / "inputs", run_root / "output",
                            Path("uc1_operational_plan.json"), {"network.xml": "hash"},
                            "aggregate", 4711, 12600,
                        ))
                self.assertEqual(len(processes), 1)
                self.assertEqual(results[0]["jvm_pid"], results[1]["jvm_pid"])
                self.assertNotEqual(results[0]["snapshot_directory"], results[1]["snapshot_directory"])
                self.assertEqual(results[0]["input_file_sha256"], {"network.xml": "hash"})
                self.assertEqual(json.loads((root / "runs/second/run.json").read_text())["run_id"], "second")
            finally:
                ws.close()
            self.assertIsNotNone(processes[0].poll())

    def test_response_identity_mismatch_is_explicit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ws = PersistentScenarioWorkspace(root, root / "workspaces",
                                             project_root=root, runs_root=root / "runs",
                                             classpath="unused")
            with patch.object(ws, "_start"), patch.object(ws, "_response", return_value={"run_id": "wrong", "exit_code": 0}):
                from unittest.mock import Mock
                ws.process = Mock()
                ws.log = Mock(name="log")
                with self.assertRaisesRegex(WorkspaceError, "persistent simulation failed"):
                    ws._execute_run("expected", root, root, root, Path("plan.json"), {}, "hash", 4711, 12600)
                ws.process = None
                ws.log = None
