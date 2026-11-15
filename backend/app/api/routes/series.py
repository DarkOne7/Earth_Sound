"""Processed, validated series: the data behind each soundscape, before any audio."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.parameters import (
    EndQuery,
    LocationDep,
    MetricDep,
    ModeQuery,
    SettingsDep,
    StartQuery,
)
from app.schemas.case_series import ProcessedCaseSeries
from app.schemas.current_series import ProcessedCurrentSeries
from app.services.catalog import covid_series, ocean_series

router = APIRouter(prefix="/series", tags=["series"])


@router.get("/ocean", response_model=ProcessedCurrentSeries)
def get_ocean_series(settings: SettingsDep, mode: ModeQuery = "point") -> ProcessedCurrentSeries:
    """Daily current speed and direction for the configured OSCAR sample."""
    return ocean_series(settings, mode)


@router.get("/covid", response_model=ProcessedCaseSeries)
def get_covid_series(
    settings: SettingsDep,
    location: LocationDep,
    metric: MetricDep,
    start: StartQuery = None,
    end: EndQuery = None,
) -> ProcessedCaseSeries:
    """Weekly reported COVID-19 counts for one location."""
    return covid_series(settings, location, metric, start, end)
