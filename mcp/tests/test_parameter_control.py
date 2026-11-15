"""Offline facade and real stdio transport tests; never connect to hardware."""
import asyncio
import copy
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from mcp import Client
from mcp.client.stdio import StdioServerParameters
from dali_mcp.fade_control import FadeTimeController
from dali_mcp.parameter_control import LampParameterController
from dali_mcp.parameter_execution import ExperimentalParameterController
from dali_mcp.service import Service
from test_parameter_execution import LabGateway

CASES = [('fade_time', 5, 4, 0xF018, '0201002e05000000'),
         ('fade_rate', 6, 7, 0xF019, '0201002f06000000'),
         ('min_level', 3, 2, 0xF01C, '0201002b03000000'),
         ('max_level', 253, 254, 0xF01E, '0201002afd000000'),
         ('configured_min_ct', 3000, 2702, 0xF028, '020100f2b80b0000'),
         ('configured_max_ct', 6000, 6535, 0xF026, '020100f270170000'),
         ('power_on', 2, 0, 0xF020, '0201000102ffff00'),
         ('system_failure', 195, 229, 0xF022, '02010001c3ffff00')]


class QuantizedParameterGateway(LabGateway):
    def exchange(self, command, payload, route, accept):
        result=super().exchange(command,payload,route,accept)
        if command in (0xf026,0xf028) and self.fault!='ignore':
            field='configured_max_ct' if command==0xf026 else 'configured_min_ct'
            requested=int.from_bytes(payload[4:6],'little')
            self.parameters[field]['value']['kelvin_raw']=1000000//(1000000//requested)
        if command in (0xf020,0xf022) and self.fault!='ignore':
            requested=int.from_bytes(payload[5:7],'little')
            field='power_on' if command==0xf020 else 'system_failure'
            if requested!=65535:
                self.parameters[field]['value']['kelvin_raw']=1000000//(1000000//requested)
        return result


class ParameterControlTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.g = QuantizedParameterGateway()
        self.g.parameters['min_level']['value']['level_raw'] = 2
        self.g.parameters['max_level']['value']['level_raw'] = 254
        self.original = copy.deepcopy(self.g.parameters)
        self.info = {'ip': '127.0.0.1', 'dali_mac': '02:00:00:00:00:01', 'dali_firmware': 'test'}
        self.service = Service({'gateway_ip': self.info['ip'], 'expected_dali_mac': self.info['dali_mac'],
                                'expected_firmware': 'test'}, self.tmp.name)
        self.controller = LampParameterController(self.service)
        self.open_patch = patch.object(self.service, '_open', side_effect=lambda: (self.g, copy.deepcopy(self.info)))
        self.open_mock = self.open_patch.start()

    def tearDown(self):
        self.open_patch.stop()
        self.tmp.cleanup()

    def plan(self, field='fade_rate', value=6):
        return self.controller.prepare_parameter(2, 0, field, value)

    def allow(self, plan):
        self.service.config.update(enable_experimental_parameter_writes=True,
            allowed_parameter_write_targets={'channel': 2, 'address': 0,
                'values': [{'field':s['field'],'values':copy.deepcopy(s['after'])} for s in plan['proposal']['steps']]})

    def apply(self, plan):
        return self.controller.execute(plan['plan_id'], plan['confirmation'])

    def test_all_eight_wire_paths_restore_every_parameter_and_output(self):
        for field, target, original, command, payload in CASES:
            with self.subTest(field=field):
                p = self.plan(field, target)
                self.assertFalse(self.g.writes)
                self.allow(p)
                self.assertEqual(self.apply(p)['state'], 'completed_readback')
                self.assertEqual(self.g.writes, [(command, bytes.fromhex(payload))])
                restore = self.controller.prepare_restore(p['plan_id'])
                self.assertEqual(len(self.g.writes), 1)
                with self.assertRaises(PermissionError): self.apply(restore)
                self.allow(restore)
                self.assertEqual(self.apply(restore)['state'], 'completed_readback')
                self.assertEqual(self.g.parameters, self.original)
                self.assertEqual(self.g.live, {'light_type_code': 1, 'level_raw': 0, 'kelvin_raw': 4000})
                self.assertEqual(len(self.g.writes), 2)
                raw=self.g.writes[-1][1]
                self.assertEqual(int.from_bytes(raw[4:6],'little') if field.startswith('configured_') else raw[4], original)
                self.assertEqual(self.controller.prepare_restore(p['plan_id'])['state'], 'already_original')
                self.g.writes.clear()

    def test_bad_scope_and_values_rejected_before_network(self):
        for field, value in [('power_on', 255), ('system_failure', -1), ('configured_min_ct', 999),
                              ('configured_max_ct', 10001), (None, 6), ([], 6), ('fade_rate', 0),
                              ('fade_rate', 16), ('fade_time', 0), ('min_level', 0), ('max_level', 255),
                              ('fade_rate', True), ('min_level', '3'), ('max_level', 253.0)]:
            with self.subTest(field=field, value=value), self.assertRaises(ValueError): self.plan(field, value)
        for channel, address in [(True, 0), (2, False), (0, 0), (2, 64)]:
            with self.assertRaises(ValueError): self.controller.prepare_parameter(channel, address, 'fade_rate', 6)
        self.open_mock.assert_not_called()

    def test_curve_zero_receipt_roundtrip_and_capability_invalidation(self):
        p=self.plan('dimming_curve',1);self.allow(p);r=self.apply(p)
        self.assertEqual(r['state'],'completed_readback')
        self.assertEqual(self.g.writes,[(0xf024,bytes.fromhex('020100e301000000'))])
        self.assertEqual(r['steps'][0]['receipt']['status_zero_means'],'save_success')
        self.assertTrue(r['capability_invalidation_id'])
        self.assertEqual(self.g.parameters['dimming_curve']['value'],{'raw':1})
        u=self.controller.prepare_restore(p['plan_id']);self.allow(u)
        self.assertEqual(self.apply(u)['state'],'completed_readback')
        self.assertEqual(self.g.parameters,self.original)
        self.assertEqual(self.g.writes[-1],(0xf024,bytes.fromhex('020100e300000000')))

    def test_curve_invalid_or_unknown_original_refused(self):
        for value in (-1,2,255,True,'1',1.0):
            with self.assertRaises(ValueError):self.plan('dimming_curve',value)
        self.open_mock.assert_not_called()
        self.g.parameters['dimming_curve']['value']['raw']=2
        with self.assertRaises(ValueError):self.plan('dimming_curve',0)
        self.assertFalse(self.g.writes)

    def test_curve_lost_receipt_readback_and_conditional_restore(self):
        p=self.plan('dimming_curve',1);self.allow(p);self.g.fault='lost'
        r=self.apply(p);self.assertEqual(r['state'],'stopped_receipt_unknown')
        self.assertEqual(len(self.g.writes),1);self.g.fault=None
        u=self.controller.prepare_restore(p['plan_id']);self.allow(u);self.apply(u)
        self.assertEqual(self.g.parameters,self.original)

    def test_curve_nonzero_receipt_is_failure_even_with_matching_readback(self):
        from dali_mcp.parameter_execution import send_candidate_once
        from types import SimpleNamespace
        p=self.plan('dimming_curve',1)
        reply=SimpleNamespace(command=0xf0a4,payload=bytes.fromhex('02010001ffffffff'))
        with patch.object(self.g,'exchange',return_value=reply):
            with self.assertRaisesRegex(RuntimeError,'curve save reported failure'):
                send_candidate_once(self.g,p['proposal']['steps'][0])

    def test_unsupported_original_rate_rejected_without_saving_executable_plan(self):
        self.g.parameters['fade_time_rate']['value'].update(raw=0x40, fade_rate_code=0)
        with self.assertRaises(ValueError): self.plan()
        self.assertEqual(list(Path(self.tmp.name).glob('parameter-plan-*.json')), [])
        self.assertFalse(self.g.writes)

    def test_min_max_order_rejected_without_writes(self):
        self.g.parameters['max_level']['value']['level_raw'] = 10
        with self.assertRaisesRegex(ValueError, 'conflict'): self.plan('min_level', 11)
        with self.assertRaisesRegex(ValueError, 'conflict'): self.plan('max_level', 1)
        self.assertFalse(self.g.writes)

    def test_default_gate_exact_address_and_value_required(self):
        p = self.plan('min_level', 3)
        with self.assertRaises(PermissionError): self.apply(p)
        self.allow(p)
        self.service.config['allowed_parameter_write_targets']['address'] = 1
        with self.assertRaises(PermissionError): self.apply(p)
        self.service.config['allowed_parameter_write_targets']['address'] = 0
        self.service.config['allowed_parameter_write_targets']['values'][0]['values']['level_raw'] = 4
        with self.assertRaises(PermissionError): self.apply(p)
        self.assertEqual(self.open_mock.call_count, 1)
        self.assertFalse(self.g.writes)

    def test_generic_unsupported_or_multi_field_plans_cannot_bypass_facade(self):
        generic = ExperimentalParameterController(self.service)
        for changes in [[{'field': 'power_on', 'values': {'level_raw': 255}}],
                        [{'field': 'fade_rate', 'values': {'code': 0}}],
                        [{'field': 'min_level', 'values': {'level_raw': 3}},
                         {'field': 'max_level', 'values': {'level_raw': 253}}]]:
            p = generic.prepare(2, 0, changes)
            self.allow(p)
            before = self.open_mock.call_count
            with self.assertRaises(ValueError): self.apply(p)
            with self.assertRaises(ValueError): self.controller.prepare_restore(p['plan_id'])
            self.assertEqual(self.open_mock.call_count, before)
        self.assertEqual(len(self.controller.get()['excluded_records']), 3)
        self.assertFalse(self.g.writes)

    def test_old_fade_facade_stays_time_only_and_shared_getter_preserves_claim(self):
        p = self.plan()
        fades = FadeTimeController(self.service)
        with self.assertRaisesRegex(ValueError, 'other parameter'): fades.get(p['plan_id'])
        self.allow(p)
        self.assertEqual(self.apply(p)['state'], 'completed_readback')
        record = self.controller.get(p['plan_id'])
        self.assertTrue(record['claimed'])
        self.assertNotIn('confirmation', record)
        with self.assertRaises(ValueError): self.apply(p)
        self.assertEqual(len(self.g.writes), 1)

    def test_lost_receipt_keeps_uncertainty_and_never_resends(self):
        p = self.plan('max_level', 253); self.allow(p); self.g.fault = 'lost'
        result = self.apply(p)
        self.assertEqual(result['state'], 'stopped_receipt_unknown')
        self.assertEqual(result['steps'][0]['state'], 'verified_receipt_unknown')
        self.assertEqual(len(self.g.writes), 1)
        with self.assertRaises(ValueError): self.apply(p)

    def test_limit_changed_live_output_stops_and_restore_does_not_claim_light_restore(self):
        p = self.plan('min_level', 3); self.allow(p); self.g.fault = 'live'
        result = self.apply(p)
        self.assertEqual(result['state'], 'stopped_live_output_changed')
        self.assertFalse(result['live_control_commands_sent'])
        self.g.fault = None
        restore = self.controller.prepare_restore(p['plan_id']); self.allow(restore)
        self.assertEqual(self.apply(restore)['state'], 'completed_readback')
        self.assertEqual(self.g.parameters, self.original)
        self.assertEqual(self.g.live['level_raw'], 1)  # Parameter undo does not silently control lights.

    def test_parameter_mismatch_is_failure_not_receipt_success(self):
        p = self.plan(); self.allow(p); self.g.fault = 'ignore'
        result = self.apply(p)
        self.assertEqual(result['state'], 'stopped_verification_failed')
        self.assertEqual(self.controller.prepare_restore(p['plan_id'])['state'], 'already_original')
        self.assertEqual(len(self.g.writes), 1)

    def test_live_change_before_apply_prevents_parameter_write(self):
        p = self.plan(); self.allow(p); self.g.live['level_raw'] = 20
        self.assertEqual(self.apply(p)['state'], 'preflight_failed')
        self.assertFalse(self.g.writes)

    def test_restore_refuses_third_value_and_unrelated_group_change(self):
        p = self.plan('max_level', 253); self.allow(p); self.apply(p)
        self.g.parameters['max_level']['value']['level_raw'] = 252
        with self.assertRaisesRegex(ValueError, 'neither original'): self.controller.prepare_restore(p['plan_id'])
        self.g.parameters['max_level']['value']['level_raw'] = 253
        self.g.parameters['groups']['value'] = {'groups': [0], 'group_mask': 1}
        with self.assertRaisesRegex(ValueError, 'other configuration'): self.controller.prepare_restore(p['plan_id'])
        self.assertEqual(len(self.g.writes), 1)

    def test_parameter_writes_invalidate_only_target_address_capabilities(self):
        for prefix in ('profile', 'fade', 'level-limits'):
            self.service._save(prefix, {'gateway': self.info, 'captured_at': time.time() - 1,
                'rows': [{'channel': 2, 'address': a} for a in (0, 1)]})
            self.assertEqual(set(self.service._cached_profiles(2, prefix)), {0, 1})
        p = self.plan('min_level', 3); self.allow(p); self.apply(p)
        for prefix in ('profile', 'fade', 'level-limits'):
            self.assertEqual(set(self.service._cached_profiles(2, prefix)), {1})

    def test_unchanged_plan_sends_nothing_and_preserves_capability_cache(self):
        p = self.plan('fade_rate', 7); self.allow(p)
        self.assertEqual(self.apply(p)['state'], 'completed_readback')
        self.assertFalse(self.g.writes)
        self.assertEqual(list(Path(self.tmp.name).glob('capability-invalidation-*.json')), [])

    def test_ct_narrowing_quantized_readback_and_evidence_bound_restore(self):
        with self.assertRaisesRegex(ValueError,'physical-limit'):self.plan('configured_min_ct',2600)
        with self.assertRaisesRegex(ValueError,'physical-limit'):self.plan('configured_max_ct',7000)
        p=self.plan('configured_max_ct',6000);self.allow(p)
        r=self.apply(p)
        self.assertEqual(r['state'],'completed_readback')
        self.assertEqual(r['steps'][0]['readback']['parameters']['configured_max_ct']['value']['kelvin_raw'],6024)
        with self.assertRaisesRegex(ValueError,'physical-limit'):self.plan('configured_max_ct',6535)
        undo=self.controller.prepare_restore(p['plan_id']);self.allow(undo)
        self.assertEqual(self.apply(undo)['state'],'completed_readback')
        self.assertEqual(self.g.parameters,self.original)

    def test_paired_brightness_preserves_nonmask_color_and_exact_gate(self):
        for field in ('power_on','system_failure'):
            self.g.parameters[field]['value']['kelvin_raw']=3003
            p=self.plan(field,2)
            self.assertEqual(p['proposal']['steps'][0]['after'],{'level_raw':2,'kelvin_raw':3003})
            self.service.config.update(enable_experimental_parameter_writes=True,
                allowed_parameter_write_targets={'channel':2,'address':0,'values':p['changes']})
            with self.assertRaises(PermissionError):self.apply(p)
            self.allow(p);self.assertEqual(self.apply(p)['state'],'completed_readback')
            undo=self.controller.prepare_restore(p['plan_id']);self.allow(undo)
            self.assertEqual(self.apply(undo)['state'],'completed_readback')
            self.assertEqual(self.g.parameters[field]['value']['kelvin_raw'],3003)

    def test_paired_unsupported_original_level_cannot_be_made_unrestorable(self):
        self.g.parameters['power_on']['value']['level_raw']=255
        with self.assertRaises(ValueError):self.plan('power_on',2)
        self.assertFalse(self.g.writes)
        self.assertFalse(list(Path(self.tmp.name).glob('parameter-plan-*.json')))

    def test_paired_color_conflict_blocks_apply_and_restore(self):
        p=self.plan('power_on',2);self.allow(p)
        self.g.parameters['power_on']['value']['kelvin_raw']=3003
        self.assertEqual(self.apply(p)['state'],'preflight_failed')
        self.assertFalse(self.g.writes)
        p=self.plan('power_on',2);self.allow(p);self.apply(p)
        self.g.parameters['power_on']['value']['kelvin_raw']=4000
        with self.assertRaisesRegex(ValueError,'neither original'):self.controller.prepare_restore(p['plan_id'])

    def test_ct_lost_terminal_receipt_keeps_applied_value_uncertain_no_replay(self):
        p=self.plan('configured_min_ct',3003);self.allow(p);self.g.fault='lost'
        r=self.apply(p)
        self.assertEqual(r['state'],'stopped_receipt_unknown')
        self.assertEqual(len(self.g.writes),1)
        self.g.fault=None
        undo=self.controller.prepare_restore(p['plan_id']);self.allow(undo)
        self.assertEqual(self.apply(undo)['state'],'completed_readback')
        self.assertEqual(self.g.parameters,self.original)

    def test_ct_restore_requires_original_ancestry_not_forged_expansion(self):
        p=self.plan('configured_min_ct',3000);self.allow(p);self.apply(p)
        undo=self.controller.prepare_restore(p['plan_id'])
        forged=copy.deepcopy(undo);forged.pop('confirmation');forged['restore_of']=None
        forged['digest']=self.controller._digest(forged)
        self.service._save('parameter-plan',forged,forged['plan_id'])
        before=self.open_mock.call_count
        with self.assertRaises(ValueError):self.controller._load(forged['plan_id'])
        self.assertEqual(self.open_mock.call_count,before)

    def test_paired_color_only_wire_readback_and_exact_restore(self):
        for field, command, level in [('power_on',0xf020,0),('system_failure',0xf022,229)]:
            with self.subTest(field=field):
                p=self.controller.prepare_power_parameter(2,0,field,kelvin_raw=4000)
                self.assertEqual(p['changes'],[{'field':field,'values':{'kelvin_raw':4000}}])
                self.allow(p)
                self.assertEqual(self.apply(p)['state'],'completed_readback')
                self.assertEqual(self.g.writes,[(command,bytes([2,1,0,1,level,0xa0,0x0f,0]))])
                undo=self.controller.prepare_restore(p['plan_id']);self.allow(undo)
                self.assertEqual(self.apply(undo)['state'],'completed_readback')
                self.assertEqual(self.g.parameters,self.original)
                self.assertEqual(self.g.live,{'light_type_code':1,'level_raw':0,'kelvin_raw':4000})
                self.g.writes.clear()

    def test_paired_both_components_quantize_and_recover_mask(self):
        for field in ('power_on','system_failure'):
            p=self.controller.prepare_power_parameter(2,0,field,level_raw=195,kelvin_raw=3000)
            self.allow(p);r=self.apply(p)
            self.assertEqual(r['state'],'completed_readback')
            self.assertEqual(r['steps'][0]['candidate']['after'],{'level_raw':195,'kelvin_raw':3000})
            self.assertEqual(r['steps'][0]['readback']['parameters'][field]['value'],{'level_raw':195,'kelvin_raw':3003})
            undo=self.controller.prepare_restore(p['plan_id']);self.allow(undo)
            self.assertEqual(self.apply(undo)['state'],'completed_readback')
            self.assertEqual(self.g.parameters,self.original)

    def test_paired_explicit_mask_from_finite_color_and_level_only(self):
        self.g.parameters['power_on']['value']['kelvin_raw']=4000
        original=copy.deepcopy(self.g.parameters)
        for kwargs,expected in [({'kelvin_raw':65535},{'level_raw':0,'kelvin_raw':65535}),
                                ({'level_raw':2},{'level_raw':2,'kelvin_raw':4000})]:
            p=self.controller.prepare_power_parameter(2,0,'power_on',**kwargs)
            self.assertEqual(p['proposal']['steps'][0]['after'],expected)
            self.allow(p);self.assertEqual(self.apply(p)['state'],'completed_readback')
            undo=self.controller.prepare_restore(p['plan_id']);self.allow(undo)
            self.assertEqual(self.apply(undo)['state'],'completed_readback')
            self.assertEqual(self.g.parameters,original)

    def test_paired_bad_input_before_network_and_unrestorable_original(self):
        for field,kwargs in [('fade_time',{'kelvin_raw':4000}),(None,{'kelvin_raw':4000}),
                             ('power_on',{}),('power_on',{'level_raw':255}),
                             ('power_on',{'level_raw':False}),('power_on',{'kelvin_raw':True}),
                             ('power_on',{'kelvin_raw':999}),('power_on',{'kelvin_raw':65534}),
                             ('power_on',{'kelvin_raw':'4000'}),('power_on',{'kelvin_raw':4000.0})]:
            with self.subTest(field=field,kwargs=kwargs),self.assertRaises(ValueError):
                self.controller.prepare_power_parameter(2,0,field,**kwargs)
        self.open_mock.assert_not_called()
        self.g.parameters['power_on']['value']['kelvin_raw']=2000
        with self.assertRaisesRegex(ValueError,'not restorable'):
            self.controller.prepare_power_parameter(2,0,'power_on',kelvin_raw=4000)
        with self.assertRaisesRegex(ValueError,'not restorable'):self.plan('power_on',2)
        self.assertFalse(list(Path(self.tmp.name).glob('parameter-plan-*.json')))
        self.assertFalse(self.g.writes)

    def test_paired_configured_limits_and_full_pair_gate(self):
        for kelvin in (2000,7000):
            with self.assertRaisesRegex(ValueError,'outside target configured limits'):
                self.controller.prepare_power_parameter(2,0,'power_on',kelvin_raw=kelvin)
        p=self.controller.prepare_power_parameter(2,0,'power_on',kelvin_raw=4000)
        self.service.config.update(enable_experimental_parameter_writes=True,
            allowed_parameter_write_targets={'channel':2,'address':0,'values':p['changes']})
        with self.assertRaises(PermissionError):self.apply(p)
        self.allow(p)
        self.service.config['allowed_parameter_write_targets']['values'][0]['values']['level_raw']=1
        with self.assertRaises(PermissionError):self.apply(p)
        self.assertFalse(self.g.writes)

    def test_paired_lost_receipt_and_unchanged_never_resend(self):
        p=self.controller.prepare_power_parameter(2,0,'power_on',kelvin_raw=65535)
        self.allow(p);self.assertEqual(self.apply(p)['state'],'completed_readback')
        self.assertFalse(self.g.writes)
        p=self.controller.prepare_power_parameter(2,0,'power_on',kelvin_raw=4000)
        self.allow(p);self.g.fault='lost'
        r=self.apply(p)
        self.assertEqual(r['state'],'stopped_receipt_unknown')
        self.assertEqual(r['steps'][0]['state'],'verified_receipt_unknown')
        with self.assertRaises(ValueError):self.apply(p)
        self.assertEqual(len(self.g.writes),1)
        self.g.fault=None
        undo=self.controller.prepare_restore(p['plan_id']);self.allow(undo)
        self.assertEqual(self.apply(undo)['state'],'completed_readback')
        self.assertEqual(self.g.parameters,self.original)

    def test_paired_preserved_component_conflict_blocks_write_and_restore(self):
        p=self.controller.prepare_power_parameter(2,0,'power_on',kelvin_raw=4000);self.allow(p)
        self.g.parameters['power_on']['value']['level_raw']=2
        self.assertEqual(self.apply(p)['state'],'preflight_failed')
        self.assertFalse(self.g.writes)
        p=self.controller.prepare_power_parameter(2,0,'power_on',kelvin_raw=4000);self.allow(p)
        self.assertEqual(self.apply(p)['state'],'completed_readback')
        self.g.parameters['power_on']['value']['level_raw']=3
        with self.assertRaisesRegex(ValueError,'neither original'):self.controller.prepare_restore(p['plan_id'])
        self.assertEqual(len(self.g.writes),1)

    def test_supported_generic_paired_plan_uses_same_checks(self):
        p=ExperimentalParameterController(self.service).prepare(2,0,
            [{'field':'system_failure','values':{'level_raw':195,'kelvin_raw':4000}}])
        self.allow(p);self.assertEqual(self.apply(p)['state'],'completed_readback')
        undo=self.controller.prepare_restore(p['plan_id']);self.allow(undo)
        self.assertEqual(self.apply(undo)['state'],'completed_readback')
        self.assertEqual(self.g.parameters,self.original)


class ParameterStdioTests(unittest.TestCase):
    def test_real_stdio_prepare_apply_restore_all_eight_fields_with_synthetic_gateway(self):
        root = Path(__file__).resolve().parents[1]
        bootstrap = '''import sys
sys.path[:0]=[sys.argv[1],sys.argv[1]+'/tests']
from test_parameter_control import QuantizedParameterGateway
from dali_mcp.service import Service
g=QuantizedParameterGateway()
g.parameters['min_level']['value']['level_raw']=2
g.parameters['max_level']['value']['level_raw']=254
Service._open=lambda self:(g,dict(ip='127.0.0.1',dali_mac='02:00:00:00:00:01',dali_firmware='test'))
from dali_mcp.__main__ import main
sys.argv=['server','--config',sys.argv[2]]
main()
'''
        async def run():
            with tempfile.TemporaryDirectory() as folder:
                config = Path(folder) / 'config.json'
                allowed=[]
                for field,target,original,_,_ in CASES:
                    for v in (target,original):
                        values=LampParameterController._values(field,v)
                        if field in LampParameterController.PAIRED:values['kelvin_raw']=65535
                        allowed.append({'field':field,'values':values})
                for field,level in [('power_on',0),('system_failure',229)]:
                    allowed.append({'field':field,'values':{'level_raw':level,'kelvin_raw':4000}})
                config.write_text(json.dumps({'gateway_ip': '127.0.0.1', 'expected_dali_mac': '02:00:00:00:00:01',
                    'expected_firmware': 'test', 'enable_experimental_parameter_writes': True,
                    'allowed_parameter_write_targets': {'channel': 2, 'address': 0, 'values': allowed}}), encoding='utf-8')
                params = StdioServerParameters(command=sys.executable, args=['-c', bootstrap, str(root), str(config)])
                async with Client(params) as client:
                    for field, target, original, _, _ in CASES:
                        prepared = await client.call_tool('dali_prepare_lamp_parameter',
                            dict(channel=2, address=0, field=field, value=target))
                        self.assertFalse(prepared.is_error); p = prepared.structured_content
                        result = await client.call_tool('dali_apply_lamp_parameter',
                            dict(plan_id=p['plan_id'], confirmation=p['confirmation']))
                        self.assertFalse(result.is_error)
                        self.assertEqual(result.structured_content['state'], 'completed_readback')
                        replay = await client.call_tool('dali_apply_lamp_parameter',
                            dict(plan_id=p['plan_id'], confirmation=p['confirmation']))
                        self.assertTrue(replay.is_error)
                        restored = await client.call_tool('dali_prepare_lamp_parameter_restore', dict(plan_id=p['plan_id']))
                        self.assertFalse(restored.is_error); r = restored.structured_content
                        result = await client.call_tool('dali_apply_lamp_parameter',
                            dict(plan_id=r['plan_id'], confirmation=r['confirmation']))
                        self.assertEqual(result.structured_content['state'], 'completed_readback')
                        record = await client.call_tool('dali_get_lamp_parameter_plan', dict(plan_id=r['plan_id']))
                        self.assertTrue(record.structured_content['claimed'])
                        expected=LampParameterController._values(field,original)
                        if field in LampParameterController.PAIRED:expected['kelvin_raw']=65535
                        self.assertEqual(record.structured_content['execution']['steps'][0]['candidate']['after'],expected)
                    for field,level in [('power_on',0),('system_failure',229)]:
                        prepared=await client.call_tool('dali_prepare_power_parameter',
                            dict(channel=2,address=0,field=field,kelvin_raw=4000))
                        self.assertFalse(prepared.is_error);p=prepared.structured_content
                        self.assertEqual(p['proposal']['steps'][0]['after'],{'level_raw':level,'kelvin_raw':4000})
                        result=await client.call_tool('dali_apply_lamp_parameter',dict(plan_id=p['plan_id'],confirmation=p['confirmation']))
                        self.assertEqual(result.structured_content['state'],'completed_readback')
                        restored=await client.call_tool('dali_prepare_lamp_parameter_restore',dict(plan_id=p['plan_id']))
                        self.assertFalse(restored.is_error);r=restored.structured_content
                        result=await client.call_tool('dali_apply_lamp_parameter',dict(plan_id=r['plan_id'],confirmation=r['confirmation']))
                        self.assertEqual(result.structured_content['state'],'completed_readback')
                        self.assertEqual(result.structured_content['steps'][0]['readback']['parameters'][field]['value'],
                            {'level_raw':level,'kelvin_raw':65535})
        asyncio.run(run())


if __name__ == '__main__': unittest.main()
