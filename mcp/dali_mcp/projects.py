"""Portable project evidence and scene plans; group plans live in group_execution."""
import hashlib
import json
import math
import re
import time
import uuid
from .protocol import Gateway
from .project_layouts import validate_project_layouts, attach_project_layouts, import_project_layout, compare_layouts
from .project_floorplan import validate_project_floorplan, compare_floorplans
from .project_names import validate_project_names, compare_names


def scope_values(channel, addresses, scenes):
    if type(channel) is not int or channel not in (1,2):raise ValueError('channel1/2 required')
    for values,maximum,label in ((addresses,63,'addresses'),(scenes,15,'scenes')):
        if (not isinstance(values,list) or not values or len(values)>maximum+1 or
            any(type(v) is not int or not 0<=v<=maximum for v in values) or len(set(values))!=len(values)):
            raise ValueError(f'explicit unique {label}0..{maximum} required')


def digest(document):
    payload=json.dumps({k:v for k,v in document.items() if k!='digest'},sort_keys=True,
                       separators=(',',':'),ensure_ascii=False,allow_nan=False)
    if len(payload.encode('utf-8'))>5_000_000:raise ValueError('project exceeds5MB')
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def identity(gateway):
    return (gateway['ip'],gateway['dali_mac'].lower(),gateway['dali_firmware'])


def validate_parameter(field,record,typ):
    if not isinstance(record,dict):raise ValueError('invalid parameter record')
    if typ==0 and field.startswith('configured_'):
        if record.get('state')!='not_applicable' or 'value' in record:raise ValueError('DT6 CT field must be not_applicable')
        return
    value=record.get('value')
    if not isinstance(value,dict) or record.get('state') not in (None,'completed'):raise ValueError('missing parameter value')
    def integer(key,maximum):
        v=value[key]
        if type(v) is not int or not 0<=v<=maximum:raise ValueError(f'invalid {field}.{key}')
        return v
    if field=='light_type':
        expected={'code','name'}
        if integer('code',1)!=typ or value['name']!=('DT8' if typ else 'DT6'):raise ValueError('inconsistent fixture type')
    elif field=='groups':
        expected={'groups','group_mask'};mask=integer('group_mask',65535);groups=value['groups']
        if (not isinstance(groups,list) or any(type(g) is not int for g in groups) or
            groups!=[g for g in range(16) if mask&(1<<g)]):raise ValueError('inconsistent group mask/list')
    elif field=='fade_time_rate':
        expected={'raw','fade_time_code','fade_rate_code','extended_fade_not_read'};raw=integer('raw',255)
        if integer('fade_time_code',15)!=raw>>4 or integer('fade_rate_code',15)!=raw&15:raise ValueError('inconsistent fade codes')
        if type(value['extended_fade_not_read']) is not bool or value['extended_fade_not_read']!=(raw>>4==0):raise ValueError('invalid extended fade marker')
    elif field in ('power_on','system_failure'):
        expected={'level_raw'}|({'kelvin_raw'} if typ else set());integer('level_raw',255)
        if typ:integer('kelvin_raw',65535)
    elif field in ('min_level','max_level'):
        expected={'level_raw'};integer('level_raw',255)
    elif field.startswith('configured_'):
        expected={'kelvin_raw'};integer('kelvin_raw',65535)
    else:
        expected={'raw'};integer('raw',255)
    if set(value)!=expected:raise ValueError(f'unexpected or missing {field} value fields')


def validate_document(document):
    """Validate completeness and restore-relevant data, not provenance authenticity."""
    try:
        if not isinstance(document,dict) or document.get('format')!='dali-toolkit-project' or type(document.get('schema_version')) is not int or document['schema_version'] not in (1,2,3,4):
            raise ValueError('unsupported project format/version')
        if document['state']!='completed':raise ValueError('incomplete project cannot be imported, compared or restored')
        if document.get('atomic_snapshot') is not False or document.get('device_writes') is not False:
            raise ValueError('project format requires non-atomic, read-only capture semantics')
        if type(document['project_id']) is not str or not re.fullmatch('[0-9a-f]{32}',document['project_id']):raise ValueError('invalid project_id')
        if not isinstance(document['name'],str) or not 1<=len(document['name'].strip())<=80:raise ValueError('invalid project name')
        if document.get('digest')!=digest(document):raise ValueError('project checksum mismatch')
        for stamp in ('started_at','completed_at'):
            if type(document[stamp]) not in (float,int) or not math.isfinite(document[stamp]):raise ValueError('invalid capture time')
        if document['completed_at']<document['started_at']:raise ValueError('invalid capture interval')
        gateway=document['gateway']
        if any(type(gateway.get(k)) is not str or not gateway[k] for k in ('ip','dali_mac','dali_firmware')):raise ValueError('missing gateway identity')
        if not re.fullmatch(r'(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}',gateway['dali_mac']):raise ValueError('invalid gateway MAC')
        scope=document['scope'];channel=scope['channel'];addresses=scope['addresses'];scenes=scope['scenes']
        scope_values(channel,addresses,scenes)
        rows=document['rows']
        if not isinstance(rows,list) or len(rows)!=len(addresses):raise ValueError('incomplete fixture rows')
        seen=[]
        for row in rows:
            a=row['address']
            if type(a) is not int or a not in addresses or a in seen or type(row['channel']) is not int or row['channel']!=channel:raise ValueError('invalid fixture scope')
            seen.append(a)
            parameters=row['parameters']
            if not isinstance(parameters,dict) or set(parameters)!=set(Gateway.PARAMETER_FIELDS):raise ValueError('incomplete supported parameters')
            typ=parameters['light_type']['value']['code']
            if type(typ) is not int or typ not in (0,1):raise ValueError('unsupported fixture type')
            for field,p in parameters.items():validate_parameter(field,p,typ)
            stored=row['scenes']
            if not isinstance(stored,list) or len(stored)!=len(scenes):raise ValueError('incomplete scene rows')
            scene_seen=[]
            for s in stored:
                for key,expected in (('channel',channel),('address',a),('light_type_code',typ)):
                    if type(s[key]) is not int or s[key]!=expected:raise ValueError('scene target/type inconsistent')
                sn=s['scene']
                if type(sn) is not int or sn not in scenes or sn in scene_seen:raise ValueError('invalid scene scope')
                scene_seen.append(sn)
                for key,maximum in (('level_raw',255),('kelvin_raw',65535)):
                    if type(s[key]) is not int or not 0<=s[key]<=maximum:raise ValueError('invalid stored scene value')
        validate_project_layouts(document)
        validate_project_floorplan(document)
        validate_project_names(document)
        return document
    except (KeyError,TypeError,AttributeError) as exc:
        raise ValueError('malformed or incomplete project document') from exc


def semantic_fields(document):
    result={}
    for row in document['rows']:
        a=row['address']
        for field,p in row['parameters'].items():
            result[(a,'parameter',field)]={'state':p.get('state','value'),'value':p.get('value')}
        for scene in row['scenes']:
            value={k:scene[k] for k in ('light_type_code','level_raw','kelvin_raw')}
            # DT6 response CT bytes have no defined color meaning.
            if value['light_type_code']==0:value.pop('kelvin_raw')
            result[(a,'scene',scene['scene'])]=value
    return result


class ProjectMixin:
    def attach_project_layouts(self,project_id,layout_ids):
        return attach_project_layouts(self,project_id,layout_ids)

    def import_project_layout(self,project_id,source_layout_id):
        return import_project_layout(self,project_id,source_layout_id)

    def capture_project(self,name,channel,addresses,scenes):
        scope_values(channel,addresses,scenes)
        if type(name) is not str or not 1<=len(name.strip())<=80:raise ValueError('project name requires1..80 characters')
        with self.lock:
            g,info=self._open();ident=uuid.uuid4().hex
            report={'format':'dali-toolkit-project','schema_version':1,'project_id':ident,'name':name.strip(),
                    'gateway':info,'scope':{'channel':channel,'addresses':sorted(addresses),'scenes':sorted(scenes)},
                    'state':'reading','started_at':time.time(),'rows':[],'origin':'fresh_bus_capture',
                    'atomic_snapshot':False,'device_writes':False,
                    'not_included':['live_output','physical_ct_limits','extended_fade','device_version',
                                    'PC_labels','PC_project_format','gateway_settings','physical_layouts']}
            try:
                self._save('project',report,ident)
                for address in sorted(addresses):
                    row={'channel':channel,'address':address,'parameters':{},'scenes':[]};report['rows'].append(row)
                    typ=None
                    for field in Gateway.PARAMETER_FIELDS:
                        report['pending']={'address':address,'kind':'parameter','field':field}
                        self._save('project',report,ident)
                        p=g.read_lamp_parameter(channel,address,field,typ);p['captured_at']=time.time()
                        row['parameters'][field]=p
                        if field=='light_type':typ=p['value']['code']
                        self._save('project',report,ident)
                    for scene in sorted(scenes):
                        report['pending']={'address':address,'kind':'scene','scene':scene}
                        self._save('project',report,ident)
                        value=g.read_scene(channel,address,scene,typ);value['captured_at']=time.time()
                        row['scenes'].append(value);self._save('project',report,ident)
                    report.pop('pending',None)
                report.update(state='completed',completed_at=time.time())
                report['digest']=digest(report);validate_document(report)
            except (Exception,KeyboardInterrupt) as exc:
                report.update(state='stopped_partial',error=str(exc) or type(exc).__name__,stopped_at=time.time())
                report.pop('digest',None)
            finally:
                try:self._save('project',report,ident)
                finally:g.close()
            return report

    def get_project(self,project_id=None):
        # Atomic files permit inspection while a capture owns the bus lock.
        if project_id is None:
            paths=sorted(self.state_dir.glob('project-*.json'),key=lambda p:p.stat().st_mtime,reverse=True)
            report={'projects':[],'invalid_records':[],'total_records':len(paths),'truncated':len(paths)>50}
            for path in paths[:50]:
                ident=path.stem.removeprefix('project-')
                try:
                    data=self.get_project(ident)
                    report['projects'].append({k:data.get(k) for k in ('project_id','name','scope','state','started_at','pending','origin')})
                except (ValueError,OSError) as exc:report['invalid_records'].append({'project_id':ident,'error':str(exc)})
            return report
        if type(project_id) is not str or not re.fullmatch('[0-9a-f]{32}',project_id):raise ValueError('invalid project_id')
        data=json.loads((self.state_dir/f'project-{project_id}.json').read_text(encoding='utf-8'))
        if not isinstance(data,dict) or data.get('project_id')!=project_id:raise ValueError('invalid project record')
        if data.get('state')=='completed':validate_document(data)
        return data

    def import_project(self,document):
        data=json.loads(json.dumps(validate_document(document),allow_nan=False))
        with self.lock:
            ident=uuid.uuid4().hex
            data.update(source_project_id=data['project_id'],source_digest=data['digest'],
                        project_id=ident,origin='imported_unverified_provenance',imported_at=time.time())
            data['digest']=digest(data);self._save('project',data,ident)
            return {'project_id':ident,'source_project_id':data['source_project_id'],'state':'imported',
                    'device_writes':False,'notice':'Checksum checks accidental corruption, not authenticity. Import does not restore any device.'}

    def diff_projects(self,before_id,after_id):
        before=validate_document(self.get_project(before_id));after=validate_document(self.get_project(after_id))
        if identity(before['gateway'])!=identity(after['gateway']) or before['scope']['channel']!=after['scope']['channel']:
            raise ValueError('comparison requires same gateway identity, firmware, IP and channel')
        left=semantic_fields(before);right=semantic_fields(after)
        rows=[];same=0
        for key in sorted(set(left)|set(right)):
            if key in left and key in right and left[key]==right[key]:same+=1;continue
            rows.append({'address':key[0],'kind':key[1],'field':key[2],
                         'state':'not_captured_before' if key not in left else 'not_captured_after' if key not in right else 'changed',
                         'before':left.get(key),'after':right.get(key)})
        return {'before_id':before_id,'after_id':after_id,'channel':before['scope']['channel'],
                'same_scope':all(set(before['scope'][k])==set(after['scope'][k]) for k in ('addresses','scenes')),'unchanged_fields':same,'differences':rows,
                'comparison':'exact stored values; read timestamps/reply envelopes ignored; no live reads or writes',
                'device_writes':False,'layout_comparison':compare_layouts(before,after),
                'native_floorplan_comparison':compare_floorplans(before,after),
                'native_name_comparison':compare_names(before,after)}

    def preview_project_scene_restore(self,project_id,addresses,scenes):
        document=validate_document(self.get_project(project_id));scope=document['scope'];channel=scope['channel']
        scope_values(channel,addresses,scenes)
        if len(addresses)*len(scenes)>64:raise ValueError('restore preview supports1..64 explicit pairs; split larger scopes')
        if not set(addresses)<=set(scope['addresses']) or not set(scenes)<=set(scope['scenes']):raise ValueError('requested targets were not captured')
        configured=(self.config['gateway_ip'],self.config['expected_dali_mac'].lower(),self.config['expected_firmware'])
        if identity(document['gateway'])!=configured:raise ValueError('project belongs to a different configured gateway')
        changes=[]
        for row in document['rows']:
            if row['address'] not in addresses:continue
            for s in row['scenes']:
                if s['scene'] not in scenes:continue
                if s['light_type_code']!=1:raise ValueError('scene restore currently supports DT8 only')
                if s['kelvin_raw']!=65535 and not 1000<=s['kelvin_raw']<=10000:raise ValueError('unsupported stored Kelvin cannot be restored')
                changes.append({k:s[k] for k in ('channel','address','scene','level_raw','kelvin_raw')})
        with self.lock:
            capabilities=self.read_profiles(channel,sorted(addresses))
            profiles={p['address']:p for p in capabilities['rows']}
            limits={p['address']:p for p in capabilities['level_limits_rows']}
            for c in changes:
                p=profiles[c['address']];bounds=limits[c['address']]
                if p['light_type_code']!=1:raise ValueError('current fixture type differs from restorable DT8')
                if c['kelvin_raw']!=65535:
                    if not 0<p['min_kelvin_raw']<=p['max_kelvin_raw']<65535:raise ValueError('current Kelvin limits are unknown or invalid')
                    if not p['min_kelvin_raw']<=c['kelvin_raw']<=p['max_kelvin_raw']:raise ValueError('stored Kelvin outside current fixture limits')
                if c['level_raw'] not in (0,255):
                    if not 1<=bounds['min_level_raw']<=bounds['max_level_raw']<=254:raise ValueError('current brightness limits are unknown or invalid')
                    if not bounds['min_level_raw']<=c['level_raw']<=bounds['max_level_raw']:raise ValueError('stored brightness outside current fixture limits')
            plan=self.preview(changes)
            plan.update(source_project_id=project_id,source_project_digest=document['digest'],restore_scope='selected stored scenes only',
                        notice='Read-only plan. Applying requires explicit user restore authorization and existing scoped write gate. No parameters/groups/addressing/live output restored.')
            self._save('plan',{k:v for k,v in plan.items() if k!='confirmation'},plan['plan_id'])
            return plan
