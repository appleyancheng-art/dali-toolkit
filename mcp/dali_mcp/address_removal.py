"""Explicit single short-address removal, with backup and no implicit allocation.

Removal makes a fixture unreachable by its former short address. The backup is
recovery evidence, not a hardware identity or a promise of automatic undo.
"""
import copy
import json
import math
import os
import time
import uuid
from .address_moves import AddressMoveController, address_set, signature


def scope(channel, address):
    if type(channel) is not int or channel not in (1, 2):
        raise ValueError('explicit channel1/2 required')
    if type(address) is not int or not 0 <= address <= 63:
        raise ValueError('single short address0..63 required')
    return dict(channel=channel, address=address)


class AddressRemovalController:
    def __init__(self, service, process_guard=None):
        self.service = service
        self.base = AddressMoveController(service, process_guard)

    def _path(self, kind, ident):
        self.base._id(ident)
        return self.service.state_dir / f'address-removal-{kind}-{ident}.json'

    def _save(self, kind, ident, data):
        path = self._path(kind, ident)
        temp = path.with_suffix('.tmp')
        with temp.open('w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2, allow_nan=False)
            f.flush()
            os.fsync(f.fileno())
        # Retry only local publication on a transient Windows sharing denial.
        # Device commands are never retried.
        for attempt in range(6):
            try:
                temp.replace(path)
                break
            except PermissionError as exc:
                if getattr(exc, 'winerror', None) not in (5,32,33) or attempt == 5:
                    raise
                time.sleep(.01 * (2**attempt))

    @staticmethod
    def _confirmation(plan):
        return f"REMOVE ADDRESS {plan['plan_id']} {plan['digest']}"

    def _load(self, ident):
        p = json.loads(self._path('plan', ident).read_text(encoding='utf-8'))
        if p.get('plan_id') != ident or p.get('digest') != self.base._digest(p):
            raise ValueError('address removal plan checksum mismatch')
        scope(p['channel'], p['address'])
        if p.get('operation') != 'remove_single_short_address':
            raise ValueError('invalid removal operation')
        before = address_set(p['before_addresses'])
        if p['address'] not in before or p['after_addresses'] != [a for a in before if a != p['address']]:
            raise ValueError('invalid removal occupancy')
        if p['original']['channel'] != p['channel'] or p['original']['address'] != p['address']:
            raise ValueError('original lamp target mismatch')
        signature(p['original'])
        for key in ('created_at', 'expires_at'):
            if type(p[key]) not in (int, float) or not math.isfinite(p[key]):
                raise ValueError('invalid plan time')
        if p['expires_at'] != p['created_at'] + 600:
            raise ValueError('invalid plan lifetime')
        if any(p.get(k) is not False for k in ('automatic_undo', 'physical_identity_verified', 'host_mappings_migrated')):
            raise ValueError('invalid removal guarantees')
        return p

    def prepare(self, channel, address):
        scope(channel, address)
        with self.base._locked():
            self.base.process_guard()
            info, scan = self.base._scan(channel)
            if address not in scan['addresses']:
                raise ValueError('source address absent')
            duplicates = self.base._duplicates(channel, info)
            original = self.base._lamp(channel, address, info)
            now = time.time()
            p = dict(plan_id=uuid.uuid4().hex, operation='remove_single_short_address',
                     **scope(channel, address), gateway=info, before_addresses=scan['addresses'],
                     after_addresses=[a for a in scan['addresses'] if a != address],
                     original=original, scan_evidence=scan, duplicate_evidence=duplicates,
                     created_at=now, expires_at=now + 600, state='prepared',
                     automatic_undo=False, physical_identity_verified=False, host_mappings_migrated=False,
                     notice='Removes only this short address. The unaddressed gear cannot be read by that address. '
                            'No automatic undo: incremental addressing is a separate channel-wide operation and '
                            'cannot prove which physical fixture was recovered. Host labels and archived mappings remain unchanged.')
            p['digest'] = self.base._digest(p)
            self._save('plan', p['plan_id'], p)
            return dict(p, confirmation=self._confirmation(p))

    def _gate(self, p):
        cfg = self.service.config
        if cfg.get('enable_experimental_address_removal') is not True:
            raise PermissionError('separate address removal gate required')
        if cfg.get('allowed_address_removal_digest') != p['digest']:
            raise PermissionError('exact removal plan digest required')
        allowed = cfg.get('allowed_address_removals')
        if not isinstance(allowed, list) or not allowed:
            raise PermissionError('exact allowed_address_removals required')
        for item in allowed:
            try:
                if not isinstance(item, dict) or set(item) != {'channel', 'address'}:
                    raise ValueError()
                scope(**item)
            except (ValueError, TypeError) as exc:
                raise PermissionError('invalid address removal scope') from exc
        if scope(p['channel'], p['address']) not in allowed:
            raise PermissionError('removal outside exact allowed scope')

    def execute(self, ident, confirmation):
        with self.base._locked():
            p = self._load(ident)
            self._gate(p)
            if confirmation != self._confirmation(p):
                raise ValueError('confirmation mismatch')
            if p['state'] != 'prepared' or time.time() > p['expires_at']:
                raise ValueError('plan consumed or expired')
            self.base.process_guard()
            run_id = uuid.uuid4().hex
            with self._path('claim', ident).open('x', encoding='utf-8') as f:
                json.dump(dict(plan_id=ident, execution_id=run_id, claimed_at=time.time()), f)
                f.flush()
                os.fsync(f.fileno())
            run = dict(plan_id=ident, execution_id=run_id, state='preflight', started_at=time.time(),
                       write_intent=False, automatic_undo=False, physical_identity_verified=False,
                       unaddressed_lamp_configuration_verified=False)
            def save():
                self._save('run', run_id, run)
            g = None
            try:
                save()
                p.update(state='consumed', execution_id=run_id)
                self._save('plan', ident, p)
                ch, addr = p['channel'], p['address']
                _, run['before_scan'] = self.base._scan(ch, p['gateway'])
                if run['before_scan']['addresses'] != p['before_addresses']:
                    raise ValueError('address occupancy changed since preparation')
                run['duplicates'] = self.base._duplicates(ch, p['gateway'])
                run['before_lamp'] = self.base._lamp(ch, addr, p['gateway'])
                if signature(run['before_lamp']) != signature(p['original']):
                    raise ValueError('source parameters/scenes/live changed')
                _, run['dispatch_scan'] = self.base._scan(ch, p['gateway'])
                if run['dispatch_scan']['addresses'] != p['before_addresses']:
                    raise ValueError('address occupancy changed during preflight')
                if time.time() > p['expires_at']:
                    raise ValueError('plan expired during preflight')
                self._gate(p)
                self.base.process_guard()
                run['capability_invalidation_id'] = self.service.invalidate_capabilities(ch, [addr], f'address removal {run_id}')
                g, _ = self.base._open(p['gateway'])
                run.update(state='writing', write_intent=True, attempt=scope(ch, addr))
                save()  # No device write before durable intent.
                try:
                    run['receipt'] = g.change_short_address(ch, addr, 255)
                except (Exception, KeyboardInterrupt) as exc:
                    run['dispatch_error'] = str(exc) or type(exc).__name__
                finally:
                    run['protocol_replies'] = list(g.recent_packets)
                    g.close()
                    g = None
                run['state'] = 'reading_after'
                save()
                _, run['after_scan'] = self.base._scan(ch, p['gateway'])
                if run['after_scan']['addresses'] != p['after_addresses']:
                    run['state'] = 'stopped_address_mismatch'
                else:
                    run['after_duplicates'] = self.base._duplicates(ch, p['gateway'])
                    run['state'] = 'verified_receipt_unknown' if 'dispatch_error' in run else 'completed_address_removal_readback'
            except (Exception, KeyboardInterrupt) as exc:
                run.update(state='stopped_unknown' if run['write_intent'] else 'preflight_failed',
                           error=str(exc) or type(exc).__name__)
            finally:
                if g is not None:
                    g.close()
                run['finished_at'] = time.time()
                try:
                    save()
                except Exception as exc:
                    run['log_error'] = str(exc)
            return run

    def get(self, ident):
        p = copy.deepcopy(self._load(ident))
        cp = self._path('claim', ident)
        p['claimed'] = cp.exists()
        p['saved_phase_does_not_prove_process_running'] = True
        if not p['claimed']:
            p['confirmation'] = self._confirmation(p)
            return p
        claim = json.loads(cp.read_text(encoding='utf-8'))
        run_id = self.base._id(claim.get('execution_id'))
        if claim.get('plan_id') != ident or p.get('execution_id', run_id) != run_id:
            raise ValueError('removal claim mismatch')
        path = self._path('run', run_id)
        if path.exists():
            run = json.loads(path.read_text(encoding='utf-8'))
            if run.get('plan_id') != ident or run.get('execution_id') != run_id:
                raise ValueError('removal execution mismatch')
            p['execution'] = run
        else:
            p['execution_record_missing'] = True
        return p
