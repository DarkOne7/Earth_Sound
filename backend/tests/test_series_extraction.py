from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pytest
import xarray as xr
from app.config import Settings
from app.schemas.current_series import SampleStatus
from app.services.current_processing import process_current_series
from app.services.dataset_loader import DatasetLoadError
from app.services.series_extraction import (
    SeriesExtractionError,
    extract_default_series,
    extract_point_series,
    extract_region_series,
    resolve_coordinate,
)

TIMES = np.array([f"2020-01-{day:02d}" for day in range(1, 13)], dtype="datetime64[ns]")
LATS = np.array([0.125, 0.375, 0.625])
LONS_360 = np.array([208.0, 208.25, 208.5])  # = -152.0, -151.75, -151.5


def write_sample(path, *, times=TIMES, u=None, v=None, lons=LONS_360, coord_attrs=True, units="m s-1"):
    shape = (len(times), len(lons), len(LATS))  # OSCAR's published order: time, longitude, latitude
    rng = np.random.default_rng(42)
    if u is None:
        u = rng.uniform(-0.5, 0.5, shape)
    if v is None:
        v = rng.uniform(-0.5, 0.5, shape)
    names = ("t", "x", "y") if not coord_attrs else ("time", "longitude", "latitude")
    dataset = xr.Dataset(
        {
            "u": (names, np.asarray(u, dtype=np.float64), {"units": units}),
            "v": (names, np.asarray(v, dtype=np.float64), {"units": units}),
        },
        coords={names[0]: times, names[1]: lons, names[2]: LATS},
    )
    if coord_attrs:
        dataset["latitude"].attrs.update(standard_name="latitude", units="degrees_north")
        dataset["longitude"].attrs.update(standard_name="longitude", units="degrees_east")
        dataset["time"].attrs.update(standard_name="time")
    dataset.to_netcdf(path, encoding={"u": {"_FillValue": -999.0}, "v": {"_FillValue": -999.0}})
    dataset.close()
    return path


@pytest.fixture
def sample(tmp_path):
    return write_sample(tmp_path / "sample.nc")


def read_raw(path, name):
    with xr.open_dataset(path) as dataset:
        return dataset[name].values.copy()


def test_point_extraction_selects_nearest_grid_point_and_window(sample):
    series = extract_point_series(
        sample, latitude=0.4, longitude=-151.8, start="2020-01-02", end="2020-01-05",
        dataset_name="OSCAR_L4_OC_FINAL_V2.0", dataset_version="2.0",
    )
    # -151.8 -> 208.2 in the 0..360 file convention; nearest 208.25 (index 1); lat 0.375 (index 1)
    assert series.selection.selected_longitude == 208.25
    assert series.selection.selected_latitude == 0.375
    assert [t.day for t in series.timestamps] == [2, 3, 4, 5]
    assert all(t.tzinfo is UTC for t in series.timestamps)
    raw_u = read_raw(sample, "u")
    np.testing.assert_array_equal(series.u, raw_u[1:5, 1, 1])
    assert series.u_units == "m s-1"
    assert series.source["dataset"] == "OSCAR_L4_OC_FINAL_V2.0"
    assert len(series.source["source_sha256"]) == 64


def test_point_extraction_accepts_both_longitude_conventions(sample):
    west = extract_point_series(sample, latitude=0.1, longitude=-151.5, start="2020-01-01", end="2020-01-12")
    east = extract_point_series(sample, latitude=0.1, longitude=208.5, start="2020-01-01", end="2020-01-12")
    np.testing.assert_array_equal(west.u, east.u)


def test_extraction_is_reproducible(sample):
    kwargs = {"latitude": 0.3, "longitude": -151.7, "start": "2020-01-01", "end": "2020-01-12"}
    first = process_current_series(extract_point_series(sample, **kwargs))
    second = process_current_series(extract_point_series(sample, **kwargs))
    assert first.model_dump_json() == second.model_dump_json()
    assert first.metadata.selection.latitude == 0.3
    assert first.metadata.selection.start == datetime(2020, 1, 1, tzinfo=UTC)


def test_point_outside_grid_rejected(sample):
    with pytest.raises(SeriesExtractionError, match="outside the dataset grid"):
        extract_point_series(sample, latitude=45.0, longitude=-151.0, start="2020-01-01", end="2020-01-12")


def test_empty_time_window_rejected(sample):
    with pytest.raises(SeriesExtractionError, match="No timestamps"):
        extract_point_series(sample, latitude=0.3, longitude=-151.7, start="2021-01-01", end="2021-01-12")
    with pytest.raises(SeriesExtractionError, match="after end"):
        extract_point_series(sample, latitude=0.3, longitude=-151.7, start="2020-01-05", end="2020-01-01")


def test_duplicate_dataset_timestamps_rejected(tmp_path):
    times = TIMES.copy()
    times[3] = times[2]
    path = write_sample(tmp_path / "dup.nc", times=times)
    with pytest.raises(SeriesExtractionError, match="Duplicate"):
        extract_point_series(path, latitude=0.3, longitude=-151.7, start="2020-01-01", end="2020-01-12")


def test_out_of_order_dataset_timestamps_rejected(tmp_path):
    times = TIMES.copy()
    times[[3, 4]] = times[[4, 3]]
    path = write_sample(tmp_path / "unordered.nc", times=times)
    with pytest.raises(SeriesExtractionError, match="out of order"):
        extract_point_series(path, latitude=0.3, longitude=-151.7, start="2020-01-01", end="2020-01-12")


def test_coordinates_resolved_from_attributes_not_names(tmp_path):
    path = write_sample(tmp_path / "plain.nc", coord_attrs=False)
    with xr.open_dataset(path) as dataset:
        dataset["y"].attrs["units"] = "degrees_north"
        dataset["x"].attrs["units"] = "degrees_east"
        dataset["t"].attrs["axis"] = "T"
        assert resolve_coordinate(dataset, "latitude") == "y"
        assert resolve_coordinate(dataset, "longitude") == "x"
        assert resolve_coordinate(dataset, "time") == "t"


def test_unidentifiable_coordinates_rejected(tmp_path):
    path = write_sample(tmp_path / "plain.nc", coord_attrs=False)
    with pytest.raises(SeriesExtractionError, match="No latitude coordinate"):
        extract_point_series(path, latitude=0.3, longitude=-151.7, start="2020-01-01", end="2020-01-12")


def test_fill_values_flow_through_as_missing(tmp_path):
    u = np.full((12, 3, 3), 0.2)
    u[0, 1, 1] = -999.0  # encoded as _FillValue on write, masked to NaN on read
    path = write_sample(tmp_path / "fill.nc", u=u, v=np.full((12, 3, 3), 0.1))
    series = extract_point_series(path, latitude=0.375, longitude=-151.75, start="2020-01-01", end="2020-01-12")
    assert np.isnan(series.u[0])
    processed = process_current_series(series)
    assert processed.samples[0].status is SampleStatus.MISSING
    assert processed.summary.usable == 11


def test_missing_variable_raises_loader_error(sample):
    with pytest.raises(DatasetLoadError, match="missing required variable"):
        extract_point_series(sample, latitude=0.3, longitude=-151.7, start="2020-01-01", end="2020-01-12", u_variable="ug")


def test_region_mean_and_status_rules(tmp_path):
    u = np.full((12, 3, 3), 0.3)
    v = np.full((12, 3, 3), 0.4)
    u[1, :, :] = np.nan  # every cell missing -> missing
    u[2, 0, 0] = 9.0  # one present cell out of the documented range -> invalid
    u[3, 0, 0] = np.nan  # one missing cell -> excluded, others averaged
    u[4, 0, 0], u[4, 1, 0] = 0.0, 0.6  # mean of 9 cells: (0 + 0.6 + 7 * 0.3) / 9 = 0.3
    path = write_sample(tmp_path / "region.nc", u=u, v=v)
    series = extract_region_series(
        path, south=0.0, north=1.0, west=-152.0, east=-151.5, start="2020-01-01", end="2020-01-12"
    )
    assert series.selection.region_cell_count == 9
    assert series.forced_status[1] is SampleStatus.MISSING
    assert series.forced_status[2] is SampleStatus.INVALID
    assert series.cell_counts[3] == 8
    assert series.u[3] == pytest.approx(0.3, abs=1e-9)
    assert series.u[4] == pytest.approx(0.3, abs=1e-9)
    processed = process_current_series(series)
    assert processed.samples[0].speed == pytest.approx(0.5, abs=1e-9)  # 0.3-0.4-0.5
    assert processed.samples[1].status is SampleStatus.MISSING
    assert processed.samples[2].status is SampleStatus.INVALID
    assert processed.samples[3].cell_count == 8


def test_region_crossing_antimeridian(tmp_path):
    lons = np.array([-179.75, -179.5, 179.5])
    path = write_sample(tmp_path / "dateline.nc", lons=lons)
    series = extract_region_series(path, south=0.0, north=1.0, west=179.0, east=-179.6, start="2020-01-01", end="2020-01-12")
    # cells at 179.5 and -179.75 (2 lons x 3 lats)
    assert series.selection.region_cell_count == 6


def test_region_without_cells_rejected(sample):
    with pytest.raises(SeriesExtractionError, match="No grid cells"):
        extract_region_series(sample, south=10.0, north=11.0, west=-152.0, east=-151.0, start="2020-01-01", end="2020-01-12")


def test_default_extraction_uses_config(sample):
    settings = Settings(processing_latitude=0.375, processing_longitude=-151.75, processing_end="2020-01-12")
    series = extract_default_series(sample, settings=settings)
    assert series.selection.selected_latitude == 0.375
    assert series.source["dataset"] == "OSCAR_L4_OC_FINAL_V2.0"
    assert len(series.timestamps) == 12
    region = extract_default_series(sample, mode="region", settings=settings)
    assert region.selection.mode == "region"


def test_time_coordinate_identified_by_cf_units_only(tmp_path):
    path = tmp_path / "time_counter.nc"
    shape = (len(TIMES), len(LONS_360), len(LATS))
    dataset = xr.Dataset(
        {
            "u": (("time_counter", "x", "y"), np.full(shape, 0.3), {"units": "m s-1"}),
            "v": (("time_counter", "x", "y"), np.full(shape, 0.4), {"units": "m s-1"}),
        },
        coords={"time_counter": TIMES, "x": LONS_360, "y": LATS},
    )
    dataset["x"].attrs["units"] = "degrees_east"
    dataset["y"].attrs["units"] = "degrees_north"
    dataset.to_netcdf(path, encoding={"time_counter": {"units": "days since 2000-01-01"}})
    dataset.close()
    with xr.open_dataset(path) as opened:
        assert "standard_name" not in opened["time_counter"].attrs
        assert resolve_coordinate(opened, "time") == "time_counter"
    series = extract_point_series(path, latitude=0.375, longitude=-151.75, start="2020-01-01", end="2020-01-12")
    assert len(series.timestamps) == 12


def test_region_converted_sentinel_is_invalid(tmp_path):
    u = np.full((12, 3, 3), 10.0)
    u[0, 0, 0] = -99900.0  # -999 m/s after conversion, not the raw fill value
    path = write_sample(tmp_path / "cms.nc", u=u, v=np.full((12, 3, 3), 10.0), units="cm s-1")
    series = extract_region_series(path, south=0.0, north=1.0, west=-152.0, east=-151.5, start="2020-01-01", end="2020-01-12")
    assert series.forced_status[0] is SampleStatus.INVALID
