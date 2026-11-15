"""Exercise actual framing/exchange against staged UDP replies, without a LAN."""
import socket
import unittest
from dali_mcp.protocol import Gateway, encode
from dali_mcp.parameter_execution import send_candidate_once
from dali_mcp.parameter_planning import encode_parameter_candidate


class SocketFixture:
    def __init__(self, packets):self.packets=list(packets);self.sent=[]
    def getsockname(self):return ('127.0.0.53',19000)
    def settimeout(self,timeout):pass
    def sendto(self,packet,peer):self.sent.append((packet,peer))
    def recvfrom(self,size):
        if not self.packets:raise socket.timeout()
        return self.packets.pop(0),('127.0.0.1',11211)


class CtReceiptTests(unittest.TestCase):
    route=bytes.fromhex('0109010115f0')
    def gateway(self,packets):
        g=Gateway.__new__(Gateway)
        g.route=self.route;g.sock=SocketFixture(packets);g.seq=10
        g.port=19000;g.peer=('127.0.0.1',11211);g.timeout=4;g.recent_packets=[]
        return g

    def reply(self,command=0xf0a8,status=1,sequence=0,transport=2,route=None,address=0):
        return encode(command,bytes([2,1,address,status,0,0,0,0]),
                      route or self.route[3:]+self.route[:3],sequence,19000,transport=transport)

    def candidate(self,field='configured_min_ct'):
        return encode_parameter_candidate(2,0,field,{'kelvin_raw':3000 if field.endswith('min_ct') else 6000})

    def test_pending_then_terminal_both_ct_limits_one_datagram(self):
        for field,command in [('configured_min_ct',0xf0a8),('configured_max_ct',0xf0a6)]:
            g=self.gateway([self.reply(command,0,11,1),self.reply(command)])
            result=send_candidate_once(g,self.candidate(field))
            self.assertEqual(len(g.sock.sent),1)
            self.assertEqual(result['sequence'],0)
            self.assertEqual(result['terminal_status_byte'],1)
            self.assertTrue(result['waited_for_terminal_status'] and result['readback_required'])
            self.assertEqual(len(g.recent_packets),2)

    def test_reject_stale_wrong_scope_route_and_alias_async(self):
        alias=self.route[3:]+bytes([self.route[0],53,self.route[2]])
        g=self.gateway([self.reply(status=0,sequence=11,transport=1),
                        self.reply(sequence=9,transport=1),self.reply(address=1),
                        self.reply(route=bytes.fromhex('0135010215f0')),
                        self.reply(route=alias),self.reply()])
        result=send_candidate_once(g,self.candidate())
        self.assertEqual(result['route'],(self.route[3:]+self.route[:3]).hex())
        self.assertEqual(len(g.sock.sent),1)
        self.assertFalse(g.sock.packets)

    def test_pending_without_final_times_out_without_resending(self):
        g=self.gateway([self.reply(status=0,sequence=11,transport=1)])
        with self.assertRaises(TimeoutError):send_candidate_once(g,self.candidate())
        self.assertEqual(len(g.sock.sent),1)
        self.assertIsNone(g.route)

    def test_power_and_failure_wait_for_terminal_preserve_mask(self):
        for field,command,level in [('power_on',0xf0a0,2),('system_failure',0xf0a2,195)]:
            candidate=encode_parameter_candidate(2,0,field,{'level_raw':level,'kelvin_raw':65535})
            g=self.gateway([self.reply(command,0,11,1),self.reply(command)])
            result=send_candidate_once(g,candidate)
            self.assertEqual(result['sequence'],0)
            self.assertTrue(result['waited_for_terminal_status'])
            self.assertEqual(len(g.sock.sent),1)
            self.assertEqual(g.sock.sent[0][0][40:42],b'\xff\xff')

    def test_nonzero_error_preserved_for_readback_not_claimed_success(self):
        g=self.gateway([self.reply(status=2)])
        result=send_candidate_once(g,self.candidate())
        self.assertEqual(result['terminal_status_byte'],2)
        self.assertFalse(result['status_interpreted'])
        self.assertTrue(result['readback_required'])

    def test_current_sequence_direct_terminal_supported(self):
        alias=self.route[3:]+bytes([self.route[0],53,self.route[2]])
        g=self.gateway([self.reply(status=1,sequence=11,transport=1,route=alias)])
        self.assertEqual(send_candidate_once(g,self.candidate())['sequence'],11)


if __name__=='__main__':unittest.main()
