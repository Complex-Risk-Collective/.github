#!/usr/bin/env python3
"""
Fire geometry at the conductor from Pyretechnics time of arrival.

Pipeline stage 9 (``fire_geometry``). Supplies the three geometric inputs
of the manuscript's radiative term (Eq. 15) per line, for one rating hour:

    alpha_f   acute angle between the local fire front and the conductor
              axis, degrees. 0 = front parallel to the line (fire spreading
              perpendicular to the line; manuscript case study, cos = 1).
              90 = front perpendicular to the line (spread along the line).
    y         horizontal distance along the ground between the active fire
              front at the rating hour and the conductor, m.
    L         length of the active fire front facing the line, m.

Method
------
1. Combine the standardized hourly ``time_of_arrival`` rasters (stage 1,
   ``fill_value: nodata``) into one arrival field T (per-cell minimum,
   minutes since the simulation start; negative = unburned).
2. Fit a local plane T = a + b*x + c*y over a k x k window (NaN-aware,
   least squares, x east and y north in metres). The gradient (b, c) points
   along the spread direction; |grad T|^-1 is the local spread rate.
3. Front tangent azimuth = spread azimuth + 90 deg (mod 180). alpha_f is the
   acute difference between the front tangent and the conductor axis
   azimuth from stage 3 (``conductor_axis_azimuth_deg.tif``).
4. Per line, the reference cell is the cell of maximum fireline intensity
   from stage 8 (``line_fireline_intensity.csv``), so the geometry matches
   the intensity used in the heat terms. If that cell has no valid
   gradient, the median over the line's cells is used.
5. Front length L: cells whose arrival is within +/- W/2 of the reference
   cell's arrival (the front as it crosses the line) and within the search
   radius R, projected onto the front tangent: L = extent + one cell.
6. Ground offset y (timestamp-matched mode): distance from the line's cells
   to the nearest active-front cell at the rating hour t, i.e. arrival in
   (t - W, t]. y = 0 if the active front touches a line cell. In
   event-maximum mode, or with ``--offset-mode zero``, y = 0.

Values that cannot be determined (no fire near the line, too few valid
neighbours) are replaced with the fallback scalars (normally the
``wildfire_heat`` values) and flagged in the CSV ``status`` column, so the
heat stage never receives a missing value for an unburned line.

Optional diagnostics (from the legacy script): flame reach ratio = flame
length / conductor height, and a reach flag (ratio >= 1), on conductor
cells, when ``--flame-length`` is given.

Outputs (``--out-dir``)
-----------------------
line_fire_front_angle_deg.tif   per-line alpha_f burned onto line cells
line_ground_offset_m.tif        per-line y
line_fire_front_length_m.tif    per-line L
fire_front_angle_deg.tif        per-cell alpha_f on conductor cells
fire_spread_azimuth_deg.tif     spread azimuth (deg from north), all cells
fire_spread_rate_m_min.tif      |grad T|^-1, all cells
flame_reach_ratio.tif, flame_reaches_conductor.tif   (optional)
fire_geometry.csv               one row per line
fire_geometry_summary.json      inputs, parameters, per-line records
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import rasterio
from rasterio.enums import Resampling
from scipy.ndimage import correlate, distance_transform_edt

from raster_utils import read_to_template, write_float_raster
from determine_fire_arrival import (
    UNIT_MINUTES,
    arrival_files,
    combined_arrival_minutes,
    parse_time,
)


def parse_arguments():
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--arrival-dir", required=True, type=Path,
                   help="Standardized time_of_arrival directory (stage 1).")
    p.add_argument("--percentile", required=True, type=int)
    p.add_argument("--units", default="min")
    p.add_argument("--reference-time-utc", required=True,
                   help="Clock origin of time_of_arrival "
                        "(simulation_start_utc).")
    p.add_argument("--rating-timestamp-utc", required=True)
    p.add_argument("--template", required=True, type=Path)
    p.add_argument("--conductor-axis", required=True, type=Path,
                   help="conductor_axis_azimuth_deg.tif (stage 3).")
    p.add_argument("--vector", required=True, type=Path)
    p.add_argument("--layer", default="conductors")
    p.add_argument("--line-id-field", default="line_id")
    p.add_argument("--all-touched", action="store_true")
    p.add_argument("--line-csv", type=Path,
                   help="line_fireline_intensity.csv (stage 8).")
    p.add_argument("--gradient-window-cells", type=int, default=3)
    p.add_argument("--minimum-valid-neighbours", type=int, default=6)
    p.add_argument("--front-window-minutes", type=float, default=60.0)
    p.add_argument("--search-radius-m", type=float, default=500.0)
    p.add_argument("--offset-mode", choices=["active_front", "zero"],
                   default="active_front")
    p.add_argument("--fallback-angle-deg", type=float, default=0.0)
    p.add_argument("--fallback-offset-m", type=float, default=0.0)
    p.add_argument("--fallback-length-m", type=float, default=100.0)
    p.add_argument("--flame-length", type=Path,
                   help="Flame-length raster at the rating hour (optional).")
    p.add_argument("--conductor-height", type=Path,
                   help="conductor_height_m.tif, for the reach ratio.")
    p.add_argument("--allow-zero-filled", action="store_true")
    p.add_argument("--out-dir", required=True, type=Path)
    p.add_argument("--overwrite", action="store_true")
    return p.parse_args()


# ----------------------------------------------------------------------
# Geometry helpers
# ----------------------------------------------------------------------

def cell_size_m(template: Path):
    """Return (dx, dy) cell size in metres and the transform/CRS."""
    with rasterio.open(template) as src:
        transform, crs = src.transform, src.crs
        shape = (src.height, src.width)
        bounds = src.bounds
    dx, dy = abs(transform.a), abs(transform.e)
    note = None
    if crs is not None and crs.is_geographic:
        lat = math.radians(0.5 * (bounds.top + bounds.bottom))
        dx *= 111_320.0 * math.cos(lat)
        dy *= 110_540.0
        note = ("Template CRS is geographic; cell size converted to metres "
                "at the grid's centre latitude.")
    return dx, dy, transform, crs, shape, note


def plane_fit_gradient(values, dx, dy, window, min_valid):
    """NaN-aware local least-squares plane fit.

    Returns dT/dx (east) and dT/dy (north) in value units per metre.
    """
    if window < 3 or window % 2 == 0:
        raise ValueError("--gradient-window-cells must be odd and >= 3.")
    h = window // 2
    offsets = np.arange(-h, h + 1, dtype="float64")
    kx = np.tile(offsets * dx, (window, 1))              # columns -> east
    ky = np.tile((-offsets * dy)[:, None], (1, window))  # rows -> north
    ones = np.ones((window, window))

    m = np.isfinite(values).astype("float64")
    t = np.where(m > 0, values, 0.0)

    def corr(a, k):
        return correlate(a, k, mode="constant", cval=0.0)

    n = corr(m, ones)
    sx, sy = corr(m, kx), corr(m, ky)
    sxx, syy, sxy = corr(m, kx * kx), corr(m, ky * ky), corr(m, kx * ky)
    st, stx, sty = corr(t, ones), corr(t, kx), corr(t, ky)

    # Normal equations for [a, b, c].
    A = np.stack([
        np.stack([n, sx, sy], -1),
        np.stack([sx, sxx, sxy], -1),
        np.stack([sy, sxy, syy], -1),
    ], -2)
    rhs = np.stack([st, stx, sty], -1)
    ok = (n >= min_valid) & (m > 0)
    gx = np.full(values.shape, np.nan)
    gy = np.full(values.shape, np.nan)
    det = np.linalg.det(A)
    ok &= np.abs(det) > 1e-9 * np.maximum(1.0, np.abs(A).max(axis=(-1, -2))) ** 3
    if ok.any():
        sol = np.linalg.solve(A[ok], rhs[ok][..., None])[..., 0]
        gx[ok], gy[ok] = sol[:, 1], sol[:, 2]
    return gx, gy


def acute_axis_difference(az1, az2):
    """Acute angle (0-90 deg) between two undirected axes."""
    d = np.mod(az1 - az2, 180.0)
    return np.minimum(d, 180.0 - d)


def cell_xy(rows, cols, dx, dy):
    """Local metric coordinates (east, north) of cell centres."""
    return cols * dx, -rows * dy


# ----------------------------------------------------------------------

def line_masks(vector, layer, id_field, all_touched, transform, crs, shape):
    import geopandas as gpd
    from rasterio.features import rasterize

    lines = gpd.read_file(vector, layer=layer)
    if crs is not None:
        lines = lines.to_crs(crs)
    for _, row in lines.iterrows():
        mask = rasterize([(row.geometry, 1)], out_shape=shape,
                         transform=transform, fill=0,
                         all_touched=all_touched).astype(bool)
        yield str(row.get(id_field)), mask


def read_line_csv(path: Path | None):
    if path is None or not path.is_file():
        return {}
    with path.open(newline="") as handle:
        return {row["line_id"]: row for row in csv.DictReader(handle)}


def finite_or_none(value):
    return None if value is None or not np.isfinite(value) else float(value)


def main():
    args = parse_arguments()
    out = args.out_dir
    targets = {
        "angle": out / "line_fire_front_angle_deg.tif",
        "offset": out / "line_ground_offset_m.tif",
        "length": out / "line_fire_front_length_m.tif",
    }
    if not args.overwrite and all(p.is_file() for p in targets.values()):
        print(f"Outputs exist in {out}; use --overwrite to rebuild.")
        return 0

    out.mkdir(parents=True, exist_ok=True)
    unit = str(args.units).strip().lower()
    if unit not in UNIT_MINUTES:
        raise ValueError(f"Unsupported time_of_arrival units: {args.units}")
    reference = parse_time(args.reference_time_utc)
    rating = parse_time(args.rating_timestamp_utc)
    t_rating = (rating - reference).total_seconds() / 60.0

    dx, dy, transform, crs, shape, crs_note = cell_size_m(args.template)
    files = arrival_files(args.arrival_dir, args.percentile)
    toa = combined_arrival_minutes(files, args.template, UNIT_MINUTES[unit],
                                   args.allow_zero_filled)

    axis, profile = read_to_template(args.conductor_axis, args.template,
                                     Resampling.nearest)
    axis = np.asarray(axis, dtype="float64")
    conductor = np.isfinite(axis)
    if not conductor.any():
        raise ValueError("Conductor axis raster has no valid cells.")

    # Spread field.
    gx, gy = plane_fit_gradient(toa, dx, dy, args.gradient_window_cells,
                                args.minimum_valid_neighbours)
    gmag = np.hypot(gx, gy)
    good = np.isfinite(gmag) & (gmag > 1e-9)
    spread_az = np.where(good, np.mod(np.degrees(np.arctan2(gx, gy)), 360.0),
                         np.nan)
    spread_rate = np.where(good, 1.0 / np.where(good, gmag, 1.0), np.nan)
    front_az = np.mod(spread_az + 90.0, 180.0)
    alpha_cell = np.where(conductor & good,
                          acute_axis_difference(front_az, axis), np.nan)

    # Active front at the rating hour.
    W = float(args.front_window_minutes)
    active = np.isfinite(toa) & (toa > t_rating - W) & (toa <= t_rating)

    line_rows = read_line_csv(args.line_csv)
    rr, cc = np.indices(shape)
    X, Y = cell_xy(rr, cc, dx, dy)

    angle_r = np.full(shape, np.nan)
    offset_r = np.full(shape, np.nan)
    length_r = np.full(shape, np.nan)
    records = []

    for line_id, mask in line_masks(args.vector, args.layer,
                                    args.line_id_field, args.all_touched,
                                    transform, crs, shape):
        notes = []
        rec = {"line_id": line_id, "line_cells": int(mask.sum())}
        burned = mask & np.isfinite(toa)
        rec["burned_line_cells"] = int(burned.sum())

        # Reference cell.
        ref = None
        row = line_rows.get(line_id)
        if row and row.get("status", "ok") == "ok":
            try:
                r0 = int(float(row["maximum_source_row"]))
                c0 = int(float(row["maximum_source_column"]))
                if (0 <= r0 < shape[0] and 0 <= c0 < shape[1]
                        and float(row["maximum_fireline_intensity_kw_m"]) > 0):
                    ref = (r0, c0)
            except (KeyError, ValueError, TypeError):
                pass
        if ref is None and burned.any():
            idx = np.argwhere(burned)
            ref = tuple(idx[np.argmin(toa[burned])])
            notes.append("reference = earliest-burned line cell "
                         "(no positive line maximum)")
        rec["reference_row"] = None if ref is None else int(ref[0])
        rec["reference_column"] = None if ref is None else int(ref[1])

        # Angle.
        alpha = np.nan
        tangent = None
        if ref is not None and np.isfinite(alpha_cell[ref]):
            alpha = alpha_cell[ref]
            rec["angle_source"] = "reference_cell"
        else:
            vals = alpha_cell[mask & np.isfinite(alpha_cell)]
            if vals.size:
                alpha = float(np.median(vals))
                rec["angle_source"] = "line_median"
                notes.append("angle from median of line cells")
        if ref is not None and np.isfinite(front_az[ref]):
            fa = math.radians(front_az[ref])
            tangent = (math.sin(fa), math.cos(fa))
            rec["spread_azimuth_deg"] = float(spread_az[ref])
            rec["spread_rate_m_min"] = float(spread_rate[ref])
        else:
            rec["spread_azimuth_deg"] = None
            rec["spread_rate_m_min"] = None
        rec["alpha_line_median_deg"] = finite_or_none(
            np.nanmedian(alpha_cell[mask]) if np.isfinite(alpha_cell[mask]).any()
            else np.nan)

        # Front length.
        length = np.nan
        if ref is not None and tangent is not None and np.isfinite(toa[ref]):
            t_ref = toa[ref]
            x0, y0 = X[ref], Y[ref]
            near = (np.hypot(X - x0, Y - y0) <= args.search_radius_m)
            front = near & np.isfinite(toa) & (np.abs(toa - t_ref) <= W / 2.0)
            if front.sum() >= 2:
                proj = (X[front] - x0) * tangent[0] + (Y[front] - y0) * tangent[1]
                length = float(proj.max() - proj.min() + 0.5 * (dx + dy))
                rec["front_cells"] = int(front.sum())
                if length >= 2 * args.search_radius_m:
                    notes.append("front length reached the search diameter")
        rec.setdefault("front_cells", 0)

        # Ground offset.
        offset = np.nan
        if args.offset_mode == "zero":
            offset = 0.0
            rec["offset_source"] = "zero"
        elif (active & mask).any():
            offset = 0.0
            rec["offset_source"] = "active_front_on_line"
        else:
            near_line = distance_transform_edt(~mask, sampling=(dy, dx))
            cand = active & (near_line <= args.search_radius_m)
            if cand.any():
                offset = float(near_line[cand].min())
                rec["offset_source"] = "active_front_distance"
            else:
                rec["offset_source"] = "none"
        rec["active_front_cells_in_radius"] = int(
            (active & (distance_transform_edt(~mask, sampling=(dy, dx))
                       <= args.search_radius_m)).sum())

        # Fallbacks.
        status = []
        if not np.isfinite(alpha):
            alpha = args.fallback_angle_deg
            status.append("angle_fallback")
        if not np.isfinite(length):
            length = args.fallback_length_m
            status.append("length_fallback")
        if not np.isfinite(offset):
            offset = args.fallback_offset_m
            status.append("offset_fallback")
        rec.update(
            fire_front_angle_deg=float(alpha),
            ground_offset_m=float(offset),
            fire_front_length_m=float(length),
            cos_alpha=float(math.cos(math.radians(alpha))),
            status="ok" if not status else ";".join(status),
            note="; ".join(notes),
        )
        records.append(rec)
        angle_r[mask], offset_r[mask], length_r[mask] = alpha, offset, length

    if not records:
        raise ValueError("No conductor lines found in the vector.")

    tags = {"rating_timestamp_utc": args.rating_timestamp_utc,
            "percentile": args.percentile}
    write_float_raster(targets["angle"], angle_r, profile,
                       "Fire-front to conductor angle alpha_f (deg)", tags=tags)
    write_float_raster(targets["offset"], offset_r, profile,
                       "Ground offset fire front to conductor y (m)", tags=tags)
    write_float_raster(targets["length"], length_r, profile,
                       "Active fire-front length L (m)", tags=tags)
    write_float_raster(out / "fire_front_angle_deg.tif", alpha_cell, profile,
                       "Per-cell alpha_f on conductor cells (deg)")
    write_float_raster(out / "fire_spread_azimuth_deg.tif", spread_az,
                       profile, "Spread azimuth from north (deg)")
    write_float_raster(out / "fire_spread_rate_m_min.tif", spread_rate,
                       profile, "Spread rate from |grad ToA| (m/min)")

    reach = None
    if args.flame_length and args.conductor_height:
        if args.flame_length.is_file():
            fl, _ = read_to_template(args.flame_length, args.template,
                                     Resampling.nearest)
            hgt, _ = read_to_template(args.conductor_height, args.template,
                                      Resampling.nearest)
            ratio = np.where(conductor & np.isfinite(fl) & (hgt > 0),
                             fl / hgt, np.nan)
            write_float_raster(out / "flame_reach_ratio.tif", ratio, profile,
                               "Flame length / conductor height")
            write_float_raster(out / "flame_reaches_conductor.tif",
                               np.where(np.isfinite(ratio),
                                        (ratio >= 1.0).astype(float), np.nan),
                               profile, "1 = flame length >= height")
            reach = {"maximum_flame_reach_ratio": finite_or_none(
                np.nanmax(ratio) if np.isfinite(ratio).any() else np.nan),
                "cells_reaching": int(np.nansum(ratio >= 1.0))}
        else:
            print(f"Flame-length raster not found, skipping reach ratio: "
                  f"{args.flame_length}", file=sys.stderr)

    fields = ["line_id", "line_cells", "burned_line_cells", "reference_row",
              "reference_column", "spread_azimuth_deg", "spread_rate_m_min",
              "fire_front_angle_deg", "alpha_line_median_deg", "cos_alpha",
              "angle_source", "fire_front_length_m", "front_cells",
              "ground_offset_m", "offset_source",
              "active_front_cells_in_radius", "status", "note"]
    with (out / "fire_geometry.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields,
                                extrasaction="ignore")
        writer.writeheader()
        for rec in records:
            writer.writerow(rec)

    summary = {
        "created_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "rating_timestamp_utc": args.rating_timestamp_utc,
        "reference_time_utc": args.reference_time_utc,
        "rating_minutes_since_reference": t_rating,
        "percentile": args.percentile,
        "arrival_rasters": [str(p) for p in files],
        "cell_size_m": [dx, dy],
        "parameters": {
            "gradient_window_cells": args.gradient_window_cells,
            "minimum_valid_neighbours": args.minimum_valid_neighbours,
            "front_window_minutes": W,
            "search_radius_m": args.search_radius_m,
            "offset_mode": args.offset_mode,
            "fallback_angle_deg": args.fallback_angle_deg,
            "fallback_offset_m": args.fallback_offset_m,
            "fallback_length_m": args.fallback_length_m,
        },
        "angle_definition": ("acute angle between fire front and conductor "
                             "axis; 0 = front parallel to line"),
        "flame_reach": reach,
        "notes": [n for n in [crs_note] if n],
        "lines": records,
    }
    (out / "fire_geometry_summary.json").write_text(
        json.dumps(summary, indent=2))
    for rec in records:
        print(f"{rec['line_id']}: alpha_f={rec['fire_front_angle_deg']:.1f} deg"
              f"  y={rec['ground_offset_m']:.0f} m"
              f"  L={rec['fire_front_length_m']:.0f} m  [{rec['status']}]",
              flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
