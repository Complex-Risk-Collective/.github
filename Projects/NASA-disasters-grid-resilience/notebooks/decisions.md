
# Decision Log

> **Original purpose of this file**
>
> This file should capture key decisions made during development, especially decisions that emerge from the multi-hazard, grid-impact, and data-processing experiments.

This is a living record of consequential choices, not a transcript of every experiment. Statuses are **confirmed**, **provisional**, and **open**. A confirmed entry is supported by code, a diagnostic, or an explicit project choice; a provisional entry is useful for current work but still needs validation; an open entry has not been settled. Dates refer to the development history available in the repository and indexed Copilot sessions as of 2026-08-26.

## Scientific Foundation

### Confirmed: two independent research tracks (2026-10-12)

1. **Physically meaningful multihazard database:** define and validate each hazard's event objects using the source variables, physical mechanisms, spatial representation, temporal cadence, and appropriate literature/engineering evidence for that hazard. Precipitation, wind, heat/cold, wildfire, and space weather need not share one threshold-selection method.
2. **Space-weather/grid-risk analysis:** independently evaluate whether GIC-relevant GeoE exposure is associated with observed grid impacts, using Eagle-I no-impact controls and, where available, independent magnetometer and solar-wind/storm context. This track may inform space-weather interpretation but does not gate terrestrial hazard definitions or completion of the multihazard database.

The Eagle-I/GeoE matched distribution-shift method is specific to the second track. Do not treat it as a required template for terrestrial-hazard thresholds. Reusable software components (windowing, provenance, validation utilities) may be reused only where scientifically appropriate; the estimand and hazard definition remain hazard-specific.

### Confirmed: project hazard system

The target system combines space weather, terrestrial weather, and wildfire hazards and evaluates their individual and joint implications for the electric power grid. This follows the project objectives in [README.md](../README.md).

### Confirmed: graph and data-integration direction

The technical direction is a data-driven, graph-based approach that unifies Earth-system data with power-grid structure. The notebooks are the current experimental surface for developing that approach.

### Open: multi-hazard impact score

The project has not settled the weighting, interaction terms, thresholds, or validation target for a joint impact score. A hazard mask, a projected network stress measure, and an observed outage impact should not be conflated while this is unresolved.

## Spatial and Temporal Representation

### Confirmed: CONUS analysis grid

Current CONUS work uses a 50 km projected grid in EPSG:5070, with approximately 25 to 50 degrees north and -125 to -65 degrees longitude. `AnalysisGridTemplate` creates or loads this representation, and `data/analysis_grid_CONUS_50km.nc` is the local repository copy.

### Provisional: common xarray dimensions

The intended common dimensions are `time`, `y`, and `x`. SWDI currently arrives with names such as `ZTIME`, `Y`, and `X` and is renamed in the pipeline; other sources have their own conventions. Every merge should verify dimensions, coordinates, and time semantics rather than relying on a name-only normalization.

### Open: final temporal resolution

Source data include hourly weather, daily wildfire products, event observations, and geoelectric time series. The effective resolution for event identification and network impact analysis is not one universal value yet; it must be chosen per use case and documented with the aggregation rule.

## Hazard Definitions and Inputs

### Open: evaluate terrestrial-weather V2 before declaring source fixes (2026-10-29)

The staged `terrestrial_weather/V2` product is a new processing version, not yet
validated evidence that previous precipitation, wind, or temperature problems are
fixed. It adds explicit units, temporal/statistic semantics, valid-pixel fractions,
and the convention NaN=no coverage versus 0=observed zero. Its data dictionary
documents MRMS hourly maxima over source files and RTMA hourly labels as analyses
valid at HH:00; these channels therefore do not have identical temporal support.
V2 currently has 111 daily files, 2024-05-01 through 2024-09-01, rather than a
complete two-year validation set.

A bounded, exploratory old-versus-V2 comparison for 2024-05-10 found:

| Field comparison | Observation | Interpretation |
| --- | --- | --- |
| `t2m` | Old and V2 values nearly identical (correlation approximately 1) | Encouraging consistency for this field/day; not a temperature-event validation. |
| `MultiSensor_QPE_01H_Pass2_00.00` | Old/V2 correlation approximately 0.44; V2 maximum 89.2 mm vs old 28.1 mm | Material processing/statistic/coverage difference requiring source-pixel QA; no conclusion that either is more correct yet. |
| `i10fg` gust | Correlation approximately 0.88; V2 maximum 44.72 m/s vs old 20.86 m/s | Material tail difference; inspect source resolution/statistic and spatial support before event validation. |
| `wspd10` | Correlation approximately 0.85 | Related but not interchangeable with gust. |
| `MAXSIZE` vs `MESH_Max_60min_00.50` | Old field all-NaN; V2 MESH has finite values | V2 improves availability for this proxy, but product semantics differ; this is not hail-impact validation. |

These are whole-grid comparisons on one date with differing product definitions,
not an equal-input controlled validation. Do not call V2 a fix or threshold upgrade
until shared-source coverage, units/scales, cell statistic, time alignment, missing
pixels, and event labels have been checked across independent case windows.

### Provisional: terrestrial-weather case windows for validation (2026-10-29)

The MYRIAD event-diagnostic section names the following candidate spot-checks:

| Candidate | Dates | V2 daily coverage | Current status |
| --- | --- | --- | --- |
| Billings winter cold | Jan 10–16, 2024 | 0/7 | Previously selected diagnostic window; unavailable in current V2 holdings. |
| California precipitation | Feb 3–8, 2024 | 0/6 | Previously selected diagnostic window; unavailable in current V2 holdings. |
| Illinois convection | Mar 13–15, 2024 | 0/3 | Previously selected diagnostic window; unavailable in current V2 holdings. |
| Gannon storm | May 10–20, 2024 | 9/11 (May 19–20 absent) | Named storm candidate; verify dates and provenance; use May 10–18 for a complete V2 subset, not as the whole storm. |
| Del Rio late-May heat | May 25–30, 2024 | 0/6 | Previously selected diagnostic window; unavailable in current V2 holdings. |
| Northeast Maine June heat | Jun 17–21, 2024 | 5/5 | Candidate for local temperature sanity check; independent validation labels/reference still need documenting. |
| Park Fire onset | Jul 22–29, 2024 | 8/8 | Candidate fire/weather context check; not by itself validation of weather event definitions. |

These were named by exploratory notebook diagnostics, not all independently
validated events. An earlier diagnostic execution failed before completing the
window loop, so treat the case list as a pre-existing review queue, not proof
that the plots/metrics were successfully checked. Attach authoritative event
references, AOIs, and the expected physical signature before calling a case a
validation event. V2 coverage counts are file presence only; inspect variable
coverage and timestamps too.

Further source inspection found a later known-event block in
`myriad_algorithm_multihazard_identification.ipynb` with stored results for a
California atmospheric river (Feb 4–7; precipitation expected, stored peak at
Feb 5 04:00 UTC) and Southern Illinois severe convection (Mar 14; precipitation
and wind expected). Stored results report precipitation detections in both,
while Illinois wind has 0 regional active-cell hours despite a nonzero CONUS
peak. These are legacy-product results, have no attached independent source
citation in the event table, and neither case overlaps currently staged V2 files.
The source cell that saves this result has concatenated statements at its end;
its stored figures/table are evidence of a prior exploratory run, not a clean
reproducible validation pass. The standalone V2 notebook indexes these separately
and must not upgrade their status without attaching/confirming references.

### Open: Gannon storm and continuous GeoE coverage-gap pulse audit (2026-10-29)

The Eagle-I exploratory material labels May 10–20, 2024 as the Gannon storm;
the May 2024 network notebook examines May 8–15. These are candidate analysis
windows, not independently verified official boundaries. The best space-weather
benchmark described by the user is a curated set of 55 carefully treated storms,
but it is currently inaccessible. When available, use it to develop/test
artifact diagnostics, then validate any rule on separate dates; do not tune a
filter solely to the July 8 Iowa case.

For the continuous modeled GeoE series, first characterize candidates associated
with source observatory distance, `nobs`/input-roster changes, and documented
quality flags. Per candidate, quantify peak and percentile |E|, pulse duration,
pre/post recovery, affected cells/area, spatial smoothness/coherence, interpolation
support, nearest-observatory distance, and whether current MYRIAD persistence,
footprint, and gap rules label an event. Report counts and distributions before
and after any predeclared candidate rule, stratified by storm/control periods and
quality/support regimes. Compare against source Ex/Ey and station/index evidence.
The source `Quality=3` semantics are unknown and distance alone does not establish
invalidity; no universal amplitude cutoff, observatory-distance rejection, or
automatic pulse removal is approved. Preserve raw values and missingness, and
apply any future objectively justified exclusion equally to impacts and controls.

The July 8, 2024 Iowa County source/cache reconstruction is recorded in
[impact_ground_context_20261019.md](impact_ground_context_20261019.md). Both
investigated pulses occur in original NOAA modeled Ex/Ey, so our spatial
interpolation did not create them; physical validity of the modelled source
remains open.

### Confirmed: short-duration precipitation variable

`MultiSensor_QPE_01H_Pass2_00.00` is the preferred gauge-corrected one-hour accumulated precipitation variable for short-duration extreme precipitation work. The 24-hour variable remains relevant for a different flood-risk question.

### Confirmed: distinct heatwave and coldwave concepts

Heatwave and coldwave are separate hazard definitions. They should not be collapsed into a single generic temperature-extreme category merely because they share an implementation pattern.

### Provisional: MYRIAD-style hazard choices

Recent MYRIAD work uses event/polygon wildfire inputs, `i10fg` for 10 m wind gust, and daily aggregates for heatwave and coldwave. These choices are the current working configuration, not a final claim that the definitions are optimal.

### Provisional: wildfire source contract

Recent work favors GeoPackage wildfire polygons and an area filter of at least 5 km2, with the event data serving as the wildfire hazard list and polygons supplying spatial representation. The final source, update contract, and handling of overlapping events remain to be formalized.

### Confirmed: production coverage window

The updated input audit found complete wildfire, terrestrial-weather, and space-weather daily coverage for 2024-2025 after the missing space-weather files were added for 2024-06-17 through 2024-08-05 and 2025-02-06 through 2025-02-09. Missing or unusable source fields must still not be converted into zero hazard in future runs.

## Multi-Hazard Database Generation

This section archives the decisions and lessons from the MYRIAD-style multi-hazard database development work. It is deliberately separate from the grid-impact analysis because the database must first establish credible hazard-event objects and their relationships before those objects are used to interpret infrastructure impacts.

### Confirmed: database purpose

The database is intended to improve multi-risk understanding by providing a coherent, spatially explicit record of individual hazards and their interactions. It should support analysis of:

- how often multi-hazard combinations occur;
- which ordered hazard combinations recur;
- where individual hazards and multi-hazard combinations form hotspots;
- how hazard relationships change when physically meaningful time lags are allowed;
- how hazard combinations can later be related to electric-grid exposure, stress, outage, and recovery observations.

The database is not intended to capture every preconditioning process. Slow fuel drying, long-term precipitation deficits, and other background susceptibility mechanisms may be scientifically important, but they should not automatically become multi-hazard event links. The primary product is a multi-hazard event database focused on interacting hazard occurrences, with preconditioning represented separately when it is intentionally modeled.

### Confirmed: substantial adaptation of MYRIAD

The work recreated the core MYRIAD concepts of spatially overlapping hazard events, optional temporal overlap within a lag, ordered hazard groups, and recurring hazard combinations. It also required substantial changes for this project, including:

- hazard-specific temporal cadences;
- hazard-specific thresholds and persistence rules;
- directional pairwise lags;
- event intensity enrichment;
- source-specific time decoding and repair;
- wildfire polygon integration;
- space-weather gridding and operational aggregation;
- event-object, footprint, source-field, and linkage diagnostics.

The resulting system should be described as a MYRIAD-informed database, not as an unchanged implementation of the published algorithm.

### Confirmed: current data sources and roles

The current working source contracts are:

| Hazard or role | Current source | Interpretation |
| --- | --- | --- |
| Wildfire | Filtered wildfire event polygons, currently `final_area_km2 >= 5 km2` | Highest-confidence event objects; polygons provide spatial footprint and dates. |
| Extreme precipitation | MRMS `MultiSensor_QPE_01H_Pass2_00.00` | Gauge-corrected one-hour accumulated precipitation in mm; appropriate for short-duration precipitation detection. |
| Daily temperature hazards | Terrestrial-weather temperature field | Used for separate MYRIAD-style heatwave and coldwave concepts. |
| Sub-daily temperature hazards | Terrestrial-weather temperature field | Used for `extreme_heat` and `extreme_cold` with monthly-conditioned thresholds. |
| Extreme wind | Terrestrial-weather `i10fg` when available | Ten-meter wind-gust field; currently exploratory and requires event-object validation. |
| Hail screening | `MESH_00.50` when valid `MAXSIZE` is unavailable | Maximum estimated hail-size proxy in mm; indirect occurrence/severity indicator, not a direct hail-damage observation. |
| Lightning | NLDN density fields when positive valid observations are available | Current files contain the NLDN variable name but no usable positive signal; lightning is unavailable in the inspected holdings. |
| Convective context | `EchoTop_18_00.50` when available | Echo-top height at 18 dBZ in km; storm-depth and convective-intensity proxy, not a substitute for lightning or hail occurrence. |
| Space weather | Ex/Ey-derived geoelectric magnitude on the analysis grid | Operational five-minute maximum aggregation, per-cell threshold, persistence, and broad-area response. Second-highest current confidence after wildfire. |

MRMS precipitation and NCEI contiguous-U.S. precipitation rankings are fundamentally different products. MRMS is optimized for high-resolution spatial and short-term temporal estimation, while NCEI rankings use long-term climate datasets such as nClimDiv/nClimGrid. Their monthly totals should not be treated as a direct pass/fail validation comparison.

### Confirmed: event identification is the primary readiness gate

The main scientific risk is not file loading or graph construction; it is whether a detected event object corresponds to a meaningful physical event. A false long-lived precipitation, hail, lightning, wind, or temperature object can contaminate event counts, combination frequencies, hotspots, and every downstream lagged-link analysis.

The central review question is:

> Does this look like one physical event, or several systems merged by the algorithm?

Internal event-object QA currently checks timestamp ordering, nonempty label references, configured footprint and active-step filters, source-field availability, and intensity availability. These checks establish structural consistency, not physical truth. Physical confidence requires case-based review using source fields, event masks, maps, movies, and independent event information where available.

### Provisional: confidence hierarchy

The current confidence hierarchy is:

1. **Wildfire:** highest confidence because the event list is polygon-based and independently interpretable.
2. **Space weather:** second-highest confidence because the operational definition and broad-area behavior are explicit, although regridding artifacts and wide-area episodes require review.
3. **MRMS precipitation:** useful for short-term detection, but event segmentation and long-lived regional objects require case-based review.
4. **Temperature and wind:** structurally implemented, but longer-period climatology and physical case validation remain necessary.
5. **Hail and lightning:** currently not production-ready in the available files. `MAXSIZE` is all missing, and NLDN density has no positive valid signal in the inspected inventory. MESH may provide a provisional hail proxy; lightning requires another usable source or a clearly labeled convective proxy.

### Confirmed: separate hazard layers

The database will retain separate layers for individual hazard events, pairwise links, and larger hazard groups. It will also retain separate temporal layers when the physical phenomenon requires them. The discovery and integration of these hazard-specific temporal characteristics are part of the project's contribution; the database should not force precipitation bursts, hail, lightning, wind, temperature extremes, wildfire, and space weather into one universal event duration.

### Confirmed: first production-release scope

The first production release will omit lightning as a detected hazard because the current holdings contain no usable positive lightning-density observations and no validated fallback source. This is a documented data-availability limitation, not a claim that lightning is absent. Convective context may still be represented through available fields such as EchoTop, but it must not be relabeled as lightning occurrence.

### Confirmed: MESH hail proxy role

When valid `MAXSIZE` data are unavailable, `MESH_00.50` will be retained as the hail layer's direct radar-derived proxy: it translates radar reflectivity into estimated maximum hail diameter in millimeters. MESH provides peak estimated size, not hailfall duration, accumulation, or direct damage. Those meanings must remain explicit in event provenance and interpretation.

### Confirmed: hazard-specific temporal concepts

One universal event duration is physically inappropriate. The database should preserve native or near-native event scales and distinguish event layers where needed:

- **Extreme precipitation:** sub-daily bursts of roughly 1-6 hours, daily 24-hour events, and separate multi-day storm-system groupings of roughly 2-5 days. Specialized longer atmospheric-river or subseasonal studies may require a separate layer.
- **Wind:** threshold-dependent duration. Thunderstorm exceedances may be minutes to hours, while synoptic exceedances can persist much longer. Threshold choice must be documented with the event object.
- **Hail:** individual hail episodes are generally brief, roughly 5-15 minutes, with severe supercell tracks potentially lasting 30-60 minutes. Current daily MESH processing is not a physically satisfactory final hail-event definition.
- **Lightning:** individual strikes are effectively instantaneous. Lightning bursts or thunderstorm episodes should be represented separately, with a short aggregation window rather than multi-day daily objects.
- **Heat and cold:** event duration and selectivity require longer-period climatology; the May pilot is not sufficient to validate these definitions.
- **Space weather:** a continent-scale geoelectric response may be one coherent driver episode with a changing footprint, rather than thousands of independent local events. Broad coverage alone is not evidence of a labeling failure.

### Confirmed: intensity indicators are retained

Each detected event can carry `peak_intensity` and `mean_intensity` derived from the hazard's native source field within its labeled footprint. These values remain in native units and should not be compared across hazards without explicit normalization. Intensity is evidence for event review and stratification; it is not yet a universal cross-hazard severity score.

### Provisional: production thresholds should represent severity, not fixed exceedance rates

The first full production run showed that common percentile-based masks for space weather, extreme heat/cold, wind, and precipitation can generate many structurally valid events while obscuring physically important hazard interactions. This is especially clear for space weather: a per-cell percentile threshold fixes an exceedance rate by construction and can make benign local departures appear alongside true geoelectric storms. Space-weather extremes should move toward an absolute or operationally meaningful geoelectric-field threshold in native units, with duration and broad-area response retained as event evidence.

The same principle applies to terrestrial hazards. Percentile thresholds remain useful diagnostics, but production definitions should use physically interpretable criteria or hybrid rules where possible, such as wet-hour-conditioned precipitation percentiles with an absolute mm/h floor, absolute wind-gust thresholds tied to damaging-wind categories, and temperature thresholds conditioned on a longer climatology with duration requirements. The database should preserve the threshold version, source-field version, and event-definition version so sensitivity runs can be compared without overwriting earlier results.

### Provisional: Eagle-I severity tail diagnostics

An exploratory OLS slope fit to the top decile of the empirical CCDF on the 2024-2025 Eagle-I table reported approximate slopes -1.41 (`peak_customers_out`), -1.55 (`duration_hours`), and -0.82 (`integrated_customer_outage_hours`). These fits do not establish power-law asymptotics or infinite population moments, particularly on a two-year, dependent event sample. They motivate robust summaries and storm-block/leave-one-storm-out sensitivity; they do not invalidate sample quantiles or prescribe threshold methods for other hazards.

For Eagle-I-assisted impact evaluation, retain robust summaries and test single-storm influence. Do not use these exploratory slopes as proof that quantiles or means are intrinsically invalid:

- Prefer interpretable impact outcomes and report medians, quantiles, exceedance rates, and robust sensitivity summaries alongside any means.
- Run leave-one-storm/date-block sensitivity before using Eagle-I severity to calibrate space-weather descriptors.
- These are exploratory finite-sample slopes, not tail-index estimates. Do not infer infinite moments from them.

### Confirmed: Eagle-I county-to-analysis-grid mapping

Eagle-I is natively indexed by county FIPS, not the shared 50 km analysis grid. A separate county-to-analysis-grid-cell mapping was built via polygon intersection on five-character FIPS codes, with Connecticut's post-2022 legacy-FIPS transition recovered using 2021 Census county geometry. Counties that cannot be mapped are retained as an explicit unmapped list rather than silently dropped, so downstream cross-hazard comparisons can see what coverage is missing instead of inferring it from a shrinking row count.

### Confirmed: Eagle-I full-period raw panel characteristics

The full 2024-2025 county-by-15-minute panel built by `build_eaglei_county_panel()` (legacy-only source, VI state-name fix applied) contains 58,183,354 panel rows across 3,077 counties, with `is_suppressed_override` exactly 0 everywhere (expected, since neither annual legacy file carries the `Contains Override Data` column) and exactly 53 distinct two-letter state codes (50 states + DC + PR + VI). This is the raw panel the companion impact-episode decision below is built from; note that only 2,999 of these 3,077 panel counties produce a qualifying `county_p99` impact episode in the current definition -- the remaining counties have panel data but no county-threshold-exceeding activity in the 2024-2025 window. See `eaglei_full_period_regen_checklist.md` for the full sanity-check record.

### Confirmed: Eagle-I impact-episode definition (production default)

The full 2024-2025 Eagle-I impact-episode table is built with `identify_eaglei_impact_episodes()` in `notebooks/geoe_eaglei_threshold_analysis.ipynb` (cells 7-8) using the following definition, referred to as `county_p99` mode:

- **Active threshold, per county:** the max of the county's own 99th-percentile historical `customers_out`/`percent_out` and an absolute floor (`min_customers=50`, `min_percent_out=0.5%`). A 15-minute county-time slot is "active" if either the customers-out or the percent-out threshold is cleared (OR logic).
- **Grouping:** consecutive active readings merge into one episode if the gap between them is at most `OUTAGE_EPISODE_MAX_GAP = 1 hour`. A gap is only bridgeable if every intervening panel slot exists and is flagged `is_suppressed_override` (Eagle-I reported but the value was nulled for data-quality reasons); any other kind of gap breaks the episode.
- **Persistence:** an episode survives only if it contains at least `OUTAGE_EPISODE_MIN_CONSECUTIVE_SAMPLES = 2` genuinely back-to-back active readings (not merely readings stitched together by gap-bridging tolerance).
- **Minimum duration:** total episode span must be at least `OUTAGE_EPISODE_MIN_DURATION = 30 minutes`.
- **Data source restriction:** the full-period production run uses only the two top-level annual legacy files (`eaglei_outages_2024.csv`, `eaglei_outages_2025.csv`), deliberately excluding the daily-granular per-day CSVs, to avoid a source-collision bug where the two formats disagreed on values and silently corrupted max-based aggregation (see `eaglei_full_period_regen_checklist.md`). Consequence: `is_suppressed_override` is `False` everywhere in the full production run, since the annual files lack the `Contains Override Data` column; it remains active for the May 2024 pilot window, which does use the daily-granular source.
- **2025 `total_customers` backfill:** the 2025 annual file lacks `total_customers` entirely; it is backfilled per-county from that county's 2024 value, and `percent_out` is recomputed wherever it was missing purely due to this gap.

The 2026-09-22 full-period regen using this definition produced 37,950 impact episodes across 2,999 counties, median duration 1.5 hours, with all sanity checks in the regen checklist passing (Harris County, TX override incident resolved; 53 distinct state codes; zero suppressed-override rows as expected for the legacy-only source).

A subsequent parameter sweep over `quantile_level` (0.95/0.99/0.995), `min_customers` (25/50/100), and `min_percent_out` (0.25/0.5/1.0) confirmed that `quantile_level` dominates episode count (195,103 episodes at 0.95 down to roughly 20,105 at 0.995 across the full national panel), while the absolute floors have comparatively little effect (`n_counties_with_episodes` stayed roughly 2,990-3,015 across all 27 combinations). This is the basis for holding the floors fixed at their production defaults (50 / 0.5) in the GeoE distributional-shift scoring experiment below and varying only `quantile_level` and the gap-bridging window there.

### Confirmed: GeoE shift-scoring pipeline needs episode subsampling at full national scale

The quiet-period-matched GeoE distributional-shift scoring pipeline (`construct_time_matched_quiet_periods()`, `score_combo_geoe_shift()` in `notebooks/geoe_eaglei_threshold_analysis.ipynb` cells 19/24/25) was fixed to (a) return an empty frame with correct columns instead of raising `KeyError` when no quiet-period candidates are found for a short window, and (b) report the true episode count in early-return cases instead of collapsing it to 0, so "no episodes at this threshold" is distinguishable from "episodes exist but no quiet matches were found."

Because the loosest threshold combinations produce up to ~195,000 episodes nationally, and each episode (plus its ~10 quiet matches) requires its own GeoE-cache window extraction, scoring every episode at full scale is not necessary to detect a distributional shift and would be impractically slow. A fixed per-combo subsample cap (`GEOE_SHIFT_MAX_EPISODES_PER_COMBO = 2000`, random-seeded) was added to the run loop in cell 25. This is a sampling choice for estimating the shift statistic and its bootstrap CI, not a per-episode feature table; the cap should be raised or dropped once a specific combo is being studied in detail rather than screened.

### Confirmed: GeoE descriptor extraction now runs against real cache data, with a resumable checkpoint pattern

The descriptor-extraction step (`extract_geoe_window_descriptors_resumable()` in `notebooks/geoe_eaglei_threshold_analysis.ipynb`) now pulls real `summarize_geoe_window()` descriptors from `geoe_cache['E_mag']` for a subsample of impact episodes and their matched quiet periods, rather than the synthetic happy-path test used earlier to validate the scoring formula. The first full run used `GEOE_SHIFT_MAX_EPISODES = 2000`, `GEOE_SHIFT_MAX_QUIET_PER_EPISODE = 5`, seed 13, and was interrupted partway through quiet-period extraction; it was restructured to checkpoint partial results to CSV every 500 rows so an interruption only costs the current checkpoint interval, not the whole pass. The resulting cached descriptors cover 1,985 of the 2,000 sampled episodes and 8,051 of the 8,108 matched quiet periods -- this roughly 1% attrition (presumably windows falling outside GeoE cache time coverage) has not yet been explained or checked for systematic clustering, and should be before the counts are relied on further.

### Open: first real distributional-shift result is null/negative, not yet interpretable as a settled finding

The first bootstrap log-exceedance-ratio score (`bootstrap_log_exceedance_ratio()`) on the real descriptor sample above, using `e_mag_max_mV_km` with the threshold set at the quiet population's own 90th percentile (444.48 mV/km), returned `log_ratio = -0.132` (95% CI `[-0.292, +0.014]`), `p_exceed_impact = 0.088` versus `p_exceed_quiet = 0.101` (n_impact=1,985, n_quiet=8,051). At this descriptor and threshold, impact episodes do not show elevated GeoE exposure relative to matched quiet periods -- if anything the point estimate runs the other way, though the CI reaches zero.

This should not yet be read as evidence that space weather is unrelated to Eagle-I impact episodes. The test pooled national impact episodes and used whole-CONUS exposure, rather than county-local exposure, so it was not the local comparison intended by the experiment. Separately, geoelectric E integrated along transmission lines and network impedance is relevant to GIC; `d_e_dt` and Ex/Ey changes are complementary transient descriptors, not universally more direct drivers. Before this feeds a production space-weather event definition or is reported as a settled null, review local exposure, timing, storm/station context, matching, and support. Do not run an unrestricted threshold sweep on the same sample and treat the best result as validation.

This methodology is specific to the space-weather/grid-risk analysis: it asks whether county-local GeoE exposure differs between Eagle-I impact windows and matched Eagle-I no-impact controls. It is not the threshold-definition method for wind, heat/cold, or precipitation. Those database hazards proceed independently using physically justified hazard-specific definitions and validation.

### Provisional: county-local GeoE and whole-window quiet validation (2026-10-12)

Source inspection showed the first shift descriptors used whole-CONUS GeoE for every county window; the derivative was of the moving spatial maximum, and quiet exclusion tested starts rather than entire windows. The recorded negative score remains a national temporal diagnostic, not evidence of a county-local null relationship.

V2 uses cached county-cell intersection areas, explicit finite/county-domain support, fixed-cell magnitude and simultaneous Ex/Ey vector changes, fully observed below-threshold quiet windows and whole-window same-county episode exclusion. Versioned filenames preserve historical caches; descriptor resume checks manifests and stable window keys rather than row counts. Thirteen synthetic regression cases passed in the working environment. A bounded cache-only pilot on 2026-10-12 used 50 episodes from one county-sorted Parquet row-group interval (32 counties; not a representative sample) and up to five controls each. It produced 48 controls across 24/50 target episodes; complete temporal GeoE support was available for 48/50 impact windows and 47/48 controls. Twenty-three targets had both a supported impact window and at least one supported quiet window. All sampled counties had effectively 100% mapped-area coverage. Three windows crossing temporal gaps were marked unsupported, not filled. No score was calculated; matched-set weighting, storm/date dependence, and confounder adjustment remain pending.

Terminology clarification from pilot review: matched “quiet” periods are **no-impact Eagle-I controls**, not geomagnetically quiet controls. The user reports ground magnetometer disturbances around the 2025-05-15 20:15 impact episode and also on some matched dates, including 2025-05-01. This does not invalidate the controls: they were intentionally not selected on GeoE or magnetic activity. It does mean exposure on controls must be interpreted explicitly, and local county GeoE plus independently defined storm episodes should be reviewed across all matched dates before estimating any contrast. This magnetometer comparison is user-observed and has not been independently audited in this code session; it is not causal evidence by itself.

The 2026-10-12 descriptive paired comparison included 23 parent episodes with supported impact exposure and at least one supported control. The local county-window maximum E_mag exceeded its episode's control median for 15/23 (lower for 8/23; median difference +1.94 mV/km; median ratio 1.20). Corresponding higher counts were 15/23 for E_mag p95, 13/23 for mean E_mag, 14/23 for fixed-cell magnitude-change max, and 16/23 for simultaneous Ex/Ey vector-change max. This small sample is selected from one county-sorted Parquet row-group interval (32 counties), clustered by place/date, and has unequal control counts; values are descriptive only, not an inferential result.

For the user's May 15 county-19033 example, impact maximum was 18.24 mV/km versus control maxima 36.53 (May 1), 11.77 (May 8), 17.53 (May 22), and 72.29 (May 29); the control median was 27.03. Thus geomagnetic activity on an Eagle-I no-impact date is expected and demonstrates why “quiet” must not imply magnetically quiet. The pilot's highest no-impact maximum was 2,038.18 mV/km in county 19095 on 2024-07-08 18:34:30 UTC; all four mapped cells were elevated together, with linear interpolation support at the peak cell (25.1% county-area weight, nearest modeled source 3.67 km away). This merits a cross-check against independent geomagnetic indices/stations; it does not establish an Eagle-I response or causation. The 100 mV/km extraction placeholder makes most active-area/integrated-excess descriptors zero and is not yet useful as an impact threshold.

### Provisional: next low-cost space-weather checks and control-yield interpretation (2026-10-12)

The 24/50 no-impact-control yield is adequate to verify that the matcher produces valid controls, but **not** adequate for an impact-association estimate. Only 23 parent episodes have supported impact exposure and at least one supported control; this pilot is limited to one county-sorted Parquet row-group interval (32 counties), control counts range from one to five, and event dates/counties are clustered. Before broadening, classify no-match reasons (calendar candidates, same-county episode exclusions, outage threshold, panel coverage) and design a geographically and temporally broader sample with predeclared rules. Do not require geomagnetically quiet controls.

Two case checks are prioritized, without treating them as significance tests: (1) county 19095 on 2024-07-08 around 18:34 UTC, especially the 18:15–19:00 local-Eagle-I no-impact control window containing the 2,038 mV/km maximum; (2) county 19033's May 15, 2025 20:15 impact window and its May 1/8/22/29 matched windows. Compare UTC-aligned local GeoE with independent ground magnetic observations (prefer nearby SuperMAG stations or equivalent) and a storm index; OMNI Bz/Vsw/pressure and AE/AL/AU offer useful upstream/global context but do not substitute for local magnetometer data. Workspace search found exploratory OMNI HAPI code and a prior SuperMAG API helper, but no confirmed local magnetometer/index cache. The SuperMAG example has a user-specific access identifier; do not copy it or request secrets in chat. Ask the user for sanitized retrieval code or station/index exports before fetching ground data. No independent-index cross-check has yet been run.

The supplied July-2025 SuperMAG station registry was read successfully with its variable-length operator list preserved. Approximate geographic nearest candidates to area-weighted county centroids: county 19095 (Iowa County, Iowa), Missouri station R01 (~309 km); county 19033 (Cerro Gordo County, Iowa), Worthington M03 (~203 km) and Hennepin R03 (~209 km). These are regional-context stations, not local-in-county measurements; use multiple nearby stations and state distances explicitly. Needed export: UTC timestamp, IAGA station code, geographic coordinates, and 1-minute north/east (and optionally vertical) perturbation components with quality flags. A separate 1- or 5-minute index export (e.g. SME/SMU/SML or AE/AL/AU) is helpful; OMNI Bz GSM, solar-wind speed, and dynamic pressure are optional upstream context. Do not share API credentials. Exact matcher audit explains 26/50 pilot targets without controls: 16 had all same-calendar candidates removed by the two-day same-county impact buffer; 10 retained candidates but each spanned missing Eagle-I 15-minute panel slots. No sampled target had an eligible control that the cached matcher failed to save.

All 24 monthly derived GeoE files exist and have a 60-second median cadence, but inspection found 1,258 within-file intervals longer than 60 seconds (maximum 376,500 seconds). Their Ex/Ey/E_mag variable unit attributes are missing, although source units were confirmed by the user as mV/km. Preserve this provenance in analysis and add unit attrs in a future cache version rather than rewriting current files.

Correction to the earlier interpretation: geoelectric field integrated along lines drives GIC through network impedance; `dE/dt` is an additional transient descriptor, not inherently a more direct driver. The exploratory top-decile OLS CCDF fits also do not establish asymptotic power laws or infinite population moments, and do not invalidate sample quantiles. Robust/storm-block inference remains warranted without those stronger claims.

### Open: space-weather regridding and coverage provenance

The striping in the space-weather fields is treated as a mapping artifact from the modeled geoelectric output grid to the 50 km analysis grid, not as a direct observational feature. Production space-weather processing should interpolate the modeled Ex/Ey field to the analysis grid, mask fill values before computing magnitude or thresholds, and carry a coverage or interpolation-support mask. Until that regridding is validated, spatial hotspot and overlap results involving space weather should be interpreted as provisional.

### Open: July 8, 2024 modeled GeoE source pulse validity (2026-10-19)

In Iowa County, Iowa (FIPS 19095), the original NOAA empirical-EMTF-2022.12
daily product contains modeled Ex/Ey pulses at source grid point 2177 (42.0 N,
92.0 W): |E| = 2,048.52 mV/km at 18:34:30 UTC and 15,383.61 mV/km at 19:06:30.
Independent Delaunay reconstruction reproduces the four county-cell cache values
to within 0.000575 mV/km. Thus the pulses are present before our regridding; this
rules out our spatial interpolation as their origin, but does not establish that
the modeled values represent valid physical ground forcing.

The source records a 1,069 km distance from this modeled point to its nearest
input observatory (distinct from the 3.67 km modeled-point-to-grid-cell distance).
R01 is not listed as an input at either pulse. The input list changes near both
pulses, including VIC's absence at 18:35:30 and 19:07:30; Quality=3 is present,
but its semantics are undocumented in the file. These are investigation leads,
not evidence that the values are invalid or that an input transition caused them.

**No amplitude-based exclusion or control removal is approved.** Keep the source
and derived values, flag the episode for source/model review, and apply any future
objective quality rule identically to impact and no-impact windows. Before
promoting a production threshold or scoring the matched pilot, inspect the
original source time series, source/model quality and scaling semantics, input
station transitions, spatial support, and model documentation. The reproducible
comparison and artifacts are recorded in
[impact_ground_context_20261019.md](impact_ground_context_20261019.md).

### Confirmed: directional lag matrix and spatial-pair rule

Pair linkage requires spatial overlap and allows non-overlapping time when the following event begins within the physically specified directional lag. The current matrix is directional because causal or interaction timescales can differ by order. Examples include:

- lightning to wildfire: short ignition holdover, capped near the observed majority of cases rather than a 7-14 day window;
- wind to wildfire: short concurrent combustion and spread relationship, excluding slower fuel-drying preconditioning;
- heat to drought: days to weeks for heat-led drying, while the reverse direction is not used to represent slow preconditioning;
- cold/extreme cold and wind: approximately 24-48 hours for the shared synoptic episode;
- space weather to terrestrial hazards: approximately 24 hours as a co-occurrence window, not an asserted causal mechanism;
- convective hazards: approximately 24 hours in the exploratory matrix, subject to source-cadence and event-object validation.

The published MYRIAD criterion that two events must overlap spatially but need not overlap directly in time is treated as controlling for the strict sensitivity analysis: shared analysis-grid cells are required there, while temporal overlap is optional within the lag. For the current working database, the one-cell spatial adjacency halo is retained provisionally because it may preserve meaningful links across gridded footprint boundaries. Its effect on large transitive chains will be compared directly with strict shared-cell linking before it is either retained or removed from the production definition.

### Provisional: retain pair evidence separately from hazard groups

The database should retain three distinct products:

1. **Individual hazard events:** the objects whose physical validity is reviewed.
2. **Pairwise links:** every spatially and temporally eligible pair, including hazard order and lag used.
3. **Hazard groups:** larger collections of linked events, ordered by individual-event start time and summarized by recurring combinations.

Unconstrained graph connected components are useful as a candidate-group diagnostic but are not automatically meaningful physical events. The May 2024 pilot produced a component containing hundreds of events across nearly the entire month, even after stricter shared-cell testing. This is transitive closure, not evidence that all events formed one simultaneous compound disaster.

Important chains should not be discarded simply because groups become large. Instead, production outputs should retain the full graph and add evidence fields such as group duration, direct-link density, bridge events, shortest paths, spatial dispersion, event degree, and compressed consecutive same-hazard runs. A later grouping rule can classify components as coherent local groups, diffuse transitive groups, or candidate preconditioning sequences without destroying the underlying pair evidence.

### Provisional: convective-hazard representation

Hail, lightning, precipitation, and wind are correlated manifestations of convective storms. This correlation is expected to appear in the database and should be analyzed statistically rather than removed by excluding same-storm hazards.

The preferred layered representation is:

- native precipitation burst objects from MRMS;
- hail occurrence/severity proxy objects from valid MESH data until `MAXSIZE` becomes usable;
- short lightning-burst objects when a usable NLDN or replacement lightning field is acquired;
- wind-gust objects at source cadence;
- convective context from EchoTop and available lightning-density fields;
- broader storm-system groups as a separate derived layer.

This avoids forcing strikes, hail, rainfall, and wind into the same event duration while preserving their interaction as a convective multi-hazard pattern.

### Open: non-wildfire event-object validation

Before production frequency and hotspot claims, the project needs stratified case review for each non-wildfire hazard. The minimum review set should include short, typical, long, broad, and high-intensity objects, with particular attention to:

- MRMS precipitation objects lasting more than 1-3 days;
- MESH objects lasting more than a few hours or covering large cumulative areas;
- future lightning bursts and their relationship to EchoTop and precipitation;
- broad extreme-temperature and wind objects;
- space-weather episodes with near-CONUS-wide masks.

### Open: production frequency and hotspot products

The current notebook can export event counts, event footprints, centroids, pair links, ordered sequences, native intensity, and preliminary group summaries. Hotspot analysis and cross-hazard intensity comparisons are intentionally deferred until a validated database has been generated. Production hotspot outputs should distinguish event count, occupied-cell recurrence, affected-area exposure, and valid-observation time, and should retain the hazard combination and lag definition used.

### Open: full-period generation gate

Full 2024-2025 generation should wait until the event-object validation layer is reviewed and the hail/lightning source status is handled explicitly. The first release may proceed without lightning, with MESH used as a clearly labeled hail proxy and space-weather regridding/provenance limitations explicitly flagged. Missing or unusable source fields must not be converted into zero hazard. Every production record should preserve source availability, cadence, threshold version, event-definition version, and lag-matrix version.

### Open: event-object validation standard

The final physical validation standard for non-wildfire event objects remains open. It must establish how much case-based review, independent source comparison, threshold sensitivity, and temporal/spatial segmentation evidence is sufficient before event frequencies, ordered-combination frequencies, and hotspots are treated as substantive results. This is a major unresolved research question rather than a cleanup task.

## Grid Impact Data

### Confirmed: county-key normalization requirement

Eagle-I county FIPS values must be converted to five-character strings and matched to Census/TIGER GEOIDs before county-based joins. This prevents silent mismatches caused by numeric/string types or dropped leading zeroes.

### Provisional: outage sources as complementary evidence

Eagle-I and Whisker Labs are being used as complementary impact datasets rather than assumed to be interchangeable. Their coverage, spatial units, timestamps, and measurement meanings must be aligned before comparison.

### Provisional: five-state observable chain

The working conceptual chain is `Hazard -> Stress propagation -> Localized fault -> Outage / swell response -> Restoration`. The intended observables include local hazard exposure, lagged stress features, Whisker sag/deep-sag/frequency-jump states, Eagle-I surge or major-outage states, spatial propagation, and recovery curves. This organizes the research but is not yet a validated causal model.

### Provisional: Whisker and Eagle-I roles

Whisker is currently treated as a finer-grained near-real-time stress proxy, primarily reflecting distribution-level behavior. Eagle-I is treated as a coarser disruption proxy at county or larger scale. Their co-movement may reflect distribution-to-transmission propagation, common hazard forcing, or both. The distinction requires multi-event statistical testing.

### Open: distribution-to-transmission bridge

The project lacks a direct, consistently labeled observation linking Whisker distribution behavior to transmission-system faults or outages. Candidate additions include transmission telemetry, protection and fault records, substation event logs, outage-management records, SCADA/PMU-derived quantities, and carefully scoped grid simulations. Partner access and confidentiality may limit what can be obtained; the absence of direct data must remain visible in conclusions.

### Open: near-miss definition and operationalization

The working scientific definition is dataset-independent: a near-miss may include a major disruption but does not become a large-scale collapse of the grid, or it is an observable approach toward a critical transition followed by return to a basin of attraction associated with normal operation. This should not be reduced to a threshold in Whisker or Eagle-I alone. A future operational definition must specify event boundaries, the normal-operation baseline, the spatial scale, and the evidence for approaching and returning from the transition.

### Emergent hypothesis: lead-lag behavior

Short-lead Whisker sags may precede some larger Eagle-I outages, while swells may follow outages or deep sags and mark a transition into automatic response and/or recovery. These relationships are probabilistic, county-dependent, hazard-dependent, and phase-dependent. They are not universal claims.

### Emergent hypothesis: distinct scales and phases

Major outages, near-misses, and quiet-time periods may have distinguishable stress and recovery timescales. Lead-lag relationships may change across ramp-up, peak, and recovery and across county topology and multi-hazard configuration. This requires enough independent events to support stratified statistical analysis; current notebook explorations do not establish it.

### Open: partner-data integration

The roles of NERC TADS, PJM data, SCE PSPS reports, CAISO information, and partner-provided data in the final validation and decision-support products remain to be specified. PJM API authentication is not complete in the current experiment.

## Network Analysis

### Confirmed: transmission network as an analysis object

The project uses a transmission-line GeoJSON with substation endpoints and NetworkX-style graph analysis. AOI clipping and network construction are established experimental operations.

### Open: projecting hazards onto the grid

The final rule for mapping gridded hazard values to transmission lines, substations, and graph elements is unresolved. Candidate approaches include proximity, cell intersection, line sampling, and graph-aware aggregation; the choice must be tested against impact observations.

### Open: validation metrics

The project has not finalized metrics for predictive accuracy, event correspondence, operational usefulness, or partner decision value. These should be selected with stakeholders rather than inferred from a convenient notebook plot.

### Provisional: primary analysis panel

The proposed primary statistical artifact is a county-by-15-minute merged panel combining hazard exposures, Whisker stress/response states, Eagle-I disruption states, network-neighbor features, event phase, and recovery indicators. Lead-lag, lift, cluster, and residual analyses should be run on this panel once its schema and coverage are verified.

### Open: risk-surface information inventory

Risk surfaces for voltage/frequency instability, outage, islanding, cascades, and restoration must be classified by information level: direct observation, quantitative proxy/model, qualitative characterization, or fundamentally unavailable. The current working inventory is:

| Outcome | Current evidence status | Main gap |
| --- | --- | --- |
| Voltage/frequency instability | Whisker frequency-jump and power-quality signals provide a proxy | Direct transmission-level measurements. |
| Localized fault | No consistently identified direct fault series | Fault labels and component-level records. |
| Outage | Eagle-I and Whisker observations at different scales | Reconciliation, coverage, and spatial linkage. |
| Islanding | No direct observation identified | Operational data or validated simulation. |
| Cascades | No direct cascade label identified | System telemetry and event reconstruction. |
| Restoration | Outage and swell decay provide partial indicators | Operator actions, restoration logs, and service-level metrics. |

This inventory is provisional and should be updated as data sources are acquired or ruled out.

## Reproducibility and Workflow

### Provisional: canonical conda specification

The root `environment.yml` is now the curated GitHub-facing project specification, with default creation name `nasa-grid-resilience` and Python 3.10. The user's existing local `spwxr_network_new` environment remains the working environment and does not need to share the YAML name. `src/environment.yml` (`grid_resilience`, Python 3.9) and `data_processing/multi-haz.yml` (`multi-haz`, Python 3.10) are retained as older or alternative records. This specification should be checked against the actually imported packages and notebook kernels before being treated as a reproducibility release.

### Provisional: terrestrial-weather processing boundary

`data_processing/` currently refers primarily to the terrestrial-weather processing work that produces daily files in `/Users/ryanmc/Documents/NASA_JPL/Projects/NaturalHazards/NASA ROSES Disasters 2025-2027/data/terrestrial_weather`. The intended future name is `terrestrial_weather_data_processing/`, but the rename is deferred until imports and notebook callers can be updated together.

### Open: canonical processing hierarchy

Future processing areas may separate terrestrial weather, grid impacts, space weather, wildfire, and shared grid utilities. No directory migration has yet been adopted as a confirmed decision.

### Provisional: external data roots

Current notebooks and processing code use exact absolute paths under `/Users/ryanmc/Documents/NASA_JPL/Projects/NaturalHazards/NASA ROSES Disasters 2025-2027/data/` and `/Users/ryanmc/Documents/Conferences/Jack_Eddy_Symposium_2022/dev`. `pipeline.py` also contains `/Users/marchett/Documents/Disasters/data`. These paths describe the current machines, not a portable configuration design.

### Provisional: daily incremental processing

Daily NetCDF outputs and incremental/event-window runs are the practical working pattern because full MRMS/RTMA periods are memory-intensive. This is an operational workaround until a more explicit storage and chunking strategy is adopted.

### Open: reproducibility package

There is no visible automated test suite, CI workflow, portable path configuration, or fully locked application-level release. A future reproducibility decision should cover environment, data manifests, configuration, provenance, and tests.

## How to Add a Decision

At a natural breakpoint, record the date, status, choice, evidence, and consequences. Link to the relevant notebook, module, output, or chat-derived diagnostic. When a decision changes, retain the old entry as superseded context rather than deleting it; update [architecture.md](architecture.md) if the change alters the system description.





