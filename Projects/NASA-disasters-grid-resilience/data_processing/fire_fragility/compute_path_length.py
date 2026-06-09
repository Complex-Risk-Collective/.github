import math
import numpy as np
import rasterio


def neighbor_offsets():
    return [
        (-1, -1), (-1, 0), (-1, 1),
        (0, -1),           (0, 1),
        (1, -1),  (1, 0),  (1, 1),
    ]


def step_distance(dr, dc, xres, yres):
    return math.sqrt((dr * yres) ** 2 + (dc * xres) ** 2)


def in_bounds(r, c, rows, cols):
    return (0 <= r < rows) and (0 <= c < cols)


def trace_one_path(toa, start_r, start_c, xres, yres, patience, nodata_mask,
                   ignition_tolerance=1e-9, max_steps=None):
    rows, cols = toa.shape
    if max_steps is None:
        max_steps = rows * cols

    if nodata_mask[start_r, start_c]:
        return 0

    current = (start_r, start_c)
    visited = set()
    total_length = 0.0
    nodata_steps_used = 0

    for _ in range(max_steps):
        r, c = current

        if current in visited:
            return 0  # cycle detected
        visited.add(current)

        tcur = toa[r, c]

        # Treat near-zero / minimum time as ignition origin
        if np.isfinite(tcur) and tcur <= ignition_tolerance:
            return total_length

        best_valid = None
        best_valid_t = np.inf
        best_valid_dist = None

        best_nodata = None
        best_nodata_dist = None

        # Examine 8-connected neighbors
        for dr, dc in neighbor_offsets():
            nr, nc = r + dr, c + dc
            if not in_bounds(nr, nc, rows, cols):
                continue

            dist = step_distance(dr, dc, xres, yres)

            if nodata_mask[nr, nc]:
                if best_nodata is None:
                    best_nodata = (nr, nc)
                    best_nodata_dist = dist
                continue

            tnext = toa[nr, nc]

            # Enforce monotonic descent in time
            if np.isfinite(tnext) and tnext < tcur and tnext < best_valid_t:
                best_valid = (nr, nc)
                best_valid_t = tnext
                best_valid_dist = dist

        if best_valid is not None:
            total_length += best_valid_dist
            current = best_valid
            continue

        # No valid downhill neighbor; optionally traverse nodata gap
        if best_nodata is not None and nodata_steps_used < patience:
            total_length += best_nodata_dist
            current = best_nodata
            nodata_steps_used += 1
            continue

        # If we are currently on nodata after gap traversal, try to recover by
        # moving to any valid neighbor with the lowest TOA
        if nodata_mask[r, c]:
            recovery = None
            recovery_t = np.inf
            recovery_dist = None

            for dr, dc in neighbor_offsets():
                nr, nc = r + dr, c + dc
                if not in_bounds(nr, nc, rows, cols):
                    continue
                if nodata_mask[nr, nc]:
                    continue

                tnext = toa[nr, nc]
                if np.isfinite(tnext) and tnext < recovery_t:
                    recovery = (nr, nc)
                    recovery_t = tnext
                    recovery_dist = step_distance(dr, dc, xres, yres)

            if recovery is not None:
                total_length += recovery_dist
                current = recovery
                continue

        # No way forward
        return total_length

    return 0  # exceeded max_steps


def compute_fire_path_lengths(toa, xres, yres, patience, nodata_mask,
                              ignition_tolerance=1e-9):
    rows, cols = toa.shape
    out = np.full((rows, cols), 0, dtype=np.float32)

    for r in range(rows):
        for c in range(cols):
            if nodata_mask[r, c]:
                continue
            if not np.isfinite(toa[r, c]):
                continue

            out[r, c] = trace_one_path(
                toa, r, c,
                xres=xres, yres=yres,
                patience=patience,
                nodata_mask=nodata_mask,
                ignition_tolerance=ignition_tolerance
            )

    return out


if __name__ == "__main__":
    toa_path = "/Users/nlahaye/fire_spread_compare/fresno-june-lc-run-nlahaye_20240626_120000_exporter_f1cb179721adebd1896a9f1a4561848f/pyretechnics-deck/time-of-arrival/time-of-arrival_combined.tif"
    out_path = "fire_path_length.tif"

    # Number of nodata cells the backtrace is allowed to cross consecutively
    patience = 0

    with rasterio.open(toa_path) as src:
        toa = src.read(1).astype("float64")
        profile = src.profile.copy()
        transform = src.transform
        nodata = src.nodata

        xres = abs(transform.a)
        yres = abs(transform.e)

        nodata_mask = np.zeros(toa.shape, dtype=bool)
        if nodata is not None:
            nodata_mask |= (toa == nodata)
        nodata_mask |= ~np.isfinite(toa)

        path_lengths = compute_fire_path_lengths(
            toa=toa,
            xres=xres,
            yres=yres,
            patience=patience,
            nodata_mask=nodata_mask,
            ignition_tolerance=1e-9
        )

        profile.update(dtype="float32", count=1, nodata=0)

        with rasterio.open(out_path, "w", **profile) as dst:
            dst.write(path_lengths.astype("float32"), 1)

    print(f"Wrote {out_path}")




