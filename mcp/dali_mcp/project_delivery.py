"""Durable device + native-file restoration coordinator.

This is a sequential transaction, never an atomic bus/filesystem commit. All
children are prepared before dispatch. Children retain their exact permissions,
originals, claims and conditional recovery rules. No replay or auto-rollback.
"""
import copy
import json
import os
import threading
import time
import uuid
from pathlib import Path

from .native_cache import canonical
from .native_file import NativeFileController, parse
from .native_floorplan_file import NativeFloorplanFileController
from .project_execution import ProjectRestoreController
from .project_names import ProjectNames
from .projects import digest, identity


class ProjectDeliveryController:
    def __init__(self,service):
        self.service=service
        self.device=ProjectRestoreController(service)
        self.names=NativeFileController(service)
        self.floorplan=NativeFloorplanFileController(service)
        self.runtime_lock=threading.RLock()
        self.active=None;self.event=None;self.worker=None
        self.worker_errors={}

    def _path(self,kind,ident):
        return self.service.state_dir/f'project-delivery-{kind}-{self.device._id(ident)}.json'

    def _read(self,kind,ident):return json.loads(self._path(kind,ident).read_text(encoding='utf-8'))

    def _journal(self,kind,ident,value):
        dest=self._path(kind,ident);temp=dest.with_suffix('.tmp')
        with temp.open('wb') as f:f.write(canonical(value));f.flush();os.fsync(f.fileno())
        NativeFileController._publish_journal(temp,dest)

    @staticmethod
    def _confirmation(p):return f"APPLY DELIVERY {p['plan_id']} {p['digest']}"

    def _local(self,phase):
        if phase['controller']=='names':return self.names
        if phase['controller']=='floorplan':return self.floorplan
        raise ValueError('unknown local restoration controller')

    def _child(self,phase):
        if phase['category']=='device':
            if phase['controller']!='project':raise ValueError('invalid device controller')
            return self.device._load(phase['plan_id'])
        if phase['category']!='local':raise ValueError('unknown restoration phase')
        return self._local(phase)._load(phase['plan_id'])[0]

    @staticmethod
    def _ref(category,controller,p):
        return dict(category=category,controller=controller,plan_id=p['plan_id'],digest=p['digest'])

    def _save_plan(self,source,scope,local_scope,phases,restore_of=None):
        now=time.time()
        p=dict(format='dali-project-delivery-v1',plan_id=uuid.uuid4().hex,
               created_at=now,expires_at=min(self._child(x)['expires_at'] for x in phases),
               source=copy.deepcopy(source),scope=copy.deepcopy(scope),local_scope=copy.deepcopy(local_scope),
               phases=copy.deepcopy(phases),restore_of=restore_of,atomic=False,
               automatic_resume=False,automatic_rollback=False)
        p['digest']=digest(p)
        NativeFileController._write_new(self._path('plan',p['plan_id']),canonical(p))
        self._load(p['plan_id'])
        return dict(p,confirmation=self._confirmation(p))

    def prepare(self,project_id,addresses,fields,group_numbers,scenes,name_selections,mark_scope=None):
        # File validation precedes network capture, not just device dispatch.
        if not isinstance(name_selections,list):raise ValueError('explicit name selections list required')
        if not name_selections and mark_scope is None:raise ValueError('select names and/or a floorplan; device-only uses project restore')
        if mark_scope not in (None,'captured_marks','all_source_marks'):raise ValueError('invalid floorplan scope')
        with self.service.lock:
            source=self.device.review._source(project_id,addresses,fields,group_numbers,scenes)
            kind='names' if mark_scope is None else 'floorplan'
            def prepare_local():
                if mark_scope is None:return ProjectNames(self.service).prepare(project_id,name_selections)
                return self.floorplan.prepare(project_id,mark_scope,name_selections or None)
            local=prepare_local()
            bus=self.device.prepare(project_id,addresses,fields,group_numbers,scenes)
            if bus['source_project']['digest']!=source['digest']:raise ValueError('source changed during delivery preparation')
            local=self._refresh_local_preparation(self._ref('local',kind,local),prepare_local)
            phases=[self._ref('device','project',bus),self._ref('local',kind,local)]
            self._local_preflight(phases[1],gate=False)
            return self._save_plan(dict(project_id=project_id,digest=source['digest'],gateway=source['gateway']),
                bus['scope'],dict(name_selections=name_selections,mark_scope=mark_scope),phases)

    def _load(self,ident,seen=None):
        seen=set() if seen is None else set(seen)
        if ident in seen or len(seen)>8:raise ValueError('invalid delivery ancestry')
        seen.add(ident);p=self._read('plan',ident)
        if p.get('format')!='dali-project-delivery-v1' or p.get('plan_id')!=ident or p.get('digest')!=digest(p):
            raise ValueError('delivery checksum mismatch')
        if any(p.get(k) is not False for k in ('atomic','automatic_resume','automatic_rollback')):
            raise ValueError('invalid delivery guarantees')
        phases=p['phases'];cats=[x['category'] for x in phases]
        if not phases or len(cats)!=len(set(cats)) or any(x not in ('device','local') for x in cats):raise ValueError('invalid phase list')
        children=[self._child(x) for x in phases]
        if any(x['digest']!=c['digest'] for x,c in zip(phases,children)):raise ValueError('delivery child binding changed')
        if p['expires_at']!=min(c['expires_at'] for c in children):raise ValueError('delivery deadline mismatch')
        if p['restore_of'] is None:
            if cats!=['device','local']:raise ValueError('forward delivery phase order mismatch')
            bus,local=children;source=bus['source_project']
            if bus['restore_of'] is not None or bus['scope']!=p['scope']:raise ValueError('device scope mismatch')
            if p['source']!=dict(project_id=source['project_id'],digest=source['digest'],gateway=source['gateway']):
                raise ValueError('delivery source differs from device source')
            spec=p['local_scope'];sels=spec['name_selections'];mark=spec['mark_scope']
            if phases[1]['controller']=='names':
                b=local['source_project_names']
                if mark is not None or local['operation']!='names' or b['selections']!=sels:
                    raise ValueError('local names scope mismatch')
                if b['project_id']!=source['project_id'] or b['project_digest']!=source['digest']:
                    raise ValueError('names source differs from device source')
            else:
                if local['operation']!='floorplan' or mark!=local['mark_scope'] or sels!=local.get('name_selections',[]):
                    raise ValueError('local floorplan/name scope mismatch')
                if local['source_project_id']!=source['project_id'] or local['source_project_digest']!=source['digest']:
                    raise ValueError('floorplan source differs from device source')
        else:
            parent=self._load(p['restore_of'],seen)
            if parent['restore_of'] is not None:raise ValueError('restore only an original delivery')
            run=self._execution(parent)
            if p['source']!=parent['source'] or p['scope']!=parent['scope'] or p['local_scope']!=parent['local_scope']:
                raise ValueError('delivery undo source/scope mismatch')
            if cats!=[c for c in ('local','device') if c in cats]:raise ValueError('undo phase order mismatch')
            attempted={x['category'] for x in run['phases']}
            for phase,child in zip(phases,children):
                old=next(x for x in parent['phases'] if x['category']==phase['category'])
                if phase['category'] not in attempted or phase['controller']!=old['controller']:
                    raise ValueError('undo contains unattempted phase')
                key='restore_of' if phase['category']=='device' else 'source_plan_id'
                if child.get(key)!=old['plan_id']:raise ValueError('undo child differs from original transaction')
        return p

    def _gate(self,p):
        cfg=self.service.config
        if cfg.get('enable_project_delivery') is not True or cfg.get('allowed_project_delivery_digest')!=p['digest']:
            raise PermissionError('delivery requires its exact digest gate')
        for phase in p['phases']:
            child=self._child(phase)
            if phase['category']=='device':self.device._gate(child)
            elif phase['controller']=='floorplan':self.floorplan._check_gates(child)
            elif cfg.get('enable_native_cache_writes') is not True:raise PermissionError('native name gate disabled')

    def _local_preflight(self,phase,gate=True,*,check_expiry=True):
        ctl=self._local(phase);p,before,_=ctl._load(phase['plan_id']);path=ctl._target()
        with ctl._locked(path):
            ctl.guard()
            if ctl._record_path('claim',p['plan_id']).exists():raise ValueError('local child already claimed; inspect without replay')
            if check_expiry and time.time()>p['expires_at']:raise ValueError('local plan expired')
            if ctl._read(path)[0]!=before:raise ValueError('local file changed before delivery')
            if phase['controller']=='names':ctl._check_dependencies(p,path)
            elif p['operation']=='restore':ctl._check_dependencies(p,path)
            else:
                ctl._check_source_current(p);ctl._check_old_asset(path,parse(before),p['original_asset'])
                if Path(p['new_image_path']).exists():raise ValueError('new background already exists')
            if gate:
                if phase['controller']=='floorplan':ctl._check_gates(p)
                elif self.service.config.get('enable_native_cache_writes') is not True:raise PermissionError('native name gate disabled')

    def _refresh_local_preparation(self,phase,prepare):
        # Device capture can take longer than a local plan's start window.
        # Never rebase changed originals: validate the initial backup, source
        # and assets first, then make a new unclaimed plan from identical bytes.
        self._local_preflight(phase,gate=False,check_expiry=False)
        original=self._child(phase)
        fresh=prepare()
        # The new preparation takes its own lock. Recheck initial dependencies
        # after that gap too, so an edited background cannot become a new original.
        self._local_preflight(phase,gate=False,check_expiry=False)
        if 'plan_id' not in fresh or fresh['before_sha256']!=original['before_sha256']:
            raise ValueError('local file changed while completing delivery preparation')
        self._local_preflight(self._ref('local',phase['controller'],fresh),gate=False)
        return fresh

    def _phase_deadline_check(self,p,phase,admission):
        # Only a live, already admitted coordinator may start a late child.
        # The callable is private, never exposed through MCP or serialized as
        # permission. It expires with this execute call; claims still block replay.
        def admitted(child):
            if not admission['active']:raise ValueError('delivery admission is no longer active')
            stamp=admission['at']
            if (child['plan_id']!=phase['plan_id'] or child['digest']!=phase['digest'] or
                not p['created_at']<=stamp<=p['expires_at'] or
                not child['created_at']<=stamp<=child['expires_at']):
                raise ValueError('child was not admitted before its start deadline')
            run=self._execution(p)
            if (run.get('admitted_at')!=stamp or run.get('state')!='executing' or
                run.get('pending')!=phase['category'] or not run['phases'] or
                run['phases'][-1]['child']!=phase or run['phases'][-1]['state']!='outcome_unknown' or
                'result' in run['phases'][-1]):
                raise ValueError('no durable active delivery phase admission')
            return True
        return admitted

    def _preflight(self,p):
        self._gate(p)
        if time.time()>p['expires_at']:raise ValueError('delivery plan expired')
        for phase in p['phases']:
            if phase['category']=='local':self._local_preflight(phase)
            else:
                child=self.device._load(phase['plan_id']);self.device._source_unchanged(child)
                if child['state']!='prepared' or (self.service.state_dir/f"project-restore-claim-{child['plan_id']}.json").exists():
                    raise ValueError('device child already consumed; never replay')
                # Undo writes local data first. Validate its device originals
                # now too, so a known device conflict never changes that file.
                if p['restore_of'] is not None:
                    with self.device._locked():
                        g,info=self.service._open()
                        try:
                            if identity(info)!=identity(child['gateway']):raise ValueError('undo gateway mismatch')
                            rows=self.device._capture(g,child['scope'])
                            if len(rows)!=len(child['originals']) or any(not self.device._same(a,b) for a,b in zip(rows,child['originals'])):
                                raise ValueError('device changed before delivery undo')
                        finally:g.close()

    def _execution(self,p):
        claim=self._read('claim',p['plan_id'])
        if claim!=dict(plan_id=p['plan_id'],digest=p['digest']):raise ValueError('delivery claim mismatch')
        run=self._read('run',p['plan_id'])
        if run.get('plan_id')!=p['plan_id'] or run.get('digest')!=p['digest']:raise ValueError('delivery run mismatch')
        refs=[x['child'] for x in run['phases']]
        if refs!=p['phases'][:len(refs)]:raise ValueError('delivery attempted prefix mismatch')
        if any(x['category']!=x['child']['category'] for x in run['phases']):raise ValueError('delivery phase evidence mismatch')
        return run

    def _final_verify(self,p):
        # Verify both domains again after the last stage, since external clients
        # are not covered by toolkit locks. UI reload remains a separate check.
        evidence={}
        for phase in p['phases']:
            if phase['category']=='local':
                ctl=self._local(phase);child,_,after=ctl._load(phase['plan_id']);path=ctl._target()
                with ctl._locked(path):
                    ctl.guard()
                    if ctl._read(path)[0]!=after:raise ValueError('local final readback differs')
                    ctl._check_dependencies(child,path)
                evidence['local']=dict(after_sha256=child['after_sha256'],readback_verified=True,ui_verified=False)
            else:
                child=self.device._load(phase['plan_id'])
                expected={r['address']:copy.deepcopy(r) for r in child['originals']}
                for s in child['steps']:self.device._apply_expected(expected[s['address']],s,s['after'])
                with self.device._locked():
                    g,info=self.service._open()
                    try:
                        if identity(info)!=identity(child['gateway']):raise ValueError('final gateway mismatch')
                        rows=self.device._capture(g,child['scope'])
                        if any(not self.device._same(r,expected[r['address']]) for r in rows):raise ValueError('device changed after its phase')
                        evidence['device']=dict(readback_verified=True,rows=rows)
                    finally:g.close()
        return evidence

    def execute(self,ident,confirmation,cancelled=lambda:False):
        with self.service.lock:
            p=self._load(ident)
            if self._path('claim',ident).exists():raise ValueError('delivery claimed; inspect without replay')
            if confirmation!=self._confirmation(p):raise ValueError('delivery confirmation mismatch')
            admitted_at=time.time()
            self._preflight(p)
            NativeFileController._write_new(self._path('claim',ident),canonical(dict(plan_id=ident,digest=p['digest'])))
            run=dict(plan_id=ident,digest=p['digest'],started_at=time.time(),admitted_at=admitted_at,state='preflight',phases=[],
                     completed_phases=0,total_phases=len(p['phases']),automatic_resume=False,automatic_rollback=False,
                     ui_verified=False,atomic=False)
            def save():self._journal('run',ident,run)
            admission=dict(at=admitted_at,active=True)
            try:
                save()
                for phase in p['phases']:
                    if cancelled():
                        run['state']='cancelled_partial' if run['phases'] else 'cancelled_before_write';return run
                    self._gate(p)
                    child=self._child(phase)
                    entry=dict(category=phase['category'],child=copy.deepcopy(phase),state='outcome_unknown')
                    run['phases'].append(entry);run.update(state='executing',pending=phase['category']);save()
                    deadline=self._phase_deadline_check(p,phase,admission)
                    if phase['category']=='device':result=self.device.execute(child['plan_id'],self.device._confirmation(child),cancelled,_deadline_check=deadline)
                    else:
                        ctl=self._local(phase);result=ctl.apply(child['plan_id'],ctl._confirmation(child),_deadline_check=deadline)
                    entry['result']=copy.deepcopy(result);entry['state']=result['state'];save()
                    if result['state'] not in ('completed_readback','written_verified','already_matches') or any(k in result for k in ('log_error','journal_error')):
                        run['state']='cancelled_partial' if result['state'].startswith('cancelled_') else 'stopped_partial';return run
                    run['completed_phases']+=1;save()
                run.update(state='final_verification',pending='both_domains');save()
                run['final_readback']=self._final_verify(p)
                run['state']='completed_readback';run.pop('pending',None)
            except (Exception,KeyboardInterrupt) as exc:
                run.update(state='stopped_partial',error=str(exc) or type(exc).__name__)
            finally:
                admission['active']=False
                run['finished_at']=time.time()
                try:save()
                except OSError as exc:run['journal_error']=str(exc)
            return run

    def prepare_restore(self,ident):
        with self.runtime_lock:
            if self.active is not None:raise RuntimeError('delivery worker still active')
        with self.service.lock:
            p=self._load(ident)
            if p['restore_of'] is not None:raise ValueError('restore only an original delivery')
            run=self._execution(p);phases=[];unchanged=[]
            for entry in reversed(run['phases']):
                phase=entry['child'];child=self._child(phase)
                if phase['category']=='device':
                    claim=self.service.state_dir/f"project-restore-claim-{child['plan_id']}.json"
                    if not claim.exists():unchanged.append('device_not_dispatched');continue
                    child_run=self.device._execution(child)
                    if not child_run['steps']:unchanged.append('device_no_attempts');continue
                    undo=self.device.prepare_restore(child['plan_id'])
                else:
                    ctl=self._local(phase)
                    if not ctl._record_path('intent',child['plan_id']).exists():
                        # A file replacement cannot occur without the durable intent.
                        # If it is missing but target drifted, require investigation.
                        _,before,_=ctl._load(child['plan_id'])
                        if ctl._read(ctl._target())[0]!=before:raise ValueError('local drift without replacement intent')
                        unchanged.append('local_not_dispatched');continue
                    undo=ctl.prepare_restore(child['plan_id'])
                if 'plan_id' not in undo:unchanged.append(phase['category']+'_already_original')
                else:phases.append(self._ref(phase['category'],phase['controller'],undo))
            if not phases:return dict(state='already_original',source_plan_id=ident,evidence=unchanged,device_writes=False,application_file_written=False)
            for index,phase in enumerate(phases):
                if phase['category']=='local':
                    ctl=self._local(phase);source_id=self._child(phase)['source_plan_id']
                    fresh=self._refresh_local_preparation(phase,lambda:ctl.prepare_restore(source_id))
                    phases[index]=self._ref('local',phase['controller'],fresh)
            return self._save_plan(p['source'],p['scope'],p['local_scope'],phases,restore_of=ident)

    def get(self,ident):
        p=self._load(ident);claimed=self._path('claim',ident).exists()
        with self.runtime_lock:
            active=self.active==ident
            runtime_error=copy.deepcopy(self.worker_errors.get(ident))
        result=dict(plan=p,claimed=claimed,active_in_this_process=active,automatic_resume=False,children=[])
        if runtime_error is not None:result['worker_error_in_this_process']=runtime_error
        if claimed:
            try:result['execution']=self._execution(p)
            except (ValueError,OSError,KeyError) as exc:result['execution_unavailable']=str(exc)
        else:result['confirmation']=self._confirmation(p)
        for phase in p['phases']:
            try:
                status=self.device.get(phase['plan_id']) if phase['category']=='device' else self._local(phase).get(phase['plan_id'])
                result['children'].append(dict(category=phase['category'],status=status))
            except (ValueError,OSError,KeyError) as exc:result['children'].append(dict(category=phase['category'],unavailable=str(exc)))
        for kind in ('cancel','worker-error'):
            if self._path(kind,ident).exists():result[kind]=self._read(kind,ident)
        return result

    def start(self,ident,confirmation):
        with self.runtime_lock:
            if self.active is not None:raise RuntimeError('a delivery is already active')
            p=self._load(ident);self._gate(p)
            if confirmation!=self._confirmation(p) or time.time()>p['expires_at']:raise ValueError('invalid confirmation or expired delivery')
            if self._path('claim',ident).exists():raise ValueError('delivery already claimed; never replay')
            if not self.service.lock.acquire(blocking=False):raise RuntimeError('gateway busy')
            self.service.lock.release();self.active=ident;event=threading.Event();self.event=event
            def work():
                try:
                    result=self.execute(ident,confirmation,event.is_set)
                    if result.get('journal_error'):raise RuntimeError('delivery result was not durably recorded: '+result['journal_error'])
                except BaseException as exc:
                    failure=dict(plan_id=ident,error=str(exc) or type(exc).__name__)
                    try:self._journal('worker-error',ident,failure)
                    except OSError as log_error:failure['journal_error']=str(log_error)
                    with self.runtime_lock:self.worker_errors[ident]=failure
                finally:
                    with self.runtime_lock:self.active=None;self.event=None
            self.worker=threading.Thread(target=work,daemon=True);self.worker.start()
        return self.get(ident)

    def cancel(self,ident):
        self.device._id(ident)
        with self.runtime_lock:
            if self.active!=ident:raise ValueError('delivery is not active in this process')
            self._journal('cancel',ident,dict(plan_id=ident,requested_at=time.time()));self.event.set()
        return self.get(ident)
