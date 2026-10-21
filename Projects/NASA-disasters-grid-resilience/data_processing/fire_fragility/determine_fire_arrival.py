#!/usr/bin/env python3
"""
Determine when the fire reaches the conductor and select the rating hour.

Reads the standardized Pyretechnics time_of_arrival rasters written by
prepare_pyretechnics_outputs.py (stage 1), samples them at the conductor
cells, converts arrival to UTC, and selects a rating hour from the hours
for which fire rasters exist.

Arrival map
-----------
Pyretechnics stores time of arrival in minutes on the simulation clock and
leaves unburned cells as NaN. The per-hour rasters are combined by taking
the per-cell minimum finite arrival, which is correct whether each hourly
file is a snapshot or cumulative. Negative values are treated as unburned.

Stage 1 must keep unburned cells as nodata for this parameter
(pyretechnics_outputs.parameters.time-of-arrival.fill_value: nodata).
With the default zero fill, unburned cells read as "arrival at minute 0";
this script refuses such input unless --allow-zero-filled is given.

Selection
---------
arrival_utc = reference_time + statistic(arrival over conductor cells)
   statistic: first (min, default) | median | last (max)
target      = arrival_utc + offset_hours
rating hour = available fire hour chosen by rounding:
   ceil    (default) first available hour >= target. The first hourly
           output in which the fire has reached the line.
   floor   last available hour <= target
   nearest closest available hour

Outputs
-------
--out-json    fire_arrival.json: selected rating_timestamp_utc, arrival
              statistics, per-line arrival (with --vector), inputs, method.
--out-raster  Conductor-cell arrival in hours since the reference time
              (-9999 = unburned or off-conductor).

Example
-------
python determine_fire_arrival.py \\
    --arrival-dir work/fire_prepared/standardized/time_of_arrival \\
    --percentile 99 \\
    --conductor-mask work/conductor/conductor_axis_azimuth_deg.tif \\
    --reference-time-utc 2024-06-26T00:00:00Z --units min \\
    --available-hours-manifest work/fire_prepared/pyretechnics_output_manifest.csv \\
    --vector work/conductor_input/conductors.gpkg --layer conductors \\
    --out-json work/fire_arrival/p099/fire_arrival.json \\
    --out-raster work/fire_arrival/p099/conductor_fire_arrival_hours.tif
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import rasterio
from rasterio.enums import Resampling

from raster_utils import read_to_template, write_float_raster

UNIT_MINUTES = {
    "min": 1.0, "minute": 1.0, "minutes": 1.0,
    "min_since_initialization": 1.0, "minutes_since_start": 1.0,
    "h": 60.0, "hr": 60.0, "hrs": 60.0, "hour": 60.0, "hours": 60.0,
    "hours_since_initialization": 60.0, "hours_since_start": 60.0,
    "s": 1.0 / 60.0, "second": 1.0 / 60.0, "seconds": 1.0 / 60.0,
}
TAG_PATTERN = re.compile(r"_p(?P<p>\d{3})_(?P<tag>\d{8}T\d{6}Z)\.tif$")


def parse_time(value) -> datetime:
    stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise ValueError(f"Time needs Z or a UTC offset: {value}")
    return stamp.astimezone(timezone.utc)


def iso(stamp: datetime) -> str:
    return stamp.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_arguments():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--arrival-dir", required=True, type=Path,
                        help="standardized/time_of_arrival directory.")
    parser.add_argument("--percentile", required=True, type=int)
    parser.add_argument("--conductor-mask", required=True, type=Path,
                        help="Conductor raster; finite cells = conductor.")
    parser.add_argument("--reference-time-utc", required=True,
                        help="Clock origin of time_of_arrival "
                             "(simulation_start_utc).")
    parser.add_argument("--units", default="min",
                        help="time_of_arrival units (default min).")
    parser.add_argument("--statistic", default="first",
                        choices=["first", "median", "last"])
    parser.add_argument("--rounding", default="ceil",
                        choices=["ceil", "floor", "nearest"])
    parser.add_argument("--offset-hours", type=float, default=0.0)
    parser.add_argument("--available-hours-manifest", type=Path,
                        help="Stage 1 manifest; restricts the choice to "
                             "hours with flame-length rasters.")
    parser.add_argument("--vector", type=Path,
                        help="Conductor vector for per-line arrival.")
    parser.add_argument("--layer", default="conductors")
    parser.add_argument("--line-id-field", default="line_id")
    parser.add_argument("--all-touched", action="store_true")
    parser.add_argument("--allow-zero-filled", action="store_true",
                        help="Accept rasters without nodata (zero fill).")
    parser.add_argument("--out-json", required=True, type=Path)
    parser.add_argument("--out-raster", type=Path)
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


def arrival_files(directory: Path, percentile: int):
    files = []
    for path in sorted(directory.glob("*.tif")):
        match = TAG_PATTERN.search(path.name)
        if match and int(match.group("p")) == percentile:
            files.append(path)
    if not files:
        if not directory.is_dir():
            found = "the directory does not exist"
        else:
            names = sorted(p.name for p in directory.glob("*.tif*"))
            found = ("it is empty" if not names else
                     "it contains: " + ", ".join(names[:10])
                     + (" ..." if len(names) > 10 else ""))
        raise FileNotFoundError(
            f"No time_of_arrival rasters for p{percentile:03d} in "
            f"{directory} ({found}). Expected "
            f"time_of_arrival_p{percentile:03d}_<YYYYMMDDTHHMMSSZ>.tif from "
            "stage 1. Check that (1) pipeline.prepare_pyretechnics_outputs "
            "is true (or stage 1 has been rerun) with the current "
            "prepare_pyretechnics_outputs.py, which accepts percentile-only "
            "names such as time-of-arrival_99.tif; (2) time-of-arrival is "
            "listed in pyretechnics_outputs.parameters; (3) the percentile "
            "is in pyretechnics_outputs.selected_percentiles. Stage 1 "
            "writes a 'filename-not-matched' warning for skipped files."
        )
    return files


def combined_arrival_minutes(files, template, factor, allow_zero_filled):
    """Per-cell minimum finite arrival (minutes) across all hourly files."""
    combined = None
    for path in files:
        with rasterio.open(path) as src:
            if src.nodata is None and not allow_zero_filled:
                raise ValueError(f"{path} has no nodata value.")
        values, _ = read_to_template(path, template,
                                     Resampling.nearest)
        values = np.asarray(values, dtype="float64") * factor
        values[values < 0] = np.nan
        combined = (values if combined is None
                    else np.fmin(combined, values))
    return combined


def available_fire_hours(manifest: Path | None, percentile: int):
    if manifest is None:
        return None
    hours = set()
    with manifest.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            if (row.get("canonical_parameter") == "flame_length"
                    and int(row["percentile"]) == percentile):
                hours.add(parse_time(row["timestamp_utc"]))
    if not hours:
        raise ValueError(
            f"No flame-length hours for p{percentile:03d} in {manifest}.")
    return sorted(hours)


def select_hour(target: datetime, rounding: str, available):
    if available is None:
        hour = target.replace(minute=0, second=0, microsecond=0)
        if rounding == "ceil" and hour < target:
            hour += timedelta(hours=1)
        elif rounding == "nearest" and (target - hour) >= timedelta(minutes=30):
            hour += timedelta(hours=1)
        return hour, None
    if rounding == "ceil":
        later = [h for h in available if h >= target]
        if later:
            return later[0], None
        return available[-1], ("target is after the last fire hour; "
                               "using the last available hour")
    if rounding == "floor":
        earlier = [h for h in available if h <= target]
        if earlier:
            return earlier[-1], None
        return available[0], ("target is before the first fire hour; "
                              "using the first available hour")
    return min(available, key=lambda h: abs((h - target).total_seconds())), None


def per_line_arrival(vector, layer, id_field, all_touched, template_path,
                     minutes, reference):
    import geopandas as gpd
    from rasterio.features import rasterize

    with rasterio.open(template_path) as tpl:
        crs, transform, shape = tpl.crs, tpl.transform, (tpl.height, tpl.width)
    lines = gpd.read_file(vector, layer=layer).to_crs(crs)
    records = []
    for _, row in lines.iterrows():
        mask = rasterize([(row.geometry, 1)], out_shape=shape,
                         transform=transform, fill=0,
                         all_touched=all_touched).astype(bool)
        values = minutes[mask]
        burned = values[np.isfinite(values)]
        first = float(burned.min()) if burned.size else None
        records.append({
            "line_id": str(row.get(id_field)),
            "line_cells": int(mask.sum()),
            "burned_cells": int(burned.size),
            "first_arrival_minutes": first,
            "first_arrival_utc": (None if first is None else
                                  iso(reference + timedelta(minutes=first))),
            "last_arrival_minutes": (float(burned.max())
                                     if burned.size else None),
        })
    return records


def main():
    args = parse_arguments()
    for path in (args.out_json, args.out_raster):
        if path is not None and path.exists() and not args.overwrite:
            raise FileExistsError(f"Output exists: {path}; use --overwrite.")

    unit = str(args.units).strip().lower()
    if unit not in UNIT_MINUTES:
        raise ValueError(f"Unsupported time_of_arrival units: {args.units}")
    reference = parse_time(args.reference_time_utc)

    with rasterio.open(args.conductor_mask) as src:
        conductor = np.isfinite(src.read(1, masked=True).filled(np.nan))
        profile = src.profile.copy()
    if not conductor.any():
        raise ValueError("Conductor mask has no valid cells.")

    files = arrival_files(args.arrival_dir, args.percentile)
    minutes = combined_arrival_minutes(files, args.conductor_mask,
                                       UNIT_MINUTES[unit],
                                       args.allow_zero_filled)
    on_line = np.where(conductor, minutes, np.nan)
    burned = on_line[np.isfinite(on_line)]

    if args.out_raster is not None:
        write_float_raster(args.out_raster, on_line / 60.0, profile,
                           description="fire_arrival_hours_since_reference")

    available = available_fire_hours(args.available_hours_manifest,
                                     args.percentile)
    summary = {
        "percentile": args.percentile,
        "reference_time_utc": iso(reference),
        "units": args.units,
        "method": {"statistic": args.statistic, "rounding": args.rounding,
                   "offset_hours": args.offset_hours,
                   "combination": "per-cell minimum over hourly rasters"},
        "conductor_cells": int(conductor.sum()),
        "burned_conductor_cells": int(burned.size),
        "source_rasters": [str(p) for p in files],
        "available_fire_hours": (None if available is None
                                 else [iso(h) for h in available]),
        "fire_reaches_conductor": bool(burned.size),
        "rating_timestamp_utc": None,
    }

    if burned.size:
        if burned.size == conductor.sum() and np.all(burned == 0):
            print("WARNING: every conductor cell has arrival 0. Check that "
                  "stage 1 used fill_value: nodata.", file=sys.stderr)
        value = {"first": burned.min(), "median": np.median(burned),
                 "last": burned.max()}[args.statistic]
        arrival = reference + timedelta(minutes=float(value))
        target = arrival + timedelta(hours=args.offset_hours)
        hour, note = select_hour(target, args.rounding, available)
        summary.update({
            "arrival_minutes": {
                "first": float(burned.min()),
                "median": float(np.median(burned)),
                "last": float(burned.max()),
            },
            "arrival_utc": {
                "first": iso(reference + timedelta(minutes=float(burned.min()))),
                "last": iso(reference + timedelta(minutes=float(burned.max()))),
            },
            "selected_arrival_utc": iso(arrival),
            "target_time_utc": iso(target),
            "rating_timestamp_utc": iso(hour),
            "note": note,
        })

    if args.vector is not None:
        summary["lines"] = per_line_arrival(
            args.vector, args.layer, args.line_id_field, args.all_touched,
            args.conductor_mask, minutes, reference)

    args.out_json.parent.mkdir(parents=True, exist_ok=True)
    args.out_json.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    if not burned.size:
        print(f"Fire does not reach the conductor in {len(files)} "
              f"time_of_arrival raster(s). Summary: {args.out_json}")
        sys.exit(3)
    print(f"First arrival at conductor: {summary['arrival_utc']['first']} "
          f"({summary['burned_conductor_cells']}/{summary['conductor_cells']} "
          "cells burned)")
    print(f"Selected rating hour ({args.statistic}, {args.rounding}, "
          f"offset {args.offset_hours:+g} h): "
          f"{summary['rating_timestamp_utc']}")
    if summary.get("note"):
        print(f"NOTE: {summary['note']}")
    print(f"Summary: {args.out_json}")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as error:
        print(f"ERROR: {error}", file=sys.stderr)
        sys.exit(1)
