"""One group scene recall, fresh membership, durable originals, no resend."""
import json
import re
import time
import uuid
from .projects import identity
from .protocol import ProtocolError


class GroupSceneMixin:
    def get_group_recall(self,operation_id=None):
        if operation_id is None:
            paths=sorted(self.state_dir.glob('recall-group-*.json'),key=lambda p:p.stat().st_mtime,reverse=True)[:10]
            return {'operations':[json.loads(p.read_text(encoding='utf-8')) for p in paths],
                    'note':'Saved progress is not proof of a live process. Never resumes or repeats a recall.'}
        if type(operation_id) is not str or not re.fullmatch('[0-9a-f]{32}',operation_id):raise ValueError('invalid group recall ID')
        return json.loads((self.state_dir/f'recall-group-{operation_id}.json').read_text(encoding='utf-8'))

    def recall_group_scene(self,channel,group,scene,expected_addresses,verify_addresses):
        if (type(channel) is not int or channel not in (1,2) or type(group) is not int or not 0<=group<=15 or
            type(scene) is not int or not 0<=scene<=15):raise ValueError('channel1/2, group0..15, scene0..15 required')
        if (not isinstance(expected_addresses,list) or not 1<=len(expected_addresses)<=64 or
            any(type(a) is not int or not 0<=a<=63 for a in expected_addresses) or len(set(expected_addresses))!=len(expected_addresses)):
            raise ValueError('provide1..64 unique expected group addresses')
        if (not isinstance(verify_addresses,list) or not 1<=len(verify_addresses)<=8 or
            any(type(a) is not int or a not in expected_addresses for a in verify_addresses) or len(set(verify_addresses))!=len(verify_addresses)):
            raise ValueError('provide1..8 unique verification addresses within the expected group')
        with self.lock:
            started=time.monotonic();ident=uuid.uuid4().hex;g=None
            report={'operation_id':ident,'channel':channel,'group':group,'scene':scene,'scope':'explicit_group',
                    'expected_addresses':sorted(expected_addresses),'verify_addresses':verify_addresses,
                    'state':'preflight','started_at':time.time(),'membership_rows':[],'originals':[],'verification':[],
                    'receipt_status':'not_sent','verification_status':'not_started','sent_count':0,'samples_verified':False,
                    'scene_storage_modified':False,'group_membership_modified':False,'automatic_rollback':False,
                    'atomic_snapshot':False,'nonmember_verification_performed':False}
            def save():self._save('recall-group',report,ident)
            save()
            try:
                g,info=self._open();report['gateway']=info
                report['pending']={'phase':'scan'};save()
                report['scan']=g.scan_existing(channel,self.config.get('scan_timeout_seconds',240.0));save()
                # The pinned firmware needs a new session after scan for group queries.
                g.close();g=None;g,second=self._open()
                if identity(second)!=identity(info):raise ValueError('gateway changed after scan')
                members=[]
                for address in report['scan']['addresses']:
                    report['pending']={'phase':'membership','address':address};save()
                    row=g.read_groups(channel,address);report['membership_rows'].append(row)
                    if group in row['groups']:members.append(address)
                report['actual_members']=members
                if sorted(members)!=sorted(expected_addresses):
                    report.update(state='scope_changed',stop_reason='Fresh membership differs; no recall sent')
                    return report
                fades=self._cached_profiles(channel,'fade')
                for address in members:
                    report['pending']={'phase':'originals','address':address};save()
                    typ=g.light_type(channel,address)
                    if typ!=1:raise ValueError('group scene verification currently requires DT8 members')
                    fade=fades[address]['profile'] if address in fades else g.read_fade_timing(channel,address)
                    bound=fade['fade_seconds_upper']
                    if bound is None or bound>7.75:raise ValueError('unknown/long member fade; no recall sent')
                    target=g.read_scene(channel,address,scene,typ)
                    before=g.read_actual(channel,address,True)
                    if before['light_type_code']!=1:raise ValueError('live type and DT8 membership disagree')
                    if (target['kelvin_raw']!=65535 and not 1000<=target['kelvin_raw']<=10000 or
                        not 1000<=before['kelvin_raw']<=10000):raise ValueError('unknown color value; cannot verify or retain restoration evidence')
                    if not 0<=before['level_raw']<=254:raise ValueError('unknown live level')
                    report['originals'].append({'address':address,'before':before,'scene_value':target,'fade':fade})
                report.pop('pending',None);report['preflight_seconds']=round(time.monotonic()-started,3)
                report['state']='dispatching';report['sent_count']=1;report['receipt_status']='unknown';save()
                send_started=time.monotonic()
                try:
                    report['receipt']=g.recall_group_scene(channel,group,scene);report['receipt_status']='received'
                except (TimeoutError,ProtocolError,OSError) as exc:
                    report['dispatch_error']=str(exc);report['protocol_replies']=list(g.recent_packets)
                    g.close();g=None;g,after_info=self._open()
                    if identity(after_info)!=identity(info):raise ValueError('gateway changed after uncertain recall')
                report['send_and_receipt_seconds']=round(time.monotonic()-send_started,3)
                report['state']='verifying';save()
                originals={x['address']:x for x in report['originals']}
                for address in verify_addresses:
                    report['pending']={'phase':'verification','address':address};save()
                    original=originals[address];target=original['scene_value'];before=original['before']
                    active={k:target[k] for k,mask in (('level_raw',255),('kelvin_raw',65535)) if target[k]!=mask}
                    expected={k:active.get(k,before[k]) for k in ('level_raw','kelvin_raw')}
                    row=self._wait_live_target(g,channel,address,expected)
                    row.update(scene_value=target,before=before,verifiable=bool(active),
                               matches_scene=bool(active) and row.get('matches_target',False))
                    report['verification'].append(row);save()
                    if row.get('error'):break
                checked=[r for r in report['verification'] if r['verifiable']]
                report['verified_sample_count']=sum(r['matches_scene'] for r in checked)
                all_read=len(report['verification'])==len(verify_addresses)
                all_match=all(r.get('matches_target',False) for r in report['verification'])
                report['samples_verified']=bool(checked) and all_read and all_match
                report['verification_status']=('samples_verified' if report['samples_verified'] else
                    'unverifiable' if all_read and all_match and not checked else 'not_verified')
                report['state']=('stopped_receipt_unknown' if 'dispatch_error' in report else
                    'samples_verified' if report['samples_verified'] else 'receipt_received_unverified')
                report.pop('pending',None)
            except (Exception,KeyboardInterrupt) as exc:
                report.update(state='outcome_unknown' if report['sent_count'] else 'preflight_failed',error=str(exc) or type(exc).__name__)
            finally:
                if g is not None:g.close()
                report['elapsed_seconds']=round(time.monotonic()-started,3);report['finished_at']=time.time()
                try:save()
                except OSError as exc:report['log_error']=str(exc)
            return report
