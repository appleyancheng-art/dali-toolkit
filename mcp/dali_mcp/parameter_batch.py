"""Serial multi-lamp parameter transactions with persistent, non-replayable claims."""
import copy
import json
import os
import re
import threading
import time
import uuid
from .parameter_execution import ExperimentalParameterController, equivalent, semantic, apply_expected, send_candidate_once
from .parameter_control import LampParameterController
from .parameter_review import ParameterReviewer
from .parameter_planning import original_values, plan_parameter_candidates, ParameterPatch, validate_values
from .projects import identity, scope_values


class ParameterBatchController(ExperimentalParameterController):
    def __init__(self, service):
        super().__init__(service)
        self.runtime_lock=threading.RLock();self.active=None;self.event=None;self.worker=None

    @staticmethod
    def _id(ident):
        if type(ident) is not str or not re.fullmatch('[0-9a-f]{32}',ident):raise ValueError('invalid parameter batch ID')
        return ident

    def _read(self,kind,ident):
        return json.loads((self.service.state_dir/f'{kind}-{self._id(ident)}.json').read_text(encoding='utf-8'))

    @staticmethod
    def _targets(channel,targets):
        result=ParameterReviewer._targets(channel,targets)
        for target in result:
            for patch in target['changes']:
                f,v=patch['field'],patch['values']
                if f in LampParameterController.PAIRED:LampParameterController._paired_values(f,v)
                else:LampParameterController._values(f,v[LampParameterController._key(f)])
        return result

    def _source_targets(self,spec):
        kind=spec['kind'];addresses=spec['addresses'];channel=spec['channel']
        scope_values(channel,addresses,[0])
        rows=[];bindings=[];reviews=[];blank=[]
        if kind=='project':
            if set(spec)!={'kind','project_id','channel','addresses','fields'}:raise ValueError('invalid project batch source')
            controller=LampParameterController(self.service)
            for address in addresses:
                document,binding,changes=controller._project_changes(spec['project_id'],address,spec['fields'])
                if document['scope']['channel']!=channel:raise ValueError('project channel mismatch')
                rows.append(dict(address=address,changes=changes));bindings.append(binding)
        elif kind=='workbook':
            if set(spec)!={'kind','workbook_id','channel','addresses','fields','brightness_policy'}:raise ValueError('invalid workbook batch source')
            from .native_workbook_parameters import NativeWorkbookParameters
            controller=NativeWorkbookParameters(self.service)
            for address in addresses:
                changes,binding,review=controller.source(spec['workbook_id'],channel,address,spec['fields'],spec['brightness_policy'])
                bindings.append(binding);reviews.append(dict(address=address,**review))
                if changes:rows.append(dict(address=address,changes=changes))
                else:blank.append(address)
        else:raise ValueError('unsupported parameter batch source')
        return rows,dict(spec=copy.deepcopy(spec),bindings=bindings,reviews=reviews,blank_addresses=blank)

    def prepare_source(self,spec):
        targets,source=self._source_targets(spec)
        if not targets:return dict(state='no_targets_in_source',source=source,device_writes=False,network_traffic=False)
        return self.prepare(spec['channel'],targets,source=source)

    def _entry(self,channel,target,snapshot,limits=None):
        for change in target['changes']:LampParameterController._validate_scope(snapshot,[change])
        proposal=plan_parameter_candidates(channel,target['address'],snapshot,target['changes'],reference_ct_limits=limits)
        return dict(address=target['address'],changes=copy.deepcopy(target['changes']),original=snapshot,proposal=proposal)

    def _publish(self,info,channel,entries,source=None,restore_of=None):
        plan=dict(plan_id=uuid.uuid4().hex,channel=channel,gateway=info,entries=entries,source=source,
                  restore_of=restore_of,state='prepared',created_at=time.time(),expires_at=time.time()+600)
        plan['digest']=self._digest(plan);self.service._save('parameter-batch-plan',plan,plan['plan_id'])
        return dict(plan,confirmation=self._confirmation(plan),total_steps=len(self._flat(plan)))

    @staticmethod
    def _confirmation(plan):return f"APPLY PARAMETER BATCH {plan['plan_id']} {plan['digest'][:12]}"

    @staticmethod
    def _flat(plan):
        return [dict(address=e['address'],candidate=s) for e in plan['entries'] for s in e['proposal']['steps']]

    def _configured(self,info):
        c=self.service.config
        if identity(info)!=(c['gateway_ip'],c['expected_dali_mac'].lower(),c['expected_firmware']):
            raise ValueError('parameter batch gateway/configuration changed')

    def prepare(self,channel,targets,source=None):
        targets=self._targets(channel,targets)
        if source is not None:
            expected,binding=self._source_targets(source['spec'])
            if targets!=expected or binding!=source:raise ValueError('parameter batch source changed')
        with self._locked():
            g,info=self.service._open()
            try:
                self._configured(info)
                entries=[self._entry(channel,t,self._snapshot(g,channel,t['address'])) for t in targets]
                return self._publish(info,channel,entries,source)
            finally:g.close()

    def _load(self,ident,_seen=None):
        seen=set() if _seen is None else set(_seen)
        if ident in seen or len(seen)>=16:raise ValueError('invalid parameter batch restore ancestry')
        seen.add(ident);p=self._read('parameter-batch-plan',ident)
        if p.get('plan_id')!=ident or p.get('digest')!=self._digest(p):raise ValueError('parameter batch checksum mismatch')
        self._configured(p['gateway'])
        targets=self._targets(p['channel'],[dict(address=e['address'],changes=e['changes']) for e in p['entries']])
        parent=None;parent_entries={}
        if p['restore_of'] is not None:
            if p['source'] is not None:raise ValueError('restore must refer to execution, not a new source')
            parent=self._load(p['restore_of'],seen);run=self._execution(parent)
            if identity(parent['gateway'])!=identity(p['gateway']) or parent['channel']!=p['channel']:
                raise ValueError('restore gateway/scope changed')
            parent_entries={e['address']:e for e in parent['entries']}
            if not {t['address'] for t in targets}<=parent_entries.keys():raise ValueError('restore address exceeds source')
        elif p['source'] is not None:
            expected,binding=self._source_targets(p['source']['spec'])
            if p['source']!=binding or targets!=expected:raise ValueError('batch source/targets changed')
        for t,e in zip(targets,p['entries']):
            limits=None
            if parent is not None:
                before=parent_entries[e['address']]
                changes=self._undo_changes(before,e['original'],run)
                if changes!=t['changes']:raise ValueError('restore targets differ from attempted original fields')
                values=original_values(before['original']['parameters'])
                limits=(values['configured_min_ct']['kelvin_raw'],values['configured_max_ct']['kelvin_raw'])
            if self._entry(p['channel'],t,e['original'],limits)!=e:raise ValueError('batch proposal does not match fields')
        return p

    def _check_gate(self,plan):
        cfg=self.service.config
        if cfg.get('enable_experimental_parameter_writes') is not True:raise PermissionError('parameter writes disabled')
        allowed=cfg.get('allowed_parameter_batch_targets')
        if not isinstance(allowed,list) or not 1<=len(allowed)<=64:raise PermissionError('explicit batch target allowlist required')
        scopes={}
        for item in allowed:
            if not isinstance(item,dict) or set(item)!={'channel','address','values'}:raise PermissionError('invalid batch allowlist entry')
            c,a=item['channel'],item['address']
            if type(c) is not int or c not in (1,2) or type(a) is not int or not 0<=a<=63 or (c,a) in scopes:
                raise PermissionError('invalid/duplicate batch allowlist address')
            values=item['values']
            if not isinstance(values,list) or len(values)>9:raise PermissionError('invalid batch values')
            fields=[]
            for value in values:
                try:
                    patch=ParameterPatch.model_validate(value);validate_values(patch.field,patch.values)
                except ValueError as exc:raise PermissionError('invalid batch field/value') from exc
                fields.append(patch.field)
            if len(set(fields))!=len(fields):raise PermissionError('duplicate allowed parameter field')
            scopes[c,a]=values
        for e in plan['entries']:
            permitted=scopes.get((plan['channel'],e['address']))
            if permitted is None or any(dict(field=s['field'],values=s['after']) not in permitted for s in e['proposal']['steps']):
                raise PermissionError('batch parameter target outside exact allowlist')

    @staticmethod
    def _same(current,original):
        return equivalent(semantic(current),semantic(original)) and all(current['actual'][k]==original['actual'][k] for k in ('level_raw','kelvin_raw'))

    def execute(self,ident,confirmation,cancelled=lambda:False):
        with self._locked():
            p=self._load(ident);self._check_gate(p)
            if confirmation!=self._confirmation(p):raise ValueError('batch confirmation mismatch')
            if p['state']!='prepared' or time.time()>p['expires_at']:raise ValueError('batch consumed or expired')
            run_id=uuid.uuid4().hex
            with (self.service.state_dir/f'parameter-batch-claim-{ident}.json').open('x',encoding='utf-8') as f:
                json.dump(dict(plan_id=ident,digest=p['digest'],execution_id=run_id),f);f.flush();os.fsync(f.fileno())
            run=dict(plan_id=ident,execution_id=run_id,state='preflight',steps=[],fresh_before=[],
                     total_steps=len(self._flat(p)),completed_steps=0,started_at=time.time(),
                     automatic_resume=False,automatic_rollback=False,live_control_commands_sent=False)
            def save():self.service._save('parameter-batch-run',run,run_id)
            g=None
            try:
                p.update(state='consumed',execution_id=run_id);self.service._save('parameter-batch-plan',p,ident);save()
                g,info=self.service._open()
                if identity(info)!=identity(p['gateway']):raise ValueError('batch gateway changed')
                # All targets rechecked and originals persisted before any write.
                for e in p['entries']:
                    if cancelled():run['state']='cancelled_before_write';return run
                    run['pending']=dict(address=e['address'],phase='preflight');save()
                    current=self._snapshot(g,p['channel'],e['address']);run['fresh_before'].append(current);save()
                    if not self._same(current,e['original']):raise ValueError('batch target changed since preparation')
                self._load(ident);self._check_gate(p)  # Recheck source/config after potentially long preflight.
                for e in p['entries']:
                    if cancelled():run['state']='cancelled_partial' if run['steps'] else 'cancelled_before_write';return run
                    # Long batches recheck each lamp immediately before its first write.
                    run['pending']=dict(address=e['address'],phase='checking');save()
                    current=self._snapshot(g,p['channel'],e['address'])
                    if not self._same(current,e['original']):raise ValueError('batch target changed during execution')
                    expected=semantic(current)
                    if e['proposal']['steps']:
                        run.setdefault('capability_invalidations',[]).append(self.service.invalidate_capabilities(p['channel'],[e['address']],f'parameter batch {run_id}'));save()
                    for candidate in e['proposal']['steps']:
                        if cancelled():run['state']='cancelled_partial' if run['steps'] else 'cancelled_before_write';return run
                        step=dict(address=e['address'],candidate=candidate,state='outcome_unknown')
                        run['steps'].append(step);run['state']='writing';run['pending']=dict(address=e['address'],field=candidate['field']);save()
                        error=None
                        try:step['receipt']=send_candidate_once(g,candidate)
                        except (Exception,KeyboardInterrupt) as exc:
                            error=str(exc) or type(exc).__name__;step['dispatch_error']=error
                            g.close();g=None;g,info=self.service._open()
                            if identity(info)!=identity(p['gateway']):raise ValueError('gateway changed after uncertain write')
                        after=self._snapshot(g,p['channel'],e['address']);step['readback']=after
                        expected=apply_expected(expected,candidate['field'],candidate['after'])
                        if not equivalent(semantic(after),expected):
                            step['state']='verification_failed';run['state']='stopped_verification_failed';return run
                        step['state']='verified_receipt_unknown' if error else 'verified_readback'
                        if error:run['state']='stopped_receipt_unknown';return run
                        if any(after['actual'][k]!=e['original']['actual'][k] for k in ('level_raw','kelvin_raw')):
                            step['live_output_changed']=True;run['state']='stopped_live_output_changed';return run
                        run['completed_steps']+=1;current=after;save()
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
        if p['state']!='consumed':raise ValueError('batch has no consumed execution')
        claim=self._read('parameter-batch-claim',p['plan_id']);run=self._read('parameter-batch-run',p['execution_id'])
        if claim!=dict(plan_id=p['plan_id'],digest=p['digest'],execution_id=p['execution_id']):raise ValueError('batch claim mismatch')
        if run['plan_id']!=p['plan_id'] or run['execution_id']!=p['execution_id']:raise ValueError('batch execution mismatch')
        attempted=[dict(address=s['address'],candidate=s['candidate']) for s in run['steps']]
        if attempted!=self._flat(p)[:len(attempted)]:raise ValueError('invalid batch attempted prefix')
        return run

    @staticmethod
    def _undo_changes(entry,current,run):
        attempted={s['candidate']['field'] for s in run['steps'] if s['address']==entry['address']}
        values=original_values(current['parameters']);expected=semantic(entry['original']);changes=[]
        for s in reversed(entry['proposal']['steps']):
            if s['field'] not in attempted:continue
            v=values[s['field']]
            if equivalent(v,s['before']):continue
            if not equivalent(v,s['after']):raise ValueError('batch recovery found third parameter value')
            expected=apply_expected(expected,s['field'],v);changes.append(dict(field=s['field'],values=s['before']))
        if not equivalent(semantic(current),expected):raise ValueError('unrelated parameter changed before batch recovery')
        if any(current['actual'][k]!=entry['original']['actual'][k] for k in ('level_raw','kelvin_raw')):
            raise ValueError('live output changed before batch recovery')
        return changes

    def prepare_restore(self,ident):
        with self._locked():
            p=self._load(ident);run=self._execution(p);g,info=self.service._open()
            try:
                if identity(info)!=identity(p['gateway']):raise ValueError('batch restoration gateway changed')
                entries=[]
                # Check all selected lamps including not-attempted ones for outside changes.
                for e in p['entries']:
                    current=self._snapshot(g,p['channel'],e['address']);changes=self._undo_changes(e,current,run)
                    if changes:
                        v=original_values(e['original']['parameters']);limits=(v['configured_min_ct']['kelvin_raw'],v['configured_max_ct']['kelvin_raw'])
                        entries.append(self._entry(p['channel'],dict(address=e['address'],changes=changes),current,limits))
                if not entries:return dict(state='already_original',source_plan_id=ident,device_writes=False)
                return self._publish(info,p['channel'],entries,restore_of=ident)
            finally:g.close()

    def start(self,ident,confirmation):
        with self.runtime_lock:
            if self.active is not None:raise RuntimeError('a parameter batch is already active')
            p=self._load(ident);self._check_gate(p)
            if confirmation!=self._confirmation(p) or p['state']!='prepared' or time.time()>p['expires_at']:
                raise ValueError('invalid, expired or consumed batch plan')
            if (self.service.state_dir/f'parameter-batch-claim-{ident}.json').exists():raise ValueError('batch already claimed; inspect, never replay')
            if not self.service.lock.acquire(blocking=False):raise RuntimeError('gateway busy')
            self.service.lock.release()
            self.active=ident;event=threading.Event();self.event=event
            def work():
                try:self.execute(ident,confirmation,event.is_set)
                except BaseException as exc:self.service._save('parameter-batch-worker-error',dict(plan_id=ident,error=str(exc) or type(exc).__name__),ident)
                finally:
                    with self.runtime_lock:self.active=None;self.event=None
            self.worker=threading.Thread(target=work,daemon=True);self.worker.start()
        return self.get(ident)

    def cancel(self,ident):
        self._id(ident)
        with self.runtime_lock:
            if self.active!=ident:raise ValueError('batch is not active in this process; inspect saved state')
            self.service._save('parameter-batch-cancel',dict(plan_id=ident,requested_at=time.time()),ident)
            self.event.set()
        return self.get(ident)

    def get(self,ident):
        p=self._load(ident);claimed=(self.service.state_dir/f'parameter-batch-claim-{ident}.json').exists()
        result=dict(plan=p,claimed=claimed,automatic_resume=False,
                    notice='Keep this server alive until terminal. Saved phases are not process liveness; never replay uncertain execution.')
        with self.runtime_lock:result['active_in_this_process']=self.active==ident
        if claimed:
            try:result['execution']=self._execution(p)
            except (ValueError,OSError,KeyError,TypeError) as exc:result['execution_unavailable']=str(exc)
        elif p['state']=='prepared':result['confirmation']=self._confirmation(p)
        for kind,key in [('parameter-batch-cancel','cancellation'),('parameter-batch-worker-error','worker_error')]:
            path=self.service.state_dir/f'{kind}-{ident}.json'
            if path.exists():result[key]=self._read(kind,ident)
        return result
