"""One JSON replacement for both local components, real temporary files."""
import copy,json,os,unittest
from pathlib import Path
from unittest.mock import Mock
from dali_mcp.native_file import canonical,parse,digest
from dali_mcp.project_names import ProjectNames
import test_native_floorplan_file as floor_tests
from test_project_floorplan import cache_fixture


class NativeProjectFileTests(unittest.TestCase):
    def setUp(self):
        floor_tests.NativeFloorplanFileTests.setUp(self)
        d=cache_fixture();d['zCh2']['addresses']['0']['name']='新灯名'
        d['zCh2']['groupNames']['15']='新组名';d['zCh2']['sceneNamesByGroup']['0']['15']='新备注'
        cache=self.s.import_configmaster_cache(d)['cache_id']
        self.names=[dict(kind='lamp',address=0),dict(kind='group',group=15),dict(kind='scene',group=0,scene=15)]
        self.project=ProjectNames(self.s).attach(self.project['project_id'],cache,self.names)
        self.cfg['enable_native_cache_writes']=True
    tearDown=floor_tests.NativeFloorplanFileTests.tearDown
    apply=floor_tests.NativeFloorplanFileTests.apply
    save_plan=floor_tests.NativeFloorplanFileTests.save_plan
    def plan(self):return self.ctl.prepare(self.project['project_id'],'captured_marks',self.names)

    def test_both_components_one_replace_exact_undo(self):
        p=self.plan();replace=self.ctl.replace;self.ctl.replace=Mock(side_effect=replace)
        self.assertEqual(self.apply(p)['state'],'written_verified');self.assertEqual(self.ctl.replace.call_count,1)
        d=parse(self.path.read_bytes());self.assertEqual(d['zCh2']['addresses']['0']['name'],'新灯名')
        self.assertEqual([m['addr'] for m in d['floorPlan']['marks']],[0,63])
        (self.s.state_dir/f"project-{self.project['project_id']}.json").unlink()
        u=self.ctl.prepare_restore(p['plan_id']);self.assertEqual(self.apply(u)['state'],'written_verified')
        self.assertEqual(self.path.read_bytes(),self.original);self.assertEqual(self.ctl.replace.call_count,2)
        self.assertTrue(Path(p['new_image_path']).exists());self.assertEqual(self.ctl.prepare_restore(p['plan_id'])['state'],'already_restored')

    def test_both_gates_required_forward_and_undo(self):
        p=self.plan()
        for gate in ('enable_native_cache_writes','enable_native_floorplan_writes'):
            self.cfg[gate]=False
            with self.assertRaisesRegex(ValueError,'gate'):self.apply(p)
            self.assertFalse(self.ctl._record_path('claim',p['plan_id']).exists());self.cfg[gate]=True
        self.apply(p);self.cfg['enable_native_cache_writes']=False
        # Preparation itself is non-writing and remains available with gates off.
        u=self.ctl.prepare_restore(p['plan_id'])
        with self.assertRaisesRegex(ValueError,'gate'):self.apply(u)
        self.cfg['enable_native_cache_writes']=True;self.apply(u);self.assertEqual(self.path.read_bytes(),self.original)

    def test_names_conflict_blocks_whole_undo_without_reverting_floorplan(self):
        p=self.plan();self.apply(p);d=parse(self.path.read_bytes());d['zCh2']['addresses']['0']['name']='第三方'
        current=canonical(d);self.path.write_bytes(current)
        with self.assertRaisesRegex(ValueError,'newer name'):self.ctl.prepare_restore(p['plan_id'])
        self.assertEqual(self.path.read_bytes(),current)

    def test_floorplan_conflict_blocks_whole_undo_without_reverting_names(self):
        p=self.plan();self.apply(p);d=parse(self.path.read_bytes());d['floorPlan']['marks'][0]['x']=.7
        current=canonical(d);self.path.write_bytes(current)
        with self.assertRaisesRegex(ValueError,'newer layout'):self.ctl.prepare_restore(p['plan_id'])
        self.assertEqual(self.path.read_bytes(),current)

    def test_undo_preserves_new_fields_and_unselected_names(self):
        p=self.plan();self.apply(p);d=parse(self.path.read_bytes());d['lastSeen']='new'
        d['zCh2']['addresses']['63']['name']='保持';self.path.write_bytes(canonical(d))
        self.apply(self.ctl.prepare_restore(p['plan_id']));expected=parse(self.original)
        expected['lastSeen']='new';expected['zCh2']['addresses']['63']['name']='保持'
        self.assertEqual(parse(self.path.read_bytes()),expected)

    def test_rehashed_unselected_name_and_false_diff_rejected(self):
        for corrupt in ('after','diff'):
            p=self.plan()
            if corrupt=='after':
                path=self.ctl._record_path('after',p['plan_id'],'bin');d=parse(path.read_bytes())
                d['zCh2']['addresses']['63']['name']='unselected';path.write_bytes(canonical(d));p['after_sha256']=digest(path.read_bytes())
            else:p['name_diff'][0]['after']['name']='wrong'
            p=self.save_plan(p)
            with self.assertRaises(ValueError):self.apply(p)
            self.assertEqual(self.path.read_bytes(),self.original)

    def test_late_permission_revocation_stops_both_changes(self):
        p=self.plan();stage=self.ctl._stage_dependencies
        def revoke(*args):stage(*args);self.cfg['enable_native_cache_writes']=False
        self.ctl._stage_dependencies=revoke
        self.assertEqual(self.apply(p)['state'],'stopped_before_replace');self.assertEqual(self.path.read_bytes(),self.original)

    def test_missing_or_outside_names_rejected_before_plan(self):
        for selections in ([],[dict(kind='lamp',address=63)],[dict(kind='lamp',address=1)]):
            with self.assertRaises(ValueError):self.ctl.prepare(self.project['project_id'],'captured_marks',selections)
        self.assertEqual(self.path.read_bytes(),self.original)

    def test_loss_after_commit_preserves_undo_evidence(self):
        p=self.plan();replace=self.ctl.replace
        def lost(a,b):replace(a,b);raise OSError('lost completion')
        self.ctl.replace=lost;self.assertEqual(self.apply(p)['state'],'written_verified_with_error')
        self.assertTrue(self.apply(p)['claimed']);self.ctl.replace=replace
        self.assertEqual(self.apply(self.ctl.prepare_restore(p['plan_id']))['state'],'written_verified')
        self.assertEqual(self.path.read_bytes(),self.original)


if __name__=='__main__':unittest.main()
