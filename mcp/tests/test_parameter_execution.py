import copy
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from dali_mcp.parameter_execution import ExperimentalParameterController,send_candidate_once
from dali_mcp.parameter_planning import encode_parameter_candidate
from dali_mcp.service import Service
from test_parameter_planning import evidence


class LabGateway:
    """In-memory fixture; deliberately decodes bytes independently of planner."""
    def __init__(self):
        self.parameters=evidence();self.live={'light_type_code':1,'level_raw':0,'kelvin_raw':4000}
        self.route=bytes.fromhex('0135010115f0');self.seq=10
        self.sock=SimpleNamespace(getsockname=lambda:('127.0.0.53',19000))
        self.writes=[];self.fault=None;self.closed=0

    def close(self):self.closed+=1

    def read_lamp_parameter(self,channel,address,field,typ):
        if self.writes and self.fault=='read_failure':raise TimeoutError('read failed after write')
        assert (channel,address)==(2,0)
        return copy.deepcopy(self.parameters[field])

    def read_actual(self,channel,address,color):return copy.deepcopy(self.live)

    def exchange(self,command,payload,route,accept):
        assert payload[:3]==bytes([2,1,0]) and len(payload)==8
        self.seq+=1;self.writes.append((command,payload))
        if self.fault!='ignore':
            if command in (0xf018,0xf019):
                fade=self.parameters['fade_time_rate']['value']
                fade['fade_time_code' if command==0xf018 else 'fade_rate_code']=payload[4]
                if self.fault=='sibling':fade['fade_rate_code']=2
                fade['raw']=fade['fade_time_code']*16+fade['fade_rate_code']
                fade['extended_fade_not_read']=fade['fade_time_code']==0
            elif command in (0xf01c,0xf01e):
                self.parameters['min_level' if command==0xf01c else 'max_level']['value']={'level_raw':payload[4]}
            elif command==0xf024:
                assert payload[3]==0xe3 and payload[4] in (0,1) and payload[5:]==b'\0'*3
                self.parameters['dimming_curve']['value']={'raw':payload[4]}
            elif command in (0xf026,0xf028):
                self.parameters['configured_max_ct' if command==0xf026 else 'configured_min_ct']['value']={'kelvin_raw':int.from_bytes(payload[4:6],'little')}
            elif command in (0xf020,0xf022):
                self.parameters['power_on' if command==0xf020 else 'system_failure']['value']={'level_raw':payload[4],'kelvin_raw':int.from_bytes(payload[5:7],'little')}
            else:raise AssertionError('unexpected command')
        if self.fault=='lost':raise TimeoutError('simulated lost receipt')
        if self.fault=='interrupt':raise KeyboardInterrupt()
        if self.fault=='live':self.live['level_raw']=1
        reply=SimpleNamespace(command=command+0x80,payload=payload[:3]+(b'\0' if command==0xf024 else b'\xfe')+b'\xff'*4,
                              sequence=self.seq,route=route[3:]+route[:3],transport=1)
        assert accept(reply)
        return reply


class ParameterExecutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.g=LabGateway();self.opens=0
        self.info={'ip':'127.0.0.1','dali_mac':'02:00:00:00:00:01','dali_firmware':'test'}
        self.service=Service({'gateway_ip':self.info['ip'],'expected_dali_mac':self.info['dali_mac'],
                              'expected_firmware':'test'},self.tmp.name)
        self.controller=ExperimentalParameterController(self.service)
        self.open_patch=patch.object(self.service,'_open',self.open);self.open_patch.start()

    def tearDown(self):self.open_patch.stop();self.tmp.cleanup()

    def open(self):self.opens+=1;return self.g,copy.deepcopy(self.info)

    def plan(self,changes=None):
        return self.controller.prepare(2,0,changes or [{'field':'fade_time','values':{'code':5}}])

    def allow(self,plan):
        self.service.config.update(enable_experimental_parameter_writes=True,allowed_parameter_write_targets={
            'channel':2,'address':0,'values':[{'field':s['field'],'values':s['after']} for s in plan['proposal']['steps']]})

    def execute(self,plan):return self.controller.execute(plan['plan_id'],plan['confirmation'])

    def edit_plan(self,plan,edit,rehash=True):
        document=copy.deepcopy(plan);document.pop('confirmation');edit(document)
        if rehash:document['digest']=self.controller._digest(document)
        self.service._save('parameter-plan',document,plan['plan_id'])

    def test_roundtrip_separate_restore_and_no_replay(self):
        p=self.plan();self.allow(p);r=self.execute(p)
        self.assertEqual(r['state'],'completed_readback');self.assertEqual(len(self.g.writes),1)
        self.assertEqual(self.g.parameters['fade_time_rate']['value']['raw'],0x57)
        self.assertFalse(r['steps'][0]['receipt']['status_interpreted'])
        with self.assertRaises(ValueError):self.execute(p)
        restore=self.controller.prepare_restore(p['plan_id']);self.assertEqual(len(self.g.writes),1)
        with self.assertRaises(PermissionError):self.execute(restore)
        self.allow(restore);self.assertEqual(self.execute(restore)['state'],'completed_readback')
        self.assertEqual(self.g.parameters,evidence());self.assertEqual(len(self.g.writes),2)
        self.assertEqual(self.controller.prepare_restore(p['plan_id'])['state'],'already_original')

    def test_independent_controllers_cannot_dispatch_same_plan_concurrently(self):
        p=self.plan();self.allow(p)
        other_service=Service(copy.deepcopy(self.service.config),self.tmp.name)
        other=ExperimentalParameterController(other_service)
        entered=threading.Event();release=threading.Event();results=[]
        original=self.controller._load
        def hold(*args,**kwargs):
            result=original(*args,**kwargs);entered.set()
            if not release.wait(5):raise RuntimeError('test barrier expired')
            return result
        def run():
            try:results.append(self.execute(p))
            except BaseException as exc:results.append(exc)
        with patch.object(self.controller,'_load',hold), patch.object(other_service,'_open') as other_open:
            worker=threading.Thread(target=run);worker.start()
            try:
                self.assertTrue(entered.wait(5))
                with self.assertRaisesRegex(RuntimeError,'holds the state directory'):
                    other.execute(p['plan_id'],p['confirmation'])
                other_open.assert_not_called()
            finally:
                release.set();worker.join(5)
        self.assertFalse(worker.is_alive());self.assertEqual(len(results),1)
        self.assertEqual(results[0]['state'],'completed_readback');self.assertEqual(len(self.g.writes),1)

    def test_os_lock_excludes_other_process_and_releases_after_process_exit(self):
        code="""
import sys
from dali_mcp.service import Service
from dali_mcp.parameter_execution import ExperimentalParameterController
controller=ExperimentalParameterController(Service({},sys.argv[1]))
with controller._locked():
    print('locked',flush=True)
    sys.stdin.readline()
"""
        env=dict(os.environ,PYTHONPATH=str(Path(__file__).resolve().parents[1]))
        child=subprocess.Popen([sys.executable,'-c',code,self.tmp.name],stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,env=env)
        try:
            # communicate timeout bounds startup/pipe failures without blocking
            # this test indefinitely. A watchdog kills only this test child.
            watchdog=threading.Timer(10,child.kill);watchdog.start()
            self.assertEqual(child.stdout.readline().strip(),'locked')
            with self.assertRaisesRegex(RuntimeError,'holds the state directory'):self.plan()
            self.assertEqual(self.opens,0)
            child.kill();child.communicate(timeout=5)
        finally:
            watchdog.cancel()
            if child.poll() is None:child.kill()
            child.communicate(timeout=5)
        self.assertEqual(self.plan()['state'],'prepared')

    def test_prepare_and_restore_conflict_before_gateway_and_lock_releases_on_error(self):
        p=self.plan();self.allow(p);self.execute(p)
        other_service=Service(copy.deepcopy(self.service.config),self.tmp.name)
        other=ExperimentalParameterController(other_service);n=self.opens
        with other._locked():
            with self.assertRaisesRegex(RuntimeError,'holds the state directory'):self.plan()
            with self.assertRaisesRegex(RuntimeError,'holds the state directory'):self.controller.prepare_restore(p['plan_id'])
        self.assertEqual(self.opens,n)
        with self.assertRaisesRegex(RuntimeError,'test error'):
            with other._locked():raise RuntimeError('test error')
        self.assertEqual(self.controller.prepare_restore(p['plan_id'])['state'],'prepared')

    def test_claim_prevents_replay_after_plan_rewind(self):
        p=self.plan();self.allow(p);self.execute(p)
        self.edit_plan(p,lambda d:None);self.g.parameters=evidence();n=self.opens
        with self.assertRaises(FileExistsError):self.execute(p)
        self.assertEqual(self.opens,n);self.assertEqual(len(self.g.writes),1)

    def test_claim_survives_failed_preflight_and_plan_publication(self):
        for failure in ('parameter-plan','parameter-run','gateway'):
            p=self.plan();self.allow(p);save=self.service._save
            def fail(kind,*args,**kwargs):
                if kind==failure:raise OSError('simulated storage failure')
                return save(kind,*args,**kwargs)
            with patch.object(self.service,'_save',fail):
                if failure=='gateway':
                    with patch.object(self.service,'_open',side_effect=TimeoutError('offline')):r=self.execute(p)
                else:r=self.execute(p)
            self.assertEqual(r['state'],'preflight_failed');self.assertFalse(self.g.writes)
            n=self.opens
            with self.assertRaises(FileExistsError):self.execute(p)
            self.assertEqual(self.opens,n)

    def test_claim_fsync_failure_stops_before_gateway_and_prevents_replay(self):
        p=self.plan();self.allow(p);n=self.opens
        with patch('dali_mcp.parameter_execution.os.fsync',side_effect=OSError('fsync failed')):
            with self.assertRaisesRegex(OSError,'fsync failed'):self.execute(p)
        with self.assertRaises(FileExistsError):self.execute(p)
        self.assertEqual(self.opens,n);self.assertFalse(self.g.writes)

    def test_restore_validates_claim_execution_and_candidate_prefix_before_open(self):
        p=self.plan([{'field':'fade_time','values':{'code':5}},{'field':'fade_rate','values':{'code':8}}])
        self.allow(p);r=self.execute(p);n=self.opens
        path=Path(self.tmp.name)/f"parameter-run-{r['execution_id']}.json"
        original=path.read_bytes()
        for change in (lambda d:d.update(execution_id='0'*32),
                       lambda d:d['steps'].reverse(),
                       lambda d:d['steps'].append(copy.deepcopy(d['steps'][0])),
                       lambda d:d['steps'][0]['candidate'].update(command='F039')):
            document=json.loads(original);change(document);path.write_text(json.dumps(document),encoding='utf-8')
            with self.assertRaises(ValueError):self.controller.prepare_restore(p['plan_id'])
            self.assertEqual(self.opens,n)
        path.write_bytes(original)
        claim_path=Path(self.tmp.name)/f"parameter-claim-{p['plan_id']}.json"
        original_claim=claim_path.read_bytes()
        for key in ('execution_id','digest','plan_id'):
            claim=json.loads(original_claim);claim[key]='0'*32
            claim_path.write_text(json.dumps(claim),encoding='utf-8')
            with self.assertRaisesRegex(ValueError,'claim'):self.controller.prepare_restore(p['plan_id'])
            self.assertEqual(self.opens,n)
        claim_path.write_bytes(original_claim)
        self.assertEqual(self.controller.prepare_restore(p['plan_id'])['state'],'prepared')

    def test_gate_disabled_wrong_scope_and_malformed_values_before_open(self):
        p=self.plan();n=self.opens
        cases=[{}, {'enable_experimental_parameter_writes':True,'allowed_parameter_write_targets':None},
               {'enable_experimental_parameter_writes':True,'allowed_parameter_write_targets':{'channel':2,'address':1}},
               {'enable_experimental_parameter_writes':True,'allowed_parameter_write_targets':{'channel':2,'address':0,'values':None}},
               {'enable_experimental_parameter_writes':True,'allowed_parameter_write_targets':{'channel':2,'address':0,'values':[{'field':'fade_time','values':{'code':True}}]}}]
        for settings in cases:
            self.service.config.pop('enable_experimental_parameter_writes',None);self.service.config.update(settings)
            with self.assertRaises(PermissionError):self.execute(p)
        self.assertEqual(self.opens,n);self.assertFalse(self.g.writes)

    def test_malformed_preparation_rejected_without_open(self):
        for changes in [[],[{'field':'groups','values':{'code':1}}],[{'field':'fade_time','values':{'code':'5'}}]]:
            with self.assertRaises(ValueError):self.controller.prepare(2,0,changes)
        self.assertEqual(self.opens,0)

    def test_rehashed_payload_or_scope_mismatch_rejected(self):
        for edit in [lambda d:d['proposal']['steps'][0].update(payload_hex='02013f2e05000000'),
                     lambda d:d['proposal']['steps'][0].update(command='F039'),
                     lambda d:d.update(address=63)]:
            p=self.plan();self.allow(p);self.edit_plan(p,edit);n=self.opens
            with self.assertRaises(ValueError):self.execute(p)
            self.assertEqual(self.opens,n)
        self.assertFalse(self.g.writes)

    def test_checksum_expiry_confirmation(self):
        p=self.plan();self.allow(p)
        with self.assertRaisesRegex(ValueError,'confirmation'):self.controller.execute(p['plan_id'],'wrong')
        self.edit_plan(p,lambda d:d.update(expires_at=time.time()-1),rehash=False)
        with self.assertRaisesRegex(ValueError,'checksum'):self.execute(p)
        self.edit_plan(p,lambda d:d.update(expires_at=time.time()-1))
        loaded=self.controller._load(p['plan_id'])
        with self.assertRaisesRegex(ValueError,'expired'):
            self.controller.execute(p['plan_id'],f"APPLY PARAMETERS {p['plan_id']} {loaded['digest'][:12]}")
        self.assertFalse(self.g.writes)

    def test_preflight_changed_configuration_live_or_gateway_stops(self):
        for kind in ('configuration','live','gateway'):
            p=self.plan();self.allow(p)
            original=copy.deepcopy((self.g.parameters,self.g.live,self.info))
            if kind=='configuration':self.g.parameters['min_level']['value']['level_raw']=4
            elif kind=='live':self.g.live['level_raw']=1
            else:self.info['dali_firmware']='different'
            self.assertEqual(self.execute(p)['state'],'preflight_failed');self.assertFalse(self.g.writes)
            self.g.parameters,self.g.live,self.info=original

    def test_lost_receipt_applied_once_readback_then_stop(self):
        p=self.plan([{'field':'fade_time','values':{'code':5}},{'field':'fade_rate','values':{'code':8}}]);self.allow(p)
        self.g.fault='lost';r=self.execute(p)
        self.assertEqual(r['state'],'stopped_receipt_unknown');self.assertEqual(len(self.g.writes),1)
        self.assertEqual(r['steps'][0]['state'],'verified_receipt_unknown');self.assertEqual(self.opens,3)
        self.g.fault=None;restore=self.controller.prepare_restore(p['plan_id']);self.allow(restore);self.execute(restore)
        self.assertEqual(self.g.parameters,evidence());self.assertEqual(len(self.g.writes),2)

    def test_mismatch_sibling_live_read_failure_and_interrupt_never_continue(self):
        states={'ignore':'stopped_verification_failed','sibling':'stopped_verification_failed',
                'live':'stopped_live_output_changed','read_failure':'stopped_unknown','interrupt':'stopped_receipt_unknown'}
        for fault,state in states.items():
            self.g=LabGateway();p=self.plan([{'field':'fade_time','values':{'code':5}},{'field':'fade_rate','values':{'code':8}}]);self.allow(p)
            self.g.fault=fault;r=self.execute(p)
            self.assertEqual(r['state'],state,fault);self.assertEqual(len(self.g.writes),1)

    def test_journal_failure_before_send_prevents_device_write(self):
        for prefix in ('parameter-run','parameter-plan','capability-invalidation'):
            p=self.plan();self.allow(p);save=self.service._save
            def fail(kind,*args,**kwargs):
                if kind==prefix:raise OSError('disk full')
                return save(kind,*args,**kwargs)
            with patch.object(self.service,'_save',fail):r=self.execute(p)
            self.assertEqual(r['state'],'preflight_failed');self.assertFalse(self.g.writes)

    def test_journal_failure_after_send_stops_without_resend(self):
        p=self.plan([{'field':'fade_time','values':{'code':5}},{'field':'fade_rate','values':{'code':8}}]);self.allow(p)
        save=self.service._save
        def fail(kind,*args,**kwargs):
            if self.g.writes:raise OSError('disk full after send')
            return save(kind,*args,**kwargs)
        with patch.object(self.service,'_save',fail):r=self.execute(p)
        self.assertEqual(r['state'],'stopped_unknown');self.assertIn('log_error',r);self.assertEqual(len(self.g.writes),1)

    def test_restore_refuses_unexpected_target_and_unrelated_changes(self):
        p=self.plan();self.allow(p);self.execute(p)
        self.g.parameters['min_level']['value']['level_raw']=4
        with self.assertRaisesRegex(ValueError,'other configuration'):self.controller.prepare_restore(p['plan_id'])
        self.g.parameters['min_level']['value']['level_raw']=3
        self.g.parameters['fade_time_rate']['value'].update(raw=0x67,fade_time_code=6)
        with self.assertRaisesRegex(ValueError,'neither original'):self.controller.prepare_restore(p['plan_id'])
        self.assertEqual(len(self.g.writes),1)

    def test_ct_narrowing_can_restore_original_bounds_only(self):
        p=self.plan([{'field':'configured_min_ct','values':{'kelvin_raw':3000}},
                     {'field':'configured_max_ct','values':{'kelvin_raw':6000}}]);self.allow(p)
        self.assertEqual(self.execute(p)['state'],'completed_readback')
        restore=self.controller.prepare_restore(p['plan_id']);self.allow(restore)
        self.assertEqual(self.execute(restore)['state'],'completed_readback');self.assertEqual(self.g.parameters,evidence())

    def test_paired_power_component_and_both_fade_nibbles_restore(self):
        p=self.plan([{'field':'power_on','values':{'level_raw':30}}, {'field':'fade_time','values':{'code':5}},
                     {'field':'fade_rate','values':{'code':8}}]);self.allow(p)
        self.assertEqual(self.execute(p)['state'],'completed_readback')
        self.assertEqual(self.g.parameters['power_on']['value']['kelvin_raw'],65535)
        restore=self.controller.prepare_restore(p['plan_id']);self.allow(restore)
        self.assertEqual(self.execute(restore)['state'],'completed_readback');self.assertEqual(self.g.parameters,evidence())

    def test_noop_does_not_send_or_invalidate(self):
        p=self.plan([{'field':'fade_time','values':{'code':4}}]);self.allow(p)
        self.assertEqual(self.execute(p)['state'],'completed_readback');self.assertFalse(self.g.writes)
        self.assertFalse(list(Path(self.tmp.name).glob('capability-invalidation-*')))

    def test_receipt_envelope_filters(self):
        candidate=encode_parameter_candidate(2,0,'fade_time',{'code':5})
        def exchange(command,payload,route,accept):
            base=dict(command=command+128,payload=payload,route=route[3:]+route[:3],sequence=self.g.seq,transport=1)
            for change in ({'sequence':0},{'transport':2},{'route':bytes(6)},{'payload':payload[:7]},
                           {'payload':bytes([2,1,63])+payload[3:]}):
                self.assertFalse(accept(SimpleNamespace(**dict(base,**change))))
            reply=SimpleNamespace(**base);self.assertTrue(accept(reply));return reply
        with patch.object(self.g,'exchange',exchange):self.assertFalse(send_candidate_once(self.g,candidate)['status_interpreted'])


class CapabilityInvalidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.config={'gateway_ip':'127.0.0.1','expected_dali_mac':'02:00:00:00:00:01','expected_firmware':'test'}
        self.s=Service(self.config,self.tmp.name)
        self.info={'ip':self.config['gateway_ip'],'dali_mac':self.config['expected_dali_mac'],'dali_firmware':'test'}
        self.stamp=time.time()-20
        for prefix in ('profile','fade','level-limits'):
            self.s._save(prefix,{'gateway':self.info,'captured_at':self.stamp,
                'rows':[{'channel':ch,'address':a} for ch in (1,2) for a in (0,1)]})

    def tearDown(self):self.tmp.cleanup()

    def test_scoped_all_cache_families_and_fresh_refresh(self):
        with patch('dali_mcp.service.time.time',return_value=self.stamp+5):self.s.invalidate_capabilities(2,[0],'test')
        for prefix in ('profile','fade','level-limits'):
            self.assertEqual(set(self.s._cached_profiles(2,prefix)),{1})
            self.assertEqual(set(self.s._cached_profiles(1,prefix)),{0,1})
            self.s._save(prefix,{'gateway':self.info,'captured_at':self.stamp+10,'rows':[{'channel':2,'address':0}]})
            self.assertEqual(set(self.s._cached_profiles(2,prefix)),{0,1})

    def test_foreign_gateway_marker_does_not_invalidate(self):
        self.s._save('capability-invalidation',{'gateway':dict(self.info,ip='127.0.0.2'),
            'invalidated_at':time.time(),'channel':2,'addresses':[0]})
        self.assertEqual(set(self.s._cached_profiles(2)),{0,1})

    def test_malformed_marker_refuses_old_cache(self):
        base={'gateway':self.info,'invalidated_at':time.time(),'channel':2,'addresses':[0]}
        for change in ({'gateway':dict(self.info,dali_mac=1)},{'channel':True},{'addresses':[True]},
                       {'addresses':[64]},{'addresses':[]},{'invalidated_at':float('nan')}):
            self.s._save('capability-invalidation',dict(base,**change),'a'*32)
            self.assertEqual(self.s._cached_profiles(2),{})


if __name__=='__main__':unittest.main()
