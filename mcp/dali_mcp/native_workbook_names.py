"""Selected native XLSX labels mapped into scoped local-cache name transactions."""
import copy
from .native_workbook_store import NativeWorkbookStore
from .native_names import name_path


class NativeWorkbookNames:
    def __init__(self,service):self.service=service;self.store=NativeWorkbookStore(service)

    def source(self,workbook_id,channel,selections,group_name_source='grouping'):
        if type(channel) is not int or channel not in (1,2):raise ValueError('explicit channel1/2 required')
        if not isinstance(selections,list) or not 1<=len(selections)<=336:
            raise ValueError('select1..336 explicit lamp/group/scene label targets')
        if group_name_source not in ('grouping','group_scene'):raise ValueError('invalid group name source')
        # Validate exact shapes, indices and duplicates before reading files.
        seen=set()
        for selection in selections:
            if not isinstance(selection,dict) or 'channel' in selection or 'name' in selection:
                raise ValueError('name selections contain only kind and its address/group/scene indices')
            path=tuple(name_path(dict(selection,channel=channel,name='')))
            if path in seen:raise ValueError('duplicate native name selection')
            seen.add(path)
        record=self.store.get(workbook_id,channel);details=record['channel_details']
        fixtures={f['address']:f for f in details['fixtures']}
        blocks={b['group']:b for b in details['group_scene_blocks']}
        changes=[];conflicts=[]
        for selection in selections:
            kind=selection['kind']
            if kind=='lamp':
                if selection['address'] not in fixtures:raise ValueError('selected native lamp is absent')
                name=fixtures[selection['address']]['name']
            elif kind=='group':
                group=selection['group'];grouping=details['group_names'][str(group)];block=blocks[group]['name']
                if grouping!=block:conflicts.append(dict(group=group,grouping=grouping,group_scene=block,chosen=group_name_source))
                name=grouping if group_name_source=='grouping' else block
            else:name=blocks[selection['group']]['scene_names'][str(selection['scene'])]
            change=dict(selection,channel=channel,name=name);name_path(change);changes.append(change)
        binding=dict(workbook_id=workbook_id,source_sha256=record['summary']['source_sha256'],channel=channel,
            selections=copy.deepcopy(selections),group_name_source=group_name_source,
            lamp_name_source='parameter sheet',scene_name_source='group scene block',
            group_name_conflicts=conflicts,source_provenance='unverified',blank_names='clear selected label')
        return changes,binding

    def prepare(self,workbook_id,channel,selections,group_name_source='grouping'):
        from .native_file import NativeFileController
        changes,binding=self.source(workbook_id,channel,selections,group_name_source)
        return NativeFileController(self.service).prepare(changes,source_workbook=binding)

    def validate_plan(self,plan):
        if plan.get('operation')!='names':raise ValueError('native label source belongs only to a name edit')
        try:
            source=plan['source_workbook']
            changes,binding=self.source(source['workbook_id'],source['channel'],source['selections'],source['group_name_source'])
            if source!=binding or changes!=plan['changes']:raise ValueError('native label source/selected targets changed')
        except (KeyError,TypeError,AttributeError) as exc:raise ValueError('malformed native label source') from exc
