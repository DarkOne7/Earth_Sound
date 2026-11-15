"""Measure local processing and generation time for the bounded MVP samples.

Times each pipeline stage separately, per domain, on the real samples under
``data/raw/``:

    ocean   extract (NetCDF -> u/v series)  ->  process (speed, direction, flags)  ->  render (WAV)
    covid   process (CSV -> weekly series)                                         ->  render (WAV)
    sirius  render (FITS -> spectrum scan WAV, including parsing)
    api     a cold request (empty render cache) and a warm one, through FastAPI's TestClient

Each stage runs ``--repeat`` times after one untimed warm-up run. The script reports
min / median / max wall-clock seconds and the peak Python heap allocation of one run
(``tracemalloc``, measured separately so it does not slow the timed runs).

    python scripts/benchmark_pipeline.py
    python scripts/benchmark_pipeline.py --repeat 20 --output data/metadata/benchmark.json

The method and recorded results are in docs/validation.md.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import statistics
import sys
import time
import tracemalloc
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import numpy
import pandas
import xarray
from app.config import Settings
from app.schemas.case_series import CaseMetric
from app.services.case_processing import process_case_series
from app.services.current_processing import process_current_series
from app.services.series_extraction import extract_default_series
from app.services.sonification import default_sonification_settings, render_soundscape
from app.services.spectrum_sonification import render_sirius


def measure(stage: Callable[[], Any], repeat: int) -> dict[str, float | int]:
    stage()  # warm-up: imports, file cache, numpy first-call costs
    times = []
    for _ in range(repeat):
        start = time.perf_counter()
        stage()
        times.append(time.perf_counter() - start)
    tracemalloc.start()
    stage()
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return {
        "runs": repeat,
        "min_s": round(min(times), 4),
        "median_s": round(statistics.median(times), 4),
        "max_s": round(max(times), 4),
        "peak_python_heap_mb": round(peak / 2**20, 1),
    }


def benchmark(settings: Settings, repeat: int) -> dict[str, dict[str, Any]]:
    results: dict[str, dict[str, Any]] = {}
    sonification = default_sonification_settings()
    ocean = Path(settings.raw_data_dir) / settings.ocean_sample_file
    covid = Path(settings.raw_data_dir) / settings.covid_sample_file
    sirius = Path(settings.raw_data_dir) / settings.sirius_spectrum_file

    if ocean.is_file():
        for mode in ("point", "region"):
            extracted = extract_default_series(ocean, mode=mode, settings=settings)
            processed = process_current_series(extracted)
            results[f"ocean.{mode}.extract"] = measure(
                lambda m=mode: extract_default_series(ocean, mode=m, settings=settings), repeat
            )
            results[f"ocean.{mode}.process"] = measure(lambda e=extracted: process_current_series(e), repeat)
            results[f"ocean.{mode}.render"] = measure(lambda p=processed: render_soundscape(p, sonification), repeat)
            results[f"ocean.{mode}.render"]["samples"] = len(processed.samples)

    if covid.is_file():
        processed = process_case_series(covid, location=settings.covid_location, metric=CaseMetric.DEATHS)
        results["covid.world.process"] = measure(
            lambda: process_case_series(covid, location=settings.covid_location, metric=CaseMetric.DEATHS), repeat
        )
        results["covid.world.render"] = measure(lambda: render_soundscape(processed, sonification), repeat)
        results["covid.world.render"]["samples"] = len(processed.samples)

    if sirius.is_file():
        results["sirius.render"] = measure(lambda: render_sirius(sirius), repeat)

    results.update(benchmark_api(settings, repeat, available={"ocean": ocean, "covid": covid, "sirius": sirius}))
    return results


def benchmark_api(settings: Settings, repeat: int, available: dict[str, Path]) -> dict[str, dict[str, Any]]:
    from app.config import get_settings
    from app.main import app
    from app.services.catalog import render_cache
    from fastapi.testclient import TestClient

    results: dict[str, dict[str, Any]] = {}
    app.dependency_overrides[get_settings] = lambda: settings
    try:
        with TestClient(app) as client:
            for domain, path in available.items():
                if not path.is_file():
                    continue
                url = f"/soundscapes/{domain}/audio"

                def cold(u: str = url) -> None:
                    render_cache.clear()
                    assert client.get(u).status_code == 200

                def warm(u: str = url) -> None:
                    assert client.get(u).status_code == 200

                results[f"api.{domain}.audio.cold"] = measure(cold, repeat)
                client.get(url)
                results[f"api.{domain}.audio.warm"] = measure(warm, repeat)
    finally:
        app.dependency_overrides.clear()
        render_cache.clear()
    return results


def environment() -> dict[str, Any]:
    return {
        "measured_at_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "python": platform.python_version(),
        "implementation": platform.python_implementation(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "processor": platform.processor() or None,
        "cpu_count": os.cpu_count(),
        "numpy": numpy.__version__,
        "pandas": pandas.__version__,
        "xarray": xarray.__version__,
    }


def as_table(results: dict[str, dict[str, Any]]) -> str:
    lines = [
        "| Stage | Median (s) | Min (s) | Max (s) | Peak heap (MB) |",
        "|---|---:|---:|---:|---:|",
    ]
    for name, r in results.items():
        lines.append(
            f"| `{name}` | {r['median_s']:.4f} | {r['min_s']:.4f} | {r['max_s']:.4f} | {r['peak_python_heap_mb']:.1f} |"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repeat", type=int, default=10, help="Timed runs per stage (default 10).")
    parser.add_argument("--output", type=Path, default=None, help="Also write the results as JSON here.")
    args = parser.parse_args(argv)
    if args.repeat < 1:
        parser.error("--repeat must be at least 1")

    settings = Settings(_env_file=None)
    results = benchmark(settings, args.repeat)
    if not results:
        print("No samples found under data/raw/. Fetch them first (see README).", file=sys.stderr)
        return 1
    report = {"environment": environment(), "results": results}
    print(json.dumps(report["environment"], indent=2))
    print(as_table(results))
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(f"Wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
