#!/usr/bin/env python3

"""
Prepare a standardized conductor-vector dataset.

Supported source modes
----------------------
endpoints:
    Construct one conductor from two endpoint coordinates.

vector:
    Read an existing GeoPackage, GeoJSON, Shapefile, or other
    GeoPandas-supported vector layer and map its attributes into
    the standardized schema.

Standardized fields
-------------------
line_id:
    Unique conductor or line identifier.

height_m:
    Conductor height above local ground in metres.

diameter_m:
    Outside conductor diameter in metres, if supplied.

solar_abs:
    Conductor solar absorptivity, if supplied.

emissivity:
    Conductor thermal emissivity, if supplied.

The output geometry remains a vector centerline. Rasterization is
performed later by rasterize_conductor_parameters.py.
"""

from __future__ import annotations

import argparse
import math
import shutil
import sys
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
import yaml
from pyproj import CRS, Geod
from shapely.geometry import LineString, box


GEOD = Geod(ellps="WGS84")

STANDARD_FIELDS = [
    "line_id",
    "height_m",
    "diameter_m",
    "solar_abs",
    "emissivity",
    "length_m",
    "axis_azimuth_deg",
    "source_type",
]


def parse_arguments():
    parser = argparse.ArgumentParser(
        description=(
            "Prepare a conductor vector from endpoint coordinates "
            "or an existing vector dataset."
        )
    )

    parser.add_argument(
        "--config",
        required=True,
        help="Pipeline YAML configuration.",
    )

    parser.add_argument(
        "--template",
        help=(
            "Optional template raster used to verify that the "
            "conductor intersects the analysis grid. Overrides "
            "grid.template_raster from the YAML."
        ),
    )

    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace an existing prepared vector.",
    )

    return parser.parse_args()


def load_config(path):
    config_path = Path(
        path
    ).expanduser().resolve()

    if not config_path.is_file():
        raise FileNotFoundError(
            f"Configuration does not exist: {config_path}"
        )

    with config_path.open(
        "r",
        encoding="utf-8",
    ) as source:
        config = yaml.safe_load(source)

    if not isinstance(config, dict):
        raise ValueError(
            "The YAML root must be a mapping."
        )

    return config, config_path


def resolve_path(value, config_path):
    if value is None:
        return None

    path = Path(value).expanduser()

    if not path.is_absolute():
        path = config_path.parent / path

    return path.resolve()


def optional_float(value, field_name):
    if value is None:
        return np.nan

    try:
        result = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(
            f"{field_name} must be numeric or null; "
            f"received {value!r}."
        ) from error

    if not math.isfinite(result):
        raise ValueError(
            f"{field_name} must be finite when supplied."
        )

    return result


def required_float(value, field_name):
    result = optional_float(
        value,
        field_name,
    )

    if not np.isfinite(result):
        raise ValueError(
            f"{field_name} is required."
        )

    return result


def validate_lon_lat(
    longitude,
    latitude,
    point_name,
):
    if not -180.0 <= longitude <= 180.0:
        raise ValueError(
            f"{point_name} longitude is outside "
            f"[-180, 180]: {longitude}"
        )

    if not -90.0 <= latitude <= 90.0:
        raise ValueError(
            f"{point_name} latitude is outside "
            f"[-90, 90]: {latitude}"
        )


def calculate_line_properties(
    geometry,
    source_crs,
):
    geographic = gpd.GeoSeries(
        [geometry],
        crs=source_crs,
    ).to_crs("EPSG:4326").iloc[0]

    coordinates = list(
        geographic.coords
    )

    if len(coordinates) < 2:
        raise ValueError(
            "A conductor centerline must contain "
            "at least two coordinates."
        )

    total_length_m = 0.0

    first_nonzero_azimuth = None

    for start, end in zip(
        coordinates[:-1],
        coordinates[1:],
    ):
        lon1, lat1 = start
        lon2, lat2 = end

        forward_azimuth, _, distance_m = (
            GEOD.inv(
                lon1,
                lat1,
                lon2,
                lat2,
            )
        )

        total_length_m += distance_m

        if (
            first_nonzero_azimuth is None
            and distance_m > 0.0
        ):
            # Treat the conductor as an undirected axis:
            # 0 degrees and 180 degrees are equivalent.
            first_nonzero_azimuth = (
                forward_azimuth % 180.0
            )

    if total_length_m <= 0.0:
        raise ValueError(
            "The conductor endpoints produce a zero-length line."
        )

    if first_nonzero_azimuth is None:
        raise ValueError(
            "Could not determine conductor azimuth."
        )

    return (
        float(total_length_m),
        float(first_nonzero_azimuth),
    )


def create_from_endpoints(
    conductor_config,
):
    endpoint_config = conductor_config.get(
        "endpoints",
        {},
    )

    source_crs = endpoint_config.get(
        "crs",
        "EPSG:4326",
    )

    CRS.from_user_input(source_crs)

    p1 = endpoint_config.get("p1", {})
    p2 = endpoint_config.get("p2", {})

    lon1 = required_float(
        p1.get("lon"),
        "conductors.endpoints.p1.lon",
    )
    lat1 = required_float(
        p1.get("lat"),
        "conductors.endpoints.p1.lat",
    )
    lon2 = required_float(
        p2.get("lon"),
        "conductors.endpoints.p2.lon",
    )
    lat2 = required_float(
        p2.get("lat"),
        "conductors.endpoints.p2.lat",
    )

    # These validations assume the endpoint input is geographic.
    if CRS.from_user_input(source_crs).is_geographic:
        validate_lon_lat(
            lon1,
            lat1,
            "p1",
        )
        validate_lon_lat(
            lon2,
            lat2,
            "p2",
        )

    geometry = LineString(
        [
            (lon1, lat1),
            (lon2, lat2),
        ]
    )

    length_m, azimuth_deg = (
        calculate_line_properties(
            geometry,
            source_crs,
        )
    )

    line_id = str(
        endpoint_config.get(
            "line_id",
            "conductor_001",
        )
    )

    if not line_id.strip():
        raise ValueError(
            "The conductor line ID cannot be empty."
        )

    height_m = required_float(
        endpoint_config.get("height_m"),
        "conductors.endpoints.height_m",
    )

    if height_m <= 0.0:
        raise ValueError(
            "Conductor height must be positive."
        )

    diameter_m = optional_float(
        endpoint_config.get("diameter_m"),
        "conductors.endpoints.diameter_m",
    )

    if (
        np.isfinite(diameter_m)
        and diameter_m <= 0.0
    ):
        raise ValueError(
            "Conductor diameter must be positive."
        )

    absorptivity = optional_float(
        endpoint_config.get(
            "solar_absorptivity"
        ),
        (
            "conductors.endpoints."
            "solar_absorptivity"
        ),
    )

    emissivity = optional_float(
        endpoint_config.get("emissivity"),
        "conductors.endpoints.emissivity",
    )

    for field_name, value in (
        ("solar_absorptivity", absorptivity),
        ("emissivity", emissivity),
    ):
        if (
            np.isfinite(value)
            and not 0.0 <= value <= 1.0
        ):
            raise ValueError(
                f"{field_name} must be between 0 and 1."
            )

    frame = gpd.GeoDataFrame(
        [
            {
                "line_id": line_id,
                "height_m": height_m,
                "diameter_m": diameter_m,
                "solar_abs": absorptivity,
                "emissivity": emissivity,
                "length_m": length_m,
                "axis_azimuth_deg": azimuth_deg,
                "source_type": "endpoints",
                "geometry": geometry,
            }
        ],
        crs=source_crs,
    )

    return frame


def first_present(
    row,
    field_name,
    default,
):
    if field_name:
        if field_name not in row.index:
            raise KeyError(
                f"Configured source field does not exist: "
                f"{field_name}"
            )

        value = row[field_name]

        if (
            value is not None
            and not pd.isna(value)
        ):
            return value

    return default


def create_from_vector(
    conductor_config,
    config_path,
):
    vector_config = conductor_config.get(
        "vector",
        {},
    )

    source_path = resolve_path(
        vector_config.get("path"),
        config_path,
    )

    if (
        source_path is None
        or not source_path.is_file()
    ):
        raise FileNotFoundError(
            "conductors.vector.path is required "
            "in vector mode."
        )

    layer = vector_config.get("layer")

    if layer:
        source = gpd.read_file(
            source_path,
            layer=layer,
        )
    else:
        source = gpd.read_file(
            source_path
        )

    if source.empty:
        raise ValueError(
            "The conductor vector contains no features."
        )

    if source.crs is None:
        raise ValueError(
            "The conductor vector has no defined CRS."
        )

    field_mapping = {
        "line_id":
            vector_config.get(
                "line_id_field",
                "line_id",
            ),
        "height_m":
            vector_config.get(
                "height_field",
                "height_m",
            ),
        "diameter_m":
            vector_config.get(
                "diameter_field",
                "diameter_m",
            ),
        "solar_abs":
            vector_config.get(
                "absorptivity_field",
                "solar_abs",
            ),
        "emissivity":
            vector_config.get(
                "emissivity_field",
                "emissivity",
            ),
    }

    defaults = {
        "line_id": None,
        "height_m":
            vector_config.get(
                "default_height_m"
            ),
        "diameter_m":
            vector_config.get(
                "default_diameter_m"
            ),
        "solar_abs":
            vector_config.get(
                "default_absorptivity"
            ),
        "emissivity":
            vector_config.get(
                "default_emissivity"
            ),
    }

    records = []

    for index, row in source.iterrows():
        geometry = row.geometry

        if (
            geometry is None
            or geometry.is_empty
        ):
            continue

        if geometry.geom_type not in {
            "LineString",
            "MultiLineString",
        }:
            raise TypeError(
                "Conductor geometries must be lines; "
                f"received {geometry.geom_type}."
            )

        line_id = first_present(
            row,
            field_mapping["line_id"],
            defaults["line_id"],
        )

        if line_id is None:
            line_id = f"conductor_{index}"

        height_m = optional_float(
            first_present(
                row,
                field_mapping["height_m"],
                defaults["height_m"],
            ),
            "height_m",
        )

        diameter_m = optional_float(
            first_present(
                row,
                field_mapping["diameter_m"],
                defaults["diameter_m"],
            ),
            "diameter_m",
        )

        absorptivity = optional_float(
            first_present(
                row,
                field_mapping["solar_abs"],
                defaults["solar_abs"],
            ),
            "solar_abs",
        )

        emissivity = optional_float(
            first_present(
                row,
                field_mapping["emissivity"],
                defaults["emissivity"],
            ),
            "emissivity",
        )

        length_m, azimuth_deg = (
            calculate_line_properties(
                geometry,
                source.crs,
            )
        )

        records.append(
            {
                "line_id": str(line_id),
                "height_m": height_m,
                "diameter_m": diameter_m,
                "solar_abs": absorptivity,
                "emissivity": emissivity,
                "length_m": length_m,
                "axis_azimuth_deg":
                    azimuth_deg,
                "source_type": "vector",
                "geometry": geometry,
            }
        )

    if not records:
        raise ValueError(
            "No usable conductor centerlines were found."
        )

    return gpd.GeoDataFrame(
        records,
        crs=source.crs,
    )


def verify_template_intersection(
    frame,
    template_path,
):
    if template_path is None:
        return

    if not template_path.is_file():
        raise FileNotFoundError(
            f"Template raster does not exist: "
            f"{template_path}"
        )

    with rasterio.open(template_path) as template:
        if template.crs is None:
            raise ValueError(
                "Template raster has no CRS."
            )

        bounds = template.bounds
        template_crs = template.crs

    projected = frame.to_crs(
        template_crs
    )

    bbox = box(bounds.left, bounds.bottom, bounds.right, bounds.top)
    bounds_polygon = gpd.GeoSeries([bbox]).set_crs(template_crs).iloc[0]

    intersects = projected.geometry.intersects(
        bounds_polygon
    )

    if not intersects.any():
        raise ValueError(
            "No prepared conductor intersects the "
            "template-raster extent."
        )

    outside_ids = projected.loc[
        ~intersects,
        "line_id",
    ].tolist()

    if outside_ids:
        print(
            "WARNING: Conductors outside template extent: "
            + ", ".join(
                str(item)
                for item in outside_ids
            ),
            file=sys.stderr,
        )


def remove_existing_geopackage(
    output_path,
):
    if output_path.exists():
        output_path.unlink()

    # Remove possible SQLite sidecar files left by interrupted writes.
    for suffix in (
        "-shm",
        "-wal",
        "-journal",
    ):
        sidecar = Path(
            f"{output_path}{suffix}"
        )

        if sidecar.exists():
            sidecar.unlink()


def main():
    args = parse_arguments()

    config, config_path = load_config(
        args.config
    )

    conductor_config = config.get(
        "conductors"
    )

    if not isinstance(
        conductor_config,
        dict,
    ):
        raise KeyError(
            "The YAML requires a conductors section."
        )

    source_type = str(
        conductor_config.get(
            "source_type",
            "endpoints",
        )
    ).lower()

    if source_type == "endpoints":
        frame = create_from_endpoints(
            conductor_config
        )

    elif source_type == "vector":
        frame = create_from_vector(
            conductor_config,
            config_path,
        )

    else:
        raise ValueError(
            "conductors.source_type must be "
            "'endpoints' or 'vector'."
        )

    if frame["line_id"].duplicated().any():
        duplicates = frame.loc[
            frame["line_id"].duplicated(
                keep=False
            ),
            "line_id",
        ].tolist()

        raise ValueError(
            "Duplicate conductor line IDs: "
            + ", ".join(
                str(item)
                for item in duplicates
            )
        )

    output_path = resolve_path(
        conductor_config.get(
            "prepared_vector",
            "../work/conductor_input/conductors.gpkg",
        ),
        config_path,
    )

    layer_name = conductor_config.get(
        "prepared_layer",
        "conductors",
    )

    template_value = (
        args.template
        or config.get(
            "grid",
            {},
        ).get(
            "template_raster"
        )
    )

    template_path = (
        resolve_path(
            template_value,
            config_path,
        )
        if template_value
        else None
    )

    verify_template_intersection(
        frame,
        template_path,
    )

    if output_path.exists():
        if not args.overwrite:
            raise FileExistsError(
                f"Output already exists: {output_path}"
            )

        remove_existing_geopackage(
            output_path
        )

    output_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    frame.to_file(
        output_path,
        layer=layer_name,
        driver="GPKG",
    )

    print(f"Prepared vector: {output_path}")
    print(f"Layer: {layer_name}")
    print(f"Source type: {source_type}")
    print(f"Feature count: {len(frame)}")

    for _, row in frame.iterrows():
        print(
            f"  {row['line_id']}: "
            f"length={row['length_m']:.3f} m, "
            f"axis_azimuth="
            f"{row['axis_azimuth_deg']:.3f} degrees, "
            f"height={row['height_m']:.3f} m"
        )


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(
            f"ERROR: {error}",
            file=sys.stderr,
        )
        sys.exit(1)
