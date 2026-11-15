"""Native parameter fields mapped without inventing a fresh toolkit project."""
from .native_workbook_store import NativeWorkbookStore
from .native_percent import review_native_percent
from .projects import identity

FIELDS=('fade_time','fade_rate','min_level','max_level','configured_min_ct','configured_max_ct','power_on','system_failure','dimming_curve')


class NativeWorkbookParameters:
    def __init__(self,service,store=None):
        self.service=service;self.store=NativeWorkbookStore(service) if store is None else store

    def source(self,workbook_id,channel,address,fields,brightness_policy='require_unique'):
        from .parameter_control import LampParameterController
        if type(channel) is not int or channel not in (1,2) or type(address) is not int or not 0<=address<=63:
            raise ValueError('explicit channel1/2 and address0..63 required')
        if (not isinstance(fields,list) or not 1<=len(fields)<=9 or
            any(type(f) is not str or f not in FIELDS for f in fields) or len(set(fields))!=len(fields)):
            raise ValueError('select1..9 unique supported parameter fields')
        if brightness_policy not in ('require_unique','native_nearest'):
            raise ValueError('invalid brightness policy')
        record=self.store.get(workbook_id,channel)
        fixture=next((f for f in record['channel_details']['fixtures'] if f['address']==address),None)
        if fixture is None or fixture['light_type']!='DT8':raise ValueError('selected native DT8 fixture required')
        params=fixture['parameters'];changes=[];kept=[];quantization=[]
        for field in fields:
            if field in ('power_on','system_failure'):
                components=[(field+'_level','level_raw'),(field+'_ct','kelvin_raw')]
            else:
                key='raw' if field=='dimming_curve' else 'code' if field.startswith('fade_') else 'kelvin_raw' if field.startswith('configured_') else 'level_raw'
                components=[(field,key)]
            values={}
            for native,key in components:
                component=params[native]
                if component['kind']=='keep':kept.append(native);continue
                if component['kind']=='mask':value=255 if key=='level_raw' else 65535
                elif key=='raw':value={'对数':0,'线性':1}[component['label']]
                elif key=='level_raw':
                    review=review_native_percent(component['percent'])
                    if brightness_policy=='require_unique' and not review['original_raw_level_uniquely_recoverable']:
                        raise ValueError(f'ambiguous/non-native native parameter percentage: {native}')
                    value=review['native_import_level_raw'];quantization.append(dict(field=native,**review))
                else:value=component['code' if key=='code' else 'kelvin']
                values[key]=value
            if not values:continue
            if field in ('power_on','system_failure'):LampParameterController._paired_values(field,values)
            else:LampParameterController._values(field,next(iter(values.values())))
            changes.append(dict(field=field,values=values))
        binding=dict(workbook_id=workbook_id,source_sha256=record['summary']['source_sha256'],channel=channel,
            address=address,fields=list(fields),brightness_policy=brightness_policy)
        review=dict(kept_blank_components=kept,percent_review=quantization,source_provenance='unverified',
            native_import_runtime_verified=False,original_precision_guaranteed=False)
        return changes,binding,review

    def prepare(self,workbook_id,channel,address,fields,brightness_policy='require_unique'):
        from .parameter_control import LampParameterController
        changes,binding,review=self.source(workbook_id,channel,address,fields,brightness_policy)
        if not changes:
            return dict(state='no_targets_in_source',source_workbook=binding,native_workbook_review=review,
                device_writes=False,network_traffic=False,notice='All selected source components are blank/keep; current device parameters were not checked.')
        controller=LampParameterController(self.service)
        with controller._locked():
            g,info=self.service._open()
            try:
                snapshot=controller._snapshot(g,channel,address)
                return controller._prepare(info,snapshot,changes,source_workbook=binding,native_workbook_review=review)
            finally:g.close()

    def validate_plan(self,plan):
        if 'source_project' in plan:raise ValueError('native and toolkit parameter sources cannot be combined')
        try:
            source=plan['source_workbook']
            changes,binding,review=self.source(**{k:v for k,v in source.items() if k!='source_sha256'})
            if source!=binding or plan['native_workbook_review']!=review:
                raise ValueError('native parameter source binding changed')
            if plan['channel']!=source['channel'] or plan['address']!=source['address']:
                raise ValueError('native parameter target scope changed')
            cfg=self.service.config
            if identity(plan['gateway'])!=(cfg['gateway_ip'],cfg['expected_dali_mac'].lower(),cfg['expected_firmware']):
                raise ValueError('native parameter configured gateway changed')
            if plan['restore_of'] is None and plan['changes']!=changes:
                raise ValueError('parameter targets differ from selected native fields')
            active={c['field'] for c in changes}
            if any(c['field'] not in active for c in plan['changes']):
                raise ValueError('parameter undo exceeds nonblank selected native fields')
        except (KeyError,TypeError,AttributeError) as exc:
            raise ValueError('malformed native parameter source') from exc
