import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from dali_mcp.service import Service
from dali_mcp.native_workbook_store import NativeWorkbookStore
from dali_mcp.native_workbook_scenes import NativeWorkbookScenes
from test_native_workbook import document,package
from test_workflows import FakeGateway


class SceneGateway(FakeGateway):
    def __init__(self):
        super().__init__();self.low=1;self.high=254;self.high_ct=6500
    def read_lamp_profile(self,ch,a):
        p=super().read_lamp_profile(ch,a);p['max_kelvin_raw']=self.high_ct;return p
    def read_lamp_parameter(self,ch,a,field,typ):
        return {'value':{'level_raw':self.low if field=='min_level' else self.high}}
    def read_scene(self,ch,a,s,typ):
        self.reads.append((ch,a,s));level,color=self.values.get((ch,a,s),(0,4000))
        return dict(channel=ch,address=a,scene=s,light_type_code=typ,level_raw=level,kelvin_raw=color)
    def write_scene(self,target):
        self.writes.append(dict(target))
        self.values[(target['channel'],target['address'],target['scene'])]=(target['level_raw'],target['kelvin_raw'])
        if self.fail_write:raise TimeoutError('lost after send')


class NativeWorkbookScenesTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.g=SceneGateway()
        self.config=dict(gateway_ip='127.0.0.1',expected_dali_mac='02:00:00:00:00:01',expected_firmware='1.6.9',
            enable_experimental_writes=True,allowed_write_targets=[dict(channel=2,scene=s,addresses=list(range(64))) for s in range(16)])
        self.service=Service(self.config,Path(self.tmp.name)/'state',lambda:self.g)
        self.store=NativeWorkbookStore(self.service);self.native=NativeWorkbookScenes(self.service)
        self.path=Path(self.tmp.name)/'source.xlsx'
        self.id=self.imported(document())

    def imported(self,d):
        self.path.write_bytes(package(d));return self.store.import_file(str(self.path))['workbook_id']

    def plan(self,**kw):
        args=dict(workbook_id=self.id,channel=2,addresses=[0,63],scenes=[8,15],scene_source='ecg')
        args.update(kw);return self.native.prepare(**args)

    def test_prepare_apply_and_conditional_undo_preserve_scope_and_originals(self):
        p=self.plan();self.assertFalse(self.g.writes)
        self.assertEqual(len(p['rows']),4)
        self.assertEqual(p['native_workbook_source']['source_sha256'],self.store.get(self.id)['summary']['source_sha256'])
        result=self.service.apply(p['plan_id'],p['confirmation'])
        self.assertEqual(result['state'],'completed')
        self.assertEqual(self.g.values[(2,63,8)],(254,4000))
        self.assertEqual(self.g.values[(2,63,15)],(0,65535))
        self.assertNotIn((2,0,7),self.g.values)
        undo=self.service.preview_scene_restore(p['plan_id'])
        restored=self.service.apply(undo['plan_id'],undo['confirmation'])
        self.assertEqual(restored['state'],'completed')
        self.assertTrue(all(v==(0,4000) for v in self.g.values.values()))

    def test_all64_by16_supported_without_truncation(self):
        self.id=self.imported(document(tuple(range(64))))
        p=self.plan(addresses=list(range(64)),scenes=list(range(16)))
        self.assertEqual(len(p['rows']),1024)
        self.assertEqual(len(self.g.profiles),64)
        result=self.service.apply(p['plan_id'],p['confirmation'])
        self.assertEqual(result['state'],'completed');self.assertEqual(len(result['results']),1024)
        self.assertEqual(len(self.g.profiles),128)
        self.assertEqual({(r['target']['address'],r['target']['scene']) for r in result['results']},
            {(a,s) for a in range(64) for s in range(16)})

    def test_ambiguous_and_non_native_percents_require_explicit_quantization(self):
        for percent in (0.1,50):
            d=document();d['通道二ECG场景']['T3']=percent;self.id=self.imported(d)
            with self.assertRaisesRegex(ValueError,'ambiguous'):self.plan(addresses=[0],scenes=[8])
            self.assertEqual(self.g.opened,0)
            changes,binding,review=self.native.targets(self.id,2,[0],[8],'ecg',brightness_policy='native_nearest')
            self.assertEqual(changes[0]['level_raw'],1 if percent==0.1 else 228)
            self.assertFalse(review['percent_review'][0]['native_import_runtime_verified'])

    def test_conflicting_group_explicit_source_no_merge_and_no_scope_expansion(self):
        d=document();d['通道二组场景']['T4']=20;self.id=self.imported(d)
        ecg=self.plan(addresses=[0],scenes=[8])
        group=self.plan(addresses=[0],scenes=[8],scene_source='group',group=0)
        self.assertEqual(ecg['rows'][0]['after']['level_raw'],254)
        self.assertEqual(group['rows'][0]['after']['level_raw'],195)
        self.assertTrue(group['native_workbook_review']['source_conflicts'])
        self.assertEqual(len(group['rows']),1)

    def test_missing_group_members_invalid_scope_and_dt6_reject_before_network(self):
        for kwargs in [dict(scene_source='group',group=15),dict(scene_source='group'),dict(group=0),
                       dict(addresses=[1]),dict(addresses=[True]),dict(scenes=[16]),dict(scene_source='merged')]:
            with self.subTest(kwargs=kwargs),self.assertRaises(ValueError):self.plan(**kwargs)
        d=document()
        for sheet in ('通道二参数','通道二分组','通道二ECG场景','通道二组场景'):
            for key in list(d[sheet]):
                if key.startswith('B') and d[sheet][key]=='DT8':d[sheet][key]='DT6'
        self.id=self.imported(d)
        with self.assertRaisesRegex(ValueError,'DT8'):self.plan()
        self.assertEqual(self.g.opened,0)

    def test_source_replaced_and_rehashed_plan_target_changes_rejected_before_network(self):
        p=self.plan();opened=self.g.opened
        record=self.service.get_plan(p['plan_id'])
        record['rows'][0]['after']['level_raw']=200
        record['digest']=hashlib.sha256(json.dumps(record['rows'],sort_keys=True).encode()).hexdigest()
        self.service._save('plan',record,p['plan_id'])
        with self.assertRaisesRegex(ValueError,'differ'):
            self.service.apply(p['plan_id'],f"APPLY {p['plan_id']} {record['digest'][:12]}")
        self.assertEqual(self.g.opened,opened);self.assertFalse(self.g.writes)
        p=self.plan()
        path=self.service.state_dir/f'native-workbook-{self.id}.json'
        saved=json.loads(path.read_text(encoding='utf-8'));saved['summary']['source_sha256']='0'*64
        path.write_text(json.dumps(saved),encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'checksum'):self.service.apply(p['plan_id'],p['confirmation'])
        self.assertFalse(self.g.writes)

    def test_fresh_limits_and_current_scene_conflicts_prevent_all_writes(self):
        p=self.plan();self.g.high=253
        with self.assertRaisesRegex(ValueError,'brightness limits'):self.service.apply(p['plan_id'],p['confirmation'])
        self.assertFalse(self.g.writes)
        self.g.high=254;self.g.values[(2,63,15)]=(10,4000)
        with self.assertRaisesRegex(RuntimeError,'scene changed'):self.service.apply(p['plan_id'],p['confirmation'])
        self.assertFalse(self.g.writes)

    def test_write_disabled_scope_and_gateway_changes_reject_before_network(self):
        p=self.plan();opened=self.g.opened
        self.config['enable_experimental_writes']=False
        with self.assertRaises(PermissionError):self.service.apply(p['plan_id'],p['confirmation'])
        self.config['enable_experimental_writes']=True;self.config['allowed_write_targets']=[]
        with self.assertRaises(PermissionError):self.service.apply(p['plan_id'],p['confirmation'])
        self.config['allowed_write_targets']=[dict(channel=2,scene=s,addresses=[0,63]) for s in (8,15)]
        self.config['gateway_ip']='127.0.0.2'
        with self.assertRaisesRegex(ValueError,'gateway binding'):self.service.apply(p['plan_id'],p['confirmation'])
        self.assertEqual(self.g.opened,opened)

    def test_uncertain_write_stops_and_explicit_restore_uses_original_not_native_target(self):
        p=self.plan();self.g.fail_write=True
        result=self.service.apply(p['plan_id'],p['confirmation'])
        self.assertEqual(result['state'],'stopped_partial');self.assertEqual(len(self.g.writes),1)
        with self.assertRaises(ValueError):self.service.apply(p['plan_id'],p['confirmation'])
        self.g.fail_write=False
        undo=self.service.preview_scene_restore(p['plan_id'])
        self.assertEqual(len(undo['rows']),1)
        self.assertEqual(undo['rows'][0]['after']['level_raw'],0)
        self.assertEqual(self.service.apply(undo['plan_id'],undo['confirmation'])['state'],'completed')


if __name__=='__main__':unittest.main()
