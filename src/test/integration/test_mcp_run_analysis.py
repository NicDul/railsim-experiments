"""Real stdio client checks against generated, synthetic retained-run fixtures."""

import asyncio
import gzip
import hashlib
import json
import os
import sys
import shutil
import unittest
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path

from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


PROJECT_ROOT = Path(__file__).resolve().parents[3]
from railsim_mcp.configuration import state_root
RUN_ROOT = state_root() / 'runs'


def payload(result):
    if result.isError:
        raise AssertionError(result.content)
    if result.structuredContent is not None:
        return result.structuredContent
    return json.loads(result.content[0].text)


class RetainedRunAnalysisStdioTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.delayed = self.make_run(72)
        self.zero_delay = self.make_run(0)
        self.isolated = self.make_run(0, isolated=True)

    def make_run(self, delay, isolated=False):
        run_id = str(uuid.uuid4())
        root = RUN_ROOT / run_id
        inputs = root / 'inputs'
        events_dir = root / 'output/ITERS/it.0'
        inputs.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, root)
        events_dir.mkdir(parents=True)
        schedule = ET.Element('transitSchedule')
        route = ET.SubElement(ET.SubElement(schedule, 'transitLine', id='FV', name=f'fixture-{delay}'), 'transitRoute', id='FV_LR')
        profile = ET.SubElement(route, 'routeProfile')
        ET.SubElement(profile, 'stop', refId='A', arrivalOffset='00:00:00', departureOffset='00:00:00')
        ET.SubElement(profile, 'stop', refId='B', arrivalOffset='00:01:40', departureOffset='00:01:40')
        route_links = ET.SubElement(route, 'route')
        ET.SubElement(route_links, 'link', refId='f02')
        departures = ET.SubElement(route, 'departures')
        trains, event_rows = [], []
        for period in range(1 if isolated else 3):
            vehicle = f'FV_00_p{period}'
            start = period * 1800
            ET.SubElement(departures, 'departure', id=vehicle, departureTime=str(start), vehicleRefId=vehicle)
            trains.append(dict(vehicle_id=vehicle, departure_id=vehicle, route_id='FV_LR',
                               terminal_stop_id='B', scheduled_departure=float(start),
                               planned_arrival=start + 100, actual_arrival=start + 100 + delay,
                               terminal_delay_seconds=float(delay), arrived=True, status='terminal_arrived'))
            for time, kind, extra in [
                (start, 'VehicleArrivesAtFacility', dict(facility='A', delay='0')),
                (start, 'VehicleDepartsAtFacility', dict(facility='A', delay='0')),
                (start + 10 + delay, 'entered link', dict(link='f02')),
                (start + 10 + delay, 'railsimLinkStateChangeEvent', dict(link='f02', state='IN_USE')),
                (start + 80 + delay, 'railsimTrainLeavesLinkEvent', dict(link='f02')),
                (start + 90 + delay, 'railsimLinkStateChangeEvent', dict(link='f02', state='EMPTY')),
                (start + 100 + delay, 'VehicleArrivesAtFacility', dict(facility='B', delay=str(delay))),
            ]:
                event_rows.append(dict(time=str(time), type=kind, vehicle=vehicle, **extra))
        (inputs / 'schedule.xml').write_bytes(ET.tostring(schedule))
        (inputs / 'network.xml').write_text('<network/>')
        (inputs / 'config.xml').write_text('<config/>')
        fleet = ET.Element('vehicleDefinitions')
        ET.SubElement(fleet, 'vehicleType', id='FV')
        for train in trains:
            ET.SubElement(fleet, 'vehicle', id=train['vehicle_id'], type='FV')
        (inputs / 'vehicles.xml').write_bytes(ET.tostring(fleet))
        hashes = {p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in inputs.iterdir()}
        conflict = dict(link_id='f02', previous_vehicle_id='FV_00_p0', following_vehicle_id='FV_00_p1',
                        headway_head_to_head_seconds=72, headway_tail_to_head_seconds=56,
                        required_headway_seconds=180, violation_head_to_head_seconds=108,
                        violation_tail_to_head_seconds=124)
        run = dict(run_id=run_id, status='completed', input_file_sha256=hashes,
                   input_sha256=hashlib.sha256('\n'.join(f'{name}:{hashes[name]}' for name in sorted(hashes)).encode()).hexdigest(),
                   trains_expected=len(trains), trains_departed=len(trains), trains_arrived=len(trains),
                   trains_unfinished=0, trains_not_started=0, horizon_seconds=7200,
                   mean_terminal_delay_seconds=float(delay), max_terminal_delay_seconds=float(delay),
                   p95_terminal_delay_seconds=float(delay), trains=trains,
                   headway_conflicts=[conflict] if delay else [], headway_conflict_count=int(bool(delay)))
        (root / 'run.json').write_text(json.dumps(run))
        events = ET.Element('events')
        for row in sorted(event_rows, key=lambda row:float(row['time'])):
            ET.SubElement(events, 'event', row)
        with gzip.open(events_dir / f'{run_id}.0.events.xml.gz', 'wb') as stream:
            stream.write(ET.tostring(events))
        return run_id

    def test_first_delay_rejects_changed_baseline_physics(self):
        from railsim_mcp.analysis import RunAnalysis
        from railsim_mcp.workspace import WorkspaceError
        analysis = RunAnalysis(RUN_ROOT)
        for filename, original, replacement in [
            ('network.xml', '<network', '<network changed="yes"'),
            ('config.xml', '<config', '<config changed="yes"'),
            ('vehicles.xml', '<vehicleType', '<vehicleType changed="yes"'),
            ('schedule.xml', '00:01:40', '00:01:50'),
        ]:
            with self.subTest(input=filename):
                reference = self.make_run(0, isolated=True)
                root = RUN_ROOT / reference
                source = root / 'inputs' / filename
                source.write_text(source.read_text().replace(original, replacement))
                metadata = json.loads((root / 'run.json').read_text())
                hashes = metadata['input_file_sha256']
                hashes[filename] = hashlib.sha256(source.read_bytes()).hexdigest()
                metadata['input_sha256'] = hashlib.sha256(
                    '\n'.join(f'{name}:{hashes[name]}' for name in sorted(hashes)).encode()).hexdigest()
                (root / 'run.json').write_text(json.dumps(metadata))
                with self.assertRaisesRegex(WorkspaceError, 'identical|same route profile'):
                    analysis.first_delay(self.delayed, reference, 'FV_00_p1')

    async def test_analysis_tools_read_retained_runs_without_running_simulations(self):
        env = os.environ.copy()
        env.pop("PYTHONPATH", None)
        params = StdioServerParameters(command=sys.executable, args=["-m", "railsim_mcp.server"], env=env)
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                names = {tool.name for tool in (await session.list_tools()).tools}
                self.assertTrue({"inspect_train_timeline", "locate_first_delay", "inspect_run_conflicts",
                                 "assess_run_feasibility", "compare_simulation_runs"} <= names)

                timeline = payload(await session.call_tool("inspect_train_timeline", {
                    "run_id": self.delayed, "train_id": "FV_00_p1", "limit": 1,
                }))
                train = timeline["trains"][0]
                self.assertEqual(1800.0, train["scheduled_departure"])
                self.assertEqual(72.0, train["terminal_delay_seconds"])
                self.assertGreaterEqual(len(train["stops"]), 2)
                self.assertIsNotNone(train["stops"][0]["actual_departure"])

                first = payload(await session.call_tool("locate_first_delay", {
                    "run_id": self.delayed, "reference_run_id": self.isolated, "train_id": "FV_00_p1",
                }))
                self.assertEqual(self.isolated, first["reference_run_id"])
                self.assertIsNotNone(first["first_positive_baseline_deviation"])
                self.assertFalse(first["evidence_labels"]["causal_attribution"] == "proven")

                conflicts = payload(await session.call_tool("inspect_run_conflicts", {
                    "run_id": self.delayed, "train_id": "FV_00_p1", "limit": 20,
                }))
                self.assertGreater(conflicts["count"], 0)
                self.assertTrue(all(not item["dispatcher_cause_proven"] for item in conflicts["conflicts"]))

                before = payload(await session.call_tool("assess_run_feasibility", {
                    "run_id": self.delayed, "period_seconds": 1800,
                }))
                after = payload(await session.call_tool("assess_run_feasibility", {
                    "run_id": self.zero_delay, "period_seconds": 1800,
                }))
                self.assertFalse(before["delay_feasible"])
                self.assertFalse(before["headway_admissible"])
                self.assertTrue(after["delay_feasible"])
                self.assertTrue(after["headway_admissible"])
                self.assertEqual(3, len(after["period_train_counts"]))

                comparison = payload(await session.call_tool("compare_simulation_runs", {
                    "before_run_id": self.delayed, "after_run_id": self.zero_delay, "limit": 100,
                }))
                self.assertTrue(comparison["frozen_input_checks"]["before_valid"])
                self.assertTrue(comparison["frozen_input_checks"]["after_valid"])
                self.assertIn("schedule.xml", comparison["frozen_input_checks"]["changed_files"])
                self.assertEqual(0.0, comparison["metrics"]["after"]["max_terminal_delay_seconds"])

                delayed = payload(await session.call_tool("list_delayed_trains", {
                    "run_id": self.delayed, "limit": 2,
                }))
                self.assertGreater(delayed["total"], 2)
                self.assertEqual(2, delayed["next_offset"])
                self.assertTrue(all(t["terminal_delay_seconds"] > 0 for t in delayed["trains"]))
                clean = payload(await session.call_tool("list_delayed_trains", {"run_id": self.zero_delay}))
                self.assertEqual(0, clean["total"])
                movements = payload(await session.call_tool("inspect_train_movements", {
                    "run_id": self.delayed, "train_id": "FV_00_p1", "link_id": "f02", "limit": 200,
                }))
                self.assertGreater(movements["total"], 0)
                self.assertTrue(any(e["type"] == "entered link" for e in movements["events"]))
                self.assertTrue(all("f02" in (e.get("link"), e.get("headLink"), e.get("tailLink"))
                                    for e in movements["events"]))
                self.assertTrue(any(c["release_seconds"]["following"] is not None
                                    for c in conflicts["conflicts"]))

                missing_reference = await session.call_tool("locate_first_delay", {
                    "run_id": self.delayed, "reference_run_id": self.zero_delay, "train_id": "FV_00_p1",
                })
                self.assertTrue(missing_reference.isError)


if __name__ == "__main__":
    unittest.main()
