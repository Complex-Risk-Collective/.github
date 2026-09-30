#!/usr/bin/env python3
"""
Build individual fire events from MODIS MCD64A1 Collection 6.1 burned area
files downloaded directly from NASA LAADS DAAC, then associate NASA FIRMS
MODIS active-fire detections with the final events.

This is an AOI-scale, batch implementation of the essential GlobFire approach
in Artés et al. (2019): daily burn patches are connected into an event when
patches are spatially close and their burn dates are <= 5 days apart. Final
fire events smaller than 5 km2 are discarded.

Install:
    pip install geopandas rasterio shapely pandas numpy requests

Requirements:
  1. Create an Earthdata Login token and export it before running:
       export EARTHDATA_TOKEN='your_token'

     Windows PowerShell:
       $env:EARTHDATA_TOKEN='your_token'

  2. Get a NASA FIRMS MAP_KEY and set FIRMS_MAP_KEY below.

  3. Provide an AOI polygon file in EPSG:4326.

  4. Set the MCD64A1 MODIS sinusoidal tiles intersecting the AOI. The LAADS
     archive is organized by date and MODIS tile, so this explicit list avoids
     accidentally downloading the full global product.

Outputs:
  output/mcd64a1_hdf/               Downloaded MCD64A1 HDF granules
  output/event_daily_patches.gpkg   Daily patches and event identifiers
  output/fire_events.gpkg           Final events >= 5 km2
  output/firms_active_fires.csv     Downloaded FIRMS records
  output/firms_event_matches.gpkg   FIRMS detections paired to events
"""

from __future__ import annotations

import os
import re
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable

import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
import requests
from rasterio.features import shapes
from rasterio.warp import transform_bounds
from rasterio.windows import Window, from_bounds
from shapely.geometry import shape
from shapely.ops import unary_union


# =============================================================================
# User configuration
# =============================================================================

AOI_FILE = "/mnt/data1/MultiHazard/data/NERC_Reliability_Coordinators.geojson"
START_DATE = "2023-01-01"
END_DATE = "2026-07-31"
OUTPUT_DIR = Path("modis_fire_events")

# Replace with the MODIS sinusoidal tiles that cover the AOI.
# Example only: h08v05. A large AOI can require multiple tiles.
MODIS_TILES = ["h08v05"]

# NASA FIRMS configuration.
FIRMS_MAP_KEY = "PASTE_YOUR_FIRMS_MAP_KEY_HERE"
FIRMS_SOURCE = "VIIRS_NOAA21_NRT" 

# Artés et al. / GlobFire-style fire-event linkage settings.
TIME_LINK_DAYS = 5
SPATIAL_LINK_M = 1110.0
MIN_EVENT_AREA_KM2 = 5.0

# FIRMS association settings.
FIRMS_BUFFER_M = 1500.0

HTTP_TIMEOUT_S = 180

# Equal-area CRS for metric distances and areas.
WORK_CRS = "EPSG:6933"

# NASA LAADS MCD64A1 Collection 6.1 archive.
LAADS_BASE = (
    "https://ladsweb.modaps.eosdis.nasa.gov/"
    "archive/allData/61/MCD64A1"
)


# =============================================================================
# Union-Find structure
# =============================================================================

class UnionFind:
    def __init__(self, n: int):
        self.parent = list(range(n))
        self.rank = [0] * n

    def find(self, x: int) -> int:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        root_a = self.find(a)
        root_b = self.find(b)

        if root_a == root_b:
            return

        if self.rank[root_a] < self.rank[root_b]:
            root_a, root_b = root_b, root_a

        self.parent[root_b] = root_a

        if self.rank[root_a] == self.rank[root_b]:
            self.rank[root_a] += 1


# =============================================================================
# AOI and date utilities
# =============================================================================

def read_aoi() -> tuple[gpd.GeoDataFrame, object]:
    """Read AOI and return WGS84 AOI plus equal-area merged geometry."""
    aoi = gpd.read_file(AOI_FILE)

    print(aoi)

    if aoi.empty:
        raise ValueError("AOI_FILE contains no features.")

    if aoi.crs is None:
        raise ValueError(
            "AOI_FILE must declare a CRS. "
            "Use a GeoJSON or vector file in EPSG:4326."
        )

    aoi_wgs84 = aoi.to_crs("EPSG:4326")
    aoi_work_geometry = unary_union(
        aoi_wgs84.to_crs(WORK_CRS).geometry
    )

    return aoi_wgs84, aoi_work_geometry


def month_starts(start: str, end: str) -> Iterable[date]:
    """Generate first day of each month in the interval [start, end)."""
    current = datetime.fromisoformat(start).date().replace(day=1)
    stop = datetime.fromisoformat(end).date().replace(day=1)

    while current < stop:
        yield current
        current = (
            current.replace(day=28) + timedelta(days=4)
        ).replace(day=1)


def earthdata_headers() -> dict:
    """Create authenticated request headers from environment token."""
    token = os.environ.get("EARTHDATA_TOKEN")

    if not token:
        raise EnvironmentError(
            "EARTHDATA_TOKEN is not set. Generate an Earthdata token and "
            "set EARTHDATA_TOKEN before running this script."
        )

    return {
        "Authorization": f"Bearer {token}"
    }


def month_doy(month: date) -> int:
    """Return ordinal day of year for the first day of a monthly product."""
    return int(month.strftime("%j"))


# =============================================================================
# LAADS MCD64A1 discovery and download
# =============================================================================

def laads_month_directory(month: date) -> str:
    """Return the LAADS MCD64A1 directory for one monthly granule date."""
    return (
        f"{LAADS_BASE}/"
        f"{month.year}/"
        f"{month_doy(month):03d}/"
    )


def list_monthly_granules(
    month: date,
    session: requests.Session,
) -> dict[str, str]:
    """
    List MCD64A1 HDF granules in the LAADS directory for one month.

    Returns:
        Dictionary mapping MODIS tile ID such as h08v05 to full HDF URL.
    """
    directory_url = laads_month_directory(month)

    response = session.get(
        directory_url,
        timeout=HTTP_TIMEOUT_S,
    )
    response.raise_for_status()

    # Example filename:
    # MCD64A1.A2024153.h08v05.061.2024170030354.hdf
    expression = re.compile(
        rf"("
        rf"MCD64A1\.A{month.year}{month_doy(month):03d}\."
        rf"(h\d{{2}}v\d{{2}})"
        rf"\.061\.\d+\.hdf"
        rf")"
    )

    granules = {}

    for filename, tile in expression.findall(response.text):
        granules[tile] = directory_url + filename

    return granules


def download_mcd64a1_granules() -> list[Path]:
    """
    Download configured MODIS tiles for all months in the request period.
    """
    output_directory = OUTPUT_DIR / "mcd64a1_hdf"
    output_directory.mkdir(parents=True, exist_ok=True)

    session = requests.Session()
    session.headers.update(earthdata_headers())

    downloaded_files = []

    for month in month_starts(START_DATE, END_DATE):
        available_granules = list_monthly_granules(
            month,
            session,
        )

        for tile in MODIS_TILES:
            granule_url = available_granules.get(tile)

            if granule_url is None:
                print(
                    f"No MCD64A1 granule for "
                    f"{month:%Y-%m}, tile {tile}; skipping."
                )
                continue

            output_file = (
                output_directory
                / granule_url.rsplit("/", 1)[-1]
            )

            downloaded_files.append(output_file)

            if (
                output_file.exists()
                and output_file.stat().st_size > 0
            ):
                print(f"Already present: {output_file.name}")
                continue

            response = session.get(
                granule_url,
                stream=True,
                timeout=HTTP_TIMEOUT_S,
            )
            response.raise_for_status()

            with open(output_file, "wb") as destination:
                for block in response.iter_content(
                    chunk_size=1024 * 1024
                ):
                    if block:
                        destination.write(block)

            print(f"Downloaded {output_file.name}")

    if not downloaded_files:
        raise RuntimeError(
            "No MCD64A1 HDF files were found for the requested "
            "date range and configured MODIS tiles."
        )

    return [
        path for path in downloaded_files
        if path.exists() and path.stat().st_size > 0
    ]


# =============================================================================
# Burn Date extraction and daily patch creation
# =============================================================================

def open_burn_date_subdataset(hdf_path: Path):
    """
    Open the MCD64A1 Burn Date HDF subdataset.

    This searches for multiple possible GDAL/Rasterio naming variants.
    """
    container = rasterio.open(hdf_path)
    subdatasets = container.subdatasets
    container.close()

    matches = [
        name for name in subdatasets
        if name.lower().endswith(":burn date")
        or name.lower().endswith(":burndate")
        or "burn date" in name.lower()
    ]

    if not matches:
        raise RuntimeError(
            f"Could not find the Burn Date subdataset in "
            f"{hdf_path.name}.\n"
            f"Available subdatasets:\n{subdatasets}"
        )

    return rasterio.open(matches[0])


def safe_window_for_aoi(
    source,
    aoi_wgs84: gpd.GeoDataFrame,
) -> Window | None:
    """
    Transform AOI bounds into source CRS and determine the overlapping window.
    """
    west, south, east, north = aoi_wgs84.total_bounds

    left, bottom, right, top = transform_bounds(
        "EPSG:4326",
        source.crs,
        west,
        south,
        east,
        north,
        densify_pts=21,
    )

    raw_window = from_bounds(
        left,
        bottom,
        right,
        top,
        source.transform,
    )

    full_window = Window(
        col_off=0,
        row_off=0,
        width=source.width,
        height=source.height,
    )

    try:
        clipped_window = (
            raw_window
            .round_offsets()
            .round_lengths()
            .intersection(full_window)
        )
    except Exception:
        return None

    if clipped_window.width <= 0 or clipped_window.height <= 0:
        return None

    return clipped_window


def daily_patches_from_hdf(
    hdf_files: list[Path],
    aoi_wgs84: gpd.GeoDataFrame,
    aoi_work_geometry,
) -> gpd.GeoDataFrame:
    """
    Convert MCD64A1 Burn Date cells into 8-connected daily polygon patches.
    """
    records = []

    for hdf_path in hdf_files:
        # Parse date from filename, e.g. MCD64A1.A2024153.h08v05...
        date_match = re.search(
            r"\.A(\d{4})(\d{3})\.",
            hdf_path.name,
        )

        if not date_match:
            raise ValueError(
                f"Could not parse year/DOY from {hdf_path.name}"
            )

        year = int(date_match.group(1))

        with open_burn_date_subdataset(hdf_path) as source:
            window = safe_window_for_aoi(source, aoi_wgs84)

            if window is None:
                continue

            burn_date = source.read(1, window=window)
            local_transform = source.window_transform(window)
            nodata = source.nodata
            source_crs = source.crs

            valid = np.isfinite(burn_date) & (burn_date > 0)

            if nodata is not None:
                valid &= burn_date != nodata

            unique_doys = np.unique(
                burn_date[valid]
            ).astype(int)

            for doy in unique_doys:
                daily_mask = (burn_date == doy).astype("uint8")
                calendar_day = (
                    date(year, 1, 1)
                    + timedelta(days=int(int(doy) - 1))
                )

                for geometry_mapping, value in shapes(
                    daily_mask,
                    mask=daily_mask.astype(bool),
                    transform=local_transform,
                    connectivity=8,
                ):
                    if value != 1:
                        continue

                    records.append(
                        {
                            "burn_date": pd.Timestamp(calendar_day),
                            "geometry": shape(geometry_mapping),
                            "source_crs": source_crs,
                        }
                    )

    if not records:
        return gpd.GeoDataFrame(
            columns=[
                "patch_id",
                "burn_date",
                "area_km2",
                "geometry",
            ],
            crs=WORK_CRS,
        )

    # Separate temporary GeoDataFrames by source CRS, convert all to WORK_CRS,
    # then combine them. MODIS tiles normally share sinusoidal projection.
    records_by_crs = defaultdict(list)

    for record in records:
        source_crs = str(record.pop("source_crs"))
        records_by_crs[source_crs].append(record)

    patch_frames = []

    for crs_string, crs_records in records_by_crs.items():
        patch_frames.append(
            gpd.GeoDataFrame(
                crs_records,
                crs=crs_string,
            ).to_crs(WORK_CRS)
        )

    patches = pd.concat(patch_frames, ignore_index=True)
    patches = gpd.GeoDataFrame(
        patches,
        geometry="geometry",
        crs=WORK_CRS,
    )

    # Clip patch geometry from its tile/AOI bounding box to the actual AOI.
    patches["geometry"] = patches.geometry.intersection(
        aoi_work_geometry
    )

    patches = patches.loc[
        ~patches.geometry.is_empty
    ].copy()

    patches["patch_id"] = np.arange(
        len(patches),
        dtype=int,
    )

    patches["area_km2"] = (
        patches.geometry.area / 1_000_000.0
    )

    return patches[
        [
            "patch_id",
            "burn_date",
            "area_km2",
            "geometry",
        ]
    ]


# =============================================================================
# Fire-event clustering
# =============================================================================

def cluster_patches_globfire_style(
    patches: gpd.GeoDataFrame,
) -> gpd.GeoDataFrame:
    """
    Assign multi-day event IDs to daily burned patches.

    Two patches are assigned to the same event if:
      * Their burn dates differ by at most TIME_LINK_DAYS; and
      * Their geometries are within SPATIAL_LINK_M.
    """
    if patches.empty:
        return patches.assign(
            event_id=pd.Series(dtype="int64")
        )

    patches = (
        patches
        .sort_values(["burn_date", "patch_id"])
        .reset_index(drop=True)
    )

    union_find = UnionFind(len(patches))
    patch_indices_by_day = defaultdict(list)

    for index, timestamp in enumerate(patches["burn_date"]):
        patch_indices_by_day[timestamp.date()].append(index)

    for current_day in sorted(patch_indices_by_day):
        current_patch_indices = patch_indices_by_day[current_day]
        candidate_indices = []

        for offset_days in range(TIME_LINK_DAYS + 1):
            candidate_day = (
                current_day
                - timedelta(days=offset_days)
            )

            candidate_indices.extend(
                patch_indices_by_day.get(candidate_day, [])
            )

        candidate_patches = patches.iloc[candidate_indices]
        spatial_index = candidate_patches.sindex

        for patch_index in current_patch_indices:
            patch_geometry = patches.geometry.iloc[patch_index]

            search_bounds = (
                patch_geometry
                .buffer(SPATIAL_LINK_M)
                .bounds
            )

            candidate_local_indices = list(
                spatial_index.intersection(search_bounds)
            )

            for local_index in candidate_local_indices:
                other_patch_index = candidate_indices[local_index]

                # Avoid duplicate and self comparisons.
                if other_patch_index >= patch_index:
                    continue

                time_difference = (
                    patches.burn_date.iloc[patch_index]
                    - patches.burn_date.iloc[other_patch_index]
                ).days

                if time_difference > TIME_LINK_DAYS:
                    continue

                separation_m = patch_geometry.distance(
                    patches.geometry.iloc[other_patch_index]
                )

                if separation_m <= SPATIAL_LINK_M:
                    union_find.union(
                        patch_index,
                        other_patch_index,
                    )

    roots = [
        union_find.find(index)
        for index in range(len(patches))
    ]

    root_start_dates = {}

    for index, root in enumerate(roots):
        patch_date = patches.burn_date.iloc[index]

        if root not in root_start_dates:
            root_start_dates[root] = patch_date
        else:
            root_start_dates[root] = min(
                root_start_dates[root],
                patch_date,
            )

    ordered_roots = sorted(
        set(roots),
        key=lambda root: (
            root_start_dates[root],
            root,
        ),
    )

    root_to_event_id = {
        root: event_id
        for event_id, root in enumerate(
            ordered_roots,
            start=1,
        )
    }

    patches["event_id"] = [
        root_to_event_id[root]
        for root in roots
    ]

    return patches


def summarise_events(
    patches: gpd.GeoDataFrame,
) -> gpd.GeoDataFrame:
    """
    Dissolve daily patches into final event polygons and filter to >=5 km2.
    """
    records = []

    for event_id, group in patches.groupby(
        "event_id",
        sort=True,
    ):
        event_geometry = unary_union(list(group.geometry))

        start_date = group.burn_date.min()
        end_date = group.burn_date.max()

        records.append(
            {
                "event_id": int(event_id),
                "start_date": start_date,
                "end_date": end_date,
                "duration_days": int(
                    (end_date - start_date).days + 1
                ),
                "daily_patch_count": int(len(group)),
                "final_area_km2": (
                    event_geometry.area / 1_000_000.0
                ),
                "geometry": event_geometry,
            }
        )

    events = gpd.GeoDataFrame(
        records,
        crs=WORK_CRS,
    )

    return events.loc[
        events["final_area_km2"] >= MIN_EVENT_AREA_KM2
    ].copy()


# =============================================================================
# FIRMS download and pairing
# =============================================================================

def download_firms(
    aoi_wgs84: gpd.GeoDataFrame,
) -> pd.DataFrame:
    """
    Download FIRMS MODIS active-fire detections in five-day API chunks.
    """
    if FIRMS_MAP_KEY == "PASTE_YOUR_FIRMS_MAP_KEY_HERE":
        raise ValueError(
            "Set FIRMS_MAP_KEY before downloading FIRMS data."
        )

    west, south, east, north = aoi_wgs84.total_bounds

    bbox = (
        f"{west:.6f},"
        f"{south:.6f},"
        f"{east:.6f},"
        f"{north:.6f}"
    )

    current = datetime.fromisoformat(START_DATE).date()
    end = datetime.fromisoformat(END_DATE).date()

    tables = []

    while current < end:
        day_count = min(
            5,
            (end - current).days,
        )

        url = (
            "https://firms.modaps.eosdis.nasa.gov/"
            "api/area/csv/"
            f"{FIRMS_MAP_KEY}/"
            f"{FIRMS_SOURCE}/"
            f"{bbox}/"
            f"{day_count}/"
            f"{current:%Y-%m-%d}"
        )

        response = requests.get(
            url,
            timeout=HTTP_TIMEOUT_S,
        )
        response.raise_for_status()

        if (
            response.text.strip()
            and "latitude" in response.text.lower()
        ):
            tables.append(
                pd.read_csv(
                    pd.io.common.BytesIO(response.content)
                )
            )

        current += timedelta(days=day_count)

        # Avoid rapid repeated API requests.
        time.sleep(0.2)

    if not tables:
        return pd.DataFrame()

    firms = (
        pd.concat(tables, ignore_index=True)
        .drop_duplicates()
    )

    firms["acq_datetime"] = pd.to_datetime(
        firms["acq_date"].astype(str)
        + " "
        + firms["acq_time"].astype(str).str.zfill(4),
        format="%Y-%m-%d %H%M",
        errors="coerce",
        utc=True,
    )

    return firms


def associate_firms_to_events(
    events: gpd.GeoDataFrame,
    firms: pd.DataFrame,
) -> gpd.GeoDataFrame:
    """
    Pair FIRMS active-fire detections with final MCD64A1 fire events.

    A point matches an event if it:
      1. Falls within FIRMS_BUFFER_M of the event footprint; and
      2. Occurs from TIME_LINK_DAYS before event start through
         TIME_LINK_DAYS after event end.
    """
    if events.empty or firms.empty:
        return gpd.GeoDataFrame(
            columns=["event_id", "geometry"],
            crs="EPSG:4326",
        )

    required_fields = {
        "longitude",
        "latitude",
        "acq_datetime",
    }

    missing_fields = required_fields - set(firms.columns)

    if missing_fields:
        raise ValueError(
            f"FIRMS output is missing fields: "
            f"{sorted(missing_fields)}"
        )

    firms_points = gpd.GeoDataFrame(
        firms.copy(),
        geometry=gpd.points_from_xy(
            firms["longitude"],
            firms["latitude"],
        ),
        crs="EPSG:4326",
    ).to_crs(WORK_CRS)

    event_buffers = events[
        [
            "event_id",
            "start_date",
            "end_date",
            "geometry",
        ]
    ].copy()

    event_buffers["geometry"] = (
        event_buffers.geometry.buffer(FIRMS_BUFFER_M)
    )

    joined = gpd.sjoin(
        firms_points,
        event_buffers,
        how="inner",
        predicate="within",
    )

    if joined.empty:
        return joined.to_crs("EPSG:4326")

    detection_date = (
        joined["acq_datetime"]
        .dt.tz_convert(None)
        .dt.normalize()
    )

    allowed_start = (
        pd.to_datetime(joined["start_date"])
        - pd.Timedelta(days=TIME_LINK_DAYS)
    )

    allowed_end = (
        pd.to_datetime(joined["end_date"])
        + pd.Timedelta(days=TIME_LINK_DAYS + 1)
    )

    joined = joined.loc[
        (detection_date >= allowed_start)
        & (detection_date <= allowed_end)
    ].copy()

    return joined.to_crs("EPSG:4326")


# =============================================================================
# Main workflow
# =============================================================================

def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    aoi_wgs84, aoi_work_geometry = read_aoi()

    hdf_files = download_mcd64a1_granules()

    patches = daily_patches_from_hdf(
        hdf_files,
        aoi_wgs84,
        aoi_work_geometry,
    )

    if patches.empty:
        raise RuntimeError(
            "No MCD64A1 burned pixels were found in the "
            "AOI and requested period."
        )

    patches = cluster_patches_globfire_style(patches)

    events = summarise_events(patches)

    retained_patches = patches.merge(
        events[["event_id"]],
        on="event_id",
        how="inner",
    )

    retained_patches.to_file(
        OUTPUT_DIR / "event_daily_patches.gpkg",
        layer="daily_patches",
        driver="GPKG",
    )

    events.to_crs("EPSG:4326").to_file(
        OUTPUT_DIR / "fire_events.gpkg",
        layer="events",
        driver="GPKG",
    )

    firms = download_firms(aoi_wgs84)

    firms.to_csv(
        OUTPUT_DIR / "firms_active_fires.csv",
        index=False,
    )

    firms_matches = associate_firms_to_events(
        events,
        firms,
    )

    firms_matches.to_file(
        OUTPUT_DIR / "firms_event_matches.gpkg",
        layer="firms_matches",
        driver="GPKG",
    )

    print(
        f"Final events >= {MIN_EVENT_AREA_KM2} km2: "
        f"{len(events):,}"
    )

    print(
        f"FIRMS detection-event matches: "
        f"{len(firms_matches):,}"
    )


if __name__ == "__main__":
    main()
