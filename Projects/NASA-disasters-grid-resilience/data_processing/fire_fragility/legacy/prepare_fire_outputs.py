#!/usr/bin/env python3

"""
Prepare pre-existing Pyretechnics fireline-intensity rasters.

This script does not run Pyretechnics and does not require LANDFIRE,
ignition geometry, fuel moisture, or other fire-model inputs.

Inputs
------
One or more georeferenced fireline-intensity GeoTIFFs.

Supported units
---------------
- kW/m
- W/m

Outputs
-------
- Standardized fireline-intensity rasters in kW/m.
- Cell-wise maximum fireline intensity over all input rasters.
- CSV manifest containing source files, timestamps, and statistics.

Important
---------
A cell-wise event maximum does not preserve the timestamp at which each
cell reached its maximum. Use timestamp-matched rasters instead when
weather and fire intensity must be paired at the same time.
"""

from __future__ import annotations

import argparse
import csv
import glob
import re
import sys
from pathlib import Path

import numpy as np
import rasterio
import yaml
from rasterio.enums import Resampling
from rasterio.warp import reproject


RESAMPLING_METHODS = {
    "nearest": Resampling.nearest,
    "bilinear": Resampling.bilinear,
    "max": Resampling.max,
}

UNIT_FACTORS_TO_KW_M = {
    "kw/m": 1.0,
    "kw_m": 1.0,
    "kilowatt_per_metre": 1.0,
    "kilowatt_per_meter": 1.0,
    "w/m": 0.001,
    "w_m": 0.001,
    "watt_per_metre": 0.001,
    "watt_per_meter": 0.001,
}


def parse_arguments():
    parser = argparse.ArgumentParser(
        description=(
            "Standardize existing Pyretechnics fireline-intensity "
            "rasters and calculate an event maximum."
        )
    )

    parser.add_argument(
        "--config",
        required=True,
        help="Pipeline YAML configuration.",
    )

    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace existing outputs.",
    )

    return parser.parse_args()


def load_config(path):
    path = Path(path).expanduser().resolve()

    if not path.is_file():
        raise FileNotFoundError(
            f"Configuration does not exist: {path}"
        )

    with path.open("r", encoding="utf-8") as source:
        config = yaml.safe_load(source)

    if not isinstance(config, dict):
        raise ValueError(
            "The YAML root must be a mapping."
        )

    return config, path


def resolve_path(value, config_path):
    path = Path(value).expanduser()

    if not path.is_absolute():
        path = config_path.parent / path

    return path.resolve()


def resolve_glob(value, config_path):
    pattern = Path(value).expanduser()

    if not pattern.is_absolute():
        pattern = config_path.parent / pattern

    return str(pattern)


def normalize_units(value):
    return str(value).strip().lower().replace(" ", "_")


def get_unit_factor(units):
    normalized = normalize_units(units)

    if normalized not in UNIT_FACTORS_TO_KW_M:
        raise ValueError(
            f"Unsupported fireline-intensity units: {units!r}. "
            "Supported inputs are kW/m and W/m."
        )

    return UNIT_FACTORS_TO_KW_M[normalized]


def read_template(path):
    with rasterio.open(path) as source:
        if source.crs is None:
            raise ValueError(
                f"Template raster has no CRS: {path}"
            )

        return {
            "crs": source.crs,
            "transform": source.transform,
            "width": source.width,
            "height": source.height,
            "shape": (
                source.height,
                source.width,
            ),
            "profile": source.profile.copy(),
        }


def read_and_align(
    source_path,
    template,
    resampling,
    conversion_factor,
):
    destination = np.full(
        template["shape"],
        np.nan,
        dtype="float32",
    )

    with rasterio.open(source_path) as source:
        if source.crs is None:
            raise ValueError(
                f"Input raster has no CRS: {source_path}"
            )

        source_array = source.read(
            1,
            masked=True,
        ).filled(np.nan).astype("float32")

        source_array *= conversion_factor

        reproject(
            source=source_array,
            destination=destination,
            src_transform=source.transform,
            src_crs=source.crs,
            src_nodata=np.nan,
            dst_transform=template["transform"],
            dst_crs=template["crs"],
            dst_nodata=np.nan,
            resampling=resampling,
        )

    return destination


def write_raster(
    output_path,
    array,
    template,
    description,
    overwrite,
):
    if output_path.exists() and not overwrite:
        raise FileExistsError(
            f"Output already exists: {output_path}"
        )

    output_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    profile = template["profile"].copy()
    profile.update(
        driver="GTiff",
        count=1,
        dtype="float32",
        nodata=-9999.0,
        compress="deflate",
        predictor=3,
        BIGTIFF="IF_SAFER",
    )

    stored = np.where(
        np.isfinite(array),
        array,
        -9999.0,
    ).astype("float32")

    temporary_path = output_path.with_name(
        f"{output_path.name}.temporary"
    )

    if temporary_path.exists():
        temporary_path.unlink()

    with rasterio.open(
        temporary_path,
        "w",
        **profile,
    ) as destination:
        destination.write(stored, 1)
        destination.set_band_description(
            1,
            description,
        )

    temporary_path.replace(output_path)


def extract_tag(path, timestamp_pattern):
    if timestamp_pattern is None:
        return None

    match = timestamp_pattern.search(
        path.name
    )

    if not match:
        return None

    if "tag" in match.groupdict():
        return match.group("tag")

    return match.group(0)


def finite_statistics(array):
    valid = array[np.isfinite(array)]

    if valid.size == 0:
        return {
            "valid_cells": 0,
            "minimum_kw_m": None,
            "maximum_kw_m": None,
            "mean_kw_m": None,
        }

    return {
        "valid_cells": int(valid.size),
        "minimum_kw_m": float(
            np.min(valid)
        ),
        "maximum_kw_m": float(
            np.max(valid)
        ),
        "mean_kw_m": float(
            np.mean(valid)
        ),
    }


def main():
    args = parse_arguments()

    config, config_path = load_config(
        args.config
    )

    grid_config = config.get("grid", {})
    fire_config = config.get(
        "fire_outputs",
        {},
    )

    template_value = grid_config.get(
        "template_raster"
    )

    if not template_value:
        raise KeyError(
            "grid.template_raster is required."
        )

    source_glob = fire_config.get(
        "source_glob"
    )

    if not source_glob:
        raise KeyError(
            "fire_outputs.source_glob is required."
        )

    template_path = resolve_path(
        template_value,
        config_path,
    )

    if not template_path.is_file():
        raise FileNotFoundError(
            f"Template raster does not exist: {template_path}"
        )

    template = read_template(
        template_path
    )

    input_files = sorted(
        Path(path).resolve()
        for path in glob.glob(
            resolve_glob(
                source_glob,
                config_path,
            ),
            recursive=True,
        )
        if Path(path).suffix.lower()
        in {".tif", ".tiff"}
    )

    if not input_files:
        raise FileNotFoundError(
            "No fireline-intensity GeoTIFFs matched:\n"
            f"  {source_glob}"
        )

    output_directory = resolve_path(
        fire_config.get(
            "output_directory",
            "../work/fire_prepared",
        ),
        config_path,
    )

    output_directory.mkdir(
        parents=True,
        exist_ok=True,
    )

    unit_factor = get_unit_factor(
        fire_config.get(
            "input_units",
            "kW/m",
        )
    )

    resampling_name = str(
        fire_config.get(
            "resampling",
            "nearest",
        )
    ).lower()

    if resampling_name not in RESAMPLING_METHODS:
        raise ValueError(
            "fire_outputs.resampling must be one of: "
            + ", ".join(RESAMPLING_METHODS)
        )

    resampling = RESAMPLING_METHODS[
        resampling_name
    ]

    timestamp_expression = fire_config.get(
        "timestamp_regex"
    )

    timestamp_pattern = (
        re.compile(timestamp_expression)
        if timestamp_expression
        else None
    )

    reject_negative = bool(
        fire_config.get(
            "reject_negative_values",
            True,
        )
    )

    write_standardized = bool(
        fire_config.get(
            "write_standardized_rasters",
            True,
        )
    )

    write_event_maximum = bool(
        fire_config.get(
            "write_event_maximum",
            True,
        )
    )

    event_maximum = np.full(
        template["shape"],
        np.nan,
        dtype="float32",
    )

    manifest_records = []
    used_output_names = set()

    for index, source_path in enumerate(
        input_files,
        start=1,
    ):
        print(
            f"[{index}/{len(input_files)}] "
            f"Reading {source_path}"
        )

        array = read_and_align(
            source_path=source_path,
            template=template,
            resampling=resampling,
            conversion_factor=unit_factor,
        )

        finite = array[np.isfinite(array)]

        if (
            reject_negative
            and finite.size > 0
            and np.min(finite) < 0
        ):
            raise ValueError(
                "Negative fireline intensity found in: "
                f"{source_path}"
            )

        tag = extract_tag(
            source_path,
            timestamp_pattern,
        )

        if tag:
            output_name = (
                f"fireline_intensity_kw_m_{tag}.tif"
            )
        else:
            safe_stem = re.sub(
                r"[^A-Za-z0-9_.-]+",
                "_",
                source_path.stem,
            )

            output_name = (
                f"fireline_intensity_kw_m_"
                f"{safe_stem}.tif"
            )

        if output_name in used_output_names:
            raise ValueError(
                "Multiple source files map to the same "
                f"standardized filename: {output_name}"
            )

        used_output_names.add(output_name)

        standardized_path = (
            output_directory / output_name
        )

        if write_standardized:
            write_raster(
                output_path=standardized_path,
                array=array,
                template=template,
                description=(
                    "pyretechnics_fireline_intensity_kw_m"
                ),
                overwrite=args.overwrite,
            )

        if write_event_maximum:
            event_maximum = np.fmax(
                event_maximum,
                array,
            )

        statistics = finite_statistics(array)

        manifest_records.append(
            {
                "source_path": str(source_path),
                "timestamp_tag": tag or "",
                "standardized_path": (
                    str(standardized_path)
                    if write_standardized
                    else ""
                ),
                "input_units": fire_config.get(
                    "input_units",
                    "kW/m",
                ),
                "output_units": "kW/m",
                "resampling": resampling_name,
                **statistics,
            }
        )

    if write_event_maximum:
        event_filename = fire_config.get(
            "event_maximum_filename",
            "maximum_fireline_intensity_kw_m.tif",
        )

        event_path = (
            output_directory / event_filename
        )

        write_raster(
            output_path=event_path,
            array=event_maximum,
            template=template,
            description=(
                "maximum_pyretechnics_fireline_intensity_kw_m"
            ),
            overwrite=args.overwrite,
        )

        print(
            f"Event maximum written to: {event_path}"
        )

    manifest_path = (
        output_directory
        / fire_config.get(
            "manifest_filename",
            "fire_output_manifest.csv",
        )
    )

    if manifest_path.exists() and not args.overwrite:
        raise FileExistsError(
            f"Manifest already exists: {manifest_path}"
        )

    with manifest_path.open(
        "w",
        newline="",
        encoding="utf-8",
    ) as destination:
        fieldnames = [
            "source_path",
            "timestamp_tag",
            "standardized_path",
            "input_units",
            "output_units",
            "resampling",
            "valid_cells",
            "minimum_kw_m",
            "maximum_kw_m",
            "mean_kw_m",
        ]

        writer = csv.DictWriter(
            destination,
            fieldnames=fieldnames,
        )

        writer.writeheader()
        writer.writerows(
            manifest_records
        )

    print(f"Manifest written to: {manifest_path}")
    print(
        f"Prepared {len(input_files)} fire raster(s)."
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


