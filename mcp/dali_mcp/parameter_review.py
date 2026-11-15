"""Fresh, read-only parameter reviews. Never creates an executable write plan."""
import copy
import json
import re
import time
import uuid
from pydantic import BaseModel, ConfigDict, StrictInt
from .parameter_planning import COMMANDS, ParameterPatch, original_values, plan_parameter_candidates, validate_values
from .projects import identity, scope_values, validate_document
from .protocol import Gateway


class ParameterReviewTarget(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    address: StrictInt
    changes: list[ParameterPatch]


class ParameterReviewer:
    def __init__(self, service):
        self.service = service

    @staticmethod
    def _targets(channel, targets):
        if not isinstance(targets, list) or not 1 <= len(targets) <= 64:
            raise ValueError('provide1..64 explicit parameter targets')
        targets = [ParameterReviewTarget.model_validate(t).model_dump() for t in targets]
        scope_values(channel, [t['address'] for t in targets], [0])
        for target in targets:
            changes = target['changes']
            if not 1 <= len(changes) <= 9 or len({p['field'] for p in changes}) != len(changes):
                raise ValueError('provide1..9 unique parameter fields per address')
            for patch in changes:
                validate_values(patch['field'], patch['values'], partial=True)
        return targets

    def preview(self, channel, targets):
        return self._review(channel, self._targets(channel, targets))

    def preview_project(self, project_id, addresses, fields):
        document = validate_document(self.service.get_project(project_id))
        channel = document['scope']['channel']
        scope_values(channel, addresses, [0])
        if (not isinstance(fields, list) or not 1 <= len(fields) <= 9 or
            any(type(f) is not str or f not in COMMANDS for f in fields) or len(set(fields)) != len(fields)):
            raise ValueError('select1..9 unique supported parameter fields')
        if not set(addresses) <= set(document['scope']['addresses']):
            raise ValueError('requested addresses were not captured')
        config = self.service.config
        configured = (config['gateway_ip'], config['expected_dali_mac'].lower(), config['expected_firmware'])
        if identity(document['gateway']) != configured:
            raise ValueError('project belongs to a different configured gateway')
        rows = {r['address']: r for r in document['rows']}
        targets = []
        for address in addresses:
            values = original_values(rows[address]['parameters'])
            targets.append({'address': address, 'changes': [{'field': f, 'values': values[f]} for f in fields]})
        source = {'project_id': project_id, 'digest': document['digest'], 'fields': list(fields),
                  'gateway': copy.deepcopy(document['gateway'])}
        return self._review(channel, self._targets(channel, targets), source)

    def _review(self, channel, targets, source=None):
        ident = uuid.uuid4().hex
        report = {'review_id': ident, 'state': 'reading', 'channel': channel,
                  'targets': targets, 'rows': [], 'started_at': time.time(),
                  'source_project': source, 'device_writes': False,
                  'write_execution_available': False, 'atomic_snapshot': False,
                  'notice': 'Review only, not authorization or an executable plan. Use a separate supported prepare/apply tool for execution; fresh originals and exact scope are required. See release validation for field/device evidence.'}
        def save():
            self.service._save('parameter-review', report, ident)
        with self.service.lock:
            g = None
            try:
                save()
                g, info = self.service._open()
                report['gateway'] = info
                if source and identity(info) != identity(source['gateway']):
                    raise ValueError('connected gateway differs from source project')
                for target in targets:
                    address = target['address']
                    row = {'channel': channel, 'address': address, 'state': 'reading', 'parameters': {}}
                    report['rows'].append(row)
                    typ = None
                    for field in Gateway.PARAMETER_FIELDS:
                        report['pending'] = {'address': address, 'field': field}
                        save()
                        value = g.read_lamp_parameter(channel, address, field, typ)
                        value['captured_at'] = time.time()
                        row['parameters'][field] = value
                        if field == 'light_type':
                            typ = value['value']['code']
                        save()
                    report['pending'] = {'address': address, 'field': 'actual'}
                    save()
                    row['actual'] = g.read_actual(channel, address, typ == 1)
                    row['captured_at'] = time.time()
                    try:
                        if typ != 1 or row['actual']['light_type_code'] != typ:
                            raise ValueError('parameter reviews currently require matching DT8 parameter/live types')
                        # No historical limits override: a backup does not prove today's physical CT limits.
                        proposal = plan_parameter_candidates(channel, address, row, target['changes'])
                        row.update(state='reviewed', proposal=proposal)
                    except ValueError as exc:
                        row.update(state='unsupported', error=str(exc))
                    report.pop('pending', None)
                    save()
                report.update(state='reviewed' if all(r['state'] == 'reviewed' for r in report['rows']) else 'reviewed_with_rejections',
                              completed_at=time.time())
            except (Exception, KeyboardInterrupt) as exc:
                report.update(state='stopped_partial', error=str(exc) or type(exc).__name__, stopped_at=time.time())
            finally:
                try:
                    save()
                finally:
                    if g is not None:
                        g.close()
        return report

    def get(self, review_id=None):
        # Do not wait on the bus lock: this is a saved phase, not process liveness.
        if review_id is None:
            paths = sorted(self.service.state_dir.glob('parameter-review-*.json'), key=lambda p: p.stat().st_mtime, reverse=True)
            result = {'reviews': [], 'invalid_records': [], 'total_records': len(paths), 'truncated': len(paths) > 10}
            for path in paths[:10]:
                ident = path.stem.removeprefix('parameter-review-')
                try:
                    row = self.get(ident)
                    result['reviews'].append({k: row.get(k) for k in ('review_id', 'state', 'channel', 'started_at', 'pending', 'source_project')})
                except (ValueError, OSError) as exc:
                    result['invalid_records'].append({'review_id': ident, 'error': str(exc)})
            return result
        if type(review_id) is not str or not re.fullmatch('[0-9a-f]{32}', review_id):
            raise ValueError('invalid parameter review ID')
        report = json.loads((self.service.state_dir / f'parameter-review-{review_id}.json').read_text(encoding='utf-8'))
        if not isinstance(report, dict) or report.get('review_id') != review_id:
            raise ValueError('invalid parameter review record')
        return report
