#!/usr/bin/env python3
"""
Resume the MYRIAD production pipeline from a step-3 checkpoint (event tables + label
cubes already on disk), running only steps 4-6 (multihazard linking, event sets,
summary). Use this instead of production_pipeline_2024_2025.py when only the
linking/summary logic needs to be re-run -- avoids redoing mask construction and
event labeling (~5 minutes) plus the risk of long re-runs while iterating on the
linking algorithm.

Usage: python3 resume_linking_from_checkpoint.py
Requires: OUTPUT_DIR/checkpoints/step3_complete.json to exist (written automatically
by production_pipeline_2024_2025.py at the end of step 3).
"""

from __future__ import annotations

import json
import logging
import sys
import time as _time
from pathlib import Path
from typing import Dict

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------
ROOT = Path("/Users/ryanmc/Documents/NASA_JPL/Projects/NaturalHazards/NASA ROSES Disasters 2025-2027/data")
LOG_DIR = Path(
    "/Users/ryanmc/Documents/Complex_Risk_Science/dev/Complex-Risk-Collective/.github/Projects/"
    "NASA-disasters-grid-resilience/logs"
)
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / f"resume_linking_{pd.Timestamp.now():%Y%m%d_%H%M%S}.log"

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)


def step(msg: str) -> None:
    log.info("=" * 70)
    log.info(msg)
    log.info("=" * 70)


# ---------------------------------------------------------------------------
# Config (must match production_pipeline_2024_2025.py)
# ---------------------------------------------------------------------------
OUTPUT_ROOT = ROOT / "MYRIAD_events_output"
TIME_START = "2024-01-01"
TIME_END = "2025-12-31"
run_start = pd.Timestamp(TIME_START)
run_end = pd.Timestamp(TIME_END)
RUN_LABEL = f"{run_start:%Y%m%d}_{run_end:%Y%m%d}"
OUTPUT_DIR = OUTPUT_ROOT / "production" / f"myriad_conus_{RUN_LABEL}"
CHECKPOINT_DIR = OUTPUT_DIR / "checkpoints"
CHECKPOINT_MARKER = CHECKPOINT_DIR / "step3_complete.json"

MULTIHAZARD_LAG_HOURS = 48.0
PAIR_LAG_HOURS = {
    ("extreme_wind", "extreme_precip"): 24.0, ("extreme_precip", "extreme_wind"): 24.0,
    ("extreme_wind", "wildfire"): 12.0, ("wildfire", "extreme_wind"): 12.0,
    ("extreme_cold", "extreme_wind"): 48.0, ("extreme_wind", "extreme_cold"): 48.0,
    ("extreme_wind_era5", "extreme_wind"): 24.0, ("extreme_wind", "extreme_wind_era5"): 24.0,
}
SPACE_WEATHER_PAIR_LAG_HOURS = 24.0
EPISODE_MAX_SPAN_HOURS = 168.0


def _lag_hours_for_ordered_pair(leading_hazard: str, following_hazard: str) -> float:
    if leading_hazard == "space_weather_extreme" or following_hazard == "space_weather_extreme":
        return SPACE_WEATHER_PAIR_LAG_HOURS
    return PAIR_LAG_HOURS.get((leading_hazard, following_hazard), MULTIHAZARD_LAG_HOURS)


def _build_event_cache(tbl: pd.DataFrame, labels: np.ndarray) -> Dict[str, dict]:
    cache: Dict[str, dict] = {}
    if tbl.empty:
        return cache
    _, yy_all, xx_all = np.where(labels > 0)
    if len(yy_all) == 0:
        return cache
    lbl_all = labels[labels > 0].astype(np.int64)
    order = np.argsort(lbl_all, kind="stable")
    lbl_sorted = lbl_all[order]
    yy_sorted = yy_all[order]
    xx_sorted = xx_all[order]
    unique_labels, boundaries = np.unique(lbl_sorted, return_index=True)
    bounds = np.r_[boundaries, len(lbl_sorted)]
    cells_by_label: Dict[int, dict] = {}
    for i, enum in enumerate(unique_labels):
        y_seg = yy_sorted[bounds[i]:bounds[i + 1]]
        x_seg = xx_sorted[bounds[i]:bounds[i + 1]]
        cells_by_label[int(enum)] = {
            "cells": set(zip(y_seg.tolist(), x_seg.tolist())),
            "y_min": int(y_seg.min()), "y_max": int(y_seg.max()),
            "x_min": int(x_seg.min()), "x_max": int(x_seg.max()),
        }
    for _, row in tbl.iterrows():
        eid = str(row["event_id"])
        enum = int(eid.split("_")[-1])
        info = cells_by_label.get(enum)
        if info is None:
            continue
        cache[eid] = {
            "start": pd.Timestamp(row["start_time"]),
            "end": pd.Timestamp(row["end_time"]),
            "cells": info["cells"],
            "y_min": info["y_min"], "y_max": info["y_max"],
            "x_min": info["x_min"], "x_max": info["x_max"],
        }
    return cache


def _events_are_linked_cached(a: dict, b: dict, hazard_a: str, hazard_b: str) -> bool:
    if a["start"] <= b["end"] and b["start"] <= a["end"]:
        gap_hours = 0.0
        lag_hours = _lag_hours_for_ordered_pair(hazard_a, hazard_b)
    elif b["start"] > a["end"]:
        gap_hours = float((b["start"] - a["end"]) / pd.Timedelta(hours=1))
        lag_hours = _lag_hours_for_ordered_pair(hazard_a, hazard_b)
    else:
        gap_hours = float((a["start"] - b["end"]) / pd.Timedelta(hours=1))
        lag_hours = _lag_hours_for_ordered_pair(hazard_b, hazard_a)
    if gap_hours > lag_hours:
        return False
    if a["y_max"] < (b["y_min"] - 1) or b["y_max"] < (a["y_min"] - 1):
        return False
    if a["x_max"] < (b["x_min"] - 1) or b["x_max"] < (a["x_min"] - 1):
        return False
    a_cells, b_cells = a["cells"], b["cells"]
    if a_cells & b_cells:
        return True
    src, tgt = (a_cells, b_cells) if len(a_cells) <= len(b_cells) else (b_cells, a_cells)
    for y, x in src:
        if (y - 1, x) in tgt or (y + 1, x) in tgt or (y, x - 1) in tgt or (y, x + 1) in tgt:
            return True
    return False


if not CHECKPOINT_MARKER.exists():
    log.error(f"No checkpoint found at {CHECKPOINT_MARKER}. Run production_pipeline_2024_2025.py first.")
    sys.exit(1)

step("Resuming from checkpoint: loading event tables + label cubes from disk")
t_step = _time.perf_counter()
with open(CHECKPOINT_MARKER) as _f:
    _ckpt_meta = json.load(_f)
event_tables: Dict[str, pd.DataFrame] = {}
label_cubes: Dict[str, np.ndarray] = {}
for hz in _ckpt_meta["hazards"]:
    event_tables[hz] = pd.read_csv(
        OUTPUT_DIR / f"single_hazard_events_{hz}.csv", parse_dates=["start_time", "end_time"])
    with np.load(CHECKPOINT_DIR / f"labels_{hz}.npz") as _npz:
        label_cubes[hz] = _npz["labels"]
single_hazard_events = pd.read_csv(
    OUTPUT_DIR / "single_hazard_events_all.csv", parse_dates=["start_time", "end_time"])
log.info(f"  Checkpoint created: {_ckpt_meta['created']}")
log.info(f"  Resumed {len(event_tables)} hazards, {len(single_hazard_events)} total events")
log.info(f"Checkpoint load complete in {_time.perf_counter() - t_step:.0f}s")

# ---------------------------------------------------------------------------
# Step 4/6: multihazard linking
# ---------------------------------------------------------------------------
step("Step 4/6: Linking multihazard event pairs")
t_step = _time.perf_counter()

hazards = sorted(event_tables)
edge_rows = []
event_cache_by_hazard: Dict[str, Dict[str, dict]] = {
    hz: _build_event_cache(event_tables[hz], label_cubes[hz]) for hz in hazards
}

for i in range(len(hazards)):
    for j in range(i + 1, len(hazards)):
        h1, h2 = hazards[i], hazards[j]
        c1 = event_cache_by_hazard[h1]
        c2 = event_cache_by_hazard[h2]
        if not c1 or not c2:
            continue
        log.info(f"  Linking pairs: {h1} vs {h2} ({len(c1)} x {len(c2)})")
        for eid1, ev1 in c1.items():
            for eid2, ev2 in c2.items():
                if _events_are_linked_cached(ev1, ev2, h1, h2):
                    edge_rows.append({
                        "event_id_a": eid1, "hazard_a": h1, "event_id_b": eid2, "hazard_b": h2,
                        "lag_hours": _lag_hours_for_ordered_pair(h1, h2),
                    })

edges_df = pd.DataFrame(edge_rows)
edges_df.to_csv(OUTPUT_DIR / "multi_hazard_event_links.csv", index=False)
log.info(f"Accepted links: {len(edges_df)}")
log.info(f"Step 4/6 complete in {_time.perf_counter() - t_step:.0f}s")

# ---------------------------------------------------------------------------
# Step 5/6: multihazard event sets (bounded-episode union-find, not raw connected components)
# ---------------------------------------------------------------------------
step("Step 5/6: Building multihazard event sets (bounded-episode union-find)")
t_step = _time.perf_counter()

event_bounds = single_hazard_events.set_index("event_id")[["start_time", "end_time"]].to_dict("index")
parent = {eid: eid for eid in event_bounds}
span_start = {eid: pd.Timestamp(v["start_time"]) for eid, v in event_bounds.items()}
span_end = {eid: pd.Timestamp(v["end_time"]) for eid, v in event_bounds.items()}


def _uf_find(x):
    while parent[x] != x:
        parent[x] = parent[parent[x]]
        x = parent[x]
    return x


def _uf_try_union(a, b, max_span_hours):
    ra, rb = _uf_find(a), _uf_find(b)
    if ra == rb:
        return True
    merged_start = min(span_start[ra], span_start[rb])
    merged_end = max(span_end[ra], span_end[rb])
    if (merged_end - merged_start) / pd.Timedelta(hours=1) > max_span_hours:
        return False
    parent[rb] = ra
    span_start[ra] = merged_start
    span_end[ra] = merged_end
    return True


rejected_for_span = 0
edges_sorted = edges_df.assign(
    _sort_key=lambda d: d["event_id_a"].map(lambda e: span_start[e])
).sort_values("_sort_key") if not edges_df.empty else edges_df
for _, edge in edges_sorted.iterrows():
    if not _uf_try_union(edge["event_id_a"], edge["event_id_b"], EPISODE_MAX_SPAN_HOURS):
        rejected_for_span += 1

groups: Dict[str, list] = {}
for eid in event_bounds:
    groups.setdefault(_uf_find(eid), []).append(eid)

set_rows = []
membership_rows = []
for k, comp in enumerate(sorted(groups.values(), key=lambda c: min(span_start[e] for e in c)), start=1):
    comp_events = single_hazard_events[single_hazard_events["event_id"].isin(comp)]
    hazards_in_set = sorted(comp_events["hazard"].unique().tolist())
    start = comp_events["start_time"].min()
    end = comp_events["end_time"].max()
    set_id = f"mh_set_{k:06d}"
    set_rows.append({
        "mh_set_id": set_id,
        "n_single_events": int(len(comp_events)),
        "n_hazard_types": int(len(hazards_in_set)),
        "hazard_types": "|".join(hazards_in_set),
        "start_time": start,
        "end_time": end,
        "duration_days": float((pd.Timestamp(end) - pd.Timestamp(start)) / pd.Timedelta(days=1) + 1.0),
    })
    for eid in comp:
        membership_rows.append({"mh_set_id": set_id, "event_id": eid})

mh_sets_df = pd.DataFrame(set_rows).sort_values(["start_time", "mh_set_id"])
mh_members_df = pd.DataFrame(membership_rows)
mh_sets_df.to_csv(OUTPUT_DIR / "multi_hazard_event_sets.csv", index=False)
mh_members_df.to_csv(OUTPUT_DIR / "multi_hazard_event_membership.csv", index=False)

log.info(f"Identified multi-hazard event sets: {len(mh_sets_df)}")
log.info(f"  Links rejected for exceeding {EPISODE_MAX_SPAN_HOURS:.0f}h episode-span cap: {rejected_for_span}")
log.info(f"  Largest episode: {mh_sets_df['n_single_events'].max() if len(mh_sets_df) else 0} events")
log.info(f"Step 5/6 complete in {_time.perf_counter() - t_step:.0f}s")

# ---------------------------------------------------------------------------
# Step 6/6: final summary
# ---------------------------------------------------------------------------
step("Step 6/6: Final summary")
summary = (
    single_hazard_events.groupby("hazard", observed=True)
    .agg(n_events=("event_id", "size"), first_start=("start_time", "min"), last_end=("end_time", "max"))
    .reset_index()
)
log.info("\n" + summary.to_string(index=False))

step("RESUME PIPELINE COMPLETE")
log.info(f"  Total single-hazard events: {len(single_hazard_events)}")
log.info(f"  Multihazard event sets: {len(mh_sets_df)}")
log.info(f"  Output directory: {OUTPUT_DIR}")
log.info(f"  Log file: {LOG_FILE}")
