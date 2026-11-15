import copy
import unittest
from dali_mcp.parameter_planning import encode_parameter_candidate,plan_parameter_candidates


def evidence():
    values={'light_type':{'code':1,'name':'DT8'},'status':{'raw':0},
            'fade_time_rate':{'raw':0x47,'fade_time_code':4,'fade_rate_code':7,'extended_fade_not_read':False},
            'min_level':{'level_raw':3},'max_level':{'level_raw':240},
            'power_on':{'level_raw':0,'kelvin_raw':65535},'system_failure':{'level_raw':229,'kelvin_raw':65535},
            'dimming_curve':{'raw':0},'configured_min_ct':{'kelvin_raw':2702},
            'configured_max_ct':{'kelvin_raw':6535},'groups':{'groups':[0,15],'group_mask':32769}}
    return {key:{'value':value} for key,value in values.items()}


class ParameterPlanningTests(unittest.TestCase):
    def plan(self,changes,parameters=None):
        return plan_parameter_candidates(2,63,{'channel':2,'address':63,'parameters':parameters or evidence()},changes)

    def test_eight_payload_layouts_from_observed_callers(self):
        cases=[('fade_time',{'code':5},'F018','02013f2e05000000'),
               ('fade_rate',{'code':7},'F019','02013f2f07000000'),
               ('min_level',{'level_raw':2},'F01C','02013f2b02000000'),
               ('max_level',{'level_raw':254},'F01E','02013f2afe000000'),
               ('power_on',{'level_raw':229,'kelvin_raw':3000},'F020','02013f01e5b80b00'),
               ('system_failure',{'level_raw':255,'kelvin_raw':65535},'F022','02013f01ffffff00'),
               ('configured_min_ct',{'kelvin_raw':3000},'F028','02013ff2b80b0000'),
               ('configured_max_ct',{'kelvin_raw':6000},'F026','02013ff270170000')]
        for field,values,command,payload in cases:
            with self.subTest(field=field):
                result=encode_parameter_candidate(2,63,field,values)
                self.assertEqual(result['command'],command);self.assertEqual(result['payload_hex'],payload)
                self.assertEqual(result['declared_length_including_crc'],12)

    def test_only_explicit_short_addresses_strict_ranges(self):
        for ch,address in [(True,0),(2,True),(2,64),(0,0),(2,255)]:
            with self.assertRaises(ValueError):encode_parameter_candidate(ch,address,'fade_time',{'code':4})
        for field,values in [('fade_time',{'code':16}),('min_level',{'level_raw':0}),('max_level',{'level_raw':255}),
                             ('power_on',{'level_raw':1}),('power_on',{'level_raw':True,'kelvin_raw':65535}),
                             ('configured_min_ct',{'kelvin_raw':65535}),('groups',{'group':1})]:
            with self.assertRaises(ValueError):encode_parameter_candidate(2,0,field,values)

    def test_paired_field_partial_patch_preserves_original_component(self):
        p=self.plan([{'field':'system_failure','values':{'level_raw':195}}])
        self.assertEqual(p['steps'][0]['after'],{'level_raw':195,'kelvin_raw':65535})
        self.assertEqual(p['restore_candidates'][0]['target'],{'level_raw':229,'kelvin_raw':65535})
        self.assertFalse(p['device_writes'] or p['write_execution_available'])

    def test_fade_single_nibble_and_restoration_evidence(self):
        p=self.plan([{'field':'fade_time','values':{'code':5}}])
        self.assertEqual(p['steps'][0]['before'],{'code':4})
        self.assertEqual(p['steps'][0]['readback_field'],'fade_time_rate')
        self.assertEqual(p['restore_candidates'][0]['payload_hex'],'02013f2e04000000')
        self.assertEqual(p['source_parameters']['fade_time_rate']['value']['fade_rate_code'],7)

    def test_raising_range_orders_max_before_min(self):
        p=self.plan([{'field':'min_level','values':{'level_raw':245}},{'field':'max_level','values':{'level_raw':250}}])
        self.assertEqual([s['field'] for s in p['steps']],['max_level','min_level'])
        self.assertEqual([s['field'] for s in p['restore_candidates']],['min_level','max_level'])

    def test_lowering_range_orders_min_before_max(self):
        p=self.plan([{'field':'max_level','values':{'level_raw':2}},{'field':'min_level','values':{'level_raw':1}}])
        self.assertEqual([s['field'] for s in p['steps']],['min_level','max_level'])
        with self.assertRaises(ValueError):self.plan([{'field':'min_level','values':{'level_raw':245}}])

    def test_ct_narrowing_only_and_explicit_target_limits(self):
        p=self.plan([{'field':'configured_min_ct','values':{'kelvin_raw':3000}},
                     {'field':'configured_max_ct','values':{'kelvin_raw':6000}}])
        self.assertEqual(len(p['steps']),2)
        with self.assertRaisesRegex(ValueError,'physical-limit'):
            self.plan([{'field':'configured_max_ct','values':{'kelvin_raw':7000}}])
        with self.assertRaisesRegex(ValueError,'outside target'):
            self.plan([{'field':'power_on','values':{'kelvin_raw':2600}}])

    def test_unchanged_omitted_no_mutation_or_implicit_changes(self):
        source=evidence();saved=copy.deepcopy(source)
        p=self.plan([{'field':'fade_rate','values':{'code':7}}],source)
        self.assertEqual(p['steps'],[]);self.assertEqual(p['restore_candidates'],[])
        self.assertEqual(p['unchanged_fields'],['fade_rate']);self.assertEqual(source,saved)

    def test_unknown_type_incomplete_and_unrestorable_original_refused(self):
        with self.assertRaisesRegex(ValueError,'different channel/address'):
            plan_parameter_candidates(2,0,{'channel':2,'address':63,'parameters':evidence()},[{'field':'fade_time','values':{'code':5}}])
        source=evidence();source.pop('groups')
        with self.assertRaises(ValueError):self.plan([{'field':'fade_time','values':{'code':5}}],source)
        source=evidence();source['light_type']['value']={'code':0,'name':'DT6'}
        with self.assertRaises(ValueError):self.plan([{'field':'fade_time','values':{'code':5}}],source)
        source=evidence();source['power_on']['value']['kelvin_raw']=0
        with self.assertRaises(ValueError):self.plan([{'field':'power_on','values':{'kelvin_raw':4000}}],source)

    def test_duplicate_coercion_and_extra_fields_refused(self):
        for changes in [[],[{'field':'fade_time','values':{'code':'5'}}],
                        [{'field':'fade_time','values':{'code':True}}],
                        [{'field':'fade_time','values':{'code':4,'unexpected':0}}],
                        [{'field':'fade_time','values':{'code':4},'force':True}],
                        [{'field':'fade_time','values':{'code':4}}]*2]:
            with self.assertRaises(ValueError):self.plan(changes)


if __name__=='__main__':unittest.main()
