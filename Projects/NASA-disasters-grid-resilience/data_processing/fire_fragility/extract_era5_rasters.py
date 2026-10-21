#!/usr/bin/env python3

"""
Extract ERA5 NetCDF variables into timestamped GeoTIFFs.

Default behavior
----------------
Every available timestamp is extracted when --times is omitted.

Produced rasters
----------------
air_temperature_c_<TIMESTAMP>.tif
dewpoint_temperature_c_<TIMESTAMP>.tif
relative_humidity_pct_<TIMESTAMP>.tif
u10_m_s_<TIMESTAMP>.tif
v10_m_s_<TIMESTAMP>.tif
wind_speed_10m_m_s_<TIMESTAMP>.tif
wind_direction_from_deg_<TIMESTAMP>.tif
surface_pressure_pa_<TIMESTAMP>.tif
total_precipitation_mm_<TIMESTAMP>.tif
ssrd_w_m2_<TIMESTAMP>.tif
direct_horizontal_w_m2_<TIMESTAMP>.tif

Important radiation terminology
-------------------------------
ERA5 fdir is direct radiation passing through a horizontal plane.
It is not direct-normal irradiance, so it is deliberately named
"direct_horizontal" here.

Relative humidity
-----------------
Near-surface relative humidity is calculated from 2 m temperature
and 2 m dew-point temperature using saturation vapor pressure over
liquid water:

    RH = 100 * es(Td) / es(T)

Radiation and precipitation
---------------------------
By default, radiation is treated as a one-hour accumulation in J/m2
and divided by 3600 to obtain W/m2.

Total precipitation is treated as accumulated water depth in metres
and multiplied by 1000 to obtain millimetres.
"""

from __future__ import annotations

import argparse
import csv
from pathlib import Path

import numpy as np
import pandas as pd
import rasterio
import xarray as xr
from rasterio.transform import from_bounds


VARIABLE_ALIASES = {
    "temperature": [
        "t2m",
        "2m_temperature",
    ],
    "dewpoint": [
        "d2m",
        "2m_dewpoint_temperature",
    ],
    "u10": [
        "u10",
        "10m_u_component_of_wind",
    ],
    "v10": [
        "v10",
        "10m_v_component_of_wind",
    ],
    "pressure": [
        "sp",
        "surface_pressure",
    ],
    "precipitation": [
        "tp",
        "total_precipitation",
    ],
    "ssrd": [
        "ssrd",
        "surface_solar_radiation_downwards",
    ],
    "direct_horizontal": [
        "fdir",
        "total_sky_direct_solar_radiation_at_surface",
    ],
}


def parse_arguments():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "input_nc",
        help="ERA5 NetCDF input file.",
    )

    parser.add_argument(
        "--times",
        nargs="+",
        default=None,
        help=(
            "Optional UTC timestamps to extract. If omitted, "
            "all timestamps are extracted."
        ),
    )

    parser.add_argument(
        "--out-dir",
        required=True,
    )

    parser.add_argument(
        "--manifest",
        help=(
            "Output manifest CSV. Defaults to "
            "<out-dir>/era5_raster_manifest.csv."
        ),
    )

    parser.add_argument(
        "--accumulation-seconds",
        type=float,
        default=3600.0,
        help=(
            "Accumulation interval used to convert radiation "
            "from J/m2 to W/m2. Default: 3600."
        ),
    )

    parser.add_argument(
        "--radiation-already-w-m2",
        action="store_true",
    )

    parser.add_argument(
        "--precipitation-already-mm",
        action="store_true",
    )

    parser.add_argument(
        "--overwrite",
        action="store_true",
    )

    return parser.parse_args()


def find_coordinate(dataset, candidates):
    for name in candidates:
        if name in dataset.coords:
            return name

    raise KeyError(
        f"None of the coordinates were found: {candidates}"
    )


def find_variable(dataset, aliases):
    for name in aliases:
        if name in dataset.data_vars:
            return dataset[name]

    raise KeyError(
        f"None of the variables were found: {aliases}. "
        f"Available variables: {list(dataset.data_vars)}"
    )


def normalize_timestamp(value):
    timestamp = pd.Timestamp(value)

    if timestamp.tzinfo is not None:
        timestamp = timestamp.tz_convert(
            "UTC"
        ).tz_localize(None)

    return timestamp


def timestamp_tag(timestamp):
    return timestamp.strftime(
        "%Y%m%dT%H%M%SZ"
    )


def select_time_indices(
    dataset,
    time_coordinate,
    requested_times,
):
    values = np.atleast_1d(
        dataset[time_coordinate].values
    )

    available = [
        normalize_timestamp(value)
        for value in values
    ]

    if requested_times is None:
        return list(enumerate(available))

    requested = [
        normalize_timestamp(value)
        for value in requested_times
    ]

    selected = []

    for requested_time in requested:
        matches = [
            index
            for index, available_time in enumerate(available)
            if available_time == requested_time
        ]

        if not matches:
            raise ValueError(
                "Requested timestamp is unavailable: "
                f"{requested_time.isoformat()}Z"
            )

        for index in matches:
            if index not in [
                item[0] for item in selected
            ]:
                selected.append(
                    (index, available[index])
                )

    return selected


def prepare_array(
    data_array,
    latitude_name,
    longitude_name,
):
    data_array = data_array.squeeze(
        drop=True
    )

    unexpected = [
        dimension
        for dimension in data_array.dims
        if dimension not in {
            latitude_name,
            longitude_name,
        }
    ]

    if unexpected:
        raise ValueError(
            "Unexpected dimensions remain after time selection: "
            f"{unexpected}; variable dimensions are "
            f"{data_array.dims}."
        )

    data_array = data_array.transpose(
        latitude_name,
        longitude_name,
    )

    result = np.asarray(
        data_array.values,
        dtype="float32",
    )

    if result.ndim != 2:
        raise ValueError(
            f"Expected a two-dimensional field; got {result.shape}."
        )

    return result


def saturation_vapor_pressure_hpa(
    temperature_c,
):
    """
    Saturation vapor pressure over liquid water.

    This approximation is intended for near-surface meteorological
    preprocessing, not for mixed-phase upper-atmosphere humidity.
    """
    return (
        6.112
        * np.exp(
            17.67
            * temperature_c
            / (temperature_c + 243.5)
        )
    )


def relative_humidity_pct(
    temperature_c,
    dewpoint_c,
):
    vapor_pressure = saturation_vapor_pressure_hpa(
        dewpoint_c
    )

    saturation_pressure = saturation_vapor_pressure_hpa(
        temperature_c
    )

    relative_humidity = (
        100.0
        * vapor_pressure
        / saturation_pressure
    )

    return np.clip(
        relative_humidity,
        0.0,
        100.0,
    )


def wind_direction_from_deg(
    u_component,
    v_component,
):
    """
    Meteorological direction from which wind originates.

    0 degrees is wind from north.
    90 degrees is wind from east.
    """
    direction = (
        np.rad2deg(
            np.arctan2(
                -u_component,
                -v_component,
            )
        )
        + 360.0
    ) % 360.0

    calm = np.hypot(
        u_component,
        v_component,
    ) < 1.0e-6

    return np.where(
        calm,
        np.nan,
        direction,
    )


def orient_array(
    array,
    latitudes,
    longitudes,
):
    if latitudes[0] < latitudes[-1]:
        array = np.flipud(array)

    if longitudes[0] > longitudes[-1]:
        array = np.fliplr(array)

    return array


def write_geotiff(
    path,
    array,
    transform,
    description,
    timestamp,
    overwrite,
):
    if path.exists() and not overwrite:
        raise FileExistsError(
            f"Output already exists: {path}"
        )

    stored = np.where(
        np.isfinite(array),
        array,
        -9999.0,
    ).astype("float32")

    profile = {
        "driver": "GTiff",
        "height": stored.shape[0],
        "width": stored.shape[1],
        "count": 1,
        "dtype": "float32",
        "crs": "EPSG:4326",
        "transform": transform,
        "nodata": -9999.0,
        "compress": "deflate",
        "predictor": 3,
        "tiled": True,
        "BIGTIFF": "IF_SAFER",
    }

    with rasterio.open(
        path,
        "w",
        **profile,
    ) as destination:
        destination.write(stored, 1)
        destination.set_band_description(
            1,
            description,
        )
        destination.update_tags(
            timestamp_utc=timestamp.strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
        )


def main():
    args = parse_arguments()

    if args.accumulation_seconds <= 0:
        raise ValueError(
            "--accumulation-seconds must be positive."
        )

    output_directory = Path(
        args.out_dir
    ).expanduser().resolve()

    output_directory.mkdir(
        parents=True,
        exist_ok=True,
    )

    manifest_path = (
        Path(args.manifest).expanduser().resolve()
        if args.manifest
        else output_directory
        / "era5_raster_manifest.csv"
    )

    dataset = xr.open_dataset(
        args.input_nc,
        decode_times=True,
    )

    time_coordinate = find_coordinate(
        dataset,
        ["valid_time", "time"],
    )

    time_dimensions = dataset[
        time_coordinate
    ].dims

    if len(time_dimensions) != 1:
        raise ValueError(
            f"Unexpected time-coordinate dimensions: "
            f"{time_dimensions}"
        )

    time_dimension = time_dimensions[0]

    latitude_name = find_coordinate(
        dataset,
        ["latitude", "lat"],
    )

    longitude_name = find_coordinate(
        dataset,
        ["longitude", "lon"],
    )

    latitudes = np.asarray(
        dataset[latitude_name].values
    )

    longitudes = np.asarray(
        dataset[longitude_name].values
    )

    if (
        latitudes.ndim != 1
        or longitudes.ndim != 1
    ):
        raise ValueError(
            "Latitude and longitude coordinates must be "
            "one-dimensional."
        )

    if (
        len(latitudes) < 2
        or len(longitudes) < 2
    ):
        raise ValueError(
            "At least two coordinates are needed on each axis."
        )

    latitude_resolution = abs(
        float(latitudes[1] - latitudes[0])
    )

    longitude_resolution = abs(
        float(longitudes[1] - longitudes[0])
    )

    west = float(
        longitudes.min()
        - longitude_resolution / 2.0
    )
    east = float(
        longitudes.max()
        + longitude_resolution / 2.0
    )
    south = float(
        latitudes.min()
        - latitude_resolution / 2.0
    )
    north = float(
        latitudes.max()
        + latitude_resolution / 2.0
    )

    selected_times = select_time_indices(
        dataset,
        time_coordinate,
        args.times,
    )

    manifest_records = []

    for time_index, timestamp in selected_times:
        selected = dataset.isel(
            {time_dimension: time_index}
        )

        raw = {}

        for variable_type, aliases in (
            VARIABLE_ALIASES.items()
        ):
            raw[variable_type] = prepare_array(
                find_variable(
                    selected,
                    aliases,
                ),
                latitude_name,
                longitude_name,
            )

        temperature_c = (
            raw["temperature"] - 273.15
        )

        dewpoint_c = (
            raw["dewpoint"] - 273.15
        )

        relative_humidity = relative_humidity_pct(
            temperature_c,
            dewpoint_c,
        )

        wind_speed = np.hypot(
            raw["u10"],
            raw["v10"],
        )

        wind_direction = wind_direction_from_deg(
            raw["u10"],
            raw["v10"],
        )

        if args.radiation_already_w_m2:
            ssrd_w_m2 = raw["ssrd"]
            direct_horizontal_w_m2 = raw[
                "direct_horizontal"
            ]
        else:
            ssrd_w_m2 = (
                raw["ssrd"]
                / args.accumulation_seconds
            )

            direct_horizontal_w_m2 = (
                raw["direct_horizontal"]
                / args.accumulation_seconds
            )

        if args.precipitation_already_mm:
            precipitation_mm = raw[
                "precipitation"
            ]
        else:
            precipitation_mm = (
                raw["precipitation"] * 1000.0
            )

        outputs = {
            "air_temperature_c": (
                temperature_c,
                "ambient_air_temperature_c",
            ),
            "dewpoint_temperature_c": (
                dewpoint_c,
                "dewpoint_temperature_c",
            ),
            "relative_humidity_pct": (
                relative_humidity,
                "relative_humidity_percent",
            ),
            "u10_m_s": (
                raw["u10"],
                "eastward_wind_component_at_10m_m_s",
            ),
            "v10_m_s": (
                raw["v10"],
                "northward_wind_component_at_10m_m_s",
            ),
            "wind_speed_10m_m_s": (
                wind_speed,
                "wind_speed_at_10m_m_s",
            ),
            "wind_direction_from_deg": (
                wind_direction,
                "meteorological_wind_direction_from_deg",
            ),
            "surface_pressure_pa": (
                raw["pressure"],
                "surface_air_pressure_pa",
            ),
            "total_precipitation_mm": (
                precipitation_mm,
                "hourly_total_precipitation_mm",
            ),
            "ssrd_w_m2": (
                np.maximum(ssrd_w_m2, 0.0),
                "global_horizontal_irradiance_w_m2",
            ),
            "direct_horizontal_w_m2": (
                np.maximum(
                    direct_horizontal_w_m2,
                    0.0,
                ),
                "direct_horizontal_irradiance_w_m2",
            ),
        }

        tag = timestamp_tag(timestamp)

        for prefix, (
            array,
            description,
        ) in outputs.items():
            array = orient_array(
                array,
                latitudes,
                longitudes,
            )

            transform = from_bounds(
                west,
                south,
                east,
                north,
                array.shape[1],
                array.shape[0],
            )

            output_path = (
                output_directory
                / f"{prefix}_{tag}.tif"
            )

            write_geotiff(
                output_path,
                array,
                transform,
                description,
                timestamp,
                args.overwrite,
            )

            manifest_records.append(
                {
                    "timestamp_utc":
                        timestamp.strftime(
                            "%Y-%m-%dT%H:%M:%SZ"
                        ),
                    "timestamp_tag": tag,
                    "parameter": prefix,
                    "raster_path": str(
                        output_path
                    ),
                }
            )

            print(f"Wrote: {output_path}")

    manifest_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    with manifest_path.open(
        "w",
        newline="",
        encoding="utf-8",
    ) as manifest_file:
        writer = csv.DictWriter(
            manifest_file,
            fieldnames=[
                "timestamp_utc",
                "timestamp_tag",
                "parameter",
                "raster_path",
            ],
        )

        writer.writeheader()
        writer.writerows(
            manifest_records
        )

    dataset.close()

    print(f"Manifest: {manifest_path}")


if __name__ == "__main__":
    main()


