#!/usr/bin/env python3

import os
import glob

import geopandas as gpd
import pandas as pd
import numpy as np
import xarray as xr
from shapely.geometry import Point
from grid import AnalysisGridTemplate

PATH = "/mnt/data1/MultiHazard/"
OUT_REL_PATH = "data/daily/microsoft_buildings/"

# Directory (or explicit list) of Microsoft building footprints
# These could be GeoParquet, GeoJSON, or Shapefile; adjust glob as needed.
BUILDING_DIR = "/mnt/data1/MultiHazard/data/building_footprints/"
BUILDING_FILES = sorted(glob.glob(os.path.join(BUILDING_DIR, "*.geojson")))

# Optional spatial subset in geographic coordinates
lat_bounds = [25, 50]
lon_bounds = [-125, -65]

# ------------------------
# Load and concatenate building footprints
# ------------------------

gdfs = []
for fn in BUILDING_FILES:
    print(f"Reading {fn}")
    gdf_tmp = gpd.read_file(fn)

    # Ensure geometry column is active
    if gdf_tmp.geometry.name is None:
        # If the geometry is stored under some other column, set it here
        raise ValueError("No active geometry column found in building data.")

    # Set or convert CRS to WGS84
    if gdf_tmp.crs is None:
        gdf_tmp = gdf_tmp.set_crs("EPSG:4326")
    else:
        gdf_tmp = gdf_tmp.to_crs("EPSG:4326")

    # Optional geographic clip to your domain
    minx, miny, maxx, maxy = lon_bounds[0], lat_bounds[0], lon_bounds[1], lat_bounds[1]
    gdf_tmp = gdf_tmp.cx[minx:maxx, miny:maxy]

    gdfs.append(gdf_tmp)

if not gdfs:
    raise RuntimeError(f"No building files found in {BUILDING_DIR}")

buildings = gpd.GeoDataFrame(
    pd.concat(gdfs, ignore_index=True),
    geometry="geometry",
    crs="EPSG:4326",
)
buildings.reset_index(drop=True, inplace=True)
print("Total buildings loaded:", len(buildings))

# ------------------------
# Compute building area 
# ------------------------

area_col = "area_m2"

# Temporarily project to an equal-area CRS for accurate area, then back
buildings_eq = buildings.to_crs("EPSG:6933")  # NSIDC EASE-Grid 2.0 global equal area
buildings[area_col] = buildings_eq.geometry.area  # in m^2

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

x_centers = grid_template.grid.coords["x"].values
y_centers = grid_template.grid.coords["y"].values
ny, nx = len(y_centers), len(x_centers)

# Reproject buildings to grid CRS
buildings_5070 = buildings.to_crs(f"EPSG:{grid_template.epsg}")


print("Completed generating/loading analysis grid")
# ------------------------
# Build grid cell centers as points
# ------------------------

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

# ------------------------
# Assign buildings to nearest grid cell
# ------------------------

buildings_with_grid = gpd.sjoin_nearest(
    buildings_5070,
    grid_points[["x", "y", "geometry", "grid_id"]],
    how="left",
    distance_col="dist_to_grid",
)

# ------------------------
# Aggregate building count and average size per grid cell
# ------------------------

agg = (
    buildings_with_grid
    .groupby("grid_id", as_index=False)
    .agg(
        building_count=("grid_id", "size"),
        mean_building_area_m2=(area_col, "mean"),
    )
)


print("Completed computation of sumarry stats")
# ------------------------
# Map grid_id to (iy, ix) indices
# ------------------------

grid_points_idx = grid_points[["grid_id", "x", "y"]].copy()
grid_points_idx["ix"] = grid_points_idx["x"].map(
    lambda xv: int(np.argmin(np.abs(x_centers - xv)))
)
grid_points_idx["iy"] = grid_points_idx["y"].map(
    lambda yv: int(np.argmin(np.abs(y_centers - yv)))
)

lookup = grid_points_idx.set_index("grid_id")[["iy", "ix"]]

agg = agg.merge(
    lookup,
    left_on="grid_id",
    right_index=True,
    how="left",
)

# ------------------------
# Build 2D arrays on (y, x)
# ------------------------

building_count_2d = np.zeros((ny, nx), dtype="int32")
mean_area_2d = np.full((ny, nx), np.nan, dtype="float32")

for _, row in agg.iterrows():
    iy = int(row["iy"])
    ix = int(row["ix"])
    building_count_2d[iy, ix] = int(row["building_count"])
    mean_area_2d[iy, ix] = float(row["mean_building_area_m2"])

print("Completed 2D grid index generation")
# ------------------------
# Build Dataset and save
# ------------------------

ds = xr.Dataset(
    data_vars={
        "building_count": (("y", "x"), building_count_2d),
        "mean_building_area_m2": (("y", "x"), mean_area_2d),
    },
    coords={
        "y": y_centers,
        "x": x_centers,
    },
    attrs={
        "description": "Microsoft building footprints aggregated to 50 km analysis grid",
        "proj": f"EPSG:{grid_template.epsg}",
        "time_resolution": "static",
    },
)

print("Final XR Dataset generated")

# Add lat/lon from template
ds["lat"] = (("y", "x"), grid_template.grid["lat"].values)
ds["lon"] = (("y", "x"), grid_template.grid["lon"].values)
ds["lat"].attrs.update({"units": "degrees_north", "standard_name": "latitude"})
ds["lon"].attrs.update({"units": "degrees_east", "standard_name": "longitude"})

ds["building_count"].attrs["long_name"] = "Number of buildings in grid cell"
ds["mean_building_area_m2"].attrs["long_name"] = "Mean building footprint area"
ds["mean_building_area_m2"].attrs["units"] = "m2"
ds.attrs["title"] = "Static building statistics from Microsoft Building Footprints on 50 km grid"

encoding = {
    "building_count": {"zlib": True, "complevel": 4, "dtype": "int32"},
    "mean_building_area_m2": {"zlib": True, "complevel": 4, "dtype": "float32"},
}

out_path = f"{PATH}/{OUT_REL_PATH}/ms_buildings_analysis_grid_static.nc"
ds.to_netcdf(out_path, encoding=encoding)
print("Saved", out_path)



