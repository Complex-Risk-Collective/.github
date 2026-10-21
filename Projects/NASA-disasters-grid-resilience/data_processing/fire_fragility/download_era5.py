#!/usr/bin/env python3

"""
Download ERA5 hourly single-level data as GRIB and convert it to NetCDF4.

Default behavior
----------------
If --hours is omitted, all 24 UTC hours are requested:

    00:00, 01:00, ..., 23:00

Requirements
------------
Python:
    pip install cdsapi netCDF4

System:
    ECMWF ecCodes, including grib_to_netcdf.

Conda:
    conda install -c conda-forge eccodes cdsapi netcdf4

Ubuntu/Debian:
    sudo apt-get install libeccodes-tools
"""

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

import cdsapi
from netCDF4 import Dataset


ERA5_DATASET = "reanalysis-era5-single-levels"


ERA5_VARIABLES = [
    "2m_temperature",
    "2m_dewpoint_temperature",
    "10m_u_component_of_wind",
    "10m_v_component_of_wind",
    "surface_pressure",
    "total_precipitation",
    "surface_solar_radiation_downwards",
    "total_sky_direct_solar_radiation_at_surface",
]

ALL_HOURS = [
    f"{hour:02d}:00"
    for hour in range(24)
]


def parse_arguments():
    parser = argparse.ArgumentParser(
        description=(
            "Download ERA5 data as GRIB and convert it "
            "locally to NetCDF4."
        )
    )

    parser.add_argument(
        "--date",
        required=True,
        help="UTC date in YYYY-MM-DD format.",
    )

    parser.add_argument(
        "--hours",
        nargs="+",
        default=None,
        help=(
            "Optional UTC hours, for example: "
            "--hours 12:00 13:00 14:00. "
            "If omitted, all 24 UTC hours are downloaded."
        ),
    )

    parser.add_argument(
        "--bbox",
        nargs=4,
        type=float,
        required=True,
        metavar=("NORTH", "WEST", "SOUTH", "EAST"),
        help=(
            "Bounding box in geographic coordinates: "
            "north west south east."
        ),
    )

    parser.add_argument(
        "--output",
        required=True,
        help="Output NetCDF4 filename with .nc or .nc4 extension.",
    )

    parser.add_argument(
        "--grib-output",
        help=(
            "Optional intermediate GRIB filename. If omitted, "
            "the name is derived from --output."
        ),
    )

    parser.add_argument(
        "--keep-grib",
        action="store_true",
        help="Retain the intermediate GRIB file.",
    )

    parser.add_argument(
        "--compression-level",
        type=int,
        choices=range(0, 10),
        default=4,
        metavar="0-9",
        help="NetCDF4 deflate compression level. Default: 4.",
    )

    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace existing GRIB or NetCDF output files.",
    )

    return parser.parse_args()


def validate_date(date_string):
    parts = date_string.split("-")

    if len(parts) != 3:
        raise ValueError(
            "--date must use YYYY-MM-DD format."
        )

    year, month, day = parts

    if not (
        len(year) == 4
        and len(month) == 2
        and len(day) == 2
        and year.isdigit()
        and month.isdigit()
        and day.isdigit()
    ):
        raise ValueError(
            "--date must use YYYY-MM-DD format."
        )

    return year, month, day


def validate_hours(hours):
    validated = []

    for value in hours:
        try:
            hour_text, minute_text = value.split(":")
            hour = int(hour_text)
            minute = int(minute_text)
        except (ValueError, AttributeError):
            raise ValueError(
                f"Invalid hour '{value}'; expected HH:MM."
            )

        if not 0 <= hour <= 23:
            raise ValueError(
                f"Hour is outside the range 00-23: {value}"
            )

        if minute != 0:
            raise ValueError(
                f"ERA5 hourly requests require whole hours: {value}"
            )

        normalized = f"{hour:02d}:00"

        if normalized not in validated:
            validated.append(normalized)

    return validated


def validate_bbox(bbox):
    north, west, south, east = bbox

    if not -90.0 <= south <= 90.0:
        raise ValueError(
            f"Invalid south latitude: {south}"
        )

    if not -90.0 <= north <= 90.0:
        raise ValueError(
            f"Invalid north latitude: {north}"
        )

    if south >= north:
        raise ValueError(
            "South latitude must be less than north latitude."
        )

    if not -180.0 <= west <= 180.0:
        raise ValueError(
            f"Invalid west longitude: {west}"
        )

    if not -180.0 <= east <= 180.0:
        raise ValueError(
            f"Invalid east longitude: {east}"
        )

    if west >= east:
        raise ValueError(
            "West longitude must be less than east longitude. "
            "Split dateline-crossing regions into separate requests."
        )


def determine_paths(args):
    netcdf_path = Path(
        args.output
    ).expanduser().resolve()

    if netcdf_path.suffix.lower() not in {".nc", ".nc4"}:
        raise ValueError(
            "--output must have a .nc or .nc4 extension."
        )

    if args.grib_output:
        grib_path = Path(
            args.grib_output
        ).expanduser().resolve()
    else:
        grib_path = netcdf_path.with_suffix(".grib")

    if grib_path == netcdf_path:
        raise ValueError(
            "The GRIB and NetCDF paths must be different."
        )

    return grib_path, netcdf_path


def prepare_output_paths(
    grib_path,
    netcdf_path,
    overwrite,
):
    existing = [
        path
        for path in (grib_path, netcdf_path)
        if path.exists()
    ]

    if existing and not overwrite:
        listing = "\n".join(
            f"  {path}" for path in existing
        )

        raise FileExistsError(
            "Output files already exist:\n"
            f"{listing}\n"
            "Use --overwrite to replace them."
        )

    if overwrite:
        for path in existing:
            path.unlink()

    grib_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    netcdf_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )


def find_grib_converter():
    converter = shutil.which("grib_to_netcdf")

    if converter is None:
        raise RuntimeError(
            "Could not find grib_to_netcdf on PATH.\n"
            "Install ECMWF ecCodes, for example:\n"
            "  conda install -c conda-forge eccodes"
        )

    return converter


def download_grib(
    date_string,
    hours,
    bbox,
    grib_path,
):
    year, month, day = validate_date(date_string)

    request = {
        "product_type": ["reanalysis"],
        "variable": ERA5_VARIABLES,
        "year": [year],
        "month": [month],
        "day": [day],
        "time": hours,
        "area": bbox,
        "data_format": "grib",
        "download_format": "unarchived",
    }

    print(
        f"Requesting {len(hours)} ERA5 hour(s): "
        f"{', '.join(hours)}"
    )
    print(f"Downloading GRIB to: {grib_path}")

    cdsapi.Client().retrieve(
        ERA5_DATASET,
        request,
        str(grib_path),
    )

    if not grib_path.exists():
        raise RuntimeError(
            "The CDS request completed without creating "
            "the expected GRIB file."
        )

    if grib_path.stat().st_size == 0:
        raise RuntimeError(
            "The downloaded GRIB file is empty."
        )


def convert_grib_to_netcdf4(
    grib_path,
    netcdf_path,
    compression_level,
    converter,
):
    temporary_path = netcdf_path.with_name(
        f"{netcdf_path.name}.temporary"
    )

    if temporary_path.exists():
        temporary_path.unlink()

    command = [
        converter,
        "-k",
        "3",
        "-d",
        str(compression_level),
        "-D",
        "NC_FLOAT",
        "-o",
        str(temporary_path),
        str(grib_path),
    ]

    print("Converting GRIB to NetCDF4:")
    print(" ".join(command))

    try:
        result = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
        )

        if result.stdout:
            print(result.stdout)

        if result.stderr:
            print(result.stderr, file=sys.stderr)

    except subprocess.CalledProcessError as error:
        if temporary_path.exists():
            temporary_path.unlink()

        message = [
            "GRIB-to-NetCDF4 conversion failed.",
            f"Command: {' '.join(command)}",
        ]

        if error.stdout:
            message.append(
                f"Standard output:\n{error.stdout}"
            )

        if error.stderr:
            message.append(
                f"Standard error:\n{error.stderr}"
            )

        raise RuntimeError(
            "\n".join(message)
        ) from error

    if (
        not temporary_path.exists()
        or temporary_path.stat().st_size == 0
    ):
        raise RuntimeError(
            "grib_to_netcdf did not create a valid output file."
        )

    temporary_path.replace(netcdf_path)


def verify_netcdf4(netcdf_path):
    try:
        with Dataset(netcdf_path, mode="r") as dataset:
            data_model = dataset.data_model
            dimensions = list(dataset.dimensions)
            variables = list(dataset.variables)

            time_size = None

            for time_name in ("valid_time", "time"):
                if time_name in dataset.variables:
                    time_size = dataset.variables[
                        time_name
                    ].size
                    break

    except Exception as error:
        raise RuntimeError(
            f"Cannot open converted output as NetCDF: "
            f"{netcdf_path}"
        ) from error

    if data_model not in {
        "NETCDF4",
        "NETCDF4_CLASSIC",
    }:
        raise RuntimeError(
            "Converted file is not NetCDF4. "
            f"Detected data model: {data_model}"
        )

    if not variables:
        raise RuntimeError(
            "Converted NetCDF contains no variables."
        )

    print(f"Verified data model: {data_model}")
    print(f"Dimensions: {', '.join(dimensions)}")
    print(f"Variables: {', '.join(variables)}")

    if time_size is not None:
        print(f"Available time records: {time_size}")


def main():
    args = parse_arguments()

    # Default to all available hours for the requested date.
    requested_hours = (
        ALL_HOURS
        if args.hours is None
        else args.hours
    )

    requested_hours = validate_hours(
        requested_hours
    )

    validate_bbox(args.bbox)

    grib_path, netcdf_path = determine_paths(args)

    prepare_output_paths(
        grib_path,
        netcdf_path,
        args.overwrite,
    )

    converter = find_grib_converter()

    try:
        download_grib(
            date_string=args.date,
            hours=requested_hours,
            bbox=args.bbox,
            grib_path=grib_path,
        )

        convert_grib_to_netcdf4(
            grib_path=grib_path,
            netcdf_path=netcdf_path,
            compression_level=args.compression_level,
            converter=converter,
        )

        verify_netcdf4(netcdf_path)

    except Exception:
        if grib_path.exists():
            print(
                f"Intermediate GRIB retained at: {grib_path}",
                file=sys.stderr,
            )

        raise

    if args.keep_grib:
        print(
            f"Intermediate GRIB retained at: {grib_path}"
        )
    else:
        grib_path.unlink()
        print("Intermediate GRIB removed.")

    print(
        f"NetCDF4 output written to: {netcdf_path}"
    )


if __name__ == "__main__":
    main()


