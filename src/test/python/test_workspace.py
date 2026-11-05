import json
import tempfile
import unittest
from pathlib import Path

from railsim_mcp.workspace import ScenarioWorkspace, WorkspaceError


class ScenarioWorkspaceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.resources = self.root / "resources"
        scenario = self.resources / "use_case_1" / "building_block_1"
        scenario.mkdir(parents=True)
        (scenario / "network.xml").write_text(
            "<network><nodes><node id='n01' x='0' y='0'/><node id='n02' x='1' y='0'/>"
            "<node id='n03' x='2' y='0'/></nodes><links>"
            "<link id='l01' from='n01' to='n02' length='1' freespeed='1'/>"
            "<link id='l02' from='n02' to='n03' length='1' freespeed='1'/></links></network>",
            encoding="utf-8")
        (scenario / "schedule.xml").write_text(
            "<transitSchedule><transitStops><stopFacility id='S1' linkRefId='l01'/>"
            "<stopFacility id='S2' linkRefId='l02'/></transitStops>"
            "<transitLine id='L1'><transitRoute id='R1'><routeProfile>"
            "<stop refId='S1' arrivalOffset='00:00:00' departureOffset='00:00:00'/>"
            "<stop refId='S2' arrivalOffset='00:01:00' departureOffset='00:01:00'/>"
            "</routeProfile><route><link refId='l01'/><link refId='l02'/></route><departures>"
            "<departure id='D1' departureTime='00:00:00' vehicleRefId='V1'/></departures>"
            "</transitRoute></transitLine></transitSchedule>", encoding="utf-8")
        (scenario / "vehicles.xml").write_text(
            "<vehicleDefinitions><vehicleType id='T1'/><vehicle id='V1' type='T1'/></vehicleDefinitions>",
            encoding="utf-8")
        (scenario / "config.xml").write_text("<config />", encoding="utf-8")
        (self.resources / "use_case_1" / "uc1_operational_plan.json").write_text(
            '{"volumes":{"period":1800,"min":1,"max":2,"step":1},"products":{"FV":{}},'
            '"flows":{"F1":{"routes":{"FV":{"forward":"R1"}}}},'
            '"patterns":{"FV_PASS":{}},"mixes":{"MAINLINE":{}},"modes":[]}',
            encoding="utf-8")
        self.workspace_root = self.root / "workspaces"
        self.store = ScenarioWorkspace(self.resources, self.workspace_root)

    def tearDown(self):
        self.temp.cleanup()

    def test_create_copies_only_selected_scenario_into_workspace(self):
        path = self.store.create("experiment-1", "UC1", "BB1")
        self.assertIn("<node id='n01' x='0' y='0'/>", (path / "network.xml").read_text(encoding="utf-8"))
        self.assertTrue((path / "uc1_operational_plan.json").is_file())

    def test_write_validates_before_atomic_replace(self):
        self.store.create("experiment-1", "UC1", "BB1")
        original = self.store.read("experiment-1", "network.xml")
        with self.assertRaises(WorkspaceError):
            self.store.write("experiment-1", "network.xml",
                             "<network><nodes/><links><link id='bad' from='n01' to='missing' length='1' freespeed='1'/>"
                             "</links></network>")
        with self.assertRaises(WorkspaceError):
            self.store.write("experiment-1", "network.xml", "<network>")
        self.assertEqual(original, self.store.read("experiment-1", "network.xml"))

    def test_rejects_paths_and_xml_entities(self):
        self.store.create("experiment-1", "UC1", "BB1")
        with self.assertRaises(WorkspaceError):
            self.store.read("experiment-1", "../network.xml")
        with self.assertRaises(WorkspaceError):
            self.store.write("experiment-1", "network.xml", "<!DOCTYPE x [<!ENTITY e SYSTEM 'file:///etc/passwd'>]><x>&e;</x>")

    def test_rejects_workspace_symlink_escape(self):
        workspace = self.store.create("experiment-1", "UC1", "BB1")
        outside = self.root / "outside.xml"
        outside.write_text("<outside />", encoding="utf-8")
        (workspace / "network.xml").unlink()
        (workspace / "network.xml").symlink_to(outside)
        with self.assertRaises(WorkspaceError):
            self.store.read("experiment-1", "network.xml")

    def test_rejects_invalid_identifiers_and_existing_workspace(self):
        with self.assertRaises(WorkspaceError):
            self.store.create("../escape", "UC1", "BB1")
        self.store.create("experiment-1", "UC1", "BB1")
        with self.assertRaises(WorkspaceError):
            self.store.create("experiment-1", "UC1", "BB1")

    def test_entity_access_updates_schedule_and_rejects_bad_references(self):
        self.store.create("experiment-1", "UC1", "BB1")
        route = self.store.get_entity("experiment-1", "transitRoute", "R1")
        self.assertEqual("transitRoute", route["type"])
        departure = self.store.get_entity("experiment-1", "departure", "D1")
        self.store.upsert_entity("experiment-1", "departure", departure["id"],
                                 {"departureTime": "00:00:30"})
        self.assertEqual("00:00:30", self.store.get_entity(
            "experiment-1", "departure", departure["id"])["attributes"]["departureTime"])
        with self.assertRaises(WorkspaceError):
            self.store.upsert_entity("experiment-1", "link", "bad-link",
                                     {"from": "missing-node", "to": "n01"}, create=True)

    def test_operational_plan_entities_validate_mode_references(self):
        self.store.create("experiment-1", "UC1", "BB1")
        products = self.store.entities("experiment-1", "product")
        self.assertIn("FV", [entity["id"] for entity in products])
        self.store.upsert_entity("experiment-1", "operatingMode", "MAINLINE:FV_PASS",
                                 {"mix": "MAINLINE", "patterns": ["FV_PASS"]}, create=True)
        self.assertIn("MAINLINE:FV_PASS", [entity["id"] for entity in
                                           self.store.entities("experiment-1", "operatingMode")])
        with self.assertRaises(WorkspaceError):
            self.store.upsert_entity("experiment-1", "operatingMode", "BAD:missing",
                                     {"mix": "absent", "patterns": ["missing"]}, create=True)

    def test_whole_plan_write_validates_references_before_replacing_file(self):
        self.store.create("experiment-1", "UC1", "BB1")
        filename = "uc1_operational_plan.json"
        original = self.store.read("experiment-1", filename)
        invalid_plan = json.loads(original)
        invalid_plan["flows"] = {"F1": {"routes": {"UNKNOWN": {}}}}
        invalid = json.dumps(invalid_plan)

        with self.assertRaisesRegex(WorkspaceError, "unknown product UNKNOWN"):
            self.store.write("experiment-1", filename, invalid)

        self.assertEqual(original, self.store.read("experiment-1", filename))

    def test_whole_plan_write_rejects_invalid_collection_shapes(self):
        self.store.create("experiment-1", "UC1", "BB1")
        with self.assertRaisesRegex(WorkspaceError, "must be objects"):
            self.store.write("experiment-1", "uc1_operational_plan.json", '{"products": []}')

    def test_rejects_disconnected_route_path_without_replacing_schedule(self):
        self.store.create("experiment-1", "UC1", "BB1")
        filename = "schedule.xml"
        original = self.store.read("experiment-1", filename)
        disconnected = original.replace("<link refId='l02'/>", "<link refId='l01'/>")
        if disconnected == original:
            disconnected = original.replace("<link refId='l02' />", "<link refId='l01' />")
        with self.assertRaisesRegex(WorkspaceError, "disconnected links"):
            self.store.write("experiment-1", filename, disconnected)
        self.assertEqual(original, self.store.read("experiment-1", filename))

    def test_rejects_plan_reference_to_route_removed_from_schedule(self):
        self.store.create("experiment-1", "UC1", "BB1")
        schedule = self.store.read("experiment-1", "schedule.xml").replace("id='R1'", "id='R2'")
        with self.assertRaisesRegex(WorkspaceError, "unknown route"):
            self.store.write("experiment-1", "schedule.xml", schedule)

    def test_rejects_reusing_vehicle_for_multiple_departures_before_run(self):
        self.store.create("experiment-1", "UC1", "BB1")
        schedule = self.store.read("experiment-1", "schedule.xml")
        schedule = schedule.replace("</departures>",
                                    "<departure id='D2' departureTime='00:02:00' vehicleRefId='V1'/>"
                                    "</departures>")
        with self.assertRaisesRegex(WorkspaceError, "multiple scheduled departures"):
            self.store.write("experiment-1", "schedule.xml", schedule)

    def test_rejects_additional_config_input_files(self):
        self.store.create('experiment-1', 'UC1', 'BB1')
        original = self.store.read('experiment-1', 'config.xml')
        with self.assertRaisesRegex(WorkspaceError, 'controlled network'):
            self.store.write('experiment-1', 'config.xml',
                             '<config><module name="plans"><param name="inputPlansFile" '
                             'value="/outside/plans.xml"/></module></config>')
        self.assertEqual(original, self.store.read('experiment-1', 'config.xml'))


if __name__ == "__main__":
    unittest.main()
