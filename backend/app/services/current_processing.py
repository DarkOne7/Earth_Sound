"""Speed/direction calculation, validation and normalization of ocean currents.

Conventions (see ``docs/processing-methodology.md``):

* Units: every output velocity and speed is in m s-1.
* Speed: ``sqrt(u**2 + v**2)``.
* Direction: oceanographic convention, the direction the current flows
  **toward**, in degrees clockwise from true north, in ``[0, 360)``.
* Missing/invalid data are flagged, never replaced by measurements.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

import numpy as np

from app.config import get_settings
from app.schemas.current_series import (
    CurrentSample,
    ExtractedSeries,
    NormalizationReference,
    ProcessedCurrentSeries,
    SampleFlag,
    SampleStatus,
    SeriesMetadata,
    SeriesSummary,
)
from app.services.reference_ranges import (
    CANONICAL_VELOCITY_UNITS,
    COMPONENT_VALID_RANGE,
    DOCUMENTED_COMPONENT_FILL_VALUE,
    NUMERIC_TOLERANCE,
    PROCESSING_VERSION,
    SPEED_NORMALIZATION_RANGE,
    VELOCITY_UNIT_FACTORS,
    ReferenceRange,
)

DIRECTION_CONVENTION = (
    "Direction the current flows toward, in degrees clockwise from true north, "
    "range [0, 360); 0 = northward, 90 = eastward, 180 = southward, 270 = westward. "
    "Computed as atan2(u, v) mod 360. Undefined (null) when speed <= numeric tolerance."
)
DIRECTION_NORMALIZATION = (
    "direction_deg / 360, range [0, 1); 360 wraps to 0. Null when direction is undefined."
)


class CurrentProcessingError(ValueError):
    """Raised when inputs cannot be processed under the documented rules."""


class UnitConversionError(CurrentProcessingError):
    """Raised for velocity units that have no documented conversion."""


class InsufficientDataError(CurrentProcessingError):
    """Raised when too few usable samples remain for sonification.

    The fully flagged series is attached as ``series`` for diagnostics.
    """

    def __init__(self, message: str, series: ProcessedCurrentSeries) -> None:
        super().__init__(message)
        self.series = series

    def __reduce__(self):  # keep picklable despite the two-argument constructor
        return (type(self), (str(self), self.series))


def velocity_unit_factor(units: str | None) -> float:
    """Return the factor converting ``units`` to m s-1, or raise if undocumented."""
    if units is None:
        raise UnitConversionError("Velocity variable has no units attribute; refusing to assume one.")
    normalized = " ".join(str(units).strip().lower().split())
    try:
        return VELOCITY_UNIT_FACTORS[normalized]
    except KeyError:
        raise UnitConversionError(
            f"Unsupported velocity units {units!r}. Supported: {', '.join(sorted(VELOCITY_UNIT_FACTORS))}"
        ) from None


def to_meters_per_second(values: np.ndarray, units: str | None) -> np.ndarray:
    """Convert velocity values to m s-1 through a documented factor."""
    factor = velocity_unit_factor(units)
    array = np.asarray(values, dtype=np.float64)
    return array if factor == 1.0 else array * factor


def current_speed(u: np.ndarray | float, v: np.ndarray | float) -> np.ndarray:
    """Speed magnitude ``sqrt(u**2 + v**2)`` (same units as the inputs)."""
    return np.hypot(np.asarray(u, dtype=np.float64), np.asarray(v, dtype=np.float64))


def current_direction(
    u: np.ndarray | float,
    v: np.ndarray | float,
    *,
    tolerance: float = NUMERIC_TOLERANCE,
) -> np.ndarray:
    """Direction the current flows toward, degrees clockwise from north, in [0, 360).

    Returns NaN where speed <= ``tolerance`` (direction undefined).
    """
    u_arr = np.asarray(u, dtype=np.float64)
    v_arr = np.asarray(v, dtype=np.float64)
    with np.errstate(invalid="ignore"):
        degrees = np.mod(np.degrees(np.arctan2(u_arr, v_arr)), 360.0)
        # np.mod can return exactly 360.0 for tiny negative angles; wrap it to 0.
        degrees = np.where(degrees >= 360.0, 0.0, degrees)
        undefined = ~(current_speed(u_arr, v_arr) > tolerance)
    return np.where(undefined, np.nan, degrees)


def normalize_speed(
    speed: float,
    reference: ReferenceRange = SPEED_NORMALIZATION_RANGE,
) -> tuple[float, SampleFlag | None]:
    """Map a finite speed to [0, 1] with the documented reference range.

    The bounds map to exactly 0.0 and 1.0. Values outside are clipped and flagged.
    """
    if not math.isfinite(speed):
        raise CurrentProcessingError(f"Cannot normalize non-finite speed {speed!r}")
    if speed < reference.minimum:
        return 0.0, SampleFlag.CLIPPED_LOW
    if speed > reference.maximum:
        return 1.0, SampleFlag.CLIPPED_HIGH
    # (max - min) / (max - min) is exactly 1.0 in IEEE arithmetic, so the upper
    # bound maps to 1.0 without a special case. ReferenceRange guarantees min < max.
    return (speed - reference.minimum) / (reference.maximum - reference.minimum), None


def normalize_direction(direction_deg: float) -> float:
    """Map degrees to [0, 1); any angle is first wrapped into [0, 360)."""
    if not math.isfinite(direction_deg):
        raise CurrentProcessingError(f"Cannot normalize non-finite direction {direction_deg!r}")
    wrapped = direction_deg % 360.0
    if wrapped >= 360.0:  # float rounding of tiny negative angles
        wrapped = 0.0
    value = wrapped / 360.0
    return 0.0 if value >= 1.0 else value


def component_valid_mask(u: np.ndarray, v: np.ndarray) -> np.ndarray:
    """Positions where both converted (m s-1) components are finite and in range.

    This is the single implementation of the documented valid-range rule.
    """
    u_arr = np.asarray(u, dtype=np.float64)
    v_arr = np.asarray(v, dtype=np.float64)
    low, high = COMPONENT_VALID_RANGE.minimum, COMPONENT_VALID_RANGE.maximum
    with np.errstate(invalid="ignore"):
        return (
            np.isfinite(u_arr)
            & np.isfinite(v_arr)
            & (u_arr >= low)
            & (u_arr <= high)
            & (v_arr >= low)
            & (v_arr <= high)
        )


def component_status_masks(
    raw_u: np.ndarray,
    raw_v: np.ndarray,
    u_units: str | None,
    v_units: str | None,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Apply the documented validity rules to raw u/v arrays of any shape.

    Returns ``(u_ms, v_ms, missing, invalid)``:

    * ``missing``: either component is NaN (a masked fill, land, ice or grid gap)
      or its **raw** value equals the documented fill sentinel. Missing wins over
      invalid, so a sample is never half-valid.
    * ``invalid``: not missing, and either component is infinite or outside the
      documented valid range after conversion to m s-1.
    """
    raw_u = np.asarray(raw_u, dtype=np.float64)
    raw_v = np.asarray(raw_v, dtype=np.float64)
    u_ms = to_meters_per_second(raw_u, u_units)
    v_ms = to_meters_per_second(raw_v, v_units)
    missing = (
        np.isnan(raw_u)
        | np.isnan(raw_v)
        | (raw_u == DOCUMENTED_COMPONENT_FILL_VALUE)
        | (raw_v == DOCUMENTED_COMPONENT_FILL_VALUE)
    )
    invalid = ~missing & ~component_valid_mask(u_ms, v_ms)
    return u_ms, v_ms, missing, invalid


def _reference_model(reference: ReferenceRange) -> NormalizationReference:
    return NormalizationReference(
        quantity=reference.quantity,
        minimum=reference.minimum,
        maximum=reference.maximum,
        units=reference.units,
        source=reference.source,
        url=reference.url,
    )


def _summarize(samples: Sequence[CurrentSample]) -> SeriesSummary:
    def count(status: SampleStatus) -> int:
        return sum(1 for sample in samples if sample.status is status)

    valid = count(SampleStatus.VALID)
    undefined = count(SampleStatus.UNDEFINED_DIRECTION)
    return SeriesSummary(
        total=len(samples),
        valid=valid,
        undefined_direction=undefined,
        missing=count(SampleStatus.MISSING),
        invalid=count(SampleStatus.INVALID),
        clipped_low=sum(1 for s in samples if SampleFlag.CLIPPED_LOW in s.flags),
        clipped_high=sum(1 for s in samples if SampleFlag.CLIPPED_HIGH in s.flags),
        usable=valid + undefined,
    )


def validate_timestamps(timestamps: Sequence) -> None:
    """Raise if timestamps are not strictly increasing (duplicates or out of order).

    Timestamps must be all timezone-aware or all naive; mixing them is rejected.
    """
    awareness = {getattr(t, "tzinfo", None) is not None for t in timestamps}
    if len(awareness) > 1:
        raise CurrentProcessingError("Timestamps mix timezone-aware and naive values")
    for index in range(1, len(timestamps)):
        previous, current = timestamps[index - 1], timestamps[index]
        if current == previous:
            raise CurrentProcessingError(f"Duplicate timestamp at index {index}: {current}")
        if current < previous:
            raise CurrentProcessingError(
                f"Timestamps out of order at index {index}: {current} follows {previous}"
            )


def process_current_series(
    series: ExtractedSeries,
    *,
    minimum_usable_samples: int | None = None,
    speed_reference: ReferenceRange = SPEED_NORMALIZATION_RANGE,
    tolerance: float = NUMERIC_TOLERANCE,
) -> ProcessedCurrentSeries:
    """Validate, derive and normalize an extracted u/v series.

    Raises ``InsufficientDataError`` (with the flagged series attached) when the
    number of usable samples is below ``minimum_usable_samples``. When it is
    ``None`` the configured ``Settings.minimum_usable_samples`` is used
    (``EARTH_JUKEBOX_MINIMUM_USABLE_SAMPLES``).
    """
    if minimum_usable_samples is None:
        minimum_usable_samples = get_settings().minimum_usable_samples
    if minimum_usable_samples < 1:
        raise CurrentProcessingError("minimum_usable_samples must be at least 1")
    count = len(series.timestamps)
    if np.shape(series.u) != (count,) or np.shape(series.v) != (count,):
        raise CurrentProcessingError(
            f"u {np.shape(series.u)} and v {np.shape(series.v)} must both be 1-D with "
            f"{count} values matching the timestamps"
        )
    if series.forced_status is not None and len(series.forced_status) != count:
        raise CurrentProcessingError("forced_status length does not match the timestamps")
    if series.cell_counts is not None and len(series.cell_counts) != count:
        raise CurrentProcessingError("cell_counts length does not match the timestamps")
    validate_timestamps(series.timestamps)

    u_ms, v_ms, missing, invalid = component_status_masks(series.u, series.v, series.u_units, series.v_units)
    speeds = current_speed(u_ms, v_ms)
    directions = current_direction(u_ms, v_ms, tolerance=tolerance)

    samples: list[CurrentSample] = []
    for index, timestamp in enumerate(series.timestamps):
        if missing[index]:
            status = SampleStatus.MISSING
        elif invalid[index]:
            status = SampleStatus.INVALID
        else:
            status = SampleStatus.VALID
        if series.forced_status is not None and series.forced_status[index] is not None:
            status = series.forced_status[index]
        cell_count = series.cell_counts[index] if series.cell_counts is not None else None

        if status is not SampleStatus.VALID:
            samples.append(
                CurrentSample(
                    timestamp=timestamp,
                    u=None,
                    v=None,
                    speed=None,
                    direction_deg=None,
                    speed_normalized=None,
                    direction_normalized=None,
                    status=status,
                    cell_count=cell_count,
                )
            )
            continue

        speed = float(speeds[index])
        speed_normalized, flag = normalize_speed(speed, speed_reference)
        direction = float(directions[index])
        if math.isnan(direction):
            status = SampleStatus.UNDEFINED_DIRECTION
            direction_value = None
            direction_normalized = None
        else:
            direction_value = direction
            direction_normalized = normalize_direction(direction)
        samples.append(
            CurrentSample(
                timestamp=timestamp,
                u=float(u_ms[index]),
                v=float(v_ms[index]),
                speed=speed,
                direction_deg=direction_value,
                speed_normalized=speed_normalized,
                direction_normalized=direction_normalized,
                status=status,
                flags=[flag] if flag is not None else [],
                cell_count=cell_count,
            )
        )

    source = series.source
    processed = ProcessedCurrentSeries(
        metadata=SeriesMetadata(
            dataset=source.get("dataset"),
            dataset_version=source.get("dataset_version"),
            source_file=source.get("source_file"),
            source_sha256=source.get("source_sha256"),
            variables={"u": series.u_variable, "v": series.v_variable},
            input_units={"u": series.u_units, "v": series.v_units},
            units=CANONICAL_VELOCITY_UNITS,
            selection=series.selection,
            direction_convention=DIRECTION_CONVENTION,
            direction_normalization=DIRECTION_NORMALIZATION,
            speed_normalization=_reference_model(speed_reference),
            component_valid_range=_reference_model(COMPONENT_VALID_RANGE),
            numeric_tolerance=tolerance,
            minimum_usable_samples=minimum_usable_samples,
            processing_version=PROCESSING_VERSION,
        ),
        samples=samples,
        summary=_summarize(samples),
    )
    if processed.summary.usable < minimum_usable_samples:
        raise InsufficientDataError(
            f"Insufficient data: {processed.summary.usable} usable sample(s) "
            f"(valid or undefined_direction) out of {processed.summary.total}; "
            f"at least {minimum_usable_samples} required. "
            f"missing={processed.summary.missing}, invalid={processed.summary.invalid}",
            processed,
        )
    return processed
