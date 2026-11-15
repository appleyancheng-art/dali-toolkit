import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from dali_mcp.protocol import Gateway
from dali_mcp.service import Service
from test_adapter import Emulator


class LampSceneTests(unittest.TestCase):
    def setUp(self):
        self.em=Emulator();self.tmp=tempfile.TemporaryDirectory()
        self.config=dict(gateway_ip='127.0.0.1',expected_dali_mac='02:00:00:00:00:01',expected_firmware='1.6.9')
        self.service=Service(self.config,self.tmp.name,lambda:Gateway('127.0.0.1','127.0.0.1',0,.1,self.em.port))
        self.em.recall_receipt='captured'
        for ch in (1,2):
            for a in range(64):self.em.live[ch,a]=(0,4000)
        self.original=dict(self.em.live)

    def tearDown(self):self.em.stop();self.tmp.cleanup()

    def recall(self,**kw):
        return self.service.recall_lamp_scene(**dict(dict(channel=2,address=0,scene=8),**kw))

    def test_literal_address_and_scene_boundaries_do_not_touch_background_or_storage(self):
        for ch,addr,scene in ((1,0,0),(2,63,15),(2,0,8)):
            before=dict(self.em.live);self.em.values[ch,addr,scene]=(180,5000)
            r=self.recall(channel=ch,address=addr,scene=scene)
            self.assertEqual(r['state'],'samples_verified')
            self.assertEqual(self.em.recalls[-1],bytes([ch,1,addr,16,scene,0,0,0]))
            self.assertEqual({k:v for k,v in before.items() if k!=(ch,addr)},
                             {k:v for k,v in self.em.live.items() if k!=(ch,addr)})
            self.assertFalse(r['scene_storage_modified']);self.assertFalse(r['non_target_outputs_verified'])
            self.assertEqual(self.service.get_lamp_recall(r['operation_id']),r)
        self.assertFalse(self.em.writes or self.em.live_commands or self.em.scans)
        self.assertEqual(len(self.service.get_lamp_recall()['operations']),3)

    def test_invalid_target_rejected_before_open(self):
        for kw in ({'channel':True},{'channel':0},{'address':True},{'address':64},{'address':-1},
                   {'address':'0'},{'scene':False},{'scene':16},{'scene':-1}):
            with self.subTest(kw=kw),self.assertRaises(ValueError):self.recall(**kw)
        self.assertFalse(self.em.requests)
        for value in ('../config',True,'a'*31):
            with self.assertRaises(ValueError):self.service.get_lamp_recall(value)

    def test_mask_components_preserve_live_values(self):
        for target,expected in (((255,5000),(90,5000)),((180,65535),(180,4000))):
            self.em.live[2,0]=(90,4000);self.em.values[2,0,8]=target
            r=self.recall();self.assertTrue(r['samples_verified']);self.assertEqual(self.em.live[2,0],expected)
            self.assertEqual(r['before']['level_raw'],90)

    def test_all_mask_verifies_preservation_but_not_effect(self):
        self.em.values[2,0,8]=(255,65535)
        r=self.recall();self.assertEqual(r['state'],'receipt_received_unverified')
        self.assertEqual(r['verification_status'],'unverifiable');self.assertTrue(r['output_matches_expected'])
        self.assertFalse(r['samples_verified']);self.assertEqual(len(self.em.recalls),1)

    def test_off_scene_is_a_verifiable_target(self):
        self.em.live[2,0]=(180,5000);self.em.values[2,0,8]=(0,65535)
        r=self.recall();self.assertTrue(r['samples_verified']);self.assertEqual(self.em.live[2,0],(0,5000))

    def test_unknown_live_values_type_and_fade_do_not_send(self):
        self.em.lamp_type=0;self.assertEqual(self.recall()['state'],'preflight_failed');self.em.lamp_type=1
        for fade in (0,10):
            self.em.fade_code=fade;self.assertEqual(self.recall()['state'],'preflight_failed')
        self.em.fade_code=4
        for live in ((255,4000),(0,65535)):
            self.em.live[2,0]=live;self.assertEqual(self.recall()['state'],'preflight_failed')
        self.assertFalse(self.em.recalls)

    def test_invalid_scene_temperature_does_not_send(self):
        self.em.values[2,0,8]=(100,0)
        self.assertEqual(self.recall()['state'],'preflight_failed');self.assertFalse(self.em.recalls)

    def test_receipt_lost_stale_wrong_route_never_resends(self):
        for receipt in ('drop','stale','wrong_route'):
            with self.subTest(receipt=receipt):
                self.em.recall_receipt=receipt;n=len(self.em.recalls)
                r=self.recall();self.assertEqual(r['state'],'stopped_receipt_unknown')
                self.assertEqual(r['receipt_status'],'unknown');self.assertTrue(r['samples_verified'])
                self.assertEqual(len(self.em.recalls),n+1)

    def test_readback_failure_preserves_uncertainty(self):
        self.em.drop_actual_after_recall=True;r=self.recall()
        self.assertEqual(r['state'],'receipt_received_unverified');self.assertFalse(r['samples_verified'])
        self.assertIn('error',r['verification']);self.assertEqual(len(self.em.recalls),1)

    def test_ignored_control_is_not_success(self):
        self.em.ignore_recall=True
        def once(g,ch,a,target):return dict(actual=g.read_actual(ch,a,True),matches_target=False)
        with patch.object(self.service,'_wait_live_target',once):r=self.recall()
        self.assertEqual(r['verification_status'],'not_verified');self.assertEqual(len(self.em.recalls),1)

    def test_failed_intent_publication_blocks_command(self):
        original=self.service._save
        def fail(prefix,report,ident=None):
            if report['state']=='dispatching':raise OSError('disk full before recall')
            return original(prefix,report,ident)
        with patch.object(self.service,'_save',fail):r=self.recall()
        self.assertEqual(r['state'],'preflight_failed');self.assertFalse(r['dispatch_attempted'])
        self.assertFalse(self.em.recalls)

    def test_after_dispatch_log_failure_still_reads_without_resending(self):
        original=self.service._save
        def fail(prefix,report,ident=None):
            if self.em.recalls:raise OSError('disk full after recall')
            return original(prefix,report,ident)
        with patch.object(self.service,'_save',fail):r=self.recall()
        self.assertEqual(r['state'],'outcome_unknown');self.assertTrue(r['samples_verified'])
        self.assertEqual(len(self.em.recalls),1);self.assertIn('log_errors',r)
        disk=json.loads(next(Path(self.tmp.name).glob('recall-lamp-*.json')).read_text())
        self.assertEqual(disk['state'],'dispatching');self.assertIn('before',disk)
        self.assertFalse(disk['dispatch_attempted'])

    def test_reconnected_different_identity_prevents_wrong_device_readback(self):
        self.em.recall_receipt='drop';original=self.service._open;opens=0
        def changed():
            nonlocal opens
            g,info=original();opens+=1
            if opens>1:info['dali_mac']='02:00:00:00:00:02'
            return g,info
        with patch.object(self.service,'_open',changed):r=self.recall()
        self.assertEqual(r['state'],'outcome_unknown');self.assertEqual(len(self.em.recalls),1)
        self.assertNotIn('verification',r);self.assertIn('gateway changed',r['error'])


if __name__=='__main__':unittest.main()
