"""Create daily-maximum V1/V2 terrestrial-weather comparison maps.

The figures compare the legacy daily files in the terrestrial-weather root with
the v2.1 files in its V2 subdirectory for selected June 2024 dates. Values are
masked for legacy MRMS negative no-coverage sentinels before plotting.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path

import geopandas as gpd
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import xarray as xr
from matplotlib.backends.backend_pdf import PdfPages
from matplotlib.colors import Normalize


DATES = ("20240626", "20240627", "20240630")
WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_V1_ROOT = Path(
    "/Users/ryanmc/Documents/NASA_JPL/Projects/NaturalHazards/"
    "NASA ROSES Disasters 2025-2027/data/terrestrial_weather"
)
DEFAULT_V2_ROOT = DEFAULT_V1_ROOT / "V2"
DEFAULT_OUTPUT_DIR = WORKSPACE_ROOT / "outputs" / "terrestrial_weather_v1_v2_june2024"
STATE_FILE = WORKSPACE_ROOT / "data" / "cb_2023_us_state_20m.geojson"


@dataclass(frozen=True)
class FieldSpec:
    key: str
    title: str
    v1_variable: str
    v2_variable: str
    units: str
    cmap: str
    mrms_sentinel: bool = False
    kelvin_to_celsius: bool = False
    note: str = ""


FIELDS = (
    FieldSpec(
        "precip_1h",
        "Maximum hourly precipitation",
        "MultiSensor_QPE_01H_Pass2_00.00",
        "MultiSensor_QPE_01H_Pass2_00.00",
        "mm per 1 h",
        "YlGnBu",
        note="Daily maximum; V1 50-km mean vs V2 cell maximum",
    ),
    FieldSpec(
        "wind_gust",
        "10 m wind gust",
        "i10fg",
        "i10fg",
        "m s$^{-1}$",
        "YlOrRd",
        note="Daily maximum; V1 50-km mean vs V2 cell maximum",
    ),
    FieldSpec(
        "temperature",
        "2 m air temperature",
        "t2m",
        "t2m",
        "$^\circ$C",
        "inferno",
        kelvin_to_celsius=True,
        note="Daily maximum of hourly t2m analysis; V2 also has t2m_max",
    ),
    FieldSpec(
        "hail_mesh",
        "MESH estimated hail size",
        "MESH_00.50",
        "MESH_Max_60min_00.50",
        "mm",
        "viridis",
        mrms_sentinel=True,
        note="Product and spatial aggregation differ; V1 negatives are no-coverage; proxy, not damage",
    ),
    FieldSpec(
        "lightning_density",
        "Cloud-to-ground lightning density",
        "NLDN_CG_001min_AvgDensity_00.00",
        "NLDN_CG_015min_AvgDensity_00.00",
        "flashes km$^{-2}$ min$^{-1}$",
        "plasma",
        mrms_sentinel=True,
        note="V1 is 1-min and V2 is 15-min density; spatial aggregation also differs",
    ),
    FieldSpec(
        "echo_top_18",
        "18 dBZ echo-top height",
        "EchoTop_18_00.50",
        "EchoTop_18_00.50",
        "km",
        "Blues",
        mrms_sentinel=True,
        note="Daily maximum; V1 50-km mean vs V2 cell maximum; convective context",
    ),
)


def daily_maximum(
    path: Path, variable: str, spec: FieldSpec
) -> tuple[np.ndarray, np.ndarray, np.ndarray, float]:
    """Return a masked daily-maximum map, projected grid centers, and coverage."""
    with xr.open_dataset(path) as dataset:
        if variable not in dataset:
            raise KeyError(f"{variable!r} is missing from {path}")
        values = np.asarray(dataset[variable].values, dtype=np.float64)
        if values.ndim != 3 or values.shape[0] != dataset.sizes["time"]:
            raise ValueError(f"Expected time,y,x data for {variable!r} in {path}")
        if spec.mrms_sentinel:
            values[values < 0] = np.nan
        valid = np.isfinite(values)
        daily_map = np.max(np.where(valid, values, -np.inf), axis=0)
        daily_map[~valid.any(axis=0)] = np.nan
        if spec.kelvin_to_celsius:
            daily_map -= 273.15
        x = np.asarray(dataset["x"].values, dtype=np.float64) / 1000.0
        y = np.asarray(dataset["y"].values, dtype=np.float64) / 1000.0
    coverage = float(np.isfinite(daily_map).mean())
    return daily_map, x, y, coverage


def load_state_boundaries(
    ax: plt.Axes,
    x_limits: tuple[float, float],
    y_limits: tuple[float, float],
) -> None:
    if not STATE_FILE.exists():
        return
    states = gpd.read_file(STATE_FILE)
    if states.crs is None:
        states = states.set_crs("EPSG:4326")
    states = states.to_crs("EPSG:5070")
    states.boundary.plot(ax=ax, color="#555555", linewidth=0.35, alpha=0.7, zorder=3)
    ax.set_xlim(*x_limits)
    ax.set_ylim(*y_limits)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--v1-root", type=Path, default=DEFAULT_V1_ROOT)
    parser.add_argument("--v2-root", type=Path, default=DEFAULT_V2_ROOT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)

    maps: dict[tuple[str, str, str], tuple[np.ndarray, np.ndarray, np.ndarray, float]] = {}
    scales: dict[str, tuple[float, float]] = {}
    summary_rows: list[dict[str, object]] = []

    # Use consistent color limits for each field across both versions and all
    # dates, so colors can be compared across all three figures.
    for spec in FIELDS:
        pooled: list[np.ndarray] = []
        for date in DATES:
            for version, root, variable in (
                ("V1", args.v1_root, spec.v1_variable),
                ("V2", args.v2_root, spec.v2_variable),
            ):
                path = root / f"weather_CONUS_{date}.nc"
                if not path.is_file():
                    raise FileNotFoundError(path)
                data, x, y, coverage = daily_maximum(path, variable, spec)
                maps[(date, version, spec.key)] = (data, x, y, coverage)
                finite = data[np.isfinite(data)]
                if finite.size:
                    pooled.append(finite)
                summary_rows.append(
                    {
                        "date": date,
                        "version": version,
                        "field": spec.key,
                        "variable": variable,
                        "coverage_fraction": coverage,
                        "daily_map_max": float(np.nanmax(data)) if finite.size else np.nan,
                        "daily_map_p99": float(np.nanpercentile(finite, 99)) if finite.size else np.nan,
                        "units": spec.units,
                        "note": spec.note,
                    }
                )
        if pooled:
            pooled_values = np.concatenate(pooled)
            vmin = float(np.nanpercentile(pooled_values, 1))
            vmax = float(np.nanpercentile(pooled_values, 99.5))
            if spec.key in {"precip_1h", "hail_mesh", "lightning_density", "echo_top_18", "wind_gust"}:
                vmin = 0.0
            if not np.isfinite(vmax) or vmax <= vmin:
                vmax = vmin + 1.0
            scales[spec.key] = (vmin, vmax)

    first = maps[(DATES[0], "V1", FIELDS[0].key)]
    y_size, x_size = first[0].shape
    x_limits = (float(first[1][0]), float(first[1][-1]))
    y_limits = (float(first[2][0]), float(first[2][-1]))
    xx, yy = np.meshgrid(first[1], first[2])

    pdf_path = args.output_dir / "v1_v2_june2024_comparison.pdf"
    with PdfPages(pdf_path) as pdf:
        for date in DATES:
            fig, axes = plt.subplots(
                nrows=len(FIELDS),
                ncols=2,
                figsize=(13.5, 20),
                constrained_layout=True,
                squeeze=False,
            )
            fig.suptitle(
                f"Terrestrial weather V1 vs V2 — {pd.to_datetime(date).strftime('%B %-d, %Y')}\n"
                "Daily maximum maps on the common 50 km EPSG:5070 grid",
                fontsize=16,
                fontweight="bold",
            )

            for row, spec in enumerate(FIELDS):
                vmin, vmax = scales[spec.key]
                for col, version in enumerate(("V1", "V2")):
                    ax = axes[row, col]
                    data, _, _, coverage = maps[(date, version, spec.key)]
                    mesh = ax.pcolormesh(
                        xx,
                        yy,
                        data,
                        shading="auto",
                        cmap=spec.cmap,
                        norm=Normalize(vmin=vmin, vmax=vmax, clip=False),
                        rasterized=True,
                        zorder=1,
                    )
                    load_state_boundaries(ax, x_limits, y_limits)
                    finite = data[np.isfinite(data)]
                    peak = float(np.nanmax(data)) if finite.size else np.nan
                    title = f"{version} — valid {coverage:.1%}; daily map max {peak:.3g}"
                    if col == 1:
                        title += f"\n{spec.note}"
                    ax.set_title(title, fontsize=9)
                    ax.set_ylabel("Northing (km)")
                    ax.set_xlabel("Easting (km)")
                    ax.set_aspect("equal", adjustable="box")
                    ax.grid(color="white", linewidth=0.25, alpha=0.35)
                    fig.colorbar(
                        mesh,
                        ax=ax,
                        shrink=0.86,
                        pad=0.012,
                        extend="max",
                        label=spec.units,
                    )
                axes[row, 0].text(
                    -0.18,
                    0.5,
                    spec.title,
                    transform=axes[row, 0].transAxes,
                    rotation=90,
                    va="center",
                    ha="center",
                    fontsize=10,
                    fontweight="bold",
                )

            fig.text(
                0.5,
                0.002,
                "Shared color scale per field across dates and versions (upper tail capped at pooled p99.5). "
                "For most gridded fields V1 is a 50-km mean and V2 is a product-specific maximum. "
                "Legacy negative MRMS sentinels are masked; V2 NaNs remain missing. "
                "Lightning windows differ (V1 1-min, V2 15-min).",
                ha="center",
                fontsize=8,
            )
            png_path = args.output_dir / f"v1_v2_{date}_side_by_side.png"
            fig.savefig(png_path, dpi=180, bbox_inches="tight")
            pdf.savefig(fig, dpi=180)
            plt.close(fig)
            print(f"Wrote {png_path}")

    summary_path = args.output_dir / "comparison_summary.csv"
    pd.DataFrame(summary_rows).to_csv(summary_path, index=False)
    print(f"Wrote {pdf_path}")
    print(f"Wrote {summary_path}")
    print(f"Grid shape: {y_size} x {x_size}; EPSG:5070 coordinates in km")


if __name__ == "__main__":
    main()