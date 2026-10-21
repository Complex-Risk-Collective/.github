"""
Shared raster helpers for the conductor-rating pipeline.

Conventions
-----------
- All arrays returned to callers are float32 with NaN for missing data.
- All rasters written by the pipeline are single-band float32 GeoTIFFs
  with nodata = -9999, DEFLATE compression, and striped (untiled) layout.
- Block sizes inherited from template profiles are removed so that
  templates with non-multiple-of-16 dimensions cannot produce invalid
  TIFF tiling (the "TileWidth ... must be a multiple of 16" error).
- Writes are atomic: data are written to a temporary file in the
  destination directory and moved into place only after success, so a
  failed run never leaves a truncated GeoTIFF behind.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Union

import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.warp import reproject

PathLike = Union[str, Path]

NODATA = -9999.0

# Profile keys that must not be inherited from an arbitrary template.
_DROP_PROFILE_KEYS = (
    "blockxsize",
    "blockysize",
    "tiled",
    "interleave",
    "photometric",
    "compress",
    "predictor",
)


def read_to_template(
    input_path: PathLike,
    template_path: PathLike,
    resampling: Resampling = Resampling.bilinear,
):
    """
    Read band 1 from input_path and align it to template_path.

    Returns
    -------
    data : numpy.ndarray
        Float32 array on the template grid, NaN where missing.
    profile : dict
        Rasterio profile copied from the template raster.
    """
    with rasterio.open(template_path) as template:
        profile = template.profile.copy()
        destination = np.full(
            (template.height, template.width),
            np.nan,
            dtype="float32",
        )
        dst_transform = template.transform
        dst_crs = template.crs

    with rasterio.open(input_path) as source:
        source_data = (
            source.read(1, masked=True)
            .astype("float32")
            .filled(np.nan)
        )

        reproject(
            source=source_data,
            destination=destination,
            src_transform=source.transform,
            src_crs=source.crs,
            src_nodata=np.nan,
            dst_transform=dst_transform,
            dst_crs=dst_crs,
            dst_nodata=np.nan,
            resampling=resampling,
        )

    return destination, profile


def is_number(value) -> bool:
    """Return True if value can be interpreted as a finite number."""
    if isinstance(value, bool):
        return False
    try:
        return bool(np.isfinite(float(value)))
    except (TypeError, ValueError):
        return False


def read_scalar_or_raster(
    value,
    template_path: PathLike,
    resampling: Resampling = Resampling.bilinear,
):
    """
    Interpret value as a numeric scalar or a raster filename.

    A scalar is expanded to the dimensions of the template raster.
    """
    if is_number(value):
        with rasterio.open(template_path) as template:
            array = np.full(
                (template.height, template.width),
                float(value),
                dtype="float32",
            )
            profile = template.profile.copy()
        return array, profile

    path = Path(str(value)).expanduser()
    if not path.is_file():
        raise FileNotFoundError(
            f"Value is neither a number nor an existing raster: {value}"
        )

    return read_to_template(
        path,
        template_path,
        resampling=resampling,
    )


def clean_profile(profile: dict, nodata: float = NODATA) -> dict:
    """Return a safe single-band float32 GeoTIFF profile."""
    output_profile = {
        key: value
        for key, value in profile.items()
        if key not in _DROP_PROFILE_KEYS
    }
    output_profile.update(
        driver="GTiff",
        dtype="float32",
        count=1,
        nodata=nodata,
        compress="deflate",
        predictor=3,
        tiled=False,
        BIGTIFF="IF_SAFER",
    )
    return output_profile


def write_float_raster(
    output_path: PathLike,
    array: np.ndarray,
    profile: dict,
    description: str | None = None,
    nodata: float = NODATA,
    tags: dict | None = None,
):
    """Atomically write a single-band float32 GeoTIFF."""
    output = np.asarray(array, dtype="float32")
    stored = np.where(
        np.isfinite(output),
        output,
        nodata,
    ).astype("float32")

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output_path.stem}_",
        suffix=".tif",
        dir=output_path.parent,
    )
    os.close(descriptor)
    temporary = Path(temporary_name)

    try:
        with rasterio.open(
            temporary,
            "w",
            **clean_profile(profile, nodata),
        ) as destination:
            destination.write(stored, 1)
            if description:
                destination.set_band_description(1, description)
            if tags:
                destination.update_tags(
                    **{str(k): str(v) for k, v in tags.items()}
                )
        os.replace(temporary, output_path)
    finally:
        if temporary.exists():
            temporary.unlink()


def conductor_statistics(values: np.ndarray, mask: np.ndarray) -> dict:
    """Summary statistics of finite values inside a boolean mask."""
    selected = np.asarray(values, dtype="float64")[mask]
    finite = selected[np.isfinite(selected)]
    if finite.size == 0:
        return {
            "cells": int(mask.sum()),
            "finite_cells": 0,
            "min": None,
            "mean": None,
            "median": None,
            "max": None,
        }
    return {
        "cells": int(mask.sum()),
        "finite_cells": int(finite.size),
        "min": float(finite.min()),
        "mean": float(finite.mean()),
        "median": float(np.median(finite)),
        "max": float(finite.max()),
    }
