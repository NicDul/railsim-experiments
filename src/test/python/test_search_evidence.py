import gzip
from pathlib import Path
import tempfile
import unittest

from railsim_mcp.search_evidence import holds, measured_context


class SearchEvidenceTests(unittest.TestCase):
    def stream(self, root, lines):
        path = root/'events.xml.gz'
        with gzip.open(path,'wt') as stream:
            stream.write('<events>'+''.join('<event '+line+'/>' for line in lines)+'</events>')
        return path

    def test_shared_resource_union_ignores_dla_and_partial_release_notifications(self):
        with tempfile.TemporaryDirectory() as d:
            path=self.stream(Path(d),[
                'type="railsimLinkStateChangeEvent" time="1" link="f03" vehicle="FV_0" state="EXHAUSTED" dla="true"',
                'type="railsimLinkStateChangeEvent" time="10" link="f03" vehicle="FV_0" state="EXHAUSTED"',
                'type="railsimLinkStateChangeEvent" time="10" link="f04" vehicle="FV_0" state="EXHAUSTED"',
                'type="railsimLinkStateChangeEvent" time="15" link="f03" vehicle="FV_0" state="EXHAUSTED"',
                'type="railsimLinkStateChangeEvent" time="20" link="f03" vehicle="FV_0" state="EMPTY"',
                'type="railsimLinkStateChangeEvent" time="20" link="f04" vehicle="FV_0" state="EMPTY"'])
            self.assertEqual(holds(path,dict(f03='switch',f04='switch')),
                             [dict(resource='switch',vehicle='FV_0',start=10,end=20)])

    def test_measured_train_and_previous_period_overlap_are_not_crowded_out(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            (root/'network.xml').write_text('<network><links><link id="f02"><attributes><attribute name="railsimTrainCapacity">1</attribute><attribute name="railsimResourceId">block_1</attribute></attributes></link></links></network>')
            path=self.stream(root,[
                'type="railsimLinkStateChangeEvent" time="3590" link="f02" vehicle="GV_09_p1" state="EXHAUSTED"',
                'type="railsimLinkStateChangeEvent" time="3610" link="f02" vehicle="GV_09_p1" state="EMPTY"',
                'type="railsimLinkStateChangeEvent" time="3610" link="f02" vehicle="RV_00_p2" state="EXHAUSTED"',
                'type="railsimLinkStateChangeEvent" time="3630" link="f02" vehicle="RV_00_p2" state="EMPTY"'])
            trains=[dict(vehicle_id=f'FV_{i:02}_p0',scheduled_departure=i,status='terminal_arrived',terminal_delay_seconds=5) for i in range(10)]
            trains.append(dict(vehicle_id='RV_00_p2',scheduled_departure=3600,status='terminal_arrived',terminal_delay_seconds=8))
            refs=dict(resources={'block_1':['f02']},exclusive_reference_offsets={'RV':{'block_1':dict(granted=5,released=25)}},entry_offsets={'RV':{'f02':10}})
            result=measured_context(dict(snapshot_directory=str(root),events_file=str(path),trains=trains),
                                    {'RV_00_p2':dict(entries={'f02':3620})},refs)
            self.assertEqual(len(result['train_rows']),1)
            row=result['train_rows'][0]
            self.assertEqual(row['exit_delay'],8)
            self.assertEqual(row['first_late_links'],[dict(link='f02',delay=10)])
            overlap=row['reservation_overlap_candidates'][0]
            self.assertEqual(overlap['holder'],'GV_09_p1')
            self.assertTrue(overlap['crosses_period'])
            self.assertEqual(overlap['overlap_seconds'],5)
            self.assertEqual(result['resource_usage'][0]['exclusive_busy'],30)

    def test_unfinished_holder_at_cutoff_is_censored_not_a_crash_or_fake_release(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            (root/'network.xml').write_text('<network><links><link id="f02"><attributes><attribute name="railsimTrainCapacity">1</attribute><attribute name="railsimResourceId">block_1</attribute></attributes></link></links></network>')
            path=self.stream(root,['type="railsimLinkStateChangeEvent" time="3500" link="f02" vehicle="GV_09_p1" state="EXHAUSTED"'])
            run=dict(snapshot_directory=str(root),events_file=str(path),horizon_seconds=12600,
                     trains=[dict(vehicle_id='FV_00_p2',scheduled_departure=3600,status='unfinished',terminal_delay_seconds=None)])
            refs=dict(resources={'block_1':['f02']},exclusive_reference_offsets={'FV':{'block_1':dict(granted=5,released=25)}})
            result=measured_context(run,{},refs)
            overlap=result['train_rows'][0]['reservation_overlap_candidates'][0]
            self.assertIsNone(overlap['holder_releases'])
            self.assertTrue(overlap['release_unknown_at_horizon'])
            self.assertEqual(overlap['holder_observed_until'],9000)
            self.assertEqual(result['resource_usage'][0]['exclusive_busy'],1800)
            self.assertIsNone(result['train_rows'][0]['exit_delay'])
            self.assertEqual(result['period_evolution'][2]['completed'],0)
