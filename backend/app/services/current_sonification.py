"""Ocean-current domain mapping: ``ProcessedCurrentSeries`` -> ``SonifiableSeries``.

This module holds every ocean-specific sonification decision, so the engine stays
domain-neutral. See ``docs/sonification-mapping.md``.

* Loudness comes from ``speed_normalized`` (speed against the documented
  0-2.5 m s-1 range from Phase 3).
* Stereo position comes from the **east-west component** of the direction the
  current flows toward: ``pan = sin(direction)``. Eastward is right, westward is
  left, northward and southward are centre.
* ``missing`` and ``invalid`` samples are silent; ``undefined_direction`` samples
  are audible and centred.
"""

from __future__ import annotations

import math

from app.schemas.current_series import ProcessedCurrentSeries, SampleStatus
from app.schemas.sonifiable import (
    MappingRule,
    MappingSource,
    SeriesProvenance,
    SonifiableSample,
    SonifiableSeries,
)

#: Sample statuses that produce sound. The others produce silence.
AUDIBLE_STATUSES = frozenset({SampleStatus.VALID, SampleStatus.UNDEFINED_DIRECTION})

#: Below this magnitude, ``sin()`` of 180 or 360 degrees is float noise, not a pan.
PAN_SNAP_TOLERANCE = 1e-12

#: Step change in ``speed_normalized`` that gives the fastest pulse rate. A quarter
#: of the full 0-2.5 m s-1 scale, i.e. 0.625 m s-1 between consecutive samples.
#: Daily surface currents change slowly, so a full scale of 1.0 would leave the
#: rhythm almost constant.
SPEED_RATE_FULL_SCALE = 0.25

OCEAN_DOMAIN = "ocean_currents"

DOMAIN_MAPPINGS: tuple[MappingRule, ...] = (
    MappingRule(
        sound_parameter="level_normalized (-> loudness)",
        source_measurement="current speed in m s-1, normalized against 0-2.5 m s-1 (Phase 3 speed_normalized)",
        rule=(
            "level_normalized = speed_normalized, used unchanged. Speeds above the reference maximum were "
            "already clipped to 1.0 by Phase 3 and carry the clipped_high flag."
        ),
        rationale=(
            "Speed is the primary quantity a listener should feel. Its reference range is global, not "
            "per-sample, so loudness is comparable between regions."
        ),
    ),
    MappingRule(
        sound_parameter="pan (-> stereo position)",
        source_measurement="direction_deg, the direction the current flows toward, clockwise from true north",
        rule=(
            "pan = sin(radians(direction_deg)), the east-west component. 90 (eastward) = +1 = right, "
            "270 (westward) = -1 = left, 0 and 180 (northward, southward) = 0 = centre. "
            "Values below 1e-12 are snapped to 0. Undefined direction (zero speed) = centre."
        ),
        rationale=(
            "Direction is circular, so mapping it to a linear parameter such as pitch would put a "
            "discontinuity at north and make 359 and 1 degrees sound maximally different although they "
            "are nearly the same flow. The east-west component maps continuously to left-right."
        ),
    ),
    MappingRule(
        sound_parameter="rate_normalized (-> pulse rate)",
        source_measurement="change in normalized speed since the previous usable sample",
        rule=(
            "rate_normalized = min(1, |speed_normalized - previous speed_normalized| / 0.25). A change of "
            "a quarter of the full 0-2.5 m s-1 scale in one step (0.625 m s-1) gives the fastest pulse. "
            "The first usable sample and any sample following a gap have no previous value, so their rate "
            "is unknown and treated as 0 (slowest)."
        ),
        rationale=(
            "A difference of two already-cited normalized speeds needs no further reference of its own. "
            "The 0.25 full scale is a product choice: daily surface-current changes are small, so a full "
            "scale of 1.0 would leave the pulse almost constant."
        ),
    ),
    MappingRule(
        sound_parameter="audibility",
        source_measurement="Phase 3 sample status",
        rule=(
            "valid and undefined_direction are audible; missing and invalid are silent. A measured zero "
            "speed therefore still sounds, at the gain floor."
        ),
        rationale="Absent or rejected measurements must not be rendered as a current of zero speed.",
    ),
)

OMITTED_MAPPINGS: tuple[MappingRule, ...] = (
    MappingRule(
        sound_parameter="pitch or timbre from temperature",
        source_measurement="sea-surface temperature (not available)",
        rule="Not rendered.",
        rationale=(
            "Phase 2 verified at file level (2026-11-14) that OSCAR v2.0 Final granules contain only u, v, ug, vg, "
            "lat, lon and time; SST appears only as an input in the 'source' attribute. "
            "See docs/ocean-dataset-decision.md."
        ),
    ),
    MappingRule(
        sound_parameter="north-south component of direction",
        source_measurement="cos(direction_deg)",
        rule="Not rendered.",
        rationale=(
            "No single clear, testable audio dimension was identified for a second circular component in "
            "the MVP, so northward and southward flow sound the same. Candidate for a later phase "
            "(e.g. brightness/filter cutoff), to be validated in Phase 6 listening tests."
        ),
    ),
)


def direction_to_pan(direction_deg: float | None) -> float | None:
    """East-west component of the direction as a pan position in [-1, 1].

    ``None`` (undefined direction) stays ``None``, which the engine centres.
    """
    if direction_deg is None:
        return None
    if not math.isfinite(direction_deg):
        raise ValueError(f"direction_deg must be finite, got {direction_deg!r}")
    pan = math.sin(math.radians(direction_deg))
    # sin() of 180 or 360 degrees is ~1e-16, not 0; snap values below float noise.
    if abs(pan) < PAN_SNAP_TOLERANCE:
        return 0.0
    return max(-1.0, min(1.0, pan))


def speed_change_to_rate(
    speed_normalized: float | None,
    previous: float | None,
    full_scale: float = SPEED_RATE_FULL_SCALE,
) -> float | None:
    """Normalized rate of change from consecutive normalized speeds.

    ``None`` when either value is missing, which the engine treats as the slowest rate.
    """
    if speed_normalized is None or previous is None:
        return None
    if full_scale <= 0:
        raise ValueError("full_scale must be positive")
    return min(1.0, abs(speed_normalized - previous) / full_scale)


def current_series_to_sonifiable(series: ProcessedCurrentSeries) -> SonifiableSeries:
    """Map a processed ocean-current series onto the engine's neutral representation."""
    metadata = series.metadata
    samples = []
    previous_speed: float | None = None
    for sample in series.samples:
        audible = sample.status in AUDIBLE_STATUSES
        rate = speed_change_to_rate(sample.speed_normalized, previous_speed) if audible else None
        samples.append(
            SonifiableSample(
                timestamp=sample.timestamp,
                audible=audible,
                status=sample.status.value,
                level_normalized=sample.speed_normalized if audible else None,
                pan=direction_to_pan(sample.direction_deg) if audible else None,
                rate_normalized=rate,
                flags=[flag.value for flag in sample.flags],
                source_values={
                    "speed_m_s": sample.speed,
                    "direction_deg": sample.direction_deg,
                    "u_m_s": sample.u,
                    "v_m_s": sample.v,
                },
            )
        )
        # A gap clears the reference, so the sample after it has no step to measure.
        previous_speed = sample.speed_normalized if audible else None
    return SonifiableSeries(
        provenance=SeriesProvenance(
            domain=OCEAN_DOMAIN,
            dataset=metadata.dataset,
            dataset_version=metadata.dataset_version,
            source_file=metadata.source_file,
            source_sha256=metadata.source_sha256,
            source_url=None,
            processing_version=metadata.processing_version,
            selection=metadata.selection.model_dump(mode="json"),
            cadence="One sample per dataset time step (daily for OSCAR v2.0).",
        ),
        mapping_source=MappingSource(
            level_quantity="current speed (m s-1), normalized against the Phase 3 reference range",
            level_reference=metadata.speed_normalization,
            pan_quantity="direction the current flows toward (degrees clockwise from true north)",
            pan_rule="pan = sin(radians(direction_deg)); east-west component only",
            rate_quantity="change in normalized speed since the previous usable sample",
            rate_rule=(
                f"rate = min(1, |delta speed_normalized| / {SPEED_RATE_FULL_SCALE}); unknown at the start "
                "and after a gap"
            ),
        ),
        samples=samples,
        domain_mappings=list(DOMAIN_MAPPINGS),
        omitted_mappings=list(OMITTED_MAPPINGS),
    )
