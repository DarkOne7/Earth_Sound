"""Typed representations of a processed COVID-19 weekly case/death series."""

from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.references import NormalizationReference


class CaseMetric(str, Enum):
    """Which reported quantity drives the soundscape."""

    DEATHS = "deaths"
    CASES = "cases"


class CaseSampleStatus(str, Enum):
    """Quality status assigned to every aggregated week."""

    VALID = "valid"
    MISSING = "missing"
    INVALID = "invalid"


class CaseSampleFlag(str, Enum):
    """Additional per-week flags that do not change the status."""

    CLIPPED_LOW = "clipped_low"
    CLIPPED_HIGH = "clipped_high"
    UNDEFINED_GROWTH = "undefined_growth"
    PARTIAL_WEEK = "partial_week"


class CaseSelection(BaseModel):
    """Parameters used to select a series, recorded for reproducibility."""

    model_config = ConfigDict(frozen=True)

    location: str
    metric: CaseMetric
    start: date
    end: date
    aggregation: Literal["calendar_week_ending_sunday"] = "calendar_week_ending_sunday"
    reporting_cadence: str = Field(description="The cadence detected in the source rows, not assumed.")
    reporting_weekday: str | None = Field(
        default=None, description="Weekday that carries the totals when the source reports weekly."
    )


class CaseSample(BaseModel):
    """One aggregated reporting week."""

    model_config = ConfigDict(frozen=True)

    week_ending: date
    count: float | None = Field(description="Reported total for the week, null when not usable.")
    reported_days: int = Field(description="Source rows present in the week; 7 for a complete week.")
    level_normalized: float | None = Field(
        default=None, description="log10 of the count against the reference range, in [0, 1]."
    )
    growth_log2: float | None = Field(
        default=None, description="log2(count / previous week's count); null when undefined."
    )
    pan: float | None = Field(default=None, description="Growth as a stereo position in [-1, 1]; null = centre.")
    status: CaseSampleStatus
    flags: list[CaseSampleFlag] = Field(default_factory=list)


class CaseSeriesSummary(BaseModel):
    model_config = ConfigDict(frozen=True)

    total: int
    valid: int
    missing: int
    invalid: int
    clipped_low: int
    clipped_high: int
    undefined_growth: int
    partial_weeks: int
    usable: int = Field(description="Weeks carrying a usable count.")
    count_min: float | None
    count_max: float | None
    peak_week_ending: date | None


class CaseSeriesMetadata(BaseModel):
    model_config = ConfigDict(frozen=True)

    dataset: str
    dataset_version: str | None
    source_file: str | None
    source_sha256: str | None
    source_url: str | None
    retrieved_at: datetime | None
    selection: CaseSelection
    units: str
    level_normalization: NormalizationReference
    level_rule: str
    growth_rule: str
    pan_log2_full_scale: float
    processing_version: str


class ProcessedCaseSeries(BaseModel):
    model_config = ConfigDict(frozen=True)

    metadata: CaseSeriesMetadata
    samples: list[CaseSample]
    summary: CaseSeriesSummary
