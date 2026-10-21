#!/usr/bin/env python3
"""
Run the wildfire conductor-rating pipeline over every available rating hour.

This wrapper calls run_rating_pipeline.py repeatedly, overriding
analysis.rating_timestamp_utc with --rating-timestamp-utc, and then
aggregates the per-hour cases into time series tables and raster stacks.

Usage
-----
python run_rating_time_series.py --config config/pipeline.yaml
python run_rating_time_series.py --config config/pipeline.yaml --list-times
python run_rating_time_series.py --config config/pipeline.yaml --dry-run
python run_rating_time_series.py --config config/pipeline.yaml \\
       --source range --start 2024-06-26T00:00:00Z --end 2024-06-27T23:00:00Z
python run_rating_time_series.py --config config/pipeline.yaml --aggregate-only

Phases
------
A  Shared, once:      prepare_fire; prepare_conductor, rasterize_conductor,
                      conductor_elevation (time independent); fire_arrival
                      when source is arrival.
B  Per ERA5 date:     download_era5, extract_era5 (each UTC date once).
C  Per rating hour:   line_fire ... wildfire_rating (stages 8-16).
D  Aggregation:       tables, raster stacks, per-cell envelopes, manifest.

Time sources (time_series.source or --source)
---------------------------------------------
fire   Hours with a prepared fireline-intensity raster for the configured
       percentile (from the Pyretechnics manifest, or from the deck file
       names when the manifest does not exist yet). Default.
era5   Hours with extracted ERA5 rasters (air temperature) on disk.
range  start_utc .. end_utc every step_hours.
list   Explicit times_utc.
arrival Fire hours from (arrival hour - before) to (arrival hour + after),
       where the arrival hour comes from stage 5 (determine_fire_arrival.py,
       time_of_arrival at the conductor cells) and
       time_series.arrival_window_hours: [before, after].

Optional start/end bounds filter any source. Hours without ERA5 inputs are
skipped and reported, not failed, unless the date can still be downloaded
or extracted in phase B.

Outputs: <work>/time_series/<SERIES>/ (see the Output Files Reference).
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import yaml

NODATA = -9999.0
RUNNER = "run_rating_pipeline.py"

# Rating-summary statistics copied into the time series table.
SUMMARY_FIELDS = {
    "baseline": ["ampacity_a", "solar_gain_w_m", "convection_loss_w_m",
                 "thermal_margin_w_m"],
    "wildfire": ["ampacity_a", "wildfire_heat_w_m", "solar_gain_w_m",
                 "thermal_margin_w_m", "capacity_ratio",
                 "capacity_reduction_pct"],
}

# Per-hour rasters stacked into one multi-band GeoTIFF per product.
STACK_PRODUCTS = {
    "line_fireline_intensity_kw_m": ("fire", "line_fireline_intensity_kw_m.tif"),
    "fire_front_angle_deg": ("fire_geometry", "line_fire_front_angle_deg.tif"),
    "ground_offset_m": ("fire_geometry", "line_ground_offset_m.tif"),
    "fire_front_length_m": ("fire_geometry", "line_fire_front_length_m.tif"),
    "wildfire_heat_w_m": ("wildfire_heat", "total_wildfire_heat_w_m.tif"),
    "baseline_ampacity_a": ("rating/baseline", "ampacity_a_{tag}.tif"),
    "wildfire_ampacity_a": ("rating/wildfire", "ampacity_a_{tag}.tif"),
    "capacity_ratio": ("rating/wildfire", "capacity_ratio_{tag}.tif"),
    "capacity_reduction_pct": ("rating/wildfire",
                               "capacity_reduction_pct_{tag}.tif"),
}


# ----------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------

def parse_time(value) -> datetime:
    text = str(value).strip().replace("Z", "+00:00")
    stamp = datetime.fromisoformat(text)
    if stamp.tzinfo is None:
        raise ValueError(f"Time needs Z or a UTC offset: {value}")
    return stamp.astimezone(timezone.utc)


def iso(stamp: datetime) -> str:
    return stamp.strftime("%Y-%m-%dT%H:%M:%SZ")


def tag_of(stamp: datetime) -> str:
    return stamp.strftime("%Y%m%dT%H%M%SZ")


def time_from_tag(tag: str) -> datetime:
    return datetime.strptime(tag, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)


class Paths:
    """Resolve paths relative to the YAML directory (same rule as runner)."""

    def __init__(self, config_path: Path):
        self.base = config_path.parent

    def __call__(self, value):
        if value is None:
            return None
        path = Path(str(value)).expanduser()
        return (path if path.is_absolute() else self.base / path).resolve()


def parse_arguments():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--config", required=True)
    parser.add_argument("--source",
                        choices=["fire", "era5", "range", "list", "arrival"])
    parser.add_argument("--arrival-window", nargs=2, type=float,
                        metavar=("BEFORE", "AFTER"),
                        help="Hours before/after the arrival hour.")
    parser.add_argument("--start", help="Inclusive lower bound (UTC).")
    parser.add_argument("--end", help="Inclusive upper bound (UTC).")
    parser.add_argument("--step-hours", type=float)
    parser.add_argument("--times", nargs="+", help="Explicit UTC times.")
    parser.add_argument("--jobs", type=int,
                        help="Parallel hours in phase C (default 1).")
    parser.add_argument("--overwrite", action="store_true",
                        help="Recompute hours that are already complete.")
    parser.add_argument("--skip-shared", action="store_true",
                        help="Skip phase A (shared outputs already exist).")
    parser.add_argument("--aggregate-only", action="store_true",
                        help="Only rebuild phase D from existing cases.")
    parser.add_argument("--stop-on-error", action="store_true",
                        help="Abort at the first failed hour.")
    parser.add_argument("--no-stacks", action="store_true",
                        help="Skip raster stacks and envelopes.")
    parser.add_argument("--list-times", action="store_true",
                        help="Print the resolved hours and exit.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print runner commands without executing.")
    return parser.parse_args()


# ----------------------------------------------------------------------
# Settings
# ----------------------------------------------------------------------

class Settings:
    def __init__(self, args):
        self.args = args
        self.config_path = Path(args.config).expanduser().resolve()
        with self.config_path.open(encoding="utf-8") as handle:
            self.config = yaml.safe_load(handle)
        if not isinstance(self.config, dict):
            raise ValueError("The configuration root must be a mapping.")
        resolve = Paths(self.config_path)
        cfg = self.config

        self.ts = cfg.get("time_series", {}) or {}
        self.pipeline = cfg.get("pipeline", {}) or {}
        analysis = cfg.get("analysis", {}) or {}
        fire = cfg.get("pyretechnics_outputs", {}) or {}
        project = cfg.get("project", {}) or {}

        self.work = resolve(project["work_directory"])
        self.script_directory = (
            resolve(project["script_directory"])
            if project.get("script_directory")
            else Path(__file__).resolve().parent
        )
        self.percentile = int(analysis["fire_percentile"])
        self.ptag = f"p{self.percentile:03d}"
        self.fire_mode = str(
            analysis.get("fire_mode", "timestamp_matched")
        ).lower()
        self.default_time = (analysis.get("rating_timestamp_utc")
                             or fire.get("simulation_start_utc"))
        arrival_cfg = cfg.get("fire_arrival", {}) or {}
        self.arrival_json = (
            self.work / "fire_arrival"
            / f"p{int(arrival_cfg.get('percentile', self.percentile)):03d}"
            / "fire_arrival.json"
        )

        self.fire_cfg = fire
        self.fire_prepared = resolve(fire.get("output_directory"))
        self.fire_root = resolve(fire.get("root_directory"))

        pick = lambda cli, key, default=None: (
            cli if cli is not None else self.ts.get(key, default)
        )
        self.source = str(pick(args.source, "source", "fire")).lower()
        self.start = pick(args.start, "start_utc")
        self.end = pick(args.end, "end_utc")
        self.step_hours = float(pick(args.step_hours, "step_hours", 1))
        self.times = pick(args.times, "times_utc", []) or []
        window = pick(args.arrival_window, "arrival_window_hours", [0, 3])
        if len(window) != 2 or min(window) < 0:
            raise ValueError("arrival_window_hours must be [before, after] >= 0.")
        self.arrival_window = [float(window[0]), float(window[1])]
        self.jobs = int(pick(args.jobs, "jobs", 1))
        self.continue_on_error = (
            False if args.stop_on_error
            else bool(self.ts.get("continue_on_error", True))
        )
        self.write_stacks = (
            False if args.no_stacks
            else bool(self.ts.get("write_stacks", True))
        )
        self.output_root = self.work / str(
            self.ts.get("output_subdirectory", "time_series")
        )

        if self.source == "fire" and self.fire_mode == "event_maximum":
            raise ValueError(
                "time_series.source: fire needs fire_mode: timestamp_matched. "
                "With event_maximum the fire raster is fixed, so use "
                "source: era5, range, or list."
            )
        if self.step_hours <= 0:
            raise ValueError("time_series.step_hours must be positive.")
        if self.jobs < 1:
            raise ValueError("time_series.jobs must be >= 1.")

    def case_name(self, tag: str) -> str:
        return (
            f"{self.ptag}_{tag}"
            if self.fire_mode == "timestamp_matched"
            else f"{self.ptag}_eventmax_{tag}"
        )

    def case_dir(self, tag: str) -> Path:
        return self.work / "cases" / self.case_name(tag)


# ----------------------------------------------------------------------
# Time discovery
# ----------------------------------------------------------------------

def fire_times(s: Settings) -> list[datetime]:
    """Hours with fireline intensity for the configured percentile."""
    manifest = s.fire_prepared / s.fire_cfg.get(
        "manifest_filename", "pyretechnics_output_manifest.csv"
    )
    times = set()
    if manifest.is_file():
        with manifest.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                if (
                    row.get("canonical_parameter") == "flame_length"
                    and int(row["percentile"]) == s.percentile
                ):
                    times.add(parse_time(row["timestamp_utc"]))
        if times:
            return sorted(times)

    # Manifest not available yet: derive hours from deck file names with
    # the same regex and hour semantics as prepare_pyretechnics_outputs.py.
    if s.fire_root is None or not s.fire_root.is_dir():
        raise FileNotFoundError(
            "Cannot discover fire hours: no Pyretechnics manifest and "
            f"no deck directory ({s.fire_root})."
        )
    pattern = re.compile(str(s.fire_cfg["filename_regex"]).strip())
    flame_names = {
        name for name, spec in (s.fire_cfg.get("parameters") or {}).items()
        if (spec or {}).get("canonical_name") == "flame_length"
    } or {"flame-length", "flame_length"}
    start = parse_time(s.fire_cfg["simulation_start_utc"])
    semantics = s.fire_cfg.get("hour_semantics", "one_based_index")
    for path in s.fire_root.rglob("*"):
        match = pattern.match(path.name)
        if not match or match.group("parameter") not in flame_names:
            continue
        if int(match.group("percentile")) != s.percentile:
            continue
        hour = int(match.group("hour"))
        elapsed = hour - 1 if semantics == "one_based_index" else hour
        times.add(start + timedelta(hours=elapsed))
    if not times:
        raise FileNotFoundError(
            f"No flame-length rasters for percentile {s.percentile} "
            f"under {s.fire_root}."
        )
    return sorted(times)


def era5_raster_times(s: Settings) -> list[datetime]:
    folder = s.work / "era5_rasters"
    return sorted(
        time_from_tag(p.name[len("air_temperature_c_"):-4])
        for p in folder.glob("air_temperature_c_*Z.tif")
    )


def range_times(s: Settings) -> list[datetime]:
    if not (s.start and s.end):
        raise ValueError("source: range needs start_utc and end_utc.")
    first, last = parse_time(s.start), parse_time(s.end)
    step = timedelta(hours=s.step_hours)
    out, now = [], first
    while now <= last:
        out.append(now)
        now += step
    return out


def arrival_times(s: Settings) -> list[datetime]:
    """Available fire hours in a window around the stage-5 arrival hour."""
    if not s.arrival_json.is_file():
        raise FileNotFoundError(
            f"Fire-arrival summary not found: {s.arrival_json}. "
            "Run without --skip-shared, or run the fire_arrival stage.")
    summary = json.loads(s.arrival_json.read_text())
    if summary.get("rating_timestamp_utc") is None:
        raise ValueError("Fire does not reach the conductor; no arrival "
                         f"window. See {s.arrival_json}.")
    centre = parse_time(summary["rating_timestamp_utc"])
    first = centre - timedelta(hours=s.arrival_window[0])
    last = centre + timedelta(hours=s.arrival_window[1])
    if s.fire_mode == "timestamp_matched":
        return [t for t in fire_times(s) if first <= t <= last]
    out, now = [], first
    while now <= last:
        out.append(now)
        now += timedelta(hours=s.step_hours)
    return out


def resolve_times(s: Settings) -> list[datetime]:
    if s.source == "arrival":
        times = arrival_times(s)
    elif s.source == "fire":
        times = fire_times(s)
    elif s.source == "era5":
        times = era5_raster_times(s)
    elif s.source == "range":
        times = range_times(s)
    elif s.source == "list":
        if not s.times:
            raise ValueError("source: list needs times_utc or --times.")
        times = sorted({parse_time(t) for t in s.times})
    else:
        raise ValueError(f"Unknown time_series.source: {s.source}")
    if s.source not in {"range", "arrival"}:
        if s.start:
            times = [t for t in times if t >= parse_time(s.start)]
        if s.end:
            times = [t for t in times if t <= parse_time(s.end)]
    if not times:
        raise ValueError("No rating hours remain after filtering.")
    return times


def weather_status(s: Settings, stamp: datetime) -> str | None:
    """None if weather is (or will be) available, else a skip reason."""
    tag = tag_of(stamp)
    if (s.work / "era5_rasters" / f"air_temperature_c_{tag}.tif").is_file():
        return None
    nc = s.work / "era5" / f"era5_{stamp:%Y%m%d}.nc"
    if s.pipeline.get("download_era5", True):
        return None
    if s.pipeline.get("extract_era5", True) and nc.is_file():
        return None
    return "no ERA5 rasters for this hour and no download/extract enabled"


# ----------------------------------------------------------------------
# Runner calls
# ----------------------------------------------------------------------

def runner_command(s: Settings, stamp, start_at, stop_after, overwrite):
    command = [
        sys.executable, str(s.script_directory / RUNNER),
        "--config", str(s.config_path),
        "--rating-timestamp-utc", iso(stamp),
        "--start-at", start_at, "--stop-after", stop_after,
    ]
    if overwrite:
        command.append("--overwrite")
    if s.args.dry_run:
        command.append("--dry-run")
    return command


def run_logged(command, log_path: Path | None) -> int:
    print("  $ " + " ".join(command), flush=True)
    if log_path is None:
        return subprocess.run(command).returncode
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as log:
        log.write("$ " + " ".join(command) + "\n")
        log.flush()
        return subprocess.run(
            command, stdout=log, stderr=subprocess.STDOUT
        ).returncode


def hour_complete(s: Settings, tag: str) -> bool:
    return (
        s.case_dir(tag) / "rating" / "wildfire" / f"rating_summary_{tag}.json"
    ).is_file()


# ----------------------------------------------------------------------
# Aggregation
# ----------------------------------------------------------------------

def read_json(path: Path):
    return json.loads(path.read_text()) if path.is_file() else None


def aggregate_tables(s: Settings, records, out_dir: Path):
    rows, line_rows, geometry_rows = [], [], []
    for rec in records:
        tag = rec["tag"]
        case_dir = s.case_dir(tag)
        row = {
            "timestamp_utc": rec["timestamp_utc"],
            "timestamp_tag": tag,
            "case": s.case_name(tag),
            "status": rec["status"],
        }
        line_csv = case_dir / "fire" / "line_fireline_intensity.csv"
        line_max = None
        if line_csv.is_file():
            with line_csv.open(newline="", encoding="utf-8") as handle:
                for line in csv.DictReader(handle):
                    line_rows.append({"timestamp_utc": rec["timestamp_utc"],
                                      "timestamp_tag": tag, **line})
                    try:
                        value = float(line["maximum_fireline_intensity_kw_m"])
                    except (TypeError, ValueError):
                        continue
                    if np.isfinite(value):
                        line_max = value if line_max is None else max(
                            line_max, value)
        row["line_max_fireline_intensity_kw_m"] = line_max

        geometry_csv = case_dir / "fire_geometry" / "fire_geometry.csv"
        if geometry_csv.is_file():
            with geometry_csv.open(newline="", encoding="utf-8") as handle:
                for line in csv.DictReader(handle):
                    geometry_rows.append({"timestamp_utc": rec["timestamp_utc"],
                                          "timestamp_tag": tag, **line})

        for case_type, stems in SUMMARY_FIELDS.items():
            summary = read_json(case_dir / "rating" / case_type
                                / f"rating_summary_{tag}.json")
            row[f"{case_type}_valid_cells"] = (
                summary["valid_cells"] if summary else None)
            row[f"{case_type}_calm_cells"] = (
                summary["calm_cells"] if summary else None)
            for stem in stems:
                stats = (summary or {}).get("statistics", {}).get(stem, {})
                for stat in ("min", "mean", "max"):
                    row[f"{case_type}_{stem}_{stat}"] = stats.get(stat)
        rows.append(row)

    table = out_dir / "rating_time_series.csv"
    with table.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    if line_rows:
        fields = list(dict.fromkeys(k for r in line_rows for k in r))
        with (out_dir / "line_fire_time_series.csv").open(
            "w", newline="", encoding="utf-8"
        ) as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(line_rows)

    if geometry_rows:
        fields = list(dict.fromkeys(k for r in geometry_rows for k in r))
        with (out_dir / "fire_geometry_time_series.csv").open(
            "w", newline="", encoding="utf-8"
        ) as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(geometry_rows)
    return rows


def aggregate_rasters(s: Settings, records, out_dir: Path):
    """Multi-band stacks (one band per hour) and per-cell envelopes."""
    import rasterio

    done = [r for r in records if r["status"] in {"completed", "existing"}]
    if not done:
        return {}
    written = {}
    stack_dir = out_dir / "stacks"
    stack_dir.mkdir(parents=True, exist_ok=True)
    first_time = parse_time(done[0]["timestamp_utc"])
    cubes = {}

    for product, (subdir, pattern) in STACK_PRODUCTS.items():
        layers, labels, profile = [], [], None
        for rec in done:
            path = s.case_dir(rec["tag"]) / subdir / pattern.format(
                tag=rec["tag"])
            if not path.is_file():
                continue
            with rasterio.open(path) as src:
                data = src.read(1, masked=True).astype("float32")
                profile = profile or src.profile.copy()
            layers.append(data.filled(np.nan))
            labels.append(rec["timestamp_utc"])
        if not layers:
            continue
        cube = np.stack(layers)
        cubes[product] = (cube, labels, profile)
        profile.update(driver="GTiff", count=len(layers), dtype="float32",
                       nodata=NODATA, compress="deflate", tiled=False)
        for key in ("blockxsize", "blockysize", "interleave"):
            profile.pop(key, None)
        target = stack_dir / f"{product}_stack.tif"
        with rasterio.open(target, "w", **profile) as dst:
            for band, (layer, label) in enumerate(zip(cube, labels), start=1):
                dst.write(np.where(np.isfinite(layer), layer, NODATA)
                          .astype("float32"), band)
                dst.set_band_description(band, label)
            dst.update_tags(product=product, band_times_utc=",".join(labels))
        written[product] = {"path": str(target), "bands": len(labels)}

    # Envelopes: worst value per cell and when it occurred.
    def envelope(product, reducer, name):
        if product not in cubes:
            return
        cube, labels, profile = cubes[product]
        finite = np.isfinite(cube)
        any_finite = finite.any(axis=0)
        fill = np.inf if reducer == "min" else -np.inf
        work = np.where(finite, cube, fill)
        index = work.argmin(axis=0) if reducer == "min" else work.argmax(axis=0)
        value = np.take_along_axis(work, index[None], axis=0)[0]
        hours = np.array([
            (parse_time(t) - first_time).total_seconds() / 3600 for t in labels
        ], dtype="float32")[index]
        single = profile.copy()
        single.update(count=1)
        for suffix, array in ((name, value),
                              (f"{name}_hours_since_series_start", hours)):
            target = out_dir / f"{suffix}.tif"
            with rasterio.open(target, "w", **single) as dst:
                dst.write(np.where(any_finite, array, NODATA)
                          .astype("float32"), 1)
                dst.update_tags(series_start_utc=iso(first_time))
            written[suffix] = {"path": str(target)}

    envelope("wildfire_ampacity_a", "min", "minimum_wildfire_ampacity_a")
    envelope("capacity_reduction_pct", "max",
             "maximum_capacity_reduction_pct")
    envelope("line_fireline_intensity_kw_m", "max",
             "maximum_line_fireline_intensity_kw_m")
    return written


# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------

def main():
    args = parse_arguments()
    s = Settings(args)
    print(f"Configuration: {s.config_path}")
    print(f"Time source:   {s.source}   fire mode: {s.fire_mode}   "
          f"percentile: {s.percentile}")

    shared_time = parse_time(s.default_time)
    do_shared = not (args.skip_shared or args.aggregate_only
                     or args.list_times)

    # Phase A1: fire preparation first, so fire hours can be discovered.
    if do_shared and s.pipeline.get("prepare_pyretechnics_outputs", False):
        print("\n=== Phase A: prepare_fire ===")
        code = run_logged(runner_command(s, shared_time, "prepare_fire",
                                         "prepare_fire", args.overwrite), None)
        if code:
            raise SystemExit(f"prepare_fire failed (exit {code}).")

    def conductor_products():
        print("\n=== Phase A: conductor products ===")
        code = run_logged(runner_command(
            s, shared_time, "prepare_conductor", "conductor_elevation",
            args.overwrite), None)
        if code:
            raise SystemExit(f"Conductor stages failed (exit {code}).")

    # With the arrival source, conductor rasters and the arrival summary
    # are needed before the hours can be resolved.
    if s.source == "arrival" and do_shared:
        conductor_products()
        print("\n=== Phase A: fire_arrival ===")
        code = run_logged(runner_command(
            s, shared_time, "fire_arrival", "fire_arrival", True), None)
        if code:
            raise SystemExit(f"fire_arrival failed (exit {code}).")

    times = resolve_times(s)
    first_tag, last_tag = tag_of(times[0]), tag_of(times[-1])
    series = (f"{s.ptag}_{first_tag}_{last_tag}"
              if s.fire_mode == "timestamp_matched"
              else f"{s.ptag}_eventmax_{first_tag}_{last_tag}")
    out_dir = s.output_root / series
    print(f"Rating hours:  {len(times)}  ({iso(times[0])} .. {iso(times[-1])})")

    if args.list_times:
        for stamp in times:
            reason = weather_status(s, stamp)
            print(f"  {iso(stamp)}" + (f"   [skip: {reason}]" if reason else ""))
        return

    if not args.dry_run:
        out_dir.mkdir(parents=True, exist_ok=True)
    log_dir = None if args.dry_run else out_dir / "logs"

    # Phase A2: time-independent conductor products.
    if do_shared and s.source != "arrival":
        conductor_products()

    records = {tag_of(t): {"timestamp_utc": iso(t), "tag": tag_of(t),
                           "status": "pending", "detail": ""}
               for t in times}

    # Phase B: ERA5 once per UTC date.
    if not args.aggregate_only and (
        s.pipeline.get("download_era5", True)
        or s.pipeline.get("extract_era5", True)
    ):
        dates = {}
        for stamp in times:
            dates.setdefault(stamp.date(), stamp)
        print(f"\n=== Phase B: ERA5 for {len(dates)} date(s) ===")
        for day, stamp in sorted(dates.items()):
            tags_today = [tag_of(t) for t in times if t.date() == day]
            have_all = all(
                (s.work / "era5_rasters" / f"air_temperature_c_{t}.tif")
                .is_file() for t in tags_today)
            if have_all and not args.overwrite:
                print(f"  {day}: ERA5 rasters present, skipping")
                continue
            nc = s.work / "era5" / f"era5_{day:%Y%m%d}.nc"
            if not s.pipeline.get("download_era5", True) and not nc.is_file():
                print(f"  {day}: no ERA5 NetCDF and download disabled")
                for t in tags_today:
                    if not (s.work / "era5_rasters"
                            / f"air_temperature_c_{t}.tif").is_file():
                        records[t].update(
                            status="skipped",
                            detail=f"no ERA5 for {day} (download disabled)")
                continue
            code = run_logged(runner_command(
                s, stamp, "download_era5", "extract_era5", args.overwrite),
                None if log_dir is None else log_dir / f"era5_{day:%Y%m%d}.log")
            if code:
                for t in tags_today:
                    records[t].update(status="failed",
                                      detail=f"ERA5 stages failed for {day}")
                if not s.continue_on_error:
                    raise SystemExit(f"ERA5 stages failed for {day}.")

    # Phase C: per-hour stages.
    pending = []
    for stamp in times:
        rec = records[tag_of(stamp)]
        if rec["status"] in {"failed", "skipped"}:
            continue
        if args.aggregate_only:
            rec["status"] = ("existing" if hour_complete(s, rec["tag"])
                             else "missing")
            continue
        reason = None if args.dry_run else weather_status(s, stamp)
        if reason:
            rec.update(status="skipped", detail=reason)
        elif hour_complete(s, rec["tag"]) and not args.overwrite:
            rec["status"] = "existing"
        else:
            pending.append(stamp)

    def run_hour(stamp):
        tag = tag_of(stamp)
        code = run_logged(
            runner_command(s, stamp, "line_fire", "wildfire_rating",
                           args.overwrite),
            None if log_dir is None else log_dir / f"{tag}.log")
        return tag, code

    if pending:
        print(f"\n=== Phase C: {len(pending)} hour(s), jobs={s.jobs} ===")
        failed_early = False
        if s.jobs == 1:
            results = []
            for stamp in pending:
                tag, code = run_hour(stamp)
                results.append((tag, code))
                if code and not s.continue_on_error:
                    failed_early = True
                    break
        else:
            with ThreadPoolExecutor(max_workers=s.jobs) as pool:
                results = list(pool.map(run_hour, pending))
        for tag, code in results:
            if args.dry_run:
                records[tag]["status"] = "dry_run"
            elif code == 0:
                records[tag]["status"] = "completed"
            else:
                records[tag].update(
                    status="failed",
                    detail=f"exit {code}; see logs/{tag}.log")
        if failed_early:
            print("Stopped at the first failed hour (--stop-on-error).")

    ordered = [records[tag_of(t)] for t in times]
    for rec in ordered:
        print(f"  {rec['timestamp_utc']}  {rec['status']:<9} {rec['detail']}")

    if args.dry_run:
        print("\nDry run complete; no aggregation performed.")
        return

    # Phase D: aggregation.
    print(f"\n=== Phase D: aggregation -> {out_dir} ===")
    rows = aggregate_tables(s, ordered, out_dir)
    rasters = aggregate_rasters(s, ordered, out_dir) if s.write_stacks else {}

    valid = [r for r in rows
             if r.get("wildfire_ampacity_a_min") is not None]
    limiting = min(valid, key=lambda r: r["wildfire_ampacity_a_min"],
                   default=None)
    manifest = {
        "configuration": str(s.config_path),
        "series": series,
        "time_source": s.source,
        "arrival_summary": (str(s.arrival_json)
                            if s.source == "arrival" else None),
        "arrival_window_hours": (s.arrival_window
                                 if s.source == "arrival" else None),
        "fire_mode": s.fire_mode,
        "fire_percentile": s.percentile,
        "hours": ordered,
        "counts": {status: sum(r["status"] == status for r in ordered)
                   for status in {r["status"] for r in ordered}},
        "limiting_hour": None if limiting is None else {
            "timestamp_utc": limiting["timestamp_utc"],
            "case": limiting["case"],
            "wildfire_ampacity_a_min": limiting["wildfire_ampacity_a_min"],
            "capacity_reduction_pct_max":
                limiting.get("wildfire_capacity_reduction_pct_max"),
        },
        "outputs": {
            "rating_time_series": str(out_dir / "rating_time_series.csv"),
            "line_fire_time_series": str(out_dir / "line_fire_time_series.csv"),
            "fire_geometry_time_series": (
                str(out_dir / "fire_geometry_time_series.csv")
                if (out_dir / "fire_geometry_time_series.csv").is_file()
                else None
            ),
            **{k: v["path"] for k, v in rasters.items()},
        },
    }
    (out_dir / "time_series_manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8")

    print(f"Hours: {manifest['counts']}")
    if limiting:
        print(f"Limiting hour: {limiting['timestamp_utc']}  "
              f"min wildfire ampacity {limiting['wildfire_ampacity_a_min']:.1f} A")
    failed = manifest["counts"].get("failed", 0)
    if failed:
        print(f"{failed} hour(s) failed; see {out_dir / 'logs'}",
              file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as error:
        print(f"ERROR: {error}", file=sys.stderr)
        sys.exit(1)
