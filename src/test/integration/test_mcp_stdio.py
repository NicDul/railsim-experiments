"""End-to-end stdio check using the official MCP Python client SDK."""

import asyncio
import copy
import json
import os
import re
import shutil
import sys
import unittest
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path

from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


PROJECT_ROOT = Path(__file__).resolve().parents[3]
from railsim_mcp.configuration import state_root
WORKSPACE_ROOT = state_root() / "workspaces"
RUN_ROOT = state_root() / "runs"


def payload(result):
    if result.structuredContent is not None:
        return result.structuredContent
    return json.loads(result.content[0].text)


def edited_xml(content, edit):
    doctype = re.search(r"<!DOCTYPE[^>]*>", content, flags=re.IGNORECASE)
    root = ET.fromstring(content)
    edit(root)
    serialized = ET.tostring(root, encoding="unicode", xml_declaration=True)
    if doctype:
        serialized = serialized.replace("?>", "?>\n" + doctype.group(0), 1)
    return serialized


class StdioServerClientTest(unittest.IsolatedAsyncioTestCase):
    async def test_discovery_workspace_mutation_and_validation_over_stdio(self):
        workspace_id = f"mcp-client-{uuid.uuid4().hex[:12]}"
        environment = os.environ.copy()
        environment.pop("PYTHONPATH", None)  # Exercise the installed wheel, not the source tree.
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "railsim_mcp.server"],
            env=environment,
        )
        run_id = None
        run_ids = []
        try:
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    self.assertTrue({"create_scenario_workspace", "read_scenario_file", "write_scenario_file",
                                     "list_scenario_entities", "get_scenario_entity", "upsert_scenario_entity",
                                     "run_simulation_once", "inspect_simulation_run"}
                                    <= {tool.name for tool in tools.tools})

                    created = await session.call_tool("create_scenario_workspace", {
                        "workspace_id": workspace_id,
                        "use_case": "UC1",
                        "building_block": "BB1",
                    })
                    self.assertFalse(created.isError)

                    # Build a new runnable line and train in this template-derived workspace.
                    network = payload(await session.call_tool("read_scenario_file", {
                        "workspace_id": workspace_id, "filename": "network.xml",
                    }))['content']

                    def add_network_objects(root):
                        nodes = root.find("nodes")
                        nodes.append(ET.Element("node", {"id": "mcp_node", "x": "10800", "y": "0"}))
                        links = root.find("links")
                        links.append(ET.Element("link", {"id": "mcp_link", "from": "n11", "to": "mcp_node",
                                                          "length": "70", "freespeed": "30", "capacity": "9999",
                                                          "permlanes": "1", "modes": "rail"}))

                    network = edited_xml(network, add_network_objects)
                    write = await session.call_tool("write_scenario_file", {
                        "workspace_id": workspace_id, "filename": "network.xml", "content": network,
                    })
                    self.assertFalse(write.isError, write.content)
                    for entity_type, entity_id in (("node", "mcp_node"), ("link", "mcp_link")):
                        listed = await session.call_tool("list_scenario_entities", {
                            "workspace_id": workspace_id, "entity_type": entity_type,
                        })
                        self.assertIn(entity_id, [item["id"] for item in payload(listed)["entities"]])

                    schedule = payload(await session.call_tool("read_scenario_file", {
                        "workspace_id": workspace_id, "filename": "schedule.xml",
                    }))['content']

                    def add_transit_line(root):
                        source = root.find("./transitLine[@id='FV']")
                        line = ET.Element(source.tag, {"id": "MCP_CREATED_LINE"})
                        route = copy.deepcopy(source.find("./transitRoute[@id='FV_LR']"))
                        route.set("id", "MCP_CREATED_ROUTE")
                        departures = route.find("departures")
                        departures.clear()
                        ET.SubElement(departures, "departure", {"id": "MCP_CREATED_DEPARTURE",
                                      "departureTime": "00:30:00", "vehicleRefId": "MCP_CREATED_VEHICLE"})
                        line.append(route)
                        root.append(line)

                    schedule = edited_xml(schedule, add_transit_line)

                    vehicles = payload(await session.call_tool("read_scenario_file", {
                        "workspace_id": workspace_id, "filename": "vehicles.xml",
                    }))['content']

                    def add_vehicle(root):
                        source = next(element for element in root.iter()
                                      if element.tag.endswith("vehicleType") and element.get("id") == "FV")
                        vehicle_type = copy.deepcopy(source)
                        vehicle_type.set("id", "MCP_CREATED_TYPE")
                        first_vehicle = next(index for index, element in enumerate(list(root))
                                             if element.tag.endswith("vehicle"))
                        root.insert(first_vehicle, vehicle_type)
                        vehicle = ET.Element(root.tag.split("}")[0] + "}vehicle" if "}" in root.tag else "vehicle",
                                             {"id": "MCP_CREATED_VEHICLE", "type": "MCP_CREATED_TYPE"})
                        root.append(vehicle)

                    vehicles = edited_xml(vehicles, add_vehicle)
                    write = await session.call_tool("write_scenario_file", {
                        "workspace_id": workspace_id, "filename": "vehicles.xml", "content": vehicles,
                    })
                    self.assertFalse(write.isError, write.content)
                    for entity_type, entity_id in (("vehicleType", "MCP_CREATED_TYPE"),
                                                   ("vehicle", "MCP_CREATED_VEHICLE")):
                        listed = await session.call_tool("list_scenario_entities", {
                            "workspace_id": workspace_id, "entity_type": entity_type,
                        })
                        self.assertIn(entity_id, [item["id"] for item in payload(listed)["entities"]])

                    write = await session.call_tool("write_scenario_file", {
                        "workspace_id": workspace_id, "filename": "schedule.xml", "content": schedule,
                    })
                    self.assertFalse(write.isError, write.content)
                    for entity_type, entity_id in (("transitLine", "MCP_CREATED_LINE"),
                                                   ("transitRoute", "MCP_CREATED_ROUTE"),
                                                   ("departure", "MCP_CREATED_DEPARTURE")):
                        listed = await session.call_tool("list_scenario_entities", {
                            "workspace_id": workspace_id, "entity_type": entity_type,
                        })
                        self.assertIn(entity_id, [item["id"] for item in payload(listed)["entities"]])
                    invalid_schedule = edited_xml(schedule, lambda root: root.find(
                        "./transitLine[@id='FV']/transitRoute[@id='FV_LR']/route/link[2]"
                    ).set("refId", "f10"))
                    rejected_route = await session.call_tool("write_scenario_file", {
                        "workspace_id": workspace_id, "filename": "schedule.xml", "content": invalid_schedule,
                    })
                    self.assertTrue(rejected_route.isError)

                    existing_pattern = await session.call_tool("get_scenario_entity", {
                        "workspace_id": workspace_id, "entity_type": "pattern", "entity_id": "FV_PASS",
                    })
                    pattern_value = payload(existing_pattern)["value"]
                    pattern_value["description"] = "Created through MCP for runnable line coverage"
                    pattern = await session.call_tool("upsert_scenario_entity", {
                        "workspace_id": workspace_id, "entity_type": "pattern", "entity_id": "MCP_PATTERN",
                        "value": pattern_value, "create": True,
                    })
                    self.assertFalse(pattern.isError, pattern.content)
                    mode = await session.call_tool("upsert_scenario_entity", {
                        "workspace_id": workspace_id, "entity_type": "operatingMode",
                        "entity_id": "MAINLINE:MCP_PATTERN",
                        "value": {"mix": "MAINLINE", "patterns": ["MCP_PATTERN"]}, "create": True,
                    })
                    self.assertFalse(mode.isError, mode.content)
                    pattern_readback = await session.call_tool("get_scenario_entity", {
                        "workspace_id": workspace_id, "entity_type": "pattern", "entity_id": "MCP_PATTERN",
                    })
                    self.assertEqual("MCP_PATTERN", payload(pattern_readback)["id"])

                    routes = await session.call_tool("list_scenario_entities", {
                        "workspace_id": workspace_id, "entity_type": "transitRoute",
                    })
                    self.assertFalse(routes.isError, routes.content)
                    route_ids = [entity["id"] for entity in payload(routes)["entities"]]
                    self.assertIn("FV_LMR", route_ids)
                    # Exercise mutations across the network, schedule, fleet, and plan formats.
                    for entity_type in ("node", "link", "stopFacility", "transitLine", "transitRoute",
                                        "vehicleType", "vehicle", "product", "flow", "pattern", "mix"):
                        listed = await session.call_tool("list_scenario_entities", {
                            "workspace_id": workspace_id, "entity_type": entity_type,
                        })
                        self.assertFalse(listed.isError, (entity_type, listed.content))
                        entity = payload(listed)["entities"][0]
                        fetched = await session.call_tool("get_scenario_entity", {
                            "workspace_id": workspace_id, "entity_type": entity_type, "entity_id": entity["id"],
                        })
                        self.assertFalse(fetched.isError, (entity_type, fetched.content))
                        value = entity.get("value", {key: val for key, val in entity.get("attributes", {}).items()
                                                       if key != "id"})
                        updated = await session.call_tool("upsert_scenario_entity", {
                            "workspace_id": workspace_id, "entity_type": entity_type,
                            "entity_id": entity["id"], "value": value,
                        })
                        self.assertFalse(updated.isError, (entity_type, updated.content))
                    departure = await session.call_tool("get_scenario_entity", {
                        "workspace_id": workspace_id, "entity_type": "departure", "entity_id": "FV_LMR_dep_0",
                    })
                    self.assertFalse(departure.isError, departure.content)
                    changed = await session.call_tool("upsert_scenario_entity", {
                        "workspace_id": workspace_id, "entity_type": "departure",
                        "entity_id": "FV_LMR_dep_0", "value": {"departureTime": "00:00:30"},
                    })
                    self.assertFalse(changed.isError, changed.content)
                    plan_entity = await session.call_tool("upsert_scenario_entity", {
                        "workspace_id": workspace_id, "entity_type": "operatingMode",
                        "entity_id": "MAINLINE:FV_PASS",
                        "value": {"mix": "MAINLINE", "patterns": ["FV_PASS"]}, "create": True,
                    })
                    self.assertFalse(plan_entity.isError, plan_entity.content)
                    invalid_mode = await session.call_tool("upsert_scenario_entity", {
                        "workspace_id": workspace_id, "entity_type": "operatingMode",
                        "entity_id": "MCP_BAD:missing", "value": {"mix": "missing", "patterns": ["missing"]},
                        "create": True,
                    })
                    self.assertTrue(invalid_mode.isError)
                    original = await session.call_tool("read_scenario_file", {
                        "workspace_id": workspace_id,
                        "filename": "network.xml",
                    })
                    self.assertFalse(original.isError)
                    content = payload(original)["content"]

                    updated = await session.call_tool("write_scenario_file", {
                        "workspace_id": workspace_id,
                        "filename": "network.xml",
                        "content": content,
                    })
                    self.assertFalse(updated.isError, updated.content)
                    rejected = await session.call_tool("write_scenario_file", {
                        "workspace_id": workspace_id,
                        "filename": "network.xml",
                        "content": "<network>",
                    })
                    self.assertTrue(rejected.isError)
                    run = await session.call_tool("run_simulation_once", {
                        "workspace_id": workspace_id, "seed": 4711, "horizon_seconds": 3600,
                    })
                    self.assertFalse(run.isError, run.content)
                    run_id = payload(run)["run_id"]
                    run_ids.append(run_id)
                    self.assertEqual(5, len(payload(run)["input_file_sha256"]))
                    self.assertEqual(5, payload(run)["trains_expected"])
                    self.assertEqual(5, payload(run)["trains_arrived"])
                    self.assertEqual(0, payload(run)["trains_unfinished"])
                    inspected = await session.call_tool("inspect_simulation_run", {"run_id": run_id})
                    self.assertFalse(inspected.isError, inspected.content)
                    self.assertEqual(run_id, payload(inspected)["run_id"])
                    self.assertIn("delay_conflict_evidence", payload(inspected))
                    self.assertIn("headway_conflicts", payload(inspected))

                    short_run = await session.call_tool("run_simulation_once", {
                        "workspace_id": workspace_id, "seed": 4711, "horizon_seconds": 600,
                    })
                    self.assertFalse(short_run.isError, short_run.content)
                    short = payload(short_run)
                    run_ids.append(short["run_id"])
                    self.assertEqual(4, short["trains_expected"])
                    self.assertGreater(short["trains_unfinished"], 0)
                    self.assertEqual(short["trains_arrived"], short["terminal_delay_sample_count"])
                    unfinished = [train for train in short["trains"] if train["status"] == "unfinished"]
                    self.assertTrue(unfinished)
                    self.assertTrue(all(train["actual_arrival"] is None and train["terminal_delay_seconds"] is None
                                        for train in unfinished))
                    stopped_midroute = next(train for train in short["trains"]
                                            if train["departure_id"] == "FV_LMR_dep_0")
                    self.assertEqual("unfinished", stopped_midroute["status"])
                    self.assertIsNone(stopped_midroute["terminal_delay_seconds"])
        finally:
            shutil.rmtree(WORKSPACE_ROOT / workspace_id, ignore_errors=True)
            for completed_run_id in ([run_id] if run_id else []) + locals().get("run_ids", []):
                shutil.rmtree(RUN_ROOT / completed_run_id, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
