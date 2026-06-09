import math
import numpy as np
import rasterio
from rasterio.warp import transform


def vector_length(vx, vy):
    return math.sqrt(vx * vx + vy * vy)


def acute_angle_deg(v1x, v1y, v2x, v2y):
    n1 = vector_length(v1x, v1y)
    n2 = vector_length(v2x, v2y)

    if n1 == 0 or n2 == 0:
        return np.nan

    dot = v1x * v2x + v1y * v2y
    cross = v1x * v2y - v1y * v2x

    angle = math.degrees(math.atan2(abs(cross), dot))  # 0..180
    if angle > 90.0:
        angle = 180.0 - angle
    return angle


def compute_toa_gradient(toa, xres, yres):
    gy, gx = np.gradient(toa, yres, xres)
    return gx, gy


def transform_lonlat_to_raster_crs(lon, lat, dst_crs):
    xs, ys = transform("EPSG:4326", dst_crs, [lon], [lat])
    return xs[0], ys[0]


def read_raster(path):
    with rasterio.open(path) as src:
        arr = src.read(1).astype("float64")
        nodata = src.nodata
        profile = src.profile.copy()
        transform_affine = src.transform
        crs = src.crs

    if nodata is not None:
        arr = np.where(arr == nodata, np.nan, arr)
    arr = np.where(np.isfinite(arr), arr, np.nan)

    return arr, profile, transform_affine, crs


def main():
    toa_raster = "/Users/nlahaye/fire_spread_compare/fresno-june-lc-run-nlahaye_20240626_120000_exporter_f1cb179721adebd1896a9f1a4561848f/pyretechnics-deck/time-of-arrival/time-of-arrival_combined.tif"
    flame_length_raster = "/Users/nlahaye/fire_spread_compare/fresno-june-lc-run-nlahaye_20240626_120000_exporter_f1cb179721adebd1896a9f1a4561848f/pyretechnics-deck/flame-length/flame-length_99_012.tif"
 
    # Conductor endpoints in lon/lat (lon, lat)
    conductor_p1_lonlat = (-119.244, 36.789)
    conductor_p2_lonlat = (-119.244, 36.7889)

    # Conductor height above ground in meters
    conductor_height_m = 50.0

    toa, profile, transform_affine, raster_crs = read_raster(toa_raster)
    flame_length, _, _, flame_crs = read_raster(flame_length_raster)

    if raster_crs is None:
        raise ValueError("TOA raster has no CRS.")

    if flame_length.shape != toa.shape:
        raise ValueError("Flame length raster must match TOA raster shape.")

    xres = abs(transform_affine.a)
    yres = abs(transform_affine.e)

    # Transform conductor points from lon/lat to raster CRS
    p1x, p1y = transform_lonlat_to_raster_crs(
        conductor_p1_lonlat[0], conductor_p1_lonlat[1], raster_crs
    )
    p2x, p2y = transform_lonlat_to_raster_crs(
        conductor_p2_lonlat[0], conductor_p2_lonlat[1], raster_crs
    )

    conductor_vx = p2x - p1x
    conductor_vy = p2y - p1y

    gx, gy = compute_toa_gradient(toa, xres, yres)

    # Fire-front tangent from TOA gradient
    front_tx = -gy
    front_ty = gx

    angle_deg = np.full(toa.shape, np.nan, dtype="float32")
    flame_reach_ratio = np.full(toa.shape, np.nan, dtype="float32")
    reach_flag = np.full(toa.shape, np.nan, dtype="float32")
    exposure_score = np.full(toa.shape, np.nan, dtype="float32")

    rows, cols = toa.shape
    for r in range(rows):
        for c in range(cols):
            if not np.isfinite(toa[r, c]):
                continue
            if not np.isfinite(front_tx[r, c]) or not np.isfinite(front_ty[r, c]):
                continue
            if not np.isfinite(flame_length[r, c]):
                continue
            if flame_length[r, c] < 0:
                continue

            # 1) Horizontal angle between local front and conductor
            ang = acute_angle_deg(
                conductor_vx, conductor_vy,
                front_tx[r, c], front_ty[r, c]
            )
            angle_deg[r, c] = ang

            # 2) Vertical reach relative to conductor height
            # reach_ratio >= 1 means flame length equals/exceeds conductor height
            reach_ratio = flame_length[r, c] / conductor_height_m
            flame_reach_ratio[r, c] = reach_ratio

            # Binary reach flag
            reach_flag[r, c] = 1.0 if reach_ratio >= 1.0 else 0.0

            # 3) Simple combined exposure score
            # Higher when flame can reach conductor and fire front is more perpendicular
            # Normalize angle so 0 = parallel, 1 = perpendicular
            angle_factor = ang / 90.0 if np.isfinite(ang) else np.nan

            # Cap reach contribution at 1 for screening metric
            reach_factor = min(reach_ratio, 1.0)

            exposure_score[r, c] = angle_factor * reach_factor

    out_profile = profile.copy()
    out_profile.update(dtype="float32", count=1, nodata=np.nan)

    with rasterio.open("fire_conductor_angle_deg.tif", "w", **out_profile) as dst:
        dst.write(angle_deg, 1)

    with rasterio.open("flame_reach_ratio.tif", "w", **out_profile) as dst:
        dst.write(flame_reach_ratio, 1)

    with rasterio.open("flame_reaches_conductor.tif", "w", **out_profile) as dst:
        dst.write(reach_flag, 1)

    with rasterio.open("fire_conductor_exposure_score.tif", "w", **out_profile) as dst:
        dst.write(exposure_score, 1)

    print("Wrote:")
    print("  fire_conductor_angle_deg.tif")
    print("  flame_reach_ratio.tif")
    print("  flame_reaches_conductor.tif")
    print("  fire_conductor_exposure_score.tif")


if __name__ == "__main__":
    main()
