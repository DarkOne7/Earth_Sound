from __future__ import annotations

import json
from pathlib import Path

import pytest

import scripts.fetch_ocean_sample as fetcher
from scripts.fetch_ocean_sample import (
    CredentialError,
    FetchError,
    _check_catalog_size,
    _job_url,
    _request_params,
    _stream_response,
    create_authenticated_session,
    fetch_sample,
    parse_args,
)


class FakeResponse:
    def __init__(self, chunks=(), headers=None, *, payload=None, url="https://harmony.earthdata.nasa.gov/request"):
        self._chunks = list(chunks)
        self.headers = headers or {}
        self.payload = payload
        self.url = url
        self.iterated = False
        self.closed = False

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload

    def close(self):
        self.closed = True

    def iter_content(self, chunk_size):
        self.iterated = True
        yield from self._chunks


def test_default_selection_is_pinned_to_small_final_sample():
    args = parse_args([])
    assert args.access == "opendap"
    assert args.collection == "OSCAR_L4_OC_FINAL_V2.0"
    assert args.version == "2.0"
    assert args.start_date == "2020-01-01"
    assert args.end_date == "2020-01-30"
    assert args.variables == ["u", "v"]
    assert args.bbox == (-152.0, 0.0, -150.0, 2.0)


def test_full_granule_over_size_limit_is_refused_without_override():
    granules = [{"granule_size": "31.7"}]
    with pytest.raises(FetchError, match="above the"):
        _check_catalog_size(granules, bbox=None, max_bytes=25 * 1024 * 1024, allow_large=False)


def test_subset_catalog_size_is_recorded_and_output_cap_remains_separate():
    size = _check_catalog_size(
        [{"granule_size": "31.7"}],
        bbox=(-152.0, 0.0, -150.0, 2.0),
        max_bytes=25 * 1024 * 1024,
        allow_large=False,
    )
    assert size == 31.7


def test_harmony_request_uses_documented_subset_axes_and_netcdf_format():
    params = _request_params(
        granule_name=None,
        start_date="2020-01-01",
        end_date="2020-01-30",
        bbox=(-152.0, 0.0, -150.0, 2.0),
    )
    assert ("format", "application/netcdf") in params
    assert ("subset", 'time("2020-01-01T00:00:00Z":"2020-01-30T23:59:59Z")') in params
    assert ("subset", "lat(0.0:2.0)") in params
    assert ("subset", "lon(-152.0:-150.0)") in params


def test_missing_earthdata_credentials_fail_actionably_without_printing_secrets(monkeypatch):
    monkeypatch.delenv("EARTHDATA_TOKEN", raising=False)
    monkeypatch.setattr("scripts.fetch_ocean_sample._earthdata_netrc_credentials_available", lambda: False)
    with pytest.raises(CredentialError, match="Earthdata Login credentials are missing"):
        create_authenticated_session()


def test_harmony_job_location_must_remain_on_trusted_https_host():
    response = FakeResponse(
        headers={"Location": "https://evil.example/jobs/123"},
        url="https://harmony.earthdata.nasa.gov/request",
    )
    with pytest.raises(FetchError, match="untrusted job URL"):
        _job_url(response, {})


NETCDF_PAYLOAD = b"CDF\x01small mocked NetCDF payload"


def _patch_catalog(monkeypatch, repo_root):
    monkeypatch.setattr(fetcher, "REPO_ROOT", repo_root)
    monkeypatch.setattr(fetcher, "find_collection", lambda *_: {"id": "C-test"})
    monkeypatch.setattr(
        fetcher,
        "search_granules",
        lambda *_args, **_kwargs: [
            {
                "title": "oscar_currents_final_20200101",
                "time_start": "2020-01-01T00:00:00Z",
                "time_end": "2020-01-01T23:59:59Z",
                "granule_size": "31.7",
            }
        ],
    )


class RecordingSession:
    def __init__(self, content=NETCDF_PAYLOAD):
        self.calls = []
        self.content = content

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return FakeResponse(
            chunks=[self.content],
            headers={"Content-Type": "application/x-netcdf", "Content-Length": str(len(self.content))},
        )


def test_fetch_writes_bounded_sample_and_idempotently_reuses_matching_record(
    monkeypatch, tmp_path
):
    _patch_catalog(monkeypatch, tmp_path)
    args = parse_args(["--access", "harmony"])
    session = RecordingSession()
    outputs = fetch_sample(args, session=session)
    assert outputs == [tmp_path / "data/raw/oscar-final-20200101-20200130/oscar-final-20200101-20200130.nc"]
    assert outputs[0].read_bytes() == NETCDF_PAYLOAD
    assert (tmp_path / "data/metadata/oscar-final-20200101-20200130.retrieval.json").is_file()
    assert len(session.calls) == 1

    second_session = RecordingSession()
    reused = fetch_sample(args, session=second_session)
    assert reused == outputs
    assert second_session.calls == []


def test_record_paths_are_repo_relative_and_reused_from_another_cwd(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(parents=True)
    _patch_catalog(monkeypatch, repo)
    monkeypatch.chdir(elsewhere)
    outputs = fetch_sample(parse_args(["--access", "harmony"]), session=RecordingSession())
    assert outputs[0].is_relative_to(repo)
    assert not (elsewhere / "data").exists()

    record = json.loads((repo / "data/metadata/oscar-final-20200101-20200130.retrieval.json").read_text())
    assert record["outputs"][0]["path"] == "data/raw/oscar-final-20200101-20200130/oscar-final-20200101-20200130.nc"

    monkeypatch.chdir(repo / "data")
    second_session = RecordingSession()
    assert fetch_sample(parse_args(["--access", "harmony"]), session=second_session) == outputs
    assert second_session.calls == []


def test_different_bbox_variables_or_output_dir_get_separate_samples(monkeypatch, tmp_path):
    _patch_catalog(monkeypatch, tmp_path)
    default = fetch_sample(parse_args(["--access", "harmony"]), session=RecordingSession())

    other_bbox = fetch_sample(parse_args(["--access", "harmony", "--bbox", "-10", "0", "-8", "2"]), session=RecordingSession())
    other_vars = fetch_sample(parse_args(["--access", "harmony", "--variables", "u"]), session=RecordingSession())
    other_dir_args = parse_args(["--access", "harmony"])
    other_dir_args.output_dir = tmp_path / "custom"
    other_dir_session = RecordingSession()
    other_dir = fetch_sample(other_dir_args, session=other_dir_session)

    paths = {default[0], other_bbox[0], other_vars[0], other_dir[0]}
    assert len(paths) == 4
    assert other_dir_session.calls, "a new --output-dir must not reuse the default sample"
    assert other_dir[0].parent == tmp_path / "custom"
    records = sorted((tmp_path / "data/metadata").glob("*.retrieval.json"))
    assert len(records) == 4
    assert all(path.is_file() for path in paths)


def test_dates_are_compared_as_dates_and_normalized():
    args = parse_args(["--start-date", "2020-1-5", "--end-date", "2020-01-30"])
    assert (args.start_date, args.end_date) == ("2020-01-05", "2020-01-30")
    with pytest.raises(SystemExit):
        parse_args(["--start-date", "2020-10-01", "--end-date", "2020-9-01"])


def test_non_netcdf_response_is_rejected_and_removed(tmp_path):
    response = FakeResponse(
        chunks=[b"<!DOCTYPE html><html>Earthdata Login</html>"],
        headers={"Content-Type": "text/html"},
    )
    destination = tmp_path / "sample.nc"
    with pytest.raises(FetchError, match="not a NetCDF file"):
        _stream_response(response, destination, max_bytes=1024)
    assert not destination.exists()
    assert not Path(str(destination) + ".part").exists()


@pytest.mark.parametrize("signature", [b"CDF\x01", b"CDF\x02", b"\x89HDF\r\n\x1a\n"])
def test_netcdf_signatures_are_accepted_across_chunks(tmp_path, signature):
    payload = signature + b"payload"
    response = FakeResponse(chunks=[payload[:3], payload[3:]])
    result = _stream_response(response, tmp_path / "sample.nc", max_bytes=1024)
    assert result["bytes"] == len(payload)


def test_response_declared_over_limit_is_refused_before_streaming(tmp_path):
    response = FakeResponse(chunks=[b"not-read"], headers={"Content-Length": "101"})
    with pytest.raises(FetchError, match="declares 101 bytes"):
        _stream_response(response, tmp_path / "sample.nc", max_bytes=100)
    assert response.iterated is False
    assert not (tmp_path / "sample.nc").exists()


def test_unknown_length_stream_aborts_at_hard_size_cap(tmp_path):
    response = FakeResponse(chunks=[b"12345", b"67890"])
    destination = tmp_path / "sample.nc"
    with pytest.raises(FetchError, match="exceeded the 8-byte cap"):
        _stream_response(response, destination, max_bytes=8)
    assert response.iterated is True
    assert not destination.exists()
    assert not Path(str(destination) + ".part").exists()


# --- Harmony route, job status, links and multi-granule combination ----------

import numpy as np  # noqa: E402
import xarray as xr  # noqa: E402

from scripts.fetch_ocean_sample import (  # noqa: E402
    _data_links,
    _request_url,
    _unique_filenames,
    combine_outputs,
    wait_for_job,
)


def test_request_url_includes_ogc_collections_segment():
    # Regression: the route without "collections/" returned HTTP 404 from Harmony.
    assert _request_url("C2098858642-POCLOUD", ["u", "v"]) == (
        "https://harmony.earthdata.nasa.gov/C2098858642-POCLOUD/"
        "ogc-api-coverages/1.0.0/collections/u,v/coverage/rangeset"
    )


def test_data_links_require_rel_data_and_netcdf():
    payload = {
        "links": [
            {"href": "https://harmony.earthdata.nasa.gov/a.nc4", "rel": "data", "type": "application/netcdf"},
            {"href": "https://harmony.earthdata.nasa.gov/b", "rel": "data", "type": "application/x-netcdf4"},
            {"href": "https://harmony.earthdata.nasa.gov/c.nc", "rel": "data"},
            {"href": "https://harmony.earthdata.nasa.gov/catalog.json", "rel": "data", "type": "application/json"},
            {"href": "https://harmony.earthdata.nasa.gov/d.nc", "rel": "stac-catalog-json"},
        ]
    }
    assert _data_links(payload) == [
        "https://harmony.earthdata.nasa.gov/a.nc4",
        "https://harmony.earthdata.nasa.gov/b",
        "https://harmony.earthdata.nasa.gov/c.nc",
    ]


def test_colliding_basenames_get_unique_names():
    names = _unique_filenames(
        ["https://x.example/a/subset.nc", "https://x.example/b/subset.nc", "https://x.example/c.nc"]
    )
    assert names == ["subset.nc", "002-subset.nc", "c.nc"]


class JobSession:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.calls = 0

    def get(self, url, **kwargs):
        self.calls += 1
        return FakeResponse(payload=self.payloads.pop(0), headers={"Content-Type": "application/json"})


DATA_LINK = {"href": "https://harmony.earthdata.nasa.gov/out.nc4", "rel": "data", "type": "application/netcdf"}
JOB_URL = "https://harmony.earthdata.nasa.gov/jobs/1"


def _wait(session):
    return wait_for_job(session, JOB_URL, poll_seconds=0, timeout_seconds=10, sleep=lambda _: None)


def test_running_job_with_partial_links_keeps_polling():
    second = {**DATA_LINK, "href": DATA_LINK["href"] + "2"}
    session = JobSession(
        [
            {"status": "running", "progress": 50, "links": [DATA_LINK]},
            {"status": "successful", "progress": 100, "links": [DATA_LINK, second]},
        ]
    )
    assert len(_wait(session)) == 2
    assert session.calls == 2


def test_progress_100_without_successful_status_is_not_success():
    session = JobSession(
        [
            {"status": "running", "progress": 100, "links": [DATA_LINK]},
            {"status": "successful", "progress": 100, "links": [DATA_LINK]},
        ]
    )
    _wait(session)
    assert session.calls == 2


@pytest.mark.parametrize("status", ["complete_with_errors", "failed", "canceled"])
def test_failed_or_partial_jobs_are_rejected(status):
    session = JobSession([{"status": status, "progress": 100, "links": [DATA_LINK], "message": "boom"}])
    with pytest.raises(FetchError, match="Harmony job"):
        _wait(session)


def _daily_file(path, day, *, lats=(0.125, 0.375)):
    dataset = xr.Dataset(
        {
            "u": (("time", "longitude", "latitude"), np.full((1, 2, len(lats)), 0.1 * day), {"units": "m s-1"}),
            "v": (("time", "longitude", "latitude"), np.full((1, 2, len(lats)), 0.2), {"units": "m s-1"}),
        },
        coords={
            "time": np.array([f"2020-01-{day:02d}"], dtype="datetime64[ns]"),
            "longitude": [208.0, 208.25],
            "latitude": list(lats),
        },
    )
    dataset["time"].attrs["standard_name"] = "time"
    dataset.to_netcdf(path, encoding={"u": {"_FillValue": -999.0}, "v": {"_FillValue": -999.0}})
    dataset.close()
    return path


def test_combine_outputs_concatenates_sorted_by_time(tmp_path):
    paths = [_daily_file(tmp_path / f"d{day}.nc", day) for day in (3, 1, 2)]
    result = combine_outputs(paths, tmp_path / "combined.nc")
    assert result["source_count"] == 3
    assert len(result["sha256"]) == 64
    with xr.open_dataset(tmp_path / "combined.nc") as combined:
        assert [str(t)[:10] for t in combined["time"].values] == ["2020-01-01", "2020-01-02", "2020-01-03"]
        assert dict(combined["u"].sizes) == {"time": 3, "longitude": 2, "latitude": 2}
        assert combined["u"].encoding.get("_FillValue") == -999.0
        assert combined["longitude"].dims == ("longitude",)


def test_combine_outputs_rejects_mismatched_grids_and_duplicates(tmp_path):
    a = _daily_file(tmp_path / "a.nc", 1)
    b = _daily_file(tmp_path / "b.nc", 2, lats=(0.125, 0.5))
    with pytest.raises(FetchError, match="grids or variables differ"):
        combine_outputs([a, b], tmp_path / "bad.nc")
    c = _daily_file(tmp_path / "c.nc", 1)
    with pytest.raises(FetchError, match="duplicate timestamps"):
        combine_outputs([a, c], tmp_path / "dup.nc")


class AsyncHarmonySession:
    """Initial JSON job response, one poll, then per-granule NetCDF downloads."""

    def __init__(self, files):
        self.files = files
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(url)
        json_headers = {"Content-Type": "application/json"}
        if "rangeset" in url:
            return FakeResponse(
                payload={"status": "running", "progress": 0, "jobID": "job-1", "links": []}, headers=json_headers
            )
        if "/jobs/" in url:
            links = [
                {
                    "href": f"https://harmony.earthdata.nasa.gov/service-results/{i}/subset.nc4",
                    "rel": "data",
                    "type": "application/netcdf",
                }
                for i in range(len(self.files))
            ]
            return FakeResponse(payload={"status": "successful", "progress": 100, "links": links}, headers=json_headers)
        index = int(url.split("/service-results/")[1].split("/")[0])
        content = self.files[index].read_bytes()
        return FakeResponse(
            chunks=[content], headers={"Content-Type": "application/netcdf", "Content-Length": str(len(content))}
        )


def test_fetch_async_job_downloads_granules_and_combines_them(monkeypatch, tmp_path):
    _patch_catalog(monkeypatch, tmp_path)
    sources = [_daily_file(tmp_path / f"src{day}.nc", day) for day in (1, 2)]
    args = parse_args(["--access", "harmony", "--poll-seconds", "0.001"])
    session = AsyncHarmonySession(sources)
    outputs = fetch_sample(args, session=session)
    sample_dir = tmp_path / "data/raw/oscar-final-20200101-20200130"
    assert outputs[0] == sample_dir / "oscar-final-20200101-20200130.nc"
    assert {p.name for p in outputs[1:]} == {"subset.nc4", "002-subset.nc4"}
    assert "/collections/u,v/coverage/rangeset" in session.calls[0]
    record_path = tmp_path / "data/metadata/oscar-final-20200101-20200130.retrieval.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    assert [o["role"] for o in record["outputs"]] == ["granule", "granule", "combined"]
    with xr.open_dataset(outputs[0]) as combined:
        assert combined.sizes["time"] == 2
    # Idempotent: a second run reuses the record and returns the combined file first.
    again = fetch_sample(args, session=AsyncHarmonySession(sources))
    assert again == outputs


def test_initial_running_payload_with_links_is_not_downloaded_directly(monkeypatch, tmp_path):
    _patch_catalog(monkeypatch, tmp_path)
    sources = [_daily_file(tmp_path / "src1.nc", 1)]

    class PartialFirst(AsyncHarmonySession):
        def get(self, url, **kwargs):
            if "rangeset" in url:
                self.calls.append(url)
                partial = {**DATA_LINK, "href": "https://harmony.earthdata.nasa.gov/service-results/0/early.nc4"}
                return FakeResponse(
                    payload={"status": "running", "progress": 10, "jobID": "job-1", "links": [partial]},
                    headers={"Content-Type": "application/json"},
                )
            return super().get(url, **kwargs)

    session = PartialFirst(sources)
    fetch_sample(parse_args(["--access", "harmony", "--poll-seconds", "0.001"]), session=session)
    assert any("/jobs/" in call for call in session.calls)


def test_failed_job_reports_job_id_and_distinct_errors():
    payload = {
        "status": "failed",
        "jobID": "abc-123",
        "message": "75 percent maximum errors exceeded. See the errors fields for more details",
        "errors": [
            {"url": "https://x.example/g1.nc", "message": "Variable dimension mismatch"},
            {"url": "https://x.example/g2.nc", "message": "Variable dimension mismatch"},
            {"url": "https://x.example/g3.nc", "message": "Request bounding box\n outside granule"},
        ],
    }
    with pytest.raises(FetchError) as info:
        _wait(JobSession([payload]))
    text = str(info.value)
    assert "Job ID: abc-123" in text
    assert "workflow-ui/abc-123" in text
    assert "3 total, 2 distinct" in text
    assert "[2x] Variable dimension mismatch" in text
    assert "[1x] Request bounding box outside granule" in text



# --- PO.DAAC Cloud OPeNDAP access (default) -----------------------------------

from scripts.fetch_ocean_sample import (  # noqa: E402
    DmrStructure,
    GridLayout,
    bbox_index_ranges,
    coordinate_index_range,
    dap4_constraint,
    granule_opendap_url,
    parse_dmr,
    resolve_grid_layout,
)

OPENDAP_GRANULE = "https://opendap.earthdata.nasa.gov/collections/C2098858642-POCLOUD/granules/oscar_currents_final_{day}"

# Mirrors the OSCAR v2.0 NetCDF header in the ESR user guide: dimensions latitude/longitude,
# coordinate variables lat(latitude)/lon(longitude), u(time, longitude, latitude).
OSCAR_DMR = """<?xml version="1.0" encoding="ISO-8859-1"?>
<Dataset xmlns="http://xml.opendap.org/ns/DAP/4.0#" dapVersion="4.0" dmrVersion="1.0" name="oscar_currents_final_20200101.nc">
  <Dimension name="latitude" size="719"/>
  <Dimension name="longitude" size="1440"/>
  <Dimension name="time" size="1"/>
  <Float64 name="lat"><Dim name="/latitude"/>
    <Attribute name="standard_name" type="String"><Value>latitude</Value></Attribute>
    <Attribute name="units" type="String"><Value>degrees_north</Value></Attribute>
  </Float64>
  <Float64 name="lon"><Dim name="/longitude"/>
    <Attribute name="standard_name" type="String"><Value>longitude</Value></Attribute>
    <Attribute name="units" type="String"><Value>degrees_east</Value></Attribute>
  </Float64>
  <Float64 name="time"><Dim name="/time"/>
    <Attribute name="standard_name" type="String"><Value>time</Value></Attribute>
    <Attribute name="axis" type="String"><Value>T</Value></Attribute>
  </Float64>
  <Float64 name="u"><Dim name="/time"/><Dim name="/longitude"/><Dim name="/latitude"/>
    <Attribute name="units" type="String"><Value>m s-1</Value></Attribute>
  </Float64>
  <Float64 name="v"><Dim name="/time"/><Dim name="/longitude"/><Dim name="/latitude"/>
    <Attribute name="units" type="String"><Value>m s-1</Value></Attribute>
  </Float64>
</Dataset>"""

OSCAR_LATS = np.round(np.arange(-89.75, 89.75 + 0.001, 0.25), 2)  # 719 values
OSCAR_LONS = np.round(np.arange(0.0, 359.75 + 0.001, 0.25), 2)  # 1440 values


def test_parse_dmr_reads_dimensions_variables_and_attributes():
    structure = parse_dmr(OSCAR_DMR)
    assert structure.dimensions == {"latitude": 719, "longitude": 1440, "time": 1}
    assert structure.variables["u"].dims == ("time", "longitude", "latitude")
    assert structure.variables["lat"].attributes["standard_name"] == "latitude"


def test_parse_dmr_rejects_login_page():
    with pytest.raises(FetchError, match="unreadable DMR"):
        parse_dmr("<html><body>Earthdata Login<br></body>")


def test_grid_layout_uses_cf_coordinates_not_dimension_names():
    layout = resolve_grid_layout(parse_dmr(OSCAR_DMR), ["u", "v"])
    assert layout == GridLayout("lat", "lon", "time", "latitude", "longitude")


def test_grid_layout_rejects_missing_variable():
    with pytest.raises(FetchError, match="not in the granule DMR"):
        resolve_grid_layout(parse_dmr(OSCAR_DMR), ["u", "sst"])


def test_bbox_maps_to_index_ranges_on_the_real_oscar_grid():
    lat_range, lon_range, native = bbox_index_ranges((-152.0, 0.0, -150.0, 2.0), OSCAR_LATS, OSCAR_LONS)
    # lat = -89.75 + 0.25 * i -> 0.0 at i=359, 2.0 at i=367; lon = 0.25 * j -> 208 at 832, 210 at 840
    assert lat_range == (359, 367)
    assert lon_range == (832, 840)
    assert native == (208.0, 210.0)
    assert OSCAR_LATS[359] == 0.0 and OSCAR_LONS[840] == 210.0


def test_bbox_across_0_360_seam_is_refused():
    with pytest.raises(FetchError, match="crosses 0 deg"):
        bbox_index_ranges((-10.0, 0.0, 10.0, 2.0), OSCAR_LATS, OSCAR_LONS)


def test_coordinate_index_range_rejects_empty_and_non_contiguous():
    with pytest.raises(FetchError, match="No latitude"):
        coordinate_index_range([1.0, 2.0], 5.0, 6.0, name="latitude")
    with pytest.raises(FetchError, match="not contiguous"):
        coordinate_index_range([0.0, 5.0, 1.0], 0.0, 1.0, name="latitude")


def test_dap4_constraint_follows_each_variables_dimension_order():
    structure = parse_dmr(OSCAR_DMR)
    layout = resolve_grid_layout(structure, ["u", "v"])
    ce = dap4_constraint(structure, layout, ["u", "v"], (359, 367), (832, 840))
    assert ce == (
        "/u[0:0][832:840][359:367];/v[0:0][832:840][359:367];/lat[359:367];/lon[832:840];/time"
    )


def test_granule_opendap_url_comes_from_cmr_links():
    entry = {
        "title": "oscar_currents_final_20200101",
        "links": [
            {"href": "https://archive.podaac.earthdata.nasa.gov/x/oscar_currents_final_20200101.nc"},
            {"href": "https://opendap.earthdata.nasa.gov"},
            {"href": OPENDAP_GRANULE.format(day="20200101")},
        ],
    }
    assert granule_opendap_url(entry) == OPENDAP_GRANULE.format(day="20200101")
    with pytest.raises(FetchError, match="no OPeNDAP link"):
        granule_opendap_url({"title": "x", "links": [{"href": "https://opendap.earthdata.nasa.gov"}]})


def _oscar_subset_bytes(tmp_path, day, *, lats=OSCAR_LATS[359:368], lons=OSCAR_LONS[832:841]):
    """A NetCDF file shaped like an OPeNDAP OSCAR subset: lat/lon are not dimension coordinates."""
    path = tmp_path / f"subset_{day}.nc4"
    shape = (1, len(lons), len(lats))
    dataset = xr.Dataset(
        {
            "u": (("time", "longitude", "latitude"), np.full(shape, 0.1 * day), {"units": "m s-1", "coordinates": "lon lat"}),
            "v": (("time", "longitude", "latitude"), np.full(shape, 0.2), {"units": "m s-1", "coordinates": "lon lat"}),
            "lat": (("latitude",), np.asarray(lats), {"standard_name": "latitude", "units": "degrees_north"}),
            "lon": (("longitude",), np.asarray(lons), {"standard_name": "longitude", "units": "degrees_east"}),
        },
        coords={"time": ("time", np.array([f"2020-01-{day:02d}T12:00"], dtype="datetime64[ns]"), {"standard_name": "time"})},
    )
    no_fill = {name: {"_FillValue": None} for name in ("lat", "lon", "time")}
    dataset.to_netcdf(path, encoding={"u": {"_FillValue": -999.0}, "v": {"_FillValue": -999.0}, **no_fill})
    dataset.close()
    return path.read_bytes()


def _coordinates_bytes(tmp_path):
    path = tmp_path / "coords.nc4"
    xr.Dataset(
        {
            "lat": (("latitude",), OSCAR_LATS, {"standard_name": "latitude"}),
            "lon": (("longitude",), OSCAR_LONS, {"standard_name": "longitude"}),
        }
    ).to_netcdf(path)
    return path.read_bytes()


class FakeOpendapSession:
    def __init__(self, tmp_path, days, *, status=200, mismatched_day=None):
        self.tmp_path = tmp_path
        self.days = days
        self.status = status
        self.mismatched_day = mismatched_day
        self.calls = []

    def get(self, url, params=None, **kwargs):
        self.calls.append((url, params))
        if self.status != 200:
            response = FakeResponse(headers={"Content-Type": "text/html"})
            response.status_code = self.status
            return response
        if url.endswith(".dmr.xml"):
            response = FakeResponse(headers={"Content-Type": "application/vnd.opendap.dap4.dataset-metadata+xml"})
            response.text = OSCAR_DMR
            response.status_code = 200
            return response
        ce = params["dap4.ce"]
        if ce == "/lat;/lon":
            content = _coordinates_bytes(self.tmp_path)
        else:
            day = int(url.split("final_")[1].split(".")[0][-2:])
            lats = OSCAR_LATS[360:369] if day == self.mismatched_day else OSCAR_LATS[359:368]
            content = _oscar_subset_bytes(self.tmp_path, day, lats=lats)
        response = FakeResponse(chunks=[content], headers={"Content-Type": "application/x-netcdf;ver=4", "Content-Length": str(len(content))})
        response.status_code = 200
        return response


def _patch_opendap_catalog(monkeypatch, repo_root, days):
    monkeypatch.setattr(fetcher, "REPO_ROOT", repo_root)
    monkeypatch.setattr(fetcher, "find_collection", lambda *_: {"id": "C2098858642-POCLOUD"})
    monkeypatch.setattr(
        fetcher,
        "search_granules",
        lambda *_args, **_kwargs: [
            {
                "title": f"oscar_currents_final_202001{day:02d}",
                "time_start": f"2020-01-{day:02d}T00:00:00Z",
                "time_end": f"2020-01-{day:02d}T23:59:59Z",
                "granule_size": "31.7",
                "links": [{"href": OPENDAP_GRANULE.format(day=f"202001{day:02d}")}],
            }
            for day in days
        ],
    )


def test_opendap_fetch_subsets_each_granule_and_combines(monkeypatch, tmp_path):
    days = [1, 2, 3]
    _patch_opendap_catalog(monkeypatch, tmp_path, days)
    session = FakeOpendapSession(tmp_path, days)
    outputs = fetch_sample(parse_args([]), session=session)

    sample_dir = tmp_path / "data/raw/oscar-final-20200101-20200130"
    assert outputs[0] == sample_dir / "oscar-final-20200101-20200130.nc"
    assert sorted(p.name for p in outputs[1:]) == [f"oscar_currents_final_202001{d:02d}.subset.nc4" for d in days]
    assert not (sample_dir / ".coordinates.nc4").exists()
    subset_calls = [params["dap4.ce"] for url, params in session.calls if params and params["dap4.ce"] != "/lat;/lon"]
    assert subset_calls == ["/u[0:0][832:840][359:367];/v[0:0][832:840][359:367];/lat[359:367];/lon[832:840];/time"] * 3

    record = json.loads((tmp_path / "data/metadata/oscar-final-20200101-20200130.retrieval.json").read_text())
    assert record["access_method"].startswith("PO.DAAC Cloud OPeNDAP")
    assert record["index_ranges"]["latitude_values"] == [0.0, 2.0]
    assert record["index_ranges"]["longitude_values"] == [208.0, 210.0]
    assert record["grid_layout"]["latitude_variable"] == "lat"
    assert [o["role"] for o in record["outputs"]] == ["granule", "granule", "granule", "combined"]

    with xr.open_dataset(outputs[0]) as combined:
        assert combined.sizes["time"] == 3
        assert combined["u"].dims == ("time", "longitude", "latitude")

    again = fetch_sample(parse_args([]), session=FakeOpendapSession(tmp_path, days))
    assert again == outputs


def test_opendap_combined_sample_feeds_phase3_extraction(monkeypatch, tmp_path):
    from app.services.series_extraction import extract_point_series

    days = [1, 2, 3]
    _patch_opendap_catalog(monkeypatch, tmp_path, days)
    combined = fetch_sample(parse_args([]), session=FakeOpendapSession(tmp_path, days))[0]
    series = extract_point_series(combined, latitude=1.0, longitude=-151.0, start="2020-01-01", end="2020-01-03")
    assert series.selection.selected_latitude == 1.0
    assert series.selection.selected_longitude == 209.0
    assert len(series.timestamps) == 3


def test_opendap_mismatched_grids_are_not_combined(monkeypatch, tmp_path):
    days = [1, 2]
    _patch_opendap_catalog(monkeypatch, tmp_path, days)
    with pytest.raises(FetchError, match="grids or variables differ"):
        fetch_sample(parse_args([]), session=FakeOpendapSession(tmp_path, days, mismatched_day=2))
    sample_dir = tmp_path / "data/raw/oscar-final-20200101-20200130"
    assert not any(sample_dir.glob("*.nc*"))


@pytest.mark.parametrize("status", [401, 403])
def test_opendap_auth_failure_is_actionable(monkeypatch, tmp_path, status):
    _patch_opendap_catalog(monkeypatch, tmp_path, [1])
    with pytest.raises(FetchError, match=f"HTTP {status}"):
        fetch_sample(parse_args([]), session=FakeOpendapSession(tmp_path, [1], status=status))


def test_opendap_requires_bbox(monkeypatch, tmp_path):
    _patch_opendap_catalog(monkeypatch, tmp_path, [1])
    with pytest.raises(FetchError, match="needs a bbox"):
        fetch_sample(parse_args(["--no-bbox", "--allow-large"]), session=FakeOpendapSession(tmp_path, [1]))


def test_combine_keeps_original_fill_conventions(monkeypatch, tmp_path):
    import netCDF4

    days = [1, 2]
    _patch_opendap_catalog(monkeypatch, tmp_path, days)
    combined = fetch_sample(parse_args([]), session=FakeOpendapSession(tmp_path, days))[0]
    with netCDF4.Dataset(combined) as d:
        for name in ("lat", "lon", "time"):
            assert "_FillValue" not in d[name].ncattrs(), name
        assert d["u"].getncattr("_FillValue") == -999.0


def test_combine_preserves_original_time_encoding(tmp_path):
    import netCDF4

    paths = []
    for day in (1, 2):
        path = tmp_path / f"t{day}.nc"
        with netCDF4.Dataset(path, "w") as d:
            d.createDimension("time", 1)
            d.createDimension("latitude", 1)
            t = d.createVariable("time", "f8", ("time",))
            t.units = "days since 1990-1-1"
            t.calendar = "julian"
            t.standard_name = "time"
            t[:] = [10956.0 + day]
            lat = d.createVariable("lat", "f8", ("latitude",))
            lat.standard_name = "latitude"
            lat[:] = [0.0]
            u = d.createVariable("u", "f8", ("time", "latitude"), fill_value=-999.0)
            u[:] = [[0.1 * day]]
        paths.append(path)
    combine_outputs(paths, tmp_path / "combined.nc")
    with netCDF4.Dataset(tmp_path / "combined.nc") as d:
        t = d["time"]
        assert t.units in ("days since 1990-1-1", "days since 1990-01-01")  # xarray normalises the date text
        assert t.calendar == "julian"
        assert t.dtype == np.float64
        assert list(t[:]) == [10957.0, 10958.0]
        assert "_FillValue" not in t.ncattrs()
