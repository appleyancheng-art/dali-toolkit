"""Scoped ConfigMaster cache name writes, exact-byte backup and explicit restore.

The application must be closed. A sidecar OS lock serializes toolkit writers
using this same path, including different state directories. ConfigMaster and
unrelated editors do not participate; do not launch/edit them during execution.
No gateway traffic, process termination, command retry or automatic recovery.
"""
from contextlib import contextmanager
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import time
import uuid
from .native_cache import canonical, inspect_configmaster_cache
from .native_names import prepare_name_document,read_name


def digest(data):
    return hashlib.sha256(data).hexdigest()


def replace_native_file(source,target):
    """ReplaceFileW preserves target ACLs/attributes; never ignore merge errors.

    https://learn.microsoft.com/windows/win32/api/winbase/nf-winbase-replacefilew
    """
    if os.name!='nt':
        raise RuntimeError('native file replacement currently supports Windows only')
    import ctypes
    from ctypes import wintypes
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    replace=kernel.ReplaceFileW
    replace.argtypes=[wintypes.LPCWSTR,wintypes.LPCWSTR,wintypes.LPCWSTR,
                      wintypes.DWORD,wintypes.LPVOID,wintypes.LPVOID]
    replace.restype=wintypes.BOOL
    if not replace(str(target),str(source),None,0,None,None):
        raise ctypes.WinError(ctypes.get_last_error())


def require_configmaster_closed():
    """Read Windows process inventory; fail closed on unsupported/error states."""
    if os.name != 'nt':
        raise RuntimeError('native file writes require the Windows process guard')
    import ctypes
    from ctypes import wintypes
    class Entry(ctypes.Structure):
        _fields_=[('dwSize',wintypes.DWORD),('cntUsage',wintypes.DWORD),
                  ('th32ProcessID',wintypes.DWORD),('th32DefaultHeapID',ctypes.c_size_t),
                  ('th32ModuleID',wintypes.DWORD),('cntThreads',wintypes.DWORD),
                  ('th32ParentProcessID',wintypes.DWORD),('pcPriClassBase',wintypes.LONG),
                  ('dwFlags',wintypes.DWORD),('szExeFile',wintypes.WCHAR*260)]
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes=[wintypes.DWORD,wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype=wintypes.HANDLE
    kernel.Process32FirstW.argtypes=[wintypes.HANDLE,ctypes.POINTER(Entry)]
    kernel.Process32FirstW.restype=wintypes.BOOL
    kernel.Process32NextW.argtypes=kernel.Process32FirstW.argtypes
    kernel.Process32NextW.restype=wintypes.BOOL
    kernel.CloseHandle.argtypes=[wintypes.HANDLE]
    kernel.CloseHandle.restype=wintypes.BOOL
    handle=kernel.CreateToolhelp32Snapshot(2,0)
    if handle==ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        entry=Entry();entry.dwSize=ctypes.sizeof(entry)
        ok=kernel.Process32FirstW(handle,ctypes.byref(entry))
        while ok:
            if entry.szExeFile.casefold()=='configmaster.exe':
                raise RuntimeError('Close ConfigMaster before preparing/applying/restoring native file names')
            ok=kernel.Process32NextW(handle,ctypes.byref(entry))
        if ctypes.get_last_error()!=18:  # ERROR_NO_MORE_FILES
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel.CloseHandle(handle)


def _no_links(path):
    for part in (path,*path.parents):
        if part.is_symlink() or (hasattr(part,'is_junction') and part.is_junction()):
            raise ValueError('native file paths may not contain symlinks or junctions')
    if path.exists():
        info=path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1:
            raise ValueError('native file must be a regular file with a single hard link')


def _unique_pairs(pairs):
    result={}
    for key,value in pairs:
        if key in result:raise ValueError('duplicate JSON keys are not safe to rewrite')
        result[key]=value
    return result


def parse(data):
    if not 0<len(data)<=2_000_000:raise ValueError('native file size must be1..2000000 bytes')
    document=json.loads(data.decode('utf-8-sig'),object_pairs_hook=_unique_pairs)
    inspect_configmaster_cache(document)
    return document


def serialize(document,original):
    # Field values are preserved, whitespace is normalized; rollback keeps the
    # complete original bytes, including BOM and formatting.
    text=json.dumps(document,ensure_ascii=False,allow_nan=False,indent=4)
    if b'\r\n' in original:text=text.replace('\n','\r\n')
    if original.endswith(b'\n'):text+='\r\n' if b'\r\n' in original else '\n'
    return (b'\xef\xbb\xbf' if original.startswith(b'\xef\xbb\xbf') else b'')+text.encode('utf-8')


def restore_names(current,original,entries):
    """Reverse only our names; preserve newer unrelated values and namespaces."""
    draft=copy.deepcopy(current)
    for entry in entries:
        if not entry['changed']:continue
        path=entry['path'];value=read_name(current,path)
        if value==entry['before']:continue
        if value!=entry['after']:
            raise ValueError('name changed after write; restoration would overwrite newer name')
        node=draft
        for part in path[:-1]:node=node[part]
        if entry['before']['present']:
            node[path[-1]]=entry['before']['name']
        else:
            del node[path[-1]]
            # Remove only empty containers introduced by this transaction.
            for length in range(len(path)-1,0,-1):
                prefix=path[:length]
                if read_name(original,prefix)['present']:break
                parent=draft
                for part in prefix[:-1]:parent=parent[part]
                if parent[prefix[-1]]:break
                del parent[prefix[-1]]
    inspect_configmaster_cache(draft)
    return draft


class NativeFileController:
    write_gate = 'enable_native_cache_writes'
    def __init__(self,service):
        self.service=service
        self.guard=require_configmaster_closed
        self.replace=replace_native_file

    def _target(self):
        value=self.service.config.get('configmaster_cache_file')
        if type(value) is not str or not value:
            raise ValueError('configure one explicit absolute configmaster_cache_file first')
        path=Path(value)
        if not path.is_absolute() or '..' in path.parts or path.suffix.lower()!='.json':
            raise ValueError('native cache must be an absolute .json path without parent traversal')
        if str(path).startswith(('\\\\','//')):raise ValueError('network cache paths are unsupported')
        _no_links(path)
        return path.resolve()

    def _read(self,path):
        _no_links(path)
        with path.open('rb') as f:data=f.read(2_000_001)
        document=parse(data)
        self.service._check_native_identity(inspect_configmaster_cache(document))
        return data,document

    @contextmanager
    def _locked(self,path):
        lock=path.with_name('.'+path.name+'.dali-toolkit.lock')
        _no_links(lock)
        with self.service.lock, lock.open('a+b') as handle:
            if handle.seek(0,2)==0:handle.write(b'\0');handle.flush()
            handle.seek(0)
            try:
                if os.name=='nt':
                    import msvcrt
                    msvcrt.locking(handle.fileno(),msvcrt.LK_NBLCK,1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
            except OSError as exc:raise RuntimeError('another native-file operation holds this cache path') from exc
            try:yield
            finally:
                handle.seek(0)
                if os.name=='nt':msvcrt.locking(handle.fileno(),msvcrt.LK_UNLCK,1)
                else:fcntl.flock(handle.fileno(),fcntl.LOCK_UN)

    def _record_path(self,kind,ident,suffix='json'):
        if type(ident) is not str or not re.fullmatch('[0-9a-f]{32}',ident):raise ValueError('invalid native file plan ID')
        return self.service.state_dir/f'native-file-{kind}-{ident}.{suffix}'

    @staticmethod
    def _write_new(path,data):
        with path.open('xb') as f:
            f.write(data);f.flush();os.fsync(f.fileno())

    def _journal(self,ident,report):
        dest=self._record_path('run',ident)
        temp=dest.with_suffix('.tmp')
        with temp.open('wb') as f:
            f.write(canonical(report));f.flush();os.fsync(f.fileno())
        self._publish_journal(temp,dest)

    @staticmethod
    def _publish_journal(temp,dest):
        # Retry only publication of already-written log bytes, never a native
        # application file replacement or any device operation.
        for attempt in range(6):
            try:
                os.replace(temp,dest);return
            except PermissionError as exc:
                if getattr(exc,'winerror',None) not in (5,32,33) or attempt==5:raise
                time.sleep(.01*(2**attempt))

    @staticmethod
    def _plan_digest(plan):
        return digest(canonical({k:v for k,v in plan.items() if k not in ('digest','confirmation')}))

    @staticmethod
    def _confirmation(plan):
        return f"WRITE NATIVE NAMES {plan['plan_id']} {plan['digest'][:12]}"

    def read(self):
        path=self._target();data,document=self._read(path)
        imported=self.service.import_configmaster_cache(document)
        return dict(imported,file_path=str(path),file_sha256=digest(data),file_read_at=time.time(),
                    source='actual local file bytes; app memory and bus not read',application_file_written=False)

    def _prepare(self,path,before,after,operation,**details):
        ident=uuid.uuid4().hex
        plan=dict(plan_id=ident,format='native-name-file-v1',operation=operation,file_path=str(path),
                  before_sha256=digest(before),after_sha256=digest(after),created_at=time.time(),
                  expires_at=time.time()+600,device_commands_sent=0,**details)
        plan['digest']=self._plan_digest(plan)
        self._write_new(self._record_path('before',ident,'bin'),before)
        self._write_new(self._record_path('after',ident,'bin'),after)
        self._write_new(self._record_path('plan',ident),canonical(plan))
        return dict(plan,confirmation=self._confirmation(plan),application_file_written=False)

    def prepare(self,changes,source_workbook=None,source_project_names=None):
        if source_project_names is not None:
            from .project_names import ProjectNames
            if source_workbook is not None:raise ValueError('exactly one native name source required')
            ProjectNames(self.service).validate_plan(dict(operation='names',changes=changes,source_project_names=source_project_names),current=True)
        if source_workbook is not None:
            from .native_workbook_names import NativeWorkbookNames
            NativeWorkbookNames(self.service).validate_plan(dict(operation='names',changes=changes,source_workbook=source_workbook))
        path=self._target()
        with self._locked(path):
            self.guard()
            before,document=self._read(path)
            diff,draft=prepare_name_document(document,changes)
            after=before if draft==document else serialize(draft,before)
            parse(after)
            details={} if source_workbook is None else {'source_workbook':copy.deepcopy(source_workbook)}
            if source_project_names is not None:details['source_project_names']=copy.deepcopy(source_project_names)
            return self._prepare(path,before,after,'names',changes=changes,diff=diff,**details)

    def _load(self,ident):
        plan=json.loads(self._record_path('plan',ident).read_text(encoding='utf-8'))
        if plan.get('plan_id')!=ident or plan.get('digest')!=self._plan_digest(plan):raise ValueError('native file plan checksum mismatch')
        if plan['file_path']!=str(self._target()):raise ValueError('configured cache path changed')
        before=self._record_path('before',ident,'bin').read_bytes()
        after=self._record_path('after',ident,'bin').read_bytes()
        if digest(before)!=plan['before_sha256'] or digest(after)!=plan['after_sha256']:raise ValueError('native file backup checksum mismatch')
        first,last=parse(before),parse(after)
        self.service._check_native_identity(inspect_configmaster_cache(first))
        self.service._check_native_identity(inspect_configmaster_cache(last))
        if plan['operation']=='names':
            if 'source_project_names' in plan:
                from .project_names import ProjectNames
                ProjectNames(self.service).validate_plan(plan)
            if 'source_workbook' in plan:
                from .native_workbook_names import NativeWorkbookNames
                NativeWorkbookNames(self.service).validate_plan(plan)
            diff,draft=prepare_name_document(first,plan['changes'])
            expected=before if draft==first else serialize(draft,before)
            if diff!=plan['diff'] or expected!=after:raise ValueError('native file plan differs from scoped name edits')
        elif plan['operation']=='restore':
            source=plan['source_plan_id']
            original=json.loads(self._record_path('plan',source).read_text(encoding='utf-8'))
            if original.get('operation')!='names':raise ValueError('restore source must be a name edit')
            original,old,new=self._load(source)
            draft=restore_names(first,parse(old),original['diff'])
            expected=old if before==new else (before if draft==first else serialize(draft,before))
            if after!=expected:raise ValueError('restore differs from scoped original names')
            intent=json.loads(self._record_path('intent',source).read_text(encoding='utf-8'))
            if intent!={'plan_id':source,'digest':original['digest']}:raise ValueError('restore source lacks matching write intent')
        else:raise ValueError('unsupported native file operation')
        return plan,before,after

    def get(self,ident):
        plan,_,_=self._load(ident)
        result=dict(plan,confirmation=self._confirmation(plan))
        result['claimed']=self._record_path('claim',ident).exists()
        run=self._record_path('run',ident)
        if run.exists():
            execution=json.loads(run.read_text(encoding='utf-8'))
            if execution.get('plan_id')!=ident or execution.get('digest')!=plan['digest']:raise ValueError('native file run mismatch')
            result['execution']=execution
        result['note']='Saved phase is not process liveness; claimed plans are never replayed. No app/bus state read.'
        return result

    def _stage_dependencies(self,plan,path,report):
        """Subclass hook for immutable resources needed before a JSON commit."""

    def _check_dependencies(self,plan,path):
        """Subclass hook; called again immediately before JSON replacement."""
        if 'source_project_names' in plan:
            from .project_names import ProjectNames
            ProjectNames(self.service).validate_plan(plan,current=True)

    def apply(self,ident,confirmation,*,_deadline_check=None):
        plan,before,after=self._load(ident)
        if self.service.config.get(self.write_gate) is not True:raise ValueError('native cache write gate is disabled')
        if confirmation!=self._confirmation(plan):raise ValueError('native file confirmation mismatch')
        path=self._target()
        with self._locked(path):
            if self._record_path('claim',ident).exists():return self.get(ident)
            if time.time()>plan['expires_at'] and (_deadline_check is None or not _deadline_check(plan)):
                raise ValueError('native file plan expired; prepare from fresh file')
            self.guard()
            current,_=self._read(path)
            if current!=before:raise ValueError('native cache changed since plan; no write attempted')
            if 'source_project_names' in plan:
                from .project_names import ProjectNames
                ProjectNames(self.service).validate_plan(plan,current=True)
            self._write_new(self._record_path('claim',ident),canonical({'plan_id':ident,'digest':plan['digest']}))
            report=dict(plan_id=ident,digest=plan['digest'],state='claimed',application_file_written=False,
                        device_commands_sent=0,replace_attempted=False,automatic_replay=False)
            temp=path.with_name('.'+path.name+'.dali-'+ident+'.tmp')
            try:
                self._journal(ident,report)
                if before==after:
                    report['state']='already_matches'
                else:
                    self._write_new(temp,after)
                    self._stage_dependencies(plan,path,report)
                    self.guard()
                    if self._target()!=path or self._read(path)[0]!=before:raise ValueError('cache changed during preparation; write stopped')
                    self._check_dependencies(plan,path)
                    self._write_new(self._record_path('intent',ident),canonical({'plan_id':ident,'digest':plan['digest']}))
                    report['state']='replace_intent';self._journal(ident,report)
                    # A second guard/check narrows the non-participating app/editor
                    # race. It is not an OS interlock against application launch.
                    self.guard()
                    if self._read(path)[0]!=before:raise ValueError('cache changed before replacement')
                    self._check_dependencies(plan,path)
                    report['replace_attempted']=True
                    self.replace(temp,path)
                    report['replace_returned']=True
                    report['application_file_written']=True
                    current,_=self._read(path)
                    report['readback_sha256']=digest(current)
                    report['state']='written_verified' if current==after else 'write_uncertain'
            except Exception as exc:
                report['error']=str(exc) or type(exc).__name__
                report['state']='write_uncertain' if report['replace_attempted'] else 'stopped_before_replace'
                if report['replace_attempted']:
                    try:
                        current,_=self._read(path);report['readback_sha256']=digest(current)
                        if current==after:
                            report.update(state='written_verified_with_error',application_file_written=True)
                        elif current==before:report['state']='not_written_verified'
                    except Exception as read_error:report['readback_error']=str(read_error)
            finally:
                try:
                    if temp.exists():temp.unlink()
                except OSError as exc:report['temp_cleanup_error']=str(exc)
                report['finished_at']=time.time()
                try:self._journal(ident,report)
                except OSError as exc:report['journal_error']=str(exc)
            return report

    def prepare_restore(self,ident):
        original,before,after=self._load(ident)
        if original['operation']!='names':raise ValueError('only original name edits can be restored')
        if before==after:return dict(state='already_restored',source_plan_id=ident,application_file_written=False)
        intent=json.loads(self._record_path('intent',ident).read_text(encoding='utf-8'))
        if intent!={'plan_id':ident,'digest':original['digest']}:raise ValueError('missing original replacement intent')
        path=self._target()
        with self._locked(path):
            self.guard()
            current,_=self._read(path)
            draft=restore_names(parse(current),parse(before),original['diff'])
            if draft==parse(current):return dict(state='already_restored',source_plan_id=ident,application_file_written=False)
            restored=before if current==after else serialize(draft,current)
            return self._prepare(path,current,restored,'restore',source_plan_id=ident,diff=original['diff'],
                                 exact_original_bytes=current==after,unrelated_current_fields_preserved=True)
