"""Small offline tests; no county downloads or production source scans."""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import xarray as xr

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "data_processing"))
import eaglei_grid_pipeline as pipeline
import eaglei_grid_mapping as mapping


@pytest.fixture
def job(tmp_path, monkeypatch):
    grid = tmp_path / "custom_grid.nc"
    xr.Dataset({"lat": (("y", "x"), [[30., 31., 32.]]),
                "lon": (("y", "x"), [[-95., -94., -93.]])},
               coords={"y": [100.], "x": [200., 300., 400.]}).to_netcdf(grid)
    weights = pd.DataFrame({"county_fips": ["01001", "01001", "01003"],
                            "state_fips": ["01"] * 3,
                            "grid_y": [0, 0, 0], "grid_x": [0, 1, 1],
                            "weight": [.25, .75, 1.],
                            "intersection_area_km2": [25., 75., 100.],
                            "county_area_km2": [100.] * 3})

    def build(counties, grid_path, output_dir):
        assert Path(grid_path) == grid
        return weights[weights.county_fips.isin(counties)].copy()

    monkeypatch.setattr(mapping, "load_or_build_grid_mapping", build, raising=False)
    monkeypatch.setattr(mapping, "grid_fingerprint",
                        lambda p: hashlib.sha256(Path(p).read_bytes()).hexdigest(), raising=False)
    out = tmp_path / "new_outputs"
    panel = tmp_path / "processed/eaglei_county_15min_panel_2024_2025.parquet"
    panel.parent.mkdir()
    return tmp_path, grid, out, panel, weights


def write_panel(path, rows):
    pd.DataFrame(rows, columns=["time", "county_fips", "customers_out",
                               "is_suppressed_override"]).to_parquet(path, index=False)


def execute(job, **kwargs):
    root, grid, out, _, _ = job
    return pipeline.process_year(2024, eaglei_dir=root, grid_path=grid,
                                 output_dir=out, chunksize=2, **kwargs)


def test_annual_streaming_nan_semantics_conservation_and_skip(job, monkeypatch):
    root, grid, out, panel, _ = job
    write_panel(panel, [
        ("2024-01-01T00:00:00Z", "01001", 100., False),
        ("2025-01-01T00:00:00Z", "01001", 999., False),
        ("2024-01-01T00:00:00Z", "01003", 50., False),
        ("2024-01-01T00:00:00Z", "02013", 7., False),
        ("2024-01-01T00:15:00Z", "01001", 999., True),
        ("2024-01-01T00:15:00Z", "01003", 20., False),
        ("2024-01-01T00:30:00Z", "01003", np.inf, False),
        ("2024-01-01T00:45:00Z", "01001", np.nan, False),
        ("2024-01-01T01:00:00Z", "01003", 0., False),
    ])
    # Production must never call the legacy eager readers or aggregators.
    def forbidden(*args, **kwargs):
        raise AssertionError("Legacy eager path called")
    monkeypatch.setattr(pipeline, "load_panel_for_year", forbidden)
    monkeypatch.setattr(pipeline, "aggregate_panel_to_grid", forbidden)
    qa = execute(job)
    assert qa["n_panel_rows"] == 8
    assert qa["valid_mapped_rows"] == 4
    assert qa["invalid_reported_rows"] == 3
    assert qa["suppressed_reported_rows"] == 1
    assert qa["nonfinite_reported_rows"] == 2
    assert qa["unmapped_rows"] == 1
    assert qa["unmapped_valid_total"] == 7
    assert qa["raw_total_customers_out_mapped_counties"] == 170
    assert qa["gridded_total_customers_out"] == 170
    assert qa["output_visible_total"] == 150
    assert qa["valid_contribution_hidden_by_nan"] == 20
    assert qa["timestamps_missing"] == 366 * 96 - 5
    nc = out / pipeline.OUTPUT_FILENAME_TEMPLATE.format(year=2024)
    with xr.open_dataset(nc) as ds:
        assert ds.sizes["time"] == 366 * 96
        np.testing.assert_allclose(ds.customers_out[0, 0], [25, 125, np.nan], equal_nan=True)
        assert np.isnan(ds.customers_out[1]).all()
        np.testing.assert_allclose(ds.customers_out[2, 0], [0, np.nan, np.nan], equal_nan=True)
        assert np.isnan(ds.customers_out[3]).all()
        np.testing.assert_allclose(ds.customers_out[4, 0], [0, 0, np.nan], equal_nan=True)
        np.testing.assert_array_equal(ds.mapped_county_count, [[1, 2, 0]])
        np.testing.assert_array_equal(ds.source_row_count[:6], [3, 2, 1, 1, 1, 0])
        assert np.isnan(ds.customers_out.isel(time=5)).all()
        np.testing.assert_allclose(ds.gridded_valid_total, ds.mapped_valid_total)
        np.testing.assert_allclose(ds.gridded_valid_total,
                                   ds.output_visible_total + ds.valid_contribution_hidden_by_nan)
        assert ds.attrs["grid_source"] == str(grid)
        assert ds.attrs["grid_sha256"] == hashlib.sha256(grid.read_bytes()).hexdigest()
        assert ds.customers_out.encoding["zlib"]
    assert not list(out.glob("*.partial*"))
    monkeypatch.setattr(pipeline, "discover_source", forbidden)
    assert execute(job) == qa  # matching identity skips without source scan
    # A size/mtime change must prevent reusing a stale output.
    with panel.open("ab") as stream:
        stream.write(b"changed")
    with pytest.raises(FileExistsError, match="identity mismatch"):
        execute(job)


@pytest.mark.parametrize("rows", [
    [("2024-01-01", "01001", 1., False)] * 2,
    [("2024-01-01", "01001", 1., False),
     ("2024-01-01 00:15", "01003", 2., False),
     ("2024-01-01", "01001", 1., False)],
    [("2024-01-01", "02013", 1., False)] * 2,
])
def test_global_duplicate_guard_and_stale_partial(job, rows):
    write_panel(job[3], rows)
    with pytest.raises(ValueError, match="Duplicate county/time"):
        execute(job)
    assert not list(job[2].glob("*.nc"))
    assert not list(job[2].glob("*.manifest.json"))
    with pytest.raises(FileExistsError, match="Stale partial"):
        execute(job, overwrite=True)


@pytest.mark.parametrize("time,county,value,flag,message", [
    ("2024-01-01 00:01", "01001", 1, False, "15-minute"),
    ("2024-01-01", "01001", -1, False, "negative"),
    ("2024-01-01", "01001", -1, True, "negative"),
    ("2024-01-01", "bad", 1, False, "FIPS"),
    (None, "01001", 1, False, "timestamp"),
    ("2024-01-01", "01001", 1, None, "suppression flag"),
])
def test_invalid_input_rejected_without_final_outputs(job, time, county, value, flag, message):
    write_panel(job[3], [(time, county, value, flag)])
    with pytest.raises(ValueError, match=message):
        execute(job)
    assert not list(job[2].glob("*.nc"))


def test_raw_fallback_utc_and_csv_suppressions(job):
    root, _, _, _, _ = job
    raw = root / "eaglei_outages_2024.csv"
    pd.DataFrame({"Fips Code": [1001, 1003], "Customers Out": [5, "suppressed"],
                  "Run Start Time": ["2023-12-31T19:00:00-05:00", "2024-01-01T00:15:00Z"],
                  "Contains Override Data": [False, True]}).to_csv(raw, index=False)
    assert pipeline.select_source(2024, root) == (raw, "raw")
    chunks = list(pipeline.iter_source_chunks(raw, 2024, 1))
    assert len(chunks) == 2
    assert chunks[0].iloc[0].county_fips == "01001"
    assert chunks[0].iloc[0].time == pd.Timestamp("2024-01-01")
    assert chunks[1].iloc[0].invalid
    qa = execute(job, source="raw")
    assert qa["raw_total_customers_out_mapped_counties"] == 5
    assert qa["identity"]["source_kind"] == "raw"


def test_panel_csv_fallback_batches(job):
    root, _, _, parquet, _ = job
    csv = parquet.with_suffix(".csv")
    pd.DataFrame({"time": ["2024-01-01", "2025-01-01", "2024-01-01 00:15"],
                  "county_fips": [1001, 1001, 1003],
                  "customers_out": [10, 99, 20]}).to_csv(csv, index=False)
    assert pipeline.select_source(2024, root) == (csv, "panel")
    chunks = list(pipeline.iter_source_chunks(csv, 2024, 1))
    assert len(chunks) == 2
    assert all(len(c) <= 1 for c in chunks)
    assert execute(job)["gridded_total_customers_out"] == 30


def test_fully_unmapped_source_stays_nan_and_is_accounted(job):
    write_panel(job[3], [("2024-01-01", "02013", 7., False)])
    qa = execute(job)
    assert qa["unmapped_rows"] == 1
    assert qa["unmapped_valid_total"] == 7
    assert qa["valid_mapped_rows"] == 0
    assert qa["gridded_total_customers_out"] == 0
    assert qa["conservation_ratio"] is None
    with xr.open_dataset(job[2] / pipeline.OUTPUT_FILENAME_TEMPLATE.format(year=2024)) as ds:
        assert np.isnan(ds.customers_out).all()
        assert ds.source_row_count[0] == 1
        assert ds.mapped_row_count[0] == 0


@pytest.mark.parametrize("mutation", ["weight", "bounds", "duplicate", "nonconus"])
def test_bad_mapping_rejected(job, mutation):
    weights = job[4].copy()
    if mutation == "weight":
        weights.loc[0, "weight"] = .5
    elif mutation == "bounds":
        weights.loc[0, "grid_x"] = 3
    elif mutation == "duplicate":
        weights = pd.concat([weights, weights.iloc[:1]])
    else:
        weights.loc[2, "county_fips"] = "02013"
    with pytest.raises(ValueError):
        pipeline._validated_weights(weights, {"01001", "01003", "02013"}, 1, 3)


def test_conservation_failure_does_not_promote(job, monkeypatch):
    write_panel(job[3], [("2024-01-01", "01001", 10., False)])
    real = pipeline._scatter_source

    def corrupt(*args, **kwargs):
        series, valid_rows = real(*args, **kwargs)
        series["mapped_valid_total"][0] += 1
        return series, valid_rows
    monkeypatch.setattr(pipeline, "_scatter_source", corrupt)
    with pytest.raises(ValueError, match="conservation failed"):
        execute(job)
    assert not list(job[2].glob("*.nc"))
    assert not list(job[2].glob("*.manifest.json"))
    assert list(job[2].glob("*.nc.partial"))


def test_actual_grid_identity_mismatch_requires_overwrite(job):
    write_panel(job[3], [("2024-01-01", "01001", 10., False)])
    execute(job)
    with job[1].open("ab") as stream:
        stream.write(b"different-grid")
    with pytest.raises(FileExistsError, match="identity mismatch"):
        execute(job)


def test_source_changed_during_pass_rejected(job, monkeypatch):
    write_panel(job[3], [("2024-01-01", "01001", 10., False)])
    original = pipeline.discover_source

    def changing(*args):
        result = original(*args)
        with job[3].open("ab") as stream:
            stream.write(b"change")
        return result
    monkeypatch.setattr(pipeline, "discover_source", changing)
    with pytest.raises(RuntimeError, match="changed during discovery"):
        execute(job)
    assert not list(job[2].glob("*.nc"))


def test_axes_and_cli_self_test(capsys):
    assert len(pipeline.build_time_index(2024)) == 366 * 96
    assert len(pipeline.build_time_index(2025)) == 365 * 96
    pipeline.main(["--self-test"])
    assert "Self-test passed" in capsys.readouterr().out


def test_legacy_output_directory_is_refused(job):
    with pytest.raises(ValueError, match="legacy raw-directory"):
        pipeline.process_year(2024, eaglei_dir=job[0], output_dir=job[0], grid_path=job[1])