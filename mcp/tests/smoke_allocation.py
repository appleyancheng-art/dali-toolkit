"""Three real SDK sessions, persistent simulated gear; no hardware traffic."""
import argparse,asyncio,copy,json,sys,tempfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tests'))
INFO=dict(ip='127.0.0.1',dali_mac='02:00:00:00:00:01',dali_firmware='1.6.9')


def server(config):
    from dali_mcp.service import Service
    from dali_mcp import address_moves
    from dali_mcp.__main__ import main
    from test_address_allocation import AllocationLab
    state=config.parent/'model.json';g=AllocationLab()
    saved=json.loads(state.read_text(encoding='utf-8'))
    g.lamps={int(k):v for k,v in saved['lamps'].items()};g.writes=[tuple(v) for v in saved['writes']]
    def close():state.write_text(json.dumps(dict(lamps=g.lamps,writes=g.writes)),encoding='utf-8')
    g.close=close
    Service._open=lambda service:(g,copy.deepcopy(INFO))
    address_moves.require_configmaster_closed=lambda:None
    sys.argv=[str(ROOT/'run_server.py'),'--config',str(config)];main()


async def verify():
    from mcp import Client
    from mcp.client.stdio import StdioServerParameters
    from test_address_allocation import AllocationLab
    ap=argparse.ArgumentParser();ap.add_argument('--output',type=Path,required=True);args=ap.parse_args()
    if args.output.exists():raise ValueError('output exists')
    with tempfile.TemporaryDirectory(prefix='dali-allocation-sdk-') as directory:
        root=Path(directory);model=root/'model.json';initial=json.loads(json.dumps(dict(lamps=AllocationLab().lamps,writes=[])))
        model.write_text(json.dumps(initial),encoding='utf-8')
        cfg=dict(gateway_ip=INFO['ip'],local_ip='127.0.0.1',expected_dali_mac=INFO['dali_mac'],expected_firmware=INFO['dali_firmware'])
        config=root/'config.json';config.write_text(json.dumps(cfg),encoding='utf-8')
        params=StdioServerParameters(command=sys.executable,args=[str(Path(__file__).resolve()),'--server',str(config)])
        calls=[]
        async def call(client,name,**kw):
            r=await client.call_tool(name,kw,read_timeout_seconds=90)
            assert not r.is_error,(name,r.content)
            calls.append(dict(tool=name,result=r.structured_content));return r.structured_content
        async def wait(client,result):
            for _ in range(120):
                if not result['active_in_this_process']:return result
                await asyncio.sleep(.1)
                result=await call(client,'dali_get_address_allocation',operation_id=result['operation_id'])
            raise TimeoutError('existing allocation worker did not finish')
        async with Client(params) as c:
            assert len((await c.list_tools()).tools)==129
            reviewed=await wait(c,await call(c,'dali_start_address_allocation_review',channel=2,expected_new_count=1))
            assert reviewed['state']=='reviewed' and not reviewed['claimed']
        assert json.loads(model.read_text())==initial
        ident=reviewed['operation_id'];confirmation=reviewed['confirmation']
        cfg.update(enable_experimental_address_allocation=True,allowed_address_allocation_digest=reviewed['plan']['digest'],allowed_address_allocation_channels=[2])
        config.write_text(json.dumps(cfg),encoding='utf-8')
        async with Client(params) as c:
            result=await wait(c,await call(c,'dali_start_address_allocation',operation_id=ident,confirmation=confirmation))
            assert result['state']=='completed_readback'
            assert result['execution']['new_addresses']==[2]
        expected=copy.deepcopy(initial);expected['lamps']['2']=AllocationLab.lamp(2);expected['writes']=[[2,2]]
        assert json.loads(model.read_text())==expected
        async with Client(params) as c:
            got=await call(c,'dali_get_address_allocation',operation_id=ident)
            assert got['claimed'] and got['execution']==result['execution'] and not got['active_in_this_process']
            assert (await c.call_tool('dali_start_address_allocation',dict(operation_id=ident,confirmation=confirmation))).is_error
        assert json.loads(model.read_text())==expected
        report=dict(state='verified',tools=129,sdk_sessions=3,hardware_io=False,review_preserves_model=True,
                    old_lamps_exactly_preserved=True,one_incremental_allocation=True,restart_replay_rejected=True,calls=calls)
        args.output.parent.mkdir(parents=True,exist_ok=True)
        with args.output.open('x',encoding='utf-8') as f:json.dump(report,f,ensure_ascii=False,indent=2)
        print(json.dumps({k:v for k,v in report.items() if k!='calls'}))


if __name__=='__main__':
    if len(sys.argv)==3 and sys.argv[1]=='--server':server(Path(sys.argv[2]))
    else:asyncio.run(verify())
