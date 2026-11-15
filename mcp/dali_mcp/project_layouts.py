"""Self-contained project layout evidence, separate from active local mappings."""
import json
import re
import time
import uuid
from .authoring import AuthoringMixin, validate_grid

CONTEXT = 'source_snapshots_not_current_installation_verification'


def validate_project_layouts(document):
    """Validate embedded geometry and binding without consulting local state/network."""
    if document['schema_version'] == 1 or (document['schema_version'] in (3,4) and 'layouts' not in document):
        if 'layouts' in document or 'layout_context' in document:
            raise ValueError('embedded layouts require project schema_version2')
        return
    layouts = document.get('layouts')
    if not isinstance(layouts, list) or not 1 <= len(layouts) <= 16:
        raise ValueError('schema2 requires1..16 explicit layout snapshots')
    if document.get('layout_context') != CONTEXT:
        raise ValueError('project layouts are source evidence, not current verification')
    g = document['gateway']
    binding = {'gateway_ip': g['ip'], 'expected_dali_mac': g['dali_mac'],
               'expected_firmware': g['dali_firmware']}
    seen = set()
    for layout in layouts:
        if not isinstance(layout, dict):raise ValueError('invalid embedded layout')
        ident = layout.get('layout_id')
        if type(ident) is not str or not re.fullmatch('[0-9a-f]{32}', ident) or ident in seen:
            raise ValueError('invalid or duplicate embedded layout_id')
        seen.add(ident)
        if type(layout.get('schema_version')) is not int or layout['schema_version'] != 1:
            raise ValueError('unsupported layout schema')
        if layout.get('digest') != AuthoringMixin._layout_digest(layout):
            raise ValueError('embedded layout checksum mismatch')
        addresses = validate_grid(layout['channel'], layout['name'], layout['address_grid'])
        if (layout['addresses'] != addresses or type(layout['rows']) is not int or
                type(layout['columns']) is not int or layout['rows'] != len(layout['address_grid']) or
                layout['columns'] != len(layout['address_grid'][0])):
            raise ValueError('inconsistent embedded layout dimensions')
        lb = layout['gateway_binding']
        if (not isinstance(lb, dict) or lb.get('gateway_ip') != binding['gateway_ip'] or
                str(lb.get('expected_dali_mac', '')).lower() != binding['expected_dali_mac'].lower() or
                lb.get('expected_firmware') != binding['expected_firmware']):
            raise ValueError('embedded layout gateway differs from project')
        if layout['channel'] != document['scope']['channel'] or not set(addresses) <= set(document['scope']['addresses']):
            raise ValueError('layout includes channel/addresses not captured by project')
        if type(layout.get('physical_mapping_verified')) is not bool:
            raise ValueError('source layout verification claim must be boolean')


def attach_project_layouts(service, project_id, layout_ids):
    from .projects import validate_document, digest, identity
    if (not isinstance(layout_ids, list) or not 1 <= len(layout_ids) <= 16 or
            any(type(v) is not str or not re.fullmatch('[0-9a-f]{32}', v) for v in layout_ids) or
            len(set(layout_ids)) != len(layout_ids)):
        raise ValueError('provide1..16 unique local layout IDs')
    with service.lock:
        source = validate_document(service.get_project(project_id))
        configured = (service.config['gateway_ip'], service.config['expected_dali_mac'].lower(),
                      service.config['expected_firmware'])
        if identity(source['gateway']) != configured:
            raise ValueError('attach layouts only to this configured gateway')
        data = json.loads(json.dumps(source, allow_nan=False))
        # One immutable new project file: no existing layout or project is overwritten.
        data.update(schema_version=4 if 'native_names' in source else 3 if 'native_floorplan' in source else 2, project_id=uuid.uuid4().hex,
                    origin='derived_project_with_layout_evidence',
                    parent_project={'project_id': source['project_id'], 'digest': source['digest']},
                    layouts=[service._load_layout(ident) for ident in layout_ids],
                    layout_context=CONTEXT, layouts_attached_at=time.time())
        data['not_included'] = [v for v in data.get('not_included', []) if v != 'physical_layouts']
        data['digest'] = digest(data)
        validate_document(data)  # Check the whole bundle before creating any file.
        service._save('project', data, data['project_id'])
        return data


def import_project_layout(service, project_id, source_layout_id):
    from .projects import validate_document
    if type(source_layout_id) is not str or not re.fullmatch('[0-9a-f]{32}', source_layout_id):
        raise ValueError('invalid source_layout_id')
    with service.lock:
        document = validate_document(service.get_project(project_id))
        layouts = [v for v in document.get('layouts', []) if v['layout_id'] == source_layout_id]
        if len(layouts) != 1:raise ValueError('layout not embedded in this project')
        # Existing import creates a new active ID and clears physical check claims.
        local = service.import_layout({'format': 'dali-toolkit-layout', 'version': 1, 'layout': layouts[0]})
        return {'state': 'layout_imported', 'project_id': project_id, 'source_layout_id': source_layout_id,
                'layout': local, 'device_writes': False, 'network_traffic': False,
                'notice': 'New local mapping, unverified at this installation. Original source claims remain in the project.'}


def compare_layouts(before, after):
    def summary(doc):
        if 'layouts' not in doc:return None
        rows = [{k:layout[k] for k in ('name', 'channel', 'address_grid')} for layout in doc['layouts']]
        return sorted(rows, key=lambda r:json.dumps(r, sort_keys=True, ensure_ascii=False))
    left, right = summary(before), summary(after)
    state = ('not_captured_both' if left is None and right is None else
             'not_captured_before' if left is None else 'not_captured_after' if right is None else
             'unchanged' if left == right else 'changed')
    return {'state': state, 'before': left, 'after': right,
            'comparison': 'Source names and geometry only; IDs/check timestamps ignored. Not proof of physical wiring or current verification.'}
