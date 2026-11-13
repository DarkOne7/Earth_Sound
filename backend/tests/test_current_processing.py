from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest
from app.schemas.current_series import (
    ExtractedSeries,
    ProcessedCurrentSeries,
    SampleFlag,
    SampleStatus,
    SelectionParameters,
)
from app.services.current_processing import (
    CurrentProcessingError,
    InsufficientDataError,
    UnitConversionError,
    current_direction,
    current_speed,
    normalize_direction,
    normalize_speed,
    process_current_series,
    to_meters_per_second,
)
from app.services.reference_ranges import (
    NUMERIC_TOLERANCE,
    SPEED_NORMALIZATION_RANGE,
)

TOL = NUMERIC_TOLERANCE
T0 = datetime(2020, 1, 1, tzinfo=UTC)
SPEED_MAX = SPEED_NORMALIZATION_RANGE.maximum


def make_series(u, v, *, timestamps=None, units="m s-1", forced_status=None) -> ExtractedSeries:
    u = np.asarray(u, dtype=float)
    v = np.asarray(v, dtype=float)
    if timestamps is None:
        timestamps = tuple(T0 + timedelta(days=i) for i in range(len(u)))
    return ExtractedSeries(
        timestamps=tuple(timestamps),
        u=u,
        v=v,
        u_units=units,
        v_units=units,
        u_variable="u",
        v_variable="v",
        selection=SelectionParameters(mode="point", start=T0, end=T0 + timedelta(days=30), latitude=1.0, longitude=-151.0),
        source={"dataset": "OSCAR_L4_OC_FINAL_V2.0", "dataset_version": "2.0", "source_file": "synthetic.nc"},
        forced_status=forced_status,
    )


# --- speed -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("u", "v", "expected"),
    [
        (3.0, 4.0, 5.0),  # 3-4-5 triangle
        (-3.0, -4.0, 5.0),
        (0.6, 0.8, 1.0),
        (1.0, 1.0, 1.4142135623730951),  # sqrt(2) written out
        (0.0, 0.0, 0.0),
        (0.0, -2.0, 2.0),
    ],
)
def test_speed_known_values(u, v, expected):
    assert float(current_speed(u, v)) == pytest.approx(expected, abs=TOL)


# --- direction -------------------------------------------------------------


@pytest.mark.parametrize(
    ("u", "v", "expected_deg"),
    [
        (0.0, 1.0, 0.0),  # flowing toward north
        (1.0, 0.0, 90.0),  # toward east
        (0.0, -1.0, 180.0),  # toward south
        (-1.0, 0.0, 270.0),  # toward west
        (1.0, 1.0, 45.0),  # toward north-east
        (1.0, -1.0, 135.0),  # toward south-east
        (-1.0, -1.0, 225.0),  # toward south-west
        (-1.0, 1.0, 315.0),  # toward north-west
        (0.5, 0.5 * math.sqrt(3.0), 30.0),  # tan(30 deg) = u / v
    ],
)
def test_direction_known_values(u, v, expected_deg):
    assert float(current_direction(u, v)) == pytest.approx(expected_deg, abs=1e-6)


def test_direction_convention_is_toward_not_from():
    # An eastward current (u > 0) flows TOWARD 90 deg. A "coming from" convention
    # would report 270 deg, so this assertion fails if the meaning is swapped.
    assert float(current_direction(0.5, 0.0)) == pytest.approx(90.0, abs=TOL)
    # A northward current flows toward 0 deg, not 180 deg.
    assert float(current_direction(0.0, 0.5)) == pytest.approx(0.0, abs=TOL)


def test_direction_range_is_half_open_and_wraps():
    # Tiny negative u just west of north must wrap below 360, never equal it.
    value = float(current_direction(-1e-17, 1.0))
    assert 0.0 <= value < 360.0
    rng = np.random.default_rng(0)
    u = rng.uniform(-3, 3, 1000)
    v = rng.uniform(-3, 3, 1000)
    directions = current_direction(u, v)
    assert np.all((directions >= 0.0) & (directions < 360.0))


def test_zero_speed_has_undefined_direction():
    assert math.isnan(float(current_direction(0.0, 0.0)))
    assert math.isnan(float(current_direction(TOL / 2, 0.0)))


# --- normalization ---------------------------------------------------------


def test_normalize_speed_exact_bounds():
    assert normalize_speed(0.0) == (0.0, None)
    assert normalize_speed(SPEED_MAX) == (1.0, None)


def test_normalize_speed_just_inside():
    value, flag = normalize_speed(1.25)
    assert value == pytest.approx(0.5, abs=TOL)  # 1.25 / 2.5
    assert flag is None
    value, flag = normalize_speed(SPEED_MAX - 1e-6)
    assert 0.0 < value < 1.0 and flag is None


def test_normalize_speed_clips_and_flags():
    assert normalize_speed(SPEED_MAX + 0.1) == (1.0, SampleFlag.CLIPPED_HIGH)
    assert normalize_speed(-0.1) == (0.0, SampleFlag.CLIPPED_LOW)


def test_normalize_speed_rejects_non_finite():
    with pytest.raises(CurrentProcessingError):
        normalize_speed(float("nan"))


def test_normalize_direction_wraps():
    assert normalize_direction(0.0) == 0.0
    assert normalize_direction(90.0) == pytest.approx(0.25, abs=TOL)
    assert normalize_direction(360.0) == 0.0
    assert normalize_direction(-90.0) == pytest.approx(0.75, abs=TOL)
    assert 0.0 <= normalize_direction(-1e-17) < 1.0


def test_normalization_is_deterministic():
    series = make_series([0.3, 2.9, -1.0] * 4, [0.4, 0.0, 1.0] * 4)
    first = process_current_series(series).model_dump_json()
    second = process_current_series(series).model_dump_json()
    assert first == second


def test_normalization_does_not_depend_on_sample_range():
    # The same speed must normalize identically whatever else is in the series.
    small = process_current_series(make_series([0.6] * 10, [0.8] * 10))
    mixed = process_current_series(make_series([0.6] + [0.1] * 9, [0.8] + [0.1] * 9))
    assert small.samples[0].speed_normalized == mixed.samples[0].speed_normalized == pytest.approx(0.4, abs=TOL)


# --- units -----------------------------------------------------------------


def test_unit_conversion_from_cm_per_second():
    np.testing.assert_allclose(to_meters_per_second(np.array([100.0, -50.0]), "cm s-1"), [1.0, -0.5], atol=TOL)


def test_unknown_units_rejected():
    with pytest.raises(UnitConversionError):
        to_meters_per_second(np.array([1.0]), "knots")
    with pytest.raises(UnitConversionError):
        to_meters_per_second(np.array([1.0]), None)


def test_processing_converts_units_before_speed():
    processed = process_current_series(make_series([30.0] * 10, [40.0] * 10, units="cm/s"))
    assert processed.samples[0].speed == pytest.approx(0.5, abs=TOL)
    assert processed.metadata.units == "m s-1"
    assert processed.metadata.input_units == {"u": "cm/s", "v": "cm/s"}


# --- validation ------------------------------------------------------------


def test_missing_and_invalid_values_are_flagged_not_filled():
    u = [0.1, np.nan, -999.0, np.inf, 5.0, 0.1, 0.0, 0.2, 0.2, 0.2, 0.2, 0.2, 0.2]
    v = [0.1, 0.1, 0.1, 0.1, 0.1, -np.inf, 0.0, 0.2, 0.2, 0.2, 0.2, 0.2, 0.2]
    processed = process_current_series(make_series(u, v), minimum_usable_samples=1)
    statuses = [s.status for s in processed.samples]
    assert statuses[:7] == [
        SampleStatus.VALID,
        SampleStatus.MISSING,  # NaN
        SampleStatus.MISSING,  # documented fill value
        SampleStatus.INVALID,  # +inf
        SampleStatus.INVALID,  # outside documented [-3, 3]
        SampleStatus.INVALID,  # -inf in v
        SampleStatus.UNDEFINED_DIRECTION,  # zero speed
    ]
    for sample in processed.samples[1:6]:
        assert sample.speed is None and sample.speed_normalized is None and sample.u is None
    zero = processed.samples[6]
    assert zero.speed == 0.0 and zero.speed_normalized == 0.0 and zero.direction_deg is None
    summary = processed.summary
    assert (summary.valid, summary.missing, summary.invalid, summary.undefined_direction) == (7, 2, 3, 1)
    assert summary.usable == 8 and summary.total == 13


def test_one_missing_component_makes_sample_missing():
    processed = process_current_series(make_series([np.nan] + [0.1] * 10, [0.5] + [0.1] * 10))
    assert processed.samples[0].status is SampleStatus.MISSING
    # Missing wins over invalid when the other component is out of range.
    processed = process_current_series(make_series([np.nan] + [0.1] * 10, [9.0] + [0.1] * 10))
    assert processed.samples[0].status is SampleStatus.MISSING


def test_component_valid_range_bounds_are_inclusive():
    processed = process_current_series(make_series([3.0, -3.0] + [0.1] * 9, [0.0, 3.0] + [0.1] * 9))
    assert processed.samples[0].status is SampleStatus.VALID
    assert processed.samples[1].status is SampleStatus.VALID
    # speed sqrt(18) ~ 4.24 > 2.5 -> clipped high, still valid
    assert processed.samples[1].flags == [SampleFlag.CLIPPED_HIGH]
    assert processed.summary.clipped_high == 2


def test_all_missing_series_is_insufficient():
    with pytest.raises(InsufficientDataError, match="0 usable") as info:
        process_current_series(make_series([np.nan] * 12, [np.nan] * 12))
    assert info.value.series.summary.missing == 12


def test_too_few_usable_samples_raises():
    u = [0.1] * 9 + [np.nan] * 5
    with pytest.raises(InsufficientDataError, match="9 usable"):
        process_current_series(make_series(u, [0.1] * 14), minimum_usable_samples=10)
    # Exactly at the minimum is accepted.
    processed = process_current_series(make_series(u, [0.1] * 14), minimum_usable_samples=9)
    assert processed.summary.usable == 9


def test_forced_status_from_extraction_is_respected():
    forced = (SampleStatus.INVALID,) + (None,) * 10
    processed = process_current_series(make_series([0.1] * 11, [0.1] * 11, forced_status=forced))
    assert processed.samples[0].status is SampleStatus.INVALID
    assert processed.samples[0].speed is None


# --- timestamps ------------------------------------------------------------


def test_timestamps_preserved_in_order():
    stamps = tuple(T0 + timedelta(hours=6 * i) for i in range(10))
    processed = process_current_series(make_series([0.1] * 10, [0.1] * 10, timestamps=stamps))
    assert [s.timestamp for s in processed.samples] == list(stamps)


def test_duplicate_timestamps_rejected():
    stamps = [T0 + timedelta(days=i) for i in range(10)]
    stamps[5] = stamps[4]
    with pytest.raises(CurrentProcessingError, match="Duplicate"):
        process_current_series(make_series([0.1] * 10, [0.1] * 10, timestamps=stamps))


def test_out_of_order_timestamps_rejected():
    stamps = [T0 + timedelta(days=i) for i in range(10)]
    stamps[3], stamps[4] = stamps[4], stamps[3]
    with pytest.raises(CurrentProcessingError, match="out of order"):
        process_current_series(make_series([0.1] * 10, [0.1] * 10, timestamps=stamps))


def test_mismatched_lengths_rejected():
    series = make_series([0.1] * 10, [0.1] * 10, timestamps=[T0 + timedelta(days=i) for i in range(9)])
    with pytest.raises(CurrentProcessingError, match="must both be 1-D"):
        process_current_series(series)


# --- schema ----------------------------------------------------------------


def test_processed_series_is_json_serializable_and_round_trips():
    processed = process_current_series(make_series([0.3, np.nan] + [0.1] * 10, [0.4, 0.1] + [0.1] * 10))
    payload = json.loads(processed.model_dump_json())
    assert payload["samples"][0]["status"] == "valid"
    assert payload["samples"][1]["speed"] is None
    assert payload["metadata"]["speed_normalization"]["url"].startswith("https://")
    assert "toward" in payload["metadata"]["direction_convention"]
    assert ProcessedCurrentSeries.model_validate(payload) == processed
