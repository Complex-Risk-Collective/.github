#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
eaglei_grid_pipeline.py

Re-grid Eagle-I county-level power-outage data onto the project's 50 km CONUS
analysis grid (``data/analysis_grid_CONUS_50km.nc``), at native Eagle-I
15-minute temporal resolution, for the 2024 and 2025 calendar years.

Pipeline summary
-----------------
1. Load the county-level 15-minute outage panel. Eagle-I only reports
   non-zero ``customers_out`` rows (README: "Entries with 0 customers
   without power are not included"), so the panel is sparse -- this is
   preserved throughout and only densified into a full (time, y, x) array at
   the very end.

   Preferred source: the cached
   ``EAGLE-I/processed/eaglei_county_15min_panel_2024_2025.csv`` panel built
   by the geoE/Eagle-I threshold-analysis notebook. If that cache is
   missing, falls back to building the same shape of table directly from
   the raw yearly CSVs (``eaglei_outages_2024.csv`` / ``_2025.csv``) so this
   script is still runnable standalone.

2. Load the area-weighted county -> analysis-grid-cell mapping built by
   ``eaglei_grid_mapping.py`` (weights sum to 1 per county, so a county's
   ``customers_out`` value is conserved when spread across the grid cells it
   overlaps, proportional to intersection area).

3. For each year, join the sparse panel to the weight table, scatter-add the
   weighted contributions into a dense (time, y, x) array at 15-minute
   resolution, and write one NetCDF4 file per year:

       eaglei_outages_analysis_grid_2024.nc
       eaglei_outages_analysis_grid_2025.nc

   to the EAGLE-I data directory (same directory as the raw CSVs), with
   zlib-compressed encoding for `customers_out`, matching the encoding
   convention used elsewhere in this repo (see pipeline.py).

4. Print a mass-conservation QA check comparing the summed raw
   ``customers_out`` (for mapped counties) against the summed gridded
   output.

Note on scope: only ``customers_out`` is produced (not ``percent_out``), so
2024 and 2025 outputs are directly comparable -- the 2025 raw Eagle-I file
does not include a ``total_customers`` column, unlike 2024.

This script depends on pandas/numpy/xarray/netCDF4 for the actual gridding
step. It does NOT need geopandas/shapely unless the area-weighted mapping
cache in eaglei_grid_mapping.py has to be rebuilt from scratch.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from eaglei_grid_mapping import (  # noqa: E402
    EAGLEI_DIR_DEFAULT,
    EAGLEI_PROCESSED_DIR_DEFAULT,
    GRID_PATH_DEFAULT,
    load_or_build_area_weighted_mapping,
    normalize_fips,
)

# ------------------------
# Config
# ------------------------

YEARS = [2024, 2025]

EAGLEI_DIR = EAGLEI_DIR_DEFAULT
EAGLEI_PROCESSED_DIR = EAGLEI_PROCESSED_DIR_DEFAULT
GRID_PATH = GRID_PATH_DEFAULT

PANEL_CSV = EAGLEI_PROCESSED_DIR / "eaglei_county_15min_panel_2024_2025.csv"

OUTPUT_FILENAME_TEMPLATE = "eaglei_outages_analysis_grid_{year}.nc"

TIME_FREQ = "15min"
PANEL_CHUNKSIZE = 2_000_000
RAW_CSV_CHUNKSIZE = 2_000_000


# ------------------------
# Panel loading
# ------------------------

def load_panel_for_year(
    year: int,
    panel_csv: Path = PANEL_CSV,
    eaglei_dir: Path = EAGLEI_DIR,
    chunksize: int = PANEL_CHUNKSIZE,
) -> pd.DataFrame:
    """
    Return a DataFrame with columns [time, county_fips, customers_out] for
    the requested year, from the cached county 15-min panel if present,
    otherwise built directly from the raw yearly Eagle-I CSV.
    """
    if panel_csv.exists():
        print(f"Loading cached county panel for {year} from: {panel_csv}")
        pieces = []
        usecols = ["time", "county_fips", "customers_out"]
        for chunk in pd.read_csv(
            panel_csv,
            usecols=usecols,
            dtype={"county_fips": str},
            chunksize=chunksize,
        ):
            chunk["time"] = pd.to_datetime(chunk["time"], errors="coerce")
            chunk = chunk[chunk["time"].dt.year == year]
            if not chunk.empty:
                chunk["county_fips"] = normalize_fips(chunk["county_fips"], 5)
                pieces.append(chunk)
        if not pieces:
            return pd.DataFrame(columns=usecols)
        panel = pd.concat(pieces, ignore_index=True)
        return panel

    print(
        f"Cached panel not found ({panel_csv}); building panel for {year} "
        "directly from the raw yearly Eagle-I CSV."
    )
    return _build_panel_from_raw_csv(year, eaglei_dir, chunksize=RAW_CSV_CHUNKSIZE)


def _find_column(columns, candidates) -> Optional[str]:
    lower = {c.lower(): c for c in columns}
    normalized = {c.lower().replace(" ", "_"): c for c in columns}
    for candidate in candidates:
        cl = candidate.lower()
        cn = cl.replace(" ", "_")
        if cl in lower:
            return lower[cl]
        if cn in normalized:
            return normalized[cn]
    return None


def _build_panel_from_raw_csv(
    year: int, eaglei_dir: Path, chunksize: int = RAW_CSV_CHUNKSIZE
) -> pd.DataFrame:
    raw_path = eaglei_dir / f"eaglei_outages_{year}.csv"
    if not raw_path.exists():
        raise FileNotFoundError(f"Raw Eagle-I CSV not found for {year}: {raw_path}")

    header = pd.read_csv(raw_path, nrows=0)
    time_col = _find_column(header.columns, ["run_start_time", "Run Start Time"])
    fips_col = _find_column(header.columns, ["fips_code", "Fips Code"])
    out_col = _find_column(header.columns, ["customers_out", "Customers Out"])
    if time_col is None or fips_col is None or out_col is None:
        raise KeyError(
            f"Could not find expected columns in {raw_path}. "
            f"Columns available: {list(header.columns)}"
        )

    pieces = []
    for chunk in pd.read_csv(
        raw_path, usecols=[time_col, fips_col, out_col], chunksize=chunksize
    ):
        chunk = chunk.rename(
            columns={time_col: "time", fips_col: "county_fips", out_col: "customers_out"}
        )
        chunk["time"] = pd.to_datetime(chunk["time"], errors="coerce")
        chunk["county_fips"] = normalize_fips(chunk["county_fips"], 5)
        chunk["customers_out"] = pd.to_numeric(chunk["customers_out"], errors="coerce")
        chunk = chunk.dropna(subset=["time", "county_fips", "customers_out"])
        pieces.append(chunk[["time", "county_fips", "customers_out"]])

    if not pieces:
        return pd.DataFrame(columns=["time", "county_fips", "customers_out"])
    return pd.concat(pieces, ignore_index=True)


# ------------------------
# Grid template access (thin wrapper so this module only needs xarray/pyproj
# when actually building output, not for the pure aggregation logic below)
# ------------------------

def load_grid_template(grid_path: Path = GRID_PATH):
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from grid import AnalysisGridTemplate  # local import: needs xarray/pyproj

    return AnalysisGridTemplate(grid_file=str(grid_path))


# ------------------------
# Core aggregation (pure pandas/numpy -- unit-testable without geopandas/xarray)
# ------------------------

def build_time_index(year: int, freq: str = TIME_FREQ) -> pd.DatetimeIndex:
    start = pd.Timestamp(year=year, month=1, day=1)
    end = pd.Timestamp(year=year, month=12, day=31, hour=23, minute=45)
    return pd.date_range(start=start, end=end, freq=freq)


def aggregate_panel_to_grid(
    panel: pd.DataFrame,
    weights: pd.DataFrame,
    time_index: pd.DatetimeIndex,
    ny: int,
    nx: int,
    dtype=np.float32,
) -> tuple[np.ndarray, dict]:
    """
    Core gridding step, isolated from any geospatial library so it can be
    unit tested with plain pandas/numpy.

    panel:   columns [time, county_fips, customers_out]
    weights: columns [county_fips, grid_y, grid_x, weight]  (weights should
             sum to ~1 per county_fips)
    time_index: full regular time axis to grid onto; rows in `panel` whose
             timestamp does not land exactly on this axis are dropped and
             counted in the returned QA dict.

    Returns (out_array, qa) where out_array has shape (len(time_index), ny, nx)
    and qa is a dict of diagnostic counts/sums.
    """
    merged = panel.merge(
        weights[["county_fips", "grid_y", "grid_x", "weight"]],
        on="county_fips",
        how="inner",
    )
    merged["contribution"] = merged["customers_out"] * merged["weight"]

    sparse = (
        merged.groupby(["time", "grid_y", "grid_x"], sort=False)["contribution"]
        .sum()
        .reset_index()
    )

    time_pos = pd.Index(time_index).get_indexer(sparse["time"])
    on_axis = time_pos >= 0
    n_off_axis = int((~on_axis).sum())

    out = np.zeros((len(time_index), ny, nx), dtype=dtype)
    if on_axis.any():
        np.add.at(
            out,
            (
                time_pos[on_axis],
                sparse.loc[on_axis, "grid_y"].to_numpy(),
                sparse.loc[on_axis, "grid_x"].to_numpy(),
            ),
            sparse.loc[on_axis, "contribution"].to_numpy(dtype=dtype),
        )

    mapped_counties = set(weights["county_fips"])
    raw_total_mapped = float(
        panel.loc[panel["county_fips"].isin(mapped_counties), "customers_out"].sum()
    )
    gridded_total = float(out.sum())

    qa = {
        "n_panel_rows": int(len(panel)),
        "n_merged_rows": int(len(merged)),
        "n_sparse_cells": int(len(sparse)),
        "n_rows_off_time_axis": n_off_axis,
        "raw_total_customers_out_mapped_counties": raw_total_mapped,
        "gridded_total_customers_out": gridded_total,
        "conservation_ratio": (
            gridded_total / raw_total_mapped if raw_total_mapped else float("nan")
        ),
    }
    return out, qa


# ------------------------
# Output dataset construction / writing (needs xarray)
# ------------------------

def build_output_dataset(out_array: np.ndarray, time_index: pd.DatetimeIndex, grid_template, year: int, qa: dict):
    import xarray as xr

    y_centers = grid_template.grid.coords["y"].values
    x_centers = grid_template.grid.coords["x"].values
    lat2d = grid_template.grid["lat"].values
    lon2d = grid_template.grid["lon"].values

    ds = xr.Dataset(
        {
            "customers_out": (
                ("time", "y", "x"),
                out_array,
                {
                    "long_name": "Number of customers without power",
                    "units": "customers",
                    "description": (
                        "County-level Eagle-I customers_out values redistributed "
                        "onto the 50 km analysis grid using area-weighted "
                        "intersection between county polygons and grid cells "
                        "(weights sum to 1 per county, so gridded totals "
                        "conserve county totals for counties mapped to this "
                        "grid). Cells/timesteps with no reported outage are 0."
                    ),
                },
            )
        },
        coords={
            "time": time_index,
            "y": y_centers,
            "x": x_centers,
            "lat": (("y", "x"), lat2d),
            "lon": (("y", "x"), lon2d),
        },
        attrs={
            "title": f"Eagle-I power outage counts on 50 km CONUS analysis grid, {year}",
            "source": "Oak Ridge National Laboratory Eagle-I power outage dataset (DOE)",
            "grid_source": str(GRID_PATH),
            "mapping_method": "area_weighted_county_to_grid_cell_intersection",
            "temporal_resolution": TIME_FREQ,
            "year": year,
            "created": datetime.now(timezone.utc).isoformat(),
            "qa_n_panel_rows": qa["n_panel_rows"],
            "qa_n_rows_off_time_axis": qa["n_rows_off_time_axis"],
            "qa_raw_total_customers_out_mapped_counties": qa[
                "raw_total_customers_out_mapped_counties"
            ],
            "qa_gridded_total_customers_out": qa["gridded_total_customers_out"],
            "qa_conservation_ratio": qa["conservation_ratio"],
        },
    )
    return ds


def write_year_netcdf(ds, out_path: Path):
    encoding = {"customers_out": {"zlib": True, "complevel": 4}}
    out_path.parent.mkdir(parents=True, exist_ok=True)
    ds.to_netcdf(out_path, format="NETCDF4", encoding=encoding)
    print(f"Wrote {out_path}")


# ------------------------
# Per-year driver
# ------------------------

def process_year(
    year: int,
    grid_template=None,
    panel_csv: Path = PANEL_CSV,
    eaglei_dir: Path = EAGLEI_DIR,
    output_dir: Optional[Path] = None,
):
    output_dir = output_dir or eaglei_dir
    out_path = output_dir / OUTPUT_FILENAME_TEMPLATE.format(year=year)

    print(f"\n=== Processing Eagle-I -> analysis grid, {year} ===")

    weights = load_or_build_area_weighted_mapping()
    panel = load_panel_for_year(year, panel_csv=panel_csv, eaglei_dir=eaglei_dir)
    if panel.empty:
        print(f"No panel rows found for {year}; skipping.")
        return

    time_index = build_time_index(year)

    if grid_template is None:
        grid_template = load_grid_template()
    ny = grid_template.grid.dims["y"]
    nx = grid_template.grid.dims["x"]

    out_array, qa = aggregate_panel_to_grid(panel, weights, time_index, ny, nx)

    print(
        f"{year}: panel rows={qa['n_panel_rows']:,}  "
        f"off-time-axis rows dropped={qa['n_rows_off_time_axis']:,}  "
        f"raw total (mapped counties)={qa['raw_total_customers_out_mapped_counties']:,.0f}  "
        f"gridded total={qa['gridded_total_customers_out']:,.0f}  "
        f"conservation ratio={qa['conservation_ratio']:.6f}"
    )

    ds = build_output_dataset(out_array, time_index, grid_template, year, qa)
    write_year_netcdf(ds, out_path)


def run(years=YEARS):
    grid_template = load_grid_template()
    for year in years:
        process_year(year, grid_template=grid_template)


# ------------------------
# Self-test (pure pandas/numpy, no geopandas/xarray required)
# ------------------------

def _self_test():
    """
    Exercise aggregate_panel_to_grid() against small synthetic inputs to
    check: (1) area weights conserve county totals exactly when a county
    spans multiple grid cells, (2) contributions from multiple counties
    sharing a grid cell sum correctly, (3) off-grid time rows are dropped
    and counted rather than silently corrupting the array shape.
    """
    time_index = pd.date_range("2024-01-01 00:00", periods=4, freq="15min")

    panel = pd.DataFrame(
        {
            "time": [
                time_index[0], time_index[0], time_index[1],
                time_index[2], "2024-01-01 09:00",  # off-axis timestamp
            ],
            "county_fips": ["00001", "00002", "00001", "00002", "00001"],
            "customers_out": [100.0, 50.0, 40.0, 10.0, 999.0],
        }
    )
    panel["time"] = pd.to_datetime(panel["time"])

    # county 00001 spans two grid cells 60/40; county 00002 sits entirely in
    # one grid cell that ALSO receives part of county 00001 (shared cell).
    weights = pd.DataFrame(
        {
            "county_fips": ["00001", "00001", "00002"],
            "grid_y": [0, 0, 0],
            "grid_x": [0, 1, 1],
            "weight": [0.6, 0.4, 1.0],
        }
    )

    out, qa = aggregate_panel_to_grid(panel, weights, time_index, ny=2, nx=2)

    assert out.shape == (4, 2, 2)
    # The off-axis row belongs to county 00001, which maps to 2 grid cells,
    # so after the county->cell join it expands to 2 sparse (time, cell) rows.
    assert qa["n_rows_off_time_axis"] == 2

    # t=0: county 00001=100 -> (0,0)+=60, (0,1)+=40 ; county 00002=50 -> (0,1)+=50
    assert np.isclose(out[0, 0, 0], 60.0)
    assert np.isclose(out[0, 0, 1], 90.0)  # 40 (from 00001) + 50 (from 00002)

    # t=1: county 00001=40 -> (0,0)+=24, (0,1)+=16
    assert np.isclose(out[1, 0, 0], 24.0)
    assert np.isclose(out[1, 0, 1], 16.0)

    # t=2: county 00002=10 -> (0,1)+=10
    assert np.isclose(out[2, 0, 1], 10.0)
    assert np.isclose(out[2, 0, 0], 0.0)

    # off-axis row (999.0 at 09:00) must NOT appear anywhere in the array
    assert np.isclose(out.sum(), 60.0 + 90.0 + 24.0 + 16.0 + 10.0)

    # conservation: raw total for mapped counties excludes the off-axis
    # timestamp only insofar as it's still counted in customers_out sum
    # (raw_total includes all panel rows for mapped counties regardless of
    # time-axis alignment -- this highlights any off-axis loss via the
    # conservation_ratio being < 1).
    expected_raw_total = panel["customers_out"].sum()  # all rows, both counties mapped
    assert np.isclose(qa["raw_total_customers_out_mapped_counties"], expected_raw_total)
    assert qa["conservation_ratio"] < 1.0  # because the 999.0 off-axis row was dropped

    print("Self-test passed: weighting, multi-county cell summation, and")
    print("off-axis-row handling all behave as expected.")
    print(f"QA: {qa}")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--self-test":
        _self_test()
    else:
        run()
