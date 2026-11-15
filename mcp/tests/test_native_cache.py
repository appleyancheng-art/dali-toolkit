import copy
import json
import tempfile
import unittest
from unittest.mock import patch
from dali_mcp.native_cache import inspect_configmaster_cache
from dali_mcp.service import Service


def document():
    channel=dict(addresses={'0':{'name':'灯零','sceneKnown':0,'scenes':[{'level':0,'colorTemp':65535}]*16},
                            '63':{'name':'','sceneKnown':65535}},
                 groupNames={'0':'全部','15':'末组'},sceneNamesByGroup={'0':{'0':'明亮','15':'末场景'},'15':{'0':'独立名称'}},
                 devicesInGroup=['FFFFFFFFFFFFFFFF']+['0000000000000000']*15,
                 groupsPerAddr=[[1,0]]*64)
    return dict(version='1.2.4.1',ip='127.0.0.1',dalimac='02:00:00:00:00:01',gwVersion='1.6.9',
                devName='Synthetic gateway',zCh1=copy.deepcopy(channel),zCh2=copy.deepcopy(channel),
                future_data={'unchanged':[1,'中文',None]})


class NativeCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.service=Service(dict(gateway_ip='127.0.0.1',expected_dali_mac='02:00:00:00:00:01',
                                  expected_firmware='1.6.9'),self.tmp.name)
        self.open_patch=patch.object(self.service,'_open',side_effect=AssertionError('cache operations must not access bus'))
        self.open_patch.start()

    def tearDown(self):self.open_patch.stop();self.tmp.cleanup()

    def test_labels_keep_channel_group_scene_zero_based_scopes(self):
        d=document();d['zCh2']['sceneNamesByGroup']['0']['0']='通道二'
        result=inspect_configmaster_cache(d)
        first,second=result['channels']
        self.assertEqual(first['cached_address_keys'],[0,63])
        self.assertEqual(first['group_names']['15'],'末组')
        self.assertEqual(first['scene_names_by_group']['15']['0'],'独立名称')
        self.assertEqual(second['scene_names_by_group']['0']['0'],'通道二')
        self.assertEqual(first['scene_names_by_group']['0']['0'],'明亮')
        self.assertEqual(first['lamp_names']['63'],'')

    def test_import_get_export_preserves_full_document_without_network(self):
        d=document();original=copy.deepcopy(d)
        imported=self.service.import_configmaster_cache(d);d['future_data']['unchanged'].append(4)
        self.assertNotIn('document',imported)
        record=self.service.get_configmaster_cache(imported['cache_id'],True)
        self.assertEqual(record['document'],original)
        self.assertEqual(len(self.service.get_configmaster_cache()['caches']),1)
        self.assertEqual(list(self.service.state_dir.glob('capability*')),[])
        self.assertEqual(list(self.service.state_dir.glob('plan-*')),[])

    def test_zero_placeholders_and_cache_markers_never_become_live_targets(self):
        result=inspect_configmaster_cache(document())
        self.assertFalse(result['online_addresses_verified'] or result['device_values_verified'] or result['scene_targets_created'])
        self.assertEqual(result['channels'][0]['scene_known_raw_by_address'],{'0':0,'63':65535})
        self.assertNotIn('scenes',result['channels'][0])
        self.assertNotIn('live_arguments',result)
        self.assertEqual(result['trust'],'local_cache_unverified')

    def test_missing_marker_remains_unknown(self):
        d=document();d['zCh2']['addresses']['0'].pop('sceneKnown')
        self.assertIsNone(inspect_configmaster_cache(d)['channels'][1]['scene_known_raw_by_address']['0'])

    def test_rejects_wrong_version_identity_and_non_gateway_exports(self):
        for change in [dict(version='1.2.5'),dict(ip='127.0.0.2'),dict(dalimac='02:00:00:00:00:02'),dict(gwVersion='1.7.0')]:
            d=document();d.update(change)
            with self.assertRaises(ValueError):self.service.import_configmaster_cache(d)
        for d in [[],{}, {'gateways':[document()]}]:
            with self.assertRaises(ValueError):self.service.import_configmaster_cache(d)
        self.assertFalse(list(self.service.state_dir.iterdir()))

    def test_rejects_ambiguous_indices_ranges_and_malformed_labels(self):
        mutations=[lambda d:d['zCh1']['addresses'].update({'00':{'name':'x'}}),
                   lambda d:d['zCh1']['addresses'].update({'64':{'name':'x'}}),
                   lambda d:d['zCh1']['groupNames'].update({'16':'x'}),
                   lambda d:d['zCh1']['sceneNamesByGroup']['0'].update({'-1':'x'}),
                   lambda d:d['zCh1']['sceneNamesByGroup'].update({'16':{}}),
                   lambda d:d['zCh1']['addresses']['0'].update(name=42),
                   lambda d:d['zCh1']['groupNames'].update({'0':'a\0b'}),
                   lambda d:d['zCh1']['addresses']['0'].update(sceneKnown=True),
                   lambda d:d['zCh1']['addresses']['0'].update(sceneKnown=65536)]
        for mutate in mutations:
            d=document();mutate(d)
            with self.assertRaises(ValueError):inspect_configmaster_cache(d)

    def test_oversize_nonfinite_and_invalid_unicode_rejected(self):
        for value in ['x'*2_000_001,float('nan'),'\ud800']:
            d=document();d['extra']=value
            with self.assertRaises(ValueError):inspect_configmaster_cache(d)

    def test_integrity_check_covers_original_and_projection(self):
        for key in ('document','summary'):
            record=self.service.import_configmaster_cache(document())
            path=self.service.state_dir/f"native-cache-{record['cache_id']}.json"
            stored=json.loads(path.read_text(encoding='utf-8'))
            stored[key]['unexpected']='altered';path.write_text(json.dumps(stored),encoding='utf-8')
            with self.assertRaisesRegex(ValueError,'checksum/summary'):self.service.get_configmaster_cache(record['cache_id'])

    def test_lookup_paths_scope_and_export_flag_are_checked(self):
        for ident in ('../outside','0'*31,'Z'*32):
            with self.assertRaises(ValueError):self.service.get_configmaster_cache(ident)
        with self.assertRaises(ValueError):self.service.get_configmaster_cache(include_document=True)
        with self.assertRaises(ValueError):self.service.get_configmaster_cache(include_document=1)
        record=self.service.import_configmaster_cache(document())
        self.service.config['gateway_ip']='127.0.0.2'
        with self.assertRaises(ValueError):self.service.get_configmaster_cache(record['cache_id'])


if __name__=='__main__':unittest.main()
