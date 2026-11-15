"""Search, optionally connect by exact MAC, then export a reusable MCP config.

Search and selection share one live SDK session. No lamp commands are exposed.
"""
import argparse
import asyncio
import ipaddress
import json
import re
import sys
import time
from pathlib import Path
from mcp import Client
from mcp.client.stdio import StdioServerParameters
from client import concise_error


def arguments(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--server',type=Path,default=Path(__file__).with_name('run_server.py'))
    parser.add_argument('--config',type=Path,default=Path('dali-connection/config.json'))
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--connect-mac',type=str)
    parser.add_argument('--local-ip',action='append',default=None)
    parser.add_argument('--window',type=float,default=2.0)
    args=parser.parse_args(argv)
    if args.output.exists():parser.error('output exists; preserve earlier records and choose another path')
    if not args.server.is_file():parser.error('server file is missing')
    if not .2<=args.window<=10:parser.error('window must be0.2..10 seconds per interface')
    if args.connect_mac:
        if not re.fullmatch(r'(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}',args.connect_mac):parser.error('MAC must contain six colon-separated hex bytes')
        args.connect_mac=args.connect_mac.lower()
    if args.local_ip:
        try:
            for ip in args.local_ip:ipaddress.IPv4Address(ip)
        except ValueError:parser.error('local-ip must be a local IPv4 address')
        if len(set(args.local_ip))!=len(args.local_ip):parser.error('duplicate local-ip')
    return args


async def run(args):
    report=dict(state='starting',started_at=time.time(),calls=[],lighting_commands_sent=False,
                session_opened=False,session_closed=False,automatic_retry=False)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    with args.output.open('x',encoding='utf-8') as f:json.dump(report,f)
    def save():
        tmp=args.output.with_suffix(args.output.suffix+'.tmp')
        tmp.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8');tmp.replace(args.output)
    exit_code=1
    try:
        async with Client(StdioServerParameters(command=sys.executable,args=[str(args.server),'--config',str(args.config)])) as client:
            report['session_opened']=True;save()
            async def call(tool,params):
                report['pending']=dict(tool=tool,arguments=params);save()
                result=await client.call_tool(tool,params,read_timeout_seconds=180)
                report['calls'].append(dict(tool=tool,result=result.model_dump(mode='json',by_alias=True)))
                report.pop('pending',None);save()
                if result.is_error:raise RuntimeError('; '.join(getattr(c,'text','') for c in result.content))
                if not result.structured_content:raise RuntimeError('missing structured gateway response')
                return result.structured_content
            search=await call('dali_search_gateways',dict(local_ips=args.local_ip,timeout_seconds=args.window))
            report['search']=search
            if search['state']=='failed':raise RuntimeError('all selected network-interface searches failed')
            if args.connect_mac:
                rows=[r for r in search['gateways'] if r['dali_mac'].lower()==args.connect_mac]
                if len(rows)!=1:raise RuntimeError('MAC was not uniquely discovered; inspect results or specify one --local-ip')
                selected=await call('dali_select_gateway',dict(search_id=search['search_id'],candidate_id=rows[0]['candidate_id']))
                if selected.get('state')!='selected':raise RuntimeError('gateway selection was not confirmed')
                report.update(state='selected',selection=selected,config_path=selected['config_path'])
                exit_code=0
            else:
                report['state']='searched' if search['state']=='completed' else 'partial_search'
                exit_code=0 if search['state']=='completed' else 2
        report['session_closed']=True
    except Exception as exc:
        report.update(state='failed',error=concise_error(exc))
    finally:
        report['completed_at']=time.time();save()
    print(json.dumps({k:report[k] for k in ('state','config_path','error','session_closed') if k in report},ensure_ascii=False))
    return exit_code


if __name__=='__main__':sys.exit(asyncio.run(run(arguments())))
