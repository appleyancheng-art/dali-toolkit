import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from dali_mcp.address_metadata import AddressMetadataController,migrate_document,restore_document
from dali_mcp.address_moves import AddressMoveController
from dali_mcp.native_file import canonical,parse
from dali_mcp.service import Service
from test_native_cache import document
from test_address_moves import AddressLab


def fixture():
    d=document();d['floorPlan']=dict(image='board.png',marks=[
        dict(ch=2,addr=0,x=.1,y=.2,extra='preserve'),dict(ch=1,addr=63,x=.7,y=.8)])
    return d


class AddressMetadataTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.path=self.root/'gateway.json'
        self.original=b'\xef\xbb\xbf'+json.dumps(fixture(),ensure_ascii=False,indent=2).encode()+b'\n'
        self.path.write_bytes(self.original);self.lab=AddressLab()
        self.info=dict(ip='127.0.0.1',dali_mac='02:00:00:00:00:01',dali_firmware='1.6.9')
        self.s=Service(dict(gateway_ip='127.0.0.1',expected_dali_mac=self.info['dali_mac'],expected_firmware='1.6.9',
            configmaster_cache_file=str(self.path),enable_experimental_address_writes=True,
            allowed_address_moves=[dict(channel=2,source_address=0,destination_address=63),dict(channel=2,source_address=63,destination_address=0)],
            enable_native_address_metadata_writes=True),self.root/'state')
        self.s._open=self.open;self.c=AddressMetadataController(self.s);self.c.guard=lambda:None
        if os.name!='nt':self.c.replace=os.replace
        self.moves=AddressMoveController(self.s,process_guard=lambda:None)
        self.move=self.moves.prepare(2,0,63);self.moves.execute(self.move['plan_id'],self.move['confirmation'])
    def tearDown(self):
        self.assertEqual(self.lab.opens,self.lab.closes);self.tmp.cleanup()
    def open(self):self.lab.opens+=1;return self.lab,copy.deepcopy(self.info)
    def prepare(self,components=None):return self.c.prepare(self.move['plan_id'],components or ['lamp_name','floorplan_mark'])
    def apply(self,p):return self.c.apply(p['plan_id'],p['confirmation'])
    def undo_bus(self):
        p=self.moves.prepare_restore(self.move['plan_id']);self.moves.execute(p['plan_id'],p['confirmation'])

    def test_full_metadata_forward_reverse_exact_bytes(self):
        p=self.prepare();self.assertEqual(self.path.read_bytes(),self.original)
        r=self.apply(p);self.assertEqual(r['state'],'written_verified');self.assertEqual(len(self.lab.writes),1)
        changed=parse(self.path.read_bytes());self.assertEqual(changed['zCh2']['addresses']['63']['name'],'灯零')
        self.assertEqual(changed['zCh2']['addresses']['0']['name'],'');self.assertEqual(changed['floorPlan']['marks'][0]['addr'],63)
        expected,_=migrate_document(fixture(),2,0,63,['lamp_name','floorplan_mark']);self.assertEqual(changed,expected)
        self.assertTrue(self.c.get(p['plan_id'])['execution']['network_traffic'])
        self.undo_bus();undo=self.c.prepare_restore(p['plan_id']);self.assertTrue(undo['exact_original_bytes'])
        self.assertEqual(self.apply(undo)['state'],'written_verified');self.assertEqual(self.path.read_bytes(),self.original)
        self.assertEqual(self.c.prepare_restore(p['plan_id'])['state'],'already_restored')

    def test_name_only_preserves_entire_floorplan_and_data(self):
        p=self.prepare(['lamp_name']);self.apply(p);d=parse(self.path.read_bytes())
        self.assertEqual(d['floorPlan'],fixture()['floorPlan'])
        for key in ('devicesInGroup','groupsPerAddr','groupNames','sceneNamesByGroup'):
            self.assertEqual(d['zCh2'][key],fixture()['zCh2'][key])
        self.assertEqual(d['zCh2']['addresses']['0']['scenes'],fixture()['zCh2']['addresses']['0']['scenes'])

    def test_marker_only_preserves_names_and_image(self):
        self.apply(self.prepare(['floorplan_mark']));d=parse(self.path.read_bytes())
        self.assertEqual(d['zCh2'],fixture()['zCh2']);self.assertEqual(d['floorPlan']['image'],'board.png')
        self.assertEqual(d['floorPlan']['marks'][0]['extra'],'preserve')

    def test_name_and_marker_collision_rejected(self):
        for change in ('name','mark'):
            d=fixture()
            if change=='name':d['zCh2']['addresses']['63']['name']='other'
            else:d['floorPlan']['marks'].append(dict(ch=2,addr=63,x=.4,y=.5))
            self.path.write_bytes(canonical(d))
            with self.assertRaisesRegex(ValueError,'destination'):self.prepare()

    def test_missing_target_row_not_created(self):
        d=fixture();del d['zCh2']['addresses']['63'];self.path.write_bytes(canonical(d))
        with self.assertRaisesRegex(ValueError,'cache rows'):self.prepare()

    def test_invalid_selection_before_reads(self):
        opened=self.lab.opens
        for components in ([],['lamp_name','lamp_name'],['parameters'],[{}],True,None):
            with self.assertRaises(ValueError):self.c.prepare(self.move['plan_id'],components)
        self.assertEqual(self.lab.opens,opened)

    def test_bus_undo_blocks_stale_forward_plan(self):
        p=self.prepare();self.undo_bus()
        with self.assertRaisesRegex(ValueError,'addresses'):self.apply(p)
        self.assertEqual(self.path.read_bytes(),self.original)

    def test_stored_parameter_or_scene_drift_blocks_file_write(self):
        p=self.prepare();self.lab.lamps[63]['scenes'][2]['level_raw']=253
        with self.assertRaisesRegex(ValueError,'lamp differs'):self.apply(p)
        self.assertEqual(self.path.read_bytes(),self.original)

    def test_bus_restore_required_before_metadata_restore(self):
        p=self.prepare();self.apply(p)
        with self.assertRaisesRegex(ValueError,'addresses'):self.c.prepare_restore(p['plan_id'])

    def test_undo_preserves_unrelated_fields_and_marker_order(self):
        p=self.prepare();self.apply(p);self.undo_bus()
        d=parse(self.path.read_bytes());d['lastSeen']='later';d['floorPlan']['marks'].reverse()
        d['floorPlan']['marks'].append(dict(ch=2,addr=7,x=.9,y=.6));self.path.write_bytes(canonical(d))
        undo=self.c.prepare_restore(p['plan_id']);self.assertFalse(undo['exact_original_bytes']);self.apply(undo)
        result=parse(self.path.read_bytes());self.assertEqual(result['lastSeen'],'later')
        self.assertEqual([m['addr'] for m in result['floorPlan']['marks']],[63,0,7])

    def test_third_party_marker_or_name_drift_blocks_undo(self):
        p=self.prepare();self.apply(p);self.undo_bus();post=self.path.read_bytes()
        for which in ('name','position','background'):
            d=parse(post)
            if which=='name':d['zCh2']['addresses']['63']['name']='third party'
            elif which=='position':d['floorPlan']['marks'][0]['x']=.9
            else:d['floorPlan']['image']='other.png'
            self.path.write_bytes(canonical(d))
            with self.assertRaises(ValueError):self.c.prepare_restore(p['plan_id'])

    def test_changed_file_after_prepare_is_not_overwritten(self):
        p=self.prepare();d=fixture();d['lastSeen']='new';self.path.write_bytes(canonical(d))
        with self.assertRaisesRegex(ValueError,'changed'):self.apply(p)
        self.assertEqual(parse(self.path.read_bytes()),d)

    def test_gate_and_confirmation_no_network(self):
        p=self.prepare();opened=self.lab.opens;self.s.config['enable_native_address_metadata_writes']=False
        with self.assertRaisesRegex(ValueError,'gate'):self.apply(p)
        self.s.config['enable_native_address_metadata_writes']=True
        with self.assertRaisesRegex(ValueError,'confirmation'):self.c.apply(p['plan_id'],'yes')
        self.assertEqual(self.lab.opens,opened)

    def test_claimed_plan_query_does_not_repeat_read_or_write(self):
        p=self.prepare();self.apply(p);opened=self.lab.opens;self.apply(p)
        self.assertEqual(self.lab.opens,opened);self.assertEqual(len(self.lab.writes),1)

    def test_unverified_move_or_missing_parent_refuses(self):
        run=self.moves.get(self.move['plan_id'])['execution']
        run['state']='stopped_unknown';self.s._save('address-run',run,run['execution_id'])
        with self.assertRaisesRegex(ValueError,'verified address'):self.prepare()

    def test_empty_label_and_missing_marker_are_explicit_noop(self):
        d=fixture();d['zCh2']['addresses']['0']['name']='';del d['floorPlan'];self.path.write_bytes(canonical(d))
        p=self.prepare();self.assertEqual(p['review']['skipped'],['source_name_empty','floorplan_absent'])
        self.assertEqual(self.apply(p)['state'],'already_matches')

    def test_layout_revision_preserves_old_grid_and_clears_claim(self):
        layout=self.s.create_layout('board',2,[[0,None],[1,7]])
        checked=self.s.record_layout_check(layout['layout_id'],layout['digest'],[0,1,7],'checked test','tester')
        r=self.c.migrate_layout(self.move['plan_id'],checked['layout_id']);new=r['layout']
        self.assertEqual(new['address_grid'],[[63,None],[1,7]]);self.assertFalse(new['physical_mapping_verified'])
        self.assertEqual(self.s.get_layout(checked['layout_id'])['address_grid'],[[0,None],[1,7]])
        self.assertEqual(len(self.lab.writes),1)

    def test_layout_collision_or_channel_mismatch_refused(self):
        for ch,grid in [(2,[[0,63]]),(1,[[0,1]]),(2,[[1,7]])]:
            p=self.s.create_layout('bad',ch,grid)
            with self.assertRaises(ValueError):self.c.migrate_layout(self.move['plan_id'],p['layout_id'])

    def test_restored_bus_refuses_new_grid_revision(self):
        p=self.s.create_layout('board',2,[[0,1]]);self.undo_bus()
        with self.assertRaisesRegex(ValueError,'addresses'):self.c.migrate_layout(self.move['plan_id'],p['layout_id'])

    def test_rehashed_unrelated_draft_change_rejected(self):
        p=self.prepare();after_path=self.c._record_path('after',p['plan_id'],'bin')
        d=parse(after_path.read_bytes());d['devName']='unrelated';changed=canonical(d);after_path.write_bytes(changed)
        plan_path=self.c._record_path('plan',p['plan_id']);plan=json.loads(plan_path.read_bytes())
        from dali_mcp.native_file import digest
        plan['after_sha256']=digest(changed);plan['digest']=self.c._plan_digest(plan);plan_path.write_bytes(canonical(plan))
        with self.assertRaisesRegex(ValueError,'unselected'):self.c.get(p['plan_id'])


if __name__=='__main__':unittest.main()
