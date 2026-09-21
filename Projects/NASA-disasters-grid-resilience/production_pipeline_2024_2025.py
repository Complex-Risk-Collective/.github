#!/usr/bin/env python3
"""
Full 2024-2025 Production Pipeline for MYRIAD-style Multihazard Identification.

Reuses the exact validated logic from the notebook's "Canonical Production Pipeline"
section (helper functions, hazard-mask construction, event labeling, multihazard
linking), extended to:
  - Run over the full 2024-01-01 to 2025-12-31 window (731 days) instead of the
    one-month pilot.
  - Apply the v2 production exclusions (hail, lightning, drought_proxy dropped;
    known data-quality issues documented in EXCLUDED_HAZARDS).
  - Add ERA5 synoptic wind (extreme_wind_era5) as an additional hazard, reusing the
    already-cached daily-max gust / p99-floored threshold from cell 50.

Progress is logged to console and to a timestamped log file so the run can be
monitored while executing in the background.
"""

from __future__ import annotations

import json
import logging
import re
import sys
import time as _time
import warnings
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd
import xarray as xr
from scipy import ndimage
from scipy.spatial import Delaunay, cKDTree

try:
    import geopandas as gpd
    from shapely.geometry import Point
except Exception:
    gpd = None
    Point = None

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------
ROOT = Path("/Users/ryanmc/Documents/NASA_JPL/Projects/NaturalHazards/NASA ROSES Disasters 2025-2027/data")
LOG_DIR = Path(
    "/Users/ryanmc/Documents/Complex_Risk_Science/dev/Complex-Risk-Collective/.github/Projects/"
    "NASA-disasters-grid-resilience/logs"
)
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / f"production_run_{pd.Timestamp.now():%Y%m%d_%H%M%S}.log"

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)


def step(msg: str) -> None:
    log.info("=" * 70)
    log.info(msg)
    log.info("=" * 70)


step("PRODUCTION PIPELINE START: 2024-01-01 to 2025-12-31 (full period)")

# ---------------------------------------------------------------------------
# Cell 1/5 equivalent: configuration
# ---------------------------------------------------------------------------
WILDFIRE_DIR = ROOT / "wildfire" / "daily"
TERRESTRIAL_DIR = ROOT / "terrestrial_weather"
SPACE_WEATHER_DIR = ROOT / "space_weather" / "NOAA_geoE"
GRID_PATH = ROOT / "analysis_grid_CONUS_50km.nc"
OUTPUT_ROOT = ROOT / "MYRIAD_events_output"

TIME_START = "2024-01-01"
TIME_END = "2025-12-31"

run_start = pd.Timestamp(TIME_START)
run_end = pd.Timestamp(TIME_END)

RUN_LABEL = f"{run_start:%Y%m%d}_{run_end:%Y%m%d}"
OUTPUT_DIR = OUTPUT_ROOT / "production" / f"myriad_conus_{RUN_LABEL}"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

MULTIHAZARD_LAG_HOURS = 48.0
MIN_EVENT_DURATION_DAYS = 1
MIN_WILDFIRE_EVENT_AREA_KM2 = 5.0
ALLOW_SPATIAL_DRIFT_LINKING = True
USE_DASK = False  # eager loading is more memory-predictable than dask at this 731-day scale
CHUNKS = None
DRY_RUN = False

# v2 production exclusions: hail/lightning/drought_proxy dropped for known data-quality reasons.
EXCLUDED_HAZARDS = {
    "hail": "MESH/MAXSIZE unusable: MAXSIZE all-missing, MESH sentinel-contaminated and diluted by 50 km mean binning",
    "lightning": "NLDN density has no positive values in the holdings (max exactly 0.0)",
    "drought_proxy": "15th percentile of an ~87%-zero hourly field is degenerate; produced zero events",
}

HAZARD_RULES = {
    "wildfire": {"mode": "fixed_event_inventory", "min_area_km2": MIN_WILDFIRE_EVENT_AREA_KM2, "min_footprint_cells": 1},
    # heatwave/coldwave (daily-max/min, 3-day rule) dropped: extreme_heat/extreme_cold (hourly,
    # month-conditioned) capture the same phenomena; a multi-day extreme_heat/cold event IS a
    # heatwave/coldwave -- inferred from its duration rather than tracked as a separate hazard.
    "extreme_heat": {"tail": "high", "percentile": 95, "persistence_steps": 3, "threshold_reference": "month", "min_footprint_cells": 20},
    "extreme_cold": {"tail": "low", "percentile": 5, "persistence_steps": 3, "threshold_reference": "month", "min_footprint_cells": 20},
    "extreme_wind": {"tail": "high", "percentile": 93, "persistence_steps": 6, "min_footprint_cells": 10},
    "extreme_precip": {"tail": "high", "percentile": 90, "source_variable": "MultiSensor_QPE_01H_Pass2_00.00", "min_footprint_cells": 20},
    "space_weather_extreme": {"mode": "fixed_operational", "tail": "high", "percentile": 95, "aggregation_minutes": 5, "persistence_steps": 5, "min_footprint_cells": 10},
    "extreme_wind_era5": {"tail": "high", "percentile": 99, "min_footprint_cells": 10},
}

PAIR_LAG_HOURS = {
    ("extreme_wind", "extreme_precip"): 24.0, ("extreme_precip", "extreme_wind"): 24.0,
    ("extreme_wind", "wildfire"): 12.0, ("wildfire", "extreme_wind"): 12.0,
    ("extreme_cold", "extreme_wind"): 48.0, ("extreme_wind", "extreme_cold"): 48.0,
    ("extreme_wind_era5", "extreme_wind"): 24.0, ("extreme_wind", "extreme_wind_era5"): 24.0,
}
SPACE_WEATHER_PAIR_LAG_HOURS = 24.0

# Pairwise PAIR_LAG_HOURS governs individual edges, but connected-components over those edges
# performs unconstrained transitive closure: two physically-unrelated events can still end up in
# the same "episode" via a chain of individually-valid links. Cap how far a single episode's
# start-to-end span can grow so lagged links stay physically meaningful instead of chaining
# across the whole analysis period. 168h = 1 week comfortably covers the largest surviving
# pairwise lag (48h) plus room for genuine multi-day chains (e.g. wind -> precip -> wind).
EPISODE_MAX_SPAN_HOURS = 168.0


def _lag_hours_for_ordered_pair(leading_hazard: str, following_hazard: str) -> float:
    if leading_hazard == "space_weather_extreme" or following_hazard == "space_weather_extreme":
        return SPACE_WEATHER_PAIR_LAG_HOURS
    return PAIR_LAG_HOURS.get((leading_hazard, following_hazard), MULTIHAZARD_LAG_HOURS)


THRESHOLDS = {
    "extreme_heat_pct": HAZARD_RULES["extreme_heat"]["percentile"],
    "extreme_cold_pct": HAZARD_RULES["extreme_cold"]["percentile"],
    "wind_pct": HAZARD_RULES["extreme_wind"]["percentile"],
    "precip_high_pct": HAZARD_RULES["extreme_precip"]["percentile"],
    "wildfire_frp_pct": 90.0,
}
TEMPORAL_MODE = {
    "wildfire": "daily",
    "extreme_heat": "subdaily", "extreme_cold": "subdaily", "extreme_wind": "subdaily",
    "extreme_precip": "subdaily", "space_weather_extreme": "subdaily", "extreme_wind_era5": "daily",
}
MIN_CONSECUTIVE_STEPS = {"extreme_heat": 3, "extreme_cold": 3, "extreme_wind": 6}
MIN_EVENT_FOOTPRINT_CELLS = {hazard: rule["min_footprint_cells"] for hazard, rule in HAZARD_RULES.items()}
MIN_EVENT_ACTIVE_CELL_STEPS = {
    "wildfire": 1, "extreme_heat": 12, "extreme_cold": 12,
    "extreme_wind": 4, "extreme_precip": 4, "space_weather_extreme": 24, "extreme_wind_era5": 1,
}
EVENT_END_GAP_STEPS = {
    "wildfire": 0, "extreme_heat": 1, "extreme_cold": 1,
    "extreme_wind": 1, "extreme_precip": 1, "space_weather_extreme": 2, "extreme_wind_era5": 0,
}
SPACE_WEATHER_OPERATIONAL = HAZARD_RULES["space_weather_extreme"]
SPACE_WEATHER_FILL_ABS_THRESHOLD = 1e20
SPACE_WEATHER_MAX_FALLBACK_DISTANCE_KM = 100.0

log.info(f"Time window: {run_start:%Y-%m-%d} to {run_end:%Y-%m-%d}")
log.info(f"Output directory: {OUTPUT_DIR}")
log.info(f"Hazards in scope: {', '.join(HAZARD_RULES)}")
log.info("Excluded (v2 data-quality issues):")
for name, reason in EXCLUDED_HAZARDS.items():
    log.info(f"  {name}: {reason}")

# ---------------------------------------------------------------------------
# Cell 2/5 equivalent: helper functions (verbatim from validated pilot pipeline)
# ---------------------------------------------------------------------------
DATE_PATTERNS = [
    re.compile(r"(20\d{2})(\d{2})(\d{2})"),
    re.compile(r"(20\d{2})[-_](\d{2})[-_](\d{2})"),
]


def _extract_date_from_name(name: str) -> Optional[pd.Timestamp]:
    for patt in DATE_PATTERNS:
        m = patt.search(name)
        if m:
            y, mo, d = m.groups()
            try:
                return pd.Timestamp(f"{y}-{mo}-{d}")
            except Exception:
                return None
    return None


def list_daily_files(data_dir: Path, glob_pattern: str) -> pd.DataFrame:
    rows = []
    for p in sorted(data_dir.glob(glob_pattern)):
        dt = _extract_date_from_name(p.name)
        if dt is None:
            continue
        rows.append({"time": dt.normalize(), "path": str(p)})
    out = pd.DataFrame(rows)
    if out.empty:
        return out
    out = out[(out["time"] >= pd.Timestamp(TIME_START)) & (out["time"] <= pd.Timestamp(TIME_END))]
    out = out.sort_values("time").drop_duplicates("time", keep="first").reset_index(drop=True)
    return out


def open_daily_stack(df_files: pd.DataFrame, chunks: Optional[dict] = None) -> xr.Dataset:
    if df_files.empty:
        raise FileNotFoundError("No daily files found for requested period.")
    paths = df_files["path"].tolist()
    open_kwargs = dict(combine="nested", concat_dim="time", decode_times=True)
    if chunks and USE_DASK:
        open_kwargs["chunks"] = chunks
    ds = xr.open_mfdataset(paths, **open_kwargs)
    if "time" in ds.coords and ds.sizes.get("time", 0) == len(df_files):
        ds = ds.assign_coords(time=df_files["time"].values)
    return ds


def infer_var(ds: xr.Dataset, candidates: Sequence[str], required: bool = True) -> Optional[str]:
    ds_vars = list(ds.data_vars)
    lower_map = {v.lower(): v for v in ds_vars}
    for c in candidates:
        if c in ds_vars:
            return c
    for c in candidates:
        c_low = c.lower()
        if c_low in lower_map:
            return lower_map[c_low]
    for c in candidates:
        c_low = c.lower()
        for v in ds_vars:
            if c_low in v.lower():
                return v
    if required:
        raise KeyError(f"None of candidate variables found: {candidates}")
    return None


def _rolling_consecutive(mask: xr.DataArray, min_days: int) -> xr.DataArray:
    if min_days <= 1:
        return mask
    rolling_hits = mask.astype(np.int16).rolling(time=min_days, min_periods=min_days).sum()
    core = rolling_hits >= min_days
    expanded = core.copy()
    for k in range(1, min_days):
        expanded = expanded | core.shift(time=k, fill_value=False)
    return expanded.fillna(False)


def _rolling_consecutive_steps(mask: xr.DataArray, min_steps: int) -> xr.DataArray:
    if min_steps <= 1:
        return mask.fillna(False)
    rolling_hits = mask.astype(np.int16).rolling(time=min_steps, min_periods=min_steps).sum()
    core = rolling_hits >= min_steps
    expanded = core.copy()
    for k in range(1, min_steps):
        expanded = expanded | core.shift(time=k, fill_value=False)
    return expanded.fillna(False)


def _quantile_threshold(da: xr.DataArray, q: float) -> xr.DataArray:
    if getattr(da, "chunks", None) is not None and "time" in da.dims:
        da = da.chunk({"time": -1})
    quant = da.quantile(q / 100.0, dim="time", skipna=True)
    if "quantile" in quant.dims:
        quant = quant.squeeze("quantile", drop=True)
    return quant


def _safe_to_numpy(da: xr.DataArray) -> np.ndarray:
    return np.asarray(da.fillna(False).astype(bool).values)


def _decode_time_coord_from_dataarray(da: xr.DataArray, fallback_base_date: str) -> pd.DatetimeIndex:
    if "time" not in da.dims:
        return pd.DatetimeIndex([])
    t_da = da["time"]
    if np.issubdtype(t_da.dtype, np.datetime64):
        return pd.to_datetime(t_da.values, errors="coerce")
    vals = np.asarray(t_da.values)
    if np.issubdtype(vals.dtype, np.number):
        vals = vals.astype(float)
        vals[~np.isfinite(vals)] = np.nan
        huge = np.abs(vals) > 1e20
        if np.any(huge):
            vals[huge] = np.nan
        attrs_lc = {str(k).lower(): v for k, v in t_da.attrs.items()}
        units_attr = str(attrs_lc.get("units", "")).lower()
        reftime_attr = attrs_lc.get("reftime", None)
        if "since" in units_attr:
            unit_map = {
                "seconds": "s", "second": "s", "sec": "s",
                "minutes": "m", "minute": "m", "min": "m",
                "hours": "h", "hour": "h", "hr": "h",
                "days": "D", "day": "D",
                "milliseconds": "ms", "millisecond": "ms", "msec": "ms",
                "microseconds": "us", "microsecond": "us", "usec": "us",
                "nanoseconds": "ns", "nanosecond": "ns", "nsec": "ns",
            }
            parsed_unit = None
            for key, val in unit_map.items():
                if key in units_attr:
                    parsed_unit = val
                    break
            if parsed_unit is not None:
                try:
                    base = units_attr.split("since", 1)[1].strip()
                    origin = pd.Timestamp(base)
                    return pd.to_datetime(vals, unit=parsed_unit, origin=origin, errors="coerce")
                except Exception:
                    pass
        if reftime_attr is not None:
            try:
                base = pd.Timestamp(str(reftime_attr))
                return base + pd.to_timedelta(vals, unit="s")
            except Exception:
                pass
        for unit in ("s", "ms", "us", "ns", "h", "m"):
            try:
                cand = pd.to_datetime(vals, unit=unit, origin="unix", errors="coerce")
                finite = cand[~pd.isna(cand)]
                if len(finite) > 0 and finite.min() >= pd.Timestamp("2000-01-01") and finite.max() <= pd.Timestamp("2100-01-01"):
                    return cand
            except Exception:
                continue
        finite_vals = vals[np.isfinite(vals)]
        if finite_vals.size > 0 and np.nanmin(finite_vals) >= 0 and np.nanmax(finite_vals) <= 172800:
            base = pd.Timestamp(fallback_base_date).normalize()
            return base + pd.to_timedelta(vals, unit="s")
    return pd.to_datetime(vals, errors="coerce")


def _safe_daily_time_coord(da: xr.DataArray) -> xr.DataArray:
    if "time" not in da.dims:
        return da
    t = _decode_time_coord_from_dataarray(da, fallback_base_date=TIME_START)
    valid = ~pd.isna(t)
    if not np.all(valid):
        da = da.isel(time=np.where(valid)[0])
        t = t[valid]
    da = da.assign_coords(time=pd.DatetimeIndex(t).normalize())
    if da.sizes.get("time", 0) > 0 and da.indexes["time"].has_duplicates:
        da = da.groupby("time").max(skipna=True)
    return da


def _safe_subdaily_time_coord(da: xr.DataArray) -> xr.DataArray:
    if "time" not in da.dims:
        return da
    t = _decode_time_coord_from_dataarray(da, fallback_base_date=TIME_START)
    valid = ~pd.isna(t)
    if not np.all(valid):
        da = da.isel(time=np.where(valid)[0])
        t = t[valid]
    t = pd.DatetimeIndex(t)
    if len(t) >= 100 and (t.max().normalize() - t.min().normalize()).days <= 2:
        resets = np.where(np.asarray(t[1:] < t[:-1]))[0] + 1
        if len(resets) > 0:
            seg_starts = np.r_[0, resets]
            seg_ends = np.r_[resets, len(t)]
            day0 = t[0].normalize()
            repaired = np.array(t.values, dtype="datetime64[ns]")
            for seg_i, (s0, s1) in enumerate(zip(seg_starts, seg_ends)):
                block = pd.DatetimeIndex(t[s0:s1])
                offsets = block - block.normalize()
                repaired[s0:s1] = (day0 + pd.to_timedelta(seg_i, unit="D") + offsets).values
            t = pd.DatetimeIndex(repaired)
    da = da.assign_coords(time=t)
    if da.sizes.get("time", 0) > 0 and da.indexes["time"].has_duplicates:
        da = da.groupby("time").max(skipna=True)
    return da


def _prepare_hazard_time(da: xr.DataArray, hazard_name: str) -> xr.DataArray:
    mode = TEMPORAL_MODE.get(hazard_name, "daily")
    if mode == "subdaily":
        return _safe_subdaily_time_coord(da)
    return _safe_daily_time_coord(da)


def _space_weather_fill_value_candidates(da: xr.DataArray) -> List[float]:
    candidates: List[float] = []
    for mapping in (getattr(da, "attrs", {}), getattr(da, "encoding", {})):
        for key in ("_FillValue", "missing_value"):
            if key not in mapping:
                continue
            value = mapping[key]
            values = np.ravel(value) if np.ndim(value) else [value]
            for item in values:
                try:
                    number = float(item)
                except (TypeError, ValueError):
                    continue
                if np.isfinite(number):
                    candidates.append(number)
    return candidates


def _mask_space_weather_fill_values(values: np.ndarray, da: Optional[xr.DataArray] = None) -> np.ndarray:
    arr = np.asarray(values, dtype=np.float32).copy()
    invalid = ~np.isfinite(arr) | (np.abs(arr) > SPACE_WEATHER_FILL_ABS_THRESHOLD)
    if da is not None:
        for fill_value in _space_weather_fill_value_candidates(da):
            if abs(fill_value) <= SPACE_WEATHER_FILL_ABS_THRESHOLD:
                invalid |= arr == np.float32(fill_value)
    arr[invalid] = np.nan
    return arr


def _approx_latlon_distance_km(lat1: np.ndarray, lon1: np.ndarray, lat2: np.ndarray, lon2: np.ndarray) -> np.ndarray:
    mean_lat_rad = np.deg2rad((lat1 + lat2) / 2.0)
    dy_km = (lat1 - lat2) * 111.32
    dx_km = (lon1 - lon2) * 111.32 * np.cos(mean_lat_rad)
    return np.hypot(dx_km, dy_km)


def _space_weather_site_coordinates(ds_space: xr.Dataset) -> Tuple[str, np.ndarray, np.ndarray, np.ndarray]:
    lat_name = "latitude" if ("latitude" in ds_space.coords or "latitude" in ds_space.data_vars) else "lat"
    lon_name = "longitude" if ("longitude" in ds_space.coords or "longitude" in ds_space.data_vars) else "lon"
    if lat_name in ds_space.data_vars:
        ds_space = ds_space.assign_coords({lat_name: ds_space[lat_name]})
    if lon_name in ds_space.data_vars:
        ds_space = ds_space.assign_coords({lon_name: ds_space[lon_name]})
    if lat_name not in ds_space.coords or lon_name not in ds_space.coords:
        raise ValueError("Space-weather dataset must include latitude/longitude as coords or data variables.")

    lat_da = ds_space[lat_name]
    lon_da = ds_space[lon_name]
    if len(lat_da.dims) == 1 and len(lon_da.dims) == 1 and lat_da.dims[0] == lon_da.dims[0]:
        site_dim = lat_da.dims[0]
        src_lat = _mask_space_weather_fill_values(lat_da.values, lat_da).astype(float)
        src_lon = _mask_space_weather_fill_values(lon_da.values, lon_da).astype(float)
    else:
        if "time" not in lat_da.dims or "time" not in lon_da.dims:
            raise ValueError("Space-weather latitude/longitude must be 1D site or 2D (time,site).")
        other_lat = [d for d in lat_da.dims if d != "time"]
        other_lon = [d for d in lon_da.dims if d != "time"]
        if len(other_lat) != 1 or len(other_lon) != 1 or other_lat[0] != other_lon[0]:
            raise ValueError("Could not infer space-weather site dimension from latitude/longitude.")
        site_dim = other_lat[0]
        lat_2d = _mask_space_weather_fill_values(lat_da.transpose("time", site_dim).values, lat_da).astype(float)
        lon_2d = _mask_space_weather_fill_values(lon_da.transpose("time", site_dim).values, lon_da).astype(float)
        src_lat = np.nanmedian(lat_2d, axis=0)
        src_lon = np.nanmedian(lon_2d, axis=0)
        bad = ~np.isfinite(src_lat) | ~np.isfinite(src_lon)
        if np.any(bad):
            valid_matrix = np.isfinite(lat_2d) & np.isfinite(lon_2d)
            for site_index in np.where(bad)[0]:
                rows = np.where(valid_matrix[:, site_index])[0]
                if len(rows) > 0:
                    row_index = rows[0]
                    src_lat[site_index] = lat_2d[row_index, site_index]
                    src_lon[site_index] = lon_2d[row_index, site_index]

    keep_sites = np.isfinite(src_lat) & np.isfinite(src_lon)
    if not keep_sites.any():
        raise ValueError("No finite space-weather site coordinates were available for gridding.")
    return site_dim, src_lat[keep_sites], src_lon[keep_sites], keep_sites


def _build_space_weather_interpolation(src_lat: np.ndarray, src_lon: np.ndarray, ds_target_grid: xr.Dataset) -> dict:
    if "lat" not in ds_target_grid or "lon" not in ds_target_grid:
        raise ValueError("Target analysis grid must contain 2D lat/lon variables.")
    target_lat = ds_target_grid["lat"].values.astype(float).ravel()
    target_lon = ds_target_grid["lon"].values.astype(float).ravel()
    target_valid = np.isfinite(target_lat) & np.isfinite(target_lon)
    source_points = np.column_stack([src_lon, src_lat])
    target_points = np.column_stack([target_lon, target_lat])

    nearest_source = cKDTree(source_points).query(target_points, k=1)[1].astype(np.int64)
    nearest_distance_km = _approx_latlon_distance_km(
        target_lat,
        target_lon,
        src_lat[nearest_source],
        src_lon[nearest_source],
    ).astype(np.float32)
    nearest_fallback_supported = target_valid & (nearest_distance_km < SPACE_WEATHER_MAX_FALLBACK_DISTANCE_KM)
    simplex = np.full(target_points.shape[0], -1, dtype=np.int64)
    vertices = np.full((target_points.shape[0], 3), -1, dtype=np.int64)
    weights = np.full((target_points.shape[0], 3), np.nan, dtype=np.float32)
    if source_points.shape[0] >= 3:
        triangulation = Delaunay(source_points)
        valid_target_indices = np.where(target_valid)[0]
        simplex_valid = triangulation.find_simplex(target_points[valid_target_indices])
        inside_target_indices = valid_target_indices[simplex_valid >= 0]
        inside_simplex = simplex_valid[simplex_valid >= 0]
        if inside_target_indices.size:
            transform = triangulation.transform[inside_simplex]
            delta = target_points[inside_target_indices] - transform[:, 2]
            first_weights = np.einsum("ijk,ik->ij", transform[:, :2, :], delta)
            barycentric = np.column_stack([first_weights, 1.0 - first_weights.sum(axis=1)]).astype(np.float32)
            simplex[inside_target_indices] = inside_simplex
            vertices[inside_target_indices] = triangulation.simplices[inside_simplex]
            weights[inside_target_indices] = barycentric
    return {
        "target_valid": target_valid,
        "nearest_source": nearest_source,
        "nearest_source_distance_km": nearest_distance_km,
        "nearest_fallback_supported": nearest_fallback_supported,
        "inside": simplex >= 0,
        "vertices": vertices,
        "weights": weights,
        "shape": (int(ds_target_grid.sizes["y"]), int(ds_target_grid.sizes["x"])),
    }


def _interpolate_space_weather_rows(values: np.ndarray, interpolation: dict) -> np.ndarray:
    arr = _mask_space_weather_fill_values(values)
    nt = arr.shape[0]
    ncells = interpolation["target_valid"].size
    out = np.full((nt, ncells), np.nan, dtype=np.float32)
    inside_indices = np.where(interpolation["inside"])[0]
    vertices = interpolation["vertices"][inside_indices]
    weights = interpolation["weights"][inside_indices]
    nearest_source = interpolation["nearest_source"]
    nearest_fallback_supported = interpolation["nearest_fallback_supported"]
    for time_index in range(nt):
        row = arr[time_index]
        gridded = np.full(ncells, np.nan, dtype=np.float32)
        if np.isfinite(row).any():
            if inside_indices.size:
                vertex_values = row[vertices]
                valid_vertices = np.isfinite(vertex_values).all(axis=1)
                if np.any(valid_vertices):
                    gridded[inside_indices[valid_vertices]] = np.sum(
                        vertex_values[valid_vertices] * weights[valid_vertices], axis=1,
                    ).astype(np.float32)
            fallback = np.isnan(gridded) & nearest_fallback_supported & np.isfinite(row[nearest_source])
            gridded[fallback] = row[nearest_source[fallback]]
        out[time_index] = gridded
    return out


def _grid_space_weather_to_analysis_grid(ds_space_raw: xr.Dataset, ds_target_grid: xr.Dataset) -> xr.Dataset:
    if ds_space_raw is None:
        raise ValueError("ds_space_raw is None")
    ds_space = ds_space_raw.copy()
    if "time" in ds_space.coords:
        fake_da = xr.DataArray(np.zeros(ds_space.sizes["time"]), dims=("time",), coords={"time": ds_space["time"]})
        t = _decode_time_coord_from_dataarray(fake_da, fallback_base_date=TIME_START)
        valid = ~pd.isna(t)
        if not np.all(valid):
            ds_space = ds_space.isel(time=np.where(valid)[0])
            t = t[valid]
        ds_space = ds_space.assign_coords(time=pd.DatetimeIndex(t))

    site_dim, src_lat, src_lon, keep_sites = _space_weather_site_coordinates(ds_space)
    interpolation = _build_space_weather_interpolation(src_lat, src_lon, ds_target_grid)
    ny, nx = interpolation["shape"]

    out_vars = {}
    for var_name, da in ds_space.data_vars.items():
        if "time" not in da.dims or site_dim not in da.dims:
            continue
        arr = da.transpose("time", site_dim).values
        if arr.shape[1] == keep_sites.size:
            arr = arr[:, keep_sites]
        arr = _mask_space_weather_fill_values(arr, da)
        gridded = _interpolate_space_weather_rows(arr, interpolation)
        out_vars[var_name] = (("time", "y", "x"), gridded.reshape(arr.shape[0], ny, nx))

    ds_out = xr.Dataset(
        data_vars=out_vars,
        coords={
            "time": ds_space["time"].values,
            "y": ds_target_grid["y"].values,
            "x": ds_target_grid["x"].values,
            "lat": (("y", "x"), ds_target_grid["lat"].values),
            "lon": (("y", "x"), ds_target_grid["lon"].values),
        },
        attrs={
            "gridding_method": "linear_delaunay_interpolation_with_nearest_fallback",
            "source_grid": "NOAA modeled geoE unstructured points",
            "fill_value_policy": f"masked nonfinite values and abs(value) > {SPACE_WEATHER_FILL_ABS_THRESHOLD:.1e}",
            "fallback_policy": f"nearest fallback only where nearest source point is < {SPACE_WEATHER_MAX_FALLBACK_DISTANCE_KM:.0f} km away; farther cells are NaN",
        },
    )
    ds_out["space_weather_linear_interpolation_support"] = (
        ("y", "x"), interpolation["inside"].reshape(ny, nx).astype(np.uint8),
    )
    ds_out["space_weather_nearest_fallback_support"] = (
        ("y", "x"), interpolation["nearest_fallback_supported"].reshape(ny, nx).astype(np.uint8),
    )
    ds_out["space_weather_nearest_source_distance_km"] = (
        ("y", "x"), interpolation["nearest_source_distance_km"].reshape(ny, nx).astype(np.float32),
    )
    ds_out["space_weather_nearest_source_index"] = (
        ("y", "x"), interpolation["nearest_source"].reshape(ny, nx).astype(np.int32),
    )
    return ds_out


def _event_connectivity_structure(allow_spatial_drift: bool = True) -> np.ndarray:
    s = np.zeros((3, 3, 3), dtype=bool)
    s[1, 1, 1] = True
    s[1, 0, 1] = True
    s[1, 2, 1] = True
    s[1, 1, 0] = True
    s[1, 1, 2] = True
    s[0, 1, 1] = True
    s[2, 1, 1] = True
    if allow_spatial_drift:
        for t in (0, 2):
            s[t, 0, 1] = True
            s[t, 2, 1] = True
            s[t, 1, 0] = True
            s[t, 1, 2] = True
    return s


def _bridge_short_time_gaps(arr: np.ndarray, max_gap_steps: int) -> np.ndarray:
    if max_gap_steps <= 0:
        return arr
    kernel = np.ones((int(max_gap_steps) + 1, 1, 1), dtype=bool)
    return ndimage.binary_closing(arr.astype(bool), structure=kernel)


def label_single_hazard_events(mask_da: xr.DataArray, hazard_name: str) -> Tuple[pd.DataFrame, np.ndarray]:
    arr = _safe_to_numpy(mask_da)
    if arr.ndim != 3:
        raise ValueError(f"Expected [time, y, x] for {hazard_name}, got shape {arr.shape}")

    gap_steps = int(EVENT_END_GAP_STEPS.get(hazard_name, 0))
    arr = _bridge_short_time_gaps(arr, gap_steps)

    structure = _event_connectivity_structure(bool(ALLOW_SPATIAL_DRIFT_LINKING))
    labels, n_labels = ndimage.label(arr, structure=structure)

    times = pd.to_datetime(mask_da["time"].values)
    if len(times) >= 2:
        dt_hours = float(np.nanmedian(np.diff(times) / np.timedelta64(1, "h")))
        if not np.isfinite(dt_hours) or dt_hours <= 0:
            dt_hours = 24.0
    else:
        dt_hours = 24.0

    min_cells = int(MIN_EVENT_FOOTPRINT_CELLS.get(hazard_name, 1))
    min_active_steps = int(MIN_EVENT_ACTIVE_CELL_STEPS.get(hazard_name, 1))

    empty_cols = [
        "event_id", "hazard", "start_time", "end_time", "duration_hours", "duration_days",
        "time_step_hours", "active_cell_steps", "footprint_cells", "footprint_km2",
    ]
    if n_labels == 0:
        return pd.DataFrame(columns=empty_cols), labels

    ti, yi, xi = np.where(labels > 0)
    if len(ti) == 0:
        return pd.DataFrame(columns=empty_cols), labels

    lbl = labels[ti, yi, xi].astype(np.int64)
    ny, nx = labels.shape[1], labels.shape[2]
    ncells = int(ny * nx)

    active_cell_steps_counts = np.bincount(lbl, minlength=n_labels + 1)
    tmin = np.full(n_labels + 1, np.iinfo(np.int64).max, dtype=np.int64)
    tmax = np.full(n_labels + 1, -1, dtype=np.int64)
    ti64 = ti.astype(np.int64)
    np.minimum.at(tmin, lbl, ti64)
    np.maximum.at(tmax, lbl, ti64)

    flat_cell = yi.astype(np.int64) * nx + xi.astype(np.int64)
    keys = lbl * ncells + flat_cell
    ukeys = np.unique(keys)
    u_lbl = (ukeys // ncells).astype(np.int64)
    footprint_counts = np.bincount(u_lbl, minlength=n_labels + 1)

    records: List[dict] = []
    for event_idx in range(1, n_labels + 1):
        active_cell_steps = int(active_cell_steps_counts[event_idx])
        if active_cell_steps == 0 or active_cell_steps < min_active_steps:
            continue
        footprint_cells = int(footprint_counts[event_idx])
        if footprint_cells < min_cells:
            continue
        start_i = int(tmin[event_idx])
        end_i = int(tmax[event_idx])
        if end_i < start_i:
            continue
        start_t = pd.Timestamp(times[start_i])
        end_t = pd.Timestamp(times[end_i])
        duration_hours = float((end_t - start_t) / pd.Timedelta(hours=1) + dt_hours)
        duration_days = duration_hours / 24.0
        if TEMPORAL_MODE.get(hazard_name, "daily") == "daily" and duration_days < float(MIN_EVENT_DURATION_DAYS):
            continue
        footprint_km2 = footprint_cells * 2500.0
        records.append({
            "event_id": f"{hazard_name}_{event_idx:06d}",
            "hazard": hazard_name,
            "start_time": start_t,
            "end_time": end_t,
            "duration_hours": duration_hours,
            "duration_days": duration_days,
            "time_step_hours": dt_hours,
            "active_cell_steps": active_cell_steps,
            "footprint_cells": footprint_cells,
            "footprint_km2": footprint_km2,
        })

    return pd.DataFrame(records), labels


def _build_event_cache(tbl: pd.DataFrame, labels: np.ndarray) -> Dict[str, dict]:
    # Group all (y, x) cells by label in a single pass instead of scanning the full label
    # array once per event (np.where(labels == enum) repeated thousands of times is O(n_events
    # * array_size) and becomes prohibitively slow at full-period scale).
    cache: Dict[str, dict] = {}
    if tbl.empty:
        return cache
    _, yy_all, xx_all = np.where(labels > 0)
    if len(yy_all) == 0:
        return cache
    lbl_all = labels[labels > 0].astype(np.int64)
    order = np.argsort(lbl_all, kind="stable")
    lbl_sorted = lbl_all[order]
    yy_sorted = yy_all[order]
    xx_sorted = xx_all[order]
    unique_labels, boundaries = np.unique(lbl_sorted, return_index=True)
    bounds = np.r_[boundaries, len(lbl_sorted)]
    cells_by_label: Dict[int, dict] = {}
    for i, enum in enumerate(unique_labels):
        y_seg = yy_sorted[bounds[i]:bounds[i + 1]]
        x_seg = xx_sorted[bounds[i]:bounds[i + 1]]
        cells_by_label[int(enum)] = {
            "cells": set(zip(y_seg.tolist(), x_seg.tolist())),
            "y_min": int(y_seg.min()), "y_max": int(y_seg.max()),
            "x_min": int(x_seg.min()), "x_max": int(x_seg.max()),
        }

    for _, row in tbl.iterrows():
        eid = str(row["event_id"])
        enum = int(eid.split("_")[-1])
        info = cells_by_label.get(enum)
        if info is None:
            continue
        cache[eid] = {
            "start": pd.Timestamp(row["start_time"]),
            "end": pd.Timestamp(row["end_time"]),
            "cells": info["cells"],
            "y_min": info["y_min"], "y_max": info["y_max"],
            "x_min": info["x_min"], "x_max": info["x_max"],
        }
    return cache


def _events_are_linked_cached(a: dict, b: dict, hazard_a: str, hazard_b: str) -> bool:
    if a["start"] <= b["end"] and b["start"] <= a["end"]:
        gap_hours = 0.0
        lag_hours = _lag_hours_for_ordered_pair(hazard_a, hazard_b)
    elif b["start"] > a["end"]:
        gap_hours = float((b["start"] - a["end"]) / pd.Timedelta(hours=1))
        lag_hours = _lag_hours_for_ordered_pair(hazard_a, hazard_b)
    else:
        gap_hours = float((a["start"] - b["end"]) / pd.Timedelta(hours=1))
        lag_hours = _lag_hours_for_ordered_pair(hazard_b, hazard_a)
    if gap_hours > lag_hours:
        return False
    if a["y_max"] < (b["y_min"] - 1) or b["y_max"] < (a["y_min"] - 1):
        return False
    if a["x_max"] < (b["x_min"] - 1) or b["x_max"] < (a["x_min"] - 1):
        return False
    a_cells, b_cells = a["cells"], b["cells"]
    if a_cells & b_cells:
        return True
    src, tgt = (a_cells, b_cells) if len(a_cells) <= len(b_cells) else (b_cells, a_cells)
    for y, x in src:
        if (y - 1, x) in tgt or (y + 1, x) in tgt or (y, x - 1) in tgt or (y, x + 1) in tgt:
            return True
    return False


step("Step 1/6: Building file manifests")
t_step = _time.perf_counter()
wildfire_files = list_daily_files(WILDFIRE_DIR, "fires_analysis_grid_*.nc")
terr_files = list_daily_files(TERRESTRIAL_DIR, "weather_CONUS_*.nc")
space_files = list_daily_files(SPACE_WEATHER_DIR, "*-empirical-EMTF-2022.12-2022.12.nc")
log.info(f"  wildfire files: {len(wildfire_files)}")
log.info(f"  terrestrial weather files: {len(terr_files)}")
log.info(f"  space weather files: {len(space_files)}")
log.info(f"Step 1/6 complete in {_time.perf_counter() - t_step:.0f}s")

# ---------------------------------------------------------------------------
# Cell 3/5 equivalent: hazard mask construction
# ---------------------------------------------------------------------------
step("Step 2/6: Loading terrestrial + space weather stacks and building hazard masks")
t_step = _time.perf_counter()

ds_terr = open_daily_stack(terr_files, chunks=CHUNKS)
ds_target_grid = xr.open_dataset(GRID_PATH)

# Site lat/lon are static across all space-weather files (same gridpt layout every day), so the
# site->analysis-cell mapping is computed once from a single file. Ex/Ey are then gridded
# per-file in a streaming loop instead of materializing the full multi-file (n_timesteps, n_sites)
# stack at once, which at 677 files x 1440 min/day x 3432 sites is a ~13 GB-per-variable blowup.
with xr.open_dataset(space_files["path"].iloc[0], decode_times=False) as _ds0:
    _site_dim, _site_lat, _site_lon, _site_keep = _space_weather_site_coordinates(_ds0)

_sw_ny, _sw_nx = ds_target_grid.sizes["y"], ds_target_grid.sizes["x"]
_sw_ncells = _sw_ny * _sw_nx
_sw_interpolation = _build_space_weather_interpolation(_site_lat, _site_lon, ds_target_grid)

_sw_gridded = {"Ex": [], "Ey": []}
_sw_times = []
_sw_agg_minutes = int(SPACE_WEATHER_OPERATIONAL["aggregation_minutes"])
for _path in space_files["path"]:
    with xr.open_dataset(_path, decode_times=False) as _dsf:
        _fake_da = xr.DataArray(np.zeros(_dsf.sizes["time"]), dims=("time",), coords={"time": _dsf["time"]})
        _t = _decode_time_coord_from_dataarray(_fake_da, fallback_base_date=TIME_START)
        _valid = np.where(~pd.isna(_t))[0]
        _t_valid = pd.DatetimeIndex(_t[_valid])

        # Reduce native (typically 1-minute) cadence to the operational aggregation window via
        # max BEFORE gridding, so the expensive per-timestep gridding loop runs far fewer times
        # and the retained arrays are ~aggregation_minutes-x smaller for the whole run.
        if _sw_agg_minutes > 1 and len(_t_valid) > 1:
            _t_agg = _t_valid.floor(f"{_sw_agg_minutes}min")
            _agg_groups, _group_starts = np.unique(_t_agg.values, return_index=True)
            _t_out = pd.DatetimeIndex(_agg_groups)
        else:
            _group_starts = None
            _t_out = _t_valid

        for _var in ("Ex", "Ey"):
            _arr_full = _dsf[_var].transpose("time", _site_dim).values[_valid]
            if _arr_full.shape[1] == _site_keep.size:
                _arr_full = _arr_full[:, _site_keep]
            _arr_full = _mask_space_weather_fill_values(_arr_full, _dsf[_var])
            if _group_starts is not None:
                _bounds = np.r_[_group_starts, len(_arr_full)]
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", category=RuntimeWarning)
                    _arr = np.stack([
                        np.nanmax(_arr_full[_bounds[i]:_bounds[i + 1]], axis=0) for i in range(len(_group_starts))
                    ])
            else:
                _arr = _arr_full
            del _arr_full
            _out = _interpolate_space_weather_rows(_arr, _sw_interpolation)
            _sw_gridded[_var].append(_out)
        _sw_times.append(_t_out)

ds_space = xr.Dataset(
    {
        "Ex": (("time", "y", "x"), np.concatenate(_sw_gridded["Ex"], axis=0).reshape(-1, _sw_ny, _sw_nx)),
        "Ey": (("time", "y", "x"), np.concatenate(_sw_gridded["Ey"], axis=0).reshape(-1, _sw_ny, _sw_nx)),
        "space_weather_linear_interpolation_support": (
            ("y", "x"), _sw_interpolation["inside"].reshape(_sw_ny, _sw_nx).astype(np.uint8),
        ),
        "space_weather_nearest_fallback_support": (
            ("y", "x"), _sw_interpolation["nearest_fallback_supported"].reshape(_sw_ny, _sw_nx).astype(np.uint8),
        ),
        "space_weather_nearest_source_distance_km": (
            ("y", "x"), _sw_interpolation["nearest_source_distance_km"].reshape(_sw_ny, _sw_nx).astype(np.float32),
        ),
        "space_weather_nearest_source_index": (
            ("y", "x"), _sw_interpolation["nearest_source"].reshape(_sw_ny, _sw_nx).astype(np.int32),
        ),
    },
    coords={
        "time": pd.DatetimeIndex(np.concatenate([t.values for t in _sw_times])),
        "y": ds_target_grid["y"].values,
        "x": ds_target_grid["x"].values,
        "lat": (("y", "x"), ds_target_grid["lat"].values),
        "lon": (("y", "x"), ds_target_grid["lon"].values),
    },
    attrs={
        "gridding_method": "linear_delaunay_interpolation_with_nearest_fallback_streaming",
        "source_grid": "NOAA modeled geoE unstructured points",
        "fill_value_policy": f"masked nonfinite values and abs(value) > {SPACE_WEATHER_FILL_ABS_THRESHOLD:.1e}",
        "fallback_policy": f"nearest fallback only where nearest source point is < {SPACE_WEATHER_MAX_FALLBACK_DISTANCE_KM:.0f} km away; farther cells are NaN",
    },
)
del _sw_gridded, _sw_times
_sw_support = int(_sw_interpolation["inside"].sum())
_sw_fallback_support = int(_sw_interpolation["nearest_fallback_supported"].sum())
log.info(
    f"  Space weather gridded via streaming interpolation loop: {ds_space.sizes['time']} timesteps; "
    f"linear support on {_sw_support}/{_sw_ncells} cells; "
    f"nearest fallback allowed on {_sw_fallback_support}/{_sw_ncells} cells"
)


common_time = pd.date_range(TIME_START, TIME_END, freq="D")
ny, nx = int(ds_target_grid.sizes["y"]), int(ds_target_grid.sizes["x"])

# --- Wildfire: GeoPackage event polygons preferred ---
wildfire_events_gpkg = ROOT / "wildfire" / "fire_events.gpkg"
wildfire_firms_gpkg = ROOT / "wildfire" / "firms_event_matches.gpkg"
wildfire_source = "unknown"
use_gpkg_events = bool(gpd is not None and wildfire_events_gpkg.exists())

if use_gpkg_events:
    events_gdf = gpd.read_file(wildfire_events_gpkg, layer="events")
    events_gdf = events_gdf.set_crs("EPSG:4326") if events_gdf.crs is None else events_gdf.to_crs("EPSG:4326")
    events_gdf["start_date"] = pd.to_datetime(events_gdf["start_date"], errors="coerce")
    events_gdf["end_date"] = pd.to_datetime(events_gdf["end_date"], errors="coerce")
    events_gdf["final_area_km2"] = pd.to_numeric(events_gdf["final_area_km2"], errors="coerce")
    events_gdf = events_gdf[events_gdf["final_area_km2"] >= float(MIN_WILDFIRE_EVENT_AREA_KM2)].copy()
    events_gdf = events_gdf[
        (events_gdf["end_date"] >= run_start) & (events_gdf["start_date"] <= run_end)
        & events_gdf["start_date"].notna() & events_gdf["end_date"].notna()
    ].copy()

    if wildfire_firms_gpkg.exists():
        firms_gdf = gpd.read_file(wildfire_firms_gpkg, layer="firms_matches")
        firms_gdf["frp"] = pd.to_numeric(firms_gdf["frp"], errors="coerce")
        frp_summary = firms_gdf.groupby("event_id", dropna=True).agg(
            n_firms_pts=("frp", "size"), frp_sum=("frp", "sum"), frp_mean=("frp", "mean"), frp_max=("frp", "max"),
        ).reset_index()
        events_gdf = events_gdf.merge(frp_summary, on="event_id", how="left")

    flat_lat = ds_target_grid["lat"].values.ravel()
    flat_lon = ds_target_grid["lon"].values.ravel()
    grid_points = gpd.GeoDataFrame(
        {"flat_idx": np.arange(ny * nx, dtype=np.int64), "lat": flat_lat, "lon": flat_lon},
        geometry=gpd.points_from_xy(flat_lon, flat_lat), crs="EPSG:4326",
    )
    grid_tree = cKDTree(np.column_stack([flat_lat.astype(float), flat_lon.astype(float)]))
    day_to_idx = {pd.Timestamp(d): i for i, d in enumerate(common_time)}
    wildfire_mask_arr = np.zeros((len(common_time), ny, nx), dtype=bool)
    wildfire_value_arr = np.full((len(common_time), ny, nx), np.nan, dtype=np.float32)

    for _, ev in events_gdf.iterrows():
        geom = ev.geometry
        if geom is None or geom.is_empty:
            continue
        inside = grid_points.intersects(geom)
        flat_ids = grid_points.loc[inside, "flat_idx"].values.astype(np.int64)
        centroid = geom.centroid
        if np.isfinite(centroid.y) and np.isfinite(centroid.x):
            _, nearest_flat = grid_tree.query([float(centroid.y), float(centroid.x)], k=1)
            flat_ids = np.unique(np.concatenate([flat_ids, np.asarray([nearest_flat], dtype=np.int64)]))
        if flat_ids.size == 0:
            continue
        ev_start = max(pd.Timestamp(ev["start_date"]).normalize(), run_start)
        ev_end = min(pd.Timestamp(ev["end_date"]).normalize(), run_end)
        if ev_end < ev_start:
            continue
        ev_days = pd.date_range(ev_start, ev_end, freq="D")
        time_ids = [day_to_idx[d] for d in ev_days if d in day_to_idx]
        if len(time_ids) == 0:
            continue
        score_val = ev.get("frp_mean", np.nan)
        if not np.isfinite(score_val):
            score_val = ev.get("final_area_km2", np.nan)
        score_val = np.float32(score_val) if np.isfinite(score_val) else np.float32(1.0)
        for ti in time_ids:
            sl = wildfire_mask_arr[ti].reshape(-1)
            sl[flat_ids] = True
            vl = wildfire_value_arr[ti].reshape(-1)
            prev = vl[flat_ids]
            vl[flat_ids] = np.where(np.isnan(prev), score_val, np.maximum(prev, score_val))

    wildfire_mask = xr.DataArray(
        wildfire_mask_arr,
        coords={"time": common_time, "y": ds_target_grid["y"].values, "x": ds_target_grid["x"].values},
        dims=("time", "y", "x"),
    ).fillna(False)
    wildfire_source = "gpkg_events"
else:
    ds_wild = open_daily_stack(wildfire_files, chunks=CHUNKS)
    wildfire_var = infer_var(ds_wild, ["fire_mask", "burned_area", "fire_area_km2", "active_fire", "frp"])
    wild_da = _prepare_hazard_time(ds_wild[wildfire_var], "wildfire")
    wildfire_mask = (wild_da >= MIN_WILDFIRE_EVENT_AREA_KM2).fillna(False)
    wildfire_source = "daily_grid_fallback"

log.info(f"  wildfire_source={wildfire_source}")

hazard_masks: Dict[str, xr.DataArray] = {"wildfire": wildfire_mask}
# Raw (pre-threshold) source field per hazard, aligned to the same time axis as its mask.
# Used to compute per-event peak/mean intensity after labeling.
hazard_source_values: Dict[str, np.ndarray] = {}
if wildfire_source == "gpkg_events":
    hazard_source_values["wildfire"] = wildfire_value_arr

# --- Temperature-based hazards: extreme_heat/extreme_cold only. A multi-day extreme_heat/cold
# event is functionally a heatwave/coldwave -- inferred from its duration, not tracked separately.
temp_var = infer_var(ds_terr, ["T2M", "TMP2m", "temperature", "air_temperature", "tmean"], required=False)
if temp_var is not None:
    temp_hourly = _safe_subdaily_time_coord(ds_terr[temp_var])
    temp_values = np.asarray(temp_hourly.values, dtype=np.float32)
    temp_months = pd.DatetimeIndex(pd.to_datetime(temp_hourly["time"].values)).month.to_numpy()
    eh_thr_by_month = np.full((12,) + temp_values.shape[1:], np.nan, dtype=np.float64)
    ec_thr_by_month = np.full((12,) + temp_values.shape[1:], np.nan, dtype=np.float64)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=RuntimeWarning)
        for m in range(1, 13):
            sel = temp_values[temp_months == m]
            if sel.shape[0] == 0:
                continue
            eh_thr_by_month[m - 1] = np.nanpercentile(sel, THRESHOLDS["extreme_heat_pct"], axis=0)
            ec_thr_by_month[m - 1] = np.nanpercentile(sel, THRESHOLDS["extreme_cold_pct"], axis=0)
    eh_thr_arr = eh_thr_by_month[temp_months - 1]
    ec_thr_arr = ec_thr_by_month[temp_months - 1]
    extreme_heat_raw = xr.DataArray(temp_values > eh_thr_arr, dims=temp_hourly.dims, coords=temp_hourly.coords)
    extreme_cold_raw = xr.DataArray(temp_values < ec_thr_arr, dims=temp_hourly.dims, coords=temp_hourly.coords)
    hazard_masks["extreme_heat"] = _rolling_consecutive_steps(extreme_heat_raw.fillna(False), MIN_CONSECUTIVE_STEPS["extreme_heat"])
    hazard_masks["extreme_cold"] = _rolling_consecutive_steps(extreme_cold_raw.fillna(False), MIN_CONSECUTIVE_STEPS["extreme_cold"])
    hazard_source_values["extreme_heat"] = temp_values
    hazard_source_values["extreme_cold"] = temp_values
    del eh_thr_arr, ec_thr_arr
    log.info(f"  temperature hazards built (var={temp_var}, month-conditioned thresholds via numpy)")

# --- Extreme wind (MRMS-based, subdaily) ---
wind_var = infer_var(ds_terr, ["i10fg", "I10FG", "wind_gust", "gust", "MAX_SHEAR", "VII_00.50", "wind"], required=False)
if wind_var is not None:
    wind_da = _prepare_hazard_time(ds_terr[wind_var], "extreme_wind")
    wind_thr = _quantile_threshold(wind_da, THRESHOLDS["wind_pct"])
    wind_mask = _rolling_consecutive_steps((wind_da > wind_thr).fillna(False), MIN_CONSECUTIVE_STEPS["extreme_wind"])
    hazard_masks["extreme_wind"] = wind_mask
    hazard_source_values["extreme_wind"] = np.asarray(wind_da.values, dtype=np.float32)
    log.info(f"  extreme_wind built (var={wind_var})")

# --- Extreme precipitation (gauge-corrected 1-hr accumulation) ---
precip_var = HAZARD_RULES["extreme_precip"]["source_variable"]
if precip_var in ds_terr.data_vars:
    p_da = _prepare_hazard_time(ds_terr[precip_var], "extreme_precip")
    p_hi = _quantile_threshold(p_da, THRESHOLDS["precip_high_pct"])
    hazard_masks["extreme_precip"] = (p_da > p_hi).fillna(False)
    hazard_source_values["extreme_precip"] = np.asarray(p_da.values, dtype=np.float32)
    log.info(f"  extreme_precip built (var={precip_var})")

# --- Space weather (already aggregated to the operational cadence during streaming load) ---
ex_var = infer_var(ds_space, ["Ex"], required=True)
ey_var = infer_var(ds_space, ["Ey"], required=True)
e_mag = np.hypot(ds_space[ex_var], ds_space[ey_var])
space_source = _prepare_hazard_time(e_mag, "space_weather_extreme")
space_threshold = _quantile_threshold(space_source, SPACE_WEATHER_OPERATIONAL["percentile"])
sw_mask = (space_source > space_threshold).fillna(False)
sw_mask = _rolling_consecutive_steps(sw_mask, SPACE_WEATHER_OPERATIONAL["persistence_steps"])
hazard_masks["space_weather_extreme"] = sw_mask
hazard_source_values["space_weather_extreme"] = np.asarray(space_source.values, dtype=np.float32)
log.info("  space_weather_extreme built (5-min operational aggregation, per-cell p95, 5-step persistence)")

# --- ERA5 synoptic wind (reuse cached daily-max gust from cell 50) ---
PRODUCTION_DIR = OUTPUT_ROOT / "production" / "v2_conditioned_2024_2025"
ERA5_WIND_CACHE = PRODUCTION_DIR / "era5_wind_daily.nc"
if ERA5_WIND_CACHE.exists():
    ds_era5_cache = xr.open_dataset(ERA5_WIND_CACHE)
    gust_50km = ds_era5_cache[list(ds_era5_cache.data_vars)[0]]
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=RuntimeWarning)
        p99_era5 = gust_50km.quantile(HAZARD_RULES["extreme_wind_era5"]["percentile"] / 100.0, dim="time", skipna=True)
        if "quantile" in p99_era5.dims:
            p99_era5 = p99_era5.squeeze("quantile", drop=True)
    p99_era5_floored = np.fmax(p99_era5, 15.0)
    era5_mask_raw = (gust_50km > p99_era5_floored).fillna(False)
    hazard_masks["extreme_wind_era5"] = era5_mask_raw.reindex(time=common_time, fill_value=False)
    hazard_source_values["extreme_wind_era5"] = np.asarray(
        gust_50km.reindex(time=common_time, fill_value=np.nan).values, dtype=np.float32)
    log.info(f"  extreme_wind_era5 built from cache: {ERA5_WIND_CACHE}")
else:
    log.info(f"  extreme_wind_era5 SKIPPED: cache not found at {ERA5_WIND_CACHE}")

# Harmonize each hazard to its own mode output timeline.
for h in list(hazard_masks):
    da = hazard_masks[h]
    if TEMPORAL_MODE.get(h, "daily") == "daily":
        da = _safe_daily_time_coord(da)
        hazard_masks[h] = da.reindex(time=common_time, fill_value=False)
    else:
        hazard_masks[h] = _safe_subdaily_time_coord(da)

for h, da in hazard_masks.items():
    frac = float(da.mean().compute() if hasattr(da.mean(), "compute") else da.mean())
    log.info(f"  {h:24s} mode={TEMPORAL_MODE.get(h, 'daily'):8s} active-cell fraction={frac:.5f}")

log.info(f"Step 2/6 complete in {_time.perf_counter() - t_step:.0f}s")

# ---------------------------------------------------------------------------
# Cell 4/5 equivalent: single-hazard event labeling
# ---------------------------------------------------------------------------
def _add_intensity_and_centroid(tbl: pd.DataFrame, labels: np.ndarray, source_values, ds_target_grid) -> pd.DataFrame:
    """Attach centroid lat/lon and peak/mean intensity (native source-field units) per event.

    Single pass over the label cube (same grouping strategy as _build_event_cache) instead of
    one np.where(labels == enum) scan per event.
    """
    if tbl.empty or source_values is None:
        for col in ("centroid_lat", "centroid_lon", "peak_intensity", "mean_intensity"):
            tbl[col] = np.nan
        return tbl

    ti_all, yi_all, xi_all = np.where(labels > 0)
    if len(yi_all) == 0:
        for col in ("centroid_lat", "centroid_lon", "peak_intensity", "mean_intensity"):
            tbl[col] = np.nan
        return tbl

    lbl_all = labels[labels > 0].astype(np.int64)
    order = np.argsort(lbl_all, kind="stable")
    lbl_sorted = lbl_all[order]
    ti_sorted = ti_all[order]
    yi_sorted = yi_all[order]
    xi_sorted = xi_all[order]
    unique_labels, boundaries = np.unique(lbl_sorted, return_index=True)
    bounds = np.r_[boundaries, len(lbl_sorted)]

    lat2d = np.asarray(ds_target_grid["lat"].values)
    lon2d = np.asarray(ds_target_grid["lon"].values)
    src = np.asarray(source_values, dtype=np.float32)

    stats_by_label: Dict[int, dict] = {}
    for i, enum in enumerate(unique_labels):
        ti_seg = ti_sorted[bounds[i]:bounds[i + 1]]
        yi_seg = yi_sorted[bounds[i]:bounds[i + 1]]
        xi_seg = xi_sorted[bounds[i]:bounds[i + 1]]
        valid_ti = ti_seg < src.shape[0]
        values = src[ti_seg[valid_ti], yi_seg[valid_ti], xi_seg[valid_ti]] if valid_ti.any() else np.array([np.nan])
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", category=RuntimeWarning)
            stats_by_label[int(enum)] = {
                "centroid_lat": float(np.median(lat2d[yi_seg, xi_seg])),
                "centroid_lon": float(np.median(lon2d[yi_seg, xi_seg])),
                "peak_intensity": float(np.nanmax(values)) if np.isfinite(values).any() else np.nan,
                "mean_intensity": float(np.nanmean(values)) if np.isfinite(values).any() else np.nan,
            }

    enums = tbl["event_id"].str.rsplit("_", n=1).str[-1].astype(int)
    for col in ("centroid_lat", "centroid_lon", "peak_intensity", "mean_intensity"):
        tbl[col] = [stats_by_label.get(e, {}).get(col, np.nan) for e in enums]
    return tbl


step("Step 3/6: Labeling single-hazard events (connected components in time+space)")
t_step = _time.perf_counter()

event_tables: Dict[str, pd.DataFrame] = {}
label_cubes: Dict[str, np.ndarray] = {}

for hz, da in hazard_masks.items():
    t_hz = _time.perf_counter()
    log.info(f"  Labeling: {hz} ...")
    tbl, lbl = label_single_hazard_events(da, hz)
    if hz == "wildfire" and wildfire_source != "gpkg_events":
        tbl = tbl[tbl["footprint_km2"] >= MIN_WILDFIRE_EVENT_AREA_KM2].copy()
    tbl = _add_intensity_and_centroid(tbl, lbl, hazard_source_values.get(hz), ds_target_grid)
    event_tables[hz] = tbl
    label_cubes[hz] = lbl
    tbl.to_csv(OUTPUT_DIR / f"single_hazard_events_{hz}.csv", index=False)
    log.info(f"  {hz}: {len(tbl)} events retained (in {_time.perf_counter() - t_hz:.0f}s)")

single_hazard_events = pd.concat(event_tables.values(), ignore_index=True)
single_hazard_events = single_hazard_events.sort_values(["start_time", "hazard"]).reset_index(drop=True)
single_hazard_events.to_csv(OUTPUT_DIR / "single_hazard_events_all.csv", index=False)

log.info(f"Total single-hazard events: {len(single_hazard_events)}")
log.info(single_hazard_events["hazard"].value_counts().to_string())
log.info(f"Step 3/6 complete in {_time.perf_counter() - t_step:.0f}s")

# Checkpoint: label cubes + event tables are the expensive-to-recompute artifacts of steps 2-3.
# Save them so a failure/change in steps 4-6 (linking, event sets) doesn't require redoing
# mask construction and event labeling.
CHECKPOINT_DIR = OUTPUT_DIR / "checkpoints"
CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
_t_ckpt = _time.perf_counter()
for _hz, _lbl in label_cubes.items():
    np.savez_compressed(CHECKPOINT_DIR / f"labels_{_hz}.npz", labels=_lbl.astype(np.int32))
with open(CHECKPOINT_DIR / "step3_complete.json", "w") as _f:
    json.dump({"hazards": list(label_cubes.keys()), "created": pd.Timestamp.now().isoformat()}, _f)
log.info(f"Checkpoint saved to {CHECKPOINT_DIR} in {_time.perf_counter() - _t_ckpt:.0f}s "
         f"(resume with resume_linking_from_checkpoint.py if steps 4-6 need to be redone)")

# ---------------------------------------------------------------------------
# Cell 4/5 equivalent: multihazard linking
# ---------------------------------------------------------------------------
step("Step 4/6: Linking multihazard event pairs")
t_step = _time.perf_counter()

hazards = sorted(event_tables)
edge_rows = []
event_cache_by_hazard: Dict[str, Dict[str, dict]] = {
    hz: _build_event_cache(event_tables[hz], label_cubes[hz]) for hz in hazards
}

for i in range(len(hazards)):
    for j in range(i + 1, len(hazards)):
        h1, h2 = hazards[i], hazards[j]
        c1 = event_cache_by_hazard[h1]
        c2 = event_cache_by_hazard[h2]
        if not c1 or not c2:
            continue
        log.info(f"  Linking pairs: {h1} vs {h2} ({len(c1)} x {len(c2)})")
        for eid1, ev1 in c1.items():
            for eid2, ev2 in c2.items():
                if _events_are_linked_cached(ev1, ev2, h1, h2):
                    edge_rows.append({
                        "event_id_a": eid1, "hazard_a": h1, "event_id_b": eid2, "hazard_b": h2,
                        "lag_hours": _lag_hours_for_ordered_pair(h1, h2),
                    })

edges_df = pd.DataFrame(edge_rows)
edges_df.to_csv(OUTPUT_DIR / "multi_hazard_event_links.csv", index=False)
log.info(f"Accepted links: {len(edges_df)}")
log.info(f"Step 4/6 complete in {_time.perf_counter() - t_step:.0f}s")

# ---------------------------------------------------------------------------
# Multihazard event sets: bounded-episode union-find (NOT raw connected components).
#
# PAIR_LAG_HOURS governs individual edges correctly, but connected-components performs
# unconstrained transitive closure: A-B and B-C both individually lag-valid does not mean A
# and C belong to the same physical episode. Merge two components only if the resulting
# episode's overall start-to-end span stays within EPISODE_MAX_SPAN_HOURS, so lagged pairwise
# links compose into bounded, physically plausible episodes instead of one global blob.
# ---------------------------------------------------------------------------
step("Step 5/6: Building multihazard event sets (bounded-episode union-find)")
t_step = _time.perf_counter()

event_bounds = single_hazard_events.set_index("event_id")[["start_time", "end_time"]].to_dict("index")
parent = {eid: eid for eid in event_bounds}
span_start = {eid: pd.Timestamp(v["start_time"]) for eid, v in event_bounds.items()}
span_end = {eid: pd.Timestamp(v["end_time"]) for eid, v in event_bounds.items()}


def _uf_find(x):
    while parent[x] != x:
        parent[x] = parent[parent[x]]
        x = parent[x]
    return x


def _uf_try_union(a, b, max_span_hours):
    ra, rb = _uf_find(a), _uf_find(b)
    if ra == rb:
        return True
    merged_start = min(span_start[ra], span_start[rb])
    merged_end = max(span_end[ra], span_end[rb])
    if (merged_end - merged_start) / pd.Timedelta(hours=1) > max_span_hours:
        return False
    parent[rb] = ra
    span_start[ra] = merged_start
    span_end[ra] = merged_end
    return True


rejected_for_span = 0
edges_sorted = edges_df.assign(
    _sort_key=lambda d: d["event_id_a"].map(lambda e: span_start[e])
).sort_values("_sort_key") if not edges_df.empty else edges_df
for _, edge in edges_sorted.iterrows():
    if not _uf_try_union(edge["event_id_a"], edge["event_id_b"], EPISODE_MAX_SPAN_HOURS):
        rejected_for_span += 1

groups: Dict[str, list] = {}
for eid in event_bounds:
    groups.setdefault(_uf_find(eid), []).append(eid)

set_rows = []
membership_rows = []
for k, comp in enumerate(sorted(groups.values(), key=lambda c: min(span_start[e] for e in c)), start=1):
    comp_events = single_hazard_events[single_hazard_events["event_id"].isin(comp)]
    hazards_in_set = sorted(comp_events["hazard"].unique().tolist())
    start = comp_events["start_time"].min()
    end = comp_events["end_time"].max()
    set_id = f"mh_set_{k:06d}"
    set_rows.append({
        "mh_set_id": set_id,
        "n_single_events": int(len(comp_events)),
        "n_hazard_types": int(len(hazards_in_set)),
        "hazard_types": "|".join(hazards_in_set),
        "start_time": start,
        "end_time": end,
        "duration_days": float((pd.Timestamp(end) - pd.Timestamp(start)) / pd.Timedelta(days=1) + 1.0),
    })
    for eid in comp:
        membership_rows.append({"mh_set_id": set_id, "event_id": eid})

mh_sets_df = pd.DataFrame(set_rows).sort_values(["start_time", "mh_set_id"])
mh_members_df = pd.DataFrame(membership_rows)
mh_sets_df.to_csv(OUTPUT_DIR / "multi_hazard_event_sets.csv", index=False)
mh_members_df.to_csv(OUTPUT_DIR / "multi_hazard_event_membership.csv", index=False)

log.info(f"Identified multi-hazard event sets: {len(mh_sets_df)}")
log.info(f"  Links rejected for exceeding {EPISODE_MAX_SPAN_HOURS:.0f}h episode-span cap: {rejected_for_span}")
log.info(f"  Largest episode: {mh_sets_df['n_single_events'].max() if len(mh_sets_df) else 0} events")
log.info(f"Step 5/6 complete in {_time.perf_counter() - t_step:.0f}s")

# ---------------------------------------------------------------------------
# Final summary
# ---------------------------------------------------------------------------
step("Step 6/6: Final summary")
summary = (
    single_hazard_events.groupby("hazard", observed=True)
    .agg(n_events=("event_id", "size"), first_start=("start_time", "min"), last_end=("end_time", "max"))
    .reindex(list(HAZARD_RULES))
    .fillna({"n_events": 0})
    .reset_index()
)
summary["n_events"] = summary["n_events"].astype(int)
log.info("\n" + summary.to_string(index=False))

step("PRODUCTION PIPELINE COMPLETE")
log.info(f"  Period: {TIME_START} to {TIME_END}")
log.info(f"  Total single-hazard events: {len(single_hazard_events)}")
log.info(f"  Multihazard event sets: {len(mh_sets_df)}")
log.info(f"  Output directory: {OUTPUT_DIR}")
log.info(f"  Log file: {LOG_FILE}")
