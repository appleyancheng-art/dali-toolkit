import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from dali_mcp.protocol import Gateway
from dali_mcp.service import Service
from test_adapter import Emulator


class GroupSceneTests(unittest.TestCase):
    def setUp(self):
        self.em=Emulator();self.tmp=tempfile.TemporaryDirectory()
        self.config={'gateway_ip':'127.0.0.1','expected_dali_mac':'02:00:00:00:00:01','expected_firmware':'1.6.9',
                     'scan_timeout_seconds':.1,'enable_experimental_writes':False}
        self.service=Service(self.config,self.tmp.name,lambda:Gateway('127.0.0.1','127.0.0.1',0,.1,self.em.port))
        self.members=[0,7,63];self.em.scan_addresses=[0,1,7,63]
        self.em.group_masks={(2,a):1<<1 for a in self.members}
        for a in self.em.scan_addresses:self.em.live[(2,a)]=(0,4000)
        for a in self.members:self.em.values[(2,a,8)]=(180,5000)

    def tearDown(self):self.em.stop();self.tmp.cleanup()

    def recall(self,**override):
        args=dict(channel=2,group=1,scene=8,expected_addresses=self.members,verify_addresses=self.members)
        return self.service.recall_group_scene(**dict(args,**override))

    def test_group_only_literal_scene_and_persisted_originals(self):
        self.em.scan_requires_reconnect=True;self.em.recall_receipt='captured'
        r=self.recall()
        self.assertEqual(r['state'],'samples_verified');self.assertTrue(r['samples_verified'])
        self.assertEqual(self.em.recalls,[bytes.fromhex('0202011008000000')])
        self.assertEqual(self.em.live[(2,1)],(0,4000));self.assertEqual(len(r['originals']),3)
        self.assertEqual(r['receipt_status'],'received');self.assertFalse(r['receipt']['status_interpreted'])
        self.assertEqual(self.service.get_group_recall(r['operation_id']),r)
        self.assertEqual(len(self.service.get_group_recall()['operations']),1)
        self.assertFalse(self.em.live_commands or self.em.writes)
        self.assertFalse(r['nonmember_verification_performed'])

    def test_channel_unchanged_and_group15_scene15_not_shifted(self):
        self.em.group_masks={(2,a):1<<15 for a in self.members}
        for a in self.members:self.em.values[(2,a,15)]=(100,4000)
        r=self.recall(group=15,scene=15)
        self.assertEqual(r['state'],'samples_verified')
        self.assertEqual(self.em.recalls,[bytes.fromhex('02020f100f000000')])
        self.assertFalse(any(k[0]==1 for k in self.em.live))

    def test_invalid_scope_rejected_before_network(self):
        for override in ({'channel':True},{'group':True},{'scene':True},{'scene':16},{'group':16},
                         {'expected_addresses':[]},{'expected_addresses':[0,True]}, {'expected_addresses':[0,0]},
                         {'verify_addresses':[]},{'verify_addresses':[1]},{'verify_addresses':[False]}):
            with self.assertRaises(ValueError):self.recall(**override)
        self.assertFalse(self.em.requests)
        with self.assertRaises(ValueError):self.service.get_group_recall('../config')

    def test_extra_and_missing_group_members_refuse(self):
        for expected in ([0,7],[0,1,7,63]):
            r=self.recall(expected_addresses=expected,verify_addresses=[0])
            self.assertEqual(r['state'],'scope_changed');self.assertEqual(r['sent_count'],0)
        self.assertFalse(self.em.recalls)

    def test_scan_failure_and_member_failure_do_not_send(self):
        self.em.drop_scan=True;r=self.recall();self.assertEqual(r['state'],'preflight_failed')
        self.em.drop_scan=False;self.em.drop_group_address=7;r=self.recall()
        self.assertEqual(r['state'],'preflight_failed');self.assertEqual(r['pending'],{'phase':'membership','address':7})
        self.assertFalse(self.em.recalls)

    def test_type_unknown_color_or_long_fade_reject_before_send(self):
        self.em.lamp_type=0;self.assertEqual(self.recall()['state'],'preflight_failed')
        self.em.lamp_type=1;self.em.fade_code=0;self.assertEqual(self.recall()['state'],'preflight_failed')
        self.em.fade_code=10;self.assertEqual(self.recall()['state'],'preflight_failed')
        self.em.fade_code=4;self.em.live[(2,0)]=(0,65535);self.assertEqual(self.recall()['state'],'preflight_failed')
        self.assertFalse(self.em.recalls)

    def test_mask_components_preserved_and_verified(self):
        self.em.live[(2,0)]=(90,4000);self.em.values[(2,0,8)]=(255,5000)
        self.em.values[(2,7,8)]=(180,65535);self.em.values[(2,63,8)]=(255,65535)
        r=self.recall();self.assertEqual(r['state'],'samples_verified');self.assertEqual(r['verified_sample_count'],2)
        self.assertEqual(self.em.live[(2,0)],(90,5000));self.assertEqual(self.em.live[(2,7)],(180,4000))
        self.assertEqual(self.em.live[(2,63)],(0,4000));self.assertFalse(r['verification'][-1]['verifiable'])

    def test_all_mask_is_not_claimed_as_effect_proof(self):
        for a in self.members:self.em.values[(2,a,8)]=(255,65535)
        r=self.recall();self.assertEqual(r['verification_status'],'unverifiable')
        self.assertFalse(r['samples_verified']);self.assertEqual(r['verified_sample_count'],0)
        self.assertEqual(len(self.em.recalls),1)

    def test_lost_stale_and_wrong_route_receipts_never_resend(self):
        for receipt in ('drop','stale','wrong_route'):
            self.em.recall_receipt=receipt;n=len(self.em.recalls);r=self.recall()
            self.assertEqual(r['state'],'stopped_receipt_unknown',receipt)
            self.assertTrue(r['samples_verified']);self.assertEqual(r['receipt_status'],'unknown')
            self.assertEqual(len(self.em.recalls),n+1)

    def test_ignored_control_reports_mismatch(self):
        self.em.ignore_recall=True
        def once(g,channel,address,expected):
            actual=g.read_actual(channel,address,True)
            return {'address':address,'actual':actual,'matches_target':False}
        with patch.object(self.service,'_wait_live_target',once):r=self.recall()
        self.assertEqual(r['verification_status'],'not_verified');self.assertFalse(r['samples_verified'])
        self.assertEqual(len(self.em.recalls),1)

    def test_readback_failure_preserves_uncertainty(self):
        self.em.drop_actual_after_recall=True;r=self.recall()
        self.assertEqual(r['verification_status'],'not_verified');self.assertEqual(len(r['verification']),1)
        self.assertIn('error',r['verification'][0]);self.assertEqual(len(self.em.recalls),1)

    def test_durable_originals_required_before_dispatch(self):
        save=self.service._save
        def fail(prefix,report,ident=None):
            if report['state']=='dispatching':raise OSError('disk full before call')
            return save(prefix,report,ident)
        with patch.object(self.service,'_save',fail):r=self.recall()
        self.assertFalse(self.em.recalls);self.assertIn('disk full',r['error'])

    def test_after_send_log_failure_never_repeats(self):
        save=self.service._save
        def fail(prefix,report,ident=None):
            if self.em.recalls:raise OSError('disk full after recall')
            return save(prefix,report,ident)
        with patch.object(self.service,'_save',fail):r=self.recall()
        self.assertEqual(r['state'],'outcome_unknown');self.assertEqual(len(self.em.recalls),1)
        self.assertIn('log_error',r)
        disk=json.loads(next(Path(self.tmp.name).glob('recall-group-*.json')).read_text())
        self.assertEqual(len(disk['originals']),3);self.assertEqual(disk['state'],'dispatching')


if __name__=='__main__':unittest.main()
