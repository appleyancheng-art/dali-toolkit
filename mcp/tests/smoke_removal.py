"""Three SDK processes: prepare, remove, inspect/reject replay. Model only."""
import argparse,asyncio,copy,json,sys,tempfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tests'))
INFO=dict(ip='127.0.0.1',dali_mac='02:00:00:00:00:01',dali_firmware='1.6.9')

def server(config):
    from dali_mcp.service import Service
    from dali_mcp import address_moves
    from dali_mcp.__main__ import main
    from test_address_removal import RemovalLab
    state=config.parent/'model.json';g=RemovalLab()
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
    from test_address_removal import RemovalLab
    ap=argparse.ArgumentParser();ap.add_argument('--output',type=Path,required=True);args=ap.parse_args()
    if args.output.exists():raise ValueError('output exists')
    with tempfile.TemporaryDirectory(prefix='dali-removal-sdk-') as directory:
        root=Path(directory);model=root/'model.json';initial=json.loads(json.dumps(dict(lamps=RemovalLab().lamps,writes=[])))
        model.write_text(json.dumps(initial),encoding='utf-8')
        cfg=dict(gateway_ip=INFO['ip'],local_ip='127.0.0.1',expected_dali_mac=INFO['dali_mac'],expected_firmware=INFO['dali_firmware'])
        config=root/'config.json';config.write_text(json.dumps(cfg),encoding='utf-8')
        params=StdioServerParameters(command=sys.executable,args=[str(Path(__file__).resolve()),'--server',str(config)])
        calls=[]
        async def call(client,name,**kw):
            r=await client.call_tool(name,kw,read_timeout_seconds=90)
            assert not r.is_error,(name,r.content)
            calls.append(dict(tool=name,result=r.structured_content));return r.structured_content
        async with Client(params) as c:
            assert len((await c.list_tools()).tools)==129
            p=await call(c,'dali_prepare_address_removal',channel=2,address=0)
            assert not (await call(c,'dali_get_address_removal',plan_id=p['plan_id']))['claimed']
        assert json.loads(model.read_text())==initial
        cfg.update(enable_experimental_address_removal=True,allowed_address_removal_digest=p['digest'],allowed_address_removals=[dict(channel=2,address=0)])
        config.write_text(json.dumps(cfg),encoding='utf-8')
        async with Client(params) as c:
            result=await call(c,'dali_apply_address_removal',plan_id=p['plan_id'],confirmation=p['confirmation'])
            assert result['state']=='completed_address_removal_readback'
        expected=copy.deepcopy(initial);del expected['lamps']['0'];expected['writes']=[[2,0,255]]
        assert json.loads(model.read_text())==expected
        async with Client(params) as c:
            got=await call(c,'dali_get_address_removal',plan_id=p['plan_id'])
            assert got['claimed'] and got['execution']==result
            assert (await c.call_tool('dali_apply_address_removal',dict(plan_id=p['plan_id'],confirmation=p['confirmation']))).is_error
        assert json.loads(model.read_text())==expected
        report=dict(state='verified',tools=129,sdk_sessions=3,hardware_io=False,prepare_preserves_model=True,
                    removed_only_selected_address=True,remaining_model_exactly_preserved=True,restart_replay_rejected=True,calls=calls)
        args.output.parent.mkdir(parents=True,exist_ok=True)
        with args.output.open('x',encoding='utf-8') as f:json.dump(report,f,ensure_ascii=False,indent=2)
        print(json.dumps({k:v for k,v in report.items() if k!='calls'}))

if __name__=='__main__':
    if len(sys.argv)==3 and sys.argv[1]=='--server':server(Path(sys.argv[2]))
    else:asyncio.run(verify())
