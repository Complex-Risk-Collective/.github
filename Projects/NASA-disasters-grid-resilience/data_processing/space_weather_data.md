# Reusable OMNI and SuperMAG access

These modules extract the supplied EZIE notebook workflows into ordinary Python
functions. Imports never fetch data. **All fetch functions default to cache-only**;
missing inputs raise `FileNotFoundError`. No full-year download or rebuild is needed.

## Environment and imports

Use the existing `spwxr_network_new` environment: pandas, NumPy, cdflib, requests,
and the official `supermag_api` package were already installed and verified.
The official functions live in `supermag_api.supermag_api`, not the package's
empty top-level initializer. SunPy is not required.

Import from the project root with `data_processing.omni_data` and
`data_processing.supermag_data`. In a notebook launched inside `notebooks`, add
the project root to Python's module search path first. These are standalone source
modules, not additions to the unrelated installed xrgrib backend package; they can
also be copied together into another research project.

## OMNI

`fetch_omni_hro2_1min(start_time, end_time, cache_dir=OMNI_CACHE_DIR,
allow_download=False)` returns a DataFrame with UTC `time` and:

| Column | Physical quantity |
| --- | --- |
| `Bz_GSM_nT`, `By_GSM_nT` | GSM IMF components, nT |
| `FlowSpeed_km_s` | Solar-wind bulk speed, km/s |
| `ProtonDensity_n_cc` | Proton number density, cm^-3 |
| `DynamicPressure_nPa` | Dynamic pressure, nPa |
| `AE_nT`, `AL_nT`, `AU_nT`, `SymH_nT` | Geomagnetic indices, nT |

- Default root: /Users/ryanmc/sunpy/data, including nested holdings.
- Start/end are **inclusive**, matching the original notebook. Naive inputs mean
  UTC; timezone-aware inputs are converted to UTC.
- Existing highest numeric local CDF version is reused, without checking online
  for updates. Corrupt local files raise; they are not silently overwritten.
- With explicit `allow_download=True`, missing months are discovered from the
  SPDF directory, downloaded to a temporary file, parsed, then published atomically.
  Directory listing and download paths are implemented but not live-network tested.
- `read_omni_cdf(filepath)` reads a single local file directly.
- CDF `FILLVAL` (with float32 precision tolerance), nonfinite values and negative
  speed/density/pressure/AE become NaN. Nominal `VALIDMAX`/`VALIDMIN` do not
  arbitrarily clip extreme storms. Missing variables remain NaN and are listed
  in source metadata. Source units/variable names/files live in `.attrs`.
- `field_limits={'Bz_GSM_nT': (-500, 500)}` can explicitly apply inclusive
  scientific bounds; each value is `(lower, upper)`, with `None` disabling a side.
  The reference script's blanket amplitude cutoffs and generic fill lists are
  not silently imposed: for example, 99.99 nPa is not necessarily a pressure
  fill in this CDF product. Variable-specific CDF metadata determines sentinels.
- Fetch results always include the complete requested UTC minute grid. Absent
  records, missing variables and masked observations stay NaN, never zero-filled.
- `freq='5min'` optionally returns available-sample means in fixed UTC
  epoch-aligned, left-labelled bins. All-missing bins remain NaN. pandas `mean`
  already has this behavior; `min_count` is not a supported argument to `mean`.
  Labels must fall within requested bounds; boundary bins can be partial.
- `return_coverage=True` returns `(frame, coverage)` without changing the default
  DataFrame-only API. Reports include `n_valid`, `n_missing`, `n_total`,
  `pct_valid` for output bins **and** `_native` versions counting all requested
  one-minute slots. Reports also live in `.attrs['coverage']`. A valid averaged
  bin does not imply that all source minutes were valid. Empty intervals have
  undefined (NaN) coverage percentages, not artificial complete coverage.
- `omni_coverage(frame)` summarizes finite output values directly.
- `fetch_omni_for_days(days, freq='5min', return_coverage=True)` returns
  `(frame, per_day_coverage)` for only the selected normalized UTC days. It
  deduplicates dates, includes the entire final five-minute bin, and does not
  fetch months between distant dates. Default return is still just the frame.
  This replaces the reference day's helper concept, but retains this module's
  physical column names and `time` column rather than its four-item SunPy tuple.
- For `fetch_omni_hro2_1min`, missing monthly files still raise by default;
  explicitly select `missing_files='nan'` to retain missing months as NaN.
  The selected-day helper defaults to that NaN policy and records absent months
  in per-day provenance. Neither helper conceals corrupt files or network errors.
- No interpolation, gap filling, storm classification, or time-lag selection.
  Monthly OMNI includes upstream processing/propagation; retain product provenance.

## SuperMAG manual text exports

`read_supermag_file(filepath, fill_value=999999.0, stations=None)` returns a
UTC-aware `(timestamp, station)` MultiIndex DataFrame.

- Supports six timestamp fields followed by station count, or the real reference
  export's **cadence in seconds then station count**. The original notebook had
  treated cadence as station count, potentially skipping stations.
- Eleven values per station: `geo_lon`, `geo_lat`, `MLT`, `mag_colat`,
  `solar_zenith`, `N`, `E`, `Z`, `geox`, `geoy`, `geoz`.
- `N/E/Z` are local magnetic north/east/down. `geox/geoy/geoz` are geographic
  components. They are not interchangeable; magnetic fields are in nT.
- Optional `stations=['R01', 'M03']` reduces retained rows, not validation of the
  source blocks. Missing requested stations yield no rows, not fabricated data.
- Retains source header/acknowledgements, baseline description and supplied cadence
  in `.attrs`. Malformed/truncated blocks and duplicate identities raise errors.
- Honors the file's exact fill sentinel, without arbitrary amplitude rejection.

## SuperMAG official API

`fetch_supermag_station(station, start_time, extent=86400, flags='all,baseline=yearly',
userid=None, cache_dir=SUPERMAG_CACHE_DIR, allow_download=False)`

`fetch_supermag_indices(start_time, extent=86400, flags='SME,SMU,SML,SMR',
userid=None, cache_dir=SUPERMAG_CACHE_DIR, allow_download=False)`

- Default root: /Users/ryanmc/Documents/NASA_JPL/Projects/NaturalHazards/NASA ROSES Disasters 2025-2027/data/SuperMAG_data.
- Supply the registered access ID through the local `SUPERMAG_USERID` environment
  variable or `userid`. IDs are not written into cache manifests or filenames.
- Requests start on whole UTC minutes; `extent` is whole minutes in seconds,
  at most one day. Bounds are **half-open** `[start, start + extent)`.
  Call per day/station to bound downloads and permit cheap resumptions.
- Station baseline must be explicitly `baseline=yearly` or `baseline=none`.
  Default is yearly, a deliberate choice different from the original implicit
  `all` request. Do not assume manual baseline `All` equals API yearly baseline.
- **No `delta=start` by default**: independently requested days are not each zeroed.
  If deliberately included, it becomes part of cache identity/provenance.
- `all` supplies metadata, not a cadence/quality guarantee. Retain `ext` and other
  returned scalar quality fields; inspect actual cadence and finite coverage.
- Raw JSON responses and request manifests are cached together under `api_cache/`;
  keys include station, start, extent, flags and schema version. Empty, failed,
  out-of-bounds or structurally invalid responses are not cached as good data.
- API nested `N/E/Z.nez` map to `N/E/Z`; nested `.geo` map to geographic columns.
  Missing coordinates stay NaN; never substitute another frame. Raw nested index
  metadata remains in the cache even when it is not a scalar DataFrame column.
- Failed client exceptions are summarized without embedding a request URL.
  Live service availability, historical station coverage, and access authorization
  are **not yet verified**. The underlying official client controls transport,
  timeout and retry behavior; this wrapper does not promise automatic retries.
- Follow SuperMAG/provider acknowledgement and use requirements for all products.

The R01 helper preserves `(combined, failures)` and its selected dates. Its
`date` alias now refers to normalized UTC `timestamp`; magnetic columns are flat
numeric values rather than nested dictionaries. Failed days record exception
categories. An explicitly empty date list does not revert to default dates.
The CLI is cache-only unless `--download` is explicitly supplied.

## Validation and next checkpoint

- 22 offline regression tests cover request caching, baseline separation,
  coordinate frames, fills, time bounds, malformed ASCII, R01 compatibility,
  explicit missing minutes, resampling, missing days and coverage denominators.
- Real-cache smoke tests: July 8, 2024 18:00–19:15 UTC (76 OMNI rows) and
  May 15, 2025 20:00–21:30 UTC (91 OMNI rows).
- Real manual export: all 2,880 ABK minute records for May 9–10, 2024 parsed;
  that particular export does not include R01/M03/R03.
- Missing-data check on cached full days: July 8, 2024 solar-wind speed has
  1,063/1,440 valid minutes (73.82%) but 252/288 valid five-minute means (87.50%);
  May 15, 2025 has 871/1,440 minutes (60.49%) versus 237/288 bins (82.29%).
  This is why the native-minute coverage is reported alongside resampled coverage.
- **No network downloads have been started.** Next bounded acquisition can be
  R01 on July 8, 2024, followed by the selected May dates/stations, after choosing
  the baseline and supplying the access ID locally. No broad sweep is needed.

Acquisition validation is not yet a scientific verdict on the extreme GeoE
windows. Ground stations may share inputs with the NOAA electric-field model;
global indices alone cannot rule out local disturbance. Quiet controls remain
outage-quiet, not geomagnetically quiet: do not filter them using these drivers.