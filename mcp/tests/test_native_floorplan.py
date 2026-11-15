import copy
import unittest

from dali_mcp.native_floorplan import inspect_native_floorplan
from test_native_cache import document


def sample():
    d = document()
    d['floorPlan'] = dict(image='02-00-00-00-00-01.png', marks=[
        dict(ch=2, addr=0, x=0.06812933025404158, y=0.06158583525789069),
        dict(ch=2, addr=1, x=0.1836027713625866, y=0.1924557351809084)])
    return d


class NativeFloorplanTests(unittest.TestCase):
    def test_observed_coordinates_are_preserved_without_grid_or_live_claims(self):
        d = sample(); r = inspect_native_floorplan(d)
        self.assertEqual(r['marks'][0]['x'], d['floorPlan']['marks'][0]['x'])
        self.assertEqual([(m['channel'],m['address']) for m in r['marks']],[(2,0),(2,1)])
        self.assertFalse(r['physical_positions_verified'] or r['online_addresses_verified'])
        self.assertFalse(r['image_bytes_loaded'] or r['layout_created'])

    def test_absent_empty_and_invalid_present_are_distinct(self):
        self.assertFalse(inspect_native_floorplan(document())['present'])
        d = sample(); d['floorPlan']['marks'] = []
        self.assertEqual(inspect_native_floorplan(d)['mark_count'], 0)
        for p in (None, [], {}, {'image':'a.png'}):
            d['floorPlan'] = p
            with self.assertRaises(ValueError): inspect_native_floorplan(d)

    def test_duplicate_pair_rejected_but_same_address_other_channel_allowed(self):
        d=sample();d['floorPlan']['marks'].append(copy.deepcopy(d['floorPlan']['marks'][0]))
        with self.assertRaisesRegex(ValueError,'duplicate'): inspect_native_floorplan(d)
        d['floorPlan']['marks'][-1]['ch']=1
        self.assertEqual(inspect_native_floorplan(d)['mark_count'],3)

    def test_invalid_channel_and_address(self):
        for key,values in [('ch',[0,3,True,'2']),('addr',[-1,64,False,'0'])]:
            for v in values:
                d=sample();d['floorPlan']['marks'][0][key]=v
                with self.assertRaises(ValueError): inspect_native_floorplan(d)

    def test_finite_coordinates_only(self):
        for v in (None,True,'0.5',float('nan'),float('inf'),10**400):
            d=sample();d['floorPlan']['marks'][0]['x']=v
            with self.assertRaises(ValueError): inspect_native_floorplan(d)

    def test_outside_image_coordinates_are_flagged_not_clamped(self):
        d=sample();d['floorPlan']['marks'][0].update(x=-0.1,y=1.2)
        r=inspect_native_floorplan(d)
        self.assertEqual(r['marks'][0]['x'],-0.1)
        self.assertFalse(r['marks'][0]['within_image_bounds'])

    def test_path_references_are_not_accepted(self):
        for name in ('../a.png','C:\\a.png','https://host/a.png','a.png:stream',
                     'CON.png','LPT1.png','a.png.','a.png ','x\0.png','', 'a.jpg'):
            d=sample();d['floorPlan']['image']=name
            with self.assertRaises(ValueError): inspect_native_floorplan(d)

    def test_unknown_metadata_and_empty_address_cache_preserved(self):
        d=sample();d['floorPlan']['future']={'label':'房间'}
        d['zCh2']['addresses']={}
        r=inspect_native_floorplan(d)
        self.assertEqual(r['mark_count'],2)
        self.assertEqual(r['native_floorplan'],d['floorPlan'])
        d['floorPlan']['future']['label']='changed'
        self.assertEqual(r['native_floorplan']['future']['label'],'房间')

    def test_identity_version_and_size_are_validated(self):
        for field,value in [('version','1.2.5'),('dalimac','bad'),('extra','x'*2_000_001)]:
            d=sample();d[field]=value
            with self.assertRaises(ValueError): inspect_native_floorplan(d)
        d=sample();d['floorPlan']['marks'] *= 65
        with self.assertRaises(ValueError): inspect_native_floorplan(d)


if __name__ == '__main__': unittest.main()
