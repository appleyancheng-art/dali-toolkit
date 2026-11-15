"""Read ConfigMaster 1.2.4.1 per-gateway JSON caches as local evidence only.

Names are scoped by gateway/channel/address or gateway/channel/group/scene.
Cached parameters, address keys and scene placeholders are never converted to
online-device evidence, scene targets, capabilities, or restoration plans.
"""
import copy
import hashlib
import ipaddress
import json
import re
import time
import uuid


def canonical(document):
    try:
        data=json.dumps(document,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode('utf-8')
    except (TypeError,ValueError,UnicodeError) as exc:
        raise ValueError('cache must contain finite JSON values and valid Unicode') from exc
    if len(data)>2_000_000:raise ValueError('cache exceeds2MB')
    return data


def _indexed(mapping,maximum,label):
    if not isinstance(mapping,dict):raise ValueError(f'{label} must be an object')
    for key in mapping:
        if type(key) is not str or not re.fullmatch(r'0|[1-9][0-9]*',key) or int(key)>maximum:
            raise ValueError(f'{label} keys must be canonical indices0..{maximum}')
    return sorted(mapping,key=int)


def _name(value):
    if type(value) is not str or len(value.encode('utf-8'))>4096 or '\0' in value:
        raise ValueError('invalid cached name')
    return value


def inspect_configmaster_cache(document):
    canonical(document)
    if not isinstance(document,dict) or document.get('version')!='1.2.4.1':
        raise ValueError('only observed ConfigMaster1.2.4.1 per-gateway cache schema supported')
    mac=document.get('dalimac')
    if type(mac) is not str or not re.fullmatch(r'(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}',mac):
        raise ValueError('cache DALI MAC required')
    ip=document.get('ip')
    if type(ip) is not str:raise ValueError('cache IPv4 required')
    ip=str(ipaddress.IPv4Address(ip))
    firmware=document.get('gwVersion')
    if type(firmware) is not str or not re.fullmatch(r'[0-9]+(?:\.[0-9]+){2}',firmware):
        raise ValueError('cache gateway firmware required')
    channels=[]
    for channel in (1,2):
        data=document.get(f'zCh{channel}')
        if not isinstance(data,dict):raise ValueError('both cache channel objects required')
        addresses=data.get('addresses');group_names=data.get('groupNames');scene_names=data.get('sceneNamesByGroup')
        address_keys=_indexed(addresses,63,'addresses')
        names={};markers={}
        for key in address_keys:
            row=addresses[key]
            if not isinstance(row,dict):raise ValueError('invalid cached address row')
            names[key]=_name(row.get('name'))
            marker=row.get('sceneKnown')
            if marker is not None and (type(marker) is not int or not 0<=marker<=65535):
                raise ValueError('invalid raw sceneKnown marker')
            markers[key]=marker
        groups={key:_name(group_names[key]) for key in _indexed(group_names,15,'group names')}
        scenes={}
        for group in _indexed(scene_names,15,'scene-name groups'):
            scenes[group]={scene:_name(scene_names[group][scene])
                for scene in _indexed(scene_names[group],15,'scene names')}
        channels.append(dict(channel=channel,cached_address_keys=[int(k) for k in address_keys],
                             lamp_names=names,group_names=groups,scene_names_by_group=scenes,
                             scene_known_raw_by_address=markers))
    return dict(format='configmaster-cache-evidence-v1',native_pc_version=document['version'],
                gateway=dict(ip=ip,dali_mac=mac.lower(),dali_firmware=firmware),
                gateway_name=_name(document.get('devName','')),channels=channels,
                source='ConfigMaster per-gateway local JSON cache',trust='local_cache_unverified',
                online_addresses_verified=False,device_values_verified=False,
                scene_targets_created=False,capabilities_refreshed=False,
                notes=['Address keys may be stale and are not a live scan.',
                       'Names are local labels; scenes are indexed within channel AND group.',
                       'sceneKnown is preserved as a raw cache marker; cached zeroes are not proven OFF.',
                       'Empty/missing labels remain distinct; no generated display label is written back.',
                       'This import does not read the bus, change ConfigMaster files, restore a project, or write devices.'])


class NativeCacheMixin:
    def _check_native_identity(self,summary):
        gateway=summary['gateway']
        if (gateway['ip']!=self.config['gateway_ip'] or
            gateway['dali_mac']!=self.config['expected_dali_mac'].lower() or
            gateway['dali_firmware']!=self.config['expected_firmware']):
            raise ValueError('native cache does not match configured gateway identity; no network connection attempted')

    def import_configmaster_cache(self,document):
        summary=inspect_configmaster_cache(document)
        self._check_native_identity(summary)
        record=dict(cache_id=uuid.uuid4().hex,imported_at=time.time(),summary=summary,
                    document=copy.deepcopy(document),document_sha256=hashlib.sha256(canonical(document)).hexdigest())
        with self.lock:self._save('native-cache',record,record['cache_id'])
        return {key:copy.deepcopy(value) for key,value in record.items() if key!='document'}

    def get_configmaster_cache(self,cache_id=None,include_document=False):
        if type(include_document) is not bool:raise ValueError('include_document must be boolean')
        if cache_id is None:
            if include_document:raise ValueError('explicit cache ID required for document export')
            paths=sorted(self.state_dir.glob('native-cache-*.json'),key=lambda p:p.stat().st_mtime,reverse=True)[:10]
            records=[self.get_configmaster_cache(p.stem.removeprefix('native-cache-')) for p in paths]
            return dict(caches=[{key:r[key] for key in ('cache_id','imported_at','document_sha256','summary')} for r in records])
        if type(cache_id) is not str or not re.fullmatch('[0-9a-f]{32}',cache_id):raise ValueError('invalid cache ID')
        record=json.loads((self.state_dir/f'native-cache-{cache_id}.json').read_text(encoding='utf-8'))
        summary=inspect_configmaster_cache(record['document'])
        if (record['cache_id']!=cache_id or record['summary']!=summary or
            record['document_sha256']!=hashlib.sha256(canonical(record['document'])).hexdigest()):
            raise ValueError('native cache evidence checksum/summary mismatch')
        self._check_native_identity(summary)
        if not include_document:record.pop('document')
        return record
