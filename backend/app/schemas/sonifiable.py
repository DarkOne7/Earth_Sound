"""Domain-neutral series that the sonification engine renders.

Each data domain (ocean currents, COVID-19 case counts, ...) validates and
normalizes its own measurements, then presents them here as two already-normalized
audio-ready quantities plus the evidence for how they were derived:

* ``level_normalized`` in ``[0, 1]`` drives loudness.
* ``pan`` in ``[-1, 1]`` drives stereo position (``None`` means centre).

The engine knows nothing about speeds, directions or case counts. It only knows
how to turn these two numbers into sound, which keeps every domain-specific
scientific decision in that domain's own module and its own documentation.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.references import NormalizationReference


class MappingRule(BaseModel):
    """One documented link from a source measurement to a sound parameter."""

    model_config = ConfigDict(frozen=True)

    sound_parameter: str
    source_measurement: str
    rule: str
    rationale: str


class SonifiableSample(BaseModel):
    """One already-validated, already-normalized point of a series."""

    model_config = ConfigDict(frozen=True)

    timestamp: datetime
    audible: bool = Field(
        description="False renders digital silence. Reserved for absent or unusable data, never for a measured zero."
    )
    status: str = Field(description="The domain's own status vocabulary, recorded verbatim in the provenance.")
    level_normalized: float | None = Field(
        default=None, ge=0.0, le=1.0, description="Loudness source in [0, 1]. Required when audible."
    )
    pan: float | None = Field(
        default=None, ge=-1.0, le=1.0, description="Stereo position in [-1, 1]. None means centre."
    )
    rate_normalized: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description=(
            "How fast the measurement is changing at this sample, in [0, 1]. Drives pulse rate: 1 is the "
            "fastest change the domain's scale allows and gives the shortest segment. None means unknown, "
            "which is treated as 0 (slowest)."
        ),
    )
    flags: list[str] = Field(default_factory=list, description="Domain flags, e.g. clipping, carried into the trace.")
    source_values: dict[str, float | None] = Field(
        default_factory=dict,
        description="The underlying measurements, so a segment can be traced back to numbers in the source data.",
    )

    @model_validator(mode="after")
    def _check_audible_has_level(self) -> SonifiableSample:
        if self.audible and self.level_normalized is None:
            raise ValueError("An audible sample must carry a level_normalized value")
        if not self.audible and (
            self.level_normalized is not None or self.pan is not None or self.rate_normalized is not None
        ):
            raise ValueError("A silent sample must not carry a level, pan or rate")
        return self


class MappingSource(BaseModel):
    """What the domain's ``level_normalized`` and ``pan`` actually mean."""

    model_config = ConfigDict(frozen=True)

    level_quantity: str = Field(description="The measurement behind loudness, with its units and scaling.")
    level_reference: NormalizationReference | None = Field(
        default=None, description="The cited range that produced level_normalized."
    )
    pan_quantity: str | None = Field(default=None, description="The measurement behind pan, or None when unmapped.")
    pan_rule: str | None = Field(default=None, description="How that measurement becomes a pan position.")
    rate_quantity: str | None = Field(
        default=None, description="The measurement behind the pulse rate, or None when unmapped."
    )
    rate_rule: str | None = Field(default=None, description="How that measurement becomes a normalized rate.")


class SeriesProvenance(BaseModel):
    """Where the series came from and how it was processed."""

    model_config = ConfigDict(frozen=True)

    domain: str = Field(description="Data domain identifier, e.g. 'ocean_currents' or 'covid_cases'.")
    dataset: str | None = None
    dataset_version: str | None = None
    source_file: str | None = None
    source_sha256: str | None = None
    source_url: str | None = None
    processing_version: str
    selection: dict[str, Any] = Field(
        default_factory=dict, description="The domain's reproducible selection parameters."
    )
    cadence: str | None = Field(default=None, description="Verified temporal resolution of the series.")


class SonifiableSeries(BaseModel):
    """A validated series plus everything needed to document its soundscape."""

    model_config = ConfigDict(frozen=True)

    provenance: SeriesProvenance
    mapping_source: MappingSource
    samples: list[SonifiableSample]
    domain_mappings: list[MappingRule] = Field(
        default_factory=list, description="The domain's own measurement-to-audio rules."
    )
    omitted_mappings: list[MappingRule] = Field(
        default_factory=list, description="Mappings deliberately not rendered, with the reason for each."
    )

    @property
    def audible_count(self) -> int:
        return sum(1 for sample in self.samples if sample.audible)
