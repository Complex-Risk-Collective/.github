#!/usr/bin/env python3

"""
Calculate the maximum fireline intensity intersecting each conductor.

Inputs
------
1. A single-band fireline-intensity raster in kW/m.
2. A conductor centerline vector dataset.

Outputs
-------
1. A GeoTIFF on the fire-intensity grid. Every rasterized cell of each
   conductor is assigned that conductor's maximum intersecting fireline
   intensity.
2. A CSV containing one record per conductor.

Method
------
For each conductor:

1. Reproject the centerline to the fire raster CRS.
2. Rasterize the line onto the fire raster grid.
3. Find all valid fireline-intensity cells touched by the line.
4. Select the maximum value.
5. Assign that maximum to all rasterized cells of the line.

This implements the limiting-cell method used in the wildfire-DLR
manuscript rather than averaging intensity over the line.

Short conductors
----------------
By default, every raster cell touched by a line is included. This is
important for short conductors that may not cross a pixel center.

Use --center-only to select only cells whose centers are crossed.

Overlap handling
----------------
If multiple conductors occupy the same output cell, the output raster
stores the maximum line-level intensity. The CSV preserves each line's
individual result.

Units
-----
The fireline-intensity raster must be in kW/m unless --input-units W/m
is specified.
"""

from __future__ import annotations

import argparse
import csv
import math
import sys
from pathlib import Path

import geopandas as gpd
import numpy as np
import rasterio
from pyproj import Geod
from rasterio.features import rasterize
from shapely.geometry import box


FLOAT_NODATA = -9999.0
GEOD = Geod(ellps="WGS84")


def parse_arguments():
    parser = argparse.ArgumentParser(
        description=(
            "Find the maximum fireline intensity intersecting "
            "each conductor."
        )
    )

    parser.add_argument(
        "fireline_intensity_raster",
        help=(
            "Single-band fireline-intensity raster. Expected "
            "units are kW/m unless --input-units is changed."
        ),
    )

    parser.add_argument(
        "transmission_lines",
        help=(
            "Conductor centerline vector dataset, such as a "
            "GeoPackage, GeoJSON, or Shapefile."
        ),
    )

    parser.add_argument(
        "--layer",
        help=(
            "Optional vector layer name. Use when the source "
            "container has multiple layers."
        ),
    )

    parser.add_argument(
        "--line-id-field",
        required=True,
        help="Field uniquely identifying each conductor.",
    )

    parser.add_argument(
        "--input-units",
        choices=("kW/m", "W/m"),
        default="kW/m",
        help=(
            "Units of the fireline-intensity raster. "
            "Default: kW/m."
        ),
    )

    parser.add_argument(
        "--minimum-intensity",
        type=float,
        default=0.0,
        help=(
            "Minimum valid fireline intensity after conversion "
            "to kW/m. Default: 0."
        ),
    )

    parser.add_argument(
        "--center-only",
        action="store_true",
        help=(
            "Use only raster cells whose centers are crossed by "
            "the conductor. The default includes every touched cell."
        ),
    )

    parser.add_argument(
        "--out-raster",
        required=True,
        help="Output line-level maximum-intensity GeoTIFF.",
    )

    parser.add_argument(
        "--out-csv",
        required=True,
        help="Output line-level CSV table.",
    )

    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace existing outputs.",
    )

    return parser.parse_args()


def clean_float_profile(
    source_profile,
    width,
    height,
):
    """
    Create a clean tiled float32 GeoTIFF profile.

    Template-specific block metadata is removed to avoid invalid TIFF
    tile dimensions inherited from the source raster.
    """
    profile = source_profile.copy()

    for key in (
        "blockxsize",
        "blockysize",
        "tiled",
        "interleave",
        "compress",
        "predictor",
        "photometric",
        "nbits",
    ):
        profile.pop(key, None)

    def valid_tile_size(
        dimension,
        preferred=256,
    ):
        dimension = int(dimension)

        if dimension <= 0:
            raise ValueError(
                f"Invalid raster dimension: {dimension}"
            )

        limited = min(
            preferred,
            max(16, dimension),
        )

        return max(
            16,
            (limited // 16) * 16,
        )

    profile.update(
        driver="GTiff",
        width=width,
        height=height,
        count=1,
        dtype="float32",
        nodata=FLOAT_NODATA,
        compress="deflate",
        predictor=3,
        tiled=True,
        blockxsize=valid_tile_size(width),
        blockysize=valid_tile_size(height),
        BIGTIFF="IF_SAFER",
    )

    return profile


def write_float_raster(
    output_path,
    array,
    source_profile,
    description,
    overwrite,
):
    """
    Write a GeoTIFF atomically.

    Existing files are removed with pathlib before GDAL is called, so
    a corrupt TIFF cannot prevent overwrite.
    """
    output_path = Path(
        output_path
    ).expanduser().resolve()

    output_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    temporary_path = output_path.with_name(
        f".{output_path.name}.temporary"
    )

    if output_path.exists():
        if not overwrite:
            raise FileExistsError(
                f"Output already exists: {output_path}"
            )

        output_path.unlink()

    if temporary_path.exists():
        temporary_path.unlink()

    array = np.asarray(
        array,
        dtype="float32",
    )

    if array.ndim != 2:
        raise ValueError(
            "Output raster array must be two-dimensional; "
            f"received shape {array.shape}."
        )

    stored = np.where(
        np.isfinite(array),
        array,
        FLOAT_NODATA,
    ).astype("float32")

    profile = clean_float_profile(
        source_profile=source_profile,
        width=array.shape[1],
        height=array.shape[0],
    )

    try:
        with rasterio.open(
            temporary_path,
            "w",
            **profile,
        ) as destination:
            destination.write(
                stored,
                1,
            )

            destination.set_band_description(
                1,
                description,
            )

            destination.update_tags(
                units="kW/m",
                statistic=(
                    "maximum fireline intensity intersecting "
                    "each conductor"
                ),
            )

        # Validate the temporary TIFF before replacing the destination.
        with rasterio.open(
            temporary_path
        ) as validation:
            if validation.count != 1:
                raise RuntimeError(
                    "Output validation failed: expected one band."
                )

            if (
                validation.height != array.shape[0]
                or validation.width != array.shape[1]
            ):
                raise RuntimeError(
                    "Output validation failed: dimensions do not "
                    "match the source array."
                )

            validation.read(
                1,
                window=(
                    (
                        0,
                        min(1, validation.height),
                    ),
                    (
                        0,
                        min(1, validation.width),
                    ),
                ),
            )

        temporary_path.replace(
            output_path
        )

    except Exception:
        if temporary_path.exists():
            temporary_path.unlink()

        raise


def write_csv_atomic(
    output_path,
    records,
    overwrite,
):
    """Write the line-level result CSV atomically."""
    output_path = Path(
        output_path
    ).expanduser().resolve()

    output_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    temporary_path = output_path.with_name(
        f".{output_path.name}.temporary"
    )

    if output_path.exists():
        if not overwrite:
            raise FileExistsError(
                f"Output already exists: {output_path}"
            )

        output_path.unlink()

    if temporary_path.exists():
        temporary_path.unlink()

    fieldnames = [
        "line_id",
        "line_length_m",
        "rasterized_line_cells",
        "valid_intensity_cells",
        "maximum_fireline_intensity_kw_m",
        "mean_intersecting_fireline_intensity_kw_m",
        "maximum_source_row",
        "maximum_source_column",
        "maximum_source_x",
        "maximum_source_y",
        "status",
    ]

    try:
        with temporary_path.open(
            "w",
            newline="",
            encoding="utf-8",
        ) as destination:
            writer = csv.DictWriter(
                destination,
                fieldnames=fieldnames,
            )

            writer.writeheader()
            writer.writerows(records)

        temporary_path.replace(
            output_path
        )

    except Exception:
        if temporary_path.exists():
            temporary_path.unlink()

        raise


def geometry_length_metres(
    geometry,
    geometry_crs,
):
    """
    Calculate geodesic centerline length in metres.
    """
    geographic = gpd.GeoSeries(
        [geometry],
        crs=geometry_crs,
    ).to_crs("EPSG:4326").iloc[0]

    if geographic.geom_type == "LineString":
        line_parts = [geographic]

    elif geographic.geom_type == "MultiLineString":
        line_parts = list(
            geographic.geoms
        )

    else:
        raise TypeError(
            "Conductor geometry must be LineString or "
            f"MultiLineString; received {geographic.geom_type}."
        )

    length_m = 0.0

    for line in line_parts:
        coordinates = list(
            line.coords
        )

        for start, end in zip(
            coordinates[:-1],
            coordinates[1:],
        ):
            _, _, distance_m = GEOD.inv(
                start[0],
                start[1],
                end[0],
                end[1],
            )

            if math.isfinite(distance_m):
                length_m += distance_m

    return float(length_m)


def burn_line_mask(
    geometry,
    output_shape,
    transform,
    all_touched,
):
    """Rasterize one conductor geometry to a Boolean mask."""
    array = rasterize(
        [(geometry, 1)],
        out_shape=output_shape,
        transform=transform,
        fill=0,
        dtype="uint8",
        all_touched=all_touched,
    )

    return array.astype(bool)


def main():
    args = parse_arguments()

    fire_path = Path(
        args.fireline_intensity_raster
    ).expanduser().resolve()

    lines_path = Path(
        args.transmission_lines
    ).expanduser().resolve()

    if not fire_path.is_file():
        raise FileNotFoundError(
            f"Fireline-intensity raster does not exist: "
            f"{fire_path}"
        )

    if not lines_path.exists():
        raise FileNotFoundError(
            f"Conductor vector does not exist: {lines_path}"
        )

    with rasterio.open(
        fire_path
    ) as source:
        if source.crs is None:
            raise ValueError(
                "The fireline-intensity raster has no CRS."
            )

        source_profile = source.profile.copy()
        fire_crs = source.crs
        fire_transform = source.transform
        fire_shape = (
            source.height,
            source.width,
        )
        fire_bounds = box(
            source.bounds.left,
            source.bounds.bottom,
            source.bounds.right,
            source.bounds.top,
        )

        fire_intensity = source.read(
            1,
            masked=True,
        ).filled(np.nan).astype(
            "float32"
        )

    if args.input_units == "W/m":
        fire_intensity *= 0.001

    finite_source = fire_intensity[
        np.isfinite(fire_intensity)
    ]

    if finite_source.size == 0:
        raise ValueError(
            "The fireline-intensity raster contains no valid data."
        )

    if np.min(finite_source) < 0.0:
        raise ValueError(
            "The fireline-intensity raster contains negative "
            "values after unit conversion."
        )

    if args.minimum_intensity < 0.0:
        raise ValueError(
            "--minimum-intensity cannot be negative."
        )

    if args.layer:
        conductors = gpd.read_file(
            lines_path,
            layer=args.layer,
        )
    else:
        conductors = gpd.read_file(
            lines_path
        )

    if conductors.empty:
        raise ValueError(
            "The conductor vector contains no features."
        )

    if conductors.crs is None:
        raise ValueError(
            "The conductor vector has no defined CRS."
        )

    if (
        args.line_id_field
        not in conductors.columns
    ):
        raise KeyError(
            f"Line-ID field does not exist: "
            f"{args.line_id_field}"
        )

    line_ids = conductors[
        args.line_id_field
    ].astype(str)

    if line_ids.duplicated().any():
        duplicate_ids = sorted(
            set(
                line_ids[
                    line_ids.duplicated(
                        keep=False
                    )
                ]
            )
        )

        raise ValueError(
            "Line IDs must be unique. Duplicate IDs: "
            + ", ".join(duplicate_ids)
        )

    original_crs = conductors.crs
    original_geometries = (
        conductors.geometry.copy()
    )

    conductors = conductors.to_crs(
        fire_crs
    )

    intersects_raster = (
        conductors.geometry.intersects(
            fire_bounds
        )
    )

    if not intersects_raster.any():
        raise ValueError(
            "No conductor intersects the fireline-intensity "
            "raster extent."
        )

    output_intensity = np.full(
        fire_shape,
        np.nan,
        dtype="float32",
    )

    records = []

    all_touched = not args.center_only

    for position, (
        row_index,
        conductor,
    ) in enumerate(
        conductors.iterrows()
    ):
        line_id = str(
            conductor[
                args.line_id_field
            ]
        )

        original_geometry = (
            original_geometries.loc[
                row_index
            ]
        )

        if (
            conductor.geometry is None
            or conductor.geometry.is_empty
        ):
            records.append(
                {
                    "line_id": line_id,
                    "line_length_m": "",
                    "rasterized_line_cells": 0,
                    "valid_intensity_cells": 0,
                    "maximum_fireline_intensity_kw_m": "",
                    "mean_intersecting_fireline_intensity_kw_m": "",
                    "maximum_source_row": "",
                    "maximum_source_column": "",
                    "maximum_source_x": "",
                    "maximum_source_y": "",
                    "status": "empty_geometry",
                }
            )

            continue

        if conductor.geometry.geom_type not in {
            "LineString",
            "MultiLineString",
        }:
            raise TypeError(
                f"Line {line_id} geometry must be a line; "
                f"received {conductor.geometry.geom_type}."
            )

        line_length_m = geometry_length_metres(
            original_geometry,
            original_crs,
        )

        line_mask = burn_line_mask(
            geometry=conductor.geometry,
            output_shape=fire_shape,
            transform=fire_transform,
            all_touched=all_touched,
        )

        rasterized_cell_count = int(
            line_mask.sum()
        )

        if rasterized_cell_count == 0:
            records.append(
                {
                    "line_id": line_id,
                    "line_length_m": line_length_m,
                    "rasterized_line_cells": 0,
                    "valid_intensity_cells": 0,
                    "maximum_fireline_intensity_kw_m": "",
                    "mean_intersecting_fireline_intensity_kw_m": "",
                    "maximum_source_row": "",
                    "maximum_source_column": "",
                    "maximum_source_x": "",
                    "maximum_source_y": "",
                    "status": (
                        "no_rasterized_cells_use_all_touched"
                        if args.center_only
                        else "no_rasterized_cells"
                    ),
                }
            )

            continue

        valid_mask = (
            line_mask
            & np.isfinite(
                fire_intensity
            )
            & (
                fire_intensity
                >= args.minimum_intensity
            )
        )

        valid_cell_count = int(
            valid_mask.sum()
        )

        if valid_cell_count == 0:
            records.append(
                {
                    "line_id": line_id,
                    "line_length_m": line_length_m,
                    "rasterized_line_cells":
                        rasterized_cell_count,
                    "valid_intensity_cells": 0,
                    "maximum_fireline_intensity_kw_m": "",
                    "mean_intersecting_fireline_intensity_kw_m": "",
                    "maximum_source_row": "",
                    "maximum_source_column": "",
                    "maximum_source_x": "",
                    "maximum_source_y": "",
                    "status": (
                        "no_valid_fire_intensity_under_line"
                    ),
                }
            )

            continue

        intersecting_values = (
            fire_intensity[
                valid_mask
            ]
        )

        maximum_intensity = float(
            np.max(intersecting_values)
        )

        mean_intensity = float(
            np.mean(intersecting_values)
        )

        candidate_rows, candidate_columns = (
            np.where(
                valid_mask
                & np.isclose(
                    fire_intensity,
                    maximum_intensity,
                    rtol=1.0e-6,
                    atol=1.0e-7,
                )
            )
        )

        maximum_row = int(
            candidate_rows[0]
        )

        maximum_column = int(
            candidate_columns[0]
        )

        maximum_x, maximum_y = (
            rasterio.transform.xy(
                fire_transform,
                maximum_row,
                maximum_column,
                offset="center",
            )
        )

        # Assign the line-level maximum to all rasterized line cells.
        #
        # If multiple lines occupy one raster cell, preserve the
        # largest line-level maximum in the output raster.
        empty_output = (
            ~np.isfinite(
                output_intensity
            )
        )

        new_cells = (
            line_mask
            & empty_output
        )

        overlap_cells = (
            line_mask
            & ~empty_output
        )

        output_intensity[
            new_cells
        ] = maximum_intensity

        output_intensity[
            overlap_cells
        ] = np.maximum(
            output_intensity[
                overlap_cells
            ],
            maximum_intensity,
        )

        records.append(
            {
                "line_id": line_id,
                "line_length_m":
                    line_length_m,
                "rasterized_line_cells":
                    rasterized_cell_count,
                "valid_intensity_cells":
                    valid_cell_count,
                "maximum_fireline_intensity_kw_m":
                    maximum_intensity,
                "mean_intersecting_fireline_intensity_kw_m":
                    mean_intensity,
                "maximum_source_row":
                    maximum_row,
                "maximum_source_column":
                    maximum_column,
                "maximum_source_x":
                    float(maximum_x),
                "maximum_source_y":
                    float(maximum_y),
                "status": "ok",
            }
        )

        print(
            f"{line_id}: "
            f"maximum={maximum_intensity:.6f} kW/m, "
            f"valid cells={valid_cell_count}, "
            f"line cells={rasterized_cell_count}"
        )

    successful_records = [
        record
        for record in records
        if record["status"] == "ok"
    ]

    if not successful_records:
        raise RuntimeError(
            "No conductor had valid fireline-intensity cells. "
            "Check conductor location, CRS, raster extent, nodata, "
            "and --minimum-intensity."
        )

    write_float_raster(
        output_path=args.out_raster,
        array=output_intensity,
        source_profile=source_profile,
        description=(
            "maximum_fireline_intensity_intersecting_"
            "each_conductor"
        ),
        overwrite=args.overwrite,
    )

    write_csv_atomic(
        output_path=args.out_csv,
        records=records,
        overwrite=args.overwrite,
    )

    print(
        f"Successful conductors: "
        f"{len(successful_records)}/{len(records)}"
    )

    print(
        f"Output raster: "
        f"{Path(args.out_raster).expanduser().resolve()}"
    )

    print(
        f"Output CSV: "
        f"{Path(args.out_csv).expanduser().resolve()}"
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
