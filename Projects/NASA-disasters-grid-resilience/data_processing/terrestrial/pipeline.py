#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Created on Fri Feb 27 10:39:02 2026

@author: marchett

v2 changes (Step 0 fixes):
  [FIX 10] Every source is conformed to the template grid and the full hourly
           axis before merging, and the merge fails loudly on conflicts
           (was compat='override', which silently keeps the first value).
  [FIX 11] Files record processing_version = "v2" as an attribute (filenames
           unchanged; write v2 output to a separate directory from v1).
"""

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import xarray as xr

from data_processing.terrestrial.get_swdi_data import download_swdi
from data_processing.terrestrial.aggregate_swdi import aggregate_swdi
from data_processing.grid import AnalysisGridTemplate
from data_processing.terrestrial.get_aggregate_mrms_data import (
    download_and_grid, add_latlon, PROCESSING_VERSION, atomic_to_netcdf)
from data_processing.terrestrial.get_aggregate_rtma_data import download_and_grid_rtma
from data_processing.terrestrial.variable_metadata import apply_metadata, write_data_dictionary


# ------------------------
# CONFIG
# ------------------------

PATH = "/Users/marchett/Documents/Disasters/data/V2"
region = "CONUS"

lat_bounds = [25, 50]
lon_bounds = [-125, -65]

swdi_variables = ["nx3tvs", "nx3hail", "nx3structure"]

mrms_variables = [
    "MergedBaseReflectivity_00.50",
    "MergedReflectivityComposite_00.50",
    "MergedReflectivityAtLowestAltitude_00.50",
    "FLASH_QPE_ARI01H_00.00",
    "FLASH_QPE_ARI12H_00.00",
    "FLASH_QPE_ARIMAX_00.00",
    "FLASH_QPE_FFGMAX_00.00",               # rain / flash-flood guidance, max over windows
    "FLASH_CREST_MAXUNITSTREAMFLOW_00.00",  # water flowing in streams now (flood response)
    "FLASH_CREST_MAXSOILSAT_00.00",         # how full the soil is (slow; flood/outage context)
    "MultiSensor_QPE_01H_Pass2_00.00",
    "MultiSensor_QPE_24H_Pass2_00.00",
    "MultiSensor_QPE_72H_Pass2_00.00",
    "MergedAzShear_0-2kmAGL_00.50",
    "MergedAzShear_3-6kmAGL_00.50",
    "RotationTrackML60min_00.50",
    # "MESH_00.50",          # replaced by the 60-min max track (full hour, 1 file)
    "MESH_Max_60min_00.50",
    "POSH_00.50",
    "VII_00.50",
    "VIL_Density_00.50",
    "PrecipRate_00.00",
    # "NLDN_CG_001min_AvgDensity_00.00",  # dropped: 015min covers the full hour
    "NLDN_CG_015min_AvgDensity_00.00",
    "EchoTop_18_00.50",
    "EchoTop_50_00.50",
    "PrecipFlag_00.00",
]

grid_template = AnalysisGridTemplate(
    grid_file=f"{PATH}/analysis_grid_{region}_50km.nc",
    lat_bounds=lat_bounds,
    lon_bounds=lon_bounds,
    res_km=50,
    epsg=5070,
)


# ------------------------
# [FIX 10] conform each source before merging
# ------------------------

LATLON_NAMES = ["lat", "lon", "LAT", "LON", "latitude", "longitude"]


def conform(ds, name, grid, time_index, fill_value=np.nan):
    """
    Put a source dataset on exactly the template x/y and the full hourly axis.
    Raises if the x/y differ by more than 1 m (i.e. a real grid mismatch),
    instead of letting xr.merge silently align or override.
    """
    ds = ds.drop_vars(LATLON_NAMES, errors="ignore")

    for dim, ref in (("x", grid.x_centers), ("y", grid.y_centers)):
        if ds.sizes.get(dim) != len(ref) or not np.allclose(ds[dim].values, ref, atol=1.0):
            raise ValueError(f"{name}: '{dim}' coordinate does not match the grid template")
    ds = ds.assign_coords(x=grid.x_centers, y=grid.y_centers)   # bit-identical coords

    if "time" in ds.dims:
        ds = ds.assign_coords(time=pd.to_datetime(ds["time"].values))
        extra = set(ds["time"].values) - set(time_index.values)
        if extra:
            print(f"  {name}: dropping {len(extra)} timestamps not on the hourly axis")
        ds = ds.reindex(time=time_index, fill_value=fill_value)  # default: gaps -> NaN
    return ds


# ========================
# PROCESS ONE DAY
# ========================

def process_one_day(date):
    day_start = datetime(date.year, date.month, date.day, 0, 0)
    day_end = datetime(date.year, date.month, date.day, 23, 0)
    time_index = pd.date_range(day_start, day_end, freq="1h")

    start_str = day_start.strftime("%Y-%m-%d %H:%M")
    end_str = day_end.strftime("%Y-%m-%d %H:%M")
    date_tag = day_start.strftime("%Y%m%d")

    # version is stored inside the file (processing_version attribute)
    output_file = f"{PATH}/daily/weather_{region}_{date_tag}.nc"
    if os.path.exists(output_file):
        with xr.open_dataset(output_file) as old:
            old_version = old.attrs.get("processing_version")
            old_failed = int(old.attrs.get("n_failed_files", 0)) + int(old.attrs.get("rtma_n_failed_files", 0))
        if old_version == PROCESSING_VERSION and old_failed == 0:
            print(f"✓ Already exists: {output_file}")
            return
        print(f"Rebuilding {output_file}: version {old_version} (current {PROCESSING_VERSION}), "
              f"{old_failed} failed source file(s)")

    print(f"\nProcessing {date_tag}")

    # ------------------------ SWDI ------------------------
    swdi_outdir = Path(PATH) / "swdi" / "swdi_raw"
    swdi_outdir.mkdir(parents=True, exist_ok=True)

    print("Downloading SWDI...")
    download_swdi(datasets=swdi_variables, fmt="csv",
                  start=day_start, end=day_end, outdir=swdi_outdir)

    swdi_vars_to_keep = {
        "nx3tvs": ["MAX_SHEAR", "MXDV"],
        "nx3hail": ["PROB", "MAXSIZE"],
        "nx3structure": ["VIL", "MAX_REFLECT"],
    }
    print("Aggregating SWDI hourly...")
    swdi_datasets = []
    for product in swdi_variables:
        ds_product = aggregate_swdi(csv_dir=swdi_outdir, product_type=product,
                                    date=day_start, grid_template=grid_template,
                                    variables=swdi_vars_to_keep.get(product))
        ds_product = ds_product.rename({"ZTIME": "time", "Y": "y", "X": "x"})
        # SWDI is event-based: an hour with no detections is a real 0, not missing
        swdi_datasets.append(conform(ds_product, f"SWDI {product}",
                                     grid_template, time_index, fill_value=0.0))
    ds_swdi = xr.merge(swdi_datasets, compat="no_conflicts", join="exact")

    # ------------------------ MRMS ------------------------
    print("Downloading & aggregating MRMS...")
    ds_mrms = download_and_grid(root=PATH, region=region, grid=grid_template,
                                variables=mrms_variables,
                                start_date=start_str, end_date=end_str,
                                lat_bounds=lat_bounds, lon_bounds=lon_bounds)
    ds_mrms = conform(ds_mrms, "MRMS", grid_template, time_index)

    # ------------------------ RTMA ------------------------
    print("Downloading & aggregating RTMA...")
    ds_rtma = download_and_grid_rtma(day_start, day_end, grid_template, root=PATH)
    ds_rtma = conform(ds_rtma, "RTMA", grid_template, time_index)

    # ------------------------ MERGE & SAVE ------------------------
    print("Merging datasets...")
    ds_daily = xr.merge([ds_swdi, ds_mrms, ds_rtma],
                        compat="no_conflicts", join="exact")   # [FIX 10]
    ds_daily = add_latlon(ds_daily, epsg_proj=grid_template.epsg)

    ds_daily.attrs.update({
        "n_failed_files": int(ds_mrms.attrs.get("n_failed_files", 0)),
        "rtma_n_failed_files": int(ds_rtma.attrs.get("rtma_n_failed_files", 0)),
        "region": region,
        "date": date_tag,
        "created": datetime.now(timezone.utc).isoformat(),
        "processing_version": PROCESSING_VERSION,
        "missing_convention": "NaN = no data/no coverage; 0 = observed zero",
    })

    os.makedirs(f"{PATH}/daily", exist_ok=True)
    encoding = {v: {"zlib": True, "complevel": 4} for v in ds_daily.data_vars}
    ds_daily = apply_metadata(ds_daily)          # long_name / units / description
    atomic_to_netcdf(ds_daily, output_file, encoding=encoding)
    if not (Path(PATH) / "daily" / "DATA_DICTIONARY.md").exists():
        write_data_dictionary(ds_daily, Path(PATH) / "daily")
    print(f"Saved {output_file}")

    del ds_swdi, ds_mrms, ds_rtma, ds_daily


def run_range(start_date, end_date):
    current = start_date
    while current <= end_date:
        try:
            process_one_day(current)
        except Exception as e:
            print(f"Failed on {current}: {e}")
        current += timedelta(days=1)


if __name__ == "__main__":
    run_range(datetime(2024, 6, 1), datetime(2024, 6, 30))
