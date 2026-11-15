"""One-session, read-only cross-category project restoration preflight.

All selected originals are captured before computing candidates. This report is
not consumable by any executor; execution must bind fresh state separately.
"""
import copy
import json
import re
import time
import uuid
from .projects import digest, identity, scope_values, validate_document
from .parameter_control import LampParameterController
from .parameter_execution import ExperimentalParameterController
from .parameter_planning import COMMANDS, original_values, plan_parameter_candidates, valid_limits
from .group_planning import plan_group_candidates


class ProjectRestoreReviewer:
    def __init__(self, service):
        self.service = service

    @staticmethod
    def _selection(values, allowed, label):
        if (not isinstance(values, list) or len(values) > len(allowed) or
            any(type(v) is not type(next(iter(allowed))) or v not in allowed for v in values) or
            len(set(values)) != len(values)):
            raise ValueError('invalid explicit ' + label)
        return list(values)

    def _source(self, project_id, addresses, fields, group_numbers, scenes):
        document = validate_document(self.service.get_project(project_id))
        channel = document['scope']['channel']
        scope_values(channel, addresses, [0])
        self._selection(fields, tuple(COMMANDS), 'parameter fields')
        self._selection(group_numbers, range(16), 'group numbers')
        self._selection(scenes, range(16), 'scene numbers')
        if not (fields or group_numbers or scenes):
            raise ValueError('select at least one restoration category')
        if not set(addresses) <= set(document['scope']['addresses']) or not set(scenes) <= set(document['scope']['scenes']):
            raise ValueError('selected addresses/scenes were not captured')
        cfg = self.service.config
        if identity(document['gateway']) != (cfg['gateway_ip'], cfg['expected_dali_mac'].lower(), cfg['expected_firmware']):
            raise ValueError('project belongs to a different configured gateway')
        rows = {r['address']: r for r in document['rows']}
        if any(rows[a]['parameters']['light_type']['value']['code'] != 1 for a in addresses):
            raise ValueError('combined restoration review currently supports DT8 only')
        return copy.deepcopy(document)

    @staticmethod
    def _scene(value, channel, address, scene):
        expected = dict(channel=channel, address=address, scene=scene, light_type_code=1)
        if any(type(value.get(k)) is not int or value[k] != v for k, v in expected.items()):
            raise ValueError('scene reply/source scope or type mismatch')
        for key, maximum in (('level_raw',255), ('kelvin_raw',65535)):
            if type(value.get(key)) is not int or not 0 <= value[key] <= maximum:
                raise ValueError('invalid scene raw value')
        if value['kelvin_raw'] != 65535 and not 1000 <= value['kelvin_raw'] <= 10000:
            raise ValueError('unsupported scene Kelvin')
        return {**expected, 'level_raw':value['level_raw'], 'kelvin_raw':value['kelvin_raw']}

    @staticmethod
    def _within(scene, values):
        return ((scene['level_raw'] in (0,255) or values['min_level']['level_raw'] <= scene['level_raw'] <= values['max_level']['level_raw']) and
                (scene['kelvin_raw'] == 65535 or values['configured_min_ct']['kelvin_raw'] <= scene['kelvin_raw'] <= values['configured_max_ct']['kelvin_raw']))

    def _compare(self, source, current, fields, groups, scenes):
        channel, address = current['channel'], current['address']
        before = original_values(current['parameters'])
        targets = original_values(source['parameters'])
        projected = copy.deepcopy(before)
        result = dict(address=address, rejections=[], parameters=[], groups=[], scenes=[])
        projection_valid = valid_limits(before)
        if not projection_valid:
            result['rejections'].append(dict(category='parameters',error='current limits invalid'))
        changes = [dict(field=f,values=targets[f]) for f in fields]
        result['parameters'] = [dict(field=f,before=before[f],after=targets[f],changed=before[f]!=targets[f]) for f in fields]
        if changes:
            try:
                for change in changes:
                    LampParameterController._validate_scope(current,[change])
                proposal = plan_parameter_candidates(channel,address,current,changes)
                result['parameter_order'] = [s['field'] for s in proposal['steps']]
                for f in fields:
                    projected[f] = copy.deepcopy(targets[f])
            except ValueError as exc:
                projection_valid = False
                result['rejections'].append(dict(category='parameters',error=str(exc)))
        if groups:
            old = current['parameters']['groups']['value']
            wanted = source['parameters']['groups']['value']['groups']
            proposal = plan_group_candidates(channel,[dict(channel=channel,address=address,**old)],
                [dict(address=address,add=[g for g in groups if g in wanted],remove=[g for g in groups if g not in wanted])])
            result['group_order'] = [dict(group=s['group'],member=s['member']) for s in proposal['steps']]
            for g in groups:
                b, a = g in old['groups'], g in wanted
                result['groups'].append(dict(group=g,before=b,after=a,changed=b!=a))
            result['projected_group_mask'] = (old['group_mask'] & ~sum(1<<g for g in groups)) | sum(1<<g for g in groups if g in wanted)
        selected = {s['scene']:s for s in source['scenes']}
        for old in current['scenes']:
            sn = old['scene']
            try:
                b = self._scene(old,channel,address,sn)
                a = self._scene(selected[sn],channel,address,sn)
                valid = self._within(a,projected) if projection_valid else None
                result['scenes'].append(dict(scene=sn,before=b,after=a,changed=b!=a,
                    fits_current_limits=self._within(a,before),fits_projected_limits=valid))
                if not valid:
                    result['rejections'].append(dict(category='scenes',scene=sn,error='scene target outside projected limits' if projection_valid else 'dependent parameter projection rejected'))
            except ValueError as exc:
                result['rejections'].append(dict(category='scenes',scene=sn,error=str(exc)))
        result['projected_limits'] = {f:projected[f] for f in ('min_level','max_level','configured_min_ct','configured_max_ct')}
        result['parameter_projection_valid'] = projection_valid
        return result

    def preview(self, project_id, addresses, fields, group_numbers, scenes):
        source = self._source(project_id,addresses,fields,group_numbers,scenes)
        channel = source['scope']['channel']; ident = uuid.uuid4().hex
        report = dict(review_id=ident,state='reading',started_at=time.time(),device_writes=False,
            write_execution_available=False,atomic_snapshot=False,
            source_project=dict(project_id=project_id,digest=source['digest'],gateway=source['gateway']),
            scope=dict(channel=channel,addresses=sorted(addresses),fields=fields,group_numbers=sorted(group_numbers),scenes=sorted(scenes)),
            originals=[],rows=[],
            suggested_category_order=['parameters','scenes','groups'],
            not_restored=['live_output','PC_labels','physical_layouts','addresses','gateway_settings','physical_ct_limits','extended_fade','device_version'],
            limitations=['Read-only preflight, not an executable plan or authorization.',
                'All selected parameters, memberships, live outputs and scenes read serially; not an atomic snapshot.',
                'Selected scene targets use projected limits after selected parameter changes; unselected scenes are not checked.',
                'Parameter changes may affect live light output; original output is evidence only, not restored by this tool.',
                'Suggested category order does not prove a complete forward/rollback transaction. Execution orchestration remains separate.'])
        def save(): self.service._save('project-restore-review',report,ident)
        with self.service.lock:
            g = None
            try:
                save(); g, info = self.service._open(); report['gateway']=info
                if identity(info) != identity(source['gateway']):
                    raise ValueError('connected gateway differs from source project')
                for address in sorted(addresses):
                    report['pending']=dict(address=address,category='parameters_and_live');save()
                    snapshot=ExperimentalParameterController._snapshot(g,channel,address)
                    snapshot['scenes']=[];report['originals'].append(snapshot);save()
                    for scene in sorted(scenes):
                        report['pending']=dict(address=address,category='scene',scene=scene);save()
                        value=g.read_scene(channel,address,scene,1)
                        self._scene(value,channel,address,scene)
                        snapshot['scenes'].append(value);save()
                report['pending']=dict(category='comparison');save()
                rows={r['address']:r for r in source['rows']}
                report['rows']=[self._compare(rows[r['address']],r,fields,group_numbers,scenes) for r in report['originals']]
                # A changed source during the capture cannot silently become the target.
                if self._source(project_id,addresses,fields,group_numbers,scenes)['digest'] != source['digest']:
                    raise ValueError('source project changed during capture')
                report['counts']={k:dict(selected=sum(len(r[k]) for r in report['rows']),changed=sum(v['changed'] for r in report['rows'] for v in r[k])) for k in ('parameters','groups','scenes')}
                rejected=sum(len(r['rejections']) for r in report['rows'])
                report['counts']['rejections']=rejected
                report.update(state='reviewed_with_rejections' if rejected else 'reviewed',completed_at=time.time())
                report.pop('pending',None)
            except (Exception,KeyboardInterrupt) as exc:
                report.update(state='stopped_partial',error=str(exc) or type(exc).__name__,stopped_at=time.time())
            finally:
                report['digest']=digest(report)
                try: save()
                finally:
                    if g is not None:g.close()
        return report

    def get(self, review_id):
        if type(review_id) is not str or not re.fullmatch('[0-9a-f]{32}',review_id):
            raise ValueError('invalid project restoration review ID')
        report=json.loads((self.service.state_dir/f'project-restore-review-{review_id}.json').read_text(encoding='utf-8'))
        if not isinstance(report,dict) or report.get('review_id')!=review_id:
            raise ValueError('invalid project restoration review record')
        if 'digest' in report and report['digest']!=digest(report):
            raise ValueError('project restoration review checksum mismatch')
        return report
