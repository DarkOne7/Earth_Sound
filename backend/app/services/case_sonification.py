"""COVID-19 domain mapping: ``ProcessedCaseSeries`` -> ``SonifiableSeries``.

This module holds every COVID-specific sonification decision, so the engine stays
domain-neutral. See ``docs/sonification-mapping.md`` and
``docs/covid-dataset-decision.md``.

* Loudness comes from ``level_normalized``: log10 of the week's reported total
  against a fixed, externally cited reference maximum.
* Stereo position comes from the week-over-week growth ratio, so a rising wave is
  heard moving right and a receding wave moving left.
* Weeks whose total is absent or unusable are silent. A reported zero is audible
  at the gain floor, because zero deaths is a measurement and a missing report
  is not.
"""

from __future__ import annotations

from datetime import UTC, datetime, time

from app.schemas.case_series import CaseSampleStatus, ProcessedCaseSeries
from app.schemas.sonifiable import (
    MappingRule,
    MappingSource,
    SeriesProvenance,
    SonifiableSample,
    SonifiableSeries,
)

COVID_DOMAIN = "covid_cases"

#: Magnitude of the week-over-week log2 ratio that gives the fastest pulse rate.
#: 2.0 means a quadrupling or a drop to a quarter in one week reaches the top of
#: the rate scale, while an ordinary week sits low on it.
RATE_LOG2_FULL_SCALE = 2.0


def growth_to_rate(growth_log2: float | None, full_scale: float = RATE_LOG2_FULL_SCALE) -> float | None:
    """Normalized rate of change from a week-over-week log2 ratio.

    Uses the magnitude only, so a collapsing wave quickens the pulse just as a
    growing one does. ``None`` when the ratio is undefined.
    """
    if growth_log2 is None:
        return None
    if full_scale <= 0:
        raise ValueError("full_scale must be positive")
    return min(1.0, abs(growth_log2) / full_scale)

DOMAIN_MAPPINGS: tuple[MappingRule, ...] = (
    MappingRule(
        sound_parameter="level_normalized (-> loudness)",
        source_measurement="reported COVID-19 deaths or cases for one calendar week, for one location",
        rule=(
            "level_normalized = log10(count) / log10(reference maximum), with the reference maximum fixed "
            "at 1e5 deaths per week (1e8 cases per week). A week of 0 or 1 sits at the gain floor; the "
            "single highest week in the global series exceeds 1e5 and clips to 1.0 with flag clipped_high."
        ),
        rationale=(
            "Weekly counts span five orders of magnitude, so a linear mapping would leave everything "
            "except the peak inaudible. A log scale matches how counts are read and reported, and the "
            "reference is fixed and cited, so loudness is comparable between locations and never "
            "derived from the sample."
        ),
    ),
    MappingRule(
        sound_parameter="pan (-> stereo position)",
        source_measurement="week-over-week ratio of the reported total",
        rule=(
            "growth_log2 = log2(count / previous week's count); pan = clamp(growth_log2 / 1.0, -1, 1). "
            "A doubling pans fully right, a halving fully left, a flat week stays centred. Undefined for "
            "the first week and whenever either week is zero or unusable, which is centred and flagged."
        ),
        rationale=(
            "The magnitude is already carried by loudness, which changes only slowly on a log scale. The "
            "rate of change is what a listener experiences as a wave arriving or receding, so giving it "
            "its own continuous dimension makes the waves audible rather than merely present."
        ),
    ),
    MappingRule(
        sound_parameter="rate_normalized (-> pulse rate)",
        source_measurement="magnitude of the week-over-week log2 ratio",
        rule=(
            "rate_normalized = min(1, |growth_log2| / 2.0), so a quadrupling or a fall to a quarter in one "
            "week reaches the fastest pulse and a flat week the slowest. The magnitude only, so a "
            "collapsing wave quickens the pulse just as a growing one does. Undefined growth gives an "
            "unknown rate, which the engine treats as the slowest."
        ),
        rationale=(
            "The pandemic's defining feature is acceleration, and a uniform pulse makes a flat month and a "
            "doubling week sound equally eventful. Driving the pulse rate from the same growth ratio that "
            "drives the pan means a surge is heard as the rhythm quickening while the image moves right. "
            "Direction is already carried by the pan, so the rate uses the magnitude and does not "
            "duplicate it."
        ),
    ),
    MappingRule(
        sound_parameter="audibility",
        source_measurement="per-week aggregation status",
        rule=(
            "A week is silent when it has no source rows, when any row in it is null (so the total "
            "cannot be known to be complete), or when a value is negative. Every other week sounds, "
            "including a reported total of zero."
        ),
        rationale=(
            "The source posts a week's total on a single weekday and zero on the others, so treating "
            "every zero as a measurement would render six silent days a week as six real reports of "
            "zero deaths. Absent reporting is rendered as absence of sound instead."
        ),
    ),
)

OMITTED_MAPPINGS: tuple[MappingRule, ...] = (
    MappingRule(
        sound_parameter="pitch or timbre",
        source_measurement="none",
        rule="Not rendered; pitch is constant.",
        rationale=(
            "Candidates such as the case-fatality ratio are dominated by testing capacity and reporting "
            "practice rather than by the quantity a listener would take them to mean, so nothing is "
            "mapped to pitch. Deliberately left for Phase 6 listening validation."
        ),
    ),
    MappingRule(
        sound_parameter="per-capita normalization",
        source_measurement="deaths or cases per million",
        rule="Not rendered in the default series.",
        rationale=(
            "The default location is World, for which absolute and per-capita series carry the same "
            "shape. Per-capita columns exist in the source and are the right choice for comparing "
            "countries, which is a later phase."
        ),
    ),
)


def case_series_to_sonifiable(series: ProcessedCaseSeries) -> SonifiableSeries:
    """Map a processed COVID-19 weekly series onto the engine's neutral representation."""
    metadata = series.metadata
    samples = []
    for sample in series.samples:
        audible = sample.status is CaseSampleStatus.VALID
        samples.append(
            SonifiableSample(
                # Each week is labelled by the UTC midnight of the Sunday that closes it.
                timestamp=datetime.combine(sample.week_ending, time.min, tzinfo=UTC),
                audible=audible,
                status=sample.status.value,
                level_normalized=sample.level_normalized if audible else None,
                pan=sample.pan if audible else None,
                rate_normalized=growth_to_rate(sample.growth_log2) if audible else None,
                flags=[flag.value for flag in sample.flags],
                source_values={
                    "count": sample.count,
                    "growth_log2": sample.growth_log2,
                    "reported_days": float(sample.reported_days),
                },
            )
        )
    selection = metadata.selection.model_dump(mode="json")
    return SonifiableSeries(
        provenance=SeriesProvenance(
            domain=COVID_DOMAIN,
            dataset=metadata.dataset,
            dataset_version=metadata.dataset_version,
            source_file=metadata.source_file,
            source_sha256=metadata.source_sha256,
            source_url=metadata.source_url,
            processing_version=metadata.processing_version,
            selection=selection,
            cadence=metadata.selection.reporting_cadence,
        ),
        mapping_source=MappingSource(
            level_quantity=(
                f"weekly reported COVID-19 {metadata.selection.metric.value} for "
                f"{metadata.selection.location} ({metadata.units}), log10 scaled"
            ),
            level_reference=metadata.level_normalization,
            pan_quantity="week-over-week growth ratio of the weekly total",
            pan_rule=metadata.growth_rule,
            rate_quantity="magnitude of the week-over-week growth ratio",
            rate_rule=f"rate = min(1, |growth_log2| / {RATE_LOG2_FULL_SCALE}); unknown when growth is undefined",
        ),
        samples=samples,
        domain_mappings=list(DOMAIN_MAPPINGS),
        omitted_mappings=list(OMITTED_MAPPINGS),
    )
