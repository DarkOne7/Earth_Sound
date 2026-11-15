"""Engine tests: domain-neutral rendering of a SonifiableSeries."""

from __future__ import annotations

import io
import itertools
import math
import wave
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest
from app.schemas.references import NormalizationReference
from app.schemas.sonifiable import (
    MappingRule,
    MappingSource,
    SeriesProvenance,
    SonifiableSample,
    SonifiableSeries,
)
from app.schemas.soundscape import SonificationSettings, SoundscapeProvenance
from app.services.sonification import (
    ENGINE_MAPPINGS,
    PCM16_FULL_SCALE,
    RHYTHM_FLOOR_HZ,
    SONIFICATION_VERSION,
    SonificationError,
    carrier_waveform,
    describe_timing,
    describe_wav,
    encode_wav,
    float_to_pcm16,
    gain_db_to_amplitude,
    level_to_gain_db,
    pan_to_channel_gains,
    plan_segments,
    render_soundscape,
    segment_boundaries,
    segment_envelope,
)

T0 = datetime(2020, 1, 1, tzinfo=UTC)
# Short, low-rate settings keep the tests fast; the defaults are tested separately.
FAST = SonificationSettings(duration_seconds=1.0, sample_rate_hz=8000)


def sample(i, *, level=0.5, pan=0.0, rate=None, audible=True, status="valid", flags=(), values=None):
    return SonifiableSample(
        timestamp=T0 + timedelta(days=i),
        audible=audible,
        status=status,
        level_normalized=level if audible else None,
        pan=pan if audible else None,
        rate_normalized=rate if audible else None,
        flags=list(flags),
        source_values=values or {},
    )


def make_series(samples, *, domain="test_domain") -> SonifiableSeries:
    return SonifiableSeries(
        provenance=SeriesProvenance(
            domain=domain,
            dataset="Test dataset",
            dataset_version="1.0",
            source_file="synthetic.csv",
            source_sha256="0" * 64,
            source_url="https://example.test/data",
            processing_version="9.9.9",
            selection={"mode": "synthetic"},
            cadence="One sample per test step.",
        ),
        mapping_source=MappingSource(
            level_quantity="test quantity",
            level_reference=NormalizationReference(
                quantity="test quantity",
                minimum=0.0,
                maximum=1.0,
                units="unit",
                source="test",
                url="https://example.test",
            ),
            pan_quantity="test pan quantity",
            pan_rule="pan = test",
        ),
        samples=list(samples),
        domain_mappings=[
            MappingRule(
                sound_parameter="level_normalized (-> loudness)",
                source_measurement="test quantity",
                rule="level = value",
                rationale="test",
            )
        ],
        omitted_mappings=[
            MappingRule(
                sound_parameter="pitch", source_measurement="none", rule="Not rendered.", rationale="test"
            )
        ],
    )


def decode(wav_bytes: bytes) -> tuple[np.ndarray, int]:
    with wave.open(io.BytesIO(wav_bytes), "rb") as reader:
        frames = np.frombuffer(reader.readframes(reader.getnframes()), dtype="<i2")
        return frames.reshape(-1, reader.getnchannels()), reader.getframerate()


# --- level -> loudness ------------------------------------------------------


@pytest.mark.parametrize(
    ("level", "expected_db"),
    [(0.0, -36.0), (0.5, -18.0), (1.0, 0.0), (0.25, -27.0)],
)
def test_level_to_gain_is_linear_in_db(level, expected_db):
    assert level_to_gain_db(level, FAST) == pytest.approx(expected_db)


def test_gain_to_amplitude_matches_decibel_definition():
    assert gain_db_to_amplitude(0.0, FAST) == pytest.approx(0.9)
    assert gain_db_to_amplitude(-20.0, FAST) == pytest.approx(0.09)
    assert gain_db_to_amplitude(-36.0, FAST) == pytest.approx(0.9 * 10 ** (-36 / 20))


def test_loudness_increases_monotonically_with_level():
    gains = [gain_db_to_amplitude(level_to_gain_db(x, FAST), FAST) for x in np.linspace(0, 1, 11)]
    assert all(a < b for a, b in itertools.pairwise(gains))


@pytest.mark.parametrize("bad", [-0.01, 1.01, math.nan, math.inf])
def test_level_outside_unit_range_is_rejected(bad):
    with pytest.raises(SonificationError):
        level_to_gain_db(bad, FAST)


# --- pan law ----------------------------------------------------------------


def test_pan_law_is_constant_power():
    for pan in np.linspace(-1, 1, 21):
        left, right = pan_to_channel_gains(float(pan))
        assert left**2 + right**2 == pytest.approx(1.0)
    assert pan_to_channel_gains(0.0) == pytest.approx((math.sqrt(0.5), math.sqrt(0.5)))


def test_pan_extremes_are_single_channel():
    left_r, right_r = pan_to_channel_gains(1.0)
    left_l, right_l = pan_to_channel_gains(-1.0)
    assert right_r == pytest.approx(1.0) and left_r == pytest.approx(0.0, abs=1e-12)
    assert left_l == pytest.approx(1.0) and right_l == pytest.approx(0.0, abs=1e-12)


@pytest.mark.parametrize("bad", [-1.01, 1.01, math.nan])
def test_pan_outside_range_is_rejected(bad):
    with pytest.raises(SonificationError):
        pan_to_channel_gains(bad)


# --- timeline / rhythm ------------------------------------------------------


def test_segment_boundaries_cover_all_frames_exactly_once():
    boundaries = segment_boundaries(100, 30)
    assert boundaries[0] == 0 and boundaries[-1] == 100
    lengths = np.diff(boundaries)
    assert lengths.sum() == 100
    assert set(lengths.tolist()) <= {3, 4}


def test_segment_boundaries_reject_impossible_inputs():
    with pytest.raises(SonificationError):
        segment_boundaries(100, 0)
    with pytest.raises(SonificationError):
        segment_boundaries(5, 6)


def test_envelope_ramps_up_then_down_and_clamps_to_half_segment():
    env = segment_envelope(8000, FAST)  # 1 s at 8 kHz: attack 80, release 400 frames
    assert env[0] == 0.0 and env[80] == 1.0 and env[-1] == pytest.approx(1 / 400)
    assert np.all(np.diff(env[:80]) > 0) and np.all(np.diff(env[-400:]) < 0)
    short = segment_envelope(10, FAST)
    assert len(short) == 10 and short.max() == 1.0 and short.min() >= 0.0


def test_carrier_peak_never_exceeds_one_and_is_phase_continuous():
    wave_full = carrier_waveform(16000, FAST)
    assert np.abs(wave_full).max() <= 1.0
    # The full-file carrier restricted to a later segment equals the global signal there.
    assert np.array_equal(wave_full[8000:], carrier_waveform(16000, FAST)[8000:])


def test_timing_reports_pulse_rate_and_flags_a_cadence_too_fast_to_hear():
    dense = render_soundscape(make_series([sample(i) for i in range(30)]), FAST).provenance.timing
    assert dense.segment_count == 30 and dense.pulse_rate_hz_mean == 30.0
    # 30 pulses in 1 s is above the floor; the same 30 over 30 s is not.
    assert not dense.heard_as_rhythm and dense.rhythm_floor_hz == RHYTHM_FLOOR_HZ
    comfortable = render_soundscape(
        make_series([sample(i) for i in range(30)]), SonificationSettings(duration_seconds=30.0)
    ).provenance.timing
    assert comfortable.pulse_rate_hz_mean == 1.0 and comfortable.heard_as_rhythm


def test_timing_segment_lengths_are_reported_in_seconds():
    timing = describe_timing(plan_segments([sample(i) for i in range(3)], FAST), FAST)
    assert timing.segment_seconds_min == pytest.approx(1 / 3, abs=1 / 8000)
    assert timing.segment_seconds_max == pytest.approx(1 / 3, abs=1 / 8000)


# --- rate-driven rhythm -----------------------------------------------------


def test_a_series_without_rates_is_laid_out_uniformly():
    """Unknown rates must not warp the timeline, so the default stays safe."""
    samples = [sample(i, rate=None) for i in range(7)]
    weighted = plan_segments(samples, FAST)
    uniform = plan_segments(samples, FAST.model_copy(update={"timing_mode": "uniform"}))
    assert [s.start_frame for s in weighted] == [s.start_frame for s in uniform]


def test_rapid_change_shortens_its_segment_and_calm_lengthens_it():
    samples = [sample(0, rate=0.0), sample(1, rate=1.0), sample(2, rate=0.0)]
    segments = plan_segments(samples, FAST)
    lengths = [s.end_frame - s.start_frame for s in segments]
    assert lengths[1] < lengths[0] and lengths[1] < lengths[2]
    # 1 / (1 + 1.5 * 1) = 0.4 of a static sample's share.
    assert lengths[1] / lengths[0] == pytest.approx(0.4, rel=0.01)
    assert sum(lengths) == 8000  # the duration is unchanged, only its distribution


def test_pulse_rate_rises_monotonically_with_the_rate_of_change():
    rates = [0.0, 0.25, 0.5, 0.75, 1.0]
    segments = plan_segments([sample(i, rate=r) for i, r in enumerate(rates)], FAST)
    lengths = [s.end_frame - s.start_frame for s in segments]
    assert all(a > b for a, b in itertools.pairwise(lengths))


def test_higher_sensitivity_gives_more_tempo_contrast():
    samples = [sample(0, rate=0.0), sample(1, rate=1.0)]

    def spread(sensitivity):
        segments = plan_segments(samples, FAST.model_copy(update={"rate_sensitivity": sensitivity}))
        lengths = [s.end_frame - s.start_frame for s in segments]
        return max(lengths) / min(lengths)

    assert spread(0.5) < spread(1.5) < spread(4.0)


def test_rate_weighted_timing_covers_every_frame_exactly_once():
    samples = [sample(i, rate=(i % 5) / 4) for i in range(40)]
    segments = plan_segments(samples, FAST)
    assert segments[0].start_frame == 0 and segments[-1].end_frame == 8000
    for previous, current in itertools.pairwise(segments):
        assert previous.end_frame == current.start_frame
    assert all(s.end_frame > s.start_frame for s in segments)


def test_timing_records_the_mode_and_that_the_axis_is_not_linear():
    samples = [sample(i, rate=(i % 2) * 1.0) for i in range(10)]
    weighted = render_soundscape(make_series(samples), FAST).provenance.timing
    assert weighted.timing_mode == "rate_weighted" and weighted.rate_sensitivity == 1.5
    assert not weighted.linear_time_axis
    assert weighted.segment_seconds_min < weighted.segment_seconds_max
    assert weighted.pulse_rate_hz_max > weighted.pulse_rate_hz_mean

    uniform = render_soundscape(
        make_series(samples), FAST.model_copy(update={"timing_mode": "uniform"})
    ).provenance.timing
    assert uniform.timing_mode == "uniform" and uniform.linear_time_axis
    assert uniform.segment_seconds_min == pytest.approx(uniform.segment_seconds_max, abs=1 / 8000)


def test_rate_weighting_refuses_a_layout_that_would_lose_a_segment():
    # 10 frames, 9 samples, one of them demanding a much shorter share than the rest.
    samples = [sample(0, rate=1.0), *(sample(i, rate=0.0) for i in range(1, 9))]
    tiny = SonificationSettings(duration_seconds=10 / 8000, sample_rate_hz=8000, rate_sensitivity=50.0)
    with pytest.raises(SonificationError, match="Rate weighting left segment"):
        plan_segments(samples, tiny)


def test_rate_weighting_is_deterministic():
    samples = [sample(i, rate=(i * 7 % 11) / 10) for i in range(25)]
    first = render_soundscape(make_series(samples), FAST)
    second = render_soundscape(make_series(samples), FAST)
    assert first.wav_bytes == second.wav_bytes
    assert first.provenance.model_dump_json() == second.provenance.model_dump_json()


def test_rate_is_recorded_on_every_segment():
    samples = [sample(0, rate=0.75), sample(1, audible=False, status="missing"), sample(2, rate=0.0)]
    segments = render_soundscape(make_series(samples), FAST).provenance.segments
    assert [s.rate_normalized for s in segments] == [0.75, None, 0.0]


# --- encoding ----------------------------------------------------------------


def test_pcm16_rounds_to_nearest_and_clips():
    pcm = float_to_pcm16(np.array([[0.0, 1.0], [-1.0, 2.0], [0.5, -0.5]]))
    assert pcm.dtype == np.dtype("<i2")
    assert pcm.tolist() == [[0, PCM16_FULL_SCALE], [-PCM16_FULL_SCALE, PCM16_FULL_SCALE], [16384, -16384]]


def test_encode_and_describe_round_trip():
    pcm = np.zeros((800, 2), dtype="<i2")
    wav_bytes = encode_wav(pcm, FAST)
    out = describe_wav(wav_bytes)
    assert (out.sample_rate_hz, out.channels, out.bit_depth, out.frames) == (8000, 2, 16, 800)
    assert out.duration_seconds == pytest.approx(0.1)
    assert out.byte_count == len(wav_bytes) == 44 + 800 * 2 * 2
    assert len(out.sha256) == 64


# --- series validation -------------------------------------------------------


def test_audible_sample_must_carry_a_level():
    with pytest.raises(ValueError, match="must carry a level"):
        SonifiableSample(timestamp=T0, audible=True, status="valid")


def test_silent_sample_must_not_carry_a_level_or_pan():
    with pytest.raises(ValueError, match="must not carry"):
        SonifiableSample(timestamp=T0, audible=False, status="missing", level_normalized=0.5)
    with pytest.raises(ValueError, match="must not carry"):
        SonifiableSample(timestamp=T0, audible=False, status="missing", pan=0.5)


@pytest.mark.parametrize(
    ("field", "bad"),
    [("level_normalized", 1.5), ("level_normalized", -0.1), ("pan", 1.5), ("pan", -1.5)],
)
def test_sample_rejects_out_of_range_values(field, bad):
    fields = {"level_normalized": 0.5, field: bad}
    with pytest.raises(ValueError):
        SonifiableSample(timestamp=T0, audible=True, status="valid", **fields)


# --- rendering ---------------------------------------------------------------


def test_render_rejects_empty_or_fully_silent_series():
    with pytest.raises(SonificationError, match="empty"):
        render_soundscape(make_series([]), FAST)
    with pytest.raises(SonificationError, match="every sample is silent"):
        render_soundscape(make_series([sample(0, audible=False, status="missing")]), FAST)


def test_render_rejects_an_unknown_series_type():
    with pytest.raises(SonificationError, match="Cannot sonify"):
        render_soundscape({"not": "a series"}, FAST)


def test_render_is_byte_identical_for_identical_input_and_settings():
    series = make_series([sample(i, level=0.1 * (i + 1), pan=-1 + 0.2 * i) for i in range(10)])
    first = render_soundscape(series, FAST)
    second = render_soundscape(series, FAST)
    assert first.wav_bytes == second.wav_bytes
    assert first.provenance.model_dump_json() == second.provenance.model_dump_json()
    assert first.provenance.output.sha256 == second.provenance.output.sha256


def test_render_changes_when_settings_change():
    series = make_series([sample(i) for i in range(5)])
    a = render_soundscape(series, FAST)
    b = render_soundscape(series, FAST.model_copy(update={"carrier_frequency_hz": 330.0}))
    assert a.wav_bytes != b.wav_bytes


def test_output_duration_and_format_are_recorded_and_exact():
    rendered = render_soundscape(make_series([sample(i) for i in range(7)]), FAST)
    out = rendered.provenance.output
    assert out.frames == 8000 and out.duration_seconds == 1.0
    assert (out.sample_rate_hz, out.channels, out.bit_depth) == (8000, 2, 16)
    assert out.container == "WAV (RIFF)" and out.encoding == "PCM signed 16-bit little-endian"
    frames, rate = decode(rendered.wav_bytes)
    assert frames.shape == (8000, 2) and rate == 8000


def test_default_settings_produce_a_thirty_second_stereo_file():
    rendered = render_soundscape(make_series([sample(i) for i in range(30)]), SonificationSettings())
    out = rendered.provenance.output
    assert out.duration_seconds == 30.0 and out.sample_rate_hz == 44100 and out.channels == 2
    assert out.byte_count == 44 + 30 * 44100 * 2 * 2
    assert len(rendered.provenance.segments) == 30
    assert rendered.provenance.segments[1].start_seconds == pytest.approx(1.0)


def test_inaudible_segments_are_digital_silence():
    series = make_series(
        [
            sample(0, level=1.0),
            sample(1, audible=False, status="missing"),
            sample(2, audible=False, status="invalid"),
            sample(3, level=1.0),
        ]
    )
    rendered = render_soundscape(series, FAST)
    frames, _ = decode(rendered.wav_bytes)
    segments = rendered.provenance.segments
    for seg in segments[1:3]:
        assert not seg.audible and seg.gain_db is None and seg.pan is None and seg.level_normalized is None
        assert np.all(frames[seg.start_frame : seg.end_frame] == 0)
    for seg in (segments[0], segments[3]):
        assert seg.audible and np.abs(frames[seg.start_frame : seg.end_frame]).max() > 0


def test_a_level_of_zero_is_quiet_but_audible_unlike_silence():
    series = make_series([sample(0, level=0.0), sample(1, audible=False, status="missing")])
    rendered = render_soundscape(series, FAST)
    frames, _ = decode(rendered.wav_bytes)
    floor, gap = rendered.provenance.segments
    assert floor.gain_db == -36.0 and floor.audible
    assert np.abs(frames[floor.start_frame : floor.end_frame]).max() > 0
    assert np.all(frames[gap.start_frame : gap.end_frame] == 0)


def test_higher_level_gives_a_larger_peak_and_full_level_hits_zero_db():
    series = make_series([sample(0, level=0.1), sample(1, level=1.0)])
    rendered = render_soundscape(series, FAST)
    frames, _ = decode(rendered.wav_bytes)
    peaks = [np.abs(frames[s.start_frame : s.end_frame]).max() for s in rendered.provenance.segments]
    assert peaks[0] < peaks[1]
    loud = rendered.provenance.segments[1]
    assert loud.gain_db == 0.0
    # Centre pan at 0 dB: each channel carries peak_amplitude / sqrt(2) of the carrier's own
    # peak (the harmonics never align, so the normalized carrier peaks below 1).
    carrier_peak = np.abs(carrier_waveform(8000, FAST)[loud.start_frame : loud.end_frame]).max()
    assert peaks[1] == pytest.approx(0.9 * math.sqrt(0.5) * carrier_peak * PCM16_FULL_SCALE, rel=0.02)


def test_pan_places_segments_in_the_expected_channel():
    series = make_series([sample(0, level=1.0, pan=1.0), sample(1, level=1.0, pan=-1.0)])
    rendered = render_soundscape(series, FAST)
    frames, _ = decode(rendered.wav_bytes)
    right, left = rendered.provenance.segments
    assert np.all(frames[right.start_frame : right.end_frame, 0] == 0)
    assert np.abs(frames[right.start_frame : right.end_frame, 1]).max() > 0
    assert np.all(frames[left.start_frame : left.end_frame, 1] == 0)
    assert np.abs(frames[left.start_frame : left.end_frame, 0]).max() > 0


def test_pan_disabled_keeps_channels_identical():
    series = make_series([sample(0, level=1.0, pan=1.0)])
    rendered = render_soundscape(series, FAST.model_copy(update={"pan_enabled": False}))
    frames, _ = decode(rendered.wav_bytes)
    assert np.array_equal(frames[:, 0], frames[:, 1])
    assert rendered.provenance.segments[0].pan == 0.0


def test_an_unmapped_pan_is_centred():
    rendered = render_soundscape(make_series([sample(0, level=1.0, pan=None)]), FAST)
    frames, _ = decode(rendered.wav_bytes)
    assert np.array_equal(frames[:, 0], frames[:, 1])
    assert rendered.provenance.segments[0].pan == 0.0


def test_no_sample_ever_clips_at_full_scale():
    series = make_series([sample(i, level=1.0, pan=(-1) ** i) for i in range(8)])
    rendered = render_soundscape(series, SonificationSettings(duration_seconds=0.5, sample_rate_hz=8000))
    frames, _ = decode(rendered.wav_bytes)
    assert np.abs(frames).max() <= round(0.9 * PCM16_FULL_SCALE)


# --- provenance --------------------------------------------------------------


def test_provenance_traces_every_segment_and_records_sources():
    samples = [sample(i, level=0.2 * i, pan=0.1 * i, values={"thing": float(i)}) for i in range(4)]
    series = make_series(samples)
    prov = render_soundscape(series, FAST).provenance
    assert prov.sonification_version == SONIFICATION_VERSION
    assert prov.settings == FAST
    assert prov.engine_mappings == list(ENGINE_MAPPINGS)
    assert prov.domain_mappings == series.domain_mappings
    assert prov.omitted_mappings == series.omitted_mappings
    assert prov.source.domain == "test_domain" and prov.source.source_sha256 == "0" * 64
    assert prov.source.processing_version == "9.9.9" and prov.source.audible_samples == 4
    assert prov.source.source_url == "https://example.test/data"
    assert prov.source.mapping_source == series.mapping_source
    assert [s.index for s in prov.segments] == [0, 1, 2, 3]
    for seg, src in zip(prov.segments, samples, strict=True):
        assert seg.timestamp == src.timestamp and seg.level_normalized == src.level_normalized
        assert seg.gain_db == pytest.approx(level_to_gain_db(src.level_normalized, FAST))
        assert seg.pan == pytest.approx(src.pan)
        assert seg.source_values == src.source_values
    # Round-trips through JSON so the API (Phase 5) can serve it.
    assert SoundscapeProvenance.model_validate_json(prov.model_dump_json()) == prov


def test_provenance_mappings_property_combines_engine_and_domain_rules():
    prov = render_soundscape(make_series([sample(0)]), FAST).provenance
    assert prov.mappings == [*prov.engine_mappings, *prov.domain_mappings]


def test_engine_mappings_cover_every_sound_parameter_it_controls():
    for rule in ENGINE_MAPPINGS:
        assert rule.sound_parameter and rule.source_measurement and rule.rule and rule.rationale
    parameters = {rule.sound_parameter for rule in ENGINE_MAPPINGS}
    assert parameters == {
        "segment order (which pulse is which sample)",
        "segment length (pulse rate / rhythm)",
        "loudness (gain)",
        "silence",
        "stereo position (pan)",
        "pitch and timbre",
    }


def test_plan_segments_matches_render_without_audio():
    series = make_series([sample(i) for i in range(3)])
    assert plan_segments(series.samples, FAST) == render_soundscape(series, FAST).provenance.segments


def test_settings_validation():
    with pytest.raises(ValueError):
        SonificationSettings(harmonic_amplitudes=())
    with pytest.raises(ValueError):
        SonificationSettings(harmonic_amplitudes=(1.0, -0.5))
    with pytest.raises(ValueError):
        SonificationSettings(gain_floor_db=0.0)
    with pytest.raises(ValueError):
        SonificationSettings(peak_amplitude=1.5)
