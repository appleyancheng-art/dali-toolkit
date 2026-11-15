"""Single DT8 short-address move to a vacant address, with conditional undo.

No delete, allocation, swap or broadcast MCP operation. Host labels/layouts are
not changed. OS lock covers this controller/state directory only; other hosts
are not participants. A short address and matching configuration are not a
hardware identity. Readback establishes observed values, not an atomic bus.
"""
from contextlib import contextmanager
import copy
import hashlib
import json
import math
import os
import re
import time
import uuid
from .native_file import require_configmaster_closed
from .protocol import Gateway
from .projects import identity


def scope(channel,source_address,destination_address):
    if type(channel) is not int or channel not in (1,2):raise ValueError('channel1/2 required')
    for value in (source_address,destination_address):
        if type(value) is not int or not 0<=value<=63:raise ValueError('short addresses0..63 required; no MASK or broadcast')
    if source_address==destination_address:raise ValueError('source and destination must differ')
    return dict(channel=channel,source_address=source_address,destination_address=destination_address)


def address_set(values):
    if (not isinstance(values,list) or any(type(a) is not int or not 0<=a<=63 for a in values)
            or values!=sorted(set(values))):raise ValueError('invalid scanned address set')
    return values


def signature(lamp):
    """Address-neutral values; retain all raw replies separately in the record."""
    if set(lamp['parameters'])!=set(Gateway.PARAMETER_FIELDS):raise ValueError('incomplete supported parameter backup')
    params={k:v['value'] for k,v in lamp['parameters'].items()}
    if params['light_type']['code']!=1:raise ValueError('address moves currently require DT8')
    scenes=lamp['scenes']
    if [r['scene'] for r in scenes]!=list(range(16)):raise ValueError('complete16-scene backup required')
    if any(r['channel']!=lamp['channel'] or r['address']!=lamp['address'] or r['light_type_code']!=1 for r in scenes):
        raise ValueError('scene backup target mismatch')
    actual=lamp['actual']
    if actual['channel']!=lamp['channel'] or actual['address']!=lamp['address'] or actual['light_type_code']!=1:
        raise ValueError('actual backup target mismatch')
    keys=('light_type_code','level_raw','kelvin_raw','second_level_raw','second_kelvin_raw')
    return dict(parameters=params,scenes=[{k:r[k] for k in ('scene','light_type_code','level_raw','kelvin_raw')} for r in scenes],
                actual={k:actual[k] for k in keys})


class AddressMoveController:
    def __init__(self,service,process_guard=None):
        self.service=service
        self.process_guard=process_guard or require_configmaster_closed

    @contextmanager
    def _locked(self):
        with self.service.lock:
            with (self.service.state_dir/'address-moves.lock').open('a+b') as handle:
                if handle.seek(0,2)==0:handle.write(b'\0');handle.flush()
                handle.seek(0)
                try:
                    if os.name=='nt':
                        import msvcrt
                        msvcrt.locking(handle.fileno(),msvcrt.LK_NBLCK,1)
                    else:
                        import fcntl
                        fcntl.flock(handle.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
                except OSError as exc:raise RuntimeError('another address move holds this state directory') from exc
                try:yield
                finally:
                    handle.seek(0)
                    if os.name=='nt':msvcrt.locking(handle.fileno(),msvcrt.LK_UNLCK,1)
                    else:fcntl.flock(handle.fileno(),fcntl.LOCK_UN)

    @staticmethod
    def _digest(plan):
        return hashlib.sha256(json.dumps({k:v for k,v in plan.items() if k not in ('digest','state','execution_id','confirmation')},
            sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()

    @staticmethod
    def _confirmation(plan):return f"MOVE ADDRESS {plan['plan_id']} {plan['digest'][:12]}"

    @staticmethod
    def _id(value):
        if type(value) is not str or not re.fullmatch('[0-9a-f]{32}',value):raise ValueError('invalid address plan ID')
        return value

    def _load(self,ident,seen=None):
        self._id(ident)
        seen=set() if seen is None else set(seen)
        if ident in seen or len(seen)>=16:raise ValueError('invalid restoration ancestry')
        seen.add(ident)
        plan=json.loads((self.service.state_dir/f'address-plan-{ident}.json').read_text(encoding='utf-8'))
        if plan.get('plan_id')!=ident or plan.get('digest')!=self._digest(plan):raise ValueError('address plan checksum mismatch')
        scope(plan['channel'],plan['source_address'],plan['destination_address'])
        addresses=address_set(plan['before_addresses'])
        src,dst=plan['source_address'],plan['destination_address']
        if src not in addresses or dst in addresses:raise ValueError('invalid original address occupancy')
        if plan['after_addresses']!=sorted((set(addresses)-{src})|{dst}):raise ValueError('invalid address mapping')
        if plan['original']['channel']!=plan['channel'] or plan['original']['address']!=src:raise ValueError('original lamp scope mismatch')
        signature(plan['original'])
        for key in ('created_at','expires_at'):
            if type(plan[key]) not in (int,float) or not math.isfinite(plan[key]):raise ValueError('invalid plan time')
        if plan['expires_at']!=plan['created_at']+600:raise ValueError('invalid plan lifetime')
        if plan.get('restore_of') is not None:
            parent=self._load(plan['restore_of'],seen)
            if (identity(plan['gateway'])!=identity(parent['gateway']) or plan['channel']!=parent['channel']
                    or src!=parent['destination_address'] or dst!=parent['source_address']
                    or plan['before_addresses']!=parent['after_addresses']
                    or plan['after_addresses']!=parent['before_addresses']
                    or signature(plan['original'])!=signature(parent['original'])):
                raise ValueError('restoration differs from original move')
            evidence=self._execution(parent)
            run=evidence.get('execution')
            if (not run or run.get('write_intent') is not True or
                    run.get('attempt')!=scope(parent['channel'],parent['source_address'],parent['destination_address'])):
                raise ValueError('restoration source lacks matching durable intent')
        return plan

    def _open(self,expected=None):
        g,info=self.service._open()
        if expected is not None and identity(info)!=identity(expected):
            g.close();raise ValueError('address plan gateway changed')
        return g,info

    def _scan(self,channel,expected=None):
        g,info=self._open(expected)
        try:result=g.scan_existing(channel)
        finally:g.close()
        if result['channel']!=channel:raise ValueError('scan channel mismatch')
        address_set(result['addresses'])
        return info,result

    def _duplicates(self,channel,expected):
        g,_=self._open(expected)
        try:result=g.read_duplicate_addresses(channel)
        finally:g.close()
        if result['channel']!=channel or result['duplicate_addresses']!=[]:
            raise ValueError('gateway reports duplicate addresses; move refused')
        return result

    def _lamp(self,channel,address,expected):
        g,_=self._open(expected)
        row=dict(channel=channel,address=address,parameters={},scenes=[],captured_at=time.time())
        try:
            typ=None
            for field in Gateway.PARAMETER_FIELDS:
                row['parameters'][field]=g.read_lamp_parameter(channel,address,field,typ)
                if field=='light_type':
                    typ=row['parameters'][field]['value']['code']
                    if typ!=1:raise ValueError('address moves currently require DT8')
            for scene in range(16):row['scenes'].append(g.read_scene(channel,address,scene,typ))
            row['actual']=g.read_actual(channel,address,True)
            signature(row)
            return row
        finally:g.close()

    def _prepare(self,channel,src,dst,info,scan,duplicates,original,restore_of=None):
        now=time.time()
        plan=dict(plan_id=uuid.uuid4().hex,**scope(channel,src,dst),gateway=info,
            before_addresses=scan['addresses'],after_addresses=sorted((set(scan['addresses'])-{src})|{dst}),
            original=original,scan_evidence=scan,duplicate_evidence=duplicates,
            created_at=now,expires_at=now+600,state='prepared',restore_of=restore_of,
            physical_identity_verified=False,host_mappings_migrated=False,
            notice='Moves one DT8 short address only. Names/layouts/archived projects still refer to old addresses; reconcile them separately. No collision-free physical identity guarantee.')
        plan['digest']=self._digest(plan);self.service._save('address-plan',plan,plan['plan_id'])
        return dict(plan,confirmation=self._confirmation(plan))

    def prepare(self,channel,source_address,destination_address):
        scope(channel,source_address,destination_address)
        with self._locked():
            self.process_guard()
            info,scan=self._scan(channel)
            if source_address not in scan['addresses']:raise ValueError('source address absent')
            if destination_address in scan['addresses']:raise ValueError('destination address occupied; no implicit deletion or swap')
            duplicates=self._duplicates(channel,info)
            original=self._lamp(channel,source_address,info)
            return self._prepare(channel,source_address,destination_address,info,scan,duplicates,original)

    def _gate(self,plan):
        cfg=self.service.config
        if cfg.get('enable_experimental_address_writes') is not True:raise PermissionError('separate address write gate required')
        allowed=cfg.get('allowed_address_moves')
        if not isinstance(allowed,list) or not allowed:raise PermissionError('exact allowed_address_moves required')
        for item in allowed:
            try:
                if not isinstance(item,dict) or set(item)!={'channel','source_address','destination_address'}:raise ValueError()
                scope(**item)
            except (ValueError,TypeError) as exc:raise PermissionError('invalid address write scope') from exc
        if scope(plan['channel'],plan['source_address'],plan['destination_address']) not in allowed:
            raise PermissionError('move outside exact allowed scope')

    def execute(self,ident,confirmation):
        with self._locked():
            plan=self._load(ident);self._gate(plan)
            if confirmation!=self._confirmation(plan):raise ValueError('confirmation mismatch')
            if plan['state']!='prepared' or time.time()>plan['expires_at']:raise ValueError('plan consumed or expired')
            self.process_guard()
            run_id=uuid.uuid4().hex
            with (self.service.state_dir/f'address-claim-{ident}.json').open('x',encoding='utf-8') as f:
                json.dump(dict(plan_id=ident,execution_id=run_id,claimed_at=time.time()),f);f.flush();os.fsync(f.fileno())
            run=dict(plan_id=ident,execution_id=run_id,state='preflight',started_at=time.time(),write_intent=False,automatic_rollback=False)
            def save():self.service._save('address-run',run,run_id)
            g=None
            try:
                save();plan.update(state='consumed',execution_id=run_id);self.service._save('address-plan',plan,ident)
                ch,src,dst=plan['channel'],plan['source_address'],plan['destination_address']
                _,run['before_scan']=self._scan(ch,plan['gateway'])
                if run['before_scan']['addresses']!=plan['before_addresses']:raise ValueError('address occupancy changed since preparation')
                run['duplicates']=self._duplicates(ch,plan['gateway'])
                run['before_lamp']=self._lamp(ch,src,plan['gateway'])
                if signature(run['before_lamp'])!=signature(plan['original']):raise ValueError('source parameters/scenes/live changed')
                # The lamp backup takes seconds; check vacancy again at the end.
                _,run['dispatch_scan']=self._scan(ch,plan['gateway'])
                if run['dispatch_scan']['addresses']!=plan['before_addresses']:raise ValueError('address occupancy changed during preflight')
                if time.time()>plan['expires_at']:raise ValueError('plan expired during preflight')
                self._gate(plan);self.process_guard()
                run['capability_invalidation_id']=self.service.invalidate_capabilities(ch,[src,dst],f'address move {run_id}')
                g,_=self._open(plan['gateway'])
                run.update(state='writing',write_intent=True,attempt=scope(ch,src,dst));save()
                try:run['receipt']=g.change_short_address(ch,src,dst)
                except (Exception,KeyboardInterrupt) as exc:run['dispatch_error']=str(exc) or type(exc).__name__
                finally:
                    run['protocol_replies']=list(g.recent_packets);g.close();g=None
                run['state']='reading_after';save()
                _,run['after_scan']=self._scan(ch,plan['gateway'])
                if run['after_scan']['addresses']!=plan['after_addresses']:
                    run['state']='stopped_address_mismatch'
                else:
                    run['after_duplicates']=self._duplicates(ch,plan['gateway'])
                    run['after_lamp']=self._lamp(ch,dst,plan['gateway'])
                    if signature(run['after_lamp'])!=signature(plan['original']):run['state']='stopped_lamp_mismatch'
                    else:run['state']='verified_receipt_unknown' if 'dispatch_error' in run else 'completed_readback'
            except (Exception,KeyboardInterrupt) as exc:
                run.update(state='stopped_unknown' if run['write_intent'] else 'preflight_failed',error=str(exc) or type(exc).__name__)
            finally:
                if g is not None:g.close()
                run['finished_at']=time.time()
                try:save()
                except Exception as exc:run['log_error']=str(exc)
            return run

    def _execution(self,plan):
        plan=copy.deepcopy(plan);ident=plan['plan_id']
        cp=self.service.state_dir/f'address-claim-{ident}.json'
        if cp.exists():
            claim=json.loads(cp.read_text(encoding='utf-8'));run_id=self._id(claim.get('execution_id'))
            if claim.get('plan_id')!=ident or plan.get('execution_id',run_id)!=run_id:raise ValueError('address claim mismatch')
            plan['claimed']=True
            rp=self.service.state_dir/f'address-run-{run_id}.json'
            if rp.exists():
                run=json.loads(rp.read_text(encoding='utf-8'))
                if run.get('plan_id')!=ident or run.get('execution_id')!=run_id:raise ValueError('address execution mismatch')
                plan['execution']=run
            else:plan['execution_record_missing']=True
        return plan

    def get(self,ident):
        plan=self._execution(self._load(ident))
        return dict(plan,confirmation=self._confirmation(plan),saved_phase_does_not_prove_process_running=True)

    def prepare_restore(self,ident):
        with self._locked():
            self.process_guard();plan=self.get(ident)
            run=plan.get('execution')
            if not run or run.get('write_intent') is not True or run.get('attempt')!=scope(plan['channel'],plan['source_address'],plan['destination_address']):
                raise ValueError('matching durable write intent required for restoration')
            ch,src,dst=plan['channel'],plan['source_address'],plan['destination_address']
            info,scan=self._scan(ch,plan['gateway']);duplicates=self._duplicates(ch,info)
            if scan['addresses']==plan['before_addresses']:
                current=self._lamp(ch,src,info)
                if signature(current)!=signature(plan['original']):raise ValueError('original-address configuration changed')
                return dict(state='already_original',source_plan_id=ident,current=current,scan=scan,device_writes=False)
            if scan['addresses']!=plan['after_addresses']:raise ValueError('current occupancy is neither original nor moved mapping')
            current=self._lamp(ch,dst,info)
            if signature(current)!=signature(plan['original']):raise ValueError('moved lamp configuration changed; conditional restore refused')
            return self._prepare(ch,dst,src,info,scan,duplicates,current,restore_of=ident)
