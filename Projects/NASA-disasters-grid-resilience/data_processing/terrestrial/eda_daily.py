#!/usr/bin/env python3
"""
Exploratory analysis and quality check of the gridded daily weather files.

Reads every daily/weather_<region>_<YYYYMMDD>.nc in a folder and writes, to an
output folder:

  summary.csv              one row per variable: units, % missing, % zero, quantiles,
                           physical-range violations, leftover placeholder values,
                           constant / stuck fields
  flags.csv                one row per problem found (what to look at first)
  completeness.png         % of grid cells with data, per variable and day
  coverage_maps.png        share of hours with data per cell (radar, RTMA, SWDI)
  dist_<group>.png         value distributions per variable
  maps_<group>.png         monthly mean (environment) or monthly max (hazard) per cell
  exceedance_maps.png      hours above a hazard threshold per cell
  exceedance_daily.png     share of CONUS above each threshold, per day
  diurnal.png              threshold exceedance by local solar hour
  correlation.png          Spearman correlation, all hours and storm hours
  redundant_pairs.csv      variable pairs with |r| >= 0.9
  report.html              all of the above on one page

Usage
    python -m data_processing.terrestrial.eda_daily --daily-dir DATA/daily --out DATA/eda
    python eda_daily.py --daily-dir DATA/daily --out DATA/eda --start 2024-06-01 --end 2024-06-30

Thresholds in HAZARD_THRESHOLDS are starting points for a first look, not final
hazard definitions.
"""

import argparse
import glob
import html
import os
import re
import warnings
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import xarray as xr

warnings.filterwarnings("ignore", category=RuntimeWarning)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

MRMS_SUFFIX = re.compile(r"_00\.(50|00)$")

# Source groups, in report order. Matched against the variable's short name.
GROUPS = [
    ("MRMS radar", re.compile(r"^(Merged|EchoTop|VII|VIL_Density|MESH|POSH|RotationTrack|PrecipRate|MultiSensor)")),
    ("MRMS precipitation type", re.compile(r"^PrecipFlag")),
    ("MRMS FLASH", re.compile(r"^FLASH_")),
    ("MRMS lightning", re.compile(r"^NLDN")),
    ("RTMA", re.compile(r"^(t2m|d2m|sh2|sp|vis|ceil|tcc|wspd10|i10fg|u10|v10|wdir10|orog|rh2_min|heat_index_max|fireweather_frac)")),
    ("SWDI", re.compile(r"^(MAX_SHEAR|MXDV|PROB|MAXSIZE|VIL|MAX_REFLECT)$")),
]
GROUP_COLOR = {   # same validated palette as the coverage diagram
    "MRMS radar": "#2a78d6", "MRMS precipitation type": "#2a78d6", "MRMS FLASH": "#1baf7a",
    "MRMS lightning": "#eda100", "RTMA": "#008300", "SWDI": "#e87ba4", "other": "#5b6b78",
}

# Physical ranges in the units stored in the file (short names; regex -> (lo, hi))
RANGES = [
    (r"^(t2m|t2m_min|t2m_max|d2m|d2m_max|heat_index_max)$", 200, 340),     # K
    (r"^rh2_min$", 0, 100),
    (r"^(sh2|sh2_max)$", 0, 0.04),
    (r"^sp$", 45000, 110000),                                               # Pa
    (r"^(vis|vis_min)$", 0, 1e5),
    (r"^(ceil|ceil_min)$", 0, 1e5),
    (r"^tcc$", 0, 100),
    (r"^(wspd10|i10fg)$", 0, 90),
    (r"^(u10|v10)$", -90, 90),
    (r"^wdir10$", 0, 360),
    (r"frac", 0, 1),
    (r"^Merged.*Reflectivity", -35, 85),                                    # dBZ
    (r"^MAX_REFLECT$", 0, 85),
    (r"^EchoTop", 0, 25),                                                   # km
    (r"^MESH", 0, 200),                                                     # mm
    (r"^POSH$", 0, 100),
    (r"^MergedAzShear", -200, 200),                                         # 0.001 s-1 as stored
    (r"^RotationTrack", 0, 200),
    (r"^(VII|VIL_Density|PrecipRate|MultiSensor|NLDN|FLASH_QPE_ARI|FLASH_QPE_FFG|FLASH_CREST)", 0, np.inf),
    (r"^FLASH_CREST_MAXSOILSAT$", 0, 100),
    (r"^PROB$", 0, 100),
]

# Starting-point thresholds for a first look at hazard frequency (file units).
HAZARD_THRESHOLDS = [
    # label, short variable name, operator, value, note
    ("Hail ≥ 25 mm", "MESH_Max_60min", ">=", 25.4, "MESH, 1 inch"),
    ("Low-level rotation", "MergedAzShear_0-2kmAGL", ">=", 6, "AzShear 0-2 km, 0.001 s⁻¹ (= 0.006 s⁻¹)"),
    ("Gust ≥ 26 m/s", "i10fg", ">=", 25.9, "58 mph, NWS severe"),
    ("Lightning", "NLDN_CG_flashes_1h", ">=", 1, "≥ 1 CG flash in the hour"),
    ("Flood guidance exceeded", "FLASH_QPE_FFGMAX", ">=", 100, "rain ≥ 100 % of flash-flood guidance"),
    ("Rain ≥ 10-yr", "FLASH_QPE_ARIMAX", ">=", 10, "recurrence interval, years"),
    ("24-h rain ≥ 50 mm", "MultiSensor_QPE_24H_Pass2", ">=", 50, "mm"),
    ("Heat index ≥ 103 °F", "heat_index_max", ">=", 312.6, "K"),
    ("Freezing", "t2m_min", "<=", 273.15, "K"),
    ("Fire weather", "fireweather_frac", ">=", 0.25, "≥ 25 % of cell"),
    ("Fog", "vis_frac_lt1km", ">=", 0.5, "≥ 50 % of cell under 1 km"),
    ("Snow", "PrecipFlag_frac_snow", ">=", 0.25, "≥ 25 % of cell"),
]

STORM_VAR, STORM_DBZ = "MergedReflectivityComposite", 30.0   # "storm hours" for correlation
EXCLUDE_FROM_CORR = {"PrecipFlag_dominant", "wdir10", "orog"}   # categorical / circular / static
SENTINEL_LE = -90.0     # leftover MRMS placeholders (-99, -999) should never survive cleaning

INK, MUTED, LINE, BG = "#16212b", "#5b6b78", "#d6dee4", "#ffffff"
plt.rcParams.update({
    "font.size": 9, "axes.titlesize": 9.5, "axes.titleweight": "bold",
    "axes.edgecolor": LINE, "axes.labelcolor": MUTED, "xtick.color": MUTED, "ytick.color": MUTED,
    "axes.spines.top": False, "axes.spines.right": False, "figure.facecolor": BG,
    "axes.facecolor": BG, "savefig.facecolor": BG, "savefig.dpi": 130,
})
SEQ = plt.get_cmap("Blues").copy()
SEQ.set_bad("#eceff2")
DIV = plt.get_cmap("RdBu_r").copy()


def short(name):
    return MRMS_SUFFIX.sub("", name)


def group_of(name):
    s = short(name)
    for g, rx in GROUPS:
        if rx.search(s):
            return g
    return "other"


def range_for(name):
    s = short(name)
    for rx, lo, hi in RANGES:
        if re.search(rx, s):
            return lo, hi
    return None


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------
class Month:
    """Lazy access to a set of daily files; loads one variable at a time."""

    def __init__(self, files):
        self.files = files
        self.dss = [xr.open_dataset(f) for f in files]
        first = self.dss[0]
        self.x, self.y = first["x"].values, first["y"].values
        self.lat = first["lat"].values if "lat" in first else None
        self.lon = first["lon"].values if "lon" in first else None
        names = []
        for ds in self.dss:
            names += [v for v in ds.data_vars if v not in names]
        self.names = names
        self.missing_in = {v: [Path(f).name for f, ds in zip(files, self.dss) if v not in ds]
                           for v in names}
        self.time = pd.DatetimeIndex(np.concatenate([ds["time"].values for ds in self.dss]))
        self.attrs = {v: next(ds[v].attrs for ds in self.dss if v in ds) for v in names}
        self.dims = {v: next(ds[v].dims for ds in self.dss if v in ds) for v in names}

    def has_time(self, v):
        return "time" in self.dims[v]

    def load(self, v):
        """(time, y, x) float32 array over the whole period; NaN where a file lacks v."""
        if not self.has_time(v):
            return self.dss[0][v].values.astype("float32")
        parts = []
        for ds in self.dss:
            if v in ds:
                parts.append(ds[v].values.astype("float32"))
            else:
                parts.append(np.full((ds.sizes["time"], len(self.y), len(self.x)), np.nan, "float32"))
        return np.concatenate(parts, axis=0)

    def find(self, short_name):
        for v in self.names:
            if short(v) == short_name:
                return v
        return None

    def close(self):
        for ds in self.dss:
            ds.close()


# ---------------------------------------------------------------------------
# Per-variable statistics and checks
# ---------------------------------------------------------------------------
def stats_for(v, a, month):
    valid = a[np.isfinite(a)]
    n = a.size
    row = dict(variable=v, short=short(v), group=group_of(v),
               units=month.attrs[v].get("units", ""),
               cell_statistic=month.attrs[v].get("cell_statistic", ""),
               files_missing=len(month.missing_in[v]),
               pct_missing=100 * (1 - valid.size / n) if n else np.nan)
    if valid.size == 0:
        row.update(pct_zero=np.nan, min=np.nan, p01=np.nan, p50=np.nan, p95=np.nan,
                   p99=np.nan, p999=np.nan, max=np.nan, mean=np.nan,
                   n_sentinel=0, n_out_of_range=0, constant=True, stuck_hours=0)
        return row
    q = np.percentile(valid, [1, 50, 95, 99, 99.9])
    lo_hi = range_for(v)
    out_of_range = 0
    if lo_hi is not None:
        lo, hi = lo_hi
        out_of_range = int(((valid < lo - 1e-6) | (valid > hi + 1e-6)).sum())
    stuck = 0
    skip_stuck = short(v).endswith("valid_frac") or short(v) == "PrecipFlag_dominant"
    if a.ndim == 3 and a.shape[0] > 1 and not skip_stuck:
        same = np.all((a[1:] == a[:-1]) | (np.isnan(a[1:]) & np.isnan(a[:-1])), axis=(1, 2))
        varying = np.nanstd(a[1:].reshape(a.shape[0] - 1, -1), axis=1) > 0
        stuck = int((same & varying).sum())
    row.update(pct_zero=100 * (valid == 0).mean(), min=valid.min(), p01=q[0], p50=q[1],
               p95=q[2], p99=q[3], p999=q[4], max=valid.max(), mean=valid.mean(),
               n_sentinel=int((valid <= SENTINEL_LE).sum()) if not short(v).startswith(("sp", "orog")) else 0,
               n_out_of_range=out_of_range, constant=bool(valid.min() == valid.max()),
               stuck_hours=stuck)
    return row


def consistency_checks(month, cache):
    """Checks between variables. Returns list of flag dicts."""
    flags = []

    def frac_bad(a_name, b_name, op, tol, label):
        va, vb = month.find(a_name), month.find(b_name)
        if va is None or vb is None:
            return
        a, b = cache(va), cache(vb)
        ok = np.isfinite(a) & np.isfinite(b)
        if not ok.any():
            return
        bad = (a[ok] > b[ok] + tol) if op == "<=" else (a[ok] < b[ok] - tol)
        if bad.mean() > 0.001:
            flags.append(dict(variable=f"{a_name} vs {b_name}", check="consistency",
                              detail=f"{label}: violated in {100 * bad.mean():.2f} % of cell-hours"))

    # radar products that should be 0 (not NaN) wherever radar sees nothing
    ref = month.find("MergedReflectivityComposite")
    if ref is not None:
        cov = np.isfinite(cache(ref))
        if cov.any():
            for v in month.names:
                s = short(v)
                if v == ref or s.endswith("valid_frac") or not s.startswith(
                        ("EchoTop", "VII", "VIL_Density", "MESH", "POSH", "NLDN", "MergedAzShear",
                         "RotationTrack", "PrecipRate", "MultiSensor")):
                    continue
                miss = np.isnan(cache(v)[cov]).mean()
                if miss > 0.2:
                    flags.append(dict(variable=v, check="no-echo stored as missing",
                                      detail=f"NaN in {100 * miss:.0f} % of cell-hours where radar has data; "
                                             "nothing detected should be 0"))

    frac_bad("d2m", "t2m", "<=", 0.5, "dew point should not exceed temperature")
    frac_bad("t2m_min", "t2m_max", "<=", 0.01, "cell min should not exceed cell max")
    frac_bad("i10fg", "wspd10", ">=", 1.0, "gust should not be below sustained wind")
    frac_bad("MultiSensor_QPE_01H_Pass2", "MultiSensor_QPE_24H_Pass2", "<=", 0.5, "1-h rain ≤ 24-h rain")
    frac_bad("MultiSensor_QPE_24H_Pass2", "MultiSensor_QPE_72H_Pass2", "<=", 0.5, "24-h rain ≤ 72-h rain")
    return flags


def time_checks(month):
    t = month.time
    expected = pd.date_range(t.min().normalize(), t.max().normalize() + pd.Timedelta(hours=23), freq="h")
    flags = []
    miss = expected.difference(t)
    if len(miss):
        flags.append(dict(variable="(all)", check="missing hours",
                          detail=f"{len(miss)} hour(s) absent: "
                                 + ", ".join(m.strftime("%Y-%m-%d %H:00") for m in miss[:8])
                                 + (" ..." if len(miss) > 8 else "")))
    if t.duplicated().any():
        flags.append(dict(variable="(all)", check="duplicate hours",
                          detail=f"{int(t.duplicated().sum())} timestamp(s) appear twice"))
    return flags


def flags_from_stats(df, month):
    flags = []
    for r in df.itertuples():
        if r.files_missing:
            flags.append(dict(variable=r.variable, check="missing in files",
                              detail=f"absent from {r.files_missing} file(s): "
                                     + ", ".join(month.missing_in[r.variable][:5])))
        if r.pct_missing >= 50 and not r.short.endswith("valid_frac"):
            flags.append(dict(variable=r.variable, check="mostly missing",
                              detail=f"{r.pct_missing:.0f} % of cell-hours are NaN"))
        if r.n_sentinel:
            flags.append(dict(variable=r.variable, check="placeholder values",
                              detail=f"{r.n_sentinel} values ≤ {SENTINEL_LE} (e.g. -99/-999 not cleaned)"))
        if r.n_out_of_range:
            lo, hi = range_for(r.variable)
            flags.append(dict(variable=r.variable, check="out of physical range",
                              detail=f"{r.n_out_of_range} values outside [{lo}, {hi}] {r.units}; "
                                     f"min {r.min:.4g}, max {r.max:.4g}"))
        if r.constant and r.pct_missing < 100 and not r.short.endswith("valid_frac"):
            flags.append(dict(variable=r.variable, check="constant",
                              detail=f"every value equals {r.min:.4g}"))
        if r.pct_missing == 100:
            flags.append(dict(variable=r.variable, check="empty", detail="no valid values at all"))
        if r.stuck_hours:
            flags.append(dict(variable=r.variable, check="repeated hours",
                              detail=f"{r.stuck_hours} hour(s) identical to the previous hour "
                                     "(a stale file reused, or normal for a slowly changing product)"))
    if "FLASH_QPE_FFGMAX" in set(df["short"]):
        p999 = float(df.loc[df["short"] == "FLASH_QPE_FFGMAX", "p999"].iloc[0])
        if p999 < 20:
            flags.append(dict(variable="FLASH_QPE_FFGMAX", check="scale",
                              detail=f"99.9th percentile {p999:.3g}: looks like a ratio, not percent; "
                                     "use 1 instead of 100 as the exceedance threshold"))
    return flags


# ---------------------------------------------------------------------------
# Figures
# ---------------------------------------------------------------------------
def small_multiples(n, ncols=4, w=3.4, h=2.5):
    nrows = max(1, -(-n // ncols))
    fig, axes = plt.subplots(nrows, ncols, figsize=(w * ncols, h * nrows), squeeze=False)
    for ax in axes.flat[n:]:
        ax.set_visible(False)
    return fig, axes.flat


def fig_completeness(month, comp, out):
    """comp: {var: (time,) fraction of cells with data}"""
    names = [v for v in comp if not v.endswith("valid_frac")]
    t = month.time
    days = pd.DatetimeIndex(t.normalize().unique())
    mat = np.full((len(names), len(days)), np.nan)
    for i, v in enumerate(names):
        s = pd.Series(comp[v], index=t).groupby(t.normalize()).mean()
        mat[i] = s.reindex(days).values * 100
    fig, ax = plt.subplots(figsize=(min(18, 3 + 0.32 * len(days)), 0.19 * len(names) + 1.6))
    im = ax.imshow(mat, aspect="auto", cmap=SEQ, vmin=0, vmax=100, interpolation="nearest")
    ax.set_yticks(range(len(names)), [short(v) for v in names], fontsize=7)
    step = max(1, len(days) // 15)
    ax.set_xticks(range(0, len(days), step), [d.strftime("%m-%d") for d in days[::step]], fontsize=7)
    ax.set_title("Cells with data, % per day\n(light rows or columns are gaps)", loc="left")
    cb = fig.colorbar(im, ax=ax, fraction=0.025, pad=0.01)
    cb.set_label("% of cells")
    fig.tight_layout()
    fig.savefig(out / "completeness.png")
    plt.close(fig)


def map_ax(ax, month, field, title, cmap=SEQ, vmin=None, vmax=None, label=""):
    xk, yk = month.x / 1000, month.y / 1000
    im = ax.pcolormesh(xk, yk, np.ma.masked_invalid(field), cmap=cmap, vmin=vmin, vmax=vmax,
                       shading="nearest", rasterized=True)
    ax.set_aspect("equal")
    ax.set_xticks([]), ax.set_yticks([])
    for s in ax.spines.values():
        s.set_visible(False)
    ax.set_title(title, loc="left", fontsize=8.5)
    cb = ax.figure.colorbar(im, ax=ax, fraction=0.035, pad=0.02)
    cb.ax.tick_params(labelsize=7)
    if label:
        cb.set_label(label, fontsize=7)


def fig_coverage(month, cache, out):
    panels = []
    for title, sname in [("MRMS radar (composite reflectivity)", "MergedReflectivityComposite"),
                         ("MRMS FLASH (rain rarity)", "FLASH_QPE_ARIMAX"),
                         ("RTMA (2 m temperature)", "t2m")]:
        v = month.find(sname)
        if v is not None:
            panels.append((title, 100 * np.isfinite(cache(v)).mean(axis=0)))
    swdi = [month.find(s) for s in ("MAXSIZE", "MAX_SHEAR", "MAX_REFLECT")]
    swdi = [v for v in swdi if v is not None]
    if swdi:
        hits = sum(np.nan_to_num(cache(v)) > 0 for v in swdi)
        panels.append(("SWDI (hours with any detection)", 100 * (hits > 0).mean(axis=0)))
    if not panels:
        return
    fig, axes = small_multiples(len(panels), ncols=2, w=6, h=3.4)
    for ax, (title, f) in zip(axes, panels):
        map_ax(ax, month, f, title, vmin=0, vmax=100, label="% of hours")
    fig.suptitle("Coverage: share of hours with valid data in each cell", x=0.01, ha="left",
                 fontweight="bold")
    fig.tight_layout()
    fig.savefig(out / "coverage_maps.png")
    plt.close(fig)


def fig_distributions(df, cache, out):
    files = []
    for g in [g for g, _ in GROUPS] + ["other"]:
        rows = df[(df["group"] == g) & ~df["short"].str.endswith("valid_frac")]
        if rows.empty:
            continue
        fig, axes = small_multiples(len(rows))
        color = GROUP_COLOR[g]
        for ax, r in zip(axes, rows.itertuples()):
            a = cache(r.variable)
            valid = a[np.isfinite(a)].ravel()
            if r.short not in ("sp", "orog"):
                valid = valid[valid > SENTINEL_LE]      # leftover placeholders are flagged, not plotted
            if valid.size == 0:
                ax.text(0.5, 0.5, "no data", ha="center", va="center", color=MUTED, transform=ax.transAxes)
            else:
                floor = valid.min()
                at_floor = (valid == floor).mean()
                mostly_floor = at_floor > 0.5
                vals = valid[valid != floor] if mostly_floor else valid
                if vals.size > 2_000_000:
                    vals = np.random.default_rng(0).choice(vals, 2_000_000, replace=False)
                if vals.size:
                    lo, hi = np.percentile(vals, [0.1, 99.9])
                    if hi <= lo:
                        lo, hi = vals.min(), vals.max() + 1e-9
                    ax.hist(np.clip(vals, lo, hi), bins=50, color=color, alpha=0.85, edgecolor=BG,
                            linewidth=0.3, log=mostly_floor)
                sub = f"{100 * at_floor:.0f}% at {floor:.4g}" if mostly_floor else \
                      f"{r.pct_zero:.0f}% zero"
                sub += f" · {r.pct_missing:.0f}% NaN"
                if mostly_floor:
                    sub += " · rest shown, log"
                ax.text(0, 1.02, sub, transform=ax.transAxes, fontsize=7, color=MUTED, va="bottom")
            ax.set_title(f"{r.short}", loc="left", fontsize=8.5, pad=13)
            ax.set_xlabel(r.units, fontsize=7)
            ax.tick_params(labelsize=7)
        fig.suptitle(f"Distributions · {g}", x=0.01, ha="left", fontweight="bold")
        fig.tight_layout(rect=(0, 0, 1, 0.975))
        name = f"dist_{re.sub(r'[^a-z]+', '_', g.lower()).strip('_')}.png"
        fig.savefig(out / name)
        plt.close(fig)
        files.append((g, name))
    return files


def fig_maps(df, month, cache, out):
    files = []
    for g in [g for g, _ in GROUPS]:
        rows = df[(df["group"] == g) & ~df["short"].str.endswith("valid_frac")
                  & (df["short"] != "PrecipFlag_dominant")]
        if rows.empty:
            continue
        fig, axes = small_multiples(len(rows), ncols=3, w=4.6, h=2.9)
        for ax, r in zip(axes, rows.itertuples()):
            a = cache(r.variable)
            if a.ndim == 2:
                f, how = a, "static"
            elif str(r.cell_statistic).startswith(("max", "see aggregate_swdi")) or g == "SWDI":
                f, how = np.nanmax(a, axis=0), "monthly max"
            else:
                f, how = np.nanmean(a, axis=0), "monthly mean"
            vmin, vmax = np.nanpercentile(f, [2, 98]) if np.isfinite(f).any() else (0, 1)
            if vmax <= vmin:
                vmax = vmin + 1e-9
            unit = f" ({r.units})" if r.units else ""
            map_ax(ax, month, f, f"{r.short}\n{how}{unit}", vmin=vmin, vmax=vmax)
        fig.suptitle(f"Maps · {g}", x=0.01, ha="left", fontweight="bold")
        fig.tight_layout(rect=(0, 0, 1, 0.975))
        name = f"maps_{re.sub(r'[^a-z]+', '_', g.lower()).strip('_')}.png"
        fig.savefig(out / name)
        plt.close(fig)
        files.append((g, name))
    return files


def exceed(a, op, value):
    with np.errstate(invalid="ignore"):
        hit = a >= value if op == ">=" else a <= value
    return hit, np.isfinite(a)


def fig_exceedance(month, cache, out):
    avail = [(lab, month.find(s), op, val, note) for lab, s, op, val, note in HAZARD_THRESHOLDS
             if month.find(s) is not None]
    if not avail:
        return [], None
    t = month.time
    utc_h = t.hour.values
    local = None
    if month.lon is not None:
        local = np.floor((utc_h[:, None, None] + month.lon[None] / 15.0) % 24).astype(int)

    fig_m, ax_m = small_multiples(len(avail), ncols=3, w=4.6, h=2.9)
    fig_d, ax_d = small_multiples(len(avail), ncols=3, w=4.2, h=2.2)
    fig_h, ax_h = small_multiples(len(avail), ncols=4, w=3.3, h=2.2)
    rows = []
    days = t.normalize()
    for i, (lab, v, op, val, note) in enumerate(avail):
        hit, ok = exceed(cache(v), op, val)
        n_hours = hit.sum(axis=0).astype(float)
        n_hours[~ok.any(axis=0)] = np.nan
        if np.nansum(n_hours) > 0:
            map_ax(ax_m[i], month, np.where(n_hours > 0, n_hours, np.nan),
                   f"{lab}\n{short(v)} {op} {val:g}", vmin=0, label="hours")
        else:
            ax_m[i].set_title(f"{lab}\n{short(v)} {op} {val:g}", loc="left", fontsize=8.5)
            ax_m[i].text(0.5, 0.5, "never above threshold", ha="center", va="center",
                         color=MUTED, transform=ax_m[i].transAxes)
            ax_m[i].axis("off")
        share = pd.Series(100 * hit.sum(axis=(1, 2)) / np.maximum(ok.sum(axis=(1, 2)), 1), index=t)
        daily = share.groupby(days).mean()
        ax_d[i].plot(daily.index, daily.values, color=INK, lw=1.6)
        ax_d[i].fill_between(daily.index, daily.values, color=INK, alpha=0.08)
        ax_d[i].set_title(lab, loc="left")
        ax_d[i].set_ylabel("% of cells", fontsize=7)
        ax_d[i].tick_params(labelsize=7)
        ax_d[i].xaxis.set_major_formatter(matplotlib.dates.DateFormatter("%m-%d"))
        ax_d[i].set_ylim(bottom=0)
        ax_d[i].grid(axis="y", color=LINE, lw=0.6)
        if local is not None and not hit.any():
            ax_h[i].set_title(lab, loc="left")
            ax_h[i].text(0.5, 0.5, "never above threshold", ha="center", va="center",
                         color=MUTED, transform=ax_h[i].transAxes)
            ax_h[i].axis("off")
        elif local is not None:
            num = np.bincount(local[hit], minlength=24)
            den = np.bincount(local[ok], minlength=24)
            freq = 100 * num / np.maximum(den, 1)
            ax_h[i].bar(range(24), freq, color=INK, width=0.8, alpha=0.8)
            ax_h[i].set_title(lab, loc="left")
            ax_h[i].set_xticks([0, 6, 12, 18, 23])
            ax_h[i].tick_params(labelsize=7)
            ax_h[i].set_xlabel("local solar hour", fontsize=7)
            ax_h[i].grid(axis="y", color=LINE, lw=0.6)
        rows.append(dict(hazard=lab, variable=v, rule=f"{op} {val:g}", note=note,
                         cell_hours=int(hit.sum()),
                         pct_cell_hours=100 * hit.sum() / max(ok.sum(), 1),
                         cells_ever=int((n_hours > 0).sum()),
                         days_with_any=int((daily > 0).sum())))
    fig_m.suptitle("Hours above threshold, per cell", x=0.01, ha="left", fontweight="bold")
    fig_d.suptitle("Share of grid cells above threshold (daily mean of hourly share)", x=0.01,
                   ha="left", fontweight="bold")
    fig_h.suptitle("When it happens: % of cell-hours above threshold by local solar hour", x=0.01,
                   ha="left", fontweight="bold")
    for f, name in [(fig_m, "exceedance_maps.png"), (fig_d, "exceedance_daily.png"),
                    (fig_h, "diurnal.png")]:
        f.tight_layout(rect=(0, 0, 1, 0.97))
        f.savefig(out / name)
        plt.close(f)
    tab = pd.DataFrame(rows)
    tab.to_csv(out / "exceedance.csv", index=False)
    return ["exceedance_maps.png", "exceedance_daily.png", "diurnal.png"], tab


def spearman(mat):
    """Spearman correlation of columns (rows with NaN already dropped)."""
    from scipy.stats import rankdata
    ranks = np.column_stack([rankdata(c) for c in mat.T])
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.corrcoef(ranks, rowvar=False)


def fig_correlation(df, month, cache, out, n_sample=300_000):
    from scipy.cluster.hierarchy import leaves_list, linkage
    from scipy.spatial.distance import squareform

    names = [r.variable for r in df.itertuples()
             if not r.short.endswith("valid_frac") and r.short not in EXCLUDE_FROM_CORR
             and month.has_time(r.variable) and not r.constant and r.pct_missing < 90]
    if len(names) < 3:
        return None, None
    nt, ny, nx = len(month.time), len(month.y), len(month.x)
    rng = np.random.default_rng(0)
    idx = rng.choice(nt * ny * nx, size=min(n_sample, nt * ny * nx), replace=False)
    X = np.column_stack([cache(v).reshape(-1)[idx] for v in names])
    keep = np.isfinite(X).all(axis=1)
    X = X[keep]
    storm_v = month.find(STORM_VAR)
    panels = [("All cell-hours", X)]
    if storm_v is not None and storm_v in names:
        sx = X[:, names.index(storm_v)] >= STORM_DBZ
        if sx.sum() > 200:
            panels.append((f"Storm hours ({STORM_VAR} ≥ {STORM_DBZ:g} dBZ)", X[sx]))
    mats = []
    for _, M in panels:
        varying = M.std(axis=0) > 0
        R = np.full((len(names), len(names)), np.nan)
        sub = spearman(M[:, varying])
        R[np.ix_(varying, varying)] = sub
        mats.append(R)
    R0 = np.nan_to_num(mats[0], nan=0.0)
    np.fill_diagonal(R0, 1.0)
    D = np.clip(1 - np.abs(R0), 0, 2)
    order = leaves_list(linkage(squareform(D, checks=False), "average"))
    labels = [short(names[i]) for i in order]

    fig = plt.figure(figsize=(9.2 * len(mats) + 0.8, 8.8))
    gs = fig.add_gridspec(1, len(mats) + 1, width_ratios=[1] * len(mats) + [0.035], wspace=0.08)
    axes = [[fig.add_subplot(gs[0, k]) for k in range(len(mats))]]
    for ax, (title, M), R in zip(axes[0], panels, mats):
        Ro = R[np.ix_(order, order)]
        im = ax.imshow(Ro, cmap=DIV, vmin=-1, vmax=1, interpolation="nearest")
        ax.set_xticks(range(len(labels)), labels, rotation=90, fontsize=6.5)
        if ax is axes[0][0]:          # same order in every panel: label the rows once
            ax.set_yticks(range(len(labels)), labels, fontsize=6.5)
        else:
            ax.set_yticks([])
        ax.set_title(f"{title} · n = {len(M):,}", loc="left")
        for s in ax.spines.values():
            s.set_visible(False)
    fig.colorbar(im, cax=fig.add_subplot(gs[0, -1]), label="Spearman r")
    fig.suptitle("Correlation between variables (ordered so similar variables sit together)",
                 x=0.01, ha="left", fontweight="bold")
    fig.savefig(out / "correlation.png", bbox_inches="tight")
    plt.close(fig)

    pairs = []
    for k, (title, _) in enumerate(panels):
        R = mats[k]
        for i in range(len(names)):
            for j in range(i + 1, len(names)):
                if np.isfinite(R[i, j]) and abs(R[i, j]) >= 0.9:
                    pairs.append(dict(sample=title, var_a=short(names[i]), var_b=short(names[j]),
                                      spearman_r=round(float(R[i, j]), 3)))
    pairs = pd.DataFrame(pairs, columns=["sample", "var_a", "var_b", "spearman_r"])
    pairs["_s"] = pairs["sample"].str.startswith("All")
    pairs["_r"] = -pairs["spearman_r"].abs()
    pairs = pairs.sort_values(["_s", "_r"]).drop(columns=["_s", "_r"])
    pairs.to_csv(out / "redundant_pairs.csv", index=False)
    return "correlation.png", pairs


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
CSS = """
body{font:14px/1.5 system-ui,-apple-system,Segoe UI,sans-serif;color:#16212b;background:#f7f9fa;margin:0}
main{max-width:1200px;margin:0 auto;padding:24px 20px 60px;display:grid;gap:26px}
h1{margin:0;font-size:26px}h2{margin:0 0 6px;font-size:18px;border-bottom:1px solid #d6dee4;padding-bottom:4px}
p{margin:0 0 6px;max-width:75ch}.muted{color:#5b6b78}img{max-width:100%;background:#fff;border:1px solid #d6dee4;border-radius:6px}
.scroll{overflow-x:auto;border:1px solid #d6dee4;border-radius:6px;background:#fff}
table{border-collapse:collapse;font-size:12px;width:100%}th,td{padding:4px 8px;border-bottom:1px solid #e6ebef;text-align:right;white-space:nowrap}
th{background:#eef2f5;position:sticky;top:0}td:first-child,th:first-child,td.l{text-align:left}
td.l:last-child{white-space:normal;min-width:320px}
tr.bad td{background:#fbeaea}.chip{display:inline-block;padding:1px 8px;border-radius:99px;font-size:12px;font-weight:600}
.ok{background:#e4f2e9;color:#2f7d4f}.warn{background:#fbf0dc;color:#a8660b}
"""


def table_html(df, fmt=None, bad=None, left=()):
    fmt = fmt or {}
    head = "".join(f"<th>{html.escape(str(c))}</th>" for c in df.columns)
    body = []
    for _, r in df.iterrows():
        cls = ' class="bad"' if bad is not None and bad(r) else ""
        cells = []
        for c in df.columns:
            v = r[c]
            s = fmt[c](v) if c in fmt and pd.notna(v) else ("" if pd.isna(v) else str(v))
            cells.append(f'<td{" class=l" if c in left else ""}>{html.escape(s)}</td>')
        body.append(f"<tr{cls}>{''.join(cells)}</tr>")
    return f'<div class="scroll"><table><tr>{head}</tr>{"".join(body)}</table></div>'


def g4(x):
    return f"{x:.4g}"


def write_report(out, month, df, flags, dist_files, map_files, exc_files, exc_tab, corr_file, pairs):
    t0, t1 = month.time.min(), month.time.max()
    expected = pd.date_range(t0.normalize(), t1.normalize() + pd.Timedelta(hours=23), freq="h")
    missing_hours = len(expected.difference(month.time))
    dup = int(month.time.duplicated().sum())
    parts = [f"<h1>Daily weather files: exploratory analysis</h1>"
             f"<p class=muted>{len(month.files)} files · {t0:%Y-%m-%d} to {t1:%Y-%m-%d} · "
             f"{len(month.time)} hours ({missing_hours} missing, {dup} duplicated) · "
             f"grid {len(month.y)} × {len(month.x)} cells · {len(df)} variables</p>"]

    status = (f'<span class="chip ok">no problems found</span>' if not flags else
              f'<span class="chip warn">{len(flags)} item(s) to check</span>')
    parts.append(f"<section><h2>Checks {status}</h2>"
                 "<p class=muted>Missing variables, placeholder values left after cleaning, values outside "
                 "physical limits, constant or repeated fields, and checks between variables.</p>")
    if flags:
        parts.append(table_html(pd.DataFrame(flags), left=("variable", "check", "detail")))
    parts.append("</section>")

    parts.append("<section><h2>Completeness and coverage</h2>"
                 "<p class=muted>Rows are variables, columns are days. Radar products have no data offshore "
                 "and in radar gaps (mostly the mountain West); RTMA covers land and near-coastal water.</p>"
                 '<img src="completeness.png"><img src="coverage_maps.png"></section>')

    cols = ["short", "group", "units", "cell_statistic", "pct_missing", "pct_zero", "min", "p50",
            "p95", "p99", "p999", "max", "n_sentinel", "n_out_of_range", "stuck_hours"]
    view = df[~df["short"].str.endswith("valid_frac")][cols]
    fmt = {c: g4 for c in ["min", "p50", "p95", "p99", "p999", "max"]}
    fmt.update(pct_missing=lambda v: f"{v:.1f}", pct_zero=lambda v: f"{v:.1f}")
    parts.append("<section><h2>Summary statistics</h2>"
                 "<p class=muted>Percentiles over all valid cell-hours. Red rows have a flagged problem. "
                 "Full table, including the coverage fields, in summary.csv.</p>"
                 + table_html(view, fmt, bad=lambda r: (r.n_sentinel or r.n_out_of_range or r.stuck_hours) > 0,
                              left=("short", "group", "units", "cell_statistic"))
                 + "</section>")

    parts.append("<section><h2>Distributions</h2><p class=muted>For mostly-zero variables only the "
                 "non-zero values are shown, on a log count axis.</p>"
                 + "".join(f'<img src="{f}" alt="{html.escape(g)}">' for g, f in dist_files) + "</section>")
    parts.append("<section><h2>Maps</h2><p class=muted>Monthly maximum for hazard variables "
                 "(cell statistic max), monthly mean for the others. Colour range is the 2nd to 98th "
                 "percentile of each map.</p>"
                 + "".join(f'<img src="{f}" alt="{html.escape(g)}">' for g, f in map_files) + "</section>")
    if exc_files:
        tfmt = dict(pct_cell_hours=lambda v: f"{v:.3f}")
        parts.append("<section><h2>Hazard thresholds: first look</h2><p class=muted>Starting-point "
                     "thresholds (HAZARD_THRESHOLDS in the script), to see where and when each hazard "
                     "shows up. Not final hazard definitions.</p>"
                     + table_html(exc_tab, tfmt, left=("hazard", "variable", "rule", "note"))
                     + "".join(f'<img src="{f}">' for f in exc_files) + "</section>")
    if corr_file:
        parts.append("<section><h2>Correlation and redundancy</h2><p class=muted>Spearman correlation on a "
                     "random sample of cell-hours where every variable has data. Storm hours repeat the "
                     "calculation where composite reflectivity is at least 30 dBZ, which removes the many "
                     "calm, all-zero hours. Pairs with |r| ≥ 0.9 carry nearly the same information. Judge redundancy on "
                     "the storm-hour pairs (listed first): over all hours, two variables that are both zero "
                     "most of the time look correlated just because they are quiet together.</p>"
                     f'<img src="{corr_file}">'
                     + (table_html(pairs, left=("sample", "var_a", "var_b")) if len(pairs) else
                        "<p>No pairs with |r| ≥ 0.9.</p>") + "</section>")
    (out / "report.html").write_text(
        f"<!doctype html><meta charset=utf-8><title>Daily data EDA</title><style>{CSS}</style>"
        f"<main>{''.join(parts)}</main>", encoding="utf-8")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def run(daily_dir, out, start=None, end=None, pattern="weather_*.nc"):
    files = sorted(glob.glob(os.path.join(daily_dir, pattern)))
    if start or end:
        def day(f):
            m = re.search(r"(\d{8})", Path(f).name)
            return pd.Timestamp(m.group(1)) if m else None
        files = [f for f in files if day(f) is not None
                 and (start is None or day(f) >= pd.Timestamp(start))
                 and (end is None or day(f) <= pd.Timestamp(end))]
    if not files:
        raise SystemExit(f"No files matching {pattern} in {daily_dir}")
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    print(f"{len(files)} daily files")

    month = Month(files)
    store = {}

    def cache(v):
        if v not in store:
            store[v] = month.load(v)
        return store[v]

    rows, comp = [], {}
    for k, v in enumerate(month.names):
        if v in ("lat", "lon"):
            continue
        a = month.load(v) if v.endswith("valid_frac") else cache(v)   # coverage fields not kept in memory
        rows.append(stats_for(v, a, month))
        if a.ndim == 3:
            comp[v] = np.isfinite(a).mean(axis=(1, 2))
        print(f"\r  statistics {k + 1}/{len(month.names)}", end="", flush=True)
    print()
    df = pd.DataFrame(rows)
    order = {g: i for i, (g, _) in enumerate(GROUPS)}
    df["_o"] = df["group"].map(order).fillna(99)
    df = df.sort_values(["_o", "short"]).drop(columns="_o").reset_index(drop=True)
    df.to_csv(out / "summary.csv", index=False)

    flags = flags_from_stats(df, month) + consistency_checks(month, cache) + time_checks(month)
    pd.DataFrame(flags, columns=["variable", "check", "detail"]).to_csv(out / "flags.csv", index=False)

    print("  figures: completeness, coverage")
    fig_completeness(month, comp, out)
    fig_coverage(month, cache, out)
    print("  figures: distributions")
    dist_files = fig_distributions(df, cache, out)
    print("  figures: maps")
    map_files = fig_maps(df, month, cache, out)
    print("  figures: thresholds, diurnal cycle")
    exc_files, exc_tab = fig_exceedance(month, cache, out)
    print("  figures: correlation")
    corr_file, pairs = fig_correlation(df, month, cache, out)

    write_report(out, month, df, flags, dist_files, map_files, exc_files, exc_tab, corr_file,
                 pairs if pairs is not None else pd.DataFrame())
    month.close()
    print(f"Done: {len(flags)} item(s) flagged. Open {out / 'report.html'}")
    return df, flags


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--daily-dir", help="folder with weather_<region>_<date>.nc files")
    ap.add_argument("--out", help="output folder for figures, tables and report")
    ap.add_argument("--start", help="first day to include, YYYY-MM-DD")
    ap.add_argument("--end", help="last day to include, YYYY-MM-DD")
    ap.add_argument("--pattern", default="weather_*.nc")
    a = ap.parse_args()
    
    a.daily_dir="/Users/marchett/Documents/Disasters/data/V2/daily"
    a.out="/Users/marchett/Documents/Disasters/data/V2/eda"
    a.start="2024-06-01"
    a.end="2024-06-01"
    
    
    run(a.daily_dir, a.out, a.start, a.end, a.pattern)
    
    
    
    
    
    
