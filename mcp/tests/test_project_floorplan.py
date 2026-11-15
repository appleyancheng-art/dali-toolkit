import base64
import copy
import hashlib
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch
import zlib

from dali_mcp.project_floorplan import inspect_png, IMAGE_LIMIT
from dali_mcp.projects import digest, validate_document
from dali_mcp.service import Service
from project_report import render
from test_native_cache import document
from test_project_layouts import project_fixture


def png(width=2,height=2,pixels=None):
    def chunk(k,v):return struct.pack('>I',len(v))+k+v+struct.pack('>I',zlib.crc32(k+v))
    raw=pixels if pixels is not None else (b'\0'+b'\xff\xff\xff'*width)*height
    return (b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',width,height,8,2,0,0,0))
            +chunk(b'IDAT',zlib.compress(raw))+chunk(b'IEND',b''))


def cache_fixture():
    d=document();d['floorPlan']=dict(image='background.png',marks=[
        dict(ch=2,addr=0,x=0.25,y=0.5),dict(ch=2,addr=63,x=1,y=1),
        dict(ch=1,addr=0,x=0,y=0)])
    return d


class ProjectFloorplanTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)
        self.config=dict(gateway_ip='127.0.0.1',expected_dali_mac='02:00:00:00:00:01',expected_firmware='1.6.9')
        self.s=Service(self.config,self.path/'state')
        self.net=patch.object(self.s,'_open',side_effect=AssertionError('floorplan must not connect')).start()
        self.doc=project_fixture();self.s._save('project',self.doc,self.doc['project_id'])
        self.cache=self.s.import_configmaster_cache(cache_fixture())
        self.image=self.path/'explicit-selected.png';self.image.write_bytes(png())

    def tearDown(self):
        self.net.assert_not_called();patch.stopall();self.tmp.cleanup()

    def attach(self,project_id=None):
        return self.s.attach_project_floorplan(project_id or self.doc['project_id'],self.cache['cache_id'],str(self.image))

    def test_inspection_preserves_legacy_cache_summary_and_no_image_read(self):
        before=self.s.get_configmaster_cache(self.cache['cache_id'],True)
        self.image.unlink()
        result=self.s.inspect_configmaster_floorplan(self.cache['cache_id'])
        self.assertEqual(result['mark_count'],3)
        self.assertEqual(self.s.get_configmaster_cache(self.cache['cache_id'],True),before)

    def test_immutable_attachment_embeds_exact_image_and_scope(self):
        attached=self.attach();p=self.s.get_project(attached['project_id'])
        self.assertEqual(p['schema_version'],3)
        self.assertEqual(p['rows'],self.doc['rows']);self.assertEqual(p['started_at'],1)
        self.assertEqual(self.s.get_project(self.doc['project_id']),self.doc)
        f=p['native_floorplan'];self.assertEqual(f['outside_capture_count'],1)
        self.assertEqual([m['address'] for m in f['selected_marks']],[0,63])
        self.assertEqual(base64.b64decode(f['image']['base64']),self.image.read_bytes())
        self.assertFalse(attached['device_writes'] or attached['physical_mapping_verified'])
        self.assertIn('原生平面图附件',render(p))

    def test_portable_roundtrip_without_cache_or_source_image(self):
        p=self.s.get_project(self.attach()['project_id'])
        self.image.unlink()
        for f in (self.path/'state').glob('native-cache-*'):f.unlink()
        other=Service(self.config,self.path/'new-state')
        with patch.object(other,'_open',side_effect=AssertionError('network')):
            ident=other.import_project(json.loads(json.dumps(p)))['project_id']
            f=other.get_project_floorplan(ident,True)
            self.assertEqual(base64.b64decode(f['image']['base64']),png())
            self.assertNotIn('base64',other.get_project_floorplan(ident)['image'])
            self.assertFalse(f['summary']['physical_positions_verified'])
        self.assertFalse(self.s.get_project_floorplan(self.doc['project_id'])['present'])

    def test_grid_and_floorplan_work_in_both_attachment_orders(self):
        grid=self.s.create_layout('Corners',2,[[0,63]])
        first=self.s.attach_project_layouts(self.doc['project_id'],[grid['layout_id']])
        second=self.s.get_project(self.attach(first['project_id'])['project_id'])
        self.assertEqual(second['layouts'],first['layouts'])
        third=self.s.attach_project_layouts(self.attach()['project_id'],[grid['layout_id']])
        self.assertEqual(third['schema_version'],3)
        self.assertEqual(third['native_floorplan'],second['native_floorplan'])
        self.assertFalse(self.s.import_project_layout(third['project_id'],grid['layout_id'])['layout']['physical_mapping_verified'])

    def test_gateway_mismatch_refuses_before_image_read(self):
        d=copy.deepcopy(self.doc);d['gateway']['dali_mac']='02:00:00:00:00:02';d['digest']=digest(d)
        self.s._save('project',d,d['project_id']);self.image.unlink()
        with self.assertRaisesRegex(ValueError,'gateway'):self.attach()

    def test_absent_or_outside_scope_refuses_no_new_project(self):
        count=len(list((self.path/'state').glob('project-*')))
        for native in (document(),cache_fixture()):
            if 'floorPlan' in native:native['floorPlan']['marks']=[dict(ch=1,addr=0,x=0,y=0)]
            self.cache=self.s.import_configmaster_cache(native)
            with self.assertRaises(ValueError):self.attach()
        self.assertEqual(len(list((self.path/'state').glob('project-*'))),count)

    def test_explicit_local_path_and_image_limits(self):
        for path in ('relative.png','//host/share/a.png',str(self.image.with_suffix('.jpg'))):
            with self.assertRaises(ValueError):self.s.attach_project_floorplan(self.doc['project_id'],self.cache['cache_id'],path)
        self.image.write_bytes(b'x'*(IMAGE_LIMIT+1))
        with self.assertRaises(ValueError):self.attach()

    def test_inner_tampering_rejected_even_with_new_outer_digest(self):
        original=self.s.get_project(self.attach()['project_id'])
        mutations=[lambda f:f['summary']['marks'][0].update(address=1),
                   lambda f:f['selected_marks'][0].update(address=1),
                   lambda f:f['selected_marks'][0].update(address=False),
                   lambda f:f.update(outside_capture_count=0),
                   lambda f:f.update(outside_capture_count=True),
                   lambda f:f['image'].update(sha256='0'*64),
                   lambda f:f['image'].update(base64='!'),
                   lambda f:f['source_document']['floorPlan']['marks'][0].update(x=0.7),
                   lambda f:f.update(context='verified'),
                   lambda f:f['summary'].update(physical_positions_verified=True)]
        for mutate in mutations:
            p=copy.deepcopy(original);mutate(p['native_floorplan']);p['digest']=digest(p)
            with self.assertRaises(ValueError):validate_document(p)

    def test_schema_downgrade_or_missing_attachment_rejected(self):
        p=self.s.get_project(self.attach()['project_id'])
        for schema in (1,2):
            d=copy.deepcopy(p);d['schema_version']=schema;d['digest']=digest(d)
            with self.assertRaises(ValueError):validate_document(d)
        p.pop('native_floorplan');p['digest']=digest(p)
        with self.assertRaises(ValueError):validate_document(p)

    def test_comparison_reports_image_or_selected_marker_changes(self):
        before=self.attach()['project_id']
        identical=self.attach()['project_id']
        self.assertEqual(self.s.diff_projects(before,identical)['native_floorplan_comparison']['state'],'unchanged')
        self.image.write_bytes(png(pixels=(b'\0'+b'\0'*6)*2))
        after=self.attach()['project_id']
        self.assertEqual(self.s.diff_projects(before,after)['native_floorplan_comparison']['state'],'changed')
        self.assertEqual(self.s.diff_projects(before,after)['differences'],[])

    def test_corrupt_png_crc_trailing_data_and_inflated_bomb_refused(self):
        bad=bytearray(png());bad[-1]^=1
        for data in (bytes(bad),png()+b'extra',png(pixels=b'\0'*100000),png(pixels=b'x'*14),png(8193,1)):
            with self.assertRaises(ValueError):inspect_png(data)
        self.assertEqual(inspect_png(png())['width'],2)

    def test_strict_include_image_flag(self):
        with self.assertRaises(ValueError):self.s.get_project_floorplan(self.doc['project_id'],1)


if __name__=='__main__':unittest.main()
