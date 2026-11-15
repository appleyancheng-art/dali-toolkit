"""Inspect observed ConfigMaster 1.2.4.1 floorPlan JSON, without file/bus I/O.

Evidence: FloorPlanWidget::onSave stores marker pos divided by image dimensions;
the 0.31 local UI trial saved and reopened two channel-2 markers. A reference to
an image is not an imported image, verified physical placement, or grid layout.
This parser is intentionally independent of legacy cache summary checksums.
"""
import copy
import hashlib
import math
import re

from .native_cache import canonical, inspect_configmaster_cache


def inspect_native_floorplan(document):
    cache = inspect_configmaster_cache(document)
    result = dict(format='configmaster-floorplan-evidence-v1',
                  native_pc_version=cache['native_pc_version'],
                  gateway=copy.deepcopy(cache['gateway']),
                  present='floorPlan' in document,
                  trust='local_cache_unverified',
                  physical_positions_verified=False,
                  online_addresses_verified=False,
                  image_bytes_loaded=False,
                  layout_created=False,
                  coordinate_system='image_width_height_fractions')
    if not result['present']:
        return result
    plan = document['floorPlan']
    if not isinstance(plan, dict):
        raise ValueError('floorPlan must be an object when present')
    image = plan.get('image')
    # Observed native output is a PNG leaf name. Never resolve user-supplied
    # paths, fetch URLs, or infer a local directory from an imported document.
    if (type(image) is not str or not image or len(image.encode('utf-8')) > 255
            or re.search(r'[\\/:*?"<>|\x00-\x1f\x7f]', image)
            or image in ('.', '..') or image.endswith((' ', '.'))
            or not image.lower().endswith('.png')):
        raise ValueError('floorPlan image must be a PNG leaf filename')
    if re.fullmatch(r'(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])', image.split('.')[0], re.I):
        raise ValueError('reserved image filename')
    marks = plan.get('marks')
    if not isinstance(marks, list) or len(marks) > 128:
        raise ValueError('floorPlan marks must contain at most 128 entries')
    seen = set()
    parsed = []
    for mark in marks:
        if not isinstance(mark, dict):
            raise ValueError('invalid floorPlan mark')
        channel, address = mark.get('ch'), mark.get('addr')
        if type(channel) is not int or channel not in (1, 2):
            raise ValueError('floorPlan ch must be 1 or 2')
        if type(address) is not int or not 0 <= address <= 63:
            raise ValueError('floorPlan addr must be 0..63')
        if (channel, address) in seen:
            raise ValueError('duplicate floorPlan channel/address')
        seen.add((channel, address))
        xy = [mark.get('x'), mark.get('y')]
        try:
            finite = all(type(v) in (int, float) and math.isfinite(v) for v in xy)
        except OverflowError:
            finite = False
        if not finite:
            raise ValueError('floorPlan coordinates must be finite numbers')
        parsed.append(dict(channel=channel, address=address, x=xy[0], y=xy[1],
                           within_image_bounds=all(0 <= v <= 1 for v in xy)))
    # Preserve out-of-image coordinates and unknown native fields as evidence;
    # do not silently clamp, guess grid cells, or drop stale cached addresses.
    result.update(image_reference=image, marks=parsed, mark_count=len(parsed),
                  native_floorplan=copy.deepcopy(plan),
                  source_sha256=hashlib.sha256(canonical(plan)).hexdigest())
    return result
