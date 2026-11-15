"""Source-bound combined parameter/scene/membership transactions.

Internal controller; no automatic rollback, replay, or live-light commands.
"""
import copy
import json
import os
import re
import time
import threading
import uuid
from contextlib import contextmanager
from .project_restore import ProjectRestoreReviewer
from .projects import digest, identity, validate_document
from .parameter_execution import ExperimentalParameterController, equivalent, semantic, apply_expected, send_candidate_once
from .parameter_control import LampParameterController
from .parameter_batch import ParameterBatchController
from .parameter_planning import original_values, plan_parameter_candidates, encode_parameter_candidate, valid_limits
from .group_execution import ExperimentalGroupController, send_group_once
from .group_planning import plan_group_candidates


class ProjectRestoreController:
    def __init__(self,service):
        self.service=service;self.review=ProjectRestoreReviewer(service)
        self.parameters=ExperimentalParameterController(service);self.groups=ExperimentalGroupController(service)
        self.runtime_lock=threading.RLock();self.active=None;self.event=None;self.worker=None

    @contextmanager
    def _locked(self):
        # Fixed order across combined controllers, plus shared in-process bus lock.
        # External applications/other state directories do not participate.
        with self.parameters._locked():
            with self.groups._locked():yield

    @staticmethod
    def _id(ident):
        if type(ident) is not str or not re.fullmatch('[0-9a-f]{32}',ident):raise ValueError('invalid project restore ID')
        return ident

    def _read(self,kind,ident):
        return json.loads((self.service.state_dir/f'{kind}-{self._id(ident)}.json').read_text(encoding='utf-8'))

    @staticmethod
    def _digest(plan):
        return digest({k:v for k,v in plan.items() if k not in ('state','execution_id','confirmation')})

    @staticmethod
    def _confirmation(p):return f"APPLY PROJECT {p['plan_id']} {p['digest'][:12]}"

    @staticmethod
    def _intent(source,scope):
        rows={r['address']:r for r in source['rows']};result=[]
        for a in scope['addresses']:
            row=rows[a];values=original_values(row['parameters'])
            result.append(dict(address=a,parameters=[dict(field=f,values=values[f]) for f in scope['fields']],
                scenes=[ProjectRestoreReviewer._scene(s,scope['channel'],a,s['scene']) for s in row['scenes'] if s['scene'] in scope['scenes']],
                groups=[dict(group=g,member=g in row['parameters']['groups']['value']['groups']) for g in scope['group_numbers']]))
        return result

    @staticmethod
    def _semantic(row):
        return dict(parameters=semantic(row),actual={k:row['actual'][k] for k in ('level_raw','kelvin_raw')},
            scenes={str(s['scene']):ProjectRestoreReviewer._scene(s,row['channel'],row['address'],s['scene']) for s in row['scenes']})

    @classmethod
    def _same(cls,a,b):
        left,right=cls._semantic(a),cls._semantic(b)
        return left['actual']==right['actual'] and equivalent(left['parameters'],right['parameters']) and equivalent(left['scenes'],right['scenes'])

    def _capture(self,g,scope,save_pending=lambda a,s:None):
        rows=[]
        for a in scope['addresses']:
            save_pending(a,None);row=self.parameters._snapshot(g,scope['channel'],a)
            if not 0<=row['actual']['level_raw']<=254 or not 1000<=row['actual']['kelvin_raw']<=10000:
                raise ValueError('known live output required')
            row['scenes']=[]
            for sn in scope['scenes']:
                save_pending(a,sn);value=g.read_scene(scope['channel'],a,sn,1)
                self.review._scene(value,scope['channel'],a,sn);row['scenes'].append(value)
            rows.append(row)
        return rows

    @staticmethod
    def _build(channel,originals,intent,references=None):
        if [r['address'] for r in originals]!=[r['address'] for r in intent]:raise ValueError('intent/original scope mismatch')
        phases={k:[] for k in ('parameter','scene','group')}
        for original,target in zip(originals,intent):
            a=original['address'];values=original_values(original['parameters']);projected=copy.deepcopy(values)
            changes=target['parameters']
            if changes:
                for change in changes:LampParameterController._validate_scope(original,[change])
                proposal=plan_parameter_candidates(channel,a,original,changes,
                    reference_ct_limits=None if references is None else references[str(a)])
                # Paired power/failure CT must fit the range at its own step,
                # not merely the final projected range.
                remaining=list(proposal['steps']);ordered=[];transient=copy.deepcopy(values)
                while remaining:
                    for candidate in remaining:
                        next_values=copy.deepcopy(transient);next_values[candidate['field']]=candidate['after']
                        k=candidate['after'].get('kelvin_raw')
                        if valid_limits(next_values) and (candidate['field'] not in ('power_on','system_failure') or k==65535 or
                            transient['configured_min_ct']['kelvin_raw']<=k<=transient['configured_max_ct']['kelvin_raw']):break
                    else:raise ValueError('no valid parameter/paired-color transition order')
                    ordered.append(candidate);remaining.remove(candidate);transient=next_values
                for candidate in ordered:
                    phases['parameter'].append(dict(category='parameter',address=a,field=candidate['field'],before=candidate['before'],after=candidate['after']))
                for change in changes:projected[change['field']].update(change['values'])
            if not valid_limits(projected):raise ValueError('projected limits invalid')
            scenes={s['scene']:s for s in original['scenes']}
            for target_scene in target['scenes']:
                sn=target_scene['scene'];after=ProjectRestoreReviewer._scene(target_scene,channel,a,sn)
                before=ProjectRestoreReviewer._scene(scenes[sn],channel,a,sn)
                if not ProjectRestoreReviewer._within(after,projected):raise ValueError('scene outside projected parameter limits')
                if not equivalent(before,after):
                    if not ProjectRestoreReviewer._within(before,values):raise ValueError('original scene outside original limits cannot be conditionally restored')
                    phases['scene'].append(dict(category='scene',address=a,scene=sn,before=before,after=after))
            if target['groups']:
                old=original['parameters']['groups']['value']
                proposal=plan_group_candidates(channel,[dict(channel=channel,address=a,**old)],
                    [dict(address=a,add=[x['group'] for x in target['groups'] if x['member']],remove=[x['group'] for x in target['groups'] if not x['member']])])
                for s in proposal['steps']:
                    phases['group'].append(dict(category='group',address=a,group=s['group'],before=not s['member'],after=s['member']))
        return [s for phase in phases.values() for s in phase]

    @staticmethod
    def _source(p):
        keys=[k for k in ('source_project','source_workbook') if k in p]
        if len(keys)!=1:raise ValueError('exactly one combined restoration source required')
        return keys[0],p[keys[0]]

    def _save_plan(self,source,scope,originals,intent,restore_of=None,references=None,source_key='source_project'):
        if source_key not in ('source_project','source_workbook'):raise ValueError('unsupported restoration source')
        p=dict(plan_id=uuid.uuid4().hex,state='prepared',created_at=time.time(),expires_at=time.time()+600,
            gateway=copy.deepcopy(source['gateway']),scope=copy.deepcopy(scope),
            originals=copy.deepcopy(originals),intent=copy.deepcopy(intent),restore_of=restore_of,references=references)
        p[source_key]=copy.deepcopy(source)
        p['steps']=self._build(scope['channel'],originals,intent,references);p['digest']=self._digest(p)
        self.service._save('project-restore-plan',p,p['plan_id'])
        return dict(p,confirmation=self._confirmation(p))

    def prepare(self,project_id,addresses,fields,group_numbers,scenes):
        source=self.review._source(project_id,addresses,fields,group_numbers,scenes)
        scope=dict(channel=source['scope']['channel'],addresses=sorted(addresses),fields=list(fields),group_numbers=sorted(group_numbers),scenes=sorted(scenes))
        intent=self._intent(source,scope)
        with self._locked():
            g,info=self.service._open()
            try:
                if identity(info)!=identity(source['gateway']):raise ValueError('project gateway mismatch')
                originals=self._capture(g,scope)
                if self.review._source(project_id,addresses,fields,group_numbers,scenes)['digest']!=source['digest']:
                    raise ValueError('project changed during preparation')
                return self._save_plan(source,scope,originals,intent)
            finally:g.close()

    def prepare_workbook(self,spec):
        from .native_workbook_restore import WorkbookRestoreSource
        adapter=WorkbookRestoreSource(self.service);source,scope,intent=adapter.capture(spec)
        if not any(r['parameters'] or r['scenes'] or r['groups'] for r in intent):
            return dict(state='no_targets_in_source',scope=scope,source_review=source['review'],device_writes=False,network_traffic=False)
        with self._locked():
            g,info=self.service._open()
            try:
                if identity(info)!=identity(source['gateway']):raise ValueError('native restoration gateway mismatch')
                originals=self._capture(g,scope);adapter.unchanged(source,scope)
                return self._save_plan(source,scope,originals,intent,source_key='source_workbook')
            finally:g.close()

    def _load(self,ident,seen=None):
        seen=set() if seen is None else set(seen)
        if ident in seen or len(seen)>=16:raise ValueError('invalid restore ancestry')
        seen.add(ident);p=self._read('project-restore-plan',ident)
        if p['plan_id']!=ident or p['digest']!=self._digest(p):raise ValueError('project restore checksum mismatch')
        source_key,source=self._source(p)
        if source_key=='source_project':
            validate_document(source);source_intent=self._intent(source,p['scope'])
        else:
            from .native_workbook_restore import WorkbookRestoreSource
            source_intent=WorkbookRestoreSource(self.service).validate(source,p['scope'])
        if identity(p['gateway'])!=identity(source['gateway']):raise ValueError('source gateway binding mismatch')
        if p['restore_of'] is None:
            if p['references'] is not None or p['intent']!=source_intent:raise ValueError('intent differs from source')
        else:
            parent=self._load(p['restore_of'],seen);run=self._execution(parent)
            intent,references=self._undo(parent,run,p['originals'])
            if p['scope']!=parent['scope'] or self._source(p)!=self._source(parent) or p['intent']!=intent or p['references']!=references:
                raise ValueError('recovery differs from attempted original targets')
        if p['steps']!=self._build(p['scope']['channel'],p['originals'],p['intent'],p['references']):raise ValueError('steps differ from validated intent')
        return p

    def _source_unchanged(self,p):
        if p['restore_of'] is None:
            source_key,src=self._source(p);scope=p['scope']
            if source_key=='source_project':
                current=self.review._source(src['project_id'],scope['addresses'],scope['fields'],scope['group_numbers'],scope['scenes'])
                if current['digest']!=src['digest']:raise ValueError('source project changed')
            else:
                from .native_workbook_restore import WorkbookRestoreSource
                WorkbookRestoreSource(self.service).unchanged(src,scope)
        cfg=self.service.config
        if identity(p['gateway'])!=(cfg['gateway_ip'],cfg['expected_dali_mac'].lower(),cfg['expected_firmware']):raise ValueError('configured gateway changed')

    def _gate(self,p):
        cfg=self.service.config;c=p['scope']['channel']
        if cfg.get('enable_project_restore') is not True or cfg.get('allowed_project_restore_digest')!=p['digest']:
            raise PermissionError('combined restoration requires exact plan digest gate')
        parameters=[s for s in p['steps'] if s['category']=='parameter']
        if parameters:
            entries=[dict(address=a,proposal=dict(steps=[dict(field=s['field'],after=s['after']) for s in parameters if s['address']==a])) for a in sorted({s['address'] for s in parameters})]
            ParameterBatchController(self.service)._check_gate(dict(channel=c,entries=entries))
        groups=[dict(address=s['address'],group=s['group'],member=s['after']) for s in p['steps'] if s['category']=='group']
        if groups:self.groups._check_gate(dict(channel=c,proposal=dict(steps=groups)))
        scenes=[s['after'] for s in p['steps'] if s['category']=='scene']
        if scenes:
            if cfg.get('enable_experimental_writes') is not True or 'allowed_write_targets' not in cfg:raise PermissionError('explicit scene write gate required')
            self.service._check_scene_scope(scenes)

    @staticmethod
    def _apply_expected(row,step,value):
        category=step['category']
        if category=='parameter':
            params=apply_expected(semantic(row),step['field'],value)
            for field,v in params.items():row['parameters'][field]['value']=v
        elif category=='scene':
            row['scenes']=[copy.deepcopy(value) if s['scene']==step['scene'] else s for s in row['scenes']]
        else:
            group=step['group'];v=row['parameters']['groups']['value'];mask=v['group_mask']
            mask=mask | (1<<group) if value else mask & ~(1<<group)
            v.update(group_mask=mask,groups=[g for g in range(16) if mask&(1<<g)])

    @staticmethod
    def _value(row,step):
        if step['category']=='parameter':return original_values(row['parameters'])[step['field']]
        if step['category']=='scene':return next(s for s in row['scenes'] if s['scene']==step['scene'])
        return step['group'] in row['parameters']['groups']['value']['groups']

    def _step_snapshot(self,g,channel,step):
        a=step['address'];category=step['category']
        if category=='parameter':return self.parameters._snapshot(g,channel,a)
        if category=='scene':
            value=g.read_scene(channel,a,step['scene'],1)
            self.review._scene(value,channel,a,step['scene']);result=dict(scene=value)
        else:result=dict(groups=g.read_lamp_parameter(channel,a,'groups',1)['value'])
        result['actual']=g.read_actual(channel,a,True)
        if result['actual']['light_type_code']!=1:raise ValueError('live type changed')
        return result

    def _step_matches(self,observed,expected,step):
        if step['category']=='parameter':return equivalent(semantic(observed),semantic(expected))
        if step['category']=='scene':return equivalent(observed['scene'],self._value(expected,step))
        return equivalent(observed['groups'],expected['parameters']['groups']['value'])

    def execute(self,ident,confirmation,cancelled=lambda:False,*,_deadline_check=None):
        with self._locked():
            p=self._load(ident);self._source_unchanged(p);self._gate(p)
            if confirmation!=self._confirmation(p) or p['state']!='prepared':raise ValueError('invalid confirmation, consumed or expired plan')
            if time.time()>p['expires_at'] and (_deadline_check is None or not _deadline_check(p)):
                raise ValueError('invalid confirmation, consumed or expired plan')
            run_id=uuid.uuid4().hex
            with (self.service.state_dir/f'project-restore-claim-{ident}.json').open('x',encoding='utf-8') as f:
                json.dump(dict(plan_id=ident,digest=p['digest'],execution_id=run_id),f);f.flush();os.fsync(f.fileno())
            run=dict(plan_id=ident,execution_id=run_id,state='preflight',started_at=time.time(),steps=[],completed_steps=0,
                total_steps=len(p['steps']),automatic_resume=False,automatic_rollback=False,live_control_commands_sent=False)
            def save():self.service._save('project-restore-run',run,run_id)
            def pending(a,s):run['pending']=dict(address=a,scene=s,phase=run['state']);save()
            g=None
            try:
                p.update(state='consumed',execution_id=run_id);self.service._save('project-restore-plan',p,ident);save()
                g,info=self.service._open()
                if identity(info)!=identity(p['gateway']):raise ValueError('gateway changed')
                current=self._capture(g,p['scope'],pending);run['fresh_before']=current;save()
                if len(current)!=len(p['originals']) or any(not self._same(a,b) for a,b in zip(current,p['originals'])):raise ValueError('current configuration/live/scene differs from plan')
                self._source_unchanged(p);self._gate(p)
                expected={r['address']:copy.deepcopy(r) for r in current};c=p['scope']['channel']
                if any(s['category']=='parameter' for s in p['steps']):
                    run['invalidation']=self.service.invalidate_capabilities(c,sorted({s['address'] for s in p['steps'] if s['category']=='parameter'}),f'project restoration {run_id}');save()
                last_scope=None
                for candidate in p['steps']:
                    if cancelled():run['state']='cancelled_partial' if run['steps'] else 'cancelled_before_write';return run
                    a=candidate['address'];run['state']='checking';pending(a,candidate.get('scene'))
                    # Full checks at each category/lamp boundary. Within a scene
                    # phase, query the affected scene/live, not all 16 scenes
                    # repeatedly. Final full-scope read catches unrelated drift.
                    single=dict(p['scope'],addresses=[a]);phase=(candidate['category'],a)
                    if phase!=last_scope:
                        before=self._capture(g,single)[0]
                        if not self._same(before,expected[a]):raise ValueError('target changed at category boundary')
                        last_scope=phase
                    before=self._step_snapshot(g,c,candidate)
                    if not self._step_matches(before,expected[a],candidate) or any(before['actual'][k]!=expected[a]['actual'][k] for k in ('level_raw','kelvin_raw')):
                        raise ValueError('target changed during restoration')
                    step=dict(candidate=candidate,state='outcome_unknown');run['steps'].append(step);run['state']='writing';save()
                    error=None
                    try:
                        if candidate['category']=='parameter':
                            wire=encode_parameter_candidate(c,a,candidate['field'],candidate['after'])
                            step['receipt']=send_candidate_once(g,wire)
                        elif candidate['category']=='scene':
                            g.write_scene(candidate['after']);step['receipt']=dict(received=True,status_interpreted=False)
                        else:step['receipt']=send_group_once(g,c,a,candidate['group'],candidate['after'])
                    except (Exception,KeyboardInterrupt) as exc:
                        error=str(exc) or type(exc).__name__;step['dispatch_error']=error
                        g.close();g=None;g,info=self.service._open()
                        if identity(info)!=identity(p['gateway']):raise ValueError('gateway changed after uncertain write')
                    after=self._step_snapshot(g,c,candidate);step['readback']=after
                    self._apply_expected(expected[a],candidate,candidate['after'])
                    if not self._step_matches(after,expected[a],candidate):
                        step['state']='verification_failed';run['state']='stopped_verification_failed';return run
                    step['state']='verified_receipt_unknown' if error else 'verified_readback'
                    if any(after['actual'][k]!=expected[a]['actual'][k] for k in ('level_raw','kelvin_raw')):
                        step['live_output_changed']=True;run['state']='stopped_live_output_changed';return run
                    if error:run['state']='stopped_receipt_unknown';return run
                    run['completed_steps']+=1;save()
                run['state']='final_verification';run['fresh_after']=self._capture(g,p['scope'],pending);save()
                if any(not self._same(r,expected[r['address']]) for r in run['fresh_after']):raise ValueError('final full-scope verification failed')
                run['state']='completed_readback';run.pop('pending',None)
            except (Exception,KeyboardInterrupt) as exc:
                run.update(state='stopped_partial' if run['steps'] else 'preflight_failed',error=str(exc) or type(exc).__name__)
            finally:
                if g is not None:g.close()
                run['finished_at']=time.time()
                try:save()
                except Exception as exc:run['log_error']=str(exc)
            return run

    def _execution(self,p):
        if p['state']!='consumed':raise ValueError('plan has no execution')
        run=self._read('project-restore-run',p['execution_id']);claim=self._read('project-restore-claim',p['plan_id'])
        if claim!=dict(plan_id=p['plan_id'],digest=p['digest'],execution_id=p['execution_id']):raise ValueError('claim mismatch')
        if run['plan_id']!=p['plan_id'] or run['execution_id']!=p['execution_id']:raise ValueError('run mismatch')
        attempted=[s['candidate'] for s in run['steps']]
        if len(attempted)>len(p['steps']) or attempted!=p['steps'][:len(attempted)]:raise ValueError('invalid attempted prefix')
        return run

    def _undo(self,p,run,current):
        if not run['steps']:raise ValueError('no attempted changes to recover')
        expected={r['address']:copy.deepcopy(r) for r in p['originals']}
        rows={r['address']:r for r in current}
        if list(rows)!=[r['address'] for r in p['originals']]:raise ValueError('recovery current scope mismatch')
        intent={a:dict(address=a,parameters=[],scenes=[],groups=[]) for a in rows}
        for entry in run['steps']:
            s=entry['candidate'];a=s['address'];value=self._value(rows[a],s)
            if equivalent(value,s['before']):continue
            if not equivalent(value,s['after']):raise ValueError('attempted target has third-party value; inspect before recovery')
            self._apply_expected(expected[a],s,value)
            if s['category']=='parameter':intent[a]['parameters'].append(dict(field=s['field'],values=s['before']))
            elif s['category']=='scene':intent[a]['scenes'].append(s['before'])
            else:intent[a]['groups'].append(dict(group=s['group'],member=s['before']))
        if any(not self._same(rows[a],expected[a]) for a in rows):raise ValueError('unattempted configuration or live output changed; inspect before recovery')
        # Recovery references must come from original pre-write evidence.
        refs={str(r['address']):[original_values(r['parameters'])[f]['kelvin_raw'] for f in ('configured_min_ct','configured_max_ct')] for r in p['originals']}
        return list(intent.values()),refs

    def prepare_restore(self,ident):
        with self._locked():
            p=self._load(ident);run=self._execution(p);g,info=self.service._open()
            try:
                if identity(info)!=identity(p['gateway']):raise ValueError('restoration gateway changed')
                current=self._capture(g,p['scope']);intent,refs=self._undo(p,run,current)
                if not any(r['parameters'] or r['scenes'] or r['groups'] for r in intent):return dict(state='already_original',source_plan_id=ident,device_writes=False)
                source_key,source=self._source(p)
                return self._save_plan(source,p['scope'],current,intent,ident,refs,source_key)
            finally:g.close()

    def get(self,ident):
        p=self._load(ident);claim=(self.service.state_dir/f'project-restore-claim-{ident}.json').exists()
        result=dict(plan=p,claimed=claim,automatic_resume=False)
        with self.runtime_lock:result['active_in_this_process']=self.active==ident
        if claim:
            try:result['execution']=self._execution(p)
            except (ValueError,OSError,KeyError) as exc:result['execution_unavailable']=str(exc)
        elif p['state']=='prepared':result['confirmation']=self._confirmation(p)
        for kind,key in [('project-restore-cancel','cancellation'),('project-restore-worker-error','worker_error')]:
            if (self.service.state_dir/f'{kind}-{ident}.json').exists():result[key]=self._read(kind,ident)
        return result

    def start(self,ident,confirmation):
        with self.runtime_lock:
            if self.active is not None:raise RuntimeError('a project restoration is already active')
            p=self._load(ident);self._source_unchanged(p);self._gate(p)
            if confirmation!=self._confirmation(p) or p['state']!='prepared' or time.time()>p['expires_at']:raise ValueError('invalid confirmation, consumed or expired plan')
            if (self.service.state_dir/f'project-restore-claim-{ident}.json').exists():raise ValueError('plan already claimed; inspect, never replay')
            if not self.service.lock.acquire(blocking=False):raise RuntimeError('gateway busy')
            self.service.lock.release();self.active=ident;event=threading.Event();self.event=event
            def work():
                try:self.execute(ident,confirmation,event.is_set)
                except BaseException as exc:self.service._save('project-restore-worker-error',dict(plan_id=ident,error=str(exc) or type(exc).__name__),ident)
                finally:
                    with self.runtime_lock:self.active=None;self.event=None
            self.worker=threading.Thread(target=work,daemon=True);self.worker.start()
        return self.get(ident)

    def cancel(self,ident):
        self._id(ident)
        with self.runtime_lock:
            if self.active!=ident:raise ValueError('restoration is not active in this process; inspect saved state')
            self.service._save('project-restore-cancel',dict(plan_id=ident,requested_at=time.time()),ident);self.event.set()
        return self.get(ident)
