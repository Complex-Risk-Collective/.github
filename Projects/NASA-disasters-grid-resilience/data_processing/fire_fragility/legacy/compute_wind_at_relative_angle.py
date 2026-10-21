#!/usr/bin/env python3

"""
Calculate the acute angle between the wind vector and conductor axis.

Output convention
-----------------
0 degrees:
    Wind is parallel to the conductor.

90 degrees:
    Wind is perpendicular to the conductor.

The calculation treats the conductor as an undirected axis, so azimuths
separated by 180 degrees are equivalent.
"""

import argparse

import numpy as np
from rasterio.enums import Resampling

from raster_utils import read_to_template, write_float_raster


def main():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "u_wind_raster",
        help="Eastward wind component in m/s.",
    )

    parser.add_argument(
        "v_wind_raster",
        help="Northward wind component in m/s.",
    )

    parser.add_argument(
        "axis_azimuth_raster",
        help=(
            "Conductor-axis azimuth in degrees clockwise "
            "from north."
        ),
    )

    # USER DECISION REQUIRED:
    # Wind direction is undefined at very low speeds. Choose a threshold
    # appropriate for the source data and thermal model.
    parser.add_argument(
        "--calm-threshold",
        type=float,
        default=0.1,
        help="Wind speeds below this value receive nodata.",
    )

    parser.add_argument(
        "--output",
        required=True,
    )

    args = parser.parse_args()

    u_wind, profile = read_to_template(
        args.u_wind_raster,
        args.axis_azimuth_raster,
        Resampling.bilinear,
    )

    v_wind, _ = read_to_template(
        args.v_wind_raster,
        args.axis_azimuth_raster,
        Resampling.bilinear,
    )

    conductor_axis, _ = read_to_template(
        args.axis_azimuth_raster,
        args.axis_azimuth_raster,
        Resampling.nearest,
    )

    wind_speed = np.hypot(u_wind, v_wind)

    # atan2(u, v) returns the direction toward which the air vector
    # is moving, measured clockwise from north.
    #
    # Adding or subtracting 180 degrees does not affect the acute
    # angle to an undirected conductor axis.
    wind_axis = (
        np.rad2deg(np.arctan2(u_wind, v_wind))
        % 180.0
    )

    conductor_axis = conductor_axis % 180.0

    difference = np.abs(
        wind_axis - conductor_axis
    )

    relative_angle = np.minimum(
        difference,
        180.0 - difference,
    )

    invalid = (
        ~np.isfinite(conductor_axis)
        | ~np.isfinite(u_wind)
        | ~np.isfinite(v_wind)
        | (wind_speed < args.calm_threshold)
    )

    relative_angle[invalid] = np.nan

    write_float_raster(
        args.output,
        relative_angle,
        profile,
        description=(
            "acute_wind_angle_relative_to_conductor_deg"
        ),
    )


if __name__ == "__main__":
    main()

