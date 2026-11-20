#!/usr/bin/env python3
"""
Quick read + visualize for the geoe_interpolated NetCDF4 files.

Requires (install locally, not needed in a sandbox with no internet):
    pip install xarray netCDF4 matplotlib numpy

Usage:
    # Just inspect structure (variables, dims, attrs) -- no plotting
    python read_and_visualize_geoe.py --file /path/to/geoe_..._2024_01.nc --info-only

    # Plot a specific variable (auto-picks a sensible slice if it has extra dims)
    python read_and_visualize_geoe.py --file /path/to/geoe_..._2024_01.nc --var Ex --time 0

    # Let the script guess the "main" variable and time-average it
    python read_and_visualize_geoe.py --file /path/to/geoe_..._2024_01.nc
"""

import argparse
import sys

import numpy as np


def main():
    parser = argparse.ArgumentParser(description="Read and visualize a NetCDF4 file.")
    parser.add_argument("--file", required=True, help="Path to the .nc file")
    parser.add_argument("--var", default=None, help="Variable name to plot (default: auto-pick largest data var)")
    parser.add_argument("--time", type=int, default=None, help="Time index to plot (default: 0, or mean over time if --agg mean)")
    parser.add_argument("--agg", choices=["none", "mean", "max"], default="none",
                         help="Aggregate over the time dimension instead of picking a single index")
    parser.add_argument("--level", type=int, default=0, help="Index into any extra non-lat/lon dim (e.g. depth/level)")
    parser.add_argument("--info-only", action="store_true", help="Just print dataset structure, don't plot")
    parser.add_argument("--out", default=None, help="Output PNG path (default: alongside input file)")
    parser.add_argument("--cmap", default="RdBu_r", help="Matplotlib colormap")
    args = parser.parse_args()

    try:
        import xarray as xr
    except ImportError:
        sys.exit(
            "xarray is not installed. Run:\n"
            "    pip install xarray netCDF4 matplotlib numpy\n"
            "then re-run this script."
        )

    print(f"Opening {args.file} ...")
    # chunks='auto' -> lazy/dask-backed read, safe for large (multi-GB) files
    ds = xr.open_dataset(args.file, chunks="auto")

    print("\n=== Dataset overview ===")
    print(ds)

    print("\n=== Variables ===")
    for name, da in ds.data_vars.items():
        print(f"  {name}: dims={da.dims}, shape={da.shape}, dtype={da.dtype}")
        units = da.attrs.get("units", "")
        long_name = da.attrs.get("long_name", "")
        if units or long_name:
            print(f"      long_name='{long_name}' units='{units}'")

    print("\n=== Coordinates ===")
    for name, coord in ds.coords.items():
        print(f"  {name}: shape={coord.shape}, range=[{np.nanmin(coord.values)}, {np.nanmax(coord.values)}]")

    if args.info_only:
        return

    # --- Pick a variable to plot ---
    var_name = args.var
    if var_name is None:
        # auto-pick the data variable with the most elements (usually the main field)
        var_name = max(ds.data_vars, key=lambda v: ds[v].size)
        print(f"\nNo --var given; auto-picked '{var_name}'")

    da = ds[var_name]
    print(f"\nPlotting variable '{var_name}' with dims {da.dims} and shape {da.shape}")

    # --- Identify spatial dims vs. extra dims (time, level, etc.) ---
    # Spatial dims may be literally named lat/lon, or a projected grid like y/x
    # (with lat/lon stored separately as 2D coordinate arrays keyed on y/x).
    lat_dim = next((d for d in da.dims if "lat" in d.lower()), None)
    lon_dim = next((d for d in da.dims if "lon" in d.lower()), None)
    if lat_dim is None or lon_dim is None:
        lat_dim = next((d for d in da.dims if d.lower() == "y"), lat_dim)
        lon_dim = next((d for d in da.dims if d.lower() == "x"), lon_dim)
    if lat_dim is None or lon_dim is None:
        sys.exit(f"Could not find lat/lon or y/x dims in {da.dims}; adjust the script for this variable's layout.")

    extra_dims = [d for d in da.dims if d not in (lat_dim, lon_dim)]

    # If we have true 2D lat/lon coordinate arrays keyed on the spatial dims
    # (common for projected grids), plot against those instead of raw y/x
    # so the map is in geographic coordinates.
    has_2d_latlon = (
        "lat" in ds.coords and "lon" in ds.coords
        and set(ds["lat"].dims) == {lat_dim, lon_dim}
        and set(ds["lon"].dims) == {lat_dim, lon_dim}
    )

    # Reduce extra dims (e.g. time, level/depth) down to a single 2D lat/lon slice
    for d in extra_dims:
        if "time" in d.lower():
            if args.agg == "mean":
                da = da.mean(dim=d)
                print(f"  Averaging over '{d}'")
            elif args.agg == "max":
                da = da.max(dim=d)
                print(f"  Taking max over '{d}'")
            else:
                idx = args.time if args.time is not None else 0
                da = da.isel({d: idx})
                print(f"  Selecting {d}={idx}")
        else:
            da = da.isel({d: args.level})
            print(f"  Selecting {d}={args.level}")

    print("Computing (loading into memory)... this may take a moment for large files.")
    da = da.load()

    # --- Plot ---
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(10, 6))
    if has_2d_latlon:
        # Curvilinear plot in true geographic coordinates
        im = da.plot.pcolormesh(
            x="lon", y="lat", ax=ax, cmap=args.cmap, add_colorbar=True
        )
        ax.set_xlabel("longitude")
        ax.set_ylabel("latitude")
    else:
        im = da.plot.pcolormesh(x=lon_dim, y=lat_dim, ax=ax, cmap=args.cmap, add_colorbar=True)
        ax.set_xlabel(lon_dim)
        ax.set_ylabel(lat_dim)
    units = da.attrs.get("units", "")
    long_name = da.attrs.get("long_name", var_name)
    ax.set_title(f"{long_name} ({units})" if units else long_name)
    fig.tight_layout()

    import os

    default_name = os.path.basename(args.file).rsplit(".", 1)[0] + f"_{var_name}.png"
    out_path = args.out
    if out_path is None:
        out_path = args.file.rsplit(".", 1)[0] + f"_{var_name}.png"
    elif out_path.endswith(os.sep) or os.path.isdir(out_path):
        os.makedirs(out_path, exist_ok=True)
        out_path = os.path.join(out_path, default_name)
    fig.savefig(out_path, dpi=150)
    print(f"\nSaved plot to: {out_path}")


if __name__ == "__main__":
    main()
