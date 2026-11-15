"""Exercise single-address recall through the standard MCP SDK on loopback."""
import argparse,asyncio,json,sys,tempfile
from pathlib import Path
from mcp import Client
from mcp.client.stdio import StdioServerParameters

async def main():
    ap=argparse.ArgumentParser();ap.add_argument('--output',type=Path,required=True);args=ap.parse_args()
    if args.output.exists():raise ValueError('output exists')
    report=dict(state='running',hardware_io=False,cases=[])
    with tempfile.TemporaryDirectory(prefix='dali-lamp-sdk-') as tmp:
        root=Path(tmp)
        for receipt in ('captured','drop'):
            folder=root/receipt;folder.mkdir();config=folder/'config.json';audit=folder/'wire.json'
            config.write_text(json.dumps(dict(gateway_ip='127.0.0.1',local_ip='127.0.0.1',
                expected_dali_mac='02:00:00:00:00:01',expected_firmware='1.6.9',
                model_receipt=receipt,model_audit=str(audit))),encoding='utf-8')
            case=dict(receipt=receipt,calls=[])
            async with Client(StdioServerParameters(command=sys.executable,args=[str(Path(__file__).with_name('lamp_scene_fixture.py')),'--config',str(config)])) as client:
                async def call(tool,**params):
                    r=await client.call_tool(tool,params)
                    case['calls'].append(dict(tool=tool,arguments=params,data=r.structured_content,is_error=r.is_error))
                    assert not r.is_error,(tool,r.content)
                    return r.structured_content
                listing=await client.list_tools()
                recall=next(t for t in listing.tools if t.name=='dali_recall_lamp_scene')
                getter=next(t for t in listing.tools if t.name=='dali_get_lamp_recall')
                assert not recall.annotations.read_only_hint and not recall.annotations.idempotent_hint
                assert getter.annotations.read_only_hint
                for params in (dict(channel=True,address=0,scene=8),dict(channel=2,address='0',scene=8),
                               dict(channel=2,address=64,scene=8),dict(channel=2,address=0,scene=16)):
                    assert (await client.call_tool('dali_recall_lamp_scene',params)).is_error
                before={ch:(await call('dali_read_actual',channel=ch,addresses=list(range(64))))['rows'] for ch in (1,2)}
                outcome=await call('dali_recall_lamp_scene',channel=2,address=0,scene=8)
                assert outcome['samples_verified'] and not outcome['scene_storage_modified']
                assert outcome['state']==('samples_verified' if receipt=='captured' else 'stopped_receipt_unknown')
                assert (await call('dali_get_lamp_recall',operation_id=outcome['operation_id']))==outcome
                after={ch:(await call('dali_read_actual',channel=ch,addresses=list(range(64))))['rows'] for ch in (1,2)}
                for ch in (1,2):
                    old={r['address']:(r['level_raw'],r['kelvin_raw']) for r in before[ch]}
                    new={r['address']:(r['level_raw'],r['kelvin_raw']) for r in after[ch]}
                    expected=dict(old)
                    if ch==2:expected[0]=(180,5000)
                    assert new==expected
            wire=json.loads(audit.read_text());assert wire==dict(recalls=['0201001008000000'],writes=0,live_commands=0,scans=0)
            case.update(state='verified',session_closed=True,wire=wire);report['cases'].append(case)
    report['state']='verified'
    with args.output.open('x',encoding='utf-8') as f:json.dump(report,f,ensure_ascii=False,indent=2)
    print(json.dumps(dict(state=report['state'],cases=len(report['cases']))))

if __name__=='__main__':asyncio.run(main())
