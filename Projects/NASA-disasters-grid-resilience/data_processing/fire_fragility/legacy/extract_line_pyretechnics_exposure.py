#!/usr/bin/env python3

"""
Extract limiting Pyretechnics exposure for each transmission line.

For each line:
1. Identify intersecting wildfire raster cells.
2. Find maximum fireline intensity.
3. If several cells share the maximum, select the earliest arrival.
4. Obtain the fire-spread normal azimuth at that cell.
5. Determine local conductor-axis azimuth.
6. Calculate the acute angle between the fire normal and conductor.

Primary output
--------------
The vector output is authoritative because each feature retains its
own line-specific values.

Raster outputs
--------------
Line attributes are also rasterized for downstream heat calculations.
When lines cross in the same cell, a raster cannot preserve both
records; use the vector output for engineering reporting.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path

import geopandas as gpd
import numpy as np
import rasterio
from pyproj import Geod, Transformer
from rasterio.features import geometry_mask, rasterize
from shapely.geometry import LineString, MultiLineString, Point, mapping


GEOD = Geod(ellps="WGS84")
NODATA = -9999.0


def parse_arguments():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "peak_intensity_raster",
        help="Peak Pyretechnics fireline intensity in kW/m.",
    )

    parser.add_argument(
        "transmission_lines",
    )

    parser.add_argument(
        "--line-id-field",
        required=True,
    )

    parser.add_argument(
        "--arrival-raster",
        help="Pyretechnics arrival time in minutes.",
    )

    parser.add_argument(
        "--normal-azimuth-raster",
        help=(
            "Pyretechnics spread-normal azimuth in degrees "
            "clockwise from north."
        ),
    )

    parser.add_argument(
        "--out-lines",
        required=True,
        help="Output GeoPackage filename.",
    )

    parser.add_argument(
        "--out-dir",
        required=True,
        help="Directory for line-property rasters.",
    )

    parser.add_argument(
        "--center-only",
        action="store_true",
        help=(
            "Use only cells whose center is intersected by a line. "
            "The default includes every touched cell."
        ),
    )

    return parser.parse_args()


def read_aligned_optional(path, reference):
    if not path:
        return None

    with rasterio.open(path) as source:
        if (
            source.crs != reference.crs
            or source.transform != reference.transform
            or source.width != reference.width
            or source.height != reference.height
        ):
            raise ValueError(
                f"Raster is not aligned with the intensity grid: {path}"
            )

        array = source.read(1).astype("float32")

        if source.nodata is not None:
            array[array == source.nodata] = np.nan

        return array


def line_parts(geometry):
    if isinstance(geometry, LineString):
        yield geometry
    elif isinstance(geometry, MultiLineString):
        yield from geometry.geoms


def nearest_segment(geometry, point):
    best_segment = None
    best_distance = np.inf

    for part in line_parts(geometry):
        coordinates = list(part.coords)

        for start, end in zip(
            coordinates[:-1],
            coordinates[1:],
        ):
            segment = LineString(
                [start, end]
            )

            distance = segment.distance(point)

            if distance < best_distance:
                best_distance = distance
                best_segment = segment

    return best_segment


def segment_axis_azimuth(segment, source_crs):
    if segment is None:
        return np.nan

    transformer = Transformer.from_crs(
        source_crs,
        "EPSG:4326",
        always_xy=True,
    )

    coordinates = list(segment.coords)

    x1, y1 = coordinates[0]
    x2, y2 = coordinates[-1]

    lon1, lat1 = transformer.transform(
        x1,
        y1,
    )
    lon2, lat2 = transformer.transform(
        x2,
        y2,
    )

    forward_azimuth, _, _ = GEOD.inv(
        lon1,
        lat1,
        lon2,
        lat2,
    )

    return forward_azimuth % 180.0


def acute_axis_angle(axis_a, axis_b):
    difference = abs(
        (axis_a % 180.0)
        - (axis_b % 180.0)
    )

    return min(
        difference,
        180.0 - difference,
    )


def parse_start_timestamp(tags):
    value = tags.get(
        "simulation_start_utc"
    )

    if not value:
        return None

    timestamp = datetime.fromisoformat(
        value.replace("Z", "+00:00")
    )

    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(
            tzinfo=timezone.utc
        )

    return timestamp.astimezone(timezone.utc)


def write_raster(path, array, profile, description):
    output_profile = profile.copy()
    output_profile.update(
        driver="GTiff",
        count=1,
        dtype="float32",
        nodata=NODATA,
        compress="deflate",
        predictor=3,
        tiled=True,
        BIGTIFF="IF_SAFER",
    )

    stored = np.where(
        np.isfinite(array),
        array,
        NODATA,
    ).astype("float32")

    with rasterio.open(path, "w", **output_profile) as destination:
        destination.write(stored, 1)
        destination.set_band_description(
            1,
            description,
        )


def main():
    args = parse_arguments()

    with rasterio.open(
        args.peak_intensity_raster
    ) as intensity_source:
        intensity = intensity_source.read(
            1
        ).astype("float32")

        if intensity_source.nodata is not None:
            intensity[
                intensity == intensity_source.nodata
            ] = np.nan

        profile = intensity_source.profile.copy()
        transform = intensity_source.transform
        raster_crs = intensity_source.crs
        raster_shape = (
            intensity_source.height,
            intensity_source.width,
        )
        tags = intensity_source.tags()

        arrival = read_aligned_optional(
            args.arrival_raster,
            intensity_source,
        )

        normal_azimuth = read_aligned_optional(
            args.normal_azimuth_raster,
            intensity_source,
        )

    simulation_start = parse_start_timestamp(
        tags
    )

    lines = gpd.read_file(
        args.transmission_lines
    )

    if lines.crs is None:
        raise ValueError(
            "Transmission lines do not have a defined CRS."
        )

    if args.line_id_field not in lines.columns:
        raise KeyError(
            f"Missing line ID field: {args.line_id_field}"
        )

    lines = lines.to_crs(
        raster_crs
    ).copy()

    maximum_values = []
    arrival_values = []
    arrival_timestamps = []
    normal_values = []
    conductor_azimuths = []
    incidence_angles = []
    cell_counts = []

    for _, feature in lines.iterrows():
        geometry = feature.geometry

        if geometry is None or geometry.is_empty:
            maximum_values.append(np.nan)
            arrival_values.append(np.nan)
            arrival_timestamps.append(None)
            normal_values.append(np.nan)
            conductor_azimuths.append(np.nan)
            incidence_angles.append(np.nan)
            cell_counts.append(0)
            continue

        selected = geometry_mask(
            [mapping(geometry)],
            out_shape=raster_shape,
            transform=transform,
            invert=True,
            all_touched=not args.center_only,
        )

        valid = (
            selected
            & np.isfinite(intensity)
            & (intensity >= 0.0)
        )

        valid_count = int(
            valid.sum()
        )
        cell_counts.append(valid_count)

        if valid_count == 0:
            maximum_values.append(np.nan)
            arrival_values.append(np.nan)
            arrival_timestamps.append(None)
            normal_values.append(np.nan)
            conductor_azimuths.append(np.nan)
            incidence_angles.append(np.nan)
            continue

        maximum = float(
            np.nanmax(
                intensity[valid]
            )
        )

        candidate_rows, candidate_columns = np.where(
            valid
            & np.isclose(
                intensity,
                maximum,
                rtol=1e-6,
                atol=1e-6,
            )
        )

        if (
            arrival is not None
            and len(candidate_rows) > 1
        ):
            candidate_arrivals = arrival[
                candidate_rows,
                candidate_columns,
            ]

            finite_arrivals = np.isfinite(
                candidate_arrivals
            )

            if finite_arrivals.any():
                local_index = np.nanargmin(
                    np.where(
                        finite_arrivals,
                        candidate_arrivals,
                        np.nan,
                    )
                )
            else:
                local_index = 0
        else:
            local_index = 0

        row = int(
            candidate_rows[local_index]
        )
        column = int(
            candidate_columns[local_index]
        )

        x, y = rasterio.transform.xy(
            transform,
            row,
            column,
            offset="center",
        )

        cell_point = Point(x, y)

        local_segment = nearest_segment(
            geometry,
            cell_point,
        )

        conductor_axis = segment_axis_azimuth(
            local_segment,
            raster_crs,
        )

        if arrival is not None:
            arrival_minutes = float(
                arrival[row, column]
            )
        else:
            arrival_minutes = np.nan

        if (
            simulation_start is not None
            and np.isfinite(arrival_minutes)
        ):
            arrival_timestamp = (
                simulation_start
                + timedelta(
                    minutes=arrival_minutes
                )
            ).isoformat().replace(
                "+00:00",
                "Z",
            )
        else:
            arrival_timestamp = None

        if normal_azimuth is not None:
            fire_normal = float(
                normal_azimuth[row, column]
            )
        else:
            fire_normal = np.nan

        if (
            np.isfinite(fire_normal)
            and np.isfinite(conductor_axis)
        ):
            incidence_angle = acute_axis_angle(
                fire_normal,
                conductor_axis,
            )
        else:
            incidence_angle = np.nan

        maximum_values.append(maximum)
        arrival_values.append(arrival_minutes)
        arrival_timestamps.append(arrival_timestamp)
        normal_values.append(fire_normal)
        conductor_azimuths.append(conductor_axis)
        incidence_angles.append(incidence_angle)

    lines["max_fli_kw_m"] = maximum_values
    lines["arrival_min"] = arrival_values
    lines["arrival_utc"] = arrival_timestamps
    lines["fire_norm_deg"] = normal_values
    lines["line_axis_deg"] = conductor_azimuths
    lines["fire_inc_deg"] = incidence_angles
    lines["fire_cells"] = cell_counts

    output_lines = Path(
        args.out_lines
    ).expanduser().resolve()

    output_lines.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    lines.to_file(
        output_lines,
        driver="GPKG",
    )

    output_directory = Path(
        args.out_dir
    ).expanduser().resolve()

    output_directory.mkdir(
        parents=True,
        exist_ok=True,
    )

    raster_fields = {
        "max_fli_kw_m": (
            "line_max_fireline_intensity_kw_m.tif",
            "maximum_fireline_intensity_for_line_kw_m",
        ),
        "arrival_min": (
            "line_fire_arrival_minutes.tif",
            "arrival_minutes_for_limiting_fire_cell",
        ),
        "fire_inc_deg": (
            "line_fire_normal_incidence_angle_deg.tif",
            "acute_angle_between_fire_normal_and_conductor_deg",
        ),
    }

    for field, (
        filename,
        description,
    ) in raster_fields.items():
        shapes = [
            (
                feature.geometry,
                float(feature[field]),
            )
            for _, feature in lines.iterrows()
            if feature.geometry is not None
            and not feature.geometry.is_empty
            and np.isfinite(feature[field])
        ]

        output_array = rasterize(
            shapes=shapes,
            out_shape=raster_shape,
            transform=transform,
            fill=np.nan,
            dtype="float32",
            all_touched=True,
        )

        write_raster(
            output_directory / filename,
            output_array,
            profile,
            description,
        )


if __name__ == "__main__":
    main()


