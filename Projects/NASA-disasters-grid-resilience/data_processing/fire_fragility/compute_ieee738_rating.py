#!/usr/bin/env python3
"""
Calculate steady-state IEEE 738-style conductor ampacity on a raster grid.

Heat balance (per metre of conductor):

    q_c + q_r = q_s + q_w + I^2 R(T_avg)
    I = sqrt((q_c + q_r - q_s - q_w) / R(T_avg))

where q_c is convective cooling, q_r radiative cooling, q_s solar gain
and q_w wildfire heat gain (convective + radiative).

Equations (IEEE 738-2012 SI form)
---------------------------------
Air density      rho_f = (1.293 - 1.525e-4 H + 6.379e-9 H^2)
                         / (1 + 0.00367 T_film)
Viscosity        mu_f  = 1.458e-6 (T_film + 273)^1.5 / (T_film + 383.4)
Conductivity     k_f   = 2.424e-2 + 7.477e-5 T_film - 4.407e-9 T_film^2
Reynolds         N_Re  = D rho_f V / mu_f
Wind factor      K     = 1.194 - cos(phi) + 0.194 cos(2 phi)
                         + 0.368 sin(2 phi)
Forced (low)     q_c1  = K (1.01 + 1.35 N_Re^0.52) k_f (T_s - T_a)
Forced (high)    q_c2  = K 0.754 N_Re^0.6 k_f (T_s - T_a)
Natural          q_cn  = 3.645 rho_f^0.5 D^0.75 (T_s - T_a)^1.25
Convection       q_c   = max(q_c1, q_c2, q_cn)
Radiation        q_r   = 17.8 D eps [((T_s+273)/100)^4 - ((T_a+273)/100)^4]
Resistance       linear interpolation/extrapolation between two
                 (temperature, resistance) points at T_avg.

Calm-wind handling
------------------
compute_wind_relative_angle.py writes nodata for calm cells. With
--calm-wind-policy natural (default), calm conductor cells use natural
convection only (forced convection = 0). With "nodata", they are
excluded, which reproduces the earlier behaviour.

Solar modes
-----------
Baseline: --solar-gain-w-m <raster or number>
Wildfire: --solar-off (manuscript assumption: smoke blocks solar gain),
          or --solar-gain-w-m if the wildfire case should keep solar.
"""

import argparse
import json
from pathlib import Path

import numpy as np
from rasterio.enums import Resampling

from raster_utils import (
    conductor_statistics,
    read_scalar_or_raster,
    read_to_template,
    write_float_raster,
)


def parse_arguments():
    parser = argparse.ArgumentParser(description=__doc__)

    parser.add_argument("ambient_temperature_c")
    parser.add_argument("wind_speed_m_s")
    parser.add_argument("wind_relative_angle_deg")
    parser.add_argument("conductor_diameter_m")

    parser.add_argument(
        "--template",
        help="Raster defining the output grid (default: ambient raster).",
    )
    parser.add_argument("--surface-temperature-c", required=True)
    parser.add_argument(
        "--average-temperature-c",
        help="Average strand temperature (default: surface temperature).",
    )
    parser.add_argument(
        "--elevation-m",
        required=True,
        help="Conductor elevation above MSL: number or aligned raster.",
    )
    parser.add_argument(
        "--emissivity",
        required=True,
        help="Number or raster.",
    )

    solar_group = parser.add_mutually_exclusive_group(required=True)
    solar_group.add_argument(
        "--solar-gain-w-m",
        help="Precomputed solar heat gain per conductor length.",
    )
    solar_group.add_argument("--solar-off", action="store_true")

    parser.add_argument("--wildfire-convective-w-m", default="0")
    parser.add_argument("--wildfire-radiative-w-m", default="0")

    parser.add_argument("--resistance-low-ohm-m", type=float, required=True)
    parser.add_argument("--temperature-low-c", type=float, required=True)
    parser.add_argument("--resistance-high-ohm-m", type=float, required=True)
    parser.add_argument("--temperature-high-c", type=float, required=True)

    parser.add_argument(
        "--calm-wind-policy",
        choices=["natural", "nodata"],
        default="natural",
    )
    parser.add_argument("--baseline-ampacity")
    parser.add_argument("--timestamp-tag", required=True)
    parser.add_argument("--out-dir", required=True)

    return parser.parse_args()


def load_value(value, template, resampling=Resampling.bilinear):
    return read_scalar_or_raster(str(value), template, resampling)[0]


def main():
    args = parse_arguments()

    if args.temperature_high_c <= args.temperature_low_c:
        raise ValueError(
            "The high resistance temperature must exceed the low one."
        )
    if args.resistance_low_ohm_m <= 0 or args.resistance_high_ohm_m <= 0:
        raise ValueError("Resistance values must be positive.")

    template = args.template or args.ambient_temperature_c

    ambient_c, profile = read_to_template(
        args.ambient_temperature_c, template, Resampling.bilinear
    )
    wind_speed = load_value(args.wind_speed_m_s, template)
    wind_angle = load_value(
        args.wind_relative_angle_deg, template, Resampling.nearest
    )
    diameter = load_value(
        args.conductor_diameter_m, template, Resampling.nearest
    )
    surface_c = load_value(args.surface_temperature_c, template)
    average_c = (
        surface_c.copy()
        if args.average_temperature_c is None
        else load_value(args.average_temperature_c, template)
    )
    elevation = load_value(args.elevation_m, template, Resampling.nearest)
    emissivity = load_value(args.emissivity, template, Resampling.nearest)
    wildfire_convection = load_value(
        args.wildfire_convective_w_m, template, Resampling.nearest
    )
    wildfire_radiation = load_value(
        args.wildfire_radiative_w_m, template, Resampling.nearest
    )

    if args.solar_off:
        solar_gain = np.zeros_like(ambient_c, dtype="float32")
    else:
        solar_gain = load_value(
            args.solar_gain_w_m, template, Resampling.nearest
        )
        # Night-time or below-threshold sun: no solar gain.
        solar_gain = np.where(
            np.isfinite(diameter) & ~np.isfinite(solar_gain),
            0.0,
            solar_gain,
        ).astype("float32")

    conductor = np.isfinite(diameter) & (diameter > 0.0)

    calm = (
        conductor
        & np.isfinite(wind_speed)
        & ~np.isfinite(wind_angle)
    )
    if args.calm_wind_policy == "natural":
        # Angle is irrelevant when forced convection is suppressed.
        wind_angle = np.where(calm, 90.0, wind_angle)

    temperature_difference = surface_c - ambient_c
    film_temperature_c = (surface_c + ambient_c) / 2.0

    air_density = (
        1.293 - 1.525e-4 * elevation + 6.379e-9 * elevation**2
    ) / (1.0 + 0.00367 * film_temperature_c)

    dynamic_viscosity = (
        1.458e-6
        * (film_temperature_c + 273.0) ** 1.5
        / (film_temperature_c + 383.4)
    )

    thermal_conductivity = (
        2.424e-2
        + 7.477e-5 * film_temperature_c
        - 4.407e-9 * film_temperature_c**2
    )

    phi = np.deg2rad(wind_angle)
    wind_direction_factor = (
        1.194
        - np.cos(phi)
        + 0.194 * np.cos(2.0 * phi)
        + 0.368 * np.sin(2.0 * phi)
    )

    reynolds_number = diameter * air_density * wind_speed / dynamic_viscosity

    with np.errstate(invalid="ignore"):
        natural_convection = (
            3.645
            * air_density**0.5
            * diameter**0.75
            * temperature_difference**1.25
        )
        low_wind_convection = (
            wind_direction_factor
            * (1.01 + 1.35 * reynolds_number**0.52)
            * thermal_conductivity
            * temperature_difference
        )
        high_wind_convection = (
            wind_direction_factor
            * 0.754
            * reynolds_number**0.6
            * thermal_conductivity
            * temperature_difference
        )

    if args.calm_wind_policy == "natural":
        low_wind_convection = np.where(calm, 0.0, low_wind_convection)
        high_wind_convection = np.where(calm, 0.0, high_wind_convection)

    convection_loss = np.maximum.reduce(
        [natural_convection, low_wind_convection, high_wind_convection]
    )

    radiation_loss = (
        17.8
        * diameter
        * emissivity
        * (
            ((surface_c + 273.0) / 100.0) ** 4
            - ((ambient_c + 273.0) / 100.0) ** 4
        )
    )

    resistance = args.resistance_low_ohm_m + (
        args.resistance_high_ohm_m - args.resistance_low_ohm_m
    ) * (average_c - args.temperature_low_c) / (
        args.temperature_high_c - args.temperature_low_c
    )

    wildfire_heat = wildfire_convection + wildfire_radiation
    thermal_margin = (
        convection_loss + radiation_loss - solar_gain - wildfire_heat
    )

    valid = (
        conductor
        & np.isfinite(ambient_c)
        & np.isfinite(wind_speed)
        & np.isfinite(wind_angle)
        & np.isfinite(surface_c)
        & np.isfinite(elevation)
        & np.isfinite(emissivity)
        & np.isfinite(solar_gain)
        & np.isfinite(wildfire_heat)
        & (wind_speed >= 0.0)
        & (wind_angle >= 0.0)
        & (wind_angle <= 90.0)
        & (temperature_difference > 0.0)
        & (emissivity >= 0.0)
        & (emissivity <= 1.0)
        & (resistance > 0.0)
    )

    with np.errstate(invalid="ignore"):
        ampacity = np.where(
            valid & (thermal_margin > 0.0),
            np.sqrt(np.maximum(thermal_margin, 0.0) / resistance),
            np.where(valid, 0.0, np.nan),
        )

    output_directory = Path(args.out_dir)
    output_directory.mkdir(parents=True, exist_ok=True)
    tag = args.timestamp_tag

    outputs = {
        "film_temperature_c": (film_temperature_c, "air_film_temperature_c"),
        "air_density_kg_m3": (air_density, "air_density_kg_m3"),
        "air_dynamic_viscosity_pa_s": (
            dynamic_viscosity, "air_dynamic_viscosity_pa_s"),
        "air_thermal_conductivity_w_m_k": (
            thermal_conductivity, "air_thermal_conductivity_w_m_k"),
        "wind_direction_factor": (
            wind_direction_factor, "ieee738_wind_direction_factor"),
        "reynolds_number": (reynolds_number, "conductor_reynolds_number"),
        "convection_loss_w_m": (
            convection_loss, "selected_convection_heat_loss_w_m"),
        "radiation_loss_w_m": (radiation_loss, "radiated_heat_loss_w_m"),
        "solar_gain_w_m": (solar_gain, "solar_heat_gain_w_m"),
        "wildfire_heat_w_m": (wildfire_heat, "total_wildfire_heat_gain_w_m"),
        "resistance_ohm_m": (resistance, "interpolated_ac_resistance_ohm_m"),
        "thermal_margin_w_m": (
            thermal_margin, "heat_available_for_joule_heating_w_m"),
        "ampacity_a": (ampacity, "steady_state_ampacity_a"),
    }

    summary = {
        "timestamp_tag": tag,
        "calm_wind_policy": args.calm_wind_policy,
        "solar": "off" if args.solar_off else str(args.solar_gain_w_m),
        "conductor_cells": int(conductor.sum()),
        "valid_cells": int(valid.sum()),
        "calm_cells": int(calm.sum()),
        "statistics": {},
    }

    for stem, (array, description) in outputs.items():
        masked = np.where(valid, array, np.nan)
        write_float_raster(
            output_directory / f"{stem}_{tag}.tif",
            masked,
            profile,
            description=description,
        )
        summary["statistics"][stem] = conductor_statistics(masked, valid)

    if args.baseline_ampacity:
        baseline, _ = read_to_template(
            args.baseline_ampacity, template, Resampling.nearest
        )
        capacity_ratio = np.where(
            valid & np.isfinite(baseline) & (baseline > 0.0),
            ampacity / baseline,
            np.nan,
        )
        reduction = (1.0 - capacity_ratio) * 100.0

        write_float_raster(
            output_directory / f"capacity_ratio_{tag}.tif",
            capacity_ratio,
            profile,
            description="wildfire_ampacity_divided_by_baseline_ampacity",
        )
        write_float_raster(
            output_directory / f"capacity_reduction_pct_{tag}.tif",
            reduction,
            profile,
            description="capacity_reduction_relative_to_baseline_percent",
        )
        summary["statistics"]["capacity_ratio"] = conductor_statistics(
            capacity_ratio, valid
        )
        summary["statistics"]["capacity_reduction_pct"] = (
            conductor_statistics(reduction, valid)
        )

    summary_path = output_directory / f"rating_summary_{tag}.json"
    summary_path.write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )

    if summary["valid_cells"] == 0:
        raise RuntimeError(
            "No valid conductor cells. Check input coverage, wind angle "
            "nodata, and that surface temperature exceeds ambient."
        )

    stats = summary["statistics"]["ampacity_a"]
    print(
        f"Ampacity over {summary['valid_cells']} conductor cells: "
        f"min {stats['min']:.1f} A, mean {stats['mean']:.1f} A, "
        f"max {stats['max']:.1f} A"
    )
    print(f"Summary: {summary_path}")


if __name__ == "__main__":
    main()
