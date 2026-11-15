import copy,json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from dali_mcp.native_workbook_store import NativeWorkbookStore
from dali_mcp.project_execution import ProjectRestoreController
from dali_mcp.service import Service
from test_native_workbook import document,package
from test_project_execution import CombinedGateway


class NativeCombinedTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.g=CombinedGateway();self.info=dict(ip='127.0.0.1',dali_mac='02:00:00:00:00:01',dali_firmware='test')
        self.s=Service(dict(gateway_ip=self.info['ip'],expected_dali_mac=self.info['dali_mac'],expected_firmware='test'),self.tmp.name)
        self.c=ProjectRestoreController(self.s);self.store=NativeWorkbookStore(self.s)
        op=patch.object(self.s,'_open',side_effect=lambda:(self.g,copy.deepcopy(self.info)));self.open=op.start();self.addCleanup(op.stop)
        self.original={a:copy.deepcopy(g.parameters) for a,g in self.g.lamps.items()};self.original_scenes=copy.deepcopy(self.g.scenes)
        self.doc=document()
        for cell in ('D2','D3'):self.doc['通道二参数'][cell]=5
        for cell in ('S3','S4'):self.doc['通道二分组'][cell]='√'
        self.ident=self.imported()

    def imported(self):
        path=Path(self.tmp.name)/'source.xlsx';path.write_bytes(package(self.doc));return self.store.import_file(str(path))['workbook_id']
    def spec(self,**kw):
        return dict(dict(workbook_id=self.ident,channel=2,addresses=[0,63],fields=['fade_time'],group_numbers=[15],scenes=[8],scene_source='ecg',group=None,brightness_policy='require_unique'),**kw)
    def plan(self,**kw):return self.c.prepare_workbook(self.spec(**kw))
    def allow(self,p):
        steps=p['steps'];c=p['scope']['channel'];cfg=self.s.config
        cfg.update(enable_project_restore=True,allowed_project_restore_digest=p['digest'],enable_experimental_parameter_writes=True,
            enable_experimental_group_writes=True,enable_experimental_writes=True)
        cfg['allowed_parameter_batch_targets']=[dict(channel=c,address=a,values=[dict(field=s['field'],values=s['after']) for s in steps if s['category']=='parameter' and s['address']==a]) for a in p['scope']['addresses']]
        cfg['allowed_group_write_operations']=[dict(channel=c,address=s['address'],group=s['group'],member=s['after']) for s in steps if s['category']=='group']
        cfg['allowed_write_targets']=[dict(channel=c,scene=sn,addresses=p['scope']['addresses']) for sn in p['scope']['scenes']]
    def apply(self,p,**kw):return self.c.execute(p['plan_id'],p['confirmation'],**kw)
    def restore(self,p):
        u=self.c.prepare_restore(p['plan_id']);self.allow(u);self.assertEqual(self.apply(u)['state'],'completed_readback')
        self.assertEqual({a:g.parameters for a,g in self.g.lamps.items()},self.original);self.assertEqual(self.g.scenes,self.original_scenes)
        return u

    def test_native_three_categories_roundtrip_without_fabricated_project(self):
        p=self.plan();self.assertEqual(len(p['steps']),6)
        self.assertNotIn('source_project',p);self.assertIn('source_workbook',p)
        source=p['source_workbook'];self.assertFalse(source['review']['device_values_verified'])
        self.assertEqual(source['format'],'dali-native-workbook-restoration-source')
        self.assertNotIn('source_path',source['evidence']);self.assertFalse(list(self.s.state_dir.glob('project-????????????????????????????????.json')))
        self.allow(p);self.assertEqual(self.apply(p)['state'],'completed_readback');u=self.restore(p)
        self.assertEqual(u['source_workbook'],p['source_workbook'])

    def test_blank_paired_component_keeps_fresh_original_not_zero(self):
        self.doc['通道二参数'].update(H2=None,I2=4000,H3=None,I3=None);self.ident=self.imported()
        self.g.lamps[0].parameters['power_on']['value']['level_raw']=33
        self.original[0]=copy.deepcopy(self.g.lamps[0].parameters)
        p=self.plan(fields=['power_on'],group_numbers=[],scenes=[],scene_source=None)
        self.assertEqual(len(p['steps']),1);self.assertEqual(p['steps'][0]['after'],dict(level_raw=33,kelvin_raw=4000))
        self.assertIn('power_on_level',p['source_workbook']['review']['parameter_reviews'][0]['kept_blank_components'])
        self.allow(p);self.assertEqual(self.apply(p)['state'],'completed_readback');self.restore(p)

    def test_all_blank_source_is_no_network_not_already_matches(self):
        self.doc['通道二参数'].update(D2=None,D3=None);self.ident=self.imported()
        p=self.plan(group_numbers=[],scenes=[],scene_source=None)
        self.assertEqual(p['state'],'no_targets_in_source');self.assertFalse(p['network_traffic']);self.open.assert_not_called()

    def test_blank_scene_is_mask_but_numeric_zero_is_off(self):
        self.doc['通道二ECG场景'].update(T3=None,U3=None,T4=0,U4=None);self.ident=self.imported()
        p=self.plan(fields=[],group_numbers=[])
        self.assertEqual([(r['scenes'][0]['level_raw'],r['scenes'][0]['kelvin_raw']) for r in p['intent']],[(255,65535),(0,65535)])
        self.assertEqual(len(p['steps']),1);self.allow(p);self.assertEqual(self.apply(p)['state'],'completed_readback');self.restore(p)

    def test_ambiguous_parameter_requires_explicit_quantization_policy(self):
        with self.assertRaisesRegex(ValueError,'ambiguous'):self.plan(fields=['min_level'],group_numbers=[],scenes=[],scene_source=None)
        self.open.assert_not_called()
        p=self.plan(fields=['min_level'],group_numbers=[],scenes=[],scene_source=None,brightness_policy='native_nearest')
        self.assertTrue(all(s['after']==dict(level_raw=1) for s in p['steps']))
        self.assertFalse(p['source_workbook']['review']['original_precision_guaranteed'])

    def test_conflicting_ecg_and_group_source_choice_is_preserved(self):
        self.doc['通道二组场景']['T4']=0;self.ident=self.imported()
        ecg=self.plan();group=self.plan(scene_source='group',group=0)
        self.assertEqual(ecg['intent'][0]['scenes'][0]['level_raw'],254)
        self.assertEqual(group['intent'][0]['scenes'][0]['level_raw'],0)
        self.assertTrue(group['source_workbook']['review']['scene_review']['source_conflicts'])
        self.assertEqual(group['source_workbook']['spec']['scene_source'],'group')
        self.assertEqual(group['intent'][0]['groups'],[dict(group=15,member=True)])

    def test_missing_or_invalid_source_selection_refused_before_network(self):
        for kw in (dict(addresses=[1]),dict(addresses=[True]),dict(channel=True),dict(scenes=[8],scene_source=None),
            dict(scenes=[],scene_source='ecg'),dict(group=False),dict(fields=['bad']),dict(group_numbers=[True]),
            dict(fields=[],scenes=[],group_numbers=[],scene_source=None),dict(workbook_id='../bad')):
            with self.subTest(kw=kw),self.assertRaises(ValueError):self.plan(**kw)
        self.open.assert_not_called();self.assertFalse(self.g.events)

    def test_stored_source_changed_or_removed_refuses_forward_before_network(self):
        p=self.plan();self.allow(p);path=self.s.state_dir/f'native-workbook-{self.ident}.json';raw=path.read_bytes()
        d=json.loads(raw);d['summary']['source_sha256']='0'*64;path.write_text(json.dumps(d),encoding='utf-8');n=self.open.call_count
        with self.assertRaises(ValueError):self.apply(p)
        self.assertEqual(self.open.call_count,n);self.assertFalse(self.g.events)
        path.write_bytes(raw);path.unlink()
        with self.assertRaises(FileNotFoundError):self.apply(p)
        self.assertEqual(self.open.call_count,n)

    def test_removed_source_does_not_block_attempted_prefix_undo(self):
        p=self.plan();self.allow(p);r=self.apply(p,cancelled=lambda:len(self.g.events)>=3)
        self.assertEqual(r['state'],'cancelled_partial')
        (self.s.state_dir/f'native-workbook-{self.ident}.json').unlink()
        u=self.restore(p);self.assertEqual(len(u['steps']),3)

    def test_source_change_during_capture_creates_no_plan(self):
        old=self.g.read_scene
        def mutate(*args):
            path=self.s.state_dir/f'native-workbook-{self.ident}.json';d=json.loads(path.read_text(encoding='utf-8'))
            d['summary']['source_sha256']='0'*64;path.write_text(json.dumps(d),encoding='utf-8');return old(*args)
        with patch.object(self.g,'read_scene',side_effect=mutate),self.assertRaises(ValueError):self.plan()
        self.assertFalse(list(self.s.state_dir.glob('project-restore-plan-*.json')));self.assertFalse(self.g.events)

    def test_embedded_source_and_review_and_mixed_source_tampering_rejected(self):
        for mode in ('bytes','review','both'):
            p=self.plan();path=self.s.state_dir/f"project-restore-plan-{p['plan_id']}.json";d=json.loads(path.read_text(encoding='utf-8'))
            if mode=='bytes':d['source_workbook']['evidence']['source_base64']='AA=='
            elif mode=='review':d['source_workbook']['review']['device_values_verified']=True
            else:d['source_project']={}
            d['digest']=self.c._digest(d);path.write_text(json.dumps(d),encoding='utf-8')
            with self.assertRaises(ValueError):self.c.get(p['plan_id'])

    def test_current_limit_conflict_is_not_resolved_by_unselected_native_limits(self):
        self.g.lamps[63].parameters['max_level']['value']['level_raw']=200
        with self.assertRaisesRegex(ValueError,'projected'):self.plan(fields=[])
        p=self.plan(fields=['max_level']);self.assertTrue(any(s['category']=='parameter' and s['address']==63 for s in p['steps']))
        self.assertFalse(self.g.events)

    def test_full64_16scene_preparation_preserves_native_source_and_scope(self):
        self.doc=document(tuple(range(64)))
        for a in range(64):self.doc['通道二参数'][f'D{a+2}']=5;self.doc['通道二分组'][f'S{a+3}']='√'
        self.ident=self.imported();p=self.plan(addresses=list(range(64)),scenes=list(range(16)))
        self.assertEqual(len(p['originals']),64);self.assertEqual(sum(len(r['scenes']) for r in p['intent']),1024)
        self.assertEqual(sum(len(r['groups']) for r in p['intent']),64);self.assertEqual(self.open.call_count,1)
        self.assertFalse(self.g.events);self.assertEqual(self.c.get(p['plan_id'])['plan']['digest'],p['digest'])

    def test_all_nine_native_parameter_fields_with_scene_and_membership_roundtrip(self):
        fields=['fade_time','fade_rate','min_level','max_level','configured_min_ct','configured_max_ct','power_on','system_failure','dimming_curve']
        for row in (2,3):
            for col,value in dict(D=5,E=6,F=0.2,G=97,H=1,I=4000,J=20,K=4000,L='线性',M=6000,N=3000).items():self.doc['通道二参数'][f'{col}{row}']=value
        for row in (3,4):self.doc['通道二ECG场景'][f'T{row}']=50
        self.ident=self.imported();p=self.plan(fields=fields,brightness_policy='native_nearest')
        self.assertEqual(sum(s['category']=='parameter' for s in p['steps']),18)
        self.allow(p);self.assertEqual(self.apply(p)['state'],'completed_readback');self.restore(p)

    def test_valid_replaced_source_bytes_are_rejected_before_forward_connection(self):
        p=self.plan();self.allow(p);old_id=self.ident
        self.doc['通道二参数']['D2']=6;replacement=self.store.snapshot(self.imported());replacement['workbook_id']=old_id
        self.s._save('native-workbook',replacement,old_id);n=self.open.call_count
        with self.assertRaisesRegex(ValueError,'source changed'):self.apply(p)
        self.assertEqual(self.open.call_count,n);self.assertFalse(self.g.events)

    def test_actual_gateway_identity_mismatch_during_native_preparation(self):
        self.info['dali_firmware']='different'
        with self.assertRaisesRegex(ValueError,'gateway mismatch'):self.plan()
        self.assertFalse(self.g.events);self.assertEqual(self.g.closed,1)

if __name__=='__main__':unittest.main()
