
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

### Open: production coverage window

The updated input audit found complete wildfire and terrestrial-weather daily coverage for 2024-2025 (731/731 days each). Space weather has 677/731 days, with gaps from 2024-06-17 through 2024-08-05 and 2025-02-06 through 2025-02-09. A preliminary full-period run may proceed with those space-weather gaps explicitly flagged, but missing data must not be converted into zero hazard.

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
2. **Space weather:** second-highest confidence because the operational definition and broad-area behavior are explicit, although source gaps remain and wide-area episodes require review.
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

Full 2024-2025 generation should wait until the event-object validation layer is reviewed and the hail/lightning source status is handled explicitly. The first release may proceed without lightning, with MESH used as a clearly labeled hail proxy and space-weather gaps explicitly flagged. Missing or unusable source fields must not be converted into zero hazard. Every production record should preserve source availability, cadence, threshold version, event-definition version, and lag-matrix version.

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





