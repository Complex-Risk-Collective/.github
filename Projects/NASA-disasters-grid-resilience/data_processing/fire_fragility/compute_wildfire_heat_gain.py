#!/usr/bin/env python3

"""
Calculate wildfire convection and radiation heat gain using
Pyretechnics-derived fireline intensity and spread direction.

Inputs
------
fireline intensity:
    Maximum Pyretechnics fireline intensity affecting each line, kW/m.

fire-front angle alpha (manuscript Eq. 15, "a"):
    Acute angle between the fire front and the receiving conductor.

    0 degrees means:
        - Fire front parallel to the conductor.
        - Fire spreading perpendicular to the line.
        - Manuscript case-study geometry (cos = 1, maximum radiation).

    Scalar or raster; compute_fire_conductor_angle.py (stage 9) writes a
    per-line raster from Pyretechnics time of arrival.

Equations
---------
Convective wildfire heat:

    qcw = f * 22 * (I_W^2 / h^5)^(1/3) * D

Radiative wildfire heat:

    qrw = Xr * I * cos(alpha) / (4*pi)
          * [2/r * atan(L/(2*r))]
          * D

where:

    r = sqrt(h^2 + y^2)

Defaults reproducing manuscript assumptions
-------------------------------------------
f:
    0.1

Xr:
    0.15

Fire-front length:
    100 m

Ground offset:
    0 m

Fire-front angle:
    0 degrees, or the stage 9 raster from Pyretechnics time of arrival.

Ground offset and fire-front length accept a scalar or a raster (stage 9).
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
from rasterio.enums import Resampling

from raster_utils import (
    read_scalar_or_raster,
    read_to_template,
    write_float_raster,
)


def parse_arguments():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "fireline_intensity_kw_m",
    )

    parser.add_argument(
        "conductor_height_m",
        help=(
            "Vertical separation between fire and conductor, "
            "as a scalar or raster in metres."
        ),
    )

    parser.add_argument(
        "conductor_diameter_m",
        help="Outside conductor diameter, scalar or raster in m.",
    )

    parser.add_argument(
        "--fire-normal-angle-deg",
        default="0",
        help=(
            "Acute angle between the fire front and the conductor "
            "(0 = front parallel to line), scalar or raster. Default: 0."
        ),
    )

    parser.add_argument(
        "--ground-offset-m",
        default="0",
        help=(
            "Horizontal distance between fire source and conductor "
            "projection, scalar or raster. Default: 0."
        ),
    )

    parser.add_argument(
        "--convective-coefficient",
        type=float,
        default=0.1,
        help="Manuscript case-study coefficient f.",
    )

    parser.add_argument(
        "--radiative-fraction",
        type=float,
        default=0.15,
        help="Manuscript case-study radiative fraction Xr.",
    )

    parser.add_argument(
        "--fire-front-length-m",
        default="100",
        help="Fire-front length L, scalar or raster in m. Default: 100.",
    )

    parser.add_argument(
        "--tag",
        help="Optional event tag appended to output filenames.",
    )

    parser.add_argument(
        "--out-dir",
        required=True,
    )

    return parser.parse_args()


def main():
    args = parse_arguments()

    if args.convective_coefficient < 0:
        raise ValueError(
            "Convective coefficient cannot be negative."
        )

    if not 0.0 <= args.radiative_fraction <= 1.0:
        raise ValueError(
            "Radiative fraction must be between zero and one."
        )

    template = args.fireline_intensity_kw_m

    intensity_kw_m, profile = read_to_template(
        template,
        template,
        Resampling.nearest,
    )

    height_m, _ = read_scalar_or_raster(
        args.conductor_height_m,
        template,
        Resampling.bilinear,
    )

    diameter_m, _ = read_scalar_or_raster(
        args.conductor_diameter_m,
        template,
        Resampling.nearest,
    )

    incidence_angle_deg, _ = read_scalar_or_raster(
        args.fire_normal_angle_deg,
        template,
        Resampling.nearest,
    )

    ground_offset_m, _ = read_scalar_or_raster(
        args.ground_offset_m,
        template,
        Resampling.nearest,
    )

    front_length_m, _ = read_scalar_or_raster(
        args.fire_front_length_m,
        template,
        Resampling.nearest,
    )
    if np.isfinite(front_length_m).any() and np.nanmax(front_length_m) <= 0.0:
        raise ValueError("Fire-front length must be positive.")

    valid = (
        np.isfinite(intensity_kw_m)
        & np.isfinite(front_length_m)
        & (front_length_m > 0.0)
        & np.isfinite(height_m)
        & np.isfinite(diameter_m)
        & np.isfinite(incidence_angle_deg)
        & np.isfinite(ground_offset_m)
        & (intensity_kw_m >= 0.0)
        & (height_m > 0.0)
        & (diameter_m > 0.0)
        & (ground_offset_m >= 0.0)
        & (incidence_angle_deg >= 0.0)
        & (incidence_angle_deg <= 90.0)
    )

    intensity_w_m = (
        intensity_kw_m * 1000.0
    )

    wildfire_convection_w_m = (
        args.convective_coefficient
        * 22.0
        * (
            intensity_w_m**2
            / height_m**5
        ) ** (1.0 / 3.0)
        * diameter_m
    )

    radial_distance_m = np.sqrt(
        height_m**2
        + ground_offset_m**2
    )

    finite_front_integral = (
        2.0
        / radial_distance_m
        * np.arctan(
            front_length_m
            / (2.0 * radial_distance_m)
        )
    )

    wildfire_radiation_w_m = (
        args.radiative_fraction
        * intensity_kw_m
        * np.cos(
            np.deg2rad(
                incidence_angle_deg
            )
        )
        / (4.0 * np.pi)
        * finite_front_integral
        * diameter_m
        * 1000.0
    )

    wildfire_convection_w_m = np.where(
        valid,
        wildfire_convection_w_m,
        np.nan,
    )

    wildfire_radiation_w_m = np.where(
        valid,
        wildfire_radiation_w_m,
        np.nan,
    )

    total_wildfire_heat_w_m = (
        wildfire_convection_w_m
        + wildfire_radiation_w_m
    )

    output_directory = Path(
        args.out_dir
    ).expanduser().resolve()

    output_directory.mkdir(
        parents=True,
        exist_ok=True,
    )

    suffix = (
        f"_{args.tag}"
        if args.tag
        else ""
    )

    write_float_raster(
        output_directory
        / f"wildfire_convective_heat_w_m{suffix}.tif",
        wildfire_convection_w_m,
        profile,
        description="wildfire_convective_heat_gain_w_m",
    )

    write_float_raster(
        output_directory
        / f"wildfire_radiative_heat_w_m{suffix}.tif",
        wildfire_radiation_w_m,
        profile,
        description="wildfire_radiative_heat_gain_w_m",
    )

    write_float_raster(
        output_directory
        / f"total_wildfire_heat_w_m{suffix}.tif",
        total_wildfire_heat_w_m,
        profile,
        description="total_wildfire_heat_gain_w_m",
    )


if __name__ == "__main__":
    main()


