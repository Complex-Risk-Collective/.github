#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Step 2 of the baseline multi-hazard analysis: single-hazard frequency maps.

Reads the hourly flag files from hazard_flags.py (hazard_flags_YYYYMMDD.nc)
and computes, per grid cell and per flag, over the whole period:

    <flag>_hours        hours with the hazard present (flag == 1)
    <flag>_valid_hours  hours with valid data (flag >= 0)
    <flag>_frac         hours / valid_hours   (corrects for coverage gaps)
    <flag>_days         days with at least one flagged hour
    <flag>_valid_days   days with at least one valid hour

Cells that were never valid are NaN in _frac and in the maps (drawn gray).

Outputs (in out_dir):
    hazard_maps_<start>_<end>.nc
    hazard_maps_<metric>_<start>_<end>.png   one panel per flag
"""

import argparse
import glob
import os
import re

import numpy as np
import xarray as xr
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.colors import LinearSegmentedColormap

SKIP = {"hazard_count", "n_hazards_valid", "lat", "lon"}

# single-hue sequential ramp (light -> dark blue); NaN / never valid = gray
SEQ = LinearSegmentedColormap.from_list(
    "seq_blue", ["#f4f8fd", "#cde2fb", "#86b6ef", "#3987e5", "#1c5cab", "#0d366b"])
SEQ.set_bad("#d9d8d4")

METRIC_LABEL = {
    "days": "days with ≥1 flagged hour",
    "hours": "flagged hours",
    "frac": "fraction of valid hours flagged",
}


# -------------------------------------------------------------------------
# AGGREGATION
# -------------------------------------------------------------------------
def aggregate(flag_dir, pattern="hazard_flags_*.nc"):
    files = sorted(glob.glob(os.path.join(flag_dir, pattern)))
    if not files:
        raise FileNotFoundError(f"No files matching {pattern} in {flag_dir}")

    acc, kinds, desc = {}, {}, {}
    dates, coords = [], None

    for fp in files:
        m = re.search(r"_(\d{8})\.nc$", fp)
        dates.append(m.group(1) if m else os.path.basename(fp))
        with xr.open_dataset(fp) as ds:
            ds = ds.load()
        if coords is None:
            coords = {k: ds[k] for k in ("y", "x")}
            latlon = {k: ds[k] for k in ("lat", "lon") if k in ds}

        for name in ds.data_vars:
            if name in SKIP or "time" not in ds[name].dims:
                continue
            f = ds[name]
            kinds[name] = f.attrs.get("kind", "")
            desc[name] = f.attrs.get("description", name)
            day = {
                "hours": (f == 1).sum("time"),
                "valid_hours": (f >= 0).sum("time"),
                "days": (f == 1).any("time").astype("int32"),
                "valid_days": (f >= 0).any("time").astype("int32"),
            }
            if name not in acc:
                acc[name] = {k: v.astype("int32") for k, v in day.items()}
            else:
                for k, v in day.items():
                    acc[name][k] = acc[name][k] + v

    out = xr.Dataset(coords=coords)
    for name, d in acc.items():
        for k, v in d.items():
            out[f"{name}_{k}"] = v.astype("int32")
        frac = d["hours"] / d["valid_hours"].where(d["valid_hours"] > 0)
        out[f"{name}_frac"] = frac.astype("float32")
        for k in ("hours", "valid_hours", "days", "valid_days", "frac"):
            out[f"{name}_{k}"].attrs = {"kind": kinds[name],
                                        "description": desc[name]}
    for k, v in latlon.items():
        out[k] = v

    out.attrs = {"start": dates[0], "end": dates[-1], "n_days": len(dates),
                 "flags": ", ".join(acc)}
    return out, list(acc), kinds, desc


# -------------------------------------------------------------------------
# PLOTTING
# -------------------------------------------------------------------------
STATES_URL = ("https://www2.census.gov/geo/tiger/GENZ2018/shp/"
              "cb_2018_us_state_20m.zip")
NON_CONUS = {"AK", "HI", "PR"}


def _boundaries(path, epsg, cache_dir):
    """
    State outlines in the grid projection. `path` may be a local file, 'census'
    (download Census 1:20M states once and cache it), or None (no overlay).
    """
    if not path:
        return None
    import geopandas as gpd
    try:
        if path == "census":
            cached = os.path.join(cache_dir, "us_states_20m.gpkg")
            if os.path.exists(cached):
                gdf = gpd.read_file(cached)
            else:
                gdf = gpd.read_file(STATES_URL)
                gdf.to_file(cached, driver="GPKG")
        else:
            gdf = gpd.read_file(path)
        if "STUSPS" in gdf:
            gdf = gdf[~gdf["STUSPS"].isin(NON_CONUS)]
        return gdf.to_crs(epsg=epsg)
    except Exception as e:
        print(f"  ! boundaries not drawn ({e})")
        return None


def plot_maps(maps, names, kinds, desc, metric, out_png,
              bnd=None, ncols=3):
    import textwrap
    from matplotlib.colors import BoundaryNorm
    from mpl_toolkits.axes_grid1 import make_axes_locatable

    # hazards first, context flags last
    names = sorted(names, key=lambda n: (kinds.get(n) == "context", n))
    n = len(names)
    nrows = int(np.ceil(n / ncols))

    xs = maps.x.values          # projected metres (EPSG:5070)
    ys = maps.y.values
    dx = np.diff(xs).mean() / 2
    dy = np.diff(ys).mean() / 2
    extent = (xs.min() - dx, xs.max() + dx, ys.min() - dy, ys.max() + dy)
    aspect = (extent[3] - extent[2]) / (extent[1] - extent[0])

    panel_w = 5.0
    fig, axes = plt.subplots(nrows, ncols,
                             figsize=(panel_w * ncols,
                                      (panel_w * 0.88 * aspect + 1.0) * nrows),
                             squeeze=False)

    n_days = int(maps.attrs["n_days"])
    for ax, name in zip(axes.flat, names):
        val = maps[f"{name}_{metric}"].astype(float)
        val = val.where(maps[f"{name}_valid_hours"] > 0)  # gray = never valid

        if metric == "days":
            # whole days: one color step per value 0..n_days
            vmax = max(int(np.nanmax(val.values)) if np.isfinite(np.nanmax(val.values)) else 1, 1)
            bounds = np.arange(-0.5, vmax + 1.5, 1)
            norm = BoundaryNorm(bounds, SEQ.N)
            ticks = np.unique(np.linspace(0, vmax, min(vmax, 5) + 1).round().astype(int))
        else:
            vmax = float(np.nanmax(val.values)) if np.isfinite(np.nanmax(val.values)) else 1.0
            vmax = vmax if vmax > 0 else 1.0
            norm = matplotlib.colors.Normalize(0, vmax)
            ticks = None

        im = ax.pcolormesh(xs, ys, val.values, cmap=SEQ, norm=norm,
                           shading="nearest")
        if bnd is not None:
            bnd.boundary.plot(ax=ax, color="#6b6a66", linewidth=0.35)
        ax.set_xlim(extent[0], extent[1])
        ax.set_ylim(extent[2], extent[3])
        ax.set_aspect("equal")
        ax.set_xticks([]); ax.set_yticks([])
        ax.set_xlabel(""); ax.set_ylabel("")   # geopandas adds these
        for sp in ax.spines.values():
            sp.set_visible(False)

        tag = "  (context)" if kinds.get(name) == "context" else ""
        ax.set_title(f"{name}{tag}", fontsize=10, loc="left", color="#222")
        ax.text(0.0, -0.03, textwrap.fill(desc.get(name, ""), 62),
                transform=ax.transAxes, fontsize=7, color="#6b6a66",
                va="top", ha="left", linespacing=1.3)

        # colorbar exactly as tall as the map
        cax = make_axes_locatable(ax).append_axes("right", size="3%", pad=0.06)
        cb = fig.colorbar(im, cax=cax, ticks=ticks)
        cb.outline.set_visible(False)
        cb.ax.tick_params(labelsize=7, colors="#444", length=0)

    for ax in axes.flat[n:]:
        ax.set_visible(False)

    fig.suptitle(f"{METRIC_LABEL[metric]} \u2014 {maps.attrs['start']} to "
                 f"{maps.attrs['end']} ({n_days} days); gray = no data",
                 fontsize=11, x=0.01, y=1 - 0.2 / fig.get_figheight(),
                 ha="left", va="top", color="#222")
    fig_h = fig.get_figheight()
    fig.subplots_adjust(left=0.02, right=0.97,
                        top=1 - 0.75 / fig_h, bottom=0.55 / fig_h,
                        wspace=0.12, hspace=0.45)
    fig.savefig(out_png, dpi=150)
    plt.close(fig)


# -------------------------------------------------------------------------
# DRIVER
# -------------------------------------------------------------------------
def run(flag_dir, out_dir, metrics=("days", "frac"), boundaries=None):
    os.makedirs(out_dir, exist_ok=True)
    maps, names, kinds, desc = aggregate(flag_dir)
    tag = f"{maps.attrs['start']}_{maps.attrs['end']}"

    nc = os.path.join(out_dir, f"hazard_maps_{tag}.nc")
    maps.to_netcdf(nc, encoding={v: {"zlib": True, "complevel": 4}
                                 for v in maps.data_vars})
    print(f"Saved {nc}")

    bnd = _boundaries(boundaries, epsg=5070, cache_dir=out_dir)
    for metric in metrics:
        png = os.path.join(out_dir, f"hazard_maps_{metric}_{tag}.png")
        plot_maps(maps, names, kinds, desc, metric, png, bnd=bnd)
        print(f"Saved {png}")

    # quick text summary: domain share of cells ever flagged
    print("\nCells flagged on ≥1 day (share of ever-valid cells):")
    for name in sorted(names, key=lambda n: (kinds[n] == "context", n)):
        ever = int((maps[f"{name}_days"] > 0).sum())
        valid = int((maps[f"{name}_valid_days"] > 0).sum())
        share = 100 * ever / valid if valid else float("nan")
        print(f"  {name:<18s} {ever:6d} / {valid:<6d} ({share:5.1f}%)  [{kinds[name]}]")
    return maps


if __name__ == "__main__":
    PATH = "/Volumes/MLIA_active_data/gridresilience/data/terrestrial/V2"
    p = argparse.ArgumentParser()
    p.add_argument("--flag_dir", default=f"{PATH}/hazard_flags")
    p.add_argument("--out_dir", default=f"{PATH}/hazard_maps")
    p.add_argument("--metrics", nargs="+", default=["days", "frac"],
                   choices=list(METRIC_LABEL))
    p.add_argument("--boundaries", default="census",
                   help="'census' (download US state outlines once, cached in "
                        "out_dir), a local shapefile/GeoJSON, or '' for none")
    a = p.parse_args()
    
    run(a.flag_dir, a.out_dir, metrics=a.metrics, boundaries=a.boundaries)
