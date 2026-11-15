import copy
import json
import socket
import struct
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from pathlib import Path
from dali_mcp.protocol import Gateway, ProtocolError, decode, encode
from dali_mcp.service import Service

class Emulator:
    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(.05)
        self.port = self.sock.getsockname()[1]
        self.stopped = False
        self.device = bytes.fromhex("0115f0")
        self.host = bytes.fromhex("cc35fe")
        self.values = {}
        self.writes = []
        self.drop_write = False
        self.inject_noise = False
        self.quantize = False
        self.mismatch = False
        self.heartbeat_gate = False
        self.pending = None
        self.recalls=[]
        self.recall_receipt='echo'
        self.ignore_recall=False
        self.drop_actual_after_recall=False
        self.live={}
        self.actual_alias=False
        self.drop_actual=False
        self.drop_actual_after_live=False
        self.live_commands=[]
        self.extreme_commands=[]
        self.drop_extreme=False
        self.drop_live=False
        self.profile_queries=0
        self.fade_live_seconds=0
        self.pending_color={}
        self.interrupted_color=0
        self.requests=[]
        self.request_packets=[]
        self.parameter_drop=None
        self.parameter_status=1
        self.lamp_type=1
        self.curve_code=0
        self.parameter_values={0xf016:b'\xa5\0\0\0',0xf01d:b'\x03\0\0\0',
                               0xf01f:b'\xf0\0\0\0',0xf021:b'\x4d\x34\x12\x99',
                               0xf023:b'\xff\xff\xff\x99'}
        self.scan_addresses=[0,7,8,31,32,63]
        self.scans=[]
        self.drop_scan=False
        self.scan_status=1
        self.scan_noise=False
        self.scan_alias=False
        self.scan_requires_reconnect=False
        self.scan_session_finished=False
        self.duplicate_addresses=[0,7,8,31,32,63]
        self.duplicate_status=1
        self.duplicate_alias=False
        self.duplicate_noise=False
        self.drop_duplicate=False
        self.group_masks={}
        self.group_queries=[]
        self.drop_group_address=None
        self.group_status=1
        self.fade_code=4
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def stop(self):
        self.stopped = True
        self.thread.join(1)
        self.sock.close()

    def send(self, command, payload, sequence, peer, asynchronous=False):
        raw = bytearray(encode(command, payload, self.host+self.device, sequence, self.port))
        raw[13] = 1
        raw[17] = 2 if asynchronous else 1
        self.sock.sendto(raw+b"\0", peer)

    def run(self):
        while not self.stopped:
            try:
                data, peer = self.sock.recvfrom(2048)
            except socket.timeout:
                continue
            p = decode(data)
            self.requests.append(p.command)
            self.request_packets.append(p)
            if p.command==self.parameter_drop:continue
            if p.command == 0xfe01:
                payload = b"\x03KNX-DALI-APP\0" + bytes([1,6,9]) + bytes.fromhex("020000000001") + bytes(8)
                self.send(0xfe81, payload, p.sequence, peer)
            elif p.command == 0xfe30:
                self.scan_session_finished=False
                self.send(0xfeb0, b"\0\2", p.sequence, peer)
            elif p.command == 0xf001:
                self.scans.append(p.payload)
                self.scan_session_finished=True
                assert p.payload in (bytes([1,0,0]),bytes([2,0,0])), 'Only the captured search mode and wire length'
                assert data[34]==7, 'Declared length includes CRC'
                bitmap=sum(1 << a for a in self.scan_addresses).to_bytes(8,'little')
                b=p.payload[:2]+bytes([self.scan_status])+bitmap
                self.send(0xf081,p.payload[:2]+bytes(9),p.sequence,peer)
                if self.scan_noise:
                    self.send(0xf081,bytes([3-p.payload[0],0,1])+bitmap,0,peer,True)
                    self.send(0xf081,bytes([p.payload[0],2,1])+bitmap,0,peer,True)
                    self.send(0xf081,b[:-1],0,peer,True)
                    self.sock.sendto(encode(0xf081,b,b'\x99'*6,p.sequence,self.port),peer)
                if self.drop_scan:continue
                if self.scan_alias:
                    alias=self.host+bytes([self.device[0],int(peer[0].split('.')[-1]),self.device[2]])
                    self.sock.sendto(encode(0xf081,b,alias,p.sequence,self.port),peer)
                elif self.heartbeat_gate:
                    self.pending=(0xf081,b,peer)
                    self.send(0xfe05,b'',0x77,peer)
                else:self.send(0xf081,b,0,peer,True)
            elif p.command == 0xf043:
                assert p.payload in (b'\x01',b'\x02') and data[34]==5
                bitmap=sum(1<<a for a in self.duplicate_addresses).to_bytes(8,'little')
                reply=p.payload+bytes([self.duplicate_status])+bitmap
                self.send(0xf0c3,p.payload+bytes(9),p.sequence,peer)
                alias=self.host+bytes([self.device[0],int(peer[0].split('.')[-1]),self.device[2]])
                if self.duplicate_noise:
                    self.send(0xf0c3,bytes([3-p.payload[0],1])+bitmap,p.sequence,peer)
                    self.send(0xf0c3,reply[:-1],p.sequence,peer)
                    self.sock.sendto(encode(0xf0c3,reply,b'\x99'*6,p.sequence,self.port),peer)
                    self.sock.sendto(encode(0xf0c3,reply,alias,p.sequence+1,self.port),peer)
                    self.sock.sendto(encode(0xf0c3,reply,alias,0,self.port,transport=2),peer)
                if self.drop_duplicate:continue
                if self.duplicate_alias:
                    self.sock.sendto(encode(0xf0c3,reply,alias,p.sequence,self.port),peer)
                elif self.heartbeat_gate:
                    self.pending=(0xf0c3,reply,peer);self.send(0xfe05,b'',0x77,peer)
                else:self.send(0xf0c3,reply,0,peer,True)
            elif p.command == 0xf032:
                channel,mode,address=p.payload[:3]
                assert len(p.payload)==8 and data[34]==12 and mode==1 and p.payload[3:]==b'\xc0\xc1'+bytes(3)
                self.group_queries.append((channel,address))
                mask=self.group_masks.get((channel,address),0)
                b=p.payload[:3]+bytes([self.group_status])+mask.to_bytes(2,'little')+bytes(2)
                self.send(0xf0b2,p.payload[:3]+bytes(5),p.sequence,peer)
                if self.scan_requires_reconnect and self.scan_session_finished:continue
                if self.drop_group_address==address:continue
                if self.inject_noise:
                    wrong=bytes([channel,1,(address+1)%64])+b[3:]
                    self.send(0xf0b2,wrong,0,peer,True)
                self.send(0xf0b2,b,0,peer,True)
            elif p.command == 0xfe85:
                if p.transport == 2 and p.sequence == 0x77 and self.pending:
                    command,b,reply_peer = self.pending
                    self.pending = None
                    self.send(command,b,0,reply_peer,True)
            elif p.command == 0xf012:
                b=p.payload
                assert len(b)==8 and b[0] in (1,2) and b[1]==1 and b[2]<64 and b[3] in (5,6) and b[4:]==bytes(4)
                self.extreme_commands.append(b)
                level=self.parameter_values[0xf01f if b[3]==5 else 0xf01d][0]
                self.live[b[0],b[2]]=(level,self.live.get((b[0],b[2]),(0,4000))[1])
                if not self.drop_extreme:self.send(0xf092,b[:3]+bytes(5),p.sequence,peer)
            elif p.command == 0xf013:
                self.recalls.append(p.payload)
                channel,mode,address,action,scene=p.payload[:5]
                assert len(p.payload)==8 and p.payload[5:]==bytes(3)
                assert (mode==0 and address==255 or mode==1 and 0<=address<=63 or mode==2 and 0<=address<=15) and action==0x10
                if not self.ignore_recall:
                    targets=(range(64) if mode==0 else [address] if mode==1 else [a for a in range(64) if self.group_masks.get((channel,a),0)&(1<<address)])
                    for a in targets:
                        level,kelvin=self.values.get((channel,a,scene),(254,4000))
                        previous=self.live.get((channel,a),(0,2702))
                        self.live[(channel,a)]=(previous[0] if level==255 else level,previous[1] if kelvin==65535 else kelvin)
                b=bytearray(p.payload);b[3]=1
                if self.recall_receipt=='echo':self.send(0xf093,b,p.sequence,peer)
                elif self.recall_receipt in ('captured','stale','wrong_route'):
                    alias=self.host+bytes([self.device[0],int(peer[0].split('.')[-1]),self.device[2]])
                    if self.recall_receipt=='wrong_route':alias=self.host+b'\x99\x99\x99'
                    seq=p.sequence if self.recall_receipt!='stale' else p.sequence-1
                    self.sock.sendto(encode(0xf093,p.payload[:3]+bytes(5),alias,seq,self.port),peer)
            elif p.command in (0xf02b,0xf02c):
                self.live_commands.append((p.command,p.payload))
                b=p.payload
                assert len(b)==8 and b[1] in (0,1,2) and b[3:5]==b'\0\0'
                channel=b[0]
                assert b[1]!=0 or b[2]==255
                assert b[1]!=2 or 0<=b[2]<=15
                targets=(range(64) if b[1]==0 else [a for a in range(64) if self.group_masks.get((channel,a),0)&(1<<b[2])] if b[1]==2 else [b[2]])
                for address in targets:
                    level,kelvin=self.live.get((channel,address),(0,2702))
                    if p.command==0xf02b:
                        assert b[6:]==b'\0\0'
                        pending=self.pending_color.pop((channel,address),None)
                        if pending:
                            if time.monotonic()<pending[0]:self.interrupted_color+=1
                            else:kelvin=pending[1]
                        level=b[5]
                    else:
                        assert b[7]==0
                        target=1000000//(1000000//int.from_bytes(b[5:7],'little'))
                        if self.fade_live_seconds:self.pending_color[(channel,address)]=(time.monotonic()+self.fade_live_seconds,target)
                        else:kelvin=target
                    self.live[(channel,address)]=(level,kelvin)
                if not self.drop_live:self.send(p.command+0x80,b,p.sequence,peer)
            elif p.command == 0xf047:
                if self.drop_actual or (self.drop_actual_after_live and self.live_commands) or (self.drop_actual_after_recall and self.recalls):continue
                channel,address=p.payload[:2]
                level,kelvin=self.live.get((channel,address),(0,2702))
                pending=self.pending_color.get((channel,address))
                if pending and time.monotonic()>=pending[0]:
                    kelvin=pending[1]
                    self.live[(channel,address)]=(level,kelvin)
                    del self.pending_color[(channel,address)]
                b=bytes([channel,1,1,level])+struct.pack('<H',kelvin)+bytes(3)
                if self.actual_alias:
                    route=self.host+bytes([self.device[0],int(peer[0].split('.')[-1]),self.device[2]])
                    self.sock.sendto(encode(0xf0c7,b,route,p.sequence,self.port),peer)
                else:self.send(0xf0c7,b,0,peer,True)
            elif p.command in self.parameter_values:
                argument={0xf016:0x90,0xf01d:0xa2,0xf01f:0xa1,
                          0xf021:self.lamp_type,0xf023:self.lamp_type}[p.command]
                assert len(p.payload)==8 and data[34]==12
                assert p.payload[1]==1 and p.payload[3:]==bytes([argument,0,0,0,0])
                b=p.payload[:3]+bytes([self.parameter_status])+self.parameter_values[p.command]
                self.send(p.command+0x80,p.payload[:3]+bytes(5),p.sequence,peer)
                if self.inject_noise:
                    self.send(p.command+0x80,b[:-1],0,peer,True)
                    self.send(p.command+0x80,bytes([3-p.payload[0]])+b[1:],0,peer,True)
                self.send(p.command+0x80,b,0,peer,True)
            elif p.command==0xf01a:
                assert p.payload[3]==0xa5
                self.send(0xf09a,p.payload[:3]+bytes([1,self.fade_code*16+7,0,0,0]),0,peer,True)
            elif p.command in (0xf025,0xf027,0xf029):
                self.profile_queries+=1
                if p.command==0xf029:
                    assert len(p.payload)==4 and p.payload[3]==0xfa
                if p.command==0xf025:assert p.payload[3]==0xee
                value={0xf025:self.curve_code,0xf027:6535,0xf029:2702}[p.command]
                b=p.payload[:3]+b'\1'+struct.pack('<H',value)+b'\0\0'
                self.send(p.command+0x80,b,0,peer,True)
            else:
                b = bytearray(p.payload)
                key = (b[0], b[2], b[4])
                if p.command == 0xf039:
                    self.writes.append(bytes(b))
                    self.values[key] = (b[5], int.from_bytes(b[6:8], "little"))
                    if self.quantize:
                        level, kelvin = self.values[key]
                        self.values[key] = (level, 1000000 // (1000000 // kelvin))
                    if self.mismatch:
                        self.values[key] = (10,4000)
                    if self.drop_write:
                        continue
                receipt = bytearray(b)
                receipt[3] = 0
                self.send(p.command+0x80, receipt, p.sequence, peer)
                b[3] = 1
                if p.command == 0xf035:
                    b[4:] = bytes([self.lamp_type,0,0,0])
                else:
                    level, kelvin = self.values.get(key, (254,4000))
                    b[5] = level
                    b[6:8] = struct.pack("<H", kelvin)
                if self.inject_noise:
                    bad = bytearray(encode(p.command+0x80, b, self.host+self.device, 0, self.port))
                    bad[17] = 2
                    bad[-1] ^= 1
                    self.sock.sendto(bad, peer)
                    wrong = bytearray(b)
                    wrong[2] = (wrong[2]+1) % 64
                    self.send(p.command+0x80, wrong, 0, peer, True)
                if self.heartbeat_gate:
                    self.pending=(p.command+0x80,b,peer)
                    self.send(0xfe05,b"",0x77,peer)
                else:
                    self.send(p.command+0x80, b, 0, peer, True)

class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.em = Emulator()
        self.tmp = tempfile.TemporaryDirectory()
        self.config = {"gateway_ip":"127.0.0.1", "expected_dali_mac":"02:00:00:00:00:01",
                       "expected_firmware":"1.6.9", "enable_experimental_writes":True}
        self.service = Service(self.config, self.tmp.name, lambda: Gateway("127.0.0.1","127.0.0.1",0,.3,self.em.port))

    def tearDown(self):
        self.em.stop()
        self.tmp.cleanup()

    def changes(self):
        return [{"channel":1,"address":a,"scene":0,"level_raw":200,"kelvin_raw":3000} for a in (0,1)]

    def test_identify_off_lamp_configured_max_then_restore_and_persist(self):
        self.config['enable_experimental_writes']=False
        self.em.live[(2,63)]=(0,4000)
        self.em.live[(2,0)]=(90,3003)
        self.em.values[(2,63,8)]=(180,5000)
        with patch.object(self.service,'_identification_hold'):
            result=self.service.identify_lamp(2,63)
        self.assertEqual(result['state'],'completed')
        self.assertTrue(result['restore_verified'] and result['effect_verified'])
        self.assertEqual([p[1][5] for p in self.em.live_commands],[240,0])
        self.assertTrue(all(c==0xf02b and b[:3]==bytes([2,1,63]) for c,b in self.em.live_commands))
        self.assertEqual(self.em.live[(2,63)],(0,4000))
        self.assertEqual(self.em.live[(2,0)],(90,3003))
        self.assertEqual(self.em.values[(2,63,8)],(180,5000))
        self.assertFalse(self.em.writes or self.em.recalls or self.em.scans)
        self.assertEqual(self.service.get_identification(result['operation_id']),result)
        self.assertEqual(self.service.get_identification()['operations'][0],result)
        with self.assertRaises(ValueError):self.service.get_identification('../config')

    def test_identify_lit_lamp_has_dark_phase_and_restores_exact_level(self):
        self.em.live[(1,0)]=(190,5000)
        with patch.object(self.service,'_identification_hold'):
            result=self.service.identify_lamp(1,0,1)
        self.assertEqual([p[1][5] for p in self.em.live_commands],[0,240,190])
        self.assertEqual(self.em.live[(1,0)],(190,5000))
        self.assertTrue(result['restore_verified'])

    def test_identify_uncertain_receipt_stops_and_restores_without_retry(self):
        self.em.drop_live=True
        with patch.object(self.service,'_identification_hold'):
            result=self.service.identify_lamp(2,0)
        self.assertEqual(result['state'],'stopped_restored')
        self.assertTrue(result['restore_verified'])
        self.assertFalse(result['all_receipts_received'])
        self.assertEqual([p[1][5] for p in self.em.live_commands],[240,0])
        self.assertEqual(result['commands'][0]['receipt_status'],'unknown')
        self.assertIn('dispatch_error',result['commands'][0])

    def test_identify_read_failure_does_not_claim_restoration_or_repeat(self):
        self.em.drop_actual_after_live=True
        with patch.object(self.service,'_identification_hold'):
            result=self.service.identify_lamp(2,0)
        self.assertEqual(result['state'],'restore_unverified')
        self.assertFalse(result['restore_verified'])
        self.assertIn('restore_error',result)
        self.assertEqual([p[1][5] for p in self.em.live_commands],[240])
        self.assertEqual(self.em.live[(2,0)][0],240)
        self.assertEqual(self.service.get_identification(result['operation_id'])['original']['level_raw'],0)

    def test_identify_interruption_restores_without_more_identifying(self):
        with patch.object(self.service,'_identification_hold',side_effect=[None,KeyboardInterrupt()]):
            result=self.service.identify_lamp(2,0)
        self.assertEqual(result['state'],'stopped_restored')
        self.assertEqual(result['error'],'KeyboardInterrupt')
        self.assertTrue(result['restore_verified'])
        self.assertEqual([p[1][5] for p in self.em.live_commands],[240,0])

    def test_identify_disk_failure_after_send_still_attempts_restore(self):
        original_save=self.service._save
        def failing_save(*args,**kwargs):
            if self.em.live_commands:raise OSError('simulated disk failure')
            return original_save(*args,**kwargs)
        with patch.object(self.service,'_identification_hold'),patch.object(self.service,'_save',side_effect=failing_save):
            result=self.service.identify_lamp(2,0)
        self.assertEqual(result['state'],'stopped_restored')
        self.assertTrue(result['restore_verified'])
        self.assertTrue(result['journal_errors'])
        self.assertEqual([p[1][5] for p in self.em.live_commands],[240,0])
        saved=self.service.get_identification(result['operation_id'])
        self.assertEqual(saved['original']['level_raw'],0)
        self.assertEqual(saved['commands'][0]['receipt_status'],'unknown')

    def test_identify_does_not_restore_color_if_changed_elsewhere(self):
        def hold(seconds):
            if self.em.live_commands:self.em.live[(2,0)]=(240,5000)
        with patch.object(self.service,'_identification_hold',side_effect=hold):
            result=self.service.identify_lamp(2,0)
        self.assertEqual(result['state'],'restore_unverified')
        self.assertTrue(result['brightness_restored'])
        self.assertFalse(result['kelvin_unchanged'])
        self.assertTrue(all(c==0xf02b for c,b in self.em.live_commands))
        self.assertEqual(self.em.live[(2,0)],(0,5000))

    def test_identify_preflight_refuses_unknown_long_fade_and_changing_output(self):
        for fade in (0,8):
            self.em.fade_code=fade
            with patch.object(self.service,'_identification_hold'):
                result=self.service.identify_lamp(2,0)
            self.assertEqual(result['state'],'preflight_failed')
            self.assertFalse(self.em.live_commands)
        self.em.fade_code=4
        def external_change(seconds):self.em.live[(2,0)]=(100,2702)
        with patch.object(self.service,'_identification_hold',side_effect=external_change):
            result=self.service.identify_lamp(2,0)
        self.assertEqual(result['state'],'preflight_failed')
        self.assertIn('changing',result['error'])
        self.assertFalse(result['restoration_needed'] or self.em.live_commands)

    def test_identify_scope_identity_and_type_rejected_before_output(self):
        for ch,addr,hold in [(True,0,2),(2,True,2),(0,0,2),(2,64,2),(2,0,True),(2,0,0),(2,0,6)]:
            with self.assertRaises(ValueError):self.service.identify_lamp(ch,addr,hold)
        self.assertEqual(self.em.requests,[])
        self.config['expected_dali_mac']='00:00:00:00:00:00'
        self.assertEqual(self.service.identify_lamp(2,0)['state'],'preflight_failed')
        self.assertEqual(self.em.requests,[0xfe01])
        self.config['expected_dali_mac']='02:00:00:00:00:01'
        self.em.lamp_type=0
        self.assertEqual(self.service.identify_lamp(2,0)['state'],'preflight_failed')
        self.assertFalse(self.em.live_commands)

    def test_identify_unknown_live_color_refuses_before_output(self):
        for kelvin in (0,65535):
            self.em.live[(2,0)]=(0,kelvin)
            with patch.object(self.service,'_identification_hold'):
                result=self.service.identify_lamp(2,0)
            self.assertEqual(result['state'],'preflight_failed')
            self.assertIn('unknown or a sentinel',result['error'])
            self.assertFalse(self.em.live_commands)

    def test_parameters_fresh_offsets_and_sentinels_no_device_writes(self):
        self.em.inject_noise=True
        self.em.group_masks[(2,63)]=0x8001
        self.config['enable_experimental_writes']=False
        result=self.service.read_lamp_parameters(2,[0,63])
        self.assertEqual(result['state'],'completed')
        p=result['rows'][1]['parameters']
        self.assertEqual(p['power_on']['value'],{'level_raw':77,'kelvin_raw':4660})
        self.assertEqual(p['system_failure']['value'],{'level_raw':255,'kelvin_raw':65535})
        self.assertEqual(p['status']['value'],{'raw':165})
        self.assertEqual(p['min_level']['value'],{'level_raw':3})
        self.assertEqual(p['max_level']['value'],{'level_raw':240})
        self.assertEqual(p['fade_time_rate']['value']['fade_time_code'],4)
        self.assertEqual(p['fade_time_rate']['value']['fade_rate_code'],7)
        self.assertEqual(p['configured_min_ct']['value'],{'kelvin_raw':2702})
        self.assertEqual(p['configured_max_ct']['value'],{'kelvin_raw':6535})
        self.assertEqual(p['groups']['value'],{'groups':[0,15],'group_mask':0x8001})
        self.assertTrue(all('captured_at' in v for v in p.values()))
        self.assertFalse(result['atomic_snapshot'])
        self.assertFalse(self.em.writes or self.em.live_commands or self.em.recalls or self.em.scans)
        self.assertEqual(set(self.em.requests),{0xfe01,0xfe30,0xf035,0xf016,0xf01a,0xf01d,0xf01f,0xf021,0xf023,0xf025,0xf029,0xf027,0xf032})
        self.em.parameter_values[0xf01d]=b'\x11\0\0\0'
        fresh=self.service.read_lamp_parameters(2,[63])
        self.assertEqual(fresh['rows'][0]['parameters']['min_level']['value']['level_raw'],17)

    def test_parameters_partial_failure_retains_prior_fields_no_retry(self):
        self.em.parameter_drop=0xf021
        result=self.service.read_lamp_parameters(2,[0,63])
        self.assertEqual(result['state'],'stopped_partial')
        self.assertEqual(result['pending'],{'address':0,'field':'power_on'})
        self.assertEqual(len(result['rows']),1)
        self.assertEqual(result['rows'][0]['state'],'partial')
        p=result['rows'][0]['parameters']
        self.assertIn('max_level',p)
        self.assertNotIn('power_on',p)
        self.assertEqual(self.em.requests.count(0xf021),1)
        self.assertFalse(any(p.payload[2]==63 for p in self.em.request_packets if len(p.payload)>=3))
        saved=json.loads((Path(self.tmp.name)/f"parameters-{result['snapshot_id']}.json").read_text())
        self.assertEqual(saved,result)

    def test_parameters_status_error_is_not_zero_value(self):
        self.em.parameter_status=2
        result=self.service.read_lamp_parameters(1,[5])
        self.assertEqual(result['pending'],{'address':5,'field':'status'})
        self.assertEqual(result['state'],'stopped_partial')
        self.assertEqual(set(result['rows'][0]['parameters']),{'light_type'})
        self.assertIn('status 2',result['error'])

    def test_parameters_dt6_skips_ct_and_does_not_invent_zero_kelvin(self):
        self.em.lamp_type=0
        self.em.fade_code=0
        result=self.service.read_lamp_parameters(1,[0])
        self.assertEqual(result['state'],'completed')
        p=result['rows'][0]['parameters']
        self.assertEqual(p['light_type']['value'],{'code':0,'name':'DT6'})
        self.assertEqual(p['power_on']['value'],{'level_raw':77})
        self.assertEqual(p['configured_min_ct']['state'],'not_applicable')
        self.assertNotIn(0xf029,self.em.requests)
        self.assertNotIn(0xf027,self.em.requests)
        self.assertTrue(p['fade_time_rate']['value']['extended_fade_not_read'])

    def test_parameters_invalid_scope_before_network_and_unknown_type_stops(self):
        for channel,addresses in [(0,[0]),(True,[0]),(2,[]),(2,[64]),(2,[True]),(2,[0,0]),(2,list(range(17)))]:
            with self.assertRaises(ValueError):self.service.read_lamp_parameters(channel,addresses)
        self.assertFalse(self.em.requests)
        self.em.lamp_type=99
        result=self.service.read_lamp_parameters(2,[0])
        self.assertEqual(result['pending']['field'],'light_type')
        self.assertEqual(result['rows'][0]['parameters'],{})
        self.assertEqual(self.em.requests,[0xfe01,0xfe30,0xf035])

    def test_parameters_gateway_identity_checked_before_lamp_read(self):
        self.config['expected_dali_mac']='00:00:00:00:00:00'
        with self.assertRaisesRegex(RuntimeError,'identity mismatch'):
            self.service.read_lamp_parameters(2,[0])
        self.assertEqual(self.em.requests,[0xfe01])

    def test_scan_existing_bitmap_boundaries_without_mutations(self):
        self.em.scan_noise=True
        self.em.heartbeat_gate=True
        self.em.values[(2,0,8)]=(254,6000)
        self.em.live[(2,0)]=(0,3003)
        before=(dict(self.em.values),dict(self.em.live))
        result=self.service.scan_existing(2)
        self.assertEqual(result['state'],'completed')
        self.assertEqual(result['addresses'],[0,7,8,31,32,63])
        self.assertEqual(result['count'],6)
        self.assertEqual(self.em.scans,[bytes([2,0,0])])
        self.assertEqual(before,(self.em.values,self.em.live))
        self.assertFalse(self.em.writes or self.em.live_commands or self.em.recalls)
        self.assertEqual(set(self.em.requests),{0xfe01,0xfe30,0xf001,0xfe85})

    def test_scan_empty_is_completed_but_timeout_is_unknown_not_offline(self):
        self.em.scan_addresses=[]
        result=self.service.scan_existing(1)
        self.assertEqual(result['addresses'],[])
        self.assertEqual(result['state'],'completed')
        self.em.drop_scan=True
        self.config['scan_timeout_seconds']=.1
        result=self.service.scan_existing(1)
        self.assertEqual(result['state'],'scan_unknown')
        self.assertNotIn('addresses',result)
        self.assertNotIn('count',result)
        self.assertEqual(len(self.em.scans),2)  # One send per separate request, no retry.
        persisted=json.loads((Path(self.tmp.name)/f"scan-{result['operation_id']}.json").read_text())
        self.assertEqual(persisted['state'],'scan_unknown')

    def test_scan_status_error_preserved_and_invalid_scope_sends_nothing(self):
        for channel in (0,3,True,'2'):
            with self.assertRaises(ValueError):self.service.scan_existing(channel)
        self.assertEqual(self.em.requests,[])
        self.em.scan_status=3
        result=self.service.scan_existing(2)
        self.assertEqual(result['state'],'scan_unknown')
        self.assertIn('status 3',result['error'])
        self.assertNotIn('addresses',result)
        self.assertEqual(len(self.em.scans),1)

    def test_scan_gateway_identity_checked_before_scan(self):
        self.config['expected_dali_mac']='00:00:00:00:00:00'
        with self.assertRaises(RuntimeError):self.service.scan_existing(2)
        self.assertEqual(self.em.scans,[])

    def test_scan_correlated_alias_can_report_error_but_cannot_prove_success(self):
        self.em.scan_alias=True
        self.em.scan_status=2
        result=self.service.scan_existing(2)
        self.assertEqual(result['state'],'scan_unknown')
        self.assertIn('status 2',result['error'])
        self.em.scan_status=1
        self.config['scan_timeout_seconds']=.1
        result=self.service.scan_existing(2)
        self.assertEqual(result['state'],'scan_unknown')
        self.assertNotIn('addresses',result)

    def test_group_membership_low_high_bits_and_scope_are_preserved(self):
        self.em.group_masks={(2,0):0x8181,(2,63):0xffff}
        self.em.inject_noise=True
        result=self.service.read_groups(2,[0,9,63])
        self.assertEqual(result['state'],'completed')
        self.assertEqual([r['groups'] for r in result['rows']],[[0,7,8,15],[],list(range(16))])
        self.assertEqual(self.em.group_queries,[(2,0),(2,9),(2,63)])
        self.assertEqual(result['requested_addresses'],[0,9,63])
        self.assertEqual(set(self.em.requests),{0xfe01,0xfe30,0xf032})
        self.assertFalse(self.em.writes or self.em.live_commands or self.em.recalls)

    def test_group_read_failure_preserves_partial_and_never_invents_empty_membership(self):
        self.em.group_masks[(2,0)]=3
        self.em.drop_group_address=9
        result=self.service.read_groups(2,[0,9,63])
        self.assertEqual(result['state'],'stopped_partial')
        self.assertEqual(result['pending_address'],9)
        self.assertEqual([r['address'] for r in result['rows']],[0])
        self.assertEqual(self.em.group_queries,[(2,0),(2,9)])
        persisted=json.loads((Path(self.tmp.name)/f"groups-{result['snapshot_id']}.json").read_text())
        self.assertEqual(persisted['rows'][0]['groups'],[0,1])
        self.assertEqual(persisted['state'],'stopped_partial')

    def test_group_query_invalid_scope_and_error_status(self):
        for channel,addresses in [(0,[0]),(True,[0]),(2,[]),(2,[0,0]),(2,[64]),(2,[True])]:
            with self.assertRaises(ValueError):self.service.read_groups(channel,addresses)
        self.assertEqual(self.em.requests,[])
        self.em.group_status=3
        result=self.service.read_groups(2,[63])
        self.assertEqual(result['rows'],[])
        self.assertEqual(result['state'],'stopped_partial')
        self.assertIn('status 3',result['error'])

    def test_async_read_ignores_receipt_crc_and_wrong_lamp(self):
        self.em.inject_noise = True
        result = self.service.read_scenes(1,[0,1],[0,1])
        self.assertEqual(len(result["rows"]),4)
        self.assertTrue(all(x["level_raw"]==254 and x["kelvin_raw"]==4000 for x in result["rows"]))

    def test_heartbeat_response_uses_reply_transport(self):
        self.em.heartbeat_gate=True
        result=self.service.read_scenes(2,[0],[0,1])
        self.assertEqual(len(result['rows']),2)

    def test_recall_is_channel_scoped_without_scene_storage_write(self):
        self.em.actual_alias=True
        self.config['enable_experimental_writes']=False
        with self.assertRaises(ValueError):self.service.recall_scene(0,0,[0])
        result=self.service.recall_scene(2,0,[0,21,63])
        self.assertEqual(self.em.recalls,[bytes([2,0,255,16,0,0,0,0])])
        self.assertEqual(self.em.writes,[])
        self.assertTrue(result['samples_verified'])
        self.assertIsNone(result['verification'][0]['before'])
        self.assertEqual(result['verification'][0]['actual']['level_raw'],254)
        self.assertNotIn((1,0),self.em.live)

    def test_recall_verification_timeout_preserves_receipt_without_resending(self):
        self.em.drop_actual=True
        result=self.service.recall_scene(2,0,[0])
        self.assertEqual(len(self.em.recalls),1)
        self.assertEqual(result['state'],'receipt_received_unverified')
        self.assertEqual(result['receipt_status'],'received')
        self.assertFalse(result['samples_verified'])
        self.assertIn('error',result['verification'][0])

    def test_nonzero_scene_captured_receipt_without_scene_echo(self):
        self.em.recall_receipt='captured'
        result=self.service.recall_scene(2,7,[0,63])
        self.assertEqual(result['receipt_status'],'received')
        self.assertEqual(result['state'],'samples_verified')
        self.assertEqual(result['receipt']['reply_payload'],'0200ff0000000000')
        self.assertNotIn('dispatch_error',result)
        self.assertEqual(len(self.em.recalls),1)

    def test_missing_or_untrusted_recall_receipt_still_checks_output_once(self):
        for receipt in ('drop','stale','wrong_route'):
            with self.subTest(receipt=receipt):
                self.em.recall_receipt=receipt
                count=len(self.em.recalls)
                result=self.service.recall_scene(2,15,[63])
                self.assertEqual(result['receipt_status'],'unknown')
                self.assertEqual(result['state'],'samples_verified')
                self.assertIn('dispatch_error',result)
                self.assertEqual(len(self.em.recalls),count+1)

    def test_all_mask_is_not_evidence_of_scene_activation(self):
        self.em.values[(2,0,7)]=(255,65535)
        result=self.service.recall_scene(2,7,[0])
        self.assertFalse(result['samples_verified'])
        self.assertEqual(result['verification_status'],'unverifiable')
        self.assertEqual(result['verified_sample_count'],0)

    def test_profile_decodes_curve_and_kelvin_limits(self):
        row=self.service.read_profiles(2,[0])['rows'][0]
        self.assertEqual((row['dimming_curve_code'],row['min_kelvin_raw'],row['max_kelvin_raw']),(0,2702,6535))

    def test_execution_target_scope(self):
        p=self.service.preview(self.changes())
        self.config['allowed_write_targets']={'channel':2,'scene':5,'addresses':list(range(64))}
        with self.assertRaises(PermissionError):self.service.apply(p['plan_id'],p['confirmation'])
        self.assertEqual(self.em.writes,[])

    def test_fast_save_cached_types_fresh_backup_full_readback(self):
        self.service.read_profiles(1,[0,1])
        self.em.requests.clear();self.em.quantize=True
        result=self.service.save_scene_batch(self.changes())
        self.assertEqual(result['state'],'completed')
        self.assertEqual(len(self.em.writes),2)
        self.assertTrue(all(r['status']=='verified' for r in result['results']))
        self.assertEqual(self.em.requests.count(0xf035),0)
        self.assertEqual(self.em.requests.count(0xf03a),4) # one backup + one readback per lamp
        self.assertEqual(self.service.get_plan(result['plan_id'])['rows'][0]['before']['level_raw'],254)

    def test_fast_save_quantized_noop_is_not_written(self):
        self.em.values[(1,0,0)]=(200,3003)
        result=self.service.save_scene_batch([self.changes()[0]])
        self.assertEqual(result['results'][0]['status'],'unchanged')
        self.assertEqual(self.em.writes,[])

    def test_fast_save_scope_gate_and_timeout(self):
        self.config['enable_experimental_writes']=False
        with self.assertRaises(PermissionError):self.service.save_scene_batch(self.changes())
        self.config['enable_experimental_writes']=True
        self.config['allowed_write_targets']={'channel':2,'scene':5,'addresses':[0]}
        with self.assertRaises(PermissionError):self.service.save_scene_batch(self.changes())
        self.assertEqual(self.em.writes,[])
        self.config.pop('allowed_write_targets')
        self.em.drop_write=True
        result=self.service.save_scene_batch(self.changes())
        self.assertEqual(result['state'],'stopped_partial')
        self.assertEqual(len(self.em.writes),1)
        self.assertEqual(len(result['rows']),2)

    def test_save_cancel_during_backup_sends_no_writes(self):
        stop=threading.Event();updates=[]
        def progress(p):
            updates.append(p)
            if p['backed_up']==1:stop.set()
        result=self.service.save_scene_batch(self.changes(),progress=progress,cancelled=stop.is_set)
        self.assertEqual(result['state'],'cancelled_before_write')
        self.assertEqual(self.em.writes,[])
        self.assertEqual(result['progress']['backed_up'],1)
        self.assertTrue(updates[-1]['plan_id'])

    def test_persisted_plan_read_does_not_wait_for_bus_lock(self):
        ident=self.service._save('plan',{'state':'applying','results':[]})
        done=threading.Event();values=[]
        def reader():
            values.append(self.service.get_plan(ident));done.set()
        with self.service.lock:
            thread=threading.Thread(target=reader)
            thread.start()
            completed=done.wait(1)
        thread.join(2)
        self.assertTrue(completed)
        self.assertEqual(values[0]['state'],'applying')

    def test_save_cancel_after_send_finishes_readback_then_stops(self):
        stop=threading.Event();original=Gateway.write_scene
        def write(g,item):
            original(g,item);stop.set()
        with patch.object(Gateway,'write_scene',write):
            result=self.service.save_scene_batch(self.changes(),cancelled=stop.is_set)
        self.assertEqual(result['state'],'cancelled_partial')
        self.assertEqual(len(self.em.writes),1)
        self.assertEqual(result['results'][0]['status'],'verified')
        self.assertEqual(result['progress']['completed'],1)
        self.assertEqual(result['results'][0]['readback']['level_raw'],200)

    def test_actual_read_exposes_correlation_limits_without_claiming_address_echo(self):
        self.em.live[(2,63)]=(100,4000)
        g=Gateway('127.0.0.1','127.0.0.1',0,.3,self.em.port)
        try:
            asynchronous=g.read_actual(2,63)
            self.assertEqual(asynchronous['level_raw'],100)
            self.assertEqual(asynchronous['reply_sequence'],0)
            self.assertEqual(asynchronous['reply_transport'],2)
            self.assertEqual(asynchronous['correlation'],'session_route_serial_no_address_echo')
            self.assertFalse(asynchronous['address_echoed'])
            self.em.actual_alias=True
            correlated=g.read_actual(2,63)
            self.assertEqual(correlated['request_sequence'],correlated['reply_sequence'])
            self.assertEqual(correlated['correlation'],'sequence_and_route')
            self.assertFalse(correlated['address_echoed'])
            self.assertGreaterEqual(correlated['received_at'],asynchronous['received_at'])
        finally:g.close()

    def group_fixture(self,group=15):
        self.em.scan_addresses=[0,7,63]
        self.em.group_masks={(2,0):1<<group,(2,63):1<<group,(2,7):0,(1,7):1<<group}
        self.em.live={(2,0):(0,2702),(2,63):(0,4000),(2,7):(90,5000),(1,7):(88,3003)}

    def test_group_scan_boundary_reconnects_before_any_membership_query(self):
        self.group_fixture()
        self.em.scan_requires_reconnect=True
        result=self.service.set_group_live_output(2,15,[0,63],[0],195)
        self.assertTrue(result['samples_verified'])
        self.assertEqual(self.em.requests.count(0xf001),1)
        self.assertEqual(self.em.requests.count(0xfe30),2)
        scan=self.em.requests.index(0xf001)
        first_group=self.em.requests.index(0xf032)
        self.assertIn(0xfe30,self.em.requests[scan+1:first_group])
        self.assertEqual(len(self.em.group_queries),3)
        self.assertEqual(len(self.em.live_commands),1)

    def test_profile_refresh_replaces_stale_fade_before_group_control(self):
        self.group_fixture()
        original=self.service.read_profiles(2,[0,63])
        self.assertEqual(len(original['fade_rows']),2)
        other=self.service._cached_profiles(2,'fade')[63]
        self.em.fade_code=0
        refreshed=self.service.read_profiles(2,[0])
        self.assertNotEqual(original['fade_snapshot_id'],refreshed['fade_snapshot_id'])
        self.assertEqual(self.service._cached_profiles(2,'fade')[0]['profile'],refreshed['fade_rows'][0])
        self.assertEqual(self.service._cached_profiles(2,'fade')[63],other)
        result=self.service.set_group_live_output(2,15,[0,63],[0],195,3000)
        self.assertEqual(result['state'],'preflight_failed')
        self.assertEqual(self.em.live_commands,[])

    def test_group_reconnect_rechecks_identity_before_controls(self):
        self.group_fixture()
        original=self.service._open
        calls=[]
        def checked_open():
            calls.append(1)
            if len(calls)==2:self.config['expected_dali_mac']='02:00:00:00:00:02'
            return original()
        with patch.object(self.service,'_open',side_effect=checked_open):
            result=self.service.set_group_live_output(2,15,[0,63],[0],195)
        self.assertEqual(result['state'],'preflight_failed')
        self.assertIn('identity mismatch',result['error'])
        self.assertEqual(self.em.live_commands,[])
        self.assertEqual(self.em.group_queries,[])

    def test_group_live_targets_literal_group_and_preserves_background(self):
        self.group_fixture()
        result=self.service.set_group_live_output(2,15,[0,63],[0,63],195)
        self.assertEqual(result['state'],'dispatched')
        self.assertTrue(result['samples_verified'])
        self.assertEqual(result['actual_members'],[0,63])
        self.assertEqual(self.em.live_commands,[(0xf02b,bytes([2,2,15,0,0,195,0,0]))])
        self.assertEqual(self.em.live[(2,0)],(195,2702))
        self.assertEqual(self.em.live[(2,63)],(195,4000))
        self.assertEqual(self.em.live[(2,7)],(90,5000))
        self.assertEqual(self.em.live[(1,7)],(88,3003))
        self.assertEqual(self.em.writes,[])
        self.assertEqual(self.em.recalls,[])
        self.assertEqual(self.em.group_queries,[(2,0),(2,7),(2,63)])
        saved=json.loads((Path(self.tmp.name)/f"live-group-{result['operation_id']}.json").read_text())
        self.assertEqual(saved,result)

    def test_group_membership_is_fresh_and_scope_change_aborts(self):
        self.group_fixture(0)
        self.service.set_group_live_output(2,0,[0,63],[0],0)
        self.em.live_commands.clear()
        self.em.group_masks[(2,7)]=1
        result=self.service.set_group_live_output(2,0,[0,63],[0],195)
        self.assertEqual(result['state'],'scope_changed')
        self.assertEqual(result['actual_members'],[0,7,63])
        self.assertEqual(self.em.live_commands,[])
        self.assertEqual(len(self.em.scans),2)

    def test_group_preflight_read_failure_keeps_partial_without_control(self):
        self.group_fixture()
        self.em.drop_group_address=7
        result=self.service.set_group_live_output(2,15,[0,63],[0],195)
        self.assertEqual(result['state'],'preflight_failed')
        self.assertEqual(result['pending'],{'phase':'membership','address':7})
        self.assertEqual(len(result['membership_rows']),1)
        self.assertEqual(self.em.live_commands,[])

    def test_group_ct_then_level_waits_all_member_fades(self):
        self.group_fixture()
        self.em.fade_live_seconds=.1
        result=self.service.set_group_live_output(2,15,[0,63],[0,63],195,3000)
        self.assertTrue(result['samples_verified'])
        self.assertEqual([c for c,b in self.em.live_commands],[0xf02c,0xf02b])
        self.assertTrue(all(b[:3]==bytes([2,2,15]) for c,b in self.em.live_commands))
        self.assertEqual(self.em.interrupted_color,0)
        self.assertEqual(self.em.live[(2,0)],(195,3003))
        self.assertEqual(self.em.live[(2,63)],(195,3003))
        self.assertEqual(self.em.live[(2,7)],(90,5000))

    def test_group_unknown_fade_color_limit_and_dt6_reject_before_control(self):
        self.group_fixture()
        self.em.fade_code=0
        result=self.service.set_group_live_output(2,15,[0,63],[0],195,3000)
        self.assertEqual(result['state'],'preflight_failed')
        self.assertEqual(self.em.live_commands,[])
        result=self.service.set_group_live_output(2,15,[0,63],[0],None,1000)
        self.assertEqual(result['state'],'preflight_failed')
        self.assertEqual(self.em.live_commands,[])
        with patch.object(self.service,'_cached_profiles',return_value={}):
            self.em.lamp_type=0
            result=self.service.set_group_live_output(2,15,[0,63],[0],195)
        self.assertEqual(result['state'],'preflight_failed')
        self.assertEqual(self.em.live_commands,[])

    def test_group_uncertain_receipt_never_retries_or_sends_following_component(self):
        self.group_fixture()
        self.em.drop_live=True
        result=self.service.set_group_live_output(2,15,[0,63],[0],195,3000)
        self.assertEqual(result['state'],'stopped_partial')
        self.assertEqual(len(self.em.live_commands),1)
        self.assertEqual(self.em.live_commands[0][0],0xf02c)
        self.assertEqual(self.em.live[(2,0)][0],0)
        self.assertEqual(result['commands'][0]['status'],'outcome_unknown')

    def test_group_invalid_scope_rejected_before_open(self):
        with patch.object(self.service,'_open') as opened:
            for args in [(True,0,[0],[0],1),(2,16,[0],[0],1),(2,0,[False],[0],1),
                         (2,0,[0,0],[0],1),(2,0,[],[0],1),(2,0,[0],[63],1),
                         (2,0,[0],[0,0],1),(2,0,[0],[0],None),(2,0,[0],[0],255)]:
                with self.assertRaises(ValueError):self.service.set_group_live_output(*args)
            opened.assert_not_called()
        g=Gateway("127.0.0.1","127.0.0.1",0,.1,self.em.port)
        try:
            with self.assertRaises(ValueError):g.set_group_live_component(2,True,'level_raw',0)
        finally:g.close()

    def test_fast_channel_broadcast_only_requested_channel_and_no_interruption(self):
        self.em.fade_live_seconds=.1
        result=self.service.set_channel_live_output(2,229,3000,[0,63])
        self.assertEqual(len(self.em.live_commands),2)
        self.assertTrue(all(b[1:3]==bytes([0,255]) for _,b in self.em.live_commands))
        self.assertEqual(self.em.interrupted_color,0)
        self.assertTrue(result['samples_verified'])
        self.assertTrue(all(self.em.live[(2,a)]==(229,3003) for a in range(64)))
        self.assertNotIn((1,0),self.em.live)
        self.assertEqual(self.em.writes,[])
        self.assertEqual(self.em.recalls,[])
        self.em.requests.clear()
        self.service.set_channel_live_output(2,229,3000,[0])
        self.assertNotIn(0xf01a,self.em.requests)
        self.assertNotIn(0xf035,self.em.requests)

    def test_fast_channel_unknown_fade_aborts_before_broadcast(self):
        self.em.fade_code=0
        with self.assertRaises(ValueError):self.service.set_channel_live_output(2,229,3000,[0])
        self.assertEqual(self.em.live_commands,[])

    def test_fast_channel_timeout_not_retried(self):
        self.em.drop_live=True
        result=self.service.set_channel_live_output(2,229,None,[0])
        self.assertEqual(result['state'],'stopped_partial')
        self.assertEqual(len(self.em.live_commands),1)
        self.assertTrue(result['samples_verified'])

    def test_live_output_previews_without_storage_or_recall_and_reuses_profiles(self):
        self.config['enable_experimental_writes']=False
        self.service.read_profiles(2,[0,1])
        count=self.em.profile_queries
        result=self.service.set_live_output(2,[{'address':0,'level_raw':195,'kelvin_raw':3000},
                                              {'address':1,'level_raw':185}],[0,1])
        self.assertEqual(self.em.profile_queries,count)
        self.assertTrue(result['samples_verified'])
        self.assertEqual(result['state'],'dispatched')
        self.assertEqual(self.em.live[(2,0)],(195,3003))
        self.assertEqual(self.em.live[(2,1)],(185,2702))
        self.assertEqual(self.em.live_commands[0],(0xf02c,bytes.fromhex('0201000000b80b00')))
        self.assertEqual(self.em.live_commands[1],(0xf02b,bytes.fromhex('0201000000c30000')))
        self.assertEqual(self.em.writes,[])
        self.assertEqual(self.em.recalls,[])
        self.assertNotIn((1,0),self.em.live)

    def test_live_all_targets_preflight_before_any_command(self):
        with self.assertRaises(ValueError):
            self.service.set_live_output(2,[{'address':0,'level_raw':195},{'address':1,'kelvin_raw':7000}],[0])
        self.assertEqual(self.em.live_commands,[])
        for bad in ([{'address':0}], [{'address':0,'level_raw':255}],
                    [{'address':0,'level_raw':195}]*2):
            with self.assertRaises(ValueError):self.service.set_live_output(2,bad,[0])
        self.assertEqual(self.em.live_commands,[])

    def test_live_color_fade_is_not_interrupted_by_brightness(self):
        self.em.fade_live_seconds=.1
        result=self.service.set_live_output(2,[{'address':a,'level_raw':191,'kelvin_raw':3200} for a in (0,1)],[0,1])
        self.assertTrue(result['samples_verified'])
        self.assertEqual(self.em.interrupted_color,0)
        self.assertEqual([x[0] for x in self.em.live_commands],[0xf02c,0xf02c,0xf02b,0xf02b])

    def test_live_timeout_stops_without_retry_and_reads_actual(self):
        self.em.drop_live=True
        result=self.service.set_live_output(2,[{'address':0,'level_raw':195},{'address':1,'level_raw':185}],[0,1])
        self.assertEqual(len(self.em.live_commands),1)
        self.assertEqual(result['state'],'stopped_partial')
        self.assertTrue(result['samples_verified'])
        self.assertEqual(result['verification'][0]['actual']['level_raw'],195)
        self.assertNotIn((2,1),self.em.live)

    def test_live_ignores_expired_or_other_gateway_profiles(self):
        profile=self.service.read_profiles(2,[0])
        path=Path(self.tmp.name)/f"profile-{profile['snapshot_id']}.json"
        profile['captured_at']=time.time()-86401
        path.write_text(json.dumps(profile))
        count=self.em.profile_queries
        self.service.set_live_output(2,[{'address':0,'level_raw':195}],[0])
        self.assertEqual(self.em.profile_queries,count+3)
        profile['captured_at']=time.time()+10
        self.service._save('profile',profile)
        self.assertLess(self.service._cached_profiles(2)[0]['captured_at'],time.time())

    def test_preview_then_apply_verified_and_one_shot(self):
        p = self.service.preview(self.changes())
        self.assertEqual(self.em.writes,[])
        result = self.service.apply(p["plan_id"],p["confirmation"])
        self.assertEqual(result["state"],"completed")
        self.assertEqual(len(self.em.writes),2)
        self.assertTrue(all(x["status"]=="verified" for x in result["results"]))
        with self.assertRaises(ValueError):
            self.service.apply(p["plan_id"],p["confirmation"])

    def test_stale_preflight_prevents_all_writes(self):
        p = self.service.preview(self.changes())
        self.em.values[(1,1,0)] = (10,4000)
        with self.assertRaises(RuntimeError):
            self.service.apply(p["plan_id"],p["confirmation"])
        self.assertEqual(self.em.writes,[])

    def test_timeout_no_retry_stops_batch_reports_uncertainty(self):
        p = self.service.preview(self.changes())
        self.em.drop_write = True
        result = self.service.apply(p["plan_id"],p["confirmation"])
        self.assertEqual(result["state"],"stopped_partial")
        self.assertEqual(result["results"][0]["status"],"outcome_unknown")
        self.assertEqual(len(self.em.writes),1)
        self.assertEqual(self.service.get_plan(p["plan_id"])["state"],"stopped_partial")

    def test_write_gate_wrong_confirmation_and_duplicate(self):
        p = self.service.preview(self.changes())
        self.config["enable_experimental_writes"] = False
        with self.assertRaises(PermissionError):
            self.service.apply(p["plan_id"],p["confirmation"])
        self.config["enable_experimental_writes"] = True
        with self.assertRaises(ValueError):
            self.service.apply(p["plan_id"],"yes")
        with self.assertRaises(ValueError):
            self.service.preview([self.changes()[0]]*2)
        self.assertEqual(self.em.writes,[])

    def test_identity_pin_prevents_lamp_commands(self):
        self.config["expected_dali_mac"]="00:00:00:00:00:00"
        with self.assertRaises(RuntimeError):
            self.service.read_scenes(1,[0],[0])

    def test_decode_rejects_bad_crc_length_and_padding(self):
        p=encode(0xf03a,bytes(8),bytes(6),1,19502)
        for bad in (p[:-1],p+b"\1",p+b"\0\0",p[:-1]+bytes([p[-1]^1])):
            with self.assertRaises(ProtocolError): decode(bad)
        self.assertEqual(decode(p).command,0xf03a)

    def test_redacted_discovery_vector(self):
        # Captured frame structure; identities redacted and CRC recomputed offline.
        raw=bytes.fromhex("4b47502d44414c492d484f5354010001520142cb2b01000000fffffe0115f00181fe23034b4e582d44414c492d415050000106090200000000010000000000000000ebaf278b00")
        p=decode(raw)
        self.assertEqual(p.command,0xfe81)
        self.assertEqual(p.payload[17:23].hex(),"020000000001")

    def test_kelvin_quantization_readback(self):
        self.em.quantize=True
        p=self.service.preview(self.changes())
        result=self.service.apply(p["plan_id"],p["confirmation"])
        self.assertEqual(result["state"],"completed")
        self.assertEqual(result["results"][0]["readback"]["kelvin_raw"],3003)

    def test_mismatch_stops_before_second_lamp(self):
        self.em.mismatch=True
        p=self.service.preview(self.changes())
        result=self.service.apply(p["plan_id"],p["confirmation"])
        self.assertEqual(result["state"],"stopped_partial")
        self.assertEqual(result["results"][0]["status"],"verification_failed")
        self.assertEqual(len(self.em.writes),1)

    def test_expired_plan_cannot_write(self):
        p=self.service.preview(self.changes())
        stored=self.service.get_plan(p["plan_id"])
        stored["expires_at"]=0
        self.service._save("plan",stored,p["plan_id"])
        with self.assertRaises(ValueError):
            self.service.apply(p["plan_id"],p["confirmation"])
        self.assertEqual(self.em.writes,[])

if __name__ == "__main__":
    unittest.main()
