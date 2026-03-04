#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Created on Thu Nov  6 12:35:24 2025

@author: marchett
"""

import boto3
from botocore import UNSIGNED
from botocore.client import Config
from pathlib import Path
import argparse
from grid import AnalysisGridTemplate
from datetime import datetime, timedelta
from collections import defaultdict
from scipy.stats import binned_statistic_2d
import numpy as np
import os

os.environ["ECCODES_SILENT"] = "1"
os.environ["ECCODES_IGNORE_INDEX_FILE"] = "1"

import xarray as xr
import gzip
import shutil
import pandas as pd
import re




# -----------------------------------------
# List hourly files
# -----------------------------------------
def list_hourly_files(region, variable, date, s3, bucket):
    """
    List first MRMS file per hour for a given date, region, and variable
    """
    prefix = f"{region}/{variable}/{date.strftime('%Y%m%d')}/"
    paginator = s3.get_paginator('list_objects_v2')
    hourly_files = []
    hour_seen = defaultdict(bool)

    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get('Contents', []):
            key = obj['Key']
            # Extract hour from filename: MRMS_VIL_Density_00.50_YYYYMMDD-HHMMSS.nc.gz
            try:
                hh = int(key.split('-')[1][:2])
            except Exception:
                continue
            if not hour_seen[hh]:
                hourly_files.append(key)
                hour_seen[hh] = True

    return sorted(hourly_files)



# ------------------------------
# Download file function
# ------------------------------

def download_file(key, s3, bucket, local_dir="downloads"):
    """
    Download a single file from S3, un-gzip if needed
    """
    os.makedirs(local_dir, exist_ok=True)
    filename = os.path.join(local_dir, os.path.basename(key))
    if os.path.exists(filename.replace('.gz','')):
        return filename.replace('.gz','')  # already downloaded

    tmp_file = filename
    s3.download_file(bucket, key, tmp_file)

    # unzip if gzipped
    if tmp_file.endswith('.gz'):
        unzipped_file = tmp_file[:-3]
        with gzip.open(tmp_file, 'rb') as f_in:
            with open(unzipped_file, 'wb') as f_out:
                shutil.copyfileobj(f_in, f_out)
        os.remove(tmp_file)
        return unzipped_file
    return tmp_file



# ------------------------------
# Load MRMS NetCDF and subset by lat/lon
# ------------------------------

def load_and_subset_nc(file_path, variable, lat_bounds=None, lon_bounds=None):
    """
    Load MRMS NetCDF and optionally subset by lat/lon
    """
    
    ds = xr.open_dataset(file_path, engine="cfgrib")


    if "unknown" in ds.data_vars:
        ds = ds.rename({"unknown": variable})

    if lat_bounds and lon_bounds:
        # convert -180-180 to 0-360 if needed
        lon0, lon1 = lon_bounds
        if lon0 < 0:
            lon0 += 360
        if lon1 < 0:
            lon1 += 360

        # check latitude order
        lat_start = lat_bounds[0]
        lat_end   = lat_bounds[1]
        if ds.latitude.values[0] > ds.latitude.values[-1]:
            # descending → reverse slice
            lat_start, lat_end = lat_end, lat_start

        ds = ds.sel(
            latitude=slice(lat_start, lat_end),
            longitude=slice(lon0, lon1)
        )
    
    return ds




def extract_time_from_key(key):
    """
    Parse MRMS filename timestamp YYYYMMDD-HHMMSS → np.datetime64
    Example: MRMS_VIL_Density_00.50_20230331-000038.grib2.gz
    """
    m = re.search(r"(\d{8})-(\d{6})", key)
    if not m:
        raise ValueError(f"Cannot parse time from {key}")

    d, t = m.group(1), m.group(2)

    iso = f"{d[:4]}-{d[4:6]}-{d[6:]}T{t[:2]}:{t[2:4]}:{t[4:]}"
    return np.datetime64(iso)



# ------------------------------
#  grid to AnalysisGridTemplate
# ------------------------------


def bin_to_grid(ds, template, var_name, statistic='mean', 
                    coarsen_factor=None):
    
    """
    Vectorized binning of MRMS variable onto AnalysisGridTemplate
    """
    
    data_var = ds[var_name]

    # Optional coarsening
    if coarsen_factor is not None:
        lat_factor, lon_factor = coarsen_factor
        data_var = data_var.coarsen(latitude=lat_factor, 
                                    longitude=lon_factor, 
                                    boundary='trim').mean()
    
    # Get 2D lat/lon matching data_var
    if 'latitude' in data_var.dims and 'longitude' in data_var.dims:
        lat2d = ds['latitude'].values
        lon2d = ds['longitude'].values
        # sometimes latitude/longitude are 1D; make them 2D
        if lat2d.ndim == 1 and lon2d.ndim == 1:
            lon2d, lat2d = np.meshgrid(lon2d, lat2d)
    else:
        raise ValueError("Dataset must have latitude and longitude coordinates")

    # Flatten to 1D for binning
    lat_flat = lat2d.flatten()
    lon_flat = lon2d.flatten()
    data_flat = data_var.values.flatten()
    
    # Remove NaNs (optional but recommended)
    mask = ~np.isnan(data_flat)
    lon_flat = lon_flat[mask]
    lat_flat = lat_flat[mask]
    data_flat = data_flat[mask]

    # Transform to projected coordinates
    xs, ys = template.to_proj.transform(lon_flat, lat_flat)

    # Compute edges from template centers
    x_centers = template.grid.x.values
    y_centers = template.grid.y.values
    dx = np.diff(x_centers).mean()
    dy = np.diff(y_centers).mean()
    x_edges = np.concatenate([[x_centers[0]-dx/2], x_centers + dx/2])
    y_edges = np.concatenate([[y_centers[0]-dy/2], y_centers + dy/2])

    # 2D binning
    stat, _, _, _ = binned_statistic_2d(
        xs, ys, data_flat,
        statistic=statistic,
        bins=[x_edges, y_edges]
    )

    arr = stat.T
    arr = np.nan_to_num(arr, nan=0.0)
    
    return arr



def add_latlon(ds, epsg_proj=5070):

    import pyproj
    
    # --- Create transformer from projected CRS to WGS84
    transformer = pyproj.Transformer.from_crs(f"EPSG:{epsg_proj}", 
                                              "EPSG:4326", always_xy=True)

    # --- Make 2D grid of x/y
    x = ds['x'].values
    y = ds['y'].values
    xx, yy = np.meshgrid(x, y)

    # --- Transform to lat/lon
    lon2d, lat2d = transformer.transform(xx, yy)

    # --- Add as new variables
    ds_out = ds.copy()
    ds_out['lat'] = (('y','x'), lat2d)
    ds_out['lon'] = (('y','x'), lon2d)


    return ds_out




# ------------------------------
# full pipeline --> download, subset, combine
# ------------------------------



def download_and_grid(
    region,
    variables,
    start_date,
    end_date,
    root,
    lat_bounds,
    lon_bounds,
    res_km=50,
    epsg=5070,
    grid=None,
    output_file=None,
):
    """
    Download MRMS hourly files for a date range and combine into a single nc
    """

    bucket = "noaa-mrms-pds"
    s3 = boto3.client("s3", config=Config(signature_version=UNSIGNED))

    root = Path(root)

    download_dir = root / "mrms/mrms_raw"
    output_dir = root / "mrms"
    download_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    if output_file is None:
        output_file = output_dir / f"MRMS_{region}_{start_date}_{end_date}.nc"
    else:
        output_file = Path(output_file)

    if output_file.exists():
        print("Output exists, skipping.")
        return xr.open_dataset(output_file)

    if grid is None:
        grid = AnalysisGridTemplate(
            grid_file=output_dir / f"MRMS_{region}_grid_{res_km}km.nc",
            lat_bounds=lat_bounds,
            lon_bounds=lon_bounds,
            res_km=res_km,
            epsg=epsg,
        )

    # ---- CLEAN HOURLY TIME AXIS ----
    start_dt = pd.to_datetime(start_date).floor("h")
    end_dt = pd.to_datetime(end_date).floor("h")
    # end_dt = end_dt + pd.Timedelta(days=1) - pd.Timedelta(hours=1)
    time_index = pd.date_range(start=start_dt, end=end_dt, freq="1h").to_pydatetime()

    # Storage for each variable
    var_data = {var: [] for var in variables}

    date = start_dt
    while date <= end_dt:
        print(f"Processing {date.date()}...")

        for variable in variables:
            hourly_keys = list_hourly_files(region, variable, date, s3, bucket)
            #print(f"  Found {len(hourly_keys)} hourly files for {variable}")
            print(f"{len(hourly_keys)} files found for {variable} on {date}")
            #print([extract_time_from_key(k) for k in hourly_keys])

            for key in hourly_keys:
                file_path = download_file(key, s3, bucket, str(download_dir))
                ds_raw = load_and_subset_nc(file_path, variable, lat_bounds, lon_bounds)

                # ---- bin to template grid ----
                arr = bin_to_grid(ds_raw, grid, variable)

                # ---- delete downloaded files ----
                try:
                    p = Path(file_path)
                    if p.exists():
                        p.unlink()
                    idx_file = p.with_name(p.name + ".5b7b6.idx")
                    if idx_file.exists():
                        idx_file.unlink()
                except Exception as e:
                    print(f"Warning: could not delete {file_path}: {e}")

                # ---- create dataset for this hour ----
                ds = xr.Dataset(
                    {variable: (("y", "x"), arr)},
                    coords={"y": grid.y_centers, "x": grid.x_centers},
                )

                # ---- normalize timestamp to exact hour ----
                raw_time = pd.to_datetime(extract_time_from_key(key))
                time_val = raw_time.replace(minute=0, second=0, microsecond=0)
                if time_val not in time_index:
                    print(f"  Adjusted time {time_val} not in clean hourly index, skipping")
                    continue

                ds = ds.assign_coords(time=time_val)
                ds = ds.expand_dims("time")
                var_data[variable].append(ds)

        date += pd.Timedelta(days=1)

    # ---- CONCAT + SAFE REINDEX ----
    combined_vars = []
    for variable, ds_list in var_data.items():
        if ds_list:
            combined_var = xr.concat(ds_list, dim="time")
            combined_var = combined_var.sortby("time")
            combined_var = combined_var.drop_duplicates("time")
            combined_var = combined_var.reindex(time=time_index)
            combined_vars.append(combined_var)

    if not combined_vars:
        raise ValueError("No MRMS data found for requested period.")

    combined = xr.merge(combined_vars)

    # Add lat/lon coordinates once
    combined = add_latlon(combined, epsg_proj=epsg)
    
    # ---- DEBUG: check final hourly time axis ----
    # if "time" in combined.coords:
    #     print("Final hourly timestamps in combined MRMS dataset:")
    #     print(combined["time"].values)
    #     print(f"Total hours: {len(combined['time'])}")
    # else:
    #     print("No 'time' coordinate found in combined dataset!")


    combined.to_netcdf(output_file)
    print(f"Saved combined NetCDF to {output_file}")

    return combined





def main():
    parser = argparse.ArgumentParser()
    
    # ----------------------------
    # Defaults
    # ----------------------------
    root = "/Users/marchett/Documents/Disasters"
    region = "CONUS"
    start = "2024-06-25"
    end = "2024-06-25"
    variables = [
        "VIL_Density_00.50",
        "ReflectivityAtLowestAltitude_00.50",
    ]
    lat_bounds = [25, 50]
    lon_bounds = [-125, -65]

    parser.add_argument("--region", default=region)
    parser.add_argument("--start", default=start)
    parser.add_argument("--end", default=end)
    parser.add_argument("--root", default=root)
    parser.add_argument("--variables", nargs="+", default=variables)
    parser.add_argument("--latmin", type=float, default=lat_bounds[0])
    parser.add_argument("--latmax", type=float, default=lat_bounds[1])
    parser.add_argument("--lonmin", type=float, default=lon_bounds[0])
    parser.add_argument("--lonmax", type=float, default=lon_bounds[1])

    args = parser.parse_args()

    download_and_grid(
        region=args.region,
        variables=args.variables,
        start_date=args.start,
        end_date=args.end,
        root=args.root,
        lat_bounds=[args.latmin, args.latmax],
        lon_bounds=[args.lonmin, args.lonmax],
    )


if __name__ == "__main__":
    main()

    














