"""Pack an event's hourly hazard grids into the files the explorer app reads.

    python build_data.py marapr_2023 [gannon_2024 ...]

Writes app/data/<event>/:
    manifest.json      grid, time axis, variables (units, defaults, stats), missing inputs
    <key>.f32          float32 (time, y, x), y south→north, NaN = no data
    cells_lonlat.f32   lon/lat of cell corners, (ny+1, nx+1, 2), for exports
    in_conus.u8        1 where the cell centre is within 25 km of CONUS
and app/data/states.json (CONUS state outlines in EPSG:5070 km) and app/data/events.json.

Loaders follow notebooks/multihazard_arcgis_time_layers_v1.ipynb so the two stay comparable.
"""

import json
import os
import sys
import zipfile
from pathlib import Path

os.environ["PROJ_NETWORK"] = "OFF"  # see the notebook: failed datum-grid downloads return inf coordinates

import geopandas as gpd
import numpy as np
import pandas as pd
import requests
import shapely
import xarray as xr
from pyproj import Transformer
from scipy.spatial import cKDTree

from events import DATA_CACHE, DATA_DIR, EVENTS, STATES_URL

HERE = Path(__file__).resolve().parent
OUT_ROOT = HERE / "app" / "data"
CRS = "EPSG:5070"

GRID = xr.open_dataset(DATA_DIR / "analysis_grid_CONUS_50km.nc")
GX, GY = GRID["x"].values.astype(float), GRID["y"].values.astype(float)
DX, DY = float(GX[1] - GX[0]), float(GY[1] - GY[0])
XX, YY = np.meshgrid(GX, GY)
TO_ALBERS = Transformer.from_crs("EPSG:4326", CRS, always_xy=True)
TO_LONLAT = Transformer.from_crs(CRS, "EPSG:4326", always_xy=True)


# ---------------------------------------------------------------- helpers

def event_hours(ev):
    return pd.date_range(pd.Timestamp(ev["start"]).floor("h"), pd.Timestamp(ev["end"]).floor("h"), freq="h")


def nearest_point_index(lon, lat, max_km):
    """For each grid cell, the index of the nearest source point, or -1 when none is within max_km."""
    px, py = TO_ALBERS.transform(np.asarray(lon, float), np.asarray(lat, float))
    dist, idx = cKDTree(np.column_stack([px, py])).query(np.column_stack([XX.ravel(), YY.ravel()]), k=1)
    return np.where(dist <= max_km * 1000, idx, -1).reshape(XX.shape)


def spread_points(values_tp, idx_grid):
    """(time, point) values → (time, y, x) using a nearest-point index; NaN where idx == -1."""
    out = np.full((values_tp.shape[0], *idx_grid.shape), np.nan, dtype="float32")
    ok = idx_grid >= 0
    out[:, ok] = values_tp[:, idx_grid[ok]]
    return out


def lonlat_to_cell(lon, lat):
    x, y = TO_ALBERS.transform(np.asarray(lon, float), np.asarray(lat, float))
    ix = np.rint((x - GX[0]) / DX).astype(int)
    iy = np.rint((y - GY[0]) / DY).astype(int)
    ok = (ix >= 0) & (ix < len(GX)) & (iy >= 0) & (iy < len(GY))
    return np.clip(ix, 0, len(GX) - 1), np.clip(iy, 0, len(GY) - 1), ok


# ---------------------------------------------------------------- loaders → (time, y, x) float32

_daily_cache = {}


def _daily_dataset(cfg, hours):
    """All daily files of one product for the event, on the analysis grid and hourly axis (cached per build)."""
    key = (str(cfg["dir"]), cfg["prefix"], hours[0], hours[-1])
    if key in _daily_cache:
        return _daily_cache[key]
    base = Path(cfg["dir"])
    base = base / "daily" if (base / "daily").exists() else base
    days = pd.date_range(hours[0].normalize(), hours[-1].normalize(), freq="D")
    files = [base / f"{cfg['prefix']}_{d:%Y%m%d}.nc" for d in days]
    found = [f for f in files if f.exists()]
    if not found:
        raise FileNotFoundError(f"no {cfg['prefix']}_YYYYMMDD.nc files in {base}")
    ds = xr.concat([xr.open_dataset(f) for f in found], dim="time", data_vars="minimal", coords="minimal", compat="override")
    ds = ds.assign_coords(time=pd.to_datetime(ds["time"].values).floor("h"))
    ds = ds.isel(time=~ds.get_index("time").duplicated())
    same = ds.sizes["x"] == len(GX) and ds.sizes["y"] == len(GY) and np.allclose(ds["x"], GX) and np.allclose(ds["y"], GY)
    ds = ds.assign_coords(x=GX, y=GY) if same else ds.interp(x=GX, y=GY, method="nearest")
    ds = ds.reindex(time=hours)
    note = f"{len(found)}/{len(files)} daily files"
    _daily_cache[key] = (ds, note)
    return ds, note


def load_daily_grid(cfg, hours):
    ds, note = _daily_dataset(cfg, hours)
    if cfg["var"] not in ds:
        raise KeyError(f"{cfg['var']} not in {cfg['prefix']} files")
    a = ds[cfg["var"]].transpose("time", "y", "x").values.astype("float32")
    a[a <= -900] = np.nan  # fill values in the team's files
    if cfg.get("negative_is_zero"):
        a[a < 0] = 0       # MRMS uses -1 for "nothing detected"
    if cfg.get("offset"):
        a += cfg["offset"]
    return a, f"{cfg['var']} from {cfg['prefix']} ({note})"


def load_site_nc(cfg, hours):
    ds = xr.open_dataset(cfg["path"])
    da = ds[cfg["var"]].sel(time=slice(hours[0], hours[-1] + pd.Timedelta("59min"))).resample(time="1h").max()
    hourly = pd.DataFrame(da.transpose("time", "site").values, index=pd.DatetimeIndex(da["time"].values)).reindex(hours)
    idx = nearest_point_index(ds["longitude"].values, ds["latitude"].values, cfg["match_km"])
    return spread_points(hourly.to_numpy(np.float32), idx), \
        f"{cfg['var']} hourly max per site ({ds.sizes['site']} sites), nearest site within {cfg['match_km']} km"


def load_noaa_geoe(cfg, hours):
    files = sorted(Path(cfg["dir"]).glob("*-empirical-EMTF-*.nc"))
    if not files:
        raise FileNotFoundError(f"no NOAA geoelectric files in {cfg['dir']} (run the notebook once to download them)")
    parts, lat, lon = [], None, None
    for fp in files:
        with xr.open_dataset(fp, decode_times=False) as ds:
            ref = pd.Timestamp(str(ds["time"].attrs.get("REFTIME", fp.name[:8])))
            secs = ds["time"].values
            ok = np.isfinite(secs) & (secs >= 0) & (secs < 2 * 86400)  # some minutes carry the netCDF fill value
            t = ref + pd.to_timedelta(secs[ok], unit="s")
            ex, ey = ds["Ex"].values[ok].astype("float32"), ds["Ey"].values[ok].astype("float32")
            for a in (ex, ey):
                a[(a <= -900) | (np.abs(a) > 1e6)] = np.nan
            parts.append(pd.DataFrame(np.hypot(ex, ey), index=pd.DatetimeIndex(t).floor("h")).groupby(level=0).max())
            if lat is None:
                lat = ds["latitude"].values.astype(float)
                lon = ds["longitude"].values.astype(float)
                lon = np.where(lon > 180, lon - 360, lon)
    hourly = pd.concat(parts).groupby(level=0).max().reindex(hours)
    idx = nearest_point_index(lon, lat, cfg["match_km"])
    return spread_points(hourly.to_numpy(np.float32), idx), \
        f"|E| = hypot(Ex, Ey), hourly max of 1-min values, {len(files)} daily files"


def load_swdi_points(cfg, hours):
    path = Path(cfg["path"])
    if not path.exists():
        raise FileNotFoundError(f"{path} (run the notebook once to download it)")
    df = pd.read_csv(path)
    out = np.zeros((len(hours), len(GY), len(GX)), "float32")  # no detection = 0, not unknown
    if len(df):
        t = pd.to_datetime(df["ZTIME"], utc=True).dt.tz_convert(None).dt.floor("h")
        ti = hours.get_indexer(t)
        ix, iy, ok = lonlat_to_cell(df["LON"], df["LAT"])
        ok &= ti >= 0
        if cfg["stat"] == "count":
            np.add.at(out, (ti[ok], iy[ok], ix[ok]), 1)
        else:
            v = pd.to_numeric(df[cfg["field"]], errors="coerce").to_numpy("float32")
            np.fmax.at(out, (ti[ok], iy[ok], ix[ok]), v[ok])
    stat = "count" if cfg["stat"] == "count" else f"max {cfg['field']}"
    return out, f"{len(df):,} SWDI records ({path.name}), hourly {stat} per cell"


LOADERS = {"daily_grid": load_daily_grid, "site_nc": load_site_nc, "noaa_geoe": load_noaa_geoe, "swdi_points": load_swdi_points}


# ---------------------------------------------------------------- static layers

def build_states():
    """CONUS state outlines as polylines in EPSG:5070 kilometres, plus a CONUS mask on the grid."""
    shp_zip = DATA_CACHE / Path(STATES_URL).name
    if not shp_zip.exists():
        print(f"downloading state boundaries → {shp_zip}")
        r = requests.get(STATES_URL, timeout=120)
        r.raise_for_status()
        shp_zip.write_bytes(r.content)
    states = gpd.read_file(f"zip://{shp_zip}")
    states = states[~states["STUSPS"].isin(["AK", "HI", "PR"])].to_crs(CRS)
    lines = []
    for geom in states.geometry.simplify(3000):
        for poly in getattr(geom, "geoms", [geom]):
            ring = np.asarray(poly.exterior.coords) / 1000
            lines.append(np.round(ring, 1).tolist())
    conus = states.union_all().buffer(25_000)
    in_conus = shapely.contains_xy(conus, XX, YY).astype("uint8")
    return lines, in_conus


def cell_corners_lonlat():
    cx = np.concatenate([GX - DX / 2, [GX[-1] + DX / 2]])
    cy = np.concatenate([GY - DY / 2, [GY[-1] + DY / 2]])
    CX, CY = np.meshgrid(cx, cy)
    lon, lat = TO_LONLAT.transform(CX, CY)
    return np.stack([lon, lat], axis=-1).astype("float32")


# ---------------------------------------------------------------- build

def stats_for(a):
    v = a[np.isfinite(a)]
    if not v.size:
        return {"valid_frac": 0.0}
    q = np.percentile(v, [0, 1, 50, 99, 99.9, 100])
    pos = v[v > 0]
    return {
        "valid_frac": round(float(v.size / a.size), 4),
        "min": float(q[0]), "p01": float(q[1]), "p50": float(q[2]), "p99": float(q[3]), "p999": float(q[4]), "max": float(q[5]),
        "pos_frac": round(float(pos.size / v.size), 4),
        "pos_p99": float(np.percentile(pos, 99)) if pos.size else 0.0,
    }


def default_threshold(spec, a):
    thr = spec["threshold"]
    if isinstance(thr, dict):
        v = a[np.isfinite(a) & (a > 0)]
        return float(np.quantile(v, thr["quantile"])) if v.size else 0.0
    return float(thr)


def build_event(event_id):
    ev = EVENTS[event_id]
    hours = event_hours(ev)
    out_dir = OUT_ROOT / event_id
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"\n{ev['title']}: {hours[0]} → {hours[-1]} ({len(hours)} h)")

    variables, missing, slots = [], [], {}
    for spec in ev["variables"]:
        src = spec["source"]
        try:
            a, note = LOADERS[src["kind"]](src, hours)
        except Exception as exc:  # missing input on this machine: record it, keep going
            print(f"  – {spec['key']:14s} not available: {type(exc).__name__}: {exc}")
            missing.append({"key": spec["key"], "label": spec["label"], "reason": f"{type(exc).__name__}: {exc}"})
            continue
        if spec["color_slot"] in slots:
            raise ValueError(f"{spec['key']} and {slots[spec['color_slot']]} share colour slot {spec['color_slot']}; "
                             "give one of them a free slot in events.py (8 slots, one hazard each)")
        slots[spec["color_slot"]] = spec["key"]
        a = np.ascontiguousarray(a, dtype="<f4")
        a.tofile(out_dir / f"{spec['key']}.f32")
        st = stats_for(a)
        variables.append({
            "key": spec["key"], "label": spec["label"], "units": spec["units"], "family": spec["family"],
            "direction": spec["direction"], "threshold": round(default_threshold(spec, a), 4),
            "color_slot": spec["color_slot"], "ramp": spec["ramp"], "file": f"{spec['key']}.f32",
            "source": note, "stats": st,
        })
        print(f"  ✓ {spec['key']:14s} valid {st['valid_frac']:.0%}  max {st.get('max', float('nan')):.4g} {spec['units']}  | {note}")

    cell_corners_lonlat().astype("<f4").tofile(out_dir / "cells_lonlat.f32")
    _, in_conus = STATES
    in_conus.tofile(out_dir / "in_conus.u8")

    manifest = {
        "event_id": event_id, "title": ev["title"],
        "start": f"{hours[0]:%Y-%m-%dT%H:%M:%SZ}", "n_hours": len(hours), "step_hours": 1,
        "grid": {"nx": len(GX), "ny": len(GY), "x0_km": GX[0] / 1000, "y0_km": GY[0] / 1000,
                 "dx_km": DX / 1000, "dy_km": DY / 1000, "crs": CRS},
        "variables": variables, "missing": missing,
        "built": pd.Timestamp.now(tz="UTC").strftime("%Y-%m-%dT%H:%MZ"),
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=1))
    return {"event_id": event_id, "title": ev["title"], "start": manifest["start"], "n_hours": len(hours),
            "n_variables": len(variables)}


if __name__ == "__main__":
    targets = sys.argv[1:] or list(EVENTS)
    unknown = [t for t in targets if t not in EVENTS]
    if unknown:
        sys.exit(f"unknown event(s) {unknown}; choose from {list(EVENTS)}")
    OUT_ROOT.mkdir(parents=True, exist_ok=True)
    lines, in_conus = build_states()
    STATES = (lines, in_conus)
    (OUT_ROOT / "states.json").write_text(json.dumps({"crs": CRS, "units": "km", "lines": lines}))

    index_path = OUT_ROOT / "events.json"
    index = {e["event_id"]: e for e in (json.loads(index_path.read_text()) if index_path.exists() else [])}
    for t in targets:
        index[t] = build_event(t)
    index_path.write_text(json.dumps([index[k] for k in EVENTS if k in index], indent=1))
    print(f"\nWrote {OUT_ROOT}. Start the app with:  python -m http.server 8765 -d {HERE / 'app'}")
