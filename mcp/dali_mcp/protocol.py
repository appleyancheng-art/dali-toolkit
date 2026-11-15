"""Observed ConfigMaster UDP framing. No arbitrary command tool is exposed."""
from dataclasses import dataclass
import socket
import struct
import time
import secrets
import zlib

MAGIC = b"KGP-DALI-HOST"

class ProtocolError(RuntimeError):
    pass

def gateway_identity(payload, route, host):
    """FE81: type, NUL-terminated GBK name, DALI version3/MAC6, extras.

    ConfigMaster MainWindow::handleData scans for the terminator before reading
    version/MAC (0x515f0..0x5170d), and decodes names with GBK (0x5149d).
    The default12-byte name must not determine offsets for another project.
    """
    if not isinstance(payload,bytes) or len(route)!=6:
        raise ProtocolError('invalid discovery identity data')
    end=payload.find(b'\0',1,33)
    if end<1 or len(payload)<end+10:
        raise ProtocolError('missing bounded gateway name terminator or identity suffix')
    raw_name=payload[1:end]
    try:
        name=raw_name.decode('gbk');name_valid=True
    except UnicodeDecodeError:
        name=raw_name.decode('gbk','replace');name_valid=False
    version=payload[end+1:end+4];mac=payload[end+4:end+10]
    if mac in (b'\x00'*6,b'\xff'*6) or mac[0]&1 or route[3:] in (b'\x00'*3,b'\xff'*3):
        raise ProtocolError('invalid gateway identity')
    return dict(ip=host,name=name,route=route[3:].hex(),
                dali_firmware='.'.join(map(str,version)),dali_mac=mac.hex(':'),
                name_raw_hex=raw_name.hex(),name_encoding='gbk',name_decode_valid=name_valid,
                raw_payload=payload.hex())

@dataclass(frozen=True)
class Packet:
    sequence: int
    transport: int
    route: bytes
    command: int
    payload: bytes

def encode(command, payload, route, sequence, port, mode=0x55, transport=1):
    if len(route) != 6 or len(payload) > 251:
        raise ValueError("invalid frame size")
    h = bytearray(35)
    h[:13] = MAGIC
    struct.pack_into("<HBBHI", h, 15, sequence, transport, mode, port, 1)
    h[25:31] = route
    h[31] = 1
    struct.pack_into("<HB", h, 32, command, len(payload) + 4)
    data = h + payload
    return bytes(data) + struct.pack("<I", zlib.crc32(data[25:]))

def decode(data):
    if len(data) < 39 or data[:13] != MAGIC or data[34] < 4:
        raise ProtocolError("bad signature or truncated frame")
    end = 35 + data[34]
    # This firmware appends exactly one zero outside its declared length.
    if len(data) not in (end, end + 1) or (len(data) > end and data[end:] != b"\0"):
        raise ProtocolError("bad declared length/padding")
    if zlib.crc32(data[25:end-4]) != struct.unpack_from("<I", data, end-4)[0]:
        raise ProtocolError("CRC mismatch")
    return Packet(struct.unpack_from("<H", data, 15)[0], data[17], data[25:31],
                  struct.unpack_from("<H", data, 32)[0], data[35:end-4])

class Gateway:
    """One socket, one outstanding request. Async results have sequence zero."""
    def __init__(self, host, local_ip, port=19502, timeout=4.0, gateway_port=11211):
        self.peer = (host, gateway_port)
        self.timeout = timeout
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        self.sock.bind((local_ip, port))
        self.port = self.sock.getsockname()[1]
        self.seq = secrets.randbelow(60000) + 1
        self.route = None
        self.device = None
        self.recent_packets=[]

    def close(self):
        self.sock.close()

    def exchange(self, command, payload, route, accept, mode=0x55, timeout=None):
        self.seq = self.seq % 65535 + 1
        self.sock.sendto(encode(command, payload, route, self.seq, self.port, mode), self.peer)
        end = time.monotonic() + (self.timeout if timeout is None else timeout)
        while time.monotonic() < end:
            self.sock.settimeout(max(.001, end - time.monotonic()))
            try:
                data, peer = self.sock.recvfrom(2048)
            except socket.timeout:
                break
            if peer != self.peer:
                continue
            try:
                p = decode(data)
            except ProtocolError:
                continue
            if (p.command == 0xfe05 and self.route is not None
                    and p.route[:3] == self.route[3:] and not p.payload):
                # Reply to this session's heartbeat, echoing its sequence and
                # reversing its route just as MainWindow::handleData does.
                self.sock.sendto(encode(0xfe85, b"", p.route[3:]+p.route[:3],
                                        p.sequence, self.port, transport=2), self.peer)
                continue
            if p.command != command + 0x80:
                continue
            self.recent_packets.append({"sequence":p.sequence,"transport":p.transport,"route":p.route.hex(),"command":hex(p.command),"payload":p.payload.hex()})
            self.recent_packets=self.recent_packets[-12:]
            if p.sequence != self.seq and not (p.sequence == 0 and p.transport == 2):
                continue
            if accept(p):
                return p
        # Invalidate after timeout. Service closes the socket on every failure;
        # unnumbered final replies cannot provide full transaction isolation.
        self.route = None
        raise TimeoutError(f"0x{command:04X}: no valid final reply; session invalidated")

    def discover(self):
        p = self.exchange(0xfe01, b"", bytes.fromhex("01fff0fffffe"),
                          lambda p: len(p.payload) >= 11, mode=0x42)
        info = gateway_identity(p.payload, p.route, self.peer[0])
        self.device = p.route[3:]
        return info

    def connect(self):
        if self.device is None:
            self.discover()
        p = self.exchange(0xfe30, b"", self.device + b"\0\0\0",
                          lambda p: p.route[3:] == self.device and len(p.payload) >= 2)
        # Captured successful handshake: status 0, channel count 2.
        if p.payload[0] != 0:
            raise ProtocolError(f"connection rejected: {p.payload.hex()}")
        self.route = self.device + p.route[:3]
        return {"route": self.route.hex(), "channels": p.payload[1]}

    def query(self, command, channel, address, light_type=0, scene=0, level=0, kelvin=0):
        if self.route is None:
            self.connect()
        route = self.route
        payload = bytes([channel, 1, address, light_type, scene, level]) + struct.pack("<H", kelvin)
        def accept(p):
            # Ignore immediate status-0 receipts and routing aliases. Only the
            # final device-originated tuple can provide a scene/type result.
            return (p.route == route[3:] + route[:3] and len(p.payload) == 8
                    and p.payload[:3] == payload[:3] and p.payload[3] != 0
                    and (command not in (0xf039, 0xf03a) or p.payload[4] == scene))
        p = self.exchange(command, payload, route, accept)
        if p.payload[3] != 1:
            raise ProtocolError(f"device returned status {p.payload[3]} for 0x{command:04X}")
        return p.payload

    def light_type(self, channel, address):
        value = self.query(0xf035, channel, address)[4]
        if value not in (0, 1):
            raise ProtocolError(f"unsupported lamp type code {value}")
        return value

    def scan_existing(self, channel, timeout=240.0):
        """Existing-address search only. No allocation mode is exposed.

        ConfigMaster on_pushButton_3_clicked uses F001 with three data bytes:
        channel, mode zero, zero. Declared length 7 INCLUDES the four-byte CRC.
        Mode 2 adds addresses; never use it here.
        F081 final: channel, mode=0, status=1, eight-byte LSB-first bitmap.
        """
        if type(channel) is not int or channel not in (1,2):
            raise ValueError('explicit channel 1 or 2 required')
        if not isinstance(timeout,(int,float)) or not .05 <= timeout <= 300:
            raise ValueError('scan timeout must be .05..300 seconds')
        if self.route is None:self.connect()
        route=self.route
        payload=bytes([channel,0,0])
        alias=route[3:]+bytes([route[0],int(self.sock.getsockname()[0].split('.')[-1]),route[2]])
        reply=self.exchange(0xf001,payload,route,lambda p:
            len(p.payload)==11 and p.payload[:2]==payload[:2] and p.payload[2]!=0
            and (p.route==route[3:]+route[:3] or
                 (p.route==alias and p.sequence==self.seq and p.transport==1 and p.payload[2]!=1)),
            timeout=timeout)
        if reply.payload[2]!=1:
            raise ProtocolError(f'existing-address scan returned status {reply.payload[2]}')
        bitmap=reply.payload[3:]
        addresses=[a for a in range(64) if bitmap[a//8] & (1 << (a%8))]
        return {'channel':channel,'addresses':addresses,'count':len(addresses),
                'bitmap_hex':bitmap.hex(),'raw_final_payload':reply.payload.hex(),
                'source':'existing_address_search_F001_mode0'}

    def change_short_address(self,channel,source,destination,timeout=30.0):
        """Low-level F002 single-address edit; caller must guard and back up.

        Native button71 changes to 0..63; button78 removes via destination255.
        This cannot establish vacancy or physical identity and does not migrate
        host names/layouts/caches. It is not exposed as a raw MCP write tool.
        A final receipt is NOT a replacement for a new scan and device readback.
        """
        if type(channel) is not int or channel not in (1,2):raise ValueError('channel1/2 required')
        if type(source) is not int or not 0<=source<=63:raise ValueError('source0..63 required')
        if type(destination) is not int or destination not in (*range(64),255) or destination==source:
            raise ValueError('different destination0..63 or explicit255 removal required')
        if type(timeout) not in (int,float) or not .05<=timeout<=60:raise ValueError('timeout .05..60 required')
        if self.route is None:self.connect()
        route=self.route;payload=bytes([channel,1,source,destination])
        # Field captures: immediate result0 uses a routing alias; final result2
        # (deletion) has exactly five bytes on the original session reverse route.
        # Native handleData identifies result1=move, result2=delete. No replay.
        p=self.exchange(0xf002,payload,route,lambda p:
            p.route==route[3:]+route[:3] and len(p.payload)==5 and
            p.payload[:3]==payload[:3] and p.payload[4]==destination and p.payload[3]!=0,
            timeout=timeout)
        expected=2 if destination==255 else 1
        if p.payload[3]!=expected:
            raise ProtocolError(f'address edit returned result{p.payload[3]}, expected{expected}; read back before recovery')
        return dict(channel=channel,source_address=source,destination_address=destination,
                    result=p.payload[3],raw_final_payload=p.payload.hex(),
                    reply_sequence=p.sequence,reply_transport=p.transport,reply_route=p.route.hex(),
                    receipt_only=True,physical_identity_verified=False)

    def allocate_missing_addresses(self,channel,timeout=240.0):
        """Low-level native '新增地址' mode2, not full reset/reallocation.

        Acts on unaddressed gear across ONE channel; it cannot select a physical
        fixture. Caller must establish the exact recovery scope beforehand.
        Firmware1.6.9 field trial restored sole vacant63; no broader claim.
        """
        if type(channel) is not int or channel not in (1,2):raise ValueError('channel1/2 required')
        if type(timeout) not in (int,float) or not .05<=timeout<=300:raise ValueError('timeout .05..300 required')
        if self.route is None:self.connect()
        route=self.route;payload=bytes([channel,2,0])
        p=self.exchange(0xf001,payload,route,lambda p:
            p.route==route[3:]+route[:3] and len(p.payload)==11 and
            p.payload[:2]==payload[:2] and p.payload[2]!=0,timeout=timeout)
        if p.payload[2]!=1:raise ProtocolError(f'incremental allocation returned result{p.payload[2]}; read back before recovery')
        bitmap=p.payload[3:]
        addresses=[a for a in range(64) if bitmap[a//8]&(1<<(a%8))]
        return dict(channel=channel,mode=2,addresses=addresses,count=len(addresses),bitmap_hex=bitmap.hex(),
                    raw_final_payload=p.payload.hex(),reply_sequence=p.sequence,reply_transport=p.transport,
                    reply_route=p.route.hex(),physical_identity_verified=False,independent_scan_required=True)

    def read_duplicate_addresses(self,channel,timeout=10.0):
        """Read the gateway's F043 duplicate-address bitmap, not a new allocation.

        Native calls this after existing-address search. Its cache/freshness is
        not attested by the reply. Do not infer physical uniqueness from zero.
        Header length5 includes CRC: exactly one request byte, the channel.
        F0C3 result1 is final; eight LSB-first bitmap bytes follow ch/result.
        Firmware1.6.9 can reply transport1 using the correlated local-IP alias.
        """
        if type(channel) is not int or channel not in (1,2):raise ValueError('explicit channel 1 or 2 required')
        if type(timeout) not in (int,float) or not .05<=timeout<=60:raise ValueError('duplicate query timeout must be .05..60 seconds')
        if self.route is None:self.connect()
        route=self.route
        alias=route[3:]+bytes([route[0],int(self.sock.getsockname()[0].split('.')[-1]),route[2]])
        reply=self.exchange(0xf043,bytes([channel]),route,lambda p:
            len(p.payload)==10 and p.payload[0]==channel and p.payload[1]!=0 and
            (p.route==route[3:]+route[:3] or (p.route==alias and p.sequence==self.seq and p.transport==1)),
            timeout=timeout)
        if reply.payload[1]!=1:raise ProtocolError(f'duplicate-address query returned status {reply.payload[1]}')
        bitmap=reply.payload[2:]
        addresses=[a for a in range(64) if bitmap[a//8]&(1<<(a%8))]
        return dict(channel=channel,duplicate_addresses=addresses,count=len(addresses),bitmap_hex=bitmap.hex(),
                    raw_final_payload=reply.payload.hex(),source='gateway_duplicate_bitmap_F043',
                    physical_uniqueness_verified=False,bitmap_freshness_verified=False,
                    reply_sequence=reply.sequence,reply_transport=reply.transport,reply_route=reply.route.hex())

    def read_groups(self,channel,address):
        """Read a short address's membership bitmap, never modify groups."""
        if type(channel) is not int or channel not in (1,2) or type(address) is not int or not 0<=address<64:
            raise ValueError('channel 1/2 and short address 0..63 required')
        if self.route is None:self.connect()
        route=self.route
        # MainWindow::makeGroupQueryRequest: C0/C1 query low/high groups.
        # Declared wire length 12 includes CRC, so eight data bytes, not twelve.
        payload=bytes([channel,1,address,0xc0,0xc1])+bytes(3)
        reply=self.exchange(0xf032,payload,route,lambda p:
            p.route==route[3:]+route[:3] and len(p.payload)==8
            and p.payload[:3]==payload[:3] and p.payload[3]!=0)
        if reply.payload[3]!=1:
            raise ProtocolError(f'group query returned status {reply.payload[3]}')
        mask=int.from_bytes(reply.payload[4:6],'little')
        return {'channel':channel,'address':address,'groups':[g for g in range(16) if mask & (1<<g)],
                'group_mask':mask,'raw_payload':reply.payload.hex()}

    def _query_configured_min_ct(self,channel,address):
        # F029 differs from the usual eight-byte request: declared length 8
        # includes CRC, leaving exactly four data bytes.
        if self.route is None:self.connect()
        route=self.route
        payload=bytes([channel,1,address,0xfa])
        p=self.exchange(0xf029,payload,route,lambda p:p.route==route[3:]+route[:3]
                        and len(p.payload)>=6 and p.payload[:3]==payload[:3] and p.payload[3]!=0)
        if p.payload[3]!=1:raise ProtocolError(f"minimum CT query status {p.payload[3]}")
        return p.payload

    def read_lamp_profile(self,channel,address):
        typ=self.light_type(channel,address)
        curve=self.query(0xf025,channel,address,0xee)[4]
        low=int.from_bytes(self._query_configured_min_ct(channel,address)[4:6],'little')
        high=int.from_bytes(self.query(0xf027,channel,address,0xfa)[4:6],'little')
        return {"channel":channel,"address":address,"light_type_code":typ,"dimming_curve_code":curve,
                "min_kelvin_raw":low,"max_kelvin_raw":high}

    PARAMETER_FIELDS=('light_type','status','fade_time_rate','min_level','max_level',
                      'power_on','system_failure','dimming_curve','configured_min_ct',
                      'configured_max_ct','groups')

    def read_lamp_parameter(self,channel,address,field,light_type=None):
        """Allowlisted reads from the single-lamp UI; no arbitrary query API.

        Power/failure replies put level at byte 4 and Kelvin at bytes 5..6,
        unlike scene replies (level 5, Kelvin 6..7). Keep raw evidence.
        """
        if type(channel) is not int or channel not in (1,2) or type(address) is not int or not 0<=address<64:
            raise ValueError('explicit channel and short address required')
        if field not in self.PARAMETER_FIELDS:raise ValueError('unknown parameter field')
        if field!='light_type' and (type(light_type) is not int or light_type not in (0,1)):
            raise ValueError('fresh supported light type required')
        if field=='groups':
            data=self.read_groups(channel,address)
            return {'value':{'groups':data['groups'],'group_mask':data['group_mask']},
                    'command':'F032','raw_reply':data['raw_payload']}
        if field.startswith('configured_') and light_type==0:
            return {'state':'not_applicable','reason':'DT6 has no DT8 color-temperature query'}
        command,argument={
            'light_type':(0xf035,0),'status':(0xf016,0x90),
            'fade_time_rate':(0xf01a,0xa5),'min_level':(0xf01d,0xa2),
            'max_level':(0xf01f,0xa1),'power_on':(0xf021,light_type),
            'system_failure':(0xf023,light_type),'dimming_curve':(0xf025,0xee),
            'configured_min_ct':(0xf029,0xfa),'configured_max_ct':(0xf027,0xfa),
        }[field]
        b=(self._query_configured_min_ct(channel,address) if command==0xf029
           else self.query(command,channel,address,argument))
        if field=='light_type':
            if b[4] not in (0,1):raise ProtocolError(f'unsupported lamp type code {b[4]}')
            value={'code':b[4],'name':'DT8' if b[4]==1 else 'DT6'}
        elif field=='fade_time_rate':
            value={'raw':b[4],'fade_time_code':b[4]>>4,'fade_rate_code':b[4]&15,
                   'extended_fade_not_read':b[4]>>4==0}
        elif field in ('power_on','system_failure'):
            value={'level_raw':b[4]}
            if light_type==1:value['kelvin_raw']=int.from_bytes(b[5:7],'little')
        elif field.startswith('configured_'):
            value={'kelvin_raw':int.from_bytes(b[4:6],'little')}
        elif field in ('min_level','max_level'):value={'level_raw':b[4]}
        else:value={'raw':b[4]}  # Preserve status/curve codes, including unknown bits.
        return {'value':value,'command':f'{command:04X}','raw_reply':b.hex()}

    def read_scene(self, channel, address, scene, light_type):
        b = self.query(0xf03a, channel, address, light_type, scene)
        return {"channel": channel, "address": address, "scene": scene,
                "light_type_code": light_type, "level_raw": b[5],
                "kelvin_raw": struct.unpack_from("<H", b, 6)[0]}

    def write_scene(self, item):
        self.query(0xf039, item["channel"], item["address"], item["light_type_code"],
                   item["scene"], item["level_raw"], item["kelvin_raw"])

    def recall_channel_scene(self, channel, scene):
        """One channel broadcast, matching MainWindow::sceneButtonBindingClick."""
        if type(channel) is not int or channel not in (1,2) or type(scene) is not int or not 0 <= scene <= 15:
            raise ValueError("explicit channel 1/2 and scene 0..15 required")
        return self._recall_scene(channel,0,255,scene)

    def recall_group_scene(self,channel,group,scene):
        """SceneConfigWidget::onTestSceneClicked: mode2, literal group/scene."""
        if (type(channel) is not int or channel not in (1,2) or type(group) is not int or not 0<=group<=15 or
            type(scene) is not int or not 0<=scene<=15):raise ValueError('channel1/2, group0..15 and scene0..15 required')
        return self._recall_scene(channel,2,group,scene)

    def recall_lamp_scene(self,channel,address,scene):
        """MainWindow short-address selection uses mode1, literal address/scene."""
        if (type(channel) is not int or channel not in (1,2) or
                type(address) is not int or not 0<=address<=63 or
                type(scene) is not int or not 0<=scene<=15):
            raise ValueError('channel1/2, address0..63 and scene0..15 required')
        return self._recall_scene(channel,1,address,scene)

    def _recall_scene(self,channel,mode,address,scene):
        if self.route is None:
            self.connect()
        route=self.route
        payload=bytes([channel,mode,address,0x10,scene,0,0,0])
        alias=route[3:]+bytes([route[0],int(self.sock.getsockname()[0].split('.')[-1]),route[2]])
        # Captured F093 is [channel,0,255,0,0,0,0,0], even for a nonzero
        # scene. It is only a receipt. Do not require a nonexistent scene echo
        # or accept an uncorrelated sequence-zero receipt from an earlier call.
        reply=self.exchange(0xf013,payload,route,lambda p:
            p.sequence==self.seq and p.transport==1
            and p.route in (route[3:]+route[:3],alias)
            and len(p.payload)==8 and p.payload[:3]==payload[:3])
        return {"command":"F013","reply_command":"F093","reply_payload":reply.payload.hex(),
                "reply_route":reply.route.hex(),"receipt_only":True,"status_interpreted":False}

    def read_actual(self, channel, address, is_dt8=True):
        """F047 live Brightness/CT pairs; unlike F03A, reply has no short address echo."""
        if self.route is None:
            self.connect()
        route=self.route
        payload=bytes([channel,address,int(is_dt8),0,0,0,0,0])
        alias=route[3:]+bytes([route[0],int(self.sock.getsockname()[0].split('.')[-1]),route[2]])
        p=self.exchange(0xf047,payload,route,lambda p:
            (p.route==route[3:]+route[:3] or (p.route==alias and p.sequence==self.seq and p.transport==1)) and len(p.payload)>=9
            and p.payload[0]==channel and p.payload[1]!=0)
        b=p.payload
        if b[1]!=1:
            raise ProtocolError(f"live query status {b[1]}")
        return {"channel":channel,"address":address,"light_type_code":b[2],
                "level_raw":b[3],"kelvin_raw":int.from_bytes(b[4:6],'little'),
                "second_level_raw":b[6],"second_kelvin_raw":int.from_bytes(b[7:9],'little'),
                "raw_payload":b.hex(),"received_at":time.time(),
                "request_sequence":self.seq,"reply_sequence":p.sequence,
                "reply_transport":p.transport,"reply_route":p.route.hex(),"address_echoed":False,
                "correlation":("sequence_and_route" if p.sequence==self.seq else
                               "session_route_serial_no_address_echo")}

    def blink_phase(self, channel, address, on):
        """Native UI flicker phase: F012 recall-max(5), or F010 off(0).

        Host timers, not the gateway, repeat these commands. This method
        never starts a loop and exposes no arbitrary DALI opcode.
        """
        if (type(channel) is not int or channel not in (1, 2)
                or type(address) is not int or not 0 <= address < 64
                or type(on) is not bool):
            raise ValueError('explicit channel 1/2, short address 0..63 and boolean on required')
        if self.route is None:
            self.connect()
        route = self.route
        command = 0xf012 if on else 0xf010
        payload = bytes([channel, 1, address, 5 if on else 0, 0, 0, 0, 0])
        alias = route[3:] + bytes([route[0], int(self.sock.getsockname()[0].split('.')[-1]), route[2]])
        reply = self.exchange(command, payload, route, lambda p:
            p.sequence == self.seq and p.transport == 1
            and p.route in (route[3:] + route[:3], alias)
            and len(p.payload) == 8 and p.payload[:3] == payload[:3])
        return {'command': f'{command:04X}', 'reply_command': f'{command+0x80:04X}',
                'request_payload': payload.hex(), 'reply_payload': reply.payload.hex(),
                'receipt_only': True, 'status_interpreted': False}

    def set_level_extreme(self, channel, address, extreme):
        """Native max/min controls: F012 with recall-max5 or recall-min6.

        Single-lamp UI buttons39/66 (66 delegates to43); buttons8/9 use
        the same selectors. Explicit short addresses only; receipts are
        not output proof.
        """
        if (type(channel) is not int or channel not in (1,2)
                or type(address) is not int or not 0<=address<64
                or type(extreme) is not str or extreme not in ('minimum','maximum')):
            raise ValueError('channel1/2, address0..63 and minimum/maximum required')
        if self.route is None:self.connect()
        route=self.route
        payload=bytes([channel,1,address,5 if extreme=='maximum' else 6,0,0,0,0])
        alias=route[3:]+bytes([route[0],int(self.sock.getsockname()[0].split('.')[-1]),route[2]])
        reply=self.exchange(0xf012,payload,route,lambda p:
            p.sequence==self.seq and p.transport==1 and p.route in (route[3:]+route[:3],alias)
            and len(p.payload)==8 and p.payload[:3]==payload[:3])
        return dict(command='F012',reply_command='F092',request_payload=payload.hex(),
            reply_payload=reply.payload.hex(),receipt_only=True,status_interpreted=False)

    def relative_dim(self, channel, address, direction):
        """One native F011 up/down command; its receipt is not output proof.

        MainWindow buttons 11/10 use direction 1/2. gateDimLDMsgHandle
        writes F011, declared length 12 (= eight payload bytes plus CRC).
        Only explicit short addresses are exposed here; no repeat loop.
        """
        if (type(channel) is not int or channel not in (1, 2)
                or type(address) is not int or not 0 <= address < 64
                or type(direction) is not str or direction not in ('up', 'down')):
            raise ValueError('channel 1/2, short address 0..63 and direction up/down required')
        if self.route is None:
            self.connect()
        route = self.route
        payload = bytes([channel, 1, address, 1 if direction == 'up' else 2, 0, 0, 0, 0])
        alias = route[3:] + bytes([route[0], int(self.sock.getsockname()[0].split('.')[-1]), route[2]])
        reply = self.exchange(0xf011, payload, route, lambda p:
            p.sequence == self.seq and p.transport == 1
            and p.route in (route[3:] + route[:3], alias)
            and len(p.payload) == 8 and p.payload[:3] == payload[:3])
        return {'command': 'F011', 'reply_command': 'F091',
                'request_payload': payload.hex(), 'reply_payload': reply.payload.hex(),
                'reply_route': reply.route.hex(), 'receipt_only': True,
                'status_interpreted': False}

    def set_live_component(self, channel, address, component, value):
        """SceneConfigWidget realtime output: F02B level or F02C CT, fade=0.

        A reply is a receipt only. Independently read actual output afterwards.
        Single short address only; never silently promote a batch to broadcast.
        """
        if type(address) is not int or not 0<=address<=63:
            raise ValueError("explicit channel and short address required")
        return self._set_live_component(channel,1,address,component,value)

    def set_channel_live_component(self,channel,component,value):
        """Explicit entire-channel broadcast, matching MainWindow broadcast sliders."""
        return self._set_live_component(channel,0,255,component,value)

    def set_group_live_component(self,channel,group,component,value):
        """MainWindow group selection uses mode2 and a literal group0..15."""
        if type(group) is not int or not 0<=group<=15:
            raise ValueError('explicit group 0..15 required')
        return self._set_live_component(channel,2,group,component,value)

    def _set_live_component(self,channel,mode,address,component,value):
        if type(channel) is not int or channel not in (1,2):raise ValueError('channel 1/2 required')
        if component not in ('level_raw','kelvin_raw') or type(value) is not int:
            raise ValueError("invalid live output component")
        if component=='level_raw':
            if not 0<=value<=254:raise ValueError("live level must be 0..254; no MASK")
            command=0xf02b
            payload=bytes([channel,mode,address,0,0,value,0,0])
        else:
            if not 1000<=value<=10000:raise ValueError("live Kelvin must be 1000..10000")
            command=0xf02c
            payload=bytes([channel,mode,address,0,0])+struct.pack('<H',value)+b'\0'
        if self.route is None:self.connect()
        route=self.route
        alias=route[3:]+bytes([route[0],int(self.sock.getsockname()[0].split('.')[-1]),route[2]])
        reply=self.exchange(command,payload,route,lambda p:
            (p.route==route[3:]+route[:3] or (p.route==alias and p.sequence==self.seq and p.transport==1))
            and len(p.payload)>=3 and p.payload[:3]==payload[:3])
        return {'command':hex(command),'reply_payload':reply.payload.hex(),'receipt_only':True}

    def read_fade_timing(self,channel,address):
        raw=self.query(0xf01a,channel,address,0xa5)[4]
        code=raw>>4
        # DALI standard fade table, rounded UP. Code 0 may use extended fade;
        # do not infer its duration without querying that separate capability.
        upper=(None,.71,1.01,1.42,2.01,2.84,4.01,5.66,8.01,11.32,16.01,22.63,32.01,45.26,64.01,90.52)[code]
        return {'channel':channel,'address':address,'fade_time_rate_raw':raw,
                'fade_time_code':code,'fade_rate_code':raw&15,'fade_seconds_upper':upper}
