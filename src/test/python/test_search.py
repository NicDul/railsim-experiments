import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from railsim_mcp.search import composition, validate_pattern, evaluate, Search, bounded_process
from railsim_mcp.configuration import Settings

class SearchTests(unittest.TestCase):
    def fixture(self):
        expected={'FV_0':(3600,'FV'),'RV_0':(7199,'RV')}
        refs={'FV':100,'RV':200}
        rows=[dict(vehicle_id=k,status='terminal_arrived',scheduled_departure=t,planned_arrival=t+refs[p],actual_arrival=t+refs[p],terminal_delay_seconds=0) for k,(t,p) in expected.items()]
        return dict(status='completed',trains=rows,headway_conflicts=[{'deficit':30}]),expected,refs

    def test_rounded_mix(self):
        self.assertEqual(composition(4),dict(FV=2,RV=1,GV=1))
        for v in range(4,101): self.assertEqual(sum(composition(v).values()),v)
        self.assertEqual(composition(5),dict(FV=2,RV=2,GV=1))

    def test_pattern_rejects_changes(self):
        valid=[['FV',0],['FV',100],['RV',200],['GV',1799]]
        self.assertEqual(len(validate_pattern(valid,4)),4)
        for bad in (valid[:-1], valid+[['RV',10]], [['FV',1800]]+valid[1:], [['FV',True]]+valid[1:]):
            with self.assertRaises(ValueError): validate_pattern(bad,4)

    def test_headways_and_noncohort_delays_are_diagnostic(self):
        run,expected,refs=self.fixture()
        run['trains'].append(dict(vehicle_id='outside',scheduled_departure=7200,status='unfinished'))
        self.assertTrue(evaluate(run,expected,refs)['passed'])

    def test_raw_positive_delay_fails(self):
        run,expected,refs=self.fixture()
        run['trains'][0]['actual_arrival']+=.00001
        run['trains'][0]['terminal_delay_seconds']=.00001
        self.assertFalse(evaluate(run,expected,refs)['passed'])

    def test_missing_and_invalid_cannot_pass(self):
        for mode in ('missing','unfinished','nan','reference','duplicate','notcompleted'):
            run,expected,refs=self.fixture()
            if mode=='missing': run['trains'].pop()
            if mode=='unfinished': run['trains'][0]['status']='unfinished'
            if mode=='nan': run['trains'][0]['actual_arrival']=float('nan')
            if mode=='reference': run['trains'][0]['planned_arrival']+=1
            if mode=='duplicate': run['trains'].append(copy.deepcopy(run['trains'][0]))
            if mode=='notcompleted': run['status']='timeout'
            self.assertFalse(evaluate(run,expected,refs)['passed'],mode)
        self.assertFalse(evaluate(dict(status='completed',trains=[]),{},refs)['passed'])

    def test_recovered_delay_is_accepted(self):
        run,expected,refs=self.fixture()
        run['intermediate_delay']=10
        self.assertTrue(evaluate(run,expected,refs)['passed'])

    def test_fake_controller_simulates_evaluates_and_checkpoints(self):
        with tempfile.TemporaryDirectory() as d:
            settings=Settings(Path(d),Path(d)/'state')
            with patch.object(Search,'revision',return_value='test'):
                search=Search(settings,Path(d)/'result.json',simulations=4)
            search.state.update(frozen={'hash':'fixed'},baselines={p:dict(terminal=100,trajectory={}) for p in ('FV','RV','GV')})
            def simulation(pattern):
                search.state['simulations']+=1
                rows=[]
                for k in (2,3):
                    for i,(p,t) in enumerate(pattern):
                        dep=t+k*1800
                        rows.append(dict(vehicle_id=f'{p}_{i:02}_p{k}',scheduled_departure=dep,planned_arrival=dep+100,actual_arrival=dep+100,terminal_delay_seconds=0,status='terminal_arrived'))
                return dict(run_id='fake',status='completed',trains=rows,input_sha256='fake',events_file='fake')
            with patch.object(search,'setup'),patch.object(search,'fingerprint',return_value={'hash':'fixed'}),patch.object(search,'simulate',side_effect=simulation),patch('railsim_mcp.search.trace',return_value={}),patch('railsim_mcp.search.analyse',return_value=[]):
                search.run()
            saved=json.loads((Path(d)/'result.json').read_text())
            self.assertEqual(saved['best_volume'],7)
            self.assertEqual(saved['status'],'budget_exhausted')

    def test_subprocess_timeout(self):
        import subprocess,sys
        with self.assertRaises(subprocess.TimeoutExpired):
            bounded_process([sys.executable,'-c','import time;time.sleep(3)'],.05)

    def test_namespaced_fleet_fingerprint_ignores_vehicle_instances(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); workspace=root/'workspaces'/'test';workspace.mkdir(parents=True)
            for f in ('network.xml','config.xml','schedule.xml'): (workspace/f).write_text('<root/>')
            (workspace/'uc1_operational_plan.json').write_text('{}')
            fleet=workspace/'vehicles.xml'
            fleet.write_text('<vehicleDefinitions xmlns="urn:test"><vehicleType id="FV"/><vehicle id="old" type="FV"/></vehicleDefinitions>')
            with patch.object(Search,'revision',return_value='test'):
                s=Search(Settings(root,root),root/'result.json')
            s.state['workspace']='test'
            before=s.fingerprint()
            fleet.write_text('<vehicleDefinitions xmlns="urn:test"><vehicleType id="FV"/><vehicle id="new" type="FV"/><vehicle id="new2" type="FV"/></vehicleDefinitions>')
            self.assertEqual(before,s.fingerprint())
            fleet.write_text('<vehicleDefinitions xmlns="urn:test"><vehicleType id="RV"/></vehicleDefinitions>')
            self.assertNotEqual(before,s.fingerprint())

    def test_entry_diagnostics_include_period_seam(self):
        from railsim_mcp.search import entry_diagnostics
        run=dict(trains=[dict(vehicle_id='FV_0_p1',scheduled_departure=3500),dict(vehicle_id='RV_1_p2',scheduled_departure=3600)])
        trajectories={'FV_0_p1':dict(entries={'f03':3500},leaves={'f03':3550}),'RV_1_p2':dict(entries={'f03':3600})}
        result=entry_diagnostics(run,trajectories)
        self.assertTrue(result['pairs'][0]['crosses_pattern_boundary'])
        self.assertEqual(result['pairs'][0]['front_deficit'],20)
        self.assertFalse(result['acceptance_gate'])

    def test_resume_keeps_limits_rng_and_simulation_count(self):
        with tempfile.TemporaryDirectory() as d:
            settings=Settings(Path(d),Path(d)/'state'); output=Path(d)/'result.json'
            with patch.object(Search,'revision',return_value='test'):
                original=Search(settings,output,simulations=5,seconds=100)
                original.random_pattern(4)
                original.state['simulations']=3
                original.save()
                next_pattern=original.random_pattern(4)
                resumed=Search(settings,output,simulations=100,seconds=1800,resume=True)
            self.assertEqual(resumed.state['simulations'],3)
            self.assertEqual(resumed.state['limits']['simulations'],5)
            self.assertEqual(resumed.random_pattern(4),next_pattern)
            with patch.object(Search,'revision',return_value='changed'):
                with self.assertRaises(ValueError): Search(settings,output,resume=True)

    def test_deadline_rejects_before_simulation(self):
        with tempfile.TemporaryDirectory() as d:
            with patch.object(Search,'revision',return_value='test'):
                search=Search(Settings(Path(d),Path(d)/'state'),Path(d)/'result.json')
            search.state['simulations']=100
            with self.assertRaises(TimeoutError): search.simulate([])
            self.assertEqual(search.state['simulations'],100)

    def make_search(self, d, policy='random', **kwargs):
        with patch.object(Search,'revision',return_value='test'):
            s=Search(Settings(Path(d),Path(d)/'state'),Path(d)/'result.json',policy=policy,**kwargs)
        s.state.update(frozen={'hash':'fixed'},baselines={p:dict(terminal=100,trajectory={}) for p in ('FV','RV','GV')})
        return s

    def fake_run(self, search, fail=True):
        def simulate(pattern):
            search.state['simulations']+=1
            search.volume_state()['simulation_calls']+=1
            rows=[]
            for k in (2,3):
                for i,(p,t) in enumerate(pattern):
                    dep=t+k*1800
                    rows.append(dict(vehicle_id=f'{p}_{i:02}_p{k}',scheduled_departure=dep,planned_arrival=dep+100,actual_arrival=dep+100+int(fail),terminal_delay_seconds=int(fail),status='terminal_arrived'))
            return dict(run_id='fake',status='completed',trains=rows,input_sha256='fake',events_file='fake')
        return simulate

    def exercise(self,s,fail=True):
        with patch('railsim_mcp.search.check_runner',return_value={}),patch.object(s,'setup'),patch.object(s,'fingerprint',return_value={'hash':'fixed'}),patch.object(s,'simulate',side_effect=self.fake_run(s,fail)),patch('railsim_mcp.search.trace',return_value={}),patch('railsim_mcp.search.analyse',return_value=[]):
            s.run()

    def test_volume_budget_stops_at_first_unresolved(self):
        with tempfile.TemporaryDirectory() as d:
            s=self.make_search(d,attempts_per_volume=7)
            self.exercise(s)
            self.assertEqual(s.state['status'],'volume_unresolved')
            self.assertEqual(s.state['simulations'],7)
            self.assertEqual(s.state['active_volume'],4)
            self.assertEqual(s.state['volumes']['4']['status'],'unresolved')

    def test_requested_volumes_are_both_tested_even_when_first_is_unresolved(self):
        with tempfile.TemporaryDirectory() as d:
            s=self.make_search(d,attempts_per_volume=3,target_volumes=[11,12])
            self.exercise(s)
            self.assertEqual(s.state['status'],'targets_completed')
            self.assertEqual(set(s.state['volumes']),{'11','12'})
            self.assertEqual(s.state['simulations'],6)
            self.assertTrue(all(v['status']=='unresolved' for v in s.state['volumes'].values()))

    def test_requested_luna_volumes_start_with_model_and_stop_after_last_pass(self):
        with tempfile.TemporaryDirectory() as d:
            s=self.make_search(d,policy='luna',target_volumes=[11,12])
            with patch.object(s,'luna_action',side_effect=lambda:dict(action='random',volume=s.state['active_volume'],pattern=[],reason='test')) as action:
                self.exercise(s,fail=False)
            self.assertEqual(action.call_count,2)
            self.assertEqual(s.state['simulations'],2)
            self.assertEqual(s.state['best_volume'],12)
            self.assertEqual(s.state['status'],'targets_completed')
            self.assertTrue(all(v['status']=='passed' for v in s.state['volumes'].values()))

    def test_duplicate_proposals_cost_attempts_but_no_simulations(self):
        with tempfile.TemporaryDirectory() as d:
            s=self.make_search(d,attempts_per_volume=4)
            same=[['FV',0],['FV',100],['RV',200],['GV',300]]
            with patch.object(s,'random_pattern',return_value=same): self.exercise(s)
            self.assertEqual(s.state['simulations'],1)
            self.assertEqual(s.state['volumes']['4']['attempts'],4)
            self.assertEqual(len([a for a in s.state['decisions'] if a.get('rejected')]),3)

    def test_each_passing_volume_gets_a_fresh_bucket(self):
        with tempfile.TemporaryDirectory() as d:
            s=self.make_search(d,simulations=4,attempts_per_volume=1)
            self.exercise(s,fail=False)
            self.assertEqual(s.state['best_volume'],7)
            for v in range(4,8):
                self.assertEqual(s.state['volumes'][str(v)]['attempts'],1)
                self.assertEqual(s.state['volumes'][str(v)]['status'],'passed')

    def test_plateau_requires_restart_after_five_nonimprovements(self):
        with tempfile.TemporaryDirectory() as d:
            s=self.make_search(d)
            trial=dict(key='best',score=dict(expected=8,completed=8,errors=[],max_delay=10,total_delay=30,passed=False))
            for _ in range(6): s.record_progress(trial)
            self.assertTrue(s.volume_state()['restart_required'])
            self.assertEqual(s.volume_state()['best_key'],'best')
            improved=copy.deepcopy(trial); improved['key']='better'; improved['score']['max_delay']=5
            s.record_progress(improved)
            self.assertEqual(s.volume_state()['best_key'],'better')

    def test_plateau_executes_random_restart_and_preserves_best(self):
        with tempfile.TemporaryDirectory() as d:
            s=self.make_search(d,attempts_per_volume=8)
            self.exercise(s)
            self.assertEqual(s.volume_state()['restarts'],1)
            self.assertTrue(any(a.get('plateau_restart') for a in s.state['decisions']))
            self.assertIsNotNone(s.volume_state()['best_key'])

    def test_warm_pattern_preserves_mix_and_existing_departures(self):
        with tempfile.TemporaryDirectory() as d:
            s=self.make_search(d)
            previous=[['FV',0],['FV',129],['FV',258],['FV',387],['RV',515],['GV',643],['GV',771],['RV',1137],['RV',1465]]
            s.state['trials']=[dict(volume=9,pattern=previous,score=dict(passed=True))]
            result=s.warm_pattern(10)
            self.assertEqual(len(result),10)
            self.assertTrue(all(row in result for row in previous))
            self.assertEqual({p:sum(a==p for a,_ in result) for p in ('FV','RV','GV')},composition(10))

    def test_tight_reference_seed_is_tried_before_random_fallback(self):
        matrix={'FV':{'FV':81,'RV':95,'GV':1},'RV':{'FV':226,'RV':214,'GV':52},'GV':{'FV':271,'RV':270,'GV':97}}
        with tempfile.TemporaryDirectory() as d:
            s=self.make_search(d)
            with patch.object(s,'reference_evidence',return_value={'same_resource_order_gap_seconds':matrix}),patch.object(s,'random_pattern',side_effect=AssertionError('tight seed discarded')):
                seeds=s.reference_cycle_seeds(13)
                self.assertEqual([row['fits_template'] for row in seeds],[False,False,True])
                self.assertEqual(seeds[-1]['template_cycle_seconds'],1792)
                pattern=s.rule_pattern(13)
            self.assertEqual(pattern,seeds[-1]['pattern'])
            self.assertEqual(len(validate_pattern(pattern,13)),13)

    def test_missing_reference_seed_is_explicit_and_probe_is_not_a_passing_seed(self):
        matrix={'FV':{'FV':81,'RV':95,'GV':1},'RV':{'FV':226,'RV':214,'GV':52},'GV':{'FV':271,'RV':270,'GV':97}}
        with tempfile.TemporaryDirectory() as d:
            s=self.make_search(d)
            with patch.object(s,'reference_evidence',return_value={'same_resource_order_gap_seconds':matrix}),patch.object(s,'random_pattern',side_effect=AssertionError('hidden fallback')):
                seeds=s.reference_cycle_seeds(14)
                self.assertFalse(any(row['fits_template'] for row in seeds))
                self.assertEqual(len(validate_pattern(seeds[-1]['compressed_probe'],14)),14)
                with self.assertRaisesRegex(ValueError,'rules unavailable'): s.rule_pattern(14)

    def conflict_trial(self):
        # FV_01 is held on f05 by GV_02 from the previous period until 7s after it wanted the block.
        return dict(volume=4,key='base',pattern=[['FV',0],['FV',600],['GV',1500],['RV',1700]],
                    score=dict(expected=8,completed=8,errors=[],max_delay=3,total_delay=6,passed=False),
                    conflict_evidence=[dict(vehicle='FV_01_p1',first_late_link=dict(link='f04',excess_seconds=3.0),
                        overlapping_resource_candidates=[dict(other_vehicle='GV_02_p0',link='f05',desired_reservation=2400.0,reserved=2300.0,released=2407.0)])])

    def test_repair_moves_follow_measured_overlap(self):
        moves=Search.repair_moves(self.conflict_trial())
        patterns=[m['pattern'] for m in moves]
        self.assertEqual(patterns[0],[['FV',0],['FV',603],['GV',1500],['RV',1700]])
        self.assertIn([['FV',0],['FV',600],['GV',1493],['RV',1700]],patterns)
        self.assertIn([['FV',0],['FV',604],['GV',1497],['RV',1700]],patterns)
        for p in patterns: self.assertEqual(len(validate_pattern(p,4)),4)

    def test_repair_moves_wrap_at_period_seam_and_ignore_unknown_vehicles(self):
        trial=self.conflict_trial()
        trial['conflict_evidence'][0]['overlapping_resource_candidates'].append(dict(other_vehicle='XX_09_p0',link='f05',desired_reservation=2400.0,released=2408.0))
        trial['pattern'][1]=['FV',1798]
        self.assertEqual(Search.repair_moves(trial)[0]['pattern'][1],['FV',1])

    def test_repair_policy_edits_best_trial_without_a_model(self):
        with tempfile.TemporaryDirectory() as d:
            s=self.make_search(d,policy='repair')
            s.state['trials']=[self.conflict_trial()]
            s.volume_state().update(rules_tried=True)
            action=s.repair_action(4)
            self.assertEqual(action['action'],'submit')
            self.assertEqual([[r['product'],r['departure']] for r in action['pattern']],[['FV',0],['FV',603],['GV',1500],['RV',1700]])
            s.volume_state()['restart_required']=True
            self.assertEqual(s.repair_action(4)['action'],'random')

    def test_repair_policy_runs_with_fallbacks_and_no_model_turns(self):
        with tempfile.TemporaryDirectory() as d:
            s=self.make_search(d,policy='repair',attempts_per_volume=8)
            with patch('railsim_mcp.search.request_action',side_effect=AssertionError('model called')),patch.object(s,'rule_pattern',side_effect=ValueError('rules unavailable')):
                self.exercise(s)
            self.assertEqual(s.state['turns'],0)
            self.assertEqual(s.state['simulations'],8)
            self.assertTrue(any(a.get('plateau_restart') for a in s.state['decisions']))

    def test_conflict_evidence_keeps_worst_finding_per_timetable_train(self):
        from railsim_mcp.search import distinct_conflicts
        row=lambda v,e:dict(vehicle=v,first_late_link=dict(excess_seconds=e))
        evidence=[row(f'FV_00_p{k}',3) for k in range(6)]+[row('RV_03_p2',9),row('GV_05_p1',1),row('RV_03_p3',12)]
        self.assertEqual([(r['vehicle'],r['first_late_link']['excess_seconds']) for r in distinct_conflicts(evidence)],
                         [('RV_03_p3',12),('FV_00_p0',3),('GV_05_p1',1)])

    def test_pick_policy_maps_model_choice_to_code_built_timetable(self):
        with tempfile.TemporaryDirectory() as d:
            s=self.make_search(d,policy='pick')
            s.state['trials']=[self.conflict_trial()]
            seen={}
            def answer(identity,prompt,schema,prefix,timeout,process,validate):
                seen.update(schema=schema,prompt=prompt)
                return validate(dict(choice=1,reason='advance the blocker'),schema),dict(usage=[])
            with patch.object(s,'rule_pattern',side_effect=ValueError('rules unavailable')),patch('railsim_mcp.search.request_action',side_effect=answer):
                action=s.pick_action(4)
            moves=Search.repair_moves(self.conflict_trial())
            self.assertEqual([[r['product'],r['departure']] for r in action['pattern']],validate_pattern(moves[1]['pattern'],4))
            self.assertEqual(seen['schema']['properties']['choice']['enum'][-1],len(moves))  # last option is restart
            self.assertIn('restart from a new random timetable',seen['prompt'])
            self.assertLess(len(seen['prompt']),6000)
            self.assertEqual(s.volume_state()['model_turns'],1)
            bad=lambda identity,prompt,schema,prefix,timeout,process,validate:(validate(dict(choice=99,reason='x'),schema),dict(usage=[]))
            with patch.object(s,'rule_pattern',side_effect=ValueError('rules unavailable')),patch('railsim_mcp.search.request_action',side_effect=bad):
                with self.assertRaises(ValueError): s.pick_action(4)

    def test_pick_policy_restarts_without_model_and_runs_end_to_end(self):
        with tempfile.TemporaryDirectory() as d:
            s=self.make_search(d,policy='pick',attempts_per_volume=8)
            s.volume_state()['restart_required']=True
            with patch('railsim_mcp.search.request_action',side_effect=AssertionError('model called')):
                self.assertEqual(s.pick_action(4)['action'],'random')
        with tempfile.TemporaryDirectory() as d:
            s=self.make_search(d,policy='pick',attempts_per_volume=8)
            with patch('railsim_mcp.search.request_action',side_effect=AssertionError('no options means no model call')),patch.object(s,'rule_pattern',side_effect=ValueError('rules unavailable')):
                self.exercise(s)
            self.assertEqual(s.state['simulations'],8)
            self.assertEqual(s.state['turns'],0)

    def exercise_batch(self,s,fail=True):
        single=self.fake_run(s,fail)
        batches=[]
        def simulate_batch(patterns):
            batches.append(len(patterns))
            out=[]
            for pattern in patterns:
                s.state['simulations']-=1; s.volume_state()['simulation_calls']-=1  # undo per-call charge below
                out.append(single(pattern))
            s.state['simulations']+=len(patterns); s.volume_state()['simulation_calls']+=len(patterns)
            return out
        with patch.object(s,'setup'),patch.object(s,'fingerprint',return_value={'hash':'fixed'}),patch.object(s,'simulate_batch',side_effect=simulate_batch),patch.object(s,'rule_pattern',side_effect=ValueError('rules unavailable')),patch('railsim_mcp.search.trace',return_value={}),patch('railsim_mcp.search.analyse',return_value=[]):
            s.run()
        return batches

    def test_parallel_batches_respect_volume_budget_and_plateau(self):
        with tempfile.TemporaryDirectory() as d:
            s=self.make_search(d,policy='repair',attempts_per_volume=10,workers=4)
            batches=self.exercise_batch(s)
            self.assertEqual(batches,[1,4,4,1])
            self.assertEqual(s.state['simulations'],10)
            self.assertEqual(s.volume_state()['attempts'],10)
            self.assertEqual(len({t['key'] for t in s.state['trials']}),10)
            self.assertEqual(s.state['status'],'volume_unresolved')
            self.assertFalse(s.volume_state()['restart_required'])  # 9 non-improvements < 5 batches

    def test_parallel_pass_advances_once_and_keeps_late_batch_members(self):
        with tempfile.TemporaryDirectory() as d:
            s=self.make_search(d,policy='random',simulations=8,workers=4)
            batches=self.exercise_batch(s,fail=False)
            self.assertEqual(batches,[1]*8)  # a passing warm insert never fans out
            self.assertEqual(s.state['best_volume'],11)
            self.assertEqual(s.state['volumes']['4']['status'],'passed')
        with tempfile.TemporaryDirectory() as d:
            s=self.make_search(d,policy='random',simulations=5,workers=4)
            s.volume_state()['attempts']=1  # past the warm insert: a full batch, all pass
            self.exercise_batch(s,fail=False)
            self.assertEqual([t['volume'] for t in s.state['trials']],[4]*4+[5])
            self.assertEqual(s.state['best_volume'],5)

    def test_parallel_workers_reject_model_policies_and_copy_frozen_workspace(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ValueError): self.make_search(d,policy='pick',workers=2)
            s=self.make_search(d,policy='repair',workers=3)
            root=s.ws.workspace_root/s.state['workspace']; root.mkdir(parents=True); (root/'network.xml').write_text('<n/>')
            slots=s.slot_workspaces(3)
            self.assertEqual(slots[1:],[s.state['workspace']+'-w1',s.state['workspace']+'-w2'])
            self.assertEqual((s.ws.workspace_root/slots[2]/'network.xml').read_text(),'<n/>')

    def test_resume_preserves_volume_accounting(self):
        with tempfile.TemporaryDirectory() as d:
            s=self.make_search(d,attempts_per_volume=9)
            s.volume_state().update(attempts=8,model_turns=7,restart_required=True)
            s.save()
            with patch.object(Search,'revision',return_value='test'):
                resumed=Search(s.settings,s.destination,resume=True,attempts_per_volume=30)
            self.assertEqual(resumed.volume_state()['attempts'],8)
            self.assertEqual(resumed.volume_state()['model_turns'],7)
            self.assertEqual(resumed.state['limits']['attempts_per_volume'],9)
            self.assertTrue(resumed.volume_state()['restart_required'])

    def test_model_turn_limit_is_local_to_volume(self):
        with tempfile.TemporaryDirectory() as d:
            s=self.make_search(d,policy='luna',turns=2)
            s.state['turns']=100
            s.volume_state()['model_turns']=2
            with self.assertRaises(TimeoutError): s.luna_action()
            s.state['active_volume']=5
            self.assertEqual(s.volume_state()['model_turns'],0)
            self.assertEqual(s.state['turns'],100)

    def test_model_cannot_spend_other_volumes_budget(self):
        with tempfile.TemporaryDirectory() as d:
            s=self.make_search(d,policy='luna',attempts_per_volume=3)
            with patch.object(s,'luna_action',return_value=dict(action='random',volume=5,pattern=[],reason='skip')):
                self.exercise(s)
            self.assertEqual(s.state['simulations'],1)
            self.assertEqual(s.state['active_volume'],4)
            self.assertEqual(s.state['status'],'volume_unresolved')
            self.assertTrue(all(a.get('rejected') for a in s.state['decisions'][1:]))

    def test_import_rejects_changed_revision_and_workspace_escape(self):
        with tempfile.TemporaryDirectory() as d:
            s=self.make_search(d)
            source=Path(d)/'old.json'
            source.write_text(json.dumps(dict(project_revision='other',seed=4711)))
            with self.assertRaises(ValueError): s.import_witness(source)
            source.write_text(json.dumps(dict(project_revision='test',seed=4711,workspace='../outside')))
            with self.assertRaises(ValueError): s.import_witness(source)

    def test_legacy_resume_requires_explicit_import(self):
        with tempfile.TemporaryDirectory() as d:
            s=self.make_search(d);s.state['version']=1;s.save()
            with patch.object(Search,'revision',return_value='test'):
                with self.assertRaises(ValueError): Search(s.settings,s.destination,resume=True)
