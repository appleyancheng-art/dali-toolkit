import copy,json,os,threading,unittest
from pathlib import Path
from unittest.mock import Mock,patch
from dali_mcp.native_file import canonical,parse
from dali_mcp.project_delivery import ProjectDeliveryController
from dali_mcp.project_names import ProjectNames
from dali_mcp.projects import digest
import test_project_execution as bus_tests
from test_project_floorplan import cache_fixture,png
from test_native_cache import document


class DeliveryTests(unittest.TestCase):
    save=bus_tests.ProjectExecutionTests.save
    assert_original=bus_tests.ProjectExecutionTests.assert_original

    def setUp(self):
        bus_tests.ProjectExecutionTests.setUp(self)
        self.info['dali_firmware']='1.6.9';self.s.config['expected_firmware']='1.6.9'
        self.doc['gateway']=copy.deepcopy(self.info);self.save()
        self.root=Path(self.tmp.name);self.file=self.root/'gateways'/'cache.json';self.file.parent.mkdir()
        d=document();self.original_file=canonical(d);self.file.write_bytes(self.original_file)
        self.s.config['configmaster_cache_file']=str(self.file)
        d=cache_fixture();d['zCh2']['addresses']['0']['name']='工程灯'
        d['zCh2']['groupNames']['15']='工程组';d['zCh2']['sceneNamesByGroup']['0']['15']='工程备注'
        cache=self.s.import_configmaster_cache(d)['cache_id'];image=self.root/'source.png';image.write_bytes(png())
        ident=self.s.attach_project_floorplan(self.doc['project_id'],cache,str(image))['project_id']
        self.selections=[dict(kind='lamp',address=0),dict(kind='group',group=15),dict(kind='scene',group=0,scene=15)]
        self.project=ProjectNames(self.s).attach(ident,cache,self.selections)['project_id']
        guard=patch('dali_mcp.native_file.require_configmaster_closed');self.guard=guard.start();self.addCleanup(guard.stop)
        if os.name!='nt':
            replace=patch('dali_mcp.native_file.replace_native_file',side_effect=os.replace);replace.start();self.addCleanup(replace.stop)
        self.d=ProjectDeliveryController(self.s)

    def plan(self,names=True,floor=True):
        return self.d.prepare(self.project,[0,63],['fade_time'],[15],[0,15],self.selections if names else [],'captured_marks' if floor else None)
    def allow(self,p):
        self.s.config.update(enable_project_delivery=True,allowed_project_delivery_digest=p['digest'],
                             enable_native_cache_writes=True,enable_native_floorplan_writes=True)
        for phase in p['phases']:
            if phase['category']=='device':bus_tests.ProjectExecutionTests.allow(self,self.d._child(phase))
    def apply(self,p,**kwargs):return self.d.execute(p['plan_id'],p['confirmation'],**kwargs)
    def undo(self,p):
        u=self.d.prepare_restore(p['plan_id']);self.allow(u);r=self.apply(u)
        self.assertEqual(r['state'],'completed_readback',r);self.assert_original();self.assertEqual(self.file.read_bytes(),self.original_file)
        return u

    def test_full_forward_and_reverse_one_file_commit_durable_stage_evidence(self):
        p=self.plan();self.assertFalse(self.g.events);self.assertEqual(self.file.read_bytes(),self.original_file)
        self.allow(p);replace=self.d.floorplan.replace;self.d.floorplan.replace=Mock(side_effect=replace)
        def evidence(category,address):
            run=self.d.get(p['plan_id'])['execution'];self.assertEqual(run['pending'],'device')
            self.assertEqual(run['phases'][0]['state'],'outcome_unknown')
        self.g.after_event=evidence;r=self.apply(p);self.g.after_event=None
        self.assertEqual(r['state'],'completed_readback',r);self.assertEqual(r['completed_phases'],2)
        self.assertFalse(r['atomic']);self.assertFalse(r['ui_verified']);self.assertEqual(self.d.floorplan.replace.call_count,1)
        self.assertEqual(parse(self.file.read_bytes())['zCh2']['addresses']['0']['name'],'工程灯')
        self.assertEqual(set(r['final_readback']),{'device','local'})
        u=self.undo(p);self.assertEqual([x['category'] for x in u['phases']],['local','device'])
        self.assertEqual(self.d.floorplan.replace.call_count,2)
        self.assertEqual(self.d.prepare_restore(p['plan_id'])['state'],'already_original')

    def test_names_only_and_floorplan_only_local_selection(self):
        for names,floor in ((True,False),(False,True)):
            p=self.plan(names,floor);self.allow(p);r=self.apply(p);self.assertEqual(r['state'],'completed_readback',r)
            current=parse(self.file.read_bytes());self.assertEqual('floorPlan' in current,floor)
            self.assertEqual(current['zCh2']['addresses']['0']['name'],'工程灯' if names else '灯零')
            self.undo(p)
        with self.assertRaises(ValueError):self.plan(False,False)

    def test_all_permissions_and_local_original_validated_before_bus_write(self):
        for gate in ('enable_project_delivery','enable_native_cache_writes','enable_native_floorplan_writes',
                     'enable_project_restore','enable_experimental_parameter_writes','enable_experimental_writes','enable_experimental_group_writes'):
            p=self.plan();self.allow(p);self.s.config[gate]=False;before=self.open.call_count
            with self.assertRaises((PermissionError,ValueError)):self.apply(p)
            self.assertEqual(self.open.call_count,before);self.assertFalse(self.g.events);self.assertFalse(self.d._path('claim',p['plan_id']).exists())
        p=self.plan();self.allow(p);d=parse(self.file.read_bytes());d['lastSeen']='changed';self.file.write_bytes(canonical(d))
        with self.assertRaisesRegex(ValueError,'local file changed'):self.apply(p)
        self.assertFalse(self.g.events)

    def test_invalid_local_source_rejected_before_network_capture(self):
        before=self.open.call_count
        with self.assertRaises(ValueError):self.d.prepare(self.project,[0],['fade_time'],[],[],[dict(kind='lamp',address=1)])
        self.assertEqual(self.open.call_count,before)

    def test_long_device_capture_does_not_return_expired_delivery(self):
        clock=[1000.0];prepare=self.d.device.prepare
        def slow_capture(*args,**kwargs):
            clock[0]+=601
            return prepare(*args,**kwargs)
        with patch('dali_mcp.project_delivery.time.time',side_effect=lambda:clock[0]),patch.object(self.d.device,'prepare',side_effect=slow_capture):
            for names,floor in ((True,True),(True,False),(False,True)):
                p=self.plan(names,floor)
                self.assertGreater(p['expires_at'],clock[0])
                self.assertFalse(self.g.events)
                self.assertEqual(self.file.read_bytes(),self.original_file)

    def test_accepted_delivery_can_finish_local_stage_after_start_deadline(self):
        clock=[1000.0]
        with patch('dali_mcp.project_delivery.time.time',side_effect=lambda:clock[0]):
            p=self.plan();self.allow(p)
            def slow_device(category,address):
                if len(self.g.events)==1:clock[0]+=601
            self.g.after_event=slow_device
            result=self.apply(p)
            self.assertEqual(result['state'],'completed_readback',result)
            self.assertGreater(clock[0],p['expires_at'])
            self.assertEqual(parse(self.file.read_bytes())['zCh2']['addresses']['0']['name'],'工程灯')
            self.g.after_event=None;self.undo(p)

    def test_file_change_during_long_capture_is_not_silently_rebased(self):
        clock=[1000.0];prepare=self.d.device.prepare
        def drift(*args,**kwargs):
            clock[0]+=601
            d=parse(self.file.read_bytes());d['lastSeen']='external-during-capture';self.file.write_bytes(canonical(d))
            return prepare(*args,**kwargs)
        with patch('dali_mcp.project_delivery.time.time',side_effect=lambda:clock[0]),patch.object(self.d.device,'prepare',side_effect=drift):
            with self.assertRaisesRegex(ValueError,'local file changed'):self.plan()
        self.assertFalse(self.g.events)
        self.assertEqual(parse(self.file.read_bytes())['lastSeen'],'external-during-capture')

    def test_long_undo_preparation_and_preflight_keep_original_admission(self):
        clock=[1000.0]
        with patch('dali_mcp.project_delivery.time.time',side_effect=lambda:clock[0]):
            p=self.plan();self.allow(p);self.assertEqual(self.apply(p)['state'],'completed_readback')
            prepare=self.d.device.prepare_restore
            def slow_prepare(*args,**kwargs):
                clock[0]+=601;return prepare(*args,**kwargs)
            with patch.object(self.d.device,'prepare_restore',side_effect=slow_prepare):u=self.d.prepare_restore(p['plan_id'])
            self.assertGreater(u['expires_at'],clock[0]);self.allow(u)
            capture=self.d.device._capture;calls=[]
            def slow_preflight(*args,**kwargs):
                if not calls:clock[0]+=601
                calls.append(1);return capture(*args,**kwargs)
            with patch.object(self.d.device,'_capture',side_effect=slow_preflight):result=self.apply(u)
            self.assertEqual(result['state'],'completed_readback',result)
            self.assertLessEqual(result['admitted_at'],u['expires_at'])
            self.assertGreater(result['started_at'],u['expires_at'])
            self.assert_original();self.assertEqual(self.file.read_bytes(),self.original_file)

    def test_background_change_during_local_refresh_is_not_rebased(self):
        self.file.write_bytes(canonical(cache_fixture()));before=self.file.read_bytes()
        directory=self.root/'floorplans';directory.mkdir();image=directory/'background.png'
        image.write_bytes(png(3,3));prepare=self.d.floorplan.prepare;calls=[]
        def race(*args,**kwargs):
            calls.append(1)
            if len(calls)==2:image.write_bytes(png(4,4))
            return prepare(*args,**kwargs)
        with patch.object(self.d.floorplan,'prepare',side_effect=race):
            with self.assertRaisesRegex(ValueError,'original background changed'):self.plan()
        self.assertFalse(self.g.events);self.assertEqual(self.file.read_bytes(),before)
        self.assertEqual(image.read_bytes(),png(4,4))

    def test_expired_standalone_children_cannot_use_parent_start_permission(self):
        clock=[1000.0]
        with patch('dali_mcp.project_delivery.time.time',side_effect=lambda:clock[0]):
            p=self.plan();self.allow(p);clock[0]+=601
            for phase in p['phases']:
                child=self.d._child(phase)
                with self.assertRaisesRegex(ValueError,'expired'):
                    if phase['category']=='device':self.d.device.execute(child['plan_id'],self.d.device._confirmation(child))
                    else:
                        ctl=self.d._local(phase);ctl.apply(child['plan_id'],ctl._confirmation(child))
            self.assertFalse(self.g.events);self.assertEqual(self.file.read_bytes(),self.original_file)

    def test_late_local_phase_still_rejects_external_file_change(self):
        clock=[1000.0]
        with patch('dali_mcp.project_delivery.time.time',side_effect=lambda:clock[0]):
            p=self.plan();self.allow(p)
            def drift(category,address):
                if len(self.g.events)==1:
                    clock[0]+=601
                    d=parse(self.file.read_bytes());d['lastSeen']='external-after-device';self.file.write_bytes(canonical(d))
            self.g.after_event=drift;r=self.apply(p)
            self.assertEqual(r['state'],'stopped_partial');self.assertEqual(r['completed_phases'],1)
            self.assertIn('native cache changed',r['error'])
            self.assertEqual(parse(self.file.read_bytes())['lastSeen'],'external-after-device')

    def test_child_deadline_admission_cannot_be_reused_after_completion(self):
        clock=[1000.0];seen=[];apply=self.d.floorplan.apply
        def capture(*args,**kwargs):
            seen.append(kwargs['_deadline_check']);return apply(*args,**kwargs)
        with patch('dali_mcp.project_delivery.time.time',side_effect=lambda:clock[0]),patch.object(self.d.floorplan,'apply',side_effect=capture):
            p=self.plan();self.allow(p)
            self.assertEqual(self.apply(p)['state'],'completed_readback')
            child=self.d._child(p['phases'][1]);clock[0]+=601
            with self.assertRaisesRegex(ValueError,'no longer active'):seen[0](child)

    def test_no_blind_retry_on_lost_bus_receipt_no_local_commit(self):
        p=self.plan();self.allow(p);self.g.fault_category='parameter';r=self.apply(p)
        self.assertEqual(r['state'],'stopped_partial');self.assertEqual(r['completed_phases'],0)
        self.assertEqual(len(self.g.events),1);self.assertEqual(self.file.read_bytes(),self.original_file)
        with self.assertRaisesRegex(ValueError,'claimed'):self.apply(p)
        self.undo(p)

    def test_failed_local_replace_keeps_device_result_and_can_undo(self):
        p=self.plan();self.allow(p);replace=self.d.floorplan.replace
        self.d.floorplan.replace=Mock(side_effect=OSError('locked file'));r=self.apply(p)
        self.assertEqual(r['state'],'stopped_partial');self.assertEqual(r['completed_phases'],1)
        self.assertEqual(r['phases'][1]['state'],'not_written_verified');self.assertEqual(self.file.read_bytes(),self.original_file)
        self.d.floorplan.replace=replace;u=self.undo(p);self.assertEqual([x['category'] for x in u['phases']],['device'])

    def test_uncertain_local_receipt_stops_even_when_readback_matches(self):
        p=self.plan();self.allow(p);replace=self.d.floorplan.replace
        def lose(a,b):replace(a,b);raise OSError('lost result')
        self.d.floorplan.replace=lose;r=self.apply(p)
        self.assertEqual(r['state'],'stopped_partial');self.assertEqual(r['completed_phases'],1)
        self.assertEqual(r['phases'][1]['state'],'written_verified_with_error')
        self.d.floorplan.replace=replace;self.undo(p)

    def test_local_drift_after_device_phase_is_preserved_and_undo_refuses(self):
        p=self.plan();self.allow(p)
        def drift(category,address):
            if len(self.g.events)==8:
                d=parse(self.file.read_bytes());d['lastSeen']='external';self.file.write_bytes(canonical(d))
        self.g.after_event=drift;r=self.apply(p);self.g.after_event=None
        self.assertEqual(r['state'],'stopped_partial');self.assertEqual(r['completed_phases'],1)
        self.assertEqual(parse(self.file.read_bytes())['lastSeen'],'external')
        # No local write intent: do not treat unexpected cache data as our write.
        with self.assertRaisesRegex(ValueError,'drift without'):self.d.prepare_restore(p['plan_id'])
        self.file.write_bytes(self.original_file);self.undo(p)

    def test_final_readback_detects_device_drift_during_file_commit(self):
        p=self.plan();self.allow(p);replace=self.d.floorplan.replace
        def drift(a,b):
            replace(a,b);self.g.lamps[0].parameters['fade_time_rate']['value'].update(raw=0x67,fade_time_code=6)
        self.d.floorplan.replace=drift;r=self.apply(p)
        self.assertEqual(r['state'],'stopped_partial');self.assertEqual(r['completed_phases'],2)
        self.assertIn('device changed',r['error'])
        with self.assertRaisesRegex(ValueError,'third-party'):self.d.prepare_restore(p['plan_id'])

    def test_cancel_before_any_write_and_between_domains(self):
        p=self.plan();self.allow(p);r=self.apply(p,cancelled=lambda:True)
        self.assertEqual(r['state'],'cancelled_before_write');self.assertFalse(self.g.events)
        self.assertEqual(self.d.prepare_restore(p['plan_id'])['state'],'already_original')
        p=self.plan();self.allow(p);r=self.apply(p,cancelled=lambda:len(self.g.events)>=8)
        self.assertEqual(r['state'],'cancelled_partial');self.assertEqual(r['completed_phases'],1)
        self.assertEqual(self.file.read_bytes(),self.original_file);self.undo(p)

    def test_worker_exposes_unpersisted_result_and_error_record_failure(self):
        for fail_record in (False,True):
            p=self.plan();self.allow(p);journal=self.d._journal
            def write(kind,*args):
                if fail_record and kind=='worker-error':raise PermissionError('error log denied')
                return journal(kind,*args)
            with patch.object(self.d,'execute',return_value={'state':'completed_readback','journal_error':'final log denied'}),patch.object(self.d,'_journal',side_effect=write):
                self.d.start(p['plan_id'],p['confirmation']);self.d.worker.join(8)
            self.assertFalse(self.d.worker.is_alive());status=self.d.get(p['plan_id'])
            self.assertFalse(status['active_in_this_process'])
            self.assertIn('not durably recorded',status['worker_error_in_this_process']['error'])
            if fail_record:self.assertEqual(status['worker_error_in_this_process']['journal_error'],'error log denied')
            else:self.assertIn('not durably recorded',status['worker-error']['error'])
            self.assertFalse(self.g.events);self.assertEqual(self.file.read_bytes(),self.original_file)

    def test_transient_local_journal_denial_does_not_repeat_device_writes(self):
        p=self.plan();self.allow(p);replace=os.replace;denied=[]
        def busy(source,target):
            if Path(target).name.startswith('native-floorplan-run-') and not denied:
                denied.append(1);error=PermissionError('Windows sharing denial');error.winerror=5;raise error
            return replace(source,target)
        with patch('os.replace',side_effect=busy):r=self.apply(p)
        self.assertEqual(r['state'],'completed_readback',r)
        self.assertEqual(len(self.g.events),8);self.assertEqual(len(denied),1);self.undo(p)

    def test_background_progress_and_cancellation_remain_available(self):
        p=self.plan();self.allow(p);entered=threading.Event();release=threading.Event()
        def block(category,address):
            if len(self.g.events)==1:entered.set();self.assertTrue(release.wait(8))
        self.g.after_event=block
        try:
            self.d.start(p['plan_id'],p['confirmation']);self.assertTrue(entered.wait(8))
            status=self.d.get(p['plan_id']);self.assertTrue(status['active_in_this_process'])
            self.assertEqual(status['execution']['pending'],'device')
            self.assertEqual(len(status['children'][0]['status']['execution']['steps']),1)
            self.d.cancel(p['plan_id'])
            with self.assertRaises(RuntimeError):self.d.prepare_restore(p['plan_id'])
        finally:
            release.set()
            if self.d.worker:self.d.worker.join(8)
        self.assertFalse(self.d.worker.is_alive());self.g.after_event=None
        self.assertEqual(self.d.get(p['plan_id'])['execution']['state'],'cancelled_partial')
        self.assertEqual(len(self.g.events),1);self.undo(p)

    def test_restart_never_replays_and_undo_survives_source_removal(self):
        p=self.plan();self.allow(p);self.assertEqual(self.apply(p)['state'],'completed_readback')
        self.d=ProjectDeliveryController(self.s)
        self.assertFalse(self.d.get(p['plan_id'])['active_in_this_process'])
        with self.assertRaisesRegex(ValueError,'claimed'):self.apply(p)
        (self.s.state_dir/f'project-{self.project}.json').unlink();self.undo(p)

    def test_claimed_child_rejected_before_other_domain_dispatch(self):
        p=self.plan();self.allow(p);local=p['phases'][1];ctl=self.d._local(local);child=self.d._child(local)
        ctl.apply(child['plan_id'],ctl._confirmation(child));before=len(self.g.events)
        with self.assertRaisesRegex(ValueError,'child already claimed'):self.apply(p)
        self.assertEqual(len(self.g.events),before)

    def test_changed_source_before_start_and_unrelated_rehashed_child_binding_reject(self):
        p=self.plan();self.allow(p);source=self.s.get_project(self.project);source['name']='changed'
        source['digest']=digest(source);self.s._save('project',source,self.project)
        with self.assertRaisesRegex(ValueError,'source project changed'):self.apply(p)
        self.assertFalse(self.g.events)
        q=self.plan();p.pop('confirmation');p['phases'][1]=q['phases'][1];p['expires_at']=min(self.d._child(x)['expires_at'] for x in p['phases'])
        p['digest']=digest(p);self.d._path('plan',p['plan_id']).write_bytes(canonical(p))
        with self.assertRaisesRegex(ValueError,'source differs'):self.d.get(p['plan_id'])

    def test_expiry_and_confirmation_have_no_effect(self):
        p=self.plan();self.allow(p)
        with self.assertRaisesRegex(ValueError,'confirmation'):self.d.execute(p['plan_id'],'yes')
        with patch('dali_mcp.project_delivery.time.time',return_value=p['expires_at']+1):
            with self.assertRaisesRegex(ValueError,'expired'):self.apply(p)
        self.assertFalse(self.g.events);self.assertEqual(self.file.read_bytes(),self.original_file)

    def test_missing_child_journal_is_not_success_and_cannot_be_undone_blindly(self):
        p=self.plan();self.allow(p);self.apply(p)
        child=self.d._child(p['phases'][0]);(self.s.state_dir/f"project-restore-run-{child['execution_id']}.json").unlink()
        self.assertIn('execution_unavailable',self.d.get(p['plan_id'])['children'][0]['status'])
        with self.assertRaises(OSError):self.d.prepare_restore(p['plan_id'])

    def test_late_device_conflict_prevents_first_undo_file_write(self):
        p=self.plan();self.allow(p);self.apply(p);u=self.d.prepare_restore(p['plan_id']);self.allow(u)
        current=self.file.read_bytes();self.g.lamps[0].parameters['fade_time_rate']['value'].update(raw=0x67,fade_time_code=6)
        with self.assertRaisesRegex(ValueError,'device changed before'):self.apply(u)
        self.assertEqual(self.file.read_bytes(),current);self.assertFalse(self.d._path('claim',u['plan_id']).exists())

    def test_full64_scope_and1024_scenes_roundtrip_is_simulated(self):
        source=self.s.get_project(self.project);template=copy.deepcopy(source['rows'][0])
        source['scope'].update(addresses=list(range(64)),scenes=list(range(16)));source['rows']=[]
        for a in range(64):
            row=copy.deepcopy(template);row['address']=a
            row['scenes']=[dict(channel=2,address=a,scene=n,light_type_code=1,level_raw=254,kelvin_raw=4000) for n in range(16)]
            source['rows'].append(row)
        # Attachment source remains local evidence; selected local labels are unchanged.
        source['digest']=digest(source);self.s._save('project',source,self.project)
        p=self.d.prepare(self.project,list(range(64)),['fade_time'],[15],list(range(16)),self.selections,'captured_marks')
        self.allow(p);child=self.d._child(p['phases'][0]);cfg=self.s.config
        # Use the planner's complete field values, not a guessed encoding.
        cfg['allowed_parameter_batch_targets']=[dict(channel=2,address=a,values=[dict(field=s['field'],values=s['after']) for s in child['steps'] if s['category']=='parameter' and s['address']==a]) for a in range(64)]
        cfg['allowed_write_targets']=[dict(channel=2,scene=n,addresses=list(range(64))) for n in range(16)]
        r=self.apply(p);self.assertEqual(r['state'],'completed_readback',r)
        self.assertEqual(len(r['final_readback']['device']['rows']),64);self.assertEqual(len(self.g.events),1152)
        u=self.d.prepare_restore(p['plan_id']);self.allow(u);child=self.d._child(u['phases'][-1])
        cfg['allowed_parameter_batch_targets']=[dict(channel=2,address=a,values=[dict(field=s['field'],values=s['after']) for s in child['steps'] if s['category']=='parameter' and s['address']==a]) for a in range(64)]
        cfg['allowed_write_targets']=[dict(channel=2,scene=n,addresses=list(range(64))) for n in range(16)]
        self.assertEqual(self.apply(u)['state'],'completed_readback');self.assert_original();self.assertEqual(self.file.read_bytes(),self.original_file)


if __name__=='__main__':unittest.main()
