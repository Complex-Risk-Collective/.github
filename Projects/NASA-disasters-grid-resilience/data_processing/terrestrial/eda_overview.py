#!/usr/bin/env python3
"""
First-pass overview of the gridded daily files: one figure per question, every
variable in the same order as the hazard coverage diagram
(hazard_coverage_diagram.py), coloured and labelled by its hazard (Hail,
Tornado / rotation, ...) with the dataset (MRMS / RTMA / SWDI) in brackets.

Writes to --out:
  cells_with_data.png     bar chart: grid cells with any data / with any non-zero value
  histograms_<ds>.png     value distribution of every variable  (<ds> = mrms, rtma; swdi if it has data)
  max_maps_<ds>.png       highest value of the period per cell
  pctl_maps_<ds>.png      hours at/above the variable's p90 (at/below p10 for low-is-hazard variables)
  pctl_by_hour_<ds>.png   the same exceedances counted by local solar hour
  hazard_<name>.png       one page per hazard: histogram | max map | p90 map | p90 hours
  correlation.png         Spearman correlation, all hours and storm hours
  thresholds.csv          the percentile threshold used for each variable
  overview.html           all figures on one page

Maps carry state, province and country outlines (Natural Earth, 1:50m). They are
downloaded once from GitHub (~5 MB) into --boundaries-dir (default
~/.cache/multi_haz/boundaries); maps are drawn without them if that fails.

Usage
    python -m data_processing.terrestrial.eda_overview --daily-dir DATA/V2/daily --out DATA/V2/eda_overview
    python -m data_processing.terrestrial.eda_overview --daily-dir ... --out ... --pct 95 --start 2024-06-01 --end 2024-06-30
or from Python:
    from data_processing.terrestrial.eda_overview import run
    run(daily_dir, out, start="2024-06-01", end="2024-06-30", pct=90)
"""

import argparse
import glob
import os
import re
import warnings
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

warnings.filterwarnings("ignore", category=RuntimeWarning)

try:
    from data_processing.terrestrial import eda_daily as E
except ImportError:          # running next to the other scripts
    import eda_daily as E

try:
    from data_processing.terrestrial import hazard_coverage_diagram as D
except ImportError as _e1:
    try:
        import hazard_coverage_diagram as D
    except ImportError as _e2:
        D = None
        print("WARNING: could not import hazard_coverage_diagram "
              f"({_e1}; {_e2}). Figures will be grey and in file order. Put "
              "hazard_coverage_diagram.py next to this script (data_processing/terrestrial/).")

INK, MUTED, LINE, BG = E.INK, E.MUTED, E.LINE, E.BG
SEQ = E.SEQ

# Variables where the hazard is a LOW value: count hours below the (100 - pct)th percentile
LOW_TAIL = {"t2m_min", "rh2_min", "vis", "vis_min", "ceil", "ceil_min"}
# No percentile / hourly analysis: categorical, circular or static
NO_PCTL = {"PrecipFlag_dominant", "PrecipFlag_frac_none", "wdir10", "orog"}

# One colour per hazard (validated: adjacent hazards stay distinct, incl. colour-blind)
HAZARD_COLORS = {
    "Hail": "#2a78d6",
    "Tornado / rotation": "#4a3aa7",
    "Damaging wind": "#1baf7a",
    "Storm intensity": "#eda100",
    "Lightning": "#e34948",
    "Flash flood": "#008f9c",
    "Prolonged rain": "#9c5b1f",
    "Heat": "#eb6834",
    "Cold / freeze": "#1fa2d6",
    "Winter precipitation": "#c2185b",
    "Fire weather": "#8a7000",
    "Fog / low visibility": "#8e7cc3",
    "Environment and terrain": "#8a96a3",
}
DATASET = {"radar": "MRMS", "flash": "MRMS", "light": "MRMS", "rtma": "RTMA", "deriv": "RTMA",
           "swdi": "SWDI"}

FALLBACK_SOURCE = ("other", "Other", "#5b6b78")


# ---------------------------------------------------------------------------
# Variable order, source and colour (from the coverage diagram)
# ---------------------------------------------------------------------------
def variable_catalog(month):
    """
    [(file variable name, short name, source id, label, colour)] in diagram order:
    hazard by hazard, main variables first. label = "<hazard> (<dataset>)" and the
    colour is the hazard's; a variable listed under several hazards takes the
    first one. Variables not in the diagram are appended at the end.
    """
    by_short = {E.short(v): v for v in month.names
                if v not in ("lat", "lon") and not v.endswith("valid_frac")
                and v != "rtma_valid_frac"}
    out, seen = [], set()
    if D is not None:
        for item in D.HAZARDS:
            if isinstance(item, str):
                continue
            hz = item["name"]
            for sid, sv, _ in D.all_vars(item):
                if sv in by_short and sv not in seen:
                    seen.add(sv)
                    out.append((by_short[sv], sv, sid, f"{hz} ({DATASET.get(sid, sid)})",
                                HAZARD_COLORS.get(hz, FALLBACK_SOURCE[2])))
    for sv, v in by_short.items():
        if sv not in seen:
            out.append((v, sv, *FALLBACK_SOURCE))
    return out


# ---------------------------------------------------------------------------
# Boundary overlay (Natural Earth states/provinces and countries)
# ---------------------------------------------------------------------------
NE_URL = "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/{}.geojson"
NE_LAYERS = {   # file -> (line width, which features)
    "ne_50m_admin_1_states_provinces_lakes": 0.35,
    "ne_50m_admin_0_countries_lakes": 0.7,
}
NE_COUNTRIES = {"United States of America", "Canada", "Mexico"}
BOUNDARY_COLOR = "#3b4650"
_BOUNDARIES = []        # [(x_km array, y_km array, line width)], filled by load_boundaries


def load_boundaries(cache_dir, month, epsg=5070):
    """Read (downloading once) the outlines, project to the grid, keep those on the map."""
    import json
    import urllib.request
    from pyproj import Transformer

    _BOUNDARIES.clear()
    cache_dir = Path(os.path.expanduser(cache_dir))
    cache_dir.mkdir(parents=True, exist_ok=True)
    tr = Transformer.from_crs("EPSG:4326", f"EPSG:{epsg}", always_xy=True)
    x0, x1 = month.x.min() / 1000 - 100, month.x.max() / 1000 + 100
    y0, y1 = month.y.min() / 1000 - 100, month.y.max() / 1000 + 100
    for layer, lw in NE_LAYERS.items():
        path = cache_dir / f"{layer}.geojson"
        try:
            if not path.exists():
                print(f"  downloading {layer} (once)")
                tmp = path.with_name(path.name + ".tmp")
                urllib.request.urlretrieve(NE_URL.format(layer), tmp)
                os.replace(tmp, path)
            gj = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"  boundaries not drawn ({layer}: {e})")
            continue
        for feat in gj["features"]:
            props = feat.get("properties", {})
            owner = props.get("admin") or props.get("ADMIN") or props.get("NAME") or ""
            if owner not in NE_COUNTRIES:
                continue
            geom = feat["geometry"]
            polys = [geom["coordinates"]] if geom["type"] == "Polygon" else geom["coordinates"]
            for poly in polys:
                for ring in poly:
                    lon, lat = np.asarray(ring, dtype=float).T
                    xs, ys = tr.transform(lon, lat)
                    xs, ys = np.asarray(xs) / 1000, np.asarray(ys) / 1000
                    if xs.max() < x0 or xs.min() > x1 or ys.max() < y0 or ys.min() > y1:
                        continue
                    _BOUNDARIES.append((xs, ys, lw))
    return len(_BOUNDARIES)


def add_boundaries(ax):
    """Draw the loaded outlines on a map axis without changing its extent."""
    if not _BOUNDARIES:
        return
    xl, yl = ax.get_xlim(), ax.get_ylim()
    for xs, ys, lw in _BOUNDARIES:
        ax.plot(xs, ys, color=BOUNDARY_COLOR, lw=lw, alpha=0.75, zorder=3, solid_joinstyle="round")
    ax.set_xlim(xl)
    ax.set_ylim(yl)


def map_ax(ax, month, field, title, **kw):
    """eda_daily.map_ax plus the boundary overlay."""
    E.map_ax(ax, month, field, title, **kw)
    add_boundaries(ax)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def color_range(f):
    """2nd-98th percentile of a map; falls back to min-max, then to a unit range."""
    if not np.isfinite(f).any():
        return 0.0, 1.0
    lo, hi = np.nanpercentile(f, [2, 98])
    if hi <= lo:
        lo, hi = np.nanmin(f), np.nanmax(f)
    if hi <= lo:
        hi = lo + 1.0
    return float(lo), float(hi)


def grid_axes(n, ncols, w, h):
    nrows = max(1, -(-n // ncols))
    fig, axes = plt.subplots(nrows, ncols, figsize=(w * ncols, h * nrows), squeeze=False)
    for ax in axes.flat[n:]:
        ax.set_visible(False)
    return fig, list(axes.flat)


def panel_title(ax, sv, src_name, color, extra=""):
    ax.set_title(sv, loc="left", fontsize=8.5, pad=14)
    ax.text(0, 1.02, f"■ {src_name}" + (f" · {extra}" if extra else ""), transform=ax.transAxes,
            fontsize=7, color=color, va="bottom")


def save(fig, out, name, title):
    fig.suptitle(title, x=0.01, ha="left", fontweight="bold", fontsize=12)
    fig.tight_layout(rect=(0, 0, 1, 0.985))
    fig.savefig(out / name)
    plt.close(fig)
    return name


def percentile_threshold(a, sv, pct):
    """
    Threshold for 'unusually high' (or low, for LOW_TAIL) values, pooled over all
    cells and hours. When more than half of the values sit at the floor (0, or
    -30 dBZ for 'no echo'), the percentile is taken over the remaining values, so
    the threshold describes events rather than calm hours.
    Returns (threshold, direction '>=' or '<=', basis text) or None. Inclusive, so
    capped variables (fractions that reach 1) still count their top values.
    """
    v = a[np.isfinite(a)]
    if v.size == 0 or v.min() == v.max():
        return None
    if sv in LOW_TAIL:
        return float(np.percentile(v, 100 - pct)), "<=", f"p{100 - pct:g} of all values"
    floor = v.min()
    if (v == floor).mean() > 0.5:
        rest = v[v > floor]
        return float(np.percentile(rest, pct)), ">=", f"p{pct:g} of values above {floor:.3g}"
    return float(np.percentile(v, pct)), ">=", f"p{pct:g} of all values"


# ---------------------------------------------------------------------------
# Figures
# ---------------------------------------------------------------------------
def fig_cells_with_data(cat, cache, month, out):
    n_cells = len(month.y) * len(month.x)
    rows = []
    for v, sv, sid, sname, color in cat:
        a = cache(v)
        valid = np.isfinite(a)
        if a.ndim == 3:
            any_valid, any_signal = valid.any(axis=0), (valid & (a != 0)).any(axis=0)
        else:
            any_valid, any_signal = valid, valid & (a != 0)
        rows.append((sv, sname, color, int(any_valid.sum()), int(any_signal.sum())))
    fig, ax = plt.subplots(figsize=(9, 0.22 * len(rows) + 1.6))
    y = np.arange(len(rows))[::-1]
    ax.barh(y, [r[3] for r in rows], color=[r[2] for r in rows], alpha=0.35, height=0.8,
            label="cells with any valid value")
    ax.barh(y, [r[4] for r in rows], color=[r[2] for r in rows], height=0.45,
            label="cells with any non-zero value")
    ax.axvline(n_cells, color=MUTED, lw=1, ls=(0, (4, 3)))
    ax.text(n_cells, len(rows) - 0.2, f" all {n_cells} cells", color=MUTED, fontsize=8, va="bottom")
    ax.set_yticks(y, [r[0] for r in rows], fontsize=7.5)
    for tick, r in zip(ax.get_yticklabels(), rows):
        tick.set_color(INK)
    ax.set_xlabel("number of grid cells")
    ax.set_xlim(0, n_cells * 1.12)
    ax.grid(axis="x", color=LINE, lw=0.6)
    from matplotlib.patches import Patch
    handles = [Patch(color=MUTED, alpha=0.35, label="any valid value"),
               Patch(color=MUTED, label="any non-zero value")]
    seen = []
    for r in rows:
        hz = r[1].rsplit(" (", 1)[0]
        if hz not in seen:
            seen.append(hz)
            handles.append(Patch(color=r[2], label=hz))
    ax.legend(handles=handles, loc="upper center", bbox_to_anchor=(0.5, -0.06 * 30 / len(rows)),
              ncol=4, fontsize=8, frameon=False)
    pd.DataFrame(rows, columns=["variable", "hazard", "colour", "cells_any_valid",
                                "cells_any_nonzero"]).drop(columns="colour") \
        .to_csv(out / "cells_with_data.csv", index=False)
    return save(fig, out, "cells_with_data.png",
                "Grid cells with data over the period (pale bar) and with any non-zero value (dark bar)")


def fig_histograms(cat, cache, out, tag="", label=""):
    fig, axes = grid_axes(len(cat), 6, 3.1, 2.3)
    for ax, (v, sv, sid, sname, color) in zip(axes, cat):
        a = cache(v)
        vals = a[np.isfinite(a)].ravel()
        if sv not in ("sp", "orog"):
            vals = vals[vals > E.SENTINEL_LE]
        extra = ""
        if vals.size == 0:
            ax.text(0.5, 0.5, "no data", ha="center", va="center", color=MUTED, transform=ax.transAxes)
            extra = "no data"
        else:
            floor = vals.min()
            at_floor = (vals == floor).mean()
            mostly_floor = at_floor > 0.5 and vals.max() > floor
            show = vals[vals > floor] if mostly_floor else vals
            if show.size > 2_000_000:
                show = np.random.default_rng(0).choice(show, 2_000_000, replace=False)
            lo, hi = np.percentile(show, [0.1, 99.9])
            if hi <= lo:
                lo, hi = show.min(), show.max() + 1e-9
            ax.hist(np.clip(show, lo, hi), bins=40, color=color, alpha=0.85, edgecolor=BG,
                    linewidth=0.3, log=mostly_floor)
            extra = (f"{100 * at_floor:.0f}% at {floor:.3g}, rest shown (log)" if mostly_floor
                     else f"n = {vals.size:,}")
        units = cache.attrs(v).get("units", "")
        ax.set_xlabel(units, fontsize=7)
        ax.tick_params(labelsize=6.5)
        panel_title(ax, sv, sname, color, extra)
    return save(fig, out, f"histograms{tag}.png",
                f"{label}: distribution of each variable (all cells and hours)")


def fig_max_maps(cat, cache, month, out, tag="", label=""):
    fig, axes = grid_axes(len(cat), 5, 3.7, 2.45)
    for ax, (v, sv, sid, sname, color) in zip(axes, cat):
        a = cache(v)
        f = a if a.ndim == 2 else np.nanmax(a, axis=0)
        units = cache.attrs(v).get("units", "")
        lo, hi = color_range(f)
        map_ax(ax, month, f, "", vmin=lo, vmax=hi, label=units)
        panel_title(ax, sv, sname, color, "static" if a.ndim == 2 else "")
    return save(fig, out, f"max_maps{tag}.png",
                f"{label}: highest value of the period in each cell "
                "(colour range: 2nd to 98th percentile of the map)")


def fig_percentile(cat, cache, month, out, pct, tag="", label=""):
    """Returns ([map file, hour file], threshold rows)."""
    sel = [c for c in cat if c[1] not in NO_PCTL and cache(c[0]).ndim == 3]
    if not sel:
        return [], []
    local = None
    if month.lon is not None:
        local = np.floor((month.time.hour.values[:, None, None] + month.lon[None] / 15.0) % 24).astype(int)
    fig_m, ax_m = grid_axes(len(sel), 5, 3.7, 2.45)
    fig_h, ax_h = grid_axes(len(sel), 6, 3.1, 2.1)
    rows = []
    for k, (v, sv, sid, sname, color) in enumerate(sel):
        a = cache(v)
        thr = percentile_threshold(a, sv, pct)
        if thr is None:
            for ax in (ax_m[k], ax_h[k]):
                ax.axis("off")
                ax.text(0.5, 0.5, "constant or no data", ha="center", va="center", color=MUTED,
                        transform=ax.transAxes)
                panel_title(ax, sv, sname, color)
            continue
        t, op, basis = thr
        with np.errstate(invalid="ignore"):
            hit = a >= t if op == ">=" else a <= t
        cnt = hit.sum(axis=0).astype(float)
        cnt[~np.isfinite(a).any(axis=0)] = np.nan
        units = cache.attrs(v).get("units", "")
        rule = f"{op} {t:.3g} {units if units != '1' else ''}".strip()
        map_ax(ax_m[k], month, np.where(cnt > 0, cnt, np.nan), "", vmin=0, label="hours")
        panel_title(ax_m[k], sv, sname, color, rule)
        if local is not None:
            by_hour = np.bincount(local[hit], minlength=24)
            ax_h[k].bar(range(24), by_hour, color=color, width=0.8)
            ax_h[k].set_xticks([0, 6, 12, 18, 23])
            ax_h[k].tick_params(labelsize=6.5)
            ax_h[k].set_xlabel("local solar hour", fontsize=7)
            ax_h[k].grid(axis="y", color=LINE, lw=0.6)
            panel_title(ax_h[k], sv, sname, color, rule)
        rows.append(dict(variable=v, hazard=sname, threshold=t, direction=op, basis=basis,
                         units=units, cell_hours=int(hit.sum()), cells_ever=int((cnt > 0).sum())))
    note = (f"p{pct:g} pooled over all cells and hours (p{100 - pct:g} and 'below' for "
            f"low-is-hazard variables); for mostly-zero variables, of the non-zero values")
    f1 = save(fig_m, out, f"pctl_maps{tag}.png",
              f"{label}: hours at or beyond the {pct:g}th percentile per cell\n{note}")
    f2 = save(fig_h, out, f"pctl_by_hour{tag}.png",
              f"{label}: cell-hours at or beyond the {pct:g}th percentile by local solar hour")
    return [f1, f2], rows


def hazard_slug(name):
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def fig_hazard_pages(cat, cache, month, out, pct):
    """
    One figure per hazard box of the coverage diagram. One row per variable
    (main variables first): histogram | max map | hours >= p90 map | p90 hours by
    local solar hour. Returns [(group heading, hazard name, status, file)].
    """
    if D is None:
        return []
    info = {sv: (v, sid, sname, color) for v, sv, sid, sname, color in cat}
    local = None
    if month.lon is not None:
        local = np.floor((month.time.hour.values[:, None, None] + month.lon[None] / 15.0) % 24).astype(int)
    pages, group = [], ""
    for item in D.HAZARDS:
        if isinstance(item, str):
            group = item
            continue
        rows = [(sv, is_main) for _, sv, is_main in D.all_vars(item) if sv in info]
        if not rows:
            continue
        fig, axes = plt.subplots(len(rows), 4, figsize=(15, 2.55 * len(rows) + 0.9), squeeze=False,
                                 gridspec_kw=dict(width_ratios=[1, 1.25, 1.25, 1]))
        for r, (sv, is_main) in enumerate(rows):
            v, sid, _, _ = info[sv]
            color = HAZARD_COLORS.get(item["name"], FALLBACK_SOURCE[2])
            sname = DATASET.get(sid, sid)
            a = cache(v)
            units = cache.attrs(v).get("units", "")
            role = "main" if is_main else "context"
            ax_h, ax_mx, ax_p, ax_hr = axes[r]

            # 1. histogram
            vals = a[np.isfinite(a)].ravel()
            if sv not in ("sp", "orog"):
                vals = vals[vals > E.SENTINEL_LE]
            if vals.size:
                floor = vals.min()
                mostly_floor = (vals == floor).mean() > 0.5 and vals.max() > floor
                show = vals[vals > floor] if mostly_floor else vals
                lo, hi = np.percentile(show, [0.1, 99.9])
                if hi <= lo:
                    lo, hi = show.min(), show.max() + 1e-9
                ax_h.hist(np.clip(show, lo, hi), bins=40, color=color, alpha=0.85, edgecolor=BG,
                          linewidth=0.3, log=mostly_floor)
                note = (f"{100 * (vals == floor).mean():.0f}% at {floor:.3g}, rest (log)"
                        if mostly_floor else f"n = {vals.size:,}")
            else:
                ax_h.text(0.5, 0.5, "no data", ha="center", va="center", color=MUTED,
                          transform=ax_h.transAxes)
                note = "no data"
            ax_h.set_xlabel(units, fontsize=7)
            ax_h.tick_params(labelsize=6.5)
            ax_h.set_title(f"{sv}", loc="left", fontsize=9, pad=15,
                           fontweight="bold" if is_main else "normal")
            ax_h.text(0, 1.03, f"■ {role} · {sname} · {note}", transform=ax_h.transAxes,
                      fontsize=7, color=color, va="bottom")

            # 2. max map
            f = a if a.ndim == 2 else (np.nanmax(a, axis=0) if np.isfinite(a).any() else
                                        np.full(a.shape[1:], np.nan))
            lo, hi = color_range(f)
            map_ax(ax_mx, month, f, "highest value" if a.ndim == 3 else "static field",
                   vmin=lo, vmax=hi, label=units)

            # 3 + 4. percentile exceedance
            thr = percentile_threshold(a, sv, pct) if (a.ndim == 3 and sv not in NO_PCTL) else None
            if thr is None:
                for ax in (ax_p, ax_hr):
                    ax.axis("off")
                    ax.text(0.5, 0.5, "no percentile view\n(no data, constant, categorical or static)",
                            ha="center", va="center", color=MUTED, fontsize=8, transform=ax.transAxes)
                continue
            t, op, basis = thr
            with np.errstate(invalid="ignore"):
                hit = a >= t if op == ">=" else a <= t
            cnt = hit.sum(axis=0).astype(float)
            cnt[~np.isfinite(a).any(axis=0)] = np.nan
            rule = f"{op} {t:.3g} {units if units != '1' else ''}".strip()
            map_ax(ax_p, month, np.where(cnt > 0, cnt, np.nan), f"hours {rule}", vmin=0, label="hours")
            if local is not None:
                ax_hr.bar(range(24), np.bincount(local[hit], minlength=24), color=color, width=0.8)
                ax_hr.set_xticks([0, 6, 12, 18, 23])
                ax_hr.tick_params(labelsize=6.5)
                ax_hr.set_xlabel("local solar hour", fontsize=7)
                ax_hr.set_ylabel("cell-hours", fontsize=7)
                ax_hr.grid(axis="y", color=LINE, lw=0.6)
                ax_hr.set_title(f"when {rule}", loc="left", fontsize=8.5)
        name = f"hazard_{hazard_slug(item['name'])}.png"
        save(fig, out, name, f"{group} · {item['name']}  ({item['status']})\n"
             f"histogram | highest value | hours at or beyond p{pct:g} | same by local hour")
        pages.append((group, item["name"], item["status"], name))
    return pages


def fig_correlation(cat, cache, month, out):
    rows = []
    for v, sv, *_ in cat:
        a = cache(v)
        valid = a[np.isfinite(a)]
        rows.append(dict(variable=v, short=sv,
                         pct_missing=100 * (1 - valid.size / a.size) if a.size else 100,
                         constant=bool(valid.size == 0 or valid.min() == valid.max())))
    name, pairs = E.fig_correlation(pd.DataFrame(rows), month, cache, out)
    return name, pairs


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
def write_html(out, month, sections, pairs, hazard_pages=(), pct=90):
    t0, t1 = month.time.min(), month.time.max()
    parts = [f"<h1>Daily weather files: overview</h1><p class=muted>{len(month.files)} file(s) · "
             f"{t0:%Y-%m-%d} to {t1:%Y-%m-%d} · grid {len(month.y)} × {len(month.x)} cells. "
             "Variables follow the order of the hazard coverage diagram; colour = the hazard each variable belongs to (the first one if it serves several), dataset in brackets.</p>"]
    for title, note, files in sections:
        files = [f for f in files if f]
        if not files:
            continue
        parts.append(f"<section><h2>{title}</h2><p class=muted>{note}</p>"
                     + "".join(f'<img src="{f}">' for f in files) + "</section>")
    if hazard_pages:
        parts.append(f"<section><h2>By hazard</h2><p class=muted>One page per hazard box of the coverage "
                     f"diagram. Each row is one variable (bold title = main variable, others are "
                     f"context): histogram, highest value per cell, hours at or beyond p{pct:g} "
                     f"(p{100 - pct:g} and below for low-is-hazard variables), and the same hours "
                     f"by local solar hour.</p>")
        toc, body, last = [], [], None
        for group, name, status, f in hazard_pages:
            anchor = hazard_slug(name)
            if group != last:
                body.append(f"<h3>{group}</h3>")
                toc.append(f"<b>{group}:</b> ")
                last = group
            toc.append(f'<a href="#{anchor}">{name}</a> ')
            body.append(f'<h4 id="{anchor}">{name} <span class="chip '
                        f'{"ok" if status == "strong" else "warn"}">{status}</span></h4>'
                        f'<img src="{f}" alt="{name}">')
        parts.append("<p>" + " ".join(toc) + "</p>" + "".join(body) + "</section>")
    if pairs is not None and len(pairs):
        parts.append("<section><h2>Highly correlated pairs (|r| ≥ 0.9)</h2>"
                     + E.table_html(pairs, left=("sample", "var_a", "var_b")) + "</section>")
    (out / "overview.html").write_text(
        f"<!doctype html><meta charset=utf-8><title>Data overview</title><style>{E.CSS}</style>"
        f"<main>{''.join(parts)}</main>", encoding="utf-8")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
class Cache:
    def __init__(self, month):
        self.month, self.store = month, {}

    def __call__(self, v):
        if v not in self.store:
            self.store[v] = self.month.load(v)
        return self.store[v]

    def attrs(self, v):
        return self.month.attrs.get(v, {})


def run(daily_dir, out, start=None, end=None, pct=90, pattern="weather_*.nc",
        boundaries_dir="~/.cache/multi_haz/boundaries"):
    files = sorted(glob.glob(os.path.join(daily_dir, pattern)))
    if start or end:
        def day(f):
            m = re.search(r"(\d{8})", Path(f).name)
            return pd.Timestamp(m.group(1)) if m else None
        files = [f for f in files if day(f) is not None
                 and (start is None or day(f) >= pd.Timestamp(start))
                 and (end is None or day(f) <= pd.Timestamp(end))]
    if not files:
        raise SystemExit(f"No files matching {pattern} in {daily_dir}"
                         + (f" between {start} and {end}" if start or end else ""))
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    print(f"{len(files)} daily file(s)")

    month = E.Month(files)
    cache = Cache(month)
    cat = variable_catalog(month)
    print(f"{len(cat)} variables")
    n_lines = load_boundaries(boundaries_dir, month) if boundaries_dir else 0
    print(f"  {n_lines} boundary lines" if n_lines else "  maps without boundaries")

    # split by dataset so each figure stays a manageable size; SWDI only if it has data
    groups = []
    for ds_name in ("MRMS", "RTMA", "SWDI"):
        sub = [c for c in cat if DATASET.get(c[2], "other") == ds_name]
        if ds_name == "SWDI" and not any(np.isfinite(cache(c[0])).any() for c in sub):
            print("  SWDI has no data: left out of the dataset figures")
            continue
        if sub:
            groups.append((ds_name, f"_{ds_name.lower()}", sub))
    other = [c for c in cat if DATASET.get(c[2], "other") not in ("MRMS", "RTMA", "SWDI")]
    if other:
        groups.append(("Other", "_other", other))

    res = {"histograms": [], "max maps": [], "percentile maps and hours": []}
    print("  cells with data")
    res["cells with data"] = fig_cells_with_data(cat, cache, month, out)
    thr_rows = []
    for ds_name, tag, sub in groups:
        print(f"  {ds_name}: histograms, max maps, percentiles")
        res["histograms"].append(fig_histograms(sub, cache, out, tag, ds_name))
        res["max maps"].append(fig_max_maps(sub, cache, month, out, tag, ds_name))
        files, rows = fig_percentile(sub, cache, month, out, pct, tag, ds_name)
        res["percentile maps and hours"] += files
        thr_rows += rows
    pd.DataFrame(thr_rows).to_csv(out / "thresholds.csv", index=False)
    print("  hazard pages")
    res["hazard pages"] = fig_hazard_pages(cat, cache, month, out, pct)
    print("  correlation")
    res["correlation"] = fig_correlation(cat, cache, month, out)

    corr_name, pairs = res["correlation"] if res["correlation"] else (None, None)
    sections = [
        ("Grid cells with data", "Pale bar: cells with at least one valid value over the period. "
         "Dark bar: cells with at least one non-zero value (where something happened).",
         [res["cells with data"]]),
        ("Distributions", "MRMS and RTMA separately. For mostly-zero variables only values above "
         "the floor are shown, on a log count axis.", res["histograms"]),
        ("Highest value per cell", "Maximum over the period at each cell, MRMS and RTMA separately.",
         res["max maps"]),
        (f"Above the {pct:g}th percentile", "Threshold per variable in thresholds.csv. Maps count "
         "hours above it per cell; bars count cell-hours above it by local solar hour.",
         res["percentile maps and hours"]),
        ("Correlation", "Spearman correlation on a sample of cell-hours, all hours and storm hours "
         "(composite reflectivity ≥ 30 dBZ). Judge redundancy on storm hours.", [corr_name]),
    ]
    write_html(out, month, sections, pairs, res["hazard pages"], pct)
    month.close()
    print(f"Done. Open {out / 'overview.html'}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="First-pass overview of the daily weather files")
    ap.add_argument("--daily-dir")
    ap.add_argument("--out")
    ap.add_argument("--start", help="first day, YYYY-MM-DD")
    ap.add_argument("--end", help="last day, YYYY-MM-DD")
    ap.add_argument("--pct", type=float, default=90, help="percentile for the exceedance maps (default 90)")
    ap.add_argument("--pattern", default="weather_*.nc")
    ap.add_argument("--boundaries-dir", default="~/.cache/multi_haz/boundaries",
                    help="where the Natural Earth outlines are cached; '' to skip them")
    a = ap.parse_args()
    
    # a.daily_dir="/Users/marchett/Documents/Disasters/data/V2/daily"
    # a.out="/Users/marchett/Documents/Disasters/data/V2/eda"
    # a.start="2024-06-01"
    # a.end="2024-06-01"
    
    run(a.daily_dir, a.out, a.start, a.end, a.pct, a.pattern, a.boundaries_dir)
