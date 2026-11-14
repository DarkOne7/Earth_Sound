"""Load and sonify the visible Sirius spectrum from an STScI CALSPEC FITS file.

This is a wavelength scan, not an extension of the time-series engine: wavelength
sets pitch and relative flux sets loudness.  The FITS reader intentionally handles
the small, fixed set of binary-table scalar types used by CALSPEC without adding
an astronomy dependency to the backend.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import re
import struct
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np


CALSPEC_URL = "https://ssb.stsci.edu/cdbs/calspec/sirius_stis_005.fits"
LOWER_WAVELENGTH_NM = 380.0
UPPER_WAVELENGTH_NM = 780.0
BIN_WIDTH_NM = 1.0
BIN_COUNT = int((UPPER_WAVELENGTH_NM - LOWER_WAVELENGTH_NM) / BIN_WIDTH_NM)
MIDI_LOW = 48.0  # C3
MIDI_HIGH = 72.0  # C5
SAMPLE_RATE_HZ = 44_100
DURATION_SECONDS = 30.0
GAIN_FLOOR_DB = -36.0
PEAK_AMPLITUDE = 0.9
ATTACK_SECONDS = 0.010
RELEASE_SECONDS = 0.050


class SpectrumError(ValueError):
    """Raised when the CALSPEC spectrum cannot be rendered as expected."""


@dataclass(frozen=True)
class SpectrumBin:
    wavelength_nm: float
    flux_flam: float
    midi_note: float
    frequency_hz: float
    level_normalized: float
    gain_db: float


def _header_value(card: str) -> Any:
    value = card[10:]
    value = value.split("/", 1)[0].strip()
    if value.startswith("'"):
        return value[1:value.rfind("'")].strip()
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
    cards: list[str] = []
    cursor = offset
    while True:
        block = data[cursor : cursor + 2880]
        if len(block) != 2880:
            raise SpectrumError("Unexpected end of FITS header")
        block_cards = [block[index : index + 80].decode("ascii") for index in range(0, 2880, 80)]
        cards.extend(block_cards)
        cursor += 2880
        if any(card.startswith("END ") for card in block_cards):
            break
    header: dict[str, Any] = {}
    for card in cards:
        key = card[:8].strip()
        if key and key not in {"COMMENT", "HISTORY", "END"} and card[8:10] == "= ":
            header[key] = _header_value(card)
    return header, cursor


_FITS_SCALAR_TYPES: dict[str, tuple[str, int]] = {
    "B": (">B", 1),
    "I": (">h", 2),
    "J": (">i", 4),
    "K": (">q", 8),
    "E": (">f", 4),
    "D": (">d", 8),
}


def _read_sci_table(path: Path) -> tuple[dict[str, np.ndarray], dict[str, Any]]:
    data = path.read_bytes()
    cursor = 0
    while cursor < len(data):
        header, table_start = _read_hdu_header(data, cursor)
        row_size = int(header.get("NAXIS1", 0))
        row_count = int(header.get("NAXIS2", 0))
        table_bytes = row_size * row_count
        if header.get("XTENSION") == "BINTABLE" and header.get("EXTNAME") == "SCI":
            columns: list[tuple[str, str, int, str, int]] = []
            byte_offset = 0
            for index in range(1, int(header["TFIELDS"]) + 1):
                name = str(header[f"TTYPE{index}"]).strip()
                form = str(header[f"TFORM{index}"]).strip()
                match = re.fullmatch(r"(\d*)([A-Z])", form)
                if not match or (match.group(2) not in _FITS_SCALAR_TYPES):
                    raise SpectrumError(f"Unsupported FITS column format {form!r} for {name}")
                repeat = int(match.group(1) or 1)
                struct_format, width = _FITS_SCALAR_TYPES[match.group(2)]
                columns.append((name, struct_format, repeat, match.group(2), byte_offset))
                byte_offset += repeat * width
            if byte_offset != row_size:
                raise SpectrumError("FITS column widths do not match NAXIS1")

            result: dict[str, np.ndarray] = {}
            for name, struct_format, repeat, code, column_offset in columns:
                width = _FITS_SCALAR_TYPES[code][1]
                values = np.empty((row_count, repeat), dtype=np.float64)
                for row_index in range(row_count):
                    start = table_start + row_index * row_size + column_offset
                    values[row_index] = struct.unpack_from(
                        f"{struct_format[0]}{repeat}{struct_format[1:]}", data, start
                    )
                result[name] = values[:, 0] if repeat == 1 else values
            return result, header
        cursor = table_start + ((table_bytes + 2879) // 2880) * 2880
    raise SpectrumError("No SCI binary table found in the CALSPEC FITS file")


def load_visible_spectrum(path: Path) -> tuple[list[SpectrumBin], dict[str, Any]]:
    """Average good STIS flux into 400 one-nanometre visible-light bins."""
    columns, table_header = _read_sci_table(path)
    if "WAVELENGTH" not in columns or "FLUX" not in columns:
        raise SpectrumError("CALSPEC SCI table must contain WAVELENGTH and FLUX columns")

    wavelength_nm = columns["WAVELENGTH"] / 10.0  # CALSPEC stores Angstroms.
    flux = columns["FLUX"]
    quality = columns.get("DATAQUAL", np.ones_like(flux))
    edges = np.arange(LOWER_WAVELENGTH_NM, UPPER_WAVELENGTH_NM + BIN_WIDTH_NM, BIN_WIDTH_NM)
    binned_flux: list[float] = []
    centers: list[float] = []
    for lower, upper in zip(edges[:-1], edges[1:]):
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
        centers.append(float((lower + upper) / 2))

    flux_array = np.asarray(binned_flux)
    min_flux = float(np.min(flux_array))
    max_flux = float(np.max(flux_array))
    if min_flux <= 0 or max_flux <= min_flux:
        raise SpectrumError("Visible CALSPEC flux must span a positive range")
    log_span = math.log10(max_flux / min_flux)

    bins: list[SpectrumBin] = []
    for wavelength, bin_flux in zip(centers, binned_flux):
        # Wavelength is deliberately transposed into a two-octave C3–C5 scale.
        midi = MIDI_LOW + (wavelength - LOWER_WAVELENGTH_NM) / (
            UPPER_WAVELENGTH_NM - LOWER_WAVELENGTH_NM
        ) * (MIDI_HIGH - MIDI_LOW)
        frequency = 440.0 * 2 ** ((midi - 69.0) / 12.0)
        level = math.log10(bin_flux / min_flux) / log_span
        bins.append(
            SpectrumBin(
                wavelength_nm=wavelength,
                flux_flam=bin_flux,
                midi_note=midi,
                frequency_hz=frequency,
                level_normalized=level,
                gain_db=GAIN_FLOOR_DB * (1 - level),
            )
        )

    return bins, {
        "target_id": "SIRIUS",
        "source_description": "STScI CALSPEC Sirius composite reference spectrum",
        "source_file": path.name,
        "source_url": CALSPEC_URL,
        "source_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "source_flux_scale": "Bohlin et al. 2020, AJ, 160, 21",
        "source_pedigree": "INFLIGHT 1997 to 2022",
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


def _render_wav(bins: list[SpectrumBin]) -> bytes:
    frame_count = round(SAMPLE_RATE_HZ * DURATION_SECONDS)
    boundaries = [round(i * frame_count / len(bins)) for i in range(len(bins) + 1)]
    output = np.zeros((frame_count, 2), dtype=np.float64)
    phase = 0.0
    for index, spectrum_bin in enumerate(bins):
        start, end = boundaries[index], boundaries[index + 1]
        length = end - start
        gain = PEAK_AMPLITUDE * 10 ** (spectrum_bin.gain_db / 20)
        phase_step = 2 * math.pi * spectrum_bin.frequency_hz / SAMPLE_RATE_HZ
        tone = np.sin(phase + phase_step * np.arange(length))
        phase = (phase + phase_step * length) % (2 * math.pi)
        attack = min(round(ATTACK_SECONDS * SAMPLE_RATE_HZ), length // 2)
        release = min(round(RELEASE_SECONDS * SAMPLE_RATE_HZ), length // 2)
        envelope = np.ones(length, dtype=np.float64)
        if attack:
            envelope[:attack] = np.arange(attack, dtype=np.float64) / attack
        if release:
            envelope[-release:] = np.arange(release, 0, -1, dtype=np.float64) / release
        mono = tone * envelope * gain
        output[start:end, 0] = mono
        output[start:end, 1] = mono

    pcm = np.rint(np.clip(output, -1, 1) * 32767).astype("<i2")
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
    provenance = {
        "sonification": "Sirius visible spectrum wavelength scan",
        "processing_version": "1.0.0",
        "references": [
            {
                "name": "STScI CALSPEC catalogue",
                "url": "https://www.stsci.edu/hst/instrumentation/reference-data-for-calibration-and-tools/astronomical-catalogs/calspec",
                "used_for": "Dataset description and Sirius composite spectrum pedigree.",
            },
            {
                "name": "STScI CALSPEC Sirius spectrum",
                "url": CALSPEC_URL,
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
                "at 780 nm; one semitone spans 16.667 nm. This is an audible encoding, not the light's "
                "physical frequency."
            ),
            "loudness": (
                "Mean calibrated flux in each 1 nm bin is normalized logarithmically between the minimum "
                "and maximum binned visible flux, then mapped linearly to -36..0 dB. A brighter bin is louder."
            ),
            "duration": f"{DURATION_SECONDS:g} seconds total; {DURATION_SECONDS / len(bins):.3f} seconds per bin.",
            "timbre": "Pure sine tone; stereo channels are identical and centered.",
            "silence": "No silence is inserted; bins with missing/invalid flux would be rejected.",
        },
        "audio": {
            "container": "WAV (RIFF)",
            "encoding": "PCM signed 16-bit little-endian",
            "sample_rate_hz": SAMPLE_RATE_HZ,
            "channels": 2,
            "duration_seconds": DURATION_SECONDS,
            "frames": round(SAMPLE_RATE_HZ * DURATION_SECONDS),
            "sha256": hashlib.sha256(wav_bytes).hexdigest(),
        },
        "bins": [
            {
                "index": index,
                "wavelength_nm": bin_.wavelength_nm,
                "flux_flam": bin_.flux_flam,
                "midi_note": bin_.midi_note,
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
