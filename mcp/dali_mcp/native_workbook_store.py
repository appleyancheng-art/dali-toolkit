"""Durable native XLSX evidence for subsequent, separately scoped restoration.

Never reads the bus or changes ConfigMaster. Preserve the exact source rather
than trusting a caller-supplied decoded document or silently resolving conflicts.
"""
import base64
import binascii
import json
from pathlib import Path
import re
import time
import uuid
from .native_workbook import inspect_workbook
from .native_percent import review_native_percent


def summarize(decoded):
    percentages = set()
    channels = []
    for ch in decoded['channels']:
        def visit(value):
            if isinstance(value, dict):
                if value.get('kind') == 'value' and 'percent' in value:
                    percentages.add(value['percent'])
                for child in value.values():
                    visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)
        visit(ch)
        channels.append(dict(channel=ch['channel'],
            addresses=[f['address'] for f in ch['fixtures']],
            fixture_count=len(ch['fixtures']),
            ecg_scene_count=sum(len(f['scenes']) for f in ch['fixtures']),
            scene_conflict_count=len(ch['scene_conflicts'])))
    return dict(format=decoded['format'],source_sha256=decoded['source_sha256'],
        gateway_info=decoded['gateway_info'],dali_mac=decoded['dali_mac'],channels=channels,
        percent_review=[review_native_percent(p) for p in sorted(percentages)],
        device_values_verified=False,online_addresses_verified=False,executable=False,
        native_import_runtime_verified=False,source_provenance='unverified',
        warnings=decoded['warnings'])


def decode_record(record, workbook_id):
    """Validate saved bytes/summary independently of current network configuration."""
    try:
        source=record['source_base64']
        if type(source) is not str or len(source)>2_666_668:raise ValueError('invalid stored workbook payload')
        decoded=inspect_workbook(base64.b64decode(source,validate=True))
        if record['workbook_id']!=workbook_id or record['summary']!=summarize(decoded):
            raise ValueError('workbook evidence checksum/summary mismatch')
    except (KeyError,TypeError,binascii.Error) as exc:raise ValueError('invalid stored workbook evidence') from exc
    return decoded


class NativeWorkbookStore:
    def __init__(self, service):
        self.service = service

    def _identity(self, decoded):
        if decoded['dali_mac'] != self.service.config['expected_dali_mac'].lower():
            raise ValueError('workbook DALI MAC differs from configured gateway; no bus connection attempted')

    def import_file(self, xlsx_path):
        if type(xlsx_path) is not str or not xlsx_path or '\0' in xlsx_path:
            raise ValueError('explicit absolute local XLSX path required')
        path = Path(xlsx_path)
        # UNC and device paths are not local workbook sources. Do not fetch URLs.
        if not path.is_absolute() or xlsx_path.startswith(('\\\\', '//')) or path.suffix.lower() != '.xlsx':
            raise ValueError('explicit absolute local XLSX path required; no UNC/device paths or URLs')
        path = path.resolve(strict=True)
        if str(path).startswith(('\\\\', '//')) or not path.is_file():
            raise ValueError('source must resolve to a local regular file')
        with path.open('rb') as stream:
            data = stream.read(2_000_001)
        decoded = inspect_workbook(data)
        self._identity(decoded)
        record = dict(workbook_id=uuid.uuid4().hex,imported_at=time.time(),
            source_path=str(path),summary=summarize(decoded),
            source_base64=base64.b64encode(data).decode('ascii'))
        with self.service.lock:
            self.service._save('native-workbook',record,record['workbook_id'])
        return {k:v for k,v in record.items() if k != 'source_base64'}

    def _load_record(self,workbook_id):
        if type(workbook_id) is not str or not re.fullmatch('[0-9a-f]{32}',workbook_id):raise ValueError('invalid workbook ID')
        path=self.service.state_dir/f'native-workbook-{workbook_id}.json'
        with path.open('rb') as stream:raw=stream.read(3_000_001)
        if len(raw)>3_000_000:raise ValueError('stored workbook evidence exceeds size limit')
        record=json.loads(raw);decoded=decode_record(record,workbook_id);self._identity(decoded)
        return record,decoded

    def snapshot(self,workbook_id):
        """Bounded embedded source for a later transaction; no source path leakage."""
        record,_=self._load_record(workbook_id)
        return {k:record[k] for k in ('workbook_id','summary','source_base64')}

    def get(self, workbook_id=None, channel=None):
        if channel is not None and (type(channel) is not int or channel not in (1,2)):
            raise ValueError('channel must be1 or2')
        if workbook_id is None:
            if channel is not None:
                raise ValueError('explicit workbook ID required for channel details')
            paths = sorted(self.service.state_dir.glob('native-workbook-*.json'),
                key=lambda p:p.stat().st_mtime,reverse=True)[:10]
            return {'workbooks':[self.get(p.stem.removeprefix('native-workbook-')) for p in paths]}
        record,decoded=self._load_record(workbook_id)
        result = {k:v for k,v in record.items() if k != 'source_base64'}
        if channel is not None:
            result['channel_details'] = decoded['channels'][channel-1]
        return result
