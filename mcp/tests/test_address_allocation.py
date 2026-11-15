import copy
import json
import tempfile
import threading
import unittest
from unittest.mock import patch
from dali_mcp.service import Service
from dali_mcp.address_allocation import AddressAllocationController, fingerprint
from test_address_moves import AddressLab


class AllocationLab(AddressLab):
    def __init__(self):
        super().__init__()
        self.new_count = 1
        self.on_allocate = None

    def allocate_missing_addresses(self, channel):
        self.writes.append((channel,2))
        if self.fault not in ('ignored','lost_not_applied'):
            vacant = [a for a in range(64) if a not in self.lamps]
            for a in vacant[:self.new_count]:self.lamps[a] = self.lamp(a)
        if self.fault == 'changed_original':self.lamps[0]['scenes'][15]['level_raw']=222
        if self.fault == 'missing_original':self.lamps.pop(0)
        if self.on_allocate:self.on_allocate(self)
        if self.fault in ('lost','lost_not_applied'):raise TimeoutError('receipt lost')
        return dict(channel=channel,mode=2,addresses=sorted(self.lamps))


class AddressAllocationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.lab=AllocationLab()
        self.info=dict(ip='127.0.0.1',dali_mac='02:00:00:00:00:01',dali_firmware='1.6.9')
        self.s=Service(dict(gateway_ip=self.info['ip'],expected_dali_mac=self.info['dali_mac'],expected_firmware='1.6.9'),self.tmp.name)
        self.open_patch=patch.object(self.s,'_open',self.open);self.open_patch.start()
        self.guard_error=False
        self.c=AddressAllocationController(self.s,self.guard)
        self.controllers=[self.c]

    def tearDown(self):
        for c in self.controllers:
            if c.worker:c.worker.join(10);self.assertFalse(c.worker.is_alive())
        self.assertEqual(self.lab.opens,self.lab.closes)
        self.open_patch.stop();self.tmp.cleanup()

    def open(self):
        self.lab.opens+=1
        return self.lab,copy.deepcopy(self.info)

    def guard(self):
        if self.guard_error:raise RuntimeError('ConfigMaster open')

    def finish(self,c=None):
        c=c or self.c
        c.worker.join(20);self.assertFalse(c.worker.is_alive())

    def prepare(self,count=1):
        ident=self.c.start_review(2,count)['operation_id'];self.finish()
        r=self.c.get(ident);self.assertEqual(r['state'],'reviewed',r)
        return r

    def allow(self,r):
        self.s.config.update(enable_experimental_address_allocation=True,
            allowed_address_allocation_digest=r['plan']['digest'],allowed_address_allocation_channels=[2])

    def execute(self,r):
        self.c.start(r['operation_id'],r['confirmation']);self.finish()
        return self.c.get(r['operation_id'],include_details=True)

    def test_background_review_allocate_preserves_existing_and_claim(self):
        before=copy.deepcopy(self.lab.lamps)
        p=self.prepare();self.assertFalse(self.lab.writes);self.allow(p)
        r=self.execute(p)
        self.assertEqual(r['state'],'completed_readback');self.assertEqual(r['execution']['new_addresses'],[2])
        self.assertEqual(self.lab.writes,[(2,2)])
        for a,row in before.items():self.assertEqual(self.lab.lamps[a],row)
        self.assertEqual(r['plan']['scope']['target'],'all_unaddressed_gear_on_channel')
        self.assertFalse(r['plan']['device_side_count_limit'])
        inv=list(self.s.state_dir.glob('capability-invalidation-*.json'))
        self.assertEqual(json.loads(inv[0].read_text())['addresses'],list(range(64)))
        restart=AddressAllocationController(self.s,self.guard)
        self.assertEqual(restart.get(p['operation_id'])['state'],'completed_readback')
        with self.assertRaisesRegex(ValueError,'claimed'):restart.start(p['operation_id'],p['confirmation'])
        self.assertEqual(len(self.lab.writes),1)

    def test_empty_channel_can_allocate_64(self):
        self.lab.lamps={};self.lab.new_count=64
        p=self.prepare(64);self.allow(p);r=self.execute(p)
        self.assertEqual(r['state'],'completed_readback')
        self.assertEqual(r['execution']['new_addresses'],list(range(64)))

    def test_invalid_scope_and_full_channel_before_writes(self):
        for args in ((0,1),(True,1),(2,0),(2,65),(2,True),(2,1.0)):
            with self.assertRaises(ValueError):self.c.start_review(*args)
        self.assertEqual(self.lab.opens,0)
        self.lab.lamps={a:self.lab.lamp(a) for a in range(64)}
        r=self.c.start_review(2,1);self.finish();r=self.c.get(r['operation_id'])
        self.assertEqual(r['state'],'review_failed');self.assertNotIn('plan',r);self.assertFalse(self.lab.writes)

    def test_mixed_dt6_dt8_snapshot(self):
        row=self.lab.lamps[0];row['parameters']['light_type']['value']=dict(code=0,name='DT6')
        for key in row['parameters']:
            if key.startswith('configured_'):row['parameters'][key]=dict(state='not_applicable')
        for item in row['scenes']+[row['actual']]:item['light_type_code']=0;item['kelvin_raw']=0
        p=self.prepare();self.allow(p)
        self.assertEqual(self.execute(p)['state'],'completed_readback')
        row['parameters']['configured_min_kelvin']={'value':3000}
        with self.assertRaises(ValueError):fingerprint(row)

    def test_exact_gates_confirmation_and_expiry(self):
        p=self.prepare()
        for delta in (dict(enable_experimental_address_allocation=False),dict(allowed_address_allocation_digest='bad'),
                      dict(allowed_address_allocation_channels=[1]),dict(allowed_address_allocation_channels=[True])):
            self.allow(p);self.s.config.update(delta)
            with self.assertRaises(PermissionError):self.c.start(p['operation_id'],p['confirmation'])
        self.allow(p)
        with self.assertRaises(ValueError):self.c.start(p['operation_id'],'yes')
        with patch('dali_mcp.address_allocation.time.time',return_value=p['plan']['expires_at']+1):
            with self.assertRaises(ValueError):self.c.start(p['operation_id'],p['confirmation'])
        self.assertFalse(self.c.get(p['operation_id'])['claimed']);self.assertFalse(self.lab.writes)

    def test_drift_in_parameters_or_scene_or_live_refuses(self):
        changes=[lambda r:r['parameters']['groups']['value'].update(group_mask=7),
                 lambda r:r['scenes'][0].update(level_raw=200),lambda r:r['actual'].update(level_raw=20)]
        for change in changes:
            self.lab.lamps[0]=self.lab.lamp(0);p=self.prepare();self.allow(p);change(self.lab.lamps[0])
            self.assertEqual(self.execute(p)['state'],'preflight_failed')
        self.assertFalse(self.lab.writes)

    def test_drift_in_occupancy_or_identity_refuses(self):
        p=self.prepare();self.allow(p);self.lab.lamps[9]=self.lab.lamp(9)
        self.assertEqual(self.execute(p)['state'],'preflight_failed')
        p=self.prepare();self.allow(p);self.info['dali_mac']='02:00:00:00:00:02'
        self.assertEqual(self.execute(p)['state'],'preflight_failed');self.assertFalse(self.lab.writes)

    def test_read_fault_and_receipt_uncertainty_never_replay(self):
        for fault,state in [('lost','verified_receipt_unknown'),('lost_not_applied','stopped_count_mismatch'),
                            ('ignored','stopped_count_mismatch'),('changed_original','stopped_lamp_mismatch'),
                            ('missing_original','stopped_address_mismatch'),('scan_failure','stopped_unknown'),
                            ('read_failure','stopped_unknown')]:
            with self.subTest(fault=fault):
                self.lab.lamps={a:self.lab.lamp(a) for a in (0,1,7)};self.lab.fault=None
                self.lab.writes=[]
                p=self.prepare();self.allow(p);self.lab.fault=fault
                r=self.execute(p);self.assertEqual(r['state'],state)
                with self.assertRaises(ValueError):self.c.start(p['operation_id'],p['confirmation'])
                self.assertEqual(len(self.lab.writes),1)

    def test_expected_count_is_not_a_device_limit(self):
        p=self.prepare();self.allow(p);self.lab.new_count=3
        r=self.execute(p)
        self.assertEqual(r['state'],'stopped_count_mismatch');self.assertEqual(r['execution']['new_addresses'],[2,3,4])
        self.assertEqual(len(self.lab.writes),1)

    def test_duplicates_review_and_preflight_refuse(self):
        self.lab.duplicates=[0];r=self.c.start_review(2,1);self.finish()
        self.assertEqual(self.c.get(r['operation_id'])['state'],'review_failed')
        self.lab.duplicates=[];p=self.prepare();self.allow(p);self.lab.duplicates=[1]
        self.assertEqual(self.execute(p)['state'],'preflight_failed');self.assertFalse(self.lab.writes)

    def test_closed_app_refuses_review_and_execution(self):
        self.guard_error=True;r=self.c.start_review(2,1);self.finish()
        self.assertEqual(self.c.get(r['operation_id'])['state'],'review_failed');self.assertEqual(self.lab.opens,0)
        self.guard_error=False;p=self.prepare();self.allow(p);self.guard_error=True
        self.assertEqual(self.execute(p)['state'],'worker_failed');self.assertFalse(self.lab.writes)

    def test_cancel_review_reports_progress_without_waiting_for_bus_lock(self):
        entered=threading.Event();release=threading.Event()
        def hold(lab):entered.set();release.wait(10)
        self.lab.on_read=hold
        try:
            r=self.c.start_review(2,1);self.assertTrue(entered.wait(5))
            status=self.c.get(r['operation_id']);self.assertTrue(status['active_in_this_process'])
            self.assertEqual(status['review']['progress'],dict(completed=0,total=3))
            self.c.cancel(r['operation_id'])
            with self.assertRaises(RuntimeError):self.c.start_review(2,1)
        finally:release.set();self.finish()
        status=self.c.get(r['operation_id'])
        self.assertEqual(status['state'],'cancelled_before_write');self.assertNotIn('plan',status)
        self.assertFalse(self.lab.writes)

    def test_cancel_preflight_claims_but_does_not_write(self):
        p=self.prepare();self.allow(p)
        self.lab.on_read=lambda lab:self.c.event.set()
        r=self.execute(p);self.assertEqual(r['state'],'cancelled_before_write')
        self.assertTrue(r['claimed']);self.assertFalse(self.lab.writes)

    def test_cancel_after_dispatch_finishes_readback_without_undo(self):
        p=self.prepare();self.allow(p);self.lab.on_allocate=lambda lab:self.c.event.set()
        r=self.execute(p);self.assertEqual(r['state'],'completed_readback')
        self.assertTrue(r['execution']['cancel_requested']);self.assertEqual(len(self.lab.writes),1)
        self.assertEqual(len(r['execution']['after_rows']),4)

    def test_mutation_during_preflight_rechecked_before_dispatch(self):
        for mutation in (lambda lab:lab.lamps.setdefault(9,lab.lamp(9)),
                         lambda lab:self.s.config.update(enable_experimental_address_allocation=False)):
            self.lab.on_read=None;self.lab.lamps={a:self.lab.lamp(a) for a in (0,1,7)}
            p=self.prepare();self.allow(p);self.lab.on_read=mutation
            self.assertEqual(self.execute(p)['state'],'preflight_failed')
        self.assertFalse(self.lab.writes)

    def test_final_scan_detects_address_change_after_readback(self):
        p=self.prepare();self.allow(p)
        def scan(lab):
            if lab.writes and lab.scans==6:lab.lamps[10]=lab.lamp(10)
        self.lab.on_scan=scan
        self.assertEqual(self.execute(p)['state'],'stopped_address_mismatch')

    def test_claim_and_intent_must_be_durable_before_dispatch(self):
        original=self.c._save
        for kind in ('claim','execution'):
            p=self.prepare();self.allow(p)
            def fail(k,i,d,exclusive=False):
                if k==kind and (k=='claim' or d.get('write_intent')):raise OSError('disk failure')
                return original(k,i,d,exclusive)
            with patch.object(self.c,'_save',fail):
                r=self.execute(p)
            self.assertIn(r['state'],('worker_failed','preflight_failed','stopped_unknown'))
        self.assertFalse(self.lab.writes)

    def test_post_dispatch_disk_failure_still_reads_all_and_reports_error(self):
        p=self.prepare();self.allow(p);save=self.c._save
        def fail(k,i,d,exclusive=False):
            if k in ('execution','worker-error') and self.lab.writes:raise OSError('disk full after command')
            return save(k,i,d,exclusive)
        with patch.object(self.c,'_save',fail):r=self.execute(p)
        self.assertEqual(r['state'],'worker_failed')
        actual=r['worker-error']['last_worker_result']
        self.assertEqual(actual['state'],'completed_readback');self.assertEqual(len(actual['after_rows']),4)
        self.assertIn('log_error',actual);self.assertEqual(len(self.lab.writes),1)

    def test_missing_execution_journal_is_uncertain_after_restart(self):
        p=self.prepare();self.allow(p);self.execute(p)
        self.c._path('execution',p['operation_id']).unlink()
        restart=AddressAllocationController(self.s,self.guard)
        r=restart.get(p['operation_id']);self.assertEqual(r['state'],'execution_unavailable')
        with self.assertRaises(ValueError):restart.start(p['operation_id'],p['confirmation'])

    def test_review_record_must_be_successful_and_plan_bound(self):
        p=self.prepare();self.allow(p)
        r=self.c._read('review',p['operation_id']);r['state']='review_failed'
        self.c._save('review',p['operation_id'],r)
        with self.assertRaises(ValueError):self.c.start(p['operation_id'],p['confirmation'])
        self.assertNotIn('confirmation',self.c.get(p['operation_id']))
        self.assertFalse(self.lab.writes)

    def test_exclusive_publish_is_complete_and_never_overwrites_claim(self):
        ident='a'*32;data=dict(operation_id=ident)
        with patch('dali_mcp.address_allocation.os.fsync',side_effect=OSError('disk failure')):
            with self.assertRaises(OSError):self.c._save('claim',ident,data,True)
        self.assertFalse(self.c._path('claim',ident).exists())
        self.c._save('claim',ident,data,True)
        with self.assertRaises(FileExistsError):self.c._save('claim',ident,dict(changed=True),True)
        self.assertEqual(self.c._read('claim',ident),data)
        self.assertEqual(list(self.s.state_dir.glob('*.tmp')),[])

    def test_progress_query_omits_large_snapshots_and_details_are_opt_in(self):
        p=self.prepare();self.allow(p);full=self.execute(p)
        compact=self.c.get(p['operation_id'])
        self.assertNotIn('originals',compact['plan']);self.assertEqual(compact['plan']['originals_count'],3)
        self.assertNotIn('after_rows',compact['execution']);self.assertEqual(compact['execution']['after_rows_count'],4)
        self.assertEqual(len(full['plan']['originals']),3);self.assertEqual(len(full['execution']['after_rows']),4)
        self.assertLess(len(json.dumps(compact)),len(json.dumps(full))/3)
        reads=self.lab.reads;self.c.get(p['operation_id'],True);self.assertEqual(self.lab.reads,reads)

    def test_review_publication_failure_never_authorizes_partial_plan(self):
        save=self.c._save
        def fail(k,i,d,exclusive=False):
            if k=='review' and d.get('state')=='reviewed':raise OSError('cannot publish finished review')
            return save(k,i,d,exclusive)
        with patch.object(self.c,'_save',fail):
            r=self.c.start_review(2,1);self.finish();r=self.c.get(r['operation_id'])
        self.assertEqual(r['state'],'worker_failed');self.assertNotIn('confirmation',r)
        self.allow(r)
        with self.assertRaises(ValueError):self.c.start(r['operation_id'],self.c._confirmation(r['plan']))
        self.assertFalse(self.lab.writes)

    def test_cancel_after_intent_fsync_still_prevents_command(self):
        p=self.prepare();self.allow(p);save=self.c._save
        def cancel(k,i,d,exclusive=False):
            save(k,i,d,exclusive)
            if k=='execution' and d.get('state')=='allocating':self.c.event.set()
        with patch.object(self.c,'_save',cancel):r=self.execute(p)
        self.assertEqual(r['state'],'cancelled_before_write');self.assertFalse(self.lab.writes)


if __name__=='__main__':unittest.main()
