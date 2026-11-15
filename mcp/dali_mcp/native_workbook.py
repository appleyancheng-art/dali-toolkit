"""Read ConfigMaster's observed nine-sheet XLSX interchange format.

This is a format decoder, not a bus backup or an executable restoration plan.
Keep native percentages (which lose raw-level precision), blank semantics and
duplicate group scene values explicit for the restoration planner to resolve.
No file writes, networking, formula evaluation or external relationship access.
"""
import hashlib
import io
import math
import posixpath
import re
import xml.etree.ElementTree as ET
import zipfile


NS = {'s': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
REL_ID = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id'
PREFIXES = ('通道一', '通道二')
SUFFIXES = ('参数', '分组', 'ECG场景', '组场景')
PARAM_HEADERS = ['短地址', '地址类型', '名称', '渐变时间', '渐变速率', '最小亮度',
                 '最大亮度', '上电亮度', '上电色温', '系统故障亮度', '系统故障色温',
                 '调光曲线', '最大色温', '最小色温']


def column(index):
    value = ''
    while index:
        index, rem = divmod(index - 1, 26)
        value = chr(65 + rem) + value
    return value


def _xml(data):
    # DTD/entity expansion is unnecessary in this format. Scan after decoding
    # so UTF-16 declarations cannot evade the guard.
    if data.startswith((b'\xff\xfe', b'\xfe\xff')):
        text = data.decode('utf-16')
    else:
        text = data.decode('utf-8-sig')
    if '\0' in text or '<!DOCTYPE' in text.upper() or '<!ENTITY' in text.upper():
        raise ValueError('DTD/entities are not supported in native workbooks')
    return ET.fromstring(text)


def _sheets(data):
    if not isinstance(data, bytes) or not 0 < len(data) <= 2_000_000:
        raise ValueError('XLSX must contain1..2000000 bytes')
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            names = [entry.filename for entry in entries]
            if len(entries) > 200 or len(set(names)) != len(names):
                raise ValueError('too many or duplicate ZIP entries')
            if sum(e.file_size for e in entries) > 20_000_000 or any(e.file_size > 8_000_000 for e in entries):
                raise ValueError('expanded XLSX exceeds size limits')
            if any(e.flag_bits & 1 for e in entries):
                raise ValueError('encrypted XLSX not supported')
            if any(n.startswith('/') or '\\' in n or '..' in n.split('/') for n in names):
                raise ValueError('unsafe ZIP member path')
            # Never silently interpret a macro/external-data workbook as an
            # ordinary native export. Relationship files are small and bounded.
            if any('vbaproject' in n.lower() or 'externallinks/' in n.lower() for n in names):
                raise ValueError('macros/external links not supported')
            for name in names:
                if name.endswith('.rels'):
                    if any(r.attrib.get('TargetMode') == 'External' for r in _xml(archive.read(name))):
                        raise ValueError('external relationships not supported')
            relations = {}
            for rel in _xml(archive.read('xl/_rels/workbook.xml.rels')):
                key = rel.attrib['Id']
                if key in relations:
                    raise ValueError('duplicate workbook relationship')
                target = rel.attrib['Target']
                target = target.lstrip('/') if target.startswith('/') else posixpath.normpath('xl/' + target)
                if not target.startswith('xl/worksheets/'):
                    continue
                relations[key] = target
            shared = []
            if 'xl/sharedStrings.xml' in names:
                shared = [''.join(t.text or '' for t in si.iter('{'+NS['s']+'}t'))
                          for si in _xml(archive.read('xl/sharedStrings.xml'))]
            book = _xml(archive.read('xl/workbook.xml'))
            result = {}
            total = 0
            for sheet in book.findall('s:sheets/s:sheet', NS):
                name = sheet.attrib['name']
                if name in result:
                    raise ValueError('duplicate sheet name')
                root = _xml(archive.read(relations[sheet.attrib[REL_ID]]))
                cells = {}
                for cell in root.findall('s:sheetData/s:row/s:c', NS):
                    total += 1
                    if total > 100_000:
                        raise ValueError('too many workbook cells')
                    reference = cell.attrib['r']
                    match = re.fullmatch(r'([A-Z]{1,2})([1-9][0-9]{0,3})', reference)
                    if not match or int(match[2]) > 2000 or reference in cells:
                        raise ValueError('invalid or duplicate cell reference')
                    if cell.find('s:f', NS) is not None:
                        raise ValueError('formula cells cannot be used as device settings')
                    kind = cell.attrib.get('t', 'n')
                    value = cell.findtext('s:v', None, NS)
                    if kind == 's':
                        if value is None or not re.fullmatch(r'0|[1-9][0-9]*', value) or int(value) >= len(shared):
                            raise ValueError('invalid shared string index')
                        value = shared[int(value)]
                    elif kind == 'inlineStr':
                        value = ''.join(t.text or '' for t in cell.findall('s:is//s:t', NS))
                    elif kind != 'n':
                        raise ValueError('unsupported cell type')
                    if value is not None and len(value) > 4096:
                        raise ValueError('cell text exceeds limit')
                    cells[reference] = value
                result[name] = cells
            expected = {p + s for p in PREFIXES for s in SUFFIXES} | {'网关信息'}
            if set(result) != expected:
                raise ValueError('expected the observed nine ConfigMaster sheets')
            return result
    except (zipfile.BadZipFile, KeyError, IndexError, ET.ParseError, UnicodeError) as exc:
        raise ValueError('malformed ConfigMaster XLSX') from exc


def _value(cells, col, row):
    return cells.get(f'{column(col)}{row}')


def _headers(cells, row, expected):
    if [_value(cells, i, row) for i in range(1, len(expected) + 1)] != expected:
        raise ValueError(f'unexpected native header at row{row}')


def _index(value, prefix, maximum):
    match = re.fullmatch(prefix + r'(0|[1-9][0-9]*)', value or '')
    if not match or int(match[1]) > maximum:
        raise ValueError(f'invalid {prefix} index')
    return int(match[1])


def _numeric(value, maximum, representation, blank, mask=False):
    if value in (None, ''):
        return {'kind': blank}
    if value == 'MASK':
        if not mask:
            raise ValueError('MASK not valid for this parameter')
        return {'kind': 'mask'}
    try:
        number = float(value)
    except (ValueError, TypeError) as exc:
        raise ValueError('invalid numeric native field') from exc
    if not math.isfinite(number) or not 0 <= number <= maximum:
        raise ValueError('native value outside supported range')
    if representation != 'percent':
        if not number.is_integer():
            raise ValueError('native code/kelvin must be integer')
        number = int(number)
    return {'kind': 'value', representation: number}


def _scene(cells, row, scene):
    return {'scene': scene,
            'brightness': _numeric(_value(cells, 4 + 2*scene, row), 100, 'percent', 'mask', True),
            'color': _numeric(_value(cells, 5 + 2*scene, row), 65534, 'kelvin', 'mask', True)}


def _address_rows(cells, start):
    result = {}
    seen = set()
    for row in range(start, 2001):
        label = _value(cells, 1, row)
        if label in (None, ''):
            continue
        address = _index(label, 'A', 63)
        if address in seen:
            raise ValueError('duplicate address in native sheet')
        seen.add(address)
        typ = _value(cells, 2, row)
        if typ in (None, ''):
            if any(_value(cells, col, row) not in (None, '') for col in range(3, 36)):
                raise ValueError('data in an address row with missing fixture type')
            continue  # Export includes 64 placeholder rows even on empty channels.
        if typ not in ('DT6', 'DT8'):
            raise ValueError('unknown native fixture type')
        result[address] = (row, typ)
    return result


def inspect_workbook(data):
    sheets = _sheets(data)
    info = sheets['网关信息']
    _headers(info, 1, ['字段', '值'])
    gateway = {_value(info, 1, r): _value(info, 2, r) for r in range(2, 5)}
    if set(gateway) != {'网关名称', 'DALI MAC', 'KNX MAC'}:
        raise ValueError('unexpected native gateway information fields')
    mac = gateway.get('DALI MAC')
    if not isinstance(mac, str) or not re.fullmatch(r'(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}', mac):
        raise ValueError('native DALI MAC required')
    channels = []
    for channel, prefix in enumerate(PREFIXES, 1):
        params, groups, scenes, group_scenes = (sheets[prefix+s] for s in SUFFIXES)
        _headers(params, 1, PARAM_HEADERS)
        _headers(groups, 2, ['短地址', '地址类型', '名称'])
        _headers(scenes, 2, ['短地址', '地址类型', '名称'] + ['亮度', '色温']*16)
        if [_value(groups, col, 1) for col in range(4, 20)] != [f'G{g}' for g in range(16)]:
            raise ValueError('group header numbering mismatch')
        if [_value(scenes, 4+2*s, 1) for s in range(16)] != [f'场景{s}' for s in range(16)]:
            raise ValueError('scene header numbering mismatch')
        tables = [_address_rows(params, 2), _address_rows(groups, 3), _address_rows(scenes, 3)]
        if any({a:t for a,(_,t) in table.items()} != {a:t for a,(_,t) in tables[0].items()} for table in tables[1:]):
            raise ValueError('address/type coverage differs between native sheets')
        fixtures = []
        by_address = {}
        for address, (row, typ) in sorted(tables[0].items()):
            parameter_values = {}
            for field, col, maximum, representation, allow_mask in (
                ('fade_time',4,15,'code',False), ('fade_rate',5,15,'code',False),
                ('min_level',6,100,'percent',False), ('max_level',7,100,'percent',False),
                ('power_on_level',8,100,'percent',True), ('power_on_ct',9,65534,'kelvin',True),
                ('system_failure_level',10,100,'percent',True), ('system_failure_ct',11,65534,'kelvin',True),
                ('configured_max_ct',13,65534,'kelvin',False), ('configured_min_ct',14,65534,'kelvin',False)):
                parameter_values[field] = _numeric(_value(params,col,row),maximum,representation,'keep',allow_mask)
            curve = _value(params,12,row)
            if curve not in (None,'','对数','线性'):
                raise ValueError('unknown dimming curve label')
            parameter_values['dimming_curve'] = {'kind':'keep'} if curve in (None,'') else {'kind':'value','label':curve}
            group_row = tables[1][address][0]
            membership = [_value(groups,4+g,group_row) for g in range(16)]
            if any(v not in (None,'','√') for v in membership):
                raise ValueError('unknown native group membership marker')
            fixture = {'address':address,'light_type':typ,'name':_value(params,3,row) or '',
                       'parameters':parameter_values,'groups':[g for g,v in enumerate(membership) if v=='√'],
                       'scenes':[_scene(scenes,tables[2][address][0],s) for s in range(16)]}
            fixtures.append(fixture)
            by_address[address] = fixture
        blocks = []
        block = None
        conflicts = []
        previous_group_scenes = {}
        for row in range(1,2001):
            label = _value(group_scenes,1,row)
            if block is not None and row in (block_start+1,block_start+2):
                continue
            if label in (None,''):
                continue
            if label.startswith('G'):
                group = _index(label,'G',15)
                if any(b['group']==group for b in blocks):
                    raise ValueError('duplicate native group scene block')
                _headers(group_scenes,row+2,['短地址','地址类型','名称']+['亮度','色温']*16)
                names = {}
                for scene in range(16):
                    heading = _value(group_scenes,4+2*scene,row) or ''
                    stem = f'场景{scene}'
                    if heading != stem and not heading.startswith(stem+'-'):
                        raise ValueError('group scene header numbering mismatch')
                    names[str(scene)] = heading[len(stem)+1:] if heading != stem else ''
                block = {'group':group,'name':_value(group_scenes,1,row+1) or '', 'scene_names':names,'rows':[]}
                blocks.append(block)
                block_start = row
            else:
                address = _index(label,'A',63)
                if block is None or address not in by_address or any(r['address']==address for r in block['rows']):
                    raise ValueError('unknown or duplicate address in group scene block')
                if _value(group_scenes,2,row) != by_address[address]['light_type']:
                    raise ValueError('group scene type mismatch')
                values = [_scene(group_scenes,row,s) for s in range(16)]
                block['rows'].append({'address':address,'scenes':values})
                for value in values:
                    scene = value['scene']; key = (address,scene)
                    if value != by_address[address]['scenes'][scene]:
                        conflicts.append({'kind':'ecg_vs_group','address':address,'scene':scene,'group':block['group']})
                    if key in previous_group_scenes and previous_group_scenes[key][1] != value:
                        conflicts.append({'kind':'group_vs_group','address':address,'scene':scene,
                                          'earlier_group':previous_group_scenes[key][0],'later_group':block['group']})
                    previous_group_scenes[key] = (block['group'],value)
        if sorted(b['group'] for b in blocks) != list(range(16)):
            raise ValueError('expected all16 native group scene blocks')
        channels.append({'channel':channel,'fixtures':fixtures,'group_names':{str(g):_value(groups,4+g,2) or '' for g in range(16)},
                         'group_scene_blocks':blocks,'scene_conflicts':conflicts})
    return {'format':'configmaster-workbook-evidence-v1','source_sha256':hashlib.sha256(data).hexdigest(),
            'native_format':'observed ConfigMaster1.2.4.1 nine-sheet XLSX',
            'gateway_info':gateway,'dali_mac':mac.lower(),'channels':channels,
            'device_values_verified':False,'online_addresses_verified':False,'executable':False,
            'warnings':['File does not prove local-cache versus fresh-bus provenance.',
                        'Native gateway information has no IP or firmware binding; fresh configured gateway identity verification is still required for restoration.',
                        'Brightness percentages do not preserve exact raw levels; do not use mathematical conversion as native round-trip proof.',
                        'Blank scene components mean MASK; blank ordinary parameters mean keep.',
                        'ECG and group scene values are retained separately. Resolve conflicts before any device write.',
                        'No native file, device, scene, group or parameter is modified by this decoder.']}
