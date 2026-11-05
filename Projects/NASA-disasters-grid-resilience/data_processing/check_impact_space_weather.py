"""Bounded geomagnetic context audit of two county-local pilot matched sets.

Not an association score or causal test. No Eagle-I/GeoE rebuilds or lag search.
Run as a module from the repository root. Downloads require --download and a
locally supplied SUPERMAG_USERID. Existing cache and outputs are preserved.
"""
from __future__ import annotations

import argparse
import json
import socket
from pathlib import Path

import numpy as np
import pandas as pd
import xarray as xr

from .omni_data import fetch_omni_hro2_1min
from .supermag_data import fetch_supermag_station

DATA_ROOT = Path('/Users/ryanmc/Documents/NASA_JPL/Projects/NaturalHazards/'
                 'NASA ROSES Disasters 2025-2027/data')
GEOE_ROOT = DATA_ROOT / 'space_weather' / 'processed_data'
EAGLE_ROOT = Path('/Users/ryanmc/Documents/Conferences/Jack_Eddy_Symposium_2022/'
                  'dev/outage_data/EAGLE-I/processed')
PARENTS = ['eaglei_19095_202407221815', 'eaglei_19033_202505152015']


def ground_descriptors(frame, start, end):
    """UTC-minute NEZ/geo metrics; changes require adjacent valid minute pairs.

    Horizontal vector step is not a GIC measurement or inferred electric field.
    Peak-to-peak is insensitive to a constant baseline, not secular baseline drift.
    """
    index = pd.date_range(start, end, freq='min', name='timestamp')
    if not isinstance(frame.index, pd.DatetimeIndex) or frame.index.has_duplicates:
        raise ValueError('A unique timestamp index is required for one station.')
    local = frame.reindex(index)
    result = {'expected_minutes': len(index)}
    for components, label in [(['N', 'E'], 'magnetic'), (['geox', 'geoy'], 'geographic')]:
        values = local[components].to_numpy(dtype=float)
        valid = np.isfinite(values).all(axis=1)
        steps = np.linalg.norm(np.diff(values, axis=0), axis=1)
        valid_pairs = valid[1:] & valid[:-1]
        steps[~valid_pairs] = np.nan
        result[label + '_valid_minutes'] = int(valid.sum())
        result[label + '_valid_fraction'] = float(valid.mean()) if len(valid) else np.nan
        result[label + '_valid_pairs'] = int(valid_pairs.sum())
        finite_steps = steps[np.isfinite(steps)]
        result[label + '_horizontal_step_max_nT_per_min'] = float(finite_steps.max()) if len(finite_steps) else np.nan
        result[label + '_horizontal_step_p95_nT_per_min'] = float(np.percentile(finite_steps, 95)) if len(finite_steps) else np.nan
        if len(finite_steps):
            result[label + '_step_peak_utc'] = index[1:][np.nanargmax(steps)].isoformat()
        for j, component in enumerate(components):
            finite = values[:, j][np.isfinite(values[:, j])]
            result[component + '_peak_to_peak_nT'] = float(np.ptp(finite)) if len(finite) else np.nan
    result['coverage_status'] = 'ok' if result['magnetic_valid_fraction'] >= .9 else 'insufficient_support'
    return result


def pilot_windows():
    frames = []
    for role in ['impact', 'quiet']:
        path = GEOE_ROOT / f'eaglei_{role}_geoe_county_local_v2_pilot_20261012.csv'
        frame = pd.read_csv(path, dtype={'county_fips': str})
        frame = frame.loc[frame.impact_episode_id.isin(PARENTS)].copy()
        frame['role'] = 'impact' if role == 'impact' else 'no_impact_control'
        frame['descriptor_source'] = str(path)
        frames.append(frame)
    windows = pd.concat(frames, ignore_index=True)
    for name in ['window_start_time', 'window_end_time']:
        windows[name] = pd.to_datetime(windows[name], utc=True)
    return windows.sort_values(['county_fips', 'window_start_time'])


def _local_geoe(start, end, cells):
    path = GEOE_ROOT / f'geoe_interpolated_v1_fillmasked_interp_noextrap_{start:%Y_%m}.nc'
    with xr.open_dataset(path) as dataset:
        block = dataset.sel(time=slice(start.tz_localize(None), end.tz_localize(None))).isel(
            y=xr.DataArray(cells.grid_y.to_numpy(int), dims='cell'),
            x=xr.DataArray(cells.grid_x.to_numpy(int), dims='cell'))
        values = block.E_mag.transpose('time', 'cell').values
        times = pd.to_datetime(block.time.values, utc=True)
    series = pd.DataFrame(values, index=times, columns=[f'cell_{i}' for i in range(len(cells))])
    return series, str(path)


def run(output_dir, allow_download=False):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt

    output_dir = Path(output_dir)
    if output_dir.exists():
        raise FileExistsError('Choose a new output directory; existing analysis is not overwritten.')
    output_dir.mkdir(parents=True)
    windows = pilot_windows()
    windows.to_csv(output_dir / 'windows.csv', index=False)
    mapping = pd.read_csv(EAGLE_ROOT / 'eaglei_county_to_analysis_grid_cells_area_weighted.csv',
                          dtype={'county_fips': str})
    ground_rows, omni_rows, errors, manifests = [], [], [], []
    # Same calendar bounds and context rule across all impact/control dates.
    for window in windows.itertuples(index=False):
        start, end = window.window_start_time, window.window_end_time
        left, right = start - pd.Timedelta(hours=2), end + pd.Timedelta(hours=2)
        label = f'{window.county_fips}_{start:%Y%m%dT%H%M}_{window.role}'
        print('Checking', label, flush=True)
        # M03/R03 absent in the May 15 inventory and empty on all five dates.
        # PIN is nearest available (~822 km); BOU (~1051 km) supplies lower-
        # latitude context. Freeze this set across dates, not on signal strength.
        stations = ['R01'] if window.county_fips == '19095' else ['PIN', 'BOU']
        cells = mapping.loc[mapping.county_fips.eq(window.county_fips)]
        geoe, geoe_path = _local_geoe(left, right, cells)
        geoe.to_csv(output_dir / f'{label}_geoe.csv', index_label='time')
        omni, coverage = fetch_omni_hro2_1min(left, right, return_coverage=True)
        manifests.append({'window': label, 'geoe_source': geoe_path,
                          'omni': omni.attrs, 'ground': []})
        omni.to_csv(output_dir / f'{label}_omni.csv', index=False)
        coverage.to_csv(output_dir / f'{label}_omni_context_coverage.csv')
        inside = omni.loc[omni.time.between(start, end)].set_index('time')
        for variable in inside:
            finite = inside[variable].dropna()
            omni_rows.append(dict(window=label, role=window.role, variable=variable,
                                  n_valid=len(finite), n_expected=len(inside),
                                  minimum=finite.min(), median=finite.median(), maximum=finite.max()))
        fig, axes = plt.subplots(6, 1, figsize=(12, 15), sharex=True)
        axes[0].plot(geoe.index, geoe, alpha=.65)
        axes[0].set_ylabel('County cells |E|\n(mV/km)')
        for station in stations:
            try:
                ground = fetch_supermag_station(station, start.normalize(),
                                                 allow_download=allow_download)
            except (ValueError, RuntimeError, FileNotFoundError) as exc:
                errors.append({'window': label, 'station': station, 'error_type': type(exc).__name__})
                print('Station unavailable:', station, type(exc).__name__, flush=True)
                continue
            manifests[-1]['ground'].append(ground.attrs)
            ground = ground.xs(station, level='station')
            context = ground.reindex(pd.date_range(left, right, freq='min', name='timestamp'))
            context.to_csv(output_dir / f'{label}_{station}.csv')
            metrics = ground_descriptors(ground, start, end)
            metrics.update(window=label, station=station, role=window.role,
                           county_fips=window.county_fips, geoe_max_mV_km=window.e_mag_max_mV_km)
            ground_rows.append(metrics)
            # Display relative to context median; never modify persisted source baseline.
            for component in ['N', 'E']:
                axes[1].plot(context.index, context[component] - context[component].median(),
                             label=f'{station} {component}', alpha=.8)
            steps = np.linalg.norm(context[['N', 'E']].diff().to_numpy(), axis=1)
            axes[2].plot(context.index, steps, label=station, alpha=.8)
        axes[1].set_ylabel('N/E minus context\nmedian (nT)')
        axes[2].set_ylabel('Horizontal vector\nstep (nT/min)')
        axes[3].plot(omni.time, omni.AE_nT, label='AE')
        axes[3].plot(omni.time, omni.SymH_nT, label='SYM-H')
        axes[3].set_ylabel('Indices (nT)')
        axes[4].plot(omni.time, omni.Bz_GSM_nT, label='Bz GSM')
        axes[4].plot(omni.time, omni.By_GSM_nT, label='By GSM', alpha=.7)
        axes[4].set_ylabel('IMF (nT)')
        axes[5].plot(omni.time, omni.FlowSpeed_km_s, label='Flow speed')
        axes[5].set_ylabel('Speed (km/s)')
        for axis in axes:
            axis.axvspan(start, end, color='grey', alpha=.18)
            axis.grid(alpha=.2)
            if axis.get_legend_handles_labels()[0]:
                axis.legend(loc='upper left', ncol=4, fontsize=8)
        axes[-1].set_xlabel('UTC; grey = original pilot window; no lag adjustment')
        fig.suptitle(label + '\nGround stations are regional context, not county GIC measurements')
        fig.tight_layout(rect=(0, 0, 1, .96))
        fig.savefig(output_dir / f'{label}.png', dpi=130)
        plt.close(fig)
        # Persist after each window so partial success is useful on interruption.
        pd.DataFrame(ground_rows).to_csv(output_dir / 'ground_window_summary.csv', index=False)
        pd.DataFrame(omni_rows).to_csv(output_dir / 'omni_window_summary.csv', index=False)
        (output_dir / 'manifest.json').write_text(json.dumps({
            'context_hours_each_side': 2, 'baseline': 'yearly; no delta=start',
            'station_selection': {'19095': ['R01'], '19033': ['PIN', 'BOU']},
            'station_selection_note': 'May M03/R03 empty; May15 inventory nearest PIN 822km, BOU 1051km selected for lower-latitude context. Not local validation.',
            'selection': 'Two preselected pilot matched sets; not representative',
            'coverage_gate': '>=90% finite horizontal minutes; descriptive only',
            'geoE_units': 'mV/km confirmed by user; missing derived variable unit attrs',
            'caveats': 'No causal attribution, threshold fitting, lag search or geomagnetic control filtering. Ground stations may share NOAA model inputs.',
            'sources': manifests, 'errors': errors}, indent=2, default=str))
    print(pd.DataFrame(ground_rows).to_string(index=False))
    print('Saved', output_dir, flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--download', action='store_true')
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    socket.setdefaulttimeout(90)
    run(args.output_dir, allow_download=args.download)