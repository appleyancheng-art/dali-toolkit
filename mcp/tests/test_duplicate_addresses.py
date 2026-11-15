import json
from pathlib import Path
import tempfile
import unittest
from dali_mcp.service import Service
from dali_mcp.protocol import Gateway
from test_adapter import Emulator


class DuplicateAddressTests(unittest.TestCase):
    def setUp(self):
        self.em=Emulator();self.tmp=tempfile.TemporaryDirectory()
        self.cfg=dict(gateway_ip='127.0.0.1',expected_dali_mac='02:00:00:00:00:01',expected_firmware='1.6.9',
                      duplicate_query_timeout_seconds=.15)
        self.connections=[]
        def factory():
            g=Gateway('127.0.0.1','127.0.0.1',port=0,timeout=.15,gateway_port=self.em.port)
            self.connections.append(g);return g
        self.s=Service(self.cfg,Path(self.tmp.name),factory)

    def tearDown(self):
        self.assertFalse(self.em.writes);self.assertFalse(self.em.live_commands);self.assertFalse(self.em.scans)
        for g in self.connections:self.assertEqual(g.sock.fileno(),-1)
        self.em.stop();self.tmp.cleanup()

    def test_direct_final_bitmap_boundaries_and_heartbeat(self):
        self.em.heartbeat_gate=True
        r=self.s.read_duplicate_addresses(2)
        self.assertEqual(r['state'],'completed');self.assertEqual(r['duplicate_addresses'],[0,7,8,31,32,63])
        self.assertFalse(r['physical_uniqueness_verified']);self.assertFalse(r['bitmap_freshness_verified'])
        self.assertEqual(r['raw_final_payload'],'02018101008001000080')
        saved=json.loads((self.s.state_dir/f"duplicate-addresses-{r['operation_id']}.json").read_text(encoding='utf-8'))
        self.assertEqual(saved,r);self.assertEqual(self.em.requests.count(0xf043),1)

    def test_correlated_alias_empty_is_reported_not_uniqueness(self):
        self.em.duplicate_alias=True;self.em.duplicate_addresses=[]
        r=self.s.read_duplicate_addresses(1)
        self.assertEqual(r['state'],'completed');self.assertEqual(r['duplicate_addresses'],[])
        self.assertEqual(r['reply_transport'],1);self.assertEqual(r['count'],0)
        self.assertFalse(r['physical_uniqueness_verified'])

    def test_foreign_channel_sequence_route_truncation_and_async_alias_ignored(self):
        self.em.duplicate_noise=True;self.em.duplicate_alias=True
        r=self.s.read_duplicate_addresses(2)
        self.assertEqual(r['state'],'completed');self.assertEqual(r['count'],6)

    def test_receipts_and_invalid_alias_never_become_empty_success(self):
        self.em.duplicate_noise=True;self.em.drop_duplicate=True
        r=self.s.read_duplicate_addresses(2)
        self.assertEqual(r['state'],'query_unknown');self.assertNotIn('duplicate_addresses',r)
        self.assertNotIn('count',r);self.assertEqual(self.em.requests.count(0xf043),1)
        self.assertIsNone(self.connections[-1].route)

    def test_final_failure_preserves_error_and_no_fabricated_bitmap(self):
        self.em.duplicate_status=2;self.em.duplicate_alias=True
        r=self.s.read_duplicate_addresses(2)
        self.assertEqual(r['state'],'query_unknown');self.assertIn('status 2',r['error'])
        self.assertNotIn('duplicate_addresses',r);self.assertEqual(self.em.requests.count(0xf043),1)

    def test_invalid_channel_or_timeout_rejected_before_socket(self):
        for ch in (True,False,0,3,1.0,'2',None):
            with self.assertRaises(ValueError):self.s.read_duplicate_addresses(ch)
        for t in (True,0,61,float('nan'),float('inf'),'10'):
            self.cfg['duplicate_query_timeout_seconds']=t
            with self.assertRaises(ValueError):self.s.read_duplicate_addresses(2)
        self.assertEqual(self.connections,[]);self.assertEqual(self.em.requests,[])

    def test_identity_mismatch_does_not_issue_query(self):
        self.cfg['expected_dali_mac']='00:00:00:00:00:00'
        r=self.s.read_duplicate_addresses(2)
        self.assertEqual(r['state'],'query_unknown');self.assertIn('identity',r['error'])
        self.assertEqual(self.em.requests,[0xfe01]);self.assertNotIn('duplicate_addresses',r)


if __name__=='__main__':unittest.main()
