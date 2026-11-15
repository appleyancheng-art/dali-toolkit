import copy
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from dali_mcp.protocol import Gateway
from dali_mcp.service import Service
from dali_mcp.projects import digest, validate_document
from test_adapter import Emulator


class ProjectTests(unittest.TestCase):
    def setUp(self):
        self.em=Emulator();self.tmp=tempfile.TemporaryDirectory()
        self.em.parameter_values[0xf01d]=bytes([2,0,0,0])
        self.em.parameter_values[0xf01f]=bytes([254,0,0,0])
        self.config={'gateway_ip':'127.0.0.1','expected_dali_mac':'02:00:00:00:00:01',
                     'expected_firmware':'1.6.9','enable_experimental_writes':False}
        self.service=Service(self.config,self.tmp.name,lambda:Gateway('127.0.0.1','127.0.0.1',0,.15,self.em.port))

    def tearDown(self):
        self.em.stop();self.tmp.cleanup()

    def capture(self,addresses=None,scenes=None):
        return self.service.capture_project('test archive',2,addresses or [0,63],scenes or [0,15])

    def assert_read_only(self):
        self.assertFalse(self.em.live_commands or self.em.writes or self.em.recalls or self.em.scans)

    def test_fresh_complete_export_all_fields_and_number_boundaries(self):
        self.em.values[(2,63,15)]=(255,65535)
        self.em.group_masks[(2,63)]=(1<<15)|1
        doc=self.capture()
        self.assertEqual(doc['state'],'completed');self.assertFalse(doc['atomic_snapshot'])
        self.assertEqual(doc['rows'][1]['parameters']['groups']['value']['groups'],[0,15])
        self.assertEqual(doc['rows'][1]['scenes'][1]['level_raw'],255)
        self.assertEqual(doc['rows'][1]['scenes'][1]['kelvin_raw'],65535)
        self.assertEqual(len(doc['rows'][0]['parameters']),len(Gateway.PARAMETER_FIELDS))
        reopened=Service(self.config,self.tmp.name)
        self.assertEqual(reopened.get_project(doc['project_id']),doc)
        self.assertEqual(reopened.get_project()['projects'][0]['state'],'completed')
        self.assert_read_only()

    def test_all64_all16_capture_scope_without_truncation(self):
        # Real protocol reads for a fixture template; exercise the full cross-product
        # with a deterministic gateway to avoid thousands of redundant UDP tests.
        template=self.capture([0],[0])
        class FullGateway:
            def read_lamp_parameter(_,ch,a,field,typ):return copy.deepcopy(template['rows'][0]['parameters'][field])
            def read_scene(_,ch,a,scene,typ):return dict(channel=ch,address=a,scene=scene,light_type_code=typ,level_raw=255,kelvin_raw=65535)
            def close(_):pass
        with patch.object(self.service,'_open',return_value=(FullGateway(),template['gateway'])),patch.object(self.service,'_save',return_value='unused'):
            result=self.capture(list(range(64)),list(range(16)))
        self.assertEqual(result['state'],'completed')
        self.assertEqual(sum(len(row['scenes']) for row in result['rows']),1024)
        self.assertEqual({(row['address'],s['scene']) for row in result['rows'] for s in row['scenes']},
                         {(a,s) for a in range(64) for s in range(16)})
        validate_document(result);self.assert_read_only()

    def test_invalid_capture_scope_before_network(self):
        for ch,addresses,scenes in [(True,[0],[0]),(2,[True],[0]),(2,[0,0],[0]),(2,[0],[16]),(2,[],[0]),(2,[0],[])]:
            with self.assertRaises(ValueError):self.service.capture_project('test',ch,addresses,scenes)
        self.assertEqual(self.em.requests,[])

    def test_partial_capture_preserves_pending_and_cannot_restore_or_import(self):
        self.em.parameter_drop=0xf01f
        doc=self.capture([0],[0]);self.assertEqual(doc['state'],'stopped_partial')
        self.assertEqual(doc['pending'],{'address':0,'kind':'parameter','field':'max_level'})
        self.assertIn('min_level',doc['rows'][0]['parameters'])
        self.assertNotIn('max_level',doc['rows'][0]['parameters'])
        for operation in (lambda:self.service.import_project(doc),
                          lambda:self.service.diff_projects(doc['project_id'],doc['project_id']),
                          lambda:self.service.preview_project_scene_restore(doc['project_id'],[0],[0])):
            with self.assertRaises(ValueError):operation()
        self.assertEqual(self.service.get_project(doc['project_id'])['state'],'stopped_partial')
        self.assert_read_only()

    def test_progress_reads_do_not_wait_for_bus_lock(self):
        doc=self.capture([0],[0]);started=threading.Event();release=threading.Event()
        def holder():
            with self.service.lock:started.set();release.wait(2)
        t=threading.Thread(target=holder);t.start();self.assertTrue(started.wait(1))
        try:self.assertEqual(self.service.get_project(doc['project_id'])['state'],'completed')
        finally:release.set();t.join(2)

    def test_import_roundtrip_new_id_without_network_or_overwrite(self):
        doc=self.capture();count=len(self.em.requests)
        result=self.service.import_project(doc);restored=self.service.get_project(result['project_id'])
        self.assertNotEqual(result['project_id'],doc['project_id'])
        self.assertEqual(restored['source_digest'],doc['digest'])
        self.assertEqual(restored['origin'],'imported_unverified_provenance')
        self.assertEqual(restored['rows'],doc['rows'])
        self.assertEqual(self.service.get_project(doc['project_id']),doc)
        self.assertEqual(count,len(self.em.requests));self.assert_read_only()

    def test_corrupt_or_semantically_incomplete_documents_rejected(self):
        doc=self.capture()
        corrupt=copy.deepcopy(doc);corrupt['name']='changed'
        with self.assertRaisesRegex(ValueError,'checksum'):self.service.import_project(corrupt)
        mutations=[lambda d:d['rows'].pop(),lambda d:d['rows'][0]['scenes'].pop(),
                   lambda d:d.update(atomic_snapshot=True),lambda d:d.update(device_writes=True),
                   lambda d:d['rows'][0]['scenes'][0].update(scene=True),
                   lambda d:d['rows'][0]['scenes'][0].update(level_raw=256),
                   lambda d:d['rows'][0]['scenes'][0].update(address=1),
                   lambda d:d['rows'][0]['parameters'].pop('groups'),
                   lambda d:d['rows'][0]['parameters']['groups'].update(value={}),
                   lambda d:d['rows'][0]['parameters']['groups']['value'].update(group_mask=32768,groups=[]),
                   lambda d:d['rows'][0]['parameters']['min_level']['value'].update(level_raw=True),
                   lambda d:d['rows'][0]['parameters']['fade_time_rate']['value'].update(fade_rate_code=0),
                   lambda d:d['rows'][0]['parameters']['light_type']['value'].update(code=9),
                   lambda d:d['scope'].update(addresses=[0,0])]
        for mutate in mutations:
            bad=copy.deepcopy(doc);mutate(bad);bad['digest']=digest(bad)
            with self.assertRaises(ValueError):self.service.import_project(bad)
        with self.assertRaises(ValueError):self.service.get_project('../config')
        path=Path(self.tmp.name)/('project-'+doc['project_id']+'.json')
        path.write_text(json.dumps(corrupt),encoding='utf-8')
        self.assertEqual(len(self.service.get_project()['invalid_records']),1)
        self.assert_read_only()

    def test_scene_read_failure_reports_exact_pending_pair(self):
        self.em.parameter_drop=0xf03a
        doc=self.capture([63],[15])
        self.assertEqual(doc['state'],'stopped_partial')
        self.assertEqual(doc['pending'],{'address':63,'kind':'scene','scene':15})
        self.assertEqual(len(doc['rows'][0]['parameters']),len(Gateway.PARAMETER_FIELDS))
        self.assertEqual(doc['rows'][0]['scenes'],[])
        self.assert_read_only()

    def test_restore_large_pair_scope_and_unknown_stored_color_rejected(self):
        doc=self.capture(list(range(5)),list(range(16)))
        count=len(self.em.requests)
        with self.assertRaisesRegex(ValueError,'1..64 explicit pairs'):
            self.service.preview_project_scene_restore(doc['project_id'],list(range(5)),list(range(16)))
        self.assertEqual(count,len(self.em.requests))
        bad=copy.deepcopy(doc);bad['rows'][0]['scenes'][0]['kelvin_raw']=0;bad['digest']=digest(bad)
        imported=self.service.import_project(bad)
        with self.assertRaisesRegex(ValueError,'unsupported stored Kelvin'):
            self.service.preview_project_scene_restore(imported['project_id'],[0],[0])
        self.assertEqual(count,len(self.em.requests));self.assert_read_only()

    def test_diff_ignores_timestamps_and_reports_parameter_and_scene_changes(self):
        old=self.capture()
        same=self.capture()
        self.assertEqual(self.service.diff_projects(old['project_id'],same['project_id'])['differences'],[])
        self.em.parameter_values[0xf01d]=bytes([3,0,0,0]);self.em.values[(2,63,15)]=(0,65535)
        newer=self.capture()
        report=self.service.diff_projects(old['project_id'],newer['project_id'])
        self.assertTrue(report['same_scope'])
        self.assertEqual({(r['address'],r['kind'],r['field']) for r in report['differences']},
                         {(0,'parameter','min_level'),(63,'parameter','min_level'),(63,'scene',15)})
        self.assertTrue(all(r['state']=='changed' for r in report['differences']))
        self.assert_read_only()

    def test_diff_missing_coverage_is_not_zero_or_deletion(self):
        small=self.capture([0],[0]);big=self.capture()
        forward=self.service.diff_projects(small['project_id'],big['project_id'])
        self.assertFalse(forward['same_scope'])
        self.assertTrue(all(r['state']=='not_captured_before' for r in forward['differences']))
        backward=self.service.diff_projects(big['project_id'],small['project_id'])
        self.assertTrue(all(r['state']=='not_captured_after' for r in backward['differences']))
        self.assert_read_only()

    def test_other_gateway_import_archival_only(self):
        doc=self.capture();other=copy.deepcopy(doc);other['gateway']['ip']='192.0.2.2';other['digest']=digest(other)
        imported=self.service.import_project(other);count=len(self.em.requests)
        with self.assertRaises(ValueError):self.service.diff_projects(doc['project_id'],imported['project_id'])
        with self.assertRaises(ValueError):self.service.preview_project_scene_restore(imported['project_id'],[0],[0])
        self.assertEqual(len(self.em.requests),count);self.assert_read_only()

    def test_restore_plan_is_read_only_and_apply_uses_existing_gate_scope_backup(self):
        self.em.values[(2,63,15)]=(255,65535)
        doc=self.capture();self.em.values[(2,0,0)]=(80,3000)
        plan=self.service.preview_project_scene_restore(doc['project_id'],[0],[0])
        self.assertEqual(plan['source_project_id'],doc['project_id'])
        self.assertEqual(self.service.get_plan(plan['plan_id'])['source_project_id'],doc['project_id'])
        self.assertEqual(len(plan['rows']),1);self.assertEqual(plan['rows'][0]['before']['level_raw'],80)
        self.assert_read_only()
        with self.assertRaises(PermissionError):self.service.apply(plan['plan_id'],plan['confirmation'])
        self.config['enable_experimental_writes']=True
        self.config['allowed_write_targets']={'channel':2,'scene':15,'addresses':[0]}
        with self.assertRaises(PermissionError):self.service.apply(plan['plan_id'],plan['confirmation'])
        self.config['allowed_write_targets']={'channel':2,'scene':0,'addresses':[0]}
        original_replace=Path.replace;denied=[]
        def replace(path,target):
            if self.em.writes and not denied:
                denied.append(True)
                error=PermissionError('Windows publication temporarily busy');error.winerror=5
                raise error
            return original_replace(path,target)
        with patch.object(Path,'replace',replace):
            done=self.service.apply(plan['plan_id'],plan['confirmation'])
        self.assertEqual(done['state'],'completed')
        self.assertEqual(denied,[True]);self.assertEqual(len(self.em.writes),1)
        self.assertEqual(self.em.values[(2,0,0)],(254,4000))
        self.assertEqual(self.em.values[(2,63,15)],(255,65535))
        self.assertFalse(self.em.live_commands or self.em.recalls)
        with self.assertRaises(ValueError):self.service.apply(plan['plan_id'],plan['confirmation'])

    def test_restore_conflict_detected_without_writes(self):
        doc=self.capture([0],[8]);self.em.values[(2,0,8)]=(80,3000)
        plan=self.service.preview_project_scene_restore(doc['project_id'],[0],[8])
        self.config['enable_experimental_writes']=True;self.em.values[(2,0,8)]=(90,3000)
        with self.assertRaisesRegex(RuntimeError,'changed since preview'):self.service.apply(plan['plan_id'],plan['confirmation'])
        self.assert_read_only()

    def test_restore_current_limits_and_uncaptured_scope_rejected(self):
        doc=self.capture([0],[0]);count=len(self.em.requests)
        for addresses,scenes in [([1],[0]),([0],[1]),([True],[0])]:
            with self.assertRaises(ValueError):self.service.preview_project_scene_restore(doc['project_id'],addresses,scenes)
        self.assertEqual(len(self.em.requests),count)
        self.em.parameter_values[0xf01f]=bytes([180,0,0,0])
        with self.assertRaisesRegex(ValueError,'brightness outside'):self.service.preview_project_scene_restore(doc['project_id'],[0],[0])
        self.em.parameter_values[0xf01f]=bytes([255,0,0,0])
        with self.assertRaisesRegex(ValueError,'brightness limits are unknown'):self.service.preview_project_scene_restore(doc['project_id'],[0],[0])
        self.assert_read_only()

    def test_dt6_archive_diff_ignores_ct_and_restore_refused(self):
        self.em.lamp_type=0
        old=self.capture([0],[0]);self.assertEqual(old['state'],'completed')
        self.assertEqual(old['rows'][0]['parameters']['configured_min_ct']['state'],'not_applicable')
        self.em.values[(2,0,0)]=(254,6000)
        new=self.capture([0],[0])
        self.assertEqual(self.service.diff_projects(old['project_id'],new['project_id'])['differences'],[])
        with self.assertRaisesRegex(ValueError,'DT8 only'):self.service.preview_project_scene_restore(old['project_id'],[0],[0])
        self.assert_read_only()


if __name__=='__main__':unittest.main()
