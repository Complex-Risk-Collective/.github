"""Read cached or explicitly download monthly SPDF OMNI HRO2 one-minute CDFs.

No SunPy import: requests + cdflib avoid the user's SunPy/Astropy mismatch.
Times are UTC; requested bounds are inclusive. Never interpolate missing data.
"""
from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urljoin
from uuid import uuid4

import numpy as np
import pandas as pd

OMNI_CACHE_DIR = Path('/Users/ryanmc/sunpy/data')
OMNI_BASE_URL = 'https://spdf.gsfc.nasa.gov/pub/data/omni/omni_cdaweb/hro2_1min'
OMNI_VARIABLES = {
    'Bz_GSM_nT': ('BZ_GSM',),
    'By_GSM_nT': ('BY_GSM',),
    'FlowSpeed_km_s': ('flow_speed',),
    'ProtonDensity_n_cc': ('proton_density',),
    'DynamicPressure_nPa': ('Pressure',),
    'AE_nT': ('AE_INDEX',),
    'AL_nT': ('AL_INDEX',),
    'AU_nT': ('AU_INDEX',),
    'SymH_nT': ('SYM_H',),
}
# Genuine sign constraints, not nominal storm-amplitude rejection thresholds.
OMNI_PHYSICAL_LIMITS = {
    'FlowSpeed_km_s': (0, None),
    'ProtonDensity_n_cc': (0, None),
    'DynamicPressure_nPa': (0, None),
    'AE_nT': (0, None),
}


def _clean_values(values, name, fill_values=(), field_limits=None):
    values = pd.to_numeric(pd.Series(values), errors='coerce').to_numpy(dtype=float)
    invalid = ~np.isfinite(values)
    for fill in np.asarray(fill_values).reshape(-1):
        # Accommodate a float32 CDF sentinel represented as a Python float.
        fill = float(fill)
        tolerance = max(1e-8, abs(fill) * np.finfo(np.float32).eps)
        invalid |= np.isclose(values, fill, rtol=0, atol=tolerance)
    limits = dict(OMNI_PHYSICAL_LIMITS)
    if field_limits is not None:
        unknown = set(field_limits) - set(OMNI_VARIABLES)
        if unknown:
            raise ValueError(f'Unknown OMNI field limits: {sorted(unknown)}')
        limits.update(field_limits)
    lower, upper = limits.get(name, (None, None))
    if lower is not None:
        invalid |= values < lower
    if upper is not None:
        invalid |= values > upper
    values[invalid] = np.nan
    return values


def omni_coverage(frame):
    """Per-variable valid/missing counts and percent, excluding the time column.

    Empty frames have zero counts and NaN percent (not misleading 100%/0%).
    For resampled frames this counts output bins, not source-minute coverage.
    """
    columns = list(OMNI_VARIABLES)
    valid = np.isfinite(frame.reindex(columns=columns).to_numpy(dtype=float)).sum(axis=0)
    total = len(frame)
    return pd.DataFrame({'n_valid': valid, 'n_missing': total - valid,
                         'n_total': total,
                         'pct_valid': 100 * valid / total if total else np.full(len(columns), np.nan)},
                        index=pd.Index(columns, name='variable'))


def utc_bounds(start_time, end_time):
    """Interpret naive inputs as UTC, convert aware inputs to UTC, validate order."""
    # Parse independently: pandas otherwise infers one format for both strings,
    # rejecting a date-only start paired with an ISO timestamp end.
    start = pd.to_datetime(start_time, utc=True)
    end = pd.to_datetime(end_time, utc=True)
    if pd.isna(start) or pd.isna(end) or end < start:
        raise ValueError('Finite start/end required, with end >= start.')
    return start, end


def read_omni_cdf(filepath, *, cdf_factory=None, field_limits=None):
    """Read source rows, masking FILLVAL, nonfinite and impossible signs.

    Preserve unusual but finite extremes: VALIDMIN/MAX can be stale nominal
    bounds, not scientific rejection criteria. Original source/units metadata
    are retained in DataFrame.attrs. Missing variables stay NaN and are listed.
    Optional field_limits maps output names to inclusive (lower, upper) bounds;
    None disables that side. Fetch functions additionally expose absent minutes.
    """
    if cdf_factory is None:
        from cdflib import CDF
        cdf_factory = CDF
    from cdflib import cdfepoch

    cdf = cdf_factory(str(filepath))
    try:
        info = cdf.cdf_info()
        names = list(info.zVariables if hasattr(info, 'zVariables') else info['zVariables'])
        lookup = {name.lower(): name for name in names}
        epoch = lookup.get('epoch')
        if epoch is None:
            raise ValueError(f'Missing Epoch in {Path(filepath).name}.')
        times = pd.to_datetime(cdfepoch.to_datetime(cdf.varget(epoch)), utc=True)
        if pd.isna(times).any():
            raise ValueError('Invalid CDF timestamps.')
        frame = pd.DataFrame({'time': times})
        units, source_columns, missing = {}, {}, []
        for output, aliases in OMNI_VARIABLES.items():
            source = next((lookup[a.lower()] for a in aliases if a.lower() in lookup), None)
            if source is None:
                frame[output] = np.nan
                missing.append(output)
                continue
            raw = np.asarray(cdf.varget(source)).reshape(-1)
            if len(raw) != len(frame):
                raise ValueError(f'{source} length does not match Epoch.')
            attrs = cdf.varattsget(source)
            values = _clean_values(raw, output, attrs.get('FILLVAL', []), field_limits)
            frame[output] = values
            units[output] = str(attrs.get('UNITS', 'unspecified'))
            source_columns[output] = source
        if frame['time'].duplicated().any():
            raise ValueError('Duplicate timestamps in a single OMNI CDF.')
        frame = frame.sort_values('time').reset_index(drop=True)
        frame.attrs.update(source_path=str(filepath), source_product='OMNI_HRO2_1MIN',
                           units=units, source_columns=source_columns,
                           missing_variables=missing, time_basis='UTC',
                           field_limits={**OMNI_PHYSICAL_LIMITS, **(field_limits or {})},
                           fill_policy='CDF FILLVAL, nonfinite and configured physical limits; no interpolation',
                           coverage=omni_coverage(frame).to_dict(orient='index'))
        return frame
    finally:
        # Some cdflib releases have close(), others release file handles internally.
        close = getattr(cdf, 'close', None)
        if callable(close):
            close()


def _month_file(month, cache_dir, allow_download, session, timeout):
    stem = f'omni_hro2_1min_{month:%Y%m}01_'
    pattern = re.compile(re.escape(stem) + r'v(\d+)\.cdf$')
    # Reuse nested SunPy holdings as well as files directly in its data root.
    candidates = [p for p in cache_dir.rglob(stem + 'v*.cdf')
                  if pattern.fullmatch(p.name) and p.stat().st_size > 0]
    if candidates:
        return max(candidates, key=lambda p: (int(pattern.fullmatch(p.name).group(1)), str(p)))
    if not allow_download:
        raise FileNotFoundError(f'No cached OMNI {month:%Y-%m} CDF under {cache_dir}; allow_download=False.')
    if session is None:
        import requests
        session = requests.Session()
    directory = f'{OMNI_BASE_URL}/{month.year}/'
    listing = session.get(directory, timeout=timeout)
    listing.raise_for_status()
    versions = set(re.findall(re.escape(stem) + r'v\d+\.cdf', listing.text))
    if not versions:
        raise FileNotFoundError(f'No OMNI file for {month:%Y-%m} in SPDF directory.')
    filename = max(versions, key=lambda name: int(pattern.fullmatch(name).group(1)))
    target = cache_dir / filename
    cache_dir.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + f'.{uuid4().hex}.partial')
    try:
        with session.get(urljoin(directory, filename), stream=True, timeout=timeout) as response:
            response.raise_for_status()
            with partial.open('wb') as handle:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        handle.write(chunk)
        # Validate parsing before publishing the new cache; never accept HTML/empty files.
        read_omni_cdf(partial)
        if target.exists():
            raise FileExistsError(target)
        partial.replace(target)
    finally:
        partial.unlink(missing_ok=True)
    return target


def fetch_omni_hro2_1min(start_time, end_time, cache_dir=OMNI_CACHE_DIR, *,
                         allow_download=False, session=None, timeout=120,
                         freq='1min', return_coverage=False, missing_files='raise',
                         field_limits=None):
    """Load an inclusive UTC interval; fetch missing months only when opted in.

    Existing local CDF versions take precedence without a network check. A bad
    cache raises an error rather than silently redownloading or overwriting it.
    Required packages: pandas, numpy, cdflib; requests only for downloads.

    Reindex onto all UTC minutes within inclusive bounds; absent observations
    and all-missing variables remain NaN. Optional freq (e.g. '5min') averages
    available minutes with no filling: all-missing bins remain NaN. Bins use UTC
    epoch-aligned left labels within bounds; boundary bins may be partial.
    return_coverage=True returns (frame, coverage), including native-minute and
    output-bin coverage separately. Reports also live in frame.attrs['coverage'].
    missing_files='nan' explicitly permits absent months as NaN, recorded in
    attrs; default 'raise' preserves cache-only errors. Parse/network failures
    are never silently converted into missing observations.
    """
    start, end = utc_bounds(start_time, end_time)
    offset = pd.tseries.frequencies.to_offset(freq)
    try:
        step = pd.Timedelta(offset.nanos, unit='ns')
    except ValueError:
        raise ValueError('freq must be a fixed whole-minute interval.') from None
    if step < pd.Timedelta(minutes=1) or step.value % pd.Timedelta(minutes=1).value:
        raise ValueError('freq must be a positive whole-minute interval.')
    if missing_files not in {'raise', 'nan'}:
        raise ValueError("missing_files must be 'raise' or 'nan'.")
    months = pd.date_range(start.normalize().replace(day=1),
                           end.normalize().replace(day=1), freq='MS')
    frames, sources, metadata, absent_months = [], [], [], []
    for month in months:
        try:
            path = _month_file(month, Path(cache_dir), allow_download, session, timeout)
        except FileNotFoundError:
            if missing_files == 'raise':
                raise
            absent_months.append(month.strftime('%Y-%m'))
            continue
        block = read_omni_cdf(path, field_limits=field_limits)
        metadata.append(dict(block.attrs))
        sources.append(str(path))
        block.attrs = {}  # Avoid pandas comparing complex attrs during concatenation.
        frames.append(block.loc[block['time'].between(start, end)])
    frame = (pd.concat(frames, ignore_index=True).sort_values('time').reset_index(drop=True)
             if frames else pd.DataFrame({'time': pd.DatetimeIndex([], tz='UTC')}))
    if frame['time'].duplicated().any():
        raise ValueError('Overlapping monthly OMNI timestamps; inspect source files.')
    if not frame['time'].eq(frame['time'].dt.floor('min')).all():
        raise ValueError('OMNI observations are not aligned to UTC minutes.')
    native_index = pd.date_range(start.ceil('min'), end.floor('min'), freq='min', name='time')
    native = frame.set_index('time').reindex(index=native_index, columns=list(OMNI_VARIABLES))
    for name in OMNI_VARIABLES:
        native[name] = _clean_values(native[name], name, field_limits=field_limits)
    native_coverage = omni_coverage(native)
    if step == pd.Timedelta(minutes=1):
        frame = native.reset_index()
    else:
        # pandas mean already leaves empty/all-NaN groups as NaN (no min_count
        # argument is supported by mean). Counts below expose partial bins.
        resampled = native.resample(offset, origin='epoch', label='left', closed='left').mean()
        frame = resampled.loc[(resampled.index >= start) & (resampled.index <= end)].reset_index()
    coverage = omni_coverage(frame)
    for column in ['n_valid', 'n_missing', 'n_total', 'pct_valid']:
        coverage[column + '_native'] = native_coverage[column]
    frame.attrs.update(source_product='OMNI_HRO2_1MIN', source_files=sources,
                       source_metadata=metadata, time_basis='UTC',
                       requested_start=start.isoformat(), requested_end=end.isoformat(),
                       units={key: value for m in metadata for key, value in m['units'].items()},
                       output_frequency=str(offset), absent_months=absent_months,
                       field_limits={**OMNI_PHYSICAL_LIMITS, **(field_limits or {})},
                       coverage=coverage.to_dict(orient='index'),
                       coverage_note='Native counts include all requested UTC minutes; bin validity does not imply full support.')
    return (frame, coverage) if return_coverage else frame


def fetch_omni_for_days(days, freq='5min', cache_dir=OMNI_CACHE_DIR, *,
                         return_coverage=False, missing_files='nan', **kwargs):
    """Load only selected UTC days, not intervening months; no downloads by default.

    Returns the usual time-column DataFrame, or (frame, per-day coverage) with
    return_coverage=True. Absent files default to all-NaN days here; manifests
    identify them. Corrupt files/network errors still raise. Days are normalized,
    deduplicated and sorted in UTC. All minutes of each day contribute to means,
    including the final bin (unlike clipping input at the last bin label).
    """
    normalized = sorted({pd.to_datetime(day, utc=True).normalize() for day in days})
    if any(pd.isna(day) for day in normalized):
        raise ValueError('Finite days required.')
    frames, reports, manifests = [], {}, {}
    for day in normalized:
        frame, coverage = fetch_omni_hro2_1min(
            day, day + pd.Timedelta(days=1) - pd.Timedelta(minutes=1),
            cache_dir, freq=freq, return_coverage=True, missing_files=missing_files,
            **kwargs,
        )
        label = day.strftime('%Y-%m-%d')
        manifests[label] = dict(frame.attrs)
        reports[label] = coverage
        frame.attrs = {}
        frames.append(frame)
    result = (pd.concat(frames, ignore_index=True) if frames else
              pd.DataFrame({'time': pd.DatetimeIndex([], tz='UTC'),
                            **{name: pd.Series(dtype=float) for name in OMNI_VARIABLES}}))
    coverage = (pd.concat(reports, names=['day', 'variable']) if reports else
                pd.DataFrame(columns=['n_valid', 'n_missing', 'n_total', 'pct_valid',
                                      'n_valid_native', 'n_missing_native',
                                      'n_total_native', 'pct_valid_native'],
                             index=pd.MultiIndex.from_tuples([], names=['day', 'variable'])))
    result.attrs.update(source_product='OMNI_HRO2_1MIN', time_basis='UTC',
                        day_metadata=manifests,
                        coverage_by_day={day: report.to_dict(orient='index') for day, report in reports.items()})
    return (result, coverage) if return_coverage else result