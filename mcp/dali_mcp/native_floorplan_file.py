"""Restore an explicitly scoped project floorplan to one closed native app.

An exclusive new PNG is durable before the JSON reference changes. Existing
images are never overwritten/deleted. Original field/bytes/image are archived;
undo reverses only our field. Toolkit locks do not interlock application launch.
"""
import base64
import copy
import json
from pathlib import Path
import re
import time
import uuid

from .native_cache import canonical, inspect_configmaster_cache
from .native_file import NativeFileController, _no_links, digest, parse, serialize, restore_names
from .native_names import prepare_name_document
from .project_names import ProjectNames
from .native_floorplan import inspect_native_floorplan
from .project_floorplan import IMAGE_LIMIT, inspect_png
from .projects import validate_document, identity


def field(document):
    return dict(present='floorPlan' in document,value=copy.deepcopy(document.get('floorPlan')))


def same(a,b):return canonical(a)==canonical(b)


def restore_field(current,old,new):
    if same(field(current),field(old)):return copy.deepcopy(current)
    if not same(field(current),field(new)):
        raise ValueError('floorPlan changed after write; refusing to overwrite newer layout')
    result=copy.deepcopy(current)
    if 'floorPlan' in old:result['floorPlan']=copy.deepcopy(old['floorPlan'])
    else:result.pop('floorPlan',None)
    return result


class NativeFloorplanFileController(NativeFileController):
    write_gate='enable_native_floorplan_writes'

    def _record_path(self,kind,ident,suffix='json'):
        if type(ident) is not str or not re.fullmatch('[0-9a-f]{32}',ident):
            raise ValueError('invalid native floorplan plan ID')
        return self.service.state_dir/f'native-floorplan-{kind}-{ident}.{suffix}'

    @staticmethod
    def _confirmation(plan):
        return f"WRITE NATIVE FLOORPLAN {plan['plan_id']} {plan['digest']}"

    def _asset_path(self,path,name):
        if path.parent.name!='gateways':
            raise ValueError('observed native floorplan storage requires cache under a gateways directory')
        if (type(name) is not str or not name or Path(name).name!=name
                or re.search(r'[\\/:*?"<>|\x00-\x1f]',name) or not name.lower().endswith('.png')):
            raise ValueError('invalid native background filename')
        target=path.parent.parent/'floorplans'/name
        _no_links(target)
        return target

    def _asset_snapshot(self,path,document):
        summary=inspect_native_floorplan(document)
        if not summary['present']:return dict(state='no_floorplan'),None
        filename=summary['image_reference'];target=self._asset_path(path,filename)
        if not target.exists():return dict(state='missing',filename=filename),None
        with target.open('rb') as f:data=f.read(IMAGE_LIMIT+1)
        return dict(state='present',filename=filename,metadata=inspect_png(data)),data

    def _check_old_asset(self,path,old,expected):
        actual,_=self._asset_snapshot(path,old)
        if not same(actual,expected):raise ValueError('original background changed; refusing uncertain restoration')

    @staticmethod
    def _name_changes(project,selections):
        return ProjectNames.changes(dict(attachment=project['native_names'],scope=project['scope'],
                                         gateway=project['gateway'],selections=selections))

    def _desired(self,project,before,ident,mark_scope,name_selections=None):
        attachment=project['native_floorplan']
        desired=copy.deepcopy(attachment['summary']['native_floorplan'])
        if mark_scope=='captured_marks':
            scope=project['scope']
            desired['marks']=[m for m in desired['marks']
                              if m['ch']==scope['channel'] and m['addr'] in scope['addresses']]
        elif mark_scope!='all_source_marks':raise ValueError('explicit mark_scope must be captured_marks or all_source_marks')
        desired['image']='dali-toolkit-'+ident+'.png'
        result=copy.deepcopy(before);result['floorPlan']=desired
        if name_selections is not None:
            _,result=prepare_name_document(result,self._name_changes(project,name_selections))
        inspect_native_floorplan(result)
        return result

    def _save_plan(self,plan,before,after):
        plan.update(format='native-floorplan-file-v1',created_at=time.time(),expires_at=time.time()+600,
                    before_sha256=digest(before),after_sha256=digest(after),device_commands_sent=0)
        plan['digest']=self._plan_digest(plan)
        ident=plan['plan_id']
        self._write_new(self._record_path('before',ident,'bin'),before)
        self._write_new(self._record_path('after',ident,'bin'),after)
        self._write_new(self._record_path('plan',ident),canonical(plan))
        return dict(plan,confirmation=self._confirmation(plan),application_file_written=False)

    def prepare(self,project_id,mark_scope,name_selections=None):
        if mark_scope not in ('captured_marks','all_source_marks'):raise ValueError('explicit mark_scope required')
        path=self._target()
        with self._locked(path):
            self.guard()
            project=validate_document(self.service.get_project(project_id))
            if 'native_floorplan' not in project:raise ValueError('project has no native floorplan attachment')
            before,document=self._read(path)
            if identity(project['gateway'])!=identity(inspect_configmaster_cache(document)['gateway']):
                raise ValueError('project and native file gateway differ')
            ident=uuid.uuid4().hex
            target=self._desired(project,document,ident,mark_scope,name_selections)
            details={}
            if name_selections is not None:
                name_diff,_=prepare_name_document(document,self._name_changes(project,name_selections))
                details=dict(name_selections=copy.deepcopy(name_selections),name_diff=name_diff)
            after=serialize(target,before);parse(after)
            new_path=self._asset_path(path,target['floorPlan']['image'])
            if new_path.exists():raise FileExistsError('new background path already exists')
            original_asset,original_bytes=self._asset_snapshot(path,document)
            source=json.dumps(project,ensure_ascii=False,allow_nan=False,separators=(',',':')).encode('utf-8')
            self._write_new(self._record_path('project',ident,'bin'),source)
            if original_bytes is not None:self._write_new(self._record_path('original-image',ident,'bin'),original_bytes)
            return self._save_plan(dict(plan_id=ident,operation='floorplan',file_path=str(path),
                source_project_id=project_id,source_project_digest=project['digest'],source_sha256=digest(source),
                mark_scope=mark_scope,before_floorplan=field(document),after_floorplan=field(target),
                original_asset=original_asset,new_image_path=str(new_path),
                target_mark_count=len(target['floorPlan']['marks']),
                local_floorplan_replaced=True,existing_images_preserved=True,
                generated_images_retained_after_undo=True,**details),before,after)

    def _load(self,ident):
        plan=json.loads(self._record_path('plan',ident).read_text(encoding='utf-8'))
        if plan.get('format')!='native-floorplan-file-v1' or plan.get('plan_id')!=ident or plan.get('digest')!=self._plan_digest(plan):
            raise ValueError('native floorplan plan checksum mismatch')
        path=self._target()
        if plan['file_path']!=str(path):raise ValueError('configured cache path changed')
        before=self._record_path('before',ident,'bin').read_bytes()
        after=self._record_path('after',ident,'bin').read_bytes()
        if digest(before)!=plan['before_sha256'] or digest(after)!=plan['after_sha256']:
            raise ValueError('native floorplan backup checksum mismatch')
        first,last=parse(before),parse(after)
        for d in (first,last):self.service._check_native_identity(inspect_configmaster_cache(d))
        if not same(plan['before_floorplan'],field(first)) or not same(plan['after_floorplan'],field(last)):
            raise ValueError('floorplan review differs from backup')
        if plan.get('device_commands_sent') != 0 or type(plan.get('device_commands_sent')) is not int:
            raise ValueError('invalid device command review')
        if plan.get('generated_images_retained_after_undo') is not True:
            raise ValueError('invalid retained image review')
        if plan['operation']=='floorplan':
            if plan.get('local_floorplan_replaced') is not True or plan.get('existing_images_preserved') is not True:
                raise ValueError('invalid floorplan replacement review')
            raw=self._record_path('project',ident,'bin').read_bytes()
            if len(raw)>5_000_000 or digest(raw)!=plan['source_sha256']:raise ValueError('project source backup mismatch')
            project=validate_document(json.loads(raw))
            if project['project_id']!=plan['source_project_id'] or project['digest']!=plan['source_project_digest']:
                raise ValueError('project source identity mismatch')
            if identity(project['gateway'])!=identity(inspect_configmaster_cache(first)['gateway']):
                raise ValueError('project source gateway mismatch')
            if ('name_selections' in plan)!=('name_diff' in plan):raise ValueError('incomplete combined name review')
            if 'name_selections' in plan:
                diff,_=prepare_name_document(first,self._name_changes(project,plan['name_selections']))
                if diff!=plan['name_diff']:raise ValueError('combined names differ from source')
            expected=self._desired(project,first,ident,plan['mark_scope'],plan.get('name_selections'))
            if serialize(expected,before)!=after:raise ValueError('plan changes more than scoped floorplan')
            if type(plan['target_mark_count']) is not int or plan['target_mark_count']!=len(expected['floorPlan']['marks']):
                raise ValueError('target mark count mismatch')
            if plan['new_image_path']!=str(self._asset_path(path,expected['floorPlan']['image'])):
                raise ValueError('new image path differs from owned filename')
            original_summary=inspect_native_floorplan(first);asset=plan['original_asset']
            if not original_summary['present']:
                if asset!=dict(state='no_floorplan'):raise ValueError('unexpected original image')
            else:
                if asset.get('filename')!=original_summary['image_reference']:raise ValueError('original image reference mismatch')
                if asset.get('state')=='present':
                    old_image=self._record_path('original-image',ident,'bin').read_bytes()
                    if not same(asset.get('metadata'),inspect_png(old_image)):raise ValueError('original image backup mismatch')
                elif asset.get('state')!='missing':raise ValueError('invalid original image state')
        elif plan['operation']=='restore':
            source_id=plan['source_plan_id']
            source_head=json.loads(self._record_path('plan',source_id).read_text(encoding='utf-8'))
            if source_head.get('operation')!='floorplan':raise ValueError('undo source must be original floorplan write')
            source,old,new=self._load(source_id)
            self._require_intent(source)
            desired=self._restore_document(first,source,old,new)
            expected=old if before==new else (before if same(desired,first) else serialize(desired,before))
            if expected!=after:raise ValueError('restore changes unrelated native fields')
            if plan['source_digest']!=source['digest']:raise ValueError('undo source digest mismatch')
            if (type(plan.get('exact_original_bytes')) is not bool or plan['exact_original_bytes']!=(before==new)
                    or plan.get('unrelated_current_fields_preserved') is not True):
                raise ValueError('invalid floorplan restoration review')
        else:raise ValueError('invalid native floorplan operation')
        return plan,before,after

    @staticmethod
    def _restore_document(current,source,old,new):
        result=restore_field(current,parse(old),parse(new))
        if 'name_selections' in source:result=restore_names(result,parse(old),source['name_diff'])
        return result

    def _check_gates(self,plan):
        source=plan if plan['operation']=='floorplan' else self._load(plan['source_plan_id'])[0]
        if self.service.config.get(self.write_gate) is not True:raise ValueError('native floorplan write gate is disabled')
        if 'name_selections' in source and self.service.config.get('enable_native_cache_writes') is not True:
            raise ValueError('combined floorplan/name write requires both native write gates')

    def apply(self,ident,confirmation,*,_deadline_check=None):
        self._check_gates(self._load(ident)[0])
        return super().apply(ident,confirmation,_deadline_check=_deadline_check)

    def _require_intent(self,plan):
        intent=json.loads(self._record_path('intent',plan['plan_id']).read_text(encoding='utf-8'))
        if intent!={'plan_id':plan['plan_id'],'digest':plan['digest']}:raise ValueError('missing original replacement intent')

    def _project_image(self,plan):
        project=validate_document(json.loads(self._record_path('project',plan['plan_id'],'bin').read_bytes()))
        return base64.b64decode(project['native_floorplan']['image']['base64'],validate=True)

    def _stage_dependencies(self,plan,path,report):
        if plan['operation']=='restore':return
        self._check_source_current(plan)
        self._check_old_asset(path,parse(self._record_path('before',plan['plan_id'],'bin').read_bytes()),plan['original_asset'])
        target=self._asset_path(path,plan['after_floorplan']['value']['image'])
        target.parent.mkdir(exist_ok=True)
        _no_links(target)
        report.update(new_image_path=str(target),generated_image_retained=True,
                      image_cleanup='Retained as transaction evidence; existing images never deleted')
        self._write_new(target,self._project_image(plan))
        report['image_created']=True

    def _check_source_current(self,plan):
        source=validate_document(self.service.get_project(plan['source_project_id']))
        if source['digest']!=plan['source_project_digest']:raise ValueError('source project changed since plan')

    def _check_dependencies(self,plan,path):
        # Both permissions are checked again immediately before a combined commit.
        if 'plan_id' in plan:self._check_gates(plan)
        source=plan
        if plan['operation']=='restore':source,_,_=self._load(plan['source_plan_id'])
        else:self._check_source_current(plan)
        old=parse(self._record_path('before',source['plan_id'],'bin').read_bytes())
        self._check_old_asset(path,old,source['original_asset'])
        target=self._asset_path(path,source['after_floorplan']['value']['image'])
        with target.open('rb') as f:actual=f.read(IMAGE_LIMIT+1)
        if actual!=self._project_image(source):raise ValueError('generated background changed or incomplete')

    def prepare_restore(self,ident):
        source,old,new=self._load(ident)
        if source['operation']!='floorplan':raise ValueError('only original floorplan plans can be restored')
        self._require_intent(source)
        path=self._target()
        with self._locked(path):
            self.guard();current,document=self._read(path)
            restored=self._restore_document(document,source,old,new)
            if same(restored,document):return dict(state='already_restored',source_plan_id=ident,application_file_written=False)
            self._check_dependencies(dict(operation='restore',source_plan_id=ident),path)
            after=old if current==new else serialize(restored,current)
            return self._save_plan(dict(plan_id=uuid.uuid4().hex,operation='restore',file_path=str(path),
                source_plan_id=ident,source_digest=source['digest'],
                before_floorplan=field(document),after_floorplan=field(restored),
                exact_original_bytes=current==new,unrelated_current_fields_preserved=True,
                generated_images_retained_after_undo=True),current,after)
