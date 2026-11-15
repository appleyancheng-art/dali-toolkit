import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from dali_mcp.service import Service
from dali_mcp.native_file import canonical, parse, digest
from dali_mcp.native_floorplan_file import NativeFloorplanFileController
from dali_mcp.projects import digest as project_digest
from test_native_cache import document
from test_project_floorplan import png, cache_fixture
from test_project_layouts import project_fixture


class NativeFloorplanFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.path=self.root/'gateways'/'gateway.json';self.path.parent.mkdir()
        self.original=b'\xef\xbb\xbf'+json.dumps(document(),ensure_ascii=False,indent=2).replace('\n','\r\n').encode()+b'\r\n'
        self.path.write_bytes(self.original)
        self.cfg=dict(gateway_ip='127.0.0.1',expected_dali_mac='02:00:00:00:00:01',expected_firmware='1.6.9',
                      configmaster_cache_file=str(self.path),enable_native_floorplan_writes=True)
        self.s=Service(self.cfg,self.root/'state');self.s._open=Mock(side_effect=AssertionError('no bus'))
        p=project_fixture();self.s._save('project',p,p['project_id'])
        c=self.s.import_configmaster_cache(cache_fixture())
        self.image=png();image_path=self.root/'source.png';image_path.write_bytes(self.image)
        self.project=self.s.attach_project_floorplan(p['project_id'],c['cache_id'],str(image_path))
        self.ctl=NativeFloorplanFileController(self.s);self.ctl.guard=Mock()
        if os.name!='nt':self.ctl.replace=os.replace

    def tearDown(self):self.s._open.assert_not_called();self.tmp.cleanup()
    def plan(self,scope='captured_marks'):return self.ctl.prepare(self.project['project_id'],scope)
    def apply(self,p):return self.ctl.apply(p['plan_id'],p['confirmation'])
    def save_plan(self,p):
        p.pop('confirmation',None);p.pop('application_file_written',None)
        p['digest']=self.ctl._plan_digest(p)
        self.ctl._record_path('plan',p['plan_id']).write_bytes(canonical(p))
        return dict(p,confirmation=self.ctl._confirmation(p))

    def test_roundtrip_scoped_marks_exact_bytes_and_retained_image(self):
        p=self.plan();self.assertEqual(self.path.read_bytes(),self.original)
        self.assertEqual(p['target_mark_count'],2)
        self.assertFalse(Path(p['new_image_path']).exists())
        result=self.apply(p);self.assertEqual(result['state'],'written_verified')
        after=parse(self.path.read_bytes());self.assertEqual([m['addr'] for m in after['floorPlan']['marks']],[0,63])
        expected=document();expected['floorPlan']=after['floorPlan'];self.assertEqual(after,expected)
        asset=Path(p['new_image_path']);self.assertEqual(asset.read_bytes(),self.image)
        undo=self.ctl.prepare_restore(p['plan_id']);self.assertTrue(undo['exact_original_bytes'])
        self.assertEqual(self.apply(undo)['state'],'written_verified')
        self.assertEqual(self.path.read_bytes(),self.original);self.assertEqual(asset.read_bytes(),self.image)
        self.assertEqual(self.ctl.prepare_restore(p['plan_id'])['state'],'already_restored')

    def test_all_source_marks_requires_explicit_selection(self):
        with self.assertRaises(ValueError):self.plan('all')
        p=self.plan('all_source_marks');self.assertEqual(p['target_mark_count'],3)
        self.assertEqual(self.apply(p)['state'],'written_verified')
        self.assertEqual(parse(self.path.read_bytes())['floorPlan']['marks'][-1]['ch'],1)

    def test_existing_background_archived_never_overwritten_and_restored(self):
        d=cache_fixture();d['floorPlan']['image']='old.PNG';self.path.write_bytes(canonical(d))
        directory=self.root/'floorplans';directory.mkdir();old=directory/'old.PNG';old.write_bytes(png(3,3))
        p=self.plan();self.assertEqual(p['original_asset']['state'],'present')
        self.assertEqual(self.ctl._record_path('original-image',p['plan_id'],'bin').read_bytes(),old.read_bytes())
        before=self.path.read_bytes();self.apply(p);self.apply(self.ctl.prepare_restore(p['plan_id']))
        self.assertEqual(self.path.read_bytes(),before);self.assertEqual(old.read_bytes(),png(3,3))

    def test_missing_original_background_preserved_as_missing(self):
        d=cache_fixture();self.path.write_bytes(canonical(d));before=self.path.read_bytes()
        p=self.plan();self.assertEqual(p['original_asset']['state'],'missing');self.apply(p)
        self.apply(self.ctl.prepare_restore(p['plan_id']));self.assertEqual(self.path.read_bytes(),before)
        self.assertFalse((self.root/'floorplans'/'background.png').exists())

    def test_undo_preserves_new_native_cache_fields(self):
        p=self.plan();self.apply(p);d=parse(self.path.read_bytes());d['lastSeen']='new refresh'
        d['future_data']={'native_update':True};self.path.write_bytes(canonical(d))
        undo=self.ctl.prepare_restore(p['plan_id']);self.assertFalse(undo['exact_original_bytes']);self.apply(undo)
        result=parse(self.path.read_bytes());self.assertNotIn('floorPlan',result)
        self.assertEqual(result['lastSeen'],'new refresh');self.assertEqual(result['future_data'],{'native_update':True})

    def test_changed_floorplan_refuses_undo(self):
        p=self.plan();self.apply(p);d=parse(self.path.read_bytes());d['floorPlan']['marks'][0]['x']=0.75
        current=canonical(d);self.path.write_bytes(current)
        with self.assertRaisesRegex(ValueError,'newer layout'):self.ctl.prepare_restore(p['plan_id'])
        self.assertEqual(self.path.read_bytes(),current)

    def test_changed_source_stops_before_image_and_never_replays(self):
        p=self.plan();d=self.s.get_project(self.project['project_id']);d['name']='changed';d['digest']=project_digest(d)
        self.s._save('project',d,d['project_id']);r=self.apply(p)
        self.assertEqual(r['state'],'stopped_before_replace');self.assertIn('source project changed',r['error'])
        self.assertFalse(Path(p['new_image_path']).exists());self.assertTrue(self.apply(p)['claimed'])

    def test_undo_uses_archived_project_after_live_project_deleted(self):
        p=self.plan();self.apply(p);(self.s.state_dir/f"project-{self.project['project_id']}.json").unlink()
        self.assertEqual(self.apply(self.ctl.prepare_restore(p['plan_id']))['state'],'written_verified')

    def test_generated_image_conflict_refuses_without_overwrite(self):
        p=self.plan();target=Path(p['new_image_path']);target.parent.mkdir();target.write_bytes(b'third party')
        r=self.apply(p);self.assertEqual(r['state'],'stopped_before_replace')
        self.assertEqual(target.read_bytes(),b'third party');self.assertEqual(self.path.read_bytes(),self.original)

    def test_image_tampered_during_staging_stops_json_commit(self):
        p=self.plan();count=0
        def guard():
            nonlocal count
            count+=1
            if count==2:Path(p['new_image_path']).write_bytes(png(3,3))
        self.ctl.guard=guard;self.ctl.replace=Mock();r=self.apply(p)
        self.assertEqual(r['state'],'stopped_before_replace');self.ctl.replace.assert_not_called()
        self.assertEqual(self.path.read_bytes(),self.original)

    def test_changed_old_image_refuses_before_write_and_undo(self):
        d=cache_fixture();self.path.write_bytes(canonical(d));directory=self.root/'floorplans';directory.mkdir()
        old=directory/'background.png';old.write_bytes(self.image);p=self.plan();old.write_bytes(png(3,3))
        self.assertEqual(self.apply(p)['state'],'stopped_before_replace')
        old.write_bytes(self.image);p=self.plan();self.apply(p);old.write_bytes(png(3,3))
        with self.assertRaisesRegex(ValueError,'original background changed'):self.ctl.prepare_restore(p['plan_id'])

    def test_independent_gate_confirmation_expiry_and_running_app(self):
        p=self.plan();self.cfg['enable_native_cache_writes']=True
        for value in (False,1,None):
            self.cfg['enable_native_floorplan_writes']=value
            with self.assertRaisesRegex(ValueError,'gate'):self.apply(p)
        self.cfg['enable_native_floorplan_writes']=True
        with self.assertRaisesRegex(ValueError,'confirmation'):self.ctl.apply(p['plan_id'],'yes')
        with patch('dali_mcp.native_file.time.time',return_value=p['expires_at']+1):
            with self.assertRaisesRegex(ValueError,'expired'):self.apply(p)
        self.ctl.guard.side_effect=RuntimeError('running')
        with self.assertRaisesRegex(RuntimeError,'running'):self.plan()
        with self.assertRaisesRegex(RuntimeError,'running'):self.apply(p)
        self.assertFalse(self.ctl._record_path('claim',p['plan_id']).exists())

    def test_uncertain_replace_recognizes_commit_and_undo(self):
        p=self.plan();replace=self.ctl.replace
        def lost_ack(a,b):replace(a,b);raise OSError('lost result')
        self.ctl.replace=lost_ack;r=self.apply(p);self.assertEqual(r['state'],'written_verified_with_error')
        self.ctl.replace=replace;self.assertEqual(self.apply(self.ctl.prepare_restore(p['plan_id']))['state'],'written_verified')

    def test_failed_json_replace_retains_image_and_prevents_replay(self):
        p=self.plan();self.ctl.replace=Mock(side_effect=OSError('sharing violation'))
        self.assertEqual(self.apply(p)['state'],'not_written_verified');self.apply(p)
        self.assertEqual(self.ctl.replace.call_count,1);self.assertEqual(Path(p['new_image_path']).read_bytes(),self.image)
        self.assertEqual(self.ctl.prepare_restore(p['plan_id'])['state'],'already_restored')

    def test_rehashed_plan_cannot_modify_unrelated_native_fields(self):
        p=self.plan();a=self.ctl._record_path('after',p['plan_id'],'bin');d=parse(a.read_bytes());d['future_data']={'bad':True}
        a.write_bytes(canonical(d));p['after_sha256']=digest(a.read_bytes());p=self.save_plan(p)
        with self.assertRaisesRegex(ValueError,'more than scoped'):self.apply(p)
        self.assertEqual(self.path.read_bytes(),self.original)

    def test_review_flags_and_source_backup_tamper_rejected(self):
        p=self.plan();p['existing_images_preserved']=1;p=self.save_plan(p)
        with self.assertRaisesRegex(ValueError,'review'):self.apply(p)
        p=self.plan();self.ctl._record_path('project',p['plan_id'],'bin').write_bytes(b'{}')
        with self.assertRaisesRegex(ValueError,'source backup'):self.apply(p)

    def test_shared_lock_with_names_controller(self):
        from dali_mcp.native_file import NativeFileController
        other=NativeFileController(self.s)
        with other._locked(self.path):
            with self.assertRaisesRegex(RuntimeError,'holds this cache path'):self.plan()

    def test_wrong_directory_and_gateway_rejected(self):
        other=self.root/'wrong.json';other.write_bytes(self.original);self.cfg['configmaster_cache_file']=str(other)
        with self.assertRaisesRegex(ValueError,'gateways'):self.plan()
        self.cfg['configmaster_cache_file']=str(self.path);d=document();d['ip']='127.0.0.2'
        self.path.write_bytes(canonical(d))
        with self.assertRaises(ValueError):self.plan()


if __name__=='__main__':unittest.main()
