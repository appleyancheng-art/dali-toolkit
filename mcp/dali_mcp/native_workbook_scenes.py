"""Selected native workbook scenes -> fresh, source-bound scene write plans.

No native application import or automatic device writes. Raw-level ambiguity is
an explicit policy decision; ECG/group source selection never implies merging.
"""
from .native_workbook_store import NativeWorkbookStore
from .native_percent import review_native_percent
from .projects import scope_values


class NativeWorkbookScenes:
    def __init__(self, service, store=None):
        self.service=service
        self.store=NativeWorkbookStore(service) if store is None else store

    def targets(self,workbook_id,channel,addresses,scenes,scene_source,group=None,
                brightness_policy='require_unique'):
        scope_values(channel,addresses,scenes)
        if scene_source not in ('ecg','group'):
            raise ValueError('explicit scene_source ecg or group required')
        if brightness_policy not in ('require_unique','native_nearest'):
            raise ValueError('brightness_policy must be require_unique or native_nearest')
        if scene_source=='ecg' and group is not None:
            raise ValueError('group must be omitted for ECG source')
        if scene_source=='group' and (type(group) is not int or not 0<=group<=15):
            raise ValueError('explicit group0..15 required for group source')
        record=self.store.get(workbook_id,channel)
        ch=record['channel_details']
        fixtures={f['address']:f for f in ch['fixtures']}
        selected=fixtures if scene_source=='ecg' else {
            r['address']:r for b in ch['group_scene_blocks'] if b['group']==group for r in b['rows']}
        if not set(addresses)<=selected.keys():
            raise ValueError('requested addresses missing from selected native scene table/block')
        changes=[];quantization=[]
        for a in sorted(addresses):
            if fixtures[a]['light_type']!='DT8':
                raise ValueError('native scene restoration currently requires DT8 source fixtures')
            stored={s['scene']:s for s in selected[a]['scenes']}
            for s in sorted(scenes):
                value=stored[s];light=value['brightness'];color=value['color']
                if light['kind']=='mask':level=255
                else:
                    review=review_native_percent(light['percent'])
                    if brightness_policy=='require_unique' and not review['original_raw_level_uniquely_recoverable']:
                        raise ValueError(f'ambiguous/non-native percentage at address{a} scene{s}; select native_nearest only if quantization is intended')
                    level=review['native_import_level_raw']
                    quantization.append(dict(address=a,scene=s,**review))
                kelvin=65535 if color['kind']=='mask' else color['kelvin']
                if kelvin!=65535 and not 1000<=kelvin<=10000:
                    raise ValueError('native scene Kelvin outside supported1000..10000/MASK')
                changes.append(dict(channel=channel,address=a,scene=s,level_raw=level,kelvin_raw=kelvin))
        binding=dict(workbook_id=workbook_id,source_sha256=record['summary']['source_sha256'],
            channel=channel,addresses=sorted(addresses),scenes=sorted(scenes),
            scene_source=scene_source,group=group,brightness_policy=brightness_policy)
        conflicts=[c for c in ch['scene_conflicts'] if c['address'] in addresses and c['scene'] in scenes]
        return changes,binding,dict(percent_review=quantization,source_conflicts=conflicts,
            conflict_resolution='explicit selected table only; no last-group override or merged values',
            source_provenance='unverified',native_import_runtime_verified=False,
            original_precision_guaranteed=False,
            notice='This is an explicit native-table target, not a proven exact backup. Stored percentages may lose raw precision. Fresh originals are backed up separately; no names, groups, parameters, addressing or live output restored.')

    def prepare(self,workbook_id,channel,addresses,scenes,scene_source,group=None,
                brightness_policy='require_unique'):
        changes,binding,review=self.targets(workbook_id,channel,addresses,scenes,scene_source,group,brightness_policy)
        with self.service.lock:
            # Reuse existing1..1024-target backup, identity/limits checks and
            # write/verification journal; do not fabricate a toolkit project.
            plan=self.service.preview(changes,verify_limits=True)
            plan.update(native_workbook_source=binding,native_workbook_review=review,
                restore_scope='selected native workbook stored scenes only')
            self.service._save('plan',{k:v for k,v in plan.items() if k!='confirmation'},plan['plan_id'])
            return plan

    def validate_plan(self,plan):
        binding=plan['native_workbook_source']
        try:
            changes,recomputed,review=self.targets(**{k:v for k,v in binding.items() if k!='source_sha256'})
            if binding!=recomputed or plan['native_workbook_review']!=review:
                raise ValueError('native scene source binding changed')
            expected=[dict(c,light_type_code=1) for c in changes]
            if [r['after'] for r in plan['rows']]!=expected:
                raise ValueError('scene targets differ from selected native workbook source')
            gateway=plan['gateway'];cfg=self.service.config
            if (gateway['ip'],gateway['dali_mac'].lower(),gateway['dali_firmware']) != (
                cfg['gateway_ip'],cfg['expected_dali_mac'].lower(),cfg['expected_firmware']):
                raise ValueError('native scene plan configured gateway binding changed')
        except (KeyError,TypeError,AttributeError) as exc:
            raise ValueError('malformed native scene source binding') from exc


def verify_current_limits(g,rows):
    """Validate ALL selected targets against fresh limits before any write."""
    bounds={}
    for row in rows:
        target=row['after'];key=(target['channel'],target['address'])
        if key not in bounds:
            p=g.read_lamp_profile(*key)
            if p['light_type_code']!=1:raise ValueError('current native restore fixture is not DT8')
            low=g.read_lamp_parameter(*key,'min_level',1)['value']['level_raw']
            high=g.read_lamp_parameter(*key,'max_level',1)['value']['level_raw']
            bounds[key]=(p['min_kelvin_raw'],p['max_kelvin_raw'],low,high)
        low_k,high_k,low,high=bounds[key]
        if not 1000<=low_k<=high_k<=10000 or not 1<=low<=high<=254:
            raise ValueError('current native restore limits unknown')
        if target['kelvin_raw']!=65535 and not low_k<=target['kelvin_raw']<=high_k:
            raise ValueError('native scene target outside current Kelvin limits')
        if target['level_raw'] not in (0,255) and not low<=target['level_raw']<=high:
            raise ValueError('native scene target outside current brightness limits')
