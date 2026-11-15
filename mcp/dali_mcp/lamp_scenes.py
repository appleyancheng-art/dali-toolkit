"""Recall one short address, retaining originals and never expanding scope."""
import json
import re
import time
import uuid
from .projects import identity
from .protocol import ProtocolError


class LampSceneMixin:
    def get_lamp_recall(self,operation_id=None):
        if operation_id is None:
            paths=sorted(self.state_dir.glob('recall-lamp-*.json'),key=lambda p:p.stat().st_mtime,reverse=True)[:10]
            return {'operations':[json.loads(p.read_text(encoding='utf-8')) for p in paths],
                    'note':'Saved stages do not prove the process is running. Never resumes or repeats control.'}
        if type(operation_id) is not str or not re.fullmatch('[0-9a-f]{32}',operation_id):
            raise ValueError('invalid lamp recall ID')
        return json.loads((self.state_dir/f'recall-lamp-{operation_id}.json').read_text(encoding='utf-8'))

    def recall_lamp_scene(self,channel,address,scene):
        if (type(channel) is not int or channel not in (1,2) or
                type(address) is not int or not 0<=address<=63 or
                type(scene) is not int or not 0<=scene<=15):
            raise ValueError('channel1/2, address0..63 and scene0..15 required')
        with self.lock:
            ident=uuid.uuid4().hex;started=time.monotonic();g=None
            report=dict(operation_id=ident,channel=channel,address=address,scene=scene,
                scope='one_short_address',state='preflight',started_at=time.time(),
                dispatch_attempted=False,receipt_status='not_sent',verification_status='not_started',
                samples_verified=False,scene_storage_modified=False,automatic_retry=False,
                automatic_rollback=False,physical_address_uniqueness_verified=False,
                non_target_outputs_verified=False)
            def save(required=False):
                try:self._save('recall-lamp',report,ident)
                except OSError as exc:
                    if required:raise
                    report.setdefault('log_errors',[]).append(str(exc))
            save(required=True)
            try:
                g,info=self._open();report['gateway']=info
                report['pending']='read_type';save(required=True)
                typ=g.light_type(channel,address)
                if typ!=1:raise ValueError('single-address scene verification currently requires DT8')
                report['pending']='read_fade';save(required=True)
                fade=g.read_fade_timing(channel,address)
                bound=fade['fade_seconds_upper']
                if bound is None or bound>7.75:raise ValueError('unknown/long fade; no recall sent')
                report['fade']=fade
                report['pending']='read_scene';save(required=True)
                target=g.read_scene(channel,address,scene,typ);report['scene_value']=target
                report['pending']='read_original';save(required=True)
                before=g.read_actual(channel,address,True);report['before']=before
                if (before['light_type_code']!=1 or not 0<=before['level_raw']<=254 or
                        not 1000<=before['kelvin_raw']<=10000 or
                        not 0<=target['level_raw']<=255 or
                        (target['kelvin_raw']!=65535 and not 1000<=target['kelvin_raw']<=10000)):
                    raise ValueError('unknown scene/live value; no recall sent')
                active={k:target[k] for k,mask in (('level_raw',255),('kelvin_raw',65535)) if target[k]!=mask}
                report['expected']={k:active.get(k,before[k]) for k in ('level_raw','kelvin_raw')}
                report['active_components']=list(active)
                report.pop('pending',None)
                report['state']='dispatching';report['receipt_status']='unknown'
                # Publish originals and intent before the only control call. A saved
                # intent is not proof that dispatch occurred if the process dies.
                save(required=True)
                report['dispatch_attempted']=True
                try:
                    report['receipt']=g.recall_lamp_scene(channel,address,scene)
                    report['receipt_status']='received'
                except (TimeoutError,ProtocolError,OSError) as exc:
                    report['dispatch_error']=str(exc);report['protocol_replies']=list(g.recent_packets)
                    save();g.close();g=None
                    g,new_info=self._open()
                    if identity(new_info)!=identity(info):raise ValueError('gateway changed after uncertain recall')
                report['state']='verifying';report['pending']='read_actual';save()
                row=self._wait_live_target(g,channel,address,report['expected'])
                report['verification']=row
                match=row.get('matches_target',False) and not row.get('error')
                report['output_matches_expected']=bool(match)
                report['samples_verified']=bool(active) and bool(match)
                report['verification_status']=('samples_verified' if report['samples_verified'] else
                    'unverifiable' if match and not active else 'not_verified')
                report['state']=('stopped_receipt_unknown' if 'dispatch_error' in report else
                    'samples_verified' if report['samples_verified'] else 'receipt_received_unverified')
                report.pop('pending',None)
            except (Exception,KeyboardInterrupt) as exc:
                report.update(state='outcome_unknown' if report['dispatch_attempted'] else 'preflight_failed',
                              error=str(exc) or type(exc).__name__)
            finally:
                if g is not None:g.close()
                report['elapsed_seconds']=round(time.monotonic()-started,3);report['finished_at']=time.time()
                if report.get('log_errors'):report['state']='outcome_unknown'
                save()
                if report.get('log_errors'):report['state']='outcome_unknown'
            return report
