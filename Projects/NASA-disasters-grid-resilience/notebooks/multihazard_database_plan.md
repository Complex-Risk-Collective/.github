# Multihazard Database Plan and Task List

This file is the working checkpoint for the MYRIAD-informed multihazard database. Keep it updated whenever a task is completed, a blocker is discovered, or the scientific definition changes. It is intentionally concise so future sessions can recover context without replaying long chats.

Statuses: `todo`, `in_progress`, `blocked`, `done`, `deferred`.

## Two Independent Work Tracks

1. **Physically meaningful multihazard database:** define and validate each hazard's event objects with that hazard's own physical mechanism, source semantics, spatial/temporal support, and literature/engineering evidence. Wind, temperature, precipitation, wildfire, and space weather do not need a common threshold-selection methodology.
2. **Space-weather/grid-risk analysis:** independently test whether local GeoE exposure is associated with observed grid impacts, using Eagle-I no-impact controls and independent magnetometer/storm context where available. This track may inform the space-weather definition, but does not gate terrestrial hazard work or multihazard-database completion.

The Eagle-I/GeoE distribution-shift experiment belongs only to track 2. Its scientific estimand and controls are space-weather-specific. Reuse generic software only where valid; do not propagate its threshold method to other hazards.

## Current Priorities

1. Compare newly staged terrestrial-weather V2 fields against the prior product and event cases before deciding whether V2 fixes known precipitation, wind, or temperature issues.
2. Audit modeled GeoE source-quality/pulse behavior and its effect on detected events; start with the July 8 source issue, then use Gannon storm and curated-storm data when accessible.
3. Replace fixed-exceedance percentile hazard definitions with physically meaningful definitions and validate individual-hazard event objects.
4. Preserve pairwise evidence separately from multihazard groups.
5. Use impact observations as one evaluation source, not a required or sufficient validation target. A positive Eagle-I/GeoE shift is not a database readiness requirement.

Standalone V2 case-comparison workspace: [terrestrial_hazard_v2_validation.ipynb](terrestrial_hazard_v2_validation.ipynb). The MYRIAD notebook has exploratory case-window diagnostics and stored California atmospheric-river/Southern Illinois results, but those use the older product and include a failed optional diagnostic execution; stored detections are not V2 ground truth.

## Multihazard Database Tasks

| Status | Task | Notes |
| --- | --- | --- |
| done | Document database purpose and MYRIAD adaptations | See `notebooks/decisions.md`. |
| done | Exclude unavailable/broken hazards from first production scope | Lightning unavailable; hail/MESH remains proxy/limited; drought proxy degenerate. |
| done | Fix GeoE fill-value masking | Mask `_FillValue`, nonfinite, and `abs(value) > 1e20` before interpolation/magnitude/thresholds. |
| done | Fix GeoE gridding artifact | Use linear Delaunay interpolation, nearest fallback only within 100 km, NaN outside support. |
| done | Generate two-year interpolated GeoE cache | Monthly NetCDFs in `/Users/ryanmc/Documents/NASA_JPL/Projects/NaturalHazards/NASA ROSES Disasters 2025-2027/data/space_weather/processed_data/`. |
| in_progress | Determine what terrestrial-weather V2 changes/fixes | V2 has explicit units, time-window/statistic documentation, valid-pixel fractions and NaN-vs-zero semantics. A May 10 one-day old/V2 comparison is diagnostic only: same `t2m` agrees, but precipitation and gust differ materially; validate each channel against independent cases before claiming a fix. V2 holdings currently cover 2024-05-01–2024-09-01 (111 days), not the full earlier validation calendar. |
| in_progress | Compare V2 candidates with documented case studies | Standalone [terrestrial_hazard_v2_validation.ipynb](terrestrial_hazard_v2_validation.ipynb) indexes embedded MYRIAD diagnostics, records V2 day coverage, runs a guarded May 10 old/V2 comparison, and has opt-in candidate detections. No independent known-event reference table is attached yet; do not report precision/recall. |
| todo | Define absolute/operational space-weather threshold candidates | Use Eagle-I matching experiment below; avoid per-cell p95 as production severity definition. |
| todo | Revisit extreme-precip definition | Prefer wet-hour-conditioned percentile plus absolute mm/h floor; verify over 2024-2025. Does not depend on the Eagle-I/GeoE space-weather work; proceeds independently using this hazard's own physically-grounded criterion. |
| todo | Revisit extreme-wind definition | Prefer absolute or hybrid gust threshold tied to damaging-wind categories and 50 km areal-mean semantics. Does not depend on the Eagle-I/GeoE space-weather work; proceeds independently using this hazard's own physically-grounded criterion. |
| todo | Revisit extreme-heat/cold definitions | Use longer climatology/month conditioning and duration requirements; avoid fixed exceedance-rate interpretation. Does not depend on the Eagle-I/GeoE space-weather work; proceeds independently using this hazard's own physically-grounded criterion. |
| todo | Run event-object validation by hazard | Stratify short/typical/long/broad/high-intensity events; review physical coherence. |
| todo | Rebuild full 2024-2025 event database after threshold revisions | Preserve source availability, threshold version, event-definition version, and lag-matrix version. |
| todo | Compare strict shared-cell vs adjacency-halo pair linking | Keep pair evidence separate; do not treat giant connected components as physical events by default. |
| todo | Add group evidence diagnostics | Duration, direct-link density, bridge events, shortest paths, spatial dispersion, event degree, compressed hazard runs. |
| todo | Produce frequency and hotspot products only after validation | Distinguish event count, occupied-cell recurrence, exposure area, and valid-observation time. |

## Space-Weather Threshold Experiment

**Research framing (2026-10-29):** Eagle-I impact/no-impact comparisons are not a dependable stand-alone detector of space-weather events. The 23 supported county-local matched sets showed weak/non-unique outage correspondence and are descriptive, clustered evidence; this neither settles the space-weather hazard definition nor gates the multihazard database. Prefer physical/model-quality and independently curated storm validation. See [decisions.md](decisions.md) for the V2 and case-window inventory.

**Latest ground-context checkpoint (2026-10-19):** bounded SuperMAG/OMNI checks
are recorded in [impact_ground_context_20261019.md](impact_ground_context_20261019.md).
R01 July data are complete; nearby M03/R03 May series are unavailable. Fixed
PIN/BOU regional comparisons confirm variability in impact and control windows
but are too distant for local validation. July 8's extreme pulses are confirmed
in the original modeled Ex/Ey and independently reconstructed from those points;
they were not created by our interpolation. Their physical/model validity remains
open because the nearest input observatory is distant and input-list changes near
the pulses are unexplained. See the open decision in [decisions.md](decisions.md)
and the linked checkpoint. Older statements below about
uncached station data are historical.

**Original-source check (2026-10-19):** both July 8 pulses are present in original
NOAA modeled Ex/Ey; reconstruction matches all four county cells within
0.000575 mV/km, ruling out our spatial interpolation as their origin. Source
point 2177 (42 N, 92 W) reaches 2048.52 and 15383.61 mV/km at 18:34:30 and
19:06:30. Its recorded observatory distance is 1069 km, distinct from distance
to the modeled point. VIC input availability changes near both pulses; Quality=3
has unspecified semantics. Developer source/model QA remains open; see the linked
checkpoint for metadata and saved comparison plot. Do not remove controls based
on exposure amplitude or infer artifact causation from input-list transitions.

Goal: identify physically meaningful `space_weather_extreme` threshold candidates by comparing GeoE episode descriptors during Eagle-I impact episodes against matched quiet periods.

This is also a standalone question about grid risk, independent of its database role: whether and how GIC-relevant electric-field exposure associates with Eagle-I grid-impact episodes is worth reporting on its own merits even if it never yields a usable production threshold.

**Scope correction (2026-10-12):** this distributional-shift methodology -- matched quiet-period construction, descriptor extraction, bootstrap log-exceedance-ratio scoring -- was built to answer a space-weather-specific question (does GIC-relevant electric-field exposure associate with Eagle-I impact episodes) and depends on inputs (GeoE cache, geomagnetic-latitude/storm-calendar context) that are specific to that hazard. It is not a general-purpose hazard-definition template. The extreme wind, temperature, and precipitation threshold-revision tasks in the main task table above do not inherit this methodology; each uses its own physically-grounded, hazard-specific criterion (per `copilot.md`'s hazard-specific-transform principle) and proceeds independently of how this experiment resolves.

### Principle

Use original NOAA GeoE files as the source product and the interpolated/support-limited analysis-grid GeoE as the derived exposure product. Impact/quiet separation can evaluate impact relevance, but cannot alone establish a physical hazard definition. A null Eagle-I result must not exclude genuine space-weather hazards or indefinitely block production.

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
| in_progress | Audit terrestrial-weather V2 against old product and known cases | Start with the shared May 10 file and complete V2 windows. Compare source/version, units, cadence/window semantics, valid coverage, matched-pixel differences, and event labels by hazard. Do not infer a fix from improved metadata or one-day agreement. |
| done | Build GeoE processed cache | Monthly interpolated, fill-masked, no-extrapolated NetCDFs. |
| in_progress | Investigate July 8 modeled GeoE source pulse validity | Original NOAA source contains both Iowa County pulses; cache interpolation reproduces them. Review source quality semantics, model/input station transitions, support distance, and model documentation. No amplitude-only filtering or control removal; any objective rule must apply equally to impact and controls. See `decisions.md` and [impact_ground_context_20261019.md](impact_ground_context_20261019.md). |
| todo | Audit GeoE coverage-gap pulse candidates and event impact | Inventory source/cell quality and observatory-distance/input-roster transitions across the continuous archive; quantify pulse amplitude, duration, spatial footprint, and whether current event rules label them. Test any proposed objective quality flags against all days and the curated storm benchmark; report event counts/characteristics before and after without deleting raw values. Distance or a station mismatch alone is not a rejection rule. |
| in_progress | Validate space-weather event behavior on Gannon storm | Use May 10–20, 2024 as a named candidate interval, but verify boundaries against the curated 55-storm list or an authoritative event source. Existing V2 weather holdings cover May 10–18 only (May 19–20 missing); derived GeoE and old weather products cover the interval. Use consistent UTC, inspect source-point coverage/quality and event duration/footprint, not Eagle-I alone. |
| blocked | Compare continuous GeoE with curated 55-storm benchmark | User reports the 55 events were more carefully treated for magnetometer-coverage artifacts; benchmark database currently inaccessible. Resume direct event-to-event comparison when available; do not infer its rules or labels in advance. |
| done | Build Eagle-I county-to-grid mapping | Five-character FIPS; polygon-intersection mapping; CT legacy FIPS recovered with 2021 Census; unmapped list retained. |
| done | Pilot Eagle-I impact episodes for 2024-05-10 to 2024-05-12 | Re-run using full-period county thresholds; output looked sensible as a mechanics/science check. |
| done | Build full-period Eagle-I county panel cache | Reusable panel lives in `/Users/ryanmc/Documents/Conferences/Jack_Eddy_Symposium_2022/dev/outage_data/EAGLE-I/processed/`. |
| done | Build full-period county threshold table | Full-period threshold CSV exists; candidate thresholds include p99/p99.5 customers out, p99/p99.5 percent out, absolute floors, severity tiers. |
| done | Re-run May 10-12 pilot using full-period outage thresholds | Threshold-estimation period is now separated from event-detection period. |
| done | Select Eagle-I impact episode definition | `county_p99` production default (county-specific p99 customers-out/percent-out, floored at 50/0.5%, OR logic; 1h gap-bridging with override-suppression exception; 2-sample persistence; 30min minimum duration). Full 2024-2025 regen (2026-09-22) passed all sanity checks: 37,950 episodes, 2,999 counties, median duration 1.5h. See `decisions.md` for the full definition and the `eaglei_full_period_regen_checklist.md` for the regen/QA record. A follow-up parameter sweep confirmed `quantile_level` dominates episode count (195,103 at 0.95 down to ~20,105 at 0.995) while absolute floors have little effect. |
| in_progress | Construct validated matched no-impact control windows | These are Eagle-I outage-quiet controls, not geomagnetically quiet intervals; do not select on GeoE or magnetometer activity. V1 checked starts only. V2 validates every inclusive county slot, both outage channels and whole-window exclusion. The 24/50 target yield is adequate only for a pipeline mechanics check, not for inference; diagnose the 26 unmatched targets before broadening. |
| in_progress | Compute county-local GeoE descriptors | V1 used whole-CONUS exposure per county window. V2 uses existing intersection areas, explicit county/domain and finite-data support, fixed-cell changes, and manifest/window-key resume. Bounded pilot: 48/50 impacts and 47/48 controls had complete time support; only 23/50 targets had both a supported impact and control. Descriptive pilot only; no score. |
| todo | Test lead/lag windows | Start with `-6h/+12h`, `-12h/+24h`, and `-24h/+24h`; account for Eagle-I reporting latency. |
| in_progress | Evaluate space-weather/grid-risk association | Historical v1 result (`log_ratio=-0.132`, CI `[-0.292,+0.014]`) used whole-CONUS exposure and is not a county-local test. V2 paired pilot is descriptive only (23 supported matched episodes); do not select thresholds from it or infer causality. Independent geomagnetic cross-check and improved sample/support design remain. |
| in_progress | Robustness checks | First validate county-local support and quiet eligibility. Then equal-weight matched sets and account for independent storm/date dependence; historical independent-row bootstrap/scoring is paused for v2. Pre-specify a small descriptor/lag/region set and held-out test rather than search until significant. |
| in_progress | Pilot yield and independent geomagnetic checks | 24/50 targets (48%) matched and 23 had supported impact plus >=1 supported control: mechanics only, not inference. Of 26 without controls, 16 had all calendar candidates removed by the 2-day same-county impact buffer; 10 retained candidates but every interval had missing 15-minute Eagle-I panel slots. No target remained unmatched despite an eligible control under exact matcher rules. Cross-check county 19095 at 2024-07-08 18:34 UTC and county 19033 impact/controls May 1/8/15/22/29 2025 against nearby ground magnetometers/storm indices; OMNI is upstream context only. Local station registry identifies Missouri (~309 km from 19095) and Worthington (~203 km from 19033), with Minneapolis/Hennepin (~209 km) as another candidate. No station time series cached. Request a small sanitized UTC station export/code; do not use embedded credentials. |
| todo | Propose production `space_weather_extreme` definition | Absolute mV/km threshold plus persistence, area/coherence rule, support rule, and versioned provenance. |

### Checkpoint — 2026-10-12

- Implemented narrow v2 helpers in [geoe_eaglei_windows.py](../data_processing/geoe_eaglei_windows.py); [13 synthetic regression tests](../tests/test_geoe_eaglei_windows.py) pass in `spwxr_network_new` (terminal and isolated notebook test cell). All 30 notebook code cells parse. No raw inputs or existing external caches rebuilt/overwritten.
- Quiet matching and descriptor extraction remain disabled by default. On 2026-10-12 the bounded 50-impact/five-control mechanics pilot ran from existing caches only, selecting from one county-sorted Parquet row-group interval (32 counties; deliberately not a national/statistical sample to limit panel reads). No raw data/cache rebuild or download occurred. Dated outputs: Eagle-I `eaglei_matched_quiet_periods_2024_2025_county_local_v2_pilot_20261012.csv`; GeoE `eaglei_impact_geoe_county_local_v2_pilot_20261012.csv` and `eaglei_quiet_geoe_county_local_v2_pilot_20261012.csv`, with manifest sidecars.
- Pilot results: 48 quiet controls across 24/50 targets; 48/50 impact windows and 47/48 quiet windows had complete one-minute GeoE support. The other two impact windows and one quiet window were explicitly `incomplete_time_coverage`, not filled. Twenty-three target episodes had both an `ok` impact descriptor and at least one `ok` quiet descriptor. All reported mapped-area coverage was effectively 100% for the sampled counties. No impact-shift score was calculated.
- Interpretation clarification: “quiet” means no qualifying Eagle-I impact in that county/window, not low solar-wind, geomagnetic, GeoE, or magnetometer activity. The user reports that the 2025-05-15 20:15 county impact episode aligns with ground-magnetometer disturbances, while several matched no-impact controls (including 2025-05-01) also have geomagnetic disturbance. This is compatible with controls containing hazard exposure by design; it may reduce impact-vs-control contrast and motivates comparing local county GeoE descriptors and independently defined storm episodes across all matched dates. Magnetometer comparison and local exposure review remain user-observed/preliminary; no causal conclusion or shift score has been established.
- Paired local-exposure review (descriptive only; 2026-10-12): correct one-to-many joins yielded 23 impact episodes with a supported impact window and >=1 supported no-impact control. For impact `e_mag_max`, 15/23 episodes exceeded their own supported-control median and 8/23 were lower; median episode-level difference was +1.94 mV/km and median ratio 1.20. `e_mag_p95` was likewise higher for 15/23, `e_mag_mean` higher for 13/23, fixed-cell magnitude-change max higher for 14/23, and simultaneous vector-change max higher for 16/23. These are descriptive comparisons of a small, region/date-clustered, nonrepresentative pilot; no p-value, confidence interval, threshold selection, or causal claim is warranted. Most `integrated_exposure`/active-area values are zero because the exploratory 100 mV/km placeholder exceeds most local fields; do not interpret that descriptor yet.
- Specific 2025-05-15 20:15 episode in county 19033: impact-window E-mag max 18.24 mV/km; four supported controls had maxima 36.53 (May 1), 11.77 (May 8), 17.53 (May 22), and 72.29 (May 29), with control median 27.03. This is consistent with the user's magnetometer observation: geomagnetic activity occurs in both impact and no-impact periods; occurrence alone does not discriminate Eagle-I impact.
- Highest supported quiet-window E-mag max in this pilot: 2,038.18 mV/km in county 19095 on 2024-07-08 18:34:30 UTC. All four mapped county cells were elevated simultaneously (approximately 1,126–2,038 mV/km); the peak cell carried 25.1% of area weight, had linear-interpolation support, and was 3.67 km from its nearest modeled source point. This is not an isolated single-cell spike in the mapped county, but needs independent storm/magnetometer cross-check before interpretation. The `nearest_fallback_support` mask indicates distance eligibility, not proof that nearest fallback supplied the value.
- Cache audit: all 24 monthly GeoE files exist; median interval 60 seconds, but 1,258 within-file intervals exceed 60 seconds, maximum 376,500 seconds. Ex/Ey/E_mag variable `units` attrs are absent in the derived cache; source units mV/km were verified by the user. The local helper rejects windows containing any internal cadence gap and keeps mapping/support failures explicit.
- Provisional support gate is 90%; nearest/degenerate mapping fallbacks are explicitly unsupported. Land-area weighting is not customer/network weighting. GeoE intervals integrate between observed samples; no fabricated contribution is assigned over gaps.
- Next: cross-check the 2024-07-08 high-exposure control and the May 15/May 1/May 29 local GeoE windows against independent ground magnetometers or storm indices; then decide whether to broaden a pre-specified, stratified pilot. Do not treat the present clustered sample as scientific shift evidence. Review whether quiet-match yield (24/50 targets) is adequate before broadening. Add unit attrs in a future derived-cache version rather than rewriting the existing cache.
- Control-yield decision: 24/50 (48%) is sufficient to show the v2 matcher can generate valid controls, but insufficient for estimating a stable impact/control association. There are only 23 supported parent episodes with at least one supported control in this convenience sample; controls per episode vary from one to five, and target episodes came from one county-sorted Parquet row-group interval. Do not broaden merely to increase the count: first classify unmatched-target reasons (no eligible same-calendar window, outage/coverage failure, or exclusion overlap), then design a broader pre-specified sample. Never require magnetically quiet controls.
- Ex/Ey line-integrated electric field is the relevant GIC forcing; fixed-cell vector/magnitude changes are complementary descriptors, not universally more direct drivers. Active fraction is not spatial coherence.
- Remaining blockers: real-data pilot QA, weather/antecedent-outage matching, equal-weight/storm-aware inference, independent validation and production group semantics. The negative v1 national result remains historical, not a local null finding.

## Update Rules

- Update this file before ending a work session if any task status changed.
- Add new tasks only when they change the actual path to a validated database or threshold definition.
- Keep stale or failed paths as short notes when they explain future choices.
- Do not mark a science task `done` because code ran; mark it `done` only when the output has been sanity-checked and its limitations are documented.
