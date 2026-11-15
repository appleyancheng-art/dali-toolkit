import copy,json,tempfile,threading,time,unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace
from dali_mcp.parameter_batch import ParameterBatchController
from dali_mcp.parameter_execution import ExperimentalParameterController
from dali_mcp.service import Service
from dali_mcp.projects import digest
from dali_mcp.native_workbook_store import NativeWorkbookStore
from test_parameter_control import QuantizedParameterGateway,CASES
from test_native_workbook import document,package


class MultiGateway:
    def __init__(self):
        self.lamps={a:QuantizedParameterGateway() for a in range(64)}
        for g in self.lamps.values():
            g.parameters['min_level']['value']['level_raw']=2;g.parameters['max_level']['value']['level_raw']=254
        self.route=self.lamps[0].route;self.sock=self.lamps[0].sock;self.seq=10
        self.writes=[];self.after_write=None;self.on_read=None;self.closed=0
    def close(self):self.closed+=1
    def read_lamp_parameter(self,c,a,f,t):
        assert c==2
        if self.on_read:self.on_read(a,f)
        return self.lamps[a].read_lamp_parameter(2,0,f,t)
    def read_actual(self,c,a,t):return self.lamps[a].read_actual(2,0,t)
    def exchange(self,command,payload,route,accept):
        a=payload[2];g=self.lamps[a];self.writes.append((a,command,payload))
        def accept_reply(reply):
            self.seq=g.seq
            reply=SimpleNamespace(**vars(reply));reply.payload=reply.payload[:2]+bytes([a])+reply.payload[3:]
            return accept(reply)
        reply=g.exchange(command,payload[:2]+b'\0'+payload[3:],route,accept_reply)
        reply.payload=reply.payload[:2]+bytes([a])+reply.payload[3:]
        if self.after_write:self.after_write(a,command)
        return reply


class ParameterBatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.g=MultiGateway();self.original={a:copy.deepcopy(g.parameters) for a,g in self.g.lamps.items()}
        self.info=dict(ip='127.0.0.1',dali_mac='02:00:00:00:00:01',dali_firmware='test')
        self.s=Service(dict(gateway_ip=self.info['ip'],expected_dali_mac=self.info['dali_mac'],expected_firmware='test'),self.tmp.name)
        self.c=ParameterBatchController(self.s)
        opener=patch.object(self.s,'_open',side_effect=lambda:(self.g,copy.deepcopy(self.info)))
        self.open=opener.start();self.addCleanup(opener.stop)
    def plan(self,addresses=(0,1),changes=None):
        changes=changes or [dict(field='fade_time',values={'code':5}),dict(field='fade_rate',values={'code':6})]
        return self.c.prepare(2,[dict(address=a,changes=copy.deepcopy(changes)) for a in addresses])
    def allow(self,p):
        self.s.config.update(enable_experimental_parameter_writes=True,allowed_parameter_batch_targets=[
            dict(channel=p['channel'],address=e['address'],values=[dict(field=s['field'],values=s['after']) for s in e['proposal']['steps']]) for e in p['entries']])
    def apply(self,p,**kw):return self.c.execute(p['plan_id'],p['confirmation'],**kw)
    def rewrite(self,p,change):
        p=self.c._read('parameter-batch-plan',p['plan_id']);change(p);p['digest']=self.c._digest(p)
        self.s._save('parameter-batch-plan',p,p['plan_id'])
    def assert_original(self):
        self.assertEqual({a:g.parameters for a,g in self.g.lamps.items()},self.original)
        self.assertTrue(all(g.live==dict(light_type_code=1,level_raw=0,kelvin_raw=4000) for g in self.g.lamps.values()))

    def test_two_lamp_all_originals_backed_up_then_serial_roundtrip(self):
        p=self.plan();self.assertEqual(p['total_steps'],4);self.assertFalse(self.g.writes)
        self.allow(p);r=self.apply(p)
        self.assertEqual(r['state'],'completed_readback');self.assertEqual(len(r['fresh_before']),2)
        self.assertEqual([w[0] for w in self.g.writes],[0,0,1,1]);self.assertEqual(self.open.call_count,2)
        undo=self.c.prepare_restore(p['plan_id']);self.allow(undo);self.assertEqual(self.apply(undo)['state'],'completed_readback')
        self.assert_original();self.assertEqual(self.c.prepare_restore(p['plan_id'])['state'],'already_original')
        self.allow(p)
        with self.assertRaises(ValueError):self.apply(p)

    def test_full64_eight_field_scope_plan_and_execution(self):
        changes=[dict(field=f,values={('code' if f.startswith('fade_') else 'kelvin_raw' if f.startswith('configured_') else 'level_raw'):target}) for f,target,*_ in CASES]
        p=self.plan(range(64),changes);self.assertEqual(p['total_steps'],512);self.allow(p)
        # Keep real durable writes; this checks the complete volume, not a sampled plan.
        r=self.apply(p);self.assertEqual(r['state'],'completed_readback');self.assertEqual(r['completed_steps'],512)
        self.assertEqual(len(self.g.writes),512);self.assertEqual({w[0] for w in self.g.writes},set(range(64)))
        undo=self.c.prepare_restore(p['plan_id']);self.assertEqual(undo['total_steps'],512)
        self.allow(undo);self.assertEqual(self.apply(undo)['state'],'completed_readback');self.assert_original()

    def test_preflight_last_lamp_conflict_sends_nothing(self):
        p=self.plan();self.allow(p);self.g.lamps[1].parameters['max_level']['value']['level_raw']=253
        r=self.apply(p);self.assertEqual(r['state'],'preflight_failed');self.assertFalse(self.g.writes)
        self.assertTrue(self.c.get(p['plan_id'])['claimed'])

    def test_nine_fields_including_curve_and_reverse_restore(self):
        changes=[dict(field=f,values={('code' if f.startswith('fade_') else 'kelvin_raw' if f.startswith('configured_') else 'level_raw'):target}) for f,target,*_ in CASES]
        changes.append(dict(field='dimming_curve',values={'raw':1}))
        p=self.plan((0,63),changes);self.assertEqual(p['total_steps'],18);self.allow(p)
        self.assertEqual(self.apply(p)['state'],'completed_readback')
        self.assertEqual([a for a,cmd,b in self.g.writes if cmd==0xf024],[0,63])
        u=self.c.prepare_restore(p['plan_id']);self.allow(u)
        self.assertEqual(self.apply(u)['state'],'completed_readback');self.assert_original()

    def test_mid_batch_conflict_stops_before_second_lamp_and_undo_refuses_outside_change(self):
        p=self.plan();self.allow(p)
        def change(a,cmd):self.g.lamps[1].parameters['max_level']['value']['level_raw']=253
        self.g.after_write=change;r=self.apply(p)
        self.assertEqual(r['state'],'stopped_partial');self.assertEqual([w[0] for w in self.g.writes],[0,0])
        with self.assertRaisesRegex(ValueError,'unrelated'):self.c.prepare_restore(p['plan_id'])
        self.g.lamps[1].parameters['max_level']['value']['level_raw']=254;self.g.after_write=None
        u=self.c.prepare_restore(p['plan_id']);self.assertEqual([e['address'] for e in u['entries']],[0]);self.allow(u);self.apply(u);self.assert_original()

    def test_lost_receipt_readback_stops_and_undo_only_attempted_prefix(self):
        p=self.plan();self.allow(p);self.g.lamps[1].fault='lost'
        r=self.apply(p);self.assertEqual(r['state'],'stopped_receipt_unknown');self.assertEqual(len(self.g.writes),3)
        self.assertEqual(r['steps'][-1]['state'],'verified_receipt_unknown');self.g.lamps[1].fault=None
        u=self.c.prepare_restore(p['plan_id']);self.assertEqual(u['total_steps'],3)
        self.allow(u);self.assertEqual(self.apply(u)['state'],'completed_readback');self.assert_original()

    def test_cancel_before_write_and_after_one_field(self):
        p=self.plan();self.allow(p);r=self.apply(p,cancelled=lambda:True)
        self.assertEqual(r['state'],'cancelled_before_write');self.assertFalse(self.g.writes)
        p=self.plan();self.allow(p);r=self.apply(p,cancelled=lambda:len(self.g.writes)>=1)
        self.assertEqual(r['state'],'cancelled_partial');self.assertEqual(len(self.g.writes),1)
        u=self.c.prepare_restore(p['plan_id']);self.assertEqual(u['total_steps'],1);self.allow(u);self.apply(u);self.assert_original()

    def test_background_get_cancel_is_responsive_while_bus_locked(self):
        p=self.plan();self.allow(p);entered=threading.Event();release=threading.Event()
        def hold(a,cmd):entered.set();self.assertTrue(release.wait(5))
        self.g.after_write=hold;self.c.start(p['plan_id'],p['confirmation'])
        try:
            self.assertTrue(entered.wait(5));self.assertTrue(self.c.get(p['plan_id'])['active_in_this_process'])
            with self.assertRaises(RuntimeError):self.c.start(p['plan_id'],p['confirmation'])
            self.assertIn('cancellation',self.c.cancel(p['plan_id']))
        finally:release.set();self.c.worker.join(5)
        self.assertFalse(self.c.worker.is_alive());r=self.c.get(p['plan_id'])
        self.assertFalse(r['active_in_this_process']);self.assertEqual(r['execution']['state'],'cancelled_partial')
        self.assertEqual(len(self.g.writes),1);self.g.after_write=None
        u=self.c.prepare_restore(p['plan_id']);self.allow(u);self.apply(u);self.assert_original()

    def test_single_address_gate_cannot_enable_batch_and_partial_pair_gate_rejected(self):
        p=self.plan(changes=[dict(field='power_on',values={'level_raw':2})]);before=self.open.call_count
        self.s.config.update(enable_experimental_parameter_writes=True,allowed_parameter_write_targets=dict(channel=2,address=0,values=[]))
        with self.assertRaises(PermissionError):self.apply(p)
        self.allow(p);self.s.config['allowed_parameter_batch_targets'][1]['values'][0]['values'].pop('kelvin_raw')
        with self.assertRaises(PermissionError):self.apply(p)
        self.assertEqual(self.open.call_count,before);self.assertFalse(self.g.writes)
        self.allow(p);self.s.config['allowed_parameter_batch_targets'].append(copy.deepcopy(self.s.config['allowed_parameter_batch_targets'][0]))
        with self.assertRaises(PermissionError):self.apply(p)

    def test_claim_survives_rewind_new_controller_and_failed_journal(self):
        p=self.plan();self.allow(p);raw=self.c._read('parameter-batch-plan',p['plan_id'])
        original_save=self.s._save
        def fail(kind,*args,**kw):
            if kind=='parameter-batch-run':raise OSError('disk failure')
            return original_save(kind,*args,**kw)
        with patch.object(self.s,'_save',fail):r=self.apply(p)
        self.assertEqual(r['state'],'preflight_failed');self.assertFalse(self.g.writes)
        self.s._save('parameter-batch-plan',raw,p['plan_id']);other=ParameterBatchController(self.s)
        with self.assertRaises(FileExistsError):other.execute(p['plan_id'],p['confirmation'])
        self.assertFalse(other.get(p['plan_id'])['active_in_this_process']);self.assertTrue(other.get(p['plan_id'])['claimed'])

    def test_invalid_scope_fade0_255_and_expiry(self):
        for c,t in [(2,[]),(True,[dict(address=0,changes=[])]),(2,[dict(address=True,changes=[])]),
                (2,[dict(address=0,changes=[dict(field='fade_time',values={'code':0})])]),
                (2,[dict(address=0,changes=[dict(field='power_on',values={'level_raw':255})])])]:
            with self.assertRaises(ValueError):self.c.prepare(c,t)
        self.open.assert_not_called();p=self.plan();self.allow(p);self.rewrite(p,lambda d:d.update(expires_at=0))
        with self.assertRaises(ValueError):self.apply(p)

    def test_rehashed_wire_or_restoration_target_tampering_rejected(self):
        p=self.plan();self.rewrite(p,lambda d:d['entries'][0]['proposal']['steps'][0].update(payload_hex='00'*8))
        with self.assertRaises(ValueError):self.c.get(p['plan_id'])
        p=self.plan();self.allow(p);self.apply(p);u=self.c.prepare_restore(p['plan_id'])
        self.rewrite(u,lambda d:d['entries'][0]['changes'][0]['values'].update(code=3))
        with self.assertRaises(ValueError):self.c.get(u['plan_id'])

    def test_live_change_stops_and_recovery_requires_inspection(self):
        p=self.plan();self.allow(p);self.g.lamps[0].fault='live';r=self.apply(p)
        self.assertEqual(r['state'],'stopped_live_output_changed');self.assertEqual(len(self.g.writes),1)
        with self.assertRaisesRegex(ValueError,'live output'):self.c.prepare_restore(p['plan_id'])

    def test_shared_parameter_lock_excludes_other_controller(self):
        other=ExperimentalParameterController(Service(copy.deepcopy(self.s.config),self.tmp.name))
        with other._locked():
            with self.assertRaises(RuntimeError):self.plan()
        self.open.assert_not_called()

    def workbook(self):
        d=document();d['通道二参数']['D2']=5;d['通道二参数']['D3']=5
        path=Path(self.tmp.name)/'source.xlsx';path.write_bytes(package(d))
        return NativeWorkbookStore(self.s).import_file(str(path))['workbook_id']

    def test_native_source_multi_address_and_changed_digest(self):
        ident=self.workbook();spec=dict(kind='workbook',workbook_id=ident,channel=2,addresses=[0,63],fields=['fade_time'],brightness_policy='require_unique')
        p=self.c.prepare_source(spec);self.assertEqual([e['address'] for e in p['entries']],[0,63]);self.allow(p)
        self.assertEqual(self.apply(p)['state'],'completed_readback')
        u=self.c.prepare_restore(p['plan_id']);self.allow(u);self.apply(u);self.assert_original()
        p=self.c.prepare_source(spec);self.allow(p);path=self.s.state_dir/f'native-workbook-{ident}.json'
        record=json.loads(path.read_text(encoding='utf-8'));record['summary']['source_sha256']='0'*64;path.write_text(json.dumps(record),encoding='utf-8')
        n=self.open.call_count
        with self.assertRaises(ValueError):self.apply(p)
        self.assertEqual(self.open.call_count,n)

    def test_project_source_binding_multi_address(self):
        rows=[]
        for a in [0,1]:
            params=copy.deepcopy(self.original[a]);params['fade_time_rate']['value'].update(raw=0x57,fade_time_code=5)
            rows.append(dict(channel=2,address=a,parameters=params,scenes=[dict(channel=2,address=a,scene=8,light_type_code=1,level_raw=254,kelvin_raw=4000)]))
        d=dict(format='dali-toolkit-project',schema_version=1,project_id='a'*32,name='backup',state='completed',atomic_snapshot=False,
            device_writes=False,started_at=1,completed_at=2,gateway=copy.deepcopy(self.info),scope=dict(channel=2,addresses=[0,1],scenes=[8]),rows=rows)
        d['digest']=digest(d);self.s._save('project',d,d['project_id'])
        p=self.c.prepare_source(dict(kind='project',project_id=d['project_id'],channel=2,addresses=[0,1],fields=['fade_time']))
        self.allow(p);self.assertEqual(self.apply(p)['state'],'completed_readback');u=self.c.prepare_restore(p['plan_id']);self.allow(u);self.apply(u);self.assert_original()

    def test_native_blank_skip_and_pair_preservation(self):
        d=document();d['通道二参数'].update(H2='',I2='',H3='',I3=4000)
        path=Path(self.tmp.name)/'blank.xlsx';path.write_bytes(package(d));ident=NativeWorkbookStore(self.s).import_file(str(path))['workbook_id']
        spec=dict(kind='workbook',workbook_id=ident,channel=2,addresses=[0,63],fields=['power_on'],brightness_policy='require_unique')
        p=self.c.prepare_source(spec);self.assertEqual(p['source']['blank_addresses'],[0])
        self.assertEqual(p['entries'][0]['proposal']['steps'][0]['after'],dict(level_raw=0,kelvin_raw=4000))
        self.allow(p);self.assertEqual(self.apply(p)['state'],'completed_readback');u=self.c.prepare_restore(p['plan_id']);self.allow(u);self.apply(u);self.assert_original()
        self.open.reset_mock();spec['addresses']=[0]
        self.assertEqual(self.c.prepare_source(spec)['state'],'no_targets_in_source');self.open.assert_not_called()

    def test_source_changed_during_preflight_stops_before_first_write(self):
        ident=self.workbook();p=self.c.prepare_source(dict(kind='workbook',workbook_id=ident,channel=2,addresses=[0,63],fields=['fade_time'],brightness_policy='require_unique'));self.allow(p)
        def alter(a,f):
            path=self.s.state_dir/f'native-workbook-{ident}.json';r=json.loads(path.read_text(encoding='utf-8'))
            r['summary']['source_sha256']='0'*64;path.write_text(json.dumps(r),encoding='utf-8');self.g.on_read=None
        self.g.on_read=alter;r=self.apply(p)
        self.assertEqual(r['state'],'preflight_failed');self.assertFalse(self.g.writes)

    def test_claim_fsync_failure_and_unknown_dispatch_not_replayed(self):
        p=self.plan();self.allow(p)
        with patch('dali_mcp.parameter_batch.os.fsync',side_effect=OSError('fsync failed')):
            with self.assertRaises(OSError):self.apply(p)
        with self.assertRaises(FileExistsError):self.apply(p)
        self.assertFalse(self.g.writes)
        p=self.plan();self.allow(p);self.g.lamps[0].fault='interrupt'
        r=self.apply(p);self.assertEqual(r['state'],'stopped_receipt_unknown');self.assertEqual(len(self.g.writes),1)
        other=ParameterBatchController(self.s)
        with self.assertRaises(ValueError):other.start(p['plan_id'],p['confirmation'])
        self.assertFalse(other.get(p['plan_id'])['active_in_this_process'])


if __name__=='__main__':unittest.main()
