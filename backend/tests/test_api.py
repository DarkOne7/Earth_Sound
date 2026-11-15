"""HTTP API tests against synthetic samples, plus a reference check on the real ones."""

from __future__ import annotations

import hashlib
import json

import pytest
from api_support import WEEKLY_DEATHS, make_settings, wav_info
from app.config import REPOSITORY_ROOT, Settings, get_settings
from app.main import app
from app.services.catalog import render_cache
from fastapi.testclient import TestClient
from test_case_processing import weekly_rows, write_csv

# --- catalog -----------------------------------------------------------------


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_domains_report_availability_without_exposing_paths(client, data_dirs):
    raw, _, _ = data_dirs
    (raw / "sirius_stis_005.fits").unlink()
    domains = {d["id"]: d for d in client.get("/domains").json()}
    assert set(domains) == {"ocean", "covid", "sirius"}
    assert domains["ocean"]["sample_available"] and domains["ocean"]["fetch_hint"] is None
    assert not domains["sirius"]["sample_available"]
    assert "calspec" in domains["sirius"]["fetch_hint"]
    assert str(raw) not in json.dumps(domains)


def test_missing_sample_is_503_with_a_fetch_hint(client, data_dirs):
    raw, _, _ = data_dirs
    (raw / "covid" / "covid.csv").unlink()
    response = client.get("/soundscapes/covid")
    assert response.status_code == 503
    body = response.json()
    assert body["error"] == "sample_unavailable"
    assert body["domain"] == "covid"
    assert "fetch_covid_sample.py" in body["fetch_hint"]
    assert str(raw) not in response.text


# --- series ------------------------------------------------------------------


def test_ocean_series_point_and_region(client):
    point = client.get("/series/ocean").json()
    region = client.get("/series/ocean", params={"mode": "region"}).json()
    assert len(point["samples"]) == 12
    assert point["summary"]["usable"] == 12
    assert point["metadata"]["selection"]["mode"] == "point"
    assert region["metadata"]["selection"]["mode"] == "region"


def test_covid_series_uses_the_retrieval_record_and_filters(client):
    body = client.get("/series/covid", params={"location": "Italy", "start": "2020-01-19"}).json()
    assert body["metadata"]["selection"]["location"] == "Italy"
    assert body["metadata"]["source_sha256"] == "ab" * 32
    assert body["metadata"]["source_url"] == "https://example.org/full_data.csv"
    assert body["samples"][0]["week_ending"] == "2020-01-19"
    assert len(body["samples"]) == len(WEEKLY_DEATHS) - 1  # weeks end 2020-01-12 .. 2020-03-29


def test_unknown_location_is_422(client):
    response = client.get("/series/covid", params={"location": "Atlantis"})
    assert response.status_code == 422
    assert response.json()["error"] == "unprocessable_data"
    assert "Atlantis" in response.json()["detail"]


def test_too_few_usable_weeks_is_422(client):
    response = client.get("/series/covid", params={"start": "2020-03-01"})
    assert response.status_code == 422
    assert "Insufficient data" in response.json()["detail"]


def test_invalid_metric_and_mode_are_rejected(client):
    assert client.get("/series/covid", params={"metric": "hospitalizations"}).status_code == 422
    assert client.get("/series/ocean", params={"mode": "global"}).status_code == 422


# --- soundscapes -------------------------------------------------------------


@pytest.mark.parametrize("domain", ["ocean", "covid"])
def test_audio_matches_its_provenance(client, domain):
    provenance = client.get(f"/soundscapes/{domain}").json()
    audio = client.get(f"/soundscapes/{domain}/audio")
    assert audio.status_code == 200
    assert audio.headers["content-type"] == "audio/wav"
    sha = hashlib.sha256(audio.content).hexdigest()
    assert sha == provenance["output"]["sha256"]
    assert audio.headers["etag"] == f'"{sha}"'
    assert wav_info(audio.content) == (8000, 2, 16000)
    assert provenance["settings"]["duration_seconds"] == 2.0


def test_sirius_audio_matches_its_provenance(client):
    provenance = client.get("/soundscapes/sirius").json()
    audio = client.get("/soundscapes/sirius/audio")
    assert hashlib.sha256(audio.content).hexdigest() == provenance["audio"]["sha256"]
    assert audio.headers["content-disposition"] == 'inline; filename="sirius-visible-spectrum.wav"'
    assert len(provenance["bins"]) == 400


def test_rendering_is_deterministic_across_cache_clears(client):
    first = client.get("/soundscapes/covid/audio").content
    render_cache.clear()
    assert client.get("/soundscapes/covid/audio").content == first


def test_parameters_are_applied_and_recorded(client):
    params = {"duration_seconds": 3, "pan_enabled": "false", "timing_mode": "uniform", "rate_sensitivity": 2}
    provenance = client.get("/soundscapes/covid", params={**params, "metric": "deaths"}).json()
    assert provenance["settings"]["duration_seconds"] == 3.0
    assert provenance["settings"]["pan_enabled"] is False
    assert provenance["settings"]["timing_mode"] == "uniform"
    assert provenance["settings"]["rate_sensitivity"] == 2.0
    assert all(s["pan"] in (None, 0.0) for s in provenance["segments"])
    audio = client.get("/soundscapes/covid/audio", params=params)
    assert wav_info(audio.content)[2] == 24000
    assert audio.headers["etag"] == f'"{provenance["output"]["sha256"]}"'


def test_different_parameters_give_different_audio(client):
    default = client.get("/soundscapes/covid/audio").headers["etag"]
    centred = client.get("/soundscapes/covid/audio", params={"pan_enabled": "false"}).headers["etag"]
    italy = client.get("/soundscapes/covid/audio", params={"location": "Italy"})
    assert default != centred
    assert italy.headers["content-disposition"] == 'inline; filename="covid.italy.deaths.wav"'


@pytest.mark.parametrize("duration", ["0", "-1", "121", "nan"])
def test_out_of_range_duration_is_rejected(client, duration):
    assert client.get("/soundscapes/ocean", params={"duration_seconds": duration}).status_code == 422


def test_settings_cap_on_duration(data_dirs):
    raw, metadata, _ = data_dirs
    app.dependency_overrides[get_settings] = lambda: make_settings(raw, metadata, api_max_duration_seconds=5)
    try:
        with TestClient(app) as test_client:
            assert test_client.get("/soundscapes/covid", params={"duration_seconds": 6}).status_code == 422
            assert test_client.get("/soundscapes/covid", params={"duration_seconds": 5}).status_code == 200
    finally:
        app.dependency_overrides.clear()
        render_cache.clear()


def test_replacing_a_sample_invalidates_the_cache(client, data_dirs):
    raw, _, _ = data_dirs
    before = client.get("/soundscapes/covid/audio").headers["etag"]
    louder = [str(int(v) * 10) for v in WEEKLY_DEATHS]
    write_csv(raw / "covid", weekly_rows("World", louder), name="covid.csv")
    assert client.get("/soundscapes/covid/audio").headers["etag"] != before


# --- HTTP caching and ranges -------------------------------------------------


def test_if_none_match_returns_304(client):
    etag = client.get("/soundscapes/sirius/audio").headers["etag"]
    response = client.get("/soundscapes/sirius/audio", headers={"If-None-Match": etag})
    assert response.status_code == 304
    assert response.content == b""
    assert client.get("/soundscapes/sirius/audio", headers={"If-None-Match": '"stale"'}).status_code == 200


@pytest.mark.parametrize(
    ("header", "first", "last"),
    [("bytes=0-43", 0, 43), ("bytes=100-", 100, None), ("bytes=-10", -10, None), ("bytes=5-999999999", 5, None)],
)
def test_byte_ranges(client, header, first, last):
    full = client.get("/soundscapes/ocean/audio").content
    response = client.get("/soundscapes/ocean/audio", headers={"Range": header})
    assert response.status_code == 206
    expected = full[first:] if last is None else full[first : last + 1]
    assert response.content == expected
    start = first if first >= 0 else len(full) + first
    assert response.headers["content-range"] == f"bytes {start}-{start + len(expected) - 1}/{len(full)}"


def test_unsatisfiable_and_ignored_ranges(client):
    size = len(client.get("/soundscapes/ocean/audio").content)
    beyond = client.get("/soundscapes/ocean/audio", headers={"Range": f"bytes={size}-"})
    assert beyond.status_code == 416
    assert beyond.headers["content-range"] == f"bytes */{size}"
    multi = client.get("/soundscapes/ocean/audio", headers={"Range": "bytes=0-1,4-5"})
    assert multi.status_code == 200 and len(multi.content) == size


def test_if_range_with_a_stale_etag_sends_the_whole_file(client):
    size = len(client.get("/soundscapes/ocean/audio").content)
    response = client.get("/soundscapes/ocean/audio", headers={"Range": "bytes=0-9", "If-Range": '"stale"'})
    assert response.status_code == 200 and len(response.content) == size


# --- reference renders -------------------------------------------------------

REFERENCES = [
    ("ocean", {}, "oscar-final-20200101-20200130.point.soundscape.json"),
    ("ocean", {"mode": "region"}, "oscar-final-20200101-20200130.region.soundscape.json"),
    ("covid", {}, "owid-covid-weekly.world.deaths.soundscape.json"),
    ("sirius", {}, "sirius-visible-spectrum.soundscape.json"),
]


@pytest.mark.parametrize(("domain", "params", "reference"), REFERENCES)
def test_default_render_equals_the_committed_reference(domain, params, reference):
    """With the real samples present, the API's default render is the committed reference render."""
    settings = Settings(_env_file=None)
    sample = settings.raw_data_dir / getattr(settings, f"{'sirius_spectrum' if domain == 'sirius' else domain + '_sample'}_file")
    if not sample.is_file():
        pytest.skip(f"real {domain} sample not fetched")
    committed = json.loads((REPOSITORY_ROOT / "data" / "metadata" / reference).read_text(encoding="utf-8"))
    render_cache.clear()
    app.dependency_overrides[get_settings] = lambda: settings
    try:
        with TestClient(app) as test_client:
            assert test_client.get(f"/soundscapes/{domain}", params=params).json() == committed
    finally:
        app.dependency_overrides.clear()
        render_cache.clear()
