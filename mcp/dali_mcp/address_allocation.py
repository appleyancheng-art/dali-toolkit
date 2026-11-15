"""Background review and one channel-wide incremental allocation (F001 mode2).

Expected count is a verification assertion, NOT a limit enforced by the gear.
No reset, replay, physical identity claim, or automatic deletion/undo.
"""
import json
import math
import os
import threading
import time
import uuid
from .address_moves import AddressMoveController, address_set
from .protocol import Gateway
from .projects import digest


def scope(channel, expected_new_count):
    if type(channel) is not int or channel not in (1, 2):
        raise ValueError('explicit channel1/2 required')
    if type(expected_new_count) is not int or not 1 <= expected_new_count <= 64:
        raise ValueError('explicit expected_new_count1..64 required; not a device-side limit')
    return dict(channel=channel, expected_new_count=expected_new_count,
                target='all_unaddressed_gear_on_channel')


def fingerprint(row):
    """Supported values only, not a hardware fingerprint or atomic snapshot."""
    if set(row['parameters']) != set(Gateway.PARAMETER_FIELDS):
        raise ValueError('incomplete supported parameter snapshot')
    typ = row['parameters']['light_type']['value']['code']
    if type(typ) is not int or typ not in (0, 1):
        raise ValueError('only DT6/DT8 snapshots supported')
    params = {}
    for field, record in row['parameters'].items():
        if typ == 0 and field.startswith('configured_'):
            if record.get('state') != 'not_applicable' or 'value' in record:
                raise ValueError('DT6 color-temperature fields must be not_applicable')
            params[field] = {'state': 'not_applicable'}
        else:
            params[field] = record['value']
    if [s['scene'] for s in row['scenes']] != list(range(16)):
        raise ValueError('all16 scenes required')
    for item in [*row['scenes'], row['actual']]:
        if item['channel'] != row['channel'] or item['address'] != row['address'] or item['light_type_code'] != typ:
            raise ValueError('snapshot target or type mismatch')
    return dict(parameters=params,
                scenes=[{k:s[k] for k in ('scene','light_type_code','level_raw','kelvin_raw')} for s in row['scenes']],
                actual={k:row['actual'][k] for k in ('light_type_code','level_raw','kelvin_raw','second_level_raw','second_kelvin_raw')})


class CancelledBeforeWrite(Exception):
    pass


class AddressAllocationController:
    def __init__(self, service, process_guard=None):
        self.service = service
        self.base = AddressMoveController(service, process_guard)
        self.runtime_lock = threading.RLock()
        self.active = None
        self.event = None
        self.worker = None
        self.errors = {}

    def _path(self, kind, ident):
        self.base._id(ident)
        return self.service.state_dir / f'address-allocation-{kind}-{ident}.json'

    def _save(self, kind, ident, data, exclusive=False):
        path = self._path(kind, ident)
        target = path.with_suffix(f'.{uuid.uuid4().hex}.tmp' if exclusive else '.tmp')
        try:
            with target.open('x' if exclusive else 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2, allow_nan=False)
                f.flush()
                os.fsync(f.fileno())
            if exclusive:
                # Publish a complete, flushed file without replacing another claim.
                os.link(target, path)
            else:
                for attempt in range(6):
                    try:
                        target.replace(path)
                        break
                    except PermissionError as exc:
                        if getattr(exc,'winerror',None) not in (5,32,33) or attempt == 5:
                            raise
                        time.sleep(.01 * (2**attempt))
        finally:
            target.unlink(missing_ok=True)

    def _record(self, kind, ident, run, tolerate_failure=False):
        try:
            self._save(kind,ident,run)
        except Exception as exc:
            if not tolerate_failure:raise
            run['log_error'] = str(exc) or type(exc).__name__

    def _review_complete(self, ident, plan):
        record = self._read('review',ident)
        if (record.get('operation_id') != ident or record.get('state') != 'reviewed'
                or record.get('plan_digest') != plan['digest']
                or self._path('worker-error',ident).exists()):
            raise ValueError('complete successful review required')

    def _read(self, kind, ident):
        return json.loads(self._path(kind, ident).read_text(encoding='utf-8'))

    def _request(self, ident):
        q = self._read('request', ident)
        if q.get('operation_id') != ident or q.get('digest') != digest(q):
            raise ValueError('allocation request checksum mismatch')
        if q['scope'] != scope(q['scope']['channel'], q['scope']['expected_new_count']):
            raise ValueError('invalid allocation scope')
        return q

    def _plan(self, ident):
        q = self._request(ident)
        p = self._read('plan', ident)
        if p.get('operation_id') != ident or p.get('digest') != digest(p):
            raise ValueError('allocation plan checksum mismatch')
        if p['request_digest'] != q['digest'] or p['scope'] != q['scope']:
            raise ValueError('allocation request/plan binding changed')
        addresses = address_set(p['before_addresses'])
        if p['scope']['expected_new_count'] > 64 - len(addresses):
            raise ValueError('expected count exceeds available short addresses')
        if [r['address'] for r in p['originals']] != addresses:
            raise ValueError('original snapshot coverage mismatch')
        for row in p['originals']:
            if row['channel'] != p['scope']['channel']:
                raise ValueError('original snapshot channel mismatch')
            fingerprint(row)
        for key in ('created_at', 'expires_at'):
            if type(p[key]) not in (int, float) or not math.isfinite(p[key]):
                raise ValueError('invalid plan time')
        if p['expires_at'] != p['created_at'] + 1800:
            raise ValueError('invalid plan lifetime')
        for key in ('physical_identity_verified','atomic_snapshot','automatic_undo','device_side_count_limit'):
            if p.get(key) is not False:
                raise ValueError('invalid allocation guarantees')
        return p

    @staticmethod
    def _confirmation(p):
        return f"ALLOCATE ALL UNADDRESSED CHANNEL {p['scope']['channel']} {p['operation_id']} {p['digest']}"

    def _lamp(self, channel, address, info):
        g, _ = self.base._open(info)
        row = dict(channel=channel,address=address,parameters={},scenes=[],captured_at=time.time())
        try:
            typ = None
            for field in Gateway.PARAMETER_FIELDS:
                row['parameters'][field] = g.read_lamp_parameter(channel,address,field,typ)
                if field == 'light_type':
                    typ = row['parameters'][field]['value']['code']
                    if type(typ) is not int or typ not in (0,1):
                        raise ValueError('unsupported lamp type')
            for scene in range(16):
                row['scenes'].append(g.read_scene(channel,address,scene,typ))
            row['actual'] = g.read_actual(channel,address,typ == 1)
            fingerprint(row)
            return row
        finally:
            g.close()

    def _rows(self, ident, kind, run, field, channel, addresses, info, cancelled=None, tolerate_log_failure=False):
        run[field] = []
        run['progress'] = dict(completed=0,total=len(addresses))
        for address in addresses:
            if cancelled and cancelled():
                raise CancelledBeforeWrite()
            run['pending'] = dict(phase=field,address=address)
            self._record(kind,ident,run,tolerate_log_failure)
            run[field].append(self._lamp(channel,address,info))
            run['progress']['completed'] += 1
            run.pop('pending',None)
            self._record(kind,ident,run,tolerate_log_failure)
        return run[field]

    def review(self, ident, cancelled=lambda:False):
        """Worker implementation: reads only, never automatically proceeds."""
        q = self._request(ident)
        run = dict(operation_id=ident,state='reviewing',started_at=time.time(),device_writes=False)
        with self.base._locked():
            try:
                self._save('review',ident,run)
                if cancelled():raise CancelledBeforeWrite()
                self.base.process_guard()
                ch = q['scope']['channel']
                info, scan = self.base._scan(ch)
                run['scan'] = scan
                if q['scope']['expected_new_count'] > 64 - len(scan['addresses']):
                    raise ValueError('channel full or expected count exceeds available addresses')
                run['duplicates'] = self.base._duplicates(ch,info)
                rows = self._rows(ident,'review',run,'originals',ch,scan['addresses'],info,cancelled)
                _, run['final_scan'] = self.base._scan(ch,info)
                if run['final_scan']['addresses'] != scan['addresses']:
                    raise ValueError('address occupancy changed during review')
                if cancelled():raise CancelledBeforeWrite()
                self.base.process_guard()
                now = time.time()
                p = dict(operation_id=ident,request_digest=q['digest'],scope=q['scope'],gateway=info,
                         before_addresses=scan['addresses'],originals=rows,created_at=now,expires_at=now+1800,
                         physical_identity_verified=False,atomic_snapshot=False,automatic_undo=False,
                         device_side_count_limit=False)
                p['digest'] = digest(p)
                self._save('plan',ident,p,exclusive=True)
                run.update(state='reviewed',plan_digest=p['digest'])
            except CancelledBeforeWrite:
                run['state'] = 'cancelled_before_write'
            except (Exception,KeyboardInterrupt) as exc:
                run.update(state='review_failed',error=str(exc) or type(exc).__name__)
            finally:
                run['finished_at'] = time.time()
                self._save('review',ident,run)
        return run

    def _gate(self, p):
        cfg = self.service.config
        if cfg.get('enable_experimental_address_allocation') is not True:
            raise PermissionError('separate incremental allocation gate required')
        if cfg.get('allowed_address_allocation_digest') != p['digest']:
            raise PermissionError('exact allocation plan digest required')
        channels = cfg.get('allowed_address_allocation_channels')
        if (not isinstance(channels,list) or not channels or
                any(type(c) is not int or c not in (1,2) for c in channels) or
                p['scope']['channel'] not in channels):
            raise PermissionError('explicit channel-wide allocation scope required')

    def execute(self, ident, confirmation, cancelled=lambda:False):
        with self.base._locked():
            p = self._plan(ident)
            self._review_complete(ident,p)
            self._gate(p)
            if confirmation != self._confirmation(p) or time.time() > p['expires_at']:
                raise ValueError('invalid confirmation or expired allocation plan')
            self.base.process_guard()
            self._save('claim',ident,dict(operation_id=ident,plan_digest=p['digest'],claimed_at=time.time()),exclusive=True)
            run = dict(operation_id=ident,plan_digest=p['digest'],state='preflight',started_at=time.time(),
                       write_intent=False,automatic_undo=False,physical_identity_verified=False)
            g = None
            try:
                self._save('execution',ident,run)
                if cancelled():raise CancelledBeforeWrite()
                ch = p['scope']['channel']
                _, run['before_scan'] = self.base._scan(ch,p['gateway'])
                if run['before_scan']['addresses'] != p['before_addresses']:
                    raise ValueError('address occupancy changed since review')
                run['duplicates'] = self.base._duplicates(ch,p['gateway'])
                originals = {r['address']:fingerprint(r) for r in p['originals']}
                rows = self._rows(ident,'execution',run,'preflight_rows',ch,p['before_addresses'],p['gateway'],cancelled)
                if {r['address']:fingerprint(r) for r in rows} != originals:
                    raise ValueError('existing lamp parameters/scenes/live changed since review')
                _, run['dispatch_scan'] = self.base._scan(ch,p['gateway'])
                if run['dispatch_scan']['addresses'] != p['before_addresses']:
                    raise ValueError('address occupancy changed during preflight')
                if time.time() > p['expires_at']:
                    raise ValueError('allocation plan expired during preflight')
                self._gate(p)
                self.base.process_guard()
                if cancelled():raise CancelledBeforeWrite()
                # Clear even stale cache entries for vacant addresses now being used.
                run['capability_invalidation_id'] = self.service.invalidate_capabilities(ch,list(range(64)),f'incremental allocation {ident}')
                g, _ = self.base._open(p['gateway'])
                if cancelled():raise CancelledBeforeWrite()
                run.update(state='allocating',write_intent=True,attempt=dict(channel=ch,mode=2,target='all_unaddressed_gear_on_channel'))
                self._save('execution',ident,run)
                if cancelled():raise CancelledBeforeWrite()
                try:
                    run['receipt'] = g.allocate_missing_addresses(ch)
                except (Exception,KeyboardInterrupt) as exc:
                    run['dispatch_error'] = str(exc) or type(exc).__name__
                finally:
                    run['protocol_replies'] = list(g.recent_packets)
                    g.close()
                    g = None
                # The native allocation cannot be undone/cancelled in flight.
                # Always finish necessary readback, including after cancellation.
                run['state'] = 'reading_after'
                self._record('execution',ident,run,True)
                _, run['after_scan'] = self.base._scan(ch,p['gateway'])
                after = run['after_scan']['addresses']
                run['new_addresses'] = sorted(set(after)-set(p['before_addresses']))
                run['missing_original_addresses'] = sorted(set(p['before_addresses'])-set(after))
                run['after_duplicates'] = self.base._duplicates(ch,p['gateway'])
                rows = self._rows(ident,'execution',run,'after_rows',ch,after,p['gateway'],tolerate_log_failure=True)
                run['changed_original_addresses'] = [r['address'] for r in rows if r['address'] in originals and fingerprint(r) != originals[r['address']]]
                _, run['final_scan'] = self.base._scan(ch,p['gateway'])
                if run['missing_original_addresses'] or run['final_scan']['addresses'] != after:
                    run['state'] = 'stopped_address_mismatch'
                elif run['changed_original_addresses']:
                    run['state'] = 'stopped_lamp_mismatch'
                elif len(run['new_addresses']) != p['scope']['expected_new_count']:
                    run['state'] = 'stopped_count_mismatch'
                elif 'dispatch_error' in run:
                    run['state'] = 'verified_receipt_unknown'
                else:
                    run['state'] = 'completed_readback'
                run['cancel_requested'] = bool(cancelled())
                if run['cancel_requested']:
                    run['cancel_effect'] = 'command_already_dispatched_readback_completed_no_rollback'
            except CancelledBeforeWrite:
                run['state'] = 'cancelled_before_write'
            except (Exception,KeyboardInterrupt) as exc:
                run.update(state='stopped_unknown' if run['write_intent'] else 'preflight_failed',
                           error=str(exc) or type(exc).__name__)
            finally:
                if g is not None:g.close()
                run['finished_at'] = time.time()
                self._record('execution',ident,run,True)
        return run

    def _launch(self, ident, phase, function):
        with self.runtime_lock:
            if self.active is not None:raise RuntimeError('an allocation task is already active')
            if not self.service.lock.acquire(blocking=False):raise RuntimeError('gateway busy')
            self.service.lock.release()
            self.active = ident
            self.event = event = threading.Event()
            def work():
                try:
                    outcome = function(event.is_set)
                    if outcome.get('log_error'):
                        error=dict(operation_id=ident,phase=phase,error=outcome['log_error'],last_worker_result=outcome)
                        with self.runtime_lock:self.errors[ident]=error
                        try:self._save('worker-error',ident,error)
                        except Exception:pass
                except BaseException as exc:
                    error=dict(operation_id=ident,phase=phase,error=str(exc) or type(exc).__name__)
                    with self.runtime_lock:self.errors[ident]=error
                    try:self._save('worker-error',ident,error)
                    except Exception:pass
                finally:
                    with self.runtime_lock:self.active=None;self.event=None
            self.worker = threading.Thread(target=work,daemon=True)
            self.worker.start()
        return self.get(ident)

    def start_review(self, channel, expected_new_count):
        target = scope(channel,expected_new_count)
        with self.runtime_lock:
            if self.active is not None:raise RuntimeError('an allocation task is already active')
            ident = uuid.uuid4().hex
            q = dict(operation_id=ident,scope=target,created_at=time.time())
            q['digest'] = digest(q)
            self._save('request',ident,q,exclusive=True)
            return self._launch(ident,'review',lambda cancel:self.review(ident,cancel))

    def start(self, ident, confirmation):
        with self.runtime_lock:
            if self.active is not None:raise RuntimeError('an allocation task is already active')
            p = self._plan(ident)
            self._review_complete(ident,p)
            self._gate(p)
            if confirmation != self._confirmation(p) or time.time() > p['expires_at']:
                raise ValueError('invalid confirmation or expired allocation plan')
            if self._path('claim',ident).exists():raise ValueError('allocation already claimed; never replay')
            return self._launch(ident,'execute',lambda cancel:self.execute(ident,confirmation,cancel))

    def get(self, ident, include_details=False):
        if type(include_details) is not bool:raise ValueError('include_details must be boolean')
        q = self._request(ident)
        with self.runtime_lock:
            active = self.active == ident
            error = self.errors.get(ident)
        result = dict(operation_id=ident,scope=q['scope'],active_in_this_process=active,
                      automatic_resume=False,state='review_pending')
        for kind in ('review','execution','worker-error','cancel'):
            if self._path(kind,ident).exists():
                record = self._read(kind,ident)
                if record.get('operation_id') != ident:raise ValueError('allocation record target mismatch')
                result[kind] = record
                if kind in ('review','execution'):result['state'] = record['state']
        result['claimed'] = self._path('claim',ident).exists()
        if self._path('plan',ident).exists():
            p = self._plan(ident)
            result['plan'] = p
            if not result['claimed']:
                if result.get('review',{}).get('state') == 'reviewed' and 'worker-error' not in result and not error:
                    result['confirmation'] = self._confirmation(p)
            else:
                claim = self._read('claim',ident)
                if claim.get('operation_id') != ident or claim.get('plan_digest') != p['digest']:
                    raise ValueError('allocation claim binding mismatch')
                if 'execution' not in result:
                    result['execution_record_missing'] = True
                    result['state'] = 'execution_unavailable'
                elif result['execution'].get('plan_digest') != p['digest']:
                    raise ValueError('allocation execution binding mismatch')
        elif result['claimed']:
            raise ValueError('claimed allocation has no complete plan')
        if error:result['worker-error'] = error
        if 'worker-error' in result:result['state'] = 'worker_failed'
        result['details_included'] = include_details
        if not include_details:
            def compact(record):
                value = dict(record)
                for key in ('originals','preflight_rows','after_rows'):
                    if key in value:value[key+'_count']=len(value.pop(key))
                return value
            for key in ('review','plan','execution'):
                if key in result:result[key]=compact(result[key])
            if 'worker-error' in result and 'last_worker_result' in result['worker-error']:
                result['worker-error']={**result['worker-error'],'last_worker_result':compact(result['worker-error']['last_worker_result'])}
        return result

    def cancel(self, ident):
        self.base._id(ident)
        with self.runtime_lock:
            if self.active != ident:raise ValueError('allocation task is not active in this process')
            self._save('cancel',ident,dict(operation_id=ident,requested_at=time.time()))
            self.event.set()
        return self.get(ident)
