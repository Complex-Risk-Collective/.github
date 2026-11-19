"""Event and variable definitions for the hazard explorer.

Each event lists the variables the app can threshold. A variable whose input is missing on this
machine is skipped at build time and listed under `missing` in the manifest, so the app can say so.

Variable fields
- key / label / units: what the app shows
- source: how build_data.py loads it (see LOADERS there)
- threshold: default threshold in display units, or {"quantile": q} to take the q-quantile of the
  positive values (for products whose units are unclear)
- direction: ">=" (hazard when high) or "<=" (hazard when low)
- color_slot: categorical palette slot 1–8 (fixed per variable so a hazard keeps its colour)
- ramp: "sequential" or "diverging" for the background field
"""

from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_DIR / "data"
DATA_CACHE = Path.home() / "Downloads" / "mh_notebook_data"   # same cache the notebooks use

STATES_URL = "https://www2.census.gov/geo/tiger/GENZ2023/shp/cb_2023_us_state_20m.zip"


def _weather(dir_name):
    return {"kind": "daily_grid", "dir": DATA_CACHE / dir_name, "prefix": "weather_CONUS"}


def _fires(dir_name):
    return {"kind": "daily_grid", "dir": DATA_CACHE / dir_name, "prefix": "fires_analysis_grid"}


# Terrestrial variables from the team's MRMS/RTMA daily grids (data_processing/get_aggregate_*_data.py).
# Thresholds follow the V3 terrestrial hazards video where it states them (25 mm/h, 25 m/s gust, 35 °C).
# These grids hold the MEAN over each 50 km cell (bin_to_grid statistic="mean"), so point-scale thresholds
# such as 1-inch hail never fire; hail and rotation default to a high quantile instead.
def terrestrial_vars(weather_dir, fires_dir):
    w, f = _weather(weather_dir), _fires(fires_dir)
    return [
        {"key": "precip_1h", "label": "1-h gauge-corrected precipitation", "units": "mm / 1 h",
         "source": {**w, "var": "MultiSensor_QPE_01H_Pass2_00.00"},
         "threshold": 25.0, "direction": ">=", "color_slot": 1, "ramp": "sequential", "family": "terrestrial"},
        {"key": "gust", "label": "10-m wind gust", "units": "m/s",
         "source": {**w, "var": "i10fg"},
         "threshold": 25.0, "direction": ">=", "color_slot": 2, "ramp": "sequential", "family": "terrestrial"},
        {"key": "t2m", "label": "2-m temperature", "units": "°C",
         "source": {**w, "var": "t2m", "offset": -273.15},
         "threshold": 35.0, "direction": ">=", "color_slot": 8, "ramp": "diverging", "family": "terrestrial"},
        {"key": "hail", "label": "Estimated hail size (MESH, 50 km cell mean)", "units": "mm",
         "source": {**w, "var": "MESH_00.50", "negative_is_zero": True},
         "threshold": {"quantile": 0.999}, "direction": ">=", "color_slot": 3, "ramp": "sequential", "family": "terrestrial"},
        {"key": "rotation", "label": "Rotation track, 60 min (MRMS, 50 km cell mean)", "units": "source units",
         "source": {**w, "var": "RotationTrackML60min_00.50", "negative_is_zero": True},
         "threshold": {"quantile": 0.999}, "direction": ">=", "color_slot": 5, "ramp": "sequential", "family": "terrestrial"},
        {"key": "flash_ari", "label": "Rainfall recurrence interval (FLASH ARI max, cell mean)", "units": "years",
         "source": {**w, "var": "FLASH_QPE_ARIMAX_00.00", "negative_is_zero": True},
         "threshold": 10.0, "direction": ">=", "color_slot": 6, "ramp": "sequential", "family": "terrestrial"},
        {"key": "frp", "label": "Active fire (fire radiative power)", "units": "MW",
         "source": {**f, "var": "frp"},
         "threshold": 100.0, "direction": ">=", "color_slot": 4, "ramp": "sequential", "family": "terrestrial"},
    ]


EVENTS = {
    "marapr_2023": {
        "title": "March–April 2023 multi-hazard event",
        "start": "2023-03-26 00:00",
        "end": "2023-04-08 23:00",
        "variables": [
            {"key": "space_weather", "label": "Geoelectric Eh (McGranaghan site product)", "units": "source units",
             "source": {"kind": "site_nc", "path": DATA_CACHE / "mcgranaghan-rawdata.nc", "var": "Eh", "match_km": 75},
             "threshold": 150.0, "direction": ">=", "color_slot": 7, "ramp": "sequential", "family": "space_weather"},
            *terrestrial_vars("weather_CONUS_2023event", "fires_analysis_grid_2023event"),
        ],
    },
    "gannon_2024": {
        "title": "Gannon geomagnetic storm, May 2024",
        "start": "2024-05-08 00:00",
        "end": "2024-05-15 23:00",
        "variables": [
            {"key": "space_weather", "label": "Geoelectric field |E| (NOAA SWPC)", "units": "mV/km",
             "source": {"kind": "noaa_geoe", "dir": DATA_CACHE / "noaa_geoe_2024event", "match_km": 75},
             "threshold": 1000.0, "direction": ">=", "color_slot": 7, "ramp": "sequential", "family": "space_weather"},
            # Radar point detections from NOAA SWDI, used while the 2024 MRMS/RTMA grids aren't on this machine
            {"key": "hail_swdi", "label": "Radar hail size (NEXRAD level 3)", "units": "in",
             "source": {"kind": "swdi_points", "path": DATA_CACHE / "swdi_2024event" / "nx3hail_20240508_20240515.csv",
                        "field": "MAXSIZE", "stat": "max"},
             "threshold": 1.0, "direction": ">=", "color_slot": 3, "ramp": "sequential", "family": "terrestrial"},
            {"key": "tvs", "label": "Tornado vortex signatures (NEXRAD level 3)", "units": "count / h",
             "source": {"kind": "swdi_points", "path": DATA_CACHE / "swdi_2024event" / "nx3tvs_20240508_20240515.csv",
                        "stat": "count"},
             "threshold": 1.0, "direction": ">=", "color_slot": 5, "ramp": "sequential", "family": "terrestrial"},
            # Picked up automatically once the 2024 grids are copied into the data cache
            *[v for v in terrestrial_vars("weather_CONUS_2024event", "fires_analysis_grid_2024event")
              if v["key"] not in ("rotation",)],
        ],
    },
}
