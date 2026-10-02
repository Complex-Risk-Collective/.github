# Eagle-I full-period (2024-2025) regen checklist

**Status: complete.** The regen described below ran, passed all sanity checks
in section 4, and its "after it's sanity-checked" follow-up (section 5) has
been done — see the "Eagle-I impact-episode definition (production default)"
entry in `decisions.md` and the "Select Eagle-I impact episode definition"
row in `multihazard_database_plan.md`. Since this regen, a parameter sweep
(`quantile_level` x `min_customers` x `min_percent_out`) confirmed
`quantile_level` dominates episode count nationally (195,103 episodes at 0.95
down to ~20,105 at 0.995) while the absolute floors barely move it, and the
GeoE quiet-period/shift-scoring pipeline downstream of these episodes has
been built and bug-fixed (see decisions.md). This checklist file is kept for
historical reference on the source-collision bug and regen procedure, not as
an open task.

Written 2026-09-21 after the pilot (May 10-12, 2024) validated three fixes in
`geoe_eaglei_threshold_analysis.ipynb`: state-name normalization, "suppress
don't drop" override handling with gap-bridging persistence, and narrowing
suppression to `Contains Override Data == True` only (not
`Total Customers Type != 'collected'`).

Revised 2026-09-21 (same day) after restructuring cells 7 and 8 so a fully
un-cached run works top-to-bottom without a `FileNotFoundError` and without a
redundant second full-file scan, and after moving all Eagle-I-only outputs
off `space_weather/processed_data/` and onto `EAGLE-I/processed/`.
`space_weather/processed_data/` is reserved for future results that align
Eagle-I impact episodes against GeoE space-weather data.

Revised 2026-09-22 after discovering and fixing a source-collision bug: for
2024, `EAGLEI_DIR` holds two overlapping raw formats -- a daily-granular
directory (with a `Contains Override Data` column) and separate top-level
annual legacy files (`eaglei_outages_2024.csv`, `eaglei_outages_2025.csv`, no
override column). The old file-selection logic pulled in both for 2024, and
`build_eaglei_county_panel()`'s column-independent `max()` aggregation
silently mixed disagreeing values between them. Per decision, the notebook
now uses **only the two annual legacy files** for both years, for one
consistent source/granularity across the whole record. Consequences:
`is_suppressed_override` is now `False` everywhere by construction (neither
annual file has an override column -- the suppression logic itself is
unchanged and still applies to cell 9's pilot, which still uses the
daily-granular source), and 2025's missing `total_customers` column is
backfilled per-county from each county's 2024 value, with `percent_out`
recomputed for those backfilled rows. This full-period regen also caught and
fixed a state-name normalization gap (`"United States Virgin Islands"` was
not collapsing to `VI`); the panel/threshold/episode CSVs were patched in
place, and `US_STATE_NAME_TO_ABBR` was updated for future runs.

## 1. Clean up stale cached outputs first

All of these were built by the *old* code (before today's fixes) and will be
silently reused if left in place, because several cells check
`if <path>.exists(): load cached` before rebuilding. Delete or move these
aside before running the full regen. Everything Eagle-I-only now lives (or
should live) in `EAGLEI_DIR/processed/`
(`/Users/ryanmc/Documents/Conferences/Jack_Eddy_Symposium_2022/dev/outage_data/EAGLE-I/processed/`) —
`space_weather/processed_data/` should have no Eagle-I files in it going
forward.

**In `EAGLEI_DIR/processed/`:**
- `eaglei_county_15min_panel_2024_2025.csv` (~5.0 GB, dated Sep 15 — pre-fix panel cache, built before the suppression-scope fix)
- `eaglei_county_thresholds_2024_2025.csv` (built from the stale panel above)
- `eaglei_county_thresholds_qa_2024_2025.csv`
- `eaglei_impact_episodes_2024_2025.csv`, `eaglei_impact_episode_qa_2024_2025.csv`, `eaglei_impact_episode_outliers_2024_2025.csv` (if present — stale, built from the pre-fix panel/thresholds)
- `eaglei_matched_quiet_periods_2024_2025.csv` and `eaglei_matched_quiet_period_qa_2024_2025.csv` (downstream of the episodes above — also stale)

**In `space_weather/processed_data/`** (under the NASA ROSES data folder):
- Check for and remove any leftover Eagle-I-only files from before the
  directory fix (e.g. an old `eaglei_county_15min_panel_2024_2025.csv` or
  `eaglei_impact_episodes_2024_2025.csv`) — these should no longer be
  written here, and any that remain are stale duplicates of what's in
  `EAGLEI_DIR/processed/`.
- The pilot outputs (`*_pilot_20240510_20240512.csv`) are no longer written
  here either — you already moved those into `EAGLEI_DIR/processed/`, and
  the notebook's default paths now point there too, so there's nothing to
  clean up in this directory for the pilot.

Recommend renaming with a `.old` / `.pre20260921` suffix rather than
deleting outright, in case you want to diff old vs. new episode counts
directly — these are large files, so only keep them around if disk space
allows.

## 2. Cell execution order (fixed — no more workaround needed)

Cells 7 and 8 were restructured today so the mutual dependency that used to
require a manual out-of-order run is gone:

- Cell 7 now only builds/caches the county panel (`RUN_EAGLEI_PANEL_BUILD`,
  renamed from `RUN_EAGLEI_EPISODE_BUILD` since it no longer builds
  episodes). It no longer reads `EAGLEI_COUNTY_THRESHOLDS_CSV`, so it can't
  hit a `FileNotFoundError` on a clean cache.
- Cell 8 (`RUN_EAGLEI_THRESHOLD_BUILD`) builds the threshold table and then
  identifies impact episodes and writes the episode/QA/outlier CSVs — this
  logic moved here from cell 7's old tail, since it needs the threshold
  table this cell produces.
- Cell 8's panel loader (`load_or_build_county_panel_for_thresholds()`)
  first checks for the `county_outage_panel` already built in-memory by
  cell 7 in the same kernel session, before touching the file cache or
  rescanning raw Eagle-I files.

Net effect: running cells 7 then 8 in their normal physical top-to-bottom
order, on a completely clean cache, now does exactly **one** full Eagle-I
file scan (in cell 7), not two, and produces both the panel and the episode
table correctly. No manual reordering is required anymore.

## 3. Run the full regen

In order:

1. Restart the kernel (clears any stale in-memory state from partial pilot
   runs).
2. Run cells 0–6 (imports, paths, helpers) normally.
3. Run cell 7 (`RUN_EAGLEI_PANEL_BUILD = True`, already its default) — full
   Eagle-I scan, builds and writes the county panel. This is the expensive
   step; expect it to take a while and to produce a multi-GB panel CSV in
   `EAGLEI_DIR/processed/`.
4. Run cell 8 (`RUN_EAGLEI_THRESHOLD_BUILD = True`, already its default) —
   reuses cell 7's in-memory panel (no re-scan), builds and writes the
   threshold table, then identifies and writes the full impact-episode
   table using those thresholds.
5. Run cell 9 only if you want to regenerate the pilot outputs too (not
   required — they're already current from the earlier pilot run, and now
   live in `EAGLEI_DIR/processed/`).
6. Run cell 10 to reset all the `RUN_*` flags back to `False` so future
   "Run All"s don't accidentally trigger another multi-GB rebuild. Note the
   flag is now `RUN_EAGLEI_PANEL_BUILD = False` (renamed from
   `RUN_EAGLEI_EPISODE_BUILD`).
7. If you want matched quiet periods refreshed against the new episodes, run
   cell 15 with `RUN_EAGLEI_QUIET_MATCHING = True` (it's `False` by default —
   flip it just for that run, then let cell 10's reset — or a manual
   reset — put it back). Its outputs also now write to `EAGLEI_DIR/processed/`.

Given the sandbox previously OOM'd on a much smaller task (loading a ~5 GB
cached CSV plus the full episode file), do this run in your local Jupyter/VS
Code environment rather than through me — you have more memory headroom
there, and it lets you watch cell-by-cell output live.

## 4. Sanity-check before trusting the output

Per the project's own rule in `multihazard_database_plan.md` ("don't mark a
science task done because code ran — mark it done once the output is
sanity-checked"), check at minimum:

- `episode_qa` / `EAGLEI_EPISODE_QA_CSV`: episode count, county count, median
  duration — do these look like a reasonable multiple of the 3-day pilot
  (212 episodes, 186 counties) scaled up to a 2-year record? A sudden 100x+
  jump or a near-zero count both warrant investigation before trusting
  anything downstream.
- Suppressed-row fraction (`is_suppressed_override.mean()` over the full
  panel): under the legacy-only source (2026-09-22 on) this should be
  **exactly 0** — neither annual file has a `Contains Override Data` column,
  so the suppression logic is inert for this build by construction. A
  nonzero value here means the daily-granular directory got scanned again
  (re-check `eagle_i_csv_files_for_impact_window()`'s source, or reload the
  notebook from disk before re-running if you edited it externally).
- Spot-check the original motivating case directly: Harris County, TX,
  around 2024-05-21 should now show a normal `customers_out` (~131,113-ish
  range per the earlier investigation), not the old ~876 million garbage
  value, and it should NOT appear in `episode_outliers` for that date. (It
  may legitimately appear in `episode_outliers` for a different date/event —
  e.g. 2024-07-08 Hurricane Beryl showed peak_customers_out > total_customers
  on the confirmed 2026-09-22 run, which looks like a genuine stale-denominator
  issue in the source data, not the override bug.)
- `episode_outliers` (`flag_percent_out_gt_100`,
  `flag_customers_out_gt_county_total`): expect some — these are residual
  source-data denominator quirks (not the override bug), not a blocker.
- Compare a few state/county names against the panel to confirm
  `normalize_state_abbr()` is producing 2-letter USPS codes everywhere
  (`state_abbr.nunique()` should be exactly 53: 50 states + DC + PR + VI).
  Watch specifically for `"United States Virgin Islands"` failing to
  collapse to `VI` — this happened on the 2026-09-22 run and was fixed both
  in `US_STATE_NAME_TO_ABBR` (for future runs) and by patching the existing
  panel/threshold/episode CSVs in place.
- Duplicate check: confirm `build_eaglei_county_panel()`'s final groupby key
  (`['time', 'county_fips']`) didn't silently merge two different counties
  that share a `county_fips` typo — spot check a few high-outage counties'
  `county_name`/`state_abbr` pairs for consistency.
- Confirm all new/updated CSVs landed in `EAGLEI_DIR/processed/`, not
  `space_weather/processed_data/` — a quick `ls -la` on both directories
  after the run is the fastest check.

**Results from the 2026-09-22 run** (legacy-only source, VI fix applied):
panel_rows 58,183,354; panel_counties 3,077; panel_rows_suppressed_override
0; impact_episodes 37,950; episode_counties 2,999; median_duration_hours
1.5; state_abbr has exactly 53 distinct 2-letter codes. All checks above
passed.

## 5. After it's sanity-checked

Update `multihazard_database_plan.md` to record that the full-period Eagle-I
episode table now reflects the state-normalization, suppress-not-drop
override handling, and persistence-check fixes — this was explicitly deferred
until "some updates in this work" were done, and this regen is that point.
