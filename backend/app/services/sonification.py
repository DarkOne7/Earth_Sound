"""Deterministic sonification of a processed ocean-current series.

Every audio parameter comes from a documented rule in ``MAPPINGS`` (see
``docs/sonification-mapping.md``). There is no random number generation, so
there is no seed: the output is a pure function of the processed series and
``SonificationSettings``.

Overview of the render:

* The output lasts exactly ``duration_seconds``. The timeline is divided into
  one segment per input sample, in series order, so the rhythm *is* the data's
  time axis (one pulse per sample; one pulse per day for the OSCAR sample).
* Each ``valid`` or ``undefined_direction`` sample is a pulse of a fixed-pitch
  tone whose loudness comes from ``speed_normalized``. ``missing`` and
  ``invalid`` samples are digital silence: a gap in the data is a gap in the sound.
* Direction pans the pulse between the left (westward) and right (eastward)
  channels through its east-west component. The north-south component is not
  rendered (documented limitation).
* Pitch and timbre are constant. OSCAR has no temperature variable (Phase 2), so
  nothing data-driven is mapped to them.
"""

from __future__ import annotations

import hashlib
import io
import math
import wave
from collections.abc import Sequence

import numpy as np

from app.config import get_settings
from app.schemas.current_series import (
    CurrentSample,
    ProcessedCurrentSeries,
    SampleStatus,
)
from app.schemas.soundscape import (
    MappingRule,
    OutputDescription,
    RenderedSoundscape,
    SegmentTrace,
    SonificationSettings,
    SoundscapeProvenance,
    SourceSeriesReference,
)

#: Version of the mapping rules. Bump it whenever a rule or default changes.
SONIFICATION_VERSION = "1.0.0"

#: Sample statuses that produce sound. The others produce silence.
AUDIBLE_STATUSES = frozenset({SampleStatus.VALID, SampleStatus.UNDEFINED_DIRECTION})

#: Largest 16-bit PCM magnitude. Samples are scaled by this and rounded to nearest.
PCM16_FULL_SCALE = 32767

DETERMINISM_STATEMENT = (
    "No randomness is used. Identical ProcessedCurrentSeries content and identical "
    "SonificationSettings give byte-identical WAV bytes and provenance on the same "
    "platform and NumPy build (tested). Across platforms, floating-point libm "
    "differences may change isolated PCM samples by at most 1 LSB; two renders are "
    "considered equivalent when their provenance (excluding output.sha256) is identical "
    "and every PCM sample differs by at most 1 LSB."
)

MAPPINGS: tuple[MappingRule, ...] = (
    MappingRule(
        sound_parameter="segment timing (rhythm)",
        source_measurement="sample order and count of the processed series (one sample per file time step)",
        rule=(
            "duration_seconds is split into len(samples) equal segments in series order; "
            "segment i covers frames [round(i*F/n), round((i+1)*F/n)) with F = round(duration*sample_rate). "
            "Each audible segment is shaped by a linear attack and release envelope, so each sample is one pulse."
        ),
        rationale="Temporal change is heard as a sequence of pulses that follows the measured time axis.",
    ),
    MappingRule(
        sound_parameter="loudness (gain)",
        source_measurement="speed_normalized in [0, 1] (speed against the documented 0-2.5 m s-1 range)",
        rule=(
            "gain_db = gain_floor_db * (1 - speed_normalized); amplitude = peak_amplitude * 10**(gain_db/20). "
            "speed_normalized = 0 gives gain_floor_db (default -36 dB), 1 gives 0 dB. "
            "Clipped speeds already carry speed_normalized = 1 (flag clipped_high) and sound at 0 dB."
        ),
        rationale=(
            "Perceived loudness grows roughly with the logarithm of amplitude, so equal steps in "
            "normalized speed give roughly equal loudness steps. Zero speed stays audible so it is "
            "distinguishable from silence, which means no data."
        ),
    ),
    MappingRule(
        sound_parameter="silence",
        source_measurement="sample status",
        rule="status missing or invalid renders digital silence for the whole segment (all PCM samples 0).",
        rationale="A gap in the data must not be heard as a measurement, so it is not heard at all.",
    ),
    MappingRule(
        sound_parameter="stereo position (pan)",
        source_measurement="direction_deg (direction the current flows toward, clockwise from north)",
        rule=(
            "pan = sin(radians(direction_deg)) in [-1, 1]; 90 (eastward) = +1 = right, 270 (westward) = -1 = left, "
            "0 and 180 (northward, southward) = 0 = centre. Undefined direction = centre. Constant-power law: "
            "angle = (pan + 1)/2 * pi/2, left = cos(angle), right = sin(angle). "
            "Disabled when direction_mapping = 'none' (all centred)."
        ),
        rationale=(
            "Direction is circular; mapping it to a linear parameter such as pitch would put a discontinuity "
            "at north. The east-west component maps continuously to left-right. The north-south component is "
            "NOT rendered: northward and southward flow sound the same (documented MVP limitation)."
        ),
    ),
    MappingRule(
        sound_parameter="pitch and timbre",
        source_measurement="none (constant)",
        rule=(
            "Fixed carrier at carrier_frequency_hz with harmonics 1..n at harmonic_amplitudes, normalized by "
            "their sum so the waveform peak never exceeds 1 before gain. Phase is continuous across the whole file."
        ),
        rationale="Nothing is mapped to pitch, so pitch must not vary and suggest a measurement that does not exist.",
    ),
)

OMITTED_MAPPINGS: tuple[MappingRule, ...] = (
    MappingRule(
        sound_parameter="pitch or timbre from temperature",
        source_measurement="sea-surface temperature (not available)",
        rule="Not rendered.",
        rationale=(
            "Phase 2 verified at file level (2026-11-14) that OSCAR v2.0 Final granules contain only u, v, ug, vg, "
            "lat, lon and time; SST appears only as an input in the 'source' attribute. See docs/ocean-dataset-decision.md."
        ),
    ),
    MappingRule(
        sound_parameter="north-south component of direction",
        source_measurement="cos(direction_deg)",
        rule="Not rendered.",
        rationale=(
            "No single clear, testable audio dimension was identified for a second circular component in the MVP. "
            "Candidate for a later phase (e.g. brightness/filter cutoff), to be validated in Phase 6 listening tests."
        ),
    ),
)


class SonificationError(ValueError):
    """Raised when a series cannot be sonified under the documented rules."""


# --- mapping functions (pure, unit-tested) --------------------------------


def speed_to_gain_db(speed_normalized: float, settings: SonificationSettings) -> float:
    """``gain_floor_db * (1 - speed_normalized)``; 0 -> floor, 1 -> 0 dB."""
    if not (math.isfinite(speed_normalized) and 0.0 <= speed_normalized <= 1.0):
        raise SonificationError(f"speed_normalized must be in [0, 1], got {speed_normalized!r}")
    return settings.gain_floor_db * (1.0 - speed_normalized)


def gain_db_to_amplitude(gain_db: float, settings: SonificationSettings) -> float:
    """Linear peak amplitude for a gain in dB, relative to ``peak_amplitude``."""
    return settings.peak_amplitude * 10.0 ** (gain_db / 20.0)


def direction_to_pan(direction_deg: float | None, settings: SonificationSettings) -> float:
    """East-west component of the direction as a pan position in [-1, 1].

    ``None`` (undefined direction) and ``direction_mapping == "none"`` give 0 (centre).
    """
    if settings.direction_mapping == "none" or direction_deg is None:
        return 0.0
    if not math.isfinite(direction_deg):
        raise SonificationError(f"direction_deg must be finite, got {direction_deg!r}")
    pan = math.sin(math.radians(direction_deg))
    # sin() of 180 or 360 degrees is ~1e-16, not 0; snap values below float noise.
    if abs(pan) < 1e-12:
        return 0.0
    return max(-1.0, min(1.0, pan))


def pan_to_channel_gains(pan: float) -> tuple[float, float]:
    """Constant-power stereo gains ``(left, right)`` for a pan in [-1, 1]."""
    if not (-1.0 <= pan <= 1.0):
        raise SonificationError(f"pan must be in [-1, 1], got {pan!r}")
    angle = (pan + 1.0) / 2.0 * (math.pi / 2.0)
    return math.cos(angle), math.sin(angle)


def segment_boundaries(total_frames: int, segment_count: int) -> np.ndarray:
    """Frame index of each segment start plus the final end (length ``segment_count + 1``).

    Segments differ in length by at most one frame and cover every frame exactly once.
    """
    if segment_count < 1:
        raise SonificationError("A soundscape needs at least one sample")
    if total_frames < segment_count:
        raise SonificationError(
            f"{total_frames} frames cannot hold {segment_count} segments of at least one frame each"
        )
    indices = np.arange(segment_count + 1, dtype=np.int64)
    # Python ints: exact, avoids float rounding differences between platforms.
    return np.array([round(int(i) * total_frames / segment_count) for i in indices], dtype=np.int64)


def segment_envelope(length: int, settings: SonificationSettings) -> np.ndarray:
    """Linear attack/release envelope in [0, 1] for a segment of ``length`` frames.

    Attack and release are each clamped to half the segment, so they never overlap.
    """
    attack = min(round(settings.attack_seconds * settings.sample_rate_hz), length // 2)
    release = min(round(settings.release_seconds * settings.sample_rate_hz), length // 2)
    envelope = np.ones(length, dtype=np.float64)
    if attack > 0:
        envelope[:attack] = np.arange(attack, dtype=np.float64) / attack
    if release > 0:
        envelope[length - release :] = np.arange(release, 0, -1, dtype=np.float64) / release
    return envelope


def carrier_waveform(total_frames: int, settings: SonificationSettings) -> np.ndarray:
    """Harmonic tone with peak <= 1, phase-continuous over the whole file."""
    t = np.arange(total_frames, dtype=np.float64) / settings.sample_rate_hz
    total = sum(settings.harmonic_amplitudes)
    wave_sum = np.zeros(total_frames, dtype=np.float64)
    for harmonic, amplitude in enumerate(settings.harmonic_amplitudes, start=1):
        if amplitude:
            wave_sum += (amplitude / total) * np.sin(2.0 * math.pi * settings.carrier_frequency_hz * harmonic * t)
    return wave_sum


def float_to_pcm16(stereo: np.ndarray) -> np.ndarray:
    """Round float samples in [-1, 1] to int16 (nearest, ties to even), clipping defensively."""
    scaled = np.rint(np.clip(stereo, -1.0, 1.0) * PCM16_FULL_SCALE)
    return scaled.astype("<i2")


def encode_wav(pcm: np.ndarray, settings: SonificationSettings) -> bytes:
    """RIFF/WAVE container around interleaved int16 frames of shape ``(frames, channels)``."""
    if pcm.ndim != 2 or pcm.shape[1] != settings.channels or pcm.dtype != np.dtype("<i2"):
        raise SonificationError("pcm must be little-endian int16 with shape (frames, channels)")
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(settings.channels)
        writer.setsampwidth(settings.bit_depth // 8)
        writer.setframerate(settings.sample_rate_hz)
        writer.writeframes(np.ascontiguousarray(pcm).tobytes())
    return buffer.getvalue()


def describe_wav(wav_bytes: bytes) -> OutputDescription:
    """Read the header back so the recorded format comes from the bytes, not from intent."""
    with wave.open(io.BytesIO(wav_bytes), "rb") as reader:
        channels = reader.getnchannels()
        sample_width = reader.getsampwidth()
        rate = reader.getframerate()
        frames = reader.getnframes()
    return OutputDescription(
        sample_rate_hz=rate,
        channels=channels,
        bit_depth=sample_width * 8,
        frames=frames,
        duration_seconds=frames / rate,
        byte_count=len(wav_bytes),
        sha256=hashlib.sha256(wav_bytes).hexdigest(),
    )


# --- rendering ---------------------------------------------------------------


def default_sonification_settings() -> SonificationSettings:
    """Defaults with the duration and sample rate taken from ``Settings``."""
    s = get_settings()
    return SonificationSettings(
        duration_seconds=s.soundscape_duration_seconds,
        sample_rate_hz=s.soundscape_sample_rate_hz,
    )


def _trace_segment(
    index: int,
    sample: CurrentSample,
    start: int,
    end: int,
    settings: SonificationSettings,
) -> SegmentTrace:
    audible = sample.status in AUDIBLE_STATUSES
    gain_db = amplitude = pan = None
    if audible:
        if sample.speed_normalized is None or sample.speed is None:
            raise SonificationError(f"Sample {index} has status {sample.status.value} but no speed")
        gain_db = speed_to_gain_db(sample.speed_normalized, settings)
        amplitude = gain_db_to_amplitude(gain_db, settings)
        pan = direction_to_pan(sample.direction_deg, settings)
    rate = settings.sample_rate_hz
    return SegmentTrace(
        index=index,
        timestamp=sample.timestamp,
        status=sample.status,
        flags=list(sample.flags),
        start_seconds=start / rate,
        end_seconds=end / rate,
        start_frame=start,
        end_frame=end,
        audible=audible,
        speed_m_s=sample.speed if audible else None,
        speed_normalized=sample.speed_normalized if audible else None,
        gain_db=gain_db,
        amplitude=amplitude,
        direction_deg=sample.direction_deg if audible else None,
        pan=pan,
    )


def plan_segments(samples: Sequence[CurrentSample], settings: SonificationSettings) -> list[SegmentTrace]:
    """Decide every audio parameter per sample without rendering any audio."""
    total_frames = round(settings.duration_seconds * settings.sample_rate_hz)
    boundaries = segment_boundaries(total_frames, len(samples))
    return [
        _trace_segment(i, sample, int(boundaries[i]), int(boundaries[i + 1]), settings)
        for i, sample in enumerate(samples)
    ]


def render_soundscape(
    series: ProcessedCurrentSeries,
    settings: SonificationSettings | None = None,
) -> RenderedSoundscape:
    """Render a processed series to WAV bytes plus full provenance.

    Raises ``SonificationError`` when the series is empty or has no audible sample.
    """
    if settings is None:
        settings = default_sonification_settings()
    samples = series.samples
    if not samples:
        raise SonificationError("Cannot sonify an empty series")
    if not any(sample.status in AUDIBLE_STATUSES for sample in samples):
        raise SonificationError("Cannot sonify a series with no usable (valid or undefined_direction) sample")

    segments = plan_segments(samples, settings)
    total_frames = int(segments[-1].end_frame)
    carrier = carrier_waveform(total_frames, settings)
    stereo = np.zeros((total_frames, settings.channels), dtype=np.float64)

    for segment in segments:
        if not segment.audible:
            continue  # stays at exact zero: digital silence
        start, end = segment.start_frame, segment.end_frame
        assert segment.amplitude is not None and segment.pan is not None
        mono = carrier[start:end] * segment_envelope(end - start, settings) * segment.amplitude
        left, right = pan_to_channel_gains(segment.pan)
        stereo[start:end, 0] = mono * left
        stereo[start:end, 1] = mono * right

    wav_bytes = encode_wav(float_to_pcm16(stereo), settings)
    output = describe_wav(wav_bytes)
    _validate_output(output, settings, total_frames)

    silent = sum(1 for segment in segments if not segment.audible)
    metadata = series.metadata
    provenance = SoundscapeProvenance(
        sonification_version=SONIFICATION_VERSION,
        determinism=DETERMINISM_STATEMENT,
        settings=settings,
        mappings=list(MAPPINGS),
        omitted_mappings=list(OMITTED_MAPPINGS),
        source=SourceSeriesReference(
            dataset=metadata.dataset,
            dataset_version=metadata.dataset_version,
            source_file=metadata.source_file,
            source_sha256=metadata.source_sha256,
            selection=metadata.selection,
            processing_version=metadata.processing_version,
            speed_normalization=(
                f"{metadata.speed_normalization.minimum}-{metadata.speed_normalization.maximum} "
                f"{metadata.speed_normalization.units} ({metadata.speed_normalization.source})"
            ),
            direction_convention=metadata.direction_convention,
            total_samples=len(samples),
            usable_samples=len(samples) - silent,
            silent_samples=silent,
        ),
        segments=segments,
        output=output,
    )
    return RenderedSoundscape(wav_bytes=wav_bytes, provenance=provenance)


def _validate_output(output: OutputDescription, settings: SonificationSettings, expected_frames: int) -> None:
    """Fail loudly if the encoded file does not match the requested format and duration."""
    problems = []
    if output.channels != settings.channels:
        problems.append(f"channels {output.channels} != {settings.channels}")
    if output.bit_depth != settings.bit_depth:
        problems.append(f"bit depth {output.bit_depth} != {settings.bit_depth}")
    if output.sample_rate_hz != settings.sample_rate_hz:
        problems.append(f"sample rate {output.sample_rate_hz} != {settings.sample_rate_hz}")
    if output.frames != expected_frames:
        problems.append(f"frames {output.frames} != {expected_frames}")
    # Exact frame count means the duration differs from the request by less than one frame.
    if abs(output.duration_seconds - settings.duration_seconds) >= 1.0 / settings.sample_rate_hz:
        problems.append(f"duration {output.duration_seconds} s != {settings.duration_seconds} s")
    if problems:
        raise SonificationError("Rendered WAV failed validation: " + "; ".join(problems))
