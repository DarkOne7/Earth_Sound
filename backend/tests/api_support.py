"""Synthetic samples and settings shared by the API test modules (fixtures live in conftest.py)."""

from __future__ import annotations

import io
import wave
from pathlib import Path

from app.config import Settings

WEEKLY_DEATHS = ["10", "20", "40", "80", "160", "320", "640", "1280", "640", "320", "160", "80"]


def make_settings(raw: Path, metadata: Path, **overrides) -> Settings:
    values = {
        "raw_data_dir": raw,
        "metadata_dir": metadata,
        "ocean_sample_file": "ocean/sample.nc",
        "covid_sample_file": "covid/covid.csv",
        "sirius_spectrum_file": "sirius_stis_005.fits",
        # The synthetic ocean grid: lat 0.125..0.625, lon -152..-151.5, 12 days.
        "processing_latitude": 0.375,
        "processing_longitude": -151.75,
        "processing_south": 0.0,
        "processing_north": 1.0,
        "processing_west": -152.1,
        "processing_east": -151.4,
        "soundscape_duration_seconds": 2.0,
        "soundscape_sample_rate_hz": 8000,
        **overrides,
    }
    return Settings(_env_file=None, **values)


def wav_info(content: bytes) -> tuple[int, int, int]:
    with wave.open(io.BytesIO(content)) as reader:
        return reader.getframerate(), reader.getnchannels(), reader.getnframes()
