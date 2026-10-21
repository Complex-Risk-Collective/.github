#!/usr/bin/env python3
"""
Run the wildfire conductor-rating pipeline from one YAML configuration.

Usage
-----
python run_rating_pipeline.py --config config/pipeline.yaml
python run_rating_pipeline.py --config config/pipeline.yaml --dry-run
python run_rating_pipeline.py --config ... --start-at wind --stop-after solar_gain
python run_rating_pipeline.py --config ... --rating-timestamp-utc 2024-06-26T03:00:00Z

For many rating hours, use run_rating_time_series.py, which calls this
runner once per hour with --rating-timestamp-utc.

Stages (in order)
-----------------
Time independent (run before the rating hour is known):
 1 prepare_fire          prepare_pyretechnics_outputs.py
 2 prepare_conductor     prepare_conductor_input.py
 3 rasterize_conductor   rasterize_conductor_parameters.py
 4 conductor_elevation   prepare_conductor_elevation.py
 5 fire_arrival          determine_fire_arrival.py
                         (only when analysis.rating_time_source: fire_arrival)
Rating-hour dependent:
 6 download_era5         download_era5.py
 7 extract_era5          extract_era5_rasters.py
 8 line_fire             compute_line_fire_max.py
 9 fire_geometry         compute_fire_conductor_angle.py
                         (fire-front angle, ground offset, front length
                         from time_of_arrival; when fire_geometry.enabled)
10 wildfire_heat         compute_wildfire_heat_gain.py
11 wind                  compute_wind_at_height.py
12 wind_angle            compute_wind_relative_angle.py
13 solar_geometry        compute_solar_incidence.py
14 solar_gain            compute_era5_solar_heat_gain.py
15 baseline_rating       compute_ieee738_rating.py (no fire, with solar)
16 wildfire_rating       compute_ieee738_rating.py (fire, solar per config)

Rating hour
-----------
Precedence: --rating-timestamp-utc > analysis.rating_time_source:
fire_arrival (hour selected in stage 5 from time_of_arrival at the
conductor cells) > analysis.rating_timestamp_utc.

Paths
-----
Relative paths in the YAML are resolved against the YAML's directory.
Scripts are located beside this runner unless project.script_directory
is set. All derived products are written beneath project.work_directory:

  <work>/era5/era5_<YYYYMMDD>.nc
  <work>/era5_rasters/<variable>_<TAG>.tif
  <work>/conductor/                       conductor rasters + elevation
  <work>/fire_arrival/p<PPP>/             fire_arrival.json + arrival raster
  <work>/met/<TAG>/                       wind, wind angle, solar
  <work>/cases/<CASE>/fire/               line fire intensity
  <work>/cases/<CASE>/fire_geometry/       alpha_f, y, L per line
  <work>/cases/<CASE>/wildfire_heat/
  <work>/cases/<CASE>/rating/baseline/
  <work>/cases/<CASE>/rating/wildfire/
  <work>/cases/<CASE>/resolved_config.yaml
  <work>/cases/<CASE>/rating_pipeline_manifest.json

TAG  = rating timestamp, e.g. 20240626T000000Z
CASE = p<percentile>_<TAG> or p<percentile>_eventmax_<TAG>
"""

from __future__ import annotations

import argparse
import copy
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml


STAGES = [
    "prepare_fire",
    "prepare_conductor",
    "rasterize_conductor",
    "conductor_elevation",
    "fire_arrival",
    "download_era5",
    "extract_era5",
    "line_fire",
    "fire_geometry",
    "wildfire_heat",
    "wind",
    "wind_angle",
    "solar_geometry",
    "solar_gain",
    "baseline_rating",
    "wildfire_rating",
]


# ----------------------------------------------------------------------
# Configuration helpers
# ----------------------------------------------------------------------

def parse_arguments():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--config", required=True)
    parser.add_argument("--start-at", choices=STAGES)
    parser.add_argument("--stop-after", choices=STAGES)
    parser.add_argument(
        "--rating-timestamp-utc",
        help="Override analysis.rating_timestamp_utc (ISO 8601, UTC).",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace existing outputs.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print commands without running them.",
    )
    return parser.parse_args()


def load_config(path):
    config_path = Path(path).expanduser().resolve()
    if not config_path.is_file():
        raise FileNotFoundError(f"Configuration not found: {config_path}")
    with config_path.open(encoding="utf-8") as source:
        config = yaml.safe_load(source)
    if not isinstance(config, dict):
        raise ValueError("The configuration root must be a mapping.")
    return config, config_path


def section(config, key):
    value = config.get(key)
    if not isinstance(value, dict):
        raise KeyError(f"Configuration section {key!r} is required.")
    return value


def required(mapping, key, context):
    value = mapping.get(key)
    if value is None or (
        isinstance(value, str)
        and (not value.strip() or "REQUIRED" in value.upper())
    ):
        raise ValueError(f"{context}.{key} must be populated.")
    return value


def is_number(value):
    if isinstance(value, bool):
        return False
    try:
        float(value)
        return True
    except (TypeError, ValueError):
        return False


class Paths:
    """Resolve configuration paths relative to the YAML directory."""

    def __init__(self, config_path):
        self.base = config_path.parent

    def __call__(self, value):
        if value is None:
            return None
        path = Path(str(value)).expanduser()
        return (path if path.is_absolute() else self.base / path).resolve()


def parse_timestamp(value):
    timestamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if timestamp.tzinfo is None:
        raise ValueError(
            "analysis.rating_timestamp_utc needs Z or a UTC offset."
        )
    return timestamp.astimezone(timezone.utc)


def iso(timestamp):
    return timestamp.strftime("%Y-%m-%dT%H:%M:%SZ")


# ----------------------------------------------------------------------
# Execution helpers
# ----------------------------------------------------------------------

class Runner:
    def __init__(self, args, script_directory):
        self.args = args
        self.script_directory = script_directory
        start = STAGES.index(args.start_at) if args.start_at else 0
        stop = (
            STAGES.index(args.stop_after)
            if args.stop_after
            else len(STAGES) - 1
        )
        if start > stop:
            raise ValueError("--start-at is after --stop-after.")
        self.selected = set(STAGES[start:stop + 1])
        self.executed = []

    def enabled(self, stage):
        return stage in self.selected

    def script(self, filename):
        path = self.script_directory / filename
        if not path.is_file():
            raise FileNotFoundError(f"Script not found: {path}")
        return path

    def run(self, stage, filename, *arguments, overwrite_flag=True):
        command = [sys.executable, self.script(filename)]
        command.extend(arguments)
        if overwrite_flag and self.args.overwrite:
            command.append("--overwrite")
        command = [str(value) for value in command]

        print(f"\n[{stage}]", flush=True)
        print(" ".join(command), flush=True)
        self.executed.append({"stage": stage, "command": command})

        if not self.args.dry_run:
            subprocess.run(command, check=True)

    def need(self, path, description):
        """Require an input file unless this is a dry run."""
        if not self.args.dry_run and not Path(path).is_file():
            raise FileNotFoundError(f"{description} not found: {path}")
        return path


# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------

def main():
    args = parse_arguments()
    config, config_path = load_config(args.config)
    resolve = Paths(config_path)

    project = section(config, "project")
    grid = section(config, "grid")
    pipeline = config.get("pipeline", {}) or {}
    analysis = section(config, "analysis")
    era5 = section(config, "era5")
    conductors = section(config, "conductors")
    elevation_cfg = section(config, "conductor_elevation")
    wildfire = section(config, "wildfire_heat")
    wind = section(config, "wind")
    solar = section(config, "solar")
    rating = section(config, "rating")
    fire_outputs = section(config, "pyretechnics_outputs")

    script_directory = (
        resolve(project["script_directory"])
        if project.get("script_directory")
        else Path(__file__).resolve().parent
    )
    runner = Runner(args, script_directory)

    work = resolve(required(project, "work_directory", "project"))
    template = resolve(required(grid, "template_raster", "grid"))
    runner.need(template, "Template raster")

    # --- Case identity (time independent) -----------------------------
    percentile = int(required(analysis, "fire_percentile", "analysis"))
    if not 0 <= percentile <= 100:
        raise ValueError("analysis.fire_percentile must be 0-100.")
    ptag = f"p{percentile:03d}"

    fire_mode = str(analysis.get("fire_mode", "timestamp_matched")).lower()
    if fire_mode not in {"timestamp_matched", "event_maximum"}:
        raise ValueError(
            "analysis.fire_mode must be timestamp_matched or event_maximum."
        )

    time_source = str(analysis.get("rating_time_source", "fixed")).lower()
    if time_source not in {"fixed", "fire_arrival"}:
        raise ValueError(
            "analysis.rating_time_source must be fixed or fire_arrival."
        )

    # --- Time-independent paths ---------------------------------------
    fire_prepared = resolve(
        required(fire_outputs, "output_directory", "pyretechnics_outputs")
    )
    conductor_dir = work / "conductor"
    arrival_dir = work / "fire_arrival" / ptag
    arrival_json = arrival_dir / "fire_arrival.json"
    arrival_raster = arrival_dir / "conductor_fire_arrival_hours.tif"
    if not args.dry_run:
        conductor_dir.mkdir(parents=True, exist_ok=True)

    prepared_vector = resolve(
        required(conductors, "prepared_vector", "conductors")
    )
    cond = {
        name: conductor_dir / f"{name}.tif"
        for name in (
            "conductor_axis_azimuth_deg", "conductor_height_m",
            "conductor_diameter_m", "solar_absorptivity", "emissivity",
        )
    }
    elevation_raster = conductor_dir / "conductor_elevation_msl_m.tif"
    elevation_summary = conductor_dir / "conductor_elevation_summary.json"

    # 1. Prepare Pyretechnics outputs ----------------------------------
    if runner.enabled("prepare_fire") and pipeline.get(
        "prepare_pyretechnics_outputs", False
    ):
        runner.run(
            "prepare_fire", "prepare_pyretechnics_outputs.py",
            "--config", config_path,
        )

    # 2. Prepare conductor vector --------------------------------------
    if runner.enabled("prepare_conductor"):
        runner.run(
            "prepare_conductor", "prepare_conductor_input.py",
            "--config", config_path,
            "--template", template,
        )

    # 3. Rasterize conductor properties --------------------------------
    if runner.enabled("rasterize_conductor"):
        defaults = conductors.get("defaults", {}) or {}
        arguments = [
            runner.need(prepared_vector, "Prepared conductor vector"),
            template,
            "--layer", conductors.get("prepared_layer", "conductors"),
            "--out-dir", conductor_dir,
        ]
        for flag, key in (
            ("--default-absorptivity", "solar_absorptivity"),
            ("--default-emissivity", "emissivity"),
            ("--default-diameter-m", "diameter_m"),
            ("--default-height-m", "height_m"),
        ):
            if defaults.get(key) is not None:
                arguments.extend([flag, defaults[key]])
        if conductors.get("all_touched", True):
            arguments.append("--all-touched")
        runner.run(
            "rasterize_conductor", "rasterize_conductor_parameters.py",
            *arguments,
        )

    # 4. Conductor elevation -------------------------------------------
    elevation_source = str(elevation_cfg.get("source", "dem")).lower()
    use_as = str(elevation_cfg.get("use_as", "raster")).lower()
    if elevation_source not in {"dem", "value"}:
        raise ValueError("conductor_elevation.source must be dem or value.")
    if use_as not in {"raster", "scalar"}:
        raise ValueError("conductor_elevation.use_as must be raster/scalar.")

    if elevation_source == "dem" and runner.enabled("conductor_elevation"):
        arguments = [
            runner.need(
                resolve(required(elevation_cfg, "dem", "conductor_elevation")),
                "DEM",
            ),
            runner.need(cond["conductor_height_m"], "Conductor height raster"),
            "--vertical-units",
            required(elevation_cfg, "dem_vertical_units", "conductor_elevation"),
            "--vertical-datum",
            required(elevation_cfg, "dem_vertical_datum", "conductor_elevation"),
            "--resampling", elevation_cfg.get("resampling", "bilinear"),
            "--scalar-statistic",
            elevation_cfg.get("scalar_statistic", "mean"),
            "--output", elevation_raster,
            "--summary-json", elevation_summary,
        ]
        if elevation_cfg.get("allow_partial_coverage", False):
            arguments.append("--allow-partial-coverage")
        runner.run(
            "conductor_elevation", "prepare_conductor_elevation.py",
            *arguments,
        )

    def elevation_argument():
        """Value passed to --elevation-m for both rating runs."""
        if elevation_source == "value":
            value = required(elevation_cfg, "value_m", "conductor_elevation")
            if not is_number(value):
                raise ValueError("conductor_elevation.value_m must be numeric.")
            return float(value)
        if use_as == "raster":
            return runner.need(elevation_raster, "Conductor elevation raster")
        if args.dry_run and not elevation_summary.is_file():
            return "<scalar from conductor_elevation_summary.json>"
        summary = json.loads(
            runner.need(elevation_summary, "Elevation summary").read_text()
        )
        return float(summary["scalar_elevation_msl_m"])

    # 5. Fire arrival at the conductor (optional rating-time source) ----
    arrival_cfg = config.get("fire_arrival", {}) or {}
    # Runs when the YAML selects fire_arrival, or when requested
    # explicitly with --start-at fire_arrival (used by the time-series
    # wrapper's arrival source).
    if runner.enabled("fire_arrival") and (
        (time_source == "fire_arrival" and not args.rating_timestamp_utc)
        or args.start_at == "fire_arrival"
    ):
        toa_name = arrival_cfg.get("canonical_name", "time_of_arrival")
        toa_units = arrival_cfg.get("units")
        if toa_units is None:
            for spec in (fire_outputs.get("parameters") or {}).values():
                if (spec or {}).get("canonical_name") == toa_name:
                    toa_units = spec.get("output_units", "min")
        arguments = [
            "--arrival-dir", fire_prepared / "standardized" / toa_name,
            "--percentile", arrival_cfg.get("percentile", percentile),
            "--conductor-mask",
            runner.need(cond["conductor_axis_azimuth_deg"], "Conductor axis"),
            "--reference-time-utc",
            arrival_cfg.get(
                "reference_time_utc",
                required(fire_outputs, "simulation_start_utc",
                         "pyretechnics_outputs"),
            ),
            "--units", toa_units or "min",
            "--statistic", arrival_cfg.get("statistic", "first"),
            "--rounding", arrival_cfg.get("rounding", "ceil"),
            "--offset-hours", arrival_cfg.get("offset_hours", 0),
            "--available-hours-manifest",
            fire_prepared / fire_outputs.get(
                "manifest_filename", "pyretechnics_output_manifest.csv"),
            "--vector", prepared_vector,
            "--layer", conductors.get("prepared_layer", "conductors"),
            "--line-id-field", conductors.get("line_id_field", "line_id"),
            "--out-json", arrival_json,
            "--out-raster", arrival_raster,
        ]
        if conductors.get("all_touched", True):
            arguments.append("--all-touched")
        runner.run("fire_arrival", "determine_fire_arrival.py", *arguments)

    # --- Resolve the rating timestamp ---------------------------------
    # Precedence: --rating-timestamp-utc > fire_arrival > fixed value.
    if args.rating_timestamp_utc:
        timestamp_origin = "command_line"
        analysis["rating_timestamp_utc"] = args.rating_timestamp_utc
    elif time_source == "fire_arrival":
        timestamp_origin = f"fire_arrival ({arrival_json})"
        if arrival_json.is_file():
            selected = json.loads(arrival_json.read_text()).get(
                "rating_timestamp_utc"
            )
            if selected is None:
                raise ValueError(
                    "Fire does not reach the conductor; no arrival-based "
                    f"rating hour. See {arrival_json}."
                )
            analysis["rating_timestamp_utc"] = selected
        elif args.dry_run:
            timestamp_origin += " [dry run: placeholder from YAML]"
        else:
            raise FileNotFoundError(
                f"Fire-arrival summary not found: {arrival_json}. "
                "Run the fire_arrival stage first."
            )
    else:
        timestamp_origin = "analysis.rating_timestamp_utc"
    timestamp = parse_timestamp(
        required(analysis, "rating_timestamp_utc", "analysis")
    )
    analysis["rating_timestamp_utc"] = iso(timestamp)
    tag = timestamp.strftime("%Y%m%dT%H%M%SZ")
    print(f"\nRating timestamp: {iso(timestamp)}  (from {timestamp_origin})",
          flush=True)

    case = (
        f"{ptag}_{tag}"
        if fire_mode == "timestamp_matched"
        else f"{ptag}_eventmax_{tag}"
    )

    # --- Time-dependent paths -----------------------------------------
    fire_intensity = (
        fire_prepared / "derived" / "fireline_intensity"
        / f"fireline_intensity_kw_m_{ptag}_{tag}.tif"
        if fire_mode == "timestamp_matched"
        else fire_prepared / "event_maximum"
        / f"maximum_fireline_intensity_kw_m_{ptag}.tif"
    )

    era5_nc = work / "era5" / f"era5_{timestamp:%Y%m%d}.nc"
    era5_dir = work / "era5_rasters"
    met_dir = work / "met" / tag
    case_dir = work / "cases" / case
    fire_dir = case_dir / "fire"
    heat_dir = case_dir / "wildfire_heat"
    geometry_dir = case_dir / "fire_geometry"
    baseline_dir = case_dir / "rating" / "baseline"
    wildfire_dir = case_dir / "rating" / "wildfire"

    if not args.dry_run:
        for directory in (
            era5_nc.parent, era5_dir, met_dir,
            fire_dir, heat_dir, baseline_dir, wildfire_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)

    era5_raster = {
        name: era5_dir / f"{name}_{tag}.tif"
        for name in (
            "air_temperature_c", "u10_m_s", "v10_m_s",
            "ssrd_w_m2", "direct_horizontal_w_m2",
        )
    }

    line_raster = fire_dir / "line_fireline_intensity_kw_m.tif"
    line_csv = fire_dir / "line_fireline_intensity.csv"
    heat_conv = heat_dir / "wildfire_convective_heat_w_m.tif"
    heat_rad = heat_dir / "wildfire_radiative_heat_w_m.tif"

    u_cond = met_dir / f"u_conductor_m_s_{tag}.tif"
    v_cond = met_dir / f"v_conductor_m_s_{tag}.tif"
    speed_cond = met_dir / f"wind_speed_conductor_m_s_{tag}.tif"
    wind_angle = met_dir / f"wind_relative_angle_deg_{tag}.tif"
    solar_angle = met_dir / f"solar_incidence_angle_deg_{tag}.tif"
    solar_elev = met_dir / f"solar_elevation_deg_{tag}.tif"
    solar_azim = met_dir / f"solar_azimuth_deg_{tag}.tif"
    solar_gain = met_dir / f"solar_heat_gain_w_m_{tag}.tif"
    baseline_ampacity = baseline_dir / f"ampacity_a_{tag}.tif"

    # 6. Download ERA5 -------------------------------------------------
    if runner.enabled("download_era5") and pipeline.get(
        "download_era5", True
    ):
        bbox = section(era5, "bbox")
        arguments = [
            "--date", f"{timestamp:%Y-%m-%d}",
            "--bbox",
            *(required(bbox, k, "era5.bbox")
              for k in ("north", "west", "south", "east")),
            "--output", era5_nc,
            "--compression-level", era5.get("compression_level", 4),
        ]
        if era5.get("keep_grib", False):
            arguments.append("--keep-grib")
        runner.run("download_era5", "download_era5.py", *arguments)

    # 7. Extract ERA5 rasters ------------------------------------------
    if runner.enabled("extract_era5") and pipeline.get("extract_era5", True):
        runner.run(
            "extract_era5", "extract_era5_rasters.py",
            runner.need(era5_nc, "ERA5 NetCDF"),
            "--out-dir", era5_dir,
        )

    # 8. Line fire intensity -------------------------------------------
    if runner.enabled("line_fire"):
        arguments = [
            runner.need(fire_intensity, "Selected fireline-intensity raster"),
            runner.need(prepared_vector, "Prepared conductor vector"),
            "--layer", conductors.get("prepared_layer", "conductors"),
            "--line-id-field", conductors.get("line_id_field", "line_id"),
            "--out-raster", line_raster,
            "--out-csv", line_csv,
        ]
        if conductors.get("center_only_for_fire_intersection", False):
            arguments.append("--center-only")
        runner.run("line_fire", "compute_line_fire_max.py", *arguments)

    # 9. Fire geometry at the line ------------------------------------
    geometry_cfg = config.get("fire_geometry", {}) or {}
    geometry_enabled = bool(geometry_cfg.get("enabled", False))
    apply_geometry = geometry_cfg.get("apply_to_heat", {}) or {}
    geometry = {
        "fire_normal_angle_deg": geometry_dir / "line_fire_front_angle_deg.tif",
        "ground_offset_m": geometry_dir / "line_ground_offset_m.tif",
        "fire_front_length_m": geometry_dir / "line_fire_front_length_m.tif",
    }
    if geometry_enabled and runner.enabled("fire_geometry"):
        toa_name = geometry_cfg.get(
            "canonical_name", arrival_cfg.get("canonical_name",
                                              "time_of_arrival"))
        toa_units = geometry_cfg.get("units", arrival_cfg.get("units"))
        if toa_units is None:
            for spec in (fire_outputs.get("parameters") or {}).values():
                if (spec or {}).get("canonical_name") == toa_name:
                    toa_units = spec.get("output_units", "min")
        offset_mode = geometry_cfg.get(
            "offset_mode",
            "zero" if fire_mode == "event_maximum" else "active_front",
        )
        flame_length = (
            fire_prepared / "standardized" / "flame_length"
            / f"flame_length_{ptag}_{tag}.tif"
        )
        arguments = [
            "--arrival-dir", fire_prepared / "standardized" / toa_name,
            "--percentile", geometry_cfg.get(
                "percentile", arrival_cfg.get("percentile", percentile)),
            "--units", toa_units or "min",
            "--reference-time-utc", arrival_cfg.get(
                "reference_time_utc",
                required(fire_outputs, "simulation_start_utc",
                         "pyretechnics_outputs"),
            ),
            "--rating-timestamp-utc", iso(timestamp),
            "--template", template,
            "--conductor-axis",
            runner.need(cond["conductor_axis_azimuth_deg"], "Conductor axis"),
            "--vector", runner.need(prepared_vector, "Prepared conductor vector"),
            "--layer", conductors.get("prepared_layer", "conductors"),
            "--line-id-field", conductors.get("line_id_field", "line_id"),
            "--line-csv", line_csv,
            "--gradient-window-cells",
            geometry_cfg.get("gradient_window_cells", 3),
            "--minimum-valid-neighbours",
            geometry_cfg.get("minimum_valid_neighbours", 6),
            "--front-window-minutes",
            geometry_cfg.get("front_window_minutes", 60),
            "--search-radius-m", geometry_cfg.get("search_radius_m", 500),
            "--offset-mode", offset_mode,
            "--fallback-angle-deg",
            wildfire.get("fire_normal_angle_deg", 0.0),
            "--fallback-offset-m", wildfire.get("ground_offset_m", 0.0),
            "--fallback-length-m", wildfire.get("fire_front_length_m", 100.0),
            "--conductor-height", cond["conductor_height_m"],
            "--flame-length", flame_length,
            "--out-dir", geometry_dir,
            "--overwrite",
        ]
        if not conductors.get("center_only_for_fire_intersection", False):
            arguments.append("--all-touched")
        runner.run(
            "fire_geometry", "compute_fire_conductor_angle.py", *arguments,
            overwrite_flag=False,
        )

    # 10. Wildfire heat gain --------------------------------------------
    if runner.enabled("wildfire_heat"):
        arguments = [
            runner.need(line_raster, "Line fire-intensity raster"),
            cond["conductor_height_m"],
            cond["conductor_diameter_m"],
        ]
        for flag, key in (
            ("--convective-coefficient", "convective_coefficient"),
            ("--radiative-fraction", "radiative_fraction"),
            ("--fire-front-length-m", "fire_front_length_m"),
            ("--ground-offset-m", "ground_offset_m"),
            ("--fire-normal-angle-deg", "fire_normal_angle_deg"),
        ):
            value = required(wildfire, key, "wildfire_heat")
            if geometry_enabled and key in geometry and apply_geometry.get(
                key, True
            ):
                value = runner.need(geometry[key], f"Fire geometry ({key})")
            arguments.extend([flag, value])
        arguments.extend(["--out-dir", heat_dir])
        runner.run(
            "wildfire_heat", "compute_wildfire_heat_gain.py", *arguments,
            overwrite_flag=False,
        )

    # 11. Wind at conductor height --------------------------------------
    method = wind.get("method", "power")
    if runner.enabled("wind"):
        arguments = [
            runner.need(era5_raster["u10_m_s"], "ERA5 u10"),
            runner.need(era5_raster["v10_m_s"], "ERA5 v10"),
            runner.need(cond["conductor_height_m"], "Conductor height raster"),
            "--method", method,
            "--out-u", u_cond,
            "--out-v", v_cond,
            "--out-speed", speed_cond,
        ]
        if method == "power":
            arguments.extend(
                ["--power-exponent", required(wind, "power_exponent", "wind")]
            )
        else:
            roughness = required(wind, "roughness_length_m", "wind")
            arguments.extend([
                "--roughness-length",
                roughness if is_number(roughness) else resolve(roughness),
            ])
        runner.run(
            "wind", "compute_wind_at_height.py", *arguments,
            overwrite_flag=False,
        )

    # 12. Wind angle relative to conductor -----------------------------
    if runner.enabled("wind_angle"):
        runner.run(
            "wind_angle", "compute_wind_relative_angle.py",
            u_cond, v_cond, cond["conductor_axis_azimuth_deg"],
            "--calm-threshold", wind.get("calm_threshold_m_s", 0.1),
            "--output", wind_angle,
        )

    # 13. Solar geometry -----------------------------------------------
    # ERA5 radiation at validity time T is the mean over (T-1h, T]; by
    # default the sun position is evaluated at the window midpoint.
    offset = float(solar.get("geometry_time_offset_minutes", -30))
    solar_time = timestamp + timedelta(minutes=offset)
    if runner.enabled("solar_geometry"):
        runner.run(
            "solar_geometry", "compute_solar_incidence.py",
            runner.need(cond["conductor_axis_azimuth_deg"], "Conductor axis"),
            "--time", iso(solar_time),
            "--out-angle", solar_angle,
            "--out-elevation", solar_elev,
            "--out-azimuth", solar_azim,
            overwrite_flag=False,
        )

    # 14. Solar heat gain ----------------------------------------------
    if runner.enabled("solar_gain"):
        arguments = [
            runner.need(era5_raster["ssrd_w_m2"], "ERA5 GHI"),
            runner.need(
                era5_raster["direct_horizontal_w_m2"], "ERA5 direct horizontal"
            ),
            solar_elev,
            solar_angle,
            cond["solar_absorptivity"],
            cond["conductor_diameter_m"],
            "--diffuse-model", solar.get("diffuse_model", "isotropic-cylinder"),
            "--minimum-solar-elevation-deg",
            solar.get("minimum_solar_elevation_deg", 3.0),
            "--timestamp-tag", tag,
            "--out-dir", met_dir,
        ]
        if solar.get("max_dni_w_m2") is not None:
            arguments.extend(["--max-dni-w-m2", solar["max_dni_w_m2"]])
        runner.run(
            "solar_gain", "compute_era5_solar_heat_gain.py", *arguments,
            overwrite_flag=False,
        )

    # 14-15. Ratings ---------------------------------------------------
    resistance = section(rating, "resistance")

    def rating_arguments(out_dir):
        arguments = [
            runner.need(era5_raster["air_temperature_c"], "ERA5 temperature"),
            runner.need(speed_cond, "Conductor wind speed"),
            runner.need(wind_angle, "Wind relative angle"),
            runner.need(cond["conductor_diameter_m"], "Conductor diameter"),
            "--template", template,
            "--surface-temperature-c",
            required(rating, "maximum_surface_temperature_c", "rating"),
            "--elevation-m", elevation_argument(),
            "--emissivity", runner.need(cond["emissivity"], "Emissivity"),
            "--calm-wind-policy", wind.get("calm_wind_policy", "natural"),
        ]
        if rating.get("average_strand_temperature_c") is not None:
            arguments.extend([
                "--average-temperature-c",
                rating["average_strand_temperature_c"],
            ])
        for flag, key in (
            ("--resistance-low-ohm-m", "low_resistance_ohm_m"),
            ("--temperature-low-c", "low_temperature_c"),
            ("--resistance-high-ohm-m", "high_resistance_ohm_m"),
            ("--temperature-high-c", "high_temperature_c"),
        ):
            arguments.extend(
                [flag, required(resistance, key, "rating.resistance")]
            )
        arguments.extend(["--timestamp-tag", tag, "--out-dir", out_dir])
        return arguments

    if runner.enabled("baseline_rating"):
        runner.run(
            "baseline_rating", "compute_ieee738_rating.py",
            *rating_arguments(baseline_dir),
            "--solar-gain-w-m", runner.need(solar_gain, "Solar heat gain"),
            overwrite_flag=False,
        )

    wildfire_solar = solar.get("wildfire_case", "off")
    if isinstance(wildfire_solar, bool):  # YAML parses bare off/on
        wildfire_solar = "on" if wildfire_solar else "off"
    wildfire_solar = str(wildfire_solar).lower()
    if wildfire_solar not in {"off", "on"}:
        raise ValueError("solar.wildfire_case must be off or on.")

    if runner.enabled("wildfire_rating"):
        solar_arguments = (
            ["--solar-off"]
            if wildfire_solar == "off"
            else ["--solar-gain-w-m", runner.need(solar_gain, "Solar gain")]
        )
        runner.run(
            "wildfire_rating", "compute_ieee738_rating.py",
            *rating_arguments(wildfire_dir),
            *solar_arguments,
            "--wildfire-convective-w-m",
            runner.need(heat_conv, "Wildfire convective heat"),
            "--wildfire-radiative-w-m",
            runner.need(heat_rad, "Wildfire radiative heat"),
            "--baseline-ampacity",
            runner.need(baseline_ampacity, "Baseline ampacity"),
            overwrite_flag=False,
        )

    # --- Provenance ----------------------------------------------------
    resolved = copy.deepcopy(config)
    try:
        resolved["conductor_elevation"]["resolved_elevation_m"] = (
            str(elevation_argument())
        )
    except FileNotFoundError:
        resolved["conductor_elevation"]["resolved_elevation_m"] = None

    manifest = {
        "configuration": str(config_path),
        "case": case,
        "fire_mode": fire_mode,
        "fire_percentile": percentile,
        "rating_timestamp_utc": iso(timestamp),
        "rating_time_source": time_source,
        "rating_timestamp_origin": timestamp_origin,
        "fire_arrival_summary": (
            str(arrival_json) if time_source == "fire_arrival" else None
        ),
        "solar_geometry_time_utc": iso(solar_time),
        "wildfire_case_solar": wildfire_solar,
        "inputs": {
            "template_raster": str(template),
            "fire_intensity_raster": str(fire_intensity),
            "prepared_conductor": str(prepared_vector),
            "era5_netcdf": str(era5_nc),
        },
        "outputs": {
            "conductor_directory": str(conductor_dir),
            "met_directory": str(met_dir),
            "line_fire_csv": str(line_csv),
            "fire_geometry_directory": (
                str(geometry_dir) if geometry_enabled else None
            ),
            "wildfire_heat_directory": str(heat_dir),
            "baseline_directory": str(baseline_dir),
            "wildfire_directory": str(wildfire_dir),
        },
        "conductor_elevation": resolved["conductor_elevation"].get(
            "resolved_elevation_m"
        ),
        "executed": runner.executed,
    }

    if not args.dry_run:
        case_dir.mkdir(parents=True, exist_ok=True)
        (case_dir / "rating_pipeline_manifest.json").write_text(
            json.dumps(manifest, indent=2), encoding="utf-8"
        )
        (case_dir / "resolved_config.yaml").write_text(
            yaml.safe_dump(resolved, sort_keys=False), encoding="utf-8"
        )

    print("\nPipeline complete." if not args.dry_run else "\nDry run complete.")
    print(f"Case:      {case}")
    print(f"Case dir:  {case_dir}")


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as error:
        print(
            f"ERROR: command failed with exit code {error.returncode}",
            file=sys.stderr,
        )
        sys.exit(error.returncode)
    except Exception as error:
        print(f"ERROR: {error}", file=sys.stderr)
        sys.exit(1)
