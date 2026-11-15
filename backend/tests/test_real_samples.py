"""Phase 6 validation against the real, documented samples.

Every check here recomputes a value **independently** of the code under test:
the NetCDF file is read with ``netCDF4`` directly (not through the xarray loader),
COVID weekly totals are rebuilt from OWID's own cumulative ``total_deaths``
column, and audio is measured from decoded WAV frames rather than read back
from the provenance that produced it.

Fault-injection tests copy a real sample, damage chosen days or weeks
(fill values, out-of-range values, nulls, negatives, removed rows), and run the
whole pipeline on the copy. They prove that missing or invalid data can never
reach the audio as a sound.

The samples live under the Git-ignored ``data/raw/``; each test skips when its
sample has not been fetched. See ``docs/validation.md`` for the record.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import shutil
import wave
from datetime import date, timedelta
from pathlib import Path

import netCDF4
import numpy as np
import pytest
from app.config import REPOSITORY_ROOT, Settings
from app.schemas.case_series import CaseMetric
from app.schemas.current_series import SampleStatus
from app.services.case_processing import process_case_series
from app.services.current_processing import process_current_series
from app.services.dataset_loader import open_ocean_dataset
from app.services.series_extraction import extract_default_series
from app.services.sonification import default_sonification_settings, render_soundscape
from app.services.spectrum_sonification import render_sirius

SETTINGS = Settings(_env_file=None)
OCEAN = SETTINGS.raw_data_dir / SETTINGS.ocean_sample_file
COVID = SETTINGS.raw_data_dir / SETTINGS.covid_sample_file
SIRIUS = SETTINGS.raw_data_dir / SETTINGS.sirius_spectrum_file
METADATA = REPOSITORY_ROOT / "data" / "metadata"

needs_ocean = pytest.mark.skipif(not OCEAN.is_file(), reason="real OSCAR sample not fetched")
needs_covid = pytest.mark.skipif(not COVID.is_file(), reason="real OWID sample not fetched")
needs_sirius = pytest.mark.skipif(not SIRIUS.is_file(), reason="real CALSPEC Sirius file not fetched")

FILL = -999.0
PCM_FULL_SCALE = 32767


# --- helpers -----------------------------------------------------------------


def decode_wav(wav_bytes: bytes) -> tuple[int, np.ndarray]:
    """Sample rate and int16 frames of shape ``(frames, channels)``."""
    with wave.open(io.BytesIO(wav_bytes)) as reader:
        rate, channels, frames = reader.getframerate(), reader.getnchannels(), reader.getnframes()
        data = np.frombuffer(reader.readframes(frames), dtype="<i2").reshape(-1, channels)
    return rate, data


def raw_ocean_components(path: Path) -> dict[str, np.ndarray]:
    """u, v, lat, lon (0-360) and day indices read with netCDF4, fill values left as -999.

    Uses the variables' own dimension names to order the axes as (time, lat, lon),
    so a transposition in the loader would show up as a mismatch.
    """
    with netCDF4.Dataset(path) as nc:
        nc.set_auto_mask(False)
        out: dict[str, np.ndarray] = {"lat": np.array(nc["lat"][:]), "lon": np.array(nc["lon"][:])}
        for name in ("u", "v"):
            var = nc[name]
            order = [var.dimensions.index(d) for d in ("time", "latitude", "longitude")]
            out[name] = np.transpose(np.array(var[:], dtype=np.float64), order)
            out[f"{name}_attrs"] = {k: var.getncattr(k) for k in var.ncattrs()}
        out["time"] = np.array(nc["time"][:])
        out["time_units"] = nc["time"].units
    return out


def silent_and_audible_frames(rendered) -> tuple[np.ndarray, list]:
    _, frames = decode_wav(rendered.wav_bytes)
    return frames, rendered.provenance.segments


def assert_silence_is_exact_and_sound_is_present(rendered) -> None:
    frames, segments = silent_and_audible_frames(rendered)
    assert segments[-1].end_frame == len(frames)
    for segment in segments:
        block = frames[segment.start_frame : segment.end_frame]
        if segment.audible:
            assert np.abs(block).max() > 0, f"audible segment {segment.index} rendered as silence"
        else:
            assert not block.any(), f"silent segment {segment.index} ({segment.status}) contains sound"


# --- 1. NetCDF loading against the documented sample --------------------------


@needs_ocean
def test_documented_sample_matches_its_committed_inspection_record():
    """Shape, coordinates, units, fill value and valid range are what Phase 2 recorded."""
    inspection = json.loads((METADATA / "oscar-final-20200101-20200130.inspection.json").read_text("utf-8"))
    retrieval = json.loads((METADATA / "oscar-final-20200101-20200130.retrieval.json").read_text("utf-8"))
    reference = json.loads((METADATA / "oscar-final-20200101-20200130.point.soundscape.json").read_text("utf-8"))
    raw = raw_ocean_components(OCEAN)

    assert raw["u"].shape == raw["v"].shape == (30, 9, 9)
    assert inspection["dimensions"] == {"time": 30, "longitude": 9, "latitude": 9}
    assert raw["lat"].min() == 0.0 and raw["lat"].max() == 2.0
    assert raw["lon"].min() == 208.0 and raw["lon"].max() == 210.0
    assert np.allclose(np.diff(raw["lat"]), 0.25) and np.allclose(np.diff(raw["lon"]), 0.25)
    assert raw["time_units"] == "days since 1990-01-01"
    assert list(raw["time"]) == [10957.0 + day for day in range(30)]  # 2020-01-01 .. 2020-01-30
    for name in ("u", "v"):
        attrs = raw[f"{name}_attrs"]
        assert attrs["units"] == "m s-1"
        assert attrs["_FillValue"] == FILL
        assert (attrs["valid_min"], attrs["valid_max"]) == (-3.0, 3.0)
        assert inspection["variables"][name]["fill_value"] == FILL
    # The documented "no missing u/v values" claim about this sample.
    assert not (raw["u"] == FILL).any() and not (raw["v"] == FILL).any()
    # The file is the one the retrieval record describes and the reference render was made from.
    assert retrieval["selection"]["bbox_wgs84"] == [-152.0, 0.0, -150.0, 2.0]
    assert len(retrieval["selection"]["granules"]) == 30
    assert hashlib.sha256(OCEAN.read_bytes()).hexdigest() == reference["source"]["source_sha256"]


@needs_ocean
def test_loader_decodes_the_same_values_netcdf4_reads(tmp_path):
    """The xarray loader returns the file's numbers unchanged and masks the fill value as NaN."""
    raw = raw_ocean_components(OCEAN)
    dataset, _ = open_ocean_dataset(OCEAN, required_variables=("u", "v"))
    with dataset:
        u = dataset["u"].transpose("time", "latitude", "longitude").values
    assert np.array_equal(u, raw["u"])

    damaged = tmp_path / OCEAN.name
    shutil.copyfile(OCEAN, damaged)
    with netCDF4.Dataset(damaged, "r+") as nc:
        nc.set_auto_mask(False)
        nc["u"][0, :, :] = FILL
    dataset, _ = open_ocean_dataset(damaged, required_variables=("u", "v"))
    with dataset:
        first_day = dataset["u"].isel(time=0).values
        second_day = dataset["u"].isel(time=1).values
    assert np.isnan(first_day).all()
    assert not np.isnan(second_day).any()


# --- 2. Speed and direction against known inputs -----------------------------


@needs_ocean
def test_point_speed_and_direction_match_an_independent_computation():
    """Nearest cell to (1.0 N, 151.0 W) is lat 1.0, lon 209.0; values recomputed by hand."""
    raw = raw_ocean_components(OCEAN)
    lat_index = int(np.argmin(np.abs(raw["lat"] - 1.0)))
    lon_index = int(np.argmin(np.abs(raw["lon"] - (360.0 - 151.0))))
    u = raw["u"][:, lat_index, lon_index]
    v = raw["v"][:, lat_index, lon_index]
    expected_speed = np.sqrt(u**2 + v**2)
    expected_direction = np.degrees(np.arctan2(u, v)) % 360.0  # toward, clockwise from north

    series = process_current_series(extract_default_series(OCEAN, mode="point", settings=SETTINGS))
    assert series.metadata.selection.selected_latitude == 1.0
    assert series.metadata.selection.selected_longitude == 209.0
    assert [s.status for s in series.samples] == [SampleStatus.VALID] * 30
    assert np.allclose([s.u for s in series.samples], u, rtol=0, atol=1e-12)
    assert np.allclose([s.v for s in series.samples], v, rtol=0, atol=1e-12)
    assert np.allclose([s.speed for s in series.samples], expected_speed, rtol=0, atol=1e-12)
    assert np.allclose([s.direction_deg for s in series.samples], expected_direction, rtol=0, atol=1e-9)
    assert np.allclose([s.speed_normalized for s in series.samples], expected_speed / 2.5, rtol=0, atol=1e-12)


@needs_ocean
def test_first_day_is_a_north_north_east_flow_of_about_0_31_m_s():
    """A pinned known value, readable by a person checking the record."""
    series = process_current_series(extract_default_series(OCEAN, mode="point", settings=SETTINGS))
    first = series.samples[0]
    assert first.u == pytest.approx(0.08133927256548532, abs=1e-12)
    assert first.v == pytest.approx(0.2947176195355425, abs=1e-12)
    assert first.speed == pytest.approx(0.30573608312755474, abs=1e-12)
    assert first.direction_deg == pytest.approx(15.42900096220732, abs=1e-9)
    # u > 0, v > 0 and v > u: flowing toward NNE. The "from" convention would give ~195 degrees.
    assert 0 < first.direction_deg < 22.5


@needs_ocean
def test_region_mean_is_the_speed_of_the_mean_vector():
    raw = raw_ocean_components(OCEAN)
    mean_u = raw["u"].mean(axis=(1, 2))
    mean_v = raw["v"].mean(axis=(1, 2))
    series = process_current_series(extract_default_series(OCEAN, mode="region", settings=SETTINGS))
    assert [s.cell_count for s in series.samples] == [81] * 30
    assert np.allclose([s.u for s in series.samples], mean_u, rtol=0, atol=1e-12)
    assert np.allclose([s.speed for s in series.samples], np.hypot(mean_u, mean_v), rtol=0, atol=1e-12)
    # Mean of cell speeds is never smaller than the speed of the mean vector.
    mean_cell_speed = np.hypot(raw["u"], raw["v"]).mean(axis=(1, 2))
    assert (mean_cell_speed >= np.hypot(mean_u, mean_v) - 1e-12).all()


# --- 3. Missing and invalid data never reach the audio (ocean) ----------------


def _damage_ocean(path: Path, lat_index: int, lon_index: int) -> dict[int, SampleStatus]:
    """Damage chosen days at one cell; returns the status each damaged day must get."""
    with netCDF4.Dataset(path, "r+") as nc:
        nc.set_auto_mask(False)
        u, v = nc["u"], nc["v"]
        lat_axis, lon_axis = u.dimensions.index("latitude"), u.dimensions.index("longitude")

        def put(var, day: int, value: float) -> None:
            index = [day, 0, 0]
            index[lat_axis], index[lon_axis] = lat_index, lon_index
            var[tuple(index)] = value

        put(u, 3, FILL)  # one component at the fill value
        put(v, 4, FILL)
        put(u, 9, 3.5)  # outside valid_max
        put(v, 10, -3.0000001)  # just outside valid_min
        put(u, 15, FILL)  # fill wins over invalid in the other component
        put(v, 15, 99.0)
        put(u, 20, 3.0)  # exactly on the bound: still valid
    return {
        3: SampleStatus.MISSING,
        4: SampleStatus.MISSING,
        9: SampleStatus.INVALID,
        10: SampleStatus.INVALID,
        15: SampleStatus.MISSING,
    }


@needs_ocean
def test_damaged_ocean_days_are_flagged_and_render_as_digital_silence(tmp_path):
    damaged = tmp_path / OCEAN.name
    shutil.copyfile(OCEAN, damaged)
    raw = raw_ocean_components(OCEAN)
    lat_index = int(np.argmin(np.abs(raw["lat"] - 1.0)))
    lon_index = int(np.argmin(np.abs(raw["lon"] - 209.0)))
    expected = _damage_ocean(damaged, lat_index, lon_index)

    series = process_current_series(extract_default_series(damaged, mode="point", settings=SETTINGS))
    for day, sample in enumerate(series.samples):
        if day in expected:
            assert sample.status == expected[day], day
            assert sample.speed is None and sample.u is None and sample.v is None
        else:
            assert sample.status == SampleStatus.VALID, day
    assert series.samples[20].u == 3.0
    assert series.summary.missing == 3 and series.summary.invalid == 2 and series.summary.usable == 25

    rendered = render_soundscape(series)
    silent = {s.index for s in rendered.provenance.segments if not s.audible}
    assert silent == set(expected)
    assert rendered.provenance.source.silent_samples == len(expected)
    assert_silence_is_exact_and_sound_is_present(rendered)


@needs_ocean
def test_one_bad_cell_makes_the_regional_day_invalid_and_an_empty_day_missing(tmp_path):
    damaged = tmp_path / OCEAN.name
    shutil.copyfile(OCEAN, damaged)
    with netCDF4.Dataset(damaged, "r+") as nc:
        nc.set_auto_mask(False)
        nc["u"][5, 4, 4] = 7.0  # one invalid cell out of 81 on day 5
        nc["u"][12, :, :] = FILL  # no cells at all on day 12
        nc["v"][18, 0, 0] = FILL  # one missing cell on day 18: averaged over the other 80

    series = process_current_series(extract_default_series(damaged, mode="region", settings=SETTINGS))
    assert series.samples[5].status == SampleStatus.INVALID and series.samples[5].speed is None
    assert series.samples[12].status == SampleStatus.MISSING and series.samples[12].speed is None
    assert series.samples[18].status == SampleStatus.VALID and series.samples[18].cell_count == 80

    rendered = render_soundscape(series)
    assert {s.index for s in rendered.provenance.segments if not s.audible} == {5, 12}
    assert_silence_is_exact_and_sound_is_present(rendered)


# --- 4. COVID weekly totals against OWID's own cumulative column --------------


def _covid_rows(path: Path, location: str = "World") -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return [row for row in csv.DictReader(handle) if row["location"] == location]


@needs_covid
def test_weekly_totals_equal_owids_own_weekly_column():
    """``count`` for each week equals OWID's precomputed rolling ``weekly_deaths`` on that Sunday."""
    by_day = {date.fromisoformat(r["date"]): r for r in _covid_rows(COVID)}
    series = process_case_series(COVID, location="World", metric=CaseMetric.DEATHS)
    assert series.metadata.selection.reporting_cadence.startswith("Weekly")
    assert series.metadata.selection.reporting_weekday == "Sunday"
    assert series.summary.missing == series.summary.invalid == 0

    checked = 0
    for sample in series.samples:
        published = by_day[sample.week_ending]["weekly_deaths"]
        if published:  # empty for the first, partial week
            assert sample.count == float(published), sample.week_ending
            checked += 1
    assert checked == len(series.samples) - 1


@needs_covid
def test_owid_cumulative_column_disagrees_with_its_daily_column_by_under_0_1_percent():
    """A property of the source, recorded in docs/validation.md.

    OWID's ``total_deaths`` is not exactly the running sum of ``new_deaths``: in 28 of
    240 weeks of the 2026-11-14 snapshot the week-on-week difference of the cumulative
    column differs from the weekly sum (4,042 deaths in absolute terms, 0.06 %). The pipeline
    follows ``new_deaths``, the column that ``weekly_deaths`` is also built from.
    """
    rows = _covid_rows(COVID)
    by_day = {date.fromisoformat(r["date"]): r for r in rows}
    series = process_case_series(COVID, location="World", metric=CaseMetric.DEATHS)
    absolute_error = 0.0
    for sample in series.samples:
        previous = by_day.get(sample.week_ending - timedelta(days=7))
        from_cumulative = float(by_day[sample.week_ending]["total_deaths"]) - (
            float(previous["total_deaths"]) if previous else 0.0
        )
        absolute_error += abs(from_cumulative - (sample.count or 0.0))
    total = sum(s.count or 0.0 for s in series.samples)
    assert total == sum(float(r["new_deaths"] or 0) for r in rows)
    assert absolute_error / total < 0.001


@needs_covid
def test_covid_peak_week_and_counts_are_pinned():
    series = process_case_series(COVID, location="World", metric=CaseMetric.DEATHS)
    assert len(series.samples) == 240
    assert series.samples[0].week_ending == date(2020, 1, 5) and series.samples[0].count == 3
    peak = max(series.samples, key=lambda s: s.count or 0)
    assert series.summary.peak_week_ending == peak.week_ending
    # level = log10(count) / 5 against the cited 10^5 deaths/week ceiling.
    for sample in series.samples:
        if sample.count and sample.count >= 1:
            assert sample.level_normalized == pytest.approx(min(1.0, math.log10(sample.count) / 5), abs=1e-12)


def _write_damaged_covid(path: Path, target: Path) -> dict[date, str]:
    """Null one Sunday, make another negative, delete a whole week. Returns expected statuses."""
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        fields, rows = reader.fieldnames, list(reader)
    world = [r for r in rows if r["location"] == "World"]
    sundays = [date.fromisoformat(r["date"]) for r in world if date.fromisoformat(r["date"]).weekday() == 6]
    null_week, negative_week, removed_week = sundays[20], sundays[60], sundays[100]
    kept = []
    for row in rows:
        day = date.fromisoformat(row["date"])
        if row["location"] == "World":
            if removed_week - timedelta(days=6) <= day <= removed_week:
                continue
            if day == null_week:
                row = {**row, "new_deaths": ""}
            if day == negative_week:
                row = {**row, "new_deaths": "-5"}
        kept.append(row)
    with target.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(kept)
    return {null_week: "missing", negative_week: "invalid", removed_week: "missing"}


@needs_covid
def test_damaged_covid_weeks_are_flagged_and_render_as_digital_silence(tmp_path):
    damaged = tmp_path / COVID.name
    expected = _write_damaged_covid(COVID, damaged)
    series = process_case_series(damaged, location="World", metric=CaseMetric.DEATHS)
    by_week = {s.week_ending: s for s in series.samples}
    for week, status in expected.items():
        assert by_week[week].status.value == status, week
        assert by_week[week].count is None and by_week[week].level_normalized is None
    assert len(series.samples) == 240  # the deleted week is kept as a gap, not dropped

    rendered = render_soundscape(series)
    silent = {s.timestamp.date() for s in rendered.provenance.segments if not s.audible}
    assert silent == set(expected)
    assert_silence_is_exact_and_sound_is_present(rendered)


# --- 5. Sonification parameters measured from the audio -----------------------


def _carrier_peak(harmonics: tuple[float, ...]) -> float:
    """Peak of the normalized harmonic waveform, found on a dense grid (not by the engine)."""
    phase = np.linspace(0.0, 2.0 * math.pi, 200_001)
    total = sum(harmonics)
    wave_sum = sum((a / total) * np.sin(k * phase) for k, a in enumerate(harmonics, start=1))
    return float(np.abs(wave_sum).max())


@needs_ocean
@needs_covid
@pytest.mark.parametrize("domain", ["ocean", "covid"])
def test_measured_gain_and_pan_of_every_pulse_match_the_documented_rules(domain):
    """Per channel, the measured peak equals 0.9 * 10^(-36 (1 - level) / 20) * pan law * carrier peak."""
    series = (
        process_current_series(extract_default_series(OCEAN, mode="point", settings=SETTINGS))
        if domain == "ocean"
        else process_case_series(COVID, location="World", metric=CaseMetric.DEATHS)
    )
    settings = default_sonification_settings()
    rendered = render_soundscape(series, settings)
    rate, frames = decode_wav(rendered.wav_bytes)
    carrier_peak = _carrier_peak(settings.harmonic_amplitudes)
    one_cycle = math.ceil(rate / settings.carrier_frequency_hz)
    fully_measured = 0

    for segment in rendered.provenance.segments:
        expected_gain_db = -36.0 * (1.0 - segment.level_normalized)
        assert segment.gain_db == pytest.approx(expected_gain_db, abs=1e-9)
        amplitude = 0.9 * 10.0 ** (expected_gain_db / 20.0)
        angle = (segment.pan + 1.0) * math.pi / 4.0
        length = segment.end_frame - segment.start_frame
        # The engine clamps attack and release to half a segment each (documented).
        attack = min(round(settings.attack_seconds * rate), length // 2)
        release = min(round(settings.release_seconds * rate), length // 2)
        whole = frames[segment.start_frame : segment.end_frame].astype(np.float64)
        sustain = whole[attack : length - release]
        for channel, law in enumerate((math.cos(angle), math.sin(angle))):
            expected = amplitude * law * carrier_peak * PCM_FULL_SCALE
            # Never louder than the rule allows, envelope or not.
            assert np.abs(whole[:, channel]).max() <= expected * 1.0001 + 1.0, (segment.index, channel)
            if len(sustain) >= one_cycle:
                # Peak sampling of a 220 Hz tone at 44.1 kHz loses < 0.5 %; allow 1 % plus 1 LSB.
                measured = np.abs(sustain[:, channel]).max()
                assert measured == pytest.approx(expected, rel=0.01, abs=1.0), (segment.index, channel)
        fully_measured += len(sustain) >= one_cycle
    # Most pulses have a sustained part long enough to measure exactly.
    assert fully_measured >= 0.5 * len(rendered.provenance.segments)


@needs_ocean
def test_ocean_pan_follows_the_east_west_component():
    series = process_current_series(extract_default_series(OCEAN, mode="point", settings=SETTINGS))
    rendered = render_soundscape(series)
    for sample, segment in zip(series.samples, rendered.provenance.segments, strict=True):
        assert segment.pan == pytest.approx(math.sin(math.radians(sample.direction_deg)), abs=1e-12)
        assert segment.pan == pytest.approx(sample.u / sample.speed, abs=1e-9)  # = u / |v|


@needs_ocean
def test_the_pitch_is_a_fixed_220_hz_carrier_with_the_documented_harmonics():
    series = process_current_series(extract_default_series(OCEAN, mode="point", settings=SETTINGS))
    rate, frames = decode_wav(render_soundscape(series).wav_bytes)
    mono = frames.astype(np.float64).sum(axis=1)
    spectrum = np.abs(np.fft.rfft(mono * np.hanning(len(mono))))
    freqs = np.fft.rfftfreq(len(mono), 1.0 / rate)

    def band(f: float) -> float:
        return spectrum[(freqs > f - 2) & (freqs < f + 2)].max()

    assert freqs[np.argmax(spectrum)] == pytest.approx(220.0, abs=0.1)
    assert band(440.0) / band(220.0) == pytest.approx(0.5, rel=0.05)
    assert band(660.0) / band(220.0) == pytest.approx(0.25, rel=0.05)
    assert band(330.0) / band(220.0) < 0.01  # no energy between harmonics


# --- 6. Determinism and the committed reference renders -----------------------


@needs_ocean
@needs_covid
@pytest.mark.parametrize(
    ("domain", "mode", "reference"),
    [
        ("ocean", "point", "oscar-final-20200101-20200130.point.soundscape.json"),
        ("ocean", "region", "oscar-final-20200101-20200130.region.soundscape.json"),
        ("covid", None, "owid-covid-weekly.world.deaths.soundscape.json"),
    ],
)
def test_real_renders_are_byte_identical_and_equal_the_committed_hash(domain, mode, reference):
    def render() -> bytes:
        if domain == "ocean":
            series = process_current_series(extract_default_series(OCEAN, mode=mode, settings=SETTINGS))
        else:
            series = process_case_series(COVID, location="World", metric=CaseMetric.DEATHS)
        return render_soundscape(series).wav_bytes

    first, second = render(), render()
    assert first == second
    committed = json.loads((METADATA / reference).read_text("utf-8"))
    assert hashlib.sha256(first).hexdigest() == committed["output"]["sha256"]


@needs_sirius
def test_sirius_render_is_deterministic_and_equals_the_committed_hash():
    first, provenance = render_sirius(SIRIUS)
    second, _ = render_sirius(SIRIUS)
    assert first == second
    committed = json.loads((METADATA / "sirius-visible-spectrum.soundscape.json").read_text("utf-8"))
    assert hashlib.sha256(first).hexdigest() == committed["audio"]["sha256"] == provenance["audio"]["sha256"]
