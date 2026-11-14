"""Render a reproducible ~30-second soundscape from a fetched sample.

Two data sources are supported:

    # Ocean surface currents (OSCAR v2.0), one pulse per day
    python scripts/render_soundscape.py ocean data/raw/<sample>/<sample>.nc
    python scripts/render_soundscape.py ocean data/raw/<sample>/<sample>.nc --mode region

    # COVID-19 reported deaths or cases, one pulse per reporting week
    python scripts/render_soundscape.py covid data/raw/owid-covid-weekly/owid-covid-weekly.csv
    python scripts/render_soundscape.py covid <csv> --location Italy --metric cases

Writes ``<output-dir>/<stem>.wav`` and ``<stem>.soundscape.json`` (the provenance).
``--provenance-copy`` also writes the provenance to a second directory, used to
commit it under ``data/metadata/`` as a record of the reference render. Running
the script twice gives byte-identical output; it compares instead of overwriting
unless ``--overwrite`` is passed.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.config import get_settings
from app.schemas.case_series import CaseMetric
from app.services.case_processing import CaseProcessingError, process_case_series
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
    parser.add_argument("source", choices=("ocean", "covid"), help="Which data domain to render.")
    parser.add_argument("sample", type=Path, help="Fetched sample file (.nc for ocean, .csv for covid).")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "data" / "processed")
    parser.add_argument("--provenance-copy", type=Path, default=None, help="Also write the provenance JSON here.")
    parser.add_argument("--duration", type=float, default=None, help="Seconds (default: settings, 30).")
    parser.add_argument("--no-pan", action="store_true", help="Disable the stereo pan mapping.")
    parser.add_argument(
        "--uniform-timing",
        action="store_true",
        help="Give every sample an equal share of the duration instead of speeding the rhythm up "
        "through rapid change. Keeps audio time proportional to elapsed data time.",
    )
    parser.add_argument(
        "--rate-sensitivity",
        type=float,
        default=None,
        help="Strength of the rate-driven rhythm (default 3.0). Higher means more contrast.",
    )
    parser.add_argument("--overwrite", action="store_true", help="Replace existing outputs instead of comparing.")

    ocean = parser.add_argument_group("ocean")
    ocean.add_argument("--mode", choices=("point", "region"), default="point")

    covid = parser.add_argument_group("covid")
    covid.add_argument("--location", default=None, help="Default: settings (World).")
    covid.add_argument("--metric", choices=("deaths", "cases"), default=None, help="Default: settings (deaths).")
    return parser.parse_args(argv)


def build_series(args: argparse.Namespace):
    """Return the processed series and the output file stem for the chosen source."""
    settings = get_settings()
    if args.source == "ocean":
        series = process_current_series(extract_default_series(args.sample, mode=args.mode))
        return series, f"{args.sample.stem}.{args.mode}"

    location = args.location or settings.covid_location
    metric = CaseMetric(args.metric or settings.covid_metric)
    record = _retrieval_record(args.sample)
    series = process_case_series(
        args.sample,
        location=location,
        metric=metric,
        minimum_usable_samples=settings.covid_minimum_usable_samples,
        source_sha256=record.get("subset_sha256"),
        source_url=record.get("source_url"),
        retrieved_at=record.get("retrieved_at_utc"),
    )
    slug = location.lower().replace(" ", "-")
    return series, f"{args.sample.stem}.{slug}.{metric.value}"


def _retrieval_record(sample: Path) -> dict:
    """Provenance written by the fetcher, if it is next to the sample."""
    path = sample.resolve().parents[2] / "metadata" / f"{sample.stem}.retrieval.json"
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not args.sample.is_file():
        print(f"error: sample file not found: {args.sample}", file=sys.stderr)
        return 2

    settings = default_sonification_settings()
    updates = {}
    if args.duration is not None:
        updates["duration_seconds"] = args.duration
    if args.no_pan:
        updates["pan_enabled"] = False
    if args.uniform_timing:
        updates["timing_mode"] = "uniform"
    if args.rate_sensitivity is not None:
        updates["rate_sensitivity"] = args.rate_sensitivity
    if updates:
        settings = settings.model_copy(update=updates)

    try:
        series, stem = build_series(args)
        rendered = render_soundscape(series, settings)
    except (InsufficientDataError, CaseProcessingError, SonificationError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

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
    timing = rendered.provenance.timing
    print(
        f"{output.duration_seconds:.3f} s, {output.sample_rate_hz} Hz, {output.channels} ch, "
        f"{output.bit_depth}-bit, {output.byte_count} bytes, sha256 {output.sha256}"
    )
    print(
        f"segments: {source.total_samples} total, {source.audible_samples} audible, "
        f"{source.silent_samples} silent"
    )
    print(
        f"rhythm ({timing.timing_mode}): {timing.pulse_rate_hz_mean:.2f} pulses/s mean, up to "
        f"{timing.pulse_rate_hz_max:.2f} /s; segments {timing.segment_seconds_min * 1000:.0f}-"
        f"{timing.segment_seconds_max * 1000:.0f} ms"
    )
    gains = [segment.gain_db for segment in rendered.provenance.segments if segment.gain_db is not None]
    if gains:
        print(f"gain range: {min(gains):.1f} to {max(gains):.1f} dB")
    if not timing.heard_as_rhythm:
        print(
            f"warning: the fastest stretch reaches {timing.pulse_rate_hz_max:.1f} pulses/s, above the "
            f"{timing.rhythm_floor_hz:.0f} Hz rhythm floor, so pulses fuse into a rough tone there "
            "instead of being heard separately. Aggregate the series, raise --duration, or lower "
            "--rate-sensitivity.",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
