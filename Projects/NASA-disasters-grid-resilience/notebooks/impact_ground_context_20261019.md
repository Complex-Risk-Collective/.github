# Impact-event ground/OMNI checkpoint — 2026-10-19

## Follow-up: original July 8 source verified

Both investigated pulses already exist in the original NOAA daily NetCDF's
modeled Ex/Ey (explicit units mV/km). At original zero-based gridpt 2177,
42.0 N, 92.0 W:

| UTC | Source Ex (mV/km) | Source Ey (mV/km) | Source magnitude (mV/km) | Derived peak county cell magnitude (mV/km) |
| --- | ---: | ---: | ---: | ---: |
| 18:34:30 | -1305.611 | 1578.549 | 2048.521 | 2038.185 |
| 19:06:30 | 1478.895 | -15312.358 | 15383.610 | 15261.831 |

Independent Delaunay reconstruction of all four county cells over 18:25–19:15
matches the existing cache within 0.000575 mV/km maximum absolute magnitude
difference. These pulses were **not created by our spatial interpolation**.
This locates their origin in the supplied modeled source, not necessarily in
physical ground forcing; model/input validity remains unresolved.

Important source metadata:
- Model empirical-EMTF-2022.12, version 2022.12; one-minute timestamps at :30.
- Point 2177 has distance-to-nearest-observatory 1069.05 km. This is distinct
  from the ~3.67 km grid-center-to-modeled-point interpolation distance.
- Quality at contributing points is 3 at both peaks; the file supplies no
  flag meaning, so do not interpret 3 as either valid or invalid.
- At 18:34:30 nobs=12, with BOU,BSL,CMO,FCC,FRD,FRN,MEA,OTT,SIT,SJG,TUC,VIC.
  At 19:06:30 nobs=11, with the same list except VIC. R01 is not an input
  observatory in either list.
- VIC disappears at 18:35:30, returns at 18:40:30, disappears at 18:53:30,
  and returns at 19:07:30. Both investigated peaks precede list transitions
  by one minute; this temporal proximity is a diagnostic lead, not proof of
  causation. Ask the developer about missing-input transitions/filtering.

Reproducible read-only audit:
[check_july_geoe_source.py](../data_processing/check_july_geoe_source.py).
New external output directory under space_weather/processed_data:
july08_original_source_audit_20261019, containing original_vs_derived.png,
source_points.csv, interpolation_vertices.csv, cached/reconstructed magnitude
tables, observatory_inputs.csv and a source metadata manifest. No original or
existing derived files were modified.

## Design and provenance

Resumed the two preselected county-local-v2 pilot matched sets, not a new sample
or an association test. Used original impact/control windows unchanged and fixed
two-hour context on each side. No lag search, control reselection, threshold fit,
or raw Eagle-I/GeoE rebuild. All times below are UTC.

Executable audit: [check_impact_space_weather.py](../data_processing/check_impact_space_weather.py).
It writes new output directories only, requires opt-in downloads, records source
paths/baseline/errors and checkpoints after each window. Three synthetic ground
metric tests and 22 acquisition regressions passed.

External artifacts are under the existing NASA data root, in
space_weather/processed_data/impact_ground_context_regional_20261019:
seven six-panel PNGs, extracted county-cell GeoE, OMNI and station context CSVs,
ground_window_summary.csv, omni_window_summary.csv, per-window OMNI coverage,
windows.csv and manifest.json. The initial nearby-station attempt is preserved
separately in impact_ground_context_20261019, including May 15 inventory.
No registered access ID is embedded in source or output manifests.

SuperMAG flags were explicitly all,baseline=yearly; no delta=start. Stored N/E/Z
are magnetic-frame components; geographic components are retained separately.
Horizontal change is sqrt(delta_N² + delta_E²), only across adjacent finite
one-minute samples. It is a descriptive magnetic step, not a GIC observation or
an estimated electric field. Constant offsets do not affect these changes.

## Availability changes the validation strength

- R01 (Missouri, ~309 km from county 19095) returned complete, finite minute data
  for July 8 and July 22, 2024, including all 46 samples in each target window.
- M03 (Worthington) and R03 (Hennepin) returned no timestamped data for all five
  May dates. May 15 inventory also excludes them, M02/M04 and R01. Do not equate
  a station's presence in the registry with historical availability.
- Nearest station in May 15 inventory is PIN (Pinawa, ~822 km from county 19033).
  BOU (Boulder, ~1051 km) was added for lower-latitude regional context. These two
  stations were fixed across all five dates based on location/availability,
  **not** signal amplitude. They cannot validate county-local magnetic forcing.
- All successful target-window ground series had 100% finite horizontal samples.
  BOU May 22 failed with unsuccessful API status, retained in the manifest; no
  automatic retry or gap filling. PIN May 22 succeeded.

## County 19033: original 20:15–21:15 May windows

| Date / role | County GeoE max (mV/km) | PIN max horizontal step (nT/min) | BOU max step (nT/min) | AE max (nT) | SYM-H min (nT) |
| --- | ---: | ---: | ---: | ---: | ---: |
| May 1 control | 36.53 | 8.59 | 3.49 | 657 | -40 |
| May 8 control | 11.77 | 3.88 | 1.41 | 182 | -28 |
| May 15 impact | 18.24 | 4.66 | 1.92 | 685 | -28 |
| May 22 control | 17.53 | 4.38 | unavailable | 251 | -10 |
| May 29 control | 72.29 | 15.81 | 7.36 | 1026 | -54 |

AE and SYM-H have complete coverage in these exact windows. OMNI speed does
not: valid minute counts are 39/61, 39/61, 51/61, 49/61 and 41/61 respectively.
May 29 speed median is ~756 km/s among available samples, versus ~432 on May 15;
do not describe these incomplete upstream measurements as a full forcing record.

This corroborates the qualitative observation that geomagnetic variability
occurs in both impact and no-impact periods. May 29 has the largest regional
magnetic steps and county GeoE of these five windows; May 1 also exceeds May 15
in both selected regional step metrics. May 15 is not uniquely distinguished by
these descriptors. This is not proof of absence/presence of GIC impacts: Eagle-I
customer outages are not a complete grid-impact measurement and terrestrial
weather/operational confounding remains unadjusted.

## County 19095: July outlier deserves source QA first

| Window / role | County GeoE max (mV/km) | R01 max horizontal step (nT/min) | AE max (nT) | SYM-H min (nT) |
| --- | ---: | ---: | ---: | ---: |
| July 8 18:15–19:00 control | 2038.18 | 1.35 | 138 | -7 |
| July 22 18:15–19:00 impact | 7.68 | 1.84 | 527 | -22 |

At the July 8 GeoE peak (~18:34:30), R01 shows smooth, small minute changes.
The fixed-context plot also exposes repeated sharp modeled-field pulses and a
much larger pulse just after the selected window (above 15,000 mV/km). The
nearby station does not show corresponding strong rapid changes in that context.

**Interpretation: unresolved source/model discrepancy, not a confirmed physical
extreme and not a proven artifact.** A single station 309 km away and global
indices cannot exclude localized forcing; conductivity/transfer functions matter,
and electric field is not universally proportional to the instantaneous local
magnetic derivative. Ground inputs may also overlap NOAA model inputs, so this
is not guaranteed independent validation.

Before broadening or scoring: inspect the original NOAA point files around
July 8 18:34:30 and the >15,000 mV/km context pulse, including source flags,
units/scales, contributing source locations, temporal continuity and model
input provenance. Distinguish pre-existing source pulses from gridding effects.
Keep the original control and its descriptors; flag for QA, do not remove it
because it weakens impact/control contrast. Any objective validity rule must
apply identically to impact and control windows.

The existing 23-episode paired pilot remains descriptive and nonrepresentative;
no new score, p-value, causal attribution or production threshold is warranted.