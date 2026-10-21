#!/usr/bin/env python3

"""
Run the ERA5-based conductor-rating pipeline.

Baseline ratings are calculated for all discovered timestamps.

Wildfire ratings are calculated only when both of the following
are supplied:
    --wildfire-heat-dir
    --wildfire-tags

Requiring explicit wildfire tags prevents an event-level maximum
fireline intensity from being silently applied to every hour.
"""

import argparse
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path


TEMPERATURE_PATTERN = re.compile(
    r"^air_temperature_c_"
    r"(?P<tag>\d{8}T\d{6}Z)\.tif$"
)


def parse_arguments():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--era5-dir",
        required=True,
    )

    parser.add_argument(
        "--conductor-dir",
        required=True,
    )

    parser.add_argument(
        "--surface-temperature-c",
        required=True,
    )

    parser.add_argument(
        "--elevation-m",
        required=True,
    )

    parser.add_argument(
        "--emissivity",
        required=True,
    )

    parser.add_argument(
        "--resistance-low-ohm-m",
        type=float,
        required=True,
    )

    parser.add_argument(
        "--temperature-low-c",
        type=float,
        required=True,
    )

    parser.add_argument(
        "--resistance-high-ohm-m",
        type=float,
        required=True,
    )

    parser.add_argument(
        "--temperature-high-c",
        type=float,
        required=True,
    )

    parser.add_argument(
        "--wind-method",
        choices=["power", "log"],
        default="power",
    )

    parser.add_argument(
        "--power-exponent",
        type=float,
        default=1.0 / 7.0,
    )

    parser.add_argument(
        "--roughness-length",
    )

    parser.add_argument(
        "--calm-threshold",
        type=float,
        default=0.1,
    )

    parser.add_argument(
        "--diffuse-model",
        choices=[
            "none",
            "isotropic-cylinder",
        ],
        default="isotropic-cylinder",
    )

    parser.add_argument(
        "--wildfire-heat-dir",
    )

    parser.add_argument(
        "--wildfire-tags",
        nargs="+",
        help=(
            "Timestamps to which wildfire heat should be "
            "applied, such as 20240922T190000Z."
        ),
    )

    parser.add_argument(
        "--script-dir",
        default=".",
    )

    parser.add_argument(
        "--out-dir",
        required=True,
    )

    parser.add_argument(
        "--python",
        default=sys.executable,
    )

    return parser.parse_args()


def require_file(path):
    path = Path(path)

    if not path.is_file():
        raise FileNotFoundError(
            f"Required input not found: {path}"
        )

    return path


def run(command):
    command = [
        str(value)
        for value in command
    ]

    print(" ".join(command))

    subprocess.run(
        command,
        check=True,
    )


def iso_timestamp(tag):
    timestamp = datetime.strptime(
        tag,
        "%Y%m%dT%H%M%SZ",
    )

    return timestamp.strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def main():
    args = parse_arguments()

    if bool(args.wildfire_heat_dir) != bool(
        args.wildfire_tags
    ):
        raise ValueError(
            "--wildfire-heat-dir and --wildfire-tags "
            "must be supplied together."
        )

    era5_directory = Path(
        args.era5_dir
    ).resolve()

    conductor_directory = Path(
        args.conductor_dir
    ).resolve()

    script_directory = Path(
        args.script_dir
    ).resolve()

    output_directory = Path(
        args.out_dir
    ).resolve()

    axis = require_file(
        conductor_directory
        / "conductor_axis_azimuth_deg.tif"
    )

    height = require_file(
        conductor_directory
        / "conductor_height_m.tif"
    )

    diameter = require_file(
        conductor_directory
        / "conductor_diameter_m.tif"
    )

    absorptivity = require_file(
        conductor_directory
        / "solar_absorptivity.tif"
    )

    wind_height_script = require_file(
        script_directory
        / "compute_wind_at_height.py"
    )

    wind_angle_script = require_file(
        script_directory
        / "compute_wind_relative_angle.py"
    )

    solar_angle_script = require_file(
        script_directory
        / "compute_solar_incidence.py"
    )

    solar_gain_script = require_file(
        script_directory
        / "compute_era5_solar_heat_gain.py"
    )

    rating_script = require_file(
        script_directory
        / "compute_ieee738_rating.py"
    )

    wildfire_tags = set(
        args.wildfire_tags or []
    )

    if args.wildfire_heat_dir:
        wildfire_directory = Path(
            args.wildfire_heat_dir
        ).resolve()

        wildfire_convection = require_file(
            wildfire_directory
            / "wildfire_convective_heat_w_m.tif"
        )

        wildfire_radiation = require_file(
            wildfire_directory
            / "wildfire_radiative_heat_w_m.tif"
        )
    else:
        wildfire_convection = None
        wildfire_radiation = None

    timestamps = []

    for path in sorted(
        era5_directory.glob(
            "air_temperature_c_*.tif"
        )
    ):
        match = TEMPERATURE_PATTERN.match(
            path.name
        )

        if match:
            timestamps.append(
                (
                    match.group("tag"),
                    path,
                )
            )

    if not timestamps:
        raise FileNotFoundError(
            "No timestamped ERA5 temperature rasters found."
        )

    available_tags = {
        tag
        for tag, _ in timestamps
    }

    missing_fire_tags = (
        wildfire_tags - available_tags
    )

    if missing_fire_tags:
        raise ValueError(
            "Wildfire timestamps are unavailable: "
            + ", ".join(
                sorted(missing_fire_tags)
            )
        )

    for tag, temperature in timestamps:
        print(f"\nProcessing {tag}")

        u10 = require_file(
            era5_directory
            / f"u10_m_s_{tag}.tif"
        )

        v10 = require_file(
            era5_directory
            / f"v10_m_s_{tag}.tif"
        )

        ghi = require_file(
            era5_directory
            / f"ssrd_w_m2_{tag}.tif"
        )

        direct_horizontal = require_file(
            era5_directory
            / f"direct_horizontal_w_m2_{tag}.tif"
        )

        hour_directory = (
            output_directory / tag
        )

        wind_directory = (
            hour_directory / "wind"
        )

        solar_directory = (
            hour_directory / "solar"
        )

        baseline_directory = (
            hour_directory / "baseline"
        )

        u_height = (
            wind_directory
            / f"u_conductor_m_s_{tag}.tif"
        )

        v_height = (
            wind_directory
            / f"v_conductor_m_s_{tag}.tif"
        )

        wind_speed = (
            wind_directory
            / f"wind_speed_conductor_m_s_{tag}.tif"
        )

        wind_angle = (
            wind_directory
            / f"wind_relative_angle_deg_{tag}.tif"
        )

        solar_incidence = (
            solar_directory
            / f"solar_incidence_angle_deg_{tag}.tif"
        )

        solar_elevation = (
            solar_directory
            / f"solar_elevation_deg_{tag}.tif"
        )

        solar_azimuth = (
            solar_directory
            / f"solar_azimuth_deg_{tag}.tif"
        )

        solar_gain = (
            solar_directory
            / f"solar_heat_gain_w_m_{tag}.tif"
        )

        wind_directory.mkdir(
            parents=True,
            exist_ok=True,
        )

        solar_directory.mkdir(
            parents=True,
            exist_ok=True,
        )

        wind_command = [
            args.python,
            wind_height_script,
            u10,
            v10,
            height,
            "--method",
            args.wind_method,
            "--out-u",
            u_height,
            "--out-v",
            v_height,
            "--out-speed",
            wind_speed,
        ]

        if args.wind_method == "power":
            wind_command.extend(
                [
                    "--power-exponent",
                    args.power_exponent,
                ]
            )
        else:
            if args.roughness_length is None:
                raise ValueError(
                    "--roughness-length is required for "
                    "the log wind profile."
                )

            wind_command.extend(
                [
                    "--roughness-length",
                    args.roughness_length,
                ]
            )

        run(wind_command)

        run(
            [
                args.python,
                wind_angle_script,
                u_height,
                v_height,
                axis,
                "--calm-threshold",
                args.calm_threshold,
                "--output",
                wind_angle,
            ]
        )

        run(
            [
                args.python,
                solar_angle_script,
                axis,
                "--time",
                iso_timestamp(tag),
                "--out-angle",
                solar_incidence,
                "--out-elevation",
                solar_elevation,
                "--out-azimuth",
                solar_azimuth,
            ]
        )

        run(
            [
                args.python,
                solar_gain_script,
                ghi,
                direct_horizontal,
                solar_elevation,
                solar_incidence,
                absorptivity,
                diameter,
                "--diffuse-model",
                args.diffuse_model,
                "--timestamp-tag",
                tag,
                "--out-dir",
                solar_directory,
            ]
        )

        run(
            [
                args.python,
                rating_script,
                temperature,
                wind_speed,
                wind_angle,
                diameter,
                "--surface-temperature-c",
                args.surface_temperature_c,
                "--elevation-m",
                args.elevation_m,
                "--emissivity",
                args.emissivity,
                "--solar-gain-w-m",
                solar_gain,
                "--resistance-low-ohm-m",
                args.resistance_low_ohm_m,
                "--temperature-low-c",
                args.temperature_low_c,
                "--resistance-high-ohm-m",
                args.resistance_high_ohm_m,
                "--temperature-high-c",
                args.temperature_high_c,
                "--timestamp-tag",
                tag,
                "--out-dir",
                baseline_directory,
            ]
        )

        if tag in wildfire_tags:
            baseline_ampacity = (
                baseline_directory
                / f"ampacity_a_{tag}.tif"
            )

            wildfire_rating_directory = (
                hour_directory / "wildfire"
            )

            run(
                [
                    args.python,
                    rating_script,
                    temperature,
                    wind_speed,
                    wind_angle,
                    diameter,
                    "--surface-temperature-c",
                    args.surface_temperature_c,
                    "--elevation-m",
                    args.elevation_m,
                    "--emissivity",
                    args.emissivity,
                    "--solar-off",
                    "--wildfire-convective-w-m",
                    wildfire_convection,
                    "--wildfire-radiative-w-m",
                    wildfire_radiation,
                    "--resistance-low-ohm-m",
                    args.resistance_low_ohm_m,
                    "--temperature-low-c",
                    args.temperature_low_c,
                    "--resistance-high-ohm-m",
                    args.resistance_high_ohm_m,
                    "--temperature-high-c",
                    args.temperature_high_c,
                    "--baseline-ampacity",
                    baseline_ampacity,
                    "--timestamp-tag",
                    tag,
                    "--out-dir",
                    wildfire_rating_directory,
                ]
            )

    print(
        f"\nCompleted {len(timestamps)} ERA5 timestamps."
    )


if __name__ == "__main__":
    main()

