from __future__ import annotations

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
    assert ("subset", "time(2020-01-01T00:00:00Z:2020-01-30T23:59:59Z)") in params
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


def test_fetch_writes_bounded_sample_and_idempotently_reuses_matching_record(
    monkeypatch, tmp_path
):
    monkeypatch.chdir(tmp_path)
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
    content = b"small mocked NetCDF payload"
    response = FakeResponse(
        chunks=[content],
        headers={"Content-Type": "application/x-netcdf", "Content-Length": str(len(content))},
    )

    class Session:
        def __init__(self):
            self.calls = []

        def get(self, url, **kwargs):
            self.calls.append((url, kwargs))
            return response

    args = parse_args([])
    args.output_dir = tmp_path / "raw"
    session = Session()
    outputs = fetch_sample(args, session=session)
    assert len(outputs) == 1
    assert outputs[0].read_bytes() == content
    assert (tmp_path / "data/metadata/oscar-final-20200101-20200130.retrieval.json").is_file()
    assert len(session.calls) == 1

    second_session = Session()
    reused = fetch_sample(args, session=second_session)
    assert reused == outputs
    assert second_session.calls == []


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
