import json,os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from dali_mcp.service import Service
from dali_mcp.native_file import NativeFileController
from dali_mcp.project_delivery import ProjectDeliveryController


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.service=Service({},self.tmp.name)

    def tearDown(self):self.tmp.cleanup()

    @staticmethod
    def busy(code):
        error=PermissionError('simulated Windows sharing denial');error.winerror=code;return error

    def test_transient_windows_replace_denial_only_retries_publication(self):
        ident='a'*32
        self.service._save('example',{'state':'old'},ident)
        original=Path.replace;attempts=[]
        def replace(path,target):
            attempts.append(1)
            if len(attempts)<3:
                self.assertEqual(json.loads(target.read_text())['state'],'old')
                raise self.busy(5)
            return original(path,target)
        with patch.object(Path,'replace',replace),patch('dali_mcp.service.time.sleep') as sleep:
            self.service._save('example',{'state':'new'},ident)
        self.assertEqual(len(attempts),3);self.assertEqual(sleep.call_count,2)
        self.assertEqual(json.loads((Path(self.tmp.name)/f'example-{ident}.json').read_text())['state'],'new')

    def test_permanent_windows_denial_bounded_preserves_old_and_pending(self):
        ident='b'*32;self.service._save('example',{'state':'old'},ident)
        with patch.object(Path,'replace',side_effect=self.busy(32)) as replace,patch('dali_mcp.service.time.sleep') as sleep:
            with self.assertRaises(PermissionError):self.service._save('example',{'state':'new'},ident)
        self.assertEqual(replace.call_count,6);self.assertEqual(sleep.call_count,5)
        self.assertEqual(json.loads((Path(self.tmp.name)/f'example-{ident}.json').read_text())['state'],'old')
        self.assertEqual(json.loads((Path(self.tmp.name)/f'example-{ident}.tmp').read_text())['state'],'new')

    def test_unrelated_permission_error_not_retried(self):
        with patch.object(Path,'replace',side_effect=PermissionError('not a Windows sharing error')) as replace,patch('dali_mcp.service.time.sleep') as sleep:
            with self.assertRaises(PermissionError):self.service._save('example',{})
        self.assertEqual(replace.call_count,1);sleep.assert_not_called()

    def journal_writers(self):
        ident='c'*32;native=NativeFileController(self.service);delivery=ProjectDeliveryController(self.service)
        return ((lambda v:native._journal(ident,v),native._record_path('run',ident)),
                (lambda v:delivery._journal('run',ident,v),delivery._path('run',ident)))

    def test_native_and_delivery_transient_sharing_denial(self):
        for write,dest in self.journal_writers():
            for code in (5,32,33):
                write({'state':'old'});original=os.replace;attempts=[]
                def replace(source,target):
                    attempts.append(1)
                    if len(attempts)<3:
                        self.assertEqual(json.loads(dest.read_text())['state'],'old')
                        raise self.busy(code)
                    return original(source,target)
                with patch('os.replace',side_effect=replace),patch('time.sleep') as sleep:
                    write({'state':'new'})
                self.assertEqual(len(attempts),3);self.assertEqual(sleep.call_count,2)
                self.assertEqual(json.loads(dest.read_text())['state'],'new')

    def test_native_and_delivery_permanent_denial_keeps_pending(self):
        for write,dest in self.journal_writers():
            write({'state':'old'})
            with patch('os.replace',side_effect=self.busy(32)) as replace,patch('time.sleep') as sleep:
                with self.assertRaises(PermissionError):write({'state':'new'})
            self.assertEqual(replace.call_count,6);self.assertEqual(sleep.call_count,5)
            self.assertEqual(json.loads(dest.read_text())['state'],'old')
            self.assertEqual(json.loads(dest.with_suffix('.tmp').read_text())['state'],'new')

    def test_native_and_delivery_unrelated_error_not_retried(self):
        for write,dest in self.journal_writers():
            with patch('os.replace',side_effect=PermissionError('unrelated')) as replace,patch('time.sleep') as sleep:
                with self.assertRaises(PermissionError):write({'state':'new'})
            self.assertEqual(replace.call_count,1);sleep.assert_not_called()


if __name__=='__main__':unittest.main()
