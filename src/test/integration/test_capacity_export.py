"""Export reader works with the Python standard library, no solver or MCP server."""

import hashlib
import gzip
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

    def test_multi_visit_arithmetic_and_uc3_integer_compositions(self):
        plan = json.loads(
            (ROOT / "src/main/resources/scenarios/use_case_3/uc3_operational_plan.json").read_text()
        )
        self.assertEqual(module.composition(plan, "METRO/HIGH_REVERSAL", 2),
                         {"RV_LML": 2, "RV_LMR": 1, "RV_RML": 1})
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.fixture(root)
            report = json.loads((root / "report.json").read_text())
            report["identity"]["config"]["use_case"] = "UC2"
            report["calibration"]["variants"] = {"R": {"product": "RV"}}
            report["rates"] = [
                dict(composition={"R": 2}, occupation=dict(
                    resource_minimum_seconds={"switch": 40}, excluded_resources={}
                ))
            ]
            (root / "report.json").write_text(json.dumps(report))
            (root / "profiles.json").write_text(json.dumps(
                {"R": {"switch": [[0, 0, 10], [80, 80, 90]]}}
            ))
            self.rehash(root)
            self.assertEqual(module.audit(root)["status"], "PASS")
            report["rates"][0]["occupation"]["resource_minimum_seconds"]["switch"] = 180
            (root / "report.json").write_text(json.dumps(report))
            self.rehash(root)
            with self.assertRaises(AssertionError):
                module.audit(root)

    def test_raw_turnback_events_preserve_one_vehicle_and_reject_detours(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "run.events.xml.gz"
            text = ('<events>'
                    '<event type="VehicleArrivesAtFacility" vehicle="RV_00" '
                    'facility="M_REVERSING" time="300"/>'
                    '<event type="VehicleDepartsAtFacility" vehicle="RV_00" '
                    'facility="M_REVERSING" time="780"/>'
                    '</events>')
            with gzip.open(path, "wt") as stream:
                stream.write(text)
            events = module.turnback_events(root)
            self.assertEqual(events["RV_00", "VehicleDepartsAtFacility"]
                             - events["RV_00", "VehicleArrivesAtFacility"], 480)
            with gzip.open(path, "wt") as stream:
                stream.write(text.replace('</events>', '<event type="railsimDetourEvent"/></events>'))
            with self.assertRaises(AssertionError):
                module.turnback_events(root)

    def rehash(self, root):
        (root / "manifest.json").write_text(json.dumps({
            str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob("*") if p.is_file() and p.name != "manifest.json"
        }))
