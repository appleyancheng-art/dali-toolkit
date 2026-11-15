"""Names vary by project; version/MAC follow the wire terminator, not column13."""
import unittest
from dali_mcp.protocol import Gateway,ProtocolError,decode,encode,gateway_identity
from dali_mcp.gateway_discovery import parse_reply

ROUTE=bytes.fromhex('fffffe010203')
MAC=bytes.fromhex('020000000011')
NIC=dict(local_ip='192.168.77.10',prefix_length=24,interface_index=8)

def body(name,suffix=b''):
    return b'\x03'+name+b'\0'+bytes([1,6,9])+MAC+suffix


class GatewayIdentityTests(unittest.TestCase):
    def test_short_long_chinese_and_empty_names_preserve_identity(self):
        for name in ('A','KNX-DALI-APP','X'*31,'客厅网关','会议室DALI',''):
            with self.subTest(name=name):
                p=body(name.encode('gbk'),bytes([1,5,9])+bytes.fromhex('020000000022'))
                info=gateway_identity(p,ROUTE,'192.168.77.99')
                self.assertEqual(info['name'],name)
                self.assertEqual(info['dali_firmware'],'1.6.9')
                self.assertEqual(info['dali_mac'],'02:00:00:00:00:11')
                self.assertEqual(info['name_raw_hex'],name.encode('gbk').hex())
                self.assertTrue(info['name_decode_valid'])

    def test_bad_name_encoding_does_not_shift_identity(self):
        info=gateway_identity(body(b'\x81'),ROUTE,'192.168.77.99')
        self.assertFalse(info['name_decode_valid']);self.assertEqual(info['name_raw_hex'],'81')
        self.assertEqual(info['dali_mac'],'02:00:00:00:00:11')

    def test_missing_terminator_overlong_name_and_truncated_suffix_rejected(self):
        for payload in (b'\x03'+b'N'*40,body(b'X'*32),body(b'A')[:-1],bytes([3,0,1,6,9]),b''):
            with self.subTest(payload=payload),self.assertRaises(ProtocolError):gateway_identity(payload,ROUTE,'192.168.77.99')

    def test_broadcast_and_unicast_use_same_variable_layout(self):
        for name in (b'A','网关B'.encode('gbk'),b'Z'*31):
            payload=body(name)
            packet=decode(encode(0xfe81,payload,ROUTE,41,11211))
            wire=parse_reply(encode(0xfe81,payload,ROUTE,41,11211),('192.168.77.99',11211),41,NIC)
            gateway=object.__new__(Gateway);gateway.peer=('192.168.77.99',11211)
            def exchange(command,payload,route,accept,**kwargs):
                self.assertEqual(command,0xfe01);self.assertEqual(payload,b'');self.assertTrue(accept(packet));return packet
            gateway.exchange=exchange
            unicast=gateway.discover()
            self.assertEqual({k:wire[k] for k in unicast},unicast)
            self.assertEqual(gateway.device,bytes.fromhex('010203'))

    def test_zero_and_multicast_mac_not_valid_identity(self):
        for mac in (bytes(6),b'\xff'*6,bytes.fromhex('030000000011')):
            with self.subTest(mac=mac),self.assertRaises(ProtocolError):
                gateway_identity(b'\3A\0'+bytes([1,6,9])+mac,ROUTE,'192.168.77.99')


if __name__=='__main__':unittest.main(verbosity=2)
