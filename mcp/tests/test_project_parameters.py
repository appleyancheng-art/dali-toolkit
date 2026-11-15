"""Project-to-executor integration, including partial execution and conditional undo."""
import copy
import tempfile
import unittest
from unittest.mock import patch
from dali_mcp.parameter_control import LampParameterController
from dali_mcp.projects import digest
from dali_mcp.service import Service
from test_parameter_control import QuantizedParameterGateway, CASES


class ProjectParameterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.g = QuantizedParameterGateway()
        self.g.parameters['min_level']['value']['level_raw'] = 2
        self.g.parameters['max_level']['value']['level_raw'] = 254
        self.original = copy.deepcopy(self.g.parameters)
        self.info = {'ip':'127.0.0.1','dali_mac':'02:00:00:00:00:01','dali_firmware':'test'}
        self.s = Service({'gateway_ip':self.info['ip'], 'expected_dali_mac':self.info['dali_mac'],
                          'expected_firmware':'test'}, self.tmp.name)
        self.c = LampParameterController(self.s)
        self.opener = patch.object(self.s, '_open', side_effect=lambda:(self.g,copy.deepcopy(self.info)))
        self.open = self.opener.start()
        self.addCleanup(self.opener.stop)
        self.doc = {'format':'dali-toolkit-project','schema_version':1,'project_id':'a'*32,'name':'backup',
                    'state':'completed','atomic_snapshot':False,'device_writes':False,'started_at':1,'completed_at':2,
                    'gateway':copy.deepcopy(self.info),'scope':{'channel':2,'addresses':[0],'scenes':[8]},
                    'rows':[{'channel':2,'address':0,'parameters':copy.deepcopy(self.original),
                             'scenes':[{'channel':2,'address':0,'scene':8,'light_type_code':1,'level_raw':254,'kelvin_raw':4000}]}]}
        self.save()

    def save(self):
        self.doc['digest'] = digest(self.doc)
        self.s._save('project',self.doc,self.doc['project_id'])

    def target(self, field, value):
        p = self.doc['rows'][0]['parameters']
        if field.startswith('fade_'):
            f = p['fade_time_rate']['value']; f[field+'_code'] = value
            f['raw'] = f['fade_time_code']*16+f['fade_rate_code']
            f['extended_fade_not_read'] = f['fade_time_code']==0
        else:
            p[field]['value']['kelvin_raw' if field.startswith('configured_') else 'level_raw'] = value
        self.save()

    def prepare(self, fields):
        return self.c.prepare_project(self.doc['project_id'],0,fields)

    def allow(self, p):
        self.s.config.update(enable_experimental_parameter_writes=True,
            allowed_parameter_write_targets={'channel':2,'address':0,
                'values':[{'field':s['field'],'values':s['after']} for s in p['proposal']['steps']]})

    def apply(self,p):
        return self.c.execute(p['plan_id'],p['confirmation'])

    def test_all_eight_fields_apply_and_undo_through_existing_tools(self):
        for f,target,*_ in CASES: self.target(f,target)
        p = self.prepare([c[0] for c in CASES])
        self.assertFalse(self.g.writes)
        self.assertEqual(p['source_project']['digest'],self.doc['digest'])
        with self.assertRaises(PermissionError):self.apply(p)
        self.allow(p)
        self.assertEqual(self.apply(p)['state'],'completed_readback')
        self.assertEqual(len(self.g.writes),8)
        undo = self.c.prepare_restore(p['plan_id'])
        self.assertEqual(undo['source_project'],p['source_project'])
        self.allow(undo)
        self.assertEqual(self.apply(undo)['state'],'completed_readback')
        self.assertEqual(self.g.parameters,self.original)
        self.assertEqual(len(self.g.writes),16)
        self.assertEqual(self.c.prepare_restore(p['plan_id'])['state'],'already_original')

    def test_reorders_crossing_brightness_limits_and_undo(self):
        self.g.parameters['min_level']['value']['level_raw']=200
        original=copy.deepcopy(self.g.parameters)
        self.target('min_level',100);self.target('max_level',150)
        p=self.prepare(['max_level','min_level'])
        self.assertEqual([s['field'] for s in p['proposal']['steps']],['min_level','max_level'])
        self.allow(p);self.assertEqual(self.apply(p)['state'],'completed_readback')
        undo=self.c.prepare_restore(p['plan_id']);self.allow(undo)
        self.assertEqual(self.apply(undo)['state'],'completed_readback')
        self.assertEqual(self.g.parameters,original)

    def test_paired_fields_restore_both_components_and_preserve_unselected(self):
        self.doc['rows'][0]['parameters']['power_on']['value']={'level_raw':2,'kelvin_raw':4000}
        self.save();self.g.parameters['fade_time_rate']['value'].update(raw=0x56,fade_time_code=5,fade_rate_code=6)
        p=self.prepare(['power_on']);self.allow(p)
        self.assertEqual(self.apply(p)['state'],'completed_readback')
        self.assertEqual(self.g.parameters['power_on']['value'],{'level_raw':2,'kelvin_raw':4000})
        self.assertEqual(self.g.parameters['fade_time_rate']['value']['raw'],0x56)

    def test_lost_receipt_stops_after_first_write_and_undo_only_attempted(self):
        self.target('fade_time',5);self.target('fade_rate',6)
        p=self.prepare(['fade_time','fade_rate']);self.allow(p);self.g.fault='lost'
        self.assertEqual(self.apply(p)['state'],'stopped_receipt_unknown')
        self.assertEqual(len(self.g.writes),1)
        self.g.fault=None;undo=self.c.prepare_restore(p['plan_id'])
        self.assertEqual([s['field'] for s in undo['proposal']['steps']],['fade_time'])
        self.allow(undo);self.assertEqual(self.apply(undo)['state'],'completed_readback')
        self.assertEqual(self.g.parameters,self.original)
        self.allow(p)
        with self.assertRaises(ValueError):self.apply(p)

    def test_changed_project_refused_before_connection_or_write(self):
        self.target('fade_time',5);p=self.prepare(['fade_time']);self.allow(p)
        before=self.open.call_count;self.target('fade_time',6)
        with self.assertRaisesRegex(ValueError,'source project changed'):self.apply(p)
        self.assertEqual(self.open.call_count,before);self.assertFalse(self.g.writes)

    def test_rehashed_plan_cannot_substitute_nonproject_targets(self):
        self.target('fade_time',5);p=self.prepare(['fade_time'])
        q=self.c.prepare_parameter(2,0,'fade_time',6)
        q['source_project']=p['source_project'];q.pop('confirmation');q['digest']=self.c._digest(q)
        self.s._save('parameter-plan',q,q['plan_id'])
        with self.assertRaisesRegex(ValueError,'differ from captured'):self.c.get(q['plan_id'])
        self.assertFalse(self.g.writes)

    def test_bad_scope_and_unusable_captured_special_values_before_network(self):
        for address,fields in [(True,['fade_time']),(1,['fade_time']),(0,[]),(0,['groups']),
                               (0,['fade_time','fade_time']),(0,[True])]:
            with self.assertRaises(ValueError):self.c.prepare_project(self.doc['project_id'],address,fields)
        self.target('fade_time',0)
        with self.assertRaises(ValueError):self.prepare(['fade_time'])
        self.target('power_on',255)
        with self.assertRaises(ValueError):self.prepare(['power_on'])
        self.open.assert_not_called()

    def test_configured_and_connected_identity_rejected(self):
        self.doc['gateway']['ip']='127.0.0.2';self.save()
        with self.assertRaisesRegex(ValueError,'configured gateway'):self.prepare(['fade_time'])
        self.open.assert_not_called()
        self.doc['gateway']['ip']='127.0.0.1';self.save();self.info['dali_firmware']='other'
        with self.assertRaisesRegex(ValueError,'connected gateway'):self.prepare(['fade_time'])
        self.assertEqual(self.g.closed,1);self.assertFalse(self.g.writes)

    def test_old_project_does_not_authorize_ct_expansion(self):
        self.g.parameters['configured_max_ct']['value']['kelvin_raw']=5000
        with self.assertRaisesRegex(ValueError,'physical-limit'):self.prepare(['configured_max_ct'])
        self.assertFalse(self.g.writes)

    def test_unchanged_project_sends_no_commands(self):
        p=self.prepare(['fade_time','fade_rate','power_on']);self.allow(p)
        self.assertEqual(self.apply(p)['state'],'completed_readback')
        self.assertFalse(self.g.writes)

    def test_fresh_configuration_conflict_prevents_all_writes(self):
        self.target('fade_time',5);p=self.prepare(['fade_time']);self.allow(p)
        self.g.parameters['groups']['value']={'group_mask':1,'groups':[0]}
        self.assertEqual(self.apply(p)['state'],'preflight_failed')
        self.assertFalse(self.g.writes)

    def test_partial_undo_refuses_third_value(self):
        self.target('fade_time',5);self.target('fade_rate',6)
        p=self.prepare(['fade_time','fade_rate']);self.allow(p);self.apply(p)
        self.g.parameters['fade_time_rate']['value'].update(raw=0x66,fade_time_code=6)
        with self.assertRaisesRegex(ValueError,'neither original'):self.c.prepare_restore(p['plan_id'])
        self.assertEqual(len(self.g.writes),2)


if __name__=='__main__':unittest.main()
