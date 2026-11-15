"""Reviewable native JSON name drafts; no application-file or device writes."""
import copy
import hashlib
import json
import re
import time
import unicodedata
import uuid
from .native_cache import canonical, inspect_configmaster_cache


def _index(value, maximum, label):
    if type(value) is not int or not 0 <= value <= maximum:
        raise ValueError(f'{label} requires a strict index0..{maximum}')


def name_path(change):
    if not isinstance(change, dict):raise ValueError('name change must be an object')
    kind=change.get('kind')
    fields={'lamp':{'address'},'group':{'group'},'scene':{'group','scene'}}
    if type(kind) is not str or kind not in fields:raise ValueError('kind must be lamp, group or scene')
    if set(change) != fields[kind] | {'kind','channel','name'}:raise ValueError('unexpected or missing name-edit fields')
    if type(change['channel']) is not int or change['channel'] not in (1,2):raise ValueError('channel1/2 required')
    name=change['name']
    if type(name) is not str:raise ValueError('name must be text; empty string clears the label')
    try:length=len(name.encode('utf-8'))
    except UnicodeError as exc:raise ValueError('name must be valid Unicode') from exc
    if length>30 or any(unicodedata.category(c) in ('Cc','Cs') for c in name):
        raise ValueError('native name requires at most30 UTF8 bytes and no control characters; never truncated')
    prefix=[f"zCh{change['channel']}"]
    if kind=='lamp':
        _index(change['address'],63,'address')
        return prefix+['addresses',str(change['address']),'name']
    _index(change['group'],15,'group')
    if kind=='group':return prefix+['groupNames',str(change['group'])]
    _index(change['scene'],15,'scene')
    return prefix+['sceneNamesByGroup',str(change['group']),str(change['scene'])]


def read_name(document,path):
    node=document
    for part in path:
        if part not in node:return dict(present=False,name=None)
        node=node[part]
    return dict(present=True,name=node)


def _set_name(document,path,name):
    node=document
    for part in path[:-1]:node=node.setdefault(part,{})
    node[path[-1]]=name


def prepare_name_document(document,changes):
    inspect_configmaster_cache(document)
    if not isinstance(changes,list) or not 1<=len(changes)<=672:
        raise ValueError('provide1..672 explicit name edits')
    seen=set();diff=[];draft=copy.deepcopy(document)
    for change in changes:
        path=name_path(change);key=tuple(path)
        if key in seen:raise ValueError('duplicate name target')
        seen.add(key)
        if change['kind']=='lamp' and str(change['address']) not in document[path[0]]['addresses']:
            raise ValueError('lamp is absent from this cache; do not create device rows while naming')
        before=read_name(document,path);after=dict(present=True,name=change['name'])
        diff.append(dict(path=path,before=before,after=after,changed=before!=after))
        if before!=after:_set_name(draft,path,change['name'])
    inspect_configmaster_cache(draft)
    return diff,draft


class NativeNameMixin:
    @staticmethod
    def _name_digest(plan):
        return hashlib.sha256(canonical({k:v for k,v in plan.items() if k!='plan_digest'})).hexdigest()

    def prepare_configmaster_names(self,cache_id,changes):
        with self.lock:
            cached=self.get_configmaster_cache(cache_id,True)
            diff,_=prepare_name_document(cached['document'],changes)
            plan=dict(format='configmaster-name-plan-v1',plan_id=uuid.uuid4().hex,source_cache_id=cache_id,
                      source_document_sha256=cached['document_sha256'],changes=copy.deepcopy(changes),
                      diff=diff,created_at=time.time(),application_file_written=False,device_commands_sent=0)
            plan['plan_digest']=self._name_digest(plan)
            self._save('native-name-plan',plan,plan['plan_id'])
            return plan

    def _load_name_plan(self,plan_id):
        if type(plan_id) is not str or not re.fullmatch('[0-9a-f]{32}',plan_id):raise ValueError('invalid name plan ID')
        plan=json.loads((self.state_dir/f'native-name-plan-{plan_id}.json').read_text(encoding='utf-8'))
        if plan.get('plan_id')!=plan_id or plan.get('plan_digest')!=self._name_digest(plan):
            raise ValueError('name plan checksum mismatch')
        cached=self.get_configmaster_cache(plan['source_cache_id'],True)
        if cached['document_sha256']!=plan['source_document_sha256']:raise ValueError('source cache changed')
        diff,document=prepare_name_document(cached['document'],plan['changes'])
        if diff!=plan['diff']:raise ValueError('name plan differs from validated intent')
        return plan,cached,document

    def get_configmaster_name_plan(self,plan_id,include_document=False):
        if type(include_document) is not bool:raise ValueError('include_document must be boolean')
        plan,_,document=self._load_name_plan(plan_id)
        if include_document:
            plan['draft_document']=document
            plan['draft_document_sha256']=hashlib.sha256(canonical(document)).hexdigest()
        return plan

    def rebase_configmaster_names(self,plan_id,current_document):
        """Compare supplied current JSON; never claim it was read from the app.

        Keep unrelated fresh fields. Concurrent changes to an edited name block
        the whole draft unless the desired name is already present. No source
        file path, file-write primitive or bus operation is provided here.
        """
        plan,cached,_=self._load_name_plan(plan_id)
        summary=inspect_configmaster_cache(current_document);self._check_native_identity(summary)
        if summary['gateway']!=cached['summary']['gateway']:raise ValueError('current cache gateway changed')
        draft=copy.deepcopy(current_document);conflicts=[];resolutions=[]
        for change,entry in zip(plan['changes'],plan['diff']):
            path=entry['path'];current=read_name(current_document,path)
            missing_lamp=(change['kind']=='lamp' and str(change['address']) not in current_document[path[0]]['addresses'])
            if missing_lamp or (current!=entry['before'] and current!=entry['after']):
                conflicts.append(dict(path=path,expected=entry['before'],current=current,target=entry['after'],
                                      reason='lamp_removed_from_cache' if missing_lamp else 'name_changed_since_plan'))
            elif current==entry['after']:
                resolutions.append(dict(path=path,state='already_current'))
            else:
                _set_name(draft,path,change['name'])
                resolutions.append(dict(path=path,state='edited_in_draft'))
        result=dict(plan_id=plan_id,state='conflict' if conflicts else 'draft_ready',conflicts=conflicts,
                    resolutions=resolutions,compared_document_sha256=hashlib.sha256(canonical(current_document)).hexdigest(),
                    comparison_source='caller_supplied_JSON; actual file freshness not established by this tool',
                    application_file_written=False,device_commands_sent=0)
        if not conflicts:
            inspect_configmaster_cache(draft)
            result.update(draft_document=draft,draft_document_sha256=hashlib.sha256(canonical(draft)).hexdigest())
        return result
