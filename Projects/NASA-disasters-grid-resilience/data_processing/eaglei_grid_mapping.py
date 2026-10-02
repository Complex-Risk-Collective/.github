#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
eaglei_grid_mapping.py

Area-weighted county -> 50 km analysis-grid mapping for Eagle-I outage data.

This module builds on the county-to-grid mapping already produced by the
geoE/Eagle-I threshold-analysis notebook
(``notebooks/geoe_eaglei_threshold_analysis.ipynb``), which is cached at:

    EAGLE-I/processed/eaglei_county_to_analysis_grid_cells.csv

That cached table records, for each Eagle-I county, every 50 km analysis-grid
cell whose polygon *intersects* the county polygon (plus a handful of
nearest-cell fallback rows for counties that don't overlap any grid cell,
e.g. small islands). It does not record *how much* of the county falls in
each cell, so a naive join would double count: the notebook's own
`aggregate_eagle-i.py` precedent adds a county's full value to every
intersecting cell.

This module instead computes the actual polygon-intersection area between
each county and each grid cell it is mapped to, and normalizes those areas
so that each county's weights sum to 1. Multiplying a county's
``customers_out`` by its per-cell weight and summing over counties therefore
preserves the total number of customers out when redistributing county
totals onto the grid (mass-conserving areal disaggregation), instead of
replicating the full county value into every cell it touches.

Nearest-cell fallback rows (no polygon overlap, e.g. islands/exclaves) get a
weight of 1.0 -- the entire county value goes to that one cell, same as the
existing precedent, since there's no polygon to split.

Outputs are cached to:

    EAGLE-I/processed/eaglei_county_to_analysis_grid_cells_area_weighted.csv

Requires: pandas, numpy, geopandas, shapely, xarray (only for building /
rebuilding the cache; downstream pipeline code only needs the cached CSV and
pandas/numpy).

@author: (data_processing pipeline, built on prior notebook work)
"""

from __future__ import annotations

import zipfile
from pathlib import Path
from typing import Iterable, Optional
from urllib.request import urlretrieve

import numpy as np
import pandas as pd

# ------------------------
# Paths (matching conventions used elsewhere in this repo / the source notebook)
# ------------------------

REPO_ROOT = Path(__file__).resolve().parents[1]
GRID_PATH_DEFAULT = REPO_ROOT / "data" / "analysis_grid_CONUS_50km.nc"

EAGLEI_DIR_DEFAULT = Path(
    "/Users/ryanmc/Documents/Conferences/Jack_Eddy_Symposium_2022/dev/outage_data/EAGLE-I"
)
EAGLEI_PROCESSED_DIR_DEFAULT = EAGLEI_DIR_DEFAULT / "processed"
COUNTY_CACHE_DIR_DEFAULT = EAGLEI_PROCESSED_DIR_DEFAULT / "county_geometry_cache"

COUNTY_GRID_MAPPING_CSV_DEFAULT = (
    EAGLEI_PROCESSED_DIR_DEFAULT / "eaglei_county_to_analysis_grid_cells.csv"
)
COUNTY_GRID_MAPPING_AREA_WEIGHTED_CSV_DEFAULT = (
    EAGLEI_PROCESSED_DIR_DEFAULT
    / "eaglei_county_to_analysis_grid_cells_area_weighted.csv"
)

INTERSECTS_METHOD = "grid_cell_intersects_county_polygon"

# County shapefile vintages available in the cache, newest first. Eagle-I FIPS
# codes occasionally correspond to legacy county boundaries (e.g. Connecticut
# planning regions vs. pre-2022 counties), so we fall back through older
# vintages exactly like the source notebook does.
COUNTY_SHAPEFILE_VINTAGES = [2024, 2023, 2022, 2021]
COUNTY_ZIP_URL_TEMPLATE = (
    "https://www2.census.gov/geo/tiger/GENZ{year}/shp/cb_{year}_us_county_500k.zip"
)


def normalize_fips(values: pd.Series, width: int) -> pd.Series:
    return values.astype(str).str.extract(r"(\d+)", expand=False).str.zfill(width)


# ------------------------
# Geometry loading (geopandas required)
# ------------------------

def _county_shapefile_paths(cache_dir: Path, year: int) -> tuple[Path, Path]:
    zip_path = cache_dir / f"cb_{year}_us_county_500k.zip"
    shp_path = cache_dir / f"cb_{year}_us_county_500k.shp"
    return zip_path, shp_path


def load_county_geometries_for_fips(
    county_fips: Iterable[str],
    cache_dir: Path = COUNTY_CACHE_DIR_DEFAULT,
    vintages: Iterable[int] = COUNTY_SHAPEFILE_VINTAGES,
):
    """
    Return a GeoDataFrame with one row per requested county_fips, pulling
    geometry from the newest available Census county shapefile vintage and
    falling back to older vintages for FIPS codes not found in newer ones
    (legacy/retired county codes, e.g. Connecticut pre-2022 counties).
    """
    import geopandas as gpd

    cache_dir.mkdir(parents=True, exist_ok=True)
    remaining = set(str(f).zfill(5) for f in county_fips)
    pieces = []

    for year in vintages:
        if not remaining:
            break
        zip_path, shp_path = _county_shapefile_paths(cache_dir, year)
        if not shp_path.exists():
            if not zip_path.exists():
                url = COUNTY_ZIP_URL_TEMPLATE.format(year=year)
                print(f"Downloading county geometries ({year}): {url}")
                urlretrieve(url, zip_path)
            with zipfile.ZipFile(zip_path) as archive:
                archive.extractall(cache_dir)
        if not shp_path.exists():
            print(f"Warning: county shapefile for {year} unavailable, skipping.")
            continue

        gdf = gpd.read_file(shp_path)
        gdf["county_fips"] = gdf["GEOID"].astype(str).str.zfill(5)
        gdf["state_fips"] = gdf["STATEFP"].astype(str).str.zfill(2)
        gdf["county_name"] = gdf["NAME"].astype(str)
        gdf = gdf[gdf["county_fips"].isin(remaining)][
            ["county_fips", "state_fips", "county_name", "geometry"]
        ].to_crs("EPSG:4326")

        if not gdf.empty:
            pieces.append(gdf)
            remaining -= set(gdf["county_fips"])

    if remaining:
        print(
            f"Warning: {len(remaining)} county FIPS not found in any cached "
            f"shapefile vintage (no geometry for area weighting): "
            f"{sorted(remaining)[:10]}{'...' if len(remaining) > 10 else ''}"
        )

    if not pieces:
        return gpd.GeoDataFrame(
            columns=["county_fips", "state_fips", "county_name", "geometry"],
            geometry="geometry",
            crs="EPSG:4326",
        )

    counties = pd.concat(pieces, ignore_index=True).drop_duplicates("county_fips")
    return gpd.GeoDataFrame(counties, geometry="geometry", crs="EPSG:4326")


def build_grid_cell_polygons(grid_path: Path = GRID_PATH_DEFAULT):
    """
    Rebuild the 50 km analysis-grid cell polygons (as boxes in EPSG:5070,
    the grid's native projection) from the existing analysis_grid_CONUS_50km.nc.
    Returns a GeoDataFrame indexed by (grid_y, grid_x) with columns lat, lon,
    geometry, and cell_area_km2.
    """
    import geopandas as gpd
    import xarray as xr
    from shapely.geometry import box

    with xr.open_dataset(grid_path) as ds_grid:
        x_values = np.asarray(ds_grid["x"].values, dtype=float)
        y_values = np.asarray(ds_grid["y"].values, dtype=float)
        lat = np.asarray(ds_grid["lat"].values, dtype=float)
        lon = np.asarray(ds_grid["lon"].values, dtype=float)
        epsg = ds_grid.attrs.get("proj", "EPSG:5070")

    dx = float(np.nanmedian(np.abs(np.diff(x_values))))
    dy = float(np.nanmedian(np.abs(np.diff(y_values))))

    records = []
    geometries = []
    ny, nx = lat.shape
    for yi in range(ny):
        for xi in range(nx):
            x0 = x_values[xi]
            y0 = y_values[yi]
            records.append(
                {
                    "grid_y": yi,
                    "grid_x": xi,
                    "lat": float(lat[yi, xi]),
                    "lon": float(lon[yi, xi]),
                }
            )
            geometries.append(box(x0 - dx / 2, y0 - dy / 2, x0 + dx / 2, y0 + dy / 2))

    grid_cells = gpd.GeoDataFrame(records, geometry=geometries, crs=epsg)
    grid_cells["cell_area_km2"] = grid_cells.geometry.area / 1.0e6
    return grid_cells.set_index(["grid_y", "grid_x"], drop=False)


# ------------------------
# Area-weight computation
# ------------------------

def compute_area_weighted_mapping(
    intersects_mapping: pd.DataFrame,
    grid_path: Path = GRID_PATH_DEFAULT,
    county_cache_dir: Path = COUNTY_CACHE_DIR_DEFAULT,
) -> pd.DataFrame:
    """
    Given the existing intersects-only county<->grid-cell mapping (loaded from
    eaglei_county_to_analysis_grid_cells.csv), compute an intersection-area
    weight for every (county_fips, grid_y, grid_x) pair, normalized so each
    county's weights sum to 1.

    Rows whose mapping_method is the nearest-grid-center fallback (no polygon
    overlap) get weight = 1.0 individually.
    """
    mapping = intersects_mapping.copy()
    mapping["county_fips"] = normalize_fips(mapping["county_fips"], 5)
    mapping["state_fips"] = normalize_fips(mapping["state_fips"], 2)

    is_intersect = mapping["mapping_method"].eq(INTERSECTS_METHOD)
    fallback_rows = mapping.loc[~is_intersect].copy()
    intersect_rows = mapping.loc[is_intersect].copy()

    fallback_rows["intersection_area_km2"] = np.nan
    fallback_rows["county_area_km2"] = np.nan
    fallback_rows["weight"] = 1.0

    if intersect_rows.empty:
        result = fallback_rows
    else:
        import geopandas as gpd

        grid_cells = build_grid_cell_polygons(grid_path)
        counties = load_county_geometries_for_fips(
            intersect_rows["county_fips"].unique(), cache_dir=county_cache_dir
        )
        counties_5070 = counties.to_crs(grid_cells.crs)
        counties_5070["county_area_km2"] = counties_5070.geometry.area / 1.0e6
        county_geom = dict(
            zip(counties_5070["county_fips"], counties_5070.geometry)
        )
        county_area = dict(
            zip(counties_5070["county_fips"], counties_5070["county_area_km2"])
        )
        cell_geom = dict(
            zip(
                zip(grid_cells["grid_y"], grid_cells["grid_x"]),
                grid_cells.geometry,
            )
        )

        areas = []
        county_areas_out = []
        for row in intersect_rows.itertuples(index=False):
            geom_cell = cell_geom.get((row.grid_y, row.grid_x))
            geom_county = county_geom.get(row.county_fips)
            if geom_cell is None or geom_county is None:
                areas.append(0.0)
                county_areas_out.append(np.nan)
                continue
            inter_area_km2 = geom_cell.intersection(geom_county).area / 1.0e6
            areas.append(inter_area_km2)
            county_areas_out.append(county_area.get(row.county_fips, np.nan))

        intersect_rows["intersection_area_km2"] = areas
        intersect_rows["county_area_km2"] = county_areas_out

        county_totals = intersect_rows.groupby("county_fips")[
            "intersection_area_km2"
        ].transform("sum")
        # Guard against zero-area edge cases (shouldn't happen for a genuine
        # intersects() pair, but be defensive).
        safe_totals = county_totals.replace(0.0, np.nan)
        intersect_rows["weight"] = (
            intersect_rows["intersection_area_km2"] / safe_totals
        )
        # If a county's intersections summed to zero area (degenerate,
        # e.g. touches only at a boundary), split evenly as a last resort.
        degenerate = intersect_rows["weight"].isna()
        if degenerate.any():
            counts = intersect_rows.loc[degenerate].groupby("county_fips")[
                "county_fips"
            ].transform("size")
            intersect_rows.loc[degenerate, "weight"] = 1.0 / counts

        result = pd.concat([intersect_rows, fallback_rows], ignore_index=True)

    result = result.sort_values(["county_fips", "grid_y", "grid_x"]).reset_index(
        drop=True
    )
    return result


def load_or_build_area_weighted_mapping(
    intersects_mapping_csv: Path = COUNTY_GRID_MAPPING_CSV_DEFAULT,
    area_weighted_csv: Path = COUNTY_GRID_MAPPING_AREA_WEIGHTED_CSV_DEFAULT,
    grid_path: Path = GRID_PATH_DEFAULT,
    county_cache_dir: Path = COUNTY_CACHE_DIR_DEFAULT,
    force_rebuild: bool = False,
) -> pd.DataFrame:
    """
    Load the cached area-weighted mapping if present, otherwise build it from
    the existing intersects-only mapping CSV and cache the result.
    """
    if area_weighted_csv.exists() and not force_rebuild:
        print(f"Loaded cached area-weighted mapping: {area_weighted_csv}")
        return pd.read_csv(
            area_weighted_csv, dtype={"county_fips": str, "state_fips": str}
        )

    if not intersects_mapping_csv.exists():
        raise FileNotFoundError(
            f"Expected intersects-only mapping CSV not found: {intersects_mapping_csv}. "
            "This should already exist from the geoE/Eagle-I threshold-analysis "
            "notebook; if it's missing, that notebook's county-to-grid mapping "
            "cells (around 'Eagle-I county-to-analysis-grid mapping') need to be "
            "re-run first."
        )

    print(f"Building area-weighted mapping from: {intersects_mapping_csv}")
    intersects_mapping = pd.read_csv(
        intersects_mapping_csv, dtype={"county_fips": str, "state_fips": str}
    )
    mapping = compute_area_weighted_mapping(
        intersects_mapping, grid_path=grid_path, county_cache_dir=county_cache_dir
    )

    # QA: each county's weights should sum to ~1.
    weight_sums = mapping.groupby("county_fips")["weight"].sum()
    off = weight_sums[(weight_sums - 1.0).abs() > 1e-6]
    if len(off):
        print(
            f"Note: {len(off)} counties have weight sums that deviate from 1.0 "
            f"by more than 1e-6 (min={off.min():.6f}, max={off.max():.6f})."
        )

    area_weighted_csv.parent.mkdir(parents=True, exist_ok=True)
    mapping.to_csv(area_weighted_csv, index=False)
    print(f"Wrote area-weighted mapping: {area_weighted_csv} ({len(mapping):,} rows)")
    return mapping


if __name__ == "__main__":
    load_or_build_area_weighted_mapping()
