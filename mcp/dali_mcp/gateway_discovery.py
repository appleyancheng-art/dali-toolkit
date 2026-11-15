"""Candidate LAN discovery. FE01 only; separate from lighting/address allocation.

Protocol evidence: ConfigMaster udp_NetConnection.o sendUdp2Devices overload,
0x84e .rdata+0x6e = 255.255.255.255, destination port0x2bcb; main window FE01.
Existing validated codec covers framing/CRC. Never select the first responder.
"""
import ipaddress
import json
import math
import secrets
import socket
import subprocess
import time

from dali_mcp.protocol import Gateway, ProtocolError, decode, encode, gateway_identity


def ipv4_host(value):
    ip = ipaddress.IPv4Address(value)
    if ip.is_unspecified or ip.is_multicast or ip.is_loopback or int(ip) == 0xffffffff:
        raise ValueError('a usable local IPv4 host is required')
    return str(ip)


def windows_interfaces(runner=subprocess.run):
    # Fixed command; no interpolated input. All operational NICs, including VPNs,
    # remain visible so the caller can explicitly select a project-facing NIC.
    script = ("$ErrorActionPreference='Stop'; [Console]::OutputEncoding=[System.Text.UTF8Encoding]::new(); "
              "$up=@(Get-NetAdapter | Where-Object Status -eq 'Up' | Select-Object -ExpandProperty ifIndex); "
              "@(Get-NetIPAddress -AddressFamily IPv4 | Where-Object { "
              "$_.InterfaceIndex -in $up -and $_.AddressState -eq 'Preferred' } | "
              "Select-Object IPAddress,PrefixLength,InterfaceIndex,InterfaceAlias) | ConvertTo-Json -Compress")
    try:
        result = runner(['powershell.exe','-NoProfile','-NonInteractive','-Command',script],
                        capture_output=True, text=True, encoding='utf-8', errors='replace',
                        timeout=15, check=True)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError('Windows network-interface query timed out; no discovery packets sent') from exc
    except subprocess.CalledProcessError as exc:
        raise RuntimeError('Cannot read Windows network interfaces: '+str(exc.stderr or exc)[:1000]) from exc
    data = json.loads(result.stdout.strip() or '[]')
    if isinstance(data, dict):
        data = [data]
    rows = []
    for item in data:
        try:
            ip = ipv4_host(item['IPAddress'])
            prefix = item['PrefixLength']
            if type(prefix) is not int or not 1 <= prefix <= 32:
                raise ValueError('invalid prefix')
            net = ipaddress.IPv4Network(f'{ip}/{prefix}',strict=False)
            if prefix > 30 or ipaddress.IPv4Address(ip) in (net.network_address,net.broadcast_address):
                continue
            rows.append(dict(local_ip=ip,prefix_length=prefix,
                             broadcast=str(net.broadcast_address),
                             interface_index=item['InterfaceIndex'],name=item['InterfaceAlias']))
        except (ValueError,KeyError,TypeError):
            continue
    return sorted(rows,key=lambda x:(x['interface_index'],x['local_ip']))


def parse_reply(data, peer, sequence, interface):
    if peer[1] != 11211:
        raise ProtocolError('unexpected source port')
    try:
        source = ipv4_host(peer[0])
    except ValueError as exc:
        raise ProtocolError('invalid source address') from exc
    net = ipaddress.IPv4Network(f"{interface['local_ip']}/{interface['prefix_length']}",strict=False)
    if ipaddress.IPv4Address(source) not in net or source in (str(net.network_address),str(net.broadcast_address)):
        raise ProtocolError('response outside selected interface subnet')
    p = decode(data)
    if p.command != 0xfe81 or p.sequence != sequence or p.route[:3] != b'\xff\xff\xfe':
        raise ProtocolError('unmatched discovery response')
    return dict(gateway_identity(p.payload,p.route,source),local_ip=interface['local_ip'],
                interface_index=interface['interface_index'])


def search_gateways(interfaces, timeout_seconds=2.0, *, socket_factory=socket.socket,
                    clock=time.monotonic, sequence_factory=lambda:secrets.randbelow(65535)+1):
    if not isinstance(interfaces,list) or not 1 <= len(interfaces) <= 16:
        raise ValueError('select1..16 local IPv4 interfaces')
    if type(timeout_seconds) not in (int,float) or not math.isfinite(timeout_seconds) or not .2 <= timeout_seconds <= 10:
        raise ValueError('discovery window must be0.2..10 seconds per interface')
    normalized=[]
    for item in interfaces:
        ip=ipv4_host(item['local_ip']);prefix=item['prefix_length']
        if type(prefix) is not int or not 1 <= prefix <= 30:
            raise ValueError('broadcast-capable IPv4 prefix1..30 required')
        net=ipaddress.IPv4Network(f'{ip}/{prefix}',strict=False)
        if ipaddress.IPv4Address(ip) in (net.network_address,net.broadcast_address):
            raise ValueError('interface address must be a host')
        if ip in [x['local_ip'] for x in normalized]:
            raise ValueError('duplicate local interface address')
        normalized.append(dict(item,local_ip=ip,prefix_length=prefix))
    started=clock(); reports=[]; found={}
    for interface in normalized:
        report=dict(local_ip=interface['local_ip'],state='starting',accepted=0,ignored=0,datagrams_sent=0)
        reports.append(report);sock=None
        try:
            sock=socket_factory(socket.AF_INET,socket.SOCK_DGRAM)
            if hasattr(socket,'SO_EXCLUSIVEADDRUSE'):
                sock.setsockopt(socket.SOL_SOCKET,socket.SO_EXCLUSIVEADDRUSE,1)
            sock.setsockopt(socket.SOL_SOCKET,socket.SO_BROADCAST,1)
            sock.bind((interface['local_ip'],0))
            seq=sequence_factory();port=sock.getsockname()[1]
            packet=encode(0xfe01,b'',bytes.fromhex('01fff0fffffe'),seq,port,mode=0x42)
            sock.sendto(packet,('255.255.255.255',11211));report['datagrams_sent']=1
            end=clock()+timeout_seconds
            for _ in range(4096):
                remaining=end-clock()
                if remaining<=0:break
                sock.settimeout(remaining)
                try:data,peer=sock.recvfrom(2048)
                except socket.timeout:break
                try:entry=parse_reply(data,peer,seq,interface)
                except (ProtocolError,ValueError):report['ignored']+=1;continue
                key=(entry['dali_mac'],entry['ip'],entry['local_ip'])
                if key in found and found[key]!=entry:
                    found[key]['identity_conflict']=True
                elif key not in found:
                    if len(found)>=256:raise RuntimeError('discovery result limit exceeded')
                    found[key]=entry
                report['accepted']+=1
            else:
                raise RuntimeError('discovery datagram limit exceeded')
            report['state']='completed'
        except (OSError,RuntimeError) as exc:
            report.update(state='failed',error=str(exc))
        finally:
            if sock is not None:sock.close()
    rows=sorted(found.values(),key=lambda x:(x['dali_mac'],x['ip'],x['local_ip']))
    for row in rows:
        row['identity_conflict']=bool(row.get('identity_conflict') or
            len({x['ip'] for x in rows if x['dali_mac']==row['dali_mac']})>1 or
            len({x['dali_mac'] for x in rows if x['ip']==row['ip']})>1)
        row['connection_tested']=False
    complete=sum(r['state']=='completed' for r in reports)
    return dict(state='completed' if complete==len(reports) else 'partial' if complete else 'failed',
                gateways=rows,interfaces=reports,seconds=round(clock()-started,3),
                selected_gateway=None,lighting_commands_sent=False,
                coverage='selected local IPv4 broadcast domains only; absence is not proof of offline')


def verify_connection(candidate, *, gateway_factory=Gateway):
    """Fresh unicast identity check then handshake; never changes active config."""
    if candidate.get('identity_conflict'):
        raise ValueError('ambiguous gateway identity; resolve discovery conflict first')
    host=ipv4_host(candidate['ip']);local=ipv4_host(candidate['local_ip'])
    gateway=gateway_factory(host,local,port=0)
    try:
        fresh=gateway.discover()
        for key in ('ip','dali_mac','dali_firmware','route'):
            if fresh[key]!=candidate[key]:
                raise RuntimeError('gateway identity changed since search; search again')
        connection=gateway.connect()
        if type(connection.get('channels')) is not int or connection['channels']!=2:
            raise RuntimeError('unsupported channel count; active target unchanged')
        return dict(state='connected',gateway=fresh,connection=connection,local_ip=local,
                    lighting_commands_sent=False,configuration_changed=False)
    finally:
        gateway.close()
