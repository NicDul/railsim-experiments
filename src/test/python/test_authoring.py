import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from railsim_mcp.workspace import ScenarioWorkspace, WorkspaceError
from railsim_mcp import transactions

from railsim_mcp.configuration import project_root
try:
    ROOT = project_root()
except ValueError:
    ROOT = None


@unittest.skipIf(ROOT is None, "Set RAILSIM_PROJECT_ROOT for simulator-template tests")
class AuthoringTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = ScenarioWorkspace(ROOT/'src/main/resources/scenarios', Path(self.temp.name)/'workspaces')
        self.store.create('edit', 'UC1', 'BB1')

    def tearDown(self):
        self.temp.cleanup()

    def snapshot(self):
        return {p.name:p.read_bytes() for p in (self.store.workspace_root/'edit').iterdir() if p.is_file()}

    def add(self, departure='new'):
        return dict(action='add_departure',route_id='RV_LMR',departure_id=departure,
                    departure_time='00:20:00',vehicle_type='RV')

    def test_correct_parent_and_vehicle_created_in_one_batch(self):
        self.store.edit('edit', [self.add()])
        result = self.store.query('edit','departure',entity_id='new')
        self.assertEqual(result['entities'][0]['parent_id'], 'RV_LMR')
        self.assertEqual(self.store.get_entity('edit','vehicle','new_vehicle')['attributes']['type'], 'RV')

    def test_invalid_batch_and_dry_run_preserve_all_bytes(self):
        before = self.snapshot()
        result = self.store.edit('edit', [self.add()], dry_run=True)
        self.assertEqual(result['changed_files'], ['schedule.xml','vehicles.xml'])
        self.assertEqual(before, self.snapshot())
        for operation in (dict(action='delete',entity_type='node',entity_id='n01'),
                          dict(action='update',entity_type='departure',entity_id='new',attributes={'departureTime':'NaN'})):
            with self.assertRaises(WorkspaceError):
                self.store.edit('edit', [self.add(), operation])
            self.assertEqual(before, self.snapshot())

    def test_revision_guard_and_bounded_query(self):
        revision = self.store.query('edit','node',limit=1)['revision']
        self.store.edit('edit', [self.add()], expected_revision=revision)
        with self.assertRaisesRegex(WorkspaceError,'stale'):
            self.store.edit('edit', [self.add('other')], expected_revision=revision)
        result = self.store.query('edit','transitRoute',limit=1)
        self.assertEqual(len(result['entities']), 1)
        self.assertNotIn('children', result['entities'][0])
        self.assertEqual(result['next_offset'], 1)

    def test_rollback_on_second_file_write_failure(self):
        before = self.snapshot()
        original = transactions.replace_text
        failed = False
        def fail_once(path, content):
            nonlocal failed
            if path.name == 'vehicles.xml' and not failed:
                failed = True
                raise OSError('injected disk write failure')
            return original(path, content)
        with patch.object(transactions, 'replace_text', side_effect=fail_once):
            with self.assertRaises(OSError):
                self.store.edit('edit', [self.add()])
        self.assertEqual(before, self.snapshot())

    def test_interrupted_commit_recovered_before_read(self):
        before = self.store.read('edit','network.xml')
        (self.store.workspace_root/'.transaction.json').write_text(json.dumps({
            'workspace_id':'edit','original':{'network.xml':before}}))
        (self.store.workspace_root/'edit/network.xml').write_text('<network/>')
        self.assertEqual(self.store.read('edit','network.xml'), before)

    def test_nested_update_preserves_other_children_and_comments(self):
        original = self.store.get_entity('edit','vehicleType','RV')
        self.store.edit('edit',[dict(action='update',entity_type='vehicleType',entity_id='RV',replace_children={
            'length':[{'type':'length','attributes':{'meter':210}}]})])
        changed = self.store.get_entity('edit','vehicleType','RV')
        self.assertEqual([e for e in original['children'] if e['type']!='length'],
                         [e for e in changed['children'] if e['type']!='length'])
        config = self.store.read('edit','config.xml')
        self.store.edit('edit',[dict(action='set_config',module='railsim',parameters={'pollInterval':2})])
        self.assertIn('Global acceleration', self.store.read('edit','config.xml'))
        with self.assertRaises(WorkspaceError):
            self.store.edit('edit',[dict(action='set_config',module='railsim',parameters={'pollInterval':-1})])

    def test_replacing_timetable_removes_unused_departures_without_dummy_trains(self):
        self.store.edit('edit',[{'action':'replace_departures','departures':[
            {k:v for k,v in self.add().items() if k!='action'}]}])
        self.assertEqual(len(self.store.entities('edit','departure')), 1)
        self.assertEqual(len(self.store.entities('edit','vehicle')), 1)
        self.assertEqual(len(self.store.entities('edit','transitRoute')), 4)

    def test_explicit_parent_and_malformed_child_rejected(self):
        for op in [dict(action='create',entity_type='departure',entity_id='oops'),
                   dict(action='update',entity_type='node',entity_id='n01',replace_children={'route':[]})]:
            with self.assertRaises(WorkspaceError):
                self.store.edit('edit',[op])
        with self.assertRaisesRegex(WorkspaceError,'parent_id'):
            self.store.upsert_entity('edit','departure','oops',{},True)

    def test_empty_scenario_can_be_generated_with_structured_data(self):
        self.store.create_empty('new')
        self.assertEqual(self.store.entities('new','node'), [])
        self.store.edit('new', [dict(action='create',entity_type='node',entity_id='a',attributes={'x':0,'y':0}),
                               dict(action='create',entity_type='node',entity_id='b',attributes={'x':100,'y':0}),
                               dict(action='create',entity_type='link',entity_id='ab',attributes={
                                   'from':'a','to':'b','length':100,'freespeed':10,'capacity':100,'permlanes':1,'modes':'rail'})])
        self.assertEqual(len(self.store.entities('new','link')), 1)

    def test_workspace_directory_symlink_escape_rejected(self):
        (self.store.workspace_root/'escape').symlink_to(ROOT/'src/main/resources/scenarios/use_case_1/building_block_1')
        with self.assertRaises(WorkspaceError):
            self.store.read('escape','network.xml')
        with self.assertRaises(WorkspaceError):
            self.store.entities('escape', 'product')


if __name__ == '__main__':
    unittest.main()
