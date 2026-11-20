# Eagle-I 2024–2025: new-grid regeneration

Completed 2026-11-05 using `eaglei_grid_pipeline.py`, with
`data/analysis_grid_CONUS_50km_v1.nc` (67 y × 107 x, EPSG:5070).
The legacy annual products and legacy mapping tables were not overwritten.
The historical `aggregate_eagle-i.py` is not part of this production path.

> **Pending reprocessing (updated 2026-11-12):** The Eagle-I grids documented
> below were generated for the analysis grid that existed on **November 3**
> (67 × 107). They have **not** been regenerated for the later V3 grid at
> `/Users/ryanmc/Documents/NASA_JPL/Projects/NaturalHazards/NASA ROSES Disasters 2025-2027/data/terrestrial_weather/V3/analysis_grid_CONUS_50km_v1.nc`
> (68 × 110), which extends far enough east to cover all of Washington County,
> Maine. The current products retain the earlier grid's approximately 69.8%
> Washington County coverage. Rebuild the county mapping and both 2024/2025
> Eagle-I gridded products against the later grid before using them as
> full-Maine products; do not treat the matching filename as proof of matching
> grid coordinates.

## Outputs and reruns

Output root:
`/Users/ryanmc/Documents/Conferences/Jack_Eddy_Symposium_2022/dev/outage_data/EAGLE-I/processed/analysis_grid_CONUS_50km_v1/`

- `eaglei_outages_analysis_grid_2024.nc` — 35,136 native 15-minute timestamps, 373,208,281 bytes.
- `eaglei_outages_analysis_grid_2025.nc` — 35,040 native 15-minute timestamps, 373,488,508 bytes.
- Matching `.qa.json` and `.manifest.json` for each year.
- `mapping_2024/` and `mapping_2025/`: rebuilt area weights, SHA256 provenance,
  county grid-coverage fractions, and explicit excluded-county tables.

CLI defaults select this grid, both years, the existing annual-only county
panel (Parquet preferred, CSV fallback), and the output root above. `--source
raw` selects only the corresponding annual legacy CSV; daily-granular holdings
are never mixed in. Other controls: `--grid`, `--years`, `--eaglei-dir`,
`--output-dir`, `--chunksize`, `--overwrite`, `--self-test`.
An unchanged successful output is validated and skipped. A changed grid or
source requires a separate directory or explicit overwrite. Interrupted partial
files are retained for inspection and must be explicitly removed before retry.
Two bounded passes per year and temporary disk-backed arrays avoid a full-year
county/cell join in RAM; allow at least 3 GB temporary disk per active year,
plus final outputs. Years run sequentially.

## Allocation and missingness

- Positive county-polygon/grid-polygon intersection area, normalized to one
  per mapped county; no nearest-center fallback. Counties use newest available
  Census geometry with older-vintage fallback (including legacy Connecticut).
- Counts are fractional **areal allocations**, not observed customer locations.
  Uniform customer density within county overlap is an assumption.
- 48 contiguous states plus DC only. The 22 excluded counties in each year
  are in Alaska, Hawaii, Puerto Rico, or the Virgin Islands.
- Zero inside mapped footprint means **no reported outage**, not independently
  verified electricity availability. Cells outside the footprint and wholly
  absent source timestamps are NaN.
- A nonfinite/suppressed reported county value makes each affected grid cell
  NaN at that time. Other valid contributions sharing that cell are not exposed
  as complete totals. `valid_contribution_hidden_by_nan` records them explicitly.
- `source_row_count`, `mapped_row_count`, and `invalid_mapped_row_count` are
  temporal diagnostics, not proof of county-level reporting completeness.
- `mapped_county_count` is the static spatial footprint, not temporal support.
- Preserve native cadence: summing four snapshots produces customer-samples,
  not hourly customers. Any later hourly mean/max needs an explicit support rule.

## Run QA

| Check | 2024 | 2025 |
|---|---:|---:|
| Panel rows | 27,701,334 | 30,482,020 |
| Mapped counties | 3,039 | 3,054 |
| Positive intersections | 13,145 | 13,209 |
| Unmapped rows | 429,021 | 417,982 |
| Nonfinite reported rows | 0 | 1,972,650 |
| Wholly absent 15-minute timestamps | 41 | 12 |
| Valid mapped customer-sample sum | 7,027,085,044 | 3,212,850,762 |
| Pre-mask allocation conservation ratio | 1.0 | 1.0 |
| Valid contributions hidden by shared-cell NaNs | 0 | 55,349,567.541689 |

Conservation is tested **before** invalid-cell masking, for every timestamp and
annually. The written visible sum plus hidden contributions equals the valid
mapped input sum. In 2025 the visible field alone does not conserve all valid
inputs because masking takes precedence over presenting incomplete cell totals.
These sums are customer-samples, not distinct customers or customer-hours.

The 2025 nonfinite count matches the raw annual CSV; sampled county/timestamp
values were checked against raw rows. The cached panel removes duplicate
county/time keys; streaming processing rejects any remaining duplicates.
Suppression flags were false throughout this annual-only panel.

**Grid clipping:** Washington County, Maine (23029) has only
0.6982130776 county-area coverage in this grid, the only mapped county below
99%. Its full county count is allocated within the overlapping area, following
the mass-conserving convention. Review this limitation before treating its
allocated spatial pattern as physical truth. No grid geometry was modified.

Validation: 37 offline regressions plus streaming self-test; independently
reopened both outputs, checked x/y/lat/lon against the supplied grid, annual
time lengths, all temporal conservation identities, selected initial/midyear/
peak/final frames, NaN spatial footprints, and all absent-source frames. One
NumPy binary-compatibility warning appeared during tests; tests and production
NetCDF I/O completed successfully.