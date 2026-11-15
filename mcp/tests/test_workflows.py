"""Offline workflow failure/scope tests. Never connect to physical equipment."""
import copy
import tempfile
import threading
import unittest
from unittest.mock import patch
from dali_mcp.service import Service
from dali_mcp.jobs import JobManager


class FakeGateway:
    def __init__(self):
        self.values={};self.writes=[];self.reads=[];self.profiles=[];self.opened=0
        self.recent_packets=[];self.fail_write=False;self.on_write=None;self.on_profile=None
    def discover(self):
        self.opened+=1
        return {'ip':'127.0.0.1','dali_mac':'02:00:00:00:00:01','dali_firmware':'1.6.9'}
    def connect(self):pass
    def close(self):pass
    def read_lamp_profile(self,ch,a):
        if self.on_profile:self.on_profile(ch,a)
        self.profiles.append((ch,a))
        return dict(channel=ch,address=a,light_type_code=1,dimming_curve_code=0,min_kelvin_raw=2700,max_kelvin_raw=6500)
    def read_fade_timing(self,ch,a):
        return dict(channel=ch,address=a,fade_seconds_upper=0)
    def read_lamp_parameter(self,ch,a,field,typ):
        return {'value':{'level_raw':1 if field=='min_level' else 254}}
    def read_scene(self,ch,a,s,typ):
        self.reads.append((ch,a,s))
        return dict(channel=ch,address=a,scene=s,light_type_code=typ,level_raw=self.values.get((ch,a,s),0),kelvin_raw=4000)
    def write_scene(self,target):
        self.writes.append(dict(target))
        self.values[(target['channel'],target['address'],target['scene'])]=target['level_raw']
        if self.on_write:self.on_write()
        if self.fail_write:raise TimeoutError('receipt lost after send')
    def light_type(self,ch,a):return 1
    def set_live_component(self,*args):return {'received':True}
    def set_channel_live_component(self,*args):return {'received':True}


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.g=FakeGateway()
        self.config={'gateway_ip':'127.0.0.1','expected_dali_mac':'02:00:00:00:00:01','expected_firmware':'1.6.9',
            'enable_experimental_writes':True,'allowed_write_targets':[
                {'channel':2,'scene':s,'addresses':list(range(64))} for s in range(6)]}
        self.service=Service(self.config,self.tmp.name,lambda:self.g)
    def tearDown(self):self.tmp.cleanup()
    @staticmethod
    def changes():
        return [dict(channel=2,address=a,scene=s,level_raw=254,kelvin_raw=4000) for s in range(6) for a in range(64)]

    def test_384_targets_one_session_all_backup_before_write_and_profile_reuse(self):
        self.g.on_write=lambda:self.assertGreaterEqual(len(self.g.reads),384)
        result=self.service.save_scene_batch(self.changes())
        self.assertEqual(result['state'],'completed')
        self.assertEqual((self.g.opened,len(self.g.profiles),len(self.g.writes)),(1,64,384))
        self.assertEqual(len(self.g.reads),768)
        self.assertEqual([x['completed'] for x in result['progress']['scenes']],[64]*6)
        self.assertTrue(all(r['status']=='verified' for r in result['results']))

    def test_disjoint_scopes_do_not_form_cartesian_product(self):
        self.config['allowed_write_targets']=[dict(channel=1,scene=0,addresses=[0]),dict(channel=2,scene=15,addresses=[63])]
        for ch,a,s in [(1,63,0),(2,0,15),(2,63,0),(1,0,15)]:
            with self.assertRaises(PermissionError):self.service.save_scene_batch([dict(channel=ch,address=a,scene=s,level_raw=0,kelvin_raw=4000)])
        self.assertEqual(self.g.opened,0)
        self.service.save_scene_batch([dict(channel=1,address=0,scene=0,level_raw=0,kelvin_raw=4000),dict(channel=2,address=63,scene=15,level_raw=0,kelvin_raw=4000)])
        self.assertFalse(self.g.writes)

    def test_empty_malformed_scopes_and_duplicate_targets_fail_before_network(self):
        for scope in [[],{},None,False,[dict(channel=True,scene=0,addresses=[0])],
                      [dict(channel=2,scene=0,addresses=[])],[dict(channel=2,scene=0,addresses=[False])]]:
            self.config['allowed_write_targets']=scope
            with self.assertRaises(PermissionError):self.service.save_scene_batch(self.changes()[:1])
        with self.assertRaises(ValueError):self.service.save_scene_batch(self.changes()[:1]*2)
        with self.assertRaises(ValueError):self.service.save_scene_batch(self.changes()*3)
        self.assertEqual(self.g.opened,0)

    def test_preview_apply_uses_same_exact_scope(self):
        p=self.service.preview(self.changes()[:2])
        self.config['allowed_write_targets']=[dict(channel=2,scene=0,addresses=[0])]
        with self.assertRaises(PermissionError):self.service.apply(p['plan_id'],p['confirmation'])
        self.assertFalse(self.g.writes)

    def test_cancel_after_first_write_completes_readback_and_preserves_all_backups(self):
        event=threading.Event();self.g.on_write=event.set
        r=self.service.save_scene_batch(self.changes(),cancelled=event.is_set)
        self.assertEqual(r['state'],'cancelled_partial')
        self.assertEqual(len(r['rows']),384)
        self.assertEqual(len(self.g.writes),1)
        self.assertEqual(r['results'][0]['status'],'verified')
        self.assertEqual(sum(s['completed'] for s in r['progress']['scenes']),1)

    def test_partial_unknown_stops_and_job_id_never_replays(self):
        self.g.fail_write=True
        manager=JobManager(self.service)
        try:
            manager.start_save('six-scenes',self.changes())
            manager.worker.join(20)
            self.assertFalse(manager.worker.is_alive())
            result=manager.get('six-scenes',True)
            self.assertEqual(result['state'],'stopped_partial')
            self.assertEqual(result['result']['results'][0]['status'],'outcome_unknown')
            manager.start_save('six-scenes',self.changes())
            self.assertEqual(len(self.g.writes),1)
        finally:manager.close()

    def test_layout_roundtrip_new_id_preserves_grid_without_inheriting_check(self):
        layout=self.service.create_layout('board',2,[[0,8],[1,9]])
        checked=self.service.record_layout_check(layout['layout_id'],layout['digest'],[0,8,1,9],'Observed each position','operator')
        self.assertTrue(checked['physical_mapping_verified'])
        self.assertFalse(self.service.get_layout(layout['layout_id'])['physical_mapping_verified'])
        imported=self.service.import_layout(self.service.export_layout(checked['layout_id']))
        self.assertEqual(imported['address_grid'],layout['address_grid'])
        self.assertNotEqual(imported['layout_id'],checked['layout_id'])
        self.assertFalse(imported['physical_mapping_verified'])
        self.assertTrue(imported['imported_from']['physical_mapping_verified_claim'])
        self.assertNotIn('mapping_check',imported)
        self.assertEqual(self.g.opened,0)

    def test_restore_unknown_only_attempted_target_and_apply_requires_scope(self):
        self.g.fail_write=True
        source=self.service.save_scene_batch(self.changes()[:3])
        restore=self.service.preview_scene_restore(source['plan_id'])
        self.assertEqual(len(restore['rows']),1)
        self.assertEqual(restore['rows'][0]['before']['level_raw'],254)
        self.assertEqual(restore['rows'][0]['after']['level_raw'],0)
        self.assertEqual(len(self.g.writes),1)
        self.config['allowed_write_targets']=[]
        with self.assertRaises(PermissionError):self.service.apply(restore['plan_id'],restore['confirmation'])
        self.config['allowed_write_targets']=dict(channel=2,scene=0,addresses=[0])
        self.g.fail_write=False
        self.assertEqual(self.service.apply(restore['plan_id'],restore['confirmation'])['state'],'completed')
        self.assertEqual(self.g.values[(2,0,0)],0)
        self.assertEqual(len(self.g.writes),2)

    def test_restore_external_changes_conflict_and_cannot_apply(self):
        source=self.service.save_scene_batch(self.changes()[:1])
        self.g.values[(2,0,0)]=100
        restore=self.service.preview_scene_restore(source['plan_id'])
        self.assertEqual(restore['state'],'restore_conflict')
        self.assertNotIn('confirmation',restore)
        with self.assertRaises(ValueError):
            self.service.apply(restore['plan_id'],f"APPLY {restore['plan_id']} {restore['digest'][:12]}")
        self.assertEqual(len(self.g.writes),1)

    def test_restore_execution_metadata_tracks_durable_write_intent(self):
        source=self.service.save_scene_batch(self.changes()[:1])
        restore=self.service.preview_scene_restore(source['plan_id'])
        self.assertFalse(restore['device_writes'])
        observed=[]
        self.g.on_write=lambda:observed.append(self.service.get_plan(restore['plan_id']))
        result=self.service.apply(restore['plan_id'],restore['confirmation'])
        self.assertEqual(result['state'],'completed')
        self.assertEqual(self.g.values[(2,0,0)],0)
        self.assertEqual(len(self.g.writes),2)
        self.assertTrue(result['device_writes'])
        self.assertEqual(result['write_intent_count'],1)
        self.assertFalse(result['preview_metadata']['device_writes'])
        self.assertNotIn('Read-only',result['notice'])
        self.assertTrue(observed[0]['device_writes'])
        self.assertEqual(observed[0]['results'][0]['status'],'outcome_unknown')
        self.assertEqual(self.service.get_plan(restore['plan_id']),result)

    def test_restore_no_change_execution_does_not_claim_a_write(self):
        source=self.service.save_scene_batch(self.changes()[:1])
        self.g.values[(2,0,0)]=0
        restore=self.service.preview_scene_restore(source['plan_id'])
        result=self.service.apply(restore['plan_id'],restore['confirmation'])
        self.assertEqual(result['state'],'completed')
        self.assertEqual(result['results'][0]['status'],'unchanged')
        self.assertFalse(result['device_writes'])
        self.assertEqual(result['write_intent_count'],0)
        self.assertNotIn('Read-only',result['notice'])
        self.assertEqual(len(self.g.writes),1)

    def test_restore_unknown_receipt_keeps_write_intent_without_retry(self):
        source=self.service.save_scene_batch(self.changes()[:1])
        restore=self.service.preview_scene_restore(source['plan_id'])
        self.g.fail_write=True
        result=self.service.apply(restore['plan_id'],restore['confirmation'])
        self.assertEqual(result['state'],'stopped_partial')
        self.assertEqual(result['results'][0]['status'],'outcome_unknown')
        self.assertTrue(result['device_writes'])
        self.assertEqual(result['write_intent_count'],1)
        self.assertEqual(len(self.g.writes),2)
        self.assertEqual(self.service.get_plan(restore['plan_id']),result)

    def test_restore_intent_log_failure_prevents_the_write(self):
        source=self.service.save_scene_batch(self.changes()[:1])
        restore=self.service.preview_scene_restore(source['plan_id'])
        original_save=self.service._save
        def failing_save(prefix,value,ident=None):
            if prefix=='plan' and value.get('results'):
                raise OSError('intent log unavailable')
            return original_save(prefix,value,ident)
        with patch.object(self.service,'_save',side_effect=failing_save):
            with self.assertRaisesRegex(OSError,'intent log'):
                self.service.apply(restore['plan_id'],restore['confirmation'])
        self.assertEqual(len(self.g.writes),1)
        saved=self.service.get_plan(restore['plan_id'])
        self.assertEqual(saved['state'],'applying')
        self.assertFalse(saved['device_writes'])
        self.assertEqual(saved['write_intent_count'],0)

    def test_restore_rejects_nonterminal_and_other_gateway_before_reads(self):
        source=self.service.save_scene_batch(self.changes()[:1])
        self.config['expected_firmware']='other'
        count=self.g.opened
        with self.assertRaises(ValueError):self.service.preview_scene_restore(source['plan_id'])
        self.assertEqual(self.g.opened,count)
        self.config['expected_firmware']='1.6.9'
        source['state']='applying';self.service._save('plan',source,source['plan_id'])
        with self.assertRaises(ValueError):self.service.preview_scene_restore(source['plan_id'])

    def test_restore_noop_excludes_all_unchanged(self):
        changes=self.changes()[:1];changes[0]['level_raw']=0
        source=self.service.save_scene_batch(changes)
        count=self.g.opened
        self.assertEqual(self.service.preview_scene_restore(source['plan_id'])['state'],'no_attempted_writes')
        self.assertEqual(self.g.opened,count)

    def test_restore_rejects_original_now_outside_changed_limits(self):
        self.g.values[(2,0,0)]=50
        source=self.service.save_scene_batch(self.changes()[:1])
        with patch.object(self.g,'read_lamp_parameter',return_value={'value':{'level_raw':100}}):
            with self.assertRaisesRegex(ValueError,'outside current fixture limits'):
                self.service.preview_scene_restore(source['plan_id'])
        self.assertEqual(len(self.g.writes),1)

    def test_layout_partial_claim_stale_digest_and_unknown_addresses(self):
        r=self.service.create_layout('board',2,[[0,63]])
        partial=self.service.record_layout_check(r['layout_id'],r['digest'],[0],'Only corner checked','operator')
        self.assertFalse(partial['physical_mapping_verified'])
        for digest,addresses in [('stale',[0]),(r['digest'],[1]),(r['digest'],[True]),(r['digest'],[0,0])]:
            with self.assertRaises(ValueError):self.service.record_layout_check(r['layout_id'],digest,addresses,'note','operator')

    def test_layout_corrupt_import_or_other_gateway_rejected(self):
        r=self.service.create_layout('board',2,[[0,63]])
        document=self.service.export_layout(r['layout_id'])
        bad=copy.deepcopy(document);bad['layout']['address_grid']=[[1,63]]
        with self.assertRaises(ValueError):self.service.import_layout(bad)
        self.config['expected_firmware']='different'
        with self.assertRaises(ValueError):self.service.import_layout(document)
        self.assertEqual(self.g.opened,0)

    def test_live_progress_available_during_locked_preflight_and_error_is_persisted(self):
        entered=threading.Event();release=threading.Event();errors=[]
        def block(ch,a):
            entered.set();release.wait(3);raise TimeoutError('profile timeout')
        self.g.on_profile=block
        def run():
            try:self.service.set_live_output(2,[dict(address=0,level_raw=200)],[0])
            except Exception as exc:errors.append(exc)
        worker=threading.Thread(target=run);worker.start()
        try:
            self.assertTrue(entered.wait(2))
            row=self.service.get_live_operation('live-output')['operations'][0]
            self.assertEqual(row['progress']['phase'],'profiles')
            self.assertEqual(row['progress']['pending_address'],0)
            self.assertEqual(row['progress']['source'],'bus')
        finally:release.set();worker.join(4)
        row=self.service.get_live_operation('live-output',row['operation_id'])
        self.assertEqual(row['state'],'preflight_failed')
        self.assertEqual(row['commands'],[])
        self.assertEqual(len(errors),1)

    def test_channel_fade_progress_and_terminal_readback(self):
        phases=[];original=self.service._live_checkpoint
        def record(*args,**kw):
            phases.append(args[2]);return original(*args,**kw)
        with patch.object(self.service,'_live_checkpoint',side_effect=record),patch.object(self.service,'_wait_live_target',return_value={'matches_target':True}),patch('dali_mcp.service.time.sleep'):
            r=self.service.set_channel_live_output(2,254,4000,[0,63])
        self.assertIn('fade_bounds',phases)
        self.assertIn('color_settling',phases)
        self.assertEqual(r['progress']['phase'],'dispatched')
        self.assertTrue(r['samples_verified'])
        self.assertEqual(self.service.get_live_operation('live-channel',r['operation_id'])['state'],'dispatched')

    def test_live_progress_rejects_path_traversal(self):
        for kind,ident in [('other',None),('live-output','../x'),('live-output',False)]:
            with self.assertRaises(ValueError):self.service.get_live_operation(kind,ident)
        self.assertEqual(self.g.opened,0)


if __name__=='__main__':unittest.main()
