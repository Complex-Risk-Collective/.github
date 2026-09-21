# Eagle-I full-period (2024-2025) regen checklist

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
  panel): the pilot window had 0% because no override rows fell in that
  3-day slice; over the full 2024-2025 record you should see a small
  non-zero fraction (`Contains Override Data == True` was rare but not
  absent — 25 of ~72k rows on the worst single day checked). If this comes
  back anywhere near the old ~71% figure, the suppression-scope fix didn't
  take — re-check cell 7's source.
- Spot-check the original motivating case directly: Harris County, TX,
  around 2024-05-21 should now show a normal `customers_out` (~131,113-ish
  range per the earlier investigation), not the old ~876 million garbage
  value, and it should NOT appear in `episode_outliers`.
- `episode_outliers` (`flag_percent_out_gt_100`,
  `flag_customers_out_gt_county_total`): expect some — the ~115-row,
  up-to-147%-outage quirk from the pilot (likely a "mixed"-type
  denominator mismatch, not the override bug) will still be present at full
  scale. Worth a follow-up look, but not a blocker for this regen.
- Compare a few state/county names against the panel to confirm
  `normalize_state_abbr()` is producing 2-letter USPS codes everywhere
  (`state_abbr.nunique()` should be ~50-53, not a mix of names and codes).
- Duplicate check: confirm `build_eaglei_county_panel()`'s final groupby key
  (`['time', 'county_fips']`) didn't silently merge two different counties
  that share a `county_fips` typo — spot check a few high-outage counties'
  `county_name`/`state_abbr` pairs for consistency.
- Confirm all new/updated CSVs landed in `EAGLEI_DIR/processed/`, not
  `space_weather/processed_data/` — a quick `ls -la` on both directories
  after the run is the fastest check.

## 5. After it's sanity-checked

Update `multihazard_database_plan.md` to record that the full-period Eagle-I
episode table now reflects the state-normalization, suppress-not-drop
override handling, and persistence-check fixes — this was explicitly deferred
until "some updates in this work" were done, and this regen is that point.
