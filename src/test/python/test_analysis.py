"""Focused read-only tests for retained run analysis."""

import gzip
import hashlib
import json
import tempfile
import unittest
import uuid
from pathlib import Path

from railsim_mcp.analysis import RunAnalysis
from railsim_mcp.workspace import WorkspaceError


class RunAnalysisTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.run_id = str(uuid.uuid4())
        run_root = self.root / self.run_id
        inputs = run_root / "inputs"
        events = run_root / "output/ITERS/it.0"
        inputs.mkdir(parents=True)
        events.mkdir(parents=True)
        (inputs / "schedule.xml").write_text(
            '<transitSchedule><transitLine id="L"><transitRoute id="R"><route><link refId="a"/></route>'
            '<routeProfile><stop refId="A" arrivalOffset="00:00:00" departureOffset="00:00:10"/>'
            '<stop refId="B" arrivalOffset="00:01:40" departureOffset="00:01:40"/>'
            '<stop refId="C" arrivalOffset="00:02:00" departureOffset="00:02:00"/></routeProfile>'
            '</transitRoute></transitLine></transitSchedule>', encoding="utf-8")
        hashes = {"schedule.xml": hashlib.sha256((inputs / "schedule.xml").read_bytes()).hexdigest()}
        aggregate = hashlib.sha256("\n".join(f"{k}:{hashes[k]}" for k in sorted(hashes)).encode()).hexdigest()
        train = {"vehicle_id": "train-1", "departure_id": "dep-1", "route_id": "R",
                 "terminal_stop_id": "C", "scheduled_departure": 0.0, "status": "terminal_arrived",
                 "arrived": True, "planned_arrival": 120.0, "actual_arrival": 120.0,
                 "terminal_delay_seconds": 0.0}
        run = {"run_id": self.run_id, "status": "completed", "input_file_sha256": hashes,
               "input_sha256": aggregate, "trains_expected": 1, "trains_departed": 1, "trains_arrived": 1,
               "trains_unfinished": 0, "horizon_seconds": 600, "headway_conflict_count": 0,
               "headway_conflicts": [], "trains": [train]}
        (run_root / "run.json").write_text(json.dumps(run), encoding="utf-8")
        rows = [
            {"time": "0", "type": "VehicleArrivesAtFacility", "vehicle": "train-1", "facility": "A", "delay": "0"},
            {"time": "10", "type": "VehicleDepartsAtFacility", "vehicle": "train-1", "facility": "A", "delay": "0"},
            {"time": "110", "type": "VehicleArrivesAtFacility", "vehicle": "train-1", "facility": "B", "delay": "0"},
            {"time": "110", "type": "VehicleDepartsAtFacility", "vehicle": "train-1", "facility": "B", "delay": "0"},
        ]
        body = '<events>' + "".join('<event ' + " ".join(f'{k}="{v}"' for k, v in row.items()) + '/>' for row in rows) + '</events>'
        with gzip.open(events / f"{self.run_id}.0.events.xml.gz", "wt", encoding="utf-8") as stream:
            stream.write(body)
        self.analysis = RunAnalysis(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def test_timeline_has_clock_offsets_and_null_missing_events(self):
        report = self.analysis.timeline(self.run_id, "train-1", 1)
        train = report["trains"][0]
        self.assertEqual(10.0, train["actual_departure"])
        self.assertEqual(100.0, train["stops"][1]["scheduled_arrival"])
        self.assertIsNone(train["stops"][2]["actual_arrival"])
        self.assertIsNone(train["stops"][2]["dwell_seconds"])

    def test_read_fails_closed_when_frozen_input_changes(self):
        path = self.root / self.run_id / "inputs/schedule.xml"
        path.write_text(path.read_text() + " ", encoding="utf-8")
        with self.assertRaisesRegex(WorkspaceError, "hash mismatch"):
            self.analysis.timeline(self.run_id, "train-1", 1)

    def test_bounds_and_unknown_trains_are_rejected(self):
        with self.assertRaisesRegex(WorkspaceError, "limit"):
            self.analysis.timeline(self.run_id, limit=201)
        with self.assertRaisesRegex(WorkspaceError, "train not found"):
            self.analysis.timeline(self.run_id, "missing", 1)

    def test_delays_rank_unknown_then_severity_and_page_without_losing_trains(self):
        path = self.root / self.run_id / "run.json"
        run = json.loads(path.read_text())
        base = run["trains"][0]
        run["trains"] += [dict(base, vehicle_id="late", terminal_delay_seconds=12),
                          dict(base, vehicle_id="missing", arrived=False, terminal_delay_seconds=None),
                          dict(base, vehicle_id="later", terminal_delay_seconds=30)]
        path.write_text(json.dumps(run))
        first = self.analysis.delayed_trains(self.run_id, limit=2)
        self.assertEqual(["missing", "later"], [t["vehicle_id"] for t in first["trains"]])
        self.assertEqual(3, first["total"])
        second = self.analysis.delayed_trains(self.run_id, offset=first["next_offset"], limit=2)
        self.assertEqual("late", second["trains"][0]["vehicle_id"])
        self.assertIsNone(second["next_offset"])
        self.assertEqual(1, self.analysis.delayed_trains(self.run_id, 12, False)["total"])
        with self.assertRaises(WorkspaceError):
            self.analysis.delayed_trains(self.run_id, float("nan"))

    def test_movement_pagination_filters_and_corrupt_xml(self):
        first = self.analysis.movements(self.run_id, "dep-1", limit=2)
        second = self.analysis.movements(self.run_id, "train-1", offset=first["next_offset"], limit=2)
        self.assertEqual([0, 1, 2, 3], [e["_index"] for e in first["events"] + second["events"]])
        self.assertEqual(2, self.analysis.movements(self.run_id, "train-1", start_seconds=110)["total"])
        self.assertEqual(0, self.analysis.movements(self.run_id, "train-1", link_id="missing")["total"])
        with self.assertRaises(WorkspaceError):
            self.analysis.movements(self.run_id, "train-1", start_seconds=20, end_seconds=10)
        with self.assertRaises(WorkspaceError):
            self.analysis.movements(self.run_id, "train-1", offset=-1)
        event_path = self.root / self.run_id / "output/ITERS/it.0" / f"{self.run_id}.0.events.xml.gz"
        event_path.write_bytes(b"broken")
        with self.assertRaisesRegex(WorkspaceError, "corrupt"):
            self.analysis.movements(self.run_id, "train-1")

    def test_period_gap_includes_empty_periods(self):
        path = self.root / self.run_id / 'run.json'
        run = json.loads(path.read_text())
        run['trains'].append(dict(run['trains'][0], vehicle_id='train-2',
                                  departure_id='dep-2', scheduled_departure=3600.0))
        run['trains_expected'] = 2
        path.write_text(json.dumps(run))
        gaps = self.analysis.feasibility(self.run_id, 1800)['warm_up_drain_evidence']['wraparound_schedule_gaps']
        self.assertEqual(3600.0, gaps[0]['schedule_gap_seconds'])

    def test_first_delay_rejects_unknown_baseline_inputs(self):
        with self.assertRaisesRegex(WorkspaceError, 'identical network.xml'):
            self.analysis.first_delay(self.run_id, self.run_id, 'train-1')

    def test_decompressed_event_size_is_bounded(self):
        self.analysis.MAX_EVENT_STREAM_BYTES = 10
        with self.assertRaisesRegex(WorkspaceError, 'decompressed read bound'):
            self.analysis.timeline(self.run_id)


if __name__ == "__main__":
    unittest.main()
