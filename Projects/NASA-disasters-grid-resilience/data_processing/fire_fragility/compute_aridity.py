import numpy as np
import rasterio
#from rasterio.enums import Resampling
from rasterio.warp import reproject, Resampling
 

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


def compute_aridity(precip, potential_evapotransp, precip_nodata=None, potential_evapotransp_nodata=None):
    mask = np.zeros(precip.shape, dtype=bool)

    if precip_nodata is not None:
        mask |= (precip == precip_nodata)
    if potential_evapotransp_nodata is not None:
        mask |= (potential_evapotransp == potential_evapotransp_nodata)

    # Handle zeros
    mask |= (potential_evapotransp <= 0)

    precip = np.where(mask, -1, precip)
    potential_evapotransp = np.where(mask, -1, potential_evapotransp)

    aridity_index = precip / potential_evapotransp
    return aridity_index


def compute_veg_aridity(aridity_index, ndvi, ndvi_nodata=None):
    mask = aridity_index < 0
    if ndvi_nodata is not None:
        mask |= (ndvi == ndvi_nodata)

    ndvi = np.where(mask, -1, ndvi)
    # Example metric: higher potential_evapotransp relative to precip ->  more arid
    # aridity_index = precip / potential_evapotransp, so potential_evapotransp / precip = 1 / aridity_index
    with np.errstate(divide="ignore", invalid="ignore"):
        inv_aridity_index = 1.0 / aridity_index
    inv_aridity_index[np.isinf(inv_aridity_index)] = -1

    Varidity_index = ndvi * inv_aridity_index
    Varidity_index[mask] = -1
    return Varidity_index


def normalize(data):
    return (data - 0) / (256 - 0)

if __name__ == "__main__":
    # Input rasters (annual or climatological means for CONUS)
    precip_path = "/Users/nlahaye/Downloads/GRIDMET-precip-12mo.tif"  # mean annual precip
    pet_path = "/Users/nlahaye/Downloads/m202406/m202406_viirsSSEBopETv61_actual_mm.tif"        # mean annual potential_evapotransp or ET0
    ndvi_path = "/Users/nlahaye/Downloads/c_gls_NDVI300_202406210000_GLOBE_OLCI_V3.0.1_cog/c_gls_NDVI300-NDVI_202406210000_GLOBE_OLCI_V3.0.1.tiff"      # annual mean ndvi (0–1 or -1–1)
    ref = "/Users/nlahaye/fire_spread_compare/fresno-june-lc-run-nlahaye_20240626_120000_exporter_f1cb179721adebd1896a9f1a4561848f/pyretechnics-deck/flame-length/flame-length_99_012.tif"

    potential_evapotransp_nodata = -9999
    ndvi_nodata = 255
    precip_nodata = None

    ref_flm, profile, flm_nodata = read_band(ref)

    # Read reference (precip) and profile
    precip, precip_nodata = align_to_reference(profile, precip_path)
    

    # Align potential_evapotransp and ndvi to precip grid
    potential_evapotransp, potential_evapotransp_nodata = align_to_reference(profile, pet_path)
    ndvi, ndvi_nodata = align_to_reference(profile, ndvi_path)

    # Compute Aridity Index (aridity_index = precip / potential_evapotransp)
    aridity_index = compute_aridity(precip, potential_evapotransp, precip_nodata, potential_evapotransp_nodata)

    # Compute a vegetation aridity metric
    Varidity_index = compute_veg_aridity(aridity_index, ndvi, ndvi_nodata)

    # preciprepare output profile
    out_profile = profile.copy()
    out_profile.update(
        dtype="float32",
        count=1,
        nodata=-1
    )

    aridity_index = normalize(aridity_index.astype("float32"))
    Varidity_index = normalize(Varidity_index.astype("float32"))

    # Write aridity_index raster
    with rasterio.open("aridity_index_ai.tif", "w", **out_profile) as dst:
        dst.write(aridity_index, 1)

    # Write Vegetation Aridity raster
    with rasterio.open("vegetation_aridity_vai.tif", "w", **out_profile) as dst:
        dst.write(Varidity_index, 1)

    print("Wrote aridity_index_ai.tif and vegetation_aridity_vai.tif")
