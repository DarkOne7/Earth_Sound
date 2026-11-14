"""Fetch a bounded OSCAR sample from NASA Earthdata.

Default access is PO.DAAC Cloud OPeNDAP (DAP4 index subsetting). NASA Harmony's
subsetter (HOSS) cannot spatially subset OSCAR v2.0: its u/v dimensions
(``latitude``/``longitude``) have no same-named coordinate variables (the
coordinates are ``lat``/``lon``), so HOSS treats them as projected and rejects
both bbox and named-dimension requests. Harmony remains available with
``--access harmony`` for unsubsetted or other collections.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
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
# Harmony capabilities for C2098858642-POCLOUD (sds/HOSS-geographic) list a single
# output format: NETCDF-4 with mimeType "application/netcdf".
DEFAULT_OUTPUT_FORMAT = "application/netcdf"
# Harmony job statuses (nasa/harmony JobStatus). Only "successful" is a clean finish.
JOB_SUCCESS_STATUSES = frozenset({"successful"})
JOB_FAILURE_STATUSES = frozenset({"failed", "canceled", "cancelled", "complete_with_errors"})
NETCDF_SUFFIXES = (".nc", ".nc4", ".netcdf", ".h5")
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
    parser.add_argument(
        "--access",
        choices=("opendap", "harmony"),
        default="opendap",
        help="opendap (default): PO.DAAC Cloud OPeNDAP index subsetting; harmony: NASA Harmony OGC Coverages",
    )
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
    """Harmony OGC API - Coverages rangeset URL.

    Path template from nasa/harmony's ogc-api-coverages-v1.0.0.yml:
    ``/{cmrCollection}/ogc-api-coverages/1.0.0/collections/{variables}/coverage/rangeset``,
    where the OGC "collection" is a comma-separated list of UMM-Var variable names.
    """
    variable_path = quote(",".join(variables), safe=",")
    return (
        f"{HARMONY_BASE_URL}/{quote(collection_id, safe='')}/"
        f"ogc-api-coverages/1.0.0/collections/{variable_path}/coverage/rangeset"
    )


def _request_params(
    *,
    granule_name: str | None,
    start_date: str | None,
    end_date: str | None,
    bbox: tuple[float, float, float, float] | None,
) -> list[tuple[str, str]]:
    """Harmony query parameters; the bbox is sent as Harmony's lat()/lon() axes."""
    params: list[tuple[str, str]] = [("format", DEFAULT_OUTPUT_FORMAT)]
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


OPENDAP_HOST = "opendap.earthdata.nasa.gov"
DAP4_NS = "{http://xml.opendap.org/ns/DAP/4.0#}"
DAP4_NON_VARIABLE_TAGS = {"Dimension", "Attribute", "Dim", "Map", "Group", "Value"}


@dataclass(frozen=True)
class DmrVariable:
    name: str
    dims: tuple[str, ...]
    attributes: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class DmrStructure:
    dimensions: dict[str, int]
    variables: dict[str, DmrVariable]


def _strip_ns(tag: str) -> str:
    return tag.split("}", 1)[-1]


def parse_dmr(xml_text: str) -> DmrStructure:
    """Parse the root group of a DAP4 DMR: dimension sizes, variable dims and attributes."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise FetchError(f"OPeNDAP returned an unreadable DMR ({exc}); this is usually a login page.") from exc
    if _strip_ns(root.tag) != "Dataset":
        raise FetchError("OPeNDAP response is not a DAP4 DMR document.")
    dimensions: dict[str, int] = {}
    variables: dict[str, DmrVariable] = {}
    for child in root:
        tag = _strip_ns(child.tag)
        name = child.get("name")
        if tag == "Dimension" and name:
            dimensions[name.lstrip("/")] = int(child.get("size", "0"))
        elif tag not in DAP4_NON_VARIABLE_TAGS and name:
            dims = tuple(
                str(dim.get("name", "")).lstrip("/") for dim in child if _strip_ns(dim.tag) == "Dim"
            )
            attributes = {}
            for attribute in child:
                if _strip_ns(attribute.tag) == "Attribute" and attribute.get("name"):
                    values = [value.text or "" for value in attribute if _strip_ns(value.tag) == "Value"]
                    attributes[str(attribute.get("name"))] = " ".join(values).strip()
            variables[name] = DmrVariable(name=name, dims=dims, attributes=attributes)
    return DmrStructure(dimensions=dimensions, variables=variables)


@dataclass(frozen=True)
class GridLayout:
    latitude_variable: str
    longitude_variable: str
    time_variable: str | None
    latitude_dim: str
    longitude_dim: str


def _find_coordinate(structure: DmrStructure, standard_name: str, units: set[str], axis: str) -> DmrVariable:
    matches = [
        var
        for var in structure.variables.values()
        if len(var.dims) == 1
        and (
            var.attributes.get("standard_name", "").lower() == standard_name
            or var.attributes.get("units", "").lower() in units
            or var.attributes.get("axis", "").upper() == axis
        )
    ]
    if len(matches) != 1:
        found = ", ".join(var.name for var in matches) or "none"
        raise FetchError(f"Could not identify a unique {standard_name} coordinate in the DMR (found: {found}).")
    return matches[0]


def resolve_grid_layout(structure: DmrStructure, variables: Iterable[str]) -> GridLayout:
    """Identify lat/lon/time coordinates from CF attributes and check the requested variables use them."""
    lat = _find_coordinate(structure, "latitude", {"degrees_north", "degree_north"}, "Y")
    lon = _find_coordinate(structure, "longitude", {"degrees_east", "degree_east"}, "X")
    try:
        time_var: DmrVariable | None = _find_coordinate(structure, "time", set(), "T")
    except FetchError:
        time_var = None
    for name in variables:
        variable = structure.variables.get(name)
        if variable is None:
            raise FetchError(f"Variable {name!r} is not in the granule DMR.")
        if lat.dims[0] not in variable.dims or lon.dims[0] not in variable.dims:
            raise FetchError(
                f"Variable {name!r} has dims {variable.dims}, which do not include the coordinate "
                f"dimensions {lat.dims[0]!r}/{lon.dims[0]!r}."
            )
    return GridLayout(
        latitude_variable=lat.name,
        longitude_variable=lon.name,
        time_variable=time_var.name if time_var else None,
        latitude_dim=lat.dims[0],
        longitude_dim=lon.dims[0],
    )


def coordinate_index_range(values: Any, low: float, high: float, *, name: str) -> tuple[int, int]:
    """Inclusive index range of the contiguous coordinate values inside [low, high]."""
    import numpy as np

    array = np.asarray(values, dtype=float)
    indices = np.flatnonzero((array >= low) & (array <= high))
    if indices.size == 0:
        raise FetchError(f"No {name} grid values fall inside [{low}, {high}].")
    if indices[-1] - indices[0] + 1 != indices.size:
        raise FetchError(f"The {name} values inside [{low}, {high}] are not contiguous; cannot index-subset.")
    return int(indices[0]), int(indices[-1])


def bbox_index_ranges(
    bbox: tuple[float, float, float, float], latitudes: Any, longitudes: Any
) -> tuple[tuple[int, int], tuple[int, int], tuple[float, float]]:
    """Map a WGS84 bbox to (lat index range, lon index range, native lon bounds)."""
    import numpy as np

    west, south, east, north = bbox
    if float(np.nanmax(longitudes)) > 180.0:
        west, east = west % 360.0, east % 360.0
        if west > east:
            raise FetchError(
                "The bbox crosses 0 deg longitude, the seam of this dataset's 0..360 grid; "
                "split it into two requests."
            )
    lat_range = coordinate_index_range(latitudes, south, north, name="latitude")
    lon_range = coordinate_index_range(longitudes, west, east, name="longitude")
    return lat_range, lon_range, (west, east)


def dap4_constraint(
    structure: DmrStructure,
    layout: GridLayout,
    variables: Iterable[str],
    lat_range: tuple[int, int],
    lon_range: tuple[int, int],
) -> str:
    """DAP4 constraint expression selecting the index window for each variable and its coordinates."""

    def hyperslab(dims: tuple[str, ...]) -> str:
        parts = []
        for dim in dims:
            if dim == layout.latitude_dim:
                parts.append(f"[{lat_range[0]}:{lat_range[1]}]")
            elif dim == layout.longitude_dim:
                parts.append(f"[{lon_range[0]}:{lon_range[1]}]")
            else:
                size = structure.dimensions.get(dim)
                if not size:
                    raise FetchError(f"DMR does not declare a size for dimension {dim!r}.")
                parts.append(f"[0:{size - 1}]")
        return "".join(parts)

    clauses = [f"/{name}{hyperslab(structure.variables[name].dims)}" for name in variables]
    clauses.append(f"/{layout.latitude_variable}[{lat_range[0]}:{lat_range[1]}]")
    clauses.append(f"/{layout.longitude_variable}[{lon_range[0]}:{lon_range[1]}]")
    if layout.time_variable:
        clauses.append(f"/{layout.time_variable}")
    return ";".join(clauses)


def granule_opendap_url(entry: dict[str, Any]) -> str:
    """The granule's OPeNDAP URL from its CMR links."""
    for link in entry.get("links") or []:
        href = str(link.get("href", ""))
        parsed = urlparse(href)
        if parsed.scheme == "https" and parsed.hostname == OPENDAP_HOST and "/granules/" in parsed.path:
            return href
    raise FetchError(f"CMR lists no OPeNDAP link for granule {entry.get('title')!r}; try --access harmony.")


def _get_opendap(session: requests.Session, url: str, **kwargs: Any) -> requests.Response:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != OPENDAP_HOST:
        raise FetchError("Refusing to send Earthdata credentials to a non-OPeNDAP host.")
    response = session.get(url, timeout=90, **kwargs)
    if response.status_code in (401, 403):
        response.close()
        raise FetchError(
            f"OPeNDAP refused the request (HTTP {response.status_code}). Check that EARTHDATA_TOKEN is valid "
            "and that your Earthdata profile has accepted the PO.DAAC data-use terms."
        )
    return response


def _fetch_via_opendap(
    args: argparse.Namespace,
    *,
    session: requests.Session,
    granules: list[dict[str, Any]],
    bbox: tuple[float, float, float, float] | None,
    output_dir: Path,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    import xarray as xr

    if not bbox:
        raise FetchError("OPeNDAP access needs a bbox; use --access harmony for unsubsetted requests.")
    urls = [granule_opendap_url(entry) for entry in granules]

    dmr_response = _get_opendap(session, urls[0] + ".dmr.xml")
    dmr_response.raise_for_status()
    structure = parse_dmr(dmr_response.text)
    layout = resolve_grid_layout(structure, args.variables)

    output_dir.mkdir(parents=True, exist_ok=True)
    coords_path = output_dir / ".coordinates.nc4"
    coords_ce = f"/{layout.latitude_variable};/{layout.longitude_variable}"
    _stream_response(
        _get_opendap(session, urls[0] + ".dap.nc4", params={"dap4.ce": coords_ce}, stream=True),
        coords_path,
        max_bytes=args.max_bytes,
    )
    try:
        with xr.open_dataset(coords_path) as coords:
            latitudes = coords[layout.latitude_variable].values
            longitudes = coords[layout.longitude_variable].values
    finally:
        coords_path.unlink(missing_ok=True)
    lat_range, lon_range, native_lon = bbox_index_ranges(bbox, latitudes, longitudes)
    constraint = dap4_constraint(structure, layout, args.variables, lat_range, lon_range)

    outputs: list[dict[str, Any]] = []
    total_written = 0
    try:
        for entry, url in zip(granules, urls, strict=True):
            name = re.sub(r"[^A-Za-z0-9._-]", "_", str(entry["title"]))
            destination = output_dir / f"{name}.subset.nc4"
            if destination.exists():
                raise FetchError(
                    f"Output already exists without a matching retrieval record: {destination}. "
                    "Move it aside or choose a different --output-dir."
                )
            response = _get_opendap(session, url + ".dap.nc4", params={"dap4.ce": constraint}, stream=True)
            try:
                result = _stream_response(
                    response,
                    destination,
                    max_bytes=args.max_bytes,
                    already_written=total_written,
                    allow_large=args.allow_large,
                )
            finally:
                response.close()
            result["path"] = _portable_path(destination)
            result["role"] = "granule"
            result["source_url"] = url
            outputs.append(result)
            total_written += result["bytes"]
    except Exception:
        for output in outputs:
            _resolve_recorded_path(output["path"]).unlink(missing_ok=True)
        raise

    request_info = {
        "access_method": "PO.DAAC Cloud OPeNDAP (Hyrax) DAP4 index subsetting",
        "opendap_constraint": constraint,
        "grid_layout": {
            "latitude_variable": layout.latitude_variable,
            "longitude_variable": layout.longitude_variable,
            "time_variable": layout.time_variable,
            "latitude_dimension": layout.latitude_dim,
            "longitude_dimension": layout.longitude_dim,
            "source": f"DAP4 DMR of {urls[0]}",
        },
        "index_ranges": {
            "latitude": list(lat_range),
            "longitude": list(lon_range),
            "latitude_values": [float(latitudes[lat_range[0]]), float(latitudes[lat_range[1]])],
            "longitude_values": [float(longitudes[lon_range[0]]), float(longitudes[lon_range[1]])],
            "native_longitude_bounds": list(native_lon),
        },
    }
    return outputs, request_info


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


def _is_netcdf_link(link: dict[str, Any]) -> bool:
    link_type = str(link.get("type", "")).lower()
    path = urlparse(str(link.get("href", ""))).path.lower()
    return "netcdf" in link_type or path.endswith(NETCDF_SUFFIXES)


def _data_links(payload: dict[str, Any]) -> list[str]:
    """Result data links: ``rel == "data"`` and NetCDF by MIME type or file suffix."""
    return [
        str(link["href"])
        for link in payload.get("links", [])
        if isinstance(link, dict) and link.get("href") and link.get("rel") == "data" and _is_netcdf_link(link)
    ]


def _job_status(payload: dict[str, Any]) -> str:
    return str(payload.get("status", "")).strip().lower()


MAX_REPORTED_JOB_ERRORS = 5


def _job_error_details(payload: dict[str, Any]) -> str:
    """Job ID, workflow UI link and the distinct per-granule error messages."""
    lines = []
    job_id = payload.get("jobID") or payload.get("jobId")
    if job_id:
        safe_id = quote(str(job_id), safe="")
        lines.append(f"Job ID: {job_id}")
        lines.append(f"Job details: {HARMONY_BASE_URL}/workflow-ui/{safe_id}")
    errors = [item for item in payload.get("errors") or [] if isinstance(item, dict)]
    if errors:
        counts: dict[str, int] = {}
        for item in errors:
            message = " ".join(str(item.get("message", "no message")).split())[:400]
            counts[message] = counts.get(message, 0) + 1
        lines.append(f"Granule errors ({len(errors)} total, {len(counts)} distinct):")
        for message, count in list(counts.items())[:MAX_REPORTED_JOB_ERRORS]:
            lines.append(f"  - [{count}x] {message}")
        if len(counts) > MAX_REPORTED_JOB_ERRORS:
            lines.append(f"  - ... {len(counts) - MAX_REPORTED_JOB_ERRORS} more distinct message(s)")
    return ("\n" + "\n".join(lines)) if lines else ""


def _raise_for_failed_job(payload: dict[str, Any]) -> None:
    status = _job_status(payload)
    if status == "complete_with_errors":
        raise FetchError(
            "Harmony job completed with errors, so some granules are missing; no partial "
            "sample was saved." + _job_error_details(payload)
        )
    if status in JOB_FAILURE_STATUSES:
        raise FetchError(
            f"Harmony job {status}: {payload.get('message') or 'no message'}" + _job_error_details(payload)
        )


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
        _raise_for_failed_job(payload)
        # Only a "successful" status means every work item finished; a running job
        # can already list data links for the items done so far.
        if _job_status(payload) in JOB_SUCCESS_STATUSES:
            links = _data_links(payload)
            if not links:
                raise FetchError("Harmony job completed but returned no NetCDF data links.")
            return links
        sleep(poll_seconds)
    raise FetchError(f"Harmony job did not finish within {timeout_seconds:g} seconds.")


def _unique_filenames(urls: list[str]) -> list[str]:
    """Safe, unique file names; colliding basenames get their index as a prefix."""
    names: list[str] = []
    used: set[str] = set()
    for index, url in enumerate(urls, start=1):
        name = _safe_filename(url, index)
        if name.lower() in used:
            name = f"{index:03d}-{name}"
        used.add(name.lower())
        names.append(name)
    return names


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


def _fetch_via_harmony(
    args: argparse.Namespace,
    *,
    session: requests.Session,
    collection_id: str,
    bbox: tuple[float, float, float, float] | None,
    output_dir: Path,
    sample_name: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
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
        _raise_for_failed_job(payload)
        status = _job_status(payload)
        # A payload with a non-final status must be polled even if it already
        # lists data links; those would be a partial result.
        links = _data_links(payload) if (status in JOB_SUCCESS_STATUSES or not status) else []
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
            for url, filename in zip(output_urls, _unique_filenames(output_urls), strict=True):
                parsed_url = urlparse(url)
                if parsed_url.scheme != "https" or not parsed_url.hostname:
                    raise FetchError("Harmony returned a non-HTTPS data link; refusing to download it.")
                destination = output_dir / filename
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
                result["role"] = "granule"
                outputs.append(result)
                total_written += result["bytes"]
    except Exception:
        for output in outputs:
            _resolve_recorded_path(output["path"]).unlink(missing_ok=True)
        raise
    finally:
        response.close()

    return outputs, {
        "access_method": "NASA Harmony OGC API - Coverages",
        "harmony_request_url": request_url,
        "harmony_request_parameters": params,
    }


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
        "access": args.access,
        "variables": list(args.variables),
        "output_dir": _portable_path(output_dir),
        "max_bytes": args.max_bytes,
        "allow_large": bool(args.allow_large),
    }
    existing = _matching_existing_record(record_path, selection)
    if existing:
        print(f"Sample already matches its retrieval record; leaving it unchanged: {record_path}")
        return _ordered_output_paths(existing)

    if session is None:
        session = create_authenticated_session()

    if args.access == "opendap":
        outputs, request_info = _fetch_via_opendap(
            args, session=session, granules=granules, bbox=bbox, output_dir=output_dir
        )
    else:
        outputs, request_info = _fetch_via_harmony(
            args, session=session, collection_id=collection_id, bbox=bbox, output_dir=output_dir,
            sample_name=sample_name,
        )

    if args.access == "opendap" or len(outputs) > 1:
        # Each daily granule is subset separately; combine them locally into the
        # single sample file the inspector and backend loader expect.
        combined = output_dir / f"{sample_name}.nc"
        try:
            result = combine_outputs([_resolve_recorded_path(o["path"]) for o in outputs], combined)
        except Exception:
            for output in outputs:
                _resolve_recorded_path(output["path"]).unlink(missing_ok=True)
            raise
        result["path"] = _portable_path(combined)
        result["role"] = "combined"
        outputs.append(result)

    record = {
        "retrieved_at_utc": datetime.now(timezone.utc).isoformat(),
        "provider": "NASA PO.DAAC",
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
        **request_info,
        "python_version": sys.version.split()[0],
        "requests_version": requests.__version__,
        "outputs": outputs,
    }
    record_path.parent.mkdir(parents=True, exist_ok=True)
    record_path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    for output in outputs:
        print(f"Saved {output['path']} ({output['bytes']} bytes, SHA-256 {output['sha256']})")
    print(f"Retrieval record: {record_path}")
    return _ordered_output_paths(outputs)


def _ordered_output_paths(outputs: list[dict[str, Any]]) -> list[Path]:
    """Output paths with the combined sample (if any) first."""
    ordered = sorted(outputs, key=lambda item: item.get("role") != "combined")
    return [_resolve_recorded_path(item["path"]) for item in ordered]


def _time_dimension(dataset: Any) -> str:
    for variable in dataset.variables.values():
        attrs = variable.attrs
        if variable.ndim == 1 and (
            str(attrs.get("standard_name", "")).lower() == "time" or str(attrs.get("axis", "")).upper() == "T"
        ):
            return str(variable.dims[0])
    if "time" in dataset.dims:
        return "time"
    raise FetchError("Cannot combine outputs: no time dimension identified by standard_name, axis or name.")


def _require_matching_static_variables(datasets: list[Any], time_dim: str) -> None:
    """Variables without the time dimension (e.g. lat/lon) must be identical in every file.

    OSCAR's ``lat``/``lon`` are not index coordinates (their dimensions are named
    ``latitude``/``longitude``), so xarray's ``join="exact"`` would not compare them.
    """
    import numpy as np

    first = datasets[0]
    names = set(first.variables)
    for other in datasets[1:]:
        if set(other.variables) != names:
            raise FetchError("Cannot combine outputs: grids or variables differ between files (variable sets).")
        for name in names:
            if time_dim in first[name].dims:
                continue
            a, b = first[name].values, other[name].values
            same = a.shape == b.shape and (
                np.array_equal(a, b, equal_nan=True) if np.issubdtype(a.dtype, np.number) else np.array_equal(a, b)
            )
            if not same:
                raise FetchError(f"Cannot combine outputs: grids or variables differ between files ({name!r}).")


def combine_outputs(paths: list[Path], destination: Path) -> dict[str, Any]:
    """Concatenate per-granule NetCDF outputs along time into one file.

    Grids must match exactly (``join="exact"``); timestamps must be unique. The
    per-granule files are kept unchanged for provenance.
    """
    import xarray as xr  # local import: only needed for multi-granule results

    if destination.exists():
        raise FetchError(f"Combined output already exists without a matching retrieval record: {destination}.")
    datasets = []
    try:
        for path in paths:
            with xr.open_dataset(path) as opened:
                datasets.append(opened.load())
        time_dim = _time_dimension(datasets[0])
        _require_matching_static_variables(datasets, time_dim)
        try:
            combined = xr.concat(
                datasets, dim=time_dim, data_vars="minimal", coords="minimal", compat="override", join="exact"
            )
        except ValueError as exc:
            raise FetchError(f"Cannot combine outputs: grids or variables differ between files ({exc}).") from exc
        combined = combined.sortby(time_dim)
        times = combined[time_dim].values
        if len(set(times.tolist())) != len(times):
            raise FetchError("Cannot combine outputs: duplicate timestamps across files.")
        combined.attrs["history"] = (
            f"{combined.attrs.get('history', '')}\n{datetime.now(timezone.utc).isoformat()} "
            f"Earth Jukebox: concatenated {len(paths)} subset granule outputs along {time_dim}"
        ).strip()
        # Keep each variable's original fill convention: xarray would otherwise add
        # _FillValue=NaN to float variables (e.g. time/lat/lon) that had none.
        # An explicit encoding replaces the variable's whole encoding, so carry the
        # original storage keys (units, calendar, dtype) across as well.
        preserved_keys = ("units", "calendar", "dtype")
        encoding = {
            name: {
                **{key: datasets[0][name].encoding[key] for key in preserved_keys if key in datasets[0][name].encoding},
                "_FillValue": None,
            }
            for name in combined.variables
            if name in datasets[0].variables and "_FillValue" not in datasets[0][name].encoding
        }
        combined.to_netcdf(destination, encoding=encoding)
    finally:
        for dataset in datasets:
            dataset.close()
    return {
        "bytes": destination.stat().st_size,
        "sha256": _sha256(destination),
        "source_count": len(paths),
    }


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
