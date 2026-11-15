import base64
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from dali_mcp.service import Service
from dali_mcp.native_workbook_store import NativeWorkbookStore
from test_native_workbook import document, package


class NativeWorkbookStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.service = Service(dict(gateway_ip='127.0.0.1',expected_dali_mac='02:00:00:00:00:01',
            expected_firmware='1.6.9'),self.root/'state',gateway_factory=lambda: self.fail('must not open bus'))
        self.store = NativeWorkbookStore(self.service)
        self.path = self.root/'工程样本.xlsx'
        self.source = package(document())
        self.path.write_bytes(self.source)

    def imported(self):
        return self.store.import_file(str(self.path))

    def test_durable_original_and_scoped_retrieval_without_device_access(self):
        result = self.imported()
        self.assertEqual(self.path.read_bytes(),self.source)
        self.path.unlink()  # retrieval must use preserved bytes, not original path
        record = self.store.get(result['workbook_id'],2)
        self.assertEqual(record['channel_details']['channel'],2)
        self.assertEqual([f['address'] for f in record['channel_details']['fixtures']],[0,63])
        self.assertEqual(record['channel_details']['fixtures'][1]['scenes'][8]['brightness']['percent'],100)
        self.assertNotIn('channel_details',self.store.get(result['workbook_id']))
        self.assertNotIn('source_base64',record)
        self.assertEqual(len(self.store.get()['workbooks']),1)
        self.assertEqual(len(list(self.service.state_dir.iterdir())),1)
        saved = json.loads(next(self.service.state_dir.iterdir()).read_text(encoding='utf-8'))
        self.assertEqual(base64.b64decode(saved['source_base64']),self.source)

    def test_quantization_is_explicit_and_not_an_executable_live_backup(self):
        summary = self.imported()['summary']
        low = next(p for p in summary['percent_review'] if p['percent']==0.1)
        self.assertEqual(low['exact_export_candidates'],list(range(1,16)))
        self.assertEqual(low['native_import_level_raw'],1)
        self.assertFalse(low['original_raw_level_uniquely_recoverable'])
        for flag in ['executable','device_values_verified','online_addresses_verified','native_import_runtime_verified']:
            self.assertIs(summary[flag],False)
        self.assertEqual(summary['source_provenance'],'unverified')

    def test_conflicts_and_names_are_preserved_for_later_decisions(self):
        d=document();d['通道一组场景']['D4']=22
        self.path.write_bytes(package(d))
        result=self.imported()
        self.assertGreater(result['summary']['channels'][0]['scene_conflict_count'],0)
        channel=self.store.get(result['workbook_id'],1)['channel_details']
        self.assertEqual(channel['group_scene_blocks'][0]['scene_names']['5'],'夜间')
        self.assertEqual(channel['group_scene_blocks'][0]['rows'][0]['scenes'][0]['brightness']['percent'],22)
        self.assertEqual(channel['fixtures'][0]['scenes'][0]['brightness']['percent'],0)

    def test_mismatched_identity_rejected_before_persistence_and_after_reconfiguration(self):
        original=self.service.config['expected_dali_mac']
        self.service.config['expected_dali_mac']='02:00:00:00:00:99'
        with self.assertRaisesRegex(ValueError,'MAC'):self.imported()
        self.assertEqual(list(self.service.state_dir.iterdir()),[])
        self.service.config['expected_dali_mac']=original
        result=self.imported()
        self.service.config['expected_dali_mac']='02:00:00:00:00:99'
        with self.assertRaisesRegex(ValueError,'MAC'):self.store.get(result['workbook_id'])

    def test_paths_size_and_invalid_workbooks_rejected_without_records(self):
        for path in ['relative.xlsx','https://example.invalid/a.xlsx',r'\\server\share\a.xlsx','',True,str(self.root)]:
            with self.subTest(path=path),self.assertRaises(ValueError):self.store.import_file(path)
        self.path.write_bytes(b'x'*2_000_001)
        with self.assertRaises(ValueError):self.imported()
        self.path.write_bytes(b'not a zip')
        with self.assertRaises(ValueError):self.imported()
        self.assertEqual(list(self.service.state_dir.iterdir()),[])

    def test_summary_source_and_id_tampering_detected(self):
        result=self.imported();path=next(self.service.state_dir.iterdir())
        original=json.loads(path.read_text(encoding='utf-8'))
        for mutation in ['summary','source','id','base64']:
            record=copy.deepcopy(original)
            if mutation=='summary':record['summary']['executable']=True
            if mutation=='source':record['source_base64']=base64.b64encode(package(document((1,)))).decode()
            if mutation=='id':record['workbook_id']='0'*32
            if mutation=='base64':record['source_base64']='!bad'
            path.write_text(json.dumps(record),encoding='utf-8')
            with self.subTest(mutation=mutation),self.assertRaises(ValueError):self.store.get(result['workbook_id'])

    def test_strict_channel_id_validation(self):
        result=self.imported()
        for value in [True,0,3,'2']:
            with self.assertRaises(ValueError):self.store.get(result['workbook_id'],value)
        for value in ['../file','',True,'A'*32]:
            with self.assertRaises(ValueError):self.store.get(value)
        with self.assertRaises(ValueError):self.store.get(channel=1)


if __name__=='__main__':unittest.main()
