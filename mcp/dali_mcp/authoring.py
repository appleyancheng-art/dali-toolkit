"""Explicit physical layouts and read-only preparation of output targets."""
import hashlib
import json
import math
import re
import time
import uuid
from pydantic import BaseModel, ConfigDict, Field


class PercentChange(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, allow_inf_nan=False)
    address: int = Field(ge=0, le=63)
    percent: float | None = Field(default=None, ge=0, le=100)
    kelvin_raw: int | None = Field(default=None, ge=1000, le=10000)


GLYPHS = {
    'X': ['10000001','01000010','00100100','00011000','00011000','00100100','01000010','10000001'],
    'M': ['10000001','11000011','10100101','10011001','10000001','10000001','10000001','10000001'],
    'N': ['10000001','11000001','10100001','10010001','10001001','10000101','10000011','10000001'],
    'B': ['11111100','10000010','10000010','11111100','10000010','10000010','10000010','11111100'],
}


def validate_grid(channel, name, grid):
    if type(channel) is not int or channel not in (1,2):
        raise ValueError('channel must be 1 or 2')
    if type(name) is not str or not 1 <= len(name.strip()) <= 80:
        raise ValueError('layout name must contain 1..80 characters')
    if not isinstance(grid,list) or not 1 <= len(grid) <= 64:
        raise ValueError('address_grid requires 1..64 rows')
    if not isinstance(grid[0],list) or not 1 <= len(grid[0]) <= 64 or len(grid)*len(grid[0]) > 256:
        raise ValueError('grid requires 1..64 columns and at most 256 cells')
    addresses=[]
    for row in grid:
        if not isinstance(row,list) or len(row)!=len(grid[0]):raise ValueError('grid must be rectangular')
        for address in row:
            if address is None:continue
            if type(address) is not int or not 0<=address<=63:raise ValueError('grid cells must be address0..63 or null')
            addresses.append(address)
    if not 1 <= len(addresses) <= 64 or len(set(addresses))!=len(addresses):
        raise ValueError('grid must contain 1..64 unique short addresses')
    return sorted(addresses)


def percent_to_raw(percent, curve):
    if type(percent) not in (int,float) or not math.isfinite(percent) or not 0<=percent<=100:
        raise ValueError('percent must be a finite number 0..100')
    if percent==0:return 0,0.0
    if type(curve) is not int or curve!=0:
        raise ValueError('nonzero percent requires verified standard logarithmic curve0; other curves are unsupported')
    if percent < .1:raise ValueError('nonzero percent below the standard logarithmic minimum0.1 is not representable')
    raw=min(254,max(1,math.floor(1+253/3*(math.log10(percent)+1)+.5)))
    return raw,round(10**(3*(raw-1)/253-1),6)


class AuthoringMixin:
    def _layout_identity(self):
        return {k:self.config[k] for k in ('gateway_ip','expected_dali_mac','expected_firmware')}

    @staticmethod
    def _layout_digest(data):
        return hashlib.sha256(json.dumps({k:v for k,v in data.items() if k!='digest'},
            sort_keys=True,separators=(',',':'),ensure_ascii=False).encode('utf-8')).hexdigest()

    def create_layout(self,name,channel,address_grid):
        addresses=validate_grid(channel,name,address_grid)
        with self.lock:
            ident=uuid.uuid4().hex
            data={'schema_version':1,'layout_id':ident,'name':name.strip(),'channel':channel,
                  'address_grid':address_grid,'addresses':addresses,'rows':len(address_grid),
                  'columns':len(address_grid[0]),'orientation':'rows top-to-bottom; columns left-to-right',
                  'gateway_binding':self._layout_identity(),'created_at':time.time(),
                  'physical_mapping_verified':False}
            data['digest']=self._layout_digest(data)
            self._save('layout',data,ident)
            return data

    def _load_layout(self,ident):
        if type(ident) is not str or not re.fullmatch('[0-9a-f]{32}',ident):raise ValueError('invalid layout_id')
        data=json.loads((self.state_dir/f'layout-{ident}.json').read_text(encoding='utf-8'))
        return self._validate_layout_document(data,ident)

    def _validate_layout_document(self,data,ident):
        if not isinstance(data,dict) or data.get('schema_version')!=1 or data.get('layout_id')!=ident:
            raise ValueError('invalid layout record')
        if data.get('digest')!=self._layout_digest(data):raise ValueError('layout checksum mismatch; inspect the local record')
        if not {'channel','name','address_grid','addresses','rows','columns','gateway_binding'} <= data.keys():
            raise ValueError('incomplete layout record')
        addresses=validate_grid(data['channel'],data['name'],data['address_grid'])
        if (data['addresses']!=addresses or data['rows']!=len(data['address_grid']) or
                data['columns']!=len(data['address_grid'][0])):raise ValueError('inconsistent layout dimensions or scope')
        if data['gateway_binding']!=self._layout_identity():raise ValueError('layout belongs to a different configured gateway/firmware')
        return data

    def export_layout(self,layout_id):
        with self.lock:
            return {'format':'dali-toolkit-layout','version':1,'layout':self._load_layout(layout_id),
                    'notice':'Contains gateway identity. Checksum is integrity only, not proof of physical mapping.'}

    def import_layout(self,document):
        if not isinstance(document,dict) or document.get('format')!='dali-toolkit-layout' or type(document.get('version')) is not int or document['version']!=1:
            raise ValueError('expected dali-toolkit-layout version1 export')
        source=document.get('layout')
        if not isinstance(source,dict):raise ValueError('missing layout')
        ident=source.get('layout_id')
        if type(ident) is not str or not re.fullmatch('[0-9a-f]{32}',ident):raise ValueError('invalid source layout_id')
        with self.lock:
            source=self._validate_layout_document(source,ident)
            # New ID; never overlay an existing mapping or inherit a claim of
            # physical verification from an imported file.
            data=self.create_layout(source['name'],source['channel'],source['address_grid'])
            data['imported_from']={'layout_id':ident,'digest':source['digest'],
                'physical_mapping_verified_claim':source.get('physical_mapping_verified',False)}
            data['digest']=self._layout_digest(data)
            self._save('layout',data,data['layout_id'])
            return data

    def record_layout_check(self,layout_id,expected_digest,checked_addresses,note,checked_by):
        if not isinstance(checked_addresses,list) or not checked_addresses or any(type(a) is not int or not 0<=a<=63 for a in checked_addresses) or len(set(checked_addresses))!=len(checked_addresses):
            raise ValueError('provide unique actually checked addresses0..63')
        if type(note) is not str or not 1<=len(note.strip())<=1000 or type(checked_by) is not str or not 1<=len(checked_by.strip())<=100:
            raise ValueError('provide a check description and checker name')
        with self.lock:
            original=self._load_layout(layout_id)
            if original['digest']!=expected_digest:raise ValueError('layout changed since review')
            if not set(checked_addresses)<=set(original['addresses']):raise ValueError('checked address is outside layout')
            # Keep original immutable. The new revision preserves its mapping
            # and records exactly the human attestation supplied by the caller.
            data=json.loads(json.dumps(original))
            data.update(layout_id=uuid.uuid4().hex,created_at=time.time(),
                previous_revision={'layout_id':layout_id,'digest':expected_digest},
                physical_mapping_verified=set(checked_addresses)==set(original['addresses']),
                mapping_check={'source':'human_attestation','addresses':sorted(checked_addresses),
                    'note':note.strip(),'checked_by':checked_by.strip(),'recorded_at':time.time(),
                    'automatically_verified':False})
            data['digest']=self._layout_digest(data)
            self._save('layout',data,data['layout_id'])
            return data

    def get_layout(self,layout_id=None):
        with self.lock:
            if layout_id is not None:return self._load_layout(layout_id)
            paths=sorted(self.state_dir.glob('layout-*.json'),key=lambda p:p.stat().st_mtime,reverse=True)
            result={'layouts':[],'invalid_records':[],'total_records':len(paths),'truncated':len(paths)>50}
            for path in paths[:50]:
                ident=path.stem.removeprefix('layout-')
                try:
                    data=self._load_layout(ident)
                    result['layouts'].append({k:data[k] for k in ('layout_id','name','channel','rows','columns','addresses','created_at')})
                except (ValueError,KeyError,TypeError,OSError) as exc:
                    result['invalid_records'].append({'layout_id':ident,'error':str(exc)})
            return result

    def prepare_live_output(self,channel,changes,scene=None):
        if type(channel) is not int or channel not in (1,2):raise ValueError('channel1/2 required')
        if scene is not None and (type(scene) is not int or not 0<=scene<=15):raise ValueError('scene0..15 required')
        if not isinstance(changes,list) or not 1<=len(changes)<=64:raise ValueError('provide1..64 percentage changes')
        changes=[PercentChange.model_validate(c).model_dump(exclude_none=True) for c in changes]
        addresses=[c['address'] for c in changes]
        if len(set(addresses))!=len(addresses):raise ValueError('duplicate address')
        if any(len(c)==1 for c in changes):raise ValueError('each change needs percent or kelvin_raw')
        # Pure input errors must fail before even opening a gateway session.
        for c in changes:
            if 'percent' in c:percent_to_raw(c['percent'],0)
        with self.lock:
            g,info=self._open()
            try:
                profiles=self._cached_profiles(channel);limits=self._cached_profiles(channel,'level-limits')
                fresh_profiles=[];fresh_limits=[];resolved=[];raw_changes=[]
                for c in changes:
                    a=c['address']
                    if a not in profiles:
                        p=g.read_lamp_profile(channel,a);fresh_profiles.append(p)
                        profiles[a]={'profile':p,'captured_at':time.time(),'source':'fresh_bus_read'}
                    evidence=profiles[a];p=evidence['profile']
                    if p['light_type_code']!=1:raise ValueError('prepared realtime output currently requires DT8')
                    raw={'address':a};row={'address':a,'requested':c,'profile_evidence':evidence}
                    if 'percent' in c:
                        level,quantized=percent_to_raw(c['percent'],p['dimming_curve_code'])
                        if level:
                            if a not in limits:
                                low=g.read_lamp_parameter(channel,a,'min_level',1)['value']['level_raw']
                                high=g.read_lamp_parameter(channel,a,'max_level',1)['value']['level_raw']
                                bound={'channel':channel,'address':a,'min_level_raw':low,'max_level_raw':high}
                                fresh_limits.append(bound)
                                limits[a]={'profile':bound,'captured_at':time.time(),'source':'fresh_bus_read'}
                            bound=limits[a]['profile'];low=bound['min_level_raw'];high=bound['max_level_raw']
                            if not 1<=low<=high<=254:raise ValueError(f'address{a}: invalid configured brightness limits')
                            if not low<=level<=high:raise ValueError(f'address{a}: requested raw{level} outside configured limits{low}..{high}; no automatic clamp')
                            row['level_limits_evidence']=limits[a]
                        raw['level_raw']=level
                        row.update(quantized_percent=quantized,percent_error=round(quantized-c['percent'],6))
                    if 'kelvin_raw' in c:
                        kelvin=c['kelvin_raw']
                        if not p['min_kelvin_raw']<=kelvin<=p['max_kelvin_raw']:
                            raise ValueError(f'address{a}: Kelvin outside configured limits; no automatic clamp')
                        raw['kelvin_raw']=kelvin
                    row['raw_target']=raw;resolved.append(row);raw_changes.append(raw)
                stamp=time.time()
                if fresh_profiles:self._save('profile',{'gateway':info,'rows':fresh_profiles,'captured_at':stamp})
                if fresh_limits:self._save('level-limits',{'gateway':info,'rows':fresh_limits,'captured_at':stamp})
                ordered=sorted(addresses)
                samples=sorted({ordered[round(i*(len(ordered)-1)/3)] for i in range(4)})
                result={'state':'prepared','gateway':info,'channel':channel,'addresses':ordered,'resolved':resolved,
                        'live_arguments':{'channel':channel,'changes':raw_changes,'verify_addresses':samples},
                        'lighting_commands_sent':False,'scene_storage_modified':False,
                        'percent_meaning':'standard logarithmic command level, not measured illuminance; quantized output still requires live readback',
                        'capability_cache_max_age_seconds':86400}
                if scene is not None:
                    result['scene_changes']=[{'channel':channel,'address':r['address'],'scene':scene,
                        'level_raw':r.get('level_raw',255),'kelvin_raw':r.get('kelvin_raw',65535)} for r in raw_changes]
                    result['scene_semantics']='Targets only, not captured current output. Omitted component becomes scene MASK, preserving it when recalled. No save performed.'
                return result
            finally:g.close()

    def compose_layout_output(self,layout_id,foreground_percent,background,kelvin_raw=None,
                              glyph=None,pattern_rows=None,scene=None):
        if background not in ('off','preserve'):raise ValueError('explicit background must be off or preserve')
        if (glyph is None)==(pattern_rows is None):raise ValueError('provide exactly one of glyph or pattern_rows')
        if glyph is not None:
            if type(glyph) is not str or glyph not in GLYPHS:raise ValueError('built-in glyphs are X,M,N,B; other patterns require explicit rows')
            pattern_rows=list(GLYPHS[glyph])
        with self.lock:
            layout=self._load_layout(layout_id)
            if (not isinstance(pattern_rows,list) or len(pattern_rows)!=layout['rows'] or
                    any(type(row) is not str or len(row)!=layout['columns'] or set(row)-set('01') for row in pattern_rows)):
                raise ValueError('pattern rows must match layout dimensions and contain only0/1')
            # Validate even when no cell will consume a parameter.
            PercentChange(address=0,percent=foreground_percent,kelvin_raw=kelvin_raw)
            percent_to_raw(foreground_percent,0)
            foreground=[];background_addresses=[];changes=[]
            for row,mask in zip(layout['address_grid'],pattern_rows):
                for address,bit in zip(row,mask):
                    if address is None:
                        if bit=='1':raise ValueError('foreground cell has no mapped fixture')
                        continue
                    if bit=='1':
                        foreground.append(address);c={'address':address,'percent':foreground_percent}
                        if kelvin_raw is not None:c['kelvin_raw']=kelvin_raw
                        changes.append(c)
                    else:
                        background_addresses.append(address)
                        if background=='off':changes.append({'address':address,'percent':0})
            if not foreground:raise ValueError('pattern requires at least one foreground fixture')
            result=self.prepare_live_output(layout['channel'],changes,scene)
            result.update(layout_id=layout_id,layout_digest=layout['digest'],pattern_rows=pattern_rows,
                glyph=glyph,background=background,foreground_addresses=sorted(foreground),
                background_addresses=sorted(background_addresses),
                untouched_addresses=sorted(set(range(64))-set(result['addresses'])),
                physical_mapping_verified=layout.get('physical_mapping_verified',False),
                mapping_check=layout.get('mapping_check'),automatically_verified=False)
            return result
