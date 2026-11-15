"""Native XLSX source adapter for combined restoration; never a fake bus snapshot."""
import copy
import re
from .native_workbook_store import NativeWorkbookStore,decode_record
from .native_workbook_parameters import NativeWorkbookParameters
from .native_workbook_scenes import NativeWorkbookScenes
from .parameter_planning import COMMANDS
from .project_restore import ProjectRestoreReviewer
from .projects import scope_values


class EmbeddedWorkbook:
    def __init__(self,evidence):
        if not isinstance(evidence,dict) or set(evidence)!={'workbook_id','summary','source_base64'}:
            raise ValueError('invalid embedded workbook evidence')
        self.record=evidence;self.decoded=decode_record(evidence,evidence['workbook_id'])

    def get(self,workbook_id,channel):
        if workbook_id!=self.record['workbook_id'] or type(channel) is not int or channel not in (1,2):
            raise ValueError('embedded source scope mismatch')
        return dict(workbook_id=workbook_id,summary=self.record['summary'],channel_details=self.decoded['channels'][channel-1])


class WorkbookRestoreSource:
    def __init__(self,service):self.service=service

    @staticmethod
    def scope(spec):
        if not isinstance(spec,dict) or set(spec)!={'workbook_id','channel','addresses','fields','group_numbers','scenes','scene_source','group','brightness_policy'}:
            raise ValueError('invalid native combined source selection')
        if type(spec['workbook_id']) is not str or not re.fullmatch('[0-9a-f]{32}',spec['workbook_id']):raise ValueError('invalid workbook ID')
        scope_values(spec['channel'],spec['addresses'],[0])
        ProjectRestoreReviewer._selection(spec['fields'],tuple(COMMANDS),'parameter fields')
        for key in ('group_numbers','scenes'):ProjectRestoreReviewer._selection(spec[key],range(16),key)
        if not any(spec[k] for k in ('fields','group_numbers','scenes')):raise ValueError('select at least one native restoration category')
        if spec['brightness_policy'] not in ('require_unique','native_nearest'):raise ValueError('invalid brightness policy')
        if spec['scenes']:
            if spec['scene_source'] not in ('ecg','group'):raise ValueError('explicit native scene source required')
            if spec['scene_source']=='ecg' and spec['group'] is not None:raise ValueError('group must be omitted for ECG scenes')
            if spec['scene_source']=='group' and (type(spec['group']) is not int or not 0<=spec['group']<=15):raise ValueError('explicit scene group0..15 required')
        elif spec['scene_source'] is not None or spec['group'] is not None:raise ValueError('omit scene source/group when no scenes selected')
        return dict(channel=spec['channel'],addresses=sorted(spec['addresses']),fields=list(spec['fields']),
            group_numbers=sorted(spec['group_numbers']),scenes=sorted(spec['scenes']))

    def resolve(self,evidence,spec):
        scope=self.scope(spec);store=EmbeddedWorkbook(evidence)
        record=store.get(spec['workbook_id'],scope['channel']);ch=record['channel_details']
        fixtures={f['address']:f for f in ch['fixtures']}
        if not set(scope['addresses'])<=fixtures.keys():raise ValueError('selected addresses missing from native workbook')
        if any(fixtures[a]['light_type']!='DT8' for a in scope['addresses']):raise ValueError('native combined restoration currently requires DT8')
        params=NativeWorkbookParameters(self.service,store=store);scene_targets=[];scene_review=None
        if scope['scenes']:
            scene_targets,_,scene_review=NativeWorkbookScenes(self.service,store=store).targets(
                spec['workbook_id'],scope['channel'],scope['addresses'],scope['scenes'],spec['scene_source'],spec['group'],spec['brightness_policy'])
        intent=[];parameter_reviews=[]
        for a in scope['addresses']:
            changes=[]
            if scope['fields']:
                changes,_,review=params.source(spec['workbook_id'],scope['channel'],a,scope['fields'],spec['brightness_policy'])
                parameter_reviews.append(dict(address=a,**review))
            intent.append(dict(address=a,parameters=changes,
                scenes=[dict(s,light_type_code=1) for s in scene_targets if s['address']==a],
                groups=[dict(group=g,member=g in fixtures[a]['groups']) for g in scope['group_numbers']]))
        review=dict(parameter_reviews=parameter_reviews,scene_review=scene_review,
            membership_source='native grouping sheet',source_provenance='unverified',device_values_verified=False,
            original_precision_guaranteed=False,source_sha256=evidence['summary']['source_sha256'],
            notice='Native XLSX targets with explicit quantization/source policy, not a bus backup. Fresh current originals are captured separately. Blank parameters keep; blank scene cells MASK. Names/layout/address/gateway settings are not restored.')
        return scope,intent,review

    def capture(self,spec):
        self.scope(spec)  # Bad selection never touches source files or network.
        evidence=NativeWorkbookStore(self.service).snapshot(spec['workbook_id'])
        scope,intent,review=self.resolve(evidence,spec);cfg=self.service.config
        source=dict(format='dali-native-workbook-restoration-source',schema_version=1,
            gateway=dict(ip=cfg['gateway_ip'],dali_mac=cfg['expected_dali_mac'].lower(),dali_firmware=cfg['expected_firmware']),
            evidence=evidence,spec=copy.deepcopy(spec),review=review)
        return source,scope,intent

    def validate(self,source,scope):
        if not isinstance(source,dict) or set(source)!={'format','schema_version','gateway','evidence','spec','review'}:
            raise ValueError('invalid embedded native source')
        if source['format']!='dali-native-workbook-restoration-source' or type(source['schema_version']) is not int or source['schema_version']!=1:
            raise ValueError('unsupported native source format')
        actual_scope,intent,review=self.resolve(source['evidence'],source['spec'])
        if actual_scope!=scope or review!=source['review']:raise ValueError('native source scope/review changed')
        if source['gateway']['dali_mac'].lower()!=source['evidence']['summary']['dali_mac']:raise ValueError('native source gateway binding mismatch')
        return intent

    def unchanged(self,source,scope):
        current,actual_scope,_=self.capture(source['spec'])
        if current!=source or actual_scope!=scope:raise ValueError('native workbook source changed since preparation')
