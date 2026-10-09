#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Created on Thu Nov  6 12:35:24 2025

@author: marchett

v2 changes (Step 0 fixes):
  [FIX 1] MRMS sentinel codes (-999 / -99 / -3) are masked before binning.
  [FIX 2] Empty / uncovered grid cells stay NaN (no nan_to_num), and a
          per-cell `<var>_valid_frac` is saved.
  [FIX 3] Per-variable statistic (max for hazard fields instead of mean).
  [FIX 4] PrecipFlag is aggregated as class fractions + dominant class.
  [FIX 5] Several files per hour are combined (hourly max) instead of only
          the first file; accumulated products use the file nearest HH:00.
  [FIX 6] Fast, cached binning (pixel -> cell index computed once per raw grid).
"""

import boto3
from botocore import UNSIGNED
from botocore.client import Config
from pathlib import Path
import argparse
from data_processing.grid import AnalysisGridTemplate
from collections import defaultdict
import numpy as np
import os
import sys
from contextlib import contextmanager
import warnings

os.environ["ECCODES_SILENT"] = "1"
os.environ["ECCODES_IGNORE_INDEX_FILE"] = "1"

import xarray as xr
import gzip
import pygrib
import shutil
import pandas as pd
import re


PROCESSING_VERSION = "v2.1"   # bump when gridding logic changes -> cached files are rebuilt
# v2.1: no-echo pixels stored as missing now become 0 (ZERO_WHEN_MISSING)


# =====================================================================
# CONFIG: sentinels, statistics, sub-hourly sampling
# =====================================================================

# [FIX 1] MRMS writes "no data" as real numbers, not NaN.
# VERIFY on one raw file per product:
#     v = ds[var].values; print(np.unique(v[v < 0]))
NO_COVERAGE_CODES = (-999.0,)   # outside radar coverage        -> NaN
NO_ECHO_CODES     = (-99.0,)    # scanned, nothing detected     -> floor value
PRECIP_NO_COVERAGE_CODES = (-3.0,)  # QPE / PrecipRate / PrecipFlag "no coverage"

# Products that can never be negative: any leftover negative value is invalid.
NONNEG_PATTERNS = ("QPE", "PrecipRate", "PrecipFlag", "MESH", "VII", "VIL",
                   "NLDN", "EchoTop", "RotationTrack", "POSH", "STREAMFLOW", "SOILSAT")
# Value used for "no echo" in signed products (dBZ, shear)
NO_ECHO_FLOOR = {"Reflectivity": -30.0, "AzShear": 0.0}

# [FIX 3] Statistic per variable. Hazards -> max (keeps the extreme inside a
# 50 km cell). Anything not listed uses DEFAULT_STAT.
DEFAULT_STAT = "max"
MRMS_STAT = {
    # e.g. "MultiSensor_QPE_72H_Pass2_00.00": "mean",  # areal mean instead
}

# [FIX 4] PrecipFlag classes (MRMS surface precip type). VERIFY codes with np.unique.
PRECIPFLAG_VAR = "PrecipFlag_00.00"
PRECIPFLAG_CLASSES = {
    "none": [0],
    "rain": [1, 6, 10, 91, 96],   # warm strat, convective, cool strat, tropical mixes
    "snow": [3],
    "hail": [7],                  # rain mixed with hail
}

# [FIX 5] Products that are already accumulated / windowed: one file per hour
# (the one nearest HH:00) is enough. Everything else is instantaneous and gets
# FILES_PER_HOUR evenly spaced files, combined with an hourly max.
# SOILSAT changes slowly, so one file per hour (the one at HH+1:00) is enough
ACCUMULATED_PATTERNS = ("QPE", "RotationTrack", "015min", "Max_60min", "SOILSAT")
# Accumulation length (min) of each file; files ending every WINDOW minutes are
# taken so the hour [HH, HH+1) is fully covered. Default 60.
ACCUMULATION_MINUTES = {"015min": 15}
MATCH_TOLERANCE = pd.Timedelta(minutes=5)   # max offset from the target end time
# Lightning count: estimated CG flashes per cell per hour, derived from the
# 15-min average-density product (hour mean density x 60 min x cell area).
LIGHTNING_COUNT_SOURCE = "NLDN_CG_015min_AvgDensity_00.00"
LIGHTNING_COUNT_VAR = "NLDN_CG_flashes_1h"

FILES_PER_HOUR = 6   # 1 = old behaviour; 6 ~ every 10 min; cost scales linearly


def _is_nonneg(variable):
    return any(p in variable for p in NONNEG_PATTERNS)


def _is_accumulated(variable):
    return any(p in variable for p in ACCUMULATED_PATTERNS)


def _stat_for(variable):
    return MRMS_STAT.get(variable, DEFAULT_STAT)


def _match(values, codes, tol=0.5):
    """Tolerant equality: GRIB packing can turn -999 into -999.0001."""
    m = np.zeros(values.shape, dtype=bool)
    for c in codes:
        m |= np.abs(values - c) < tol
    return m


# Products that store "nothing detected" as missing instead of 0 (seen in real data:
# MESH, EchoTop, VII, VIL_Density and NLDN were NaN in 70-96 % of cells that radar
# covers). For these, every missing or negative pixel becomes 0; real radar gaps
# are put back afterwards from composite reflectivity (apply_radar_coverage).
ZERO_WHEN_MISSING = ("EchoTop", "VII", "VIL_Density", "MESH", "POSH", "NLDN")
RADAR_COVERAGE_VAR = "MergedReflectivityComposite_00.50"


def _zero_when_missing(variable):
    return any(p in variable for p in ZERO_WHEN_MISSING)


def clean_mrms(values, variable):
    """[FIX 1] Replace MRMS sentinel codes with NaN (no coverage) or a floor (no echo)."""
    v = np.asarray(values, dtype="float64").copy()
    if _zero_when_missing(variable):
        v[~np.isfinite(v) | (v < 0)] = 0.0
        return v
    v[_match(v, NO_COVERAGE_CODES)] = np.nan

    if _is_nonneg(variable):
        v[_match(v, PRECIP_NO_COVERAGE_CODES)] = np.nan
        v[_match(v, NO_ECHO_CODES)] = 0.0
        v[v < 0] = np.nan                       # anything else negative is junk
    else:
        floor = next((f for k, f in NO_ECHO_FLOOR.items() if k in variable), np.nan)
        v[_match(v, NO_ECHO_CODES)] = floor
    return v


# =====================================================================
# S3 listing / download
# =====================================================================

def extract_time_from_key(key):
    """
    Parse MRMS filename timestamp YYYYMMDD-HHMMSS -> pd.Timestamp
    Example: MRMS_VIL_Density_00.50_20230331-000038.grib2.gz
    """
    m = re.search(r"(\d{8})-(\d{6})", key)
    if not m:
        raise ValueError(f"Cannot parse time from {key}")
    return pd.to_datetime(m.group(1) + m.group(2), format="%Y%m%d%H%M%S")


def list_files_by_hour(region, variable, date, s3, bucket):
    """
    [FIX 5] Return {hour: [(timestamp, key), ...]} for ALL files of the day
    (the old list_hourly_files kept only the first file of each hour).
    """
    prefix = f"{region}/{variable}/{date.strftime('%Y%m%d')}/"
    paginator = s3.get_paginator("list_objects_v2")
    by_hour = defaultdict(list)
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            try:
                t = extract_time_from_key(key)
            except ValueError:
                continue
            by_hour[t.hour].append((t, key))
    for h in by_hour:
        by_hour[h].sort()
    return by_hour


def _window_minutes(variable):
    return next((m for p, m in ACCUMULATION_MINUTES.items() if p in variable), 60)


def select_keys_for_hour(files, hour_start, variable, files_per_hour=FILES_PER_HOUR):
    """
    Pick the files that represent the hour labelled HH = [HH:00, HH+1:00).
    `files` is a time-sorted list of (timestamp, key); for accumulated products
    it must include the first files of the next day (for hour 23).

      accumulated products -> files whose accumulation ENDS at HH+W, HH+2W, ... HH+60
                              (W = accumulation length), so [HH, HH+1) is covered
      instantaneous        -> files_per_hour evenly spaced files inside the hour
    """
    if not files:
        return []
    hour_end = hour_start + pd.Timedelta(hours=1)

    if _is_accumulated(variable):
        w = _window_minutes(variable)
        keys = []
        for k in range(1, 60 // w + 1):
            target = hour_start + pd.Timedelta(minutes=k * w)
            t, key = min(files, key=lambda tk: abs(tk[0] - target))
            if abs(t - target) <= MATCH_TOLERANCE:
                keys.append(key)
        return keys

    inside = [tk for tk in files if hour_start <= tk[0] < hour_end]
    if not inside:
        return []
    n = max(1, files_per_hour)
    idx = np.unique(np.linspace(0, len(inside) - 1, n).round().astype(int))
    return [inside[i][1] for i in idx]


def time_window_text(variable, n_files):
    """Human-readable description of what the hourly value covers."""
    if _is_accumulated(variable):
        w = _window_minutes(variable)
        if w == 60:
            return ("[HH:00, HH+1:00): product accumulated/tracked up to HH+1:00 "
                    "(longer windows such as 24H/72H also end at HH+1:00)")
        return f"[HH:00, HH+1:00): max of {n_files} consecutive {w}-min windows"
    how = "mean" if variable == PRECIPFLAG_VAR else "max"
    return f"[HH:00, HH+1:00): {how} over {n_files} files"


def download_file(key, s3, bucket, local_dir="downloads"):
    """Download a single file from S3, un-gzip if needed."""
    os.makedirs(local_dir, exist_ok=True)
    filename = os.path.join(local_dir, os.path.basename(key))
    if os.path.exists(filename.replace(".gz", "")):
        return filename.replace(".gz", "")

    s3.download_file(bucket, key, filename)
    if filename.endswith(".gz"):
        unzipped = filename[:-3]
        with gzip.open(filename, "rb") as f_in, open(unzipped, "wb") as f_out:
            shutil.copyfileobj(f_in, f_out)
        os.remove(filename)
        return unzipped
    return filename


# =====================================================================
# [SPEED] In-memory read: S3 -> gunzip -> pygrib, no files on disk
# =====================================================================

_LATLON_CACHE = {}


@contextmanager
def _quiet_c_stderr():
    """
    Silence stderr at the OS level (fd 2) for C-library messages such as
    ecCodes' 'Truncating time: non-zero seconds ignored' for MRMS files,
    whose timestamps include seconds (time is taken from the filename).
    Python exceptions still propagate normally.
    """
    sys.stderr.flush()
    saved = os.dup(2)
    devnull = os.open(os.devnull, os.O_WRONLY)
    try:
        os.dup2(devnull, 2)
        yield
    finally:
        os.dup2(saved, 2)
        os.close(devnull)
        os.close(saved)


def fetch_grib_bytes(key, s3, bucket):
    """Read an S3 object into memory and un-gzip it."""
    body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    return gzip.decompress(body) if key.endswith(".gz") else body


def decode_mrms(grib_bytes):
    """
    Decode a single-message MRMS GRIB2 from bytes.
    Returns (values (nlat, nlon) float64 with NaN for masked points, lat1d, lon1d).
    The regular lat/lon axes are computed once per grid and cached.
    The whole CONUS field is kept: pixels outside the analysis grid are
    dropped by the binning index, so no lat/lon subsetting is needed.
    """
    with _quiet_c_stderr():
        grb = pygrib.fromstring(grib_bytes)
        values = np.ma.filled(np.ma.asarray(grb.values, dtype="float64"), np.nan)

        gkey = (grb["Ni"], grb["Nj"],
                grb["latitudeOfFirstGridPointInDegrees"],
                grb["longitudeOfFirstGridPointInDegrees"],
                grb["iDirectionIncrementInDegrees"],
                grb["jDirectionIncrementInDegrees"])
        if gkey not in _LATLON_CACHE:
            lats, lons = grb.latlons()
            _LATLON_CACHE[gkey] = (lats[:, 0].copy(), lons[0, :].copy())
    lat1d, lon1d = _LATLON_CACHE[gkey]
    return values, lat1d, lon1d


def read_mrms_key(key, s3, bucket):
    """S3 key -> (values, lat1d, lon1d), entirely in memory."""
    return decode_mrms(fetch_grib_bytes(key, s3, bucket))


# =====================================================================
# Load + subset (file-based; kept for other scripts, no longer used here)
# =====================================================================

def load_and_subset_nc(file_path, variable, lat_bounds=None, lon_bounds=None):
    """Load an MRMS GRIB2 file (eagerly) and optionally subset by lat/lon."""
    # indexpath="" -> cfgrib writes no .idx side file (nothing to clean up)
    with xr.open_dataset(file_path, engine="cfgrib",
                         backend_kwargs={"indexpath": ""}) as ds:
        if "unknown" in ds.data_vars:
            ds = ds.rename({"unknown": variable})

        if lat_bounds and lon_bounds:
            lon0, lon1 = [l + 360 if l < 0 else l for l in lon_bounds]
            lat_start, lat_end = lat_bounds
            if ds.latitude.values[0] > ds.latitude.values[-1]:
                lat_start, lat_end = lat_end, lat_start
            ds = ds.sel(latitude=slice(lat_start, lat_end),
                        longitude=slice(lon0, lon1))
        ds = ds.load()   # read before the file is deleted
    return ds


# =====================================================================
# [FIX 2 + 6] Binning onto AnalysisGridTemplate
# =====================================================================

_BIN_CACHE = {}


def _get_latlon_2d(ds, data_var):
    """lat/lon for data_var: 1D (MRMS, regular grid) or 2D (RTMA), as stored."""
    lat = data_var["latitude"].values if "latitude" in data_var.coords else ds["latitude"].values
    lon = data_var["longitude"].values if "longitude" in data_var.coords else ds["longitude"].values
    return lat, lon


def _cell_index(lat, lon, template):
    """
    Map every raw pixel to a flat grid-cell index ONCE per raw grid and cache it.
    Raw grids are identical from hour to hour, so re-projecting ~20M points
    for every file (old code) is wasted work.
    lat/lon: 1D (regular grid, values shaped (nlat, nlon)) or 2D.
    The 2D mesh for 1D input is built only on a cache miss.
    """
    shape = (lat.size, lon.size) if lat.ndim == 1 else lat.shape
    key = (id(template), lat.ndim, shape,
           float(lat.flat[0]), float(lat.flat[-1]),
           float(lon.flat[0]), float(lon.flat[-1]))
    if key in _BIN_CACHE:
        return _BIN_CACHE[key]

    if lat.ndim == 1:
        lon2d, lat2d = np.meshgrid(lon, lat)
    else:
        lat2d, lon2d = lat, lon

    lon = ((lon2d.ravel() + 180.0) % 360.0) - 180.0      # 0..360 -> -180..180
    xs, ys = template.to_proj.transform(lon, lat2d.ravel())

    nx, ny = len(template.x_centers), len(template.y_centers)
    ix = np.searchsorted(template.x_edges, xs, side="right") - 1
    iy = np.searchsorted(template.y_edges, ys, side="right") - 1
    inside = (ix >= 0) & (ix < nx) & (iy >= 0) & (iy < ny)
    flat = np.where(inside, iy * nx + ix, -1)

    order = np.argsort(flat, kind="stable")
    pix = order[flat[order] >= 0]                 # raw pixels inside grid, sorted by cell
    cells = flat[pix].astype(np.int32)
    uniq, starts = np.unique(cells, return_index=True)
    total = np.bincount(cells, minlength=nx * ny)  # raw pixels per cell (valid or not)

    entry = dict(pix=pix, cells=cells, uniq=uniq, starts=starts,
                 total=total, shape=(ny, nx))
    _BIN_CACHE[key] = entry
    return entry


def aggregate_to_grid(values2d, lat2d, lon2d, template, statistic="mean"):
    """
    Aggregate a raw 2D field onto the template grid.
    Returns (arr, valid_frac), both (ny, nx):
      arr        : statistic of valid pixels; NaN where a cell has none   [FIX 2]
      valid_frac : valid raw pixels / raw pixels falling in the cell
    statistic: 'mean' | 'max' | 'min' | 'median' | 'count'
               | 'frac_lt:<thr>' | 'frac_gt:<thr>'  (share of valid pixels
                 below / above thr, e.g. 'frac_lt:1000')
    """
    c = _cell_index(lat2d, lon2d, template)
    ny, nx = c["shape"]
    n = ny * nx

    v = np.asarray(values2d, dtype="float64").ravel()[c["pix"]]
    valid = ~np.isnan(v)
    n_valid = np.bincount(c["cells"][valid], minlength=n)

    if statistic.startswith(("frac_lt:", "frac_gt:")):
        thr = float(statistic.split(":", 1)[1])
        hit = (v < thr) if statistic.startswith("frac_lt") else (v > thr)
        k = np.bincount(c["cells"][valid & hit], minlength=n)
        with np.errstate(invalid="ignore", divide="ignore"):
            out = k / n_valid
    elif statistic == "median":
        cv, vv = c["cells"][valid], v[valid]
        order = np.lexsort((vv, cv))            # sort by cell, then value
        vv = vv[order]
        starts = np.concatenate([[0], np.cumsum(n_valid)[:-1]])
        has = n_valid > 0
        lo = starts[has] + (n_valid[has] - 1) // 2
        hi = starts[has] + n_valid[has] // 2
        out = np.full(n, np.nan)
        out[has] = 0.5 * (vv[lo] + vv[hi])
    elif statistic == "mean":
        s = np.bincount(c["cells"][valid], weights=v[valid], minlength=n)
        with np.errstate(invalid="ignore", divide="ignore"):
            out = s / n_valid
    elif statistic in ("max", "min"):
        fill = -np.inf if statistic == "max" else np.inf
        ufunc = np.maximum if statistic == "max" else np.minimum
        w = np.where(valid, v, fill)
        out = np.full(n, np.nan)
        if len(w):
            out[c["uniq"]] = ufunc.reduceat(w, c["starts"])
        out[~np.isfinite(out)] = np.nan
    elif statistic == "count":
        out = n_valid.astype(float)
    else:
        raise ValueError(f"Unsupported statistic: {statistic}")

    out[n_valid == 0] = np.nan if statistic != "count" else 0.0
    with np.errstate(invalid="ignore", divide="ignore"):
        valid_frac = np.where(c["total"] > 0, n_valid / c["total"], np.nan)

    return out.reshape(ny, nx), valid_frac.reshape(ny, nx)


def bin_to_grid(ds, template, var_name, statistic="mean",
                coarsen_factor=None, return_valid_frac=False):
    """
    Backward-compatible wrapper.
    CHANGED: empty cells are now NaN (old code replaced them with 0).
    """
    data_var = ds[var_name]
    if coarsen_factor is not None:
        lat_f, lon_f = coarsen_factor
        data_var = data_var.coarsen(latitude=lat_f, longitude=lon_f,
                                    boundary="trim").mean()
    # coordinates taken from data_var itself -> correct after coarsening too
    lat2d, lon2d = _get_latlon_2d(ds, data_var)
    arr, vf = aggregate_to_grid(data_var.values, lat2d, lon2d, template, statistic)
    return (arr, vf) if return_valid_frac else arr


def grid_mrms_file(ds_raw, variable, template):
    """Clean + bin one MRMS file already loaded as an xarray Dataset."""
    lat, lon = _get_latlon_2d(ds_raw, ds_raw[variable])
    return grid_mrms_array(ds_raw[variable].values, lat, lon, variable, template)


def grid_mrms_array(raw_values, lat2d, lon2d, variable, template):
    """
    Clean + bin one MRMS field. Returns {output_var_name: (arr, valid_frac)}.
    PrecipFlag -> one fraction field per class  [FIX 4]
    """
    values = clean_mrms(raw_values, variable)

    if variable == PRECIPFLAG_VAR:
        out = {}
        for cls, codes in PRECIPFLAG_CLASSES.items():
            ind = np.where(np.isnan(values), np.nan, np.isin(values, codes).astype(float))
            out[f"PrecipFlag_frac_{cls}"] = aggregate_to_grid(ind, lat2d, lon2d, template, "mean")
        return out

    out = {variable: aggregate_to_grid(values, lat2d, lon2d, template, _stat_for(variable))}

    if variable == LIGHTNING_COUNT_SOURCE:
        # cell-mean density (flashes km-2 min-1) x cell area (km2) = flashes per minute
        dens, vf = aggregate_to_grid(values, lat2d, lon2d, template, "mean")
        cell_area_km2 = (np.diff(template.x_centers).mean()
                         * np.diff(template.y_centers).mean()) / 1e6   # EPSG:5070 is equal-area
        out[LIGHTNING_COUNT_VAR] = (dens * cell_area_km2, vf)
    return out


def combine_within_hour(results, variable):
    """
    [FIX 5] Combine gridded results from several files of the same hour.
      'max'/'min' stats -> elementwise fmax/fmin (ignores NaN)
      means / fractions -> nanmean
      valid_frac        -> mean over files
    """
    combined = {}
    for name in results[0]:
        if name == LIGHTNING_COUNT_VAR or variable == PRECIPFLAG_VAR:
            stat = "mean"
        else:
            stat = _stat_for(variable)
        arrs = np.stack([r[name][0] for r in results])
        vfs = np.stack([r[name][1] for r in results])
        with warnings.catch_warnings():          # all-NaN cells -> NaN, silently
            warnings.simplefilter("ignore", RuntimeWarning)
            if stat == "max":
                a = np.fmax.reduce(arrs, axis=0)
            elif stat == "min":
                a = np.fmin.reduce(arrs, axis=0)
            else:
                a = np.nanmean(arrs, axis=0)
            vf = np.nanmean(vfs, axis=0)
        if name == LIGHTNING_COUNT_VAR:
            a = a * 60.0      # mean flashes per minute over the hour -> flashes per hour
        combined[name] = (a, vf)
    return combined


def apply_radar_coverage(ds):
    """
    Put radar gaps back for the ZERO_WHEN_MISSING radar products: a cell is NaN
    where composite reflectivity has no data in that hour. Lightning (NLDN) is
    not radar-based and keeps its zeros everywhere.
    """
    if RADAR_COVERAGE_VAR not in ds:
        print(f"  Note: {RADAR_COVERAGE_VAR} not downloaded; radar gaps in "
              f"{', '.join(ZERO_WHEN_MISSING)} products will show as 0")
        return ds
    cov = ds[RADAR_COVERAGE_VAR].notnull()
    cov_frac = ds.get(f"{RADAR_COVERAGE_VAR}_valid_frac")
    with xr.set_options(keep_attrs=True):
        for v in list(ds.data_vars):
            if v.endswith("_valid_frac") or not _zero_when_missing(v) or v.startswith("NLDN"):
                continue
            ds[v] = ds[v].where(cov)
            ds[v].attrs["coverage"] = f"0 = nothing detected; NaN where {RADAR_COVERAGE_VAR} has no data"
            if cov_frac is not None and f"{v}_valid_frac" in ds:
                ds[f"{v}_valid_frac"] = cov_frac.copy()
    return ds


def add_precipflag_dominant(ds):
    """[FIX 4] Dominant precip class per cell/hour, CF flag encoding."""
    classes = list(PRECIPFLAG_CLASSES)
    names = [f"PrecipFlag_frac_{c}" for c in classes]
    if not all(n in ds for n in names):
        return ds
    stack = xr.concat([ds[n] for n in names], dim="cls")
    all_nan = stack.isnull().all("cls")
    dom = stack.fillna(-1).argmax("cls").astype("int8").where(~all_nan, -1)
    ds["PrecipFlag_dominant"] = dom.astype("int8")
    ds["PrecipFlag_dominant"].attrs = {
        "flag_values": list(range(len(classes))),
        "flag_meanings": " ".join(classes),
        "_FillValue_meaning": "-1 = no coverage",
        "cell_statistic": "argmax of class fractions",
        "time_window": "[HH:00, HH+1:00)",
    }
    return ds


def add_latlon(ds, epsg_proj=5070):
    import pyproj
    transformer = pyproj.Transformer.from_crs(f"EPSG:{epsg_proj}", "EPSG:4326",
                                              always_xy=True)
    xx, yy = np.meshgrid(ds["x"].values, ds["y"].values)
    lon2d, lat2d = transformer.transform(xx, yy)
    ds_out = ds.copy()
    ds_out["lat"] = (("y", "x"), lat2d)
    ds_out["lon"] = (("y", "x"), lon2d)
    return ds_out


# =====================================================================
# Safe saving and reuse of gridded files
# =====================================================================

def atomic_to_netcdf(ds, path, **kwargs):
    """
    Write to <path>.tmp, then rename. An interrupted run leaves only the .tmp
    file, never a half-written file under the real name that a later run
    would mistake for a finished one.
    """
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    tmp.unlink(missing_ok=True)
    ds.to_netcdf(tmp, **kwargs)
    os.replace(tmp, path)


def cached_file_ok(path, variables=(), version=PROCESSING_VERSION):
    """
    True if a saved gridded file can be reused: same processing version,
    every requested product present, and no files failed while making it.
    Returns (ok, reason).
    """
    path = Path(path)
    if not path.exists():
        return False, "not found"
    try:
        with xr.open_dataset(path) as ds:
            attrs, names = dict(ds.attrs), set(ds.data_vars)
            sources = {ds[v].attrs.get("source_product") for v in ds.data_vars}
    except Exception as e:
        return False, f"unreadable ({e})"
    if attrs.get("processing_version") != version:
        return False, f"made with version {attrs.get('processing_version')}, current is {version}"
    missing = [v for v in variables if v not in names and v not in sources]
    if missing:
        return False, f"missing {', '.join(missing)}"
    if int(attrs.get("n_failed_files", 0)) > 0:
        return False, f"{attrs['n_failed_files']} source file(s) failed when it was made"
    return True, "ok"


# =====================================================================
# Full pipeline: download, clean, grid, combine
# =====================================================================

def download_and_grid(region, variables, start_date, end_date, root,
                      lat_bounds, lon_bounds, res_km=50, epsg=5070,
                      grid=None, output_file=None, files_per_hour=FILES_PER_HOUR):
    """Download MRMS files for a date range and grid them into one hourly Dataset."""
    bucket = "noaa-mrms-pds"
    s3 = boto3.client("s3", config=Config(signature_version=UNSIGNED))

    root = Path(root)
    output_dir = root / "mrms"
    output_dir.mkdir(parents=True, exist_ok=True)

    start_dt = pd.to_datetime(start_date).floor("h")
    end_dt = pd.to_datetime(end_date).floor("h")

    if output_file is None:
        tag = f"{start_dt:%Y%m%dT%H}_{end_dt:%Y%m%dT%H}"
        output_file = output_dir / f"MRMS_{region}_{tag}.nc"
    output_file = Path(output_file)
    ok, reason = cached_file_ok(output_file, variables)
    if ok:
        print(f"  Reusing {output_file.name}")
        return xr.open_dataset(output_file)
    if output_file.exists():
        print(f"  Rebuilding {output_file.name}: {reason}")

    if grid is None:
        grid = AnalysisGridTemplate(
            grid_file=output_dir / f"MRMS_{region}_grid_{res_km}km.nc",
            lat_bounds=lat_bounds, lon_bounds=lon_bounds,
            res_km=res_km, epsg=epsg)

    time_index = pd.date_range(start=start_dt, end=end_dt, freq="1h")
    per_var = defaultdict(list)          # output name -> list of hourly Datasets
    var_attrs = {}
    failed = []                          # S3 keys that could not be read

    listing_cache = {}

    def listing(variable, d):
        k = (variable, d)
        if k not in listing_cache:
            listing_cache[k] = list_files_by_hour(region, variable, d, s3, bucket)
        return listing_cache[k]

    day = start_dt.normalize()
    while day <= end_dt:
        print(f"Processing {day.date()}...")
        for variable in variables:
            by_hour = listing(variable, day)
            n_found = sum(len(v) for v in by_hour.values())
            print(f"  {variable}: {n_found} files in {len(by_hour)} hours")

            files = sorted(tk for h in by_hour.values() for tk in h)
            if _is_accumulated(variable):
                # hour 23 needs the accumulation ending at 00:00 of the next day
                nxt = listing(variable, day + pd.Timedelta(days=1))
                files += nxt.get(0, [])

            for hour in range(24):
                hour_start = day + pd.Timedelta(hours=hour)
                if hour_start not in time_index:
                    continue
                keys = select_keys_for_hour(files, hour_start, variable,
                                            files_per_hour)
                if not keys:
                    continue
                results = []
                for key in keys:
                    try:
                        values, lat1d, lon1d = read_mrms_key(key, s3, bucket)  # [SPEED]
                        results.append(grid_mrms_array(values, lat1d, lon1d,
                                                       variable, grid))
                    except Exception as e:
                        failed.append(key)
                        print(f"    Warning: failed on {key}: {e}")
                if not results:
                    continue

                hourly = combine_within_hour(results, variable)
                for name, (arr, vf) in hourly.items():
                    per_var[name].append(xr.Dataset(
                        {name: (("y", "x"), arr.astype("float32")),
                         f"{name}_valid_frac": (("y", "x"), vf.astype("float32"))},
                        coords={"y": grid.y_centers, "x": grid.x_centers,
                                "time": hour_start}))
                    var_attrs[name] = {
                        "source_product": variable,
                        "cell_statistic": ("mean of class indicator (area fraction)"
                                           if variable == PRECIPFLAG_VAR
                                           else _stat_for(variable)),
                        "time_window": time_window_text(variable, len(keys)),
                    }
                    if name == LIGHTNING_COUNT_VAR:
                        var_attrs[name].update({
                            "cell_statistic": ("cell-mean density x cell area, "
                                               "mean over files x 60 min"),
                            "time_window": (f"[HH:00, HH+1:00): full hour from "
                                            f"{len(keys)} consecutive 15-min windows"),
                        })
        day += pd.Timedelta(days=1)

    if not per_var:
        raise ValueError("No MRMS data found for requested period.")

    combined_vars = []
    for name, ds_list in per_var.items():
        dsv = xr.concat(ds_list, dim="time").sortby("time").drop_duplicates("time")
        dsv = dsv.reindex(time=time_index)          # missing hours -> NaN
        dsv[name].attrs.update(var_attrs[name])
        dsv[f"{name}_valid_frac"].attrs.update({
            "source_product": var_attrs[name]["source_product"],
            "cell_statistic": "fraction of raw pixels with valid data (mean over files)",
            "time_window": var_attrs[name]["time_window"],
        })
        combined_vars.append(dsv)

    combined = xr.merge(combined_vars, compat="no_conflicts", join="exact")
    combined = apply_radar_coverage(combined)
    combined = add_precipflag_dominant(combined)
    combined = add_latlon(combined, epsg_proj=epsg)
    combined.attrs.update({
        "processing_version": PROCESSING_VERSION,
        "files_per_hour": files_per_hour,
        "n_failed_files": len(failed),
        "failed_files": "; ".join(failed[:50]),
    })
    if failed:
        print(f"  {len(failed)} file(s) failed; saved, but the next run will rebuild this file")

    atomic_to_netcdf(combined, output_file)
    print(f"Saved combined NetCDF to {output_file}")
    return combined


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--region", default="CONUS")
    parser.add_argument("--start", default="2024-06-25")
    parser.add_argument("--end", default="2024-06-25")
    parser.add_argument("--root", default="/Users/marchett/Documents/Disasters")
    parser.add_argument("--variables", nargs="+",
                        default=["VIL_Density_00.50", "ReflectivityAtLowestAltitude_00.50"])
    parser.add_argument("--latmin", type=float, default=25)
    parser.add_argument("--latmax", type=float, default=50)
    parser.add_argument("--lonmin", type=float, default=-125)
    parser.add_argument("--lonmax", type=float, default=-65)
    parser.add_argument("--files-per-hour", type=int, default=FILES_PER_HOUR)
    args = parser.parse_args()

    download_and_grid(region=args.region, variables=args.variables,
                      start_date=args.start, end_date=args.end, root=args.root,
                      lat_bounds=[args.latmin, args.latmax],
                      lon_bounds=[args.lonmin, args.lonmax],
                      files_per_hour=args.files_per_hour)


if __name__ == "__main__":
    main()
