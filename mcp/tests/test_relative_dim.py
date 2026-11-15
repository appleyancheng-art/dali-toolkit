import copy
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dali_mcp.protocol import Gateway, encode, decode
from dali_mcp.service import Service


class WireSocket:
    def __init__(self, replies): self.replies = list(replies); self.sent = []
    def getsockname(self): return ('127.0.0.53', 19000)
    def settimeout(self, timeout): pass
    def sendto(self, packet, peer): self.sent.append(packet)
    def recvfrom(self, size):
        if not self.replies: raise socket.timeout()
        return self.replies.pop(0), ('127.0.0.1', 11211)


class RelativeWireTests(unittest.TestCase):
    route = bytes.fromhex('0109010115f0')

    def gateway(self, replies):
        g = Gateway.__new__(Gateway)
        g.route = self.route; g.sock = WireSocket(replies); g.seq = 10
        g.port = 19000; g.peer = ('127.0.0.1', 11211); g.timeout = 4; g.recent_packets = []
        return g

    def reply(self, seq=11, transport=1, route=None, payload=None, command=0xf091):
        return encode(command, payload if payload is not None else bytes([2,1,63,0,0,0,0,0]),
                      route or self.route[3:] + self.route[:3], seq, 19000, transport=transport)

    def test_native_up_down_payload_and_receipt_only(self):
        for direction, value in [('up', 1), ('down', 2)]:
            g = self.gateway([self.reply()])
            result = g.relative_dim(2, 63, direction)
            self.assertEqual(len(g.sock.sent), 1)
            sent = decode(g.sock.sent[0])
            self.assertEqual(sent.command, 0xf011)
            self.assertEqual(sent.payload, bytes([2,1,63,value,0,0,0,0]))
            self.assertEqual(g.sock.sent[0][34], 12)
            self.assertTrue(result['receipt_only'])
            self.assertFalse(result['status_interpreted'])

    def test_stale_async_wrong_target_route_and_command_do_not_count(self):
        alias = self.route[3:] + bytes([self.route[0], 53, self.route[2]])
        replies = [self.reply(seq=9), self.reply(seq=0, transport=2),
                   self.reply(transport=2), self.reply(route=bytes(6)),
                   self.reply(payload=bytes([2,1,62,0,0,0,0,0])),
                   self.reply(payload=bytes([2,1,63])), self.reply(command=0xf093),
                   self.reply(route=alias)]
        g = self.gateway(replies)
        self.assertEqual(g.relative_dim(2,63,'up')['reply_route'], alias.hex())
        self.assertEqual(len(g.sock.sent), 1)
        self.assertFalse(g.sock.replies)

    def test_timeout_invalidates_without_resend(self):
        g = self.gateway([self.reply(seq=0, transport=2)])
        with self.assertRaises(TimeoutError): g.relative_dim(2,63,'up')
        self.assertIsNone(g.route)
        self.assertEqual(len(g.sock.sent), 1)

    def test_invalid_input_before_any_send(self):
        for args in [(0,0,'up'), (True,0,'up'), (2,False,'up'), (2,64,'down'),
                     (2,1,'UP'), (2,1,1), (2,1,None)]:
            g = self.gateway([])
            with self.assertRaises(ValueError): g.relative_dim(*args)
            self.assertFalse(g.sock.sent)


class Backend:
    def __init__(self):
        self.value = 100; self.color = 4000; self.type = 1; self.sends = []
        self.opens = 0; self.closes = 0; self.reads = 0
        self.drop = False; self.reverse = False; self.unchanged = False
        self.change_color = False; self.drifting = False; self.fail_readback = False
        self.firmware = '1.6.9'

    def gateway(self):
        backend = self
        class FakeGateway:
            recent_packets = []
            def discover(self):
                backend.opens += 1
                return {'dali_mac':'02:00:00:00:00:01','dali_firmware':backend.firmware}
            def connect(self): pass
            def close(self): backend.closes += 1
            def light_type(self, ch, addr): return backend.type
            def read_actual(self, ch, addr, dt8):
                backend.reads += 1
                if backend.fail_readback and backend.sends: raise TimeoutError('readback lost')
                if backend.drifting: backend.value += 1
                return {'channel':ch,'address':addr,'light_type_code':1,
                        'level_raw':backend.value,'kelvin_raw':backend.color}
            def relative_dim(self, ch, addr, direction):
                backend.sends.append((ch,addr,direction))
                delta = 10 if direction == 'up' else -10
                if not backend.unchanged: backend.value += -delta if backend.reverse else delta
                if backend.change_color: backend.color = 5000
                if backend.drop: raise TimeoutError('receipt lost')
                return {'receipt_only':True,'status_interpreted':False}
        return FakeGateway()


class RelativeServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.backend = Backend()
        self.service = Service({'expected_dali_mac':'02:00:00:00:00:01','expected_firmware':'1.6.9'},
                               Path(self.temp.name), self.backend.gateway)
        self.service._relative_wait = lambda seconds: None

    def tearDown(self): self.temp.cleanup()

    def test_single_address_up_down_and_persisted_original(self):
        for direction, expected in [('up',110),('down',100)]:
            result = self.service.relative_dim(2,63,direction)
            self.assertEqual(result['state'], 'completed_readback')
            self.assertEqual(result['actual']['level_raw'], expected)
            self.assertTrue(result['color_unchanged'] and result['output_stable_observed'])
            self.assertEqual(self.service.get_relative_dim(result['operation_id']), result)
        self.assertEqual(self.backend.sends, [(2,63,'up'),(2,63,'down')])
        self.assertEqual(self.backend.opens, self.backend.closes)
        self.assertEqual(len(self.service.get_relative_dim()['operations']), 2)

    def test_receipt_loss_is_uncertain_even_with_expected_output_no_retry(self):
        self.backend.drop = True
        r = self.service.relative_dim(2,0,'up')
        self.assertEqual(r['state'], 'outcome_uncertain')
        self.assertEqual(r['actual']['level_raw'],110)
        self.assertFalse(r['receipt_received'])
        self.assertEqual(len(self.backend.sends),1)
        self.assertEqual(self.backend.opens,2)
        self.assertEqual(self.backend.closes,2)

    def test_unchanged_wrong_direction_or_color_not_success(self):
        for flag, expected in [('unchanged','no_change_observed'),('reverse','unexpected_output'),
                               ('change_color','outcome_uncertain')]:
            self.backend = Backend(); self.service.gateway_factory = self.backend.gateway
            setattr(self.backend, flag, True)
            r = self.service.relative_dim(2,0,'up')
            self.assertEqual(r['state'], expected)
            self.assertEqual(len(self.backend.sends),1)

    def test_preflight_invalid_unstable_or_wrong_identity_sends_nothing(self):
        for attribute, value in [('value',255),('color',65535),('type',0),
                                  ('drifting',True),('firmware','unvalidated')]:
            self.backend = Backend(); self.service.gateway_factory = self.backend.gateway
            setattr(self.backend, attribute, value)
            r = self.service.relative_dim(2,0,'up')
            self.assertEqual(r['state'],'preflight_failed')
            self.assertFalse(self.backend.sends)
            self.assertEqual(self.backend.opens,self.backend.closes)

    def test_failed_readback_does_not_send_again_or_restore(self):
        self.backend.fail_readback = True
        r = self.service.relative_dim(2,0,'up')
        self.assertEqual(r['state'],'outcome_uncertain')
        self.assertEqual(self.backend.sends,[(2,0,'up')])
        self.assertEqual(self.backend.value,110)

    def test_disk_failure_before_dispatch_prevents_control(self):
        original = self.service._save
        def save(kind, report, ident=None):
            if report['state'] == 'dispatch_intent': raise OSError('disk unavailable')
            return original(kind, report, ident)
        with patch.object(self.service, '_save', side_effect=save):
            r = self.service.relative_dim(2,0,'up')
        self.assertEqual(r['state'],'preflight_failed')
        self.assertFalse(self.backend.sends)

    def test_disk_failure_after_send_stops_and_preserves_unknown_evidence(self):
        original = self.service._save
        def save(kind, report, ident=None):
            if report['command_attempted']: raise OSError('disk unavailable')
            return original(kind, report, ident)
        with patch.object(self.service, '_save', side_effect=save):
            r = self.service.relative_dim(2,0,'up')
        self.assertEqual(r['state'],'outcome_uncertain')
        self.assertIn('journal_error',r)
        self.assertEqual(len(self.backend.sends),1)
        saved = self.service.get_relative_dim(r['operation_id'])
        self.assertEqual(saved['state'],'dispatch_intent')

    def test_validation_and_saved_report_queries_never_connect(self):
        for args in [(True,0,'up'),(2,64,'down'),(2,0,'invalid')]:
            with self.assertRaises(ValueError): self.service.relative_dim(*args)
        for ident in ['../x','x'*32,'']:
            with self.assertRaises(ValueError): self.service.get_relative_dim(ident)
        self.assertEqual(self.service.get_relative_dim()['operations'],[])
        self.assertEqual(self.backend.opens,0)


if __name__ == '__main__': unittest.main()
