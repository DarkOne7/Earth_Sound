"""Tests for the Sirius CALSPEC wavelength-scan renderer, using synthetic FITS files."""

from __future__ import annotations

import io
import wave
from pathlib import Path

import numpy as np
import pytest
from app.services import spectrum_sonification as spectrum
from app.services.spectrum_sonification import (
    BIN_COUNT,
    CALSPEC_FILE_NAME,
    CALSPEC_URL,
    FRAME_COUNT,
    MIDI_HIGH,
    MIDI_LOW,
    SAMPLE_RATE_HZ,
    SpectrumError,
    bin_boundaries,
    bin_envelope,
    load_visible_spectrum,
    render_sirius,
    scan_pitch_midi,
    wavelength_to_midi,
)


def _card(key: str, value: object | None = None, comment: str = "") -> bytes:
    if value is None:
        text = key.ljust(80)
    else:
        if isinstance(value, bool):
            rendered = ("T" if value else "F").rjust(20)
        elif isinstance(value, str):
            rendered = "'" + value.replace("'", "''").ljust(8) + "'"
        else:
            rendered = str(value).rjust(20)
        text = f"{key:<8}= {rendered}"
        if comment:
            text += f" / {comment}"
    return text.ljust(80)[:80].encode("ascii")


def _header(cards: list[bytes]) -> bytes:
    raw = b"".join(cards) + _card("END")
    return raw.ljust(-(-len(raw) // 2880) * 2880, b" ")


def _pad(data: bytes) -> bytes:
    return data.ljust(-(-len(data) // 2880) * 2880, b"\0")


def write_fits(
    path: Path,
    *,
    target: str = "SIRIUS",
    pedigree: str = "INFLIGHT 1997 to 2022",
    source: str = "Flux scale of Bohlin, et al.2020, AJ, 160, 21",
    flux_unit: str = "FLAM",
    primary_data: bool = False,
    pcount: int = 0,
    drop_range_nm: tuple[float, float] | None = None,
) -> Path:
    """Write a minimal CALSPEC-like FITS: primary HDU + SCI BINTABLE (WAVELENGTH, FLUX, DATAQUAL)."""
    wavelength_a = np.arange(3700.0, 7900.0, 2.0)
    flux = 1e-8 * (1.0 + 0.5 * np.sin(wavelength_a / 300.0))
    quality = np.ones_like(wavelength_a, dtype=np.int16)
    if drop_range_nm is not None:
        lower, upper = drop_range_nm
        quality[(wavelength_a / 10 >= lower) & (wavelength_a / 10 < upper)] = 0

    primary_cards = [_card("SIMPLE", True)]
    primary_payload = b""
    if primary_data:
        primary_cards += [_card("BITPIX", 16), _card("NAXIS", 2), _card("NAXIS1", 10), _card("NAXIS2", 3)]
        primary_payload = _pad(np.arange(30, dtype=">i2").tobytes())
    else:
        primary_cards += [_card("BITPIX", 8), _card("NAXIS", 0)]
    primary_cards += [
        _card("EXTEND", True),
        _card("TARGETID", target),
        _card("SOURCE", source),
        _card("PEDIGREE", pedigree),
        _card("DESCRIP", "Standard star flux with an HST/STIS calibration"),
    ]

    rows = np.zeros(
        len(wavelength_a), dtype=[("WAVELENGTH", ">f8"), ("FLUX", ">f4"), ("DATAQUAL", ">i2")]
    )
    rows["WAVELENGTH"] = wavelength_a
    rows["FLUX"] = flux
    rows["DATAQUAL"] = quality
    table_cards = [
        _card("XTENSION", "BINTABLE"),
        _card("BITPIX", 8),
        _card("NAXIS", 2),
        _card("NAXIS1", rows.dtype.itemsize),
        _card("NAXIS2", len(rows)),
        _card("PCOUNT", pcount),
        _card("GCOUNT", 1),
        _card("TFIELDS", 3),
        _card("EXTNAME", "SCI"),
        _card("TTYPE1", "WAVELENGTH"),
        _card("TFORM1", "1D"),
        _card("TUNIT1", "ANGSTROMS"),
        _card("TTYPE2", "FLUX"),
        _card("TFORM2", "1E"),
        _card("TUNIT2", flux_unit),
        _card("TTYPE3", "DATAQUAL"),
        _card("TFORM3", "1I"),
    ]
    table_payload = _pad(rows.tobytes() + b"\0" * pcount)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_header(primary_cards) + primary_payload + _header(table_cards) + table_payload)
    return path


@pytest.fixture()
def fits_path(tmp_path: Path) -> Path:
    return write_fits(tmp_path / CALSPEC_FILE_NAME)


def _pcm(wav_bytes: bytes) -> np.ndarray:
    with wave.open(io.BytesIO(wav_bytes)) as reader:
        assert reader.getframerate() == SAMPLE_RATE_HZ
        assert reader.getnchannels() == 2
        frames = reader.readframes(reader.getnframes())
    return np.frombuffer(frames, dtype="<i2").reshape(-1, 2)


# ------------------------------------------------------------------ FITS reader


@pytest.mark.parametrize(
    ("card", "expected"),
    [
        ("DESCRIP = 'Standard star flux with an HST/STIS calibration' /", "Standard star flux with an HST/STIS calibration"),
        ("TUNIT2  = 'ERG/S/CM2/A'        / flux unit", "ERG/S/CM2/A"),
        ("OBJECT  = 'O''Brien '", "O'Brien"),
        ("NAXIS1  =                   16 / width", 16),
        ("EXPTIME =               1.5D+2", 150.0),
        ("EXTEND  =                    T", True),
    ],
)
def test_header_values_keep_slashes_and_quotes(card: str, expected: object) -> None:
    assert spectrum._header_value(card.ljust(80)) == expected


def test_slashed_unit_reaches_provenance(tmp_path: Path) -> None:
    path = write_fits(tmp_path / CALSPEC_FILE_NAME, flux_unit="ERG/S/CM2/A")
    _, source = load_visible_spectrum(path)
    assert source["spectrum_table_units"]["flux"] == "ERG/S/CM2/A"


def test_primary_data_and_heap_are_skipped_correctly(tmp_path: Path, fits_path: Path) -> None:
    baseline, _ = load_visible_spectrum(fits_path)
    awkward = write_fits(tmp_path / "awkward" / CALSPEC_FILE_NAME, primary_data=True, pcount=5000)
    bins, _ = load_visible_spectrum(awkward)
    assert bins == baseline


def test_missing_bin_is_rejected(tmp_path: Path) -> None:
    path = write_fits(tmp_path / CALSPEC_FILE_NAME, drop_range_nm=(500.0, 501.0))
    with pytest.raises(SpectrumError, match="500–501 nm"):
        load_visible_spectrum(path)


# ----------------------------------------------------------------- provenance


def test_provenance_is_read_from_the_file(tmp_path: Path) -> None:
    path = write_fits(
        tmp_path / "sirius_stis_006.fits", pedigree="INFLIGHT 1997 to 2025", source="Some other scale"
    )
    _, source = load_visible_spectrum(path)
    assert source["source_pedigree"] == "INFLIGHT 1997 to 2025"
    assert source["source_flux_scale"] == "Some other scale"
    assert source["source_file"] == "sirius_stis_006.fits"
    assert source["source_url"] is None


def test_documented_file_gets_its_download_url(fits_path: Path) -> None:
    _, source = load_visible_spectrum(fits_path)
    assert source["source_url"] == CALSPEC_URL
    assert source["target_id"] == "SIRIUS"


def test_other_targets_are_rejected(tmp_path: Path) -> None:
    path = write_fits(tmp_path / "vega_stis_011.fits", target="VEGA")
    with pytest.raises(SpectrumError, match="TARGETID"):
        load_visible_spectrum(path)


# ------------------------------------------------------------------ mappings


def test_bins_and_levels(fits_path: Path) -> None:
    bins, _ = load_visible_spectrum(fits_path)
    assert len(bins) == BIN_COUNT
    assert bins[0].wavelength_nm == 380.5 and bins[-1].wavelength_nm == 779.5
    levels = [bin_.level_normalized for bin_ in bins]
    assert min(levels) == 0.0 and max(levels) == 1.0
    assert all(-36.0 <= bin_.gain_db <= 0.0 for bin_ in bins)
    assert bins[0].midi_start == MIDI_LOW and bins[-1].midi_end == MIDI_HIGH


def test_wavelength_to_midi_anchors() -> None:
    assert wavelength_to_midi(380.0) == 48.0
    assert wavelength_to_midi(580.0) == 60.0
    assert wavelength_to_midi(780.0) == 72.0


def test_scan_starts_on_c3_and_ends_on_c5() -> None:
    pitch = scan_pitch_midi()
    assert pitch[0] == MIDI_LOW and pitch[-1] == MIDI_HIGH
    assert np.all(np.diff(pitch) > 0)


def test_envelope_uses_stated_times_and_returns_to_silence() -> None:
    length = bin_boundaries(BIN_COUNT)[1]
    envelope = bin_envelope(length)
    assert envelope[0] == 0.0 and envelope[-1] == 0.0
    attack = round(0.010 * SAMPLE_RATE_HZ)
    release = round(0.050 * SAMPLE_RATE_HZ)
    assert np.all(envelope[attack : length - release] == 1.0)
    assert envelope[length - release] == 1.0 and envelope[length - release + 1] < 1.0


def test_short_bins_scale_the_envelope() -> None:
    envelope = bin_envelope(100)
    assert len(envelope) == 100 and envelope[0] == 0.0 and envelope[-1] == 0.0


# --------------------------------------------------------------------- audio


def test_render_shape_and_bin_edges_are_silent(fits_path: Path) -> None:
    wav_bytes, provenance = render_sirius(fits_path)
    pcm = _pcm(wav_bytes)
    assert pcm.shape == (FRAME_COUNT, 2)
    assert np.array_equal(pcm[:, 0], pcm[:, 1])
    boundaries = bin_boundaries(BIN_COUNT)
    assert all(pcm[start, 0] == 0 for start in boundaries[:-1])
    assert all(pcm[end - 1, 0] == 0 for end in boundaries[1:])
    assert provenance["audio"]["frames"] == FRAME_COUNT
    assert len(provenance["bins"]) == BIN_COUNT


def test_render_is_deterministic(fits_path: Path) -> None:
    first, first_provenance = render_sirius(fits_path)
    second, second_provenance = render_sirius(fits_path)
    assert first == second
    assert first_provenance == second_provenance
