"""Application settings loaded from environment variables and `.env`."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """Runtime settings for the API process."""

    app_name: str = "Earth Jukebox API"
    environment: Literal["development", "test", "production"] = "development"

    # Phase 5 API. Samples are read from local files produced by the fetch scripts;
    # the API never downloads data itself. File names are relative to raw_data_dir.
    raw_data_dir: Path = REPOSITORY_ROOT / "data" / "raw"
    metadata_dir: Path = REPOSITORY_ROOT / "data" / "metadata"
    ocean_sample_file: str = "oscar-final-20200101-20200130/oscar-final-20200101-20200130.nc"
    covid_sample_file: str = "owid-covid-weekly/owid-covid-weekly.csv"
    sirius_spectrum_file: str = "sirius_stis_005.fits"
    api_max_duration_seconds: float = Field(default=120.0, gt=0)
    api_render_cache_size: int = Field(default=32, ge=0)
    cors_origins: list[str] = Field(default_factory=list)

    # Phase 3 processing defaults. They match the Phase 2 reproducible sample
    # (docs/ocean-dataset-decision.md): OSCAR v2.0 Final, 2020-01-01..2020-01-30,
    # bbox west -152, south 0, east -150, north 2. The default point is the bbox centre.
    dataset_short_name: str = "OSCAR_L4_OC_FINAL_V2.0"
    dataset_version: str = "2.0"
    u_variable: str = "u"
    v_variable: str = "v"
    processing_latitude: float = 1.0
    processing_longitude: float = -151.0
    processing_south: float = 0.0
    processing_north: float = 2.0
    processing_west: float = -152.0
    processing_east: float = -150.0
    processing_start: str = "2020-01-01"
    processing_end: str = "2020-01-30"
    minimum_usable_samples: int = Field(default=10, ge=1)

    # Phase 4 sonification defaults. The remaining mapping constants live in
    # app/schemas/soundscape.py (SonificationSettings) and docs/sonification-mapping.md.
    soundscape_duration_seconds: float = Field(default=30.0, gt=0)
    soundscape_sample_rate_hz: int = Field(default=44100, ge=8000)

    # Phase 4b COVID-19 series defaults. "World" is the default location because
    # it is the Earth-scale series and avoids singling out one country; deaths is
    # the default metric because reported cases track testing capacity far more
    # strongly. See docs/covid-dataset-decision.md.
    covid_location: str = "World"
    covid_metric: Literal["deaths", "cases"] = "deaths"
    covid_minimum_usable_samples: int = Field(default=10, ge=1)

    model_config = SettingsConfigDict(
        env_prefix="EARTH_JUKEBOX_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    """Return the process settings, loaded once and cached."""
    return Settings()
