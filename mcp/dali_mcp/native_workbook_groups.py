"""Native workbook membership bits, retaining source and current-state evidence."""
from .native_workbook_store import NativeWorkbookStore
from .group_execution import ExperimentalGroupController,_state,_members
from .group_planning import plan_group_candidates
from .projects import identity


class NativeWorkbookGroups:
    def __init__(self,service):
        self.service=service
        self.store=NativeWorkbookStore(service)
        self.controller=ExperimentalGroupController(service)

    def source(self,workbook_id,channel,addresses,group_numbers):
        if type(channel) is not int or channel not in (1,2):raise ValueError('channel1/2 required')
        for values,maximum,label in ((addresses,63,'addresses'),(group_numbers,15,'group_numbers')):
            if (not isinstance(values,list) or not 1<=len(values)<=maximum+1 or
                any(type(v) is not int or not 0<=v<=maximum for v in values) or len(set(values))!=len(values)):
                raise ValueError(f'explicit unique {label}0..{maximum} required')
        record=self.store.get(workbook_id,channel)
        fixtures={f['address']:f for f in record['channel_details']['fixtures']}
        if not set(addresses)<=fixtures.keys():raise ValueError('selected addresses missing from native parameter/group sheets')
        binding=dict(workbook_id=workbook_id,source_sha256=record['summary']['source_sha256'],
            channel=channel,addresses=sorted(addresses),group_numbers=sorted(group_numbers),
            membership_source='native grouping sheet',source_provenance='unverified')
        return fixtures,binding

    @staticmethod
    def changes(fixtures,binding,baseline):
        current=_state(binding['channel'],baseline)
        if set(current)!=set(binding['addresses']):raise ValueError('native membership baseline scope mismatch')
        changes=[]
        for a in binding['addresses']:
            fixture=fixtures[a]
            if current[a][1][0] != (1 if fixture['light_type']=='DT8' else 0):
                raise ValueError('current fixture type differs from native membership source')
            changes.append(dict(address=a,add=[g for g in binding['group_numbers'] if g in fixture['groups']],
                remove=[g for g in binding['group_numbers'] if g not in fixture['groups']]))
        return changes

    def prepare(self,workbook_id,channel,addresses,group_numbers):
        fixtures,binding=self.source(workbook_id,channel,addresses,group_numbers)
        with self.controller._locked():
            g,info=self.service._open()
            try:
                baseline=self.controller._read(g,channel,binding['addresses'])
                changes=self.changes(fixtures,binding,baseline)
                proposal=plan_group_candidates(channel,_members(baseline),changes)
                if not proposal['steps']:
                    return dict(state='already_matches',channel=channel,source_workbook=binding,
                        baseline=baseline,proposal=proposal,device_writes=False,
                        notice='Only selected membership bits checked; other bits preserved; no execution plan created.')
                return self.controller._prepare(info,channel,baseline,changes,source_workbook=binding)
            finally:g.close()

    def validate_plan(self,plan):
        if plan.get('restore_of') or 'source_project' in plan:
            raise ValueError('native membership intent cannot be combined with project intent or execution rollback')
        try:
            source=plan['source_workbook']
            fixtures,binding=self.source(source['workbook_id'],source['channel'],source['addresses'],source['group_numbers'])
            if source!=binding or plan['channel']!=source['channel']:
                raise ValueError('native membership source/scope changed')
            cfg=self.service.config
            if identity(plan['gateway'])!=(cfg['gateway_ip'],cfg['expected_dali_mac'].lower(),cfg['expected_firmware']):
                raise ValueError('native membership configured gateway changed')
            if plan['changes']!=self.changes(fixtures,binding,plan['baseline']):
                raise ValueError('membership intent differs from selected native sheet bits')
        except (KeyError,TypeError,AttributeError) as exc:
            raise ValueError('malformed native membership source') from exc
