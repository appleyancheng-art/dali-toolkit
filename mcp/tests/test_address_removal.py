import copy
import json
import tempfile
import time
import unittest
from unittest.mock import patch
from dali_mcp.service import Service
from dali_mcp.address_removal import AddressRemovalController
from test_address_moves import AddressLab


class RemovalLab(AddressLab):
    def change_short_address(self, ch, src, dst):
        assert dst == 255 and src in self.lamps
        self.writes.append((ch, src, dst))
        if self.fault not in ('ignored', 'lost_not_applied'):
            self.removed = self.lamps.pop(src)
        if self.fault == 'changed_unrelated_address':
            self.lamps[9] = self.lamp(9)
        if self.fault in ('lost', 'lost_not_applied'):
            raise TimeoutError('receipt lost')
        if self.fault == 'interrupt':
            raise KeyboardInterrupt()
        return dict(result=2, receipt_only=True)


class AddressRemovalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.lab = RemovalLab()
        self.info = dict(ip='127.0.0.1', dali_mac='02:00:00:00:00:01', dali_firmware='1.6.9')
        self.s = Service(dict(gateway_ip=self.info['ip'], expected_dali_mac=self.info['dali_mac'], expected_firmware='1.6.9'), self.tmp.name)
        self.open_patch = patch.object(self.s, '_open', self.open)
        self.open_patch.start()
        self.guard_error = False
        self.c = AddressRemovalController(self.s, self.guard)

    def tearDown(self):
        self.assertEqual(self.lab.opens, self.lab.closes)
        self.open_patch.stop()
        self.tmp.cleanup()

    def open(self):
        self.lab.opens += 1
        return self.lab, copy.deepcopy(self.info)

    def guard(self):
        if self.guard_error:
            raise RuntimeError('ConfigMaster open')

    def prepare(self):
        return self.c.prepare(2, 0)

    def allow(self, p):
        self.s.config.update(enable_experimental_address_removal=True,
            allowed_address_removal_digest=p['digest'], allowed_address_removals=[dict(channel=2, address=0)])

    def execute(self, p):
        return self.c.execute(p['plan_id'], p['confirmation'])

    def rewrite(self, p):
        q={k:v for k,v in p.items() if k != 'confirmation'}
        q['digest']=self.c.base._digest(q)
        self.c._save('plan', q['plan_id'], q)
        return dict(q, confirmation=self.c._confirmation(q))

    def test_backup_single_removal_preserves_remaining_model_and_records_limits(self):
        before=copy.deepcopy(self.lab.lamps)
        p=self.prepare();self.assertEqual(p['original'], {**before[0], 'captured_at':p['original']['captured_at']})
        self.assertFalse(self.lab.writes);self.allow(p)
        r=self.execute(p)
        self.assertEqual(r['state'], 'completed_address_removal_readback')
        self.assertEqual(self.lab.writes, [(2,0,255)])
        self.assertEqual(self.lab.lamps, {a:v for a,v in before.items() if a != 0})
        self.assertEqual(self.lab.removed,before[0])
        self.assertFalse(r['unaddressed_lamp_configuration_verified'])
        self.assertFalse(r['automatic_undo'])
        got=self.c.get(p['plan_id']);self.assertTrue(got['claimed']);self.assertEqual(got['execution'],r)
        inv=list(self.s.state_dir.glob('capability-invalidation-*.json'))
        self.assertEqual(len(inv),1);self.assertEqual(json.loads(inv[0].read_text())['addresses'],[0])

    def test_invalid_scope_before_network(self):
        for args in ((0,0),(3,0),(True,0),(2,-1),(2,64),(2,255),(2,False),(2,0.0),(2,'0')):
            with self.assertRaises(ValueError):self.c.prepare(*args)
        self.assertEqual(self.lab.opens,0)

    def test_missing_duplicate_and_non_dt8_refuse(self):
        with self.assertRaisesRegex(ValueError,'absent'):self.c.prepare(2,63)
        self.lab.duplicates=[0]
        with self.assertRaisesRegex(ValueError,'duplicate'):self.prepare()
        self.lab.duplicates=[];self.lab.lamps[0]['parameters']['light_type']['value']['code']=0
        with self.assertRaisesRegex(ValueError,'DT8'):self.prepare()
        self.assertFalse(self.lab.writes)

    def test_separate_exact_gates_all_required(self):
        p=self.prepare()
        for changes in ({'enable_experimental_address_removal':False},
                        {'allowed_address_removal_digest':'bad'},
                        {'allowed_address_removals':[dict(channel=2,address=1)]},
                        {'allowed_address_removals':[dict(channel=True,address=0)]},
                        {'allowed_address_removals':[dict(channel=2,address=0,extra=1)]}):
            self.allow(p);self.s.config.update(changes)
            with self.assertRaises(PermissionError):self.execute(p)
        self.assertFalse(self.c.get(p['plan_id'])['claimed']);self.assertFalse(self.lab.writes)

    def test_wrong_confirmation_and_expiry_prevent_claim(self):
        p=self.prepare();self.allow(p)
        with self.assertRaisesRegex(ValueError,'confirmation'):self.c.execute(p['plan_id'],'yes')
        with patch('dali_mcp.address_removal.time.time',return_value=p['expires_at']+1):
            with self.assertRaisesRegex(ValueError,'expired'):self.execute(p)
        self.assertFalse(self.c.get(p['plan_id'])['claimed'])

    def test_closed_app_guard_preparation_and_execution(self):
        self.guard_error=True
        with self.assertRaises(RuntimeError):self.prepare()
        self.assertEqual(self.lab.opens,0)
        self.guard_error=False;p=self.prepare();self.allow(p);self.guard_error=True
        with self.assertRaises(RuntimeError):self.execute(p)
        self.assertFalse(self.lab.writes)

    def test_occupancy_drift_stops_before_write(self):
        p=self.prepare();self.allow(p);self.lab.lamps[9]=self.lab.lamp(9)
        r=self.execute(p);self.assertEqual(r['state'],'preflight_failed');self.assertFalse(r['write_intent'])
        self.assertFalse(self.lab.writes)

    def test_parameter_scene_and_live_drift_stop_before_write(self):
        changes=[lambda row:row['scenes'][15].update(level_raw=201),
                 lambda row:row['actual'].update(level_raw=10),
                 lambda row:row['parameters']['fade_time_rate']['value'].update(raw=87)]
        for change in changes:
            self.lab.lamps[0]=self.lab.lamp(0)
            p=self.prepare();self.allow(p);change(self.lab.lamps[0])
            r=self.execute(p);self.assertEqual(r['state'],'preflight_failed');self.assertIn('source',r['error'])
        self.assertFalse(self.lab.writes)

    def test_final_scan_catches_change_during_backup(self):
        p=self.prepare();self.allow(p)
        self.lab.on_read=lambda lab:lab.lamps.setdefault(9,lab.lamp(9))
        r=self.execute(p);self.assertEqual(r['state'],'preflight_failed');self.assertIn('during preflight',r['error'])
        self.assertFalse(self.lab.writes)

    def test_permission_change_during_preflight_is_rechecked(self):
        p=self.prepare();self.allow(p)
        self.lab.on_read=lambda lab:self.s.config.update(enable_experimental_address_removal=False)
        self.assertEqual(self.execute(p)['state'],'preflight_failed');self.assertFalse(self.lab.writes)

    def test_gateway_changed_refuses(self):
        p=self.prepare();self.allow(p);self.info['dali_mac']='02:00:00:00:00:02'
        self.assertEqual(self.execute(p)['state'],'preflight_failed');self.assertFalse(self.lab.writes)

    def test_lost_receipt_reads_without_replay(self):
        p=self.prepare();self.allow(p);self.lab.fault='lost'
        r=self.execute(p);self.assertEqual(r['state'],'verified_receipt_unknown')
        self.assertIn('dispatch_error',r);self.assertEqual(len(self.lab.writes),1)
        with self.assertRaises(ValueError):self.execute(p)
        self.assertEqual(len(self.lab.writes),1)

    def test_no_apply_or_extra_occupancy_never_success(self):
        for fault in ('ignored','lost_not_applied','changed_unrelated_address'):
            with self.subTest(fault=fault):
                self.lab.lamps={a:self.lab.lamp(a) for a in (0,1,7)};self.lab.fault=None
                p=self.prepare();self.allow(p);self.lab.fault=fault
                self.assertEqual(self.execute(p)['state'],'stopped_address_mismatch')
        self.assertEqual(len(self.lab.writes),3)

    def test_post_scan_failure_and_interrupt_preserve_intent(self):
        p=self.prepare();self.allow(p);self.lab.fault='scan_failure'
        r=self.execute(p);self.assertEqual(r['state'],'stopped_unknown');self.assertTrue(r['write_intent'])
        self.assertEqual(self.c.get(p['plan_id'])['execution']['attempt'],dict(channel=2,address=0))

    def test_keyboard_interrupt_after_dispatch_still_reads(self):
        p=self.prepare();self.allow(p);self.lab.fault='interrupt'
        self.assertEqual(self.execute(p)['state'],'verified_receipt_unknown');self.assertEqual(len(self.lab.writes),1)

    def test_intent_write_failure_sends_nothing(self):
        p=self.prepare();self.allow(p);original=self.c._save
        def save(kind,ident,data):
            if kind=='run' and data['state']=='writing':raise OSError('disk failed')
            return original(kind,ident,data)
        with patch.object(self.c,'_save',save):r=self.execute(p)
        self.assertEqual(r['state'],'stopped_unknown');self.assertFalse(self.lab.writes)
        self.assertTrue(self.c.get(p['plan_id'])['claimed'])

    def test_missing_execution_record_is_visible_and_never_replayed(self):
        p=self.prepare();self.allow(p);r=self.execute(p)
        self.c._path('run',r['execution_id']).unlink()
        got=self.c.get(p['plan_id']);self.assertTrue(got['execution_record_missing'])
        with self.assertRaises(ValueError):self.execute(p)
        self.assertEqual(len(self.lab.writes),1)

    def test_plan_tampering_rejected_even_with_recomputed_digest(self):
        p=self.prepare()
        for key,value in [('after_addresses',[1]),('automatic_undo',True),('operation','allocate'),('expires_at',float('nan'))]:
            q=copy.deepcopy(p);q[key]=value
            if key=='expires_at':
                with self.assertRaises(ValueError):self.rewrite(q)
                continue
            self.rewrite(q)
            with self.assertRaises(ValueError):self.c.get(p['plan_id'])
        self.rewrite(p)
        path=self.c._path('plan',p['plan_id']);q=json.loads(path.read_text());q['address']=1;path.write_text(json.dumps(q))
        with self.assertRaisesRegex(ValueError,'checksum'):self.c.get(p['plan_id'])

    def test_get_does_not_open_gateway_and_new_controller_cannot_replay(self):
        p=self.prepare();n=self.lab.opens;self.c.get(p['plan_id']);self.assertEqual(self.lab.opens,n)
        self.allow(p);self.execute(p)
        other=AddressRemovalController(self.s,self.guard)
        self.assertEqual(other.get(p['plan_id'])['execution']['state'],'completed_address_removal_readback')
        with self.assertRaises(ValueError):other.execute(p['plan_id'],p['confirmation'])
        self.assertEqual(len(self.lab.writes),1)

    def test_guard_change_during_backup_prevents_dispatch(self):
        p=self.prepare();self.allow(p)
        def change(lab):self.guard_error=True
        self.lab.on_read=change
        self.assertEqual(self.execute(p)['state'],'preflight_failed');self.assertFalse(self.lab.writes)

    def test_plan_expiring_during_reads_prevents_dispatch(self):
        p=self.prepare();self.allow(p);clock=[p['created_at']]
        self.lab.on_read=lambda lab:clock.__setitem__(0,p['expires_at']+1)
        with patch('dali_mcp.address_removal.time.time',side_effect=lambda:clock[0]):
            r=self.execute(p)
        self.assertEqual(r['state'],'preflight_failed');self.assertIn('expired during',r['error'])
        self.assertFalse(self.lab.writes)

    def test_last_address_is_supported_not_implicit_reset(self):
        self.lab.lamps={63:self.lab.lamp(63)}
        p=self.c.prepare(2,63)
        self.s.config.update(enable_experimental_address_removal=True,allowed_address_removal_digest=p['digest'],allowed_address_removals=[dict(channel=2,address=63)])
        r=self.execute(p)
        self.assertEqual(r['state'],'completed_address_removal_readback');self.assertEqual(self.lab.writes,[(2,63,255)])
        self.assertEqual(r['after_scan']['addresses'],[])

    def test_fsync_failure_before_intent_publication_sends_no_packet(self):
        p=self.prepare();self.allow(p)
        import os
        real_fsync=os.fsync;calls=[]
        def sync(fd):
            calls.append(fd)
            # claim, initial run, consumed plan, then dispatch intent.
            if len(calls)==4:raise OSError('intent fsync failed')
            return real_fsync(fd)
        with patch('dali_mcp.address_removal.os.fsync',side_effect=sync):r=self.execute(p)
        self.assertEqual(r['state'],'stopped_unknown');self.assertIn('fsync',r['error'])
        self.assertFalse(self.lab.writes)
        with self.assertRaises(ValueError):self.execute(p)


if __name__=='__main__':unittest.main()
