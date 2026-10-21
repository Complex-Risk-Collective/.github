#!/usr/bin/env python3

"""
Prepare existing Pyretechnics output rasters for wildfire line rating.

Expected filename convention
----------------------------
    <parameter>_<percentile>_<hour>.tif

Examples
--------
    flame-length_10_001.tif
    flame-length_99_003.tif
    spread-rate_50_012.tif
    crown-fire_90_006.tif

Exception: time-invariant parameters
------------------------------------
Pyretechnics writes time of arrival once per percentile, without an hour:

    <parameter>_<percentile>.tif        e.g. time-of-arrival_99.tif

Its values are minutes since the simulation start, so the file has no
model hour. Such files are matched by a second pattern
(``time_invariant_filename_regex``) and accepted only for parameters that
are time invariant (``time-of-arrival`` by default, or any parameter with
``time_invariant: true``). They are standardized with the simulation-start
tag,

    time_of_arrival_p099_<simulation_start_tag>.tif

so downstream stages that expect ``_pNNN_<tag>.tif`` read them unchanged.
The manifest records ``model_hour`` as empty and ``time_invariant`` as true.

The script:
- Parses parameter, percentile, and elapsed/model hour (percentile only
  for time-invariant parameters such as time of arrival).
- Converts model-relative hours into absolute UTC timestamps.
- Aligns all recognized rasters to a template grid.
- Preserves percentile identity.
- Derives fireline intensity from flame length.
- Writes a separate event-maximum intensity for each percentile.
- Writes a CSV manifest.

Fireline-intensity relationship
-------------------------------
Pyretechnics uses:

    flame_length_m =
        0.07747042253266703
        * fireline_intensity_kw_m ** 0.46

The inverse is:

    fireline_intensity_kw_m =
        (flame_length_m / 0.07747042253266703) ** (1 / 0.46)

No Pyretechnics installation or fire-model inputs are required.
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
import warnings
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import rasterio
import yaml
from rasterio.enums import Resampling
from rasterio.warp import reproject


FLAME_LENGTH_COEFFICIENT = 0.07747042253266703
FLAME_LENGTH_EXPONENT = 0.46

RESAMPLING_METHODS = {
    "nearest": Resampling.nearest,
    "bilinear": Resampling.bilinear,
    "cubic": Resampling.cubic,
    "average": Resampling.average,
    "max": Resampling.max,
    "min": Resampling.min,
    "mode": Resampling.mode,
}

# Parameters written once per percentile (no hour in the filename).
TIME_INVARIANT_PARAMETERS = {"time-of-arrival"}

DEFAULT_TIME_INVARIANT_REGEX = (
    r"^(?P<parameter>.+)_"
    r"(?P<percentile>\d{1,3})\."
    r"(?:tif|tiff)$"
)

PARAMETER_ALIASES = {
    "flame-length": "flame-length",
    "flame_length": "flame-length",
    "flamelength": "flame-length",

    "spread-rate": "spread-rate",
    "spread_rate": "spread-rate",
    "spreadrate": "spread-rate",

    "crown-fire": "crown-fire",
    "crown_fire": "crown-fire",
    "crownfire": "crown-fire",

    "time-of-arrival": "time-of-arrival",
    "time_of_arrival": "time-of-arrival",
    "toa": "time-of-arrival",

    "hours-since-burned": "hours-since-burned",
    "hours_since_burned": "hours-since-burned",

    "isochrones": "isochrones",
    "isochrone": "isochrones",
}


def parse_arguments():
    parser = argparse.ArgumentParser(
        description=(
            "Prepare existing Pyretechnics outputs and derive "
            "fireline intensity from flame length."
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
        help="Replace existing output files.",
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
    path = Path(value).expanduser()

    if not path.is_absolute():
        path = config_path.parent / path

    return path.resolve()


def parse_utc_timestamp(value):
    timestamp = datetime.fromisoformat(
        str(value).replace("Z", "+00:00")
    )

    if timestamp.tzinfo is None:
        raise ValueError(
            "simulation_start_utc must include Z or "
            "an explicit UTC offset."
        )

    return timestamp.astimezone(
        timezone.utc
    )


def normalize_parameter(value):
    normalized = (
        str(value)
        .strip()
        .lower()
        .replace(" ", "-")
    )

    return PARAMETER_ALIASES.get(
        normalized,
        normalized,
    )


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


def calculate_timestamp(
    simulation_start,
    model_hour,
    hour_semantics,
):
    if hour_semantics == "elapsed_since_start":
        elapsed_hours = model_hour

    elif hour_semantics == "one_based_index":
        if model_hour < 1:
            raise ValueError(
                "one_based_index requires model hours >= 1."
            )

        elapsed_hours = model_hour - 1

    else:
        raise ValueError(
            "hour_semantics must be elapsed_since_start "
            "or one_based_index."
        )

    return (
        simulation_start
        + timedelta(hours=elapsed_hours)
    )


def timestamp_tag(timestamp):
    return timestamp.strftime(
        "%Y%m%dT%H%M%SZ"
    )


def percentile_tag(percentile):
    return f"p{percentile:03d}"


def read_and_align(
    source_path,
    template,
    resampling,
    fill_value="zero",
):
    """
    Read one Pyretechnics raster and align it to the template grid.

    fill_value="zero"   (default) nodata, NaN and out-of-coverage cells
                        become 0 (assumption G13: unburned = 0 kW/m).
    fill_value="nodata" these cells stay NaN (written as -9999). Required
                        for time_of_arrival, where 0 is a valid value
                        (arrival at ignition) and NaN means unburned.
    """
    keep_nodata = str(fill_value).lower() == "nodata"
    background = np.nan if keep_nodata else 0.0

    destination = np.full(
        template["shape"],
        background,
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
        ).astype("float32").filled(background)
        source_array[~np.isfinite(source_array)] = background

        reproject(
            source=source_array,
            destination=destination,
            src_transform=source.transform,
            src_crs=source.crs,
            src_nodata=background,
            dst_transform=template["transform"],
            dst_crs=template["crs"],
            dst_nodata=background,
            resampling=resampling,
        )

    return destination


def convert_flame_length_to_metres(
    flame_length,
    input_units,
):
    normalized = (
        str(input_units)
        .strip()
        .lower()
        .replace(" ", "")
    )

    if normalized in {
        "m",
        "meter",
        "meters",
        "metre",
        "metres",
    }:
        return flame_length

    if normalized in {
        "ft",
        "foot",
        "feet",
    }:
        return flame_length * 0.3048

    raise ValueError(
        "Flame-length input units must be metres or feet; "
        f"received {input_units!r}."
    )


def derive_fireline_intensity(
    flame_length,
    input_units,
):
    """
    Convert flame length to fireline intensity.

    Output units:
        kW/m
    """
    flame_length_m = convert_flame_length_to_metres(
        flame_length,
        input_units,
    )

    valid = (
        np.isfinite(flame_length_m)
        & (flame_length_m >= 0.0)
    )

    intensity = np.full(
        flame_length_m.shape,
        0,
        dtype="float32",
    )

    # Zero flame length corresponds to zero intensity.
    intensity[valid] = np.power(
        flame_length_m[valid]
        / FLAME_LENGTH_COEFFICIENT,
        1.0 / FLAME_LENGTH_EXPONENT,
    ).astype("float32")

    return intensity


def write_float_raster(
    output_path,
    array,
    template,
    description,
    units,
    tags,
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

    profile = {
        key: value
        for key, value in template["profile"].items()
        if key not in {"blockxsize", "blockysize", "tiled", "interleave"}
    }

    profile.update(
        driver="GTiff",
        count=1,
        dtype="float32",
        nodata=-9999.0,
        compress="deflate",
        predictor=3,
        tiled=False,
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

        destination.update_tags(
            units=units,
            **{
                key: str(value)
                for key, value in tags.items()
                if value is not None
            },
        )

    temporary_path.replace(
        output_path
    )


def statistics(array):
    valid = array[np.isfinite(array)]

    if valid.size == 0:
        return {
            "valid_cells": 0,
            "minimum": "",
            "maximum": "",
            "mean": "",
        }

    return {
        "valid_cells": int(valid.size),
        "minimum": float(np.min(valid)),
        "maximum": float(np.max(valid)),
        "mean": float(np.mean(valid)),
    }


def selected_percentiles(value):
    if value is None:
        return None

    if isinstance(value, str):
        if value.lower() == "all":
            return None

        values = [
            item.strip()
            for item in value.split(",")
            if item.strip()
        ]
    else:
        values = list(value)

    result = {
        int(item)
        for item in values
    }

    invalid = [
        item
        for item in result
        if not 0 <= item <= 100
    ]

    if invalid:
        raise ValueError(
            "Percentiles must be between 0 and 100: "
            + ", ".join(
                str(item) for item in invalid
            )
        )

    return result


def handle_unknown_parameter(
    parameter,
    path,
    action,
):
    message = (
        f"Unrecognized Pyretechnics parameter "
        f"{parameter!r}: {path}"
    )

    if action == "error":
        raise ValueError(message)

    if action == "warn":
        warnings.warn(message)

    elif action != "skip":
        raise ValueError(
            "unknown_parameter_action must be "
            "error, warn, or skip."
        )


def main():
    args = parse_arguments()

    config, config_path = load_config(
        args.config
    )

    output_config = config.get(
        "pyretechnics_outputs",
        {},
    )

    grid_config = config.get(
        "grid",
        {},
    )

    template_value = grid_config.get(
        "template_raster"
    )

    if not template_value:
        raise KeyError(
            "grid.template_raster is required."
        )

    root_value = output_config.get(
        "root_directory"
    )

    if not root_value:
        raise KeyError(
            "pyretechnics_outputs.root_directory is required."
        )

    template_path = resolve_path(
        template_value,
        config_path,
    )

    output_root = resolve_path(
        output_config.get(
            "output_directory",
            "../work/fire_prepared",
        ),
        config_path,
    )

    source_root = resolve_path(
        root_value,
        config_path,
    )

    if not source_root.is_dir():
        raise FileNotFoundError(
            f"Pyretechnics output directory does not exist: "
            f"{source_root}"
        )

    output_root.mkdir(
        parents=True,
        exist_ok=True,
    )

    template = read_template(
        template_path
    )

    filename_regex = re.compile(
        output_config.get(
            "filename_regex",
            (
                r"^(?P<parameter>.+)_"
                r"(?P<percentile>\d{1,3})_"
                r"(?P<hour>\d+)\."
                r"(?:tif|tiff)$"
            ),
        ),
        flags=re.IGNORECASE,
    )

    time_invariant_regex = re.compile(
        output_config.get(
            "time_invariant_filename_regex",
            DEFAULT_TIME_INVARIANT_REGEX,
        ),
        flags=re.IGNORECASE,
    )

    simulation_start = parse_utc_timestamp(
        output_config[
            "simulation_start_utc"
        ]
    )

    hour_semantics = output_config.get(
        "hour_semantics",
        "elapsed_since_start",
    )

    requested_percentiles = (
        selected_percentiles(
            output_config.get(
                "selected_percentiles",
                "all",
            )
        )
    )

    unknown_action = output_config.get(
        "unknown_parameter_action",
        "warn",
    ).lower()

    parameter_configurations = (
        output_config.get(
            "parameters",
            {},
        )
    )

    write_standardized = bool(
        output_config.get(
            "write_standardized_inputs",
            True,
        )
    )

    derive_intensity = bool(
        output_config.get(
            "derive_fireline_intensity",
            True,
        )
    )

    write_event_maximum = bool(
        output_config.get(
            "write_percentile_event_maximum",
            True,
        )
    )

    files = sorted(
        path
        for path in source_root.rglob("*")
        if path.is_file()
        and path.suffix.lower()
        in {".tif", ".tiff"}
    )

    if not files:
        raise FileNotFoundError(
            f"No GeoTIFFs found below {source_root}"
        )

    manifest_records = []

    # Separate event maximum for each percentile.
    intensity_maxima = {}
    intensity_counts = {}

    recognized_count = 0
    intensity_count = 0

    for source_path in files:
        match = filename_regex.match(
            source_path.name
        )
        time_invariant = False

        if not match:
            # Exception: <parameter>_<percentile>.tif (no hour), e.g.
            # time-of-arrival_99.tif.
            match = time_invariant_regex.match(
                source_path.name
            )
            time_invariant = match is not None

        if not match:
            handle_unknown_parameter(
                "filename-not-matched",
                source_path,
                unknown_action,
            )
            continue

        raw_parameter = match.group(
            "parameter"
        )

        parameter = normalize_parameter(
            raw_parameter
        )

        percentile = int(
            match.group("percentile")
        )

        if time_invariant:
            declared = parameter_configurations.get(
                parameter, {}
            ).get(
                "time_invariant",
                parameter in TIME_INVARIANT_PARAMETERS,
            )

            if not declared:
                handle_unknown_parameter(
                    f"{parameter} (no hour in filename; parameter "
                    "is not time invariant)",
                    source_path,
                    unknown_action,
                )
                continue

            model_hour = None

        else:
            model_hour = int(
                match.group("hour")
            )

        if not 0 <= percentile <= 100:
            raise ValueError(
                f"Invalid percentile in {source_path.name}: "
                f"{percentile}"
            )

        if (
            requested_percentiles is not None
            and percentile
            not in requested_percentiles
        ):
            continue

        if parameter not in parameter_configurations:
            handle_unknown_parameter(
                parameter,
                source_path,
                unknown_action,
            )
            continue

        parameter_config = (
            parameter_configurations[
                parameter
            ]
        )

        canonical_name = (
            parameter_config.get(
                "canonical_name",
                parameter.replace("-", "_"),
            )
        )

        resampling_name = (
            parameter_config.get(
                "resampling",
                "nearest",
            ).lower()
        )

        if resampling_name not in RESAMPLING_METHODS:
            raise ValueError(
                f"Unsupported resampling method "
                f"{resampling_name!r} for {parameter}."
            )

        if time_invariant:
            # Values are relative to the simulation start; tag with it.
            timestamp = simulation_start
        else:
            timestamp = calculate_timestamp(
                simulation_start,
                model_hour,
                hour_semantics,
            )

        tag = timestamp_tag(timestamp)
        ptag = percentile_tag(percentile)

        array = read_and_align(
            source_path=source_path,
            template=template,
            resampling=RESAMPLING_METHODS[
                resampling_name
            ],
            fill_value=parameter_config.get(
                "fill_value",
                "zero",
            ),
        )

        if parameter_config.get(
            "reject_negative",
            False,
        ):
            finite = array[np.isfinite(array)]

            if (
                finite.size
                and np.min(finite) < 0.0
            ):
                raise ValueError(
                    f"Negative values found in {source_path}"
                )

        standardized_path = (
            output_root
            / "standardized"
            / canonical_name
            / (
                f"{canonical_name}_{ptag}_"
                f"{tag}.tif"
            )
        )

        common_tags = {
            "source_file": str(source_path),
            "source_parameter": parameter,
            "mcmc_percentile": percentile,
            "model_hour": (
                "" if model_hour is None else model_hour
            ),
            "time_invariant": time_invariant,
            "timestamp_utc":
                timestamp.strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                ),
            "hour_semantics": hour_semantics,
        }

        if write_standardized:
            write_float_raster(
                output_path=standardized_path,
                array=array,
                template=template,
                description=canonical_name,
                units=parameter_config.get(
                    "output_units",
                    parameter_config.get(
                        "input_units",
                        "unknown",
                    ),
                ),
                tags=common_tags,
                overwrite=args.overwrite,
            )

        source_statistics = statistics(
            array
        )

        derived_path = None

        if (
            parameter == "flame-length"
            and derive_intensity
        ):
            intensity = derive_fireline_intensity(
                flame_length=array,
                input_units=parameter_config.get(
                    "input_units",
                    "m",
                ),
            )

            derived_path = (
                output_root
                / "derived"
                / "fireline_intensity"
                / (
                    "fireline_intensity_kw_m_"
                    f"{ptag}_{tag}.tif"
                )
            )

            write_float_raster(
                output_path=derived_path,
                array=intensity,
                template=template,
                description=(
                    "fireline_intensity_derived_"
                    "from_pyretechnics_flame_length"
                ),
                units="kW/m",
                tags={
                    **common_tags,
                    "derivation": (
                        "(flame_length_m / "
                        "0.07747042253266703)"
                        "**(1/0.46)"
                    ),
                    "flame_length_coefficient":
                        FLAME_LENGTH_COEFFICIENT,
                    "flame_length_exponent":
                        FLAME_LENGTH_EXPONENT,
                },
                overwrite=args.overwrite,
            )

            if percentile not in intensity_maxima:
                intensity_maxima[percentile] = (
                    intensity.copy()
                )
                intensity_counts[percentile] = 1
            else:
                intensity_maxima[percentile] = (
                    np.fmax(
                        intensity_maxima[
                            percentile
                        ],
                        intensity,
                    )
                )
                intensity_counts[percentile] += 1

            intensity_count += 1

        manifest_records.append(
            {
                "source_path": str(source_path),
                "source_parameter": parameter,
                "canonical_parameter":
                    canonical_name,
                "percentile": percentile,
                "model_hour": (
                    "" if model_hour is None else model_hour
                ),
                "time_invariant": time_invariant,
                "timestamp_utc":
                    timestamp.strftime(
                        "%Y-%m-%dT%H:%M:%SZ"
                    ),
                "timestamp_tag": tag,
                "hour_semantics":
                    hour_semantics,
                "input_units":
                    parameter_config.get(
                        "input_units",
                        "",
                    ),
                "standardized_path": (
                    str(standardized_path)
                    if write_standardized
                    else ""
                ),
                "derived_fireline_intensity_path": (
                    str(derived_path)
                    if derived_path
                    else ""
                ),
                **source_statistics,
            }
        )

        recognized_count += 1

    if recognized_count == 0:
        raise RuntimeError(
            "No recognized Pyretechnics outputs were processed."
        )

    if derive_intensity and intensity_count == 0:
        raise RuntimeError(
            "Fireline-intensity derivation was enabled, "
            "but no flame-length rasters were processed."
        )

    if write_event_maximum:
        for percentile, maximum in (
            sorted(
                intensity_maxima.items()
            )
        ):
            ptag = percentile_tag(
                percentile
            )

            output_path = (
                output_root
                / "event_maximum"
                / (
                    "maximum_fireline_intensity_"
                    f"kw_m_{ptag}.tif"
                )
            )

            write_float_raster(
                output_path=output_path,
                array=maximum,
                template=template,
                description=(
                    "temporal_maximum_fireline_"
                    "intensity_for_percentile"
                ),
                units="kW/m",
                tags={
                    "mcmc_percentile":
                        percentile,
                    "source_hour_count":
                        intensity_counts[
                            percentile
                        ],
                    "derivation": (
                        "temporal maximum of fireline "
                        "intensity derived from flame length"
                    ),
                },
                overwrite=args.overwrite,
            )

            print(
                f"Percentile {percentile} maximum: "
                f"{output_path}"
            )

    manifest_path = (
        output_root
        / output_config.get(
            "manifest_filename",
            "pyretechnics_output_manifest.csv",
        )
    )

    if manifest_path.exists() and not args.overwrite:
        raise FileExistsError(
            f"Manifest already exists: {manifest_path}"
        )

    manifest_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    fieldnames = [
        "source_path",
        "source_parameter",
        "canonical_parameter",
        "percentile",
        "model_hour",
        "time_invariant",
        "timestamp_utc",
        "timestamp_tag",
        "hour_semantics",
        "input_units",
        "standardized_path",
        "derived_fireline_intensity_path",
        "valid_cells",
        "minimum",
        "maximum",
        "mean",
    ]

    with manifest_path.open(
        "w",
        newline="",
        encoding="utf-8",
    ) as destination:
        writer = csv.DictWriter(
            destination,
            fieldnames=fieldnames,
        )

        writer.writeheader()
        writer.writerows(
            manifest_records
        )

    print(f"Manifest written to: {manifest_path}")
    print(f"Recognized rasters: {recognized_count}")
    print(
        "Derived fireline-intensity rasters: "
        f"{intensity_count}"
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"ERROR: {error}", file=sys.stderr)
        sys.exit(1)
