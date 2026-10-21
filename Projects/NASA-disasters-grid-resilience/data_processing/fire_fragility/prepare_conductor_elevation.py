#!/usr/bin/env python3
"""
Derive conductor elevation above mean sea level from a DEM.

    H_conductor(x, y) = H_terrain(x, y) + h_above_ground(x, y)

Inputs
------
dem            Single-band DEM GeoTIFF with a CRS (any grid/CRS).
height_raster  conductor_height_m.tif from rasterize_conductor_parameters.py.
               It defines both the output grid and the conductor mask
               (finite cells = conductor), and supplies per-cell height
               above ground, so the height is not configured twice.

Outputs
-------
--output        Conductor-masked elevation raster (m above the DEM's
                vertical datum), NaN/nodata outside conductors.
--summary-json  Statistics (min/mean/median/max), DEM provenance, and
                the selected scalar for downstream scalar use.

Assumptions
-----------
- DEM band scale/offset are applied, then vertical units are converted
  to metres. No vertical-datum transformation is performed: the result
  is in the DEM's own datum (NAVD88 for LANDFIRE/3DEP over CONUS).
- Air density in IEEE 738 depends only weakly on elevation (about 1 %
  per 100 m), so the scalar summary is adequate for short spans; the
  raster is preferred for long or steep lines.
"""

import argparse
import json
from pathlib import Path

import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.warp import reproject

from raster_utils import conductor_statistics, write_float_raster

UNIT_FACTORS = {
    "m": 1.0,
    "ft": 0.3048,
    "us_survey_ft": 1200.0 / 3937.0,
}

RESAMPLING = {
    "nearest": Resampling.nearest,
    "bilinear": Resampling.bilinear,
    "average": Resampling.average,
}


def parse_arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dem")
    parser.add_argument("height_raster")
    parser.add_argument(
        "--vertical-units", choices=sorted(UNIT_FACTORS), required=True
    )
    parser.add_argument(
        "--vertical-datum",
        required=True,
        help="Verified DEM vertical datum, recorded for provenance.",
    )
    parser.add_argument(
        "--resampling", choices=sorted(RESAMPLING), default="bilinear"
    )
    parser.add_argument(
        "--scalar-statistic",
        choices=["mean", "median", "min", "max"],
        default="mean",
    )
    parser.add_argument("--allow-partial-coverage", action="store_true")
    parser.add_argument("--output", required=True)
    parser.add_argument("--summary-json", required=True)
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


def main():
    args = parse_arguments()

    datum = args.vertical_datum.strip()
    if not datum or datum.lower() in {"unknown", "none", "unspecified"}:
        raise ValueError("Supply the verified DEM vertical datum.")

    output = Path(args.output)
    summary_path = Path(args.summary_json)
    for path in (output, summary_path):
        if path.exists() and not args.overwrite:
            raise FileExistsError(f"Output exists: {path}; use --overwrite.")

    with rasterio.open(args.height_raster) as template:
        if template.crs is None or template.count != 1:
            raise ValueError("Height raster must be single-band with a CRS.")
        height = (
            template.read(1, masked=True)
            .astype("float64")
            .filled(np.nan)
        )
        profile = template.profile.copy()
        dst_crs = template.crs
        dst_transform = template.transform
        shape = template.shape

    conductor = np.isfinite(height)
    if not conductor.any():
        raise ValueError("Height raster contains no conductor cells.")
    if (height[conductor] < 0).any():
        raise ValueError("Conductor height above ground is negative.")

    with rasterio.open(args.dem) as dem:
        if dem.crs is None or dem.count != 1:
            raise ValueError("DEM must be single-band with a CRS.")
        scale = float(dem.scales[0])
        offset = float(dem.offsets[0])
        if not np.isfinite([scale, offset]).all() or scale == 0:
            raise ValueError("Invalid DEM band scale/offset.")
        source = (
            dem.read(1, masked=True)
            .astype("float64")
            .filled(np.nan)
        )
        source = (source * scale + offset) * UNIT_FACTORS[args.vertical_units]

        terrain = np.full(shape, np.nan, dtype="float64")
        reproject(
            source=source,
            destination=terrain,
            src_transform=dem.transform,
            src_crs=dem.crs,
            src_nodata=np.nan,
            dst_transform=dst_transform,
            dst_crs=dst_crs,
            dst_nodata=np.nan,
            resampling=RESAMPLING[args.resampling],
        )
        dem_description = {
            "path": str(Path(args.dem).resolve()),
            "crs": dem.crs.to_string(),
            "resolution": list(dem.res),
            "scale": scale,
            "offset": offset,
        }

    missing = conductor & ~np.isfinite(terrain)
    if missing.any() and not args.allow_partial_coverage:
        raise ValueError(
            f"DEM does not cover {int(missing.sum())} of "
            f"{int(conductor.sum())} conductor cells."
        )

    elevation = np.where(conductor, terrain + height, np.nan)

    write_float_raster(
        output,
        elevation,
        profile,
        description="conductor_elevation_above_msl_m",
        tags={
            "vertical_datum": datum,
            "dem_vertical_units": args.vertical_units,
            "definition": "terrain elevation + conductor height above ground",
        },
    )

    stats = conductor_statistics(elevation, conductor)
    terrain_stats = conductor_statistics(terrain, conductor)
    scalar = stats[args.scalar_statistic]
    if scalar is None:
        raise ValueError("No finite conductor elevations were produced.")

    summary = {
        "conductor_elevation_raster": str(output.resolve()),
        "scalar_statistic": args.scalar_statistic,
        "scalar_elevation_msl_m": scalar,
        "conductor_elevation_m": stats,
        "terrain_elevation_m": terrain_stats,
        "missing_dem_cells": int(missing.sum()),
        "vertical_datum": datum,
        "dem_vertical_units": args.vertical_units,
        "resampling": args.resampling,
        "dem": dem_description,
    }
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    print(
        f"Conductor elevation ({datum}): {stats['min']:.1f} to "
        f"{stats['max']:.1f} m; {args.scalar_statistic} {scalar:.1f} m"
    )


if __name__ == "__main__":
    main()
