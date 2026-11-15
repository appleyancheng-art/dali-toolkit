import copy,json,tempfile,threading,unittest
from types import SimpleNamespace
from unittest.mock import patch
from dali_mcp.project_execution import ProjectRestoreController
from dali_mcp.projects import digest
from dali_mcp.service import Service
from test_parameter_batch import MultiGateway


class CombinedGateway(MultiGateway):
    def __init__(self):
        super().__init__();self.events=[];self.fault_category=None;self.after_event=None;self.scene_reads=0
        self.scenes={(a,s):dict(channel=2,address=a,scene=s,light_type_code=1,level_raw=0,kelvin_raw=65535) for a in range(64) for s in range(16)}
        for lamp in self.lamps.values():lamp.parameters['groups']['value']=dict(groups=[0,1],group_mask=3)
    def read_scene(self,c,a,s,t):
        assert (c,t)==(2,1);self.scene_reads+=1;return copy.deepcopy(self.scenes[a,s])
    def event(self,category,address):
        self.events.append((category,address))
        if self.after_event:self.after_event(category,address)
        if self.fault_category==category:
            self.fault_category=None;raise TimeoutError('lost receipt after applying '+category)
    def write_scene(self,target):
        a,s=target['address'],target['scene'];params=self.lamps[a].parameters
        level=target['level_raw'];ct=target['kelvin_raw']
        assert level in (0,255) or params['min_level']['value']['level_raw']<=level<=params['max_level']['value']['level_raw']
        assert ct==65535 or params['configured_min_ct']['value']['kelvin_raw']<=ct<=params['configured_max_ct']['value']['kelvin_raw']
        self.scenes[a,s]=copy.deepcopy(target);self.event('scene',a)
    def exchange(self,command,payload,route,accept):
        if command!=0xf015:
            result=super().exchange(command,payload,route,accept);self.event('parameter',payload[2]);return result
        assert payload[0]==2 and payload[1]==1 and payload[3] in (0x60,0x70)
        a,g=payload[2],payload[4];v=self.lamps[a].parameters['groups']['value'];mask=v['group_mask']
        mask=mask|(1<<g) if payload[3]==0x60 else mask&~(1<<g)
        v.update(group_mask=mask,groups=[n for n in range(16) if mask&(1<<n)])
        self.seq+=1;reply=SimpleNamespace(command=0xf095,sequence=self.seq,transport=1,route=route[3:]+route[:3],payload=payload[:3]+b'\xfe'+b'\0'*4)
        assert accept(reply);self.event('group',a);return reply


class ProjectExecutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.g=CombinedGateway()
        self.info=dict(ip='127.0.0.1',dali_mac='02:00:00:00:00:01',dali_firmware='test')
        self.s=Service(dict(gateway_ip=self.info['ip'],expected_dali_mac=self.info['dali_mac'],expected_firmware='test'),self.tmp.name)
        self.c=ProjectRestoreController(self.s)
        opener=patch.object(self.s,'_open',side_effect=lambda:(self.g,copy.deepcopy(self.info)))
        self.open=opener.start();self.addCleanup(opener.stop)
        self.original={a:copy.deepcopy(g.parameters) for a,g in self.g.lamps.items()};self.original_scenes=copy.deepcopy(self.g.scenes)
        self.doc=dict(format='dali-toolkit-project',schema_version=1,project_id='a'*32,name='combined target',
            state='completed',atomic_snapshot=False,device_writes=False,started_at=1,completed_at=2,
            gateway=copy.deepcopy(self.info),scope=dict(channel=2,addresses=[0,63],scenes=[0,15]),rows=[])
        for a in (0,63):
            params=copy.deepcopy(self.g.lamps[a].parameters)
            params['fade_time_rate']['value'].update(raw=0x57,fade_time_code=5)
            params['groups']['value']=dict(groups=[0,1,15],group_mask=32771)
            self.doc['rows'].append(dict(channel=2,address=a,parameters=params,
                scenes=[dict(channel=2,address=a,scene=s,light_type_code=1,level_raw=254,kelvin_raw=4000) for s in (0,15)]))
        self.save()

    def save(self):
        self.doc['digest']=digest(self.doc);self.s._save('project',self.doc,self.doc['project_id'])
    def plan(self,fields=None):
        return self.c.prepare(self.doc['project_id'],[0,63],['fade_time'] if fields is None else fields,[15],[0,15])
    def allow(self,p):
        cfg=self.s.config;steps=p['steps'];c=p['scope']['channel']
        cfg.update(enable_project_restore=True,allowed_project_restore_digest=p['digest'],enable_experimental_parameter_writes=True,
            enable_experimental_group_writes=True,enable_experimental_writes=True)
        cfg['allowed_parameter_batch_targets']=[dict(channel=c,address=a,values=[dict(field=s['field'],values=s['after']) for s in steps if s['category']=='parameter' and s['address']==a]) for a in (0,63)]
        cfg['allowed_group_write_operations']=[dict(channel=c,address=s['address'],group=s['group'],member=s['after']) for s in steps if s['category']=='group']
        cfg['allowed_write_targets']=[dict(channel=c,scene=s,addresses=[0,63]) for s in (0,15)]
    def apply(self,p,**kw):return self.c.execute(p['plan_id'],p['confirmation'],**kw)
    def assert_original(self):
        self.assertEqual({a:g.parameters for a,g in self.g.lamps.items()},self.original)
        self.assertEqual(self.g.scenes,self.original_scenes)
    def undo(self,p):
        u=self.c.prepare_restore(p['plan_id']);self.allow(u)
        self.assertEqual(self.apply(u)['state'],'completed_readback');self.assert_original();return u

    def test_all_categories_two_lamps_roundtrip_and_all_originals_before_writes(self):
        p=self.plan();self.assertEqual(len(p['steps']),8);self.assertFalse(self.g.events)
        with self.assertRaises(PermissionError):self.apply(p)
        self.allow(p)
        def evidence(category,a):
            run=self.c.get(p['plan_id'])['execution']
            self.assertEqual(len(run['fresh_before']),2)
            self.assertEqual(run['steps'][-1]['state'],'outcome_unknown')
        self.g.after_event=evidence;r=self.apply(p)
        self.assertEqual(r['state'],'completed_readback');self.assertEqual(r['completed_steps'],8)
        self.assertEqual([x[0] for x in self.g.events],['parameter']*2+['scene']*4+['group']*2)
        self.g.after_event=None;self.undo(p)
        self.assertEqual(self.c.prepare_restore(p['plan_id'])['state'],'already_original')

    def test_all_category_gates_checked_before_first_send(self):
        for disabled in ('enable_experimental_parameter_writes','enable_experimental_writes','enable_experimental_group_writes','enable_project_restore'):
            p=self.plan();self.allow(p);self.s.config[disabled]=False;n=self.open.call_count
            with self.assertRaises(PermissionError):self.apply(p)
            self.assertEqual(self.open.call_count,n);self.assertFalse(self.g.events)

    def test_last_address_scene_conflict_sends_nothing(self):
        p=self.plan();self.allow(p);self.g.scenes[63,15]['level_raw']=3
        self.assertEqual(self.apply(p)['state'],'preflight_failed');self.assertFalse(self.g.events)

    def test_original_out_of_limit_scene_cannot_be_replaced_without_restore_path(self):
        self.g.scenes[63,15]['level_raw']=1
        with self.assertRaisesRegex(ValueError,'original scene outside original limits'):self.plan()
        self.assertFalse(self.g.events)
        self.assertFalse(list(self.s.state_dir.glob('project-restore-plan-*.json')))

    def test_target_scene_limits_validate_after_parameters_and_restore_before_old_scenes(self):
        for a in (0,63):
            self.g.lamps[a].parameters['max_level']['value']['level_raw']=200
            self.original[a]=copy.deepcopy(self.g.lamps[a].parameters)
        p=self.plan(['max_level']);self.allow(p)
        self.assertEqual(self.apply(p)['state'],'completed_readback');self.undo(p)

    def test_lost_receipt_each_category_stops_once_then_recovers_attempted_only(self):
        for category in ('parameter','scene','group'):
            p=self.plan();self.allow(p);self.g.fault_category=category;n=len(self.g.events)
            r=self.apply(p);self.assertEqual(r['state'],'stopped_receipt_unknown')
            attempted=len(r['steps']);self.assertEqual(len(self.g.events)-n,attempted)
            self.assertEqual(r['steps'][-1]['state'],'verified_receipt_unknown')
            u=self.undo(p);self.assertEqual(len(u['steps']),attempted)

    def test_cancel_after_parameter_and_scene_restores_only_prefix(self):
        p=self.plan();self.allow(p)
        r=self.apply(p,cancelled=lambda:len(self.g.events)>=3)
        self.assertEqual(r['state'],'cancelled_partial');self.assertEqual(len(r['steps']),3)
        u=self.undo(p);self.assertEqual(len(u['steps']),3)
    def test_cancel_before_first_write(self):
        p=self.plan();self.allow(p);r=self.apply(p,cancelled=lambda:True)
        self.assertEqual(r['state'],'cancelled_before_write');self.assertFalse(self.g.events)

    def test_third_party_and_unattempted_changes_refuse_undo(self):
        p=self.plan();self.allow(p);self.apply(p,cancelled=lambda:len(self.g.events)>=1)
        fade=self.g.lamps[0].parameters['fade_time_rate']['value'];fade.update(raw=0x67,fade_time_code=6)
        with self.assertRaisesRegex(ValueError,'third-party'):self.c.prepare_restore(p['plan_id'])
        fade.update(raw=0x57,fade_time_code=5);self.g.scenes[63,15]['level_raw']=3
        with self.assertRaisesRegex(ValueError,'unattempted'):self.c.prepare_restore(p['plan_id'])

    def test_live_change_stops_and_blocks_automatic_recovery(self):
        p=self.plan();self.allow(p)
        self.g.after_event=lambda category,a:self.g.lamps[a].live.update(level_raw=1)
        r=self.apply(p);self.assertEqual(r['state'],'stopped_live_output_changed');self.assertEqual(len(r['steps']),1)
        with self.assertRaisesRegex(ValueError,'live output'):self.c.prepare_restore(p['plan_id'])

    def test_source_change_before_apply_refused_but_original_undo_does_not_need_source_file(self):
        p=self.plan();self.allow(p);self.doc['name']='changed';self.save()
        with self.assertRaisesRegex(ValueError,'source project changed'):self.apply(p)
        self.assertFalse(self.g.events)
        p=self.plan();self.allow(p);self.apply(p)
        (self.s.state_dir/f"project-{self.doc['project_id']}.json").unlink()
        self.undo(p)

    def test_rehashed_wire_target_and_unattempted_undo_tampering_rejected(self):
        p=self.plan();path=self.s.state_dir/f"project-restore-plan-{p['plan_id']}.json"
        raw=json.loads(path.read_text(encoding='utf-8'));raw['steps'][0]['after']['code']=6;raw['digest']=self.c._digest(raw)
        path.write_text(json.dumps(raw),encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'steps differ'):self.c.get(p['plan_id'])
        p=self.plan();self.allow(p);self.apply(p,cancelled=lambda:len(self.g.events)>=1);u=self.c.prepare_restore(p['plan_id'])
        path=self.s.state_dir/f"project-restore-plan-{u['plan_id']}.json";raw=json.loads(path.read_text(encoding='utf-8'))
        raw['intent'][1]['groups']=[dict(group=15,member=False)];raw['digest']=self.c._digest(raw);path.write_text(json.dumps(raw),encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'recovery differs'):self.c.get(u['plan_id'])

    def test_claim_survives_rewound_plan_and_failed_journal(self):
        p=self.plan();self.allow(p);raw=self.c._load(p['plan_id']);save=self.s._save
        def fail(kind,*args,**kwargs):
            if kind=='project-restore-run':raise OSError('disk failure')
            return save(kind,*args,**kwargs)
        with patch.object(self.s,'_save',fail):r=self.apply(p)
        self.assertEqual(r['state'],'preflight_failed');self.assertFalse(self.g.events)
        self.s._save('project-restore-plan',raw,p['plan_id'])
        with self.assertRaises(FileExistsError):self.apply(p)

    def test_capability_invalidation_and_existing_family_locks(self):
        p=self.plan();self.allow(p)
        with self.c.groups._locked():
            with self.assertRaises(RuntimeError):self.apply(p)
        self.assertFalse(self.g.events)
        r=self.apply(p);self.assertIn('invalidation',r);self.undo(p)

    def test_final_full_scope_catches_change_to_previously_finished_lamp(self):
        p=self.plan();self.allow(p)
        def mutate(category,a):
            if category=='group' and a==63:self.g.scenes[0,15]['level_raw']=3
        self.g.after_event=mutate;r=self.apply(p)
        self.assertEqual(r['state'],'stopped_partial');self.assertIn('final full-scope',r['error'])

    def test_multiple_parameter_fields_invalidate_unique_addresses(self):
        for row in self.doc['rows']:row['parameters']['dimming_curve']['value']['raw']=1
        self.save();p=self.plan(['fade_time','dimming_curve']);self.allow(p)
        r=self.apply(p);self.assertEqual(r['state'],'completed_readback');self.assertEqual(r['completed_steps'],10)
        self.undo(p)

    def test_scene_phase_avoids_quadratic_scene_rereads(self):
        self.doc['scope']['scenes']=list(range(16))
        for row in self.doc['rows']:
            row['scenes']=[dict(channel=2,address=row['address'],scene=s,light_type_code=1,level_raw=254,kelvin_raw=4000) for s in range(16)]
        self.save();p=self.c.prepare(self.doc['project_id'],[0,63],[],[],list(range(16)));self.allow(p)
        self.s.config['allowed_write_targets']=[dict(channel=2,scene=s,addresses=[0,63]) for s in range(16)]
        before=self.g.scene_reads;r=self.apply(p)
        self.assertEqual(r['state'],'completed_readback')
        # 32 initial + 32 category boundary + 64 per-scene + 32 final.
        self.assertEqual(self.g.scene_reads-before,160)

    def test_conditional_ct_range_restore_uses_captured_original_evidence(self):
        for row in self.doc['rows']:row['parameters']['configured_max_ct']['value']['kelvin_raw']=6000
        self.save();p=self.plan(['configured_max_ct']);self.allow(p)
        self.assertEqual(self.apply(p)['state'],'completed_readback');u=self.undo(p)
        self.assertIsNotNone(u['references'])

    def test_undo_reorders_range_expansion_before_paired_color(self):
        # Restoring original 6535K power color needs original upper limit first.
        for a in (0,63):
            self.g.lamps[a].parameters['power_on']['value']['kelvin_raw']=6535
            self.original[a]=copy.deepcopy(self.g.lamps[a].parameters)
        for row in self.doc['rows']:
            row['parameters']['power_on']['value']['kelvin_raw']=5000
            row['parameters']['configured_max_ct']['value']['kelvin_raw']=6000
        self.save();p=self.plan(['power_on','configured_max_ct']);self.allow(p)
        self.assertEqual(self.apply(p)['state'],'completed_readback')
        u=self.c.prepare_restore(p['plan_id'])
        for a in (0,63):self.assertEqual([s['field'] for s in u['steps'] if s['category']=='parameter' and s['address']==a],['configured_max_ct','power_on'])
        self.allow(u);self.assertEqual(self.apply(u)['state'],'completed_readback');self.assert_original()

    def test_background_get_and_cancel_do_not_wait_for_bus_lock(self):
        p=self.plan();self.allow(p);entered=threading.Event();release=threading.Event()
        def block(category,a):
            if len(self.g.events)==1:entered.set();self.assertTrue(release.wait(5))
        self.g.after_event=block
        try:
            started=self.c.start(p['plan_id'],p['confirmation']);self.assertTrue(entered.wait(5))
            self.assertTrue(self.c.get(p['plan_id'])['active_in_this_process'])
            cancelled=self.c.cancel(p['plan_id']);self.assertTrue(cancelled['active_in_this_process'])
            self.assertIn('cancellation',cancelled)
            with self.assertRaises(RuntimeError):self.c.start(p['plan_id'],p['confirmation'])
        finally:
            release.set()
            if self.c.worker:self.c.worker.join(5)
        result=self.c.get(p['plan_id']);self.assertFalse(result['active_in_this_process'])
        self.assertEqual(result['execution']['state'],'cancelled_partial');self.assertEqual(len(result['execution']['steps']),1)
        self.g.after_event=None;self.undo(p)

    def test_worker_error_is_not_claimed_as_success_and_restart_does_not_resume(self):
        p=self.plan();self.allow(p)
        with patch.object(self.c,'execute',side_effect=RuntimeError('worker failed before claim')):
            self.c.start(p['plan_id'],p['confirmation']);self.c.worker.join(5)
        result=self.c.get(p['plan_id']);self.assertFalse(result['active_in_this_process'])
        self.assertIn('worker_error',result);self.assertNotIn('execution',result);self.assertFalse(self.g.events)
        other=ProjectRestoreController(self.s)
        self.assertFalse(other.get(p['plan_id'])['active_in_this_process'])
        self.assertFalse(other.get(p['plan_id'])['automatic_resume'])

if __name__=='__main__':unittest.main()
