import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from dali_mcp.jobs import JobManager, TERMINAL


class ControlledService:
    def __init__(self,path):
        self.state_dir=path
        self.lock=threading.RLock()
        self.entered=threading.Event()
        self.release=threading.Event()
        self.calls=0
    def validate_scene_changes(self,changes):return changes
    def save_scene_batch(self,changes,progress,cancelled):
        with self.lock:
            self.calls+=1
            progress({'plan_id':'a'*32,'phase':'backup','total':2,'backed_up':1,'completed':0})
            self.entered.set()
            if not self.release.wait(3):raise RuntimeError('test release timed out')
            state='cancelled_before_write' if cancelled() else 'completed'
            return {'state':state,'plan_id':'a'*32,'results':[]}


class JobTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.service=ControlledService(self.tmp.name)
        self.manager=JobManager(self.service)
    def tearDown(self):
        self.service.release.set()
        if self.manager.worker:self.manager.worker.join(4)
        self.manager.close()
        self.tmp.cleanup()
    def wait_terminal(self,ident):
        deadline=time.monotonic()+4
        while time.monotonic()<deadline:
            data=self.manager.get(ident,True)
            if data['state'] in TERMINAL:return data
            time.sleep(.01)
        self.fail('job did not finish')
    def test_progress_and_cancel_are_available_while_bus_is_locked(self):
        self.manager.start_save('test-request',[{'address':0}])
        self.assertTrue(self.service.entered.wait(2))
        data=self.manager.get('test-request')
        self.assertEqual(data['progress']['backed_up'],1)
        self.assertNotIn('changes',data)
        data=self.manager.cancel('test-request')
        self.assertTrue(data['cancel_requested'])
        self.assertNotIn(data['state'],TERMINAL)
        self.service.release.set()
        self.assertEqual(self.wait_terminal('test-request')['state'],'cancelled_before_write')
    def test_duplicate_request_never_runs_again(self):
        changes=[{'address':0}]
        self.manager.start_save('same',changes)
        self.assertTrue(self.service.entered.wait(2))
        self.manager.start_save('same',changes)
        with self.assertRaises(ValueError):self.manager.start_save('same',[{'address':1}])
        with self.assertRaises(RuntimeError):self.manager.start_save('second',changes)
        self.service.release.set();self.wait_terminal('same')
        self.manager.start_save('same',changes)
        self.assertEqual(self.service.calls,1)
    def test_restart_marks_pending_interrupted_without_replay(self):
        self.manager._save({'job_id':'crash','state':'running','digest':'old','plan_id':'b'*32})
        self.manager.close()
        self.manager=JobManager(self.service)
        data=self.manager.get('crash')
        self.assertEqual(data['state'],'interrupted')
        self.assertEqual(data['plan_id'],'b'*32)
        self.assertEqual(self.service.calls,0)
    def test_second_owner_rejected_and_id_cannot_escape_directory(self):
        with self.assertRaises(RuntimeError):JobManager(self.service)
        for ident in ('../elsewhere','', 'x'*65):
            with self.assertRaises(ValueError):self.manager.start_save(ident,[])
    def test_worker_error_is_persisted(self):
        def fail(*args,**kwargs):raise RuntimeError('controlled failure')
        self.service.save_scene_batch=fail
        self.manager.start_save('failed',[{'address':0}])
        data=self.wait_terminal('failed')
        self.assertEqual(data['state'],'failed')
        self.assertIn('controlled failure',data['error'])
