import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from dali_mcp.authoring import percent_to_raw, PercentChange
from dali_mcp.protocol import Gateway
from dali_mcp.service import Service
from test_adapter import Emulator


class AuthoringTests(unittest.TestCase):
    def setUp(self):
        self.em=Emulator()
        self.em.parameter_values[0xf01d]=bytes([2,0,0,0])
        self.em.parameter_values[0xf01f]=bytes([254,0,0,0])
        self.tmp=tempfile.TemporaryDirectory()
        self.config={'gateway_ip':'127.0.0.1','expected_dali_mac':'02:00:00:00:00:01',
                     'expected_firmware':'1.6.9','enable_experimental_writes':False}
        self.service=Service(self.config,self.tmp.name,lambda:Gateway('127.0.0.1','127.0.0.1',0,.3,self.em.port))

    def tearDown(self):
        self.em.stop();self.tmp.cleanup()

    def layout(self,grid=None):
        return self.service.create_layout('test board',2,grid if grid is not None else [[8*c+r for c in range(8)] for r in range(8)])

    def assert_read_only(self):
        self.assertFalse(self.em.live_commands or self.em.writes or self.em.recalls or self.em.scans)

    def test_log_conversion_and_strict_inputs(self):
        for p,raw in [(0,0),(.1,1),(1,85),(20,195),(50,229),(100,254)]:
            self.assertEqual(percent_to_raw(p,0)[0],raw)
        self.assertEqual(percent_to_raw(0,99),(0,0))
        for p in [True,'50',float('nan'),float('inf'),-.1,.01,101]:
            with self.assertRaises(ValueError):percent_to_raw(p,0)
        for curve in [1,None,False,'0']:
            with self.assertRaises(ValueError):percent_to_raw(50,curve)
        for row in [{'address':True,'percent':20},{'address':0,'percent':True},
                    {'address':0,'percent':'20'},{'address':0,'kelvin_raw':True}]:
            with self.assertRaises(ValueError):PercentChange.model_validate(row)

    def test_layout_persists_without_gateway_and_keeps_orientation(self):
        with patch.object(self.service,'_open',side_effect=AssertionError('bus forbidden')):
            layout=self.layout()
            self.assertEqual(layout['address_grid'][0],[0,8,16,24,32,40,48,56])
            self.assertEqual(layout['address_grid'][7][7],63)
            reopened=Service(self.config,self.tmp.name)
            self.assertEqual(reopened.get_layout(layout['layout_id']),layout)
            self.assertEqual(len(reopened.get_layout()['layouts']),1)
            self.assertFalse(layout['physical_mapping_verified'])

    def test_layout_invalid_scope_and_corruption_rejected(self):
        for grid in [[],[[0,0]],[[True]],[[64]],[[0],[1,2]],[[None]],[[0]*65]]:
            with self.assertRaises(ValueError):self.layout(grid)
        with self.assertRaises(ValueError):self.service.get_layout('../config')
        layout=self.layout();path=Path(self.tmp.name)/('layout-'+layout['layout_id']+'.json')
        layout['channel']=1;path.write_text(json.dumps(layout),encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'checksum'):self.service.get_layout(layout['layout_id'])
        self.assertEqual(len(self.service.get_layout()['invalid_records']),1)
        self.assertEqual(self.em.requests,[])

    def test_layout_different_gateway_rejected_before_bus(self):
        layout=self.layout();self.config['expected_firmware']='9.9.9'
        with self.assertRaisesRegex(ValueError,'different configured gateway'):
            self.service.compose_layout_output(layout['layout_id'],100,'off',4000,'X')
        self.assertEqual(self.em.requests,[])

    def test_x_full_scope_and_scene_number_masks_without_controls(self):
        layout=self.layout()
        result=self.service.compose_layout_output(layout['layout_id'],100,'off',4000,'X',scene=6)
        expected=[0,7,9,14,18,21,27,28,35,36,42,45,49,54,56,63]
        self.assertEqual(result['foreground_addresses'],expected)
        self.assertEqual(result['addresses'],list(range(64)))
        for row in result['live_arguments']['changes']:
            self.assertEqual(row['level_raw'],254 if row['address'] in expected else 0)
            self.assertEqual(row.get('kelvin_raw'),4000 if row['address'] in expected else None)
        for row in result['scene_changes']:
            self.assertEqual(row['scene'],6)
            self.assertEqual(row['kelvin_raw'],4000 if row['address'] in expected else 65535)
        self.assertFalse(result['lighting_commands_sent'] or result['scene_storage_modified'])
        self.assert_read_only()

    def test_preserve_subset_and_preview_integration_no_scene_write(self):
        layout=self.layout([[0,7],[56,63]])
        result=self.service.compose_layout_output(layout['layout_id'],50,'preserve',4000,pattern_rows=['10','01'],scene=8)
        self.assertEqual(result['addresses'],[0,63])
        self.assertEqual(len(result['untouched_addresses']),62)
        self.assertTrue(all(r['scene']==8 for r in result['scene_changes']))
        self.assert_read_only()
        preview=self.service.set_live_output(**result['live_arguments'])
        self.assertTrue(preview['samples_verified'])
        self.assertEqual(self.em.live,{(2,0):(229,4000),(2,63):(229,4000)})
        self.assertFalse(self.em.writes or self.em.recalls)
        self.assertTrue(all(p[1][1]==1 for p in self.em.live_commands))

    def test_other_builtin_glyphs_have_explicit_column_major_addresses(self):
        layout=self.layout()
        sides=set(range(8))|set(range(56,64))
        expectations={'M':sides|{9,49,18,42,27,35},
                      'N':sides|{9,18,27,36,45,54},
                      'B':set(range(8))|{8,16,24,32,40,11,19,27,35,43,15,23,31,39,47,49,50,52,53,54}}
        for glyph,addresses in expectations.items():
            with self.subTest(glyph=glyph):
                result=self.service.compose_layout_output(layout['layout_id'],100,'preserve',6000,glyph)
                self.assertEqual(result['foreground_addresses'],sorted(addresses))
                self.assertEqual(result['addresses'],sorted(addresses))
        self.assert_read_only()

    def test_empty_cells_custom_pattern_and_masks(self):
        layout=self.layout([[0,None,63]])
        result=self.service.compose_layout_output(layout['layout_id'],20,'off',pattern_rows=['100'],scene=15)
        self.assertEqual(result['addresses'],[0,63])
        self.assertEqual(result['scene_changes'],[
            {'channel':2,'address':0,'scene':15,'level_raw':195,'kelvin_raw':65535},
            {'channel':2,'address':63,'scene':15,'level_raw':0,'kelvin_raw':65535}])
        self.assert_read_only()

    def test_invalid_patterns_fail_before_bus(self):
        layout=self.layout([[0,None]])
        invalid=[{'glyph':'X'}, {'pattern_rows':['01']}, {'pattern_rows':['00']},
                 {'pattern_rows':['1x']}, {'pattern_rows':['10'],'glyph':'M'},
                 {'pattern_rows':['10'],'foreground_percent':True},
                 {'pattern_rows':['10'],'background':'automatic'}, {'pattern_rows':['10'],'scene':16}]
        for override in invalid:
            args=dict(layout_id=layout['layout_id'],foreground_percent=50,background='off');args.update(override)
            with self.assertRaises(ValueError):self.service.compose_layout_output(**args)
        self.assertEqual(self.em.requests,[])

    def test_brightness_limits_and_profile_refresh(self):
        self.service.prepare_live_output(2,[{'address':0,'percent':100}])
        self.em.parameter_values[0xf01f]=bytes([180,0,0,0])
        refreshed=self.service.read_profiles(2,[0])
        self.assertEqual(refreshed['level_limits_rows'][0]['max_level_raw'],180)
        with self.assertRaisesRegex(ValueError,'outside configured limits'):
            self.service.prepare_live_output(2,[{'address':0,'percent':50}])
        with self.assertRaisesRegex(ValueError,'outside configured limits'):
            self.service.prepare_live_output(2,[{'address':1,'percent':.1}])
        self.assert_read_only()

    def test_unknown_curve_rejects_nonzero_but_off_and_ct_only_work(self):
        self.em.curve_code=1
        with self.assertRaisesRegex(ValueError,'curve0'):
            self.service.prepare_live_output(2,[{'address':0,'percent':50}])
        result=self.service.prepare_live_output(2,[{'address':0,'percent':0},{'address':63,'kelvin_raw':4000}],scene=0)
        self.assertEqual(result['scene_changes'][1]['level_raw'],255)
        self.assertEqual(result['scene_changes'][1]['scene'],0)
        self.assert_read_only()

    def test_all_off_scene_omits_live_ct_and_emits_storage_mask(self):
        result=self.service.prepare_live_output(2,[{'address':a,'percent':0} for a in range(64)],scene=1)
        self.assertEqual(result['live_arguments']['changes'],[{'address':a,'level_raw':0} for a in range(64)])
        self.assertEqual(result['scene_changes'],[{'channel':2,'address':a,'scene':1,'level_raw':0,'kelvin_raw':65535} for a in range(64)])
        self.assert_read_only()
        requests=len(self.em.requests)
        with self.assertRaises(ValueError):self.service.prepare_live_output(2,[{'address':0,'percent':0,'kelvin_raw':65535}],scene=1)
        self.assertEqual(len(self.em.requests),requests)

    def test_status_explicitly_does_not_check_connectivity(self):
        with patch.object(self.service,'_open',side_effect=AssertionError('must stay offline')):
            status=self.service.status()
        self.assertEqual(status['connectivity'],'not_checked');self.assertFalse(status['network_traffic'])
        self.assertEqual(self.em.requests,[])

    def test_stale_and_different_identity_cache_not_reused(self):
        self.service.prepare_live_output(2,[{'address':0,'percent':100}])
        for path in Path(self.tmp.name).glob('level-limits-*.json'):
            data=json.loads(path.read_text());data['captured_at']=time.time()-86401
            path.write_text(json.dumps(data))
        self.em.parameter_values[0xf01f]=bytes([180,0,0,0])
        with self.assertRaises(ValueError):self.service.prepare_live_output(2,[{'address':0,'percent':100}])
        for path in Path(self.tmp.name).glob('level-limits-*.json'):
            data=json.loads(path.read_text());data['captured_at']=time.time();data['gateway']['dali_firmware']='bad'
            path.write_text(json.dumps(data))
        with self.assertRaises(ValueError):self.service.prepare_live_output(2,[{'address':0,'percent':100}])
        self.assert_read_only()

    def test_invalid_preparation_and_partial_failure_never_control(self):
        for changes in [[],[{'address':0}],[{'address':0,'percent':50}]*2,[{'address':0,'percent':.01}]]:
            with self.assertRaises(ValueError):self.service.prepare_live_output(2,changes)
        self.assertEqual(self.em.requests,[])
        with self.assertRaises(ValueError):
            self.service.prepare_live_output(2,[{'address':0,'percent':50},{'address':1,'kelvin_raw':10000}])
        self.assert_read_only()
        self.config['expected_dali_mac']='02:00:00:00:00:02'
        with self.assertRaises(RuntimeError):self.service.prepare_live_output(2,[{'address':0,'percent':50}])
        self.assert_read_only()


if __name__=='__main__':unittest.main()
