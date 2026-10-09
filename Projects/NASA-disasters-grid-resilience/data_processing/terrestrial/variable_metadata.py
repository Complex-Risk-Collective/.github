#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Variable descriptions for the gridded daily product (v2).

apply_metadata(ds)          -> writes long_name / units / source / description
                               into each variable's netCDF attributes
write_data_dictionary(ds)   -> Markdown + CSV table built from the actual file,
                               so the description always matches the contents
"""

import re
from pathlib import Path

import pandas as pd

# ---------------------------------------------------------------------------
# Base variables: name -> (long_name, units, source, description)
# ---------------------------------------------------------------------------
BASE = {
    # ---- MRMS reflectivity / storm structure ----
    "MergedBaseReflectivity_00.50": ("Base reflectivity", "dBZ", "MRMS",
        "Near-surface radar reflectivity from all radars; rain/hail intensity at low levels"),
    "MergedReflectivityComposite_00.50": ("Composite reflectivity", "dBZ", "MRMS",
        "Maximum reflectivity in the vertical column; overall storm severity"),
    "MergedReflectivityAtLowestAltitude_00.50": ("Reflectivity at lowest altitude", "dBZ", "MRMS",
        "Reflectivity at the lowest available altitude; near-surface storm impact"),
    "EchoTop_18_00.50": ("18 dBZ echo top", "km", "MRMS",
        "Height of the 18 dBZ echo; storm depth / convective intensity"),
    "EchoTop_50_00.50": ("50 dBZ echo top", "km", "MRMS",
        "Height of the 50 dBZ echo; strongest storm cores, hail/wind indicator"),
    "VII_00.50": ("Vertically integrated ice", "kg m-2", "MRMS",
        "Ice mass in the storm column; hail and severe convection potential"),
    "VIL_Density_00.50": ("VIL density", "g m-3", "MRMS",
        "Vertically integrated liquid divided by echo top; downburst/hail proxy"),
    "MESH_Max_60min_00.50": ("Maximum estimated hail size, 60-min max track", "mm", "MRMS",
        "Largest MESH at each pixel over the hour (hail swath); cell maximum"),
    "POSH_00.50": ("Probability of severe hail", "%", "MRMS",
        "Probability of hail >= 1 inch (25 mm); max over sampled files and cell"),
    "FLASH_QPE_FFGMAX_00.00": ("Rainfall as % of flash-flood guidance, max", "%", "MRMS FLASH",
        "Rainfall divided by flash flood guidance x 100, max over accumulation windows; "
        ">= 100 means guidance exceeded"),
    "FLASH_CREST_MAXUNITSTREAMFLOW_00.00": ("Peak stream flow per km2 (CREST model)", "m3 s-1 km-2", "MRMS FLASH",
        "How much water is flowing in streams, per km2 of drainage area. "
        "Rises within hours of heavy rain and drops quickly. Use for: is it flooding now?"),
    "FLASH_CREST_MAXSOILSAT_00.00": ("Soil saturation (CREST model)", "%", "MRMS FLASH",
        "How full the soil is with water (100 % = fully saturated). Changes slowly and stays "
        "high for days after rain. Use for: will the next rain run off, and wet ground + wind "
        "(uprooted trees, outages)"),
    "MESH_00.50": ("Maximum estimated size of hail", "mm", "MRMS",
        "Radar-estimated maximum hail diameter; direct hail damage proxy"),
    # ---- MRMS rotation ----
    "MergedAzShear_0-2kmAGL_00.50": ("Azimuthal shear 0-2 km AGL", "0.001 s-1", "MRMS",
        "Low-level rotation; tornado potential"),
    "MergedAzShear_3-6kmAGL_00.50": ("Azimuthal shear 3-6 km AGL", "0.001 s-1", "MRMS",
        "Mid-level rotation; mesocyclone / organized convection"),
    "RotationTrackML60min_00.50": ("60-min rotation track (ML-filtered)", "0.001 s-1", "MRMS",
        "Maximum low-level shear over the past 60 min; rotation/damage swaths"),
    # ---- MRMS precipitation ----
    "PrecipRate_00.00": ("Precipitation rate", "mm h-1", "MRMS",
        "Instantaneous surface rain rate; flash flood / downburst risk"),
    "MultiSensor_QPE_01H_Pass2_00.00": ("1-h precipitation, gauge-corrected", "mm", "MRMS",
        "1-hour accumulation (Pass 2, gauge-corrected); short-term flood potential"),
    "MultiSensor_QPE_24H_Pass2_00.00": ("24-h precipitation, gauge-corrected", "mm", "MRMS",
        "24-hour accumulation; flooding risk"),
    "MultiSensor_QPE_72H_Pass2_00.00": ("72-h precipitation, gauge-corrected", "mm", "MRMS",
        "72-hour accumulation; soil saturation / prolonged flood risk"),
    "FLASH_QPE_ARI01H_00.00": ("1-h rainfall recurrence interval", "years", "MRMS FLASH",
        "Average recurrence interval of the 1-h rainfall; extreme short-duration rain"),
    "FLASH_QPE_ARI12H_00.00": ("12-h rainfall recurrence interval", "years", "MRMS FLASH",
        "Average recurrence interval of the 12-h rainfall"),
    "FLASH_QPE_ARIMAX_00.00": ("Max rainfall recurrence interval", "years", "MRMS FLASH",
        "Largest recurrence interval across accumulation windows; exceptional rain"),
    # ---- MRMS lightning ----
    "NLDN_CG_001min_AvgDensity_00.00": ("CG lightning density, 1-min", "flashes km-2 min-1", "MRMS/NLDN",
        "Cloud-to-ground flash density, 1-minute average"),
    "NLDN_CG_flashes_1h": ("Estimated CG lightning flashes per cell per hour", "flashes cell-1 h-1",
        "MRMS/NLDN", "Hour-mean 15-min average density x 60 min x cell area (2500 km2); "
        "approximate count, exposure for outages and fire ignition"),
    "NLDN_CG_015min_AvgDensity_00.00": ("CG lightning density, peak 15-min", "flashes km-2 min-1", "MRMS/NLDN",
        "Highest 15-min average CG flash density at any 1 km pixel in the cell during the hour; storm intensity"),
    # ---- MRMS precip type (derived) ----
    "PrecipFlag_frac_none": ("Fraction of cell with no precipitation", "1", "MRMS PrecipFlag",
        "Share of valid pixels (averaged over the hour) flagged as no precipitation"),
    "PrecipFlag_frac_rain": ("Fraction of cell with rain", "1", "MRMS PrecipFlag",
        "Share flagged as warm/cool stratiform, convective or tropical rain (codes 1,6,10,91,96)"),
    "PrecipFlag_frac_snow": ("Fraction of cell with snow", "1", "MRMS PrecipFlag",
        "Share flagged as snow (code 3)"),
    "PrecipFlag_frac_hail": ("Fraction of cell with rain+hail", "1", "MRMS PrecipFlag",
        "Share flagged as rain mixed with hail (code 7)"),
    "PrecipFlag_dominant": ("Dominant precipitation class", "category", "MRMS PrecipFlag",
        "Class with the largest fraction: 0 none, 1 rain, 2 snow, 3 hail; -1 no coverage"),
    # ---- RTMA ----
    "t2m": ("2 m air temperature", "K", "RTMA", "Surface thermal state; heat, icing, rain/snow phase"),
    "d2m": ("2 m dew point", "K", "RTMA", "Atmospheric moisture; fog and convection potential"),
    "sh2": ("2 m specific humidity", "kg kg-1", "RTMA", "Absolute moisture content"),
    "sp": ("Surface pressure", "Pa", "RTMA",
        "Station-level pressure; varies mostly with elevation, compare with orog"),
    "vis": ("Visibility", "m", "RTMA", "Surface visibility; fog, heavy precipitation, smoke"),
    "ceil": ("Cloud ceiling", "m", "RTMA",
        "Cloud base height; check the value used for 'no ceiling' before analysis"),
    "tcc": ("Total cloud cover", "%", "RTMA", "Cloud fraction"),
    "rh2_min": ("2 m relative humidity, minimum in cell", "%", "RTMA (derived)",
        "RH from t2m/d2m per 2.5 km pixel (Magnus), then cell minimum; fire weather"),
    "heat_index_max": ("Heat index, maximum in cell", "K", "RTMA (derived)",
        "NWS heat index from t2m and RH per 2.5 km pixel, then cell maximum; heat hazard"),
    "fireweather_frac": ("Fraction of cell with fire-weather conditions", "1", "RTMA (derived)",
        "Share of 2.5 km pixels with RH <= 15 % AND (wind >= 20 mph OR gust >= 25 mph) at the same pixel"),
    "wspd10": ("10 m wind speed", "m s-1", "RTMA", "Sustained wind"),
    "i10fg": ("10 m wind gust", "m s-1", "RTMA", "Peak gust; main driver of wind damage/outages"),
    "u10": ("10 m eastward wind", "m s-1", "RTMA",
        "Earth-relative, rebuilt from wspd10/wdir10 at 2.5 km, then cell mean"),
    "v10": ("10 m northward wind", "m s-1", "RTMA",
        "Earth-relative, rebuilt from wspd10/wdir10 at 2.5 km, then cell mean"),
    "wdir10": ("10 m wind direction (from)", "degree", "RTMA",
        "Meteorological direction computed from cell-mean u10/v10"),
    "orog": ("Surface elevation", "m", "RTMA", "Static terrain height (no time dimension)"),
    "rtma_valid_frac": ("RTMA valid-pixel fraction", "1", "RTMA",
        "Share of 2.5 km pixels in the cell with valid t2m; <1 near coasts/borders"),
    # ---- SWDI (aggregation defined in aggregate_swdi.py) ----
    "MAX_SHEAR": ("TVS maximum shear", "as in SWDI (NEXRAD L3)", "SWDI nx3tvs",
        "Strongest rotation signature of tornado vortex detections in the cell"),
    "MXDV": ("TVS maximum delta velocity", "as in SWDI (NEXRAD L3)", "SWDI nx3tvs",
        "Maximum gate-to-gate velocity difference of TVS detections"),
    "PROB": ("Probability of hail", "%", "SWDI nx3hail", "NEXRAD hail detection probability"),
    "MAXSIZE": ("Maximum expected hail size", "as in SWDI (NEXRAD L3)", "SWDI nx3hail",
        "Hail detection maximum size estimate"),
    "VIL": ("Storm-cell VIL", "kg m-2", "SWDI nx3structure", "Vertically integrated liquid of storm cells"),
    "MAX_REFLECT": ("Storm-cell max reflectivity", "dBZ", "SWDI nx3structure",
        "Peak reflectivity of storm cells"),
    # ---- coordinates ----
    "lat": ("Latitude of cell center", "degrees_north", "grid", "WGS84"),
    "lon": ("Longitude of cell center", "degrees_east", "grid", "WGS84"),
}

SUFFIX_DESC = {
    "min": ("minimum in cell", None),
    "max": ("maximum in cell", None),
    "frac_lt1km": ("fraction of cell below 1 km", "1"),
    "frac_lt300m": ("fraction of cell below 300 m", "1"),
}

DEFAULT_TIME_WINDOW = {
    "SWDI": "detections within the hour (see aggregate_swdi.py)",
}


def describe(name):
    """Return (long_name, units, source, description) for any output variable."""
    if name in BASE:
        return BASE[name]
    if name.endswith("_valid_frac"):
        base = name[: -len("_valid_frac")]
        ln, _, src, _ = describe(base)
        return (f"{ln}: valid-pixel fraction", "1", src,
                "Share of raw pixels in the cell with valid data (0 = no coverage)")
    for suf, (txt, unit_override) in SUFFIX_DESC.items():
        if name.endswith("_" + suf):
            base = name[: -len(suf) - 1]
            if base in BASE:
                ln, units, src, desc = BASE[base]
                return (f"{ln}, {txt}", unit_override or units, src, desc)
    return (name, "", "", "")


def apply_metadata(ds):
    """Write long_name, units, source, description into every variable's attrs."""
    for name in list(ds.data_vars) + [c for c in ("lat", "lon") if c in ds.coords]:
        ln, units, src, desc = describe(name)
        attrs = ds[name].attrs
        attrs.setdefault("long_name", ln)
        if units:
            attrs.setdefault("units", units)
        attrs.setdefault("source", src)
        attrs.setdefault("description", desc)
        if src.startswith("SWDI"):
            attrs.setdefault("time_window", DEFAULT_TIME_WINDOW["SWDI"])
            attrs.setdefault("cell_statistic", "see aggregate_swdi.py")
    for c in ("lat", "lon"):
        if c in ds:
            ds[c].attrs.setdefault("cell_statistic", "cell-center coordinate")
            ds[c].attrs.setdefault("time_window", "static")
    ds.attrs.setdefault("missing_convention", "NaN = no data / no coverage; 0 = observed zero")
    ds.attrs.setdefault("grid", "50 km cells, EPSG:5070 (CONUS Albers); x/y in m")
    ds.attrs.setdefault("time_convention",
                        "label HH = hour [HH:00, HH+1:00); RTMA = analysis at HH:00; "
                        "see time_window per variable")
    return ds


def data_dictionary(ds):
    """One row per variable, taken from the dataset's own attributes."""
    rows = []
    for name in ds.data_vars:
        a = ds[name].attrs
        rows.append({
            "variable": name,
            "source": a.get("source", ""),
            "long_name": a.get("long_name", ""),
            "units": a.get("units", ""),
            "dims": ",".join(ds[name].dims),
            "cell_statistic": a.get("cell_statistic", ""),
            "time_window": a.get("time_window", ""),
            "description": a.get("description", ""),
        })
    order = {"MRMS": 0, "MRMS FLASH": 0, "MRMS/NLDN": 0, "MRMS PrecipFlag": 1,
             "RTMA": 2, "RTMA (derived)": 2, "SWDI nx3tvs": 3, "SWDI nx3hail": 3, "SWDI nx3structure": 3, "grid": 4}
    df = pd.DataFrame(rows)
    df["_o"] = df["source"].map(order).fillna(9)
    df["_v"] = df["variable"].str.endswith("_valid_frac")
    return df.sort_values(["_o", "_v", "variable"]).drop(columns=["_o", "_v"])


def write_data_dictionary(ds, out_dir):
    """Write DATA_DICTIONARY.md and DATA_DICTIONARY.csv next to the daily files."""
    out_dir = Path(out_dir)
    df = data_dictionary(ds)
    df.to_csv(out_dir / "DATA_DICTIONARY.csv", index=False)

    lines = [f"# Gridded weather product — data dictionary ({ds.attrs.get('processing_version', '')})", ""]
    for k in ("grid", "time_convention", "missing_convention"):
        if k in ds.attrs:
            lines.append(f"- **{k}**: {ds.attrs[k]}")
    lines += ["", "| variable | source | units | cell statistic | time window | description |",
              "|---|---|---|---|---|---|"]
    for r in df.itertuples():
        desc = r.description if not r.variable.endswith("_valid_frac") else "valid-pixel fraction (0–1)"
        lines.append(f"| `{r.variable}` | {r.source} | {r.units} | {r.cell_statistic} | "
                     f"{r.time_window} | {r.long_name}. {desc} |")
    (out_dir / "DATA_DICTIONARY.md").write_text("\n".join(lines) + "\n")
