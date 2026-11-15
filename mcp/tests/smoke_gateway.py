import asyncio,argparse,json,sys,tempfile
from pathlib import Path
from mcp import Client
from mcp.client.stdio import StdioServerParameters

async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    base=Path(__file__).resolve().parent
    with tempfile.TemporaryDirectory(prefix='dali-selection-') as tmp:
        root=Path(tmp);config=root/'config.json'
        config.write_text(json.dumps(dict(gateway_ip='192.168.77.99',local_ip='192.168.77.10',
            expected_dali_mac='02:00:00:00:00:11',expected_firmware='1.6.9',enable_experimental_writes=True)),encoding='utf-8')
        report=dict(state='running',hardware_io=False,calls=[])
        async with Client(StdioServerParameters(command=sys.executable,args=[str(base/'gateway_fixture.py'),'--config',str(config)])) as client:
            async def call(name,**args):
                r=await client.call_tool(name,args);assert not r.is_error,(name,r)
                report['calls'].append(dict(tool=name,data=r.structured_content))
                return r.structured_content
            initial=await call('dali_get_gateway_selection')
            a=await call('dali_read_actual',channel=2,addresses=[0])
            assert a['rows'][0]['model_site']=='Site-A'
            search=await call('dali_search_gateways')
            pending=asyncio.create_task(call('dali_read_actual',channel=2,addresses=[63]))
            await asyncio.sleep(.2)
            busy=await client.call_tool('dali_select_gateway',dict(search_id=search['search_id'],candidate_id='1'))
            assert busy.is_error,'must not switch while live read is active'
            await pending
            unchanged=await call('dali_get_gateway_selection')
            assert unchanged['gateway']['expected_dali_mac']=='02:00:00:00:00:11'
            first=await call('dali_select_gateway',search_id=search['search_id'],candidate_id='0')
            assert first['state_dir']==initial['state_dir']
            status=await call('dali_status');assert not status['writes_enabled']
            second=await call('dali_select_gateway',search_id=search['search_id'],candidate_id='1')
            assert second['state_dir']!=first['state_dir']
            unsupported=await client.call_tool('dali_select_gateway',dict(search_id=search['search_id'],candidate_id='2'))
            assert unsupported.is_error
            after_error=await call('dali_get_gateway_selection')
            assert after_error['gateway']['expected_dali_mac']=='02:00:00:00:00:12'
            b=await call('dali_read_actual',channel=2,addresses=[0])
            assert b['rows'][0]['model_site']=='Site-B'
            again=await call('dali_select_gateway',search_id=search['search_id'],candidate_id='0')
            assert again['state_dir']==first['state_dir']
            assert len(list(Path(first['state_dir']).glob('actual-*.json')))==2
            assert len(list(Path(second['state_dir']).glob('actual-*.json')))==1
            moved=await call('dali_search_gateways')
            changed=await call('dali_select_gateway',search_id=moved['search_id'],candidate_id='0')
            assert changed['gateway']['gateway_ip']=='192.168.77.88' and changed['state_dir']==first['state_dir']
            fresh=await call('dali_read_actual',channel=2,addresses=[0])
            assert fresh['gateway']['ip']=='192.168.77.88'
            report.update(state='verified',two_gateway_isolation=True,reselection_preserves_evidence=True,
                          new_ip_used_by_existing_tools=True,old_write_gates_closed=True,
                          concurrent_switch_rejected=True,unsupported_firmware_preserves_selection=True)
        with args.output.open('x',encoding='utf-8') as f:json.dump(report,f,indent=2)
        print(json.dumps({k:v for k,v in report.items() if k!='calls'},indent=2))

if __name__=='__main__':asyncio.run(main())
