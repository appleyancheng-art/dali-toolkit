import copy
import tempfile
import time
import unittest
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import patch
from dali_mcp.group_execution import ExperimentalGroupController, send_group_once
from dali_mcp.service import Service


class GroupLab:
    def __init__(self):
        self.masks={0:3,63:3,1:5}
        self.levels={a:0 for a in self.masks}
        self.types={a:1 for a in self.masks}
        self.route=bytes.fromhex('0135010115f0');self.seq=10
        self.sock=SimpleNamespace(getsockname=lambda:('127.0.0.53',19000))
        self.writes=[];self.fault=None;self.reads=0;self.on_read=None

    def close(self):pass

    def read_groups(self,channel,address):
        self.reads+=1
        if self.on_read:self.on_read(self)
        if self.writes and self.fault=='read_fail':raise TimeoutError('read failure')
        mask=self.masks[address]
        return dict(channel=channel,address=address,group_mask=mask,
                    groups=[g for g in range(16) if mask & (1<<g)])

    def light_type(self,channel,address):return self.types[address]

    def read_actual(self,channel,address,is_dt8):
        assert is_dt8 == (self.types[address]==1)
        return dict(light_type_code=self.types[address],level_raw=self.levels[address],kelvin_raw=4000)

    def exchange(self,command,payload,route,accept):
        assert command==0xf015 and len(payload)==8 and payload[0:2]==b'\x02\x01'
        assert payload[3] in (0x60,0x70) and payload[5:]==b'\x00\x00\x00'
        address,group=payload[2],payload[4]
        self.seq+=1;self.writes.append((command,payload))
        if self.fault not in ('ignored','lost_not_applied'):
            if payload[3]==0x60:self.masks[address]|=1<<group
            else:self.masks[address]&=~(1<<group)
        if self.fault=='other_bit':self.masks[address]^=128
        if self.fault=='live':self.levels[address]=1
        if self.fault in ('lost','lost_not_applied'):raise TimeoutError('receipt lost')
        if self.fault=='interrupt':raise KeyboardInterrupt()
        reply=SimpleNamespace(command=0xf095,payload=payload[:3]+bytes([254])*5,
                              sequence=self.seq,route=route[3:]+route[:3],transport=1)
        assert accept(reply)
        return reply


class GroupExecutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.g=GroupLab();self.opens=0
        self.info=dict(ip='127.0.0.1',dali_mac='02:00:00:00:00:01',dali_firmware='test')
        self.service=Service({'gateway_ip':self.info['ip'],'expected_dali_mac':self.info['dali_mac'],
                              'expected_firmware':'test'},self.tmp.name)
        self.controller=ExperimentalGroupController(self.service)
        self.open_patch=patch.object(self.service,'_open',self.open);self.open_patch.start()

    def tearDown(self):self.open_patch.stop();self.tmp.cleanup()

    def open(self):self.opens+=1;return self.g,copy.deepcopy(self.info)

    def plan(self,addresses=(0,63)):
        return self.controller.prepare(2,[{'address':a,'add':[15],'remove':[]} for a in addresses])

    def allow(self,plan):
        self.service.config.update(enable_experimental_group_writes=True,
            allowed_group_write_operations=[dict(channel=plan['channel'],
                **{k:s[k] for k in ('address','group','member')}) for s in plan['proposal']['steps']]
            or [dict(channel=2,address=0,group=15,member=True)])

    def execute(self,plan):return self.controller.execute(plan['plan_id'],plan['confirmation'])

    def edit_plan(self,plan,edit,rehash=True):
        document=copy.deepcopy(plan);document.pop('confirmation');edit(document)
        if rehash:document['digest']=self.controller._digest(document)
        self.service._save('group-plan',document,plan['plan_id'])
        document['confirmation']=self.controller._confirmation(document)
        return document

    def test_exact_membership_roundtrip_separate_restore_no_replay(self):
        p=self.plan();self.allow(p);r=self.execute(p)
        self.assertEqual(r['state'],'completed_readback');self.assertEqual(len(self.g.writes),2)
        self.assertEqual(self.g.masks,{0:32771,63:32771,1:5})
        self.assertFalse(r['steps'][0]['receipt']['status_interpreted'])
        with self.assertRaises(ValueError):self.execute(p)
        restore=self.controller.prepare_restore(p['plan_id'])
        with self.assertRaises(PermissionError):self.execute(restore)
        self.allow(restore);self.assertEqual(self.execute(restore)['state'],'completed_readback')
        self.assertEqual(self.g.masks,{0:3,63:3,1:5})
        self.assertEqual(self.controller.prepare_restore(p['plan_id'])['state'],'already_original')
        self.assertEqual(len(self.g.writes),4)

    def test_lost_ack_applied_once_partial_restore_preserves_scope(self):
        p=self.plan();self.allow(p);self.g.fault='lost';r=self.execute(p)
        self.assertEqual(r['state'],'stopped_receipt_unknown')
        self.assertEqual(r['steps'][0]['state'],'verified_receipt_unknown')
        self.assertEqual(len(self.g.writes),1);self.assertEqual(self.opens,3)
        self.g.fault=None
        restore=self.controller.prepare_restore(p['plan_id'])
        self.assertEqual([s['address'] for s in restore['proposal']['steps']],[0])
        self.assertEqual(len(restore['baseline']),2)
        self.allow(restore);self.assertEqual(self.execute(restore)['state'],'completed_readback')
        self.assertEqual(self.g.masks,{0:3,63:3,1:5})

    def test_separate_switch_exact_scope_and_strict_gate(self):
        p=self.plan();n=self.opens
        configurations=[{},dict(enable_experimental_writes=True),
            dict(enable_experimental_group_writes=True,allowed_group_write_operations=[]),
            dict(enable_experimental_group_writes=True,allowed_group_write_operations=[dict(channel=2,address=0,group=15,member=1)]),
            dict(enable_experimental_group_writes=True,allowed_group_write_operations=[dict(channel=2,address=1,group=15,member=True)])]
        for config in configurations:
            self.service.config.clear();self.service.config.update(config)
            with self.assertRaises(PermissionError):self.execute(p)
        self.assertEqual(self.opens,n);self.assertFalse(self.g.writes)

    def test_invalid_preparation_rejected_before_read(self):
        for changes in [[],None,[{'address':True,'add':[1],'remove':[]}],
                        [{'address':0,'add':[1],'remove':[1]}],[{'address':64,'add':[1],'remove':[]}]]:
            with self.assertRaises(ValueError):self.controller.prepare(2,changes)
        self.assertEqual(self.opens,0)

    def test_saved_payload_tamper_is_not_executable_even_rehashed(self):
        p=self.plan();self.allow(p)
        p=self.edit_plan(p,lambda d:d['proposal']['steps'][0].update(command='F039'))
        n=self.opens
        with self.assertRaises(ValueError):self.execute(p)
        self.assertEqual(self.opens,n);self.assertFalse(self.g.writes)

    def test_confirmation_checksum_and_expiry_before_open(self):
        p=self.plan();self.allow(p);n=self.opens
        with self.assertRaisesRegex(ValueError,'confirmation'):
            self.controller.execute(p['plan_id'],'incorrect')
        self.edit_plan(p,lambda d:d.update(expires_at=0),rehash=False)
        with self.assertRaisesRegex(ValueError,'checksum'):self.execute(p)
        p=self.edit_plan(p,lambda d:d.update(expires_at=time.time()-1))
        with self.assertRaisesRegex(ValueError,'expired'):self.execute(p)
        self.assertEqual(self.opens,n)

    def test_preflight_changes_stop_and_claim_blocks_fresh_controller(self):
        p=self.plan();self.allow(p);self.g.masks[63]=5
        r=self.execute(p);self.assertEqual(r['state'],'preflight_failed');self.assertFalse(self.g.writes)
        other=ExperimentalGroupController(self.service)
        self.g.masks[63]=3;n=self.opens
        with self.assertRaises(FileExistsError):other.execute(p['plan_id'],p['confirmation'])
        self.assertEqual(self.opens,n)

    def test_gateway_or_live_preflight_change_stops(self):
        for fault in ('gateway','live'):
            p=self.plan();self.allow(p)
            if fault=='gateway':self.info['dali_firmware']='changed'
            else:self.g.levels[0]=1
            self.assertEqual(self.execute(p)['state'],'preflight_failed')
            self.info['dali_firmware']='test';self.g.levels[0]=0
        self.assertFalse(self.g.writes)

    def test_write_faults_stop_after_one_attempt(self):
        for fault,expected in [('ignored','stopped_verification_failed'),('other_bit','stopped_verification_failed'),
            ('live','stopped_verification_failed'),('read_fail','stopped_unknown'),
            ('interrupt','stopped_receipt_unknown'),('lost_not_applied','stopped_verification_failed')]:
            self.g=GroupLab();p=self.plan();self.allow(p);self.g.fault=fault
            r=self.execute(p)
            self.assertEqual(r['state'],expected,fault);self.assertEqual(len(self.g.writes),1)

    def test_conflict_between_writes_stops_without_touching_second_address(self):
        p=self.plan();self.allow(p)
        def drift(g):
            if g.reads==7:g.masks[63]=5
        self.g.on_read=drift;r=self.execute(p)
        self.assertEqual(r['state'],'stopped_conflict');self.assertEqual(len(self.g.writes),1)

    def test_final_full_scope_read_detects_drift_on_earlier_address(self):
        p=self.plan();self.allow(p)
        def drift(g):
            if g.reads==9:g.masks[0]^=128
        self.g.on_read=drift;r=self.execute(p)
        self.assertEqual(r['state'],'stopped_final_conflict');self.assertEqual(len(self.g.writes),2)

    def test_journal_failures_before_send_and_after_send_no_replay(self):
        for when in ('initial','consume','intent','after'):
            self.g=GroupLab();p=self.plan();self.allow(p);save=self.service._save
            def failing(kind,value,*args):
                if ((when=='initial' and kind=='group-run') or
                    (when=='consume' and kind=='group-plan') or
                    (when=='intent' and kind=='group-run' and value['steps']) or
                    (when=='after' and self.g.writes)):
                    raise OSError('simulated disk failure')
                return save(kind,value,*args)
            with patch.object(self.service,'_save',failing):r=self.execute(p)
            self.assertEqual(len(self.g.writes),1 if when=='after' else 0,when)
            self.assertNotEqual(r['state'],'completed_readback')
            with self.assertRaises((ValueError,FileExistsError)):self.execute(p)

    def test_restore_refuses_unrelated_change_and_unrecorded_progress(self):
        p=self.plan();self.allow(p);self.g.fault='lost';self.execute(p);self.g.fault=None
        self.g.masks[63]=32771
        with self.assertRaisesRegex(ValueError,'recorded attempted'):self.controller.prepare_restore(p['plan_id'])
        self.g.masks[63]=3;self.g.masks[0]^=128
        with self.assertRaisesRegex(ValueError,'conflict'):self.controller.prepare_restore(p['plan_id'])
        self.g.masks[0]^=128;self.g.levels[63]=1
        with self.assertRaisesRegex(ValueError,'live output'):self.controller.prepare_restore(p['plan_id'])

    def test_restore_ancestry_cannot_authorize_new_group(self):
        p=self.plan();self.allow(p);self.execute(p)
        restore=self.controller.prepare_restore(p['plan_id'])
        def tamper(d):
            from dali_mcp.group_planning import plan_group_candidates
            d['changes'][0]['add']=[14]
            d['proposal']=plan_group_candidates(2,[r['membership'] for r in d['baseline']],d['changes'])
        restore=self.edit_plan(restore,tamper)
        self.allow(restore);n=self.opens
        with self.assertRaisesRegex(ValueError,'restoration intent'):self.execute(restore)
        self.assertEqual(self.opens,n)

    def test_noop_and_dt6_membership_preserve_live_state(self):
        self.g.masks[0]=32771;self.g.types[0]=0
        p=self.plan((0,));self.allow(p)
        self.assertEqual(self.execute(p)['state'],'completed_readback')
        self.assertFalse(self.g.writes)
        p=self.controller.prepare(2,[{'address':0,'add':[],'remove':[15]}]);self.allow(p)
        self.assertEqual(self.execute(p)['state'],'completed_readback')
        self.assertEqual(self.g.masks[0],3);self.assertEqual(self.g.levels[0],0)

    def test_receipt_filter_rejects_wrong_correlation(self):
        accepted=[]
        def exchange(command,payload,route,accept):
            self.g.seq+=1
            reply=SimpleNamespace(command=0xf095,sequence=self.g.seq,transport=1,
                route=route[3:]+route[:3],payload=payload[:3]+bytes(5))
            for field,value in [('command',0xf093),('sequence',0),('transport',2),
                                ('route',bytes(6)),('payload',b'\x02\x01\x3f'+bytes(5))]:
                bad=copy.copy(reply);setattr(bad,field,value);accepted.append(accept(bad))
            self.assertTrue(accept(reply))
            reply.route=route[3:]+bytes([route[0],53,route[2]])
            self.assertTrue(accept(reply))
            return reply
        with patch.object(self.g,'exchange',exchange):send_group_once(self.g,2,0,15,True)
        self.assertEqual(accepted,[False]*5)

    def test_state_directory_lock_blocks_other_controller_until_released(self):
        other_service=Service(copy.deepcopy(self.service.config),self.tmp.name)
        other=ExperimentalGroupController(other_service)
        with self.controller._locked():
            with self.assertRaisesRegex(RuntimeError,'holds the state directory'):
                with other._locked():self.fail('concurrent group controller entered')
        with other._locked():pass

    def test_recovery_of_durable_intent_after_process_exit_requires_fresh_read(self):
        p=self.plan();self.allow(p);self.g.fault='lost';r=self.execute(p);self.g.fault=None
        # Simulate the durable record at the crash point; the previous executor
        # has exited and its OS lifetime lock is released.
        r['state']='writing';r['steps'][0].pop('readback');r['steps'][0]['state']='intent_recorded'
        self.service._save('group-run',r,r['execution_id'])
        restore=ExperimentalGroupController(self.service).prepare_restore(p['plan_id'])
        self.assertEqual(len(restore['proposal']['steps']),1)
        self.assertEqual(restore['proposal']['steps'][0]['address'],0)

    def test_os_lock_blocks_live_process_and_releases_after_termination(self):
        script = '''import pathlib,sys,threading
from types import SimpleNamespace
from dali_mcp.group_execution import ExperimentalGroupController
c=ExperimentalGroupController(SimpleNamespace(state_dir=pathlib.Path(sys.argv[1]),lock=threading.RLock()))
with c._locked():
 print('locked',flush=True)
 sys.stdin.readline()
'''
        child=subprocess.Popen([sys.executable,'-c',script,self.tmp.name],
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        try:
            self.assertEqual(child.stdout.readline().strip(),'locked')
            with self.assertRaisesRegex(RuntimeError,'holds the state directory'):
                with self.controller._locked():self.fail('entered while other process held the lock')
        finally:
            child.terminate();child.communicate(timeout=10)
        with self.controller._locked():pass

    def test_changed_gateway_after_lost_receipt_stops_without_read_or_resend(self):
        p=self.plan();self.allow(p);self.g.fault='lost'
        def reopen():
            gateway,info=self.open()
            if self.opens>=3:info['dali_firmware']='changed'
            return gateway,info
        with patch.object(self.service,'_open',reopen):result=self.execute(p)
        self.assertEqual(result['state'],'stopped_unknown')
        self.assertIn('gateway changed',result['error'])
        self.assertEqual(len(self.g.writes),1)

    def test_journal_get_is_readonly_and_does_not_wait_for_group_lock(self):
        p=self.plan();self.allow(p);self.execute(p);n=self.opens
        with self.controller._locked():
            record=self.controller.get(p['plan_id'])
            recent=self.controller.get()
        self.assertEqual(record['execution']['state'],'completed_readback')
        self.assertTrue(record['saved_phase_does_not_prove_process_running'])
        self.assertEqual(recent['plans'][0]['plan_id'],p['plan_id'])
        self.assertEqual(self.opens,n)

    def test_get_reports_claim_without_run_and_rejects_foreign_run(self):
        p=self.plan();run_id='a'*32
        self.service._save('group-claim',dict(plan_id=p['plan_id'],execution_id=run_id),p['plan_id'])
        self.assertTrue(self.controller.get(p['plan_id'])['execution_record_missing'])
        self.service._save('group-run',dict(plan_id='b'*32,execution_id=run_id),run_id)
        with self.assertRaises(ValueError):self.controller.get(p['plan_id'])


if __name__=='__main__':unittest.main()
