"""Project-to-group integration, including the real group transaction executor."""
import copy
import tempfile
import unittest
from unittest.mock import patch

from dali_mcp.group_execution import ExperimentalGroupController
from dali_mcp.group_planning import plan_group_candidates
from dali_mcp.projects import digest
from dali_mcp.service import Service
from test_group_execution import GroupLab


class ProjectGroupLab(GroupLab):
    def read_lamp_parameter(self, channel, address, field, typ):
        if field == 'groups':
            row = self.read_groups(channel, address)
            value = {k: row[k] for k in ('groups', 'group_mask')}
        elif field == 'light_type':
            typ = self.types[address]
            value = dict(code=typ, name='DT8' if typ else 'DT6')
        elif field == 'fade_time_rate':
            value = dict(raw=0x47, fade_time_code=4, fade_rate_code=7, extended_fade_not_read=False)
        elif field in ('min_level', 'max_level'):
            value = dict(level_raw=2 if field == 'min_level' else 254)
        elif field in ('power_on', 'system_failure'):
            value = dict(level_raw=0)
            if typ: value['kelvin_raw'] = 65535
        elif field.startswith('configured_'):
            if not typ: return dict(state='not_applicable')
            value = dict(kelvin_raw=2702 if field == 'configured_min_ct' else 6535)
        else:
            value = dict(raw=0)
        return dict(value=value)

    def read_scene(self, channel, address, scene, typ):
        return dict(channel=channel, address=address, scene=scene,
                    light_type_code=typ, level_raw=255, kelvin_raw=65535)


class ProjectGroupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.g = ProjectGroupLab()
        self.info = dict(ip='127.0.0.1', dali_mac='02:00:00:00:00:01', dali_firmware='test')
        self.config = dict(gateway_ip=self.info['ip'], expected_dali_mac=self.info['dali_mac'],
                           expected_firmware=self.info['dali_firmware'])
        self.service = Service(self.config, self.tmp.name)
        self.controller = ExperimentalGroupController(self.service)
        self.opens = 0
        self.open_patch = patch.object(self.service, '_open', self.open)
        self.open_patch.start()
        self.document = self.service.capture_project('group originals', 2, [0, 63], [0])
        self.assertEqual(self.document['state'], 'completed')
        self.opens = 0

    def tearDown(self):
        self.open_patch.stop()
        self.tmp.cleanup()

    def open(self):
        self.opens += 1
        return self.g, copy.deepcopy(self.info)

    def prepare(self, addresses=None, groups=None, project_id=None):
        return self.controller.prepare_project_restore(
            project_id or self.document['project_id'],
            [0, 63] if addresses is None else addresses, [1, 15] if groups is None else groups)

    def allow(self, plan):
        self.config.update(enable_experimental_group_writes=True,
            allowed_group_write_operations=[dict(channel=2,
                **{k: step[k] for k in ('address', 'group', 'member')}) for step in plan['proposal']['steps']])

    def execute(self, plan):
        return self.controller.execute(plan['plan_id'], plan['confirmation'])

    def test_matching_selected_bits_no_plan_no_writes_no_scope_expansion(self):
        self.g.masks[0] |= 1 << 7  # Outside the explicitly selected bits.
        result = self.prepare()
        self.assertEqual(result['state'], 'already_matches')
        self.assertEqual(result['proposal']['steps'], [])
        self.assertNotIn('plan_id', result)
        self.assertEqual(list(self.service.state_dir.glob('group-plan-*.json')), [])
        self.assertEqual([r['membership']['address'] for r in result['baseline']], [0, 63])
        self.assertEqual(self.g.writes, [])

    def test_selected_bits_apply_and_undo_preserve_unselected_members_and_addresses(self):
        self.g.masks[0] = 1 | (1 << 7) | (1 << 15)
        self.g.levels[0] = 123
        before = copy.deepcopy(self.g.masks)
        plan = self.prepare()
        self.assertEqual([(s['address'], s['group'], s['member']) for s in plan['proposal']['steps']],
                         [(0, 15, False), (0, 1, True)])
        self.assertEqual(plan['source_project']['digest'], self.document['digest'])
        self.assertEqual(len(plan['baseline']), 2)
        self.assertEqual(self.g.writes, [])
        with self.assertRaises(PermissionError): self.execute(plan)
        self.allow(plan)
        self.assertEqual(self.execute(plan)['state'], 'completed_readback')
        self.assertEqual(self.g.masks, {0: 131, 63: 3, 1: 5})
        self.assertEqual(self.g.levels[0], 123)
        undo = self.controller.prepare_restore(plan['plan_id'])
        self.assertEqual([r['membership']['address'] for r in undo['baseline']], [0, 63])
        with self.assertRaises(PermissionError): self.execute(undo)
        self.allow(undo)
        self.assertEqual(self.execute(undo)['state'], 'completed_readback')
        self.assertEqual(self.g.masks, before)
        self.assertEqual(self.g.levels[0], 123)

    def test_changed_state_after_preview_stops_before_any_write(self):
        self.g.masks[0] |= 1 << 15
        plan = self.prepare()
        self.allow(plan)
        self.g.masks[63] ^= 1 << 9  # Even an unselected bit cannot drift after preview.
        result = self.execute(plan)
        self.assertEqual(result['state'], 'preflight_failed')
        self.assertEqual(self.g.writes, [])

    def test_unknown_receipt_stops_and_undo_preserves_unchanged_selected_addresses(self):
        self.g.masks[0] = 1 | (1 << 15)
        before = copy.deepcopy(self.g.masks)
        plan = self.prepare()
        self.allow(plan)
        self.g.fault = 'lost'
        result = self.execute(plan)
        self.assertEqual(result['state'], 'stopped_receipt_unknown')
        self.assertEqual(len(self.g.writes), 1)
        self.g.fault = None
        undo = self.controller.prepare_restore(plan['plan_id'])
        self.allow(undo)
        self.assertEqual(self.execute(undo)['state'], 'completed_readback')
        self.assertEqual(self.g.masks, before)
        self.assertEqual(len(self.g.writes), 2)

    def test_all64_all16_explicit_selection_not_truncated(self):
        self.g.masks = {a: 1 << (a % 16) for a in range(64)}
        self.g.levels = {a: 0 for a in range(64)}
        self.g.types = {a: 1 for a in range(64)}
        document = self.service.capture_project('all groups', 2, list(range(64)), [15])
        self.assertEqual(document['state'], 'completed')
        self.g.masks = {a: mask ^ 65535 for a, mask in self.g.masks.items()}
        plan = self.prepare(list(reversed(range(64))), list(reversed(range(16))), document['project_id'])
        self.assertEqual(len(plan['proposal']['steps']), 1024)
        self.assertEqual({(s['address'], s['group']) for s in plan['proposal']['steps']},
                         {(a, g) for a in range(64) for g in range(16)})
        self.assertEqual([r['group_mask'] for r in plan['proposal']['targets']], [1 << (a % 16) for a in range(64)])
        self.assertEqual(self.g.writes, [])

    def test_strict_scope_rejected_before_gateway_open(self):
        for addresses, groups in [([], [1]), ([True], [1]), ([0, 0], [1]), ([64], [1]),
                                   ([0], []), ([0], [False]), ([0], [16]), ([0], [1, 1]),
                                   ([1], [1]), ('0', [1]), ([0], '1')]:
            with self.assertRaises(ValueError): self.prepare(addresses, groups)
        self.assertEqual(self.opens, 0)

    def test_partial_corrupt_and_foreign_projects_rejected_before_network(self):
        path = self.service.state_dir / f"project-{self.document['project_id']}.json"
        partial = copy.deepcopy(self.document)
        partial['state'] = 'stopped_partial'
        self.service._save('project', partial, partial['project_id'])
        with self.assertRaises(ValueError): self.prepare()
        corrupt = copy.deepcopy(self.document)
        corrupt['name'] = 'corrupted'
        self.service._save('project', corrupt, corrupt['project_id'])
        with self.assertRaises(ValueError): self.prepare()
        self.service._save('project', self.document, self.document['project_id'])
        for key, value in [('ip', '192.0.2.2'), ('dali_mac', '02:00:00:00:00:ff'), ('dali_firmware', 'other')]:
            foreign = copy.deepcopy(self.document)
            foreign['gateway'][key] = value
            foreign['digest'] = digest(foreign)
            ident = self.service.import_project(foreign)['project_id']
            with self.assertRaises(ValueError): self.prepare(project_id=ident)
        self.assertTrue(path.exists())
        self.assertEqual(self.opens, 0)

    def test_fixture_type_change_and_read_failure_create_no_plan(self):
        self.g.types[63] = 0
        with self.assertRaisesRegex(ValueError, 'fixture type'): self.prepare()
        self.g.types[63] = 1
        with patch.object(self.g, 'read_groups', side_effect=TimeoutError('unavailable')):
            with self.assertRaises(TimeoutError): self.prepare()
        self.assertEqual(list(self.service.state_dir.glob('group-plan-*.json')), [])
        self.assertEqual(self.g.writes, [])

    def test_dt6_group_restore_ignores_undefined_color_without_changing_type(self):
        self.g.types[0] = 0
        document = self.service.capture_project('DT6', 2, [0], [0])
        self.g.masks[0] = 1
        plan = self.prepare([0], [1], document['project_id'])
        self.allow(plan)
        self.assertEqual(self.execute(plan)['state'], 'completed_readback')
        self.assertEqual(self.g.masks[0], 3)
        self.assertEqual(self.g.types[0], 0)

    def test_source_digest_change_invalidates_plan_before_write(self):
        self.g.masks[0] |= 1 << 15
        plan = self.prepare()
        self.allow(plan)
        edited = copy.deepcopy(self.document)
        edited['name'] = 'rewritten archive'
        edited['digest'] = digest(edited)
        self.service._save('project', edited, edited['project_id'])
        count = self.opens
        with self.assertRaisesRegex(ValueError, 'source'): self.execute(plan)
        self.assertEqual(self.opens, count)
        self.assertEqual(self.g.writes, [])

    def test_rehashed_intent_change_cannot_escape_selected_archived_bits(self):
        self.g.masks[0] |= 1 << 15
        plan = self.prepare([0], [1, 15])
        forged = copy.deepcopy(plan)
        forged.pop('confirmation')
        forged['changes'][0]['add'].append(7)
        forged['proposal'] = plan_group_candidates(2,
            [r['membership'] for r in forged['baseline']], forged['changes'])
        forged['digest'] = self.controller._digest(forged)
        self.service._save('group-plan', forged, forged['plan_id'])
        self.allow(forged)
        count = self.opens
        with self.assertRaisesRegex(ValueError, 'intent'):
            self.controller.execute(forged['plan_id'], self.controller._confirmation(forged))
        self.assertEqual(self.opens, count)
        self.assertEqual(self.g.writes, [])

    def test_imported_archive_plan_survives_restart_and_retains_source(self):
        imported = self.service.import_project(self.document)
        self.g.masks[0] |= 1 << 15
        plan = self.prepare([0], [15], imported['project_id'])
        reopened = ExperimentalGroupController(Service(self.config, self.tmp.name))
        loaded = reopened.get(plan['plan_id'])
        self.assertEqual(loaded['source_project'], plan['source_project'])
        self.assertEqual(loaded['confirmation'], plan['confirmation'])
        self.assertEqual(loaded['proposal'], plan['proposal'])
        self.assertEqual(self.g.writes, [])


if __name__ == '__main__': unittest.main()
