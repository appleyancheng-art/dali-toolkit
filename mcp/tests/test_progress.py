"""No hardware: request isolation, transport failure and stdio notification tests."""
import asyncio
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
import anyio
from mcp import Client
from mcp.client.stdio import StdioServerParameters
from dali_mcp.progress import publish_saved, run_with_progress
from dali_mcp.service import Service


class Context:
    def __init__(self, failure=False, delay=0):
        self.events=[];self.failure=failure;self.delay=delay
    async def report_progress(self,n,total,message):
        if self.delay:await anyio.sleep(self.delay)
        if self.failure:raise OSError('subscriber disconnected')
        self.events.append((n,total,json.loads(message)))


class ProgressTests(unittest.TestCase):
    def test_saved_before_notification_and_result_preserved(self):
        with tempfile.TemporaryDirectory() as folder:
            service=Service({},folder);ctx=Context()
            def operation():
                ident=service._save('live-output',{'state':'reading','progress':{'phase':'profiles','completed':0,'total':64}})
                self.assertTrue((Path(folder)/f'live-output-{ident}.json').is_file())
                time.sleep(.02)
                service._save('live-output',{'state':'completed','commands':[{}]},ident)
                return {'operation_id':ident,'state':'completed'}
            result=anyio.run(run_with_progress,ctx,operation)
            self.assertTrue(ctx.events)
            self.assertTrue(all(e[1] is None and e[2]['operation_id']==result['operation_id'] for e in ctx.events))
            self.assertEqual(ctx.events[-1][2]['state'],'completed')
            self.assertEqual([e[0] for e in ctx.events],list(range(1,len(ctx.events)+1)))

    def test_two_requests_never_share_progress_and_sink_is_cleared(self):
        contexts=[Context(),Context()]
        def operation(ident):
            for n in range(4):
                publish_saved('live-channel',ident,{'state':'reading','progress':{'completed':n}})
                time.sleep(.005)
            return ident
        async def run():
            return await asyncio.gather(*(run_with_progress(c,operation,str(n)*32) for n,c in enumerate(contexts)))
        self.assertEqual(anyio.run(run),['0'*32,'1'*32])
        for n,ctx in enumerate(contexts):
            self.assertTrue(ctx.events)
            self.assertTrue(all(e[2]['operation_id']==str(n)*32 for e in ctx.events))
        before=[len(c.events) for c in contexts]
        publish_saved('live-channel','unrelated',{'state':'completed'})
        self.assertEqual(before,[len(c.events) for c in contexts])

    def test_slow_or_broken_subscriber_does_not_block_or_retry_worker(self):
        for ctx in (Context(failure=True),Context(delay=10)):
            calls=[]
            def operation():
                calls.append(1)
                for n in range(100):publish_saved('live-group','a'*32,{'state':'reading','progress':{'completed':n}})
                return 'original result'
            started=time.monotonic()
            self.assertEqual(anyio.run(run_with_progress,ctx,operation),'original result')
            self.assertLess(time.monotonic()-started,2)
            self.assertEqual(calls,[1])

    def test_original_failure_not_wrapped_or_retried(self):
        failure=ValueError('invalid scoped target');calls=[]
        def operation():
            calls.append(1);publish_saved('live-output','a'*32,{'state':'preflight_failed'})
            raise failure
        with self.assertRaises(ValueError) as caught:anyio.run(run_with_progress,Context(),operation)
        self.assertIs(caught.exception,failure);self.assertEqual(calls,[1])

    def test_cancellation_waits_for_worker_no_detached_control(self):
        finished=threading.Event();calls=[]
        def operation():
            calls.append(1)
            time.sleep(.12)
            publish_saved('live-output','a'*32,{'state':'completed'})
            finished.set()
        async def run():
            with anyio.move_on_after(.03):
                await run_with_progress(Context(),operation)
            self.assertTrue(finished.is_set())
        anyio.run(run);self.assertEqual(calls,[1])

    def test_unrelated_records_are_not_emitted(self):
        ctx=Context()
        def operation():publish_saved('capabilities','a'*32,{'secret':'not progress'})
        anyio.run(run_with_progress,ctx,operation)
        self.assertEqual(ctx.events,[])

    def test_notifications_add_no_bus_queries_or_controls_for_64_targets(self):
        from test_workflows import FakeGateway
        transcripts=[]
        for use_progress in (False,True):
            with tempfile.TemporaryDirectory() as folder:
                gateway=FakeGateway();wire=[]
                def set_component(ch,a,field,value):
                    wire.append(('set',ch,a,field,value));return {'received':True}
                def read_actual(ch,a,typ):
                    wire.append(('read',ch,a));return dict(channel=ch,address=a,light_type_code=typ,level_raw=254,kelvin_raw=4000)
                gateway.set_live_component=set_component;gateway.read_actual=read_actual
                config={'gateway_ip':'127.0.0.1','expected_dali_mac':'02:00:00:00:00:01','expected_firmware':'1.6.9'}
                service=Service(config,folder,lambda:gateway)
                args=(2,[{'address':a,'level_raw':254} for a in range(64)],[0,63])
                if use_progress:result=anyio.run(run_with_progress,Context(),service.set_live_output,*args)
                else:result=service.set_live_output(*args)
                self.assertTrue(result['samples_verified'])
                transcripts.append((gateway.opened,gateway.profiles,wire))
        self.assertEqual(transcripts[0],transcripts[1])
        self.assertEqual(len([x for x in transcripts[1][2] if x[0]=='set']),64)

    def test_standard_sdk_delivers_before_final_result_and_hides_context_argument(self):
        # Real server entrypoint and standard stdio SDK, synthetic gateway only.
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);fixture=root/'server.py';config=root/'config.json'
            source=Path(__file__).resolve().parents[1]
            fixture.write_text('''import sys,time
sys.path.insert(0,sys.argv.pop(1))
from dali_mcp import __main__ as entry
class Gateway:
    def discover(self):return {'ip':'127.0.0.1','dali_mac':'02:00:00:00:00:01','dali_firmware':'1.6.9'}
    def connect(self):pass
    def close(self):pass
    def read_lamp_profile(self,ch,a):
        time.sleep(.08)
        return dict(channel=ch,address=a,light_type_code=1,dimming_curve_code=0,min_kelvin_raw=2700,max_kelvin_raw=6500)
    def set_live_component(self,ch,a,field,value):return {'received':True}
    def read_actual(self,ch,a,typ):return dict(channel=ch,address=a,light_type_code=typ,level_raw=0,kelvin_raw=4000)
real=entry.Service
entry.Service=lambda config,state:real(config,state,Gateway)
entry.main()
''',encoding='utf-8')
            config.write_text(json.dumps({'gateway_ip':'127.0.0.1','expected_dali_mac':'02:00:00:00:00:01','expected_firmware':'1.6.9'}))
            async def run():
                events=[];returned=False
                async def progress(n,total,message):
                    self.assertFalse(returned)
                    events.append(json.loads(message))
                params=StdioServerParameters(command=sys.executable,args=[str(fixture),str(source),'--config',str(config)])
                async with Client(params) as session:
                    listing=await session.list_tools()
                    for t in listing.tools:
                        if t.name in ('dali_set_live_output','dali_set_channel_live_output','dali_set_group_live_output'):
                            self.assertNotIn('ctx',t.input_schema.get('properties',{}))
                    reply=await session.call_tool('dali_set_live_output',{'channel':2,'changes':[{'address':0,'level_raw':0}],'verify_addresses':[0]},progress_callback=progress)
                    returned=True
                    self.assertFalse(reply.is_error,reply.content)
                    self.assertTrue(reply.structured_content['samples_verified'])
                    self.assertTrue(events)
                    self.assertTrue(all(e['operation_id']==reply.structured_content['operation_id'] for e in events))
                    self.assertIn('profiles',[e['progress'].get('phase') for e in events])
                    rejected=await session.call_tool('dali_set_channel_live_output',{'channel':9,'level_raw':0,'verify_addresses':[0]})
                    self.assertTrue(rejected.is_error)
            anyio.run(run)


if __name__=='__main__':unittest.main()
