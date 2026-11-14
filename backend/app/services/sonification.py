"""Deterministic sonification engine.

The engine renders a :class:`~app.schemas.sonifiable.SonifiableSeries`: a series
whose domain has already validated its measurements and reduced them to two
audio-ready numbers per sample, ``level_normalized`` in ``[0, 1]`` and ``pan`` in
``[-1, 1]``. The engine therefore holds no knowledge of currents, case counts or
any other quantity; each domain documents its own mapping and supplies the rules
it used, which are recorded alongside the engine's own.

Engine rules (see ``docs/sonification-mapping.md``):

* The output lasts exactly ``duration_seconds``. The timeline is divided into one
  segment per sample, in series order, so the rhythm *is* the data's time axis.
* Each audible sample is a pulse of a fixed-pitch tone whose loudness comes from
  ``level_normalized``, mapped linearly in decibels.
* Samples the domain marked inaudible are digital silence: absent data is a gap
  in the sound, never a rendered zero.
* ``pan`` places the pulse between the left and right channels under a
  constant-power law.
* Pitch and timbre are constant, because no domain maps a measurement to them.

There is no random number generation, so there is no seed: the output is a pure
function of the series and :class:`SonificationSettings`.
"""

from __future__ import annotations

import hashlib
import io
import math
import wave
from collections.abc import Sequence
from fractions import Fraction
from typing import TYPE_CHECKING, Any

import numpy as np

from app.config import get_settings
from app.schemas.sonifiable import MappingRule, SonifiableSample, SonifiableSeries
from app.schemas.soundscape import (
    OutputDescription,
    RenderedSoundscape,
    SegmentTrace,
    SonificationSettings,
    SoundscapeProvenance,
    SourceSeriesReference,
    TimingDescription,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.schemas.current_series import ProcessedCurrentSeries

#: Version of the engine's rules. Bump it whenever a rule or default changes.
#: 2.0.0 generalized the engine from ocean currents to any validated series.
SONIFICATION_VERSION = "2.0.0"

#: Largest 16-bit PCM magnitude. Samples are scaled by this and rounded to nearest.
PCM16_FULL_SCALE = 32767

#: Above roughly 20 pulses per second, successive pulses stop being heard as
#: separate events and fuse into one rough or buzzing tone. A series dense enough
#: to cross this threshold is still rendered, but the provenance records that its
#: rhythm cannot be counted, so a buzz is never mistaken for a reported rhythm.
RHYTHM_FLOOR_HZ = 20.0

DETERMINISM_STATEMENT = (
    "No randomness is used. Identical series content and identical SonificationSettings "
    "give byte-identical WAV bytes and provenance on the same platform and NumPy build "
    "(tested). Across platforms, floating-point libm differences may change isolated PCM "
    "samples by at most 1 LSB; two renders are considered equivalent when their provenance "
    "(excluding output.sha256) is identical and every PCM sample differs by at most 1 LSB."
)

ENGINE_MAPPINGS: tuple[MappingRule, ...] = (
    MappingRule(
        sound_parameter="segment order (which pulse is which sample)",
        source_measurement="sample order of the validated series",
        rule=(
            "The samples are rendered in series order, one segment each, covering the whole duration with "
            "no overlap and no gap. Each audible segment is shaped by a linear attack and release envelope, "
            "so each sample is one pulse. Every segment records its own timestamp, start_seconds and "
            "end_seconds, so the mapping from audio time to data time is always exact."
        ),
        rationale="Temporal change is heard as a sequence of pulses that follows the measured time axis.",
    ),
    MappingRule(
        sound_parameter="segment length (pulse rate / rhythm)",
        source_measurement=(
            "rate_normalized in [0, 1], the normalized rate of change derived and documented by the data "
            "domain; unused under timing_mode='uniform'"
        ),
        rule=(
            "timing_mode='uniform': every sample gets an equal share of the duration. "
            "timing_mode='rate_weighted' (default): a sample's share is proportional to "
            "1 / (1 + rate_sensitivity * rate_normalized), so rapid change gives short segments and a fast "
            "pulse, calm stretches give long segments and a slow pulse. The total duration is unchanged; "
            "only its distribution moves. Boundaries are computed in exact rational arithmetic. "
            "provenance.timing records the mode, the mean and fastest pulse rates, whether the fastest "
            "stretch stays below the 20 Hz rhythm floor, and whether the time axis is linear."
        ),
        rationale=(
            "A uniform pulse makes a long calm stretch and a sudden surge sound equally eventful. Letting "
            "the rate of change set the pulse rate means acceleration is heard as acceleration, which is "
            "the quantity a listener registers most immediately. The cost is that audio time is no longer "
            "proportional to elapsed data time, so linear_time_axis is recorded as false and each segment "
            "carries its own timestamp for the exact mapping."
        ),
    ),
    MappingRule(
        sound_parameter="loudness (gain)",
        source_measurement="level_normalized in [0, 1], derived and documented by the data domain",
        rule=(
            "gain_db = gain_floor_db * (1 - level_normalized); amplitude = peak_amplitude * 10**(gain_db/20). "
            "level_normalized = 0 gives gain_floor_db (default -36 dB), 1 gives 0 dB."
        ),
        rationale=(
            "Perceived loudness grows roughly with the logarithm of amplitude, so equal steps in the "
            "normalized level give roughly equal loudness steps. A level of 0 stays audible so that a "
            "measured minimum is distinguishable from silence, which means absent data."
        ),
    ),
    MappingRule(
        sound_parameter="silence",
        source_measurement="the domain's audibility decision for each sample",
        rule="A sample the domain marked inaudible renders digital silence for its whole segment (all PCM samples 0).",
        rationale="A gap in the data must not be heard as a measurement, so it is not heard at all.",
    ),
    MappingRule(
        sound_parameter="stereo position (pan)",
        source_measurement="pan in [-1, 1], derived and documented by the data domain",
        rule=(
            "Constant-power law: angle = (pan + 1)/2 * pi/2, left = cos(angle), right = sin(angle), so "
            "left**2 + right**2 = 1 at every position. pan = None (unmapped or undefined) is centred. "
            "Disabled when pan_enabled is false, which centres every segment."
        ),
        rationale=(
            "Stereo position is a continuous dimension with no discontinuity, so it carries a second "
            "variable without disturbing the loudness that encodes the first."
        ),
    ),
    MappingRule(
        sound_parameter="pitch and timbre",
        source_measurement="none (constant)",
        rule=(
            "Fixed carrier at carrier_frequency_hz with harmonics 1..n at harmonic_amplitudes, normalized by "
            "their sum so the waveform peak never exceeds 1 before gain. Phase is continuous across the whole file."
        ),
        rationale=(
            "No domain maps a measurement to pitch, so pitch must not vary and imply a measurement "
            "that does not exist."
        ),
    ),
)


class SonificationError(ValueError):
    """Raised when a series cannot be sonified under the documented rules."""


# --- engine mapping functions (pure, unit-tested) ---------------------------


def level_to_gain_db(level_normalized: float, settings: SonificationSettings) -> float:
    """``gain_floor_db * (1 - level_normalized)``; 0 -> floor, 1 -> 0 dB."""
    if not (math.isfinite(level_normalized) and 0.0 <= level_normalized <= 1.0):
        raise SonificationError(f"level_normalized must be in [0, 1], got {level_normalized!r}")
    return settings.gain_floor_db * (1.0 - level_normalized)


def gain_db_to_amplitude(gain_db: float, settings: SonificationSettings) -> float:
    """Linear peak amplitude for a gain in dB, relative to ``peak_amplitude``."""
    return settings.peak_amplitude * 10.0 ** (gain_db / 20.0)


def pan_to_channel_gains(pan: float) -> tuple[float, float]:
    """Constant-power stereo gains ``(left, right)`` for a pan in [-1, 1]."""
    if not (math.isfinite(pan) and -1.0 <= pan <= 1.0):
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
    # Exact rational arithmetic: avoids float rounding differences between platforms.
    return np.array(
        [round(Fraction(i * total_frames, segment_count)) for i in range(segment_count + 1)], dtype=np.int64
    )


def rate_weighted_boundaries(
    total_frames: int,
    rates: Sequence[float | None],
    sensitivity: float,
) -> np.ndarray:
    """Segment boundaries whose lengths shrink where the measurement changes fast.

    Each sample's share of the duration is proportional to
    ``1 / (1 + sensitivity * rate_normalized)``, so a sample at the top of its
    domain's rate scale gets ``1 / (1 + sensitivity)`` of the share a completely
    static one gets, and the rhythm audibly accelerates through rapid change.
    ``None`` rates (unknown, including every silent sample) count as 0, the
    slowest. The total duration is unchanged: only its distribution moves.

    Computed in exact rational arithmetic so the layout is reproducible.
    """
    count = len(rates)
    if count < 1:
        raise SonificationError("A soundscape needs at least one sample")
    if total_frames < count:
        raise SonificationError(
            f"{total_frames} frames cannot hold {count} segments of at least one frame each"
        )
    if sensitivity <= 0:
        raise SonificationError("rate_sensitivity must be positive")
    weights = [
        Fraction(1) / (Fraction(1) + Fraction(sensitivity) * Fraction(0.0 if rate is None else rate))
        for rate in rates
    ]
    total_weight = sum(weights)
    boundaries = [0]
    cumulative = Fraction(0)
    for weight in weights:
        cumulative += weight
        boundaries.append(round(Fraction(total_frames) * cumulative / total_weight))
    boundaries[-1] = total_frames  # guard against a rounding shortfall at the end
    lengths = np.diff(boundaries)
    if lengths.min() < 1:
        shortest = int(np.argmin(lengths))
        raise SonificationError(
            f"Rate weighting left segment {shortest} with {int(lengths[shortest])} frames. "
            f"Lower rate_sensitivity, raise duration_seconds, or use timing_mode='uniform'."
        )
    return np.array(boundaries, dtype=np.int64)


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


def describe_timing(segments: Sequence[SegmentTrace], settings: SonificationSettings) -> TimingDescription:
    """Pulse rate of the rendered soundscape and whether it can be heard as rhythm.

    Under rate weighting the pulse rate varies, so the fastest stretch is what
    decides whether the rhythm can still be counted.
    """
    lengths = [segment.end_frame - segment.start_frame for segment in segments]
    rate = settings.sample_rate_hz
    shortest_seconds = min(lengths) / rate
    fastest = 1.0 / shortest_seconds
    return TimingDescription(
        timing_mode=settings.timing_mode,
        rate_sensitivity=settings.rate_sensitivity,
        linear_time_axis=settings.timing_mode == "uniform",
        segment_count=len(segments),
        segment_seconds_min=shortest_seconds,
        segment_seconds_max=max(lengths) / rate,
        pulse_rate_hz_mean=len(segments) / settings.duration_seconds,
        pulse_rate_hz_max=fastest,
        rhythm_floor_hz=RHYTHM_FLOOR_HZ,
        heard_as_rhythm=fastest <= RHYTHM_FLOOR_HZ,
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
    sample: SonifiableSample,
    start: int,
    end: int,
    settings: SonificationSettings,
) -> SegmentTrace:
    gain_db = amplitude = pan = None
    if sample.audible:
        assert sample.level_normalized is not None  # guaranteed by SonifiableSample
        gain_db = level_to_gain_db(sample.level_normalized, settings)
        amplitude = gain_db_to_amplitude(gain_db, settings)
        pan = sample.pan if (settings.pan_enabled and sample.pan is not None) else 0.0
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
        audible=sample.audible,
        source_values=dict(sample.source_values),
        level_normalized=sample.level_normalized,
        rate_normalized=sample.rate_normalized,
        gain_db=gain_db,
        amplitude=amplitude,
        pan=pan,
    )


def plan_segments(samples: Sequence[SonifiableSample], settings: SonificationSettings) -> list[SegmentTrace]:
    """Decide every audio parameter per sample without rendering any audio."""
    total_frames = round(settings.duration_seconds * settings.sample_rate_hz)
    if settings.timing_mode == "rate_weighted":
        boundaries = rate_weighted_boundaries(
            total_frames, [sample.rate_normalized for sample in samples], settings.rate_sensitivity
        )
    else:
        boundaries = segment_boundaries(total_frames, len(samples))
    return [
        _trace_segment(i, sample, int(boundaries[i]), int(boundaries[i + 1]), settings)
        for i, sample in enumerate(samples)
    ]


def _as_sonifiable(series: SonifiableSeries | ProcessedCurrentSeries | Any) -> SonifiableSeries:
    """Accept a neutral series, or adapt a known domain series to one."""
    if isinstance(series, SonifiableSeries):
        return series
    # Imported lazily so the engine does not depend on any particular domain.
    from app.schemas.case_series import ProcessedCaseSeries as _Covid
    from app.schemas.current_series import ProcessedCurrentSeries as _Ocean

    if isinstance(series, _Ocean):
        from app.services.current_sonification import current_series_to_sonifiable

        return current_series_to_sonifiable(series)
    if isinstance(series, _Covid):
        from app.services.case_sonification import case_series_to_sonifiable

        return case_series_to_sonifiable(series)
    raise SonificationError(
        f"Cannot sonify {type(series).__name__}; expected a SonifiableSeries or a known domain series"
    )


def render_soundscape(
    series: SonifiableSeries | ProcessedCurrentSeries,
    settings: SonificationSettings | None = None,
) -> RenderedSoundscape:
    """Render a validated series to WAV bytes plus full provenance.

    Accepts a :class:`SonifiableSeries`, or a domain series that has a registered
    adapter (currently ``ProcessedCurrentSeries``).

    Raises ``SonificationError`` when the series is empty or has no audible sample.
    """
    if settings is None:
        settings = default_sonification_settings()
    sonifiable = _as_sonifiable(series)
    samples = sonifiable.samples
    if not samples:
        raise SonificationError("Cannot sonify an empty series")
    if not any(sample.audible for sample in samples):
        raise SonificationError("Cannot sonify a series in which every sample is silent")

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
    source = sonifiable.provenance
    provenance = SoundscapeProvenance(
        sonification_version=SONIFICATION_VERSION,
        determinism=DETERMINISM_STATEMENT,
        settings=settings,
        engine_mappings=list(ENGINE_MAPPINGS),
        domain_mappings=list(sonifiable.domain_mappings),
        omitted_mappings=list(sonifiable.omitted_mappings),
        source=SourceSeriesReference(
            domain=source.domain,
            dataset=source.dataset,
            dataset_version=source.dataset_version,
            source_file=source.source_file,
            source_sha256=source.source_sha256,
            source_url=source.source_url,
            processing_version=source.processing_version,
            cadence=source.cadence,
            selection=dict(source.selection),
            mapping_source=sonifiable.mapping_source,
            total_samples=len(samples),
            audible_samples=len(samples) - silent,
            silent_samples=silent,
        ),
        timing=describe_timing(segments, settings),
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
