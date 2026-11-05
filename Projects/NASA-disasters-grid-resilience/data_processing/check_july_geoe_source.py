"""Read-only July 8 source/cache pulse audit; writes a new diagnostic directory."""
from pathlib import Path
import json

import numpy as np
import pandas as pd
import xarray as xr
from scipy.spatial import Delaunay

from .check_impact_space_weather import GEOE_ROOT, EAGLE_ROOT


def run():
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt

    source = GEOE_ROOT.parent / 'NOAA_geoE' / '20240708-empirical-EMTF-2022.12-2022.12.nc'
    cache = GEOE_ROOT / 'geoe_interpolated_v1_fillmasked_interp_noextrap_2024_07.nc'
    output = GEOE_ROOT / 'july08_original_source_audit_20261019'
    if output.exists():
        raise FileExistsError(output)
    output.mkdir()
    with xr.open_dataset(source, decode_times=False) as raw, xr.open_dataset(cache) as derived:
        # Upper-case UNITS is not automatically decoded by xarray.
        times = pd.Timestamp(raw.time.attrs['REFTIME'], tz='UTC') + pd.to_timedelta(raw.time.values, unit='s')
        lat = raw.latitude.values.astype(float)
        lon = raw.longitude.values.astype(float)
        lon = (lon + 180) % 360 - 180
        keep = np.isfinite(lat) & np.isfinite(lon)
        original_indices = np.flatnonzero(keep)
        tri = Delaunay(np.column_stack([lon[keep], lat[keep]]))
        cells = pd.read_csv(EAGLE_ROOT / 'eaglei_county_to_analysis_grid_cells_area_weighted.csv',
                            dtype={'county_fips': str})
        cells = cells.loc[cells.county_fips.eq('19095')].reset_index(drop=True)
        target = np.column_stack([derived.lon.values[cells.grid_y, cells.grid_x],
                                  derived.lat.values[cells.grid_y, cells.grid_x]])
        simplex = tri.find_simplex(target)
        if np.any(simplex < 0):
            raise ValueError('Unexpected unsupported target.')
        transform = tri.transform[simplex]
        first = np.einsum('ijk,ik->ij', transform[:, :2, :], target - transform[:, 2, :])
        weights = np.column_stack([first, 1 - first.sum(axis=1)])
        vertices = original_indices[tri.simplices[simplex]]
        selected = (times >= pd.Timestamp('2024-07-08T18:25Z')) & (times <= pd.Timestamp('2024-07-08T19:15Z'))
        pos = np.flatnonzero(selected)
        ex, ey = [raw[name].isel(time=pos).values.astype(float) for name in ['Ex', 'Ey']]
        for values in [ex, ey]:
            values[~np.isfinite(values) | (np.abs(values) > 1e20)] = np.nan
        reconstructed_ex = np.einsum('tcv,cv->tc', ex[:, vertices], weights)
        reconstructed_ey = np.einsum('tcv,cv->tc', ey[:, vertices], weights)
        reconstructed = np.hypot(reconstructed_ex, reconstructed_ey)
        local = derived.sel(time=times[selected].tz_localize(None)).isel(
            y=xr.DataArray(cells.grid_y.to_numpy(), dims='cell'),
            x=xr.DataArray(cells.grid_x.to_numpy(), dims='cell'))
        cached = local.E_mag.transpose('time', 'cell').values
        records, geometry = [], []
        for cell, vertex_group in enumerate(vertices):
            for vertex, weight in zip(vertex_group, weights[cell]):
                geometry.append(dict(grid_y=int(cells.grid_y[cell]), grid_x=int(cells.grid_x[cell]),
                                     source_gridpt=int(vertex), source_lat=lat[vertex], source_lon=lon[vertex],
                                     barycentric_weight=float(weight),
                                     source_distance_to_observatory_km=float(raw.distance[vertex])))
        for vertex in np.unique(vertices):
            for j, p in enumerate(pos):
                records.append(dict(time=times[p].isoformat(), source_gridpt=int(vertex),
                                    latitude=lat[vertex], longitude=lon[vertex],
                                    Ex=ex[j, vertex], Ey=ey[j, vertex],
                                    E_mag=np.hypot(ex[j, vertex], ey[j, vertex]),
                                    Quality=float(raw.Quality[p, vertex]), nobs=int(raw.nobs[p]),
                                    obslist=raw.obslist[p].item().decode().strip(),
                                    source_emax=float(raw.emax[p])))
        pd.DataFrame(records).to_csv(output / 'source_points.csv', index=False)
        pd.DataFrame(geometry).to_csv(output / 'interpolation_vertices.csv', index=False)
        for name, values in [('cached_magnitude', cached), ('reconstructed_magnitude', reconstructed)]:
            pd.DataFrame(values, index=times[selected], columns=[f'y{y}_x{x}' for y, x in zip(cells.grid_y, cells.grid_x)]).to_csv(output / f'{name}.csv', index_label='time')
        error = float(np.nanmax(np.abs(cached - reconstructed)))
        events = pd.DataFrame(records)
        events = events.loc[events.time.isin(['2024-07-08T18:34:30+00:00', '2024-07-08T19:06:30+00:00'])]
        print(events.to_string(index=False))
        print('Max cache vs reconstructed magnitude difference (mV/km):', error)
        transitions = pd.DataFrame({'time': times[selected], 'nobs': raw.nobs.values[selected],
                                    'obslist': [v.decode().strip() for v in raw.obslist.values[selected]]})
        print('Observatory-list transitions:', transitions.loc[transitions.obslist.ne(transitions.obslist.shift())].to_string(index=False))
        transitions.to_csv(output / 'observatory_inputs.csv', index=False)
        (output / 'manifest.json').write_text(json.dumps(dict(source=str(source), cache=str(cache),
            source_attrs=raw.attrs, source_variable_attrs={name: raw[name].attrs for name in raw},
            max_absolute_reconstruction_difference_mV_km=error,
            quality_note='Quality code semantics unspecified in file; do not infer good/bad from code 3.',
            conclusion='Both pulses exist in original modeled Ex/Ey; interpolation is not their origin.'), indent=2, default=str))
        fig, axes = plt.subplots(3, 1, figsize=(12, 10), sharex=True)
        unique = np.unique(vertices)
        for vertex in unique:
            axes[0].plot(times[selected], np.hypot(ex[:, vertex], ey[:, vertex]), label=f'point {vertex}')
        axes[0].set_ylabel('Original point |E|\n(mV/km)')
        axes[0].legend(ncol=4, fontsize=7)
        for cell in range(len(cells)):
            axes[1].plot(times[selected], cached[:, cell], label=f'cache ({cells.grid_y[cell]}, {cells.grid_x[cell]})')
            axes[1].plot(times[selected], reconstructed[:, cell], '--', color='black', alpha=.4)
        axes[1].set_ylabel('County grid |E|\n(mV/km)')
        axes[1].legend(fontsize=7)
        axes[2].step(times[selected], raw.nobs.values[selected], where='mid')
        axes[2].set_ylabel('Input observatories')
        for axis in axes:
            for timestamp in ['2024-07-08T18:34:30Z', '2024-07-08T19:06:30Z']:
                axis.axvline(pd.Timestamp(timestamp), color='red', linestyle=':', alpha=.6)
            axis.grid(alpha=.25)
        axes[-1].set_xlabel('2024-07-08 UTC; red = investigated pulses')
        fig.suptitle('Original NOAA modeled-point pulses and independently reconstructed county grid')
        fig.tight_layout()
        fig.savefig(output / 'original_vs_derived.png', dpi=150)
        plt.close(fig)
    print('Saved', output)


if __name__ == '__main__':
    run()