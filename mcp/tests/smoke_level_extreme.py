"""Native extreme controls via standard MCP, loopback only."""
import argparse,asyncio,json,sys,tempfile
from pathlib import Path
from mcp import Client
from mcp.client.stdio import StdioServerParameters

async def main():
    ap=argparse.ArgumentParser();ap.add_argument('--output',type=Path,required=True);args=ap.parse_args()
    assert not args.output.exists()
    report=dict(state='running',hardware_io=False,cases=[])
    with tempfile.TemporaryDirectory(prefix='dali-extreme-sdk-') as tmp:
        for drop in (False,True):
            root=Path(tmp)/str(drop);root.mkdir();config=root/'config.json';audit=root/'wire.json'
            config.write_text(json.dumps(dict(gateway_ip='127.0.0.1',local_ip='127.0.0.1',expected_dali_mac='02:00:00:00:00:01',expected_firmware='1.6.9',
                model_audit=str(audit),model_include_extremes=True,model_drop_extreme=drop)),encoding='utf8')
            case=dict(drop_receipt=drop,calls=[]);targets=[(2,63,'minimum',3)] if drop else [(1,63,'maximum',240),(2,0,'minimum',3)]
            expected={f'{ch}:{a}':[0,4000] for ch in (1,2) for a in range(64)}
            async with Client(StdioServerParameters(command=sys.executable,args=[str(Path(__file__).with_name('lamp_scene_fixture.py')),'--config',str(config)])) as client:
                for ch,a,extreme,level in targets:
                    result=await client.call_tool('dali_set_lamp_level_extreme',dict(channel=ch,address=a,extreme=extreme))
                    assert not result.is_error;data=result.structured_content;case['calls'].append(data)
                    assert data['state']==('stopped_receipt_unknown' if drop else 'completed_readback')
                    assert data['target_verified'] and data['actual']['level_raw']==level and data['color_unchanged']
                    assert data['expected_level_raw']==level and not data['storage_modified']
                    assert (await client.call_tool('dali_get_level_extreme',dict(operation_id=data['operation_id']))).structured_content==data
                    expected[f'{ch}:{a}']=[level,4000]
            wire=json.loads(audit.read_text(encoding='utf8'))
            assert wire['live']==expected
            assert wire['extremes']==[bytes([ch,1,a,5 if e=='maximum' else 6,0,0,0,0]).hex() for ch,a,e,_ in targets]
            assert not wire['recalls'] and wire['writes']==wire['live_commands']==wire['scans']==0
            case.update(wire=wire,session_closed=True);report['cases'].append(case)
    report['state']='verified'
    with args.output.open('x',encoding='utf8') as f:json.dump(report,f,ensure_ascii=False,indent=2)
    print('verified: native maximum/minimum, unchanged background, missing receipt without retry')
if __name__=='__main__':asyncio.run(main())
