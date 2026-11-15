"""Independent SDK processes exercise the new-site CLI and reusable config."""
import asyncio
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from mcp import Client
from mcp.client.stdio import StdioServerParameters
ROOT=Path(__file__).resolve().parents[1]


class GatewayClientTests(unittest.TestCase):
    def cli(self,root,name,extra=()):
        output=root/(name+'.json')
        command=[sys.executable,str(ROOT/'gateway_client.py'),'--server',str(ROOT/'tests/gateway_fixture.py'),
                 '--config',str(root/'bootstrap/config.json'),'--output',str(output),*extra]
        result=subprocess.run(command,capture_output=True,text=True,encoding='utf-8',timeout=45)
        return result,json.loads(output.read_text(encoding='utf-8')) if output.exists() else None

    def test_separate_search_connect_and_read_processes(self):
        with tempfile.TemporaryDirectory(prefix='dali-gateway-cli-') as tmp:
            root=Path(tmp)
            result,report=self.cli(root,'search')
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(report['state'],'searched');self.assertEqual(len(report['calls']),1)
            result,connected=self.cli(root,'connect',['--connect-mac','02:00:00:00:00:12'])
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(connected['state'],'selected');self.assertTrue(connected['session_closed'])
            self.assertEqual([c['tool'] for c in connected['calls']],['dali_search_gateways','dali_select_gateway'])
            config=Path(connected['config_path']);self.assertTrue(config.is_file())
            async def read():
                async with Client(StdioServerParameters(command=sys.executable,args=[str(ROOT/'tests/gateway_fixture.py'),'--config',str(config)])) as client:
                    r=await client.call_tool('dali_read_actual',dict(channel=2,addresses=[0]))
                    self.assertFalse(r.is_error)
                    self.assertEqual(r.structured_content['rows'][0]['model_site'],'Site-B')
            asyncio.run(read())

    def test_missing_mac_stops_before_selection(self):
        with tempfile.TemporaryDirectory(prefix='dali-gateway-cli-miss-') as tmp:
            result,report=self.cli(Path(tmp),'missing',['--connect-mac','02:00:00:00:00:99'])
            self.assertNotEqual(result.returncode,0)
            self.assertEqual(report['state'],'failed')
            self.assertEqual([c['tool'] for c in report['calls']],['dali_search_gateways'])
            self.assertNotIn('config_path',report)

    def test_existing_evidence_is_not_overwritten(self):
        with tempfile.TemporaryDirectory(prefix='dali-gateway-cli-old-') as tmp:
            root=Path(tmp);(root/'old.json').write_text('{"keep":true}',encoding='utf-8')
            result,report=self.cli(root,'old')
            self.assertNotEqual(result.returncode,0);self.assertEqual(report,dict(keep=True))


if __name__=='__main__':unittest.main(verbosity=2)
