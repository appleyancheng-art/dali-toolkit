import hashlib
import json
import threading
import time
import uuid
from . import __version__
from pathlib import Path
from pydantic import BaseModel, ConfigDict, Field
from .protocol import Gateway, ProtocolError
from .authoring import AuthoringMixin
from .projects import ProjectMixin
from .group_scenes import GroupSceneMixin
from .lamp_scenes import LampSceneMixin
from .diagnostics import DiagnosticsMixin
from .native_cache import NativeCacheMixin
from .native_names import NativeNameMixin
from .progress import publish_saved
from .relative_dim import RelativeDimMixin
from .level_extreme import LevelExtremeMixin
from .project_floorplan import ProjectFloorplanMixin

class OperationCancelled(Exception):
    pass

class SceneChange(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    channel: int = Field(ge=1, le=2)
    address: int = Field(ge=0, le=63)
    scene: int = Field(ge=0, le=15)
    level_raw: int = Field(ge=0, le=255, description="0=OFF, 1..254=DALI raw level, 255=MASK; not percentage")
    kelvin_raw: int = Field(ge=0, le=65535, description="Kelvin; 65535=MASK. v0.1 allows 1000..10000 or MASK.")

class LiveChange(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    address: int = Field(ge=0,le=63)
    level_raw: int | None = Field(default=None,ge=0,le=254,description="DALI raw level, not percentage; omit to preserve brightness")
    kelvin_raw: int | None = Field(default=None,ge=1000,le=10000,description="Kelvin; omit to preserve color temperature")

class Service(AuthoringMixin, ProjectMixin, GroupSceneMixin, LampSceneMixin, DiagnosticsMixin, NativeCacheMixin, NativeNameMixin, RelativeDimMixin, LevelExtremeMixin, ProjectFloorplanMixin):
    def __init__(self, config, state_dir, gateway_factory=None):
        self.config = config
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.gateway_factory = gateway_factory or (lambda: Gateway(
            config["gateway_ip"], config["local_ip"], config.get("listen_port", 19502),
            config.get("timeout_seconds", 4)))

    def _open(self):
        g = self.gateway_factory()
        try:
            info = g.discover()
            if info["dali_mac"].lower() != self.config["expected_dali_mac"].lower():
                raise RuntimeError("Gateway identity mismatch; no lamp commands sent")
            if info["dali_firmware"] != self.config["expected_firmware"]:
                raise RuntimeError("Unvalidated gateway firmware; no lamp commands sent")
            g.connect()
            return g, info
        except Exception:
            g.close()
            raise

    def status(self):
        return {"project_restore_writes_enabled": self.config.get("enable_project_restore") is True, "version": __version__, "gateway_ip": self.config["gateway_ip"],
                "address_allocation_writes_enabled": self.config.get("enable_experimental_address_allocation") is True,
                "address_allocation": "Background complete DT6/DT8 review, exact channel/digest gate, one incremental channel-wide allocation and independent preservation/count readback. Expected count is NOT a device limit. No reset, replay or automatic undo; public B63 incremental allocation and independent full-board audit verified on the known DT8 board; DT6/multiple-new-gear remain simulated.",
                "address_removal_writes_enabled": self.config.get("enable_experimental_address_removal") is True,
                "address_removal": "Single DT8 short-address removal, full supported backup, exact plan gate, single attempt and independent occupancy readback. No automatic allocation/undo, physical identity proof or host mapping changes; public B63 removal field trial verified; recovery used a separate controlled test harness, not a public automatic undo.",
                "project_delivery_writes_enabled": self.config.get("enable_project_delivery") is True,
                "project_delivery": "Staged device parameters/scenes/memberships then one scoped local names/floorplan commit, final readback, progress/cancel and conditional undo. Exact coordinator and child gates required. No atomic cross-domain commit, automatic replay, UI proof or addressing/gateway/grid activation.",
                "project_names": "Immutable schema4 local label attachments, portable without native source files; selected name restoration binds source digest and uses closed-app native file transactions with conditional undo. No device commands or complete engineering orchestration.",
                "address_metadata_writes_enabled": self.config.get('enable_native_address_metadata_writes') is True,
                "address_metadata": "Separate post-move local label/marker file migration and conditional undo with fresh bus mapping/configuration checks; immutable selected grid revision clears physical claim. Source move evidence, configured file and closed app required. No parameter/group cache migration, archived project mutation or automatic physical verification.",
                "address_writes_enabled": self.config.get('enable_experimental_address_writes') is True,
                "address_moves": "Explicit DT8 single-address move to vacant0..63: closed-app Windows guard, fresh scan/duplicate report and full supported lamp backup, exact gate, single-attempt journal, cache invalidation, independent readback and conditional reverse plan. No allocation/reset/swap in move tools; deletion uses a separate controller, hardware-identity guarantee or host name/layout migration. Reprepare other old address-bound plans after topology changes.",
                "duplicate_addresses": "Read gateway-reported F043 bitmap for explicit channel1/2; no allocation or new scan; empty result does not prove physical uniqueness or bitmap freshness; unknown is not empty.",
                "native_floorplans": "Read native cache markers; explicit local PNG attachment creates portable schema3 project with exact image bytes and capture-scoped marks, preserving grids. Separate closed-app native-file restoration uses explicit mark scope, a new PNG, shared cache lock and conditional undo; no device traffic, grid conversion or physical verification. PNG <=2MB, noninterlaced, <=4M pixels; caller-selected image provenance unverified.",
                "native_workbooks": "Nine-sheet XLSX evidence and source-bound selected scenes/group bits/single or multi-lamp parameters, plus combined parameter/scene/membership restoration plans with embedded source bytes and conditional undo; fresh originals, explicit source scope and percentage policy; no ConfigMaster application import or complete-project restoration; one native UI local-import sample and cache quantization verified; consult release validation for limited hardware scope and remaining native UI paths",
                "parameter_batches": "Explicit1..64 DT8 addresses and1..9 fields each; includes raw0 logarithmic/raw1 linear curve (B0 curve0->1->0 storage verified; photometric response unmeasured); all-target originals/preflight, serial writes with readback, background progress/cancel and conditional attempted-field restoration; separate exact multi-address allowlist; keep server alive; consult release-specific physical scope",
                "native_blink": "Finite single-address native max/off cycles, cancel and original-brightness restore;<=60s requested sequence; fast phases not physically sampled; no automatic replay; B0 slow4 phases read back, fast6 receipts and final restoration only, active cancellation restored raw200; UI clicks and physical fast-cycle timing unverified",
                "relative_dimming": "One native single-address up/down command with fresh original and independent output observations; no fixed percentage step, automatic replay or restoration; B0 raw200->209->200 at4000K verified on pinned firmware1.6.9; other settings/devices not physically validated",
                "connectivity": "not_checked", "network_traffic": False,
                "fast_paths": "dali_set_channel_live_output for explicitly authorized uniform entire-channel preview; dali_save_scene_batch for authorized save with fresh backup and full readback",
                "writes_enabled": self.config.get("enable_experimental_writes", False),
                "group_writes_enabled": self.config.get("enable_experimental_group_writes", False),
                "fade_time_writes_enabled": self.config.get("enable_experimental_parameter_writes", False),
                "parameter_writes_enabled": self.config.get("enable_experimental_parameter_writes", False),
                "parameter_write_fields": ["fade_time", "fade_rate", "min_level", "max_level", "configured_min_ct", "configured_max_ct", "power_on", "system_failure", "dimming_curve"],
                "scene_write_validation": "physical DT8 scene write/readback validated on pinned firmware 1.6.9; Kelvin quantization observed",
                "limits": "DT8 scene storage; realtime single-address/batch/channel/group brightness and CT; channel/group scene recall; scoped group membership; bounded identification; existing-address scan and configuration reads; single-field fade, brightness/CT limits and power/failure brightness-color transactions plus project-bound selected multi-field restoration for one lamp; omitted pair components preserved, brightness255 unsupported; CT range expansion only by evidence-bound restore; single-address vacant-target move with conditional restoration; separately gated single DT8 address removal and channel-wide incremental allocation; no reset/full reallocation/swap or gateway settings",
                "authoring_workflow": "Set live output first, tune the visible effect, then save scene storage. Do not write a scene merely to preview lighting.",
                "target_preparation": "Explicit persisted physical layouts; standard-logarithmic percentage targets checked against configured limits; X/M/N/B or custom bitmaps. Preparation reads capabilities but never controls or stores scenes.",
                "project_workflow": "Fresh scoped toolkit JSON capture/import/export/diff; schema2 attaches explicit layout snapshots as source evidence in a new revision, with separate unverified local mapping extraction. Selected-scene and selected-group-bit restore plans use separately authorized apply/backup/readback. Combined parameter/scene/group project preflight reads one serial fresh scope and checks scenes against projected selected parameter limits; separate experimental source-bound combined execution and conditional undo use exact plan and category gates; see validation for physical scope. Not ConfigMaster project compatibility; no automatic device restoration.",
                "native_cache_workflow": "ConfigMaster1.2.4.1 cache evidence and name drafts; explicit configured-file name transactions with closed-app guard, byte backup/readback and conflict-checked restore; no live-state trust or native device restoration. Actual cache three-name apply/restart display/restore/restart display verified on PC1.2.4.1; other versions and concurrent app launch unsupported.",
                "native_floorplan_writes_enabled": self.config.get("enable_native_floorplan_writes") is True,
                "native_cache_writes_enabled": self.config.get('enable_native_cache_writes') is True,
                "native_cache_file_configured": bool(self.config.get('configmaster_cache_file')),
                "scene_names": "PC-side labels can be written only through the separate configured native-file transaction; never DALI scene parameters"}

    def discover(self):
        with self.lock:
            g = self.gateway_factory()
            try:
                info = g.discover()
                info["expected_identity"] = info["dali_mac"].lower() == self.config["expected_dali_mac"].lower()
                return info
            finally:
                g.close()

    def scan_existing(self,channel):
        if type(channel) is not int or channel not in (1,2):
            raise ValueError('explicit channel 1 or 2 required')
        with self.lock:
            started=time.monotonic()
            g,info=self._open()
            report={'gateway':info,'channel':channel,'state':'scanning',
                    'started_at':time.time(),'address_allocation':False,
                    'scene_storage_modified':False,'lighting_commands_sent':False}
            ident=self._save('scan',report)
            report['operation_id']=ident
            try:
                result=g.scan_existing(channel,self.config.get('scan_timeout_seconds',240.0))
                report.update(result,state='completed',captured_at=time.time())
            except (TimeoutError,ProtocolError,OSError) as exc:
                # A scan timeout is not an empty bus. Never turn missing evidence
                # into "all offline", or silently resend a long-running scan.
                report.update(state='scan_unknown',error=str(exc),protocol_replies=list(g.recent_packets))
            finally:
                report['elapsed_seconds']=round(time.monotonic()-started,3)
                self._save('scan',report,ident)
                g.close()
            return report

    def read_duplicate_addresses(self,channel):
        if type(channel) is not int or channel not in (1,2):raise ValueError('explicit channel 1 or 2 required')
        timeout=self.config.get('duplicate_query_timeout_seconds',10.0)
        if type(timeout) not in (int,float) or not .05<=timeout<=60:raise ValueError('duplicate query timeout must be .05..60 seconds')
        with self.lock:
            report=dict(channel=channel,state='querying',started_at=time.time(),
                        address_allocation=False,lighting_commands_sent=False,
                        physical_uniqueness_verified=False,bitmap_freshness_verified=False)
            ident=self._save('duplicate-addresses',report);report['operation_id']=ident
            g=None;started=time.monotonic()
            try:
                g,info=self._open();report['gateway']=info
                report.update(g.read_duplicate_addresses(channel,timeout),state='completed',captured_at=time.time())
            except (TimeoutError,RuntimeError,OSError) as exc:
                report.update(state='query_unknown',error=str(exc),protocol_replies=list(g.recent_packets) if g else [])
            finally:
                if g is not None:g.close()
                report['elapsed_seconds']=round(time.monotonic()-started,3)
                self._save('duplicate-addresses',report,ident)
            return report

    def read_groups(self,channel,addresses):
        if type(channel) is not int or channel not in (1,2):raise ValueError('channel 1/2 required')
        if (not 1<=len(addresses)<=64 or len(set(addresses))!=len(addresses)
                or any(type(a) is not int or not 0<=a<64 for a in addresses)):
            raise ValueError('provide 1..64 unique short addresses')
        with self.lock:
            started=time.monotonic();g,info=self._open()
            report={'gateway':info,'channel':channel,'requested_addresses':addresses,
                    'scope':'explicit_short_addresses','rows':[],'state':'reading','started_at':time.time()}
            ident=self._save('groups',report);report['snapshot_id']=ident
            try:
                for address in addresses:
                    report['pending_address']=address
                    self._save('groups',report,ident)
                    report['rows'].append(g.read_groups(channel,address))
                    report.pop('pending_address',None)
                    self._save('groups',report,ident)
                report.update(state='completed',captured_at=time.time())
            except (TimeoutError,ProtocolError,OSError) as exc:
                report.update(state='stopped_partial',error=str(exc),protocol_replies=list(g.recent_packets))
            finally:
                report['elapsed_seconds']=round(time.monotonic()-started,3)
                self._save('groups',report,ident);g.close()
            return report

    def read_lamp_parameters(self,channel,addresses):
        """Fresh sequential parameters with per-field partial evidence.

        This is not an atomic snapshot and does not update the capability
        cache; consumers must not mistake old cache entries for these reads.
        """
        if type(channel) is not int or channel not in (1,2):raise ValueError('channel 1/2 required')
        if (not 1<=len(addresses)<=16 or any(type(a) is not int or not 0<=a<64 for a in addresses)
                or len(set(addresses))!=len(addresses)):
            raise ValueError('provide 1..16 unique short addresses; split larger parameter reads')
        with self.lock:
            started=time.monotonic();g,info=self._open()
            report={'gateway':info,'channel':channel,'requested_addresses':addresses,
                    'state':'reading','started_at':time.time(),'rows':[],
                    'source':'fresh_sequential_bus_reads','atomic_snapshot':False,
                    'fields':list(Gateway.PARAMETER_FIELDS),
                    'not_included':['scenes','live_output','physical_ct_limits','extended_fade','device_version'],
                    'device_writes':False}
            ident=self._save('parameters',report);report['snapshot_id']=ident
            try:
                for address in addresses:
                    row={'channel':channel,'address':address,'state':'reading','parameters':{}}
                    report['rows'].append(row)
                    typ=None
                    for field in Gateway.PARAMETER_FIELDS:
                        report['pending']={'address':address,'field':field}
                        self._save('parameters',report,ident)
                        result=g.read_lamp_parameter(channel,address,field,typ)
                        result['captured_at']=time.time()
                        row['parameters'][field]=result
                        if field=='light_type':typ=result['value']['code']
                        self._save('parameters',report,ident)
                    row['state']='completed'
                    report.pop('pending',None)
                report.update(state='completed',captured_at=time.time())
            except (TimeoutError,ProtocolError,OSError) as exc:
                row['state']='partial'
                report.update(state='stopped_partial',error=str(exc),protocol_replies=list(g.recent_packets))
            finally:
                report['elapsed_seconds']=round(time.monotonic()-started,3)
                self._save('parameters',report,ident);g.close()
            return report

    def recall_scene(self, channel, scene, verify_addresses):
        if type(channel) is not int or channel not in (1,2) or type(scene) is not int or not 0 <= scene <=15:
            raise ValueError("channel 1/2 and scene 0..15 required")
        if not 1 <= len(verify_addresses) <= 8 or len(set(verify_addresses))!=len(verify_addresses):
            raise ValueError("provide 1..8 unique verification addresses")
        if any(type(a) is not int or not 0 <= a <= 63 for a in verify_addresses):
            raise ValueError("invalid verification address")
        with self.lock:
            started=time.monotonic()
            g,info=self._open()
            report={"gateway":info,"channel":channel,"scene":scene,"scope":"entire_channel",
                    "scene_storage_modified":False,"sent_count":0,"verification":[],"created_at":time.time(),
                    "receipt_status":"not_sent","verification_status":"not_started"}
            ident=self._save("recall",report)
            try:
                targets=[]
                profiles=self._cached_profiles(channel)
                for address in verify_addresses:
                    typ=profiles[address]['profile']['light_type_code'] if address in profiles else g.light_type(channel,address)
                    if typ!=1:raise ValueError('scene recall verification currently requires DT8 samples')
                    expected=g.read_scene(channel,address,scene,typ)
                    before=None
                    targets.append((address,typ,expected,before))
                report["state"]="dispatching"
                self._save("recall",report,ident)
                report["sent_count"]=1
                try:
                    report["receipt"]=g.recall_channel_scene(channel,scene)
                    report["state"]="gateway_reply_received"
                    report["receipt_status"]="received"
                except (TimeoutError,ProtocolError) as exc:
                    report["state"]="outcome_unknown"
                    report["receipt_status"]="unknown"
                    report["dispatch_error"]=str(exc)
                    report["protocol_replies"]=list(g.recent_packets)
                self._save("recall",report,ident)
                for address,typ,expected,before in targets:
                    target={k:expected[k] for k,mask in (('level_raw',255),('kelvin_raw',65535)) if expected[k]!=mask}
                    row=self._wait_live_target(g,channel,address,target)
                    row.update(expected=expected,before=before,verifiable=bool(target))
                    row['matches_scene']=bool(target) and row.get('matches_target',False)
                    report["verification"].append(row)
                    self._save("recall",report,ident)
                checked=[r for r in report['verification'] if r['verifiable']]
                report["samples_verified"]=bool(checked) and all(r['matches_scene'] for r in checked)
                report['verified_sample_count']=sum(r['matches_scene'] for r in checked)
                report['verification_status']='samples_verified' if report['samples_verified'] else ('unverifiable' if not checked else 'not_verified')
                report['state']='samples_verified' if report['samples_verified'] else ('receipt_received_unverified' if report['receipt_status']=='received' else 'outcome_unknown')
                report['elapsed_seconds']=round(time.monotonic()-started,3)
                report["operation_id"]=ident
                self._save("recall",report,ident)
                return report
            finally:
                g.close()

    def read_actual(self,channel,addresses):
        if type(channel) is not int or channel not in (1,2):
            raise ValueError("channel must be 1 or 2")
        if not 1<=len(addresses)<=64 or len(set(addresses))!=len(addresses) or any(type(a) is not int or not 0<=a<=63 for a in addresses):
            raise ValueError("provide 1..64 unique short addresses")
        with self.lock:
            g,info=self._open()
            try:
                profiles=self._cached_profiles(channel)
                rows=[g.read_actual(channel,a,(profiles[a]['profile']['light_type_code'] if a in profiles else g.light_type(channel,a))==1) for a in addresses]
                result={"gateway":info,"rows":rows,"captured_at":time.time(),"source":"live_F047"}
                result['snapshot_id']=self._save('actual',result)
                return result
            finally:g.close()

    def read_profiles(self,channel,addresses):
        if type(channel) is not int or channel not in (1,2):raise ValueError("channel 1/2 required")
        if not 1<=len(addresses)<=64 or len(set(addresses))!=len(addresses) or any(type(a) is not int or not 0<=a<=63 for a in addresses):
            raise ValueError("provide 1..64 unique short addresses")
        with self.lock:
            g,info=self._open()
            try:
                rows=[g.read_lamp_profile(channel,a) for a in addresses]
                fade_rows=[g.read_fade_timing(channel,a) for a in addresses]
                level_rows=[{'channel':channel,'address':a,
                    'min_level_raw':g.read_lamp_parameter(channel,a,'min_level',1)['value']['level_raw'],
                    'max_level_raw':g.read_lamp_parameter(channel,a,'max_level',1)['value']['level_raw']} for a in addresses]
                result={"gateway":info,"rows":rows,"fade_rows":fade_rows,"level_limits_rows":level_rows,"captured_at":time.time()}
                result['level_limits_snapshot_id']=self._save('level-limits',{'gateway':info,'rows':level_rows,
                                                           'captured_at':result['captured_at']})
                result['fade_snapshot_id']=self._save('fade',{'gateway':info,'rows':fade_rows,
                                                           'captured_at':result['captured_at']})
                result['snapshot_id']=self._save('profile',result)
                return result
            finally:g.close()

    def _cached_profiles(self, channel, prefix='profile'):
        """Reuse only recent profiles from this pinned gateway; live values are never cached."""
        rows={}
        now=time.time()
        invalidated={}
        for path in self.state_dir.glob('capability-invalidation-*.json'):
            try:
                marker=json.loads(path.read_text(encoding='utf-8'))
                bound=marker['gateway']
                if any(type(bound[k]) is not str for k in ('ip','dali_mac','dali_firmware')):return {}
                if (bound['ip']!=self.config['gateway_ip'] or
                    bound['dali_mac'].lower()!=self.config['expected_dali_mac'].lower() or
                    bound['dali_firmware']!=self.config['expected_firmware']):continue
                stamp=marker['invalidated_at']
                if type(stamp) not in (int,float) or not 0<stamp<float('inf'):return {}
                if type(marker['channel']) is not int or marker['channel'] not in (1,2):return {}
                addresses=marker['addresses']
                if (not isinstance(addresses,list) or not 1<=len(addresses)<=64 or
                    any(type(a) is not int or not 0<=a<=63 for a in addresses) or len(set(addresses))!=len(addresses)):return {}
                if marker['channel']==channel:
                    for address in addresses:invalidated[address]=max(stamp,invalidated.get(address,0))
            except (ValueError,KeyError,TypeError,OSError):return {}  # Unreadable invalidation cannot justify old capabilities.
        for path in self.state_dir.glob(f'{prefix}-*.json'):
            try:
                data=json.loads(path.read_text(encoding='utf-8'))
                stamp=data['captured_at']; info=data['gateway']
                if not 0<=now-stamp<=86400:continue
                if (info['ip']!=self.config['gateway_ip'] or
                    info['dali_mac'].lower()!=self.config['expected_dali_mac'].lower() or
                    info['dali_firmware']!=self.config['expected_firmware']):continue
                for row in data['rows']:
                    key=row['address']
                    if row['channel']==channel and stamp>invalidated.get(key,0) and (key not in rows or rows[key]['captured_at']<stamp):
                        rows[key]={'profile':row,'captured_at':stamp,'snapshot':path.name}
            except (ValueError,KeyError,TypeError):continue
        return rows

    def invalidate_capabilities(self,channel,addresses,reason):
        """Publish before a parameter write; no cache deletion and no bus traffic."""
        if type(channel) is not int or channel not in (1,2):raise ValueError('channel1/2 required')
        if (not isinstance(addresses,list) or not 1<=len(addresses)<=64 or
            any(type(a) is not int or not 0<=a<=63 for a in addresses) or len(set(addresses))!=len(addresses)):
            raise ValueError('explicit unique addresses0..63 required')
        with self.lock:
            marker={'gateway':{'ip':self.config['gateway_ip'],'dali_mac':self.config['expected_dali_mac'],
                               'dali_firmware':self.config['expected_firmware']},
                    'channel':channel,'addresses':addresses,'reason':str(reason),'invalidated_at':time.time()}
            return self._save('capability-invalidation',marker)

    @staticmethod
    def _same_scene(a,b):
        keys=('channel','address','scene','light_type_code','level_raw')
        if any(a[k]!=b[k] for k in keys):return False
        x,y=a['kelvin_raw'],b['kelvin_raw']
        return x==y or (0<x<65535 and 0<y<65535 and 1000000//x==1000000//y)

    def get_live_operation(self,kind,operation_id=None):
        if kind not in ('live-output','live-channel','live-group'):raise ValueError('invalid live operation kind')
        if operation_id is None:
            paths=sorted(self.state_dir.glob(f'{kind}-*.json'),key=lambda p:p.stat().st_mtime,reverse=True)[:10]
            return {'operations':[json.loads(p.read_text(encoding='utf-8')) for p in paths],
                    'notice':'Saved phase is not process liveness. Never resume or resend automatically.'}
        if type(operation_id) is not str or len(operation_id)!=32 or any(c not in '0123456789abcdef' for c in operation_id):
            raise ValueError('invalid operation_id')
        return json.loads((self.state_dir/f'{kind}-{operation_id}.json').read_text(encoding='utf-8'))

    def _live_checkpoint(self,kind,report,phase,completed,total,address=None,source=None):
        # Coalesce cache-only checkpoints; publish before every real bus wait.
        if source=='cache' and completed%8:return
        report['progress']={'phase':phase,'completed':completed,'total':total,
            'pending_address':address,'source':source,'updated_at':time.time()}
        self._save(kind,report,report['operation_id'])

    def set_channel_live_output(self,channel,level_raw,kelvin_raw,verify_addresses):
        """Opt-in broadcast ONLY when the user's scope is the entire channel."""
        if type(channel) is not int or channel not in (1,2):raise ValueError('channel 1/2 required')
        target=LiveChange(address=0,level_raw=level_raw,kelvin_raw=kelvin_raw).model_dump(exclude_none=True)
        target.pop('address')
        if not target:raise ValueError('brightness or Kelvin required')
        if not 1<=len(verify_addresses)<=8 or len(set(verify_addresses))!=len(verify_addresses) or any(type(a) is not int or not 0<=a<=63 for a in verify_addresses):
            raise ValueError('provide 1..8 unique verification addresses')
        with self.lock:
            started=time.monotonic();g,info=self._open()
            report={'channel':channel,'scope':'entire_channel','target':target,'gateway':info,
                    'commands':[],'verification':[],'created_at':time.time(),
                    'scene_storage_modified':False,'scene_recalled':False,'state':'preflight'}
            ident=self._save('live-channel',report);report['operation_id']=ident
            try:
                profiles=self._cached_profiles(channel);fresh=[]
                for a in range(64):
                    self._live_checkpoint('live-channel',report,'profiles',a,64,a,'cache' if a in profiles else 'bus')
                    p=profiles[a]['profile'] if a in profiles else g.read_lamp_profile(channel,a)
                    if a not in profiles:fresh.append(p)
                    if p['light_type_code']!=1:raise ValueError('channel broadcast requires all 64 confirmed DT8 profiles')
                    if kelvin_raw is not None and not p['min_kelvin_raw']<=kelvin_raw<=p['max_kelvin_raw']:
                        raise ValueError(f'address {a}: Kelvin outside limits')
                self._live_checkpoint('live-channel',report,'profiles',64,64)
                if fresh:self._save('profile',{'gateway':info,'rows':fresh,'captured_at':time.time()})
                wait=0
                if len(target)==2:
                    fades=self._cached_profiles(channel,'fade');fresh=[];bounds=[]
                    for a in range(64):
                        self._live_checkpoint('live-channel',report,'fade_bounds',a,64,a,'cache' if a in fades else 'bus')
                        p=fades[a]['profile'] if a in fades else g.read_fade_timing(channel,a)
                        if a not in fades:fresh.append(p)
                        bounds.append(p['fade_seconds_upper'])
                    if fresh:self._save('fade',{'gateway':info,'rows':fresh,'captured_at':time.time()})
                    if any(x is None or x>8.1 for x in bounds):
                        raise ValueError('Unknown/long fade: use per-address live output; no broadcast sent')
                    wait=max(bounds)+.25
                    report['fade_guard_seconds']=wait
                    self._live_checkpoint('live-channel',report,'fade_bounds',64,64)
                report['preflight_seconds']=round(time.monotonic()-started,3)
                report['state']='dispatching'
                self._live_checkpoint('live-channel',report,'dispatch',0,len(target))
                failed=False
                for component in ('kelvin_raw','level_raw'):
                    if component not in target:continue
                    if component=='level_raw' and kelvin_raw is not None:
                        # Bounds cover ALL fixtures, not merely the sampled lamps.
                        self._live_checkpoint('live-channel',report,'color_settling',0,len(verify_addresses))
                        time.sleep(wait)
                        report['color_settling']=[self._wait_live_target(g,channel,a,{'kelvin_raw':kelvin_raw}) for a in verify_addresses]
                        if not all(r.get('matches_target',False) for r in report['color_settling']):
                            report['stop_reason']='Color verification failed; brightness broadcast not sent';failed=True;break
                    item={'component':component,'value':target[component],'status':'outcome_unknown'}
                    report['commands'].append(item);self._save('live-channel',report,ident)
                    try:
                        item['receipt']=g.set_channel_live_component(channel,component,target[component]);item['status']='reply_received'
                    except (TimeoutError,ProtocolError,OSError) as exc:
                        item['error']=str(exc);failed=True
                    self._save('live-channel',report,ident)
                    if failed:break
                report['dispatch_seconds']=round(time.monotonic()-started,3)
                self._live_checkpoint('live-channel',report,'verification',0,len(verify_addresses))
                report['verification']=[self._wait_live_target(g,channel,a,target) for a in verify_addresses]
                report['samples_verified']=all(r.get('matches_target',False) for r in report['verification'])
                report['state']='stopped_partial' if failed else 'dispatched'
                report['elapsed_seconds']=round(time.monotonic()-started,3)
                self._live_checkpoint('live-channel',report,report['state'],len(report['verification']),len(verify_addresses))
                return report
            except Exception as exc:
                report.update(state='stopped_partial' if report['commands'] else 'preflight_failed',error=str(exc),elapsed_seconds=round(time.monotonic()-started,3))
                self._save('live-channel',report,ident)
                raise
            finally:g.close()

    def set_group_live_output(self,channel,group,expected_addresses,verify_addresses,level_raw=None,kelvin_raw=None):
        """Control an explicit group only after fresh discovered membership agrees."""
        if type(channel) is not int or channel not in (1,2) or type(group) is not int or not 0<=group<=15:
            raise ValueError('channel 1/2 and group 0..15 required')
        if (not 1<=len(expected_addresses)<=64 or any(type(a) is not int or not 0<=a<64 for a in expected_addresses)
                or len(set(expected_addresses))!=len(expected_addresses)):
            raise ValueError('provide 1..64 unique expected group addresses')
        if (not 1<=len(verify_addresses)<=8 or any(type(a) is not int or a not in expected_addresses for a in verify_addresses)
                or len(set(verify_addresses))!=len(verify_addresses)):
            raise ValueError('provide 1..8 unique verification addresses within expected group')
        target=LiveChange(address=0,level_raw=level_raw,kelvin_raw=kelvin_raw).model_dump(exclude_none=True)
        target.pop('address')
        if not target:raise ValueError('brightness or Kelvin required')
        with self.lock:
            started=time.monotonic();g,info=self._open()
            report={'gateway':info,'channel':channel,'group':group,'scope':'explicit_group',
                    'expected_addresses':sorted(expected_addresses),'target':target,'commands':[],
                    'membership_rows':[],'verification':[],'state':'preflight','started_at':time.time(),
                    'scene_storage_modified':False,'group_membership_modified':False,
                    'membership_atomic':False,'samples_verified':False}
            ident=self._save('live-group',report);report['operation_id']=ident
            try:
                report['scan']=g.scan_existing(channel,self.config.get('scan_timeout_seconds',240.0))
                self._save('live-group',report,ident)
                # Firmware1.6.9 can acknowledge the first membership request
                # after a completed scan without returning its final result.
                # Start a fresh pinned session before issuing any membership
                # query, rather than timing out and replaying that query.
                g.close()
                g,report['membership_gateway']=self._open()
                members=[]
                for address in report['scan']['addresses']:
                    report['pending']={'phase':'membership','address':address}
                    self._save('live-group',report,ident)
                    row=g.read_groups(channel,address);report['membership_rows'].append(row)
                    if group in row['groups']:members.append(address)
                report['actual_members']=members
                if sorted(members)!=sorted(expected_addresses):
                    report.update(state='scope_changed',stop_reason='Fresh discovered group membership differs; no control sent')
                    return report
                profiles=self._cached_profiles(channel);fresh=[];fades=self._cached_profiles(channel,'fade');fresh_fades=[];bounds=[]
                for address in members:
                    report['pending']={'phase':'capabilities','address':address}
                    self._save('live-group',report,ident)
                    p=profiles[address]['profile'] if address in profiles else g.read_lamp_profile(channel,address)
                    if address not in profiles:fresh.append(p)
                    if p['light_type_code']!=1:raise ValueError('group preview currently requires all members confirmed DT8')
                    if kelvin_raw is not None and not p['min_kelvin_raw']<=kelvin_raw<=p['max_kelvin_raw']:
                        raise ValueError(f'address {address}: Kelvin outside limits')
                    if len(target)==2:
                        fade=fades[address]['profile'] if address in fades else g.read_fade_timing(channel,address)
                        if address not in fades:fresh_fades.append(fade)
                        bounds.append(fade['fade_seconds_upper'])
                if fresh:self._save('profile',{'gateway':info,'rows':fresh,'captured_at':time.time()})
                if fresh_fades:self._save('fade',{'gateway':info,'rows':fresh_fades,'captured_at':time.time()})
                if bounds:
                    if any(x is None or x>8.1 for x in bounds):raise ValueError('unknown/long member fade; no group control sent')
                    report['fade_guard_seconds']=max(bounds)+.25
                report.pop('pending',None)
                report['preflight_seconds']=round(time.monotonic()-started,3)
                failed=False
                for component in ('kelvin_raw','level_raw'):
                    if component not in target:continue
                    if component=='level_raw' and kelvin_raw is not None:
                        time.sleep(report['fade_guard_seconds'])
                        report['color_settling']=[self._wait_live_target(g,channel,a,{'kelvin_raw':kelvin_raw}) for a in verify_addresses]
                        if not all(r.get('matches_target',False) for r in report['color_settling']):
                            report['stop_reason']='Color did not settle; group brightness not sent';failed=True;break
                    item={'component':component,'value':target[component],'status':'outcome_unknown'}
                    report['commands'].append(item);report['state']='dispatching'
                    self._save('live-group',report,ident)
                    try:
                        item['receipt']=g.set_group_live_component(channel,group,component,target[component])
                        item['status']='reply_received'
                    except (TimeoutError,ProtocolError,OSError) as exc:
                        item['error']=str(exc);item['protocol_replies']=list(g.recent_packets);failed=True
                    self._save('live-group',report,ident)
                    if failed:break
                report['dispatch_seconds']=round(time.monotonic()-started,3)
                report['verification']=[self._wait_live_target(g,channel,a,target) for a in verify_addresses]
                report['samples_verified']=all(r.get('matches_target',False) for r in report['verification'])
                report['state']='stopped_partial' if failed else 'dispatched'
            except (TimeoutError,ProtocolError,OSError,ValueError,RuntimeError) as exc:
                report.update(state='stopped_partial' if report['commands'] else 'preflight_failed',error=str(exc),
                              protocol_replies=list(g.recent_packets))
            finally:
                report['elapsed_seconds']=round(time.monotonic()-started,3)
                self._save('live-group',report,ident);g.close()
            return report

    def validate_scene_changes(self,changes):
        """Pure scope/schema checks; shared by synchronous and job entrypoints."""
        if not self.config.get('enable_experimental_writes',False):raise PermissionError('Scene storage writes disabled')
        if not isinstance(changes,list) or not 1<=len(changes)<=1024:raise ValueError('provide 1..1024 changes')
        changes=[SceneChange.model_validate(c).model_dump() for c in changes]
        if len({(c['channel'],c['address'],c['scene']) for c in changes})!=len(changes):raise ValueError('duplicate target')
        self._check_scene_scope(changes)
        for c in changes:
            if c['kelvin_raw']!=65535 and not 1000<=c['kelvin_raw']<=10000:raise ValueError('invalid Kelvin')
        return changes

    def _check_scene_scope(self,changes):
        # Missing key preserves legacy explicitly-enabled behavior. An empty or
        # malformed configured scope must never silently mean unrestricted.
        if 'allowed_write_targets' not in self.config:return
        scopes=self.config['allowed_write_targets']
        if isinstance(scopes,dict):scopes=[scopes]
        if not isinstance(scopes,list) or not scopes:raise PermissionError('empty allowed write targets')
        allowed=set()
        for scope in scopes:
            if not isinstance(scope,dict) or set(scope)!={'channel','scene','addresses'}:
                raise PermissionError('invalid allowed write targets')
            ch,scene,addresses=scope['channel'],scope['scene'],scope['addresses']
            if (type(ch) is not int or ch not in (1,2) or type(scene) is not int or not 0<=scene<=15
                    or not isinstance(addresses,list) or not 1<=len(addresses)<=64
                    or any(type(a) is not int or not 0<=a<=63 for a in addresses)
                    or len(set(addresses))!=len(addresses)):
                raise PermissionError('invalid allowed write targets')
            allowed.update((ch,a,scene) for a in addresses)
        if any((c['channel'],c['address'],c['scene']) not in allowed for c in changes):
            raise PermissionError('outside allowed write targets')

    def save_scene_batch(self,changes,progress=None,cancelled=None):
        """Fresh backup -> serial write/readback, with optional job checkpoints.

        Cancellation never interrupts a sent write's readback or rolls it back.
        """
        changes=self.validate_scene_changes(changes)
        with self.lock:
            started=time.monotonic();g,info=self._open()
            plan={'gateway':info,'created_at':time.time(),'state':'preparing','rows':[],'results':[],
                  'mode':'authorized_single_session','scene_recalled':False,
                  'timing_seconds':{'write_receipts':0.0,'readback':0.0}}
            ident=self._save('plan',plan);plan['plan_id']=ident
            def checkpoint(phase,check_cancel=True):
                scene_progress=[]
                for ch,scene in sorted({(c['channel'],c['scene']) for c in changes}):
                    scene_results=[r for r in plan['results'] if r['target']['channel']==ch and r['target']['scene']==scene]
                    scene_progress.append({'channel':ch,'scene':scene,
                        'total':sum(c['channel']==ch and c['scene']==scene for c in changes),
                        'backed_up':sum(r['after']['channel']==ch and r['after']['scene']==scene for r in plan['rows']),
                        'completed':sum(r['status'] in ('verified','unchanged') for r in scene_results),
                        'attempted':sum(r['status']!='unchanged' for r in scene_results)})
                plan['progress']={'phase':phase,'total':len(changes),
                    'scenes':scene_progress,
                    'backed_up':len(plan['rows']),
                    'completed':sum(r['status'] in ('verified','unchanged') for r in plan['results']),
                    'written_verified':sum(r['status']=='verified' for r in plan['results']),
                    'unchanged':sum(r['status']=='unchanged' for r in plan['results']),
                    'plan_id':ident}
                self._save('plan',plan,ident)
                if progress:progress(dict(plan['progress']))
                if check_cancel and cancelled and cancelled():raise OperationCancelled()
            try:
                checkpoint('backup')
                profiles={ch:self._cached_profiles(ch) for ch in {c['channel'] for c in changes}}
                for c in changes:
                    p=profiles[c['channel']].get(c['address'])
                    if not p:
                        profile=g.read_lamp_profile(c['channel'],c['address'])
                        captured=time.time()
                        self._save('profile',{'gateway':info,'captured_at':captured,'rows':[profile]})
                        p={'profile':profile,'captured_at':captured}
                        profiles[c['channel']][c['address']]=p
                    typ=p['profile']['light_type_code']
                    if typ!=1:raise ValueError('confirmed DT8 required')
                    if p and c['kelvin_raw']!=65535 and not p['profile']['min_kelvin_raw']<=c['kelvin_raw']<=p['profile']['max_kelvin_raw']:
                        raise ValueError('Kelvin outside verified fixture limits')
                    before=g.read_scene(c['channel'],c['address'],c['scene'],typ)
                    after=dict(c,light_type_code=typ)
                    plan['rows'].append({'before':before,'after':after,'changed':not self._same_scene(before,after)})
                    checkpoint('backup')
                plan['backup_seconds']=round(time.monotonic()-started,3)
                plan['state']='applying';self._save('plan',plan,ident)
                for row in plan['rows']:
                    checkpoint('write_verify')
                    target=row['after']
                    result={'target':target,'status':'outcome_unknown'}
                    plan['results'].append(result)
                    if not row['changed']:
                        result.update(status='unchanged',readback=row['before']);checkpoint('write_verify',False);continue
                    self._save('plan',plan,ident)
                    try:
                        phase_started=time.monotonic()
                        try:
                            g.write_scene(target)
                        finally:
                            result['write_receipt_seconds']=round(time.monotonic()-phase_started,6)
                            plan['timing_seconds']['write_receipts']+=result['write_receipt_seconds']
                        phase_started=time.monotonic()
                        try:
                            result['readback']=g.read_scene(target['channel'],target['address'],target['scene'],target['light_type_code'])
                        finally:
                            result['readback_seconds']=round(time.monotonic()-phase_started,6)
                            plan['timing_seconds']['readback']+=result['readback_seconds']
                        if not self._same_scene(result['readback'],target):
                            result['status']='verification_failed';raise RuntimeError('scene readback mismatch')
                        result['status']='verified';checkpoint('write_verify',False)
                    except Exception as exc:
                        result['error']=str(exc);plan['state']='stopped_partial';break
                else:plan['state']='completed'
                plan['elapsed_seconds']=round(time.monotonic()-started,3)
                checkpoint(plan['state'],False)
                return plan
            except OperationCancelled:
                plan['state']='cancelled_partial' if any(r['status']!='unchanged' for r in plan['results']) else 'cancelled_before_write'
                plan['elapsed_seconds']=round(time.monotonic()-started,3)
                checkpoint(plan['state'],False)
                return plan
            except Exception as exc:
                plan['state']='stopped_partial' if plan['results'] else 'preflight_failed'
                plan['error']=str(exc);self._save('plan',plan,ident);raise
            finally:g.close()

    def set_live_output(self, channel, changes, verify_addresses):
        """Tune first, save later: no F039 storage write or F013 scene recall."""
        if type(channel) is not int or channel not in (1,2):raise ValueError('channel 1/2 required')
        if not 1<=len(changes)<=64:raise ValueError('provide 1..64 live changes')
        changes=[LiveChange.model_validate(c).model_dump(exclude_none=True) for c in changes]
        addresses=[c['address'] for c in changes]
        if len(set(addresses))!=len(addresses):raise ValueError('duplicate address')
        if any(len(c)==1 for c in changes):raise ValueError('each address needs brightness or Kelvin')
        if not 1<=len(verify_addresses)<=8 or len(set(verify_addresses))!=len(verify_addresses):
            raise ValueError('provide 1..8 unique verification addresses')
        if any(type(a) is not int or a not in addresses for a in verify_addresses):
            raise ValueError('verification addresses must be among the changed addresses')
        with self.lock:
            started=time.monotonic()
            g,info=self._open()
            report={'gateway':info,'channel':channel,'changes':changes,'created_at':time.time(),
                    'scene_storage_modified':False,'scene_recalled':False,'commands':[],
                    'verification':[],'profiles':[],'state':'preflight'}
            ident=self._save('live-output',report)
            report['operation_id']=ident
            try:
                profiles=self._cached_profiles(channel)
                fresh=[]
                # Validate the entire batch before the first live command.
                for index,c in enumerate(changes):
                    a=c['address']
                    self._live_checkpoint('live-output',report,'profiles',index,len(changes),a,'cache' if a in profiles else 'bus')
                    if a not in profiles:
                        row=g.read_lamp_profile(channel,a);fresh.append(row)
                        profiles[a]={'profile':row,'captured_at':time.time(),'source':'fresh_bus_read'}
                    evidence=profiles[a]; p=evidence['profile']
                    if p['light_type_code']!=1:raise ValueError('realtime output currently supports confirmed DT8 only')
                    if 'kelvin_raw' in c and not p['min_kelvin_raw']<=c['kelvin_raw']<=p['max_kelvin_raw']:
                        raise ValueError(f"address {a}: Kelvin outside verified fixture limits")
                    report['profiles'].append(evidence)
                if fresh:self._save('profile',{'gateway':info,'captured_at':time.time(),'rows':fresh})
                report['preflight_seconds']=round(time.monotonic()-started,3)
                self._live_checkpoint('live-output',report,'profiles',len(changes),len(changes))
                report['state']='dispatching';self._save('live-output',report,ident)
                stopped=False
                # Start all color changes first. Before touching a lamp's brightness,
                # observe that its CT fade has completed: F02B can interrupt F02C.
                for component in ('kelvin_raw','level_raw'):
                    for c in changes:
                        if component not in c:continue
                        if component=='level_raw' and 'kelvin_raw' in c:
                            self._live_checkpoint('live-output',report,'color_settling',len(report.get('color_settling',[])),sum('level_raw' in x and 'kelvin_raw' in x for x in changes),c['address'])
                            settled=self._wait_live_target(g,channel,c['address'],{'kelvin_raw':c['kelvin_raw']})
                            report.setdefault('color_settling',[]).append(settled)
                            if not settled.get('matches_target',False):
                                report['stop_reason']='Color did not settle; brightness was not sent for this lamp'
                                stopped=True
                                break
                        item={'address':c['address'],'component':component,'value':c[component],'status':'outcome_unknown'}
                        self._live_checkpoint('live-output',report,'dispatch',len(report['commands']),sum(len(x)-1 for x in changes),c['address'])
                        report['commands'].append(item);self._save('live-output',report,ident)
                        try:
                            item['receipt']=g.set_live_component(channel,c['address'],component,c[component])
                            item['status']='reply_received'
                        except (TimeoutError,ProtocolError,OSError) as exc:
                            item['error']=str(exc);item['protocol_replies']=list(g.recent_packets)
                            stopped=True
                        self._save('live-output',report,ident)
                        if stopped:break
                    if stopped:break
                report['dispatch_seconds']=round(time.monotonic()-started,3)
                attempted={x['address'] for x in report['commands']}
                sample=list(dict.fromkeys(([report['commands'][-1]['address']] if stopped else [])+verify_addresses))
                for address in sample:
                    if address not in attempted:continue
                    self._live_checkpoint('live-output',report,'verification',len(report['verification']),len(sample),address)
                    expected=next(c for c in changes if c['address']==address)
                    row=self._wait_live_target(g,channel,address,expected)
                    report['verification'].append(row)
                report['samples_verified']=bool(report['verification']) and all(r.get('matches_target',False) for r in report['verification'])
                report['state']='stopped_partial' if stopped else 'dispatched'
                report['elapsed_seconds']=round(time.monotonic()-started,3)
                self._live_checkpoint('live-output',report,report['state'],len(report['verification']),len(sample))
                return report
            except Exception as exc:
                report.update(state='stopped_partial' if report['commands'] else 'preflight_failed',error=str(exc),elapsed_seconds=round(time.monotonic()-started,3))
                self._save('live-output',report,ident)
                raise
            finally:g.close()

    @staticmethod
    def _identification_hold(seconds):
        time.sleep(seconds)

    def get_identification(self,operation_id=None):
        """Read saved evidence without taking the physical-operation lock."""
        if operation_id is None:
            paths=sorted(self.state_dir.glob('identify-*.json'),key=lambda p:p.stat().st_mtime,reverse=True)[:10]
            return {'operations':[json.loads(p.read_text(encoding='utf-8')) for p in paths],
                    'note':'Saved phase does not prove the process is alive. No automatic resume or restore.'}
        if not isinstance(operation_id,str) or len(operation_id)!=32 or any(c not in '0123456789abcdef' for c in operation_id):
            raise ValueError('operation_id must be 32 lowercase hexadecimal characters')
        return json.loads((self.state_dir/f'identify-{operation_id}.json').read_text(encoding='utf-8'))

    def identify_lamp(self,channel,address,hold_seconds=2):
        """A bounded brightness-only identification sequence with restoration.

        Restoration is part of this explicitly requested live operation, not
        a storage rollback. No restart/resend is scheduled after process exit.
        """
        if type(channel) is not int or channel not in (1,2) or type(address) is not int or not 0<=address<64:
            raise ValueError('explicit channel 1/2 and short address 0..63 required')
        if type(hold_seconds) is not int or not 1<=hold_seconds<=5:
            raise ValueError('hold_seconds must be an integer 1..5')
        with self.lock:
            started=time.monotonic();ident=uuid.uuid4().hex
            report={'operation_id':ident,'channel':channel,'address':address,'hold_seconds':hold_seconds,
                    'state':'preflight','started_at':time.time(),'commands':[],
                    'scene_storage_modified':False,'color_command_sent':False,
                    'restoration_needed':False,'restore_verified':False}
            g=None;before=None;failure=None
            def persist(restoring=False):
                try:self._save('identify',report,ident)
                except OSError as exc:
                    if not restoring:raise
                    # The original was durably saved before identification.
                    # Losing subsequent disk access must not prevent its live
                    # restoration; return the logging failure to the client.
                    report.setdefault('journal_errors',[]).append(str(exc))
            def transition(target,phase):
                report['state']=phase
                item={'phase':phase,'level_raw':target,'receipt_status':'unknown'}
                report['commands'].append(item)
                persist(phase=='restoring')  # Durable intent before identification send.
                try:
                    item['receipt_status']='unknown'
                    item['receipt']=g.set_live_component(channel,address,'level_raw',target)
                    item['receipt_status']='received'
                except (TimeoutError,ProtocolError,OSError) as exc:item['dispatch_error']=str(exc)
                item['verification']=self._wait_live_target(g,channel,address,{'level_raw':target})
                persist(phase=='restoring')
                return item
            try:
                persist()
                g,report['gateway']=self._open()
                if g.light_type(channel,address)!=1:raise ValueError('identification currently requires confirmed DT8')
                report['fade']=g.read_fade_timing(channel,address)
                fade=report['fade']['fade_seconds_upper']
                if fade is None or fade>5:raise ValueError('unknown or long fade; no identification commands sent')
                maximum=g.read_lamp_parameter(channel,address,'max_level',1)['value']['level_raw']
                if not 1<=maximum<=254:raise ValueError('invalid configured maximum level')
                before=g.read_actual(channel,address,True)
                if not 0<=before['level_raw']<=254:raise ValueError('invalid original live level')
                if not 1000<=before['kelvin_raw']<=10000:
                    raise ValueError('original live color is unknown or a sentinel; no identification commands sent')
                self._identification_hold(1)
                stable=g.read_actual(channel,address,True)
                if any(before[k]!=stable[k] for k in ('level_raw','kelvin_raw')):
                    raise ValueError('live output is changing; wait for it to settle before identifying')
                report.update(original=before,maximum_level_raw=maximum,state='ready')
                persist()
                targets=([0] if before['level_raw'] else [])+[maximum]
                for target in targets:
                    report['restoration_needed']=True
                    item=transition(target,'identifying')
                    if item['receipt_status']!='received' or not item['verification'].get('matches_target'):
                        raise RuntimeError('Identification transition uncertain; no more identification steps')
                    self._identification_hold(hold_seconds)
                report['effect_verified']=True
            except (Exception,KeyboardInterrupt) as exc:
                failure=str(exc) or type(exc).__name__
                report['error']=failure
            finally:
                try:
                    if report['restoration_needed'] and before is not None:
                        report['state']='restoring';persist(True)
                        # A timeout invalidates the old session. Recheck identity
                        # before a distinct restoration, never repeat identification.
                        if g is None or g.route is None:
                            if g is not None:g.close()
                            g,report['restore_gateway']=self._open()
                        actual=g.read_actual(channel,address,True)
                        if actual['level_raw']!=before['level_raw']:
                            restored=transition(before['level_raw'],'restoring')
                            actual=restored['verification'].get('actual',actual)
                        report['restored_actual']=actual
                        report['brightness_restored']=actual['level_raw']==before['level_raw']
                        report['kelvin_unchanged']=actual['kelvin_raw']==before['kelvin_raw']
                        report['restore_verified']=report['brightness_restored'] and report['kelvin_unchanged']
                        report['state']=('stopped_restored' if failure else 'completed') if report['restore_verified'] else 'restore_unverified'
                    else:report['state']='preflight_failed'
                except (Exception,KeyboardInterrupt) as exc:
                    report.update(state='restore_unverified',restore_error=str(exc) or type(exc).__name__)
                finally:
                    if g is not None:g.close()
                    report['elapsed_seconds']=round(time.monotonic()-started,3)
                    report['all_receipts_received']=all(c['receipt_status']=='received' for c in report['commands'])
                    persist(True)
            return report

    @staticmethod
    def _wait_live_target(g,channel,address,expected):
        """Read through fixture fade without resending a control command."""
        row={'address':address,'expected':expected,'observations':[]}
        deadline=time.monotonic()+8
        try:
            while True:
                actual=g.read_actual(channel,address,True)
                row['actual']=actual
                row['observations'].append(actual)
                row['matches_target']=('level_raw' not in expected or actual['level_raw']==expected['level_raw']) and (
                    'kelvin_raw' not in expected or (actual['kelvin_raw']>0 and
                    1000000//actual['kelvin_raw']==1000000//expected['kelvin_raw']))
                if row['matches_target']:break
                if time.monotonic()>=deadline:
                    row['settling_timeout']=True
                    break
                time.sleep(.25)
        except (TimeoutError,ProtocolError,OSError) as exc:row['error']=str(exc)
        return row

    def read_scenes(self, channel, addresses, scenes):
        if type(channel) is not int or channel not in (1, 2):
            raise ValueError("channel must be 1 or 2")
        if not addresses or not scenes or len(addresses)*len(scenes) > 64:
            raise ValueError("request 1..64 explicit address/scene pairs")
        if any(type(x) is not int or not 0 <= x <= 63 for x in addresses):
            raise ValueError("addresses must be integers 0..63")
        if any(type(x) is not int or not 0 <= x <= 15 for x in scenes):
            raise ValueError("scenes must be integers 0..15")
        if len(set(addresses)) != len(addresses) or len(set(scenes)) != len(scenes):
            raise ValueError("duplicate addresses/scenes")
        with self.lock:
            g, info = self._open()
            rows = []
            try:
                profiles=self._cached_profiles(channel)
                for address in addresses:
                    typ = profiles[address]['profile']['light_type_code'] if address in profiles else g.light_type(channel, address)
                    for scene in scenes:
                        rows.append(g.read_scene(channel, address, scene, typ))
            except Exception as exc:
                snapshot_id = self._save("snapshot", {"gateway": info, "rows": rows,
                    "complete": False, "error": str(exc), "captured_at": time.time()})
                raise RuntimeError(f"Partial read: {len(rows)} rows saved in snapshot {snapshot_id}; {exc}") from exc
            finally:
                g.close()
            result = {"gateway": info, "captured_at": time.time(), "rows": rows}
            result["snapshot_id"] = self._save("snapshot", result)
            return result

    def _save(self, prefix, value, ident=None):
        ident = ident or uuid.uuid4().hex
        path = self.state_dir / f"{prefix}-{ident}.json"
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        # Windows readers/indexers can briefly deny an atomic replacement.
        # Retry ONLY this local publication, never the preceding device I/O.
        for attempt in range(6):
            try:
                temp.replace(path)
                break
            except PermissionError as exc:
                if getattr(exc,'winerror',None) not in (5,32,33) or attempt==5:raise
                time.sleep(.01*(2**attempt))
        publish_saved(prefix, ident, value)
        return ident

    def _load_plan(self, ident):
        if len(ident) != 32 or any(c not in "0123456789abcdef" for c in ident):
            raise ValueError("invalid plan_id")
        return json.loads((self.state_dir / f"plan-{ident}.json").read_text(encoding="utf-8"))

    def preview(self, changes, verify_limits=False):
        if not isinstance(changes,list) or not 1 <= len(changes) <= 1024:
            raise ValueError("provide 1..1024 changes")
        changes = [SceneChange.model_validate(c).model_dump() for c in changes]
        keys = [(c["channel"], c["address"], c["scene"]) for c in changes]
        if len(set(keys)) != len(keys):
            raise ValueError("duplicate lamp/scene; shared group memberships must be resolved first")
        for c in changes:
            if c["kelvin_raw"] != 65535 and not 1000 <= c["kelvin_raw"] <= 10000:
                raise ValueError("kelvin_raw must be 1000..10000 or 65535 (MASK)")
        with self.lock:
            g, info = self._open()
            rows, types, bounds = [], {}, {}
            try:
                for c in changes:
                    key = (c["channel"], c["address"])
                    if key not in types:
                        if verify_limits:
                            profile=g.read_lamp_profile(*key)
                            types[key]=profile['light_type_code']
                            if types[key]!=1:raise ValueError('restore requires confirmed DT8')
                            low=g.read_lamp_parameter(*key,'min_level',1)['value']['level_raw']
                            high=g.read_lamp_parameter(*key,'max_level',1)['value']['level_raw']
                            bounds[key]=(profile['min_kelvin_raw'],profile['max_kelvin_raw'],low,high)
                        else:
                            cached=self._cached_profiles(key[0]).get(key[1])
                            types[key] = cached['profile']['light_type_code'] if cached else g.light_type(*key)
                    if types[key] != 1:
                        raise ValueError("v0.1 scene writes support only confirmed DT8 code 1")
                    if verify_limits:
                        low_k,high_k,low,high=bounds[key]
                        if not 1000<=low_k<=high_k<=10000 or not 1<=low<=high<=254:
                            raise ValueError('unknown current fixture limits')
                        if c['kelvin_raw']!=65535 and not low_k<=c['kelvin_raw']<=high_k:
                            raise ValueError('restored Kelvin outside current fixture limits')
                        if c['level_raw'] not in (0,255) and not low<=c['level_raw']<=high:
                            raise ValueError('restored brightness outside current fixture limits')
                    before = g.read_scene(*key, c["scene"], types[key])
                    after = dict(c, light_type_code=types[key])
                    rows.append({"before": before, "after": after, "changed": not self._same_scene(before,after)})
            finally:
                g.close()
            plan = {"created_at": time.time(), "expires_at": time.time()+600,
                    "gateway": info, "rows": rows, "state": "previewed",
                    "notice": "DT8 scene writes validated on pinned firmware 1.6.9. Check fixture Kelvin limits with dali_read_lamp_profiles before writing. No scene recall is performed."}
            plan["digest"] = hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()
            ident = self._save("plan", plan)
            return dict(plan, plan_id=ident, confirmation=f"APPLY {ident} {plan['digest'][:12]}")

    def get_plan(self, ident):
        # _save publishes by atomic replace. Reading persisted progress must
        # not wait for a long-running gateway operation to release the bus.
        return self._load_plan(ident)

    def preview_scene_restore(self,plan_id):
        """Review only attempted writes from a terminal save, never resume it."""
        with self.lock:
            source=self._load_plan(plan_id)
            if source.get('state') not in ('completed','stopped_partial','cancelled_partial'):
                raise ValueError('source must be a terminal executed scene plan')
            gateway=source.get('gateway',{})
            if (gateway.get('ip')!=self.config['gateway_ip'] or
                    gateway.get('dali_mac','').lower()!=self.config['expected_dali_mac'].lower() or
                    gateway.get('dali_firmware')!=self.config['expected_firmware']):
                raise ValueError('source scene plan belongs to a different gateway')
            originals={}
            for row in source.get('rows',[]):
                after=row['after'];key=tuple(after[k] for k in ('channel','address','scene'))
                if tuple(row['before'][k] for k in ('channel','address','scene'))!=key:
                    raise ValueError('source backup target mismatch')
                if key in originals:raise ValueError('duplicate source target')
                originals[key]=row
            selected=[];seen=set()
            for result in source.get('results',[]):
                target=result['target'];key=tuple(target[k] for k in ('channel','address','scene'))
                if key in seen or key not in originals or target!=originals[key]['after']:
                    raise ValueError('execution journal does not match source backup')
                seen.add(key)
                if result['status']=='unchanged':continue
                if result['status'] not in ('verified','outcome_unknown','verification_failed'):
                    raise ValueError('unsupported source result status')
                selected.append(originals[key])
            if not selected:
                return {'state':'no_attempted_writes','source_plan_id':plan_id,'device_writes':False}
            fields=('channel','address','scene','level_raw','kelvin_raw')
            changes=[{k:r['before'][k] for k in fields} for r in selected]
            # Preview reads all current targets. It remains non-executable if
            # a current value matches neither the old nor attempted value.
            plan=self.preview(changes,verify_limits=True)
            conflicts=[]
            for row,old in zip(plan['rows'],selected):
                if not self._same_scene(row['before'],old['before']) and not self._same_scene(row['before'],old['after']):
                    conflicts.append({'current':row['before'],'original':old['before'],'attempted':old['after']})
            plan.update(source_plan_id=plan_id,restore_scope='attempted scene writes only',conflicts=conflicts,
                device_writes=False,notice='Read-only recovery review; never replay the old job. Applying needs explicit restoration authorization and current write scope.')
            if conflicts:plan['state']='restore_conflict';plan.pop('confirmation',None)
            self._save('plan',{k:v for k,v in plan.items() if k!='confirmation'},plan['plan_id'])
            return plan

    def apply(self, ident, confirmation):
        with self.lock:
            if not self.config.get("enable_experimental_writes", False):
                raise PermissionError("Device writes disabled. Enable only for an explicitly authorized physical validation/configuration task.")
            plan = self._load_plan(ident)
            if confirmation != f"APPLY {ident} {plan['digest'][:12]}":
                raise ValueError("confirmation does not match preview")
            if plan["state"] != "previewed" or time.time() > plan["expires_at"]:
                raise ValueError("plan already consumed or expired; make a new preview")
            if hashlib.sha256(json.dumps(plan["rows"], sort_keys=True).encode()).hexdigest() != plan["digest"]:
                raise ValueError("plan content changed")
            self._check_scene_scope([row['after'] for row in plan['rows']])
            if 'native_workbook_source' in plan:
                from .native_workbook_scenes import NativeWorkbookScenes
                NativeWorkbookScenes(self).validate_plan(plan)
            g, info = self._open()
            try:
                if info["dali_mac"] != plan["gateway"]["dali_mac"]:
                    raise RuntimeError("plan gateway identity changed")
                if 'native_workbook_source' in plan:
                    from .native_workbook_scenes import verify_current_limits
                    verify_current_limits(g,plan['rows'])
                # All preflight reads finish before the first write.
                for row in plan["rows"]:
                    b = row["before"]
                    cached=self._cached_profiles(b['channel']).get(b['address'])
                    typ = cached['profile']['light_type_code'] if cached else g.light_type(b["channel"], b["address"])
                    now = g.read_scene(b["channel"], b["address"], b["scene"], typ)
                    if now != b:
                        raise RuntimeError("scene changed since preview; no writes sent")
                plan["state"] = "applying"
                plan["results"] = []
                # Review metadata describes the earlier read-only step, not
                # this execution. Persist intent before dispatch; True is
                # conservative and must not be read as proof of a sent packet.
                plan["preview_metadata"] = {k:plan.pop(k) for k in ("device_writes","notice") if k in plan}
                plan.update(device_writes=False,write_intent_count=0,
                    notice="Execution record: device_writes indicates recorded write intent; a command may have been sent. It does not prove receipt or readback. Inspect per-target results. No scene recall is performed.")
                self._save("plan", plan, ident)
                for row in plan["rows"]:
                    target = row["after"]
                    if not row["changed"]:
                        plan["results"].append({"target": target, "status": "unchanged"})
                        continue
                    # Journal intent BEFORE send: crash/timeout is an uncertain
                    # outcome, never a reason to blindly resend a write.
                    result = {"target": target, "status": "outcome_unknown"}
                    plan["results"].append(result)
                    plan["device_writes"] = True
                    plan["write_intent_count"] += 1
                    self._save("plan", plan, ident)
                    try:
                        g.write_scene(target)
                        actual = g.read_scene(target["channel"], target["address"], target["scene"], target["light_type_code"])
                        result["readback"] = actual
                        # DT8 Kelvin is quantized through integral reciprocal
                        # megakelvin (mired): 3000K can read back as 3003K.
                        expected = dict(target)
                        if (0 < target["kelvin_raw"] < 65535 and 0 < actual["kelvin_raw"] < 65535
                                and 1000000 // target["kelvin_raw"] == 1000000 // actual["kelvin_raw"]):
                            expected["kelvin_raw"] = actual["kelvin_raw"]
                        if actual != expected:
                            result["status"] = "verification_failed"
                            raise RuntimeError("scene readback mismatch")
                        result["status"] = "verified"
                        self._save("plan", plan, ident)
                    except Exception as exc:
                        result["error"] = str(exc)
                        result["protocol_replies"] = g.recent_packets
                        plan["state"] = "stopped_partial"
                        self._save("plan", plan, ident)
                        return plan
                plan["state"] = "completed"
                self._save("plan", plan, ident)
                return plan
            finally:
                g.close()
