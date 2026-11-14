"""Fetch a COVID-19 weekly reporting sample from Our World In Data.

The source is Our World In Data's WHO-compiled COVID-19 cases-and-deaths table,
mirrored in the public ``owid/covid-19-data`` repository. It needs no account and
no credentials.

The published table covers every location in one 20 MB file. This script streams
it, hashes the whole stream so the provenance records the SHA-256 of the exact
published artifact, and writes only the rows for the requested locations to
``data/raw/`` (Git-ignored). The subset is what the backend reads.

Usage (from the repository root):

    python scripts/fetch_covid_sample.py
    python scripts/fetch_covid_sample.py --location Italy --location World
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import sys
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

DEFAULT_URL = (
    "https://raw.githubusercontent.com/owid/covid-19-data/master/"
    "public/data/cases_deaths/full_data.csv"
)
DEFAULT_LOCATIONS = ("World",)
DEFAULT_SAMPLE_NAME = "owid-covid-weekly"
#: The published table is ~20 MB. The limit guards against an unexpectedly large
#: upstream file rather than against this known size.
DEFAULT_MAX_BYTES = 64 * 1024 * 1024
CHUNK_BYTES = 1 << 20
REQUIRED_COLUMNS = ("date", "location", "new_cases", "new_deaths")
USER_AGENT = "earth-jukebox-fetcher"


class FetchError(RuntimeError):
    """Raised when the sample cannot be retrieved under the documented rules."""


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default=DEFAULT_URL, help="Published CSV to stream.")
    parser.add_argument(
        "--location",
        action="append",
        dest="locations",
        help=f"Location to keep; repeatable. Default: {', '.join(DEFAULT_LOCATIONS)}",
    )
    parser.add_argument("--sample-name", default=DEFAULT_SAMPLE_NAME)
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    parser.add_argument("--overwrite", action="store_true", help="Replace an existing subset.")
    return parser.parse_args(argv)


def download(url: str, max_bytes: int) -> tuple[bytes, str]:
    """Stream ``url`` into memory, returning its bytes and the SHA-256 of the stream."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    digest = hashlib.sha256()
    buffer = io.BytesIO()
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            if getattr(response, "status", 200) != 200:
                raise FetchError(f"{url} returned HTTP {response.status}")
            while True:
                chunk = response.read(CHUNK_BYTES)
                if not chunk:
                    break
                digest.update(chunk)
                buffer.write(chunk)
                if buffer.tell() > max_bytes:
                    raise FetchError(
                        f"{url} exceeds the {max_bytes} byte limit; raise --max-bytes if this is expected"
                    )
    except urllib.error.URLError as error:
        raise FetchError(f"Could not download {url}: {error}") from error
    if buffer.tell() == 0:
        raise FetchError(f"{url} returned an empty response")
    return buffer.getvalue(), digest.hexdigest()


def filter_locations(raw: bytes, locations: tuple[str, ...]) -> tuple[list[str], list[list[str]], dict[str, int]]:
    """Return the header, the rows for ``locations``, and a per-location row count."""
    text = raw.decode("utf-8-sig")
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        raise FetchError("The downloaded file has no header row") from None
    missing = [column for column in REQUIRED_COLUMNS if column not in header]
    if missing:
        raise FetchError(f"The downloaded file is missing required column(s): {', '.join(missing)}")
    location_index = header.index("location")
    wanted = set(locations)
    counts = dict.fromkeys(locations, 0)
    rows: list[list[str]] = []
    for row in reader:
        if len(row) <= location_index:
            continue
        name = row[location_index]
        if name in wanted:
            rows.append(row)
            counts[name] += 1
    absent = [name for name, count in counts.items() if count == 0]
    if absent:
        raise FetchError(f"No rows found for location(s): {', '.join(absent)}")
    return header, rows, counts


def write_subset(path: Path, header: list[str], rows: list[list[str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(rows)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    locations = tuple(args.locations or DEFAULT_LOCATIONS)
    destination = REPO_ROOT / "data" / "raw" / args.sample_name / f"{args.sample_name}.csv"
    record_path = REPO_ROOT / "data" / "metadata" / f"{args.sample_name}.retrieval.json"

    if destination.exists() and not args.overwrite:
        print(f"Subset already exists; use --overwrite to replace it: {destination}")
        return 0

    try:
        raw, upstream_sha256 = download(args.url, args.max_bytes)
        header, rows, counts = filter_locations(raw, locations)
    except FetchError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    write_subset(destination, header, rows)
    subset_sha256 = hashlib.sha256(destination.read_bytes()).hexdigest()
    dates = sorted({row[header.index("date")] for row in rows})

    record = {
        "retrieved_at_utc": datetime.now(UTC).isoformat(),
        "provider": "Our World In Data (WHO-compiled COVID-19 cases and deaths)",
        "source_url": args.url,
        "upstream_bytes": len(raw),
        "upstream_sha256": upstream_sha256,
        "subset_file": str(destination.relative_to(REPO_ROOT)).replace("\\", "/"),
        "subset_sha256": subset_sha256,
        "subset_rows": len(rows),
        "rows_per_location": counts,
        "date_min": dates[0],
        "date_max": dates[-1],
        "columns": header,
        "license": "Our World In Data: CC BY 4.0. Underlying data compiled from WHO.",
        "citation": (
            "Edouard Mathieu, Hannah Ritchie, Lucas Rodes-Guirao et al. (2020) "
            "'Coronavirus Pandemic (COVID-19)'. Published online at OurWorldInData.org. "
            "Underlying data: World Health Organization."
        ),
    }
    record_path.parent.mkdir(parents=True, exist_ok=True)
    record_path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")

    print(f"wrote {destination} ({len(rows)} rows, {', '.join(f'{k}={v}' for k, v in counts.items())})")
    print(f"wrote {record_path}")
    print(f"upstream {len(raw)} bytes, sha256 {upstream_sha256}")
    print(f"dates {dates[0]} .. {dates[-1]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
