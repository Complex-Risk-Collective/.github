"""SuperMAG ASCII reader and opt-in, request-cached official API wrappers.

Preserve magnetic NEZ vs geographic XYZ; do not rebaseline or fill time gaps.
Raw downloads and derived products belong in the project SuperMAG_data root.
Honor SuperMAG/provider acknowledgement and data-use requirements.
"""
from __future__ import annotations

import hashlib
import importlib
import json
import os
import re
from pathlib import Path
from uuid import uuid4

import numpy as np
import pandas as pd

SUPERMAG_CACHE_DIR = Path(
    '/Users/ryanmc/Documents/NASA_JPL/Projects/NaturalHazards/'
    'NASA ROSES Disasters 2025-2027/data/SuperMAG_data'
)
DEFAULT_FLAGS = 'all,baseline=yearly'
ASCII_FIELDS = ['geo_lon', 'geo_lat', 'MLT', 'mag_colat', 'solar_zenith',
                'N', 'E', 'Z', 'geox', 'geoy', 'geoz']


def _utc(value):
    if isinstance(value, (tuple, list)):
        if not 3 <= len(value) <= 6:
            raise ValueError('Date lists need year, month, day and optional hour/minute/second.')
        value = pd.Timestamp(**dict(zip(['year', 'month', 'day', 'hour', 'minute', 'second'], value)))
    timestamp = pd.to_datetime(value, utc=True)
    if pd.isna(timestamp):
        raise ValueError('A finite UTC timestamp is required.')
    return timestamp


def _clean(series, fill_value=999999.0):
    values = pd.to_numeric(series, errors='coerce').astype(float)
    return values.mask(~np.isfinite(values) | values.eq(fill_value))


def read_supermag_file(filepath, fill_value=999999.0, *, stations=None):
    """Stream a SuperMAG custom ASCII export, following the user's reader.

    Returns a sorted (timestamp, station) MultiIndex DataFrame. timestamp is
    UTC-aware. Eleven numeric columns follow the station ID. N/E/Z are in the
    local magnetic frame; geox/geoy/geoz are geographic components, all in nT.
    Malformed data rows and duplicate identities raise instead of disappearing.
    Header/acknowledgement text is retained in attrs; the source is not changed.
    """
    selected = None if stations is None else {str(s).upper() for s in stations}
    records, header = [], []
    current = None
    expected_stations = seen_stations = None
    intervals = set()
    timestamp_pattern = re.compile(r'^\d{4}\s+\d{1,2}\s+\d{1,2}\s+\d{1,2}\s+\d{1,2}\s+\d{1,2}(?:\s|$)')
    with Path(filepath).open(encoding='utf-8-sig') as handle:
        for line_number, line in enumerate(handle, 1):
            text = line.strip()
            if not text:
                continue
            parts = text.split()
            if timestamp_pattern.match(text):
                if current is not None and expected_stations is not None and seen_stations != expected_stations:
                    raise ValueError(f'Incomplete timestamp block before line {line_number}.')
                if len(parts) not in (7, 8):
                    raise ValueError(f'Expected timestamp, optional cadence, and station count at line {line_number}.')
                year, month, day, hour, minute, second = map(int, parts[:6])
                expected_stations = int(parts[-1])
                if expected_stations < 0:
                    raise ValueError('Negative station count.')
                if len(parts) == 8:
                    interval = int(parts[6])
                    if interval <= 0:
                        raise ValueError('Nonpositive SuperMAG sampling interval.')
                    intervals.add(interval)
                current = pd.Timestamp(year=year, month=month, day=day, hour=hour,
                                       minute=minute, second=second, tz='UTC')
                seen_stations = 0
            elif current is None:
                header.append(line.rstrip('\n'))
            else:
                if len(parts) != 12:
                    raise ValueError(f'Expected station + eleven values at line {line_number}.')
                try:
                    values = [float(v) for v in parts[1:]]
                except ValueError as exc:
                    raise ValueError(f'Nonnumeric station data at line {line_number}.') from exc
                seen_stations += 1
                station = parts[0].upper()
                if selected is None or station in selected:
                    records.append({'timestamp': current, 'station': station,
                                    **dict(zip(ASCII_FIELDS, values))})
    if current is None:
        raise ValueError('Could not find a timestamp block in SuperMAG file.')
    if expected_stations is not None and seen_stations != expected_stations:
        raise ValueError('Incomplete final SuperMAG timestamp block.')
    frame = pd.DataFrame(records, columns=['timestamp', 'station'] + ASCII_FIELDS)
    frame['timestamp'] = pd.to_datetime(frame['timestamp'], utc=True)
    for name in ASCII_FIELDS:
        frame[name] = _clean(frame[name], fill_value)
    if frame.duplicated(['timestamp', 'station']).any():
        raise ValueError('Duplicate timestamp/station records in SuperMAG file.')
    frame = frame.set_index(['timestamp', 'station']).sort_index()
    frame.attrs.update(source_path=str(filepath), source_header='\n'.join(header),
                       time_basis='UTC', magnetic_components=['N', 'E', 'Z'],
                       geographic_components=['geox', 'geoy', 'geoz'],
                       component_units='nT', source_cadence_seconds=sorted(intervals),
                       baseline='see retained source_header')
    return frame


def normalize_supermag_response(data, *, station=None, kind='station'):
    """Flatten official API nested NEZ/geo records without mixing frames.

    Keep other scalar metadata/quality fields. N/E/Z map to nested 'nez';
    geox/geoy/geoz map to nested 'geo'. Do not substitute one for the other.
    """
    source = data.copy() if isinstance(data, pd.DataFrame) else pd.DataFrame(data)
    if source.empty or 'tval' not in source:
        raise ValueError('SuperMAG returned no timestamped data.')
    times = pd.to_datetime(pd.to_numeric(source['tval'], errors='coerce'), unit='s', utc=True)
    if times.isna().any():
        raise ValueError('Invalid API epoch timestamps.')
    out = pd.DataFrame({'timestamp': times})
    if kind == 'station':
        ids = source['iaga'].astype('string').str.upper() if 'iaga' in source else pd.Series(station, index=source.index, dtype='string')
        if ids.isna().any() or (station is not None and not ids.eq(station).all()):
            raise ValueError('Station identity missing or inconsistent with request.')
        out['station'] = ids
        for component, geographic in [('N', 'geox'), ('E', 'geoy'), ('Z', 'geoz')]:
            if component not in source:
                raise ValueError(f'Missing component {component}.')
            values = source[component]
            if not values.map(lambda v: isinstance(v, dict)).all():
                raise ValueError(f'{component} must contain explicit nez/geo coordinate keys.')
            out[component] = _clean(values.map(lambda v: v.get('nez', np.nan)))
            out[geographic] = _clean(values.map(lambda v: v.get('geo', np.nan)))
    for name in source:
        if name in {'tval', 'iaga', 'N', 'E', 'Z'}:
            continue
        if source[name].map(lambda v: isinstance(v, (dict, list))).any():
            continue  # Full raw structure remains in the request cache.
        numeric = pd.to_numeric(source[name], errors='coerce')
        target = {'glon': 'geo_lon', 'glat': 'geo_lat', 'mlt': 'MLT',
                  'mcolat': 'mag_colat', 'sza': 'solar_zenith'}.get(name, name)
        out[target] = _clean(numeric) if numeric.notna().any() else source[name]
    keys = ['timestamp', 'station'] if kind == 'station' else ['timestamp']
    if out.duplicated(keys).any():
        raise ValueError('Duplicate SuperMAG API observation identities.')
    out = out.set_index(keys).sort_index()
    out.attrs.update(time_basis='UTC', source_product='SuperMAG', component_units='nT')
    return out


def _flags(value, kind):
    flags = sorted({part.strip().lower() for part in value.split(',') if part.strip()})
    if not flags or any(not re.fullmatch(r'[a-z0-9_=]+', f) for f in flags):
        raise ValueError('Invalid SuperMAG flags.')
    if kind == 'station':
        supported = {'all', 'mlt', 'mag', 'geo', 'decl', 'sza', 'delta=start',
                     'baseline=yearly', 'baseline=none'}
        if not set(flags).issubset(supported):
            raise ValueError('Unsupported station flags in the installed official client.')
        baseline = [f for f in flags if f.startswith('baseline=')]
        if len(baseline) != 1:
            raise ValueError('Specify exactly one baseline: baseline=yearly or baseline=none.')
    return ','.join(flags)


def _request(kind, start_time, extent, *, station=None, flags, userid=None,
             cache_dir=SUPERMAG_CACHE_DIR, allow_download=False, api_client=None):
    start = _utc(start_time)
    if start.second or start.microsecond or start.nanosecond:
        raise ValueError('SuperMAG requests must start on a whole minute.')
    if extent != int(extent) or not 0 < extent <= 86400 or int(extent) % 60:
        raise ValueError('Request whole minutes, at most one day per call.')
    if kind == 'station':
        station = str(station).upper()
        if not re.fullmatch(r'[A-Z0-9]{3}', station):
            raise ValueError('Expected a three-character IAGA station code.')
    flags = _flags(flags, kind)
    params = dict(schema_version=1, kind=kind, start_utc=start.isoformat(),
                  extent_seconds=int(extent), station=station, flags=flags)
    digest = hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()[:20]
    label = station if kind == 'station' else 'indices'
    path = Path(cache_dir) / 'api_cache' / f'{label}_{start:%Y%m%dT%H%M}_{digest}.json'
    if path.exists():
        envelope = json.loads(path.read_text())
        if envelope.get('request') != params:
            raise ValueError('SuperMAG cache manifest mismatch.')
        raw = envelope['records']
    else:
        if not allow_download:
            raise FileNotFoundError(f'No cached {label} request for {start.isoformat()}; allow_download=False.')
        userid = userid or os.environ.get('SUPERMAG_USERID')
        if not userid or userid == 'YOUR_SUPERMAG_USERID':
            raise ValueError('Set SUPERMAG_USERID locally or pass userid; do not store credentials in code.')
        if api_client is None:
            # The installed package __init__ is empty; functions live in its submodule.
            api_client = importlib.import_module('supermag_api.supermag_api')
        begin = start.strftime('%Y-%m-%dT%H:%M')
        # Suppress client exceptions here: request URLs can contain user identifiers.
        try:
            if kind == 'station':
                success, response = api_client.SuperMAGGetData(userid, begin, int(extent), flags, station)
            else:
                success, response = api_client.SuperMAGGetIndices(userid, begin, int(extent), flags)
        except Exception:
            raise RuntimeError('SuperMAG client request failed; check service/access locally (URL omitted).') from None
        if not success:
            raise RuntimeError('SuperMAG returned unsuccessful status (server response omitted).')
        raw = (json.loads(response.to_json(orient='records'))
               if isinstance(response, pd.DataFrame) else response)
        checked = normalize_supermag_response(raw, station=station, kind=kind)
        # Official bounds are request-start plus extent, treated as half-open.
        times = checked.index.get_level_values('timestamp')
        if not ((times >= start) & (times < start + pd.Timedelta(seconds=extent))).all():
            raise ValueError('SuperMAG returned observations outside the requested half-open interval.')
        path.parent.mkdir(parents=True, exist_ok=True)
        partial = path.with_name(path.name + f'.{uuid4().hex}.partial')
        try:
            partial.write_text(json.dumps({'request': params, 'records': raw}, allow_nan=False))
            if path.exists():
                raise FileExistsError(path)
            partial.replace(path)
        finally:
            partial.unlink(missing_ok=True)
    frame = normalize_supermag_response(raw, station=station, kind=kind)
    times = frame.index.get_level_values('timestamp')
    if not ((times >= start) & (times < start + pd.Timedelta(seconds=extent))).all():
        raise ValueError('SuperMAG cached observations outside requested interval.')
    frame.attrs.update(request=params, source_path=str(path),
                       coverage_note='Returned samples only; no interpolation or implied completeness.')
    return frame


def fetch_supermag_station(station, start_time, extent=86400, *, flags=DEFAULT_FLAGS,
                           userid=None, cache_dir=SUPERMAG_CACHE_DIR,
                           allow_download=False, api_client=None):
    """Get <=1 day of station data, cache by station/baseline/time/extent.

    Default baseline is explicitly yearly; delta=start is deliberately NOT
    applied, so separately requested days are not arbitrarily zeroed. API data
    retain all supplied scalar quality/metadata columns. No quality flag is
    invented. Cached missing samples remain missing.
    """
    return _request('station', start_time, extent, station=station, flags=flags,
                    userid=userid, cache_dir=cache_dir, allow_download=allow_download,
                    api_client=api_client)


def fetch_supermag_indices(start_time, extent=86400, *, flags='SME,SMU,SML,SMR',
                           userid=None, cache_dir=SUPERMAG_CACHE_DIR,
                           allow_download=False, api_client=None):
    """Cache official SuperMAG indices with their source field names, UTC index."""
    return _request('indices', start_time, extent, flags=flags, userid=userid,
                    cache_dir=cache_dir, allow_download=allow_download,
                    api_client=api_client)