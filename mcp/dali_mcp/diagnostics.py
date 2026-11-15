"""Fresh scoped observations and consistency checks, never a repair action."""
import json,re,time,uuid
from .protocol import Gateway


def assess(parameters,actual):
    """Compare captured values only; raw status is not a decoded fault diagnosis."""
    values={k:v.get('value') for k,v in parameters.items()}
    findings=[]
    def note(code,**detail):findings.append(dict(code=code,**detail))
    typ=values['light_type']['code']
    if actual['light_type_code']!=typ:note('parameter_live_type_mismatch',parameter_type=typ,actual_type=actual['light_type_code'])
    low=values['min_level']['level_raw'];high=values['max_level']['level_raw'];level=actual['level_raw']
    if not 1<=low<=high<=254:note('invalid_configured_level_range',minimum=low,maximum=high)
    if not 0<=level<=254:note('unknown_live_level',raw=level)
    elif level and not low<=level<=high:note('live_level_outside_configured_range',actual=level,minimum=low,maximum=high)
    if values['fade_time_rate']['extended_fade_not_read']:note('extended_fade_duration_unknown')
    if values['dimming_curve']['raw'] not in (0,1):note('unknown_dimming_curve',raw=values['dimming_curve']['raw'])
    if typ==1:
        low=values['configured_min_ct']['kelvin_raw'];high=values['configured_max_ct']['kelvin_raw'];kelvin=actual['kelvin_raw']
        if not 1000<=low<=high<=10000:note('invalid_configured_ct_range',minimum=low,maximum=high)
        if not 1000<=kelvin<=10000:note('unknown_live_ct',raw=kelvin)
        elif not low<=kelvin<=high:note('live_ct_outside_configured_range',actual=kelvin,minimum=low,maximum=high)
    return dict(findings=findings,raw_status=values['status']['raw'],status_bits_interpreted=False,
        conclusion='findings_present' if findings else 'no_inconsistency_observed',
        note='Sequential observations can straddle fades or external changes. Not proof of physical health, fault absence or address uniqueness.')


class DiagnosticsMixin:
    def diagnose_lamps(self,channel,addresses):
        if type(channel) is not int or channel not in (1,2):raise ValueError('channel1/2 required')
        if (not isinstance(addresses,list) or not 1<=len(addresses)<=16 or
                any(type(a) is not int or not 0<=a<=63 for a in addresses) or len(set(addresses))!=len(addresses)):
            raise ValueError('provide1..16 unique addresses0..63')
        ident=uuid.uuid4().hex;started=time.monotonic()
        report=dict(operation_id=ident,state='starting',channel=channel,addresses=list(addresses),rows=[],
            started_at=time.time(),device_writes=False,automatic_retry=False,automatic_repair=False,
            atomic_snapshot=False,physical_health_verified=False,address_inventory_scanned=False,
            source='fresh_sequential_bus_reads',capability_cache_used=False,
            not_checked=['scenes','physical_ct_limits','extended_fade_duration','device_version',
                         'physical_wiring','address_uniqueness','decoded_status_fault_bits'])
        def save():self._save('diagnostic',report,ident)
        with self.lock:
            g=None
            save()  # Failed initial recording must prevent even a query.
            try:
                report['state']='connecting';report['pending']=dict(phase='gateway_identity_and_handshake');save()
                g,info=self._open();report['gateway']=info;report['state']='reading';save()
                for address in addresses:
                    row=dict(address=address,state='reading',parameters={});report['rows'].append(row);typ=None
                    for field in Gateway.PARAMETER_FIELDS:
                        report['pending']=dict(address=address,field=field);save()
                        value=g.read_lamp_parameter(channel,address,field,typ)
                        value['captured_at']=time.time();row['parameters'][field]=value
                        if field=='light_type':typ=value['value']['code']
                        save()
                    report['pending']=dict(address=address,field='actual');save()
                    row['actual']=g.read_actual(channel,address,typ==1)
                    row['assessment']=assess(row['parameters'],row['actual']);row['state']='completed';save()
                report.pop('pending',None);report['state']='completed'
            except (Exception,KeyboardInterrupt) as exc:
                if report['rows'] and report['rows'][-1]['state']=='reading':report['rows'][-1]['state']='partial'
                report['state']='connection_failed' if g is None else 'stopped_partial'
                report['error']=str(exc) or type(exc).__name__
                report['error_type']=type(exc).__name__
                report['offline_conclusion']=False
                if g is not None:report['protocol_replies']=list(g.recent_packets)
            finally:
                if g is not None:
                    try:g.close()
                    except Exception as exc:report['close_error']=str(exc);report['state']='stopped_partial'
                report['completed_addresses']=[r['address'] for r in report['rows'] if r['state']=='completed']
                report['uncompleted_addresses']=[a for a in addresses if a not in report['completed_addresses']]
                report['finding_count']=sum(len(r.get('assessment',{}).get('findings',[])) for r in report['rows'])
                report['finished_at']=time.time();report['elapsed_seconds']=round(time.monotonic()-started,3)
                save()
        return report

    def get_diagnostic(self,operation_id=None):
        if operation_id is None:
            paths=sorted(self.state_dir.glob('diagnostic-*.json'),key=lambda p:p.stat().st_mtime,reverse=True)[:10]
            return dict(operations=[json.loads(p.read_text(encoding='utf-8')) for p in paths],
                note='Local saved observations only; not current online state or a live-process indicator. Never retries or repairs.')
        if type(operation_id) is not str or not re.fullmatch('[0-9a-f]{32}',operation_id):raise ValueError('invalid diagnostic ID')
        return json.loads((self.state_dir/f'diagnostic-{operation_id}.json').read_text(encoding='utf-8'))
