"""Query parameters shared by several routes."""

from __future__ import annotations

from typing import Literal

from fastapi import Depends, HTTPException, Query
from pydantic import ValidationError

from app.config import Settings, get_settings
from app.schemas.case_series import CaseMetric
from app.schemas.soundscape import SonificationSettings


def covid_location(
    location: str | None = Query(
        None, min_length=1, max_length=100, description="OWID location name. Default: settings (World)."
    ),
    settings: Settings = Depends(get_settings),
) -> str:
    return location or settings.covid_location


def covid_metric(
    metric: CaseMetric | None = Query(None, description="Default: settings (deaths)."),
    settings: Settings = Depends(get_settings),
) -> CaseMetric:
    return metric or CaseMetric(settings.covid_metric)


def sonification_settings(
    duration_seconds: float | None = Query(
        None, gt=0, description="Output length in seconds. Default: settings (30); capped by the server."
    ),
    pan_enabled: bool = Query(True, description="False centres every pulse."),
    timing_mode: Literal["uniform", "rate_weighted"] | None = Query(
        None,
        description="'uniform' keeps audio time proportional to data time. Default: rate_weighted.",
    ),
    rate_sensitivity: float | None = Query(None, gt=0, description="Strength of the rate-driven rhythm."),
    settings: Settings = Depends(get_settings),
) -> SonificationSettings:
    """The engine's documented defaults, with only the parameters the client set replaced."""
    if duration_seconds is not None and duration_seconds > settings.api_max_duration_seconds:
        raise HTTPException(
            status_code=422,
            detail=f"duration_seconds must be at most {settings.api_max_duration_seconds:g}",
        )
    # Same defaults as default_sonification_settings(), but from the injected settings.
    updates: dict[str, object] = {
        "duration_seconds": settings.soundscape_duration_seconds,
        "sample_rate_hz": settings.soundscape_sample_rate_hz,
        "pan_enabled": pan_enabled,
    }
    if duration_seconds is not None:
        updates["duration_seconds"] = duration_seconds
    if timing_mode is not None:
        updates["timing_mode"] = timing_mode
    if rate_sensitivity is not None:
        updates["rate_sensitivity"] = rate_sensitivity
    # Re-validate rather than model_copy(update=...), which would skip the validators.
    try:
        return SonificationSettings(**{**SonificationSettings().model_dump(), **updates})
    except ValidationError as error:
        raise HTTPException(status_code=422, detail=error.errors(include_url=False)) from error
