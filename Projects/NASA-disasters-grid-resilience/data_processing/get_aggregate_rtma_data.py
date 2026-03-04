#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Created on Tue Mar  3 15:39:43 2026

@author: marchett
"""

import numpy as np
import re, os
import xarray as xr
from get_aggregate_mrms_data import bin_to_grid
from get_aggregate_mrms_data import add_latlon
import boto3
from botocore import UNSIGNED
from botocore.client import Config
from pathlib import Path
import pandas as pd
from datetime import timedelta, datetime
import cfgrib
import pygrib


# ------------------------
# RTMA CONFIG
# ------------------------

# rtma_variables = [
#     "Temperature_height_above_ground",
#     "Dewpoint_temperature_height_above_ground",
#     "wind_speed_height_above_ground",
#     "wind_gust_height_above_ground",
#     "u_component_of_wind_height_above_ground",
#     "v_component_of_wind_height_above_ground",
#     "wind_from_direction_height_above_ground"
# ]

# # optional rename for convenience
# rtma_rename = {
#     "Temperature_height_above_ground": "TMP_2m",
#     "Dewpoint_temperature_height_above_ground": "DPT_2m",
#     "wind_speed_height_above_ground": "WIND_10m",
#     "wind_gust_height_above_ground": "GUST_10m",
#     "u_component_of_wind_height_above_ground": "UGRD_10m",
#     "v_component_of_wind_height_above_ground": "VGRD_10m",
#     "wind_from_direction_height_above_ground": "WDIR_10m",
# }


RTMA_VARS = {
    "10u": "u10",        # u-component 10m wind
    "10v": "v10",        # v-component 10m wind
    "10si": "wspd10",    # wind speed 10m
    "10wdir": "wdir10",  # wind direction 10m
    "2t": "t2m",         # 2m temperature
    "2d": "d2m",         # 2m dew point
    "2sh": "sh2",        # 2m specific humidity
    "sp": "sp",          # surface pressure
    "vis": "vis",        # visibility
    "ceil": "ceil",      # cloud ceiling
    "tcc": "tcc",        # total cloud cover
    "orog": "orog",      # surface elevation
    "i10fg": "i10fg"     # icing forecast (if present)
}



def list_rtma_files(date, s3):
    """
    List RTMA 2D surface analysis GRIB2_wexp files for a given date.
    """
    prefix = f"rtma2p5.{date.strftime('%Y%m%d')}/"
    paginator = s3.get_paginator('list_objects_v2')
    files = []

    for page in paginator.paginate(Bucket="noaa-rtma-pds", Prefix=prefix):
        for obj in page.get('Contents', []):
            key = obj['Key']
            # Only grab the hourly analysis _wexp files
            if key.endswith(".grb2_wexp") and ".2dvaranl_ndfd" in key:
                files.append(key)

    return sorted(files)



def download_rtma(key, s3, local_dir="downloads/rtma"):
    """
    Download a single RTMA GRIB2 file from S3
    """
    import os
    os.makedirs(local_dir, exist_ok=True)
    local_file = os.path.join(local_dir, os.path.basename(key))
    if os.path.exists(local_file):
        return local_file
    s3.download_file("noaa-rtma-pds", key, local_file)
    return local_file




def load_rtma(file_path):
    """
    Load an RTMA GRIB2 file and return xarray Dataset
    with variables having 'latitude' and 'longitude' as dims,
    ready for bin_to_grid.
    """
    grbs = pygrib.open(file_path)
    data = {}
    lats2d, lons2d = None, None

    for grb in grbs:
        if grb.shortName in RTMA_VARS:
            var_name = RTMA_VARS[grb.shortName]

            if lats2d is None or lons2d is None:
                lats2d, lons2d = grb.latlons()

            # Use latitude/longitude as dims
            da = xr.DataArray(
                grb.values,
                dims=("latitude", "longitude"),
                coords={"latitude": (("latitude","longitude"), lats2d),
                        "longitude": (("latitude","longitude"), lons2d)}
            )
            data[var_name] = da

    grbs.close()

    if not data:
        raise ValueError(f"No variables found in {file_path} matching RTMA_VARS.")

    ds = xr.Dataset(data)

    # extract timestamp
    m_hour = re.search(r"t(\d{2})z", file_path)
    m_date = re.search(r"rtma2p5\.(\d{8})", file_path)
    if m_hour and m_date:
        hour = int(m_hour.group(1))
        date = datetime.datetime.strptime(m_date.group(1), "%Y%m%d")
        timestamp = pd.Timestamp(date + datetime.timedelta(hours=hour))
        ds = ds.assign_coords(time=timestamp)

    return ds





def download_and_grid_rtma(day_start, day_end, grid_template, root):
    """
    Download RTMA files for a date range, subset variables, and bin to grid
    """

    s3 = boto3.client("s3", config=Config(signature_version=UNSIGNED))
    current = day_start
    rtma_dir = Path(root) / "rtma" / "raw"
    rtma_dir.mkdir(parents=True, exist_ok=True)

    var_data = {var: [] for var in RTMA_VARS.values()}

    while current <= day_end:
        print(f"Processing RTMA {current.date()}...")
        keys = list_rtma_files(current, s3)
        for key in keys:
            local_file = download_rtma(key, s3, local_dir=str(rtma_dir))
            ds_raw = load_rtma(local_file)
            
            # bin each variable onto AnalysisGridTemplate
            for var in ds_raw.data_vars:
                arr = bin_to_grid(ds_raw, grid_template, var)
                ds_var = xr.Dataset({var: (("y","x"), arr)},
                                    coords={"y": grid_template.y_centers,
                                            "x": grid_template.x_centers})
                # assign time from filename
                time_str = re.search(r"t(\d{2})z", key).group(1)
                hour = int(time_str[:2])
                ds_var = ds_var.assign_coords(time=pd.Timestamp(current.year,
                                                                 current.month,
                                                                 current.day,
                                                                 hour))
                var_data[var].append(ds_var)
        
        
            # DELETE the raw GRIB2 file
            try:
                os.remove(local_file)
                # print(f"Deleted raw GRIB2 file: {local_file}")
            except Exception as e:
                print(f"Failed to delete {local_file}: {e}")

        current += timedelta(days=1)
    
    # merge all hours
    combined_vars = []
    for var, ds_list in var_data.items():
        if ds_list:
            combined_var = xr.concat(ds_list, dim="time")
            combined_var = combined_var.sortby("time")
            combined_var = combined_var.drop_duplicates("time")
            combined_vars.append(combined_var)
    if not combined_vars:
        raise ValueError("No RTMA data found for requested period.")

    combined = xr.merge(combined_vars)
    combined = add_latlon(combined, epsg_proj=grid_template.epsg)
    return combined



