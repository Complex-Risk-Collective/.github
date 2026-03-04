#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Created on Fri Feb 27 10:39:02 2026

@author: marchett
"""

import os
from datetime import datetime, timedelta
import xarray as xr
from get_swdi_data import download_swdi
from aggregate_swdi import aggregate_swdi
from grid import AnalysisGridTemplate
from get_aggregate_mrms_data import download_and_grid
from get_aggregate_rtma_data import download_and_grid_rtma
from pathlib import Path


# ------------------------
# CONFIG
# ------------------------

PATH = "/Users/marchett/Documents/Disasters/data"
region = "CONUS"

lat_bounds = [25, 50]
lon_bounds = [-125, -65]

swdi_variables = ["nx3tvs", "nx3hail", "nx3structure"]

mrms_variables = [
    "MergedBaseReflectivity_00.50", # rain/hail intensity at low levels, near surface, all radars
    "MergedReflectivityComposite_00.50", # max reflectivity in the vertical column, overall severity
    "MergedReflectivityAtLowestAltitude_00.50", # intensity at fixed hight, ~1km
    "FLASH_QPE_ARI01H_00.00", # quantitative precipitation estimates at 1H, extreme rain
    "FLASH_QPE_ARI012H_00.00", # quantitative precipitation estimates at 12H, extreme rain
    "FLASH_QPE_ARIMAX_00.00", # quantitative precipitation estimates, extreme rain
    "MultiSensor_QPE_01H_Pass2_00.00", # precipitation accumulation 1h, gauge corrected
    "MultiSensor_QPE_24H_Pass2_00.00", # precipitation accumulation 72hours
    "MultiSensor_QPE_72H_Pass2_00.00", # precipitation accumulation 72hours
    "MergedAzShear0to2kmAGL_00.50", # low level rotational sheer, tornado potential
    "MergedAzShear3to6kmAGL_00.50", # low level rotational sheer, tornado potential
    "RotationTrackML60min_00.50", # max rotational shear/change in radial velocity
    "MESH_00.50", # maximum estimated size of hail inches
    "VII_00.50", # vertically integrated ice
    "VIL_Density_00.50", # virtically integraed liquid, intensity per vertical depth
    "PrecipRate_00.00", # instant rainfall intensity at surface
    "NLDN_CG_001min_AvgDensity_00.00", # cloud to ground lightning flash density
    "NLDN_CG_015min_AvgDensity_00.00", # cloud to ground lightning flash density
    "EchoTop_18_00.50", # 
    "EchoTop_50_00.50", # 
    "PrecipFlag_00.00", # precipitation type flag
]


# ------------------------
# GRID TEMPLATE
# ------------------------

grid_template = AnalysisGridTemplate(
    grid_file=f"{PATH}/analysis_grid_{region}_50km.nc",
    lat_bounds=lat_bounds,
    lon_bounds=lon_bounds,
    res_km=50,
    epsg=5070
)



# ========================
# PROCESS ONE DAY
# ========================

def process_one_day(date):

    day_start = datetime(date.year, date.month, date.day, 0, 0)
    day_end   = datetime(date.year, date.month, date.day, 23, 0)

    start_str = day_start.strftime("%Y-%m-%d %H:%M")
    end_str   = day_end.strftime("%Y-%m-%d %H:%M")
    date_tag  = day_start.strftime("%Y%m%d")

    output_file = f"{PATH}/daily/weather_{region}_{date_tag}.nc"

    if os.path.exists(output_file):
        print(f"✓ Already exists: {output_file}")
        return

    print(f"\nProcessing {date_tag}")

    # ------------------------
    # SWDI
    # ------------------------
    
    swdi_outdir = Path(PATH) / "swdi" / "swdi_raw" 
    swdi_outdir.mkdir(parents=True, exist_ok=True)

    print("Downloading SWDI...")
    download_swdi(
        datasets=swdi_variables,
        fmt='csv',
        start=day_start,
        end=day_end,
        outdir=swdi_outdir
    )


    swdi_datasets = []
    swdi_vars_to_keep = {
        "nx3tvs": ["MAX_SHEAR","MXDV"],
        "nx3hail": ["PROB","MAXSIZE"],
        "nx3structure": ["VIL","MAX_REFLECT"]
    }
    
    print("Aggregating SWDI hourly...")
    swdi_datasets = []
    for product in swdi_variables:
        
        vars_to_keep = swdi_vars_to_keep.get(product)
        ds_product = aggregate_swdi(
            csv_dir=swdi_outdir,
            product_type=product,
            date=day_start,
            grid_template=grid_template,
            variables=vars_to_keep,
        )
        # Drop lat/lon to avoid conflicts when merging
        # ds_product = ds_product.drop_vars(['lat','lon'])
        ds_product = ds_product.drop_vars(['lat','lon'], errors='ignore')
        swdi_datasets.append(ds_product)

    # Merge all SWDI products
    ds_swdi = xr.merge(swdi_datasets)
    ds_swdi = ds_swdi.rename({
        "ZTIME": "time",
        "Y": "y",
        "X": "x"
    })
    
    # Drop duplicate lat/lon if present
    ds_swdi = ds_swdi.drop_vars(["LAT", "LON"], errors="ignore")
    
    
    # ------------------------
    # MRMS
    # ------------------------

    print("Downloading & aggregating MRMS...")
    ds_mrms = download_and_grid(
        root=PATH,
        region=region,
        grid=grid_template,
        variables=mrms_variables,
        start_date=start_str,
        end_date=end_str,
        lat_bounds=lat_bounds,
        lon_bounds=lon_bounds,
        output_file=None
    )

    
       
    # ------------------------
    # RTMA
    # ------------------------
    
    print("Downloading & aggregating RTMA...")
    ds_rtma = download_and_grid_rtma(
            day_start,
            day_end,
            grid_template,
            root=PATH
        )
    
    ds_rtma = ds_rtma.drop_vars(["latitude", "longitude"], errors="ignore")
    
    # ------------------------
    # MERGE & SAVE DAILY
    # ------------------------

    print("Merging datasets...")
    datasets_to_merge = [ds for ds in [ds_swdi, ds_mrms, ds_rtma] if ds is not None]
    ds_daily = xr.merge(datasets_to_merge, compat='override')

    ds_daily.attrs["region"] = region
    ds_daily.attrs["date"] = date_tag
    ds_daily.attrs["created"] = datetime.utcnow().isoformat()

    os.makedirs(f"{PATH}/daily", exist_ok=True)

    encoding = {
        var: {"zlib": True, "complevel": 4}
        for var in ds_daily.data_vars
    }
    
    ds_daily.to_netcdf(output_file, encoding=encoding)
    
    print(f"Saved {output_file}")
    
    # Clean up memory
    del ds_swdi, ds_mrms, ds_daily
    
    
    



# ========================
# RUN RANGE
# ========================

def run_range(start_date, end_date):

    current = start_date

    while current <= end_date:
        try:
            process_one_day(current)
        except Exception as e:
            print(f"Failed on {current}: {e}")

        current += timedelta(days=1)


if __name__ == "__main__":

    start = datetime(2024, 6, 1)
    end   = datetime(2024, 6, 30)

    run_range(start, end)
    
    

