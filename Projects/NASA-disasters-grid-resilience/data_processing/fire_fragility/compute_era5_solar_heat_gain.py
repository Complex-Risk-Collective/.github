#!/usr/bin/env python3

"""
Calculate conductor solar heat gain from ERA5 radiation.

Inputs
------
GHI:
    ERA5 surface solar radiation downwards converted to W/m2.

BHI:
    ERA5 total-sky direct solar radiation at the surface,
    converted to W/m2. This is direct-horizontal irradiance,
    not DNI.

Solar elevation:
    Degrees above the horizon.

Solar incidence angle:
    Angle between the Sun's rays and the conductor axis.

Calculations
------------
DNI = BHI / sin(solar elevation)

DHI = max(GHI - BHI, 0)

Direct conductor gain:
    alpha * D * DNI * sin(theta)

Optional isotropic diffuse-sky gain for a horizontal cylinder:
    alpha * D * DHI * pi/2

Ground-reflected shortwave radiation is not included because the
required ground albedo/irradiance geometry is not presently supplied.

This is an ERA5 adaptation. It is not the IEEE 738 clear-sky
polynomial method used in a strict standard calculation.
"""

import argparse
from pathlib import Path

import numpy as np
from rasterio.enums import Resampling

from raster_utils import (
    read_to_template,
    write_float_raster,
)


def parse_arguments():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "ghi_w_m2",
        help="ERA5 global-horizontal irradiance raster.",
    )

    parser.add_argument(
        "direct_horizontal_w_m2",
        help="ERA5 direct-horizontal irradiance raster.",
    )

    parser.add_argument(
        "solar_elevation_deg",
    )

    parser.add_argument(
        "solar_incidence_angle_deg",
    )

    parser.add_argument(
        "solar_absorptivity",
    )

    parser.add_argument(
        "conductor_diameter_m",
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
        "--minimum-solar-elevation-deg",
        type=float,
        default=3.0,
        help=(
            "Below this elevation, DNI is not reconstructed "
            "because division by a small sine is unstable."
        ),
    )

    parser.add_argument(
        "--max-dni-w-m2",
        type=float,
        help=(
            "Optional DNI cap. No cap is applied by default "
            "because selection of a cap is a model assumption."
        ),
    )

    parser.add_argument(
        "--timestamp-tag",
        required=True,
        help="Filesystem timestamp such as 20260715T120000Z.",
    )

    parser.add_argument(
        "--out-dir",
        required=True,
    )

    return parser.parse_args()


def main():
    args = parse_arguments()

    template = args.solar_absorptivity

    absorptivity, profile = read_to_template(
        args.solar_absorptivity,
        template,
        Resampling.nearest,
    )

    diameter, _ = read_to_template(
        args.conductor_diameter_m,
        template,
        Resampling.nearest,
    )

    ghi, _ = read_to_template(
        args.ghi_w_m2,
        template,
        Resampling.bilinear,
    )

    direct_horizontal, _ = read_to_template(
        args.direct_horizontal_w_m2,
        template,
        Resampling.bilinear,
    )

    solar_elevation, _ = read_to_template(
        args.solar_elevation_deg,
        template,
        Resampling.bilinear,
    )

    incidence_angle, _ = read_to_template(
        args.solar_incidence_angle_deg,
        template,
        Resampling.bilinear,
    )

    valid = (
        np.isfinite(absorptivity)
        & np.isfinite(diameter)
        & np.isfinite(ghi)
        & np.isfinite(direct_horizontal)
        & np.isfinite(solar_elevation)
        & (absorptivity >= 0.0)
        & (absorptivity <= 1.0)
        & (diameter > 0.0)
        & (ghi >= 0.0)
        & (direct_horizontal >= 0.0)
    )

    day = (
        valid
        & (
            solar_elevation
            > args.minimum_solar_elevation_deg
        )
    )

    elevation_sine = np.sin(
        np.deg2rad(solar_elevation)
    )

    dni = np.zeros_like(
        ghi,
        dtype="float32",
    )

    dni[day] = (
        direct_horizontal[day]
        / elevation_sine[day]
    )

    dni = np.maximum(
        dni,
        0.0,
    )

    if args.max_dni_w_m2 is not None:
        if args.max_dni_w_m2 <= 0:
            raise ValueError(
                "--max-dni-w-m2 must be positive."
            )

        dni = np.minimum(
            dni,
            args.max_dni_w_m2,
        )

    diffuse_horizontal = np.maximum(
        ghi - direct_horizontal,
        0.0,
    )

    direct_gain = np.zeros_like(
        ghi,
        dtype="float32",
    )

    direct_valid = (
        day
        & np.isfinite(incidence_angle)
    )

    direct_gain[direct_valid] = (
        absorptivity[direct_valid]
        * diameter[direct_valid]
        * dni[direct_valid]
        * np.sin(
            np.deg2rad(
                incidence_angle[direct_valid]
            )
        )
    )

    if args.diffuse_model == "none":
        diffuse_gain = np.zeros_like(
            ghi,
            dtype="float32",
        )
    else:
        diffuse_gain = (
            absorptivity
            * diameter
            * diffuse_horizontal
            * np.pi
            / 2.0
        )

        diffuse_gain = np.where(
            solar_elevation > 0.0,
            diffuse_gain,
            0.0,
        )

    total_gain = (
        direct_gain + diffuse_gain
    )

    output_directory = Path(
        args.out_dir
    )

    output_directory.mkdir(
        parents=True,
        exist_ok=True,
    )

    tag = args.timestamp_tag

    write_float_raster(
        output_directory
        / f"dni_estimated_w_m2_{tag}.tif",
        np.where(valid, dni, np.nan),
        profile,
        description=(
            "estimated_direct_normal_irradiance_w_m2"
        ),
    )

    write_float_raster(
        output_directory
        / f"diffuse_horizontal_w_m2_{tag}.tif",
        np.where(
            valid,
            diffuse_horizontal,
            np.nan,
        ),
        profile,
        description=(
            "estimated_diffuse_horizontal_irradiance_w_m2"
        ),
    )

    write_float_raster(
        output_directory
        / f"solar_direct_gain_w_m_{tag}.tif",
        np.where(valid, direct_gain, np.nan),
        profile,
        description=(
            "direct_solar_heat_gain_per_conductor_length_w_m"
        ),
    )

    write_float_raster(
        output_directory
        / f"solar_diffuse_gain_w_m_{tag}.tif",
        np.where(valid, diffuse_gain, np.nan),
        profile,
        description=(
            "diffuse_sky_heat_gain_per_conductor_length_w_m"
        ),
    )

    write_float_raster(
        output_directory
        / f"solar_heat_gain_w_m_{tag}.tif",
        np.where(valid, total_gain, np.nan),
        profile,
        description=(
            "total_solar_heat_gain_per_conductor_length_w_m"
        ),
    )


if __name__ == "__main__":
    main()
