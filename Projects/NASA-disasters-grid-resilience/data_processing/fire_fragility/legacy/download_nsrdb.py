#!/usr/bin/env python3

"""
Subset NSRDB data for a region and day and write local NetCDF4.

Default behavior
----------------
- Source: NSRDB v3 annual HDF5 through NLR HSDS.
- Temporal output: all 24 UTC hours for --date.
- Native 30-minute records are averaged to hourly records.
- Wind speed and direction are averaged as vector components.
- Region: supplied with either --boundary or --bbox.

The output uses a site dimension because the NSRDB source is stored as
time by location. extract_nsrdb_rasters.py converts the sites to rasters.

HSDS configuration
------------------
Install h5pyd and configure ~/.hscfg:

    hs_endpoint = https://developer.nrel.gov/api/hsds
    hs_api_key = YOUR_API_KEY

Alternatively, supply a locally downloaded annual NSRDB HDF5 file
with --source.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from pathlib import Path

import h5py
import h5pyd
import numpy as np
import pandas as pd
from netCDF4 import Dataset
from shapely import intersects, points


DEFAULT_VARIABLES = [
    "air_temperature",
    "relative_humidity",
    "surface_pressure",
    "wind_speed",
    "wind_direction",
    "ghi",
    "dni",
    "dhi",
    "solar_zenith_angle",
]




from h5pyd._apps.config import Config




def parse_arguments():
    parser = argparse.ArgumentParser()

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
            "Optional UTC hours, such as 00 06 12 18 or "
            "00:00 06:00. The default is every hour from "
            "00:00 through 23:00."
        ),
    )

    region = parser.add_mutually_exclusive_group(
        required=True
    )

    region.add_argument(
        "--boundary",
        help=(
            "Polygon vector file defining the analysis region. "
            "All layers are reprojected to EPSG:4326."
        ),
    )

    region.add_argument(
        "--bbox",
        nargs=4,
        type=float,
        metavar=("WEST", "SOUTH", "EAST", "NORTH"),
        help="Region bounding box in EPSG:4326.",
    )

    parser.add_argument(
        "--source",
        help=(
            "Local annual HDF5 filename or remote HSDS domain. "
            "The default is /nrel/nsrdb/v3/nsrdb_<YEAR>.h5."
        ),
    )

    parser.add_argument(
        "--variables",
        nargs="+",
        default=DEFAULT_VARIABLES,
        help="NSRDB datasets to retrieve.",
    )

    parser.add_argument(
        "--hourly-method",
        choices=["mean", "select"],
        default="mean",
        help=(
            "'mean' averages native records within each UTC hour. "
            "'select' retains only records stamped at minute zero."
        ),
    )

    parser.add_argument(
        "--max-spatial-gap",
        type=int,
        default=8,
        help=(
            "Maximum number of unselected source columns included "
            "when grouping HSDS reads. Default: 8."
        ),
    )

    parser.add_argument(
        "--output",
        required=True,
        help="Output NetCDF4 filename.",
    )

    parser.add_argument(
        "--overwrite",
        action="store_true",
    )

    return parser.parse_args()


def parse_hours(values):
    if values is None:
        return list(range(24))

    hours = []

    for value in values:
        text = str(value).strip()

        if ":" in text:
            hour_text, minute_text = text.split(":", 1)

            if int(minute_text) != 0:
                raise ValueError(
                    f"Only whole UTC hours are supported: {value}"
                )

            hour = int(hour_text)
        else:
            hour = int(text)

        if not 0 <= hour <= 23:
            raise ValueError(
                f"Hour must be between 0 and 23: {value}"
            )

        if hour not in hours:
            hours.append(hour)

    return sorted(hours)


def decode_text(value):
    if isinstance(value, bytes):
        return value.decode("utf-8")

    return str(value)


def decode_attribute(value, default=""):
    if value is None:
        return default

    if isinstance(value, bytes):
        return value.decode("utf-8")

    if isinstance(value, np.ndarray) and value.size == 1:
        return decode_attribute(value.item(), default)

    return str(value)


@contextmanager
def open_nsrdb(source):
    """
    Open either a local HDF5 file or an HSDS domain.

    Remote HSDS authentication is read by h5pyd from ~/.hscfg.
    """
    source_path = Path(source).expanduser()

    cfg = Config()

    print(cfg["hs_endpoint"], cfg["hs_api_key"])
    print(source_path)

    if source_path.exists():
        handle = h5py.File(domain=source_path, mode="r",
            endpoint=cfg["hs_endpoint"],
        api_key=cfg["hs_api_key"])
    else:
        handle = h5pyd.File(domain=source, mode="r",
            endpoint=cfg["hs_endpoint"],
        api_key=cfg["hs_api_key"],)

    try:
        yield handle
    finally:
        handle.close()


def read_coordinates(source):
    """
    Return latitude and longitude for every NSRDB site.

    The preferred source is the coordinates dataset. A structured
    metadata table is used as a fallback.
    """
    if "coordinates" in source:
        coordinates = np.asarray(
            source["coordinates"][...],
            dtype="float64",
        )

        if coordinates.ndim != 2 or coordinates.shape[1] != 2:
            raise ValueError(
                "Unexpected NSRDB coordinates array shape: "
                f"{coordinates.shape}"
            )

        first = coordinates[:, 0]
        second = coordinates[:, 1]

        # Western-CONUS longitude values make the ordering unambiguous.
        if (
            np.nanmin(first) >= -90
            and np.nanmax(first) <= 90
            and np.nanmin(second) >= -180
            and np.nanmax(second) <= 180
        ):
            latitude = first
            longitude = second
        elif (
            np.nanmin(second) >= -90
            and np.nanmax(second) <= 90
            and np.nanmin(first) >= -180
            and np.nanmax(first) <= 180
        ):
            longitude = first
            latitude = second
        else:
            raise ValueError(
                "Could not identify latitude and longitude columns."
            )

        return latitude, longitude

    if "meta" not in source:
        raise KeyError(
            "The source has neither coordinates nor meta datasets."
        )

    metadata = source["meta"][...]

    if metadata.dtype.names is None:
        raise ValueError(
            "The NSRDB meta dataset is not a structured table."
        )

    lookup = {
        name.lower(): name
        for name in metadata.dtype.names
    }

    latitude_name = lookup.get("latitude")
    longitude_name = lookup.get("longitude")

    if not latitude_name or not longitude_name:
        raise KeyError(
            "Latitude and longitude fields were not found in meta."
        )

    return (
        np.asarray(
            metadata[latitude_name],
            dtype="float64",
        ),
        np.asarray(
            metadata[longitude_name],
            dtype="float64",
        ),
    )


def read_boundary(path):
    import geopandas as gpd

    boundary = gpd.read_file(path)

    if boundary.crs is None:
        raise ValueError(
            "The boundary dataset does not have a defined CRS."
        )

    boundary = boundary.to_crs("EPSG:4326")
    boundary = boundary[
        boundary.geometry.notna()
        & ~boundary.geometry.is_empty
    ]

    if boundary.empty:
        raise ValueError(
            "The boundary dataset has no valid geometries."
        )

    if hasattr(boundary.geometry, "union_all"):
        geometry = boundary.geometry.union_all()
    else:
        geometry = boundary.geometry.unary_union

    return geometry


def select_sites(
    latitude,
    longitude,
    boundary_path=None,
    bbox=None,
):
    if bbox:
        west, south, east, north = bbox
    else:
        geometry = read_boundary(boundary_path)
        west, south, east, north = geometry.bounds

    if west >= east or south >= north:
        raise ValueError(
            "The region bounds are invalid."
        )

    candidate = (
        np.isfinite(latitude)
        & np.isfinite(longitude)
        & (longitude >= west)
        & (longitude <= east)
        & (latitude >= south)
        & (latitude <= north)
    )

    candidate_indices = np.flatnonzero(candidate)

    if boundary_path:
        candidate_points = points(
            longitude[candidate_indices],
            latitude[candidate_indices],
        )

        inside = intersects(
            geometry,
            candidate_points,
        )

        selected = candidate_indices[
            np.asarray(inside, dtype=bool)
        ]
    else:
        selected = candidate_indices

    if selected.size == 0:
        raise ValueError(
            "No NSRDB sites intersect the requested region."
        )

    return np.sort(selected.astype("int64"))


def read_time_index(source):
    if "time_index" not in source:
        raise KeyError(
            "The NSRDB source does not contain time_index."
        )

    raw = source["time_index"][...]
    decoded = [decode_text(value) for value in raw]

    times = pd.to_datetime(
        decoded,
        utc=True,
        errors="raise",
    )

    return pd.DatetimeIndex(times)


def create_time_groups(
    source_times,
    date_string,
    hours,
    method,
):
    day = pd.Timestamp(
        date_string,
        tz="UTC",
    ).normalize()

    if day.year != source_times[0].year:
        raise ValueError(
            "The requested date and annual source file "
            "refer to different years."
        )

    groups = []

    for hour in hours:
        output_time = day + pd.Timedelta(
            hours=hour
        )

        if method == "mean":
            indices = np.flatnonzero(
                source_times.floor("h")
                == output_time
            )
        else:
            indices = np.flatnonzero(
                source_times == output_time
            )

        if indices.size == 0:
            raise ValueError(
                "No source record was found for "
                f"{output_time.isoformat()}."
            )

        groups.append(
            (output_time, indices)
        )

    return groups


def spatial_read_groups(
    selected_gids,
    max_gap,
):
    """
    Group nearby GIDs to reduce the number of remote HSDS requests.

    Each result is:
        selected_start, selected_stop, remote_start, remote_stop
    """
    start_position = 0

    for position in range(
        1,
        selected_gids.size,
    ):
        gap = (
            selected_gids[position]
            - selected_gids[position - 1]
            - 1
        )

        if gap > max_gap:
            yield (
                start_position,
                position,
                int(selected_gids[start_position]),
                int(selected_gids[position - 1]),
            )

            start_position = position

    yield (
        start_position,
        selected_gids.size,
        int(selected_gids[start_position]),
        int(selected_gids[-1]),
    )


def scale_dataset_values(dataset, raw):
    raw = np.asarray(raw)

    missing = np.zeros(
        raw.shape,
        dtype=bool,
    )

    for name in (
        "_FillValue",
        "missing_value",
        "fill_value",
        "psm_fill_value",
    ):
        if name in dataset.attrs:
            fill_value = np.asarray(
                dataset.attrs[name]
            ).reshape(-1)[0]

            missing |= raw == fill_value

    scale_factor = 1.0

    for name in (
        "psm_scale_factor",
        "psm_scale-factor",
        "scale_factor",
    ):
        if name in dataset.attrs:
            scale_factor = float(
                np.asarray(
                    dataset.attrs[name]
                ).reshape(-1)[0]
            )
            break

    if scale_factor == 0:
        raise ValueError(
            f"Invalid scale factor for {dataset.name}"
        )

    values = raw.astype(
        "float32"
    ) / scale_factor

    if (
        "add_offset" in dataset.attrs
        and "scale_factor" in dataset.attrs
    ):
        values += float(
            np.asarray(
                dataset.attrs["add_offset"]
            ).reshape(-1)[0]
        )

    values[missing] = np.nan

    return values


def read_dataset_subset(
    dataset,
    source_time_indices,
    selected_gids,
    max_gap,
):
    """
    Read a time/location subset without loading the entire annual field.
    """
    time_min = int(
        source_time_indices.min()
    )
    time_max = int(
        source_time_indices.max()
    )

    relative_times = (
        source_time_indices
        - time_min
    )

    output = np.full(
        (
            source_time_indices.size,
            selected_gids.size,
        ),
        np.nan,
        dtype="float32",
    )

    for (
        selected_start,
        selected_stop,
        remote_start,
        remote_stop,
    ) in spatial_read_groups(
        selected_gids,
        max_gap,
    ):
        block = dataset[
            time_min:time_max + 1,
            remote_start:remote_stop + 1,
        ]

        block = scale_dataset_values(
            dataset,
            block,
        )

        requested_columns = (
            selected_gids[
                selected_start:selected_stop
            ]
            - remote_start
        )

        output[
            :,
            selected_start:selected_stop,
        ] = block[
            relative_times[:, None],
            requested_columns[None, :],
        ]

    return output


def aggregate_scalar(
    raw,
    group_rows,
    method,
):
    output = np.full(
        (
            len(group_rows),
            raw.shape[1],
        ),
        np.nan,
        dtype="float32",
    )

    for output_index, rows in enumerate(
        group_rows
    ):
        if method == "select":
            output[output_index] = raw[
                rows[0]
            ]
        else:
            output[output_index] = np.nanmean(
                raw[rows],
                axis=0,
            )

    return output


def aggregate_wind(
    speed,
    direction_from_deg,
    group_rows,
    method,
):
    """
    Average meteorological wind as vector components.

    Direction follows the meteorological convention:
    degrees clockwise from north, indicating where wind comes from.
    """
    direction_rad = np.deg2rad(
        direction_from_deg
    )

    eastward = (
        -speed * np.sin(direction_rad)
    )

    northward = (
        -speed * np.cos(direction_rad)
    )

    output_speed = np.full(
        (
            len(group_rows),
            speed.shape[1],
        ),
        np.nan,
        dtype="float32",
    )

    output_direction = np.full_like(
        output_speed,
        np.nan,
    )

    for output_index, rows in enumerate(
        group_rows
    ):
        if method == "select":
            u_mean = eastward[rows[0]]
            v_mean = northward[rows[0]]
        else:
            u_mean = np.nanmean(
                eastward[rows],
                axis=0,
            )

            v_mean = np.nanmean(
                northward[rows],
                axis=0,
            )

        output_speed[output_index] = np.hypot(
            u_mean,
            v_mean,
        )

        output_direction[output_index] = (
            np.rad2deg(
                np.arctan2(
                    -u_mean,
                    -v_mean,
                )
            )
            % 360.0
        )

    calm = output_speed < 1.0e-6
    output_direction[calm] = np.nan

    return output_speed, output_direction


def create_output_file(
    output_path,
    output_times,
    latitude,
    longitude,
    selected_gids,
    overwrite,
):
    output_path = Path(
        output_path
    ).expanduser().resolve()

    if output_path.exists():
        if not overwrite:
            raise FileExistsError(
                f"Output already exists: {output_path}"
            )

        output_path.unlink()

    output_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    destination = Dataset(
        output_path,
        mode="w",
        format="NETCDF4",
    )

    destination.createDimension(
        "time",
        len(output_times),
    )

    destination.createDimension(
        "site",
        selected_gids.size,
    )

    time_variable = destination.createVariable(
        "time",
        "i8",
        ("time",),
    )

    time_variable.units = (
        "seconds since 1970-01-01 00:00:00 UTC"
    )

    time_variable.calendar = "standard"
    time_variable.standard_name = "time"

    time_variable[:] = (
        output_times.asi8 // 1_000_000_000
    )

    site_variable = destination.createVariable(
        "source_gid",
        "i8",
        ("site",),
    )

    site_variable[:] = selected_gids

    latitude_variable = destination.createVariable(
        "latitude",
        "f8",
        ("site",),
    )

    latitude_variable.units = "degrees_north"
    latitude_variable.standard_name = "latitude"
    latitude_variable[:] = latitude[selected_gids]

    longitude_variable = destination.createVariable(
        "longitude",
        "f8",
        ("site",),
    )

    longitude_variable.units = "degrees_east"
    longitude_variable.standard_name = "longitude"
    longitude_variable[:] = longitude[selected_gids]

    destination.title = (
        "Regional hourly subset of NSRDB"
    )

    destination.Conventions = "CF-1.8"
    destination.hourly_timestamp_definition = (
        "UTC hour beginning at the filename/output timestamp"
    )

    return destination, output_path


def create_data_variable(
    destination,
    name,
    units,
    source_name,
):
    variable = destination.createVariable(
        name,
        "f4",
        ("time", "site"),
        fill_value=-9999.0,
        zlib=True,
        complevel=4,
        shuffle=True,
        chunksizes=(
            1,
            min(
                len(destination.dimensions["site"]),
                16384,
            ),
        ),
    )

    variable.units = units
    variable.source_dataset = source_name

    return variable


def main():
    args = parse_arguments()

    requested_date = pd.Timestamp(
        args.date
    )

    source_name = (
        args.source
        if args.source
        else (
            "/nsrdb/v2/"
            f"nsrdb_{requested_date.year}.h5"
        )
    )

    hours = parse_hours(
        args.hours
    )

    with open_nsrdb(source_name) as source:
        latitude, longitude = read_coordinates(
            source
        )

        selected_gids = select_sites(
            latitude=latitude,
            longitude=longitude,
            boundary_path=args.boundary,
            bbox=args.bbox,
        )

        source_times = read_time_index(
            source
        )

        groups = create_time_groups(
            source_times=source_times,
            date_string=args.date,
            hours=hours,
            method=args.hourly_method,
        )

        source_time_indices = np.unique(
            np.concatenate(
                [
                    indices
                    for _, indices in groups
                ]
            )
        )

        row_lookup = {
            int(source_index): local_index
            for local_index, source_index in enumerate(
                source_time_indices
            )
        }

        group_rows = [
            np.asarray(
                [
                    row_lookup[int(index)]
                    for index in indices
                ],
                dtype="int64",
            )
            for _, indices in groups
        ]

        output_times = pd.DatetimeIndex(
            [
                timestamp
                for timestamp, _ in groups
            ]
        )

        destination, output_path = (
            create_output_file(
                output_path=args.output,
                output_times=output_times,
                latitude=latitude,
                longitude=longitude,
                selected_gids=selected_gids,
                overwrite=args.overwrite,
            )
        )

        destination.source = source_name
        destination.source_product = "NSRDB"
        destination.hourly_method = (
            args.hourly_method
        )

        try:
            variables = list(
                dict.fromkeys(args.variables)
            )

            has_wind_pair = (
                "wind_speed" in variables
                and "wind_direction" in variables
            )

            scalar_variables = [
                name
                for name in variables
                if name not in {
                    "wind_speed",
                    "wind_direction",
                }
                or not has_wind_pair
            ]

            for name in scalar_variables:
                if name not in source:
                    raise KeyError(
                        f"NSRDB dataset not found: {name}. "
                        f"Available datasets include: "
                        f"{list(source.keys())}"
                    )

                dataset = source[name]

                raw = read_dataset_subset(
                    dataset=dataset,
                    source_time_indices=(
                        source_time_indices
                    ),
                    selected_gids=selected_gids,
                    max_gap=args.max_spatial_gap,
                )

                hourly = aggregate_scalar(
                    raw=raw,
                    group_rows=group_rows,
                    method=args.hourly_method,
                )

                units = decode_attribute(
                    dataset.attrs.get(
                        "psm_units",
                        dataset.attrs.get(
                            "units",
                            "",
                        ),
                    )
                )

                output_variable = (
                    create_data_variable(
                        destination=destination,
                        name=name,
                        units=units,
                        source_name=name,
                    )
                )

                output_variable[:] = np.ma.masked_invalid(
                    hourly
                )

                print(f"Wrote variable: {name}")

            if has_wind_pair:
                for name in (
                    "wind_speed",
                    "wind_direction",
                ):
                    if name not in source:
                        raise KeyError(
                            f"NSRDB dataset not found: {name}"
                        )

                speed_raw = read_dataset_subset(
                    dataset=source["wind_speed"],
                    source_time_indices=(
                        source_time_indices
                    ),
                    selected_gids=selected_gids,
                    max_gap=args.max_spatial_gap,
                )

                direction_raw = read_dataset_subset(
                    dataset=source[
                        "wind_direction"
                    ],
                    source_time_indices=(
                        source_time_indices
                    ),
                    selected_gids=selected_gids,
                    max_gap=args.max_spatial_gap,
                )

                (
                    hourly_speed,
                    hourly_direction,
                ) = aggregate_wind(
                    speed=speed_raw,
                    direction_from_deg=direction_raw,
                    group_rows=group_rows,
                    method=args.hourly_method,
                )

                speed_variable = create_data_variable(
                    destination=destination,
                    name="wind_speed",
                    units="m s-1",
                    source_name="wind_speed",
                )

                speed_variable[:] = np.ma.masked_invalid(
                    hourly_speed
                )

                direction_variable = (
                    create_data_variable(
                        destination=destination,
                        name="wind_direction",
                        units="degree",
                        source_name="wind_direction",
                    )
                )

                direction_variable.direction_convention = (
                    "meteorological direction from which "
                    "wind originates, clockwise from north"
                )

                direction_variable[:] = (
                    np.ma.masked_invalid(
                        hourly_direction
                    )
                )

                print("Wrote vector-averaged wind.")

        finally:
            destination.close()

    print(
        f"Selected NSRDB sites: {selected_gids.size}"
    )
    print(
        f"Hourly records written: {len(output_times)}"
    )
    print(f"NetCDF4 output: {output_path}")


if __name__ == "__main__":
    main()
