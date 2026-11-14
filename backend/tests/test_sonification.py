from __future__ import annotations

import io
import itertools
import math
import wave
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest
from app.schemas.current_series import (
    CurrentSample,
    NormalizationReference,
    ProcessedCurrentSeries,
    SampleFlag,
    SampleStatus,
    SelectionParameters,
    SeriesMetadata,
    SeriesSummary,
)
from app.schemas.soundscape import SonificationSettings, SoundscapeProvenance
from app.services.sonification import (
    AUDIBLE_STATUSES,
    MAPPINGS,
    OMITTED_MAPPINGS,
    PCM16_FULL_SCALE,
    SONIFICATION_VERSION,
    SonificationError,
    carrier_waveform,
    describe_wav,
    direction_to_pan,
    encode_wav,
    float_to_pcm16,
    gain_db_to_amplitude,
    pan_to_channel_gains,
    plan_segments,
    render_soundscape,
    segment_boundaries,
    segment_envelope,
    speed_to_gain_db,
)

T0 = datetime(2020, 1, 1, tzinfo=UTC)
# Short, low-rate settings keep the tests fast; the defaults are tested separately.
FAST = SonificationSettings(duration_seconds=1.0, sample_rate_hz=8000)


def reference(quantity="current speed", minimum=0.0, maximum=2.5):
    return NormalizationReference(
        quantity=quantity, minimum=minimum, maximum=maximum, units="m s-1", source="test", url="https://example.test"
    )


def sample(i, *, speed=None, direction=None, status=SampleStatus.VALID, flags=()):
    audible = status in AUDIBLE_STATUSES
    return CurrentSample(
        timestamp=T0 + timedelta(days=i),
        u=0.0 if audible else None,
        v=0.0 if audible else None,
        speed=speed if audible else None,
        direction_deg=direction if status is SampleStatus.VALID else None,
        speed_normalized=(speed / 2.5 if speed is not None else None) if audible else None,
        direction_normalized=(direction / 360 if direction is not None else None) if status is SampleStatus.VALID else None,
        status=status,
        flags=list(flags),
    )


def make_series(samples) -> ProcessedCurrentSeries:
    valid = sum(s.status is SampleStatus.VALID for s in samples)
    undefined = sum(s.status is SampleStatus.UNDEFINED_DIRECTION for s in samples)
    return ProcessedCurrentSeries(
        metadata=SeriesMetadata(
            dataset="OSCAR_L4_OC_FINAL_V2.0",
            dataset_version="2.0",
            source_file="synthetic.nc",
            source_sha256="0" * 64,
            variables={"u": "u", "v": "v"},
            input_units={"u": "m s-1", "v": "m s-1"},
            units="m s-1",
            selection=SelectionParameters(mode="point", start=T0, end=T0 + timedelta(days=30), latitude=1.0, longitude=-151.0),
            direction_convention="toward, clockwise from north",
            direction_normalization="deg/360",
            speed_normalization=reference(),
            component_valid_range=reference("velocity component (u, v)", -3.0, 3.0),
            numeric_tolerance=1e-9,
            minimum_usable_samples=1,
            processing_version="1.0.2",
        ),
        samples=list(samples),
        summary=SeriesSummary(
            total=len(samples),
            valid=valid,
            undefined_direction=undefined,
            missing=sum(s.status is SampleStatus.MISSING for s in samples),
            invalid=sum(s.status is SampleStatus.INVALID for s in samples),
            clipped_low=0,
            clipped_high=sum(SampleFlag.CLIPPED_HIGH in s.flags for s in samples),
            usable=valid + undefined,
        ),
    )


def decode(wav_bytes: bytes) -> tuple[np.ndarray, int]:
    with wave.open(io.BytesIO(wav_bytes), "rb") as reader:
        frames = np.frombuffer(reader.readframes(reader.getnframes()), dtype="<i2")
        return frames.reshape(-1, reader.getnchannels()), reader.getframerate()


# --- speed -> loudness -------------------------------------------------------


@pytest.mark.parametrize(
    ("speed_normalized", "expected_db"),
    [(0.0, -36.0), (0.5, -18.0), (1.0, 0.0), (0.25, -27.0)],
)
def test_speed_to_gain_is_linear_in_db(speed_normalized, expected_db):
    assert speed_to_gain_db(speed_normalized, FAST) == pytest.approx(expected_db)


def test_gain_to_amplitude_matches_decibel_definition():
    assert gain_db_to_amplitude(0.0, FAST) == pytest.approx(0.9)
    assert gain_db_to_amplitude(-20.0, FAST) == pytest.approx(0.09)
    assert gain_db_to_amplitude(-36.0, FAST) == pytest.approx(0.9 * 10 ** (-36 / 20))


def test_loudness_increases_monotonically_with_speed():
    gains = [gain_db_to_amplitude(speed_to_gain_db(s, FAST), FAST) for s in np.linspace(0, 1, 11)]
    assert all(a < b for a, b in itertools.pairwise(gains))


@pytest.mark.parametrize("bad", [-0.01, 1.01, math.nan, math.inf])
def test_speed_outside_unit_range_is_rejected(bad):
    with pytest.raises(SonificationError):
        speed_to_gain_db(bad, FAST)


# --- direction -> pan -------------------------------------------------------


@pytest.mark.parametrize(
    ("direction", "expected_pan"),
    [(0.0, 0.0), (90.0, 1.0), (180.0, 0.0), (270.0, -1.0), (45.0, math.sin(math.radians(45))), (359.999999, 0.0)],
)
def test_east_west_component_pans(direction, expected_pan):
    assert direction_to_pan(direction, FAST) == pytest.approx(expected_pan, abs=1e-6)


def test_eastward_flow_is_right_and_westward_is_left():
    left_e, right_e = pan_to_channel_gains(direction_to_pan(90.0, FAST))
    left_w, right_w = pan_to_channel_gains(direction_to_pan(270.0, FAST))
    assert right_e == pytest.approx(1.0) and left_e == pytest.approx(0.0, abs=1e-12)
    assert left_w == pytest.approx(1.0) and right_w == pytest.approx(0.0, abs=1e-12)


def test_north_and_south_are_centred_and_indistinguishable():
    assert direction_to_pan(0.0, FAST) == direction_to_pan(180.0, FAST) == 0.0


def test_undefined_direction_and_disabled_mapping_are_centred():
    assert direction_to_pan(None, FAST) == 0.0
    off = FAST.model_copy(update={"direction_mapping": "none"})
    assert direction_to_pan(90.0, off) == 0.0


def test_pan_law_is_constant_power():
    for pan in np.linspace(-1, 1, 21):
        left, right = pan_to_channel_gains(float(pan))
        assert left**2 + right**2 == pytest.approx(1.0)
    assert pan_to_channel_gains(0.0) == pytest.approx((math.sqrt(0.5), math.sqrt(0.5)))


def test_pan_wraps_continuously_around_north():
    assert direction_to_pan(359.0, FAST) == pytest.approx(direction_to_pan(-1.0 % 360, FAST))
    assert abs(direction_to_pan(359.0, FAST) - direction_to_pan(1.0, FAST)) < 0.04


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


# --- rendering ---------------------------------------------------------------


def test_render_rejects_empty_or_fully_silent_series():
    with pytest.raises(SonificationError, match="empty"):
        render_soundscape(make_series([]), FAST)
    with pytest.raises(SonificationError, match="no usable"):
        render_soundscape(make_series([sample(0, status=SampleStatus.MISSING)]), FAST)


def test_render_is_byte_identical_for_identical_input_and_settings():
    series = make_series([sample(i, speed=0.1 * (i + 1), direction=30.0 * i) for i in range(10)])
    first = render_soundscape(series, FAST)
    second = render_soundscape(series, FAST)
    assert first.wav_bytes == second.wav_bytes
    assert first.provenance.model_dump_json() == second.provenance.model_dump_json()
    assert first.provenance.output.sha256 == second.provenance.output.sha256


def test_render_changes_when_settings_change():
    series = make_series([sample(i, speed=0.5, direction=90.0) for i in range(5)])
    a = render_soundscape(series, FAST)
    b = render_soundscape(series, FAST.model_copy(update={"carrier_frequency_hz": 330.0}))
    assert a.wav_bytes != b.wav_bytes


def test_output_duration_and_format_are_recorded_and_exact():
    series = make_series([sample(i, speed=0.5, direction=90.0) for i in range(7)])
    rendered = render_soundscape(series, FAST)
    out = rendered.provenance.output
    assert out.frames == 8000 and out.duration_seconds == 1.0
    assert (out.sample_rate_hz, out.channels, out.bit_depth) == (8000, 2, 16)
    assert out.container == "WAV (RIFF)" and out.encoding == "PCM signed 16-bit little-endian"
    frames, rate = decode(rendered.wav_bytes)
    assert frames.shape == (8000, 2) and rate == 8000


def test_default_settings_produce_a_thirty_second_stereo_file():
    series = make_series([sample(i, speed=0.3, direction=45.0) for i in range(30)])
    rendered = render_soundscape(series, SonificationSettings())
    out = rendered.provenance.output
    assert out.duration_seconds == 30.0 and out.sample_rate_hz == 44100 and out.channels == 2
    assert out.byte_count == 44 + 30 * 44100 * 2 * 2
    assert len(rendered.provenance.segments) == 30
    assert rendered.provenance.segments[1].start_seconds == pytest.approx(1.0)


def test_missing_and_invalid_segments_are_digital_silence():
    series = make_series(
        [
            sample(0, speed=1.0, direction=90.0),
            sample(1, status=SampleStatus.MISSING),
            sample(2, status=SampleStatus.INVALID),
            sample(3, speed=1.0, direction=90.0),
        ]
    )
    rendered = render_soundscape(series, FAST)
    frames, _ = decode(rendered.wav_bytes)
    segments = rendered.provenance.segments
    for seg in segments[1:3]:
        assert not seg.audible and seg.gain_db is None and seg.pan is None
        assert np.all(frames[seg.start_frame : seg.end_frame] == 0)
    for seg in (segments[0], segments[3]):
        assert seg.audible and np.abs(frames[seg.start_frame : seg.end_frame]).max() > 0


def test_zero_speed_is_quiet_but_audible_unlike_silence():
    series = make_series(
        [sample(0, speed=0.0, status=SampleStatus.UNDEFINED_DIRECTION), sample(1, status=SampleStatus.MISSING)]
    )
    rendered = render_soundscape(series, FAST)
    frames, _ = decode(rendered.wav_bytes)
    zero_speed, gap = rendered.provenance.segments
    assert zero_speed.gain_db == -36.0 and zero_speed.pan == 0.0
    assert np.abs(frames[zero_speed.start_frame : zero_speed.end_frame]).max() > 0
    assert np.all(frames[gap.start_frame : gap.end_frame] == 0)


def test_louder_speed_gives_larger_peak_and_clipped_speed_hits_full_gain():
    series = make_series(
        [
            sample(0, speed=0.25, direction=0.0),
            sample(1, speed=2.5, direction=0.0),
            sample(2, speed=2.5, direction=0.0, flags=(SampleFlag.CLIPPED_HIGH,)),
        ]
    )
    rendered = render_soundscape(series, FAST)
    frames, _ = decode(rendered.wav_bytes)
    peaks = [np.abs(frames[s.start_frame : s.end_frame]).max() for s in rendered.provenance.segments]
    assert peaks[0] < peaks[1]
    assert rendered.provenance.segments[1].gain_db == 0.0 == rendered.provenance.segments[2].gain_db
    # Centre pan at 0 dB: each channel carries peak_amplitude / sqrt(2) of the carrier's own peak
    # (the harmonics never peak together, so the normalized carrier peaks below 1).
    seg = rendered.provenance.segments[1]
    carrier_peak = np.abs(carrier_waveform(8000, FAST)[seg.start_frame : seg.end_frame]).max()
    expected_peak = 0.9 * math.sqrt(0.5) * carrier_peak * PCM16_FULL_SCALE
    assert peaks[1] == pytest.approx(expected_peak, rel=0.02)
    assert rendered.provenance.segments[2].flags == [SampleFlag.CLIPPED_HIGH]


def test_eastward_segment_is_right_only_and_westward_left_only():
    series = make_series([sample(0, speed=1.0, direction=90.0), sample(1, speed=1.0, direction=270.0)])
    rendered = render_soundscape(series, FAST)
    frames, _ = decode(rendered.wav_bytes)
    east, west = rendered.provenance.segments
    assert np.all(frames[east.start_frame : east.end_frame, 0] == 0)
    assert np.abs(frames[east.start_frame : east.end_frame, 1]).max() > 0
    assert np.all(frames[west.start_frame : west.end_frame, 1] == 0)
    assert np.abs(frames[west.start_frame : west.end_frame, 0]).max() > 0


def test_direction_mapping_none_keeps_channels_identical():
    series = make_series([sample(0, speed=1.0, direction=90.0)])
    rendered = render_soundscape(series, FAST.model_copy(update={"direction_mapping": "none"}))
    frames, _ = decode(rendered.wav_bytes)
    assert np.array_equal(frames[:, 0], frames[:, 1])
    assert rendered.provenance.segments[0].pan == 0.0


def test_no_sample_ever_clips_at_full_scale():
    series = make_series([sample(i, speed=2.5, direction=90.0 * i) for i in range(8)])
    rendered = render_soundscape(series, SonificationSettings(duration_seconds=0.5, sample_rate_hz=8000))
    frames, _ = decode(rendered.wav_bytes)
    assert np.abs(frames).max() <= round(0.9 * PCM16_FULL_SCALE)


def test_provenance_traces_every_segment_and_records_sources():
    series = make_series([sample(i, speed=0.5 + 0.1 * i, direction=10.0 * i) for i in range(4)])
    rendered = render_soundscape(series, FAST)
    prov = rendered.provenance
    assert prov.sonification_version == SONIFICATION_VERSION
    assert prov.settings == FAST
    assert prov.mappings == list(MAPPINGS) and prov.omitted_mappings == list(OMITTED_MAPPINGS)
    assert prov.source.dataset == "OSCAR_L4_OC_FINAL_V2.0" and prov.source.source_sha256 == "0" * 64
    assert prov.source.processing_version == "1.0.2" and prov.source.usable_samples == 4
    assert [s.index for s in prov.segments] == [0, 1, 2, 3]
    for seg, src in zip(prov.segments, series.samples):
        assert seg.timestamp == src.timestamp and seg.speed_normalized == src.speed_normalized
        assert seg.gain_db == pytest.approx(speed_to_gain_db(src.speed_normalized, FAST))
        assert seg.pan == pytest.approx(direction_to_pan(src.direction_deg, FAST))
    # Round-trips through JSON so the API (Phase 5) can serve it.
    assert SoundscapeProvenance.model_validate_json(prov.model_dump_json()) == prov


def test_every_mapped_sound_parameter_names_a_source_measurement():
    for rule in (*MAPPINGS, *OMITTED_MAPPINGS):
        assert rule.sound_parameter and rule.source_measurement and rule.rule and rule.rationale
    parameters = {rule.sound_parameter for rule in MAPPINGS}
    assert {"loudness (gain)", "stereo position (pan)", "segment timing (rhythm)", "silence", "pitch and timbre"} <= parameters
    assert any("temperature" in rule.sound_parameter for rule in OMITTED_MAPPINGS)


def test_plan_segments_matches_render_without_audio():
    series = make_series([sample(i, speed=0.4, direction=120.0) for i in range(3)])
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
