"""Structured scenario editing. XML never needs to cross the tool boundary."""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import xml.etree.ElementTree as ET

from .workspace import WorkspaceError, _ENTITY_FILES, _ENTITY_PARENTS

# Ordered children accepted by MATSim's scenario readers. Unchanged children survive edits.
CHILDREN = {
    'node': ['attributes'], 'link': ['attributes'], 'stopFacility': ['attributes'],
    'transitLine': ['attributes', 'transitRoute'],
    'transitRoute': ['attributes', 'description', 'transportMode', 'routeProfile', 'route', 'departures'],
    'vehicleType': ['description', 'attributes', 'capacity', 'length', 'width', 'maximumVelocity',
                    'engineInformation', 'costInformation', 'passengerCarEquivalents', 'networkMode', 'flowEfficiencyFactor'],
    'vehicle': ['attributes'], 'departure': [],
    'attributes': ['attribute'], 'attribute': [], 'description': [], 'transportMode': [],
    'routeProfile': ['stop'], 'stop': [], 'route': ['link'], 'departures': ['departure'],
    'capacity': ['seats', 'standingRoom'], 'seats': [], 'standingRoom': [],
    'length': [], 'width': [], 'maximumVelocity': [], 'engineInformation': ['attributes'],
    'costInformation': [], 'passengerCarEquivalents': [], 'networkMode': [], 'flowEfficiencyFactor': [],
    'module': ['param', 'parameterset'], 'parameterset': ['param', 'parameterset'], 'param': [],
}


def elements(root, tag):
    return [e for e in root.iter() if isinstance(e.tag, str) and e.tag.rsplit('}', 1)[-1] == tag]


def scalar(value):
    if isinstance(value, bool):
        return str(value).lower()
    if not isinstance(value, (str, int, float)) or isinstance(value, float) and not math.isfinite(value):
        raise WorkspaceError('values must be finite JSON scalars')
    return str(value)


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,100}', value):
        raise WorkspaceError('id must be 1-100 letters, digits, _, ., :, or -')
    return value


def attrs(element, values):
    if not isinstance(values, dict):
        raise WorkspaceError('attributes must be an object')
    for key, value in values.items():
        if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_.-]{0,99}', key) or key == 'id':
            raise WorkspaceError(f'invalid or immutable attribute: {key}')
        if value is None:
            element.attrib.pop(key, None)
        else:
            element.set(key, scalar(value))


class Authoring:
    def __init__(self, store, workspace_id):
        self.store, self.workspace_id = store, workspace_id
        self.plan_name = store._plan_file(workspace_id).name
        self.original = {name: store.read(workspace_id, name) for name in
                         ('network.xml', 'schedule.xml', 'vehicles.xml', 'config.xml', self.plan_name)}
        self.roots = {name: store._parse(value) for name, value in self.original.items() if name.endswith('.xml')}
        self.plan = json.loads(self.original[self.plan_name])
        self.changed = set()

    def revision(self):
        return hashlib.sha256(json.dumps(self.original, sort_keys=True).encode()).hexdigest()

    def find(self, kind, entity_id):
        matches = [e for e in elements(self.roots[_ENTITY_FILES[kind]], kind) if e.get('id') == entity_id]
        if len(matches) != 1:
            raise WorkspaceError(f'{kind} {entity_id}: expected exactly one entity, found {len(matches)}')
        return matches[0]

    def child(self, parent, tag):
        return ET.SubElement(parent, self.store._qualified(parent, tag))

    def container(self, parent, tag):
        found = next((e for e in parent if isinstance(e.tag, str) and self.store._local(e.tag) == tag), None)
        return found if found is not None else self.child(parent, tag)

    def build(self, parent, record, depth=0):
        if depth > 12 or not isinstance(record, dict) or set(record) - {'type', 'id', 'attributes', 'children', 'text'}:
            raise WorkspaceError('invalid structured child record')
        tag = record.get('type')
        if tag not in CHILDREN.get(self.store._local(parent.tag), []):
            raise WorkspaceError(f'{tag} is not a supported child of {self.store._local(parent.tag)}')
        element = self.child(parent, tag)
        if record.get('id') is not None:
            element.set('id', identifier(record['id']))
        values = dict(record.get('attributes', {}))
        nested_id = values.pop('id', None)
        if nested_id is not None and nested_id != record.get('id'):
            raise WorkspaceError('child id and attributes.id must agree')
        attrs(element, values)
        if record.get('text') is not None:
            element.text = scalar(record['text'])
        for child in record.get('children', []):
            self.build(element, child, depth + 1)
        return element

    def apply(self, op):
        if not isinstance(op, dict):
            raise WorkspaceError('each operation must be an object')
        action = op.get('action')
        allowed = {
            'create': {'entity_type','entity_id','parent_id','attributes','children'},
            'update': {'entity_type','entity_id','attributes','replace_children'},
            'delete': {'entity_type','entity_id'},
            'set_config': {'module','parameters','parameter_sets'},
            'set_plan': {'fields'},
            'add_departure': {'route_id','departure_id','departure_time','vehicle_type','vehicle_id'},
            'replace_departures': {'departures'},
        }
        if action not in allowed or set(op) - allowed[action] - {'action'}:
            raise WorkspaceError(f'unsupported action or fields: {action}')
        if action == 'add_departure':
            identifier(op['departure_id'])
            vehicle_id = op.get('vehicle_id', op['departure_id'] + '_vehicle')
            self.apply({'action':'create','entity_type':'vehicle','entity_id':vehicle_id,
                        'attributes':{'type':op['vehicle_type']}})
            self.apply({'action':'create','entity_type':'departure','entity_id':op['departure_id'],
                        'parent_id':op['route_id'], 'attributes':{'departureTime':op['departure_time'], 'vehicleRefId':vehicle_id}})
            return
        if action == 'replace_departures':
            # Delete only vehicles referenced by the replaced timetable; preserve unrelated inventory.
            used = {e.get('vehicleRefId') for e in elements(self.roots['schedule.xml'], 'departure')}
            for container in elements(self.roots['schedule.xml'], 'departures'):
                for departure in list(container):
                    if isinstance(departure.tag, str) and self.store._local(departure.tag) == 'departure':
                        container.remove(departure)
            fleet = self.roots['vehicles.xml']
            for vehicle in list(fleet):
                if isinstance(vehicle.tag, str) and self.store._local(vehicle.tag) == 'vehicle' and vehicle.get('id') in used:
                    fleet.remove(vehicle)
            self.changed.update(('schedule.xml','vehicles.xml'))
            if not isinstance(op['departures'], list) or len(op['departures']) > 10000:
                raise WorkspaceError('departures must be an array of at most 10000 entries')
            for departure in op['departures']:
                self.apply(dict(departure, action='add_departure'))
            return
        if action == 'set_plan':
            if not isinstance(op['fields'], dict) or set(op['fields']) - {'volumes','products','flows','patterns','mixes','modes','description','name'}:
                raise WorkspaceError('unsupported operational plan fields')
            self.plan.update(copy.deepcopy(op['fields']))
            self.changed.add(self.plan_name)
            return
        if action == 'set_config':
            root = self.roots['config.xml']
            name = identifier(op['module'])
            matches = [e for e in elements(root, 'module') if e.get('name') == name]
            if len(matches) > 1:
                raise WorkspaceError('duplicate config module')
            module = matches[0] if matches else self.child(root, 'module')
            module.set('name', name)
            for key, value in op.get('parameters', {}).items():
                identifier(key)
                matches = [e for e in module if e.tag == 'param' and e.get('name') == key]
                for existing in matches:
                    module.remove(existing)
                if value is not None:
                    ET.SubElement(module, 'param', name=key, value=scalar(value))
            if 'parameter_sets' in op:
                for existing in list(module):
                    if existing.tag == 'parameterset':
                        module.remove(existing)
                for record in op['parameter_sets']:
                    if record.get('type') != 'parameterset':
                        raise WorkspaceError('parameter_sets requires parameterset records')
                    self.build(module, record)
            self.changed.add('config.xml')
            return
        kind, entity_id = op['entity_type'], identifier(op['entity_id'])
        if kind not in _ENTITY_FILES:
            raise WorkspaceError(f'unsupported entity type: {kind}')
        filename = _ENTITY_FILES[kind]
        root = self.roots[filename]
        if action == 'create':
            if any(e.get('id') == entity_id for e in elements(root, kind)):
                raise WorkspaceError(f'{kind} already exists: {entity_id}')
            if kind in ('transitRoute', 'departure'):
                if not op.get('parent_id'):
                    raise WorkspaceError(f'{kind} requires explicit parent_id')
                parent = self.find('transitLine' if kind == 'transitRoute' else 'transitRoute', op['parent_id'])
                if kind == 'departure':
                    parent = self.container(parent, 'departures')
            else:
                if op.get('parent_id'):
                    raise WorkspaceError('parent_id is only used for routes and departures')
                parent_tag = _ENTITY_PARENTS[kind]
                parent = root if self.store._local(root.tag) == parent_tag else self.container(root, parent_tag)
            element = self.child(parent, kind)
            element.set('id', entity_id)
            attrs(element, op.get('attributes', {}))
            for record in op.get('children', []):
                self.build(element, record)
            if kind == 'transitRoute':
                self.container(element, 'departures')
                if not any(e.text and e.text.strip() for e in elements(element, 'transportMode')):
                    raise WorkspaceError('new transitRoute requires transportMode text')
            if kind == 'vehicleType':
                parent.remove(element)
                index = next((i for i,e in enumerate(parent) if isinstance(e.tag,str) and self.store._local(e.tag) == 'vehicle'), len(parent))
                parent.insert(index, element)
        else:
            element = self.find(kind, entity_id)
            if action == 'delete':
                parent = next(p for p in root.iter() if element in list(p))
                parent.remove(element)
            else:
                attrs(element, op.get('attributes', {}))
                groups = op.get('replace_children', {})
                if not isinstance(groups, dict):
                    raise WorkspaceError('replace_children must map child tags to arrays of records')
                for tag, records in groups.items():
                    if tag not in CHILDREN[kind] or not isinstance(records, list):
                        raise WorkspaceError(f'unsupported child group: {tag}')
                    for child in list(element):
                        if isinstance(child.tag, str) and self.store._local(child.tag) == tag:
                            element.remove(child)
                    for record in records:
                        if record.get('type') != tag:
                            raise WorkspaceError('child group and record type must match')
                        self.build(element, record)
        # Maintain schema order without dropping comments, attributes, or unedited groups.
        order = CHILDREN[kind]
        element[:] = sorted(element, key=lambda e: order.index(self.store._local(e.tag))
                            if isinstance(e.tag, str) and self.store._local(e.tag) in order else -1)
        self.changed.add(filename)

    def files(self):
        files = dict(self.original)
        for name in self.changed:
            if name == self.plan_name:
                files[name] = json.dumps(self.plan, indent=2, ensure_ascii=False) + '\n'
            else:
                serialized = ET.tostring(self.roots[name], encoding='unicode', xml_declaration=True)
                doctype = re.search(r'<!DOCTYPE[^>]*>', self.original[name], re.I)
                if doctype:
                    serialized = serialized.replace('?>', '?>\n' + doctype.group(0), 1)
                files[name] = serialized
        return files

    def execute(self, operations, dry_run=False, expected_revision=None):
        if not isinstance(operations, list) or not 1 <= len(operations) <= 10000:
            raise WorkspaceError('operations must contain 1..10000 edits')
        if expected_revision is not None and expected_revision != self.revision():
            raise WorkspaceError('stale scenario revision; query current data before retrying')
        changes = []
        for index, op in enumerate(operations):
            try:
                self.apply(op)
            except (KeyError, TypeError, ValueError, AttributeError) as exc:
                raise WorkspaceError(f'operation {index}: {exc}') from exc
            changes.append({'index':index, 'action':op['action'],
                            **{k:op[k] for k in ('entity_type','entity_id','route_id','departure_id','module') if k in op}})
        files = self.files()
        for name, content in files.items():
            self.store._validate(name, content)
        self.store._validate_workspace_files(files, self.plan_name)
        changed = sorted(name for name in files if files[name] != self.original[name])
        if not dry_run and changed:
            self.store._commit_files(self.workspace_id, {name:files[name] for name in changed})
        return {'workspace_id':self.workspace_id, 'dry_run':dry_run, 'changes':changes,
                'changed_files':changed, 'previous_revision':self.revision(),
                'revision':hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()}
