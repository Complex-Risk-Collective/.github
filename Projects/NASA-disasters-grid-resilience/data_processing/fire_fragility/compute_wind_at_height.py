#!/usr/bin/env python3

"""
Adjust 10 m wind components to conductor height.

Available methods
-----------------
power:
    V(z) = V(10) * (z / 10)^alpha

log:
    V(z) = V(10) * ln(z / z0) / ln(10 / z0)

Missing information that must be supplied or justified
-------------------------------------------------------
- Conductor height above local ground.
- Wind-profile method.
- Power-law exponent, or aerodynamic roughness length.
- Whether neutral atmospheric stability is an acceptable assumption.
- Whether terrain, forest canopy, buildings, or line wake effects require
  a more detailed wind model.

The default power exponent is provided only as a placeholder. It must not
be treated as universally applicable.
"""

import argparse

import numpy as np
from rasterio.enums import Resampling

from raster_utils import (
    read_scalar_or_raster,
    read_to_template,
    write_float_raster,
)


def is_number(value):
    try:
        float(value)
        return True
    except ValueError:
        return False


def main():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "u10_raster",
        help="Eastward 10 m wind component in m/s.",
    )

    parser.add_argument(
        "v10_raster",
        help="Northward 10 m wind component in m/s.",
    )

    parser.add_argument(
        "height",
        help=(
            "Conductor-height raster or scalar height above "
            "local ground in metres."
        ),
    )

    parser.add_argument(
        "--method",
        choices=["power", "log"],
        default="power",
    )

    # USER ASSUMPTION REQUIRED:
    # Replace this with a value justified for the terrain, exposure,
    # vegetation, and atmospheric conditions.
    parser.add_argument(
        "--power-exponent",
        type=float,
        default=1.0 / 7.0,
    )

    # USER INPUT REQUIRED FOR LOG METHOD:
    # Roughness length must describe the upwind land surface.
    parser.add_argument(
        "--roughness-length",
        help="Roughness-length raster or scalar in metres.",
    )

    parser.add_argument("--out-u", required=True)
    parser.add_argument("--out-v", required=True)
    parser.add_argument("--out-speed", required=True)

    args = parser.parse_args()

    if is_number(args.height):
        template_path = args.u10_raster
    else:
        template_path = args.height

    u10, profile = read_to_template(
        args.u10_raster,
        template_path,
        Resampling.bilinear,
    )

    v10, _ = read_to_template(
        args.v10_raster,
        template_path,
        Resampling.bilinear,
    )

    height, _ = read_scalar_or_raster(
        args.height,
        template_path,
        Resampling.bilinear,
    )

    valid = (
        np.isfinite(u10)
        & np.isfinite(v10)
        & np.isfinite(height)
        & (height > 0.0)
    )

    if args.method == "power":
        if args.power_exponent <= 0:
            raise ValueError(
                "The power-law exponent must be positive."
            )

        adjustment_factor = np.where(
            valid,
            (height / 10.0) ** args.power_exponent,
            np.nan,
        )

    else:
        if args.roughness_length is None:
            raise ValueError(
                "--roughness-length is required for the log method."
            )

        roughness_length, _ = read_scalar_or_raster(
            args.roughness_length,
            template_path,
            Resampling.bilinear,
        )

        valid &= (
            np.isfinite(roughness_length)
            & (roughness_length > 0.0)
            & (height > roughness_length)
            & (10.0 > roughness_length)
        )

        adjustment_factor = np.where(
            valid,
            np.log(height / roughness_length)
            / np.log(10.0 / roughness_length),
            np.nan,
        )

    u_at_conductor = u10 * adjustment_factor
    v_at_conductor = v10 * adjustment_factor

    wind_speed = np.hypot(
        u_at_conductor,
        v_at_conductor,
    )

    write_float_raster(
        args.out_u,
        u_at_conductor,
        profile,
        description=(
            "eastward_wind_at_conductor_height_m_s"
        ),
    )

    write_float_raster(
        args.out_v,
        v_at_conductor,
        profile,
        description=(
            "northward_wind_at_conductor_height_m_s"
        ),
    )

    write_float_raster(
        args.out_speed,
        wind_speed,
        profile,
        description="wind_speed_at_conductor_height_m_s",
    )


if __name__ == "__main__":
    main()

