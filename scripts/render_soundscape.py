"""Render the reproducible ~30-second soundscape from an OSCAR sample.

Usage (from the repository root, with the backend installed):

    python scripts/render_soundscape.py data/raw/<sample>/<sample>.nc
    python scripts/render_soundscape.py data/raw/<sample>/<sample>.nc --mode region --output-dir data/processed

Writes ``<output-dir>/<sample>.<mode>.wav`` and ``<sample>.<mode>.soundscape.json``
(the provenance). ``--provenance-copy`` also copies the provenance to a second
directory, used to commit it under ``data/metadata/`` as a record. Running the
script twice on the same file gives byte-identical outputs (checked by the
script when the files already exist, unless ``--overwrite``).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.services.current_processing import (
    InsufficientDataError,
    process_current_series,
)
from app.services.series_extraction import extract_default_series
from app.services.sonification import (
    SonificationError,
    default_sonification_settings,
    render_soundscape,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("sample", type=Path, help="Combined NetCDF sample written by fetch_ocean_sample.py")
    parser.add_argument("--mode", choices=("point", "region"), default="point")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "data" / "processed")
    parser.add_argument("--provenance-copy", type=Path, default=None, help="Also write the provenance JSON here.")
    parser.add_argument("--duration", type=float, default=None, help="Seconds (default: settings, 30).")
    parser.add_argument("--no-direction", action="store_true", help="Disable the stereo pan from direction.")
    parser.add_argument("--overwrite", action="store_true", help="Replace existing outputs instead of comparing.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not args.sample.is_file():
        print(f"error: sample file not found: {args.sample}", file=sys.stderr)
        return 2

    settings = default_sonification_settings()
    updates = {}
    if args.duration is not None:
        updates["duration_seconds"] = args.duration
    if args.no_direction:
        updates["direction_mapping"] = "none"
    if updates:
        settings = settings.model_copy(update=updates)

    try:
        series = process_current_series(extract_default_series(args.sample, mode=args.mode))
        rendered = render_soundscape(series, settings)
    except InsufficientDataError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    except (SonificationError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    stem = f"{args.sample.stem}.{args.mode}"
    args.output_dir.mkdir(parents=True, exist_ok=True)
    wav_path = args.output_dir / f"{stem}.wav"
    provenance_path = args.output_dir / f"{stem}.soundscape.json"
    provenance_json = rendered.provenance.model_dump_json(indent=2) + "\n"

    if wav_path.exists() and not args.overwrite:
        if wav_path.read_bytes() == rendered.wav_bytes:
            print(f"unchanged: {wav_path} is byte-identical to this render")
        else:
            print(f"error: {wav_path} exists and differs; use --overwrite", file=sys.stderr)
            return 1
    else:
        wav_path.write_bytes(rendered.wav_bytes)
        print(f"wrote {wav_path}")

    provenance_path.write_text(provenance_json, encoding="utf-8")
    print(f"wrote {provenance_path}")
    if args.provenance_copy is not None:
        args.provenance_copy.mkdir(parents=True, exist_ok=True)
        copy_path = args.provenance_copy / provenance_path.name
        copy_path.write_text(provenance_json, encoding="utf-8")
        print(f"wrote {copy_path}")

    output = rendered.provenance.output
    source = rendered.provenance.source
    print(
        f"{output.duration_seconds:.3f} s, {output.sample_rate_hz} Hz, {output.channels} ch, "
        f"{output.bit_depth}-bit, {output.byte_count} bytes, sha256 {output.sha256}"
    )
    print(f"segments: {source.total_samples} total, {source.usable_samples} audible, {source.silent_samples} silent")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
