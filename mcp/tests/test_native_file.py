import copy
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock,patch
from dali_mcp.service import Service
from dali_mcp.native_file import NativeFileController,canonical,parse,replace_native_file
from test_native_cache import document


class NativeFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.path=self.root/'gateway.json'
        self.original=b'\xef\xbb\xbf'+json.dumps(document(),ensure_ascii=False,indent=2).replace('\n','\r\n').encode()+b'\r\n'
        self.path.write_bytes(self.original)
        self.cfg=dict(gateway_ip='127.0.0.1',expected_dali_mac='02:00:00:00:00:01',expected_firmware='1.6.9',
                      configmaster_cache_file=str(self.path),enable_native_cache_writes=True)
        self.service=Service(self.cfg,self.root/'state')
        self.service._open=Mock(side_effect=AssertionError('native file transactions never access bus'))
        self.ctl=NativeFileController(self.service);self.ctl.guard=Mock()
        # Exercise the real Windows primitive on Windows; portable tests use rename.
        if os.name!='nt':self.ctl.replace=os.replace
        self.edits=[dict(kind='lamp',channel=2,address=0,name='入口灯'),
                    dict(kind='group',channel=2,group=15,name='展示组'),
                    dict(kind='scene',channel=2,group=0,scene=5,name='休闲')]

    def tearDown(self):self.service._open.assert_not_called();self.tmp.cleanup()

    def plan(self):return self.ctl.prepare(self.edits)
    def apply(self,plan):return self.ctl.apply(plan['plan_id'],plan['confirmation'])

    def test_roundtrip_preserves_other_fields_and_exact_original_bytes(self):
        plan=self.plan();self.assertEqual(self.path.read_bytes(),self.original)
        written=self.apply(plan);self.assertEqual(written['state'],'written_verified')
        expected=copy.deepcopy(document())
        expected['zCh2']['addresses']['0']['name']='入口灯'
        expected['zCh2']['groupNames']['15']='展示组'
        expected['zCh2']['sceneNamesByGroup']['0']['5']='休闲'
        self.assertEqual(parse(self.path.read_bytes()),expected)
        restore=self.ctl.prepare_restore(plan['plan_id'])
        self.assertEqual(self.apply(restore)['state'],'written_verified')
        self.assertEqual(self.path.read_bytes(),self.original)
        self.assertEqual(self.ctl.prepare_restore(plan['plan_id'])['state'],'already_restored')

    def test_read_imports_actual_file_without_application_guard_or_write(self):
        self.ctl.guard.side_effect=AssertionError('read should not require closing app')
        result=self.ctl.read()
        self.assertEqual(result['document_sha256'],self.service.get_configmaster_cache(result['cache_id'])['document_sha256'])
        self.assertFalse(result['application_file_written'])
        self.assertEqual(self.path.read_bytes(),self.original)

    def test_gate_confirmation_expiry_and_changed_binding_refuse_before_claim(self):
        plan=self.plan()
        self.cfg['enable_native_cache_writes']=False
        with self.assertRaisesRegex(ValueError,'gate'):self.apply(plan)
        self.cfg['enable_native_cache_writes']=1
        with self.assertRaisesRegex(ValueError,'gate'):self.apply(plan)
        self.cfg['enable_native_cache_writes']=True
        with self.assertRaisesRegex(ValueError,'confirmation'):self.ctl.apply(plan['plan_id'],'yes')
        with patch('dali_mcp.native_file.time.time',return_value=plan['expires_at']+1):
            with self.assertRaisesRegex(ValueError,'expired'):self.apply(plan)
        other=self.root/'other.json';other.write_bytes(self.original)
        self.cfg['configmaster_cache_file']=str(other)
        with self.assertRaisesRegex(ValueError,'path changed'):self.apply(plan)
        self.assertEqual(self.path.read_bytes(),self.original)
        self.assertFalse(self.ctl._record_path('claim',plan['plan_id']).exists())

    def test_app_running_rejects_prepare_apply_and_restore(self):
        self.ctl.guard.side_effect=RuntimeError('app running')
        with self.assertRaisesRegex(RuntimeError,'running'):self.plan()
        self.ctl.guard.side_effect=None;plan=self.plan()
        self.ctl.guard.side_effect=RuntimeError('app running')
        with self.assertRaisesRegex(RuntimeError,'running'):self.apply(plan)
        self.ctl.guard.side_effect=None;self.apply(plan)
        self.ctl.guard.side_effect=RuntimeError('app running')
        with self.assertRaisesRegex(RuntimeError,'running'):self.ctl.prepare_restore(plan['plan_id'])

    def test_changed_file_refuses_apply_and_restore_without_overwriting(self):
        plan=self.plan();changed=copy.deepcopy(document());changed['future_data']['new']='preserve'
        fresh=canonical(changed);self.path.write_bytes(fresh)
        with self.assertRaisesRegex(ValueError,'changed since'):self.apply(plan)
        self.assertEqual(self.path.read_bytes(),fresh)
        self.path.write_bytes(self.original);self.apply(plan)
        target=parse(self.path.read_bytes());target['zCh2']['addresses']['0']['name']='第三方改名'
        later=canonical(target);self.path.write_bytes(later)
        with self.assertRaisesRegex(ValueError,'overwrite newer name'):self.ctl.prepare_restore(plan['plan_id'])
        self.assertEqual(self.path.read_bytes(),later)

    def test_claim_survives_exception_no_replay(self):
        plan=self.plan();self.ctl.replace=Mock(side_effect=OSError('sharing violation'))
        result=self.apply(plan)
        self.assertEqual(result['state'],'not_written_verified')
        repeated=self.apply(plan)
        self.assertTrue(repeated['claimed']);self.assertEqual(self.ctl.replace.call_count,1)
        self.assertEqual(self.path.read_bytes(),self.original)

    def test_raised_after_actual_replace_is_read_back_and_recoverable(self):
        plan=self.plan();replace=self.ctl.replace
        def committed_then_error(source,target):replace(source,target);raise OSError('lost completion')
        self.ctl.replace=committed_then_error
        result=self.apply(plan)
        self.assertEqual(result['state'],'written_verified_with_error')
        self.ctl.replace=replace
        self.assertEqual(self.apply(self.ctl.prepare_restore(plan['plan_id']))['state'],'written_verified')
        self.assertEqual(self.path.read_bytes(),self.original)

    def test_before_replace_journal_failure_never_changes_app_file(self):
        plan=self.plan();self.ctl._journal=Mock(side_effect=OSError('disk full'))
        result=self.apply(plan)
        self.assertEqual(result['state'],'stopped_before_replace')
        self.assertIn('journal_error',result)
        self.assertEqual(self.path.read_bytes(),self.original)
        self.assertTrue(self.ctl.get(plan['plan_id'])['claimed'])

    def test_post_replace_journal_loss_keeps_durable_recovery_intent(self):
        plan=self.plan();journal=self.ctl._journal
        def fail_final(ident,report):
            if report['state']=='written_verified':raise OSError('disk full at completion')
            journal(ident,report)
        self.ctl._journal=fail_final
        result=self.apply(plan)
        self.assertEqual(result['state'],'written_verified');self.assertIn('journal_error',result)
        self.ctl._journal=journal
        self.assertEqual(self.ctl.get(plan['plan_id'])['execution']['state'],'replace_intent')
        restore=self.ctl.prepare_restore(plan['plan_id']);self.apply(restore)
        self.assertEqual(self.path.read_bytes(),self.original)

    def test_file_or_app_changes_during_staging_stop_before_replace(self):
        plan=self.plan();calls=0
        def change_on_second_guard():
            nonlocal calls
            calls+=1
            if calls==2:self.path.write_bytes(canonical(dict(document(),future_data={'changed':True})))
        self.ctl.guard=change_on_second_guard;self.ctl.replace=Mock()
        result=self.apply(plan)
        self.assertEqual(result['state'],'stopped_before_replace');self.ctl.replace.assert_not_called()
        self.assertTrue(parse(self.path.read_bytes())['future_data']['changed'])

    def test_new_file_value_after_replace_stays_uncertain_not_overwritten(self):
        plan=self.plan();changed=document();changed['zCh2']['addresses']['0']['name']='third party'
        later=canonical(changed)
        def third_party_write(source,target):target.write_bytes(later)
        self.ctl.replace=third_party_write
        result=self.apply(plan)
        self.assertEqual(result['state'],'write_uncertain')
        with self.assertRaisesRegex(ValueError,'overwrite newer'):self.ctl.prepare_restore(plan['plan_id'])
        self.assertEqual(self.path.read_bytes(),later)

    def test_restore_preserves_unrelated_new_fields_and_sibling_names(self):
        plan=self.plan();self.apply(plan)
        current=parse(self.path.read_bytes());current['lastSeen']='new timestamp'
        current['zCh2']['addresses']['63']['name']='其他灯的新名称'
        current['zCh2']['sceneNamesByGroup']['0']['6']='其他新场景'
        self.path.write_bytes(canonical(current))
        restore=self.ctl.prepare_restore(plan['plan_id'])
        self.assertFalse(restore['exact_original_bytes'])
        self.assertEqual(self.apply(restore)['state'],'written_verified')
        current['zCh2']['addresses']['0']['name']='灯零'
        current['zCh2']['groupNames']['15']='末组'
        del current['zCh2']['sceneNamesByGroup']['0']['5']
        self.assertEqual(parse(self.path.read_bytes()),current)

    def test_restore_created_namespace_prunes_only_empty_new_container(self):
        self.edits=[dict(kind='scene',channel=2,group=8,scene=0,name='new')]
        plan=self.plan();self.apply(plan)
        current=parse(self.path.read_bytes());current['zCh2']['sceneNamesByGroup']['8']['1']='keep'
        self.path.write_bytes(canonical(current));restore=self.ctl.prepare_restore(plan['plan_id'])
        self.apply(restore)
        self.assertEqual(parse(self.path.read_bytes())['zCh2']['sceneNamesByGroup']['8'],{'1':'keep'})
        second=self.plan();self.apply(second)
        latest=parse(self.path.read_bytes());del latest['zCh2']['sceneNamesByGroup']['8']['1']
        self.path.write_bytes(canonical(latest));self.apply(self.ctl.prepare_restore(second['plan_id']))
        # Group8 existed before the second write, so its empty container survives.
        self.assertEqual(parse(self.path.read_bytes())['zCh2']['sceneNamesByGroup']['8'],{})

    def test_restore_does_not_claim_unmodified_targets(self):
        self.edits=[dict(kind='lamp',channel=2,address=0,name='灯零'),dict(kind='scene',channel=2,group=0,scene=5,name='new')]
        plan=self.plan();self.apply(plan)
        current=parse(self.path.read_bytes());current['zCh2']['addresses']['0']['name']='later'
        self.path.write_bytes(canonical(current));self.apply(self.ctl.prepare_restore(plan['plan_id']))
        restored=parse(self.path.read_bytes())
        self.assertEqual(restored['zCh2']['addresses']['0']['name'],'later')
        self.assertNotIn('5',restored['zCh2']['sceneNamesByGroup']['0'])

    def test_tampered_backups_and_rehashed_non_name_edits_rejected(self):
        plan=self.plan();afterpath=self.ctl._record_path('after',plan['plan_id'],'bin')
        after=afterpath.read_bytes();afterpath.write_bytes(b'broken')
        with self.assertRaisesRegex(ValueError,'backup checksum'):self.apply(plan)
        afterpath.write_bytes(after)
        altered=parse(after);altered['zCh2']['addresses']['0']['sceneKnown']=100
        data=canonical(altered);afterpath.write_bytes(data)
        planpath=self.ctl._record_path('plan',plan['plan_id'])
        changed=json.loads(planpath.read_text(encoding='utf-8'));from dali_mcp.native_file import digest
        changed['after_sha256']=digest(data);changed['digest']=self.ctl._plan_digest(changed)
        planpath.write_bytes(canonical(changed))
        with self.assertRaisesRegex(ValueError,'scoped name edits'):self.ctl.get(plan['plan_id'])
        self.assertEqual(self.path.read_bytes(),self.original)

    def test_noop_does_not_rewrite_or_create_replace_intent(self):
        self.edits=[dict(kind='lamp',channel=2,address=0,name='灯零')]
        plan=self.plan();self.ctl.replace=Mock()
        self.assertEqual(self.apply(plan)['state'],'already_matches');self.ctl.replace.assert_not_called()
        self.assertEqual(self.path.read_bytes(),self.original)
        self.assertFalse(self.ctl._record_path('intent',plan['plan_id']).exists())

    def test_duplicate_json_unknown_identity_bad_paths_and_hardlinks_rejected(self):
        with self.assertRaisesRegex(ValueError,'duplicate'):parse(b'{"version":"1.2.4.1","version":"other"}')
        self.path.write_bytes(canonical(dict(document(),ip='127.0.0.2')))
        with self.assertRaisesRegex(ValueError,'identity'):self.plan()
        self.path.write_bytes(self.original)
        for path in ('relative.json',str(self.root/'..'/'escape.json'),str(self.root/'data.txt')):
            self.cfg['configmaster_cache_file']=path
            with self.assertRaises(ValueError):self.ctl.read()
        self.cfg['configmaster_cache_file']=str(self.path)
        alias=self.root/'alias.json';os.link(self.path,alias)
        with self.assertRaisesRegex(ValueError,'hard link'):self.ctl.read()

    def test_same_path_lock_serializes_different_state_directories(self):
        other=NativeFileController(Service(self.cfg,self.root/'state2'));other.guard=Mock()
        errors=[]
        def contender():
            try:other.prepare(self.edits)
            except Exception as exc:errors.append(exc)
        with self.ctl._locked(self.path):
            thread=threading.Thread(target=contender);thread.start();thread.join(5)
            self.assertFalse(thread.is_alive())
        self.assertEqual(len(errors),1);self.assertIsInstance(errors[0],RuntimeError)
        self.assertEqual(self.path.read_bytes(),self.original)


if __name__=='__main__':unittest.main()
