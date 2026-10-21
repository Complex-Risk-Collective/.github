#!/usr/bin/env python3

"""
Compute temperature-dependent air properties for conductor heat transfer.

Outputs
-------
- film_temperature_c.tif
- air_density_kg_m3.tif
- air_thermal_conductivity_w_m_k.tif
- air_dynamic_viscosity_pa_s.tif

Required conductor-specific input
---------------------------------
Conductor surface temperature is required to calculate film temperature:

    Tfilm = (Ts + Ta) / 2

Ts cannot be obtained directly from ERA5. It must come from:
- A conductor-temperature measurement.
- A previous thermal calculation.
- A design or rating temperature scenario.
- A coupled iterative heat-balance calculation.

Density methods
---------------
ieee738:
    Uses film temperature and conductor elevation above mean sea level.

ideal_gas:
    Uses film temperature and surface pressure for dry air.

Humidity is not included in the ideal-gas option. Additional humidity or
dew-point data would be required for moist-air density.
"""

import argparse
from pathlib import Path

import numpy as np
from rasterio.enums import Resampling

from raster_utils import (
    read_scalar_or_raster,
    read_to_template,
    write_float_raster,
)


DRY_AIR_GAS_CONSTANT = 287.05


def main():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "ambient_temperature_c",
        help="Ambient-temperature raster in degrees Celsius.",
    )

    # USER INPUT REQUIRED:
    # Supply a raster or a scalar conductor surface temperature.
    parser.add_argument(
        "conductor_surface_temperature_c",
        help=(
            "Conductor surface-temperature raster or "
            "scalar in degrees Celsius."
        ),
    )

    parser.add_argument(
        "--density-method",
        choices=["ieee738", "ideal_gas"],
        default="ieee738",
    )

    # USER INPUT REQUIRED FOR IEEE METHOD:
    # This must be conductor elevation above mean sea level, not merely
    # conductor height above the ground.
    parser.add_argument(
        "--elevation-m",
        help=(
            "Conductor elevation raster or scalar in metres "
            "above mean sea level."
        ),
    )

    # USER INPUT REQUIRED FOR IDEAL-GAS METHOD:
    # Use actual surface/station pressure, not sea-level-adjusted pressure.
    parser.add_argument(
        "--pressure-raster",
        help="Air-pressure raster in pascals.",
    )

    parser.add_argument(
        "--mask",
        help=(
            "Optional conductor mask or conductor-property raster. "
            "Only finite mask cells are retained."
        ),
    )

    parser.add_argument(
        "--out-dir",
        required=True,
    )

    args = parser.parse_args()

    template_path = args.ambient_temperature_c

    ambient_temperature, profile = read_to_template(
        args.ambient_temperature_c,
        template_path,
        Resampling.bilinear,
    )

    conductor_temperature, _ = read_scalar_or_raster(
        args.conductor_surface_temperature_c,
        template_path,
        Resampling.bilinear,
    )

    film_temperature = (
        conductor_temperature + ambient_temperature
    ) / 2.0

    film_temperature_kelvin = (
        film_temperature + 273.15
    )

    valid = (
        np.isfinite(ambient_temperature)
        & np.isfinite(conductor_temperature)
        & (film_temperature_kelvin > 0.0)
    )

    # IEEE 738-style temperature correlation.
    dynamic_viscosity = (
        1.458e-6
        * film_temperature_kelvin ** 1.5
        / (film_temperature + 383.4)
    )

    # IEEE 738-style temperature correlation.
    thermal_conductivity = (
        2.424e-2
        + 7.477e-5 * film_temperature
        - 4.407e-9 * film_temperature**2
    )

    if args.density_method == "ieee738":
        if args.elevation_m is None:
            raise ValueError(
                "--elevation-m is required when "
                "--density-method=ieee738."
            )

        conductor_elevation, _ = read_scalar_or_raster(
            args.elevation_m,
            template_path,
            Resampling.bilinear,
        )

        valid &= np.isfinite(conductor_elevation)

        air_density = (
            1.293
            - 1.525e-4 * conductor_elevation
            + 6.379e-9 * conductor_elevation**2
        ) / (
            1.0 + 0.00367 * film_temperature
        )

    else:
        if args.pressure_raster is None:
            raise ValueError(
                "--pressure-raster is required when "
                "--density-method=ideal_gas."
            )

        pressure, _ = read_to_template(
            args.pressure_raster,
            template_path,
            Resampling.bilinear,
        )

        valid &= (
            np.isfinite(pressure)
            & (pressure > 0.0)
        )

        # Dry-air density.
        air_density = pressure / (
            DRY_AIR_GAS_CONSTANT
            * film_temperature_kelvin
        )

    if args.mask:
        mask, _ = read_to_template(
            args.mask,
            template_path,
            Resampling.nearest,
        )

        valid &= np.isfinite(mask)

    film_temperature = np.where(
        valid,
        film_temperature,
        np.nan,
    )

    air_density = np.where(
        valid,
        air_density,
        np.nan,
    )

    thermal_conductivity = np.where(
        valid,
        thermal_conductivity,
        np.nan,
    )

    dynamic_viscosity = np.where(
        valid,
        dynamic_viscosity,
        np.nan,
    )

    output_directory = Path(args.out_dir)
    output_directory.mkdir(parents=True, exist_ok=True)

    write_float_raster(
        output_directory / "film_temperature_c.tif",
        film_temperature,
        profile,
        description="air_film_temperature_c",
    )

    write_float_raster(
        output_directory / "air_density_kg_m3.tif",
        air_density,
        profile,
        description="air_density_kg_m3",
    )

    write_float_raster(
        output_directory
        / "air_thermal_conductivity_w_m_k.tif",
        thermal_conductivity,
        profile,
        description="air_thermal_conductivity_w_m_k",
    )

    write_float_raster(
        output_directory
        / "air_dynamic_viscosity_pa_s.tif",
        dynamic_viscosity,
        profile,
        description="air_dynamic_viscosity_pa_s",
    )


if __name__ == "__main__":
    main()

