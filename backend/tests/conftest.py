"""Fixtures shared by the API test modules: a synthetic sample tree and a client bound to it."""

from __future__ import annotations

import json

import pytest
from api_support import WEEKLY_DEATHS, make_settings
from app.config import get_settings
from app.main import app
from app.services.catalog import render_cache
from fastapi.testclient import TestClient
from test_case_processing import weekly_rows, write_csv
from test_series_extraction import write_sample
from test_spectrum_sonification import write_fits


@pytest.fixture
def data_dirs(tmp_path):
    raw, metadata = tmp_path / "raw", tmp_path / "metadata"
    (raw / "ocean").mkdir(parents=True)
    (raw / "covid").mkdir()
    metadata.mkdir()
    write_sample(raw / "ocean" / "sample.nc")
    covid = write_csv(raw / "covid", weekly_rows("World", WEEKLY_DEATHS) + weekly_rows("Italy", WEEKLY_DEATHS),
                      name="covid.csv")
    (metadata / "covid.retrieval.json").write_text(
        json.dumps({"subset_sha256": "ab" * 32, "source_url": "https://example.org/full_data.csv",
                    "retrieved_at_utc": "2026-11-14T10:55:25+00:00"}),
        encoding="utf-8",
    )
    write_fits(raw / "sirius_stis_005.fits")
    return raw, metadata, covid


@pytest.fixture
def client(data_dirs):
    raw, metadata, _ = data_dirs
    settings = make_settings(raw, metadata)
    render_cache.clear()
    app.dependency_overrides[get_settings] = lambda: settings
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
    render_cache.clear()
