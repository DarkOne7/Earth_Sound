from __future__ import annotations

import numpy as np
import pytest
import xarray as xr

from app.services.dataset_loader import DatasetLoadError, open_ocean_dataset
from scripts.inspect_ocean_sample import inspect_netcdf, main as inspect_main


@pytest.fixture
def sample_netcdf(tmp_path):
    path = tmp_path / "tiny_ocean_sample.nc"
    values = np.array(
        [
            [[1.0, -9999.0], [2.0, 99.0]],
            [[2.0, 4.0], [3.0, 5.0]],
        ],
        dtype=np.float32,
    )
    dataset = xr.Dataset(
        {"u": (("time", "lat", "lon"), values)},
        coords={
            "time": np.array(["2020-01-01", "2020-01-02"], dtype="datetime64[D]"),
            "lat": np.array([0.0, 0.25]),
            "lon": np.array([208.0, 208.25]),
        },
        attrs={"title": "Synthetic test fixture", "source": "created in test"},
    )
    dataset["u"].attrs.update(
        {
            "units": "m s-1",
            "long_name": "zonal total surface current",
            "valid_min": -5.0,
            "valid_max": 5.0,
        }
    )
    dataset.to_netcdf(path, encoding={"u": {"_FillValue": -9999.0, "dtype": "float32"}})
    dataset.close()
    return path


def test_inspector_reads_dimensions_coordinates_fill_values_and_time(sample_netcdf):
    report = inspect_netcdf(sample_netcdf)

    assert report["dimensions"] == {"time": 2, "lat": 2, "lon": 2}
    assert report["longitude_convention"] == "0 to 360"
    assert report["grid_resolution_degrees"] == {"latitude": 0.25, "longitude": 0.25}
    assert report["time"]["cadence_seconds"] == 86400.0
    assert report["time"]["cadence_regular"] is True
    assert report["variables"]["u"]["units"] == "m s-1"
    assert report["variables"]["u"]["fill_value"] == -9999.0
    assert report["variables"]["u"]["missing_count"] == 1
    assert report["variables"]["u"]["range_check"]["outside_count"] == 1
    assert report["sst"]["present"] is False
    assert report["global_attributes"]["title"] == "Synthetic test fixture"


def test_loader_reads_variable_metadata_and_masks_fill_values(sample_netcdf):
    dataset, metadata = open_ocean_dataset(sample_netcdf, required_variables=("u",))
    try:
        assert metadata.variables["u"].units == "m s-1"
        assert metadata.variables["u"].dimensions == ("time", "lat", "lon")
        assert np.isnan(dataset["u"].values[0, 0, 1])
    finally:
        dataset.close()


def test_loader_reports_missing_file(tmp_path):
    with pytest.raises(DatasetLoadError, match="does not exist"):
        open_ocean_dataset(tmp_path / "missing.nc")


def test_loader_reports_missing_required_variable(sample_netcdf):
    with pytest.raises(DatasetLoadError, match="missing required variable"):
        open_ocean_dataset(sample_netcdf, required_variables=("v",))


def test_inspector_writes_machine_and_human_reports(sample_netcdf, tmp_path):
    report_dir = tmp_path / "metadata"
    result = inspect_main([str(sample_netcdf), "--report-dir", str(report_dir)])
    assert result == 0
    assert (report_dir / "tiny_ocean_sample.inspection.json").is_file()
    text_report = (report_dir / "tiny_ocean_sample.inspection.txt").read_text(encoding="utf-8")
    assert "SST present: no" in text_report
    assert "Global attributes:" in text_report
