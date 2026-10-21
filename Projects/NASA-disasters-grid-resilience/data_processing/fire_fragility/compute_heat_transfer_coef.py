#!/usr/bin/env python3
"""
Estimate and check the wildfire heat-gain coefficients used by stage 10.

Standalone helper (not a pipeline stage). Replaces the legacy
``legacy/compute_heat_transfer_coef.py`` (hard-coded paths, flat-plate
correlation, and a wind-magnitude error). It addresses the two
coefficients in ``wildfire_heat``:

    convective_coefficient  f    manuscript Eq. 5
    radiative_fraction      X_r  manuscript Eq. 15

Convective term
---------------
The manuscript term

    q_cw = f * 22 * (I_W^2 / h^5)^(1/3) * D            [W/m], I_W in W/m

is equivalent to a heat-transfer coefficient h_c = 100 f W/m^2K acting on a
plume excess temperature dT = 22 (I_kW^2 / h^5)^(1/3) over the projected
width D. This script estimates a physically consistent value instead:

    q_cw = h'(V) * dT_plume                             [W/m]

    h'(V)     IEEE 738 cylinder convection per unit length (W/m/K), the same
              correlation as compute_ieee738_rating.py, evaluated at the gas
              speed past the conductor V = sqrt(U^2 + w^2) (wind U plus
              line-plume updraft w), film properties at (T_s + T_gas)/2.
    dT_plume  plume excess temperature at height h, from one of
                vanwagner   b * I^(2/3) / h               (field fit, b = 3.85)
                line_plume  Yuan & Cox line plume, C_T = 2.6 (convective I)
                manuscript  22 * (I^2 / h^5)^(1/3)        (point-source form)
              optionally reduced for wind with Van Wagner's factor
              sqrt(0.0256 I / (0.0256 I + U^3)).

Reported: q_cw (gain form, comparable to Eq. 5), the net change in the
convective balance relative to the baseline rating,
    dq = h'(U) (T_s - T_a) - h'(V) (T_s - T_a - dT),
and the equivalent manuscript f (``f_equivalent``) that reproduces q_cw.

Radiative term
--------------
For fixed geometry the incident radiant flux is linear in X_r:

    q'' = X_r * I * cos(alpha_f) / (4 pi) * (2/r) * atan(L / (2r)),
    r = sqrt(h^2 + y^2)                                  [kW/m^2]

The script reports q'' and q_rw = q'' D for a list of X_r values, flags
fluxes above bounding flame emissive powers, and (optionally) estimates an
event-specific fraction from satellite or airborne fire radiative power,
X_r(detected) = FRP / (I * L_front). That ratio is a lower bound on the
total-basis fraction used by Eq. 15.

Validity flags
--------------
Flame length from Byram, L_f = 0.0775 I^0.46 (as in stage 1). Where
L_f / h >= 1 the conductor is in the flame and neither plume law nor the
point-source radiation model applies.

Modes
-----
Scalar:  --intensity-kw-m 424 1000 --height-m 10 --diameter-m 0.03 ...
Case:    --case-dir <work>/cases/<CASE>  (reads the stage 3/8/9/11 rasters,
         writes per-cell rasters and a summary JSON)
Presets: --list-presets  (literature values with sources)

Defaults for f, X_r, L, y, alpha_f, the conductor surface temperature, and
the elevation are read from --config when given.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import numpy as np

G = 9.81                     # m/s^2
CP_AIR = 1005.0              # J/kg/K
BYRAM_A, BYRAM_B = 0.0775, 0.46

# Line-plume constants (Yuan & Cox, as summarized in IAFSS AOFST 2-200).
LINE_PLUME_C_U = 2.04        # w = C_u (g Q' / (rho cp T))^(1/3)
LINE_PLUME_C_T = 2.6         # dT/T = C_T (g Q'/(rho cp T))^(2/3) / (g z)
VANWAGNER_B = 3.85           # dT = b I^(2/3) / z (I in kW/m)
VANWAGNER_WIND_A = 0.0256    # wind factor sqrt(a I / (a I + U^3))

FLUX_BOUNDS_KW_M2 = {
    "surface_fire_measured_max": 77.0,
    "crown_flame_emissive_mean": 200.0,
    "crown_measured_max": 263.0,
}

PRESETS = {
    "radiative_fraction": [
        ("Grass/forest fuels, lab, nadir MIR (FRE)", "0.12 +/- 0.03",
         "https://agupubs.onlinelibrary.wiley.com/doi/pdfdirect/10.1029/2007JD008679?download=true"),
        ("Grass/forest fuels, lab, nadir MIR (FRE)", "0.14 +/- 0.03",
         "https://gaftp.epa.gov/Air/aqmg/baker/info/Journal%20of%20Geophysical%20Research%20%20Atmospheres%20-%202005%20-%20Wooster.pdf"),
        ("Field surface fires, 151-813 kW/m", "0.17 (s.d. 0.03)",
         "https://www.fs.usda.gov/nrs/pubs/jrnl/2012/nrs_2012_kremens_001.pdf"),
        ("Wildland fuels, range", "0.13-0.22",
         "https://research.fs.usda.gov/download/treesearch/55650.pdf"),
        ("Lab fuel beds, flame + embers", "0.17-0.22",
         "https://hal.science/hal-00905612/document"),
        ("Dry grass, excelsior, Douglas fir, calorimetry", "0.23-0.34",
         "https://www.nist.gov/el/fcd/global-burning-properties-little-bluestem-excelsior-and-douglas-fir"),
        ("Wet Douglas fir (46 % moisture)", "0.13",
         "https://www.nist.gov/publications/global-burning-properties-little-bluestem-excelsior-and-douglas-fir"),
        ("Engineering point-source default", "0.30",
         "https://standards.ieee.org/wp-content/uploads/2026/04/NUREG-05.1-IEEE-979.xlsx"),
        ("Manuscript (smoky conditions)", "0.15",
         "https://doi.org/10.1016/j.epsr.2025.112467"),
    ],
    "convective": [
        ("Van Wagner line-fire plume, dT = b I^(2/3)/z", "b = 3.85 (higher reported)",
         "https://www.firescience.gov/ords/prd/jf_jfsp/file/getfile/PRODUCTS/32775"),
        ("Van Wagner wind correction", "0.741 I^(7/6) / (z (0.0256 I + U^3)^(1/2))",
         "https://www.fs.usda.gov/rm/pubs_other/rmrs_2009_cochrane_m001.pdf"),
        ("Line plume (Yuan & Cox)", "C_u = 2.04, C_T = 2.6",
         "https://publications.iafss.org/publications/aofst/2/200/view/aofst_2-200.pdf"),
        ("Point-source plume (Heskestad)", "dT = 25 Q^(2/3) z^(-5/3)",
         "https://formula.expert/solvers/plume-centreline-temperature"),
        ("Manuscript Eq. 5", "f = 0.1 (equiv. h_c = 10 W/m^2K on D)",
         "https://doi.org/10.1016/j.epsr.2025.112467"),
    ],
}


# ----------------------------------------------------------------------
# Physics
# ----------------------------------------------------------------------

def air_properties(film_c, elevation_m):
    """IEEE 738 film properties: density, viscosity, conductivity."""
    film_c = np.asarray(film_c, dtype="float64")
    rho = ((1.293 - 1.525e-4 * elevation_m + 6.379e-9 * elevation_m ** 2)
           / (1.0 + 0.00367 * film_c))
    mu = 1.458e-6 * (film_c + 273.0) ** 1.5 / (film_c + 383.4)
    k = 2.424e-2 + 7.477e-5 * film_c - 4.407e-9 * film_c ** 2
    return rho, mu, k


def convection_per_kelvin(speed, diameter, film_c, elevation_m, delta_k,
                          k_angle=1.0):
    """IEEE 738 convection per unit length and kelvin (W/m/K).

    Larger of the two forced correlations and natural convection.
    """
    rho, mu, k = air_properties(film_c, elevation_m)
    reynolds = diameter * rho * np.maximum(speed, 0.0) / mu
    forced_low = k_angle * (1.01 + 1.35 * reynolds ** 0.52) * k
    forced_high = k_angle * 0.754 * reynolds ** 0.6 * k
    dk = np.maximum(np.abs(delta_k), 1.0)
    natural = 3.645 * np.sqrt(rho) * diameter ** 0.75 * dk ** 1.25 / dk
    return np.maximum.reduce([forced_low, forced_high, natural])


def flame_length_m(intensity_kw_m):
    return BYRAM_A * np.power(np.maximum(intensity_kw_m, 0.0), BYRAM_B)


def wind_factor(intensity_kw_m, wind_m_s):
    a_i = VANWAGNER_WIND_A * np.maximum(intensity_kw_m, 1e-9)
    return np.sqrt(a_i / (a_i + np.maximum(wind_m_s, 0.0) ** 3))


def plume_excess_temperature(model, intensity_kw_m, height_m, air_c,
                             radiative_fraction, vanwagner_b, elevation_m):
    if model == "vanwagner":
        return vanwagner_b * intensity_kw_m ** (2.0 / 3.0) / height_m
    if model == "manuscript":
        return 22.0 * (intensity_kw_m ** 2 / height_m ** 5) ** (1.0 / 3.0)
    if model == "line_plume":
        t_k = air_c + 273.15
        rho, _, _ = air_properties(air_c, elevation_m)
        q_conv_w = (1.0 - radiative_fraction) * intensity_kw_m * 1000.0
        return (LINE_PLUME_C_T * t_k ** (1.0 / 3.0) * G ** (-1.0 / 3.0)
                * (q_conv_w / (rho * CP_AIR)) ** (2.0 / 3.0) / height_m)
    raise ValueError(f"unknown plume model {model!r}")


def plume_velocity(intensity_kw_m, air_c, radiative_fraction, elevation_m):
    t_k = air_c + 273.15
    rho, _, _ = air_properties(air_c, elevation_m)
    q_conv_w = (1.0 - radiative_fraction) * intensity_kw_m * 1000.0
    return LINE_PLUME_C_U * (G * q_conv_w / (rho * CP_AIR * t_k)) ** (1.0 / 3.0)


def manuscript_bracket(intensity_kw_m, height_m, diameter_m):
    """22 (I_W^2/h^5)^(1/3) D, the factor multiplying f in Eq. 5."""
    i_w = intensity_kw_m * 1000.0
    return 22.0 * (i_w ** 2 / height_m ** 5) ** (1.0 / 3.0) * diameter_m


def convective_estimate(p):
    """Vectorized convective estimate. ``p`` is a dict of arrays/scalars."""
    i, h, d = p["intensity_kw_m"], p["height_m"], p["diameter_m"]
    u, ta, ts, z = p["wind_m_s"], p["air_c"], p["surface_c"], p["elevation_m"]
    xr = p["radiative_fraction"]

    dt = plume_excess_temperature(p["plume_model"], i, h, ta, xr,
                                  p["vanwagner_b"], z)
    wf = wind_factor(i, u) if p["wind_correction"] else np.ones_like(dt)
    dt = dt * wf

    if p["plume_velocity_m_s"] is not None:
        w = np.full_like(dt, float(p["plume_velocity_m_s"]))
    elif p["include_updraft"]:
        w = plume_velocity(i, ta, xr, z)
    else:
        w = np.zeros_like(dt)
    speed = np.sqrt(u ** 2 + w ** 2)

    t_gas = ta + dt
    hc_fire = convection_per_kelvin(speed, d, 0.5 * (ts + t_gas), z, ts - t_gas)
    hc_base = convection_per_kelvin(u, d, 0.5 * (ts + ta), z, ts - ta)

    q_gain = hc_fire * dt
    q_net = hc_base * (ts - ta) - hc_fire * (ts - ta - dt)
    bracket = manuscript_bracket(i, h, d)
    with np.errstate(divide="ignore", invalid="ignore"):
        f_equiv = np.where(bracket > 0, q_gain / bracket, np.nan)
        f_equiv_net = np.where(bracket > 0, q_net / bracket, np.nan)
    lf = flame_length_m(i)
    return {
        "plume_excess_temperature_k": dt,
        "wind_factor": wf,
        "plume_updraft_m_s": w,
        "gas_speed_m_s": speed,
        "h_prime_fire_w_m_k": hc_fire,
        "h_prime_baseline_w_m_k": hc_base,
        "h_c_fire_w_m2_k_perimeter": hc_fire / (math.pi * d),
        "q_cw_gain_w_m": q_gain,
        "q_cw_net_change_w_m": q_net,
        "q_cw_manuscript_w_m": p["convective_coefficient"] * bracket,
        "f_equivalent": f_equiv,
        "f_equivalent_net": f_equiv_net,
        "flame_length_m": lf,
        "flame_reach_ratio": lf / h,
    }


def radiative_flux_per_unit_xr(p):
    """Incident flux per unit X_r (kW/m^2)."""
    i, h, d = p["intensity_kw_m"], p["height_m"], p["diameter_m"]
    r = np.sqrt(h ** 2 + p["ground_offset_m"] ** 2)
    integral = 2.0 / r * np.arctan(p["front_length_m"] / (2.0 * r))
    return i * np.cos(np.deg2rad(p["front_angle_deg"])) / (4 * math.pi) * integral


def radiative_estimate(p, fractions):
    phi = radiative_flux_per_unit_xr(p)
    out = {"flux_per_unit_xr_kw_m2": phi, "by_fraction": {}}
    for xr in fractions:
        flux = xr * phi
        out["by_fraction"][f"{xr:g}"] = {
            "incident_flux_kw_m2": flux,
            "q_rw_w_m": flux * p["diameter_m"] * 1000.0,
            "exceeds_crown_flame_emissive_mean": flux
            > FLUX_BOUNDS_KW_M2["crown_flame_emissive_mean"],
        }
    # X_r at which the flux would reach each bound
    out["xr_at_bound"] = {
        k: np.where(phi > 0, v / phi, np.nan) for k, v in FLUX_BOUNDS_KW_M2.items()
    }
    return out


# ----------------------------------------------------------------------
# Config / IO
# ----------------------------------------------------------------------

def load_config(path):
    if not path:
        return {}
    import yaml
    with open(path, "r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def defaults_from_config(cfg):
    wh = cfg.get("wildfire_heat", {}) or {}
    rating = cfg.get("rating", {}) or {}
    elev = cfg.get("conductor_elevation", {}) or {}
    return {
        "convective_coefficient": wh.get("convective_coefficient", 0.1),
        "radiative_fraction": wh.get("radiative_fraction", 0.15),
        "front_length_m": wh.get("fire_front_length_m", 100.0),
        "ground_offset_m": wh.get("ground_offset_m", 0.0),
        "front_angle_deg": wh.get("fire_normal_angle_deg", 0.0),
        "surface_c": rating.get("maximum_surface_temperature_c", 75.0),
        "elevation_m": elev.get("value_m") or 0.0,
    }


def to_jsonable(value):
    if isinstance(value, dict):
        return {k: to_jsonable(v) for k, v in value.items()}
    if isinstance(value, np.ndarray):
        if value.ndim == 0:
            return to_jsonable(value.item())
        return [to_jsonable(v) for v in value.tolist()]
    if isinstance(value, (np.floating, float)):
        return None if not np.isfinite(value) else round(float(value), 6)
    if isinstance(value, (np.bool_, bool)):
        return bool(value)
    if isinstance(value, (np.integer,)):
        return int(value)
    return value


def summarize(arr):
    a = np.asarray(arr, dtype="float64")
    a = a[np.isfinite(a)]
    if a.size == 0:
        return None
    return {
        "count": int(a.size),
        "min": float(a.min()),
        "p10": float(np.percentile(a, 10)),
        "median": float(np.median(a)),
        "p90": float(np.percentile(a, 90)),
        "max": float(a.max()),
    }


def read_raster(path, template_profile=None):
    import rasterio
    from rasterio.warp import reproject, Resampling
    with rasterio.open(path) as src:
        data = src.read(1, masked=True).astype("float64").filled(np.nan)
        profile = src.profile.copy()
        if template_profile is None or (
            src.crs == template_profile["crs"]
            and src.transform == template_profile["transform"]
            and src.width == template_profile["width"]
            and src.height == template_profile["height"]
        ):
            return data, profile
        out = np.full((template_profile["height"], template_profile["width"]),
                      np.nan)
        reproject(data, out, src_transform=src.transform, src_crs=src.crs,
                  dst_transform=template_profile["transform"],
                  dst_crs=template_profile["crs"], src_nodata=np.nan,
                  dst_nodata=np.nan, resampling=Resampling.nearest)
        return out, template_profile


def write_raster(path, array, profile):
    import rasterio
    prof = profile.copy()
    prof.update(driver="GTiff", count=1, dtype="float32", nodata=-9999.0,
                compress="deflate")
    for key in ("blockxsize", "blockysize", "interleave", "tiled"):
        prof.pop(key, None)
    with rasterio.open(path, "w", **prof) as dst:
        dst.write(np.where(np.isfinite(array), array, -9999.0)
                  .astype("float32"), 1)


# ----------------------------------------------------------------------
# Modes
# ----------------------------------------------------------------------

def run_case(args, base):
    case_dir = Path(args.case_dir).resolve()
    work = case_dir.parents[1]
    tag = case_dir.name.split("_")[-1]
    fire = case_dir / "fire" / "line_fireline_intensity_kw_m.tif"
    intensity, profile = read_raster(fire)
    get = lambda p: read_raster(p, profile)[0]
    cond = work / "conductor"
    height = get(cond / "conductor_height_m.tif")
    diameter = get(cond / "conductor_diameter_m.tif")
    wind_path = work / "met" / tag / f"wind_speed_conductor_m_s_{tag}.tif"
    air_path = work / "era5_rasters" / f"air_temperature_c_{tag}.tif"
    wind = get(wind_path) if wind_path.is_file() else np.full_like(
        intensity, args.wind_m_s if args.wind_m_s is not None else 0.0)
    air = get(air_path) if air_path.is_file() else np.full_like(
        intensity, args.air_temp_c)
    elev_path = cond / "conductor_elevation_msl_m.tif"
    elevation = (get(elev_path) if elev_path.is_file()
                 else np.full_like(intensity, base["elevation_m"]))

    geom = case_dir / "fire_geometry"
    def geometry(name, scalar):
        path = geom / name
        if path.is_file():
            arr = get(path)
            return np.where(np.isfinite(arr), arr, scalar)
        return np.full_like(intensity, scalar)
    p = dict(base)
    p.update(
        intensity_kw_m=intensity, height_m=height, diameter_m=diameter,
        wind_m_s=wind, air_c=air, elevation_m=elevation,
        front_length_m=geometry("line_fire_front_length_m.tif",
                                base["front_length_m"]),
        ground_offset_m=geometry("line_ground_offset_m.tif",
                                 base["ground_offset_m"]),
        front_angle_deg=geometry("line_fire_front_angle_deg.tif",
                                 base["front_angle_deg"]),
    )
    valid = (np.isfinite(intensity) & (intensity > 0) & np.isfinite(height)
             & (height > 0) & np.isfinite(diameter) & (diameter > 0))
    if not valid.any():
        sys.exit(f"No conductor cells with fire intensity > 0 in {fire}")
    for key in ("intensity_kw_m", "height_m", "diameter_m", "wind_m_s",
                "air_c", "elevation_m", "front_length_m", "ground_offset_m",
                "front_angle_deg"):
        p[key] = np.where(valid, p[key], np.nan)

    with np.errstate(invalid="ignore", divide="ignore"):
        conv = convective_estimate(p)
        rad = radiative_estimate(p, args.radiative_fractions)

    out_dir = Path(args.out_dir) if args.out_dir else case_dir / "heat_coefficients"
    out_dir.mkdir(parents=True, exist_ok=True)
    for key in ("f_equivalent", "f_equivalent_net", "q_cw_gain_w_m",
                "q_cw_net_change_w_m", "plume_excess_temperature_k",
                "gas_speed_m_s", "flame_reach_ratio"):
        write_raster(out_dir / f"{key}.tif", conv[key], profile)
    write_raster(out_dir / "radiant_flux_per_unit_xr_kw_m2.tif",
                 rad["flux_per_unit_xr_kw_m2"], profile)

    in_flame = valid & (conv["flame_reach_ratio"] >= 1.0)
    summary = {
        "case_directory": str(case_dir),
        "timestamp_tag": tag,
        "settings": {k: base[k] for k in (
            "plume_model", "wind_correction", "include_updraft",
            "plume_velocity_m_s", "vanwagner_b", "radiative_fraction",
            "convective_coefficient", "surface_c")},
        "inputs": {
            "wind_raster": str(wind_path) if wind_path.is_file() else None,
            "air_temperature_raster": str(air_path) if air_path.is_file() else None,
            "fire_geometry_used": geom.is_dir(),
        },
        "fire_cells": int(valid.sum()),
        "cells_with_flame_reaching_conductor": int(in_flame.sum()),
        "convective": {k: summarize(v[valid]) for k, v in conv.items()},
        "radiative": {
            "flux_per_unit_xr_kw_m2": summarize(rad["flux_per_unit_xr_kw_m2"][valid]),
            "by_fraction": {
                xr: {"incident_flux_kw_m2": summarize(v["incident_flux_kw_m2"][valid]),
                     "q_rw_w_m": summarize(v["q_rw_w_m"][valid]),
                     "cells_exceeding_crown_flame_emissive_mean":
                         int(np.sum(v["exceeds_crown_flame_emissive_mean"] & valid))}
                for xr, v in rad["by_fraction"].items()},
            "xr_at_bound": {k: summarize(v[valid])
                            for k, v in rad["xr_at_bound"].items()},
        },
    }
    path = out_dir / "heat_coefficient_summary.json"
    path.write_text(json.dumps(to_jsonable(summary), indent=2), encoding="utf-8")

    c, r = summary["convective"], summary["radiative"]
    print(f"Case {case_dir.name}: {valid.sum()} fire cells "
          f"({in_flame.sum()} with flame reaching the conductor)")
    print(f"  plume dT (K)          median {c['plume_excess_temperature_k']['median']:.1f}"
          f"  [{c['plume_excess_temperature_k']['p10']:.1f}, {c['plume_excess_temperature_k']['p90']:.1f}]")
    print(f"  gas speed (m/s)       median {c['gas_speed_m_s']['median']:.1f}")
    print(f"  q_cw estimate (W/m)   median {c['q_cw_gain_w_m']['median']:.1f}"
          f"  vs manuscript f={base['convective_coefficient']}: "
          f"{c['q_cw_manuscript_w_m']['median']:.1f}")
    print(f"  f equivalent          median {c['f_equivalent']['median']:.2f}"
          f"  [{c['f_equivalent']['p10']:.2f}, {c['f_equivalent']['p90']:.2f}]")
    for xr, v in r["by_fraction"].items():
        print(f"  X_r={xr:<5} flux median {v['incident_flux_kw_m2']['median']:.2f} kW/m2"
              f"  q_rw median {v['q_rw_w_m']['median']:.1f} W/m")
    print(f"Wrote {path}")


def run_scalar(args, base):
    intens = np.asarray(args.intensity_kw_m, dtype="float64")
    p = dict(base)
    p.update(
        intensity_kw_m=intens,
        height_m=np.full_like(intens, args.height_m),
        diameter_m=np.full_like(intens, args.diameter_m),
        wind_m_s=np.full_like(intens, args.wind_m_s or 0.0),
        air_c=np.full_like(intens, args.air_temp_c),
        elevation_m=np.full_like(intens, base["elevation_m"]),
        front_length_m=np.full_like(intens, base["front_length_m"]),
        ground_offset_m=np.full_like(intens, base["ground_offset_m"]),
        front_angle_deg=np.full_like(intens, base["front_angle_deg"]),
    )
    conv = convective_estimate(p)
    rad = radiative_estimate(p, args.radiative_fractions)

    print(f"h = {args.height_m} m, D = {args.diameter_m} m, U = {args.wind_m_s or 0} m/s, "
          f"T_a = {args.air_temp_c} C, T_s = {base['surface_c']} C, "
          f"plume = {base['plume_model']}"
          f"{' + wind correction' if base['wind_correction'] else ''}")
    hdr = (f"{'I kW/m':>8} {'Lf/h':>5} {'dT K':>6} {'V m/s':>6} {'h_c':>6} "
           f"{'q_cw':>7} {'q_net':>7} {'q_ms':>6} {'f_eq':>6}")
    print("\nConvective (h_c W/m2K on perimeter; q in W/m; q_ms = manuscript f="
          f"{base['convective_coefficient']})")
    print(hdr)
    for k in range(intens.size):
        print(f"{intens[k]:8.0f} {conv['flame_reach_ratio'][k]:5.2f} "
              f"{conv['plume_excess_temperature_k'][k]:6.1f} "
              f"{conv['gas_speed_m_s'][k]:6.1f} "
              f"{conv['h_c_fire_w_m2_k_perimeter'][k]:6.1f} "
              f"{conv['q_cw_gain_w_m'][k]:7.1f} {conv['q_cw_net_change_w_m'][k]:7.1f} "
              f"{conv['q_cw_manuscript_w_m'][k]:6.1f} {conv['f_equivalent'][k]:6.2f}"
              f"{'  IN FLAME' if conv['flame_reach_ratio'][k] >= 1 else ''}")

    print(f"\nRadiative (L = {base['front_length_m']} m, y = {base['ground_offset_m']} m, "
          f"alpha_f = {base['front_angle_deg']} deg)")
    print(f"{'I kW/m':>8} " + " ".join(f"{'Xr=' + format(x, 'g'):>16}"
                                      for x in args.radiative_fractions)
          + f" {'Xr@200kW/m2':>12}")
    for k in range(intens.size):
        cells = []
        for x in args.radiative_fractions:
            v = rad["by_fraction"][f"{x:g}"]
            cells.append(f"{v['incident_flux_kw_m2'][k]:6.2f}kW {v['q_rw_w_m'][k]:6.1f}W")
        print(f"{intens[k]:8.0f} " + " ".join(f"{c:>16}" for c in cells)
              + f" {rad['xr_at_bound']['crown_flame_emissive_mean'][k]:12.2f}")

    result = {"inputs": {k: p[k] for k in p}, "convective": conv,
              "radiative": rad}
    if args.frp_mw is not None:
        front = args.burning_front_length_m or base["front_length_m"]
        i_ref = float(np.max(intens))
        hrr_mw = args.hrr_mw if args.hrr_mw is not None else i_ref * front / 1000.0
        xr_det = args.frp_mw / hrr_mw
        result["frp_calibration"] = {
            "frp_mw": args.frp_mw, "hrr_mw": hrr_mw,
            "xr_detected_lower_bound": xr_det,
        }
        print(f"\nFRP calibration: FRP {args.frp_mw} MW / HRR {hrr_mw:.1f} MW "
              f"-> X_r(detected) = {xr_det:.3f} (lower bound on total-basis X_r)")
    if args.out_json:
        Path(args.out_json).write_text(json.dumps(to_jsonable(result), indent=2),
                                       encoding="utf-8")
        print(f"\nWrote {args.out_json}")


def list_presets():
    for group, rows in PRESETS.items():
        print(f"\n{group}")
        for label, value, url in rows:
            print(f"  {label:<52} {value:<34} {url}")


def build_parser():
    ap = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--config", help="pipeline.yaml for defaults")
    ap.add_argument("--list-presets", action="store_true",
                    help="print literature values and sources, then exit")
    mode = ap.add_argument_group("inputs")
    mode.add_argument("--case-dir", help="<work>/cases/<CASE> (raster mode)")
    mode.add_argument("--out-dir", help="raster-mode output directory "
                      "(default <case>/heat_coefficients)")
    mode.add_argument("--intensity-kw-m", type=float, nargs="+",
                      help="scalar mode: one or more fireline intensities")
    mode.add_argument("--height-m", type=float, default=10.0)
    mode.add_argument("--diameter-m", type=float, default=0.03)
    mode.add_argument("--wind-m-s", type=float, default=None,
                      help="wind speed at the conductor (raster mode: fallback)")
    mode.add_argument("--air-temp-c", type=float, default=35.0)
    mode.add_argument("--surface-temp-c", type=float, default=None,
                      help="conductor surface temperature (default: rating MOT)")
    mode.add_argument("--elevation-m", type=float, default=None)
    mode.add_argument("--front-length-m", type=float, default=None)
    mode.add_argument("--ground-offset-m", type=float, default=None)
    mode.add_argument("--front-angle-deg", type=float, default=None)
    conv = ap.add_argument_group("convective")
    conv.add_argument("--plume-model", default="vanwagner",
                      choices=["vanwagner", "line_plume", "manuscript"])
    conv.add_argument("--vanwagner-b", type=float, default=VANWAGNER_B)
    conv.add_argument("--no-wind-correction", action="store_true")
    conv.add_argument("--no-updraft", action="store_true",
                      help="gas speed = wind only (lower bound on h_c)")
    conv.add_argument("--plume-velocity-m-s", type=float, default=None,
                      help="override the line-plume updraft speed")
    conv.add_argument("--convective-coefficient", type=float, default=None,
                      help="current f for comparison (default from config)")
    rad = ap.add_argument_group("radiative")
    rad.add_argument("--radiative-fraction", type=float, default=None,
                     help="X_r used for the plume heat split (default config)")
    rad.add_argument("--radiative-fractions", type=float, nargs="+",
                     default=[0.10, 0.15, 0.20, 0.30])
    rad.add_argument("--frp-mw", type=float, default=None,
                     help="observed fire radiative power for the same front")
    rad.add_argument("--burning-front-length-m", type=float, default=None)
    rad.add_argument("--hrr-mw", type=float, default=None,
                     help="heat release rate; default I_max x front length")
    ap.add_argument("--out-json", help="scalar mode: write results as JSON")
    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.list_presets:
        list_presets()
        return 0
    if not args.case_dir and not args.intensity_kw_m:
        build_parser().error("give --case-dir or --intensity-kw-m "
                             "(or --list-presets)")

    base = defaults_from_config(load_config(args.config))
    overrides = {
        "convective_coefficient": args.convective_coefficient,
        "radiative_fraction": args.radiative_fraction,
        "front_length_m": args.front_length_m,
        "ground_offset_m": args.ground_offset_m,
        "front_angle_deg": args.front_angle_deg,
        "surface_c": args.surface_temp_c,
        "elevation_m": args.elevation_m,
    }
    base.update({k: v for k, v in overrides.items() if v is not None})
    base.update(
        plume_model=args.plume_model,
        vanwagner_b=args.vanwagner_b,
        wind_correction=not args.no_wind_correction,
        include_updraft=not args.no_updraft,
        plume_velocity_m_s=args.plume_velocity_m_s,
    )
    if args.case_dir:
        run_case(args, base)
    else:
        run_scalar(args, base)
    return 0


if __name__ == "__main__":
    sys.exit(main())
