#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Reusable NOAA/USGS geoelectric-field (GeoE) daily-file access.

Consolidates the GeoE ingest logic previously duplicated across event
notebooks. Given an event window, `open_geoe` selects daily NetCDF files,
decodes the NOAA seconds-of-day time convention against each file's base
date, concatenates along time, and returns one raw site-level dataset.

Cleaned reprocessings (e.g. the 2024-2025 SWPC Testbed files from Jordan
Guerra under ``NOAA_geoE/2024-2025``) are preferred per day over legacy
files when ``cleaned_dir`` is supplied. Different model versions sit on
different site grids (``gridpt`` sizes differ), so mixed versions are never
concatenated: the cleaned group wins and dropped days are reported in the
provenance mapping.

Imports never fetch data; missing paths simply yield an empty selection.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd
import xarray as xr

GEOE_ROOT_DEFAULT = Path(
    "/Users/ryanmc/Documents/NASA_JPL/Projects/NaturalHazards/"
    "NASA ROSES Disasters 2025-2027/data/space_weather/NOAA_geoE"
)
GEOE_CLEANED_DIR_DEFAULT = GEOE_ROOT_DEFAULT / "2024-2025"

# Seconds-of-day values beyond this are sentinel/outlier entries, not times.
_PLAUSIBLE_SECONDS_MAX = 1e7


def _to_naive_timestamp(ts) -> pd.Timestamp:
    ts = pd.Timestamp(ts)
    return ts.tz_convert(None) if ts.tzinfo is not None else ts


def extract_yyyymmdd(text) -> Optional[pd.Timestamp]:
    """Extract the first YYYYMMDD date token from text, if parseable."""
    if text is None:
        return None
    m = re.search(r"(\d{8})", str(text))
    if not m:
        return None
    try:
        return pd.to_datetime(m.group(1), format="%Y%m%d")
    except (ValueError, TypeError):
        return None


def extract_geoe_base_time(ds_obj: xr.Dataset, file_path=None) -> Optional[pd.Timestamp]:
    """Resolve the base date for a NOAA GeoE daily file's seconds-of-day axis."""
    time_da = ds_obj.get("time", None)
    if time_da is not None:
        units = time_da.attrs.get("UNITS") or time_da.attrs.get("units")
        if units and "since" in str(units).lower():
            try:
                base_txt = re.split(r"since", str(units), flags=re.IGNORECASE)[1].strip()
                base = pd.to_datetime(base_txt, errors="coerce")
                if pd.notna(base):
                    return base
            except (IndexError, ValueError, TypeError):
                pass
        reftime = time_da.attrs.get("REFTIME") or time_da.attrs.get("reftime")
        if reftime:
            ts = pd.to_datetime(reftime, errors="coerce")
            if pd.notna(ts):
                return ts

    for cand in [
        ds_obj.attrs.get("FILENAME"),
        ds_obj.attrs.get("filename"),
        ds_obj.encoding.get("source"),
        str(file_path) if file_path is not None else None,
    ]:
        ts = extract_yyyymmdd(cand)
        if ts is not None:
            return ts
    return None


def select_geoe_files(
    root,
    day_start,
    day_end,
    cleaned_dir=None,
) -> Tuple[List[Path], Dict[str, dict]]:
    """Select one GeoE daily file per date, preferring cleaned over legacy.

    Returns (selected_files, provenance) where provenance maps
    'YYYY-MM-DD' -> {'source': 'cleaned'|'legacy', 'file': name}.
    Files without a filename date are excluded (daily GeoE files always
    carry YYYYMMDD names).
    """
    day_start = _to_naive_timestamp(day_start).normalize()
    day_end = _to_naive_timestamp(day_end).normalize()

    def _collect(p) -> List[Path]:
        p = Path(p) if p is not None else None
        if p is None or not p.exists():
            return []
        if p.is_file() and p.suffix.lower() == ".nc":
            return [p]
        return sorted(f for f in p.glob("*.nc"))

    legacy_files = _collect(root)
    cleaned_files = [f for f in _collect(cleaned_dir) if f not in legacy_files]

    by_day: Dict[pd.Timestamp, Dict[str, Path]] = {}
    for tier, files in [("legacy", legacy_files), ("cleaned", cleaned_files)]:
        for fp in files:
            d = extract_yyyymmdd(fp.name)
            if d is None or not (day_start <= d <= day_end):
                continue
            by_day.setdefault(d, {})[tier] = fp

    selected: List[Path] = []
    provenance: Dict[str, dict] = {}
    for d in sorted(by_day):
        slot = by_day[d]
        tier = "cleaned" if "cleaned" in slot else "legacy"
        selected.append(slot[tier])
        provenance[d.strftime("%Y-%m-%d")] = {"source": tier, "file": slot[tier].name}
    return selected, provenance


def resolve_mixed_gridpt_groups(
    datasets: Sequence[xr.Dataset],
    tiers: Sequence[str],
    provenance: Dict[str, dict],
) -> List[xr.Dataset]:
    """Keep only datasets sharing one gridpt site grid; never mix model versions.

    The cleaned group wins when present; otherwise the most common size.
    Records a '_mixed_grid_note' entry in provenance when days are dropped.
    """
    sizes = [int(ds.sizes.get("gridpt", -1)) for ds in datasets]
    if len(set(sizes)) <= 1:
        return list(datasets)

    cleaned_sizes = {s for s, t in zip(sizes, tiers) if t == "cleaned"}
    if cleaned_sizes:
        keep_size = sorted(cleaned_sizes)[0]
    else:
        from collections import Counter

        keep_size = Counter(sizes).most_common(1)[0][0]

    dropped = [
        str(pd.Timestamp(ds["time"].values[0]).date())
        for ds, s in zip(datasets, sizes)
        if s != keep_size and ds.sizes.get("time", 0) > 0
    ]
    provenance["_mixed_grid_note"] = (
        f"Kept gridpt={keep_size} group; dropped {len(dropped)} day(s) on a "
        f"different site grid: {dropped}. NOAA GeoE model versions are never "
        "mixed in one concatenated dataset."
    )
    return [ds for ds, s in zip(datasets, sizes) if s == keep_size]


def _decode_daily_time(ds: xr.Dataset, fp: Path) -> Optional[xr.Dataset]:
    """Decode one daily file's time axis to absolute datetime64, or None."""
    if "time" not in ds.coords:
        return None
    tvals = np.asarray(ds["time"].values)
    if tvals.size == 0:
        return None

    if np.issubdtype(tvals.dtype, np.datetime64):
        return ds

    sec = np.asarray(tvals, dtype=float)
    finite = np.isfinite(sec)
    if not finite.any():
        return None
    if not finite.all():
        keep = np.where(finite)[0]
        ds = ds.isel(time=keep)
        sec = sec[keep]

    plausible = np.abs(sec) <= _PLAUSIBLE_SECONDS_MAX
    if not plausible.any():
        return None
    if not plausible.all():
        keep = np.where(plausible)[0]
        ds = ds.isel(time=keep)
        sec = sec[keep]

    base = extract_geoe_base_time(ds, fp)
    if base is None or pd.isna(base):
        return None

    abs_time = base + pd.to_timedelta(sec, unit="s")
    return ds.assign_coords(
        time=("time", pd.DatetimeIndex(abs_time).to_numpy(dtype="datetime64[ns]"))
    )


def open_geoe(
    root,
    event_start,
    event_end,
    cleaned_dir=None,
    return_provenance: bool = False,
):
    """Open GeoE daily files for an event window as one raw site-level dataset.

    Prefers per-day cleaned files from ``cleaned_dir`` and refuses to mix
    model versions with different site grids. Returns the dataset, or
    (dataset, provenance) when ``return_provenance`` is True. Either return
    slot can be None/empty when nothing usable is found.
    """
    start = _to_naive_timestamp(event_start)
    end = _to_naive_timestamp(event_end)

    selected_files, provenance = select_geoe_files(
        root, start, end, cleaned_dir=cleaned_dir
    )
    if not selected_files:
        return (None, provenance) if return_provenance else None

    file_tier = {
        v["file"]: v["source"] for v in provenance.values() if isinstance(v, dict)
    }

    datasets: List[xr.Dataset] = []
    tiers: List[str] = []
    for fp in selected_files:
        ds_t = _decode_daily_time(xr.open_dataset(fp), fp)
        if ds_t is None:
            continue
        ds_t = ds_t.sortby("time").sel(time=slice(start, end))
        if ds_t.sizes.get("time", 0) > 0:
            datasets.append(ds_t)
            tiers.append(file_tier.get(fp.name, "legacy"))

    if not datasets:
        return (None, provenance) if return_provenance else None

    datasets = resolve_mixed_gridpt_groups(datasets, tiers, provenance)

    if len(datasets) == 1:
        out = datasets[0]
    else:
        out = xr.concat(
            datasets, dim="time", data_vars="minimal", coords="minimal", compat="override"
        ).sortby("time")

    t = pd.DatetimeIndex(out["time"].values)
    _, first_idx = np.unique(t.values, return_index=True)
    out = out.isel(time=np.sort(first_idx))

    return (out, provenance) if return_provenance else out
