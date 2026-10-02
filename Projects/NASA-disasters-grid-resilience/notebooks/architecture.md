
# Architecture

> **Original purpose of this file**
>
> This file should detail what is in this directory, file structure, key dependencies, and directories the code accesses outside this directory.

## Purpose

This project develops a data-driven, graph-based system for quantifying and reducing risk to the electric power grid from interacting space-weather, terrestrial-weather, and wildfire hazards. The scientific objectives and intended stakeholder partnerships are described in [README.md](../README.md). This document describes the implementation as it exists on 2026-10-02 (last substantively updated on that date); it is an experimental research codebase, not yet a packaged production system.

## Repository Layout

| Path | Role |
| --- | --- |
| `data/` | Small, local project assets: analysis-grid NetCDF, California and event AOIs, NERC boundaries, outage/intersection tables, and shapefile components. |
| `data_processing/` | Currently the terrestrial-weather data-processing area: download, aggregation, gridding, and orchestration modules that produce daily files for the external `terrestrial_weather` data directory. Its contents are not yet reorganized into a general processing hierarchy. |
| `notebooks/` | Exploratory analyses, diagnostic experiments, event studies, visualizations, and the three project-maintenance documents. `_cache/` and `outputs/` contain notebook-generated or staged artifacts. |
| `environment.yml` | New curated conda specification for reproducing the project; its default environment name is `nasa-grid-resilience`. |
| `src/environment.yml` | Older `grid_resilience` exported environment record; retained for comparison and historical reproducibility. |
| `data_processing/multi-haz.yml` | Older `multi-haz` processing environment record; not the canonical project environment. |
| `README.md` | Scientific motivation, goals, technical approach, and partner context. |

The notebooks are deliberately named as experiments and versions. They are evidence of development and analysis, but they should not automatically be treated as reusable library code or as reproducible results. Many retain outputs from earlier execution even when their cells are currently marked unexecuted.

## Main Data Flow

```text
NOAA SWDI / NOAA SWPC / AWS MRMS and RTMA / wildfire sources / outage archives
				    |
			    download or cache
				    |
		   AnalysisGridTemplate (EPSG:5070)
				    |
		    daily gridding and xarray merge
				    |
		hazard layers on a shared 50 km CONUS grid
				    |
	notebooks: events, network projection, impacts, diagnostics
```

### Current processing boundary

At present, `data_processing/` was created for the terrestrial-weather work. Its principal output is the daily NetCDF collection in `/Users/ryanmc/Documents/NASA_JPL/Projects/NaturalHazards/NASA ROSES Disasters 2025-2027/data/terrestrial_weather`. The directory also contains `aggregate_eagle-i.py`, `multi-haz.yml`, and product spreadsheets, so its physical contents do not yet perfectly match that conceptual boundary.

The intended future name for the current area is `terrestrial_weather_data_processing/`. This restructure is documented but deferred: moving the files now would require updating bare imports, notebook path assumptions, and any external callers. A future general processing layout may add separate areas for grid impacts, space weather, wildfire, and shared utilities.

### Processing modules

- `data_processing/grid.py` defines `AnalysisGridTemplate`. It loads or creates a projected grid, stores per-cell latitude and longitude, creates bin edges, and can create hourly time axes.
- `data_processing/get_swdi_data.py` downloads NOAA SWDI CSV products; `aggregate_swdi.py` aggregates selected products to the analysis grid.
- `data_processing/get_aggregate_mrms_data.py` accesses public MRMS GRIB2 files on AWS, loads selected variables, and bins them to the grid.
- `data_processing/get_aggregate_rtma_data.py` performs the corresponding RTMA GRIB2 loading and gridding.
- `data_processing/pipeline.py` orchestrates one-day processing: SWDI, MRMS, and RTMA are merged with xarray and written as compressed daily NetCDF files.
- `data_processing/aggregate_eagle-i.py` is an impact-data utility currently colocated with the terrestrial-weather code; it should move to a grid-impact area when the deferred restructure occurs. `aggregate_swdi.py` belongs to the current terrestrial-weather area. Other processing scripts are exploratory or support specific data sources and should be read before reuse.

The current pipeline uses `xr.merge(..., compat="override")`, renames SWDI dimensions to `time`, `y`, and `x`, and writes files named `weather_CONUS_YYYYMMDD.nc`. It currently contains a machine-specific `PATH` (`/Users/marchett/Documents/Disasters/data`) and a June 2024 example date range. This is an important implementation constraint, not a project-wide configuration contract.

## Canonical Spatial Representation

The working CONUS representation is a projected 50 km grid in EPSG:5070, with approximate geographic bounds of 25 to 50 degrees north and -125 to -65 degrees longitude. Grid coordinates are projected `x` and `y`; two-dimensional `lat` and `lon` variables identify the cells. Hazard datasets are expected to be brought to this common representation before comparison or event identification.

The grid template is stored locally at `data/analysis_grid_CONUS_50km.nc`. A copy is also referenced by some notebooks in the external NASA data directory. Those copies must be checked for equivalence before being treated as interchangeable.

## Hazard and Impact Inputs

| Family | Current sources and examples | Current role |
| --- | --- | --- |
| Severe weather | NOAA SWDI products such as `nx3tvs`, `nx3hail`, and `nx3structure` | Point/event observations aggregated to the common grid. |
| Terrestrial weather | MRMS and RTMA GRIB2 data on public AWS S3 | Hourly precipitation, reflectivity, lightning, wind, temperature, and related fields; selected variables continue to evolve. |
| Space weather | NOAA SWPC InterMagEarthScope geoelectric NetCDF files, interpolated to a fill-masked, no-extrapolation analysis-grid cache (`geoe_interpolated_v1_fillmasked_interp_noextrap`, monthly NetCDFs, 2024-2025) | `Ex`/`Ey`-derived `E_mag` on the analysis grid, now also compared against Eagle-I grid-impact episodes to derive an operational threshold (see Eagle-I Processing below). |
| Wildfire | Daily wildfire event/polygon products, including GeoPackage-based inputs in recent work | Event inventory and spatial hazard mask. The final source contract is still provisional. |
| Grid impacts | Eagle-I county outage records, Whisker Labs JSON/THD data, and NERC/PJM/SCE experiments | Temporal and spatial impact evidence for validation and decision-support exploration. |

## Eagle-I Processing

Eagle-I county-level power-outage data has a complete production pipeline in `notebooks/geoe_eaglei_threshold_analysis.ipynb`, kept distinct from the general multi-hazard grid flow above because Eagle-I is natively county-FIPS-indexed, not analysis-grid-indexed.

- **Raw sources:** two overlapping raw formats exist for 2024 (a daily-granular directory with a `Contains Override Data` column, and separate top-level annual legacy files for 2024 and 2025). The full-period production pipeline deliberately uses only the two annual legacy files for both years, after a source-collision bug was found where mixing both formats silently corrupted max-based aggregation (see `decisions.md`). The daily-granular source remains in use for the May 2024 pilot window only.
- **County panel:** `build_eaglei_county_panel()` builds a county-by-15-minute panel (58,183,354 rows, 3,077 counties, 2024-2025) from the raw source, with 2025's missing `total_customers` backfilled per-county from 2024 values.
- **Thresholds and episodes:** a per-county threshold table (99th-percentile customers-out/percent-out, floored absolute minimums) feeds `identify_eaglei_impact_episodes()`, which identifies impact episodes under the `county_p99` definition (see `decisions.md` for the full rule) -- 37,950 episodes across 2,999 counties in the current full-period run.
- **Matched quiet periods and GeoE alignment:** `construct_time_matched_quiet_periods()` builds time-conditioned quiet-period controls per episode, and a descriptor-extraction/bootstrap-shift pipeline compares GeoE electric-field exposure between the two populations -- the active workstream for a `space_weather_extreme` threshold (see `multihazard_database_plan.md`).
- **Gridding:** Eagle-I's native unit is county FIPS, not the shared 50 km analysis grid. A separate county-to-analysis-grid-cell mapping (polygon intersection on five-character FIPS, with Connecticut's post-2022 legacy-FIPS transition recovered via 2021 Census geometry, and an explicit unmapped-county list rather than silent drops) exists for cross-hazard comparison, independent of the county-native panel used for Eagle-I's own threshold/episode work.
- **Output locations:** Eagle-I-only outputs (panel, thresholds, episodes, quiet periods) live in `EAGLEI_DIR/processed/`; outputs that align Eagle-I against GeoE live in `space_weather/processed_data/` (`GEOE_OUTPUT_DIR`). These were deliberately separated after Eagle-I-only files were found duplicated in the wrong directory (see `eaglei_full_period_regen_checklist.md`).

## Network Analysis Layer

The network notebooks load transmission-line geometries, construct graph representations, clip them to event AOIs, and explore assigning gridded hazards to lines, substations, or other network elements. `grid_network_analysis_v1.ipynb` is foundational; `multihazard_network_analysis_experiment_v1.ipynb` through `v3` progressively combine network structure with hazard layers. The later notebooks are scaffolds and diagnostics: the final projection rule and multi-hazard impact score are not yet fixed.

## Observable System Architecture

The emerging scientific model is a five-state chain:

```text
Hazard -> Stress propagation -> Localized fault -> Outage / swell response -> Restoration
```

This is a conceptual architecture for organizing observables and hypotheses, not yet a validated causal model. The current measurement strategy is:

- Build local hazard-exposure features for lines, substations, and counties, including lag windows such as 0-6 hours.
- Treat Whisker sag, deep-sag, frequency-jump, outage, and swell behavior as intermediate stress or response observables.
- Use lagged Whisker features and network-neighbor terms to predict Eagle-I surge or major-outage states.
- Trace Whisker and Eagle-I events through space and time, then measure cluster growth, lead-lag relationships, spread, and recovery.
- Keep stress decomposed by hazard mechanism as well as summed, so attribution is not lost.

The primary proposed analysis table is a county-by-15-minute merged panel containing hazard exposures, Whisker states, Eagle-I states, network context, event phase, and recovery indicators. This panel is a proposed research product, not yet a stable artifact in the repository.

### Evidence and information levels

Risk surfaces should be developed only after classifying each outcome by the strongest available evidence:

| Outcome | Direct observation currently identified | Proxy or model candidate | Qualitative or unavailable gap |
| --- | --- | --- | --- |
| Voltage/frequency instability | Whisker frequency-jump and power-quality signals | Hazard-conditioned stress model | Direct transmission-level measurements need assessment. |
| Localized fault | No consistently identified direct fault series | Line/substation exposure and network-neighbor models | Fault mechanisms and labels remain a major gap. |
| Outage | Eagle-I and Whisker outage observations at different scales | Lagged hazard/stress prediction | Source reconciliation and coverage limits remain. |
| Islanding | No direct observation identified | Network simulation or partner data | Likely requires operational grid data. |
| Cascades | No direct cascade label identified | Graph-based spread and event-sequence models | Direct cascade evidence is fundamentally limited without system telemetry. |
| Restoration | Outage and swell decay may provide partial indicators | Recovery curves and phase-aware models | Restoration actions and operator state are not directly observed. |

The table is a working inventory and must be revised as datasets are inspected. It distinguishes observation from inference and from absence of quantitative information.

## Notebook Families

- **Multi-hazard network analysis:** `multihazard_network_analysis_experiment_v1.ipynb`, `v2`, `v3`, and `multihazard_network_analysis_may2024_storm.ipynb` develop grid construction, AOI clipping, hazard alignment, network overlays, and event diagnostics.
- **California and event studies:** `ca_multihazard_2024_exploration.ipynb`, `multihazard_system_CA_experiment_v1.ipynb`, `CA_grid_representation_exploration.ipynb`, `California_shape_files.ipynb`, and `calculate_aoi_geojsons.ipynb` focus on California geometry, event AOIs, and system-level impact questions.
- **Grid impact data:** `grid_network_analysis_v1.ipynb`, `eagleI_outage_visualization_experiment_v1.ipynb`, `NERC_TADS_data_exploration_v1.ipynb`, `PJM_API_experiment_v1.ipynb`, `PJM_PCLLRW_experiment_v1.ipynb`, `SCE_PSPS_report_extractor_experiment_v1.ipynb`, and `whisker_labs_experiment_v1.ipynb` investigate network structure, outages, reliability data, and partner-specific sources.
- **MYRIAD identification:** `myriad_data_experiment_v1.ipynb` prepares a historical reference event list; `myriad_algorithm_multihazard_identification.ipynb` implements and tunes a CONUS multi-hazard event-identification workflow.
- **Space weather:** `NOAA_geoE_exploration_v1.ipynb` documents NOAA geoelectric file structure, caching, and location time series. Its current cells are unexecuted, although some outputs remain stored.
- **Eagle-I / GeoE distributional-shift analysis:** `geoe_eaglei_threshold_analysis.ipynb` builds the Eagle-I county outage panel and `county_p99` impact-episode table, constructs matched quiet-period controls, extracts GeoE electric-field descriptors over both populations from the interpolated cache, and scores distributional shift via a bootstrap log-exceedance-ratio -- the active workstream for a `space_weather_extreme` threshold (see `decisions.md` and `multihazard_database_plan.md`). See the dedicated "Eagle-I Processing" section above for the full pipeline.

## External Dependencies and Paths

The code currently reaches outside this directory. These paths are exact local paths observed in the work and are not portable interfaces:

- Hazard data and MYRIAD outputs: `/Users/ryanmc/Documents/NASA_JPL/Projects/NaturalHazards/NASA ROSES Disasters 2025-2027/data/`
- External development and grid/outage references: `/Users/ryanmc/Documents/Conferences/Jack_Eddy_Symposium_2022/dev`
- Transmission lines: `/Users/ryanmc/Documents/Conferences/Jack_Eddy_Symposium_2022/dev/physical_grid_data/U.S._Electric_Power_Transmission_Lines.geojson`
- Eagle-I 2024 outages: `/Users/ryanmc/Documents/Conferences/Jack_Eddy_Symposium_2022/dev/outage_data/EAGLE-I/eaglei_outages_2024.csv`
- NERC TADS data: `/Users/ryanmc/Documents/Conferences/Jack_Eddy_Symposium_2022/dev/NERC_TADS`
- Historical MYRIAD source: `/Users/ryanmc/Documents/Conferences/Jack_Eddy_Symposium_2022/dev/candidate_multihazards_data/myriad-hes.csv`
- Whisker Labs data: `/Users/ryanmc/Documents/NASA_JPL/Projects/NaturalHazards/NASA ROSES Disasters 2025-2027/data/Whisker_Labs_Data_March2026/`
- NOAA geoelectric cache: `/Users/ryanmc/Documents/NASA_JPL/Projects/NaturalHazards/NASA ROSES Disasters 2025-2027/data/space_weather/NOAA_geoE/`
- Eagle-I-only processed outputs (panel, thresholds, episodes, quiet periods): `/Users/ryanmc/Documents/Conferences/Jack_Eddy_Symposium_2022/dev/outage_data/EAGLE-I/processed/`
- Outputs aligning Eagle-I against GeoE (`GEOE_OUTPUT_DIR`): `/Users/ryanmc/Documents/NASA_JPL/Projects/NaturalHazards/NASA ROSES Disasters 2025-2027/data/space_weather/processed_data/`

Online interfaces include NOAA SWDI, NOAA SWPC, and anonymous/public AWS S3 access for MRMS and RTMA. Census/TIGER county geometry is also used in some workflows. Credentials, network availability, public bucket policies, and local cache contents affect reproducibility.

## Environment

The canonical project specification is the root `environment.yml`, whose default creation name is `nasa-grid-resilience` and whose packages target Python 3.10 and conda-forge. The user currently works locally in the separately named `spwxr_network_new` environment; the two names do not need to match. The specification is intentionally curated around the current terrestrial-weather, geospatial, notebook, and network-analysis workflows. Important packages include xarray, numpy, pandas, scipy, scikit-learn, dask, geopandas, shapely, pyproj, rasterio/rioxarray, netCDF4/h5py/cdflib, cfgrib/pygrib, boto3, matplotlib, folium, NetworkX, and Jupyter. `src/environment.yml` (`grid_resilience`, Python 3.9) and `data_processing/multi-haz.yml` (`multi-haz`, Python 3.10) remain older environment records and should not be silently treated as equivalent.

## Known Architectural Constraints

- Machine-specific absolute paths prevent a fresh clone from running without edits.
- Coordinate and time naming differs across source modules; normalize before merging.
- Large MRMS/RTMA periods can exceed memory and disk budgets; daily or chunked processing is the current practical pattern.
- Eagle-I county keys must be normalized as five-character strings before joining to Census/TIGER GEOIDs.
- Whisker timestamps and NOAA geoelectric time encodings require explicit timezone/epoch checks.
- There is no visible automated test suite or CI workflow. Notebook outputs are useful evidence but require provenance checks.
- Resolved: the terrestrial-weather and NOAA geoelectric coverage gaps found in the 2026-08-26 MYRIAD input audit (missing terrestrial-weather days 2024-06-02 through 2024-06-14, missing geoelectric days 2024-06-17 through 2024-08-05, no 2025 files for either source) were filled; `decisions.md` records complete 2024-2025 daily coverage for wildfire, terrestrial-weather, and space weather as of the updated audit. Full 2024-2025 generation is no longer blocked on this specific gap.

## Documentation Maintenance

Update this file when a data flow, interface, dependency, or ownership boundary becomes stable. Record the decision and its status in [decisions.md](decisions.md), and update [copilot.md](copilot.md) when the working conventions change. Treat notebook conclusions as provisional until their inputs, execution state, and validation are recorded.

