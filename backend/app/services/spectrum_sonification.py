"""Load and sonify the visible Sirius spectrum from an STScI CALSPEC FITS file.

This is a wavelength scan, not an extension of the time-series engine: wavelength
sets pitch and relative flux sets loudness.  The FITS reader intentionally handles
the small, fixed set of binary-table scalar types used by CALSPEC without adding
an astronomy dependency to the backend.
"""

from __future__ import annotations

import hashlib
import io
import itertools
import json
import math
import re
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

CALSPEC_FILE_NAME = "sirius_stis_005.fits"
CALSPEC_URL = f"https://ssb.stsci.edu/cdbs/calspec/{CALSPEC_FILE_NAME}"
EXPECTED_TARGET_ID = "SIRIUS"
LOWER_WAVELENGTH_NM = 380.0
UPPER_WAVELENGTH_NM = 780.0
BIN_WIDTH_NM = 1.0
BIN_COUNT = int((UPPER_WAVELENGTH_NM - LOWER_WAVELENGTH_NM) / BIN_WIDTH_NM)
MIDI_LOW = 48.0  # C3
MIDI_HIGH = 72.0  # C5
SAMPLE_RATE_HZ = 44_100
DURATION_SECONDS = 30.0
FRAME_COUNT = round(SAMPLE_RATE_HZ * DURATION_SECONDS)
GAIN_FLOOR_DB = -36.0
PEAK_AMPLITUDE = 0.9
ATTACK_SECONDS = 0.010
RELEASE_SECONDS = 0.050
PROCESSING_VERSION = "1.1.0"

FITS_BLOCK_BYTES = 2880
FITS_CARD_BYTES = 80

DETERMINISM_STATEMENT = (
    "No randomness is used. An identical input file gives byte-identical WAV bytes and "
    "provenance on the same platform and NumPy build (tested). Across platforms, "
    "floating-point libm differences may change isolated PCM samples by at most 1 LSB; "
    "two renders are considered equivalent when their provenance (excluding audio.sha256) "
    "is identical and every PCM sample differs by at most 1 LSB."
)


class SpectrumError(ValueError):
    """Raised when the CALSPEC spectrum cannot be rendered as expected."""


@dataclass(frozen=True)
class SpectrumBin:
    wavelength_nm: float
    flux_flam: float
    midi_note: float
    midi_start: float
    midi_end: float
    frequency_hz: float
    level_normalized: float
    gain_db: float


def wavelength_to_midi(wavelength_nm: float) -> float:
    """Linear wavelength-to-pitch map: 380 nm is C3 (48), 780 nm is C5 (72)."""
    fraction = (wavelength_nm - LOWER_WAVELENGTH_NM) / (UPPER_WAVELENGTH_NM - LOWER_WAVELENGTH_NM)
    return MIDI_LOW + fraction * (MIDI_HIGH - MIDI_LOW)


def midi_to_hz(midi: float | np.ndarray) -> float | np.ndarray:
    return 440.0 * 2 ** ((midi - 69.0) / 12.0)


# --------------------------------------------------------------------------- FITS


def _header_value(card: str) -> Any:
    """Parse the value of a FITS ``KEY = value / comment`` card.

    String values are read up to their closing quote first, so a ``/`` inside a
    string (``'HST/STIS'``) is kept; doubled quotes (``''``) are an escaped quote.
    """
    raw = card[10:]
    stripped = raw.lstrip()
    if stripped.startswith("'"):
        chars: list[str] = []
        index = 1
        while index < len(stripped):
            char = stripped[index]
            if char == "'":
                if stripped[index + 1 : index + 2] == "'":
                    chars.append("'")
                    index += 2
                    continue
                return "".join(chars).rstrip()
            chars.append(char)
            index += 1
        raise SpectrumError(f"Unterminated FITS string value in card {card[:8].strip()!r}")
    value = raw.split("/", 1)[0].strip()
    if value in {"T", "F"}:
        return value == "T"
    try:
        return int(value)
    except ValueError:
        try:
            return float(value.replace("D", "E"))
        except ValueError:
            return value


def _read_hdu_header(data: bytes, offset: int) -> tuple[dict[str, Any], int]:
    header: dict[str, Any] = {}
    cursor = offset
    while True:
        block = data[cursor : cursor + FITS_BLOCK_BYTES]
        if len(block) != FITS_BLOCK_BYTES:
            raise SpectrumError("Unexpected end of FITS header")
        cursor += FITS_BLOCK_BYTES
        for index in range(0, FITS_BLOCK_BYTES, FITS_CARD_BYTES):
            card = block[index : index + FITS_CARD_BYTES].decode("ascii")
            key = card[:8].strip()
            if key == "END":
                return header, cursor
            if key and key not in {"COMMENT", "HISTORY"} and card[8:10] == "= ":
                header[key] = _header_value(card)


def _hdu_data_bytes(header: dict[str, Any]) -> int:
    """Data size per the FITS standard: |BITPIX|/8 * GCOUNT * (PCOUNT + NAXIS1*...*NAXISn)."""
    naxis = int(header.get("NAXIS", 0))
    if naxis == 0:
        return 0
    try:
        bitpix = int(header["BITPIX"])
        element_count = math.prod(int(header[f"NAXIS{axis}"]) for axis in range(1, naxis + 1))
    except KeyError as error:
        raise SpectrumError(f"FITS header is missing required keyword {error.args[0]}") from error
    pcount = int(header.get("PCOUNT", 0))
    gcount = int(header.get("GCOUNT", 1))
    return abs(bitpix) // 8 * gcount * (pcount + element_count)


def _padded(size: int) -> int:
    return -(-size // FITS_BLOCK_BYTES) * FITS_BLOCK_BYTES


_FITS_SCALAR_TYPES: dict[str, str] = {
    "B": "u1",
    "I": ">i2",
    "J": ">i4",
    "K": ">i8",
    "E": ">f4",
    "D": ">f8",
}


def _decode_bintable(
    data: bytes, header: dict[str, Any], table_start: int
) -> dict[str, np.ndarray]:
    if int(header.get("BITPIX", 0)) != 8 or int(header.get("NAXIS", 0)) != 2:
        raise SpectrumError("SCI binary table must have BITPIX = 8 and NAXIS = 2")
    row_size = int(header["NAXIS1"])
    row_count = int(header["NAXIS2"])
    names: list[str] = []
    formats: list[Any] = []
    offsets: list[int] = []
    byte_offset = 0
    for index in range(1, int(header["TFIELDS"]) + 1):
        name = str(header[f"TTYPE{index}"]).strip()
        form = str(header[f"TFORM{index}"]).strip()
        match = re.fullmatch(r"(\d*)([A-Z])", form)
        if not match or match.group(2) not in _FITS_SCALAR_TYPES:
            raise SpectrumError(f"Unsupported FITS column format {form!r} for {name}")
        repeat = int(match.group(1) or 1)
        dtype = np.dtype(_FITS_SCALAR_TYPES[match.group(2)])
        names.append(name)
        formats.append(dtype if repeat == 1 else (dtype, (repeat,)))
        offsets.append(byte_offset)
        byte_offset += repeat * dtype.itemsize
    if byte_offset != row_size:
        raise SpectrumError("FITS column widths do not match NAXIS1")
    if table_start + row_size * row_count > len(data):
        raise SpectrumError("FITS binary table extends past the end of the file")

    row_dtype = np.dtype(
        {"names": names, "formats": formats, "offsets": offsets, "itemsize": row_size}
    )
    table = np.frombuffer(data, dtype=row_dtype, count=row_count, offset=table_start)
    return {name: table[name].astype(np.float64) for name in names}


def _read_calspec(
    data: bytes,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, np.ndarray]]:
    """Return (primary header, SCI header, SCI columns)."""
    primary_header: dict[str, Any] | None = None
    cursor = 0
    while cursor < len(data):
        header, data_start = _read_hdu_header(data, cursor)
        if primary_header is None:
            primary_header = header
        elif header.get("XTENSION") == "BINTABLE" and header.get("EXTNAME") == "SCI":
            return primary_header, header, _decode_bintable(data, header, data_start)
        cursor = data_start + _padded(_hdu_data_bytes(header))
    raise SpectrumError("No SCI binary table found in the CALSPEC FITS file")


# ------------------------------------------------------------------- spectrum


def _load_bins(data: bytes, file_name: str) -> tuple[list[SpectrumBin], dict[str, Any]]:
    primary_header, table_header, columns = _read_calspec(data)
    # CALSPEC files carry the target and pedigree in the primary header; some
    # writers repeat them on the SCI extension, so fall back to it.
    def keyword(name: str) -> Any:
        return primary_header.get(name, table_header.get(name))

    target_id = str(keyword("TARGETID") or "").strip()
    if target_id.upper() != EXPECTED_TARGET_ID:
        raise SpectrumError(
            f"FITS TARGETID is {target_id!r}; this renderer only handles {EXPECTED_TARGET_ID}"
        )
    if "WAVELENGTH" not in columns or "FLUX" not in columns:
        raise SpectrumError("CALSPEC SCI table must contain WAVELENGTH and FLUX columns")

    wavelength_nm = columns["WAVELENGTH"] / 10.0  # CALSPEC stores Angstroms.
    flux = columns["FLUX"]
    quality = columns.get("DATAQUAL", np.ones_like(flux))
    edges = LOWER_WAVELENGTH_NM + BIN_WIDTH_NM * np.arange(BIN_COUNT + 1)
    binned_flux: list[float] = []
    for lower, upper in itertools.pairwise(edges):
        selected = (
            (wavelength_nm >= lower)
            & (wavelength_nm < upper)
            & (quality == 1)
            & np.isfinite(flux)
            & (flux > 0)
        )
        if not np.any(selected):
            raise SpectrumError(f"No good CALSPEC flux samples in {lower:g}–{upper:g} nm")
        binned_flux.append(float(np.mean(flux[selected])))

    min_flux = min(binned_flux)
    max_flux = max(binned_flux)
    if max_flux <= min_flux:
        raise SpectrumError("Visible CALSPEC flux must span a positive range")
    log_span = math.log10(max_flux / min_flux)

    bins: list[SpectrumBin] = []
    for lower, upper, bin_flux in zip(edges[:-1], edges[1:], binned_flux):
        center = float((lower + upper) / 2)
        midi = wavelength_to_midi(center)
        level = min(1.0, max(0.0, math.log10(bin_flux / min_flux) / log_span))
        bins.append(
            SpectrumBin(
                wavelength_nm=center,
                flux_flam=bin_flux,
                midi_note=midi,
                midi_start=wavelength_to_midi(float(lower)),
                midi_end=wavelength_to_midi(float(upper)),
                frequency_hz=float(midi_to_hz(midi)),
                level_normalized=level,
                gain_db=GAIN_FLOOR_DB * (1 - level),
            )
        )

    pedigree = keyword("PEDIGREE")
    flux_scale = keyword("SOURCE")
    return bins, {
        "target_id": target_id,
        "source_description": "STScI CALSPEC Sirius composite reference spectrum",
        "source_file": file_name,
        # Only the documented file has a known download location.
        "source_url": CALSPEC_URL if file_name == CALSPEC_FILE_NAME else None,
        "source_sha256": hashlib.sha256(data).hexdigest(),
        "source_flux_scale": None if flux_scale is None else str(flux_scale).strip(),
        "source_pedigree": None if pedigree is None else str(pedigree).strip(),
        "spectrum_extension": "SCI binary table",
        "input_wavelength_unit": "Angstrom",
        "input_flux_unit": "FLAM (erg s-1 cm-2 Angstrom-1)",
        "quality_rule": "Only DATAQUAL=1 rows with finite positive flux are used.",
        "selected_wavelength_nm": [LOWER_WAVELENGTH_NM, UPPER_WAVELENGTH_NM],
        "bin_width_nm": BIN_WIDTH_NM,
        "bin_count": len(bins),
        "flux_min_flam": min_flux,
        "flux_max_flam": max_flux,
        "flux_normalization": "log10(F/F_min) / log10(F_max/F_min), clipped to [0,1]",
        "spectrum_table_units": {
            "wavelength": table_header.get("TUNIT1", "Angstroms"),
            "flux": table_header.get("TUNIT2", "FLAM"),
        },
    }


def load_visible_spectrum(path: Path) -> tuple[list[SpectrumBin], dict[str, Any]]:
    """Average good STIS flux into 400 one-nanometre visible-light bins."""
    return _load_bins(path.read_bytes(), path.name)


# ---------------------------------------------------------------------- audio


def bin_boundaries(bin_count: int, frame_count: int = FRAME_COUNT) -> list[int]:
    return [round(index * frame_count / bin_count) for index in range(bin_count + 1)]


def bin_envelope(length: int) -> np.ndarray:
    """Linear attack from 0 and linear release that ends exactly at 0.

    The 10 ms attack and 50 ms release are used as stated; only if a bin were
    shorter than both together are they scaled down proportionally.
    """
    attack = round(ATTACK_SECONDS * SAMPLE_RATE_HZ)
    release = round(RELEASE_SECONDS * SAMPLE_RATE_HZ)
    if attack + release > length:
        attack = length * attack // (attack + release)
        release = length - attack
    envelope = np.ones(length, dtype=np.float64)
    if attack:
        envelope[:attack] = np.arange(attack, dtype=np.float64) / attack
    if release:
        envelope[length - release :] = np.arange(release - 1, -1, -1, dtype=np.float64) / max(
            release - 1, 1
        )
    return envelope


def scan_pitch_midi(frame_count: int = FRAME_COUNT) -> np.ndarray:
    """Per-sample pitch: a continuous glide from exactly C3 (first sample) to C5 (last)."""
    return np.linspace(MIDI_LOW, MIDI_HIGH, frame_count, dtype=np.float64)


def _render_wav(bins: list[SpectrumBin]) -> bytes:
    frequency = midi_to_hz(scan_pitch_midi())
    phase = 2 * np.pi * (np.cumsum(frequency) - frequency[0]) / SAMPLE_RATE_HZ
    tone = np.sin(phase)
    amplitude = np.zeros(FRAME_COUNT, dtype=np.float64)
    boundaries = bin_boundaries(len(bins))
    for index, spectrum_bin in enumerate(bins):
        start, end = boundaries[index], boundaries[index + 1]
        gain = PEAK_AMPLITUDE * 10 ** (spectrum_bin.gain_db / 20)
        amplitude[start:end] = bin_envelope(end - start) * gain
    mono = tone * amplitude

    pcm_mono = np.rint(np.clip(mono, -1, 1) * 32767).astype("<i2")
    pcm = np.column_stack((pcm_mono, pcm_mono))
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(2)
        writer.setsampwidth(2)
        writer.setframerate(SAMPLE_RATE_HZ)
        writer.writeframes(np.ascontiguousarray(pcm).tobytes())
    return buffer.getvalue()


def render_sirius(path: Path) -> tuple[bytes, dict[str, Any]]:
    """Create a 30-second wavelength scan and machine-readable provenance."""
    bins, source = load_visible_spectrum(path)
    wav_bytes = _render_wav(bins)
    seconds_per_bin = DURATION_SECONDS / len(bins)
    provenance = {
        "sonification": "Sirius visible spectrum wavelength scan",
        "processing_version": PROCESSING_VERSION,
        "determinism": DETERMINISM_STATEMENT,
        "references": [
            {
                "name": "STScI CALSPEC catalogue",
                "url": "https://www.stsci.edu/hst/instrumentation/reference-data-for-calibration-and-tools/astronomical-catalogs/calspec",
                "used_for": "Dataset description and Sirius composite spectrum pedigree.",
            },
            {
                "name": "STScI CALSPEC Sirius spectrum",
                "url": source["source_url"],
                "used_for": "Wavelength and absolutely calibrated flux input; file header metadata.",
            },
            {
                "name": "Bohlin (2014), HST CALSPEC Flux Standards: Sirius (and Vega)",
                "url": "https://arxiv.org/abs/1403.6861",
                "used_for": "Published Sirius absolute-flux calibration context.",
            },
            {
                "name": "STScI HST ETC spectral distribution reference",
                "url": "https://etc.stsci.edu/etcstatic/users_guide/1_ref_2_spectral_distribution.html",
                "used_for": "Sirius identification as an A1V CALSPEC standard.",
            },
        ],
        "source": source,
        "mappings": {
            "order": "Wavelength bins ascend from violet (380 nm) to red (780 nm).",
            "pitch": (
                "Wavelength is mapped linearly to equal-tempered MIDI C3 (48) at 380 nm through C5 (72) "
                "at 780 nm; one semitone spans 16.667 nm. Pitch glides continuously across the scan, "
                "so each 1 nm bin sweeps from its lower-edge to its upper-edge pitch (0.06 semitone) "
                "and the first and last samples are exactly C3 and C5. This is an audible encoding, "
                "not the light's physical frequency."
            ),
            "loudness": (
                "Mean calibrated flux in each 1 nm bin is normalized logarithmically between the minimum "
                "and maximum binned visible flux, then mapped linearly to -36..0 dB. A brighter bin is louder."
            ),
            "envelope": (
                f"Each bin is one pulse: {ATTACK_SECONDS * 1000:g} ms linear attack from silence, "
                f"sustain, then {RELEASE_SECONDS * 1000:g} ms linear release ending at silence."
            ),
            "duration": f"{DURATION_SECONDS:g} seconds total; {seconds_per_bin:.3f} seconds per bin.",
            "timbre": "Pure sine tone; stereo channels are identical and centered.",
            "silence": "No silence is inserted; bins with missing/invalid flux would be rejected.",
        },
        "audio": {
            "container": "WAV (RIFF)",
            "encoding": "PCM signed 16-bit little-endian",
            "sample_rate_hz": SAMPLE_RATE_HZ,
            "channels": 2,
            "duration_seconds": DURATION_SECONDS,
            "frames": FRAME_COUNT,
            "sha256": hashlib.sha256(wav_bytes).hexdigest(),
        },
        "bins": [
            {
                "index": index,
                "wavelength_nm": bin_.wavelength_nm,
                "flux_flam": bin_.flux_flam,
                "midi_note": bin_.midi_note,
                "midi_start": bin_.midi_start,
                "midi_end": bin_.midi_end,
                "frequency_hz": bin_.frequency_hz,
                "level_normalized": bin_.level_normalized,
                "gain_db": bin_.gain_db,
                "start_seconds": index * DURATION_SECONDS / len(bins),
                "end_seconds": (index + 1) * DURATION_SECONDS / len(bins),
            }
            for index, bin_ in enumerate(bins)
        ],
    }
    return wav_bytes, provenance


def write_sirius_render(
    source: Path, output_dir: Path, metadata_dir: Path | None = None
) -> tuple[Path, Path, dict[str, Any]]:
    wav_bytes, provenance = render_sirius(source)
    output_dir.mkdir(parents=True, exist_ok=True)
    wav_path = output_dir / "sirius-visible-spectrum.wav"
    provenance_path = output_dir / "sirius-visible-spectrum.soundscape.json"
    wav_path.write_bytes(wav_bytes)
    rendered_json = json.dumps(provenance, indent=2, ensure_ascii=False) + "\n"
    provenance_path.write_text(rendered_json, encoding="utf-8")
    if metadata_dir is not None:
        metadata_dir.mkdir(parents=True, exist_ok=True)
        (metadata_dir / provenance_path.name).write_text(rendered_json, encoding="utf-8")
    return wav_path, provenance_path, provenance
