# Gridded weather product — data dictionary (v2)

- **grid**: 50 km cells, EPSG:5070 (CONUS Albers); x/y in m
- **time_convention**: hourly; see time_window attribute per variable
- **missing_convention**: NaN = no data / no coverage; 0 = observed zero

| variable | source | units | cell statistic | time window | description |
|---|---|---|---|---|---|
| `EchoTop_18_00.50` | MRMS | km | max | max over 6 files in [HH:00, HH+1:00) | 18 dBZ echo top. Height of the 18 dBZ echo; storm depth / convective intensity |
| `EchoTop_50_00.50` | MRMS | km | max | max over 6 files in [HH:00, HH+1:00) | 50 dBZ echo top. Height of the 50 dBZ echo; strongest storm cores, hail/wind indicator |
| `FLASH_QPE_ARI012H_00.00` | MRMS FLASH | years | max | accumulated product, file nearest HH:00 (window ends at label time) | 12-h rainfall recurrence interval. Average recurrence interval of the 12-h rainfall |
| `FLASH_QPE_ARI01H_00.00` | MRMS FLASH | years | max | accumulated product, file nearest HH:00 (window ends at label time) | 1-h rainfall recurrence interval. Average recurrence interval of the 1-h rainfall; extreme short-duration rain |
| `FLASH_QPE_ARIMAX_00.00` | MRMS FLASH | years | max | accumulated product, file nearest HH:00 (window ends at label time) | Max rainfall recurrence interval. Largest recurrence interval across accumulation windows; exceptional rain |
| `MESH_00.50` | MRMS | mm | max | max over 6 files in [HH:00, HH+1:00) | Maximum estimated size of hail. Radar-estimated maximum hail diameter; direct hail damage proxy |
| `MergedAzShear0to2kmAGL_00.50` | MRMS | s-1 | max | max over 6 files in [HH:00, HH+1:00) | Azimuthal shear 0-2 km AGL. Low-level rotation; tornado potential |
| `MergedAzShear3to6kmAGL_00.50` | MRMS | s-1 | max | max over 6 files in [HH:00, HH+1:00) | Azimuthal shear 3-6 km AGL. Mid-level rotation; mesocyclone / organized convection |
| `MergedBaseReflectivity_00.50` | MRMS | dBZ | max | max over 6 files in [HH:00, HH+1:00) | Base reflectivity. Near-surface radar reflectivity from all radars; rain/hail intensity at low levels |
| `MergedReflectivityAtLowestAltitude_00.50` | MRMS | dBZ | max | max over 6 files in [HH:00, HH+1:00) | Reflectivity at lowest altitude. Reflectivity at the lowest available altitude; near-surface storm impact |
| `MergedReflectivityComposite_00.50` | MRMS | dBZ | max | max over 6 files in [HH:00, HH+1:00) | Composite reflectivity. Maximum reflectivity in the vertical column; overall storm severity |
| `MultiSensor_QPE_01H_Pass2_00.00` | MRMS | mm | max | accumulated product, file nearest HH:00 (window ends at label time) | 1-h precipitation, gauge-corrected. 1-hour accumulation (Pass 2, gauge-corrected); short-term flood potential |
| `MultiSensor_QPE_24H_Pass2_00.00` | MRMS | mm | max | accumulated product, file nearest HH:00 (window ends at label time) | 24-h precipitation, gauge-corrected. 24-hour accumulation; flooding risk |
| `MultiSensor_QPE_72H_Pass2_00.00` | MRMS | mm | max | accumulated product, file nearest HH:00 (window ends at label time) | 72-h precipitation, gauge-corrected. 72-hour accumulation; soil saturation / prolonged flood risk |
| `NLDN_CG_001min_AvgDensity_00.00` | MRMS/NLDN | flashes km-2 min-1 | max | max over 6 files in [HH:00, HH+1:00) | CG lightning density, 1-min. Cloud-to-ground flash density, 1-minute average |
| `NLDN_CG_015min_AvgDensity_00.00` | MRMS/NLDN | flashes km-2 min-1 | max | accumulated product, file nearest HH:00 (window ends at label time) | CG lightning density, 15-min. Cloud-to-ground flash density, 15-minute average |
| `PrecipRate_00.00` | MRMS | mm h-1 | max | max over 6 files in [HH:00, HH+1:00) | Precipitation rate. Instantaneous surface rain rate; flash flood / downburst risk |
| `RotationTrackML60min_00.50` | MRMS | s-1 | max | accumulated product, file nearest HH:00 (window ends at label time) | 60-min rotation track (ML-filtered). Maximum low-level shear over the past 60 min; rotation/damage swaths |
| `VII_00.50` | MRMS | kg m-2 | max | max over 6 files in [HH:00, HH+1:00) | Vertically integrated ice. Ice mass in the storm column; hail and severe convection potential |
| `VIL_Density_00.50` | MRMS | g m-3 | max | max over 6 files in [HH:00, HH+1:00) | VIL density. Vertically integrated liquid divided by echo top; downburst/hail proxy |
| `EchoTop_18_00.50_valid_frac` | MRMS | 1 |  |  | 18 dBZ echo top: valid-pixel fraction. valid-pixel fraction (0–1) |
| `EchoTop_50_00.50_valid_frac` | MRMS | 1 |  |  | 50 dBZ echo top: valid-pixel fraction. valid-pixel fraction (0–1) |
| `FLASH_QPE_ARI012H_00.00_valid_frac` | MRMS FLASH | 1 |  |  | 12-h rainfall recurrence interval: valid-pixel fraction. valid-pixel fraction (0–1) |
| `FLASH_QPE_ARI01H_00.00_valid_frac` | MRMS FLASH | 1 |  |  | 1-h rainfall recurrence interval: valid-pixel fraction. valid-pixel fraction (0–1) |
| `FLASH_QPE_ARIMAX_00.00_valid_frac` | MRMS FLASH | 1 |  |  | Max rainfall recurrence interval: valid-pixel fraction. valid-pixel fraction (0–1) |
| `MESH_00.50_valid_frac` | MRMS | 1 |  |  | Maximum estimated size of hail: valid-pixel fraction. valid-pixel fraction (0–1) |
| `MergedAzShear0to2kmAGL_00.50_valid_frac` | MRMS | 1 |  |  | Azimuthal shear 0-2 km AGL: valid-pixel fraction. valid-pixel fraction (0–1) |
| `MergedAzShear3to6kmAGL_00.50_valid_frac` | MRMS | 1 |  |  | Azimuthal shear 3-6 km AGL: valid-pixel fraction. valid-pixel fraction (0–1) |
| `MergedBaseReflectivity_00.50_valid_frac` | MRMS | 1 |  |  | Base reflectivity: valid-pixel fraction. valid-pixel fraction (0–1) |
| `MergedReflectivityAtLowestAltitude_00.50_valid_frac` | MRMS | 1 |  |  | Reflectivity at lowest altitude: valid-pixel fraction. valid-pixel fraction (0–1) |
| `MergedReflectivityComposite_00.50_valid_frac` | MRMS | 1 |  |  | Composite reflectivity: valid-pixel fraction. valid-pixel fraction (0–1) |
| `MultiSensor_QPE_01H_Pass2_00.00_valid_frac` | MRMS | 1 |  |  | 1-h precipitation, gauge-corrected: valid-pixel fraction. valid-pixel fraction (0–1) |
| `MultiSensor_QPE_24H_Pass2_00.00_valid_frac` | MRMS | 1 |  |  | 24-h precipitation, gauge-corrected: valid-pixel fraction. valid-pixel fraction (0–1) |
| `MultiSensor_QPE_72H_Pass2_00.00_valid_frac` | MRMS | 1 |  |  | 72-h precipitation, gauge-corrected: valid-pixel fraction. valid-pixel fraction (0–1) |
| `NLDN_CG_001min_AvgDensity_00.00_valid_frac` | MRMS/NLDN | 1 |  |  | CG lightning density, 1-min: valid-pixel fraction. valid-pixel fraction (0–1) |
| `NLDN_CG_015min_AvgDensity_00.00_valid_frac` | MRMS/NLDN | 1 |  |  | CG lightning density, 15-min: valid-pixel fraction. valid-pixel fraction (0–1) |
| `PrecipRate_00.00_valid_frac` | MRMS | 1 |  |  | Precipitation rate: valid-pixel fraction. valid-pixel fraction (0–1) |
| `RotationTrackML60min_00.50_valid_frac` | MRMS | 1 |  |  | 60-min rotation track (ML-filtered): valid-pixel fraction. valid-pixel fraction (0–1) |
| `VII_00.50_valid_frac` | MRMS | 1 |  |  | Vertically integrated ice: valid-pixel fraction. valid-pixel fraction (0–1) |
| `VIL_Density_00.50_valid_frac` | MRMS | 1 |  |  | VIL density: valid-pixel fraction. valid-pixel fraction (0–1) |
| `PrecipFlag_dominant` | MRMS PrecipFlag | category | argmax of class fractions | [HH:00, HH+1:00) | Dominant precipitation class. Class with the largest fraction: 0 none, 1 rain, 2 snow, 3 hail; -1 no coverage |
| `PrecipFlag_frac_hail` | MRMS PrecipFlag | 1 | mean | mean over 6 files in [HH:00, HH+1:00) | Fraction of cell with rain+hail. Share flagged as rain mixed with hail (code 7) |
| `PrecipFlag_frac_none` | MRMS PrecipFlag | 1 | mean | mean over 6 files in [HH:00, HH+1:00) | Fraction of cell with no precipitation. Share of valid pixels (averaged over the hour) flagged as no precipitation |
| `PrecipFlag_frac_rain` | MRMS PrecipFlag | 1 | mean | mean over 6 files in [HH:00, HH+1:00) | Fraction of cell with rain. Share flagged as warm/cool stratiform, convective or tropical rain (codes 1,6,10,91,96) |
| `PrecipFlag_frac_snow` | MRMS PrecipFlag | 1 | mean | mean over 6 files in [HH:00, HH+1:00) | Fraction of cell with snow. Share flagged as snow (code 3) |
| `PrecipFlag_frac_hail_valid_frac` | MRMS PrecipFlag | 1 |  |  | Fraction of cell with rain+hail: valid-pixel fraction. valid-pixel fraction (0–1) |
| `PrecipFlag_frac_none_valid_frac` | MRMS PrecipFlag | 1 |  |  | Fraction of cell with no precipitation: valid-pixel fraction. valid-pixel fraction (0–1) |
| `PrecipFlag_frac_rain_valid_frac` | MRMS PrecipFlag | 1 |  |  | Fraction of cell with rain: valid-pixel fraction. valid-pixel fraction (0–1) |
| `PrecipFlag_frac_snow_valid_frac` | MRMS PrecipFlag | 1 |  |  | Fraction of cell with snow: valid-pixel fraction. valid-pixel fraction (0–1) |
| `ceil` | RTMA | m | median | analysis valid at HH:00 | Cloud ceiling. Cloud base height; check the value used for 'no ceiling' before analysis |
| `ceil_frac_lt300m` | RTMA | 1 | frac_lt:300 | analysis valid at HH:00 | Cloud ceiling, fraction of cell below 300 m. Cloud base height; check the value used for 'no ceiling' before analysis |
| `ceil_min` | RTMA | m | min | analysis valid at HH:00 | Cloud ceiling, minimum in cell. Cloud base height; check the value used for 'no ceiling' before analysis |
| `d2m` | RTMA | K | mean | analysis valid at HH:00 | 2 m dew point. Atmospheric moisture; fog and convection potential |
| `d2m_max` | RTMA | K | max | analysis valid at HH:00 | 2 m dew point, maximum in cell. Atmospheric moisture; fog and convection potential |
| `i10fg` | RTMA | m s-1 | max | analysis valid at HH:00 | 10 m wind gust. Peak gust; main driver of wind damage/outages |
| `orog` | RTMA | m | mean |  | Surface elevation. Static terrain height (no time dimension) |
| `sh2` | RTMA | kg kg-1 | mean | analysis valid at HH:00 | 2 m specific humidity. Absolute moisture content |
| `sh2_max` | RTMA | kg kg-1 | max | analysis valid at HH:00 | 2 m specific humidity, maximum in cell. Absolute moisture content |
| `sp` | RTMA | Pa | mean | analysis valid at HH:00 | Surface pressure. Station-level pressure; varies mostly with elevation, compare with orog |
| `t2m` | RTMA | K | mean | analysis valid at HH:00 | 2 m air temperature. Surface thermal state; heat, icing, rain/snow phase |
| `t2m_max` | RTMA | K | max | analysis valid at HH:00 | 2 m air temperature, maximum in cell. Surface thermal state; heat, icing, rain/snow phase |
| `t2m_min` | RTMA | K | min | analysis valid at HH:00 | 2 m air temperature, minimum in cell. Surface thermal state; heat, icing, rain/snow phase |
| `tcc` | RTMA | % | mean | analysis valid at HH:00 | Total cloud cover. Cloud fraction |
| `u10` | RTMA | m s-1 | mean | analysis valid at HH:00 | 10 m eastward wind. Earth-relative, rebuilt from wspd10/wdir10 at 2.5 km, then cell mean |
| `v10` | RTMA | m s-1 | mean | analysis valid at HH:00 | 10 m northward wind. Earth-relative, rebuilt from wspd10/wdir10 at 2.5 km, then cell mean |
| `vis` | RTMA | m | median | analysis valid at HH:00 | Visibility. Surface visibility; fog, heavy precipitation, smoke |
| `vis_frac_lt1km` | RTMA | 1 | frac_lt:1000 | analysis valid at HH:00 | Visibility, fraction of cell below 1 km. Surface visibility; fog, heavy precipitation, smoke |
| `vis_min` | RTMA | m | min | analysis valid at HH:00 | Visibility, minimum in cell. Surface visibility; fog, heavy precipitation, smoke |
| `wdir10` | RTMA | degree | derived from cell-mean u10/v10 | analysis valid at HH:00 | 10 m wind direction (from). Meteorological direction computed from cell-mean u10/v10 |
| `wspd10` | RTMA | m s-1 | max | analysis valid at HH:00 | 10 m wind speed. Sustained wind |
| `rtma_valid_frac` | RTMA | 1 |  | analysis valid at HH:00 | RTMA valid-pixel fraction. valid-pixel fraction (0–1) |
| `MAXSIZE` | SWDI nx3hail | as in SWDI (NEXRAD L3) | see aggregate_swdi.py | detections within the hour (see aggregate_swdi.py) | Maximum expected hail size. Hail detection maximum size estimate |
| `MAX_REFLECT` | SWDI nx3structure | dBZ | see aggregate_swdi.py | detections within the hour (see aggregate_swdi.py) | Storm-cell max reflectivity. Peak reflectivity of storm cells |
| `MAX_SHEAR` | SWDI nx3tvs | as in SWDI (NEXRAD L3) | see aggregate_swdi.py | detections within the hour (see aggregate_swdi.py) | TVS maximum shear. Strongest rotation signature of tornado vortex detections in the cell |
| `MXDV` | SWDI nx3tvs | as in SWDI (NEXRAD L3) | see aggregate_swdi.py | detections within the hour (see aggregate_swdi.py) | TVS maximum delta velocity. Maximum gate-to-gate velocity difference of TVS detections |
| `PROB` | SWDI nx3hail | % | see aggregate_swdi.py | detections within the hour (see aggregate_swdi.py) | Probability of hail. NEXRAD hail detection probability |
| `VIL` | SWDI nx3structure | kg m-2 | see aggregate_swdi.py | detections within the hour (see aggregate_swdi.py) | Storm-cell VIL. Vertically integrated liquid of storm cells |
| `lat` | grid | degrees_north |  |  | Latitude of cell center. WGS84 |
| `lon` | grid | degrees_east |  |  | Longitude of cell center. WGS84 |
