#!/usr/bin/env python3

"""
Calculate solar heat gain per unit conductor length.

Equation
--------
    qs = alpha * Qse * sin(theta) * D

where:
    qs    = solar heat gain per unit conductor length, W/m
    alpha = conductor solar absorptivity, dimensionless
    Qse   = selected solar heat intensity, W/m2
    theta = effective solar incidence angle, degrees
    D     = outside conductor diameter, m

Critical model decision
-----------------------
The user must decide what irradiance product represents Qse.

ERA5 surface solar radiation downwards is radiation on a horizontal
surface. It should not automatically be treated as conductor-normal
irradiance if direct and diffuse radiation are being modeled separately.

This script performs the equation only. It does not resolve the ambiguity
between horizontal irradiance, direct normal irradiance, diffuse sky
radiation, and the IEEE 738 solar-intensity method.
"""

import argparse

import numpy as np
from rasterio.enums import Resampling

from raster_utils import read_to_template, write_float_raster


def main():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "solar_absorptivity_raster",
    )

    parser.add_argument(
        "solar_intensity_w_m2",
        help="Selected Qse raster in W/m2.",
    )

    parser.add_argument(
        "incidence_angle_deg_raster",
    )

    parser.add_argument(
        "conductor_diameter_m_raster",
    )

    parser.add_argument(
        "--output",
        required=True,
    )

    args = parser.parse_args()

    template_path = args.solar_absorptivity_raster

    absorptivity, profile = read_to_template(
        args.solar_absorptivity_raster,
        template_path,
        Resampling.nearest,
    )

    solar_intensity, _ = read_to_template(
        args.solar_intensity_w_m2,
        template_path,
        Resampling.bilinear,
    )

    incidence_angle, _ = read_to_template(
        args.incidence_angle_deg_raster,
        template_path,
        Resampling.bilinear,
    )

    diameter, _ = read_to_template(
        args.conductor_diameter_m_raster,
        template_path,
        Resampling.nearest,
    )

    valid = (
        np.isfinite(absorptivity)
        & np.isfinite(solar_intensity)
        & np.isfinite(incidence_angle)
        & np.isfinite(diameter)
        & (absorptivity >= 0.0)
        & (absorptivity <= 1.0)
        & (solar_intensity >= 0.0)
        & (diameter > 0.0)
    )

    solar_heat_gain = (
        absorptivity
        * solar_intensity
        * np.sin(np.deg2rad(incidence_angle))
        * diameter
    )

    solar_heat_gain = np.where(
        valid,
        solar_heat_gain,
        np.nan,
    )

    write_float_raster(
        args.output,
        solar_heat_gain,
        profile,
        description=(
            "solar_heat_gain_per_conductor_length_w_m"
        ),
    )


if __name__ == "__main__":
    main()

