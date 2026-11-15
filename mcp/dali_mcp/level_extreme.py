"""One native recall-minimum/maximum action, verified against fresh configured limits."""
import json,re,time,uuid

class LevelExtremeMixin:
    def set_level_extreme(self,channel,address,extreme):
        if (type(channel) is not int or channel not in (1,2)
                or type(address) is not int or not 0<=address<64
                or type(extreme) is not str or extreme not in ('minimum','maximum')):
            raise ValueError('channel1/2, address0..63 and minimum/maximum required')
        ident=uuid.uuid4().hex;started=time.monotonic()
        report=dict(operation_id=ident,channel=channel,address=address,extreme=extreme,
            state='preflight',started_at=time.time(),command_attempted=False,receipt_status='not_attempted',
            storage_modified=False,color_command_sent=False,automatic_retry=False,automatic_restore=False)
        def save():self._save('level-extreme',report,ident)
        with self.lock:
            g=None
            try:
                save()
                g,report['gateway']=self._open()
                if g.light_type(channel,address)!=1:raise ValueError('confirmed DT8 required')
                minimum=g.read_lamp_parameter(channel,address,'min_level',1)['value']['level_raw']
                maximum=g.read_lamp_parameter(channel,address,'max_level',1)['value']['level_raw']
                if type(minimum) is not int or type(maximum) is not int or not 1<=minimum<=maximum<=254:
                    raise ValueError('invalid configured brightness range')
                report['configured_limits']=dict(minimum=minimum,maximum=maximum)
                report['expected_level_raw']=minimum if extreme=='minimum' else maximum
                before=g.read_actual(channel,address,True);self._relative_values(before)
                report['original']=before
                self._relative_wait(1)
                stable=g.read_actual(channel,address,True);report['preflight_second_read']=stable
                if self._relative_values(stable)!=self._relative_values(before):
                    raise ValueError('output is changing; no extreme command sent')
                report['state']='dispatch_intent';save()
                report['command_attempted']=True
                try:
                    report['receipt']=g.set_level_extreme(channel,address,extreme)
                    report['receipt_status']='received'
                except (Exception,KeyboardInterrupt) as exc:
                    report['receipt_status']='unknown';report['dispatch_error']=str(exc) or type(exc).__name__
                    report['protocol_replies']=list(g.recent_packets)
                    g.close();g=None
                    g,report['readback_gateway']=self._open()
                report['state']='reading_output';save()
                check=self._wait_live_target(g,channel,address,dict(level_raw=report['expected_level_raw'],kelvin_raw=before['kelvin_raw']))
                report['verification']=check;report['actual']=check.get('actual')
                report['color_unchanged']=check.get('actual',{}).get('kelvin_raw')==before['kelvin_raw']
                report['output_changed']=check.get('actual',{}).get('level_raw')!=before['level_raw'] if check.get('actual') else None
                report['target_verified']=bool(check.get('matches_target') and report['color_unchanged'])
                if report['receipt_status']!='received':report['state']='stopped_receipt_unknown'
                elif not report['target_verified']:report['state']='verification_failed'
                else:report['state']='completed_readback'
            except (Exception,KeyboardInterrupt) as exc:
                report['state']='outcome_unknown' if report['command_attempted'] else 'preflight_failed'
                report['error']=str(exc) or type(exc).__name__
            finally:
                if g is not None:
                    try:g.close()
                    except Exception as exc:
                        report['close_error']=str(exc);report['state']='outcome_unknown'
                report['elapsed_seconds']=round(time.monotonic()-started,3)
                try:save()
                except (Exception,KeyboardInterrupt) as exc:
                    report['journal_error']=str(exc) or type(exc).__name__
                    report['state']='outcome_unknown' if report['command_attempted'] else 'preflight_failed'
        return report

    def get_level_extreme(self,operation_id=None):
        if operation_id is None:
            paths=sorted(self.state_dir.glob('level-extreme-*.json'),key=lambda p:p.stat().st_mtime,reverse=True)[:10]
            return dict(operations=[json.loads(p.read_text(encoding='utf8')) for p in paths],
                note='Saved evidence only, not process liveness or current output; never repeats or restores.')
        if type(operation_id) is not str or not re.fullmatch('[0-9a-f]{32}',operation_id):raise ValueError('invalid operation ID')
        return json.loads((self.state_dir/f'level-extreme-{operation_id}.json').read_text(encoding='utf8'))
