"""Offline parameter-write review. No network, apply method, or MCP registration.

Payload layouts are derived from ConfigMaster callers and message wrappers.
Physical write/receipt behavior remains unvalidated; this is not an executor.
"""
import copy
from pydantic import BaseModel, ConfigDict, StrictInt
from .projects import validate_parameter
from .protocol import Gateway


COMMANDS={
    'dimming_curve':(0xf024,0xe3),
    'fade_time':(0xf018,0x2e),'fade_rate':(0xf019,0x2f),
    'min_level':(0xf01c,0x2b),'max_level':(0xf01e,0x2a),
    'power_on':(0xf020,1),'system_failure':(0xf022,1),
    'configured_max_ct':(0xf026,0xf2),'configured_min_ct':(0xf028,0xf2),
}


class ParameterPatch(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    field:str
    values:dict[str,StrictInt]


def validate_values(field,values,partial=False):
    if field not in COMMANDS:raise ValueError('unsupported parameter field')
    expected=({'raw'} if field=='dimming_curve' else {'code'} if field.startswith('fade_') else {'kelvin_raw'} if field.startswith('configured_')
              else {'level_raw','kelvin_raw'} if field in ('power_on','system_failure') else {'level_raw'})
    if (not isinstance(values,dict) or not values or not set(values)<=expected or
        (not partial and set(values)!=expected)):raise ValueError(f'invalid values for {field}')
    for key,value in values.items():
        if type(value) is not int:raise ValueError('parameter values must be strict integers')
        if key=='raw' and value not in (0,1):raise ValueError('curve raw requires0 logarithmic or1 linear')
        if key=='code' and not 0<=value<=15:raise ValueError('fade code must be0..15; not seconds')
        if key=='level_raw':
            low,high=(0,255) if field in ('power_on','system_failure') else (1,254)
            if not low<=value<=high:raise ValueError(f'{field} raw level requires{low}..{high}')
        if key=='kelvin_raw' and not (1000<=value<=10000 or field in ('power_on','system_failure') and value==65535):
            raise ValueError('Kelvin requires1000..10000;65535 only for paired power/failure fields')


def encode_parameter_candidate(channel,address,field,values):
    if type(channel) is not int or channel not in (1,2) or type(address) is not int or not 0<=address<=63:
        raise ValueError('explicit channel1/2 and short address0..63 required')
    validate_values(field,values)
    command,selector=COMMANDS[field]
    payload=bytearray([channel,1,address,selector,0,0,0,0])
    if field=='dimming_curve':payload[4]=values['raw']
    elif field.startswith('fade_'):payload[4]=values['code']
    elif field.startswith('configured_'):payload[4:6]=values['kelvin_raw'].to_bytes(2,'little')
    else:
        payload[4]=values['level_raw']
        if field in ('power_on','system_failure'):payload[5:7]=values['kelvin_raw'].to_bytes(2,'little')
    return {'field':field,'command':f'{command:04X}','payload_hex':payload.hex(),
            'declared_length_including_crc':12,'addressing_mode':'single_short_address',
            'readback_field':'fade_time_rate' if field.startswith('fade_') else field}


def original_values(parameters):
    if not isinstance(parameters,dict) or set(parameters)!=set(Gateway.PARAMETER_FIELDS):raise ValueError('complete supported parameter snapshot required')
    try:
        typ=parameters['light_type']['value']['code']
        if type(typ) is not int or typ!=1:raise ValueError('offline write planning currently supports DT8 only')
        for field,result in parameters.items():validate_parameter(field,result,typ)
        result={f:copy.deepcopy(parameters[f]['value']) for f in COMMANDS if not f.startswith('fade_')}
        fade=parameters['fade_time_rate']['value']
        result.update(fade_time={'code':fade['fade_time_code']},fade_rate={'code':fade['fade_rate_code']})
        return result
    except (KeyError,TypeError) as exc:raise ValueError('invalid parameter evidence') from exc


def valid_limits(values):
    return (1<=values['min_level']['level_raw']<=values['max_level']['level_raw']<=254 and
            1000<=values['configured_min_ct']['kelvin_raw']<=values['configured_max_ct']['kelvin_raw']<=10000)


def plan_parameter_candidates(channel,address,parameter_row,changes,*,reference_ct_limits=None):
    # Scope validation must also occur when all changes are unchanged.
    if type(channel) is not int or channel not in (1,2) or type(address) is not int or not 0<=address<=63:
        raise ValueError('explicit channel1/2 and short address0..63 required')
    if (not isinstance(parameter_row,dict) or type(parameter_row.get('channel')) is not int or
        type(parameter_row.get('address')) is not int or parameter_row['channel']!=channel or parameter_row['address']!=address):
        raise ValueError('parameter evidence belongs to a different channel/address')
    parameters=parameter_row.get('parameters')
    if not isinstance(changes,list) or not 1<=len(changes)<=9:raise ValueError('provide1..9 explicit parameter patches')
    patches=[ParameterPatch.model_validate(p).model_dump() for p in changes]
    if len({p['field'] for p in patches})!=len(patches):raise ValueError('duplicate parameter field')
    before=original_values(parameters)
    if not valid_limits(before):raise ValueError('original configured limits are invalid or unsupported')
    target=copy.deepcopy(before);requested={p['field']:p['values'] for p in patches}
    for field,values in requested.items():
        validate_values(field,values,partial=True)
        target[field].update(values);validate_values(field,target[field])
        validate_values(field,before[field])  # Never propose a change without encodable original.
    if not valid_limits(target):raise ValueError('target minimum/maximum ranges conflict')
    reference_ct_limits=reference_ct_limits or (before['configured_min_ct']['kelvin_raw'],before['configured_max_ct']['kelvin_raw'])
    if (len(reference_ct_limits)!=2 or any(type(v) is not int for v in reference_ct_limits) or
        not 1000<=reference_ct_limits[0]<=reference_ct_limits[1]<=10000):raise ValueError('invalid CT reference limits')
    if (target['configured_min_ct']['kelvin_raw']<reference_ct_limits[0] or
        target['configured_max_ct']['kelvin_raw']>reference_ct_limits[1]):
        raise ValueError('expanding CT limits requires physical-limit evidence, which is not implemented')
    for field in ('power_on','system_failure'):
        if 'kelvin_raw' in requested.get(field,{}) and target[field]['kelvin_raw']!=65535:
            if not target['configured_min_ct']['kelvin_raw']<=target[field]['kelvin_raw']<=target['configured_max_ct']['kelvin_raw']:
                raise ValueError('requested power/failure Kelvin outside target configured limits')
    pending=[p['field'] for p in patches if before[p['field']]!=target[p['field']]]
    unchanged=[p['field'] for p in patches if before[p['field']]==target[p['field']]]
    state=copy.deepcopy(before);ordered=[]
    while pending:
        for field in pending:
            proposed=dict(state);proposed[field]=target[field]
            if valid_limits(proposed):break
        else:raise ValueError('no valid transition order without adding unrequested changes')
        ordered.append(field);state[field]=target[field];pending.remove(field)
    forward=[];restore=[]
    for field in ordered:
        forward.append(dict(encode_parameter_candidate(channel,address,field,target[field]),
                            before=before[field],after=target[field],requested=requested[field]))
    for field in reversed(ordered):
        restore.append(dict(encode_parameter_candidate(channel,address,field,before[field]),target=before[field]))
    return {'state':'offline_review_only','channel':channel,'address':address,
            'source_parameters':copy.deepcopy(parameters),'steps':forward,'restore_candidates':restore,
            'unchanged_fields':unchanged,'device_writes':False,'write_execution_available':False,
            'limitations':['not a live authorization or executable plan','write receipt behavior has not been physically validated',
                          'refresh originals and live output before an authorized physical test',
                          'min/max changes can affect live output; power/failure behavior requires separate testing',
                          'fade code0/255-level/65535-K values remain raw codes; no unsupported behavior interpretation',
                          'restoration is conditional on actual readback, never an automatic blind replay']}
