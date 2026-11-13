"""Fetch a bounded OSCAR sample through NASA Harmony."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote, urljoin, urlparse

import requests

CMR_BASE_URL = "https://cmr.earthdata.nasa.gov/search"
HARMONY_BASE_URL = "https://harmony.earthdata.nasa.gov"
DEFAULT_COLLECTION = "OSCAR_L4_OC_FINAL_V2.0"
DEFAULT_VERSION = "2.0"
DEFAULT_START = "2020-01-01"
DEFAULT_END = "2020-01-30"
DEFAULT_BBOX = (-152.0, 0.0, -150.0, 2.0)
DEFAULT_VARIABLES = ("u", "v")
DEFAULT_MAX_BYTES = 25 * 1024 * 1024
MAX_GRANULES = 100
CHUNK_BYTES = 64 * 1024
REPO_ROOT = Path(__file__).resolve().parents[1]
# Classic NetCDF (CDF1/2/5) and NetCDF-4 (HDF5) file signatures.
NETCDF_SIGNATURES = (b"CDF\x01", b"CDF\x02", b"CDF\x05", b"\x89HDF\r\n\x1a\n")
SIGNATURE_BYTES = 8


class FetchError(RuntimeError):
    """A safe, actionable fetch error that does not contain credentials."""


class CredentialError(FetchError):
    """Raised when Earthdata Login credentials are unavailable."""


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--collection", default=DEFAULT_COLLECTION)
    parser.add_argument("--version", default=DEFAULT_VERSION)
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--granule-name", help="Exact readable CMR granule name")
    selection.add_argument("--start-date", default=DEFAULT_START, help="UTC date, YYYY-MM-DD")
    parser.add_argument("--end-date", default=DEFAULT_END, help="UTC date, YYYY-MM-DD")
    parser.add_argument(
        "--bbox",
        nargs=4,
        type=float,
        metavar=("WEST", "SOUTH", "EAST", "NORTH"),
        default=DEFAULT_BBOX,
        help="Optional WGS84 bounding box; omit with --no-bbox",
    )
    parser.add_argument("--no-bbox", action="store_true", help="Request no spatial subset")
    parser.add_argument("--variables", nargs="+", default=list(DEFAULT_VARIABLES))
    parser.add_argument("--output-dir", type=Path, help="Defaults to data/raw/<sample-name>")
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    parser.add_argument(
        "--allow-large",
        action="store_true",
        help="Allow output beyond --max-bytes; use only after reviewing the size",
    )
    parser.add_argument("--poll-seconds", type=float, default=3.0)
    parser.add_argument("--job-timeout-seconds", type=float, default=1200.0)
    args = parser.parse_args(argv)
    if args.max_bytes <= 0:
        parser.error("--max-bytes must be a positive integer")
    if args.poll_seconds <= 0 or args.job_timeout_seconds <= 0:
        parser.error("poll and job timeout values must be positive")
    if args.granule_name:
        args.start_date = None
        args.end_date = None
    try:
        start = datetime.strptime(args.start_date, "%Y-%m-%d").date() if args.start_date else None
        end = datetime.strptime(args.end_date, "%Y-%m-%d").date() if args.end_date else None
    except ValueError as exc:
        parser.error(f"dates must use YYYY-MM-DD: {exc}")
    if start and end and start > end:
        parser.error("--start-date must be on or before --end-date")
    # Normalize to zero-padded ISO dates so CMR and Harmony always get valid timestamps.
    args.start_date = start.isoformat() if start else None
    args.end_date = end.isoformat() if end else None
    if args.no_bbox:
        args.bbox = None
    elif args.bbox:
        west, south, east, north = args.bbox
        if west >= east or south >= north or south < -90 or north > 90:
            parser.error("bbox must satisfy west < east and -90 <= south < north <= 90")
    if not args.variables or any(not re.fullmatch(r"[A-Za-z0-9_:-]+", v) for v in args.variables):
        parser.error("--variables must contain one or more simple variable names")
    return args


def _get_feed_entries(payload: dict[str, Any]) -> list[dict[str, Any]]:
    entries = payload.get("feed", {}).get("entry", [])
    if isinstance(entries, dict):
        return [entries]
    return entries if isinstance(entries, list) else []


def find_collection(
    session: requests.Session, short_name: str, version: str
) -> dict[str, Any]:
    response = session.get(
        f"{CMR_BASE_URL}/collections.json",
        params={"short_name": short_name, "version": version, "page_size": 10},
        timeout=30,
    )
    response.raise_for_status()
    entries = _get_feed_entries(response.json())
    if not entries:
        raise FetchError(f"CMR did not find {short_name!r}, version {version!r}.")
    if len(entries) > 1:
        raise FetchError(
            f"CMR returned multiple collections for {short_name!r}, version {version!r}; select a unique collection."
        )
    entry = entries[0]
    if not entry.get("id"):
        raise FetchError("CMR collection metadata did not include a collection concept ID.")
    return entry


def search_granules(
    session: requests.Session,
    collection_id: str,
    *,
    granule_name: str | None,
    start_date: str | None,
    end_date: str | None,
    bbox: tuple[float, float, float, float] | None,
) -> list[dict[str, Any]]:
    params: dict[str, Any] = {
        "collection_concept_id": collection_id,
        "page_size": MAX_GRANULES,
        "sort_key": "start_date",
    }
    if granule_name:
        params["readable_granule_name"] = granule_name
    elif start_date and end_date:
        params["temporal"] = (
            f"{start_date}T00:00:00Z,{end_date}T23:59:59Z"
        )
    if bbox:
        west, south, east, north = bbox
        params["bounding_box"] = f"{west},{south},{east},{north}"

    response = session.get(f"{CMR_BASE_URL}/granules.json", params=params, timeout=45)
    response.raise_for_status()
    entries = _get_feed_entries(response.json())
    if not entries:
        raise FetchError("CMR found no granules for the requested selection.")
    if len(entries) >= MAX_GRANULES:
        raise FetchError(
            f"The selection reached the {MAX_GRANULES}-granule safety limit; narrow the time range."
        )
    if any(not entry.get("title") for entry in entries):
        raise FetchError("CMR returned a granule without a readable name.")
    return entries


def _earthdata_netrc_credentials_available() -> bool:
    try:
        return requests.utils.get_netrc_auth("https://urs.earthdata.nasa.gov") is not None
    except (OSError, IOError):
        return False


def create_authenticated_session() -> requests.Session:
    """Use a bearer token or requests' local netrc support; never log either."""
    token = os.environ.get("EARTHDATA_TOKEN", "").strip()
    if not token and not _earthdata_netrc_credentials_available():
        raise CredentialError(
            "Earthdata Login credentials are missing. Create an Earthdata Login account, "
            "then set EARTHDATA_TOKEN in this shell or configure a local .netrc/_netrc "
            "entry for urs.earthdata.nasa.gov. No credentials were sent or printed."
        )
    session = requests.Session()
    session.trust_env = True
    if token:
        session.headers.update({"Authorization": f"Bearer {token}"})
    session.headers.setdefault("User-Agent", "EarthJukebox/0.1 dataset-sample-fetcher")
    return session


def _request_url(collection_id: str, variables: Iterable[str]) -> str:
    variable_path = quote(",".join(variables), safe=",")
    return (
        f"{HARMONY_BASE_URL}/{quote(collection_id, safe='')}/"
        f"ogc-api-coverages/1.0.0/{variable_path}/coverage/rangeset"
    )


def _request_params(
    *,
    granule_name: str | None,
    start_date: str | None,
    end_date: str | None,
    bbox: tuple[float, float, float, float] | None,
) -> list[tuple[str, str]]:
    params: list[tuple[str, str]] = [("format", "application/x-netcdf4")]
    if granule_name:
        params.append(("granuleName", granule_name))
    elif start_date and end_date:
        params.append(
            (
                "subset",
                f'time("{start_date}T00:00:00Z":"{end_date}T23:59:59Z")',
            )
        )
    if bbox:
        west, south, east, north = bbox
        params.extend(
            [
                ("subset", f"lat({south}:{north})"),
                ("subset", f"lon({west}:{east})"),
            ]
        )
    return params


def _size_mb(entry: dict[str, Any]) -> float | None:
    value = entry.get("granule_size")
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _check_catalog_size(
    granules: list[dict[str, Any]], *, bbox: Any, max_bytes: int, allow_large: bool
) -> float | None:
    sizes = [_size_mb(entry) for entry in granules]
    if any(size is None for size in sizes):
        if not bbox and not allow_large:
            raise FetchError(
                "CMR did not report a size for every selected granule. A full-granule "
                "download is refused without a verified size; add a bbox or review --allow-large."
            )
        return None
    total_mb = sum(size for size in sizes if size is not None)
    if bbox:
        # Harmony will return only the requested subset. The returned response is
        # separately checked against the hard byte cap before and while writing.
        return total_mb
    if total_mb * 1024 * 1024 > max_bytes and not allow_large:
        raise FetchError(
            f"CMR reports {total_mb:.2f} MB for the selected full granule(s), above the "
            f"{max_bytes / (1024 * 1024):.2f} MiB limit. Add --bbox or explicitly pass --allow-large."
        )
    return total_mb


def _job_url(response: requests.Response, payload: dict[str, Any]) -> str | None:
    location = response.headers.get("Location")
    if location and "/jobs/" in location:
        candidate = urljoin(response.url, location)
        parsed = urlparse(candidate)
        if parsed.scheme != "https" or parsed.hostname != "harmony.earthdata.nasa.gov":
            raise FetchError("Harmony returned an untrusted job URL; refusing to send credentials.")
        return candidate
    job_id = payload.get("jobID") or payload.get("jobId")
    if job_id:
        return f"{HARMONY_BASE_URL}/jobs/{quote(str(job_id), safe='')}"
    return None


def _data_links(payload: dict[str, Any]) -> list[str]:
    links = payload.get("links", [])
    outputs = [
        str(link["href"])
        for link in links
        if isinstance(link, dict)
        and link.get("href")
        and (link.get("rel") == "data" or str(link.get("type", "")).endswith("netcdf"))
    ]
    return outputs


def wait_for_job(
    session: requests.Session,
    job_url: str,
    *,
    poll_seconds: float,
    timeout_seconds: float,
    sleep=time.sleep,
    monotonic=time.monotonic,
) -> list[str]:
    deadline = monotonic() + timeout_seconds
    while monotonic() < deadline:
        response = session.get(job_url, timeout=45)
        response.raise_for_status()
        payload = response.json()
        status = str(payload.get("status", "")).lower()
        if status in {"failed", "canceled", "cancelled", "error"}:
            detail = payload.get("message") or status
            raise FetchError(f"Harmony job failed: {detail}")
        if status in {"successful", "complete", "completed"} or payload.get("progress") == 100:
            if payload.get("errors"):
                raise FetchError("Harmony completed with errors; inspect the job in Earthdata before retrying.")
            links = _data_links(payload)
            if not links:
                raise FetchError("Harmony job completed but returned no NetCDF data links.")
            return links
        sleep(poll_seconds)
    raise FetchError(f"Harmony job did not finish within {timeout_seconds:g} seconds.")


def _safe_filename(url: str, index: int) -> str:
    name = Path(urlparse(url).path).name
    name = re.sub(r"[^A-Za-z0-9._-]", "_", name)
    if not name or name in {".", ".."}:
        name = f"subset-{index:03d}.nc"
    if not name.lower().endswith((".nc", ".nc4", ".netcdf")):
        name += ".nc"
    return name


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _require_netcdf_signature(header: bytes, response: requests.Response) -> None:
    if not any(header.startswith(signature) for signature in NETCDF_SIGNATURES):
        content_type = response.headers.get("Content-Type", "unknown")
        raise FetchError(
            f"Downloaded response is not a NetCDF file (Content-Type {content_type!r}); "
            "this is usually an Earthdata Login page. The partial file was removed."
        )


def _stream_response(
    response: requests.Response,
    destination: Path,
    *,
    max_bytes: int,
    already_written: int = 0,
    allow_large: bool = False,
) -> dict[str, Any]:
    response.raise_for_status()
    remaining_limit = max_bytes - already_written
    declared_size = response.headers.get("Content-Length")
    if declared_size:
        try:
            declared_bytes = int(declared_size)
        except ValueError:
            declared_bytes = None
        if declared_bytes is not None and not allow_large and declared_bytes > remaining_limit:
            raise FetchError(
                f"Remote response declares {declared_bytes} bytes, above the remaining "
                f"{max(remaining_limit, 0)}-byte size limit; refusing download."
            )

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".part")
    total = 0
    header = b""
    digest = hashlib.sha256()
    try:
        with temporary.open("wb") as target:
            for chunk in response.iter_content(chunk_size=CHUNK_BYTES):
                if not chunk:
                    continue
                total += len(chunk)
                if not allow_large and total + already_written > max_bytes:
                    raise FetchError(
                        f"Remote response exceeded the {max_bytes}-byte cap while streaming; "
                        "the partial file was removed."
                    )
                if len(header) < SIGNATURE_BYTES:
                    header += chunk[: SIGNATURE_BYTES - len(header)]
                    if len(header) == SIGNATURE_BYTES:
                        _require_netcdf_signature(header, response)
                target.write(chunk)
                digest.update(chunk)
        if total == 0:
            raise FetchError("Harmony returned an empty data response.")
        _require_netcdf_signature(header, response)
        temporary.replace(destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return {"path": str(destination), "bytes": total, "sha256": digest.hexdigest()}


def _portable_path(path: Path) -> str:
    """Record paths relative to the repo root when possible so records work from any cwd."""
    resolved = path.resolve()
    try:
        return resolved.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return str(resolved)


def _resolve_recorded_path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else REPO_ROOT / path


def _matching_existing_record(record_path: Path, selection: dict[str, Any]) -> list[dict[str, Any]] | None:
    if not record_path.is_file():
        return None
    try:
        record = json.loads(record_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if record.get("selection") != selection:
        return None
    outputs = record.get("outputs")
    if not outputs:
        return None
    for output in outputs:
        path = _resolve_recorded_path(output.get("path", ""))
        if not output.get("path") or not path.is_file() or _sha256(path) != output.get("sha256"):
            return None
    return outputs


def fetch_sample(args: argparse.Namespace, *, session: requests.Session | None = None) -> list[Path]:
    bbox = tuple(args.bbox) if args.bbox else None
    discovery = requests.Session()
    collection = find_collection(discovery, args.collection, args.version)
    collection_id = str(collection["id"])
    granules = search_granules(
        discovery,
        collection_id,
        granule_name=args.granule_name,
        start_date=args.start_date,
        end_date=args.end_date,
        bbox=bbox,
    )
    catalog_size_mb = _check_catalog_size(
        granules,
        bbox=bbox,
        max_bytes=args.max_bytes,
        allow_large=args.allow_large,
    )

    custom_output_dir = _portable_path(args.output_dir) if args.output_dir else None
    sample_name = _sample_name(args, bbox=bbox, custom_output_dir=custom_output_dir)
    output_dir = args.output_dir.resolve() if args.output_dir else REPO_ROOT / "data" / "raw" / sample_name
    record_path = REPO_ROOT / "data" / "metadata" / f"{sample_name}.retrieval.json"
    selection = {
        "collection": args.collection,
        "version": args.version,
        "collection_concept_id": collection_id,
        "granules": [entry["title"] for entry in granules],
        "start_date": args.start_date,
        "end_date": args.end_date,
        "bbox_wgs84": list(bbox) if bbox else None,
        "variables": list(args.variables),
        "output_dir": _portable_path(output_dir),
        "max_bytes": args.max_bytes,
        "allow_large": bool(args.allow_large),
    }
    existing = _matching_existing_record(record_path, selection)
    if existing:
        print(f"Sample already matches its retrieval record; leaving it unchanged: {record_path}")
        return [_resolve_recorded_path(item["path"]) for item in existing]

    if session is None:
        session = create_authenticated_session()

    request_url = _request_url(collection_id, args.variables)
    params = _request_params(
        granule_name=args.granule_name,
        start_date=args.start_date,
        end_date=args.end_date,
        bbox=bbox,
    )
    response = session.get(request_url, params=params, stream=True, timeout=90)
    response.raise_for_status()
    content_type = response.headers.get("Content-Type", "").lower()
    if "json" in content_type:
        payload = response.json()
        response.close()
        links = _data_links(payload)
        if not links:
            job_url = _job_url(response, payload)
            if not job_url:
                raise FetchError(
                    "Harmony returned JSON without a data link or job ID; no data was downloaded."
                )
            links = wait_for_job(
                session,
                job_url,
                poll_seconds=args.poll_seconds,
                timeout_seconds=args.job_timeout_seconds,
            )
        output_urls = links
    else:
        output_urls = [""]

    outputs: list[dict[str, Any]] = []
    total_written = 0
    try:
        if output_urls == [""]:
            filename = f"{sample_name}.nc"
            destination = output_dir / filename
            if destination.exists():
                raise FetchError(
                    f"Output already exists without a matching retrieval record: {destination}. "
                    "Move it aside or choose a different --output-dir."
                )
            result = _stream_response(
                response,
                destination,
                max_bytes=args.max_bytes,
                already_written=total_written,
                allow_large=args.allow_large,
            )
            result["path"] = _portable_path(destination)
            outputs.append(result)
            total_written += result["bytes"]
        else:
            response.close()
            for index, url in enumerate(output_urls, start=1):
                parsed_url = urlparse(url)
                if parsed_url.scheme != "https" or not parsed_url.hostname:
                    raise FetchError("Harmony returned a non-HTTPS data link; refusing to download it.")
                destination = output_dir / _safe_filename(url, index)
                if destination.exists():
                    raise FetchError(
                        f"Output already exists without a matching retrieval record: {destination}. "
                        "Move it aside or choose a different --output-dir."
                    )
                if parsed_url.hostname == "harmony.earthdata.nasa.gov":
                    data_session = session
                else:
                    # Harmony output links may be signed links to another host.
                    # Never forward the bearer token to those hosts.
                    data_session = requests.Session()
                    data_session.trust_env = True
                data_response = data_session.get(url, stream=True, timeout=90)
                result = _stream_response(
                    data_response,
                    destination,
                    max_bytes=args.max_bytes,
                    already_written=total_written,
                    allow_large=args.allow_large,
                )
                result["path"] = _portable_path(destination)
                outputs.append(result)
                total_written += result["bytes"]
    except Exception:
        for output in outputs:
            _resolve_recorded_path(output["path"]).unlink(missing_ok=True)
        raise
    finally:
        response.close()

    record = {
        "retrieved_at_utc": datetime.now(timezone.utc).isoformat(),
        "provider": "NASA PO.DAAC",
        "access_method": "NASA Harmony OGC API - Coverages",
        "cmr_collection_id": collection_id,
        "cmr_granules": [
            {
                "name": entry.get("title"),
                "time_start": entry.get("time_start"),
                "time_end": entry.get("time_end"),
                "size_mb": _size_mb(entry),
            }
            for entry in granules
        ],
        "catalog_full_granules_total_mb": catalog_size_mb,
        "selection": selection,
        "harmony_request_url": request_url,
        "harmony_request_parameters": params,
        "python_version": sys.version.split()[0],
        "requests_version": requests.__version__,
        "outputs": outputs,
    }
    record_path.parent.mkdir(parents=True, exist_ok=True)
    record_path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    for output in outputs:
        print(f"Saved {output['path']} ({output['bytes']} bytes, SHA-256 {output['sha256']})")
    print(f"Retrieval record: {record_path}")
    return [_resolve_recorded_path(item["path"]) for item in outputs]


def _sample_name(
    args: argparse.Namespace,
    *,
    bbox: tuple[float, float, float, float] | None,
    custom_output_dir: str | None,
) -> str:
    """Name the sample from its selection; non-default subsets get a short stable suffix."""
    if args.granule_name:
        selection = re.sub(r"[^A-Za-z0-9-]+", "-", args.granule_name).strip("-")
    else:
        selection = f"{args.start_date.replace('-', '')}-{args.end_date.replace('-', '')}"
    variant = {
        "bbox": list(bbox) if bbox else None,
        "variables": list(args.variables),
        "output_dir": custom_output_dir,
    }
    default_variant = {
        "bbox": list(DEFAULT_BBOX),
        "variables": list(DEFAULT_VARIABLES),
        "output_dir": None,
    }
    if variant != default_variant:
        encoded = json.dumps(variant, sort_keys=True).encode("utf-8")
        selection += "-" + hashlib.sha256(encoded).hexdigest()[:8]
    if args.collection == DEFAULT_COLLECTION:
        return f"oscar-final-{selection}"
    product = re.sub(r"[^a-z0-9]+", "-", args.collection.lower()).strip("-")
    return f"{product}-{selection}"


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        fetch_sample(args)
    except requests.RequestException as exc:
        print(f"Dataset request failed: {exc}", file=sys.stderr)
        return 1
    except FetchError as exc:
        print(f"Dataset fetch blocked: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
