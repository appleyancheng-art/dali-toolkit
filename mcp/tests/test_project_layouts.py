"""Offline portable handover, never connects a physical gateway."""
import asyncio
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from mcp import Client
from mcp.client.stdio import StdioServerParameters
from dali_mcp.projects import digest, validate_document
from dali_mcp.service import Service
from test_parameter_planning import evidence
from project_report import render


def project_fixture():
    doc={'format':'dali-toolkit-project','schema_version':1,'project_id':'1'*32,'name':'Synthetic handover',
         'state':'completed','started_at':1,'completed_at':2,'origin':'synthetic_test_only',
         'gateway':dict(ip='127.0.0.1',dali_mac='02:00:00:00:00:01',dali_firmware='1.6.9'),
         'scope':dict(channel=2,addresses=[0,63],scenes=[0,15]),'atomic_snapshot':False,
         'device_writes':False,'not_included':['live_output','physical_layouts'],'rows':[]}
    for a in (0,63):
        doc['rows'].append(dict(channel=2,address=a,parameters=evidence(),scenes=[
            dict(channel=2,address=a,scene=s,light_type_code=1,level_raw=255,kelvin_raw=65535) for s in (0,15)]))
    doc['digest']=digest(doc)
    return validate_document(doc)


class ProjectLayoutTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.config=dict(gateway_ip='127.0.0.1',expected_dali_mac='02:00:00:00:00:01',expected_firmware='1.6.9')
        self.s=Service(self.config,self.tmp.name)
        self.net=patch.object(self.s,'_open',side_effect=AssertionError('offline workflow attempted network')).start()
        self.doc=project_fixture();self.s._save('project',self.doc,self.doc['project_id'])
        self.layout=self.s.create_layout('Corners',2,[[0,None],[None,63]])

    def tearDown(self):
        self.net.assert_not_called();patch.stopall();self.tmp.cleanup()

    def attach(self,layout=None):
        return self.s.attach_project_layouts(self.doc['project_id'],[(layout or self.layout)['layout_id']])

    def test_attach_immutable_self_contained_revision_preserves_capture_and_grid(self):
        result=self.attach()
        self.assertEqual(result['schema_version'],2)
        self.assertEqual(result['rows'],self.doc['rows'])
        self.assertEqual(result['started_at'],1);self.assertEqual(result['completed_at'],2)
        self.assertEqual(result['layouts'][0],self.layout)
        self.assertNotIn('physical_layouts',result['not_included'])
        self.assertEqual(self.s.get_project(self.doc['project_id']),self.doc)
        self.assertEqual(self.s.get_layout(self.layout['layout_id']),self.layout)
        self.assertNotEqual(result['project_id'],self.doc['project_id'])
        self.assertEqual(validate_document(json.loads(json.dumps(result))),result)

    def test_full_human_claim_retained_only_as_source_and_extraction_resets(self):
        verified=self.s.record_layout_check(self.layout['layout_id'],self.layout['digest'],[0,63],
                                            'Synthetic test attestation, no real board','test fixture')
        doc=self.attach(verified)
        with tempfile.TemporaryDirectory() as destination:
            other=Service(self.config,destination)
            with patch.object(other,'_open',side_effect=AssertionError('must be offline')) as opened:
                imported=other.import_project(doc);archived=other.get_project(imported['project_id'])
                self.assertTrue(archived['layouts'][0]['physical_mapping_verified'])
                self.assertEqual(archived['layout_context'],'source_snapshots_not_current_installation_verification')
                self.assertEqual(other.get_layout()['layouts'],[])
                local=other.import_project_layout(imported['project_id'],verified['layout_id'])['layout']
                self.assertNotEqual(local['layout_id'],verified['layout_id'])
                self.assertFalse(local['physical_mapping_verified'])
                self.assertNotIn('mapping_check',local)
                self.assertTrue(local['imported_from']['physical_mapping_verified_claim'])
                self.assertEqual(local['address_grid'],verified['address_grid'])
                self.assertEqual(other.get_project(imported['project_id']),archived)
                opened.assert_not_called()

    def test_scope_channel_and_gateway_mismatch_create_no_new_project(self):
        layouts=[self.s.create_layout('Outside',2,[[1]]),self.s.create_layout('Other channel',1,[[0]])]
        before=set(Path(self.tmp.name).glob('project-*'))
        for layout in layouts:
            with self.assertRaisesRegex(ValueError,'not captured'):self.attach(layout)
        self.config['expected_firmware']='other'
        with self.assertRaisesRegex(ValueError,'configured gateway'):self.attach()
        self.assertEqual(before,set(Path(self.tmp.name).glob('project-*')))

    def test_invalid_ids_empty_duplicates_and_too_many_rejected(self):
        ident=self.layout['layout_id']
        for ids in ([],[ident,ident],['../bad'],[True],[f'{i:032x}' for i in range(17)],'x'):
            with self.assertRaises(ValueError):self.s.attach_project_layouts(self.doc['project_id'],ids)
        for ident in (None,True,'../bad','0'*32):
            with self.assertRaises(ValueError):self.s.import_project_layout(self.doc['project_id'],ident)

    def test_nested_tampering_and_rehashed_invalid_geometry_are_rejected(self):
        doc=self.attach()
        mutations=[lambda d:d['layouts'][0]['address_grid'][0].__setitem__(0,1),
                   lambda d:d['layouts'][0].update(schema_version=True),
                   lambda d:d['layouts'][0].update(rows=True),
                   lambda d:d['layouts'][0]['gateway_binding'].update(expected_firmware='other'),
                   lambda d:d.update(layout_context='currently_verified'),
                   lambda d:d['layouts'].append(copy.deepcopy(d['layouts'][0]))]
        for mutation in mutations:
            for recalc_nested in (False,True):
                bad=copy.deepcopy(doc);mutation(bad)
                if recalc_nested:
                    for layout in bad['layouts']:layout['digest']=self.s._layout_digest(layout)
                bad['digest']=digest(bad)
                with self.assertRaises(ValueError):validate_document(bad)

    def test_v1_cannot_silently_hide_layouts_and_partial_projects_refused(self):
        bad=copy.deepcopy(self.doc);bad['layouts']=[self.layout];bad['digest']=digest(bad)
        with self.assertRaisesRegex(ValueError,'schema_version2'):validate_document(bad)
        bad=copy.deepcopy(self.doc);bad['state']='stopped_partial';bad.pop('digest')
        self.s._save('project',bad,bad['project_id'])
        with self.assertRaisesRegex(ValueError,'incomplete'):self.attach()

    def test_other_gateway_archive_is_readable_but_cannot_activate_mapping(self):
        doc=self.attach()
        with tempfile.TemporaryDirectory() as folder:
            other=Service(dict(self.config,expected_firmware='other'),folder)
            imported=other.import_project(doc)
            self.assertEqual(other.get_project(imported['project_id'])['layouts'],doc['layouts'])
            with self.assertRaisesRegex(ValueError,'different configured gateway'):
                other.import_project_layout(imported['project_id'],self.layout['layout_id'])
            self.assertEqual(other.get_layout()['layouts'],[])

    def test_diff_distinguishes_uncaptured_and_geometry_from_device_changes(self):
        a=self.attach()
        diff=self.s.diff_projects(self.doc['project_id'],a['project_id'])
        self.assertEqual(diff['differences'],[])
        self.assertEqual(diff['layout_comparison']['state'],'not_captured_before')
        same=self.attach()
        self.assertEqual(self.s.diff_projects(a['project_id'],same['project_id'])['layout_comparison']['state'],'unchanged')
        moved=self.s.create_layout('Corners',2,[[63,None],[None,0]])
        b=self.attach(moved)
        diff=self.s.diff_projects(a['project_id'],b['project_id'])
        self.assertEqual(diff['differences'],[])
        self.assertEqual(diff['layout_comparison']['state'],'changed')

    def test_replacing_bundle_creates_revision_without_changing_previous(self):
        a=self.attach()
        second=self.s.create_layout('Other view',2,[[63,0]])
        b=self.s.attach_project_layouts(a['project_id'],[second['layout_id']])
        self.assertEqual(len(b['layouts']),1)
        self.assertEqual(b['parent_project']['project_id'],a['project_id'])
        self.assertEqual(self.s.get_project(a['project_id'])['layouts'],[self.layout])

    def test_persistence_failure_does_not_mutate_sources(self):
        with patch.object(self.s,'_save',side_effect=OSError('disk full')):
            with self.assertRaises(OSError):self.attach()
        self.assertEqual(self.s.get_project(self.doc['project_id']),self.doc)
        self.assertEqual(self.s.get_layout(self.layout['layout_id']),self.layout)

    def test_report_exports_orientation_nulls_and_source_claim_boundary(self):
        report=render(self.attach())
        self.assertIn('0 .\n. 63',report)
        self.assertIn('不是当前现场已核对',report)
        self.assertIn('不继承为当前现场结论',report)


class ProjectLayoutStdioTests(unittest.TestCase):
    def test_real_stdio_archival_attach_export_import_and_extract_offline(self):
        async def run():
            root=Path(__file__).resolve().parents[1]
            with tempfile.TemporaryDirectory() as folder:
                config=Path(folder)/'config.json'
                config.write_text(json.dumps(dict(gateway_ip='127.0.0.1',expected_dali_mac='02:00:00:00:00:01',expected_firmware='1.6.9')),encoding='utf-8')
                async with Client(StdioServerParameters(command=sys.executable,args=[str(root/'run_server.py'),'--config',str(config)])) as client:
                    async def call(name,args):
                        r=await client.call_tool(name,args)
                        self.assertFalse(r.is_error,str(r.content));return r.structured_content
                    p=await call('dali_import_project',{'document':project_fixture()})
                    l=await call('dali_create_layout',dict(name='Corners',channel=2,address_grid=[[0,None],[None,63]]))
                    bundle=await call('dali_attach_project_layouts',dict(project_id=p['project_id'],layout_ids=[l['layout_id']]))
                    exported=await call('dali_get_project',dict(project_id=bundle['project_id']))
                    imported=await call('dali_import_project',dict(document=exported))
                    local=await call('dali_import_project_layout',dict(project_id=imported['project_id'],source_layout_id=l['layout_id']))
                    self.assertFalse(local['layout']['physical_mapping_verified'])
                    self.assertNotEqual(local['layout']['layout_id'],l['layout_id'])
                    self.assertEqual(local['layout']['address_grid'],l['address_grid'])
        asyncio.run(run())


if __name__=='__main__':unittest.main()
