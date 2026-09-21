# Multihazard Database Plan and Task List

This file is the working checkpoint for the MYRIAD-informed multihazard database. Keep it updated whenever a task is completed, a blocker is discovered, or the scientific definition changes. It is intentionally concise so future sessions can recover context without replaying long chats.

Statuses: `todo`, `in_progress`, `blocked`, `done`, `deferred`.

## Current Priorities

1. Stabilize hazard source preprocessing and provenance.
2. Replace fixed-exceedance percentile hazard definitions with physically meaningful definitions.
3. Generate validated individual-hazard event objects.
4. Preserve pairwise evidence separately from multihazard groups.
5. Use impact observations to calibrate or evaluate hazard thresholds where possible.

## Multihazard Database Tasks

| Status | Task | Notes |
| --- | --- | --- |
| done | Document database purpose and MYRIAD adaptations | See `notebooks/decisions.md`. |
| done | Exclude unavailable/broken hazards from first production scope | Lightning unavailable; hail/MESH remains proxy/limited; drought proxy degenerate. |
| done | Fix GeoE fill-value masking | Mask `_FillValue`, nonfinite, and `abs(value) > 1e20` before interpolation/magnitude/thresholds. |
| done | Fix GeoE gridding artifact | Use linear Delaunay interpolation, nearest fallback only within 100 km, NaN outside support. |
| done | Generate two-year interpolated GeoE cache | Monthly NetCDFs in `/Users/ryanmc/Documents/NASA_JPL/Projects/NaturalHazards/NASA ROSES Disasters 2025-2027/data/space_weather/processed_data/`. |
| todo | Define absolute/operational space-weather threshold candidates | Use Eagle-I matching experiment below; avoid per-cell p95 as production severity definition. |
| todo | Revisit extreme-precip definition | Prefer wet-hour-conditioned percentile plus absolute mm/h floor; verify over 2024-2025. |
| todo | Revisit extreme-wind definition | Prefer absolute or hybrid gust threshold tied to damaging-wind categories and 50 km areal-mean semantics. |
| todo | Revisit extreme-heat/cold definitions | Use longer climatology/month conditioning and duration requirements; avoid fixed exceedance-rate interpretation. |
| todo | Run event-object validation by hazard | Stratify short/typical/long/broad/high-intensity events; review physical coherence. |
| todo | Rebuild full 2024-2025 event database after threshold revisions | Preserve source availability, threshold version, event-definition version, and lag-matrix version. |
| todo | Compare strict shared-cell vs adjacency-halo pair linking | Keep pair evidence separate; do not treat giant connected components as physical events by default. |
| todo | Add group evidence diagnostics | Duration, direct-link density, bridge events, shortest paths, spatial dispersion, event degree, compressed hazard runs. |
| todo | Produce frequency and hotspot products only after validation | Distinguish event count, occupied-cell recurrence, exposure area, and valid-observation time. |

## Space-Weather Threshold Experiment

Goal: identify physically meaningful `space_weather_extreme` threshold candidates by comparing GeoE episode descriptors during Eagle-I impact episodes against matched quiet periods.

### Principle

Use original NOAA GeoE files as the source product and the interpolated/no-extrapolated analysis-grid GeoE as the derived exposure product. Thresholds should be derived from distributional separation between impact episodes and matched quiet periods, not from per-cell GeoE percentiles alone.

### Inputs and Outputs

| Item | Path or Source | Status |
| --- | --- | --- |
| GeoE source files | `/Users/ryanmc/Documents/NASA_JPL/Projects/NaturalHazards/NASA ROSES Disasters 2025-2027/data/space_weather/NOAA_geoE/` | done |
| GeoE processed cache | `/Users/ryanmc/Documents/NASA_JPL/Projects/NaturalHazards/NASA ROSES Disasters 2025-2027/data/space_weather/processed_data/` | done |
| Eagle-I source files | `/Users/ryanmc/Documents/Conferences/Jack_Eddy_Symposium_2022/dev/outage_data/EAGLE-I/` | done |
| Eagle-I general processed mapping | `/Users/ryanmc/Documents/Conferences/Jack_Eddy_Symposium_2022/dev/outage_data/EAGLE-I/processed/` | done |
| Space-weather-specific analysis outputs | `/Users/ryanmc/Documents/NASA_JPL/Projects/NaturalHazards/NASA ROSES Disasters 2025-2027/data/space_weather/processed_data/` | in_progress |
| Working notebook | `notebooks/geoe_eaglei_threshold_analysis.ipynb` | in_progress |

### Experiment Task List

| Status | Task | Notes |
| --- | --- | --- |
| done | Build GeoE processed cache | Monthly interpolated, fill-masked, no-extrapolated NetCDFs. |
| done | Build Eagle-I county-to-grid mapping | Five-character FIPS; polygon-intersection mapping; CT legacy FIPS recovered with 2021 Census; unmapped list retained. |
| done | Pilot Eagle-I impact episodes for 2024-05-10 to 2024-05-12 | Re-run using full-period county thresholds; output looked sensible as a mechanics/science check. |
| done | Build full-period Eagle-I county panel cache | Reusable panel lives in `/Users/ryanmc/Documents/Conferences/Jack_Eddy_Symposium_2022/dev/outage_data/EAGLE-I/processed/`. |
| done | Build full-period county threshold table | Full-period threshold CSV exists; candidate thresholds include p99/p99.5 customers out, p99/p99.5 percent out, absolute floors, severity tiers. |
| done | Re-run May 10-12 pilot using full-period outage thresholds | Threshold-estimation period is now separated from event-detection period. |
| in_progress | Select Eagle-I impact episode definition | Current working definition uses county-specific full-period thresholds, short-gap grouping, duration, peak customers out, peak percent out, and integrated customer-outage-hours. Needs full-period QA. |
| in_progress | Construct matched quiet windows | Next step after full-period impact episodes. Match county, month/season, day of week, hour, duration, and later antecedent outage/weather/load if available. Do not match on GeoE. |
| todo | Compute county-window GeoE descriptors | Max/p95/mean `E_mag`, rate of change, persistence, integrated exposure, active-area fraction, support quality. |
| todo | Test lead/lag windows | Start with `-6h/+12h`, `-12h/+24h`, and `-24h/+24h`; account for Eagle-I reporting latency. |
| todo | Estimate threshold candidates | Exceedance curves, impact/quiet log ratios, risk differences, bootstrap uncertainty. |
| todo | Robustness checks | Vary outage threshold, GeoE descriptor, support mask, lead/lag window, region/season, and remove single-event dominance. |
| todo | Propose production `space_weather_extreme` definition | Absolute mV/km threshold plus persistence, area/coherence rule, support rule, and versioned provenance. |

## Update Rules

- Update this file before ending a work session if any task status changed.
- Add new tasks only when they change the actual path to a validated database or threshold definition.
- Keep stale or failed paths as short notes when they explain future choices.
- Do not mark a science task `done` because code ran; mark it `done` only when the output has been sanity-checked and its limitations are documented.
