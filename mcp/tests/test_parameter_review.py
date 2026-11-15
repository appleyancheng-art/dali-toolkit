import copy
import tempfile
import threading
import unittest
from unittest.mock import patch
from dali_mcp.parameter_review import ParameterReviewer
from dali_mcp.parameter_execution import ExperimentalParameterController
from dali_mcp.projects import digest
from dali_mcp.service import Service
from test_parameter_planning import evidence


class ReadGateway:
    """Only read operations exist: any attempt at control fails this fixture."""
    def __init__(self):
        self.values = {a: evidence() for a in range(64)}
        self.calls = []; self.closed = 0; self.fail = None

    def read_lamp_parameter(self, channel, address, field, typ):
        self.calls.append((channel, address, field))
        if (address, field) == self.fail:
            raise TimeoutError('simulated missing parameter')
        return copy.deepcopy(self.values[address][field])

    def read_actual(self, channel, address, color):
        self.calls.append((channel, address, 'actual'))
        return {'channel': channel, 'address': address, 'light_type_code': int(color), 'level_raw': 0, 'kelvin_raw': 4000}

    def close(self):
        self.closed += 1


class ParameterReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.g = ReadGateway()
        self.info = {'ip': '127.0.0.1', 'dali_mac': '02:00:00:00:00:01', 'dali_firmware': 'test'}
        self.service = Service({'gateway_ip': self.info['ip'], 'expected_dali_mac': self.info['dali_mac'],
                                'expected_firmware': 'test'}, self.tmp.name)
        self.open = patch.object(self.service, '_open', return_value=(self.g, self.info)).start()
        self.addCleanup(patch.stopall); self.addCleanup(self.tmp.cleanup)
        self.review = ParameterReviewer(self.service)

    def targets(self, addresses=(0,), changes=None):
        return [{'address': a, 'changes': changes or [{'field': 'fade_time', 'values': {'code': 5}}]} for a in addresses]

    def project(self):
        doc = {'format': 'dali-toolkit-project', 'schema_version': 1, 'project_id': 'a'*32, 'name': 'backup',
               'state': 'completed', 'atomic_snapshot': False, 'device_writes': False, 'started_at': 1, 'completed_at': 2,
               'gateway': copy.deepcopy(self.info), 'scope': {'channel': 2, 'addresses': [0,63], 'scenes': [8]},
               'rows': [{'channel': 2, 'address': a, 'parameters': evidence(), 'scenes': [
                   {'channel': 2, 'address': a, 'scene': 8, 'light_type_code': 1, 'level_raw': 254, 'kelvin_raw': 6000}]} for a in [0,63]]}
        doc['digest'] = digest(doc)
        self.service._save('project', doc, doc['project_id'])
        return doc

    def test_fresh_review_and_persistence_no_executable_plan(self):
        r = self.review.preview(2, self.targets())
        self.assertEqual(r['state'], 'reviewed')
        step = r['rows'][0]['proposal']['steps'][0]
        self.assertEqual((step['before'], step['after']), ({'code': 4}, {'code': 5}))
        self.assertFalse(r['device_writes'] or r['write_execution_available'])
        self.assertNotIn('confirmation', r)
        self.assertEqual(self.review.get(r['review_id']), r)
        self.assertEqual(self.g.closed, 1)
        self.assertEqual(list(self.service.state_dir.glob('parameter-plan-*')), [])
        with self.assertRaises(FileNotFoundError):
            ExperimentalParameterController(self.service)._load(r['review_id'])

    def test_full64_preserves_scope_and_paired_components(self):
        r = self.review.preview(2, self.targets(range(64), [{'field': 'power_on', 'values': {'level_raw': 100}}]))
        self.assertEqual(r['state'], 'reviewed'); self.assertEqual(len(r['rows']), 64)
        for row in r['rows']:
            self.assertEqual(row['proposal']['steps'][0]['after'], {'level_raw': 100, 'kelvin_raw': 65535})
        self.assertEqual({a for ch,a,f in self.g.calls}, set(range(64)))

    def test_invalid_scope_and_fields_reject_before_connection(self):
        bad = [(True,self.targets()), (2,self.targets([True])), (2,self.targets([64])), (2,self.targets([0,0])),
               (2,[]), (2,self.targets(changes=[{'field':'groups','values':{'code':1}}])),
               (2,self.targets(changes=[{'field':'fade_time','values':{'code':True}}])),
               (2,self.targets(changes=[{'field':'fade_time','values':{'code':5}}]*2))]
        for channel, targets in bad:
            with self.subTest(targets=targets), self.assertRaises(ValueError): self.review.preview(channel,targets)
        self.open.assert_not_called()

    def test_partial_read_preserves_fields_pending_and_no_retry(self):
        self.g.fail = (63,'max_level')
        r = self.review.preview(2,self.targets([0,63]))
        self.assertEqual(r['state'],'stopped_partial')
        self.assertEqual(r['pending'],{'address':63,'field':'max_level'})
        self.assertEqual(r['rows'][0]['state'],'reviewed')
        self.assertIn('min_level',r['rows'][1]['parameters'])
        self.assertNotIn('max_level',r['rows'][1]['parameters'])
        self.assertEqual(self.g.calls.count((2,63,'max_level')),1)
        self.assertEqual(self.review.get(r['review_id']),r)

    def test_constraints_reject_one_lamp_without_hiding_other_results(self):
        self.g.values[0]['configured_max_ct']['value']['kelvin_raw']=4000
        r = self.review.preview(2,self.targets([0,63],[{'field':'configured_max_ct','values':{'kelvin_raw':6000}}]))
        self.assertEqual(r['state'],'reviewed_with_rejections')
        self.assertEqual([v['state'] for v in r['rows']],['unsupported','reviewed'])
        self.assertIn('physical-limit',r['rows'][0]['error'])

    def test_project_selected_fields_keep_current_unselected_parameters(self):
        doc=self.project()
        self.g.values[63]['fade_time_rate']['value'].update(raw=0x52,fade_time_code=5,fade_rate_code=2)
        r=self.review.preview_project(doc['project_id'],[63],['fade_time'])
        self.assertEqual(r['state'],'reviewed'); self.assertEqual(len(r['rows']),1)
        proposal=r['rows'][0]['proposal']
        self.assertEqual([s['field'] for s in proposal['steps']],['fade_time'])
        self.assertEqual(proposal['steps'][0]['after'],{'code':4})
        self.assertEqual(proposal['source_parameters']['fade_time_rate']['value']['fade_rate_code'],2)
        self.assertEqual(r['source_project']['digest'],doc['digest'])
        self.assertEqual({a for ch,a,f in self.g.calls},{63})

    def test_project_unchanged_is_review_not_write(self):
        doc=self.project(); r=self.review.preview_project(doc['project_id'],[0],['power_on'])
        self.assertEqual(r['rows'][0]['proposal']['steps'],[])
        self.assertEqual(r['rows'][0]['proposal']['unchanged_fields'],['power_on'])

    def test_project_old_ct_limits_cannot_override_current_limits(self):
        doc=self.project(); self.g.values[0]['configured_max_ct']['value']['kelvin_raw']=5000
        r=self.review.preview_project(doc['project_id'],[0],['configured_max_ct'])
        self.assertEqual(r['state'],'reviewed_with_rejections')
        self.assertIn('physical-limit',r['rows'][0]['error'])

    def test_project_bad_identity_scope_and_incomplete_reject_before_reads(self):
        doc=self.project()
        for addresses,fields in [([1],['fade_time']),([0],[]),([0],['groups']),([0],['fade_time']*2)]:
            with self.assertRaises(ValueError): self.review.preview_project(doc['project_id'],addresses,fields)
        doc['gateway']['ip']='127.0.0.2'; doc['digest']=digest(doc)
        self.service._save('project',doc,doc['project_id'])
        with self.assertRaises(ValueError):self.review.preview_project(doc['project_id'],[0],['fade_time'])
        doc['state']='stopped_partial'; self.service._save('project',doc,doc['project_id'])
        with self.assertRaises(ValueError):self.review.preview_project(doc['project_id'],[0],['fade_time'])
        self.open.assert_not_called()

    def test_connected_identity_mismatch_stops_before_parameter_queries(self):
        doc=self.project(); self.info['dali_firmware']='changed'
        r=self.review.preview_project(doc['project_id'],[0],['fade_time'])
        self.assertEqual(r['state'],'stopped_partial'); self.assertEqual(self.g.calls,[])

    def test_current_type_change_rejected_not_assumed_dt8(self):
        doc=self.project(); self.g.values[0]['light_type']['value']={'code':0,'name':'DT6'}
        r=self.review.preview_project(doc['project_id'],[0],['fade_time'])
        self.assertEqual(r['state'],'reviewed_with_rejections')
        self.assertIn('DT8',r['rows'][0]['error'])

    def test_progress_read_does_not_wait_for_bus_and_rejects_path_ids(self):
        r=self.review.preview(2,self.targets()); started=threading.Event(); release=threading.Event()
        def holder():
            with self.service.lock: started.set(); release.wait(2)
        thread=threading.Thread(target=holder); thread.start(); self.assertTrue(started.wait(1))
        try:
            self.assertEqual(self.review.get(r['review_id']),r)
            self.assertEqual(self.review.get()['reviews'][0]['review_id'],r['review_id'])
            with self.assertRaises(ValueError):self.review.get('../bad')
        finally:release.set(); thread.join(2)


if __name__=='__main__':unittest.main()
