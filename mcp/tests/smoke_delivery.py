"""Three real SDK/server sessions; simulated bus, real isolated native files.

No gateway packets, no AppData or actual application process interaction.
The model persists on close, so later sessions must recover actual model changes.
"""
import argparse,asyncio,copy,json,os,sys,tempfile,time
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tests'))
from dali_mcp.native_cache import canonical
from dali_mcp.projects import digest
from test_native_cache import document
from test_project_floorplan import cache_fixture,png
from test_project_execution import CombinedGateway

INFO=dict(ip='127.0.0.1',dali_mac='02:00:00:00:00:01',dali_firmware='1.6.9')

def snapshot(g):
    return dict(parameters={str(a):v.parameters for a,v in g.lamps.items()},
                scenes=[v for _,v in sorted(g.scenes.items())])

def server(config,slow_clock=False):
    from dali_mcp.service import Service
    from dali_mcp import native_file
    from dali_mcp.__main__ import main
    state=config.parent/'simulated-device.json';g=CombinedGateway()
    if state.exists():
        saved=json.loads(state.read_text(encoding='utf-8'))
        for a,v in saved['parameters'].items():g.lamps[int(a)].parameters=v
        g.scenes={(v['address'],v['scene']):v for v in saved['scenes']}
    def close():state.write_bytes(canonical(snapshot(g)))
    g.close=close
    Service._open=lambda service:(g,copy.deepcopy(INFO))
    native_file.require_configmaster_closed=lambda:None
    if os.name!='nt':native_file.replace_native_file=os.replace
    if slow_clock:
        # Test-server-only clock; persist elapsed model time across stdio sessions.
        # No sleeps and no network/device implementation is replaced in production.
        from dali_mcp.project_delivery import ProjectDeliveryController
        from dali_mcp.project_execution import ProjectRestoreController
        from unittest.mock import patch
        clock_file=config.parent/'simulated-clock.json';real_time=time.time
        clock=json.loads(clock_file.read_text()) if clock_file.exists() else dict(offset=0,events=[])
        def advance(label):
            clock['offset']+=601;clock['events'].append(label)
            clock_file.write_bytes(canonical(clock))
        def delayed(method,label):
            def wrapped(*a,**kw):advance(label);return method(*a,**kw)
            return wrapped
        prepare=ProjectRestoreController.prepare;undo=ProjectRestoreController.prepare_restore
        preflight=ProjectDeliveryController._preflight;capture=ProjectRestoreController._capture
        pending=[False]
        def undo_preflight(controller,p):
            pending[0]=p['restore_of'] is not None
            try:return preflight(controller,p)
            finally:pending[0]=False
        def timed_capture(*a,**kw):
            if pending[0]:pending[0]=False;advance('undo_preflight')
            return capture(*a,**kw)
        def after_event(category,address):
            if len(g.events)==1:advance('device_execution')
        g.after_event=after_event
        ProjectRestoreController.prepare=delayed(prepare,'forward_preparation')
        ProjectRestoreController.prepare_restore=delayed(undo,'undo_preparation')
        ProjectDeliveryController._preflight=undo_preflight
        ProjectRestoreController._capture=timed_capture
        with patch('time.time',side_effect=lambda:real_time()+clock['offset']):
            sys.argv=[str(ROOT/'run_server.py'),'--config',str(config)];main()
        return
    sys.argv=[str(ROOT/'run_server.py'),'--config',str(config)];main()

async def verify():
    from mcp import Client
    from mcp.client.stdio import StdioServerParameters
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--slow-clock',action='store_true');args=parser.parse_args()
    if args.output.exists():raise ValueError('output exists; preserve prior evidence')
    with tempfile.TemporaryDirectory(prefix='dali-delivery-sdk-') as directory:
        root=Path(directory);cache_file=root/'gateways'/'cache.json';cache_file.parent.mkdir()
        original=canonical(document());cache_file.write_bytes(original);image=root/'source.png';image.write_bytes(png())
        cfg=dict(gateway_ip=INFO['ip'],local_ip='127.0.0.1',expected_dali_mac=INFO['dali_mac'],expected_firmware=INFO['dali_firmware'],
                 configmaster_cache_file=str(cache_file))
        config=root/'config.json';config.write_bytes(canonical(cfg))
        params=StdioServerParameters(command=sys.executable,args=[str(Path(__file__).resolve()),'--server',str(config)]+(['--slow-clock'] if args.slow_clock else []))
        calls=[]
        async def call(client,name,**kw):
            reply=await client.call_tool(name,kw,read_timeout_seconds=90)
            if reply.is_error:raise AssertionError((name,reply.content))
            data=reply.structured_content;calls.append(dict(tool=name,state=data.get('state',data.get('execution',{}).get('state'))))
            return data
        async def finish(client,p):
            status=await call(client,'dali_start_project_delivery',plan_id=p['plan_id'],confirmation=p['confirmation'])
            while status['active_in_this_process']:
                await asyncio.sleep(.1)
                status=await call(client,'dali_get_project_delivery',plan_id=p['plan_id'])
            assert status['execution']['state']=='completed_readback',status
            assert not any(key in status for key in ('worker-error','worker_error_in_this_process','execution_unavailable')),status
            assert 'journal_error' not in status['execution'],status
            assert status['execution']['completed_phases']==2
            return status
        def authorize(status):
            p=status['plan'];child=next(x['status']['plan'] for x in status['children'] if x['category']=='device')
            cfg.update(enable_project_delivery=True,allowed_project_delivery_digest=p['digest'],enable_project_restore=True,
                       allowed_project_restore_digest=child['digest'],enable_experimental_parameter_writes=True,
                       enable_experimental_group_writes=True,enable_experimental_writes=True,
                       enable_native_cache_writes=True,enable_native_floorplan_writes=True)
            cfg['allowed_parameter_batch_targets']=[dict(channel=2,address=a,values=[dict(field=s['field'],values=s['after']) for s in child['steps'] if s['category']=='parameter' and s['address']==a]) for a in (0,63)]
            cfg['allowed_group_write_operations']=[dict(channel=2,address=s['address'],group=s['group'],member=s['after']) for s in child['steps'] if s['category']=='group']
            cfg['allowed_write_targets']=[dict(channel=2,scene=15,addresses=[0,63])]
            config.write_bytes(canonical(cfg))
        initial=CombinedGateway();original_device=copy.deepcopy(snapshot(initial))
        doc=dict(format='dali-toolkit-project',schema_version=1,project_id='a'*32,name='SDK delivery',state='completed',
                 atomic_snapshot=False,device_writes=False,started_at=1,completed_at=2,gateway=INFO,
                 scope=dict(channel=2,addresses=[0,63],scenes=[15]),rows=[])
        for a in (0,63):
            par=copy.deepcopy(initial.lamps[a].parameters);par['fade_time_rate']['value'].update(raw=0x57,fade_time_code=5)
            par['groups']['value']=dict(groups=[0,1,15],group_mask=32771)
            doc['rows'].append(dict(channel=2,address=a,parameters=par,scenes=[dict(channel=2,address=a,scene=15,light_type_code=1,level_raw=254,kelvin_raw=4000)]))
        doc['digest']=digest(doc);local=cache_fixture();local['zCh2']['addresses']['0']['name']='SDK工程灯'
        names=[dict(kind='lamp',address=0)]
        async with Client(params) as client:
            listing=await client.list_tools()
            required={'dali_import_project','dali_import_configmaster_cache','dali_attach_project_floorplan',
                      'dali_attach_project_names','dali_prepare_project_delivery','dali_get_project_delivery',
                      'dali_start_project_delivery','dali_prepare_project_delivery_undo'}
            assert required<={tool.name for tool in listing.tools}
            assert all('_deadline_check' not in json.dumps(tool.input_schema) for tool in listing.tools)
            imported=await call(client,'dali_import_project',document=doc)
            cached=await call(client,'dali_import_configmaster_cache',document=local)
            floor=await call(client,'dali_attach_project_floorplan',project_id=imported['project_id'],cache_id=cached['cache_id'],image_path=str(image))
            named=await call(client,'dali_attach_project_names',project_id=floor['project_id'],cache_id=cached['cache_id'],selections=names)
            p=await call(client,'dali_prepare_project_delivery',project_id=named['project_id'],addresses=[0,63],fields=['fade_time'],group_numbers=[15],scenes=[15],name_selections=names,mark_scope='captured_marks')
            status=await call(client,'dali_get_project_delivery',plan_id=p['plan_id'])
        assert cache_file.read_bytes()==original
        assert json.loads((root/'simulated-device.json').read_text(encoding='utf-8'))==original_device
        authorize(status)
        async with Client(params) as client:
            forward=await finish(client,p)
            assert json.loads(cache_file.read_text(encoding='utf-8'))['zCh2']['addresses']['0']['name']=='SDK工程灯'
            u=await call(client,'dali_prepare_project_delivery_undo',plan_id=p['plan_id'])
            status=await call(client,'dali_get_project_delivery',plan_id=u['plan_id'])
        authorize(status)
        async with Client(params) as client:reverse=await finish(client,u)
        assert cache_file.read_bytes()==original
        assert json.loads((root/'simulated-device.json').read_text(encoding='utf-8'))==original_device
        report=dict(state='verified',tools=len(listing.tools),sdk_sessions=3,device_model=True,hardware_io=False,actual_app_file_written=False,
                    native_temporary_file_exactly_restored=True,device_model_exactly_restored=True,
                    forward_phases=forward['execution']['completed_phases'],undo_phases=reverse['execution']['completed_phases'],calls=calls)
        if args.slow_clock:
            clock=json.loads((root/'simulated-clock.json').read_text())
            assert clock['events']==['forward_preparation','device_execution','undo_preparation','undo_preflight','device_execution'],clock
            for status in (forward,reverse):
                run=status['execution'];plan=status['plan']
                assert plan['created_at']<=run['admitted_at']<=plan['expires_at']<run['finished_at'],status
            assert reverse['execution']['started_at']>reverse['plan']['expires_at']
            report['simulated_elapsed']=clock
            report['admitted_before_deadline_completed_after_deadline']=True
        args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_bytes(canonical(report))
        print(json.dumps({k:v for k,v in report.items() if k!='calls'}))

if __name__=='__main__':
    if len(sys.argv) in (3,4) and sys.argv[1]=='--server':server(Path(sys.argv[2]),'--slow-clock' in sys.argv[3:])
    else:asyncio.run(verify())
