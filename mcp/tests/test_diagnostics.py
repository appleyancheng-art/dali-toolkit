import tempfile,unittest
from unittest.mock import patch
from dali_mcp.service import Service
from dali_mcp.protocol import Gateway
from test_adapter import Emulator

DiagnosticService=Service

class DiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.em=Emulator();self.tmp=tempfile.TemporaryDirectory()
        self.service=DiagnosticService(dict(gateway_ip='127.0.0.1',expected_dali_mac='02:00:00:00:00:01',expected_firmware='1.6.9'),
            self.tmp.name,lambda:Gateway('127.0.0.1','127.0.0.1',0,.1,self.em.port))
        self.em.live[2,0]=(0,4000);self.em.live[2,63]=(150,4000)
    def tearDown(self):self.em.stop();self.tmp.cleanup()
    def test_fresh_read_scope_raw_status_and_no_control(self):
        r=self.service.diagnose_lamps(2,[0,63]);self.assertEqual(r['state'],'completed')
        self.assertEqual(r['completed_addresses'],[0,63]);self.assertFalse(r['uncompleted_addresses'])
        for row in r['rows']:
            self.assertEqual(row['assessment']['raw_status'],0xa5)
            self.assertFalse(row['assessment']['status_bits_interpreted'])
            self.assertEqual(row['assessment']['conclusion'],'no_inconsistency_observed')
        self.assertFalse(self.em.writes or self.em.live_commands or self.em.recalls or self.em.scans)
        self.assertEqual(self.service.get_diagnostic(r['operation_id']),r)
        self.assertEqual(len(self.service.get_diagnostic()['operations']),1)
    def test_invalid_scope_never_opens(self):
        for channel,addresses in ((True,[0]),(0,[0]),(2,[]),(2,[True]),(2,[64]),(2,[0,0]),(2,list(range(17)))):
            with self.assertRaises(ValueError):self.service.diagnose_lamps(channel,addresses)
        self.assertFalse(self.em.requests)
        with self.assertRaises(ValueError):self.service.get_diagnostic('../bad')
    def test_timeout_keeps_values_and_stops_before_next_address(self):
        self.em.parameter_drop=0xf01d
        r=self.service.diagnose_lamps(2,[0,63]);self.assertEqual(r['state'],'stopped_partial')
        self.assertFalse(r['offline_conclusion']);self.assertEqual(r['pending'],dict(address=0,field='min_level'))
        self.assertEqual(len(r['rows']),1);self.assertIn('fade_time_rate',r['rows'][0]['parameters'])
        self.assertNotIn('min_level',r['rows'][0]['parameters']);self.assertEqual(r['uncompleted_addresses'],[0,63])
        self.assertEqual(self.em.requests.count(0xf01d),1)
    def test_initial_logging_failure_does_not_connect(self):
        with patch.object(self.service,'_save',side_effect=OSError('disk full')):
            with self.assertRaises(OSError):self.service.diagnose_lamps(2,[0])
        self.assertFalse(self.em.requests)
    def test_identity_failure_not_offline(self):
        self.service.config['expected_dali_mac']='02:00:00:00:00:ff'
        r=self.service.diagnose_lamps(2,[0]);self.assertEqual(r['state'],'connection_failed')
        self.assertFalse(r['offline_conclusion']);self.assertEqual(r['rows'],[])
    def test_off_is_not_below_minimum_and_unknown_values_are_findings(self):
        self.em.live[2,0]=(0,4000);self.em.curve_code=77;self.em.fade_code=0
        r=self.service.diagnose_lamps(2,[0]);codes={x['code'] for x in r['rows'][0]['assessment']['findings']}
        self.assertEqual(codes,{'unknown_dimming_curve','extended_fade_duration_unknown'})
        self.em.live[2,0]=(255,65535)
        r=self.service.diagnose_lamps(2,[0]);codes={x['code'] for x in r['rows'][0]['assessment']['findings']}
        self.assertTrue({'unknown_live_level','unknown_live_ct'}<=codes)
    def test_range_disagreement_does_not_auto_repair(self):
        self.em.live[2,0]=(254,9000)
        r=self.service.diagnose_lamps(2,[0]);codes={x['code'] for x in r['rows'][0]['assessment']['findings']}
        self.assertEqual(codes,{'live_level_outside_configured_range','live_ct_outside_configured_range'})
        self.assertFalse(self.em.live_commands or self.em.writes)
    def test_dt6_has_no_color_requirement(self):
        self.em.lamp_type=0
        r=self.service.diagnose_lamps(2,[0]);self.assertEqual(r['state'],'completed')
        self.assertEqual(r['rows'][0]['parameters']['configured_min_ct']['state'],'not_applicable')
        self.assertFalse(any(x['code'].endswith('_ct') for x in r['rows'][0]['assessment']['findings']))
    def test_logging_failure_stops_remaining_queries(self):
        original=self.service._save;failed=False
        def save(prefix,report,ident=None):
            nonlocal failed
            if not failed and report.get('pending',{}).get('field')=='min_level':
                failed=True;raise OSError('disk full during review')
            return original(prefix,report,ident)
        with patch.object(self.service,'_save',save):r=self.service.diagnose_lamps(2,[0,63])
        self.assertEqual(r['state'],'stopped_partial');self.assertNotIn(0xf01d,self.em.requests)
        self.assertEqual(r['uncompleted_addresses'],[0,63]);self.assertEqual(r['error_type'],'OSError')
    def test_get_record_does_not_open_connection(self):
        r=self.service.diagnose_lamps(2,[0]);requests=list(self.em.requests)
        self.service.get_diagnostic(r['operation_id']);self.service.get_diagnostic()
        self.assertEqual(self.em.requests,requests)

if __name__=='__main__':unittest.main()
