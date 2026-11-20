#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Stream annual Eagle-I outages onto the supplied CONUS analysis grid.

Production uses two bounded source passes per year, preferring the cached
Parquet panel, then CSV panel, then annual raw CSV. County polygons are mapped
through the grid-specific mapping API, never the legacy intersects cache.
Float64 contributions and invalid-report masks are disk-backed; annual NetCDF
files are compressed daily slabs. QA and a provenance manifest accompany each
atomic output. A failed/interrupted run leaves partial files that must be
inspected and explicitly removed before retrying (even with --overwrite).

Zero means no reported outage, not verified availability or completeness.
Unmapped footprint and reported invalid/suppressed contributions are NaN.
The legacy eager aggregation helpers remain available for small callers;
their old off-axis-drop behavior is NOT used by the strict production driver.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from eaglei_grid_mapping import (  # noqa: E402
    EAGLEI_DIR_DEFAULT,
    EAGLEI_PROCESSED_DIR_DEFAULT,
    normalize_fips,
)

# ------------------------
# Config
# ------------------------

YEARS = [2024, 2025]

EAGLEI_DIR = EAGLEI_DIR_DEFAULT
EAGLEI_PROCESSED_DIR = EAGLEI_PROCESSED_DIR_DEFAULT
# NOTE (2026-11-12): this repository copy is the 67x107 grid from Nov 3.
# Eagle-I outputs already generated with it have NOT been reprocessed for the
# later 68x110 V3 grid (which covers all of Washington County, Maine). Pass the
# external V3 grid explicitly with --grid when regenerating those products.
GRID_PATH = Path(__file__).resolve().parents[1] / "data/analysis_grid_CONUS_50km_v1.nc"

PANEL_CSV = EAGLEI_PROCESSED_DIR / "eaglei_county_15min_panel_2024_2025.csv"

OUTPUT_FILENAME_TEMPLATE = "eaglei_outages_analysis_grid_{year}.nc"

TIME_FREQ = "15min"
PANEL_CHUNKSIZE = 250_000
RAW_CSV_CHUNKSIZE = 250_000


# ------------------------
# Panel loading
# ------------------------

def load_panel_for_year(
    year: int,
    panel_csv: Path = PANEL_CSV,
    eaglei_dir: Path = EAGLEI_DIR,
    chunksize: int = PANEL_CHUNKSIZE,
) -> pd.DataFrame:
    """
    Return a DataFrame with columns [time, county_fips, customers_out] for
    the requested year, from the cached county 15-min panel if present,
    otherwise built directly from the raw yearly Eagle-I CSV.
    """
    if panel_csv.exists():
        print(f"Loading cached county panel for {year} from: {panel_csv}")
        pieces = []
        usecols = ["time", "county_fips", "customers_out"]
        for chunk in pd.read_csv(
            panel_csv,
            usecols=usecols,
            dtype={"county_fips": str},
            chunksize=chunksize,
        ):
            chunk["time"] = pd.to_datetime(chunk["time"], errors="coerce")
            chunk = chunk[chunk["time"].dt.year == year]
            if not chunk.empty:
                chunk["county_fips"] = normalize_fips(chunk["county_fips"], 5)
                pieces.append(chunk)
        if not pieces:
            return pd.DataFrame(columns=usecols)
        panel = pd.concat(pieces, ignore_index=True)
        return panel

    print(
        f"Cached panel not found ({panel_csv}); building panel for {year} "
        "directly from the raw yearly Eagle-I CSV."
    )
    return _build_panel_from_raw_csv(year, eaglei_dir, chunksize=RAW_CSV_CHUNKSIZE)


def _find_column(columns, candidates) -> Optional[str]:
    lower = {c.lower(): c for c in columns}
    normalized = {c.lower().replace(" ", "_"): c for c in columns}
    for candidate in candidates:
        cl = candidate.lower()
        cn = cl.replace(" ", "_")
        if cl in lower:
            return lower[cl]
        if cn in normalized:
            return normalized[cn]
    return None


def _build_panel_from_raw_csv(
    year: int, eaglei_dir: Path, chunksize: int = RAW_CSV_CHUNKSIZE
) -> pd.DataFrame:
    raw_path = eaglei_dir / f"eaglei_outages_{year}.csv"
    if not raw_path.exists():
        raise FileNotFoundError(f"Raw Eagle-I CSV not found for {year}: {raw_path}")

    header = pd.read_csv(raw_path, nrows=0)
    time_col = _find_column(header.columns, ["run_start_time", "Run Start Time"])
    fips_col = _find_column(header.columns, ["fips_code", "Fips Code"])
    out_col = _find_column(header.columns, ["customers_out", "Customers Out"])
    if time_col is None or fips_col is None or out_col is None:
        raise KeyError(
            f"Could not find expected columns in {raw_path}. "
            f"Columns available: {list(header.columns)}"
        )

    pieces = []
    for chunk in pd.read_csv(
        raw_path, usecols=[time_col, fips_col, out_col], chunksize=chunksize
    ):
        chunk = chunk.rename(
            columns={time_col: "time", fips_col: "county_fips", out_col: "customers_out"}
        )
        chunk["time"] = pd.to_datetime(chunk["time"], errors="coerce")
        chunk["county_fips"] = normalize_fips(chunk["county_fips"], 5)
        chunk["customers_out"] = pd.to_numeric(chunk["customers_out"], errors="coerce")
        chunk = chunk.dropna(subset=["time", "county_fips", "customers_out"])
        pieces.append(chunk[["time", "county_fips", "customers_out"]])

    if not pieces:
        return pd.DataFrame(columns=["time", "county_fips", "customers_out"])
    return pd.concat(pieces, ignore_index=True)


# ------------------------
# Grid template access (thin wrapper so this module only needs xarray/pyproj
# when actually building output, not for the pure aggregation logic below)
# ------------------------

def load_grid_template(grid_path: Path = GRID_PATH):
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from grid import AnalysisGridTemplate  # local import: needs xarray/pyproj

    return AnalysisGridTemplate(grid_file=str(grid_path))


# ------------------------
# Core aggregation (pure pandas/numpy -- unit-testable without geopandas/xarray)
# ------------------------

def build_time_index(year: int, freq: str = TIME_FREQ) -> pd.DatetimeIndex:
    start = pd.Timestamp(year=year, month=1, day=1)
    end = pd.Timestamp(year=year, month=12, day=31, hour=23, minute=45)
    return pd.date_range(start=start, end=end, freq=freq)


def aggregate_panel_to_grid(
    panel: pd.DataFrame,
    weights: pd.DataFrame,
    time_index: pd.DatetimeIndex,
    ny: int,
    nx: int,
    dtype=np.float32,
) -> tuple[np.ndarray, dict]:
    """
    Core gridding step, isolated from any geospatial library so it can be
    unit tested with plain pandas/numpy.

    panel:   columns [time, county_fips, customers_out]
    weights: columns [county_fips, grid_y, grid_x, weight]  (weights should
             sum to ~1 per county_fips)
    time_index: full regular time axis to grid onto; rows in `panel` whose
             timestamp does not land exactly on this axis are dropped and
             counted in the returned QA dict.

    Returns (out_array, qa) where out_array has shape (len(time_index), ny, nx)
    and qa is a dict of diagnostic counts/sums.
    """
    merged = panel.merge(
        weights[["county_fips", "grid_y", "grid_x", "weight"]],
        on="county_fips",
        how="inner",
    )
    merged["contribution"] = merged["customers_out"] * merged["weight"]

    sparse = (
        merged.groupby(["time", "grid_y", "grid_x"], sort=False)["contribution"]
        .sum()
        .reset_index()
    )

    time_pos = pd.Index(time_index).get_indexer(sparse["time"])
    on_axis = time_pos >= 0
    n_off_axis = int((~on_axis).sum())

    out = np.zeros((len(time_index), ny, nx), dtype=dtype)
    if on_axis.any():
        np.add.at(
            out,
            (
                time_pos[on_axis],
                sparse.loc[on_axis, "grid_y"].to_numpy(),
                sparse.loc[on_axis, "grid_x"].to_numpy(),
            ),
            sparse.loc[on_axis, "contribution"].to_numpy(dtype=dtype),
        )

    mapped_counties = set(weights["county_fips"])
    raw_total_mapped = float(
        panel.loc[panel["county_fips"].isin(mapped_counties), "customers_out"].sum()
    )
    gridded_total = float(out.sum())

    qa = {
        "n_panel_rows": int(len(panel)),
        "n_merged_rows": int(len(merged)),
        "n_sparse_cells": int(len(sparse)),
        "n_rows_off_time_axis": n_off_axis,
        "raw_total_customers_out_mapped_counties": raw_total_mapped,
        "gridded_total_customers_out": gridded_total,
        "conservation_ratio": (
            gridded_total / raw_total_mapped if raw_total_mapped else float("nan")
        ),
    }
    return out, qa


# ------------------------
# Output dataset construction / writing (needs xarray)
# ------------------------

def build_output_dataset(out_array: np.ndarray, time_index: pd.DatetimeIndex, grid_template, year: int, qa: dict, grid_path: Path = GRID_PATH):
    import xarray as xr

    y_centers = grid_template.grid.coords["y"].values
    x_centers = grid_template.grid.coords["x"].values
    lat2d = grid_template.grid["lat"].values
    lon2d = grid_template.grid["lon"].values

    ds = xr.Dataset(
        {
            "customers_out": (
                ("time", "y", "x"),
                out_array,
                {
                    "long_name": "Number of customers without power",
                    "units": "customers",
                    "description": (
                        "County-level Eagle-I customers_out values redistributed "
                        "onto the 50 km analysis grid using area-weighted "
                        "intersection between county polygons and grid cells "
                        "(weights sum to 1 per county, so gridded totals "
                        "conserve county totals for counties mapped to this "
                        "grid). Cells/timesteps with no reported outage are 0."
                    ),
                },
            )
        },
        coords={
            "time": time_index,
            "y": y_centers,
            "x": x_centers,
            "lat": (("y", "x"), lat2d),
            "lon": (("y", "x"), lon2d),
        },
        attrs={
            "title": f"Eagle-I power outage counts on 50 km CONUS analysis grid, {year}",
            "source": "Oak Ridge National Laboratory Eagle-I power outage dataset (DOE)",
            "grid_source": str(grid_path),
            "mapping_method": "area_weighted_county_to_grid_cell_intersection",
            "temporal_resolution": TIME_FREQ,
            "year": year,
            "created": datetime.now(timezone.utc).isoformat(),
            "qa_n_panel_rows": qa["n_panel_rows"],
            "qa_n_rows_off_time_axis": qa["n_rows_off_time_axis"],
            "qa_raw_total_customers_out_mapped_counties": qa[
                "raw_total_customers_out_mapped_counties"
            ],
            "qa_gridded_total_customers_out": qa["gridded_total_customers_out"],
            "qa_conservation_ratio": qa["conservation_ratio"],
        },
    )
    return ds


def write_year_netcdf(ds, out_path: Path):
    encoding = {"customers_out": {"zlib": True, "complevel": 4}}
    out_path.parent.mkdir(parents=True, exist_ok=True)
    ds.to_netcdf(out_path, format="NETCDF4", encoding=encoding)
    print(f"Wrote {out_path}")


# ------------------------
# Per-year driver
# ------------------------

ZERO_SEMANTICS = (
    "Within the mapped county footprint, zero means no reported outages, NOT "
    "verified power availability or source completeness. Outside that footprint "
    "is NaN. A reported suppressed/nonfinite county value makes every affected "
    "cell/time NaN, including contributions from other counties sharing that cell. "
    "Wholly absent source timestamps are NaN. Consult source_row_count; sparse reports do "
    "not establish county-level reporting completeness."
)
PIPELINE_VERSION = "streaming-grid-v1-1"


def source_identity(path):
    path = Path(path).resolve()
    stat = path.stat()
    return {"path": str(path), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def select_source(year, eaglei_dir, source="panel", panel_csv=None):
    """Prefer the bounded Parquet reader, then panel CSV, then annual raw CSV."""
    eaglei_dir = Path(eaglei_dir)
    if source == "panel":
        csv = Path(panel_csv) if panel_csv else (
            eaglei_dir / "processed/eaglei_county_15min_panel_2024_2025.csv"
        )
        for path in (csv.with_suffix(".parquet"), csv):
            if path.exists():
                return path, "panel"
    elif source != "raw":
        raise ValueError(f"Unknown source: {source}")
    path = eaglei_dir / f"eaglei_outages_{year}.csv"
    if not path.exists():
        raise FileNotFoundError(path)
    return path, "raw"


def iter_source_chunks(path, year, chunksize=250_000):
    """Read only needed columns; normalize UTC and reject rather than drop bad keys."""
    path = Path(path)
    if chunksize <= 0:
        raise ValueError("chunksize must be positive")
    if path.suffix == ".parquet":
        import pyarrow.parquet as pq
        parquet = pq.ParquetFile(path)
        columns = parquet.schema_arrow.names
    else:
        columns = pd.read_csv(path, nrows=0).columns
    names = {
        "time": _find_column(columns, ["time", "run_start_time"]),
        "county_fips": _find_column(columns, ["county_fips", "fips_code"]),
        "customers_out": _find_column(columns, ["customers_out"]),
    }
    if any(v is None for v in names.values()):
        raise ValueError(f"Missing required source columns in {path}: {list(columns)}")
    flags = [c for c in columns if c.lower().replace(" ", "_") in
             ("is_suppressed_override", "contains_override_data")]
    selected = list(names.values()) + flags
    if path.suffix == ".parquet":
        reader = (b.to_pandas() for b in parquet.iter_batches(
            batch_size=chunksize, columns=selected, use_threads=False))
    else:
        reader = pd.read_csv(path, usecols=selected, chunksize=chunksize,
                             dtype={names["county_fips"]: str})
    for chunk in reader:
        chunk = chunk.rename(columns={v: k for k, v in names.items()})
        # format='mixed' is necessary for mixed ISO strings on pandas >=2.
        times = chunk["time"]
        parse_kwargs = {"format": "mixed"} if int(pd.__version__.split(".")[0]) >= 2 else {}
        times = pd.to_datetime(times, utc=True, errors="raise", **parse_kwargs)
        if times.isna().any():
            raise ValueError("Missing source timestamp")
        chunk["time"] = times.dt.tz_convert(None)
        chunk = chunk.loc[chunk["time"].dt.year == year].copy()
        if chunk.empty:
            continue
        chunk["county_fips"] = chunk["county_fips"].astype(str).str.replace(
            r"\.0$", "", regex=True).str.zfill(5)
        if not chunk["county_fips"].str.fullmatch(r"\d{5}").all():
            raise ValueError("Invalid county FIPS in source")
        ns = chunk["time"].astype("int64").to_numpy()
        if np.any(ns % (15 * 60 * 1_000_000_000)):
            raise ValueError("Source timestamp is off the exact UTC 15-minute axis")
        values = pd.to_numeric(chunk["customers_out"], errors="coerce").to_numpy(dtype=float)
        if np.any(np.isfinite(values) & (values < 0)):
            raise ValueError("Finite negative customers_out is invalid")
        suppressed = np.zeros(len(chunk), dtype=bool)
        for flag in flags:
            text = chunk[flag].astype(str).str.lower().str.strip()
            if not text.isin(["true", "false", "1", "0", "1.0", "0.0"]).all():
                raise ValueError(f"Invalid or missing suppression flag: {flag}")
            suppressed |= text.isin(["true", "1", "1.0"]).to_numpy()
        chunk["customers_out"] = values
        chunk["suppressed"] = suppressed
        chunk["invalid"] = suppressed | ~np.isfinite(values)
        yield chunk[["time", "county_fips", "customers_out", "suppressed", "invalid"]]


def discover_source(path, year, chunksize):
    """First pass: bounded per-county statistics, including all unmapped reports."""
    stats = {}
    for chunk in iter_source_chunks(path, year, chunksize):
        chunk["valid_total"] = chunk["customers_out"].where(~chunk["invalid"], 0.0)
        chunk["nonfinite"] = ~np.isfinite(chunk["customers_out"])
        grouped = chunk.groupby("county_fips").agg(
            rows=("invalid", "size"), invalid_rows=("invalid", "sum"),
            suppressed_rows=("suppressed", "sum"), nonfinite_rows=("nonfinite", "sum"),
            valid_total=("valid_total", "sum"))
        for county, row in grouped.iterrows():
            entry = stats.setdefault(county, {"rows": 0, "invalid_rows": 0,
                                              "suppressed_rows": 0, "nonfinite_rows": 0,
                                              "valid_total": 0.0})
            for key in entry:
                entry[key] += float(row[key]) if key == "valid_total" else int(row[key])
    if not stats:
        raise ValueError(f"No source rows for {year}: {path}")
    return stats


def _validated_weights(weights, counties, ny, nx):
    weights = weights.copy()
    weights["county_fips"] = normalize_fips(weights["county_fips"], 5)
    if not set(weights["county_fips"]).issubset(counties):
        raise ValueError("Mapping contains unrequested counties")
    # CONUS states plus DC; exclude AK, HI and territories even if a bad cache is supplied.
    conus = {"01", "04", "05", "06", "08", "09", "10", "11", "12", "13",
             "16", "17", "18", "19", "20", "21", "22", "23", "24", "25",
             "26", "27", "28", "29", "30", "31", "32", "33", "34", "35",
             "36", "37", "38", "39", "40", "41", "42", "44", "45", "46",
             "47", "48", "49", "50", "51", "53", "54", "55", "56"}
    if not weights["county_fips"].str[:2].isin(conus).all():
        raise ValueError("Mapping must be CONUS only")
    for name, limit in (("grid_y", ny), ("grid_x", nx)):
        values = weights[name].to_numpy(dtype=float)
        if not np.all(np.isfinite(values) & (values >= 0) & (values < limit) &
                      (values == np.floor(values))):
            raise ValueError(f"Invalid mapping {name}")
        weights[name] = values.astype(np.int64)
    w = weights["weight"].to_numpy(dtype=float)
    if not np.all(np.isfinite(w) & (w > 0)):
        raise ValueError("Mapping weights must be positive finite values")
    if weights.duplicated(["county_fips", "grid_y", "grid_x"]).any():
        raise ValueError("Duplicate mapping edges")
    sums = weights.groupby("county_fips")["weight"].sum().to_numpy()
    if not np.allclose(sums, 1, rtol=1e-10, atol=1e-12):
        raise ValueError("Mapping weights do not sum to one per county")
    return weights


def _scatter_source(path, year, chunksize, time_index, counties, weights,
                    dense, invalid, seen, nx):
    """Second pass. Scatter at most 1M edges per operation; never join a year."""
    nt = len(time_index)
    county_index = pd.Index(counties)
    edges = {}
    for county, group in weights.groupby("county_fips", sort=False):
        edges[county_index.get_loc(county)] = (
            (group.grid_y.to_numpy() * nx + group.grid_x.to_numpy()).astype(np.int64),
            group.weight.to_numpy(dtype=np.float64))
    source_count = np.zeros(nt, dtype=np.int64)
    mapped_count = np.zeros(nt, dtype=np.int64)
    invalid_count = np.zeros(nt, dtype=np.int64)
    source_total = np.zeros(nt, dtype=np.float64)
    mapped_total = np.zeros(nt, dtype=np.float64)
    valid_mapped_rows = 0
    for chunk in iter_source_chunks(path, year, chunksize):
        t = time_index.get_indexer(chunk["time"])
        if (t < 0).any():
            raise ValueError("Source row is off the output time axis")
        c = county_index.get_indexer(chunk["county_fips"])
        if (c < 0).any():
            raise RuntimeError("Source counties changed between passes")
        keys = c.astype(np.int64) * nt + t
        if len(np.unique(keys)) != len(keys) or np.any(seen[keys]):
            raise ValueError("Duplicate county/time source rows (including across batches)")
        seen[keys] = 1
        values = chunk["customers_out"].to_numpy(dtype=np.float64)
        bad = chunk["invalid"].to_numpy(dtype=bool)
        np.add.at(source_count, t, 1)
        np.add.at(source_total, t[~bad], values[~bad])
        order = np.argsort(c, kind="stable")
        boundaries = np.r_[0, np.flatnonzero(np.diff(c[order])) + 1, len(c)]
        for lo, hi in zip(boundaries[:-1], boundaries[1:]):
            rows = order[lo:hi]
            edge = edges.get(c[rows[0]])
            if edge is None:
                continue
            cells, w = edge
            np.add.at(mapped_count, t[rows], 1)
            good = rows[~bad[rows]]
            valid_mapped_rows += len(good)
            np.add.at(mapped_total, t[good], values[good])
            np.add.at(invalid_count, t[rows[bad[rows]]], 1)
            block = max(1, 1_000_000 // len(cells))
            for start in range(0, len(rows), block):
                selected = rows[start:start + block]
                good = selected[~bad[selected]]
                np.add.at(dense, (t[good, None], cells[None, :]), values[good, None] * w)
                rejected = selected[bad[selected]]
                invalid[t[rejected, None], cells[None, :]] = 1
    return {"source_row_count": source_count, "mapped_row_count": mapped_count,
            "invalid_mapped_row_count": invalid_count, "source_valid_total": source_total,
            "mapped_valid_total": mapped_total}, valid_mapped_rows


def _write_streamed_netcdf(partial, grid, time_index, dense, invalid, footprint,
                           series, identity, year):
    """Write compressed daily slabs and verify conservation before masking NaNs."""
    import netCDF4
    nt, ncells = dense.shape
    ny, nx = len(grid.y), len(grid.x)
    gridded_total = np.zeros(nt, dtype=np.float64)
    visible_total = np.zeros(nt, dtype=np.float64)
    masked_contribution = np.zeros(nt, dtype=np.float64)
    with netCDF4.Dataset(partial, "w", format="NETCDF4") as ds:
        for name, size in (("time", nt), ("y", ny), ("x", nx)):
            ds.createDimension(name, size)
        for name in ("y", "x"):
            var = ds.createVariable(name, "f8", (name,))
            var[:] = grid[name].values
            var.setncatts({k: v for k, v in grid[name].attrs.items() if k != "_FillValue"})
        time = ds.createVariable("time", "i8", ("time",))
        time.units = f"minutes since {year}-01-01 00:00:00"
        time.calendar = "proleptic_gregorian"
        time[:] = ((time_index - pd.Timestamp(year=year, month=1, day=1)) /
                   pd.Timedelta(minutes=1)).to_numpy(dtype=np.int64)
        for name in ("lat", "lon"):
            var = ds.createVariable(name, "f8", ("y", "x"))
            var[:] = grid[name].transpose("y", "x").values
            var.units = "degrees_north" if name == "lat" else "degrees_east"
        count = ds.createVariable("mapped_county_count", "i4", ("y", "x"))
        count[:] = footprint.reshape(ny, nx)
        count.long_name = "Number of source counties mapped to each cell (not temporal coverage)"
        output = ds.createVariable("customers_out", "f8", ("time", "y", "x"),
                                   zlib=True, complevel=4, fill_value=np.nan,
                                   chunksizes=(min(96, nt), ny, nx))
        output.units = "customers"
        output.coordinates = "lat lon"
        output.description = ZERO_SEMANTICS
        for start in range(0, nt, 96):
            stop = min(start + 96, nt)
            slab = np.array(dense[start:stop], dtype=np.float64)
            gridded_total[start:stop] = slab.sum(axis=1, dtype=np.float64)
            if not np.allclose(gridded_total[start:stop],
                               series["mapped_valid_total"][start:stop], rtol=1e-5, atol=1e-8):
                raise ValueError("Per-timestamp mass conservation failed")
            mask = np.asarray(invalid[start:stop], dtype=bool)
            masked_contribution[start:stop] = np.where(mask, slab, 0).sum(axis=1)
            slab[mask | (footprint[None, :] == 0)] = np.nan
            slab[series["source_row_count"][start:stop] == 0] = np.nan
            visible_total[start:stop] = np.nansum(slab, axis=1)
            output[start:stop] = slab.reshape(stop - start, ny, nx)
        series.update(gridded_valid_total=gridded_total, output_visible_total=visible_total,
                      valid_contribution_hidden_by_nan=masked_contribution)
        for name, values in series.items():
            var = ds.createVariable(name, "i8" if values.dtype.kind in "iu" else "f8", ("time",))
            var[:] = values
            var.units = "rows" if "row_count" in name else "customers"
        ds["source_row_count"].description = (
            "All reported source rows, including unmapped and invalid. Zero flags a wholly "
            "absent source timestamp; positive counts do not prove complete reporting.")
        ds["gridded_valid_total"].description = (
            "Sum of valid mapped contributions BEFORE masking cells affected by invalid reports. "
            "Equals output_visible_total + valid_contribution_hidden_by_nan.")
        ds.setncatts({"title": f"Eagle-I outages on supplied CONUS analysis grid, {year}",
                      "year": year, "grid_source": identity["grid_source"],
                      "grid_sha256": identity["grid_sha256"],
                      "proj": grid.attrs.get("proj", "EPSG:5070"),
                      "source_identity": json.dumps(identity["source"], sort_keys=True),
                      "pipeline_version": PIPELINE_VERSION, "temporal_resolution": TIME_FREQ,
                      "mapping_method": "area_weighted_polygon_intersection_no_nearest_fallback",
                      "zero_semantics": ZERO_SEMANTICS,
                      "created": datetime.now(timezone.utc).isoformat()})
    expected = float(series["mapped_valid_total"].sum(dtype=np.float64))
    actual = float(gridded_total.sum(dtype=np.float64))
    if not np.isclose(actual, expected, rtol=1e-5, atol=1e-8):
        raise ValueError("Annual mass conservation failed")
    return expected, actual


def process_year(year, grid_template=None, panel_csv=None, eaglei_dir=EAGLEI_DIR,
                 output_dir=None, *, grid_path=GRID_PATH, source="panel",
                 overwrite=False, chunksize=250_000):
    """Two-pass streaming production driver. Legacy eager helpers are not used."""
    import xarray as xr
    import netCDF4
    # Imported lazily while the new mapping API is being developed independently.
    from eaglei_grid_mapping import grid_fingerprint, load_or_build_grid_mapping

    eaglei_dir, grid_path = Path(eaglei_dir), Path(grid_path).resolve()
    if grid_template is not None:
        raise ValueError("Supply grid_path, not a legacy in-memory grid_template")
    output_dir = Path(output_dir) if output_dir else eaglei_dir / "processed/analysis_grid_CONUS_50km_v1"
    if output_dir.resolve() == eaglei_dir.resolve():
        raise ValueError("Refusing legacy raw-directory outputs; choose a separate output directory")
    output_dir.mkdir(parents=True, exist_ok=True)
    out = output_dir / OUTPUT_FILENAME_TEMPLATE.format(year=year)
    qa_path, manifest_path = out.with_suffix(".qa.json"), out.with_suffix(".manifest.json")
    partial = Path(str(out) + ".partial")
    work = [Path(str(partial) + suffix) for suffix in (".dense", ".invalid", ".seen")]
    qa_partial, manifest_partial = Path(str(qa_path) + ".partial"), Path(str(manifest_path) + ".partial")
    if any(p.exists() for p in [partial, qa_partial, manifest_partial] + work):
        raise FileExistsError(f"Stale partial files for {out}; inspect and remove explicitly before retry")
    path, actual_source = select_source(year, eaglei_dir, source, panel_csv)
    identity = {"pipeline_version": PIPELINE_VERSION, "year": year,
                "grid_source": str(grid_path), "grid_sha256": grid_fingerprint(grid_path),
                "source": source_identity(path), "source_kind": actual_source,
                "zero_semantics": ZERO_SEMANTICS}
    if any(p.exists() for p in (out, qa_path, manifest_path)) and not overwrite:
        if not all(p.exists() for p in (out, qa_path, manifest_path)):
            raise FileExistsError("Incomplete output set; explicit --overwrite required")
        saved = json.loads(manifest_path.read_text())
        if saved.get("identity") != identity or not saved.get("success"):
            raise FileExistsError("Existing output grid/source identity mismatch; explicit --overwrite required")
        with netCDF4.Dataset(out) as ds, xr.open_dataset(grid_path) as grid:
            if (ds.getncattr("grid_sha256") != identity["grid_sha256"] or
                ds.getncattr("grid_source") != str(grid_path) or
                ds.getncattr("source_identity") != json.dumps(identity["source"], sort_keys=True) or
                len(ds.dimensions["time"]) != len(build_time_index(year)) or
                not np.array_equal(ds["x"][:], grid.x.values) or
                not np.array_equal(ds["y"][:], grid.y.values)):
                raise ValueError("Existing NetCDF provenance or axes do not match")
        qa = json.loads(qa_path.read_text())
        if qa.get("identity") != identity or not qa.get("conservation_passed"):
            raise ValueError("Existing QA does not match successful output")
        print(f"Validated existing output; skipping {out}", flush=True)
        return qa
    print(f"{year}: discovering counties from {path}", flush=True)
    stats = discover_source(path, year, chunksize)
    if source_identity(path) != identity["source"]:
        raise RuntimeError("Source changed during discovery")
    time_index = build_time_index(year)
    with xr.open_dataset(grid_path) as grid_file:
        grid = grid_file.load()
    ny, nx = len(grid.y), len(grid.x)
    counties = sorted(stats)
    weights = load_or_build_grid_mapping(counties, grid_path, output_dir / f"mapping_{year}")
    weights = _validated_weights(weights, counties, ny, nx)
    footprint = np.zeros(ny * nx, dtype=np.int32)
    np.add.at(footprint, weights.grid_y.to_numpy() * nx + weights.grid_x.to_numpy(), 1)
    shape = (len(time_index), ny * nx)
    dense = np.memmap(work[0], mode="w+", dtype="float64", shape=shape)
    invalid = np.memmap(work[1], mode="w+", dtype="uint8", shape=shape)
    seen = np.memmap(work[2], mode="w+", dtype="uint8", shape=(len(counties) * len(time_index),))
    # Newly created/truncated files are OS zero-filled, without touching every page.
    try:
        print(f"{year}: scattering bounded batches onto {ny} x {nx}", flush=True)
        series, valid_rows = _scatter_source(path, year, chunksize, time_index, counties,
                                            weights, dense, invalid, seen, nx)
        dense.flush()
        invalid.flush()
        if int(series["source_row_count"].sum()) != sum(v["rows"] for v in stats.values()):
            raise RuntimeError("Source row count changed between passes")
        expected, actual = _write_streamed_netcdf(
            partial, grid, time_index, dense, invalid, footprint, series, identity, year)
        if source_identity(path) != identity["source"] or grid_fingerprint(grid_path) != identity["grid_sha256"]:
            raise RuntimeError("Source or grid changed during processing")
        unmapped = {c: stats[c] for c in counties if c not in set(weights.county_fips)}
        qa = {"identity": identity, "n_panel_rows": int(series["source_row_count"].sum()),
              "n_rows_off_time_axis": 0, "valid_mapped_rows": valid_rows,
              "invalid_reported_rows": sum(v["invalid_rows"] for v in stats.values()),
              "suppressed_reported_rows": sum(v["suppressed_rows"] for v in stats.values()),
              "nonfinite_reported_rows": sum(v["nonfinite_rows"] for v in stats.values()),
              "invalid_mapped_rows": int(series["invalid_mapped_row_count"].sum()),
              "unmapped_counties": unmapped, "n_unmapped_counties": len(unmapped),
              "unmapped_rows": sum(v["rows"] for v in unmapped.values()),
              "unmapped_valid_total": sum(v["valid_total"] for v in unmapped.values()),
              "timestamps_missing": int((series["source_row_count"] == 0).sum()),
              "missing_timestamps_utc": time_index[series["source_row_count"] == 0].astype(str).tolist(),
              "raw_total_customers_out_mapped_counties": expected,
              "gridded_total_customers_out": actual, "conservation_passed": True,
              "conservation_rtol": 1e-5, "conservation_ratio": actual / expected if expected else None,
              "valid_contribution_hidden_by_nan": float(series["valid_contribution_hidden_by_nan"].sum()),
              "output_visible_total": float(series["output_visible_total"].sum()),
              "grid_shape": [ny, nx], "zero_semantics": ZERO_SEMANTICS}
        qa_partial.write_text(json.dumps(qa, indent=2, allow_nan=False) + "\n")
        manifest_partial.write_text(json.dumps({"identity": identity, "success": True,
                                    "output": str(out.resolve()), "qa": str(qa_path.resolve()),
                                    "created": datetime.now(timezone.utc).isoformat()},
                                   indent=2, allow_nan=False) + "\n")
        # The manifest is the commit marker: interruptions leave a detectable partial set.
        os.replace(partial, out)
        os.replace(qa_partial, qa_path)
        os.replace(manifest_partial, manifest_path)
    finally:
        for array in (dense, invalid, seen):
            array.flush()
            array._mmap.close()
    for p in work:
        p.unlink()
    print(f"Wrote {out}; mapped total={actual:,.3f}; conservation passed", flush=True)
    return qa


def run(years=YEARS, **kwargs):
    return [process_year(year, **kwargs) for year in years]


# ------------------------
# Self-test (pure pandas/numpy, no geopandas/xarray required)
# ------------------------

def _self_test():
    """
    Exercise aggregate_panel_to_grid() against small synthetic inputs to
    check: (1) area weights conserve county totals exactly when a county
    spans multiple grid cells, (2) contributions from multiple counties
    sharing a grid cell sum correctly, (3) off-grid time rows are dropped
    and counted rather than silently corrupting the array shape.
    """
    time_index = pd.date_range("2024-01-01 00:00", periods=4, freq="15min")

    panel = pd.DataFrame(
        {
            "time": [
                time_index[0], time_index[0], time_index[1],
                time_index[2], "2024-01-01 09:00",  # off-axis timestamp
            ],
            "county_fips": ["00001", "00002", "00001", "00002", "00001"],
            "customers_out": [100.0, 50.0, 40.0, 10.0, 999.0],
        }
    )
    panel["time"] = pd.to_datetime(panel["time"])

    # county 00001 spans two grid cells 60/40; county 00002 sits entirely in
    # one grid cell that ALSO receives part of county 00001 (shared cell).
    weights = pd.DataFrame(
        {
            "county_fips": ["00001", "00001", "00002"],
            "grid_y": [0, 0, 0],
            "grid_x": [0, 1, 1],
            "weight": [0.6, 0.4, 1.0],
        }
    )

    out, qa = aggregate_panel_to_grid(panel, weights, time_index, ny=2, nx=2)

    assert out.shape == (4, 2, 2)
    # The off-axis row belongs to county 00001, which maps to 2 grid cells,
    # so after the county->cell join it expands to 2 sparse (time, cell) rows.
    assert qa["n_rows_off_time_axis"] == 2

    # t=0: county 00001=100 -> (0,0)+=60, (0,1)+=40 ; county 00002=50 -> (0,1)+=50
    assert np.isclose(out[0, 0, 0], 60.0)
    assert np.isclose(out[0, 0, 1], 90.0)  # 40 (from 00001) + 50 (from 00002)

    # t=1: county 00001=40 -> (0,0)+=24, (0,1)+=16
    assert np.isclose(out[1, 0, 0], 24.0)
    assert np.isclose(out[1, 0, 1], 16.0)

    # t=2: county 00002=10 -> (0,1)+=10
    assert np.isclose(out[2, 0, 1], 10.0)
    assert np.isclose(out[2, 0, 0], 0.0)

    # off-axis row (999.0 at 09:00) must NOT appear anywhere in the array
    assert np.isclose(out.sum(), 60.0 + 90.0 + 24.0 + 16.0 + 10.0)

    # conservation: raw total for mapped counties excludes the off-axis
    # timestamp only insofar as it's still counted in customers_out sum
    # (raw_total includes all panel rows for mapped counties regardless of
    # time-axis alignment -- this highlights any off-axis loss via the
    # conservation_ratio being < 1).
    expected_raw_total = panel["customers_out"].sum()  # all rows, both counties mapped
    assert np.isclose(qa["raw_total_customers_out_mapped_counties"], expected_raw_total)
    assert qa["conservation_ratio"] < 1.0  # because the 999.0 off-axis row was dropped

    print("Self-test passed: weighting, multi-county cell summation, and")
    print("off-axis-row handling all behave as expected.")
    print(f"QA: {qa}")

    # Also exercise the actual streaming engine (not just legacy behavior).
    import tempfile
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "source.csv"
        pd.DataFrame({"time": [time_index[0], time_index[0], time_index[1]],
                      "county_fips": ["01001", "01003", "01001"],
                      "customers_out": [100., 50., np.nan]}).to_csv(path, index=False)
        stream_weights = weights.copy()
        stream_weights["county_fips"] = ["01001", "01001", "01003"]
        dense = np.zeros((4, 4), dtype=np.float64)
        invalid = np.zeros((4, 4), dtype=np.uint8)
        seen = np.zeros(8, dtype=np.uint8)
        series, valid_rows = _scatter_source(
            path, 2024, 1, time_index, ["01001", "01003"], stream_weights,
            dense, invalid, seen, 2)
        np.testing.assert_allclose(dense[0], [60., 90., 0., 0.])
        np.testing.assert_array_equal(invalid[1], [1, 1, 0, 0])
        np.testing.assert_array_equal(series["source_row_count"], [2, 1, 0, 0])
        assert valid_rows == 2
        assert series["mapped_valid_total"].sum() == dense.sum() == 150
        # Re-reading must not silently double count already-seen county/time keys.
        try:
            _scatter_source(path, 2024, 1, time_index, ["01001", "01003"],
                            stream_weights, dense, invalid, seen, 2)
        except ValueError as exc:
            assert "Duplicate county/time" in str(exc)
        else:
            raise AssertionError("Streaming duplicate guard failed")
    print("Self-test passed: bounded streaming, invalid-report masks and global duplicate guard.")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--grid", type=Path, default=GRID_PATH)
    parser.add_argument("--years", nargs="+", type=int, default=YEARS)
    parser.add_argument("--eaglei-dir", type=Path, default=EAGLEI_DIR)
    parser.add_argument("--output-dir", type=Path, default=None,
                        help="Default: EAGLEI_DIR/processed/analysis_grid_CONUS_50km_v1")
    parser.add_argument("--source", choices=("raw", "panel"), default="panel",
                        help="Panel prefers Parquet, then CSV, then annual raw CSV")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--chunksize", type=int, default=250_000)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args(argv)
    if args.chunksize <= 0:
        parser.error("--chunksize must be positive")
    if args.self_test:
        _self_test()
        return
    run(args.years, grid_path=args.grid, eaglei_dir=args.eaglei_dir,
        output_dir=args.output_dir, source=args.source,
        overwrite=args.overwrite, chunksize=args.chunksize)


if __name__ == "__main__":
    main()
