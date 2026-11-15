import copy,json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from dali_mcp.service import Service
from dali_mcp.parameter_control import LampParameterController
from dali_mcp.native_workbook_store import NativeWorkbookStore
from dali_mcp.native_workbook_parameters import NativeWorkbookParameters,FIELDS
from test_parameter_control import QuantizedParameterGateway
from test_native_workbook import document,package


class NativeWorkbookParameterTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.g=QuantizedParameterGateway()
        self.g.parameters['min_level']['value']['level_raw']=2
        self.g.parameters['max_level']['value']['level_raw']=254
        self.original=copy.deepcopy(self.g.parameters)
        self.info=dict(ip='127.0.0.1',dali_mac='02:00:00:00:00:01',dali_firmware='test')
        self.s=Service(dict(gateway_ip=self.info['ip'],expected_dali_mac=self.info['dali_mac'],expected_firmware='test'),Path(self.tmp.name)/'state')
        self.c=LampParameterController(self.s);self.native=NativeWorkbookParameters(self.s)
        patcher=patch.object(self.s,'_open',side_effect=lambda:(self.g,copy.deepcopy(self.info)))
        self.open=patcher.start();self.addCleanup(patcher.stop)
        self.path=Path(self.tmp.name)/'native.xlsx';self.id=self.imported(document())
    def imported(self,d):
        self.path.write_bytes(package(d));return NativeWorkbookStore(self.s).import_file(str(self.path))['workbook_id']
    def plan(self,fields=None,**kw):return self.native.prepare(self.id,2,0,fields or ['fade_time','fade_rate'],**kw)
    def allow(self,p):
        self.s.config.update(enable_experimental_parameter_writes=True,allowed_parameter_write_targets=dict(channel=2,address=0,
            values=[dict(field=step['field'],values=step['after']) for step in p['proposal']['steps']]))
    def execute(self,p):return self.c.execute(p['plan_id'],p['confirmation'])

    def test_all_eight_native_fields_apply_and_conditional_undo(self):
        d=document();d['通道二参数'].update(D2=5,E2=6,F2=20,G2=97,H2=20,I2=4000,J2=20,K2=4000,M2=6000,N2=3000)
        self.id=self.imported(d);p=self.plan(list(FIELDS));self.assertEqual(len(p['changes']),9)
        self.assertFalse(self.g.writes)
        with self.assertRaises(PermissionError):self.execute(p)
        self.allow(p);self.assertEqual(self.execute(p)['state'],'completed_readback');self.assertEqual(len(self.g.writes),8)
        undo=self.c.prepare_restore(p['plan_id']);self.assertEqual(undo['source_workbook'],p['source_workbook'])
        self.allow(undo);self.assertEqual(self.execute(undo)['state'],'completed_readback')
        self.assertEqual(self.g.parameters,self.original);self.assertEqual(len(self.g.writes),16)

    def test_blank_fields_skip_and_blank_pair_component_keeps_current(self):
        d=document();d['通道二参数'].update(D2='',H2='',I2=4000)
        self.id=self.imported(d);p=self.plan(['fade_time','power_on'])
        self.assertEqual(p['changes'],[dict(field='power_on',values={'kelvin_raw':4000})])
        self.assertEqual(p['native_workbook_review']['kept_blank_components'],['fade_time','power_on_level'])
        self.assertEqual(p['proposal']['steps'][0]['after'],dict(level_raw=0,kelvin_raw=4000))
        self.allow(p);self.assertEqual(self.execute(p)['state'],'completed_readback')
        undo=self.c.prepare_restore(p['plan_id']);self.allow(undo);self.assertEqual(self.execute(undo)['state'],'completed_readback')
        self.assertEqual(self.g.parameters,self.original)

    def test_all_selected_blank_returns_no_plan_without_network(self):
        d=document();d['通道二参数'].update(D2='',H2='',I2='');self.id=self.imported(d)
        result=self.plan(['fade_time','power_on'])
        self.assertEqual(result['state'],'no_targets_in_source');self.assertNotIn('plan_id',result)
        self.assertFalse(result['network_traffic']);self.open.assert_not_called()

    def test_ambiguous_brightness_explicit_policy_and_unsupported_values(self):
        with self.assertRaisesRegex(ValueError,'ambiguous'):self.plan(['min_level'])
        self.open.assert_not_called()
        changes,_,review=self.native.source(self.id,2,0,['min_level'],'native_nearest')
        self.assertEqual(changes,[dict(field='min_level',values={'level_raw':1})])
        self.assertEqual(review['percent_review'][0]['exact_export_candidates'],list(range(1,16)))
        for cell,value,field in [('D2',0,'fade_time'),('H2','MASK','power_on')]:
            d=document();d['通道二参数'][cell]=value;self.id=self.imported(d)
            with self.assertRaises(ValueError):self.plan([field])
        self.open.assert_not_called()

    def test_old_file_cannot_expand_current_ct_range(self):
        self.g.parameters['configured_max_ct']['value']['kelvin_raw']=6000
        with self.assertRaises(ValueError):self.plan(['configured_max_ct'])
        self.assertEqual(self.g.writes,[])

    def test_native_curve_labels_roundtrip_and_blank_keep(self):
        d=document();d['通道二参数']['L2']='线性';self.id=self.imported(d)
        p=self.plan(['dimming_curve']);self.assertEqual(p['changes'],[dict(field='dimming_curve',values={'raw':1})])
        self.allow(p);self.assertEqual(self.execute(p)['state'],'completed_readback')
        u=self.c.prepare_restore(p['plan_id']);self.allow(u);self.execute(u)
        self.assertEqual(self.g.parameters,self.original)
        d['通道二参数']['L2']='';self.id=self.imported(d);count=self.open.call_count
        self.assertEqual(self.plan(['dimming_curve'])['state'],'no_targets_in_source')
        self.assertEqual(self.open.call_count,count)

    def test_source_or_plan_target_tampering_and_identity_change_rejected(self):
        d=document();d['通道二参数']['D2']=5;self.id=self.imported(d);p=self.plan(['fade_time'])
        other=self.c.prepare_parameter(2,0,'fade_time',6)
        other['source_workbook']=p['source_workbook'];other['native_workbook_review']=p['native_workbook_review']
        other.pop('confirmation');other['digest']=self.c._digest(other);self.s._save('parameter-plan',other,other['plan_id'])
        with self.assertRaisesRegex(ValueError,'differ'):self.c.get(other['plan_id'])
        before=self.open.call_count;self.allow(p);self.s.config['gateway_ip']='127.0.0.2'
        with self.assertRaisesRegex(ValueError,'gateway changed'):self.execute(p)
        self.s.config['gateway_ip']='127.0.0.1'
        path=self.s.state_dir/f'native-workbook-{self.id}.json';record=json.loads(path.read_text(encoding='utf-8'))
        record['summary']['source_sha256']='0'*64;path.write_text(json.dumps(record),encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'checksum'):self.execute(p)
        self.assertEqual(self.open.call_count,before);self.assertEqual(self.g.writes,[])

    def test_unknown_receipt_stops_then_undo_only_attempted_fields(self):
        d=document();d['通道二参数'].update(D2=5,E2=6);self.id=self.imported(d);p=self.plan();self.allow(p)
        self.g.fault='lost';self.assertEqual(self.execute(p)['state'],'stopped_receipt_unknown')
        self.assertEqual(len(self.g.writes),1);self.g.fault=None
        undo=self.c.prepare_restore(p['plan_id']);self.assertEqual(len(undo['changes']),1)
        self.allow(undo);self.assertEqual(self.execute(undo)['state'],'completed_readback')
        self.assertEqual(self.g.parameters,self.original)

    def test_invalid_field_scope_and_missing_fixture_fail_without_network(self):
        for channel,address,fields in [(True,0,['fade_time']),(2,True,['fade_time']),(2,1,['fade_time']),
                (2,0,[]),(2,0,['curve']),(2,0,['fade_time','fade_time'])]:
            with self.assertRaises(ValueError):self.native.prepare(self.id,channel,address,fields)
        self.open.assert_not_called()

if __name__=='__main__':unittest.main()
