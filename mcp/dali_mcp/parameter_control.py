"""Single-field parameter facade; paired power fields preserve omitted color."""
from .parameter_execution import ExperimentalParameterController
from .parameter_planning import original_values, validate_values
from .projects import identity, validate_document


class LampParameterController(ExperimentalParameterController):
    FIELDS = frozenset(('fade_time', 'fade_rate', 'min_level', 'max_level',
                        'configured_min_ct', 'configured_max_ct', 'power_on', 'system_failure', 'dimming_curve'))
    PAIRED = frozenset(('power_on', 'system_failure'))

    @classmethod
    def _values(cls, field, value):
        if type(field) is not str or field not in cls.FIELDS:
            raise ValueError('unsupported lamp parameter write field')
        key = cls._key(field)
        low, high = ((0,1) if key=='raw' else (1,15) if key=='code' else (1000,10000) if key=='kelvin_raw'
                     else (0,254) if field in cls.PAIRED else (1,254))
        if type(value) is not int or not low <= value <= high:
            raise ValueError(f'{field} requires raw integer{low}..{high}')
        return {key: value}

    @staticmethod
    def _key(field):
        return 'raw' if field=='dimming_curve' else 'code' if field.startswith('fade_') else 'kelvin_raw' if field.startswith('configured_') else 'level_raw'

    @classmethod
    def _paired_values(cls, field, values):
        if type(field) is not str or field not in cls.PAIRED:
            raise ValueError('paired parameter requires power_on or system_failure')
        validate_values(field, values, partial=True)
        if values.get('level_raw') == 255:
            raise ValueError('paired raw brightness255 is not supported by this facade')
        return dict(values)

    @classmethod
    def _validate_scope(cls, snapshot, changes):
        if not isinstance(changes, list) or len(changes) != 1:
            raise ValueError('lamp-parameter MCP tools require exactly one field')
        change = changes[0]
        field = change['field']
        if type(field) is not str or field not in cls.FIELDS:
            raise ValueError('unsupported lamp parameter write field')
        source = original_values(snapshot['parameters'])[field]
        if field in cls.PAIRED:
            target = cls._paired_values(field, change['values'])
            cls._paired_values(field, source)
            # A full paired value is sent on recovery. Reject originals that the
            # configured-range planner could not restore, even for level-only edits.
            originals = original_values(snapshot['parameters'])
            if source['kelvin_raw'] != 65535 and not (
                originals['configured_min_ct']['kelvin_raw'] <= source['kelvin_raw'] <=
                originals['configured_max_ct']['kelvin_raw']):
                raise ValueError('original paired color outside configured limits is not restorable')
        else:
            key = cls._key(field)
            target = cls._values(field, change['values'].get(key))
            cls._values(field, source.get(key))
        if change != {'field': field, 'values': target}:
            raise ValueError('unexpected fields in lamp parameter change')

    def _project_changes(self, project_id, address, fields):
        if type(address) is not int or not 0 <= address <= 63:
            raise ValueError('explicit short address0..63 required')
        if (not isinstance(fields, list) or not 1 <= len(fields) <= 9 or
            any(type(f) is not str or f not in self.FIELDS for f in fields) or len(set(fields)) != len(fields)):
            raise ValueError('select1..9 unique supported parameter fields')
        document = validate_document(self.service.get_project(project_id))
        if address not in document['scope']['addresses']:
            raise ValueError('requested address was not captured')
        cfg = self.service.config
        configured = (cfg['gateway_ip'], cfg['expected_dali_mac'].lower(), cfg['expected_firmware'])
        if identity(document['gateway']) != configured:
            raise ValueError('project belongs to a different configured gateway')
        row = next(r for r in document['rows'] if r['address'] == address)
        values = original_values(row['parameters'])
        changes = [{'field': f, 'values': values[f]} for f in fields]
        for change in changes:
            self._validate_scope(row, [change])
        source = {'project_id': project_id, 'digest': document['digest'], 'fields': list(fields)}
        return document, source, changes

    def _prepare(self, info, snapshot, changes, restore_of=None, reference_ct_limits=None, source_project=None, source_workbook=None, native_workbook_review=None):
        if restore_of is not None:
            parent=self._load(restore_of)
            source_project = parent.get('source_project')
            source_workbook = parent.get('source_workbook')
            native_workbook_review = parent.get('native_workbook_review')
        if source_project is None and source_workbook is None:
            self._validate_scope(snapshot, changes)
        else:
            for change in changes:
                self._validate_scope(snapshot, [change])
        return super()._prepare(info, snapshot, changes, restore_of, reference_ct_limits, source_project, source_workbook, native_workbook_review)

    def _load(self, ident, _seen=None):
        plan = super()._load(ident, _seen)
        if 'source_workbook' in plan:
            from .native_workbook_parameters import NativeWorkbookParameters
            NativeWorkbookParameters(self.service).validate_plan(plan)
            for change in plan['changes']:
                self._validate_scope(plan['original'],[change])
            return plan
        source = plan.get('source_project')
        if source is None:
            self._validate_scope(plan['original'], plan['changes'])
        else:
            if not isinstance(source, dict) or set(source) != {'project_id', 'digest', 'fields'}:
                raise ValueError('invalid source project binding')
            document, expected_source, changes = self._project_changes(source['project_id'], plan['address'], source['fields'])
            if (source != expected_source or document['scope']['channel'] != plan['channel'] or
                identity(document['gateway']) != identity(plan['gateway'])):
                raise ValueError('source project changed or belongs to a different target')
            if plan['restore_of'] is None and plan['changes'] != changes:
                raise ValueError('project parameter targets differ from captured fields')
            if any(p['field'] not in source['fields'] for p in plan['changes']):
                raise ValueError('parameter recovery exceeds selected project fields')
            for change in plan['changes']:
                self._validate_scope(plan['original'], [change])
        return plan

    def prepare_project(self, project_id, address, fields):
        """Prepare selected captured fields for one lamp using fresh current originals."""
        with self._locked():
            document, source, changes = self._project_changes(project_id, address, fields)
            g, info = self.service._open()
            try:
                if identity(info) != identity(document['gateway']):
                    raise ValueError('connected gateway differs from source project')
                snapshot = self._snapshot(g, document['scope']['channel'], address)
                return self._prepare(info, snapshot, changes, source_project=source)
            finally:
                g.close()

    def prepare_parameter(self, channel, address, field, value):
        values = self._values(field, value)  # Reject unsupported fields before opening a gateway.
        return super().prepare(channel, address, [{'field': field, 'values': values}])

    def prepare_power_parameter(self, channel, address, field, *, level_raw=None, kelvin_raw=None):
        values = {k: v for k, v in dict(level_raw=level_raw, kelvin_raw=kelvin_raw).items() if v is not None}
        values = self._paired_values(field, values)
        return super().prepare(channel, address, [{'field': field, 'values': values}])
