"""GeoE daily-file selection and model-version mixing guards."""
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd
import xarray as xr

from data_processing.geoe_data import (
    resolve_mixed_gridpt_groups,
    select_geoe_files,
)


def _touch(directory: Path, name: str) -> Path:
    fp = directory / name
    fp.write_bytes(b"")
    return fp


def _site_dataset(day: str, n_gridpt: int) -> xr.Dataset:
    times = pd.date_range(f"{day} 00:00:30", periods=4, freq="1min")
    return xr.Dataset(
        {"Ex": (("time", "gridpt"), np.ones((4, n_gridpt), dtype=np.float32))},
        coords={"time": times.to_numpy()},
    )


class SelectGeoeFilesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cleaned = self.root / "2024-2025"
        self.cleaned.mkdir()

    def test_prefers_cleaned_file_per_day_and_filters_dates(self):
        _touch(self.root, "20240509-empirical-EMTF-2022.12-2022.12.nc")
        _touch(self.root, "20240510-empirical-EMTF-2022.12-2022.12.nc")
        _touch(self.cleaned, "20240510-empirical-EMTF-3D-v2024.07.nc")
        _touch(self.root, "20240601-empirical-EMTF-2022.12-2022.12.nc")  # outside

        files, prov = select_geoe_files(
            self.root, "2024-05-08", "2024-05-15", cleaned_dir=self.cleaned
        )

        self.assertEqual([f.name for f in files], [
            "20240509-empirical-EMTF-2022.12-2022.12.nc",
            "20240510-empirical-EMTF-3D-v2024.07.nc",
        ])
        self.assertEqual(prov["2024-05-09"]["source"], "legacy")
        self.assertEqual(prov["2024-05-10"]["source"], "cleaned")
        self.assertNotIn("2024-06-01", prov)

    def test_no_cleaned_dir_falls_back_to_legacy_only(self):
        _touch(self.root, "20240510-empirical-EMTF-2022.12-2022.12.nc")
        files, prov = select_geoe_files(self.root, "2024-05-10", "2024-05-10")
        self.assertEqual(len(files), 1)
        self.assertEqual(prov["2024-05-10"]["source"], "legacy")


class ResolveMixedGridptTests(unittest.TestCase):
    def test_cleaned_group_wins_even_as_minority(self):
        datasets = [
            _site_dataset("2024-05-08", 3432),
            _site_dataset("2024-05-09", 3432),
            _site_dataset("2024-05-10", 3784),
            _site_dataset("2024-05-14", 3432),
        ]
        tiers = ["legacy", "legacy", "cleaned", "legacy"]
        provenance = {}

        kept = resolve_mixed_gridpt_groups(datasets, tiers, provenance)

        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0].sizes["gridpt"], 3784)
        self.assertIn("_mixed_grid_note", provenance)
        self.assertIn("2024-05-08", provenance["_mixed_grid_note"])

    def test_uniform_grid_passes_through_without_note(self):
        datasets = [_site_dataset("2024-05-10", 3784), _site_dataset("2024-05-11", 3784)]
        provenance = {}
        kept = resolve_mixed_gridpt_groups(datasets, ["cleaned", "cleaned"], provenance)
        self.assertEqual(len(kept), 2)
        self.assertNotIn("_mixed_grid_note", provenance)


if __name__ == "__main__":
    unittest.main()
