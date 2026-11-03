"""Build an entire scenario from structured MCP results, edit it, then run MATSim."""
import json
import os
from pathlib import Path
import shutil
import sys
import unittest
import uuid

from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

ROOT = Path(__file__).resolve().parents[3]
from railsim_mcp.configuration import state_root


class StructuredAuthoringTest(unittest.IsolatedAsyncioTestCase):
    async def test_generate_edit_and_simulate_without_xml_payloads(self):
        prefix = 'authoring-' + uuid.uuid4().hex[:10]
        template, target = prefix+'-src', prefix+'-new'
        env = dict(os.environ)
        env.pop('PYTHONPATH', None)
        params = StdioServerParameters(command=sys.executable, args=['-m','railsim_mcp.server'], env=env)
        run_id = None
        try:
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    async def call(name, **args):
                        result = await session.call_tool(name,args)
                        self.assertFalse(result.isError, result.content)
                        return result.structuredContent or json.loads(result.content[0].text)
                    discovered = {t.name for t in (await session.list_tools()).tools}
                    self.assertTrue({'apply_scenario_edits','query_scenario','add_train_departure',
                                     'move_train_departure','replace_timetable','create_empty_scenario'} <= discovered)
                    await call('create_scenario_workspace',workspace_id=template,use_case='UC1',building_block='BB1')
                    await call('create_empty_scenario',workspace_id=target)
                    ops = []
                    for kind in ('node','link','stopFacility','vehicleType','vehicle','transitLine'):
                        rows = await call('query_scenario',workspace_id=template,entity_type=kind,include_children=True)
                        for row in rows['entities']:
                            ops.append(dict(action='create',entity_type=kind,entity_id=row['id'],
                                attributes={k:v for k,v in row['attributes'].items() if k!='id'},children=row['children']))
                    plan = await call('query_scenario',workspace_id=template,entity_type='plan')
                    ops.append({'action':'set_plan','fields':{r['id']:r['value'] for r in plan['entities']}})
                    ops.append({'action':'set_config','module':'railsim','parameters':{'pollInterval':1.0}})
                    dry = await call('apply_scenario_edits',workspace_id=target,operations=ops,dry_run=True)
                    self.assertTrue(dry['dry_run'])
                    self.assertEqual((await call('query_scenario',workspace_id=target,entity_type='node'))['total'],0)
                    await call('apply_scenario_edits',workspace_id=target,operations=ops,expected_revision=dry['previous_revision'])
                    await call('replace_timetable',workspace_id=target,departures=[{
                        'route_id':'FV_LR','departure_id':'service','departure_time':'00:00:00','vehicle_type':'FV'}],
                        periods=2,period_seconds=1800)
                    await call('add_train_departure',workspace_id=target,route_id='RV_LMR',departure_id='extra',
                               departure_time='01:00:00',vehicle_type='RV')
                    await call('move_train_departure',workspace_id=target,departure_id='extra',departure_time='01:05:00')
                    rows = await call('query_scenario',workspace_id=target,entity_type='departure',parent_id='RV_LMR')
                    self.assertEqual(rows['entities'][0]['attributes']['departureTime'],'01:05:00')
                    rejected = await session.call_tool('apply_scenario_edits',{'workspace_id':target,'operations':[
                        {'action':'delete','entity_type':'node','entity_id':'n01'}]})
                    self.assertTrue(rejected.isError)
                    result = await call('run_simulation_once',workspace_id=target,horizon_seconds=5400)
                    run_id = result['run_id']
                    self.assertEqual(result['trains_expected'],3)
                    self.assertEqual(result['trains_arrived'],3)
                    self.assertEqual(result['trains_unfinished'],0)
        finally:
            for name in (template,target):
                shutil.rmtree(state_root()/'workspaces'/name,ignore_errors=True)
            if run_id:
                shutil.rmtree(state_root()/'runs'/run_id,ignore_errors=True)


if __name__ == '__main__':
    unittest.main()
