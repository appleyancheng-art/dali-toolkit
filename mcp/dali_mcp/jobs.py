"""Persistent, single-owner jobs. No automatic resume or retry after process exit."""
import hashlib
import json
import os
import re
import threading
import time
from pathlib import Path

TERMINAL={'completed','stopped_partial','failed','interrupted','cancelled_partial','cancelled_before_write'}


class JobManager:
    def __init__(self,service):
        self.service=service
        self.root=Path(service.state_dir)/'jobs'
        self.root.mkdir(exist_ok=True)
        self.lock=threading.RLock()
        self.active=None
        self.stop_event=None
        self.worker=None
        # One owner per state directory; prevents a second process from
        # declaring the first process's live jobs interrupted or running them.
        self.owner=(self.root/'owner.lock').open('a+b')
        self.owner.seek(0,2)
        if self.owner.tell()==0:self.owner.write(b'0');self.owner.flush()
        self.owner.seek(0)
        try:
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(self.owner.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(self.owner.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError:
            self.owner.close()
            raise RuntimeError('Another MCP process owns this job directory')
        for path in self.root.glob('job-*.json'):
            data=json.loads(path.read_text(encoding='utf-8'))
            if data['state'] not in TERMINAL:
                data.update(state='interrupted',error='Previous process exited; read plan and device state before new writes. No automatic resume.')
                self._save(data)

    @staticmethod
    def _validate_id(ident):
        if not isinstance(ident,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',ident):
            raise ValueError('request_id/job_id must be 1..64 ASCII letters, digits, _ or -')
        return ident

    def _path(self,ident):
        return self.root/f'job-{self._validate_id(ident)}.json'

    def _save(self,data):
        data['updated_at']=time.time()
        path=self._path(data['job_id']);temp=path.with_suffix('.tmp')
        temp.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
        temp.replace(path)

    def _load(self,ident):
        return json.loads(self._path(ident).read_text(encoding='utf-8'))

    def get(self,ident,include_result=False):
        with self.lock:
            data=self._load(ident)
            if not include_result:
                data.pop('changes',None);data.pop('result',None)
            return data

    def start_save(self,request_id,changes):
        ident=self._validate_id(request_id)
        changes=self.service.validate_scene_changes(changes)
        digest=hashlib.sha256(json.dumps(changes,sort_keys=True).encode()).hexdigest()
        with self.lock:
            if self._path(ident).exists():
                existing=self._load(ident)
                if existing['digest']!=digest:raise ValueError('request_id already used with different changes')
                return self.get(ident)
            if self.active:raise RuntimeError('A save job is already active; query or cancel it first')
            # Do not queue behind other bus work. The worker acquires the
            # service's existing serial lock before any physical request.
            if not self.service.lock.acquire(blocking=False):raise RuntimeError('Gateway is busy')
            self.service.lock.release()
            data={'job_id':ident,'kind':'save_scene_batch','state':'queued','created_at':time.time(),
                  'digest':digest,'changes':changes,'cancel_requested':False,
                  'progress':{'phase':'queued','total':len(changes),'backed_up':0,'completed':0},
                  'lifecycle':'Keep this MCP server process alive until terminal; process exit never resumes writes.'}
            self._save(data)
            self.active=ident;self.stop_event=threading.Event()
            self.worker=threading.Thread(target=self._run,args=(ident,changes,self.stop_event),daemon=True)
            self.worker.start()
            return self.get(ident)

    def _run(self,ident,changes,event):
        def update(progress):
            with self.lock:
                data=self._load(ident)
                data.update(progress=progress,plan_id=progress['plan_id'])
                self._save(data)
        try:
            with self.lock:
                data=self._load(ident);data['state']='running';self._save(data)
            result=self.service.save_scene_batch(changes,progress=update,cancelled=event.is_set)
            with self.lock:
                data=self._load(ident)
                data.update(state=result['state'],result=result,plan_id=result['plan_id'])
                self._save(data)
        except Exception as exc:
            with self.lock:
                data=self._load(ident);data.update(state='failed',error=str(exc));self._save(data)
        finally:
            with self.lock:self.active=None;self.stop_event=None

    def cancel(self,ident):
        with self.lock:
            data=self._load(ident)
            if data['state'] in TERMINAL:return self.get(ident)
            if self.active!=ident:raise RuntimeError('Job is not owned by this process; inspect its state')
            data['cancel_requested']=True;self._save(data)
            self.stop_event.set()
            return self.get(ident)

    def close(self):
        with self.lock:
            if self.active:raise RuntimeError('Cannot release job ownership while a job is active')
        if self.owner.closed:return
        self.owner.seek(0)
        if os.name=='nt':
            import msvcrt
            msvcrt.locking(self.owner.fileno(),msvcrt.LK_UNLCK,1)
        else:
            import fcntl
            fcntl.flock(self.owner.fileno(),fcntl.LOCK_UN)
        self.owner.close()
