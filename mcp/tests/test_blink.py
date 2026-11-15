import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from dali_mcp.blink import BlinkController
from dali_mcp.protocol import Gateway, encode, decode
from dali_mcp.service import Service
from test_relative_dim import WireSocket


class BlinkWireTests(unittest.TestCase):
    route = bytes.fromhex('0109010115f0')

    def gateway(self, replies):
        g=Gateway.__new__(Gateway); g.route=self.route; g.sock=WireSocket(replies)
        g.seq=10;g.port=19000;g.peer=('127.0.0.1',11211);g.timeout=4;g.recent_packets=[]
        return g

    def reply(self, on, seq=11, transport=1, address=0, route=None):
        return encode(0xf092 if on else 0xf090,bytes([2,1,address,0,0,0,0,0]),
                      route or self.route[3:]+self.route[:3],seq,19000,transport=transport)

    def test_native_max_and_off_frames(self):
        for on,command,value in [(True,0xf012,5),(False,0xf010,0)]:
            g=self.gateway([self.reply(on)])
            r=g.blink_phase(2,0,on)
            self.assertEqual(len(g.sock.sent),1)
            p=decode(g.sock.sent[0]);self.assertEqual(p.command,command)
            self.assertEqual(p.payload,bytes([2,1,0,value,0,0,0,0]))
            self.assertEqual(g.sock.sent[0][34],12)
            self.assertTrue(r['receipt_only']);self.assertFalse(r['status_interpreted'])

    def test_stale_wrong_target_async_and_wrong_route_rejected(self):
        alias=self.route[3:]+bytes([self.route[0],53,self.route[2]])
        for on in (True,False):
            g=self.gateway([self.reply(on,seq=9),self.reply(on,seq=0,transport=2),
                self.reply(on,transport=2),self.reply(on,address=1),self.reply(on,route=bytes(6)),
                self.reply(not on),self.reply(on,route=alias)])
            g.blink_phase(2,0,on)
            self.assertEqual(len(g.sock.sent),1);self.assertFalse(g.sock.replies)

    def test_lost_receipt_no_repeated_frame(self):
        g=self.gateway([])
        with self.assertRaises(TimeoutError):g.blink_phase(2,0,True)
        self.assertEqual(len(g.sock.sent),1);self.assertIsNone(g.route)

    def test_invalid_before_send(self):
        for args in [(True,0,True),(2,64,True),(2,False,False),(2,0,1),(2,0,'on')]:
            g=self.gateway([])
            with self.assertRaises(ValueError):g.blink_phase(*args)
            self.assertFalse(g.sock.sent)


class Clock:
    def __init__(self):self.now=0
    def __call__(self):return self.now
    def wait(self,event,seconds):self.now+=max(0,seconds);return event.is_set()


class Fixture:
    def __init__(self):
        self.value=100;self.color=4000;self.maximum=254;self.typ=1;self.fade=2.01
        self.commands=[];self.opens=0;self.closes=0;self.fail_edge=False;self.fail_restore=False
        self.unknown_restore=False;self.change_color=False;self.unstable=False;self.ignore_edges=False
        self.clock=Clock();self.edge_delay=0;self.firmware='1.6.9'

    def gateway(self):
        b=self
        class FakeGateway:
            route=b'connected';recent_packets=[]
            def discover(self):
                b.opens+=1
                return {'dali_mac':'02:00:00:00:00:01','dali_firmware':b.firmware}
            def connect(self):pass
            def close(self):b.closes+=1
            def light_type(self,ch,a):return b.typ
            def read_fade_timing(self,ch,a):return {'fade_seconds_upper':b.fade}
            def read_lamp_parameter(self,ch,a,field,typ):return {'value':{'level_raw':b.maximum}}
            def read_actual(self,ch,a,dt8):
                if b.unstable:b.value+=1
                return {'channel':ch,'address':a,'light_type_code':1,'level_raw':b.value,'kelvin_raw':b.color}
            def blink_phase(self,ch,a,on):
                b.commands.append(('blink',ch,a,on,b.clock()))
                if not b.ignore_edges:b.value=b.maximum if on else 0
                if b.change_color:b.color=5000
                b.clock.now+=b.edge_delay
                if b.fail_edge:raise TimeoutError('edge receipt lost')
                return {'receipt_only':True}
            def set_live_component(self,ch,a,component,value):
                b.commands.append(('restore',ch,a,value,b.clock()))
                if not b.fail_restore:b.value=value
                if b.unknown_restore:raise TimeoutError('restore receipt lost')
                return {'receipt_only':True}
        return FakeGateway()


class BlinkTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.b=Fixture()
        self.service=Service({'expected_dali_mac':'02:00:00:00:00:01','expected_firmware':'1.6.9'},
                             Path(self.temp.name),self.b.gateway)
        self.controller=BlinkController(self.service)
        self.controller.clock=self.b.clock;self.controller.wait=self.b.clock.wait
        # Keep restoration tests deterministic while retaining explicit readback.
        def read(g,ch,a,expected):
            actual=g.read_actual(ch,a,True)
            return {'actual':actual,'matches_target':actual['level_raw']==expected['level_raw']}
        self.service._wait_live_target=read

    def tearDown(self):self.temp.cleanup()

    def test_slow_native_edges_samples_and_original_lit_restore(self):
        r=self.controller.execute(2,63,cycles=2,period_ms=2000)
        self.assertEqual(r['state'],'completed_verified_restored')
        self.assertTrue(r['all_phase_samples_verified'] and r['restore_verified'])
        self.assertEqual([x[3] for x in self.b.commands if x[0]=='blink'],[True,False,True,False])
        self.assertEqual([p['dispatch_offset_seconds'] for p in r['phases']],[0,1,2,3])
        self.assertEqual(self.b.commands[-1][:4],('restore',2,63,100))
        self.assertEqual(self.b.value,100);self.assertEqual(self.b.opens,self.b.closes)
        saved=self.controller.get(r['operation_id'])
        self.assertEqual(saved['state'],r['state']);self.assertFalse(saved['active_in_this_process'])

    def test_fast_does_not_claim_unobserved_physical_phases(self):
        self.b.value=0
        r=self.controller.execute(2,0,cycles=3,period_ms=400)
        self.assertEqual(r['state'],'dispatched_restored')
        self.assertFalse(r['all_phase_samples_verified'] or r['phase_sampling'])
        self.assertEqual(len(self.b.commands),6)
        self.assertEqual([p['dispatch_offset_seconds'] for p in r['phases']],[0,.2,.4,.6,.8,1])
        self.assertTrue(r['restore_verified'])

    def test_phase_mismatch_not_reported_verified(self):
        self.b.ignore_edges=True
        r=self.controller.execute(2,0,1,2000)
        self.assertEqual(r['state'],'dispatched_restored')
        self.assertFalse(r['all_phase_samples_verified'])
        self.assertTrue(r['restore_verified'])

    def test_uncertain_edge_stops_sequence_and_restores_without_replay(self):
        self.b.fail_edge=True
        r=self.controller.execute(2,0,3,400)
        self.assertEqual(r['state'],'stopped_restored')
        self.assertEqual([c[0] for c in self.b.commands],['blink','restore'])
        self.assertEqual(self.b.opens,2);self.assertEqual(self.b.closes,2)
        self.assertTrue(r['restore_verified'])

    def test_scheduling_overrun_does_not_burst_catch_up(self):
        self.b.edge_delay=.5
        r=self.controller.execute(2,0,3,400)
        self.assertEqual(r['state'],'stopped_restored')
        self.assertEqual(sum(c[0]=='blink' for c in self.b.commands),1)
        self.assertIn('overrun',r['error'])

    def test_cancel_interrupts_hold_then_restores(self):
        def wait(event,seconds):
            if self.b.commands:event.set()
            return self.b.clock.wait(event,seconds)
        self.controller.wait=wait
        r=self.controller.execute(2,0,3,2000)
        self.assertEqual(r['state'],'cancelled_restored')
        self.assertEqual(sum(c[0]=='blink' for c in self.b.commands),1)
        self.assertTrue(r['restore_verified'])

    def test_get_and_cancel_during_bus_lock_do_not_block(self):
        holding=threading.Event();release=threading.Event();result=[]
        def wait(event,seconds):
            if self.b.commands:
                holding.set();release.wait(2)
            return self.b.clock.wait(event,seconds)
        self.controller.wait=wait
        thread=threading.Thread(target=lambda:result.append(self.controller.execute(2,0,3,2000)))
        thread.start()
        try:
            self.assertTrue(holding.wait(2))
            record=self.controller.get()['operations'][0]
            self.assertTrue(record['active_in_this_process'])
            self.assertTrue(self.controller.cancel(record['operation_id'])['cancel_requested'])
        finally:
            release.set();thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result[0]['state'],'cancelled_restored')
        self.assertFalse(self.controller.cancel(result[0]['operation_id'])['cancel_requested'])

    def test_color_change_is_not_overwritten_or_reported_restored(self):
        self.b.change_color=True
        r=self.controller.execute(2,0,2,2000)
        self.assertEqual(r['state'],'restore_unverified')
        self.assertEqual(self.b.value,100);self.assertEqual(self.b.color,5000)
        self.assertEqual(sum(c[0]=='blink' for c in self.b.commands),1)

    def test_restore_failure_or_lost_receipt_retains_readback_truth(self):
        self.b.fail_restore=True
        r=self.controller.execute(2,0,1,400)
        self.assertEqual(r['state'],'restore_unverified')
        self.b.fail_restore=False;self.b.value=100;self.b.unknown_restore=True
        r=self.controller.execute(2,0,1,400)
        self.assertTrue(r['restore_verified'])
        self.assertIn('restore_dispatch_error',r)
        self.assertEqual(sum(c[0]=='restore' for c in self.b.commands),2)

    def test_invalid_and_unstable_preflight_send_nothing(self):
        for field,value in [('value',255),('color',65535),('maximum',0),('typ',0),
                             ('fade',None),('fade',6),('unstable',True),('firmware','other')]:
            previous=getattr(self.b,field);setattr(self.b,field,value)
            r=self.controller.execute(2,0,1,2000)
            self.assertEqual(r['state'],'preflight_failed')
            self.assertFalse(self.b.commands)
            setattr(self.b,field,previous)
        self.assertEqual(self.b.opens,self.b.closes)

    def test_invalid_bounds_before_network(self):
        for args in [(True,0,1,400),(2,64,1,400),(2,0,True,400),(2,0,1,True),
                     (2,0,0,400),(2,0,101,400),(2,0,1,399),(2,0,1,450),(2,0,2,60000)]:
            with self.assertRaises(ValueError):self.controller.execute(*args)
        self.assertEqual(self.b.opens,0)
        for ident in ('../bad','X'*32):
            with self.assertRaises(ValueError):self.controller.get(ident)
            with self.assertRaises(ValueError):self.controller.cancel(ident)

    def test_disk_failure_before_first_edge_prevents_control(self):
        original=self.service._save
        def save(kind,report,ident=None):
            if report['state']=='dispatch_intent':raise OSError('disk full')
            return original(kind,report,ident)
        with patch.object(self.service,'_save',side_effect=save):r=self.controller.execute(2,0,1,400)
        self.assertEqual(r['state'],'preflight_failed');self.assertFalse(self.b.commands)

    def test_disk_failure_after_edge_does_not_prevent_restoration(self):
        original=self.service._save
        def save(kind,report,ident=None):
            if self.b.commands:raise OSError('disk full')
            return original(kind,report,ident)
        with patch.object(self.service,'_save',side_effect=save):r=self.controller.execute(2,0,2,400)
        self.assertEqual(r['state'],'stopped_restored');self.assertTrue(r['restore_verified'])
        self.assertTrue(r['journal_errors'])
        self.assertEqual([c[0] for c in self.b.commands],['blink','restore'])


if __name__=='__main__':unittest.main()
