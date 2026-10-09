#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Created on Fri Oct 24 10:20:23 2025

@author: marchett
"""

import os
from pathlib import Path
import numpy as np
import pandas as pd
import xarray as xr


# -----------------------------
# config
# -----------------------------

# region="CONUS"
# start_date = "2024-06-25 00:00"
# end_date   = "2024-07-04 23:00"

# lat_bounds = [25, 50]   # optional lat subset
# lon_bounds = [-125, -65]  # optional lon subset


# INPUT_FOLDER = "/Users/marchett/Documents/Disasters/data/swdi"
# # OUTFILE = f"{INPUT_FOLDER}/swdi_nx3_hourly_50km_conus.nc"
# OUTFILE = f"{INPUT_FOLDER}/SWDI_{region}_{start_date}_{end_date}.nc"
# input_folder = f"{INPUT_FOLDER}/swdi_raw/"

# # choose variables (from: nx3tvs, nx3meso, nx3hail, nx3structure)
# variables = ["nx3tvs", "nx3hail", "nx3structure"]

# # which variables have which values
# columns_dict = {
#     "nx3tvs": ["MAX_SHEAR", "RANGE", "MXDV"],
#     "nx3hail": ["MAXSIZE"],
#     "nx3meso": ["MAXSIZE"],
#     "nx3structure": ["MAX_REFLECT", "VIL", "RANGE"],
# }


# grid_template = AnalysisGridTemplate(
#     grid_file=f"{INPUT_FOLDER}/SWDI_{region}_grid_50km.nc",
#     lat_bounds=lat_bounds,
#     lon_bounds=lon_bounds,
#     res_km=50,
#     epsg=5070
# )
   
    
# grid_template.create_time_axis(
#     start_date=start_date,
#     end_date=end_date,
#     freq="1h")
    

# -----------------------------
# helpers
# -----------------------------


# def list_csv_files(input_folder, variables):
#     variables = [v.lower() for v in variables]
#     out = []

#     for root, _, files in os.walk(input_folder):
#         for fname in files:
#             if not fname.lower().endswith(".csv"):
#                 continue
#             name_lower = fname.lower()
#             if any(v in name_lower for v in variables):
#                 out.append(os.path.join(root, fname))

#     return sorted(set(out))


def list_csv_files(input_folder, variables, start_date=None, end_date=None):
    variables = [v.lower() for v in variables]
    out = []

    # Normalize dates if provided
    if start_date is not None and end_date is not None:
        start_dt = pd.to_datetime(start_date)
        end_dt   = pd.to_datetime(end_date)
        date_tag = f"{start_dt:%Y%m%d}-{end_dt:%Y%m%d}"
    else:
        date_tag = None

    for root, _, files in os.walk(input_folder):
        for fname in files:
            if not fname.lower().endswith(".csv"):
                continue

            name_lower = fname.lower()

            # Match variable
            if not any(v in name_lower for v in variables):
                continue

            # Match date if provided
            if date_tag is not None and date_tag not in fname:
                continue

            out.append(os.path.join(root, fname))

    return sorted(set(out))


# --- map points to projected grid
def project_points(lon, lat, transformer):
    xs, ys = transformer.transform(lon, lat)
    return xs, ys


# def bin_to_grid(xs, ys, x_edges, y_edges):
#     ix = np.digitize(xs, x_edges) - 1
#     iy = np.digitize(ys, y_edges) - 1
#     nx = len(x_edges) - 1
#     ny = len(y_edges) - 1
#     inside = (ix >= 0) & (ix < nx) & (iy >= 0) & (iy < ny)
#     return ix, iy, inside


def bin_to_grid(xs, ys, x_edges, y_edges):
    ix = np.digitize(xs, x_edges) - 1
    iy = np.digitize(ys, y_edges) - 1
    nx = len(x_edges) - 1
    ny = len(y_edges) - 1
    inside = (ix >= 0) & (ix < nx) & (iy >= 0) & (iy < ny)
    return ix, iy, inside



# -----------------------------
# aggregation
# -----------------------------

_expected_columns = {
    "nx3tvs": ['ZTIME','WSR_ID','CELL_ID','CELL_TYPE','RANGE','AZIMUTH','MAX_SHEAR','MXDV','LAT','LON'],
    "nx3hail": ['ZTIME','WSR_ID','CELL_ID','PROB','SEVPROB','MAXSIZE','LAT','LON'],
    "nx3structure": ['ZTIME','WSR_ID','CELL_ID','RANGE','AZIMUTH','VIL','MAX_REFLECT','LAT','LON']
}

def aggregate_swdi(csv_dir, product_type, date, grid_template, variables=None):


    # --------------------------------------------------
    # Paths & files
    # --------------------------------------------------
    csv_dir = Path(csv_dir)
    product_dir = csv_dir / product_type
    date_str = date.strftime("%Y%m%d")
    daily_files = list(product_dir.glob(f"*{date_str}*.csv"))

    # print("daily_files detected:")
    # print(daily_files)

    ny, nx = grid_template.grid.sizes['y'], grid_template.grid.sizes['x']
    
    # tz-naive hourly timestamps
    hours = pd.date_range(date, periods=24, freq="h", tz="UTC").tz_convert(None)
    # print("hours:", hours)

    # --------------------------------------------------
    # Helper: create empty dataset (all NaNs)
    # --------------------------------------------------
    def empty_dataset(vars_out):
        print("Creating empty dataset for variables:", vars_out)
        return xr.Dataset(
            {
                v: (("ZTIME", "Y", "X"),
                    np.full((24, ny, nx), np.nan, dtype=float))
                for v in vars_out
            },
            coords={
                "ZTIME": hours,
                "Y": grid_template.y_centers,
                "X": grid_template.x_centers,
            },
        )

    # --------------------------------------------------
    # No files → return empty
    # --------------------------------------------------
    if not daily_files:
        default_vars = {
            "nx3tvs": ["MAX_SHEAR", "MXDV"],
            "nx3hail": ["PROB", "SEVPROB", "MAXSIZE"],
            "nx3structure": ["VIL", "MAX_REFLECT"],
        }
        vars_out = variables or default_vars.get(product_type, [])
        print("No CSV files found, returning empty dataset.")
        return empty_dataset(vars_out)

    # --------------------------------------------------
    # Process first file to determine variables
    # --------------------------------------------------
    sample_df = None
    for csv_file in daily_files:
        print("\nTrying to read sample CSV:", csv_file)
        try:
            df_try = pd.read_csv(csv_file, engine="python", dtype=str)
            print("Columns in sample CSV:", df_try.columns.tolist())
            # detect separator issues
            if "ZTIME" in df_try.columns:
                sample_df = df_try
                break
        except Exception as e:
            print("Failed reading CSV:", e)
            continue

    if sample_df is None or "ZTIME" not in sample_df.columns:
        default_vars = {
            "nx3tvs": ["MAX_SHEAR", "MXDV"],
            "nx3hail": ["PROB", "SEVPROB", "MAXSIZE"],
            "nx3structure": ["VIL", "MAX_REFLECT"],
        }
        vars_out = variables or default_vars.get(product_type, [])
        print("No ZTIME column detected, returning empty dataset.")
        return empty_dataset(vars_out)

    # Remove footer rows from sample
    sample_df = sample_df[pd.to_datetime(sample_df.get("ZTIME", pd.Series()), errors="coerce").notna()]
    # print("sample_df after removing footer rows:")
    # print(sample_df.head())

    if sample_df.empty:
        default_vars = {
            "nx3tvs": ["MAX_SHEAR", "MXDV"],
            "nx3hail": ["PROB", "SEVPROB", "MAXSIZE"],
            "nx3structure": ["VIL", "MAX_REFLECT"],
        }
        vars_out = variables or default_vars.get(product_type, [])
        print("Sample df is empty after footer removal, returning empty dataset.")
        return empty_dataset(vars_out)

    # Determine variables to aggregate
    if variables is None:
        variables = [
            c for c in sample_df.columns
            if c not in ["ZTIME", "LAT", "LON", "WSR_ID", "CELL_ID", "CELL_TYPE"]
        ]
    # print("Variables to aggregate:", variables)

    # --------------------------------------------------
    # Initialize aggregation arrays
    # --------------------------------------------------
    sum_grids = {v: np.zeros((24, ny, nx)) for v in variables}
    count_grids = {v: np.zeros((24, ny, nx), dtype=int) for v in variables}

    x_edges = grid_template.x_edges.copy()
    y_edges = grid_template.y_edges.copy()
    x_edges[-1] += 1e-6
    y_edges[-1] += 1e-6
    
    # print("Grid edges prepared:")
    # print("X edges:", x_edges[:5], "...", x_edges[-5:])
    # print("Y edges:", y_edges[:5], "...", y_edges[-5:])

    # --------------------------------------------------
    # Process CSVs
    # --------------------------------------------------
    for csv_file in daily_files:
        
        print("\nProcessing CSV:", csv_file)
        try:
            df = pd.read_csv(csv_file, engine="python", dtype=str)
            if "ZTIME" not in df.columns:
                df = pd.read_csv(csv_file, sep="\t", engine="python", dtype=str)
        except Exception as e:
            print("Failed reading CSV:", e)
            continue

        # print("df before footer removal:")
        # print(df.head())

        if "ZTIME" not in df.columns:
            print("Skipping CSV: no ZTIME column")
            continue

        # Remove footer rows
        df = df[pd.to_datetime(df.get("ZTIME", pd.Series()), errors="coerce").notna()]
        # print("df after footer removal:")
        # print(df.head())

        if df.empty:
            print("Skipping CSV: empty after footer removal")
            continue

        # Parse ZTIME tz-naive in UTC
        df["ZTIME"] = pd.to_datetime(df["ZTIME"], errors="coerce", utc=True).dt.tz_convert(None)
        # print("df after ZTIME parsing:")
        # print(df.head())

        # --------------------------------------------------
        # Loop over rows to bin
        # --------------------------------------------------
        for idx, row in df.iterrows():
            try:
                hour_index = int((row["ZTIME"] - pd.Timestamp(date)).total_seconds() // 3600)
            except Exception as e:
                print("Skipping row: ZTIME calculation failed:", e)
                continue
            if not (0 <= hour_index < 24):
                continue

            try:
                lat = float(row["LAT"])
                lon = float(row["LON"])
            except Exception as e:
                print("Skipping row: LAT/LON conversion failed:", e)
                continue

            # Project to grid
            try:
                x, y = grid_template.to_proj.transform(lon, lat)
            except Exception as e:
                print("Skipping row: projection failed:", e)
                continue

            # Bin to grid
            try:
                ix, iy, inside = bin_to_grid(np.array([x]), np.array([y]), x_edges, y_edges)
            except Exception as e:
                print("Skipping row: binning failed:", e)
                continue

            if not inside[0]:
                continue

            i, j = iy[0], ix[0]

            # Sum/count aggregation
            for var in variables:
                val = row.get(var, np.nan)
                if pd.notna(val):
                    try:
                        val = float(val)
                    except Exception as e:
                        print(f"Skipping value for {var}: conversion failed:", e)
                        continue
                    sum_grids[var][hour_index, i, j] += val
                    count_grids[var][hour_index, i, j] += 1

    # --------------------------------------------------
    # Compute nanmean
    # --------------------------------------------------
    avg_grids = {}
    for var in variables:
        avg = np.full((24, ny, nx), np.nan)
        mask = count_grids[var] > 0
        avg[mask] = sum_grids[var][mask] / count_grids[var][mask]
        avg_grids[var] = avg
        print(f"Sample aggregated values for {var}:")
        print(avg[~np.isnan(avg)][:5])  # first few non-NaN

    daily_swdi = xr.Dataset(
        {v: (("ZTIME", "Y", "X"), avg_grids[v]) for v in variables},
        coords={
            "ZTIME": hours,
            "Y": grid_template.y_centers,
            "X": grid_template.x_centers,
        },
    )

    # ---------- ADD LAT/LON ----------
    X2d, Y2d = np.meshgrid(daily_swdi["X"].values, daily_swdi["Y"].values)

    # Inverse transform to get lon/lat
    lon_2d, lat_2d = grid_template.to_proj.transform(Y2d, X2d, direction='INVERSE')
    
    # Add as coords
    daily_swdi = daily_swdi.assign_coords(
        LAT=(("Y", "X"), lat_2d),
        LON=(("Y", "X"), lon_2d)
    )
    
    print("Returning aggregated dataset")
    return daily_swdi





# -----------------------------
# save netcdf
# -----------------------------
    

def save_to_netcdf_with_stats(stats_dict, time_index, y_centers, x_centers,
                           vars_list, columns_dict, outfile, grid_template,
                           crs_epsg=5070):
    """
    Save aggregated SWDI daily stats to NetCDF.
    """
    if not stats_dict or not any(
        np.isfinite(arr).any()
        for stat_data in stats_dict.values()
        for arr in stat_data.values()
    ):
        print(f"No valid SWDI data to save for {outfile}, skipping NetCDF.")
        return

    ds = xr.Dataset(
        coords={"time": time_index, "y": ("y", y_centers), "x": ("x", x_centers)},
        attrs={"description": "SWDI events aggregated to projected grid",
               "proj": f"EPSG:{crs_epsg}",
               "time_resolution": "daily"}
    )

    # Add lat/lon from template
    ds["lat"] = (("y", "x"), grid_template.grid["lat"].values)
    ds["lon"] = (("y", "x"), grid_template.grid["lon"].values)
    ds["lat"].attrs.update({"units": "degrees_north", "standard_name": "latitude"})
    ds["lon"].attrs.update({"units": "degrees_east", "standard_name": "longitude"})

    # Write variables
    for var_idx, var_name in enumerate(vars_list):
        numeric_cols = columns_dict.get(var_name, [])
        for stat_name, stat_data in stats_dict.items():
            for col in numeric_cols:
                if col not in stat_data:
                    continue
                arr4d = stat_data[col]  # (time, y, x, nvars)
                data3d = arr4d[..., var_idx]
                key = f"{var_name}_{col}_{stat_name}"
                ds[key] = (("time", "y", "x"), data3d)
                ds[key].attrs.update({"statistic": stat_name,
                                      "source_variable": col,
                                      "units": "number of reports" if stat_name=="count" else "same as original data"})

    enc = {k: {"zlib": True, "complevel": 4, "dtype": "int32" if k.endswith("_count") else "float32"}
           for k in ds.data_vars}
    ds.to_netcdf(outfile, format="netcdf4", encoding=enc)
    print(f"Wrote: {outfile}")
    
    


    
    
# -----------------------------
# run aggregation pipeline
# -----------------------------

# def run_pipeline_swdi(
#     input_folder,
#     grid_template=None,
#     variables=None,
#     start_date=None,
#     end_date=None,
#     output_file=None,
#     region="CONUS",
#     lat_bounds=None,
#     lon_bounds=None,
#     res_km=50,
#     epsg=5070,
# ):
#     from pathlib import Path

#     if variables is None:
#         variables = ["nx3tvs", "nx3hail", "nx3structure"]

#     if start_date is None or end_date is None:
#         raise ValueError("start_date and end_date must be provided")

#     # --- default grid ---
#     if grid_template is None:
#         if lat_bounds is None:
#             lat_bounds = [25, 50]
#         if lon_bounds is None:
#             lon_bounds = [-125, -65]
#         if output_file is None:
#             raise ValueError("output_file must be provided if grid is None")

#         grid_template = AnalysisGridTemplate(
#             grid_file=Path(output_file).parent / f"SWDI_{region}_grid_{res_km}km.nc",
#             lat_bounds=lat_bounds,
#             lon_bounds=lon_bounds,
#             res_km=res_km,
#             epsg=epsg,
#         )

#     date_tag = f"{pd.to_datetime(start_date):%Y%m%d}_{pd.to_datetime(end_date):%Y%m%d}"

#     # --- aggregate ---
#     print(f"Aggregating SWDI for {date_tag}...")
#     stats_dict, time_index, y_centers, x_centers, vars_list, columns_dict_local = \
#         aggregate_swdi(input_folder, grid_template, variables, start_date, end_date, stats_fns={"mean": np.nanmean})

#     # --- skip saving if no valid data ---
#     has_valid_data = any(
#         np.isfinite(arr).any()
#         for stat_data in stats_dict.values()
#         for arr in stat_data.values()
#     )
#     if not has_valid_data:
#         print(f"No valid SWDI data after aggregation for {date_tag}, skipping NetCDF save.")
#         return

#     print("Saving to NetCDF...")
#     save_to_netcdf_with_stats(
#         stats_dict,
#         time_index,
#         y_centers,
#         x_centers,
#         vars_list,
#         columns_dict_local,
#         output_file,
#         grid_template
#     )




if __name__ == "__main__":

    import argparse
    from datetime import datetime

    parser = argparse.ArgumentParser(
        description="SWDI Aggregation"
    )
    
    # -----------------------------
    # Defaults
    # -----------------------------
    root = "/Users/marchett/Documents/Disasters/data/swdi/"
    input_folder = f"{root}/swdi_raw"
    region = "CONUS"
    start = "2024-06-25 00:00"
    end = "2024-06-25 23:00"
    variables = ["nx3tvs", "nx3hail", "nx3structure"]
    lat_bounds = [25, 50]
    lon_bounds = [-125, -65]
    grid = None  # will auto-create if None
    output_file = f"{root}/SWDI_{start}.nc"
    

    parser.add_argument("--input_folder", default=input_folder)
    parser.add_argument("--output_file", default=output_file)
    parser.add_argument("--start", default=start)
    parser.add_argument("--end", default=end)
    parser.add_argument("--variables", nargs="+", default=variables)
    parser.add_argument("--region", default=region)
    parser.add_argument("--latmin", type=float, default=lat_bounds[0])
    parser.add_argument("--latmax", type=float, default=lat_bounds[1])
    parser.add_argument("--lonmin", type=float, default=lon_bounds[0])
    parser.add_argument("--lonmax", type=float, default=lon_bounds[1])

    args = parser.parse_args()


    run_pipeline_swdi(
        input_folder=args.input_folder,
        grid_template=grid,
        variables=args.variables,
        start_date=datetime.strptime(args.start, "%Y-%m-%d %H:%M"),
        end_date=datetime.strptime(args.end, "%Y-%m-%d %H:%M"),
        output_file=args.output_file,
        region=args.region,
        lat_bounds=[args.latmin, args.latmax],
        lon_bounds=[args.lonmin, args.lonmax]
    )
    
    


    


