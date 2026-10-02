
# Decision Log

> **Original purpose of this file**
>
> This file should capture key decisions made during development, especially decisions that emerge from the multi-hazard, grid-impact, and data-processing experiments.

This is a living record of consequential choices, not a transcript of every experiment. Statuses are **confirmed**, **provisional**, and **open**. A confirmed entry is supported by code, a diagnostic, or an explicit project choice; a provisional entry is useful for current work but still needs validation; an open entry has not been settled. Dates refer to the development history available in the repository and indexed Copilot sessions as of 2026-08-26.

## Scientific Foundation

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

### Confirmed: Eagle-I impact-episode severity metrics are heavy-tailed

An empirical CCDF tail-slope fit (ordinary least squares on log-log rank/frequency over the top decile) on the full 2024-2025 Eagle-I impact-episode table (`eaglei_impact_episodes_2024_2025.csv`, see the exploratory notebook `notebooks/eaglei_impact_exploration.ipynb`) found tail slopes of approximately -1.41 for `peak_customers_out`, -1.55 for `duration_hours`, and -0.82 for `integrated_customer_outage_hours`. Interpreted as power-law exponents (P(X > x) ~ x^-alpha), all three are below 2 (infinite theoretical variance), and `integrated_customer_outage_hours` is below 1 (infinite theoretical mean) -- its sample mean does not converge and is dominated by whichever handful of the largest storms happen to fall inside the observation window. The heavier tail on `integrated_customer_outage_hours` relative to `peak_customers_out` and `duration_hours` is consistent with it compounding both underlying heavy tails (roughly duration times severity) in storms where size and duration are themselves correlated.

This is direct empirical support for the "production thresholds should represent severity, not fixed exceedance rates" decision above: for these metrics, percentile- and mean-based statistics are not just philosophically disfavored, they are statistically unstable by construction. Consequences for the space-weather threshold experiment and any later use of Eagle-I impact severity to calibrate hazard thresholds:

- Prefer `peak_customers_out` or `duration_hours` as the primary impact-severity metric for impact/quiet-period comparisons; treat `integrated_customer_outage_hours` (alpha < 1) as secondary/confirmatory rather than a primary calibration target unless using rank-based statistics (median, exceedance counts, log-ratios) instead of means.
- The plan's existing "remove single-event dominance" robustness check is not optional for these metrics -- a leave-one-storm-out sensitivity check should be run before trusting any threshold or descriptor derived from them, especially for `integrated_customer_outage_hours`.
- These are OLS fits on the top decile of a two-year record, not MLE power-law exponent estimates; treat the regime (all three below 2, one below 1) as the reliable signal, not the specific decimal values, and expect the fitted slopes to shift if the record length changes or a top event is added or removed.

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

This should not yet be read as evidence that space weather is unrelated to Eagle-I impact episodes. The test as constructed pools all ~38,000 impact episodes nationally into one population; the overwhelming majority are ordinary weather- or equipment-driven outages with no plausible space-weather mechanism, so a real but rare GIC-driven subset (tied to specific storms, geomagnetic latitudes, and grid topologies) would be diluted well below detectability in an unconditional national comparison at this sample size. `e_mag_max` is also the field magnitude rather than its rate of change (`d_e_dt_max_mV_km_per_min`), which is the more physically direct driver of induced current. Before this feeds a production `space_weather_extreme` threshold or gets recorded as a confirmed null result, the plan's "Robustness checks" task (now in progress) should cover: rerunning on `d_e_dt_max_mV_km_per_min`; the full threshold-grid sweep rather than one percentile; cross-referencing sampled episode dates against known 2024-2025 geomagnetic storm windows; and stratifying by geomagnetic latitude.

This distributional-shift methodology -- matched quiet-period construction, descriptor extraction, bootstrap log-exceedance-ratio scoring -- is also intended, once finalized here, to be extended to the extreme wind, temperature, and precipitation threshold-revision tasks in `multihazard_database_plan.md`. That extension is planned, not started.

### Open: space-weather regridding and coverage provenance

The striping in the space-weather fields is treated as a mapping artifact from the modeled geoelectric output grid to the 50 km analysis grid, not as a direct observational feature. Production space-weather processing should interpolate the modeled Ex/Ey field to the analysis grid, mask fill values before computing magnitude or thresholds, and carry a coverage or interpolation-support mask. Until that regridding is validated, spatial hotspot and overlap results involving space weather should be interpreted as provisional.

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





