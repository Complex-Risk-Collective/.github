#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Created on Fri Jan 16 12:02:47 2026

@author: marchett
"""
import numpy as np
import xarray as xr
import pandas as pd
import pyproj





class AnalysisGridTemplate:
    def __init__(self, grid_file="analysis_grid.nc",
                 lat_bounds=(24, 50),
                 lon_bounds=(-125, -66),
                 res_km=50,
                 epsg=5070):
        """
        Create a projected analysis grid for a given lat/lon box and km resolution.

        Parameters:
        - grid_file: filename to save/load grid
        - lat_bounds: (min_lat, max_lat) in degrees
        - lon_bounds: (min_lon, max_lon) in degrees
        - res_km: resolution in km in projected space
        - epsg: projected coordinate system EPSG code
        """
        
        self.grid_file = grid_file
        self.epsg = epsg
        self.res_km = res_km

        # Transformers
        self.to_proj = pyproj.Transformer.from_crs("EPSG:4326", 
                                                   f"EPSG:{epsg}", always_xy=True)
        self.to_latlon = pyproj.Transformer.from_crs(f"EPSG:{epsg}", 
                                                     "EPSG:4326", always_xy=True)
        
        # Load or create grid
        try:
            self.grid = xr.open_dataset(grid_file)
            print(f"Loaded existing grid: {grid_file}")
        except FileNotFoundError:
            print(f"Creating new {res_km} km grid in EPSG:{epsg} from lat/lon bounds")
            self.grid = self.create_grid_from_latlon(lat_bounds, lon_bounds, res_km)
            self.grid.to_netcdf(grid_file)
        
        # Compute edges for binning in projected coordinates
        self.x_centers = self.grid.coords['x'].values
        self.y_centers = self.grid.coords['y'].values
        dx = np.diff(self.x_centers).mean()
        dy = np.diff(self.y_centers).mean()
        self.x_edges = np.concatenate([[self.x_centers[0]-dx/2],
                                       (self.x_centers[:-1] + self.x_centers[1:])/2,
                                       [self.x_centers[-1]+dx/2]])
        self.y_edges = np.concatenate([[self.y_centers[0]-dy/2],
                                       (self.y_centers[:-1] + self.y_centers[1:])/2,
                                       [self.y_centers[-1]+dy/2]])

    def create_grid_from_latlon(self, lat_bounds, lon_bounds, res_km):
        
        """
        Create grid in projected space with given km resolution, 
        storing lat/lon per cell.
        """
        
        # Convert bounds to projected coordinates
        min_x, min_y = self.to_proj.transform(lon_bounds[0], lat_bounds[0])
        max_x, max_y = self.to_proj.transform(lon_bounds[1], lat_bounds[1])
        res_m = res_km * 1000

        # Grid in projected coordinates
        x = np.arange(min_x, max_x + res_m, res_m)
        y = np.arange(min_y, max_y + res_m, res_m)
        xx, yy = np.meshgrid(x, y)

        # Lat/lon coordinates of each grid cell
        lon, lat = self.to_latlon.transform(xx, yy)

        # Optional: approximate spacing in km in lat/lon for metadata
        geod = pyproj.Geod(ellps="WGS84")
        dx_km, dy_km = None, None
        if x.size > 1 and y.size > 1:
            _, _, dx_m = geod.inv(lon[0, :-1], lat[0, :-1], lon[0, 1:], lat[0, 1:])
            _, _, dy_m = geod.inv(lon[:-1, 0], lat[:-1, 0], lon[1:, 0], lat[1:, 0])
            dx_km = np.mean(dx_m) / 1000
            dy_km = np.mean(dy_m) / 1000

        grid = xr.Dataset(
            {
                "lat": (["y", "x"], lat),
                "lon": (["y", "x"], lon)
            },
            coords={
                "x": x,  # <--- MUST be in coords
                "y": y   # <--- MUST be in coords
            },
            attrs={
                "proj": f"EPSG:{self.epsg}",
                "resolution_km": res_km,
                "lat_min": lat_bounds[0],
                "lat_max": lat_bounds[1],
                "lon_min": lon_bounds[0],
                "lon_max": lon_bounds[1],
                "approx_dx_km": dx_km,
                "approx_dy_km": dy_km
            }
        )
        
        print(f"Grid created: {len(y)} y × {len(x)} x points (~{res_km} km spacing)")
        return grid

    def create_time_axis(self, start_date, end_date, freq="1H"):
        """
        Create a time axis.
        """
        self.time = pd.date_range(start=start_date, end=end_date, freq=freq)
        print(f"Time axis created: {len(self.time)} steps from {self.time[0]} to {self.time[-1]}")
        return self.time

    def get_uniform_latlon_grid(self, n_lat=None, n_lon=None):
        
        """
        Returns a regular lat/lon mesh for plotting or exporting.
        The grid is approximately uniform in degrees.

        Parameters:
        - n_lat: number of points along latitude (optional)
        - n_lon: number of points along longitude (optional)

        Returns:
        - lon_grid, lat_grid: 2D arrays of lat/lon
        """
        
        lat_min = self.grid.lat.min().item()
        lat_max = self.grid.lat.max().item()
        lon_min = self.grid.lon.min().item()
        lon_max = self.grid.lon.max().item()

        if n_lat is None:
            n_lat = self.grid.y.size
        if n_lon is None:
            n_lon = self.grid.x.size

        lat = np.linspace(lat_min, lat_max, n_lat)
        lon = np.linspace(lon_min, lon_max, n_lon)
        lon_grid, lat_grid = np.meshgrid(lon, lat)

        return lon_grid, lat_grid




# Define lat/lon bounds for CONUS
# lat_bounds = (24, 50)
# lon_bounds = (-125, -66)

# # Create grid with 50 km resolution in EPSG:5070
# grid_template = AnalysisGridTemplate(
#     grid_file="conus_grid_50km.nc",
#     lat_bounds=lat_bounds,
#     lon_bounds=lon_bounds,
#     res_km=50,
#     epsg=5070
# )

# # Create hourly time axis
# time_axis = grid_template.create_time_axis("2023-03-31", "2023-04-04")

# # Access grid coordinates
# print(grid_template.grid)
# print(time_axis)




