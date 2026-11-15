"""Experimental parameter transactions. Not registered as MCP tools.

Independent exact-scope enablement is mandatory. Receipts are recorded without
assuming an undocumented status meaning; success is established by fresh reads.
An OS lock serializes parameter controllers sharing one state directory. Other
tool families, directories, panels and ConfigMaster do not share this lock.
"""
import copy
from contextlib import contextmanager
import hashlib
import json
import os
import re
import time
import uuid
from .parameter_planning import plan_parameter_candidates,original_values,ParameterPatch,validate_values
from .projects import identity
from .protocol import Gateway


def equivalent(actual,expected,key=None):
    if isinstance(expected,dict):
        return isinstance(actual,dict) and actual.keys()==expected.keys() and all(equivalent(actual[k],v,k) for k,v in expected.items())
    if key=='kelvin_raw' and type(actual) is int and type(expected) is int and 0<actual<65535 and 0<expected<65535:
        return 1000000//actual==1000000//expected
    return type(actual) is type(expected) and actual==expected


def semantic(snapshot):
    return {f:copy.deepcopy(p.get('value')) for f,p in snapshot['parameters'].items() if f!='status'}


def apply_expected(values,field,target):
    result=copy.deepcopy(values)
    if field in ('fade_time','fade_rate'):
        fade=result['fade_time_rate'];fade['fade_time_code' if field=='fade_time' else 'fade_rate_code']=target['code']
        fade['raw']=(fade['fade_time_code']<<4)|fade['fade_rate_code'];fade['extended_fade_not_read']=fade['fade_time_code']==0
    else:result[field]=copy.deepcopy(target)
    return result


def send_candidate_once(g,step):
    """Send once; asynchronous parameter status zero is not a terminal receipt.

    ConfigMaster::handleData logs F0A0/F0A2/F0A6/F0A8 status 0 as saving in
    progress. Wait on this exchange before issuing queries or reconnecting.
    Nonzero status alone still does not establish the requested stored values.
    """
    if g.route is None:raise RuntimeError('connected verified gateway required')
    route=g.route;payload=bytes.fromhex(step['payload_hex']);command=int(step['command'],16)
    alias=route[3:]+bytes([route[0],int(g.sock.getsockname()[0].split('.')[-1]),route[2]])
    asynchronous=command in (0xf020,0xf022,0xf026,0xf028)
    def accept(p):
        if len(p.payload)!=8 or p.payload[:3]!=payload[:3]:return False
        direct=p.sequence==g.seq and p.transport==1 and p.route in (route[3:]+route[:3],alias)
        final=asynchronous and p.sequence==0 and p.transport==2 and p.route==route[3:]+route[:3]
        return (direct or final) and (not asynchronous or p.payload[3]!=0)
    reply=g.exchange(command,payload,route,accept)
    if command==0xf024 and reply.payload[3]!=0:
        raise RuntimeError(f'curve save reported failure; reply={reply.command:04X}/{reply.payload.hex()}; readback required')
    result={'reply_command':f'{reply.command:04X}','payload':reply.payload.hex(),
            'sequence':reply.sequence,'route':reply.route.hex(),'status_interpreted':False}
    if asynchronous:
        result.update(waited_for_terminal_status=True,terminal_status_byte=reply.payload[3],
                      status_zero_means='saving_in_progress',readback_required=True)
    elif command==0xf024:
        result.update(status_interpreted=True,status_zero_means='save_success',readback_required=True)
    return result


class ExperimentalParameterController:
    def __init__(self,service):self.service=service

    @contextmanager
    def _locked(self):
        with self.service.lock:
            with (self.service.state_dir/'experimental-parameters.lock').open('a+b') as handle:
                if handle.seek(0,2)==0:
                    handle.write(b'\0');handle.flush()
                handle.seek(0)
                try:
                    if os.name=='nt':
                        import msvcrt
                        msvcrt.locking(handle.fileno(),msvcrt.LK_NBLCK,1)
                    else:
                        import fcntl
                        fcntl.flock(handle.fileno(),fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as exc:
                    raise RuntimeError('another experimental parameter controller holds the state directory') from exc
                try:yield
                finally:
                    handle.seek(0)
                    if os.name=='nt':msvcrt.locking(handle.fileno(),msvcrt.LK_UNLCK,1)
                    else:fcntl.flock(handle.fileno(),fcntl.LOCK_UN)

    @staticmethod
    def _digest(plan):
        return hashlib.sha256(json.dumps({k:v for k,v in plan.items() if k not in ('digest','state','execution_id','confirmation')},
            sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()

    def _load(self,ident,_seen=None):
        if type(ident) is not str or not re.fullmatch('[0-9a-f]{32}',ident):raise ValueError('invalid parameter plan ID')
        seen=set() if _seen is None else set(_seen)
        if ident in seen or len(seen)>=16:raise ValueError('invalid restoration ancestry')
        seen.add(ident)
        plan=json.loads((self.service.state_dir/f'parameter-plan-{ident}.json').read_text(encoding='utf-8'))
        if plan.get('plan_id')!=ident or plan.get('digest')!=self._digest(plan):raise ValueError('parameter plan checksum mismatch')
        limits=None
        if plan.get('restore_of') is not None:
            parent=self._load(plan['restore_of'],seen)
            if (parent['state']!='consumed' or identity(parent['gateway'])!=identity(plan['gateway']) or
                parent['channel']!=plan['channel'] or parent['address']!=plan['address']):raise ValueError('invalid restoration source')
            values=original_values(parent['original']['parameters'])
            limits=(values['configured_min_ct']['kelvin_raw'],values['configured_max_ct']['kelvin_raw'])
        # A checksum detects corruption, not authorization. Rebuild wire bytes
        # from typed, scoped fields so even a rehashed edited payload is refused.
        expected=plan_parameter_candidates(plan['channel'],plan['address'],plan['original'],plan['changes'],reference_ct_limits=limits)
        if expected!=plan['proposal']:raise ValueError('parameter proposal does not match validated fields')
        return plan

    @staticmethod
    def _snapshot(g,channel,address):
        parameters={};typ=None
        for field in Gateway.PARAMETER_FIELDS:
            result=g.read_lamp_parameter(channel,address,field,typ);result['captured_at']=time.time();parameters[field]=result
            if field=='light_type':typ=result['value']['code']
        original_values(parameters)  # Validate DT8 and complete parameter evidence.
        actual=g.read_actual(channel,address,True)
        if actual['light_type_code']!=1:raise ValueError('live type and DT8 parameters disagree')
        return {'channel':channel,'address':address,'parameters':parameters,'actual':actual,'captured_at':time.time()}

    def _prepare(self,info,snapshot,changes,restore_of=None,reference_ct_limits=None,source_project=None,source_workbook=None,native_workbook_review=None):
        proposal=plan_parameter_candidates(snapshot['channel'],snapshot['address'],snapshot,changes,reference_ct_limits=reference_ct_limits)
        plan={'plan_id':uuid.uuid4().hex,'gateway':info,'channel':snapshot['channel'],'address':snapshot['address'],
              'changes':changes,'original':snapshot,'proposal':proposal,'restore_of':restore_of,
              'created_at':time.time(),'expires_at':time.time()+600,'state':'prepared',
              'validation':'experimental; consult release documentation for field-specific physical validation'}
        if source_project is not None:plan['source_project']=copy.deepcopy(source_project)
        if source_workbook is not None:
            plan['source_workbook']=copy.deepcopy(source_workbook)
            plan['native_workbook_review']=copy.deepcopy(native_workbook_review)
        plan['digest']=self._digest(plan);self.service._save('parameter-plan',plan,plan['plan_id'])
        return dict(plan,confirmation=f"APPLY PARAMETERS {plan['plan_id']} {plan['digest'][:12]}")

    def prepare(self,channel,address,changes):
        # Reject obvious invalid scope before opening a gateway.
        if type(channel) is not int or channel not in (1,2) or type(address) is not int or not 0<=address<=63:
            raise ValueError('explicit channel1/2 and address0..63 required')
        if not isinstance(changes,list) or not 1<=len(changes)<=9:raise ValueError('provide1..9 explicit parameter patches')
        for change in changes:
            p=ParameterPatch.model_validate(change)
            validate_values(p.field,p.values,partial=True)
        if len({p['field'] for p in changes})!=len(changes):raise ValueError('duplicate parameter field')
        with self._locked():
            g,info=self.service._open()
            try:return self._prepare(info,self._snapshot(g,channel,address),changes)
            finally:g.close()

    def _check_gate(self,plan):
        config=self.service.config
        if config.get('enable_experimental_parameter_writes') is not True:
            raise PermissionError('Parameter writes require their separate experimental switch and explicit user authorization')
        allowed=config.get('allowed_parameter_write_targets',{})
        if (not isinstance(allowed,dict) or type(allowed.get('channel')) is not int or type(allowed.get('address')) is not int or
            allowed['channel']!=plan['channel'] or allowed['address']!=plan['address']):
            raise PermissionError('Parameter target not authorized by this execution configuration')
        permitted=allowed.get('values',[])
        if not isinstance(permitted,list):raise PermissionError('invalid parameter field/value scope')
        for entry in permitted:
            try:
                p=ParameterPatch.model_validate(entry);validate_values(p.field,p.values)
            except ValueError as exc:raise PermissionError('invalid parameter field/value scope') from exc
        if any({'field':s['field'],'values':s['after']} not in permitted for s in plan['proposal']['steps']):
            raise PermissionError('Parameter field/value outside exact execution scope')

    def execute(self,ident,confirmation):
        with self._locked():
            plan=self._load(ident);self._check_gate(plan)
            if confirmation!=f"APPLY PARAMETERS {ident} {plan['digest'][:12]}":raise ValueError('confirmation mismatch')
            if plan['state']!='prepared' or time.time()>plan['expires_at']:raise ValueError('parameter plan consumed or expired')
            g=None;run_id=uuid.uuid4().hex
            # Durable exclusive claim survives failed preflight, crashes and a
            # rewound plan file. Never delete it or retry an existing plan.
            with (self.service.state_dir/f'parameter-claim-{ident}.json').open('x',encoding='utf-8') as claim:
                json.dump(dict(plan_id=ident,digest=plan['digest'],execution_id=run_id,claimed_at=time.time()),claim)
                claim.flush();os.fsync(claim.fileno())
            run={'execution_id':run_id,'plan_id':ident,'state':'preflight','steps':[],'started_at':time.time(),
                 'receipt_status_interpreted':False,'automatic_rollback':False,'live_control_commands_sent':False}
            def save():self.service._save('parameter-run',run,run_id)
            try:
                g,info=self.service._open()
                if identity(info)!=identity(plan['gateway']):raise ValueError('parameter plan gateway changed')
                current=self._snapshot(g,plan['channel'],plan['address']);run['fresh_before']=current
                if not equivalent(semantic(current),semantic(plan['original'])):raise ValueError('configuration changed since preparation')
                live_keys=('level_raw','kelvin_raw')
                if any(current['actual'][k]!=plan['original']['actual'][k] for k in live_keys):raise ValueError('live output changed since preparation')
                save()  # Fresh originals must be durable before consuming or sending.
                plan.update(state='consumed',execution_id=run_id);self.service._save('parameter-plan',plan,ident)
                expected=semantic(current)
                if plan['proposal']['steps']:
                    run['capability_invalidation_id']=self.service.invalidate_capabilities(plan['channel'],[plan['address']],f'parameter execution {run_id}')
                    save()
                for candidate in plan['proposal']['steps']:
                    step={'candidate':candidate,'state':'outcome_unknown'};run['steps'].append(step);run['state']='writing';save()
                    send_error=None
                    try:step['receipt']=send_candidate_once(g,candidate)
                    except (Exception,KeyboardInterrupt) as exc:
                        send_error=str(exc) or type(exc).__name__;step['dispatch_error']=send_error
                        g.close();g=None
                        # Fresh identity-checked session is for reads only, never for resend.
                        g,info=self.service._open()
                        if identity(info)!=identity(plan['gateway']):raise RuntimeError('gateway changed after uncertain write')
                    after=self._snapshot(g,plan['channel'],plan['address']);step['readback']=after
                    expected=apply_expected(expected,candidate['field'],candidate['after'])
                    if not equivalent(semantic(after),expected):
                        step['state']='verification_failed';run['state']='stopped_verification_failed';save();break
                    step['state']='verified_receipt_unknown' if send_error else 'verified_readback'
                    step['live_output_changed']=any(after['actual'][k]!=current['actual'][k] for k in live_keys)
                    current=after
                    if send_error:run['state']='stopped_receipt_unknown';save();break
                    if step['live_output_changed']:
                        run['state']='stopped_live_output_changed';save();break
                    save()
                else:run['state']='completed_readback'
            except (Exception,KeyboardInterrupt) as exc:
                run.update(state='stopped_unknown' if run['steps'] else 'preflight_failed',error=str(exc) or type(exc).__name__)
            finally:
                if g is not None:g.close()
                run['finished_at']=time.time()
                try:save()
                except Exception as exc:run['log_error']=str(exc)
            return run

    def _execution(self,plan):
        run_id=plan.get('execution_id')
        if type(run_id) is not str or not re.fullmatch('[0-9a-f]{32}',run_id):
            raise ValueError('invalid source execution ID')
        claim=json.loads((self.service.state_dir/f"parameter-claim-{plan['plan_id']}.json").read_text(encoding='utf-8'))
        if any(claim.get(k)!=v for k,v in dict(plan_id=plan['plan_id'],digest=plan['digest'],execution_id=run_id).items()):
            raise ValueError('execution claim does not match source plan')
        run=json.loads((self.service.state_dir/f'parameter-run-{run_id}.json').read_text(encoding='utf-8'))
        if run.get('plan_id')!=plan['plan_id'] or run.get('execution_id')!=run_id:
            raise ValueError('execution does not match source plan')
        attempted=[s['candidate'] for s in run['steps']]
        if attempted!=plan['proposal']['steps'][:len(attempted)]:
            raise ValueError('invalid parameter execution prefix')
        return run

    def prepare_restore(self,ident):
        """User-requested recovery only. Read actual state; never blindly replay undo."""
        with self._locked():
            plan=self._load(ident)
            if plan['state']!='consumed':raise ValueError('no attempted execution to restore')
            run=self._execution(plan)
            attempted={s['candidate']['field'] for s in run['steps']}
            if not attempted:raise ValueError('no attempted parameter changes')
            g,info=self.service._open()
            try:
                if identity(info)!=identity(plan['gateway']):raise ValueError('restoration gateway changed')
                current=self._snapshot(g,plan['channel'],plan['address'])
                current_values=original_values(current['parameters']);expected=semantic(plan['original']);changes=[]
                for candidate in reversed(plan['proposal']['steps']):
                    field=candidate['field']
                    if field not in attempted:continue
                    value=current_values[field]
                    if equivalent(value,candidate['before']):continue
                    if not equivalent(value,candidate['after']):raise ValueError('current field is neither original nor requested target; inspect before recovery')
                    expected=apply_expected(expected,field,value)
                    changes.append({'field':field,'values':candidate['before']})
                if not equivalent(semantic(current),expected):raise ValueError('other configuration changed; recovery requires inspection')
                if not changes:return {'state':'already_original','source_plan_id':ident,'device_writes':False}
                original=original_values(plan['original']['parameters'])
                limits=(original['configured_min_ct']['kelvin_raw'],original['configured_max_ct']['kelvin_raw'])
                return self._prepare(info,current,changes,restore_of=ident,reference_ct_limits=limits)
            finally:g.close()

    def get(self,plan_id=None):
        if plan_id is None:
            paths=sorted(self.service.state_dir.glob('parameter-plan-*.json'),key=lambda p:p.stat().st_mtime,reverse=True)[:10]
            result={'plans':[],'excluded_records':[],
                'notice':'Local saved evidence, not proof of process liveness. Never resumes or resends.'}
            for path in paths:
                ident=path.stem.removeprefix('parameter-plan-')
                try:
                    p=self._load(ident)
                    result['plans'].append({k:p[k] for k in ('plan_id','channel','address','state','created_at','restore_of')})
                except (ValueError,KeyError,TypeError,OSError) as exc:
                    result['excluded_records'].append({'plan_id':ident,'reason':str(exc)})
            return result
        p=self._load(plan_id)
        claim=self.service.state_dir/f'parameter-claim-{plan_id}.json'
        result={'plan':p,'claimed':claim.exists(),'automatic_resume':False,
                'notice':'A claim without a readable execution is uncertain, never permission to rerun.'}
        if claim.exists():
            try:
                result['claim']=json.loads(claim.read_text(encoding='utf-8'))
                result['execution']=self._execution(p)
            except (ValueError,KeyError,TypeError,OSError) as exc:
                result['execution_unavailable']=str(exc)
        elif p['state']=='prepared':
            result['confirmation']=f"APPLY PARAMETERS {plan_id} {p['digest'][:12]}"
        return result
