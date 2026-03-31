#!/usr/bin/env python3

import sys
import os
import rioxarray
import pandas as pd
import geopandas as gpd
import numpy as np
import xarray as xr
from shapely.geometry import Point
from grid import AnalysisGridTemplate

# ------------------------
# Paths and inputs
# ------------------------

PATH = "/mnt/data1/MultiHazard/"
OUT_REL_PATH = "data/daily/firms_fire/"

FIRES_CSV = [
    "/mnt/data1/MultiHazard/data/spread_data_firms/fire_archive_J1V-C2_731498.csv",
    "/mnt/data1/MultiHazard/data/spread_data_firms/fire_archive_M-C61_731496.csv",
    "/mnt/data1/MultiHazard/data/spread_data_firms/fire_archive_SV-C2_731500.csv",
    "/mnt/data1/MultiHazard/data/spread_data_firms/fire_nrt_J2V-C2_731499.csv",
    "/mnt/data1/MultiHazard/data/spread_data_firms/fire_nrt_LS_731497.csv",
]

# ------------------------
# Load and concatenate FIRMS CSVs
# ------------------------

df_out = None
df_all = None
dfs = []
for fn in FIRES_CSV:
    df_tmp = pd.read_csv(fn)
    dfs.append(df_tmp)

df_all = pd.concat(dfs, ignore_index=True)

df_all["latitude"] = pd.to_numeric(df_all["latitude"], errors="coerce")
df_all["longitude"] = pd.to_numeric(df_all["longitude"], errors="coerce")
df_all["frp"] = pd.to_numeric(df_all["frp"], errors="coerce")
df_all = df_all.dropna(subset=["frp"])
df_all["frp"] = df_all["frp"].astype(float)
df_all = df_all.dropna(subset=["latitude", "longitude"])

df_all = df_all[
    (df_all["longitude"] >= -180) & (df_all["longitude"] <= 180) &
    (df_all["latitude"] >= -90) & (df_all["latitude"] <= 90)
]

df_out = gpd.GeoDataFrame(
    df_all,
    geometry=gpd.points_from_xy(df_all["longitude"], df_all["latitude"]),
    crs="EPSG:4326",
)

 
print("Completed initial load and concat")

# ------------------------
# Build timestamps and hourly aggregation in lat/lon space
# ------------------------

# Ensure date/time are strings with consistent format
df_out["acq_date"] = df_out["acq_date"].astype(str).str.replace("-", "/")
df_out["acq_time"] = df_out["acq_time"].astype(str).str.zfill(4)  # '930' -> '0930'

# Combine and parse: FIRMS format is typically YYYY/MM/DD and HHMM
df_out["__ts"] = pd.to_datetime(
    df_out["acq_date"] + " " + df_out["acq_time"],
    format="%Y/%m/%d %H%M",
    errors="coerce",
)
df_out = df_out.dropna(subset=["__ts"])
print("Parsed timestamps range:", df_out["__ts"].min(), "to", df_out["__ts"].max())

# Hour bin key
df_out["hour_ts"] = df_out["__ts"].dt.floor("h")

# Aggregate FRP per (hour, lat, lon)
df_out_hourly = (
    df_out.groupby(["hour_ts", "latitude", "longitude"], as_index=False)
    .agg({"frp": "mean"})
    .rename(columns={"hour_ts": "__ts"})
)

df_out_hourly["__ts"] = df_out_hourly["__ts"].dt.tz_localize(None)

fires = gpd.GeoDataFrame(
    df_out_hourly.copy(),
    geometry=gpd.points_from_xy(df_out_hourly["longitude"], df_out_hourly["latitude"]),
    crs="EPSG:4326",
)
print("Hourly fire points:", fires.shape, fires.iloc[0])
print("Completed timestamp build and initial agg.")
# ------------------------
# Create analysis grid
# ------------------------

start_date = "2023-01-01"
end_date = "2024-12-31"

start_ts = pd.Timestamp(start_date)
end_ts = pd.Timestamp(end_date)

lat_bounds = [25, 50]
lon_bounds = [-125, -65]

grid_template = AnalysisGridTemplate(
    grid_file=f"{PATH}/grid/analysis_grid.nc",
    lat_bounds=lat_bounds,
    lon_bounds=lon_bounds,
    res_km=50,
    epsg=5070,
)


# Reproject fires to grid CRS
print(f"EPSG:{grid_template.epsg}")


fires = fires[    (fires.geometry.x >= lon_bounds[0]) & (fires.geometry.x <= lon_bounds[1]) &
    (fires.geometry.y >= lat_bounds[0]) & (fires.geometry.y <= lat_bounds[1])
]


fires_5070 = fires.to_crs(f"EPSG:{grid_template.epsg}")
fires_5070 = fires_5070[np.isfinite(fires_5070.geometry.x) & np.isfinite(fires_5070.geometry.y)].copy()

x_centers = grid_template.grid.coords["x"].values
y_centers = grid_template.grid.coords["y"].values
ny, nx = len(y_centers), len(x_centers)

# Build grid points GeoDataFrame
grid_x, grid_y = np.meshgrid(x_centers, y_centers)
grid_points = gpd.GeoDataFrame(
    {
        "x": grid_x.ravel(),
        "y": grid_y.ravel(),
        "geometry": [Point(x, y) for x, y in zip(grid_x.ravel(), grid_y.ravel())],
    },
    crs=f"EPSG:{grid_template.epsg}",
)

grid_points = grid_points.reset_index().rename(columns={"index": "grid_id"})

fires_with_grid = gpd.sjoin_nearest(
    fires_5070,
    grid_points[["x", "y", "geometry", "grid_id"]],
    how="left",
    max_distance=50000,  # 50 km
    distance_col="dist_to_grid",
)
 
fires_with_grid = fires_with_grid.dropna(subset=["grid_id"])

fires_with_grid["__ts"] = pd.to_datetime(fires_with_grid["__ts"])
fires_with_grid["hour_ts"] = fires_with_grid["__ts"].dt.floor("h")

## Sum FRP per (hour, grid cell)
grid_hourly = (
    fires_with_grid.groupby(["hour_ts", "grid_id"], as_index=False)
    .agg({"frp": "sum"})
)

grid_hourly["hour_ts"] = pd.to_datetime(grid_hourly["hour_ts"])

print("Completed projection to grid")

# ------------------------
# Fill missing hours per grid cell
# ------------------------

# Use requested range; change to min/max of data if preferred
full_time = pd.date_range(
    start=start_ts.floor("h"),
    end=end_ts.ceil("h"),
    freq="h",
)

filled_list = []
for gid, sub in grid_hourly.groupby("grid_id"):
    sub = sub.set_index("hour_ts").sort_index()
    sub = sub.reindex(full_time)
    sub["grid_id"] = gid
    # If you want last-observation-carried-forward, use ffill():
    sub["frp"] = sub["frp"].ffill()
    # If you prefer "0 when no fire detected":
    #sub["frp"] = sub["frp"].fillna(0.0)
    sub = sub.reset_index().rename(columns={"index": "hour_ts"})
    filled_list.append(sub)

grid_hourly = pd.concat(filled_list, ignore_index=True)

print("Completed missing hours fill")
# ------------------------
# Map grid_id to (iy, ix)
# ------------------------

grid_points_idx = grid_points[["grid_id", "x", "y"]].copy()

grid_points_idx["ix"] = grid_points_idx["x"].map(
    lambda xv: int(np.argmin(np.abs(x_centers - xv)))
)
grid_points_idx["iy"] = grid_points_idx["y"].map(
    lambda yv: int(np.argmin(np.abs(y_centers - yv)))
)

lookup = grid_points_idx.set_index("grid_id")[["iy", "ix"]]

grid_hourly["hour_ts"] = pd.to_datetime(grid_hourly["hour_ts"])
grid_hourly["grid_id"] = grid_hourly["grid_id"].astype(int)

grid_hourly = grid_hourly.merge(
    lookup,
    left_on="grid_id",
    right_index=True,
    how="left",
)

print("Completed 2D grid index generation")
# ------------------------
# Build 3D FRP array (time, y, x)
# ------------------------

times = np.sort(grid_hourly["hour_ts"].unique())
nt = len(times)
time_index = {t: i for i, t in enumerate(times)}

frp_3d = np.full((nt, ny, nx), np.nan, dtype="float32")

# Aggregate to single (time, iy, ix) if there are duplicates
grid_hourly_agg = (
    grid_hourly.groupby(["hour_ts", "iy", "ix"], as_index=False)
    .agg({"frp": "sum"})
)

for _, row in grid_hourly_agg.iterrows():
    it = time_index[row["hour_ts"]]
    iy = int(row["iy"])
    ix = int(row["ix"])
    frp_3d[it, iy, ix] = row["frp"]


print("Completed building 3D FRP data")
# ------------------------
# Build Dataset and save per day
# ------------------------

ds = xr.Dataset(
    data_vars={
        "frp": (("time", "y", "x"), frp_3d),
    },
    coords={
        "time": times,
        "y": y_centers,
        "x": x_centers,
    },
    attrs={
        "description": "Fire events aggregated to projected grid",
        "proj": f"EPSG:{grid_template.epsg}",
        "time_resolution": "hourly",
    },
)

# Add lat/lon from template
ds["lat"] = (("y", "x"), grid_template.grid["lat"].values)
ds["lon"] = (("y", "x"), grid_template.grid["lon"].values)
ds["lat"].attrs.update(
    {"units": "degrees_north", "standard_name": "latitude"}
)
ds["lon"].attrs.update(
    {"units": "degrees_east", "standard_name": "longitude"}
)

ds["frp"].attrs["units"] = "MW"
ds["frp"].attrs["long_name"] = "Fire radiative power"
ds.attrs["title"] = "Hourly FRP on 50 km grid"


print("Final XR Dataset generated")
# Optionally write a single big file
# ds.to_netcdf(f"{PATH}/fires_analysis_grid_hourly_all.nc")

# Optional: encoding for compression
encoding = {
    "frp": {
        "zlib": True,
        "complevel": 4,
        "dtype": "float32",
    }
}

# Per-day files within requested range
dates = pd.to_datetime(ds["time"].values).normalize()
unique_days = np.unique(dates)

for day in unique_days:
    day_ts = pd.Timestamp(day)
    if not (start_ts <= day_ts <= end_ts):
        continue

    day_str = day_ts.strftime("%Y%m%d")
    day_mask = pd.to_datetime(ds["time"].values).normalize() == day_ts
    ds_day = ds.isel(time=day_mask)

    if ds_day.sizes.get("time", 0) == 0:
        continue

    out_path = f"{PATH}/{OUT_REL_PATH}/fires_analysis_grid_{day_str}.nc"
    ds_day.to_netcdf(out_path, encoding=encoding)
    print("Saved", out_path)

print("Saved aggregated fires to NetCDF files.")
