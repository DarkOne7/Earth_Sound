"""Loading, validation and normalization of COVID-19 weekly case/death series.

Conventions (see ``docs/covid-dataset-decision.md``):

* **The source is weekly, not daily.** Our World In Data's ``new_cases`` and
  ``new_deaths`` columns post a whole week's total on one weekday and zero on the
  other six. The cadence is *detected* from the rows, never assumed, and the
  series is aggregated to calendar weeks ending Sunday so that non-reporting days
  are never treated as measurements of zero.
* **Loudness is logarithmic.** Weekly counts span five orders of magnitude, so
  ``level_normalized = log10(count) / log10(maximum)`` against a fixed, cited
  reference maximum. The reference is never derived from the sample.
* **Stereo position carries the rate of change**, so a rising wave is heard
  moving one way and a receding wave the other.
* Weeks whose total cannot be trusted are flagged, never filled.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from app.schemas.case_series import (
    CaseMetric,
    CaseSample,
    CaseSampleFlag,
    CaseSampleStatus,
    CaseSelection,
    CaseSeriesMetadata,
    CaseSeriesSummary,
    ProcessedCaseSeries,
)
from app.schemas.references import NormalizationReference
from app.services.reference_ranges import (
    CASE_PROCESSING_VERSION,
    COVID_WEEKLY_CASES_RANGE,
    COVID_WEEKLY_DEATHS_RANGE,
    PAN_LOG2_FULL_SCALE,
    ReferenceRange,
)

#: Source column holding the reported total for each metric.
METRIC_COLUMNS: dict[CaseMetric, str] = {
    CaseMetric.DEATHS: "new_deaths",
    CaseMetric.CASES: "new_cases",
}

#: Reference range used to normalize each metric.
METRIC_REFERENCES: dict[CaseMetric, ReferenceRange] = {
    CaseMetric.DEATHS: COVID_WEEKLY_DEATHS_RANGE,
    CaseMetric.CASES: COVID_WEEKLY_CASES_RANGE,
}

#: Columns the fetched subset must contain.
REQUIRED_COLUMNS = ("date", "location", "new_cases", "new_deaths")

LEVEL_RULE = (
    "level_normalized = (log10(clamp(count, minimum, maximum)) - log10(minimum)) / "
    "(log10(maximum) - log10(minimum)); with minimum = 1 this is log10(count) / log10(maximum). "
    "A count of 0 or 1 gives 0.0 (the gain floor), and counts below the minimum or above the maximum "
    "are clipped and flagged. Logarithmic because weekly counts span several orders of magnitude."
)
GROWTH_RULE = (
    "growth_log2 = log2(count / previous week's count), and pan = clamp(growth_log2 / "
    f"{PAN_LOG2_FULL_SCALE}, -1, 1), so a doubling pans fully right and a halving fully left. "
    "Undefined (null, centred, flagged undefined_growth) for the first week and whenever either "
    "week's count is zero or unusable."
)


class CaseProcessingError(ValueError):
    """Raised when a case series cannot be processed under the documented rules."""


@dataclass(frozen=True)
class ReportingCadence:
    """What cadence the source rows actually use, detected rather than assumed."""

    cadence: str
    weekday: str | None
    nonzero_rows: int
    total_rows: int

    def describe(self) -> str:
        if self.cadence == "weekly":
            return (
                f"Weekly: {self.nonzero_rows} of {self.total_rows} rows are non-zero and every one of them "
                f"falls on a {self.weekday}, so each {self.weekday} row carries that week's total."
            )
        if self.cadence == "daily":
            return f"Daily: {self.nonzero_rows} of {self.total_rows} rows are non-zero, spread across weekdays."
        return f"Indeterminate: {self.nonzero_rows} of {self.total_rows} rows are non-zero."


# --- loading ----------------------------------------------------------------


def load_case_frame(path: str | Path) -> pd.DataFrame:
    """Read a fetched OWID subset CSV and check that it has the expected columns."""
    path = Path(path)
    if not path.is_file():
        raise CaseProcessingError(f"Case data file not found: {path}")
    frame = pd.read_csv(path)
    missing = [column for column in REQUIRED_COLUMNS if column not in frame.columns]
    if missing:
        raise CaseProcessingError(f"{path} is missing required column(s): {', '.join(missing)}")
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    if frame["date"].isna().any():
        raise CaseProcessingError(f"{path} contains unparseable date values")
    return frame


def select_location(frame: pd.DataFrame, location: str) -> pd.DataFrame:
    """Rows for one location, ordered by date, with duplicates rejected."""
    rows = frame[frame["location"] == location].sort_values("date")
    if rows.empty:
        available = sorted(frame["location"].dropna().unique())
        hint = ", ".join(available[:8]) + ("..." if len(available) > 8 else "")
        raise CaseProcessingError(f"Location {location!r} is not in the data. Available: {hint}")
    if rows["date"].duplicated().any():
        duplicate = rows.loc[rows["date"].duplicated(), "date"].iloc[0].date()
        raise CaseProcessingError(f"Duplicate row for {location} on {duplicate}")
    return rows


def detect_reporting_cadence(rows: pd.DataFrame, column: str) -> ReportingCadence:
    """Detect whether the source posts totals weekly (on one weekday) or daily.

    This is the check that stops the pipeline from treating six non-reporting days
    a week as six measurements of zero.
    """
    values = pd.to_numeric(rows[column], errors="coerce")
    nonzero = rows.loc[values.fillna(0) > 0, "date"]
    total = len(rows)
    if nonzero.empty:
        return ReportingCadence("indeterminate", None, 0, total)
    weekdays = set(nonzero.dt.day_name())
    if len(weekdays) == 1 and len(nonzero) <= total * 0.3:
        return ReportingCadence("weekly", weekdays.pop(), len(nonzero), total)
    return ReportingCadence("daily", None, len(nonzero), total)


def week_ending_sunday(day: date) -> date:
    """The Sunday that closes the Monday-to-Sunday week containing ``day``."""
    return day + timedelta(days=(6 - day.weekday()) % 7)


# --- normalization ----------------------------------------------------------


def normalize_log_count(
    count: float,
    reference: ReferenceRange,
) -> tuple[float, CaseSampleFlag | None]:
    """Map a count to [0, 1] on a log10 scale against a cited reference range.

    The bounds map to exactly 0.0 and 1.0. Values outside are clipped and flagged.
    """
    if not math.isfinite(count):
        raise CaseProcessingError(f"Cannot normalize non-finite count {count!r}")
    if count < reference.minimum:
        return 0.0, CaseSampleFlag.CLIPPED_LOW
    if count > reference.maximum:
        return 1.0, CaseSampleFlag.CLIPPED_HIGH
    low, high = math.log10(reference.minimum), math.log10(reference.maximum)
    return (math.log10(count) - low) / (high - low), None


def growth_log2(count: float | None, previous: float | None) -> float | None:
    """``log2(count / previous)``, or ``None`` when the ratio is undefined."""
    if count is None or previous is None:
        return None
    if not (math.isfinite(count) and math.isfinite(previous)) or count <= 0 or previous <= 0:
        return None
    return math.log2(count / previous)


def growth_to_pan(growth: float | None, full_scale: float = PAN_LOG2_FULL_SCALE) -> float | None:
    """Map a week-over-week log2 ratio to a pan position in [-1, 1]."""
    if growth is None:
        return None
    if full_scale <= 0:
        raise CaseProcessingError("pan full scale must be positive")
    return max(-1.0, min(1.0, growth / full_scale))


# --- processing -------------------------------------------------------------


def _aggregate_weeks(rows: pd.DataFrame, column: str) -> list[dict[str, Any]]:
    """Sum the metric over every calendar week between the first and last row.

    Weeks with no source rows at all appear as ``missing`` so the timeline stays
    continuous. A week containing a null value is ``missing`` too, because its
    total cannot be known to be complete. A negative value makes the week
    ``invalid``.
    """
    values = pd.to_numeric(rows[column], errors="coerce")
    frame = pd.DataFrame(
        {
            "week_ending": [week_ending_sunday(d.date()) for d in rows["date"]],
            "value": values.to_numpy(),
        }
    )
    first = week_ending_sunday(rows["date"].iloc[0].date())
    last = week_ending_sunday(rows["date"].iloc[-1].date())
    grouped = {week: group["value"] for week, group in frame.groupby("week_ending")}

    weeks: list[dict[str, Any]] = []
    week = first
    while week <= last:
        series = grouped.get(week)
        if series is None or len(series) == 0:
            weeks.append({"week_ending": week, "count": None, "reported_days": 0, "status": CaseSampleStatus.MISSING})
        elif series.isna().any():
            weeks.append(
                {
                    "week_ending": week,
                    "count": None,
                    "reported_days": int(series.notna().sum()),
                    "status": CaseSampleStatus.MISSING,
                }
            )
        elif (series < 0).any():
            weeks.append(
                {
                    "week_ending": week,
                    "count": None,
                    "reported_days": len(series),
                    "status": CaseSampleStatus.INVALID,
                }
            )
        else:
            weeks.append(
                {
                    "week_ending": week,
                    "count": float(series.sum()),
                    "reported_days": len(series),
                    "status": CaseSampleStatus.VALID,
                }
            )
        week += timedelta(days=7)
    return weeks


def _summarize(samples: Sequence[CaseSample]) -> CaseSeriesSummary:
    def count_status(status: CaseSampleStatus) -> int:
        return sum(1 for sample in samples if sample.status is status)

    def count_flag(flag: CaseSampleFlag) -> int:
        return sum(1 for sample in samples if flag in sample.flags)

    usable = [sample for sample in samples if sample.count is not None]
    peak = max(usable, key=lambda sample: sample.count, default=None)
    return CaseSeriesSummary(
        total=len(samples),
        valid=count_status(CaseSampleStatus.VALID),
        missing=count_status(CaseSampleStatus.MISSING),
        invalid=count_status(CaseSampleStatus.INVALID),
        clipped_low=count_flag(CaseSampleFlag.CLIPPED_LOW),
        clipped_high=count_flag(CaseSampleFlag.CLIPPED_HIGH),
        undefined_growth=count_flag(CaseSampleFlag.UNDEFINED_GROWTH),
        partial_weeks=count_flag(CaseSampleFlag.PARTIAL_WEEK),
        usable=len(usable),
        count_min=min((sample.count for sample in usable), default=None),
        count_max=max((sample.count for sample in usable), default=None),
        peak_week_ending=peak.week_ending if peak is not None else None,
    )


def _reference_model(reference: ReferenceRange) -> NormalizationReference:
    return NormalizationReference(
        quantity=reference.quantity,
        minimum=reference.minimum,
        maximum=reference.maximum,
        units=reference.units,
        source=reference.source,
        url=reference.url,
    )


def process_case_series(
    path: str | Path,
    *,
    location: str,
    metric: CaseMetric = CaseMetric.DEATHS,
    start: date | None = None,
    end: date | None = None,
    minimum_usable_samples: int = 10,
    source_sha256: str | None = None,
    source_url: str | None = None,
    dataset: str = "Our World In Data COVID-19 (WHO-compiled)",
    dataset_version: str | None = None,
    retrieved_at: datetime | None = None,
) -> ProcessedCaseSeries:
    """Load, validate, aggregate and normalize one location's weekly series.

    ``start`` and ``end`` are inclusive bounds on the week-ending dates.
    """
    frame = load_case_frame(path)
    rows = select_location(frame, location)
    column = METRIC_COLUMNS[metric]
    if column not in rows.columns:
        raise CaseProcessingError(f"Metric column {column!r} is not in the data")
    cadence = detect_reporting_cadence(rows, column)
    reference = METRIC_REFERENCES[metric]

    weeks = _aggregate_weeks(rows, column)
    if start is not None:
        weeks = [week for week in weeks if week["week_ending"] >= start]
    if end is not None:
        weeks = [week for week in weeks if week["week_ending"] <= end]
    if not weeks:
        raise CaseProcessingError("The requested window contains no weeks")

    samples: list[CaseSample] = []
    previous_count: float | None = None
    for week in weeks:
        status: CaseSampleStatus = week["status"]
        count: float | None = week["count"]
        flags: list[CaseSampleFlag] = []
        level = pan = growth = None
        if status is CaseSampleStatus.VALID:
            assert count is not None
            level, clip_flag = normalize_log_count(count, reference)
            if clip_flag is not None:
                flags.append(clip_flag)
            growth = growth_log2(count, previous_count)
            pan = growth_to_pan(growth)
            if growth is None:
                flags.append(CaseSampleFlag.UNDEFINED_GROWTH)
            if week["reported_days"] < 7:
                flags.append(CaseSampleFlag.PARTIAL_WEEK)
        samples.append(
            CaseSample(
                week_ending=week["week_ending"],
                count=count,
                reported_days=week["reported_days"],
                level_normalized=level,
                growth_log2=growth,
                pan=pan,
                status=status,
                flags=flags,
            )
        )
        previous_count = count

    summary = _summarize(samples)
    if summary.usable < minimum_usable_samples:
        raise CaseProcessingError(
            f"Insufficient data: {summary.usable} usable week(s) out of {summary.total}; "
            f"at least {minimum_usable_samples} required"
        )
    return ProcessedCaseSeries(
        metadata=CaseSeriesMetadata(
            dataset=dataset,
            dataset_version=dataset_version,
            source_file=Path(path).name,
            source_sha256=source_sha256,
            source_url=source_url,
            retrieved_at=retrieved_at,
            selection=CaseSelection(
                location=location,
                metric=metric,
                start=samples[0].week_ending,
                end=samples[-1].week_ending,
                reporting_cadence=cadence.describe(),
                reporting_weekday=cadence.weekday,
            ),
            units=reference.units,
            level_normalization=_reference_model(reference),
            level_rule=LEVEL_RULE,
            growth_rule=GROWTH_RULE,
            pan_log2_full_scale=PAN_LOG2_FULL_SCALE,
            processing_version=CASE_PROCESSING_VERSION,
        ),
        samples=samples,
        summary=summary,
    )
