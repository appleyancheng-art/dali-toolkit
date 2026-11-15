"""Offline group membership proposals. No transport or execution entry point.

F015 layout is confirmed from ConfigMaster sendNextGroupNum and its wrapper.
Physical write acknowledgements and timing remain unvalidated.
"""
import copy


def _integer(value, low, high, label):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f'{label} requires a strict integer {low}..{high}')


def _groups(values):
    if not isinstance(values, list):
        raise ValueError('groups must be an explicit list')
    for value in values:
        _integer(value, 0, 15, 'group')
    if len(values) != len(set(values)):
        raise ValueError('duplicate groups')
    return sorted(values)


def _snapshots(channel, rows):
    _integer(channel, 1, 2, 'channel')
    if not isinstance(rows, list) or not 1 <= len(rows) <= 64:
        raise ValueError('provide 1..64 complete membership snapshots')
    result = {}
    for row in rows:
        if not isinstance(row, dict) or set(row) != {'channel', 'address', 'groups', 'group_mask'}:
            raise ValueError('snapshot requires channel, address, groups and group_mask')
        _integer(row['channel'], 1, 2, 'snapshot channel')
        _integer(row['address'], 0, 63, 'address')
        _integer(row['group_mask'], 0, 65535, 'group mask')
        groups = _groups(row['groups'])
        if row['channel'] != channel or row['address'] in result:
            raise ValueError('snapshot channel mismatch or duplicate address')
        if sum(1 << group for group in groups) != row['group_mask']:
            raise ValueError('group list and mask disagree')
        result[row['address']] = dict(channel=channel, address=row['address'],
                                      groups=groups, group_mask=row['group_mask'])
    return result


def encode_group_candidate(channel, address, group, member):
    _integer(channel, 1, 2, 'channel')
    _integer(address, 0, 63, 'address')
    _integer(group, 0, 15, 'group')
    if type(member) is not bool:
        raise ValueError('member requires an explicit boolean')
    return {'command': 'F015',
            'payload_hex': bytes([channel, 1, address, 0x60 if member else 0x70,
                                  group, 0, 0, 0]).hex(),
            'declared_length_including_crc': 12,
            'addressing_mode': 'single_short_address'}


def plan_group_candidates(channel, originals, changes):
    """Apply explicit add/remove deltas, preserving every other membership bit.

    Caller must obtain fresh complete snapshots before future execution; this
    pure function cannot establish freshness, identity or user authorization.
    Remove-before-add ordering avoids temporary additional memberships, but can
    leave temporary gaps. No plan is automatically executed or restored.
    """
    before = _snapshots(channel, originals)
    if not isinstance(changes, list) or not 1 <= len(changes) <= 64:
        raise ValueError('provide 1..64 explicit address edits')
    edits = {}
    for change in changes:
        if not isinstance(change, dict) or set(change) != {'address', 'add', 'remove'}:
            raise ValueError('edit requires address, add and remove')
        address = change['address']
        _integer(address, 0, 63, 'address')
        add, remove = _groups(change['add']), _groups(change['remove'])
        if address in edits or set(add) & set(remove) or not (add or remove):
            raise ValueError('duplicate address, overlapping edits or empty edit')
        edits[address] = dict(address=address, add=add, remove=remove)
    if set(edits) != set(before):
        raise ValueError('snapshots must exactly cover the edited addresses')
    steps, after, unchanged = [], [], []
    for address in sorted(edits):
        edit = edits[address]
        mask = before[address]['group_mask']
        target = (mask & ~sum(1 << g for g in edit['remove'])) | sum(1 << g for g in edit['add'])
        for member, groups in ((False, edit['remove']), (True, edit['add'])):
            for group in groups:
                if bool(mask & (1 << group)) == member:
                    unchanged.append(dict(address=address, group=group, member=member))
                    continue
                next_mask = mask | (1 << group) if member else mask & ~(1 << group)
                steps.append(dict(address=address, group=group, member=member,
                                  before_mask=mask, after_mask=next_mask,
                                  **encode_group_candidate(channel, address, group, member)))
                mask = next_mask
        assert mask == target
        after.append(dict(channel=channel, address=address, group_mask=target,
                          groups=[g for g in range(16) if target & (1 << g)]))
    return dict(format='dali-group-candidate-v1', channel=channel,
                originals=[copy.deepcopy(before[a]) for a in sorted(before)],
                changes=[edits[a] for a in sorted(edits)], targets=after,
                steps=steps, unchanged=unchanged, device_writes=False,
                write_execution_available=False,
                evidence_scope='offline encoding only; fresh identity and membership required before execution')


def plan_group_restore_candidates(proposal, current):
    """Restore only from a fresh state matching an exact sequential prefix.

    Any unrelated bit change or impossible mixed progress is a conflict. Stored
    proposal payloads are regenerated; they are never accepted as executable
    input. This check detects accidental alteration, not malicious provenance.
    """
    if not isinstance(proposal, dict):
        raise ValueError('group proposal required')
    try:
        canonical = plan_group_candidates(proposal['channel'], proposal['originals'], proposal['changes'])
    except (KeyError, TypeError) as exc:
        raise ValueError('invalid group proposal') from exc
    if proposal != canonical:
        raise ValueError('proposal differs from regenerated original intent')
    channel = canonical['channel']
    observed = _snapshots(channel, current)
    initial = {r['address']: r['group_mask'] for r in canonical['originals']}
    actual = {a: r['group_mask'] for a, r in observed.items()}
    if set(initial) != set(actual):
        raise ValueError('restoration scope differs from original scope')
    reachable = dict(initial)
    prefix = 0 if reachable == actual else None
    for index, step in enumerate(canonical['steps'], 1):
        reachable[step['address']] = step['after_mask']
        if reachable == actual:
            prefix = index
    if prefix is None:
        raise ValueError('membership conflict: current state is not an expected execution prefix')
    changes = []
    rows = []
    for address, mask in sorted(actual.items()):
        original = initial[address]
        if mask == original:
            continue
        rows.append(observed[address])
        changes.append(dict(address=address,
                            add=[g for g in range(16) if original & ~mask & (1 << g)],
                            remove=[g for g in range(16) if mask & ~original & (1 << g)]))
    restore = (plan_group_candidates(channel, rows, changes) if changes else None)
    return dict(state='restore_candidate' if changes else 'already_original',
                matched_prefix_steps=prefix, proposal=restore,
                device_writes=False, write_execution_available=False)
