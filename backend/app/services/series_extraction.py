"""Point/region and time-window extraction of u/v series from an ocean NetCDF file.

Coordinates are identified from CF metadata in the file (``standard_name``,
``units`` or ``axis``), falling back only to the dimension names published in
NASA CMR UMM-Var for OSCAR v2.0 (``time``, ``longitude``, ``latitude``).
"""

from __future__ import annotations

import hashlib
import math
from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import Literal

import numpy as np
import pandas as pd
import xarray as xr

from app.config import Settings, get_settings
from app.schemas.current_series import ExtractedSeries, SampleStatus, SelectionParameters
from app.services.current_processing import component_valid_mask, to_meters_per_second
from app.services.dataset_loader import open_ocean_dataset
from app.services.reference_ranges import DOCUMENTED_COMPONENT_FILL_VALUE

TimeBound = datetime | date | str

_AXIS_RULES = {
    "latitude": {
        "standard_name": "latitude",
        "units": {"degrees_north", "degree_north", "degree_n", "degrees_n", "degreen", "degreesn"},
        "axis": "Y",
        "documented_name": "latitude",
    },
    "longitude": {
        "standard_name": "longitude",
        "units": {"degrees_east", "degree_east", "degree_e", "degrees_e", "degreee", "degreese"},
        "axis": "X",
        "documented_name": "longitude",
    },
    "time": {"standard_name": "time", "units": set(), "axis": "T", "documented_name": "time"},
}


class SeriesExtractionError(ValueError):
    """Raised when a series cannot be extracted under the documented rules."""


def _to_utc(value: TimeBound, *, end_of_day: bool) -> datetime:
    """Parse a time bound to an aware UTC datetime.

    A date-only bound covers the whole day: start -> 00:00:00, end -> 23:59:59.999999.
    Naive datetimes are interpreted as UTC.
    """
    if isinstance(value, str):
        text = value.strip()
        try:
            if len(text) == 10:
                value = date.fromisoformat(text)
            else:
                value = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise SeriesExtractionError(f"Invalid time bound {value!r}: {exc}") from exc
    if isinstance(value, datetime):
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    if isinstance(value, date):
        return datetime.combine(value, time.max if end_of_day else time.min, tzinfo=UTC)
    raise SeriesExtractionError(f"Unsupported time bound type: {type(value).__name__}")


def resolve_coordinate(dataset: xr.Dataset, axis: Literal["latitude", "longitude", "time"]) -> str:
    """Return the name of the 1-D coordinate for ``axis`` from the file's metadata."""
    rules = _AXIS_RULES[axis]
    candidates = []
    for name, variable in dataset.variables.items():
        if variable.ndim != 1:
            continue
        attrs = variable.attrs
        standard_name = str(attrs.get("standard_name", "")).strip().lower()
        units = str(attrs.get("units", "")).strip().lower().replace(" ", "_")
        axis_attr = str(attrs.get("axis", "")).strip().upper()
        if standard_name == rules["standard_name"] or units in rules["units"] or axis_attr == rules["axis"]:
            candidates.append(str(name))
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        raise SeriesExtractionError(f"Ambiguous {axis} coordinate; candidates: {', '.join(candidates)}")
    documented = rules["documented_name"]
    if documented in dataset.variables and dataset[documented].ndim == 1:
        return documented
    raise SeriesExtractionError(
        f"No {axis} coordinate identified by standard_name, units or axis attributes, "
        f"and no 1-D variable named {documented!r}."
    )


def _longitude_in_file_convention(longitude: float, file_longitudes: np.ndarray) -> float:
    if np.nanmax(file_longitudes) > 180.0:
        return longitude % 360.0
    return ((longitude + 180.0) % 360.0) - 180.0


def _grid_step(values: np.ndarray) -> float:
    if values.size < 2:
        return math.inf
    return float(np.median(np.abs(np.diff(values))))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _time_axis(dataset: xr.Dataset, time_name: str) -> list[datetime]:
    values = dataset[time_name].values
    if not np.issubdtype(values.dtype, np.datetime64):
        raise SeriesExtractionError(
            f"Time coordinate {time_name!r} was not decoded to datetimes (dtype {values.dtype})."
        )
    if np.isnat(values).any():
        raise SeriesExtractionError(f"Time coordinate {time_name!r} contains missing timestamps (NaT).")
    return [pd.Timestamp(value).tz_localize("UTC").to_pydatetime() for value in values]


def _select_times(
    timestamps: list[datetime], start: datetime, end: datetime
) -> tuple[np.ndarray, tuple[datetime, ...]]:
    if start > end:
        raise SeriesExtractionError(f"Start {start.isoformat()} is after end {end.isoformat()}")
    indices = np.array([i for i, t in enumerate(timestamps) if start <= t <= end], dtype=int)
    selected = tuple(timestamps[i] for i in indices)
    for position in range(1, len(selected)):
        if selected[position] == selected[position - 1]:
            raise SeriesExtractionError(f"Duplicate timestamp in dataset: {selected[position].isoformat()}")
        if selected[position] < selected[position - 1]:
            raise SeriesExtractionError(
                f"Dataset timestamps are out of order: {selected[position].isoformat()} "
                f"follows {selected[position - 1].isoformat()}"
            )
    if not selected:
        raise SeriesExtractionError(
            f"No timestamps between {start.isoformat()} and {end.isoformat()} in the dataset."
        )
    return indices, selected


def _variable_array(
    dataset: xr.Dataset, name: str, time_dim: str, lat_dim: str, lon_dim: str
) -> xr.DataArray:
    variable = dataset[name]
    required = {time_dim, lat_dim, lon_dim}
    if not required.issubset(variable.dims):
        raise SeriesExtractionError(
            f"Variable {name!r} has dims {variable.dims}; expected {sorted(required)}."
        )
    extra = [dim for dim in variable.dims if dim not in required]
    for dim in extra:
        if variable.sizes[dim] != 1:
            raise SeriesExtractionError(
                f"Variable {name!r} has extra dimension {dim!r} of size {variable.sizes[dim]}; "
                "select it explicitly before processing."
            )
    return variable.squeeze(extra, drop=True).transpose(time_dim, lat_dim, lon_dim)


class _OpenedSample:
    """Shared opening logic: loader, coordinate resolution, time selection."""

    def __init__(self, path: str | Path, u_variable: str, v_variable: str) -> None:
        self.path = Path(path)
        self.dataset, self.metadata = open_ocean_dataset(self.path, required_variables=(u_variable, v_variable))
        try:
            self.lat_name = resolve_coordinate(self.dataset, "latitude")
            self.lon_name = resolve_coordinate(self.dataset, "longitude")
            self.time_name = resolve_coordinate(self.dataset, "time")
            self.lat_dim = self.dataset[self.lat_name].dims[0]
            self.lon_dim = self.dataset[self.lon_name].dims[0]
            self.time_dim = self.dataset[self.time_name].dims[0]
            self.lats = np.asarray(self.dataset[self.lat_name].values, dtype=np.float64)
            self.lons = np.asarray(self.dataset[self.lon_name].values, dtype=np.float64)
            self.u = _variable_array(self.dataset, u_variable, self.time_dim, self.lat_dim, self.lon_dim)
            self.v = _variable_array(self.dataset, v_variable, self.time_dim, self.lat_dim, self.lon_dim)
        except Exception:
            self.dataset.close()
            raise

    def units(self, name: str) -> str:
        units = self.metadata.variables[name].units
        if units is None:
            raise SeriesExtractionError(f"Variable {name!r} has no units attribute.")
        return str(units)

    def source(self, dataset_name: str | None, dataset_version: str | None) -> dict[str, str | None]:
        return {
            "dataset": dataset_name,
            "dataset_version": dataset_version,
            "source_file": self.path.name,
            "source_sha256": _sha256(self.path),
        }


def extract_point_series(
    path: str | Path,
    *,
    latitude: float,
    longitude: float,
    start: TimeBound,
    end: TimeBound,
    u_variable: str = "u",
    v_variable: str = "v",
    dataset_name: str | None = None,
    dataset_version: str | None = None,
) -> ExtractedSeries:
    """Extract the u/v series at the grid point nearest to (latitude, longitude).

    Longitude may be given in either convention; it is converted to the file's.
    Ties in distance resolve to the lowest grid index. Requests farther than one
    grid step from every grid point are rejected.
    """
    if not -90.0 <= latitude <= 90.0:
        raise SeriesExtractionError(f"Latitude {latitude} is outside [-90, 90].")
    start_utc, end_utc = _to_utc(start, end_of_day=False), _to_utc(end, end_of_day=True)
    sample = _OpenedSample(path, u_variable, v_variable)
    try:
        file_lon = _longitude_in_file_convention(longitude, sample.lons)
        lat_distance = np.abs(sample.lats - latitude)
        lon_delta = np.abs(sample.lons - file_lon) % 360.0
        lon_distance = np.minimum(lon_delta, 360.0 - lon_delta)
        i, j = int(np.argmin(lat_distance)), int(np.argmin(lon_distance))
        if lat_distance[i] > _grid_step(sample.lats) or lon_distance[j] > _grid_step(sample.lons):
            raise SeriesExtractionError(
                f"Point ({latitude}, {longitude}) is outside the dataset grid "
                f"(nearest is {sample.lats[i]}, {sample.lons[j]})."
            )
        time_indices, timestamps = _select_times(_time_axis(sample.dataset, sample.time_name), start_utc, end_utc)
        u_values = np.asarray(sample.u.isel({sample.time_dim: time_indices, sample.lat_dim: i, sample.lon_dim: j}).values, dtype=np.float64)
        v_values = np.asarray(sample.v.isel({sample.time_dim: time_indices, sample.lat_dim: i, sample.lon_dim: j}).values, dtype=np.float64)
        return ExtractedSeries(
            timestamps=timestamps,
            u=u_values,
            v=v_values,
            u_units=sample.units(u_variable),
            v_units=sample.units(v_variable),
            u_variable=u_variable,
            v_variable=v_variable,
            selection=SelectionParameters(
                mode="point",
                start=start_utc,
                end=end_utc,
                latitude=latitude,
                longitude=longitude,
                selected_latitude=float(sample.lats[i]),
                selected_longitude=float(sample.lons[j]),
            ),
            source=sample.source(dataset_name, dataset_version),
        )
    finally:
        sample.dataset.close()


def extract_region_series(
    path: str | Path,
    *,
    south: float,
    north: float,
    west: float,
    east: float,
    start: TimeBound,
    end: TimeBound,
    u_variable: str = "u",
    v_variable: str = "v",
    dataset_name: str | None = None,
    dataset_version: str | None = None,
) -> ExtractedSeries:
    """Extract a region-mean u/v series over grid cells inside inclusive bounds.

    Per timestamp: a cell is *present* when both components are non-NaN and not
    the documented fill value. No present cells -> ``missing``. Any present cell
    outside the documented valid range (or infinite) -> ``invalid`` (no averaging
    around bad data). Otherwise u and v are averaged separately over present
    cells; speed is later computed from that mean vector. ``west > east`` crosses
    the antimeridian.
    """
    if not (-90.0 <= south <= north <= 90.0):
        raise SeriesExtractionError(f"Invalid latitude bounds: south={south}, north={north}.")
    start_utc, end_utc = _to_utc(start, end_of_day=False), _to_utc(end, end_of_day=True)
    sample = _OpenedSample(path, u_variable, v_variable)
    try:
        lat_idx = np.flatnonzero((sample.lats >= south) & (sample.lats <= north))
        file_west = _longitude_in_file_convention(west, sample.lons)
        file_east = _longitude_in_file_convention(east, sample.lons)
        if file_west <= file_east:
            lon_mask = (sample.lons >= file_west) & (sample.lons <= file_east)
        else:
            lon_mask = (sample.lons >= file_west) | (sample.lons <= file_east)
        lon_idx = np.flatnonzero(lon_mask)
        if lat_idx.size == 0 or lon_idx.size == 0:
            raise SeriesExtractionError(
                f"No grid cells inside south={south}, north={north}, west={west}, east={east}."
            )
        time_indices, timestamps = _select_times(_time_axis(sample.dataset, sample.time_name), start_utc, end_utc)
        selector = {sample.time_dim: time_indices, sample.lat_dim: lat_idx, sample.lon_dim: lon_idx}
        u_cube = np.asarray(sample.u.isel(selector).values, dtype=np.float64).reshape(len(timestamps), -1)
        v_cube = np.asarray(sample.v.isel(selector).values, dtype=np.float64).reshape(len(timestamps), -1)
        u_units, v_units = sample.units(u_variable), sample.units(v_variable)

        u_mean = np.full(len(timestamps), np.nan)
        v_mean = np.full(len(timestamps), np.nan)
        statuses: list[SampleStatus | None] = []
        counts: list[int] = []
        for t in range(len(timestamps)):
            u_raw, v_raw = u_cube[t], v_cube[t]
            present = (
                ~np.isnan(u_raw)
                & ~np.isnan(v_raw)
                & (u_raw != DOCUMENTED_COMPONENT_FILL_VALUE)
                & (v_raw != DOCUMENTED_COMPONENT_FILL_VALUE)
            )
            counts.append(int(present.sum()))
            if not present.any():
                statuses.append(SampleStatus.MISSING)
                continue
            valid = component_valid_mask(to_meters_per_second(u_raw, u_units), to_meters_per_second(v_raw, v_units))
            if (present & ~valid).any():
                statuses.append(SampleStatus.INVALID)
                continue
            u_mean[t] = float(np.mean(u_raw[present]))
            v_mean[t] = float(np.mean(v_raw[present]))
            statuses.append(None)

        return ExtractedSeries(
            timestamps=timestamps,
            u=u_mean,
            v=v_mean,
            u_units=u_units,
            v_units=v_units,
            u_variable=u_variable,
            v_variable=v_variable,
            selection=SelectionParameters(
                mode="region",
                start=start_utc,
                end=end_utc,
                south=south,
                north=north,
                west=west,
                east=east,
                region_cell_count=int(lat_idx.size * lon_idx.size),
            ),
            source=sample.source(dataset_name, dataset_version),
            forced_status=tuple(statuses),
            cell_counts=tuple(counts),
        )
    finally:
        sample.dataset.close()


def extract_default_series(
    path: str | Path,
    *,
    mode: Literal["point", "region"] = "point",
    settings: Settings | None = None,
) -> ExtractedSeries:
    """Extract using the reproducible defaults from ``config.py``."""
    s = settings or get_settings()
    common = {
        "start": s.processing_start,
        "end": s.processing_end,
        "u_variable": s.u_variable,
        "v_variable": s.v_variable,
        "dataset_name": s.dataset_short_name,
        "dataset_version": s.dataset_version,
    }
    if mode == "point":
        return extract_point_series(path, latitude=s.processing_latitude, longitude=s.processing_longitude, **common)
    if mode == "region":
        return extract_region_series(
            path,
            south=s.processing_south,
            north=s.processing_north,
            west=s.processing_west,
            east=s.processing_east,
            **common,
        )
    raise SeriesExtractionError(f"Unknown extraction mode {mode!r}")
