"""Standard MCP readonly diagnostic cases using the loopback-only fixture."""
import argparse,asyncio,json,sys,tempfile
from pathlib import Path
from mcp import Client
from mcp.client.stdio import StdioServerParameters

async def main():
    ap=argparse.ArgumentParser();ap.add_argument('--output',type=Path,required=True);args=ap.parse_args()
    if args.output.exists():raise ValueError('output exists')
    report=dict(state='running',hardware_io=False,cases=[])
    with tempfile.TemporaryDirectory(prefix='dali-diagnostic-sdk-') as tmp:
        root=Path(tmp)
        for dropped in (None,0xf01d):
            folder=root/str(dropped);folder.mkdir();audit=folder/'wire.json';config=folder/'config.json'
            config.write_text(json.dumps(dict(gateway_ip='127.0.0.1',local_ip='127.0.0.1',
                expected_dali_mac='02:00:00:00:00:01',expected_firmware='1.6.9',
                model_parameter_drop=dropped,model_audit=str(audit))),encoding='utf-8')
            async with Client(StdioServerParameters(command=sys.executable,args=[str(Path(__file__).with_name('lamp_scene_fixture.py')),'--config',str(config)])) as client:
                listing=await client.list_tools()
                for name in ('dali_diagnose_lamps','dali_get_diagnostic'):
                    tool=next(x for x in listing.tools if x.name==name)
                    assert tool.annotations.read_only_hint and not tool.annotations.destructive_hint
                r=await client.call_tool('dali_diagnose_lamps',dict(channel=2,addresses=[0,63]));assert not r.is_error
                data=r.structured_content
                assert data['state']==('completed' if dropped is None else 'stopped_partial')
                assert data['completed_addresses']==([0,63] if dropped is None else [])
                assert not data['physical_health_verified'] and not data['device_writes']
                assert data['rows'][0]['parameters']['status']['value']['raw']==0xa5
                if dropped is not None:
                    assert data['pending']==dict(address=0,field='min_level')
                    assert not data['offline_conclusion'] and len(data['rows'])==1
                saved=await client.call_tool('dali_get_diagnostic',dict(operation_id=data['operation_id']))
                assert not saved.is_error and saved.structured_content==data
            wire=json.loads(audit.read_text());assert wire==dict(recalls=[],writes=0,live_commands=0,scans=0)
            report['cases'].append(dict(dropped_query=dropped,report=data,wire=wire,session_closed=True))
    report['state']='verified'
    with args.output.open('x',encoding='utf-8') as f:json.dump(report,f,ensure_ascii=False,indent=2)
    print(json.dumps(dict(state='verified',cases=len(report['cases']))))

if __name__=='__main__':asyncio.run(main())
