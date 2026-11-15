import copy
import json
import tempfile
import unittest
from unittest.mock import patch
from dali_mcp.project_restore import ProjectRestoreReviewer
from dali_mcp.projects import digest
from dali_mcp.service import Service
from test_parameter_batch import MultiGateway


class ReviewGateway(MultiGateway):
    def __init__(self):
        super().__init__();self.reads=[];self.scene_hook=None
    def read_scene(self,c,a,s,t):
        self.reads.append((c,a,s))
        if self.scene_hook:self.scene_hook(a,s)
        return dict(channel=c,address=a,scene=s,light_type_code=1,level_raw=0,kelvin_raw=65535)
    def exchange(self,*args,**kwargs):
        raise AssertionError('read-only review must never dispatch writes')


class ProjectRestoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.g=ReviewGateway();self.info=dict(ip='127.0.0.1',dali_mac='02:00:00:00:00:01',dali_firmware='test')
        for lamp in self.g.lamps.values():lamp.parameters['groups']['value']=dict(groups=[0,1],group_mask=3)
        self.s=Service(dict(gateway_ip=self.info['ip'],expected_dali_mac=self.info['dali_mac'],expected_firmware='test'),self.tmp.name)
        self.c=ProjectRestoreReviewer(self.s)
        opener=patch.object(self.s,'_open',side_effect=lambda:(self.g,copy.deepcopy(self.info)))
        self.open=opener.start();self.addCleanup(opener.stop)
        self.doc=dict(format='dali-toolkit-project',schema_version=1,project_id='a'*32,name='full backup',
            state='completed',atomic_snapshot=False,device_writes=False,started_at=1,completed_at=2,
            gateway=copy.deepcopy(self.info),scope=dict(channel=2,addresses=[0,63],scenes=[0,15]),rows=[])
        self.populate([0,63],[0,15])

    def populate(self,addresses,scenes):
        self.doc['scope']=dict(channel=2,addresses=addresses,scenes=scenes)
        self.doc['rows']=[dict(channel=2,address=a,parameters=copy.deepcopy(self.g.lamps[a].parameters),
            scenes=[dict(channel=2,address=a,scene=s,light_type_code=1,level_raw=254,kelvin_raw=4000) for s in scenes]) for a in addresses]
        self.save()

    def save(self):
        self.doc['digest']=digest(self.doc);self.s._save('project',self.doc,self.doc['project_id'])

    def review(self,fields=None,groups=None,scenes=None):
        return self.c.preview(self.doc['project_id'],self.doc['scope']['addresses'],
            [] if fields is None else fields,[] if groups is None else groups,[0,15] if scenes is None else scenes)

    def test_combined_single_connection_exact_scope_and_no_write_plan(self):
        for row in self.doc['rows']:
            row['parameters']['fade_time_rate']['value'].update(raw=0x57,fade_time_code=5)
            old=row['parameters']['groups']['value'];old['group_mask']|=1<<15;old['groups']=sorted(set(old['groups'])|{15})
        self.save();r=self.review(['fade_time'],[15])
        self.assertEqual(r['state'],'reviewed');self.assertEqual(self.open.call_count,1);self.assertEqual(self.g.closed,1)
        self.assertEqual(r['counts']['parameters'],dict(selected=2,changed=2))
        self.assertEqual(r['counts']['scenes'],dict(selected=4,changed=4))
        self.assertEqual(r['counts']['groups'],dict(selected=2,changed=2))
        self.assertEqual([x['address'] for x in r['originals']],[0,63])
        self.assertEqual(self.g.reads,[(2,a,s) for a in (0,63) for s in (0,15)])
        self.assertNotIn('confirmation',r);self.assertNotIn('plan_id',r)
        self.assertFalse(r['write_execution_available']);self.assertFalse(self.g.writes)
        self.assertEqual(self.c.get(r['review_id']),r)

    def test_projected_limits_reject_scene_even_if_current_limits_allow_it(self):
        for row in self.doc['rows']:row['parameters']['max_level']['value']['level_raw']=200
        self.save();r=self.review(['max_level'])
        self.assertEqual(r['state'],'reviewed_with_rejections');self.assertEqual(r['counts']['rejections'],4)
        for row in r['rows']:
            self.assertTrue(all(s['fits_current_limits'] and not s['fits_projected_limits'] for s in row['scenes']))

    def test_projected_expanded_brightness_permits_scene_after_parameters(self):
        for lamp in self.g.lamps.values():lamp.parameters['max_level']['value']['level_raw']=200
        r=self.review(['max_level'])
        self.assertEqual(r['state'],'reviewed')
        self.assertEqual(r['suggested_category_order'],['parameters','scenes','groups'])
        for row in r['rows']:
            self.assertTrue(all(not s['fits_current_limits'] and s['fits_projected_limits'] for s in row['scenes']))

    def test_unsupported_curve_fade0_and_ct_expansion_are_not_executable(self):
        for field,value in [('dimming_curve',{'raw':2}),('configured_min_ct',{'kelvin_raw':2000})]:
            old=copy.deepcopy(self.doc['rows'][0]['parameters'][field])
            self.doc['rows'][0]['parameters'][field]['value']=value;self.save()
            r=self.review([field]);self.assertEqual(r['state'],'reviewed_with_rejections')
            self.doc['rows'][0]['parameters'][field]=old
        self.doc['rows'][0]['parameters']['fade_time_rate']['value'].update(raw=7,fade_time_code=0,extended_fade_not_read=True)
        self.save();self.assertEqual(self.review(['fade_time'])['state'],'reviewed_with_rejections')

    def test_unselected_group_bits_and_parameters_preserved(self):
        self.g.lamps[0].parameters['groups']['value']=dict(groups=[2,8],group_mask=(1<<2)|(1<<8))
        r=self.review([], [15],[])
        self.assertEqual(r['state'],'reviewed');self.assertFalse(self.g.reads)
        self.assertEqual(r['rows'][0]['projected_group_mask'],(1<<2)|(1<<8))
        self.assertEqual(r['counts']['parameters']['selected'],0)

    def test_invalid_scope_and_wrong_source_identity_rejected_before_network(self):
        for args in [([True],[],[],[0]),([1],[],[],[0]),([0],['bad'],[],[]),([0],[],[True],[]),
                     ([0],[],[],[]),([0],[],[],[1]),([0],['fade_time']*2,[],[])]:
            with self.assertRaises(ValueError):self.c.preview(self.doc['project_id'],*args)
        self.doc['gateway']['ip']='127.0.0.2';self.save()
        with self.assertRaises(ValueError):self.review()
        self.open.assert_not_called()

    def test_actual_gateway_mismatch_closes_without_reading(self):
        self.info['dali_firmware']='other';r=self.review()
        self.assertEqual(r['state'],'stopped_partial');self.assertFalse(self.g.reads);self.assertEqual(self.g.closed,1)

    def test_last_read_failure_preserves_originals_and_never_compares_partial(self):
        def fail(a,s):
            if (a,s)==(63,15):raise TimeoutError('lost final scene reply')
        self.g.scene_hook=fail;r=self.review()
        self.assertEqual(r['state'],'stopped_partial');self.assertEqual(r['rows'],[])
        self.assertEqual([len(x['scenes']) for x in r['originals']],[2,1])
        self.assertEqual(r['pending'],dict(address=63,category='scene',scene=15))
        self.assertEqual(self.g.closed,1);self.assertEqual(self.c.get(r['review_id']),r)

    def test_source_changed_during_capture_is_rejected_and_original_digest_retained(self):
        old=self.doc['digest']
        def changed(a,s):
            if (a,s)==(63,15):self.doc['name']='changed source';self.save()
        self.g.scene_hook=changed;r=self.review()
        self.assertEqual(r['state'],'stopped_partial');self.assertIn('source project changed',r['error'])
        self.assertEqual(r['source_project']['digest'],old)

    def test_all64_all16_scope_and_checksum_tampering(self):
        self.populate(list(range(64)),list(range(16)))
        r=self.review([],list(range(16)),list(range(16)))
        self.assertEqual(r['state'],'reviewed');self.assertEqual(r['counts']['scenes']['selected'],1024)
        self.assertEqual(r['counts']['groups']['selected'],1024)
        self.assertEqual(len(self.g.reads),1024);self.assertEqual(self.open.call_count,1)
        path=self.s.state_dir/f"project-restore-review-{r['review_id']}.json"
        raw=json.loads(path.read_text(encoding='utf-8'));raw['state']='fake';path.write_text(json.dumps(raw),encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'checksum'):self.c.get(r['review_id'])


if __name__=='__main__':unittest.main()
