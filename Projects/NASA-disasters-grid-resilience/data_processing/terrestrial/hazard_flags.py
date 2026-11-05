#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Step 1 of the baseline multi-hazard analysis: hazard flags.

Reads daily gridded files (weather_<region>_YYYYMMDD.nc, see DATA_DICTIONARY.md)
and writes, per day, hourly flags on the same 50 km grid:

    flag values:  1 = hazard present, 0 = absent (observed), -1 = no data

Two kinds of flags:
  * hazards  - impact hazards; counted in `hazard_count`
  * context  - storm-intensity indicators; NOT counted (they drive several
               hazards at once and would double count), used later to
               condition, e.g. P(hail | convective storm)

All variables and thresholds live in hazards.yml (next to this script, or
pass --config); edit them there. A copy is saved with the output as
hazards_used.yml. Temperature thresholds are in degC.
"""

import argparse
import glob
import os
import re
import shutil

import numpy as np
import pandas as pd
import xarray as xr
import yaml

# -------------------------------------------------------------------------
# CONFIG (hazards.yml)
# -------------------------------------------------------------------------
DEFAULT_CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hazards.yml")

OPS = {">=": np.greater_equal, ">": np.greater,
       "<=": np.less_equal, "<": np.less}


def load_config(path=DEFAULT_CONFIG):
    """Read and check hazards.yml. Returns the config dict."""
    with open(path) as f:
        cfg = yaml.safe_load(f)
    cfg.setdefault("min_valid", 0.5)
    cfg.setdefault("validity", {})
    cfg["context"] = cfg.get("context") or {}
    if not cfg.get("hazards"):
        raise ValueError(f"{path}: no `hazards` defined")
    names = set()
    for group in ("hazards", "context"):
        for name, h in cfg[group].items():
            if name in names:
                raise ValueError(f"{path}: {name} defined twice")
            names.add(name)
            if not h or not h.get("conditions"):
                raise ValueError(f"{path}: {name} has no conditions")
            for c in h["conditions"]:
                miss = {"var", "op", "threshold"} - set(c)
                if miss:
                    raise ValueError(f"{path}: {name} condition missing {sorted(miss)}")
                if c["op"] not in OPS:
                    raise ValueError(f"{path}: {name} op {c['op']!r}, use one of {list(OPS)}")
                if c.get("validity") is not None and c["validity"] not in cfg["validity"]:
                    raise ValueError(f"{path}: {name} validity {c['validity']!r} not under `validity`")
    return cfg


# Temperature thresholds in hazards.yml are in degC (pipeline.py FIX 12). Older
# daily files stored them in K; those are converted on read so thresholds still apply.
TEMPERATURE_VARS = ("t2m", "t2m_min", "t2m_max", "d2m", "d2m_max", "heat_index_max")


def ensure_celsius(ds):
    """Convert temperature fields still stored in K (older files) to degC."""
    for v in TEMPERATURE_VARS:
        if v in ds and ds[v].attrs.get("units", "K") == "K":
            attrs = dict(ds[v].attrs)
            ds[v] = ds[v] - 273.15
            ds[v].attrs = {**attrs, "units": "degC"}
    return ds


# -------------------------------------------------------------------------
# FLAGGING
# -------------------------------------------------------------------------
def _condition(ds, cond, cfg):
    """Return (hit, valid) boolean arrays for one condition, or None if var missing."""
    var, op, thr = cond["var"], cond["op"], cond["threshold"]
    if var not in ds:
        return None
    x = ds[var]
    min_valid = cfg["min_valid"]
    rule = cfg["validity"].get(cond.get("validity")) or {}
    if "reference" in rule:
        # validity from a reference variable; NaN in x = not met
        ref = rule["reference"]
        if ref not in ds:
            return None
        valid = ds[ref].notnull()
        if f"{ref}_valid_frac" in ds:
            valid = valid & (ds[f"{ref}_valid_frac"] >= min_valid)
        hit = OPS[op](x, thr).fillna(False) & valid
        return hit, valid
    valid = x.notnull()
    if "valid_frac" in rule:
        vf_name = rule["valid_frac"].format(var=var)
        if vf_name in ds:
            valid = valid & (ds[vf_name] >= min_valid)
    hit = OPS[op](x, thr) & valid
    return hit, valid


def make_flag(ds, conditions, cfg):
    """
    OR-combine conditions. 1 if any condition hits; 0 if none hits and at
    least one is valid; -1 if none is valid. None if no variable is present.
    """
    hits, valids = [], []
    for cond in conditions:
        res = _condition(ds, cond, cfg)
        if res is not None:
            hits.append(res[0])
            valids.append(res[1])
    if not hits:
        return None
    any_hit = hits[0]
    any_valid = valids[0]
    for h, v in zip(hits[1:], valids[1:]):
        any_hit = any_hit | h
        any_valid = any_valid | v
    flag = xr.where(any_hit, 1, xr.where(any_valid, 0, -1)).astype("int8")
    return flag


def flag_dataset(ds, cfg):
    out = xr.Dataset(coords={k: ds.coords[k] for k in ("time", "y", "x")
                             if k in ds.coords})
    missing = []
    for group, table in (("hazard", cfg["hazards"]), ("context", cfg["context"])):
        for name, h in table.items():
            conds = h["conditions"]
            f = make_flag(ds, conds, cfg)
            if f is None:
                missing.append(name)
                continue
            # static variables (e.g. no time dim) are not expected here
            f.attrs = {"description": h.get("description", ""), "kind": group,
                       "flag_values": "1 present, 0 absent, -1 no data",
                       "conditions": " OR ".join(f"{c['var']} {c['op']} {c['threshold']}"
                                                 for c in conds)}
            out[name] = f

    hz = [n for n in cfg["hazards"] if n in out]
    if hz:
        stack = xr.concat([out[n] for n in hz], dim="hazard")
        out["hazard_count"] = (stack == 1).sum("hazard").astype("int8")
        out["hazard_count"].attrs = {
            "description": "number of impact hazards present (context flags excluded)",
            "hazards": ", ".join(hz)}
        out["n_hazards_valid"] = (stack >= 0).sum("hazard").astype("int8")
        out["n_hazards_valid"].attrs = {
            "description": "number of hazards with valid data in this cell-hour"}

    for c in ("lat", "lon"):
        if c in ds:
            out[c] = ds[c]
    out.attrs = {"min_valid_frac": cfg["min_valid"],
                 "config": cfg.get("_path", ""),
                 "source": ds.attrs.get("date", ""),
                 "region": ds.attrs.get("region", "")}
    return out, missing


# -------------------------------------------------------------------------
# DRIVER
# -------------------------------------------------------------------------
def summarize(flags, date_tag):
    rows = []
    for name in flags.data_vars:
        if name in ("hazard_count", "n_hazards_valid", "lat", "lon"):
            continue
        f = flags[name]
        rows.append({
            "date": date_tag,
            "flag": name,
            "kind": f.attrs.get("kind", ""),
            "cell_hours_present": int((f == 1).sum()),
            "cell_hours_valid": int((f >= 0).sum()),
            "cells_any_hour": int(((f == 1).any("time")).sum()) if "time" in f.dims else int((f == 1).sum()),
        })
    return rows


def run(daily_dir, out_dir, config=DEFAULT_CONFIG, pattern="weather_*_*.nc", overwrite=False):
    os.makedirs(out_dir, exist_ok=True)
    cfg = load_config(config)
    cfg["_path"] = os.path.abspath(config)
    shutil.copy(config, os.path.join(out_dir, "hazards_used.yml"))   # record of the definitions
    files = sorted(glob.glob(os.path.join(daily_dir, pattern)))
    if not files:
        raise FileNotFoundError(f"No files matching {pattern} in {daily_dir}")

    rows, warned = [], set()
    for fp in files:
        m = re.search(r"_(\d{8})\.nc$", fp)
        date_tag = m.group(1) if m else os.path.basename(fp)
        out_fp = os.path.join(out_dir, f"hazard_flags_{date_tag}.nc")

        with xr.open_dataset(fp) as ds:
            flags, missing = flag_dataset(ensure_celsius(ds.load()), cfg)

        for name in missing:
            if name not in warned:
                print(f"  ! {name}: none of its variables found, skipped")
                warned.add(name)

        if overwrite or not os.path.exists(out_fp):
            enc = {v: {"zlib": True, "complevel": 4} for v in flags.data_vars}
            flags.to_netcdf(out_fp, encoding=enc)
        rows += summarize(flags, date_tag)
        print(f"✓ {date_tag}")

    summary = pd.DataFrame(rows)
    summary.to_csv(os.path.join(out_dir, "hazard_flags_summary.csv"), index=False)

    # quick overview: totals per flag over all days
    tot = (summary.groupby(["kind", "flag"])
                  [["cell_hours_present", "cell_hours_valid"]].sum())
    tot["pct_present"] = 100 * tot.cell_hours_present / tot.cell_hours_valid.replace(0, np.nan)
    print("\nTotals over all days:")
    print(tot.round(3).to_string())
    return summary


if __name__ == "__main__":
    PATH = "/Volumes/MLIA_active_data/gridresilience/data/terrestrial/V2"
    p = argparse.ArgumentParser()
    p.add_argument("--daily_dir", default=f"{PATH}/daily")
    p.add_argument("--out_dir", default=f"{PATH}/hazard_flags")
    p.add_argument("--config", default=DEFAULT_CONFIG, help="hazard definitions (YAML)")
    p.add_argument("--overwrite", action="store_true")
    a = p.parse_args()

    run(a.daily_dir, a.out_dir, config=a.config, overwrite=a.overwrite)
