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


def test_harmony_request_uses_documented_subset_axes_and_netcd4_format():
    params = _request_params(
        granule_name=None,
        start_date="2020-01-01",
        end_date="2020-01-30",
        bbox=(-152.0, 0.0, -150.0, 2.0),
    )
    assert ("format", "application/x-netcdf4") in params
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
    args = parse_args([])
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
    outputs = fetch_sample(parse_args([]), session=RecordingSession())
    assert outputs[0].is_relative_to(repo)
    assert not (elsewhere / "data").exists()

    record = json.loads((repo / "data/metadata/oscar-final-20200101-20200130.retrieval.json").read_text())
    assert record["outputs"][0]["path"] == "data/raw/oscar-final-20200101-20200130/oscar-final-20200101-20200130.nc"

    monkeypatch.chdir(repo / "data")
    second_session = RecordingSession()
    assert fetch_sample(parse_args([]), session=second_session) == outputs
    assert second_session.calls == []


def test_different_bbox_variables_or_output_dir_get_separate_samples(monkeypatch, tmp_path):
    _patch_catalog(monkeypatch, tmp_path)
    default = fetch_sample(parse_args([]), session=RecordingSession())

    other_bbox = fetch_sample(parse_args(["--bbox", "-10", "0", "-8", "2"]), session=RecordingSession())
    other_vars = fetch_sample(parse_args(["--variables", "u"]), session=RecordingSession())
    other_dir_args = parse_args([])
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
