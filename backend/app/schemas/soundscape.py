"""Typed settings, per-segment traces and provenance for rendered soundscapes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.sonifiable import MappingRule, MappingSource

__all__ = [
    "MappingRule",
    "OutputDescription",
    "RenderedSoundscape",
    "SegmentTrace",
    "SonificationSettings",
    "SoundscapeProvenance",
    "SourceSeriesReference",
    "TimingDescription",
]


class SonificationSettings(BaseModel):
    """Every parameter that influences the rendered audio.

    All fields have documented defaults (``docs/sonification-mapping.md``).
    The model is frozen and fully recorded in the provenance, so a soundscape
    can be regenerated from its provenance alone.
    """

    model_config = ConfigDict(frozen=True)

    duration_seconds: float = Field(default=30.0, gt=0, description="Total output length.")
    sample_rate_hz: int = Field(default=44100, ge=8000)
    channels: Literal[2] = Field(default=2, description="Stereo, so the pan mapping has somewhere to go.")
    bit_depth: Literal[16] = Field(default=16, description="16-bit signed PCM WAV.")

    carrier_frequency_hz: float = Field(
        default=220.0, gt=0, description="Fixed pitch (A3). Not data-driven in any domain."
    )
    harmonic_amplitudes: tuple[float, ...] = Field(
        default=(1.0, 0.5, 0.25),
        description="Relative amplitudes of harmonics 1..n of the carrier; fixed timbre.",
    )

    gain_floor_db: float = Field(
        default=-36.0,
        lt=0,
        description="Gain at level_normalized = 0 (quietest audible level). 0 dB at level_normalized = 1.",
    )
    peak_amplitude: float = Field(
        default=0.9, gt=0, le=1.0, description="Full-scale fraction at 0 dB gain; leaves headroom."
    )

    attack_seconds: float = Field(default=0.01, ge=0, description="Linear fade-in at each segment start.")
    release_seconds: float = Field(default=0.05, ge=0, description="Linear fade-out at each segment end.")

    pan_enabled: bool = Field(
        default=True, description="False keeps every segment centred, ignoring the series' pan values."
    )

    timing_mode: Literal["uniform", "rate_weighted"] = Field(
        default="rate_weighted",
        description=(
            "'uniform' gives every sample an equal share of the duration, so audio time maps linearly "
            "onto the date range. 'rate_weighted' shortens segments where the measurement is changing "
            "fast, so the rhythm speeds up through rapid change and slows through calm stretches."
        ),
    )
    rate_sensitivity: float = Field(
        default=1.5,
        gt=0,
        description=(
            "Strength of the rate weighting: segment length is proportional to "
            "1 / (1 + rate_sensitivity * rate_normalized), so the default makes the fastest-changing "
            "sample's segment 1/2.5 the length of a completely static one. 1.5 was chosen because it is "
            "the largest round value that keeps the reference COVID-19 render's fastest stretch below the "
            "20 Hz rhythm floor."
        ),
    )

    @model_validator(mode="after")
    def _check_harmonics(self) -> SonificationSettings:
        if not self.harmonic_amplitudes:
            raise ValueError("harmonic_amplitudes must contain at least one value")
        if any(a < 0 for a in self.harmonic_amplitudes):
            raise ValueError("harmonic_amplitudes must be non-negative")
        if sum(self.harmonic_amplitudes) <= 0:
            raise ValueError("harmonic_amplitudes must sum to a positive value")
        return self


class SegmentTrace(BaseModel):
    """What was rendered for one sample of the input series."""

    model_config = ConfigDict(frozen=True)

    index: int
    timestamp: datetime
    status: str
    flags: list[str] = Field(default_factory=list)
    start_seconds: float
    end_seconds: float
    start_frame: int
    end_frame: int = Field(description="Exclusive.")
    audible: bool = Field(description="False renders digital silence (absent data), never a measured zero.")
    source_values: dict[str, float | None] = Field(default_factory=dict)
    level_normalized: float | None
    rate_normalized: float | None = Field(description="The normalized rate of change that set this segment's length.")
    gain_db: float | None
    amplitude: float | None = Field(description="Peak linear amplitude after gain, before the envelope.")
    pan: float | None = Field(description="-1 = fully left, 0 = centre, +1 = fully right.")


class SourceSeriesReference(BaseModel):
    """Enough of the series' metadata to trace the audio back to the source data."""

    model_config = ConfigDict(frozen=True)

    domain: str
    dataset: str | None
    dataset_version: str | None
    source_file: str | None
    source_sha256: str | None
    source_url: str | None
    processing_version: str
    cadence: str | None
    selection: dict[str, Any]
    mapping_source: MappingSource
    total_samples: int
    audible_samples: int
    silent_samples: int


class TimingDescription(BaseModel):
    """The rendered rhythm, and whether it is slow enough to be heard as one."""

    model_config = ConfigDict(frozen=True)

    timing_mode: Literal["uniform", "rate_weighted"]
    rate_sensitivity: float
    linear_time_axis: bool = Field(
        description=(
            "True when equal audio time means equal elapsed data time. False under rate weighting, where "
            "each segment's own timestamp and start_seconds give the exact mapping instead."
        )
    )
    segment_count: int
    segment_seconds_min: float
    segment_seconds_max: float
    pulse_rate_hz_mean: float = Field(description="Segments per second averaged over the whole soundscape.")
    pulse_rate_hz_max: float = Field(description="Fastest instantaneous pulse rate, from the shortest segment.")
    rhythm_floor_hz: float = Field(description="Above this pulse rate, separate pulses fuse into a rough tone.")
    heard_as_rhythm: bool = Field(
        description=(
            "False means the fastest stretch is too dense for its duration: pulses fuse there instead of "
            "being counted."
        )
    )


class OutputDescription(BaseModel):
    model_config = ConfigDict(frozen=True)

    container: Literal["WAV (RIFF)"] = "WAV (RIFF)"
    encoding: Literal["PCM signed 16-bit little-endian"] = "PCM signed 16-bit little-endian"
    sample_rate_hz: int
    channels: int
    bit_depth: int
    frames: int
    duration_seconds: float
    byte_count: int
    sha256: str


class SoundscapeProvenance(BaseModel):
    """Complete record of how a soundscape was produced."""

    model_config = ConfigDict(frozen=True)

    sonification_version: str
    determinism: str
    settings: SonificationSettings
    engine_mappings: list[MappingRule] = Field(description="Domain-independent rules the engine always applies.")
    domain_mappings: list[MappingRule] = Field(description="How this domain's measurements became level and pan.")
    omitted_mappings: list[MappingRule]
    source: SourceSeriesReference
    timing: TimingDescription
    segments: list[SegmentTrace]
    output: OutputDescription

    @property
    def mappings(self) -> list[MappingRule]:
        """Every rule behind the audio: engine rules plus this domain's rules."""
        return [*self.engine_mappings, *self.domain_mappings]


@dataclass(frozen=True)
class RenderedSoundscape:
    """WAV bytes with their provenance."""

    wav_bytes: bytes
    provenance: SoundscapeProvenance
