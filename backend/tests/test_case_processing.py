"""COVID-19 weekly series tests: cadence detection, aggregation, log level, growth pan."""

from __future__ import annotations

import io
import math
import wave
from datetime import date, timedelta

import numpy as np
import pytest
from app.schemas.case_series import CaseMetric, CaseSampleFlag, CaseSampleStatus
from app.schemas.soundscape import SonificationSettings
from app.services.case_processing import (
    CaseProcessingError,
    detect_reporting_cadence,
    growth_log2,
    growth_to_pan,
    load_case_frame,
    normalize_log_count,
    process_case_series,
    select_location,
    week_ending_sunday,
)
from app.services.case_sonification import (
    DOMAIN_MAPPINGS,
    OMITTED_MAPPINGS,
    case_series_to_sonifiable,
)
from app.services.reference_ranges import (
    COVID_WEEKLY_CASES_RANGE,
    COVID_WEEKLY_DEATHS_RANGE,
)
from app.services.sonification import render_soundscape

DEATHS = COVID_WEEKLY_DEATHS_RANGE
FAST = SonificationSettings(duration_seconds=1.0, sample_rate_hz=8000)
HEADER = "date,location,new_cases,new_deaths,total_cases,total_deaths\n"


def write_csv(tmp_path, rows, *, name="sample.csv", header=HEADER):
    """Write a CSV whose rows are ``(date, location, new_cases, new_deaths)`` strings."""
    path = tmp_path / name
    body = "".join(f"{d},{loc},{cases},{deaths},,\n" for d, loc, cases, deaths in rows)
    path.write_text(header + body, encoding="utf-8")
    return path


def weekly_rows(location, weekly_values, *, start=date(2020, 1, 6), cases=""):
    """Sunday-reporting rows: six zero days then the week's total on Sunday.

    ``start`` must be a Monday so each block of seven days is one Monday-to-Sunday week.
    """
    rows = []
    day = start
    for value in weekly_values:
        for offset in range(7):
            current = day + timedelta(days=offset)
            is_sunday = current.weekday() == 6
            rows.append((current.isoformat(), location, cases if is_sunday else "0", value if is_sunday else "0"))
        day += timedelta(days=7)
    return rows


# --- week boundaries --------------------------------------------------------


@pytest.mark.parametrize(
    ("day", "expected"),
    [
        (date(2020, 1, 5), date(2020, 1, 5)),  # a Sunday closes its own week
        (date(2020, 1, 6), date(2020, 1, 12)),  # Monday -> the following Sunday
        (date(2020, 1, 11), date(2020, 1, 12)),  # Saturday -> the next day
        (date(2024, 8, 4), date(2024, 8, 4)),
    ],
)
def test_week_ending_sunday(day, expected):
    assert week_ending_sunday(day) == expected


# --- cadence detection ------------------------------------------------------


def test_weekly_reporting_is_detected_and_names_the_weekday(tmp_path):
    path = write_csv(tmp_path, weekly_rows("World", ["100", "200", "400"]))
    rows = select_location(load_case_frame(path), "World")
    cadence = detect_reporting_cadence(rows, "new_deaths")
    assert cadence.cadence == "weekly" and cadence.weekday == "Sunday"
    assert cadence.nonzero_rows == 3 and cadence.total_rows == 21
    assert "every one of them falls on a Sunday" in cadence.describe()


def test_daily_reporting_is_detected(tmp_path):
    rows = [(f"2020-03-{day:02d}", "World", "5", "5") for day in range(1, 29)]
    frame = select_location(load_case_frame(write_csv(tmp_path, rows)), "World")
    cadence = detect_reporting_cadence(frame, "new_deaths")
    assert cadence.cadence == "daily" and cadence.weekday is None


def test_all_zero_series_is_indeterminate(tmp_path):
    rows = [(f"2020-03-{day:02d}", "World", "0", "0") for day in range(1, 29)]
    frame = select_location(load_case_frame(write_csv(tmp_path, rows)), "World")
    assert detect_reporting_cadence(frame, "new_deaths").cadence == "indeterminate"


# --- log normalization ------------------------------------------------------


@pytest.mark.parametrize(
    ("count", "expected"),
    [
        (1.0, 0.0),  # log10(1) = 0 -> the gain floor
        (10.0, 0.2),  # one decade of five
        (100.0, 0.4),
        (1_000.0, 0.6),
        (10_000.0, 0.8),
        (100_000.0, 1.0),  # the reference maximum
    ],
)
def test_each_decade_is_one_fifth_of_the_level(count, expected):
    level, flag = normalize_log_count(count, DEATHS)
    assert level == pytest.approx(expected)
    assert flag is None


def test_zero_sits_at_the_floor_and_is_flagged_as_clipped_low():
    level, flag = normalize_log_count(0.0, DEATHS)
    assert level == 0.0 and flag is CaseSampleFlag.CLIPPED_LOW


def test_counts_above_the_reference_maximum_clip_and_are_flagged():
    level, flag = normalize_log_count(103_719.0, DEATHS)
    assert level == 1.0 and flag is CaseSampleFlag.CLIPPED_HIGH


def test_level_is_monotonic_in_the_count():
    levels = [normalize_log_count(float(10**k), DEATHS)[0] for k in range(6)]
    assert levels == sorted(levels)
    assert all(0.0 <= level <= 1.0 for level in levels)


def test_cases_use_a_wider_eight_decade_scale():
    assert COVID_WEEKLY_CASES_RANGE.maximum == 100_000_000.0
    assert normalize_log_count(10_000.0, COVID_WEEKLY_CASES_RANGE)[0] == pytest.approx(0.5)


@pytest.mark.parametrize("bad", [math.nan, math.inf])
def test_non_finite_counts_are_rejected(bad):
    with pytest.raises(CaseProcessingError):
        normalize_log_count(bad, DEATHS)


# --- growth -> pan ----------------------------------------------------------


@pytest.mark.parametrize(
    ("count", "previous", "expected_growth", "expected_pan"),
    [
        (200.0, 100.0, 1.0, 1.0),  # doubling pans fully right
        (50.0, 100.0, -1.0, -1.0),  # halving pans fully left
        (100.0, 100.0, 0.0, 0.0),  # flat stays centred
        (400.0, 100.0, 2.0, 1.0),  # quadrupling clamps at the right rail
        (141.4213562, 100.0, 0.5, 0.5),  # sqrt(2) is half a doubling
    ],
)
def test_growth_maps_to_pan(count, previous, expected_growth, expected_pan):
    growth = growth_log2(count, previous)
    assert growth == pytest.approx(expected_growth, abs=1e-6)
    assert growth_to_pan(growth) == pytest.approx(expected_pan, abs=1e-6)


@pytest.mark.parametrize(
    ("count", "previous"),
    [(None, 100.0), (100.0, None), (0.0, 100.0), (100.0, 0.0), (math.nan, 100.0)],
)
def test_growth_is_undefined_without_two_positive_counts(count, previous):
    assert growth_log2(count, previous) is None
    assert growth_to_pan(None) is None


def test_rising_and_falling_pan_to_opposite_sides():
    assert growth_to_pan(growth_log2(150.0, 100.0)) > 0
    assert growth_to_pan(growth_log2(100.0, 150.0)) < 0


# --- aggregation and status -------------------------------------------------


def test_weekly_totals_are_recovered_from_sunday_reporting(tmp_path):
    path = write_csv(tmp_path, weekly_rows("World", ["7", "70", "700"]))
    series = process_case_series(path, location="World", minimum_usable_samples=3)
    assert [s.count for s in series.samples] == [7.0, 70.0, 700.0]
    assert [s.reported_days for s in series.samples] == [7, 7, 7]
    assert all(s.status is CaseSampleStatus.VALID for s in series.samples)
    # The six zero days of each week are never counted as measurements.
    assert series.summary.total == 3 and series.summary.usable == 3


def test_a_week_with_a_null_value_is_missing_not_zero(tmp_path):
    rows = weekly_rows("World", ["10", "20", "30"])
    rows[8] = (rows[8][0], "World", "0", "")  # blank a mid-week day of week 2
    path = write_csv(tmp_path, rows)
    series = process_case_series(path, location="World", minimum_usable_samples=1)
    assert series.samples[1].status is CaseSampleStatus.MISSING
    assert series.samples[1].count is None and series.samples[1].level_normalized is None
    assert series.summary.missing == 1 and series.summary.usable == 2


def test_a_negative_value_makes_the_week_invalid(tmp_path):
    rows = weekly_rows("World", ["10", "-5", "30"])
    path = write_csv(tmp_path, rows)
    series = process_case_series(path, location="World", minimum_usable_samples=1)
    assert series.samples[1].status is CaseSampleStatus.INVALID
    assert series.samples[1].count is None
    assert series.summary.invalid == 1


def test_a_week_with_no_rows_at_all_is_kept_as_a_silent_gap(tmp_path):
    rows = weekly_rows("World", ["10", "20", "30", "40"])
    del rows[7:14]  # remove every row of week 2
    path = write_csv(tmp_path, rows)
    series = process_case_series(path, location="World", minimum_usable_samples=1)
    assert len(series.samples) == 4  # the timeline keeps its place
    assert series.samples[1].status is CaseSampleStatus.MISSING
    assert series.samples[1].reported_days == 0
    assert [s.count for s in series.samples] == [10.0, None, 30.0, 40.0]


def test_growth_is_undefined_across_a_gap(tmp_path):
    rows = weekly_rows("World", ["10", "20", "30"])
    del rows[7:14]
    path = write_csv(tmp_path, rows)
    series = process_case_series(path, location="World", minimum_usable_samples=1)
    # Week 3 follows a missing week, so its ratio has no denominator.
    assert series.samples[2].growth_log2 is None
    assert CaseSampleFlag.UNDEFINED_GROWTH in series.samples[2].flags
    assert series.samples[2].pan is None


def test_the_first_week_has_no_growth_and_a_partial_week_is_flagged(tmp_path):
    # Start on a Sunday so the first week bin holds only that one row.
    path = write_csv(tmp_path, [("2020-01-05", "World", "1", "1"), *weekly_rows("World", ["10", "20"])])
    series = process_case_series(path, location="World", minimum_usable_samples=1)
    first = series.samples[0]
    assert first.reported_days == 1 and CaseSampleFlag.PARTIAL_WEEK in first.flags
    assert first.growth_log2 is None and CaseSampleFlag.UNDEFINED_GROWTH in first.flags
    assert first.count == 1.0 and first.level_normalized == 0.0
    assert series.summary.partial_weeks == 1


def test_window_bounds_are_inclusive(tmp_path):
    path = write_csv(tmp_path, weekly_rows("World", ["10", "20", "30", "40"]))
    weeks = [s.week_ending for s in process_case_series(path, location="World", minimum_usable_samples=1).samples]
    trimmed = process_case_series(
        path, location="World", minimum_usable_samples=1, start=weeks[1], end=weeks[2]
    )
    assert [s.week_ending for s in trimmed.samples] == weeks[1:3]


def test_an_empty_window_is_rejected(tmp_path):
    path = write_csv(tmp_path, weekly_rows("World", ["10", "20"]))
    with pytest.raises(CaseProcessingError, match="no weeks"):
        process_case_series(path, location="World", start=date(2030, 1, 6), end=date(2030, 1, 13))


def test_too_few_usable_weeks_is_rejected(tmp_path):
    path = write_csv(tmp_path, weekly_rows("World", ["10", "20"]))
    with pytest.raises(CaseProcessingError, match="Insufficient data"):
        process_case_series(path, location="World", minimum_usable_samples=10)


def test_unknown_location_lists_what_is_available(tmp_path):
    path = write_csv(tmp_path, weekly_rows("World", ["10", "20"]))
    with pytest.raises(CaseProcessingError, match="Available: World"):
        process_case_series(path, location="Atlantis")


def test_duplicate_dates_are_rejected(tmp_path):
    rows = weekly_rows("World", ["10", "20"])
    path = write_csv(tmp_path, [*rows, rows[0]])
    with pytest.raises(CaseProcessingError, match="Duplicate row"):
        process_case_series(path, location="World", minimum_usable_samples=1)


def test_missing_columns_are_rejected(tmp_path):
    path = tmp_path / "bad.csv"
    path.write_text("date,location\n2020-01-05,World\n", encoding="utf-8")
    with pytest.raises(CaseProcessingError, match="missing required column"):
        process_case_series(path, location="World")


def test_unparseable_dates_are_rejected(tmp_path):
    path = write_csv(tmp_path, [("not-a-date", "World", "1", "1")])
    with pytest.raises(CaseProcessingError, match="unparseable date"):
        process_case_series(path, location="World")


def test_missing_file_is_rejected(tmp_path):
    with pytest.raises(CaseProcessingError, match="not found"):
        process_case_series(tmp_path / "absent.csv", location="World")


# --- metadata and summary ---------------------------------------------------


def test_metadata_records_the_cited_reference_and_the_detected_cadence(tmp_path):
    path = write_csv(tmp_path, weekly_rows("World", ["10", "20", "30"]))
    metadata = process_case_series(
        path, location="World", minimum_usable_samples=1, source_sha256="a" * 64, source_url="https://example.test"
    ).metadata
    assert metadata.level_normalization.maximum == 100_000.0
    assert "WHO" in metadata.level_normalization.source
    assert metadata.level_normalization.url.startswith("https://www.who.int/")
    assert metadata.selection.reporting_weekday == "Sunday"
    assert metadata.selection.metric is CaseMetric.DEATHS
    assert metadata.source_sha256 == "a" * 64 and metadata.source_url == "https://example.test"
    assert metadata.pan_log2_full_scale == 1.0
    assert "log10" in metadata.level_rule and "log2" in metadata.growth_rule


def test_summary_reports_the_peak_week(tmp_path):
    path = write_csv(tmp_path, weekly_rows("World", ["10", "9000", "30"]))
    series = process_case_series(path, location="World", minimum_usable_samples=1)
    assert series.summary.count_max == 9000.0 and series.summary.count_min == 10.0
    assert series.summary.peak_week_ending == series.samples[1].week_ending


def test_cases_metric_reads_the_other_column(tmp_path):
    rows = weekly_rows("World", ["5", "5", "5"], cases="50000")
    path = write_csv(tmp_path, rows)
    series = process_case_series(path, location="World", metric=CaseMetric.CASES, minimum_usable_samples=1)
    assert [s.count for s in series.samples] == [50000.0] * 3
    assert series.metadata.level_normalization.maximum == 100_000_000.0
    assert series.metadata.units == "cases per week"


# --- adapter and rendering --------------------------------------------------


def decode(wav_bytes: bytes) -> np.ndarray:
    with wave.open(io.BytesIO(wav_bytes), "rb") as reader:
        frames = np.frombuffer(reader.readframes(reader.getnframes()), dtype="<i2")
        return frames.reshape(-1, reader.getnchannels())


def test_adapter_carries_level_growth_and_silence(tmp_path):
    rows = weekly_rows("World", ["100", "200", "50"])
    rows[15] = (rows[15][0], "World", "0", "")  # blank a day of week 3
    path = write_csv(tmp_path, rows)
    series = process_case_series(path, location="World", minimum_usable_samples=1)
    sonifiable = case_series_to_sonifiable(series)
    assert sonifiable.provenance.domain == "covid_cases"
    assert "Sunday" in sonifiable.provenance.cadence
    first, rising, gap = sonifiable.samples
    assert first.audible and first.pan is None  # no previous week
    assert rising.audible and rising.pan == pytest.approx(1.0)  # 100 -> 200 is a doubling
    assert not gap.audible and gap.level_normalized is None
    assert rising.source_values["count"] == 200.0
    assert rising.source_values["growth_log2"] == pytest.approx(1.0)
    assert sonifiable.mapping_source.level_reference.maximum == 100_000.0


def test_adapter_declares_its_mappings_and_omissions(tmp_path):
    path = write_csv(tmp_path, weekly_rows("World", ["10", "20"]))
    series = process_case_series(path, location="World", minimum_usable_samples=1)
    sonifiable = case_series_to_sonifiable(series)
    assert sonifiable.domain_mappings == list(DOMAIN_MAPPINGS)
    assert sonifiable.omitted_mappings == list(OMITTED_MAPPINGS)
    assert any("pitch" in rule.sound_parameter for rule in OMITTED_MAPPINGS)


def test_processed_case_series_renders_directly_and_deterministically(tmp_path):
    path = write_csv(tmp_path, weekly_rows("World", ["1", "100", "10000", "100000"]))
    series = process_case_series(path, location="World", minimum_usable_samples=1)
    first = render_soundscape(series, FAST)
    second = render_soundscape(series, FAST)
    assert first.wav_bytes == second.wav_bytes
    assert first.provenance.source.domain == "covid_cases"
    gains = [segment.gain_db for segment in first.provenance.segments]
    assert gains == pytest.approx([-36.0, -21.6, -7.2, 0.0])


def test_a_silent_week_renders_as_digital_silence(tmp_path):
    rows = weekly_rows("World", ["100", "200", "300"])
    del rows[7:14]
    path = write_csv(tmp_path, rows)
    series = process_case_series(path, location="World", minimum_usable_samples=1)
    rendered = render_soundscape(series, FAST)
    frames = decode(rendered.wav_bytes)
    gap = rendered.provenance.segments[1]
    assert not gap.audible
    assert np.all(frames[gap.start_frame : gap.end_frame] == 0)
    assert rendered.provenance.source.silent_samples == 1
