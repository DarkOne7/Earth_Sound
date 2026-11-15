"""Processed, validated series: the data behind each soundscape, before any audio."""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Query

from app.api.parameters import covid_location, covid_metric
from app.config import Settings, get_settings
from app.schemas.case_series import CaseMetric, ProcessedCaseSeries
from app.schemas.current_series import ProcessedCurrentSeries
from app.services.catalog import OceanMode, covid_series, ocean_series

router = APIRouter(prefix="/series", tags=["series"])


@router.get("/ocean", response_model=ProcessedCurrentSeries)
def get_ocean_series(
    mode: OceanMode = Query("point", description="Nearest grid point, or the mean over the configured bbox."),
    settings: Settings = Depends(get_settings),
) -> ProcessedCurrentSeries:
    """Daily current speed and direction for the configured OSCAR sample."""
    return ocean_series(settings, mode)


@router.get("/covid", response_model=ProcessedCaseSeries)
def get_covid_series(
    location: str = Depends(covid_location),
    metric: CaseMetric = Depends(covid_metric),
    start: date | None = Query(None, description="Earliest week-ending date to include."),
    end: date | None = Query(None, description="Latest week-ending date to include."),
    settings: Settings = Depends(get_settings),
) -> ProcessedCaseSeries:
    """Weekly reported COVID-19 counts for one location."""
    return covid_series(settings, location, metric, start, end)
