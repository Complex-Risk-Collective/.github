#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Created on Tue Mar  3 15:39:43 2026

@author: marchett

v2 changes (Step 0 fixes):
  [FIX 1] GRIB missing values (bitmap) become NaN explicitly.
  [FIX 2] Empty cells stay NaN; one `rtma_valid_frac` field is saved.
  [FIX 3] Per-variable statistic: max for gust / wind speed, mean otherwise.
  [FIX 7] Wind direction is not averaged as an angle: earth-relative u/v are
          built from wspd10 + wdir10, averaged, and wdir10 is recomputed.
  [FIX 8] Output reindexed to the full hourly axis (gaps become NaN, visible).
  [FIX 9] orog stored once (no time dim); dead timestamp code removed
          (it called datetime.datetime.strptime after `from datetime import datetime`).
  [v2.2]  Pixel-level derived fields: rh2_min, heat_index_max, fireweather_frac.
  [v2.1]  Several statistics per variable (RTMA_OUTPUTS): min/max for
          temperature and moisture, median + min + fraction-below-threshold
          for visibility and ceiling.
"""

import os
import re
from datetime import timedelta
from pathlib import Path

import boto3
import numpy as np
import pandas as pd
import pygrib
import xarray as xr
from botocore import UNSIGNED
from botocore.client import Config

from data_processing.terrestrial.get_aggregate_mrms_data import aggregate_to_grid, add_latlon


# ------------------------
# RTMA CONFIG
# ------------------------

RTMA_VARS = {
    "10u": "u10",        # u-component 10m wind (grid-relative on the NDFD grid)
    "10v": "v10",        # v-component 10m wind (grid-relative on the NDFD grid)
    "10si": "wspd10",    # wind speed 10m
    "10wdir": "wdir10",  # wind direction 10m (meteorological, "from")
    "2t": "t2m",         # 2m temperature
    "2d": "d2m",         # 2m dew point
    "2sh": "sh2",        # 2m specific humidity
    "sp": "sp",          # surface pressure
    "vis": "vis",        # visibility
    "ceil": "ceil",      # cloud ceiling
    "tcc": "tcc",        # total cloud cover
    "orog": "orog",      # surface elevation (static)
    "i10fg": "i10fg",    # 10m wind GUST (old comment said "icing" - wrong)
}

# Statistics per RTMA field: list of (output suffix, statistic).
# The first entry keeps the plain variable name; others become <var>_<suffix>.
# Statistics: mean | median | min | max | frac_lt:<thr> | frac_gt:<thr>
RTMA_OUTPUTS = {
    "t2m":    [("", "mean"), ("min", "min"), ("max", "max")],
    "d2m":    [("", "mean"), ("max", "max")],
    "sh2":    [("", "mean"), ("max", "max")],
    "sp":     [("", "mean")],
    "vis":    [("", "median"), ("min", "min"), ("frac_lt1km", "frac_lt:1000")],   # m
    "ceil":   [("", "median"), ("min", "min"), ("frac_lt300m", "frac_lt:300")],   # m
    "tcc":    [("", "mean")],
    "wspd10": [("", "max")],
    "i10fg":  [("", "max")],
    "u10":    [("", "mean")],
    "v10":    [("", "mean")],
    "orog":   [("", "mean")],
    # derived per 2.5 km pixel BEFORE gridding (see derive_pixel_fields)
    "rh2":        [("min", "min")],      # -> rh2_min
    "heat_index": [("max", "max")],      # -> heat_index_max
    "fireweather":[("frac", "mean")],    # -> fireweather_frac (mean of 0/1 flag)
}
DEFAULT_OUTPUTS = [("", "mean")]
STATIC_VARS = {"orog"}

# Fire-weather pixel flag (NWS red-flag style; adjust to local criteria):
# RH <= 15 % AND (sustained wind >= 20 mph OR gust >= 25 mph)
FIRE_RH_MAX = 15.0          # %
FIRE_WSPD_MIN = 8.94        # m/s  (20 mph)
FIRE_GUST_MIN = 11.18       # m/s  (25 mph)


def _out_name(var, suffix):
    return f"{var}_{suffix}" if suffix else var


def list_rtma_files(date, s3):
    """List hourly RTMA 2.5 km analysis files (_wexp) for a given date."""
    prefix = f"rtma2p5.{date.strftime('%Y%m%d')}/"
    paginator = s3.get_paginator("list_objects_v2")
    files = []
    for page in paginator.paginate(Bucket="noaa-rtma-pds", Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key.endswith(".grb2_wexp") and ".2dvaranl_ndfd" in key:
                files.append(key)
    return sorted(files)


def download_rtma(key, s3, local_dir="downloads/rtma"):
    os.makedirs(local_dir, exist_ok=True)
    local_file = os.path.join(local_dir, os.path.basename(key))
    if not os.path.exists(local_file):
        s3.download_file("noaa-rtma-pds", key, local_file)
    return local_file


def load_rtma(file_path):
    """
    Read an RTMA GRIB2 file.
    Returns (fields, lat2d, lon2d); fields = {name: 2D float array, NaN = missing}.
    Plain arrays instead of an xarray Dataset: simpler, and avoids 2D
    coordinates that share their names with dimensions.
    """
    fields, lat2d, lon2d = {}, None, None
    grbs = pygrib.open(file_path)
    try:
        for grb in grbs:
            name = RTMA_VARS.get(grb.shortName)
            if name is None or name in fields:
                continue
            if lat2d is None:
                lat2d, lon2d = grb.latlons()
            # [FIX 1] masked (bitmap) points -> NaN
            fields[name] = np.ma.filled(np.ma.asarray(grb.values, dtype="float64"), np.nan)
    finally:
        grbs.close()
    if not fields:
        raise ValueError(f"No variables found in {file_path} matching RTMA_VARS.")
    return fields, lat2d, lon2d


def earth_relative_uv(wspd, wdir_deg):
    """
    [FIX 7] u/v from speed + meteorological direction ("from", deg).
    Earth-relative, unlike the raw NDFD u/v which are grid-relative.
    """
    rad = np.deg2rad(wdir_deg)
    return -wspd * np.sin(rad), -wspd * np.cos(rad)


def wind_direction(u, v):
    """Meteorological direction (deg, 'from') from earth-relative u/v."""
    return (270.0 - np.rad2deg(np.arctan2(v, u))) % 360.0


def relative_humidity(t_k, td_k):
    """RH (%) from temperature and dew point (K), Magnus formula over water."""
    tc, tdc = t_k - 273.15, td_k - 273.15
    es = np.exp(17.67 * tc / (tc + 243.5))
    e = np.exp(17.67 * tdc / (tdc + 243.5))
    return np.clip(100.0 * e / es, 0.0, 100.0)


def heat_index(t_k, rh):
    """
    NWS heat index (Rothfusz regression with NWS adjustments).
    Input K and %, output K. Equals ~air temperature in cool conditions.
    """
    t = (t_k - 273.15) * 9 / 5 + 32                        # degF
    hi = 0.5 * (t + 61.0 + (t - 68.0) * 1.2 + rh * 0.094)  # simple formula
    use_full = (hi + t) / 2 >= 80
    full = (-42.379 + 2.04901523 * t + 10.14333127 * rh
            - 0.22475541 * t * rh - 6.83783e-3 * t**2 - 5.481717e-2 * rh**2
            + 1.22874e-3 * t**2 * rh + 8.5282e-4 * t * rh**2
            - 1.99e-6 * t**2 * rh**2)
    with np.errstate(invalid="ignore"):
        dry = (rh < 13) & (t >= 80) & (t <= 112)
        full = np.where(dry, full - ((13 - rh) / 4)
                        * np.sqrt(np.clip((17 - np.abs(t - 95)) / 17, 0, None)), full)
        humid = (rh > 85) & (t >= 80) & (t <= 87)
        full = np.where(humid, full + ((rh - 85) / 10) * ((87 - t) / 5), full)
    hi = np.where(use_full, full, hi)
    return (hi - 32) * 5 / 9 + 273.15


def derive_pixel_fields(fields):
    """
    Add fields that must be computed per 2.5 km pixel BEFORE gridding
    (non-linear, or needing conditions to coincide at the same pixel).
    """
    t, td = fields.get("t2m"), fields.get("d2m")
    if t is None or td is None:
        return fields
    rh = relative_humidity(t, td)
    fields["rh2"] = rh
    fields["heat_index"] = heat_index(t, rh)

    ws, gust = fields.get("wspd10"), fields.get("i10fg")
    if ws is not None and gust is not None:
        with np.errstate(invalid="ignore"):
            flag = (rh <= FIRE_RH_MAX) & ((ws >= FIRE_WSPD_MIN) | (gust >= FIRE_GUST_MIN))
        missing = np.isnan(rh) | np.isnan(ws) | np.isnan(gust)
        fields["fireweather"] = np.where(missing, np.nan, flag.astype(float))
    return fields


def grid_rtma_file(fields, lat2d, lon2d, template):
    """Bin one RTMA hour. Returns ({name: arr}, valid_frac)."""
    fields = dict(fields)

    # [FIX 7] replace grid-relative u/v by earth-relative ones; drop raw angle
    if "wspd10" in fields and "wdir10" in fields:
        fields["u10"], fields["v10"] = earth_relative_uv(fields["wspd10"], fields["wdir10"])
    fields.pop("wdir10", None)
    fields = derive_pixel_fields(fields)

    out, valid_frac = {}, None
    for name, values in fields.items():
        for suffix, stat in RTMA_OUTPUTS.get(name, DEFAULT_OUTPUTS):
            arr, vf = aggregate_to_grid(values, lat2d, lon2d, template, stat)
            out[_out_name(name, suffix)] = arr
            if name == "t2m" and not suffix:
                valid_frac = vf

    if "u10" in out and "v10" in out:
        out["wdir10"] = wind_direction(out["u10"], out["v10"])
    return out, valid_frac


def download_and_grid_rtma(day_start, day_end, grid_template, root):
    """Download RTMA files for a date range and bin them onto the template grid."""
    s3 = boto3.client("s3", config=Config(signature_version=UNSIGNED))
    rtma_dir = Path(root) / "rtma" / "raw"
    rtma_dir.mkdir(parents=True, exist_ok=True)
    # Every raw file is deleted right after it is read, so anything left here is
    # from an interrupted run (possibly incomplete): remove it before starting.
    for leftover in rtma_dir.iterdir():
        if leftover.is_file():
            leftover.unlink(missing_ok=True)
    failed = []

    time_index = pd.date_range(pd.Timestamp(day_start).floor("h"),
                               pd.Timestamp(day_end).floor("h"), freq="1h")
    coords = {"y": grid_template.y_centers, "x": grid_template.x_centers}
    hourly, static = [], {}

    current = pd.Timestamp(day_start).normalize()
    while current <= pd.Timestamp(day_end):
        print(f"Processing RTMA {current.date()}...")
        for key in list_rtma_files(current, s3):
            m = re.search(r"t(\d{2})z", key)
            if not m:
                continue
            t = current + pd.Timedelta(hours=int(m.group(1)))
            if t not in time_index:
                continue

            local_file = os.path.join(str(rtma_dir), os.path.basename(key))
            try:
                download_rtma(key, s3, local_dir=str(rtma_dir))
                fields, lat2d, lon2d = load_rtma(local_file)
                out, vf = grid_rtma_file(fields, lat2d, lon2d, grid_template)
            except Exception as e:          # download or read error: skip this hour
                failed.append(key)
                print(f"  Warning: failed on {key}: {e}")
                continue
            finally:
                try:
                    os.remove(local_file)
                except FileNotFoundError:
                    pass
                except OSError as e:
                    print(f"  Failed to delete {local_file}: {e}")

            # [FIX 9] static field stored once
            for name in STATIC_VARS & out.keys():
                static.setdefault(name, out.pop(name))

            data = {n: (("y", "x"), a.astype("float32")) for n, a in out.items()}
            if vf is not None:
                data["rtma_valid_frac"] = (("y", "x"), vf.astype("float32"))
            hourly.append(xr.Dataset(data, coords={**coords, "time": t}))

        current += timedelta(days=1)

    if not hourly:
        raise ValueError("No RTMA data found for requested period.")

    combined = (xr.concat(hourly, dim="time")
                  .sortby("time")
                  .drop_duplicates("time")
                  .reindex(time=time_index))          # [FIX 8] gaps -> NaN
    for name, arr in static.items():
        combined[name] = (("y", "x"), arr.astype("float32"))

    for var, outputs in RTMA_OUTPUTS.items():
        for suffix, stat in outputs:
            name = _out_name(var, suffix)
            if name in combined:
                combined[name].attrs["cell_statistic"] = stat
    if "wdir10" in combined:
        combined["wdir10"].attrs["cell_statistic"] = "derived from cell-mean u10/v10"
    if "rtma_valid_frac" in combined:
        combined["rtma_valid_frac"].attrs["cell_statistic"] = \
            "fraction of 2.5 km pixels with valid t2m"
    for name in combined.data_vars:
        if name == "orog":
            combined[name].attrs["time_window"] = "static"
        else:
            combined[name].attrs["time_window"] = ("analysis valid at HH:00 "
                                                   "(start of the hour window)")
    if "wdir10" in combined:
        combined["u10"].attrs["note"] = "earth-relative, built from wspd10/wdir10"
        combined["v10"].attrs["note"] = "earth-relative, built from wspd10/wdir10"

    combined.attrs["rtma_n_failed_files"] = len(failed)
    combined.attrs["rtma_failed_files"] = "; ".join(failed[:50])
    combined = add_latlon(combined, epsg_proj=grid_template.epsg)
    return combined
