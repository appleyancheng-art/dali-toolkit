"""Scene restore execution metadata through MCP and a loopback-only gateway."""
import argparse,asyncio,json,sys,tempfile
from pathlib import Path
from mcp import Client
from mcp.client.stdio import StdioServerParameters

async def main():
    ap=argparse.ArgumentParser();ap.add_argument('--output',type=Path,required=True);args=ap.parse_args()
    assert not args.output.exists()
    report=dict(state='running',hardware_io=False,cases=[])
    with tempfile.TemporaryDirectory(prefix='dali-restore-metadata-') as tmp:
        for already_original in (False,True):
            root=Path(tmp)/str(already_original);root.mkdir();config=root/'config.json';wire=root/'wire.json'
            config.write_text(json.dumps(dict(gateway_ip='127.0.0.1',local_ip='127.0.0.1',
                expected_dali_mac='02:00:00:00:00:01',expected_firmware='1.6.9',model_audit=str(wire),
                enable_experimental_writes=True,allowed_write_targets=dict(channel=2,scene=8,addresses=[0]))),encoding='utf8')
            case=dict(already_original=already_original,calls=[])
            async with Client(StdioServerParameters(command=sys.executable,args=[str(Path(__file__).with_name('lamp_scene_fixture.py')),'--config',str(config)])) as client:
                async def call(name,**params):
                    r=await client.call_tool(name,params);assert not r.is_error,(name,r.content)
                    case['calls'].append(dict(tool=name,params=params,result=r.structured_content))
                    return r.structured_content
                target=dict(channel=2,address=0,scene=8,level_raw=220,kelvin_raw=5000)
                source=await call('dali_save_scene_batch',changes=[target]);assert source['state']=='completed'
                if already_original:
                    restored=await call('dali_save_scene_batch',changes=[dict(target,level_raw=180)])
                    assert restored['state']=='completed'
                preview=await call('dali_preview_scene_restore',plan_id=source['plan_id'])
                assert preview['device_writes'] is False and preview['state']=='previewed'
                result=await call('dali_apply_scene_batch',plan_id=preview['plan_id'],confirmation=preview['confirmation'])
                assert result['state']=='completed'
                assert result['device_writes'] is (not already_original)
                assert result['write_intent_count']==(0 if already_original else 1)
                assert result['preview_metadata']['device_writes'] is False
                assert 'Read-only' not in result['notice']
                assert result['results'][0]['status']==('unchanged' if already_original else 'verified')
                assert (await call('dali_get_scene_plan',plan_id=preview['plan_id']))==result
                actual=await call('dali_read_scenes',channel=2,addresses=[0],scenes=[8])
                assert actual['rows'][0]['level_raw']==180 and actual['rows'][0]['kelvin_raw']==5000
            audit=json.loads(wire.read_text(encoding='utf8'))
            assert audit==dict(recalls=[],writes=2,live_commands=0,scans=0),audit
            case.update(session_closed=True,wire=audit);report['cases'].append(case)
    report['state']='verified'
    with args.output.open('x',encoding='utf8') as f:json.dump(report,f,ensure_ascii=False,indent=2)
    print('verified: changed and already-original restoration through standard MCP')

if __name__=='__main__':asyncio.run(main())
