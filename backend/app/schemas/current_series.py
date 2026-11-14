"""Typed, JSON-serializable representations of extracted and processed series."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Literal

import numpy as np
from pydantic import BaseModel, ConfigDict, Field


class SampleStatus(str, Enum):
    """Quality status assigned to every processed sample."""

    VALID = "valid"
    MISSING = "missing"
    INVALID = "invalid"
    UNDEFINED_DIRECTION = "undefined_direction"


class SampleFlag(str, Enum):
    """Additional per-sample flags that do not change the status."""

    CLIPPED_LOW = "clipped_low"
    CLIPPED_HIGH = "clipped_high"


class SelectionParameters(BaseModel):
    """Parameters used to extract a series, recorded for reproducibility."""

    model_config = ConfigDict(frozen=True)

    mode: Literal["point", "region"]
    start: datetime
    end: datetime
    latitude: float | None = None
    longitude: float | None = None
    south: float | None = None
    north: float | None = None
    west: float | None = None
    east: float | None = None
    selected_latitude: float | None = Field(
        default=None, description="Grid latitude actually used (point mode)."
    )
    selected_longitude: float | None = Field(
        default=None, description="Grid longitude actually used, in the file's convention."
    )
    region_cell_count: int | None = Field(
        default=None, description="Grid cells inside the region bounds (region mode)."
    )


@dataclass(frozen=True, eq=False)
class ExtractedSeries:
    """Raw, ordered u/v time series taken from a dataset, before validation.

    ``forced_status`` holds a status decided during extraction (region
    aggregation) or ``None`` when processing should decide.

    ``eq=False``: numpy array fields make generated ``__eq__``/``__hash__``
    unusable, so instances compare and hash by identity.
    """

    timestamps: tuple[datetime, ...]
    u: np.ndarray
    v: np.ndarray
    u_units: str
    v_units: str
    u_variable: str
    v_variable: str
    selection: SelectionParameters
    source: dict[str, Any] = field(default_factory=dict)
    forced_status: tuple[SampleStatus | None, ...] | None = None
    cell_counts: tuple[int, ...] | None = None


class NormalizationReference(BaseModel):
    model_config = ConfigDict(frozen=True)

    quantity: str
    minimum: float
    maximum: float
    units: str
    source: str
    url: str


class SeriesMetadata(BaseModel):
    dataset: str | None
    dataset_version: str | None
    source_file: str | None
    source_sha256: str | None
    variables: dict[str, str]
    input_units: dict[str, str]
    units: str
    selection: SelectionParameters
    direction_convention: str
    direction_normalization: str
    speed_normalization: NormalizationReference
    component_valid_range: NormalizationReference
    numeric_tolerance: float
    minimum_usable_samples: int
    processing_version: str


class CurrentSample(BaseModel):
    timestamp: datetime
    u: float | None = Field(description="Eastward component in m s-1, null if not usable.")
    v: float | None = Field(description="Northward component in m s-1, null if not usable.")
    speed: float | None
    direction_deg: float | None
    speed_normalized: float | None
    direction_normalized: float | None
    status: SampleStatus
    flags: list[SampleFlag] = Field(default_factory=list)
    cell_count: int | None = None


class SeriesSummary(BaseModel):
    total: int
    valid: int
    undefined_direction: int
    missing: int
    invalid: int
    clipped_low: int
    clipped_high: int
    usable: int = Field(description="Samples with a usable speed: valid + undefined_direction.")


class ProcessedCurrentSeries(BaseModel):
    metadata: SeriesMetadata
    samples: list[CurrentSample]
    summary: SeriesSummary
