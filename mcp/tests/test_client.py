import asyncio
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import client
from mcp.types import TextContent


class Reply:
    def __init__(self, data, error=False):
        self.structured_content=data; self.is_error=error; self.content=['tool rejected' if error else 'ok']
    def model_dump(self, **kwargs):
        return {'structuredContent':self.structured_content,'isError':self.is_error,'content':self.content}


class Session:
    def __init__(self, replies=(), enter_error=None, exit_error=None):
        self.replies=list(replies); self.enter_error=enter_error; self.exit_error=exit_error
        self.calls=[]; self.exits=0
    async def __aenter__(self):
        if self.enter_error:raise self.enter_error
        return self
    async def __aexit__(self, *exc):
        self.exits+=1
        if self.exit_error:raise self.exit_error
    async def call_tool(self, tool, arguments, **kwargs):
        self.calls.append((tool,arguments))
        value=self.replies.pop(0)
        if isinstance(value,BaseException):raise value
        return value


class ClientJournalTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        root=Path(self.tmp.name)
        self.args=SimpleNamespace(arguments=None,tool='dali_status',output=root/'result.json',
                                  server=root/'server.py',config=root/'config.json',timeout_seconds=10)
    def run_client(self,session):
        with patch.object(client,'Client',return_value=session), contextlib.redirect_stdout(io.StringIO()):
            return asyncio.run(client.run(self.args))
    def report(self):return json.loads(self.args.output.read_text(encoding='utf-8'))

    def test_diagnostic_connection_and_partial_failures_are_nonzero_without_retry(self):
        for i,state in enumerate(('connection_failed','stopped_partial','completed')):
            self.args.tool='dali_diagnose_lamps';self.args.output=Path(self.tmp.name)/f'diagnostic-{i}.json'
            session=Session([Reply(dict(state=state,finding_count=2,physical_health_verified=False))])
            self.assertEqual(self.run_client(session),0 if state=='completed' else 2)
            self.assertEqual(len(session.calls),1)
        summary=client.result_summary('dali_diagnose_lamps',dict(state='completed',finding_count=2,physical_health_verified=False))
        self.assertEqual(summary['finding_count'],2);self.assertFalse(summary['physical_health_verified'])

    def test_native_extreme_outcome_and_summary_do_not_hide_uncertainty(self):
        for i,state in enumerate(('completed_readback','preflight_failed','stopped_receipt_unknown','verification_failed','outcome_unknown')):
            self.args.tool='dali_set_lamp_level_extreme';self.args.output=Path(self.tmp.name)/f'extreme-{i}.json'
            data=dict(state=state,extreme='minimum',expected_level_raw=3,target_verified=state in ('completed_readback','stopped_receipt_unknown'))
            session=Session([Reply(data)])
            self.assertEqual(self.run_client(session),0 if state=='completed_readback' else 2)
            self.assertEqual(len(session.calls),1)
            summary=client.result_summary(self.args.tool,data)
            self.assertEqual(summary['target_verified'],data['target_verified'])
            self.assertEqual(summary['expected_level_raw'],3)

    def test_relative_uncertainty_wrong_direction_and_log_failure_are_nonzero(self):
        outcomes=[dict(state='outcome_uncertain'),dict(state='unexpected_output'),dict(state='completed_readback',journal_error='disk unavailable'),dict(state='completed_readback')]
        for i,data in enumerate(outcomes):
            self.args.tool='dali_relative_dim';self.args.output=Path(self.tmp.name)/f'relative-{i}.json'
            session=Session([Reply(data)])
            self.assertEqual(self.run_client(session),0 if i==3 else 2)
            self.assertEqual(len(session.calls),1)
            if 'journal_error' in data:self.assertEqual(client.result_summary(self.args.tool,data)['journal_error'],data['journal_error'])

    def test_allocation_review_waits_same_operation_until_reviewed(self):
        self.args.tool='dali_start_address_allocation_review'
        session=Session([Reply(dict(operation_id='a'*32,state='reviewing',active_in_this_process=True)),
                         Reply(dict(operation_id='a'*32,state='reviewed',active_in_this_process=False,claimed=False))])
        with patch.object(client.asyncio,'sleep',new_callable=AsyncMock):self.assertEqual(self.run_client(session),0)
        self.assertEqual(session.calls[1],('dali_get_address_allocation',{'operation_id':'a'*32}))
        self.assertTrue(self.report()['normal_session_exit_confirmed'])

    def test_allocation_cli_fetches_full_evidence_once_after_compact_poll(self):
        self.args.tool='dali_start_address_allocation'
        session=Session([Reply(dict(operation_id='e'*32,state='allocating',active_in_this_process=True,details_included=False)),
                         Reply(dict(operation_id='e'*32,state='completed_readback',active_in_this_process=False,details_included=False)),
                         Reply(dict(operation_id='e'*32,state='completed_readback',active_in_this_process=False,details_included=True,execution={'after_rows':[{'address':2}]}))])
        with patch.object(client.asyncio,'sleep',new_callable=AsyncMock):self.assertEqual(self.run_client(session),0)
        self.assertEqual(session.calls[-1],('dali_get_address_allocation',dict(operation_id='e'*32,include_details=True)))
        self.assertEqual(self.report()['result']['execution']['after_rows'],[{'address':2}])

    def test_allocation_apply_waits_but_only_clean_readback_succeeds(self):
        for i,state in enumerate(('completed_readback','reviewed','verified_receipt_unknown','stopped_count_mismatch','worker_failed')):
            self.args.tool='dali_start_address_allocation';self.args.output=Path(self.tmp.name)/f'alloc-{i}.json'
            session=Session([Reply(dict(operation_id='b'*32,state='preflight',active_in_this_process=True)),
                             Reply(dict(operation_id='b'*32,state=state,active_in_this_process=False,claimed=True))])
            with patch.object(client.asyncio,'sleep',new_callable=AsyncMock):
                self.assertEqual(self.run_client(session),0 if state=='completed_readback' else 2)
            self.assertEqual(len(session.calls),2)

    def test_allocation_poll_failure_preserves_operation_without_restart(self):
        self.args.tool='dali_start_address_allocation'
        session=Session([Reply(dict(operation_id='c'*32,state='allocating',active_in_this_process=True)),TimeoutError('poll failed')])
        with patch.object(client.asyncio,'sleep',new_callable=AsyncMock),self.assertRaises(TimeoutError):self.run_client(session)
        self.assertEqual(self.report()['pending'],dict(tool='dali_get_address_allocation',arguments={'operation_id':'c'*32}))
        self.assertEqual(len(session.calls),2)

    def test_allocation_errors_override_success_and_query_does_not_wait(self):
        for i,extra in enumerate(({'worker-error':{'error':'disk full'}},{'execution_record_missing':True},
                                 {'execution':{'state':'completed_readback','log_error':'disk full'}},
                                 {'active_in_this_process':True})):
            self.args.tool='dali_get_address_allocation';self.args.output=Path(self.tmp.name)/f'query-{i}.json'
            data=dict(operation_id='d'*32,state='completed_readback',active_in_this_process=False,claimed=True);data.update(extra)
            session=Session([Reply(data)]);self.assertEqual(self.run_client(session),2);self.assertEqual(len(session.calls),1)

    def test_address_apply_uncertainty_and_mismatch_are_nonzero_without_retry(self):
        for tool in ('dali_apply_address_move','dali_apply_address_removal'):
            for state in ('stopped_address_mismatch','stopped_lamp_mismatch','verified_receipt_unknown','stopped_unknown','preflight_failed'):
                with self.subTest(tool=tool,state=state):
                    self.args.tool=tool;self.args.output=Path(self.tmp.name)/(tool+'-'+state+'.json')
                    session=Session([Reply(dict(state=state,write_intent=True,plan_id='a'*32))])
                    self.assertEqual(self.run_client(session),2)
                    self.assertEqual(len(session.calls),1);self.assertTrue(self.report()['normal_session_exit_confirmed'])
                    self.assertEqual(self.report()['result']['state'],state)

    def test_address_get_uses_recorded_execution_not_consumed_plan_state(self):
        self.args.tool='dali_get_address_removal'
        data=dict(state='consumed',plan_id='b'*32,claimed=True,execution=dict(state='stopped_address_mismatch',write_intent=True))
        self.assertEqual(self.run_client(Session([Reply(data)])),2)
        summary=client.result_summary(self.args.tool,data)
        self.assertEqual(summary['state'],'stopped_address_mismatch');self.assertTrue(summary['write_intent'])

    def test_missing_address_execution_record_is_uncertain(self):
        self.args.tool='dali_get_address_move'
        data=dict(state='consumed',plan_id='c'*32,claimed=True,execution_record_missing=True)
        self.assertEqual(self.run_client(Session([Reply(data)])),2)
        self.assertEqual(client.result_summary(self.args.tool,data)['state'],'execution_unavailable')

    def test_address_completed_readback_is_success_for_apply_and_get(self):
        for i,(tool,state) in enumerate([('dali_apply_address_move','completed_readback'),('dali_apply_address_removal','completed_address_removal_readback'),('dali_get_address_move','completed_readback'),('dali_get_address_removal','completed_address_removal_readback')]):
            self.args.tool=tool;self.args.output=Path(self.tmp.name)/f'success-{i}.json'
            run=dict(state=state,write_intent=True)
            data=dict(state='consumed',claimed=True,execution=run) if tool.startswith('dali_get_') else run
            self.assertEqual(self.run_client(Session([Reply(data)])),0)
            self.assertEqual(client.result_summary(tool,data)['state'],state)

    def test_address_log_error_is_not_reported_as_clean_success(self):
        self.args.tool='dali_apply_address_removal'
        data=dict(state='completed_address_removal_readback',log_error='disk full')
        self.assertEqual(self.run_client(Session([Reply(data)])),2)
        self.assertEqual(client.result_summary(self.args.tool,data)['log_error'],'disk full')

    def test_unexecuted_address_plan_query_does_not_claim_device_failure(self):
        self.args.tool='dali_get_address_removal';data=dict(state='prepared',claimed=False,plan_id='d'*32)
        self.assertEqual(self.run_client(Session([Reply(data)])),0)
        self.assertEqual(client.result_summary(self.args.tool,data)['state'],'prepared')

    def test_startup_failure_is_terminal_with_no_request_intent(self):
        session=Session(enter_error=ExceptionGroup('SDK startup',[RuntimeError('owner directory in use')]))
        with self.assertRaises(ExceptionGroup):self.run_client(session)
        r=self.report()
        self.assertEqual((r['state'],r['failure_phase']),('client_error','connecting'))
        self.assertEqual(r['request_intent_count'],0); self.assertEqual(r['calls'],[])
        self.assertNotIn('pending',r); self.assertFalse(r['session_opened'])
        self.assertEqual(r['error_details']['causes'][0]['message'],'owner directory in use')
        self.assertEqual(session.calls,[])

    def test_success_marks_closed_and_preserves_tool_result(self):
        session=Session([Reply({'state':'reviewed','review_id':'example'})])
        self.assertEqual(self.run_client(session),0)
        r=self.report()
        self.assertEqual((r['state'],r['phase']),('returned','finished'))
        self.assertTrue(r['session_opened'] and r['normal_session_exit_confirmed'])
        self.assertEqual(r['request_intent_count'],1); self.assertEqual(len(r['calls']),1)
        self.assertEqual(r['result']['review_id'],'example'); self.assertEqual(session.exits,1)

    def test_tool_timeout_keeps_pending_without_retry_or_rollback(self):
        self.args.tool='dali_save_scene_batch'
        session=Session([TimeoutError('response not received')])
        with self.assertRaises(TimeoutError):self.run_client(session)
        r=self.report()
        self.assertEqual(r['failure_phase'],'calling_tool')
        self.assertEqual(r['pending']['tool'],self.args.tool)
        self.assertEqual(r['calls'],[]); self.assertEqual(len(session.calls),1)
        self.assertFalse(r['automatic_retry']); self.assertNotIn('result',r)

    def test_cleanup_failure_does_not_hide_received_success(self):
        session=Session([Reply({'state':'completed','plan_id':'already-written'})],exit_error=RuntimeError('stdio close failed'))
        with self.assertRaises(RuntimeError):self.run_client(session)
        r=self.report()
        self.assertEqual((r['state'],r['failure_phase']),('client_error','closing_session'))
        self.assertEqual(r['result']['plan_id'],'already-written')
        self.assertEqual(len(r['calls']),1); self.assertNotIn('pending',r)
        self.assertEqual(len(session.calls),1)

    def test_tool_error_preserves_response_as_distinct_from_missing_reply(self):
        session=Session([Reply({'reason':'invalid target'},error=True)])
        with self.assertRaises(RuntimeError):self.run_client(session)
        r=self.report()
        self.assertEqual(r['failure_phase'],'response_received')
        self.assertTrue(r['calls'][0]['result']['isError']); self.assertNotIn('pending',r)

    def test_job_poll_timeout_preserves_original_job_id_without_restart(self):
        self.args.tool='dali_start_scene_save'
        session=Session([Reply({'job_id':'keep-original-id','state':'queued'}),TimeoutError('poll failed')])
        with patch.object(client.asyncio,'sleep',new_callable=AsyncMock), self.assertRaises(TimeoutError):
            self.run_client(session)
        r=self.report()
        self.assertEqual(r['pending'],{'tool':'dali_get_job','arguments':{'job_id':'keep-original-id'}})
        self.assertEqual(r['calls'][0]['result']['structuredContent']['job_id'],'keep-original-id')
        self.assertEqual([name for name,_ in session.calls],['dali_start_scene_save','dali_get_job'])

    def test_job_success_polls_same_id_and_closes_after_final_result(self):
        self.args.tool='dali_start_scene_save'
        session=Session([Reply({'job_id':'same-id','state':'queued'}),Reply({'job_id':'same-id','state':'completed'}),
                         Reply({'job_id':'same-id','state':'completed','result':{'verified':True}})])
        with patch.object(client.asyncio,'sleep',new_callable=AsyncMock):self.assertEqual(self.run_client(session),0)
        self.assertEqual(session.calls[1:], [('dali_get_job',{'job_id':'same-id'}),
                                             ('dali_get_job',{'job_id':'same-id','include_result':True})])
        self.assertTrue(self.report()['result']['result']['verified'])

    def test_partial_review_exit_code_is_unsuccessful_but_not_transport_error(self):
        self.assertEqual(self.run_client(Session([Reply({'state':'reviewed_with_rejections'})])),2)
        self.assertEqual(self.report()['state'],'returned')

    def test_parameter_batch_cli_keeps_session_until_worker_exit_and_reports_partial(self):
        self.args.tool='dali_start_parameter_batch';self.args.arguments=Path(self.tmp.name)/'args.json'
        self.args.arguments.write_text(json.dumps(dict(plan_id='a'*32,confirmation='explicit')),encoding='utf-8')
        session=Session([Reply(dict(active_in_this_process=True,plan=dict(plan_id='a'*32))),
            Reply(dict(active_in_this_process=True,execution=dict(state='completed_readback',completed_steps=4,total_steps=4))),
            Reply(dict(active_in_this_process=False,execution=dict(state='stopped_receipt_unknown',completed_steps=3,total_steps=4)))])
        with patch.object(client.asyncio,'sleep',new_callable=AsyncMock):self.assertEqual(self.run_client(session),2)
        self.assertEqual([name for name,_ in session.calls],['dali_start_parameter_batch','dali_get_parameter_batch','dali_get_parameter_batch'])
        self.assertTrue(self.report()['normal_session_exit_confirmed']);self.assertEqual(session.exits,1)
        self.assertEqual(client.result_summary(self.args.tool,self.report()['result'])['state'],'stopped_receipt_unknown')

    def test_parameter_batch_cli_poll_error_retains_plan_without_restart(self):
        self.args.tool='dali_start_parameter_batch';self.args.arguments=Path(self.tmp.name)/'args.json'
        self.args.arguments.write_text(json.dumps(dict(plan_id='a'*32,confirmation='explicit')),encoding='utf-8')
        session=Session([Reply(dict(active_in_this_process=True,plan=dict(plan_id='a'*32))),TimeoutError('get unavailable')])
        with patch.object(client.asyncio,'sleep',new_callable=AsyncMock),self.assertRaises(TimeoutError):self.run_client(session)
        self.assertEqual(self.report()['pending'],dict(tool='dali_get_parameter_batch',arguments=dict(plan_id='a'*32)))
        self.assertEqual(len(session.calls),2);self.assertEqual(self.report()['calls'][0]['result']['structuredContent']['plan']['plan_id'],'a'*32)

    def test_restore_conflict_is_unsuccessful_without_retry(self):
        session=Session([Reply({'state':'restore_conflict','conflicts':[{'address':0}]})])
        self.assertEqual(self.run_client(session),2)
        self.assertEqual(self.report()['state'],'returned')
        self.assertEqual(len(session.calls),1)

    def test_project_restore_cli_keeps_server_alive_until_worker_exit(self):
        self.args.tool='dali_start_project_restore';self.args.arguments=Path(self.tmp.name)/'args.json'
        self.args.arguments.write_text(json.dumps(dict(plan_id='b'*32,confirmation='explicit')),encoding='utf-8')
        session=Session([Reply(dict(active_in_this_process=True,plan=dict(plan_id='b'*32))),
            Reply(dict(active_in_this_process=False,execution=dict(state='completed_readback',completed_steps=3,total_steps=3)))])
        with patch.object(client.asyncio,'sleep',new_callable=AsyncMock):self.assertEqual(self.run_client(session),0)
        self.assertEqual([name for name,_ in session.calls],['dali_start_project_restore','dali_get_project_restore'])
        self.assertTrue(self.report()['normal_session_exit_confirmed'])
        self.assertEqual(client.result_summary(self.args.tool,self.report()['result'])['completed_steps'],3)

    def test_project_restore_cli_poll_failure_never_restarts(self):
        self.args.tool='dali_start_project_restore';self.args.arguments=Path(self.tmp.name)/'args.json'
        self.args.arguments.write_text(json.dumps(dict(plan_id='b'*32,confirmation='explicit')),encoding='utf-8')
        session=Session([Reply(dict(active_in_this_process=True,plan=dict(plan_id='b'*32))),TimeoutError('poll failed')])
        with patch.object(client.asyncio,'sleep',new_callable=AsyncMock),self.assertRaises(TimeoutError):self.run_client(session)
        self.assertEqual(self.report()['pending'],dict(tool='dali_get_project_restore',arguments=dict(plan_id='b'*32)))
        self.assertEqual(len(session.calls),2)

    def test_delivery_journal_and_worker_errors_are_nonzero_even_with_old_success(self):
        self.args.arguments=Path(self.tmp.name)/'delivery-args.json'
        self.args.arguments.write_text(json.dumps({'plan_id':'a'*32,'confirmation':'test'}),encoding='utf-8')
        errors=[{'worker-error':{'error':'failed'}},{'worker_error_in_this_process':{'error':'failed','journal_error':'disk denied'}},
                {'execution_unavailable':'missing'},{'execution':{'state':'completed_readback','journal_error':'disk denied'}}]
        for tool in client.DELIVERY_TOOLS:
            for i,error in enumerate(errors):
                self.args.tool=tool;self.args.output=Path(self.tmp.name)/f'{tool}-{i}.json'
                data=dict(active_in_this_process=False,plan={'plan_id':'a'*32},execution={'state':'completed_readback'});data.update(error)
                session=Session([Reply(data)])
                self.assertEqual(self.run_client(session),2);self.assertEqual(len(session.calls),1)
                summary=client.result_summary(tool,data)
                self.assertTrue(any(summary.get(k) for k in ('worker_error','journal_error','execution_unavailable')))

    def test_project_delivery_cli_waits_and_reports_both_phases(self):
        self.args.tool='dali_start_project_delivery';self.args.arguments=Path(self.tmp.name)/'args.json'
        self.args.arguments.write_text(json.dumps(dict(plan_id='b'*32,confirmation='explicit')),encoding='utf-8')
        session=Session([Reply(dict(active_in_this_process=True,plan=dict(plan_id='b'*32))),
            Reply(dict(active_in_this_process=False,execution=dict(state='completed_readback',completed_phases=2,total_phases=2)))])
        with patch.object(client.asyncio,'sleep',new_callable=AsyncMock):self.assertEqual(self.run_client(session),0)
        self.assertEqual([name for name,_ in session.calls],['dali_start_project_delivery','dali_get_project_delivery'])
        self.assertEqual(client.result_summary(self.args.tool,self.report()['result'])['completed_phases'],2)
        self.assertTrue(self.report()['normal_session_exit_confirmed'])

    def test_project_delivery_partial_and_poll_error_are_not_replayed(self):
        self.args.tool='dali_start_project_delivery';self.args.arguments=Path(self.tmp.name)/'args.json'
        self.args.arguments.write_text(json.dumps(dict(plan_id='b'*32,confirmation='explicit')),encoding='utf-8')
        session=Session([Reply(dict(active_in_this_process=False,execution=dict(state='stopped_partial',completed_phases=1,total_phases=2)))])
        self.assertEqual(self.run_client(session),2);self.assertEqual(len(session.calls),1)
        self.args.output=Path(self.tmp.name)/'poll-failed.json'
        session=Session([Reply(dict(active_in_this_process=True,plan=dict(plan_id='b'*32))),TimeoutError('poll lost')])
        with patch.object(client.asyncio,'sleep',new_callable=AsyncMock),self.assertRaises(TimeoutError):self.run_client(session)
        self.assertEqual([name for name,_ in session.calls],['dali_start_project_delivery','dali_get_project_delivery'])

    def test_parameter_unexpected_live_output_is_unsuccessful(self):
        session=Session([Reply({'state':'stopped_live_output_changed'})])
        self.assertEqual(self.run_client(session),2)
        self.assertEqual(len(session.calls),1)

    def test_cancellation_is_journaled_without_resend(self):
        session=Session([asyncio.CancelledError()])
        with self.assertRaises(asyncio.CancelledError):self.run_client(session)
        self.assertEqual(self.report()['error_details']['type'],'CancelledError')
        self.assertEqual(len(session.calls),1)

    def test_existing_output_refuses_before_starting_client(self):
        self.args.output.write_text('original',encoding='utf-8')
        with patch.object(client,'Client') as construct, self.assertRaises(FileExistsError):
            asyncio.run(client.run(self.args))
        construct.assert_not_called(); self.assertEqual(self.args.output.read_text(),'original')

    def test_live_progress_is_bounded_and_preserved_on_timeout_without_resend(self):
        self.args.tool='dali_set_live_output'
        class ProgressSession(Session):
            async def call_tool(inner,tool,arguments,**kwargs):
                inner.calls.append((tool,arguments))
                for n in range(150):
                    await kwargs['progress_callback'](n+1,None,json.dumps({'operation_id':'a'*32,'phase':'profiles'}))
                raise TimeoutError('final response missing')
        session=ProgressSession()
        with self.assertRaises(TimeoutError):self.run_client(session)
        r=self.report()
        self.assertEqual(r['progress_notification_count'],150)
        self.assertEqual(len(r['progress_notifications']),128)
        self.assertEqual(r['progress_notifications'][0]['progress'],23)
        self.assertEqual(r['progress_notifications'][-1]['progress'],150)
        self.assertEqual(r['pending']['tool'],self.args.tool)
        self.assertNotIn('result',r);self.assertEqual(len(session.calls),1)

    def test_progress_display_failure_does_not_interrupt_physical_call(self):
        class ProgressSession(Session):
            async def call_tool(inner,tool,arguments,**kwargs):
                inner.calls.append((tool,arguments))
                with patch.object(client,'print',side_effect=OSError('display unavailable'),create=True):
                    await kwargs['progress_callback'](1,None,'profiles')
                return Reply({'state':'completed','operation_id':'b'*32})
        session=ProgressSession()
        self.assertEqual(self.run_client(session),0)
        self.assertEqual(len(session.calls),1)
        self.assertEqual(self.report()['progress_reporting_error'],'display unavailable')
        self.assertEqual(self.report()['result']['state'],'completed')

    def test_failure_to_save_error_preserves_original_exception(self):
        failure=RuntimeError('original startup failure')
        with patch.object(Path,'replace',side_effect=PermissionError('disk denied')):
            with self.assertRaises(RuntimeError) as caught:self.run_client(Session(enter_error=failure))
        self.assertIs(caught.exception,failure)
        self.assertIn('Could not persist final client failure',failure.__notes__[0])
        # The old durable file and the newer failed-publication temporary file remain evidence.
        self.assertEqual(self.report()['state'],'starting')
        pending=json.loads(self.args.output.with_suffix('.json.tmp').read_text(encoding='utf-8'))
        self.assertEqual(pending['state'],'client_error')


class ClientPresentationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        (self.root/'server.py').write_text('# offline test',encoding='utf-8')
        (self.root/'config.json').write_text('{}',encoding='utf-8')
        self.argv=['--server',str(self.root/'server.py'),'--config',str(self.root/'config.json'),
                   '--tool','dali_status','--output',str(self.root/'result.json')]

    def invoke(self,extra=()):
        out=io.StringIO();err=io.StringIO()
        with contextlib.redirect_stdout(out),contextlib.redirect_stderr(err):code=client.main(self.argv+list(extra))
        return code,out.getvalue(),err.getvalue()

    def test_missing_paths_and_invalid_json_do_not_start_client(self):
        cases=[('--arguments','missing.json'),('--config','missing.json'),('--server','missing.py')]
        for flag,name in cases:
            with patch.object(client,'Client') as construct:
                code,out,err=self.invoke([flag,str(self.root/name)])
                self.assertEqual(code,2);construct.assert_not_called()
                self.assertIn(flag,err);self.assertNotIn('Traceback',err);self.assertFalse(out)
                self.assertFalse((self.root/'result.json').exists())
        for value in ('{broken','[]'):
            args=self.root/'args.json';args.write_text(value,encoding='utf-8')
            with patch.object(client,'Client') as construct:
                code,_,err=self.invoke(['--arguments',str(args)])
                self.assertEqual(code,2);construct.assert_not_called();self.assertIn('JSON',err)

    def test_existing_output_is_preserved_and_explained(self):
        output=self.root/'result.json';output.write_text('original',encoding='utf-8')
        with patch.object(client,'Client') as construct:
            code,_,err=self.invoke()
        self.assertEqual(code,2);construct.assert_not_called();self.assertEqual(output.read_text(),'original')
        self.assertIn('--output',err);self.assertIn('已存在',err);self.assertNotIn('Traceback',err)

    def test_nested_tool_error_is_text_only_and_preserves_journal(self):
        reply=Reply({},error=True)
        # Use the actual SDK TextContent shape, not the fixture's string list.
        reply.content=[TextContent(type='text',text='tool rejected\nKelvin outside limits')]
        reply.model_dump=lambda **kwargs: {'isError':True,'content':[item.model_dump() for item in reply.content]}
        session=Session([reply])
        with patch.object(client,'Client',return_value=session):code,_,err=self.invoke()
        self.assertEqual(code,2);self.assertIn('Kelvin outside limits',err)
        self.assertNotIn('TextContent(',err);self.assertNotIn('Traceback',err)
        report=json.loads((self.root/'result.json').read_text(encoding='utf-8'))
        self.assertTrue(report['calls'][0]['result']['isError']);self.assertEqual(len(session.calls),1)
        nested=ExceptionGroup('outer',[ExceptionGroup('inner',[RuntimeError('specific cause')])])
        self.assertEqual(client.concise_error(nested),'specific cause')

    def test_debug_includes_traceback_without_retry(self):
        session=Session(enter_error=RuntimeError('startup failed'))
        with patch.object(client,'Client',return_value=session):code,_,err=self.invoke(['--debug'])
        self.assertEqual(code,2);self.assertIn('Traceback',err);self.assertEqual(session.calls,[])

    def test_read_only_summary_and_scene_counts_are_not_empty(self):
        session=Session([Reply({'rows':[{},{}]})])
        with patch.object(client,'Client',return_value=session):code,out,err=self.invoke()
        self.assertEqual(code,0);self.assertFalse(err)
        self.assertEqual(json.loads(out),{'tool':'dali_status','state':'returned','rows_count':2})
        result=client.result_summary('dali_apply_scene_batch',{'state':'completed','results':[
            {'status':'verified'},{'status':'unchanged'},{'status':'unchanged'}]})
        self.assertEqual(result['result_status_counts'],{'verified':1,'unchanged':2})
        self.assertEqual(client.result_summary('dali_get_layout',{'layouts':[]})['layouts_count'],0)


if __name__=='__main__':unittest.main()
