import copy
import random
import unittest
from dali_mcp.group_planning import encode_group_candidate, plan_group_candidates, plan_group_restore_candidates


def row(address, mask, channel=2):
    return dict(channel=channel, address=address, group_mask=mask,
                groups=[g for g in range(16) if mask & (1 << g)])


class GroupPlanningTests(unittest.TestCase):
    def test_literal_wire_boundary_vectors(self):
        self.assertEqual(encode_group_candidate(2, 63, 15, True)['payload_hex'], '02013f600f000000')
        self.assertEqual(encode_group_candidate(1, 0, 0, False)['payload_hex'], '0101007000000000')
        self.assertEqual(encode_group_candidate(2, 0, 8, True)['command'], 'F015')

    def test_strict_scopes_and_membership_flag(self):
        for ch, address, group, member in [(True, 0, 0, True), (2, 64, 0, True),
                (2, True, 0, True), (2, 0, 16, True), (2, 0, '1', True), (2, 0, 0, 1)]:
            with self.subTest(case=(ch,address,group,member)), self.assertRaises(ValueError):
                encode_group_candidate(ch,address,group,member)

    def test_minimal_diff_preserves_other_memberships(self):
        p = plan_group_candidates(2, [row(63, 0x8003)], [{'address':63,'add':[2,15],'remove':[1,3]}])
        self.assertEqual(p['targets'], [row(63, 0x8005)])
        self.assertEqual([(s['group'],s['member']) for s in p['steps']], [(1,False),(2,True)])
        self.assertEqual(len(p['unchanged']), 2)
        self.assertFalse(p['device_writes'] or p['write_execution_available'])

    def test_equal_changes_make_no_candidate_commands(self):
        p = plan_group_candidates(2,[row(0,1)],[{'address':0,'add':[0],'remove':[15]}])
        self.assertEqual(p['steps'], [])
        self.assertEqual(plan_group_restore_candidates(p,[row(0,1)])['state'], 'already_original')

    def test_conflicting_or_missing_snapshots_are_rejected(self):
        edits = [{'address':0,'add':[1],'remove':[]}]
        bad = [[],[row(0,0),row(0,0)],[row(1,0)],[row(0,0),row(1,0)],
               [row(0,0,1)],[dict(row(0,0),groups=[1])],[dict(row(0,0),group_mask=False)],
               [dict(row(0,0),groups=[True])],[dict(row(0,1),groups=[0,0])]]
        for snapshots in bad:
            with self.subTest(snapshots=snapshots), self.assertRaises(ValueError):
                plan_group_candidates(2,snapshots,edits)

    def test_ambiguous_or_extra_edit_fields_are_rejected(self):
        valid = {'address':0,'add':[1],'remove':[]}
        for edits in [[],[valid,valid],[dict(valid,add=[1,1])],[dict(valid,remove=[1])],
                      [dict(valid,add=[])],[dict(valid,add=[16])],[dict(valid,address=True)],
                      [dict(valid,remove=[],mode='broadcast')],[{'address':0,'add':[1]}]]:
            with self.subTest(edits=edits), self.assertRaises(ValueError):
                plan_group_candidates(2,[row(0,0)],edits)

    def test_every_interrupted_prefix_can_restore_exactly(self):
        p = plan_group_candidates(2,[row(0,0x8003),row(63,0x11)],
            [{'address':0,'add':[2,4],'remove':[1]}, {'address':63,'add':[15],'remove':[0,4]}])
        masks = {0:0x8003,63:0x11}
        for index in range(len(p['steps'])+1):
            restore = plan_group_restore_candidates(p,[row(a,m) for a,m in masks.items()])
            self.assertEqual(restore['matched_prefix_steps'],index)
            recovered = dict(masks)
            if restore['proposal']:
                for step in restore['proposal']['steps']:
                    self.assertEqual(recovered[step['address']], step['before_mask'])
                    recovered[step['address']] = step['after_mask']
            self.assertEqual(recovered,{0:0x8003,63:0x11})
            if index < len(p['steps']):
                step = p['steps'][index]; masks[step['address']] = step['after_mask']

    def test_restore_rejects_external_changes_and_impossible_progress(self):
        p = plan_group_candidates(2,[row(0,1),row(63,1)],
            [{'address':0,'add':[1],'remove':[]},{'address':63,'add':[1],'remove':[]}])
        for snapshots in [[row(0,5),row(63,1)],[row(0,1),row(63,3)],[row(0,1)]]:
            with self.assertRaises(ValueError): plan_group_restore_candidates(p,snapshots)

    def test_saved_payload_tampering_and_caller_mutation(self):
        originals, edits = [row(0,1)], [{'address':0,'add':[15],'remove':[]}]
        p = plan_group_candidates(2,originals,edits)
        originals[0]['groups'].append(1); edits[0]['add'].append(2)
        self.assertEqual(p['originals'],[row(0,1)])
        self.assertEqual(p['changes'][0]['add'],[15])
        corrupt = copy.deepcopy(p); corrupt['steps'][0]['payload_hex'] = '00'*8
        with self.assertRaises(ValueError):plan_group_restore_candidates(corrupt,[row(0,0x8001)])

    def test_many_masks_roundtrip_and_group_isolation(self):
        rng = random.Random(810)
        for _ in range(256):
            original, desired = rng.randrange(65536), rng.randrange(65536)
            changes = dict(address=63,add=[g for g in range(16) if desired & (1<<g)],
                           remove=[g for g in range(16) if not desired & (1<<g)])
            p = plan_group_candidates(2,[row(63,original)],[changes])
            self.assertEqual(p['targets'],[row(63,desired)])
            self.assertEqual(len(p['steps']),(original^desired).bit_count())
            restore = plan_group_restore_candidates(p,p['targets'])
            if original != desired:self.assertEqual(restore['proposal']['targets'],[row(63,original)])


if __name__ == '__main__':
    unittest.main()
