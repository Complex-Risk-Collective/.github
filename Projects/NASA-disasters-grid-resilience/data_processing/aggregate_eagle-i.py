#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Created on Wed Feb 11 16:36:12 2026

@author: marchett
"""

import os
import requests
import zipfile
import io
import pandas as pd
import geopandas as gpd
import numpy as np
import xarray as xr
from shapely.geometry import Point
from grid import AnalysisGridTemplate




# ------------------------
# Load outage CSV and aggregate hourly
# ------------------------


PATH = "/Users/marchett/Documents/Disasters/data/eagle-i/"
RAW_DIR = os.path.join(PATH, "eagle-i_raw")
os.makedirs(RAW_DIR, exist_ok=True)

OUTAGE_CSV = f"{PATH}/eagle-i_raw/20230325T000300_20230409T000400_county_outage_data.csv"

df_out = pd.read_csv(OUTAGE_CSV)
df_out['__ts'] = pd.to_datetime(df_out['Run Start Time'])
df_out['GEOID'] = df_out['Fips Code'].astype(str).str.zfill(5)

df_out_hourly = df_out.groupby(['GEOID', pd.Grouper(key='__ts', freq='h')])['Customers Out'].sum().reset_index()
df_out_hourly['__ts'] = df_out_hourly['__ts'].dt.tz_localize(None)


# ------------------------
# Load county shapefile in projected coordinates
# ------------------------

url = "https://www2.census.gov/geo/tiger/TIGER2024/COUNTY/tl_2024_us_county.zip"
r = requests.get(url)
z = zipfile.ZipFile(io.BytesIO(r.content))
z.extractall("tl_2024_us_county")
counties = gpd.read_file("tl_2024_us_county/tl_2024_us_county.shp")
counties = counties.to_crs(5070)


# ------------------------
# Create analysis grid
# ------------------------

start_date = "2024-06-25"
end_date   = "2024-07-04"

lat_bounds = [25, 50]   # optional lat subset
lon_bounds = [-125, -65]  # optional lon subset


grid_template = AnalysisGridTemplate(
    grid_file=f"{PATH}/analysis_grid.nc",
    lat_bounds=lat_bounds,
    lon_bounds=lon_bounds,
    res_km=50,
    epsg=5070
)

x_centers = grid_template.grid.coords['x'].values
y_centers = grid_template.grid.coords['y'].values
ny, nx = len(y_centers), len(x_centers)

# Grid points as GeoDataFrame
grid_x, grid_y = np.meshgrid(x_centers, y_centers)
grid_points = gpd.GeoDataFrame({
    'x': grid_x.ravel(),
    'y': grid_y.ravel(),
    'geometry': [Point(x, y) for x, y in zip(grid_x.ravel(), grid_y.ravel())]
}, crs=f"EPSG:{grid_template.epsg}")

# Spatial join to assign each grid cell to a county
grid_with_county = gpd.sjoin(grid_points, counties[['GEOID','geometry']],
                             how='left', predicate='intersects')

grid_with_county = grid_with_county.dropna(subset=['GEOID']).reset_index(drop=True)
grid_with_county['grid_index'] = np.arange(len(grid_with_county))


# ------------------------
# Merge outages to grid
# ------------------------

grid_df = grid_with_county[['grid_index', 'GEOID', 'x', 'y']].copy()
grid_df = grid_df.merge(df_out_hourly, on='GEOID', how='left')
grid_df['Customers Out'] = grid_df['Customers Out'].fillna(0)


# ------------------------
# Aggregate per time step
# ------------------------

times = pd.date_range(df_out_hourly['__ts'].min(), df_out_hourly['__ts'].max(), freq='H')
out_data = np.zeros((len(times), ny, nx))  # time, y, x

# Map grid_index to y/x indices
grid_yx = [(np.where(y_centers == y)[0][0], np.where(x_centers == x)[0][0])
           for x, y in zip(grid_with_county['x'], grid_with_county['y'])]

for t_idx, t in enumerate(times):
    df_t = grid_df[grid_df['__ts'] == t]
    if not df_t.empty:
        for idx, row in df_t.iterrows():
            i, j = grid_yx[int(row['grid_index'])]
            out_data[t_idx, i, j] += row['Customers Out']

# ------------------------
# Save to NetCDF
# ------------------------
ds_outages = xr.Dataset(
    {'customers_out': (('time','y','x'), out_data)},
    coords={'time': times, 'y': y_centers, 'x': x_centers}
)
ds_outages.to_netcdf(f"{PATH}/outages_analysis_grid_50km.nc", format="NETCDF4")
print("Saved aggregated outages to NetCDF.")




