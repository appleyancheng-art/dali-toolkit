import copy
import socket
import subprocess
import unittest
from types import SimpleNamespace
from dali_mcp.gateway_discovery import search_gateways,parse_reply,verify_connection,windows_interfaces
from dali_mcp.protocol import encode,decode,ProtocolError

NIC=dict(local_ip='192.168.77.10',prefix_length=24,interface_index=8,name='Ethernet')
PEER=('192.168.77.99',11211)


def reply(seq=24,mac='020000000011',route='fffffe0115f0',name=b'KNX-DALI-APP'):
    payload=b'\x03'+name+b'\0'+bytes([1,6,9])+bytes.fromhex(mac)
    return encode(0xfe81,payload,bytes.fromhex(route),seq,11211,mode=0x42)


class FakeSocket:
    def __init__(self,packets=(),error=None):
        self.packets=list(packets);self.error=error;self.sent=[];self.closed=False;self.bound=None;self.options=[]
    def setsockopt(self,*args):self.options.append(args)
    def bind(self,address):
        if self.error:raise self.error
        self.bound=address
    def getsockname(self):return self.bound[0],30431
    def sendto(self,data,target):self.sent.append((data,target))
    def settimeout(self,value):pass
    def recvfrom(self,size):
        if not self.packets:raise socket.timeout()
        return self.packets.pop(0)
    def close(self):self.closed=True


class DiscoveryTests(unittest.TestCase):
    def search(self,socks,nics=None,**kwargs):
        it=iter(socks)
        return search_gateways(nics or [NIC],socket_factory=lambda *args:next(it),sequence_factory=lambda:24,**kwargs)

    def test_multiple_responses_and_dedup_no_selection_or_connect(self):
        sock=FakeSocket([(reply(),PEER),(reply(),PEER),
            (reply(mac='020000000012'),('192.168.77.22',11211))])
        result=self.search([sock])
        self.assertEqual(len(result['gateways']),2)
        self.assertIsNone(result['selected_gateway'])
        self.assertTrue(sock.closed)
        self.assertEqual(len(sock.sent),1)
        data,target=sock.sent[0]
        self.assertEqual(target,('255.255.255.255',11211))
        packet=decode(data)
        self.assertEqual((packet.command,packet.payload,packet.sequence),(0xfe01,b'',24))
        self.assertEqual(int.from_bytes(data[19:21],'little'),30431)
        self.assertFalse(any(x['connection_tested'] for x in result['gateways']))

    def test_invalid_datagrams_do_not_pollute_results(self):
        corrupt=bytearray(reply());corrupt[-1]^=1
        packets=[(bytes(corrupt),PEER),(reply(seq=25),PEER),
            (reply(),('192.168.77.99',1234)),(reply(),('192.168.88.99',11211)),
            (reply(route='0115f0fffffe'),PEER),(b'noise',PEER),
            (reply(mac='000000000000'),PEER),(reply(mac='030000000011'),PEER),
            (reply(),PEER)]
        r=self.search([FakeSocket(packets)])
        self.assertEqual(len(r['gateways']),1)
        self.assertEqual(r['interfaces'][0]['ignored'],8)

    def test_same_identity_two_ips_conflict(self):
        r=self.search([FakeSocket([(reply(),PEER),(reply(),('192.168.77.20',11211))])])
        self.assertTrue(all(g['identity_conflict'] for g in r['gateways']))

    def test_same_ip_two_identities_conflict(self):
        r=self.search([FakeSocket([(reply(),PEER),(reply(mac='020000000012'),PEER)])])
        self.assertTrue(all(g['identity_conflict'] for g in r['gateways']))

    def test_changed_firmware_or_name_for_same_endpoint_conflict(self):
        r=self.search([FakeSocket([(reply(),PEER),(reply(name=b'OTHER'),PEER)])])
        self.assertTrue(r['gateways'][0]['identity_conflict'])

    def test_one_interface_failed_other_retained(self):
        broken=FakeSocket(error=OSError('bind unavailable'))
        other=dict(NIC,local_ip='192.168.88.10',interface_index=9)
        r=self.search([broken,FakeSocket([(reply(),('192.168.88.99',11211))])],[NIC,other])
        self.assertEqual(r['state'],'partial')
        self.assertTrue(broken.closed)
        self.assertEqual(r['gateways'][0]['local_ip'],'192.168.88.10')

    def test_empty_search_is_complete_but_not_proof_offline(self):
        r=self.search([FakeSocket()])
        self.assertEqual(r['state'],'completed');self.assertEqual(r['gateways'],[])
        self.assertIn('not proof',r['coverage'])

    def test_invalid_inputs_fail_before_socket(self):
        def no_socket(*args):raise AssertionError('must not create socket')
        for nics,seconds in [([],2),([NIC,NIC],2),([dict(NIC,prefix_length=32)],2),
            ([dict(NIC,local_ip='127.0.0.1')],2),([NIC],True),([NIC],float('nan')),([NIC],11)]:
            with self.subTest(nics=nics,seconds=seconds),self.assertRaises(ValueError):
                search_gateways(nics,seconds,socket_factory=no_socket)

    def test_datagram_flood_bounded_and_socket_closed(self):
        sock=FakeSocket([(b'bad',PEER)]*4100)
        r=self.search([sock]);self.assertEqual(r['state'],'failed')
        self.assertEqual(r['interfaces'][0]['ignored'],4096);self.assertTrue(sock.closed)

    def test_enumeration_normalizes_single_result_and_skips_nonbroadcast(self):
        calls=[]
        def run(args,**kwargs):
            calls.append((args,kwargs))
            return SimpleNamespace(stdout='{"IPAddress":"10.2.3.4","PrefixLength":24,"InterfaceIndex":2,"InterfaceAlias":"Ethernet"}')
        r=windows_interfaces(run);self.assertEqual(r[0]['broadcast'],'10.2.3.255')
        self.assertIs(calls[0][1]['check'],True)
        self.assertEqual(windows_interfaces(lambda *a,**k:SimpleNamespace(stdout='[]')),[])

    def test_enumeration_failure_is_not_reported_as_empty(self):
        def failed(*args,**kwargs):raise subprocess.TimeoutExpired('powershell',15)
        with self.assertRaisesRegex(RuntimeError,'timed out'):windows_interfaces(failed)

    def test_connection_checks_identity_before_handshake_and_closes(self):
        candidate=parse_reply(reply(),PEER,24,NIC)
        class FakeGateway:
            def __init__(self,*args,**kwargs):self.connected=False;self.closed=False
            def discover(self):return copy.deepcopy(candidate)
            def connect(self):self.connected=True;return dict(channels=2,route='abc')
            def close(self):self.closed=True
        gateway=FakeGateway()
        r=verify_connection(candidate,gateway_factory=lambda *a,**k:gateway)
        self.assertEqual(r['state'],'connected');self.assertTrue(gateway.closed)
        self.assertFalse(r['configuration_changed']);self.assertFalse(r['lighting_commands_sent'])
        bad=dict(candidate,dali_mac='02:00:00:00:00:12');gateway=FakeGateway()
        with self.assertRaises(RuntimeError):verify_connection(bad,gateway_factory=lambda *a,**k:gateway)
        self.assertFalse(gateway.connected);self.assertTrue(gateway.closed)

    def test_ambiguous_candidate_never_connects(self):
        with self.assertRaises(ValueError):verify_connection(dict(identity_conflict=True),gateway_factory=lambda *a,**k:self.fail('no socket'))


if __name__=='__main__':unittest.main(verbosity=2)
