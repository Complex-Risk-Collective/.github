"""Offline geometry checks for rebuilt county allocations and cache identity."""
from __future__ import annotations

import sys
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import pytest
import xarray as xr
from shapely.geometry import box

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "data_processing"))
import eaglei_grid_mapping as mapping


def test_geometry_allocation_exclusions_and_content_cache(tmp_path, monkeypatch):
    grid = tmp_path / "grid.nc"
    xr.Dataset({"lat": (("y", "x"), np.zeros((2, 2))),
                "lon": (("y", "x"), np.zeros((2, 2)))},
               coords={"x": [0., 1000.], "y": [0., 1000.]},
               attrs={"proj": "EPSG:5070"}).to_netcdf(grid)
    # First county is 75% in left cell, 25% in right. Second partly leaves
    # the grid and must export its 50% coverage, not hide edge truncation.
    counties = gpd.GeoDataFrame({"county_fips": ["01001", "01003", "01005"],
                                "state_fips": ["01"] * 3,
                                "county_name": ["split", "partial", "outside"]},
                               geometry=[box(-250, -250, 750, 250),
                                         box(-1000, 750, 0, 1250),
                                         box(5000, 5000, 6000, 6000)], crs="EPSG:5070")
    def load(fips, **kwargs):
        assert "02013" not in fips and "15001" not in fips and "72001" not in fips
        return counties.loc[counties.county_fips.isin(fips)].copy()
    monkeypatch.setattr(mapping, "load_county_geometries_for_fips", load)
    requested = ["01001", "01003", "01005", "01007", "02013", "15001", "72001"]
    out = tmp_path / "mapping"
    result = mapping.load_or_build_grid_mapping(requested, grid, out)
    np.testing.assert_allclose(result.loc[result.county_fips.eq("01001"), "weight"], [.75, .25])
    np.testing.assert_allclose(result.groupby("county_fips").weight.sum(), 1)
    assert result.loc[result.county_fips.eq("01003"), "county_grid_coverage_fraction"].iloc[0] == .5
    excluded = pd.read_csv(out / "eaglei_unmapped_counties.csv", dtype={"county_fips": str}).set_index("county_fips")
    assert excluded.loc["01005", "reason"] == "no_positive_grid_overlap"
    assert excluded.loc["01007", "reason"] == "county_geometry_missing"
    assert excluded.loc["02013", "reason"] == "outside_CONUS"
    cached = mapping.load_or_build_grid_mapping(requested, grid, out)
    pd.testing.assert_frame_equal(cached, result, check_dtype=False)
    with pytest.raises(ValueError, match="identity changed"):
        mapping.load_or_build_grid_mapping(requested[:-1], grid, out)
    # Same name, same shape, different coordinates must invalidate the cache.
    ds = xr.load_dataset(grid)
    ds = ds.assign_coords(x=ds.x + 10)
    ds.to_netcdf(grid)
    with pytest.raises(ValueError, match="identity changed"):
        mapping.load_or_build_grid_mapping(requested, grid, out)


def test_fips_normalization_rejects_embedded_digits():
    values = mapping.normalize_fips(pd.Series([1001, "01003", "1005.0", None, "county1007", "123456"]), 5)
    assert values.iloc[:3].tolist() == ["01001", "01003", "01005"]
    assert values.iloc[3:].isna().all()