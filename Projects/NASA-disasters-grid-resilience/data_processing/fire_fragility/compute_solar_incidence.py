#!/usr/bin/env python3

"""
Compute the effective angle between incoming solar radiation and a conductor.

The conductor-axis raster must contain degrees clockwise from north, with
values from 0 to 180 degrees.

The effective incidence angle is calculated as:

    theta = acos[
        cos(solar_elevation)
        * cos(solar_azimuth - conductor_azimuth)
    ]

USER INPUT REQUIRED:
- A UTC timestamp.
- A conductor-axis azimuth raster.
"""

import argparse
from datetime import datetime, timezone

import numpy as np
import rasterio
from rasterio.warp import transform as transform_coordinates

from raster_utils import write_float_raster


def solar_position(timestamp, latitude_deg, longitude_deg):
    """
    Approximate solar elevation and azimuth.

    Azimuth is returned in degrees clockwise from geographic north.
    """
    day_of_year = timestamp.timetuple().tm_yday

    decimal_hour = (
        timestamp.hour
        + timestamp.minute / 60.0
        + timestamp.second / 3600.0
    )

    fractional_year = (
        2.0
        * np.pi
        / 365.0
        * (
            day_of_year
            - 1
            + (decimal_hour - 12.0) / 24.0
        )
    )

    equation_of_time = 229.18 * (
        0.000075
        + 0.001868 * np.cos(fractional_year)
        - 0.032077 * np.sin(fractional_year)
        - 0.014615 * np.cos(2.0 * fractional_year)
        - 0.040849 * np.sin(2.0 * fractional_year)
    )

    solar_declination = (
        0.006918
        - 0.399912 * np.cos(fractional_year)
        + 0.070257 * np.sin(fractional_year)
        - 0.006758 * np.cos(2.0 * fractional_year)
        + 0.000907 * np.sin(2.0 * fractional_year)
        - 0.002697 * np.cos(3.0 * fractional_year)
        + 0.001480 * np.sin(3.0 * fractional_year)
    )

    true_solar_time_minutes = (
        decimal_hour * 60.0
        + equation_of_time
        + 4.0 * longitude_deg
    ) % 1440.0

    hour_angle = np.deg2rad(
        true_solar_time_minutes / 4.0 - 180.0
    )

    latitude = np.deg2rad(latitude_deg)

    solar_elevation = np.arcsin(
        np.sin(latitude) * np.sin(solar_declination)
        + np.cos(latitude)
        * np.cos(solar_declination)
        * np.cos(hour_angle)
    )

    solar_azimuth = (
        np.arctan2(
            np.sin(hour_angle),
            np.cos(hour_angle) * np.sin(latitude)
            - np.tan(solar_declination) * np.cos(latitude),
        )
        + np.pi
    ) % (2.0 * np.pi)

    return (
        np.rad2deg(solar_elevation),
        np.rad2deg(solar_azimuth),
    )


def main():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "axis_azimuth_raster",
        help="Conductor-axis azimuth raster.",
    )

    parser.add_argument(
        "--time",
        required=True,
        help="UTC ISO timestamp, for example 2026-07-15T20:00:00Z",
    )

    parser.add_argument(
        "--out-angle",
        required=True,
    )

    parser.add_argument("--out-elevation")
    parser.add_argument("--out-azimuth")

    args = parser.parse_args()

    timestamp = datetime.fromisoformat(
        args.time.replace("Z", "+00:00")
    )

    if timestamp.tzinfo is None:
        raise ValueError(
            "The timestamp must include Z or an explicit UTC offset."
        )

    timestamp = timestamp.astimezone(timezone.utc)

    with rasterio.open(args.axis_azimuth_raster) as source:
        conductor_azimuth = source.read(1).astype("float32")
        profile = source.profile.copy()

        if source.nodata is not None:
            conductor_azimuth[
                conductor_azimuth == source.nodata
            ] = np.nan

        rows, columns = np.indices(conductor_azimuth.shape)

        x, y = rasterio.transform.xy(
            source.transform,
            rows,
            columns,
            offset="center",
        )

        x = np.asarray(x).ravel()
        y = np.asarray(y).ravel()

        longitude, latitude = transform_coordinates(
            source.crs,
            "EPSG:4326",
            x,
            y,
        )

    longitude = np.asarray(longitude).reshape(
        conductor_azimuth.shape
    )
    latitude = np.asarray(latitude).reshape(
        conductor_azimuth.shape
    )

    solar_elevation, solar_azimuth = solar_position(
        timestamp,
        latitude,
        longitude,
    )

    elevation_radians = np.deg2rad(solar_elevation)

    azimuth_difference = np.deg2rad(
        solar_azimuth - conductor_azimuth
    )

    cosine_theta = (
        np.cos(elevation_radians)
        * np.cos(azimuth_difference)
    )

    incidence_angle = np.rad2deg(
        np.arccos(np.clip(cosine_theta, -1.0, 1.0))
    )

    # USER MODEL DECISION:
    # Nighttime incidence is set to nodata because there is no direct
    # solar heat gain when the sun is below the horizon.
    invalid = (
        (solar_elevation <= 0.0)
        | ~np.isfinite(conductor_azimuth)
    )

    incidence_angle[invalid] = np.nan

    write_float_raster(
        args.out_angle,
        incidence_angle,
        profile,
        description="effective_solar_incidence_angle_deg",
    )

    if args.out_elevation:
        write_float_raster(
            args.out_elevation,
            np.where(
                np.isfinite(conductor_azimuth),
                solar_elevation,
                np.nan,
            ),
            profile,
            description="solar_elevation_deg",
        )

    if args.out_azimuth:
        write_float_raster(
            args.out_azimuth,
            np.where(
                np.isfinite(conductor_azimuth),
                solar_azimuth,
                np.nan,
            ),
            profile,
            description=(
                "solar_azimuth_deg_clockwise_from_north"
            ),
        )


if __name__ == "__main__":
    main()


