"""Synthetic checks for bounded ground-context audit; no network/data files."""
import unittest

import numpy as np
import pandas as pd

from data_processing.check_impact_space_weather import ground_descriptors


class GroundDescriptorTests(unittest.TestCase):
    def test_horizontal_change_preserves_frames(self):
        times = pd.date_range('2024-07-08T18:15Z', periods=3, freq='min')
        frame = pd.DataFrame({'N': [0., 3., 3.], 'E': [0., 4., 4.],
                              'geox': [0., 6., 6.], 'geoy': [0., 8., 8.]}, index=times)
        result = ground_descriptors(frame, times[0], times[-1])
        self.assertEqual(result['magnetic_horizontal_step_max_nT_per_min'], 5)
        self.assertEqual(result['geographic_horizontal_step_max_nT_per_min'], 10)
        self.assertEqual(result['magnetic_valid_pairs'], 2)

    def test_no_change_across_missing_minute(self):
        times = pd.date_range('2024-07-08T18:15Z', periods=3, freq='min')
        frame = pd.DataFrame({name: [0., 100.] for name in ['N', 'E', 'geox', 'geoy']},
                              index=times[[0, 2]])
        result = ground_descriptors(frame, times[0], times[-1])
        self.assertEqual(result['expected_minutes'], 3)
        self.assertEqual(result['magnetic_valid_pairs'], 0)
        self.assertTrue(np.isnan(result['magnetic_horizontal_step_max_nT_per_min']))
        self.assertEqual(result['coverage_status'], 'insufficient_support')

    def test_constant_baseline_does_not_change_metrics(self):
        times = pd.date_range('2024-07-08T18:15Z', periods=3, freq='min')
        frame = pd.DataFrame({name: [0., 3., 4.] for name in ['N', 'E', 'geox', 'geoy']}, index=times)
        first = ground_descriptors(frame, times[0], times[-1])
        second = ground_descriptors(frame + 20000, times[0], times[-1])
        self.assertEqual(first, second)


if __name__ == '__main__':
    unittest.main()