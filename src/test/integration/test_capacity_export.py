"""Export reader works with the Python standard library, no solver or MCP server."""

import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location(
    "audit_capacity_export", ROOT / "scripts/audit_capacity_export.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class PortableExportTests(unittest.TestCase):
    def fixture(self, root):
        report = dict(
            identity={"config": {"period_seconds": 1800, "use_case": "UC1"}},
            calibration={"variants": {}},
            rates=[],
            bounds={"maximum": None, "simulator_lower_bound_trains_per_hour": None},
        )
        for name, data in [("report.json", report), ("profiles.json", {})]:
            (root / name).write_text(json.dumps(data))
        manifest = {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in root.iterdir()
        }
        (root / "manifest.json").write_text(json.dumps(manifest))

    def test_portable_directory_and_tamper_detection(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.fixture(root)
            self.assertEqual(module.audit(root)["status"], "PASS")
            (root / "profiles.json").write_text('{"changed":true}')
            with self.assertRaises(AssertionError):
                module.audit(root)

    def test_manifest_cannot_refer_outside_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.fixture(root)
            (root / "manifest.json").write_text(
                json.dumps({"../external-file": "ignored"})
            )
            with self.assertRaises(AssertionError):
                module.audit(root)
