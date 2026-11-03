"""Confined, copy-on-write workspaces for hand-built Railsim scenarios."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import shlex
import signal
import subprocess
import tempfile
import time
import uuid
import xml.etree.ElementTree as ET
from collections import defaultdict
from .transactions import serialized, commit
from pathlib import Path


class WorkspaceError(ValueError):
    """Raised when a workspace request is invalid or outside its scope."""


_WORKSPACE_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
_FILE_NAMES = {
    "network.xml",
    "schedule.xml",
    "vehicles.xml",
    "config.xml",
    "uc0_operational_plan.json",
    "uc1_operational_plan.json",
    "uc2_operational_plan.json",
    "uc3_operational_plan.json",
}
_ENTITY_FILES = {
    "node": "network.xml", "link": "network.xml",
    "stopFacility": "schedule.xml", "transitLine": "schedule.xml",
    "transitRoute": "schedule.xml", "departure": "schedule.xml",
    "vehicleType": "vehicles.xml", "vehicle": "vehicles.xml",
}
_ENTITY_PARENTS = {
    "node": "nodes", "link": "links", "stopFacility": "transitStops",
    "transitLine": "transitSchedule", "transitRoute": "transitLine",
    "departure": "departures", "vehicleType": "vehicleDefinitions", "vehicle": "vehicleDefinitions",
}
_PLAN_COLLECTIONS = {"product": "products", "flow": "flows", "pattern": "patterns", "mix": "mixes"}


class ScenarioWorkspace:
    """Copies one built-in use case/building block into a confined writable area."""

    def __init__(self, resource_root: Path, workspace_root: Path, *,
                 project_root: Path | None = None, runs_root: Path | None = None):
        self.resource_root = resource_root.resolve()
        self.workspace_root = workspace_root.resolve()
        self.project_root = project_root.resolve() if project_root else None
        self.runs_root = (runs_root or self.workspace_root.parent / "runs").resolve()

    @serialized
    def create(self, workspace_id: str, use_case: str, building_block: str) -> Path:
        if not _WORKSPACE_ID.fullmatch(workspace_id):
            raise WorkspaceError("workspace_id must be 1-40 lowercase letters, digits, '_' or '-'")
        match = re.fullmatch(r"UC([0-3])", use_case)
        block_match = re.fullmatch(r"BB([1-3])", building_block)
        if not match or not block_match:
            raise WorkspaceError("use_case must be UC0..UC3 and building_block must be BB1..BB3")

        use_case_dir = self.resource_root / f"use_case_{match.group(1)}"
        source = use_case_dir / f"building_block_{block_match.group(1)}"
        if not source.is_dir():
            raise WorkspaceError(f"scenario {use_case}/{building_block} does not exist")

        target = self.workspace_root / workspace_id
        if target.exists():
            raise WorkspaceError(f"workspace already exists: {workspace_id}")
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copytree(source, target)
            target.mkdir(exist_ok=True)
            plan = use_case_dir / f"uc{match.group(1)}_operational_plan.json"
            shutil.copy2(plan, target / plan.name)
        except Exception:
            shutil.rmtree(target, ignore_errors=True)
            raise
        return target

    def path_for(self, workspace_id: str, filename: str) -> Path:
        if not _WORKSPACE_ID.fullmatch(workspace_id):
            raise WorkspaceError("invalid workspace_id")
        if filename not in _FILE_NAMES or Path(filename).name != filename:
            raise WorkspaceError(f"unsupported scenario file: {filename}")
        try:
            root = (self.workspace_root / workspace_id).resolve(strict=True)
            path = (root / filename).resolve(strict=True)
        except FileNotFoundError as exc:
            raise WorkspaceError(f"workspace file does not exist: {filename}") from exc
        if not root.is_relative_to(self.workspace_root) or not path.is_relative_to(root):
            raise WorkspaceError("path escapes workspace")
        if not path.is_file():
            raise WorkspaceError(f"scenario file does not exist: {filename}")
        return path

    @serialized
    def read(self, workspace_id: str, filename: str) -> str:
        return self.path_for(workspace_id, filename).read_text(encoding="utf-8")

    @serialized
    def write(self, workspace_id: str, filename: str, content: str) -> None:
        path = self.path_for(workspace_id, filename)
        self._validate(filename, content)
        self._validate_workspace_content(workspace_id, filename, content)
        fd, temp_name = tempfile.mkstemp(prefix=f".{filename}.", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_name, path)
        except Exception:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass
            raise

    def _commit_files(self, workspace_id, files):
        commit(self, workspace_id, files)

    @serialized
    def edit(self, workspace_id, operations, dry_run=False, expected_revision=None):
        from .authoring import Authoring
        return Authoring(self, workspace_id).execute(operations, dry_run, expected_revision)

    @serialized
    def query(self, workspace_id, entity_type, entity_id=None, parent_id=None, offset=0, limit=100, include_children=False):
        from .authoring import Authoring, elements
        if not isinstance(offset, int) or offset < 0 or not isinstance(limit, int) or not 1 <= limit <= 200:
            raise WorkspaceError("offset must be non-negative and limit must be 1..200")
        model = Authoring(self, workspace_id)
        if entity_type == 'config':
            records = [self._entity_record(e) for e in elements(model.roots['config.xml'], 'module')]
            for record in records:
                record['id'] = record['attributes']['name']
        elif entity_type == 'plan':
            records = [{'id':key, 'value':value} for key,value in model.plan.items()]
        else:
            records = self.entities(workspace_id, entity_type)
            if entity_type in ('transitRoute', 'departure'):
                tree = model.roots['schedule.xml']
                parents = {}
                for parent in elements(tree, 'transitLine' if entity_type == 'transitRoute' else 'transitRoute'):
                    for child in elements(parent, entity_type):
                        parents[child.get('id')] = parent.get('id')
                for record in records:
                    record['parent_id'] = parents.get(record['id'])
        records = [r for r in records if (entity_id is None or r['id'] == entity_id)
                   and (parent_id is None or r.get('parent_id') == parent_id)]
        total = len(records)
        records = records[offset:offset+limit]
        if not include_children:
            for record in records:
                record.pop('children', None)
        return {'workspace_id':workspace_id, 'revision':model.revision(), 'total':total, 'offset':offset,
                'next_offset':offset+limit if offset+limit < total else None, 'entities':records}

    @serialized
    def create_empty(self, workspace_id):
        # Reuse the known runner configuration; no network, timetable, fleet or product is inherited.
        path = self.create(workspace_id, 'UC1', 'BB1')
        try:
            files = {'network.xml':'<?xml version="1.0"?><!DOCTYPE network SYSTEM "http://www.matsim.org/files/dtd/network_v2.dtd"><network><nodes/><links/></network>',
                     'schedule.xml':'<?xml version="1.0"?><!DOCTYPE transitSchedule SYSTEM "http://www.matsim.org/files/dtd/transitSchedule_v2.dtd"><transitSchedule><transitStops/></transitSchedule>',
                     'vehicles.xml':'<?xml version="1.0"?><vehicleDefinitions xmlns="http://www.matsim.org/files/dtd" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:schemaLocation="http://www.matsim.org/files/dtd http://www.matsim.org/files/dtd/vehicleDefinitions_v2.0.xsd"/>',
                     'uc1_operational_plan.json':'{"volumes":{"period":1800,"min":0,"max":0,"step":1,"bidirectional":false},"products":{},"flows":{},"patterns":{},"mixes":{},"modes":[]}'}
            self._commit_files(workspace_id, files)
        except BaseException:
            shutil.rmtree(path, ignore_errors=True)
            raise
        return {'workspace_id':workspace_id, 'status':'created', 'configuration_source':'UC1/BB1',
                'revision':self.query(workspace_id, 'node')['revision']}

    def _validate_workspace_content(self, workspace_id: str, filename: str, content: str) -> None:
        """Check cross-file IDs, references, positive physical quantities, and timetable offsets."""
        names = ("network.xml", "schedule.xml", "vehicles.xml", "config.xml")
        files = {name: content if name == filename else self.read(workspace_id, name) for name in names}
        candidates = [f"uc{i}_operational_plan.json" for i in range(4)]
        plan_name = filename if filename in candidates else next(
            (name for name in candidates if (self.workspace_root / workspace_id / name).is_file()), None)
        if plan_name:
            files[plan_name] = content if filename == plan_name else self.read(workspace_id, plan_name)
        self._validate_workspace_files(files, plan_name)

    def _validate_workspace_files(self, files: dict[str, str], plan_name: str | None) -> None:
        def file(name: str) -> str:
            try:
                return files[name]
            except KeyError as exc:
                raise WorkspaceError(f"required scenario file is missing: {name}") from exc

        network = self._parse(file("network.xml"))
        schedule = self._parse(file("schedule.xml"))
        vehicles = self._parse(file("vehicles.xml"))
        for name, root, expected in (("network.xml", network, "network"), ("schedule.xml", schedule, "transitSchedule"),
                                     ("vehicles.xml", vehicles, "vehicleDefinitions"),
                                     ("config.xml", self._parse(file("config.xml")), "config")):
            if self._local(root.tag) != expected:
                raise WorkspaceError(f"{name} must have {expected} root")
        for root, tags in ((network, ('node','link')), (schedule, ('stopFacility','transitLine','transitRoute','departure')),
                           (vehicles, ('vehicleType','vehicle'))):
            for element in root.iter():
                if isinstance(element.tag, str) and self._local(element.tag) in tags and not element.get('id'):
                    raise WorkspaceError(f"{self._local(element.tag)} requires an id")
        ids: dict[str, set[str]] = defaultdict(set)
        for tree in (network, schedule, vehicles):
            for element in tree.iter():
                if isinstance(element.tag, str) and element.get("id"):
                    tag = self._local(element.tag)
                    if element.get("id") in ids[tag]:
                        raise WorkspaceError(f"duplicate {tag} id: {element.get('id')}")
                    ids[tag].add(element.get("id"))

        def positive(value: str | None, label: str) -> float:
            try:
                number = float(value or "")
            except ValueError as exc:
                raise WorkspaceError(f"{label} must be numeric") from exc
            if not math.isfinite(number) or number <= 0:
                raise WorkspaceError(f"{label} must be finite and positive")
            return number

        for node in network.iter():
            if isinstance(node.tag, str) and self._local(node.tag) == "node":
                for coordinate in ("x", "y"):
                    try:
                        number = float(node.get(coordinate, ""))
                    except ValueError as exc:
                        raise WorkspaceError(f"node {node.get('id')} {coordinate} must be numeric") from exc
                    if not math.isfinite(number):
                        raise WorkspaceError(f"node {node.get('id')} {coordinate} must be finite")
        for link in (e for e in network.iter() if isinstance(e.tag, str) and self._local(e.tag) == "link"):
            if link.get("from") not in ids["node"] or link.get("to") not in ids["node"]:
                raise WorkspaceError(f"link {link.get('id')} from/to must reference existing nodes")
            positive(link.get("length"), f"link {link.get('id')} length")
            positive(link.get("freespeed"), f"link {link.get('id')} freespeed")
            for field in ('capacity', 'permlanes'):
                if link.get(field) is not None:
                    positive(link.get(field), f"link {link.get('id')} {field}")
        for stop in (e for e in schedule.iter() if isinstance(e.tag, str) and self._local(e.tag) == "stopFacility"):
            if stop.get("linkRefId") not in ids["link"]:
                raise WorkspaceError(f"stop {stop.get('id')} linkRefId must reference an existing link")
        for route in (e for e in schedule.iter() if isinstance(e.tag, str) and self._local(e.tag) == "transitRoute"):
            stops = [e for e in route.iter() if isinstance(e.tag, str) and self._local(e.tag) == "stop"]
            route_links = [e.get("refId") for e in route.iter()
                           if isinstance(e.tag, str) and self._local(e.tag) == "link"]
            if len(stops) < 2 or not route_links:
                raise WorkspaceError(f"route {route.get('id')} requires a link path and at least two stops")
            if any(link_id not in ids["link"] for link_id in route_links):
                raise WorkspaceError(f"route {route.get('id')} references an unknown network link")
            link_elements = {e.get("id"): e for e in network.iter()
                             if isinstance(e.tag, str) and self._local(e.tag) == "link"}
            for prior, following in zip(route_links, route_links[1:]):
                if link_elements[prior].get("to") != link_elements[following].get("from"):
                    raise WorkspaceError(f"route {route.get('id')} has disconnected links {prior} -> {following}")
            stop_positions = []
            for stop in stops:
                if stop.get("refId") not in ids["stopFacility"]:
                    raise WorkspaceError(f"route {route.get('id')} references an unknown stop")
                facility_id = stop.get("refId")
                facility = next(e for e in schedule.iter() if isinstance(e.tag, str)
                                and self._local(e.tag) == "stopFacility" and e.get("id") == facility_id)
                facility_link = facility.get("linkRefId")
                if facility_link not in route_links:
                    raise WorkspaceError(f"route {route.get('id')} stop {facility_id} is not on its link path")
                stop_positions.append(route_links.index(facility_link))
            if stop_positions[0] != 0 or stop_positions[-1] != len(route_links) - 1:
                raise WorkspaceError(f"route {route.get('id')} path must start and end at its terminal stop links")
            if any(a >= b for a, b in zip(stop_positions, stop_positions[1:])):
                raise WorkspaceError(f"route {route.get('id')} stop links must follow route order")
            prior_departure_offset = -1.0
            for stop in stops:
                arrival = self._seconds(stop.get("arrivalOffset"), f"route {route.get('id')} arrivalOffset")
                departure = self._seconds(stop.get("departureOffset"), f"route {route.get('id')} departureOffset")
                if arrival < prior_departure_offset or departure < arrival:
                    raise WorkspaceError(f"route {route.get('id')} stop offsets must be nondecreasing")
                prior_departure_offset = departure
            departures = [e for e in route.iter() if isinstance(e.tag, str)
                          and self._local(e.tag) == "departure"]

        scheduled_vehicles: set[str] = set()
        for departure in (e for e in schedule.iter() if isinstance(e.tag, str) and self._local(e.tag) == "departure"):
            self._seconds(departure.get("departureTime"), f"departure {departure.get('id')} time")
            if departure.get("vehicleRefId") not in ids["vehicle"]:
                raise WorkspaceError(f"departure {departure.get('id')} vehicleRefId must reference an existing vehicle")
            vehicle_id = departure.get("vehicleRefId")
            if vehicle_id in scheduled_vehicles:
                raise WorkspaceError(f"vehicle {vehicle_id} is assigned to multiple scheduled departures")
            scheduled_vehicles.add(vehicle_id)
        for vehicle in (e for e in vehicles.iter() if isinstance(e.tag, str) and self._local(e.tag) == "vehicle"):
            if vehicle.get("type") not in ids["vehicleType"]:
                raise WorkspaceError(f"vehicle {vehicle.get('id')} type must reference an existing vehicleType")
        for vehicle_type in (e for e in vehicles.iter() if isinstance(e.tag, str) and self._local(e.tag) == "vehicleType"):
            for measure, unit in (("length", "meter"), ("width", "meter"), ("maximumVelocity", "meterPerSecond")):
                element = next((c for c in vehicle_type if isinstance(c.tag, str) and self._local(c.tag) == measure), None)
                if element is not None:
                    positive(element.get(unit), f"vehicleType {vehicle_type.get('id')} {measure}")

        for root in (network, vehicles):
            for attribute in root.iter():
                if not isinstance(attribute.tag, str) or self._local(attribute.tag) != 'attribute':
                    continue
                name, value = attribute.get('name'), attribute.text
                if name in ('railsimAcceleration','railsimDeceleration','railsimTrainCapacity'):
                    positive(value, name)
                if name == 'railsimMinimumTime':
                    try:
                        number = float(value or '')
                        if not math.isfinite(number) or number < 0:
                            raise ValueError
                    except ValueError as exc:
                        raise WorkspaceError('railsimMinimumTime must be finite and non-negative') from exc
        config = self._parse(file('config.xml'))
        module_names = set()
        for module in config:
            if not isinstance(module.tag, str):
                continue
            if module.tag != 'module' or not module.get('name') or module.get('name') in module_names:
                raise WorkspaceError('config requires uniquely named modules')
            module_names.add(module.get('name'))
            for container in module.iter():
                names = set()
                for param in container:
                    if param.tag == 'param':
                        if not param.get('name') or param.get('value') is None or param.get('name') in names:
                            raise WorkspaceError('config parameters require unique names and values')
                        names.add(param.get('name'))
                        words = re.sub(r'([a-z0-9])([A-Z])', r'\1_\2', param.get('name')).lower().split('_')
                        controlled_files = {('network', 'inputNetworkFile'),
                                            ('transit', 'transitScheduleFile'), ('transit', 'vehiclesFile')}
                        if (set(words) & {'file', 'filename', 'url', 'uri'}
                                and (module.get('name'), param.get('name')) not in controlled_files
                                and param.get('value').strip().lower() not in ('', 'null')):
                            raise WorkspaceError('config may reference only the controlled network, schedule and fleet files')
                        if module.get('name') == 'railsim' and param.get('name') in (
                                'accelerationDefault','decelerationDefault','pollInterval','updateInterval'):
                            positive(param.get('value'), param.get('name'))

        if plan_name is not None:
            plan = json.loads(file(plan_name))
            self._validate_plan(plan)
            route_ids = ids["transitRoute"]
            for flow_id, flow in plan.get("flows", {}).items():
                for product_id, directions in flow.get("routes", {}).items():
                    if not isinstance(directions, dict):
                        raise WorkspaceError(f"flow {flow_id} product {product_id} routes must be an object")
                    for direction, route_id in directions.items():
                        if route_id not in route_ids:
                            raise WorkspaceError(f"flow {flow_id} {product_id}/{direction} references unknown route {route_id}")

    @staticmethod
    def _seconds(value: str | None, label: str) -> float:
        try:
            fields = (value or "").split(":")
            if len(fields) != 3:
                raise ValueError
            hour, minute, second = (float(item) for item in fields)
            result = hour * 3600 + minute * 60 + second
            if hour < 0 or not 0 <= minute < 60 or not 0 <= second < 60 or not math.isfinite(result):
                raise ValueError
            return result
        except ValueError as exc:
            raise WorkspaceError(f"{label} must be a valid non-negative HH:MM:SS time") from exc

    @serialized
    def entities(self, workspace_id: str, entity_type: str) -> list[dict]:
        if entity_type in (*_PLAN_COLLECTIONS, "operatingMode"):
            return self._plan_entities(workspace_id, entity_type)
        filename = self._entity_file(entity_type)
        root = self._parse(self.read(workspace_id, filename))
        return [self._entity_record(element) for element in root.iter()
                if isinstance(element.tag, str) and self._local(element.tag) == entity_type]

    @serialized
    def get_entity(self, workspace_id: str, entity_type: str, entity_id: str) -> dict:
        for entity in self.entities(workspace_id, entity_type):
            if entity.get("id") == entity_id:
                return entity
        raise WorkspaceError(f"{entity_type} not found: {entity_id}")

    @serialized
    def upsert_entity(self, workspace_id: str, entity_type: str, entity_id: str,
                      attributes: dict[str, str], create: bool = False) -> dict:
        if entity_type in (*_PLAN_COLLECTIONS, "operatingMode"):
            return self._upsert_plan_entity(workspace_id, entity_type, entity_id, attributes, create)
        if create and entity_type in ("transitRoute", "departure"):
            raise WorkspaceError("use apply_scenario_edits with explicit parent_id for routes/departures")
        filename = self._entity_file(entity_type)
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,100}", entity_id):
            raise WorkspaceError("entity id must be 1-100 letters, digits, '.', '_', ':', or '-'")
        if not isinstance(attributes, dict) or any(
                not isinstance(key, str) or not isinstance(value, (str, int, float))
                for key, value in attributes.items()):
            raise WorkspaceError("attributes must be an object of scalar values")
        path = self.path_for(workspace_id, filename)
        content = path.read_text(encoding="utf-8")
        root = self._parse(content)
        found = next((el for el in root.iter() if isinstance(el.tag, str) and self._local(el.tag) == entity_type
                      and el.get("id") == entity_id), None)
        if create:
            if found is not None:
                raise WorkspaceError(f"{entity_type} already exists: {entity_id}")
            parent_tag = _ENTITY_PARENTS[entity_type]
            parent = next((el for el in root.iter() if self._local(el.tag) == parent_tag), None)
            if parent is None:
                raise WorkspaceError(f"required {parent_tag} container is missing")
            found = ET.SubElement(parent, self._qualified(parent, entity_type), {"id": entity_id})
        elif found is None:
            raise WorkspaceError(f"{entity_type} not found: {entity_id}")
        for key, value in attributes.items():
            if key == "id" or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.:-]{0,99}", key):
                raise WorkspaceError(f"invalid or immutable attribute: {key}")
            found.set(key, str(value))
        self._validate_entity_refs(workspace_id, root, entity_type, found)
        doctype_match = re.search(r"<!DOCTYPE[^>]*>", content, flags=re.IGNORECASE)
        serialized = ET.tostring(root, encoding="unicode", xml_declaration=True)
        if doctype_match:
            serialized = serialized.replace("?>", "?>\n" + doctype_match.group(0), 1)
        self.write(workspace_id, filename, serialized)
        return self._entity_record(found)

    @staticmethod
    def _local(tag: str) -> str:
        return tag.rsplit("}", 1)[-1]

    @classmethod
    def _parse(cls, content: str) -> ET.Element:
        parser = ET.XMLParser(target=ET.TreeBuilder(insert_comments=True))
        try:
            return ET.fromstring(content, parser=parser)
        except ET.ParseError as exc:
            raise WorkspaceError(f"invalid scenario XML: {exc}") from exc

    @staticmethod
    def _qualified(parent: ET.Element, tag: str) -> str:
        if parent.tag.startswith("{"):
            return parent.tag.split("}", 1)[0] + "}" + tag
        return tag

    @classmethod
    def _entity_record(cls, element: ET.Element) -> dict:
        return {"type": cls._local(element.tag), "id": element.get("id"), "text": element.text.strip() if element.text and element.text.strip() else None,
                "attributes": dict(element.attrib),
                "children": [cls._entity_record(child) for child in list(element)
                             if isinstance(child.tag, str)]}

    @staticmethod
    def _entity_file(entity_type: str) -> str:
        try:
            return _ENTITY_FILES[entity_type]
        except KeyError as exc:
            raise WorkspaceError(f"unsupported entity type: {entity_type}") from exc

    def _plan_file(self, workspace_id: str) -> Path:
        root = self.workspace_root / workspace_id
        if not _WORKSPACE_ID.fullmatch(workspace_id) or not root.is_dir():
            raise WorkspaceError("workspace does not exist")
        candidates = [root / f"uc{i}_operational_plan.json" for i in range(4)]
        plan = next((path for path in candidates if path.is_file()), None)
        if plan is None or not plan.resolve().is_relative_to(root.resolve()):
            raise WorkspaceError("workspace operational plan does not exist")
        return self.path_for(workspace_id, plan.name)

    def _plan_entities(self, workspace_id: str, entity_type: str) -> list[dict]:
        value = json.loads(self._plan_file(workspace_id).read_text(encoding="utf-8"))
        if entity_type == "operatingMode":
            return [{"type": entity_type, "id": f"{item['mix']}:{','.join(item['patterns'])}",
                     "value": item} for item in value.get("modes", [])]
        collection = value.get(_PLAN_COLLECTIONS[entity_type], {})
        if not isinstance(collection, dict):
            raise WorkspaceError(f"operational plan {_PLAN_COLLECTIONS[entity_type]} must be an object")
        return [{"type": entity_type, "id": key, "value": item} for key, item in collection.items()]

    def _upsert_plan_entity(self, workspace_id: str, entity_type: str, entity_id: str,
                            value: dict, create: bool) -> dict:
        if not isinstance(value, dict):
            raise WorkspaceError("operational plan entity must be an object")
        path = self._plan_file(workspace_id)
        plan = json.loads(path.read_text(encoding="utf-8"))
        if entity_type == "operatingMode":
            if not isinstance(value.get("mix"), str) or not isinstance(value.get("patterns"), list):
                raise WorkspaceError("operatingMode requires mix and patterns")
            if f"{value['mix']}:{','.join(value['patterns'])}" != entity_id:
                raise WorkspaceError("operatingMode id must match 'mix:pattern1,pattern2'")
            modes = plan.setdefault("modes", [])
            index = next((i for i, item in enumerate(modes)
                          if f"{item.get('mix')}:{','.join(item.get('patterns', []))}" == entity_id), None)
            if create and index is not None:
                raise WorkspaceError(f"operatingMode already exists: {entity_id}")
            if not create and index is None:
                raise WorkspaceError(f"operatingMode not found: {entity_id}")
            if value["mix"] not in plan.get("mixes", {}) or any(p not in plan.get("patterns", {}) for p in value["patterns"]):
                raise WorkspaceError("operatingMode mix and patterns must reference existing entities")
            if index is None:
                modes.append(value)
            else:
                modes[index] = value
            self._validate_plan(plan)
        else:
            collection = plan.setdefault(_PLAN_COLLECTIONS[entity_type], {})
            if create and entity_id in collection:
                raise WorkspaceError(f"{entity_type} already exists: {entity_id}")
            if not create and entity_id not in collection:
                raise WorkspaceError(f"{entity_type} not found: {entity_id}")
            collection[entity_id] = value
            self._validate_plan(plan)
        content = json.dumps(plan, ensure_ascii=False, indent=2) + "\n"
        self.write(workspace_id, path.name, content)
        return {"type": entity_type, "id": entity_id, "value": value}

    @staticmethod
    def _validate_plan(plan: dict) -> None:
        volumes = plan.get("volumes", {})
        if not isinstance(volumes, dict):
            raise WorkspaceError("volumes must be an object")
        if volumes and (not all(isinstance(volumes.get(key), int) and not isinstance(volumes.get(key), bool)
                                for key in ("period", "step", "min", "max"))
                        or volumes["period"] <= 0 or volumes["step"] <= 0
                        or volumes["min"] < 0 or volumes["max"] < volumes["min"]):
            raise WorkspaceError("volumes require positive period/step and 0 <= min <= max")
        collections = {name: plan.get(name, {}) for name in ("products", "flows", "patterns", "mixes")}
        if any(not isinstance(collection, dict) for collection in collections.values()):
            raise WorkspaceError("products, flows, patterns, and mixes must be objects")
        def shares_valid(shares, label):
            if not isinstance(shares, dict) or any(isinstance(v, bool) or not isinstance(v, (int, float))
                    or not math.isfinite(v) or v < 0 for v in shares.values()):
                raise WorkspaceError(f"{label} shares must be finite non-negative numbers")
            if abs(sum(shares.values()) - 1) > 1e-5:
                raise WorkspaceError(f"{label} shares must sum to one")

        products = set(collections["products"])
        flows = collections["flows"]
        patterns = collections["patterns"]
        mixes = collections["mixes"]
        if any(not isinstance(item, dict) for collection in collections.values() for item in collection.values()):
            raise WorkspaceError("operational plan entities must be objects")
        for flow_id, flow in flows.items():
            if not isinstance(flow.get("routes", {}), dict):
                raise WorkspaceError(f"flow {flow_id} routes must be an object")
            for product_id in flow.get("routes", {}):
                if product_id not in products:
                    raise WorkspaceError(f"flow {flow_id} references unknown product {product_id}")
        for pattern_id, pattern in patterns.items():
            if not isinstance(pattern.get("shares", {}), dict):
                raise WorkspaceError(f"pattern {pattern_id} shares must be an object")
            for product_id, shares in pattern.get("shares", {}).items():
                if product_id not in products:
                    raise WorkspaceError(f"pattern {pattern_id} references unknown product {product_id}")
                if not isinstance(shares, dict):
                    raise WorkspaceError(f"pattern {pattern_id} product shares must be an object")
                shares_valid(shares, f"pattern {pattern_id}")
                if any(flow_id not in flows for flow_id in shares):
                    raise WorkspaceError(f"pattern {pattern_id} references an unknown flow")
        for mix_id, mix in mixes.items():
            if not isinstance(mix.get("shares", {}), dict):
                raise WorkspaceError(f"mix {mix_id} shares must be an object")
            if "shares" in mix:
                shares_valid(mix["shares"], f"mix {mix_id}")
            if any(product_id not in products for product_id in mix.get("shares", {})):
                raise WorkspaceError(f"mix {mix_id} references an unknown product")
        modes = plan.get("modes", [])
        if not isinstance(modes, list) or any(not isinstance(mode, dict) for mode in modes):
            raise WorkspaceError("modes must be a list of objects")
        for mode in modes:
            if not isinstance(mode.get("patterns", []), list):
                raise WorkspaceError("operating mode patterns must be a list")
            if mode.get("mix") not in mixes or any(pattern_id not in patterns for pattern_id in mode.get("patterns", [])):
                raise WorkspaceError("operating mode references an unknown mix or pattern")

    def _validate_entity_refs(self, workspace_id: str, root: ET.Element, entity_type: str,
                              element: ET.Element) -> None:
        ids = defaultdict(set)
        for candidate in root.iter():
            if isinstance(candidate.tag, str) and candidate.get("id"):
                ids[self._local(candidate.tag)].add(candidate.get("id"))
        if entity_type == "node":
            for ref in ("inLinks", "outLinks"):
                values = element.find(ref)
                if values is not None and any(item.get("refId") not in ids.get("link", set())
                                              for item in values):
                    raise WorkspaceError(f"{ref} contains an unknown link reference")
        elif entity_type == "link":
            if element.get("from") not in ids.get("node", set()) or element.get("to") not in ids.get("node", set()):
                raise WorkspaceError("link from/to must reference existing network nodes")
        elif entity_type == "departure":
            vehicle_root = self._parse(self.read(workspace_id, "vehicles.xml"))
            ids["vehicle"].update(candidate.get("id") for candidate in vehicle_root.iter()
                                   if self._local(candidate.tag) == "vehicle" and candidate.get("id"))
            if element.get("vehicleRefId") and element.get("vehicleRefId") not in ids.get("vehicle", set()):
                raise WorkspaceError("departure vehicleRefId must reference an existing vehicle")

    def run_once(self, workspace_id: str, seed: int, horizon_seconds: int) -> dict:
        """Run one explicit snapshot of a workspace once.

        The workspace lock covers validation and the immutable input snapshot only,
        so simulations of different workspaces can run concurrently.
        """
        return self._execute_run(**self._snapshot_run(workspace_id, seed, horizon_seconds))

    @serialized
    def _snapshot_run(self, workspace_id, seed, horizon_seconds):
        if self.project_root is None:
            raise WorkspaceError("No simulator configured: provide project_root")
        if isinstance(seed, bool) or not isinstance(seed, int) or not -(2**63) <= seed < 2**63:
            raise WorkspaceError("seed must be a signed 64-bit integer")
        if isinstance(horizon_seconds, bool) or not isinstance(horizon_seconds, int) or not 600 <= horizon_seconds <= 172800:
            raise WorkspaceError("horizon_seconds must be between 600 and 172800")
        source = self.workspace_root / workspace_id
        if not _WORKSPACE_ID.fullmatch(workspace_id) or not source.is_dir():
            raise WorkspaceError("workspace does not exist")
        source = source.resolve(strict=True)
        if not source.is_relative_to(self.workspace_root):
            raise WorkspaceError("workspace escapes configured workspace root")
        # Validate all inputs before snapshotting. config paths are overridden by the fixed Java runner.
        plan_source = self._plan_file(workspace_id)
        filenames = ("network.xml", "schedule.xml", "vehicles.xml", "config.xml", plan_source.name)
        input_contents = {}
        for filename in filenames:
            input_path = (source / filename).resolve(strict=True)
            if not input_path.is_relative_to(source):
                raise WorkspaceError(f"scenario file escapes workspace: {filename}")
            input_contents[filename] = input_path.read_text(encoding="utf-8")
        for filename, content in input_contents.items():
            self._validate(filename, content)
        self._validate_workspace_files(input_contents, plan_source.name)
        run_id = str(uuid.uuid4())
        run_root = self.runs_root / run_id
        snapshot = run_root / "inputs"
        output = run_root / "output"
        snapshot.mkdir(parents=True)
        hashes = {}
        for filename in filenames:
            data = input_contents[filename].encode("utf-8")
            (snapshot / filename).write_bytes(data)
            hashes[filename] = hashlib.sha256(data).hexdigest()
            (snapshot / filename).chmod(0o444)
        aggregate_hash = hashlib.sha256("\n".join(f"{name}:{hashes[name]}" for name in sorted(hashes)).encode()).hexdigest()
        output.mkdir()
        return dict(run_id=run_id, run_root=run_root, snapshot=snapshot, output=output, plan_source=plan_source,
                    hashes=hashes, aggregate_hash=aggregate_hash, seed=seed, horizon_seconds=horizon_seconds)

    def _execute_run(self, run_id, run_root, snapshot, output, plan_source, hashes, aggregate_hash,
                     seed, horizon_seconds):
        args = ["run-once", "--workspace", str(snapshot), "--output", str(output), "--operational-plan",
                str(snapshot / plan_source.name), "--run-id", run_id,
                "--seed", str(seed), "--horizon-seconds", str(horizon_seconds)]
        command = [str(self.project_root / "mvnw"), "-o", "-q", "compile", "exec:java",
                   f"-Dexec.args={shlex.join(args)}"]
        maven_repo = os.environ.get("RAILSIM_MAVEN_REPO")
        if maven_repo:
            command.insert(3, f"-Dmaven.repo.local={Path(maven_repo).resolve()}")
        started = time.monotonic()
        try:
            environment = os.environ.copy()
            environment["XDG_CONFIG_HOME"] = str(run_root / "config")
            process = subprocess.Popen(command, cwd=self.project_root, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        text=True, shell=False, env=environment, start_new_session=True)
            try:
                stdout, stderr = process.communicate(timeout=300)
            except BaseException as exc:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.communicate()
                if isinstance(exc, subprocess.TimeoutExpired):
                    raise WorkspaceError("single simulation exceeded the 300 second execution bound") from exc
                raise
            completed = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
            if completed.returncode != 0:
                message = (completed.stderr or completed.stdout)[-5000:]
                raise WorkspaceError(f"single simulation failed (exit {completed.returncode}): {message}")
            result_path = output / "simulation.json"
            if not result_path.is_file():
                raise WorkspaceError("simulation process returned success without simulation evidence")
            result = json.loads(result_path.read_text(encoding="utf-8"))
            result.update({"input_sha256": aggregate_hash, "input_file_sha256": hashes,
                           "elapsed_seconds": round(time.monotonic() - started, 3),
                           "snapshot_directory": str(snapshot), "output_directory": str(output)})
            (run_root / "run.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
            return result
        except (OSError, json.JSONDecodeError) as exc:
            raise WorkspaceError(f"could not complete single simulation: {exc}") from exc

    def inspect_run(self, run_id: str) -> dict:
        try:
            parsed = str(uuid.UUID(run_id))
        except (ValueError, AttributeError) as exc:
            raise WorkspaceError("invalid run_id") from exc
        run_root = self.runs_root / parsed
        result_path = run_root / "run.json"
        if not result_path.is_file() or not result_path.resolve().is_relative_to(self.runs_root.resolve()):
            raise WorkspaceError("completed simulation run not found")
        result = json.loads(result_path.read_text(encoding="utf-8"))
        delayed = [train for train in result.get("trains", [])
                   if train.get("terminal_delay_seconds") is not None and train["terminal_delay_seconds"] > 0]
        result["delay_conflict_evidence"] = delayed
        result["delay_definition"] = "signed actual minus planned terminal arrival time; unfinished trains are excluded; delay conflict evidence selects positive delays"
        return result

    @staticmethod
    def _validate(filename: str, content: str) -> None:
        if not isinstance(content, str) or not content.strip():
            raise WorkspaceError("content must be a non-empty string")
        try:
            if filename.endswith(".json"):
                value = json.loads(content)
                if not isinstance(value, dict):
                    raise WorkspaceError("operational plan must be a JSON object")
                ScenarioWorkspace._validate_plan(value)
            else:
                upper = content.upper()
                if "<!ENTITY" in upper:
                    raise WorkspaceError("XML entity declarations are not allowed")
                ET.fromstring(content)
        except (json.JSONDecodeError, ET.ParseError) as exc:
            raise WorkspaceError(f"invalid {filename}: {exc}") from exc
