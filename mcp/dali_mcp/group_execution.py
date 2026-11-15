"""Scoped group transactions with single-attempt journals and explicit recovery.

Physical add/remove validated for one DT8 address/group on pinned firmware1.6.9.
Requires a separate exact-operation gate; other scopes need their own evidence.
One claim per plan prevents replay, and an OS file lock serializes experimental
group controllers sharing a state directory. Other tool families, other state
directories, panels and ConfigMaster do not participate in this lock.
"""
import copy
from contextlib import contextmanager
import hashlib
import json
import math
import os
import re
import time
import uuid
from .group_planning import plan_group_candidates, plan_group_restore_candidates, encode_group_candidate, _snapshots
from .projects import identity, validate_document


def send_group_once(g, channel, address, group, member):
    candidate = encode_group_candidate(channel, address, group, member)
    if g.route is None:
        raise RuntimeError('connected verified gateway required')
    route = g.route
    payload = bytes.fromhex(candidate['payload_hex'])
    alias = route[3:] + bytes([route[0], int(g.sock.getsockname()[0].split('.')[-1]), route[2]])
    reply = g.exchange(0xf015, payload, route, lambda p:
        p.command == 0xf095 and p.sequence == g.seq and p.transport == 1
        and p.route in (route[3:] + route[:3], alias)
        and len(p.payload) == 8 and p.payload[:3] == payload[:3])
    return dict(command='F095', sequence=reply.sequence, route=reply.route.hex(),
                payload=reply.payload.hex(), status_interpreted=False)


def _members(baseline):
    return [copy.deepcopy(r['membership']) for r in baseline]


def _state(channel, baseline):
    members = _snapshots(channel, _members(baseline))
    result = {}
    for row in baseline:
        address = row['membership']['address']
        actual = row['actual']
        typ = actual['light_type_code']
        if type(typ) is not int or typ not in (0, 1):
            raise ValueError('supported live type required')
        level = actual['level_raw']
        if type(level) is not int or not 0 <= level <= 254:
            raise ValueError('valid live brightness required')
        live = (typ, level)
        if typ == 1:
            ct = actual['kelvin_raw']
            if type(ct) is not int or not 1000 <= ct <= 10000:
                raise ValueError('known live CT required for DT8')
            live += (ct,)
        result[address] = (members[address]['group_mask'], live)
    return result


class ExperimentalGroupController:
    def __init__(self, service):
        self.service = service

    @contextmanager
    def _locked(self):
        # Lifetime lock, not a stale file-existence flag: the OS releases it on
        # process exit. Recovery cannot race a still-running group executor.
        with self.service.lock:
            with (self.service.state_dir / 'experimental-groups.lock').open('a+b') as handle:
                if handle.seek(0,2) == 0:
                    handle.write(b'\0');handle.flush()
                handle.seek(0)
                try:
                    if os.name == 'nt':
                        import msvcrt
                        msvcrt.locking(handle.fileno(),msvcrt.LK_NBLCK,1)
                    else:
                        import fcntl
                        fcntl.flock(handle.fileno(),fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as exc:
                    raise RuntimeError('another experimental group controller holds the state directory') from exc
                try:
                    yield
                finally:
                    handle.seek(0)
                    if os.name == 'nt':msvcrt.locking(handle.fileno(),msvcrt.LK_UNLCK,1)
                    else:fcntl.flock(handle.fileno(),fcntl.LOCK_UN)

    @staticmethod
    def _digest(plan):
        return hashlib.sha256(json.dumps({k:v for k,v in plan.items()
            if k not in ('digest', 'state', 'execution_id', 'confirmation')},
            sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()

    @staticmethod
    def _confirmation(plan):
        return f"APPLY GROUPS {plan['plan_id']} {plan['digest'][:12]}"

    def _load(self, ident, seen=None):
        if type(ident) is not str or not re.fullmatch('[0-9a-f]{32}', ident):
            raise ValueError('invalid group plan ID')
        seen = set() if seen is None else set(seen)
        if ident in seen or len(seen) >= 16:
            raise ValueError('invalid restoration ancestry')
        seen.add(ident)
        plan = json.loads((self.service.state_dir / f'group-plan-{ident}.json').read_text(encoding='utf-8'))
        if plan.get('plan_id') != ident or plan.get('digest') != self._digest(plan):
            raise ValueError('group plan checksum mismatch')
        for key in ('created_at', 'expires_at'):
            if type(plan[key]) not in (int,float) or not math.isfinite(plan[key]):
                raise ValueError('invalid plan time')
        state = _state(plan['channel'], plan['baseline'])
        addresses = {c['address'] for c in plan['changes']}
        proposal = plan_group_candidates(plan['channel'],
            [r for r in _members(plan['baseline']) if r['address'] in addresses], plan['changes'])
        if proposal != plan['proposal']:
            raise ValueError('group proposal differs from validated intent')
        if plan.get('restore_of'):
            parent = self._load(plan['restore_of'], seen)
            if parent['state'] != 'consumed' or identity(parent['gateway']) != identity(plan['gateway']):
                raise ValueError('invalid restoration source')
            if plan['channel'] != parent['channel'] or set(state) != set(_state(parent['channel'], parent['baseline'])):
                raise ValueError('restoration scope changed')
            restored = self._restoration(parent, plan['baseline'])
            if restored['proposal'] != proposal:
                raise ValueError('restoration intent differs from source original')
        elif addresses != set(state):
            raise ValueError('unexpected baseline scope')
        if 'source_project' in plan:
            if plan.get('restore_of'):
                raise ValueError('project intent and execution rollback cannot be combined')
            source = plan['source_project']
            document, canonical_source = self._project_source(
                source['project_id'], source['addresses'], source['group_numbers'])
            if source != canonical_source or plan['channel'] != document['scope']['channel']:
                raise ValueError('project source or selected scope changed')
            if identity(plan['gateway']) != identity(document['gateway']):
                raise ValueError('group plan gateway differs from project')
            if plan['changes'] != self._project_changes(document, source, plan['baseline']):
                raise ValueError('group intent differs from selected project memberships')
        if 'source_workbook' in plan:
            from .native_workbook_groups import NativeWorkbookGroups
            NativeWorkbookGroups(self.service).validate_plan(plan)
        return plan

    @staticmethod
    def _read(g, channel, addresses):
        result = []
        for address in addresses:
            membership = g.read_groups(channel, address)
            typ = g.light_type(channel, address)
            actual = g.read_actual(channel, address, typ == 1)
            if actual['light_type_code'] != typ:
                raise ValueError('live type changed during read')
            result.append(dict(membership={k:membership[k] for k in ('channel','address','groups','group_mask')},
                               group_reply=membership, actual=actual, captured_at=time.time()))
        _state(channel, result)
        if [r['membership']['address'] for r in result] != list(addresses):
            raise ValueError('readback address scope mismatch')
        return result

    def _prepare(self, info, channel, baseline, changes, restore_of=None, source_project=None, source_workbook=None):
        addresses = {c['address'] for c in changes}
        proposal = plan_group_candidates(channel,
            [r for r in _members(baseline) if r['address'] in addresses], changes)
        plan = dict(plan_id=uuid.uuid4().hex, gateway=info, channel=channel,
                    baseline=copy.deepcopy(baseline), changes=copy.deepcopy(changes), proposal=proposal,
                    restore_of=restore_of, created_at=time.time(), expires_at=time.time()+600,
                    state='prepared', validation='physical B0/group15 add/remove verified on pinned DT8 firmware1.6.9; broader cases simulated')
        if source_project is not None:
            plan['source_project'] = copy.deepcopy(source_project)
        if source_workbook is not None:
            plan['source_workbook'] = copy.deepcopy(source_workbook)
        plan['digest'] = self._digest(plan)
        self.service._save('group-plan', plan, plan['plan_id'])
        return dict(plan, confirmation=self._confirmation(plan))

    def prepare(self, channel, changes):
        # Reuse the strict planner to reject malformed edits before any I/O.
        if not isinstance(changes, list) or any(not isinstance(c,dict) or 'address' not in c for c in changes):
            raise ValueError('explicit group edits required')
        dummy = [dict(channel=channel,address=c['address'],groups=[],group_mask=0) for c in changes]
        plan_group_candidates(channel, dummy, changes)
        addresses = sorted(c['address'] for c in changes)
        with self._locked():
            g, info = self.service._open()
            try:
                return self._prepare(info, channel, self._read(g,channel,addresses), changes)
            finally:
                g.close()

    def _project_source(self, project_id, addresses, group_numbers):
        document = validate_document(self.service.get_project(project_id))
        for values, maximum, label in ((addresses, 63, 'addresses'), (group_numbers, 15, 'group_numbers')):
            if (not isinstance(values, list) or not 1 <= len(values) <= maximum + 1 or
                any(type(v) is not int or not 0 <= v <= maximum for v in values) or
                len(set(values)) != len(values)):
                raise ValueError(f'explicit unique {label}0..{maximum} required')
        if not set(addresses) <= set(document['scope']['addresses']):
            raise ValueError('requested addresses were not captured in the project')
        config = self.service.config
        configured = (config['gateway_ip'], config['expected_dali_mac'].lower(), config['expected_firmware'])
        if identity(document['gateway']) != configured:
            raise ValueError('project belongs to a different configured gateway')
        source = dict(project_id=project_id, digest=document['digest'],
                      addresses=sorted(addresses), group_numbers=sorted(group_numbers))
        return document, source

    @staticmethod
    def _project_changes(document, source, baseline):
        """Selected archived membership bits only; all other current bits survive."""
        current = _state(document['scope']['channel'], baseline)
        if set(current) != set(source['addresses']):
            raise ValueError('project restoration baseline scope mismatch')
        rows = {row['address']: row for row in document['rows']}
        changes = []
        for address in source['addresses']:
            parameters = rows[address]['parameters']
            if current[address][1][0] != parameters['light_type']['value']['code']:
                raise ValueError('current fixture type differs from captured project')
            target_mask = parameters['groups']['value']['group_mask']
            changes.append(dict(address=address,
                add=[g for g in source['group_numbers'] if target_mask & (1 << g)],
                remove=[g for g in source['group_numbers'] if not target_mask & (1 << g)]))
        return changes

    def prepare_project_restore(self, project_id, addresses, group_numbers):
        """Read-only preview; execution uses the existing exact-operation gate.

        A complete toolkit snapshot is required. Imported provenance is not
        authenticated. Neither omitted addresses nor omitted group bits imply
        deletion, and matching targets create no executable plan.
        """
        document, source = self._project_source(project_id, addresses, group_numbers)
        channel = document['scope']['channel']
        with self._locked():
            g, info = self.service._open()
            try:
                if identity(info) != identity(document['gateway']):
                    raise ValueError('current gateway differs from project')
                baseline = self._read(g, channel, source['addresses'])
                changes = self._project_changes(document, source, baseline)
                proposal = plan_group_candidates(channel, _members(baseline), changes)
                if not proposal['steps']:
                    return dict(state='already_matches', channel=channel, source_project=source,
                                baseline=baseline, proposal=proposal, device_writes=False,
                                notice='Only selected addresses/group bits checked; no execution plan created.')
                return self._prepare(info, channel, baseline, changes, source_project=source)
            finally:
                g.close()

    def _check_gate(self, plan):
        config = self.service.config
        if config.get('enable_experimental_group_writes') is not True:
            raise PermissionError('separate group-write enablement and user authorization required')
        allowed = config.get('allowed_group_write_operations')
        if not isinstance(allowed, list) or not allowed:
            raise PermissionError('explicit group operation scope required')
        for item in allowed:
            try:
                if not isinstance(item,dict) or set(item) != {'channel','address','group','member'}:
                    raise ValueError('invalid operation fields')
                encode_group_candidate(**item)
            except (TypeError, ValueError) as exc:
                raise PermissionError('invalid group operation scope') from exc
        for step in plan['proposal']['steps']:
            operation = dict(channel=plan['channel'], **{k:step[k] for k in ('address','group','member')})
            if operation not in allowed:
                raise PermissionError('group operation outside exact execution scope')

    def execute(self, ident, confirmation):
        with self._locked():
            plan = self._load(ident)
            self._check_gate(plan)
            if confirmation != self._confirmation(plan):
                raise ValueError('confirmation mismatch')
            if plan['state'] != 'prepared' or time.time() > plan['expires_at']:
                raise ValueError('group plan consumed or expired')
            run_id = uuid.uuid4().hex
            # Exclusive claim persists even on preflight failure. A new fresh
            # plan is required; deleting a claim to retry is never supported.
            with (self.service.state_dir / f'group-claim-{ident}.json').open('x',encoding='utf-8') as claim:
                json.dump(dict(plan_id=ident,execution_id=run_id,claimed_at=time.time()),claim)
                claim.flush(); os.fsync(claim.fileno())
            run = dict(execution_id=run_id,plan_id=ident,state='preflight',steps=[],
                       started_at=time.time(),automatic_rollback=False,receipt_status_interpreted=False)
            def save():self.service._save('group-run',run,run_id)
            g = None
            try:
                g, info = self.service._open()
                if identity(info) != identity(plan['gateway']):raise ValueError('gateway changed')
                channel = plan['channel']
                addresses = sorted(_state(channel,plan['baseline']))
                fresh = self._read(g,channel,addresses)
                expected = _state(channel,plan['baseline'])
                run['fresh_before'] = fresh
                if _state(channel,fresh) != expected:raise ValueError('membership or live output changed since preparation')
                save()
                plan.update(state='consumed',execution_id=run_id)
                self.service._save('group-plan',plan,ident)
                for candidate in plan['proposal']['steps']:
                    address = candidate['address']
                    fresh_target = self._read(g,channel,[address])
                    if _state(channel,fresh_target)[address] != expected[address]:
                        run['state']='stopped_conflict';run['conflict_readback']=fresh_target;break
                    step = dict(candidate=candidate,state='intent_recorded')
                    run['steps'].append(step);run['state']='writing';save()
                    send_error = None
                    try:
                        step['receipt'] = send_group_once(g,channel,address,candidate['group'],candidate['member'])
                    except (Exception,KeyboardInterrupt) as exc:
                        send_error = str(exc) or type(exc).__name__
                        step['dispatch_error'] = send_error
                        g.close();g=None
                        g,info=self.service._open()  # Read-only recovery, never resend.
                        if identity(info) != identity(plan['gateway']):raise ValueError('gateway changed after uncertain write')
                    step['readback']=self._read(g,channel,[address])
                    expected[address]=(candidate['after_mask'],expected[address][1])
                    if _state(channel,step['readback'])[address] != expected[address]:
                        step['state']='verification_failed';run['state']='stopped_verification_failed';break
                    step['state']='verified_receipt_unknown' if send_error else 'verified_readback'
                    if send_error:run['state']='stopped_receipt_unknown';break
                    save()
                else:
                    run['final_readback']=self._read(g,channel,addresses)
                    run['state']=('completed_readback' if _state(channel,run['final_readback'])==expected
                                  else 'stopped_final_conflict')
            except (Exception,KeyboardInterrupt) as exc:
                run.update(state='stopped_unknown' if run['steps'] else 'preflight_failed',
                           error=str(exc) or type(exc).__name__)
            finally:
                if g is not None:g.close()
                run['finished_at']=time.time()
                try:save()
                except Exception as exc:run['log_error']=str(exc)
            return run

    def _restoration(self, plan, baseline):
        run_id=plan.get('execution_id','')
        if type(run_id) is not str or not re.fullmatch('[0-9a-f]{32}',run_id):
            raise ValueError('invalid source execution ID')
        run=json.loads((self.service.state_dir/f'group-run-{run_id}.json').read_text(encoding='utf-8'))
        if run['plan_id'] != plan['plan_id'] or run['execution_id'] != run_id:
            raise ValueError('execution does not match source plan')
        attempted=[s['candidate'] for s in run['steps']]
        if attempted != plan['proposal']['steps'][:len(attempted)]:
            raise ValueError('invalid execution prefix')
        # A restoration plan can include unchanged addresses in its baseline.
        scope={r['address'] for r in plan['proposal']['originals']}
        current_state=_state(plan['channel'],baseline)
        original_state=_state(plan['channel'],plan['baseline'])
        if set(current_state)!=set(original_state):raise ValueError('restoration scope mismatch')
        for address,(mask,live) in current_state.items():
            if live != original_state[address][1]:raise ValueError('live output changed; inspect before restoration')
            if address not in scope and mask != original_state[address][0]:raise ValueError('other membership changed')
        result=plan_group_restore_candidates(plan['proposal'],[r for r in _members(baseline) if r['address'] in scope])
        if result['matched_prefix_steps']>len(attempted):
            raise ValueError('readback exceeds recorded attempted scope')
        return result

    def prepare_restore(self, ident):
        with self._locked():
            plan=self._load(ident)
            if plan['state']!='consumed':raise ValueError('no consumed execution to restore')
            g,info=self.service._open()
            try:
                if identity(info)!=identity(plan['gateway']):raise ValueError('restoration gateway changed')
                baseline=self._read(g,plan['channel'],sorted(_state(plan['channel'],plan['baseline'])))
                result=self._restoration(plan,baseline)
                if result['proposal'] is None:
                    return dict(state='already_original',source_plan_id=ident,device_writes=False)
                return self._prepare(info,plan['channel'],baseline,result['proposal']['changes'],restore_of=ident)
            finally:g.close()

    def get(self, plan_id=None):
        """Read journals without the operation lock or gateway traffic.

        Persisted phase is not proof the owning process is running. An exclusive
        claim without a run record is reported, never cleared or retried here.
        """
        if plan_id is None:
            paths=sorted(self.service.state_dir.glob('group-plan-*.json'),
                         key=lambda p:p.stat().st_mtime,reverse=True)[:10]
            records=[self.get(p.stem.removeprefix('group-plan-')) for p in paths]
            return dict(plans=[dict(plan_id=r['plan_id'],channel=r['channel'],state=r['state'],
                created_at=r['created_at'],addresses=[x['membership']['address'] for x in r['baseline']],
                execution_state=r.get('execution',{}).get('state'),claimed=r.get('claimed',False)) for r in records])
        plan=self._load(plan_id)
        claim_path=self.service.state_dir/f'group-claim-{plan_id}.json'
        if claim_path.exists():
            claim=json.loads(claim_path.read_text(encoding='utf-8'))
            run_id=claim.get('execution_id')
            if (claim.get('plan_id')!=plan_id or type(run_id) is not str or
                not re.fullmatch('[0-9a-f]{32}',run_id) or
                plan.get('execution_id',run_id)!=run_id):raise ValueError('invalid group execution claim')
            plan['claimed']=True
            run_path=self.service.state_dir/f'group-run-{run_id}.json'
            if run_path.exists():
                run=json.loads(run_path.read_text(encoding='utf-8'))
                if run.get('plan_id')!=plan_id or run.get('execution_id')!=run_id:
                    raise ValueError('execution does not match group claim')
                plan['execution']=run
            else:plan['execution_record_missing']=True
        plan['confirmation']=self._confirmation(plan)
        plan['saved_phase_does_not_prove_process_running']=True
        return plan
