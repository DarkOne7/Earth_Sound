"""Phase 6: the API checked clause by clause against docs/api.md.

``test_api.py`` covers the main paths. This module covers what is left of the
published contract: every documented error status and body, every documented
response header, parameter validation, CORS, and the endpoint inventory.
"""

from __future__ import annotations

import importlib
import json

import pytest
from api_support import WEEKLY_DEATHS, make_settings
from app import config
from app.config import get_settings
from app.main import app
from app.services.catalog import render_cache
from fastapi.testclient import TestClient
from test_case_processing import weekly_rows, write_csv

DOCUMENTED_PATHS = {
    "/health",
    "/domains",
    "/series/ocean",
    "/series/covid",
    "/soundscapes/ocean",
    "/soundscapes/ocean/audio",
    "/soundscapes/covid",
    "/soundscapes/covid/audio",
    "/soundscapes/sirius",
    "/soundscapes/sirius/audio",
}
AUDIO_PATHS = ["/soundscapes/ocean/audio", "/soundscapes/covid/audio", "/soundscapes/sirius/audio"]


# --- inventory -----------------------------------------------------------------


def test_the_published_endpoints_are_exactly_the_documented_ones_and_all_get(client):
    paths = client.get("/openapi.json").json()["paths"]
    assert set(paths) == DOCUMENTED_PATHS
    for path, operations in paths.items():
        assert set(operations) == {"get"}, path


@pytest.mark.parametrize("path", sorted(DOCUMENTED_PATHS))
def test_every_endpoint_rejects_writes(client, path):
    assert client.post(path).status_code == 405


# --- 503 sample_unavailable ------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "domain", "file", "hint"),
    [
        ("/series/ocean", "ocean", "ocean/sample.nc", "fetch_ocean_sample.py"),
        ("/soundscapes/ocean/audio", "ocean", "ocean/sample.nc", "fetch_ocean_sample.py"),
        ("/series/covid", "covid", "covid/covid.csv", "fetch_covid_sample.py"),
        ("/soundscapes/sirius", "sirius", "sirius_stis_005.fits", "calspec"),
        ("/soundscapes/sirius/audio", "sirius", "sirius_stis_005.fits", "calspec"),
    ],
)
def test_every_domain_reports_a_missing_sample_as_503(client, data_dirs, path, domain, file, hint):
    raw, _, _ = data_dirs
    (raw / file).unlink()
    response = client.get(path)
    assert response.status_code == 503
    body = response.json()
    assert body["error"] == "sample_unavailable"
    assert body["domain"] == domain
    assert body["sample_file"] == file.split("/")[-1]
    assert hint in body["fetch_hint"]
    assert str(raw) not in response.text


# --- 500 sample_invalid ------------------------------------------------------------


def _corrupt(raw, domain: str, kind: str) -> None:
    if domain == "ocean":
        (raw / "ocean" / "sample.nc").write_bytes(b"this is not a NetCDF file")
    elif domain == "sirius":
        (raw / "sirius_stis_005.fits").write_bytes(b"SIMPLE  =" + b" " * 2871)
    elif kind == "columns":
        (raw / "covid" / "covid.csv").write_text("date,location\n2020-01-05,World\n", encoding="utf-8")
    elif kind == "empty":
        (raw / "covid" / "covid.csv").write_bytes(b"")
    elif kind == "binary":
        (raw / "covid" / "covid.csv").write_bytes(b"\x00\xff\xfe\x89PNG\r\n")
    elif kind == "duplicates":
        (raw / "covid" / "covid.csv").write_text(
            "date,location,new_cases,new_deaths\n2020-01-05,World,1,1\n2020-01-05,World,2,2\n", encoding="utf-8"
        )
    else:
        (raw / "covid" / "covid.csv").write_text(
            "date,location,new_cases,new_deaths\nnot-a-date,World,1,1\n", encoding="utf-8"
        )


@pytest.mark.parametrize(
    ("path", "domain", "kind"),
    [
        ("/series/ocean", "ocean", ""),
        ("/soundscapes/ocean", "ocean", ""),
        ("/series/covid", "covid", "columns"),
        ("/soundscapes/covid/audio", "covid", "dates"),
        ("/series/covid", "covid", "empty"),
        ("/series/covid", "covid", "binary"),
        ("/soundscapes/covid", "covid", "duplicates"),
        ("/soundscapes/sirius", "sirius", ""),
    ],
)
def test_a_malformed_sample_is_500_sample_invalid_without_leaking_paths(client, data_dirs, path, domain, kind):
    raw, _, _ = data_dirs
    _corrupt(raw, domain, kind)
    response = client.get(path)
    assert response.status_code == 500, response.text
    assert response.json()["error"] == "sample_invalid"
    assert response.json()["detail"]
    assert str(raw) not in response.text
    assert str(raw.parent) not in response.text


# --- 422 unprocessable_data and validation ----------------------------------------


@pytest.mark.parametrize(
    "params",
    [
        {"start": "2020-03-29", "end": "2020-01-12"},  # start after end
        {"start": "2031-01-01"},  # after the sample
        {"end": "2019-01-01"},  # before the sample
    ],
)
def test_an_empty_window_is_422_unprocessable_data(client, params):
    for path in ("/series/covid", "/soundscapes/covid"):
        response = client.get(path, params=params)
        assert response.status_code == 422
        assert response.json()["error"] == "unprocessable_data"


def test_window_bounds_are_inclusive_over_http(client):
    body = client.get("/series/covid", params={"start": "2020-01-12", "end": "2020-03-22"}).json()
    weeks = [s["week_ending"] for s in body["samples"]]
    assert weeks[0] == "2020-01-12" and weeks[-1] == "2020-03-22"
    assert len(weeks) == 11


def test_unknown_location_lists_available_locations_without_paths(client, data_dirs):
    raw, _, _ = data_dirs
    response = client.get("/soundscapes/covid/audio", params={"location": "Atlantis"})
    assert response.status_code == 422
    assert "Italy" in response.json()["detail"] and "World" in response.json()["detail"]
    assert str(raw) not in response.text


@pytest.mark.parametrize(
    ("path", "params"),
    [
        ("/series/covid", {"start": "2020-13-01"}),
        ("/series/covid", {"end": "yesterday"}),
        ("/series/covid", {"location": ""}),
        ("/series/covid", {"location": "x" * 101}),
        ("/soundscapes/covid", {"metric": "DEATHS"}),
        ("/soundscapes/ocean", {"timing_mode": "random"}),
        ("/soundscapes/ocean", {"rate_sensitivity": "0"}),
        ("/soundscapes/ocean", {"rate_sensitivity": "-1"}),
        ("/soundscapes/ocean/audio", {"pan_enabled": "sometimes"}),
        ("/soundscapes/ocean/audio", {"duration_seconds": "inf"}),
        ("/soundscapes/ocean/audio", {"duration_seconds": "thirty"}),
    ],
)
def test_invalid_parameters_are_422_validation_errors(client, path, params):
    response = client.get(path, params=params)
    assert response.status_code == 422
    assert "detail" in response.json()


def test_cases_metric_is_served(client, data_dirs):
    raw, _, _ = data_dirs
    rows = weekly_rows("World", WEEKLY_DEATHS, cases="")
    rows = [(d, loc, deaths, deaths) for d, loc, _cases, deaths in rows]  # cases column populated
    write_csv(raw / "covid", rows, name="covid.csv")
    body = client.get("/series/covid", params={"metric": "cases"}).json()
    assert body["metadata"]["selection"]["metric"] == "cases"
    assert client.get("/soundscapes/covid/audio", params={"metric": "cases"}).headers[
        "content-disposition"
    ] == 'inline; filename="covid.world.cases.wav"'


# --- audio delivery headers --------------------------------------------------------


@pytest.mark.parametrize("path", AUDIO_PATHS)
def test_audio_carries_every_documented_header(client, path):
    response = client.get(path)
    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/wav"
    assert response.headers["accept-ranges"] == "bytes"
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["etag"].startswith('"') and len(response.headers["etag"]) == 66
    assert response.headers["content-disposition"].startswith('inline; filename="')
    assert int(response.headers["content-length"]) == len(response.content)


@pytest.mark.parametrize(
    ("path", "params", "filename"),
    [
        ("/soundscapes/ocean/audio", {}, "sample.point.wav"),
        ("/soundscapes/ocean/audio", {"mode": "region"}, "sample.region.wav"),
        ("/soundscapes/covid/audio", {}, "covid.world.deaths.wav"),
        ("/soundscapes/covid/audio", {"start": "2020-01-19"}, "covid.world.deaths.2020-01-19_end.wav"),
        ("/soundscapes/covid/audio", {"end": "2020-03-22"}, "covid.world.deaths.start_2020-03-22.wav"),
    ],
)
def test_content_disposition_uses_the_script_stems(client, path, params, filename):
    disposition = client.get(path, params=params).headers["content-disposition"]
    assert disposition == f'inline; filename="{filename}"'


@pytest.mark.parametrize("path", AUDIO_PATHS)
def test_304_and_206_keep_the_validators(client, path):
    etag = client.get(path).headers["etag"]
    not_modified = client.get(path, headers={"If-None-Match": f'"other", {etag}'})
    assert not_modified.status_code == 304
    assert not_modified.headers["etag"] == etag
    assert client.get(path, headers={"If-None-Match": "*"}).status_code == 304

    partial = client.get(path, headers={"Range": "bytes=0-43", "If-Range": etag})
    assert partial.status_code == 206
    assert partial.headers["etag"] == etag
    assert partial.headers["content-type"] == "audio/wav"
    assert partial.content[:4] == b"RIFF" and len(partial.content) == 44


@pytest.mark.parametrize("header", ["bytes=-0", "bytes=10-5"])
def test_other_unsatisfiable_ranges_are_416(client, header):
    size = len(client.get("/soundscapes/covid/audio").content)
    response = client.get("/soundscapes/covid/audio", headers={"Range": header})
    assert response.status_code == 416
    assert response.headers["content-range"] == f"bytes */{size}"


@pytest.mark.parametrize("header", ["items=0-10", "bytes=", "bytes=-", "bytes=a-b"])
def test_malformed_ranges_send_the_whole_file(client, header):
    size = len(client.get("/soundscapes/covid/audio").content)
    response = client.get("/soundscapes/covid/audio", headers={"Range": header})
    assert response.status_code == 200 and len(response.content) == size


def test_a_suffix_range_longer_than_the_file_returns_the_whole_file_as_206(client):
    size = len(client.get("/soundscapes/covid/audio").content)
    response = client.get("/soundscapes/covid/audio", headers={"Range": f"bytes=-{size * 2}"})
    assert response.status_code == 206
    assert response.headers["content-range"] == f"bytes 0-{size - 1}/{size}"


# --- caching -------------------------------------------------------------------------


def test_provenance_then_audio_renders_once(client):
    render_cache.clear()
    client.get("/soundscapes/covid")
    entries = len(render_cache)
    client.get("/soundscapes/covid/audio")
    assert len(render_cache) == entries


def test_a_zero_size_cache_still_serves_identical_audio(data_dirs):
    raw, metadata, _ = data_dirs
    app.dependency_overrides[get_settings] = lambda: make_settings(raw, metadata, api_render_cache_size=0)
    try:
        render_cache.clear()
        with TestClient(app) as test_client:
            first = test_client.get("/soundscapes/covid/audio").content
            assert len(render_cache) == 0
            assert test_client.get("/soundscapes/covid/audio").content == first
    finally:
        app.dependency_overrides.clear()
        render_cache.clear()


# --- CORS ------------------------------------------------------------------------------


@pytest.fixture
def cors_app(monkeypatch):
    """Re-import the app with CORS configured, as uvicorn would at start-up."""
    from app import main

    monkeypatch.setenv("EARTH_JUKEBOX_CORS_ORIGINS", json.dumps(["http://localhost:5173"]))
    config.get_settings.cache_clear()
    try:
        yield importlib.reload(main).app
    finally:
        monkeypatch.delenv("EARTH_JUKEBOX_CORS_ORIGINS")
        config.get_settings.cache_clear()
        importlib.reload(main)


def test_cors_is_off_by_default(client):
    response = client.get("/health", headers={"Origin": "http://localhost:5173"})
    assert "access-control-allow-origin" not in response.headers


def test_cors_allows_the_configured_origin_and_exposes_audio_headers(cors_app):
    with TestClient(cors_app) as cors_client:
        preflight = cors_client.options(
            "/soundscapes/covid/audio",
            headers={
                "Origin": "http://localhost:5173",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "range",
            },
        )
        assert preflight.status_code == 200
        assert preflight.headers["access-control-allow-origin"] == "http://localhost:5173"
        assert "range" in preflight.headers["access-control-allow-headers"].lower()

        response = cors_client.get("/health", headers={"Origin": "http://localhost:5173"})
        assert response.headers["access-control-allow-origin"] == "http://localhost:5173"
        exposed = response.headers["access-control-expose-headers"].lower()
        for header in ("etag", "content-range", "accept-ranges", "content-disposition"):
            assert header in exposed

        other = cors_client.get("/health", headers={"Origin": "https://evil.example"})
        assert "access-control-allow-origin" not in other.headers
        refused = cors_client.options(
            "/health",
            headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "POST"},
        )
        assert refused.status_code == 400
