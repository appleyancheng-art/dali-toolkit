"""Real UDP correlation tests; address writes are not MCP-exposed yet."""
import socket
import threading
import unittest
from dali_mcp.protocol import Gateway, ProtocolError, encode, decode


class AddressPeer:
    def __init__(self):
        self.sock=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
        self.sock.bind(('127.0.0.1',0));self.sock.settimeout(.05)
        self.port=self.sock.getsockname()[1]
        self.device=bytes.fromhex('0115f0');self.host=bytes.fromhex('cc35fe')
        self.requests=[];self.errors=[];self.stopped=False
        self.drop=False;self.noise=False;self.status=None;self.alias_final=False
        self.heartbeat=False;self.heartbeat_seen=False;self.pending=None
        self.thread=threading.Thread(target=self.run,daemon=True);self.thread.start()

    def send(self,command,payload,peer,seq=0,transport=2,route=None):
        self.sock.sendto(encode(command,payload,route or self.host+self.device,seq,self.port,transport=transport),peer)

    def run(self):
        while not self.stopped:
            try:data,peer=self.sock.recvfrom(2048)
            except socket.timeout:continue
            try:
                p=decode(data);self.requests.append(p)
                if p.command==0xfe01:
                    self.send(0xfe81,b'\x03KNX-DALI-APP\0'+bytes([1,6,9])+bytes.fromhex('020000000001'),peer,p.sequence,1)
                elif p.command==0xfe30:self.send(0xfeb0,b'\x00\x02',peer,p.sequence,1)
                elif p.command==0xfe85:
                    self.heartbeat_seen=True
                    if self.pending:
                        command,final,peer=self.pending;self.pending=None;self.send(command,final,peer)
                else:
                    assert p.command in (0xf001,0xf002)
                    if p.command==0xf002:
                        assert len(p.payload)==4 and data[34]==8 and p.payload[1]==1
                        status=(2 if p.payload[3]==255 else 1) if self.status is None else self.status
                        final=p.payload[:3]+bytes([status,p.payload[3]])
                        progress=p.payload[:3]+b'\0'+p.payload[3:]
                    else:
                        assert p.payload in (b'\x01\x02\0',b'\x02\x02\0') and data[34]==7
                        status=1 if self.status is None else self.status
                        final=p.payload[:2]+bytes([status])+b'\xff'*8
                        progress=p.payload[:2]+bytes(9)
                    command=p.command+0x80
                    alias=self.host+bytes([self.device[0],1,self.device[2]])
                    self.send(command,progress,peer,p.sequence,1,alias)
                    if self.noise:
                        self.send(command,bytes([3-p.payload[0]])+final[1:],peer)
                        self.send(command,final[:-1],peer)
                        self.send(command,final+b'\0',peer)
                        self.send(command,final,peer,(p.sequence+1)%65536,1)
                        self.send(command,final,peer,0,2,b'\x99'*6)
                        self.send(command,final,peer,p.sequence,1,alias)
                        wrong=bytearray(final);wrong[1]=0;self.send(command,bytes(wrong),peer)
                        if p.command==0xf002:
                            wrong=bytearray(final);wrong[2]=(wrong[2]+1)%64;self.send(command,bytes(wrong),peer)
                            wrong=bytearray(final);wrong[4]=(wrong[4]+1)%64;self.send(command,bytes(wrong),peer)
                    if self.drop:continue
                    if self.alias_final:self.send(command,final,peer,p.sequence,1,alias)
                    elif self.heartbeat:
                        self.pending=(command,final,peer);self.send(0xfe05,b'',peer,0x77,2)
                    else:self.send(command,final,peer)
            except BaseException as exc:self.errors.append(str(exc))

    def close(self):
        self.stopped=True;self.thread.join(1);self.sock.close()


class AddressProtocolTests(unittest.TestCase):
    def setUp(self):
        self.peer=AddressPeer()
        self.g=Gateway('127.0.0.1','127.0.0.1',port=0,gateway_port=self.peer.port,timeout=.2)

    def tearDown(self):
        self.g.close();self.peer.close();self.assertEqual(self.peer.errors,[])

    def test_remove63_exact_wire_and_result2(self):
        r=self.g.change_short_address(2,63,255,.2)
        self.assertEqual(r['raw_final_payload'],'02013f02ff');self.assertTrue(r['receipt_only'])
        self.assertEqual(self.peer.requests[-1].payload,b'\x02\x01\x3f\xff')

    def test_move_result1_and_scope(self):
        r=self.g.change_short_address(1,0,63,.2)
        self.assertEqual(r['result'],1);self.assertEqual(r['destination_address'],63)
        self.assertEqual(self.peer.requests[-1].payload,b'\x01\x01\x00\x3f')
        self.assertFalse(r['physical_identity_verified'])

    def test_incremental_mode2_exact_wire_and_bitmap(self):
        r=self.g.allocate_missing_addresses(2,.2)
        self.assertEqual(r['addresses'],list(range(64)));self.assertTrue(r['independent_scan_required'])
        self.assertEqual(self.peer.requests[-1].payload,b'\x02\x02\0')

    def test_wrong_move_result2_is_error_not_success(self):
        self.peer.status=2
        with self.assertRaisesRegex(ProtocolError,'expected1'):self.g.change_short_address(2,0,63,.2)

    def test_wrong_remove_result1_is_error_not_success(self):
        self.peer.status=1
        with self.assertRaisesRegex(ProtocolError,'expected2'):self.g.change_short_address(2,63,255,.2)

    def test_incremental_error_not_bitmap_success(self):
        self.peer.status=3
        with self.assertRaisesRegex(ProtocolError,'result3'):self.g.allocate_missing_addresses(2,.2)

    def test_edit_noise_filters_and_heartbeat(self):
        self.peer.noise=True;self.peer.heartbeat=True
        self.assertEqual(self.g.change_short_address(2,63,255,.3)['result'],2)
        self.assertTrue(self.peer.heartbeat_seen)

    def test_incremental_noise_filters_and_heartbeat(self):
        self.peer.noise=True;self.peer.heartbeat=True
        self.assertEqual(self.g.allocate_missing_addresses(1,.3)['count'],64)
        self.assertTrue(self.peer.heartbeat_seen)

    def test_edit_missing_final_never_replays(self):
        self.peer.drop=True;self.peer.noise=True
        with self.assertRaises(TimeoutError):self.g.change_short_address(2,63,255,.1)
        self.assertIsNone(self.g.route)
        self.assertEqual(sum(p.command==0xf002 for p in self.peer.requests),1)

    def test_incremental_missing_final_never_replays(self):
        self.peer.drop=True;self.peer.noise=True
        with self.assertRaises(TimeoutError):self.g.allocate_missing_addresses(2,.1)
        self.assertIsNone(self.g.route)
        self.assertEqual(sum(p.command==0xf001 for p in self.peer.requests),1)

    def test_alias_positive_is_not_observed_final_format(self):
        self.peer.alias_final=True
        with self.assertRaises(TimeoutError):self.g.change_short_address(2,63,255,.1)

    def test_invalid_scope_rejected_before_any_packet(self):
        for ch,src,dst in [(0,0,1),(3,0,1),(True,0,1),(1,-1,1),(1,64,1),(1,False,1),
                           (1,0,0),(1,0,64),(1,0,254),(1,0,256),(1,0,True),(1,0,'63')]:
            with self.assertRaises(ValueError):self.g.change_short_address(ch,src,dst,.2)
        for ch in (0,3,True,1.0,'2',None):
            with self.assertRaises(ValueError):self.g.allocate_missing_addresses(ch,.2)
        for t in (True,0,.01,301,float('nan'),float('inf'),'1'):
            with self.assertRaises(ValueError):self.g.allocate_missing_addresses(2,t)
            with self.assertRaises(ValueError):self.g.change_short_address(2,0,63,t)
        self.assertEqual(self.peer.requests,[])


if __name__=='__main__':unittest.main()
