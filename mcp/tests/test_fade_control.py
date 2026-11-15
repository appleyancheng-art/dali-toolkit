import copy
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from dali_mcp.service import Service
from dali_mcp.fade_control import FadeTimeController
from dali_mcp.parameter_execution import ExperimentalParameterController
from test_parameter_execution import LabGateway


class FadeControlTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.g=LabGateway()
        self.info={'ip':'127.0.0.1','dali_mac':'02:00:00:00:00:01','dali_firmware':'test'}
        self.service=Service({'gateway_ip':self.info['ip'],'expected_dali_mac':self.info['dali_mac'],
            'expected_firmware':'test'},self.tmp.name)
        self.controller=FadeTimeController(self.service)
        self.open_patch=patch.object(self.service,'_open',side_effect=lambda:(self.g,copy.deepcopy(self.info)))
        self.open_mock=self.open_patch.start()
    def tearDown(self):self.open_patch.stop();self.tmp.cleanup()
    def allow(self,code):
        self.service.config.update(enable_experimental_parameter_writes=True,
            allowed_parameter_write_targets={'channel':2,'address':0,'values':[{'field':'fade_time','values':{'code':code}}]})
    def plan(self):return self.controller.prepare_fade(2,0,5)
    def apply(self,plan):return self.controller.execute(plan['plan_id'],plan['confirmation'])

    def test_invalid_codes_fail_before_network(self):
        for code in [True,'5',None,0,16,-1,1.5]:
            with self.assertRaises(ValueError):self.controller.prepare_fade(2,0,code)
        self.open_mock.assert_not_called()

    def test_default_write_gate_denies_and_exact_value_scope_is_required(self):
        p=self.plan()
        with self.assertRaises(PermissionError):self.apply(p)
        self.allow(4)
        with self.assertRaises(PermissionError):self.apply(p)
        self.assertFalse(self.g.writes)

    def test_roundtrip_get_claim_readback_and_restore_no_replay(self):
        p=self.plan();self.allow(5)
        self.assertFalse(self.controller.get(p['plan_id'])['claimed'])
        self.assertEqual(self.apply(p)['state'],'completed_readback')
        record=self.controller.get(p['plan_id'])
        self.assertTrue(record['claimed'])
        self.assertNotIn('confirmation',record)
        self.assertEqual(record['execution']['steps'][0]['state'],'verified_readback')
        with self.assertRaises(ValueError):self.apply(p)
        restore=self.controller.prepare_restore(p['plan_id']);self.allow(4)
        self.assertEqual(self.apply(restore)['state'],'completed_readback')
        self.assertEqual(self.g.parameters['fade_time_rate']['value']['raw'],0x47)
        self.assertEqual(len(self.g.writes),2)

    def test_generic_parameter_plan_cannot_bypass_fade_only_facade(self):
        p=ExperimentalParameterController(self.service).prepare(2,0,[{'field':'fade_rate','values':{'code':5}}])
        self.service.config.update(enable_experimental_parameter_writes=True,
            allowed_parameter_write_targets={'channel':2,'address':0,'values':[{'field':'fade_rate','values':{'code':5}}]})
        with self.assertRaisesRegex(ValueError,'other parameter fields'):self.apply(p)
        self.assertFalse(self.g.writes)
        self.assertEqual(len(self.controller.get()['excluded_records']),1)

    def test_original_extended_fade_cannot_become_unrestorable_plan(self):
        self.g.parameters['fade_time_rate']['value'].update(raw=7,fade_time_code=0,extended_fade_not_read=True)
        with self.assertRaises(ValueError):self.plan()
        self.assertFalse(self.g.writes)

    def test_claim_without_execution_report_remains_uncertain(self):
        p=self.plan()
        path=Path(self.tmp.name)/f"parameter-claim-{p['plan_id']}.json"
        path.write_text(json.dumps({'execution_id':'f'*32}),encoding='utf-8')
        result=self.controller.get(p['plan_id'])
        self.assertTrue(result['claimed'])
        self.assertIn('execution_unavailable',result)
        self.assertNotIn('confirmation',result)
        self.allow(5)
        with self.assertRaises(FileExistsError):self.apply(p)
        self.assertFalse(self.g.writes)

    def test_read_progress_without_bus_lock_or_network(self):
        p=self.plan();entered=threading.Event();release=threading.Event()
        def hold():
            with self.service.lock:entered.set();release.wait(3)
        worker=threading.Thread(target=hold);worker.start()
        try:
            self.assertTrue(entered.wait(1))
            calls=self.open_mock.call_count
            self.assertEqual(self.controller.get(p['plan_id'])['plan']['plan_id'],p['plan_id'])
            self.assertEqual(self.open_mock.call_count,calls)
        finally:release.set();worker.join(4)

    def test_tamper_and_path_traversal_rejected(self):
        p=self.plan()
        with self.assertRaises(ValueError):self.controller.get('../bad')
        path=Path(self.tmp.name)/f"parameter-plan-{p['plan_id']}.json"
        stored=json.loads(path.read_text(encoding='utf-8'));stored['changes'][0]['values']['code']=6
        path.write_text(json.dumps(stored),encoding='utf-8')
        with self.assertRaises(ValueError):self.controller.get(p['plan_id'])
        self.assertFalse(self.g.writes)


if __name__=='__main__':unittest.main()
