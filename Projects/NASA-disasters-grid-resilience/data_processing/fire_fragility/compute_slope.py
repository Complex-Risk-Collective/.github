import numpy as np
import rasterio

input_dem = "srtm_dem.tif"
output_slope = "srtm_slope_degrees.tif"


def compute_slope_horn(dem, xres, yres, nodata=None):
    dem = dem.astype("float32")

    if nodata is not None:
        dem = np.where(dem == nodata, np.nan, dem)

    # Pad edges so output keeps same size
    z = np.pad(dem, 1, mode="edge")

    a = z[:-2, :-2]
    b = z[:-2, 1:-1]
    c = z[:-2, 2:]
    d = z[1:-1, :-2]
    e = z[1:-1, 1:-1]
    f = z[1:-1, 2:]
    g = z[2:, :-2]
    h = z[2:, 1:-1]
    q = z[2:, 2:]

    # Horn (1981) style derivatives
    dz_dx = ((c + 2 * f + q) - (a + 2 * d + g)) / (8 * xres)
    dz_dy = ((g + 2 * h + q) - (a + 2 * b + c)) / (8 * yres)

    rise_run = np.sqrt(dz_dx**2 + dz_dy**2)
    slope_deg = np.degrees(np.arctan(rise_run))

    # Mask cells influenced by nodata
    invalid = np.isnan(a) | np.isnan(b) | np.isnan(c) | np.isnan(d) | np.isnan(e) | np.isnan(f) | np.isnan(g) | np.isnan(h) | np.isnan(q)
    slope_deg[invalid] = np.nan

    return slope_deg



with rasterio.open(input_dem) as src:
    dem = src.read(1)
    transform = src.transform
    profile = src.profile.copy()
    nodata = src.nodata

    xres = transform.a
    yres = abs(transform.e)

    slope = compute_slope_horn(dem, xres, yres, nodata=nodata)

    profile.update(
        dtype="float32",
        count=1,
        nodata=np.nan
    )

    with rasterio.open(output_slope, "w", **profile) as dst:
        dst.write(slope.astype("float32"), 1)

print(f"Wrote slope raster to: {output_slope}")
