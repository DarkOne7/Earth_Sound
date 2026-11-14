"""Ocean-current domain mapping tests: speed -> loudness, direction -> pan."""

from __future__ import annotations

import io
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
from app.schemas.soundscape import SonificationSettings
from app.services.current_sonification import (
    AUDIBLE_STATUSES,
    DOMAIN_MAPPINGS,
    OMITTED_MAPPINGS,
    current_series_to_sonifiable,
    direction_to_pan,
)
from app.services.sonification import (
    PCM16_FULL_SCALE,
    carrier_waveform,
    render_soundscape,
)

T0 = datetime(2020, 1, 1, tzinfo=UTC)
FAST = SonificationSettings(duration_seconds=1.0, sample_rate_hz=8000)


def reference(quantity="current speed", minimum=0.0, maximum=2.5):
    return NormalizationReference(
        quantity=quantity, minimum=minimum, maximum=maximum, units="m s-1", source="test", url="https://example.test"
    )


def sample(i, *, speed=None, direction=None, status=SampleStatus.VALID, flags=()):
    audible = status in AUDIBLE_STATUSES
    directional = status is SampleStatus.VALID
    return CurrentSample(
        timestamp=T0 + timedelta(days=i),
        u=0.0 if audible else None,
        v=0.0 if audible else None,
        speed=speed if audible else None,
        direction_deg=direction if directional else None,
        speed_normalized=(speed / 2.5 if speed is not None else None) if audible else None,
        direction_normalized=(direction / 360 if direction is not None else None) if directional else None,
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
            selection=SelectionParameters(
                mode="point", start=T0, end=T0 + timedelta(days=30), latitude=1.0, longitude=-151.0
            ),
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


# --- direction -> pan -------------------------------------------------------


@pytest.mark.parametrize(
    ("direction", "expected_pan"),
    [
        (0.0, 0.0),
        (90.0, 1.0),
        (180.0, 0.0),
        (270.0, -1.0),
        (45.0, math.sin(math.radians(45))),
        (359.999999, 0.0),
    ],
)
def test_east_west_component_pans(direction, expected_pan):
    assert direction_to_pan(direction) == pytest.approx(expected_pan, abs=1e-6)


def test_north_and_south_are_centred_and_indistinguishable():
    assert direction_to_pan(0.0) == direction_to_pan(180.0) == 0.0


def test_undefined_direction_stays_unmapped():
    assert direction_to_pan(None) is None


def test_pan_wraps_continuously_around_north():
    assert abs(direction_to_pan(359.0) - direction_to_pan(1.0)) < 0.04


@pytest.mark.parametrize("bad", [math.nan, math.inf])
def test_non_finite_direction_is_rejected(bad):
    with pytest.raises(ValueError, match="finite"):
        direction_to_pan(bad)


# --- adapter ----------------------------------------------------------------


def test_adapter_carries_speed_to_level_and_direction_to_pan():
    series = make_series([sample(0, speed=1.25, direction=90.0)])
    sonifiable = current_series_to_sonifiable(series)
    one = sonifiable.samples[0]
    assert sonifiable.provenance.domain == "ocean_currents"
    assert sonifiable.provenance.dataset == "OSCAR_L4_OC_FINAL_V2.0"
    assert sonifiable.provenance.selection["mode"] == "point"
    assert one.audible and one.status == "valid"
    assert one.level_normalized == pytest.approx(0.5)
    assert one.pan == pytest.approx(1.0)
    assert one.source_values["speed_m_s"] == 1.25
    assert one.source_values["direction_deg"] == 90.0
    assert sonifiable.mapping_source.level_reference == series.metadata.speed_normalization


def test_adapter_silences_missing_and_invalid_and_centres_zero_speed():
    series = make_series(
        [
            sample(0, status=SampleStatus.MISSING),
            sample(1, status=SampleStatus.INVALID),
            sample(2, speed=0.0, status=SampleStatus.UNDEFINED_DIRECTION),
        ]
    )
    missing, invalid, zero = current_series_to_sonifiable(series).samples
    for silent in (missing, invalid):
        assert not silent.audible and silent.level_normalized is None and silent.pan is None
    assert zero.audible and zero.level_normalized == 0.0 and zero.pan is None
    assert zero.status == "undefined_direction"


def test_adapter_preserves_clipping_flags():
    series = make_series([sample(0, speed=2.5, direction=0.0, flags=(SampleFlag.CLIPPED_HIGH,))])
    assert current_series_to_sonifiable(series).samples[0].flags == ["clipped_high"]


def test_adapter_declares_its_mappings_and_omissions():
    sonifiable = current_series_to_sonifiable(make_series([sample(0, speed=1.0, direction=0.0)]))
    assert sonifiable.domain_mappings == list(DOMAIN_MAPPINGS)
    assert sonifiable.omitted_mappings == list(OMITTED_MAPPINGS)
    omitted = {rule.sound_parameter for rule in OMITTED_MAPPINGS}
    assert any("temperature" in parameter for parameter in omitted)
    assert any("north-south" in parameter for parameter in omitted)


# --- end-to-end -------------------------------------------------------------


def test_processed_current_series_renders_directly():
    series = make_series([sample(i, speed=0.5 + 0.1 * i, direction=30.0 * i) for i in range(5)])
    rendered = render_soundscape(series, FAST)
    assert rendered.provenance.source.domain == "ocean_currents"
    assert rendered.provenance.output.duration_seconds == 1.0
    assert len(rendered.provenance.segments) == 5


def test_eastward_segment_is_right_only_and_westward_left_only():
    series = make_series([sample(0, speed=1.0, direction=90.0), sample(1, speed=1.0, direction=270.0)])
    rendered = render_soundscape(series, FAST)
    frames, _ = decode(rendered.wav_bytes)
    east, west = rendered.provenance.segments
    assert np.all(frames[east.start_frame : east.end_frame, 0] == 0)
    assert np.abs(frames[east.start_frame : east.end_frame, 1]).max() > 0
    assert np.all(frames[west.start_frame : west.end_frame, 1] == 0)
    assert np.abs(frames[west.start_frame : west.end_frame, 0]).max() > 0


def test_missing_and_invalid_samples_are_digital_silence():
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
        assert np.all(frames[seg.start_frame : seg.end_frame] == 0)
    for seg in (segments[0], segments[3]):
        assert np.abs(frames[seg.start_frame : seg.end_frame]).max() > 0


def test_zero_speed_is_quiet_but_audible_unlike_silence():
    series = make_series(
        [
            sample(0, speed=0.0, status=SampleStatus.UNDEFINED_DIRECTION),
            sample(1, status=SampleStatus.MISSING),
        ]
    )
    rendered = render_soundscape(series, FAST)
    frames, _ = decode(rendered.wav_bytes)
    zero_speed, gap = rendered.provenance.segments
    assert zero_speed.gain_db == -36.0 and zero_speed.pan == 0.0
    assert np.abs(frames[zero_speed.start_frame : zero_speed.end_frame]).max() > 0
    assert np.all(frames[gap.start_frame : gap.end_frame] == 0)


def test_clipped_speed_reaches_full_gain():
    series = make_series(
        [
            sample(0, speed=0.25, direction=0.0),
            sample(1, speed=2.5, direction=0.0),
            sample(2, speed=2.5, direction=0.0, flags=(SampleFlag.CLIPPED_HIGH,)),
        ]
    )
    rendered = render_soundscape(series, FAST)
    frames, _ = decode(rendered.wav_bytes)
    segments = rendered.provenance.segments
    peaks = [np.abs(frames[s.start_frame : s.end_frame]).max() for s in segments]
    assert peaks[0] < peaks[1]
    assert segments[1].gain_db == 0.0 == segments[2].gain_db
    carrier_peak = np.abs(carrier_waveform(8000, FAST)[segments[1].start_frame : segments[1].end_frame]).max()
    assert peaks[1] == pytest.approx(0.9 * math.sqrt(0.5) * carrier_peak * PCM16_FULL_SCALE, rel=0.02)
    assert segments[2].flags == ["clipped_high"]


def test_ocean_render_is_deterministic():
    series = make_series([sample(i, speed=0.1 * (i + 1), direction=30.0 * i) for i in range(10)])
    first = render_soundscape(series, FAST)
    second = render_soundscape(series, FAST)
    assert first.wav_bytes == second.wav_bytes
    assert first.provenance.model_dump_json() == second.provenance.model_dump_json()
