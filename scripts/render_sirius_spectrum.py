"""Render the visible Sirius CALSPEC reference spectrum as a wavelength scan.

Download the public STScI file first (see docs/sirius-spectrum-decision.md), then run:

    python scripts/render_sirius_spectrum.py data/raw/sirius_stis_005.fits
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.services.spectrum_sonification import (
    BIN_COUNT,
    CALSPEC_URL,
    SpectrumError,
    write_sirius_render,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("spectrum", type=Path, help="STScI CALSPEC sirius_stis_005.fits file")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "data" / "processed")
    parser.add_argument("--provenance-copy", type=Path, default=ROOT / "data" / "metadata")
    args = parser.parse_args(argv)
    if not args.spectrum.is_file():
        print(f"error: spectrum file not found: {args.spectrum}", file=sys.stderr)
        print(f"download it from {CALSPEC_URL}", file=sys.stderr)
        return 2
    try:
        wav, provenance_path, provenance = write_sirius_render(
            args.spectrum, args.output_dir, args.provenance_copy
        )
    except (OSError, SpectrumError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(f"wrote {wav}")
    print(f"wrote {provenance_path}")
    print(f"{provenance['audio']['duration_seconds']:.1f} s, {BIN_COUNT} bins")
    print(f"WAV SHA-256: {provenance['audio']['sha256']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
