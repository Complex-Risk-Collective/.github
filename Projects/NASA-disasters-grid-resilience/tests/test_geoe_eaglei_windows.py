"""Small regression cases; no external files, downloads or full-period runs."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd
import xarray as xr

from data_processing.geoe_eaglei_windows import (
    construct_time_matched_quiet_periods,
    extract_geoe_window_descriptors_resumable,
    summarize_geoe_window,
)


class QuietWindowTests(unittest.TestCase):
    def setUp(self):
        self.times = pd.date_range("2024-01-08 12:00", periods=5, freq="15min")
        self.panel = pd.DataFrame({
            "time": self.times, "county_fips": "01001", "customers_out": 0.,
            "percent_out": 0., "is_suppressed_override": False,
        })
        self.episodes = pd.DataFrame({
            "impact_episode_id": [1], "county_fips": ["01001"],
            "start_time": [pd.Timestamp("2024-01-01 12:00")],
            "end_time": [pd.Timestamp("2024-01-01 13:00")],
        })
        self.thresholds = pd.DataFrame({
            "county_fips": ["01001"], "customers_out_threshold": [50.],
            "percent_out_threshold": [0.5],
        })

    def match(self, panel=None, exclusions=None):
        return construct_time_matched_quiet_periods(
            self.episodes, self.panel if panel is None else panel,
            thresholds=self.thresholds, exclusion_buffer=pd.Timedelta(0),
            exclusion_episodes=exclusions,
        )

    def test_complete_quiet_window_and_numeric_fips(self):
        panel = self.panel.assign(county_fips=1001)
        result = self.match(panel)
        self.assertEqual(len(result), 1)
        self.assertEqual(result.iloc[0].county_fips, "01001")
        self.assertEqual(result.iloc[0].quiet_end_time, self.times[-1])
        self.assertTrue(result.iloc[0].quiet_window_id)

    def test_full_window_overlap_not_only_start(self):
        extra = self.episodes.copy()
        extra["start_time"] = self.times[2]
        extra["end_time"] = self.times[3]
        extra["impact_episode_id"] = 2
        self.assertTrue(self.match(exclusions=pd.concat([self.episodes, extra])).empty)
        # An impact in a different county must not invalidate this control.
        extra["county_fips"] = "01003"
        self.assertEqual(len(self.match(exclusions=pd.concat([self.episodes, extra]))), 1)

    def test_missing_internal_or_end_reading(self):
        for position in (2, 4):
            with self.subTest(position=position):
                self.assertTrue(self.match(self.panel.drop(position)).empty)

    def test_any_active_nonfinite_or_suppressed_reading(self):
        for column, value in [("customers_out", 50.), ("percent_out", 0.5),
                              ("customers_out", np.nan), ("percent_out", np.nan),
                              ("is_suppressed_override", True)]:
            with self.subTest(column=column, value=value):
                panel = self.panel.copy()
                panel.loc[2, column] = value
                self.assertTrue(self.match(panel).empty)

    def test_duplicate_timestamp_fails(self):
        with self.assertRaisesRegex(ValueError, "duplicate"):
            self.match(pd.concat([self.panel, self.panel.iloc[[0]]]))

    def test_empty_result_has_identity_columns(self):
        result = self.match(self.panel.iloc[:0])
        self.assertTrue({"county_fips", "quiet_window_id", "impact_episode_id"}.issubset(result.columns))


class LocalExposureTests(unittest.TestCase):
    def setUp(self):
        self.times = pd.date_range("2024-01-01", periods=3, freq="1min")
        self.geoe = xr.Dataset({
            "E_mag": (("time", "y", "x"), np.array([[[10., 1000.]], [[20., 1000.]], [[30., 1000.]]])),
            "Ex": (("time", "y", "x"), np.array([[[10., 1000.]], [[20., 1000.]], [[30., 1000.]]])),
            "Ey": (("time", "y", "x"), np.zeros((3, 1, 2))),
        }, coords={"time": self.times, "y": [0], "x": [0, 1]})
        self.mapping = pd.DataFrame({
            "county_fips": ["01001", "01003"], "grid_y": [0, 0], "grid_x": [0, 1],
            "intersection_area_km2": [100., 100.], "county_area_km2": [100., 100.],
        })

    def summarize(self, geoe=None, mapping=None, fips="01001", threshold=15.):
        return summarize_geoe_window(
            self.geoe if geoe is None else geoe, self.times[0], self.times[-1], threshold,
            county_fips=fips, county_mapping=self.mapping if mapping is None else mapping,
        )

    def test_local_not_national_peak_and_derivative(self):
        result = self.summarize()
        self.assertEqual(result["exposure_status"], "ok")
        self.assertEqual(result["e_mag_max_mV_km"], 30.)
        self.assertEqual(result["d_e_dt_max_mV_km_per_min"], 10.)
        self.assertEqual(result["vector_change_max_mV_km_per_min"], 10.)
        self.assertAlmostEqual(result["integrated_exposure_mV_km_hours"], 12.5 / 60)
        self.assertEqual(self.summarize(fips="01003")["e_mag_max_mV_km"], 1000.)

    def test_area_weighted_mean_and_supported_denominator(self):
        mapping = pd.DataFrame({
            "county_fips": ["01001", "01001"], "grid_y": [0, 0], "grid_x": [0, 1],
            "intersection_area_km2": [95., 5.], "county_area_km2": [100., 100.],
        })
        result = self.summarize(mapping=mapping)
        self.assertAlmostEqual(result["e_mag_mean_mV_km"], 69.)
        geoe = self.geoe.copy(deep=True)
        geoe["E_mag"].values[:, 0, 1] = np.nan
        result = self.summarize(geoe=geoe, mapping=mapping, threshold=1.)
        self.assertEqual(result["exposure_status"], "ok")
        self.assertEqual(result["max_active_area_fraction"], 1.)
        self.assertAlmostEqual(result["min_finite_area_fraction"], .95)

    def test_missing_support_is_not_zero_hazard(self):
        geoe = self.geoe.copy(deep=True)
        geoe["E_mag"].values[:, 0, 0] = np.nan
        result = self.summarize(geoe=geoe)
        self.assertEqual(result["exposure_status"], "insufficient_finite_support")
        self.assertTrue(np.isnan(result["e_mag_max_mV_km"]))
        self.assertEqual(self.summarize(fips="99999")["exposure_status"], "unmapped")

    def test_partial_county_coverage_and_fallback_not_hidden(self):
        mapping = self.mapping.copy()
        mapping.loc[0, "county_area_km2"] = 200.
        result = self.summarize(mapping=mapping)
        self.assertEqual(result["exposure_status"], "insufficient_county_grid_coverage")
        self.assertEqual(result["county_grid_coverage_fraction"], .5)
        mapping.loc[0, "intersection_area_km2"] = np.nan
        self.assertEqual(self.summarize(mapping=mapping)["exposure_status"], "unsupported_mapping")

    def test_internal_time_gap_rejected(self):
        self.assertEqual(self.summarize(geoe=self.geoe.isel(time=[0, 2]))["exposure_status"], "incomplete_time_coverage")

    def test_vector_change_with_constant_magnitude(self):
        geoe = self.geoe.copy(deep=True)
        geoe["E_mag"].values[:, 0, 0] = 10.
        geoe["Ex"].values[:, 0, 0] = [10., -10., 10.]
        result = self.summarize(geoe=geoe)
        self.assertEqual(result["d_e_dt_max_mV_km_per_min"], 0.)
        self.assertEqual(result["vector_change_max_mV_km_per_min"], 20.)

    def test_resume_identity_and_changed_inputs(self):
        windows = pd.DataFrame({"impact_episode_id": [1, 1], "county_fips": ["01001", "01003"],
                                "start": self.times[:2], "end": self.times[1:]})
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "local.csv"
            def extract(frame=windows, threshold=15.):
                return extract_geoe_window_descriptors_resumable(
                    frame, self.geoe, "impact_episode_id", "start", "end", path,
                    county_mapping=self.mapping, source_version="synthetic-v1",
                    checkpoint_every=1, candidate_threshold=threshold,
                )
            first = extract()
            resumed = extract()
            self.assertEqual(len(resumed), 2)
            self.assertEqual(first.window_key.tolist(), resumed.window_key.tolist())
            self.assertEqual(resumed.county_fips.tolist(), ["01001", "01003"])
            with self.assertRaisesRegex(ValueError, "inputs changed"):
                extract(threshold=20.)
            with self.assertRaisesRegex(ValueError, "inputs changed"):
                extract(frame=windows.iloc[::-1])


if __name__ == "__main__":
    unittest.main()