import os
import glob

import pandas as pd
import geopandas as gpd
import numpy as np
import xarray as xr
from shapely.geometry import Point
from grid import AnalysisGridTemplate

PATH = "/mnt/data1/MultiHazard/"
OUT_REL_PATH = "data/daily/uci_fire/"

FIRES_GPDS = [
"/mnt/data1/MultiHazard/data/spread_data/feds_western_us_2020_af_postprocessed.parquet",
"/mnt/data1/MultiHazard/data/spread_data/feds_western_us_2021_af_postprocessed.parquet"
]


start_date = "2020-01-03"
end_date = "2021-01-31"
start_ts = pd.Timestamp(start_date)
end_ts = pd.Timestamp(end_date)

lat_bounds = [25, 50]
lon_bounds = [-125, -65]

# ------------------------
# Load and concatenate with polygon hull geometry
# ------------------------

gdfs = []
for fn in FIRES_GPDS:
    print(f"Reading {fn}")
    df_tmp = gpd.read_parquet(fn)

    # Ensure hull is the active geometry
    if "hull" not in df_tmp.columns:
        raise KeyError("Expected a 'hull' column with polygon geometry.")

    df_tmp = df_tmp.set_geometry("hull")

    if df_tmp.crs is None:
        df_tmp = df_tmp.set_crs("EPSG:4326")
    else:
        df_tmp = df_tmp.to_crs("EPSG:4326")

    gdfs.append(df_tmp)

if not gdfs:
    raise RuntimeError(f"No files found in {os.path.dirname(FIRES_GPDS)}")

perims = gpd.GeoDataFrame(
    pd.concat(gdfs, ignore_index=True),
    geometry="hull",
    crs="EPSG:4326",
)
perims.reset_index(drop=True, inplace=True)
print("Total perimeter records:", len(perims))
print(perims.shape)
print(perims.iloc[0])
print(perims.iloc[-1])
# ------------------------
# Parse time and filter
# ------------------------

if "t_ed" not in perims.columns:
    raise KeyError("Expected a 't_ed' column for timestamps in the schema.")

perims["__ts"] = pd.to_datetime(perims["t_ed"], errors="coerce")
perims = perims.dropna(subset=["__ts"])
perims["__ts"] = perims["__ts"].dt.tz_localize(None)

perims = perims[(perims["__ts"] >= start_ts) & (perims["__ts"] <= end_ts)]
print("Time-filtered perimeters:", len(perims))

# ------------------------
# Choose quantity to aggregate (e.g., meanFRP)
# ------------------------

value_col = "meanFRP"  # or "farea", "flinelen", etc.
if value_col not in perims.columns:
    raise KeyError(f"Expected a '{value_col}' column in the schema.")

perims[value_col] = pd.to_numeric(perims[value_col], errors="coerce")
perims = perims.dropna(subset=[value_col])

# ------------------------
# Hourly aggregation by time and polygon (no lat/lon grouping)
# ------------------------

perims["hour_ts"] = perims["__ts"].dt.floor("h")

group_keys = ["hour_ts"]
if "fireid" in perims.columns:
    group_keys.append("fireid")

perims_hourly = (
    perims
    .groupby(group_keys, as_index=False)
    .agg({value_col: "mean"})
)

# Merge back geometry and any ID columns from original perims
perims_hourly = perims_hourly.merge(
    perims[group_keys + ["hull"]].drop_duplicates(),
    on=group_keys,
    how="left",
)

perims_hourly["__ts"] = perims_hourly["hour_ts"].dt.tz_localize(None)

fires = gpd.GeoDataFrame(
    perims_hourly.copy(),
    geometry="hull",
    crs="EPSG:4326",
)
print("Hourly fire polygons:", fires.shape)

# ------------------------
# Create analysis grid
# ------------------------

grid_template = AnalysisGridTemplate(
    grid_file=f"{PATH}/grid/analysis_grid.nc",
    lat_bounds=lat_bounds,
    lon_bounds=lon_bounds,
    res_km=50,
    epsg=5070,
)

fires_5070 = fires.to_crs(f"EPSG:{grid_template.epsg}")
print(fires_5070.iloc[0])
print(fires_5070.iloc[-1])


x_centers = grid_template.grid.coords["x"].values
y_centers = grid_template.grid.coords["y"].values
ny, nx = len(y_centers), len(x_centers)

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

print("Completed analysis grid generation")
# ------------------------
# Assign each polygon to nearest grid cell center
# ------------------------

fires_with_grid = gpd.sjoin_nearest(
    fires_5070,
    grid_points[["x", "y", "geometry", "grid_id"]],
    how="left",
    distance_col="dist_to_grid",
)

fires_with_grid["hour_ts"] = pd.to_datetime(fires_with_grid["hour_ts"])

# Sum value_col per (hour, grid cell)
grid_hourly = (
    fires_with_grid
    .groupby(["hour_ts", "grid_id"], as_index=False)
    .agg({value_col: "sum"})
)

grid_hourly["hour_ts"] = pd.to_datetime(grid_hourly["hour_ts"])


print("Completed polygon -> grid assignment")
# ------------------------
# Fill missing hours per grid cell
# ------------------------

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
    sub[value_col] = sub[value_col].ffill()
    # If you prefer "0 when no fire detected":
    #sub[value_col] = sub[value_col].fillna(0.0)
    sub = sub.reset_index().rename(columns={"index": "hour_ts"})
    filled_list.append(sub)

grid_hourly = pd.concat(filled_list, ignore_index=True)

print("Completed filling-in of missing hours")
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

print("Completed 2D Grid point generation")
# ------------------------
# Build 3D array and Dataset
# ------------------------

times = np.sort(grid_hourly["hour_ts"].unique())
nt = len(times)
time_index = {t: i for i, t in enumerate(times)}

frp_3d = np.full((nt, ny, nx), np.nan, dtype="float32")
grid_hourly_agg = (
    grid_hourly.groupby(["hour_ts", "iy", "ix"], as_index=False)
    .agg({value_col: "sum"})
)

for _, row in grid_hourly_agg.iterrows():
    it = time_index[row["hour_ts"]]
    iy = int(row["iy"])
    ix = int(row["ix"])
    frp_3d[it, iy, ix] = row[value_col]

ds = xr.Dataset(
    data_vars={value_col: (("time", "y", "x"), frp_3d)},
    coords={"time": times, "y": y_centers, "x": x_centers},
    attrs={
        "description": f"OpenVEDA fire perimeters aggregated to projected grid ({value_col})",
        "proj": f"EPSG:{grid_template.epsg}",
        "time_resolution": "hourly",
    },
)

print("Generated final XR Dataset")

ds["lat"] = (("y", "x"), grid_template.grid["lat"].values)
ds["lon"] = (("y", "x"), grid_template.grid["lon"].values)
ds["lat"].attrs.update({"units": "degrees_north", "standard_name": "latitude"})
ds["lon"].attrs.update({"units": "degrees_east", "standard_name": "longitude"})

ds[value_col].attrs["long_name"] = value_col
ds.attrs["title"] = f"Hourly {value_col} from OpenVEDA fire perimeters on 50 km grid"

encoding = {
    value_col: {"zlib": True, "complevel": 4, "dtype": "float32"},
}

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

    out_path = f"{PATH}/{OUT_REL_PATH}/fires_perims_{value_col}_grid_uci_{day_str}.nc"
    ds_day.to_netcdf(out_path, encoding=encoding)
    print("Saved", out_path)
