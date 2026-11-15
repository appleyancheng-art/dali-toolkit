import copy
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from dali_mcp.service import Service
from dali_mcp.protocol import Gateway
from dali_mcp.address_moves import AddressMoveController, signature


class AddressLab:
    def __init__(self):
        self.lamps={a:self.lamp(a) for a in (0,1,7)}
        self.writes=[];self.opens=0;self.closes=0;self.reads=0;self.recent_packets=[]
        self.duplicates=[];self.fault=None;self.on_read=None;self.on_scan=None;self.scans=0

    @staticmethod
    def lamp(address):
        params={k:dict(value={'raw':0}) for k in Gateway.PARAMETER_FIELDS}
        params['light_type']={'value':dict(code=1,name='DT8')}
        params['fade_time_rate']={'value':dict(raw=71,fade_time_code=4,fade_rate_code=7)}
        params['groups']={'value':dict(groups=[0,1],group_mask=3)}
        return dict(channel=2,address=address,parameters=params,
            scenes=[dict(channel=2,address=address,scene=n,light_type_code=1,level_raw=address+n,kelvin_raw=4000) for n in range(16)],
            actual=dict(channel=2,address=address,light_type_code=1,level_raw=0,kelvin_raw=4000,second_level_raw=0,second_kelvin_raw=0))

    def close(self):self.closes+=1
    def scan_existing(self,ch):
        self.scans+=1
        if self.on_scan:self.on_scan(self)
        if self.writes and self.fault=='scan_failure':raise TimeoutError('scan failed')
        return dict(channel=ch,addresses=sorted(self.lamps))
    def read_duplicate_addresses(self,ch):return dict(channel=ch,duplicate_addresses=self.duplicates[:])
    def read_lamp_parameter(self,ch,a,f,t=None):
        self.reads+=1
        if self.on_read:self.on_read(self)
        if self.writes and self.fault=='read_failure':raise TimeoutError('read failed')
        return copy.deepcopy(self.lamps[a]['parameters'][f])
    def read_scene(self,ch,a,n,t):return copy.deepcopy(self.lamps[a]['scenes'][n])
    def read_actual(self,ch,a,dt8):return copy.deepcopy(self.lamps[a]['actual'])
    def change_short_address(self,ch,src,dst):
        assert dst not in self.lamps and src in self.lamps
        self.writes.append((ch,src,dst))
        if self.fault not in ('ignored','lost_not_applied'):
            row=self.lamps.pop(src);row['address']=dst;row['actual']['address']=dst
            for scene in row['scenes']:scene['address']=dst
            self.lamps[dst]=row
        if self.fault=='changed_scene':self.lamps[dst]['scenes'][15]['level_raw']=254
        if self.fault=='changed_live':self.lamps[dst]['actual']['level_raw']=254
        if self.fault=='changed_unrelated_address':self.lamps[9]=self.lamp(9)
        if self.fault in ('lost','lost_not_applied'):raise TimeoutError('receipt lost')
        if self.fault=='interrupt':raise KeyboardInterrupt()
        return dict(result=1,receipt_only=True)


class AddressMoveTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.lab=AddressLab()
        self.info=dict(ip='127.0.0.1',dali_mac='02:00:00:00:00:01',dali_firmware='1.6.9')
        self.s=Service(dict(gateway_ip=self.info['ip'],expected_dali_mac=self.info['dali_mac'],expected_firmware='1.6.9'),self.tmp.name)
        self.open_patch=patch.object(self.s,'_open',self.open);self.open_patch.start()
        self.guard_calls=0;self.guard_error=False
        self.c=AddressMoveController(self.s,self.guard)
    def tearDown(self):
        self.assertEqual(self.lab.opens,self.lab.closes)
        self.open_patch.stop();self.tmp.cleanup()
    def open(self):self.lab.opens+=1;return self.lab,copy.deepcopy(self.info)
    def guard(self):
        self.guard_calls+=1
        if self.guard_error:raise RuntimeError('ConfigMaster open')
    def prepare(self):return self.c.prepare(2,0,63)
    def allow(self,p):
        self.s.config.update(enable_experimental_address_writes=True,allowed_address_moves=[
            {k:p[k] for k in ('channel','source_address','destination_address')}])
    def execute(self,p):return self.c.execute(p['plan_id'],p['confirmation'])
    def rewrite(self,p):
        p={k:v for k,v in p.items() if k!='confirmation'};p['digest']=self.c._digest(p)
        self.s._save('address-plan',p,p['plan_id']);return p

    def test_backup_move_and_conditional_roundtrip(self):
        p=self.prepare();self.assertEqual(len(p['original']['scenes']),16);self.allow(p)
        r=self.execute(p);self.assertEqual(r['state'],'completed_readback')
        self.assertEqual(self.lab.writes,[(2,0,63)]);self.assertEqual(sorted(self.lab.lamps),[1,7,63])
        self.assertEqual(signature(r['after_lamp']),signature(p['original']))
        invalidations=list(self.s.state_dir.glob('capability-invalidation-*.json'));self.assertEqual(len(invalidations),1)
        self.assertEqual(json.loads(invalidations[0].read_text())['addresses'],[0,63])
        undo=self.c.prepare_restore(p['plan_id']);self.assertEqual(undo['restore_of'],p['plan_id'])
        self.allow(undo);self.assertEqual(self.execute(undo)['state'],'completed_readback')
        self.assertEqual(sorted(self.lab.lamps),[0,1,7]);self.assertEqual(self.c.prepare_restore(p['plan_id'])['state'],'already_original')

    def test_occupied_or_missing_no_write(self):
        for src,dst in ((0,7),(5,63)):
            with self.assertRaises(ValueError):self.c.prepare(2,src,dst)
        self.assertEqual(self.lab.writes,[])

    def test_invalid_scope_before_network(self):
        for args in [(0,0,63),(True,0,63),(2,0,0),(2,0,255),(2,-1,63),(2,0,64),(2,False,63),(2,0,63.0)]:
            with self.assertRaises(ValueError):self.c.prepare(*args)
        self.assertEqual(self.lab.opens,0)

    def test_process_guard_before_reads_and_before_write(self):
        self.guard_error=True
        with self.assertRaises(RuntimeError):self.prepare()
        self.assertEqual(self.lab.opens,0)
        self.guard_error=False;p=self.prepare();self.allow(p);self.guard_error=True
        with self.assertRaises(RuntimeError):self.execute(p)
        self.assertFalse(self.lab.writes)

    def test_duplicate_report_blocks_preparation(self):
        self.lab.duplicates=[7]
        with self.assertRaisesRegex(ValueError,'duplicate'):self.prepare()
        self.assertEqual(self.lab.writes,[])

    def test_wrong_type_backup_rejected(self):
        self.lab.lamps[0]['parameters']['light_type']['value']['code']=0
        with self.assertRaisesRegex(ValueError,'DT8'):self.prepare()

    def test_gate_and_confirmation_before_network(self):
        p=self.prepare();count=self.lab.opens
        with self.assertRaises(PermissionError):self.execute(p)
        self.allow(p);self.s.config['allowed_address_moves'][0]['destination_address']=62
        with self.assertRaises(PermissionError):self.execute(p)
        self.allow(p)
        with self.assertRaises(ValueError):self.c.execute(p['plan_id'],'wrong')
        self.assertEqual(count,self.lab.opens)

    def test_bad_or_broadened_gate_rejected(self):
        p=self.prepare();self.allow(p)
        for value in ([],[dict(channel=0,source_address=0,destination_address=63)],
                      [dict(channel=2,source_address=0,destination_address=255)],
                      [dict(channel=2,source_address=0,destination_address=63,extra=True)]):
            self.s.config['allowed_address_moves']=value
            with self.assertRaises(PermissionError):self.execute(p)

    def test_preflight_occupancy_change(self):
        p=self.prepare();self.allow(p);self.lab.lamps[63]=self.lab.lamp(63)
        self.assertEqual(self.execute(p)['state'],'preflight_failed');self.assertEqual(self.lab.writes,[])

    def test_preflight_scene_or_live_change(self):
        p=self.prepare();self.allow(p);self.lab.lamps[0]['scenes'][5]['kelvin_raw']=3000
        self.assertEqual(self.execute(p)['state'],'preflight_failed');self.assertEqual(self.lab.writes,[])

    def test_last_vacancy_check_after_slow_backup(self):
        p=self.prepare();self.allow(p)
        def occupy(lab):lab.lamps[63]=lab.lamp(63)
        self.lab.on_read=occupy
        self.assertEqual(self.execute(p)['state'],'preflight_failed');self.assertEqual(self.lab.writes,[])

    def test_expiry_and_tampered_mapping(self):
        p=self.prepare();self.allow(p);p['created_at']=time.time()-1000;p['expires_at']=p['created_at']+600;self.rewrite(p)
        with self.assertRaisesRegex(ValueError,'expired'):self.c.execute(p['plan_id'],self.c._confirmation(self.c._load(p['plan_id'])))
        p=self.prepare();p['after_addresses']=[63];self.rewrite(p)
        with self.assertRaisesRegex(ValueError,'mapping'):self.c.get(p['plan_id'])
        self.assertEqual(self.lab.writes,[])

    def test_missing_receipt_readback_and_restore_no_replay(self):
        p=self.prepare();self.allow(p);self.lab.fault='lost'
        self.assertEqual(self.execute(p)['state'],'verified_receipt_unknown');self.assertEqual(len(self.lab.writes),1)
        self.lab.fault=None;undo=self.c.prepare_restore(p['plan_id']);self.allow(undo)
        self.assertEqual(self.execute(undo)['state'],'completed_readback');self.assertEqual(len(self.lab.writes),2)

    def test_missing_receipt_not_applied_already_original(self):
        p=self.prepare();self.allow(p);self.lab.fault='lost_not_applied'
        self.assertEqual(self.execute(p)['state'],'stopped_address_mismatch')
        self.assertEqual(self.c.prepare_restore(p['plan_id'])['state'],'already_original');self.assertEqual(len(self.lab.writes),1)

    def test_wrong_readback_never_reports_success_or_blind_undo(self):
        for fault in ('changed_scene','changed_live','changed_unrelated_address'):
            with self.subTest(fault=fault):
                self.lab.lamps={a:self.lab.lamp(a) for a in (0,1,7)};self.lab.fault=None
                p=self.prepare();self.allow(p);self.lab.fault=fault
                self.assertTrue(self.execute(p)['state'].startswith('stopped_'));self.lab.fault=None
                with self.assertRaises(ValueError):self.c.prepare_restore(p['plan_id'])

    def test_readback_failure_keeps_intent_and_can_later_recover(self):
        p=self.prepare();self.allow(p);self.lab.fault='read_failure'
        r=self.execute(p);self.assertEqual(r['state'],'stopped_unknown');self.assertTrue(r['write_intent'])
        self.lab.fault=None;self.assertEqual(self.c.prepare_restore(p['plan_id'])['source_address'],63)

    def test_keyboard_interrupt_after_send_readback_not_repeat(self):
        p=self.prepare();self.allow(p);self.lab.fault='interrupt'
        self.assertEqual(self.execute(p)['state'],'verified_receipt_unknown');self.assertEqual(len(self.lab.writes),1)

    def test_replay_claim_survives_plan_rewind(self):
        p=self.prepare();self.allow(p);self.execute(p)
        old={k:v for k,v in p.items() if k!='confirmation'};self.s._save('address-plan',old,p['plan_id'])
        with self.assertRaises(FileExistsError):self.execute(p)
        self.assertEqual(len(self.lab.writes),1)

    def test_missing_journal_not_restorable_and_get_not_network(self):
        p=self.prepare();self.allow(p);run=self.execute(p)
        (self.s.state_dir/f"address-run-{run['execution_id']}.json").unlink()
        opens=self.lab.opens;r=self.c.get(p['plan_id'])
        self.assertTrue(r['execution_record_missing']);self.assertEqual(opens,self.lab.opens)
        with self.assertRaisesRegex(ValueError,'durable'):self.c.prepare_restore(p['plan_id'])

    def test_no_attempt_no_restore(self):
        p=self.prepare();self.allow(p);self.lab.lamps[0]['actual']['level_raw']=20
        self.execute(p)
        with self.assertRaisesRegex(ValueError,'durable'):self.c.prepare_restore(p['plan_id'])

    def test_mismatched_claim_rejected(self):
        p=self.prepare();self.allow(p);self.execute(p)
        cp=self.s.state_dir/f"address-claim-{p['plan_id']}.json";c=json.loads(cp.read_text());c['plan_id']='a'*32;cp.write_text(json.dumps(c))
        with self.assertRaises(ValueError):self.c.get(p['plan_id'])

    def test_gateway_change_refused(self):
        p=self.prepare();self.allow(p);self.info['dali_mac']='02:00:00:00:00:02'
        self.assertEqual(self.execute(p)['state'],'preflight_failed');self.assertEqual(self.lab.writes,[])

    def test_restore_parent_missing_or_tampered_refused(self):
        p=self.prepare();self.allow(p);self.execute(p);undo=self.c.prepare_restore(p['plan_id'])
        undo['source_address']=7;undo['original']['address']=7
        for scene in undo['original']['scenes']:scene['address']=7
        undo['original']['actual']['address']=7
        undo['after_addresses']=[0,1,63];self.rewrite(undo)
        with self.assertRaisesRegex(ValueError,'restoration'):self.c.get(undo['plan_id'])

    def test_claim_fsync_failure_and_intent_log_failure_send_nothing(self):
        p=self.prepare();self.allow(p)
        with patch('dali_mcp.address_moves.os.fsync',side_effect=OSError('disk')):
            with self.assertRaises(OSError):self.execute(p)
        with self.assertRaises(FileExistsError):self.execute(p)
        p=self.prepare();self.allow(p);save=self.s._save
        def fail_intent(prefix,value,ident=None):
            if prefix=='address-run' and value.get('state')=='writing':raise OSError('intent disk failure')
            return save(prefix,value,ident)
        with patch.object(self.s,'_save',side_effect=fail_intent):r=self.execute(p)
        self.assertEqual(r['state'],'stopped_unknown');self.assertEqual(self.lab.writes,[])

    def test_get_does_not_wait_on_operation_lock(self):
        p=self.prepare()
        with patch.object(self.c,'_locked',side_effect=AssertionError('must not lock')):
            self.assertEqual(self.c.get(p['plan_id'])['state'],'prepared')

    def test_guard_recheck_after_preflight_sends_nothing(self):
        p=self.prepare();self.allow(p)
        self.lab.on_read=lambda lab:setattr(self,'guard_error',True)
        self.assertEqual(self.execute(p)['state'],'preflight_failed');self.assertEqual(self.lab.writes,[])


if __name__=='__main__':unittest.main()
