
# Working Guide for Copilot

> **Original purpose of this file**
>
> This file should establish what the project is and how to work in it: its foundation, style guide, and the durable context Copilot should read at the start of new sessions.

## Project Foundation

This is the NASA Disasters 2025-2027 project *Enhancing Power Grid Resilience through Multi-Hazard Analyses and Dynamic Responses*. The work quantifies risk to the electric grid from compounding space weather, terrestrial weather, and wildfire hazards, then develops decision-support products with utility, regional, and national grid partners.

The implementation is research software organized around a shared CONUS 50 km grid, graph-based power-grid analysis, xarray datasets, and notebook-led experiments. The project is exploratory. Do not describe a scaffold, diagnostic, or notebook output as a validated operational result without evidence.

## Start-of-Session Routine

1. Read [architecture.md](architecture.md) for the current data flow and external dependencies.
2. Read [decisions.md](decisions.md) before changing a hazard definition, grid representation, input source, time aggregation, or validation approach.
3. Inspect the target notebook or module locally before proposing changes. Preserve the user's current notebook edits and outputs.
4. Check the actual input files, dates, coordinate names, execution state, and output paths. State uncertainty when a conclusion depends on retained notebook output.
5. Use the user's locally maintained `spwxr_network_new` conda environment when running Python or notebooks. The root `environment.yml` is the GitHub-facing reproducibility specification and defaults to an independent environment named `nasa-grid-resilience`; the names do not need to match. Treat `src/environment.yml` and `data_processing/multi-haz.yml` as older records, not interchangeable current environments.

## Working Conventions

- Prefer small, reversible changes that match existing notebook and module patterns.
- Keep exploratory work in notebooks; move stable, reusable ingestion or transformation logic into `data_processing/` only when the interface is clear.
- Use the existing `AnalysisGridTemplate` and EPSG:5070 representation unless a documented decision changes the spatial basis.
- Normalize shared dimensions and coordinates before xarray merges. The intended common names are `time`, `y`, and `x`; inspect source metadata rather than assuming.
- Normalize Eagle-I county FIPS and Census/TIGER GEOID as strings, zero-padded to five characters, before merging.
- Treat timestamps as a scientific input. Check UTC versus local time, epoch decoding, daylight-saving transitions, and the time window used by each source.
- Avoid loading long MRMS/RTMA periods into memory at once. Prefer daily processing, chunking, or incremental outputs and report resource implications.
- Do not delete raw or cached data to make a workflow pass. Explain cleanup behavior when a processing function removes downloaded files.
- Use absolute external paths exactly as documented when working on this machine, but identify them as non-portable and do not invent replacements.
- Do not run long downloads, full-year generation, or expensive tuning sweeps without confirming the intended date range and input coverage.

## Analysis Standards

- Separate confirmed observations, provisional interpretations, and open design questions.
- For hazard thresholds, state the variable, temporal aggregation, spatial baseline, threshold rule, and reason for selection.
- For network impacts, distinguish a hazard mask, a projection onto network elements, an impact observation, and a causal or predictive score. These are different objects.
- Validate joins and alignment with counts, date ranges, coordinate shapes, missingness, and a small visual or tabular spot check.
- Preserve provenance: cite the source file or URL, local path, date window, execution state, and output artifact where practical.
- Do not silently overwrite user work, broaden the scope, or “fix” unrelated notebook cells.

## Observable-Chain Analysis

Organize the emerging analysis around `Hazard -> Stress propagation -> Localized fault -> Outage / swell response -> Restoration`, while treating it as a hypothesis framework rather than an established causal chain.

- Use hazard-specific physical transforms before cross-hazard normalization: engineering exceedance for wind or shear, operational intensity thresholds for reflectivity or precipitation, FRP/proximity weighting for wildfire, and local quiet-time anomaly or exceedance for space weather.
- Use regional and seasonal conditioning where appropriate. A percentile or empirical-CDF rarity scale makes variables commensurate but does not by itself establish physical equivalence.
- Begin terrestrial-weather selection with mechanism groups such as wind, convection, precipitation/flooding, icing, and thermal stress. Control redundancy and retain variables that are stable across counties and bootstrap samples.
- Evaluate lag windows explicitly, including 0-15 minutes, 15-60 minutes, 1-3 hours, and 3-6 hours, rather than assuming one universal lead time.
- Use Whisker as a finer-grained stress proxy and Eagle-I as a coarser disruption proxy, but account for common hazard forcing before interpreting their relationship as propagation.
- Prefer county-by-15-minute panels for the primary statistical work. Include event phase, network neighbors, rolling-baseline residuals, and data-quality fields.
- Keep models stratified by hazard type, county or topology, and ramp-up, peak, and recovery phase when aggregation would otherwise swamp the signal.
- Validate with leave-county-out or leave-region-out tests, time-shuffled permutation baselines, confidence intervals, and sensitivity to temporal, spatial, and binning windows.
- Treat a near-miss as an event that may include substantial disruption but not a large-scale collapse of the grid, or as an observable approach toward a critical transition followed by return to a normal-operation basin of attraction. This is a dataset-independent scientific hypothesis; do not define it using one dataset alone.
- Treat swell as a possible phase marker with multiple processes, including automatic actions and restoration, not as a single-process label.
- Do not claim that Whisker-to-Eagle-I co-movement proves distribution-to-transmission propagation. Common weather forcing is an alternative explanation that must be tested.

## Information Inventory

When proposing a risk surface for voltage/frequency instability, outage, islanding, cascades, or restoration, first classify the outcome as directly observed, quantitatively modeled from proxies, qualitatively characterized, or fundamentally unobserved. Record which dataset supports the classification and what additional data would change it.

## Current Boundaries

The canonical 50 km grid is established for current work, but the final multi-hazard score and network projection method remain open. Wildfire source selection and stakeholder product validation also remain provisional or open. As of 2026-08-26, missing 2024 terrestrial-weather and geoelectric coverage, plus absent 2025 files, blocks an unqualified full 2024-2025 MYRIAD run.

## Natural Breakpoints

When a design decision is reached, a problem is diagnosed, or a direction is chosen, summarize the result in the conversation and propose an entry for [decisions.md](decisions.md). Update [architecture.md](architecture.md) when the change affects the system structure or data flow. Update this guide only when the durable working rules change.





