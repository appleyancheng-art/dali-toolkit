import copy,itertools,json,os,tempfile,unittest
from pathlib import Path
from unittest.mock import Mock,patch
from dali_mcp.service import Service
from dali_mcp.projects import digest,validate_document
from dali_mcp.project_names import ProjectNames,compare_names
from dali_mcp.native_file import NativeFileController,parse
from test_native_cache import document
from test_project_layouts import project_fixture
from test_project_floorplan import cache_fixture,png
from project_report import render


class ProjectNameTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.file=self.root/'cache.json';self.original=json.dumps(document(),ensure_ascii=False,indent=2).encode();self.file.write_bytes(self.original)
        self.cfg=dict(gateway_ip='127.0.0.1',expected_dali_mac='02:00:00:00:00:01',expected_firmware='1.6.9',configmaster_cache_file=str(self.file))
        self.s=Service(self.cfg,self.root/'state');self.s._open=Mock(side_effect=AssertionError('names must not use bus'))
        guard=patch('dali_mcp.native_file.require_configmaster_closed');self.guard=guard.start();self.addCleanup(guard.stop)
        if os.name!='nt':
            p=patch('dali_mcp.native_file.replace_native_file',side_effect=os.replace);p.start();self.addCleanup(p.stop)
        self.n=ProjectNames(self.s);self.f=NativeFileController(self.s);self.project=project_fixture()
        self.s._save('project',self.project,self.project['project_id'])
        self.cache=self.s.import_configmaster_cache(cache_fixture())['cache_id']
        self.selections=[dict(kind='lamp',address=0),dict(kind='group',group=15),dict(kind='scene',group=0,scene=15)]
    def tearDown(self):self.s._open.assert_not_called()
    def attach(self,selections=None,project_id=None):
        return self.n.attach(project_id or self.project['project_id'],self.cache,self.selections if selections is None else selections)['project_id']
    def apply(self,p):return self.f.apply(p['plan_id'],p['confirmation'])
    def target(self):
        d=cache_fixture();d['zCh2']['addresses']['0']['name']='迁移灯';d['zCh2']['groupNames']['15']='工程组';d['zCh2']['sceneNamesByGroup']['0']['15']='工程备注'
        self.cache=self.s.import_configmaster_cache(d)['cache_id'];return self.attach()

    def test_immutable_attachment_preserves_device_evidence_and_missing_empty(self):
        sels=[dict(kind='lamp',address=63),dict(kind='group',group=14),dict(kind='scene',group=1,scene=0)]
        ident=self.attach(sels);p=self.s.get_project(ident)
        self.assertEqual(p['schema_version'],4);self.assertEqual(p['rows'],self.project['rows'])
        self.assertEqual(self.s.get_project(self.project['project_id']),self.project)
        self.assertEqual([e['present'] for e in self.n.get(ident)['entries']],[True,False,False])
        self.assertEqual([e['name'] for e in self.n.get(ident)['entries']],['',None,None])
        self.assertIn('原生名称附件',render(p));self.assertFalse(self.n.get(self.project['project_id'])['present'])

    def test_all_attachment_orders_preserve_names_floorplan_grid(self):
        layout=self.s.create_layout('Grid',2,[[0,63]])['layout_id'];image=self.root/'image.png';image.write_bytes(png())
        for order in itertools.permutations(('names','floor','grid')):
            ident=self.project['project_id']
            for step in order:
                if step=='names':ident=self.attach(project_id=ident)
                elif step=='floor':ident=self.s.attach_project_floorplan(ident,self.cache,str(image))['project_id']
                else:ident=self.s.attach_project_layouts(ident,[layout])['project_id']
            p=validate_document(self.s.get_project(ident));self.assertEqual(p['schema_version'],4)
            self.assertEqual(p['rows'],self.project['rows']);self.assertIn('native_floorplan',p);self.assertIn('layouts',p);self.assertIn('native_names',p)

    def test_portable_restore_without_original_cache_and_source_files(self):
        p=self.s.get_project(self.target())
        other=Service(self.cfg,self.root/'other');other._open=Mock(side_effect=AssertionError('no bus'))
        ident=other.import_project(json.loads(json.dumps(p)))['project_id']
        for path in self.s.state_dir.glob('native-cache-*'):path.unlink()
        n=ProjectNames(other);f=NativeFileController(other)
        self.assertEqual(n.get(ident)['entries'],p['native_names']['entries'])
        plan=n.prepare(ident,self.selections);self.cfg['enable_native_cache_writes']=True
        self.assertEqual(f.apply(plan['plan_id'],plan['confirmation'])['state'],'written_verified')
        self.assertEqual(parse(self.file.read_bytes())['zCh2']['addresses']['0']['name'],'迁移灯')
        # Recovery uses bound originals even after imported project disappears.
        (other.state_dir/f'project-{ident}.json').unlink()
        undo=f.prepare_restore(plan['plan_id']);self.assertEqual(f.apply(undo['plan_id'],undo['confirmation'])['state'],'written_verified')
        self.assertEqual(self.file.read_bytes(),self.original);other._open.assert_not_called()

    def test_missing_source_clears_only_selected_default_label(self):
        sels=[dict(kind='group',group=14)];ident=self.attach(sels)
        current=document();current['zCh2']['groupNames']['14']='已有';self.file.write_text(json.dumps(current),encoding='utf-8')
        plan=self.n.prepare(ident,sels);self.assertEqual(plan['changes'][0]['name'],'')
        self.cfg['enable_native_cache_writes']=True;self.apply(plan)
        current['zCh2']['groupNames']['14']='';self.assertEqual(parse(self.file.read_bytes()),current)

    def test_selection_scope_invalid_shapes_and_uncaptured_rejected(self):
        for sels in ([],[dict(kind='lamp',address=1)],[dict(kind='lamp',address=True)],
                     [dict(kind='scene',group=0,scene=7)],[dict(kind='group',group=0,name='x')],
                     [self.selections[0],self.selections[0]]):
            with self.assertRaises(ValueError):self.attach(sels)
        ident=self.attach()
        with self.assertRaisesRegex(ValueError,'not included'):self.n.prepare(ident,[dict(kind='lamp',address=63)])
        self.assertEqual(self.file.read_bytes(),self.original)

    def test_gateway_and_inner_source_tampering_rejected(self):
        p=self.s.get_project(self.attach())
        for change in ('gateway','hash','entry','version','missing'):
            d=copy.deepcopy(p)
            if change=='gateway':d['gateway']['dali_mac']='02:00:00:00:00:02'
            elif change=='hash':d['native_names']['source_document_sha256']='0'*64
            elif change=='entry':d['native_names']['entries'][0]['name']='different'
            elif change=='version':d['schema_version']=1
            else:del d['native_names']
            d['digest']=digest(d)
            with self.assertRaises(ValueError):validate_document(d)

    def test_source_changed_after_prepare_refuses_before_claim(self):
        ident=self.target();plan=self.n.prepare(ident,self.selections);self.cfg['enable_native_cache_writes']=True
        p=self.s.get_project(ident);p['name']='Changed';p['digest']=digest(p);self.s._save('project',p,ident)
        with self.assertRaisesRegex(ValueError,'source project changed'):self.apply(plan)
        self.assertFalse(self.f._record_path('claim',plan['plan_id']).exists());self.assertEqual(self.file.read_bytes(),self.original)

    def test_exact_changes_are_bound_and_conflicting_source_types_reject(self):
        ident=self.target();plan=self.n.prepare(ident,self.selections)
        wrong=self.f.prepare([dict(kind='lamp',channel=2,address=0,name='wrong')])
        path=self.f._record_path('plan',wrong['plan_id']);p=json.loads(path.read_text(encoding='utf-8'))
        p['source_project_names']=plan['source_project_names'];p['digest']=self.f._plan_digest(p);path.write_text(json.dumps(p),encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'differ'):self.f.get(wrong['plan_id'])
        with self.assertRaisesRegex(ValueError,'exactly one'):self.f.prepare(plan['changes'],source_workbook={},source_project_names=plan['source_project_names'])

    def test_write_gate_app_guard_claim_no_replay_and_unrelated_preserved(self):
        ident=self.target();plan=self.n.prepare(ident,self.selections)
        with self.assertRaisesRegex(ValueError,'gate'):self.apply(plan)
        self.cfg['enable_native_cache_writes']=True;self.guard.side_effect=RuntimeError('running')
        with self.assertRaisesRegex(RuntimeError,'running'):self.apply(plan)
        self.guard.side_effect=None;self.assertEqual(self.apply(plan)['state'],'written_verified')
        self.assertTrue(self.apply(plan)['claimed'])
        d=parse(self.file.read_bytes());d['future_data']['fresh']=42;self.file.write_text(json.dumps(d),encoding='utf-8')
        undo=self.f.prepare_restore(plan['plan_id']);self.apply(undo)
        expected=document();expected['future_data']['fresh']=42;self.assertEqual(parse(self.file.read_bytes()),expected)

    def test_third_party_label_change_stops_undo(self):
        plan=self.n.prepare(self.target(),self.selections);self.cfg['enable_native_cache_writes']=True;self.apply(plan)
        d=parse(self.file.read_bytes());d['zCh2']['addresses']['0']['name']='第三方';self.file.write_text(json.dumps(d),encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'changed after'):self.f.prepare_restore(plan['plan_id'])

    def test_long_label_kept_as_evidence_but_not_truncated_for_write(self):
        d=document();d['zCh2']['addresses']['0']['name']='长'*11;self.cache=self.s.import_configmaster_cache(d)['cache_id']
        ident=self.attach();self.assertEqual(self.n.get(ident)['entries'][0]['name'],'长'*11)
        with self.assertRaisesRegex(ValueError,'30'):self.n.prepare(ident,self.selections)

    def test_name_comparison_distinguishes_capture_and_change(self):
        a=self.s.get_project(self.attach());b=self.s.get_project(self.target())
        self.assertEqual(compare_names(self.project,a)['state'],'not_captured_before')
        self.assertEqual(compare_names(a,b)['state'],'changed')
        self.assertEqual(compare_names(a,copy.deepcopy(a))['state'],'unchanged')

if __name__=='__main__':unittest.main()
