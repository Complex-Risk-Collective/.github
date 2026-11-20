"""Offline acquisition regression tests: no network or user cache writes."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pandas as pd

from data_processing.omni_data import (
    _clean_values, _month_file, fetch_omni_for_days, fetch_omni_hro2_1min,
    omni_coverage, read_omni_cdf, utc_bounds,
)
from data_processing.supermag_data import (
    fetch_supermag_indices, fetch_supermag_station, normalize_supermag_response,
    read_supermag_file,
)
from data_processing.get_r01_data import get_r01_data


class AcquisitionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.records = [{'tval': 1719792000.0, 'iaga': 'R01', 'ext': 60,
                         'N': {'nez': 1, 'geo': 2},
                         'E': {'nez': 999999, 'geo': 4},
                         'Z': {'nez': 5, 'geo': 6}, 'glat': 38.89}]

    def client(self, records=None):
        calls = []

        def get(*args):
            calls.append(args)
            return 1, pd.DataFrame(self.records if records is None else records)
        return SimpleNamespace(SuperMAGGetData=get, SuperMAGGetIndices=get), calls

    def test_nested_coordinate_frames_and_fill(self):
        frame = normalize_supermag_response(self.records, station='R01')
        self.assertEqual(frame.iloc[0]['N'], 1)
        self.assertEqual(frame.iloc[0]['geox'], 2)
        self.assertTrue(np.isnan(frame.iloc[0]['E']))
        self.assertEqual(frame.iloc[0]['geoy'], 4)
        self.assertEqual(str(frame.index.get_level_values('timestamp').tz), 'UTC')

    def test_station_cache_hit_does_not_call_client(self):
        client, calls = self.client()
        first = fetch_supermag_station('r01', '2024-07-01', cache_dir=self.root,
                                        allow_download=True, userid='test-user', api_client=client)
        second = fetch_supermag_station('R01', '2024-07-01', cache_dir=self.root,
                                         api_client=client)
        pd.testing.assert_frame_equal(first, second)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1], '2024-07-01T00:00')
        path = next(self.root.rglob('*.json'))
        self.assertNotIn('test-user', path.read_text())
        self.assertEqual(json.loads(path.read_text())['records'][0]['N']['geo'], 2)

    def test_flags_change_cache_key_and_require_explicit_baseline(self):
        client, _ = self.client()
        fetch_supermag_station('R01', '2024-07-01', cache_dir=self.root,
                               allow_download=True, userid='test', api_client=client)
        with self.assertRaises(FileNotFoundError):
            fetch_supermag_station('R01', '2024-07-01', flags='all,baseline=none', cache_dir=self.root)
        for flags in ['all', 'all,typo,baseline=yearly', 'baseline=none,baseline=yearly']:
            with self.assertRaises(ValueError):
                fetch_supermag_station('R01', '2024-07-01', flags=flags, cache_dir=self.root)

    def test_network_disabled_by_default(self):
        client, calls = self.client()
        with self.assertRaises(FileNotFoundError):
            fetch_supermag_station('R01', '2024-07-01', cache_dir=self.root, api_client=client)
        self.assertEqual(calls, [])
        self.assertFalse((self.root / 'api_cache').exists())

    def test_wrong_station_and_duplicate_and_bounds_rejected(self):
        with self.assertRaises(ValueError):
            normalize_supermag_response(self.records, station='M03')
        with self.assertRaises(ValueError):
            normalize_supermag_response(self.records * 2, station='R01')
        client, _ = self.client()
        with self.assertRaises(ValueError):
            fetch_supermag_station('R01', '2024-07-02', cache_dir=self.root,
                                   allow_download=True, userid='test', api_client=client)
        self.assertFalse(list(self.root.rglob('*.json')))

    def test_failed_request_not_cached_or_identifier_exposed(self):
        def bad(*args):
            raise OSError('https://example.org?userid=sensitive')
        client = SimpleNamespace(SuperMAGGetData=bad)
        with self.assertRaises(RuntimeError) as raised:
            fetch_supermag_station('R01', '2024-07-01', cache_dir=self.root,
                                   allow_download=True, userid='test', api_client=client)
        self.assertNotIn('sensitive', str(raised.exception))
        self.assertFalse(list(self.root.rglob('*.json')))

    def test_indices_cache(self):
        client, calls = self.client([{'tval': 1719792000.0, 'SME': 88, 'SMR': 999999}])
        frame = fetch_supermag_indices('2024-07-01', cache_dir=self.root,
                                        allow_download=True, userid='test', api_client=client)
        self.assertEqual(frame.iloc[0]['SME'], 88)
        self.assertTrue(np.isnan(frame.iloc[0]['SMR']))
        self.assertEqual(len(calls), 1)

    def test_request_time_validation(self):
        for start, extent in [('2024-07-01T00:00:01Z', 60), ('2024-07-01', 61),
                              ('2024-07-01', 86460)]:
            with self.assertRaises(ValueError):
                fetch_supermag_station('R01', start, extent, cache_dir=self.root)

    def test_r01_empty_dates_and_list_timestamp(self):
        frame, failures = get_r01_data(dates=[], cache_dir=self.root)
        self.assertTrue(frame.empty)
        self.assertEqual(failures, {})
        client, _ = self.client()
        frame, failures = get_r01_data(dates=[[2024, 7, 1, 0, 0]], cache_dir=self.root,
                                       allow_download=True, userid='test', api_client=client)
        self.assertEqual(failures, {})
        self.assertEqual(frame.iloc[0]['request_start'], '2024-07-01')

    def test_ascii_reader_and_filter(self):
        path = self.root / 'sample.txt'
        path.write_text('Acknowledgements\n2024 07 01 00 00 00 2\n'
                        'R01 267 38 1 40 60 1 2 999999 4 5 6\n'
                        'M03 260 40 1 42 60 7 8 9 10 11 12\n')
        frame = read_supermag_file(path, stations=['R01'])
        self.assertEqual(len(frame), 1)
        self.assertEqual(frame.iloc[0]['N'], 1)
        self.assertTrue(np.isnan(frame.iloc[0]['Z']))
        self.assertEqual(frame.iloc[0]['geoz'], 6)
        self.assertIn('Acknowledgements', frame.attrs['source_header'])

    def test_ascii_truncated_block_rejected(self):
        path = self.root / 'bad.txt'
        path.write_text('2024 07 01 00 00 00 2\nR01 267 38 1 40 60 1 2 3 4 5 6\n')
        with self.assertRaises(ValueError):
            read_supermag_file(path)

    def test_ascii_real_export_cadence_field(self):
        path = self.root / 'export.txt'
        path.write_text('2024 07 01 00 00 00 60 1\nR01 267 38 1 40 60 1 2 3 4 5 6\n')
        frame = read_supermag_file(path)
        self.assertEqual(frame.attrs['source_cadence_seconds'], [60])
        self.assertEqual(len(frame), 1)

    def test_local_omni_version_selection_and_no_download(self):
        (self.root / 'nested').mkdir()
        for version in [1, 10, 2]:
            (self.root / 'nested' / f'omni_hro2_1min_20240701_v{version:02d}.cdf').write_bytes(b'test')
        selected = _month_file(pd.Timestamp('2024-07-01'), self.root, False, None, 1)
        self.assertEqual(selected.name, 'omni_hro2_1min_20240701_v10.cdf')
        with self.assertRaises(FileNotFoundError):
            _month_file(pd.Timestamp('2024-08-01'), self.root, False, None, 1)

    def test_omni_fill_not_nominal_range_and_missing_variables(self):
        from cdflib import cdfepoch

        class FakeCDF:
            def cdf_info(self):
                return SimpleNamespace(zVariables=['Epoch', 'BZ_GSM'])

            def varget(self, name):
                if name == 'Epoch':
                    return cdfepoch.compute_epoch([[2024, 7, 1, 0, i, 0, 0] for i in range(3)])
                return np.array([120, 9999.99, -5], dtype=np.float32)

            def varattsget(self, name):
                return {'FILLVAL': np.float32(9999.99), 'VALIDMAX': 99.9, 'UNITS': 'nT'}

        frame = read_omni_cdf('mock.cdf', cdf_factory=lambda _: FakeCDF())
        self.assertEqual(frame.iloc[0]['Bz_GSM_nT'], 120)
        self.assertTrue(np.isnan(frame.iloc[1]['Bz_GSM_nT']))
        self.assertTrue(frame['AE_nT'].isna().all())
        self.assertIn('AE_nT', frame.attrs['missing_variables'])

    def test_omni_utc_bounds_and_inclusive_clip(self):
        with self.assertRaises(ValueError):
            utc_bounds('2024-07-02', '2024-07-01')
        times = pd.date_range('2024-07-01', periods=4, freq='min', tz='UTC')
        block = pd.DataFrame({'time': times, 'Bz_GSM_nT': [1, 2, 3, 4]})
        block.attrs['units'] = {'Bz_GSM_nT': 'nT'}
        with patch('data_processing.omni_data._month_file', return_value=Path('mock.cdf')), \
             patch('data_processing.omni_data.read_omni_cdf', return_value=block):
            frame = fetch_omni_hro2_1min('2024-06-30T20:01-04:00', '2024-07-01T00:02Z')
        self.assertEqual(list(frame['Bz_GSM_nT']), [2, 3])

    def omni_block(self, minutes, values):
        block = pd.DataFrame({'time': pd.to_datetime(minutes, utc=True), 'Bz_GSM_nT': values})
        block.attrs['units'] = {'Bz_GSM_nT': 'nT'}
        return block

    def test_omni_absent_minutes_and_native_coverage(self):
        block = self.omni_block(['2024-07-01T00:01Z', '2024-07-01T00:03Z'], [2, np.nan])
        with patch('data_processing.omni_data._month_file', return_value=Path('mock.cdf')), \
             patch('data_processing.omni_data.read_omni_cdf', return_value=block):
            frame, coverage = fetch_omni_hro2_1min('2024-07-01', '2024-07-01T00:04Z', return_coverage=True)
        self.assertEqual(len(frame), 5)
        self.assertEqual(frame['Bz_GSM_nT'].notna().sum(), 1)
        self.assertTrue(frame['AE_nT'].isna().all())
        self.assertEqual(coverage.loc['Bz_GSM_nT', 'pct_valid'], 20)
        self.assertEqual(coverage.loc['AE_nT', 'n_missing_native'], 5)
        self.assertEqual(frame.attrs['coverage']['Bz_GSM_nT']['n_total'], 5)

    def test_omni_means_preserve_all_missing_bins_and_native_counts(self):
        block = self.omni_block(['2024-07-01T00:00Z', '2024-07-01T00:04Z',
                                 '2024-07-01T00:06Z'], [2, 4, np.nan])
        with patch('data_processing.omni_data._month_file', return_value=Path('mock.cdf')), \
             patch('data_processing.omni_data.read_omni_cdf', return_value=block):
            frame, coverage = fetch_omni_hro2_1min('2024-07-01', '2024-07-01T00:09Z',
                                                   freq='5min', return_coverage=True)
        self.assertEqual(len(frame), 2)
        self.assertEqual(frame.iloc[0]['Bz_GSM_nT'], 3)
        self.assertTrue(np.isnan(frame.iloc[1]['Bz_GSM_nT']))
        self.assertEqual(coverage.loc['Bz_GSM_nT', 'pct_valid'], 50)
        self.assertEqual(coverage.loc['Bz_GSM_nT', 'pct_valid_native'], 20)

    def test_omni_missing_file_policy(self):
        with self.assertRaises(FileNotFoundError):
            fetch_omni_hro2_1min('2024-07-01', '2024-07-01T00:09Z', cache_dir=self.root)
        frame, coverage = fetch_omni_hro2_1min('2024-07-01', '2024-07-01T00:09Z',
                                               cache_dir=self.root, freq='5min',
                                               missing_files='nan', return_coverage=True)
        self.assertEqual(len(frame), 2)
        self.assertTrue(frame.drop(columns='time').isna().all().all())
        self.assertTrue(coverage['pct_valid'].eq(0).all())
        self.assertEqual(frame.attrs['absent_months'], ['2024-07'])

    def test_omni_physical_signs_float32_fills_and_optional_limits(self):
        self.assertTrue(np.isnan(_clean_values([-1], 'FlowSpeed_km_s')[0]))
        self.assertTrue(np.isnan(_clean_values([np.float32(9999.99)], 'Bz_GSM_nT', [9999.99])[0]))
        self.assertEqual(_clean_values([99.99], 'DynamicPressure_nPa')[0], 99.99)
        self.assertEqual(_clean_values([-120], 'Bz_GSM_nT')[0], -120)
        self.assertTrue(np.isnan(_clean_values([-600], 'Bz_GSM_nT',
                                              field_limits={'Bz_GSM_nT': (-500, 500)})[0]))

    def test_omni_selected_days_keep_missing_days_without_intervening_months(self):
        days = ['2024-07-08', '2025-05-01', '2024-07-08']
        with patch('data_processing.omni_data._month_file', side_effect=FileNotFoundError) as lookup:
            frame, coverage = fetch_omni_for_days(days, cache_dir=self.root, return_coverage=True)
        self.assertEqual(lookup.call_count, 2)
        self.assertEqual(len(frame), 2 * 288)
        self.assertEqual(coverage.index.names, ['day', 'variable'])
        self.assertTrue(coverage['n_total_native'].eq(1440).all())
        self.assertTrue(coverage['pct_valid'].eq(0).all())

    def test_omni_day_final_bin_uses_last_four_minutes(self):
        block = self.omni_block(['2024-07-01T23:55Z', '2024-07-01T23:59Z'], [2, 6])
        with patch('data_processing.omni_data._month_file', return_value=Path('mock.cdf')), \
             patch('data_processing.omni_data.read_omni_cdf', return_value=block):
            frame, coverage = fetch_omni_for_days(['2024-07-01'], return_coverage=True)
        self.assertEqual(len(frame), 288)
        self.assertEqual(frame.iloc[-1]['Bz_GSM_nT'], 4)
        self.assertEqual(coverage.loc[('2024-07-01', 'Bz_GSM_nT'), 'n_valid_native'], 2)

    def test_omni_empty_days_and_invalid_resampling(self):
        frame, coverage = fetch_omni_for_days([], return_coverage=True)
        self.assertTrue(frame.empty)
        self.assertTrue(coverage.empty)
        self.assertTrue(omni_coverage(frame)['pct_valid'].isna().all())
        for freq in ['30s', '0min', '-5min', 'MS']:
            with self.assertRaises(ValueError):
                fetch_omni_hro2_1min('2024-07-01', '2024-07-02', freq=freq)


if __name__ == '__main__':
    unittest.main()