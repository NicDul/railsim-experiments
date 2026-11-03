import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from railsim_mcp.uc1_modes import UC1Mode, rounded_counts
from railsim_mcp.uc1 import best_cycle, cyclic_pattern, pattern_departures
from railsim_mcp.search import Search, validate_pattern, assess_retained_run
from railsim_mcp.configuration import Settings


def plan():
    return dict(modes=[dict(mix='MAINLINE',patterns=['FV_PASS','FV_STOP']),dict(mix='TRANSIT',patterns=['FV_STOP']),dict(mix='EXPRESS',patterns=['FV_STOP']),dict(mix='METRO',patterns=['RV_STOP'])],
                mixes={'MAINLINE':dict(shares=dict(FV=.4,RV=.4,GV=.2)),'TRANSIT':dict(shares=dict(FV=.5,GV=.5)),
                       'EXPRESS':dict(shares=dict(FV=1)),'METRO':dict(shares=dict(RV=1))},
                patterns={'FV_PASS':dict(shares={'FV':{'LR':1},'RV':{'LMR':1},'GV':{'LR':1}}),
                          'FV_STOP':dict(shares={'FV':{'LMR':1},'RV':{'LMR':1},'GV':{'LR':1}}),
                          'RV_STOP':dict(shares={'RV':{'LMR':1}})},
                flows={'LR':dict(routes={'FV':dict(forward='FV_LR'),'GV':dict(forward='GV_LR')}),
                       'LMR':dict(routes={'FV':dict(forward='FV_LMR'),'RV':dict(forward='RV_LMR')})})


class ModeSearchTests(unittest.TestCase):
    def test_only_the_five_declared_modes_are_selected(self):
        data=plan()
        modes=[UC1Mode.from_plan(data,m['mix']+'/'+p) for m in data['modes'] for p in m['patterns']]
        self.assertEqual(len(modes),5)
        self.assertEqual(modes[1].routes['FV'],'FV_LMR')
        self.assertEqual(modes[4].routes,{'RV':'RV_LMR'})
        with self.assertRaises(ValueError):UC1Mode.from_plan(data,'EXPRESS/FV_PASS')

    def test_rounding_is_deterministic_and_has_no_absent_products(self):
        self.assertEqual(rounded_counts(5,dict(FV=.5,GV=.5)),dict(FV=3,GV=2))
        self.assertEqual(rounded_counts(13,dict(RV=1)),dict(RV=13))
        for n in range(4,101):
            self.assertEqual(sum(rounded_counts(n,dict(FV=.4,RV=.4,GV=.2)).values()),n)
        with self.assertRaises(ValueError):rounded_counts(5,dict(FV=.6,RV=.3))

    def test_single_product_cycle_and_stopping_route_are_supported(self):
        cost,seq=best_cycle(8,{('RV','RV'):214},(8,),('RV',))
        self.assertEqual(cost,1712)
        pattern=cyclic_pattern(seq,{('RV','RV'):214})
        self.assertEqual(len(validate_pattern(pattern,8,{'RV':8})),8)
        departures=pattern_departures([['FV',0]],{'FV':'FV_LMR'})
        self.assertEqual(departures[0]['route_id'],'FV_LMR')
        with self.assertRaises(ValueError):validate_pattern([['FV',i*100] for i in range(8)],8,{'RV':8})

    def test_resume_and_warm_start_cannot_change_mode(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);path=root/'src/main/resources/scenarios/use_case_1/uc1_operational_plan.json'
            path.parent.mkdir(parents=True);path.write_text(json.dumps(plan()))
            settings=Settings(root,root/'state');dest=root/'checkpoint.json'
            with patch.object(Search,'revision',return_value='test'):
                search=Search(settings,dest,mode='METRO/RV_STOP')
                self.assertEqual(search.composition(8),{'RV':8})
                self.assertEqual(search.types,('RV',))
                search.save()
                resumed=Search(settings,dest,resume=True)
                self.assertEqual(resumed.mode.id,'METRO/RV_STOP')
                with self.assertRaises(ValueError):Search(settings,dest,resume=True,mode='MAINLINE/FV_PASS')
                foreign=root/'foreign.json';foreign.write_text(json.dumps({'operating_mode':'MAINLINE/FV_PASS'}))
                with self.assertRaisesRegex(ValueError,'different operating mode'):search.import_witness(foreign)

    def test_assessment_cannot_omit_delayed_fv_on_the_stopping_route(self):
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            (root/'vehicles.xml').write_text('<vehicleDefinitions xmlns="urn:fleet"><vehicle id="a" type="FV"/><vehicle id="b" type="RV"/></vehicleDefinitions>')
            (root/'schedule.xml').write_text('<transitSchedule><transitLine>'+''.join(
                f'<transitRoute id="{route}"><routeProfile><stop refId="R1" arrivalOffset="00:01:40"/></routeProfile><departures><departure vehicleRefId="{v}" departureTime="01:00:00"/></departures></transitRoute>'
                for route,v in [('FV_LMR','a'),('RV_LMR','b')])+'</transitLine></transitSchedule>')
            run=dict(status='completed',snapshot_directory=str(root),trains=[dict(vehicle_id=v,status='terminal_arrived',scheduled_departure=3600,planned_arrival=3700,actual_arrival=3700+delay,terminal_delay_seconds=delay) for v,delay in [('a',1),('b',0)]])
            ws=Mock();ws.inspect_run.return_value=run;ws._seconds.side_effect=lambda value,label:sum(float(x)*factor for x,factor in zip(value.split(':'),(3600,60,1)))
            result=assess_retained_run(ws,'test')
            self.assertEqual(result['expected'],2)
            self.assertFalse(result['passed'])
