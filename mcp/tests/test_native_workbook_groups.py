import copy,json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from dali_mcp.service import Service
from dali_mcp.group_execution import ExperimentalGroupController
from dali_mcp.group_planning import plan_group_candidates
from dali_mcp.native_workbook_store import NativeWorkbookStore
from dali_mcp.native_workbook_groups import NativeWorkbookGroups
from test_group_execution import GroupLab
from test_native_workbook import document,package


class NativeWorkbookGroupsTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.g=GroupLab();self.opens=0
        self.info=dict(ip='127.0.0.1',dali_mac='02:00:00:00:00:01',dali_firmware='test')
        self.config=dict(gateway_ip=self.info['ip'],expected_dali_mac=self.info['dali_mac'],expected_firmware='test')
        self.service=Service(self.config,Path(self.tmp.name)/'state')
        self.controller=ExperimentalGroupController(self.service)
        self.native=NativeWorkbookGroups(self.service)
        patcher=patch.object(self.service,'_open',self.open);patcher.start();self.addCleanup(patcher.stop)
        self.path=Path(self.tmp.name)/'source.xlsx';self.id=self.imported(document())
    def open(self):self.opens+=1;return self.g,copy.deepcopy(self.info)
    def imported(self,d):
        self.path.write_bytes(package(d));return NativeWorkbookStore(self.service).import_file(str(self.path))['workbook_id']
    def prepare(self,**kw):
        args=dict(workbook_id=self.id,channel=2,addresses=[0,63],group_numbers=[1,15]);args.update(kw)
        return self.native.prepare(**args)
    def allow(self,p):
        self.config.update(enable_experimental_group_writes=True,allowed_group_write_operations=[
            dict(channel=2,**{k:s[k] for k in ('address','group','member')}) for s in p['proposal']['steps']])
    def execute(self,p):return self.controller.execute(p['plan_id'],p['confirmation'])

    def test_matching_selected_bits_no_plan_even_with_other_group_changes(self):
        self.g.masks[0]|=128
        p=self.prepare();self.assertEqual(p['state'],'already_matches');self.assertNotIn('plan_id',p)
        self.assertEqual(p['source_workbook']['membership_source'],'native grouping sheet')
        self.assertEqual(self.g.writes,[])

    def test_apply_undo_preserve_unselected_addresses_bits_and_output(self):
        self.g.masks[0]=1|128|32768;self.g.levels[0]=123
        originals=copy.deepcopy(self.g.masks)
        p=self.prepare();self.assertEqual([(s['address'],s['group'],s['member']) for s in p['proposal']['steps']],[(0,15,False),(0,1,True)])
        self.assertEqual(self.g.writes,[])
        with self.assertRaises(PermissionError):self.execute(p)
        self.allow(p);self.assertEqual(self.execute(p)['state'],'completed_readback')
        self.assertEqual(self.g.masks,{0:131,63:3,1:5});self.assertEqual(self.g.levels[0],123)
        undo=self.controller.prepare_restore(p['plan_id']);self.allow(undo)
        self.assertEqual(self.execute(undo)['state'],'completed_readback')
        self.assertEqual(self.g.masks,originals);self.assertEqual(self.g.levels[0],123)

    def test_all64_by16_memberships_are_included(self):
        self.id=self.imported(document(tuple(range(64))))
        self.g.masks={a:65532 for a in range(64)};self.g.levels={a:0 for a in range(64)};self.g.types={a:1 for a in range(64)}
        p=self.prepare(addresses=list(reversed(range(64))),group_numbers=list(reversed(range(16))))
        self.assertEqual(len(p['proposal']['steps']),1024)
        self.assertEqual({(s['address'],s['group']) for s in p['proposal']['steps']},{(a,g) for a in range(64) for g in range(16)})
        self.assertTrue(all(r['group_mask']==3 for r in p['proposal']['targets']))
        self.assertEqual(self.g.writes,[])

    def test_invalid_scope_and_missing_fixtures_rejected_before_network(self):
        for kw in [dict(addresses=[]),dict(addresses=[True]),dict(addresses=[1]),dict(addresses=[0,0]),
                   dict(group_numbers=[]),dict(group_numbers=[16]),dict(group_numbers=[False]),dict(channel=True)]:
            with self.subTest(kw=kw),self.assertRaises(ValueError):self.prepare(**kw)
        self.assertEqual(self.opens,0)

    def test_type_mismatch_and_current_live_or_membership_conflict_stop(self):
        self.g.types[0]=0
        with self.assertRaisesRegex(ValueError,'type'):self.prepare()
        self.g.types[0]=1;self.g.masks[0]|=32768
        p=self.prepare();self.allow(p);self.g.masks[63]|=128
        self.assertEqual(self.execute(p)['state'],'preflight_failed');self.assertEqual(self.g.writes,[])

    def test_rehashed_payload_not_matching_source_is_rejected(self):
        self.g.masks[0]|=32768;p=self.prepare();opened=self.opens
        d=copy.deepcopy(p);d.pop('confirmation');d['changes'][0]['remove']=[0,15]
        d['proposal']=plan_group_candidates(2,[r['membership'] for r in d['baseline']],d['changes'])
        d['digest']=self.controller._digest(d);self.service._save('group-plan',d,d['plan_id'])
        self.allow(d)
        with self.assertRaisesRegex(ValueError,'intent differs'):
            self.controller.execute(d['plan_id'],self.controller._confirmation(d))
        self.assertEqual(self.opens,opened);self.assertEqual(self.g.writes,[])

    def test_source_tampering_and_config_identity_change_reject_before_network(self):
        self.g.masks[0]|=32768;p=self.prepare();self.allow(p);opened=self.opens
        self.config['gateway_ip']='127.0.0.2'
        with self.assertRaisesRegex(ValueError,'gateway changed'):self.execute(p)
        self.config['gateway_ip']='127.0.0.1'
        path=self.service.state_dir/f'native-workbook-{self.id}.json'
        d=json.loads(path.read_text(encoding='utf-8'));d['summary']['source_sha256']='0'*64
        path.write_text(json.dumps(d),encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'checksum'):self.execute(p)
        self.assertEqual(self.opens,opened);self.assertEqual(self.g.writes,[])

    def test_unknown_receipt_stops_once_and_can_conditionally_undo(self):
        self.g.masks[0]=1|32768;original=copy.deepcopy(self.g.masks)
        p=self.prepare();self.allow(p);self.g.fault='lost'
        self.assertEqual(self.execute(p)['state'],'stopped_receipt_unknown');self.assertEqual(len(self.g.writes),1)
        self.g.fault=None;undo=self.controller.prepare_restore(p['plan_id']);self.allow(undo)
        self.assertEqual(self.execute(undo)['state'],'completed_readback')
        self.assertEqual(self.g.masks,original);self.assertEqual(len(self.g.writes),2)

if __name__=='__main__':unittest.main()
