import copy,json,os,tempfile,unittest
from pathlib import Path
from unittest.mock import Mock,patch
from dali_mcp.service import Service
from dali_mcp.native_file import NativeFileController,parse
from dali_mcp.native_names import prepare_name_document
from dali_mcp.native_workbook_store import NativeWorkbookStore
from dali_mcp.native_workbook_names import NativeWorkbookNames
from test_native_cache import document as cache_document
from test_native_workbook import document,package,column


class NativeWorkbookNameTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.path=self.root/'cache.json';self.original=json.dumps(cache_document(),ensure_ascii=False,indent=2).encode();self.path.write_bytes(self.original)
        self.cfg=dict(gateway_ip='127.0.0.1',expected_dali_mac='02:00:00:00:00:01',expected_firmware='1.6.9',configmaster_cache_file=str(self.path))
        self.s=Service(self.cfg,self.root/'state');self.s._open=Mock(side_effect=AssertionError('name restore never uses bus'))
        self.guard=patch('dali_mcp.native_file.require_configmaster_closed');self.guard_mock=self.guard.start();self.addCleanup(self.guard.stop)
        if os.name!='nt':
            p=patch('dali_mcp.native_file.replace_native_file',side_effect=os.replace);p.start();self.addCleanup(p.stop)
        self.n=NativeWorkbookNames(self.s);self.file=NativeFileController(self.s);self.doc=document();self.id=self.imported(self.doc)
    def imported(self,d):
        path=self.root/'source.xlsx';path.write_bytes(package(d));return NativeWorkbookStore(self.s).import_file(str(path))['workbook_id']
    def plan(self,selection=None,**kw):
        return self.n.prepare(self.id,2,selection or [dict(kind='lamp',address=0),dict(kind='group',group=15),dict(kind='scene',group=0,scene=5)],**kw)
    def apply(self,p):return self.file.apply(p['plan_id'],p['confirmation'])
    def tearDown(self):self.s._open.assert_not_called()

    def test_three_label_kinds_apply_and_restore_exact_bytes(self):
        p=self.plan();self.assertEqual(self.path.read_bytes(),self.original)
        with self.assertRaisesRegex(ValueError,'gate'):self.apply(p)
        self.cfg['enable_native_cache_writes']=True;self.assertEqual(self.apply(p)['state'],'written_verified')
        expected=cache_document();expected['zCh2']['addresses']['0']['name']='灯0';expected['zCh2']['groupNames']['15']=''
        expected['zCh2']['sceneNamesByGroup']['0']['5']='夜间';self.assertEqual(parse(self.path.read_bytes()),expected)
        undo=self.file.prepare_restore(p['plan_id']);self.assertEqual(self.apply(undo)['state'],'written_verified')
        self.assertEqual(self.path.read_bytes(),self.original)

    def test_group_source_conflict_is_explicit_and_not_merged(self):
        self.doc['通道二分组']['S2']='分组表名';self.id=self.imported(self.doc)
        p=self.plan([dict(kind='group',group=15)])
        self.assertEqual(p['changes'][0]['name'],'分组表名');self.assertEqual(p['source_workbook']['group_name_conflicts'][0]['chosen'],'grouping')
        p=self.plan([dict(kind='group',group=15)],group_name_source='group_scene')
        self.assertEqual(p['changes'][0]['name'],'Group name 15')

    def test_all336_selected_names_preserve_device_values_and_other_channel(self):
        self.doc=document(range(64));self.id=self.imported(self.doc);d=cache_document()
        d['zCh2']['addresses']={str(a):dict(name='',sceneKnown=0,untouched=a) for a in range(64)}
        self.original=json.dumps(d,ensure_ascii=False,indent=2).encode();self.path.write_bytes(self.original)
        selections=[dict(kind='lamp',address=a) for a in range(64)]+[dict(kind='group',group=g) for g in range(16)]+[dict(kind='scene',group=g,scene=s) for g in range(16) for s in range(16)]
        p=self.plan(selections);self.assertEqual(len(p['changes']),336);self.cfg['enable_native_cache_writes']=True;self.apply(p)
        actual=parse(self.path.read_bytes());self.assertEqual(actual['zCh1'],d['zCh1'])
        self.assertTrue(all(actual['zCh2']['addresses'][str(a)]['untouched']==a for a in range(64)))
        self.apply(self.file.prepare_restore(p['plan_id']));self.assertEqual(self.path.read_bytes(),self.original)

    def test_blank_name_clears_only_selected_scope(self):
        self.doc['通道二参数']['C2']='';self.id=self.imported(self.doc);p=self.plan([dict(kind='lamp',address=0)])
        self.cfg['enable_native_cache_writes']=True;self.apply(p)
        expected=cache_document();expected['zCh2']['addresses']['0']['name']='';self.assertEqual(parse(self.path.read_bytes()),expected)

    def test_app_running_or_missing_cached_lamp_stops_before_plan(self):
        self.guard_mock.side_effect=RuntimeError('app running')
        with self.assertRaisesRegex(RuntimeError,'app running'):self.plan()
        self.guard_mock.side_effect=None;d=cache_document();del d['zCh2']['addresses']['0'];self.path.write_text(json.dumps(d),encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'absent'):self.plan()
        self.assertFalse(list(self.s.state_dir.glob('native-file-plan-*')))

    def test_invalid_selections_and_long_native_names_not_truncated(self):
        for selections in ([],[dict(kind='lamp',address=True)],[dict(kind='lamp',address=0,name='injected')],
                           [dict(kind='group',group=16)],[dict(kind='scene',group=0,scene=1),dict(kind='scene',group=0,scene=1)]):
            with self.assertRaises(ValueError):self.n.prepare(self.id,2,selections)
        with self.assertRaises(ValueError):self.n.prepare(self.id,True,[dict(kind='lamp',address=0)])
        self.doc['通道二参数']['C2']='长'*11;self.id=self.imported(self.doc)
        with self.assertRaisesRegex(ValueError,'30'):self.plan()
        self.assertEqual(self.path.read_bytes(),self.original)

    def test_changed_source_and_rehashed_alternate_name_rejected_before_file_write(self):
        p=self.plan();self.cfg['enable_native_cache_writes']=True
        other=self.file.prepare([dict(kind='lamp',channel=2,address=0,name='不是Excel名字')])
        record_path=self.file._record_path('plan',other['plan_id']);record=json.loads(record_path.read_text(encoding='utf-8'))
        record['source_workbook']=p['source_workbook'];record['digest']=self.file._plan_digest(record);record_path.write_text(json.dumps(record),encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'source/selected'):self.file.get(other['plan_id'])
        source_path=self.s.state_dir/f'native-workbook-{self.id}.json';r=json.loads(source_path.read_text(encoding='utf-8'))
        r['summary']['source_sha256']='0'*64;source_path.write_text(json.dumps(r),encoding='utf-8')
        with self.assertRaises(ValueError):self.apply(p)
        self.assertEqual(self.path.read_bytes(),self.original)

    def test_file_conflicts_and_conditional_restore_preserve_new_unrelated_data(self):
        p=self.plan();self.cfg['enable_native_cache_writes']=True;self.apply(p)
        current=parse(self.path.read_bytes());current['future_data']['new']='keep';self.path.write_text(json.dumps(current),encoding='utf-8')
        undo=self.file.prepare_restore(p['plan_id']);self.assertFalse(undo['exact_original_bytes']);self.apply(undo)
        expected=cache_document();expected['future_data']['new']='keep';self.assertEqual(parse(self.path.read_bytes()),expected)
        p=self.plan();current=parse(self.path.read_bytes());current['zCh2']['addresses']['0']['name']='外部修改';self.path.write_text(json.dumps(current),encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'changed since'):self.apply(p)


if __name__=='__main__':unittest.main()
