### Terrestrial multi-hazard analysis

Hourly MRMS, RTMA and SWDI data on a 50 km CONUS grid. Run the steps in order;
each reads the previous step's output.

```bash
conda env create -f multi-haz.yml && conda activate multi-haz
```

1. **Get and bin data on a grid**:
   Set `PATH` and the dates first. Writes `daily/weather_<region>_YYYYMMDD.nc`.
   (e.g. `/Volumes/MLIA_active_data/gridresilience/data/terrestrial/V2`)
   Variables are described in `DATA_DICTIONARY.md`.
   Temperatures (`t2m*`, `d2m*`, `heat_index_max`) are stored in °C. Existing
   Kelvin files are converted in place on the next run, without re-downloading.
   
   ```bash
   python pipeline.py
   ```
   

2. **Exploratory data analysis**: 
    Data quality checks first, summary statistics and maps; open report.html
    One page per hazard overview then; open overview.html
    Both accept --start and --end (YYYY-MM-DD)
   
     
   ```bash
   python eda_daily.py --daily-dir <PATH>/daily --out <PATH>/eda
   python eda_overview.py --daily-dir <PATH>/daily --out <PATH>/eda_overview
   ```

3. **Create hazard flag files**:
   Writes hourly flags per hazard (1 present, 0 absent, -1 no data).
   Hazard variables and thresholds are in `hazards.yml` (temperatures in °C);
   use `--config` for another file. Use `--overwrite` after changing them.
   A copy is saved next to the flags as `hazards_used.yml`. Needs `pyyaml`.
   
   Hazards are defined based on variables -- definition can be changed.
   
     
   ```bash
   python hazard_flags.py --daily_dir <PATH>/daily --out_dir <PATH>/hazard_flags
   ```

4. **Single-hazard maps**: 
   Maps of days with each hazard and the fraction of valid hours flagged.
   
     
   ```bash
   python hazard_maps.py --flag_dir <PATH>/hazard_flags --out_dir <PATH>/hazard_maps
   ```

5. **Co-occurrence**: next step, not written yet.


