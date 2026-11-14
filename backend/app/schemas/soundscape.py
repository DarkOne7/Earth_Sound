"""Typed settings, per-segment traces and provenance for rendered soundscapes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.current_series import SampleFlag, SampleStatus, SelectionParameters


class SonificationSettings(BaseModel):
    """Every parameter that influences the rendered audio.

    All fields have documented defaults (``docs/sonification-mapping.md``).
    The model is frozen and fully recorded in the provenance, so a soundscape
    can be regenerated from its provenance alone.
    """

    model_config = ConfigDict(frozen=True)

    duration_seconds: float = Field(default=30.0, gt=0, description="Total output length.")
    sample_rate_hz: int = Field(default=44100, ge=8000)
    channels: Literal[2] = Field(default=2, description="Stereo, so direction can pan.")
    bit_depth: Literal[16] = Field(default=16, description="16-bit signed PCM WAV.")

    carrier_frequency_hz: float = Field(
        default=220.0, gt=0, description="Fixed pitch (A3). Not data-driven: no temperature variable."
    )
    harmonic_amplitudes: tuple[float, ...] = Field(
        default=(1.0, 0.5, 0.25),
        description="Relative amplitudes of harmonics 1..n of the carrier; fixed timbre.",
    )

    gain_floor_db: float = Field(
        default=-36.0,
        lt=0,
        description="Gain at speed_normalized = 0 (quietest audible level). 0 dB at speed_normalized = 1.",
    )
    peak_amplitude: float = Field(
        default=0.9, gt=0, le=1.0, description="Full-scale fraction at 0 dB gain; leaves headroom."
    )

    attack_seconds: float = Field(default=0.01, ge=0, description="Linear fade-in at each segment start.")
    release_seconds: float = Field(default=0.05, ge=0, description="Linear fade-out at each segment end.")

    direction_mapping: Literal["stereo_pan_east_west", "none"] = Field(
        default="stereo_pan_east_west",
        description="How direction is rendered. 'none' keeps every segment centred.",
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


class MappingRule(BaseModel):
    """One documented link from a source measurement to a sound parameter."""

    model_config = ConfigDict(frozen=True)

    sound_parameter: str
    source_measurement: str
    rule: str
    rationale: str


class SegmentTrace(BaseModel):
    """What was rendered for one sample of the input series."""

    model_config = ConfigDict(frozen=True)

    index: int
    timestamp: datetime
    status: SampleStatus
    flags: list[SampleFlag] = Field(default_factory=list)
    start_seconds: float
    end_seconds: float
    start_frame: int
    end_frame: int = Field(description="Exclusive.")
    audible: bool = Field(description="False renders digital silence (no data), never zero speed.")
    speed_m_s: float | None
    speed_normalized: float | None
    gain_db: float | None
    amplitude: float | None = Field(description="Peak linear amplitude after gain, before the envelope.")
    direction_deg: float | None
    pan: float | None = Field(description="-1 = fully left (westward), 0 = centre, +1 = fully right (eastward).")


class SourceSeriesReference(BaseModel):
    """Enough of the processed series' metadata to trace the audio back to data."""

    model_config = ConfigDict(frozen=True)

    dataset: str | None
    dataset_version: str | None
    source_file: str | None
    source_sha256: str | None
    selection: SelectionParameters
    processing_version: str
    speed_normalization: str
    direction_convention: str
    total_samples: int
    usable_samples: int
    silent_samples: int


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
    mappings: list[MappingRule]
    omitted_mappings: list[MappingRule]
    source: SourceSeriesReference
    segments: list[SegmentTrace]
    output: OutputDescription


@dataclass(frozen=True)
class RenderedSoundscape:
    """WAV bytes with their provenance."""

    wav_bytes: bytes
    provenance: SoundscapeProvenance
