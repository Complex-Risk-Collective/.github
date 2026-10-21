#!/usr/bin/env python3

"""
Calculate the acute wind angle relative to a conductor axis.

Inputs
------
u_wind_raster:
    Eastward wind component in m/s.

v_wind_raster:
    Northward wind component in m/s.

axis_azimuth_raster:
    Conductor-axis azimuth in degrees clockwise from geographic north.
    Values are normalized to an undirected axis using modulo 180.

Output convention
-----------------
0 degrees:
    Wind is parallel to the conductor.

90 degrees:
    Wind is perpendicular to the conductor.

Grid handling
-------------
The conductor-axis raster defines the output CRS, transform, dimensions,
and conductor mask.

Wind components are reprojected independently using bilinear
resampling. The components must already describe geographic eastward
and northward wind, as in the ERA5 inputs used by this pipeline.

This script does not rotate grid-relative vector components and does
not adjust wind height. Use conductor-height wind inputs if required.

Calm wind
---------
Wind direction is undefined at zero speed and unreliable near zero.
Cells below --calm-threshold are written as nodata.

This preserves the previous pipeline behavior. Downstream rating code
must not interpret missing angles or missing ratings as zero capacity.

Dependencies
------------
    numpy
    rasterio

No raster_utils.py dependency is required.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from tempfile import NamedTemporaryFile

import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.warp import reproject


NODATA = -9999.0


def parse_arguments():
    parser = argparse.ArgumentParser(
        description=(
            "Calculate the acute wind angle relative to "
            "the conductor axis."
        )
    )

    parser.add_argument(
        "u_wind_raster",
        help="Geographic eastward wind component in m/s.",
    )

    parser.add_argument(
        "v_wind_raster",
        help="Geographic northward wind component in m/s.",
    )

    parser.add_argument(
        "axis_azimuth_raster",
        help=(
            "Conductor-axis azimuth raster in degrees "
            "clockwise from geographic north."
        ),
    )

    parser.add_argument(
        "--calm-threshold",
        type=float,
        default=0.1,
        help=(
            "Wind speeds below this threshold receive nodata. "
            "Exactly zero speed always receives nodata. "
            "Default: 0.1 m/s."
        ),
    )

    parser.add_argument(
        "--output",
        required=True,
        help="Output relative-angle GeoTIFF.",
    )

    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace an existing output.",
    )

    return parser.parse_args()


def read_template(path):
    """Read conductor azimuth and output-grid metadata."""
    with rasterio.open(path) as source:
        if source.crs is None:
            raise ValueError(
                f"Conductor-axis raster has no CRS: {path}"
            )

        if source.count != 1:
            raise ValueError(
                "Conductor-axis input must be single-band."
            )

        azimuth = (
            source.read(1, masked=True)
            .astype("float32")
            .filled(np.nan)
        )

        template = {
            "crs": source.crs,
            "transform": source.transform,
            "height": source.height,
            "width": source.width,
            "shape": (source.height, source.width),
            "tags": source.tags(),
        }

    finite = np.isfinite(azimuth)

    if not finite.any():
        raise ValueError(
            "Conductor-axis raster contains no valid cells."
        )

    if np.any(
        finite & ((azimuth < 0.0) | (azimuth > 360.0))
    ):
        raise ValueError(
            "Conductor azimuth must be between 0 and 360 "
            "degrees; use raster nodata for background cells."
        )

    return azimuth, template


def read_aligned_component(path, template):
    """Align one wind component to the conductor grid."""
    destination = np.full(
        template["shape"],
        np.nan,
        dtype="float32",
    )

    with rasterio.open(path) as source:
        if source.crs is None:
            raise ValueError(
                f"Wind raster has no CRS: {path}"
            )

        if source.count != 1:
            raise ValueError(
                f"Wind raster must be single-band: {path}"
            )

        data = (
            source.read(1, masked=True)
            .astype("float32")
            .filled(np.nan)
        )

        same_grid = (
            source.crs == template["crs"]
            and source.transform == template["transform"]
            and data.shape == template["shape"]
        )

        if same_grid:
            return data

        reproject(
            source=data,
            destination=destination,
            src_transform=source.transform,
            src_crs=source.crs,
            src_nodata=np.nan,
            dst_transform=template["transform"],
            dst_crs=template["crs"],
            dst_nodata=np.nan,
            resampling=Resampling.bilinear,
            init_dest_nodata=True,
        )

    return destination


def compute_relative_angle(
    u_wind,
    v_wind,
    conductor_azimuth,
    calm_threshold,
):
    """
    Return relative angle, wind speed, and cell-validity masks.
    """
    conductor_cells = np.isfinite(conductor_azimuth)

    covered_cells = (
        conductor_cells
        & np.isfinite(u_wind)
        & np.isfinite(v_wind)
    )

    speed = np.hypot(u_wind, v_wind)

    calm_cells = (
        covered_cells
        & (
            (speed < calm_threshold)
            | (speed == 0.0)
        )
    )

    valid_cells = covered_cells & ~calm_cells

    # atan2(eastward, northward) gives direction TOWARD which
    # the wind travels, clockwise from geographic north.
    #
    # Wind-from versus wind-toward does not change the acute
    # angle to an undirected conductor axis.
    wind_axis = np.mod(
        np.degrees(np.arctan2(u_wind, v_wind)),
        180.0,
    )

    conductor_axis = np.mod(
        conductor_azimuth,
        180.0,
    )

    difference = np.abs(
        wind_axis - conductor_axis
    )

    angle = np.minimum(
        difference,
        180.0 - difference,
    )

    angle = np.where(
        valid_cells,
        np.clip(angle, 0.0, 90.0),
        np.nan,
    ).astype("float32")

    return (
        angle,
        speed,
        conductor_cells,
        covered_cells,
        calm_cells,
        valid_cells,
    )


def write_raster(
    output_path,
    angle,
    template,
    input_paths,
    calm_threshold,
    overwrite,
):
    """
    Write to a new temporary TIFF, then replace the destination.

    Creating a fresh profile prevents inherited invalid block sizes.
    Filesystem replacement does not require GDAL to open an existing,
    possibly corrupted destination TIFF.
    """
    if output_path.exists() and not overwrite:
        raise FileExistsError(
            f"Output exists: {output_path}. "
            "Use --overwrite to replace it."
        )

    output_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    profile = {
        "driver": "GTiff",
        "width": template["width"],
        "height": template["height"],
        "count": 1,
        "dtype": "float32",
        "crs": template["crs"],
        "transform": template["transform"],
        "nodata": NODATA,
        "compress": "deflate",
        "predictor": 3,
        "tiled": False,
        "BIGTIFF": "IF_SAFER",
    }

    stored = np.where(
        np.isfinite(angle),
        angle,
        NODATA,
    ).astype("float32")

    # Create a unique name, then remove the empty placeholder so
    # Rasterio creates an entirely new dataset.
    with NamedTemporaryFile(
        prefix=f".{output_path.stem}_",
        suffix=".tif",
        dir=output_path.parent,
        delete=False,
    ) as temporary:
        temporary_path = Path(temporary.name)

    temporary_path.unlink()

    try:
        with rasterio.open(
            temporary_path,
            "w",
            **profile,
        ) as destination:
            destination.write(stored, 1)

            destination.set_band_description(
                1,
                "acute_wind_angle_relative_to_conductor_deg",
            )

            metadata = {
                "units": "degree",
                "angle_parallel_deg": "0",
                "angle_perpendicular_deg": "90",
                "wind_components": (
                    "geographic eastward and northward"
                ),
                "calm_threshold_m_s": str(calm_threshold),
                "calm_policy": "nodata",
                "source_u": str(input_paths[0]),
                "source_v": str(input_paths[1]),
                "source_conductor_axis": str(input_paths[2]),
            }

            timestamp = template["tags"].get(
                "timestamp_utc"
            )

            if timestamp:
                metadata["timestamp_utc"] = timestamp

            destination.update_tags(**metadata)

        with rasterio.open(temporary_path) as validation:
            if (
                validation.count != 1
                or validation.shape != angle.shape
            ):
                raise RuntimeError(
                    "Written raster failed dimension validation."
                )

            # Check that at least the first pixel can be decoded.
            validation.read(
                1,
                window=((0, 1), (0, 1)),
            )

        temporary_path.replace(output_path)

    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def main():
    args = parse_arguments()

    if (
        not np.isfinite(args.calm_threshold)
        or args.calm_threshold < 0.0
    ):
        raise ValueError(
            "--calm-threshold must be finite and nonnegative."
        )

    input_paths = [
        Path(value).expanduser().resolve()
        for value in (
            args.u_wind_raster,
            args.v_wind_raster,
            args.axis_azimuth_raster,
        )
    ]

    for path in input_paths:
        if not path.is_file():
            raise FileNotFoundError(
                f"Required raster does not exist: {path}"
            )

    output_path = (
        Path(args.output).expanduser().resolve()
    )

    if output_path in input_paths:
        raise ValueError(
            "Output path must differ from every input path."
        )

    if output_path.exists() and not args.overwrite:
        raise FileExistsError(
            f"Output exists: {output_path}. "
            "Use --overwrite to replace it."
        )

    azimuth, template = read_template(
        input_paths[2]
    )

    u_wind = read_aligned_component(
        input_paths[0],
        template,
    )

    v_wind = read_aligned_component(
        input_paths[1],
        template,
    )

    (
        angle,
        speed,
        conductor_cells,
        covered_cells,
        calm_cells,
        valid_cells,
    ) = compute_relative_angle(
        u_wind,
        v_wind,
        azimuth,
        args.calm_threshold,
    )

    if not covered_cells.any():
        raise RuntimeError(
            "No valid wind data overlap the conductor cells. "
            "Check input CRS, extent, and nodata."
        )

    missing_count = int(
        (conductor_cells & ~covered_cells).sum()
    )

    if missing_count:
        print(
            f"WARNING: {missing_count} conductor cells have "
            "missing wind data.",
            file=sys.stderr,
        )

    if calm_cells.any():
        print(
            f"WARNING: {int(calm_cells.sum())} conductor cells "
            "are calm and will receive nodata angles.",
            file=sys.stderr,
        )

    write_raster(
        output_path=output_path,
        angle=angle,
        template=template,
        input_paths=input_paths,
        calm_threshold=args.calm_threshold,
        overwrite=args.overwrite,
    )

    print(f"Conductor cells: {int(conductor_cells.sum())}")
    print(f"Valid angle cells: {int(valid_cells.sum())}")

    if valid_cells.any():
        print(
            "Relative-angle range: "
            f"{angle[valid_cells].min():.3f} to "
            f"{angle[valid_cells].max():.3f} degrees"
        )

    print(f"Output: {output_path}")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"ERROR: {error}", file=sys.stderr)
        sys.exit(1)

