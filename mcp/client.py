"""Reusable MCP stdio client with a one-attempt journal and save-job progress.

Arguments are a JSON file, avoiding shell interpolation of device parameters.
This client does not enable storage writes or retry failed physical commands.
"""
import argparse
import asyncio
import json
import sys
import time
import traceback
from pathlib import Path
from mcp import Client
from mcp.client.stdio import StdioServerParameters

TERMINAL={'completed','stopped_partial','failed','interrupted','cancelled_partial','cancelled_before_write'}
UNSUCCESSFUL=(TERMINAL-{'completed'})|{'scan_unknown','outcome_unknown','receipt_received_unverified','preflight_failed','verification_failed','stopped_restored','restore_unverified','scope_changed','stopped_receipt_unknown','stopped_conflict','stopped_verification_failed','stopped_final_conflict','stopped_unknown','conflict','reviewed_with_rejections','restore_conflict','stopped_live_output_changed'}
ADDRESS_TOOLS={'dali_apply_address_move','dali_get_address_move','dali_apply_address_removal','dali_get_address_removal'}
UNSUCCESSFUL.add('connection_failed')
UNSUCCESSFUL.update({'outcome_uncertain','unexpected_output'})
ADDRESS_COMPLETED={'completed_readback','completed_address_removal_readback'}
ALLOCATION_STARTS={'dali_start_address_allocation_review','dali_start_address_allocation'}
ALLOCATION_TOOLS=ALLOCATION_STARTS|{'dali_get_address_allocation','dali_cancel_address_allocation'}
DELIVERY_TOOLS={'dali_start_project_delivery','dali_get_project_delivery','dali_cancel_project_delivery'}


def allocation_unsuccessful(tool,data):
    if data.get('worker-error') or data.get('execution_record_missing') or data.get('execution',{}).get('log_error'):
        return True
    if data.get('active_in_this_process'):return True
    expected={'reviewed'} if tool=='dali_start_address_allocation_review' else {'completed_readback'}
    if tool=='dali_get_address_allocation' and not data.get('claimed'):expected={'reviewed'}
    return data.get('state') not in expected


def address_outcome(tool,data):
    """Separate a successful record query from the recorded device outcome."""
    run=data.get('execution')
    if isinstance(run,dict):return run
    if data.get('claimed') or data.get('execution_record_missing'):
        return {'state':'execution_unavailable'}
    return data


def address_unsuccessful(tool,data):
    outcome=address_outcome(tool,data)
    if outcome.get('log_error'):return True
    if tool.startswith('dali_get_') and not data.get('claimed') and outcome.get('state')=='prepared':
        return False  # A prepared plan was queried, not executed.
    return outcome.get('state') not in ADDRESS_COMPLETED


def tool_error_text(content):
    texts=[item if isinstance(item,str) else getattr(item,'text',None) for item in content]
    return '\n'.join(text for text in texts if isinstance(text,str) and text) or '工具返回错误，详情见输出记录'


def concise_error(exc):
    children=getattr(exc,'exceptions',None)
    if children:
        return '; '.join(dict.fromkeys(concise_error(child) for child in children))[:2000]
    return ' '.join((str(exc) or type(exc).__name__).split())[:2000]


def result_summary(tool,data):
    summary={'tool':tool,'state':data.get('state','returned')}
    if tool in ('dali_set_lamp_level_extreme','dali_get_level_extreme'):
        summary.update({k:data[k] for k in ('extreme','expected_level_raw','target_verified','color_unchanged','output_changed','command_attempted') if k in data})
    if tool in ('dali_diagnose_lamps','dali_get_diagnostic'):
        summary.update({k:data[k] for k in ('finding_count','completed_addresses','uncompleted_addresses','pending','physical_health_verified') if k in data})
    if tool in ALLOCATION_TOOLS:
        record=data.get('execution') or data.get('review',{})
        summary.update(operation_id=data.get('operation_id'),active=data.get('active_in_this_process'),
                       claimed=data.get('claimed'),progress=record.get('progress'),pending=record.get('pending'),
                       new_addresses=record.get('new_addresses'),write_intent=record.get('write_intent',False),
                       worker_error=data.get('worker-error',{}).get('error'),log_error=record.get('log_error'))
    if tool in ADDRESS_TOOLS:
        outcome=address_outcome(tool,data)
        summary.update(state=outcome.get('state','execution_unavailable'),plan_id=data.get('plan_id'),
                       write_intent=outcome.get('write_intent',False))
        if 'claimed' in data:summary['claimed']=data['claimed']
        if outcome.get('log_error'):summary['log_error']=outcome['log_error']
    if tool in ('dali_start_parameter_batch','dali_get_parameter_batch','dali_cancel_parameter_batch','dali_start_project_restore','dali_get_project_restore','dali_cancel_project_restore','dali_start_project_delivery','dali_get_project_delivery','dali_cancel_project_delivery'):
        run=data.get('execution',{})
        summary.update(state=run.get('state','execution_unavailable'),plan_id=data.get('plan',{}).get('plan_id'),
            active_in_this_process=data.get('active_in_this_process'),completed_steps=run.get('completed_steps'),total_steps=run.get('total_steps'))
        if tool.endswith('_project_delivery'):
            summary.update(completed_phases=run.get('completed_phases'),total_phases=run.get('total_phases'),pending=run.get('pending'))
            failure=data.get('worker_error_in_this_process') or data.get('worker-error')
            if failure:summary['worker_error']=failure
            if run.get('journal_error'):summary['journal_error']=run['journal_error']
            if data.get('execution_unavailable'):summary['execution_unavailable']=data['execution_unavailable']
    summary.update({k:data[k] for k in ('version','receipt_status','samples_verified','elapsed_seconds',
                                      'job_id','operation_id','connectivity','journal_error') if k in data})
    for key in ('rows','layouts','results'):
        if isinstance(data.get(key),list):summary[key+'_count']=len(data[key])
    if isinstance(data.get('results'),list):
        counts={}
        for row in data['results']:
            status=row.get('status','unspecified') if isinstance(row,dict) else 'unspecified'
            counts[status]=counts.get(status,0)+1
        summary['result_status_counts']=counts
    return summary


def preflight_paths(args):
    for label,path in (('--server',args.server),('--config',args.config),('--arguments',args.arguments)):
        if path is None:continue
        if not path.is_file():raise ValueError(f'{label} 文件不存在或不是普通文件：{path}')
        if label!='--server':
            try:data=json.loads(path.read_text(encoding='utf-8-sig'))
            except (ValueError,UnicodeError) as exc:raise ValueError(f'{label} 不是有效的 UTF-8 JSON：{path}') from exc
            if not isinstance(data,dict):raise ValueError(f'{label} 必须是 JSON 对象：{path}')
    if args.output.exists():
        raise ValueError(f'--output 路径已存在，原记录未覆盖：{args.output}。先检查旧记录；新路径不代表允许重试设备操作')


def exception_details(exc, depth=0):
    """Keep useful transport/startup causes from nested SDK exception groups."""
    result={'type':type(exc).__name__,'message':str(exc)[:4000]}
    children=getattr(exc,'exceptions',None)
    if children and depth<6:
        result['causes']=[exception_details(child,depth+1) for child in children[:8]]
        if len(children)>8:result['omitted_causes']=len(children)-8
    return result


async def run(args):
    arguments=json.loads(args.arguments.read_text(encoding='utf-8-sig')) if args.arguments else {}
    if not isinstance(arguments,dict):raise ValueError('Arguments must be a JSON object')
    report={'tool':args.tool,'arguments':arguments,'started_at':time.time(),'state':'starting','phase':'connecting',
            'calls':[],'request_intent_count':0,'session_opened':False,'normal_session_exit_confirmed':False,
            'automatic_retry':False,'notice':'Pending records describe request intent, not proof of dispatch. Client failure does not prove a device operation failed or rolled back; inspect returned results, original job IDs and device state before any further action.'}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    # Refuse reuse even after a crash. A new file is not permission to retry:
    # inspect the earlier journal and live state first.
    with args.output.open('x',encoding='utf-8') as f:json.dump(report,f,ensure_ascii=False,indent=2)
    def save():
        temp=args.output.with_suffix(args.output.suffix+'.tmp')
        temp.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8');temp.replace(args.output)
    try:
        async with Client(StdioServerParameters(command=sys.executable,args=[str(args.server),'--config',str(args.config)])) as client:
            report.update(session_opened=True,phase='session_ready');save()
            async def call(tool,params):
                report['pending']={'tool':tool,'arguments':params}
                report['phase']='calling_tool';report['request_intent_count']+=1;save()
                async def on_progress(progress,total,message):
                    event={'tool':tool,'received_at':time.time(),'progress':progress,'total':total,'message':message}
                    # Bound the journal; retain the newest notification and total count.
                    report['progress_notification_count']=report.get('progress_notification_count',0)+1
                    events=report.setdefault('progress_notifications',[])
                    events.append(event)
                    if len(events)>128:del events[:-128]
                    try:
                        save()
                        print(json.dumps({'event':'progress',**event},ensure_ascii=True),flush=True)
                    except Exception as exc:
                        # A progress-display failure must not abort an in-flight control.
                        report['progress_reporting_error']=concise_error(exc)
                result=await client.call_tool(tool,params,read_timeout_seconds=args.timeout_seconds,progress_callback=on_progress)
                report['calls'].append({'tool':tool,'result':result.model_dump(mode='json',by_alias=True)})
                report.pop('pending',None);report['phase']='response_received';save()
                if result.is_error:raise RuntimeError(tool_error_text(result.content))
                return result.structured_content or {}
            data=await call(args.tool,arguments)
            last_progress=None
            if args.tool=='dali_start_scene_save':
                while True:
                    progress={k:data.get(k) for k in ('job_id','state','progress','cancel_requested')}
                    if progress!=last_progress:print(json.dumps(progress,ensure_ascii=True),flush=True);last_progress=progress
                    if data['state'] in TERMINAL:break
                    report['phase']='waiting_for_job';save()
                    await asyncio.sleep(.5)
                    data=await call('dali_get_job',{'job_id':data['job_id']})
                data=await call('dali_get_job',{'job_id':data['job_id'],'include_result':True})
            elif args.tool in ALLOCATION_STARTS:
                ident=data['operation_id']
                while data['active_in_this_process']:
                    progress=result_summary(args.tool,data)
                    if progress!=last_progress:print(json.dumps(progress,ensure_ascii=True),flush=True);last_progress=progress
                    report['phase']='waiting_for_address_allocation';save()
                    await asyncio.sleep(.5)
                    data=await call('dali_get_address_allocation',{'operation_id':ident})
                if data.get('details_included') is False:
                    data=await call('dali_get_address_allocation',{'operation_id':ident,'include_details':True})
            elif args.tool in ('dali_start_parameter_batch','dali_start_project_restore','dali_start_project_delivery'):
                # This CLI owns an ephemeral stdio server. Do not close it just
                # because start returned while its parameter worker is writing.
                ident=arguments['plan_id']
                while data['active_in_this_process']:
                    progress=result_summary(args.tool,data)
                    if progress!=last_progress:print(json.dumps(progress,ensure_ascii=True),flush=True);last_progress=progress
                    report['phase']='waiting_for_'+args.tool.removeprefix('dali_start_');save()
                    await asyncio.sleep(.5)
                    data=await call(args.tool.replace('dali_start_','dali_get_',1),{'plan_id':ident})
            report.update(state='result_received',result=data,phase='closing_session');save()
        report.update(state='returned',phase='finished',normal_session_exit_confirmed=True,elapsed_seconds=round(time.time()-report['started_at'],3));save()
        print(json.dumps(result_summary(args.tool,data),ensure_ascii=True),flush=True)
        if args.tool in ADDRESS_TOOLS and address_unsuccessful(args.tool,data):return 2
        if args.tool in ALLOCATION_TOOLS and allocation_unsuccessful(args.tool,data):return 2
        if args.tool in ('dali_start_parameter_batch','dali_start_project_restore','dali_start_project_delivery') and data.get('execution',{}).get('state')!='completed_readback':return 2
        if args.tool in DELIVERY_TOOLS and (data.get('worker_error_in_this_process') or data.get('worker-error') or data.get('execution_unavailable') or data.get('execution',{}).get('journal_error')):return 2
        if data.get('state') in UNSUCCESSFUL or data.get('samples_verified') is False or data.get('journal_error'):return 2
        return 0
    except BaseException as exc:
        report.update(state='client_error',failure_phase=report['phase'],phase='failed',
                      error=str(exc) or type(exc).__name__,error_details=exception_details(exc),
                      elapsed_seconds=round(time.time()-report['started_at'],3))
        try:save()
        except BaseException as journal_error:
            # A second filesystem failure must not replace the transport/control error.
            exc.add_note(f'Could not persist final client failure: {type(journal_error).__name__}: {journal_error}')
        raise


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--server',type=Path,default=Path(__file__).with_name('run_server.py'))
    parser.add_argument('--config',type=Path,required=True)
    parser.add_argument('--tool',required=True)
    parser.add_argument('--arguments',type=Path)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--timeout-seconds',type=int,default=300,help='Per-call timeout; full project capture may need1800. No automatic retries.')
    parser.add_argument('--debug',action='store_true',help='Show full Python traceback on failure; never enables retries.')
    args=parser.parse_args(argv)
    if not 1<=args.timeout_seconds<=3600:parser.error('--timeout-seconds must be1..3600')
    try:
        preflight_paths(args)
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print('已中断；核对输出记录及设备状态后再操作，不自动重发。',file=sys.stderr)
        if args.debug:traceback.print_exc()
        return 130
    except Exception as exc:
        print('错误：'+concise_error(exc),file=sys.stderr)
        if args.output.is_file():print(f'记录位置：{args.output}；请核对原结果，不自动重发。',file=sys.stderr)
        if args.debug:traceback.print_exc()
        return 2


if __name__=='__main__':sys.exit(main())
