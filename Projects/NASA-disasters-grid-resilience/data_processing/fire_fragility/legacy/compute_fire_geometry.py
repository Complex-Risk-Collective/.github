#!/usr/bin/env python3

"""
Derive Pyretechnics surface-fire geometry from fire-behavior rasters.

Required input
--------------
Fireline intensity in kW/m.

Optional flame-depth inputs
---------------------------
Method 1:
    Spread rate in m/min and weighted characteristic SAV ratio in 1/ft.

Method 2:
    Spread rate in m/min and residence time in minutes.

Outputs
-------
- flame_length_m_<timestamp>.tif
- residence_time_min_<timestamp>.tif, when calculable
- flame_depth_m_<timestamp>.tif, when calculable
- heat_per_unit_area_kj_m2_<timestamp>.tif, when spread rate is supplied

Pyretechnics relationships
--------------------------
    flame_length_m = 0.07747042253266703
                     * fireline_intensity_kw_m ** 0.46

    residence_time_min = 384 / sigma_prime_per_ft

    flame_depth_m = spread_rate_m_min * residence_time_min

    heat_per_unit_area_kj_m2 =
        60 * fireline_intensity_kw_m / spread_rate_m_min
"""

import argparse
import re
from pathlib import Path

import numpy as np
from rasterio.enums import Resampling

from raster_utils import (
    read_scalar_or_raster,
    read_to_template,
    write_float_raster,
)


TIMESTAMP_PATTERN = re.compile(
    r"(?P<tag>\d{8}T\d{6}Z)"
)


def parse_arguments():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "fireline_intensity_kw_m",
        help="Fireline-intensity raster in kW/m.",
    )

    parser.add_argument(
        "--spread-rate-m-min",
        help=(
            "Spread-rate raster or scalar in m/min. Required "
            "for flame depth and heat per unit area."
        ),
    )

    depth_group = parser.add_mutually_exclusive_group()

    depth_group.add_argument(
        "--sigma-prime-per-ft",
        help=(
            "Weighted characteristic fuel-particle SAV ratio "
            "as a raster or scalar in 1/ft."
        ),
    )

    depth_group.add_argument(
        "--residence-time-min",
        help=(
            "Flaming residence time as a raster or scalar "
            "in minutes."
        ),
    )

    parser.add_argument(
        "--out-dir",
        required=True,
    )

    return parser.parse_args()


def get_timestamp_tag(path):
    match = TIMESTAMP_PATTERN.search(
        Path(path).name
    )

    if match:
        return f"_{match.group('tag')}"

    return ""


def main():
    args = parse_arguments()

    template = args.fireline_intensity_kw_m

    intensity, profile = read_to_template(
        args.fireline_intensity_kw_m,
        template,
        Resampling.nearest,
    )

    valid_intensity = (
        np.isfinite(intensity)
        & (intensity >= 0.0)
    )

    flame_length = np.where(
        valid_intensity,
        0.07747042253266703
        * np.power(intensity, 0.46),
        np.nan,
    )

    output_directory = Path(args.out_dir)
    output_directory.mkdir(
        parents=True,
        exist_ok=True,
    )

    tag = get_timestamp_tag(
        args.fireline_intensity_kw_m
    )

    write_float_raster(
        output_directory
        / f"flame_length_m{tag}.tif",
        flame_length,
        profile,
        description=(
            "pyretechnics_average_flame_length_m"
        ),
    )

    if args.spread_rate_m_min is None:
        if (
            args.sigma_prime_per_ft is not None
            or args.residence_time_min is not None
        ):
            raise ValueError(
                "--spread-rate-m-min is required to calculate "
                "flame depth."
            )

        print(
            "Flame length was calculated. Flame depth was not "
            "calculated because spread rate was not supplied."
        )
        return

    spread_rate, _ = read_scalar_or_raster(
        args.spread_rate_m_min,
        template,
        Resampling.bilinear,
    )

    valid_spread = (
        valid_intensity
        & np.isfinite(spread_rate)
        & (spread_rate > 0.0)
    )

    heat_per_unit_area = np.where(
        valid_spread,
        60.0 * intensity / spread_rate,
        np.nan,
    )

    write_float_raster(
        output_directory
        / f"heat_per_unit_area_kj_m2{tag}.tif",
        heat_per_unit_area,
        profile,
        description=(
            "surface_fire_heat_per_unit_area_kj_m2"
        ),
    )

    residence_time = None

    if args.sigma_prime_per_ft is not None:
        sigma_prime, _ = read_scalar_or_raster(
            args.sigma_prime_per_ft,
            template,
            Resampling.bilinear,
        )

        valid_residence = (
            valid_spread
            & np.isfinite(sigma_prime)
            & (sigma_prime > 0.0)
        )

        residence_time = np.where(
            valid_residence,
            384.0 / sigma_prime,
            np.nan,
        )

    elif args.residence_time_min is not None:
        residence_time, _ = read_scalar_or_raster(
            args.residence_time_min,
            template,
            Resampling.bilinear,
        )

        valid_residence = (
            valid_spread
            & np.isfinite(residence_time)
            & (residence_time > 0.0)
        )

        residence_time = np.where(
            valid_residence,
            residence_time,
            np.nan,
        )

    if residence_time is None:
        print(
            "Flame length and heat per unit area were calculated. "
            "Flame depth was not calculated because neither "
            "residence time nor sigma-prime was supplied."
        )
        return

    flame_depth = np.where(
        valid_spread
        & np.isfinite(residence_time),
        spread_rate * residence_time,
        np.nan,
    )

    write_float_raster(
        output_directory
        / f"residence_time_min{tag}.tif",
        residence_time,
        profile,
        description=(
            "surface_fire_flaming_residence_time_min"
        ),
    )

    write_float_raster(
        output_directory
        / f"flame_depth_m{tag}.tif",
        flame_depth,
        profile,
        description=(
            "surface_fire_active_flaming_zone_depth_m"
        ),
    )


if __name__ == "__main__":
    main()

