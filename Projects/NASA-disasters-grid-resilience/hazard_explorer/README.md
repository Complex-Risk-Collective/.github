# Multi-hazard explorer

A local browser app for exploring hourly hazard grids. Set a threshold for each variable, define rules for when hazards that happen close together in space and time count as a potentially significant interaction, and step through the event with a time slider. It runs entirely on your machine: no ArcGIS, no internet connection, no extra libraries.

## Quick start

```bash
cd Projects/NASA-disasters-grid-resilience/hazard_explorer
conda activate gdal_test                # any env with xarray, geopandas, scipy, pyproj
python build_data.py                    # or: python build_data.py marapr_2023
python -m http.server 8765 -d app       # then open http://localhost:8765
```

`build_data.py` reads the same inputs as `notebooks/multihazard_arcgis_time_layers_v1.ipynb`, from `~/Downloads/mh_notebook_data/`. It writes `app/data/` (about 60 MB for the 2023 event), which git ignores. Rebuild whenever the inputs or `events.py` change. The first run also downloads Census state outlines (about 1 MB) into the same cache.

## What's in the app

**Hazard thresholds (left).** Each variable has two threshold types:
- **Absolute value:** a fixed number, in the variable's units.
- **Place-specific percentile:** each cell is compared with its own percentile across the event window. An optional absolute floor keeps near-zero values from counting. This percentile is relative to the loaded window, not a climatology.

Set the direction to *high (≥)* or *low (≤)*, for cold extremes for example. A variable that is switched off is ignored everywhere.

**Interaction rules (left).** A cell is flagged in a given hour when:
- at least *N* of the rule's hazards exceed their thresholds within *R* km of the cell and within ± *W* hours, and
- the rule's *including* hazard is among them, if one is set, and
- (with *only flag cells where one of these hazards occurs* ticked) one of the rule's hazards exceeds in that cell itself during the window. This stops the distance buffer alone from flagging empty cells.

You can define up to 8 rules. Two are created by default: *any two hazards*, and *space weather + a terrestrial hazard*.

**Views (centre).**
- *Interactions map:* a background field of your choice, a coloured outline per exceeding hazard, and hatched flagged cells. Drag the legend by its header to move it, use – / + to collapse it, and double-click the header to put it back in the corner.
- *Variable fields:* one panel per variable with exceedance contours, the same layout as the V3 terrestrial-hazards video.

The timeline under the map shows, for every hour, how many cells exceed each threshold and how many each rule flags. Click or drag on it to move in time.

**Episodes (right).** Flagged cells that connect in space (8 neighbours) or time (the same cell one hour later) are grouped into episodes, ranked by cell-hours. Click an episode to jump to its peak hour. Click any map cell to pin its hourly series for every variable, with the threshold drawn in.

**Saving and sharing.**
- Settings are kept in the browser, per event.
- *Save settings* / *Load settings* move thresholds and rules around as JSON, so the team can compare hazard definitions.
- The URL hash (`#event=…&t=…&cell=…`) reopens the same hour and cell.
- *Export flagged cells* writes time-stamped GeoJSON polygons (with `start_time` / `end_time`, the rules and the hazard values) that open in QGIS or ArcGIS.
- *Export episodes* writes a CSV.

Keyboard: ← / → step an hour; space plays or pauses.

## Data and caveats

- **Grid:** the team's 50 km CONUS grid in EPSG:5070 (`data/analysis_grid_CONUS_50km.nc`), hourly.
- **Cell means:** MRMS and RTMA variables come from `data_processing/get_aggregate_*_data.py` and hold the **mean** over each 50 km cell. Point-scale thresholds don't transfer: 1-inch MESH hail never fires, because the cell mean, with MRMS's -1 "no hail" values included, peaks at about 3.8 mm for the 2023 event. Hail and rotation therefore start at the 99.9th percentile of their positive values. Treat those defaults as placeholders.
- **Space weather:** NOAA SWPC |E| for 2024 and the McGranaghan site product for 2023 (units not stated). Each grid cell takes the nearest site within 75 km; farther cells have no data and are drawn in grey.
- **Fire:** FRP ≥ 100 MW turns on somewhere in almost every hour of the 2023 event. Many of those cells are small agricultural or prescribed burns, so fire dominates the default episodes. Raise it, or use the place-specific mode, if that isn't what you want.
- **2024 Gannon:** only space weather and NEXRAD hail and tornado signatures (SWDI points) are on this machine. Copy `weather_CONUS_2024event/` and `fires_analysis_grid_2024event/` into the cache and rebuild; precipitation, gust, temperature, MESH, rainfall ARI and fire then appear automatically.

## Adding an event or variable

Edit `events.py`. An event has a start, an end and a list of variables. Each variable names a loader (`daily_grid`, `site_nc`, `noaa_geoe` or `swdi_points`), its source variable, units, a default threshold (a number, or `{"quantile": q}`), a direction, and a colour slot from 1 to 8. Each slot belongs to one hazard, so a hazard keeps its colour in every view. The palette has 8 slots, so an event can hold at most 8 variables; past that, colours stop being distinguishable. Then run `python build_data.py <event_id>`.

For June 2024 (the V3 terrestrial hazards video), add an event with `start`/`end` in June and point `terrestrial_vars(...)` at the June daily-grid folders.

## Files

| Path | What it is |
|---|---|
| `events.py` | Event and variable definitions, default thresholds |
| `build_data.py` | Loads each variable onto the grid and writes `app/data/<event>/` |
| `app/index.html`, `app/app.js`, `app/style.css` | The app: plain HTML/JS, no build step |
| `app/data/` | Generated (`manifest.json`, one `<variable>.f32` per variable, mask, cell corners, state outlines) |
