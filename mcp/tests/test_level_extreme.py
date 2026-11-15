import copy,tempfile,unittest
from unittest.mock import patch
from dali_mcp.protocol import Gateway,encode,decode
from dali_mcp.service import Service
from test_relative_dim import WireSocket

class ExtremeWireTests(unittest.TestCase):
    route=bytes.fromhex('0109010115f0')
    def gateway(self,replies):
        g=Gateway.__new__(Gateway);g.route=self.route;g.sock=WireSocket(replies)
        g.seq=10;g.port=19000;g.peer=('127.0.0.1',11211);g.timeout=4;g.recent_packets=[]
        return g
    def reply(self,ch=2,address=0,seq=11,transport=1,route=None,payload=None):
        return encode(0xf092,payload if payload is not None else bytes([ch,1,address,0,0,0,0,0]),
            route or self.route[3:]+self.route[:3],seq,19000,transport=transport)
    def test_boundaries_native_frames_and_receipt_only(self):
        for ch,address in ((1,0),(1,63),(2,0),(2,63)):
            for extreme,code in (('minimum',6),('maximum',5)):
                g=self.gateway([self.reply(ch,address)])
                result=g.set_level_extreme(ch,address,extreme)
                self.assertEqual(len(g.sock.sent),1)
                packet=decode(g.sock.sent[0]);self.assertEqual(packet.command,0xf012)
                self.assertEqual(packet.payload,bytes([ch,1,address,code,0,0,0,0]))
                self.assertEqual(g.sock.sent[0][34],12)
                self.assertTrue(result['receipt_only']);self.assertFalse(result['status_interpreted'])
    def test_wrong_target_route_sequence_and_async_rejected(self):
        alias=self.route[3:]+bytes([self.route[0],53,self.route[2]])
        replies=[self.reply(ch=1),self.reply(address=1),self.reply(seq=9),self.reply(transport=2),
            self.reply(route=bytes(6)),self.reply(payload=b'\x02\x01\0'),self.reply(route=alias)]
        g=self.gateway(replies);g.set_level_extreme(2,0,'minimum')
        self.assertEqual(len(g.sock.sent),1);self.assertFalse(g.sock.replies)
    def test_missing_receipt_not_resent(self):
        g=self.gateway([])
        with self.assertRaises(TimeoutError):g.set_level_extreme(2,0,'minimum')
        self.assertEqual(len(g.sock.sent),1);self.assertIsNone(g.route)
    def test_invalid_scope_before_send(self):
        for args in ((True,0,'maximum'),(2,False,'minimum'),(2,64,'maximum'),(0,0,'minimum'),(2,0,'off')):
            g=self.gateway([])
            with self.assertRaises(ValueError):g.set_level_extreme(*args)
            self.assertFalse(g.sock.sent)

class FakeGateway:
    def __init__(self):
        self.opened=0;self.closed=0;self.reads=0;self.commands=[];self.recent_packets=[]
        self.minimum=17;self.maximum=210;self.level=100;self.kelvin=4000;self.typ=1
        self.fail_receipt=False;self.bad_identity=False;self.on_write=None;self.drift=False
    def discover(self):
        self.opened+=1
        return dict(ip='127.0.0.1',dali_mac='changed' if self.bad_identity else '02:00:00:00:00:01',dali_firmware='1.6.9')
    def connect(self):pass
    def close(self):self.closed+=1
    def light_type(self,ch,address):return self.typ
    def read_lamp_parameter(self,ch,address,field,typ):
        return dict(value=dict(level_raw=self.minimum if field=='min_level' else self.maximum))
    def read_actual(self,ch,address,color):
        self.reads+=1
        if self.drift and self.reads==2:self.level+=1
        return dict(channel=ch,address=address,light_type_code=self.typ,level_raw=self.level,kelvin_raw=self.kelvin)
    def set_level_extreme(self,ch,address,extreme):
        self.commands.append((ch,address,extreme));self.level=self.minimum if extreme=='minimum' else self.maximum
        if self.on_write:self.on_write()
        if self.fail_receipt:raise TimeoutError('receipt missing after dispatch')
        return dict(receipt_only=True)

class ExtremeServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.g=FakeGateway()
        self.service=Service(dict(gateway_ip='127.0.0.1',expected_dali_mac='02:00:00:00:00:01',expected_firmware='1.6.9'),self.tmp.name,lambda:self.g)
        self.service._relative_wait=lambda _:None
    def tearDown(self):self.tmp.cleanup()
    def test_configured_extremes_not_zero_or_254_and_single_scope(self):
        for extreme,expected in (('maximum',210),('minimum',17)):
            r=self.service.set_level_extreme(2,63,extreme)
            self.assertEqual(r['state'],'completed_readback');self.assertEqual(r['actual']['level_raw'],expected)
            self.assertTrue(r['target_verified'] and r['color_unchanged'])
            self.assertFalse(r['storage_modified'] or r['color_command_sent'] or r['automatic_retry'])
            self.assertEqual(self.service.get_level_extreme(r['operation_id']),r)
        self.assertEqual(self.g.commands,[(2,63,'maximum'),(2,63,'minimum')])
    def test_already_at_target_does_not_claim_changed_output(self):
        self.g.level=self.g.maximum
        r=self.service.set_level_extreme(1,0,'maximum')
        self.assertEqual(r['state'],'completed_readback');self.assertFalse(r['output_changed'])
        self.assertEqual(len(self.g.commands),1)
    def test_invalid_public_scope_never_opens(self):
        for args in ((2,64,'minimum'),(True,0,'maximum'),(2,False,'maximum'),(2,0,'off')):
            with self.assertRaises(ValueError):self.service.set_level_extreme(*args)
        self.assertEqual(self.g.opened,0)
    def test_unknown_type_range_output_or_drift_refuses_dispatch(self):
        for field,value in (('typ',0),('minimum',0),('maximum',255),('minimum',211),('level',255),('kelvin',65535),('drift',True)):
            self.g=FakeGateway();setattr(self.g,field,value)
            r=self.service.set_level_extreme(2,0,'minimum')
            self.assertEqual(r['state'],'preflight_failed',(field,r));self.assertFalse(self.g.commands)
    def test_lost_receipt_reads_fresh_but_remains_unknown_without_retry(self):
        self.g.fail_receipt=True
        r=self.service.set_level_extreme(2,0,'minimum')
        self.assertEqual(r['state'],'stopped_receipt_unknown');self.assertTrue(r['target_verified'])
        self.assertEqual(self.g.opened,2);self.assertEqual(len(self.g.commands),1)
    def test_readback_identity_change_stops_without_retransmit(self):
        self.g.fail_receipt=True;self.g.on_write=lambda:setattr(self.g,'bad_identity',True)
        r=self.service.set_level_extreme(2,0,'minimum')
        self.assertEqual(r['state'],'outcome_unknown');self.assertEqual(len(self.g.commands),1)
        self.assertNotIn('actual',r)
    def test_color_change_and_failed_readback_are_not_success(self):
        for check in ({'actual':dict(level_raw=210,kelvin_raw=5000),'matches_target':False}, {'error':'query timeout'}):
            with patch.object(self.service,'_wait_live_target',return_value=check):
                r=self.service.set_level_extreme(2,0,'maximum')
            self.assertEqual(r['state'],'verification_failed');self.assertFalse(r['target_verified'])
    def test_initial_or_intent_log_failure_prevents_dispatch(self):
        original=self.service._save
        for at in ('preflight','dispatch_intent'):
            def fail(prefix,value,ident=None):
                if value['state']==at:raise OSError('disk unavailable')
                return original(prefix,value,ident)
            with patch.object(self.service,'_save',side_effect=fail):r=self.service.set_level_extreme(2,0,'maximum')
            self.assertEqual(r['state'],'preflight_failed');self.assertFalse(self.g.commands)
        self.assertEqual(self.g.opened,1)
    def test_final_journal_failure_overrides_success(self):
        original=self.service._save
        def fail(prefix,value,ident=None):
            if value['state']=='completed_readback':raise OSError('final journal unavailable')
            return original(prefix,value,ident)
        with patch.object(self.service,'_save',side_effect=fail):r=self.service.set_level_extreme(2,0,'maximum')
        self.assertEqual(r['state'],'outcome_unknown');self.assertIn('journal_error',r)
        self.assertEqual(len(self.g.commands),1)
        self.assertEqual(self.service.get_level_extreme(r['operation_id'])['state'],'reading_output')
    def test_saved_intent_precedes_dispatch_and_getter_no_network(self):
        seen=[]
        self.g.on_write=lambda:seen.append(copy.deepcopy(self.service.get_level_extreme()['operations'][0]))
        r=self.service.set_level_extreme(2,0,'maximum')
        self.assertEqual(seen[0]['state'],'dispatch_intent');self.assertFalse(seen[0]['command_attempted'])
        count=self.g.opened;self.service.get_level_extreme(r['operation_id']);self.service.get_level_extreme()
        with self.assertRaises(ValueError):self.service.get_level_extreme('../invalid')
        self.assertEqual(self.g.opened,count)

if __name__=='__main__':unittest.main()
