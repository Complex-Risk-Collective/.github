import numpy as np
import pandas as pd
import rasterio

from rasterio.warp import reproject, Resampling

from fuel_bed_lookup import FUEL_BED_LOOKUP
  
fbfm40_path = "/Users/nlahaye/Downloads/LF2024_FBFM40_CONUS/Tif/LF2024_FBFM40_CONUS.tif"
output_raster = "fuelbed_depth.tif"
ref = "/Users/nlahaye/fire_spread_compare/fresno-june-lc-run-nlahaye_20240626_120000_exporter_f1cb179721adebd1896a9f1a4561848f/pyretechnics-deck/flame-length/flame-length_99_012.tif"

depth_map = FUEL_BED_LOOKUP

def read_band(path):
     src = rasterio.open(path)
     arr = src.read(1).astype("float32")
     profile = src.profile
     nodata = src.nodata
     src.close()
     return arr, profile, nodata
 

def align_to_reference(ref_profile, path):
     """Resample 'path' raster to match ref_profile (if needed)."""
     with rasterio.open(path) as src:
         if (src.width == ref_profile["width"] and
             src.height == ref_profile["height"] and
             src.transform == ref_profile["transform"] and
             src.crs == ref_profile["crs"]):
             arr = src.read(1).astype("float32")
             nodata = src.nodata
             return arr, nodata
 
         dst_data = np.empty((src.count, ref_profile["height"], ref_profile["width"]), dtype=src.dtypes[0])
         reproject(
             source=rasterio.band(src, 1),
             destination=dst_data[0],
             src_transform=src.transform,
             src_crs=src.crs,
             src_nodata=src.nodata,
             dst_transform=ref_profile["transform"],
             dst_crs=ref_profile["crs"],
             dst_nodata=src.nodata,
             resampling=Resampling.cubic,  # or nearest, cubic, etc.
         )
 
         arr = dst_data[0]
         nodata = src.nodata
         return arr, nodata



ref_flm, profile, flm_nodata = read_band(ref)
fbfm40, fbfm40_nodata = align_to_reference(profile, fbfm40_path)



# Output initialized to nan
out = np.full(fbfm40.shape, -1, dtype=np.float32)
 
# Mask valid pixels
if fbfm40_nodata is not None:
    valid = fbfm40 != fbfm40_nodata
else:
    valid = np.ones(fbfm40.shape, dtype=bool)

# Get unique FBFM40 IDs present
ids = np.unique(fbfm40[valid]).astype(int)
 
# Assign depth by lookup
for fbfm40id in ids:
    if fbfm40id in depth_map:
        out[fbfm40 == fbfm40id] = depth_map[fbfm40id]
 
profile.update(dtype="float32", count=1, nodata=-1) 

with rasterio.open(output_raster, "w", **profile) as dst:
    dst.write(out, 1)

print(f"Wrote {output_raster}")



