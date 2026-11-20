"""County-local GeoE exposure and fully observed Eagle-I quiet windows.

No downloads or cache rebuilds. All timestamps are interpreted as UTC. Interval
ends are inclusive; GeoE durations integrate between observed samples only.
Area weights describe county land overlap, not customer or network exposure.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd
import xarray as xr

VERSION = "county_local_v2"
QUIET_COLUMNS = [
    "impact_episode_id", "county_fips", "quiet_start_time", "quiet_end_time",
    "match_month", "match_dayofweek", "match_hour", "match_duration_hours",
    "quiet_window_id", "quiet_validation_version",
]
GEOE_DESCRIPTOR_COLUMNS = [
    "e_mag_max_mV_km", "e_mag_p95_mV_km", "e_mag_mean_mV_km",
    "d_e_dt_max_mV_km_per_min", "vector_change_max_mV_km_per_min",
    "duration_hours_above_threshold", "max_consecutive_samples_above_threshold",
    "integrated_exposure_mV_km_hours", "max_active_area_fraction",
    "mean_active_area_fraction",
]


def _fips(series):
    return series.astype("string").str.replace(r"\.0$", "", regex=True).str.zfill(5)


def _utc(values):
    return pd.to_datetime(values, utc=True).tz_convert(None)


def construct_time_matched_quiet_periods(
    impact_episodes, county_panel, *, thresholds, n_matches=20,
    exclusion_buffer=pd.Timedelta("2D"), random_seed=42,
    cadence=pd.Timedelta("15min"), exclusion_episodes=None,
):
    """Match county/month/weekday/hour/minute, validating every inclusive slot.

    Reject missing, suppressed, nonfinite or above-threshold readings in either
    outage channel. Exclude whole-window overlap with buffered episodes in the
    same county. When sampling target episodes, pass ALL episodes separately as
    exclusion_episodes. This is time matching, not weather/confounder adjustment.
    """
    if impact_episodes.empty or county_panel.empty:
        return pd.DataFrame(columns=QUIET_COLUMNS)
    cadence = pd.Timedelta(cadence)
    exclusion_buffer = pd.Timedelta(exclusion_buffer)
    if cadence <= pd.Timedelta(0) or exclusion_buffer < pd.Timedelta(0) or n_matches < 1:
        raise ValueError("Cadence/match count must be positive; buffer nonnegative.")
    targets = impact_episodes.copy()
    targets["county_fips"] = _fips(targets["county_fips"])
    exclusions = (impact_episodes if exclusion_episodes is None else exclusion_episodes).copy()
    exclusions["county_fips"] = _fips(exclusions["county_fips"])
    limits = thresholds.copy()
    limits["county_fips"] = _fips(limits["county_fips"])
    if limits["county_fips"].duplicated().any():
        raise ValueError("Duplicate county thresholds.")
    limits = limits.set_index("county_fips")
    target_counties = set(targets["county_fips"])
    panel = county_panel.loc[_fips(county_panel["county_fips"]).isin(target_counties)].copy()
    panel["county_fips"] = _fips(panel["county_fips"])
    # Group one county at a time; prefix sums validate candidates without
    # repeatedly materializing a national 15-minute reindex.
    rng = np.random.default_rng(random_seed)
    records = []
    for fips, county in panel.groupby("county_fips", sort=True):
        episodes = targets.loc[targets["county_fips"].eq(fips)]
        if episodes.empty or fips not in limits.index:
            continue
        county = county.copy()
        county["time"] = _utc(county["time"].values)
        county = county.sort_values("time")
        times = pd.DatetimeIndex(county["time"])
        if times.hasnans or times.has_duplicates:
            raise ValueError(f"Missing/duplicate panel timestamps for {fips}.")
        values = county[["customers_out", "percent_out"]].apply(pd.to_numeric, errors="coerce").to_numpy(float)
        bounds = pd.to_numeric(limits.loc[fips, ["customers_out_threshold", "percent_out_threshold"]], errors="coerce").to_numpy(float)
        if not np.isfinite(bounds).all():
            continue
        valid = np.isfinite(values).all(axis=1) & (values >= 0).all(axis=1) & (values < bounds).all(axis=1)
        if "is_suppressed_override" in county:
            # Unknown flags are not evidence of a trustworthy quiet observation.
            valid &= county["is_suppressed_override"].eq(False).to_numpy()
        bad_prefix = np.r_[0, np.cumsum(~valid)]
        gap_prefix = np.r_[0, np.cumsum(np.diff(times.asi8) != cadence.value)]
        blocked = exclusions.loc[exclusions["county_fips"].eq(fips)]
        blocked_start = _utc(blocked["start_time"].values) - exclusion_buffer
        blocked_end = _utc(blocked["end_time"].values) + exclusion_buffer
        for episode in episodes.itertuples(index=False):
            start, end = _utc([episode.start_time, episode.end_time])
            duration = end - start
            if duration < pd.Timedelta(0) or duration.value % cadence.value:
                raise ValueError("Episode span must be nonnegative and cadence-aligned.")
            pos = np.flatnonzero(
                (times.month == start.month) & (times.dayofweek == start.dayofweek)
                & (times.hour == start.hour) & (times.minute == start.minute)
            )
            candidates = times[pos]
            ends = candidates + duration
            allowed = np.ones(len(pos), dtype=bool)
            for left, right in zip(blocked_start, blocked_end):
                allowed &= (ends < left) | (candidates > right)
            hi = times.searchsorted(ends, side="right")
            expected = int(duration / cadence) + 1
            allowed &= hi - pos == expected
            allowed &= bad_prefix[hi] - bad_prefix[pos] == 0
            allowed &= gap_prefix[np.maximum(hi - 1, pos)] - gap_prefix[pos] == 0
            choices = candidates[allowed]
            if not len(choices):
                continue
            selected = rng.choice(choices.values, size=min(n_matches, len(choices)), replace=False)
            for quiet_start in pd.DatetimeIndex(selected):
                quiet_end = quiet_start + duration
                key = f"{episode.impact_episode_id}|{fips}|{quiet_start.isoformat()}|{quiet_end.isoformat()}"
                records.append({
                    "impact_episode_id": episode.impact_episode_id, "county_fips": fips,
                    "quiet_start_time": quiet_start, "quiet_end_time": quiet_end,
                    "match_month": start.month, "match_dayofweek": start.dayofweek,
                    "match_hour": start.hour,
                    "match_duration_hours": float((duration + cadence) / pd.Timedelta("1h")),
                    "quiet_window_id": hashlib.sha256(key.encode()).hexdigest()[:24],
                    "quiet_validation_version": VERSION,
                })
    return pd.DataFrame(records, columns=QUIET_COLUMNS).sort_values(
        ["impact_episode_id", "quiet_start_time"]
    ).reset_index(drop=True)


def summarize_geoe_window(
    geoe, start_time, end_time, candidate_threshold, *, county_fips,
    county_mapping, cadence=pd.Timedelta("1min"), min_support_fraction=0.9,
):
    """Summarize mapped cells only; inadequate support stays explicit, not zero.

    Peak/p95 use finite cell-time magnitudes; mean/integral/area fractions use
    intersection-area weights. dE/dt is fixed-cell magnitude change, whereas
    vector_change is simultaneous Ex/Ey change (not the derivative of a moving
    spatial max). Neither is assumed to be a more direct GIC driver than E.
    """
    if not 0 < min_support_fraction <= 1:
        raise ValueError("Support fraction must be in (0, 1].")
    cadence = pd.Timedelta(cadence)
    if cadence <= pd.Timedelta(0) or not np.isfinite(candidate_threshold) or candidate_threshold < 0:
        raise ValueError("Positive cadence and finite nonnegative threshold required.")
    result = {name: np.nan for name in GEOE_DESCRIPTOR_COLUMNS}
    result.update(county_fips=str(county_fips).zfill(5), descriptor_version=VERSION,
                  exposure_status="unmapped", n_mapped_cells=0,
                  temporal_support_fraction=0.0, min_finite_area_fraction=0.0,
                  county_grid_coverage_fraction=np.nan)
    cells = county_mapping.loc[_fips(county_mapping["county_fips"]).eq(result["county_fips"])].copy()
    if cells.empty:
        return result
    if cells[["grid_y", "grid_x"]].duplicated().any():
        raise ValueError("Duplicate county-grid cells.")
    # Reject nearest-point and degenerate-area fallbacks rather than silently
    # treating them as complete county coverage.
    areas = cells["intersection_area_km2"].to_numpy(float)
    county_areas = cells["county_area_km2"].to_numpy(float)
    if not np.isfinite(areas).all() or not np.isfinite(county_areas).all() or np.any(county_areas <= 0) or not np.allclose(county_areas, county_areas[0]):
        result["exposure_status"] = "unsupported_mapping"
        return result
    cells = cells.loc[areas > 0]
    areas = areas[areas > 0]
    coverage = areas.sum() / county_areas[0]
    result.update(n_mapped_cells=len(cells), county_grid_coverage_fraction=float(coverage))
    if coverage < min_support_fraction or coverage > 1 + 1e-6:
        result["exposure_status"] = "insufficient_county_grid_coverage"
        return result
    weights = areas / areas.sum()
    start, end = _utc([start_time, end_time])
    if end < start:
        raise ValueError("Window end precedes start.")
    indices = {name: xr.DataArray(cells[column].to_numpy(int), dims="cell")
               for name, column in [("y", "grid_y"), ("x", "grid_x")]}
    local = geoe.sel(time=slice(start, end)).isel(**indices)
    field = local["E_mag"] if isinstance(local, xr.Dataset) else local
    values = np.asarray(field.transpose("time", "cell").values, dtype=float)
    times = pd.DatetimeIndex(pd.to_datetime(field.time.values))
    if times.has_duplicates or not times.is_monotonic_increasing:
        raise ValueError("GeoE timestamps must be sorted and unique.")
    expected = int((end - start) / cadence) + 1
    result["temporal_support_fraction"] = min(1.0, len(times) / expected)
    # NOAA samples may be offset by seconds from Eagle-I timestamps. Require
    # edge coverage within one cadence and no missing internal intervals.
    if not len(times) or times[0] - start >= cadence or end - times[-1] >= cadence or (len(times) > 1 and not np.all(np.diff(times.asi8) == cadence.value)):
        result["exposure_status"] = "incomplete_time_coverage"
        return result
    finite = np.isfinite(values)
    support = finite @ weights
    result["min_finite_area_fraction"] = float(support.min())
    if np.any(support < min_support_fraction):
        result["exposure_status"] = "insufficient_finite_support"
        return result
    mean_series = np.where(finite, values, 0) @ weights / support
    active = finite & (values >= candidate_threshold)
    active_fraction = active @ weights / support
    excess = np.where(finite, np.maximum(values - candidate_threshold, 0), 0) @ weights / support
    dt_hours = np.diff(times.asi8) / 3.6e12
    dt_minutes = dt_hours * 60
    changes = np.abs(np.diff(values, axis=0)) / dt_minutes[:, None]
    run = longest = 0
    for flag in active.any(axis=1):
        run = run + 1 if flag else 0
        longest = max(longest, run)
    result.update(
        exposure_status="ok", e_mag_max_mV_km=float(np.nanmax(values)),
        e_mag_p95_mV_km=float(np.nanpercentile(values[finite], 95)),
        e_mag_mean_mV_km=float(mean_series.mean()),
        d_e_dt_max_mV_km_per_min=float(np.nanmax(changes)) if np.isfinite(changes).any() else np.nan,
        duration_hours_above_threshold=float(np.sum(active.any(axis=1)[:-1] * dt_hours)),
        max_consecutive_samples_above_threshold=longest,
        integrated_exposure_mV_km_hours=float(np.sum((excess[:-1] + excess[1:]) * 0.5 * dt_hours)),
        max_active_area_fraction=float(active_fraction.max()),
        mean_active_area_fraction=float(active_fraction.mean()),
    )
    if isinstance(local, xr.Dataset) and {"Ex", "Ey"}.issubset(local.data_vars):
        ex = np.asarray(local.Ex.transpose("time", "cell").values, float)
        ey = np.asarray(local.Ey.transpose("time", "cell").values, float)
        vector_changes = np.hypot(np.diff(ex, axis=0), np.diff(ey, axis=0)) / dt_minutes[:, None]
        if np.isfinite(vector_changes).any():
            result["vector_change_max_mV_km_per_min"] = float(np.nanmax(vector_changes))
    return result


def extract_geoe_window_descriptors_resumable(
    windows, geoe, id_col, start_col, end_col, output_csv, *, county_mapping,
    source_version, checkpoint_every=100, candidate_threshold=100.0,
    cadence=pd.Timedelta("1min"), min_support_fraction=0.9,
):
    """Resume only an identical versioned input manifest; preserve each window.

    source_version must identify the input-cache version, not just a directory.
    Stable keys retain matched episode IDs, county, times and explicit failures.
    """
    output_csv = Path(output_csv)
    if checkpoint_every < 1 or not source_version:
        raise ValueError("Positive checkpoint interval and source version required.")
    selected = windows[[id_col, "county_fips", start_col, end_col]].copy()
    selected["county_fips"] = _fips(selected["county_fips"])
    for col in (start_col, end_col):
        selected[col] = _utc(selected[col].values)
    if selected.isna().any().any() or selected.duplicated().any():
        raise ValueError("Missing or duplicate window identities.")
    identity = selected.astype(str).agg("|".join, axis=1)
    keys = identity.map(lambda value: hashlib.sha256(value.encode()).hexdigest())
    config = {
        "version": VERSION, "source_version": source_version,
        "threshold": float(candidate_threshold), "cadence": str(cadence),
        "min_support_fraction": min_support_fraction,
        "window_hash": hashlib.sha256(selected.to_csv(index=False).encode()).hexdigest(),
        "mapping_hash": hashlib.sha256(county_mapping.to_csv(index=False).encode()).hexdigest(),
        "coordinate_hash": hashlib.sha256(b"".join(np.asarray(geoe[name].values).tobytes() for name in ("time", "y", "x"))).hexdigest(),
    }
    manifest = output_csv.with_suffix(output_csv.suffix + ".manifest.json")
    if output_csv.exists() and not manifest.exists():
        raise ValueError("Unversioned checkpoint: use a new output filename.")
    if manifest.exists() and json.loads(manifest.read_text()) != config:
        raise ValueError("Checkpoint inputs changed: use a new output filename.")
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    if not manifest.exists():
        manifest.write_text(json.dumps(config, indent=2))
    existing = pd.read_csv(output_csv, dtype={"county_fips": str}) if output_csv.exists() else pd.DataFrame()
    done = set(existing["window_key"]) if not existing.empty else set()
    if not existing.empty and (existing["window_key"].duplicated().any() or not done.issubset(set(keys))):
        raise ValueError("Invalid checkpoint window keys.")
    buffer = []
    for position, row in enumerate(selected.itertuples(index=False, name=None)):
        key = keys.iloc[position]
        if key in done:
            continue
        episode_id, fips, start, end = row
        record = summarize_geoe_window(
            geoe, start, end, candidate_threshold, county_fips=fips,
            county_mapping=county_mapping, cadence=cadence,
            min_support_fraction=min_support_fraction,
        )
        record.update({id_col: episode_id, "window_key": key,
                       "window_start_time": start, "window_end_time": end})
        buffer.append(record)
        if len(buffer) >= checkpoint_every:
            pd.DataFrame(buffer).to_csv(output_csv, mode="a", header=not output_csv.exists(), index=False)
            buffer = []
    if buffer:
        pd.DataFrame(buffer).to_csv(output_csv, mode="a", header=not output_csv.exists(), index=False)
    if not output_csv.exists():
        return pd.DataFrame(columns=[id_col, "county_fips", "window_key", "exposure_status"] + GEOE_DESCRIPTOR_COLUMNS)
    return pd.read_csv(output_csv, dtype={"county_fips": str})