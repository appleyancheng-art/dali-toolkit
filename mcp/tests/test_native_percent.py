import unittest
from dali_mcp.native_percent import LOG_PERCENT, review_native_percent


class NativePercentTests(unittest.TestCase):
    def test_low_level_roundtrip_loss_is_exposed(self):
        r=review_native_percent(.1)
        self.assertEqual(r['exact_export_candidates'],list(range(1,16)))
        self.assertEqual(r['native_import_level_raw'],1)
        self.assertFalse(r['original_raw_level_uniquely_recoverable'])
        self.assertFalse(r['native_import_runtime_verified'])

    def test_known_native_samples_and_endpoints(self):
        self.assertEqual(len(LOG_PERCENT),255)
        for percent,raw in [(0,0),(100,254),(20,195),(51,229)]:
            self.assertEqual(review_native_percent(percent)['native_import_level_raw'],raw)
        self.assertEqual(LOG_PERCENT[2],.1)

    def test_nearest_table_lookup_is_not_log_formula(self):
        r=review_native_percent(.11)
        self.assertEqual(r['native_import_level_raw'],1)
        self.assertEqual(r['exact_export_candidates'],[])
        self.assertFalse(r['original_raw_level_uniquely_recoverable'])

    def test_every_native_display_roundtrips_to_first_equal_level(self):
        for raw,percent in enumerate(LOG_PERCENT):
            r=review_native_percent(percent)
            self.assertEqual(r['native_import_level_raw'],LOG_PERCENT.index(percent))
            self.assertIn(raw,r['exact_export_candidates'])

    def test_invalid_input_and_mask_require_separate_handling(self):
        for value in (True,None,'MASK',float('nan'),float('inf'),-.1,100.1):
            with self.assertRaises(ValueError):review_native_percent(value)


if __name__=='__main__':unittest.main()
