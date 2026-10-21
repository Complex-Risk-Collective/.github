#!/usr/bin/env python3
"""
Rasterize conductor-specific properties from centerline features.

Outputs (all on the template grid, nodata outside conductor cells)
-------------------------------------------------------------------
conductor_axis_azimuth_deg.tif   Undirected axis azimuth, 0-180 deg from
                                 true north (0 = N-S line). Defines the
                                 conductor mask for later steps.
solar_absorptivity.tif           Solar absorptivity (0-1).
emissivity.tif                   Thermal emissivity (0-1).
conductor_diameter_m.tif         Outside diameter (m).
conductor_height_m.tif           Height above local ground (m).
projected_area_per_length_m2_per_m.tif
                                 Equal to diameter for a cylinder; kept for
                                 traceability to the projected-area input.

Values come from vector attribute fields, falling back to defaults.

Limitation: a cell holds one value; where conductors overlap, the last
feature burned wins. Keep the vector for segment-level engineering work.
"""

import argparse
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
from pyproj import Geod, Transformer
from rasterio.features import rasterize
from shapely.geometry import LineString, MultiLineString

from raster_utils import write_float_raster

GEOD = Geod(ellps="WGS84")

OUTPUT_NAMES = (
    "conductor_axis_azimuth_deg.tif",
    "solar_absorptivity.tif",
    "emissivity.tif",
    "conductor_diameter_m.tif",
    "conductor_height_m.tif",
    "projected_area_per_length_m2_per_m.tif",
)


def iter_line_parts(geometry):
    if isinstance(geometry, LineString):
        yield geometry
    elif isinstance(geometry, MultiLineString):
        yield from geometry.geoms


def get_value(row, field, default, parameter_name):
    if field and field in row.index:
        value = row.get(field)
        if value is not None and not pd.isna(value):
            return float(value)
    if default is None:
        raise ValueError(
            f"No value was supplied for {parameter_name}. "
            "Provide an attribute field or a default value."
        )
    return float(default)


def parse_arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("lines", help="Conductor centerline vector dataset.")
    parser.add_argument("template", help="Raster defining the output grid.")
    parser.add_argument("--layer")
    parser.add_argument("--out-dir", required=True)

    parser.add_argument("--absorptivity-field", default="solar_abs")
    parser.add_argument("--emissivity-field", default="emissivity")
    parser.add_argument("--diameter-field", default="diameter_m")
    parser.add_argument("--height-field", default="height_m")

    parser.add_argument("--default-absorptivity", type=float)
    parser.add_argument("--default-emissivity", type=float)
    parser.add_argument("--default-diameter-m", type=float)
    parser.add_argument("--default-height-m", type=float)

    parser.add_argument(
        "--all-touched",
        action="store_true",
        help="Burn every raster cell touched by a conductor segment.",
    )
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


def main():
    args = parse_arguments()
    output_directory = Path(args.out_dir)

    existing = [
        output_directory / name
        for name in OUTPUT_NAMES
        if (output_directory / name).exists()
    ]
    if existing and not args.overwrite:
        raise FileExistsError(
            f"Outputs exist (use --overwrite): {existing[0]}"
        )

    with rasterio.open(args.template) as template:
        profile = template.profile.copy()
        output_shape = (template.height, template.width)
        output_crs = template.crs
        output_transform = template.transform

    conductors = (
        gpd.read_file(args.lines, layer=args.layer)
        if args.layer
        else gpd.read_file(args.lines)
    )
    if conductors.crs is None:
        raise ValueError("The conductor dataset has no CRS.")
    conductors = conductors.to_crs(output_crs)

    to_wgs84 = Transformer.from_crs(
        output_crs, "EPSG:4326", always_xy=True
    ).transform

    shapes = {name: [] for name in OUTPUT_NAMES}

    for _, row in conductors.iterrows():
        absorptivity = get_value(
            row, args.absorptivity_field, args.default_absorptivity,
            "solar absorptivity",
        )
        emissivity = get_value(
            row, args.emissivity_field, args.default_emissivity,
            "emissivity",
        )
        diameter = get_value(
            row, args.diameter_field, args.default_diameter_m,
            "outside conductor diameter",
        )
        height = get_value(
            row, args.height_field, args.default_height_m,
            "conductor height",
        )

        for name, value in (
            ("solar absorptivity", absorptivity),
            ("emissivity", emissivity),
        ):
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be in [0, 1]; got {value}.")
        if diameter <= 0:
            raise ValueError("Conductor diameter must be positive.")
        if height <= 0:
            raise ValueError("Conductor height must be positive.")

        for part in iter_line_parts(row.geometry):
            coordinates = list(part.coords)
            for start, end in zip(coordinates[:-1], coordinates[1:]):
                segment = LineString([start[:2], end[:2]])
                lon1, lat1 = to_wgs84(*segment.coords[0])
                lon2, lat2 = to_wgs84(*segment.coords[1])
                forward_azimuth, _, _ = GEOD.inv(lon1, lat1, lon2, lat2)
                # Undirected axis: 0 and 180 degrees are equivalent.
                axis = forward_azimuth % 180.0

                for name, value in zip(
                    OUTPUT_NAMES,
                    (axis, absorptivity, emissivity, diameter, height,
                     diameter),
                ):
                    shapes[name].append((segment, value))

    if not shapes[OUTPUT_NAMES[0]]:
        raise ValueError("No line segments were found in the conductor data.")

    for name in OUTPUT_NAMES:
        array = rasterize(
            shapes=shapes[name],
            out_shape=output_shape,
            transform=output_transform,
            fill=np.nan,
            dtype="float32",
            all_touched=args.all_touched,
        )
        write_float_raster(
            output_directory / name,
            array,
            profile,
            description=Path(name).stem,
        )

    cells = int(np.isfinite(array).sum())
    if cells == 0:
        raise ValueError(
            "Conductor does not intersect the template grid."
        )
    print(f"Rasterized {cells} conductor cells to {output_directory}")


if __name__ == "__main__":
    main()
