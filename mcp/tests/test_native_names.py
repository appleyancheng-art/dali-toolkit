import copy
import json
import tempfile
import unittest
from unittest.mock import patch
from dali_mcp.service import Service
from dali_mcp.native_names import prepare_name_document
from test_native_cache import document


class NativeNameTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.service=Service(dict(gateway_ip='127.0.0.1',expected_dali_mac='02:00:00:00:00:01',expected_firmware='1.6.9'),self.tmp.name)
        self.open_patch=patch.object(self.service,'_open',side_effect=AssertionError('name drafts must never access bus'))
        self.open_patch.start();self.doc=document();self.cache=self.service.import_configmaster_cache(self.doc)

    def tearDown(self):self.open_patch.stop();self.tmp.cleanup()

    def plan(self,changes=None):
        return self.service.prepare_configmaster_names(self.cache['cache_id'],changes or
            [dict(kind='scene',channel=2,group=0,scene=15,name='测试名称')])

    def test_three_name_kinds_preserve_every_other_field(self):
        changes=[dict(kind='lamp',channel=2,address=63,name='末灯'),dict(kind='group',channel=2,group=15,name='末组改名'),
                 dict(kind='scene',channel=2,group=15,scene=0,name='独立场景')]
        plan=self.plan(changes);result=self.service.get_configmaster_name_plan(plan['plan_id'],True)
        draft=result['draft_document'];expected=copy.deepcopy(self.doc)
        expected['zCh2']['addresses']['63']['name']='末灯';expected['zCh2']['groupNames']['15']='末组改名'
        expected['zCh2']['sceneNamesByGroup']['15']['0']='独立场景'
        self.assertEqual(draft,expected)
        self.assertEqual(self.service.get_configmaster_cache(self.cache['cache_id'],True)['document'],self.doc)
        self.assertFalse(result['application_file_written']);self.assertEqual(result['device_commands_sent'],0)

    def test_empty_label_and_missing_label_are_distinct(self):
        plan=self.plan([dict(kind='group',channel=2,group=1,name=''),dict(kind='lamp',channel=2,address=63,name='')])
        self.assertEqual(plan['diff'][0]['before'],dict(present=False,name=None))
        self.assertEqual(plan['diff'][0]['after'],dict(present=True,name=''))
        self.assertTrue(plan['diff'][0]['changed']);self.assertFalse(plan['diff'][1]['changed'])

    def test_utf8_limits_reject_truncation_and_allow_exact_boundary(self):
        for value in ('灯'*10,'a'*30,'💡'*7):self.plan([dict(kind='lamp',channel=2,address=0,name=value)])
        for value in ('灯'*11,'a'*31,'💡'*8,'a\nb','\ud800',True):
            with self.assertRaises(ValueError):self.plan([dict(kind='lamp',channel=2,address=0,name=value)])

    def test_invalid_indices_duplicate_paths_and_extra_fields_rejected(self):
        good=dict(kind='scene',channel=2,group=0,scene=15,name='test')
        invalid=[[dict(good,channel=True)],[dict(good,scene=16)],[dict(good,group='0')],
                 [dict(good,address=0)],[dict(good,kind='gateway')],[good,good],
                 [dict(kind='lamp',channel=2,address=1,name='missing')]]
        for edits in invalid:
            with self.assertRaises(ValueError):self.plan(edits)
        with self.assertRaises(ValueError):prepare_name_document(self.doc,[])

    def test_rebase_keeps_unrelated_current_parameter_and_name_changes(self):
        plan=self.plan();current=copy.deepcopy(self.doc)
        current['zCh1']['addresses']['0']['name']='其他灯新名'
        current['zCh2']['addresses']['0']['scenes'][0]['level']=254
        current['future_data']['new']='preserve'
        result=self.service.rebase_configmaster_names(plan['plan_id'],current)
        expected=copy.deepcopy(current);expected['zCh2']['sceneNamesByGroup']['0']['15']='测试名称'
        self.assertEqual(result['state'],'draft_ready');self.assertEqual(result['draft_document'],expected)
        self.assertNotEqual(current,expected)

    def test_conflict_blocks_entire_draft_even_with_other_safe_edits(self):
        plan=self.plan([dict(kind='lamp',channel=2,address=0,name='new'),dict(kind='scene',channel=2,group=0,scene=15,name='new')])
        current=copy.deepcopy(self.doc);current['zCh2']['sceneNamesByGroup']['0']['15']='第三个名称'
        result=self.service.rebase_configmaster_names(plan['plan_id'],current)
        self.assertEqual(result['state'],'conflict');self.assertNotIn('draft_document',result)
        self.assertEqual(len(result['conflicts']),1)
        self.assertEqual(current['zCh2']['addresses']['0']['name'],self.doc['zCh2']['addresses']['0']['name'])

    def test_already_applied_target_is_noop(self):
        plan=self.plan();current=copy.deepcopy(self.doc);current['zCh2']['sceneNamesByGroup']['0']['15']='测试名称'
        result=self.service.rebase_configmaster_names(plan['plan_id'],current)
        self.assertEqual(result['draft_document'],current)
        self.assertEqual(result['resolutions'][0]['state'],'already_current')

    def test_removed_lamp_is_not_recreated(self):
        plan=self.plan([dict(kind='lamp',channel=2,address=63,name='末灯')])
        current=copy.deepcopy(self.doc);del current['zCh2']['addresses']['63']
        result=self.service.rebase_configmaster_names(plan['plan_id'],current)
        self.assertEqual(result['conflicts'][0]['reason'],'lamp_removed_from_cache')
        self.assertNotIn('draft_document',result)

    def test_missing_group_namespace_can_be_added_without_changing_other_groups(self):
        plan=self.plan([dict(kind='scene',channel=2,group=8,scene=8,name='八号')])
        result=self.service.rebase_configmaster_names(plan['plan_id'],self.doc)
        self.assertEqual(result['draft_document']['zCh2']['sceneNamesByGroup']['8'],{'8':'八号'})
        self.assertEqual(result['draft_document']['zCh2']['sceneNamesByGroup']['0'],self.doc['zCh2']['sceneNamesByGroup']['0'])

    def test_changed_identity_plan_corruption_and_rehashed_diff_rejected(self):
        plan=self.plan();current=copy.deepcopy(self.doc);current['ip']='127.0.0.2'
        with self.assertRaises(ValueError):self.service.rebase_configmaster_names(plan['plan_id'],current)
        path=self.service.state_dir/f"native-name-plan-{plan['plan_id']}.json"
        changed=copy.deepcopy(plan);changed['diff'][0]['path']=['zCh1','addresses','0','name']
        changed['plan_digest']=self.service._name_digest(changed)
        path.write_text(json.dumps(changed),encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'validated intent'):self.service.get_configmaster_name_plan(plan['plan_id'])

    def test_plan_does_not_alias_input_and_lookup_stays_in_namespace(self):
        edits=[dict(kind='scene',channel=2,group=0,scene=15,name='new')]
        plan=self.plan(edits);edits[0]['name']='changed later'
        self.assertEqual(self.service.get_configmaster_name_plan(plan['plan_id'])['changes'][0]['name'],'new')
        with self.assertRaises(ValueError):self.service.get_configmaster_name_plan('../outside')
        with self.assertRaises(ValueError):self.service.get_configmaster_name_plan(plan['plan_id'],1)


if __name__=='__main__':unittest.main()
