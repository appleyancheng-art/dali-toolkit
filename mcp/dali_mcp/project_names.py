"""Portable local labels and source-bound native-name restoration, no bus IO."""
import copy
import hashlib
import re
import time
import uuid
from .native_cache import canonical, inspect_configmaster_cache
from .native_names import name_path, read_name

CONTEXT='local_labels_not_device_configuration_or_online_evidence'


def selections_check(scope,selections):
    if not isinstance(selections,list) or not 1<=len(selections)<=336:
        raise ValueError('select1..336 explicit label targets')
    seen=set()
    for item in selections:
        if not isinstance(item,dict) or 'name' in item or 'channel' in item:
            raise ValueError('label selection contains kind and indices only')
        path=tuple(name_path(dict(item,channel=scope['channel'],name='')))
        if path in seen:raise ValueError('duplicate label selection')
        seen.add(path)
        if item['kind']=='lamp' and item['address'] not in scope['addresses']:
            raise ValueError('lamp label outside captured addresses')
        if item['kind']=='scene' and item['scene'] not in scope['scenes']:
            raise ValueError('scene label outside captured scene numbers')


def entries_from(source,scope,selections):
    selections_check(scope,selections);entries=[]
    for item in selections:
        path=name_path(dict(item,channel=scope['channel'],name=''))
        value=read_name(source,path)
        if item['kind']=='lamp' and not value['present']:
            raise ValueError('selected lamp absent from source cache')
        entries.append(dict(selection=copy.deepcopy(item),**value))
    return entries


def validate_attachment(item,scope,gateway):
    from .projects import identity
    if not isinstance(item,dict) or item.get('context')!=CONTEXT:
        raise ValueError('native names require local evidence context')
    source=item['source_document'];summary=inspect_configmaster_cache(source)
    if identity(summary['gateway'])!=identity(gateway):raise ValueError('names/project gateway mismatch')
    if item['source_document_sha256']!=hashlib.sha256(canonical(source)).hexdigest():
        raise ValueError('names source checksum mismatch')
    if canonical(item['entries'])!=canonical(entries_from(source,scope,item['selections'])):
        raise ValueError('names selection differs from source evidence')


def validate_project_names(document):
    if document['schema_version']!=4:
        if 'native_names' in document:raise ValueError('native names require schema4')
        return
    validate_attachment(document.get('native_names'),document['scope'],document['gateway'])


class ProjectNames:
    def __init__(self,service):self.service=service

    def attach(self,project_id,cache_id,selections):
        from .projects import validate_document,digest
        with self.service.lock:
            project=validate_document(self.service.get_project(project_id))
            record=self.service.get_configmaster_cache(cache_id,True)
            item=dict(context=CONTEXT,source_document=record['document'],
                      source_document_sha256=record['document_sha256'],selections=copy.deepcopy(selections),
                      entries=entries_from(record['document'],project['scope'],selections))
            validate_attachment(item,project['scope'],project['gateway'])
            result=copy.deepcopy(project)
            result.update(schema_version=4,project_id=uuid.uuid4().hex,native_names=item,
                origin='derived_project_with_native_names',names_attached_at=time.time(),
                parent_project=dict(project_id=project_id,digest=project['digest']))
            # PC_labels remains excluded from bus capture; this separate source is explicitly local.
            result['digest']=digest(result);validate_document(result)
            self.service._save('project',result,result['project_id'])
            return dict(project_id=result['project_id'],digest=result['digest'],entries=copy.deepcopy(item['entries']),
                        state='attached',device_writes=False,network_traffic=False,source_context=CONTEXT)

    def get(self,project_id):
        from .projects import validate_document
        project=validate_document(self.service.get_project(project_id))
        item=project.get('native_names')
        return dict(project_id=project_id,present=item is not None,device_writes=False,network_traffic=False,
            source_context=CONTEXT,entries=[] if item is None else copy.deepcopy(item['entries']))

    @staticmethod
    def changes(binding):
        validate_attachment(binding['attachment'],binding['scope'],binding['gateway'])
        selections_check(binding['scope'],binding['selections'])
        source=binding['attachment']['entries'];changes=[]
        for selection in binding['selections']:
            matches=[e for e in source if e['selection']==selection]
            if len(matches)!=1:raise ValueError('label not included in project attachment')
            entry=matches[0]
            # Missing local group/note label means native default, not keep-current.
            change=dict(selection,channel=binding['scope']['channel'],name=entry['name'] if entry['present'] else '')
            name_path(change);changes.append(change)
        return changes

    def prepare(self,project_id,selections):
        from .projects import validate_document
        from .native_file import NativeFileController
        project=validate_document(self.service.get_project(project_id))
        if 'native_names' not in project:raise ValueError('project has no name attachment')
        binding=dict(project_id=project_id,project_digest=project['digest'],scope=copy.deepcopy(project['scope']),
                     gateway=copy.deepcopy(project['gateway']),attachment=copy.deepcopy(project['native_names']),
                     selections=copy.deepcopy(selections))
        return NativeFileController(self.service).prepare(self.changes(binding),source_project_names=binding)

    def validate_plan(self,plan,current=False):
        from .projects import validate_document,identity
        try:
            binding=plan['source_project_names']
            if plan.get('operation')!='names' or 'source_workbook' in plan:
                raise ValueError('project labels require exactly one name source')
            if not re.fullmatch('[0-9a-f]{32}',binding['project_id']) or not re.fullmatch('[0-9a-f]{64}',binding['project_digest']):
                raise ValueError('invalid name source binding')
            if self.changes(binding)!=plan['changes']:raise ValueError('name edits differ from bound project')
            cfg=self.service.config
            if identity(binding['gateway'])!=(cfg['gateway_ip'],cfg['expected_dali_mac'].lower(),cfg['expected_firmware']):
                raise ValueError('configured gateway differs from name source')
            if current:
                project=validate_document(self.service.get_project(binding['project_id']))
                if (project['digest']!=binding['project_digest'] or project['scope']!=binding['scope']
                    or project['gateway']!=binding['gateway'] or project.get('native_names')!=binding['attachment']):
                    raise ValueError('source project changed before label write')
        except (KeyError,TypeError,AttributeError) as exc:raise ValueError('malformed project label binding') from exc


def compare_names(before,after):
    def selected(doc):
        item=doc.get('native_names')
        return None if item is None else sorted(item['entries'],key=lambda e:canonical(e['selection']))
    a,b=selected(before),selected(after)
    state=('not_captured_both' if a is None and b is None else 'not_captured_before' if a is None
           else 'not_captured_after' if b is None else 'unchanged' if a==b else 'changed')
    return dict(state=state,before=a,after=b,context=CONTEXT)
