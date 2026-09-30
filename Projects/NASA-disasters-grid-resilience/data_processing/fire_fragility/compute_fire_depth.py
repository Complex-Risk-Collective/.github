import numpy as np
import rasterio


def compute_fire_depth(toa, xres, yres, residence_time_s):
    """
    Compute fire depth from a time-of-arrival raster and flame residence time.

    Parameters
    ----------
    toa : 2D numpy array
        Time of arrival raster in seconds.
    xres, yres : float
        Raster cell size in meters.
    residence_time_s : float
        Flame residence time in seconds.

    Returns
    -------
    depth : 2D numpy array
        Fire depth in meters.
    ros : 2D numpy array
        Rate of spread in m/s.
    """
    # Gradient of arrival time: seconds per meter
    dTdy, dTdx = np.gradient(toa, yres, xres)

    grad_mag = np.sqrt(dTdx**2 + dTdy**2)

    # Avoid divide-by-zero
    with np.errstate(divide="ignore", invalid="ignore"):
        ros = 1.0 / grad_mag

    ros[~np.isfinite(ros)] = np.nan
    ros[grad_mag <= 0] = np.nan

    depth = ros * residence_time_s
    return depth, ros


def main():
    toa_raster =  "/Users/nlahaye/fire_spread_compare/fresno-june-lc-run-nlahaye_20240626_120000_exporter_f1cb179721adebd1896a9f1a4561848f/pyretechnics-deck/time-of-arrival/time-of-arrival_combined.tif"

    # Example flame residence time in seconds
    residence_time_s = 25.0

    with rasterio.open(toa_raster) as src:
        toa = src.read(1).astype("float64")
        nodata = src.nodata
        profile = src.profile.copy()
        transform = src.transform

        if nodata is not None:
            toa = np.where(toa == nodata, np.nan, toa)
        toa = np.where(np.isfinite(toa), toa, np.nan)

        xres = abs(transform.a)
        yres = abs(transform.e)

        depth, ros = compute_fire_depth(toa, xres, yres, residence_time_s)

        out_profile = profile.copy()
        out_profile.update(dtype="float32", count=1, nodata=np.nan)

        with rasterio.open("fire_depth_m.tif", "w", **out_profile) as dst:
            dst.write(depth.astype("float32"), 1)


    print("Wrote fire_depth_m.tif")


if __name__ == "__main__":
    main()
